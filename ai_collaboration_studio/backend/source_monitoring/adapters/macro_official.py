"""Fixed official macro adapters with an explicit three-state lifecycle."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Protocol

from ...market.official_macro import OfficialMacroSourceClient
from ...source_poll_control import (
    SourcePollCancelled,
    SourcePollDeadlineExceeded,
    ensure_source_poll_active,
    validate_source_poll_control,
)
from ..contracts import (
    AdapterPollResult,
    SourceMonitoringContractError,
    SourcePollError,
    canonical_sha256,
)
from ..macro_contracts import (
    MACRO_CHECKPOINT_ENTRY_LIMIT,
    MACRO_LIFECYCLE_VERSION,
    MACRO_PROJECTION_VERSION,
    normalize_macro_checkpoint,
    normalize_macro_source_errors,
    project_macro_records,
)
from ..packet_builder import SourcePacketBuildError, build_source_import_packet
from .base import (
    MAX_POLL_INTERVAL_MS,
    MIN_POLL_INTERVAL_MS,
    SOURCE_ADAPTER_CONTRACT_VERSION,
    validate_poll_context,
)


FEDERAL_RESERVE_ADAPTER_KEY = "federal_reserve"
BLS_RELEASES_ADAPTER_KEY = "bls_releases"
TREASURY_RELEASES_ADAPTER_KEY = "treasury_releases"
OFFICIAL_MACRO_CALENDAR_ADAPTER_KEY = "official_macro_calendar"

FEDERAL_RESERVE_CHECKPOINT_VERSION = "federal_reserve_checkpoint_v1"
BLS_RELEASES_CHECKPOINT_VERSION = "bls_releases_checkpoint_v1"
TREASURY_RELEASES_CHECKPOINT_VERSION = "treasury_releases_checkpoint_v1"
OFFICIAL_MACRO_CALENDAR_CHECKPOINT_VERSION = "official_macro_calendar_checkpoint_v1"
OFFICIAL_MACRO_CONFIG_BASIS_VERSION = "official_macro_config_basis_v1"

FEDERAL_RESERVE_POLL_INTERVAL_MS = 15 * 60 * 1_000
BLS_RELEASES_POLL_INTERVAL_MS = 6 * 60 * 60 * 1_000
TREASURY_RELEASES_POLL_INTERVAL_MS = 60 * 60 * 1_000
OFFICIAL_MACRO_CALENDAR_POLL_INTERVAL_MS = 6 * 60 * 60 * 1_000

FEDERAL_RESERVE_CANDIDATE_LIMIT = 50
BLS_RELEASES_CANDIDATE_LIMIT = 12
TREASURY_RELEASES_CANDIDATE_LIMIT = 10
OFFICIAL_MACRO_CALENDAR_CANDIDATE_LIMIT = 50


class _OfficialMacroBatchClient(Protocol):
    def federal_reserve_releases(
        self,
        *,
        limit: int,
        deadline_monotonic_ms: int = 0,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]: ...

    def bls_releases(
        self,
        *,
        limit: int,
        deadline_monotonic_ms: int = 0,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]: ...

    def treasury_releases(
        self,
        *,
        limit: int,
        deadline_monotonic_ms: int = 0,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]: ...

    def calendar_events(
        self,
        *,
        limit: int,
        deadline_monotonic_ms: int = 0,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]: ...


def _native_observed_at(value: Any) -> tuple[int, datetime]:
    if type(value) is not int or value < 0:
        raise SourceMonitoringContractError(
            "SOURCE_MONITORING_CAPTURE_TIME_INVALID",
            "observed_at_ms must be a non-negative native integer",
        )
    try:
        observed = datetime.fromtimestamp(value / 1_000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise SourceMonitoringContractError(
            "SOURCE_MONITORING_CAPTURE_TIME_INVALID",
            "observed_at_ms is outside the supported UTC datetime range",
        ) from exc
    return value, observed


def _manifest_snapshot(client: Any) -> dict[str, Any]:
    manifest = getattr(client, "source_manifest", None)
    if callable(manifest):
        manifest = manifest()
    if type(manifest) is not dict:
        manifest = {
            "transport_identity": getattr(client, "transport_identity", "custom"),
            "client_type": f"{type(client).__module__}.{type(client).__qualname__}",
        }
    return manifest


class _OfficialMacroSourceAdapter:
    """Common sealed wrapper; concrete subclasses only select one fixed batch."""

    contract_version = SOURCE_ADAPTER_CONTRACT_VERSION
    official_source = True
    execution_capability = "none"
    live_trading_allowed = False

    adapter_key = ""
    checkpoint_version = ""
    batch_method = ""
    subject_phase = ""
    allowed_authorities = frozenset()
    default_candidate_limit = 0
    default_poll_interval_ms = 0

    @property
    def max_candidates_per_poll(self) -> int:
        return self._candidate_limit

    @property
    def poll_interval_ms(self) -> int:
        return self._poll_interval_ms

    @property
    def config_version(self) -> str:
        return self._config_version

    def _config_basis(self) -> dict[str, Any]:
        return {
            "version": OFFICIAL_MACRO_CONFIG_BASIS_VERSION,
            "adapter_key": self.adapter_key,
            "checkpoint_version": self.checkpoint_version,
            "lifecycle_version": MACRO_LIFECYCLE_VERSION,
            "projection_version": MACRO_PROJECTION_VERSION,
            "batch_method": self.batch_method,
            "subject_phase": self.subject_phase,
            "allowed_authorities": sorted(self.allowed_authorities),
            "candidate_limit": self.max_candidates_per_poll,
            "checkpoint_entry_limit": MACRO_CHECKPOINT_ENTRY_LIMIT,
            "poll_interval_ms": self.poll_interval_ms,
            "client_type": self._inner_client_type_token,
            "transport_identity": self._transport_identity,
            "source_manifest_sha256": self._source_manifest_sha256,
            "window_checkpoint_policy": "replace_complete_current_window_v1",
            "source_error_policy": "atomic_zero_candidates_v1",
        }

    def _assert_config_seal(self) -> None:
        if (
            self._client is not self._sealed_inner_client
            or (
                f"{type(self._client).__module__}."
                f"{type(self._client).__qualname__}"
            )
            != self._inner_client_type_token
        ):
            raise SourceMonitoringContractError(
                "OFFICIAL_MACRO_SOURCE_PROVENANCE_DRIFT",
                f"{self.adapter_key} inner client changed after construction",
            )
        current_batch = getattr(self._client, self.batch_method, None)
        current_batch_token = getattr(current_batch, "__func__", current_batch)
        if (
            not callable(current_batch)
            or current_batch_token is not self._sealed_batch_token
        ):
            raise SourceMonitoringContractError(
                "OFFICIAL_MACRO_SOURCE_PROVENANCE_DRIFT",
                f"{self.adapter_key} batch callable changed after construction",
            )
        if getattr(self._client, "transport_identity", None) != self._transport_identity:
            raise SourceMonitoringContractError(
                "OFFICIAL_MACRO_SOURCE_PROVENANCE_DRIFT",
                f"{self.adapter_key} transport identity changed after construction",
            )
        if canonical_sha256(_manifest_snapshot(self._client)) != self._source_manifest_sha256:
            raise SourceMonitoringContractError(
                "OFFICIAL_MACRO_SOURCE_PROVENANCE_DRIFT",
                f"{self.adapter_key} source manifest changed after construction",
            )
        current_transport = getattr(self._client, "_fetch_bytes", None)
        current_transport_token = getattr(
            current_transport, "__func__", current_transport
        )
        if current_transport_token is not self._sealed_transport_token:
            raise SourceMonitoringContractError(
                "OFFICIAL_MACRO_SOURCE_PROVENANCE_DRIFT",
                f"{self.adapter_key} transport changed after construction",
            )
        current_config_sha256 = canonical_sha256(self._config_basis())
        if current_config_sha256 != self._sealed_config_sha256:
            raise SourceMonitoringContractError(
                "OFFICIAL_MACRO_CONFIG_DRIFT",
                f"{self.adapter_key} configuration changed after construction",
            )
        expected = f"{self.adapter_key}_config_v1_" + self._sealed_config_sha256[:16]
        if self.config_version != expected:
            raise SourceMonitoringContractError(
                "OFFICIAL_MACRO_CONFIG_DRIFT",
                f"{self.adapter_key} configuration changed after construction",
            )

    def __init__(
        self,
        *,
        client: _OfficialMacroBatchClient | None = None,
        candidate_limit: int | None = None,
        poll_interval_ms: int | None = None,
    ) -> None:
        selected_limit = (
            self.default_candidate_limit
            if candidate_limit is None
            else candidate_limit
        )
        if type(selected_limit) is not int or not 1 <= selected_limit <= 50:
            raise ValueError("candidate_limit must be a native integer from 1 to 50")
        selected_interval = (
            self.default_poll_interval_ms
            if poll_interval_ms is None
            else poll_interval_ms
        )
        if (
            type(selected_interval) is not int
            or not MIN_POLL_INTERVAL_MS <= selected_interval <= MAX_POLL_INTERVAL_MS
        ):
            raise ValueError(
                "poll_interval_ms must be a native integer from one minute to seven days"
            )
        source_client = OfficialMacroSourceClient() if client is None else client
        batch_callable = getattr(source_client, self.batch_method, None)
        if not callable(batch_callable):
            raise ValueError(f"client must implement {self.batch_method}(limit=...)")
        self._candidate_limit = selected_limit
        self._poll_interval_ms = selected_interval
        self._client = source_client
        self._sealed_inner_client = source_client
        self._inner_client_type_token = (
            f"{type(source_client).__module__}.{type(source_client).__qualname__}"
        )
        self._sealed_batch_callable = batch_callable
        self._sealed_batch_token = getattr(batch_callable, "__func__", batch_callable)
        sealed_transport = getattr(source_client, "_fetch_bytes", None)
        self._sealed_transport_token = getattr(
            sealed_transport, "__func__", sealed_transport
        )
        transport_identity = getattr(source_client, "transport_identity", "custom")
        if type(transport_identity) is not str or not transport_identity:
            raise ValueError("client transport_identity must be a non-empty native string")
        self._transport_identity = transport_identity
        self._source_manifest_sha256 = canonical_sha256(_manifest_snapshot(source_client))
        self._sealed_config_sha256 = canonical_sha256(self._config_basis())
        self._config_version = (
            f"{self.adapter_key}_config_v1_" + self._sealed_config_sha256[:16]
        )

    def poll(
        self,
        checkpoint: Any,
        *,
        observed_at_ms: Any,
        deadline_monotonic_ms: Any = 0,
        cancel_event: threading.Event | None = None,
        etag: Any = "",
        last_modified: Any = "",
        max_items: Any = 50,
    ) -> AdapterPollResult:
        self._assert_config_seal()
        deadline, event = validate_source_poll_control(
            deadline_monotonic_ms=deadline_monotonic_ms,
            cancel_event=cancel_event,
        )
        clean_etag, clean_last_modified, safe_max_items = validate_poll_context(
            etag=etag,
            last_modified=last_modified,
            max_items=max_items,
        )
        if safe_max_items < self.max_candidates_per_poll:
            raise SourceMonitoringContractError(
                "OFFICIAL_MACRO_ITEM_CAPACITY_TOO_LOW",
                (
                    f"max_items={safe_max_items} is below the sealed {self.adapter_key} "
                    f"candidate bound {self.max_candidates_per_poll}"
                ),
            )
        captured_at_ms, observed_at = _native_observed_at(observed_at_ms)
        started_checkpoint, _old_order, previous_projections = (
            normalize_macro_checkpoint(
                checkpoint,
                checkpoint_version=self.checkpoint_version,
                maximum_entries=MACRO_CHECKPOINT_ENTRY_LIMIT,
            )
        )
        try:
            ensure_source_poll_active(
                deadline_monotonic_ms=deadline,
                cancel_event=event,
            )
            if type(self._client) is OfficialMacroSourceClient:
                payload = self._sealed_batch_callable(
                    limit=self.max_candidates_per_poll,
                    deadline_monotonic_ms=deadline,
                    cancel_event=event,
                )
            else:
                payload = self._sealed_batch_callable(
                    limit=self.max_candidates_per_poll
                )
            ensure_source_poll_active(
                deadline_monotonic_ms=deadline,
                cancel_event=event,
            )
        except (SourcePollCancelled, SourcePollDeadlineExceeded):
            raise
        except Exception as exc:
            return AdapterPollResult.build(
                adapter_key=self.adapter_key,
                started_checkpoint=started_checkpoint,
                next_checkpoint=started_checkpoint,
                observed_items=(),
                source_errors=(SourcePollError.build(
                    "OFFICIAL_MACRO_POLL_ERROR",
                    str(exc)[:1_000] or "official macro poll failed",
                    self.adapter_key,
                ),),
                retry_after_ms=self.poll_interval_ms,
                captured_at_ms=captured_at_ms,
                etag=clean_etag,
                last_modified=clean_last_modified,
            )
        if type(payload) is not dict or set(payload) != {"rows", "source_errors"}:
            payload = {
                "rows": [],
                "source_errors": [{
                    "code": "OFFICIAL_MACRO_PAYLOAD_INVALID",
                    "message": "official macro client returned a non-object payload",
                    "scope": self.adapter_key,
                }],
            }
        source_errors = normalize_macro_source_errors(
            payload.get("source_errors"),
            fallback_scope=self.adapter_key,
        )
        if source_errors:
            return AdapterPollResult.build(
                adapter_key=self.adapter_key,
                started_checkpoint=started_checkpoint,
                next_checkpoint=started_checkpoint,
                observed_items=(),
                source_errors=source_errors,
                retry_after_ms=self.poll_interval_ms,
                captured_at_ms=captured_at_ms,
                etag=clean_etag,
                last_modified=clean_last_modified,
            )
        (
            next_checkpoint,
            observed_items,
            duplicate_count,
            rejected_count,
            projection_errors,
        ) = project_macro_records(
            payload.get("rows"),
            started_checkpoint=started_checkpoint,
            previous_projections=previous_projections,
            checkpoint_version=self.checkpoint_version,
            allowed_authorities=self.allowed_authorities,
            subject_phase=self.subject_phase,
            observed_at=observed_at,
            candidate_limit=self.max_candidates_per_poll,
            checkpoint_entry_limit=MACRO_CHECKPOINT_ENTRY_LIMIT,
        )
        if not projection_errors:
            try:
                build_source_import_packet(
                    adapter_key=self.adapter_key,
                    external_run_id=(
                        f"macro-contract-{self.adapter_key}-{captured_at_ms}"
                    ),
                    captured_at_ms=captured_at_ms,
                    observed_items=observed_items,
                    max_items=safe_max_items,
                )
            except SourcePacketBuildError as exc:
                projection_errors = (SourcePollError.build(
                    "OFFICIAL_MACRO_PACKET_REJECTED",
                    str(exc)[:1_000],
                    self.adapter_key,
                ),)
                rejected_count += len(observed_items)
        return AdapterPollResult.build(
            adapter_key=self.adapter_key,
            started_checkpoint=started_checkpoint,
            next_checkpoint=(started_checkpoint if projection_errors else next_checkpoint),
            observed_items=(() if projection_errors else observed_items),
            source_errors=projection_errors,
            retry_after_ms=self.poll_interval_ms if projection_errors else 0,
            captured_at_ms=captured_at_ms,
            etag=clean_etag,
            last_modified=clean_last_modified,
            duplicate_count=duplicate_count,
            rejected_count=rejected_count,
        )


class FederalReserveSourceAdapter(_OfficialMacroSourceAdapter):
    adapter_key = FEDERAL_RESERVE_ADAPTER_KEY
    checkpoint_version = FEDERAL_RESERVE_CHECKPOINT_VERSION
    batch_method = "federal_reserve_releases"
    subject_phase = "release"
    allowed_authorities = frozenset({"federal_reserve"})
    default_candidate_limit = FEDERAL_RESERVE_CANDIDATE_LIMIT
    default_poll_interval_ms = FEDERAL_RESERVE_POLL_INTERVAL_MS


class BlsReleaseSourceAdapter(_OfficialMacroSourceAdapter):
    adapter_key = BLS_RELEASES_ADAPTER_KEY
    checkpoint_version = BLS_RELEASES_CHECKPOINT_VERSION
    batch_method = "bls_releases"
    subject_phase = "release"
    allowed_authorities = frozenset({"bls"})
    default_candidate_limit = BLS_RELEASES_CANDIDATE_LIMIT
    default_poll_interval_ms = BLS_RELEASES_POLL_INTERVAL_MS


class TreasuryReleaseSourceAdapter(_OfficialMacroSourceAdapter):
    adapter_key = TREASURY_RELEASES_ADAPTER_KEY
    checkpoint_version = TREASURY_RELEASES_CHECKPOINT_VERSION
    batch_method = "treasury_releases"
    subject_phase = "release"
    allowed_authorities = frozenset({"treasury"})
    default_candidate_limit = TREASURY_RELEASES_CANDIDATE_LIMIT
    default_poll_interval_ms = TREASURY_RELEASES_POLL_INTERVAL_MS


class OfficialMacroCalendarSourceAdapter(_OfficialMacroSourceAdapter):
    adapter_key = OFFICIAL_MACRO_CALENDAR_ADAPTER_KEY
    checkpoint_version = OFFICIAL_MACRO_CALENDAR_CHECKPOINT_VERSION
    batch_method = "calendar_events"
    subject_phase = "schedule"
    allowed_authorities = frozenset({"federal_reserve", "bls", "treasury"})
    default_candidate_limit = OFFICIAL_MACRO_CALENDAR_CANDIDATE_LIMIT
    default_poll_interval_ms = OFFICIAL_MACRO_CALENDAR_POLL_INTERVAL_MS


__all__ = [
    "BLS_RELEASES_ADAPTER_KEY",
    "BLS_RELEASES_CANDIDATE_LIMIT",
    "BLS_RELEASES_CHECKPOINT_VERSION",
    "BLS_RELEASES_POLL_INTERVAL_MS",
    "FEDERAL_RESERVE_ADAPTER_KEY",
    "FEDERAL_RESERVE_CANDIDATE_LIMIT",
    "FEDERAL_RESERVE_CHECKPOINT_VERSION",
    "FEDERAL_RESERVE_POLL_INTERVAL_MS",
    "OFFICIAL_MACRO_CALENDAR_ADAPTER_KEY",
    "OFFICIAL_MACRO_CALENDAR_CANDIDATE_LIMIT",
    "OFFICIAL_MACRO_CALENDAR_CHECKPOINT_VERSION",
    "OFFICIAL_MACRO_CALENDAR_POLL_INTERVAL_MS",
    "TREASURY_RELEASES_ADAPTER_KEY",
    "TREASURY_RELEASES_CANDIDATE_LIMIT",
    "TREASURY_RELEASES_CHECKPOINT_VERSION",
    "TREASURY_RELEASES_POLL_INTERVAL_MS",
    "BlsReleaseSourceAdapter",
    "FederalReserveSourceAdapter",
    "OfficialMacroCalendarSourceAdapter",
    "TreasuryReleaseSourceAdapter",
]
