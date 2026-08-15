from __future__ import annotations

import hashlib
import json
import math
import statistics
from copy import deepcopy
from datetime import date, datetime, timezone
from typing import Any

from .market.futu_readonly import STORAGE_SYMBOLS
from .walk_forward_friction import (
    PAPER_FRICTION_MODEL_VERSION,
    PAPER_LIQUIDITY_PROXY_VERSION,
    STORAGE_FRICTION_SCENARIOS_VERSION,
    UNFILLABLE_POLICY,
    apply_paper_friction,
    get_storage_friction_scenarios,
)


ENGINE_VERSION_V1 = "walk_forward_engine_v1"
ENGINE_VERSION_V2 = "walk_forward_engine_v2"
ENGINE_VERSION_V3 = "walk_forward_engine_v3"
ENGINE_VERSION_V4 = "walk_forward_engine_v4"
CONFIG_VERSION_V1 = "walk_forward_config_v1"
CONFIG_VERSION_V2 = "walk_forward_config_v2"
CONFIG_VERSION_V3 = "walk_forward_config_v3"
RESULT_VERSION_V1 = "walk_forward_result_v1"
RESULT_VERSION_V2 = "walk_forward_result_v2"
RESULT_VERSION_V3 = "walk_forward_result_v3"
RESULT_VERSION_V4 = "walk_forward_result_v4"
INPUT_SNAPSHOT_VERSION_V1 = "walk_forward_input_snapshot_v1"
INPUT_SNAPSHOT_VERSION_V2 = "walk_forward_input_snapshot_v2"
INPUT_SNAPSHOT_VERSION_V3 = "walk_forward_input_snapshot_v3"

# Compatibility aliases intentionally remain on the established v2/v1
# defaults. Newer engines are selected only by their explicit config version.
ENGINE_VERSION = ENGINE_VERSION_V2
PLAN_VERSION = "walk_forward_plan_v1"
PLAN_VERSION_V2 = "walk_forward_plan_v2"
CONFIG_VERSION = CONFIG_VERSION_V1
RESULT_VERSION = RESULT_VERSION_V2
INPUT_SNAPSHOT_VERSION = INPUT_SNAPSHOT_VERSION_V1
STRATEGY_RULE_CONTRACT_VERSION = "strategy_rule_contract_v1"
RULE_ID = "cross_sectional_total_return_rank_v1"
FEASIBILITY_VERSION = "walk_forward_feasibility_v1"
MINIMUM_INDEPENDENT_FOLDS = 20
INSUFFICIENT_WINDOWS_REASON = "INSUFFICIENT_NON_OVERLAPPING_TEST_WINDOWS"

_PLAN_KEYS = {
    "version",
    "portfolio_id",
    "portfolio_version",
    "strategy_created_at",
    "mode",
    "strategy_provenance",
    "out_of_sample_claim",
    "evaluation_as_of_date",
    "data_snapshot_cutoff",
    "name",
    "positions",
}
_PLAN_V2_KEYS = _PLAN_KEYS | {
    "future_performance_claim",
    "retrospective_dataset",
    "source_user_decision_id",
    "decision_anchor_sha256",
    "source_decision_head_sequence",
    "source_decision_head_sha256",
}
_POSITION_KEYS = {"symbol", "side", "weight_pct", "thesis", "invalidation"}
_CONFIG_V1_KEYS = {
    "version",
    "train_days",
    "test_days",
    "step_days",
    "transaction_cost_bps",
    "price_adjustment",
}
_CONFIG_V2_KEYS = {
    "version",
    "train_days",
    "test_days",
    "step_days",
    "price_adjustment",
    "friction_scenario_set",
    "unfillable_policy",
}
_CONFIG_V3_KEYS = _CONFIG_V2_KEYS | {"strategy_rule_contract"}
_STRATEGY_RULE_CONTRACT_KEYS = {
    "version",
    "rule_id",
    "universe",
    "signal",
    "fit_scope",
    "ranking",
    "long_count",
    "short_count",
    "long_budget_pct",
    "short_budget_pct",
    "weighting",
    "rebalance",
    "execution_lag_trading_days",
    "test_data_excluded_from_fit",
    "contract_selected_before_full_history",
    "out_of_sample_claim",
    "future_performance_claim",
    "partial_fills_allowed",
    "position_shrinking_allowed",
    "date_shifting_allowed",
    "source_portfolio_id",
    "source_portfolio_version",
    "source_positions_sha256",
}
_FRICTION_SCENARIO_IDS = ("baseline", "stressed", "severe")
_SIDES = {"LONG", "SHORT", "FLAT"}
_MODE = "retroactive_fixed_plan_replay"
_STRATEGY_PROVENANCE = "current_plan_retroactive"
_MODE_V2 = "fold_train_only_next_session_test_replay"
_STRATEGY_PROVENANCE_V2 = "server_whitelisted_fold_trained_rule"


class WalkForwardValidationError(ValueError):
    """Fail-closed validation error for local historical inputs."""


class WalkForwardFeasibilityError(WalkForwardValidationError):
    """Pre-fold failure carrying a deterministic, JSON-safe diagnostic."""

    def __init__(self, diagnostic: dict[str, Any]) -> None:
        self.diagnostic = diagnostic
        super().__init__(
            "walk-forward 可行性失败 "
            f"[{diagnostic['reason_code']}]：当前共同历史 "
            f"{diagnostic['common_trading_days']} 行，在 train/test/step="
            f"{diagnostic['train_days']}/{diagnostic['test_days']}/"
            f"{diagnostic['step_days']} 下最多只有 "
            f"{diagnostic['maximum_non_overlapping_test_fold_count']} 个"
            "非重叠测试窗口，最低要求 "
            f"{diagnostic['minimum_non_overlapping_test_folds']} 个；"
            f"至少需要 {diagnostic['minimum_common_trading_days']} 行"
            f"（还缺 {diagnostic['history_row_shortfall']} 行）。"
            "未生成 fold，也不会缩短 1/5/20 日等测试期限或补造历史。"
        )


def _finite_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise WalkForwardValidationError(f"{label} 必须是数字") from exc
    if not math.isfinite(number):
        raise WalkForwardValidationError(f"{label} 必须是有限数字")
    return number


def _strict_integer(value: Any, label: str, *, minimum: int) -> int:
    if isinstance(value, bool):
        raise WalkForwardValidationError(f"{label} 必须是整数")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise WalkForwardValidationError(f"{label} 必须是整数") from exc
    try:
        numeric_value = float(value)
    except (TypeError, ValueError) as exc:
        raise WalkForwardValidationError(f"{label} 必须是整数") from exc
    if not math.isfinite(numeric_value) or numeric_value != number:
        raise WalkForwardValidationError(f"{label} 必须是整数")
    if number < minimum:
        raise WalkForwardValidationError(f"{label} 必须至少为 {minimum}")
    return number


def _iso_date(value: Any, label: str) -> str:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise WalkForwardValidationError(
            f"{label} 必须是 YYYY-MM-DD 日期"
        ) from exc


def _utc_timestamp(value: Any, label: str) -> str:
    if isinstance(value, bool):
        raise WalkForwardValidationError(f"{label} 必须是带时区时间")
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)) or float(value) <= 0:
            raise WalkForwardValidationError(f"{label} 必须是有效时间")
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            parsed = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise WalkForwardValidationError(
                f"{label} 必须是有效时间"
            ) from exc
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise WalkForwardValidationError(
                f"{label} 必须是带时区 ISO 时间"
            ) from exc
        if parsed.tzinfo is None:
            raise WalkForwardValidationError(f"{label} 必须包含时区")
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _timestamp_ms(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise WalkForwardValidationError(f"{label} 必须是毫秒时间戳")
    try:
        timestamp = int(value)
        numeric_value = float(value)
    except (TypeError, ValueError) as exc:
        raise WalkForwardValidationError(f"{label} 必须是毫秒时间戳") from exc
    if (
        not math.isfinite(numeric_value)
        or numeric_value != timestamp
        or timestamp < 1_000_000_000_000
    ):
        raise WalkForwardValidationError(f"{label} 必须是毫秒时间戳")
    return timestamp


def _normalize_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if symbol and not symbol.startswith("US."):
        symbol = f"US.{symbol}"
    if symbol not in STORAGE_SYMBOLS:
        raise WalkForwardValidationError("仅支持 US.MU、US.SNDK、US.WDC、US.STX")
    return symbol


def _normalize_source_positions(value: Any) -> list[dict[str, Any]]:
    """Canonicalize the source portfolio constraints used by the v4 rule.

    These positions determine only the number of long/short selections and
    each side's notional budget.  They are never replayed as the fold's test
    holdings.
    """

    if not isinstance(value, list) or not value:
        raise WalkForwardValidationError("source positions must be a non-empty array")
    positions_by_symbol: dict[str, dict[str, Any]] = {}
    for index, raw_position in enumerate(value):
        if not isinstance(raw_position, dict):
            raise WalkForwardValidationError(
                f"source positions[{index}] must be an object"
            )
        unknown = set(raw_position) - _POSITION_KEYS
        if unknown:
            raise WalkForwardValidationError(
                f"source positions[{index}] contains unknown fields: "
                f"{', '.join(sorted(unknown))}"
            )
        symbol = _normalize_symbol(raw_position.get("symbol"))
        if symbol in positions_by_symbol:
            raise WalkForwardValidationError(
                f"source positions contains duplicate symbol: {symbol}"
            )
        side = str(raw_position.get("side") or "").strip().upper()
        if side not in _SIDES:
            raise WalkForwardValidationError(
                f"{symbol} source side must be LONG, SHORT, or FLAT"
            )
        if isinstance(raw_position.get("weight_pct"), bool):
            raise WalkForwardValidationError(
                f"{symbol} source weight_pct must be a finite number"
            )
        weight = _finite_number(
            raw_position.get("weight_pct"),
            f"{symbol} source weight_pct",
        )
        if weight < 0 or weight > 100:
            raise WalkForwardValidationError(
                f"{symbol} source weight_pct must be between 0 and 100"
            )
        if side == "FLAT" and weight != 0:
            raise WalkForwardValidationError(
                f"{symbol} source weight_pct must be 0 when side is FLAT"
            )
        if side != "FLAT" and weight <= 0:
            raise WalkForwardValidationError(
                f"{symbol} source weight_pct must be positive for LONG or SHORT"
            )
        positions_by_symbol[symbol] = {
            "symbol": symbol,
            "side": side,
            "weight_pct": round(weight, 8),
            "thesis": str(raw_position.get("thesis") or "").strip(),
            "invalidation": str(raw_position.get("invalidation") or "").strip(),
        }

    positions = [
        positions_by_symbol.get(symbol)
        or {
            "symbol": symbol,
            "side": "FLAT",
            "weight_pct": 0.0,
            "thesis": "",
            "invalidation": "",
        }
        for symbol in STORAGE_SYMBOLS
    ]
    if not any(position["side"] != "FLAT" for position in positions):
        raise WalkForwardValidationError(
            "source positions require at least one LONG or SHORT constraint"
        )
    return positions


def _source_portfolio_identity(value: dict[str, Any]) -> tuple[str, int]:
    raw_portfolio_id = value.get("portfolio_id") or value.get("id")
    if not isinstance(raw_portfolio_id, str):
        raise WalkForwardValidationError("source portfolio id must be a string")
    portfolio_id = raw_portfolio_id.strip()
    if not portfolio_id or len(portfolio_id) > 160:
        raise WalkForwardValidationError(
            "source portfolio id must be a non-empty string up to 160 characters"
        )
    raw_version = value.get("portfolio_version")
    if raw_version is None:
        raw_version = value.get("version")
    portfolio_version = _strict_integer(
        raw_version,
        "source portfolio version",
        minimum=1,
    )
    return portfolio_id, portfolio_version


def build_strategy_rule_contract(
    plan_or_portfolio: Any,
    rule_id: Any,
) -> dict[str, Any]:
    """Build the only server-whitelisted v4 strategy-rule contract.

    ``plan_or_portfolio`` may be a normalized walk-forward plan (using
    ``portfolio_id``/``portfolio_version``) or a persisted paper portfolio
    (using ``id``/integer ``version``).  No market history is accepted here,
    making it explicit that this contract is not evidence of prospective rule
    selection.
    """

    if not isinstance(plan_or_portfolio, dict):
        raise WalkForwardValidationError(
            "plan_or_portfolio must be a JSON object"
        )
    if not isinstance(rule_id, str):
        raise WalkForwardValidationError(
            f"rule_id must be the server-whitelisted {RULE_ID}"
        )
    normalized_rule_id = rule_id.strip()
    if normalized_rule_id != RULE_ID:
        raise WalkForwardValidationError(
            f"rule_id must be the server-whitelisted {RULE_ID}"
        )
    source_portfolio_id, source_portfolio_version = _source_portfolio_identity(
        plan_or_portfolio
    )
    source_positions = _normalize_source_positions(
        plan_or_portfolio.get("positions")
    )
    long_positions = [
        position for position in source_positions if position["side"] == "LONG"
    ]
    short_positions = [
        position for position in source_positions if position["side"] == "SHORT"
    ]
    long_count = len(long_positions)
    short_count = len(short_positions)
    if long_count + short_count > len(STORAGE_SYMBOLS):
        raise WalkForwardValidationError(
            "strategy long_count + short_count must not exceed the universe"
        )
    if long_count + short_count < 1:
        raise WalkForwardValidationError(
            "strategy requires at least one long or short selection"
        )
    return {
        "version": STRATEGY_RULE_CONTRACT_VERSION,
        "rule_id": RULE_ID,
        "universe": list(STORAGE_SYMBOLS),
        "signal": "training_window_total_return",
        "fit_scope": "fold_training_window_only",
        "ranking": "descending_return_then_symbol_ascending",
        "long_count": long_count,
        "short_count": short_count,
        "long_budget_pct": round(
            sum(float(position["weight_pct"]) for position in long_positions),
            8,
        ),
        "short_budget_pct": round(
            sum(float(position["weight_pct"]) for position in short_positions),
            8,
        ),
        "weighting": "equal_notional_within_side",
        "rebalance": "fold_entry_only",
        "execution_lag_trading_days": 1,
        "test_data_excluded_from_fit": True,
        "contract_selected_before_full_history": False,
        "out_of_sample_claim": False,
        "future_performance_claim": False,
        "partial_fills_allowed": False,
        "position_shrinking_allowed": False,
        "date_shifting_allowed": False,
        "source_portfolio_id": source_portfolio_id,
        "source_portfolio_version": source_portfolio_version,
        "source_positions_sha256": _sha256(source_positions),
    }


def _normalize_strategy_rule_contract(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WalkForwardValidationError(
            "strategy_rule_contract must be a JSON object"
        )
    unknown = set(value) - _STRATEGY_RULE_CONTRACT_KEYS
    missing = _STRATEGY_RULE_CONTRACT_KEYS - set(value)
    if unknown:
        raise WalkForwardValidationError(
            "strategy_rule_contract contains unknown fields: "
            + ", ".join(sorted(unknown))
        )
    if missing:
        raise WalkForwardValidationError(
            "strategy_rule_contract is missing fields: "
            + ", ".join(sorted(missing))
        )

    exact_values = {
        "version": STRATEGY_RULE_CONTRACT_VERSION,
        "rule_id": RULE_ID,
        "universe": list(STORAGE_SYMBOLS),
        "signal": "training_window_total_return",
        "fit_scope": "fold_training_window_only",
        "ranking": "descending_return_then_symbol_ascending",
        "weighting": "equal_notional_within_side",
        "rebalance": "fold_entry_only",
        "execution_lag_trading_days": 1,
        "test_data_excluded_from_fit": True,
        "contract_selected_before_full_history": False,
        "out_of_sample_claim": False,
        "future_performance_claim": False,
        "partial_fills_allowed": False,
        "position_shrinking_allowed": False,
        "date_shifting_allowed": False,
    }
    for field, expected in exact_values.items():
        if value.get(field) != expected or (
            isinstance(expected, bool) and value.get(field) is not expected
        ) or (
            field == "execution_lag_trading_days"
            and isinstance(value.get(field), bool)
        ):
            raise WalkForwardValidationError(
                f"strategy_rule_contract.{field} must be {expected!r}"
            )

    long_count = _strict_integer(
        value.get("long_count"),
        "strategy_rule_contract.long_count",
        minimum=0,
    )
    short_count = _strict_integer(
        value.get("short_count"),
        "strategy_rule_contract.short_count",
        minimum=0,
    )
    if long_count + short_count < 1 or long_count + short_count > len(
        STORAGE_SYMBOLS
    ):
        raise WalkForwardValidationError(
            "strategy_rule_contract long_count + short_count must be between 1 and 4"
        )

    budgets: dict[str, float] = {}
    for side, count in (("long", long_count), ("short", short_count)):
        field = f"{side}_budget_pct"
        if isinstance(value.get(field), bool):
            raise WalkForwardValidationError(
                f"strategy_rule_contract.{field} must be a finite number"
            )
        budget = _finite_number(value.get(field), f"strategy_rule_contract.{field}")
        if budget < 0 or budget > count * 100:
            raise WalkForwardValidationError(
                f"strategy_rule_contract.{field} is outside its source-position bound"
            )
        if (count == 0 and budget != 0) or (count > 0 and budget <= 0):
            raise WalkForwardValidationError(
                f"strategy_rule_contract.{field} does not match {side}_count"
            )
        budgets[field] = round(budget, 8)

    if not isinstance(value.get("source_portfolio_id"), str):
        raise WalkForwardValidationError(
            "strategy_rule_contract.source_portfolio_id is invalid"
        )
    source_portfolio_id = value["source_portfolio_id"].strip()
    if not source_portfolio_id or len(source_portfolio_id) > 160:
        raise WalkForwardValidationError(
            "strategy_rule_contract.source_portfolio_id is invalid"
        )
    if not isinstance(value.get("source_positions_sha256"), str):
        raise WalkForwardValidationError(
            "strategy_rule_contract.source_positions_sha256 must be lowercase SHA-256"
        )
    source_positions_sha256 = value["source_positions_sha256"]
    if (
        len(source_positions_sha256) != 64
        or source_positions_sha256.lower() != source_positions_sha256
        or any(character not in "0123456789abcdef" for character in source_positions_sha256)
    ):
        raise WalkForwardValidationError(
            "strategy_rule_contract.source_positions_sha256 must be lowercase SHA-256"
        )

    return {
        **exact_values,
        "long_count": long_count,
        "short_count": short_count,
        **budgets,
        "source_portfolio_id": source_portfolio_id,
        "source_portfolio_version": _strict_integer(
            value.get("source_portfolio_version"),
            "strategy_rule_contract.source_portfolio_version",
            minimum=1,
        ),
        "source_positions_sha256": source_positions_sha256,
    }


def _normalize_plan(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WalkForwardValidationError("plan 必须是 JSON 对象")
    unknown = set(value) - _PLAN_KEYS
    if unknown:
        raise WalkForwardValidationError(
            f"plan 包含未知字段：{', '.join(sorted(unknown))}"
        )
    if value.get("version") != PLAN_VERSION:
        raise WalkForwardValidationError(f"plan.version 必须是 {PLAN_VERSION}")

    portfolio_id = str(value.get("portfolio_id") or "").strip()
    if not portfolio_id or len(portfolio_id) > 160:
        raise WalkForwardValidationError("plan.portfolio_id 必须绑定有效模拟组合")
    portfolio_version = _strict_integer(
        value.get("portfolio_version"),
        "plan.portfolio_version",
        minimum=1,
    )
    strategy_created_at = _timestamp_ms(
        value.get("strategy_created_at"),
        "plan.strategy_created_at",
    )
    if value.get("mode") != _MODE:
        raise WalkForwardValidationError(f"plan.mode 必须是 {_MODE}")
    if value.get("strategy_provenance") != _STRATEGY_PROVENANCE:
        raise WalkForwardValidationError(
            f"plan.strategy_provenance 必须是 {_STRATEGY_PROVENANCE}"
        )
    if value.get("out_of_sample_claim") is not False:
        raise WalkForwardValidationError(
            "回放当前计划时 plan.out_of_sample_claim 必须为 false"
        )
    evaluation_as_of_date = _iso_date(
        value.get("evaluation_as_of_date"),
        "plan.evaluation_as_of_date",
    )
    data_snapshot_cutoff = _iso_date(
        value.get("data_snapshot_cutoff"),
        "plan.data_snapshot_cutoff",
    )
    if data_snapshot_cutoff >= evaluation_as_of_date:
        raise WalkForwardValidationError(
            "plan.data_snapshot_cutoff 必须早于 evaluation_as_of_date"
        )

    raw_positions = value.get("positions")
    if not isinstance(raw_positions, list) or not raw_positions:
        raise WalkForwardValidationError("plan.positions 必须是非空数组")

    positions_by_symbol: dict[str, dict[str, Any]] = {}
    for index, raw_position in enumerate(raw_positions):
        if not isinstance(raw_position, dict):
            raise WalkForwardValidationError(f"positions[{index}] 必须是对象")
        unknown_position_fields = set(raw_position) - _POSITION_KEYS
        if unknown_position_fields:
            raise WalkForwardValidationError(
                f"positions[{index}] 包含未知字段："
                f"{', '.join(sorted(unknown_position_fields))}"
            )
        symbol = _normalize_symbol(raw_position.get("symbol"))
        if symbol in positions_by_symbol:
            raise WalkForwardValidationError(f"plan.positions 标的重复：{symbol}")
        side = str(raw_position.get("side") or "").strip().upper()
        if side not in _SIDES:
            raise WalkForwardValidationError(
                f"{symbol} 的 side 必须是 LONG、SHORT 或 FLAT"
            )
        weight = _finite_number(
            raw_position.get("weight_pct"),
            f"{symbol} weight_pct",
        )
        if weight < 0 or weight > 100:
            raise WalkForwardValidationError(
                f"{symbol} weight_pct 必须在 0 到 100 之间"
            )
        if side == "FLAT" and weight != 0:
            raise WalkForwardValidationError(f"{symbol} 为 FLAT 时 weight_pct 必须为 0")
        if side != "FLAT" and weight <= 0:
            raise WalkForwardValidationError(
                f"{symbol} 为 LONG 或 SHORT 时 weight_pct 必须大于 0"
            )
        positions_by_symbol[symbol] = {
            "symbol": symbol,
            "side": side,
            "weight_pct": round(weight, 8),
            "thesis": str(raw_position.get("thesis") or "").strip(),
            "invalidation": str(raw_position.get("invalidation") or "").strip(),
        }

    normalized_positions = [
        positions_by_symbol.get(symbol)
        or {
            "symbol": symbol,
            "side": "FLAT",
            "weight_pct": 0.0,
            "thesis": "",
            "invalidation": "",
        }
        for symbol in STORAGE_SYMBOLS
    ]
    if not any(position["side"] != "FLAT" for position in normalized_positions):
        raise WalkForwardValidationError("plan 至少需要一个 LONG 或 SHORT 纸面权重")

    return {
        "version": PLAN_VERSION,
        "portfolio_id": portfolio_id,
        "portfolio_version": portfolio_version,
        "strategy_created_at": strategy_created_at,
        "mode": _MODE,
        "strategy_provenance": _STRATEGY_PROVENANCE,
        "out_of_sample_claim": False,
        "evaluation_as_of_date": evaluation_as_of_date,
        "data_snapshot_cutoff": data_snapshot_cutoff,
        "name": str(value.get("name") or "").strip(),
        "positions": normalized_positions,
    }


def _normalize_plan_v2(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WalkForwardValidationError("plan must be a JSON object")
    unknown = set(value) - _PLAN_V2_KEYS
    if unknown:
        raise WalkForwardValidationError(
            f"plan contains unknown fields: {', '.join(sorted(unknown))}"
        )
    missing = _PLAN_V2_KEYS - set(value)
    if missing:
        raise WalkForwardValidationError(
            f"plan is missing fields: {', '.join(sorted(missing))}"
        )
    if value.get("version") != PLAN_VERSION_V2:
        raise WalkForwardValidationError(
            f"plan.version must be {PLAN_VERSION_V2}"
        )

    portfolio_id = str(value.get("portfolio_id") or "").strip()
    if not portfolio_id or len(portfolio_id) > 160:
        raise WalkForwardValidationError(
            "plan.portfolio_id must bind a valid paper portfolio"
        )
    portfolio_version = _strict_integer(
        value.get("portfolio_version"),
        "plan.portfolio_version",
        minimum=1,
    )
    source_user_decision_id = str(
        value.get("source_user_decision_id") or ""
    ).strip()
    if not source_user_decision_id or len(source_user_decision_id) > 160:
        raise WalkForwardValidationError(
            "plan.source_user_decision_id must be non-empty and at most 160 characters"
        )
    decision_anchor_sha256 = str(value.get("decision_anchor_sha256") or "")
    source_decision_head_sha256 = str(
        value.get("source_decision_head_sha256") or ""
    )
    for field, digest in (
        ("decision_anchor_sha256", decision_anchor_sha256),
        ("source_decision_head_sha256", source_decision_head_sha256),
    ):
        if (
            len(digest) != 64
            or digest.lower() != digest
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise WalkForwardValidationError(
                f"plan.{field} must be a lowercase SHA-256 digest"
            )
    source_decision_head_sequence = _strict_integer(
        value.get("source_decision_head_sequence"),
        "plan.source_decision_head_sequence",
        minimum=1,
    )
    strategy_created_at = _timestamp_ms(
        value.get("strategy_created_at"),
        "plan.strategy_created_at",
    )
    if value.get("mode") != _MODE_V2:
        raise WalkForwardValidationError(f"plan.mode must be {_MODE_V2}")
    if value.get("strategy_provenance") != _STRATEGY_PROVENANCE_V2:
        raise WalkForwardValidationError(
            "plan.strategy_provenance must be "
            f"{_STRATEGY_PROVENANCE_V2}"
        )
    for field, expected in (
        ("out_of_sample_claim", False),
        ("future_performance_claim", False),
        ("retrospective_dataset", True),
    ):
        if value.get(field) is not expected:
            raise WalkForwardValidationError(
                f"plan.{field} must be {str(expected).lower()}"
            )

    evaluation_as_of_date = _iso_date(
        value.get("evaluation_as_of_date"),
        "plan.evaluation_as_of_date",
    )
    data_snapshot_cutoff = _iso_date(
        value.get("data_snapshot_cutoff"),
        "plan.data_snapshot_cutoff",
    )
    if data_snapshot_cutoff >= evaluation_as_of_date:
        raise WalkForwardValidationError(
            "plan.data_snapshot_cutoff must be earlier than evaluation_as_of_date"
        )
    source_positions = _normalize_source_positions(value.get("positions"))

    return {
        "version": PLAN_VERSION_V2,
        "portfolio_id": portfolio_id,
        "portfolio_version": portfolio_version,
        "source_user_decision_id": source_user_decision_id,
        "decision_anchor_sha256": decision_anchor_sha256,
        "source_decision_head_sequence": source_decision_head_sequence,
        "source_decision_head_sha256": source_decision_head_sha256,
        "strategy_created_at": strategy_created_at,
        "mode": _MODE_V2,
        "strategy_provenance": _STRATEGY_PROVENANCE_V2,
        "out_of_sample_claim": False,
        "future_performance_claim": False,
        "retrospective_dataset": True,
        "evaluation_as_of_date": evaluation_as_of_date,
        "data_snapshot_cutoff": data_snapshot_cutoff,
        "name": str(value.get("name") or "").strip(),
        "positions": source_positions,
    }


def _normalize_config_v1(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WalkForwardValidationError("config 必须是 JSON 对象")
    unknown = set(value) - _CONFIG_V1_KEYS
    if unknown:
        raise WalkForwardValidationError(
            f"config 包含未知字段：{', '.join(sorted(unknown))}"
        )
    if value.get("version") != CONFIG_VERSION:
        raise WalkForwardValidationError(f"config.version 必须是 {CONFIG_VERSION}")

    adjustment = str(value.get("price_adjustment") or "QFQ").strip().upper()
    if adjustment != "QFQ":
        raise WalkForwardValidationError("walk-forward 仅接受 Futu QFQ 日线")
    transaction_cost_bps = _finite_number(
        value.get("transaction_cost_bps"),
        "transaction_cost_bps",
    )
    if transaction_cost_bps < 0 or transaction_cost_bps > 1000:
        raise WalkForwardValidationError(
            "transaction_cost_bps 必须在 0 到 1000 之间"
        )

    return {
        "version": CONFIG_VERSION,
        "train_days": _strict_integer(
            value.get("train_days"),
            "train_days",
            minimum=2,
        ),
        "test_days": _strict_integer(
            value.get("test_days"),
            "test_days",
            minimum=1,
        ),
        "step_days": _strict_integer(
            value.get("step_days"),
            "step_days",
            minimum=1,
        ),
        "transaction_cost_bps": round(transaction_cost_bps, 8),
        "price_adjustment": "QFQ",
    }


def _normalize_config_v2(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WalkForwardValidationError("config must be a JSON object")
    unknown = set(value) - _CONFIG_V2_KEYS
    if unknown:
        raise WalkForwardValidationError(
            f"config contains unknown fields: {', '.join(sorted(unknown))}"
        )
    if value.get("version") != CONFIG_VERSION_V2:
        raise WalkForwardValidationError(
            f"config.version must be {CONFIG_VERSION_V2}"
        )

    adjustment = str(value.get("price_adjustment") or "QFQ").strip().upper()
    if adjustment != "QFQ":
        raise WalkForwardValidationError(
            "walk-forward only accepts Futu QFQ daily history"
        )
    scenario_set = str(value.get("friction_scenario_set") or "").strip()
    if scenario_set != STORAGE_FRICTION_SCENARIOS_VERSION:
        raise WalkForwardValidationError(
            "friction_scenario_set must select "
            f"{STORAGE_FRICTION_SCENARIOS_VERSION}; custom scenarios are not allowed"
        )
    unfillable_policy = str(value.get("unfillable_policy") or "").strip()
    if unfillable_policy != UNFILLABLE_POLICY:
        raise WalkForwardValidationError(
            f"unfillable_policy must be {UNFILLABLE_POLICY}"
        )

    return {
        "version": CONFIG_VERSION_V2,
        "train_days": _strict_integer(
            value.get("train_days"),
            "train_days",
            minimum=2,
        ),
        "test_days": _strict_integer(
            value.get("test_days"),
            "test_days",
            minimum=1,
        ),
        "step_days": _strict_integer(
            value.get("step_days"),
            "step_days",
            minimum=1,
        ),
        "price_adjustment": "QFQ",
        "friction_scenario_set": STORAGE_FRICTION_SCENARIOS_VERSION,
        "unfillable_policy": UNFILLABLE_POLICY,
    }


def _normalize_config_v3(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WalkForwardValidationError("config must be a JSON object")
    unknown = set(value) - _CONFIG_V3_KEYS
    if unknown:
        raise WalkForwardValidationError(
            f"config contains unknown fields: {', '.join(sorted(unknown))}"
        )
    missing = _CONFIG_V3_KEYS - set(value)
    if missing:
        raise WalkForwardValidationError(
            f"config is missing fields: {', '.join(sorted(missing))}"
        )
    if value.get("version") != CONFIG_VERSION_V3:
        raise WalkForwardValidationError(
            f"config.version must be {CONFIG_VERSION_V3}"
        )

    adjustment = str(value.get("price_adjustment") or "QFQ").strip().upper()
    if adjustment != "QFQ":
        raise WalkForwardValidationError(
            "walk-forward only accepts Futu QFQ daily history"
        )
    scenario_set = str(value.get("friction_scenario_set") or "").strip()
    if scenario_set != STORAGE_FRICTION_SCENARIOS_VERSION:
        raise WalkForwardValidationError(
            "friction_scenario_set must select "
            f"{STORAGE_FRICTION_SCENARIOS_VERSION}; custom scenarios are not allowed"
        )
    unfillable_policy = str(value.get("unfillable_policy") or "").strip()
    if unfillable_policy != UNFILLABLE_POLICY:
        raise WalkForwardValidationError(
            f"unfillable_policy must be {UNFILLABLE_POLICY}"
        )

    return {
        "version": CONFIG_VERSION_V3,
        "train_days": _strict_integer(
            value.get("train_days"),
            "train_days",
            minimum=2,
        ),
        "test_days": _strict_integer(
            value.get("test_days"),
            "test_days",
            minimum=1,
        ),
        "step_days": _strict_integer(
            value.get("step_days"),
            "step_days",
            minimum=1,
        ),
        "price_adjustment": "QFQ",
        "friction_scenario_set": STORAGE_FRICTION_SCENARIOS_VERSION,
        "unfillable_policy": UNFILLABLE_POLICY,
        "strategy_rule_contract": _normalize_strategy_rule_contract(
            value.get("strategy_rule_contract")
        ),
    }


def _normalize_config(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and value.get("version") == CONFIG_VERSION_V3:
        return _normalize_config_v3(value)
    if isinstance(value, dict) and value.get("version") == CONFIG_VERSION_V2:
        return _normalize_config_v2(value)
    return _normalize_config_v1(value)


def normalize_walk_forward_plan(value: Any) -> dict[str, Any]:
    """Validate and canonicalize a versioned paper plan without side effects."""

    if isinstance(value, dict) and value.get("version") == PLAN_VERSION_V2:
        return _normalize_plan_v2(value)
    return _normalize_plan(value)


def normalize_walk_forward_config(value: Any) -> dict[str, Any]:
    """Validate and canonicalize a versioned walk-forward configuration."""

    return _normalize_config(value)


def calculate_walk_forward_feasibility(
    common_trading_days: Any,
    config: Any,
) -> dict[str, Any]:
    """Return the exact pre-fold capacity for the current row count/config.

    ``test_days`` is the number of close-to-close return observations. A test
    window therefore consumes ``test_days + 1`` price rows. Adjacent windows
    may share the boundary close, but never a return observation.
    """

    history_rows = _strict_integer(
        common_trading_days,
        "common_trading_days",
        minimum=1,
    )
    normalized_config = _normalize_config(config)
    train_days = normalized_config["train_days"]
    test_days = normalized_config["test_days"]
    step_days = normalized_config["step_days"]

    usable_start_span = history_rows - train_days - test_days
    candidate_fold_count = (
        (usable_start_span + step_days - 1) // step_days
        if usable_start_span > 0
        else 0
    )
    selection_stride = (test_days + step_days - 1) // step_days
    maximum_non_overlapping = (
        1 + (candidate_fold_count - 1) // selection_stride
        if candidate_fold_count
        else 0
    )
    required_candidate_folds = (
        1 + (MINIMUM_INDEPENDENT_FOLDS - 1) * selection_stride
    )
    minimum_common_trading_days = (
        train_days
        + (required_candidate_folds - 1) * step_days
        + test_days
        + 1
    )
    ready = maximum_non_overlapping >= MINIMUM_INDEPENDENT_FOLDS
    return {
        "version": FEASIBILITY_VERSION,
        "status": "ready" if ready else "blocked",
        "reason_code": None if ready else INSUFFICIENT_WINDOWS_REASON,
        "failure_stage": None if ready else "pre_fold_generation",
        "common_trading_days": history_rows,
        "train_days": train_days,
        "test_days": test_days,
        "step_days": step_days,
        "test_window_return_observations": test_days,
        "test_window_price_rows": test_days + 1,
        "selection_stride_generated_folds": selection_stride,
        "maximum_candidate_fold_count": candidate_fold_count,
        "maximum_non_overlapping_test_fold_count": maximum_non_overlapping,
        "minimum_non_overlapping_test_folds": MINIMUM_INDEPENDENT_FOLDS,
        "minimum_common_trading_days": minimum_common_trading_days,
        "history_row_shortfall": max(
            0,
            minimum_common_trading_days - history_rows,
        ),
        "calculated_before_fold_generation": True,
        "boundary_close_sharing_only": True,
        "window_shortening_allowed": False,
        "synthetic_padding_allowed": False,
    }


def _extract_history_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WalkForwardValidationError("histories 必须是 JSON 对象")
    candidate = value.get("histories") if "histories" in value else value
    if not isinstance(candidate, dict) or not candidate:
        raise WalkForwardValidationError("histories 不能为空")
    return candidate


def _date_from_row(row: dict[str, Any], symbol: str, index: int) -> str:
    raw_time = (
        row.get("market_time")
        or row.get("time_key")
        or row.get("time")
    )
    text = str(raw_time or "").strip()
    if len(text) < 10:
        raise WalkForwardValidationError(
            f"{symbol} rows[{index}] 缺少有效交易日期"
        )
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise WalkForwardValidationError(
            f"{symbol} rows[{index}] 交易日期无效"
        ) from exc


def _normalize_history_rows(
    symbol: str,
    history_value: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(history_value, dict):
        raise WalkForwardValidationError(f"{symbol} history 必须是带来源元数据的对象")
    if history_value.get("ok") is not True:
        raise WalkForwardValidationError(f"{symbol} 历史数据状态不是 ok")
    if history_value.get("source") != "futu_opend":
        raise WalkForwardValidationError(f"{symbol} 历史数据 source 必须是 futu_opend")
    if str(history_value.get("interval") or "").strip().lower() != "1d":
        raise WalkForwardValidationError(f"{symbol} 历史数据 interval 必须是 1d")
    if str(history_value.get("price_adjustment") or "").strip().upper() != "QFQ":
        raise WalkForwardValidationError(f"{symbol} 历史数据 price_adjustment 必须是 QFQ")
    if history_value.get("execution_capability") != "none":
        raise WalkForwardValidationError(f"{symbol} 历史数据不能获得订单执行能力")
    if history_value.get("live_trading_allowed") is not False:
        raise WalkForwardValidationError(f"{symbol} 历史数据不能打开真实交易")
    if history_value.get("source_errors"):
        raise WalkForwardValidationError(f"{symbol} 历史数据包含来源错误")

    captured_at = _utc_timestamp(
        history_value.get("captured_at"),
        f"{symbol} captured_at",
    )
    as_of_date = _iso_date(
        history_value.get("as_of_date"),
        f"{symbol} as_of_date",
    )
    last_completed_session = _iso_date(
        history_value.get("last_completed_session"),
        f"{symbol} last_completed_session",
    )
    actual_start = _iso_date(
        history_value.get("actual_start"),
        f"{symbol} actual_start",
    )
    actual_end = _iso_date(
        history_value.get("actual_end"),
        f"{symbol} actual_end",
    )
    if last_completed_session >= as_of_date:
        raise WalkForwardValidationError(
            f"{symbol} last_completed_session 必须早于 as_of_date"
        )

    rows = history_value.get("rows")
    if not isinstance(rows, list) or not rows:
        raise WalkForwardValidationError(f"{symbol} rows 必须是非空数组")

    normalized: list[dict[str, Any]] = []
    seen_dates: set[str] = set()
    previous_date = ""
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise WalkForwardValidationError(f"{symbol} rows[{index}] 必须是对象")
        if row.get("symbol"):
            row_symbol = _normalize_symbol(row.get("symbol"))
            if row_symbol != symbol:
                raise WalkForwardValidationError(
                    f"{symbol} rows[{index}] 的 symbol 不匹配"
                )
        date_key = _date_from_row(row, symbol, index)
        if date_key in seen_dates:
            raise WalkForwardValidationError(f"{symbol} 存在重复交易日期：{date_key}")
        if previous_date and date_key <= previous_date:
            raise WalkForwardValidationError(
                f"{symbol} 交易日期必须严格递增：{date_key}"
            )
        if "close" not in row or row.get("close") is None:
            raise WalkForwardValidationError(
                f"{symbol} {date_key} 缺少 close"
            )
        close = _finite_number(row.get("close"), f"{symbol} {date_key} close")
        if close <= 0:
            raise WalkForwardValidationError(
                f"{symbol} {date_key} close 必须大于 0"
            )
        normalized.append({"date": date_key, "close": close})
        seen_dates.add(date_key)
        previous_date = date_key
    if normalized[0]["date"] != actual_start:
        raise WalkForwardValidationError(f"{symbol} actual_start 与首行日期不一致")
    if normalized[-1]["date"] != actual_end:
        raise WalkForwardValidationError(f"{symbol} actual_end 与末行日期不一致")
    if normalized[-1]["date"] > last_completed_session:
        raise WalkForwardValidationError(
            f"{symbol} 最后一行超过 last_completed_session"
        )
    return normalized, {
        "source": "futu_opend",
        "interval": "1d",
        "price_adjustment": "QFQ",
        "captured_at": captured_at,
        "as_of_date": as_of_date,
        "last_completed_session": last_completed_session,
        "actual_start": actual_start,
        "actual_end": actual_end,
    }


def _normalize_histories(
    value: Any,
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, dict[str, Any]],
]:
    raw_histories = _extract_history_mapping(value)
    normalized: dict[str, list[dict[str, Any]]] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for raw_symbol, history_value in raw_histories.items():
        symbol = _normalize_symbol(raw_symbol)
        if symbol in normalized:
            raise WalkForwardValidationError(f"histories 标的重复：{symbol}")
        rows, history_metadata = _normalize_history_rows(symbol, history_value)
        normalized[symbol] = rows
        metadata[symbol] = history_metadata

    missing_symbols = [
        symbol for symbol in STORAGE_SYMBOLS if symbol not in normalized
    ]
    if missing_symbols:
        raise WalkForwardValidationError(
            "histories 必须完整覆盖四只白名单标的，缺少："
            + ", ".join(missing_symbols)
        )

    ordered = {
        symbol: normalized[symbol]
        for symbol in STORAGE_SYMBOLS
    }
    reference_symbol = next(iter(ordered))
    reference_dates = [row["date"] for row in ordered[reference_symbol]]
    for symbol, rows in ordered.items():
        dates = [row["date"] for row in rows]
        if dates != reference_dates:
            raise WalkForwardValidationError(
                f"{symbol} 与 {reference_symbol} 的交易日期不完整对齐"
            )
    ordered_metadata = {
        symbol: metadata[symbol]
        for symbol in STORAGE_SYMBOLS
    }
    as_of_dates = {
        item["as_of_date"]
        for item in ordered_metadata.values()
    }
    completed_sessions = {
        item["last_completed_session"]
        for item in ordered_metadata.values()
    }
    captured_times = {
        item["captured_at"]
        for item in ordered_metadata.values()
    }
    if len(as_of_dates) != 1:
        raise WalkForwardValidationError("四只标的的 as_of_date 必须一致")
    if len(completed_sessions) != 1:
        raise WalkForwardValidationError(
            "四只标的的 last_completed_session 必须一致"
        )
    if len(captured_times) != 1:
        raise WalkForwardValidationError("四只标的的 captured_at 必须一致")
    return ordered, ordered_metadata


def _optional_history_number(
    value: Any,
    label: str,
    *,
    positive_when_present: bool,
) -> float | None:
    if value is None:
        return None
    number = _finite_number(value, label)
    if positive_when_present and number <= 0:
        raise WalkForwardValidationError(f"{label} must be positive when provided")
    return number


def _normalize_histories_v3(
    value: Any,
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, dict[str, Any]],
]:
    """Preserve the complete, canonical Futu daily research snapshot for v3.

    The v2 normalizer remains the source of truth for source metadata, calendar,
    close, and four-symbol validation.  v3 then binds optional OHLCV/turnover
    observations into every hash.  Missing and non-positive liquidity values
    remain explicit so the paper capacity model can fail closed.
    """

    close_rows_by_symbol, metadata = _normalize_histories(value)
    raw_mapping = _extract_history_mapping(value)
    raw_by_symbol: dict[str, Any] = {}
    for raw_symbol, raw_history in raw_mapping.items():
        symbol = _normalize_symbol(raw_symbol)
        raw_by_symbol[symbol] = raw_history

    normalized: dict[str, list[dict[str, Any]]] = {}
    for symbol in STORAGE_SYMBOLS:
        raw_rows = raw_by_symbol[symbol].get("rows")
        canonical_close_rows = close_rows_by_symbol[symbol]
        full_rows: list[dict[str, Any]] = []
        for index, (raw_row, close_row) in enumerate(
            zip(raw_rows, canonical_close_rows, strict=True)
        ):
            date_key = close_row["date"]
            full_rows.append(
                {
                    "date": date_key,
                    "open": _optional_history_number(
                        raw_row.get("open"),
                        f"{symbol} {date_key} open",
                        positive_when_present=True,
                    ),
                    "high": _optional_history_number(
                        raw_row.get("high"),
                        f"{symbol} {date_key} high",
                        positive_when_present=True,
                    ),
                    "low": _optional_history_number(
                        raw_row.get("low"),
                        f"{symbol} {date_key} low",
                        positive_when_present=True,
                    ),
                    "close": close_row["close"],
                    "volume": _optional_history_number(
                        raw_row.get("volume"),
                        f"{symbol} {date_key} volume",
                        positive_when_present=False,
                    ),
                    "turnover": _optional_history_number(
                        raw_row.get("turnover"),
                        f"{symbol} {date_key} turnover",
                        positive_when_present=False,
                    ),
                }
            )
        normalized[symbol] = full_rows
    return normalized, metadata


def _sha256(value: Any) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _signed_weights(plan: dict[str, Any]) -> dict[str, float]:
    weights: dict[str, float] = {}
    for position in plan["positions"]:
        direction = (
            1.0
            if position["side"] == "LONG"
            else -1.0
            if position["side"] == "SHORT"
            else 0.0
        )
        weights[position["symbol"]] = direction * position["weight_pct"] / 100
    return weights


def _fixed_initial_notional_equity_path(
    rows_by_symbol: dict[str, list[dict[str, Any]]],
    weights: dict[str, float],
    *,
    start_index: int,
    end_index: int,
    entry_cost_rate: float = 0.0,
    exit_cost_rate: float = 0.0,
) -> list[float]:
    entry_prices = {
        symbol: rows_by_symbol[symbol][start_index]["close"]
        for symbol, weight in weights.items()
        if weight != 0
    }
    equity_path = [1.0 - entry_cost_rate]
    for current_index in range(start_index + 1, end_index + 1):
        equity = 1.0 - entry_cost_rate
        for symbol, weight in weights.items():
            if weight == 0:
                continue
            current_close = rows_by_symbol[symbol][current_index]["close"]
            equity += weight * (
                current_close / entry_prices[symbol] - 1
            )
        equity_path.append(equity)
    equity_path[-1] -= exit_cost_rate
    return equity_path


def _equity_path_metrics(equity_path: list[float]) -> tuple[float, float]:
    peak = 1.0
    max_drawdown = 0.0
    for equity in equity_path:
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
    return equity_path[-1] - 1, max_drawdown


def _train_risk_metrics(
    rows_by_symbol: dict[str, list[dict[str, Any]]],
    weights: dict[str, float],
    *,
    start_index: int,
    end_index: int,
) -> dict[str, float | bool | None]:
    equity_path = _fixed_initial_notional_equity_path(
        rows_by_symbol,
        weights,
        start_index=start_index,
        end_index=end_index,
    )
    _train_return, max_drawdown = _equity_path_metrics(equity_path)
    equity_nonpositive = any(equity <= 0 for equity in equity_path)
    daily_returns = (
        [
            current / previous - 1
            for previous, current in zip(equity_path, equity_path[1:])
        ]
        if not equity_nonpositive
        else []
    )
    volatility = (
        statistics.stdev(daily_returns) * math.sqrt(252) * 100
        if len(daily_returns) >= 2
        else None
    )
    return {
        "annualized_volatility_pct": (
            round(volatility, 8) if volatility is not None else None
        ),
        "max_drawdown_pct": round(max_drawdown * 100, 8),
        "equity_nonpositive": equity_nonpositive,
    }


def _fold_payload_rows(
    rows_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    start_index: int,
    end_index: int,
) -> dict[str, list[dict[str, Any]]]:
    return {
        symbol: [
            {"date": row["date"], "close": row["close"]}
            for row in rows[start_index:end_index + 1]
        ]
        for symbol, rows in rows_by_symbol.items()
    }


def _fold_payload_rows_v3(
    rows_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    start_index: int,
    end_index: int,
) -> dict[str, list[dict[str, Any]]]:
    fields = ("date", "open", "high", "low", "close", "volume", "turnover")
    return {
        symbol: [
            {field: row.get(field) for field in fields}
            for row in rows[start_index:end_index + 1]
        ]
        for symbol, rows in rows_by_symbol.items()
    }


def _terminal_gross_notional(
    rows_by_symbol: dict[str, list[dict[str, Any]]],
    weights: dict[str, float],
    *,
    start_index: int,
    end_index: int,
) -> float:
    return sum(
        abs(weight)
        * rows_by_symbol[symbol][end_index]["close"]
        / rows_by_symbol[symbol][start_index]["close"]
        for symbol, weight in weights.items()
        if weight != 0
    )


def _mark_non_overlapping_folds(
    folds: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    last_test_end = ""
    non_overlapping: list[dict[str, Any]] = []
    for fold in folds:
        # Adjacent windows may share the boundary close without sharing a
        # close-to-close return observation.
        selected = (
            not last_test_end or fold["execution_start"] >= last_test_end
        )
        fold["non_overlapping_test_window"] = selected
        # Kept only for old readers. It does not claim statistical
        # independence.
        fold["independent_sample"] = selected
        if selected:
            non_overlapping.append(fold)
            last_test_end = fold["test_end"]
    return non_overlapping


def _compounded_return_pct(folds: list[dict[str, Any]], field: str) -> float | None:
    wealth = 1.0
    for fold in folds:
        value = fold.get(field)
        if value is None:
            return None
        multiplier = 1 + float(value) / 100
        if multiplier <= 0:
            return None
        wealth *= multiplier
    return round((wealth - 1) * 100, 8) if folds else None


def _summary(
    folds: list[dict[str, Any]],
    non_overlapping_folds: list[dict[str, Any]],
) -> dict[str, Any]:
    returns = [fold["net_return_pct"] for fold in non_overlapping_folds]
    drawdowns = [fold["max_drawdown_pct"] for fold in non_overlapping_folds]
    mean_return = round(statistics.fmean(returns), 8) if returns else None
    median_return = round(statistics.median(returns), 8) if returns else None
    worst_return = round(min(returns), 8) if returns else None
    maximum_drawdown = round(min(drawdowns), 8) if drawdowns else None
    equity_nonpositive = any(
        bool(fold.get("equity_nonpositive"))
        for fold in non_overlapping_folds
    )
    if equity_nonpositive:
        status = "blocked"
    elif len(non_overlapping_folds) >= MINIMUM_INDEPENDENT_FOLDS:
        status = "sufficient"
    else:
        status = "insufficient"
    positive_ratio = (
        round(
            sum(1 for fold in non_overlapping_folds if fold["positive"])
            / len(non_overlapping_folds),
            8,
        )
        if non_overlapping_folds
        else None
    )
    return {
        "fold_count": len(folds),
        "non_overlapping_test_fold_count": len(non_overlapping_folds),
        "minimum_non_overlapping_test_folds": MINIMUM_INDEPENDENT_FOLDS,
        "independent_fold_count": len(non_overlapping_folds),
        "minimum_independent_folds": MINIMUM_INDEPENDENT_FOLDS,
        "status": status,
        "adequacy_status": status,
        "sample_adequacy": {
            "status": status,
            "required_non_overlapping_test_folds": MINIMUM_INDEPENDENT_FOLDS,
            "actual_non_overlapping_test_folds": len(non_overlapping_folds),
            "equity_nonpositive": equity_nonpositive,
        },
        "historical_only": True,
        "basis": "non_overlapping_test_windows_not_statistical_independence",
        "return_unit": "percent",
        "mean": mean_return,
        "median": median_return,
        "worst": worst_return,
        "max_drawdown": maximum_drawdown,
        "mean_return_pct": mean_return,
        "median_return_pct": median_return,
        "worst_return_pct": worst_return,
        "max_drawdown_pct": maximum_drawdown,
        "worst_fold_max_drawdown_pct": maximum_drawdown,
        "historical_positive_fold_ratio": positive_ratio,
        "positive_fold_rate": positive_ratio,
        "portfolio_cumulative_return_pct": _compounded_return_pct(
            non_overlapping_folds,
            "net_return_pct",
        ),
        "benchmark_cumulative_return_pct": _compounded_return_pct(
            non_overlapping_folds,
            "benchmark_equal_weight_return_pct",
        ),
    }


def _run_walk_forward_v2(
    histories: Any,
    plan: Any,
    config: Any,
) -> dict[str, Any]:
    """Replay a current, versioned paper plan over frozen Futu QFQ rows.

    This is deliberately a retroactive fixed-plan replay, not an out-of-sample
    strategy validation. Within each fold the reference-window inputs and the
    decision hash end at ``decision_cutoff``. The next available session close
    is the simulated entry price, and the first return is observed one session
    later. Each fold holds fixed initial notionals through its exit close.

    This function performs no network, model, database, account, or order work.
    """

    normalized_plan = _normalize_plan(plan)
    normalized_config = _normalize_config(config)
    rows_by_symbol, history_metadata = _normalize_histories(histories)
    signed_weights = _signed_weights(normalized_plan)
    active_symbols = [
        symbol
        for symbol in STORAGE_SYMBOLS
        if signed_weights.get(symbol, 0.0) != 0
    ]
    symbols = list(rows_by_symbol)
    dates = [row["date"] for row in rows_by_symbol[symbols[0]]]
    common_as_of_date = history_metadata[symbols[0]]["as_of_date"]
    common_data_cutoff = history_metadata[symbols[0]]["last_completed_session"]
    if normalized_plan["evaluation_as_of_date"] != common_as_of_date:
        raise WalkForwardValidationError(
            "plan.evaluation_as_of_date 与冻结历史不一致"
        )
    if normalized_plan["data_snapshot_cutoff"] != common_data_cutoff:
        raise WalkForwardValidationError(
            "plan.data_snapshot_cutoff 与冻结历史不一致"
        )
    if not dates or dates[-1] != common_data_cutoff:
        raise WalkForwardValidationError("冻结历史末行与数据截止日不一致")
    train_days = normalized_config["train_days"]
    test_days = normalized_config["test_days"]
    step_days = normalized_config["step_days"]
    feasibility = calculate_walk_forward_feasibility(
        len(dates),
        normalized_config,
    )
    if feasibility["status"] != "ready":
        raise WalkForwardFeasibilityError(feasibility)
    gross_exposure = sum(abs(signed_weights[symbol]) for symbol in active_symbols)
    cost_rate = normalized_config["transaction_cost_bps"] / 10_000
    entry_cost_rate = cost_rate * gross_exposure
    benchmark_weights = {symbol: 1 / len(symbols) for symbol in symbols}

    folds: list[dict[str, Any]] = []
    test_start_index = train_days
    while test_start_index + test_days < len(dates):
        train_start_index = test_start_index - train_days
        train_end_index = test_start_index - 1
        test_end_index = test_start_index + test_days
        train_rows = _fold_payload_rows(
            rows_by_symbol,
            start_index=train_start_index,
            end_index=train_end_index,
        )
        decision_payload = {
            "engine_version": ENGINE_VERSION,
            "plan": normalized_plan,
            "config": normalized_config,
            "decision_cutoff": dates[train_end_index],
            "scheduled_entry_date": dates[test_start_index],
            "train_rows": train_rows,
        }
        decision_input_hash = _sha256(decision_payload)
        test_rows = _fold_payload_rows(
            rows_by_symbol,
            start_index=test_start_index,
            end_index=test_end_index,
        )
        fold_input_hash = _sha256(
            {
                "decision_input_hash": decision_input_hash,
                "test_rows": test_rows,
            }
        )

        exit_gross_notional = _terminal_gross_notional(
            rows_by_symbol,
            signed_weights,
            start_index=test_start_index,
            end_index=test_end_index,
        )
        exit_cost_rate = cost_rate * exit_gross_notional
        portfolio_equity_path = _fixed_initial_notional_equity_path(
            rows_by_symbol,
            signed_weights,
            start_index=test_start_index,
            end_index=test_end_index,
            entry_cost_rate=entry_cost_rate,
            exit_cost_rate=exit_cost_rate,
        )
        net_return, max_drawdown = _equity_path_metrics(portfolio_equity_path)
        benchmark_equity_path = _fixed_initial_notional_equity_path(
            rows_by_symbol,
            benchmark_weights,
            start_index=test_start_index,
            end_index=test_end_index,
        )
        benchmark_return, _benchmark_drawdown = _equity_path_metrics(
            benchmark_equity_path
        )
        train_risk = _train_risk_metrics(
            rows_by_symbol,
            signed_weights,
            start_index=train_start_index,
            end_index=train_end_index,
        )
        net_return_pct = round(net_return * 100, 8)
        benchmark_return_pct = round(benchmark_return * 100, 8)
        max_drawdown_pct = round(max_drawdown * 100, 8)
        positive = net_return > 0
        equity_nonpositive = any(equity <= 0 for equity in portfolio_equity_path)

        folds.append(
            {
                "fold_index": len(folds) + 1,
                "fold_id": f"fold_{len(folds) + 1:03d}",
                "train_start": dates[train_start_index],
                "train_end": dates[train_end_index],
                "decision_cutoff": dates[train_end_index],
                "test_start": dates[test_start_index],
                "scheduled_entry_date": dates[test_start_index],
                "execution_start": dates[test_start_index],
                "entry_price_date": dates[test_start_index],
                "first_return_date": dates[test_start_index + 1],
                "test_end": dates[test_end_index],
                "exit_date": dates[test_end_index],
                "train_days": train_days,
                "test_days": test_days,
                "return_unit": "percent",
                "net_return": net_return_pct,
                "benchmark_equal_weight_return": benchmark_return_pct,
                "max_drawdown": max_drawdown_pct,
                "is_positive": positive,
                "net_return_pct": net_return_pct,
                "benchmark_equal_weight_return_pct": benchmark_return_pct,
                "max_drawdown_pct": max_drawdown_pct,
                "positive": positive,
                "equity_nonpositive": equity_nonpositive,
                "entry_cost_pct": round(entry_cost_rate * 100, 8),
                "exit_cost_pct": round(exit_cost_rate * 100, 8),
                "transaction_cost_pct": round(
                    (entry_cost_rate + exit_cost_rate) * 100,
                    8,
                ),
                "signal_source": "versioned_user_paper_weights",
                "signal_weights_pct": {
                    symbol: round(signed_weights[symbol] * 100, 8)
                    for symbol in STORAGE_SYMBOLS
                },
                "risk_input_scope": "rolling_train_before_test_start_only",
                "holding_rule": "fixed_initial_notional_buy_and_hold",
                "rebalancing": "none_within_fold",
                "train_risk": train_risk,
                "decision_input_hash": decision_input_hash,
                "input_hash": fold_input_hash,
            }
        )
        test_start_index += step_days

    non_overlapping_folds = _mark_non_overlapping_folds(folds)
    if (
        len(folds) != feasibility["maximum_candidate_fold_count"]
        or len(non_overlapping_folds)
        != feasibility["maximum_non_overlapping_test_fold_count"]
    ):
        raise WalkForwardValidationError(
            "walk-forward 窗口计数与运行前可行性诊断不一致"
        )
    summary = _summary(folds, non_overlapping_folds)
    global_input_hash = _sha256(
        {
            "engine_version": ENGINE_VERSION,
            "plan": normalized_plan,
            "config": normalized_config,
            "histories": rows_by_symbol,
            "history_metadata": history_metadata,
        }
    )
    return {
        "version": RESULT_VERSION,
        "engine_version": ENGINE_VERSION,
        "state": summary["status"],
        "scope": "historical_only",
        "historical_only": True,
        "evaluation_mode": _MODE,
        "strategy_provenance": _STRATEGY_PROVENANCE,
        "out_of_sample_claim": False,
        "source": "futu_qfq_daily_history",
        "price_adjustment": "QFQ",
        "evaluation_as_of_date": common_as_of_date,
        "data_snapshot_cutoff": common_data_cutoff,
        "portfolio_id": normalized_plan["portfolio_id"],
        "portfolio_version": normalized_plan["portfolio_version"],
        "plan_version": normalized_plan["version"],
        "config": normalized_config,
        "symbols": symbols,
        "required_symbols": list(STORAGE_SYMBOLS),
        "covered_symbols": symbols,
        "active_symbols": active_symbols,
        "benchmark_symbols": symbols,
        "method": {
            "signal": "versioned_user_paper_weights",
            "evaluation_mode": _MODE,
            "strategy_provenance": _STRATEGY_PROVENANCE,
            "out_of_sample_claim": False,
            "risk_input_scope": "rolling_train_before_test_start_only",
            "execution_lag_trading_days": 1,
            "execution_rule": "next_session_close_after_decision_cutoff",
            "holding_rule": "fixed_initial_notional_buy_and_hold",
            "rebalancing": "none_within_fold",
            "transaction_cost": "fixed_bps_entry_and_exit",
            "short_borrow_fee_included": False,
            "slippage_included": False,
        },
        "data_quality": {
            "common_trading_days": len(dates),
            "actual_start": dates[0] if dates else None,
            "actual_end": dates[-1] if dates else None,
            "four_symbol_calendar_aligned": True,
            "completed_sessions_only": True,
        },
        "feasibility": feasibility,
        "input_hash": global_input_hash,
        "folds": folds,
        "summary": summary,
        "interpretation": (
            "结果是把当前固定纸面方案追溯应用于历史窗口的滚动回放。"
            "计划本身可能参考过整段历史，因此 out_of_sample_claim=false；"
            "历史非重叠窗口正收益比例不是未来胜率、策略样本外验证、投资建议或订单。"
        ),
        "provider_calls_total": 0,
        "openai_calls": 0,
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


def _friction_cost_percentages(
    friction_result: dict[str, Any],
) -> dict[str, float | None]:
    costs = friction_result.get("costs")
    if not isinstance(costs, dict):
        return {
            "entry_cost_pct": None,
            "exit_cost_pct": None,
            "commission_cost_pct": None,
            "slippage_cost_pct": None,
            "short_borrow_cost_pct": None,
            "total_friction_cost_pct": None,
        }
    capital = float(friction_result["scenario"]["paper_reference_notional_usd"])

    def percent(amount: float) -> float:
        return round(float(amount) / capital * 100, 8)

    entry_cost = (
        float(costs["entry_commission_usd"])
        + float(costs["entry_slippage_cost_usd"])
    )
    exit_cost = (
        float(costs["exit_commission_usd"])
        + float(costs["exit_slippage_cost_usd"])
    )
    return {
        "entry_cost_pct": percent(entry_cost),
        "exit_cost_pct": percent(exit_cost),
        "commission_cost_pct": percent(costs["commission_cost_usd"]),
        "slippage_cost_pct": percent(costs["slippage_cost_usd"]),
        "short_borrow_cost_pct": percent(costs["short_borrow_cost_usd"]),
        "total_friction_cost_pct": percent(costs["total_friction_cost_usd"]),
    }


def _v3_fillability(friction_result: dict[str, Any]) -> dict[str, Any]:
    checks = deepcopy(friction_result["liquidity_checks"])
    blockers = [check for check in checks if not check["fillable"]]
    capacity_gap = max(
        (float(check["capacity_gap_usd"]) for check in checks),
        default=0.0,
    )
    return {
        "status": friction_result["status"],
        "blocked": bool(friction_result["blocked"]),
        "reason_code": friction_result["reason_code"],
        "capacity_gap_usd": round(capacity_gap, 8),
        "first_blocker": deepcopy(blockers[0]) if blockers else None,
        "liquidity_checks": checks,
        "capacity_is_proxy": True,
        "actual_execution": False,
        "actual_execution_observed": False,
    }


def _summary_v3(
    folds: list[dict[str, Any]],
    non_overlapping_folds: list[dict[str, Any]],
) -> dict[str, Any]:
    formal_unfillable = [
        fold for fold in non_overlapping_folds if bool(fold["blocked"])
    ]
    all_unfillable = [fold for fold in folds if bool(fold["blocked"])]
    equity_nonpositive = any(
        bool(fold.get("equity_nonpositive"))
        for fold in non_overlapping_folds
        if not fold["blocked"]
    )
    if formal_unfillable or equity_nonpositive:
        status = "blocked"
    elif len(non_overlapping_folds) >= MINIMUM_INDEPENDENT_FOLDS:
        status = "sufficient"
    else:
        status = "insufficient"

    metrics_visible = status != "blocked"
    returns = (
        [float(fold["net_return_pct"]) for fold in non_overlapping_folds]
        if metrics_visible
        else []
    )
    drawdowns = (
        [float(fold["max_drawdown_pct"]) for fold in non_overlapping_folds]
        if metrics_visible
        else []
    )
    mean_return = round(statistics.fmean(returns), 8) if returns else None
    median_return = round(statistics.median(returns), 8) if returns else None
    worst_return = round(min(returns), 8) if returns else None
    maximum_drawdown = round(min(drawdowns), 8) if drawdowns else None
    positive_ratio = (
        round(
            sum(1 for fold in non_overlapping_folds if fold["positive"])
            / len(non_overlapping_folds),
            8,
        )
        if metrics_visible and non_overlapping_folds
        else None
    )
    return {
        "fold_count": len(folds),
        "non_overlapping_test_fold_count": len(non_overlapping_folds),
        "minimum_non_overlapping_test_folds": MINIMUM_INDEPENDENT_FOLDS,
        "independent_fold_count": len(non_overlapping_folds),
        "minimum_independent_folds": MINIMUM_INDEPENDENT_FOLDS,
        "status": status,
        "state": status,
        "adequacy_status": status,
        "blocked": status == "blocked",
        "unfillable_fold_count": len(all_unfillable),
        "formal_unfillable_fold_count": len(formal_unfillable),
        "returns_hidden": not metrics_visible,
        "sample_adequacy": {
            "status": status,
            "required_non_overlapping_test_folds": MINIMUM_INDEPENDENT_FOLDS,
            "actual_non_overlapping_test_folds": len(non_overlapping_folds),
            "equity_nonpositive": equity_nonpositive,
            "formal_unfillable_fold_count": len(formal_unfillable),
        },
        "historical_only": True,
        "basis": "non_overlapping_test_windows_not_statistical_independence",
        "return_unit": "percent",
        "mean": mean_return,
        "median": median_return,
        "worst": worst_return,
        "max_drawdown": maximum_drawdown,
        "mean_return_pct": mean_return,
        "median_return_pct": median_return,
        "worst_return_pct": worst_return,
        "max_drawdown_pct": maximum_drawdown,
        "worst_fold_max_drawdown_pct": maximum_drawdown,
        "historical_positive_fold_ratio": positive_ratio,
        "positive_fold_rate": positive_ratio,
        "portfolio_cumulative_return_pct": (
            _compounded_return_pct(non_overlapping_folds, "net_return_pct")
            if metrics_visible
            else None
        ),
        # The equal-weight benchmark is deliberately friction-free and remains
        # observable even when a paper portfolio scenario cannot be filled.
        "benchmark_cumulative_return_pct": _compounded_return_pct(
            non_overlapping_folds,
            "benchmark_equal_weight_return_pct",
        ),
    }


def _scenario_first_blocker(
    folds: list[dict[str, Any]],
) -> dict[str, Any] | None:
    blocked_folds = [fold for fold in folds if fold["blocked"]]
    formal = [fold for fold in blocked_folds if fold["non_overlapping_test_window"]]
    candidates = formal or blocked_folds
    if not candidates:
        return None
    fold = candidates[0]
    blocker = deepcopy(fold["first_blocker"])
    if blocker is None:
        return None
    return {
        "fold_index": fold["fold_index"],
        "fold_id": fold["fold_id"],
        **blocker,
    }


def _fit_v4_strategy_decision(
    rows_by_symbol: dict[str, list[dict[str, Any]]],
    strategy_rule_contract: dict[str, Any],
    *,
    train_start_index: int,
    train_end_index: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, float]]:
    """Fit one deterministic rule using close rows from one fold only."""

    train_close_rows = _fold_payload_rows(
        rows_by_symbol,
        start_index=train_start_index,
        end_index=train_end_index,
    )
    raw_scores = {
        symbol: (
            rows_by_symbol[symbol][train_end_index]["close"]
            / rows_by_symbol[symbol][train_start_index]["close"]
            - 1
        )
        for symbol in STORAGE_SYMBOLS
    }
    ranking = sorted(
        STORAGE_SYMBOLS,
        key=lambda symbol: (-raw_scores[symbol], symbol),
    )
    long_count = int(strategy_rule_contract["long_count"])
    short_count = int(strategy_rule_contract["short_count"])
    long_symbols = set(ranking[:long_count])
    short_symbols = set(ranking[-short_count:]) if short_count else set()
    if long_symbols & short_symbols:
        raise WalkForwardValidationError(
            "strategy selections overlap despite the four-symbol count bound"
        )

    long_weight = (
        float(strategy_rule_contract["long_budget_pct"]) / long_count
        if long_count
        else 0.0
    )
    short_weight = (
        float(strategy_rule_contract["short_budget_pct"]) / short_count
        if short_count
        else 0.0
    )
    selected_positions: list[dict[str, Any]] = []
    signed_weights_pct: dict[str, float] = {}
    for symbol in sorted(STORAGE_SYMBOLS):
        if symbol in long_symbols:
            side = "LONG"
            weight_pct = round(long_weight, 8)
            signed_weight_pct = weight_pct
        elif symbol in short_symbols:
            side = "SHORT"
            weight_pct = round(short_weight, 8)
            signed_weight_pct = -weight_pct
        else:
            signed_weights_pct[symbol] = 0.0
            continue
        selected_positions.append(
            {
                "symbol": symbol,
                "side": side,
                "weight_pct": weight_pct,
            }
        )
        signed_weights_pct[symbol] = signed_weight_pct
    signed_weights_pct = {
        symbol: signed_weights_pct.get(symbol, 0.0)
        for symbol in STORAGE_SYMBOLS
    }

    fit_window = {
        "start": rows_by_symbol[STORAGE_SYMBOLS[0]][train_start_index]["date"],
        "end": rows_by_symbol[STORAGE_SYMBOLS[0]][train_end_index]["date"],
        "trading_day_rows": train_end_index - train_start_index + 1,
    }
    fit_input_hash = _sha256(
        {
            "engine_version": ENGINE_VERSION_V4,
            "input_snapshot_version": INPUT_SNAPSHOT_VERSION_V3,
            "strategy_rule_contract": strategy_rule_contract,
            "fit_window": fit_window,
            "train_close_rows": train_close_rows,
        }
    )
    scores_pct = {
        symbol: round(raw_scores[symbol] * 100, 8)
        for symbol in STORAGE_SYMBOLS
    }
    decision = {
        "version": "fold_strategy_decision_v1",
        "rule_id": RULE_ID,
        "signal": "training_window_total_return",
        "score_unit": "percent",
        "fit_scope": "fold_training_window_only",
        "fit_window": fit_window,
        "scores": scores_pct,
        "scores_pct": scores_pct,
        "ranking": ranking,
        "ranking_rule": "descending_return_then_symbol_ascending",
        "selected_positions": selected_positions,
        "weights": signed_weights_pct,
        "weights_pct": signed_weights_pct,
        "weighting": "equal_notional_within_side",
        "rebalance": "fold_entry_only",
        "execution_lag_trading_days": 1,
        "test_excluded": True,
        "test_data_excluded_from_fit": True,
        "source_positions_directly_replayed": False,
        "fit_hash": fit_input_hash,
        "fit_input_hash": fit_input_hash,
    }
    return decision, selected_positions, {
        symbol: signed_weights_pct[symbol] / 100
        for symbol in STORAGE_SYMBOLS
    }


def _run_walk_forward_v3(
    histories: Any,
    plan: Any,
    config: Any,
) -> dict[str, Any]:
    normalized_plan = _normalize_plan(plan)
    normalized_config = _normalize_config_v2(config)
    rows_by_symbol, history_metadata = _normalize_histories_v3(histories)
    signed_weights = _signed_weights(normalized_plan)
    active_symbols = [
        symbol
        for symbol in STORAGE_SYMBOLS
        if signed_weights.get(symbol, 0.0) != 0
    ]
    symbols = list(rows_by_symbol)
    dates = [row["date"] for row in rows_by_symbol[symbols[0]]]
    common_as_of_date = history_metadata[symbols[0]]["as_of_date"]
    common_data_cutoff = history_metadata[symbols[0]]["last_completed_session"]
    if normalized_plan["evaluation_as_of_date"] != common_as_of_date:
        raise WalkForwardValidationError(
            "plan.evaluation_as_of_date does not match frozen history"
        )
    if normalized_plan["data_snapshot_cutoff"] != common_data_cutoff:
        raise WalkForwardValidationError(
            "plan.data_snapshot_cutoff does not match frozen history"
        )
    if not dates or dates[-1] != common_data_cutoff:
        raise WalkForwardValidationError(
            "the final frozen-history row must match data_snapshot_cutoff"
        )

    train_days = normalized_config["train_days"]
    test_days = normalized_config["test_days"]
    step_days = normalized_config["step_days"]
    feasibility = calculate_walk_forward_feasibility(len(dates), normalized_config)
    if feasibility["status"] != "ready":
        raise WalkForwardFeasibilityError(feasibility)

    scenario_set = get_storage_friction_scenarios()
    friction_model = {
        "version": PAPER_FRICTION_MODEL_VERSION,
        "scenario_set": scenario_set,
        "liquidity_proxy_version": PAPER_LIQUIDITY_PROXY_VERSION,
        "unfillable_policy": UNFILLABLE_POLICY,
        "benchmark_friction_applied": False,
        "capacity_is_proxy": True,
        "actual_execution": False,
        "actual_execution_observed": False,
    }
    benchmark_weights = {symbol: 1 / len(symbols) for symbol in symbols}
    positions = normalized_plan["positions"]
    scenario_folds: dict[str, list[dict[str, Any]]] = {
        scenario_id: [] for scenario_id in _FRICTION_SCENARIO_IDS
    }
    scenario_assumptions: dict[str, dict[str, Any]] = {}

    test_start_index = train_days
    while test_start_index + test_days < len(dates):
        train_start_index = test_start_index - train_days
        train_end_index = test_start_index - 1
        test_end_index = test_start_index + test_days
        train_rows = _fold_payload_rows_v3(
            rows_by_symbol,
            start_index=train_start_index,
            end_index=train_end_index,
        )
        test_rows = _fold_payload_rows_v3(
            rows_by_symbol,
            start_index=test_start_index,
            end_index=test_end_index,
        )
        benchmark_equity_path = _fixed_initial_notional_equity_path(
            rows_by_symbol,
            benchmark_weights,
            start_index=test_start_index,
            end_index=test_end_index,
        )
        benchmark_return, _benchmark_drawdown = _equity_path_metrics(
            benchmark_equity_path
        )
        benchmark_return_pct = round(benchmark_return * 100, 8)
        train_risk = _train_risk_metrics(
            rows_by_symbol,
            signed_weights,
            start_index=train_start_index,
            end_index=train_end_index,
        )

        for scenario_id in _FRICTION_SCENARIO_IDS:
            decision_payload = {
                "engine_version": ENGINE_VERSION_V3,
                "input_snapshot_version": INPUT_SNAPSHOT_VERSION_V2,
                "plan": normalized_plan,
                "config": normalized_config,
                "friction_model": friction_model,
                "scenario_id": scenario_id,
                "decision_cutoff": dates[train_end_index],
                "scheduled_entry_date": dates[test_start_index],
                "train_rows": train_rows,
            }
            decision_input_hash = _sha256(decision_payload)
            fold_input_hash = _sha256(
                {
                    "decision_input_hash": decision_input_hash,
                    "scenario_id": scenario_id,
                    "friction_model": friction_model,
                    "test_rows": test_rows,
                }
            )
            friction_result = apply_paper_friction(
                test_rows,
                positions,
                scenario_id=scenario_id,
            )
            scenario_assumptions.setdefault(
                scenario_id,
                deepcopy(friction_result["scenario"]),
            )
            fillability = _v3_fillability(friction_result)
            cost_percentages = _friction_cost_percentages(friction_result)
            blocked = bool(friction_result["blocked"])
            net_return_pct = friction_result["net_return_pct"]
            max_drawdown_pct = friction_result["max_drawdown_pct"]
            positive = friction_result["positive"]
            paper_equity_path = deepcopy(friction_result["equity_path"])
            equity_nonpositive = (
                any(
                    float(row["net_equity_usd"]) <= 0
                    for row in paper_equity_path
                )
                if paper_equity_path is not None
                else False
            )
            fold_index = len(scenario_folds[scenario_id]) + 1
            fold = {
                "fold_index": fold_index,
                "fold_id": f"fold_{fold_index:03d}",
                "scenario_id": scenario_id,
                "train_start": dates[train_start_index],
                "train_end": dates[train_end_index],
                "decision_cutoff": dates[train_end_index],
                "test_start": dates[test_start_index],
                "scheduled_entry_date": dates[test_start_index],
                "execution_start": dates[test_start_index],
                "entry_price_date": dates[test_start_index],
                "first_return_date": dates[test_start_index + 1],
                "test_end": dates[test_end_index],
                "exit_date": dates[test_end_index],
                "train_days": train_days,
                "test_days": test_days,
                "return_unit": "percent",
                "status": friction_result["status"],
                "blocked": blocked,
                "reason_code": friction_result["reason_code"],
                "fillability": fillability,
                "capacity_gap_usd": fillability["capacity_gap_usd"],
                "first_blocker": deepcopy(fillability["first_blocker"]),
                "gross_return_pct": friction_result["gross_return_pct"],
                "net_return_before_slippage_pct": friction_result[
                    "net_return_before_slippage_pct"
                ],
                "net_return": net_return_pct,
                "benchmark_equal_weight_return": benchmark_return_pct,
                "max_drawdown": max_drawdown_pct,
                "is_positive": positive,
                "net_return_pct": net_return_pct,
                "benchmark_equal_weight_return_pct": benchmark_return_pct,
                "max_drawdown_pct": max_drawdown_pct,
                "positive": positive,
                "equity_nonpositive": equity_nonpositive,
                **cost_percentages,
                "transaction_cost_pct": cost_percentages[
                    "total_friction_cost_pct"
                ],
                "friction_costs": deepcopy(friction_result["costs"]),
                "paper_equity_path": paper_equity_path,
                "signal_source": "versioned_user_paper_weights",
                "signal_weights_pct": {
                    symbol: round(signed_weights[symbol] * 100, 8)
                    for symbol in STORAGE_SYMBOLS
                },
                "risk_input_scope": "rolling_train_before_test_start_only",
                "holding_rule": "fixed_initial_notional_buy_and_hold",
                "rebalancing": "none_within_fold",
                "train_risk": train_risk,
                "decision_input_hash": decision_input_hash,
                "input_hash": fold_input_hash,
            }
            scenario_folds[scenario_id].append(fold)
        test_start_index += step_days

    scenario_results: list[dict[str, Any]] = []
    for scenario_id in _FRICTION_SCENARIO_IDS:
        folds = scenario_folds[scenario_id]
        non_overlapping_folds = _mark_non_overlapping_folds(folds)
        if (
            len(folds) != feasibility["maximum_candidate_fold_count"]
            or len(non_overlapping_folds)
            != feasibility["maximum_non_overlapping_test_fold_count"]
        ):
            raise WalkForwardValidationError(
                "walk-forward fold count does not match preflight feasibility"
            )
        summary = _summary_v3(folds, non_overlapping_folds)
        if summary["status"] == "blocked":
            for fold in folds:
                for field in (
                    "gross_return_pct",
                    "net_return_before_slippage_pct",
                    "net_return",
                    "max_drawdown",
                    "is_positive",
                    "net_return_pct",
                    "max_drawdown_pct",
                    "positive",
                ):
                    fold[field] = None
                fold["paper_equity_path"] = None
                fold["returns_hidden_due_to_scenario_block"] = True
        else:
            for fold in folds:
                fold["returns_hidden_due_to_scenario_block"] = False
        all_checks = [
            check
            for fold in folds
            for check in fold["fillability"]["liquidity_checks"]
        ]
        capacity_gap = max(
            (float(check["capacity_gap_usd"]) for check in all_checks),
            default=0.0,
        )
        assumptions = scenario_assumptions[scenario_id]
        scenario_results.append(
            {
                "scenario_id": scenario_id,
                "label": assumptions["label"],
                "state": summary["status"],
                "blocked": summary["status"] == "blocked",
                "unfillable_fold_count": summary["unfillable_fold_count"],
                "formal_unfillable_fold_count": summary[
                    "formal_unfillable_fold_count"
                ],
                "first_blocker": _scenario_first_blocker(folds),
                "capacity_gap_usd": round(capacity_gap, 8),
                "assumptions": deepcopy(assumptions),
                "folds": folds,
                "summary": summary,
            }
        )

    baseline = scenario_results[0]
    global_input_hash = _sha256(
        {
            "engine_version": ENGINE_VERSION_V3,
            "input_snapshot_version": INPUT_SNAPSHOT_VERSION_V2,
            "plan": normalized_plan,
            "config": normalized_config,
            "friction_model": friction_model,
            "histories": rows_by_symbol,
            "history_metadata": history_metadata,
        }
    )
    return {
        "version": RESULT_VERSION_V3,
        "engine_version": ENGINE_VERSION_V3,
        "input_snapshot_version": INPUT_SNAPSHOT_VERSION_V2,
        "state": baseline["state"],
        "scope": "historical_only",
        "historical_only": True,
        "evaluation_mode": _MODE,
        "strategy_provenance": _STRATEGY_PROVENANCE,
        "out_of_sample_claim": False,
        "source": "futu_qfq_daily_history",
        "price_adjustment": "QFQ",
        "evaluation_as_of_date": common_as_of_date,
        "data_snapshot_cutoff": common_data_cutoff,
        "portfolio_id": normalized_plan["portfolio_id"],
        "portfolio_version": normalized_plan["portfolio_version"],
        "plan_version": normalized_plan["version"],
        "config": normalized_config,
        "symbols": symbols,
        "required_symbols": list(STORAGE_SYMBOLS),
        "covered_symbols": symbols,
        "active_symbols": active_symbols,
        "benchmark_symbols": symbols,
        "friction_model": friction_model,
        "method": {
            "signal": "versioned_user_paper_weights",
            "evaluation_mode": _MODE,
            "strategy_provenance": _STRATEGY_PROVENANCE,
            "out_of_sample_claim": False,
            "risk_input_scope": "rolling_train_before_test_start_only",
            "execution_lag_trading_days": 1,
            "execution_rule": "next_session_close_after_decision_cutoff",
            "holding_rule": "fixed_initial_notional_buy_and_hold",
            "rebalancing": "none_within_fold",
            "commission_included": True,
            "slippage_included": True,
            "short_borrow_fee_included": True,
            "short_borrow_day_count": "actual_calendar_days_over_365",
            "liquidity_capacity_proxy_included": True,
            "liquidity_proxy_version": PAPER_LIQUIDITY_PROXY_VERSION,
            "benchmark_friction_applied": False,
            "actual_execution": False,
            "actual_execution_observed": False,
            "out_of_sample": False,
        },
        "data_quality": {
            "common_trading_days": len(dates),
            "actual_start": dates[0] if dates else None,
            "actual_end": dates[-1] if dates else None,
            "four_symbol_calendar_aligned": True,
            "completed_sessions_only": True,
            "ohlcv_and_turnover_preserved": True,
            "missing_or_nonpositive_liquidity_fails_closed": True,
        },
        "feasibility": feasibility,
        "input_hash": global_input_hash,
        "scenario_results": scenario_results,
        "folds": deepcopy(baseline["folds"]),
        "summary": deepcopy(baseline["summary"]),
        "interpretation": (
            "This is a retroactive fixed-plan paper replay with immutable "
            "commission, slippage, borrow-fee, and liquidity-proxy assumptions. "
            "It is not observed execution, an out-of-sample claim, a future win "
            "rate, investment advice, or an order."
        ),
        "safety": {
            "provider_calls_total": 0,
            "openai_calls": 0,
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
            "actual_execution": False,
            "actual_execution_observed": False,
            "out_of_sample": False,
            "out_of_sample_claim": False,
        },
        "provider_calls_total": 0,
        "openai_calls": 0,
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
        "actual_execution": False,
        "actual_execution_observed": False,
        "out_of_sample": False,
    }


def _run_walk_forward_v4(
    histories: Any,
    plan: Any,
    config: Any,
) -> dict[str, Any]:
    """Run the offline fold-trained strategy engine over retrospective data."""

    normalized_plan = _normalize_plan_v2(plan)
    normalized_config = _normalize_config_v3(config)
    strategy_rule_contract = normalized_config["strategy_rule_contract"]
    expected_contract = build_strategy_rule_contract(normalized_plan, RULE_ID)
    if strategy_rule_contract != expected_contract:
        raise WalkForwardValidationError(
            "config.strategy_rule_contract does not match the frozen source portfolio"
        )
    strategy_contract_sha256 = _sha256(strategy_rule_contract)

    rows_by_symbol, history_metadata = _normalize_histories_v3(histories)
    symbols = list(rows_by_symbol)
    dates = [row["date"] for row in rows_by_symbol[symbols[0]]]
    common_as_of_date = history_metadata[symbols[0]]["as_of_date"]
    common_data_cutoff = history_metadata[symbols[0]]["last_completed_session"]
    if normalized_plan["evaluation_as_of_date"] != common_as_of_date:
        raise WalkForwardValidationError(
            "plan.evaluation_as_of_date does not match frozen history"
        )
    if normalized_plan["data_snapshot_cutoff"] != common_data_cutoff:
        raise WalkForwardValidationError(
            "plan.data_snapshot_cutoff does not match frozen history"
        )
    if not dates or dates[-1] != common_data_cutoff:
        raise WalkForwardValidationError(
            "the final frozen-history row must match data_snapshot_cutoff"
        )

    train_days = normalized_config["train_days"]
    test_days = normalized_config["test_days"]
    step_days = normalized_config["step_days"]
    feasibility = calculate_walk_forward_feasibility(len(dates), normalized_config)
    if feasibility["status"] != "ready":
        raise WalkForwardFeasibilityError(feasibility)

    scenario_set = get_storage_friction_scenarios()
    friction_model = {
        "version": PAPER_FRICTION_MODEL_VERSION,
        "scenario_set": scenario_set,
        "liquidity_proxy_version": PAPER_LIQUIDITY_PROXY_VERSION,
        "unfillable_policy": UNFILLABLE_POLICY,
        "benchmark_friction_applied": False,
        "capacity_is_proxy": True,
        "partial_fills_allowed": False,
        "position_shrinking_allowed": False,
        "date_shifting_allowed": False,
        "actual_execution": False,
        "actual_execution_observed": False,
    }
    benchmark_weights = {symbol: 1 / len(symbols) for symbol in symbols}
    scenario_folds: dict[str, list[dict[str, Any]]] = {
        scenario_id: [] for scenario_id in _FRICTION_SCENARIO_IDS
    }
    scenario_assumptions: dict[str, dict[str, Any]] = {}

    test_start_index = train_days
    while test_start_index + test_days < len(dates):
        train_start_index = test_start_index - train_days
        train_end_index = test_start_index - 1
        test_end_index = test_start_index + test_days
        if train_end_index + 1 != test_start_index:
            raise WalkForwardValidationError(
                "test_start must be the trading session immediately after train_end"
            )

        strategy_decision, selected_positions, signed_weights = (
            _fit_v4_strategy_decision(
                rows_by_symbol,
                strategy_rule_contract,
                train_start_index=train_start_index,
                train_end_index=train_end_index,
            )
        )
        # Fitting is complete before any scenario or test-row processing.
        decision_input_hash = _sha256(
            {
                "engine_version": ENGINE_VERSION_V4,
                "input_snapshot_version": INPUT_SNAPSHOT_VERSION_V3,
                "plan": normalized_plan,
                "strategy_rule_contract": strategy_rule_contract,
                "strategy_decision": strategy_decision,
                "decision_cutoff": dates[train_end_index],
                "scheduled_entry_date": dates[test_start_index],
            }
        )
        strategy_decision_sha256 = _sha256(strategy_decision)
        test_rows = _fold_payload_rows_v3(
            rows_by_symbol,
            start_index=test_start_index,
            end_index=test_end_index,
        )
        benchmark_equity_path = _fixed_initial_notional_equity_path(
            rows_by_symbol,
            benchmark_weights,
            start_index=test_start_index,
            end_index=test_end_index,
        )
        benchmark_return, _benchmark_drawdown = _equity_path_metrics(
            benchmark_equity_path
        )
        benchmark_return_pct = round(benchmark_return * 100, 8)
        train_risk = _train_risk_metrics(
            rows_by_symbol,
            signed_weights,
            start_index=train_start_index,
            end_index=train_end_index,
        )

        for scenario_id in _FRICTION_SCENARIO_IDS:
            fold_input_hash = _sha256(
                {
                    "engine_version": ENGINE_VERSION_V4,
                    "input_snapshot_version": INPUT_SNAPSHOT_VERSION_V3,
                    "decision_input_hash": decision_input_hash,
                    "strategy_decision_sha256": strategy_decision_sha256,
                    "scenario_id": scenario_id,
                    "friction_model": friction_model,
                    "test_rows": test_rows,
                }
            )
            friction_result = apply_paper_friction(
                test_rows,
                selected_positions,
                scenario_id=scenario_id,
            )
            scenario_assumptions.setdefault(
                scenario_id,
                deepcopy(friction_result["scenario"]),
            )
            fillability = _v3_fillability(friction_result)
            cost_percentages = _friction_cost_percentages(friction_result)
            blocked = bool(friction_result["blocked"])
            net_return_pct = friction_result["net_return_pct"]
            max_drawdown_pct = friction_result["max_drawdown_pct"]
            positive = friction_result["positive"]
            paper_equity_path = deepcopy(friction_result["equity_path"])
            equity_nonpositive = (
                any(
                    float(row["net_equity_usd"]) <= 0
                    for row in paper_equity_path
                )
                if paper_equity_path is not None
                else False
            )
            fold_index = len(scenario_folds[scenario_id]) + 1
            fold = {
                "fold_index": fold_index,
                "fold_id": f"fold_{fold_index:03d}",
                "scenario_id": scenario_id,
                "train_start": dates[train_start_index],
                "train_end": dates[train_end_index],
                "decision_cutoff": dates[train_end_index],
                "test_start": dates[test_start_index],
                "scheduled_entry_date": dates[test_start_index],
                "execution_start": dates[test_start_index],
                "entry_price_date": dates[test_start_index],
                "first_return_date": dates[test_start_index + 1],
                "test_end": dates[test_end_index],
                "exit_date": dates[test_end_index],
                "train_days": train_days,
                "test_days": test_days,
                "test_start_is_next_trading_session": True,
                "return_unit": "percent",
                "status": friction_result["status"],
                "blocked": blocked,
                "reason_code": friction_result["reason_code"],
                "fillability": fillability,
                "capacity_gap_usd": fillability["capacity_gap_usd"],
                "first_blocker": deepcopy(fillability["first_blocker"]),
                "gross_return_pct": friction_result["gross_return_pct"],
                "net_return_before_slippage_pct": friction_result[
                    "net_return_before_slippage_pct"
                ],
                "net_return": net_return_pct,
                "benchmark_equal_weight_return": benchmark_return_pct,
                "max_drawdown": max_drawdown_pct,
                "is_positive": positive,
                "net_return_pct": net_return_pct,
                "benchmark_equal_weight_return_pct": benchmark_return_pct,
                "max_drawdown_pct": max_drawdown_pct,
                "positive": positive,
                "equity_nonpositive": equity_nonpositive,
                **cost_percentages,
                "transaction_cost_pct": cost_percentages[
                    "total_friction_cost_pct"
                ],
                "friction_costs": deepcopy(friction_result["costs"]),
                "paper_equity_path": paper_equity_path,
                "signal_source": RULE_ID,
                "signal_weights_pct": deepcopy(strategy_decision["weights_pct"]),
                "strategy_decision": deepcopy(strategy_decision),
                "strategy_decision_sha256": strategy_decision_sha256,
                "risk_input_scope": "fold_training_window_only",
                "holding_rule": "fixed_initial_notional_buy_and_hold",
                "rebalancing": "fold_entry_only",
                "train_risk": train_risk,
                "prospective_test_protocol": True,
                "test_data_excluded_from_fold_fit": True,
                "retrospective_dataset": True,
                "out_of_sample": False,
                "out_of_sample_claim": False,
                "future_performance_claim": False,
                "source_positions_directly_replayed": False,
                "decision_input_hash": decision_input_hash,
                "input_hash": fold_input_hash,
            }
            scenario_folds[scenario_id].append(fold)
        test_start_index += step_days

    scenario_results: list[dict[str, Any]] = []
    for scenario_id in _FRICTION_SCENARIO_IDS:
        folds = scenario_folds[scenario_id]
        non_overlapping_folds = _mark_non_overlapping_folds(folds)
        if (
            len(folds) != feasibility["maximum_candidate_fold_count"]
            or len(non_overlapping_folds)
            != feasibility["maximum_non_overlapping_test_fold_count"]
        ):
            raise WalkForwardValidationError(
                "walk-forward fold count does not match preflight feasibility"
            )
        summary = _summary_v3(folds, non_overlapping_folds)
        if summary["status"] == "blocked":
            for fold in folds:
                for field in (
                    "gross_return_pct",
                    "net_return_before_slippage_pct",
                    "net_return",
                    "max_drawdown",
                    "is_positive",
                    "net_return_pct",
                    "max_drawdown_pct",
                    "positive",
                ):
                    fold[field] = None
                fold["paper_equity_path"] = None
                fold["returns_hidden_due_to_scenario_block"] = True
        else:
            for fold in folds:
                fold["returns_hidden_due_to_scenario_block"] = False
        all_checks = [
            check
            for fold in folds
            for check in fold["fillability"]["liquidity_checks"]
        ]
        capacity_gap = max(
            (float(check["capacity_gap_usd"]) for check in all_checks),
            default=0.0,
        )
        assumptions = scenario_assumptions[scenario_id]
        scenario_results.append(
            {
                "scenario_id": scenario_id,
                "label": assumptions["label"],
                "state": summary["status"],
                "blocked": summary["status"] == "blocked",
                "unfillable_fold_count": summary["unfillable_fold_count"],
                "formal_unfillable_fold_count": summary[
                    "formal_unfillable_fold_count"
                ],
                "first_blocker": _scenario_first_blocker(folds),
                "capacity_gap_usd": round(capacity_gap, 8),
                "assumptions": deepcopy(assumptions),
                "folds": folds,
                "summary": summary,
            }
        )

    baseline = scenario_results[0]
    active_symbols = sorted(
        {
            position["symbol"]
            for fold in baseline["folds"]
            for position in fold["strategy_decision"]["selected_positions"]
        }
    )
    global_input_hash = _sha256(
        {
            "engine_version": ENGINE_VERSION_V4,
            "input_snapshot_version": INPUT_SNAPSHOT_VERSION_V3,
            "plan": normalized_plan,
            "config": normalized_config,
            "strategy_contract_sha256": strategy_contract_sha256,
            "friction_model": friction_model,
            "histories": rows_by_symbol,
            "history_metadata": history_metadata,
        }
    )
    return {
        "version": RESULT_VERSION_V4,
        "engine_version": ENGINE_VERSION_V4,
        "input_snapshot_version": INPUT_SNAPSHOT_VERSION_V3,
        "state": baseline["state"],
        "scope": "historical_only",
        "historical_only": True,
        "retrospective_dataset": True,
        "prospective_test_protocol": True,
        "test_data_excluded_from_fold_fit": True,
        "evaluation_mode": _MODE_V2,
        "strategy_provenance": _STRATEGY_PROVENANCE_V2,
        "out_of_sample": False,
        "out_of_sample_claim": False,
        "future_performance_claim": False,
        "source": "futu_qfq_daily_history",
        "price_adjustment": "QFQ",
        "evaluation_as_of_date": common_as_of_date,
        "data_snapshot_cutoff": common_data_cutoff,
        "portfolio_id": normalized_plan["portfolio_id"],
        "portfolio_version": normalized_plan["portfolio_version"],
        "source_user_decision_id": normalized_plan["source_user_decision_id"],
        "decision_anchor_sha256": normalized_plan["decision_anchor_sha256"],
        "source_decision_head_sequence": normalized_plan[
            "source_decision_head_sequence"
        ],
        "source_decision_head_sha256": normalized_plan[
            "source_decision_head_sha256"
        ],
        "plan_version": normalized_plan["version"],
        "config": normalized_config,
        "strategy_rule_contract": deepcopy(strategy_rule_contract),
        "strategy_contract_sha256": strategy_contract_sha256,
        "source_positions_role": "selection_counts_and_side_budgets_only",
        "source_positions_directly_replayed": False,
        "symbols": symbols,
        "required_symbols": list(STORAGE_SYMBOLS),
        "covered_symbols": symbols,
        "active_symbols": active_symbols,
        "benchmark_symbols": symbols,
        "friction_model": friction_model,
        "method": {
            "signal": "training_window_total_return",
            "rule_id": RULE_ID,
            "evaluation_mode": _MODE_V2,
            "strategy_provenance": _STRATEGY_PROVENANCE_V2,
            "fit_scope": "fold_training_window_only",
            "risk_input_scope": "fold_training_window_only",
            "ranking": "descending_return_then_symbol_ascending",
            "weighting": "equal_notional_within_side",
            "source_positions_role": "selection_counts_and_side_budgets_only",
            "source_positions_directly_replayed": False,
            "execution_lag_trading_days": 1,
            "execution_rule": "next_session_close_after_fold_training_cutoff",
            "holding_rule": "fixed_initial_notional_buy_and_hold",
            "rebalancing": "fold_entry_only",
            "commission_included": True,
            "slippage_included": True,
            "short_borrow_fee_included": True,
            "short_borrow_day_count": "actual_calendar_days_over_365",
            "liquidity_capacity_proxy_included": True,
            "liquidity_proxy_version": PAPER_LIQUIDITY_PROXY_VERSION,
            "benchmark_friction_applied": False,
            "partial_fills_allowed": False,
            "position_shrinking_allowed": False,
            "date_shifting_allowed": False,
            "prospective_test_protocol": True,
            "test_data_excluded_from_fold_fit": True,
            "retrospective_dataset": True,
            "actual_execution": False,
            "actual_execution_observed": False,
            "out_of_sample": False,
            "out_of_sample_claim": False,
            "future_performance_claim": False,
        },
        "data_quality": {
            "common_trading_days": len(dates),
            "actual_start": dates[0] if dates else None,
            "actual_end": dates[-1] if dates else None,
            "four_symbol_calendar_aligned": True,
            "completed_sessions_only": True,
            "ohlcv_and_turnover_preserved": True,
            "missing_or_nonpositive_liquidity_fails_closed": True,
            "retrospective_dataset": True,
            "test_data_excluded_from_fold_fit": True,
        },
        "feasibility": feasibility,
        "input_hash": global_input_hash,
        "scenario_results": scenario_results,
        "folds": deepcopy(baseline["folds"]),
        "summary": deepcopy(baseline["summary"]),
        "interpretation": (
            "This retrospective dataset uses a prospective-style protocol: "
            "each fold fits the server-whitelisted rule only on that fold's "
            "training closes, then enters at the next session close. The source "
            "portfolio supplies selection counts and side budgets, not replayed "
            "test holdings. Contract selection was not established before the "
            "full history, so this is not out-of-sample evidence, a future win "
            "rate, a future-performance claim, investment advice, or an order."
        ),
        "safety": {
            "model_calls_total": 0,
            "provider_calls_total": 0,
            "openai_calls": 0,
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
            "actual_execution": False,
            "actual_execution_observed": False,
            "partial_fills_allowed": False,
            "position_shrinking_allowed": False,
            "date_shifting_allowed": False,
            "out_of_sample": False,
            "out_of_sample_claim": False,
            "future_performance_claim": False,
        },
        "model_calls_total": 0,
        "provider_calls_total": 0,
        "openai_calls": 0,
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
        "actual_execution": False,
        "actual_execution_observed": False,
    }


def run_walk_forward_backtest(
    histories: Any,
    plan: Any,
    config: Any,
) -> dict[str, Any]:
    """Dispatch a versioned local replay without switching current aliases."""

    if isinstance(config, dict) and config.get("version") == CONFIG_VERSION_V3:
        return _run_walk_forward_v4(histories, plan, config)
    if isinstance(config, dict) and config.get("version") == CONFIG_VERSION_V2:
        return _run_walk_forward_v3(histories, plan, config)
    return _run_walk_forward_v2(histories, plan, config)


__all__ = [
    "CONFIG_VERSION",
    "CONFIG_VERSION_V1",
    "CONFIG_VERSION_V2",
    "CONFIG_VERSION_V3",
    "ENGINE_VERSION",
    "ENGINE_VERSION_V1",
    "ENGINE_VERSION_V2",
    "ENGINE_VERSION_V3",
    "ENGINE_VERSION_V4",
    "FEASIBILITY_VERSION",
    "INPUT_SNAPSHOT_VERSION",
    "INPUT_SNAPSHOT_VERSION_V1",
    "INPUT_SNAPSHOT_VERSION_V2",
    "INPUT_SNAPSHOT_VERSION_V3",
    "INSUFFICIENT_WINDOWS_REASON",
    "MINIMUM_INDEPENDENT_FOLDS",
    "PLAN_VERSION",
    "PLAN_VERSION_V2",
    "RULE_ID",
    "RESULT_VERSION",
    "RESULT_VERSION_V1",
    "RESULT_VERSION_V2",
    "RESULT_VERSION_V3",
    "RESULT_VERSION_V4",
    "STRATEGY_RULE_CONTRACT_VERSION",
    "WalkForwardFeasibilityError",
    "WalkForwardValidationError",
    "build_strategy_rule_contract",
    "calculate_walk_forward_feasibility",
    "normalize_walk_forward_config",
    "normalize_walk_forward_plan",
    "run_walk_forward_backtest",
]
