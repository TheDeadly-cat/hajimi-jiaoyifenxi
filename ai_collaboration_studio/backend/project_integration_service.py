from __future__ import annotations

import copy
import math
import re
import unicodedata
from typing import Any

from .collaboration_result import (
    ARTIFACT_DRAFT_PROFILE_VERSION,
    DECISION_PROFILE_VERSION,
    FIXED_RESULT_SAFETY,
    RESEARCH_REPORT_PROFILE_VERSION,
    CollaborationResultError,
    build_collaboration_result,
    invocation_binding_from_envelope,
    verify_collaboration_result,
)
from .decision_lineage import canonical_sha256
from .project_invocation import (
    PROJECT_INVOCATION_ENVELOPE_VERSION,
    PROJECT_INVOCATION_SEMANTICS_VERSION,
    SENSITIVE_DATA_CLASSIFICATIONS,
    SUPPORTED_DATA_CLASSIFICATIONS,
    SUPPORTED_RETENTION_POLICIES,
    SUPPORTED_WORKFLOW_RESULT_PROFILES,
    derive_project_invocation_room_id,
    normalize_project_invocation_envelope,
    project_invocation_semantics,
)


PROJECT_INTEGRATION_SERVICE_VERSION = "project_integration_service_v1"

PROJECT_SOURCE_ID = "source_project"
DOMAIN_CONTEXT_SOURCE_ID = "source_domain"
ROOM_SNAPSHOT_SOURCE_ID = "source_room_snapshot"
ARTIFACT_SOURCE_ID = "source_artifact"
MANUAL_CHATGPT_SOURCE_ID = "source_manual_chatgpt"
MANUAL_DECISION_CARD_SOURCE_ID = "source_manual_decision_card"

PROJECT_EVIDENCE_ID = "evidence_project_source"
DOMAIN_CONTEXT_EVIDENCE_ID = "evidence_domain_context"
ROOM_SNAPSHOT_EVIDENCE_ID = "evidence_room_snapshot"
ARTIFACT_EVIDENCE_ID = "evidence_artifact"
MANUAL_CHATGPT_EVIDENCE_ID = "evidence_manual_chatgpt"
MANUAL_DECISION_CARD_EVIDENCE_ID = "evidence_manual_decision_card"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")

_SEMANTICS_FIELDS = frozenset({
    "version",
    "envelope_version",
    "caller_id",
    "project_id",
    "client_request_id",
    "request_sha256",
    "room_id",
    "source",
    "workflow_kind",
    "result_profile",
    "room_spec",
    "domain_context",
    "input_manifest",
    "data_handling",
    "budget",
    "user_confirmation",
    "safety",
})
_SEMANTICS_SOURCE_FIELDS = frozenset({
    "item_id",
    "revision",
    "content_sha256",
})
_SEMANTICS_ROOM_SPEC_FIELDS = frozenset({
    "title_sha256",
    "title_characters",
    "objective_sha256",
    "objective_characters",
    "domain",
    "category",
    "template_id",
    "capability_pack_ids",
})
_DOMAIN_CONTEXT_FIELDS = frozenset({
    "schema_version",
    "schema_sha256",
    "payload_sha256",
})
_INPUT_MANIFEST_FIELDS = frozenset({"content_sha256", "content_bytes"})
_DATA_HANDLING_FIELDS = frozenset({
    "classification",
    "retention_policy",
    "retention_days",
})
_BUDGET_FIELDS = frozenset({
    "max_provider_calls",
    "max_context_bytes",
    "max_result_bytes",
})
_USER_CONFIRMATION_FIELDS = frozenset({"required", "boundary"})
_SAFETY_FIELDS = frozenset({
    "execution_capability",
    "live_trading_allowed",
    "can_autonomously_decide",
})


class ProjectIntegrationError(ValueError):
    """Fail-closed intake/projection error without any external side effect."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> None:
    raise ProjectIntegrationError(code, message)


def _object(value: Any, fields: frozenset[str], path: str) -> dict[str, Any]:
    if (
        type(value) is not dict
        or any(type(key) is not str for key in value)
        or set(value) != fields
    ):
        _fail("PROJECT_INTEGRATION_INTAKE_INVALID", f"{path} is not a closed object")
    return value


def _text(
    value: Any,
    path: str,
    *,
    maximum: int,
    minimum: int = 1,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if (
        type(value) is not str
        or not minimum <= len(value) <= maximum
        or value != value.strip()
        or unicodedata.normalize("NFC", value) != value
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        _fail("PROJECT_INTEGRATION_INTAKE_INVALID", f"{path} is invalid")
    return value


def _identifier(value: Any, path: str, *, maximum: int = 160) -> str:
    text = _text(value, path, maximum=maximum, pattern=_IDENTIFIER)
    if len(text) > maximum:
        _fail("PROJECT_INTEGRATION_INTAKE_INVALID", f"{path} is too long")
    return text


def _sha256(value: Any, path: str) -> str:
    return _text(value, path, maximum=64, pattern=_SHA256)


def _integer(value: Any, path: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("PROJECT_INTEGRATION_INTAKE_INVALID", f"{path} is invalid")
    return value


def _normalize_semantics_projection(value: Any) -> dict[str, Any]:
    """Validate the privacy-reduced projection accepted from a trusted intake.

    A full envelope is preferable because its request hash can be recomputed by
    ``project_invocation``. This path exists for a previously normalized intake
    projection and intentionally does not reconstruct redacted title/objective
    plaintext.
    """

    raw = _object(value, _SEMANTICS_FIELDS, "$.intake")
    if (
        type(raw["version"]) is not str
        or raw["version"] != PROJECT_INVOCATION_SEMANTICS_VERSION
    ):
        _fail("PROJECT_INTEGRATION_INTAKE_INVALID", "unsupported intake version")
    if (
        type(raw["envelope_version"]) is not str
        or raw["envelope_version"] != PROJECT_INVOCATION_ENVELOPE_VERSION
    ):
        _fail("PROJECT_INTEGRATION_INTAKE_INVALID", "unsupported envelope version")

    caller_id = _identifier(raw["caller_id"], "$.intake.caller_id", maximum=80)
    project_id = _identifier(raw["project_id"], "$.intake.project_id")
    client_request_id = _identifier(
        raw["client_request_id"],
        "$.intake.client_request_id",
    )
    room_id = _identifier(raw["room_id"], "$.intake.room_id", maximum=80)
    if room_id != derive_project_invocation_room_id(
        caller_id,
        project_id,
        client_request_id,
    ):
        _fail("PROJECT_INTEGRATION_ROOM_BINDING_INVALID", "room identity is not bound")
    request_sha256 = _sha256(raw["request_sha256"], "$.intake.request_sha256")

    source_raw = _object(raw["source"], _SEMANTICS_SOURCE_FIELDS, "$.intake.source")
    source = {
        "item_id": _identifier(source_raw["item_id"], "$.intake.source.item_id"),
        "revision": _text(
            source_raw["revision"],
            "$.intake.source.revision",
            maximum=160,
        ),
        "content_sha256": _sha256(
            source_raw["content_sha256"],
            "$.intake.source.content_sha256",
        ),
    }

    workflow_kind = _text(raw["workflow_kind"], "$.intake.workflow_kind", maximum=40)
    result_profile = _text(raw["result_profile"], "$.intake.result_profile", maximum=80)
    if SUPPORTED_WORKFLOW_RESULT_PROFILES.get(workflow_kind) != result_profile:
        _fail(
            "PROJECT_INTEGRATION_PROFILE_BINDING_INVALID",
            "workflow kind and result profile do not match",
        )

    room_spec_raw = _object(
        raw["room_spec"],
        _SEMANTICS_ROOM_SPEC_FIELDS,
        "$.intake.room_spec",
    )
    pack_ids = room_spec_raw["capability_pack_ids"]
    if type(pack_ids) is not list or len(pack_ids) > 32:
        _fail("PROJECT_INTEGRATION_INTAKE_INVALID", "capability pack ids are invalid")
    normalized_pack_ids = [
        _identifier(item, f"$.intake.room_spec.capability_pack_ids[{index}]", maximum=80)
        for index, item in enumerate(pack_ids)
    ]
    if normalized_pack_ids != sorted(set(normalized_pack_ids)):
        _fail(
            "PROJECT_INTEGRATION_INTAKE_INVALID",
            "capability pack ids must be sorted and unique",
        )
    room_spec = {
        "title_sha256": _sha256(
            room_spec_raw["title_sha256"],
            "$.intake.room_spec.title_sha256",
        ),
        "title_characters": _integer(
            room_spec_raw["title_characters"],
            "$.intake.room_spec.title_characters",
            minimum=1,
            maximum=80,
        ),
        "objective_sha256": _sha256(
            room_spec_raw["objective_sha256"],
            "$.intake.room_spec.objective_sha256",
        ),
        "objective_characters": _integer(
            room_spec_raw["objective_characters"],
            "$.intake.room_spec.objective_characters",
            minimum=1,
            maximum=2_000,
        ),
        "domain": _identifier(
            room_spec_raw["domain"],
            "$.intake.room_spec.domain",
            maximum=60,
        ),
        "category": _text(
            room_spec_raw["category"],
            "$.intake.room_spec.category",
            maximum=120,
        ),
        "template_id": _identifier(
            room_spec_raw["template_id"],
            "$.intake.room_spec.template_id",
            maximum=80,
        ),
        "capability_pack_ids": normalized_pack_ids,
    }

    domain_raw = _object(
        raw["domain_context"],
        _DOMAIN_CONTEXT_FIELDS,
        "$.intake.domain_context",
    )
    domain_context = {
        "schema_version": _identifier(
            domain_raw["schema_version"],
            "$.intake.domain_context.schema_version",
            maximum=80,
        ),
        "schema_sha256": _sha256(
            domain_raw["schema_sha256"],
            "$.intake.domain_context.schema_sha256",
        ),
        "payload_sha256": _sha256(
            domain_raw["payload_sha256"],
            "$.intake.domain_context.payload_sha256",
        ),
    }

    manifest_raw = _object(
        raw["input_manifest"],
        _INPUT_MANIFEST_FIELDS,
        "$.intake.input_manifest",
    )
    input_manifest = {
        "content_sha256": _sha256(
            manifest_raw["content_sha256"],
            "$.intake.input_manifest.content_sha256",
        ),
        "content_bytes": _integer(
            manifest_raw["content_bytes"],
            "$.intake.input_manifest.content_bytes",
            minimum=0,
            maximum=10_000_000,
        ),
    }
    if source["content_sha256"] != input_manifest["content_sha256"]:
        _fail(
            "PROJECT_INTEGRATION_SOURCE_HASH_MISMATCH",
            "source and input manifest hashes do not match",
        )

    handling_raw = _object(
        raw["data_handling"],
        _DATA_HANDLING_FIELDS,
        "$.intake.data_handling",
    )
    classification = _text(
        handling_raw["classification"],
        "$.intake.data_handling.classification",
        maximum=40,
    )
    retention_policy = _text(
        handling_raw["retention_policy"],
        "$.intake.data_handling.retention_policy",
        maximum=40,
    )
    if classification not in SUPPORTED_DATA_CLASSIFICATIONS:
        _fail("PROJECT_INTEGRATION_INTAKE_INVALID", "unsupported data classification")
    if retention_policy not in SUPPORTED_RETENTION_POLICIES:
        _fail("PROJECT_INTEGRATION_INTAKE_INVALID", "unsupported retention policy")
    retention_days = handling_raw["retention_days"]
    if retention_policy == "bounded_days":
        retention_days = _integer(
            retention_days,
            "$.intake.data_handling.retention_days",
            minimum=1,
            maximum=365,
        )
    elif retention_days is not None:
        _fail("PROJECT_INTEGRATION_INTAKE_INVALID", "unexpected retention days")
    if (
        classification in SENSITIVE_DATA_CLASSIFICATIONS
        and (
            retention_policy == "project_default"
            or (retention_policy == "bounded_days" and retention_days > 30)
        )
    ):
        _fail("PROJECT_INTEGRATION_INTAKE_INVALID", "unsafe sensitive retention")
    data_handling = {
        "classification": classification,
        "retention_policy": retention_policy,
        "retention_days": retention_days,
    }

    budget_raw = _object(raw["budget"], _BUDGET_FIELDS, "$.intake.budget")
    budget = {
        "max_provider_calls": _integer(
            budget_raw["max_provider_calls"],
            "$.intake.budget.max_provider_calls",
            minimum=0,
            maximum=100,
        ),
        "max_context_bytes": _integer(
            budget_raw["max_context_bytes"],
            "$.intake.budget.max_context_bytes",
            minimum=1,
            maximum=10_000_000,
        ),
        "max_result_bytes": _integer(
            budget_raw["max_result_bytes"],
            "$.intake.budget.max_result_bytes",
            minimum=1,
            maximum=10_000_000,
        ),
    }

    confirmation_raw = _object(
        raw["user_confirmation"],
        _USER_CONFIRMATION_FIELDS,
        "$.intake.user_confirmation",
    )
    if (
        type(confirmation_raw["required"]) is not bool
        or confirmation_raw["required"] is not True
        or type(confirmation_raw["boundary"]) is not str
        or confirmation_raw["boundary"] != "before_room_creation"
    ):
        _fail("PROJECT_INTEGRATION_INTAKE_INVALID", "user confirmation is missing")
    user_confirmation = {
        "required": True,
        "boundary": "before_room_creation",
    }

    safety_raw = _object(raw["safety"], _SAFETY_FIELDS, "$.intake.safety")
    if (
        type(safety_raw["execution_capability"]) is not str
        or safety_raw["execution_capability"] != "none"
        or type(safety_raw["live_trading_allowed"]) is not bool
        or safety_raw["live_trading_allowed"] is not False
        or type(safety_raw["can_autonomously_decide"]) is not bool
        or safety_raw["can_autonomously_decide"] is not False
    ):
        _fail("PROJECT_INTEGRATION_EXECUTION_FORBIDDEN", "execution authority is forbidden")
    safety = {
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
    }

    return {
        "version": PROJECT_INVOCATION_SEMANTICS_VERSION,
        "envelope_version": PROJECT_INVOCATION_ENVELOPE_VERSION,
        "caller_id": caller_id,
        "project_id": project_id,
        "client_request_id": client_request_id,
        "request_sha256": request_sha256,
        "room_id": room_id,
        "source": source,
        "workflow_kind": workflow_kind,
        "result_profile": result_profile,
        "room_spec": room_spec,
        "domain_context": domain_context,
        "input_manifest": input_manifest,
        "data_handling": data_handling,
        "budget": budget,
        "user_confirmation": user_confirmation,
        "safety": safety,
    }


def _resolve_intake(
    envelope_or_intake: Any,
    intake_projection: Any | None,
) -> dict[str, Any]:
    if type(envelope_or_intake) is not dict:
        _fail("PROJECT_INTEGRATION_INTAKE_INVALID", "intake must be an object")
    version = envelope_or_intake.get("version")
    if type(version) is not str:
        _fail("PROJECT_INTEGRATION_INTAKE_INVALID", "intake version is invalid")
    if version == PROJECT_INVOCATION_ENVELOPE_VERSION:
        normalized_envelope = normalize_project_invocation_envelope(envelope_or_intake)
        derived = _normalize_semantics_projection(
            project_invocation_semantics(normalized_envelope)
        )
        if intake_projection is not None:
            supplied = _normalize_semantics_projection(intake_projection)
            if supplied != derived:
                _fail(
                    "PROJECT_INTEGRATION_INTAKE_MISMATCH",
                    "intake projection does not match the normalized envelope",
                )
        return derived
    if version == PROJECT_INVOCATION_SEMANTICS_VERSION:
        if intake_projection is not None:
            _fail(
                "PROJECT_INTEGRATION_INTAKE_INVALID",
                "a second intake projection is not allowed",
            )
        return _normalize_semantics_projection(envelope_or_intake)
    _fail("PROJECT_INTEGRATION_INTAKE_INVALID", "unsupported intake version")


def _source(
    source_id: str,
    source_kind: str,
    record_id: str,
    record_revision: str,
    record_sha256: str,
    provenance: str,
    trust_state: str,
) -> dict[str, str]:
    return {
        "source_id": source_id,
        "source_kind": source_kind,
        "record_id": record_id,
        "record_revision": record_revision,
        "record_sha256": record_sha256,
        "provenance": provenance,
        "trust_state": trust_state,
    }


def _evidence(
    evidence_id: str,
    source_id: str,
    verification_status: str,
    review_note: str,
) -> dict[str, str]:
    return {
        "evidence_id": evidence_id,
        "source_id": source_id,
        "evidence_role": "context",
        "verification_status": verification_status,
        "review_note": review_note,
    }


def _is_exact_json(value: Any) -> bool:
    value_type = type(value)
    if value is None or value_type in {str, bool, int}:
        return True
    if value_type is float:
        return math.isfinite(value)
    if value_type is list:
        return all(_is_exact_json(item) for item in value)
    if value_type is dict:
        return all(
            type(key) is str and _is_exact_json(item)
            for key, item in value.items()
        )
    return False


def _safe_hash_matches(value: Any, expected_sha256: Any) -> bool:
    if (
        type(value) is not dict
        or type(expected_sha256) is not str
        or not _SHA256.fullmatch(expected_sha256)
        or not _is_exact_json(value)
    ):
        return False
    try:
        return canonical_sha256(value) == expected_sha256
    except (TypeError, ValueError, OverflowError):
        return False


def _room_version_record(value: Any, room_id: str) -> dict[str, Any] | None:
    if type(value) is not dict:
        return None
    candidate = value.get("room_version")
    if type(candidate) is dict:
        value = candidate
    version = value.get("version")
    snapshot = value.get("snapshot")
    stored_sha256 = value.get("stored_snapshot_sha256")
    snapshot_sha256 = (
        stored_sha256
        if type(stored_sha256) is str and bool(stored_sha256)
        else value.get("snapshot_sha256")
    )
    if (
        value.get("integrity_ok") is not True
        or value.get("snapshot_storage_integrity_ok") is not True
        or type(value.get("room_id")) is not str
        or value.get("room_id") != room_id
        or type(version) is not int
        or version <= 0
        or type(snapshot) is not dict
        or not _safe_hash_matches(snapshot, snapshot_sha256)
        or type(snapshot.get("id")) is not str
        or snapshot.get("id") != room_id
        or type(snapshot.get("settings_version")) is not int
        or snapshot.get("settings_version") != version
    ):
        return None
    return {
        "room_id": room_id,
        "version": version,
        "snapshot_sha256": snapshot_sha256,
        "snapshot": snapshot,
    }


def _artifact_version_record(value: Any, room_id: str) -> dict[str, Any] | None:
    if type(value) is not dict:
        return None
    candidate = value.get("artifact_version")
    if type(candidate) is dict:
        value = candidate
    artifact_id = value.get("artifact_id")
    version = value.get("version")
    snapshot = value.get("snapshot")
    stored_sha256 = value.get("stored_snapshot_sha256")
    snapshot_sha256 = (
        stored_sha256
        if type(stored_sha256) is str and bool(stored_sha256)
        else value.get("snapshot_sha256")
    )
    if (
        value.get("integrity_ok") is not True
        or value.get("snapshot_storage_integrity_ok") is not True
        or type(value.get("room_id")) is not str
        or value.get("room_id") != room_id
        or type(artifact_id) is not str
        or not _IDENTIFIER.fullmatch(artifact_id)
        or len(artifact_id) > 128
        or type(version) is not int
        or version <= 0
        or type(snapshot) is not dict
        or not _safe_hash_matches(snapshot, snapshot_sha256)
        or type(snapshot.get("id")) is not str
        or snapshot.get("id") != artifact_id
        or type(snapshot.get("room_id")) is not str
        or snapshot.get("room_id") != room_id
        or type(snapshot.get("version")) is not int
        or snapshot.get("version") != version
    ):
        return None
    return {
        "artifact_id": artifact_id,
        "version": version,
        "snapshot_sha256": snapshot_sha256,
        "snapshot": snapshot,
    }


def _manual_session_record(value: Any, room_id: str) -> dict[str, Any] | None:
    if type(value) is not dict:
        return None
    integrity = value.get("integrity")
    result = value.get("result")
    result_sha256 = value.get("result_sha256")
    session_id = value.get("id")
    if (
        type(integrity) is not dict
        or integrity.get("ok") is not True
        or type(value.get("room_id")) is not str
        or value.get("room_id") != room_id
        or type(session_id) is not str
        or not _IDENTIFIER.fullmatch(session_id)
        or len(session_id) > 128
        or type(result_sha256) is not str
        or not _safe_hash_matches(result, result_sha256)
    ):
        return None
    record: dict[str, Any] = {
        "session_id": session_id,
        "result_sha256": result_sha256,
        "result": result,
        "decision_card_sha256": "",
    }
    decision_card = value.get("decision_card")
    decision_card_sha256 = value.get("decision_card_sha256")
    if (
        type(decision_card_sha256) is str
        and _safe_hash_matches(decision_card, decision_card_sha256)
        and decision_card.get("session_id") == session_id
        and decision_card.get("room_id") == room_id
        and decision_card.get("result_sha256") == result_sha256
    ):
        record["decision_card_sha256"] = decision_card_sha256
    return record


def _candidate_profile(
    result_profile: str,
    *,
    room_record: dict[str, Any] | None,
    artifact_record: dict[str, Any] | None,
    manual_record: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Read only an explicitly named profile from a verified sealed payload."""

    candidates: list[Any] = []
    if artifact_record is not None:
        snapshot = artifact_record["snapshot"]
        content = snapshot.get("content")
        if type(content) is dict:
            candidates.append(content.get("collaboration_profile"))
    if manual_record is not None:
        candidates.append(manual_record["result"].get("collaboration_profile"))
    if room_record is not None:
        candidates.append(room_record["snapshot"].get("collaboration_profile"))
    for candidate in candidates:
        if type(candidate) is dict and candidate.get("version") == result_profile:
            return copy.deepcopy(candidate)
    return None


def _pending_decision_profile() -> dict[str, Any]:
    refs = [DOMAIN_CONTEXT_EVIDENCE_ID, PROJECT_EVIDENCE_ID]
    return {
        "version": DECISION_PROFILE_VERSION,
        "question": "What should the user review when a verified result becomes available?",
        "summary": {
            "text": "No verified collaboration result is available; the decision remains withheld.",
            "evidence_ids": refs,
        },
        "criteria": [{
            "criterion_id": "criterion_verified_evidence",
            "title": "Verified evidence",
            "description": "Keep the decision unresolved until provenance and evidence can be reviewed.",
            "evidence_ids": refs,
        }],
        "options": [{
            "option_id": "option_wait_for_verified_result",
            "title": "Wait for a verified result",
            "description": "Preserve the read-only invocation without selecting a substantive option.",
            "benefits": ["Preserves provenance and user control."],
            "risks": ["No substantive decision is available yet."],
            "tradeoffs": ["The decision is deferred until reviewable evidence arrives."],
            "evidence_ids": refs,
        }],
        "recommendation": {
            "state": "withheld",
            "option_id": "",
            "rationale": "A recommendation is withheld until a verified result is projected.",
            "evidence_ids": [],
        },
        "open_questions": [{
            "question_id": "question_verified_result",
            "text": "Has a sealed collaboration result been produced and independently reviewed?",
            "evidence_ids": refs,
        }],
    }


def _pending_research_profile() -> dict[str, Any]:
    refs = [DOMAIN_CONTEXT_EVIDENCE_ID, PROJECT_EVIDENCE_ID]
    return {
        "version": RESEARCH_REPORT_PROFILE_VERSION,
        "title": "Pending evidence-bound research result",
        "scope": {
            "subject": "The hash-bound project invocation and domain context.",
            "data_cutoff_utc": "",
        },
        "summary": {
            "text": "No verified research result is available; substantive findings are withheld.",
            "evidence_ids": refs,
        },
        "findings": [],
        "counterpoints": [],
        "limitations": [{
            "limitation_id": "limitation_result_pending",
            "text": "Only input identity and hashes are bound; domain claims have not been established.",
            "evidence_ids": refs,
        }],
        "open_questions": [{
            "question_id": "question_research_result",
            "text": "Which sealed evidence and independent review will support the first finding?",
            "evidence_ids": refs,
        }],
        "conclusion": {
            "state": "withheld",
            "text": "No conclusion is issued before verified evidence is available.",
            "evidence_ids": [],
        },
    }


def _artifact_kind(intake: dict[str, Any]) -> tuple[str, str]:
    room_spec = intake["room_spec"]
    markers = " ".join([
        room_spec["domain"],
        room_spec["category"],
        room_spec["template_id"],
        *room_spec["capability_pack_ids"],
    ]).lower()
    if any(token in markers for token in ("ppt", "presentation", "slide", "deck", "演示")):
        return "presentation", "pptx"
    return "document", "docx"


def _pending_artifact_profile(intake: dict[str, Any]) -> dict[str, Any]:
    artifact_kind, target_format = _artifact_kind(intake)
    refs = [DOMAIN_CONTEXT_EVIDENCE_ID, PROJECT_EVIDENCE_ID]
    noun = "presentation" if artifact_kind == "presentation" else "document"
    return {
        "version": ARTIFACT_DRAFT_PROFILE_VERSION,
        "artifact_kind": artifact_kind,
        "title": f"Pending evidence-bound {noun} draft",
        "audience": "The source project's human reviewer.",
        "purpose": "Hold a non-executing draft slot until verified content is available.",
        "sections": [{
            "section_id": "section_pending_result",
            "ordinal": 1,
            "title": "Result pending",
            "purpose": "Make the withheld state and evidence boundary explicit.",
            "body": "No verified draft content is available. Rendering and export remain pending.",
            "bullets": [],
            "speaker_notes": "Obtain a sealed profile and complete render verification before export.",
            "evidence_ids": refs,
        }],
        "asset_briefs": [],
        "export_plan": {
            "target_format": target_format,
            "suggested_filename": f"collaboration-draft.{target_format}",
            "renderer_id": "host_renderer_pending",
            "renderer_version": "1.0.0",
            "user_selected_destination_required": True,
            "overwrite_allowed": False,
            "render_required": True,
            "verification_required": True,
        },
        "delivery": {
            "render_state": "not_rendered",
            "render_package_sha256": "",
            "verification_state": "not_run",
            "verification_receipt_sha256": "",
            "export_state": "not_exported",
            "export_receipt_sha256": "",
            "failure_codes": [],
        },
    }


def _pending_profile(intake: dict[str, Any]) -> dict[str, Any]:
    profile_version = intake["result_profile"]
    if profile_version == DECISION_PROFILE_VERSION:
        return _pending_decision_profile()
    if profile_version == RESEARCH_REPORT_PROFILE_VERSION:
        return _pending_research_profile()
    if profile_version == ARTIFACT_DRAFT_PROFILE_VERSION:
        return _pending_artifact_profile(intake)
    _fail("PROJECT_INTEGRATION_PROFILE_BINDING_INVALID", "unsupported result profile")


def project_collaboration_result(
    envelope_or_intake: Any,
    *,
    intake_projection: Any | None = None,
    studio_snapshot: Any | None = None,
    manual_session: Any | None = None,
    artifact: Any | None = None,
) -> dict[str, Any]:
    """Project one invocation into a deterministic, read-only collaboration result.

    The first argument may be a complete normalized/sealed project invocation
    envelope or its privacy-reduced semantics projection. A complete envelope is
    normalized first, so ``input_manifest.content_sha256`` is authoritatively
    carried to ``source.content_sha256`` before result verification.

    Optional Studio values are never copied wholesale. Only independently
    hash-verifiable room/artifact/manual identities are bound. An explicitly
    named ``collaboration_profile`` may be consumed from such a sealed payload;
    malformed or incompatible optional content deterministically falls back to
    the valid pending/withheld profile.
    """

    intake = _resolve_intake(envelope_or_intake, intake_projection)

    room_record = _room_version_record(studio_snapshot, intake["room_id"])
    artifact_record = _artifact_version_record(artifact, intake["room_id"])
    manual_record = _manual_session_record(manual_session, intake["room_id"])

    sources = [
        _source(
            PROJECT_SOURCE_ID,
            "project_source",
            intake["source"]["item_id"],
            intake["source"]["revision"],
            intake["source"]["content_sha256"],
            "caller_supplied",
            "hash_bound_only",
        ),
        _source(
            DOMAIN_CONTEXT_SOURCE_ID,
            "domain_context",
            "domain_context",
            intake["domain_context"]["schema_version"],
            intake["domain_context"]["payload_sha256"],
            "caller_supplied",
            "hash_bound_only",
        ),
    ]
    evidence = [
        _evidence(
            PROJECT_EVIDENCE_ID,
            PROJECT_SOURCE_ID,
            "unreviewed",
            "The caller source identity and content hash are bound; domain truth is not asserted.",
        ),
        _evidence(
            DOMAIN_CONTEXT_EVIDENCE_ID,
            DOMAIN_CONTEXT_SOURCE_ID,
            "unreviewed",
            "The domain context payload is hash-bound; its claims remain unreviewed.",
        ),
    ]

    if room_record is not None:
        sources.append(_source(
            ROOM_SNAPSHOT_SOURCE_ID,
            "studio_room_snapshot",
            room_record["room_id"],
            str(room_record["version"]),
            room_record["snapshot_sha256"],
            "studio_sealed",
            "host_verified_binding",
        ))
        evidence.append(_evidence(
            ROOM_SNAPSHOT_EVIDENCE_ID,
            ROOM_SNAPSHOT_SOURCE_ID,
            "source_checked",
            "The exact Studio room version snapshot seal was checked.",
        ))

    studio_binding = {
        "round_id": "",
        "artifact_id": "",
        "artifact_version": 0,
        "artifact_snapshot_sha256": "",
        "manual_chatgpt_session_id": "",
        "manual_chatgpt_result_sha256": "",
        "decision_card_sha256": "",
    }
    if artifact_record is not None:
        studio_binding.update({
            "artifact_id": artifact_record["artifact_id"],
            "artifact_version": artifact_record["version"],
            "artifact_snapshot_sha256": artifact_record["snapshot_sha256"],
        })
        sources.append(_source(
            ARTIFACT_SOURCE_ID,
            "studio_artifact_version",
            artifact_record["artifact_id"],
            str(artifact_record["version"]),
            artifact_record["snapshot_sha256"],
            "studio_sealed",
            "host_verified_binding",
        ))
        evidence.append(_evidence(
            ARTIFACT_EVIDENCE_ID,
            ARTIFACT_SOURCE_ID,
            "source_checked",
            "The exact Studio artifact version snapshot seal was checked.",
        ))
    if manual_record is not None:
        studio_binding.update({
            "manual_chatgpt_session_id": manual_record["session_id"],
            "manual_chatgpt_result_sha256": manual_record["result_sha256"],
            "decision_card_sha256": manual_record["decision_card_sha256"],
        })
        result_version = manual_record["result"].get("version")
        if (
            type(result_version) is not str
            or not _IDENTIFIER.fullmatch(result_version)
        ):
            result_version = "manual_chatgpt_result_v1"
        sources.append(_source(
            MANUAL_CHATGPT_SOURCE_ID,
            "manual_chatgpt_import",
            manual_record["session_id"],
            result_version,
            manual_record["result_sha256"],
            "manual_ai_import",
            "manual_import_untrusted",
        ))
        evidence.append(_evidence(
            MANUAL_CHATGPT_EVIDENCE_ID,
            MANUAL_CHATGPT_SOURCE_ID,
            "unreviewed",
            "The manual ChatGPT import hash was checked; model content remains advisory and untrusted.",
        ))
        if manual_record["decision_card_sha256"]:
            sources.append(_source(
                MANUAL_DECISION_CARD_SOURCE_ID,
                "manual_chatgpt_decision_card",
                manual_record["session_id"],
                "manual_chatgpt_decision_card_v1",
                manual_record["decision_card_sha256"],
                "host_projection",
                "host_verified_binding",
            ))
            evidence.append(_evidence(
                MANUAL_DECISION_CARD_EVIDENCE_ID,
                MANUAL_DECISION_CARD_SOURCE_ID,
                "source_checked",
                "The host-projected decision card hash and session binding were checked.",
            ))

    profile = None
    if intake["data_handling"]["retention_policy"] != "no_payload_retention":
        profile = _candidate_profile(
            intake["result_profile"],
            room_record=room_record,
            artifact_record=artifact_record,
            manual_record=manual_record,
        )
    pending_profile = _pending_profile(intake)
    payload = {
        "invocation_binding": invocation_binding_from_envelope(intake),
        "studio_binding": studio_binding,
        "workflow_kind": intake["workflow_kind"],
        "result_profile": intake["result_profile"],
        "domain_context": copy.deepcopy(intake["domain_context"]),
        "source_manifest": sources,
        "evidence_manifest": evidence,
        "profile": profile or pending_profile,
        "independent_review": {
            "status": "not_run",
            "source_ids": [],
            "findings": [],
            "open_questions": [],
            "review_bundle_sha256": "",
        },
        "user_boundary": {
            "status": "pending",
            "outcome": "unresolved",
            "record_id": "",
            "record_version": "",
            "record_sha256": "",
            "selected_item_id": "",
        },
    }
    try:
        result = build_collaboration_result(payload)
    except CollaborationResultError:
        if profile is not None:
            _fail(
                "PROJECT_INTEGRATION_PROFILE_REJECTED",
                "A sealed collaboration profile failed deterministic validation.",
            )
        _fail(
            "PROJECT_INTEGRATION_RESULT_INVALID",
            "The deterministic pending result failed validation.",
        )
    verified = verify_collaboration_result(result, expected_envelope=intake)
    if verified["safety"] != FIXED_RESULT_SAFETY:
        _fail("PROJECT_INTEGRATION_SAFETY_MISMATCH", "fixed safety boundary changed")
    return verified


class ProjectIntegrationService:
    """Stateless facade for callers that prefer an explicit service object."""

    @staticmethod
    def project_result(
        envelope_or_intake: Any,
        *,
        intake_projection: Any | None = None,
        studio_snapshot: Any | None = None,
        manual_session: Any | None = None,
        artifact: Any | None = None,
    ) -> dict[str, Any]:
        return project_collaboration_result(
            envelope_or_intake,
            intake_projection=intake_projection,
            studio_snapshot=studio_snapshot,
            manual_session=manual_session,
            artifact=artifact,
        )


project_result = project_collaboration_result


__all__ = [
    "ARTIFACT_EVIDENCE_ID",
    "DOMAIN_CONTEXT_EVIDENCE_ID",
    "MANUAL_CHATGPT_EVIDENCE_ID",
    "PROJECT_EVIDENCE_ID",
    "PROJECT_INTEGRATION_SERVICE_VERSION",
    "ProjectIntegrationError",
    "ProjectIntegrationService",
    "project_collaboration_result",
    "project_result",
]
