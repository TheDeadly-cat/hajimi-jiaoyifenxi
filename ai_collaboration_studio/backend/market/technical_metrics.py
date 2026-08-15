from __future__ import annotations

import math
import statistics
from typing import Any


TECHNICAL_FORMULA_VERSION = "technical_metrics_v1"


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _rounded(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def _period_return(closes: list[float], periods: int) -> float | None:
    if len(closes) <= periods or closes[-periods - 1] == 0:
        return None
    return (closes[-1] / closes[-periods - 1] - 1) * 100


def _simple_average(values: list[float], periods: int) -> float | None:
    if len(values) < periods:
        return None
    return sum(values[-periods:]) / periods


def _rsi14(closes: list[float]) -> float | None:
    if len(closes) < 15:
        return None
    changes = [closes[index] - closes[index - 1] for index in range(len(closes) - 14, len(closes))]
    average_gain = sum(max(change, 0.0) for change in changes) / 14
    average_loss = sum(max(-change, 0.0) for change in changes) / 14
    if average_gain == 0 and average_loss == 0:
        return 50.0
    if average_loss == 0:
        return 100.0
    relative_strength = average_gain / average_loss
    return 100 - 100 / (1 + relative_strength)


def _realized_volatility_20d(closes: list[float]) -> float | None:
    if len(closes) < 21:
        return None
    window = closes[-21:]
    log_returns = [math.log(window[index] / window[index - 1]) for index in range(1, len(window))]
    return statistics.stdev(log_returns) * math.sqrt(252) * 100


def _max_drawdown(closes: list[float]) -> float | None:
    if not closes:
        return None
    peak = closes[0]
    worst = 0.0
    for close in closes:
        peak = max(peak, close)
        worst = min(worst, close / peak - 1)
    return worst * 100


def calculate_technical_metrics(symbol: str, history_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate deterministic, backward-looking metrics from adjusted daily bars."""

    ordered_rows = sorted(
        (dict(row) for row in history_rows if isinstance(row, dict)),
        key=lambda row: str(row.get("time") or row.get("market_time") or ""),
    )
    valid_rows: list[dict[str, Any]] = []
    for row in ordered_rows:
        close = _finite(row.get("close"))
        if close is None or close <= 0:
            continue
        valid_rows.append({**row, "close": close, "volume": _finite(row.get("volume"))})

    closes = [float(row["close"]) for row in valid_rows]
    latest = valid_rows[-1] if valid_rows else {}
    sma20 = _simple_average(closes, 20)
    sma50 = _simple_average(closes, 50)
    previous_volumes = [
        float(row["volume"])
        for row in valid_rows[-21:-1]
        if row.get("volume") is not None and float(row["volume"]) >= 0
    ]
    latest_volume = _finite(latest.get("volume"))
    average_volume_20d = sum(previous_volumes) / len(previous_volumes) if len(previous_volumes) == 20 else None
    latest_volume_ratio = (
        latest_volume / average_volume_20d
        if latest_volume is not None and average_volume_20d not in (None, 0)
        else None
    )
    metrics = {
        "return_1d_pct": _rounded(_period_return(closes, 1)),
        "return_5d_pct": _rounded(_period_return(closes, 5)),
        "return_20d_pct": _rounded(_period_return(closes, 20)),
        "sma20": _rounded(sma20),
        "sma50": _rounded(sma50),
        "price_vs_sma20_pct": _rounded((closes[-1] / sma20 - 1) * 100) if closes and sma20 else None,
        "rsi14": _rounded(_rsi14(closes)),
        "realized_volatility_20d_annualized_pct": _rounded(_realized_volatility_20d(closes)),
        "average_volume_20d": _rounded(average_volume_20d),
        "latest_volume_ratio_20d": _rounded(latest_volume_ratio),
        "max_drawdown_pct": _rounded(_max_drawdown(closes)),
    }
    quality = "ready" if len(closes) >= 50 else "limited" if len(closes) >= 2 else "unavailable"
    return {
        "symbol": str(symbol or "").upper(),
        "source": "futu_qfq_daily_history",
        "formula_version": TECHNICAL_FORMULA_VERSION,
        "as_of": latest.get("market_time") or latest.get("time") or None,
        "sample_count": len(closes),
        "quality": quality,
        "missing_metrics": [name for name, value in metrics.items() if value is None],
        **metrics,
    }
