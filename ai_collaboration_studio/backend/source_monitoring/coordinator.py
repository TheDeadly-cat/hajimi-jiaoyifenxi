"""One-worker coordinator for independent monitoring source pipelines."""

from __future__ import annotations

from typing import Any, Callable

from .runtime import (
    DEFAULT_RUNTIME_HEARTBEAT_INTERVAL_MS,
    DEFAULT_RUNTIME_JOIN_TIMEOUT_MS,
    SourceMonitoringRuntime,
    SourceMonitoringRuntimeError,
)
from .runtime_state import DEFAULT_RUNTIME_STALL_AFTER_MS
from .scheduler import SourceMonitoringScheduler
from .settings import SourceMonitoringSettings


OFFICIAL_PIPELINE = "official_source"
READONLY_MARKET_PIPELINE = "readonly_market"
SOURCE_MONITORING_PIPELINE_ORDER = (
    OFFICIAL_PIPELINE,
    READONLY_MARKET_PIPELINE,
)


class SourceMonitoringRuntimeCoordinator(SourceMonitoringRuntime):
    """Coordinate the two sealed registries through one non-daemon worker.

    The base runtime owns the lifecycle, cancellation event, state projection,
    and serial worker loop.  This exact subtype only admits both known pipeline
    names in deterministic order; it never flattens adapters into a mixed
    registry.
    """

    def __init__(
        self,
        *,
        pipeline_schedulers: Any,
        settings: SourceMonitoringSettings,
        heartbeat_interval_ms: Any = DEFAULT_RUNTIME_HEARTBEAT_INTERVAL_MS,
        join_timeout_ms: Any = DEFAULT_RUNTIME_JOIN_TIMEOUT_MS,
        poll_timeout_ms: Any = None,
        clock_ms: Callable[[], Any] | None = None,
        monotonic_ms: Callable[[], Any] | None = None,
        stall_after_ms: Any = DEFAULT_RUNTIME_STALL_AFTER_MS,
        cycle_observer: Callable[[dict[str, Any]], Any] | None = None,
        start_gate: Callable[[], Any] | None = None,
    ) -> None:
        if type(settings) is not SourceMonitoringSettings:
            raise SourceMonitoringRuntimeError(
                "SOURCE_MONITORING_RUNTIME_SETTINGS_INVALID",
                "settings must be SourceMonitoringSettings",
            )
        if (
            settings.official_only is not True
            or settings.allow_readonly_market is not True
        ):
            raise SourceMonitoringRuntimeError(
                "SOURCE_MONITORING_RUNTIME_SETTINGS_MISMATCH",
                "coordinator settings must enable both sealed source pipelines",
            )
        if (
            type(pipeline_schedulers) is not tuple
            or len(pipeline_schedulers) != 2
            or tuple(
                row[0]
                for row in pipeline_schedulers
                if type(row) is tuple and len(row) == 2
            )
            != SOURCE_MONITORING_PIPELINE_ORDER
            or any(
                type(row) is not tuple
                or len(row) != 2
                or type(row[1]) is not SourceMonitoringScheduler
                for row in pipeline_schedulers
            )
        ):
            raise SourceMonitoringRuntimeError(
                "SOURCE_MONITORING_RUNTIME_PIPELINES_INVALID",
                "coordinator requires official_source then readonly_market pipelines",
            )
        official_scheduler = pipeline_schedulers[0][1]
        market_scheduler = pipeline_schedulers[1][1]
        if (
            official_scheduler.registry.official_only is not True
            or market_scheduler.registry.official_only is not False
            or any(
                market_scheduler.registry.metadata_for(adapter_key).official_source
                is not False
                for adapter_key in market_scheduler.registry.adapter_keys
            )
        ):
            raise SourceMonitoringRuntimeError(
                "SOURCE_MONITORING_RUNTIME_PIPELINES_INVALID",
                "coordinator requires an official registry before a read-only market registry",
            )
        expected_official_policy = settings.initialization_policy_for(
            official_source=True,
        )
        expected_market_policy = settings.initialization_policy_for(
            official_source=False,
        )
        if (
            official_scheduler.supervisor.settings.initialization_policy_for(
                official_source=True,
            )
            != expected_official_policy
            or market_scheduler.supervisor.settings.initialization_policy_for(
                official_source=False,
            )
            != expected_market_policy
        ):
            raise SourceMonitoringRuntimeError(
                "SOURCE_MONITORING_RUNTIME_SETTINGS_MISMATCH",
                "pipeline initialization policies must match coordinator policy",
            )
        super().__init__(
            scheduler=official_scheduler,
            settings=settings,
            heartbeat_interval_ms=heartbeat_interval_ms,
            join_timeout_ms=join_timeout_ms,
            poll_timeout_ms=poll_timeout_ms,
            clock_ms=clock_ms,
            monotonic_ms=monotonic_ms,
            stall_after_ms=stall_after_ms,
            cycle_observer=cycle_observer,
            start_gate=start_gate,
            pipeline_schedulers=pipeline_schedulers,
        )


__all__ = [
    "OFFICIAL_PIPELINE",
    "READONLY_MARKET_PIPELINE",
    "SOURCE_MONITORING_PIPELINE_ORDER",
    "SourceMonitoringRuntimeCoordinator",
]
