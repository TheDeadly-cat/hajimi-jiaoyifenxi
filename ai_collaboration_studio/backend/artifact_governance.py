from __future__ import annotations

import copy
import re
from typing import Any, Mapping, Sequence

from .decision_lineage import (
    artifact_binding_payload,
    canonical_sha256,
)


ARTIFACT_GOVERNANCE_VERSION = "artifact_governance_v1"
ARTIFACT_GOVERNANCE_ATTESTATION_VERSION = (
    "artifact_governance_attestation_v1"
)
ROUND_GOVERNANCE_EVALUATOR_VERSION = "round_governance_evaluator_v1"
ROUND_GOVERNANCE_INPUT_VERSION = "round_governance_input_v1"

GOVERNED_CANDIDATE_FIELDS = (
    "title",
    "description",
    "benefits",
    "risks",
    "value",
    "cost",
    "timeline",
    "dependencies",
    "reversibility",
    "evidence",
)
GOVERNED_CANDIDATE_LINEAGE_FIELDS = (
    "version",
    "origin_message_id",
    "latest_message_id",
    "revision",
)

CANDIDATE_LINEAGE_VERSION = "candidate_lineage_v1"
CANDIDATE_RISK_REVIEW_VERSION = "candidate_risk_review_v1"
RISK_REVIEW_ACTIONS = frozenset({"support", "challenge", "reject"})

RISK_DISPOSITIONS_ARE_USER_DECISIONS = False
EXECUTION_CAPABILITY = "none"
LIVE_TRADING_ALLOWED = False
CAN_AUTONOMOUSLY_DECIDE = False


def canonical_governance_sha256(value: Any) -> str:
    return canonical_sha256(value)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _integer(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"(?:0|[1-9][0-9]*)", value.strip()):
        return int(value.strip())
    return default


def _mapping(value: Any) -> dict[str, Any]:
    return copy.deepcopy(dict(value)) if isinstance(value, Mapping) else {}


def _records(value: Any) -> list[dict[str, Any]]:
    return [
        copy.deepcopy(dict(item))
        for item in value if isinstance(item, Mapping)
    ] if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else []


def _issue(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        **{
            key: copy.deepcopy(value)
            for key, value in details.items()
            if value not in (None, "", [], {})
        },
    }


def _issue_code(value: Any, fallback: str) -> str:
    if isinstance(value, Mapping):
        return _text(value.get("code")) or fallback
    raw = _text(value)
    if not raw:
        return fallback
    prefix = raw.split(":", 1)[0].strip().upper()
    return prefix if prefix.replace("_", "").isalnum() else fallback


def _issue_message(value: Any, fallback: str) -> str:
    if isinstance(value, Mapping):
        return _text(value.get("message")) or fallback
    return _text(value) or fallback


def _evidence_identity(value: Any) -> list[dict[str, str]]:
    identities: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in _records(value):
        source_type = _text(item.get("type")).lower()
        source_id = _text(item.get("id"))
        key = (source_type, source_id)
        if not source_type or not source_id or key in seen:
            continue
        seen.add(key)
        identities.append({"type": source_type, "id": source_id})
    return sorted(identities, key=lambda item: (item["type"], item["id"]))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [_text(item) for item in value if _text(item)]


def _candidate_lineage_slice(value: Any) -> dict[str, Any]:
    lineage = _mapping(value)
    return {
        "version": _text(lineage.get("version")),
        "origin_message_id": _text(lineage.get("origin_message_id")),
        "latest_message_id": _text(lineage.get("latest_message_id")),
        "revision": _integer(lineage.get("revision")),
    }


def _candidate_slice(value: Mapping[str, Any]) -> dict[str, Any]:
    candidate = {
        "id": _text(value.get("id")),
        "title": _text(value.get("title")),
        "description": _text(value.get("description") or value.get("text")),
        "benefits": _string_list(value.get("benefits")),
        "risks": _string_list(value.get("risks")),
        "value": _text(value.get("value")),
        "cost": _text(value.get("cost")),
        "timeline": _text(value.get("timeline")),
        "dependencies": _string_list(value.get("dependencies")),
        "reversibility": _text(value.get("reversibility") or "unknown").lower(),
        "evidence": _evidence_identity(value.get("evidence")),
        "lineage": _candidate_lineage_slice(value.get("lineage")),
    }
    return candidate


def _normalize_candidate_lineage(value: Any) -> dict[str, Any]:
    lineage = _mapping(value)
    candidates = [
        {
            "id": _text(item.get("id")),
            "origin_message_id": _text(item.get("origin_message_id")),
            "latest_message_id": _text(item.get("latest_message_id")),
            "revision": _integer(item.get("revision")),
        }
        for item in _records(lineage.get("candidates"))
        if _text(item.get("id"))
    ]
    issues = _records(lineage.get("issues"))
    ready = bool(
        lineage.get("version") == CANDIDATE_LINEAGE_VERSION
        and lineage.get("applicable") is not False
        and lineage.get("ready") is True
        and not issues
        and candidates
        and all(
            item["origin_message_id"]
            and item["latest_message_id"]
            and item["revision"] > 0
            for item in candidates
        )
    )
    return {
        "version": _text(lineage.get("version")),
        "applicable": lineage.get("applicable") is not False,
        "ready": ready,
        "status": "ready" if ready else _text(lineage.get("status") or "blocked"),
        "decision_message_id": _text(lineage.get("decision_message_id")),
        "referenced_candidate_ids": sorted(set(
            _string_list(lineage.get("referenced_candidate_ids"))
        )),
        "candidates": sorted(candidates, key=lambda item: item["id"]),
        "issues": issues,
    }


def _normalize_candidate_risk_reviews(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    applicable = source.get("applicable") is True
    issues = _records(source.get("issues"))
    normalized_reviews: list[dict[str, Any]] = []
    normalization_issues: list[dict[str, Any]] = []
    for item in _records(source.get("reviews")):
        action = _text(item.get("action")).lower()
        candidate_snapshot = _mapping(item.get("candidate_snapshot"))
        review = {
            "candidate_id": _text(item.get("candidate_id")),
            "candidate_revision": _integer(item.get("candidate_revision")),
            "candidate_latest_message_id": _text(
                item.get("candidate_latest_message_id")
            ),
            "candidate_snapshot_sha256": _text(
                item.get("candidate_snapshot_sha256")
            ).lower(),
            "candidate_snapshot": candidate_snapshot,
            "action": action,
            "disposition_only": True,
            "review_message_id": _text(item.get("review_message_id")),
            "reviewer_member_id": _text(item.get("reviewer_member_id")),
            "reviewer_member_version": _integer(item.get("reviewer_member_version")),
            "reviewer_name": _text(item.get("reviewer_name")),
            "reviewer_stage": _text(item.get("reviewer_stage")),
            "status": _text(item.get("status")).lower(),
            "current_candidate_revision": _integer(
                item.get("current_candidate_revision")
            ),
            "risk_ids": sorted(set(_string_list(item.get("risk_ids")))),
            "execution_capability": EXECUTION_CAPABILITY,
            "live_trading_allowed": LIVE_TRADING_ALLOWED,
            "can_autonomously_decide": CAN_AUTONOMOUSLY_DECIDE,
        }
        if action not in RISK_REVIEW_ACTIONS:
            normalization_issues.append(_issue(
                "CANDIDATE_RISK_REVIEW_ACTION_INVALID",
                "候选风险意见动作无效。",
                candidate_id=review["candidate_id"],
            ))
        if (
            not review["candidate_id"]
            or review["candidate_revision"] <= 0
            or not review["candidate_latest_message_id"]
            or not review["review_message_id"]
            or not review["reviewer_member_id"]
            or review["reviewer_member_version"] <= 0
            or not _is_sha256(review["candidate_snapshot_sha256"])
            or not candidate_snapshot
            or canonical_governance_sha256(candidate_snapshot)
            != review["candidate_snapshot_sha256"]
        ):
            normalization_issues.append(_issue(
                "CANDIDATE_RISK_REVIEW_BINDING_INVALID",
                "候选风险意见缺少精确候选版本或评审者版本绑定。",
                candidate_id=review["candidate_id"],
            ))
        normalized_reviews.append(review)
    issues.extend(normalization_issues)
    ready = bool(
        source.get("version") == CANDIDATE_RISK_REVIEW_VERSION
        and source.get("ready") is True
        and not issues
        and source.get("review_actions_are_dispositions_only") is True
        and (
            not applicable
            or all(review["status"] == "current" for review in normalized_reviews)
        )
    )
    action_counts = {
        action: sum(1 for review in normalized_reviews if review["action"] == action)
        for action in sorted(RISK_REVIEW_ACTIONS)
    }
    return {
        "version": _text(source.get("version")),
        "applicable": applicable,
        "ready": ready,
        "status": "ready" if ready and applicable else "not_required" if ready else "blocked",
        "decision_message_id": _text(source.get("decision_message_id")),
        "target_candidate_ids": sorted(set(
            _string_list(source.get("target_candidate_ids"))
        )),
        "target_candidate_count": len(set(
            _string_list(source.get("target_candidate_ids"))
        )),
        "reviewed_candidate_count": len({
            review["candidate_id"]
            for review in normalized_reviews
            if review["status"] == "current" and review["candidate_id"]
        }),
        "review_count": len(normalized_reviews),
        "current_review_count": sum(
            1 for review in normalized_reviews if review["status"] == "current"
        ),
        "stale_review_count": sum(
            1 for review in normalized_reviews if review["status"] != "current"
        ),
        "action_counts": action_counts,
        "reviews": sorted(
            normalized_reviews,
            key=lambda item: (
                item["candidate_id"], item["candidate_revision"], item["review_message_id"]
            ),
        ),
        "issues": issues,
        "review_actions_are_dispositions_only": True,
        "execution_capability": EXECUTION_CAPABILITY,
        "live_trading_allowed": LIVE_TRADING_ALLOWED,
        "can_autonomously_decide": CAN_AUTONOMOUSLY_DECIDE,
    }


def _is_sha256(value: Any) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", _text(value).lower()))


def _decision_slice(value: Any) -> dict[str, Any]:
    decision = _mapping(value)
    options = [_candidate_slice(item) for item in _records(decision.get("options"))]
    return {
        "status": _text(decision.get("status") or "undecided").lower(),
        "options": sorted(options, key=lambda item: item["id"]),
        "preferred_option_id": _text(decision.get("preferred_option_id")),
        "rationale": _text(decision.get("rationale")),
        "evidence": _evidence_identity(decision.get("evidence")),
    }


def _lineage_consistency_issues(
    projection: Mapping[str, Any],
) -> list[dict[str, Any]]:
    lineage = _mapping(projection.get("candidate_lineage"))
    lineage_by_id = {
        _text(item.get("id")): item
        for item in _records(lineage.get("candidates"))
        if _text(item.get("id"))
    }
    issues: list[dict[str, Any]] = []
    projected_decision = _mapping(projection.get("decision"))
    for option in _records(projected_decision.get("options")):
        candidate_id = _text(option.get("id"))
        option_lineage = _mapping(option.get("lineage"))
        expected = lineage_by_id.get(candidate_id)
        if not expected:
            issues.append(_issue(
                "PROJECTED_CANDIDATE_LINEAGE_MISSING",
                f"权威投影候选 {candidate_id or '（空）'} 缺少形成谱系。",
                candidate_id=candidate_id,
            ))
            continue
        if (
            _text(option_lineage.get("origin_message_id"))
            != _text(expected.get("origin_message_id"))
            or _text(option_lineage.get("latest_message_id"))
            != _text(expected.get("latest_message_id"))
            or _integer(option_lineage.get("revision"), -1)
            != _integer(expected.get("revision"), -2)
        ):
            issues.append(_issue(
                "PROJECTED_CANDIDATE_LINEAGE_MISMATCH",
                f"权威投影候选 {candidate_id} 的修订谱系不一致。",
                candidate_id=candidate_id,
            ))
    return issues


def _artifact_alignment(
    artifact: Mapping[str, Any],
    projection: Mapping[str, Any],
) -> dict[str, Any]:
    content = _mapping(artifact.get("content"))
    artifact_decision = _decision_slice(content.get("decision"))
    projected_decision = _decision_slice(projection.get("decision"))
    artifact_by_id = {
        item["id"]: item for item in artifact_decision["options"] if item["id"]
    }
    projected_by_id = {
        item["id"]: item for item in projected_decision["options"] if item["id"]
    }
    artifact_ids = sorted(artifact_by_id)
    projected_ids = sorted(projected_by_id)
    issues: list[dict[str, Any]] = []

    if artifact_decision["status"] != projected_decision["status"]:
        issues.append(_issue(
            "ARTIFACT_GOVERNANCE_DECISION_STATUS_MISMATCH",
            "纪要决策状态与封印轮次的权威投影不一致。",
        ))
    if artifact_ids != projected_ids:
        issues.append(_issue(
            "ARTIFACT_GOVERNANCE_CANDIDATE_SET_MISMATCH",
            "纪要候选集合与封印轮次中经过讨论的候选集合不一致。",
            artifact_candidate_ids=artifact_ids,
            projected_candidate_ids=projected_ids,
        ))
    if (
        artifact_decision["preferred_option_id"]
        != projected_decision["preferred_option_id"]
    ):
        issues.append(_issue(
            "ARTIFACT_GOVERNANCE_PREFERRED_OPTION_MISMATCH",
            "纪要首选方案与封印轮次的权威首选不一致。",
        ))
    if artifact_decision["rationale"] != projected_decision["rationale"]:
        issues.append(_issue(
            "ARTIFACT_GOVERNANCE_RATIONALE_MISMATCH",
            "纪要选择理由已偏离封印轮次的权威投影。",
        ))
    if artifact_decision["evidence"] != projected_decision["evidence"]:
        issues.append(_issue(
            "ARTIFACT_GOVERNANCE_DECISION_EVIDENCE_MISMATCH",
            "纪要决策证据引用已偏离封印轮次的权威投影。",
        ))
    shared_candidate_ids = sorted(set(artifact_by_id).intersection(projected_by_id))
    changed_candidates = [
        candidate_id
        for candidate_id in shared_candidate_ids
        if any(
            artifact_by_id[candidate_id].get(field)
            != projected_by_id[candidate_id].get(field)
            for field in GOVERNED_CANDIDATE_FIELDS
        )
    ]
    if changed_candidates:
        issues.append(_issue(
            "ARTIFACT_GOVERNANCE_CANDIDATE_FIELDS_MISMATCH",
            "纪要中的候选内容已被改写，不能沿用原轮次的风险复核。",
            candidate_ids=changed_candidates,
        ))
    issues.extend(_lineage_consistency_issues(projection))

    return {
        "applicable": True,
        "ready": not issues,
        "status": "aligned" if not issues else "mismatch",
        "artifact_decision_status": artifact_decision["status"],
        "projected_decision_status": projected_decision["status"],
        "artifact_candidate_ids": artifact_ids,
        "projected_candidate_ids": projected_ids,
        "candidate_ids_match": artifact_ids == projected_ids,
        "artifact_preferred_option_id": artifact_decision["preferred_option_id"],
        "projected_preferred_option_id": projected_decision["preferred_option_id"],
        "preferred_option_matches": (
            artifact_decision["preferred_option_id"]
            == projected_decision["preferred_option_id"]
        ),
        "candidate_core_fields": list(GOVERNED_CANDIDATE_FIELDS),
        "candidate_lineage_fields": list(GOVERNED_CANDIDATE_LINEAGE_FIELDS),
        "governed_artifact_decision_sha256": canonical_governance_sha256(
            artifact_decision
        ),
        "projected_decision_sha256": canonical_governance_sha256(
            projected_decision
        ),
        "issues": issues,
    }


def _user_decision_state(
    artifact: Mapping[str, Any],
    user_decisions: Sequence[Mapping[str, Any]],
    projected_preferred_option_id: str,
    projected_candidate_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    authoritative_candidate_ids = {
        _text(candidate_id)
        for candidate_id in projected_candidate_ids or []
        if _text(candidate_id)
    }
    if projected_candidate_ids is None:
        artifact_decision = _mapping(
            _mapping(artifact.get("content")).get("decision")
        )
        authoritative_candidate_ids = {
            _text(option.get("id"))
            for option in _records(artifact_decision.get("options"))
            if _text(option.get("id"))
        }
    current: Mapping[str, Any] | None = None
    for decision in user_decisions:
        if not isinstance(decision, Mapping):
            continue
        if decision.get("is_current") is True and decision.get("integrity_ok") is not False:
            current = decision
            break
    action = _text((current or {}).get("action")).lower()
    decision_version = _text(
        (current or {}).get("decision_version")
        or "artifact_user_decision_v1"
    )
    selected_option_id = _text(
        (current or {}).get("selected_option_id")
        if decision_version == "artifact_user_decision_v2"
        else (current or {}).get("preferred_option_id")
        if action == "support"
        else ""
    )
    matches_projected = bool(
        selected_option_id
        and selected_option_id in authoritative_candidate_ids
    )
    selection_valid = bool(
        matches_projected
        if action == "support"
        else not selected_option_id
    )
    if current and not selection_valid:
        current = None
        action = ""
        selected_option_id = ""
        decision_version = ""
        matches_projected = False
    ai_preferred_option_id = _text(projected_preferred_option_id)
    selected_is_ai_preferred = bool(
        selected_option_id
        and selected_option_id == ai_preferred_option_id
    )
    artifact_confirmed = _text(artifact.get("status")).upper() == "CONFIRMED"
    status = (
        "user_supported"
        if action == "support"
        else "user_held"
        if action == "hold"
        else "returned_for_revision"
        if action == "return"
        else "awaiting_user_decision"
        if artifact_confirmed
        else "artifact_not_confirmed"
    )
    return {
        "status": status,
        "decision_id": _text((current or {}).get("id")),
        "artifact_version": _integer(artifact.get("version")),
        "action": action,
        "decision_version": decision_version,
        "ai_preferred_option_id": ai_preferred_option_id,
        "selected_option_id": selected_option_id,
        # Compatibility alias for consumers that have not migrated to v2 yet.
        "preferred_option_id": selected_option_id,
        "selected_is_ai_preferred": selected_is_ai_preferred,
        "is_current": current is not None,
        "artifact_binding_integrity_ok": bool(
            current and current.get("artifact_binding_integrity_ok") is True
        ),
        "governance_attestation_integrity_ok": bool(
            current and current.get("governance_attestation_integrity_ok") is True
        ),
        "matches_projected_candidate": matches_projected if current else False,
        "history_count": len(user_decisions),
        "final_authority": "user",
        "execution_capability": EXECUTION_CAPABILITY,
        "live_trading_allowed": LIVE_TRADING_ALLOWED,
        "can_autonomously_decide": CAN_AUTONOMOUSLY_DECIDE,
    }


def _layer_semantics(
    candidate_lineage: Mapping[str, Any],
    candidate_risk_reviews: Mapping[str, Any],
    user_decision_state: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "candidate_lineage": {
            "ready": candidate_lineage.get("ready") is True,
            "meaning": "candidate_source_and_exact_revision_only",
            "does_not_imply_risk_review": True,
            "does_not_imply_user_decision": True,
        },
        "candidate_risk_review": {
            "applicable": candidate_risk_reviews.get("applicable") is True,
            "ready": candidate_risk_reviews.get("ready") is True,
            "meaning": "structured_risk_dispositions_only",
            "support_challenge_reject_are_dispositions_only": True,
            "does_not_imply_approval": True,
            "does_not_imply_user_decision": True,
            "does_not_authorize_execution": True,
        },
        "user_decision": {
            "ready": user_decision_state.get("is_current") is True,
            "status": _text(user_decision_state.get("status")),
            "meaning": "final_human_disposition",
            "final_authority": "user",
            "does_not_authorize_execution": True,
        },
    }


def _attested_governance_payload(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "version": ARTIFACT_GOVERNANCE_VERSION,
        "artifact": copy.deepcopy(_mapping(snapshot.get("artifact"))),
        "round": copy.deepcopy(_mapping(snapshot.get("round"))),
        "projection_sha256": _text(snapshot.get("projection_sha256")),
        "candidate_lineage": copy.deepcopy(_mapping(snapshot.get("candidate_lineage"))),
        "candidate_risk_reviews": copy.deepcopy(
            _mapping(snapshot.get("candidate_risk_reviews"))
        ),
        "artifact_alignment": copy.deepcopy(_mapping(snapshot.get("artifact_alignment"))),
        "semantics": copy.deepcopy(_mapping(snapshot.get("semantics"))),
        "ready": snapshot.get("ready") is True,
        "issues": copy.deepcopy(_records(snapshot.get("issues"))),
        "execution_capability": EXECUTION_CAPABILITY,
        "live_trading_allowed": LIVE_TRADING_ALLOWED,
        "can_autonomously_decide": CAN_AUTONOMOUSLY_DECIDE,
    }


def build_governance_attestation(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    if snapshot.get("applicable") is not True:
        raise ValueError("治理证明只适用于绑定正式发言合同的产物")
    alignment = _mapping(snapshot.get("artifact_alignment"))
    lineage = _mapping(snapshot.get("candidate_lineage"))
    risk_reviews = _mapping(snapshot.get("candidate_risk_reviews"))
    if (
        snapshot.get("ready") is not True
        or snapshot.get("issues")
        or alignment.get("ready") is not True
        or lineage.get("ready") is not True
        or risk_reviews.get("ready") is not True
    ):
        raise ValueError("治理投影尚未闭环，不能生成确认凭证")
    projection = _mapping(snapshot.get("projection"))
    projection_sha256 = _text(snapshot.get("projection_sha256")).lower()
    if (
        not projection
        or not _is_sha256(projection_sha256)
        or canonical_governance_sha256(projection) != projection_sha256
    ):
        raise ValueError("Governance projection does not match projection_sha256")
    artifact = _mapping(snapshot.get("artifact"))
    round_info = _mapping(snapshot.get("round"))
    base = {
        "attestation_version": ARTIFACT_GOVERNANCE_ATTESTATION_VERSION,
        "evaluator_version": ROUND_GOVERNANCE_EVALUATOR_VERSION,
        "artifact_id": _text(artifact.get("artifact_id")),
        "artifact_version": _integer(artifact.get("artifact_version")),
        "room_id": _text(artifact.get("room_id")),
        "round_id": _text(round_info.get("round_id")),
        "turn_contract_version": _text(round_info.get("turn_contract_version")),
        "candidate_risk_review_version": _text(
            round_info.get("candidate_risk_review_version")
        ),
        "round_governance_input_sha256": _text(
            round_info.get("round_governance_input_sha256")
        ),
        "projection_sha256": _text(snapshot.get("projection_sha256")),
        "artifact_binding_sha256": _text(
            snapshot.get("artifact_binding_sha256")
        ),
        "governance_snapshot_sha256": canonical_governance_sha256(
            _attested_governance_payload(snapshot)
        ),
        "execution_capability": EXECUTION_CAPABILITY,
        "live_trading_allowed": LIVE_TRADING_ALLOWED,
        "can_autonomously_decide": CAN_AUTONOMOUSLY_DECIDE,
    }
    if not all((
        base["artifact_id"],
        base["room_id"],
        base["round_id"],
        base["artifact_version"] > 0,
        _is_sha256(base["round_governance_input_sha256"]),
        _is_sha256(base["projection_sha256"]),
        _is_sha256(base["artifact_binding_sha256"]),
    )):
        raise ValueError("治理证明缺少精确版本或 SHA-256 绑定")
    return {
        **base,
        "attestation_sha256": canonical_governance_sha256(base),
    }


def verify_governance_attestation(
    snapshot: Mapping[str, Any],
    attestation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(attestation, Mapping):
        return {
            "valid": False,
            "issues": ["GOVERNANCE_ATTESTATION_MISSING"],
            "expected": None,
        }
    supplied = copy.deepcopy(dict(attestation))
    supplied_sha256 = _text(supplied.pop("attestation_sha256", ""))
    issues: list[str] = []
    if supplied.get("attestation_version") != ARTIFACT_GOVERNANCE_ATTESTATION_VERSION:
        issues.append("GOVERNANCE_ATTESTATION_VERSION_INVALID")
    if supplied.get("evaluator_version") != ROUND_GOVERNANCE_EVALUATOR_VERSION:
        issues.append("GOVERNANCE_EVALUATOR_VERSION_INVALID")
    if canonical_governance_sha256(supplied) != supplied_sha256:
        issues.append("GOVERNANCE_ATTESTATION_SHA256_MISMATCH")
    try:
        expected = build_governance_attestation(snapshot)
    except (TypeError, ValueError):
        expected = None
        issues.append("GOVERNANCE_EXPECTED_ATTESTATION_UNAVAILABLE")
    if expected is not None and {
        **supplied,
        "attestation_sha256": supplied_sha256,
    } != expected:
        issues.append("GOVERNANCE_ATTESTATION_BINDING_MISMATCH")
    return {
        "valid": not issues,
        "issues": list(dict.fromkeys(issues)),
        "expected": expected,
    }


def build_governance_snapshot(
    artifact: Mapping[str, Any],
    *,
    round_metadata: Mapping[str, Any] | None = None,
    bundle_projection: Mapping[str, Any] | None = None,
    user_decisions: Sequence[Mapping[str, Any]] | None = None,
    attestation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    artifact_copy = copy.deepcopy(dict(artifact))
    metadata = _mapping(round_metadata)
    projection = _mapping(
        bundle_projection
        if isinstance(bundle_projection, Mapping)
        else metadata.get("projection")
    )
    round_id = _text(artifact_copy.get("round_id"))
    applicable = metadata.get("applicable") is True
    metadata_status = _text(metadata.get("status"))
    artifact_status = _text(artifact_copy.get("status") or "DRAFT").upper()
    artifact_info = {
        "artifact_id": _text(artifact_copy.get("id")),
        "artifact_version": _integer(artifact_copy.get("version")),
        "room_id": _text(artifact_copy.get("room_id")),
        "round_id": round_id,
        "status": artifact_status,
    }
    round_info = {
        "round_id": round_id,
        "round_status": _text(metadata.get("round_status")).upper(),
        "turn_contract_version": _text(metadata.get("turn_contract_version")),
        "candidate_risk_review_version": _text(
            metadata.get("candidate_risk_review_version")
        ),
        "candidate_risk_review_required": (
            metadata.get("candidate_risk_review_required") is True
        ),
        "bundle_integrity_ok": metadata.get("bundle_valid") is True,
        "round_governance_input_sha256": _text(
            metadata.get("round_governance_input_sha256")
        ),
    }
    semantics = {
        "risk_reviews_are_dispositions_only": True,
        "risk_review_is_user_decision": RISK_DISPOSITIONS_ARE_USER_DECISIONS,
        "risk_review_is_approval": False,
        "risk_review_is_veto": False,
        "risk_review_is_execution_authority": False,
        "final_authority": "user",
    }

    if not round_id:
        status = "not_round_bound"
        applicable = False
    elif not applicable:
        status = "legacy_unavailable"
    else:
        status = "blocked"

    if not applicable:
        candidate_lineage = {
            "version": CANDIDATE_LINEAGE_VERSION,
            "applicable": False,
            "ready": True,
            "status": "not_applicable",
            "decision_message_id": "",
            "referenced_candidate_ids": [],
            "candidates": [],
            "issues": [],
        }
        candidate_risk_reviews = {
            "version": CANDIDATE_RISK_REVIEW_VERSION,
            "applicable": False,
            "ready": True,
            "status": "not_applicable",
            "reviews": [],
            "issues": [],
            "review_actions_are_dispositions_only": True,
            "execution_capability": EXECUTION_CAPABILITY,
            "live_trading_allowed": LIVE_TRADING_ALLOWED,
            "can_autonomously_decide": CAN_AUTONOMOUSLY_DECIDE,
        }
        user_decision_state = _user_decision_state(
            artifact_copy,
            list(user_decisions or []),
            _text(
                _mapping(
                    _mapping(artifact_copy.get("content")).get("decision")
                ).get("preferred_option_id")
            ),
            [
                _text(option.get("id"))
                for option in _records(
                    _mapping(
                        _mapping(artifact_copy.get("content")).get("decision")
                    ).get("options")
                )
                if _text(option.get("id"))
            ],
        )
        snapshot = {
            "version": ARTIFACT_GOVERNANCE_VERSION,
            "source": "server_reprojection",
            "applicable": False,
            "ready": True,
            "status": status,
            "integrity_ok": True,
            "attestation_integrity_ok": True,
            "artifact": artifact_info,
            "round": round_info,
            "projection": {},
            "projection_sha256": "",
            "candidate_lineage": candidate_lineage,
            "candidate_risk_reviews": candidate_risk_reviews,
            "artifact_alignment": {
                "applicable": False,
                "ready": True,
                "status": "not_applicable",
                "issues": [],
            },
            "user_decision_state": user_decision_state,
            "semantics": semantics,
            "layer_semantics": _layer_semantics(
                candidate_lineage,
                candidate_risk_reviews,
                user_decision_state,
            ),
            "issues": [],
            "execution_capability": EXECUTION_CAPABILITY,
            "live_trading_allowed": LIVE_TRADING_ALLOWED,
            "can_autonomously_decide": CAN_AUTONOMOUSLY_DECIDE,
        }
        snapshot["artifact_binding_sha256"] = canonical_governance_sha256(
            artifact_binding_payload(artifact_copy)
        )
        snapshot["snapshot_sha256"] = canonical_governance_sha256(snapshot)
        return snapshot

    issues: list[dict[str, Any]] = []
    if metadata.get("bundle_valid") is not True:
        issues.append(_issue(
            "ROUND_GOVERNANCE_BUNDLE_INVALID",
            "封印轮次未通过完整性复核。",
        ))
        for raw_issue in metadata.get("bundle_issues") or []:
            issues.append(_issue(
                _issue_code(raw_issue, "ROUND_GOVERNANCE_BUNDLE_ISSUE"),
                _issue_message(raw_issue, "封印轮次存在完整性问题。"),
            ))
    for raw_issue in metadata.get("projection_issues") or []:
        issues.append(_issue(
            _issue_code(raw_issue, "ROUND_GOVERNANCE_PROJECTION_INVALID"),
            _issue_message(raw_issue, "封印轮次治理投影失败。"),
        ))
    if not projection:
        issues.append(_issue(
            "ROUND_GOVERNANCE_PROJECTION_MISSING",
            "封印轮次缺少确定性治理投影。",
        ))
    if round_info["round_status"] and round_info["round_status"] != "COMPLETED":
        issues.append(_issue(
            "ROUND_GOVERNANCE_ROUND_NOT_COMPLETED",
            "绑定轮次尚未完成，不能形成确认治理证明。",
        ))

    candidate_lineage = _normalize_candidate_lineage(
        projection.get("candidate_lineage")
    )
    candidate_risk_reviews = _normalize_candidate_risk_reviews(
        projection.get("candidate_risk_reviews")
    )
    if candidate_lineage.get("ready") is not True:
        issues.append(_issue(
            "CANDIDATE_LINEAGE_NOT_READY",
            "候选形成谱系尚未闭环。",
        ))
    if candidate_risk_reviews.get("ready") is not True:
        issues.append(_issue(
            "CANDIDATE_RISK_REVIEW_NOT_READY",
            "精确版本风控复核尚未闭环。",
        ))
    alignment = _artifact_alignment(artifact_copy, projection) if projection else {
        "applicable": True,
        "ready": False,
        "status": "projection_missing",
        "issues": [_issue(
            "ARTIFACT_GOVERNANCE_PROJECTION_MISSING",
            "没有权威投影可用于核对纪要决策。",
        )],
    }
    issues.extend(_records(alignment.get("issues")))
    projection_sha256 = (
        canonical_governance_sha256(projection) if projection else ""
    )
    projected_decision = _mapping(projection.get("decision"))
    projected_preferred_option_id = _text(
        projected_decision.get("preferred_option_id")
    )
    projected_candidate_ids = [
        _text(option.get("id"))
        for option in _records(projected_decision.get("options"))
        if _text(option.get("id"))
    ]
    user_decision_state = _user_decision_state(
        artifact_copy,
        list(user_decisions or []),
        projected_preferred_option_id,
        projected_candidate_ids,
    )
    snapshot: dict[str, Any] = {
        "version": ARTIFACT_GOVERNANCE_VERSION,
        "source": "server_reprojection",
        "applicable": True,
        "ready": False,
        "status": "blocked",
        "integrity_ok": False,
        "attestation_integrity_ok": False,
        "artifact": artifact_info,
        "round": round_info,
        "projection": projection,
        "projection_sha256": projection_sha256,
        "candidate_lineage": candidate_lineage,
        "candidate_risk_reviews": candidate_risk_reviews,
        "artifact_alignment": alignment,
        "user_decision_state": user_decision_state,
        "semantics": semantics,
        "layer_semantics": _layer_semantics(
            candidate_lineage,
            candidate_risk_reviews,
            user_decision_state,
        ),
        "issues": issues,
        "execution_capability": EXECUTION_CAPABILITY,
        "live_trading_allowed": LIVE_TRADING_ALLOWED,
        "can_autonomously_decide": CAN_AUTONOMOUSLY_DECIDE,
    }
    snapshot["artifact_binding_sha256"] = canonical_governance_sha256(
        artifact_binding_payload(artifact_copy)
    )
    foundation_ready = bool(
        not issues
        and candidate_lineage.get("ready") is True
        and candidate_risk_reviews.get("ready") is True
        and alignment.get("ready") is True
    )
    snapshot["ready"] = foundation_ready
    verification = verify_governance_attestation(snapshot, attestation)
    attestation_valid = verification["valid"] is True
    snapshot["attestation_version"] = _text(
        (attestation or {}).get("attestation_version")
        if isinstance(attestation, Mapping)
        else ""
    )
    snapshot["attestation_sha256"] = _text(
        (attestation or {}).get("attestation_sha256")
        if isinstance(attestation, Mapping)
        else ""
    )
    snapshot["attestation_integrity_ok"] = attestation_valid
    snapshot["attestation_issues"] = verification["issues"]
    if foundation_ready and artifact_status != "CONFIRMED":
        snapshot["status"] = "ready_to_attest"
    elif foundation_ready and attestation_valid:
        snapshot["status"] = "ready"
    elif foundation_ready and not isinstance(attestation, Mapping):
        snapshot["status"] = "legacy_unattested"
    elif foundation_ready:
        snapshot["status"] = "governance_drift"
    snapshot["integrity_ok"] = bool(
        foundation_ready
        and (artifact_status != "CONFIRMED" or attestation_valid)
    )
    snapshot["snapshot_sha256"] = canonical_governance_sha256(snapshot)
    return snapshot


def governance_blocking_issue_codes(snapshot: Mapping[str, Any]) -> tuple[str, ...]:
    if snapshot.get("applicable") is not True:
        return ()
    codes: list[str] = []
    for item in snapshot.get("issues") or []:
        code = _issue_code(item, "ARTIFACT_GOVERNANCE_INVALID")
        if code not in codes:
            codes.append(code)
    alignment = _mapping(snapshot.get("artifact_alignment"))
    for item in alignment.get("issues") or []:
        code = _issue_code(item, "ARTIFACT_GOVERNANCE_ALIGNMENT_INVALID")
        if code not in codes:
            codes.append(code)
    if _mapping(snapshot.get("candidate_lineage")).get("ready") is not True:
        if "CANDIDATE_LINEAGE_NOT_READY" not in codes:
            codes.append("CANDIDATE_LINEAGE_NOT_READY")
    if _mapping(snapshot.get("candidate_risk_reviews")).get("ready") is not True:
        if "CANDIDATE_RISK_REVIEW_NOT_READY" not in codes:
            codes.append("CANDIDATE_RISK_REVIEW_NOT_READY")
    if (
        _text(_mapping(snapshot.get("artifact")).get("status")).upper()
        == "CONFIRMED"
        and snapshot.get("attestation_integrity_ok") is not True
    ):
        for code in snapshot.get("attestation_issues") or [
            "GOVERNANCE_ATTESTATION_INVALID"
        ]:
            clean_code = _issue_code(code, "GOVERNANCE_ATTESTATION_INVALID")
            if clean_code not in codes:
                codes.append(clean_code)
    return tuple(codes)
