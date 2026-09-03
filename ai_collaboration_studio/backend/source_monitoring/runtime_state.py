"""Thread-safe, process-local liveness for the managed monitoring worker.

Runtime liveness is deliberately not persisted.  A process crash must never
leave a stale SQLite heartbeat that can be mistaken for a live worker.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable

from .contracts import MAX_NATIVE_INTEGER, normalize_adapter_key
from .settings import SourceMonitoringSettings


SOURCE_MONITORING_RUNTIME_HEALTH_VERSION = "source_monitoring_runtime_health_v1"
SOURCE_MONITORING_RUNTIME_STATUSES = (
    "disabled",
    "stopped",
    "starting",
    "running",
    "degraded",
    "stalled",
    "failed",
    "stopping",
)
DEFAULT_RUNTIME_STALL_AFTER_MS = 120_000

_RUNTIME_ID_RE = re.compile(r"source_monitor_runtime_[0-9a-f]{32}\Z")
_ERROR_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,99}\Z")


class SourceMonitoringRuntimeStateError(ValueError):
    """Raised when process-local runtime state violates its closed contract."""


def _native_non_negative(value: Any, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_NATIVE_INTEGER:
        raise SourceMonitoringRuntimeStateError(
            f"{field} must be a non-negative native signed 64-bit integer"
        )
    return value


def _runtime_id(value: Any) -> str:
    if type(value) is not str or _RUNTIME_ID_RE.fullmatch(value) is None:
        raise SourceMonitoringRuntimeStateError("runtime_id is invalid")
    return value


def _error_code(value: Any) -> str:
    if value == "":
        return ""
    if type(value) is not str or _ERROR_CODE_RE.fullmatch(value) is None:
        raise SourceMonitoringRuntimeStateError("runtime error code is invalid")
    return value


class SourceMonitoringRuntimeState:
    """Mutable liveness state whose public projection is bounded and versioned."""

    def __init__(
        self,
        settings: SourceMonitoringSettings,
        *,
        clock_ms: Callable[[], Any] | None = None,
        monotonic_ms: Callable[[], Any] | None = None,
        stall_after_ms: Any = DEFAULT_RUNTIME_STALL_AFTER_MS,
    ) -> None:
        if type(settings) is not SourceMonitoringSettings:
            raise SourceMonitoringRuntimeStateError(
                "settings must be SourceMonitoringSettings"
            )
        if clock_ms is not None and not callable(clock_ms):
            raise SourceMonitoringRuntimeStateError("clock_ms must be callable")
        if monotonic_ms is not None and not callable(monotonic_ms):
            raise SourceMonitoringRuntimeStateError("monotonic_ms must be callable")
        self.settings = settings
        self.stall_after_ms = _native_non_negative(
            stall_after_ms,
            "stall_after_ms",
        )
        if self.stall_after_ms < 1:
            raise SourceMonitoringRuntimeStateError("stall_after_ms must be positive")
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1_000))
        self._monotonic_ms = monotonic_ms or (lambda: int(time.monotonic() * 1_000))
        self._lock = threading.RLock()
        self._status = "disabled" if not settings.enabled else "stopped"
        self._runtime_id = ""
        self._started_at = 0
        self._heartbeat_at = 0
        self._heartbeat_monotonic_at: int | None = None
        self._last_loop_at = 0
        self._active_adapter = ""
        self._next_due_at = 0
        self._last_fatal_error_code = ""

    def _now(self) -> tuple[int, int]:
        return (
            _native_non_negative(self._clock_ms(), "runtime clock"),
            _native_non_negative(
                self._monotonic_ms(),
                "runtime monotonic clock",
            ),
        )

    def _heartbeat_locked(self, epoch_ms: int, monotonic_ms: int) -> None:
        self._heartbeat_at = epoch_ms
        self._heartbeat_monotonic_at = monotonic_ms

    def mark_starting(self, runtime_id: Any) -> None:
        clean_id = _runtime_id(runtime_id)
        epoch_ms, monotonic_ms = self._now()
        with self._lock:
            self._status = "starting"
            self._runtime_id = clean_id
            self._started_at = epoch_ms
            self._last_loop_at = 0
            self._active_adapter = ""
            self._next_due_at = 0
            self._last_fatal_error_code = ""
            self._heartbeat_locked(epoch_ms, monotonic_ms)

    def mark_running(self) -> None:
        epoch_ms, monotonic_ms = self._now()
        with self._lock:
            self._status = "running"
            self._heartbeat_locked(epoch_ms, monotonic_ms)

    def begin_loop(self) -> None:
        epoch_ms, monotonic_ms = self._now()
        with self._lock:
            if self._status not in {"running", "degraded"}:
                raise SourceMonitoringRuntimeStateError(
                    "runtime loop cannot begin outside a live state"
                )
            self._last_loop_at = epoch_ms
            self._heartbeat_locked(epoch_ms, monotonic_ms)

    def set_active_adapter(self, adapter_key: Any) -> None:
        clean_key = "" if adapter_key == "" else normalize_adapter_key(adapter_key)
        epoch_ms, monotonic_ms = self._now()
        with self._lock:
            self._active_adapter = clean_key
            self._heartbeat_locked(epoch_ms, monotonic_ms)

    def complete_loop(self, *, degraded: Any, next_due_at: Any) -> None:
        if type(degraded) is not bool:
            raise SourceMonitoringRuntimeStateError("degraded must be a native boolean")
        clean_due = _native_non_negative(next_due_at, "next_due_at")
        epoch_ms, monotonic_ms = self._now()
        with self._lock:
            self._status = "degraded" if degraded else "running"
            self._last_loop_at = epoch_ms
            self._active_adapter = ""
            self._next_due_at = clean_due
            self._heartbeat_locked(epoch_ms, monotonic_ms)

    def mark_stopping(self) -> None:
        epoch_ms, monotonic_ms = self._now()
        with self._lock:
            if self._status == "disabled":
                return
            self._status = "stopping"
            self._active_adapter = ""
            self._heartbeat_locked(epoch_ms, monotonic_ms)

    def mark_failed(self, error_code: Any) -> None:
        clean_code = _error_code(error_code)
        if not clean_code:
            raise SourceMonitoringRuntimeStateError("failed state requires an error code")
        epoch_ms, monotonic_ms = self._now()
        with self._lock:
            self._status = "failed"
            self._active_adapter = ""
            self._next_due_at = 0
            self._last_fatal_error_code = clean_code
            self._heartbeat_locked(epoch_ms, monotonic_ms)

    def mark_stopped(self) -> None:
        epoch_ms, monotonic_ms = self._now()
        with self._lock:
            self._status = "disabled" if not self.settings.enabled else "stopped"
            self._active_adapter = ""
            self._next_due_at = 0
            self._heartbeat_locked(epoch_ms, monotonic_ms)

    def snapshot(self, *, thread_alive: Any) -> dict[str, Any]:
        if type(thread_alive) is not bool:
            raise SourceMonitoringRuntimeStateError(
                "thread_alive must be a native boolean"
            )
        _epoch_ms, monotonic_ms = self._now()
        with self._lock:
            status = self._status
            heartbeat_monotonic_at = self._heartbeat_monotonic_at
            snapshot = {
                "runtime_id": self._runtime_id,
                "started_at": self._started_at,
                "heartbeat_at": self._heartbeat_at,
                "last_loop_at": self._last_loop_at,
                "active_adapter": self._active_adapter,
                "next_due_at": self._next_due_at,
                "last_fatal_error_code": self._last_fatal_error_code,
            }
        heartbeat_age_ms = (
            0
            if heartbeat_monotonic_at is None
            else max(0, monotonic_ms - heartbeat_monotonic_at)
        )
        active_status = status in {"starting", "running", "degraded", "stopping"}
        if thread_alive and status in {"running", "degraded"}:
            if heartbeat_age_ms > self.stall_after_ms:
                status = "stalled"
        elif not thread_alive and active_status:
            status = "failed"
            if not snapshot["last_fatal_error_code"]:
                snapshot["last_fatal_error_code"] = (
                    "SOURCE_MONITORING_RUNTIME_THREAD_EXITED"
                )
        liveness_verified = bool(
            thread_alive
            and status in {"running", "degraded"}
            and heartbeat_age_ms <= self.stall_after_ms
        )
        return {
            "version": SOURCE_MONITORING_RUNTIME_HEALTH_VERSION,
            "status": status,
            **snapshot,
            "thread_alive": thread_alive,
            "heartbeat_age_ms": heartbeat_age_ms,
            "stall_after_ms": self.stall_after_ms,
            "liveness_verified": liveness_verified,
            "enabled": self.settings.enabled,
            "auto_start": self.settings.auto_start,
            "dry_run": self.settings.dry_run,
            "execution_capability": "none",
            "live_trading_allowed": False,
        }


__all__ = [
    "DEFAULT_RUNTIME_STALL_AFTER_MS",
    "SOURCE_MONITORING_RUNTIME_HEALTH_VERSION",
    "SOURCE_MONITORING_RUNTIME_STATUSES",
    "SourceMonitoringRuntimeState",
    "SourceMonitoringRuntimeStateError",
]
