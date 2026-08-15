"""Storage-pack candidate-to-simulation contracts.

This module is deliberately outside the generic room/artifact kernel.  It
turns one server-verified trading-research candidate into a deterministic,
paper-only implementation contract for the MU/SNDK/WDC/STX capability pack.
It never accepts account, order, quantity, price, or execution instructions.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from typing import Any

from .decision_lineage import canonical_sha256
from .market.futu_readonly import STORAGE_SYMBOLS


CANDIDATE_SIMULATION_SEED_VERSION = "candidate_simulation_seed_v1"
CANDIDATE_SIMULATION_CONFIRMATION_VERSION = (
    "candidate_simulation_confirmation_v1"
)
CANDIDATE_SIMULATION_CONTRACT_VERSION = "candidate_simulation_contract_v1"
CANDIDATE_SIMULATION_ADAPTER_ID = "storage_single_name_directional_v1"
CANDIDATE_SIMULATION_RULE_ID = "fixed_candidate_direction_replay_v1"
CANDIDATE_SIMULATION_RULE_VERSION = "candidate_simulation_rule_v1"
CANDIDATE_SIMULATION_HORIZONS = frozenset({1, 5, 20})

_CANDIDATE_FIELDS = (
    "title",
    "symbol",
    "direction",
    "horizon_days",
    "thesis",
    "invalidation",
)
_CONFIRMATION_FIELDS = frozenset({
    "version",
    "expected_source_sha256",
    "expected_candidate_revision",
    "expected_candidate_snapshot_sha256",
    "expected_target_weight_pct",
    "strategy_rule_id",
    "user_confirmed",
})
_DIRECTION_TO_SIDE = {"UP": "LONG", "DOWN": "SHORT"}


class CandidateSimulationContractError(ValueError):
    """Typed validation failure suitable for an HTTP JSON error response."""

    def __init__(self, code: str, message: str, *, status: int = 422) -> None:
        super().__init__(message)
        self.code = str(code)
        self.status = int(status)


def _error(code: str, message: str, *, status: int = 422) -> None:
    raise CandidateSimulationContractError(code, message, status=status)


def _is_sha256(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _canonical_candidate_snapshot(value: Any) -> dict[str, Any]:
    source = dict(value) if isinstance(value, Mapping) else {}
    raw_horizon = source.get("horizon_days")
    horizon_days = (
        raw_horizon
        if isinstance(raw_horizon, int) and not isinstance(raw_horizon, bool)
        else None
    )
    return {
        "title": str(source.get("title") or "").strip(),
        "symbol": str(source.get("symbol") or "").strip().upper(),
        "direction": str(source.get("direction") or "UNSPECIFIED").strip().upper(),
        "horizon_days": horizon_days,
        "thesis": str(source.get("thesis") or "").strip(),
        "invalidation": str(source.get("invalidation") or "").strip(),
    }


def _storage_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if symbol and not symbol.startswith("US."):
        symbol = f"US.{symbol}"
    return symbol


def build_candidate_simulation_seed(anchor_value: Any) -> dict[str, Any]:
    """Build a read-only storage adapter seed from a verified decision anchor."""

    anchor = dict(anchor_value) if isinstance(anchor_value, Mapping) else {}
    raw_snapshot = anchor.get("selected_candidate_snapshot")
    raw_snapshot_available = bool(
        isinstance(raw_snapshot, Mapping)
        and any(
            raw_snapshot.get(field) not in (None, "")
            for field in _CANDIDATE_FIELDS
        )
    )
    snapshot = _canonical_candidate_snapshot(raw_snapshot)
    stored_snapshot_sha256 = str(
        anchor.get("selected_candidate_snapshot_sha256") or ""
    ).strip().lower()
    computed_snapshot_sha256 = (
        canonical_sha256(snapshot) if raw_snapshot_available else ""
    )
    applicable = bool(raw_snapshot_available and computed_snapshot_sha256)
    issues: list[dict[str, str]] = []

    if not applicable:
        return {
            "version": CANDIDATE_SIMULATION_SEED_VERSION,
            "adapter_id": CANDIDATE_SIMULATION_ADAPTER_ID,
            "applicable": False,
            "ready": False,
            "status": "legacy_lineage_only",
            "issues": [{
                "code": "CANDIDATE_SIMULATION_SOURCE_UNAVAILABLE",
                "message": "该决定没有正式发言合同中的精确候选快照。",
            }],
            "allowed_rules": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
        }

    if (
        not _is_sha256(stored_snapshot_sha256)
        or stored_snapshot_sha256 != computed_snapshot_sha256
    ):
        issues.append({
            "code": "CANDIDATE_SIMULATION_SNAPSHOT_HASH_MISMATCH",
            "message": "精确候选快照哈希与治理复核记录不一致。",
        })

    symbol = _storage_symbol(snapshot.get("symbol"))
    if symbol not in STORAGE_SYMBOLS:
        issues.append({
            "code": "CANDIDATE_SIMULATION_SYMBOL_UNSUPPORTED",
            "message": "候选标的不属于 MU、SNDK、WDC、STX 存储产业样板。",
        })

    direction = str(snapshot.get("direction") or "UNSPECIFIED")
    side = _DIRECTION_TO_SIDE.get(direction, "")
    if direction == "NEUTRAL":
        issues.append({
            "code": "CANDIDATE_SIMULATION_NEUTRAL_BAND_REQUIRED",
            "message": "NEUTRAL 观点缺少可验证的中性区间，v1 不会把它静默映射为 FLAT。",
        })
    elif direction in {"FLAT", "UNSPECIFIED"} or not side:
        issues.append({
            "code": "CANDIDATE_SIMULATION_DIRECTION_UNSUPPORTED",
            "message": "v1 只支持将 UP 映射为 LONG、DOWN 映射为 SHORT。",
        })

    horizon_days = snapshot.get("horizon_days")
    if horizon_days not in CANDIDATE_SIMULATION_HORIZONS:
        issues.append({
            "code": "CANDIDATE_SIMULATION_HORIZON_UNSUPPORTED",
            "message": "v1 候选模拟期限必须是 1、5 或 20 个交易日。",
        })
    if not snapshot.get("thesis"):
        issues.append({
            "code": "CANDIDATE_SIMULATION_THESIS_REQUIRED",
            "message": "候选缺少可冻结的研究依据。",
        })
    if not snapshot.get("invalidation"):
        issues.append({
            "code": "CANDIDATE_SIMULATION_INVALIDATION_REQUIRED",
            "message": "候选缺少可冻结的失效条件。",
        })

    source = {
        "version": CANDIDATE_SIMULATION_SEED_VERSION,
        "adapter_id": CANDIDATE_SIMULATION_ADAPTER_ID,
        "user_decision_id": str(anchor.get("user_decision_id") or ""),
        "decision_version": str(anchor.get("decision_version") or ""),
        "decision_record_sha256": str(
            anchor.get("decision_record_sha256") or ""
        ),
        "artifact_id": str(anchor.get("artifact_id") or ""),
        "artifact_version": int(anchor.get("artifact_version") or 0),
        "artifact_snapshot_sha256": str(
            anchor.get("artifact_snapshot_sha256") or ""
        ),
        "governance_attestation_sha256": str(
            anchor.get("governance_attestation_sha256") or ""
        ),
        "candidate_id": str(anchor.get("selected_option_id") or ""),
        "candidate_revision": int(
            anchor.get("selected_option_revision") or 0
        ),
        "candidate_origin_message_id": str(
            anchor.get("selected_option_origin_message_id") or ""
        ),
        "candidate_latest_message_id": str(
            anchor.get("selected_option_latest_message_id") or ""
        ),
        "selected_option_snapshot_sha256": str(
            anchor.get("selected_option_snapshot_sha256") or ""
        ),
        "candidate_snapshot": copy.deepcopy(snapshot),
        "candidate_snapshot_sha256": stored_snapshot_sha256,
    }
    for field, code, message in (
        ("user_decision_id", "CANDIDATE_SIMULATION_DECISION_MISSING", "候选缺少用户决定 ID。"),
        ("candidate_id", "CANDIDATE_SIMULATION_CANDIDATE_MISSING", "候选 ID 为空。"),
        ("candidate_origin_message_id", "CANDIDATE_SIMULATION_ORIGIN_MISSING", "候选初始消息 ID 为空。"),
        ("candidate_latest_message_id", "CANDIDATE_SIMULATION_LATEST_MISSING", "候选最新消息 ID 为空。"),
    ):
        if not source[field]:
            issues.append({"code": code, "message": message})
    if source["candidate_revision"] < 1:
        issues.append({
            "code": "CANDIDATE_SIMULATION_REVISION_INVALID",
            "message": "候选修订号无效。",
        })
    if anchor.get("integrity_ok") is not True or anchor.get("current") is not True:
        issues.append({
            "code": "CANDIDATE_SIMULATION_SOURCE_STALE",
            "message": "候选决定已过期或完整性校验失败。",
        })
    if str(anchor.get("action") or "") != "support":
        issues.append({
            "code": "CANDIDATE_SIMULATION_SUPPORT_REQUIRED",
            "message": "只有用户当前明确支持的候选才能建立模拟合同。",
        })

    source_sha256 = canonical_sha256(source)
    ready = not issues
    return {
        **copy.deepcopy(source),
        "applicable": True,
        "ready": ready,
        "status": "ready" if ready else "blocked",
        "issues": issues,
        "source_sha256": source_sha256,
        "symbol": symbol,
        "direction": direction,
        "target_side": side,
        "horizon_days": horizon_days,
        "thesis": snapshot.get("thesis") or "",
        "invalidation": snapshot.get("invalidation") or "",
        "allowed_rules": [{
            "id": CANDIDATE_SIMULATION_RULE_ID,
            "version": CANDIDATE_SIMULATION_RULE_VERSION,
            "label": "固定候选方向持有",
        }] if ready else [],
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
    }


def normalize_candidate_simulation_confirmation(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _error(
            "CANDIDATE_SIMULATION_CONFIRMATION_REQUIRED",
            "建立或改变候选模拟映射前必须由用户明确确认。",
            status=400,
        )
    unknown = set(value) - _CONFIRMATION_FIELDS
    missing = _CONFIRMATION_FIELDS - set(value)
    if unknown:
        _error(
            "CANDIDATE_SIMULATION_UNKNOWN_FIELD",
            "candidate_simulation_confirmation 包含未知字段："
            + "、".join(sorted(unknown)),
            status=400,
        )
    if missing:
        _error(
            "CANDIDATE_SIMULATION_REQUEST_INVALID",
            "candidate_simulation_confirmation 缺少字段："
            + "、".join(sorted(missing)),
            status=400,
        )
    if value.get("version") != CANDIDATE_SIMULATION_CONFIRMATION_VERSION:
        _error(
            "CANDIDATE_SIMULATION_REQUEST_INVALID",
            "候选模拟确认版本无效。",
            status=400,
        )
    if value.get("user_confirmed") is not True:
        _error(
            "CANDIDATE_SIMULATION_CONFIRMATION_REQUIRED",
            "用户尚未确认候选到纸面规格的映射。",
            status=400,
        )
    source_sha256 = str(value.get("expected_source_sha256") or "").strip().lower()
    snapshot_sha256 = str(
        value.get("expected_candidate_snapshot_sha256") or ""
    ).strip().lower()
    if not _is_sha256(source_sha256) or not _is_sha256(snapshot_sha256):
        _error(
            "CANDIDATE_SIMULATION_REQUEST_INVALID",
            "候选模拟确认缺少有效的精确快照令牌。",
            status=400,
        )
    revision = value.get("expected_candidate_revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        _error(
            "CANDIDATE_SIMULATION_REQUEST_INVALID",
            "候选模拟确认的修订号无效。",
            status=400,
        )
    expected_weight = value.get("expected_target_weight_pct")
    if (
        not isinstance(expected_weight, (int, float))
        or isinstance(expected_weight, bool)
        or not math.isfinite(float(expected_weight))
        or float(expected_weight) <= 0
    ):
        _error(
            "CANDIDATE_SIMULATION_REQUEST_INVALID",
            "候选模拟确认缺少有效的精确纸面权重。",
            status=400,
        )
    rule_id = str(value.get("strategy_rule_id") or "").strip()
    if rule_id != CANDIDATE_SIMULATION_RULE_ID:
        _error(
            "CANDIDATE_SIMULATION_RULE_UNSUPPORTED",
            "候选模拟规则不在服务端白名单中。",
        )
    return {
        "version": CANDIDATE_SIMULATION_CONFIRMATION_VERSION,
        "expected_source_sha256": source_sha256,
        "expected_candidate_revision": revision,
        "expected_candidate_snapshot_sha256": snapshot_sha256,
        "expected_target_weight_pct": round(float(expected_weight), 8),
        "strategy_rule_id": rule_id,
        "user_confirmed": True,
    }


def build_candidate_simulation_contract(
    anchor_value: Any,
    normalized_plan: Mapping[str, Any],
    confirmation_value: Any,
) -> dict[str, Any]:
    seed = build_candidate_simulation_seed(anchor_value)
    if seed.get("applicable") is not True or seed.get("ready") is not True:
        first_issue = next(iter(seed.get("issues") or []), {})
        _error(
            str(first_issue.get("code") or "CANDIDATE_SIMULATION_SOURCE_UNAVAILABLE"),
            str(first_issue.get("message") or "候选模拟来源不可用。"),
            status=409 if str(first_issue.get("code") or "").endswith("STALE") else 422,
        )
    confirmation = normalize_candidate_simulation_confirmation(
        confirmation_value
    )
    if confirmation["expected_source_sha256"] != seed["source_sha256"]:
        _error(
            "CANDIDATE_SIMULATION_SOURCE_STALE",
            "候选来源已变化，请刷新决定包后重新确认。",
            status=409,
        )
    if (
        confirmation["expected_candidate_revision"]
        != seed["candidate_revision"]
        or confirmation["expected_candidate_snapshot_sha256"]
        != seed["candidate_snapshot_sha256"]
    ):
        _error(
            "CANDIDATE_SIMULATION_SNAPSHOT_HASH_MISMATCH",
            "候选精确版本已变化，请刷新后重新确认。",
            status=409,
        )

    positions = [
        copy.deepcopy(dict(item))
        for item in normalized_plan.get("positions") or []
        if isinstance(item, Mapping)
    ]
    active = [
        position
        for position in positions
        if str(position.get("side") or "").upper() != "FLAT"
        or float(position.get("weight_pct") or 0) != 0
    ]
    if len(active) != 1:
        _error(
            "CANDIDATE_SIMULATION_EXTRA_ACTIVE_POSITION",
            "v1 候选模拟必须只有候选标的一个非观望仓位。",
        )
    target = active[0]
    if str(target.get("symbol") or "").upper() != seed["symbol"]:
        _error(
            "CANDIDATE_SIMULATION_SYMBOL_MISMATCH",
            "纸面组合的唯一活跃标的与用户所选候选不一致。",
        )
    if str(target.get("side") or "").upper() != seed["target_side"]:
        _error(
            "CANDIDATE_SIMULATION_POSITION_SIDE_MISMATCH",
            "纸面方向与候选方向映射不一致。",
        )
    weight = float(target.get("weight_pct") or 0)
    if weight <= 0:
        _error(
            "CANDIDATE_SIMULATION_WEIGHT_REQUIRED",
            "候选标的必须由用户设置正的纸面权重。",
        )
    if round(weight, 8) != confirmation["expected_target_weight_pct"]:
        _error(
            "CANDIDATE_SIMULATION_WEIGHT_CONFIRMATION_MISMATCH",
            "用户确认的纸面权重与提交的组合权重不一致。",
            status=409,
        )
    if str(target.get("thesis") or "").strip() != seed["thesis"]:
        _error(
            "CANDIDATE_SIMULATION_THESIS_MISMATCH",
            "纸面组合不能改写正式候选的研究依据。",
        )
    if str(target.get("invalidation") or "").strip() != seed["invalidation"]:
        _error(
            "CANDIDATE_SIMULATION_INVALIDATION_MISMATCH",
            "纸面组合不能改写或删除正式候选的失效条件。",
        )

    source = {
        key: copy.deepcopy(seed[key])
        for key in (
            "version",
            "adapter_id",
            "user_decision_id",
            "decision_version",
            "decision_record_sha256",
            "artifact_id",
            "artifact_version",
            "artifact_snapshot_sha256",
            "governance_attestation_sha256",
            "candidate_id",
            "candidate_revision",
            "candidate_origin_message_id",
            "candidate_latest_message_id",
            "selected_option_snapshot_sha256",
            "candidate_snapshot",
            "candidate_snapshot_sha256",
            "source_sha256",
        )
    }
    implementation = {
        "mapping_policy": "single_name_exact_v1",
        "target_symbol": seed["symbol"],
        "target_side": seed["target_side"],
        "target_weight_pct": round(weight, 8),
        "other_symbols_policy": "flat",
        "thesis_binding": "exact",
        "invalidation_binding": "exact",
        "positions": positions,
        "positions_sha256": canonical_sha256(positions),
    }
    evaluation = {
        "rule_id": CANDIDATE_SIMULATION_RULE_ID,
        "rule_version": CANDIDATE_SIMULATION_RULE_VERSION,
        "evaluation_mode": "fixed_selected_candidate_direction_replay",
        "signal": "human_selected_candidate_direction",
        "horizon_days": int(seed["horizon_days"]),
        "horizon_unit": "trading_sessions",
        "test_days": int(seed["horizon_days"]),
        "step_days": int(seed["horizon_days"]),
        "price_adjustment": "QFQ",
        "source_positions_directly_replayed": True,
        "historical_only": True,
        "out_of_sample_claim": False,
        "future_performance_claim": False,
    }
    evaluation_basis_sha256 = canonical_sha256({
        "adapter_id": CANDIDATE_SIMULATION_ADAPTER_ID,
        "rule_id": CANDIDATE_SIMULATION_RULE_ID,
        "horizon_days": int(seed["horizon_days"]),
        "universe": list(STORAGE_SYMBOLS),
        "interval": "1d",
        "price_adjustment": "QFQ",
        "friction_scenario_set": "storage_friction_scenarios_v1",
    })
    payload = {
        "version": CANDIDATE_SIMULATION_CONTRACT_VERSION,
        "adapter_id": CANDIDATE_SIMULATION_ADAPTER_ID,
        "source": source,
        "implementation": implementation,
        "evaluation": evaluation,
        "evaluation_basis_sha256": evaluation_basis_sha256,
        "user_confirmed": True,
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
    }
    return {
        **payload,
        "contract_sha256": canonical_sha256(payload),
    }


def verify_candidate_simulation_contract(
    contract_value: Any,
    anchor_value: Any,
    normalized_plan: Mapping[str, Any],
) -> dict[str, Any]:
    contract = copy.deepcopy(dict(contract_value)) if isinstance(
        contract_value, Mapping
    ) else {}
    source = contract.get("source") if isinstance(
        contract.get("source"), Mapping
    ) else {}
    confirmation = {
        "version": CANDIDATE_SIMULATION_CONFIRMATION_VERSION,
        "expected_source_sha256": str(source.get("source_sha256") or ""),
        "expected_candidate_revision": source.get("candidate_revision"),
        "expected_candidate_snapshot_sha256": str(
            source.get("candidate_snapshot_sha256") or ""
        ),
        "expected_target_weight_pct": (
            contract.get("implementation")
            if isinstance(contract.get("implementation"), Mapping)
            else {}
        ).get("target_weight_pct"),
        "strategy_rule_id": str(
            (
                contract.get("evaluation")
                if isinstance(contract.get("evaluation"), Mapping)
                else {}
            ).get("rule_id")
            or ""
        ),
        "user_confirmed": contract.get("user_confirmed"),
    }
    expected = build_candidate_simulation_contract(
        anchor_value,
        normalized_plan,
        confirmation,
    )
    if contract != expected:
        _error(
            "CANDIDATE_SIMULATION_CONTRACT_HASH_MISMATCH",
            "候选模拟合同与当前候选或组合内容不一致。",
            status=409,
        )
    return copy.deepcopy(expected)


def candidate_simulation_contract_self_integrity(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    contract = copy.deepcopy(dict(value))
    stored_sha256 = str(contract.pop("contract_sha256", "") or "")
    return bool(
        _is_sha256(stored_sha256)
        and contract.get("version") == CANDIDATE_SIMULATION_CONTRACT_VERSION
        and contract.get("execution_capability") == "none"
        and contract.get("live_trading_allowed") is False
        and contract.get("can_autonomously_decide") is False
        and canonical_sha256(contract) == stored_sha256
    )


__all__ = [
    "CANDIDATE_SIMULATION_ADAPTER_ID",
    "CANDIDATE_SIMULATION_CONFIRMATION_VERSION",
    "CANDIDATE_SIMULATION_CONTRACT_VERSION",
    "CANDIDATE_SIMULATION_HORIZONS",
    "CANDIDATE_SIMULATION_RULE_ID",
    "CANDIDATE_SIMULATION_RULE_VERSION",
    "CANDIDATE_SIMULATION_SEED_VERSION",
    "CandidateSimulationContractError",
    "build_candidate_simulation_contract",
    "build_candidate_simulation_seed",
    "candidate_simulation_contract_self_integrity",
    "normalize_candidate_simulation_confirmation",
    "verify_candidate_simulation_contract",
]
