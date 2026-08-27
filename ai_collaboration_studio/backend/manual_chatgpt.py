from __future__ import annotations

import copy
import json
import math
import os
import re
import secrets
import time
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from urllib.parse import urlsplit

from .decision_lineage import canonical_sha256
from .provider_call_ledger import ProviderCallLedger
from .providers.base import (
    classify_provider_exception,
    normalize_provider_error_code,
)
from .store import StudioStore, now_ms


MANUAL_CHATGPT_BUNDLE_VERSION = "compact_room_bundle_v2"
MANUAL_CHATGPT_RESULT_VERSION = "manual_chatgpt_result_v1"
MANUAL_CHATGPT_SESSION_VERSION = "manual_chatgpt_session_v1"
MANUAL_CHATGPT_EVENT_VERSION = "manual_chatgpt_event_v1"
LEGACY_MANUAL_CHATGPT_IMPORT_CONTRACT_VERSION = "manual_chatgpt_import_contract_v1"
MANUAL_CHATGPT_IMPORT_CONTRACT_VERSION = "manual_chatgpt_import_contract_v2"
SUPPORTED_MANUAL_CHATGPT_IMPORT_CONTRACT_VERSIONS = frozenset({
    LEGACY_MANUAL_CHATGPT_IMPORT_CONTRACT_VERSION,
    MANUAL_CHATGPT_IMPORT_CONTRACT_VERSION,
})
MANUAL_CHATGPT_PLANNING_VERSION = "manual_chatgpt_planning_v1"
MANUAL_CHATGPT_TOKEN_ESTIMATE_VERSION = "cjk_one_ascii_four_v1"
MANUAL_CHATGPT_API_REVIEW_VERSION = "manual_chatgpt_api_review_v1"
MANUAL_CHATGPT_API_REVIEW_RECORD_VERSION = "manual_chatgpt_api_review_record_v1"
MANUAL_CHATGPT_REVIEW_PLAN_VERSION = "manual_chatgpt_review_plan_v1"
MANUAL_CHATGPT_DECISION_CARD_VERSION = "manual_chatgpt_decision_card_v1"
MANUAL_CHATGPT_CONFIRMATION_VERSION = "manual_chatgpt_confirmation_v1"
MANUAL_CHATGPT_FREEZE_ACKNOWLEDGEMENT = "RESEARCH_ONLY_USER_DECISION"
MANUAL_CHATGPT_REVIEW_RECOVERY_VERSION = "manual_chatgpt_review_recovery_v1"
MANUAL_CHATGPT_REVIEW_RECOVERY_ACKNOWLEDGEMENT = (
    "REAUTHORIZE_ORPHANED_ZERO_CALL_REVIEW"
)
MANUAL_CHATGPT_REVIEW_ORPHAN_AGE_MS = 5 * 60 * 1000

MODE_REVIEW_KINDS: dict[str, list[str]] = {
    "quick": ["fact_check", "risk_review"],
    "standard": ["fact_check", "counterargument", "risk_review"],
    "deep": ["fact_check", "counterargument", "risk_review", "evidence_audit"],
}

MANUAL_CHATGPT_STATES = frozenset({
    "DRAFT",
    "BUNDLE_READY",
    "WAITING_FOR_CHATGPT",
    "RESULT_IMPORTED",
    "VALIDATING",
    "API_REVIEW",
    "READY_FOR_DECISION",
    "FROZEN",
    "CONTEXT_STALE",
    "IMPORT_REJECTED",
    "BUDGET_BLOCKED",
    "NEEDS_USER_ACTION",
})

MODE_PRESETS: dict[str, dict[str, Any]] = {
    "quick": {
        "label": "快速",
        "chatgpt_panels": 1,
        "api_reviews": 2,
        "api_review_output_token_budget": 900,
        "panel_kinds": ["synthesis"],
    },
    "standard": {
        "label": "标准",
        "chatgpt_panels": 2,
        "api_reviews": 3,
        "api_review_output_token_budget": 1_400,
        "panel_kinds": ["synthesis", "counterargument"],
    },
    "deep": {
        "label": "深度",
        "chatgpt_panels": 3,
        "api_reviews": 4,
        "api_review_output_token_budget": 2_000,
        "panel_kinds": ["synthesis", "counterargument", "stress_test"],
    },
}

INDEPENDENCE_CLASSIFICATIONS = frozenset({
    "same_answer_multi_role_views",
    "same_model_independent_call",
    "different_provider_independent_opinion",
})

MAX_IMPORT_CHARS = 200_000
MAX_OBJECTIVE_CHARS = 4_000
MAX_EVIDENCE_ITEMS = 40
MAX_CANDIDATE_GROUPS = 12
MAX_HISTORY_ITEMS = 20
MAX_ROLE_COUNT = 24
MAX_RATE_USD_PER_MILLION_TOKENS = Decimal("10000")


@dataclass(frozen=True)
class ImportIssue:
    path: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message}


class ManualChatGPTError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "MANUAL_CHATGPT_INVALID",
        status: int = 400,
        issues: Sequence[ImportIssue] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.issues = list(issues or [])


def _bounded_text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _json_object(value: Any) -> dict[str, Any]:
    return copy.deepcopy(value) if isinstance(value, Mapping) else {}


def _json_list(value: Any) -> list[Any]:
    return copy.deepcopy(list(value)) if isinstance(value, list) else []


def _safe_http_url(value: Any) -> str:
    candidate = _bounded_text(value, 1_000)
    if not candidate:
        return ""
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    return candidate


def mode_preset(mode: Any) -> dict[str, Any]:
    clean = str(mode or "standard").strip().lower()
    if clean not in MODE_PRESETS:
        raise ManualChatGPTError(
            "协作模式必须是 quick、standard 或 deep。",
            code="MANUAL_CHATGPT_MODE_INVALID",
        )
    return copy.deepcopy(MODE_PRESETS[clean] | {"id": clean})


def estimate_text_tokens(value: Any) -> int:
    """Return a deterministic planning estimate without claiming tokenizer parity."""

    content = str(value or "")
    if not content:
        return 0
    ascii_characters = sum(1 for character in content if ord(character) < 128)
    non_ascii_characters = len(content) - ascii_characters
    return max(1, math.ceil(ascii_characters / 4) + non_ascii_characters)


def api_review_rate_card_from_environment() -> dict[str, Any]:
    """Read optional, non-secret pricing assumptions without embedding live prices."""

    label = _bounded_text(
        os.getenv("AI_STUDIO_MANUAL_CHATGPT_REVIEW_RATE_LABEL"),
        120,
    )
    input_rate = _bounded_text(
        os.getenv("AI_STUDIO_MANUAL_CHATGPT_REVIEW_INPUT_USD_PER_MILLION"),
        40,
    )
    output_rate = _bounded_text(
        os.getenv("AI_STUDIO_MANUAL_CHATGPT_REVIEW_OUTPUT_USD_PER_MILLION"),
        40,
    )
    if not label and not input_rate and not output_rate:
        return {}
    return {
        "label": label,
        "input_usd_per_million_tokens": input_rate,
        "output_usd_per_million_tokens": output_rate,
    }


def _rate_decimal(value: Any) -> Decimal | None:
    try:
        rate = Decimal(str(value or "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not rate.is_finite() or rate < 0 or rate > MAX_RATE_USD_PER_MILLION_TOKENS:
        return None
    return rate


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def build_planning_projection(
    context: Mapping[str, Any],
    preset: Mapping[str, Any],
    *,
    review_rate_card: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    canonical_context = json.dumps(
        dict(context),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    context_characters = len(canonical_context)
    context_utf8_bytes = len(canonical_context.encode("utf-8"))
    context_tokens = estimate_text_tokens(canonical_context)
    review_calls = int(preset.get("api_reviews") or 0)
    output_tokens_per_review = int(
        preset.get("api_review_output_token_budget") or 0
    )
    estimated_review_input_tokens = context_tokens * review_calls
    review_output_token_budget = output_tokens_per_review * review_calls
    supplied_rate_card = dict(review_rate_card or {})
    rate_label = _bounded_text(supplied_rate_card.get("label"), 120)
    raw_input_rate = supplied_rate_card.get("input_usd_per_million_tokens")
    raw_output_rate = supplied_rate_card.get("output_usd_per_million_tokens")
    input_rate = _rate_decimal(raw_input_rate)
    output_rate = _rate_decimal(raw_output_rate)

    has_rate_values = any(
        str(value or "").strip()
        for value in (rate_label, raw_input_rate, raw_output_rate)
    )
    if not has_rate_values:
        cost_estimate = {
            "status": "unavailable",
            "currency": "USD",
            "amount_usd": None,
            "reason_code": "RATE_CARD_NOT_CONFIGURED",
            "rate_card_label": "",
            "scope": "independent_api_reviews_only",
            "manual_chatgpt_cost_included": False,
            "not_a_bill": True,
        }
    elif not rate_label or input_rate is None or output_rate is None:
        cost_estimate = {
            "status": "invalid_configuration",
            "currency": "USD",
            "amount_usd": None,
            "reason_code": "RATE_CARD_INVALID",
            "rate_card_label": rate_label,
            "scope": "independent_api_reviews_only",
            "manual_chatgpt_cost_included": False,
            "not_a_bill": True,
        }
    else:
        amount = (
            Decimal(estimated_review_input_tokens) * input_rate
            + Decimal(review_output_token_budget) * output_rate
        ) / Decimal(1_000_000)
        amount = amount.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        cost_estimate = {
            "status": "estimated",
            "currency": "USD",
            "amount_usd": format(amount, ".6f"),
            "reason_code": "",
            "rate_card_label": rate_label,
            "input_usd_per_million_tokens": _decimal_text(input_rate),
            "output_usd_per_million_tokens": _decimal_text(output_rate),
            "scope": "independent_api_reviews_only",
            "manual_chatgpt_cost_included": False,
            "not_a_bill": True,
        }

    return {
        "version": MANUAL_CHATGPT_PLANNING_VERSION,
        "context_size": {
            "serialization": "canonical_json_utf8",
            "characters": context_characters,
            "utf8_bytes": context_utf8_bytes,
            "estimated_tokens": context_tokens,
            "token_estimation_method": MANUAL_CHATGPT_TOKEN_ESTIMATE_VERSION,
        },
        "workload": {
            "chatgpt_panel_calls": int(preset.get("chatgpt_panels") or 0),
            "independent_api_review_calls": review_calls,
            "estimated_api_review_input_tokens": estimated_review_input_tokens,
            "api_review_output_token_budget": review_output_token_budget,
        },
        "estimated_api_cost": cost_estimate,
    }


def _role_projection(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    roles: list[dict[str, Any]] = []
    members = snapshot.get("members") if isinstance(snapshot.get("members"), list) else []
    enabled = [
        member
        for member in members
        if isinstance(member, Mapping)
        and member.get("enabled") is True
        and not member.get("archived")
    ]
    enabled.sort(key=lambda item: (int(item.get("position") or 0), str(item.get("id") or "")))
    for member in enabled[:MAX_ROLE_COUNT]:
        role_id = _bounded_text(member.get("id"), 80)
        if not role_id:
            continue
        roles.append({
            "role_id": role_id,
            "name": _bounded_text(member.get("name"), 120),
            "identity": _bounded_text(member.get("identity"), 600),
            "responsibilities": _bounded_text(member.get("responsibilities"), 800),
            "boundaries": _bounded_text(member.get("boundaries"), 800),
            "stance": _bounded_text(member.get("stance"), 80),
        })
    if not roles:
        raise ManualChatGPTError(
            "当前房间没有可冻结的启用角色。",
            code="MANUAL_CHATGPT_ROSTER_EMPTY",
            status=409,
        )
    return roles


def _evidence_projection(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    materials = snapshot.get("materials") if isinstance(snapshot.get("materials"), list) else []
    for material in materials[:MAX_EVIDENCE_ITEMS]:
        if not isinstance(material, Mapping) or material.get("active") is not True:
            continue
        evidence_id = _bounded_text(material.get("id"), 80)
        if not evidence_id:
            continue
        metadata = material.get("metadata") if isinstance(material.get("metadata"), Mapping) else {}
        url = _safe_http_url(
            material.get("source_url")
            or metadata.get("source_url")
            or metadata.get("url")
        )
        evidence.append({
            "evidence_id": evidence_id,
            "version": max(1, int(material.get("version") or 1)),
            "title": _bounded_text(material.get("title"), 240),
            "kind": _bounded_text(material.get("kind"), 80),
            "source_url": url,
            "source_snapshot_sha256": _bounded_text(
                material.get("source_snapshot_sha256"), 64
            ).lower(),
            "excerpt": _bounded_text(material.get("content"), 1_600),
        })
    return evidence


def _candidate_projection(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    artifacts = snapshot.get("artifacts") if isinstance(snapshot.get("artifacts"), list) else []
    for artifact in artifacts:
        if len(groups) >= MAX_CANDIDATE_GROUPS or not isinstance(artifact, Mapping):
            break
        content = artifact.get("content") if isinstance(artifact.get("content"), Mapping) else {}
        decision = content.get("decision") if isinstance(content.get("decision"), Mapping) else {}
        raw_options = decision.get("options") if isinstance(decision.get("options"), list) else []
        options: list[dict[str, Any]] = []
        for option in raw_options[:8]:
            if not isinstance(option, Mapping):
                continue
            option_id = _bounded_text(option.get("id"), 80)
            title = _bounded_text(option.get("title"), 180)
            description = _bounded_text(option.get("description"), 1_200)
            if not option_id or not title or not description:
                continue
            options.append({
                "option_id": option_id,
                "title": title,
                "description": description,
                "benefits": [
                    _bounded_text(item, 300)
                    for item in _json_list(option.get("benefits"))[:6]
                    if _bounded_text(item, 300)
                ],
                "risks": [
                    _bounded_text(item, 300)
                    for item in _json_list(option.get("risks"))[:6]
                    if _bounded_text(item, 300)
                ],
            })
        if not options:
            continue
        groups.append({
            "artifact_id": _bounded_text(artifact.get("id"), 80),
            "artifact_version": max(1, int(artifact.get("version") or 1)),
            "artifact_status": _bounded_text(artifact.get("status"), 20).upper(),
            "title": _bounded_text(artifact.get("title"), 240),
            "preferred_option_id": _bounded_text(decision.get("preferred_option_id"), 80),
            "options": options,
        })
    return groups


def _history_projection(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    packages = snapshot.get("decision_packages")
    for package in packages if isinstance(packages, list) else []:
        if len(history) >= MAX_HISTORY_ITEMS or not isinstance(package, Mapping):
            break
        anchor = package.get("anchor") if isinstance(package.get("anchor"), Mapping) else {}
        history.append({
            "record_type": "user_decision",
            "record_id": _bounded_text(package.get("package_id"), 80),
            "state": _bounded_text(package.get("state"), 40),
            "selected_option_id": _bounded_text(
                anchor.get("selected_option_id")
                or _json_object(anchor.get("selected_option")).get("id"),
                80,
            ),
            "record_sha256": _bounded_text(package.get("manifest_sha256"), 64).lower(),
        })
    decisions = snapshot.get("director_decisions")
    for decision in decisions if isinstance(decisions, list) else []:
        if len(history) >= MAX_HISTORY_ITEMS or not isinstance(decision, Mapping):
            break
        history.append({
            "record_type": "director_decision",
            "record_id": _bounded_text(decision.get("id"), 80),
            "action": _bounded_text(decision.get("action"), 40),
            "stage": _bounded_text(decision.get("stage"), 40),
            "reason": _bounded_text(decision.get("reason"), 600),
        })
    return history


def compact_context(
    snapshot: Mapping[str, Any],
    *,
    objective: Any,
    mode: Any,
) -> dict[str, Any]:
    room = snapshot.get("room") if isinstance(snapshot.get("room"), Mapping) else {}
    room_id = _bounded_text(room.get("id"), 80)
    clean_objective = _bounded_text(objective, MAX_OBJECTIVE_CHARS)
    if not room_id:
        raise ManualChatGPTError(
            "房间快照缺少身份。",
            code="MANUAL_CHATGPT_ROOM_INVALID",
            status=409,
        )
    if not clean_objective:
        raise ManualChatGPTError(
            "研究问题不能为空。",
            code="MANUAL_CHATGPT_OBJECTIVE_EMPTY",
        )
    preset = mode_preset(mode)
    return {
        "room": {
            "room_id": room_id,
            "title": _bounded_text(room.get("title"), 240),
            "domain": _bounded_text(room.get("domain"), 80),
            "category": _bounded_text(room.get("category"), 80),
            "settings_version": max(1, int(room.get("settings_version") or 1)),
        },
        "objective": clean_objective,
        "mode": preset["id"],
        "roles": _role_projection(snapshot),
        "candidate_matrix": _candidate_projection(snapshot),
        "historical_decisions": _history_projection(snapshot),
        "evidence_index": _evidence_projection(snapshot),
        "safety": {
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
            "user_final_decision_required": True,
        },
    }


def build_compact_bundle(
    snapshot: Mapping[str, Any],
    *,
    objective: Any,
    mode: Any,
    session_id: str,
    round_id: str,
    created_at: int,
    review_rate_card: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    context = compact_context(snapshot, objective=objective, mode=mode)
    preset = mode_preset(mode)
    bundle: dict[str, Any] = {
        "version": MANUAL_CHATGPT_BUNDLE_VERSION,
        "session_id": session_id,
        "room_id": context["room"]["room_id"],
        "round_id": round_id,
        "created_at": int(created_at),
        "context_sha256": canonical_sha256(context),
        "budget": {
            "chatgpt_panel_calls": preset["chatgpt_panels"],
            "independent_api_reviews": preset["api_reviews"],
            "api_review_output_token_budget_per_call": preset[
                "api_review_output_token_budget"
            ],
            "panel_kinds": preset["panel_kinds"],
        },
        "planning": build_planning_projection(
            context,
            preset,
            review_rate_card=review_rate_card,
        ),
        "context": context,
        "independence_notice": (
            "同一回答中的多角色视角不是多条独立模型意见；独立性必须按实际调用层级标注。"
        ),
        "import_contract_version": MANUAL_CHATGPT_IMPORT_CONTRACT_VERSION,
    }
    bundle["bundle_sha256"] = canonical_sha256(bundle)
    return bundle


def _bundle_import_contract_version(bundle: Mapping[str, Any]) -> str:
    version = _bounded_text(bundle.get("import_contract_version"), 80)
    if version not in SUPPORTED_MANUAL_CHATGPT_IMPORT_CONTRACT_VERSIONS:
        raise ManualChatGPTError(
            "冻结任务包的导入契约版本不可识别。",
            code="MANUAL_CHATGPT_IMPORT_CONTRACT_UNSUPPORTED",
            status=409,
        )
    return version


def _schema_template(bundle: Mapping[str, Any]) -> dict[str, Any]:
    roles = _json_list(_json_object(bundle.get("context")).get("roles"))
    role_views = [{
        "role_id": _bounded_text(role.get("role_id"), 80),
        "assessment": "",
        "evidence_refs": [],
        "uncertainty": "",
    } for role in roles if isinstance(role, Mapping)]
    panels = []
    budget = _json_object(bundle.get("budget"))
    panel_kinds = _json_list(budget.get("panel_kinds"))
    panel_calls = int(budget.get("chatgpt_panel_calls") or 0)
    contract_version = _bundle_import_contract_version(bundle)
    default_panel_independence = (
        "same_model_independent_call"
        if (
            contract_version == MANUAL_CHATGPT_IMPORT_CONTRACT_VERSION
            and panel_calls > 1
        )
        else "same_answer_multi_role_views"
    )
    for index, panel_kind in enumerate(panel_kinds, start=1):
        panels.append({
            "panel_id": f"panel_{index}",
            "panel_kind": panel_kind,
            "call_index": index,
            "declared_independence": default_panel_independence,
            "summary": "",
            "conclusion": "",
            "disagreements": [],
            "risks": [],
            "evidence_refs": [],
            "role_views": copy.deepcopy(role_views),
        })
    return {
        "version": MANUAL_CHATGPT_RESULT_VERSION,
        "room_id": _bounded_text(bundle.get("room_id"), 80),
        "round_id": _bounded_text(bundle.get("round_id"), 80),
        "bundle_sha256": _bounded_text(bundle.get("bundle_sha256"), 64),
        "context_sha256": _bounded_text(bundle.get("context_sha256"), 64),
        "declared_model": "",
        "panels": panels,
        "final_synthesis": {
            "summary": "",
            "decision_options": [{
                "option_id": "option_1",
                "title": "",
                "rationale": "",
                "evidence_refs": [],
                "risks": [],
            }],
            "recommended_option_id": "",
            "open_questions": [],
            "evidence_refs": [],
        },
    }


def import_contract(bundle: Mapping[str, Any]) -> dict[str, Any]:
    contract_version = _bundle_import_contract_version(bundle)
    return {
        "version": contract_version,
        "result_version": MANUAL_CHATGPT_RESULT_VERSION,
        "one_json_object_only": True,
        "markdown_fence_tolerated": True,
        "duplicate_keys_rejected": True,
        "nonfinite_numbers_rejected": True,
        "missing_conclusions_may_be_inferred": False,
        "declared_model_is_trusted": False,
        "allowed_independence_classifications": sorted(INDEPENDENCE_CLASSIFICATIONS),
        "required_panel_kinds": _json_list(
            _json_object(bundle.get("budget")).get("panel_kinds")
        ),
        "result_template": _schema_template(bundle),
    }


def task_prompt(bundle: Mapping[str, Any]) -> str:
    contract = import_contract(bundle)
    contract_version = contract["version"]
    budget = _json_object(bundle.get("budget"))
    panel_kinds = [
        _bounded_text(item, 80)
        for item in _json_list(budget.get("panel_kinds"))
        if _bounded_text(item, 80)
    ]
    panel_calls = int(budget.get("chatgpt_panel_calls") or 0)
    if panel_calls != len(panel_kinds) or panel_calls not in {1, 2, 3}:
        raise ManualChatGPTError(
            "冻结任务包的 ChatGPT 回合预算不一致。",
            code="MANUAL_CHATGPT_PANEL_BUDGET_INVALID",
            status=409,
        )
    turn_lines: list[str] = []
    if contract_version == MANUAL_CHATGPT_IMPORT_CONTRACT_VERSION:
        turn_lines.append(
            f"本任务需要在同一 ChatGPT 会话中完成恰好 {panel_calls} 次分别发送的回复，"
            "每次回复只完成一个 Panel；不要在第一条回复中一次性生成全部 Panel。"
        )
        for index, panel_kind in enumerate(panel_kinds, start=1):
            if index < panel_calls:
                turn_lines.append(
                    f"第 {index}/{panel_calls} 次回复：只完成 {panel_kind} Panel，"
                    f"不要输出最终 JSON；结尾提示用户回复“继续第 {index + 1}/{panel_calls} 个 Panel”。"
                )
            else:
                turn_lines.append(
                    f"第 {index}/{panel_calls} 次回复：完成 {panel_kind} Panel，"
                    "再把本会话此前各 Panel 合并为 IMPORT_CONTRACT 要求的唯一 JSON 对象；"
                    "本次回复只输出该 JSON 对象。"
                )
    else:
        turn_lines.append("按 required_panel_kinds 完成 Panel。")
        turn_lines.append("旧版 v1 导入契约不声明多回合 ChatGPT 协议。")
    return "\n".join([
        "你正在参与 AI 共创室的 ChatGPT 协作席位。",
        "请严格使用下方冻结任务包；不要声称执行交易、写数据库或替用户作最终决定。",
        *turn_lines,
        "按 required_panel_kinds 和 call_index 完成 Panel。每个 Panel 中的角色只是该次回答内的分析视角，",
        "不能把 12 个 role_views 计为 12 条独立模型意见。declared_model 只是用户声明元数据。",
        (
            "ChatGPT 回合数、declared_model 与 declared_independence 都是人工流程声明，"
            "导入本身不能证明真实模型来源或调用独立性。"
            if contract_version == MANUAL_CHATGPT_IMPORT_CONTRACT_VERSION
            else "declared_model 与 declared_independence 都是不可信的人工声明元数据。"
        ),
        "引用只能使用 evidence_index 中存在的 evidence_id。",
        "最终只输出一个 JSON 对象；允许用一个 Markdown json 代码围栏包裹，不要附加第二个对象。",
        "不得猜测或省略结论字段；不确定时写入 uncertainty/open_questions。",
        "",
        "COMPACT_ROOM_BUNDLE:",
        json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True),
        "",
        "IMPORT_CONTRACT:",
        json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True),
    ])


_FENCE_RE = re.compile(r"\A\s*```(?:json)?\s*(.*?)\s*```\s*\Z", re.IGNORECASE | re.DOTALL)
_JSONPATH_IDENTIFIER_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


class _JSONObjectPairs(list[tuple[str, Any]]):
    pass


class _DuplicateJSONKey(ValueError):
    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


class _NonFiniteJSONNumber(ValueError):
    def __init__(self, value: str) -> None:
        super().__init__(value)
        self.value = value


def _jsonpath_child(path: str, key: str) -> str:
    if _JSONPATH_IDENTIFIER_RE.fullmatch(key):
        return f"{path}.{key}"
    return f"{path}[{json.dumps(key, ensure_ascii=False)}]"


def _materialize_json_pairs(value: Any, path: str = "$") -> Any:
    if isinstance(value, _JSONObjectPairs):
        result: dict[str, Any] = {}
        for key, item in value:
            child_path = _jsonpath_child(path, key)
            if key in result:
                raise _DuplicateJSONKey(child_path)
            result[key] = _materialize_json_pairs(item, child_path)
        return result
    if isinstance(value, list):
        return [
            _materialize_json_pairs(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    return value


def _reject_nonfinite_json_number(value: str) -> None:
    raise _NonFiniteJSONNumber(value)


def parse_single_json_object(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise ManualChatGPTError(
            "导入内容必须是文本。",
            code="MANUAL_CHATGPT_IMPORT_TYPE_INVALID",
            issues=[ImportIssue("$", "TYPE", "必须提供 JSON 文本。")],
        )
    if len(raw) > MAX_IMPORT_CHARS:
        raise ManualChatGPTError(
            "导入内容超过 200000 字符上限。",
            code="MANUAL_CHATGPT_IMPORT_TOO_LARGE",
            status=413,
            issues=[ImportIssue("$", "MAX_LENGTH", "导入内容超过 200000 字符上限。")],
        )
    candidate = raw.strip()
    fenced = _FENCE_RE.fullmatch(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        pairs = json.loads(
            candidate,
            object_pairs_hook=_JSONObjectPairs,
            parse_constant=_reject_nonfinite_json_number,
        )
        value = _materialize_json_pairs(pairs)
    except _DuplicateJSONKey as exc:
        raise ManualChatGPTError(
            "导入 JSON 包含重复字段。",
            code="MANUAL_CHATGPT_IMPORT_DUPLICATE_KEY",
            issues=[ImportIssue(exc.path, "DUPLICATE_KEY", "同一路径只能出现一次。")],
        ) from exc
    except _NonFiniteJSONNumber as exc:
        raise ManualChatGPTError(
            "导入 JSON 包含非有限数字。",
            code="MANUAL_CHATGPT_IMPORT_NONFINITE_NUMBER",
            issues=[ImportIssue("$", "NONFINITE_NUMBER", f"{exc.value} 不是标准 JSON 数字。")],
        ) from exc
    except json.JSONDecodeError as exc:
        path = f"$[line={exc.lineno},column={exc.colno}]"
        raise ManualChatGPTError(
            "无法解析唯一 JSON 对象。",
            code="MANUAL_CHATGPT_IMPORT_JSON_INVALID",
            issues=[ImportIssue(path, "JSON_PARSE", exc.msg)],
        ) from exc
    except RecursionError as exc:
        raise ManualChatGPTError(
            "导入 JSON 嵌套过深。",
            code="MANUAL_CHATGPT_IMPORT_DEPTH_INVALID",
            issues=[ImportIssue("$", "MAX_DEPTH", "JSON 嵌套超过解析安全上限。")],
        ) from exc
    if not isinstance(value, dict):
        raise ManualChatGPTError(
            "导入根节点必须是一个 JSON 对象。",
            code="MANUAL_CHATGPT_IMPORT_ROOT_INVALID",
            issues=[ImportIssue("$", "TYPE", "根节点必须是 object。")],
        )
    return value


def _unknown_fields(
    value: Mapping[str, Any],
    allowed: set[str],
    path: str,
    issues: list[ImportIssue],
) -> None:
    for field in sorted(set(value) - allowed):
        issues.append(ImportIssue(f"{path}.{field}", "UNKNOWN_FIELD", "字段不在导入契约中。"))


def _required_text(
    value: Mapping[str, Any],
    field: str,
    path: str,
    issues: list[ImportIssue],
    *,
    maximum: int,
) -> str:
    raw = value.get(field)
    field_path = f"{path}.{field}"
    if not isinstance(raw, str):
        issues.append(ImportIssue(field_path, "TYPE", "必须是字符串。"))
        return ""
    clean = raw.strip()
    if not clean:
        issues.append(ImportIssue(field_path, "REQUIRED", "不能为空。"))
    elif len(clean) > maximum:
        issues.append(ImportIssue(field_path, "MAX_LENGTH", f"不能超过 {maximum} 字符。"))
    return clean


def _string_list(
    value: Any,
    path: str,
    issues: list[ImportIssue],
    *,
    allowed: set[str] | None = None,
    maximum_items: int = 40,
    maximum_text: int = 1_000,
) -> list[str]:
    if not isinstance(value, list):
        issues.append(ImportIssue(path, "TYPE", "必须是数组。"))
        return []
    if len(value) > maximum_items:
        issues.append(ImportIssue(path, "MAX_ITEMS", f"不能超过 {maximum_items} 项。"))
    result: list[str] = []
    for index, item in enumerate(value[:maximum_items]):
        item_path = f"{path}[{index}]"
        if not isinstance(item, str) or not item.strip():
            issues.append(ImportIssue(item_path, "TYPE", "必须是非空字符串。"))
            continue
        clean = item.strip()
        if len(clean) > maximum_text:
            issues.append(ImportIssue(item_path, "MAX_LENGTH", f"不能超过 {maximum_text} 字符。"))
        if allowed is not None and clean not in allowed:
            issues.append(ImportIssue(item_path, "UNKNOWN_REFERENCE", "引用不在冻结证据索引中。"))
        result.append(clean)
    return result


def validate_import_result(
    value: Mapping[str, Any],
    bundle: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, list[ImportIssue]]:
    issues: list[ImportIssue] = []
    _unknown_fields(value, {
        "version", "room_id", "round_id", "bundle_sha256", "context_sha256",
        "declared_model", "panels", "final_synthesis",
    }, "$", issues)
    exact_fields = {
        "version": MANUAL_CHATGPT_RESULT_VERSION,
        "room_id": _bounded_text(bundle.get("room_id"), 80),
        "round_id": _bounded_text(bundle.get("round_id"), 80),
        "bundle_sha256": _bounded_text(bundle.get("bundle_sha256"), 64),
        "context_sha256": _bounded_text(bundle.get("context_sha256"), 64),
    }
    normalized: dict[str, Any] = {}
    for field, expected in exact_fields.items():
        actual = value.get(field)
        if actual != expected:
            issues.append(ImportIssue(f"$.{field}", "EXACT_VALUE", "必须与冻结任务包完全一致。"))
        normalized[field] = actual
    normalized["declared_model"] = _required_text(
        value, "declared_model", "$", issues, maximum=160
    )

    context = _json_object(bundle.get("context"))
    expected_roles = [
        _bounded_text(role.get("role_id"), 80)
        for role in _json_list(context.get("roles"))
        if isinstance(role, Mapping) and _bounded_text(role.get("role_id"), 80)
    ]
    evidence_ids = {
        _bounded_text(item.get("evidence_id"), 80)
        for item in _json_list(context.get("evidence_index"))
        if isinstance(item, Mapping) and _bounded_text(item.get("evidence_id"), 80)
    }
    expected_kinds = [
        str(item)
        for item in _json_list(_json_object(bundle.get("budget")).get("panel_kinds"))
    ]
    raw_panels = value.get("panels")
    panels: list[dict[str, Any]] = []
    if not isinstance(raw_panels, list):
        issues.append(ImportIssue("$.panels", "TYPE", "必须是数组。"))
        raw_panels = []
    if len(raw_panels) != len(expected_kinds):
        issues.append(ImportIssue("$.panels", "EXACT_COUNT", f"必须恰好包含 {len(expected_kinds)} 个 Panel。"))
    panel_ids: set[str] = set()
    for index, raw_panel in enumerate(raw_panels):
        path = f"$.panels[{index}]"
        if not isinstance(raw_panel, Mapping):
            issues.append(ImportIssue(path, "TYPE", "Panel 必须是对象。"))
            continue
        _unknown_fields(raw_panel, {
            "panel_id", "panel_kind", "call_index", "declared_independence",
            "summary", "conclusion", "disagreements", "risks", "evidence_refs", "role_views",
        }, path, issues)
        panel_id = _required_text(raw_panel, "panel_id", path, issues, maximum=80)
        if panel_id in panel_ids:
            issues.append(ImportIssue(f"{path}.panel_id", "DUPLICATE", "Panel ID 必须唯一。"))
        panel_ids.add(panel_id)
        expected_kind = expected_kinds[index] if index < len(expected_kinds) else ""
        panel_kind = raw_panel.get("panel_kind")
        if panel_kind != expected_kind:
            issues.append(ImportIssue(f"{path}.panel_kind", "EXACT_VALUE", f"此位置必须是 {expected_kind or '契约指定类型'}。"))
        if type(raw_panel.get("call_index")) is not int or raw_panel.get("call_index") != index + 1:
            issues.append(ImportIssue(f"{path}.call_index", "EXACT_VALUE", f"必须是整数 {index + 1}。"))
        independence = raw_panel.get("declared_independence")
        if independence not in INDEPENDENCE_CLASSIFICATIONS:
            issues.append(ImportIssue(f"{path}.declared_independence", "ENUM", "独立性声明不在允许集合中。"))
        summary = _required_text(raw_panel, "summary", path, issues, maximum=8_000)
        conclusion = _required_text(raw_panel, "conclusion", path, issues, maximum=8_000)
        disagreements = _string_list(raw_panel.get("disagreements"), f"{path}.disagreements", issues, maximum_items=24)
        risks = _string_list(raw_panel.get("risks"), f"{path}.risks", issues, maximum_items=24)
        refs = _string_list(raw_panel.get("evidence_refs"), f"{path}.evidence_refs", issues, allowed=evidence_ids)
        raw_views = raw_panel.get("role_views")
        views: list[dict[str, Any]] = []
        if not isinstance(raw_views, list):
            issues.append(ImportIssue(f"{path}.role_views", "TYPE", "必须是数组。"))
            raw_views = []
        if len(raw_views) != len(expected_roles):
            issues.append(ImportIssue(f"{path}.role_views", "EXACT_COUNT", f"必须恰好包含 {len(expected_roles)} 个角色视角。"))
        seen_roles: set[str] = set()
        for view_index, raw_view in enumerate(raw_views):
            view_path = f"{path}.role_views[{view_index}]"
            if not isinstance(raw_view, Mapping):
                issues.append(ImportIssue(view_path, "TYPE", "角色视角必须是对象。"))
                continue
            _unknown_fields(raw_view, {"role_id", "assessment", "evidence_refs", "uncertainty"}, view_path, issues)
            role_id = raw_view.get("role_id")
            if not isinstance(role_id, str) or role_id not in expected_roles:
                issues.append(ImportIssue(f"{view_path}.role_id", "UNKNOWN_REFERENCE", "角色不在冻结角色表中。"))
                role_id = str(role_id or "")
            if role_id in seen_roles:
                issues.append(ImportIssue(f"{view_path}.role_id", "DUPLICATE", "角色视角不能重复。"))
            seen_roles.add(role_id)
            views.append({
                "role_id": role_id,
                "assessment": _required_text(raw_view, "assessment", view_path, issues, maximum=3_000),
                "evidence_refs": _string_list(raw_view.get("evidence_refs"), f"{view_path}.evidence_refs", issues, allowed=evidence_ids),
                "uncertainty": _required_text(raw_view, "uncertainty", view_path, issues, maximum=1_500),
            })
        if set(expected_roles) != seen_roles:
            issues.append(ImportIssue(f"{path}.role_views", "ROLE_COVERAGE", "必须且只能覆盖冻结角色表中的每个角色一次。"))
        panels.append({
            "panel_id": panel_id,
            "panel_kind": panel_kind,
            "call_index": raw_panel.get("call_index"),
            "declared_independence": independence,
            "independence_trusted": False,
            "summary": summary,
            "conclusion": conclusion,
            "disagreements": disagreements,
            "risks": risks,
            "evidence_refs": refs,
            "role_views": views,
        })

    raw_final = value.get("final_synthesis")
    final: dict[str, Any] = {}
    if not isinstance(raw_final, Mapping):
        issues.append(ImportIssue("$.final_synthesis", "TYPE", "必须是对象。"))
        raw_final = {}
    _unknown_fields(raw_final, {
        "summary", "decision_options", "recommended_option_id", "open_questions", "evidence_refs",
    }, "$.final_synthesis", issues)
    final["summary"] = _required_text(raw_final, "summary", "$.final_synthesis", issues, maximum=10_000)
    raw_options = raw_final.get("decision_options")
    if not isinstance(raw_options, list) or not raw_options:
        issues.append(ImportIssue("$.final_synthesis.decision_options", "REQUIRED", "必须包含至少一个决策选项。"))
        raw_options = []
    if len(raw_options) > 8:
        issues.append(ImportIssue("$.final_synthesis.decision_options", "MAX_ITEMS", "不能超过 8 项。"))
    options: list[dict[str, Any]] = []
    option_ids: set[str] = set()
    for index, raw_option in enumerate(raw_options[:8]):
        path = f"$.final_synthesis.decision_options[{index}]"
        if not isinstance(raw_option, Mapping):
            issues.append(ImportIssue(path, "TYPE", "决策选项必须是对象。"))
            continue
        _unknown_fields(raw_option, {"option_id", "title", "rationale", "evidence_refs", "risks"}, path, issues)
        option_id = _required_text(raw_option, "option_id", path, issues, maximum=80)
        if option_id in option_ids:
            issues.append(ImportIssue(f"{path}.option_id", "DUPLICATE", "决策选项 ID 必须唯一。"))
        option_ids.add(option_id)
        options.append({
            "option_id": option_id,
            "title": _required_text(raw_option, "title", path, issues, maximum=240),
            "rationale": _required_text(raw_option, "rationale", path, issues, maximum=5_000),
            "evidence_refs": _string_list(raw_option.get("evidence_refs"), f"{path}.evidence_refs", issues, allowed=evidence_ids),
            "risks": _string_list(raw_option.get("risks"), f"{path}.risks", issues, maximum_items=20),
        })
    recommendation = raw_final.get("recommended_option_id")
    if not isinstance(recommendation, str):
        issues.append(ImportIssue("$.final_synthesis.recommended_option_id", "TYPE", "必须是字符串；无推荐时使用空字符串。"))
        recommendation = ""
    recommendation = recommendation.strip()
    if recommendation and recommendation not in option_ids:
        issues.append(ImportIssue("$.final_synthesis.recommended_option_id", "UNKNOWN_REFERENCE", "推荐选项不在 decision_options 中。"))
    final.update({
        "decision_options": options,
        "recommended_option_id": recommendation,
        "open_questions": _string_list(raw_final.get("open_questions"), "$.final_synthesis.open_questions", issues, maximum_items=30),
        "evidence_refs": _string_list(raw_final.get("evidence_refs"), "$.final_synthesis.evidence_refs", issues, allowed=evidence_ids),
    })
    normalized["panels"] = panels
    normalized["final_synthesis"] = final
    normalized["declared_model_trusted"] = False
    normalized["role_views_are_independent_opinions"] = False
    return (None, issues) if issues else (normalized, [])


def repair_prompt(bundle: Mapping[str, Any], issues: Sequence[ImportIssue]) -> str:
    paths = "\n".join(
        f"- {issue.path}: {issue.code} — {issue.message}" for issue in issues[:80]
    )
    return "\n".join([
        "请只修复 JSON 格式和下列契约错误，不要改变研究结论或补造证据。",
        "修复后只返回一个 JSON 对象；保留冻结的 room_id、round_id、bundle_sha256 和 context_sha256。",
        "declared_model 仍只是用户声明，不得声称已被系统验证。",
        "",
        paths or "- $: 未提供可诊断错误。",
        "",
        "RESULT_TEMPLATE:",
        json.dumps(_schema_template(bundle), ensure_ascii=False, indent=2, sort_keys=True),
    ])


def _review_schema_template(review_kind: str) -> dict[str, Any]:
    return {
        "version": MANUAL_CHATGPT_API_REVIEW_VERSION,
        "review_kind": review_kind,
        "verdict": "pass|concern|block",
        "summary": "",
        "findings": [{
            "severity": "low|medium|high|blocking",
            "claim": "",
            "rationale": "",
            "evidence_refs": [],
        }],
        "open_questions": [],
    }


def _review_instructions(review_kind: str) -> str:
    focus = {
        "fact_check": "核查关键事实、引文绑定和结论是否超出冻结证据。",
        "counterargument": "给出真正独立的反方审查，寻找未处理的替代解释。",
        "risk_review": "审查风险、不可逆后果、权限边界和遗漏的失败模式。",
        "evidence_audit": "审计证据覆盖、来源强度、不确定性和可复现性。",
    }[review_kind]
    return "\n".join([
        "你是只读研究审查器。不得执行交易、调用工具、补造证据或扩大权限。",
        focus,
        "只评审提供的冻结输入。evidence_refs 只能引用输入中的 evidence_id。",
        "pass 表示没有实质问题；concern 表示存在非阻断问题；block 表示在用户决定前必须阻断。",
        "只返回一个 JSON 对象，字段必须与模板完全一致，不得使用 Markdown。",
        json.dumps(_review_schema_template(review_kind), ensure_ascii=False, sort_keys=True),
    ])


def _review_input(
    *,
    session_id: str,
    bundle: Mapping[str, Any],
    result: Mapping[str, Any],
    result_sha256: str,
    review_kind: str,
    review_index: int,
) -> dict[str, Any]:
    context = _json_object(bundle.get("context"))
    return {
        "version": MANUAL_CHATGPT_REVIEW_PLAN_VERSION,
        "session_id": session_id,
        "review_index": review_index,
        "review_kind": review_kind,
        "room_id": _bounded_text(bundle.get("room_id"), 80),
        "round_id": _bounded_text(bundle.get("round_id"), 80),
        "bundle_sha256": _bounded_text(bundle.get("bundle_sha256"), 64),
        "context_sha256": _bounded_text(bundle.get("context_sha256"), 64),
        "result_sha256": result_sha256,
        "objective": _bounded_text(context.get("objective"), MAX_OBJECTIVE_CHARS),
        "evidence_index": _json_list(context.get("evidence_index")),
        "candidate_matrix": _json_list(context.get("candidate_matrix")),
        "imported_result": copy.deepcopy(dict(result)),
        "safety": {
            "read_only_research": True,
            "execution_capability": "none",
            "live_trading_allowed": False,
            "user_final_decision_required": True,
        },
    }


def validate_api_review(
    raw: Any,
    *,
    review_kind: str,
    allowed_evidence_ids: set[str],
) -> dict[str, Any]:
    try:
        parsed = parse_single_json_object(raw)
    except ManualChatGPTError as exc:
        raise ManualChatGPTError(
            "API 审查返回的 JSON 无法解析。",
            code="MANUAL_CHATGPT_API_REVIEW_INVALID",
            status=422,
        ) from exc
    required = {
        "version", "review_kind", "verdict", "summary", "findings", "open_questions",
    }
    if set(parsed) != required:
        raise ManualChatGPTError(
            "API 审查返回字段不符合闭合契约。",
            code="MANUAL_CHATGPT_API_REVIEW_INVALID",
            status=422,
        )
    if parsed.get("version") != MANUAL_CHATGPT_API_REVIEW_VERSION:
        raise ManualChatGPTError(
            "API 审查版本不受支持。",
            code="MANUAL_CHATGPT_API_REVIEW_INVALID",
            status=422,
        )
    if parsed.get("review_kind") != review_kind:
        raise ManualChatGPTError(
            "API 审查类型与独立调用计划不一致。",
            code="MANUAL_CHATGPT_API_REVIEW_INVALID",
            status=422,
        )
    verdict = str(parsed.get("verdict") or "").strip().lower()
    summary = _bounded_text(parsed.get("summary"), 4_000)
    if verdict not in {"pass", "concern", "block"} or not summary:
        raise ManualChatGPTError(
            "API 审查 verdict 或 summary 无效。",
            code="MANUAL_CHATGPT_API_REVIEW_INVALID",
            status=422,
        )
    raw_findings = parsed.get("findings")
    if not isinstance(raw_findings, list) or len(raw_findings) > 12:
        raise ManualChatGPTError(
            "API 审查 findings 必须是最多 12 项的数组。",
            code="MANUAL_CHATGPT_API_REVIEW_INVALID",
            status=422,
        )
    findings: list[dict[str, Any]] = []
    for finding in raw_findings:
        if not isinstance(finding, Mapping) or set(finding) != {
            "severity", "claim", "rationale", "evidence_refs",
        }:
            raise ManualChatGPTError(
                "API 审查 finding 字段无效。",
                code="MANUAL_CHATGPT_API_REVIEW_INVALID",
                status=422,
            )
        severity = str(finding.get("severity") or "").strip().lower()
        claim = _bounded_text(finding.get("claim"), 1_000)
        rationale = _bounded_text(finding.get("rationale"), 2_000)
        refs = finding.get("evidence_refs")
        if (
            severity not in {"low", "medium", "high", "blocking"}
            or not claim
            or not rationale
            or not isinstance(refs, list)
            or len(refs) > 20
        ):
            raise ManualChatGPTError(
                "API 审查 finding 内容无效。",
                code="MANUAL_CHATGPT_API_REVIEW_INVALID",
                status=422,
            )
        clean_refs: list[str] = []
        for reference in refs:
            clean_reference = _bounded_text(reference, 120)
            if not clean_reference or clean_reference not in allowed_evidence_ids:
                raise ManualChatGPTError(
                    "API 审查引用了冻结范围外的证据。",
                    code="MANUAL_CHATGPT_API_REVIEW_INVALID",
                    status=422,
                )
            if clean_reference not in clean_refs:
                clean_refs.append(clean_reference)
        findings.append({
            "severity": severity,
            "claim": claim,
            "rationale": rationale,
            "evidence_refs": clean_refs,
        })
    raw_questions = parsed.get("open_questions")
    if not isinstance(raw_questions, list) or len(raw_questions) > 12:
        raise ManualChatGPTError(
            "API 审查 open_questions 必须是最多 12 项的数组。",
            code="MANUAL_CHATGPT_API_REVIEW_INVALID",
            status=422,
        )
    questions = [_bounded_text(item, 1_000) for item in raw_questions]
    if any(not item for item in questions):
        raise ManualChatGPTError(
            "API 审查 open_questions 含有空项。",
            code="MANUAL_CHATGPT_API_REVIEW_INVALID",
            status=422,
        )
    if verdict == "pass" and any(
        item["severity"] in {"high", "blocking"} for item in findings
    ):
        raise ManualChatGPTError(
            "API 审查 pass 与高风险 finding 冲突。",
            code="MANUAL_CHATGPT_API_REVIEW_INVALID",
            status=422,
        )
    if verdict == "block" and not any(
        item["severity"] == "blocking" for item in findings
    ):
        raise ManualChatGPTError(
            "API 审查 block 必须包含 blocking finding。",
            code="MANUAL_CHATGPT_API_REVIEW_INVALID",
            status=422,
        )
    return {
        "version": MANUAL_CHATGPT_API_REVIEW_VERSION,
        "review_kind": review_kind,
        "verdict": verdict,
        "summary": summary,
        "findings": findings,
        "open_questions": questions,
    }


def build_decision_card(
    *,
    session: Mapping[str, Any],
    reviews: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    result = _json_object(session.get("result"))
    synthesis = _json_object(result.get("final_synthesis"))
    ordered_reviews = [copy.deepcopy(dict(review)) for review in reviews]
    blocking_findings: list[dict[str, Any]] = []
    concerns: list[dict[str, Any]] = []
    for review in ordered_reviews:
        kind = _bounded_text(review.get("review_kind"), 80)
        for finding in _json_list(review.get("findings")):
            if not isinstance(finding, Mapping):
                continue
            projected = {
                "review_kind": kind,
                "severity": _bounded_text(finding.get("severity"), 20),
                "claim": _bounded_text(finding.get("claim"), 1_000),
                "rationale": _bounded_text(finding.get("rationale"), 2_000),
                "evidence_refs": _json_list(finding.get("evidence_refs")),
            }
            if projected["severity"] == "blocking":
                blocking_findings.append(projected)
            else:
                concerns.append(projected)
    return {
        "version": MANUAL_CHATGPT_DECISION_CARD_VERSION,
        "session_id": _bounded_text(session.get("id"), 80),
        "room_id": _bounded_text(session.get("room_id"), 80),
        "round_id": _bounded_text(session.get("round_id"), 80),
        "bundle_sha256": _bounded_text(session.get("bundle_sha256"), 64),
        "context_sha256": _bounded_text(session.get("context_sha256"), 64),
        "result_sha256": _bounded_text(session.get("result_sha256"), 64),
        "summary": _bounded_text(synthesis.get("summary"), 8_000),
        "decision_options": _json_list(synthesis.get("decision_options")),
        "imported_recommended_option_id": _bounded_text(
            synthesis.get("recommended_option_id"),
            120,
        ),
        "recommendation_provenance": "manual_chatgpt_import_untrusted_model_declaration",
        "review_count": len(ordered_reviews),
        "review_verdicts": [{
            "review_kind": _bounded_text(review.get("review_kind"), 80),
            "verdict": _bounded_text(review.get("verdict"), 20),
            "summary": _bounded_text(review.get("summary"), 4_000),
        } for review in ordered_reviews],
        "blocking_findings": blocking_findings,
        "nonblocking_findings": concerns,
        "open_questions": list(dict.fromkeys(
            _bounded_text(question, 1_000)
            for review in ordered_reviews
            for question in _json_list(review.get("open_questions"))
            if _bounded_text(question, 1_000)
        ))[:30],
        "ready_for_user_decision": not blocking_findings,
        "user_confirmation_required": True,
        "safety": {
            "research_only": True,
            "execution_capability": "none",
            "live_trading_allowed": False,
            "provider_reviews_are_advisory": True,
        },
    }


_TRANSITIONS = {
    "": {"DRAFT"},
    "DRAFT": {"BUNDLE_READY"},
    "BUNDLE_READY": {"WAITING_FOR_CHATGPT", "BUDGET_BLOCKED", "NEEDS_USER_ACTION"},
    "WAITING_FOR_CHATGPT": {"RESULT_IMPORTED", "IMPORT_REJECTED", "CONTEXT_STALE", "NEEDS_USER_ACTION"},
    "IMPORT_REJECTED": {"RESULT_IMPORTED", "IMPORT_REJECTED", "CONTEXT_STALE", "NEEDS_USER_ACTION"},
    "RESULT_IMPORTED": {"VALIDATING", "IMPORT_REJECTED"},
    "VALIDATING": {"API_REVIEW", "IMPORT_REJECTED"},
    "API_REVIEW": {
        "API_REVIEW",
        "READY_FOR_DECISION",
        "CONTEXT_STALE",
        "BUDGET_BLOCKED",
        "NEEDS_USER_ACTION",
    },
    "READY_FOR_DECISION": {"FROZEN", "CONTEXT_STALE", "NEEDS_USER_ACTION"},
}


class ManualChatGPTService:
    def __init__(
        self,
        store: StudioStore,
        *,
        review_rate_card: Mapping[str, Any] | None = None,
        providers: Any = None,
    ) -> None:
        self.store = store
        self.providers = providers
        self.review_rate_card = (
            copy.deepcopy(dict(review_rate_card))
            if review_rate_card is not None
            else api_review_rate_card_from_environment()
        )

    @staticmethod
    def _review_tables_available(connection: Any) -> bool:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?)",
            (
                "manual_chatgpt_review_runs",
                "manual_chatgpt_api_reviews",
                "manual_chatgpt_decisions",
            ),
        ).fetchall()
        return {str(row[0]) for row in rows} == {
            "manual_chatgpt_review_runs",
            "manual_chatgpt_api_reviews",
            "manual_chatgpt_decisions",
        }

    @classmethod
    def _require_review_tables(cls, connection: Any) -> None:
        if not cls._review_tables_available(connection):
            raise ManualChatGPTError(
                "独立 API 审查表尚未迁移；正式迁移需要单独授权。",
                code="MANUAL_CHATGPT_MIGRATION_REQUIRED",
                status=409,
            )

    @staticmethod
    def _review_recovery_table_available(connection: Any) -> bool:
        row = connection.execute(
            """SELECT 1 FROM sqlite_master
                 WHERE type='table' AND name='manual_chatgpt_review_recoveries'"""
        ).fetchone()
        return bool(row)

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{secrets.token_hex(12)}"

    @staticmethod
    def _loads_object(raw: Any) -> dict[str, Any]:
        try:
            value = json.loads(str(raw or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _loads_list(raw: Any) -> list[Any]:
        try:
            value = json.loads(str(raw or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return value if isinstance(value, list) else []

    @staticmethod
    def _context_stale_issue(
        snapshot: Mapping[str, Any],
        *,
        objective: Any,
        mode: Any,
        expected_context_sha256: Any,
    ) -> ImportIssue | None:
        current_context = compact_context(
            snapshot,
            objective=objective,
            mode=mode,
        )
        current_sha256 = canonical_sha256(current_context)
        expected_sha256 = _bounded_text(expected_context_sha256, 64).lower()
        if (
            re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
            and secrets.compare_digest(current_sha256, expected_sha256)
        ):
            return None
        return ImportIssue(
            "$.context_sha256",
            "CONTEXT_STALE",
            "房间角色、证据、候选或历史决定已变化，请生成新任务包。",
        )

    def _transition_context_stale(
        self,
        connection: Any,
        *,
        session_id: str,
        room_id: str,
        from_state: str,
        issue: ImportIssue,
        stage: str,
        created_at: int,
        review_run_id: str = "",
    ) -> dict[str, Any]:
        if review_run_id:
            connection.execute(
                """UPDATE manual_chatgpt_review_runs
                      SET status='FAILED',error_code='MANUAL_CHATGPT_CONTEXT_STALE',
                          updated_at=?,completed_at=?
                    WHERE id=? AND session_id=? AND status='RUNNING'""",
                (created_at, created_at, review_run_id, session_id),
            )
        connection.execute(
            """UPDATE manual_chatgpt_sessions
                  SET last_issues_json=?
                WHERE id=? AND room_id=?""",
            (
                json.dumps([issue.as_dict()], ensure_ascii=False),
                session_id,
                room_id,
            ),
        )
        payload: dict[str, Any] = {
            "issue_code": issue.code,
            "stage": _bounded_text(stage, 80),
        }
        if review_run_id:
            payload.update({
                "review_run_id": review_run_id,
                "provider_calls_are_not_refunded": True,
            })
        self._append_event(
            connection,
            session_id=session_id,
            room_id=room_id,
            from_state=from_state,
            to_state="CONTEXT_STALE",
            event_type="context_drift_detected",
            payload=payload,
            created_at=created_at,
        )
        return self._public_session(connection, session_id, room_id)

    def _append_event(
        self,
        connection: Any,
        *,
        session_id: str,
        room_id: str,
        from_state: str,
        to_state: str,
        event_type: str,
        payload: Mapping[str, Any],
        created_at: int,
    ) -> None:
        if to_state not in _TRANSITIONS.get(from_state, set()):
            raise ManualChatGPTError(
                f"不允许从 {from_state or 'EMPTY'} 转到 {to_state}。",
                code="MANUAL_CHATGPT_STATE_CONFLICT",
                status=409,
            )
        row = connection.execute(
            "SELECT event_sequence,event_head_sha256 FROM manual_chatgpt_sessions WHERE id=? AND room_id=?",
            (session_id, room_id),
        ).fetchone()
        if not row:
            raise LookupError("ChatGPT 协作任务不存在。")
        sequence_no = int(row["event_sequence"] or 0) + 1
        previous = str(row["event_head_sha256"] or "")
        event_payload = {
            "version": MANUAL_CHATGPT_EVENT_VERSION,
            "session_id": session_id,
            "room_id": room_id,
            "sequence_no": sequence_no,
            "from_state": from_state,
            "to_state": to_state,
            "event_type": event_type,
            "payload": copy.deepcopy(dict(payload)),
            "previous_event_sha256": previous,
            "created_at": int(created_at),
        }
        event_sha256 = canonical_sha256(event_payload)
        connection.execute(
            """INSERT INTO manual_chatgpt_events(
                   id,session_id,room_id,sequence_no,from_state,to_state,event_type,
                   payload_json,previous_event_sha256,event_sha256,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                self._new_id("mcge"), session_id, room_id, sequence_no, from_state,
                to_state, event_type,
                json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                previous, event_sha256, int(created_at),
            ),
        )
        expected_current_state = to_state if not from_state else from_state
        updated = connection.execute(
            """UPDATE manual_chatgpt_sessions
                  SET state=?,event_sequence=?,event_head_sha256=?,updated_at=?
                WHERE id=? AND room_id=? AND state=? AND event_sequence=?""",
            (
                to_state, sequence_no, event_sha256, int(created_at), session_id,
                room_id, expected_current_state, sequence_no - 1,
            ),
        )
        if updated.rowcount != 1:
            raise ManualChatGPTError(
                "ChatGPT 协作状态已并发变化。",
                code="MANUAL_CHATGPT_STATE_CONFLICT",
                status=409,
            )

    def create(self, room_id: str, *, objective: Any, mode: Any = "standard") -> dict[str, Any]:
        clean_room_id = _bounded_text(room_id, 80)
        preset = mode_preset(mode)
        timestamp = now_ms()
        session_id = self._new_id("mcg")
        round_id = self._new_id("mcgr")
        with self.store._lock:
            snapshot = self.store.room_snapshot(clean_room_id)
            if not snapshot:
                raise LookupError("房间不存在。")
            bundle = build_compact_bundle(
                snapshot,
                objective=objective,
                mode=preset["id"],
                session_id=session_id,
                round_id=round_id,
                created_at=timestamp,
                review_rate_card=self.review_rate_card,
            )
            bundle_json = json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            with closing(self.store._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """INSERT INTO manual_chatgpt_sessions(
                           id,room_id,round_id,mode,state,objective,bundle_json,
                           bundle_sha256,context_sha256,result_json,result_sha256,
                           declared_model,declared_model_trusted,last_issues_json,
                           event_sequence,event_head_sha256,created_at,updated_at,frozen_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        session_id, clean_room_id, round_id, preset["id"], "DRAFT",
                        bundle["context"]["objective"], bundle_json,
                        bundle["bundle_sha256"], bundle["context_sha256"], "{}", "",
                        "", 0, "[]", 0, "", timestamp, timestamp, 0,
                    ),
                )
                self._append_event(
                    connection,
                    session_id=session_id,
                    room_id=clean_room_id,
                    from_state="",
                    to_state="DRAFT",
                    event_type="session_created",
                    payload={"mode": preset["id"]},
                    created_at=timestamp,
                )
                self._append_event(
                    connection,
                    session_id=session_id,
                    room_id=clean_room_id,
                    from_state="DRAFT",
                    to_state="BUNDLE_READY",
                    event_type="bundle_frozen",
                    payload={
                        "bundle_sha256": bundle["bundle_sha256"],
                        "context_sha256": bundle["context_sha256"],
                    },
                    created_at=timestamp,
                )
        record = self.get(clean_room_id, session_id)
        if not record:
            raise RuntimeError("ChatGPT 协作任务未能重新读取。")
        return record

    def dispatch(self, room_id: str, session_id: str) -> dict[str, Any]:
        timestamp = now_ms()
        with self.store._lock, closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state,bundle_sha256 FROM manual_chatgpt_sessions WHERE id=? AND room_id=?",
                (session_id, room_id),
            ).fetchone()
            if not row:
                raise LookupError("ChatGPT 协作任务不存在。")
            self._append_event(
                connection,
                session_id=session_id,
                room_id=room_id,
                from_state=str(row["state"]),
                to_state="WAITING_FOR_CHATGPT",
                event_type="task_prompt_copied",
                payload={"bundle_sha256": str(row["bundle_sha256"])},
                created_at=timestamp,
            )
        return self.get(room_id, session_id) or {}

    def import_result(self, room_id: str, session_id: str, raw: Any) -> dict[str, Any]:
        timestamp = now_ms()
        with self.store._lock:
            snapshot = self.store.room_snapshot(room_id)
            if not snapshot:
                raise LookupError("房间不存在。")
            with closing(self.store._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM manual_chatgpt_sessions WHERE id=? AND room_id=?",
                    (session_id, room_id),
                ).fetchone()
                if not row:
                    raise LookupError("ChatGPT 协作任务不存在。")
                state = str(row["state"])
                if state not in {"WAITING_FOR_CHATGPT", "IMPORT_REJECTED"}:
                    raise ManualChatGPTError(
                        "当前状态不接受 ChatGPT 导入。",
                        code="MANUAL_CHATGPT_STATE_CONFLICT",
                        status=409,
                    )
                bundle = self._loads_object(row["bundle_json"])
                stale_issue = self._context_stale_issue(
                    snapshot,
                    objective=row["objective"],
                    mode=row["mode"],
                    expected_context_sha256=row["context_sha256"],
                )
                if stale_issue is not None:
                    return self._transition_context_stale(
                        connection,
                        session_id=session_id,
                        room_id=room_id,
                        from_state=state,
                        issue=stale_issue,
                        stage="before_import",
                        created_at=timestamp,
                    )
                try:
                    parsed = parse_single_json_object(raw)
                except ManualChatGPTError as exc:
                    issues = exc.issues or [ImportIssue("$", exc.code, str(exc))]
                    connection.execute(
                        "UPDATE manual_chatgpt_sessions SET last_issues_json=? WHERE id=?",
                        (json.dumps([item.as_dict() for item in issues], ensure_ascii=False), session_id),
                    )
                    self._append_event(
                        connection,
                        session_id=session_id,
                        room_id=room_id,
                        from_state=state,
                        to_state="IMPORT_REJECTED",
                        event_type="import_rejected",
                        payload={"issue_codes": [item.code for item in issues]},
                        created_at=timestamp,
                    )
                    return self._public_session(connection, session_id, room_id)
                normalized, issues = validate_import_result(parsed, bundle)
                if issues or normalized is None:
                    connection.execute(
                        "UPDATE manual_chatgpt_sessions SET last_issues_json=? WHERE id=?",
                        (json.dumps([item.as_dict() for item in issues], ensure_ascii=False), session_id),
                    )
                    self._append_event(
                        connection,
                        session_id=session_id,
                        room_id=room_id,
                        from_state=state,
                        to_state="IMPORT_REJECTED",
                        event_type="import_rejected",
                        payload={"issue_codes": [item.code for item in issues]},
                        created_at=timestamp,
                    )
                    return self._public_session(connection, session_id, room_id)
                result_json = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                result_sha256 = canonical_sha256(normalized)
                connection.execute(
                    """UPDATE manual_chatgpt_sessions
                          SET result_json=?,result_sha256=?,declared_model=?,
                              declared_model_trusted=0,last_issues_json='[]'
                        WHERE id=? AND room_id=?""",
                    (
                        result_json, result_sha256, normalized["declared_model"],
                        session_id, room_id,
                    ),
                )
                self._append_event(
                    connection,
                    session_id=session_id,
                    room_id=room_id,
                    from_state=state,
                    to_state="RESULT_IMPORTED",
                    event_type="validated_result_persisted",
                    payload={"result_sha256": result_sha256},
                    created_at=timestamp,
                )
                self._append_event(
                    connection,
                    session_id=session_id,
                    room_id=room_id,
                    from_state="RESULT_IMPORTED",
                    to_state="VALIDATING",
                    event_type="deterministic_validation_started",
                    payload={},
                    created_at=timestamp,
                )
                self._append_event(
                    connection,
                    session_id=session_id,
                    room_id=room_id,
                    from_state="VALIDATING",
                    to_state="API_REVIEW",
                    event_type="deterministic_validation_passed",
                    payload={
                        "result_sha256": result_sha256,
                        "declared_model_trusted": False,
                    },
                    created_at=timestamp,
                )
                return self._public_session(connection, session_id, room_id)

    def _mark_review_run_failed(
        self,
        room_id: str,
        session_id: str,
        *,
        review_run_id: str,
        error_code: str,
        budget_blocked: bool = False,
    ) -> dict[str, Any]:
        timestamp = now_ms()
        clean_code = re.sub(
            r"[^A-Z0-9_]+",
            "_",
            str(error_code or "MANUAL_CHATGPT_API_REVIEW_FAILED").upper(),
        ).strip("_")[:80] or "MANUAL_CHATGPT_API_REVIEW_FAILED"
        issue = ImportIssue(
            "$.api_review",
            clean_code,
            "独立 API 审查未完整通过；不会生成可冻结决定。",
        )
        target_state = "BUDGET_BLOCKED" if budget_blocked else "NEEDS_USER_ACTION"
        with self.store._lock, closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_review_tables(connection)
            row = connection.execute(
                "SELECT state FROM manual_chatgpt_sessions WHERE id=? AND room_id=?",
                (session_id, room_id),
            ).fetchone()
            if not row:
                raise LookupError("ChatGPT 协作任务不存在。")
            connection.execute(
                """UPDATE manual_chatgpt_review_runs
                      SET status=?,error_code=?,updated_at=?,completed_at=?
                    WHERE id=? AND session_id=?""",
                (
                    "BUDGET_BLOCKED" if budget_blocked else "FAILED",
                    clean_code,
                    timestamp,
                    timestamp,
                    review_run_id,
                    session_id,
                ),
            )
            connection.execute(
                "UPDATE manual_chatgpt_sessions SET last_issues_json=? WHERE id=?",
                (json.dumps([issue.as_dict()], ensure_ascii=False), session_id),
            )
            if str(row["state"] or "") == "API_REVIEW":
                self._append_event(
                    connection,
                    session_id=session_id,
                    room_id=room_id,
                    from_state="API_REVIEW",
                    to_state=target_state,
                    event_type="api_review_failed",
                    payload={
                        "review_run_id": review_run_id,
                        "error_code": clean_code,
                        "provider_calls_are_not_refunded": True,
                    },
                    created_at=timestamp,
                )
            return self._public_session(connection, session_id, room_id)

    def run_api_review(
        self,
        room_id: str,
        session_id: str,
        *,
        provider_id: Any,
        model: Any = "",
        client_request_id: Any,
        expected_result_sha256: Any,
    ) -> dict[str, Any]:
        clean_provider_id = _bounded_text(provider_id, 80).lower()
        clean_model = _bounded_text(model, 160)
        clean_request_id = _bounded_text(client_request_id, 160)
        clean_expected_hash = _bounded_text(expected_result_sha256, 64).lower()
        if not clean_provider_id or not clean_request_id:
            raise ManualChatGPTError(
                "独立 API 审查必须显式提供 provider 和 client_request_id。",
                code="MANUAL_CHATGPT_REVIEW_REQUEST_INVALID",
            )
        if not re.fullmatch(r"[0-9a-f]{64}", clean_expected_hash):
            raise ManualChatGPTError(
                "独立 API 审查必须绑定有效的 result_sha256。",
                code="MANUAL_CHATGPT_REVIEW_REQUEST_INVALID",
            )
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN")
            self._require_review_tables(connection)
        session = self.get(room_id, session_id)
        if not session:
            raise LookupError("ChatGPT 协作任务不存在。")
        if session.get("integrity", {}).get("ok") is not True:
            raise ManualChatGPTError(
                "协作任务完整性失败，禁止执行 API 审查。",
                code="MANUAL_CHATGPT_INTEGRITY_FAILED",
                status=409,
            )
        if not secrets.compare_digest(
            str(session.get("result_sha256") or ""),
            clean_expected_hash,
        ):
            raise ManualChatGPTError(
                "导入结果已变化，请重新确认审查输入。",
                code="MANUAL_CHATGPT_REVIEW_INPUT_STALE",
                status=409,
            )
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN")
            existing_authorization = connection.execute(
                "SELECT * FROM manual_chatgpt_review_runs WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if existing_authorization:
                route_matches = bool(
                    str(existing_authorization["client_request_id"] or "")
                    == clean_request_id
                    and str(existing_authorization["provider"] or "")
                    == clean_provider_id
                    and (
                        not clean_model
                        or str(existing_authorization["requested_model"] or "")
                        == clean_model
                    )
                )
                if not route_matches:
                    raise ManualChatGPTError(
                        "该任务已绑定另一份 API 审查授权。",
                        code="MANUAL_CHATGPT_REVIEW_REQUEST_CONFLICT",
                        status=409,
                    )
                if str(existing_authorization["status"] or "") in {
                    "COMPLETED", "FAILED", "BUDGET_BLOCKED",
                }:
                    return session
                raise ManualChatGPTError(
                    "该任务的 API 审查已启动且不能重复调用。",
                    code="MANUAL_CHATGPT_REVIEW_ALREADY_STARTED",
                    status=409,
                )
        if session.get("state") != "API_REVIEW":
            raise ManualChatGPTError(
                "当前状态不允许执行独立 API 审查。",
                code="MANUAL_CHATGPT_STATE_CONFLICT",
                status=409,
            )
        if self.providers is None:
            raise ManualChatGPTError(
                "独立 API 审查 Provider 注册表不可用。",
                code="MANUAL_CHATGPT_PROVIDER_UNAVAILABLE",
                status=503,
            )
        provider = self.providers.get(clean_provider_id)
        if provider is None:
            raise ManualChatGPTError(
                "所选 Provider 不存在或被固定策略禁用。",
                code="MANUAL_CHATGPT_PROVIDER_UNAVAILABLE",
                status=409,
            )
        try:
            provider_status = provider.status()
        except Exception as exc:
            raise ManualChatGPTError(
                "无法读取所选 Provider 状态。",
                code="MANUAL_CHATGPT_PROVIDER_UNAVAILABLE",
                status=503,
            ) from exc
        if not bool((provider_status or {}).get("configured")):
            raise ManualChatGPTError(
                "所选 Provider 尚未配置；未发送任何调用。",
                code="MANUAL_CHATGPT_PROVIDER_NOT_CONFIGURED",
                status=409,
            )
        resolved_model = clean_model
        if not resolved_model:
            resolver = getattr(self.providers, "resolved_model", None)
            resolved_model = (
                _bounded_text(resolver(clean_provider_id, ""), 160)
                if callable(resolver)
                else _bounded_text((provider_status or {}).get("model"), 160)
            )
        if not resolved_model:
            raise ManualChatGPTError(
                "所选 Provider 没有可冻结的模型路由；未发送任何调用。",
                code="MANUAL_CHATGPT_MODEL_REQUIRED",
                status=409,
            )
        mode = str(session.get("mode") or "")
        review_kinds = MODE_REVIEW_KINDS.get(mode) or []
        expected_calls = int(
            _json_object(_json_object(session.get("bundle")).get("budget")).get(
                "independent_api_reviews"
            ) or 0
        )
        if len(review_kinds) != expected_calls or expected_calls not in {2, 3, 4}:
            raise ManualChatGPTError(
                "冻结任务包的 API 审查预算不一致。",
                code="MANUAL_CHATGPT_REVIEW_BUDGET_INVALID",
                status=409,
            )
        review_inputs = [
            _review_input(
                session_id=session_id,
                bundle=_json_object(session.get("bundle")),
                result=_json_object(session.get("result")),
                result_sha256=clean_expected_hash,
                review_kind=kind,
                review_index=index,
            )
            for index, kind in enumerate(review_kinds, start=1)
        ]
        plan = {
            "version": MANUAL_CHATGPT_REVIEW_PLAN_VERSION,
            "session_id": session_id,
            "room_id": room_id,
            "result_sha256": clean_expected_hash,
            "provider": clean_provider_id,
            "model": resolved_model,
            "expected_calls": expected_calls,
            "reviews": [{
                "review_index": index,
                "review_kind": kind,
                "request_sha256": canonical_sha256(review_inputs[index - 1]),
            } for index, kind in enumerate(review_kinds, start=1)],
        }
        plan_sha256 = canonical_sha256(plan)
        review_run_id = self._new_id("mcgrv")
        timestamp = now_ms()
        with self.store._lock:
            snapshot = self.store.room_snapshot(room_id)
            if not snapshot:
                raise LookupError("房间不存在。")
            with closing(self.store._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                self._require_review_tables(connection)
                current = connection.execute(
                    """SELECT state,result_sha256,objective,mode,context_sha256
                         FROM manual_chatgpt_sessions WHERE id=? AND room_id=?""",
                    (session_id, room_id),
                ).fetchone()
                if (
                    not current
                    or str(current["state"] or "") != "API_REVIEW"
                    or not secrets.compare_digest(
                        str(current["result_sha256"] or ""),
                        clean_expected_hash,
                    )
                ):
                    raise ManualChatGPTError(
                        "API 审查输入在启动前已变化；未发送任何调用。",
                        code="MANUAL_CHATGPT_REVIEW_INPUT_STALE",
                        status=409,
                    )
                stale_issue = self._context_stale_issue(
                    snapshot,
                    objective=current["objective"],
                    mode=current["mode"],
                    expected_context_sha256=current["context_sha256"],
                )
                if stale_issue is not None:
                    return self._transition_context_stale(
                        connection,
                        session_id=session_id,
                        room_id=room_id,
                        from_state="API_REVIEW",
                        issue=stale_issue,
                        stage="before_api_review_authorization",
                        created_at=timestamp,
                    )
            try:
                ledger = ProviderCallLedger.create(
                    self.store,
                    room_id,
                    scope="manual_chatgpt_review",
                    client_request_id=clean_request_id,
                    plan_hash=plan_sha256,
                    max_calls=expected_calls,
                    kind_call_limits={"manual_chatgpt_review": expected_calls},
                )
            except Exception as exc:
                raise ManualChatGPTError(
                    "无法冻结独立 API 审查调用预算；未发送任何调用。",
                    code="MANUAL_CHATGPT_REVIEW_LEDGER_FAILED",
                    status=409,
                ) from exc
            with closing(self.store._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                self._require_review_tables(connection)
                current = connection.execute(
                    "SELECT state,result_sha256 FROM manual_chatgpt_sessions WHERE id=? AND room_id=?",
                    (session_id, room_id),
                ).fetchone()
                if (
                    not current
                    or str(current["state"] or "") != "API_REVIEW"
                    or not secrets.compare_digest(
                        str(current["result_sha256"] or ""),
                        clean_expected_hash,
                    )
                ):
                    raise ManualChatGPTError(
                        "API 审查输入在启动前已变化；未发送任何调用。",
                        code="MANUAL_CHATGPT_REVIEW_INPUT_STALE",
                        status=409,
                    )
                if connection.execute(
                    "SELECT 1 FROM manual_chatgpt_review_runs WHERE session_id=?",
                    (session_id,),
                ).fetchone():
                    raise ManualChatGPTError(
                        "API 审查已由另一请求启动；未发送重复调用。",
                        code="MANUAL_CHATGPT_REVIEW_ALREADY_STARTED",
                        status=409,
                    )
                connection.execute(
                    """INSERT INTO manual_chatgpt_review_runs(
                           id,session_id,room_id,provider_execution_run_id,
                           client_request_id,provider,requested_model,mode,status,
                           expected_calls,completed_calls,plan_json,plan_sha256,
                           error_code,created_at,updated_at,completed_at
                       ) VALUES(?,?,?,?,?,?,?,?, 'RUNNING',?,0,?,?, '',?,?,0)""",
                    (
                        review_run_id,
                        session_id,
                        room_id,
                        ledger.run_id,
                        clean_request_id,
                        clean_provider_id,
                        resolved_model,
                        mode,
                        expected_calls,
                        json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                        plan_sha256,
                        timestamp,
                        timestamp,
                    ),
                )
        evidence_ids = {
            _bounded_text(item.get("evidence_id"), 120)
            for item in _json_list(
                _json_object(_json_object(session.get("bundle")).get("context")).get(
                    "evidence_index"
                )
            )
            if isinstance(item, Mapping) and _bounded_text(item.get("evidence_id"), 120)
        }
        completed_reviews: list[dict[str, Any]] = []
        for index, (kind, review_input) in enumerate(
            zip(review_kinds, review_inputs, strict=True),
            start=1,
        ):
            with self.store._lock:
                snapshot = self.store.room_snapshot(room_id)
                if not snapshot:
                    raise LookupError("房间不存在。")
                stale_issue = self._context_stale_issue(
                    snapshot,
                    objective=session.get("objective"),
                    mode=session.get("mode"),
                    expected_context_sha256=session.get("context_sha256"),
                )
                if stale_issue is not None:
                    with closing(self.store._connect()) as connection, connection:
                        connection.execute("BEGIN IMMEDIATE")
                        current = connection.execute(
                            """SELECT state,result_sha256
                                 FROM manual_chatgpt_sessions
                                WHERE id=? AND room_id=?""",
                            (session_id, room_id),
                        ).fetchone()
                        if (
                            not current
                            or str(current["state"] or "") != "API_REVIEW"
                            or not secrets.compare_digest(
                                str(current["result_sha256"] or ""),
                                clean_expected_hash,
                            )
                        ):
                            raise ManualChatGPTError(
                                "API 审查输入在调用前已变化。",
                                code="MANUAL_CHATGPT_REVIEW_INPUT_STALE",
                                status=409,
                            )
                        return self._transition_context_stale(
                            connection,
                            session_id=session_id,
                            room_id=room_id,
                            from_state="API_REVIEW",
                            issue=stale_issue,
                            stage=f"before_api_review_call_{index}",
                            created_at=now_ms(),
                            review_run_id=review_run_id,
                        )
                try:
                    reservation = ledger.reserve(
                        kind="manual_chatgpt_review",
                        provider=clean_provider_id,
                        model=resolved_model,
                    )
                except Exception as exc:
                    budget_blocked = "budget" in str(
                        getattr(exc, "code", "") or ""
                    ).lower()
                    return self._mark_review_run_failed(
                        room_id,
                        session_id,
                        review_run_id=review_run_id,
                        error_code=(
                            "MANUAL_CHATGPT_REVIEW_BUDGET_EXHAUSTED"
                            if budget_blocked
                            else "MANUAL_CHATGPT_REVIEW_LEDGER_FAILED"
                        ),
                        budget_blocked=budget_blocked,
                    )
            started = time.monotonic()
            terminal_status = "FAILED"
            terminal_code = "provider_error"
            usage: Any = None
            normalized_review: dict[str, Any] | None = None
            response_model = ""
            try:
                request = {
                    "instructions": _review_instructions(kind),
                    "input_text": json.dumps(
                        review_input,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "model": resolved_model,
                }
                generate_json = getattr(provider, "generate_json", None)
                response = (
                    generate_json(**request)
                    if callable(generate_json)
                    else provider.generate(**request)
                )
                usage = getattr(response, "usage", None)
                response_provider = _bounded_text(
                    getattr(response, "provider", ""),
                    80,
                ).lower()
                response_model = _bounded_text(getattr(response, "model", ""), 160)
                if not bool(getattr(response, "ok", False)):
                    terminal_code = normalize_provider_error_code(
                        getattr(response, "error_code", "")
                    )
                elif response_provider != clean_provider_id:
                    terminal_status = "INVALID"
                    terminal_code = "provider_identity_mismatch"
                elif not response_model:
                    terminal_status = "INVALID"
                    terminal_code = "model_identity_missing"
                elif (
                    completed_reviews
                    and response_model
                    != str(completed_reviews[0].get("response_model") or "")
                ):
                    terminal_status = "INVALID"
                    terminal_code = "model_identity_mismatch"
                else:
                    normalized_review = validate_api_review(
                        getattr(response, "content", ""),
                        review_kind=kind,
                        allowed_evidence_ids=evidence_ids,
                    )
                    terminal_status = "RESPONDED"
                    terminal_code = ""
            except ManualChatGPTError:
                terminal_status = "INVALID"
                terminal_code = "invalid_response"
            except Exception as exc:
                terminal_status = "FAILED"
                terminal_code = classify_provider_exception(exc)
            elapsed_ms = min(
                604_800_000,
                max(0, int((time.monotonic() - started) * 1000)),
            )
            try:
                ledger.finish(
                    str(reservation.get("id") or ""),
                    str(reservation.get("attempt_token") or ""),
                    status=terminal_status,
                    error_code=terminal_code,
                    elapsed_ms=elapsed_ms,
                    usage=usage,
                )
            except Exception:
                return self._mark_review_run_failed(
                    room_id,
                    session_id,
                    review_run_id=review_run_id,
                    error_code="MANUAL_CHATGPT_REVIEW_LEDGER_FAILED",
                )
            if terminal_status != "RESPONDED" or normalized_review is None:
                return self._mark_review_run_failed(
                    room_id,
                    session_id,
                    review_run_id=review_run_id,
                    error_code=f"MANUAL_CHATGPT_REVIEW_{terminal_code.upper()}",
                )
            request_sha256 = canonical_sha256(review_input)
            review_record_basis = {
                "version": MANUAL_CHATGPT_API_REVIEW_RECORD_VERSION,
                "review_index": index,
                "review_kind": kind,
                "provider": clean_provider_id,
                "requested_model": resolved_model,
                "response_model": response_model,
                "independence_classification": "same_model_independent_call",
                "provider_attempt_id": str(reservation.get("id") or ""),
                "request_sha256": request_sha256,
                "review": normalized_review,
            }
            review_sha256 = canonical_sha256(review_record_basis)
            persisted_review = {
                **normalized_review,
                "review_index": index,
                "provider": clean_provider_id,
                "requested_model": resolved_model,
                "response_model": response_model,
                "independence_classification": "same_model_independent_call",
                "provider_attempt_id": str(reservation.get("id") or ""),
                "request_sha256": request_sha256,
                "review_sha256": review_sha256,
            }
            with self.store._lock, closing(self.store._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """INSERT INTO manual_chatgpt_api_reviews(
                           id,review_run_id,session_id,room_id,review_index,review_kind,
                           provider,requested_model,response_model,
                           independence_classification,provider_attempt_id,
                           request_sha256,review_json,review_sha256,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        self._new_id("mcgar"),
                        review_run_id,
                        session_id,
                        room_id,
                        index,
                        kind,
                        clean_provider_id,
                        resolved_model,
                        response_model,
                        "same_model_independent_call",
                        str(reservation.get("id") or ""),
                        request_sha256,
                        json.dumps(
                            normalized_review,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        review_sha256,
                        now_ms(),
                    ),
                )
                connection.execute(
                    """UPDATE manual_chatgpt_review_runs
                          SET completed_calls=?,updated_at=?
                        WHERE id=? AND status='RUNNING'""",
                    (index, now_ms(), review_run_id),
                )
            completed_reviews.append(persisted_review)
        reviews_sha256 = canonical_sha256([
            review["review_sha256"] for review in completed_reviews
        ])
        decision_card = build_decision_card(session=session, reviews=completed_reviews)
        decision_card["reviews_sha256"] = reviews_sha256
        decision_card_sha256 = canonical_sha256(decision_card)
        target_state = (
            "READY_FOR_DECISION"
            if decision_card["ready_for_user_decision"]
            else "NEEDS_USER_ACTION"
        )
        timestamp = now_ms()
        with self.store._lock:
            snapshot = self.store.room_snapshot(room_id)
            if not snapshot:
                raise LookupError("房间不存在。")
            with closing(self.store._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    """SELECT state,result_sha256,objective,mode,context_sha256
                         FROM manual_chatgpt_sessions WHERE id=? AND room_id=?""",
                    (session_id, room_id),
                ).fetchone()
                if (
                    not current
                    or str(current["state"] or "") != "API_REVIEW"
                    or not secrets.compare_digest(
                        str(current["result_sha256"] or ""),
                        clean_expected_hash,
                    )
                ):
                    raise ManualChatGPTError(
                        "审查完成时协作状态已变化，决定卡未发布。",
                        code="MANUAL_CHATGPT_STATE_CONFLICT",
                        status=409,
                    )
                stale_issue = self._context_stale_issue(
                    snapshot,
                    objective=current["objective"],
                    mode=current["mode"],
                    expected_context_sha256=current["context_sha256"],
                )
                if stale_issue is not None:
                    return self._transition_context_stale(
                        connection,
                        session_id=session_id,
                        room_id=room_id,
                        from_state="API_REVIEW",
                        issue=stale_issue,
                        stage="before_decision_card_publish",
                        created_at=timestamp,
                        review_run_id=review_run_id,
                    )
                connection.execute(
                    """UPDATE manual_chatgpt_review_runs
                          SET status='COMPLETED',completed_calls=?,error_code='',
                              updated_at=?,completed_at=?
                        WHERE id=? AND status='RUNNING'""",
                    (expected_calls, timestamp, timestamp, review_run_id),
                )
                connection.execute(
                    """INSERT INTO manual_chatgpt_decisions(
                           session_id,room_id,review_run_id,result_sha256,reviews_sha256,
                           decision_card_json,decision_card_sha256,selected_option_id,
                           confirmation_json,confirmation_sha256,created_at,frozen_at
                       ) VALUES(?,?,?,?,?,?,?, '', '{}','',?,0)""",
                    (
                        session_id,
                        room_id,
                        review_run_id,
                        clean_expected_hash,
                        reviews_sha256,
                        json.dumps(
                            decision_card,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        decision_card_sha256,
                        timestamp,
                    ),
                )
                connection.execute(
                    "UPDATE manual_chatgpt_sessions SET last_issues_json='[]' WHERE id=?",
                    (session_id,),
                )
                self._append_event(
                    connection,
                    session_id=session_id,
                    room_id=room_id,
                    from_state="API_REVIEW",
                    to_state=target_state,
                    event_type="api_reviews_completed",
                    payload={
                        "review_run_id": review_run_id,
                        "provider_execution_run_id": ledger.run_id,
                        "completed_calls": expected_calls,
                        "reviews_sha256": reviews_sha256,
                        "decision_card_sha256": decision_card_sha256,
                        "blocking_findings": len(decision_card["blocking_findings"]),
                    },
                    created_at=timestamp,
                )
                return self._public_session(connection, session_id, room_id)

    def freeze_decision(
        self,
        room_id: str,
        session_id: str,
        *,
        expected_result_sha256: Any,
        decision_card_sha256: Any,
        selected_option_id: Any,
        acknowledgement: Any,
    ) -> dict[str, Any]:
        clean_result_hash = _bounded_text(expected_result_sha256, 64).lower()
        clean_card_hash = _bounded_text(decision_card_sha256, 64).lower()
        clean_option_id = _bounded_text(selected_option_id, 120)
        clean_acknowledgement = _bounded_text(acknowledgement, 80)
        if (
            not re.fullmatch(r"[0-9a-f]{64}", clean_result_hash)
            or not re.fullmatch(r"[0-9a-f]{64}", clean_card_hash)
            or not clean_option_id
            or clean_acknowledgement != MANUAL_CHATGPT_FREEZE_ACKNOWLEDGEMENT
        ):
            raise ManualChatGPTError(
                "冻结请求缺少精确哈希、决定选项或研究只读确认。",
                code="MANUAL_CHATGPT_FREEZE_REQUEST_INVALID",
            )
        timestamp = now_ms()
        with self.store._lock, closing(self.store._connect()) as connection, connection:
            snapshot = self.store.room_snapshot(room_id)
            if not snapshot:
                raise LookupError("房间不存在。")
            connection.execute("BEGIN IMMEDIATE")
            self._require_review_tables(connection)
            row = connection.execute(
                """SELECT state,result_sha256,objective,mode,context_sha256
                     FROM manual_chatgpt_sessions WHERE id=? AND room_id=?""",
                (session_id, room_id),
            ).fetchone()
            if not row:
                raise LookupError("ChatGPT 协作任务不存在。")
            decision = connection.execute(
                "SELECT * FROM manual_chatgpt_decisions WHERE session_id=? AND room_id=?",
                (session_id, room_id),
            ).fetchone()
            if str(row["state"] or "") == "FROZEN" and decision:
                try:
                    persisted_confirmation = json.loads(
                        str(decision["confirmation_json"] or "{}")
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    persisted_confirmation = {}
                if (
                    str(row["result_sha256"] or "") == clean_result_hash
                    and str(decision["decision_card_sha256"] or "") == clean_card_hash
                    and str(decision["selected_option_id"] or "") == clean_option_id
                    and str(persisted_confirmation.get("acknowledgement") or "")
                    == MANUAL_CHATGPT_FREEZE_ACKNOWLEDGEMENT
                    and canonical_sha256(persisted_confirmation)
                    == str(decision["confirmation_sha256"] or "")
                ):
                    return self._public_session(connection, session_id, room_id)
                raise ManualChatGPTError(
                    "已冻结决定与重放请求不一致。",
                    code="MANUAL_CHATGPT_FREEZE_REQUEST_CONFLICT",
                    status=409,
                )
            if str(row["state"] or "") != "READY_FOR_DECISION":
                raise ManualChatGPTError(
                    "只有 READY_FOR_DECISION 才能由用户冻结。",
                    code="MANUAL_CHATGPT_STATE_CONFLICT",
                    status=409,
                )
            stale_issue = self._context_stale_issue(
                snapshot,
                objective=row["objective"],
                mode=row["mode"],
                expected_context_sha256=row["context_sha256"],
            )
            if stale_issue is not None:
                return self._transition_context_stale(
                    connection,
                    session_id=session_id,
                    room_id=room_id,
                    from_state="READY_FOR_DECISION",
                    issue=stale_issue,
                    stage="before_user_decision_freeze",
                    created_at=timestamp,
                )
            if not decision:
                raise ManualChatGPTError(
                    "决定卡不存在，禁止冻结。",
                    code="MANUAL_CHATGPT_DECISION_CARD_MISSING",
                    status=409,
                )
            try:
                card = json.loads(str(decision["decision_card_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ManualChatGPTError(
                    "决定卡完整性失败，禁止冻结。",
                    code="MANUAL_CHATGPT_INTEGRITY_FAILED",
                    status=409,
                ) from exc
            if (
                not secrets.compare_digest(str(row["result_sha256"] or ""), clean_result_hash)
                or not secrets.compare_digest(str(decision["result_sha256"] or ""), clean_result_hash)
                or not secrets.compare_digest(str(decision["decision_card_sha256"] or ""), clean_card_hash)
                or canonical_sha256(card) != clean_card_hash
                or card.get("ready_for_user_decision") is not True
            ):
                raise ManualChatGPTError(
                    "冻结前置哈希或决定卡状态已变化。",
                    code="MANUAL_CHATGPT_FREEZE_INPUT_STALE",
                    status=409,
                )
            option_ids = {
                _bounded_text(option.get("option_id"), 120)
                for option in _json_list(card.get("decision_options"))
                if isinstance(option, Mapping)
            }
            if clean_option_id not in option_ids:
                raise ManualChatGPTError(
                    "所选决定不在冻结决定卡中。",
                    code="MANUAL_CHATGPT_FREEZE_OPTION_INVALID",
                )
            confirmation = {
                "version": MANUAL_CHATGPT_CONFIRMATION_VERSION,
                "session_id": session_id,
                "room_id": room_id,
                "result_sha256": clean_result_hash,
                "decision_card_sha256": clean_card_hash,
                "selected_option_id": clean_option_id,
                "acknowledgement": MANUAL_CHATGPT_FREEZE_ACKNOWLEDGEMENT,
                "confirmed_at": timestamp,
            }
            confirmation_sha256 = canonical_sha256(confirmation)
            connection.execute(
                """UPDATE manual_chatgpt_decisions
                      SET selected_option_id=?,confirmation_json=?,
                          confirmation_sha256=?,frozen_at=?
                    WHERE session_id=? AND confirmation_sha256=''""",
                (
                    clean_option_id,
                    json.dumps(
                        confirmation,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    confirmation_sha256,
                    timestamp,
                    session_id,
                ),
            )
            connection.execute(
                "UPDATE manual_chatgpt_sessions SET frozen_at=? WHERE id=?",
                (timestamp, session_id),
            )
            self._append_event(
                connection,
                session_id=session_id,
                room_id=room_id,
                from_state="READY_FOR_DECISION",
                to_state="FROZEN",
                event_type="user_decision_frozen",
                payload={
                    "decision_card_sha256": clean_card_hash,
                    "confirmation_sha256": confirmation_sha256,
                    "selected_option_id": clean_option_id,
                    "provider_calls_performed": 0,
                },
                created_at=timestamp,
            )
            return self._public_session(connection, session_id, room_id)

    def recover_api_review(
        self,
        room_id: str,
        session_id: str,
        *,
        expected_result_sha256: Any,
        acknowledgement: Any,
    ) -> dict[str, Any]:
        clean_expected_hash = _bounded_text(expected_result_sha256, 64).lower()
        clean_acknowledgement = _bounded_text(acknowledgement, 80)
        if (
            not re.fullmatch(r"[0-9a-f]{64}", clean_expected_hash)
            or clean_acknowledgement
            != MANUAL_CHATGPT_REVIEW_RECOVERY_ACKNOWLEDGEMENT
        ):
            raise ManualChatGPTError(
                "恢复独立审查需要绑定当前结果并显式重新授权。",
                code="MANUAL_CHATGPT_REVIEW_RECOVERY_REQUEST_INVALID",
            )
        timestamp = now_ms()
        with self.store._lock, closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_review_tables(connection)
            if not self._review_recovery_table_available(connection):
                raise ManualChatGPTError(
                    "独立审查恢复表尚未迁移；正式迁移需要单独授权。",
                    code="MANUAL_CHATGPT_MIGRATION_REQUIRED",
                    status=409,
                )
            session_row = connection.execute(
                """SELECT * FROM manual_chatgpt_sessions
                     WHERE id=? AND room_id=?""",
                (session_id, room_id),
            ).fetchone()
            if not session_row:
                raise LookupError("ChatGPT 协作任务不存在。")
            if (
                str(session_row["state"] or "") != "API_REVIEW"
                or not secrets.compare_digest(
                    str(session_row["result_sha256"] or ""),
                    clean_expected_hash,
                )
            ):
                raise ManualChatGPTError(
                    "当前任务或导入结果不允许恢复独立审查。",
                    code="MANUAL_CHATGPT_REVIEW_RECOVERY_INPUT_STALE",
                    status=409,
                )
            run_row = connection.execute(
                """SELECT * FROM manual_chatgpt_review_runs
                     WHERE session_id=?""",
                (session_id,),
            ).fetchone()
            if not run_row or str(run_row["status"] or "") != "RUNNING":
                raise ManualChatGPTError(
                    "当前没有可恢复的运行中审查。",
                    code="MANUAL_CHATGPT_REVIEW_RECOVERY_NOT_AVAILABLE",
                    status=409,
                )
            review_run = dict(run_row)
            provider_execution_run_id = str(
                review_run.get("provider_execution_run_id") or ""
            )
            execution_run = connection.execute(
                "SELECT * FROM provider_execution_runs WHERE id=?",
                (provider_execution_run_id,),
            ).fetchone()
            api_review_count = int(connection.execute(
                "SELECT COUNT(*) FROM manual_chatgpt_api_reviews WHERE review_run_id=?",
                (str(review_run.get("id") or ""),),
            ).fetchone()[0])
            attempt_count = int(connection.execute(
                "SELECT COUNT(*) FROM provider_call_attempts WHERE run_id=?",
                (provider_execution_run_id,),
            ).fetchone()[0])
            decision_count = int(connection.execute(
                "SELECT COUNT(*) FROM manual_chatgpt_decisions WHERE session_id=?",
                (session_id,),
            ).fetchone()[0])
            age_ms = max(0, timestamp - int(review_run.get("updated_at") or 0))
            ledger_unused = bool(
                execution_run
                and int(execution_run["reserved_calls"] or 0) == 0
                and int(execution_run["completed_calls"] or 0) == 0
            )
            if (
                age_ms < MANUAL_CHATGPT_REVIEW_ORPHAN_AGE_MS
                or int(review_run.get("completed_calls") or 0) != 0
                or api_review_count != 0
                or attempt_count != 0
                or decision_count != 0
                or not ledger_unused
            ):
                raise ManualChatGPTError(
                    "审查尚未达到零调用孤儿恢复条件；不会删除或退款任何调用。",
                    code="MANUAL_CHATGPT_REVIEW_RECOVERY_NOT_SAFE",
                    status=409,
                )
            snapshot = {
                "version": MANUAL_CHATGPT_REVIEW_RECOVERY_VERSION,
                "session_id": session_id,
                "room_id": room_id,
                "review_run": review_run,
                "provider_execution_run": dict(execution_run),
                "provider_calls_performed": 0,
            }
            snapshot_sha256 = canonical_sha256(snapshot)
            connection.execute(
                """INSERT INTO manual_chatgpt_review_recoveries(
                       id,record_version,session_id,room_id,
                       previous_review_run_id,previous_provider_execution_run_id,
                       previous_run_snapshot_json,previous_run_snapshot_sha256,
                       acknowledgement,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    self._new_id("mcgrc"),
                    MANUAL_CHATGPT_REVIEW_RECOVERY_VERSION,
                    session_id,
                    room_id,
                    str(review_run.get("id") or ""),
                    provider_execution_run_id,
                    json.dumps(
                        snapshot,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    snapshot_sha256,
                    clean_acknowledgement,
                    timestamp,
                ),
            )
            connection.execute(
                """UPDATE provider_execution_runs
                      SET status='ABANDONED',updated_at=?,completed_at=?
                    WHERE id=? AND status IN ('OPEN','EXHAUSTED')""",
                (timestamp, timestamp, provider_execution_run_id),
            )
            deleted = connection.execute(
                """DELETE FROM manual_chatgpt_review_runs
                     WHERE id=? AND session_id=? AND status='RUNNING'
                       AND completed_calls=0""",
                (str(review_run.get("id") or ""), session_id),
            )
            if deleted.rowcount != 1:
                raise ManualChatGPTError(
                    "审查恢复期间状态发生变化。",
                    code="MANUAL_CHATGPT_REVIEW_RECOVERY_CONFLICT",
                    status=409,
                )
            self._append_event(
                connection,
                session_id=session_id,
                room_id=room_id,
                from_state="API_REVIEW",
                to_state="API_REVIEW",
                event_type="api_review_reauthorized",
                payload={
                    "previous_review_run_id": str(review_run.get("id") or ""),
                    "recovery_snapshot_sha256": snapshot_sha256,
                    "provider_calls_performed": 0,
                    "next_client_request_id_must_be_new": True,
                },
                created_at=timestamp,
            )
            return self._public_session(connection, session_id, room_id)

    def list(self, room_id: str, *, limit: int = 30) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ManualChatGPTError(
                "limit 必须是 1 到 50 的整数。",
                code="MANUAL_CHATGPT_LIST_REQUEST_INVALID",
            )
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                """SELECT id FROM manual_chatgpt_sessions
                     WHERE room_id=? ORDER BY created_at DESC,id DESC LIMIT ?""",
                (room_id, limit),
            ).fetchall()
            return [
                self._public_session(connection, str(row["id"]), room_id)
                for row in rows
            ]

    def latest(self, room_id: str) -> dict[str, Any] | None:
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                """SELECT id FROM manual_chatgpt_sessions
                     WHERE room_id=? ORDER BY created_at DESC,id DESC LIMIT 1""",
                (room_id,),
            ).fetchone()
            return self._public_session(connection, str(row["id"]), room_id) if row else None

    def get(self, room_id: str, session_id: str) -> dict[str, Any] | None:
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT id FROM manual_chatgpt_sessions WHERE id=? AND room_id=?",
                (session_id, room_id),
            ).fetchone()
            return self._public_session(connection, session_id, room_id) if row else None

    def _public_session(self, connection: Any, session_id: str, room_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM manual_chatgpt_sessions WHERE id=? AND room_id=?",
            (session_id, room_id),
        ).fetchone()
        if not row:
            raise LookupError("ChatGPT 协作任务不存在。")
        data = dict(row)
        bundle = self._loads_object(data.get("bundle_json"))
        result = self._loads_object(data.get("result_json"))
        issues = self._loads_list(data.get("last_issues_json"))
        event_rows = connection.execute(
            "SELECT * FROM manual_chatgpt_events WHERE session_id=? ORDER BY sequence_no",
            (session_id,),
        ).fetchall()
        review_feature_available = self._review_tables_available(connection)
        review_run: dict[str, Any] = {}
        public_reviews: list[dict[str, Any]] = []
        decision_card: dict[str, Any] = {}
        decision_card_sha256 = ""
        confirmation: dict[str, Any] = {}
        reviews_integrity_ok = True
        decision_integrity_ok = True
        recovery_integrity_ok = True
        recovery_records: list[dict[str, Any]] = []
        recovery_status = {
            "available": False,
            "eligible": False,
            "reason_code": "RECOVERY_MIGRATION_REQUIRED",
            "minimum_orphan_age_ms": MANUAL_CHATGPT_REVIEW_ORPHAN_AGE_MS,
            "age_ms": 0,
            "acknowledgement": MANUAL_CHATGPT_REVIEW_RECOVERY_ACKNOWLEDGEMENT,
            "recovery_count": 0,
        }
        provider_calls_performed = 0
        if review_feature_available:
            run_row = connection.execute(
                "SELECT * FROM manual_chatgpt_review_runs WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if run_row:
                review_run = dict(run_row)
                try:
                    plan = json.loads(str(review_run.get("plan_json") or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    plan = {}
                execution_run = connection.execute(
                    "SELECT * FROM provider_execution_runs WHERE id=?",
                    (str(review_run.get("provider_execution_run_id") or ""),),
                ).fetchone()
                reviews_integrity_ok = bool(
                    isinstance(plan, dict)
                    and plan
                    and canonical_sha256(plan) == str(review_run.get("plan_sha256") or "")
                    and str(plan.get("session_id") or "") == session_id
                    and str(plan.get("room_id") or "") == room_id
                    and str(plan.get("provider") or "")
                    == str(review_run.get("provider") or "")
                    and str(plan.get("model") or "")
                    == str(review_run.get("requested_model") or "")
                    and int(plan.get("expected_calls") or 0)
                    == int(review_run.get("expected_calls") or 0)
                    and execution_run
                    and str(execution_run["room_id"] or "") == room_id
                    and str(execution_run["scope"] or "") == "manual_chatgpt_review"
                    and str(execution_run["client_request_id"] or "")
                    == str(review_run.get("client_request_id") or "")
                    and str(execution_run["plan_hash"] or "")
                    == str(review_run.get("plan_sha256") or "")
                    and int(execution_run["max_calls"] or 0)
                    == int(review_run.get("expected_calls") or 0)
                )
                planned_reviews = {
                    int(item.get("review_index") or 0): item
                    for item in _json_list(plan.get("reviews"))
                    if isinstance(item, Mapping)
                }
                review_rows = connection.execute(
                    """SELECT * FROM manual_chatgpt_api_reviews
                         WHERE session_id=? ORDER BY review_index""",
                    (session_id,),
                ).fetchall()
                attempt_ids: set[str] = set()
                for review_row in review_rows:
                    review_data = dict(review_row)
                    try:
                        review_content = json.loads(
                            str(review_data.get("review_json") or "{}")
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        review_content = {}
                    attempt_id = str(review_data.get("provider_attempt_id") or "")
                    attempt = connection.execute(
                        "SELECT * FROM provider_call_attempts WHERE id=?",
                        (attempt_id,),
                    ).fetchone()
                    review_index = int(review_data.get("review_index") or 0)
                    planned_review = planned_reviews.get(review_index) or {}
                    review_record_basis = {
                        "version": MANUAL_CHATGPT_API_REVIEW_RECORD_VERSION,
                        "review_index": review_index,
                        "review_kind": str(review_data.get("review_kind") or ""),
                        "provider": str(review_data.get("provider") or ""),
                        "requested_model": str(
                            review_data.get("requested_model") or ""
                        ),
                        "response_model": str(review_data.get("response_model") or ""),
                        "independence_classification": str(
                            review_data.get("independence_classification") or ""
                        ),
                        "provider_attempt_id": attempt_id,
                        "request_sha256": str(review_data.get("request_sha256") or ""),
                        "review": review_content,
                    }
                    row_ok = bool(
                        isinstance(review_content, dict)
                        and review_content
                        and canonical_sha256(review_record_basis)
                        == str(review_data.get("review_sha256") or "")
                        and str(review_content.get("review_kind") or "")
                        == str(review_data.get("review_kind") or "")
                        and attempt_id
                        and attempt_id not in attempt_ids
                        and attempt
                        and str(attempt["run_id"] or "")
                        == str(review_run.get("provider_execution_run_id") or "")
                        and str(attempt["status"] or "") == "RESPONDED"
                        and str(attempt["kind"] or "") == "manual_chatgpt_review"
                        and str(attempt["provider"] or "")
                        == str(review_data.get("provider") or "")
                        and str(planned_review.get("review_kind") or "")
                        == str(review_data.get("review_kind") or "")
                        and str(planned_review.get("request_sha256") or "")
                        == str(review_data.get("request_sha256") or "")
                    )
                    reviews_integrity_ok = reviews_integrity_ok and row_ok
                    attempt_ids.add(attempt_id)
                    public_reviews.append({
                        **review_content,
                        "review_index": int(review_data.get("review_index") or 0),
                        "provider": str(review_data.get("provider") or ""),
                        "requested_model": str(review_data.get("requested_model") or ""),
                        "response_model": str(review_data.get("response_model") or ""),
                        "independence_classification": str(
                            review_data.get("independence_classification") or ""
                        ),
                        "request_sha256": str(review_data.get("request_sha256") or ""),
                        "review_sha256": str(review_data.get("review_sha256") or ""),
                    })
                provider_calls_performed = int(connection.execute(
                    "SELECT COUNT(*) FROM provider_call_attempts WHERE run_id=?",
                    (str(review_run.get("provider_execution_run_id") or ""),),
                ).fetchone()[0])
                if (
                    str(review_run.get("status") or "") == "COMPLETED"
                    and (
                        len(public_reviews) != int(review_run.get("expected_calls") or 0)
                        or len(public_reviews) != int(review_run.get("completed_calls") or 0)
                    )
                ):
                    reviews_integrity_ok = False
            decision_row = connection.execute(
                "SELECT * FROM manual_chatgpt_decisions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if decision_row:
                decision_data = dict(decision_row)
                try:
                    decision_card = json.loads(
                        str(decision_data.get("decision_card_json") or "{}")
                    )
                    confirmation = json.loads(
                        str(decision_data.get("confirmation_json") or "{}")
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    decision_card = {}
                    confirmation = {}
                decision_card_sha256 = str(
                    decision_data.get("decision_card_sha256") or ""
                )
                expected_reviews_sha256 = canonical_sha256([
                    review.get("review_sha256") for review in public_reviews
                ])
                decision_integrity_ok = bool(
                    decision_card
                    and canonical_sha256(decision_card) == decision_card_sha256
                    and str(decision_data.get("result_sha256") or "")
                    == str(data.get("result_sha256") or "")
                    and str(decision_data.get("reviews_sha256") or "")
                    == expected_reviews_sha256
                    and str(decision_card.get("reviews_sha256") or "")
                    == expected_reviews_sha256
                )
                confirmation_sha256 = str(
                    decision_data.get("confirmation_sha256") or ""
                )
                if confirmation_sha256:
                    decision_integrity_ok = bool(
                        decision_integrity_ok
                        and confirmation
                        and canonical_sha256(confirmation) == confirmation_sha256
                        and str(confirmation.get("decision_card_sha256") or "")
                        == decision_card_sha256
                        and str(confirmation.get("selected_option_id") or "")
                        == str(decision_data.get("selected_option_id") or "")
                    )
            stored_state = str(data.get("state") or "")
            if stored_state in {"READY_FOR_DECISION", "FROZEN"}:
                decision_integrity_ok = bool(
                    decision_integrity_ok
                    and decision_card
                    and decision_card.get("ready_for_user_decision") is True
                )
            if stored_state == "FROZEN":
                decision_integrity_ok = bool(
                    decision_integrity_ok and confirmation
                )
            if self._review_recovery_table_available(connection):
                recovery_rows = connection.execute(
                    """SELECT * FROM manual_chatgpt_review_recoveries
                         WHERE session_id=? ORDER BY created_at,id""",
                    (session_id,),
                ).fetchall()
                for recovery_row in recovery_rows:
                    recovery_data = dict(recovery_row)
                    try:
                        recovery_snapshot = json.loads(str(
                            recovery_data.get("previous_run_snapshot_json") or "{}"
                        ))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        recovery_snapshot = {}
                    row_ok = bool(
                        type(recovery_snapshot) is dict
                        and recovery_snapshot
                        and recovery_data.get("record_version")
                        == MANUAL_CHATGPT_REVIEW_RECOVERY_VERSION
                        and recovery_data.get("session_id") == session_id
                        and recovery_data.get("room_id") == room_id
                        and recovery_data.get("acknowledgement")
                        == MANUAL_CHATGPT_REVIEW_RECOVERY_ACKNOWLEDGEMENT
                        and canonical_sha256(recovery_snapshot)
                        == str(
                            recovery_data.get("previous_run_snapshot_sha256")
                            or ""
                        )
                    )
                    recovery_integrity_ok = recovery_integrity_ok and row_ok
                    recovery_records.append({
                        "id": str(recovery_data.get("id") or ""),
                        "previous_review_run_id": str(
                            recovery_data.get("previous_review_run_id") or ""
                        ),
                        "snapshot_sha256": str(
                            recovery_data.get("previous_run_snapshot_sha256") or ""
                        ),
                        "created_at": int(recovery_data.get("created_at") or 0),
                    })
                recovery_status.update({
                    "available": True,
                    "reason_code": "NO_RUNNING_REVIEW",
                    "recovery_count": len(recovery_records),
                })
                if str(review_run.get("status") or "") == "RUNNING":
                    execution_run_id = str(
                        review_run.get("provider_execution_run_id") or ""
                    )
                    recovery_execution = connection.execute(
                        "SELECT * FROM provider_execution_runs WHERE id=?",
                        (execution_run_id,),
                    ).fetchone()
                    recovery_api_review_count = int(connection.execute(
                        """SELECT COUNT(*) FROM manual_chatgpt_api_reviews
                             WHERE review_run_id=?""",
                        (str(review_run.get("id") or ""),),
                    ).fetchone()[0])
                    recovery_decision_count = int(connection.execute(
                        """SELECT COUNT(*) FROM manual_chatgpt_decisions
                             WHERE session_id=?""",
                        (session_id,),
                    ).fetchone()[0])
                    age_ms = max(
                        0,
                        now_ms() - int(review_run.get("updated_at") or 0),
                    )
                    zero_call = bool(
                        provider_calls_performed == 0
                        and int(review_run.get("completed_calls") or 0) == 0
                        and recovery_api_review_count == 0
                        and recovery_decision_count == 0
                        and recovery_execution
                        and int(recovery_execution["reserved_calls"] or 0) == 0
                        and int(recovery_execution["completed_calls"] or 0) == 0
                    )
                    eligible = bool(
                        zero_call
                        and age_ms >= MANUAL_CHATGPT_REVIEW_ORPHAN_AGE_MS
                    )
                    recovery_status.update({
                        "eligible": eligible,
                        "reason_code": (
                            "ORPHANED_ZERO_CALL_REVIEW"
                            if eligible
                            else "REVIEW_NOT_OLD_ENOUGH"
                            if zero_call
                            else "REVIEW_HAS_CALL_ACTIVITY"
                        ),
                        "age_ms": age_ms,
                    })
        previous = ""
        event_chain_ok = True
        events: list[dict[str, Any]] = []
        for event_row in event_rows:
            event = dict(event_row)
            payload = self._loads_object(event.get("payload_json"))
            basis = {
                "version": MANUAL_CHATGPT_EVENT_VERSION,
                "session_id": session_id,
                "room_id": room_id,
                "sequence_no": int(event.get("sequence_no") or 0),
                "from_state": str(event.get("from_state") or ""),
                "to_state": str(event.get("to_state") or ""),
                "event_type": str(event.get("event_type") or ""),
                "payload": payload,
                "previous_event_sha256": str(event.get("previous_event_sha256") or ""),
                "created_at": int(event.get("created_at") or 0),
            }
            expected_hash = canonical_sha256(basis)
            if (
                basis["previous_event_sha256"] != previous
                or str(event.get("event_sha256") or "") != expected_hash
            ):
                event_chain_ok = False
            previous = str(event.get("event_sha256") or "")
            events.append({
                "sequence_no": basis["sequence_no"],
                "from_state": basis["from_state"],
                "to_state": basis["to_state"],
                "event_type": basis["event_type"],
                "created_at": basis["created_at"],
                "event_sha256": str(event.get("event_sha256") or ""),
            })
        bundle_basis = copy.deepcopy(bundle)
        stored_bundle_hash = str(data.get("bundle_sha256") or "")
        declared_bundle_hash = str(bundle_basis.pop("bundle_sha256", "") or "")
        bundle_integrity_ok = bool(
            bundle_basis
            and declared_bundle_hash == stored_bundle_hash
            and canonical_sha256(bundle_basis) == stored_bundle_hash
            and canonical_sha256(_json_object(bundle.get("context")))
            == str(data.get("context_sha256") or "")
            and bundle.get("import_contract_version")
            in SUPPORTED_MANUAL_CHATGPT_IMPORT_CONTRACT_VERSIONS
        )
        result_integrity_ok = True
        if str(data.get("result_sha256") or ""):
            result_integrity_ok = canonical_sha256(result) == str(data.get("result_sha256") or "")
        event_chain_ok = bool(
            event_chain_ok
            and len(events) == int(data.get("event_sequence") or 0)
            and previous == str(data.get("event_head_sha256") or "")
        )
        integrity_ok = bool(
            bundle_integrity_ok
            and result_integrity_ok
            and event_chain_ok
            and reviews_integrity_ok
            and decision_integrity_ok
            and recovery_integrity_ok
        )
        public_bundle = bundle if integrity_ok else {}
        public_result = result if integrity_ok else {}
        return {
            "version": MANUAL_CHATGPT_SESSION_VERSION,
            "id": str(data.get("id") or ""),
            "room_id": str(data.get("room_id") or ""),
            "round_id": str(data.get("round_id") or ""),
            "mode": str(data.get("mode") or ""),
            "state": str(data.get("state") or "IMPORT_REJECTED") if integrity_ok else "IMPORT_REJECTED",
            "objective": str(data.get("objective") or "") if integrity_ok else "",
            "bundle": public_bundle,
            "bundle_sha256": stored_bundle_hash if integrity_ok else "",
            "context_sha256": str(data.get("context_sha256") or "") if integrity_ok else "",
            "task_prompt": task_prompt(public_bundle) if integrity_ok else "",
            "import_contract": import_contract(public_bundle) if integrity_ok else {},
            "result": public_result,
            "result_sha256": str(data.get("result_sha256") or "") if integrity_ok else "",
            "declared_model": str(data.get("declared_model") or "") if integrity_ok else "",
            "declared_model_trusted": False,
            "validation_issues": issues if integrity_ok else [{
                "path": "$",
                "code": "INTEGRITY_FAILED",
                "message": "协作任务完整性校验失败，内容已隐藏。",
            }],
            "repair_prompt": repair_prompt(public_bundle, [
                ImportIssue(
                    str(item.get("path") or "$"),
                    str(item.get("code") or "INVALID"),
                    str(item.get("message") or "导入不符合契约。"),
                )
                for item in issues if isinstance(item, Mapping)
            ]) if integrity_ok and issues and str(data.get("state") or "") == "IMPORT_REJECTED" else "",
            "events": events if integrity_ok else [],
            "integrity": {
                "ok": integrity_ok,
                "bundle_ok": bundle_integrity_ok,
                "result_ok": result_integrity_ok,
                "event_chain_ok": event_chain_ok,
                "api_reviews_ok": reviews_integrity_ok,
                "decision_card_ok": decision_integrity_ok,
                "review_recoveries_ok": recovery_integrity_ok,
            },
            "api_review": {
                "available": review_feature_available,
                "migration_required": not review_feature_available,
                "run_id": str(review_run.get("id") or "") if integrity_ok else "",
                "status": str(review_run.get("status") or "NOT_STARTED") if integrity_ok else "INTEGRITY_FAILED",
                "provider": str(review_run.get("provider") or "") if integrity_ok else "",
                "requested_model": str(review_run.get("requested_model") or "") if integrity_ok else "",
                "expected_calls": int(review_run.get("expected_calls") or 0) if integrity_ok else 0,
                "completed_calls": int(review_run.get("completed_calls") or 0) if integrity_ok else 0,
                "reviews": public_reviews if integrity_ok else [],
                "all_calls_are_distinct": bool(
                    integrity_ok
                    and public_reviews
                    and len(public_reviews) == len({
                        review.get("request_sha256") for review in public_reviews
                    })
                ),
            },
            "decision_card": decision_card if integrity_ok else {},
            "decision_card_sha256": decision_card_sha256 if integrity_ok else "",
            "confirmation": ({
                **confirmation,
                "confirmation_sha256": str(
                    decision_data.get("confirmation_sha256") or ""
                ) if review_feature_available and decision_row else "",
            } if integrity_ok and confirmation else {}),
            "review_recovery": ({
                **recovery_status,
                "records": recovery_records,
            } if integrity_ok else {
                **recovery_status,
                "eligible": False,
                "reason_code": "INTEGRITY_FAILED",
                "records": [],
            }),
            "next_step": self._next_step(str(data.get("state") or ""), integrity_ok),
            "created_at": int(data.get("created_at") or 0),
            "updated_at": int(data.get("updated_at") or 0),
            "frozen_at": int(data.get("frozen_at") or 0),
            "safety": {
                "execution_capability": "none",
                "live_trading_allowed": False,
                "provider_calls_performed": provider_calls_performed,
                "formal_database_migration_authorized": False,
                "user_final_decision_required": True,
            },
        }

    @staticmethod
    def _next_step(state: str, integrity_ok: bool) -> dict[str, Any]:
        if not integrity_ok:
            return {"id": "integrity_failed", "label": "完整性失败", "actionable": False}
        steps = {
            "BUNDLE_READY": ("copy_and_open_chatgpt", "复制任务包并打开 ChatGPT", True),
            "WAITING_FOR_CHATGPT": ("import_clipboard", "从剪贴板导入", True),
            "IMPORT_REJECTED": ("repair_and_reimport", "复制修复提示", True),
            "CONTEXT_STALE": ("create_new_bundle", "生成新任务包", True),
            "API_REVIEW": ("run_api_review", "运行独立 API 审查", True),
            "READY_FOR_DECISION": ("confirm_freeze", "确认并冻结", True),
            "FROZEN": ("frozen", "已冻结", False),
            "BUDGET_BLOCKED": ("budget_blocked", "审查预算已阻断", False),
            "NEEDS_USER_ACTION": ("needs_user_action", "需要用户处理", False),
        }
        step_id, label, actionable = steps.get(
            state,
            ("needs_user_action", "需要用户处理", False),
        )
        return {"id": step_id, "label": label, "actionable": actionable}


__all__ = [
    "INDEPENDENCE_CLASSIFICATIONS",
    "MANUAL_CHATGPT_API_REVIEW_VERSION",
    "MANUAL_CHATGPT_BUNDLE_VERSION",
    "MANUAL_CHATGPT_EVENT_VERSION",
    "MANUAL_CHATGPT_IMPORT_CONTRACT_VERSION",
    "MANUAL_CHATGPT_RESULT_VERSION",
    "MANUAL_CHATGPT_DECISION_CARD_VERSION",
    "MANUAL_CHATGPT_FREEZE_ACKNOWLEDGEMENT",
    "MANUAL_CHATGPT_SESSION_VERSION",
    "MANUAL_CHATGPT_STATES",
    "MODE_PRESETS",
    "ImportIssue",
    "ManualChatGPTError",
    "ManualChatGPTService",
    "build_compact_bundle",
    "build_decision_card",
    "compact_context",
    "import_contract",
    "mode_preset",
    "parse_single_json_object",
    "repair_prompt",
    "task_prompt",
    "validate_api_review",
    "validate_import_result",
]
