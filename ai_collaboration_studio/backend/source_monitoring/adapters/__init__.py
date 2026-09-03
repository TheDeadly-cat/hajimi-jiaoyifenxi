"""Lazy public exports for fixed source-monitoring adapters."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "SOURCE_ADAPTER_CONTRACT_VERSION",
    "MAX_POLL_INTERVAL_MS",
    "MIN_POLL_INTERVAL_MS",
    "SourceAdapter",
    "SourceAdapterContractError",
    "SourceAdapterMetadata",
    "validate_poll_context",
    "validate_source_adapter",
    "BlsReleaseSourceAdapter",
    "FederalReserveSourceAdapter",
    "OfficialMacroCalendarSourceAdapter",
    "TreasuryReleaseSourceAdapter",
    "FUTU_ANOMALY_ADAPTER_KEY",
    "FutuAnomalySourceAdapter",
]

_BASE_EXPORTS = (
    "SOURCE_ADAPTER_CONTRACT_VERSION",
    "MAX_POLL_INTERVAL_MS",
    "MIN_POLL_INTERVAL_MS",
    "SourceAdapter",
    "SourceAdapterContractError",
    "SourceAdapterMetadata",
    "validate_poll_context",
    "validate_source_adapter",
)
_MACRO_EXPORTS = (
    "BlsReleaseSourceAdapter",
    "FederalReserveSourceAdapter",
    "OfficialMacroCalendarSourceAdapter",
    "TreasuryReleaseSourceAdapter",
)
_FUTU_EXPORTS = (
    "FUTU_ANOMALY_ADAPTER_KEY",
    "FutuAnomalySourceAdapter",
)
_EXPORTS = {
    **{name: (".base", name) for name in _BASE_EXPORTS},
    **{name: (".macro_official", name) for name in _MACRO_EXPORTS},
    **{name: (".futu_anomaly", name) for name in _FUTU_EXPORTS},
}


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
