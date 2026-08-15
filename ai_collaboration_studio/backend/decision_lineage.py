from __future__ import annotations

import hashlib
import json
import re
from typing import Any


DECISION_PACKAGE_VERSION_V1 = "decision_package_v1"
DECISION_PACKAGE_VERSION = "decision_package_v2"
DECISION_LINEAGE_EVENT_VERSION = "decision_lineage_event_v1"
DECISION_LINEAGE_RELATIONS = {
    "implements",
    "revises",
    "confirms",
    "tests",
    "evaluates",
    "records_outcome",
}
MAX_RESOURCE_SNAPSHOT_BYTES = 256 * 1024

_RESOURCE_TYPE_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{2,119}")
_FORBIDDEN_RESOURCE_TOKENS = {
    "account",
    "broker",
    "brokerage",
    "execution",
    "order",
    "orders",
    "payment",
    "trade",
    "trading",
    "wallet",
}
_FORBIDDEN_FIELD_NAMES = {
    "account",
    "account_id",
    "api_key",
    "apikey",
    "broker_session",
    "client_order_id",
    "credential",
    "order",
    "order_id",
    "password",
    "payment",
    "prompt",
    "secret",
    "token",
    "trade_context",
    "unlock" + "_trade",
    "wallet",
}


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def artifact_binding_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Match the immutable payload hashed by artifact_user_decisions."""

    content = snapshot.get("content") if isinstance(snapshot.get("content"), dict) else {}
    return {
        "id": str(snapshot.get("id") or ""),
        "room_id": str(snapshot.get("room_id") or ""),
        "round_id": str(snapshot.get("round_id") or ""),
        "title": str(snapshot.get("title") or ""),
        "status": str(snapshot.get("status") or ""),
        "version": int(snapshot.get("version") or 0),
        "content": content,
    }


def selected_option_snapshot(
    artifact_snapshot: dict[str, Any],
    preferred_option_id: str,
) -> dict[str, Any]:
    content = (
        artifact_snapshot.get("content")
        if isinstance(artifact_snapshot.get("content"), dict)
        else {}
    )
    decision = content.get("decision") if isinstance(content.get("decision"), dict) else {}
    if str(decision.get("status") or "").strip().lower() != "candidate":
        return {}
    preferred = str(preferred_option_id or "").strip()
    for option in decision.get("options") if isinstance(decision.get("options"), list) else []:
        if isinstance(option, dict) and str(option.get("id") or "").strip() == preferred:
            return json.loads(json.dumps(option, ensure_ascii=False, allow_nan=False))
    return {}


def clean_relation_note(value: Any, *, required: bool = False) -> str:
    note = re.sub(r"\s+", " ", str(value or "")).strip()[:1000]
    if required and len(note) < 3:
        raise ValueError("关联决策方案时必须填写推导说明")
    return note


def clean_resource_type(value: Any) -> str:
    resource_type = str(value or "").strip().lower()[:120]
    if not _RESOURCE_TYPE_PATTERN.fullmatch(resource_type):
        raise ValueError("决策谱系资源类型无效")
    tokens = {token for token in re.split(r"[._-]+", resource_type) if token}
    if any(
        forbidden in token
        for token in tokens
        for forbidden in _FORBIDDEN_RESOURCE_TOKENS
    ):
        raise ValueError("决策谱系不能关联账户、订单、执行或资金资源")
    return resource_type


def _reject_sensitive_fields(value: Any) -> None:
    if isinstance(value, dict):
        for raw_key, nested in value.items():
            key = str(raw_key).strip().lower().replace("-", "_")
            forbidden_fragment = any(
                key == fragment
                or key.startswith(f"{fragment}_")
                or key.endswith(f"_{fragment}")
                for fragment in (
                    "account",
                    "credential",
                    "order",
                    "password",
                    "payment",
                    "prompt",
                    "secret",
                    "token",
                    "wallet",
                )
            )
            unsafe_execution = (
                "execution" in key and key != "execution_capability"
            )
            if key in _FORBIDDEN_FIELD_NAMES or forbidden_fragment or unsafe_execution:
                raise ValueError("决策谱系快照包含不允许持久化的敏感或执行字段")
            if key == "execution_capability" and nested != "none":
                raise ValueError("决策谱系资源不能获得执行能力")
            if key == "live_trading_allowed" and nested is not False:
                raise ValueError("决策谱系资源不能打开真实交易")
            if key == "can_autonomously_decide" and nested is not False:
                raise ValueError("决策谱系资源不能获得自主决策权限")
            _reject_sensitive_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_sensitive_fields(nested)


def normalize_resource_snapshot(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("决策谱系资源快照必须是对象")
    try:
        snapshot = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("决策谱系资源快照不能序列化") from exc
    _reject_sensitive_fields(snapshot)
    if len(canonical_json(snapshot).encode("utf-8")) > MAX_RESOURCE_SNAPSHOT_BYTES:
        raise ValueError("决策谱系资源快照超过大小限制")
    return snapshot


def event_hash_payload(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": DECISION_LINEAGE_EVENT_VERSION,
        "id": str(event.get("id") or ""),
        "room_id": str(event.get("room_id") or ""),
        "user_decision_id": str(event.get("user_decision_id") or ""),
        "sequence_no": int(event.get("sequence_no") or 0),
        "relation_type": str(event.get("relation_type") or ""),
        "resource_type": str(event.get("resource_type") or ""),
        "resource_id": str(event.get("resource_id") or ""),
        "resource_revision": str(event.get("resource_revision") or ""),
        "resource_state": str(event.get("resource_state") or ""),
        "relation_note": str(event.get("relation_note") or ""),
        "resource_snapshot_sha256": str(event.get("resource_snapshot_sha256") or ""),
        "previous_event_sha256": str(event.get("previous_event_sha256") or ""),
        "created_by": str(event.get("created_by") or ""),
        "created_at": int(event.get("created_at") or 0),
    }


def calculate_event_sha256(event: dict[str, Any]) -> str:
    return canonical_sha256(event_hash_payload(event))


__all__ = [
    "DECISION_LINEAGE_EVENT_VERSION",
    "DECISION_LINEAGE_RELATIONS",
    "DECISION_PACKAGE_VERSION",
    "DECISION_PACKAGE_VERSION_V1",
    "artifact_binding_payload",
    "calculate_event_sha256",
    "canonical_json",
    "canonical_sha256",
    "clean_relation_note",
    "clean_resource_type",
    "normalize_resource_snapshot",
    "selected_option_snapshot",
]
