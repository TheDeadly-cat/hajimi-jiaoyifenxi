"""Closed, code-owned monitoring scopes shared by preview, runtime and UI."""

from __future__ import annotations

from typing import Any

from .contracts import SourceMonitoringContractError, canonical_sha256


SEC_MICRON_TRIAL_PROFILE = "sec_micron_trial_v1"
SOURCE_PROFILE_IDS = ("", SEC_MICRON_TRIAL_PROFILE)


def source_profile_manifest(profile_id: str) -> dict[str, Any] | None:
    if type(profile_id) is not str or profile_id not in SOURCE_PROFILE_IDS:
        raise SourceMonitoringContractError(
            "SOURCE_MONITORING_PROFILE_INVALID", "source profile must be a code-defined name",
        )
    if not profile_id:
        return None
    manifest = {
        "version": "source_monitoring_profile_v1",
        "profile_id": SEC_MICRON_TRIAL_PROFILE,
        "label": "SEC + Micron 官方来源试用",
        "initial_mode": "seed_only",
        "sources": [
            {"adapter_key": "sec_filings", "symbols": ["US.NVDA"], "forms": ["8-K"],
             "format": "sec_submissions_recent", "history_limit": 1000,
             "per_symbol_limit": 3, "poll_interval_ms": 300_000},
            {"adapter_key": "company_ir", "symbols": ["US.MU"], "forms": [],
             "format": "micron_q4_public_json_v1", "history_limit": 30,
             "per_symbol_limit": 8, "poll_interval_ms": 300_000},
        ],
        "model_calls_allowed": False,
        "execution_capability": "none",
        "live_trading_allowed": False,
    }
    return {**manifest, "scope_sha256": canonical_sha256(manifest)}


def require_profile_registry(registry: Any, profile_id: str) -> None:
    """Reject injected or drifted scopes before a source request can begin."""

    manifest = source_profile_manifest(profile_id)
    if manifest is None:
        return
    from .adapters.company_ir import CompanyIrSourceAdapter
    from .adapters.sec_filings import SecFilingsSourceAdapter
    from .registry import SourceAdapterRegistry

    def reject() -> None:
        raise SourceMonitoringContractError(
            "SOURCE_MONITORING_PROFILE_SCOPE_MISMATCH",
            "the active registry differs from the fixed SEC/Micron trial scope",
        )

    if (
        type(registry) is not SourceAdapterRegistry
        or registry.official_only is not True
        or set(registry.adapter_keys) != {row["adapter_key"] for row in manifest["sources"]}
    ):
        reject()
    for row in manifest["sources"]:
        adapter = registry.require(row["adapter_key"])
        if row["adapter_key"] == "sec_filings":
            if (
                type(adapter) is not SecFilingsSourceAdapter
                or adapter.allowed_symbols != tuple(row["symbols"])
                or adapter.allowed_forms != tuple(row["forms"])
            ):
                reject()
        elif (
            type(adapter) is not CompanyIrSourceAdapter
            or adapter.symbols != tuple(row["symbols"])
            or adapter._format_for("US.MU") != "q4_json"
        ):
            reject()
        if (
            adapter.per_symbol_limit != row["per_symbol_limit"]
            or adapter.poll_interval_ms != row["poll_interval_ms"]
        ):
            reject()
        adapter._assert_config_seal()


__all__ = ["SEC_MICRON_TRIAL_PROFILE", "SOURCE_PROFILE_IDS", "source_profile_manifest", "require_profile_registry"]
