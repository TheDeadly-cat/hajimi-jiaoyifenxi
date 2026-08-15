from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from datetime import datetime, timezone
from typing import Any

from .market.futu_readonly import STORAGE_SYMBOLS, validate_readonly_daily_history


PORTFOLIO_SIDES = {"LONG", "SHORT", "FLAT"}
PORTFOLIO_BUDGET_FIELDS = {
    "max_gross_exposure_pct": (100.0, 1.0, 200.0, "总敞口"),
    "max_net_exposure_pct": (100.0, 0.0, 200.0, "净敞口"),
    "max_single_name_pct": (35.0, 1.0, 100.0, "单一标的集中度"),
    "max_annualized_volatility_pct": (40.0, 0.1, 200.0, "年化波动"),
    "max_historical_var_95_1d_pct": (4.0, 0.1, 50.0, "单日历史 VaR 95%"),
    "max_drawdown_pct": (30.0, 0.1, 100.0, "历史最大回撤"),
    "max_worst_5d_loss_pct": (15.0, 0.1, 100.0, "历史最差 5 日"),
    "max_stress_loss_pct": (15.0, 0.1, 100.0, "压力情景最大损失"),
}
DEFAULT_STRESS_SCENARIOS = (
    {
        "id": "broad_storage_selloff",
        "name": "存储板块同步回撤",
        "shocks": {symbol: -10.0 for symbol in STORAGE_SYMBOLS},
    },
    {
        "id": "memory_price_downcycle",
        "name": "DRAM / NAND 下行周期",
        "shocks": {"US.MU": -18.0, "US.SNDK": -18.0, "US.WDC": -8.0, "US.STX": -8.0},
    },
    {
        "id": "hdd_demand_shock",
        "name": "HDD 需求冲击",
        "shocks": {"US.MU": -7.0, "US.SNDK": -7.0, "US.WDC": -18.0, "US.STX": -18.0},
    },
)
_PLAN_KEYS = {"name", "positions", "budgets", "stress_scenarios"}
_POSITION_KEYS = {"symbol", "side", "weight_pct", "thesis", "invalidation"}
_SCENARIO_KEYS = {"id", "name", "shocks"}
_SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,47}$")


def _finite_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 必须是数字") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} 必须是有限数字")
    return number


def default_paper_portfolio_plan() -> dict[str, Any]:
    return {
        "name": "存储产业模拟组合",
        "positions": [
            {
                "symbol": symbol,
                "side": "FLAT",
                "weight_pct": 0.0,
                "thesis": "",
                "invalidation": "",
            }
            for symbol in STORAGE_SYMBOLS
        ],
        "budgets": {
            field: default
            for field, (default, _minimum, _maximum, _label) in PORTFOLIO_BUDGET_FIELDS.items()
        },
        "stress_scenarios": [
            {
                "id": scenario["id"],
                "name": scenario["name"],
                "shocks": dict(scenario["shocks"]),
            }
            for scenario in DEFAULT_STRESS_SCENARIOS
        ],
    }


def normalize_paper_portfolio_plan(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("模拟组合必须是 JSON 对象")
    unknown = set(value) - _PLAN_KEYS
    if unknown:
        raise ValueError(f"模拟组合包含未知字段：{', '.join(sorted(unknown))}")

    name = str(value.get("name") or "").strip()[:80]
    if not name:
        raise ValueError("请填写模拟组合名称")

    raw_positions = value.get("positions")
    if not isinstance(raw_positions, list):
        raise ValueError("positions 必须是数组")
    by_symbol: dict[str, dict[str, Any]] = {}
    for index, raw_position in enumerate(raw_positions):
        if not isinstance(raw_position, dict):
            raise ValueError(f"positions[{index}] 必须是对象")
        extra = set(raw_position) - _POSITION_KEYS
        if extra:
            raise ValueError(f"positions[{index}] 包含未知字段：{', '.join(sorted(extra))}")
        symbol = str(raw_position.get("symbol") or "").strip().upper()
        if symbol and not symbol.startswith("US."):
            symbol = f"US.{symbol}"
        if symbol not in STORAGE_SYMBOLS:
            raise ValueError("模拟组合仅支持 MU、SNDK、WDC、STX")
        if symbol in by_symbol:
            raise ValueError(f"positions 中标的重复：{symbol}")
        side = str(raw_position.get("side") or "FLAT").strip().upper()
        if side not in PORTFOLIO_SIDES:
            raise ValueError(f"{symbol} 的方向必须是 LONG、SHORT 或 FLAT")
        weight = _finite_number(raw_position.get("weight_pct") or 0, f"{symbol} 模拟权重")
        if weight < 0 or weight > 100:
            raise ValueError(f"{symbol} 模拟权重必须在 0% 到 100% 之间")
        if side == "FLAT":
            weight = 0.0
        elif weight <= 0:
            raise ValueError(f"{symbol} 选择模拟做多或做空时，权重必须大于 0")
        by_symbol[symbol] = {
            "symbol": symbol,
            "side": side,
            "weight_pct": round(weight, 6),
            "thesis": str(raw_position.get("thesis") or "").strip()[:1200],
            "invalidation": str(raw_position.get("invalidation") or "").strip()[:1200],
        }

    positions = [
        by_symbol.get(symbol) or {
            "symbol": symbol,
            "side": "FLAT",
            "weight_pct": 0.0,
            "thesis": "",
            "invalidation": "",
        }
        for symbol in STORAGE_SYMBOLS
    ]
    if not any(position["side"] != "FLAT" for position in positions):
        raise ValueError("模拟组合至少需要一个非观望标的")

    raw_budgets = value.get("budgets")
    if raw_budgets is None:
        raw_budgets = {}
    if not isinstance(raw_budgets, dict):
        raise ValueError("budgets 必须是对象")
    unknown_budgets = set(raw_budgets) - set(PORTFOLIO_BUDGET_FIELDS)
    if unknown_budgets:
        raise ValueError(f"budgets 包含未知字段：{', '.join(sorted(unknown_budgets))}")
    budgets: dict[str, float] = {}
    for field, (default, minimum, maximum, label) in PORTFOLIO_BUDGET_FIELDS.items():
        number = _finite_number(raw_budgets.get(field, default), label)
        if number < minimum or number > maximum:
            raise ValueError(f"{label}预算必须在 {minimum:g}% 到 {maximum:g}% 之间")
        budgets[field] = round(number, 6)

    raw_scenarios = value.get("stress_scenarios")
    if raw_scenarios is None:
        raw_scenarios = DEFAULT_STRESS_SCENARIOS
    if not isinstance(raw_scenarios, (list, tuple)) or not 1 <= len(raw_scenarios) <= 8:
        raise ValueError("压力情景必须包含 1 到 8 项")
    scenarios: list[dict[str, Any]] = []
    scenario_ids: set[str] = set()
    for index, raw_scenario in enumerate(raw_scenarios):
        if not isinstance(raw_scenario, dict):
            raise ValueError(f"stress_scenarios[{index}] 必须是对象")
        extra = set(raw_scenario) - _SCENARIO_KEYS
        if extra:
            raise ValueError(
                f"stress_scenarios[{index}] 包含未知字段：{', '.join(sorted(extra))}"
            )
        scenario_id = str(raw_scenario.get("id") or "").strip().lower()
        if not _SLUG_PATTERN.fullmatch(scenario_id):
            raise ValueError(f"stress_scenarios[{index}].id 必须是小写英文 slug")
        if scenario_id in scenario_ids:
            raise ValueError(f"压力情景 ID 重复：{scenario_id}")
        scenario_ids.add(scenario_id)
        scenario_name = str(raw_scenario.get("name") or "").strip()[:80]
        if not scenario_name:
            raise ValueError(f"stress_scenarios[{index}].name 不能为空")
        raw_shocks = raw_scenario.get("shocks")
        if not isinstance(raw_shocks, dict):
            raise ValueError(f"stress_scenarios[{index}].shocks 必须是对象")
        unknown_symbols = set(raw_shocks) - set(STORAGE_SYMBOLS)
        if unknown_symbols:
            raise ValueError(
                f"压力情景包含非白名单标的：{', '.join(sorted(unknown_symbols))}"
            )
        shocks: dict[str, float] = {}
        for symbol in STORAGE_SYMBOLS:
            shock = _finite_number(raw_shocks.get(symbol, 0), f"{scenario_name} · {symbol} 冲击")
            if shock < -100 or shock > 100:
                raise ValueError(f"{scenario_name} 的单标的冲击必须在 -100% 到 100% 之间")
            shocks[symbol] = round(shock, 6)
        scenarios.append({"id": scenario_id, "name": scenario_name, "shocks": shocks})

    return {
        "name": name,
        "positions": positions,
        "budgets": budgets,
        "stress_scenarios": scenarios,
    }


def _clean_history(history: Any) -> list[tuple[str, float]]:
    if not isinstance(history, dict):
        return []
    by_date: dict[str, float] = {}
    for row in history.get("rows") or []:
        if not isinstance(row, dict):
            continue
        raw_time = str(row.get("market_time") or row.get("time_key") or "")
        try:
            date_key = datetime.strptime(raw_time[:10], "%Y-%m-%d").date().isoformat()
            close = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        if close > 0 and math.isfinite(close):
            by_date[date_key] = close
    return sorted(by_date.items())


def _sample_standard_deviation(values: list[float]) -> float | None:
    return statistics.stdev(values) if len(values) >= 2 else None


def _budget_check(
    code: str,
    label: str,
    actual: float | None,
    maximum: float,
    *,
    available: bool = True,
) -> dict[str, Any]:
    if not available or actual is None:
        return {
            "code": code,
            "label": label,
            "status": "UNAVAILABLE",
            "actual_pct": None,
            "maximum_pct": maximum,
        }
    return {
        "code": code,
        "label": label,
        "status": "PASS" if actual <= maximum + 1e-9 else "BREACH",
        "actual_pct": round(actual, 4),
        "maximum_pct": maximum,
    }


def evaluate_paper_portfolio(
    plan_value: Any,
    histories: dict[str, dict[str, Any]],
    *,
    evaluated_at: str | None = None,
) -> dict[str, Any]:
    plan = normalize_paper_portfolio_plan(plan_value)
    signed_weights = {
        position["symbol"]: (
            position["weight_pct"]
            if position["side"] == "LONG"
            else -position["weight_pct"]
            if position["side"] == "SHORT"
            else 0.0
        )
        for position in plan["positions"]
    }
    active_symbols = [
        symbol for symbol in STORAGE_SYMBOLS if abs(signed_weights.get(symbol, 0.0)) > 0
    ]
    gross_exposure = sum(abs(signed_weights[symbol]) for symbol in active_symbols)
    net_exposure = sum(signed_weights[symbol] for symbol in active_symbols)
    long_exposure = sum(max(0.0, signed_weights[symbol]) for symbol in active_symbols)
    short_exposure = sum(max(0.0, -signed_weights[symbol]) for symbol in active_symbols)
    max_single_name = max((abs(signed_weights[symbol]) for symbol in active_symbols), default=0.0)
    concentration_hhi = (
        sum((abs(signed_weights[symbol]) / gross_exposure) ** 2 for symbol in active_symbols) * 100
        if gross_exposure
        else 0.0
    )

    history_contracts = {
        symbol: validate_readonly_daily_history(
            histories.get(symbol),
            expected_symbol=symbol,
        )
        for symbol in active_symbols
    }
    invalid_history_symbols = [
        symbol
        for symbol in active_symbols
        if history_contracts[symbol].get("ready") is not True
    ]
    series_by_symbol = {
        symbol: dict(_clean_history(histories.get(symbol) or {}))
        if history_contracts[symbol].get("ready") is True
        else {}
        for symbol in active_symbols
    }
    missing_symbols = [
        symbol for symbol in active_symbols if len(series_by_symbol.get(symbol) or {}) < 2
    ]
    common_dates: list[str] = []
    portfolio_returns: list[float] = []
    return_dates: list[str] = []
    if not missing_symbols:
        common_dates = sorted(
            set.intersection(*(set(series_by_symbol[symbol]) for symbol in active_symbols))
        )
        for previous_date, current_date in zip(common_dates, common_dates[1:]):
            daily_return = sum(
                signed_weights[symbol] / 100
                * (
                    series_by_symbol[symbol][current_date]
                    / series_by_symbol[symbol][previous_date]
                    - 1
                )
                for symbol in active_symbols
            )
            portfolio_returns.append(daily_return)
            return_dates.append(current_date)

    sample_count = len(portfolio_returns)
    portfolio_std = _sample_standard_deviation(portfolio_returns)
    annualized_volatility = (
        portfolio_std * math.sqrt(252) * 100 if portfolio_std is not None else None
    )
    wealth = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for daily_return in portfolio_returns:
        wealth *= 1 + daily_return
        peak = max(peak, wealth)
        max_drawdown = min(max_drawdown, wealth / peak - 1)

    sorted_returns = sorted(portfolio_returns)
    quantile_index = max(0, math.ceil(0.05 * sample_count) - 1) if sample_count else 0
    fifth_percentile = sorted_returns[quantile_index] if sorted_returns else None
    rolling_five = [
        math.prod(1 + value for value in portfolio_returns[index:index + 5]) - 1
        for index in range(max(0, sample_count - 4))
    ]
    worst_index = min(range(sample_count), key=portfolio_returns.__getitem__) if sample_count else None
    metrics = {
        "annualized_volatility_pct": (
            round(annualized_volatility, 4) if annualized_volatility is not None else None
        ),
        "max_drawdown_pct": round(max_drawdown * 100, 4) if sample_count else None,
        "historical_var_95_1d_pct": (
            round(max(0.0, -fifth_percentile * 100), 4)
            if fifth_percentile is not None
            else None
        ),
        "worst_day_pct": (
            round(portfolio_returns[worst_index] * 100, 4)
            if worst_index is not None
            else None
        ),
        "worst_day_date": return_dates[worst_index] if worst_index is not None else "",
        "worst_5d_pct": round(min(rolling_five) * 100, 4) if rolling_five else None,
    }

    stress_results = []
    for scenario in plan["stress_scenarios"]:
        simulated_return = sum(
            signed_weights[symbol] / 100 * float(scenario["shocks"].get(symbol) or 0)
            for symbol in active_symbols
        )
        stress_results.append({
            "id": scenario["id"],
            "name": scenario["name"],
            "portfolio_return_pct": round(simulated_return, 4),
            "loss_pct": round(max(0.0, -simulated_return), 4),
            "shocks": dict(scenario["shocks"]),
        })
    worst_stress_loss = max(
        (result["loss_pct"] for result in stress_results),
        default=0.0,
    )

    history_ready = not missing_symbols and sample_count >= 20
    budgets = plan["budgets"]
    budget_checks = [
        _budget_check(
            "GROSS_EXPOSURE",
            "总敞口",
            gross_exposure,
            budgets["max_gross_exposure_pct"],
        ),
        _budget_check(
            "NET_EXPOSURE",
            "净敞口绝对值",
            abs(net_exposure),
            budgets["max_net_exposure_pct"],
        ),
        _budget_check(
            "SINGLE_NAME",
            "单一标的集中度",
            max_single_name,
            budgets["max_single_name_pct"],
        ),
        _budget_check(
            "ANNUALIZED_VOLATILITY",
            "年化波动",
            metrics["annualized_volatility_pct"],
            budgets["max_annualized_volatility_pct"],
            available=history_ready,
        ),
        _budget_check(
            "HISTORICAL_VAR_95_1D",
            "单日历史 VaR 95%",
            metrics["historical_var_95_1d_pct"],
            budgets["max_historical_var_95_1d_pct"],
            available=history_ready,
        ),
        _budget_check(
            "MAX_DRAWDOWN",
            "历史最大回撤",
            abs(metrics["max_drawdown_pct"]) if metrics["max_drawdown_pct"] is not None else None,
            budgets["max_drawdown_pct"],
            available=history_ready,
        ),
        _budget_check(
            "WORST_5D_LOSS",
            "历史最差 5 日",
            max(0.0, -(metrics["worst_5d_pct"] or 0.0))
            if metrics["worst_5d_pct"] is not None
            else None,
            budgets["max_worst_5d_loss_pct"],
            available=history_ready,
        ),
        _budget_check(
            "STRESS_LOSS",
            "压力情景最大损失",
            worst_stress_loss,
            budgets["max_stress_loss_pct"],
        ),
    ]
    blockers = [
        {
            "code": check["code"],
            "title": (
                f"{check['label']}缺少可验证历史数据"
                if check["status"] == "UNAVAILABLE"
                else f"{check['label']}超过预算"
            ),
            "actual_pct": check["actual_pct"],
            "maximum_pct": check["maximum_pct"],
        }
        for check in budget_checks
        if check["status"] != "PASS"
    ]

    history_payload = {
        symbol: list(series_by_symbol.get(symbol, {}).items())
        for symbol in active_symbols
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            {"plan": plan, "history": history_payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    source_errors: list[dict[str, Any]] = []
    for symbol in invalid_history_symbols:
        source_errors.append({
            "source": "futu_qfq_daily_history",
            "symbol": symbol,
            "code": "PORTFOLIO_HISTORY_CONTRACT_INVALID",
            "message": f"{symbol} 未通过只读 Futu 1d/QFQ 历史契约，已拒绝参与风险计算",
            "issues": list(history_contracts[symbol].get("issues") or []),
        })
    for symbol in missing_symbols:
        if symbol in invalid_history_symbols:
            continue
        source_errors.append({
            "source": "futu_qfq_daily_history",
            "code": "PORTFOLIO_HISTORY_INSUFFICIENT",
            "message": f"{symbol} 复权日线不足，未补造风险结果",
        })
    if not missing_symbols and sample_count < 20:
        source_errors.append({
            "source": "futu_qfq_daily_history",
            "code": "COMMON_HISTORY_INSUFFICIENT",
            "message": "共同历史收益少于 20 个交易日，不能通过风险门",
        })

    state = (
        "ready"
        if history_ready and sample_count >= 120
        else "limited"
        if history_ready
        else "offline"
    )
    timestamp = evaluated_at or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {
        "version": "paper_portfolio_risk_v1",
        "state": state,
        "evaluated_at": timestamp,
        "source": (
            "futu_qfq_daily_history"
            if not invalid_history_symbols
            else "unverified_history_rejected"
        ),
        "history_contract_ready": not invalid_history_symbols,
        "history_contracts": history_contracts,
        "input_fingerprint": fingerprint,
        "active_symbols": active_symbols,
        "sample_count": sample_count,
        "first_return_date": return_dates[0] if return_dates else "",
        "last_return_date": return_dates[-1] if return_dates else "",
        "exposures": {
            "gross_exposure_pct": round(gross_exposure, 4),
            "net_exposure_pct": round(net_exposure, 4),
            "long_exposure_pct": round(long_exposure, 4),
            "short_exposure_pct": round(short_exposure, 4),
            "unallocated_long_budget_pct": round(max(0.0, 100.0 - long_exposure), 4),
            "max_single_name_pct": round(max_single_name, 4),
            "concentration_hhi_pct": round(concentration_hhi, 4),
        },
        "metrics": metrics,
        "stress_results": stress_results,
        "budget_checks": budget_checks,
        "risk_gate": {
            "status": "PASS" if history_ready and not blockers else "BLOCKED",
            "ready": bool(history_ready and not blockers),
            "blockers": blockers,
        },
        "source_errors": source_errors,
        "interpretation": (
            "这是用户定义纸面权重在富途复权历史和显式压力冲击下的确定性风险复算；"
            "不是订单、真实仓位、投资建议或未来损失保证。"
        ),
        "execution_capability": "none",
        "live_trading_allowed": False,
    }
