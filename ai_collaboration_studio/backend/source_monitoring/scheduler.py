"""Deterministic due selection and bounded failure backoff.

This module never starts a thread or service.  A host may call ``run_due``
explicitly; the default settings keep that method inert.
"""

from __future__ import annotations

import math
import random
import time
from typing import TYPE_CHECKING, Any, Callable

from .contracts import MAX_NATIVE_INTEGER, SourceMonitoringContractError
from .registry import SourceAdapterRegistry
from .state_repository import SourceMonitoringStateRepository

if TYPE_CHECKING:
    from .supervisor import SourceMonitoringSupervisor


MAX_SCHEDULED_RUNS_PER_CYCLE = 50


class SourceMonitoringScheduleError(SourceMonitoringContractError):
    """Raised when a scheduler input violates the closed local contract."""


def _native_non_negative(value: Any, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_NATIVE_INTEGER:
        raise SourceMonitoringScheduleError(
            "SOURCE_MONITORING_SCHEDULE_INTEGER_INVALID",
            f"{field} must be a non-negative native signed 64-bit integer",
        )
    return value


class BackoffPolicy:
    """Capped exponential backoff with an injected deterministic random source."""

    def __init__(
        self,
        *,
        initial_delay_ms: Any = 30_000,
        maximum_delay_ms: Any = 60 * 60 * 1_000,
        jitter_ratio: Any = 0.20,
        random_source: Callable[[], Any] | None = None,
    ) -> None:
        self.initial_delay_ms = _native_non_negative(
            initial_delay_ms,
            field="initial_delay_ms",
        )
        self.maximum_delay_ms = _native_non_negative(
            maximum_delay_ms,
            field="maximum_delay_ms",
        )
        if self.initial_delay_ms < 1 or self.maximum_delay_ms < self.initial_delay_ms:
            raise SourceMonitoringScheduleError(
                "SOURCE_MONITORING_BACKOFF_RANGE_INVALID",
                "backoff delays must be positive and monotonically bounded",
            )
        if type(jitter_ratio) not in {int, float} or isinstance(jitter_ratio, bool):
            raise SourceMonitoringScheduleError(
                "SOURCE_MONITORING_BACKOFF_JITTER_INVALID",
                "jitter_ratio must be a native finite number",
            )
        ratio = float(jitter_ratio)
        if not math.isfinite(ratio) or not 0 <= ratio <= 0.5:
            raise SourceMonitoringScheduleError(
                "SOURCE_MONITORING_BACKOFF_JITTER_INVALID",
                "jitter_ratio must be between 0 and 0.5",
            )
        if random_source is not None and not callable(random_source):
            raise SourceMonitoringScheduleError(
                "SOURCE_MONITORING_RANDOM_SOURCE_INVALID",
                "random_source must be callable",
            )
        self.jitter_ratio = ratio
        self._random_source = random_source or random.random

    def delay_ms(
        self,
        consecutive_failures: Any,
        *,
        retry_after_ms: Any = 0,
    ) -> int:
        failures = _native_non_negative(
            consecutive_failures,
            field="consecutive_failures",
        )
        retry_after = _native_non_negative(retry_after_ms, field="retry_after_ms")
        if failures < 1:
            raise SourceMonitoringScheduleError(
                "SOURCE_MONITORING_FAILURE_COUNT_INVALID",
                "consecutive_failures must be at least one",
            )
        exponent = min(failures - 1, 62)
        base = min(
            self.maximum_delay_ms,
            self.initial_delay_ms * (1 << exponent),
        )
        random_value = self._random_source()
        if (
            type(random_value) not in {int, float}
            or isinstance(random_value, bool)
            or not math.isfinite(float(random_value))
            or not 0 <= float(random_value) <= 1
        ):
            raise SourceMonitoringScheduleError(
                "SOURCE_MONITORING_RANDOM_VALUE_INVALID",
                "random_source must return a finite native number between 0 and 1",
            )
        jitter_span = int(base * self.jitter_ratio)
        jitter = int(round((2 * float(random_value) - 1) * jitter_span))
        exponential = min(max(0, base + jitter), self.maximum_delay_ms)
        return min(MAX_NATIVE_INTEGER, max(exponential, retry_after))

    def failure_due_at_ms(
        self,
        now_ms: Any,
        consecutive_failures: Any,
        *,
        retry_after_ms: Any = 0,
    ) -> int:
        now = _native_non_negative(now_ms, field="now_ms")
        delay = self.delay_ms(
            consecutive_failures,
            retry_after_ms=retry_after_ms,
        )
        return min(MAX_NATIVE_INTEGER, now + delay)

    @staticmethod
    def success_due_at_ms(now_ms: Any, poll_interval_ms: Any) -> int:
        now = _native_non_negative(now_ms, field="now_ms")
        interval = _native_non_negative(poll_interval_ms, field="poll_interval_ms")
        if interval < 1:
            raise SourceMonitoringScheduleError(
                "SOURCE_MONITORING_POLL_INTERVAL_INVALID",
                "poll_interval_ms must be positive",
            )
        return min(MAX_NATIVE_INTEGER, now + interval)


class SourceMonitoringScheduler:
    """Select due enabled adapters and run one bounded, failure-isolated cycle."""

    def __init__(
        self,
        *,
        registry: SourceAdapterRegistry,
        repository: SourceMonitoringStateRepository,
        supervisor: SourceMonitoringSupervisor,
        clock_ms: Callable[[], Any] | None = None,
    ) -> None:
        if type(registry) is not SourceAdapterRegistry:
            raise SourceMonitoringScheduleError(
                "SOURCE_MONITORING_REGISTRY_INVALID",
                "registry must be SourceAdapterRegistry",
            )
        if type(repository) is not SourceMonitoringStateRepository:
            raise SourceMonitoringScheduleError(
                "SOURCE_MONITORING_REPOSITORY_INVALID",
                "repository must be SourceMonitoringStateRepository",
            )
        self.registry = registry
        self.repository = repository
        self.supervisor = supervisor
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1_000))
        self._ephemeral_due_at_ms: dict[str, int] = {}

    def _now_ms(self) -> int:
        return _native_non_negative(self._clock_ms(), field="scheduler_clock_ms")

    def due_adapter_keys(self) -> tuple[str, ...]:
        settings = self.supervisor.settings
        if not settings.enabled or not settings.auto_start:
            return ()
        now = self._now_ms()
        registered = set(self.registry.adapter_keys)
        due = [
            state["adapter_key"]
            for state in self.repository.list_states()
            if state["enabled"]
            and state["adapter_key"] in registered
            and state["next_due_at_ms"] <= now
            and self._ephemeral_due_at_ms.get(state["adapter_key"], 0) <= now
        ]
        return tuple(sorted(due))

    def run_due(self, *, max_runs: Any = MAX_SCHEDULED_RUNS_PER_CYCLE) -> dict[str, Any]:
        if (
            type(max_runs) is not int
            or not 1 <= max_runs <= MAX_SCHEDULED_RUNS_PER_CYCLE
        ):
            raise SourceMonitoringScheduleError(
                "SOURCE_MONITORING_SCHEDULE_LIMIT_INVALID",
                "max_runs must be a native integer between 1 and 50",
            )
        started_at = self._now_ms()
        results: list[dict[str, Any]] = []
        for adapter_key in self.due_adapter_keys()[:max_runs]:
            boundary_failed = False
            try:
                result = self.supervisor.run_once(adapter_key)
            except Exception as exc:  # isolate one worker boundary from its peers
                boundary_failed = True
                result = {
                    "version": "source_monitoring_run_result_v1",
                    "adapter_key": adapter_key,
                    "status": "FAILED",
                    "error_code": "SOURCE_MONITORING_SCHEDULER_BOUNDARY",
                    "error_message": " ".join(str(exc).split())[:500],
                    "state_recorded": False,
                    "safety": {
                        "execution_capability": "none",
                        "live_trading_allowed": False,
                        "provider_calls_performed": 0,
                        "market_calls_performed": 0,
                    },
                }
            results.append(result)
            if boundary_failed or (
                result.get("status") == "FAILED"
                and result.get("state_recorded") is not True
            ):
                self._ephemeral_due_at_ms[adapter_key] = min(
                    MAX_NATIVE_INTEGER,
                    started_at + self.supervisor.backoff_policy.initial_delay_ms,
                )
            elif result.get("status") in {"DRY_RUN", "DRY_RUN_FAILED"}:
                metadata = self.registry.metadata_for(adapter_key)
                self._ephemeral_due_at_ms[adapter_key] = min(
                    MAX_NATIVE_INTEGER,
                    started_at + metadata.poll_interval_ms,
                )
            else:
                self._ephemeral_due_at_ms.pop(adapter_key, None)
        return {
            "version": "source_monitoring_schedule_cycle_v1",
            "started_at_ms": started_at,
            "completed_at_ms": self._now_ms(),
            "run_count": len(results),
            "results": results,
            "safety": {
                "execution_capability": "none",
                "live_trading_allowed": False,
                "provider_calls_performed": 0,
                "market_calls_performed": 0,
            },
        }


__all__ = [
    "MAX_SCHEDULED_RUNS_PER_CYCLE",
    "BackoffPolicy",
    "SourceMonitoringScheduleError",
    "SourceMonitoringScheduler",
]
