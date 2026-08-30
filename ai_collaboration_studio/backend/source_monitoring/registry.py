"""Closed registry for code-defined source adapters."""

from __future__ import annotations

from typing import Any, Iterator

from .adapters.base import SourceAdapterMetadata, validate_source_adapter
from .contracts import SourceMonitoringContractError, normalize_adapter_key


class SourceAdapterRegistryError(SourceMonitoringContractError):
    """Raised when a registry invariant is not satisfied."""


class SourceAdapterRegistry:
    """An immutable adapter set with revalidated safety metadata."""

    def __init__(
        self,
        adapters: Any = (),
        *,
        official_only: Any = True,
    ) -> None:
        if type(official_only) is not bool:
            raise SourceAdapterRegistryError(
                "SOURCE_ADAPTER_REGISTRY_MODE_INVALID",
                "official_only must be a native boolean",
            )
        if type(adapters) not in {list, tuple}:
            raise SourceAdapterRegistryError(
                "SOURCE_ADAPTER_REGISTRY_INPUT_INVALID",
                "adapters must be a native list or tuple",
            )
        self._official_only = official_only
        self._adapters: dict[str, Any] = {}
        self._metadata: dict[str, SourceAdapterMetadata] = {}
        for adapter in adapters:
            metadata = validate_source_adapter(adapter)
            if official_only and metadata.official_source is not True:
                raise SourceAdapterRegistryError(
                    "SOURCE_ADAPTER_NON_OFFICIAL_FORBIDDEN",
                    f"adapter {metadata.adapter_key} is not an official source",
                )
            if metadata.adapter_key in self._adapters:
                raise SourceAdapterRegistryError(
                    "SOURCE_ADAPTER_DUPLICATE",
                    f"adapter key {metadata.adapter_key} is already registered",
                )
            self._adapters[metadata.adapter_key] = adapter
            self._metadata[metadata.adapter_key] = metadata

    @property
    def official_only(self) -> bool:
        return self._official_only

    @property
    def adapter_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def __len__(self) -> int:
        return len(self._adapters)

    def __iter__(self) -> Iterator[Any]:
        for adapter_key in self.adapter_keys:
            yield self.require(adapter_key)

    def get(self, adapter_key: Any) -> Any | None:
        clean_key = normalize_adapter_key(adapter_key)
        if clean_key not in self._adapters:
            return None
        return self.require(clean_key)

    def require(self, adapter_key: Any) -> Any:
        clean_key = normalize_adapter_key(adapter_key)
        adapter = self._adapters.get(clean_key)
        if adapter is None:
            raise SourceAdapterRegistryError(
                "SOURCE_ADAPTER_NOT_REGISTERED",
                f"adapter {clean_key} is not registered",
            )
        current = validate_source_adapter(adapter)
        frozen = self._metadata[clean_key]
        if current != frozen:
            raise SourceAdapterRegistryError(
                "SOURCE_ADAPTER_METADATA_DRIFT",
                f"adapter {clean_key} metadata changed after registration",
            )
        if self._official_only and current.official_source is not True:
            raise SourceAdapterRegistryError(
                "SOURCE_ADAPTER_NON_OFFICIAL_FORBIDDEN",
                f"adapter {clean_key} is not an official source",
            )
        return adapter

    def metadata_for(self, adapter_key: Any) -> SourceAdapterMetadata:
        clean_key = normalize_adapter_key(adapter_key)
        self.require(clean_key)
        metadata = self._metadata[clean_key]
        return SourceAdapterMetadata(**metadata.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "official_only": self.official_only,
            "adapter_count": len(self),
            "adapters": [
                self.metadata_for(adapter_key).to_dict()
                for adapter_key in self.adapter_keys
            ],
        }


__all__ = [
    "SourceAdapterRegistry",
    "SourceAdapterRegistryError",
]
