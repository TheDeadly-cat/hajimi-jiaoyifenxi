"""Lazy public exports for read-only research market adapters.

Keeping package import side-effect free lets narrow official-source tooling
import ``backend.market.official_macro`` without loading Futu configuration or
constructing the global storage service. Public attribute compatibility is
preserved through PEP 562 lazy resolution.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "FutuUsMarketAdapter",
    "FredIndustryProxyAdapter",
    "OfficialEarningsMaterialsAdapter",
    "STORAGE_MARKET",
    "STORAGE_SYMBOLS",
    "StorageResearchMarketService",
]

_EXPORTS = {
    "FutuUsMarketAdapter": (".futu_readonly", "FutuUsMarketAdapter"),
    "FredIndustryProxyAdapter": (".industry_proxies", "FredIndustryProxyAdapter"),
    "OfficialEarningsMaterialsAdapter": (
        ".earnings_materials",
        "OfficialEarningsMaterialsAdapter",
    ),
    "STORAGE_MARKET": (".storage_service", "STORAGE_MARKET"),
    "STORAGE_SYMBOLS": (".futu_readonly", "STORAGE_SYMBOLS"),
    "StorageResearchMarketService": (
        ".storage_service",
        "StorageResearchMarketService",
    ),
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
