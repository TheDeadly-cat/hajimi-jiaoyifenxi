"""Read-only observation receipts and a separate missed-execution evaluator.

No process launch, signal, database, HTTP or model authority is provided here.
The probe supplied by the separately approved local observer owns identity and
loopback validation. A watchdog must run independently of that observer.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path


def _integer(value):
    return type(value) is int and value>=0


def _identity(value):
    return (type(value) is dict and set(value)=={'candidate_sha','activation_sha256','policy_sha256'}
            and all(type(v) is str and re.fullmatch('[0-9a-f]{'+str(40 if k=='candidate_sha' else 64)+'}',v)
                    for k,v in value.items()))


def source_freshness(adapters, *, now_ms, maximum_gaps):
    results=[]
    for adapter in adapters:
        key=adapter['adapter_key']
        last=adapter.get('last_success_at_ms')
        bound=maximum_gaps.get(key)
        if not _integer(now_ms) or bound is not None and (not _integer(bound) or bound==0):
            raise ValueError('invalid_source_freshness_standard')
        age=now_ms-last if _integer(last) and last>0 else None
        state=('unconfigured' if bound is None else 'no_full_success' if age is None else
               'clock_unconfirmed' if age<0 else 'stale' if age>bound else 'fresh')
        results.append({'adapter_key':key,'last_full_success_at_ms':last,'full_success_age_ms':age,
            'maximum_gap_ms':bound,'freshness':state,'source_state':adapter.get('state'),
            'last_error_code':adapter.get('last_error_code',''),'next_due_at_ms':adapter.get('next_due_at_ms'),
            'backoff_not_overridden':True,'automatic_retry_requested':False})
    return results


class MonitorReceiptWriter:
    def __init__(self, directory, identity, *, wall_ms, monotonic_ms):
        if not _identity(identity):
            raise ValueError('monitor_identity_invalid')
        self.directory=Path(directory)
        self.identity=copy.deepcopy(identity)
        self.wall_ms,self.monotonic_ms=wall_ms,monotonic_ms
        self.sequence=0
        self.last_successful_read_at_ms=None
        self.previous=''

    def execute(self, *, scheduled_at_ms, probe):
        started,mono_start=self.wall_ms(),self.monotonic_ms()
        if not all(_integer(v) for v in (scheduled_at_ms,started,mono_start)):
            raise ValueError('invalid_monitor_clock')
        result={'version':'news_review_monitor_execution_v1','identity':copy.deepcopy(self.identity),
            'scheduled_at_ms':scheduled_at_ms,'started_at_ms':started,'completed_at_ms':None,
            'schedule_delay_ms':started-scheduled_at_ms,'elapsed_monotonic_ms':None,
            'read_succeeded':False,'error_code':'','model_task_outcome_inferred':False,
            'http_retries':0,'automatic_restart_allowed':False}
        self.directory.mkdir(parents=True,exist_ok=True)
        # Durable evidence exists even if the bounded probe or its process
        # never returns. A second writer cannot reuse this run's sequence.
        with (self.directory/f'monitor-start-{self.sequence+1:06d}.json').open('x',encoding='utf-8') as f:
            json.dump(result,f,ensure_ascii=False,indent=2)
        try:
            observation=probe()
            if (type(observation) is not dict or observation.get('identity')!=self.identity
                    or observation.get('process_identity_verified') is not True
                    or observation.get('port_ownership_verified') is not True):
                raise ValueError('probe_identity_unconfirmed')
            # Persist only already validated counts/codes/liveness; never raw
            # responses, documents, exception text or process environments.
            health=observation['health']
            if type(health) is not dict or set(health)!={'host_alive','workers_alive','source_stale_keys'}:
                raise ValueError('probe_health_invalid')
            if (type(health['host_alive']) is not bool or type(health['workers_alive']) is not bool
                    or type(health['source_stale_keys']) is not list or len(health['source_stale_keys'])>32
                    or any(type(k) is not str or re.fullmatch('[a-z][a-z0-9_]{0,63}',k) is None for k in health['source_stale_keys'])):
                raise ValueError('probe_health_invalid')
            result['health']=copy.deepcopy(health)
            result['read_succeeded']=True
        except TimeoutError:
            result['error_code']='STATUS_READ_TIMEOUT'
        except Exception:
            result['error_code']='OBSERVATION_UNCONFIRMED'
        completed,mono_end=self.wall_ms(),self.monotonic_ms()
        if not all(_integer(v) for v in (completed,mono_end)):
            raise ValueError('invalid_monitor_clock')
        result['completed_at_ms']=completed
        result['elapsed_monotonic_ms']=mono_end-mono_start
        if result['read_succeeded']:
            self.last_successful_read_at_ms=completed
        result['last_successful_read_at_ms']=self.last_successful_read_at_ms
        result['sequence']=self.sequence+1
        result['previous_sha256']=self.previous
        digest=hashlib.sha256(json.dumps(result,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        self.directory.mkdir(parents=True,exist_ok=True)
        path=self.directory/f'monitor-execution-{result["sequence"]:06d}.json'
        with path.open('x',encoding='utf-8') as f:
            json.dump({'receipt':result,'sha256':digest},f,ensure_ascii=False,indent=2)
        self.previous=digest
        self.sequence+=1
        return result

    def run_schedule(self, *, probe, stop_event, expires_at_ms, interval_ms=300000):
        """Foreground loop for a separately approved observer, never auto-started.

        The injected probe must enforce its own process/port identity and
        bounded GET timeouts. No missed checks are replayed on wake/resume.
        """
        if not _integer(expires_at_ms) or not _integer(interval_ms) or interval_ms<=0:
            raise ValueError('invalid_monitor_schedule')
        scheduled=self.wall_ms()
        deadline_mono=self.monotonic_ms()+max(0,expires_at_ms-scheduled)
        while not stop_event.is_set() and self.wall_ms()<expires_at_ms and self.monotonic_ms()<deadline_mono:
            self.execute(scheduled_at_ms=scheduled,probe=probe)
            scheduled+=interval_ms
            now=self.wall_ms()
            if scheduled<=now:
                scheduled=now+interval_ms
            wait_ms=max(0,min(scheduled-now,expires_at_ms-now,deadline_mono-self.monotonic_ms()))
            if stop_event.wait(wait_ms/1000):
                break


def evaluate_watchdog(receipt, *, identity, now_ms, observer_identity_alive, maximum_silence_ms=360000):
    """Called by a different scheduler/process; queued wakeups are not receipts."""
    if not _identity(identity) or not _integer(now_ms) or not _integer(maximum_silence_ms) or maximum_silence_ms==0:
        raise ValueError('invalid_watchdog_input')
    alerts=[]
    if not observer_identity_alive:
        alerts.append('OBSERVER_LIVENESS_UNCONFIRMED')
    if type(receipt) is not dict or receipt.get('identity')!=identity:
        alerts.append('MONITOR_RECEIPT_IDENTITY_UNCONFIRMED')
    else:
        completed=receipt.get('completed_at_ms')
        if not _integer(completed):
            alerts.append('MONITOR_EXECUTION_UNCONFIRMED')
        elif completed>now_ms:
            alerts.append('MONITOR_CLOCK_UNCONFIRMED')
        elif now_ms-completed>maximum_silence_ms:
            alerts.append('MONITOR_EXECUTION_OVERDUE')
        if type(receipt.get('schedule_delay_ms')) is int and receipt['schedule_delay_ms']>maximum_silence_ms:
            alerts.append('MONITOR_SCHEDULE_DELAY_EXCEEDED')
        if receipt.get('read_succeeded') is not True:
            alerts.append('MONITOR_READ_UNCONFIRMED')
        last=receipt.get('last_successful_read_at_ms')
        if last is None or not _integer(last) or last>now_ms or now_ms-last>maximum_silence_ms:
            alerts.append('MONITOR_SUCCESSFUL_READ_OVERDUE')
        health=receipt.get('health',{})
        if health.get('host_alive') is False:
            alerts.append('HOST_NOT_ALIVE')
        if health.get('workers_alive') is False:
            alerts.append('WORKER_NOT_ALIVE')
        if health.get('source_stale_keys'):
            alerts.append('SOURCE_FULL_SUCCESS_STALE')
    return {'version':'news_review_watchdog_v1','identity':copy.deepcopy(identity),'checked_at_ms':now_ms,
        'alerts':alerts,'notification_required':bool(alerts),'automatic_actions':[],
        'independent_watchdog_required':True,'live_acceptance_proven':False}


def notification_changes(previous_alerts, verdict):
    previous,current=set(previous_alerts),set(verdict['alerts'])
    opened,resolved=sorted(current-previous),sorted(previous-current)
    return {'opened':opened,'resolved':resolved,'notify':bool(opened or resolved),
            'active_alerts':sorted(current),'automatic_actions':[]}
