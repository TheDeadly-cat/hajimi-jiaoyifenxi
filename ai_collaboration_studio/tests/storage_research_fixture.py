from __future__ import annotations

import hashlib
from typing import Any


STORAGE_SYMBOLS = ("US.MU", "US.SNDK", "US.WDC", "US.STX")
FIXTURE_IR_HOSTS = {
    "US.MU": "investors.micron.com",
    "US.SNDK": "investor.sandisk.com",
    "US.WDC": "investor.wdc.com",
    "US.STX": "investors.seagate.com",
}


def _fixture_earnings_pack(symbol: str) -> dict[str, Any]:
    fiscal_period = "FY2026-Q3"
    release_url = f"https://{FIXTURE_IR_HOSTS[symbol]}/fixture/fy2026-q3"
    pack_id = "earnings_" + hashlib.sha256(
        f"{symbol}|{fiscal_period}|{release_url}".encode("utf-8")
    ).hexdigest()[:24]
    return {
        "pack_id": pack_id,
        "version": "official_earnings_pack_v1",
        "symbol": symbol,
        "fiscal_period": fiscal_period,
        "release_url": release_url,
        "source_kind": "company_ir_release",
        "source_type": "company_ir",
        "source_tier": "primary",
        "claim_status": "company_statement",
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


def ready_storage_research_evidence(
    *,
    captured_at: str = "2026-07-20T20:00:00Z",
    technical_as_of: str = "2026-07-20 00:00:00",
) -> dict[str, Any]:
    """Small, fully local evidence contract for orchestration tests."""

    return {
        "version": "storage_market_evidence_v6",
        "state": "ready",
        "captured_at": captured_at,
        "technical": {
            "source": "futu_qfq_daily_history",
            "rows": [
                {
                    "symbol": symbol,
                    "as_of": technical_as_of,
                    "quality": "ready",
                    "sample_count": 120,
                }
                for symbol in STORAGE_SYMBOLS
            ],
            "source_errors": [],
        },
        "official_filings": {
            "source": "sec_edgar_submissions",
            "rows": [
                {"symbol": symbol, "filings": [{"form": "10-Q"}]}
                for symbol in STORAGE_SYMBOLS
            ],
            "source_errors": [],
        },
        "company_ir_releases": {
            "source": "official_company_ir",
            "rows": [
                {"symbol": symbol, "releases": [{"title": "fixture"}]}
                for symbol in STORAGE_SYMBOLS
            ],
            "source_errors": [],
        },
        "official_earnings_packs": {
            "version": "official_earnings_pack_v1",
            "state": "ready",
            "source": "official_company_ir_and_sec",
            "rows": [
                {
                    "symbol": symbol,
                    "packs": [_fixture_earnings_pack(symbol)],
                }
                for symbol in STORAGE_SYMBOLS
            ],
            "source_errors": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
        },
    }
