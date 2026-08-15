from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any


DISCUSSION_AUDIT_VERSION = "discussion_audit_v1"
ROUND_EXECUTION_TRACE_VERSION = "round_execution_trace_v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,159}$")
_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_SECRET_LIKE_RE = re.compile(
    r"(?i)(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|(?:api|access|secret)[_-]?key)"
)
_FALLBACK_SOURCES = {
    "fallback",
    "safe_fallback",
    "director_circuit_breaker",
    "director_call_budget_exhausted",
    "provider_call_budget_reserve",
    "provider_call_budget_exhausted",
}


class DiscussionAuditConflict(ValueError):
    """Raised when the verified source projections cannot be combined safely."""

    def __init__(self, code: str) -> None:
        clean_code = _public_code(code, upper=True)
        self.code = clean_code or "DISCUSSION_AUDIT_INPUT_CONFLICT"
        super().__init__("discussion audit inputs failed integrity checks")


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _raw_id(value: Any) -> str:
    return str(value or "").strip()


def _public_id(value: Any) -> str:
    raw = _raw_id(value)
    if not raw:
        return ""
    if (
        _PUBLIC_ID_RE.fullmatch(raw)
        and not _SECRET_LIKE_RE.search(raw)
    ):
        return raw
    return "opaque_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _public_code(value: Any, *, upper: bool = False) -> str:
    raw = str(value or "").strip()
    if not raw or not _CODE_RE.fullmatch(raw) or _SECRET_LIKE_RE.search(raw):
        return ""
    return raw.upper() if upper else raw.lower()


def _issue_codes(value: Any) -> list[str]:
    result: list[str] = []
    for item in _items(value):
        raw = item.get("code") if isinstance(item, Mapping) else item
        code = _public_code(raw, upper=True)
        if code and code not in result:
            result.append(code)
    return result


def _safe_int(value: Any, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        return minimum
    try:
        return max(minimum, int(value or 0))
    except (TypeError, ValueError):
        return minimum


def _is_nonnegative_int(value: Any) -> bool:
    return type(value) is int and value >= 0


def _verify_inputs(
    trace: Mapping[str, Any],
    turn_contract_bundle: Mapping[str, Any],
    *,
    expected_room_id: str,
    expected_round_id: str,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    if str(trace.get("version") or "") != ROUND_EXECUTION_TRACE_VERSION:
        raise DiscussionAuditConflict("TRACE_VERSION_UNSUPPORTED")

    trace_room_id = _raw_id(trace.get("room_id"))
    trace_round_id = _raw_id(trace.get("round_id"))
    if (
        not trace_room_id
        or not trace_round_id
        or trace_room_id != expected_room_id
        or trace_round_id != expected_round_id
    ):
        raise DiscussionAuditConflict("TRACE_SCOPE_MISMATCH")

    trace_hash = str(trace.get("trace_hash") or "").strip().lower()
    if not _SHA256_RE.fullmatch(trace_hash):
        raise DiscussionAuditConflict("TRACE_HASH_INVALID")

    integrity = _mapping(trace.get("integrity"))
    if (
        integrity.get("ok") is not True
        or str(integrity.get("status") or "").strip().lower() == "invalid"
    ):
        raise DiscussionAuditConflict("TRACE_INTEGRITY_INVALID")

    safety = _mapping(trace.get("safety"))
    if (
        safety.get("read_only") is not True
        or type(safety.get("provider_calls_performed")) is not int
        or safety.get("provider_calls_performed") != 0
        or str(safety.get("execution_capability") or "") != "none"
        or safety.get("live_trading_allowed") is not False
    ):
        raise DiscussionAuditConflict("TRACE_SAFETY_INVALID")

    events = [item for item in _items(trace.get("events")) if isinstance(item, Mapping)]
    if len(events) != len(_items(trace.get("events"))):
        raise DiscussionAuditConflict("TRACE_EVENT_SHAPE_INVALID")
    page = _mapping(trace.get("page"))
    summary = _mapping(trace.get("summary"))
    if (
        not _is_nonnegative_int(page.get("cursor"))
        or page.get("cursor") != 0
        or page.get("has_more") is not False
        or not _is_nonnegative_int(page.get("total"))
        or page.get("total") != len(events)
        or not _is_nonnegative_int(summary.get("event_count"))
        or summary.get("event_count") != len(events)
    ):
        raise DiscussionAuditConflict("TRACE_PAGE_INCOMPLETE")
    for event in events:
        event_integrity = _mapping(event.get("integrity"))
        if (
            event_integrity.get("ok") is not True
            or str(event_integrity.get("status") or "").strip().lower() == "invalid"
        ):
            raise DiscussionAuditConflict("TRACE_EVENT_INTEGRITY_INVALID")

    applicable = turn_contract_bundle.get("applicable") is True
    if turn_contract_bundle.get("valid") is not True:
        raise DiscussionAuditConflict("TURN_CONTRACT_BUNDLE_INVALID")
    if applicable and (
        str(turn_contract_bundle.get("execution_capability") or "") != "none"
        or turn_contract_bundle.get("live_trading_allowed") is not False
        or turn_contract_bundle.get("can_autonomously_decide") is not False
    ):
        raise DiscussionAuditConflict("TURN_CONTRACT_SAFETY_INVALID")

    history_mode = str(_mapping(trace.get("history")).get("mode") or "")
    if history_mode == "current_envelope" and not applicable:
        raise DiscussionAuditConflict("TURN_CONTRACT_BUNDLE_NOT_APPLICABLE")

    raw_messages = _items(turn_contract_bundle.get("messages"))
    messages = [item for item in raw_messages if isinstance(item, Mapping)]
    if len(messages) != len(raw_messages) or (not applicable and messages):
        raise DiscussionAuditConflict("TURN_CONTRACT_MESSAGE_SHAPE_INVALID")

    seen_message_ids: set[str] = set()
    seen_turn_ids: set[str] = set()
    source_message_ids: list[str] = []
    for message in sorted(messages, key=lambda item: _safe_int(item.get("turn_order"))):
        message_id = _raw_id(message.get("id"))
        turn_id = _raw_id(message.get("round_turn_id"))
        if (
            not message_id
            or not turn_id
            or message_id in seen_message_ids
            or turn_id in seen_turn_ids
            or _raw_id(message.get("round_id")) != expected_round_id
            or message.get("turn_contract_qualified") is not True
            or message.get("turn_contract_integrity_ok") is not True
            or message.get("member_snapshot_integrity_ok") is not True
        ):
            raise DiscussionAuditConflict("TURN_CONTRACT_SCOPE_OR_INTEGRITY_MISMATCH")
        seen_message_ids.add(message_id)
        seen_turn_ids.add(turn_id)
        source_message_ids.append(message_id)

    projection = trace.get("candidate_projection")
    if messages:
        if not isinstance(projection, Mapping):
            raise DiscussionAuditConflict("TRACE_CANDIDATE_PROJECTION_MISSING")
        projection_source_ids = [
            _raw_id(item) for item in _items(projection.get("source_message_ids"))
        ]
        if (
            _safe_int(projection.get("qualified_message_count")) != len(messages)
            or projection_source_ids != source_message_ids
        ):
            raise DiscussionAuditConflict("TRACE_BUNDLE_DIVERGED")
    elif projection is not None:
        raise DiscussionAuditConflict("TRACE_BUNDLE_DIVERGED")

    reserved_turn_ids: set[str] = set()
    terminal_turn_ids: set[str] = set()
    persisted_message_ids: set[str] = set()
    for event in events:
        event_type = str(event.get("type") or "")
        refs = _mapping(event.get("refs"))
        if event_type == "round_turn_reserved":
            reserved_turn_ids.add(_raw_id(refs.get("round_turn_id")))
        elif event_type == "round_turn_terminal":
            terminal_turn_ids.add(_raw_id(refs.get("round_turn_id")))
        elif event_type == "message_persisted":
            persisted_message_ids.add(_raw_id(refs.get("message_id")))
    if messages and (
        not seen_turn_ids.issubset(reserved_turn_ids)
        or not seen_turn_ids.issubset(terminal_turn_ids)
        or not seen_message_ids.issubset(persisted_message_ids)
    ):
        raise DiscussionAuditConflict("TRACE_BUNDLE_EVENT_LINK_MISSING")
    formal_turn_count = summary.get("formal_turn_count")
    if (
        not _is_nonnegative_int(formal_turn_count)
        or formal_turn_count < len(messages)
    ):
        raise DiscussionAuditConflict("TRACE_BUNDLE_TURN_COUNT_MISMATCH")

    return events, sorted(messages, key=lambda item: _safe_int(item.get("turn_order")))


def _project_selections(events: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    selections: list[dict[str, Any]] = []
    director_events = [
        event for event in events
        if str(event.get("type") or "") == "director_decision_recorded"
    ]
    director_events.sort(key=lambda event: (
        _safe_int(_mapping(event.get("source")).get("sequence_no")),
        _safe_int(event.get("ordinal")),
    ))
    for event in director_events:
        payload = _mapping(event.get("payload"))
        context = _mapping(payload.get("moderator_context"))
        scheduling = _mapping(context.get("scheduling_context"))
        raw_source = str(payload.get("source") or "").strip().lower()
        authority = _public_code(context.get("decision_authority"))
        discussion_mode = _public_code(context.get("discussion_mode")) or "unrecorded"
        action = _public_code(payload.get("action")) or "unrecorded"
        raw_member_id = _raw_id(payload.get("member_id"))
        eligible_ids = [
            _raw_id(item) for item in _items(scheduling.get("eligible_member_ids"))
            if _raw_id(item)
        ]
        gap_codes = [
            code for code in (
                _public_code(item) for item in _items(scheduling.get("gap_codes"))
            ) if code
        ]
        selected_gap_codes = [
            code for code in (
                _public_code(item)
                for item in _items(scheduling.get("selected_gap_codes"))
            ) if code and code in gap_codes
        ]
        selected_contribution: Mapping[str, Any] = {}
        for item in _items(scheduling.get("candidate_contributions")):
            if isinstance(item, Mapping) and _raw_id(item.get("member_id")) == raw_member_id:
                selected_contribution = item
                break
        if not selected_gap_codes and selected_contribution:
            selected_gap_codes = [
                code for code in (
                    _public_code(item)
                    for item in _items(selected_contribution.get("gap_codes"))
                ) if code and code in gap_codes
            ]

        schedule_recorded = (
            str(scheduling.get("version") or "") == "director_scheduling_context_v1"
        )
        context_recorded = (
            str(context.get("version") or "") == "director_moderator_context_v1"
            and authority in {
                "moderator_model",
                "user_direction",
                "service_policy",
                "safety_fallback",
            }
            and type(context.get("model_used")) is bool
        )
        selected_member_eligible = bool(
            action == "finish" or (raw_member_id and raw_member_id in eligible_ids)
        )
        authority_consistent = context.get("model_used") is (
            authority == "moderator_model"
        )
        event_verified = (
            str(_mapping(event.get("integrity")).get("status") or "").lower()
            == "verified"
        )
        if discussion_mode == "dynamic":
            structural_status = (
                "verified"
                if context_recorded
                and schedule_recorded
                and action in {"speak", "finish"}
                and selected_member_eligible
                and authority_consistent
                and event_verified
                else "partial"
            )
        elif discussion_mode == "sequential":
            structural_status = "not_dynamic"
        else:
            structural_status = "unknown"

        fallback = bool(
            authority == "safety_fallback"
            or raw_source in _FALLBACK_SOURCES
            or "fallback" in raw_source
            or "circuit_breaker" in raw_source
            or "budget_exhausted" in raw_source
        )
        refs = _mapping(event.get("refs"))
        source = _mapping(event.get("source"))
        selections.append({
            "sequence_no": _safe_int(source.get("sequence_no")),
            "event_id": _public_id(event.get("event_id")),
            "director_decision_id": _public_id(
                refs.get("director_decision_id") or source.get("id")
            ),
            "action": action,
            "selected_member_id": _public_id(raw_member_id),
            "source": _public_code(raw_source) or "unrecognized",
            "decision_authority": authority or "unrecorded",
            "discussion_mode": discussion_mode,
            "moderator_model_call_recorded": context.get("model_used") is True,
            "fallback": fallback,
            "structural_status": structural_status,
            "scheduling_snapshot": {
                "recorded": schedule_recorded,
                "eligible_member_count": len(eligible_ids),
                "gap_count": len(gap_codes),
                "selected_member_eligible": selected_member_eligible,
                "selected_gap_codes": selected_gap_codes,
            },
        })
    return selections


def _project_response_edges(
    messages: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    message_ids = {_raw_id(message.get("id")) for message in messages}
    edges: list[dict[str, Any]] = []
    for message in messages:
        source_id = _raw_id(message.get("id"))
        reply_target = _raw_id(message.get("reply_to_message_id"))
        contract = _mapping(message.get("turn_contract"))
        seen_targets: set[str] = set()
        for response in _items(contract.get("responds_to")):
            if not isinstance(response, Mapping):
                continue
            target_id = _raw_id(response.get("id"))
            if not target_id or target_id in seen_targets:
                continue
            seen_targets.add(target_id)
            edges.append({
                "from_message_id": _public_id(source_id),
                "to_message_id": _public_id(target_id),
                "target_within_formal_bundle": target_id in message_ids,
                "persisted_reply_target": target_id == reply_target,
                "structurally_verified": True,
                "semantic_causality_status": "unknown",
            })
    return edges


def _project_candidate_checkpoint(
    trace: Mapping[str, Any],
    turn_contract_bundle: Mapping[str, Any],
) -> dict[str, Any]:
    applicable = turn_contract_bundle.get("applicable") is True
    projection = _mapping(trace.get("candidate_projection"))
    lineage = _mapping(projection.get("candidate_lineage"))
    risk_review = _mapping(projection.get("candidate_risk_reviews"))
    decision = _mapping(projection.get("decision"))

    candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in _items(lineage.get("candidates")):
        if not isinstance(item, Mapping):
            continue
        raw_candidate_id = _raw_id(item.get("id"))
        if not raw_candidate_id or raw_candidate_id in seen_ids:
            raise DiscussionAuditConflict("CANDIDATE_LINEAGE_ID_INVALID")
        seen_ids.add(raw_candidate_id)
        candidates.append({
            "id": _public_id(raw_candidate_id),
            "revision": _safe_int(item.get("revision"), minimum=1),
            "origin_message_id": _public_id(item.get("origin_message_id")),
            "latest_message_id": _public_id(item.get("latest_message_id")),
        })

    lineage_ready = lineage.get("ready") is True
    risk_required = bool(
        turn_contract_bundle.get("candidate_risk_review_required") is True
        or risk_review.get("required") is True
    )
    risk_ready = risk_review.get("ready") is True
    decision_status = _public_code(decision.get("status")) or "undecided"
    comparison_count_satisfied = len(candidates) >= 2
    ready = bool(
        applicable
        and comparison_count_satisfied
        and lineage_ready
        and risk_ready
        and decision_status in {"candidate", "deferred"}
    )
    return {
        "applicable": applicable,
        "status": "ready" if ready else "not_applicable" if not applicable else "blocked",
        "ready": ready,
        "candidate_count": len(candidates),
        "minimum_comparison_count": 2,
        "comparison_count_satisfied": comparison_count_satisfied,
        "candidates": candidates,
        "lineage": {
            "status": _public_code(lineage.get("status")) or "not_recorded",
            "ready": lineage_ready,
            "blocker_codes": _issue_codes(lineage.get("issues")),
            "referenced_candidate_ids": [
                _public_id(item)
                for item in _items(lineage.get("referenced_candidate_ids"))
                if _raw_id(item)
            ],
        },
        "risk_review": {
            "required": risk_required,
            "status": _public_code(risk_review.get("status")) or "not_recorded",
            "ready": risk_ready,
            "target_candidate_count": _safe_int(
                risk_review.get("target_candidate_count")
            ),
            "reviewed_candidate_count": _safe_int(
                risk_review.get("reviewed_candidate_count")
            ),
            "review_count": _safe_int(risk_review.get("review_count")),
            "blocker_codes": _issue_codes(risk_review.get("issues")),
        },
        "decision": {
            "status": decision_status,
            "preferred_option_id": _public_id(decision.get("preferred_option_id")),
        },
    }


def project_discussion_audit(
    trace: Mapping[str, Any],
    turn_contract_bundle: Mapping[str, Any],
    *,
    expected_room_id: str,
    expected_round_id: str,
) -> dict[str, Any]:
    """Build a deterministic, non-sensitive audit view from verified reads.

    This function performs no I/O. Structural links are projected from the
    trace and qualified turn contracts; semantic causality remains unknown
    because v1 does not persist an attestation of the model's effective input.
    """

    if not isinstance(trace, Mapping) or not isinstance(turn_contract_bundle, Mapping):
        raise DiscussionAuditConflict("DISCUSSION_AUDIT_INPUT_SHAPE_INVALID")
    clean_room_id = _raw_id(expected_room_id)
    clean_round_id = _raw_id(expected_round_id)
    if not clean_room_id or not clean_round_id:
        raise DiscussionAuditConflict("DISCUSSION_AUDIT_SCOPE_MISSING")

    events, messages = _verify_inputs(
        trace,
        turn_contract_bundle,
        expected_room_id=clean_room_id,
        expected_round_id=clean_round_id,
    )
    history = _mapping(trace.get("history"))
    integrity = _mapping(trace.get("integrity"))
    history_mode = str(history.get("mode") or "")
    selections = _project_selections(events)
    response_edges = _project_response_edges(messages)
    candidate_checkpoint = _project_candidate_checkpoint(
        trace,
        turn_contract_bundle,
    )

    dynamic_selections = [
        item for item in selections if item["discussion_mode"] == "dynamic"
    ]
    if history_mode != "current_envelope":
        structural_status = "legacy_unknown"
    elif dynamic_selections:
        structural_status = (
            "verified"
            if all(item["structural_status"] == "verified" for item in dynamic_selections)
            else "partial"
        )
    elif any(item["discussion_mode"] == "sequential" for item in selections):
        structural_status = "not_dynamic"
    else:
        structural_status = "not_recorded"

    findings: list[dict[str, Any]] = [{
        "code": "SEMANTIC_CAUSALITY_UNKNOWN",
        "severity": "info",
        "scope": "round",
    }]
    if history_mode != "current_envelope":
        findings.append({
            "code": "LEGACY_HISTORY_PARTIAL",
            "severity": "warning",
            "scope": "round",
        })
    elif structural_status in {"not_dynamic", "not_recorded"}:
        findings.append({
            "code": "STRUCTURAL_DYNAMIC_NOT_RECORDED",
            "severity": "warning",
            "scope": "director",
        })
    elif structural_status == "partial":
        findings.append({
            "code": "STRUCTURAL_DYNAMIC_PARTIAL",
            "severity": "warning",
            "scope": "director",
        })
    fallback_count = sum(1 for item in selections if item["fallback"])
    if fallback_count:
        findings.append({
            "code": "FALLBACK_USED",
            "severity": "warning",
            "scope": "director",
            "count": fallback_count,
        })
    if (
        candidate_checkpoint["applicable"]
        and not candidate_checkpoint["comparison_count_satisfied"]
    ):
        findings.append({
            "code": "CANDIDATE_GENERATION_INSUFFICIENT",
            "severity": "warning",
            "scope": "candidate_checkpoint",
            "candidate_count": candidate_checkpoint["candidate_count"],
            "minimum_required": candidate_checkpoint["minimum_comparison_count"],
        })
    elif (
        candidate_checkpoint["applicable"]
        and not candidate_checkpoint["ready"]
    ):
        findings.append({
            "code": "CANDIDATE_CHECKPOINT_BLOCKED",
            "severity": "warning",
            "scope": "candidate_checkpoint",
        })

    audit: dict[str, Any] = {
        "version": DISCUSSION_AUDIT_VERSION,
        "room_id": _public_id(clean_room_id),
        "round_id": _public_id(clean_round_id),
        "source": {
            "trace_version": str(trace.get("version") or ""),
            "trace_hash": str(trace.get("trace_hash") or "").strip().lower(),
            "trace_integrity_status": _public_code(integrity.get("status"))
            or "unknown",
            "trace_integrity_issue_codes": _issue_codes(integrity.get("issues")),
            "turn_contract_applicable": (
                turn_contract_bundle.get("applicable") is True
            ),
            "turn_contract_valid": turn_contract_bundle.get("valid") is True,
            "turn_contract_version": _public_code(
                turn_contract_bundle.get("turn_contract_version")
            ),
        },
        "coverage": {
            "history_mode": _public_code(history_mode) or "unknown",
            "status": _public_code(history.get("coverage")) or "unknown",
            "limitation_codes": _issue_codes(history.get("limitations")),
        },
        "structural": {
            "dynamic_status": structural_status,
            "selection_count": len(selections),
            "dynamic_selection_count": len(dynamic_selections),
            "fallback_count": fallback_count,
            "selections": selections,
            "response_edge_count": len(response_edges),
            "response_edges": response_edges,
        },
        "candidate_checkpoint": candidate_checkpoint,
        "semantic_causality": {
            "status": "unknown",
            "proven": False,
            "reason_code": "EFFECTIVE_MODEL_INPUT_ATTESTATION_UNAVAILABLE",
        },
        "findings": findings,
        "safety": {
            "read_only": True,
            "database_writes_performed": 0,
            "provider_calls_performed": 0,
            "market_data_calls_performed": 0,
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
            "raw_content_included": False,
        },
    }
    audit["audit_hash"] = _canonical_sha256(audit)
    return audit


__all__ = [
    "DISCUSSION_AUDIT_VERSION",
    "DiscussionAuditConflict",
    "project_discussion_audit",
]
