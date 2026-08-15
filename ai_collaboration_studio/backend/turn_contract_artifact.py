from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from .turn_contract import (
    CANDIDATE_LINEAGE_VERSION,
    CANDIDATE_RISK_REVIEW_VERSION,
    TURN_CONTRACT_VERSION,
)


MemberResolver = Callable[[str, int], Mapping[str, Any] | None]

_CANDIDATE_OBJECT_FIELDS = (
    "title",
    "symbol",
    "direction",
    "horizon_days",
    "thesis",
    "invalidation",
)
_CANDIDATE_RISK_REVIEW_ACTIONS = ("support", "challenge", "reject")


def decision_candidate_prompt_snapshot(
    messages: list[dict[str, Any]],
    *,
    target_member: Mapping[str, Any],
    member_resolver: MemberResolver,
) -> dict[str, Any] | None:
    """Return the canonical read-only candidate slate for a decision turn.

    Only qualified, integrity-checked formal contracts contribute. Decision
    messages are references and therefore never become candidate sources. The
    same accumulator used by artifact projection defines object creation and
    revision semantics, so prompt construction cannot drift from convergence.
    """

    if not _is_decision_member(target_member):
        return None
    candidates: dict[str, dict[str, Any]] = {}
    risk_reviews: list[dict[str, Any]] = []
    for message in messages:
        if not _qualified_message(message):
            continue
        member = _resolve_member(message, member_resolver)
        if _is_decision_member(member):
            continue
        if _is_risk_member(member):
            _capture_candidate_risk_reviews(
                risk_reviews,
                [],
                candidates=candidates,
                message=message,
                member=member,
            )
        _accumulate_candidates(candidates, message)
    if len(candidates) > 8:
        return {
            "version": CANDIDATE_LINEAGE_VERSION,
            "candidate_risk_review_version": CANDIDATE_RISK_REVIEW_VERSION,
            "read_only": True,
            "ready": False,
            "candidate_count": len(candidates),
            "immutable_fields": ["id", *_CANDIDATE_OBJECT_FIELDS],
            "source_message_ids": [],
            "candidates": [],
            "issues": [{
                "code": "CANDIDATE_PROMPT_CAPACITY_EXCEEDED",
                "message": "合格候选超过 8 项；服务端拒绝静默截断规范候选快照。",
            }],
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

    source_message_ids: list[str] = []
    snapshots: list[dict[str, Any]] = []
    for candidate_id, candidate in candidates.items():
        source_message_ids.extend(
            str(message_id)
            for message_id in candidate.get("_source_message_ids") or []
            if str(message_id)
        )
        contract_snapshot = candidate.get("_contract_snapshot") or {}
        candidate_revision = int(candidate.get("_revision") or 1)
        current_risk_reviews = [
            {
                "review_message_id": str(review.get("review_message_id") or ""),
                "action": str(review.get("action") or ""),
                "reviewer_member_id": str(review.get("reviewer_member_id") or ""),
                "candidate_revision": int(review.get("candidate_revision") or 0),
            }
            for review in risk_reviews
            if (
                str(review.get("candidate_id") or "") == candidate_id
                and int(review.get("candidate_revision") or 0) == candidate_revision
                and review.get("candidate_snapshot") == contract_snapshot
            )
        ]
        snapshots.append({
            "id": candidate_id,
            "revision": candidate_revision,
            "origin_message_id": str(candidate.get("_origin_message_id") or ""),
            "latest_message_id": str(candidate.get("_latest_message_id") or ""),
            **{
                field: copy.deepcopy(contract_snapshot.get(field))
                for field in _CANDIDATE_OBJECT_FIELDS
            },
            "current_risk_reviews": current_risk_reviews,
        })
    return {
        "version": CANDIDATE_LINEAGE_VERSION,
        "candidate_risk_review_version": CANDIDATE_RISK_REVIEW_VERSION,
        "read_only": True,
        "ready": True,
        "candidate_count": len(snapshots),
        "immutable_fields": ["id", *_CANDIDATE_OBJECT_FIELDS],
        "source_message_ids": list(dict.fromkeys(source_message_ids)),
        "candidates": snapshots,
        "issues": [],
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


def candidate_risk_review_prompt_snapshot(
    messages: list[dict[str, Any]],
    *,
    target_member: Mapping[str, Any],
    member_resolver: MemberResolver,
) -> dict[str, Any] | None:
    """Return the canonical, read-only candidate slate only to a risk role.

    The snapshot intentionally reuses the artifact candidate accumulator.  A
    risk reviewer therefore sees the exact object fields and server revision
    that later projection will bind.  A disposition is review metadata only;
    it never selects a candidate or authorizes execution.
    """

    if not _is_risk_member(target_member):
        return None
    candidates: dict[str, dict[str, Any]] = {}
    for message in messages:
        if not _qualified_message(message):
            continue
        member = _resolve_member(message, member_resolver)
        if _is_decision_member(member):
            continue
        _accumulate_candidates(candidates, message)
    if len(candidates) > 8:
        return {
            "version": CANDIDATE_RISK_REVIEW_VERSION,
            "read_only": True,
            "ready": False,
            "candidate_count": len(candidates),
            "immutable_fields": ["id", *_CANDIDATE_OBJECT_FIELDS],
            "allowed_review_actions": list(_CANDIDATE_RISK_REVIEW_ACTIONS),
            "responds_to_required": "candidate.latest_message_id",
            "source_message_ids": [],
            "candidates": [],
            "issues": [{
                "code": "CANDIDATE_RISK_REVIEW_PROMPT_CAPACITY_EXCEEDED",
                "message": "合格候选超过 8 项；服务端拒绝静默截断风控候选快照。",
            }],
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
        }

    source_message_ids: list[str] = []
    snapshots: list[dict[str, Any]] = []
    for candidate_id, candidate in candidates.items():
        source_message_ids.extend(
            str(message_id)
            for message_id in candidate.get("_source_message_ids") or []
            if str(message_id)
        )
        contract_snapshot = candidate.get("_contract_snapshot") or {}
        snapshots.append({
            "id": candidate_id,
            "revision": int(candidate.get("_revision") or 1),
            "origin_message_id": str(candidate.get("_origin_message_id") or ""),
            "latest_message_id": str(candidate.get("_latest_message_id") or ""),
            **{
                field: copy.deepcopy(contract_snapshot.get(field))
                for field in _CANDIDATE_OBJECT_FIELDS
            },
        })
    return {
        "version": CANDIDATE_RISK_REVIEW_VERSION,
        "read_only": True,
        "ready": True,
        "candidate_count": len(snapshots),
        "immutable_fields": ["id", *_CANDIDATE_OBJECT_FIELDS],
        "allowed_review_actions": list(_CANDIDATE_RISK_REVIEW_ACTIONS),
        "responds_to_required": "candidate.latest_message_id",
        "source_message_ids": list(dict.fromkeys(source_message_ids)),
        "candidates": snapshots,
        "issues": [],
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
    }


def project_turn_contract_artifact(
    messages: list[dict[str, Any]],
    *,
    member_resolver: MemberResolver,
    candidate_risk_review_required: bool = False,
) -> dict[str, Any]:
    """Project qualified v1 contracts into a draft artifact workspace.

    The projection never parses visible prose. It only consumes contracts that
    survived persistence integrity checks, and only an exact historical decision
    role may select or defer a candidate.
    """

    qualified = [message for message in messages if _qualified_message(message)]
    candidates: dict[str, dict[str, Any]] = {}
    risks: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    seen_risks: set[tuple[str, str]] = set()
    seen_actions: set[tuple[str, str]] = set()
    authoritative_decision: tuple[
        dict[str, Any],
        dict[str, Any],
        dict[str, dict[str, Any]],
    ] | None = None
    risk_reviews: list[dict[str, Any]] = []
    risk_review_issues: list[dict[str, Any]] = []
    frozen_risk_reviews: list[dict[str, Any]] | None = None
    frozen_risk_review_issues: list[dict[str, Any]] | None = None
    post_decision_review_activity: list[dict[str, Any]] = []
    decision_seen = False

    for message in qualified:
        message_id = str(message.get("id") or "")
        contract = message["turn_contract"]
        sender_name = str(message.get("sender_name") or "待分配")[:120] or "待分配"
        member = _resolve_member(message, member_resolver)
        if _is_decision_member(member):
            # A decision turn may only reference the candidate objects that
            # existed before it.  Keep the exact pre-decision snapshot so a
            # later decision cannot create an option or silently rewrite its
            # title/thesis while selecting it.
            authoritative_decision = (message, contract, copy.deepcopy(candidates))
            frozen_risk_reviews = copy.deepcopy(risk_reviews)
            frozen_risk_review_issues = copy.deepcopy(risk_review_issues)
            post_decision_review_activity = []
            decision_seen = True
        else:
            risk_review_count_before = len(risk_reviews)
            if (
                candidate_risk_review_required
                and _is_risk_member(member)
            ):
                _capture_candidate_risk_reviews(
                    risk_reviews,
                    risk_review_issues,
                    candidates=candidates,
                    message=message,
                    member=member,
                )
            if candidate_risk_review_required and decision_seen:
                relevant_actions: set[str] = {
                    str(review.get("action") or "")
                    for review in risk_reviews[risk_review_count_before:]
                    if str(review.get("action") or "")
                }
                for update in contract.get("candidate_updates") or []:
                    if not isinstance(update, dict):
                        continue
                    action = str(update.get("action") or "").strip().lower()
                    candidate_id = str(update.get("id") or "").strip()
                    # A byte-equivalent repeated proposal is explicitly harmless
                    # in the candidate accumulator. Only a genuinely new option
                    # or a revision changes the slate after a decision.
                    if action == "propose" and candidate_id not in candidates:
                        relevant_actions.add(action)
                    elif action == "revise" and candidate_id in candidates:
                        relevant_actions.add(action)
                if relevant_actions:
                    post_decision_review_activity.append({
                        "code": "CANDIDATE_RISK_REVIEW_DECISION_REVISIT_REQUIRED",
                        "path": "decision",
                        "message_id": message_id,
                        "actions": sorted(relevant_actions),
                        "message": (
                            "合格候选或风险复核在最近一次决策发言后发生变化；"
                            "必须由 decision_synthesis 角色重新引用当前快照。"
                        ),
                    })
            _accumulate_candidates(candidates, message)

        for risk in contract.get("risks") or []:
            if not isinstance(risk, dict):
                continue
            risk_id = str(risk.get("id") or "").strip()
            text = str(risk.get("text") or "").strip()[:3000]
            key = (message_id, risk_id)
            if not text or key in seen_risks:
                continue
            seen_risks.add(key)
            severity = str(risk.get("severity") or "unknown").strip().lower()
            if severity == "critical":
                text = f"【严重级别：critical】{text}"[:3000]
            risks.append(
                {
                    "id": _stable_item_id("risk", message_id, risk_id),
                    "text": text,
                    "probability": "unknown",
                    "impact": (
                        "high"
                        if severity == "critical"
                        else severity if severity in {"low", "medium", "high"} else "unknown"
                    ),
                    "blocking": risk.get("blocking") is not False,
                    "trigger": str(risk.get("trigger") or "").strip()[:2000],
                    "mitigation": str(risk.get("mitigation") or "").strip()[:3000],
                    "owner": sender_name,
                    "status": str(risk.get("status") or "open").strip().lower(),
                    "evidence": _projected_evidence(message_id, risk.get("evidence")),
                }
            )

        for action in contract.get("next_actions") or []:
            if not isinstance(action, dict):
                continue
            action_id = str(action.get("id") or "").strip()
            text = str(action.get("text") or "").strip()[:3000]
            key = (message_id, action_id)
            if not text or key in seen_actions:
                continue
            seen_actions.add(key)
            actions.append(
                {
                    "id": _stable_item_id("action", message_id, action_id),
                    "text": text,
                    "owner": str(action.get("owner") or sender_name).strip()[:120] or sender_name,
                    "due": str(action.get("due") or "").strip()[:80],
                    "state": str(action.get("state") or "open").strip().lower(),
                    "evidence": _projected_evidence(message_id, action.get("evidence")),
                }
            )

    decision_candidates, candidate_lineage = _candidate_lineage_projection(
        candidates,
        authoritative_decision,
    )
    candidate_risk_reviews = _candidate_risk_review_projection(
        required=candidate_risk_review_required,
        candidates=(
            authoritative_decision[2]
            if authoritative_decision is not None
            else candidates
        ),
        authoritative=authoritative_decision,
        reviews=(
            frozen_risk_reviews
            if frozen_risk_reviews is not None
            else risk_reviews
        ),
        capture_issues=(
            frozen_risk_review_issues
            if frozen_risk_review_issues is not None
            else risk_review_issues
        ),
        post_decision_issues=post_decision_review_activity,
    )
    if len(decision_candidates) > 8:
        raise ValueError("发言合同候选方案超过产物容量，不能静默截断")
    if len(risks) > 40:
        raise ValueError("发言合同风险登记超过产物容量，不能静默截断")
    if len(actions) > 40:
        raise ValueError("发言合同下一步超过产物容量，不能静默截断")
    decision = _project_decision(
        decision_candidates,
        authoritative_decision,
        lineage_ready=(
            candidate_lineage["ready"]
            and candidate_risk_reviews["ready"]
        ),
    )
    return {
        "version": TURN_CONTRACT_VERSION,
        "qualified_message_count": len(qualified),
        "source_message_ids": [str(message.get("id") or "") for message in qualified],
        "candidate_lineage": candidate_lineage,
        "candidate_risk_reviews": candidate_risk_reviews,
        "risks": risks,
        "actions": actions,
        "decision": decision,
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
    }


def _qualified_message(message: dict[str, Any]) -> bool:
    contract = message.get("turn_contract")
    return bool(
        message.get("sender_type") == "ai"
        and message.get("is_formal_round_turn") is True
        and message.get("turn_contract_version") == TURN_CONTRACT_VERSION
        and message.get("turn_contract_qualified") is True
        and message.get("turn_contract_integrity_ok") is True
        and not (message.get("turn_contract_issues") or [])
        and isinstance(contract, dict)
        and contract.get("version") == TURN_CONTRACT_VERSION
        and contract.get("execution_capability") == "none"
        and contract.get("live_trading_allowed") is False
    )


def _accumulate_candidates(
    candidates: dict[str, dict[str, Any]],
    message: dict[str, Any],
) -> None:
    message_id = str(message.get("id") or "")
    contract = message.get("turn_contract")
    updates = contract.get("candidate_updates") if isinstance(contract, dict) else []
    for update in updates or []:
        if not isinstance(update, dict):
            continue
        candidate_id = str(update.get("id") or "").strip()
        if not candidate_id:
            continue
        action = str(update.get("action") or "").strip().lower()
        snapshot = _candidate_object_snapshot(update)
        projected = candidates.get(candidate_id)
        if projected is None:
            if action != "propose":
                continue
            projected = {
                "id": candidate_id,
                "title": "",
                "description": "",
                "benefits": [],
                "risks": [],
                "value": "",
                "cost": "",
                "timeline": "",
                "dependencies": [],
                "reversibility": "unknown",
                "evidence": [],
                "_contract_snapshot": snapshot,
                "_origin_message_id": message_id,
                "_latest_message_id": message_id,
                "_revision": 1,
                "_revision_history": [{
                    "revision": 1,
                    "latest_message_id": message_id,
                    "snapshot": copy.deepcopy(snapshot),
                }],
                "_source_message_ids": [message_id],
            }
            candidates[candidate_id] = projected
        elif action == "revise":
            projected["_contract_snapshot"] = snapshot
            projected["_latest_message_id"] = message_id
            projected["_revision"] = int(projected.get("_revision") or 1) + 1
            projected.setdefault("_revision_history", []).append({
                "revision": int(projected["_revision"]),
                "latest_message_id": message_id,
                "snapshot": copy.deepcopy(snapshot),
            })
            projected["risks"] = []
        elif action != "propose" or snapshot != projected.get("_contract_snapshot"):
            # support/challenge/reject/select/defer are references, not object
            # mutation operations.  A repeated, byte-equivalent proposal is
            # harmless; any other payload is ignored as an attempted rewrite.
            continue
        if message_id and message_id not in projected.get("_source_message_ids", []):
            projected.setdefault("_source_message_ids", []).append(message_id)
        title = str(update.get("title") or "").strip()[:180]
        thesis = str(update.get("thesis") or "").strip()[:3000]
        invalidation = str(update.get("invalidation") or "").strip()[:500]
        projected["title"] = title
        projected["description"] = thesis
        if invalidation and invalidation not in projected["risks"]:
            projected["risks"].append(invalidation)
        horizon_days = update.get("horizon_days")
        if isinstance(horizon_days, int) and not isinstance(horizon_days, bool):
            projected["timeline"] = f"观察期限：{horizon_days} 天"
        else:
            projected["timeline"] = ""
        _merge_evidence(
            projected["evidence"],
            [{"type": "message", "id": message_id, "role": "context"}],
        )
        _merge_evidence(projected["evidence"], update.get("evidence"))


def _capture_candidate_risk_reviews(
    reviews: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    *,
    candidates: dict[str, dict[str, Any]],
    message: dict[str, Any],
    member: Mapping[str, Any] | None,
) -> None:
    """Bind exact risk-role dispositions to the candidate revision they saw."""

    contract = message.get("turn_contract")
    if not isinstance(contract, dict):
        return
    message_id = str(message.get("id") or "")
    responds_to_ids = {
        str(item.get("id") or "").strip()
        for item in contract.get("responds_to") or []
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    risk_ids = [
        str(item.get("id") or "").strip()
        for item in contract.get("risks") or []
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]
    for update in contract.get("candidate_updates") or []:
        if not isinstance(update, dict):
            continue
        action = str(update.get("action") or "").strip().lower()
        if action not in _CANDIDATE_RISK_REVIEW_ACTIONS:
            continue
        candidate_id = str(update.get("id") or "").strip()
        candidate = candidates.get(candidate_id)
        if candidate is None:
            issues.append({
                "code": "CANDIDATE_RISK_REVIEW_SOURCE_MISSING",
                "path": "candidate_updates",
                "candidate_id": candidate_id,
                "review_message_id": message_id,
                "message": f"风险复核引用的候选 {candidate_id or '（空）'} 尚无合格提案来源。",
            })
            continue

        supplied_snapshot = _candidate_object_snapshot(update)
        current_snapshot = candidate.get("_contract_snapshot") or {}
        if supplied_snapshot != current_snapshot:
            historical_revision = _matching_candidate_revision(
                candidate,
                supplied_snapshot,
            )
            if historical_revision is not None:
                issues.append({
                    "code": "CANDIDATE_RISK_REVIEW_STALE_REFERENCE",
                    "path": "candidate_updates",
                    "candidate_id": candidate_id,
                    "review_message_id": message_id,
                    "referenced_revision": historical_revision,
                    "current_revision": int(candidate.get("_revision") or 1),
                    "message": (
                        f"风险复核引用了候选 {candidate_id} 的旧版本 "
                        f"r{historical_revision}，当前为 r{int(candidate.get('_revision') or 1)}。"
                    ),
                })
            else:
                changed_fields = [
                    field
                    for field in _CANDIDATE_OBJECT_FIELDS
                    if supplied_snapshot.get(field) != current_snapshot.get(field)
                ]
                issues.append({
                    "code": "CANDIDATE_RISK_REVIEW_REWRITE_FORBIDDEN",
                    "path": "candidate_updates",
                    "candidate_id": candidate_id,
                    "review_message_id": message_id,
                    "changed_fields": changed_fields,
                    "message": (
                        f"风险复核只能引用候选 {candidate_id} 的当前只读快照，"
                        f"不能改写字段：{', '.join(changed_fields)}。"
                    ),
                })
            continue

        latest_message_id = str(candidate.get("_latest_message_id") or "")
        if not latest_message_id or latest_message_id not in responds_to_ids:
            issues.append({
                "code": "CANDIDATE_RISK_REVIEW_RESPONSE_TARGET_MISSING",
                "path": "responds_to",
                "candidate_id": candidate_id,
                "review_message_id": message_id,
                "required_message_id": latest_message_id,
                "message": (
                    f"风险复核必须在 responds_to 中明确引用候选 {candidate_id} "
                    f"当前版本的来源消息 {latest_message_id or '（缺失）'}。"
                ),
            })
            continue

        try:
            reviewer_member_version = int(message.get("member_version") or 0)
        except (TypeError, ValueError):
            reviewer_member_version = 0
        reviews.append({
            "candidate_id": candidate_id,
            "candidate_revision": int(candidate.get("_revision") or 1),
            "candidate_latest_message_id": latest_message_id,
            "action": action,
            "disposition_only": True,
            "review_message_id": message_id,
            "reviewer_member_id": str(message.get("sender_id") or ""),
            "reviewer_member_version": reviewer_member_version,
            "reviewer_name": str(message.get("sender_name") or "")[:120],
            "reviewer_stage": str((member or {}).get("workflow_stage") or ""),
            "risk_ids": risk_ids,
            "candidate_snapshot": copy.deepcopy(current_snapshot),
            "candidate_snapshot_sha256": hashlib.sha256(
                json.dumps(
                    current_snapshot,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "status": "current",
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
        })


def _matching_candidate_revision(
    candidate: Mapping[str, Any],
    supplied_snapshot: Mapping[str, Any],
) -> int | None:
    current_revision = int(candidate.get("_revision") or 1)
    for item in candidate.get("_revision_history") or []:
        if not isinstance(item, Mapping):
            continue
        revision = int(item.get("revision") or 0)
        if revision < current_revision and item.get("snapshot") == supplied_snapshot:
            return revision
    return None


def _candidate_risk_review_projection(
    *,
    required: bool,
    candidates: dict[str, dict[str, Any]],
    authoritative: tuple[
        dict[str, Any],
        dict[str, Any],
        dict[str, dict[str, Any]],
    ] | None,
    reviews: list[dict[str, Any]],
    capture_issues: list[dict[str, Any]],
    post_decision_issues: list[dict[str, Any]],
) -> dict[str, Any]:
    """Project server-bound risk dispositions against decision-time revisions."""

    base = {
        "version": CANDIDATE_RISK_REVIEW_VERSION,
        "applicable": bool(required),
        "ready": True,
        "status": "not_required",
        "decision_message_id": "",
        "target_candidate_ids": [],
        "target_candidate_count": 0,
        "reviewed_candidate_count": 0,
        "reviewed_count": 0,
        "review_count": 0,
        "current_review_count": 0,
        "stale_review_count": 0,
        "action_counts": {
            action: 0 for action in _CANDIDATE_RISK_REVIEW_ACTIONS
        },
        "reviews": [],
        "issues": [],
        "review_actions_are_dispositions_only": True,
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
    }
    if not required:
        return base

    issues = copy.deepcopy(capture_issues)
    decision_message_id = ""
    target_ids: list[str] = []
    decision_contract: Mapping[str, Any] = {}
    if authoritative is None:
        # Before a qualified decision exists, every current candidate is a
        # review target. This lets the moderator route an incomplete round
        # back to risk review instead of repeatedly asking decision synthesis
        # to consume candidates that have never been reviewed.
        target_ids = sorted(candidates)
        issues.append({
            "code": "CANDIDATE_RISK_REVIEW_DECISION_MISSING",
            "path": "decision",
            "message": "本轮尚无合格决策发言，无法冻结候选风险复核版本。",
        })
    else:
        decision_message, decision_contract, _ = authoritative
        decision_message_id = str(decision_message.get("id") or "")
        for update in decision_contract.get("candidate_updates") or []:
            if not isinstance(update, dict):
                continue
            candidate_id = str(update.get("id") or "").strip()
            if candidate_id and candidate_id not in target_ids:
                target_ids.append(candidate_id)
        if not target_ids:
            issues.append({
                "code": "CANDIDATE_RISK_REVIEW_TARGET_MISSING",
                "path": "decision.candidate_updates",
                "message": "决策发言没有引用可供风险复核绑定的候选。",
            })
        issues.extend(copy.deepcopy(post_decision_issues))

    projected_reviews: list[dict[str, Any]] = []
    current_by_candidate: dict[str, list[dict[str, Any]]] = {}
    stale_by_candidate: dict[str, list[dict[str, Any]]] = {}
    action_counts = {action: 0 for action in _CANDIDATE_RISK_REVIEW_ACTIONS}
    for source_review in reviews:
        review = copy.deepcopy(source_review)
        candidate_id = str(review.get("candidate_id") or "")
        candidate = candidates.get(candidate_id)
        current_revision = int(candidate.get("_revision") or 1) if candidate else 0
        bound_revision = int(review.get("candidate_revision") or 0)
        is_current = bool(
            candidate is not None
            and bound_revision == current_revision
            and review.get("candidate_snapshot") == candidate.get("_contract_snapshot")
        )
        review["status"] = "current" if is_current else "stale"
        review["current_candidate_revision"] = current_revision
        projected_reviews.append(review)
        action = str(review.get("action") or "")
        if action in action_counts:
            action_counts[action] += 1
        target = current_by_candidate if is_current else stale_by_candidate
        target.setdefault(candidate_id, []).append(review)

    for candidate_id in target_ids:
        candidate = candidates.get(candidate_id)
        if candidate is None:
            issues.append({
                "code": "CANDIDATE_RISK_REVIEW_SOURCE_MISSING",
                "path": "decision.candidate_updates",
                "candidate_id": candidate_id,
                "message": f"决策候选 {candidate_id} 没有可绑定的合格提案来源。",
            })
            continue
        if current_by_candidate.get(candidate_id):
            continue
        stale = stale_by_candidate.get(candidate_id) or []
        if stale:
            issues.append({
                "code": "CANDIDATE_RISK_REVIEW_STALE",
                "path": "candidate_risk_reviews",
                "candidate_id": candidate_id,
                "reviewed_revisions": sorted({
                    int(item.get("candidate_revision") or 0) for item in stale
                }),
                "current_revision": int(candidate.get("_revision") or 1),
                "message": (
                    f"候选 {candidate_id} 已修订为 r{int(candidate.get('_revision') or 1)}；"
                    "旧版本风险复核已失效，必须重新复核。"
                ),
            })
        else:
            issues.append({
                "code": "CANDIDATE_RISK_REVIEW_MISSING",
                "path": "candidate_risk_reviews",
                "candidate_id": candidate_id,
                "current_revision": int(candidate.get("_revision") or 1),
                "message": (
                    f"候选 {candidate_id} 的当前版本 "
                    f"r{int(candidate.get('_revision') or 1)} 尚无有效风险复核。"
                ),
            })

    if authoritative is not None:
        for update in decision_contract.get("candidate_updates") or []:
            if not isinstance(update, dict) or update.get("action") != "select":
                continue
            candidate_id = str(update.get("id") or "").strip()
            valid_review_message_ids = {
                str(item.get("review_message_id") or "")
                for item in current_by_candidate.get(candidate_id) or []
                if str(item.get("review_message_id") or "")
            }
            decision_evidence_message_ids = {
                str(item.get("id") or "").strip()
                for item in update.get("evidence") or []
                if isinstance(item, dict)
                and str(item.get("type") or "").strip().lower() == "message"
                and str(item.get("id") or "").strip()
            }
            if not valid_review_message_ids.intersection(decision_evidence_message_ids):
                issues.append({
                    "code": "CANDIDATE_RISK_REVIEW_DECISION_REFERENCE_MISSING",
                    "path": "decision.candidate_updates.evidence",
                    "candidate_id": candidate_id,
                    "required_review_message_ids": sorted(valid_review_message_ids),
                    "message": (
                        f"决策选择候选 {candidate_id} 时，必须在 evidence 中引用"
                        "该候选当前版本的一条有效风险复核消息。"
                    ),
                })

    reviewed_candidate_count = sum(
        1 for candidate_id in target_ids if current_by_candidate.get(candidate_id)
    )
    current_review_count = sum(
        1 for review in projected_reviews if review.get("status") == "current"
    )
    stale_review_count = len(projected_reviews) - current_review_count
    ready = authoritative is not None and not issues
    return {
        **base,
        "ready": ready,
        "status": (
            "ready"
            if ready
            else "decision_missing"
            if authoritative is None
            else "blocked"
        ),
        "decision_message_id": decision_message_id,
        "target_candidate_ids": target_ids,
        "target_candidate_count": len(target_ids),
        "reviewed_candidate_count": reviewed_candidate_count,
        "reviewed_count": reviewed_candidate_count,
        "review_count": len(projected_reviews),
        "current_review_count": current_review_count,
        "stale_review_count": stale_review_count,
        "action_counts": action_counts,
        "reviews": projected_reviews,
        "issues": issues,
    }


def _candidate_lineage_projection(
    candidates: dict[str, dict[str, Any]],
    authoritative: tuple[
        dict[str, Any],
        dict[str, Any],
        dict[str, dict[str, Any]],
    ] | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    decision_message_id = ""
    referenced_ids: list[str] = []
    available = candidates

    if authoritative is None:
        issues.append({
            "code": "CANDIDATE_LINEAGE_DECISION_MISSING",
            "path": "decision",
            "message": "本轮尚无合格决策角色引用既有候选。",
        })
    else:
        message, contract, available = authoritative
        decision_message_id = str(message.get("id") or "")
        updates = [
            update
            for update in contract.get("candidate_updates") or []
            if isinstance(update, dict)
        ]
        for update in updates:
            candidate_id = str(update.get("id") or "").strip()
            if candidate_id and candidate_id not in referenced_ids:
                referenced_ids.append(candidate_id)
        for candidate_id in referenced_ids:
            if candidate_id not in available:
                issues.append({
                    "code": "CANDIDATE_LINEAGE_SOURCE_MISSING",
                    "path": "decision.candidate_updates",
                    "candidate_id": candidate_id,
                    "message": f"决策引用的候选 {candidate_id} 在该决策发言前没有合格提案来源。",
                })
        for update in updates:
            candidate_id = str(update.get("id") or "").strip()
            prior = available.get(candidate_id)
            if not prior:
                continue
            changed_fields = [
                field
                for field in _CANDIDATE_OBJECT_FIELDS
                if _candidate_field_value(update, field)
                != (prior.get("_contract_snapshot") or {}).get(field)
            ]
            if changed_fields:
                issues.append({
                    "code": "CANDIDATE_LINEAGE_REWRITE_FORBIDDEN",
                    "path": "decision.candidate_updates",
                    "candidate_id": candidate_id,
                    "changed_fields": changed_fields,
                    "message": (
                        f"决策只能引用候选 {candidate_id} 的决策前快照，"
                        f"不能改写字段：{', '.join(changed_fields)}。"
                    ),
                })

    projected_ids = referenced_ids if authoritative is not None else list(available)
    projected = {
        candidate_id: copy.deepcopy(available[candidate_id])
        for candidate_id in projected_ids
        if candidate_id in available
    }
    if authoritative is not None and len(projected) < 2:
        issues.append({
            "code": "CANDIDATE_LINEAGE_COMPARISON_INSUFFICIENT",
            "path": "decision.candidate_updates",
            "message": "决策前至少需要两个具有合格来源的候选对象。",
        })
    lineage_candidates = [
        {
            "id": candidate_id,
            "origin_message_id": str(candidate.get("_origin_message_id") or ""),
            "latest_message_id": str(candidate.get("_latest_message_id") or ""),
            "revision": int(candidate.get("_revision") or 1),
        }
        for candidate_id, candidate in projected.items()
    ]
    ready = authoritative is not None and not issues
    return projected, {
        "version": CANDIDATE_LINEAGE_VERSION,
        "applicable": True,
        "ready": ready,
        "status": "ready" if ready else "decision_missing" if authoritative is None else "blocked",
        "decision_message_id": decision_message_id,
        "referenced_candidate_ids": referenced_ids,
        "candidates": lineage_candidates,
        "issues": issues,
    }


def _candidate_object_snapshot(update: Mapping[str, Any]) -> dict[str, Any]:
    return {
        field: _candidate_field_value(update, field)
        for field in _CANDIDATE_OBJECT_FIELDS
    }


def _candidate_field_value(update: Mapping[str, Any], field: str) -> Any:
    if field == "horizon_days":
        value = update.get(field)
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    if field == "symbol":
        return str(update.get(field) or "").strip().upper()
    if field == "direction":
        return str(update.get(field) or "UNSPECIFIED").strip().upper()
    return str(update.get(field) or "").strip()


def _resolve_member(
    message: dict[str, Any],
    member_resolver: MemberResolver,
) -> Mapping[str, Any] | None:
    frozen_snapshot = message.get("member_snapshot")
    if (
        isinstance(frozen_snapshot, Mapping)
        and message.get("member_snapshot_integrity_ok") is True
    ):
        return frozen_snapshot
    sender_id = str(message.get("sender_id") or "")
    try:
        member_version = int(message.get("member_version") or 0)
    except (TypeError, ValueError):
        return None
    if not sender_id or member_version < 1:
        return None
    try:
        return member_resolver(sender_id, member_version)
    except (TypeError, ValueError):
        return None


def _is_decision_member(member: Mapping[str, Any] | None) -> bool:
    if not isinstance(member, Mapping):
        return False
    capabilities = {
        str(item or "").strip().lower()
        for item in member.get("capabilities") or []
        if str(item or "").strip()
    }
    return bool(
        str(member.get("workflow_stage") or "").strip().lower() == "decision"
        or str(member.get("stance") or "").strip().lower() == "portfolio_manager"
        or "decision_synthesis" in capabilities
    )


def _is_risk_member(member: Mapping[str, Any] | None) -> bool:
    if not isinstance(member, Mapping):
        return False
    capabilities = {
        str(item or "").strip().lower()
        for item in member.get("capabilities") or []
        if str(item or "").strip()
    }
    return bool(
        str(member.get("workflow_stage") or "").strip().lower() == "risk"
        or str(member.get("stance") or "").strip().lower() == "risk"
        or "risk_review" in capabilities
    )


def _project_decision(
    candidates: dict[str, dict[str, Any]],
    authoritative: tuple[
        dict[str, Any],
        dict[str, Any],
        dict[str, dict[str, Any]],
    ] | None,
    *,
    lineage_ready: bool,
) -> dict[str, Any]:
    options = [
        _public_candidate(candidate)
        for candidate in candidates.values()
        if str(candidate.get("title") or "").strip()
        and str(candidate.get("description") or "").strip()
    ]
    option_ids = {str(option.get("id") or "") for option in options}
    status = "undecided"
    preferred_option_id = ""
    rationale = ""
    evidence: list[dict[str, Any]] = []

    if authoritative and lineage_ready:
        message, contract, _ = authoritative
        message_id = str(message.get("id") or "")
        updates = [
            update
            for update in contract.get("candidate_updates") or []
            if isinstance(update, dict)
        ]
        selected = [update for update in updates if update.get("action") == "select"]
        deferred = [update for update in updates if update.get("action") == "defer"]
        if len(options) >= 2 and len(selected) == 1 and len(deferred) == 0:
            selected_id = str(selected[0].get("id") or "")
            if selected_id in option_ids:
                status = "candidate"
                preferred_option_id = selected_id
                rationale = _decision_rationale(selected[0], prefix="选择依据")
                evidence = _projected_evidence(message_id, selected[0].get("evidence"))
        elif len(options) >= 2 and len(deferred) == 1 and len(selected) == 0:
            status = "deferred"
            rationale = _decision_rationale(deferred[0], prefix="暂缓依据")
            evidence = _projected_evidence(message_id, deferred[0].get("evidence"))

    return {
        "status": status,
        "options": options,
        "preferred_option_id": preferred_option_id,
        "rationale": rationale,
        "evidence": evidence,
    }


def _public_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in candidate.items()
        if not str(key).startswith("_")
    } | {
        "lineage": {
            "version": CANDIDATE_LINEAGE_VERSION,
            "origin_message_id": str(candidate.get("_origin_message_id") or ""),
            "latest_message_id": str(candidate.get("_latest_message_id") or ""),
            "revision": int(candidate.get("_revision") or 1),
        }
    }


def _decision_rationale(update: dict[str, Any], *, prefix: str) -> str:
    thesis = str(update.get("thesis") or "").strip()
    invalidation = str(update.get("invalidation") or "").strip()
    parts = [f"{prefix}：{thesis}" if thesis else ""]
    if invalidation:
        parts.append(f"失效条件：{invalidation}")
    return "；".join(part for part in parts if part)[:3000]


def _projected_evidence(message_id: str, raw: Any) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    _merge_evidence(
        projected,
        [{"type": "message", "id": message_id, "role": "context"}],
    )
    _merge_evidence(projected, raw)
    return projected


def _merge_evidence(target: list[dict[str, Any]], raw: Any) -> None:
    seen = {(str(item.get("type") or ""), str(item.get("id") or "")) for item in target}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        source_type = str(item.get("type") or "").strip().lower()
        source_id = str(item.get("id") or "").strip()
        key = (source_type, source_id)
        if (
            source_type not in {"message", "material", "round_market_snapshot"}
            or not source_id
            or key in seen
        ):
            continue
        role = str(item.get("role") or "context").strip().lower()
        target.append(
            {
                "type": source_type,
                "id": source_id,
                "evidence_role": role if role in {"support", "counter", "context"} else "context",
                "verification_status": "unreviewed",
            }
        )
        seen.add(key)


def _stable_item_id(prefix: str, message_id: str, item_id: str) -> str:
    raw = f"{prefix}_{message_id}_{item_id}"
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    if clean and clean[0].isalpha() and len(clean) <= 80:
        return clean
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"
