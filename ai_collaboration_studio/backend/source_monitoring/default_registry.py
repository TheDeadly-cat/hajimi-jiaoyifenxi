"""Code-defined official adapters for monitoring phases 1 through 3."""

from __future__ import annotations

from ..market.official_macro import (
    OFFICIAL_MACRO_TRANSPORT_IDENTITY,
    OfficialMacroSourceClient,
)
from .adapters.company_ir import CompanyIrSourceAdapter
from .adapters.macro_official import (
    BlsReleaseSourceAdapter,
    FederalReserveSourceAdapter,
    OfficialMacroCalendarSourceAdapter,
    TreasuryReleaseSourceAdapter,
)
from .adapters.sec_filings import SecFilingsSourceAdapter
from .registry import SourceAdapterRegistry


def build_official_source_registry() -> SourceAdapterRegistry:
    """Return the production official registry without injection or polling."""

    sec = SecFilingsSourceAdapter()
    ir = CompanyIrSourceAdapter()
    federal_reserve = FederalReserveSourceAdapter()
    bls = BlsReleaseSourceAdapter()
    treasury = TreasuryReleaseSourceAdapter()
    macro_calendar = OfficialMacroCalendarSourceAdapter()
    if sec._inner_transport_mode != "sec_default_https_v1":
        raise RuntimeError("production SEC registry requires the default HTTPS transport")
    if ir._inner_transport_mode != "company_ir_default_https_v1":
        raise RuntimeError("production IR registry requires the default HTTPS transport")
    macro_adapters = (federal_reserve, bls, treasury, macro_calendar)
    for adapter in macro_adapters:
        if (
            type(adapter._client) is not OfficialMacroSourceClient
            or adapter._transport_identity != OFFICIAL_MACRO_TRANSPORT_IDENTITY
            or adapter._sealed_transport_token
            is not OfficialMacroSourceClient._default_fetch_bytes
        ):
            raise RuntimeError(
                f"production {adapter.adapter_key} registry requires the default HTTPS transport"
            )
    return SourceAdapterRegistry(
        (sec, ir, federal_reserve, bls, treasury, macro_calendar),
        official_only=True,
    )


__all__ = ["build_official_source_registry"]
