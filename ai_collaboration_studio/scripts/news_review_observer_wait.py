"""Pre-activation receipt waiting; never launches or activates the trial."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from backend.news_review_monitor import publish_json_once
from backend.news_review_monitor_probe import ProbeError, canonical, integer, native_ticks, require

WAIT_REQUEST = 'news_review_observer_wait_request_v1'
WAIT_PLAN = 'news_review_observer_wait_plan_v1'
STATIC_RECEIPTS = ('preparation-manifest.json', 'ci-verification.json', 'activation-proposal.json')


def readiness_order(ready_ns, implementation, activation):
    before,after = activation.get('activation_monotonic_before_ns'),activation.get('activation_monotonic_after_ns')
    if (implementation != 'QueryPerformanceCounter()'
            or activation.get('activation_monotonic_implementation') != implementation
            or not all(integer(v) for v in (ready_ns,before,after)) or before > after):
        return 'unconfirmed'
    if ready_ns < before:
        return 'before_activation_verified'
    if ready_ns > after:
        return 'after_activation_observed'
    return 'unconfirmed'


def prepare_wait(request, powershell, *, core):
    require(type(request) is dict and set(request) == {'version','root','candidate_sha','activation_sha256',
        'policy_id','source_standards'} and request['version'] == WAIT_REQUEST, 'waiting_request_invalid')
    require(type(request['candidate_sha']) is str and re.fullmatch('[0-9a-f]{40}',request['candidate_sha'])
            and type(request['activation_sha256']) is str and re.fullmatch('[0-9a-f]{64}',request['activation_sha256']),
            'waiting_identity_invalid')
    standards = request['source_standards']
    require(type(standards) is dict and set(standards) == {'sec_filings','company_ir'}
            and all(type(v) is dict and set(v) == {'first_success_ms','maximum_gap_ms'}
                    and all(integer(n,positive=True) for n in v.values()) for v in standards.values()),
            'monitor_standards_invalid')
    root = core.safe_path(request['root'])
    require(root.name.startswith('NewsReviewTrial') and not root.is_relative_to(core.APP.parent), 'isolated_trial_required')
    require(not any((root/name).exists() for name in ('activation-receipt.json','activation-policy.json')),
            'preactivation_observer_required')
    records = {name:core.read_record(core.safe_path(str(root/name))) for name in STATIC_RECEIPTS}
    core.verify_preparation(root,request,records[STATIC_RECEIPTS[0]][0],records[STATIC_RECEIPTS[1]][0])
    proposal = records['activation-proposal.json'][0]
    activation = proposal.get('activation')
    require(type(activation) is dict and proposal.get('activation_sha256') == canonical(activation)
            == request['activation_sha256'], 'activation_proposal_mismatch')
    template = activation.get('policy_template',{})
    require(template.get('candidate_sha') == request['candidate_sha'] and template.get('policy_id') == request['policy_id']
            and core.safe_path(template.get('database_path')) == root/'data'/'studio.sqlite3', 'activation_proposal_mismatch')
    require(integer(template.get('not_before_ms')) and integer(template.get('expires_at_ms'),positive=True)
            and template['not_before_ms'] < template['expires_at_ms'] and activation.get('activations') == 1
            and integer(activation.get('duration_ms'),positive=True) and activation['duration_ms'] <= 86_400_000
            and activation.get('permitted_changes') in (['not_before_ms','expires_at_ms'],
                                                       ['not_before_ms','expires_at_ms','clock_activation']),
            'activation_proposal_mismatch')
    core.verify_checkout(request['candidate_sha'])
    shell = core.safe_path(powershell)
    require(shell.name.lower() == 'pwsh.exe' and shell.is_file(), 'powershell7_required')
    plan = {'version':WAIT_PLAN,'request':copy.deepcopy(request),'activation':activation,
        'receipt_sha256':{name:v[1] for name,v in records.items()},
        'monitor_files':{name:core.sha(core.APP/name) for name in core.FILES},
        'powershell':{'path':str(shell),'sha256':core.sha(shell)},
        'python':{'path':str(Path(sys.executable).resolve()),'sha256':core.sha(sys.executable)},
        'output_directory':str(root/'monitoring'),'poll_interval_ms':300000,'waiting_interval_ms':30000,
        'inspection_timeout_seconds':45,'status_timeout_seconds':15,'maximum_silence_ms':360000,
        'receipt_publication_grace_ms':45000,'activation_eligibility_until_ms':template['expires_at_ms'],
        'observer_activations':1,'request_authorized':False,
        'permitted_binding':'existing_approved_launch_activation_and_native_host_receipts_only'}
    return {'plan':plan,'plan_sha256':canonical(plan)}


class WaitingSession:
    def __init__(self, plan, directory, *, core, wall_ms, monotonic_ms):
        self.plan, self.directory, self.core = copy.deepcopy(plan),Path(directory),core
        self.wall_ms,self.monotonic_ms = wall_ms,monotonic_ms
        self.root = core.safe_path(plan['request']['root'])
        self.identity = {k:plan['request'][k] for k in ('candidate_sha','activation_sha256')}
        self.identity['policy_sha256'] = None  # Explicitly unresolved; never a placeholder policy hash.
        self.hashes,self.pending = {},{}
        self.inspector = None
        self.launcher = None
        self.activated_until = None
        self.activated_seen_mono = None
        self.activation_receipt = None

    def verify_static(self):
        for name,expected in self.plan['receipt_sha256'].items():
            require(self.core.sha(self.core.safe_path(str(self.root/name))) == expected, 'waiting_preparation_changed')
        for name,expected in self.plan['monitor_files'].items():
            require(self.core.sha(self.core.APP/name) == expected, 'monitor_source_changed')
        for name in ('python','powershell'):
            require(self.core.sha(self.plan[name]['path']) == self.plan[name]['sha256'], 'observer_runtime_changed')

    def record(self, name):
        require(name in ('user-approval.json','launch-receipt.json','launcher-start.json','activation-policy.json',
                         'activation-receipt.json') or re.fullmatch(r'host-[A-Za-z0-9_-]+\.json',name), 'unsafe_wait_receipt')
        path = self.core.safe_path(str(self.root/name))
        if not path.exists():
            require(name not in self.hashes, 'waiting_receipt_disappeared')
            return None
        try:
            value,digest = self.core.read_record(path)
        except (json.JSONDecodeError,UnicodeError):
            since = self.pending.setdefault(name,self.monotonic_ms())
            require(self.monotonic_ms()-since <= self.plan['receipt_publication_grace_ms'], 'receipt_publication_incomplete')
            return None
        require(type(value) is dict, 'waiting_receipt_invalid')
        require(name not in self.hashes or digest == self.hashes[name], 'waiting_receipt_changed')
        self.hashes[name] = digest
        self.pending.pop(name,None)
        return value

    def pair(self, names, label):
        values = [self.record(name) for name in names]
        if all(v is not None for v in values):
            self.pending.pop(label,None)
        elif any((self.root/name).exists() for name in names):
            since = self.pending.setdefault(label,self.monotonic_ms())
            require(self.monotonic_ms()-since <= self.plan['receipt_publication_grace_ms'], 'receipt_publication_incomplete')
        return values

    def activation(self):
        activation,policy = self.pair(('activation-receipt.json','activation-policy.json'),'activation_pair')
        if activation is None or policy is None:
            return None,None
        require(activation.get('activation') == self.plan['activation']
                and activation.get('activation_sha256') == self.identity['activation_sha256']
                and canonical(policy) == activation.get('policy_sha256'), 'activation_receipt_mismatch')
        begin,end = activation.get('activated_at'),activation.get('expires_at')
        template = self.plan['activation']['policy_template']
        changes = self.plan['activation']['permitted_changes']
        require(integer(begin) and integer(end) and template['not_before_ms'] <= begin < template['expires_at_ms']
                and end-begin == self.plan['activation']['duration_ms']
                and policy.get('not_before_ms') == begin and policy.get('expires_at_ms') == end
                and {k:v for k,v in policy.items() if k not in changes} == {k:v for k,v in template.items() if k not in changes},
                'activation_window_mismatch')
        self.activated_until = end
        self.activation_receipt = activation
        reading = policy.get('clock_activation')
        if isinstance(reading,dict):
            require(activation.get('activation_monotonic_before_ns') == reading.get('monotonic_before_ns')
                    and activation.get('activation_monotonic_after_ns') == reading.get('monotonic_after_ns'),
                    'activation_monotonic_evidence_mismatch')
        if self.activated_seen_mono is None: self.activated_seen_mono = self.monotonic_ms()
        require(self.wall_ms() < end+255000, 'observer_window_closed')
        return activation,policy

    def step(self):
        self.verify_static()
        approval = self.record('user-approval.json')
        if approval is None:
            return {'phase':'waiting_trial_approval','inspection_complete':None}
        require(approval.get('approved') is True and approval.get('preparation_manifest_sha256')
                == self.plan['receipt_sha256']['preparation-manifest.json']
                and all(approval.get(k) == self.identity[k] for k in ('candidate_sha','activation_sha256')),
                'trial_approval_unconfirmed')
        launch,start = self.pair(('launch-receipt.json','launcher-start.json'),'launch_pair')
        if launch is None or start is None:
            return {'phase':'waiting_launch_receipts','inspection_complete':None}
        require(all(v.get(k) == self.identity[k] for v in (launch,start) for k in ('candidate_sha','activation_sha256')),
                'launcher_receipt_mismatch')
        launcher = {'pid':launch.get('process_id'),'start_utc':launch.get('process_start_utc'),'parent_pid':None}
        self.core.pin_key(launcher)
        require(start.get('launcher_pid') == launcher['pid']
                and native_ticks(start.get('launcher_process_start_utc')) == native_ticks(launcher['start_utc']),
                'launcher_receipt_mismatch')
        if self.inspector is None:
            self.launcher = launcher
            native_plan = {**self.plan,'request':{'target':{'identity':self.identity,'host_url':None},
                'launcher_pin':launcher,'host_pin':None,'pins':[launcher]}}
            self.inspector = self.core.NativeInspector(native_plan,self.directory)
        require(self.core.pin_key(launcher) == self.core.pin_key(self.launcher), 'launcher_generation_changed')
        activation,policy = self.activation()
        try:
            tree = self.inspector()
        except ProbeError as exc:
            if str(exc) in ('observer_not_independent','monitor_source_changed','powershell_changed'):
                raise
            return {'phase':'process_inspection_incomplete','inspection_complete':False}
        complete = tree.get('known_identity_inspection_incomplete') is False and tree.get('enumeration_consistent') is True
        if activation is None or policy is None:
            if complete and all(p['alive'] is False for p in tree['processes']):
                raise ProbeError('registered_launch_tree_gone_before_activation')
            phase = ('waiting_receipt_publication' if any((self.root/n).exists() for n in ('activation-receipt.json','activation-policy.json'))
                     else 'waiting_local_input')
            return {'phase':phase if complete else 'process_inspection_incomplete',
                    'inspection_complete':complete}
        begin,end = activation.get('activated_at'),activation.get('expires_at')
        hosts = list(self.root.glob('host-*.json'))
        require(len(hosts) <= 1, 'host_receipt_ambiguous')
        host = self.record(hosts[0].name) if hosts else None
        if host is None:
            require(self.monotonic_ms()-self.activated_seen_mono <= self.plan['maximum_silence_ms'], 'activated_host_unconfirmed')
            return {'phase':'waiting_host_receipt','inspection_complete':complete}
        require(integer(host.get('process_id'),positive=True) and integer(host.get('process_start_utc_ticks'),positive=True),
                'host_native_pin_missing')
        pins = [p for p in self.inspector.pins if p['pid'] == host['process_id']
                and native_ticks(p['start_utc']) == host['process_start_utc_ticks']]
        if not complete or len(pins) != 1:
            return {'phase':'activation_host_identity_unconfirmed','inspection_complete':False}
        target = {'identity':{**self.identity,'policy_sha256':activation['policy_sha256']},
            'policy_id':self.plan['request']['policy_id'],'activated_at_ms':begin,'expires_at_ms':end,
            'host_url':host.get('url'),'source_standards':copy.deepcopy(self.plan['request']['source_standards'])}
        request = {'version':'news_review_observer_request_v1','root':str(self.root),'target':target,
            'host_receipt_name':hosts[0].name,'launcher_pin':self.launcher,'host_pin':pins[0],
            'pins':copy.deepcopy(self.inspector.pins)}
        active = self.core.prepare(request,self.plan['powershell']['path'])['plan']
        require(all(active[k] == self.plan[k] for k in ('monitor_files','powershell','python','output_directory')),
                'observer_preparation_changed')
        return {'phase':'activation_receipts_bound','inspection_complete':True,'resolved_plan':active}


def write_wait(directory, identity, sequence, previous, *, started, completed, elapsed, phase, inspection_complete):
    receipt = {'version':'news_review_observer_wait_execution_v1','identity':identity,'sequence':sequence,
        'previous_sha256':previous,'started_at_ms':started,'completed_at_ms':completed,
        'elapsed_monotonic_ms':elapsed,'phase':phase,'inspection_complete':inspection_complete,
        'http_get_requests':0,'activation_created':False,'key_window_opened':False}
    digest = hashlib.sha256(json.dumps(receipt,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    publish_json_once(directory/f'monitor-wait-execution-{sequence:06d}.json',{'receipt':receipt,'sha256':digest})
    return digest


def run_waiting(plan, plan_hash, *, core):
    directory = core.safe_path(plan['output_directory'])
    directory.mkdir(exist_ok=False)
    wall = lambda:time.time_ns()//1_000_000
    mono = lambda:time.monotonic_ns()//1_000_000
    started,started_mono = wall(),mono()
    identity = {k:plan['request'][k] for k in ('candidate_sha','activation_sha256')}
    identity['policy_sha256'] = None
    ticks = core.native_creation_ticks(os.getpid())
    require(integer(ticks,positive=True), 'observer_native_identity_unconfirmed')
    pin = {'version':'news_review_waiting_observer_pin_v1','identity':identity,'plan_sha256':plan_hash,
        'pid':os.getpid(),'process_start_utc_ticks':ticks,'started_at_ms':started}
    publish_json_once(directory/'observer-pin.json',pin)
    ready_ns = time.monotonic_ns()
    ready_implementation = time.get_clock_info('monotonic').implementation
    session = WaitingSession(plan,directory,core=core,wall_ms=wall,monotonic_ms=mono)
    maximum_end = plan['activation_eligibility_until_ms']+plan['activation']['duration_ms']+255000
    maximum_mono = started_mono+max(0,maximum_end-started)
    eligibility_mono = started_mono+max(0,plan['activation_eligibility_until_ms']-started)
    sequence,previous,scheduled = 0,'',started
    ended = {'phase':'waiting_window_closed','code':'OBSERVER_WINDOW_CLOSED'}
    transitioned = False
    try:
        while wall() < maximum_end and mono() < maximum_mono:
            begin,begin_mono = wall(),mono()
            publish_json_once(directory/f'monitor-wait-start-{sequence+1:06d}.json',
                {'identity':identity,'scheduled_at_ms':scheduled,'started_at_ms':begin})
            result = session.step()
            sequence += 1
            previous = write_wait(directory,identity,sequence,previous,started=begin,completed=wall(),
                elapsed=mono()-begin_mono,phase=result['phase'],inspection_complete=result['inspection_complete'])
            active = result.get('resolved_plan')
            if active is not None:
                session.inspector.bind_active(active)
                order = readiness_order(ready_ns,ready_implementation,session.activation_receipt)
                binding = {'version':'news_review_observer_activation_binding_v1','original_plan_sha256':plan_hash,
                    'resolved_plan_sha256':canonical(active),'resolved_plan':active,'observer_pid':os.getpid(),
                    'observer_process_start_utc_ticks':ticks,'observer_ready_at_ms':started,
                    'bound_at_ms':wall(),'waiting_last_sha256':previous,'waiting_checks':sequence,
                    'observer_ready_monotonic_ns':ready_ns,'observer_monotonic_implementation':ready_implementation,
                    'activation_monotonic_before_ns':session.activation_receipt.get('activation_monotonic_before_ns'),
                    'activation_monotonic_after_ns':session.activation_receipt.get('activation_monotonic_after_ns'),
                    'activation_monotonic_implementation':session.activation_receipt.get('activation_monotonic_implementation'),
                    'pre_activation_order':order,'observer_ready_before_activation':order == 'before_activation_verified',
                    'activation_created':False,'host_started_by_observer':False,'approval_fabricated':False}
                publish_json_once(directory/'observer-activation-binding.json',binding)
                publish_json_once(directory/'observer-active-pin.json',{
                    **pin,'version':'news_review_active_observer_pin_v1','identity':active['request']['target']['identity'],
                    'started_at_ms':wall(),'original_started_at_ms':started,'binding_sha256':canonical(binding)})
                transitioned = True
                return core.observe_active(active,plan_hash,directory,inspector=session.inspector)
            if session.activated_until is None and (wall() >= plan['activation_eligibility_until_ms'] or mono() >= eligibility_mono):
                raise ProbeError('activation_eligibility_elapsed_without_complete_receipt')
            if session.activated_until is not None and wall() >= session.activated_until+255000:
                raise ProbeError('observer_window_closed')
            scheduled += plan['waiting_interval_ms']
            now = wall()
            if scheduled <= now: scheduled = now+plan['waiting_interval_ms']
            delay = min(scheduled-now,maximum_end-now,maximum_mono-mono())
            threading.Event().wait(max(0,delay)/1000)
    except BaseException as exc:
        code = str(exc) if isinstance(exc,ProbeError) else 'OBSERVER_WAIT_INCOMPLETE'
        ended = {'phase':'waiting_failure_user_action_required','code':code}
        raise
    finally:
        if not transitioned or not (directory/'observer-exit.json').exists():
            publish_json_once(directory/'observer-exit.json',{
                'version':'news_review_observer_wait_exit_v1','identity':identity,'plan_sha256':plan_hash,
                'ended_at_ms':wall(),'completed_wait_checks':sequence,**ended,
                'activation_created':False,'key_window_opened':False,'final_acceptance_verified':False})
    return 2
