from __future__ import annotations

import copy
import math
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Mapping, Sequence

from .market.futu_readonly import STORAGE_SYMBOLS


WALK_FORWARD_ENGINE_VERSION = "walk_forward_engine_v3"
WALK_FORWARD_CONFIG_VERSION = "walk_forward_config_v2"
WALK_FORWARD_RESULT_VERSION = "walk_forward_result_v3"
WALK_FORWARD_INPUT_SNAPSHOT_VERSION = "walk_forward_input_snapshot_v2"
PAPER_FRICTION_MODEL_VERSION = "paper_friction_model_v1"
PAPER_LIQUIDITY_PROXY_VERSION = "paper_liquidity_proxy_v1"
STORAGE_FRICTION_SCENARIOS_VERSION = "storage_friction_scenarios_v1"
UNFILLABLE_POLICY = "block_scenario_no_partial_fill"

_FILLABLE = "FILLABLE"
_UNFILLABLE = "UNFILLABLE"
_SIDES = {"LONG", "SHORT", "FLAT"}


class PaperFrictionValidationError(ValueError):
    """Fail-closed validation error for local paper-friction inputs."""


@dataclass(frozen=True, slots=True)
class _Scenario:
    scenario_id: str
    label: str
    paper_reference_notional_usd: float
    commission_bps_per_side: float
    entry_slippage_bps: float
    exit_slippage_bps: float
    short_borrow_fee_bps_annual: float
    max_daily_turnover_participation_pct: float


# These are server-owned research assumptions, not live broker rates. Frozen
# dataclasses in a tuple prevent callers from replacing or mutating the
# canonical definitions. Public accessors always return independent copies.
_SCENARIOS = (
    _Scenario("baseline", "基准摩擦", 1_000_000.0, 10.0, 5.0, 5.0, 300.0, 2.0),
    _Scenario("stressed", "压力摩擦", 5_000_000.0, 15.0, 25.0, 25.0, 1_500.0, 1.0),
    _Scenario("severe", "极端摩擦", 10_000_000.0, 25.0, 75.0, 75.0, 3_000.0, 0.25),
)
_SCENARIOS_BY_ID = {scenario.scenario_id: scenario for scenario in _SCENARIOS}


def get_storage_friction_scenarios() -> dict[str, Any]:
    """Return a deep copy of the immutable server-owned scenario set."""

    payload = {
        "version": STORAGE_FRICTION_SCENARIOS_VERSION,
        "friction_model_version": PAPER_FRICTION_MODEL_VERSION,
        "liquidity_proxy_version": PAPER_LIQUIDITY_PROXY_VERSION,
        "assumption_scope": "paper_research_only",
        "server_owned": True,
        "custom_overrides_allowed": False,
        "live_broker_rates": False,
        "actual_execution_observed": False,
        "scenarios": [asdict(scenario) for scenario in _SCENARIOS],
    }
    return copy.deepcopy(payload)


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise PaperFrictionValidationError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PaperFrictionValidationError(
            f"{label} must be a finite number"
        ) from exc
    if not math.isfinite(number):
        raise PaperFrictionValidationError(f"{label} must be a finite number")
    return number


def _positive_number_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _normalize_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if symbol and not symbol.startswith("US."):
        symbol = f"US.{symbol}"
    if symbol not in STORAGE_SYMBOLS:
        raise PaperFrictionValidationError(
            "symbol must be one of US.MU, US.SNDK, US.WDC, US.STX"
        )
    return symbol


def _normalize_positions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise PaperFrictionValidationError("positions must be a sequence")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_position in enumerate(value):
        if not isinstance(raw_position, Mapping):
            raise PaperFrictionValidationError(
                f"positions[{index}] must be an object"
            )
        symbol = _normalize_symbol(raw_position.get("symbol"))
        if symbol in seen:
            raise PaperFrictionValidationError(f"duplicate position: {symbol}")
        seen.add(symbol)
        side = str(raw_position.get("side") or "").strip().upper()
        if side not in _SIDES:
            raise PaperFrictionValidationError(
                f"{symbol} side must be LONG, SHORT, or FLAT"
            )
        weight_pct = _finite_number(
            raw_position.get("weight_pct"),
            f"{symbol} weight_pct",
        )
        if weight_pct < 0 or weight_pct > 100:
            raise PaperFrictionValidationError(
                f"{symbol} weight_pct must be between 0 and 100"
            )
        if side == "FLAT" and weight_pct != 0:
            raise PaperFrictionValidationError(
                f"{symbol} FLAT position must have zero weight"
            )
        if side != "FLAT" and weight_pct <= 0:
            raise PaperFrictionValidationError(
                f"{symbol} active position must have positive weight"
            )
        normalized.append(
            {
                "symbol": symbol,
                "side": side,
                "weight_pct": weight_pct,
            }
        )
    return normalized


def _row_date(row: Mapping[str, Any], symbol: str, index: int) -> date:
    raw = row.get("date") or row.get("market_time") or row.get("time_key")
    text = str(raw or "").strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise PaperFrictionValidationError(
            f"{symbol} rows[{index}] must have a valid ISO session date"
        ) from exc


def _extract_rows(value: Any, symbol: str) -> list[dict[str, Any]]:
    candidate = value.get("rows") if isinstance(value, Mapping) else value
    if not isinstance(candidate, Sequence) or isinstance(candidate, (str, bytes)):
        raise PaperFrictionValidationError(f"{symbol} rows must be a sequence")
    if len(candidate) < 2:
        raise PaperFrictionValidationError(
            f"{symbol} requires at least entry and exit rows"
        )

    normalized: list[dict[str, Any]] = []
    previous_date: date | None = None
    for index, raw_row in enumerate(candidate):
        if not isinstance(raw_row, Mapping):
            raise PaperFrictionValidationError(
                f"{symbol} rows[{index}] must be an object"
            )
        session = _row_date(raw_row, symbol, index)
        if previous_date is not None and session <= previous_date:
            raise PaperFrictionValidationError(
                f"{symbol} session dates must be strictly increasing"
            )
        close = _finite_number(raw_row.get("close"), f"{symbol} close")
        if close <= 0:
            raise PaperFrictionValidationError(f"{symbol} close must be positive")
        normalized.append(
            {
                "date": session,
                "close": close,
                # Invalid or non-positive liquidity observations are retained
                # as unavailable so the scenario fails closed as UNFILLABLE.
                "turnover": _positive_number_or_none(raw_row.get("turnover")),
                "volume": _positive_number_or_none(raw_row.get("volume")),
            }
        )
        previous_date = session
    return normalized


def _normalize_rows_by_symbol(
    value: Any,
    active_symbols: set[str],
) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, Mapping):
        raise PaperFrictionValidationError("rows_by_symbol must be an object")

    normalized_keys: dict[str, Any] = {}
    for raw_symbol, rows in value.items():
        symbol = _normalize_symbol(raw_symbol)
        if symbol in normalized_keys:
            raise PaperFrictionValidationError(f"duplicate history: {symbol}")
        normalized_keys[symbol] = rows

    missing = sorted(active_symbols - set(normalized_keys))
    if missing:
        raise PaperFrictionValidationError(
            f"missing active-symbol rows: {', '.join(missing)}"
        )

    normalized = {
        symbol: _extract_rows(normalized_keys[symbol], symbol)
        for symbol in sorted(active_symbols)
    }
    if not normalized:
        # All-FLAT input still needs a calendar to make the local result
        # explicit and auditable. Use the first supplied storage history.
        if not normalized_keys:
            raise PaperFrictionValidationError("rows_by_symbol cannot be empty")
        symbol = sorted(normalized_keys)[0]
        normalized[symbol] = _extract_rows(normalized_keys[symbol], symbol)

    reference_dates = [row["date"] for row in next(iter(normalized.values()))]
    for symbol, rows in normalized.items():
        if [row["date"] for row in rows] != reference_dates:
            raise PaperFrictionValidationError(
                f"{symbol} session calendar does not align"
            )
    return normalized


def _liquidity_proxy(row: Mapping[str, Any]) -> tuple[float | None, str | None]:
    turnover = row.get("turnover")
    if isinstance(turnover, (int, float)) and turnover > 0:
        return float(turnover), "reported_turnover"
    volume = row.get("volume")
    close = row.get("close")
    if (
        isinstance(volume, (int, float))
        and volume > 0
        and isinstance(close, (int, float))
        and close > 0
    ):
        return float(close) * float(volume), "close_times_volume_proxy"
    return None, None


def _round_money(value: float) -> float:
    return round(value, 8)


def _round_pct(value: float) -> float:
    return round(value, 8)


def _scenario_payload(scenario: _Scenario) -> dict[str, Any]:
    payload = asdict(scenario)
    payload.update(
        {
            "assumption_scope": "paper_research_only",
            "server_owned": True,
            "custom_overrides_allowed": False,
            "live_broker_rates": False,
            "actual_execution_observed": False,
        }
    )
    return payload


def _base_result(
    scenario: _Scenario,
    liquidity_checks: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "version": PAPER_FRICTION_MODEL_VERSION,
        "engine_version": WALK_FORWARD_ENGINE_VERSION,
        "config_version": WALK_FORWARD_CONFIG_VERSION,
        "result_version": WALK_FORWARD_RESULT_VERSION,
        "input_snapshot_version": WALK_FORWARD_INPUT_SNAPSHOT_VERSION,
        "scenario_set_version": STORAGE_FRICTION_SCENARIOS_VERSION,
        "scenario_id": scenario.scenario_id,
        "scenario": copy.deepcopy(_scenario_payload(scenario)),
        "unfillable_policy": UNFILLABLE_POLICY,
        "liquidity_model": {
            "version": PAPER_LIQUIDITY_PROXY_VERSION,
            "capacity_is_proxy": True,
            "proxy_precedence": "positive_turnover_else_close_times_volume",
            "capacity_formula": "liquidity_proxy_times_max_participation_pct",
            "entry_and_exit_checked": True,
            "actual_execution_observed": False,
        },
        "liquidity_checks": liquidity_checks,
        "partial_fills_allowed": False,
        "position_shrinking_allowed": False,
        "date_shifting_allowed": False,
        "provider_calls_total": 0,
        "openai_calls": 0,
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
        "out_of_sample_claim": False,
        "actual_execution_observed": False,
    }


def apply_paper_friction(
    rows_by_symbol: Any,
    positions: Any,
    *,
    scenario_id: str,
) -> dict[str, Any]:
    """Apply one immutable paper-friction scenario to a fixed-notional fold.

    The first row is the paper entry close and the last row is the paper exit
    close. Liquidity is only a conservative capacity proxy; this function does
    not observe fills and cannot place, route, resize, or defer transactions.
    It performs no network, provider, account, database, or execution work.
    """

    if not isinstance(scenario_id, str) or scenario_id not in _SCENARIOS_BY_ID:
        raise PaperFrictionValidationError(
            "scenario_id must select baseline, stressed, or severe; "
            "custom scenarios are not allowed"
        )
    scenario = _SCENARIOS_BY_ID[scenario_id]
    normalized_positions = _normalize_positions(positions)
    active_positions = [
        position for position in normalized_positions if position["side"] != "FLAT"
    ]
    active_symbols = {position["symbol"] for position in active_positions}
    normalized_rows = _normalize_rows_by_symbol(rows_by_symbol, active_symbols)

    capital = scenario.paper_reference_notional_usd
    shares_by_symbol: dict[str, float] = {}
    entry_notional_by_symbol: dict[str, float] = {}
    exit_notional_by_symbol: dict[str, float] = {}
    for position in active_positions:
        symbol = position["symbol"]
        entry_close = normalized_rows[symbol][0]["close"]
        entry_notional = capital * position["weight_pct"] / 100
        shares = entry_notional / entry_close
        shares_by_symbol[symbol] = shares
        entry_notional_by_symbol[symbol] = entry_notional
        exit_notional_by_symbol[symbol] = (
            shares * normalized_rows[symbol][-1]["close"]
        )

    liquidity_checks: list[dict[str, Any]] = []
    participation_rate = scenario.max_daily_turnover_participation_pct / 100
    for position in active_positions:
        symbol = position["symbol"]
        rows = normalized_rows[symbol]
        for phase, row, required_notional in (
            ("entry", rows[0], entry_notional_by_symbol[symbol]),
            ("exit", rows[-1], exit_notional_by_symbol[symbol]),
        ):
            liquidity_notional, proxy_basis = _liquidity_proxy(row)
            capacity = (
                liquidity_notional * participation_rate
                if liquidity_notional is not None
                else None
            )
            fillable = (
                capacity is not None
                and required_notional
                <= capacity + max(1e-9, capacity * 1e-12)
            )
            reason_code = (
                None
                if fillable
                else "LIQUIDITY_PROXY_UNAVAILABLE"
                if capacity is None
                else "CAPACITY_EXCEEDED"
            )
            capacity_shortfall = (
                max(0.0, required_notional - capacity)
                if capacity is not None
                else required_notional
            )
            liquidity_checks.append(
                {
                    "symbol": symbol,
                    "side": position["side"],
                    "phase": phase,
                    "session_date": row["date"].isoformat(),
                    "required_notional_usd": _round_money(required_notional),
                    "liquidity_proxy_notional_usd": (
                        _round_money(liquidity_notional)
                        if liquidity_notional is not None
                        else None
                    ),
                    "proxy_basis": proxy_basis,
                    "max_participation_pct": (
                        scenario.max_daily_turnover_participation_pct
                    ),
                    "capacity_usd": (
                        _round_money(capacity) if capacity is not None else None
                    ),
                    "capacity_gap_usd": _round_money(capacity_shortfall),
                    "capacity_ratio": (
                        round(capacity / required_notional, 8)
                        if capacity is not None and required_notional > 0
                        else None
                    ),
                    "fillable": fillable,
                    "reason_code": reason_code,
                    "capacity_is_proxy": True,
                    "actual_execution_observed": False,
                }
            )

    result = _base_result(scenario, liquidity_checks)
    if any(not check["fillable"] for check in liquidity_checks):
        result.update(
            {
                "status": _UNFILLABLE,
                "blocked": True,
                "reason_code": _UNFILLABLE,
                "equity_path": None,
                "costs": None,
                "gross_return_pct": None,
                "net_return_before_slippage_pct": None,
                "net_return_pct": None,
                "max_drawdown_pct": None,
                "positive": None,
            }
        )
        return result

    commission_rate = scenario.commission_bps_per_side / 10_000
    entry_slippage_rate = scenario.entry_slippage_bps / 10_000
    exit_slippage_rate = scenario.exit_slippage_bps / 10_000
    borrow_rate = scenario.short_borrow_fee_bps_annual / 10_000

    entry_commission = sum(entry_notional_by_symbol.values()) * commission_rate
    exit_commission = sum(exit_notional_by_symbol.values()) * commission_rate
    entry_slippage = sum(entry_notional_by_symbol.values()) * entry_slippage_rate
    exit_slippage = sum(exit_notional_by_symbol.values()) * exit_slippage_rate

    reference_rows = next(iter(normalized_rows.values()))
    cumulative_borrow = 0.0
    borrow_by_symbol = {
        position["symbol"]: 0.0
        for position in active_positions
        if position["side"] == "SHORT"
    }
    equity_path: list[dict[str, Any]] = []
    for row_index, reference_row in enumerate(reference_rows):
        if row_index:
            previous_date = reference_rows[row_index - 1]["date"]
            current_date = reference_row["date"]
            calendar_days = (current_date - previous_date).days
            for position in active_positions:
                if position["side"] != "SHORT":
                    continue
                symbol = position["symbol"]
                shares = shares_by_symbol[symbol]
                previous_close = normalized_rows[symbol][row_index - 1]["close"]
                current_close = normalized_rows[symbol][row_index]["close"]
                average_short_notional = (
                    shares * (previous_close + current_close) / 2
                )
                interval_fee = (
                    average_short_notional
                    * borrow_rate
                    * calendar_days
                    / 365
                )
                borrow_by_symbol[symbol] += interval_fee
                cumulative_borrow += interval_fee

        gross_equity = capital
        for position in active_positions:
            symbol = position["symbol"]
            shares = shares_by_symbol[symbol]
            entry_close = normalized_rows[symbol][0]["close"]
            current_close = normalized_rows[symbol][row_index]["close"]
            direction = 1 if position["side"] == "LONG" else -1
            gross_equity += direction * shares * (current_close - entry_close)

        is_exit = row_index == len(reference_rows) - 1
        before_slippage = (
            gross_equity
            - entry_commission
            - cumulative_borrow
            - (exit_commission if is_exit else 0.0)
        )
        net_equity = (
            before_slippage
            - entry_slippage
            - (exit_slippage if is_exit else 0.0)
        )
        equity_path.append(
            {
                "session_date": reference_row["date"].isoformat(),
                "gross_equity_usd": _round_money(gross_equity),
                "cumulative_short_borrow_cost_usd": _round_money(
                    cumulative_borrow
                ),
                "equity_before_slippage_usd": _round_money(before_slippage),
                "net_equity_usd": _round_money(net_equity),
            }
        )

    net_values = [row["net_equity_usd"] for row in equity_path]
    peak = capital
    max_drawdown = 0.0
    for equity in net_values:
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)

    gross_final = equity_path[-1]["gross_equity_usd"]
    before_slippage_final = equity_path[-1]["equity_before_slippage_usd"]
    net_final = equity_path[-1]["net_equity_usd"]
    gross_return_pct = (gross_final / capital - 1) * 100
    before_slippage_return_pct = (before_slippage_final / capital - 1) * 100
    net_return_pct = (net_final / capital - 1) * 100

    result.update(
        {
            "status": _FILLABLE,
            "blocked": False,
            "reason_code": None,
            "equity_path": equity_path,
            "costs": {
                "entry_commission_usd": _round_money(entry_commission),
                "exit_commission_usd": _round_money(exit_commission),
                "commission_cost_usd": _round_money(
                    entry_commission + exit_commission
                ),
                "entry_slippage_cost_usd": _round_money(entry_slippage),
                "exit_slippage_cost_usd": _round_money(exit_slippage),
                "slippage_cost_usd": _round_money(
                    entry_slippage + exit_slippage
                ),
                "short_borrow_cost_usd": _round_money(cumulative_borrow),
                "short_borrow_cost_by_symbol_usd": {
                    symbol: _round_money(fee)
                    for symbol, fee in sorted(borrow_by_symbol.items())
                },
                "total_friction_cost_usd": _round_money(
                    entry_commission
                    + exit_commission
                    + entry_slippage
                    + exit_slippage
                    + cumulative_borrow
                ),
            },
            "gross_return_pct": _round_pct(gross_return_pct),
            "net_return_before_slippage_pct": _round_pct(
                before_slippage_return_pct
            ),
            "net_return_pct": _round_pct(net_return_pct),
            "max_drawdown_pct": _round_pct(max_drawdown * 100),
            "positive": net_return_pct > 0,
        }
    )
    return result


__all__ = [
    "PAPER_FRICTION_MODEL_VERSION",
    "PAPER_LIQUIDITY_PROXY_VERSION",
    "PaperFrictionValidationError",
    "STORAGE_FRICTION_SCENARIOS_VERSION",
    "UNFILLABLE_POLICY",
    "WALK_FORWARD_CONFIG_VERSION",
    "WALK_FORWARD_ENGINE_VERSION",
    "WALK_FORWARD_INPUT_SNAPSHOT_VERSION",
    "WALK_FORWARD_RESULT_VERSION",
    "apply_paper_friction",
    "get_storage_friction_scenarios",
]
