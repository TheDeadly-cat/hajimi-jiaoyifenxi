"""Fixed adapter protocol exports for source monitoring."""

from .base import (
    SOURCE_ADAPTER_CONTRACT_VERSION,
    MAX_POLL_INTERVAL_MS,
    MIN_POLL_INTERVAL_MS,
    SourceAdapter,
    SourceAdapterContractError,
    SourceAdapterMetadata,
    validate_source_adapter,
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
