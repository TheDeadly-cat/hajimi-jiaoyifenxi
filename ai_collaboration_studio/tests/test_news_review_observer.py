import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from backend.news_review_monitor_probe import ProbeError, canonical
from scripts import run_news_review_observer as observer
from scripts.news_review_watchdog import native_creation_ticks


def write(path, value):
    Path(path).write_text(json.dumps(value), encoding='utf-8')


def utc_ticks(ticks):
    seconds, fraction = divmod(ticks, 10_000_000)
    value = datetime(1,1,1,tzinfo=timezone.utc)+timedelta(seconds=seconds)
    return value.strftime('%Y-%m-%dT%H:%M:%S')+f'.{fraction:07d}Z'


class ObserverPreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='observer-preparation-fixture-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)/'NewsReviewTrialObserverFixture'
        self.root.mkdir()
        self.shell = Path(temporary.name)/'pwsh.exe'
        self.shell.write_bytes(b'fixture executable never run')
        begin = time.time_ns()//1_000_000
        template = {'policy_id':'synthetic-observer', 'candidate_sha':'a'*40,
            'database_path':str(self.root/'data'/'studio.sqlite3'), 'not_before_ms':begin-1000,
            'expires_at_ms':begin+3_600_000}
        proposal = {'policy_template':template, 'permitted_changes':['not_before_ms','expires_at_ms'],
                    'activations':1, 'duration_ms':60_000}
        policy = {**template, 'not_before_ms':begin, 'expires_at_ms':begin+60_000}
        identity = {'candidate_sha':'a'*40,'activation_sha256':canonical(proposal),'policy_sha256':canonical(policy)}
        launcher = {'pid':101, 'start_utc':'2026-09-27T00:00:00.1234567Z','parent_pid':None}
        host = {'pid':102, 'start_utc':'2026-09-27T00:00:01.1234567Z','parent_pid':101}
        self.request = {'version':'news_review_observer_request_v1','root':str(self.root),
            'target': {'identity':identity, 'policy_id':policy['policy_id'], 'activated_at_ms':begin,
                'expires_at_ms':begin+60_000, 'host_url':'http://127.0.0.1:55029',
                'source_standards':{key:{'first_success_ms':900000,'maximum_gap_ms':900000}
                                    for key in ('sec_filings','company_ir')}},
            'launcher_pin':launcher, 'host_pin':host, 'pins':[launcher,host], 'host_receipt_name':'host-fixture.json'}
        ci = {'candidate_sha':'a'*40,'evidence_reviewed':True,'workflow_runs':[
            {'id':i,'head_sha':'a'*40,'status':'completed','conclusion':'success'} for i in (1,2)]}
        write(self.root/'ci-verification.json',ci)
        manifest = {**identity,'files':[{'name':'ci-verification.json','sha256':observer.sha(self.root/'ci-verification.json')}]}
        write(self.root/'preparation-manifest.json',manifest)
        write(self.root/'user-approval.json',{**identity,'approved':True,
            'preparation_manifest_sha256':observer.sha(self.root/'preparation-manifest.json')})
        write(self.root/'launch-receipt.json',{**identity,'process_id':101,'process_start_utc':launcher['start_utc']})
        write(self.root/'launcher-start.json',{**identity,'launcher_pid':101,'launcher_process_start_utc':launcher['start_utc']})
        write(self.root/'activation-policy.json',policy)
        write(self.root/'activation-receipt.json',{'activation':proposal,'activation_sha256':identity['activation_sha256'],
            'policy_sha256':identity['policy_sha256'],'activated_at':begin,'expires_at':begin+60000})
        write(self.root/'host-fixture.json',{'url':self.request['target']['host_url'],
            'policy_sha256':identity['policy_sha256'],'expires_at_ms':begin+60000})

    def prepared(self):
        with patch.object(observer,'verify_checkout'):
            return observer.prepare(self.request,str(self.shell))

    def test_preparation_binds_source_runtime_receipts_and_starts_nothing(self):
        with patch.object(observer,'verify_checkout'), patch.object(observer,'run_observer') as run, \
                patch('socket.socket') as wire:
            wrapped = observer.prepare(self.request,str(self.shell))
        self.assertEqual(wrapped['plan_sha256'],canonical(wrapped['plan']))
        self.assertFalse(wrapped['plan']['request_authorized'])
        self.assertEqual(set(wrapped['plan']['monitor_files']),set(observer.FILES))
        self.assertEqual(wrapped['plan']['powershell']['sha256'],observer.sha(self.shell))
        self.assertEqual(len(wrapped['plan']['receipt_sha256']),8)
        self.assertFalse((self.root/'monitoring').exists())
        run.assert_not_called()
        wire.assert_not_called()

    def test_wrong_approval_changed_receipt_and_changed_runtime_block_before_output(self):
        wrapped = self.prepared()
        with self.assertRaisesRegex(ProbeError,'observer_approval_mismatch'):
            observer.verify_plan(wrapped,'0'*64)
        self.shell.write_bytes(b'changed fixture runtime')
        with patch.object(observer,'verify_checkout'),self.assertRaisesRegex(ProbeError,'observer_preparation_changed'):
            observer.verify_plan(wrapped,wrapped['plan_sha256'])
        self.assertFalse((self.root/'monitoring').exists())

    def test_resealed_target_cannot_change_original_window(self):
        self.request['target']['expires_at_ms'] += 1
        with self.assertRaisesRegex(ProbeError,'activation_window_mismatch'):
            self.prepared()

    def test_parent_pin_precision_and_unknown_ancestry_rejected(self):
        self.request['launcher_pin']['start_utc']='2026-09-27T00:00:00.1234568Z'
        with self.assertRaisesRegex(ProbeError,'launcher_receipt_mismatch'):
            self.prepared()
        self.request['launcher_pin']['start_utc']='2026-09-27T00:00:00.1234567Z'
        self.request['host_pin']['parent_pid']=999
        with self.assertRaisesRegex(ProbeError,'process_ancestry_unconfirmed'):
            self.prepared()

    def test_manifest_change_is_not_silently_accepted(self):
        path=self.root/'ci-verification.json'
        value=observer.read_json(path)
        value['workflow_runs'][1]['conclusion']='failure'
        write(path,value)
        with self.assertRaisesRegex(ProbeError,'prepared_file_changed'):
            self.prepared()

    def test_existing_monitor_directory_cannot_be_resumed(self):
        wrapped=self.prepared()
        directory=self.root/'monitoring'
        directory.mkdir()
        write(directory/'observer-pin.json',{'original':'evidence'})
        with patch.object(observer,'verify_plan',return_value=wrapped['plan']), \
                patch.object(observer,'NativeInspector') as inspect,self.assertRaises(FileExistsError):
            observer.run_observer(wrapped['plan'],wrapped['plan_sha256'])
        inspect.assert_not_called()
        self.assertEqual(observer.read_json(directory/'observer-pin.json'),{'original':'evidence'})

    def test_run_api_requires_approval_before_creating_directory(self):
        wrapped=self.prepared()
        with patch.object(observer,'NativeInspector') as inspect,self.assertRaisesRegex(ProbeError,'observer_approval_mismatch'):
            observer.run_observer(wrapped['plan'],'0'*64)
        inspect.assert_not_called()
        self.assertFalse((self.root/'monitoring').exists())

    def test_cli_prepare_preserves_existing_plan_and_wrong_run_hash_has_no_effect(self):
        request_path=self.root/'observer-request.json';output=self.root/'observer-plan.json'
        write(request_path,self.request)
        args=['--prepare','--request',str(request_path),'--powershell',str(self.shell),'--output',str(output)]
        with patch.object(observer,'verify_checkout'),patch('builtins.print'):
            self.assertEqual(observer.main(args),0)
            original=output.read_bytes()
            with self.assertRaises(FileExistsError): observer.main(args)
            self.assertEqual(output.read_bytes(),original)
        with self.assertRaisesRegex(ProbeError,'observer_approval_mismatch'):
            observer.main(['--run','--plan',str(output),'--approve-plan-sha256','0'*64])
        self.assertFalse((self.root/'monitoring').exists())

    def test_expired_observation_plan_cannot_start(self):
        wrapped=self.prepared()
        now=self.request['target']['expires_at_ms']+255000
        with patch.object(observer,'verify_checkout'),patch.object(observer.time,'time_ns',return_value=now*1_000_000), \
                patch.object(observer.os,'name','nt'),self.assertRaisesRegex(ProbeError,'observer_window_closed'):
            observer.verify_plan(wrapped,wrapped['plan_sha256'])
        self.assertFalse((self.root/'monitoring').exists())

    def tree(self):
        return {'version':'news_review_native_inspection_v1','checked_at_utc':'2026-09-27T01:00:00.1234567Z',
            **self.request['target']['identity'],'launcher_pid':101,'owner_pid':102,
            'pins':copy.deepcopy(self.request['pins']),
            'processes':[{**p,'alive':True,'pid_reused':False} for p in self.request['pins']],
            'enumeration_consistent':True,'known_identity_inspection_incomplete':False,
            'owner_identity_pinned':True,'owner_alive':True,'host_url':self.request['target']['host_url'],
            'listeners':[{'LocalAddress':'127.0.0.1','LocalPort':55029,'OwningProcess':102}],
            'safe_status_reads_allowed':True,'ownership_lock_verified':False,'terminal_status_inferred':False,
            'database_opened':False,'http_requests':0}

    def inspector(self, effect):
        wrapped=self.prepared()
        directory=self.root/'inspections'
        directory.mkdir()
        run=Mock(side_effect=effect)
        return observer.NativeInspector(wrapped['plan'],directory,run=run),run,directory

    def test_native_inspections_preserve_descendants_and_never_store_raw_stderr(self):
        first=self.tree()
        child={'pid':103,'parent_pid':102,'start_utc':'2026-09-27T00:00:02.1234567Z'}
        first['pins'].append(child)
        first['processes'].append({**child,'alive':True,'pid_reused':False})
        replies=iter([first,self.tree()])
        def result(*a,**k):
            return subprocess.CompletedProcess(a[0],0,json.dumps(next(replies)).encode(),b'not persisted raw stderr')
        inspect,run,directory=self.inspector(result)
        self.assertEqual(len(inspect()['pins']),3)
        with self.assertRaisesRegex(ProbeError,'native_inspection_unconfirmed'):
            inspect()
        self.assertEqual(len(inspect.pins),3)
        self.assertEqual(observer.read_json(directory/'process-tree-input-000002.json')['pins'],first['pins'])
        self.assertNotIn('not persisted',''.join(path.read_text() for path in directory.glob('*.json')))
        self.assertEqual(run.call_args.kwargs['timeout'],45)

    def test_timeout_is_uncertain_and_does_not_infer_terminal(self):
        inspect,_,directory=self.inspector(subprocess.TimeoutExpired('fixture',45))
        with self.assertRaisesRegex(ProbeError,'native_inspection_timeout'):
            inspect()
        self.assertEqual(observer.read_json(directory/'process-tree-check-000001.json')['code'],'NATIVE_INSPECTION_TIMEOUT')
        self.assertFalse(observer.terminal_tree(inspect.last))

    def test_native_drain_timeout_uses_remaining_hard_budget(self):
        tree=self.tree()
        def result(*args,**kwargs):
            return subprocess.CompletedProcess(args[0],0,json.dumps(tree).encode(),b'')
        inspect,run,_=self.inspector(result)
        with patch.object(observer.time,'time_ns',return_value=1000000*1_000_000), \
                patch.object(observer.time,'monotonic_ns',return_value=100000*1_000_000):
            inspect(deadline_monotonic_ms=101500,expires_at_ms=1001200)
        self.assertEqual(run.call_args.kwargs['timeout'],1.2)

    def test_partial_process_list_or_extra_payload_is_not_persisted(self):
        tree=self.tree()
        tree['processes'].pop()
        def result(*a,**k):
            return subprocess.CompletedProcess(a[0],0,json.dumps(tree).encode(),b'')
        inspect,_,directory=self.inspector(result)
        with self.assertRaises(ProbeError): inspect()
        tree=self.tree()
        tree['raw_body']='must never persist'
        with self.assertRaises(ProbeError): inspect()
        self.assertNotIn('must never persist',''.join(path.read_text() for path in directory.glob('*.json')))

    def test_observer_must_not_belong_to_the_monitored_tree(self):
        tree=self.tree()
        pin={'pid':os.getpid(),'parent_pid':102,'start_utc':'2026-09-27T00:00:03.1234567Z'}
        tree['pins'].append(pin)
        tree['processes'].append({**pin,'alive':True,'pid_reused':False})
        def result(*a,**k):
            return subprocess.CompletedProcess(a[0],0,json.dumps(tree).encode(),b'')
        inspect,_,directory=self.inspector(result)
        with self.assertRaisesRegex(ProbeError,'observer_not_independent'): inspect()
        self.assertIn(pin,observer.read_json(directory/'process-tree-check-000001.json')['pins'])

    def test_two_terminal_checks_stop_observer_but_missing_report_is_a_fault(self):
        wrapped=self.prepared()
        tree=self.tree()
        tree.update(owner_alive=False,safe_status_reads_allowed=False,listeners=[])
        for process in tree['processes']: process['alive']=False
        inspect=Mock(side_effect=[copy.deepcopy(tree),copy.deepcopy(tree)])
        with patch.object(observer,'verify_plan',return_value=wrapped['plan']), \
                patch.object(observer,'native_creation_ticks',return_value=638000000000000000), \
                patch.object(observer,'NativeInspector',return_value=inspect),patch('socket.socket') as wire:
            self.assertEqual(observer.run_observer(wrapped['plan'],wrapped['plan_sha256']),2)
        self.assertEqual(inspect.call_count,2)
        wire.assert_not_called()
        result=observer.read_json(self.root/'monitoring'/'observer-exit.json')
        self.assertEqual(result['phase'],'terminal_report_missing_user_action_required')
        self.assertTrue(result['two_native_terminal_checks'])
        self.assertFalse(result['final_acceptance_verified'])
        self.assertFalse((self.root/'window-report.json').exists())

    def test_terminal_requires_whole_registered_tree_and_no_listener(self):
        tree=self.tree()
        tree['processes'][0]['alive']=False
        self.assertFalse(observer.terminal_tree(tree))
        tree['processes'][1]['alive']=False
        self.assertFalse(observer.terminal_tree(tree))
        tree['listeners']=[]
        self.assertTrue(observer.terminal_tree(tree))
        tree['known_identity_inspection_incomplete']=True
        self.assertFalse(observer.terminal_tree(tree))

    def drain_fixture(self, exit_delay, *, rollback_ms=0):
        wrapped=self.prepared()
        self.drain_plan=wrapped
        expiry=self.request['target']['expires_at_ms']
        clock={'wall':expiry-10000,'mono':100000}
        checks=[];status_reads=[]
        case=self
        class Event:
            stopped=False
            corrected=False
            def is_set(self): return self.stopped
            def set(self): self.stopped=True
            def wait(self, seconds):
                if self.stopped: return True
                advance=int(round(seconds*1000))
                case.assertGreater(advance,0)
                clock['wall']+=advance;clock['mono']+=advance
                if not self.corrected:
                    clock['wall']-=rollback_ms
                    self.corrected=True
                if exit_delay is not None and clock['wall']>=expiry+exit_delay and not (case.root/'window-report.json').exists():
                    write(case.root/'window-report.json',{'synthetic_host_report':True})
                return False
        def native_check(**_kwargs):
            checks.append(clock['wall'])
            tree=self.tree()
            if exit_delay is not None and clock['wall']>=expiry+exit_delay:
                tree.update(owner_alive=False,safe_status_reads_allowed=False,listeners=[])
                for process in tree['processes']: process['alive']=False
            return tree
        def probe_factory(target, *, inspect_tree, wall_ms):
            def probe():
                inspect_tree()
                status_reads.append(wall_ms())
                self.assertLess(wall_ms(),expiry)
                return {'identity':target['identity'],'process_identity_verified':True,
                    'port_ownership_verified':True,'health':{'host_alive':True,'workers_alive':True,'source_stale_keys':[]}}
            return probe
        directory=self.root/'monitoring';directory.mkdir()
        with patch.object(observer.time,'time_ns',side_effect=lambda:clock['wall']*1_000_000), \
                patch.object(observer.time,'monotonic_ns',side_effect=lambda:clock['mono']*1_000_000), \
                patch.object(observer.threading,'Event',Event), \
                patch.object(observer,'NewsReviewStatusProbe',side_effect=probe_factory),patch('socket.socket') as wire:
            result=observer.observe_active(wrapped['plan'],wrapped['plan_sha256'],directory,inspector=native_check)
        self.assertEqual(status_reads,[expiry-10000])
        ended=observer.read_json(directory/'observer-exit.json')
        wire.assert_not_called()
        return result,ended,clock,checks,expiry

    def test_normal_drain_is_checked_before_255_seconds_without_extra_status_gets(self):
        result,ended,clock,checks,expiry=self.drain_fixture(120000)
        self.assertEqual(result,0)
        self.assertLess(clock['wall'],expiry+255000)
        self.assertGreaterEqual(sum(t>=expiry+120000 for t in checks),2)
        self.assertEqual(ended['phase'],'registered_tree_terminal')
        self.assertTrue(ended['two_native_terminal_checks'])

    def test_live_tree_at_drain_deadline_is_unconfirmed_and_never_extended(self):
        result,ended,clock,checks,expiry=self.drain_fixture(None)
        self.assertEqual(result,2)
        self.assertEqual(clock['wall'],expiry+255000)
        self.assertTrue(all(t<expiry+255000 for t in checks))
        self.assertEqual(ended['phase'],'drain_deadline_unconfirmed_user_action_required')
        self.assertFalse(ended['two_native_terminal_checks'])
        self.assertFalse(ended['original_report_exists'])

    def test_rollback_elapsed_drain_routes_normal_exit_to_final_review(self):
        from scripts.news_review_watchdog import check
        result,ended,clock,_,expiry=self.drain_fixture(120000,rollback_ms=1000)
        self.assertEqual(result,0)
        directory=self.root/'monitoring'
        start=observer.read_json(directory/'observer-drain-start.json')
        self.assertEqual(start['started_at_ms'],expiry-1000)
        self.assertEqual(start['schedule_end']['ended_monotonic_ms'],start['schedule_end']['deadline_monotonic_ms'])
        pin={'version':'news_review_direct_observer_pin_v1','identity':self.request['target']['identity'],
             **self.drain_plan,'pid':123,'process_start_utc_ticks':638000000000000000,
             'started_at_ms':expiry-10000}
        verdict=check(directory,pin,[],now_ms=clock['wall'],inspect=lambda _:None)
        self.assertEqual(verdict['monitoring_phase'],'terminal_review')
        self.assertTrue(verdict['final_verification_required'])
        self.assertEqual(verdict['alerts'],[])
        self.assertFalse(verdict['live_acceptance_proven'])


class PublishExitReceiptTests(unittest.TestCase):
    """X-03: the controlled primary/fallback exit publisher (report 8.3.1-8.3.4)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        # Captured BEFORE any patch: a helper that calls observer.publish_json_once
        # from inside its own side_effect would recurse into the mock and make both
        # publications fail, masking the behaviour under test.
        self._real_publish = observer.publish_json_once

    def test_primary_success(self):
        result = observer.publish_exit_receipt(self.dir, {'phase': 'registered_tree_terminal'},
            fallback_version='v', ticks_provider=lambda pid: 2)
        self.assertEqual(result, 'primary_published')
        self.assertTrue((self.dir/'observer-exit.json').exists())
        self.assertFalse(list(self.dir.glob('observer-exit-fallback-*.json')))

    def test_primary_collision_writes_fallback_without_overwriting(self):
        (self.dir/'observer-exit.json').write_text('PRE-EXISTING', encoding='utf-8')
        result = observer.publish_exit_receipt(self.dir, {'phase': 'registered_tree_terminal'},
            fallback_version='v', ticks_provider=lambda pid: 99)
        self.assertEqual(result, 'fallback_only_published')
        # The pre-existing primary receipt is never overwritten (os.link semantics).
        self.assertEqual((self.dir/'observer-exit.json').read_text(encoding='utf-8'), 'PRE-EXISTING')
        fallback = self.dir.glob('observer-exit-fallback-*-99.json')
        self.assertEqual(len(list(self.dir.glob('observer-exit-fallback-*.json'))), 1)
        payload = json.loads(next(iter(fallback)).read_text(encoding='utf-8'))
        self.assertFalse(payload['final_acceptance_verified'])
        self.assertFalse(payload['monitored_processes_stopped_by_observer'])
        self.assertFalse(payload['database_opened'])
        self.assertEqual(payload['fallback_identity_basis'], 'native_creation_ticks')
        self.assertIn('exit_receipt_publish_failed:FileExistsError', payload['failure_codes'])

    def test_fallback_collision_reports_path_exists_content_unverified(self):
        # R-04: a same-name fallback is "the path exists, content NOT verified".
        # It is no longer called positive evidence, and is never overwritten.
        (self.dir/'observer-exit.json').write_text('PRE', encoding='utf-8')
        pid = observer.os.getpid()
        existing = self.dir/f'observer-exit-fallback-{pid}-99.json'
        existing.write_text('EXISTING-FALLBACK-NOT-JSON', encoding='utf-8')
        ended = {'phase': 'x', 'failure_codes': ['report_path_unresolved:ProbeError']}
        result = observer.publish_exit_receipt(self.dir, ended,
            fallback_version='v', ticks_provider=lambda _pid: 99)
        self.assertEqual(result, 'fallback_present_content_unverified')
        # Overwrite refused: bytes unchanged.
        self.assertEqual(existing.read_text(encoding='utf-8'), 'EXISTING-FALLBACK-NOT-JSON')
        # R-03: pre-existing report code survives, and the collision is recorded
        # as content-unverified rather than as a success.
        self.assertIn('report_path_unresolved:ProbeError', ended['failure_codes'])
        self.assertIn('fallback_present_content_unverified', ended['failure_codes'])

    def test_both_fail_returns_both_failed_without_raising(self):
        ended = {'phase': 'x', 'failure_codes': ['report_hash_unavailable:OSError']}
        with patch.object(observer, 'publish_json_once', side_effect=OSError('boom')):
            result = observer.publish_exit_receipt(self.dir, ended,
                fallback_version='v', ticks_provider=lambda pid: 1)
        self.assertEqual(result, 'both_failed')
        # R-03: a primary+fallback double failure keeps BOTH codes in the
        # receipt payload instead of leaving them in process memory only.
        self.assertIn('report_hash_unavailable:OSError', ended['failure_codes'])
        self.assertTrue(any(c.startswith('exit_receipt_publish_failed') for c in ended['failure_codes']))
        self.assertTrue(any(c.startswith('exit_receipt_fallback_publish_failed') for c in ended['failure_codes']))
        # Nothing was written (both publications failed).
        self.assertFalse(list(self.dir.glob('observer-exit*.json')))

    def test_no_free_text_exception_message_leaks_into_fallback(self):
        real = observer.publish_json_once

        def fake_publish(path, value):
            if Path(path).name == 'observer-exit.json':
                raise OSError('SECRET free text')
            return real(path, value)  # let the fallback actually be written

        with patch.object(observer, 'publish_json_once', side_effect=fake_publish):
            observer.publish_exit_receipt(self.dir, {'phase': 'x'},
                fallback_version='v', ticks_provider=lambda pid: 6)
        text = next(iter(self.dir.glob('observer-exit-fallback-*-6.json'))).read_text(encoding='utf-8')
        self.assertNotIn('SECRET free text', text)
        self.assertIn('exit_receipt_publish_failed:OSError', json.loads(text)['failure_codes'])


    # ---- R-01: identity is queried INSIDE the protected path ----

    def test_R01_identity_query_failure_cannot_abort_publication(self):
        # The native query raises AFTER the primary failed (the query only runs on
        # the fallback path).  It must be recorded as a bounded code and must NOT
        # propagate out of the publisher -- propagating would abort the caller's
        # finally block and lose both receipts, which is what R-01 forbids.
        ended = {'phase': 'registered_tree_terminal', 'failure_codes': []}

        def failing_query(_pid):
            raise OSError('native identity query failed')

        with patch.object(observer, 'publish_json_once', side_effect=self._primary_fails):
            result = observer.publish_exit_receipt(self.dir, ended,
                fallback_version='v', ticks_provider=failing_query)
        # No exception escaped, and the fallback was still published.
        self.assertEqual(result, 'fallback_only_published')
        self.assertIn('identity_unavailable:OSError', ended['failure_codes'])
        self.assertIn('exit_receipt_publish_failed:OSError', ended['failure_codes'])
        self.assertTrue(list(self.dir.glob('observer-exit-fallback-unidentified-*.json')))

    def test_R01_identity_query_success_path_is_unaffected(self):
        # A successful primary publication never queries identity, so the normal
        # closeout path keeps its original behaviour and code.
        ended = {'phase': 'registered_tree_terminal', 'failure_codes': []}

        def exploding_query(_pid):
            raise AssertionError('identity must not be queried when primary succeeds')

        result = observer.publish_exit_receipt(self.dir, ended,
            fallback_version='v', ticks_provider=exploding_query)
        self.assertEqual(result, 'primary_published')
        self.assertTrue((self.dir/'observer-exit.json').exists())
        self.assertEqual(ended['failure_codes'], [])

    def test_R01_identity_unavailable_never_yields_a_none_filename(self):
        # The query returns None instead of an exact creation time.  The fallback
        # must use the unidentified name and must not claim a pin-level match.
        ended = {'phase': 'x', 'failure_codes': []}
        with patch.object(observer, 'publish_json_once', side_effect=self._primary_fails):
            result = observer.publish_exit_receipt(self.dir, ended,
                fallback_version='v', ticks_provider=lambda _pid: None)
        self.assertEqual(result, 'fallback_only_published')
        names = [x.name for x in self.dir.glob('observer-exit-fallback-*.json')]
        self.assertEqual(len(names), 1)
        self.assertNotIn('None', names[0])
        self.assertTrue(names[0].startswith('observer-exit-fallback-unidentified-'))
        payload = json.loads(next(iter(self.dir.glob('observer-exit-fallback-*.json'))).read_text(encoding='utf-8'))
        self.assertEqual(payload['fallback_identity_basis'], 'pid_only')

    def test_R01_exact_ticks_still_names_the_fallback_for_pin_matching(self):
        # An exact creation time yields the ticks-suffixed name and declares the
        # stronger identity basis.
        ended = {'phase': 'x', 'failure_codes': []}
        with patch.object(observer, 'publish_json_once', side_effect=self._primary_fails):
            result = observer.publish_exit_receipt(self.dir, ended,
                fallback_version='v', ticks_provider=lambda _pid: 638000000000000000)
        self.assertEqual(result, 'fallback_only_published')
        payload = json.loads(next(iter(self.dir.glob('observer-exit-fallback-*.json'))).read_text(encoding='utf-8'))
        self.assertEqual(payload['fallback_identity_basis'], 'native_creation_ticks')
        self.assertTrue(any(x.name.endswith('-638000000000000000.json')
                            for x in self.dir.glob('observer-exit-fallback-*.json')))

    # ---- R-02: the publication directory is re-validated before writing ----

    def test_R02_directory_rejected_writes_nothing(self):
        # If the directory no longer passes safe_path at write time (a junction
        # appeared after planning), nothing is written anywhere and the result
        # says so.  Writing anyway is what R-02 forbids.
        ended = {'phase': 'x', 'failure_codes': []}
        attempts = []

        def record(path, value):
            attempts.append(str(path))
            return observer.publish_json_once(path, value)

        with patch.object(observer, 'safe_path', side_effect=ProbeError('linked_path_rejected')), \
                patch.object(observer, 'publish_json_once', side_effect=record):
            result = observer.publish_exit_receipt(self.dir, ended,
                fallback_version='v', ticks_provider=lambda _pid: 1)
        self.assertEqual(result, 'receipt_directory_rejected:ProbeError')
        self.assertEqual(attempts, [])
        self.assertFalse(list(self.dir.glob('observer-exit*.json')))
        self.assertIn('receipt_directory_rejected:ProbeError', ended['failure_codes'])
        # A rejected directory is never reported as a successful publication.
        self.assertNotEqual(result, 'primary_published')

    def test_T2e_link_failure_leaves_no_pending_residue(self):
        # publish_json_once writes a '.pending' temporary then hard-links it; its
        # finally unlinks the temporary.  When os.link fails, the publisher must
        # degrade without leaving residue -- this asserts the cleanup of a file
        # the forbidden-to-modify publisher owns, from outside it.
        from backend import news_review_monitor as monitor_module
        ended = {'phase': 'x', 'failure_codes': []}

        def failing_link(_src, _dst):
            raise OSError('synthetic link failure')

        with patch.object(monitor_module.os, 'link', side_effect=failing_link):
            result = observer.publish_exit_receipt(self.dir, ended,
                fallback_version='v', ticks_provider=lambda _pid: 1)
        self.assertEqual(result, 'both_failed')
        # No .pending residue in the publication directory (hidden-name glob).
        residue = [x.name for x in self.dir.iterdir() if x.name.endswith('.pending')]
        self.assertEqual(residue, [])
        # Neither receipt exists, and both failure codes were recorded (R-03).
        self.assertFalse(list(self.dir.glob('observer-exit*.json')))
        self.assertTrue(any(c.startswith('exit_receipt_publish_failed') for c in ended['failure_codes']))
        self.assertTrue(any(c.startswith('exit_receipt_fallback_publish_failed') for c in ended['failure_codes']))

    def _primary_fails(self, path, value):
        if Path(path).name == 'observer-exit.json':
            raise OSError('primary publication failed')
        return self._real_publish(path, value)


class ObserverCloseoutReturnCodeTests(unittest.TestCase):
    """X-03 report 8.3.3: the return code reflects whether closeout publication
    was CONFIRMED, which is orthogonal to the native terminal fact.  A
    native-terminal run whose primary exit receipt was not published must NOT
    return 0 -- otherwise the 'closeout unconfirmed' signal is lost."""

    def setUp(self):
        self.f = ObserverPreparationTests()
        self.f.addCleanup = self.addCleanup
        self.f.setUp()

    def _run_active(self, *, report_exists, fail_primary, fail_fallback):
        wrapped = self.f.prepared()
        plan = wrapped['plan']
        directory = self.f.root/'monitoring'
        directory.mkdir(exist_ok=True)
        if report_exists:
            write(Path(plan['request']['root'])/'window-report.json', {'synthetic_host_report': True})
        tree = self.f.tree()
        tree.update(owner_alive=False, safe_status_reads_allowed=False, listeners=[])
        for process in tree['processes']:
            process['alive'] = False

        def inspector(**_kwargs):
            return copy.deepcopy(tree)

        def probe_factory(target, *, inspect_tree, wall_ms):
            def probe():
                inspect_tree()
                return {'identity': target['identity'], 'process_identity_verified': True,
                        'port_ownership_verified': True,
                        'health': {'host_alive': False, 'workers_alive': False, 'source_stale_keys': []}}
            return probe

        real = observer.publish_json_once

        def fake_publish(path, value):
            name = Path(path).name
            if name == 'observer-exit.json' and fail_primary:
                raise OSError('synthetic primary failure')
            if name.startswith('observer-exit-fallback') and fail_fallback:
                raise OSError('synthetic fallback failure')
            return real(path, value)

        with patch.object(observer, 'NewsReviewStatusProbe', side_effect=probe_factory), \
                patch.object(observer, 'native_creation_ticks', return_value=638000000000000000), \
                patch.object(observer, 'publish_json_once', side_effect=fake_publish), \
                patch('socket.socket') as wire:
            result = observer.observe_active(plan, wrapped['plan_sha256'], directory, inspector=inspector)
        wire.assert_not_called()
        return result, directory

    def test_primary_published_native_terminal_returns_zero(self):
        result, directory = self._run_active(report_exists=True, fail_primary=False, fail_fallback=False)
        self.assertEqual(result, 0)
        ended = observer.read_json(directory/'observer-exit.json')
        self.assertEqual(ended['phase'], 'registered_tree_terminal')
        self.assertTrue(ended['two_native_terminal_checks'])
        self.assertIs(ended['original_report_exists'], True)
        # exit_receipt_publication is a runtime return value, not a persisted
        # field of the primary receipt; assert the observable outcome instead:
        # the primary receipt exists and no fallback was produced.
        self.assertTrue((directory/'observer-exit.json').exists())
        self.assertFalse(list(directory.glob('observer-exit-fallback-*.json')))

    def test_T2k_both_fail_native_terminal_returns_two(self):
        # The exact path r2 wrongly claimed was impossible: native terminal true
        # AND report present, yet both receipts fail.  Must NOT return 0.
        result, directory = self._run_active(report_exists=True, fail_primary=True, fail_fallback=True)
        self.assertEqual(result, 2)
        self.assertFalse((directory/'observer-exit.json').exists())
        self.assertFalse(list(directory.glob('observer-exit-fallback-*.json')))

    def test_T2l_fallback_only_native_terminal_returns_two(self):
        result, directory = self._run_active(report_exists=True, fail_primary=True, fail_fallback=False)
        self.assertEqual(result, 2)
        self.assertFalse((directory/'observer-exit.json').exists())
        fallbacks = list(directory.glob('observer-exit-fallback-*.json'))
        self.assertEqual(len(fallbacks), 1)
        payload = observer.read_json(fallbacks[0])
        # Native terminal fact is preserved in the payload, not faked away.
        self.assertEqual(payload['phase'], 'registered_tree_terminal')
        self.assertFalse(payload['final_acceptance_verified'])
        self.assertFalse(payload['monitored_processes_stopped_by_observer'])
        self.assertFalse(payload['database_opened'])
        self.assertNotIn('synthetic primary failure', fallbacks[0].read_text(encoding='utf-8'))

    def test_report_state_unknown_is_not_confirmed_missing(self):
        # safe_path rejection -> original_report_exists stays None (unknown),
        # never False; phase must not become terminal_report_missing.
        wrapped = self.f.prepared()
        plan = wrapped['plan']
        directory = self.f.root/'monitoring'
        directory.mkdir(exist_ok=True)
        tree = self.f.tree()
        tree.update(owner_alive=False, safe_status_reads_allowed=False, listeners=[])
        for process in tree['processes']:
            process['alive'] = False

        def inspector(**_kwargs):
            return copy.deepcopy(tree)

        def probe_factory(target, *, inspect_tree, wall_ms):
            def probe():
                inspect_tree()
                return {'identity': target['identity'], 'process_identity_verified': True,
                        'port_ownership_verified': True,
                        'health': {'host_alive': False, 'workers_alive': False, 'source_stale_keys': []}}
            return probe

        # Only the REPORT path is rejected.  safe_path must stay valid for the
        # monitoring directory: R-02 re-validates it before publishing, and a
        # blanket rejection would (correctly) refuse to write the receipt at all,
        # which is a different scenario covered by test_R02_directory_rejected.
        real_safe_path = observer.safe_path

        def report_path_only(value):
            if 'window-report.json' in str(value):
                raise ProbeError('linked_path_rejected')
            return real_safe_path(value)

        with patch.object(observer, 'NewsReviewStatusProbe', side_effect=probe_factory), \
                patch.object(observer, 'native_creation_ticks', return_value=638000000000000000), \
                patch.object(observer, 'safe_path', side_effect=report_path_only), \
                patch('socket.socket'):
            result = observer.observe_active(plan, wrapped['plan_sha256'], directory, inspector=inspector)
        ended = observer.read_json(directory/'observer-exit.json')
        self.assertIsNone(ended['original_report_exists'])
        self.assertEqual(ended['phase'], 'terminal_report_state_unconfirmed_user_action_required')
        self.assertTrue(any(c.startswith('report_path_unresolved') for c in ended['failure_codes']))
        # Unknown report state cannot claim a clean closeout, so it returns 2
        # (phase is not registered_tree_terminal).  Returning 0 would assert a
        # confirmed clean closeout that the evidence does not support.
        self.assertEqual(result, 2)
        # The receipt itself was still published (the directory was not rejected),
        # and it carries no publication-failure code.  Note: the publication
        # STATUS is assigned to the in-memory receipt only after publishing
        # returns, so it is by construction absent from the on-disk receipt; the
        # return code and failure_codes are what make it observable.
        self.assertNotIn('exit_receipt_publication', ended)
        self.assertFalse([c for c in ended['failure_codes']
                          if c.startswith('exit_receipt_publish_failed')
                          or c.startswith('receipt_directory_rejected')])

    def test_T2b_report_existence_check_failure_stays_unknown(self):
        # report.is_file() raising must leave original_report_exists as None
        # (unknown), never False, and record report_existence_unconfirmed.
        wrapped = self.f.prepared()
        plan = wrapped['plan']
        directory = self.f.root/'monitoring'
        directory.mkdir(exist_ok=True)
        tree = self.f.tree()
        tree.update(owner_alive=False, safe_status_reads_allowed=False, listeners=[])
        for process in tree['processes']:
            process['alive'] = False

        def inspector(**_kwargs):
            return copy.deepcopy(tree)

        def probe_factory(target, *, inspect_tree, wall_ms):
            def probe():
                inspect_tree()
                return {'identity': target['identity'], 'process_identity_verified': True,
                        'port_ownership_verified': True,
                        'health': {'host_alive': False, 'workers_alive': False, 'source_stale_keys': []}}
            return probe

        real_is_file = Path.is_file

        def is_file_fails_for_report(self):
            if 'window-report.json' in str(self):
                raise OSError('synthetic is_file failure')
            return real_is_file(self)

        with patch.object(observer, 'NewsReviewStatusProbe', side_effect=probe_factory), \
                patch.object(observer, 'native_creation_ticks', return_value=638000000000000000), \
                patch.object(Path, 'is_file', new=is_file_fails_for_report), \
                patch('socket.socket'):
            result = observer.observe_active(plan, wrapped['plan_sha256'], directory, inspector=inspector)
        ended = observer.read_json(directory/'observer-exit.json')
        self.assertIsNone(ended['original_report_exists'])
        self.assertIsNone(ended['original_report_sha256'])
        self.assertEqual(ended['phase'], 'terminal_report_state_unconfirmed_user_action_required')
        self.assertTrue(any(c.startswith('report_existence_unconfirmed') for c in ended['failure_codes']))
        self.assertEqual(result, 2)

    def test_T2c_report_hash_failure_keeps_existence_true(self):
        # The report exists (is_file True) but sha() fails: existence must stay
        # True while the hash is None, and report_hash_unavailable is recorded.
        wrapped = self.f.prepared()
        plan = wrapped['plan']
        directory = self.f.root/'monitoring'
        directory.mkdir(exist_ok=True)
        write(Path(plan['request']['root'])/'window-report.json', {'synthetic_host_report': True})
        tree = self.f.tree()
        tree.update(owner_alive=False, safe_status_reads_allowed=False, listeners=[])
        for process in tree['processes']:
            process['alive'] = False

        def inspector(**_kwargs):
            return copy.deepcopy(tree)

        def probe_factory(target, *, inspect_tree, wall_ms):
            def probe():
                inspect_tree()
                return {'identity': target['identity'], 'process_identity_verified': True,
                        'port_ownership_verified': True,
                        'health': {'host_alive': False, 'workers_alive': False, 'source_stale_keys': []}}
            return probe

        with patch.object(observer, 'NewsReviewStatusProbe', side_effect=probe_factory), \
                patch.object(observer, 'native_creation_ticks', return_value=638000000000000000), \
                patch.object(observer, 'sha', side_effect=OSError('synthetic hash failure')), \
                patch('socket.socket'):
            result = observer.observe_active(plan, wrapped['plan_sha256'], directory, inspector=inspector)
        ended = observer.read_json(directory/'observer-exit.json')
        self.assertIs(ended['original_report_exists'], True)
        self.assertIsNone(ended['original_report_sha256'])
        self.assertTrue(any(c.startswith('report_hash_unavailable') for c in ended['failure_codes']))
        # Existence is confirmed True, so this is NOT the unconfirmed-state phase.
        self.assertEqual(ended['phase'], 'registered_tree_terminal')
        self.assertEqual(result, 0)




SERVER_CHILD = r'''
import json,os,sys,threading,time
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
root=Path(sys.argv[1])
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length','2')
        self.end_headers()
        self.wfile.write(b'{}')
server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
worker=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.05})
worker.start()
(root/'server-ready.json').write_text(json.dumps({'port':server.server_port,'pid':os.getpid(),'parent_pid':os.getppid()}))
while not (root/'stop-server').exists(): time.sleep(.02)
server.shutdown();server.server_close();worker.join(3)
(root/'server-exit.json').write_text(json.dumps({'worker_alive':worker.is_alive()}))
'''

LAUNCHER_CHILD = r'''
import json,subprocess,sys,time
from pathlib import Path
root=Path(sys.argv[1]);code=sys.argv[2]
child=subprocess.Popen([sys.executable,'-B','-c',code,str(root)],stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
deadline=time.monotonic()+10
while not (root/'server-ready.json').exists():
    if time.monotonic()>deadline: raise RuntimeError('fixture_start_timeout')
    time.sleep(.02)
# Windows venv launchers may be redirector processes. The actual server
# reports its own identity; the Popen PID remains an independently tracked pin.
print(json.dumps({'spawned_pid':child.pid,**json.loads((root/'server-ready.json').read_text())}),flush=True)
sys.stdin.readline()
'''


@unittest.skipUnless(os.name=='nt','Native Windows process/listener inspection')
class NativeObserverTests(unittest.TestCase):
    def test_native_parent_exit_retains_child_and_pid_reuse_refuses_reads(self):
        shell=os.environ.get('NEWS_MONITOR_TEST_POWERSHELL') or shutil.which('pwsh')
        if not shell: self.skipTest('PowerShell 7 not available')
        with tempfile.TemporaryDirectory(prefix='native-monitor-fixture-') as tmp:
            root=Path(tmp)
            launch=subprocess.Popen([sys.executable,'-B','-c',LAUNCHER_CHILD,str(root),SERVER_CHILD],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
            child_pid=None;registered={}
            try:
                hello=json.loads(launch.stdout.readline().decode())
                child_pid=hello['pid']
                launcher={'pid':launch.pid,'parent_pid':None,'start_utc':utc_ticks(native_creation_ticks(launch.pid))}
                host={'pid':child_pid,'parent_pid':hello['parent_pid'],'start_utc':utc_ticks(native_creation_ticks(child_pid))}
                registered.update(observer.pin_key(pin) for pin in (launcher,host))
                registered[hello['spawned_pid']]=native_creation_ticks(hello['spawned_pid'])
                identity={'candidate_sha':'a'*40,'activation_sha256':'b'*64,'policy_sha256':'c'*64}
                plan={'request':{'target':{'identity':identity,'host_url':f'http://127.0.0.1:{hello["port"]}'},
                    'launcher_pin':launcher,'host_pin':host,'pins':[launcher,host]},
                    'powershell':{'path':shell,'sha256':observer.sha(shell)},
                    'monitor_files':{name:observer.sha(observer.APP/name) for name in observer.FILES}}
                directory=root/'inspection';directory.mkdir()
                waiting=copy.deepcopy(plan)
                waiting['request']['target']['identity']['policy_sha256']=None
                waiting['request']['target']['host_url']=None
                waiting['request']['host_pin']=None
                waiting['request']['pins']=[launcher]
                inspect=observer.NativeInspector(waiting,directory)
                before_activation=inspect()
                registered.update(observer.pin_key(pin) for pin in inspect.pins)
                self.assertIsNone(before_activation['policy_sha256'])
                self.assertEqual(before_activation['listeners'],[])
                self.assertFalse(before_activation['safe_status_reads_allowed'])
                self.assertIn(child_pid,[p['pid'] for p in inspect.pins])
                self.assertIn(hello['spawned_pid'],[p['pid'] for p in inspect.pins])
                self.assertEqual(next(p for p in inspect.pins if p['pid']==child_pid)['parent_pid'],hello['parent_pid'])
                print(json.dumps({'native_fixture_identity':{'spawned_pid':hello['spawned_pid'],
                    'host_pid':child_pid,'host_parent_pid':hello['parent_pid'],
                    'redirector_observed':hello['spawned_pid']!=child_pid,'registered_pins':len(registered)}}))
                plan['request']['pins']=copy.deepcopy(inspect.pins)
                inspect.bind_active(plan)
                live=inspect()
                self.assertTrue(live['owner_alive'])
                self.assertEqual(live['listeners'],[{'LocalAddress':'127.0.0.1','LocalPort':hello['port'],'OwningProcess':child_pid}])
                self.assertEqual(live['safe_status_reads_allowed'],not live['known_identity_inspection_incomplete'])
                launch.stdin.write(b'exit\n');launch.stdin.flush();launch.wait(timeout=10)
                orphan=inspect()
                self.assertFalse(next(p for p in orphan['processes'] if p['pid']==launch.pid)['alive'])
                self.assertTrue(next(p for p in orphan['processes'] if p['pid']==child_pid)['alive'])
                self.assertFalse(observer.terminal_tree(orphan))
                changed=copy.deepcopy(plan)
                changed['request']['host_pin']['start_utc']=utc_ticks(native_creation_ticks(child_pid)+1)
                changed['request']['pins']=[changed['request']['host_pin'] if p['pid']==child_pid else p
                                           for p in changed['request']['pins']]
                wrong_dir=root/'wrong-pin';wrong_dir.mkdir()
                reused=observer.NativeInspector(changed,wrong_dir)()
                self.assertTrue(reused['known_identity_inspection_incomplete'])
                self.assertFalse(reused['safe_status_reads_allowed'])
                wrong_key=observer.pin_key(changed['request']['host_pin'])
                wrong_state=next(p for p in reused['processes'] if
                    observer.pin_key({k:p[k] for k in ('pid','start_utc','parent_pid')})==wrong_key)
                self.assertTrue(wrong_state['pid_reused'])
                self.assertFalse(wrong_state['alive'])
            finally:
                (root/'stop-server').touch()
                if launch.poll() is None:
                    launch.stdin.write(b'exit\n');launch.stdin.flush();launch.wait(timeout=10)
                launch.stdin.close();launch.stdout.close()
                deadline=time.monotonic()+10
                while any(native_creation_ticks(pid)==ticks for pid,ticks in registered.items()) and time.monotonic()<deadline:
                    time.sleep(.05)
                for pid,ticks in registered.items():
                    self.assertNotEqual(native_creation_ticks(pid),ticks)
                self.assertEqual(observer.read_json(root/'server-exit.json'),{'worker_alive':False})


if __name__=='__main__':
    unittest.main()
