from __future__ import annotations

import threading
import unittest

from backend.source_poll_control import (
    SourcePollCancelled,
    SourcePollControlError,
    SourcePollDeadlineExceeded,
    ensure_source_poll_active,
    source_poll_timeout_seconds,
    validate_source_poll_control,
    wait_for_source_poll,
)


class SourcePollControlTests(unittest.TestCase):
    def test_control_requires_exact_native_deadline_and_event(self) -> None:
        event = threading.Event()
        self.assertEqual(
            validate_source_poll_control(
                deadline_monotonic_ms=123,
                cancel_event=event,
            ),
            (123, event),
        )
        for invalid in (True, 1.0, "1", -1):
            with self.subTest(deadline=repr(invalid)):
                with self.assertRaises(SourcePollControlError):
                    validate_source_poll_control(
                        deadline_monotonic_ms=invalid,
                        cancel_event=None,
                    )
        with self.assertRaises(SourcePollControlError):
            validate_source_poll_control(
                deadline_monotonic_ms=0,
                cancel_event=object(),
            )

    def test_cancel_and_deadline_have_distinct_machine_codes(self) -> None:
        event = threading.Event()
        event.set()
        with self.assertRaises(SourcePollCancelled) as cancelled:
            ensure_source_poll_active(
                deadline_monotonic_ms=0,
                cancel_event=event,
                monotonic_ms=lambda: 10,
            )
        self.assertEqual(cancelled.exception.code, "SOURCE_MONITORING_POLL_CANCELLED")

        with self.assertRaises(SourcePollDeadlineExceeded) as expired:
            ensure_source_poll_active(
                deadline_monotonic_ms=10,
                cancel_event=None,
                monotonic_ms=lambda: 10,
            )
        self.assertEqual(
            expired.exception.code,
            "SOURCE_MONITORING_POLL_DEADLINE_EXCEEDED",
        )

    def test_socket_budget_is_clipped_to_absolute_deadline(self) -> None:
        self.assertEqual(
            source_poll_timeout_seconds(
                12,
                deadline_monotonic_ms=1_500,
                cancel_event=None,
                monotonic_ms=lambda: 1_000,
            ),
            0.5,
        )

    def test_policy_wait_is_interruptible(self) -> None:
        event = threading.Event()
        trigger = threading.Timer(0.02, event.set)
        trigger.start()
        try:
            with self.assertRaises(SourcePollCancelled):
                wait_for_source_poll(
                    5,
                    deadline_monotonic_ms=0,
                    cancel_event=event,
                )
        finally:
            trigger.cancel()
            trigger.join(timeout=0.2)


if __name__ == "__main__":
    unittest.main()
