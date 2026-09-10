"""Deterministic due selection and bounded failure backoff.

This module never starts a thread or service.  A host may call ``run_due``
explicitly; the default settings keep that method inert.
"""

from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from ..source_poll_control import (
    SourcePollCancelled,
    SourcePollDeadlineExceeded,
    ensure_source_poll_active,
    validate_source_poll_control,
)
from .contracts import MAX_NATIVE_INTEGER, SourceMonitoringContractError
from .registry import SourceAdapterRegistry
from .state_repository import SourceMonitoringStateRepository

if TYPE_CHECKING:
    from .supervisor import SourceMonitoringSupervisor


MAX_SCHEDULED_RUNS_PER_CYCLE = 50


class SourceMonitoringScheduleError(SourceMonitoringContractError):
    """Raised when a scheduler input violates the closed local contract."""


class SourceMonitoringSelectionChanged(SourceMonitoringScheduleError):
    """An enabled/configured state changed before its selected run began."""


@dataclass(frozen=True)
class SourceMonitoringRunSelection:
    """One due decision, bound to the state that authorized that decision."""

    adapter_key: str
    config_version: str
    state_version: int
    due_at_ms: int


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

    def _effective_due_by_adapter(self) -> dict[str, int]:
        """Return enabled registered adapters' effective due times without writes.

        Dry-run and boundary-failure cycles intentionally do not advance the
        persisted adapter state.  Their process-local due time therefore has
        to participate in every scheduling projection, not only due
        selection, or a managed worker could repeatedly poll without waiting.
        """

        settings = self.supervisor.settings
        if not settings.enabled or not settings.auto_start:
            return {}
        registered = set(self.registry.adapter_keys)
        return {
            state["adapter_key"]: max(
                state["next_due_at_ms"],
                self._ephemeral_due_at_ms.get(state["adapter_key"], 0),
            )
            for state in self.repository.list_states()
            if state["enabled"] and state["adapter_key"] in registered
        }

    def effective_next_due_at_ms(self) -> int:
        """Return the nearest effective due time, or zero when none is enabled.

        This is a read-only projection.  In particular, it never creates an
        adapter state or changes an adapter's explicit enablement.
        """

        effective = self._effective_due_by_adapter()
        return min(effective.values(), default=0)

    def effective_due_entries(self) -> tuple[tuple[int, str], ...]:
        """Return deterministic ``(due_at_ms, adapter_key)`` entries.

        A multi-pipeline coordinator uses this read-only projection to choose
        one globally earliest adapter without flattening the registries or
        sharing their process-local backoff maps.
        """

        effective = self._effective_due_by_adapter()
        return tuple(
            sorted(
                (due_at_ms, adapter_key)
                for adapter_key, due_at_ms in effective.items()
            )
        )

    def due_adapter_keys(self) -> tuple[str, ...]:
        effective = self._effective_due_by_adapter()
        if not effective:
            return ()
        now = self._now_ms()
        due = [
            adapter_key
            for adapter_key, due_at_ms in effective.items()
            if due_at_ms <= now
        ]
        return tuple(sorted(due))

    def due_run_selections(
        self,
        *,
        now_ms: int | None = None,
    ) -> tuple[SourceMonitoringRunSelection, ...]:
        """Read each candidate's identity and due time from the same snapshot."""

        settings = self.supervisor.settings
        if not settings.enabled or not settings.auto_start:
            return ()
        now = (
            self._now_ms() if now_ms is None
            else _native_non_negative(now_ms, field="now_ms")
        )
        registered = set(self.registry.adapter_keys)
        selections: list[SourceMonitoringRunSelection] = []
        for state in self.repository.list_states():
            key = state["adapter_key"]
            if not state["enabled"] or key not in registered:
                continue
            due_at = max(
                state["next_due_at_ms"], self._ephemeral_due_at_ms.get(key, 0),
            )
            if due_at <= now:
                selections.append(SourceMonitoringRunSelection(
                    adapter_key=key,
                    config_version=state["config_version"],
                    state_version=state["state_version"],
                    due_at_ms=due_at,
                ))
        return tuple(sorted(
            selections, key=lambda selection: selection.adapter_key,
        ))

    @staticmethod
    def _cycle_payload(
        *,
        started_at: int,
        completed_at: int,
        results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        market_call_values = [
            result.get("safety", {}).get("market_calls_performed")
            for result in results
        ]
        market_calls_exact = all(type(value) is int for value in market_call_values)
        market_calls_possible_max = sum(
            int(result.get("safety", {}).get("market_calls_possible_max") or 0)
            for result in results
        )
        return {
            "version": "source_monitoring_schedule_cycle_v1",
            "started_at_ms": started_at,
            "completed_at_ms": completed_at,
            "run_count": len(results),
            "results": results,
            "safety": {
                "execution_capability": "none",
                "live_trading_allowed": False,
                "provider_calls_performed": 0,
                "market_calls_performed": (
                    sum(market_call_values) if market_calls_exact else None
                ),
                "market_calls_accounting": (
                    "exact" if market_calls_exact else "unknown"
                ),
                "market_calls_possible_max": market_calls_possible_max,
            },
        }

    def _run_adapter(
        self,
        selection: SourceMonitoringRunSelection,
        *,
        started_at: int,
        deadline_monotonic_ms: int,
        cancel_event: threading.Event | None,
    ) -> dict[str, Any] | None:
        adapter_key = selection.adapter_key
        boundary_failed = False
        try:
            result = self.supervisor.run_once(
                adapter_key,
                expected_config_version=selection.config_version,
                expected_state_version=selection.state_version,
                deadline_monotonic_ms=deadline_monotonic_ms,
                cancel_event=cancel_event,
            )
        except (SourcePollCancelled, SourcePollDeadlineExceeded):
            raise
        except SourceMonitoringSelectionChanged:
            # No request or run was started. Recompute selection on the next
            # cycle without inventing an adapter failure or failure backoff.
            return None
        except Exception as exc:  # isolate one worker boundary from its peers
            boundary_failed = True
            metadata = self.registry.metadata_for(adapter_key)
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
                    "market_calls_performed": (
                        0 if metadata.max_market_calls_per_poll == 0 else None
                    ),
                    "market_calls_accounting": (
                        "exact"
                        if metadata.max_market_calls_per_poll == 0
                        else "unknown"
                    ),
                    "market_calls_possible_max": (
                        metadata.max_market_calls_per_poll
                    ),
                    "formal_rounds_created": 0,
                },
            }
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
        return result

    def run_one_due(
        self,
        adapter_key: Any,
        *,
        selection: SourceMonitoringRunSelection | None = None,
        deadline_monotonic_ms: Any = 0,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Run one named adapter only when it is currently due.

        The explicit key prevents a coordinator from selecting one adapter and
        the pipeline scheduler silently running a different alphabetic peer.
        A supplied selection keeps its original config/state versions through
        the supervisor's atomic start gate; rechecking due never reselects a key.
        """

        deadline, event = validate_source_poll_control(
            deadline_monotonic_ms=deadline_monotonic_ms,
            cancel_event=cancel_event,
        )
        adapter = self.registry.require(adapter_key)
        clean_adapter_key = adapter.adapter_key
        started_at = self._now_ms()
        ensure_source_poll_active(
            deadline_monotonic_ms=deadline,
            cancel_event=event,
        )
        if selection is None:
            selection = next((
                candidate for candidate in self.due_run_selections(now_ms=started_at)
                if candidate.adapter_key == clean_adapter_key
            ), None)
        elif (
            type(selection) is not SourceMonitoringRunSelection
            or type(selection.adapter_key) is not str
            or selection.adapter_key != clean_adapter_key
            or type(selection.config_version) is not str
            or not selection.config_version
            or type(selection.state_version) is not int
            or not 1 <= selection.state_version <= MAX_NATIVE_INTEGER
            or type(selection.due_at_ms) is not int
            or not 0 <= selection.due_at_ms <= MAX_NATIVE_INTEGER
        ):
            raise SourceMonitoringScheduleError(
                "SOURCE_MONITORING_SELECTION_INVALID",
                "selected run identity is outside the announced adapter boundary",
            )
        current_due = self._effective_due_by_adapter().get(clean_adapter_key)
        if selection is None or current_due is None or current_due > started_at:
            return self._cycle_payload(
                started_at=started_at,
                completed_at=self._now_ms(),
                results=[],
            )
        result = self._run_adapter(
            selection,
            started_at=started_at,
            deadline_monotonic_ms=deadline,
            cancel_event=event,
        )
        return self._cycle_payload(
            started_at=started_at,
            completed_at=self._now_ms(),
            results=[] if result is None else [result],
        )

    def run_due(
        self,
        *,
        max_runs: Any = MAX_SCHEDULED_RUNS_PER_CYCLE,
        deadline_monotonic_ms: Any = 0,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        if (
            type(max_runs) is not int
            or not 1 <= max_runs <= MAX_SCHEDULED_RUNS_PER_CYCLE
        ):
            raise SourceMonitoringScheduleError(
                "SOURCE_MONITORING_SCHEDULE_LIMIT_INVALID",
                "max_runs must be a native integer between 1 and 50",
            )
        deadline, event = validate_source_poll_control(
            deadline_monotonic_ms=deadline_monotonic_ms,
            cancel_event=cancel_event,
        )
        started_at = self._now_ms()
        results: list[dict[str, Any]] = []
        for selection in self.due_run_selections(now_ms=started_at)[:max_runs]:
            ensure_source_poll_active(
                deadline_monotonic_ms=deadline,
                cancel_event=event,
            )
            result = self._run_adapter(
                selection,
                started_at=started_at,
                deadline_monotonic_ms=deadline,
                cancel_event=event,
            )
            if result is not None:
                results.append(result)
        return self._cycle_payload(
            started_at=started_at,
            completed_at=self._now_ms(),
            results=results,
        )


__all__ = [
    "MAX_SCHEDULED_RUNS_PER_CYCLE",
    "BackoffPolicy",
    "SourceMonitoringScheduleError",
    "SourceMonitoringRunSelection",
    "SourceMonitoringSelectionChanged",
    "SourceMonitoringScheduler",
]
