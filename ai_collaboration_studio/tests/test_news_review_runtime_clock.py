"""Bound dual-clock execution through the native pipeline; no external traffic."""
import copy
import threading
import time
import unittest
from contextlib import closing
from unittest.mock import patch

from backend.decision_lineage import canonical_sha256
from backend.document_evidence import DocumentEvidenceService
from backend.execution_boundary import AuthorizedTextRequest
from backend.news_review_clock_policy_review import Reading
from backend.news_review_contracts import DUAL_CLOCK_POLICY, DUAL_POLICY_VERSION, NewsReviewError, validate_policy
from backend.news_review_controller import NewsReviewController
from backend.news_review_report import NewsReviewJournal, build_news_review_report
from backend.news_review_service import NewsReviewService
from backend.news_review_stop import NewsReviewStop
from backend.source_poll_control import SourcePollCancelled, ensure_source_poll_active, source_poll_timeout_seconds
from tests import test_news_review as fixtures
from tests.test_document_evidence import import_event


class RuntimeClockTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.NewsReviewTests()
        self.f.addCleanup = self.addCleanup
        self.f.setUp()
        self.mono = int(time.monotonic()*1000)
        self.pin = (12345, 638000000000000001)
        self.f.policy.update(version=DUAL_POLICY_VERSION, clock_policy=copy.deepcopy(DUAL_CLOCK_POLICY),
                             clock_activation=vars(self.sample()), resume_within_window=False)
        self.f.policy['expires_at_ms'] = self.f.now+10000
        self.f.service = self.new_service()

    def sample(self):
        return Reading(self.f.now, self.mono, self.mono, *self.pin,
                       self.mono*1000000, self.mono*1000000)

    def new_service(self):
        f = self.f
        return NewsReviewService(f.store, f.providers, instance_owner=f.owner, documents=f.documents,
                                 clock=lambda:f.now, monotonic_ms=lambda:self.mono, clock_sampler=self.sample)

    def advance(self, elapsed, correction=0):
        self.mono += elapsed
        self.f.now += elapsed+correction

    def assert_code(self, code, call):
        with self.assertRaises(NewsReviewError) as raised:
            call()
        self.assertEqual(raised.exception.code, code)

    def test_template_is_preview_only_and_clock_contract_is_closed(self):
        f = self.f
        template = {**f.policy, 'clock_activation':None}
        f.service.preview(template, allow_clock_template=True)
        self.assertIsNone(f.service._dual_clock)
        self.assert_code('clock_activation_required', lambda:f.service.approve(
            template, approved_policy_sha256=canonical_sha256(template)))
        self.assertEqual(f.count('news_review_policies'), 0)
        for field, value, code in [('clock_policy', {**DUAL_CLOCK_POLICY, 'max_step_ms':2001}, 'clock_policy_mismatch'),
                                   ('resume_within_window', True, 'dual_clock_resume_forbidden'),
                                   ('clock_activation', {**f.policy['clock_activation'], 'process_id':True}, 'clock_activation_invalid'),
                                   ('clock_activation', {**f.policy['clock_activation'], 'monotonic_before_ns':None}, 'clock_activation_invalid')]:
            with self.subTest(field=field, code=code):
                self.assert_code(code, lambda:validate_policy({**f.policy, field:value}))

    def test_small_corrections_may_accumulate_without_resetting_or_extending_deadline(self):
        f = self.f
        f.approve()
        binding = f.service._dual_clock
        initial = binding.reference.snapshot()['initial_elapsed_deadline_ms']
        self.advance(1000, 1245)
        f.service.check_clock()
        self.advance(1000, 757)
        f.service.check_clock()
        self.assertEqual(binding.deadline(), initial-2002)
        self.advance(1000, -500)
        f.service.check_clock()
        self.assertEqual(binding.deadline(), initial-2002)
        f.approve()
        self.assertIs(f.service._dual_clock, binding)
        self.assertEqual(binding.activation.wall_ms, f.policy['not_before_ms'])
        self.assertEqual(f.count('provider_execution_runs'), 1)

    def test_another_service_cannot_recover_reapprove_or_use_live_binding(self):
        f = self.f
        item, _ = f.prepare()
        before = f.service.snapshot(f.policy['policy_id'])
        other = self.new_service()
        self.assert_code('dual_clock_fresh_trial_required', lambda:other.approve(
            f.policy, approved_policy_sha256=canonical_sha256(f.policy)))
        self.assert_code('clock_service_binding_mismatch', lambda:other.queue(f.policy['policy_id'],item))
        self.assert_code('clock_service_binding_mismatch', lambda:other.run_one(f.policy['policy_id']))
        self.assertEqual(f.service.snapshot(f.policy['policy_id']), before)
        self.assertEqual(f.run_one().call_count, 1)

    def test_end_session_revokes_binding_and_resume_never_reanchors(self):
        f = self.f
        item, _ = f.prepare()
        binding = f.service._dual_clock
        self.assert_code('dual_clock_resume_forbidden', lambda:f.service.resume(
            f.policy['policy_id'], approved_policy_sha256=canonical_sha256(f.policy)))
        f.service.end_host_session()
        self.assertFalse(binding.installed)
        self.assert_code('clock_activation_required', lambda:f.service.queue(f.policy['policy_id'],item))
        self.assert_code('clock_activation_required', f.approve)
        self.assert_code('dual_clock_fresh_trial_required', lambda:self.new_service().approve(
            f.policy, approved_policy_sha256=canonical_sha256(f.policy)))

    def test_elapsed_expiry_blocks_all_new_admissions_and_keeps_reservations(self):
        f = self.f
        item, _ = f.prepare()
        before = f.service.snapshot(f.policy['policy_id'])
        self.advance(10000, -1000)
        self.assertLess(f.now, f.policy['expires_at_ms'])
        for action in (lambda:f.service.discover(f.policy['policy_id']),
                       lambda:f.service.request_document(f.policy['policy_id'],item),
                       lambda:f.service.queue(f.policy['policy_id'],item),
                       lambda:f.service.run_one(f.policy['policy_id'])):
            self.assert_code('policy_expired', action)
        f.service.close_expired_work()
        after = f.service.snapshot(f.policy['policy_id'])
        self.assertTrue(after['expired'])
        self.assertEqual(f.view(item)['observation_until'],0)
        self.assertEqual(after['job_counts'].get('CANCELLED'), 1)
        for field in ('documents_reserved','calls_reserved','tokens_reserved','cost_reserved_cny'):
            self.assertEqual(after[field], before[field])
        self.assertEqual(f.count('provider_call_attempts'), 0)

    def test_source_timeout_uses_remaining_window_and_cannot_reopen_after_expiry(self):
        f = self.f
        f.approve()
        controller = NewsReviewController(f.service, f.policy['policy_id'])
        self.advance(9400)
        with controller.source_authorization('sec_filings'):
            self.assertEqual(source_poll_timeout_seconds(12,deadline_monotonic_ms=0,
                cancel_event=None,monotonic_ms=lambda:self.mono), .6)
            self.assertEqual(source_poll_timeout_seconds(12,deadline_monotonic_ms=self.mono+200,
                cancel_event=None,monotonic_ms=lambda:self.mono), .2)
            self.advance(600,-100)
            with self.assertRaises(SourcePollCancelled) as raised:
                ensure_source_poll_active(deadline_monotonic_ms=0,cancel_event=None,monotonic_ms=lambda:self.mono)
            self.assertEqual(raised.exception.code,'NEWS_REVIEW_WINDOW_EXPIRED')
        self.advance(0,-100)
        self.assert_code('policy_expired',f.service.check_clock)

    def test_independent_document_runner_inherits_window_and_rejects_late_body(self):
        f = self.f
        f.approve()
        item = import_event(f.store)
        f.service.discover(f.policy['policy_id'])
        job = f.service.request_document(f.policy['policy_id'],item)
        original = f.fetcher.side_effect
        observed = []
        def fetch(source, **kwargs):
            observed.append(source_poll_timeout_seconds(12,deadline_monotonic_ms=0,
                cancel_event=None,monotonic_ms=lambda:self.mono))
            self.advance(10000,-1000)
            return original(source,**kwargs)
        f.fetcher.side_effect = fetch
        independent = DocumentEvidenceService(f.store,fetcher=f.fetcher,clock=lambda:f.now)
        independent.run(job,cancel_event=f.cancel,deadline_monotonic_ms=int(time.monotonic()*1000)+12000)
        self.assertEqual(observed,[10])
        self.assertEqual(f.fetcher.call_count,1)
        self.assertEqual(independent.view(item)['job']['error_code'],'NEWS_REVIEW_WINDOW_EXPIRED')
        self.assertEqual(f.count('source_document_versions'),0)
        self.assertEqual(f.service.snapshot(f.policy['policy_id'])['documents_reserved'],1)

    def test_final_model_admission_rechecks_elapsed_deadline_after_preflight(self):
        f = self.f
        item, _ = f.prepare()
        controller = NewsReviewController(f.service,f.policy['policy_id'])
        def request(*args,**kwargs):
            bound = AuthorizedTextRequest(*args,**kwargs)
            check = bound.before_send
            def preflight():
                check()
                self.advance(10000,-1000)
            bound.before_send = preflight
            return bound
        with patch('backend.news_review_service.AuthorizedTextRequest',side_effect=request), \
             patch('urllib.request.OpenerDirector.open') as wire:
            controller.model_cycle()
        wire.assert_not_called()
        receipt = f.view(item)['reviews'][0]['receipt']
        self.assertEqual(receipt['error_code'],'policy_expired')
        self.assertFalse(receipt['http_attempted'])
        self.assertEqual(f.count('news_review_content_claims'),1)
        self.assertEqual(f.count('provider_call_attempts'),1)
        self.assertEqual(f.service.snapshot(f.policy['policy_id'])['calls_reserved'],1)

    def stop_fixture(self):
        f = self.f
        journal = NewsReviewJournal(f.service,f.policy['policy_id'])
        journal.record('session_started')
        controller = NewsReviewController(f.service,f.policy['policy_id'])
        stopper = NewsReviewStop(f.service,f.policy,controller,journal,threading.Event(),f.path.parent.parent)
        return journal,stopper

    def test_expiry_while_waiting_for_send_gate_is_checked_inside_admission(self):
        f = self.f
        item, _ = f.prepare()
        controller = NewsReviewController(f.service,f.policy['policy_id'])
        prepared = threading.Event()
        errors = []
        def request(*args,**kwargs):
            bound = AuthorizedTextRequest(*args,**kwargs)
            check = bound.before_send
            def preflight():
                check()
                prepared.set()
            bound.before_send = preflight
            return bound
        def work():
            try:
                controller.model_cycle()
            except BaseException as exc:
                errors.append(exc)
        with patch('backend.news_review_service.AuthorizedTextRequest',side_effect=request), \
             patch('urllib.request.OpenerDirector.open') as wire:
            controller.send_gate._lock.acquire()
            worker = threading.Thread(target=work)
            try:
                worker.start()
                self.assertTrue(prepared.wait(10))
                self.advance(10000,-1000)
            finally:
                controller.send_gate._lock.release()
                worker.join(10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors,[])
            wire.assert_not_called()
        self.assertEqual(f.view(item)['reviews'][0]['receipt']['error_code'],'policy_expired')
        self.assertEqual(f.count('provider_call_attempts'),1)

    def test_stop_and_cold_reader_distinguish_elapsed_expiry_from_full_day(self):
        f = self.f
        f.approve()
        journal,stopper = self.stop_fixture()
        self.advance(10000,-1000)
        stopper.watch({},interval_seconds=.001)
        self.assertEqual(stopper.summary()['exit_code'],0)
        self.assertEqual(stopper.summary()['stop_events'][0]['stop_type'],'window_elapsed')
        journal.record('session_stopped')
        expected = build_news_review_report(f.service,f.policy['policy_id'])
        f.service.end_host_session()
        report = build_news_review_report(self.new_service(),f.policy['policy_id'])
        self.assertEqual(report['authorization_time'],expected['authorization_time'])
        self.assertTrue(report['authorization_time']['authorized_window_end_observed'])
        self.assertFalse(report['authorization_time']['actual_full_24h_proven'])
        self.assertEqual(report['authorization_time']['authorized_elapsed_lower_ms'],10000)
        self.assertEqual(report['authorization_time']['stop_reason'],'elapsed_deadline_reached')
        self.assertTrue(report['outcome']['work_completed'])
        self.assertFalse(report['outcome']['acceptance_passed'])

    def test_process_pin_fault_cannot_be_erased_by_later_wall_expiry(self):
        f = self.f
        f.approve()
        journal,stopper = self.stop_fixture()
        self.pin = (self.pin[0],self.pin[1]+1)
        stopper.watch({},interval_seconds=.001)
        self.assertEqual(stopper.summary()['exit_code'],2)
        self.assertEqual(stopper.summary()['stop_events'][0]['error_code'],'clock_changed')
        self.advance(10000)
        journal.record('session_stopped')
        report = build_news_review_report(f.service,f.policy['policy_id'])
        self.assertFalse(report['outcome']['work_completed'])
        self.assertEqual(report['authorization_time']['stop_reason'],'process_identity_changed')
        self.assertFalse(report['authorization_time']['authorized_window_end_observed'])
        self.assertEqual(report['continuity']['clock_anomalies'],1)

    def test_sleep_over_expiry_closes_authority_but_is_not_clean_completion(self):
        f = self.f
        f.approve()
        journal,stopper = self.stop_fixture()
        self.advance(35000)
        stopper.watch({},interval_seconds=.001)
        self.assertEqual(stopper.summary()['exit_code'],2)
        self.assertEqual(stopper.summary()['stop_events'][0]['error_code'],'clock_changed')
        journal.record('session_stopped')
        report = build_news_review_report(f.service,f.policy['policy_id'])
        self.assertTrue(report['authorization_time']['authorized_window_end_observed'])
        self.assertFalse(report['outcome']['work_completed'])
        self.assertEqual(report['continuity']['clock_anomalies'],1)
        self.assertFalse(report['continuity']['authorized_window_coverage_observed'])


if __name__ == '__main__':
    unittest.main()
