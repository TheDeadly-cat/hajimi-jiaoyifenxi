from __future__ import annotations

import json


def append_valid_turn_contract(
    content: str,
    *,
    instructions: str,
    input_text: str,
    output_format: str = "auto",
) -> str:
    """Encode a deterministic, role-complete fake speaker response.

    Production now requires the frozen turn protocol for every new formal
    round.  Test providers remain offline and deterministic, so their speaker
    replies need to model both the historical XML block and the pure JSON
    envelope without weakening production checks.
    """

    clean_format = str(output_format or "auto").strip().lower()
    if clean_format == "auto":
        if "turn_envelope_v1" in instructions:
            clean_format = "json_envelope"
        elif "<turn_contract>{JSON" in instructions:
            clean_format = "legacy_xml"
        else:
            return content
    if clean_format not in {"json_envelope", "legacy_xml"}:
        raise ValueError("unsupported test turn output format")
    if clean_format == "legacy_xml" and "<turn_contract" in content:
        return content

    payload = build_valid_turn_contract_payload(
        instructions=instructions,
        input_text=input_text,
    )
    if clean_format == "json_envelope":
        return wrap_turn_envelope(content, payload)
    return append_xml_turn_contract(content, payload)


def wrap_turn_envelope(content: str, payload: dict) -> str:
    return json.dumps(
        {
            "version": "turn_envelope_v1",
            "turn_contract": payload,
            "visible_content": str(content or "").rstrip(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def append_xml_turn_contract(content: str, payload: dict) -> str:
    return (
        f"{str(content or '').rstrip()}\n"
        f"<turn_contract>{json.dumps(payload, ensure_ascii=False)}</turn_contract>"
    )


def build_valid_turn_contract_payload(
    *,
    instructions: str,
    input_text: str,
) -> dict:
    """Build the shared semantic contract used by both transport fixtures."""

    allowed_message_ids = _ids_from_line(
        input_text,
        "本条发言合同允许引用的消息ID：",
    )
    prior_ai_message_ids = _ids_from_line(
        input_text,
        "本轮此前正式 AI 消息ID：",
    )
    evidence_id = allowed_message_ids[0] if allowed_message_ids else ""
    evidence = (
        [{"type": "message", "id": evidence_id, "role": "support"}]
        if evidence_id
        else []
    )
    responds_to = (
        [{
            "type": "message",
            "id": prior_ai_message_ids[-1],
            "relation": "challenges",
            "reason": "Test fixture audits the preceding AI contribution.",
        }]
        if prior_ai_message_ids
        else []
    )
    capability_line = next(
        (line for line in instructions.splitlines() if line.startswith("服务端能力：")),
        "",
    )
    is_decision = bool(
        "流程阶段：decision。" in instructions
        or "立场：portfolio_manager。" in instructions
        or "decision_synthesis" in capability_line
    )
    is_risk = bool(
        "流程阶段：risk。" in instructions
        or "立场：risk。" in instructions
        or "risk_review" in capability_line
    )
    risk_snapshot = _json_snapshot_after(
        input_text,
        "服务端规范候选只读快照（candidate_risk_review_v1，仅供风险复核角色）：",
    )
    decision_snapshot = _json_snapshot_after(
        input_text,
        "服务端规范候选只读快照（candidate_lineage_v1，仅供决策角色）：",
    )
    snapshot_candidates = (
        decision_snapshot.get("candidates")
        if is_decision and isinstance(decision_snapshot, dict)
        else risk_snapshot.get("candidates")
        if is_risk and isinstance(risk_snapshot, dict)
        else None
    )
    candidate_updates = _fixture_candidate_updates(
        snapshot_candidates,
        is_decision=is_decision,
        is_risk=is_risk,
        fallback_evidence=evidence,
    )
    if is_risk and isinstance(snapshot_candidates, list):
        reviewed_message_ids = [
            str(candidate.get("latest_message_id") or "")
            for candidate in snapshot_candidates
            if isinstance(candidate, dict)
            and str(candidate.get("latest_message_id") or "")
        ]
        responds_to = [
            {
                "type": "message",
                "id": message_id,
                "relation": "challenges",
                "reason": "Test fixture reviews the server-frozen candidate revision.",
            }
            for message_id in dict.fromkeys(reviewed_message_ids)
        ] or responds_to
    payload = {
        "version": "turn_contract_v1",
        "claims": [{
            "id": "fixture_fact",
            "kind": "fact",
            "text": "The test response is grounded in the frozen round context.",
            "as_of": "2026-08-02T00:00:00Z",
            "evidence": evidence,
        }],
        "responds_to": responds_to,
        "candidate_updates": candidate_updates,
        "risks": [{
            "id": "fixture_risk",
            "text": "The frozen evidence may be incomplete.",
            "severity": "medium",
            "status": "open",
            "trigger": "A required source is missing or stale.",
            "mitigation": "Request explicit user review before any decision.",
            "blocking": False,
            "evidence": evidence,
        }],
        "next_actions": [{
            "id": "fixture_review",
            "text": "Review the frozen evidence and response edge.",
            "owner": "user",
            "state": "open",
            "due": "this round",
            "evidence": evidence,
        }],
        "confidence": {
            "kind": "model_subjective",
            "value": None,
            "label": "unknown",
            "basis": "Deterministic offline test fixture.",
        },
    }
    return payload


def _ids_from_line(input_text: str, prefix: str) -> list[str]:
    line = next(
        (candidate for candidate in input_text.splitlines() if candidate.startswith(prefix)),
        "",
    )
    raw = line[len(prefix):].strip() if line else ""
    if not raw or raw.startswith("无"):
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _json_snapshot_after(input_text: str, marker: str) -> dict | None:
    if marker not in input_text:
        return None
    tail = input_text.split(marker, 1)[1].lstrip()
    payload = tail.splitlines()[0] if tail else ""
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _fixture_candidate_updates(
    snapshot_candidates: object,
    *,
    is_decision: bool,
    is_risk: bool,
    fallback_evidence: list[dict],
) -> list[dict]:
    if isinstance(snapshot_candidates, list):
        updates: list[dict] = []
        for index, raw_candidate in enumerate(snapshot_candidates):
            if not isinstance(raw_candidate, dict):
                continue
            candidate = {
                "id": str(raw_candidate.get("id") or ""),
                "title": str(raw_candidate.get("title") or ""),
                "action": (
                    "select" if is_decision and index == 0
                    else "reject" if is_decision
                    else "challenge" if is_risk and index == 0
                    else "support"
                ),
                "symbol": str(raw_candidate.get("symbol") or ""),
                "direction": str(raw_candidate.get("direction") or "UNSPECIFIED"),
                "horizon_days": raw_candidate.get("horizon_days"),
                "thesis": str(raw_candidate.get("thesis") or ""),
                "invalidation": str(raw_candidate.get("invalidation") or ""),
                "evidence": list(fallback_evidence),
            }
            if is_decision and index == 0:
                current_reviews = raw_candidate.get("current_risk_reviews")
                review = next(
                    (
                        item for item in current_reviews or []
                        if isinstance(item, dict)
                        and str(item.get("review_message_id") or "")
                    ),
                    None,
                )
                if review:
                    candidate["evidence"] = [{
                        "type": "message",
                        "id": str(review["review_message_id"]),
                        "role": "context",
                    }]
            updates.append(candidate)
        return updates

    return [
        {
            "id": "fixture_option_a",
            "title": "Fixture option A",
            "action": "select" if is_decision else "propose",
            "symbol": "",
            "direction": "UNSPECIFIED",
            "horizon_days": None,
            "thesis": "Continue the read-only research workflow.",
            "invalidation": "New evidence contradicts the frozen context.",
            "evidence": list(fallback_evidence),
        },
        {
            "id": "fixture_option_b",
            "title": "Fixture option B",
            "action": "reject" if is_decision else "propose",
            "symbol": "",
            "direction": "UNSPECIFIED",
            "horizon_days": None,
            "thesis": "Keep the current evidence-gated candidate.",
            "invalidation": "The user rejects the candidate after review.",
            "evidence": list(fallback_evidence),
        },
    ]
