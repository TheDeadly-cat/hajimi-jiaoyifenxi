"""Shared deadline and cancellation controls for bounded source polling.

The module is deliberately independent from monitoring registries, stores, and
providers so both official HTTP clients and isolated market brokers can consume
the same control without creating an import cycle or gaining new authority.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Any, Callable


MAX_MONOTONIC_MILLISECONDS = (1 << 63) - 1
_EVENT_TYPE = type(threading.Event())


class SourcePollControlError(ValueError):
    """Raised when a poll control is malformed or has been activated."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SourcePollCancelled(SourcePollControlError):
    """Raised after the owning runtime explicitly requests cancellation."""


class SourcePollDeadlineExceeded(TimeoutError):
    """Raised after the absolute monotonic poll budget is exhausted."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_source_poll_control(
    *,
    deadline_monotonic_ms: Any,
    cancel_event: Any,
) -> tuple[int, threading.Event | None]:
    """Validate a closed poll control without coercing identity-bearing values."""

    if (
        type(deadline_monotonic_ms) is not int
        or not 0 <= deadline_monotonic_ms <= MAX_MONOTONIC_MILLISECONDS
    ):
        raise SourcePollControlError(
            "SOURCE_MONITORING_POLL_DEADLINE_INVALID",
            "deadline_monotonic_ms must be a non-negative native integer",
        )
    if cancel_event is not None and type(cancel_event) is not _EVENT_TYPE:
        raise SourcePollControlError(
            "SOURCE_MONITORING_POLL_CANCEL_EVENT_INVALID",
            "cancel_event must be an exact threading.Event or None",
        )
    return deadline_monotonic_ms, cancel_event


def ensure_source_poll_active(
    *,
    deadline_monotonic_ms: Any,
    cancel_event: Any,
    monotonic_ms: Callable[[], Any] | None = None,
) -> int:
    """Fail closed when cancellation or the absolute deadline is observable."""

    deadline, event = validate_source_poll_control(
        deadline_monotonic_ms=deadline_monotonic_ms,
        cancel_event=cancel_event,
    )
    if event is not None and event.is_set():
        raise SourcePollCancelled(
            "SOURCE_MONITORING_POLL_CANCELLED",
            "source poll was cancelled by its owning runtime",
        )
    clock = monotonic_ms or (lambda: int(time.monotonic() * 1_000))
    if not callable(clock):
        raise SourcePollControlError(
            "SOURCE_MONITORING_POLL_CLOCK_INVALID",
            "monotonic_ms must be callable",
        )
    now = clock()
    if type(now) is not int or not 0 <= now <= MAX_MONOTONIC_MILLISECONDS:
        raise SourcePollControlError(
            "SOURCE_MONITORING_POLL_CLOCK_INVALID",
            "monotonic clock must return a non-negative native integer",
        )
    if deadline and now >= deadline:
        raise SourcePollDeadlineExceeded(
            "SOURCE_MONITORING_POLL_DEADLINE_EXCEEDED",
            "source poll exceeded its absolute monotonic deadline",
        )
    return now


def source_poll_timeout_seconds(
    default_seconds: Any,
    *,
    deadline_monotonic_ms: Any,
    cancel_event: Any,
    monotonic_ms: Callable[[], Any] | None = None,
) -> float:
    """Return a positive socket budget clipped to the shared poll deadline."""

    if (
        type(default_seconds) not in {int, float}
        or isinstance(default_seconds, bool)
        or not math.isfinite(float(default_seconds))
        or float(default_seconds) <= 0
    ):
        raise SourcePollControlError(
            "SOURCE_MONITORING_POLL_TIMEOUT_INVALID",
            "default source timeout must be a finite positive native number",
        )
    clock = monotonic_ms or (lambda: int(time.monotonic() * 1_000))
    now = ensure_source_poll_active(
        deadline_monotonic_ms=deadline_monotonic_ms,
        cancel_event=cancel_event,
        monotonic_ms=clock,
    )
    if deadline_monotonic_ms == 0:
        return float(default_seconds)
    remaining = (deadline_monotonic_ms - now) / 1_000
    if remaining <= 0:
        raise SourcePollDeadlineExceeded(
            "SOURCE_MONITORING_POLL_DEADLINE_EXCEEDED",
            "source poll exceeded its absolute monotonic deadline",
        )
    return min(float(default_seconds), remaining)


def wait_for_source_poll(
    seconds: Any,
    *,
    deadline_monotonic_ms: Any,
    cancel_event: Any,
    monotonic_ms: Callable[[], Any] | None = None,
) -> None:
    """Perform an interruptible policy wait such as SEC request pacing."""

    if (
        type(seconds) not in {int, float}
        or isinstance(seconds, bool)
        or not math.isfinite(float(seconds))
        or float(seconds) < 0
    ):
        raise SourcePollControlError(
            "SOURCE_MONITORING_POLL_WAIT_INVALID",
            "source poll wait must be a finite non-negative native number",
        )
    clock = monotonic_ms or (lambda: int(time.monotonic() * 1_000))
    timeout = float(seconds)
    if timeout == 0:
        ensure_source_poll_active(
            deadline_monotonic_ms=deadline_monotonic_ms,
            cancel_event=cancel_event,
            monotonic_ms=clock,
        )
        return
    if deadline_monotonic_ms:
        timeout = source_poll_timeout_seconds(
            timeout,
            deadline_monotonic_ms=deadline_monotonic_ms,
            cancel_event=cancel_event,
            monotonic_ms=clock,
        )
    else:
        ensure_source_poll_active(
            deadline_monotonic_ms=0,
            cancel_event=cancel_event,
            monotonic_ms=clock,
        )
    if cancel_event is None:
        time.sleep(timeout)
    elif cancel_event.wait(timeout):
        raise SourcePollCancelled(
            "SOURCE_MONITORING_POLL_CANCELLED",
            "source poll was cancelled by its owning runtime",
        )
    ensure_source_poll_active(
        deadline_monotonic_ms=deadline_monotonic_ms,
        cancel_event=cancel_event,
        monotonic_ms=clock,
    )


__all__ = [
    "MAX_MONOTONIC_MILLISECONDS",
    "SourcePollCancelled",
    "SourcePollControlError",
    "SourcePollDeadlineExceeded",
    "ensure_source_poll_active",
    "source_poll_timeout_seconds",
    "validate_source_poll_control",
    "wait_for_source_poll",
]
