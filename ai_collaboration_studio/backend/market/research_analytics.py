from __future__ import annotations

import math
import statistics
from datetime import datetime
from typing import Any


DEFAULT_HORIZONS = (1, 5, 20)


def _clean_series(history: dict[str, Any]) -> list[tuple[str, float]]:
    by_date: dict[str, float] = {}
    for row in history.get("rows") or []:
        raw_time = str(row.get("market_time") or row.get("time_key") or "")
        try:
            date_key = datetime.strptime(raw_time[:10], "%Y-%m-%d").date().isoformat()
            close = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        if close > 0 and math.isfinite(close):
            by_date[date_key] = close
    return sorted(by_date.items())


def _non_overlapping_returns(series: list[tuple[str, float]], horizon: int) -> list[dict[str, Any]]:
    if horizon <= 0 or len(series) <= horizon:
        return []
    windows: list[dict[str, Any]] = []
    exit_index = len(series) - 1
    while exit_index - horizon >= 0:
        entry_index = exit_index - horizon
        entry_date, entry_close = series[entry_index]
        exit_date, exit_close = series[exit_index]
        return_pct = (exit_close / entry_close - 1) * 100
        windows.append({
            "entry_date": entry_date,
            "exit_date": exit_date,
            "return_pct": round(return_pct, 8),
        })
        exit_index = entry_index
    windows.reverse()
    return windows


def historical_base_rates(
    histories: dict[str, dict[str, Any]],
    *,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    threshold_pct: float = 2.0,
) -> dict[str, Any]:
    clean_threshold = max(0.0, float(threshold_pct))
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for symbol, history in sorted(histories.items()):
        series = _clean_series(history if isinstance(history, dict) else {})
        if len(series) < 2:
            errors.append({
                "source": "futu_qfq_daily_history",
                "code": "HISTORY_INSUFFICIENT",
                "message": f"{symbol} 历史收盘数据不足",
            })
        for horizon in horizons:
            windows = _non_overlapping_returns(series, int(horizon))
            returns = [float(window["return_pct"]) for window in windows]
            sample_count = len(returns)
            up_hits = sum(value >= clean_threshold for value in returns)
            down_hits = sum(value <= -clean_threshold for value in returns)
            neutral_hits = sum(abs(value) < clean_threshold for value in returns)
            rows.append({
                "symbol": symbol,
                "horizon_days": int(horizon),
                "threshold_pct": clean_threshold,
                "sample_count": sample_count,
                "up_base_rate_pct": round(up_hits / sample_count * 100, 2) if sample_count else None,
                "down_base_rate_pct": round(down_hits / sample_count * 100, 2) if sample_count else None,
                "neutral_base_rate_pct": round(neutral_hits / sample_count * 100, 2) if sample_count else None,
                "average_return_pct": round(statistics.fmean(returns), 4) if returns else None,
                "median_return_pct": round(statistics.median(returns), 4) if returns else None,
                "first_entry_date": windows[0]["entry_date"] if windows else "",
                "last_exit_date": windows[-1]["exit_date"] if windows else "",
                "quality": "ready" if sample_count >= 20 else "limited" if sample_count else "unavailable",
            })
    ready_rows = sum(1 for row in rows if row["quality"] in {"ready", "limited"})
    return {
        "version": "historical_base_rate_v1",
        "source": "futu_qfq_daily_history",
        "state": "ready" if rows and ready_rows == len(rows) else "degraded" if ready_rows else "offline",
        "threshold_pct": clean_threshold,
        "rows": rows,
        "source_errors": errors,
        "interpretation": "这是固定阈值在互不重叠历史窗口中的基准命中频率，不是策略回测、交易建议或未来胜率。",
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


def _sample_standard_deviation(values: list[float]) -> float | None:
    return statistics.stdev(values) if len(values) >= 2 else None


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_scale = math.sqrt(sum((value - left_mean) ** 2 for value in left))
    right_scale = math.sqrt(sum((value - right_mean) ** 2 for value in right))
    denominator = left_scale * right_scale
    return numerator / denominator if denominator else None


def equal_weight_portfolio_risk(histories: dict[str, dict[str, Any]]) -> dict[str, Any]:
    series_by_symbol = {symbol: dict(_clean_series(history)) for symbol, history in histories.items()}
    symbols = sorted(symbol for symbol, series in series_by_symbol.items() if len(series) >= 2)
    if len(symbols) < 2:
        return {
            "version": "equal_weight_risk_v1",
            "source": "futu_qfq_daily_history",
            "state": "offline",
            "symbols": symbols,
            "sample_count": 0,
            "source_errors": [{
                "source": "futu_qfq_daily_history",
                "code": "PORTFOLIO_HISTORY_INSUFFICIENT",
                "message": "至少需要两个标的的共同历史区间",
            }],
            "interpretation": "仅为等权模拟组合的历史风险描述，不包含真实仓位或下单能力。",
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

    common_dates = sorted(set.intersection(*(set(series_by_symbol[symbol]) for symbol in symbols)))
    returns_by_symbol: dict[str, list[float]] = {symbol: [] for symbol in symbols}
    portfolio_returns: list[float] = []
    return_dates: list[str] = []
    for previous_date, current_date in zip(common_dates, common_dates[1:]):
        daily_returns = []
        for symbol in symbols:
            previous_close = series_by_symbol[symbol][previous_date]
            current_close = series_by_symbol[symbol][current_date]
            daily_return = current_close / previous_close - 1
            returns_by_symbol[symbol].append(daily_return)
            daily_returns.append(daily_return)
        portfolio_returns.append(statistics.fmean(daily_returns))
        return_dates.append(current_date)

    sample_count = len(portfolio_returns)
    portfolio_std = _sample_standard_deviation(portfolio_returns)
    annualized_volatility = portfolio_std * math.sqrt(252) * 100 if portfolio_std is not None else None
    wealth = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in portfolio_returns:
        wealth *= 1 + value
        peak = max(peak, wealth)
        max_drawdown = min(max_drawdown, wealth / peak - 1)

    sorted_returns = sorted(portfolio_returns)
    quantile_index = max(0, math.ceil(0.05 * len(sorted_returns)) - 1) if sorted_returns else 0
    fifth_percentile = sorted_returns[quantile_index] if sorted_returns else None
    rolling_five = [
        math.prod(1 + value for value in portfolio_returns[index:index + 5]) - 1
        for index in range(max(0, sample_count - 4))
    ]
    correlations: list[dict[str, Any]] = []
    finite_correlations: list[float] = []
    for left_index, left in enumerate(symbols):
        for right in symbols[left_index + 1:]:
            correlation = _pearson(returns_by_symbol[left], returns_by_symbol[right])
            correlations.append({
                "left": left,
                "right": right,
                "correlation": round(correlation, 4) if correlation is not None else None,
            })
            if correlation is not None:
                finite_correlations.append(correlation)

    worst_index = min(range(sample_count), key=portfolio_returns.__getitem__) if sample_count else None
    return {
        "version": "equal_weight_risk_v1",
        "source": "futu_qfq_daily_history",
        "state": "ready" if sample_count >= 60 and len(symbols) == len(histories) else "limited" if sample_count else "offline",
        "symbols": symbols,
        "weighting": "equal_weight",
        "weight_pct": round(100 / len(symbols), 4),
        "sample_count": sample_count,
        "first_return_date": return_dates[0] if return_dates else "",
        "last_return_date": return_dates[-1] if return_dates else "",
        "annualized_volatility_pct": round(annualized_volatility, 4) if annualized_volatility is not None else None,
        "max_drawdown_pct": round(max_drawdown * 100, 4) if sample_count else None,
        "historical_var_95_1d_pct": round(max(0.0, -fifth_percentile * 100), 4) if fifth_percentile is not None else None,
        "worst_day_pct": round(portfolio_returns[worst_index] * 100, 4) if worst_index is not None else None,
        "worst_day_date": return_dates[worst_index] if worst_index is not None else "",
        "worst_5d_pct": round(min(rolling_five) * 100, 4) if rolling_five else None,
        "average_pair_correlation": round(statistics.fmean(finite_correlations), 4) if finite_correlations else None,
        "max_pair_correlation": round(max(finite_correlations), 4) if finite_correlations else None,
        "correlations": correlations,
        "source_errors": [],
        "interpretation": "仅为四只白名单股票的等权历史模拟风险；不使用真实账户、仓位或交易权限，也不代表未来损失上限。",
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


def build_research_analytics(histories: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": "storage_research_analytics_v1",
        "base_rates": historical_base_rates(histories),
        "portfolio_risk": equal_weight_portfolio_risk(histories),
        "execution_capability": "none",
        "live_trading_allowed": False,
    }
