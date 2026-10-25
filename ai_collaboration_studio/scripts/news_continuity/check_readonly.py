"""One read-only heartbeat; only new monitoring receipts and its dedup state change."""
from pathlib import Path
from datetime import datetime, timezone
from ctypes import wintypes
import argparse, ctypes, hashlib, importlib.util, json, os, re, stat, time
from contextlib import contextmanager

HERE=Path(__file__).resolve().parent
OUT=HERE.parent/'heartbeat-observations'
SOURCE_IDS=['fed','bls','seagate']

def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def dt(value): return datetime.fromisoformat(value.replace('Z','+00:00'))
def notification_run_identity(binding):
    """A guardian replacement keeps the same original observation identity."""
    observer=binding['identities']['observer']
    value={'schema':'news_notification_run_v1','started_sha256':binding['started_sha256'],
           'observer_pid':observer['pid'],
           'observer_creation_filetime_ticks':str(observer['creation_filetime_ticks'])}
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def completion_identity(run_identity, terminal):
    value={'schema':'news_completion_identity_v1','run_identity':run_identity,'terminal':terminal}
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def notification_prior(prior,binding,run_identity):
    if prior.get('notification_run_identity')==run_identity:
        return prior
    # Unbound legacy booleans and another run's log anchors are not evidence
    # that this run's completion notification was generated. Keep the binding's
    # explicit alert baseline; retain old receipts/snapshots on disk untouched.
    result=dict(binding['notification_baseline'])
    for key in ('completion_reported','completion_notification','last_receipt','last_checked_utc',
                'last_log_sequence','last_log_record_sha256','finalized_lifecycle_hashes'):
        result.pop(key,None)
    result['notification_run_identity']=run_identity
    return result


@contextmanager
def notification_state_lock(out,g,timeout=30):
    """Serialize heartbeat state publication; the OS releases this on exit."""
    path=out/'notification-state.lock';g.no_alias(path)
    with path.open('a+b') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
            raise ValueError('notification_lock_file_invalid')
        if info.st_size==0:
            stream.write(b'\0');stream.flush();os.fsync(stream.fileno())
        if os.name=='nt':
            import msvcrt
            lock=lambda:msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            unlock=lambda:msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            lock=lambda:fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            unlock=lambda:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
        deadline=time.monotonic()+timeout
        while True:
            stream.seek(0)
            try:
                lock();break
            except OSError:
                if time.monotonic()>=deadline:
                    raise TimeoutError('notification_state_busy') from None
                time.sleep(0.05)
        try:
            yield
        finally:
            stream.seek(0);unlock()


def completion_generation(out,g,run_identity,completed_identity):
    path=out/('completion-'+completed_identity+'-generated.json')
    g.no_alias(path)
    if not path.exists():
        return None
    record=g.read_json(path)
    if (record.get('schema')!='news_completion_notification_generated_v1'
            or record.get('notification_run_identity')!=run_identity
            or record.get('completion_identity')!=completed_identity
            or record.get('generation_status')!='generated'
            or record.get('delivery_status')!='unknown'):
        raise ValueError('completion_generation_identity_invalid')
    name=record.get('receipt')
    if not isinstance(name,str) or not re.fullmatch(r'\d{8}T\d{12}Z-receipt\.json',name):
        raise ValueError('completion_generation_receipt_invalid')
    raw=g.safe_read(out/name,100*1024*1024)
    if hashlib.sha256(raw).hexdigest()!=record.get('receipt_sha256'):
        raise ValueError('completion_generation_receipt_changed')
    saved=json.loads(raw.decode('utf-8'),object_pairs_hook=g.object_pairs)
    if (saved.get('normal_completion') is not True
            or saved.get('notification_run_identity')!=run_identity
            or saved.get('completion_identity')!=completed_identity
            or saved.get('completion_notification_required') is not True
            or completion_identity(run_identity,saved.get('terminal'))!=completed_identity):
        raise ValueError('completion_generation_receipt_unbound')
    return record


def persist_heartbeat(out,g,tag,receipt,updated,run_identity,completed_identity,other_notification):
    """Called under the state lock. Generation is durable; delivery is unknown.

    A saved generation credential references the already-fsynced completion
    receipt. A later state-write failure cannot erase it and cause a duplicate.
    No notification result is returned before state replacement succeeds.
    """
    generated=completion_generation(out,g,run_identity,completed_identity) if completed_identity else None
    remembered=updated.get('completion_notification')
    if (completed_identity and generated is None and isinstance(remembered,dict)
            and remembered.get('completion_identity')==completed_identity):
        raise ValueError('completion_generation_missing')
    required=bool(completed_identity and generated is None)
    receipt={**receipt,'notification_run_identity':run_identity,'completion_identity':completed_identity,
             'completion_notification_required':required,
             'notification_required':bool(other_notification or required),
             'completion_delivery_status':'unknown' if completed_identity else None}
    name=tag+'-receipt.json'
    g.write_new(out/name,receipt)
    if required:
        generated={'schema':'news_completion_notification_generated_v1',
                   'notification_run_identity':run_identity,'completion_identity':completed_identity,
                   'receipt':name,'receipt_sha256':digest(out/name),
                   'generated_utc':receipt['checked_utc'],'generation_status':'generated','delivery_status':'unknown'}
        # Publish only a fully written/fsynced credential. A failed write may
        # leave a complete-looking temporary file, but it is never generation
        # evidence. On Windows rename also refuses replacing an existing name;
        # the state lock serializes the existence check on other platforms.
        temporary=out/(tag+'-completion.tmp')
        target=out/('completion-'+completed_identity+'-generated.json')
        g.write_new(temporary,generated)
        if target.exists():
            raise FileExistsError('completion_generation_already_exists')
        os.rename(temporary,target)
    updated={**updated,'notification_run_identity':run_identity}
    updated.pop('completion_reported',None)
    if generated is not None:
        updated['completion_notification']=generated
    g.write_new(out/(tag+'-notification-state.json'),updated)
    temp=out/(tag+'-state.tmp');g.write_new(temp,updated);os.replace(temp,out/'notification-state.json')
    return receipt


def native_identity(pid, expected):
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];k.OpenProcess.restype=wintypes.HANDLE
    k.GetProcessId.argtypes=[wintypes.HANDLE];k.GetProcessId.restype=wintypes.DWORD
    k.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4;k.GetProcessTimes.restype=wintypes.BOOL
    k.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];k.WaitForSingleObject.restype=wintypes.DWORD
    k.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)];k.GetExitCodeProcess.restype=wintypes.BOOL
    k.CloseHandle.argtypes=[wintypes.HANDLE]
    h=k.OpenProcess(0x100000|0x1000,False,pid)
    result={'pid':pid,'expected_creation_filetime':str(expected),'identity_match':False}
    if not h:return {**result,'state':'unavailable','winerror':ctypes.get_last_error()}
    try:
        times=[wintypes.FILETIME() for _ in range(4)]
        if not k.GetProcessTimes(h,*(ctypes.byref(x) for x in times)):return {**result,'state':'unconfirmed'}
        actual=(times[0].dwHighDateTime<<32)|times[0].dwLowDateTime
        matched=actual==int(expected) and k.GetProcessId(h)==pid;wait=k.WaitForSingleObject(h,0)
        result.update(actual_creation_filetime=str(actual),identity_match=matched,
            state='identity_changed' if not matched else 'alive' if wait==258 else 'exited' if wait==0 else 'unconfirmed')
        if matched and wait==0:
            code=wintypes.DWORD();result['exit_code']=int(code.value) if k.GetExitCodeProcess(h,ctypes.byref(code)) else None
            result['exit_filetime_ticks']=str((times[1].dwHighDateTime<<32)|times[1].dwLowDateTime)
        return result
    finally:k.CloseHandle(h)

def load_module(name,path,expected):
    if digest(path)!=expected:raise ValueError('helper_hash_changed')
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

def read_records(m,run):
    path=run/'observations.jsonl';m.no_alias(path)
    assert path.stat().st_size<=100*1024*1024,'log_bounds'
    raw=path.read_bytes();complete=raw[:raw.rfind(b'\n')+1];rows=[];previous='0'*64
    for index,line in enumerate(complete.splitlines()):
        assert len(line)<=262144 and index<100000,'log_bounds'
        row=json.loads(line,object_pairs_hook=m._unique_object);h=row.pop('record_sha256')
        assert row['version']=='news_continuity_observation_v1' and row['sequence']==index and row['previous_sha256']==previous and m.sha(row)==h,'log_chain_changed'
        rows.append({**row,'record_sha256':h});previous=h
    return raw,complete,rows

def cooldowns(payload):
    rows=[]
    for source in payload.get('overview',{}).get('sources',[]):
        if source.get('id') not in SOURCE_IDS:continue
        row={'id':source['id']}
        for key in ['cooldown_active','recovery_pending','refresh_queued','manual_hold']:
            row[key]=source.get(key) if type(source.get(key)) is bool else None
        row['cooldown_reason']=source.get('cooldown_reason') if source.get('cooldown_reason') in [None,'rate_limited','access_denied','http_error','source_unavailable'] else 'unknown'
        value=source.get('cooldown_until')
        row['cooldown_until']=value if isinstance(value,str) and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z',value) else None
        value=source.get('recovery_successes');row['recovery_successes']=value if type(value) is int and 0<=value<=100 else None
        rows.append(row)
    return rows

def runtime_health(endpoints,backend,start):
    result=set()
    for path in ('/api/readiness','/api/version','/api/news/overview'):
        if endpoints.get(path,{}).get('result')!='ok':result.add('endpoint_failure:'+path)
    ready=endpoints.get('/api/readiness',{}).get('state',{})
    if any(ready.get(k) is not True for k in ('ok','ready','schema_expected','service_expected')):result.add('host_not_ready')
    version=endpoints.get('/api/version',{}).get('state',{})
    if version.get('backend_source_sha256')!=backend or any(version.get(k) is not True for k in ('ok','schema_expected','backend_available')):result.add('backend_identity_mismatch')
    news=endpoints.get('/api/news/overview',{}).get('state',{});worker=news.get('worker',{})
    if any(news.get(k) is not True for k in ('ok','enabled','running')) or worker.get('alive') is not True or worker.get('heartbeat_fresh') is not True or worker.get('status')!='running':result.add('collector_unhealthy')
    for sid in news.get('missing_source_ids',[]):result.add('source_missing:'+sid)
    if news.get('additional_source_count'):result.add('additional_source_detected')
    gap=news.get('continuity',{}).get('last_gap') or {}
    if gap.get('started_at') and dt(gap['started_at'])>=start:result.add('new_runtime_gap')
    return result

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--trigger-utc',required=True);parser.add_argument('--binding-sha256',required=True);args=parser.parse_args()
    assert dt(args.trigger_utc).tzinfo is not None
    manifest=HERE/'observation-binding.json'
    assert digest(manifest)==args.binding_sha256,'binding_hash_changed'
    binding=json.loads(manifest.read_text(encoding='utf-8'))
    m=load_module('readonly_observer',HERE/'observer.py',binding['observer_sha256'])
    g=load_module('readonly_supervisor',HERE/'guardian.py',binding['supervisor_sha256'])
    g.no_alias(OUT);OUT.mkdir(exist_ok=True)
    with notification_state_lock(OUT,g):
        return check_bound(args,binding,m,g)


def check_bound(args,binding,m,g):
    # Full identities first, before any HTTP read.
    processes={role:native_identity(item['pid'],item['creation_filetime_ticks']) for role,item in binding['identities'].items()}
    assert re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}',binding['run_name'])
    run=Path(binding['run_directory']);life=HERE/'lifecycle'/binding['lifecycle_name']
    assert run.is_absolute() and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}',binding['lifecycle_name'])
    for path in (OUT,run,life,OUT/'notification-state.json'):m.no_alias(path)
    OUT.mkdir(exist_ok=True)
    prior=json.loads((OUT/'notification-state.json').read_text(encoding='utf-8')) if (OUT/'notification-state.json').is_file() else binding['notification_baseline']
    run_identity=notification_run_identity(binding)
    prior=notification_prior(prior,binding,run_identity)
    now=datetime.now(timezone.utc);tag=now.strftime('%Y%m%dT%H%M%S%fZ')
    issues=set();historical=set();terminal=None;guardian_end=None;proof=None;rows=[];samples=[];checks={};excluded=set();live={};ai={};cd=[];frozen={}
    fatal=False
    try:
        assert digest(run/'started.json')==binding['started_sha256'],'started_hash_changed'
        assert digest(Path(binding['observer_script']))==binding['observer_sha256'],'observer_source_changed'
        for name,expected in binding['startup_receipt_hashes'].items():assert digest(life/name)==expected,'startup_receipt_changed'
        started=g.read_json(run/'started.json');start=dt(started['observed_utc'])
        assert g.identity(started['observer_identity'])==g.identity(binding['identities']['observer']),'observer_binding_changed'
        assert started['binding']==binding['host_binding'],'host_binding_changed'
        raw,complete,rows=read_records(m,run)
        assert rows and rows[0]['kind']=='started' and rows[0]['record_sha256']==started['initial_record_sha256'],'start_anchor_changed'
        assert {k:v for k,v in started.items() if k!='initial_record_sha256'}==rows[0]['data'],'start_data_changed'
        oldseq=prior.get('last_log_sequence',-1)
        if oldseq>=0:assert len(rows)>oldseq and rows[oldseq]['record_sha256']==prior['last_log_record_sha256'],'previous_prefix_changed'
        if processes['observer']['state']!='alive':assert raw==complete,'exited_with_partial_log'
        # Final receipts are read only after their writer is no longer alive.
        # This avoids treating an in-progress fsync/publication as corruption.
        if (run/'terminal.json').is_file() and processes['observer']['state']!='alive':
            proof=m.verify(run);terminal=g.read_json(run/'terminal.json')
        for name,expected in prior.get('finalized_lifecycle_hashes',{}).items():
            assert name in ('exited.json','supervisor-terminal.json') and (life/name).is_file() and digest(life/name)==expected,'finalized_lifecycle_missing_or_changed'
        for name in ('exited.json','supervisor-terminal.json'):
            if (life/name).is_file() and processes['guardian']['state']!='alive':
                frozen[name]=digest(life/name)
                if name in prior.get('finalized_lifecycle_hashes',{}):assert frozen[name]==prior['finalized_lifecycle_hashes'][name],'lifecycle_receipt_changed'
        if (life/'exited.json').is_file() and processes['guardian']['state']!='alive':
            exit_record=g.read_json(life/'exited.json')
            assert g.identity(exit_record['guardian_identity'])==g.identity(binding['identities']['guardian']),'exit_guardian_mismatch'
            assert g.identity(exit_record['observer_identity'])==g.identity(binding['identities']['observer']),'exit_observer_mismatch'
            proof=g.closeout(run,binding['identities']['observer'],exit_record['process'],m.verify,binding['host_binding'])
        if (life/'supervisor-terminal.json').is_file() and processes['guardian']['state']!='alive':
            guardian_end=g.read_json(life/'supervisor-terminal.json')
            assert guardian_end['config_sha256']==binding['config_sha256'],'guardian_config_changed'
            assert g.identity(guardian_end['guardian_identity'])==g.identity(binding['identities']['guardian']),'guardian_terminal_identity_changed'
            assert guardian_end['supervision_gap']==binding['supervision_gap'] and guardian_end.get('continuous_supervision') is False,'supervision_gap_binding_changed'
            if guardian_end.get('normal_completion') is not True:issues.add('supervisor_abnormal_closeout')
            else:assert proof and proof.get('normal_completion') is True,'guardian_completion_unverified'
        if terminal is None and proof and proof.get('normal_completion') is True:
            terminal=g.read_json(run/'terminal.json')
        if terminal:
            if rows[-1]['record_sha256']!=terminal['final_record_sha256']:
                raw,complete,rows=read_records(m,run)
            assert raw==complete and len(rows)==terminal['log_records'] and rows[-1]['record_sha256']==terminal['final_record_sha256'],'final_snapshot_not_aligned'
            assert rows[0]['record_sha256']==started['initial_record_sha256'],'final_start_anchor_changed'
            if oldseq>=0:assert rows[oldseq]['record_sha256']==prior['last_log_record_sha256'],'final_previous_prefix_changed'
        complete_ok=bool(proof and proof.get('normal_completion') is True and guardian_end and guardian_end.get('normal_completion') is True)
        pending_closeout=bool(terminal and terminal['terminal'].get('reason')=='observation_duration_reached' and 0<=(datetime.now(timezone.utc)-dt(terminal['terminal']['observed_utc'])).total_seconds()<30)
        if not complete_ok and not pending_closeout:
            for role in ('observer','guardian'):
                if processes[role]['state']!='alive' or not processes[role]['identity_match']:issues.add(role+'_identity_or_liveness_failure')
            if terminal and terminal['terminal'].get('reason')=='observation_duration_reached':issues.add('verified_exit_closeout_missing')
        if terminal and terminal['terminal'].get('reason')!='observation_duration_reached':issues.add('observer_abnormal_terminal')
        samples=[r for r in rows if r['kind']=='sample'];assert samples,'no_saved_sample'
        upper=dt(terminal['terminal']['observed_utc']) if terminal else dt(samples[-1]['data']['observed_utc'])
        for row in samples:
            if row['sequence']>oldseq:historical.update(runtime_health(row['data']['endpoints'],binding['host_binding']['backend_source_sha256'],start))
            for check in row['data']['endpoints'].get('/api/news/overview',{}).get('state',{}).get('recent_checks',[]):
                if check['outcome']=='started':continue
                key=tuple(check.get(k) for k in ['source_id','started_at','finished_at','outcome'])
                if not (check['started_at'] and check['finished_at'] and start<=dt(check['started_at'])<=dt(check['finished_at'])<=upper):excluded.add(key);continue
                assert key not in checks or checks[key]==check,'conflicting_check'
                checks[key]=check
        if not terminal and (now-dt(samples[-1]['data']['observed_utc'])).total_seconds()>330:issues.add('observer_samples_stale')
        if not complete_ok and (now-start).total_seconds()>86700:issues.add('verified_completion_missing_after_deadline')
        if any(r['kind']=='sample_gap' and r['sequence']>oldseq for r in rows):historical.add('new_sample_gap')
    except Exception as error:
        fatal=True;issues.add('evidence_integrity_or_binding_failure');proof={'verified':False,'error_category':type(error).__name__}
        complete_ok=False
    if processes['host']['state']!='alive' or not processes['host']['identity_match']:issues.add('host_identity_or_liveness_failure')
    if not fatal and processes['host']['state']=='alive' and processes['host']['identity_match']:
        clock=m.RealClock();transport=m.LoopbackTransport(clock)
        for path in m.PATHS:
            try:
                payload=transport.get(path,deadline=clock.monotonic()+5)
                live[path]={'result':'ok','state':m.project(path,payload,SOURCE_IDS)}
                if path=='/api/news/overview':
                    cd=cooldowns(payload);a=payload.get('overview',{}).get('ai',{})
                    ai={k:a.get(k) if type(a.get(k)) is bool else None for k in ('configured','busy')}
                    ai['calls_reserved']=a.get('calls_reserved') if type(a.get('calls_reserved')) is int and 0<=a['calls_reserved']<=10000 else None
            except Exception:live[path]={'result':'failed','error_code':'fixed_get_or_projection_failed'};issues.add('endpoint_failure:'+path)
        ready=live.get('/api/readiness',{}).get('state',{})
        if any(ready.get(k) is not True for k in ('ok','ready','schema_expected','service_expected')):issues.add('host_not_ready')
        version=live.get('/api/version',{}).get('state',{})
        if version.get('backend_source_sha256')!=binding['host_binding']['backend_source_sha256']:issues.add('backend_identity_mismatch')
        news=live.get('/api/news/overview',{}).get('state',{});worker=news.get('worker',{})
        if any(news.get(k) is not True for k in ('ok','enabled','running')) or worker.get('alive') is not True or worker.get('heartbeat_fresh') is not True or worker.get('status')!='running':issues.add('collector_unhealthy')
        if ai!={'configured':False,'busy':False,'calls_reserved':0}:issues.add('ai_state_changed_or_unconfirmed')
        issues.update(runtime_health(live,binding['host_binding']['backend_source_sha256'],dt(binding['started_utc'])))
    prior_active=set(prior.get('active_alerts',[]));visible={s['id']:s for s in live.get('/api/news/overview',{}).get('state',{}).get('sources',[])}
    for sid in SOURCE_IDS:
        source=visible.get(sid);code='source_unhealthy:'+sid
        if not source:issues.add('source_missing:'+sid)
        healthy=source and source['enabled'] is True and source['status']=='ok' and source['stale'] is False and source['overdue'] is False and source['last_success_at']
        processing=source and source['enabled'] is True and source['status']=='fetching' and source['stale'] is False and source['overdue'] is False
        if not healthy and not processing:issues.add(code)
        recovered_source=healthy and dt(source['last_success_at'])>=dt(binding['started_utc'])
        for oldcode in (code,'source_missing:'+sid):
            if oldcode in prior_active and not recovered_source:issues.add(oldcode)
    failures=[c for c in checks.values() if c['outcome'] not in ('success','not_modified')]
    known={sid:set(v) for sid,v in prior.get('known_failure_http_statuses',{}).items()};new_types=[];transient=[]
    for c in failures:
        if m.sha(c) in prior.get('seen_failed_window_checks',[]):continue
        if c['http_status'] not in known.get(c['source_id'],set()):new_types.append({'source_id':c['source_id'],'http_status':c['http_status']})
        known.setdefault(c['source_id'],set()).add(c['http_status'])
        if 'source_unhealthy:'+c['source_id'] not in prior_active:transient.append(c)
    recovery_allowed=lambda code:code in ('host_not_ready','collector_unhealthy','ai_state_changed_or_unconfirmed','additional_source_detected') or code.startswith(('endpoint_failure:','source_unhealthy:','source_missing:'))
    recovered=sorted(c for c in prior_active-issues if recovery_allowed(c)) if not fatal and all(live.get(p,{}).get('result')=='ok' for p in m.PATHS) else []
    new_alerts=sorted(issues-prior_active)
    historical_alerts=sorted(historical-prior_active)
    summary={sid:{'completed':0,'successful':0,'failed_or_unknown':0,'http_statuses':{}} for sid in SOURCE_IDS}
    for c in checks.values():
        item=summary[c['source_id']];item['completed']+=1;item['successful' if c['outcome'] in ('success','not_modified') else 'failed_or_unknown']+=1
        k=str(c['http_status']);item['http_statuses'][k]=item['http_statuses'].get(k,0)+1
    other_notification=bool(new_alerts or historical_alerts or recovered or new_types or transient)
    completed_identity=completion_identity(run_identity,terminal) if complete_ok else None
    receipt={'schema_version':'supervised_news_observation_heartbeat_v2','trigger_utc':args.trigger_utc,'checked_utc':now.isoformat(),
        'binding_sha256':args.binding_sha256,'processes':processes,'records':len(rows),'samples':len(samples),'chain_verified':not fatal,
        'latest_sample_utc':samples[-1]['data']['observed_utc'] if samples else None,'window_source_summary':summary,
        'window_checks':sorted(checks.values(),key=lambda c:(c['finished_at'],c['source_id'])),'excluded_old_or_post_window_checks':len(excluded),
        'live_endpoints':live,'live_cooldowns':cd,'ai':ai,'terminal':terminal,'completion_proof':proof,'normal_completion':complete_ok,
        'current_alerts':sorted(issues),'new_alerts':new_alerts,'historical_alerts':historical_alerts,'recovered_alerts':recovered,'new_failure_types':new_types,
        'new_transient_failures':transient,'product_mutated':False,'external_or_paid_requests':0,
        'live_checks_excluded_from_saved_window':True,'supervision_gap':binding['supervision_gap'],
        'continuous_supervision_passed':False}
    updated={**prior,'last_receipt':tag+'-receipt.json','last_checked_utc':now.isoformat(),'active_alerts':sorted(issues),
        'known_failure_http_statuses':{k:sorted(v,key=str) for k,v in known.items()},
        'seen_failed_window_checks':sorted(set(prior.get('seen_failed_window_checks',[])+[m.sha(c) for c in failures])),
        'finalized_lifecycle_hashes':{**prior.get('finalized_lifecycle_hashes',{}),**frozen}}
    if not fatal and rows:updated.update(last_log_sequence=rows[-1]['sequence'],last_log_record_sha256=rows[-1]['record_sha256'])
    receipt=persist_heartbeat(OUT,g,tag,receipt,updated,run_identity,completed_identity,other_notification)
    print(json.dumps({k:receipt[k] for k in ('checked_utc','records','samples','chain_verified','normal_completion','window_source_summary','current_alerts','new_alerts','historical_alerts','recovered_alerts','new_failure_types','notification_required')},indent=2))
    print('RECEIPT '+str(OUT/(tag+'-receipt.json')))
    return 2 if fatal else 0

if __name__=='__main__':raise SystemExit(main())
