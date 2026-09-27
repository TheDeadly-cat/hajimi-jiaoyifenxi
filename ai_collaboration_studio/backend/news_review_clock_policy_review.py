"""Time-bound evaluator, with no standalone request or activation authority.

The runtime adapter binds this evaluator to one explicitly versioned policy and
owned service session. Legacy policies retain the cumulative two-second guard.
The evaluator alone cannot activate, restart, reserve or send any request.
"""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass

VERSION = 'news_review_dual_deadline_reference_v1'
CLOCK_BASIS = 'windows_query_performance_counter_including_suspend'
MAX_DURATION_MS = 86_400_000
MAX_STEP_MS = 2_000
MAX_OBSERVATION_GAP_MS = 30_000
MAX_SAMPLING_SPAN_MS = 50
MAX_INTEGER = (1 << 63) - 1


def _integer(value):
    return type(value) is int and 0 <= value <= MAX_INTEGER


@dataclass(frozen=True)
class Reading:
    wall_ms: int
    monotonic_before_ms: int
    monotonic_after_ms: int
    process_id: int
    process_creation_utc_ticks: int
    monotonic_before_ns: int | None = None
    monotonic_after_ns: int | None = None

    def valid(self):
        native = (self.monotonic_before_ns, self.monotonic_after_ns)
        native_valid = (native == (None, None) or
            all(_integer(v) for v in native) and native[0] <= native[1]
            and native[0]//1_000_000 == self.monotonic_before_ms
            and (native[1]+999_999)//1_000_000 == self.monotonic_after_ms)
        return (native_valid and all(_integer(v) for v in (self.wall_ms, self.monotonic_before_ms,
                self.monotonic_after_ms, self.process_id, self.process_creation_utc_ticks))
                and self.process_id > 0 and self.process_creation_utc_ticks > 0
                and 0 <= self.monotonic_after_ms-self.monotonic_before_ms <= MAX_SAMPLING_SPAN_MS)


@dataclass(frozen=True)
class BoundWindow:
    policy_sha256: str
    not_before_ms: int
    expires_at_ms: int
    activation_reading: Reading
    clock_basis: str = CLOCK_BASIS

    def valid(self):
        import re
        return (type(self.policy_sha256) is str and re.fullmatch('[0-9a-f]{64}', self.policy_sha256)
                and _integer(self.not_before_ms) and _integer(self.expires_at_ms)
                and 0 < self.expires_at_ms-self.not_before_ms <= MAX_DURATION_MS
                and type(self.activation_reading) is Reading and self.activation_reading.valid()
                and self.not_before_ms <= self.activation_reading.wall_ms < self.expires_at_ms
                and self.clock_basis == CLOCK_BASIS)


class DualDeadlineReference:
    """Assess injected observations; no authority to create or restore a window.

    The caller supplies the original activation reading, not a fresh anchor on
    restart. A different process pin stops even if the PID was reused. Actual
    integration must enforce single activation independently of this object.
    """
    def __init__(self, window: BoundWindow, sample):
        if type(window) is not BoundWindow or not window.valid() or not callable(sample):
            raise ValueError('invalid_review_clock_window')
        self._window = window
        self._sample = sample
        self._last = window.activation_reading
        self._high_wall = self._last.wall_ms
        # Use the early edge of the activation bracket so sampling latency
        # cannot grant more elapsed time than remained at activation.
        self._initial_deadline = self._last.monotonic_before_ms + window.expires_at_ms-self._last.wall_ms
        if self._initial_deadline > MAX_INTEGER:
            raise ValueError('invalid_review_clock_deadline')
        self._deadline = self._initial_deadline
        self._first_stop = None
        self._latest = None
        self._sequence = 0
        self._lock = threading.Lock()

    def check(self):
        with self._lock:
            if self._first_stop is not None:
                return copy.deepcopy(self._first_stop)
            try:
                reading = self._sample()
            except Exception:
                reading = None
            self._sequence += 1
            reason = ''
            metrics = {}
            if type(reading) is not Reading or not reading.valid():
                reason = 'clock_sample_unconfirmed'
            elif (reading.process_id, reading.process_creation_utc_ticks) != (
                    self._window.activation_reading.process_id,
                    self._window.activation_reading.process_creation_utc_ticks):
                reason = 'process_identity_changed'
            elif (reading.monotonic_before_ns is None) != (self._last.monotonic_before_ns is None):
                reason = 'clock_precision_changed'
            elif (reading.monotonic_before_ns < self._last.monotonic_after_ns
                  if reading.monotonic_before_ns is not None else
                  reading.monotonic_before_ms < self._last.monotonic_after_ms):
                reason = 'monotonic_clock_regressed'
            else:
                previous = self._last
                interval_min = reading.monotonic_before_ms-previous.monotonic_after_ms
                interval_max = reading.monotonic_after_ms-previous.monotonic_before_ms
                if reading.monotonic_before_ns is not None:
                    # Native nanoseconds prove order even when conservatively
                    # rounded millisecond brackets overlap during rapid checks.
                    interval_min = (reading.monotonic_before_ns-previous.monotonic_after_ns)//1_000_000
                    interval_max = (reading.monotonic_after_ns-previous.monotonic_before_ns+999_999)//1_000_000
                wall_delta = reading.wall_ms-previous.wall_ms
                # Native wall timestamps are floored to milliseconds. Include
                # the difference's submillisecond uncertainty in both limits.
                uncertainty = 1 if reading.monotonic_before_ns is not None else 0
                step_min, step_max = wall_delta-interval_max-uncertainty, wall_delta-interval_min+uncertainty
                step_exceeded = step_min > MAX_STEP_MS or step_max < -MAX_STEP_MS
                step_unconfirmed = not step_exceeded and (step_min < -MAX_STEP_MS or step_max > MAX_STEP_MS)
                rollback = max(0, self._high_wall-reading.wall_ms)
                rollback_exceeded = rollback-uncertainty > MAX_STEP_MS
                rollback_unconfirmed = not rollback_exceeded and rollback+uncertainty > MAX_STEP_MS
                metrics = {'wall_delta_ms': wall_delta, 'elapsed_interval_min_ms': interval_min,
                           'elapsed_interval_max_ms': interval_max,
                           'clock_step_min_ms': step_min, 'clock_step_max_ms': step_max,
                           'clock_step_exceeded': step_exceeded,
                           'clock_step_limit_unconfirmed': step_unconfirmed,
                           'wall_quantization_uncertainty_ms': uncertainty,
                           'wall_rollback_from_high_water_ms': rollback,
                           'wall_rollback_limit_unconfirmed': rollback_unconfirmed,
                           'observation_gap_exceeded': interval_min > MAX_OBSERVATION_GAP_MS,
                           'observation_gap_limit_unconfirmed':
                               interval_min <= MAX_OBSERVATION_GAP_MS < interval_max}
                # Check expiry before any possible later clock correction.
                # Once stopped, subsequent samples cannot reopen the window.
                if reading.wall_ms >= self._window.expires_at_ms:
                    reason = 'absolute_deadline_reached'
                elif reading.monotonic_after_ms >= self._deadline:
                    reason = 'elapsed_deadline_reached'
                elif reading.wall_ms < self._window.not_before_ms:
                    reason = 'wall_clock_before_authorization'
                elif interval_min > MAX_OBSERVATION_GAP_MS:
                    reason = 'observation_gap_exceeded'
                elif interval_max > MAX_OBSERVATION_GAP_MS:
                    # Sampling uncertainty must not silently widen the gap
                    # limit. Distinguish an uncertain interval from a proven gap.
                    reason = 'observation_gap_limit_unconfirmed'
                elif rollback_exceeded:
                    reason = 'wall_clock_rollback'
                elif rollback_unconfirmed:
                    reason = 'wall_clock_rollback_limit_unconfirmed'
                elif step_exceeded:
                    reason = 'wall_clock_step_exceeded'
                elif step_unconfirmed:
                    reason = 'wall_clock_step_limit_unconfirmed'
                else:
                    # A forward correction may only shorten the deadline.
                    # A later rollback cannot return any time already removed.
                    self._deadline = min(self._deadline, reading.monotonic_before_ms +
                                         self._window.expires_at_ms-reading.wall_ms)
                    if reading.monotonic_after_ms >= self._deadline:
                        reason = 'elapsed_deadline_reached'
                self._high_wall = max(self._high_wall, reading.wall_ms)
                self._last = reading
            verdict = {'version': VERSION, 'sequence': self._sequence,
                       'policy_sha256': self._window.policy_sha256,
                       'within_proposed_time_bounds': not reason, 'stop_reason': reason,
                       'absolute_expires_at_ms': self._window.expires_at_ms,
                       'initial_elapsed_deadline_ms': self._initial_deadline,
                       'effective_elapsed_deadline_ms': self._deadline,
                       'deadline_extended': self._deadline > self._initial_deadline,
                       'metrics': metrics, 'runtime_integrated': False,
                       'reading': dict(vars(reading)) if type(reading) is Reading and reading.valid() else None,
                       'request_authorized': False, 'live_acceptance_proven': False}
            self._latest = verdict
            if reason:
                self._first_stop = copy.deepcopy(verdict)
            return copy.deepcopy(verdict)

    def snapshot(self):
        with self._lock:
            return {'version': VERSION, 'policy_sha256': self._window.policy_sha256,
                    'clock_basis': self._window.clock_basis,
                    'absolute_expires_at_ms': self._window.expires_at_ms,
                    'initial_elapsed_deadline_ms': self._initial_deadline,
                    'effective_elapsed_deadline_ms': self._deadline,
                    'latest': copy.deepcopy(self._latest),
                    'first_stop': copy.deepcopy(self._first_stop),
                    'runtime_integrated': False, 'request_authorized': False}
