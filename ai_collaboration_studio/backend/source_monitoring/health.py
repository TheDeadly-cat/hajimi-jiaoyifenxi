"""Pure, fail-closed health projections for official source monitoring.

This module performs no persistence, network, Provider, or market operations.
It accepts repository state projections and returns bounded, versioned data for
local status APIs and UI consumers.
"""

from __future__ import annotations

from typing import Any

from .contracts import MAX_NATIVE_INTEGER


SOURCE_ADAPTER_HEALTH_VERSION = "source_adapter_health_v1"
SOURCE_MONITORING_HEALTH_VERSION = "source_monitoring_health_v1"
SOURCE_MONITOR_HEALTH_STATES = (
    "disabled",
    "idle",
    "running",
    "healthy",
    "degraded",
    "backing_off",
    "failed",
)


def _native_non_negative_int(value: Any) -> int:
    return value if type(value) is int and 0 <= value <= MAX_NATIVE_INTEGER else 0


def _valid_native_non_negative_int(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MAX_NATIVE_INTEGER


def _native_text(value: Any, *, maximum: int) -> str:
    return value.strip()[:maximum] if type(value) is str else ""


def project_adapter_health(
    state: Any,
    *,
    now_ms: Any,
    running: Any = False,
) -> dict[str, Any]:
    """Project one persisted adapter state into a bounded health record.

    Invalid native types are never coerced. An enabled record with an invalid
    failure counter or clock is projected as ``failed``; missing timestamps
    remain zero rather than being synthesized.
    """

    state_map = state if type(state) is dict else {}
    enabled = state_map.get("enabled") is True
    running_now = running is True
    now_valid = _valid_native_non_negative_int(now_ms)
    safe_now_ms = now_ms if now_valid else 0

    raw_failures = state_map.get("consecutive_failures")
    failures_valid = _valid_native_non_negative_int(raw_failures)
    consecutive_failures = raw_failures if failures_valid else 0
    persisted_clock_fields = (
        "last_started_at_ms",
        "last_success_at_ms",
        "last_event_at_ms",
        "next_due_at_ms",
    )
    clocks_valid = all(
        _valid_native_non_negative_int(state_map.get(field))
        for field in persisted_clock_fields
    )
    discovery_value = state_map.get("discovery_delay_ms", 0)
    discovery_valid = _valid_native_non_negative_int(discovery_value)
    persisted_state_valid = failures_valid and clocks_valid and discovery_valid
    next_due_at_ms = _native_non_negative_int(state_map.get("next_due_at_ms"))
    last_success_at_ms = _native_non_negative_int(state_map.get("last_success_at_ms"))
    projected_running = enabled and running_now and now_valid and persisted_state_valid

    if not enabled:
        health_state = "disabled"
    elif not persisted_state_valid or not now_valid:
        health_state = "failed"
    elif projected_running:
        health_state = "running"
    elif consecutive_failures >= 5:
        health_state = "failed"
    elif consecutive_failures > 0 and next_due_at_ms > safe_now_ms:
        health_state = "backing_off"
    elif consecutive_failures > 0:
        health_state = "degraded"
    elif last_success_at_ms > 0:
        health_state = "healthy"
    else:
        health_state = "idle"

    return {
        "version": SOURCE_ADAPTER_HEALTH_VERSION,
        "adapter_key": _native_text(state_map.get("adapter_key"), maximum=160),
        "state": health_state,
        "enabled": enabled,
        "running": projected_running,
        "last_checked_at_ms": _native_non_negative_int(
            state_map.get("last_started_at_ms")
        ),
        "last_success_at_ms": last_success_at_ms,
        "last_event_at_ms": _native_non_negative_int(
            state_map.get("last_event_at_ms")
        ),
        "next_due_at_ms": next_due_at_ms,
        "consecutive_failures": consecutive_failures,
        "discovery_delay_ms": _native_non_negative_int(
            state_map.get("discovery_delay_ms")
        ),
        "last_error_code": _native_text(
            state_map.get("last_error_code"),
            maximum=100,
        ),
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


def project_monitoring_health(
    states: Any,
    now_ms: Any,
    running_adapter_keys: Any = (),
) -> dict[str, Any]:
    """Project a deterministic aggregate without performing monitoring work."""

    state_rows = states if type(states) in {list, tuple} else ()
    key_values = (
        running_adapter_keys
        if type(running_adapter_keys) in {list, tuple, set, frozenset}
        else ()
    )
    running_keys = {
        value.strip()
        for value in key_values
        if type(value) is str and value.strip()
    }
    adapters = [
        project_adapter_health(
            state,
            now_ms=now_ms,
            running=(
                type(state) is dict
                and type(state.get("adapter_key")) is str
                and state["adapter_key"].strip() in running_keys
            ),
        )
        for state in state_rows
    ]
    adapters.sort(key=lambda item: item["adapter_key"])

    counts = {
        health_state: sum(
            1 for adapter in adapters if adapter["state"] == health_state
        )
        for health_state in SOURCE_MONITOR_HEALTH_STATES
    }
    projected_states = {adapter["state"] for adapter in adapters}
    if not adapters:
        overall_state = "idle"
    elif projected_states == {"disabled"}:
        overall_state = "disabled"
    elif "failed" in projected_states:
        overall_state = "failed"
    elif "degraded" in projected_states:
        overall_state = "degraded"
    elif "backing_off" in projected_states:
        overall_state = "backing_off"
    elif "running" in projected_states:
        overall_state = "running"
    elif "healthy" in projected_states:
        overall_state = "healthy"
    else:
        overall_state = "idle"

    return {
        "version": SOURCE_MONITORING_HEALTH_VERSION,
        "captured_at_ms": _native_non_negative_int(now_ms),
        "state": overall_state,
        "adapter_count": len(adapters),
        "counts": counts,
        "adapters": adapters,
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


__all__ = [
    "SOURCE_ADAPTER_HEALTH_VERSION",
    "SOURCE_MONITORING_HEALTH_VERSION",
    "SOURCE_MONITOR_HEALTH_STATES",
    "project_adapter_health",
    "project_monitoring_health",
]
