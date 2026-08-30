"""Side-effect-free adapter protocol and safety metadata validation."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Protocol, runtime_checkable

from ..contracts import (
    AdapterPollResult,
    SourceMonitoringContractError,
    normalize_adapter_key,
)


SOURCE_ADAPTER_CONTRACT_VERSION = "source_adapter_v1"
MIN_POLL_INTERVAL_MS = 60_000
MAX_POLL_INTERVAL_MS = 7 * 24 * 60 * 60 * 1_000

_CONFIG_VERSION_RE = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")


class SourceAdapterContractError(SourceMonitoringContractError):
    """Raised when an adapter does not satisfy the closed local protocol."""


@dataclass(frozen=True, slots=True)
class SourceAdapterMetadata:
    contract_version: str
    adapter_key: str
    config_version: str
    poll_interval_ms: int
    official_source: bool
    execution_capability: str
    live_trading_allowed: bool

    def __post_init__(self) -> None:
        if (
            type(self.contract_version) is not str
            or self.contract_version != SOURCE_ADAPTER_CONTRACT_VERSION
        ):
            raise SourceAdapterContractError(
                "SOURCE_ADAPTER_CONTRACT_VERSION_INVALID",
                "adapter contract_version is unsupported",
            )
        clean_key = normalize_adapter_key(self.adapter_key)
        if clean_key != self.adapter_key:
            raise SourceAdapterContractError(
                "SOURCE_ADAPTER_KEY_NONCANONICAL",
                "adapter_key must already be canonical",
            )
        if (
            type(self.config_version) is not str
            or not _CONFIG_VERSION_RE.fullmatch(self.config_version)
        ):
            raise SourceAdapterContractError(
                "SOURCE_ADAPTER_CONFIG_VERSION_INVALID",
                "config_version must be a canonical lowercase version token",
            )
        if (
            type(self.poll_interval_ms) is not int
            or not MIN_POLL_INTERVAL_MS
            <= self.poll_interval_ms
            <= MAX_POLL_INTERVAL_MS
        ):
            raise SourceAdapterContractError(
                "SOURCE_ADAPTER_POLL_INTERVAL_INVALID",
                "poll_interval_ms must be a native integer between one minute and seven days",
            )
        if type(self.official_source) is not bool:
            raise SourceAdapterContractError(
                "SOURCE_ADAPTER_OFFICIAL_FLAG_INVALID",
                "official_source must be a native boolean",
            )
        if (
            type(self.execution_capability) is not str
            or self.execution_capability != "none"
        ):
            raise SourceAdapterContractError(
                "SOURCE_ADAPTER_EXECUTION_BOUNDARY_INVALID",
                "adapter execution_capability must be none",
            )
        if type(self.live_trading_allowed) is not bool:
            raise SourceAdapterContractError(
                "SOURCE_ADAPTER_LIVE_BOUNDARY_INVALID",
                "live_trading_allowed must be a native boolean",
            )
        if self.live_trading_allowed is not False:
            raise SourceAdapterContractError(
                "SOURCE_ADAPTER_LIVE_BOUNDARY_INVALID",
                "live_trading_allowed must be false",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "adapter_key": self.adapter_key,
            "config_version": self.config_version,
            "poll_interval_ms": self.poll_interval_ms,
            "official_source": self.official_source,
            "execution_capability": self.execution_capability,
            "live_trading_allowed": self.live_trading_allowed,
        }


@runtime_checkable
class SourceAdapter(Protocol):
    """Closed polling port implemented by fixed, code-registered adapters."""

    contract_version: str
    adapter_key: str
    config_version: str
    poll_interval_ms: int
    official_source: bool
    execution_capability: str
    live_trading_allowed: bool

    def poll(
        self,
        checkpoint: dict[str, Any],
        *,
        observed_at_ms: int,
        etag: str = "",
        last_modified: str = "",
        max_items: int = 50,
    ) -> AdapterPollResult: ...


def validate_source_adapter(adapter: Any) -> SourceAdapterMetadata:
    """Validate and snapshot one adapter's exact safety metadata."""

    if adapter is None or isinstance(adapter, type):
        raise SourceAdapterContractError(
            "SOURCE_ADAPTER_INVALID",
            "adapter must be an instance",
        )
    poll = getattr(adapter, "poll", None)
    if not callable(poll):
        raise SourceAdapterContractError(
            "SOURCE_ADAPTER_POLL_MISSING",
            "adapter must implement poll(checkpoint, observed_at_ms=...)",
        )
    return SourceAdapterMetadata(
        contract_version=getattr(adapter, "contract_version", None),
        adapter_key=getattr(adapter, "adapter_key", None),
        config_version=getattr(adapter, "config_version", None),
        poll_interval_ms=getattr(adapter, "poll_interval_ms", None),
        official_source=getattr(adapter, "official_source", None),
        execution_capability=getattr(adapter, "execution_capability", None),
        live_trading_allowed=getattr(adapter, "live_trading_allowed", None),
    )


__all__ = [
    "SOURCE_ADAPTER_CONTRACT_VERSION",
    "MAX_POLL_INTERVAL_MS",
    "MIN_POLL_INTERVAL_MS",
    "SourceAdapter",
    "SourceAdapterContractError",
    "SourceAdapterMetadata",
    "validate_source_adapter",
]
