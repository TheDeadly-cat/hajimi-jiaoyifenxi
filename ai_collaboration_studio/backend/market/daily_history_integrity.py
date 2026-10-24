from __future__ import annotations

import math
from datetime import date
from typing import Any


def validated_close_rows(rows: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep supplied session positions intact: one bad row rejects the series.

    This pure calculation gate does not prove source provenance or infer an
    exchange calendar. The snapshot boundary separately validates the complete
    read-only history envelope. In particular, Friday -> Monday is legitimate.
    """
    if not isinstance(rows, list):
        return [], ["HISTORY_ROWS_INVALID"]
    normalized: list[dict[str, Any]] = []
    issues: list[str] = []
    seen_sessions: set[str] = set()
    for index, row in enumerate(rows):
        prefix = f"HISTORY_ROW_{index}"
        if not isinstance(row, dict):
            issues.append(f"{prefix}_INVALID")
            continue
        raw_time = row.get("market_time") or row.get("time_key") or row.get("time") or ""
        try:
            session = date.fromisoformat(str(raw_time)[:10]).isoformat()
        except ValueError:
            issues.append(f"{prefix}_SESSION_INVALID")
            continue
        if session in seen_sessions:
            issues.append(f"{prefix}_DUPLICATE_SESSION")
        seen_sessions.add(session)
        try:
            close = float(row.get("close"))
        except (TypeError, ValueError, OverflowError):
            close = math.nan
        if isinstance(row.get("close"), bool) or not math.isfinite(close) or close <= 0:
            issues.append(f"{prefix}_CLOSE_INVALID")
            continue
        normalized.append({**row, "close": close, "session_date": session})
    if issues:
        return [], issues
    return sorted(normalized, key=lambda row: row["session_date"]), []


def history_integrity_error(symbol: str, issues: list[str]) -> dict[str, Any]:
    return {
        "source": "futu_qfq_daily_history",
        "symbol": symbol,
        "code": "HISTORY_INVALID",
        "message": f"{symbol} 日线数据不完整或会话重复，未跳过缺口计算收益。",
        "issues": issues,
    }
