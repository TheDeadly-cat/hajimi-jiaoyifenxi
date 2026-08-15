from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from typing import Any


TURN_CONTRACT_VERSION = "turn_contract_v1"
CANDIDATE_LINEAGE_VERSION = "candidate_lineage_v1"
CANDIDATE_RISK_REVIEW_VERSION = "candidate_risk_review_v1"
CONFIDENCE_BOUNDARY = "模型主观置信度不是统计胜率、概率或收益承诺。"

_BLOCK_PATTERN = re.compile(
    r"<turn_contract\s*>(.*?)</turn_contract\s*>",
    re.IGNORECASE | re.DOTALL,
)
_OPEN_TAG_PATTERN = re.compile(r"<turn_contract\s*>", re.IGNORECASE)
_CLOSE_TAG_PATTERN = re.compile(r"</turn_contract\s*>", re.IGNORECASE)
_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")
_SYMBOL_PATTERN = re.compile(r"^(?:[A-Z]{1,8}\.)?[A-Z0-9][A-Z0-9.-]{0,23}$")

_TOP_LEVEL_FIELDS = {
    "version",
    "claims",
    "responds_to",
    "candidate_updates",
    "risks",
    "next_actions",
    "confidence",
}
_FORBIDDEN_KEYS = {
    "account",
    "account_id",
    "api_key",
    "execute",
    "execution",
    "order",
    "order_id",
    "payment",
    "place" + "_order",
    "private_key",
    "secret",
    "send_order",
    "tool",
    "tool_call",
    "wallet",
}

_ARRAY_LIMITS = {
    "claims": 12,
    "responds_to": 12,
    "candidate_updates": 8,
    "risks": 12,
    "next_actions": 12,
}
_CLAIM_KINDS = {"fact", "inference", "unknown"}
_EVIDENCE_TYPES = {"message", "material", "round_market_snapshot"}
_EVIDENCE_ROLES = {"support", "counter", "context"}
_RESPONSE_RELATIONS = {"supports", "challenges", "qualifies", "questions"}
_CANDIDATE_ACTIONS = {
    "propose",
    "revise",
    "support",
    "challenge",
    "select",
    "reject",
    "defer",
}
_CANDIDATE_DIRECTIONS = {"UP", "DOWN", "NEUTRAL", "FLAT", "UNSPECIFIED"}
_RISK_LEVELS = {"unknown", "low", "medium", "high", "critical"}
_RISK_STATUSES = {"open", "monitoring", "mitigated", "accepted"}
_ACTION_STATES = {"open", "in_progress", "blocked", "done"}
_CONFIDENCE_LABELS = {"unknown", "low", "medium", "high"}

_ROLE_STAGES = {"facilitate", "analysis", "debate", "plan", "risk", "decision"}
_STANCE_ROLES = {
    "facilitator": "facilitate",
    "sector": "analysis",
    "fundamental": "analysis",
    "technical": "analysis",
    "sentiment": "analysis",
    "data_guardian": "analysis",
    "bull": "debate",
    "bear": "debate",
    "paper_trader": "plan",
    "risk": "risk",
    "portfolio_manager": "decision",
}
_CAPABILITY_ROLES = {
    "facilitation": "facilitate",
    "evidence_review": "analysis",
    "storage_sector_analysis": "analysis",
    "fundamental_analysis": "analysis",
    "technical_analysis": "analysis",
    "sentiment_analysis": "analysis",
    "data_quality_review": "analysis",
    "bull_case": "debate",
    "bear_case": "debate",
    "critical_review": "debate",
    "simulation_planning": "plan",
    "risk_review": "risk",
    "decision_synthesis": "decision",
}


def candidate_risk_review_protocol_required(
    workflow_policy: Mapping[str, Any] | None,
    members: Iterable[Mapping[str, Any]] | None,
) -> bool:
    """Enable exact-version risk review only for an explicit risk workflow.

    General collaboration rooms keep the auditable turn contract without being
    forced into a trading-company committee shape. A room opts into this kernel
    protocol by requiring the ``risk`` stage or a dedicated ``risk_review``
    coverage item and by freezing at least one matching risk member.
    """

    policy = workflow_policy if isinstance(workflow_policy, Mapping) else {}
    stage_coverage = policy.get("minimum_stage_coverage")
    risk_stage_required = bool(
        isinstance(stage_coverage, Mapping)
        and isinstance(stage_coverage.get("risk"), int)
        and not isinstance(stage_coverage.get("risk"), bool)
        and int(stage_coverage.get("risk") or 0) > 0
    )
    requirements = policy.get("required_coverage")
    risk_requirement = any(
        isinstance(requirement, Mapping)
        and str(requirement.get("id") or "").strip().lower() == "risk_review"
        and isinstance(requirement.get("minimum"), int)
        and not isinstance(requirement.get("minimum"), bool)
        and int(requirement.get("minimum") or 0) > 0
        for requirement in (
            requirements if isinstance(requirements, list) else []
        )
    )
    if not (risk_stage_required or risk_requirement):
        return False
    return any(
        "risk" in _role_profiles(member)
        for member in (members or [])
        if isinstance(member, Mapping)
    )


class _DuplicateJsonKey(ValueError):
    pass


def validate_turn_contract_payload(
    payload: Any,
    *,
    member: Mapping[str, Any] | None = None,
    allowed_message_ids: Iterable[str] = (),
    allowed_material_ids: Iterable[str] = (),
    allowed_market_snapshot_id: str = "",
    prior_ai_message_ids: Iterable[str] | None = None,
    require_all_input_fields: bool = False,
) -> dict[str, Any]:
    """Normalize and qualify one already-decoded turn-contract payload.

    Transport parsers own framing concerns such as XML tags, JSON decoding and
    duplicate-key detection.  This function is the single semantic validator
    for both the historical XML block and newer structured envelopes.  Legacy
    XML callers deliberately keep ``require_all_input_fields=False`` so the
    migration cannot silently tighten a paused historical round.
    """

    issues: list[dict[str, str]] = []
    normalized: dict[str, Any] | None = None
    if not isinstance(payload, Mapping):
        _issue(
            issues,
            "TURN_CONTRACT_OBJECT_REQUIRED",
            "turn_contract",
            "发言合同根节点必须是 JSON 对象。",
        )
    else:
        clean_payload = _clean_json_keys(dict(payload), issues, "turn_contract")
        if require_all_input_fields:
            for field in sorted(_TOP_LEVEL_FIELDS - set(clean_payload)):
                _issue(
                    issues,
                    "TURN_CONTRACT_FIELD_MISSING",
                    f"turn_contract.{field}",
                    f"发言合同缺少字段 {field}。",
                )
        _find_forbidden_keys(clean_payload, issues)
        normalized = _normalize_contract(
            clean_payload,
            issues,
            allowed_message_ids={
                str(item) for item in allowed_message_ids if str(item)
            },
            allowed_material_ids={
                str(item) for item in allowed_material_ids if str(item)
            },
            allowed_market_snapshot_id=str(
                allowed_market_snapshot_id or ""
            ).strip(),
        )
        _validate_response_graph(
            normalized,
            prior_ai_message_ids=prior_ai_message_ids,
            issues=issues,
        )
        _validate_professional_deliverable(normalized, member or {}, issues)

    return {
        "version": TURN_CONTRACT_VERSION,
        "contract": normalized,
        "role_profiles": _role_profiles(member or {}),
        "qualified": normalized is not None and not issues,
        "issues": issues,
        "confidence_is_not_win_rate": True,
        "confidence_boundary": CONFIDENCE_BOUNDARY,
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
    }


def extract_turn_contract(
    content: Any,
    *,
    member: Mapping[str, Any] | None = None,
    allowed_message_ids: Iterable[str] = (),
    allowed_material_ids: Iterable[str] = (),
    allowed_market_snapshot_id: str = "",
    prior_ai_message_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Extract, normalize, and qualify one hidden turn contract.

    The returned visible text never contains a complete ``turn_contract`` block.
    Parsing and professional-deliverable validation are deliberately fail closed:
    any structural, reference, or role issue sets ``qualified`` to ``False``.
    """

    raw_content = str(content or "")
    issues: list[dict[str, str]] = []
    matches = list(_BLOCK_PATTERN.finditer(raw_content))
    open_count = len(_OPEN_TAG_PATTERN.findall(raw_content))
    close_count = len(_CLOSE_TAG_PATTERN.findall(raw_content))
    visible_content = _visible_text(raw_content, matches)

    if len(raw_content) > 60_000:
        _issue(issues, "CONTENT_TOO_LONG", "content", "群聊消息与隐藏合同合计不能超过 60000 个字符。")
    if open_count != close_count or len(matches) != open_count:
        _issue(issues, "TURN_CONTRACT_TAG_MALFORMED", "content", "turn_contract 标签缺失、嵌套或不完整。")
    if not matches:
        _issue(issues, "TURN_CONTRACT_MISSING", "content", "缺少唯一的 turn_contract 隐藏块。")
    elif len(matches) != 1:
        _issue(issues, "TURN_CONTRACT_MULTIPLE", "content", "每条发言只能包含一个 turn_contract 隐藏块。")
    if not visible_content:
        _issue(issues, "VISIBLE_CONTENT_REQUIRED", "content", "隐藏合同之外必须保留可展示的群聊正文。")
    elif len(visible_content) > 8_000:
        _issue(issues, "VISIBLE_CONTENT_TOO_LONG", "content", "可展示正文不能超过 8000 个字符。")

    validation: dict[str, Any] | None = None
    if len(matches) == 1 and open_count == 1 and close_count == 1:
        payload_text = matches[0].group(1).strip()
        if len(payload_text) > 32_000:
            _issue(issues, "TURN_CONTRACT_TOO_LONG", "turn_contract", "隐藏合同不能超过 32000 个字符。")
        else:
            payload = _load_json_object(payload_text, issues)
            if payload is not None:
                validation = validate_turn_contract_payload(
                    payload,
                    member=member,
                    allowed_message_ids=allowed_message_ids,
                    allowed_material_ids=allowed_material_ids,
                    allowed_market_snapshot_id=allowed_market_snapshot_id,
                    prior_ai_message_ids=prior_ai_message_ids,
                )
                for issue in validation["issues"]:
                    if issue not in issues:
                        issues.append(issue)

    normalized = validation.get("contract") if validation is not None else None
    return {
        "version": TURN_CONTRACT_VERSION,
        "found": len(matches) == 1 and open_count == 1 and close_count == 1,
        "visible_content": visible_content,
        "contract": normalized,
        "role_profiles": (
            validation.get("role_profiles")
            if validation is not None
            else _role_profiles(member or {})
        ),
        "qualified": normalized is not None and not issues,
        "issues": issues,
        "confidence_is_not_win_rate": True,
        "confidence_boundary": CONFIDENCE_BOUNDARY,
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
    }


def validate_stored_turn_contract(
    value: Any,
    *,
    member: Mapping[str, Any] | None = None,
    allowed_message_ids: Iterable[str] = (),
    allowed_material_ids: Iterable[str] = (),
    allowed_market_snapshot_id: str = "",
    prior_ai_message_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Revalidate a persisted normalized contract against frozen inputs.

    Stored qualification flags are not authoritative on their own: this path
    rejects duplicate JSON keys, schema drift, changed references, role changes,
    and any mismatch between the canonical normalized form and stored content.
    """

    issues: list[dict[str, str]] = []
    if isinstance(value, str):
        payload = _load_json_object(value, issues)
    elif isinstance(value, Mapping):
        payload = _clean_json_keys(dict(value), issues, "turn_contract")
    else:
        payload = None
        _issue(
            issues,
            "STORED_TURN_CONTRACT_INVALID",
            "turn_contract",
            "持久化发言合同必须是 JSON 对象。",
        )

    normalized: dict[str, Any] | None = None
    stored_fields = _TOP_LEVEL_FIELDS | {
        "confidence_is_not_win_rate",
        "execution_capability",
        "live_trading_allowed",
    }
    if payload is not None:
        _reject_unknown_fields(payload, stored_fields, "turn_contract", issues)
        missing = sorted(stored_fields - set(payload))
        for field in missing:
            _issue(
                issues,
                "STORED_TURN_CONTRACT_FIELD_MISSING",
                f"turn_contract.{field}",
                f"持久化发言合同缺少字段 {field}。",
            )
        _find_forbidden_keys(payload, issues)
        source_payload = {field: payload.get(field) for field in _TOP_LEVEL_FIELDS}
        validation = validate_turn_contract_payload(
            source_payload,
            member=member,
            allowed_message_ids=allowed_message_ids,
            allowed_material_ids=allowed_material_ids,
            allowed_market_snapshot_id=allowed_market_snapshot_id,
            prior_ai_message_ids=prior_ai_message_ids,
        )
        normalized = validation.get("contract")
        for issue in validation["issues"]:
            if issue not in issues:
                issues.append(issue)
        if payload.get("confidence_is_not_win_rate") is not True:
            _issue(
                issues,
                "CONFIDENCE_BOUNDARY_INVALID",
                "turn_contract.confidence_is_not_win_rate",
                "持久化合同必须保留主观置信度边界。",
            )
        if payload.get("execution_capability") != "none":
            _issue(
                issues,
                "EXECUTION_BOUNDARY_INVALID",
                "turn_contract.execution_capability",
                "持久化合同不得具有执行能力。",
            )
        if payload.get("live_trading_allowed") is not False:
            _issue(
                issues,
                "LIVE_TRADING_BOUNDARY_INVALID",
                "turn_contract.live_trading_allowed",
                "持久化合同不得允许真实交易。",
            )
        if normalized != payload:
            _issue(
                issues,
                "STORED_TURN_CONTRACT_NOT_CANONICAL",
                "turn_contract",
                "持久化发言合同与当前规范化结果不一致。",
            )

    return {
        "version": TURN_CONTRACT_VERSION,
        "contract": normalized,
        "qualified": normalized is not None and not issues,
        "issues": issues,
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
    }


def _visible_text(raw_content: str, matches: list[re.Match[str]]) -> str:
    visible = _BLOCK_PATTERN.sub("", raw_content)
    # If a tag is malformed, hide everything following an unmatched opening tag
    # so internal JSON or instructions cannot leak into the chat timeline.
    unmatched_open = _OPEN_TAG_PATTERN.search(visible)
    if unmatched_open:
        visible = visible[: unmatched_open.start()]
    visible = _OPEN_TAG_PATTERN.sub("", visible)
    visible = _CLOSE_TAG_PATTERN.sub("", visible)
    visible = re.sub(r"[ \t]+\n", "\n", visible)
    visible = re.sub(r"\n{3,}", "\n\n", visible)
    return visible.strip()


def _load_json_object(payload_text: str, issues: list[dict[str, str]]) -> dict[str, Any] | None:
    if not payload_text:
        _issue(issues, "TURN_CONTRACT_EMPTY", "turn_contract", "隐藏合同不能为空。")
        return None

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise _DuplicateJsonKey(str(key))
            value[key] = item
        return value

    try:
        parsed = json.loads(payload_text, object_pairs_hook=unique_object)
    except _DuplicateJsonKey as exc:
        _issue(issues, "JSON_DUPLICATE_KEY", "turn_contract", f"JSON 包含重复字段：{exc}。")
        return None
    except (TypeError, ValueError, json.JSONDecodeError):
        _issue(issues, "TURN_CONTRACT_JSON_INVALID", "turn_contract", "隐藏合同不是有效 JSON。")
        return None
    if not isinstance(parsed, dict):
        _issue(issues, "TURN_CONTRACT_OBJECT_REQUIRED", "turn_contract", "隐藏合同根节点必须是 JSON 对象。")
        return None
    return parsed


def _find_forbidden_keys(value: Any, issues: list[dict[str, str]], path: str = "turn_contract") -> None:
    if isinstance(value, dict):
        for raw_key, nested in value.items():
            key = str(raw_key).strip().lower()
            child_path = f"{path}.{raw_key}"
            if key in _FORBIDDEN_KEYS:
                _issue(
                    issues,
                    "EXECUTION_FIELD_FORBIDDEN",
                    child_path,
                    "发言合同不能携带账户、密钥、工具调用、支付或订单执行字段。",
                )
            _find_forbidden_keys(nested, issues, child_path)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _find_forbidden_keys(nested, issues, f"{path}[{index}]")


def _clean_json_keys(
    value: Any,
    issues: list[dict[str, str]],
    path: str,
) -> Any:
    """Reject non-string mapping keys before deterministic schema traversal."""

    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                _issue(
                    issues,
                    "JSON_KEY_STRING_REQUIRED",
                    path,
                    "JSON 对象字段名必须是字符串。",
                )
                continue
            clean[key] = _clean_json_keys(nested, issues, f"{path}.{key}")
        return clean
    if isinstance(value, list):
        return [
            _clean_json_keys(nested, issues, f"{path}[{index}]")
            for index, nested in enumerate(value)
        ]
    return value


def _normalize_contract(
    payload: dict[str, Any],
    issues: list[dict[str, str]],
    *,
    allowed_message_ids: set[str],
    allowed_material_ids: set[str],
    allowed_market_snapshot_id: str,
) -> dict[str, Any]:
    _reject_unknown_fields(payload, _TOP_LEVEL_FIELDS, "turn_contract", issues)
    version = str(payload.get("version") or "").strip()
    if version != TURN_CONTRACT_VERSION:
        _issue(
            issues,
            "TURN_CONTRACT_VERSION_INVALID",
            "turn_contract.version",
            f"version 必须是 {TURN_CONTRACT_VERSION}。",
        )

    claims = _normalize_array(
        payload.get("claims"),
        "claims",
        issues,
        lambda item, path: _normalize_claim(
            item,
            path,
            issues,
            allowed_message_ids,
            allowed_material_ids,
            allowed_market_snapshot_id,
        ),
    )
    _validate_unique_ids(claims, "turn_contract.claims", "CLAIM_ID_DUPLICATE", issues)

    responds_to = _normalize_array(
        payload.get("responds_to"),
        "responds_to",
        issues,
        lambda item, path: _normalize_response(item, path, issues, allowed_message_ids),
    )
    candidate_updates = _normalize_array(
        payload.get("candidate_updates"),
        "candidate_updates",
        issues,
        lambda item, path: _normalize_candidate(
            item,
            path,
            issues,
            allowed_message_ids,
            allowed_material_ids,
            allowed_market_snapshot_id,
        ),
    )
    _validate_unique_ids(
        candidate_updates,
        "turn_contract.candidate_updates",
        "CANDIDATE_ID_DUPLICATE",
        issues,
    )
    risks = _normalize_array(
        payload.get("risks"),
        "risks",
        issues,
        lambda item, path: _normalize_risk(
            item,
            path,
            issues,
            allowed_message_ids,
            allowed_material_ids,
            allowed_market_snapshot_id,
        ),
    )
    _validate_unique_ids(risks, "turn_contract.risks", "RISK_ID_DUPLICATE", issues)
    next_actions = _normalize_array(
        payload.get("next_actions"),
        "next_actions",
        issues,
        lambda item, path: _normalize_next_action(
            item,
            path,
            issues,
            allowed_message_ids,
            allowed_material_ids,
            allowed_market_snapshot_id,
        ),
    )
    _validate_unique_ids(
        next_actions,
        "turn_contract.next_actions",
        "ACTION_ID_DUPLICATE",
        issues,
    )
    confidence = _normalize_confidence(payload.get("confidence"), issues)
    return {
        "version": TURN_CONTRACT_VERSION,
        "claims": claims,
        "responds_to": responds_to,
        "candidate_updates": candidate_updates,
        "risks": risks,
        "next_actions": next_actions,
        "confidence": confidence,
        "confidence_is_not_win_rate": True,
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


def _normalize_array(
    value: Any,
    field: str,
    issues: list[dict[str, str]],
    normalizer: Any,
) -> list[dict[str, Any]]:
    path = f"turn_contract.{field}"
    if value is None:
        return []
    if not isinstance(value, list):
        _issue(issues, "ARRAY_REQUIRED", path, f"{field} 必须是数组。")
        return []
    maximum = _ARRAY_LIMITS[field]
    if len(value) > maximum:
        _issue(issues, "ARRAY_LIMIT_EXCEEDED", path, f"{field} 最多包含 {maximum} 项。")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(value[:maximum]):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            _issue(issues, "OBJECT_REQUIRED", item_path, "数组成员必须是 JSON 对象。")
            continue
        clean = normalizer(item, item_path)
        if clean is not None:
            normalized.append(clean)
    return normalized


def _normalize_claim(
    item: dict[str, Any],
    path: str,
    issues: list[dict[str, str]],
    allowed_message_ids: set[str],
    allowed_material_ids: set[str],
    allowed_market_snapshot_id: str,
) -> dict[str, Any]:
    _reject_unknown_fields(item, {"id", "kind", "text", "as_of", "evidence"}, path, issues)
    claim_id = _clean_id(item.get("id"), f"{path}.id", issues)
    kind = _enum(item.get("kind"), _CLAIM_KINDS, f"{path}.kind", issues, "CLAIM_KIND_INVALID")
    text = _text(item.get("text"), f"{path}.text", issues, minimum=1, maximum=800)
    as_of = _text(item.get("as_of"), f"{path}.as_of", issues, minimum=0, maximum=80)
    evidence = _normalize_evidence(
        item.get("evidence"),
        f"{path}.evidence",
        issues,
        allowed_message_ids,
        allowed_material_ids,
        allowed_market_snapshot_id,
    )
    return {"id": claim_id, "kind": kind, "text": text, "as_of": as_of, "evidence": evidence}


def _normalize_response(
    item: dict[str, Any],
    path: str,
    issues: list[dict[str, str]],
    allowed_message_ids: set[str],
) -> dict[str, Any]:
    _reject_unknown_fields(item, {"type", "id", "relation", "reason"}, path, issues)
    source_type = str(item.get("type") or "").strip().lower()
    if source_type != "message":
        _issue(issues, "RESPONSE_TYPE_INVALID", f"{path}.type", "responds_to 只允许引用 message。")
    source_id = _text(item.get("id"), f"{path}.id", issues, minimum=1, maximum=100)
    if source_id and source_id not in allowed_message_ids:
        _issue(issues, "REFERENCE_NOT_ALLOWED", f"{path}.id", "responds_to 引用了未授权消息。")
    relation = _enum(
        item.get("relation"),
        _RESPONSE_RELATIONS,
        f"{path}.relation",
        issues,
        "RESPONSE_RELATION_INVALID",
    )
    reason = _text(item.get("reason"), f"{path}.reason", issues, minimum=1, maximum=500)
    return {"type": "message", "id": source_id, "relation": relation, "reason": reason}


def _validate_response_graph(
    contract: Mapping[str, Any],
    *,
    prior_ai_message_ids: Iterable[str] | None,
    issues: list[dict[str, str]],
) -> None:
    """Require auditable AI-to-AI response edges when the caller supplies a prefix.

    ``None`` preserves the legacy contract behavior.  An explicitly empty
    iterable represents the first AI turn and therefore does not require a
    response edge.  Once at least one prior AI message exists, every declared
    response must target that frozen prefix and at least one valid relation is
    required.
    """

    if prior_ai_message_ids is None:
        return
    prior_ids = {
        str(message_id).strip()
        for message_id in prior_ai_message_ids
        if str(message_id).strip()
    }
    if not prior_ids:
        return

    responses = contract.get("responds_to")
    response_rows = responses if isinstance(responses, list) else []
    valid_response_found = False
    for index, response in enumerate(response_rows):
        if not isinstance(response, Mapping):
            continue
        message_id = str(response.get("id") or "").strip()
        relation = str(response.get("relation") or "").strip()
        if message_id not in prior_ids:
            _issue(
                issues,
                "RESPONSE_TARGET_NOT_PRIOR_AI",
                f"turn_contract.responds_to[{index}].id",
                "responds_to 只能指向本轮冻结前缀中的既有 AI 消息。",
            )
            continue
        if relation in _RESPONSE_RELATIONS:
            valid_response_found = True

    if not valid_response_found:
        _issue(
            issues,
            "PRIOR_AI_RESPONSE_REQUIRED",
            "turn_contract.responds_to",
            "除本轮首位 AI 外，每条正式发言必须回应至少一条本轮既有 AI 消息。",
        )


def _normalize_candidate(
    item: dict[str, Any],
    path: str,
    issues: list[dict[str, str]],
    allowed_message_ids: set[str],
    allowed_material_ids: set[str],
    allowed_market_snapshot_id: str,
) -> dict[str, Any]:
    fields = {
        "id",
        "title",
        "action",
        "symbol",
        "direction",
        "horizon_days",
        "thesis",
        "invalidation",
        "evidence",
    }
    _reject_unknown_fields(item, fields, path, issues)
    candidate_id = _clean_id(item.get("id"), f"{path}.id", issues)
    title = _text(item.get("title"), f"{path}.title", issues, minimum=1, maximum=180)
    action = _enum(
        item.get("action"),
        _CANDIDATE_ACTIONS,
        f"{path}.action",
        issues,
        "CANDIDATE_ACTION_INVALID",
    )
    symbol = str(item.get("symbol") or "").strip().upper()
    if symbol and not _SYMBOL_PATTERN.fullmatch(symbol):
        _issue(issues, "CANDIDATE_SYMBOL_INVALID", f"{path}.symbol", "symbol 格式无效。")
    raw_direction = str(item.get("direction") or "UNSPECIFIED").strip().upper()
    direction = _enum(
        raw_direction,
        _CANDIDATE_DIRECTIONS,
        f"{path}.direction",
        issues,
        "CANDIDATE_DIRECTION_INVALID",
    )
    horizon_days = _optional_int(
        item.get("horizon_days"),
        f"{path}.horizon_days",
        issues,
        minimum=1,
        maximum=3650,
    )
    thesis = _text(item.get("thesis"), f"{path}.thesis", issues, minimum=1, maximum=1200)
    invalidation = _text(item.get("invalidation"), f"{path}.invalidation", issues, minimum=0, maximum=1000)
    evidence = _normalize_evidence(
        item.get("evidence"),
        f"{path}.evidence",
        issues,
        allowed_message_ids,
        allowed_material_ids,
        allowed_market_snapshot_id,
    )
    return {
        "id": candidate_id,
        "title": title,
        "action": action,
        "symbol": symbol,
        "direction": direction,
        "horizon_days": horizon_days,
        "thesis": thesis,
        "invalidation": invalidation,
        "evidence": evidence,
    }


def _normalize_risk(
    item: dict[str, Any],
    path: str,
    issues: list[dict[str, str]],
    allowed_message_ids: set[str],
    allowed_material_ids: set[str],
    allowed_market_snapshot_id: str,
) -> dict[str, Any]:
    fields = {"id", "text", "severity", "status", "trigger", "mitigation", "blocking", "evidence"}
    _reject_unknown_fields(item, fields, path, issues)
    risk_id = _clean_id(item.get("id"), f"{path}.id", issues)
    text = _text(item.get("text"), f"{path}.text", issues, minimum=1, maximum=800)
    severity = _enum(item.get("severity"), _RISK_LEVELS, f"{path}.severity", issues, "RISK_SEVERITY_INVALID")
    status = _enum(item.get("status"), _RISK_STATUSES, f"{path}.status", issues, "RISK_STATUS_INVALID")
    trigger = _text(item.get("trigger"), f"{path}.trigger", issues, minimum=0, maximum=500)
    mitigation = _text(item.get("mitigation"), f"{path}.mitigation", issues, minimum=0, maximum=800)
    blocking_value = item.get("blocking")
    if not isinstance(blocking_value, bool):
        _issue(issues, "BOOLEAN_REQUIRED", f"{path}.blocking", "blocking 必须是布尔值。")
        blocking_value = True
    evidence = _normalize_evidence(
        item.get("evidence"),
        f"{path}.evidence",
        issues,
        allowed_message_ids,
        allowed_material_ids,
        allowed_market_snapshot_id,
    )
    return {
        "id": risk_id,
        "text": text,
        "severity": severity,
        "status": status,
        "trigger": trigger,
        "mitigation": mitigation,
        "blocking": blocking_value,
        "evidence": evidence,
    }


def _normalize_next_action(
    item: dict[str, Any],
    path: str,
    issues: list[dict[str, str]],
    allowed_message_ids: set[str],
    allowed_material_ids: set[str],
    allowed_market_snapshot_id: str,
) -> dict[str, Any]:
    fields = {"id", "text", "owner", "state", "due", "evidence"}
    _reject_unknown_fields(item, fields, path, issues)
    action_id = _clean_id(item.get("id"), f"{path}.id", issues)
    text = _text(item.get("text"), f"{path}.text", issues, minimum=1, maximum=600)
    owner = _text(item.get("owner"), f"{path}.owner", issues, minimum=1, maximum=120)
    state = _enum(item.get("state"), _ACTION_STATES, f"{path}.state", issues, "ACTION_STATE_INVALID")
    due = _text(item.get("due"), f"{path}.due", issues, minimum=0, maximum=80)
    evidence = _normalize_evidence(
        item.get("evidence"),
        f"{path}.evidence",
        issues,
        allowed_message_ids,
        allowed_material_ids,
        allowed_market_snapshot_id,
    )
    return {
        "id": action_id,
        "text": text,
        "owner": owner,
        "state": state,
        "due": due,
        "evidence": evidence,
    }


def _normalize_evidence(
    value: Any,
    path: str,
    issues: list[dict[str, str]],
    allowed_message_ids: set[str],
    allowed_material_ids: set[str],
    allowed_market_snapshot_id: str,
) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        _issue(issues, "ARRAY_REQUIRED", path, "evidence 必须是数组。")
        return []
    if len(value) > 8:
        _issue(issues, "EVIDENCE_LIMIT_EXCEEDED", path, "每项最多引用 8 条证据。")
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(value[:8]):
        item_path = f"{path}[{index}]"
        if not isinstance(raw, dict):
            _issue(issues, "OBJECT_REQUIRED", item_path, "证据引用必须是 JSON 对象。")
            continue
        _reject_unknown_fields(raw, {"type", "id", "role"}, item_path, issues)
        source_type = str(raw.get("type") or "").strip().lower()
        if source_type not in _EVIDENCE_TYPES:
            _issue(
                issues,
                "EVIDENCE_TYPE_INVALID",
                f"{item_path}.type",
                "证据类型只能是 message、material 或 round_market_snapshot。",
            )
            continue
        source_id = _text(raw.get("id"), f"{item_path}.id", issues, minimum=1, maximum=100)
        role = _enum(raw.get("role"), _EVIDENCE_ROLES, f"{item_path}.role", issues, "EVIDENCE_ROLE_INVALID")
        allowed = (
            allowed_message_ids
            if source_type == "message"
            else allowed_material_ids
            if source_type == "material"
            else {allowed_market_snapshot_id} if allowed_market_snapshot_id else set()
        )
        if source_id not in allowed:
            _issue(
                issues,
                "REFERENCE_NOT_ALLOWED",
                f"{item_path}.id",
                "证据引用不在本轮允许的消息、资料或唯一冻结市场快照 ID 中。",
            )
            continue
        key = (source_type, source_id)
        if key in seen:
            _issue(issues, "EVIDENCE_DUPLICATE", item_path, "同一项中不能重复引用相同证据。")
            continue
        seen.add(key)
        normalized.append({"type": source_type, "id": source_id, "role": role})
    return normalized


def _normalize_confidence(value: Any, issues: list[dict[str, str]]) -> dict[str, Any]:
    path = "turn_contract.confidence"
    if value is None:
        return {"kind": "model_subjective", "value": None, "label": "unknown", "basis": ""}
    if not isinstance(value, dict):
        _issue(issues, "CONFIDENCE_OBJECT_REQUIRED", path, "confidence 必须是 JSON 对象或 null。")
        return {"kind": "model_subjective", "value": None, "label": "unknown", "basis": ""}
    _reject_unknown_fields(value, {"kind", "value", "label", "basis"}, path, issues)
    kind = str(value.get("kind") or "").strip().lower()
    if kind != "model_subjective":
        _issue(
            issues,
            "CONFIDENCE_KIND_INVALID",
            f"{path}.kind",
            "confidence.kind 只能是 model_subjective；不得填写胜率或统计概率。",
        )
        kind = "model_subjective"
    raw_value = value.get("value")
    number: float | None = None
    if raw_value is not None:
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            _issue(issues, "CONFIDENCE_VALUE_INVALID", f"{path}.value", "主观置信度必须是 0 到 100 的数字或 null。")
        else:
            candidate = float(raw_value)
            if not math.isfinite(candidate) or not 0 <= candidate <= 100:
                _issue(issues, "CONFIDENCE_VALUE_INVALID", f"{path}.value", "主观置信度必须是 0 到 100 的有限数字。")
            else:
                number = round(candidate, 4)
    label = _enum(value.get("label"), _CONFIDENCE_LABELS, f"{path}.label", issues, "CONFIDENCE_LABEL_INVALID")
    basis = _text(value.get("basis"), f"{path}.basis", issues, minimum=0, maximum=500)
    if number is not None and not basis:
        _issue(issues, "CONFIDENCE_BASIS_REQUIRED", f"{path}.basis", "填写主观置信度数值时必须说明依据。")
    return {"kind": kind, "value": number, "label": label, "basis": basis}


def _validate_professional_deliverable(
    contract: dict[str, Any],
    member: Mapping[str, Any],
    issues: list[dict[str, str]],
) -> None:
    profiles = _role_profiles(member)
    if not profiles:
        if not any(contract.get(field) for field in _ARRAY_LIMITS):
            _issue(
                issues,
                "GENERIC_DELIVERABLE_REQUIRED",
                "turn_contract",
                "未识别专业角色时，仍需至少提交一项主张、回应、候选、风险或下一步。",
            )
        return

    claims = contract["claims"]
    responses = contract["responds_to"]
    candidates = contract["candidate_updates"]
    risks = contract["risks"]
    actions = contract["next_actions"]

    for profile in profiles:
        if profile == "analysis":
            grounded = [
                claim
                for claim in claims
                if claim["kind"] in {"fact", "inference"} and claim["evidence"]
            ]
            if not grounded:
                _issue(
                    issues,
                    "ANALYSIS_GROUNDED_CLAIM_REQUIRED",
                    "turn_contract.claims",
                    "分析角色至少需要一项带允许证据引用的事实或推断。",
                )
            if not any(claim["kind"] == "fact" and claim["as_of"] for claim in grounded):
                _issue(
                    issues,
                    "ANALYSIS_AS_OF_REQUIRED",
                    "turn_contract.claims",
                    "分析角色至少需要一项带数据时间的事实主张。",
                )
        elif profile == "debate":
            if not any(response["relation"] in {"challenges", "qualifies", "questions"} for response in responses):
                _issue(
                    issues,
                    "DEBATE_TARGET_REQUIRED",
                    "turn_contract.responds_to",
                    "辩论角色必须指向并质疑、限定或追问一条允许的既有消息。",
                )
            if not (claims or candidates or risks):
                _issue(
                    issues,
                    "DEBATE_SUBSTANCE_REQUIRED",
                    "turn_contract",
                    "辩论角色必须提交主张、候选修订或风险，而不能只表示同意或反对。",
                )
            if not any(candidate["invalidation"] for candidate in candidates) and not any(risk["trigger"] for risk in risks):
                _issue(
                    issues,
                    "DEBATE_FALSIFIABILITY_REQUIRED",
                    "turn_contract",
                    "辩论角色必须提供候选失效条件或风险触发信号。",
                )
        elif profile == "risk":
            if not responses:
                _issue(
                    issues,
                    "RISK_TARGET_REQUIRED",
                    "turn_contract.responds_to",
                    "风控角色必须引用其正在复核的既有方案消息。",
                )
            complete_risks = [risk for risk in risks if risk["trigger"]]
            if not complete_risks:
                _issue(
                    issues,
                    "RISK_REGISTER_REQUIRED",
                    "turn_contract.risks",
                    "风控角色至少需要一项带触发信号的结构化风险。",
                )
            if complete_risks and not any(risk["mitigation"] for risk in complete_risks) and not actions:
                _issue(
                    issues,
                    "RISK_RESPONSE_REQUIRED",
                    "turn_contract.risks",
                    "风控角色必须提供缓解动作或明确下一步。",
                )
        elif profile == "plan":
            proposed = [candidate for candidate in candidates if candidate["action"] in {"propose", "revise"}]
            if not any(
                candidate["thesis"] and candidate["invalidation"] and candidate["evidence"]
                for candidate in proposed
            ):
                _issue(
                    issues,
                    "PLAN_CANDIDATE_REQUIRED",
                    "turn_contract.candidate_updates",
                    "方案角色必须提交带依据和失效条件的候选方案。",
                )
            if not actions:
                _issue(
                    issues,
                    "PLAN_NEXT_ACTION_REQUIRED",
                    "turn_contract.next_actions",
                    "方案角色必须提交至少一个可验证下一步。",
                )
        elif profile == "decision":
            candidate_ids = {candidate["id"] for candidate in candidates if candidate["id"]}
            selected = [candidate for candidate in candidates if candidate["action"] == "select"]
            deferred = [candidate for candidate in candidates if candidate["action"] == "defer"]
            if len(candidate_ids) < 2:
                _issue(
                    issues,
                    "DECISION_COMPARISON_REQUIRED",
                    "turn_contract.candidate_updates",
                    "决策角色必须比较至少两个不同候选。",
                )
            if not (
                (len(selected) == 1 and len(deferred) == 0)
                or (len(selected) == 0 and len(deferred) == 1)
            ):
                _issue(
                    issues,
                    "DECISION_SELECTION_REQUIRED",
                    "turn_contract.candidate_updates",
                    "决策角色必须且只能选择一个候选，或明确暂缓一次。",
                )
            if selected and not selected[0]["evidence"]:
                _issue(
                    issues,
                    "DECISION_EVIDENCE_REQUIRED",
                    "turn_contract.candidate_updates",
                    "被选择候选必须引用本轮允许的证据。",
                )
            if not risks:
                _issue(
                    issues,
                    "DECISION_RISK_REQUIRED",
                    "turn_contract.risks",
                    "决策角色必须保留至少一项风险或阻断条件。",
                )
        elif profile == "facilitate":
            if not claims:
                _issue(
                    issues,
                    "FACILITATOR_FRAMING_REQUIRED",
                    "turn_contract.claims",
                    "主持角色必须记录至少一项目标、约束或待验证问题。",
                )
            if not actions:
                _issue(
                    issues,
                    "FACILITATOR_NEXT_ACTION_REQUIRED",
                    "turn_contract.next_actions",
                    "主持角色必须安排至少一个下一步。",
                )


def _role_profiles(member: Mapping[str, Any]) -> list[str]:
    profiles: list[str] = []

    def add(profile: str | None) -> None:
        if profile and profile not in profiles:
            profiles.append(profile)

    stage = str(member.get("workflow_stage") or "").strip().lower()
    if stage in _ROLE_STAGES:
        add(stage)
    add(_STANCE_ROLES.get(str(member.get("stance") or "").strip().lower()))
    raw_capabilities = member.get("capabilities")
    capabilities = raw_capabilities if isinstance(raw_capabilities, list) else []
    for capability in capabilities:
        add(_CAPABILITY_ROLES.get(str(capability or "").strip().lower()))
    return profiles


def _reject_unknown_fields(
    value: Mapping[str, Any],
    allowed: set[str],
    path: str,
    issues: list[dict[str, str]],
) -> None:
    for key in sorted(set(value) - allowed):
        _issue(issues, "UNKNOWN_FIELD", f"{path}.{key}", f"不支持字段 {key}。")


def _validate_unique_ids(
    rows: list[dict[str, Any]],
    path: str,
    code: str,
    issues: list[dict[str, str]],
) -> None:
    ids = [str(row.get("id") or "") for row in rows]
    if len(set(ids)) != len(ids):
        _issue(issues, code, path, "同一发言合同中的 id 不能重复。")


def _clean_id(value: Any, path: str, issues: list[dict[str, str]]) -> str:
    clean = str(value or "").strip()
    if not _ID_PATTERN.fullmatch(clean):
        _issue(issues, "ID_INVALID", path, "id 必须以英文字母开头，仅包含字母、数字、下划线或短横线，最长 80。")
    return clean[:80]


def _text(
    value: Any,
    path: str,
    issues: list[dict[str, str]],
    *,
    minimum: int,
    maximum: int,
) -> str:
    if value is not None and not isinstance(value, str):
        _issue(issues, "STRING_REQUIRED", path, "该字段必须是字符串。")
    clean = str(value or "").strip()
    if len(clean) < minimum:
        _issue(issues, "TEXT_REQUIRED", path, "该字段不能为空。")
    if len(clean) > maximum:
        _issue(issues, "TEXT_TOO_LONG", path, f"该字段最多 {maximum} 个字符。")
    return clean[:maximum]


def _enum(
    value: Any,
    allowed: set[str],
    path: str,
    issues: list[dict[str, str]],
    code: str,
) -> str:
    clean = str(value or "").strip()
    if clean not in allowed:
        _issue(issues, code, path, f"必须是以下值之一：{', '.join(sorted(allowed))}。")
    return clean


def _optional_int(
    value: Any,
    path: str,
    issues: list[dict[str, str]],
    *,
    minimum: int,
    maximum: int,
) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        _issue(issues, "INTEGER_REQUIRED", path, "该字段必须是整数或 null。")
        return None
    if value < minimum or value > maximum:
        _issue(issues, "INTEGER_OUT_OF_RANGE", path, f"该字段必须在 {minimum} 到 {maximum} 之间。")
    return max(minimum, min(maximum, value))


def _issue(
    issues: list[dict[str, str]],
    code: str,
    path: str,
    message: str,
) -> None:
    issue = {"code": code, "path": path, "message": message}
    if issue not in issues:
        issues.append(issue)


__all__ = [
    "CANDIDATE_LINEAGE_VERSION",
    "CONFIDENCE_BOUNDARY",
    "TURN_CONTRACT_VERSION",
    "extract_turn_contract",
    "validate_stored_turn_contract",
    "validate_turn_contract_payload",
]
