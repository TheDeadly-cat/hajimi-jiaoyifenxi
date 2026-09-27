from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from backend.source_poll_control import (
    SourcePollCancelled,
    SourcePollControlError,
    SourcePollDeadlineExceeded,
    ensure_source_poll_active,
    source_poll_timeout_seconds,
    validate_source_poll_control,
    wait_for_source_poll,
    source_poll_authorization,
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

    def test_inherited_authorization_clips_socket_wait_and_resets_after_context(self):
        now, deadline = 1000, 1600
        def timeout(own=0):
            return source_poll_timeout_seconds(12,deadline_monotonic_ms=own,
                cancel_event=None,monotonic_ms=lambda:now)
        with source_poll_authorization(lambda:None,deadline=lambda:deadline,expiry_code='NEWS_REVIEW_WINDOW_EXPIRED'):
            self.assertEqual(timeout(),.6)
            self.assertEqual(timeout(1100),.1)
            deadline=1400
            self.assertEqual(timeout(),.4)
            with patch('backend.source_poll_control.time.sleep') as sleep:
                wait_for_source_poll(5,deadline_monotonic_ms=0,cancel_event=None,monotonic_ms=lambda:now)
            sleep.assert_called_once_with(.4)
            with self.assertRaises(ValueError):
                with source_poll_authorization(lambda:None):
                    self.fail('nested context entered')
            now=1400
            with self.assertRaises(SourcePollCancelled) as raised:
                timeout()
            self.assertEqual(raised.exception.code,'NEWS_REVIEW_WINDOW_EXPIRED')
        self.assertEqual(timeout(),12)

    def test_inherited_authorization_rejects_missing_or_invalid_deadline(self):
        for value in (None, True, 1.5, -1, 0, 1<<63):
            with self.subTest(value=value), source_poll_authorization(lambda:None,deadline=lambda:value):
                with self.assertRaises(SourcePollControlError) as raised:
                    ensure_source_poll_active(deadline_monotonic_ms=0,cancel_event=None,monotonic_ms=lambda:0)
                self.assertEqual(raised.exception.code,'SOURCE_MONITORING_AUTHORIZATION_DEADLINE_INVALID')
        for options in ({'deadline':123},{'expiry_code':'private text'}):
            with self.assertRaises(ValueError):
                with source_poll_authorization(lambda:None,**options):
                    self.fail('invalid context entered')

    def test_time_spent_checking_authorization_is_not_added_back_to_socket_budget(self):
        now = 1000
        def deadline():
            nonlocal now
            now += 100
            return 2000
        with source_poll_authorization(lambda:None,deadline=deadline):
            budget = source_poll_timeout_seconds(12,deadline_monotonic_ms=0,
                cancel_event=None,monotonic_ms=lambda:now)
        self.assertEqual(budget,(2000-now)/1000)


if __name__ == "__main__":
    unittest.main()
