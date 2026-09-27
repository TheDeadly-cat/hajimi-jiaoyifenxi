import json
import threading
import unittest
from unittest.mock import patch

from backend.news_review_clock import NewsReviewClock
from backend.news_review_contracts import NewsReviewError
from backend.news_review_controller import NewsReviewController
from backend.news_review_report import NewsReviewJournal, build_news_review_report
from backend.news_review_stop import NewsReviewStop
from tests import test_news_review as fixtures


class ClockEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.wall, self.mono = 1_790_000_000_000, 100_000
        self.guard = NewsReviewClock(lambda:self.wall,lambda:self.mono)

    def test_virtual_full_day_and_sleep_with_matching_clocks(self):
        for delta in (0, 1000, 3_600_000, 43_200_000, 86_400_000):
            self.wall, self.mono = 1_790_000_000_000+delta, 100_000+delta
            self.assertTrue(self.guard.sample()['accepted'])
        self.assertIsNone(self.guard.snapshot()['first_failure'])

    def test_gradual_drift_records_failure_even_with_small_adjacent_differences(self):
        for seconds in range(2002):
            self.wall, self.mono = 1_790_000_000_000+seconds*1001, 100_000+seconds*1000
            value = self.guard.sample()
            self.assertEqual(value['accepted'], seconds<=2000)
        failure = self.guard.snapshot()['first_failure']
        self.assertEqual(failure['cumulative_difference_ms'],2001)
        self.assertEqual(failure['adjacent_difference_ms'],1)
        self.assertEqual(failure['sample_interval_monotonic_ms'],1000)

    def test_wall_jump_rollback_and_sleep_excluded_by_monotonic_remain_closed(self):
        for shift in (2001,-2001,3_600_000):
            with self.subTest(shift=shift):
                self.wall, self.mono = 1_790_000_000_000,100_000
                guard=NewsReviewClock(lambda:self.wall,lambda:self.mono)
                guard.sample()
                self.wall+=shift
                self.assertFalse(guard.sample()['accepted'])
                self.assertEqual(guard.snapshot()['first_failure']['cumulative_difference_ms'],shift)

    def test_trial27_step_then_gradual_drift_stops_without_resetting_the_anchor(self):
        # The observed pattern was a 1245 ms step, then smaller increments.
        # A small adjacent change must not mask the cumulative two-second gate.
        start_wall, start_mono = self.wall, self.mono
        for elapsed, drift in ((3_600_000, 0), (12_790_000, 1245),
                               (28_800_000, 1245), (35_070_000, 1999),
                               (35_080_000, 2000), (35_090_000, 2001)):
            self.wall, self.mono = start_wall + elapsed + drift, start_mono + elapsed
            sample = self.guard.sample()
            self.assertEqual(sample['accepted'], drift <= 2000)
        value = self.guard.snapshot()
        self.assertEqual(value['anchor_wall_ms'], start_wall)
        self.assertEqual(value['anchor_monotonic_ms'], start_mono)
        self.assertEqual(value['first_failure']['cumulative_difference_ms'], 2001)
        self.assertEqual(value['first_failure']['adjacent_difference_ms'], 1)

    def test_first_failure_is_preserved_after_further_samples_and_not_mutable_by_reader(self):
        self.wall+=2001
        self.guard.sample()
        first=self.guard.snapshot()['first_failure']
        self.wall+=100
        self.guard.sample()
        returned=self.guard.snapshot()
        self.assertEqual(returned['first_failure'],first)
        returned['first_failure']['wall_ms']=0
        self.assertEqual(self.guard.snapshot()['first_failure'],first)

    def test_non_integer_readings_are_not_serialized_as_objects(self):
        self.wall=object()
        self.assertFalse(self.guard.sample()['accepted'])
        snapshot=self.guard.snapshot()
        self.assertIsNone(snapshot['first_failure']['wall_ms'])
        json.dumps(snapshot)


class ClockGateIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.NewsReviewTests()
        self.f.addCleanup=self.addCleanup
        self.f.setUp()

    def test_authorization_expiry_still_blocks_send_without_resetting_reservations(self):
        f=self.f
        _,job=f.prepare()
        f.run_one()
        before=f.service.snapshot(f.policy['policy_id'])
        f.now=f.policy['expires_at_ms']
        with patch('urllib.request.OpenerDirector.open') as wire, self.assertRaises(NewsReviewError) as raised:
            f.service.run_one(f.policy['policy_id'])
        self.assertEqual(raised.exception.code,'policy_expired')
        after=f.service.snapshot(f.policy['policy_id'])
        self.assertEqual(wire.call_count,0)
        self.assertEqual(after['policy']['expires_at_ms'],f.policy['expires_at_ms'])
        for field in ('calls_reserved','documents_reserved','tokens_reserved','cost_reserved_cny'):
            self.assertEqual(after[field],before[field])

    def test_stop_and_report_preserve_trigger_readings_and_distinguish_adjacent_metric(self):
        f=self.f
        f.approve()
        journal=NewsReviewJournal(f.service,f.policy['policy_id'])
        journal.record('session_started')
        initial=f.now
        f.service.clock_guard=NewsReviewClock(lambda:f.now,lambda:initial)
        f.now+=2001
        with self.assertRaises(NewsReviewError) as raised:
            f.service.check_clock()
        self.assertEqual(raised.exception.code,'clock_changed')
        controller=NewsReviewController(f.service,f.policy['policy_id'])
        host_stop=threading.Event()
        stop=NewsReviewStop(f.service,f.policy,controller,journal,host_stop,f.path.parent.parent)
        stop.request('worker_failure',trigger='evidence',code='clock_changed')
        self.assertTrue(host_stop.is_set())
        self.assertTrue(controller.stop_event.is_set())
        receipt=json.loads(next(f.path.parent.parent.glob('stop-*.json')).read_text(encoding='utf-8'))
        self.assertEqual(receipt['clock_diagnostics']['first_failure']['cumulative_difference_ms'],2001)
        journal.record('session_stopped')
        report=build_news_review_report(f.service,f.policy['policy_id'])
        self.assertEqual(report['clock_guard_diagnostics'],receipt['clock_diagnostics'])
        self.assertEqual(report['clock_anomalies_basis'],'adjacent_heartbeat_samples')
        self.assertFalse(report['outcome']['acceptance_passed'])
        self.assertFalse(report['continuity']['continuous_window_observed'])
