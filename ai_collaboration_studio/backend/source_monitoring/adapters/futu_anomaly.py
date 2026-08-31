"""Sealed read-only Futu quote adapter for deterministic anomaly signals."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from ...market.futu_readonly import FutuUsMarketAdapter, STORAGE_SYMBOLS
from ..contracts import (
    FUTU_ANOMALY_SOURCE_CHANNEL,
    READONLY_MARKET_SOURCE_CLASS,
    AdapterPollResult,
    SourceMonitoringContractError,
    SourcePollError,
    canonical_sha256,
)
from ..futu_anomaly_contracts import (
    FUTU_ANOMALY_CHECKPOINT_VERSION,
    FUTU_ANOMALY_PROJECTION_VERSION,
    FUTU_ANOMALY_RULE_IDS,
    FUTU_ANOMALY_SOURCE_URL,
    futu_anomaly_policy_manifest,
    normalize_futu_anomaly_checkpoint,
    project_futu_anomaly_snapshot,
)
from ..packet_builder import SourcePacketBuildError, build_source_import_packet
from .base import (
    MAX_POLL_INTERVAL_MS,
    MIN_POLL_INTERVAL_MS,
    SOURCE_ADAPTER_CONTRACT_VERSION,
    validate_poll_context,
)


FUTU_ANOMALY_ADAPTER_KEY = "futu_anomaly_signals"
FUTU_ANOMALY_CONFIG_BASIS_VERSION = "futu_anomaly_config_basis_v1"
FUTU_ANOMALY_POLL_INTERVAL_MS = 60_000
FUTU_ANOMALY_CANDIDATE_LIMIT = len(STORAGE_SYMBOLS) * len(
    FUTU_ANOMALY_RULE_IDS
)
FUTU_ANOMALY_MAX_MARKET_CALLS_PER_POLL = 1


class _ReadonlyQuoteBatchClient(Protocol):
    def quote_batch(
        self,
        symbols: tuple[str, ...] | list[str],
        *,
        force: bool = False,
    ) -> dict[str, Any]: ...


def _callable_token(value: Any) -> Any:
    return getattr(value, "__func__", value)


def _native_observed_at(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise SourceMonitoringContractError(
            "FUTU_ANOMALY_OBSERVED_TIME_INVALID",
            "observed_at_ms must be a non-negative native integer",
        )
    try:
        datetime.fromtimestamp(value / 1_000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise SourceMonitoringContractError(
            "FUTU_ANOMALY_OBSERVED_TIME_INVALID",
            "observed_at_ms is outside the supported UTC datetime range",
        ) from exc
    return value


class FutuAnomalySourceAdapter:
    """Poll exactly one four-symbol read-only quote snapshot per run."""

    contract_version = SOURCE_ADAPTER_CONTRACT_VERSION
    adapter_key = FUTU_ANOMALY_ADAPTER_KEY
    official_source = False
    source_class = READONLY_MARKET_SOURCE_CLASS
    source_channel = FUTU_ANOMALY_SOURCE_CHANNEL
    max_market_calls_per_poll = FUTU_ANOMALY_MAX_MARKET_CALLS_PER_POLL
    execution_capability = "none"
    live_trading_allowed = False

    @property
    def allowed_symbols(self) -> tuple[str, ...]:
        return self._allowed_symbols

    @property
    def max_candidates_per_poll(self) -> int:
        return FUTU_ANOMALY_CANDIDATE_LIMIT

    @property
    def poll_interval_ms(self) -> int:
        return self._poll_interval_ms

    @property
    def config_version(self) -> str:
        return self._config_version

    def __init__(
        self,
        *,
        market_adapter: _ReadonlyQuoteBatchClient | None = None,
        poll_interval_ms: int = FUTU_ANOMALY_POLL_INTERVAL_MS,
    ) -> None:
        if (
            type(poll_interval_ms) is not int
            or not MIN_POLL_INTERVAL_MS <= poll_interval_ms <= MAX_POLL_INTERVAL_MS
        ):
            raise ValueError(
                "poll_interval_ms must be a native integer from one minute to seven days"
            )
        client = FutuUsMarketAdapter() if market_adapter is None else market_adapter
        if isinstance(client, type):
            raise ValueError("market_adapter must be an instance")
        quote_batch = getattr(client, "quote_batch", None)
        if not callable(quote_batch):
            raise ValueError("market_adapter must implement quote_batch(symbols, force=...)")

        self._allowed_symbols = tuple(STORAGE_SYMBOLS)
        self._poll_interval_ms = poll_interval_ms
        self._market_adapter = client
        self._sealed_market_adapter = client
        self._client_type_token = f"{type(client).__module__}.{type(client).__qualname__}"
        self._sealed_quote_batch_token = _callable_token(quote_batch)
        self._client_mode = (
            "futu_readonly_client_v1"
            if type(client) is FutuUsMarketAdapter
            else "injected_readonly_quote_client_v1"
        )
        self._sealed_socket_probe = getattr(client, "_socket_probe", None)
        self._sealed_clock = getattr(client, "_clock", None)
        self._sealed_monotonic_clock = getattr(client, "_monotonic_clock", None)
        self._sealed_snapshot_id_factory = getattr(
            client,
            "_snapshot_id_factory",
            None,
        )
        self._sealed_client_configuration = self._client_configuration()
        self._sealed_config_sha256 = canonical_sha256(self._config_basis())
        self._config_version = (
            "futu_anomaly_config_v1_" + self._sealed_config_sha256[:16]
        )

    def _client_configuration(self) -> dict[str, Any]:
        if type(self._market_adapter) is not FutuUsMarketAdapter:
            return {
                "client_mode": self._client_mode,
                "client_type": self._client_type_token,
            }
        client = self._market_adapter
        return {
            "client_mode": self._client_mode,
            "client_type": self._client_type_token,
            "host": client.host,
            "port": client.port,
            "cache_ttl_seconds": client.cache_ttl_seconds,
        }

    def _config_basis(self) -> dict[str, Any]:
        return {
            "version": FUTU_ANOMALY_CONFIG_BASIS_VERSION,
            "adapter_key": self.adapter_key,
            "checkpoint_version": FUTU_ANOMALY_CHECKPOINT_VERSION,
            "projection_version": FUTU_ANOMALY_PROJECTION_VERSION,
            "source_class": self.source_class,
            "source_channel": self.source_channel,
            "source_url": FUTU_ANOMALY_SOURCE_URL,
            "allowed_symbols": list(self.allowed_symbols),
            "rule_ids": list(FUTU_ANOMALY_RULE_IDS),
            "policy": futu_anomaly_policy_manifest(),
            "candidate_limit": self.max_candidates_per_poll,
            "max_market_calls_per_poll": self.max_market_calls_per_poll,
            "poll_interval_ms": self.poll_interval_ms,
            "client": self._client_configuration(),
            "event_identity_policy": "session_rule_stable_v1",
            "source_error_policy": "atomic_zero_candidates_v1",
        }

    def _assert_config_seal(self) -> None:
        if (
            self._market_adapter is not self._sealed_market_adapter
            or f"{type(self._market_adapter).__module__}.{type(self._market_adapter).__qualname__}"
            != self._client_type_token
        ):
            raise SourceMonitoringContractError(
                "FUTU_ANOMALY_SOURCE_PROVENANCE_DRIFT",
                "Futu anomaly inner quote client changed after construction",
            )
        quote_batch = getattr(self._market_adapter, "quote_batch", None)
        if (
            not callable(quote_batch)
            or _callable_token(quote_batch) is not self._sealed_quote_batch_token
        ):
            raise SourceMonitoringContractError(
                "FUTU_ANOMALY_SOURCE_PROVENANCE_DRIFT",
                "Futu anomaly quote callable changed after construction",
            )
        if self._client_configuration() != self._sealed_client_configuration:
            raise SourceMonitoringContractError(
                "FUTU_ANOMALY_SOURCE_PROVENANCE_DRIFT",
                "Futu anomaly quote client configuration changed after construction",
            )
        if type(self._market_adapter) is FutuUsMarketAdapter and any((
            self._market_adapter._socket_probe is not self._sealed_socket_probe,
            self._market_adapter._clock is not self._sealed_clock,
            self._market_adapter._monotonic_clock is not self._sealed_monotonic_clock,
            self._market_adapter._snapshot_id_factory
            is not self._sealed_snapshot_id_factory,
        )):
            raise SourceMonitoringContractError(
                "FUTU_ANOMALY_SOURCE_PROVENANCE_DRIFT",
                "Futu anomaly quote client callables changed after construction",
            )
        current_sha = canonical_sha256(self._config_basis())
        expected_version = "futu_anomaly_config_v1_" + current_sha[:16]
        if (
            current_sha != self._sealed_config_sha256
            or self.config_version != expected_version
        ):
            raise SourceMonitoringContractError(
                "FUTU_ANOMALY_CONFIG_DRIFT",
                "Futu anomaly adapter configuration changed after construction",
            )

    def poll(
        self,
        checkpoint: Any,
        *,
        observed_at_ms: Any,
        etag: Any = "",
        last_modified: Any = "",
        max_items: Any = 50,
    ) -> AdapterPollResult:
        self._assert_config_seal()
        clean_etag, clean_last_modified, safe_max_items = validate_poll_context(
            etag=etag,
            last_modified=last_modified,
            max_items=max_items,
        )
        captured_at_ms = _native_observed_at(observed_at_ms)
        started_checkpoint, _ = normalize_futu_anomaly_checkpoint(checkpoint)
        if safe_max_items < self.max_candidates_per_poll:
            raise SourceMonitoringContractError(
                "FUTU_ANOMALY_ITEM_CAPACITY_TOO_LOW",
                (
                    f"max_items={safe_max_items} is below the sealed Futu anomaly "
                    f"candidate bound of {self.max_candidates_per_poll}"
                ),
            )

        try:
            snapshot = self._market_adapter.quote_batch(
                self.allowed_symbols,
                force=True,
            )
        except Exception as exc:
            return AdapterPollResult.build(
                adapter_key=self.adapter_key,
                started_checkpoint=started_checkpoint,
                next_checkpoint=started_checkpoint,
                observed_items=(),
                source_errors=(SourcePollError.build(
                    "FUTU_ANOMALY_POLL_ERROR",
                    str(exc)[:1_000] or "read-only Futu quote poll failed",
                    self.adapter_key,
                ),),
                retry_after_ms=self.poll_interval_ms,
                captured_at_ms=captured_at_ms,
                etag=clean_etag,
                last_modified=clean_last_modified,
                rejected_count=1,
                market_calls_performed=1,
            )

        projected = project_futu_anomaly_snapshot(
            snapshot,
            started_checkpoint=started_checkpoint,
            observed_at_ms=captured_at_ms,
        )
        projection_errors = projected.source_errors
        rejected_count = projected.rejected_count
        if not projection_errors:
            try:
                build_source_import_packet(
                    adapter_key=self.adapter_key,
                    external_run_id=f"futu-anomaly-contract-{captured_at_ms}",
                    captured_at_ms=captured_at_ms,
                    observed_items=projected.observed_items,
                    source_channel=self.source_channel,
                    max_items=safe_max_items,
                )
            except SourcePacketBuildError as exc:
                projection_errors = (SourcePollError.build(
                    "FUTU_ANOMALY_PACKET_REJECTED",
                    str(exc)[:1_000],
                    self.adapter_key,
                ),)
                rejected_count += len(projected.observed_items)

        return AdapterPollResult.build(
            adapter_key=self.adapter_key,
            started_checkpoint=started_checkpoint,
            next_checkpoint=(
                started_checkpoint if projection_errors else projected.next_checkpoint
            ),
            observed_items=(() if projection_errors else projected.observed_items),
            source_errors=projection_errors,
            retry_after_ms=self.poll_interval_ms if projection_errors else 0,
            captured_at_ms=captured_at_ms,
            etag=clean_etag,
            last_modified=clean_last_modified,
            duplicate_count=projected.duplicate_count,
            rejected_count=rejected_count,
            market_calls_performed=1,
        )


__all__ = [
    "FUTU_ANOMALY_ADAPTER_KEY",
    "FUTU_ANOMALY_CANDIDATE_LIMIT",
    "FUTU_ANOMALY_CONFIG_BASIS_VERSION",
    "FUTU_ANOMALY_MAX_MARKET_CALLS_PER_POLL",
    "FUTU_ANOMALY_POLL_INTERVAL_MS",
    "FutuAnomalySourceAdapter",
]
