"""Deterministic, side-effect-free planning for first monitoring polls.

The planner validates the complete adapter result through the Source Inbox
contract before selecting a bounded subset.  It never opens a database,
advances a checkpoint, imports an item, or invokes a provider.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..source_inbox_contracts import accept_source_import
from .adapters.base import SourceAdapterMetadata
from .contracts import AdapterPollResult, SourceMonitoringContractError, canonical_sha256
from .packet_builder import (
    build_packet_from_poll_result,
    build_source_import_packet,
    canonical_source_import_payload,
)
from .settings import SourceMonitoringSettings


SOURCE_MONITORING_INITIAL_PREVIEW_VERSION = (
    "source_monitoring_initial_preview_v1"
)
SOURCE_MONITORING_INITIALIZATION_VERSION = "source_monitoring_initialization_v1"
_PREVIEW_EXTERNAL_RUN_ID = "source_monitoring_initial_preview"


class SourceMonitoringInitializationError(SourceMonitoringContractError):
    """Raised when a first-poll policy cannot be applied safely."""


def _error(code: str, message: str) -> SourceMonitoringInitializationError:
    return SourceMonitoringInitializationError(code, message)


def _timestamp_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000)


def _validate_context(
    result: Any,
    metadata: Any,
    settings: Any,
    *,
    initial_required: Any,
    received_at_ms: Any,
) -> tuple[AdapterPollResult, SourceAdapterMetadata, SourceMonitoringSettings, int]:
    if type(result) is not AdapterPollResult:
        raise _error(
            "SOURCE_MONITORING_POLL_RESULT_INVALID",
            "initial planning requires an exact AdapterPollResult",
        )
    if type(metadata) is not SourceAdapterMetadata:
        raise _error(
            "SOURCE_MONITORING_ADAPTER_METADATA_INVALID",
            "initial planning requires exact sealed adapter metadata",
        )
    if type(settings) is not SourceMonitoringSettings:
        raise _error(
            "SOURCE_MONITORING_SETTINGS_INVALID",
            "initial planning requires exact monitoring settings",
        )
    if type(initial_required) is not bool:
        raise _error(
            "SOURCE_MONITORING_INITIAL_STATE_INVALID",
            "initial_required must be a native boolean",
        )
    if type(received_at_ms) is not int or received_at_ms < result.captured_at_ms:
        raise _error(
            "SOURCE_MONITORING_INITIAL_TIME_INVALID",
            "received_at_ms must be no earlier than the adapter capture time",
        )
    if result.adapter_key != metadata.adapter_key:
        raise _error(
            "SOURCE_MONITORING_ADAPTER_RESULT_MISMATCH",
            "poll result does not match sealed adapter metadata",
        )
    if (
        initial_required
        and metadata.official_source is not True
        and settings.initial_mode != "seed_only"
    ):
        raise _error(
            "SOURCE_MONITORING_MARKET_INITIAL_MODE_FORBIDDEN",
            "read-only market adapters support seed_only initialization only",
        )
    if (
        settings.initial_mode == "from_time"
        and settings.from_time_ms > result.captured_at_ms
    ):
        raise _error(
            "SOURCE_MONITORING_FROM_TIME_FUTURE",
            "from_time cannot be later than the adapter capture time",
        )
    return result, metadata, settings, received_at_ms


@dataclass(frozen=True, slots=True)
class SourceMonitoringInitialPlan:
    """Internal selected items plus a safe, hash-bound public preview."""

    initial_required: bool
    initialization_blocked: bool
    selected_items: tuple[dict[str, Any], ...]
    preview: dict[str, Any]

    def __post_init__(self) -> None:
        if type(self.initial_required) is not bool:
            raise _error(
                "SOURCE_MONITORING_INITIAL_PLAN_INVALID",
                "plan initial_required must be a native boolean",
            )
        if type(self.initialization_blocked) is not bool:
            raise _error(
                "SOURCE_MONITORING_INITIAL_PLAN_INVALID",
                "plan initialization_blocked must be a native boolean",
            )
        if type(self.selected_items) is not tuple or any(
            type(item) is not dict for item in self.selected_items
        ):
            raise _error(
                "SOURCE_MONITORING_INITIAL_PLAN_INVALID",
                "plan selected_items must be a tuple of native dictionaries",
            )
        if type(self.preview) is not dict:
            raise _error(
                "SOURCE_MONITORING_INITIAL_PLAN_INVALID",
                "plan preview must be a native dictionary",
            )
        object.__setattr__(
            self,
            "selected_items",
            tuple(copy.deepcopy(item) for item in self.selected_items),
        )
        object.__setattr__(self, "preview", copy.deepcopy(self.preview))

    def selected_packet(
        self,
        result: AdapterPollResult,
        *,
        external_run_id: str,
        source_channel: str,
        max_items: int,
    ) -> dict[str, Any]:
        cutoff_at_ms = (
            self.preview["from_time_ms"]
            if self.preview["mode"] == "from_time"
            else result.captured_at_ms
        )
        return build_source_import_packet(
            adapter_key=result.adapter_key,
            external_run_id=external_run_id,
            captured_at_ms=result.captured_at_ms,
            cutoff_at_ms=cutoff_at_ms,
            observed_items=self.selected_items,
            source_channel=source_channel,
            max_items=max_items,
        )

    def public_preview(self) -> dict[str, Any]:
        return copy.deepcopy(self.preview)

    def initialization_receipt(self) -> dict[str, Any] | None:
        if not self.initial_required or self.initialization_blocked:
            return None
        preview = self.preview
        return {
            "version": SOURCE_MONITORING_INITIALIZATION_VERSION,
            "mode": preview["mode"],
            "config_version": preview["config_version"],
            "preview_sha256": preview["preview_sha256"],
            "catch_up_max_items": preview["catch_up_max_items"],
            "from_time_ms": preview["from_time_ms"],
            "candidate_count": preview["candidate_count"],
            "selected_count": preview["selected_count"],
            "skipped_count": preview["skipped_count"],
            "adapter_duplicate_count": preview["adapter_duplicate_count"],
            "earliest_occurred_at": preview["earliest_occurred_at"],
            "latest_occurred_at": preview["latest_occurred_at"],
            "starting_checkpoint_sha256": preview[
                "starting_checkpoint_sha256"
            ],
            "next_checkpoint_sha256": preview["next_checkpoint_sha256"],
            "captured_at_ms": preview["captured_at_ms"],
        }


def require_catch_up_confirmation_before_poll(
    settings: SourceMonitoringSettings,
    *,
    initial_required: bool,
) -> None:
    """Reject unconfirmed catch-up before a run row or network poll exists."""

    if (
        initial_required
        and settings.initial_mode == "catch_up"
        and not settings.initial_preview_sha256
    ):
        raise _error(
            "SOURCE_MONITORING_CATCH_UP_PREVIEW_REQUIRED",
            "catch_up requires an explicit preview SHA-256 confirmation",
        )


def require_initial_preview_match(
    plan: SourceMonitoringInitialPlan,
    settings: SourceMonitoringSettings,
) -> None:
    if (
        plan.initial_required
        and settings.initial_mode == "catch_up"
        and plan.preview["preview_sha256"] != settings.initial_preview_sha256
    ):
        raise _error(
            "SOURCE_MONITORING_CATCH_UP_PREVIEW_MISMATCH",
            "catch_up source evidence changed after the confirmed preview",
        )


def plan_initial_poll(
    result: Any,
    *,
    metadata: Any,
    settings: Any,
    initial_required: Any,
    received_at_ms: Any,
) -> SourceMonitoringInitialPlan:
    """Validate all candidates, then deterministically apply initial policy."""

    result, metadata, settings, received_at_ms = _validate_context(
        result,
        metadata,
        settings,
        initial_required=initial_required,
        received_at_ms=received_at_ms,
    )
    full_packet = build_packet_from_poll_result(
        result,
        external_run_id=_PREVIEW_EXTERNAL_RUN_ID,
        max_items=settings.max_items_per_run,
        source_channel=metadata.source_channel,
    )
    full_payload = canonical_source_import_payload(
        full_packet,
        received_at_ms=received_at_ms,
    )
    normalized_packet, _receipt = accept_source_import(
        full_payload,
        received_at_ms=received_at_ms,
    )
    normalized_items = normalized_packet["items"]
    if len(normalized_items) != len(result.observed_items):
        raise _error(
            "SOURCE_MONITORING_INITIAL_PLAN_INVALID",
            "normalized item count does not match the adapter result",
        )

    indexed = [
        {
            "index": index,
            "occurred_at": item["occurred_at"],
            "occurred_at_ms": _timestamp_ms(item["occurred_at"]),
            "fingerprint": item["server_fingerprint"],
        }
        for index, item in enumerate(normalized_items)
    ]
    blocked = bool(
        initial_required and (result.source_errors or result.rejected_count > 0)
    )
    if blocked or (initial_required and settings.initial_mode == "seed_only"):
        selected = []
    elif initial_required and settings.initial_mode == "catch_up":
        selected = sorted(
            indexed,
            key=lambda item: (-item["occurred_at_ms"], item["fingerprint"]),
        )[: settings.catch_up_max_items]
    elif settings.initial_mode == "from_time":
        selected = [
            item
            for item in indexed
            if item["occurred_at_ms"] >= settings.from_time_ms
        ]
    else:
        selected = indexed

    selected_items = tuple(
        copy.deepcopy(result.observed_items[item["index"]]) for item in selected
    )
    candidate_times = [item["occurred_at"] for item in indexed]
    preview_basis = {
        "version": SOURCE_MONITORING_INITIAL_PREVIEW_VERSION,
        "adapter_key": metadata.adapter_key,
        "config_version": metadata.config_version,
        "mode": settings.initial_mode,
        "initial_required": initial_required,
        "initialization_blocked": blocked,
        "catch_up_max_items": settings.catch_up_max_items,
        "from_time_ms": settings.from_time_ms,
        "candidate_count": len(indexed),
        "selected_count": len(selected),
        "skipped_count": len(indexed) - len(selected),
        "adapter_duplicate_count": result.duplicate_count,
        "source_error_count": len(result.source_errors),
        "rejected_count": result.rejected_count,
        "earliest_occurred_at": min(candidate_times) if candidate_times else "",
        "latest_occurred_at": max(candidate_times) if candidate_times else "",
        "candidate_fingerprints": sorted(
            item["fingerprint"] for item in indexed
        ),
        "selected_fingerprints": [item["fingerprint"] for item in selected],
        "starting_checkpoint_sha256": canonical_sha256(
            result.started_checkpoint
        ),
        "next_checkpoint_sha256": canonical_sha256(result.next_checkpoint),
        "safety": {
            "database_writes_performed": 0,
            "checkpoint_writes_performed": 0,
            "source_inbox_writes_performed": 0,
            "provider_calls_performed": 0,
            "execution_capability": "none",
            "live_trading_allowed": False,
        },
    }
    preview = {
        **preview_basis,
        "captured_at_ms": result.captured_at_ms,
        "preview_sha256": canonical_sha256(preview_basis),
    }
    plan = SourceMonitoringInitialPlan(
        initial_required=initial_required,
        initialization_blocked=blocked,
        selected_items=selected_items,
        preview=preview,
    )
    return plan


__all__ = [
    "SOURCE_MONITORING_INITIALIZATION_VERSION",
    "SOURCE_MONITORING_INITIAL_PREVIEW_VERSION",
    "SourceMonitoringInitialPlan",
    "SourceMonitoringInitializationError",
    "plan_initial_poll",
    "require_catch_up_confirmation_before_poll",
    "require_initial_preview_match",
]
