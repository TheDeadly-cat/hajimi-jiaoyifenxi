"""Deterministic, side-effect-free planning for first monitoring polls.

The planner validates the complete adapter result through the Source Inbox
contract before selecting a bounded subset.  It never opens a database,
advances a checkpoint, imports an item, or invokes a provider.
"""

from __future__ import annotations

import copy
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..source_inbox_contracts import accept_source_import
from ..source_poll_control import ensure_source_poll_active
from .adapters.base import SourceAdapterMetadata
from .contracts import AdapterPollResult, SourceMonitoringContractError, canonical_sha256
from .packet_builder import (
    build_packet_from_poll_result,
    build_source_import_packet,
    canonical_source_import_payload,
)
from .settings import (
    SourceMonitoringInitializationPolicy,
    SourceMonitoringSettings,
)


SOURCE_MONITORING_INITIAL_PREVIEW_VERSION = (
    "source_monitoring_initial_preview_v1"
)
SOURCE_MONITORING_INITIALIZATION_VERSION = "source_monitoring_initialization_v1"
SOURCE_MONITORING_INITIALIZATION_VERSION_V2 = "source_monitoring_initialization_v2"
SOURCE_MONITORING_STATIC_SEED_PREVIEW_VERSION = (
    "source_monitoring_static_seed_preview_v1"
)
_PREVIEW_EXTERNAL_RUN_ID = "source_monitoring_initial_preview"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class SourceMonitoringInitializationError(SourceMonitoringContractError):
    """Raised when a first-poll policy cannot be applied safely."""


def _error(code: str, message: str) -> SourceMonitoringInitializationError:
    return SourceMonitoringInitializationError(code, message)


def poll_for_initialization(
    adapter: Any,
    checkpoint: dict[str, Any],
    *,
    initial_required: bool,
    initialization_policy: SourceMonitoringInitializationPolicy,
    observed_at_ms: int,
    deadline_monotonic_ms: int = 0,
    cancel_event: threading.Event | None = None,
    etag: str = "",
    last_modified: str = "",
    max_items: int = 50,
) -> AdapterPollResult:
    """Classify the complete bounded SEC/IR history before any initial delivery."""

    if type(initialization_policy) is not SourceMonitoringInitializationPolicy:
        raise _error("SOURCE_MONITORING_INITIAL_POLICY_INVALID", "initial polling requires an exact effective policy")
    poll = adapter.poll
    extra = {}
    if (
        initial_required
        and adapter.adapter_key in {"sec_filings", "company_ir"}
    ):
        seed_only = initialization_policy.mode == "seed_only"
        poll = getattr(adapter, "poll_seed_baseline" if seed_only else "poll_initial_history", None)
        if not callable(poll):
            raise _error(
                "SEC_BASELINE_SCOPE_INCOMPLETE" if adapter.adapter_key == "sec_filings" else "COMPANY_IR_BASELINE_SCOPE_INCOMPLETE",
                "Official initialization requires the bounded complete-history capability",
            )
        if not seed_only:
            extra["initialization_policy"] = initialization_policy
    result = poll(
        checkpoint,
        observed_at_ms=observed_at_ms,
        deadline_monotonic_ms=deadline_monotonic_ms,
        cancel_event=cancel_event,
        etag=etag,
        last_modified=last_modified,
        max_items=max_items,
        **extra,
    )
    if extra and type(result) is AdapterPollResult and not (
        result.source_errors or result.rejected_count or result.initial_history_sha256
    ):
        raise _error("SOURCE_MONITORING_INITIAL_HISTORY_INVALID", "initial official history result is missing its complete scope seal")
    return result


def _timestamp_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000)


def _resolve_initialization_policy(
    metadata: SourceAdapterMetadata,
    *,
    settings: Any = None,
    initialization_policy: Any = None,
) -> SourceMonitoringInitializationPolicy:
    if settings is not None and initialization_policy is not None:
        raise _error(
            "SOURCE_MONITORING_INITIAL_POLICY_INVALID",
            "provide settings or initialization_policy, not both",
        )
    if initialization_policy is not None:
        if type(initialization_policy) is not SourceMonitoringInitializationPolicy:
            raise _error(
                "SOURCE_MONITORING_INITIAL_POLICY_INVALID",
                "initialization_policy must be an exact effective policy",
            )
        return initialization_policy
    if type(settings) is not SourceMonitoringSettings:
        raise _error(
            "SOURCE_MONITORING_SETTINGS_INVALID",
            "initial planning requires exact monitoring settings",
        )
    return settings.initialization_policy_for(
        official_source=metadata.official_source,
    )


def normalize_initial_seed_policy(
    value: Any,
    *,
    metadata: SourceAdapterMetadata,
) -> dict[str, Any]:
    """Validate the closed, adapter-owned static seed policy manifest."""

    expected_fields = {
        "version",
        "adapter_key",
        "config_version",
        "adapter_config_sha256",
        "broker_policy_sha256",
        "initial_mode",
        "source_policy_sha256",
        "symbol_allowlist",
        "execution_capability",
        "live_trading_allowed",
    }
    if type(value) is not dict or set(value) != expected_fields:
        raise _error(
            "SOURCE_MONITORING_INITIAL_SEED_POLICY_INVALID",
            "initial seed policy does not match the closed v1 projection",
        )
    if (
        value.get("version") != "source_monitoring_initial_seed_policy_v1"
        or value.get("adapter_key") != metadata.adapter_key
        or value.get("config_version") != metadata.config_version
        or value.get("initial_mode") != "seed_only"
        or value.get("execution_capability") != "none"
        or value.get("live_trading_allowed") is not False
        or type(value.get("adapter_config_sha256")) is not str
        or _SHA256_RE.fullmatch(value["adapter_config_sha256"]) is None
        or type(value.get("source_policy_sha256")) is not str
        or _SHA256_RE.fullmatch(value["source_policy_sha256"]) is None
        or type(value.get("broker_policy_sha256")) is not str
        or (
            value["broker_policy_sha256"] != ""
            and _SHA256_RE.fullmatch(value["broker_policy_sha256"]) is None
        )
    ):
        raise _error(
            "SOURCE_MONITORING_INITIAL_SEED_POLICY_INVALID",
            "initial seed policy identity or safety boundary is invalid",
        )
    symbols = value.get("symbol_allowlist")
    if (
        type(symbols) is not list
        or not symbols
        or len(symbols) > 50
        or any(
            type(symbol) is not str
            or not symbol
            or symbol != symbol.strip()
            or len(symbol) > 32
            for symbol in symbols
        )
        or len(symbols) != len(set(symbols))
    ):
        raise _error(
            "SOURCE_MONITORING_INITIAL_SEED_POLICY_INVALID",
            "initial seed policy symbol allowlist is invalid",
        )
    unsigned = {
        key: copy.deepcopy(item)
        for key, item in value.items()
        if key != "source_policy_sha256"
    }
    if canonical_sha256(unsigned) != value["source_policy_sha256"]:
        raise _error(
            "SOURCE_MONITORING_INITIAL_SEED_POLICY_INVALID",
            "initial seed policy self-seal is invalid",
        )
    return copy.deepcopy(value)


def build_static_seed_preview(
    *,
    metadata: Any,
    initialization_policy: Any,
    initial_seed_policy: Any,
    starting_checkpoint: Any,
) -> dict[str, Any]:
    """Build a zero-poll authorization preview for a read-only market seed."""

    if type(metadata) is not SourceAdapterMetadata:
        raise _error(
            "SOURCE_MONITORING_ADAPTER_METADATA_INVALID",
            "static seed preview requires exact adapter metadata",
        )
    if type(initialization_policy) is not SourceMonitoringInitializationPolicy:
        raise _error(
            "SOURCE_MONITORING_INITIAL_POLICY_INVALID",
            "static seed preview requires an exact effective policy",
        )
    if metadata.official_source is not False or initialization_policy.mode != "seed_only":
        raise _error(
            "SOURCE_MONITORING_STATIC_SEED_POLICY_FORBIDDEN",
            "static seed preview is reserved for read-only market seed policy",
        )
    checkpoint_sha256 = canonical_sha256(starting_checkpoint)
    seed_policy = normalize_initial_seed_policy(
        initial_seed_policy,
        metadata=metadata,
    )
    basis = {
        "version": SOURCE_MONITORING_STATIC_SEED_PREVIEW_VERSION,
        "preview_kind": "static_seed_policy",
        "adapter_key": metadata.adapter_key,
        "config_version": metadata.config_version,
        "source_class": metadata.source_class,
        "source_channel": metadata.source_channel,
        "mode": "seed_only",
        "starting_checkpoint_sha256": checkpoint_sha256,
        "initial_seed_policy": seed_policy,
        "safety": {
            "provider_calls_performed": 0,
            "model_calls_performed": 0,
            "market_calls_performed": 0,
            "execution_capability": "none",
            "live_trading_allowed": False,
        },
    }
    return {
        **basis,
        "source_policy_sha256": seed_policy["source_policy_sha256"],
        "symbol_allowlist": copy.deepcopy(seed_policy["symbol_allowlist"]),
        "preview_sha256": canonical_sha256(basis),
    }


def _is_revision_item(item: dict[str, Any]) -> bool:
    extensions = item.get("extensions")
    if type(extensions) is not dict:
        return False
    macro = extensions.get("macro_official_v1")
    if type(macro) is dict and macro.get("event_state") == "revised":
        return True
    company_ir = extensions.get("company_ir_v2", extensions.get("company_ir_v1"))
    return bool(
        type(company_ir) is dict and company_ir.get("is_revision") is True
    )


def _cutoff_time_ms(item: dict[str, Any], *, captured_at_ms: int) -> int:
    """Use poll observation time for revisions without mutating imported items."""

    if _is_revision_item(item):
        return captured_at_ms
    return _timestamp_ms(item["occurred_at"])


def select_initial_history(
    items: list[dict[str, Any]],
    *,
    adapter_key: str,
    policy: SourceMonitoringInitializationPolicy,
    captured_at_ms: int,
    deadline_monotonic_ms: int = 0,
    cancel_event: threading.Event | None = None,
) -> tuple[tuple[int, ...], str]:
    """Validate one bounded history and seal its complete authorized subset.

    This is classification, not a larger delivery packet. Adapters retain
    excluded identities, emit at their ordinary limits, and leave authorized
    but undelivered identities unseen for subsequent polls. No cutoff survives
    initialization unless the operator separately configured a continuous one.
    """

    if (
        type(items) is not list or len(items) > 1_000
        or type(policy) is not SourceMonitoringInitializationPolicy
        or policy.mode not in {"catch_up", "from_time"}
        or adapter_key not in {"sec_filings", "company_ir"}
    ):
        raise _error("SOURCE_MONITORING_INITIAL_HISTORY_INVALID", "invalid bounded official history context")
    normalized_items = []
    # Retain the existing 50-item Inbox packet bound while validating every
    # candidate, including identities that will be excluded or delivered later.
    for offset in range(0, len(items), 50):
        ensure_source_poll_active(
            deadline_monotonic_ms=deadline_monotonic_ms, cancel_event=cancel_event,
        )
        packet = build_source_import_packet(
            adapter_key=adapter_key, external_run_id=_PREVIEW_EXTERNAL_RUN_ID,
            captured_at_ms=captured_at_ms, observed_items=items[offset:offset + 50],
        )
        normalized, _ = accept_source_import(
            canonical_source_import_payload(packet, received_at_ms=captured_at_ms),
            received_at_ms=captured_at_ms,
        )
        normalized_items.extend(normalized["items"])
    if policy.mode == "catch_up":
        selected = sorted(
            range(len(normalized_items)),
            key=lambda index: (
                -_timestamp_ms(normalized_items[index]["occurred_at"]),
                normalized_items[index]["server_fingerprint"],
            ),
        )[:policy.catch_up_max_items]
    else:
        selected = [
            index for index, item in enumerate(normalized_items)
            if _cutoff_time_ms(item, captured_at_ms=captured_at_ms) >= policy.initial_from_time_ms
        ]
    ensure_source_poll_active(
        deadline_monotonic_ms=deadline_monotonic_ms, cancel_event=cancel_event,
    )
    history_sha256 = canonical_sha256({
        "version": "source_monitoring_initial_history_v1",
        "adapter_key": adapter_key,
        "mode": policy.mode,
        "catch_up_max_items": policy.catch_up_max_items,
        "from_time_ms": policy.initial_from_time_ms,
        "candidate_fingerprints": sorted(item["server_fingerprint"] for item in normalized_items),
        "authorized_fingerprints": sorted(normalized_items[index]["server_fingerprint"] for index in selected),
    })
    return tuple(selected), history_sha256


def _validate_context(
    result: Any,
    metadata: Any,
    policy: Any,
    *,
    initial_required: Any,
    received_at_ms: Any,
) -> tuple[
    AdapterPollResult,
    SourceAdapterMetadata,
    SourceMonitoringInitializationPolicy,
    int,
]:
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
    if type(policy) is not SourceMonitoringInitializationPolicy:
        raise _error(
            "SOURCE_MONITORING_INITIAL_POLICY_INVALID",
            "initial planning requires an exact effective policy",
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
        and policy.mode != "seed_only"
    ):
        raise _error(
            "SOURCE_MONITORING_MARKET_INITIAL_MODE_FORBIDDEN",
            "read-only market adapters support seed_only initialization only",
        )
    if (
        initial_required
        and policy.mode == "from_time"
        and policy.initial_from_time_ms > result.captured_at_ms
    ):
        raise _error(
            "SOURCE_MONITORING_FROM_TIME_FUTURE",
            "from_time cannot be later than the adapter capture time",
        )
    if (
        policy.continuous_event_cutoff_ms
        and policy.continuous_event_cutoff_ms > result.captured_at_ms
    ):
        raise _error(
            "SOURCE_MONITORING_CONTINUOUS_CUTOFF_FUTURE",
            "continuous event cutoff cannot be later than adapter capture time",
        )
    return result, metadata, policy, received_at_ms


@dataclass(frozen=True, slots=True)
class SourceMonitoringInitialPlan:
    """Internal selected items plus a safe, hash-bound public preview."""

    initial_required: bool
    initialization_blocked: bool
    selected_items: tuple[dict[str, Any], ...]
    preview: dict[str, Any]
    applied_cutoff_at_ms: int
    source_policy_sha256: str
    execution_preview_sha256: str

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
        if type(self.applied_cutoff_at_ms) is not int or self.applied_cutoff_at_ms < 0:
            raise _error(
                "SOURCE_MONITORING_INITIAL_PLAN_INVALID",
                "plan cutoff must be a non-negative native integer",
            )
        if type(self.source_policy_sha256) is not str or (
            self.source_policy_sha256
            and _SHA256_RE.fullmatch(self.source_policy_sha256) is None
        ):
            raise _error(
                "SOURCE_MONITORING_INITIAL_PLAN_INVALID",
                "plan source policy digest is invalid",
            )
        if (
            type(self.execution_preview_sha256) is not str
            or _SHA256_RE.fullmatch(self.execution_preview_sha256) is None
        ):
            raise _error(
                "SOURCE_MONITORING_INITIAL_PLAN_INVALID",
                "plan execution preview digest is invalid",
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
        return build_source_import_packet(
            adapter_key=result.adapter_key,
            external_run_id=external_run_id,
            captured_at_ms=result.captured_at_ms,
            cutoff_at_ms=self.applied_cutoff_at_ms,
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
        receipt = {
            "version": (
                SOURCE_MONITORING_INITIALIZATION_VERSION_V2
                if self.source_policy_sha256
                else SOURCE_MONITORING_INITIALIZATION_VERSION
            ),
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
        if self.source_policy_sha256:
            receipt.update({
                "authorization_kind": "static_seed_policy",
                "source_policy_sha256": self.source_policy_sha256,
                "execution_preview_sha256": self.execution_preview_sha256,
            })
        return receipt


def require_catch_up_confirmation_before_poll(
    policy: SourceMonitoringInitializationPolicy | SourceMonitoringSettings,
    *,
    initial_required: bool,
) -> None:
    """Reject unconfirmed catch-up before a run row or network poll exists."""

    effective = (
        policy.initialization_policy_for(official_source=True)
        if type(policy) is SourceMonitoringSettings
        else policy
    )
    if type(effective) is not SourceMonitoringInitializationPolicy:
        raise _error(
            "SOURCE_MONITORING_INITIAL_POLICY_INVALID",
            "catch-up confirmation requires an exact effective policy",
        )
    if (
        initial_required
        and effective.mode == "catch_up"
        and not effective.initial_preview_sha256
    ):
        raise _error(
            "SOURCE_MONITORING_CATCH_UP_PREVIEW_REQUIRED",
            "catch_up requires an explicit preview SHA-256 confirmation",
        )


def require_initial_preview_match(
    plan: SourceMonitoringInitialPlan,
    policy: SourceMonitoringInitializationPolicy | SourceMonitoringSettings,
) -> None:
    effective = (
        policy.initialization_policy_for(official_source=True)
        if type(policy) is SourceMonitoringSettings
        else policy
    )
    if type(effective) is not SourceMonitoringInitializationPolicy:
        raise _error(
            "SOURCE_MONITORING_INITIAL_POLICY_INVALID",
            "preview matching requires an exact effective policy",
        )
    if (
        plan.initial_required
        and effective.mode == "catch_up"
        and plan.preview["preview_sha256"] != effective.initial_preview_sha256
    ):
        raise _error(
            "SOURCE_MONITORING_CATCH_UP_PREVIEW_MISMATCH",
            "catch_up source evidence changed after the confirmed preview",
        )


def plan_initial_poll(
    result: Any,
    *,
    metadata: Any,
    settings: Any = None,
    initialization_policy: Any = None,
    initial_seed_policy: Any = None,
    initial_required: Any,
    received_at_ms: Any,
) -> SourceMonitoringInitialPlan:
    """Validate all candidates, then deterministically apply initial policy."""

    if type(metadata) is not SourceAdapterMetadata:
        raise _error(
            "SOURCE_MONITORING_ADAPTER_METADATA_INVALID",
            "initial planning requires exact sealed adapter metadata",
        )
    policy = _resolve_initialization_policy(
        metadata,
        settings=settings,
        initialization_policy=initialization_policy,
    )
    result, metadata, policy, received_at_ms = _validate_context(
        result,
        metadata,
        policy,
        initial_required=initial_required,
        received_at_ms=received_at_ms,
    )
    full_packet = build_packet_from_poll_result(
        result,
        external_run_id=_PREVIEW_EXTERNAL_RUN_ID,
        max_items=policy.max_items_per_run,
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
            "cutoff_time_ms": _cutoff_time_ms(
                item,
                captured_at_ms=result.captured_at_ms,
            ),
            "fingerprint": item["server_fingerprint"],
        }
        for index, item in enumerate(normalized_items)
    ]
    blocked = bool(
        initial_required and (result.source_errors or result.rejected_count > 0)
    )
    if (
        initial_required and not blocked
        and metadata.adapter_key in {"sec_filings", "company_ir"}
        and policy.mode in {"catch_up", "from_time"}
        and not result.initial_history_sha256
    ):
        raise _error(
            "SOURCE_MONITORING_INITIAL_HISTORY_INVALID",
            "initial official backfill requires a seal over the complete history and authorization",
        )
    if blocked or (initial_required and policy.mode == "seed_only"):
        selected = []
    elif initial_required and policy.mode == "catch_up":
        selected = sorted(
            indexed,
            key=lambda item: (-item["occurred_at_ms"], item["fingerprint"]),
        )[: policy.catch_up_max_items]
    elif initial_required and policy.mode == "from_time":
        selected = [
            item
            for item in indexed
            if item["cutoff_time_ms"] >= policy.initial_from_time_ms
        ]
    elif (not initial_required) and policy.continuous_event_cutoff_ms:
        selected = [
            item
            for item in indexed
            if item["cutoff_time_ms"] >= policy.continuous_event_cutoff_ms
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
        "mode": policy.mode,
        "initial_required": initial_required,
        "initialization_blocked": blocked,
        "catch_up_max_items": policy.catch_up_max_items,
        "from_time_ms": policy.initial_from_time_ms,
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
    if result.initial_history_sha256:
        if not initial_required or metadata.adapter_key not in {"sec_filings", "company_ir"}:
            raise _error("SOURCE_MONITORING_INITIAL_HISTORY_INVALID", "history seal is only valid for an initial official poll")
        preview_basis["initial_history_sha256"] = result.initial_history_sha256
    execution_preview_basis = {
        **preview_basis,
        "continuous_event_cutoff_ms": policy.continuous_event_cutoff_ms,
        "applied_cutoff_at_ms": (
            policy.initial_from_time_ms
            if initial_required and policy.mode == "from_time"
            else policy.continuous_event_cutoff_ms
            if not initial_required and policy.continuous_event_cutoff_ms
            else result.captured_at_ms
        ),
    }
    execution_preview_sha256 = canonical_sha256(execution_preview_basis)
    source_policy_sha256 = ""
    authorization_preview_sha256 = canonical_sha256(preview_basis)
    if initial_required and metadata.official_source is False:
        static_preview = build_static_seed_preview(
            metadata=metadata,
            initialization_policy=policy,
            initial_seed_policy=initial_seed_policy,
            starting_checkpoint=result.started_checkpoint,
        )
        source_policy_sha256 = static_preview["source_policy_sha256"]
        authorization_preview_sha256 = static_preview["preview_sha256"]
    preview = {
        **preview_basis,
        "captured_at_ms": result.captured_at_ms,
        "preview_sha256": authorization_preview_sha256,
    }
    plan = SourceMonitoringInitialPlan(
        initial_required=initial_required,
        initialization_blocked=blocked,
        selected_items=selected_items,
        preview=preview,
        applied_cutoff_at_ms=execution_preview_basis["applied_cutoff_at_ms"],
        source_policy_sha256=source_policy_sha256,
        execution_preview_sha256=execution_preview_sha256,
    )
    return plan


__all__ = [
    "poll_for_initialization",
    "select_initial_history",
    "SOURCE_MONITORING_INITIALIZATION_VERSION",
    "SOURCE_MONITORING_INITIALIZATION_VERSION_V2",
    "SOURCE_MONITORING_INITIAL_PREVIEW_VERSION",
    "SOURCE_MONITORING_STATIC_SEED_PREVIEW_VERSION",
    "SourceMonitoringInitialPlan",
    "SourceMonitoringInitializationError",
    "build_static_seed_preview",
    "normalize_initial_seed_policy",
    "plan_initial_poll",
    "require_catch_up_confirmation_before_poll",
    "require_initial_preview_match",
]
