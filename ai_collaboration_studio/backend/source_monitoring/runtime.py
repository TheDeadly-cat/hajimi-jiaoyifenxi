"""Managed, fail-closed worker for source-monitoring scheduling.

Construction is inert: no thread is created, no adapter is enabled, and no
repository or source is touched until an authorized ``start`` call.  The host
owns the runtime lifecycle and must confirm the worker is stopped before it
releases the database instance owner.
"""

from __future__ import annotations

import copy
import math
import threading
import time
import uuid
from typing import Any, Callable

from ..source_inbox_service import SourceInboxService
from ..store import StudioStore
from .contracts import MAX_NATIVE_INTEGER, SourceMonitoringContractError
from .default_registry import (
    build_futu_anomaly_registry,
    build_official_source_registry,
)
from .runtime_state import (
    DEFAULT_RUNTIME_STALL_AFTER_MS,
    SourceMonitoringRuntimeState,
)
from .scheduler import SourceMonitoringScheduler
from .settings import SourceMonitoringSettings, load_source_monitoring_settings
from .state_repository import SourceMonitoringStateRepository
from .supervisor import SourceMonitoringSupervisor
from .trading_impact_rules import TradingImpactRulesV1


DEFAULT_RUNTIME_HEARTBEAT_INTERVAL_MS = 5_000
DEFAULT_RUNTIME_JOIN_TIMEOUT_MS = 30_000

SOURCE_MONITORING_RUNTIME_FATAL = "SOURCE_MONITORING_RUNTIME_FATAL"
SOURCE_MONITORING_RUNTIME_INITIALIZE_FAILED = (
    "SOURCE_MONITORING_RUNTIME_INITIALIZE_FAILED"
)
SOURCE_MONITORING_RUNTIME_THREAD_START_FAILED = (
    "SOURCE_MONITORING_RUNTIME_THREAD_START_FAILED"
)
SOURCE_MONITORING_RUNTIME_OBSERVER_FAILED = (
    "SOURCE_MONITORING_RUNTIME_OBSERVER_FAILED"
)
SOURCE_MONITORING_RUNTIME_START_GATE_FAILED = (
    "SOURCE_MONITORING_RUNTIME_START_GATE_FAILED"
)
SOURCE_MONITORING_RUNTIME_CYCLE_OBSERVATION_VERSION = (
    "source_monitoring_runtime_cycle_observation_v1"
)

_SUCCESSFUL_RUN_STATUSES = frozenset({"SUCCEEDED", "DRY_RUN"})
_OBSERVABLE_RUN_STATUSES = frozenset({
    "SUCCEEDED",
    "DRY_RUN",
    "DEGRADED",
    "FAILED",
    "DRY_RUN_FAILED",
})


class SourceMonitoringRuntimeError(SourceMonitoringContractError):
    """Raised when a managed-runtime input violates its local contract."""


def _positive_milliseconds(value: Any, *, field: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_NATIVE_INTEGER:
        raise SourceMonitoringRuntimeError(
            "SOURCE_MONITORING_RUNTIME_INTERVAL_INVALID",
            f"{field} must be a positive native signed 64-bit integer",
        )
    return value


def _join_timeout_seconds(value: Any) -> float | None:
    if value is None:
        return None
    if (
        type(value) not in {int, float}
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= MAX_NATIVE_INTEGER / 1_000
    ):
        raise SourceMonitoringRuntimeError(
            "SOURCE_MONITORING_RUNTIME_JOIN_TIMEOUT_INVALID",
            "join timeout must be None or a finite non-negative number of seconds",
        )
    return float(value)


class SourceMonitoringRuntime:
    """Own one non-daemon worker and serialize all due adapter runs."""

    def __init__(
        self,
        *,
        scheduler: SourceMonitoringScheduler,
        settings: SourceMonitoringSettings,
        heartbeat_interval_ms: Any = DEFAULT_RUNTIME_HEARTBEAT_INTERVAL_MS,
        join_timeout_ms: Any = DEFAULT_RUNTIME_JOIN_TIMEOUT_MS,
        clock_ms: Callable[[], Any] | None = None,
        monotonic_ms: Callable[[], Any] | None = None,
        stall_after_ms: Any = DEFAULT_RUNTIME_STALL_AFTER_MS,
        cycle_observer: Callable[[dict[str, Any]], Any] | None = None,
        start_gate: Callable[[], Any] | None = None,
    ) -> None:
        if type(scheduler) is not SourceMonitoringScheduler:
            raise SourceMonitoringRuntimeError(
                "SOURCE_MONITORING_RUNTIME_SCHEDULER_INVALID",
                "scheduler must be SourceMonitoringScheduler",
            )
        if type(settings) is not SourceMonitoringSettings:
            raise SourceMonitoringRuntimeError(
                "SOURCE_MONITORING_RUNTIME_SETTINGS_INVALID",
                "settings must be SourceMonitoringSettings",
            )
        if scheduler.supervisor.settings != settings:
            raise SourceMonitoringRuntimeError(
                "SOURCE_MONITORING_RUNTIME_SETTINGS_MISMATCH",
                "runtime and scheduler settings must match",
            )
        if clock_ms is not None and not callable(clock_ms):
            raise SourceMonitoringRuntimeError(
                "SOURCE_MONITORING_RUNTIME_CLOCK_INVALID",
                "clock_ms must be callable",
            )
        if monotonic_ms is not None and not callable(monotonic_ms):
            raise SourceMonitoringRuntimeError(
                "SOURCE_MONITORING_RUNTIME_CLOCK_INVALID",
                "monotonic_ms must be callable",
            )
        if cycle_observer is not None and not callable(cycle_observer):
            raise SourceMonitoringRuntimeError(
                "SOURCE_MONITORING_RUNTIME_OBSERVER_INVALID",
                "cycle_observer must be callable",
            )
        if start_gate is not None and not callable(start_gate):
            raise SourceMonitoringRuntimeError(
                "SOURCE_MONITORING_RUNTIME_START_GATE_INVALID",
                "start_gate must be callable",
            )

        self.scheduler = scheduler
        self.settings = settings
        self.heartbeat_interval_ms = _positive_milliseconds(
            heartbeat_interval_ms,
            field="heartbeat_interval_ms",
        )
        self.join_timeout_ms = _positive_milliseconds(
            join_timeout_ms,
            field="join_timeout_ms",
        )
        self._clock_ms = clock_ms or scheduler._clock_ms
        self.state = SourceMonitoringRuntimeState(
            settings,
            clock_ms=self._clock_ms,
            monotonic_ms=monotonic_ms,
            stall_after_ms=stall_after_ms,
        )
        self._lifecycle_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._degraded_adapter_keys: set[str] = set()
        self._cycle_observer = cycle_observer
        self._start_gate = start_gate

    def _now_ms(self) -> int:
        value = self._clock_ms()
        if type(value) is not int or not 0 <= value <= MAX_NATIVE_INTEGER:
            raise SourceMonitoringRuntimeError(
                "SOURCE_MONITORING_RUNTIME_CLOCK_INVALID",
                "runtime clock must return a non-negative native integer",
            )
        return value

    def _safe_mark_failed(self, error_code: str) -> None:
        try:
            self.state.mark_failed(error_code)
        except BaseException:
            # State projection will still detect a dead formerly-live thread.
            pass

    def start(self) -> bool:
        """Start once when globally enabled and explicitly authorized to auto-start.

        Initialization is synchronous and occurs before thread creation.  All
        fatal failures are reduced to a bounded state code and are not raised
        into the HTTP host.
        """

        with self._lifecycle_lock:
            current = self._thread
            if current is not None and current.is_alive():
                return False
            if not self.settings.enabled or not self.settings.auto_start:
                return False

            self._stop_event.clear()
            self._degraded_adapter_keys.clear()
            runtime_id = f"source_monitor_runtime_{uuid.uuid4().hex}"
            try:
                self.state.mark_starting(runtime_id)
                self.scheduler.supervisor.initialize()
            except BaseException:
                self._safe_mark_failed(
                    SOURCE_MONITORING_RUNTIME_INITIALIZE_FAILED
                )
                return False

            try:
                worker = threading.Thread(
                    target=self._worker_main,
                    name="source-monitoring-runtime",
                    daemon=False,
                )
                self._thread = worker
                worker.start()
            except BaseException:
                self._thread = None
                self._safe_mark_failed(
                    SOURCE_MONITORING_RUNTIME_THREAD_START_FAILED
                )
                return False
            return True

    def stop(self) -> bool:
        """Signal shutdown, wait for the configured bound, and report quiescence."""

        with self._lifecycle_lock:
            worker = self._thread
            if worker is None or not worker.is_alive():
                return True
            try:
                self.state.mark_stopping()
            except BaseException:
                self._safe_mark_failed(SOURCE_MONITORING_RUNTIME_FATAL)
            self._stop_event.set()
        return self.join(self.join_timeout_ms / 1_000)

    def join(self, timeout: Any = None) -> bool:
        """Wait for the current worker; ``None`` intentionally means unbounded."""

        clean_timeout = _join_timeout_seconds(timeout)
        with self._lifecycle_lock:
            worker = self._thread
        if worker is None or not worker.is_alive():
            return True
        if worker is threading.current_thread():
            return False
        worker.join(clean_timeout)
        return not worker.is_alive()

    def wait_until_stopped(self, timeout: Any = None) -> bool:
        """Alias for hosts that express shutdown in lifecycle terminology."""

        return self.join(timeout)

    def snapshot(self) -> dict[str, Any]:
        """Return liveness using a fresh probe of the managed thread object."""

        with self._lifecycle_lock:
            worker = self._thread
            thread_alive = bool(worker is not None and worker.is_alive())
        return self.state.snapshot(thread_alive=thread_alive)

    @staticmethod
    def _cycle_results(cycle: Any) -> list[dict[str, Any]]:
        if type(cycle) is not dict:
            raise SourceMonitoringRuntimeError(
                "SOURCE_MONITORING_RUNTIME_CYCLE_INVALID",
                "scheduler cycle must be an object",
            )
        run_count = cycle.get("run_count")
        results = cycle.get("results")
        if (
            type(run_count) is not int
            or not 0 <= run_count <= 1
            or type(results) is not list
            or len(results) != run_count
            or any(type(result) is not dict for result in results)
        ):
            raise SourceMonitoringRuntimeError(
                "SOURCE_MONITORING_RUNTIME_CYCLE_INVALID",
                "scheduler cycle is outside the one-run runtime contract",
            )
        return results

    def _record_cycle_results(
        self,
        active_adapter: str,
        cycle: Any,
    ) -> int:
        results = self._cycle_results(cycle)
        for result in results:
            adapter_key = result.get("adapter_key")
            status = result.get("status")
            if type(adapter_key) is not str or adapter_key != active_adapter:
                raise SourceMonitoringRuntimeError(
                    "SOURCE_MONITORING_RUNTIME_CYCLE_INVALID",
                    "scheduler ran an adapter outside the announced serial boundary",
                )
            if status in _SUCCESSFUL_RUN_STATUSES:
                self._degraded_adapter_keys.discard(adapter_key)
            else:
                self._degraded_adapter_keys.add(adapter_key)
        return len(results)

    def _observe_cycle(self, active_adapter: str, cycle: Any) -> None:
        observer = self._cycle_observer
        if observer is None:
            return
        results = self._cycle_results(cycle)
        for result in results:
            run_id = result.get("run_id")
            status = result.get("status")
            state_recorded = result.get("state_recorded")
            safety = result.get("safety")
            source_inbox_writes = result.get(
                "source_inbox_writes_performed"
            )
            if (
                type(run_id) is not str
                or not run_id
                or len(run_id) > 200
                or type(status) is not str
                or status not in _OBSERVABLE_RUN_STATUSES
                or type(state_recorded) is not bool
                or type(safety) is not dict
                or type(safety.get("execution_capability")) is not str
                or safety.get("execution_capability") != "none"
                or safety.get("live_trading_allowed") is not False
                or type(safety.get("provider_calls_performed")) is not int
                or safety.get("provider_calls_performed") != 0
                or type(safety.get("formal_rounds_created")) is not int
                or safety.get("formal_rounds_created") != 0
                or not (
                    source_inbox_writes is None
                    or type(source_inbox_writes) is bool
                )
            ):
                raise SourceMonitoringRuntimeError(
                    "SOURCE_MONITORING_RUNTIME_CYCLE_INVALID",
                    "scheduler result is unsafe for cycle observation",
                )
            market_calls = safety.get("market_calls_performed")
            market_calls_max = safety.get("market_calls_possible_max")
            if (
                (
                    market_calls is not None
                    and (type(market_calls) is not int or market_calls < 0)
                )
                or type(market_calls_max) is not int
                or market_calls_max < 0
                or (
                    type(market_calls) is int
                    and market_calls > market_calls_max
                )
            ):
                raise SourceMonitoringRuntimeError(
                    "SOURCE_MONITORING_RUNTIME_CYCLE_INVALID",
                    "scheduler market-call evidence is invalid",
                )
            runtime = self.state.snapshot(thread_alive=True)
            observation = {
                "version": SOURCE_MONITORING_RUNTIME_CYCLE_OBSERVATION_VERSION,
                "runtime_id": runtime["runtime_id"],
                "adapter_key": active_adapter,
                "run_id": run_id,
                "status": status,
                "state_recorded": state_recorded,
                "source_inbox_writes_performed": source_inbox_writes,
                "market_calls_performed": market_calls,
                "market_calls_possible_max": market_calls_max,
                "provider_calls_performed": 0,
                "formal_rounds_created": 0,
                "execution_capability": "none",
                "live_trading_allowed": False,
            }
            try:
                observer(copy.deepcopy(observation))
            except BaseException as exc:
                raise SourceMonitoringRuntimeError(
                    SOURCE_MONITORING_RUNTIME_OBSERVER_FAILED,
                    "cycle observer failed closed",
                ) from exc

    def _wait_delay_ms(self, *, next_due_at_ms: int, ran_count: int) -> int:
        if next_due_at_ms == 0:
            return self.heartbeat_interval_ms
        now_ms = self._now_ms()
        if next_due_at_ms <= now_ms:
            # A completed run may leave another adapter immediately due.  That
            # is useful work rather than an idle spin, so drain it serially.
            # If the scheduler reported no work despite an overdue projection,
            # use the full heartbeat wait so a transient mismatch cannot turn
            # into a millisecond polling loop.
            return 0 if ran_count else self.heartbeat_interval_ms
        return min(self.heartbeat_interval_ms, next_due_at_ms - now_ms)

    def _worker_main(self) -> None:
        try:
            if self._start_gate is not None:
                try:
                    gate_result = self._start_gate()
                    if gate_result is not None:
                        raise ValueError("start gate returned a value")
                except BaseException as exc:
                    raise SourceMonitoringRuntimeError(
                        SOURCE_MONITORING_RUNTIME_START_GATE_FAILED,
                        "runtime start gate failed closed",
                    ) from exc
            self.state.mark_running()
            while not self._stop_event.is_set():
                try:
                    self.state.begin_loop()
                except BaseException:
                    if self._stop_event.is_set():
                        break
                    raise
                if self._stop_event.is_set():
                    break

                due_adapter_keys = self.scheduler.due_adapter_keys()
                if self._stop_event.is_set():
                    break
                active_adapter = (
                    due_adapter_keys[0] if due_adapter_keys else ""
                )
                ran_count = 0
                if active_adapter:
                    self.state.set_active_adapter(active_adapter)
                    try:
                        if self._stop_event.is_set():
                            break
                        cycle = self.scheduler.run_due(max_runs=1)
                        ran_count = self._record_cycle_results(
                            active_adapter,
                            cycle,
                        )
                        self._observe_cycle(active_adapter, cycle)
                    finally:
                        self.state.set_active_adapter("")
                if self._stop_event.is_set():
                    break

                next_due_at_ms = self.scheduler.effective_next_due_at_ms()
                self.state.complete_loop(
                    degraded=bool(self._degraded_adapter_keys),
                    next_due_at=next_due_at_ms,
                )
                wait_ms = self._wait_delay_ms(
                    next_due_at_ms=next_due_at_ms,
                    ran_count=ran_count,
                )
                if wait_ms:
                    self._stop_event.wait(wait_ms / 1_000)
        except BaseException as exc:
            fatal_code = SOURCE_MONITORING_RUNTIME_FATAL
            if isinstance(exc, SourceMonitoringRuntimeError) and exc.code in {
                SOURCE_MONITORING_RUNTIME_OBSERVER_FAILED,
                SOURCE_MONITORING_RUNTIME_START_GATE_FAILED,
            }:
                fatal_code = exc.code
            self._safe_mark_failed(fatal_code)
            return

        try:
            self.state.mark_stopped()
        except BaseException:
            self._safe_mark_failed(SOURCE_MONITORING_RUNTIME_FATAL)


def build_source_monitoring_runtime(
    store: StudioStore,
    settings: SourceMonitoringSettings | None = None,
    *,
    heartbeat_interval_ms: Any = DEFAULT_RUNTIME_HEARTBEAT_INTERVAL_MS,
    join_timeout_ms: Any = DEFAULT_RUNTIME_JOIN_TIMEOUT_MS,
    clock_ms: Callable[[], Any] | None = None,
    monotonic_ms: Callable[[], Any] | None = None,
    stall_after_ms: Any = DEFAULT_RUNTIME_STALL_AFTER_MS,
    cycle_observer: Callable[[dict[str, Any]], Any] | None = None,
    start_gate: Callable[[], Any] | None = None,
) -> SourceMonitoringRuntime:
    """Construct the production runtime graph without database or source I/O."""

    resolved_settings = (
        load_source_monitoring_settings() if settings is None else settings
    )
    if type(resolved_settings) is not SourceMonitoringSettings:
        raise SourceMonitoringRuntimeError(
            "SOURCE_MONITORING_RUNTIME_SETTINGS_INVALID",
            "settings must be SourceMonitoringSettings",
        )

    registry = (
        build_official_source_registry()
        if resolved_settings.official_only
        else build_futu_anomaly_registry()
    )
    repository = SourceMonitoringStateRepository(store, clock_ms=clock_ms)
    source_inbox = (
        SourceInboxService(store)
        if clock_ms is None
        else SourceInboxService(store, clock=lambda: clock_ms() / 1_000)
    )
    impact_rules = (
        TradingImpactRulesV1()
        if resolved_settings.trading_impact_rules_enabled
        else None
    )
    supervisor = SourceMonitoringSupervisor(
        registry=registry,
        repository=repository,
        source_inbox=source_inbox,
        settings=resolved_settings,
        clock_ms=clock_ms,
        impact_rules=impact_rules,
    )
    scheduler = SourceMonitoringScheduler(
        registry=registry,
        repository=repository,
        supervisor=supervisor,
        clock_ms=clock_ms,
    )
    return SourceMonitoringRuntime(
        scheduler=scheduler,
        settings=resolved_settings,
        heartbeat_interval_ms=heartbeat_interval_ms,
        join_timeout_ms=join_timeout_ms,
        clock_ms=clock_ms,
        monotonic_ms=monotonic_ms,
        stall_after_ms=stall_after_ms,
        cycle_observer=cycle_observer,
        start_gate=start_gate,
    )


__all__ = [
    "DEFAULT_RUNTIME_HEARTBEAT_INTERVAL_MS",
    "DEFAULT_RUNTIME_JOIN_TIMEOUT_MS",
    "SOURCE_MONITORING_RUNTIME_FATAL",
    "SOURCE_MONITORING_RUNTIME_INITIALIZE_FAILED",
    "SOURCE_MONITORING_RUNTIME_CYCLE_OBSERVATION_VERSION",
    "SOURCE_MONITORING_RUNTIME_OBSERVER_FAILED",
    "SOURCE_MONITORING_RUNTIME_START_GATE_FAILED",
    "SOURCE_MONITORING_RUNTIME_THREAD_START_FAILED",
    "SourceMonitoringRuntime",
    "SourceMonitoringRuntimeError",
    "build_source_monitoring_runtime",
]
