from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


ARTIFACT_EVIDENCE_GRAPH_VERSION_V1 = "artifact_evidence_graph_v1"
ARTIFACT_EVIDENCE_GRAPH_VERSION = "artifact_evidence_graph_v2"
ARTIFACT_EVIDENCE_REVIEW_EVENT_VERSION = "artifact_evidence_review_event_v1"
ARTIFACT_EVIDENCE_REVIEW_CHAIN_VERSION = "artifact_evidence_review_chain_v1"

ARTIFACT_SECTIONS = (
    "requirements",
    "risks",
    "conclusions",
    "disagreements",
    "unknowns",
    "actions",
)

EVIDENCE_ROLES = frozenset({"support", "counter", "context"})
VERIFICATION_STATUSES = frozenset({
    "unreviewed",
    "source_checked",
    "corroborated",
    "disputed",
})
VERSION_DECISIONS = frozenset({"current", "keep_snapshot", "review_required"})
REVIEW_EVENT_TYPES = frozenset({"created", "revised", "confirmed"})

TARGET_FIELDS_BY_SECTION = {
    "requirements": (
        "id", "text", "status", "owner", "acceptance_criteria",
    ),
    "risks": (
        "id", "text", "status", "probability", "impact", "blocking",
        "trigger", "mitigation", "owner",
    ),
    "conclusions": ("id", "text"),
    "disagreements": (
        "id", "text", "positions", "status", "blocking", "owner",
        "resolution",
    ),
    "unknowns": ("id", "text"),
    "actions": ("id", "text", "owner", "due", "state"),
    "decision_options": (
        "id", "title", "description", "benefits", "risks", "value", "cost",
        "timeline", "dependencies", "reversibility",
    ),
    "decision": ("status", "preferred_option_id", "rationale"),
}


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def opaque_graph_id(kind: str, *parts: Any) -> str:
    clean_kind = "".join(
        character for character in str(kind or "node").lower()
        if character.isalnum() or character == "_"
    )[:32] or "node"
    digest = canonical_sha256([str(part or "") for part in parts])[:32]
    return f"{clean_kind}_{digest}"


def _clean_text(value: Any, max_length: int) -> str:
    return str(value or "").strip()[:max_length]


def _clean_nonnegative_integer(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def target_snapshot_payload(item_key: str, raw_target: Any) -> dict[str, Any]:
    target = raw_target if isinstance(raw_target, dict) else {}
    if item_key == "summary":
        return {"summary": _clean_text(target.get("summary"), 5000)}
    section = item_key.split(":", 1)[0]
    fields = TARGET_FIELDS_BY_SECTION.get(section, tuple(
        key for key in target if key != "evidence"
    ))
    return {field: target.get(field) for field in fields if field in target}


def _relation(
    item_key: str,
    target_snapshot_sha256: str,
    raw: Any,
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    source_type = _clean_text(raw.get("type"), 40).lower()
    source_id = _clean_text(raw.get("id"), 80)
    if source_type not in {"material", "message", "round_market_snapshot"} or not source_id:
        return None
    evidence_role = _clean_text(raw.get("evidence_role"), 40).lower()
    verification_status = _clean_text(raw.get("verification_status"), 40).lower()
    version_decision = _clean_text(raw.get("version_decision"), 40).lower()
    version_status = _clean_text(raw.get("version_status"), 40).lower()
    return {
        "item_key": _clean_text(item_key, 180),
        "target_snapshot_sha256": _clean_text(target_snapshot_sha256, 64).lower(),
        "source_type": source_type,
        "source_id": source_id,
        "source_version": _clean_nonnegative_integer(raw.get("version")),
        "source_revision": _clean_text(raw.get("source_revision"), 160),
        "source_snapshot_sha256": _clean_text(
            raw.get("source_snapshot_sha256"),
            64,
        ).lower(),
        "evidence_role": evidence_role if evidence_role in EVIDENCE_ROLES else "context",
        "verification_status": (
            verification_status
            if verification_status in VERIFICATION_STATUSES
            else "unreviewed"
        ),
        "review_note": _clean_text(raw.get("review_note"), 500),
        "version_decision": (
            version_decision if version_decision in VERSION_DECISIONS else "current"
        ),
        "latest_version": _clean_nonnegative_integer(raw.get("latest_version")),
        "version_status": version_status or "current",
        "source_active": raw.get("source_active") is not False,
    }


def project_evidence_relations(content: Any) -> list[dict[str, Any]]:
    """Project every explicit artifact evidence relation into a stable snapshot.

    The projection never infers semantic relationships from free text.  Relation
    identity follows the persisted ``artifact_evidence`` key and exact frozen
    source fields.  Duplicate persisted keys are rejected instead of silently
    choosing a relation.
    """

    data = content if isinstance(content, dict) else {}
    relations: list[dict[str, Any]] = []

    def extend(item_key: str, target: Any, raw_relations: Any) -> None:
        target_snapshot_sha256 = canonical_sha256(
            target_snapshot_payload(item_key, target)
        )
        for raw in raw_relations if isinstance(raw_relations, list) else []:
            relation = _relation(item_key, target_snapshot_sha256, raw)
            if relation is not None:
                relations.append(relation)

    extend(
        "summary",
        {"summary": _clean_text(data.get("summary"), 5000)},
        data.get("summary_evidence"),
    )
    for section in ARTIFACT_SECTIONS:
        for item in data.get(section) if isinstance(data.get(section), list) else []:
            if not isinstance(item, dict):
                continue
            item_id = _clean_text(item.get("id"), 80)
            if item_id:
                extend(f"{section}:{item_id}", item, item.get("evidence"))
    decision = data.get("decision") if isinstance(data.get("decision"), dict) else {}
    extend("decision", decision, decision.get("evidence"))
    for option in decision.get("options") if isinstance(decision.get("options"), list) else []:
        if not isinstance(option, dict):
            continue
        option_id = _clean_text(option.get("id"), 80)
        if option_id:
            extend(f"decision_options:{option_id}", option, option.get("evidence"))

    relations.sort(key=relation_sort_key)
    seen: set[tuple[str, str, str]] = set()
    for relation in relations:
        persisted_key = (
            relation["item_key"],
            relation["source_type"],
            relation["source_id"],
        )
        if persisted_key in seen:
            raise ValueError("duplicate artifact evidence relation identity")
        seen.add(persisted_key)
    return relations


def relation_sort_key(relation: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(relation.get("item_key") or ""),
        str(relation.get("target_snapshot_sha256") or ""),
        str(relation.get("source_type") or ""),
        str(relation.get("source_id") or ""),
        int(relation.get("source_version") or 0),
        str(relation.get("source_revision") or ""),
        str(relation.get("source_snapshot_sha256") or ""),
    )


def persisted_relation_snapshot(
    relations: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    fields = (
        "item_key",
        "source_type",
        "source_id",
        "source_version",
        "source_revision",
        "source_snapshot_sha256",
        "evidence_role",
        "verification_status",
        "review_note",
        "version_decision",
    )
    projected = [
        {field: relation.get(field) for field in fields}
        for relation in relations
    ]
    projected.sort(key=relation_sort_key)
    return projected


def relation_identity_payload(relation: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "item_key",
        "target_snapshot_sha256",
        "source_type",
        "source_id",
        "source_version",
        "source_revision",
        "source_snapshot_sha256",
    )
    return {field: relation.get(field) for field in fields}


def relation_identity(relation: dict[str, Any]) -> str:
    return canonical_sha256(relation_identity_payload(relation))


def review_event_sha256(event: dict[str, Any]) -> str:
    fields = (
        "event_version",
        "room_id",
        "artifact_id",
        "artifact_version",
        "sequence_no",
        "event_type",
        "artifact_status",
        "relation_snapshot_sha256",
        "previous_event_sha256",
        "created_by",
        "created_at",
    )
    return canonical_sha256({field: event.get(field) for field in fields})


def relation_snapshot_delta(
    previous: Iterable[dict[str, Any]],
    current: Iterable[dict[str, Any]],
) -> dict[str, int]:
    previous_by_id = {relation_identity(item): item for item in previous}
    current_by_id = {relation_identity(item): item for item in current}
    shared_ids = previous_by_id.keys() & current_by_id.keys()
    return {
        "added_relation_count": len(current_by_id.keys() - previous_by_id.keys()),
        "removed_relation_count": len(previous_by_id.keys() - current_by_id.keys()),
        "changed_relation_count": sum(
            previous_by_id[relation_id] != current_by_id[relation_id]
            for relation_id in shared_ids
        ),
    }


def relation_summary(relations: Iterable[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "relation_count": 0,
        "support_count": 0,
        "counter_count": 0,
        "context_count": 0,
        "unreviewed_count": 0,
        "source_checked_count": 0,
        "corroborated_count": 0,
        "disputed_count": 0,
    }
    for relation in relations:
        summary["relation_count"] += 1
        role = str(relation.get("evidence_role") or "context")
        status = str(relation.get("verification_status") or "unreviewed")
        if f"{role}_count" in summary:
            summary[f"{role}_count"] += 1
        if f"{status}_count" in summary:
            summary[f"{status}_count"] += 1
    return summary
