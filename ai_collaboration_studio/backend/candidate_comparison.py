"""Read-only, same-basis comparison for verified candidate replay runs.

The comparison is deliberately a transient projection.  It does not create a
new decision, portfolio, order, or market-data request.  Every selected run
must already be a fully verified storage candidate replay, and all runs must
share the same frozen price rows, horizon, paper weight, engine generation,
and server-owned friction assumptions before any result metric is exposed.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from typing import Any

from .candidate_simulation_contract import (
    CANDIDATE_SIMULATION_CONTRACT_VERSION,
    CANDIDATE_SIMULATION_RULE_ID,
    candidate_simulation_contract_self_integrity,
)
from .decision_lineage import canonical_sha256
from .market.futu_readonly import STORAGE_SYMBOLS
from .walk_forward import (
    CONFIG_VERSION_V2,
    ENGINE_VERSION_V3,
    INPUT_SNAPSHOT_VERSION_V2,
    RESULT_VERSION_V3,
)
from .walk_forward_friction import (
    STORAGE_FRICTION_SCENARIOS_VERSION,
    UNFILLABLE_POLICY,
)


CANDIDATE_COMPARISON_REQUEST_VERSION = "candidate_comparison_request_v1"
CANDIDATE_COMPARISON_PREVIEW_VERSION = "candidate_comparison_preview_v1"
CANDIDATE_COMPARISON_BASIS_VERSION = "candidate_comparison_basis_v1"
MIN_COMPARISON_RUNS = 2
MAX_COMPARISON_RUNS = 6
SCENARIO_IDS = ("baseline", "stressed", "severe")
_REQUEST_FIELDS = frozenset({
    "version",
    "run_ids",
    "user_confirmed_historical_only",
})


class CandidateComparisonError(ValueError):
    """Typed comparison failure suitable for a structured HTTP response."""

    def __init__(self, code: str, message: str, *, status: int = 422) -> None:
        super().__init__(message)
        self.code = str(code)
        self.status = int(status)


def _error(code: str, message: str, *, status: int = 422) -> None:
    raise CandidateComparisonError(code, message, status=status)


def normalize_candidate_comparison_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _error(
            "CANDIDATE_COMPARISON_REQUEST_INVALID",
            "候选比较请求必须是 JSON 对象。",
            status=400,
        )
    unknown = set(value) - _REQUEST_FIELDS
    missing = _REQUEST_FIELDS - set(value)
    if unknown or missing:
        detail = sorted(unknown or missing)
        _error(
            "CANDIDATE_COMPARISON_REQUEST_INVALID",
            "候选比较请求字段不完整或包含未知字段：" + "、".join(detail),
            status=400,
        )
    if value.get("version") != CANDIDATE_COMPARISON_REQUEST_VERSION:
        _error(
            "CANDIDATE_COMPARISON_REQUEST_INVALID",
            "候选比较请求版本无效。",
            status=400,
        )
    raw_run_ids = value.get("run_ids")
    if (
        not isinstance(raw_run_ids, Sequence)
        or isinstance(raw_run_ids, (str, bytes, bytearray))
    ):
        _error(
            "CANDIDATE_COMPARISON_SELECTION_INVALID",
            "候选比较必须提交回放记录 ID 数组。",
            status=400,
        )
    run_ids = [str(item or "").strip() for item in raw_run_ids]
    if not MIN_COMPARISON_RUNS <= len(run_ids) <= MAX_COMPARISON_RUNS:
        _error(
            "CANDIDATE_COMPARISON_SELECTION_INVALID",
            f"候选比较必须选择 {MIN_COMPARISON_RUNS}–{MAX_COMPARISON_RUNS} 条回放记录。",
            status=400,
        )
    if any(not run_id or len(run_id) > 160 for run_id in run_ids):
        _error(
            "CANDIDATE_COMPARISON_SELECTION_INVALID",
            "候选比较包含无效的回放记录 ID。",
            status=400,
        )
    if len(set(run_ids)) != len(run_ids):
        _error(
            "CANDIDATE_COMPARISON_DUPLICATE_RUN",
            "同一回放记录不能重复进入候选比较。",
            status=400,
        )
    if value.get("user_confirmed_historical_only") is not True:
        _error(
            "CANDIDATE_COMPARISON_ACKNOWLEDGEMENT_REQUIRED",
            "用户必须确认该比较仅代表历史模拟，不是未来胜率或交易指令。",
            status=400,
        )
    return {
        "version": CANDIDATE_COMPARISON_REQUEST_VERSION,
        "run_ids": run_ids,
        "user_confirmed_historical_only": True,
    }


def _finite_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _integer(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def _canonical_hash(value: Any) -> str:
    try:
        return canonical_sha256(value)
    except (TypeError, ValueError, OverflowError):
        return ""


def _mapping(value: Any) -> dict[str, Any]:
    return copy.deepcopy(dict(value)) if isinstance(value, Mapping) else {}


def _issue(code: str, message: str, *, run_id: str = "") -> dict[str, str]:
    issue = {"code": code, "message": message}
    if run_id:
        issue["run_id"] = run_id
    return issue


def _history_dataset_basis(
    input_snapshot: Mapping[str, Any],
    *,
    run_id: str,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    histories = input_snapshot.get("histories")
    issues: list[dict[str, str]] = []
    if not isinstance(histories, Mapping) or set(histories) != set(STORAGE_SYMBOLS):
        return {}, [_issue(
            "CANDIDATE_COMPARISON_DATASET_INCOMPLETE",
            "冻结数据没有完整覆盖 MU、SNDK、WDC、STX。",
            run_id=run_id,
        )]
    normalized: dict[str, Any] = {}
    for symbol in STORAGE_SYMBOLS:
        history = _mapping(histories.get(symbol))
        rows = history.get("rows")
        source_errors = history.get("source_errors")
        if (
            history.get("ok") is not True
            or history.get("source") != "futu_opend"
            or history.get("interval") != "1d"
            or history.get("price_adjustment") != "QFQ"
            or history.get("execution_capability") != "none"
            or history.get("live_trading_allowed") is not False
            or not isinstance(rows, list)
            or not rows
            or (isinstance(source_errors, list) and source_errors)
        ):
            issues.append(_issue(
                "CANDIDATE_COMPARISON_DATASET_INVALID",
                f"{symbol} 的冻结 Futu QFQ 历史不满足只读完整性要求。",
                run_id=run_id,
            ))
            continue
        normalized[symbol] = {
            "symbol": str(history.get("symbol") or symbol),
            "source": "futu_opend",
            "interval": "1d",
            "price_adjustment": "QFQ",
            "as_of_date": str(history.get("as_of_date") or ""),
            "last_completed_session": str(
                history.get("last_completed_session") or ""
            ),
            "actual_start": str(history.get("actual_start") or ""),
            "actual_end": str(history.get("actual_end") or ""),
            # Capture timestamps and request diagnostics are intentionally
            # excluded.  The exact price/volume rows and their market cutoff
            # are the deterministic inputs consumed by the replay engine.
            "rows": copy.deepcopy(rows),
        }
    if issues:
        return {}, issues
    calendars = [
        [str(row.get("market_time") or "")[:10] for row in item["rows"]]
        for item in normalized.values()
    ]
    if any(calendar != calendars[0] for calendar in calendars[1:]):
        return {}, [_issue(
            "CANDIDATE_COMPARISON_CALENDAR_MISMATCH",
            "冻结历史没有使用同一完整交易日历。",
            run_id=run_id,
        )]
    dataset_sha256 = _canonical_hash(normalized)
    if not dataset_sha256:
        return {}, [_issue(
            "CANDIDATE_COMPARISON_DATASET_INVALID",
            "冻结历史不能形成规范化数据指纹。",
            run_id=run_id,
        )]
    return {
        "dataset_content_sha256": dataset_sha256,
        "symbols": list(STORAGE_SYMBOLS),
        "common_trading_days": len(calendars[0]),
        "actual_start": calendars[0][0],
        "actual_end": calendars[0][-1],
        "last_completed_session": normalized[STORAGE_SYMBOLS[0]][
            "last_completed_session"
        ],
    }, []


def _scenario_metrics(
    result: Mapping[str, Any],
    *,
    run_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    raw_scenarios = result.get("scenario_results")
    if not isinstance(raw_scenarios, list):
        return [], [_issue(
            "CANDIDATE_COMPARISON_SCENARIOS_INVALID",
            "回放结果缺少三档服务端摩擦情景。",
            run_id=run_id,
        )]
    by_id: dict[str, dict[str, Any]] = {}
    for value in raw_scenarios:
        scenario = _mapping(value)
        scenario_id = str(
            scenario.get("scenario_id") or scenario.get("id") or ""
        ).strip().lower()
        if scenario_id in SCENARIO_IDS and scenario_id not in by_id:
            by_id[scenario_id] = scenario
    if set(by_id) != set(SCENARIO_IDS):
        return [], [_issue(
            "CANDIDATE_COMPARISON_SCENARIOS_INVALID",
            "回放结果没有精确包含 baseline、stressed、severe 三档情景。",
            run_id=run_id,
        )]
    rows: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    for scenario_id in SCENARIO_IDS:
        scenario = by_id[scenario_id]
        state = str(scenario.get("state") or "").strip().lower()
        blocked = bool(
            scenario.get("blocked") is True
            or state == "blocked"
            or _integer(_finite_number(
                scenario.get("formal_unfillable_fold_count")
            )) > 0
        )
        summary = _mapping(scenario.get("summary"))
        metrics = {
            "portfolio_cumulative_return_pct": _finite_number(
                summary.get("portfolio_cumulative_return_pct")
            ),
            "historical_positive_window_ratio": _finite_number(
                summary.get("historical_positive_fold_ratio")
            ),
            "max_drawdown_pct": _finite_number(summary.get("max_drawdown_pct")),
            "mean_window_return_pct": _finite_number(
                summary.get("mean_return_pct")
            ),
            "worst_window_return_pct": _finite_number(
                summary.get("worst_return_pct")
            ),
        }
        if blocked:
            metrics = {key: None for key in metrics}
        elif any(value is None for value in metrics.values()):
            issues.append(_issue(
                "CANDIDATE_COMPARISON_METRICS_INCOMPLETE",
                f"{scenario_id} 情景缺少可比较的历史指标。",
                run_id=run_id,
            ))
        rows.append({
            "scenario_id": scenario_id,
            "state": state or ("blocked" if blocked else "ready"),
            "blocked": blocked,
            "metrics_visible": not blocked,
            "metrics": metrics,
            "capacity_gap_usd": (
                _finite_number(scenario.get("capacity_gap_usd"))
                if blocked else None
            ),
            "first_blocker": (
                copy.deepcopy(scenario.get("first_blocker"))
                if blocked and isinstance(scenario.get("first_blocker"), Mapping)
                else None
            ),
        })
    return rows, issues


def _record_projection(record_value: Any) -> dict[str, Any]:
    record = _mapping(record_value)
    run = _mapping(record.get("run"))
    input_snapshot = _mapping(record.get("input_snapshot"))
    run_id = str(run.get("id") or "")
    issues: list[dict[str, str]] = []

    required_integrity_fields = (
        "fully_verified",
        "candidate_simulation_binding_verified",
        "candidate_simulation_lineage_verified",
        "candidate_simulation_marker_binding_verified",
        "integrity_profile_verified",
        "walk_forward_v3_lineage_verified",
    )
    if any(run.get(field) is not True for field in required_integrity_fields):
        issues.append(_issue(
            "CANDIDATE_COMPARISON_RUN_UNVERIFIED",
            "所选回放没有通过候选合同、谱系、输入和重算完整性校验。",
            run_id=run_id,
        ))
    if (
        _integer(run.get("record_version")) != 2
        or run.get("engine_version") != ENGINE_VERSION_V3
        or run.get("result_version") != RESULT_VERSION_V3
        or input_snapshot.get("version") != INPUT_SNAPSHOT_VERSION_V2
    ):
        issues.append(_issue(
            "CANDIDATE_COMPARISON_GENERATION_MISMATCH",
            "候选比较只接受已验证的固定方向 v3 回放记录。",
            run_id=run_id,
        ))

    portfolio_snapshot = _mapping(input_snapshot.get("portfolio_snapshot"))
    contract = _mapping(portfolio_snapshot.get("candidate_simulation_contract"))
    source = _mapping(contract.get("source"))
    implementation = _mapping(contract.get("implementation"))
    evaluation = _mapping(contract.get("evaluation"))
    candidate_snapshot = _mapping(source.get("candidate_snapshot"))
    contract_sha256 = str(contract.get("contract_sha256") or "")
    if (
        not candidate_simulation_contract_self_integrity(contract)
        or contract.get("version") != CANDIDATE_SIMULATION_CONTRACT_VERSION
        or evaluation.get("rule_id") != CANDIDATE_SIMULATION_RULE_ID
        or contract.get("execution_capability") != "none"
        or contract.get("live_trading_allowed") is not False
        or contract.get("can_autonomously_decide") is not False
        or contract_sha256
        != str(run.get("candidate_simulation_contract_sha256") or "")
        or str(contract.get("evaluation_basis_sha256") or "")
        != str(run.get("candidate_evaluation_basis_sha256") or "")
    ):
        issues.append(_issue(
            "CANDIDATE_COMPARISON_CONTRACT_INVALID",
            "所选回放的候选语义合同或运行标记不一致。",
            run_id=run_id,
        ))

    config = _mapping(run.get("config"))
    horizon_days = _integer(evaluation.get("horizon_days"))
    target_weight = _finite_number(implementation.get("target_weight_pct"))
    if (
        config.get("version") != CONFIG_VERSION_V2
        or config.get("price_adjustment") != "QFQ"
        or config.get("friction_scenario_set")
        != STORAGE_FRICTION_SCENARIOS_VERSION
        or config.get("unfillable_policy") != UNFILLABLE_POLICY
        or _integer(config.get("test_days")) != horizon_days
        or _integer(config.get("step_days")) != horizon_days
        or horizon_days not in {1, 5, 20}
        or target_weight is None
        or target_weight <= 0
    ):
        issues.append(_issue(
            "CANDIDATE_COMPARISON_EVALUATION_INVALID",
            "候选期限、权重或服务端回放配置不满足同口径比较要求。",
            run_id=run_id,
        ))

    history_basis, history_issues = _history_dataset_basis(
        input_snapshot,
        run_id=run_id,
    )
    issues.extend(history_issues)
    manifest = _mapping(input_snapshot.get("manifest"))
    assumptions = _mapping(manifest.get("assumptions"))
    friction_scenario_set = assumptions.get("friction_scenario_set")
    if not isinstance(friction_scenario_set, Mapping):
        issues.append(_issue(
            "CANDIDATE_COMPARISON_FRICTION_INVALID",
            "冻结输入缺少完整的服务端摩擦情景。",
            run_id=run_id,
        ))
        friction_sha256 = ""
    else:
        friction_sha256 = _canonical_hash(friction_scenario_set)
        if not friction_sha256:
            issues.append(_issue(
                "CANDIDATE_COMPARISON_FRICTION_INVALID",
                "冻结摩擦情景不能形成规范化指纹。",
                run_id=run_id,
            ))

    result = _mapping(run.get("result"))
    scenarios, scenario_issues = _scenario_metrics(result, run_id=run_id)
    issues.extend(scenario_issues)
    basis = {
        "version": CANDIDATE_COMPARISON_BASIS_VERSION,
        **history_basis,
        "walk_forward_config_sha256": (
            _canonical_hash(config) if config else ""
        ),
        "friction_scenario_set_sha256": friction_sha256,
        "candidate_evaluation_basis_sha256": str(
            contract.get("evaluation_basis_sha256") or ""
        ),
        "record_version": _integer(run.get("record_version")),
        "engine_version": str(run.get("engine_version") or ""),
        "result_version": str(run.get("result_version") or ""),
        "input_snapshot_version": str(input_snapshot.get("version") or ""),
        "train_days": _integer(config.get("train_days")),
        "test_days": _integer(config.get("test_days")),
        "step_days": _integer(config.get("step_days")),
        "price_adjustment": str(config.get("price_adjustment") or ""),
        "friction_scenario_set": str(
            config.get("friction_scenario_set") or ""
        ),
        "unfillable_policy": str(config.get("unfillable_policy") or ""),
        "target_weight_pct": target_weight,
    }
    candidate = {
        "run_id": run_id,
        "portfolio_id": str(run.get("portfolio_id") or ""),
        "portfolio_version": _integer(run.get("portfolio_version")),
        "candidate_id": str(source.get("candidate_id") or ""),
        "candidate_revision": _integer(source.get("candidate_revision")),
        "candidate_snapshot_sha256": str(
            source.get("candidate_snapshot_sha256") or ""
        ),
        "title": str(candidate_snapshot.get("title") or ""),
        "symbol": str(implementation.get("target_symbol") or ""),
        "direction": str(candidate_snapshot.get("direction") or ""),
        "side": str(implementation.get("target_side") or ""),
        "target_weight_pct": target_weight,
        "horizon_days": horizon_days,
        "thesis": str(candidate_snapshot.get("thesis") or ""),
        "invalidation": str(candidate_snapshot.get("invalidation") or ""),
        "contract_sha256": contract_sha256,
        "source_decision_current": run.get("source_decision_current") is True,
        "actionable_now": run.get("actionable_now") is True,
        "metrics_visible": False,
        "scenarios": [],
    }
    return {
        "candidate": candidate,
        "basis": basis,
        "scenarios": scenarios,
        "issues": issues,
    }


def build_candidate_comparison_preview(
    room_id: str,
    request_value: Any,
    records_value: Any,
) -> dict[str, Any]:
    request = normalize_candidate_comparison_request(request_value)
    records = list(records_value) if isinstance(records_value, Sequence) else []
    if len(records) != len(request["run_ids"]):
        _error(
            "CANDIDATE_COMPARISON_RUN_NOT_FOUND",
            "至少一条候选回放记录不存在或不属于当前房间。",
            status=404,
        )
    projections = [_record_projection(record) for record in records]
    issues = [
        issue
        for projection in projections
        for issue in projection["issues"]
    ]
    returned_ids = [
        str(projection["candidate"].get("run_id") or "")
        for projection in projections
    ]
    if returned_ids != request["run_ids"]:
        _error(
            "CANDIDATE_COMPARISON_RUN_NOT_FOUND",
            "候选回放记录顺序或归属与请求不一致。",
            status=404,
        )

    candidate_keys = [
        (
            candidate.get("candidate_id"),
            candidate.get("candidate_revision"),
            candidate.get("candidate_snapshot_sha256"),
        )
        for candidate in (
            projection["candidate"] for projection in projections
        )
    ]
    if len(set(candidate_keys)) != len(candidate_keys):
        issues.append(_issue(
            "CANDIDATE_COMPARISON_DUPLICATE_CANDIDATE",
            "同一精确候选版本不能重复进入比较。",
        ))

    bases = [projection["basis"] for projection in projections]
    if bases:
        first = bases[0]
        basis_checks = (
            (
                "dataset_content_sha256",
                "CANDIDATE_COMPARISON_DATASET_MISMATCH",
                "所选候选没有使用完全相同的冻结行情数据。",
            ),
            (
                "walk_forward_config_sha256",
                "CANDIDATE_COMPARISON_CONFIG_MISMATCH",
                "所选候选的训练、测试、步进或回放配置不一致。",
            ),
            (
                "friction_scenario_set_sha256",
                "CANDIDATE_COMPARISON_FRICTION_MISMATCH",
                "所选候选没有使用完全相同的三档摩擦假设。",
            ),
            (
                "candidate_evaluation_basis_sha256",
                "CANDIDATE_COMPARISON_EVALUATION_BASIS_MISMATCH",
                "所选候选的评估期限或规则基础不一致。",
            ),
            (
                "target_weight_pct",
                "CANDIDATE_COMPARISON_WEIGHT_MISMATCH",
                "所选候选的纸面权重不同，不能冒充同口径比较。",
            ),
            (
                "engine_version",
                "CANDIDATE_COMPARISON_GENERATION_MISMATCH",
                "所选候选的回放引擎代际不一致。",
            ),
        )
        for field, code, message in basis_checks:
            if any(basis.get(field) != first.get(field) for basis in bases[1:]):
                issues.append(_issue(code, message))

    # Deduplicate deterministic issue codes while keeping per-run diagnostics.
    unique_issues: list[dict[str, str]] = []
    seen_issues: set[tuple[str, str]] = set()
    for issue in issues:
        key = (str(issue.get("code") or ""), str(issue.get("run_id") or ""))
        if key not in seen_issues:
            seen_issues.add(key)
            unique_issues.append(issue)
    ready = not unique_issues
    candidates: list[dict[str, Any]] = []
    for projection in projections:
        candidate = copy.deepcopy(projection["candidate"])
        if ready:
            candidate["metrics_visible"] = True
            candidate["scenarios"] = copy.deepcopy(projection["scenarios"])
        candidates.append(candidate)
    comparison_basis = copy.deepcopy(bases[0]) if ready and bases else {}
    comparison_basis_sha256 = (
        _canonical_hash(comparison_basis) if comparison_basis else ""
    )
    payload = {
        "version": CANDIDATE_COMPARISON_PREVIEW_VERSION,
        "room_id": str(room_id or ""),
        "selected_run_ids": list(request["run_ids"]),
        "status": "ready" if ready else "blocked",
        "ready": ready,
        "metrics_visible": ready,
        "issues": unique_issues,
        "comparison_basis": comparison_basis,
        "comparison_basis_sha256": comparison_basis_sha256,
        "candidates": candidates,
        "metric_semantics": {
            "historical_positive_window_ratio": (
                "历史正收益窗口比例，不是未来胜率"
            ),
            "ranking_produced": False,
            "winner_claim": False,
            "user_final_decision_required": True,
        },
        "user_confirmed_historical_only": True,
        "historical_only": True,
        "out_of_sample_claim": False,
        "future_performance_claim": False,
        "provider_calls_total": 0,
        "openai_calls": 0,
        "market_data_reads": 0,
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
    }
    return {
        **payload,
        "preview_sha256": canonical_sha256(payload),
    }


class CandidateComparisonService:
    """Resolve selected runs in one SQLite snapshot and build a safe preview."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def preview(self, room_id: str, request_value: Any) -> dict[str, Any]:
        lifecycle_guard = getattr(self.store, "require_room_plugin_action", None)
        if callable(lifecycle_guard):
            try:
                lifecycle_guard(room_id, "candidate_comparison.preview")
            except LookupError:
                raise LookupError("房间不存在。") from None
            except ValueError as exc:
                _error(
                    "CANDIDATE_COMPARISON_PLUGIN_UNAVAILABLE",
                    str(exc),
                    status=409,
                )
        request = normalize_candidate_comparison_request(request_value)
        records = self.store.candidate_comparison_run_records(
            room_id,
            request["run_ids"],
        )
        if records is None:
            raise LookupError("房间不存在。")
        return build_candidate_comparison_preview(room_id, request, records)


__all__ = [
    "CANDIDATE_COMPARISON_BASIS_VERSION",
    "CANDIDATE_COMPARISON_PREVIEW_VERSION",
    "CANDIDATE_COMPARISON_REQUEST_VERSION",
    "CandidateComparisonError",
    "CandidateComparisonService",
    "build_candidate_comparison_preview",
    "normalize_candidate_comparison_request",
]
