"""Native Windows readings for clock integration; no activation or runtime I/O.

Only the calling process is inspected. Capturing a reading does not create a
window, replace an activation anchor or grant source/provider authority.
"""
from __future__ import annotations

import ctypes
import math
import os
import time

from .news_review_clock_policy_review import CLOCK_BASIS, Reading

_FILETIME_TO_UTC_TICKS = 504911232000000000


def _capability():
    if os.name != "nt":
        raise ValueError("dual_clock_requires_windows")
    info = time.get_clock_info("monotonic")
    if (info.monotonic is not True or info.adjustable is not False
            or info.implementation != "QueryPerformanceCounter()"
            or type(info.resolution) not in {int, float}
            or not math.isfinite(info.resolution) or not 0 < info.resolution <= 0.001):
        raise ValueError("windows_qpc_capability_unconfirmed")
    return {"clock_basis": CLOCK_BASIS, "implementation": info.implementation,
            "monotonic": info.monotonic, "adjustable": info.adjustable,
            "resolution_seconds": info.resolution}


def _current_process_identity():
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    fields = [wintypes.FILETIME() for _ in range(4)]
    # GetCurrentProcess returns a pseudo handle; it must not be closed.
    if not kernel.GetProcessTimes(kernel.GetCurrentProcess(), *[ctypes.byref(field) for field in fields]):
        raise ValueError("current_process_creation_unconfirmed")
    creation = (fields[0].dwHighDateTime << 32) + fields[0].dwLowDateTime
    if creation <= 0:
        raise ValueError("current_process_creation_unconfirmed")
    return os.getpid(), creation + _FILETIME_TO_UTC_TICKS


def _milliseconds(nanoseconds, *, round_up=False):
    if type(nanoseconds) is not int or nanoseconds < 0:
        raise ValueError("native_clock_reading_unconfirmed")
    return (nanoseconds + (999_999 if round_up else 0)) // 1_000_000


def sample_windows_clock():
    """Bracket wall time and native identity with monotonic readings.

    Do not retry here. A long/invalid sample must remain a failed observation,
    not be replaced by a later reading that could slide a proposed anchor.
    """
    _capability()
    before_ns = time.monotonic_ns()
    before = _milliseconds(before_ns)
    process_id, creation_ticks = _current_process_identity()
    wall = _milliseconds(time.time_ns())
    # Preserve an enclosing interval when reducing nanoseconds to milliseconds:
    # rounding the late edge down could hide a span beyond the 50 ms limit.
    after_ns = time.monotonic_ns()
    after = _milliseconds(after_ns, round_up=True)
    value = Reading(wall, before, after, process_id, creation_ticks, before_ns, after_ns)
    if not value.valid():
        raise ValueError("native_clock_sample_unconfirmed")
    return value


def inspect_windows_clock():
    """One read-only diagnostic sample; not a sustained-runtime certificate."""
    capability = _capability()
    reading = sample_windows_clock()
    return {"version": "news_review_windows_clock_inspection_v1", **capability,
            "process_id": reading.process_id, "process_creation_utc_ticks": reading.process_creation_utc_ticks,
            "wall_ms": reading.wall_ms, "monotonic_before_ms": reading.monotonic_before_ms,
            "monotonic_after_ms": reading.monotonic_after_ms,
            "monotonic_before_ns": reading.monotonic_before_ns, "monotonic_after_ns": reading.monotonic_after_ns,
            "sampling_span_ms": reading.monotonic_after_ms - reading.monotonic_before_ms,
            "external_utc_verified": False, "actual_suspend_test_performed": False,
            "runtime_integrated": False, "activation_created": False, "request_authorized": False}
