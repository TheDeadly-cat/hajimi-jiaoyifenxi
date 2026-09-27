"""One independent local watchdog check. No GET, DB, restart or notification send.

Schedule this separately only after the future observer is prepared/approved.
The exit status and saved alert receipt are the scheduler's notification input.
"""
import argparse
import ctypes
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.news_review_monitor import evaluate_watchdog, notification_changes, publish_json_once


def native_creation_ticks(pid):
    if os.name!='nt' or type(pid) is not int or pid<=0:
        return None
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
    kernel.GetProcessTimes.restype=wintypes.BOOL
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    kernel.WaitForSingleObject.restype=wintypes.DWORD
    handle=kernel.OpenProcess(0x101000,False,pid)
    if not handle:
        return None
    try:
        if kernel.WaitForSingleObject(handle,0)!=258:
            return None
        fields=[wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle,*[ctypes.byref(v) for v in fields]):
            return None
        return (fields[0].dwHighDateTime<<32)+fields[0].dwLowDateTime+504911232000000000
    finally:
        kernel.CloseHandle(handle)


def read_receipt_chain(directory, identity, *, prefix='monitor-execution'):
    if prefix not in ('monitor-execution','monitor-wait-execution'):
        raise ValueError('receipt_prefix_invalid')
    previous=''
    latest=None
    paths=sorted(Path(directory).glob(prefix+'-*.json'))
    if len(paths)>10000:
        raise ValueError('receipt_count_limit')
    for sequence,path in enumerate(paths,1):
        if path.stat().st_size>65536:
            raise ValueError('receipt_size_limit')
        wrapped=json.loads(path.read_text(encoding='utf-8'))
        r=wrapped['receipt']
        digest=hashlib.sha256(json.dumps(r,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if (wrapped['sha256']!=digest or r['identity']!=identity or r['sequence']!=sequence
                or r['previous_sha256']!=previous or path.name!=f'{prefix}-{sequence:06d}.json'):
            raise ValueError('receipt_chain_unconfirmed')
        previous,latest=digest,r
    return latest


def check(directory, pin, previous_alerts, *, now_ms, inspect=native_creation_ticks):
    identity=pin['identity']
    expected=pin.get('process_start_utc_ticks')
    first,second=inspect(pin.get('pid')),inspect(pin.get('pid'))
    alive=type(expected) is int and expected>0 and first==second==expected
    integrity=False
    try:
        receipt=read_receipt_chain(directory,identity)
        integrity=receipt is not None
    except (ValueError,KeyError,OSError,TypeError):
        receipt=None
    verdict=evaluate_watchdog(receipt,identity=identity,now_ms=now_ms,observer_identity_alive=alive)
    if first!=second or first is not None and first!=expected:
        verdict['alerts'].append('OBSERVER_IDENTITY_UNCONFIRMED')
    verdict['notification_required']=bool(verdict['alerts'])
    verdict['receipt_chain_verified']=integrity
    verdict['changes']=notification_changes(previous_alerts,verdict)
    verdict['alerts']=verdict['changes']['active_alerts']
    verdict['notification_required']=bool(verdict['alerts'])
    return verdict


def _bounded_record(path):
    with Path(path).open('rb') as stream:
        data=stream.read(131073)
    if len(data)>131072:
        raise ValueError('watchdog_record_size_limit')
    return json.loads(data.decode('utf-8'))


def check_waiting_session(directory, pin, previous, *, now_ms, inspect=native_creation_ticks):
    """Track one approved observer through waiting and actual binding.

    Session identity keeps policy_sha256 explicitly null until a verified
    binding exists. Successful GET receipts keep their actual policy identity.
    No database, HTTP, process launch or application action occurs here.
    """
    identity=pin.get('identity',{})
    if (pin.get('version')!='news_review_waiting_observer_pin_v1'
            or set(identity)!={'candidate_sha','activation_sha256','policy_sha256'}
            or identity['policy_sha256'] is not None
            or any(type(identity.get(k)) is not str or re.fullmatch('[0-9a-f]{'+str(n)+'}',identity[k]) is None
                   for k,n in (('candidate_sha',40),('activation_sha256',64)))
            or type(pin.get('plan_sha256')) is not str or re.fullmatch('[0-9a-f]{64}',pin['plan_sha256']) is None):
        raise ValueError('waiting_pin_invalid')
    previous=previous or {}
    if previous and (previous.get('identity')!=identity or previous.get('plan_sha256')!=pin['plan_sha256']
                     or type(previous.get('actual_activation_confirmed')) is not bool):
        raise ValueError('previous_watchdog_identity_mismatch')
    root=Path(directory)
    first,second=inspect(pin.get('pid')),inspect(pin.get('pid'))
    expected=pin.get('process_start_utc_ticks')
    alive=type(expected) is int and expected>0 and first==second==expected
    alerts=[] if alive else ['OBSERVER_LIVENESS_UNCONFIRMED']
    if first!=second or first is not None and first!=expected:
        alerts.append('OBSERVER_IDENTITY_UNCONFIRMED')
    recovery={code:False for code in ('HOST_NOT_ALIVE','WORKER_NOT_ALIVE',
        'SOURCE_FULL_SUCCESS_STALE','LAUNCHER_EXITED_CHILD_ALIVE')}
    guard_alerts=list(alerts)
    verdict={'version':'news_review_waiting_watchdog_v1','identity':identity,'plan_sha256':pin['plan_sha256'],'checked_at_ms':now_ms,
        'alerts':alerts,'monitoring_phase':'waiting','actual_activation_confirmed':False,
        'health_recovery_confirmed':recovery,'automatic_actions':[],
        'receipt_chain_verified':False,'live_acceptance_proven':False}
    # Once bound, missing/corrupt files cannot reset the session to waiting or
    # grant a fresh first-read grace period on a later watchdog invocation.
    for key in ('resolved_policy_sha256','resolved_binding_sha256','active_pin_sha256','active_receipt_seen'):
        if key in previous:
            verdict[key]=previous[key]
    try:
        waiting=read_receipt_chain(root,identity,prefix='monitor-wait-execution')
        active_path=root/'observer-active-pin.json'
        if active_path.exists():
            active=_bounded_record(active_path)
            binding=_bounded_record(root/'observer-activation-binding.json')
            digest=hashlib.sha256(json.dumps(binding,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
            active_digest=hashlib.sha256(json.dumps(active,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
            resolved_digest=hashlib.sha256(json.dumps(binding.get('resolved_plan'),ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
            if (active.get('version')!='news_review_active_observer_pin_v1'
                    or active.get('pid')!=pin.get('pid') or active.get('process_start_utc_ticks')!=expected
                    or active.get('plan_sha256')!=pin['plan_sha256'] or binding.get('original_plan_sha256')!=pin['plan_sha256']
                    or binding.get('observer_pid')!=pin.get('pid') or binding.get('observer_process_start_utc_ticks')!=expected
                    or active.get('binding_sha256')!=digest or not waiting
                    or binding.get('resolved_plan_sha256')!=resolved_digest
                    or binding.get('waiting_checks')!=waiting.get('sequence')
                    or binding.get('waiting_last_sha256')!=hashlib.sha256(json.dumps(waiting,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                    or any(active.get('identity',{}).get(k)!=identity[k] for k in ('candidate_sha','activation_sha256'))
                    or binding.get('resolved_plan',{}).get('request',{}).get('target',{}).get('identity')!=active.get('identity')):
                raise ValueError('observer_activation_binding_unconfirmed')
            policy=active['identity']['policy_sha256']
            if (type(policy) is not str or re.fullmatch('[0-9a-f]{64}',policy) is None
                    or previous.get('resolved_policy_sha256') not in (None,policy)
                    or previous.get('resolved_binding_sha256') not in (None,digest)
                    or previous.get('active_pin_sha256') not in (None,active_digest)):
                raise ValueError('observer_activation_binding_changed')
            verdict['resolved_policy_sha256']=policy
            verdict['resolved_binding_sha256']=digest
            verdict['active_pin_sha256']=active_digest
            verdict['active_receipt_seen']=bool(previous.get('active_receipt_seen') or list(root.glob('monitor-execution-*.json')))
            receipt=read_receipt_chain(root,active['identity'])
            if receipt is None and not verdict['active_receipt_seen'] and type(active.get('started_at_ms')) is int and 0<=now_ms-active['started_at_ms']<=360000:
                verdict['monitoring_phase']='active_starting'
            else:
                evaluated=check(root,active,previous.get('alerts',[]),now_ms=now_ms,inspect=inspect)
                verdict.update(evaluated,identity=identity,monitoring_phase='active',resolved_policy_sha256=policy)
                verdict['actual_activation_confirmed']=bool(receipt and receipt.get('read_succeeded') is True)
                if receipt is None and type(active.get('started_at_ms')) is int and now_ms-active['started_at_ms']>360000:
                    verdict['alerts'].append('MONITOR_SUCCESSFUL_READ_OVERDUE')
            from scripts.news_review_observer_wait import readiness_order
            order=readiness_order(binding.get('observer_ready_monotonic_ns'),binding.get('observer_monotonic_implementation'),binding)
            if order!=binding.get('pre_activation_order') or binding.get('observer_ready_before_activation') is not (order=='before_activation_verified'):
                raise ValueError('observer_readiness_order_unconfirmed')
            if order=='after_activation_observed':
                verdict['alerts'].append('OBSERVATION_STARTED_AFTER_ACTIVATION')
            elif order!='before_activation_verified':
                verdict['alerts'].append('OBSERVATION_ORDER_UNCONFIRMED')
        else:
            if previous.get('resolved_policy_sha256') is not None or previous.get('actual_activation_confirmed'):
                raise ValueError('observer_activation_binding_disappeared')
            started=pin.get('started_at_ms')
            completed=waiting.get('completed_at_ms') if waiting else started
            if type(completed) is not int or completed>now_ms:
                alerts.append('MONITOR_CLOCK_UNCONFIRMED')
            elif now_ms-completed>90000:
                alerts.append('MONITOR_WAIT_EXECUTION_OVERDUE')
            if waiting:
                if (waiting.get('version')!='news_review_observer_wait_execution_v1'
                        or waiting.get('http_get_requests')!=0 or waiting.get('activation_created') is not False
                        or waiting.get('key_window_opened') is not False):
                    raise ValueError('waiting_receipt_unconfirmed')
                verdict['receipt_chain_verified']=True
                if waiting.get('inspection_complete') is False:
                    alerts.append('TRIAL_PROCESS_INSPECTION_UNCONFIRMED')
        exit_path=root/'observer-exit.json'
        if exit_path.exists():
            ended=_bounded_record(exit_path)
            if (ended.get('plan_sha256')!=pin['plan_sha256']
                    or any(ended.get('identity',{}).get(k)!=identity[k] for k in ('candidate_sha','activation_sha256'))):
                raise ValueError('observer_exit_identity_unconfirmed')
            if (verdict.get('resolved_policy_sha256') is not None
                    and (ended.get('version')!='news_review_observer_exit_v1'
                         or ended.get('identity')!={**identity,'policy_sha256':verdict['resolved_policy_sha256']})):
                raise ValueError('observer_exit_policy_unconfirmed')
            if (ended.get('phase')=='registered_tree_terminal' and ended.get('two_native_terminal_checks') is True
                    and verdict.get('resolved_policy_sha256') is not None
                    and ended.get('original_report_exists') is True
                    and type(ended.get('original_report_sha256')) is str
                    and re.fullmatch('[0-9a-f]{64}',ended['original_report_sha256'])):
                verdict['monitoring_phase']='terminal_review'
                verdict['final_verification_required']=True
                # The exit receipt routes to final verification; it does not
                # assert current ownership, a final ledger or accepted quality.
                verdict['alerts']=[v for v in verdict['alerts'] if v not in ('OBSERVER_LIVENESS_UNCONFIRMED',
                    'MONITOR_READ_UNCONFIRMED','MONITOR_SUCCESSFUL_READ_OVERDUE','MONITOR_EXECUTION_OVERDUE')]
            else:
                verdict['alerts'].append('OBSERVER_EXIT_REQUIRES_USER_ACTION')
    except (ValueError,KeyError,OSError,TypeError):
        verdict['alerts'].append('OBSERVER_SESSION_EVIDENCE_UNCONFIRMED')
    if verdict['monitoring_phase']!='terminal_review':
        verdict['alerts']=sorted(set(verdict['alerts'])|set(guard_alerts))
    elif 'OBSERVER_IDENTITY_UNCONFIRMED' in guard_alerts:
        verdict['alerts'].append('OBSERVER_IDENTITY_UNCONFIRMED')
    verdict['actual_activation_confirmed']=bool(verdict['actual_activation_confirmed'] or previous.get('actual_activation_confirmed'))
    events=[]
    if verdict['actual_activation_confirmed'] and not previous.get('actual_activation_confirmed'):
        events.append('ACTIVATION_CONFIRMED')
    if verdict.get('final_verification_required') and not previous.get('final_verification_required'):
        events.append('FINAL_VERIFICATION_REQUIRED')
    changes=notification_changes(previous.get('alerts',[]),verdict)
    changes['events']=events
    changes['notify']=changes['notify'] or bool(events)
    verdict['changes']=changes
    verdict['version']='news_review_waiting_watchdog_v1'
    verdict['alerts']=changes['active_alerts']
    verdict['notification_required']=bool(verdict['alerts']) or bool(events)
    return verdict


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipts',required=True)
    parser.add_argument('--pin',required=True,help='Prepared PID, native UTC ticks and candidate/activation/policy identity JSON')
    parser.add_argument('--previous',help='Previous watchdog output for notification deduplication')
    parser.add_argument('--output',required=True,help='New unique JSON output; existing files are never overwritten')
    args=parser.parse_args()
    pin=json.loads(Path(args.pin).read_text(encoding='utf-8'))
    previous=json.loads(Path(args.previous).read_text(encoding='utf-8')) if args.previous else None
    if previous and previous['identity']!=pin['identity']:
        raise ValueError('previous_watchdog_identity_mismatch')
    verdict=(check_waiting_session(args.receipts,pin,previous,now_ms=time.time_ns()//1_000_000)
             if pin.get('version')=='news_review_waiting_observer_pin_v1' else
             check(args.receipts,pin,previous['alerts'] if previous else [],now_ms=time.time_ns()//1_000_000))
    publish_json_once(args.output, verdict)
    print(json.dumps({'output':args.output,'notify':verdict['changes']['notify'],'alerts':verdict['alerts']}))
    return 2 if verdict['changes']['notify'] else 0


if __name__=='__main__':
    raise SystemExit(main())
