from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable
from urllib.parse import urlsplit

from .ir_releases import IR_FEEDS


OFFICIAL_EARNINGS_PACK_VERSION = "official_earnings_pack_v1"
_FISCAL_PERIOD_PATTERN = re.compile(r"FY20\d{2}-Q[1-4]")
_PACK_TEXT_FIELDS = (
    "pack_id",
    "version",
    "symbol",
    "fiscal_period",
    "release_url",
    "source_kind",
    "source_type",
    "source_tier",
    "claim_status",
    "execution_capability",
)


def is_valid_official_earnings_pack(
    pack: Any,
    *,
    expected_symbol: str,
) -> bool:
    """Validate the minimum immutable identity and safety fields for one pack."""

    if not isinstance(pack, dict) or any(
        not isinstance(pack.get(field), str) for field in _PACK_TEXT_FIELDS
    ):
        return False
    raw_symbol = pack["symbol"]
    symbol = raw_symbol.strip().upper()
    expected = str(expected_symbol or "").strip().upper()
    raw_fiscal_period = pack["fiscal_period"]
    fiscal_period = raw_fiscal_period.strip()
    raw_release_url = pack["release_url"]
    release_url = raw_release_url.strip()
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in raw_release_url):
        return False
    try:
        parsed_url = urlsplit(release_url)
        hostname = parsed_url.hostname
        port = parsed_url.port
    except (TypeError, ValueError):
        return False
    allowed_hosts = set((IR_FEEDS.get(expected) or {}).get("hosts") or [])
    expected_pack_id = "earnings_" + hashlib.sha256(
        f"{expected}|{fiscal_period}|{release_url}".encode("utf-8")
    ).hexdigest()[:24]
    return bool(
        expected
        and raw_symbol == expected
        and symbol == expected
        and raw_fiscal_period == fiscal_period
        and raw_release_url == release_url
        and pack.get("version") == OFFICIAL_EARNINGS_PACK_VERSION
        and pack["pack_id"] == expected_pack_id
        and _FISCAL_PERIOD_PATTERN.fullmatch(fiscal_period)
        and parsed_url.scheme == "https"
        and hostname in allowed_hosts
        and port in {None, 443}
        and not any(character.isspace() for character in (hostname or ""))
        and not parsed_url.username
        and not parsed_url.password
        and pack.get("source_kind") == "company_ir_release"
        and pack.get("source_type") == "company_ir"
        and pack.get("source_tier") == "primary"
        and pack.get("claim_status") == "company_statement"
        and pack.get("execution_capability") == "none"
        and pack.get("live_trading_allowed") is False
    )


def covered_official_earnings_pack_symbols(
    payload: Any,
    required_symbols: Iterable[str],
) -> set[str]:
    """Return symbols with at least one schema-valid official earnings pack."""

    if (
        not isinstance(payload, dict)
        or payload.get("version") != OFFICIAL_EARNINGS_PACK_VERSION
        or payload.get("execution_capability") != "none"
        or payload.get("live_trading_allowed") is not False
    ):
        return set()
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return set()
    required = {
        str(symbol or "").strip().upper()
        for symbol in required_symbols
        if str(symbol or "").strip()
    }
    covered: set[str] = set()
    invalid_payload = False
    for row in rows:
        if not isinstance(row, dict):
            invalid_payload = True
            continue
        row_symbol = row.get("symbol")
        packs = row.get("packs")
        if not isinstance(row_symbol, str) or not isinstance(packs, list) or not packs:
            invalid_payload = True
            continue
        symbol = row_symbol.strip().upper()
        if row_symbol != symbol:
            invalid_payload = True
            continue
        if symbol not in required or symbol in covered:
            invalid_payload = True
            continue
        if not all(
            is_valid_official_earnings_pack(pack, expected_symbol=symbol)
            for pack in packs
        ):
            invalid_payload = True
            continue
        covered.add(symbol)
    # Preserve precise per-symbol gaps when possible, while ensuring an extra
    # malformed/duplicate row can never hide behind otherwise complete coverage.
    return set() if invalid_payload and covered == required else covered


__all__ = [
    "OFFICIAL_EARNINGS_PACK_VERSION",
    "covered_official_earnings_pack_symbols",
    "is_valid_official_earnings_pack",
]
