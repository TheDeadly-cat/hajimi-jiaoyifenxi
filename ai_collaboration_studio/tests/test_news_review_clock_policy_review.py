"""Virtual-clock review of an unregistered policy; never runs the application."""
import dataclasses
import unittest

from backend.news_review_clock_policy_review import (
    BoundWindow, DualDeadlineReference, Reading, MAX_DURATION_MS,
)


class DualDeadlineReferenceTests(unittest.TestCase):
    def setUp(self):
        self.start = 1790000000000
        self.mono = 100000
        self.pin = (1234, 639260000000000000)
        self.current = Reading(self.start, self.mono, self.mono, *self.pin)
        self.window = BoundWindow('a'*64, self.start, self.start+MAX_DURATION_MS, self.current)
        self.guard = DualDeadlineReference(self.window, lambda: self.current)

    def advance(self, elapsed, offset=0, **changes):
        self.current = dataclasses.replace(Reading(self.start+elapsed+offset,
            self.mono+elapsed, self.mono+elapsed, *self.pin), **changes)
        return self.guard.check()

    def test_normal_virtual_day_never_admits_at_or_after_expiry(self):
        for elapsed in range(0, MAX_DURATION_MS, 10000):
            self.assertTrue(self.advance(elapsed)['within_proposed_time_bounds'])
        final = self.advance(MAX_DURATION_MS)
        self.assertEqual(final['stop_reason'], 'absolute_deadline_reached')
        self.assertFalse(final['request_authorized'])

    def test_trial27_forward_step_then_gradual_offset_does_not_reset_or_extend_time(self):
        shortened = []
        for elapsed in range(0, MAX_DURATION_MS, 10000):
            step = 1245 if elapsed >= 12_790_000 else 0
            drift = min(757, max(0, elapsed-28_800_000)//8000)
            result = self.advance(elapsed, step+drift)
            self.assertTrue(result['within_proposed_time_bounds'])
            shortened.append(result['effective_elapsed_deadline_ms'])
        self.assertEqual(shortened[-1], self.mono+MAX_DURATION_MS-2002)
        self.assertEqual(shortened, sorted(shortened, reverse=True))
        self.assertFalse(self.advance(MAX_DURATION_MS, 2002)['within_proposed_time_bounds'])

    def test_slow_wall_clock_stops_at_original_elapsed_budget(self):
        for elapsed in range(0, MAX_DURATION_MS, 10000):
            result = self.advance(elapsed, -(elapsed//10000))
            self.assertTrue(result['within_proposed_time_bounds'])
        result = self.advance(MAX_DURATION_MS, -8640)
        self.assertEqual(result['stop_reason'], 'elapsed_deadline_reached')
        self.assertEqual(result['effective_elapsed_deadline_ms'], self.mono+MAX_DURATION_MS)

    def test_forward_then_small_rollback_cannot_recover_shortened_time(self):
        first = self.advance(10000, 1245)
        second = self.advance(20000, 0)
        self.assertTrue(second['within_proposed_time_bounds'])
        self.assertEqual(first['effective_elapsed_deadline_ms'], second['effective_elapsed_deadline_ms'])

    def test_forward_expiry_is_latched_after_clock_is_corrected_back(self):
        result = self.advance(1000, MAX_DURATION_MS)
        self.assertEqual(result['stop_reason'], 'absolute_deadline_reached')
        self.assertEqual(self.advance(2000), result)

    def test_large_forward_and_backward_steps_stop_within_window(self):
        for delta in (2001, -2001):
            with self.subTest(delta=delta):
                guard = DualDeadlineReference(self.window, lambda: self.current)
                self.current = Reading(self.start+10000+delta, self.mono+10000, self.mono+10000, *self.pin)
                self.assertEqual(guard.check()['stop_reason'], 'wall_clock_step_exceeded')

    def test_step_boundary_is_not_silently_widened(self):
        result = self.advance(10000, 2000)
        self.assertTrue(result['within_proposed_time_bounds'])
        self.assertEqual(self.advance(20000, 4001)['stop_reason'], 'wall_clock_step_exceeded')

    def test_repeated_small_rollbacks_are_compared_with_high_water(self):
        self.advance(10000)
        self.assertTrue(self.advance(10001, -1001)['within_proposed_time_bounds'])
        self.assertTrue(self.advance(10002, -2002)['within_proposed_time_bounds'])
        self.assertEqual(self.advance(10003, -3004)['stop_reason'], 'wall_clock_rollback')

    def test_sleep_inside_window_is_an_observation_gap_not_extra_time(self):
        result = self.advance(60000)
        self.assertEqual(result['stop_reason'], 'observation_gap_exceeded')
        self.assertTrue(result['metrics']['observation_gap_exceeded'])

    def test_sampling_uncertainty_cannot_widen_observation_gap_limit(self):
        activation = dataclasses.replace(self.window.activation_reading,
            monotonic_after_ms=self.mono+50)
        guard = DualDeadlineReference(dataclasses.replace(self.window,
            activation_reading=activation), lambda: self.current)
        self.current = Reading(self.start+30025, self.mono+30025,
            self.mono+30025, *self.pin)
        result = guard.check()
        self.assertEqual(result['stop_reason'], 'observation_gap_limit_unconfirmed')
        self.assertEqual(result['metrics']['elapsed_interval_min_ms'], 29975)
        self.assertEqual(result['metrics']['elapsed_interval_max_ms'], 30025)
        self.assertFalse(result['metrics']['observation_gap_exceeded'])
        self.assertTrue(result['metrics']['observation_gap_limit_unconfirmed'])
        self.assertTrue(self.advance(30000)['within_proposed_time_bounds'])

    def test_sleep_past_expiry_stops_even_if_wall_clock_is_slow(self):
        result = self.advance(MAX_DURATION_MS+1000, -100000)
        self.assertEqual(result['stop_reason'], 'elapsed_deadline_reached')
        self.assertTrue(result['metrics']['observation_gap_exceeded'])

    def test_monotonic_regression_and_pid_reuse_cannot_resume(self):
        self.advance(10000)
        self.assertEqual(self.advance(9999)['stop_reason'], 'monotonic_clock_regressed')
        other = DualDeadlineReference(self.window, lambda: self.current)
        self.current = dataclasses.replace(self.window.activation_reading,
            process_creation_utc_ticks=self.pin[1]+1)
        self.assertEqual(other.check()['stop_reason'], 'process_identity_changed')

    def test_slow_sample_is_unconfirmed_and_callback_exceptions_stay_closed(self):
        result = self.advance(100, monotonic_after_ms=self.mono+151)
        self.assertEqual(result['stop_reason'], 'clock_sample_unconfirmed')
        def error():
            raise RuntimeError('not serialized')
        other = DualDeadlineReference(self.window, error)
        self.assertEqual(other.check()['stop_reason'], 'clock_sample_unconfirmed')

    def test_bracket_latency_cannot_extend_initial_deadline(self):
        activation = dataclasses.replace(self.window.activation_reading, monotonic_after_ms=self.mono+50)
        guard = DualDeadlineReference(dataclasses.replace(self.window, activation_reading=activation), lambda: self.current)
        self.assertEqual(guard.snapshot()['initial_elapsed_deadline_ms'], self.mono+MAX_DURATION_MS)

    def test_invalid_window_and_unsupported_clock_semantics_are_rejected(self):
        for window in (dataclasses.replace(self.window, clock_basis='linux_clock_monotonic_excludes_suspend'),
                       dataclasses.replace(self.window, expires_at_ms=self.window.expires_at_ms+1),
                       dataclasses.replace(self.window, not_before_ms=True),
                       dataclasses.replace(self.window, policy_sha256='unbound')):
            with self.subTest(window=window), self.assertRaises(ValueError):
                DualDeadlineReference(window, lambda: self.current)

    def test_verdict_and_snapshot_do_not_mutate_original_stop_or_bound_window(self):
        result = self.advance(10000, 2001)
        result['stop_reason'] = 'overwritten'
        snapshot = self.guard.snapshot()
        snapshot['first_stop']['stop_reason'] = 'overwritten'
        self.assertEqual(self.guard.check()['stop_reason'], 'wall_clock_step_exceeded')
        self.assertEqual(self.window.expires_at_ms, self.start+MAX_DURATION_MS)


if __name__ == '__main__':
    unittest.main()
