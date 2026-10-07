import copy
import hashlib
import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.news_review_monitor import MonitorReceiptWriter, publish_json_once
from backend.news_review_monitor_probe import ProbeError, canonical, native_ticks
from scripts import run_news_review_observer as core
from scripts import news_review_observer_wait as waiting
from scripts.news_review_watchdog import check_waiting_session, check as check_direct
from tests import test_news_review_observer as fixtures

write=fixtures.write


class WaitingObserverTests(unittest.TestCase):
    def setUp(self):
        fixture=fixtures.ObserverPreparationTests()
        fixture.addCleanup=self.addCleanup
        fixture.setUp()
        self.f,self.root=fixture,fixture.root
        names=('activation-policy.json','activation-receipt.json','host-fixture.json')
        self.active={name:core.read_json(self.root/name) for name in names}
        self.active['activation-receipt.json'].update(activation_monotonic_before_ns=10_100_000_000,
            activation_monotonic_after_ns=10_200_000_000,activation_monotonic_implementation='QueryPerformanceCounter()')
        for name in names: (self.root/name).unlink()
        proposal=self.active['activation-receipt.json']
        write(self.root/'activation-proposal.json',{'activation':proposal['activation'],'activation_sha256':proposal['activation_sha256']})
        manifest=core.read_json(self.root/'preparation-manifest.json')
        manifest['files'].append({'name':'activation-proposal.json','sha256':core.sha(self.root/'activation-proposal.json')})
        write(self.root/'preparation-manifest.json',manifest)
        approval=core.read_json(self.root/'user-approval.json')
        approval['preparation_manifest_sha256']=core.sha(self.root/'preparation-manifest.json')
        write(self.root/'user-approval.json',approval)
        self.request={'version':waiting.WAIT_REQUEST,'root':str(self.root),
            **{k:fixture.request['target']['identity'][k] for k in ('candidate_sha','activation_sha256')},
            'policy_id':fixture.request['target']['policy_id'],'source_standards':fixture.request['target']['source_standards']}
        self.wall=proposal['activated_at']-100
        self.mono=10000
        with patch.object(core,'verify_checkout'):
            self.wrapped=core.prepare(self.request,str(fixture.shell))
        self.directory=self.root/'monitoring'

    def activate(self):
        host=self.active['host-fixture.json']
        pin=self.f.request['host_pin']
        host.update(process_id=pin['pid'],process_start_utc_ticks=native_ticks(pin['start_utc']))
        for name,value in self.active.items(): write(self.root/name,value)

    def inspector_class(self):
        case=self
        class Inspector(core.NativeInspector):
            def __call__(self):
                self.sequence+=1
                self.pins=copy.deepcopy(case.f.request['pins'])
                tree=case.f.tree()
                tree.update(version='news_review_native_wait_inspection_v1',policy_sha256=None,
                    owner_pid=None,owner_identity_pinned=False,owner_alive=False,host_url=None,listeners=[],safe_status_reads_allowed=False)
                self.last=tree
                return tree
        return Inspector

    def session(self):
        self.directory.mkdir()
        return waiting.WaitingSession(self.wrapped['plan'],self.directory,core=core,
            wall_ms=lambda:self.wall,monotonic_ms=lambda:self.mono)

    def test_prepare_wait_leaves_policy_unresolved_and_creates_no_activation(self):
        self.assertEqual(self.wrapped['plan']['version'],waiting.WAIT_PLAN)
        self.assertEqual(self.wrapped['plan']['activation'],self.active['activation-receipt.json']['activation'])
        self.assertFalse(self.wrapped['plan']['request_authorized'])
        self.assertFalse(self.directory.exists())
        self.assertFalse((self.root/'activation-receipt.json').exists())
        self.assertFalse((self.root/'activation-policy.json').exists())
        self.activate()
        with patch.object(core,'verify_checkout'),self.assertRaisesRegex(ProbeError,'preactivation_observer_required'):
            core.prepare(self.request,str(self.f.shell))

    def test_waiting_process_checks_cannot_issue_http_or_resolve_a_policy(self):
        session=self.session()
        with patch.object(core,'NativeInspector',self.inspector_class()),patch('socket.socket') as wire:
            result=session.step()
        self.assertEqual(result,{'phase':'waiting_local_input','inspection_complete':True})
        self.assertIsNone(session.identity['policy_sha256'])
        self.assertNotIn('resolved_plan',result)
        self.assertEqual(len(session.inspector.pins),2)
        wire.assert_not_called()

    def test_partial_activation_publication_waits_but_cannot_last_forever(self):
        session=self.session()
        (self.root/'activation-policy.json').write_text('{',encoding='utf-8')
        with patch.object(core,'NativeInspector',self.inspector_class()):
            self.assertEqual(session.step()['phase'],'waiting_receipt_publication')
            self.mono+=45001
            with self.assertRaisesRegex(ProbeError,'receipt_publication_incomplete'): session.step()
        self.assertEqual((self.root/'activation-policy.json').read_text(),'{')
        self.assertFalse((self.root/'activation-receipt.json').exists())

    def test_actual_receipts_bind_original_window_and_keep_native_inspector(self):
        session=self.session()
        with patch.object(core,'NativeInspector',self.inspector_class()),patch.object(core,'verify_checkout'):
            session.step()
            original=session.inspector
            self.activate();self.wall+=30000;self.mono+=30000
            result=session.step()
            active=result['resolved_plan']
            self.assertIs(session.inspector,original)
            self.assertEqual(active['request']['target']['identity']['policy_sha256'],self.active['activation-receipt.json']['policy_sha256'])
            self.assertEqual(active['request']['target']['expires_at_ms'],self.active['activation-receipt.json']['expires_at'])
            original.bind_active(active)
            self.assertFalse(original.waiting)
            with self.assertRaisesRegex(ProbeError,'observer_activation_rebinding_forbidden'): original.bind_active(active)

    def test_changed_launch_receipt_cannot_rebind_waiting_session(self):
        session=self.session()
        with patch.object(core,'NativeInspector',self.inspector_class()):
            session.step()
            value=core.read_json(self.root/'launch-receipt.json')
            value['process_start_utc']='2026-09-27T00:00:00.1234568Z'
            write(self.root/'launch-receipt.json',value)
            with self.assertRaisesRegex(ProbeError,'waiting_receipt_changed'): session.step()

    def test_wall_clock_order_cannot_substitute_for_same_machine_monotonic_evidence(self):
        activation=self.active['activation-receipt.json']
        # A wall-clock rollback could make a later observer look earlier.
        self.assertEqual(waiting.readiness_order(10_300_000_000,'QueryPerformanceCounter()',activation),
                         'after_activation_observed')
        self.assertEqual(waiting.readiness_order(10_150_000_000,'QueryPerformanceCounter()',activation),'unconfirmed')
        self.assertEqual(waiting.readiness_order(10_100_000_000,'QueryPerformanceCounter()',activation),'unconfirmed')
        self.assertEqual(waiting.readiness_order(10_000_000_000,'QueryPerformanceCounter()',activation),'before_activation_verified')
        self.assertEqual(waiting.readiness_order(10_000_000_000,'unknown_clock',activation),'unconfirmed')

    def test_run_waiting_hands_off_in_same_process_without_second_directory(self):
        handoffs=[]
        def pause(seconds):
            self.wall+=int(seconds*1000);self.mono+=int(seconds*1000)
            self.activate()
            return False
        def active(plan,authority,directory,*,inspector,observer_identity):
            handoffs.append((plan,authority,directory,inspector,observer_identity))
            publish_json_once(directory/'observer-exit.json',{'phase':'synthetic_handoff_only'})
            return 0
        event=Mock();event.wait.side_effect=pause
        with patch.object(core,'verify_checkout'),patch.object(core,'native_creation_ticks',return_value=638000000000000000), \
                patch.object(core,'NativeInspector',self.inspector_class()),patch.object(core,'observe_active',side_effect=active), \
                patch.object(waiting.time,'time_ns',side_effect=lambda:self.wall*1000000), \
                patch.object(waiting.time,'monotonic_ns',side_effect=lambda:self.mono*1000000), \
                patch.object(waiting.threading,'Event',return_value=event),patch('socket.socket') as wire:
            self.assertEqual(waiting.run_waiting(self.wrapped['plan'],self.wrapped['plan_sha256'],core=core),0)
        self.assertEqual(len(handoffs),1)
        pin=core.read_json(self.directory/'observer-pin.json')
        active_pin=core.read_json(self.directory/'observer-active-pin.json')
        binding=core.read_json(self.directory/'observer-activation-binding.json')
        self.assertEqual(pin['pid'],active_pin['pid'])
        self.assertEqual(pin['process_start_utc_ticks'],active_pin['process_start_utc_ticks'])
        self.assertEqual(handoffs[0][4], (pin['pid'], pin['process_start_utc_ticks']))
        self.assertIsNone(pin['identity']['policy_sha256'])
        self.assertTrue(binding['observer_ready_before_activation'])
        self.assertFalse(binding['activation_created'])
        self.assertFalse(binding['approval_fabricated'])
        self.assertFalse(handoffs[0][3].waiting)
        self.assertEqual(handoffs[0][3].sequence,2)
        wire.assert_not_called()

    def _run_to_transition(self, active_side_effect):
        """Drive run_waiting to the active handoff with a fake observe_active."""
        def pause(seconds):
            self.wall += int(seconds*1000); self.mono += int(seconds*1000)
            self.activate()
            return False
        event = Mock(); event.wait.side_effect = pause
        with patch.object(core,'verify_checkout'), \
                patch.object(core,'native_creation_ticks',return_value=638000000000000000), \
                patch.object(core,'NativeInspector',self.inspector_class()), \
                patch.object(core,'observe_active',side_effect=active_side_effect), \
                patch.object(waiting.time,'time_ns',side_effect=lambda:self.wall*1000000), \
                patch.object(waiting.time,'monotonic_ns',side_effect=lambda:self.mono*1000000), \
                patch.object(waiting.threading,'Event',return_value=event), \
                patch('socket.socket'):
            return waiting.run_waiting(self.wrapped['plan'],self.wrapped['plan_sha256'],core=core)

    def test_T2w2_active_terminal_receipt_is_not_overwritten_by_waiting_layer(self):
        # Report 8.7: once transitioned, the waiting layer's finally must NOT
        # write a waiting-state primary receipt over the active layer's outcome.
        sentinel = {'version':'news_review_observer_exit_v1','phase':'ACTIVE_SENTINEL',
                    'identity':{'candidate_sha':'a'*40,'activation_sha256':'b'*64,'policy_sha256':'c'*64}}
        def active(plan,authority,directory,*,inspector,observer_identity):
            publish_json_once(directory/'observer-exit.json',sentinel)
            return 2
        result = self._run_to_transition(active)
        self.assertEqual(result, 2)  # active return code passes through
        on_disk = core.read_json(self.directory/'observer-exit.json')
        self.assertEqual(on_disk['phase'], 'ACTIVE_SENTINEL')  # not waiting_window_closed
        self.assertEqual(on_disk['identity']['policy_sha256'], 'c'*64)  # active identity preserved

    def test_T2w3_active_fallback_only_is_not_masked_as_normal_closeout(self):
        # Active layer wrote ONLY a fallback (primary failed) and returned 2.
        # The waiting layer must not create a waiting-state primary that would
        # make this look like a normal closeout.
        def active(plan,authority,directory,*,inspector,observer_identity):
            publish_json_once(directory/'observer-exit-fallback-123-638000000000000000.json',
                {'version':'news_review_observer_exit_fallback_v1','phase':'registered_tree_terminal',
                 'failure_codes':['exit_receipt_publish_failed:OSError']})
            return 2
        result = self._run_to_transition(active)
        self.assertEqual(result, 2)
        self.assertTrue((self.directory/'observer-exit-fallback-123-638000000000000000.json').exists())
        # No waiting-state primary receipt masking the active-layer failure.
        self.assertFalse((self.directory/'observer-exit.json').exists())

    def test_T2w4_active_both_failed_passes_through_nonzero(self):
        def active(plan,authority,directory,*,inspector,observer_identity):
            return 2  # wrote nothing: primary and fallback both failed
        result = self._run_to_transition(active)
        self.assertEqual(result, 2)
        self.assertFalse((self.directory/'observer-exit.json').exists())
        self.assertFalse(list(self.directory.glob('observer-exit-fallback-*.json')))

    def test_T2w1_unactivated_waiting_publish_failure_is_protected(self):
        # Pre-activation waiting that never hands off owns its receipt.  When the
        # primary publish fails, publish_exit_receipt must degrade to a fallback
        # without raising, and the waiting identity keeps policy_sha256 = None.
        real_publish = core.publish_json_once
        def fake_publish(path, value):
            if Path(path).name == 'observer-exit.json':
                raise OSError('SECRET waiting failure')
            return real_publish(path, value)
        with patch.object(core,'verify_checkout'), \
                patch.object(core,'native_creation_ticks',return_value=638000000000000000), \
                patch.object(core,'NativeInspector',self.inspector_class()), \
                patch.object(core,'publish_json_once',side_effect=fake_publish), \
                patch.object(waiting.WaitingSession,'step',side_effect=ProbeError('synthetic_wait_failure')), \
                patch.object(waiting.time,'time_ns',side_effect=lambda:self.wall*1000000), \
                patch.object(waiting.time,'monotonic_ns',side_effect=lambda:self.mono*1000000), \
                patch('socket.socket'):
            with self.assertRaises(ProbeError):
                waiting.run_waiting(self.wrapped['plan'],self.wrapped['plan_sha256'],core=core)
        fallbacks = list(self.directory.glob('observer-exit-fallback-*.json'))
        self.assertEqual(len(fallbacks), 1)
        text = fallbacks[0].read_text(encoding='utf-8')
        self.assertNotIn('SECRET waiting failure', text)  # no free-text leak
        payload = json.loads(text)
        self.assertIsNone(payload['identity']['policy_sha256'])  # waiting mode: unresolved, not a placeholder
        self.assertEqual(payload['version'], 'news_review_observer_wait_exit_fallback_v1')

    def test_T2w5_preexisting_same_name_fallback_is_not_overwritten(self):
        # Report 8.7 rule 4 + review R-04: when the fallback name this process
        # would use already exists, publication must be REFUSED (os.link never
        # overwrites) and the pre-existing bytes must survive untouched.
        #
        # Honest scope note: run_waiting discards publish_exit_receipt's return
        # value, so the 'fallback_present_content_unverified' status is NOT
        # observable from this layer -- it is asserted directly against
        # core.publish_exit_receipt in test_news_review_observer.py.  What IS
        # observable here is that nothing was overwritten, no second fallback
        # appeared, and the original failure still propagates unmasked.
        ticks = 638000000000000000
        # run_waiting creates the monitoring directory itself with mkdir() (no
        # exist_ok), so it cannot be pre-created here.  Instead the pre-existing
        # receipt is laid down at the moment the fallback path is about to be
        # written, which deterministically produces the same-name collision.
        expected = f'observer-exit-fallback-{os.getpid()}-{ticks}.json'
        preexisting = self.directory/expected
        seeded = []

        real_publish = core.publish_json_once
        def fake_publish(path, value):
            if Path(path).name == 'observer-exit.json':
                raise OSError('primary waiting failure')
            if Path(path).name == expected and not seeded:
                preexisting.write_text('PRE-EXISTING-FALLBACK-BYTES', encoding='utf-8')
                seeded.append(str(path))
            return real_publish(path, value)

        with patch.object(core,'verify_checkout'), \
                patch.object(core,'native_creation_ticks',return_value=ticks), \
                patch.object(core,'NativeInspector',self.inspector_class()), \
                patch.object(core,'publish_json_once',side_effect=fake_publish), \
                patch.object(waiting.WaitingSession,'step',side_effect=ProbeError('synthetic_wait_failure')), \
                patch.object(waiting.time,'time_ns',side_effect=lambda:self.wall*1000000), \
                patch.object(waiting.time,'monotonic_ns',side_effect=lambda:self.mono*1000000), \
                patch('socket.socket'):
            with self.assertRaises(ProbeError):
                waiting.run_waiting(self.wrapped['plan'],self.wrapped['plan_sha256'],core=core)

        # The collision path was actually reached.
        self.assertTrue(seeded)
        # Overwrite refused: the pre-existing receipt survived untouched.
        self.assertEqual(preexisting.read_text(encoding='utf-8'), 'PRE-EXISTING-FALLBACK-BYTES')
        # Exactly one fallback exists -- no second, renamed or unidentified one.
        self.assertEqual(len(list(self.directory.glob('observer-exit-fallback-*.json'))), 1)
        # The primary was not published either, so no waiting-state receipt
        # pretends the run closed out normally.
        self.assertFalse((self.directory/'observer-exit.json').exists())


    def test_T2w6_waiting_and_active_identity_differ_and_are_both_preserved(self):
        # Report 8.3.1: the waiting pin carries policy_sha256=None; the active pin
        # carries the resolved policy hash.  They must NOT be forced equal.
        captured = {}
        def active(plan,authority,directory,*,inspector,observer_identity):
            captured['active_identity'] = plan['request']['target']['identity']
            publish_json_once(directory/'observer-exit.json',{'phase':'ACTIVE'})
            return 0
        self._run_to_transition(active)
        pin = core.read_json(self.directory/'observer-pin.json')
        active_pin = core.read_json(self.directory/'observer-active-pin.json')
        self.assertIsNone(pin['identity']['policy_sha256'])
        self.assertIsNotNone(active_pin['identity']['policy_sha256'])
        self.assertEqual(active_pin['identity']['policy_sha256'], captured['active_identity']['policy_sha256'])
        # PID and ticks are identical across waiting and active pins (same process).
        self.assertEqual(pin['pid'], active_pin['pid'])
        self.assertEqual(pin['process_start_utc_ticks'], active_pin['process_start_utc_ticks'])


class WaitingWatchdogTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp=tempfile.TemporaryDirectory(prefix='waiting-watchdog-fixture-')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.now=1_000_000
        self.identity={'candidate_sha':'a'*40,'activation_sha256':'b'*64,'policy_sha256':None}
        self.pin={'version':'news_review_waiting_observer_pin_v1','identity':self.identity,'plan_sha256':'d'*64,
                  'pid':123,'process_start_utc_ticks':638000000000000000,'started_at_ms':self.now}
        self.wait_hash=waiting.write_wait(self.root,self.identity,1,'',started=self.now,completed=self.now,
            elapsed=1,phase='waiting_local_input',inspection_complete=True)

    def check(self, previous=None, *, alive=True):
        return check_waiting_session(self.root,self.pin,previous,now_ms=self.now,
            inspect=lambda _:self.pin['process_start_utc_ticks'] if alive else None)

    def bind(self, *, ready=True, expires_in_ms=1000):
        identity={**self.identity,'policy_sha256':'c'*64}
        plan={'request':{'target':{'identity':identity,'expires_at_ms':self.now+expires_in_ms}},'drain_grace_ms':255000}
        binding={'original_plan_sha256':self.pin['plan_sha256'],'resolved_plan_sha256':canonical(plan),
            'resolved_plan':plan,'observer_pid':123,'observer_process_start_utc_ticks':self.pin['process_start_utc_ticks'],
            'waiting_checks':1,'waiting_last_sha256':self.wait_hash,'observer_ready_before_activation':ready}
        binding['pre_activation_order']='before_activation_verified' if ready else 'after_activation_observed'
        binding.update(observer_ready_monotonic_ns=100 if ready else 400,
            observer_monotonic_implementation='QueryPerformanceCounter()',activation_monotonic_before_ns=200,
            activation_monotonic_after_ns=300,activation_monotonic_implementation='QueryPerformanceCounter()')
        publish_json_once(self.root/'observer-activation-binding.json',binding)
        active={**self.pin,'version':'news_review_active_observer_pin_v1','identity':identity,'binding_sha256':canonical(binding)}
        publish_json_once(self.root/'observer-active-pin.json',active)
        return identity

    def test_normal_wait_and_first_get_in_progress_are_quiet(self):
        first=self.check()
        self.assertFalse(first['changes']['notify'])
        self.bind()
        starting=self.check(first)
        self.assertEqual(starting['monitoring_phase'],'active_starting')
        self.assertFalse(starting['changes']['notify'])
        self.now+=360001
        late=self.check(starting)
        self.assertIn('MONITOR_SUCCESSFUL_READ_OVERDUE',late['alerts'])

    def test_activation_confirmation_is_once_and_requires_successful_actual_read(self):
        first=self.check()
        identity=self.bind()
        writer=MonitorReceiptWriter(self.root,identity,wall_ms=lambda:self.now,monotonic_ms=lambda:1)
        writer.execute(scheduled_at_ms=self.now,probe=lambda:{'identity':identity,'process_identity_verified':True,
            'port_ownership_verified':True,'health':{'host_alive':True,'workers_alive':True,'source_stale_keys':[]}})
        active=self.check(first)
        self.assertEqual(active['changes']['events'],['ACTIVATION_CONFIRMED'])
        self.assertTrue(active['actual_activation_confirmed'])
        self.assertFalse(self.check(active)['changes']['notify'])

    def test_missed_wait_checks_and_dead_observer_are_separate(self):
        first=self.check()
        self.now+=90001
        missed=self.check(first)
        self.assertEqual(missed['alerts'],['MONITOR_WAIT_EXECUTION_OVERDUE'])
        dead=self.check(missed,alive=False)
        self.assertIn('OBSERVER_LIVENESS_UNCONFIRMED',dead['alerts'])

    def test_changed_binding_and_removed_first_execution_are_not_startup_grace(self):
        self.bind()
        (self.root/'monitor-execution-000001.json').write_text('{',encoding='utf-8')
        corrupt=self.check()
        self.assertIn('OBSERVER_SESSION_EVIDENCE_UNCONFIRMED',corrupt['alerts'])
        (self.root/'monitor-execution-000001.json').unlink()
        missing=self.check(corrupt)
        self.assertNotEqual(missing['monitoring_phase'],'active_starting')
        self.assertIn('MONITOR_RECEIPT_IDENTITY_UNCONFIRMED',missing['alerts'])

    def test_normal_exit_routes_to_final_review_without_claiming_acceptance(self):
        self.bind()
        publish_json_once(self.root/'observer-exit.json',{'version':'news_review_observer_exit_v1','plan_sha256':self.pin['plan_sha256'],
            'identity':{**self.identity,'policy_sha256':'c'*64},'phase':'registered_tree_terminal',
            'ended_at_ms':self.now,'two_native_terminal_checks':True,'original_report_exists':True,'original_report_sha256':'e'*64})
        verdict=self.check(alive=False)
        self.assertEqual(verdict['monitoring_phase'],'terminal_review')
        self.assertEqual(verdict['changes']['events'],['FINAL_VERIFICATION_REQUIRED'])
        self.assertNotIn('OBSERVER_LIVENESS_UNCONFIRMED',verdict['alerts'])
        self.assertFalse(verdict['live_acceptance_proven'])

    def test_lost_activation_binding_cannot_return_to_waiting_or_forget_identity(self):
        self.bind()
        active=self.check()
        path=self.root/'observer-active-pin.json'
        original=path.read_bytes()
        path.unlink()
        missing=self.check(active)
        repeated=self.check(missing)
        for result in (missing,repeated):
            self.assertIn('OBSERVER_SESSION_EVIDENCE_UNCONFIRMED',result['alerts'])
            self.assertEqual(result.get('resolved_policy_sha256'),'c'*64)
        self.assertFalse(repeated['changes']['notify'])
        path.write_bytes(original)
        restored=self.check(repeated)
        self.assertEqual(restored['changes']['resolved'],['OBSERVER_SESSION_EVIDENCE_UNCONFIRMED'])

    def test_changed_active_pin_cannot_renew_first_read_grace(self):
        self.bind()
        active=self.check()
        path=self.root/'observer-active-pin.json'
        changed=core.read_json(path)
        self.now+=360001
        changed['started_at_ms']=self.now
        write(path,changed)
        verdict=self.check(active)
        self.assertIn('OBSERVER_SESSION_EVIDENCE_UNCONFIRMED',verdict['alerts'])

    def test_exit_for_other_policy_cannot_suppress_observer_liveness_failure(self):
        self.bind()
        publish_json_once(self.root/'observer-exit.json',{'version':'news_review_observer_exit_v1',
            'plan_sha256':self.pin['plan_sha256'],'identity':{**self.identity,'policy_sha256':'f'*64},
            'phase':'registered_tree_terminal','two_native_terminal_checks':True,
            'ended_at_ms':self.now,'original_report_exists':True,'original_report_sha256':'e'*64})
        verdict=self.check(alive=False)
        self.assertNotEqual(verdict['monitoring_phase'],'terminal_review')
        self.assertIn('OBSERVER_LIVENESS_UNCONFIRMED',verdict['alerts'])
        self.assertIn('OBSERVER_SESSION_EVIDENCE_UNCONFIRMED',verdict['alerts'])

    def drain_receipts(self, *, stale_source=False, last_offset=200000):
        identity=self.bind(expires_in_ms=250000)
        writer=MonitorReceiptWriter(self.root,identity,wall_ms=lambda:self.now,monotonic_ms=lambda:1)
        writer.execute(scheduled_at_ms=self.now,probe=lambda:{'identity':identity,'process_identity_verified':True,
            'port_ownership_verified':True,'health':{'host_alive':True,'workers_alive':True,
                'source_stale_keys':['company_ir'] if stale_source else []}})
        previous=self.check()
        expiry=self.now+250000
        publish_json_once(self.root/'observer-drain-start.json',{
            'version':'news_review_observer_drain_start_v2','identity':identity,'plan_sha256':self.pin['plan_sha256'],
            'started_at_ms':expiry,'started_monotonic_ms':250001,'original_expires_at_ms':expiry,
            'deadline_at_ms':expiry+255000,'deadline_monotonic_ms':505001,'http_get_requests':0,
            'schedule_end':{'version':'news_review_monitor_schedule_end_v1','started_at_ms':self.now,
                'started_monotonic_ms':1,'expires_at_ms':expiry,'deadline_monotonic_ms':250001,
                'ended_at_ms':expiry,'ended_monotonic_ms':250001,'stop_requested':False}})
        digest=''
        for index,offset in enumerate(range(0,last_offset+1,10000),1):
            self.now=expiry+offset
            receipt={'version':'news_review_observer_drain_execution_v1','identity':identity,
                'plan_sha256':self.pin['plan_sha256'],'sequence':index,'previous_sha256':digest,
                'completed_at_ms':self.now,'inspection_complete':True,'http_get_requests':0}
            digest=canonical(receipt)
            publish_json_once(self.root/f'observer-drain-execution-{index:06d}.json',{'receipt':receipt,'sha256':digest})
        return previous,expiry

    def test_fresh_native_drain_is_quiet_without_fabricated_health_recovery(self):
        previous,expiry=self.drain_receipts(stale_source=True)
        verdict=self.check(previous)
        self.assertEqual(verdict['monitoring_phase'],'draining')
        self.assertEqual(verdict['alerts'],['SOURCE_FULL_SUCCESS_STALE'])
        self.assertEqual(verdict['changes']['resolved'],[])
        self.assertFalse(verdict['changes']['notify'])
        self.assertFalse(any(verdict['health_recovery_confirmed'].values()))
        self.now=expiry+255000
        overdue=self.check(verdict)
        self.assertIn('OBSERVER_DRAIN_DEADLINE_UNCONFIRMED',overdue['alerts'])
        self.assertTrue(overdue['changes']['notify'])

    def test_missing_drain_evidence_cannot_become_normal_startup_grace(self):
        previous,_=self.drain_receipts()
        active=self.check(previous)
        self.assertFalse(active['changes']['notify'])
        (self.root/'observer-drain-start.json').unlink()
        self.assertIn('OBSERVER_SESSION_EVIDENCE_UNCONFIRMED',self.check(active)['alerts'])

    def test_elapsed_drain_before_wall_expiry_needs_original_exhausted_deadline(self):
        previous,expiry=self.drain_receipts()
        path=self.root/'observer-drain-start.json'
        valid=core.read_json(path)
        valid['started_at_ms']=expiry-1000
        valid['schedule_end']['ended_at_ms']=expiry-1000
        write(path,valid)
        verdict=self.check(previous)
        self.assertEqual(verdict['monitoring_phase'],'draining')
        self.assertFalse(verdict['changes']['notify'])
        mutations=[
            lambda r:r['schedule_end'].update(ended_monotonic_ms=250000),
            lambda r:r['schedule_end'].update(deadline_monotonic_ms=250002),
            lambda r:r['schedule_end'].update(stop_requested=True),
            lambda r:r.update(started_monotonic_ms=250000),
            lambda r:r.update(deadline_monotonic_ms=505002),
            lambda r:r.pop('schedule_end'),
            lambda r:r.update(version='news_review_observer_drain_start_v1')]
        for index,mutate in enumerate(mutations):
            with self.subTest(index=index):
                invalid=copy.deepcopy(valid);mutate(invalid);write(path,invalid)
                rejected=self.check(previous)
                self.assertIn('OBSERVER_SESSION_EVIDENCE_UNCONFIRMED',rejected['alerts'])
                self.assertNotEqual(rejected['monitoring_phase'],'draining')

    def test_late_exit_does_not_claim_timely_terminal_review(self):
        previous,expiry=self.drain_receipts()
        self.now=expiry+255001
        publish_json_once(self.root/'observer-exit.json',{'version':'news_review_observer_exit_v1',
            'plan_sha256':self.pin['plan_sha256'],'identity':{**self.identity,'policy_sha256':'c'*64},
            'phase':'registered_tree_terminal','two_native_terminal_checks':True,'ended_at_ms':self.now,
            'original_report_exists':True,'original_report_sha256':'e'*64})
        verdict=self.check(previous,alive=False)
        self.assertNotEqual(verdict['monitoring_phase'],'terminal_review')
        self.assertIn('OBSERVER_SESSION_EVIDENCE_UNCONFIRMED',verdict['alerts'])
        self.assertIn('OBSERVER_LIVENESS_UNCONFIRMED',verdict['alerts'])

    def test_missing_drain_execution_stays_visible(self):
        previous,expiry=self.drain_receipts()
        active=self.check(previous)
        self.now=expiry+200001
        for path in self.root.glob('observer-drain-execution-*.json'):
            path.unlink()  # Only this test's owned temporary receipts.
        missing=self.check(active)
        self.assertIn('OBSERVER_SESSION_EVIDENCE_UNCONFIRMED',missing['alerts'])

    def test_missed_native_drain_check_is_separate_from_observer_liveness(self):
        previous,expiry=self.drain_receipts(last_offset=0)
        self.now=expiry+90001
        verdict=self.check(previous)
        self.assertIn('MONITOR_DRAIN_EXECUTION_OVERDUE',verdict['alerts'])
        self.assertNotIn('OBSERVER_LIVENESS_UNCONFIRMED',verdict['alerts'])

    def test_normal_terminal_receipt_read_later_is_not_a_new_deadline_fault(self):
        previous,expiry=self.drain_receipts(stale_source=True)
        ended_at=self.now
        publish_json_once(self.root/'observer-exit.json',{'version':'news_review_observer_exit_v1',
            'plan_sha256':self.pin['plan_sha256'],'identity':{**self.identity,'policy_sha256':'c'*64},
            'phase':'registered_tree_terminal','two_native_terminal_checks':True,'ended_at_ms':ended_at,
            'original_report_exists':True,'original_report_sha256':'e'*64})
        self.now=expiry+360000
        verdict=self.check(previous,alive=False)
        self.assertEqual(verdict['monitoring_phase'],'terminal_review')
        self.assertNotIn('OBSERVER_DRAIN_DEADLINE_UNCONFIRMED',verdict['alerts'])
        self.assertNotIn('OBSERVER_LIVENESS_UNCONFIRMED',verdict['alerts'])
        self.assertIn('SOURCE_FULL_SUCCESS_STALE',verdict['alerts'])
        self.assertFalse(verdict['live_acceptance_proven'])

    def test_direct_observer_uses_same_bounded_drain_and_terminal_rules(self):
        _,expiry=self.drain_receipts()
        active=core.read_json(self.root/'observer-active-pin.json')
        binding=core.read_json(self.root/'observer-activation-binding.json')
        plan={**binding['resolved_plan'],'version':'news_review_observer_plan_v1'}
        plan_hash=canonical(plan)
        pin={**self.pin,'version':'news_review_direct_observer_pin_v1',
             'identity':active['identity'],'plan':plan,'plan_sha256':plan_hash}
        start=core.read_json(self.root/'observer-drain-start.json')
        start['plan_sha256']=plan_hash
        write(self.root/'observer-drain-start.json',start)
        previous_hash=''
        for path in sorted(self.root.glob('observer-drain-execution-*.json')):
            receipt=core.read_json(path)['receipt']
            receipt.update(plan_sha256=plan_hash,previous_sha256=previous_hash)
            previous_hash=canonical(receipt)
            write(path,{'receipt':receipt,'sha256':previous_hash})
        verdict=check_direct(self.root,pin,[],now_ms=self.now,
            inspect=lambda _:pin['process_start_utc_ticks'])
        self.assertEqual(verdict['monitoring_phase'],'draining')
        self.assertFalse(verdict['changes']['notify'])
        publish_json_once(self.root/'observer-exit.json',{'version':'news_review_observer_exit_v1',
            'plan_sha256':plan_hash,'identity':pin['identity'],'phase':'registered_tree_terminal',
            'two_native_terminal_checks':True,'ended_at_ms':self.now,
            'original_report_exists':True,'original_report_sha256':'e'*64})
        self.now=expiry+360000
        final=check_direct(self.root,pin,verdict['alerts'],previous=verdict,now_ms=self.now,inspect=lambda _:None)
        self.assertEqual(final['monitoring_phase'],'terminal_review')
        self.assertEqual(final['changes']['events'],['FINAL_VERIFICATION_REQUIRED'])
        self.assertFalse(final['live_acceptance_proven'])
        self.assertFalse(check_direct(self.root,pin,final['alerts'],previous=final,
                                     now_ms=self.now,inspect=lambda _:None)['changes']['notify'])


if __name__=='__main__':
    unittest.main()
