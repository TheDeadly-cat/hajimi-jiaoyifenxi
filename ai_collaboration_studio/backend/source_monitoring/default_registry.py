"""Code-defined, construction-only registries for monitoring phases 1 through 4."""

from __future__ import annotations

import ipaddress

from ..market.official_macro import (
    OFFICIAL_MACRO_TRANSPORT_IDENTITY,
    OfficialMacroSourceClient,
)
from ..market.futu_readonly import FutuUsMarketAdapter
from .adapters.company_ir import CompanyIrSourceAdapter
from .adapters.macro_official import (
    BlsReleaseSourceAdapter,
    FederalReserveSourceAdapter,
    OfficialMacroCalendarSourceAdapter,
    TreasuryReleaseSourceAdapter,
)
from .adapters.sec_filings import SecFilingsSourceAdapter
from .adapters.futu_anomaly import FutuAnomalySourceAdapter
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


def build_futu_anomaly_registry() -> SourceAdapterRegistry:
    """Return the separate, default-disabled local Futu anomaly registry.

    Construction imports no SDK, probes no socket, and reads no quote.  The
    production binding accepts only a literal loopback OpenD host and the exact
    existing read-only quote client.
    """

    adapter = FutuAnomalySourceAdapter()
    client = adapter._market_adapter
    try:
        address = ipaddress.ip_address(client.host)
    except (AttributeError, ValueError) as exc:
        raise RuntimeError(
            "production Futu anomaly registry requires a literal loopback host"
        ) from exc
    quote_batch = getattr(client, "quote_batch", None)
    quote_token = getattr(quote_batch, "__func__", quote_batch)
    if (
        type(client) is not FutuUsMarketAdapter
        or type(client.host) is not str
        or client.host != str(address)
        or address.is_loopback is not True
        or type(client.port) is not int
        or not 1 <= client.port <= 65_535
        or quote_token is not FutuUsMarketAdapter.quote_batch
    ):
        raise RuntimeError(
            "production Futu anomaly registry requires the sealed local read-only quote client"
        )
    return SourceAdapterRegistry((adapter,), official_only=False)


__all__ = ["build_futu_anomaly_registry", "build_official_source_registry"]
