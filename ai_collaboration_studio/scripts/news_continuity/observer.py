"""Read-only observer for an already-running, exactly bound daily news host."""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import signal
import stat
import time

HERE = Path(__file__).resolve().parent
INTERVAL, TARGET, MAXIMUM = 150, 86400, 86700
PATHS = ('/api/readiness','/api/version','/api/news/overview')
MAX_BODY, MAX_HEADER, REQUEST_SECONDS = 262144, 16384, 5
STOP_SIGNAL = None

def request_signal(signum, frame):
    global STOP_SIGNAL
    STOP_SIGNAL = int(signum)

OUTCOMES = {'started','success','not_modified','failed','interrupted_unknown'}
SOURCE_STATES = {'never','ok','error','fetching','interrupted_unknown'}
WORKER_STATES = {'running','paused','recovering','stopped','unresponsive','starting'}
GAP_KINDS = {'clean_restart','unclean_restart','heartbeat_gap','monitor_paused','clock_changed'}


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')


def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


class ObserverError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class RealClock:
    monotonic = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)

    @staticmethod
    def utcnow():
        return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')


def no_alias(path):
    current = Path(Path(os.path.abspath(path)).anchor)
    for part in Path(os.path.abspath(path)).parts[1:]:
        current /= part
        try:
            value = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(value.st_mode) or getattr(value,'st_file_attributes',0)&0x400:
            raise ObserverError('output_path_alias')


class ReceiptLog:
    def __init__(self, output):
        self.output = Path(output)
        no_alias(self.output)
        self.output.mkdir(exist_ok=False)
        self.stream = (self.output/'observations.jsonl').open('xb')
        self.sequence, self.previous = 0, '0'*64
        self.terminal_attempted = False

    def append(self, kind, data):
        value = {'version':'news_continuity_observation_v1','sequence':self.sequence,
                 'previous_sha256':self.previous,'kind':kind,'data':data}
        digest = sha(value)
        self.stream.write(canonical({**value,'record_sha256':digest})+b'\n')
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.previous = digest
        self.sequence += 1

    def terminal(self, value):
        if self.terminal_attempted:
            raise ObserverError('terminal_already_attempted')
        self.terminal_attempted = True
        self.append('terminal',value)
        receipt = {'version':'news_continuity_terminal_v1','terminal':value,
                   'log_records':self.sequence,'final_record_sha256':self.previous}
        receipt['receipt_sha256'] = sha(receipt)
        with (self.output/'terminal.json').open('xb') as stream:
            stream.write(canonical(receipt)+b'\n')
            stream.flush()
            os.fsync(stream.fileno())
        return receipt

    def close(self):
        self.stream.close()


def _unique_object(pairs):
    value = {}
    for key,item in pairs:
        if key in value:
            raise ObserverError('invalid_json')
        value[key] = item
    return value


class LoopbackTransport:
    """Direct TCP only: no proxy configuration, redirects, cookies or credentials."""
    def __init__(self, clock=None):
        self.clock = clock or RealClock()

    def get(self, path, *, deadline):
        if path not in PATHS:
            raise ObserverError('endpoint_not_allowed')
        deadline = min(deadline,self.clock.monotonic()+REQUEST_SECONDS)
        def remaining():
            seconds = deadline-self.clock.monotonic()
            if seconds <= 0:
                raise ObserverError('http_timeout')
            return seconds
        try:
            with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as connection:
                connection.settimeout(remaining())
                connection.connect(('127.0.0.1',8770))
                connection.settimeout(remaining())
                connection.sendall(('GET '+path+' HTTP/1.1\r\nHost: 127.0.0.1:8770\r\n'
                    'Accept: application/json\r\nConnection: close\r\nUser-Agent: LocalNewsObserver/1\r\n\r\n').encode('ascii'))
                header = bytearray()
                while b'\r\n\r\n' not in header:
                    connection.settimeout(remaining())
                    block = connection.recv(4096)
                    if not block:
                        raise ObserverError('incomplete_headers')
                    header.extend(block)
                    if len(header)>MAX_HEADER+4096:
                        raise ObserverError('headers_too_large')
                raw_header,body = bytes(header).split(b'\r\n\r\n',1)
                if len(raw_header)>MAX_HEADER:
                    raise ObserverError('headers_too_large')
                lines = raw_header.split(b'\r\n')
                status = re.fullmatch(br'HTTP/1\.[01] ([0-9]{3})(?: [^\r\n]*)?',lines[0])
                if status is None:
                    raise ObserverError('invalid_http_status')
                code = int(status.group(1))
                if 300<=code<400:
                    raise ObserverError('redirect_refused')
                if code!=200:
                    raise ObserverError('http_status_'+str(code))
                headers = {}
                for line in lines[1:]:
                    if b':' not in line:
                        raise ObserverError('invalid_header')
                    name,value = line.split(b':',1)
                    name = name.strip().lower()
                    if name in headers:
                        raise ObserverError('duplicate_header')
                    headers[name] = value.strip()
                length = headers.get(b'content-length',b'')
                if b'transfer-encoding' in headers or not re.fullmatch(br'[0-9]{1,7}',length):
                    raise ObserverError('unsupported_body_framing')
                expected = int(length)
                if expected>MAX_BODY:
                    raise ObserverError('response_too_large')
                if not headers.get(b'content-type',b'').lower().startswith(b'application/json'):
                    raise ObserverError('unexpected_content_type')
                if len(body)>expected:
                    raise ObserverError('body_length_mismatch')
                content = bytearray(body)
                while len(content)<expected:
                    connection.settimeout(remaining())
                    block = connection.recv(min(16384,expected-len(content)))
                    if not block:
                        raise ObserverError('incomplete_body')
                    content.extend(block)
                remaining()
                try:
                    result = json.loads(content.decode('utf-8'),object_pairs_hook=_unique_object)
                except (UnicodeError,ValueError,RecursionError):
                    raise ObserverError('invalid_json') from None
                if type(result) is not dict:
                    raise ObserverError('invalid_response_shape')
                return result
        except (TimeoutError,socket.timeout):
            raise ObserverError('http_timeout') from None
        except OSError:
            raise ObserverError('http_unavailable') from None


def boolean(value):
    return value if type(value) is bool else None


def number(value):
    return value if type(value) is int and 0<=value<=10**12 else None


def timestamp(value):
    if not isinstance(value,str) or len(value)>80:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
        return parsed.astimezone(timezone.utc).isoformat().replace('+00:00','Z') if parsed.tzinfo else None
    except ValueError:
        return None


def enum(value, allowed):
    return value if isinstance(value,str) and value in allowed else 'unrecognized'


def project(path, payload, source_ids):
    """Explicit selections only; never return arbitrary server text or AI fields."""
    if type(payload) is not dict:
        raise ObserverError('invalid_response_shape')
    if path=='/api/readiness':
        service=payload.get('service') if type(payload.get('service')) is dict else {}
        return {'ok':boolean(payload.get('ok')),'ready':boolean(payload.get('ready')),
                'schema_expected':payload.get('schema_version')=='host_readiness_v1',
                'service_expected':service.get('id')=='ai_collaboration_studio'}
    if path=='/api/version':
        backend=payload.get('backend_build') if type(payload.get('backend_build')) is dict else {}
        value=backend.get('source_sha256')
        return {'ok':boolean(payload.get('ok')),'schema_expected':payload.get('schema_version')=='host_version_v2',
                'backend_available':boolean(backend.get('available')),
                'backend_source_sha256':value.lower() if isinstance(value,str) and re.fullmatch(r'[a-fA-F0-9]{64}',value) else None}
    overview=payload.get('overview')
    if type(overview) is not dict or type(overview.get('sources')) is not list or len(overview['sources'])>50:
        raise ObserverError('invalid_response_shape')
    worker=overview.get('worker_health') if type(overview.get('worker_health')) is dict else {}
    continuity=overview.get('continuity') if type(overview.get('continuity')) is dict else {}
    gap=continuity.get('last_gap') if type(continuity.get('last_gap')) is dict else {}
    counts=overview.get('checks_24h') if type(overview.get('checks_24h')) is dict else {}
    sources=[]
    for source in overview['sources']:
        if type(source) is not dict or source.get('id') not in source_ids:
            continue
        sources.append({'id':source['id'],'status':enum(source.get('status'),SOURCE_STATES),
            'enabled':boolean(source.get('enabled')),'stale':boolean(source.get('stale')),'overdue':boolean(source.get('overdue')),
            'consecutive_failures':number(source.get('consecutive_failures')),
            'last_attempt_at':timestamp(source.get('last_attempt_at')),'last_success_at':timestamp(source.get('last_success_at')),
            'next_poll_at':timestamp(source.get('next_poll_at'))})
    if len({source['id'] for source in sources})!=len(sources):
        raise ObserverError('duplicate_source_status')
    checks=[]
    for check in overview.get('recent_checks',[])[:10] if type(overview.get('recent_checks')) is list else []:
        if type(check) is not dict or check.get('source_id') not in source_ids:
            continue
        checks.append({'source_id':check['source_id'],'outcome':enum(check.get('outcome'),OUTCOMES),
            'started_at':timestamp(check.get('started_at')),'finished_at':timestamp(check.get('finished_at')),
            'http_status':number(check.get('http_status')),'new_items':number(check.get('new_items')),'updated_items':number(check.get('updated_items'))})
    return {'ok':boolean(payload.get('ok')),'enabled':boolean(overview.get('enabled')),'running':boolean(overview.get('running')),
        'worker':{'status':enum(worker.get('status'),WORKER_STATES),'alive':boolean(worker.get('alive')),
                  'heartbeat_fresh':boolean(worker.get('heartbeat_fresh')),'heartbeat_at':timestamp(worker.get('heartbeat_at')),
                  'consecutive_failures':number(worker.get('consecutive_failures')),'next_recovery_at':timestamp(worker.get('next_recovery_at'))},
        'continuity':{'session_started_at':timestamp(continuity.get('session_started_at')),
                      'last_heartbeat_at':timestamp(continuity.get('last_heartbeat_at')),
                      'previous_session_unclean':boolean(continuity.get('previous_session_unclean')),
                      'unverified_gap_count_24h':number(continuity.get('unverified_gap_count_24h')),
                      'last_gap':{'kind':enum(gap.get('kind'),GAP_KINDS),'started_at':timestamp(gap.get('started_at')),'ended_at':timestamp(gap.get('ended_at'))} if gap else None},
        'checks_24h':{key:number(counts.get(key)) for key in ('started','completed','successful','failed','interrupted','not_modified','new_items','updated_items')},
        'sources':sources,'missing_source_ids':sorted(set(source_ids)-{source['id'] for source in sources}),
        'additional_source_count':len(overview['sources'])-len(sources),'recent_checks':checks}


class WindowsProcess:
    """Retain one exact process handle for the whole observation; never rebind."""
    def __init__(self,pid,creation_ticks):
        if os.name!='nt':
            raise ObserverError('windows_required')
        from ctypes import wintypes
        self.pid,self.creation_ticks=pid,creation_ticks
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        self.kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        self.kernel.OpenProcess.restype=wintypes.HANDLE
        self.kernel.GetProcessId.argtypes=[wintypes.HANDLE]
        self.kernel.GetProcessId.restype=wintypes.DWORD
        self.kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
        self.kernel.GetProcessTimes.restype=wintypes.BOOL
        self.kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype=wintypes.DWORD
        self.kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        self.kernel.GetExitCodeProcess.restype=wintypes.BOOL
        self.kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        self.kernel.CloseHandle.restype=wintypes.BOOL
        self.handle=self.kernel.OpenProcess(0x100000|0x1000,False,pid)
        if not self.handle:
            raise ObserverError('process_handle_unavailable')
        first=self.sample()
        if first['state']!='alive':
            self.close()
            raise ObserverError('target_not_confirmed_alive')

    def sample(self):
        from ctypes import wintypes
        wait=self.kernel.WaitForSingleObject(self.handle,0)
        if wait==0:
            code=wintypes.DWORD()
            known=bool(self.kernel.GetExitCodeProcess(self.handle,ctypes.byref(code)))
            return {'state':'exited','bound_handle_signaled':True,'exit_code':int(code.value) if known else None}
        values=[wintypes.FILETIME() for _ in range(4)]
        if not self.kernel.GetProcessTimes(self.handle,*(ctypes.byref(value) for value in values)):
            return {'state':'observation_error','error_code':'process_times_unavailable'}
        ticks=(values[0].dwHighDateTime<<32)|values[0].dwLowDateTime
        if self.kernel.GetProcessId(self.handle)!=self.pid or ticks!=self.creation_ticks:
            return {'state':'identity_changed','identity_match':False}
        if wait==258:
            return {'state':'alive','identity_match':True}
        return {'state':'observation_error','error_code':'process_wait_unavailable'}

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle=None


def own_identity():
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetCurrentProcess.restype=wintypes.HANDLE
    kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
    kernel.GetProcessTimes.restype=wintypes.BOOL
    values=[wintypes.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(kernel.GetCurrentProcess(),*(ctypes.byref(value) for value in values)):
        raise ObserverError('observer_identity_unavailable')
    return {'pid':os.getpid(),'creation_filetime_ticks':(values[0].dwHighDateTime<<32)|values[0].dwLowDateTime}


class Observer:
    def __init__(self,log,process,transport,clock,binding,*,simulation=False,interval=INTERVAL,target=TARGET,maximum=MAXIMUM):
        if not simulation and (interval,target,maximum)!=(INTERVAL,TARGET,MAXIMUM):
            raise ObserverError('real_observation_duration_is_fixed')
        if not (0<interval and 0<target<=maximum):
            raise ObserverError('invalid_observation_bounds')
        self.log,self.process,self.transport,self.clock=log,process,transport,clock
        self.binding,self.simulation=binding,simulation
        self.interval,self.target,self.maximum=interval,target,maximum
        self.stats={'samples':0,'sample_gaps':0,'maximum_sample_gap_seconds':0,'process_observation_errors':0,
                    'backend_identity_confirmed_samples':0,'backend_identity_unconfirmed_samples':0,
                    'readiness_unhealthy_samples':0,'monitor_disabled_or_unknown_samples':0,'worker_unhealthy_or_unknown_samples':0,
                    'http_failures':{path:0 for path in PATHS},'source_summaries':{source:{'samples':0,'states':{},'stale_samples':0,'overdue_samples':0,'terminal_checks':{}} for source in binding['source_ids']}}
        self.seen_checks=set()
        self.last_process=None

    def stop_requested(self):
        path=self.log.output/'STOP'
        try:
            info=path.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or getattr(info,'st_file_attributes',0)&0x400:
            raise ObserverError('stop_file_unsafe')
        return True  # Its contents are deliberately never opened.

    def probe(self):
        try:
            value=self.process.sample()
        except Exception:
            value={'state':'observation_error','error_code':'process_probe_failed'}
        if value.get('state') not in {'alive','exited','identity_changed','observation_error'}:
            value={'state':'observation_error','error_code':'process_probe_invalid'}
        if value['state']=='observation_error':
            self.stats['process_observation_errors']+=1
        if value!=self.last_process:
            self.log.append('process_state',{'observed_utc':self.clock.utcnow(),'elapsed_seconds':self.clock.monotonic()-self.started,'process':value})
        self.last_process=value
        return value

    def take_sample(self):
        sample={'observed_utc':self.clock.utcnow(),'elapsed_seconds':self.clock.monotonic()-self.started,'process':self.last_process,'endpoints':{}}
        for path in PATHS:
            try:
                payload=self.transport.get(path,deadline=self.started+self.maximum)
                value=project(path,payload,self.binding['source_ids'])
                sample['endpoints'][path]={'result':'ok','state':value}
            except ObserverError as error:
                code=error.code if re.fullmatch(r'[a-z0-9_]{1,64}',error.code) else 'transport_failed'
                sample['endpoints'][path]={'result':'failed','error_code':code}
            except Exception:
                sample['endpoints'][path]={'result':'failed','error_code':'transport_or_projection_failed'}
        self.log.append('sample',sample)
        self.stats['samples']+=1
        for path,item in sample['endpoints'].items():
            self.stats['http_failures'][path]+=item['result']=='failed'
        news=sample['endpoints']['/api/news/overview'].get('state')
        ready=sample['endpoints']['/api/readiness'].get('state')
        if not ready or not all(ready.get(key) is True for key in ('ok','ready','schema_expected','service_expected')):
            self.stats['readiness_unhealthy_samples']+=1
        if not news or news.get('enabled') is not True:
            self.stats['monitor_disabled_or_unknown_samples']+=1
        if not news or news['worker']['alive'] is not True or news['worker']['heartbeat_fresh'] is not True or news['worker']['status'] not in {'running','paused'}:
            self.stats['worker_unhealthy_or_unknown_samples']+=1
        if news:
            for source in news['sources']:
                summary=self.stats['source_summaries'][source['id']]
                summary['samples']+=1
                summary['states'][source['status']]=summary['states'].get(source['status'],0)+1
                summary['stale_samples']+=source['stale'] is True
                summary['overdue_samples']+=source['overdue'] is True
            for check in news['recent_checks']:
                key=(check['source_id'],check['started_at'],check['finished_at'],check['outcome'])
                if (check['outcome'] in OUTCOMES-{'started'} and check['started_at'] and check['finished_at']
                        and datetime.fromisoformat(self.started_utc)<=datetime.fromisoformat(check['started_at'])<=datetime.fromisoformat(check['finished_at'])<=datetime.fromisoformat(self.clock.utcnow()) and key not in self.seen_checks):
                    self.seen_checks.add(key)
                    counts=self.stats['source_summaries'][check['source_id']]['terminal_checks']
                    counts[check['outcome']]=counts.get(check['outcome'],0)+1
        version=sample['endpoints']['/api/version'].get('state')
        if version and version['ok'] is True and version['schema_expected'] is True and version['backend_available'] is True and version['backend_source_sha256']==self.binding['backend_source_sha256']:
            self.stats['backend_identity_confirmed_samples']+=1
        else:
            self.stats['backend_identity_unconfirmed_samples']+=1
        if version and version['backend_source_sha256'] is not None and version['backend_source_sha256']!=self.binding['backend_source_sha256']:
            return 'backend_source_identity_mismatch'
        return None

    def run(self):
        self.started=self.clock.monotonic()
        self.started_utc=self.clock.utcnow()
        initial={'observed_utc':self.started_utc,'started_monotonic_seconds':self.started,'observer_identity':{'pid':os.getpid(),'simulation':True} if self.simulation else own_identity(),'binding':self.binding,
            'interval_seconds':self.interval,'target_seconds':self.target,'maximum_seconds':self.maximum,'simulation':self.simulation}
        self.log.append('started',initial)
        with (self.log.output/'started.json').open('xb') as stream:
            stream.write(canonical({**initial,'initial_record_sha256':self.log.previous})+b'\n')
            stream.flush()
            os.fsync(stream.fileno())
        reason='observer_internal_error'
        last_sample=None
        next_sample=self.started
        try:
            while True:
                if STOP_SIGNAL is not None:
                    reason='observer_signal_requested'
                    break
                if self.stop_requested():
                    reason='observer_stop_requested'
                    break
                process=self.probe()
                if process['state'] in {'exited','identity_changed'}:
                    reason='bound_process_exited' if process['state']=='exited' else 'bound_process_identity_changed'
                    break
                now=self.clock.monotonic()
                if now-self.started>=self.maximum:
                    reason='observation_maximum_reached'
                    break
                if now>=next_sample or now-self.started>=self.target:
                    mismatch=self.take_sample()
                    if last_sample is not None:
                        gap=now-last_sample
                        self.stats['maximum_sample_gap_seconds']=max(self.stats['maximum_sample_gap_seconds'],gap)
                        if gap>self.interval+min(30,self.interval/5):
                            self.stats['sample_gaps']+=1
                            self.log.append('sample_gap',{'elapsed_seconds':now-self.started,'gap_seconds':gap,
                                'missed_nominal_intervals':max(0,int(gap//self.interval)-1),'meaning':'unobserved_interval_not_exact_downtime'})
                    last_sample=now
                    if mismatch:
                        reason=mismatch
                        break
                    process=self.probe()
                    if process['state'] in {'exited','identity_changed'}:
                        reason='bound_process_exited' if process['state']=='exited' else 'bound_process_identity_changed'
                        break
                    if self.clock.monotonic()-self.started>=self.maximum:
                        reason='observation_maximum_reached'
                        break
                    if self.clock.monotonic()-self.started>=self.target:
                        reason='observation_duration_reached'
                        break
                    next_sample=self.started+(math.floor((self.clock.monotonic()-self.started)/self.interval)+1)*self.interval
                self.clock.sleep(min(5,max(0,next_sample-self.clock.monotonic()),max(0,self.started+self.target-self.clock.monotonic())))
        except ObserverError as error:
            reason=error.code
        except KeyboardInterrupt:
            reason='observer_interrupted'
        except SystemExit:
            reason='observer_system_exit'
        except BaseException:
            reason='observer_internal_error'
        elapsed=max(0,self.clock.monotonic()-self.started)
        tail=max(0,self.clock.monotonic()-last_sample) if last_sample is not None else elapsed
        if tail>self.interval+min(30,self.interval/5):
            self.stats['sample_gaps']+=1
            self.stats['maximum_sample_gap_seconds']=max(self.stats['maximum_sample_gap_seconds'],tail)
            self.log.append('sample_gap',{'elapsed_seconds':elapsed,'gap_seconds':tail,
                'missed_nominal_intervals':max(0,int(tail//self.interval)-1),'meaning':'unobserved_tail_not_exact_downtime'})
        terminal={'observed_utc':self.clock.utcnow(),'reason':reason,'binding':self.binding,'simulation':self.simulation,
            'elapsed_monotonic_seconds':elapsed,'elapsed_lower_bound_seconds':math.floor(elapsed*1000)/1000,
            'unsampled_tail_seconds':tail,'maximum_duration_overrun_seconds':max(0,elapsed-self.maximum),
            'observed_for_at_least_24h':not self.simulation and elapsed>=TARGET,
            'continuous_24h_passed':False,'last_process_state':self.last_process,'statistics':self.stats,
            'limitations':['This samples local read-only APIs and one retained process handle; it does not fetch sources, call models, or inspect business databases.',
                'Sampling gaps are unobserved intervals, not exact downtime. Recent-check samples may miss checks between samples.',
                'A 24-hour monotonic duration is not a continuity pass, source-completeness proof, or the historical exact F06 trial.',
                'The JSONL hash chain detects record changes against its terminal anchor; it is not a digital signature.']}
        return self.log.terminal(terminal)


def verify(output):
    """Verify complete evidence; never repairs, appends, or infers successful exit."""
    output=Path(output)
    for path in (output,output/'observations.jsonl',output/'started.json',output/'terminal.json'):
        no_alias(path)
    def read_json(raw):
        return json.loads(raw,object_pairs_hook=_unique_object,parse_constant=lambda x: (_ for _ in ()).throw(ObserverError('non_finite_json')))
    raw=(output/'observations.jsonl').read_bytes()
    if not raw.endswith(b'\n') or len(raw)>100*1024*1024:
        raise ObserverError('log_incomplete_or_oversized')
    previous,sequence='0'*64,0
    rows=[]
    sample_elapsed=-1
    for line in raw.splitlines():
        if not line or len(line)>262144 or sequence>100000:
            raise ObserverError('log_bounds_invalid')
        value=read_json(line)
        digest=value.pop('record_sha256')
        if value['version']!='news_continuity_observation_v1' or value['sequence']!=sequence or value['previous_sha256']!=previous or sha(value)!=digest:
            raise ObserverError('log_chain_mismatch')
        if value['kind']=='sample':
            elapsed=value['data']['elapsed_seconds']
            if type(elapsed) not in (int,float) or not math.isfinite(elapsed) or elapsed<sample_elapsed:
                raise ObserverError('sample_monotonic_order_invalid')
            sample_elapsed=elapsed
        rows.append({**value,'record_sha256':digest})
        previous,sequence=digest,sequence+1
    started_raw=(output/'started.json').read_bytes()
    started=read_json(started_raw)
    if not rows or rows[0]['kind']!='started' or sum(r['kind']=='started' for r in rows)!=1:
        raise ObserverError('start_record_invalid')
    if started.pop('initial_record_sha256')!=rows[0]['record_sha256'] or started!=rows[0]['data']:
        raise ObserverError('started_binding_mismatch')
    if not started['simulation'] and (started['interval_seconds'],started['target_seconds'],started['maximum_seconds'])!=(INTERVAL,TARGET,MAXIMUM):
        raise ObserverError('real_duration_binding_invalid')
    receipt=read_json((output/'terminal.json').read_bytes())
    digest=receipt.pop('receipt_sha256')
    if receipt['version']!='news_continuity_terminal_v1' or sha(receipt)!=digest or receipt['final_record_sha256']!=previous or receipt['log_records']!=sequence:
        raise ObserverError('terminal_binding_mismatch')
    last=rows[-1]
    if last['kind']!='terminal' or sum(r['kind']=='terminal' for r in rows)!=1 or last['data']!=receipt['terminal']:
        raise ObserverError('terminal_data_mismatch')
    terminal=receipt['terminal']
    if terminal['binding']!=started['binding'] or terminal['simulation']!=started['simulation']:
        raise ObserverError('terminal_start_binding_mismatch')
    if terminal['statistics']['samples']!=sum(row['kind']=='sample' for row in rows):
        raise ObserverError('sample_statistics_mismatch')
    elapsed=terminal['elapsed_monotonic_seconds']
    if type(elapsed) not in (int,float) or not math.isfinite(elapsed) or elapsed<max(0,sample_elapsed):
        raise ObserverError('terminal_elapsed_invalid')
    if terminal['reason']=='observation_duration_reached' and elapsed<started['target_seconds']:
        raise ObserverError('premature_duration_terminal')
    if datetime.fromisoformat(terminal['observed_utc'])<datetime.fromisoformat(started['observed_utc']):
        raise ObserverError('terminal_utc_before_start')
    return {'verified':True,'records':sequence,'final_record_sha256':previous,
            'started_sha256':hashlib.sha256(started_raw).hexdigest()}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid',type=int)
    parser.add_argument('--creation-ticks',type=int)
    parser.add_argument('--backend-source-sha256')
    parser.add_argument('--source-id',action='append')
    parser.add_argument('--output-name',required=True)
    parser.add_argument('--verify',action='store_true')
    args=parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}',args.output_name):
        raise ObserverError('invalid_output_name')
    output=HERE/args.output_name
    if args.verify:
        print(json.dumps(verify(output)))
        return 0
    if not args.pid or not 1<=args.pid<=0xffffffff or not args.creation_ticks or not 1<=args.creation_ticks<=0xffffffffffffffff or not re.fullmatch(r'[a-f0-9]{64}',args.backend_source_sha256 or ''):
        raise ObserverError('exact_binding_required')
    sources=args.source_id or ['fed','bls','seagate']
    if len(sources)>20 or len(set(sources))!=len(sources) or not all(re.fullmatch(r'[a-z][a-z0-9_-]{0,31}',source) for source in sources):
        raise ObserverError('invalid_source_ids')
    binding={'pid':args.pid,'creation_filetime_ticks':args.creation_ticks,'backend_source_sha256':args.backend_source_sha256,'source_ids':sources,
             'origin':'http://127.0.0.1:8770','process_handle_rebind_allowed':False}
    for name in ('SIGINT','SIGTERM','SIGBREAK'):
        if hasattr(signal,name): signal.signal(getattr(signal,name),request_signal)
    log=ReceiptLog(output)
    process=None
    try:
        process=WindowsProcess(args.pid,args.creation_ticks)
        receipt=Observer(log,process,LoopbackTransport(),RealClock(),binding).run()
        print(json.dumps({'terminal_reason':receipt['terminal']['reason'],'elapsed_seconds':receipt['terminal']['elapsed_monotonic_seconds']}))
        return 0 if receipt['terminal']['reason']=='observation_duration_reached' else 2
    except BaseException as error:
        code=error.code if isinstance(error,ObserverError) and re.fullmatch(r'[a-z0-9_]{1,64}',error.code) else 'observer_closeout_or_setup_failed'
        # Independent failure evidence: never retries/overwrites a partial terminal.
        try:
            with (output/'observer-failure.json').open('xb') as stream:
                stream.write(canonical({'error_code':code,'observed_utc':RealClock.utcnow(),
                    'binding':binding,'normal_completion':False})+b'\n')
                stream.flush();os.fsync(stream.fileno())
        except BaseException:
            pass  # Independent supervisor still retains the native exit evidence.
        print(json.dumps({'error_code':code}))
        return 2
    finally:
        if process is not None:
            process.close()
        log.close()


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (ObserverError,OSError,ValueError) as error:
        print(json.dumps({'error_code':error.code if isinstance(error,ObserverError) else 'observer_setup_failed'}))
        raise SystemExit(2)
