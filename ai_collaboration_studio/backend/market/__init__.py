"""Read-only market-data adapters for research rooms."""

from .earnings_materials import OfficialEarningsMaterialsAdapter
from .futu_readonly import FutuUsMarketAdapter, STORAGE_SYMBOLS
from .industry_proxies import FredIndustryProxyAdapter
from .storage_service import STORAGE_MARKET, StorageResearchMarketService

__all__ = [
    "FutuUsMarketAdapter",
    "FredIndustryProxyAdapter",
    "OfficialEarningsMaterialsAdapter",
    "STORAGE_MARKET",
    "STORAGE_SYMBOLS",
    "StorageResearchMarketService",
]
