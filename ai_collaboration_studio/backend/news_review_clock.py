"""Bounded clock evidence; the original cumulative two-second gate is unchanged."""
from __future__ import annotations

import threading


class NewsReviewClock:
    def __init__(self, wall_ms, monotonic_ms):
        self.wall_ms, self.monotonic_ms = wall_ms, monotonic_ms
        self.anchor = (wall_ms(), monotonic_ms())
        self._lock = threading.Lock()
        self._last = None
        self._first_failure = None
        self._sequence = 0

    def sample(self):
        with self._lock:
            wall, monotonic = self.wall_ms(), self.monotonic_ms()
            valid = all(type(value) is int for value in (*self.anchor, wall, monotonic))
            previous = self._last
            self._sequence += 1
            wall_delta = wall-self.anchor[0] if valid else None
            mono_delta = monotonic-self.anchor[1] if valid else None
            interval_wall = wall-previous['wall_ms'] if valid and previous and previous['readings_valid'] else None
            interval_mono = monotonic-previous['monotonic_ms'] if valid and previous and previous['readings_valid'] else None
            cumulative = wall_delta-mono_delta if valid else None
            value = {'sequence':self._sequence, 'wall_ms':wall if type(wall) is int else None,
                     'monotonic_ms':monotonic if type(monotonic) is int else None,
                     'wall_since_anchor_ms':wall_delta, 'monotonic_since_anchor_ms':mono_delta,
                     'cumulative_difference_ms':cumulative, 'sample_interval_wall_ms':interval_wall,
                     'sample_interval_monotonic_ms':interval_mono,
                     'adjacent_difference_ms':interval_wall-interval_mono if interval_wall is not None else None,
                     'readings_valid':valid, 'accepted':bool(valid and abs(cumulative)<=2000)}
            self._last = value
            if not value['accepted'] and self._first_failure is None:
                self._first_failure = dict(value)
            return dict(value)

    def snapshot(self):
        with self._lock:
            return {'version':'news_review_clock_diagnostics_v1', 'threshold_ms':2000,
                    'gate_basis':'cumulative_difference_from_service_start',
                    'anchor_wall_ms':self.anchor[0] if type(self.anchor[0]) is int else None,
                    'anchor_monotonic_ms':self.anchor[1] if type(self.anchor[1]) is int else None,
                    'latest_sample':dict(self._last) if self._last else None,
                    'first_failure':dict(self._first_failure) if self._first_failure else None}
