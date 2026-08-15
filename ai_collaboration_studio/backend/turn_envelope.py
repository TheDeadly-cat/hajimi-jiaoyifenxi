from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from .turn_contract import (
    CONFIDENCE_BOUNDARY,
    TURN_CONTRACT_VERSION,
    extract_turn_contract,
    validate_turn_contract_payload,
)


TURN_ENVELOPE_VERSION = "turn_envelope_v1"
TURN_ENVELOPE_OUTPUT_MODES = frozenset({
    "json_schema",
    "json_object",
    "prompt_json",
})

_ENVELOPE_FIELDS = {"version", "turn_contract", "visible_content"}
_TURN_CONTRACT_TAG_PATTERN = re.compile(r"</?turn_contract\b", re.IGNORECASE)


def _object_schema(
    properties: dict[str, Any],
    *,
    required: Iterable[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required if required is not None else properties),
        "additionalProperties": False,
    }


_EVIDENCE_SCHEMA = _object_schema({
    "type": {"type": "string", "enum": [
        "message",
        "material",
        "round_market_snapshot",
    ]},
    "id": {"type": "string", "minLength": 1, "maxLength": 100},
    "role": {"type": "string", "enum": ["support", "counter", "context"]},
})

_TURN_CONTRACT_SCHEMA = _object_schema({
    "version": {"type": "string", "const": TURN_CONTRACT_VERSION},
    "claims": {
        "type": "array",
        "maxItems": 12,
        "items": _object_schema({
            "id": {
                "type": "string",
                "pattern": "^[A-Za-z][A-Za-z0-9_-]{0,79}$",
            },
            "kind": {"type": "string", "enum": ["fact", "inference", "unknown"]},
            "text": {"type": "string", "minLength": 1, "maxLength": 800},
            "as_of": {"type": "string", "maxLength": 80},
            "evidence": {
                "type": "array",
                "maxItems": 8,
                "items": _EVIDENCE_SCHEMA,
            },
        }),
    },
    "responds_to": {
        "type": "array",
        "maxItems": 12,
        "items": _object_schema({
            "type": {"type": "string", "const": "message"},
            "id": {"type": "string", "minLength": 1, "maxLength": 100},
            "relation": {"type": "string", "enum": [
                "supports",
                "challenges",
                "qualifies",
                "questions",
            ]},
            "reason": {"type": "string", "minLength": 1, "maxLength": 500},
        }),
    },
    "candidate_updates": {
        "type": "array",
        "maxItems": 8,
        "items": _object_schema({
            "id": {
                "type": "string",
                "pattern": "^[A-Za-z][A-Za-z0-9_-]{0,79}$",
            },
            "title": {"type": "string", "minLength": 1, "maxLength": 180},
            "action": {"type": "string", "enum": [
                "propose",
                "revise",
                "support",
                "challenge",
                "select",
                "reject",
                "defer",
            ]},
            "symbol": {"type": "string", "maxLength": 24},
            "direction": {"type": "string", "enum": [
                "UP",
                "DOWN",
                "NEUTRAL",
                "FLAT",
                "UNSPECIFIED",
            ]},
            "horizon_days": {
                "anyOf": [
                    {"type": "integer", "minimum": 1, "maximum": 3650},
                    {"type": "null"},
                ],
            },
            "thesis": {"type": "string", "minLength": 1, "maxLength": 1200},
            "invalidation": {"type": "string", "maxLength": 1000},
            "evidence": {
                "type": "array",
                "maxItems": 8,
                "items": _EVIDENCE_SCHEMA,
            },
        }),
    },
    "risks": {
        "type": "array",
        "maxItems": 12,
        "items": _object_schema({
            "id": {
                "type": "string",
                "pattern": "^[A-Za-z][A-Za-z0-9_-]{0,79}$",
            },
            "text": {"type": "string", "minLength": 1, "maxLength": 800},
            "severity": {"type": "string", "enum": [
                "unknown",
                "low",
                "medium",
                "high",
                "critical",
            ]},
            "status": {"type": "string", "enum": [
                "open",
                "monitoring",
                "mitigated",
                "accepted",
            ]},
            "trigger": {"type": "string", "maxLength": 500},
            "mitigation": {"type": "string", "maxLength": 800},
            "blocking": {"type": "boolean"},
            "evidence": {
                "type": "array",
                "maxItems": 8,
                "items": _EVIDENCE_SCHEMA,
            },
        }),
    },
    "next_actions": {
        "type": "array",
        "maxItems": 12,
        "items": _object_schema({
            "id": {
                "type": "string",
                "pattern": "^[A-Za-z][A-Za-z0-9_-]{0,79}$",
            },
            "text": {"type": "string", "minLength": 1, "maxLength": 600},
            "owner": {"type": "string", "minLength": 1, "maxLength": 120},
            "state": {"type": "string", "enum": [
                "open",
                "in_progress",
                "blocked",
                "done",
            ]},
            "due": {"type": "string", "maxLength": 80},
            "evidence": {
                "type": "array",
                "maxItems": 8,
                "items": _EVIDENCE_SCHEMA,
            },
        }),
    },
    "confidence": _object_schema({
        "kind": {"type": "string", "const": "model_subjective"},
        "value": {
            "anyOf": [
                {"type": "number", "minimum": 0, "maximum": 100},
                {"type": "null"},
            ],
        },
        "label": {"type": "string", "enum": ["unknown", "low", "medium", "high"]},
        "basis": {"type": "string", "maxLength": 500},
    }),
})

TURN_ENVELOPE_SCHEMA: dict[str, Any] = _object_schema({
    "version": {"type": "string", "const": TURN_ENVELOPE_VERSION},
    # Keep the machine contract before the prose in schema-guided generation;
    # this makes a token-limit truncation less likely to erase the contract.
    "turn_contract": _TURN_CONTRACT_SCHEMA,
    "visible_content": {"type": "string", "minLength": 1, "maxLength": 8000},
})


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


TURN_ENVELOPE_SCHEMA_SHA256 = _canonical_sha256(TURN_ENVELOPE_SCHEMA)


class _DuplicateJsonKey(ValueError):
    pass


class _NonFiniteJsonNumber(ValueError):
    pass


def normalize_turn_envelope_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    if mode not in TURN_ENVELOPE_OUTPUT_MODES:
        raise ValueError("turn envelope output mode is invalid")
    return mode


def normalize_turn_envelope_member_modes(value: Any) -> dict[str, str]:
    """Return a deterministic member-to-output-mode mapping for sealing."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("turn envelope member output modes must be an object")
    if len(value) > 256:
        raise ValueError("turn envelope member output modes may contain at most 256 members")
    normalized: dict[str, str] = {}
    for raw_member_id, raw_mode in value.items():
        if not isinstance(raw_member_id, str):
            raise ValueError("turn envelope member id must be a string")
        member_id = raw_member_id.strip()
        if (
            not member_id
            or len(member_id) > 128
            or any(ord(character) < 32 for character in member_id)
        ):
            raise ValueError("turn envelope member id is invalid")
        if member_id in normalized:
            raise ValueError("turn envelope member ids must be unique after normalization")
        normalized[member_id] = normalize_turn_envelope_mode(raw_mode)
    return {member_id: normalized[member_id] for member_id in sorted(normalized)}


def turn_envelope_protocol(member_modes: Any = None) -> dict[str, Any]:
    """Build the canonical protocol marker frozen by a launch/checkpoint layer."""

    return {
        "version": TURN_ENVELOPE_VERSION,
        "schema_sha256": TURN_ENVELOPE_SCHEMA_SHA256,
        "member_output_modes": normalize_turn_envelope_member_modes(member_modes),
    }


def extract_turn_envelope(
    content: Any,
    *,
    member: Mapping[str, Any] | None = None,
    allowed_message_ids: Iterable[str] = (),
    allowed_material_ids: Iterable[str] = (),
    allowed_market_snapshot_id: str = "",
    prior_ai_message_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Parse one exact ``turn_envelope_v1`` JSON object and qualify its contract.

    No Markdown-fence stripping, embedded-object salvage, XML fallback or
    semantic repair is attempted.  A malformed structured response is a local,
    fail-closed validation failure and never becomes visible chat content.
    """

    issues: list[dict[str, str]] = []
    raw_content = content if isinstance(content, str) else ""
    if not isinstance(content, str):
        _add_issue(
            issues,
            "TURN_ENVELOPE_STRING_REQUIRED",
            "turn_envelope",
            "发言封装必须是 JSON 字符串。",
        )
    if len(raw_content) > 60_000:
        _add_issue(
            issues,
            "CONTENT_TOO_LONG",
            "turn_envelope",
            "发言封装不能超过 60000 个字符。",
        )

    payload = (
        _load_unique_json_object(raw_content, issues)
        if len(raw_content) <= 60_000
        else None
    )
    visible_content = ""
    contract_payload: Any = None
    if payload is not None:
        for field in sorted(_ENVELOPE_FIELDS - set(payload)):
            _add_issue(
                issues,
                "TURN_ENVELOPE_FIELD_MISSING",
                f"turn_envelope.{field}",
                f"发言封装缺少字段 {field}。",
            )
        for field in sorted(set(payload) - _ENVELOPE_FIELDS):
            _add_issue(
                issues,
                "TURN_ENVELOPE_FIELD_UNKNOWN",
                f"turn_envelope.{field}",
                f"发言封装不支持字段 {field}。",
            )
        if payload.get("version") != TURN_ENVELOPE_VERSION:
            _add_issue(
                issues,
                "TURN_ENVELOPE_VERSION_INVALID",
                "turn_envelope.version",
                f"version 必须是 {TURN_ENVELOPE_VERSION}。",
            )

        raw_visible = payload.get("visible_content")
        if not isinstance(raw_visible, str):
            _add_issue(
                issues,
                "VISIBLE_CONTENT_STRING_REQUIRED",
                "turn_envelope.visible_content",
                "可展示正文必须是字符串。",
            )
        else:
            visible_content = _normalize_visible_content(raw_visible)
            if any(
                ord(character) < 32 and character not in {"\n", "\r", "\t"}
                for character in raw_visible
            ):
                _add_issue(
                    issues,
                    "VISIBLE_CONTENT_CONTROL_CHARACTER_FORBIDDEN",
                    "turn_envelope.visible_content",
                    "可展示正文不能包含不可见控制字符。",
                )
            if not visible_content:
                _add_issue(
                    issues,
                    "VISIBLE_CONTENT_REQUIRED",
                    "turn_envelope.visible_content",
                    "发言封装必须包含可展示的群聊正文。",
                )
            elif len(visible_content) > 8_000:
                _add_issue(
                    issues,
                    "VISIBLE_CONTENT_TOO_LONG",
                    "turn_envelope.visible_content",
                    "可展示正文不能超过 8000 个字符。",
                )
            if _TURN_CONTRACT_TAG_PATTERN.search(raw_visible):
                _add_issue(
                    issues,
                    "TURN_CONTRACT_TAG_FORBIDDEN_IN_VISIBLE_CONTENT",
                    "turn_envelope.visible_content",
                    "纯 JSON 发言封装的可展示正文不得再携带 turn_contract 标签。",
                )

        contract_payload = payload.get("turn_contract")
        if isinstance(contract_payload, Mapping):
            try:
                contract_size = len(json.dumps(
                    contract_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ))
            except (TypeError, ValueError):
                contract_size = 32_001
            if contract_size > 32_000:
                _add_issue(
                    issues,
                    "TURN_CONTRACT_TOO_LONG",
                    "turn_envelope.turn_contract",
                    "发言合同不能超过 32000 个字符。",
                )

    contract_validation = validate_turn_contract_payload(
        contract_payload,
        member=member,
        allowed_message_ids=allowed_message_ids,
        allowed_material_ids=allowed_material_ids,
        allowed_market_snapshot_id=allowed_market_snapshot_id,
        prior_ai_message_ids=prior_ai_message_ids,
        require_all_input_fields=True,
    )
    for issue in contract_validation["issues"]:
        if issue not in issues:
            issues.append(issue)

    return {
        "version": TURN_ENVELOPE_VERSION,
        "turn_envelope_version": TURN_ENVELOPE_VERSION,
        "turn_contract_version": TURN_CONTRACT_VERSION,
        "wire_format": "json_envelope",
        "contract_attempted": True,
        "found": payload is not None,
        "visible_content": visible_content,
        "contract": contract_validation.get("contract"),
        "role_profiles": contract_validation.get("role_profiles") or [],
        "qualified": (
            payload is not None
            and contract_validation.get("qualified") is True
            and not issues
        ),
        "issues": issues,
        "confidence_is_not_win_rate": True,
        "confidence_boundary": CONFIDENCE_BOUNDARY,
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
    }


def parse_speaker_output(
    content: Any,
    *,
    turn_contract_version: str | None,
    turn_envelope_version: str | None,
    member: Mapping[str, Any] | None = None,
    allowed_message_ids: Iterable[str] = (),
    allowed_material_ids: Iterable[str] = (),
    allowed_market_snapshot_id: str = "",
    prior_ai_message_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Dispatch by the frozen wire protocol without content-based downgrade."""

    clean_contract_version = str(turn_contract_version or "").strip() or None
    clean_envelope_version = str(turn_envelope_version or "").strip() or None
    common_kwargs = {
        "member": member,
        "allowed_message_ids": allowed_message_ids,
        "allowed_material_ids": allowed_material_ids,
        "allowed_market_snapshot_id": allowed_market_snapshot_id,
        "prior_ai_message_ids": prior_ai_message_ids,
    }
    if clean_envelope_version is not None:
        if clean_envelope_version != TURN_ENVELOPE_VERSION:
            return _unsupported_protocol_result(
                clean_contract_version,
                clean_envelope_version,
                "TURN_ENVELOPE_VERSION_UNSUPPORTED",
            )
        result = extract_turn_envelope(content, **common_kwargs)
        if clean_contract_version != TURN_CONTRACT_VERSION:
            _add_issue(
                result["issues"],
                "TURN_CONTRACT_VERSION_UNSUPPORTED",
                "turn_contract.version",
                "纯 JSON 发言封装必须绑定 turn_contract_v1。",
            )
            result["qualified"] = False
        return result

    if clean_contract_version == TURN_CONTRACT_VERSION:
        result = extract_turn_contract(content, **common_kwargs)
        return {
            **result,
            "turn_envelope_version": None,
            "turn_contract_version": TURN_CONTRACT_VERSION,
            "wire_format": "legacy_xml",
            "contract_attempted": bool(
                result.get("found")
                or _TURN_CONTRACT_TAG_PATTERN.search(str(content or ""))
            ),
        }
    if clean_contract_version is not None:
        return _unsupported_protocol_result(
            clean_contract_version,
            None,
            "TURN_CONTRACT_VERSION_UNSUPPORTED",
        )

    return {
        "version": None,
        "turn_envelope_version": None,
        "turn_contract_version": None,
        "wire_format": "plain",
        "contract_attempted": False,
        "found": False,
        "visible_content": str(content or ""),
        "contract": None,
        "role_profiles": [],
        "qualified": False,
        "issues": [],
        "confidence_is_not_win_rate": True,
        "confidence_boundary": CONFIDENCE_BOUNDARY,
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
    }


def _load_unique_json_object(
    raw_content: str,
    issues: list[dict[str, str]],
) -> dict[str, Any] | None:
    if not raw_content.strip():
        _add_issue(
            issues,
            "TURN_ENVELOPE_EMPTY",
            "turn_envelope",
            "发言封装不能为空。",
        )
        return None

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise _DuplicateJsonKey(str(key))
            value[key] = item
        return value

    def reject_constant(value: str) -> None:
        raise _NonFiniteJsonNumber(value)

    try:
        parsed = json.loads(
            raw_content,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except _DuplicateJsonKey as exc:
        _add_issue(
            issues,
            "JSON_DUPLICATE_KEY",
            "turn_envelope",
            f"JSON 包含重复字段：{exc}。",
        )
        return None
    except _NonFiniteJsonNumber:
        _add_issue(
            issues,
            "TURN_ENVELOPE_JSON_NON_FINITE",
            "turn_envelope",
            "发言封装不能包含 NaN 或无穷数值。",
        )
        return None
    except (TypeError, ValueError, json.JSONDecodeError):
        _add_issue(
            issues,
            "TURN_ENVELOPE_JSON_INVALID",
            "turn_envelope",
            "发言封装不是完整有效的 JSON。",
        )
        return None
    if not isinstance(parsed, dict):
        _add_issue(
            issues,
            "TURN_ENVELOPE_OBJECT_REQUIRED",
            "turn_envelope",
            "发言封装根节点必须是 JSON 对象。",
        )
        return None
    return parsed


def _normalize_visible_content(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    visible = re.sub(r"[ \t]+\n", "\n", value)
    visible = re.sub(r"\n{3,}", "\n\n", visible)
    return visible.strip()


def _unsupported_protocol_result(
    turn_contract_version: str | None,
    turn_envelope_version: str | None,
    code: str,
) -> dict[str, Any]:
    return {
        "version": turn_envelope_version or turn_contract_version,
        "turn_envelope_version": turn_envelope_version,
        "turn_contract_version": turn_contract_version,
        "wire_format": "unsupported",
        "contract_attempted": bool(turn_contract_version or turn_envelope_version),
        "found": False,
        "visible_content": "",
        "contract": None,
        "role_profiles": [],
        "qualified": False,
        "issues": [{
            "code": code,
            "path": "turn_envelope_version" if turn_envelope_version else "turn_contract_version",
            "message": "冻结的发言输出协议版本不受当前服务支持。",
        }],
        "confidence_is_not_win_rate": True,
        "confidence_boundary": CONFIDENCE_BOUNDARY,
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
    }


def _add_issue(
    issues: list[dict[str, str]],
    code: str,
    path: str,
    message: str,
) -> None:
    issue = {"code": code, "path": path, "message": message}
    if issue not in issues:
        issues.append(issue)


__all__ = [
    "TURN_ENVELOPE_OUTPUT_MODES",
    "TURN_ENVELOPE_SCHEMA",
    "TURN_ENVELOPE_SCHEMA_SHA256",
    "TURN_ENVELOPE_VERSION",
    "extract_turn_envelope",
    "normalize_turn_envelope_member_modes",
    "normalize_turn_envelope_mode",
    "parse_speaker_output",
    "turn_envelope_protocol",
]
