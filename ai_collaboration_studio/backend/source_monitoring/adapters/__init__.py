"""Fixed adapter protocol exports for source monitoring."""

from .base import (
    SOURCE_ADAPTER_CONTRACT_VERSION,
    MAX_POLL_INTERVAL_MS,
    MIN_POLL_INTERVAL_MS,
    SourceAdapter,
    SourceAdapterContractError,
    SourceAdapterMetadata,
    validate_poll_context,
    validate_source_adapter,
)
from .macro_official import (
    BlsReleaseSourceAdapter,
    FederalReserveSourceAdapter,
    OfficialMacroCalendarSourceAdapter,
    TreasuryReleaseSourceAdapter,
)
from .futu_anomaly import (
    FUTU_ANOMALY_ADAPTER_KEY,
    FutuAnomalySourceAdapter,
)

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
