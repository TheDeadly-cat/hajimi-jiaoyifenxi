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
from scripts.news_review_watchdog import check_waiting_session
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
        def active(plan,authority,directory,*,inspector):
            handoffs.append((plan,authority,directory,inspector))
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
        self.assertIsNone(pin['identity']['policy_sha256'])
        self.assertTrue(binding['observer_ready_before_activation'])
        self.assertFalse(binding['activation_created'])
        self.assertFalse(binding['approval_fabricated'])
        self.assertFalse(handoffs[0][3].waiting)
        self.assertEqual(handoffs[0][3].sequence,2)
        wire.assert_not_called()


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

    def bind(self, *, ready=True):
        identity={**self.identity,'policy_sha256':'c'*64}
        plan={'request':{'target':{'identity':identity}}}
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
            'two_native_terminal_checks':True,'original_report_exists':True,'original_report_sha256':'e'*64})
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
            'original_report_exists':True,'original_report_sha256':'e'*64})
        verdict=self.check(alive=False)
        self.assertNotEqual(verdict['monitoring_phase'],'terminal_review')
        self.assertIn('OBSERVER_LIVENESS_UNCONFIRMED',verdict['alerts'])
        self.assertIn('OBSERVER_SESSION_EVIDENCE_UNCONFIRMED',verdict['alerts'])


if __name__=='__main__':
    unittest.main()
