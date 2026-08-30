"""Code-defined official adapters for the first monitoring rollout."""

from __future__ import annotations

from .adapters.company_ir import CompanyIrSourceAdapter
from .adapters.sec_filings import SecFilingsSourceAdapter
from .registry import SourceAdapterRegistry


def build_official_source_registry() -> SourceAdapterRegistry:
    """Return the production SEC/IR registry without injection or polling."""

    sec = SecFilingsSourceAdapter()
    ir = CompanyIrSourceAdapter()
    if sec._inner_transport_mode != "sec_default_https_v1":
        raise RuntimeError("production SEC registry requires the default HTTPS transport")
    if ir._inner_transport_mode != "company_ir_default_https_v1":
        raise RuntimeError("production IR registry requires the default HTTPS transport")
    return SourceAdapterRegistry((sec, ir), official_only=True)


__all__ = ["build_official_source_registry"]
