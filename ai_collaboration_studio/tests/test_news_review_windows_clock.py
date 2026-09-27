"""Native sampling boundaries; no clock changes, suspend or network actions."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.news_review_clock_policy_review import BoundWindow, DualDeadlineReference, Reading
from backend.news_review_windows_clock import (
    _capability, _current_process_identity, _milliseconds,
    inspect_windows_clock, sample_windows_clock,
)


class WindowsClockReadingTests(unittest.TestCase):
    def setUp(self):
        self.info = SimpleNamespace(monotonic=True, adjustable=False,
                                    implementation="QueryPerformanceCounter()", resolution=1e-7)

    def mocked_reading(self, before=100, wall=1000, after=101, pin=(17, 639261194770000000)):
        with patch("backend.news_review_windows_clock._capability", return_value={}), \
             patch("backend.news_review_windows_clock._current_process_identity", return_value=pin), \
             patch("backend.news_review_windows_clock.time.monotonic_ns", side_effect=[before * 1_000_000, after * 1_000_000]) as mono, \
             patch("backend.news_review_windows_clock.time.time_ns", return_value=wall * 1_000_000):
            value = sample_windows_clock()
        self.assertEqual(mono.call_count, 2)
        return value

    def test_reading_keeps_both_edges_and_full_native_ticks(self):
        reading = self.mocked_reading()
        self.assertEqual(reading, Reading(1000, 100, 101, 17, 639261194770000000, 100000000, 101000000))
        window = BoundWindow("a" * 64, 1000, 11000, reading)
        guard = DualDeadlineReference(window, lambda: Reading(1001, 101, 102, 17, 639261194770000000, 101000000, 102000000))
        self.assertTrue(guard.check()["within_proposed_time_bounds"])
        self.assertEqual(guard.snapshot()["initial_elapsed_deadline_ms"], 10100)

    def test_long_or_regressed_sampling_bracket_is_rejected_without_retry(self):
        for after in (151, 99):
            with self.subTest(after=after), self.assertRaisesRegex(ValueError, "native_clock_sample_unconfirmed"):
                self.mocked_reading(after=after)

    def test_invalid_identity_is_rejected(self):
        for pin in ((0, 123), (17, 0), (True, 123), (17, "639261194770000000")):
            with self.subTest(pin=pin), self.assertRaisesRegex(ValueError, "native_clock_sample_unconfirmed"):
                self.mocked_reading(pin=pin)

    def test_nanoseconds_remain_native_integers(self):
        for invalid in (True, -1, 1.0, None):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "native_clock_reading_unconfirmed"):
                _milliseconds(invalid)
        self.assertEqual(_milliseconds(123456789), 123)
        self.assertEqual(_milliseconds(123456789, round_up=True), 124)

    def test_rounding_cannot_hide_a_sampling_span_across_the_limit(self):
        with patch("backend.news_review_windows_clock._capability", return_value={}), \
             patch("backend.news_review_windows_clock._current_process_identity", return_value=(17, 123)), \
             patch("backend.news_review_windows_clock.time.monotonic_ns", side_effect=[100000001, 150000001]) as mono, \
             patch("backend.news_review_windows_clock.time.time_ns", return_value=1000000000), \
             self.assertRaisesRegex(ValueError, "native_clock_sample_unconfirmed"):
            sample_windows_clock()
        self.assertEqual(mono.call_count, 2)

    def test_native_precision_proves_order_of_overlapping_rounded_brackets(self):
        first = Reading(1000, 100, 101, 17, 123, 100100000, 100200000)
        current = Reading(1000, 100, 101, 17, 123, 100300000, 100400000)
        window = BoundWindow("a" * 64, 1000, 11000, first)
        guard = DualDeadlineReference(window, lambda: current)
        self.assertTrue(guard.check()["within_proposed_time_bounds"])

    def test_submillisecond_regression_and_precision_loss_are_rejected(self):
        first = Reading(1000, 100, 101, 17, 123, 100100000, 100200000)
        window = BoundWindow("a" * 64, 1000, 11000, first)
        for current, reason in ((Reading(1000, 100, 101, 17, 123, 100150000, 100190000), "monotonic_clock_regressed"),
                                (Reading(1001, 101, 102, 17, 123), "clock_precision_changed")):
            with self.subTest(reason=reason):
                guard = DualDeadlineReference(window, lambda: current)
                self.assertEqual(guard.check()["stop_reason"], reason)

    def test_wall_quantization_cannot_hide_a_step_or_rollback_boundary(self):
        first = Reading(1000, 100, 100, 17, 123, 100000000, 100000000)
        current = Reading(5000, 2100, 2100, 17, 123, 2100000000, 2100000000)
        window = BoundWindow("a"*64, 1000, 11000, first)
        guard = DualDeadlineReference(window, lambda: current)
        result = guard.check()
        self.assertEqual(result["stop_reason"], "wall_clock_step_limit_unconfirmed")
        self.assertEqual((result["metrics"]["clock_step_min_ms"], result["metrics"]["clock_step_max_ms"]), (1999, 2001))
        current = Reading(2000, 1100, 1100, 17, 123, 1100000000, 1100000000)
        guard = DualDeadlineReference(window, lambda: current)
        self.assertTrue(guard.check()["within_proposed_time_bounds"])
        current = Reading(5000, 4100, 4100, 17, 123, 4100000000, 4100000000)
        self.assertTrue(guard.check()["within_proposed_time_bounds"])
        current = Reading(3000, 4100, 4100, 17, 123, 4100000000, 4100000000)
        self.assertEqual(guard.check()["stop_reason"], "wall_clock_rollback_limit_unconfirmed")

    def test_unconfirmed_process_identity_stops_sampling(self):
        with patch("backend.news_review_windows_clock._capability", return_value={}), \
             patch("backend.news_review_windows_clock._current_process_identity", side_effect=ValueError("current_process_creation_unconfirmed")), \
             self.assertRaisesRegex(ValueError, "current_process_creation_unconfirmed"):
            sample_windows_clock()

    def test_non_windows_is_not_silently_accepted(self):
        with patch("backend.news_review_windows_clock.os.name", "posix"), \
             self.assertRaisesRegex(ValueError, "dual_clock_requires_windows"):
            _capability()

    def test_qpc_implementation_and_flags_are_required(self):
        for change in ({"monotonic": False}, {"adjustable": True}, {"implementation": "other_clock"},
                       {"resolution": float("nan")}, {"resolution": 0.1}, {"resolution": 0}):
            value = SimpleNamespace(**(vars(self.info) | change))
            with self.subTest(change=change), patch("backend.news_review_windows_clock.os.name", "nt"), \
                 patch("backend.news_review_windows_clock.time.get_clock_info", return_value=value), \
                 self.assertRaisesRegex(ValueError, "windows_qpc_capability_unconfirmed"):
                _capability()

    def test_inspection_does_not_claim_activation_suspend_or_external_utc(self):
        with patch("backend.news_review_windows_clock._capability", return_value={"implementation": "QueryPerformanceCounter()"}), \
             patch("backend.news_review_windows_clock.sample_windows_clock", return_value=Reading(1000, 100, 101, 17, 123)):
            result = inspect_windows_clock()
        for key in ("activation_created", "request_authorized", "runtime_integrated", "external_utc_verified", "actual_suspend_test_performed"):
            self.assertFalse(result[key])

    @unittest.skipUnless(os.name == "nt", "native process API is Windows-only")
    def test_actual_current_process_identity_is_stable(self):
        first = _current_process_identity()
        second = _current_process_identity()
        self.assertEqual(first, second)
        self.assertEqual(first[0], os.getpid())
        self.assertIs(type(first[1]), int)
        self.assertGreater(first[1], 504911232000000000)


if __name__ == "__main__":
    unittest.main()
