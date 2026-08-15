"""P23 decision-before, atomic multi-candidate historical experiments.

This module deliberately does not import a provider adapter and does not use
``artifact_user_decision_v2`` or ``candidate_simulation_contract_v1``.  A user
authorizes one historical comparison of exact governed candidate revisions;
the server owns the common replay specification, reads one frozen market
dataset, computes every arm in memory, and commits the whole cohort once.
"""

from __future__ import annotations

import copy
import json
import math
import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from typing import Any, Callable

from .artifact_governance import governance_blocking_issue_codes
from .capability_packs import room_has_capability
from .decision_lineage import artifact_binding_payload, canonical_sha256
from .football_research import FOOTBALL_RESEARCH_CAPABILITY_PACK_ID
from .stock_research import STOCK_RESEARCH_CAPABILITY_PACK_ID
from .market.futu_readonly import STORAGE_SYMBOLS
from .paper_portfolio_service import PaperPortfolioService
from .store import StudioStore, new_id, now_ms
from .walk_forward import (
    CONFIG_VERSION_V2,
    ENGINE_VERSION_V3,
    RESULT_VERSION_V3,
    normalize_walk_forward_config,
    run_walk_forward_backtest,
)
from .walk_forward_friction import (
    PAPER_FRICTION_MODEL_VERSION,
    PAPER_LIQUIDITY_PROXY_VERSION,
    STORAGE_FRICTION_SCENARIOS_VERSION,
    UNFILLABLE_POLICY,
    get_storage_friction_scenarios,
)


CANDIDATE_EXPERIMENT_REQUEST_VERSION = "candidate_experiment_request_v1"
CANDIDATE_EXPERIMENT_AUTHORIZATION_VERSION = (
    "candidate_experiment_authorization_v1"
)
CANDIDATE_EXPERIMENT_AUTHORIZATION_BINDING_VERSION = (
    "candidate_experiment_authorization_binding_v1"
)
CANDIDATE_EXPERIMENT_SPEC_VERSION = "candidate_experiment_common_spec_v1"
CANDIDATE_EXPERIMENT_DATASET_VERSION = "candidate_experiment_dataset_v1"
CANDIDATE_EXPERIMENT_DATASET_SEAL_VERSION = (
    "candidate_experiment_dataset_seal_v1"
)
CANDIDATE_EXPERIMENT_INPUT_SEAL_VERSION = (
    "candidate_experiment_input_seal_v1"
)
CANDIDATE_EXPERIMENT_ARM_VERSION = "candidate_experiment_arm_v1"
CANDIDATE_EXPERIMENT_AGGREGATE_VERSION = (
    "candidate_experiment_aggregate_v1"
)
CANDIDATE_EXPERIMENT_COHORT_VERSION = "candidate_experiment_cohort_v1"
CANDIDATE_EXPERIMENT_EVALUATION_RULE = (
    "fixed_direction_historical_walk_forward_v1"
)
MIN_EXPERIMENT_ARMS = 2
MAX_EXPERIMENT_ARMS = 6
SERVER_PAPER_WEIGHT_PCT = 25.0
SERVER_TRAIN_DAYS = 99
SUPPORTED_HORIZONS = frozenset({1, 5, 20})
SCENARIO_IDS = ("baseline", "stressed", "severe")

_REQUEST_FIELDS = frozenset({
    "version",
    "client_request_id",
    "artifact_id",
    "expected_artifact_version",
    "expected_governance_attestation_sha256",
    "candidate_selections",
    "user_authorized_historical_comparison",
})
_SELECTION_FIELDS = frozenset({
    "candidate_id",
    "expected_candidate_revision",
    "expected_candidate_origin_message_id",
    "expected_candidate_latest_message_id",
    "expected_candidate_snapshot_sha256",
})
_CLIENT_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SQLITE_INT_MAX = (1 << 63) - 1


def _safety_fields() -> dict[str, Any]:
    return {
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
        "ranking_produced": False,
        "winner_claim": False,
        "user_final_decision_required": True,
    }


class CandidateExperimentError(ValueError):
    """Typed, fail-closed experiment error for service and HTTP callers."""

    def __init__(self, code: str, message: str, *, status: int = 422) -> None:
        super().__init__(message)
        self.code = str(code)
        self.status = int(status)


def _error(code: str, message: str, *, status: int = 422) -> None:
    raise CandidateExperimentError(code, message, status=status)


def _positive_integer(value: Any, code: str, message: str) -> int:
    if isinstance(value, bool):
        _error(code, message, status=400)
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value.strip()):
        parsed = int(value.strip())
    else:
        _error(code, message, status=400)
    if parsed <= 0 or parsed > _SQLITE_INT_MAX:
        _error(code, message, status=400)
    return parsed


def _sha256_token(value: Any, code: str, message: str) -> str:
    token = str(value or "").strip().lower()
    if not _SHA256.fullmatch(token):
        _error(code, message, status=400)
    return token


def _bounded_token(value: Any, code: str, message: str, limit: int = 160) -> str:
    token = str(value or "").strip()
    if not token or len(token) > limit:
        _error(code, message, status=400)
    return token


def normalize_candidate_experiment_request(value: Any) -> dict[str, Any]:
    """Strictly normalize the user authorization request.

    No cutoff, engine, weight, friction, horizon conversion, provider, order,
    decision, or prior run field is accepted from the client.
    """

    if not isinstance(value, Mapping):
        _error(
            "CANDIDATE_EXPERIMENT_REQUEST_INVALID",
            "候选联合实验请求必须是 JSON 对象。",
            status=400,
        )
    unknown = set(value) - _REQUEST_FIELDS
    missing = _REQUEST_FIELDS - set(value)
    if unknown or missing:
        detail = ", ".join(sorted(unknown or missing))
        _error(
            "CANDIDATE_EXPERIMENT_REQUEST_INVALID",
            f"候选联合实验请求字段不完整或包含未知字段：{detail}",
            status=400,
        )
    if value.get("version") != CANDIDATE_EXPERIMENT_REQUEST_VERSION:
        _error(
            "CANDIDATE_EXPERIMENT_REQUEST_VERSION_INVALID",
            "候选联合实验请求版本无效。",
            status=400,
        )
    client_request_id = str(value.get("client_request_id") or "").strip()
    if not _CLIENT_REQUEST_ID.fullmatch(client_request_id):
        _error(
            "CANDIDATE_EXPERIMENT_CLIENT_REQUEST_ID_INVALID",
            "实验 client request id 格式无效。",
            status=400,
        )
    artifact_id = _bounded_token(
        value.get("artifact_id"),
        "CANDIDATE_EXPERIMENT_ARTIFACT_ID_INVALID",
        "候选联合实验必须绑定有效产物 ID。",
    )
    expected_artifact_version = _positive_integer(
        value.get("expected_artifact_version"),
        "CANDIDATE_EXPERIMENT_ARTIFACT_VERSION_INVALID",
        "候选联合实验必须绑定有效产物版本。",
    )
    expected_attestation = _sha256_token(
        value.get("expected_governance_attestation_sha256"),
        "CANDIDATE_EXPERIMENT_ATTESTATION_INVALID",
        "候选联合实验必须绑定有效治理证明 SHA-256。",
    )
    if value.get("user_authorized_historical_comparison") is not True:
        _error(
            "CANDIDATE_EXPERIMENT_AUTHORIZATION_REQUIRED",
            "用户必须明确授权本次只读历史联合实验。",
            status=400,
        )
    raw_selections = value.get("candidate_selections")
    if (
        not isinstance(raw_selections, Sequence)
        or isinstance(raw_selections, (str, bytes, bytearray))
    ):
        _error(
            "CANDIDATE_EXPERIMENT_SELECTION_INVALID",
            "候选联合实验必须提交候选选择数组。",
            status=400,
        )
    if not MIN_EXPERIMENT_ARMS <= len(raw_selections) <= MAX_EXPERIMENT_ARMS:
        _error(
            "CANDIDATE_EXPERIMENT_SELECTION_INVALID",
            "候选联合实验必须选择 2–6 个精确候选。",
            status=400,
        )
    selections: list[dict[str, Any]] = []
    for index, raw_selection in enumerate(raw_selections):
        if not isinstance(raw_selection, Mapping):
            _error(
                "CANDIDATE_EXPERIMENT_SELECTION_INVALID",
                f"第 {index + 1} 个候选选择必须是对象。",
                status=400,
            )
        unknown_selection = set(raw_selection) - _SELECTION_FIELDS
        missing_selection = _SELECTION_FIELDS - set(raw_selection)
        if unknown_selection or missing_selection:
            detail = ", ".join(sorted(unknown_selection or missing_selection))
            _error(
                "CANDIDATE_EXPERIMENT_SELECTION_INVALID",
                f"第 {index + 1} 个候选选择字段无效：{detail}",
                status=400,
            )
        selections.append({
            "candidate_id": _bounded_token(
                raw_selection.get("candidate_id"),
                "CANDIDATE_EXPERIMENT_CANDIDATE_ID_INVALID",
                "候选 ID 无效。",
                120,
            ),
            "expected_candidate_revision": _positive_integer(
                raw_selection.get("expected_candidate_revision"),
                "CANDIDATE_EXPERIMENT_CANDIDATE_REVISION_INVALID",
                "候选 revision 无效。",
            ),
            "expected_candidate_origin_message_id": _bounded_token(
                raw_selection.get("expected_candidate_origin_message_id"),
                "CANDIDATE_EXPERIMENT_CANDIDATE_ORIGIN_INVALID",
                "候选来源消息令牌无效。",
                120,
            ),
            "expected_candidate_latest_message_id": _bounded_token(
                raw_selection.get("expected_candidate_latest_message_id"),
                "CANDIDATE_EXPERIMENT_CANDIDATE_LATEST_INVALID",
                "候选最新消息令牌无效。",
                120,
            ),
            "expected_candidate_snapshot_sha256": _sha256_token(
                raw_selection.get("expected_candidate_snapshot_sha256"),
                "CANDIDATE_EXPERIMENT_CANDIDATE_SNAPSHOT_INVALID",
                "候选快照 SHA-256 无效。",
            ),
        })
    candidate_ids = [selection["candidate_id"] for selection in selections]
    if len(set(candidate_ids)) != len(candidate_ids):
        _error(
            "CANDIDATE_EXPERIMENT_DUPLICATE_CANDIDATE",
            "同一候选 ID 不能重复进入一个联合实验。",
            status=400,
        )
    return {
        "version": CANDIDATE_EXPERIMENT_REQUEST_VERSION,
        "client_request_id": client_request_id,
        "artifact_id": artifact_id,
        "expected_artifact_version": expected_artifact_version,
        "expected_governance_attestation_sha256": expected_attestation,
        "candidate_selections": selections,
        "user_authorized_historical_comparison": True,
    }


def candidate_experiment_request_semantics(
    room_id: str,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "version": CANDIDATE_EXPERIMENT_REQUEST_VERSION,
        "room_id": str(room_id or "").strip(),
        "client_request_id": str(request.get("client_request_id") or ""),
        "artifact_id": str(request.get("artifact_id") or ""),
        "expected_artifact_version": int(
            request.get("expected_artifact_version") or 0
        ),
        "expected_governance_attestation_sha256": str(
            request.get("expected_governance_attestation_sha256") or ""
        ),
        "candidate_selections": copy.deepcopy(
            list(request.get("candidate_selections") or [])
        ),
        "user_authorized_historical_comparison": True,
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_clone(value: Any) -> Any:
    return json.loads(_canonical_json(value))


class _FrozenDict(dict):
    def _blocked(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("candidate experiment dataset is immutable")

    __setitem__ = _blocked
    __delitem__ = _blocked
    clear = _blocked
    pop = _blocked
    popitem = _blocked
    setdefault = _blocked
    update = _blocked
    __ior__ = _blocked

    def __deepcopy__(self, _memo: dict[int, Any]) -> "_FrozenDict":
        return self


class _FrozenList(list):
    def _blocked(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("candidate experiment dataset is immutable")

    __setitem__ = _blocked
    __delitem__ = _blocked
    append = _blocked
    clear = _blocked
    extend = _blocked
    insert = _blocked
    pop = _blocked
    remove = _blocked
    reverse = _blocked
    sort = _blocked
    __iadd__ = _blocked
    __imul__ = _blocked

    def __deepcopy__(self, _memo: dict[int, Any]) -> "_FrozenList":
        return self


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList([_freeze(item) for item in value])
    return value


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return copy.deepcopy(value)
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(
            value,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {token}")
            ),
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _finite_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _integrity_int(value: Any, default: int = 0) -> int:
    """Coerce persisted mirror fields without allowing tamper to escape GET."""

    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip()):
        try:
            return int(value.strip())
        except (TypeError, ValueError, OverflowError):
            return default
    return default


def _integrity_sha256(value: Any) -> str:
    """Hash untrusted persisted JSON, returning a guaranteed mismatch on error."""

    try:
        return canonical_sha256(value)
    except (TypeError, ValueError, OverflowError):
        return ""


def _redacted_arm(
    row: Mapping[str, Any],
    candidate: Mapping[str, Any] | None,
    sequence_no: int,
) -> dict[str, Any]:
    """Build a strict safe projection for a cohort with failed integrity."""

    trusted = candidate if isinstance(candidate, Mapping) else {}
    candidate_id = str(
        trusted.get("candidate_id") or row.get("candidate_id") or ""
    )[:120]
    revision = _integrity_int(
        trusted.get("candidate_revision")
        or row.get("candidate_revision")
        or 0
    )
    snapshot_sha256 = str(
        trusted.get("candidate_snapshot_sha256")
        or row.get("candidate_snapshot_sha256")
        or ""
    ).lower()
    if not _SHA256.fullmatch(snapshot_sha256):
        snapshot_sha256 = ""
    scenarios = [
        {
            "scenario_id": scenario_id,
            "state": "integrity_failed",
            "blocked": True,
            "metrics_visible": False,
            "metrics": {
                "portfolio_cumulative_return_pct": None,
                "historical_positive_window_ratio": None,
                "max_drawdown_pct": None,
                "mean_window_return_pct": None,
                "worst_window_return_pct": None,
            },
            "capacity_gap_usd": None,
            "first_blocker": None,
        }
        for scenario_id in SCENARIO_IDS
    ]
    return {
        "id": str(row.get("id") or "")[:160],
        "version": CANDIDATE_EXPERIMENT_ARM_VERSION,
        "sequence_no": sequence_no,
        "candidate_id": candidate_id,
        "candidate_revision": max(0, revision),
        "candidate_origin_message_id": "",
        "candidate_latest_message_id": "",
        "candidate_snapshot_sha256": snapshot_sha256,
        "candidate_binding_sha256": "",
        "title": "",
        "symbol": "",
        "direction": "",
        "side": "",
        "horizon_days": 0,
        "paper_weight_pct": SERVER_PAPER_WEIGHT_PCT,
        "thesis": "",
        "invalidation": "",
        "evidence": [],
        "counterevidence": [],
        "shared_spec_sha256": "",
        "shared_dataset_seal_sha256": "",
        "plan_sha256": "",
        "result_sha256": "",
        "arm_sha256": "",
        "scenarios": scenarios,
        "metrics_visible": False,
        "integrity_ok": False,
        **_safety_fields(),
    }


def _project_scenarios(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_scenarios = result.get("scenario_results")
    if not isinstance(raw_scenarios, list):
        _error(
            "CANDIDATE_EXPERIMENT_SCENARIOS_INVALID",
            "历史引擎未返回完整的三档摩擦结果。",
        )
    by_id: dict[str, dict[str, Any]] = {}
    for raw in raw_scenarios:
        if not isinstance(raw, Mapping):
            continue
        scenario = copy.deepcopy(dict(raw))
        scenario_id = str(
            scenario.get("scenario_id") or scenario.get("id") or ""
        ).strip().lower()
        if scenario_id in SCENARIO_IDS and scenario_id not in by_id:
            by_id[scenario_id] = scenario
    if set(by_id) != set(SCENARIO_IDS):
        _error(
            "CANDIDATE_EXPERIMENT_SCENARIOS_INVALID",
            "历史引擎必须精确返回 baseline、stressed、severe。",
        )
    projected: list[dict[str, Any]] = []
    for scenario_id in SCENARIO_IDS:
        scenario = by_id[scenario_id]
        state = str(scenario.get("state") or "").strip().lower()
        unfillable = _finite_number(
            scenario.get("formal_unfillable_fold_count")
        )
        blocked = bool(
            scenario.get("blocked") is True
            or state == "blocked"
            or (unfillable is not None and unfillable > 0)
        )
        summary = (
            scenario.get("summary")
            if isinstance(scenario.get("summary"), Mapping)
            else {}
        )
        metrics = {
            "portfolio_cumulative_return_pct": _finite_number(
                summary.get("portfolio_cumulative_return_pct")
            ),
            "historical_positive_window_ratio": _finite_number(
                summary.get("historical_positive_fold_ratio")
            ),
            "max_drawdown_pct": _finite_number(
                summary.get("max_drawdown_pct")
            ),
            "mean_window_return_pct": _finite_number(
                summary.get("mean_return_pct")
            ),
            "worst_window_return_pct": _finite_number(
                summary.get("worst_return_pct")
            ),
        }
        if blocked:
            metrics = {key: None for key in metrics}
        elif any(metric is None for metric in metrics.values()):
            _error(
                "CANDIDATE_EXPERIMENT_METRICS_INCOMPLETE",
                f"{scenario_id} 历史结果缺少共同指标。",
            )
        ratio = metrics["historical_positive_window_ratio"]
        if ratio is not None and not 0 <= ratio <= 1:
            _error(
                "CANDIDATE_EXPERIMENT_METRICS_INVALID",
                f"{scenario_id} 历史正收益窗口比例无效。",
            )
        projected.append({
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
    return projected


def _issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


class CandidateExperimentService:
    """Orchestrate one atomic, provider-free candidate experiment cohort."""

    def __init__(
        self,
        store: StudioStore,
        market_service: Any,
        *,
        engine_runner: Callable[[Any, Any, Any], dict[str, Any]] = (
            run_walk_forward_backtest
        ),
        fault_injector: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.store = store
        self.market_service = market_service
        self.engine_runner = engine_runner
        self.fault_injector = fault_injector

    def _fault(self, stage: str, **context: Any) -> None:
        if self.fault_injector is not None:
            self.fault_injector(stage, context)

    def _artifact_for_version(
        self,
        connection: sqlite3.Connection,
        room_id: str,
        artifact_id: str,
        artifact_version: int,
        *,
        require_current: bool,
    ) -> tuple[dict[str, Any], bool]:
        current_row = connection.execute(
            "SELECT * FROM artifacts WHERE room_id=? AND id=?",
            (room_id, artifact_id),
        ).fetchone()
        if current_row is None:
            _error(
                "CANDIDATE_EXPERIMENT_ARTIFACT_NOT_FOUND",
                "候选联合实验绑定的产物不存在。",
                status=404,
            )
        version_rows = connection.execute(
            """SELECT snapshot_json,snapshot_sha256 FROM artifact_versions
               WHERE room_id=? AND artifact_id=? AND version=?""",
            (room_id, artifact_id, artifact_version),
        ).fetchall()
        if len(version_rows) != 1:
            _error(
                "CANDIDATE_EXPERIMENT_ARTIFACT_VERSION_UNAVAILABLE",
                "产物精确版本快照不存在或不唯一。",
                status=409,
            )
        frozen = _json_object(str(version_rows[0]["snapshot_json"] or "{}"))
        stored_snapshot_sha256 = str(
            version_rows[0]["snapshot_sha256"] or ""
        ).strip().lower()
        if (
            stored_snapshot_sha256
            and stored_snapshot_sha256 != canonical_sha256(frozen)
        ):
            _error(
                "CANDIDATE_EXPERIMENT_ARTIFACT_VERSION_INTEGRITY_FAILED",
                "产物精确版本封印无法验证。",
                status=409,
            )
        if (
            str(frozen.get("id") or "") != artifact_id
            or str(frozen.get("room_id") or "") != room_id
            or int(frozen.get("version") or 0) != artifact_version
            or str(frozen.get("status") or "").upper() != "CONFIRMED"
        ):
            _error(
                "CANDIDATE_EXPERIMENT_ARTIFACT_VERSION_INVALID",
                "产物精确版本快照身份或确认状态无效。",
                status=409,
            )
        current = self.store._artifact_dict(current_row)
        source_current = bool(
            int(current.get("version") or 0) == artifact_version
            and str(current.get("status") or "").upper() == "CONFIRMED"
            and canonical_sha256(artifact_binding_payload(current))
            == canonical_sha256(artifact_binding_payload(frozen))
        )
        if require_current and not source_current:
            _error(
                "CANDIDATE_EXPERIMENT_ARTIFACT_VERSION_DRIFT",
                "产物版本已变化，请刷新后重新授权联合实验。",
                status=409,
            )
        return (current if require_current else frozen), source_current

    def _authorization_context(
        self,
        connection: sqlite3.Connection,
        room_id: str,
        request: Mapping[str, Any],
        *,
        require_current: bool,
    ) -> dict[str, Any]:
        room_row = connection.execute(
            "SELECT * FROM rooms WHERE id=?",
            (room_id,),
        ).fetchone()
        if room_row is None:
            _error(
                "CANDIDATE_EXPERIMENT_ROOM_NOT_FOUND",
                "候选联合实验房间不存在。",
                status=404,
            )
        room = self.store._room_dict(room_row)
        active_pack_ids = {
            str(pack_id or "")
            for pack_id in (
                room.get("active_capability_pack_ids")
                if isinstance(room.get("active_capability_pack_ids"), list)
                else room.get("capability_pack_ids") or []
            )
        }
        # Mixing the storage experiment pack with another research-only domain
        # is denied.  The v1 artifact model has no immutable storage-only
        # discriminator, so a storage contribution alone is insufficient.
        incompatible_research_pack_ids = {
            FOOTBALL_RESEARCH_CAPABILITY_PACK_ID,
            STOCK_RESEARCH_CAPABILITY_PACK_ID,
        }
        if active_pack_ids & incompatible_research_pack_ids:
            _error(
                "CANDIDATE_EXPERIMENT_DOMAIN_NOT_STORAGE_ONLY",
                "足球或通用股票只读能力包与存储候选历史实验不兼容；请使用独立的存储研究房间。",
                status=409,
            )
        if not room_has_capability(
            room,
            "simulation.paper_portfolio",
        ):
            _error(
                "CANDIDATE_EXPERIMENT_CAPABILITY_DISABLED",
                "当前房间未启用纸面历史实验能力。",
                status=409,
            )
        artifact_id = str(request.get("artifact_id") or "")
        artifact_version = int(request.get("expected_artifact_version") or 0)
        artifact, source_current = self._artifact_for_version(
            connection,
            room_id,
            artifact_id,
            artifact_version,
            require_current=require_current,
        )
        plugin_context = self.store._artifact_plugin_registry_context(
            connection,
            artifact,
        )
        frozen_plugin_snapshot = (
            plugin_context.get("snapshot")
            if isinstance(plugin_context.get("snapshot"), dict)
            else {}
        )
        frozen_pack_ids = {
            str(pack_id or "")
            for pack_id in frozen_plugin_snapshot.get(
                "selected_capability_pack_ids",
                [],
            )
            if isinstance(pack_id, str) and str(pack_id or "")
        }
        # The artifact is an immutable authorization source in its own right.
        # Re-check its frozen pack selection instead of trusting only the
        # room's current settings: removing an incompatible pack later must not
        # turn a mixed-domain artifact into a storage candidate experiment.
        if frozen_pack_ids & incompatible_research_pack_ids:
            _error(
                "CANDIDATE_EXPERIMENT_DOMAIN_NOT_STORAGE_ONLY",
                "足球或通用股票只读产物不能进入存储股票候选历史实验。",
                status=409,
            )
        artifact_contribution_ids = {
            str(item.get("contribution_id") or "")
            for item in (
                frozen_plugin_snapshot.get("ui_contributions") or []
                if isinstance(frozen_plugin_snapshot, dict)
                else []
            )
            if isinstance(item, dict)
        }
        if (
            plugin_context.get("integrity_ok") is not True
            or plugin_context.get("runtime_available") is not True
            or "storage_research.artifact_workspace/v1"
            not in artifact_contribution_ids
        ):
            _error(
                "CANDIDATE_EXPERIMENT_ARTIFACT_CAPABILITY_UNAVAILABLE",
                "该产物的冻结插件合同未授权候选历史联合实验。",
                status=409,
            )
        if require_current:
            confirmation_issues = self.store._current_artifact_confirmation_issues(
                connection,
                artifact,
            )
            if confirmation_issues:
                _error(
                    "CANDIDATE_EXPERIMENT_ARTIFACT_CONFIRMATION_INVALID",
                    "当前确认产物未通过证据与确认复核："
                    + "、".join(confirmation_issues[:8]),
                    status=409,
                )
        governance = self.store._project_artifact_governance(
            connection,
            artifact,
        )
        if governance.get("applicable") is not True:
            _error(
                "CANDIDATE_EXPERIMENT_GOVERNANCE_REQUIRED",
                "候选联合实验只接受具有正式候选治理证明的产物。",
                status=422,
            )
        blockers = governance_blocking_issue_codes(governance)
        if governance.get("integrity_ok") is not True or blockers:
            _error(
                "CANDIDATE_EXPERIMENT_GOVERNANCE_INVALID",
                "产物治理证明未通过：" + "、".join(blockers[:8]),
                status=409,
            )
        attestation_sha256 = str(
            governance.get("attestation_sha256") or ""
        ).strip().lower()
        expected_attestation = str(
            request.get("expected_governance_attestation_sha256") or ""
        ).strip().lower()
        if (
            not _SHA256.fullmatch(attestation_sha256)
            or attestation_sha256 != expected_attestation
        ):
            _error(
                "CANDIDATE_EXPERIMENT_ATTESTATION_DRIFT",
                "治理证明已变化，请刷新候选后重新授权联合实验。",
                status=409,
            )
        projection = (
            governance.get("projection")
            if isinstance(governance.get("projection"), dict)
            else {}
        )
        decision = (
            projection.get("decision")
            if isinstance(projection.get("decision"), dict)
            else {}
        )
        options = [
            option
            for option in decision.get("options") or []
            if isinstance(option, dict)
        ]
        risk_gate = (
            governance.get("candidate_risk_reviews")
            if isinstance(governance.get("candidate_risk_reviews"), dict)
            else {}
        )
        candidates: list[dict[str, Any]] = []
        for selection in request.get("candidate_selections") or []:
            candidate_id = str(selection.get("candidate_id") or "")
            matching_options = [
                option
                for option in options
                if str(option.get("id") or "") == candidate_id
            ]
            if len(matching_options) != 1:
                _error(
                    "CANDIDATE_EXPERIMENT_CANDIDATE_NOT_UNIQUE",
                    f"候选 {candidate_id} 不属于已封印的唯一治理选项。",
                    status=409,
                )
            try:
                exact = self.store._governed_exact_candidate_binding(
                    governance,
                    candidate_id,
                )
            except ValueError as exc:
                _error(
                    "CANDIDATE_EXPERIMENT_CANDIDATE_BINDING_INVALID",
                    str(exc),
                    status=409,
                )
            revision = int(exact.get("selected_option_revision") or 0)
            origin_message_id = str(
                exact.get("selected_option_origin_message_id") or ""
            )
            latest_message_id = str(
                exact.get("selected_option_latest_message_id") or ""
            )
            candidate_snapshot = (
                copy.deepcopy(exact.get("selected_candidate_snapshot"))
                if isinstance(exact.get("selected_candidate_snapshot"), dict)
                else {}
            )
            candidate_snapshot_sha256 = str(
                exact.get("selected_candidate_snapshot_sha256") or ""
            ).strip().lower()
            if (
                revision != int(selection.get("expected_candidate_revision") or 0)
                or origin_message_id
                != str(selection.get("expected_candidate_origin_message_id") or "")
                or latest_message_id
                != str(selection.get("expected_candidate_latest_message_id") or "")
                or candidate_snapshot_sha256
                != str(selection.get("expected_candidate_snapshot_sha256") or "")
            ):
                _error(
                    "CANDIDATE_EXPERIMENT_CANDIDATE_VERSION_DRIFT",
                    f"候选 {candidate_id} 的精确版本令牌已变化。",
                    status=409,
                )
            if (
                not candidate_snapshot
                or not _SHA256.fullmatch(candidate_snapshot_sha256)
                or canonical_sha256(candidate_snapshot)
                != candidate_snapshot_sha256
            ):
                _error(
                    "CANDIDATE_EXPERIMENT_CANDIDATE_SNAPSHOT_INVALID",
                    f"候选 {candidate_id} 缺少唯一的当前风险复核快照。",
                    status=409,
                )
            title = str(candidate_snapshot.get("title") or "").strip()
            symbol = str(candidate_snapshot.get("symbol") or "").strip().upper()
            direction = str(
                candidate_snapshot.get("direction") or ""
            ).strip().upper()
            horizon_days = candidate_snapshot.get("horizon_days")
            thesis = str(candidate_snapshot.get("thesis") or "").strip()
            invalidation = str(
                candidate_snapshot.get("invalidation") or ""
            ).strip()
            if (
                not title
                or symbol not in STORAGE_SYMBOLS
                or direction not in {"UP", "DOWN"}
                or isinstance(horizon_days, bool)
                or not isinstance(horizon_days, int)
                or horizon_days not in SUPPORTED_HORIZONS
                or not thesis
                or not invalidation
            ):
                _error(
                    "CANDIDATE_EXPERIMENT_CANDIDATE_INCOMPATIBLE",
                    f"候选 {candidate_id} 的标的、方向、期限、论点或失效条件不兼容。",
                    status=422,
                )
            matching_reviews = [
                review
                for review in risk_gate.get("reviews") or []
                if isinstance(review, dict)
                and str(review.get("candidate_id") or "") == candidate_id
                and int(review.get("candidate_revision") or 0) == revision
                and str(review.get("candidate_latest_message_id") or "")
                == latest_message_id
                and str(review.get("candidate_snapshot_sha256") or "")
                == candidate_snapshot_sha256
                and str(review.get("status") or "").lower() == "current"
            ]
            if len(matching_reviews) != 1:
                _error(
                    "CANDIDATE_EXPERIMENT_RISK_REVIEW_AMBIGUOUS",
                    f"候选 {candidate_id} 的当前风险复核不唯一。",
                    status=409,
                )
            option = copy.deepcopy(matching_options[0])
            review = copy.deepcopy(matching_reviews[0])
            evidence = copy.deepcopy(
                option.get("evidence")
                if isinstance(option.get("evidence"), list)
                else []
            )
            counterevidence: list[Any] = copy.deepcopy(
                option.get("risks")
                if isinstance(option.get("risks"), list)
                else []
            )
            if str(review.get("action") or "").lower() in {"challenge", "reject"}:
                counterevidence.append({
                    "id": str(review.get("review_message_id") or ""),
                    "label": "当前精确版本风控意见",
                    "detail": str(review.get("action") or "").lower(),
                })
            for risk_id in review.get("risk_ids") or []:
                counterevidence.append({
                    "id": str(risk_id),
                    "label": str(risk_id),
                    "detail": "candidate risk review",
                })
            candidate = {
                "candidate_id": candidate_id,
                "candidate_revision": revision,
                "candidate_origin_message_id": origin_message_id,
                "candidate_latest_message_id": latest_message_id,
                "candidate_snapshot": candidate_snapshot,
                "candidate_snapshot_sha256": candidate_snapshot_sha256,
                "artifact_option_snapshot": option,
                "artifact_option_snapshot_sha256": canonical_sha256(option),
                "title": title,
                "symbol": symbol,
                "direction": direction,
                "side": "LONG" if direction == "UP" else "SHORT",
                "horizon_days": horizon_days,
                "thesis": thesis,
                "invalidation": invalidation,
                "evidence": evidence,
                "counterevidence": counterevidence,
                "risk_review": {
                    "action": str(review.get("action") or "").lower(),
                    "review_message_id": str(
                        review.get("review_message_id") or ""
                    ),
                    "reviewer_member_id": str(
                        review.get("reviewer_member_id") or ""
                    ),
                    "reviewer_member_version": int(
                        review.get("reviewer_member_version") or 0
                    ),
                    "risk_ids": copy.deepcopy(list(review.get("risk_ids") or [])),
                    "disposition_only": True,
                },
                **_safety_fields(),
            }
            candidate["candidate_binding_sha256"] = canonical_sha256(candidate)
            candidates.append(candidate)
        horizons = {candidate["horizon_days"] for candidate in candidates}
        if len(horizons) != 1:
            _error(
                "CANDIDATE_EXPERIMENT_HORIZON_MISMATCH",
                "所有候选必须具有完全相同且无需换算的研究期限。",
                status=422,
            )
        artifact_snapshot_sha256 = canonical_sha256(
            artifact_binding_payload(artifact)
        )
        binding = {
            "version": CANDIDATE_EXPERIMENT_AUTHORIZATION_BINDING_VERSION,
            "room_id": room_id,
            "artifact_id": artifact_id,
            "artifact_version": artifact_version,
            "artifact_snapshot_sha256": artifact_snapshot_sha256,
            "governance_attestation_sha256": attestation_sha256,
            "governance_projection_sha256": str(
                governance.get("projection_sha256") or ""
            ),
            "candidate_bindings": copy.deepcopy(candidates),
            "common_horizon_days": next(iter(horizons)),
            "invalidation_conditions": [
                "artifact_exact_version_or_confirmation_changes_before_commit",
                "governance_attestation_or_projection_changes_before_commit",
                "candidate_revision_origin_latest_or_snapshot_changes_before_commit",
                "server_common_spec_or_dataset_seal_mismatch",
                "any_arm_or_aggregate_integrity_failure",
            ],
            "does_not_imply_artifact_support": True,
            "does_not_create_artifact_user_decision": True,
            **_safety_fields(),
        }
        return {
            "artifact": artifact,
            "source_current": source_current,
            "governance": governance,
            "candidates": candidates,
            "common_horizon_days": next(iter(horizons)),
            "artifact_snapshot_sha256": artifact_snapshot_sha256,
            "authorization_binding": binding,
            "authorization_binding_sha256": canonical_sha256(binding),
        }

    @staticmethod
    def _authorization(
        room_id: str,
        request: Mapping[str, Any],
        context: Mapping[str, Any],
        request_semantics: Mapping[str, Any],
        *,
        authorization_id: str,
        created_at: int,
    ) -> dict[str, Any]:
        payload = {
            "version": CANDIDATE_EXPERIMENT_AUTHORIZATION_VERSION,
            "id": authorization_id,
            "room_id": room_id,
            "client_request_id": str(request["client_request_id"]),
            "artifact_id": str(request["artifact_id"]),
            "expected_artifact_version": int(
                request["expected_artifact_version"]
            ),
            "expected_governance_attestation_sha256": str(
                request["expected_governance_attestation_sha256"]
            ),
            "candidate_selections": copy.deepcopy(
                list(request["candidate_selections"])
            ),
            "user_authorized_historical_comparison": True,
            "request_semantics": copy.deepcopy(dict(request_semantics)),
            "request_semantics_sha256": canonical_sha256(request_semantics),
            "artifact_snapshot_sha256": str(
                context["artifact_snapshot_sha256"]
            ),
            "authorization_binding": copy.deepcopy(
                context["authorization_binding"]
            ),
            "authorization_binding_sha256": str(
                context["authorization_binding_sha256"]
            ),
            "does_not_imply_artifact_support": True,
            "does_not_create_artifact_user_decision": True,
            "created_at": int(created_at),
            **_safety_fields(),
        }
        payload["authorization_sha256"] = canonical_sha256(payload)
        return payload

    @staticmethod
    def _public_authorization(
        authorization: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Return only the user's authorization contract, never sealed inputs."""

        return {
            "version": str(authorization.get("version") or ""),
            "client_request_id": str(
                authorization.get("client_request_id") or ""
            ),
            "artifact_id": str(authorization.get("artifact_id") or ""),
            "expected_artifact_version": int(
                authorization.get("expected_artifact_version") or 0
            ),
            "expected_governance_attestation_sha256": str(
                authorization.get(
                    "expected_governance_attestation_sha256"
                )
                or ""
            ),
            "candidate_selections": copy.deepcopy(
                list(authorization.get("candidate_selections") or [])
            ),
            "user_authorized_historical_comparison": (
                authorization.get(
                    "user_authorized_historical_comparison"
                )
                is True
            ),
            "does_not_imply_artifact_support": True,
            "does_not_create_artifact_user_decision": True,
            **_safety_fields(),
        }

    @staticmethod
    def _dataset_payload(histories_value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        histories = _json_clone(histories_value)
        if not isinstance(histories, dict) or set(histories) != set(STORAGE_SYMBOLS):
            _error(
                "CANDIDATE_EXPERIMENT_DATASET_INCOMPLETE",
                "共同历史数据未完整覆盖四只白名单标的。",
            )
        calendars: list[list[str]] = []
        as_of_dates: set[str] = set()
        cutoffs: set[str] = set()
        captured_times: set[str] = set()
        for symbol in STORAGE_SYMBOLS:
            history = histories.get(symbol)
            if not isinstance(history, dict):
                _error(
                    "CANDIDATE_EXPERIMENT_DATASET_INVALID",
                    f"{symbol} 历史数据无效。",
                )
            if (
                history.get("ok") is not True
                or history.get("source") != "futu_opend"
                or str(history.get("interval") or "").lower() != "1d"
                or str(history.get("price_adjustment") or "").upper() != "QFQ"
                or history.get("execution_capability") != "none"
                or history.get("live_trading_allowed") is not False
                or history.get("source_errors")
            ):
                _error(
                    "CANDIDATE_EXPERIMENT_DATASET_INVALID",
                    f"{symbol} 不是完整只读 Futu QFQ 日线。",
                )
            rows = history.get("rows")
            if not isinstance(rows, list) or not rows:
                _error(
                    "CANDIDATE_EXPERIMENT_DATASET_INVALID",
                    f"{symbol} 共同历史数据为空。",
                )
            if any(not isinstance(row, Mapping) for row in rows):
                _error(
                    "CANDIDATE_EXPERIMENT_DATASET_INVALID",
                    f"{symbol} 共同历史数据包含非对象行。",
                )
            calendar = [str(row.get("market_time") or "")[:10] for row in rows]
            if any(not day for day in calendar):
                _error(
                    "CANDIDATE_EXPERIMENT_CALENDAR_INVALID",
                    f"{symbol} 交易日历无效。",
                )
            calendars.append(calendar)
            as_of_dates.add(str(history.get("as_of_date") or ""))
            cutoffs.add(str(history.get("last_completed_session") or ""))
            captured_times.add(str(history.get("captured_at") or ""))
        if any(calendar != calendars[0] for calendar in calendars[1:]):
            _error(
                "CANDIDATE_EXPERIMENT_CALENDAR_MISMATCH",
                "四只标的必须使用完全相同的共同交易日历，禁止静默取交集。",
            )
        if (
            len(as_of_dates) != 1
            or len(cutoffs) != 1
            or len(captured_times) != 1
            or not next(iter(as_of_dates))
            or not next(iter(cutoffs))
            or not next(iter(captured_times))
        ):
            _error(
                "CANDIDATE_EXPERIMENT_DATASET_CUTOFF_MISMATCH",
                "四只标的必须共享完全相同的捕获时间、as-of 与截止日。",
            )
        histories_sha256 = canonical_sha256(histories)
        calendar_sha256 = canonical_sha256(calendars[0])
        seal = {
            "version": CANDIDATE_EXPERIMENT_DATASET_SEAL_VERSION,
            "source": "futu_opend",
            "interval": "1d",
            "price_adjustment": "QFQ",
            "captured_at": next(iter(captured_times)),
            "as_of_date": next(iter(as_of_dates)),
            "cutoff_date": next(iter(cutoffs)),
            "actual_start": calendars[0][0],
            "actual_end": calendars[0][-1],
            "common_trading_days": len(calendars[0]),
            "trading_calendar_sha256": calendar_sha256,
            "dataset_content_sha256": histories_sha256,
            "symbols": list(STORAGE_SYMBOLS),
            "market_data_reads": 1,
            "provider_calls_total": 0,
            "openai_calls": 0,
            **_safety_fields(),
        }
        payload = {
            "version": CANDIDATE_EXPERIMENT_DATASET_VERSION,
            "seal": seal,
            "histories": histories,
        }
        return payload, seal

    @staticmethod
    def _common_spec(
        context: Mapping[str, Any],
        dataset_seal: Mapping[str, Any],
    ) -> dict[str, Any]:
        horizon_days = int(context["common_horizon_days"])
        config = normalize_walk_forward_config({
            "version": CONFIG_VERSION_V2,
            "train_days": SERVER_TRAIN_DAYS,
            "test_days": horizon_days,
            "step_days": horizon_days,
            "price_adjustment": "QFQ",
            "friction_scenario_set": STORAGE_FRICTION_SCENARIOS_VERSION,
            "unfillable_policy": UNFILLABLE_POLICY,
        })
        friction_scenarios = get_storage_friction_scenarios()
        return {
            "version": CANDIDATE_EXPERIMENT_SPEC_VERSION,
            "cutoff_date": str(dataset_seal["cutoff_date"]),
            "evaluation_as_of_date": str(dataset_seal["as_of_date"]),
            "trading_calendar_sha256": str(
                dataset_seal["trading_calendar_sha256"]
            ),
            "price_adjustment": "QFQ",
            "horizon_days": horizon_days,
            "paper_weight_pct": SERVER_PAPER_WEIGHT_PCT,
            "train_days": int(config["train_days"]),
            "test_days": int(config["test_days"]),
            "step_days": int(config["step_days"]),
            "engine_version": ENGINE_VERSION_V3,
            "result_version": RESULT_VERSION_V3,
            "config_version": CONFIG_VERSION_V2,
            "engine_config": config,
            "evaluation_rule": CANDIDATE_EXPERIMENT_EVALUATION_RULE,
            "metric_semantics": {
                "portfolio_cumulative_return_pct": "historical_replay_only",
                "historical_positive_window_ratio": (
                    "historical_positive_fold_share_not_future_win_rate"
                ),
                "max_drawdown_pct": "historical_replay_only",
                "mean_window_return_pct": "historical_replay_only",
                "worst_window_return_pct": "historical_replay_only",
            },
            "friction_scenario_set": STORAGE_FRICTION_SCENARIOS_VERSION,
            "friction_scenarios": friction_scenarios,
            "friction_scenarios_sha256": canonical_sha256(friction_scenarios),
            "friction_model_version": PAPER_FRICTION_MODEL_VERSION,
            "liquidity_proxy_version": PAPER_LIQUIDITY_PROXY_VERSION,
            "unfillable_policy": UNFILLABLE_POLICY,
            "partial_fills_allowed": False,
            "position_shrinking_allowed": False,
            "date_shifting_allowed": False,
            "scenario_ids": list(SCENARIO_IDS),
            "historical_only": True,
            "out_of_sample_claim": False,
            "future_performance_claim": False,
            "market_data_reads": 1,
            "provider_calls_total": 0,
            "openai_calls": 0,
            **_safety_fields(),
        }

    @staticmethod
    def _arm_plan(
        candidate: Mapping[str, Any],
        spec: Mapping[str, Any],
        *,
        arm_id: str,
        created_at: int,
    ) -> dict[str, Any]:
        positions = []
        for symbol in STORAGE_SYMBOLS:
            active = symbol == candidate["symbol"]
            positions.append({
                "symbol": symbol,
                "side": candidate["side"] if active else "FLAT",
                "weight_pct": SERVER_PAPER_WEIGHT_PCT if active else 0,
                "thesis": str(candidate["thesis"]) if active else "",
                "invalidation": str(candidate["invalidation"]) if active else "",
            })
        portfolio = {
            "id": arm_id,
            "version": 1,
            "created_at": int(created_at),
            "name": f"历史联合实验 · {candidate['title']}",
            "positions": positions,
        }
        return PaperPortfolioService._walk_forward_plan(
            portfolio,
            evaluation_as_of_date=str(spec["evaluation_as_of_date"]),
            data_snapshot_cutoff=str(spec["cutoff_date"]),
        )

    def _compute(
        self,
        authorization: Mapping[str, Any],
        context: Mapping[str, Any],
        histories: Any,
        spec: Mapping[str, Any],
        *,
        spec_sha256: str,
        dataset_seal_sha256: str,
    ) -> list[dict[str, Any]]:
        dataset_content_sha256 = canonical_sha256(histories)
        if dataset_content_sha256 != str(
            spec.get("dataset_content_sha256") or dataset_content_sha256
        ):
            _error(
                "CANDIDATE_EXPERIMENT_DATASET_MUTATED",
                "共同内存历史数据在计算前发生变化。",
            )
        arms: list[dict[str, Any]] = []
        for sequence_no, candidate in enumerate(context["candidates"], start=1):
            arm_id = new_id("candidate_experiment_arm")
            plan = self._arm_plan(
                candidate,
                spec,
                arm_id=arm_id,
                created_at=int(authorization["created_at"]),
            )
            before_sha256 = canonical_sha256(histories)
            result = self.engine_runner(
                histories,
                plan,
                spec["engine_config"],
            )
            if canonical_sha256(histories) != before_sha256:
                _error(
                    "CANDIDATE_EXPERIMENT_DATASET_MUTATED",
                    "历史引擎修改了共同不可变内存数据。",
                )
            if (
                not isinstance(result, dict)
                or result.get("version") != RESULT_VERSION_V3
                or result.get("engine_version") != ENGINE_VERSION_V3
                or result.get("config") != spec["engine_config"]
                or result.get("execution_capability") != "none"
                or result.get("live_trading_allowed") is not False
                or result.get("can_autonomously_decide") is not False
            ):
                _error(
                    "CANDIDATE_EXPERIMENT_RESULT_INVALID",
                    f"候选 {candidate['candidate_id']} 的历史结果合同无效。",
                )
            scenarios = _project_scenarios(result)
            plan_sha256 = canonical_sha256(plan)
            result_sha256 = canonical_sha256(result)
            public = {
                "id": arm_id,
                "version": CANDIDATE_EXPERIMENT_ARM_VERSION,
                "sequence_no": sequence_no,
                "candidate_id": candidate["candidate_id"],
                "candidate_revision": candidate["candidate_revision"],
                "candidate_origin_message_id": candidate[
                    "candidate_origin_message_id"
                ],
                "candidate_latest_message_id": candidate[
                    "candidate_latest_message_id"
                ],
                "candidate_snapshot_sha256": candidate[
                    "candidate_snapshot_sha256"
                ],
                "candidate_binding_sha256": candidate[
                    "candidate_binding_sha256"
                ],
                "title": candidate["title"],
                "symbol": candidate["symbol"],
                "direction": candidate["direction"],
                "side": candidate["side"],
                "horizon_days": candidate["horizon_days"],
                "paper_weight_pct": SERVER_PAPER_WEIGHT_PCT,
                "thesis": candidate["thesis"],
                "invalidation": candidate["invalidation"],
                "evidence": copy.deepcopy(candidate["evidence"]),
                "counterevidence": copy.deepcopy(candidate["counterevidence"]),
                "shared_spec_sha256": spec_sha256,
                "shared_dataset_seal_sha256": dataset_seal_sha256,
                "plan_sha256": plan_sha256,
                "result_sha256": result_sha256,
                "scenarios": scenarios,
                "metrics_visible": any(
                    scenario["metrics_visible"] for scenario in scenarios
                ),
                "integrity_ok": True,
                **_safety_fields(),
            }
            arm_seal = {
                "version": CANDIDATE_EXPERIMENT_ARM_VERSION,
                "id": arm_id,
                "sequence_no": sequence_no,
                "candidate_binding_sha256": candidate[
                    "candidate_binding_sha256"
                ],
                "candidate_snapshot_sha256": candidate[
                    "candidate_snapshot_sha256"
                ],
                "spec_sha256": spec_sha256,
                "dataset_seal_sha256": dataset_seal_sha256,
                "plan_sha256": plan_sha256,
                "result_sha256": result_sha256,
                "public_projection_sha256": canonical_sha256(public),
                **_safety_fields(),
            }
            public["arm_sha256"] = canonical_sha256(arm_seal)
            arms.append({
                "id": arm_id,
                "sequence_no": sequence_no,
                "candidate": copy.deepcopy(candidate),
                "plan": plan,
                "plan_sha256": plan_sha256,
                "result": result,
                "result_sha256": result_sha256,
                "public": public,
                "arm_seal": arm_seal,
                "arm_sha256": public["arm_sha256"],
            })
            self._fault(
                "after_arm_compute",
                sequence_no=sequence_no,
                candidate_id=candidate["candidate_id"],
            )
        if canonical_sha256(histories) != dataset_content_sha256:
            _error(
                "CANDIDATE_EXPERIMENT_DATASET_MUTATED",
                "共同内存历史数据在多臂计算期间发生变化。",
            )
        return arms

    def _existing_by_client(
        self,
        connection: sqlite3.Connection,
        room_id: str,
        client_request_id: str,
        request_semantics_sha256: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """SELECT * FROM candidate_experiment_cohorts
               WHERE room_id=? AND client_request_id=?""",
            (room_id, client_request_id),
        ).fetchone()
        if row is None:
            return None
        if str(row["request_semantics_sha256"] or "") != request_semantics_sha256:
            _error(
                "CANDIDATE_EXPERIMENT_IDEMPOTENCY_CONFLICT",
                "同一 client request id 已绑定不同联合实验语义。",
                status=409,
            )
        return self._read_connection(connection, room_id, str(row["id"]))

    def run(self, room_id: str, request_value: Any) -> dict[str, Any]:
        room_id = _bounded_token(
            room_id,
            "CANDIDATE_EXPERIMENT_ROOM_ID_INVALID",
            "候选联合实验房间 ID 无效。",
            120,
        )
        try:
            self.store.require_room_plugin_action(
                room_id,
                "candidate_experiment.run_historical",
            )
        except LookupError:
            _error(
                "CANDIDATE_EXPERIMENT_ROOM_NOT_FOUND",
                "候选联合实验房间不存在。",
                status=404,
            )
        except ValueError as exc:
            _error(
                "CANDIDATE_EXPERIMENT_PLUGIN_UNAVAILABLE",
                str(exc),
                status=409,
            )
        request = normalize_candidate_experiment_request(request_value)
        request_semantics = candidate_experiment_request_semantics(
            room_id,
            request,
        )
        request_semantics_sha256 = canonical_sha256(request_semantics)

        # One store-instance lock keeps concurrent retries from repeating the
        # one market read.  BEGIN IMMEDIATE below remains the cross-instance
        # correctness boundary.
        with self.store._lock:
            with closing(self.store._connect()) as connection:
                connection.execute("BEGIN")
                existing = self._existing_by_client(
                    connection,
                    room_id,
                    request["client_request_id"],
                    request_semantics_sha256,
                )
                if existing is not None:
                    existing["idempotent_replay"] = True
                    return existing
                context = self._authorization_context(
                    connection,
                    room_id,
                    request,
                    require_current=True,
                )
            created_at = now_ms()
            authorization = self._authorization(
                room_id,
                request,
                context,
                request_semantics,
                authorization_id=new_id("candidate_experiment_authorization"),
                created_at=created_at,
            )
            self._fault("after_authorization_preflight")

            # Exactly one batch history read for the whole cohort.
            histories_plain = PaperPortfolioService(
                self.store,
                self.market_service,
            )._walk_forward_histories()
            dataset_payload, dataset_seal = self._dataset_payload(histories_plain)
            dataset_seal_sha256 = canonical_sha256(dataset_payload)
            frozen_histories = _freeze(dataset_payload["histories"])
            if canonical_sha256(frozen_histories) != str(
                dataset_seal["dataset_content_sha256"]
            ):
                _error(
                    "CANDIDATE_EXPERIMENT_DATASET_SEAL_MISMATCH",
                    "共同历史数据封印失败。",
                )
            self._fault(
                "after_market_freeze",
                dataset_seal_sha256=dataset_seal_sha256,
            )

            spec = self._common_spec(context, dataset_seal)
            spec["dataset_content_sha256"] = str(
                dataset_seal["dataset_content_sha256"]
            )
            spec["dataset_seal_sha256"] = dataset_seal_sha256
            spec_sha256 = canonical_sha256(spec)
            arms = self._compute(
                authorization,
                context,
                frozen_histories,
                spec,
                spec_sha256=spec_sha256,
                dataset_seal_sha256=dataset_seal_sha256,
            )
            cohort_id = new_id("candidate_experiment_cohort")
            input_seal = {
                "version": CANDIDATE_EXPERIMENT_INPUT_SEAL_VERSION,
                "cohort_id": cohort_id,
                "authorization_sha256": authorization[
                    "authorization_sha256"
                ],
                "authorization_binding_sha256": authorization[
                    "authorization_binding_sha256"
                ],
                "request_semantics_sha256": request_semantics_sha256,
                "artifact_snapshot_sha256": authorization[
                    "artifact_snapshot_sha256"
                ],
                "governance_attestation_sha256": authorization[
                    "expected_governance_attestation_sha256"
                ],
                "candidate_binding_sha256s": [
                    arm["candidate"]["candidate_binding_sha256"]
                    for arm in arms
                ],
                "spec_sha256": spec_sha256,
                "dataset_seal_sha256": dataset_seal_sha256,
                **_safety_fields(),
            }
            input_seal_sha256 = canonical_sha256(input_seal)
            aggregate = {
                "version": CANDIDATE_EXPERIMENT_AGGREGATE_VERSION,
                "cohort_id": cohort_id,
                "authorization_sha256": authorization[
                    "authorization_sha256"
                ],
                "input_seal_sha256": input_seal_sha256,
                "spec_sha256": spec_sha256,
                "dataset_seal_sha256": dataset_seal_sha256,
                "arm_count": len(arms),
                "ordered_arm_ids": [arm["id"] for arm in arms],
                "ordered_arm_sha256s": [arm["arm_sha256"] for arm in arms],
                "ranking_produced": False,
                "winner_claim": False,
                **_safety_fields(),
            }
            aggregate_sha256 = canonical_sha256(aggregate)
            self._fault("before_atomic_commit", cohort_id=cohort_id)
            return self._commit(
                room_id,
                request,
                request_semantics_sha256=request_semantics_sha256,
                expected_context=context,
                authorization=authorization,
                cohort_id=cohort_id,
                spec=spec,
                spec_sha256=spec_sha256,
                dataset_payload=dataset_payload,
                dataset_seal_sha256=dataset_seal_sha256,
                input_seal=input_seal,
                input_seal_sha256=input_seal_sha256,
                arms=arms,
                aggregate=aggregate,
                aggregate_sha256=aggregate_sha256,
                created_at=created_at,
            )

    def _commit(
        self,
        room_id: str,
        request: Mapping[str, Any],
        *,
        request_semantics_sha256: str,
        expected_context: Mapping[str, Any],
        authorization: Mapping[str, Any],
        cohort_id: str,
        spec: Mapping[str, Any],
        spec_sha256: str,
        dataset_payload: Mapping[str, Any],
        dataset_seal_sha256: str,
        input_seal: Mapping[str, Any],
        input_seal_sha256: str,
        arms: Sequence[Mapping[str, Any]],
        aggregate: Mapping[str, Any],
        aggregate_sha256: str,
        created_at: int,
    ) -> dict[str, Any]:
        with closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._existing_by_client(
                connection,
                room_id,
                str(request["client_request_id"]),
                request_semantics_sha256,
            )
            if existing is not None:
                existing["idempotent_replay"] = True
                return existing
            try:
                self.store._require_room_plugin_action_connection(
                    connection,
                    room_id,
                    "candidate_experiment.run_historical",
                )
            except LookupError:
                _error(
                    "CANDIDATE_EXPERIMENT_ROOM_NOT_FOUND",
                    "candidate experiment room does not exist",
                    status=404,
                )
            except ValueError as exc:
                _error(
                    "CANDIDATE_EXPERIMENT_PLUGIN_UNAVAILABLE",
                    str(exc),
                    status=409,
                )
            current_context = self._authorization_context(
                connection,
                room_id,
                request,
                require_current=True,
            )
            if (
                str(current_context["authorization_binding_sha256"])
                != str(expected_context["authorization_binding_sha256"])
                or [
                    candidate["candidate_binding_sha256"]
                    for candidate in current_context["candidates"]
                ]
                != [
                    candidate["candidate_binding_sha256"]
                    for candidate in expected_context["candidates"]
                ]
            ):
                _error(
                    "CANDIDATE_EXPERIMENT_BINDING_DRIFT",
                    "产物、候选或治理证明在读取期间发生变化；未写入实验。",
                    status=409,
                )
            expected_request_sha256 = canonical_sha256(
                candidate_experiment_request_semantics(room_id, request)
            )
            if expected_request_sha256 != request_semantics_sha256:
                _error(
                    "CANDIDATE_EXPERIMENT_REQUEST_SEMANTICS_DRIFT",
                    "实验请求语义在提交前发生变化；未写入实验。",
                    status=409,
                )
            connection.execute(
                """INSERT INTO candidate_experiment_authorizations(
                       id,room_id,artifact_id,artifact_version,
                       authorization_version,client_request_id,
                       request_semantics_sha256,
                       governance_attestation_sha256,
                       authorization_binding_sha256,authorization_json,
                       authorization_sha256,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    authorization["id"],
                    room_id,
                    request["artifact_id"],
                    request["expected_artifact_version"],
                    CANDIDATE_EXPERIMENT_AUTHORIZATION_VERSION,
                    request["client_request_id"],
                    request_semantics_sha256,
                    request["expected_governance_attestation_sha256"],
                    authorization["authorization_binding_sha256"],
                    _canonical_json(authorization),
                    authorization["authorization_sha256"],
                    created_at,
                ),
            )
            self._fault("after_authorization_insert", cohort_id=cohort_id)
            connection.execute(
                """INSERT INTO candidate_experiment_cohorts(
                       id,room_id,authorization_id,artifact_id,
                       artifact_version,cohort_version,client_request_id,
                       request_semantics_sha256,arm_count,aggregate_json,
                       aggregate_sha256,execution_capability,
                       live_trading_allowed,can_autonomously_decide,
                       ranking_produced,winner_claim,
                       user_final_decision_required,market_data_reads,
                       provider_calls_total,openai_calls,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    cohort_id,
                    room_id,
                    authorization["id"],
                    request["artifact_id"],
                    request["expected_artifact_version"],
                    CANDIDATE_EXPERIMENT_COHORT_VERSION,
                    request["client_request_id"],
                    request_semantics_sha256,
                    len(arms),
                    _canonical_json(aggregate),
                    aggregate_sha256,
                    "none",
                    0,
                    0,
                    0,
                    0,
                    1,
                    1,
                    0,
                    0,
                    created_at,
                ),
            )
            self._fault("after_cohort_insert", cohort_id=cohort_id)
            connection.execute(
                """INSERT INTO candidate_experiment_input_seals(
                       cohort_id,seal_version,spec_json,spec_sha256,
                       dataset_json,dataset_seal_sha256,input_seal_json,
                       input_seal_sha256,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    cohort_id,
                    CANDIDATE_EXPERIMENT_INPUT_SEAL_VERSION,
                    _canonical_json(spec),
                    spec_sha256,
                    _canonical_json(dataset_payload),
                    dataset_seal_sha256,
                    _canonical_json(input_seal),
                    input_seal_sha256,
                    created_at,
                ),
            )
            self._fault("after_input_seal_insert", cohort_id=cohort_id)
            for arm in arms:
                candidate = arm["candidate"]
                connection.execute(
                    """INSERT INTO candidate_experiment_arms(
                           id,cohort_id,sequence_no,arm_version,candidate_id,
                           candidate_revision,candidate_origin_message_id,
                           candidate_latest_message_id,
                           candidate_snapshot_sha256,candidate_binding_sha256,
                           plan_json,plan_sha256,result_json,result_sha256,
                           arm_json,arm_sha256,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        arm["id"],
                        cohort_id,
                        arm["sequence_no"],
                        CANDIDATE_EXPERIMENT_ARM_VERSION,
                        candidate["candidate_id"],
                        candidate["candidate_revision"],
                        candidate["candidate_origin_message_id"],
                        candidate["candidate_latest_message_id"],
                        candidate["candidate_snapshot_sha256"],
                        candidate["candidate_binding_sha256"],
                        _canonical_json(arm["plan"]),
                        arm["plan_sha256"],
                        _canonical_json(arm["result"]),
                        arm["result_sha256"],
                        _canonical_json({
                            "public": arm["public"],
                            "seal": arm["arm_seal"],
                        }),
                        arm["arm_sha256"],
                        created_at,
                    ),
                )
                self._fault(
                    "after_arm_insert",
                    cohort_id=cohort_id,
                    sequence_no=arm["sequence_no"],
                )
            self._fault("before_transaction_exit", cohort_id=cohort_id)
        experiment = self.get(room_id, cohort_id)
        experiment["idempotent_replay"] = False
        return experiment

    def get(self, room_id: str, cohort_id: str) -> dict[str, Any]:
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN")
            experiment = self._read_connection(connection, room_id, cohort_id)
        if experiment is None:
            _error(
                "CANDIDATE_EXPERIMENT_NOT_FOUND",
                "候选联合实验不存在。",
                status=404,
            )
        return experiment

    def _read_connection(
        self,
        connection: sqlite3.Connection,
        room_id: str,
        cohort_id: str,
    ) -> dict[str, Any] | None:
        try:
            return self._read_connection_verified(
                connection,
                room_id,
                cohort_id,
            )
        except Exception:
            # Persisted JSON and SQLite dynamic values are untrusted at read
            # time.  Any ordinary verifier failure must still return a strict
            # no-metrics projection instead of leaking a partial record or
            # turning local tamper into an HTTP 500.
            cohort_row = connection.execute(
                """SELECT * FROM candidate_experiment_cohorts
                   WHERE room_id=? AND id=?""",
                (room_id, cohort_id),
            ).fetchone()
            if cohort_row is None:
                return None
            try:
                arm_rows = connection.execute(
                    """SELECT * FROM candidate_experiment_arms
                       WHERE cohort_id=? ORDER BY sequence_no,id""",
                    (cohort_id,),
                ).fetchall()
            except sqlite3.Error:
                arm_rows = []
            cohort = dict(cohort_row)
            return {
                "version": CANDIDATE_EXPERIMENT_COHORT_VERSION,
                "id": cohort_id,
                "room_id": room_id,
                "artifact_id": str(cohort.get("artifact_id") or "")[:160],
                "artifact_version": _integrity_int(
                    cohort.get("artifact_version") or 0
                ),
                "client_request_id": str(
                    cohort.get("client_request_id") or ""
                )[:128],
                "status": "integrity_failed",
                "integrity_ok": False,
                "metrics_visible": False,
                "integrity_issues": [
                    _issue(
                        "CANDIDATE_EXPERIMENT_INTEGRITY_VERIFIER_FAILED",
                        "联合实验完整性重算失败，整组指标已隐藏。",
                    )
                ],
                "source_current": False,
                "source_invalidation_conditions": [
                    "integrity_verification_failed"
                ],
                "authorization": {},
                "common_spec": {},
                "dataset_seal": {},
                "spec_sha256": "",
                "dataset_seal_sha256": "",
                "input_seal_sha256": "",
                "aggregate_sha256": "",
                "request_semantics_sha256": "",
                "arms": [
                    _redacted_arm(dict(row), None, index + 1)
                    for index, row in enumerate(arm_rows)
                ],
                "market_data_reads": 1,
                "provider_calls_total": 0,
                "openai_calls": 0,
                "historical_only": True,
                "out_of_sample_claim": False,
                "future_performance_claim": False,
                "created_at": 0,
                **_safety_fields(),
            }

    def _read_connection_verified(
        self,
        connection: sqlite3.Connection,
        room_id: str,
        cohort_id: str,
    ) -> dict[str, Any] | None:
        cohort_row = connection.execute(
            """SELECT * FROM candidate_experiment_cohorts
               WHERE room_id=? AND id=?""",
            (room_id, cohort_id),
        ).fetchone()
        if cohort_row is None:
            return None
        authorization_rows = connection.execute(
            """SELECT * FROM candidate_experiment_authorizations
               WHERE id=? AND room_id=?""",
            (cohort_row["authorization_id"], room_id),
        ).fetchall()
        input_rows = connection.execute(
            """SELECT * FROM candidate_experiment_input_seals
               WHERE cohort_id=?""",
            (cohort_id,),
        ).fetchall()
        arm_rows = connection.execute(
            """SELECT * FROM candidate_experiment_arms
               WHERE cohort_id=? ORDER BY sequence_no,id""",
            (cohort_id,),
        ).fetchall()
        issues: list[dict[str, str]] = []

        def add_issue(code: str, message: str) -> None:
            if not any(issue["code"] == code for issue in issues):
                issues.append(_issue(code, message))

        if len(authorization_rows) != 1:
            add_issue(
                "CANDIDATE_EXPERIMENT_AUTHORIZATION_CARDINALITY_INVALID",
                "联合实验授权记录缺失或不唯一。",
            )
        if len(input_rows) != 1:
            add_issue(
                "CANDIDATE_EXPERIMENT_INPUT_SEAL_CARDINALITY_INVALID",
                "联合实验输入封印缺失或不唯一。",
            )
        authorization_row = (
            dict(authorization_rows[0]) if len(authorization_rows) == 1 else {}
        )
        input_row = dict(input_rows[0]) if len(input_rows) == 1 else {}
        cohort = dict(cohort_row)
        authorization = _json_object(authorization_row.get("authorization_json"))
        aggregate = _json_object(cohort.get("aggregate_json"))
        spec = _json_object(input_row.get("spec_json"))
        dataset_payload = _json_object(input_row.get("dataset_json"))
        input_seal = _json_object(input_row.get("input_seal_json"))

        supplied_authorization_sha256 = str(
            authorization.get("authorization_sha256") or ""
        )
        sealed_created_at = _integrity_int(
            authorization.get("created_at") or 0
        )
        authorization_base = copy.deepcopy(authorization)
        authorization_base.pop("authorization_sha256", None)
        if (
            not authorization
            or _integrity_sha256(authorization_base)
            != supplied_authorization_sha256
            or supplied_authorization_sha256
            != str(authorization_row.get("authorization_sha256") or "")
        ):
            add_issue(
                "CANDIDATE_EXPERIMENT_AUTHORIZATION_HASH_MISMATCH",
                "联合实验授权哈希不一致。",
            )
        if (
            str(authorization_row.get("authorization_version") or "")
            != CANDIDATE_EXPERIMENT_AUTHORIZATION_VERSION
            or str(authorization.get("version") or "")
            != CANDIDATE_EXPERIMENT_AUTHORIZATION_VERSION
            or str(authorization.get("id") or "")
            != str(authorization_row.get("id") or "")
            or str(authorization.get("room_id") or "") != room_id
            or str(authorization_row.get("room_id") or "") != room_id
            or str(authorization.get("artifact_id") or "")
            != str(authorization_row.get("artifact_id") or "")
            or _integrity_int(
                authorization.get("expected_artifact_version") or 0
            )
            != _integrity_int(authorization_row.get("artifact_version") or 0)
            or str(authorization.get("client_request_id") or "")
            != str(authorization_row.get("client_request_id") or "")
            or str(
                authorization.get(
                    "expected_governance_attestation_sha256"
                ) or ""
            )
            != str(
                authorization_row.get("governance_attestation_sha256") or ""
            )
        ):
            add_issue(
                "CANDIDATE_EXPERIMENT_AUTHORIZATION_MIRROR_MISMATCH",
                "联合实验授权镜像字段不一致。",
            )
        timestamp_rows: list[Mapping[str, Any]] = [
            authorization_row,
            cohort,
            input_row,
            *(dict(row) for row in arm_rows),
        ]
        if (
            sealed_created_at <= 0
            or any(
                _integrity_int(row.get("created_at") or 0)
                != sealed_created_at
                for row in timestamp_rows
            )
        ):
            add_issue(
                "CANDIDATE_EXPERIMENT_TIMESTAMP_MIRROR_MISMATCH",
                "联合实验授权、cohort、输入与 arm 时间镜像不一致。",
            )
        request_semantics = (
            authorization.get("request_semantics")
            if isinstance(authorization.get("request_semantics"), dict)
            else {}
        )
        request_semantics_sha256 = _integrity_sha256(request_semantics)
        if (
            request_semantics_sha256
            != str(cohort.get("request_semantics_sha256") or "")
            or request_semantics_sha256
            != str(authorization_row.get("request_semantics_sha256") or "")
            or request_semantics_sha256
            != str(authorization.get("request_semantics_sha256") or "")
        ):
            add_issue(
                "CANDIDATE_EXPERIMENT_REQUEST_SEMANTICS_HASH_MISMATCH",
                "联合实验请求语义哈希不一致。",
            )
        request_payload = {
            key: copy.deepcopy(request_semantics.get(key))
            for key in _REQUEST_FIELDS
        }
        request_payload.pop("room_id", None)
        try:
            normalized_request = normalize_candidate_experiment_request(
                request_payload
            )
        except (
            CandidateExperimentError,
            TypeError,
            ValueError,
            KeyError,
            AttributeError,
            OverflowError,
        ):
            normalized_request = {}
            add_issue(
                "CANDIDATE_EXPERIMENT_REQUEST_SEMANTICS_INVALID",
                "联合实验冻结请求语义无效。",
            )

        historical_context: dict[str, Any] = {}
        source_current = False
        if normalized_request:
            try:
                historical_context = self._authorization_context(
                    connection,
                    room_id,
                    normalized_request,
                    require_current=False,
                )
                source_current = bool(historical_context["source_current"])
            except (
                CandidateExperimentError,
                TypeError,
                ValueError,
                KeyError,
                AttributeError,
                OverflowError,
            ):
                add_issue(
                    "CANDIDATE_EXPERIMENT_SOURCE_BINDING_INVALID",
                    "联合实验冻结产物、治理证明或候选绑定不完整。",
                )
        if historical_context and (
            str(historical_context["authorization_binding_sha256"])
            != str(authorization.get("authorization_binding_sha256") or "")
            or str(authorization.get("authorization_binding_sha256") or "")
            != str(authorization_row.get("authorization_binding_sha256") or "")
        ):
            add_issue(
                "CANDIDATE_EXPERIMENT_AUTHORIZATION_BINDING_MISMATCH",
                "联合实验授权绑定已变化。",
            )

        spec_sha256 = _integrity_sha256(spec)
        dataset_seal_sha256 = _integrity_sha256(dataset_payload)
        input_seal_sha256 = _integrity_sha256(input_seal)
        aggregate_sha256 = _integrity_sha256(aggregate)
        if (
            str(cohort.get("cohort_version") or "")
            != CANDIDATE_EXPERIMENT_COHORT_VERSION
            or str(cohort.get("authorization_id") or "")
            != str(authorization_row.get("id") or "")
            or str(cohort.get("artifact_id") or "")
            != str(authorization_row.get("artifact_id") or "")
            or _integrity_int(cohort.get("artifact_version") or 0)
            != _integrity_int(authorization_row.get("artifact_version") or 0)
            or str(cohort.get("client_request_id") or "")
            != str(authorization_row.get("client_request_id") or "")
            or str(input_row.get("seal_version") or "")
            != CANDIDATE_EXPERIMENT_INPUT_SEAL_VERSION
            or str(input_seal.get("version") or "")
            != CANDIDATE_EXPERIMENT_INPUT_SEAL_VERSION
        ):
            add_issue(
                "CANDIDATE_EXPERIMENT_COHORT_MIRROR_MISMATCH",
                "联合实验 cohort 或输入封印镜像字段不一致。",
            )
        if spec_sha256 != str(input_row.get("spec_sha256") or ""):
            add_issue(
                "CANDIDATE_EXPERIMENT_SPEC_HASH_MISMATCH",
                "联合实验共同规格哈希不一致。",
            )
        if dataset_seal_sha256 != str(
            input_row.get("dataset_seal_sha256") or ""
        ):
            add_issue(
                "CANDIDATE_EXPERIMENT_DATASET_HASH_MISMATCH",
                "联合实验共同数据封印哈希不一致。",
            )
        if input_seal_sha256 != str(input_row.get("input_seal_sha256") or ""):
            add_issue(
                "CANDIDATE_EXPERIMENT_INPUT_SEAL_HASH_MISMATCH",
                "联合实验输入封印哈希不一致。",
            )
        if aggregate_sha256 != str(cohort.get("aggregate_sha256") or ""):
            add_issue(
                "CANDIDATE_EXPERIMENT_AGGREGATE_HASH_MISMATCH",
                "联合实验聚合哈希不一致。",
            )
        dataset_seal = (
            dataset_payload.get("seal")
            if isinstance(dataset_payload.get("seal"), dict)
            else {}
        )
        histories = dataset_payload.get("histories")
        try:
            expected_dataset_payload, expected_dataset_seal = self._dataset_payload(
                histories
            )
        except (
            CandidateExperimentError,
            TypeError,
            ValueError,
            KeyError,
            AttributeError,
            OverflowError,
        ):
            expected_dataset_payload, expected_dataset_seal = {}, {}
            add_issue(
                "CANDIDATE_EXPERIMENT_DATASET_INVALID",
                "联合实验共同数据无法通过重算校验。",
            )
        if (
            expected_dataset_payload
            and (
                expected_dataset_payload != dataset_payload
                or expected_dataset_seal != dataset_seal
            )
        ):
            add_issue(
                "CANDIDATE_EXPERIMENT_DATASET_SEAL_MISMATCH",
                "联合实验共同数据内容与封印不一致。",
            )
        if historical_context and dataset_seal:
            try:
                expected_spec = self._common_spec(
                    historical_context,
                    dataset_seal,
                )
                expected_spec["dataset_content_sha256"] = str(
                    dataset_seal["dataset_content_sha256"]
                )
                expected_spec["dataset_seal_sha256"] = dataset_seal_sha256
            except (
                CandidateExperimentError,
                KeyError,
                TypeError,
                ValueError,
                AttributeError,
                OverflowError,
            ):
                expected_spec = {}
            if expected_spec != spec:
                add_issue(
                    "CANDIDATE_EXPERIMENT_SPEC_CONTENT_MISMATCH",
                    "联合实验共同规格不是服务端规范结果。",
                )

        expected_input_seal = {
            "version": CANDIDATE_EXPERIMENT_INPUT_SEAL_VERSION,
            "cohort_id": cohort_id,
            "authorization_sha256": supplied_authorization_sha256,
            "authorization_binding_sha256": str(
                authorization.get("authorization_binding_sha256") or ""
            ),
            "request_semantics_sha256": request_semantics_sha256,
            "artifact_snapshot_sha256": str(
                authorization.get("artifact_snapshot_sha256") or ""
            ),
            "governance_attestation_sha256": str(
                authorization.get(
                    "expected_governance_attestation_sha256"
                ) or ""
            ),
            "candidate_binding_sha256s": [
                str(candidate.get("candidate_binding_sha256") or "")
                for candidate in historical_context.get("candidates", [])
            ],
            "spec_sha256": spec_sha256,
            "dataset_seal_sha256": dataset_seal_sha256,
            **_safety_fields(),
        }
        if input_seal != expected_input_seal:
            add_issue(
                "CANDIDATE_EXPERIMENT_INPUT_SEAL_CONTENT_MISMATCH",
                "联合实验输入封印内容不一致。",
            )

        expected_arm_count = _integrity_int(cohort.get("arm_count") or 0)
        if (
            expected_arm_count != len(arm_rows)
            or not MIN_EXPERIMENT_ARMS <= len(arm_rows) <= MAX_EXPERIMENT_ARMS
            or [_integrity_int(row["sequence_no"]) for row in arm_rows]
            != list(range(1, len(arm_rows) + 1))
            or len({str(row["candidate_id"]) for row in arm_rows})
            != len(arm_rows)
        ):
            add_issue(
                "CANDIDATE_EXPERIMENT_ARM_SET_INVALID",
                "联合实验 arm 数量、顺序或候选唯一性无效。",
            )
        public_arms: list[dict[str, Any]] = []
        ordered_arm_sha256s: list[str] = []
        try:
            frozen_histories = (
                _freeze(_json_clone(histories))
                if isinstance(histories, dict)
                else {}
            )
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
            frozen_histories = {}
            add_issue(
                "CANDIDATE_EXPERIMENT_DATASET_INVALID",
                "联合实验共同数据无法安全反序列化。",
            )
        context_candidates_value = historical_context.get("candidates", [])
        context_candidates = (
            context_candidates_value
            if isinstance(context_candidates_value, list)
            else []
        )
        for index, raw_row in enumerate(arm_rows):
            row = dict(raw_row)
            arm_record = _json_object(row.get("arm_json"))
            public = (
                copy.deepcopy(arm_record.get("public"))
                if isinstance(arm_record.get("public"), dict)
                else {}
            )
            seal = (
                copy.deepcopy(arm_record.get("seal"))
                if isinstance(arm_record.get("seal"), dict)
                else {}
            )
            plan = _json_object(row.get("plan_json"))
            result = _json_object(row.get("result_json"))
            plan_sha256 = _integrity_sha256(plan)
            result_sha256 = _integrity_sha256(result)
            candidate = (
                context_candidates[index]
                if index < len(context_candidates)
                and isinstance(context_candidates[index], dict)
                else {}
            )
            if (
                str(row.get("arm_version") or "")
                != CANDIDATE_EXPERIMENT_ARM_VERSION
                or str(public.get("version") or "")
                != CANDIDATE_EXPERIMENT_ARM_VERSION
                or str(seal.get("version") or "")
                != CANDIDATE_EXPERIMENT_ARM_VERSION
                or str(public.get("id") or "") != str(row.get("id") or "")
                or str(seal.get("id") or "") != str(row.get("id") or "")
                or _integrity_int(public.get("sequence_no") or 0) != index + 1
                or _integrity_int(seal.get("sequence_no") or 0) != index + 1
                or _integrity_int(row.get("sequence_no") or 0) != index + 1
            ):
                add_issue(
                    "CANDIDATE_EXPERIMENT_ARM_MIRROR_MISMATCH",
                    "联合实验 arm 版本、身份或顺序镜像不一致。",
                )
            if (
                str(row.get("candidate_id") or "")
                != str(candidate.get("candidate_id") or "")
                or _integrity_int(row.get("candidate_revision") or 0)
                != _integrity_int(candidate.get("candidate_revision") or 0)
                or str(row.get("candidate_origin_message_id") or "")
                != str(candidate.get("candidate_origin_message_id") or "")
                or str(row.get("candidate_latest_message_id") or "")
                != str(candidate.get("candidate_latest_message_id") or "")
                or str(row.get("candidate_snapshot_sha256") or "")
                != str(candidate.get("candidate_snapshot_sha256") or "")
                or str(row.get("candidate_binding_sha256") or "")
                != str(candidate.get("candidate_binding_sha256") or "")
            ):
                add_issue(
                    "CANDIDATE_EXPERIMENT_ARM_CANDIDATE_MISMATCH",
                    "联合实验 arm 候选绑定不一致。",
                )
            if (
                plan_sha256 != str(row.get("plan_sha256") or "")
                or result_sha256 != str(row.get("result_sha256") or "")
            ):
                add_issue(
                    "CANDIDATE_EXPERIMENT_ARM_INPUT_RESULT_HASH_MISMATCH",
                    "联合实验 arm 计划或结果哈希不一致。",
                )
            if candidate and spec:
                try:
                    expected_plan = self._arm_plan(
                        candidate,
                        spec,
                        arm_id=str(row.get("id") or ""),
                        created_at=_integrity_int(
                            authorization.get("created_at") or 0
                        ),
                    )
                except Exception:
                    expected_plan = {}
                if expected_plan != plan:
                    add_issue(
                        "CANDIDATE_EXPERIMENT_ARM_PLAN_MISMATCH",
                        "联合实验 arm 计划不是服务端共同规格映射。",
                    )
            recomputed_result: dict[str, Any] = {}
            if frozen_histories and plan and spec.get("engine_config"):
                try:
                    recomputed_result = self.engine_runner(
                        frozen_histories,
                        plan,
                        spec["engine_config"],
                    )
                except Exception:
                    recomputed_result = {}
                if recomputed_result != result:
                    add_issue(
                        "CANDIDATE_EXPERIMENT_ARM_RESULT_RECOMPUTE_MISMATCH",
                        "联合实验 arm 结果重算不一致。",
                    )
            expected_public = {}
            if candidate and result:
                try:
                    scenarios = _project_scenarios(result)
                except CandidateExperimentError:
                    scenarios = []
                expected_public = {
                    "id": str(row.get("id") or ""),
                    "version": CANDIDATE_EXPERIMENT_ARM_VERSION,
                    "sequence_no": index + 1,
                    "candidate_id": candidate["candidate_id"],
                    "candidate_revision": candidate["candidate_revision"],
                    "candidate_origin_message_id": candidate[
                        "candidate_origin_message_id"
                    ],
                    "candidate_latest_message_id": candidate[
                        "candidate_latest_message_id"
                    ],
                    "candidate_snapshot_sha256": candidate[
                        "candidate_snapshot_sha256"
                    ],
                    "candidate_binding_sha256": candidate[
                        "candidate_binding_sha256"
                    ],
                    "title": candidate["title"],
                    "symbol": candidate["symbol"],
                    "direction": candidate["direction"],
                    "side": candidate["side"],
                    "horizon_days": candidate["horizon_days"],
                    "paper_weight_pct": SERVER_PAPER_WEIGHT_PCT,
                    "thesis": candidate["thesis"],
                    "invalidation": candidate["invalidation"],
                    "evidence": copy.deepcopy(candidate["evidence"]),
                    "counterevidence": copy.deepcopy(candidate["counterevidence"]),
                    "shared_spec_sha256": spec_sha256,
                    "shared_dataset_seal_sha256": dataset_seal_sha256,
                    "plan_sha256": plan_sha256,
                    "result_sha256": result_sha256,
                    "scenarios": scenarios,
                    "metrics_visible": any(
                        scenario.get("metrics_visible") is True
                        for scenario in scenarios
                    ),
                    "integrity_ok": True,
                    **_safety_fields(),
                }
                expected_seal = {
                    "version": CANDIDATE_EXPERIMENT_ARM_VERSION,
                    "id": str(row.get("id") or ""),
                    "sequence_no": index + 1,
                    "candidate_binding_sha256": candidate[
                        "candidate_binding_sha256"
                    ],
                    "candidate_snapshot_sha256": candidate[
                        "candidate_snapshot_sha256"
                    ],
                    "spec_sha256": spec_sha256,
                    "dataset_seal_sha256": dataset_seal_sha256,
                    "plan_sha256": plan_sha256,
                    "result_sha256": result_sha256,
                    "public_projection_sha256": _integrity_sha256(
                        expected_public
                    ),
                    **_safety_fields(),
                }
                expected_public["arm_sha256"] = _integrity_sha256(expected_seal)
                if public != expected_public or seal != expected_seal:
                    add_issue(
                        "CANDIDATE_EXPERIMENT_ARM_SEAL_MISMATCH",
                        "联合实验 arm 公共投影或封印不一致。",
                    )
            arm_sha256 = _integrity_sha256(seal)
            if arm_sha256 != str(row.get("arm_sha256") or ""):
                add_issue(
                    "CANDIDATE_EXPERIMENT_ARM_HASH_MISMATCH",
                    "联合实验 arm 聚合哈希不一致。",
                )
            ordered_arm_sha256s.append(str(row.get("arm_sha256") or ""))
            public_arms.append(public)

        expected_aggregate = {
            "version": CANDIDATE_EXPERIMENT_AGGREGATE_VERSION,
            "cohort_id": cohort_id,
            "authorization_sha256": supplied_authorization_sha256,
            "input_seal_sha256": input_seal_sha256,
            "spec_sha256": spec_sha256,
            "dataset_seal_sha256": dataset_seal_sha256,
            "arm_count": len(arm_rows),
            "ordered_arm_ids": [str(row["id"]) for row in arm_rows],
            "ordered_arm_sha256s": ordered_arm_sha256s,
            "ranking_produced": False,
            "winner_claim": False,
            **_safety_fields(),
        }
        if aggregate != expected_aggregate:
            add_issue(
                "CANDIDATE_EXPERIMENT_AGGREGATE_CONTENT_MISMATCH",
                "联合实验 cohort 聚合内容不一致。",
            )
        safety_columns_ok = bool(
            cohort.get("execution_capability") == "none"
            and _integrity_int(cohort.get("live_trading_allowed") or 0) == 0
            and _integrity_int(cohort.get("can_autonomously_decide") or 0) == 0
            and _integrity_int(cohort.get("ranking_produced") or 0) == 0
            and _integrity_int(cohort.get("winner_claim") or 0) == 0
            and _integrity_int(
                cohort.get("user_final_decision_required") or 0
            ) == 1
            and _integrity_int(cohort.get("market_data_reads") or 0) == 1
            and _integrity_int(cohort.get("provider_calls_total") or 0) == 0
            and _integrity_int(cohort.get("openai_calls") or 0) == 0
        )
        if not safety_columns_ok:
            add_issue(
                "CANDIDATE_EXPERIMENT_SAFETY_BOUNDARY_INVALID",
                "联合实验无执行、无自动决定边界不一致。",
            )
        integrity_ok = not issues
        if not integrity_ok:
            public_arms = [
                _redacted_arm(
                    dict(row),
                    (
                        context_candidates[index]
                        if index < len(context_candidates)
                        and isinstance(context_candidates[index], Mapping)
                        else None
                    ),
                    index + 1,
                )
                for index, row in enumerate(arm_rows)
            ]
        return {
            "version": CANDIDATE_EXPERIMENT_COHORT_VERSION,
            "id": cohort_id,
            "room_id": room_id,
            "artifact_id": str(cohort.get("artifact_id") or ""),
            "artifact_version": _integrity_int(
                cohort.get("artifact_version") or 0
            ),
            "client_request_id": str(cohort.get("client_request_id") or ""),
            "status": "ready" if integrity_ok else "integrity_failed",
            "integrity_ok": integrity_ok,
            "metrics_visible": integrity_ok,
            "integrity_issues": issues,
            "source_current": source_current,
            "source_invalidation_conditions": (
                [] if source_current else ["artifact_version_changed_after_commit"]
            ),
            "authorization": (
                self._public_authorization(authorization)
                if integrity_ok
                else {}
            ),
            "common_spec": spec if integrity_ok else {},
            "dataset_seal": dataset_seal if integrity_ok else {},
            "spec_sha256": spec_sha256 if integrity_ok else "",
            "dataset_seal_sha256": (
                dataset_seal_sha256 if integrity_ok else ""
            ),
            "input_seal_sha256": input_seal_sha256 if integrity_ok else "",
            "aggregate_sha256": aggregate_sha256 if integrity_ok else "",
            "request_semantics_sha256": (
                request_semantics_sha256 if integrity_ok else ""
            ),
            "arms": public_arms,
            "market_data_reads": 1,
            "provider_calls_total": 0,
            "openai_calls": 0,
            "historical_only": True,
            "out_of_sample_claim": False,
            "future_performance_claim": False,
            "created_at": sealed_created_at if integrity_ok else 0,
            **_safety_fields(),
        }


__all__ = [
    "CANDIDATE_EXPERIMENT_AUTHORIZATION_VERSION",
    "CANDIDATE_EXPERIMENT_COHORT_VERSION",
    "CANDIDATE_EXPERIMENT_REQUEST_VERSION",
    "CandidateExperimentError",
    "CandidateExperimentService",
    "candidate_experiment_request_semantics",
    "normalize_candidate_experiment_request",
]
