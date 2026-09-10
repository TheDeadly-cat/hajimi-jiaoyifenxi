"""Code-defined, construction-only registries for monitoring phases 1 through 4."""

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
from .adapters.futu_anomaly import FutuAnomalySourceAdapter
from .adapters.sec_filings import SecFilingsSourceAdapter
from .futu_readonly_broker import (
    FUTU_READONLY_BROKER_HOST,
    FUTU_READONLY_BROKER_POLICY_SHA256,
    FUTU_READONLY_BROKER_PORT,
    FutuReadOnlyBroker,
)
from .registry import SourceAdapterRegistry
from .profiles import require_profile_registry, source_profile_manifest


def build_official_source_registry(*, source_profile: str = "") -> SourceAdapterRegistry:
    """Return the production official registry without injection or polling."""

    profile = source_profile_manifest(source_profile)
    if profile is not None:
        sec_scope, ir_scope = profile["sources"]
        sec = SecFilingsSourceAdapter(
            allowed_symbols=sec_scope["symbols"], allowed_forms=sec_scope["forms"],
            per_symbol_limit=sec_scope["per_symbol_limit"], poll_interval_ms=sec_scope["poll_interval_ms"],
        )
        ir = CompanyIrSourceAdapter(
            symbols=ir_scope["symbols"], per_symbol_limit=ir_scope["per_symbol_limit"],
            poll_interval_ms=ir_scope["poll_interval_ms"],
        )
    else:
        sec = SecFilingsSourceAdapter()
        ir = CompanyIrSourceAdapter()
    if sec._inner_transport_mode != "sec_default_https_v1":
        raise RuntimeError("production SEC registry requires the default HTTPS transport")
    if ir._inner_transport_mode != "company_ir_q4_json_and_rss_default_https_v1":
        raise RuntimeError("production IR registry requires the default HTTPS transport")
    if profile is not None:
        registry = SourceAdapterRegistry((sec, ir), official_only=True)
        require_profile_registry(registry, source_profile)
        return registry
    federal_reserve = FederalReserveSourceAdapter()
    bls = BlsReleaseSourceAdapter()
    treasury = TreasuryReleaseSourceAdapter()
    macro_calendar = OfficialMacroCalendarSourceAdapter()
    if sec._inner_transport_mode != "sec_default_https_v1":
        raise RuntimeError("production SEC registry requires the default HTTPS transport")
    if ir._inner_transport_mode != "company_ir_q4_json_and_rss_default_https_v1":
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

    Construction imports no SDK, starts no subprocess, probes no socket, and
    reads no quote.  The production binding accepts only the exact managed
    isolated broker with its fixed loopback target and sealed policy.
    """

    adapter = FutuAnomalySourceAdapter()
    client = adapter._market_adapter
    quote_batch = getattr(client, "quote_batch", None)
    quote_token = getattr(quote_batch, "__func__", quote_batch)
    if (
        type(client) is not FutuReadOnlyBroker
        or type(client.host) is not str
        or client.host != FUTU_READONLY_BROKER_HOST
        or type(client.port) is not int
        or client.port != FUTU_READONLY_BROKER_PORT
        or client.mode != "managed"
        or client.policy_sha256 != FUTU_READONLY_BROKER_POLICY_SHA256
        or quote_token is not FutuReadOnlyBroker.quote_batch
    ):
        raise RuntimeError(
            "production Futu anomaly registry requires the sealed local read-only quote client"
        )
    return SourceAdapterRegistry((adapter,), official_only=False)


__all__ = ["build_futu_anomaly_registry", "build_official_source_registry"]
