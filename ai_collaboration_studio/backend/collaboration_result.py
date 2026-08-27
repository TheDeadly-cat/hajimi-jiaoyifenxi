from __future__ import annotations

import copy
import re
from datetime import datetime
from typing import Any, Mapping

from .decision_lineage import canonical_sha256


COLLABORATION_RESULT_VERSION = "collaboration_result_v1"
DECISION_PROFILE_VERSION = "decision_v1"
RESEARCH_REPORT_PROFILE_VERSION = "research_report_v1"
ARTIFACT_DRAFT_PROFILE_VERSION = "artifact_draft_v1"

RESULT_PROFILE_WORKFLOW_KINDS = {
    DECISION_PROFILE_VERSION: "decision",
    RESEARCH_REPORT_PROFILE_VERSION: "research",
    ARTIFACT_DRAFT_PROFILE_VERSION: "artifact_authoring",
}

FIXED_RESULT_SAFETY: dict[str, Any] = {
    "advisory_only": True,
    "execution_capability": "none",
    "live_trading_allowed": False,
    "betting_allowed": False,
    "external_write_authorized": False,
    "can_autonomously_decide": False,
    "can_replace_user_decision": False,
    "user_final_decision_required": True,
}

SOURCE_KINDS = frozenset({
    "project_source",
    "domain_context",
    "studio_room_snapshot",
    "studio_round_checkpoint",
    "studio_artifact_version",
    "manual_chatgpt_import",
    "manual_chatgpt_decision_card",
    "api_review_bundle",
    "deterministic_engine_receipt",
    "document_ingest_receipt",
    "artifact_render_package",
    "render_verification_receipt",
    "artifact_export_receipt",
    "user_decision_record",
})

_SOURCE_POLICIES: dict[str, frozenset[tuple[str, str]]] = {
    "project_source": frozenset({("caller_supplied", "hash_bound_only")}),
    "domain_context": frozenset({
        ("caller_supplied", "hash_bound_only"),
        ("deterministic_engine", "deterministic_contract_verified"),
    }),
    "studio_room_snapshot": frozenset({
        ("studio_sealed", "host_verified_binding"),
    }),
    "studio_round_checkpoint": frozenset({
        ("studio_sealed", "host_verified_binding"),
    }),
    "studio_artifact_version": frozenset({
        ("studio_sealed", "host_verified_binding"),
    }),
    "manual_chatgpt_import": frozenset({
        ("manual_ai_import", "manual_import_untrusted"),
    }),
    "manual_chatgpt_decision_card": frozenset({
        ("host_projection", "host_verified_binding"),
    }),
    "api_review_bundle": frozenset({
        ("provider_review", "provider_output_advisory"),
    }),
    "deterministic_engine_receipt": frozenset({
        ("deterministic_engine", "deterministic_contract_verified"),
    }),
    "document_ingest_receipt": frozenset({
        ("document_pipeline", "host_verified_binding"),
    }),
    "artifact_render_package": frozenset({
        ("document_pipeline", "host_verified_binding"),
    }),
    "render_verification_receipt": frozenset({
        ("document_pipeline", "host_verified_binding"),
    }),
    "artifact_export_receipt": frozenset({
        ("document_pipeline", "host_verified_binding"),
    }),
    "user_decision_record": frozenset({
        ("studio_sealed", "host_verified_binding"),
    }),
}

EVIDENCE_ROLES = frozenset({"support", "counter", "context"})
EVIDENCE_VERIFICATION_STATUSES = frozenset({
    "unreviewed",
    "source_checked",
    "corroborated",
    "disputed",
})
REVIEW_STATUSES = frozenset({"not_run", "passed", "concern", "blocked"})
REVIEW_SEVERITIES = frozenset({"low", "medium", "high", "blocking"})
USER_BOUNDARY_STATUSES = frozenset({"pending", "recorded"})
USER_BOUNDARY_OUTCOMES = frozenset({
    "unresolved",
    "accepted",
    "deferred",
    "rejected",
})

ARTIFACT_RENDER_STATES = frozenset({"not_rendered", "rendered", "failed"})
ARTIFACT_VERIFICATION_STATES = frozenset({
    "not_run",
    "failed",
    "needs_user_review",
    "verified",
})
ARTIFACT_EXPORT_STATES = frozenset({"not_exported", "exported"})

ARTIFACT_DELIVERY_FAILURE_CODES = frozenset({
    "PPTX_SOURCE_TOO_LARGE",
    "PPTX_INVALID_ZIP",
    "PPTX_STRUCTURE_INVALID",
    "PPTX_ZIP_BOMB_LIMIT",
    "PPTX_TOO_MANY_SLIDES",
    "PPTX_MACRO_ENABLED_REJECTED",
    "PPTX_EMBEDDED_OBJECT_REJECTED",
    "PPTX_EXTERNAL_RELATIONSHIP_RECORDED",
    "PPTX_TEXT_TRUNCATED",
    "RENDER_INPUT_CONTRACT_INVALID",
    "RENDER_PROFILE_UNSUPPORTED",
    "RENDER_PROFILE_HASH_MISMATCH",
    "RENDERER_UNAVAILABLE",
    "RENDERER_VERSION_MISMATCH",
    "RENDER_FAILED",
    "RENDER_OUTPUT_MISSING",
    "RENDER_OUTPUT_HASH_MISMATCH",
    "RENDER_SLIDE_COUNT_MISMATCH",
    "RENDER_SECTION_COVERAGE_INCOMPLETE",
    "RENDER_TEXT_OVERFLOW",
    "RENDER_MISSING_FONT",
    "RENDER_BROKEN_ASSET",
    "RENDER_PREVIEW_FAILED",
    "VERIFY_CHECK_UNAVAILABLE",
    "VERIFY_USER_REVIEW_PENDING",
    "VERIFY_USER_REJECTED",
    "EXPORT_USER_DESTINATION_REQUIRED",
    "EXPORT_TARGET_EXISTS",
    "EXPORT_EXPECTED_TARGET_HASH_MISMATCH",
    "EXPORT_WRITE_FAILED",
    "EXPORT_POSTWRITE_HASH_MISMATCH",
})

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_UTC_PATTERN = re.compile(
    r"(?:19|20|21)[0-9]{2}-(?:0[1-9]|1[0-2])-"
    r"(?:0[1-9]|[12][0-9]|3[01])T"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z"
)

_BUILD_FIELDS = frozenset({
    "invocation_binding",
    "studio_binding",
    "workflow_kind",
    "result_profile",
    "domain_context",
    "source_manifest",
    "evidence_manifest",
    "profile",
    "independent_review",
    "user_boundary",
})
_ROOT_FIELDS = frozenset({
    "version",
    "schema_sha256",
    "result_id",
    *_BUILD_FIELDS,
    "profile_schema_sha256",
    "profile_sha256",
    "safety",
    "result_sha256",
})

_INVOCATION_BINDING_FIELDS = frozenset({
    "client_request_id",
    "request_sha256",
    "room_id",
    "caller_id",
    "project_id",
    "source_item_id",
    "source_revision",
})
_STUDIO_BINDING_FIELDS = frozenset({
    "round_id",
    "artifact_id",
    "artifact_version",
    "artifact_snapshot_sha256",
    "manual_chatgpt_session_id",
    "manual_chatgpt_result_sha256",
    "decision_card_sha256",
})
_DOMAIN_CONTEXT_FIELDS = frozenset({
    "schema_version",
    "schema_sha256",
    "payload_sha256",
})
_SOURCE_FIELDS = frozenset({
    "source_id",
    "source_kind",
    "record_id",
    "record_revision",
    "record_sha256",
    "provenance",
    "trust_state",
})
_EVIDENCE_FIELDS = frozenset({
    "evidence_id",
    "source_id",
    "evidence_role",
    "verification_status",
    "review_note",
})
_INDEPENDENT_REVIEW_FIELDS = frozenset({
    "status",
    "source_ids",
    "findings",
    "open_questions",
    "review_bundle_sha256",
})
_REVIEW_FINDING_FIELDS = frozenset({
    "finding_id",
    "severity",
    "statement",
    "rationale",
    "evidence_ids",
})
_USER_BOUNDARY_FIELDS = frozenset({
    "status",
    "outcome",
    "record_id",
    "record_version",
    "record_sha256",
    "selected_item_id",
})


def _closed(fields: frozenset[str] | set[str], **metadata: Any) -> dict[str, Any]:
    return {
        "required": sorted(fields),
        "additional_properties": False,
        **metadata,
    }


DECISION_PROFILE_SCHEMA: dict[str, Any] = {
    "version": DECISION_PROFILE_VERSION,
    "root": _closed({
        "version",
        "question",
        "summary",
        "criteria",
        "options",
        "recommendation",
        "open_questions",
    }),
    "definitions": {
        "text_binding_v1": _closed({"text", "evidence_ids"}),
        "criterion_v1": _closed({
            "criterion_id", "title", "description", "evidence_ids",
        }),
        "decision_option_v1": _closed({
            "option_id", "title", "description", "benefits", "risks",
            "tradeoffs", "evidence_ids",
        }),
        "recommendation_v1": _closed({
            "state", "option_id", "rationale", "evidence_ids",
        }, states=["candidate", "deferred", "withheld"]),
        "open_question_v1": _closed({"question_id", "text", "evidence_ids"}),
    },
}

RESEARCH_REPORT_PROFILE_SCHEMA: dict[str, Any] = {
    "version": RESEARCH_REPORT_PROFILE_VERSION,
    "root": _closed({
        "version",
        "title",
        "scope",
        "summary",
        "findings",
        "counterpoints",
        "limitations",
        "open_questions",
        "conclusion",
    }),
    "definitions": {
        "scope_v1": _closed({"subject", "data_cutoff_utc"}),
        "text_binding_v1": _closed({"text", "evidence_ids"}),
        "research_claim_v1": _closed({
            "claim_id", "statement", "claim_kind", "support_state",
            "evidence_ids", "uncertainty",
        }, claim_kinds=[
            "deterministic_fact", "sourced_fact", "model_inference",
            "interpretation",
        ], support_states=["supported", "mixed", "insufficient_evidence"]),
        "limitation_v1": _closed({"limitation_id", "text", "evidence_ids"}),
        "open_question_v1": _closed({"question_id", "text", "evidence_ids"}),
        "conclusion_v1": _closed({"state", "text", "evidence_ids"}, states=[
            "supported", "mixed", "insufficient_evidence", "withheld",
        ]),
    },
}

ARTIFACT_DRAFT_PROFILE_SCHEMA: dict[str, Any] = {
    "version": ARTIFACT_DRAFT_PROFILE_VERSION,
    "root": _closed({
        "version",
        "artifact_kind",
        "title",
        "audience",
        "purpose",
        "sections",
        "asset_briefs",
        "export_plan",
        "delivery",
    }, artifact_kinds=["presentation", "document"]),
    "definitions": {
        "section_v1": _closed({
            "section_id", "ordinal", "title", "purpose", "body", "bullets",
            "speaker_notes", "evidence_ids",
        }),
        "asset_brief_v1": _closed({
            "asset_id", "asset_kind", "description", "section_id",
            "evidence_ids",
        }, asset_kinds=["image", "chart", "table", "diagram"]),
        "export_plan_v1": _closed({
            "target_format", "suggested_filename", "renderer_id",
            "renderer_version", "user_selected_destination_required",
            "overwrite_allowed", "render_required", "verification_required",
        }, target_formats=["pptx", "docx", "pdf"], fixed={
            "user_selected_destination_required": True,
            "overwrite_allowed": False,
            "render_required": True,
            "verification_required": True,
        }),
        "delivery_v1": _closed({
            "render_state", "render_package_sha256", "verification_state",
            "verification_receipt_sha256", "export_state",
            "export_receipt_sha256", "failure_codes",
        }, render_states=sorted(ARTIFACT_RENDER_STATES),
            verification_states=sorted(ARTIFACT_VERIFICATION_STATES),
            export_states=sorted(ARTIFACT_EXPORT_STATES),
            failure_codes=sorted(ARTIFACT_DELIVERY_FAILURE_CODES)),
    },
}

COLLABORATION_PROFILE_SCHEMAS: dict[str, dict[str, Any]] = {
    DECISION_PROFILE_VERSION: DECISION_PROFILE_SCHEMA,
    RESEARCH_REPORT_PROFILE_VERSION: RESEARCH_REPORT_PROFILE_SCHEMA,
    ARTIFACT_DRAFT_PROFILE_VERSION: ARTIFACT_DRAFT_PROFILE_SCHEMA,
}
COLLABORATION_PROFILE_SCHEMA_SHA256: dict[str, str] = {
    version: canonical_sha256(schema)
    for version, schema in COLLABORATION_PROFILE_SCHEMAS.items()
}

COLLABORATION_RESULT_SCHEMA: dict[str, Any] = {
    "version": "collaboration_result_schema_v1",
    "root": _closed(_ROOT_FIELDS),
    "definitions": {
        "invocation_binding_v1": _closed(
            _INVOCATION_BINDING_FIELDS,
            identifier_max_characters=160,
        ),
        "studio_binding_v1": _closed(_STUDIO_BINDING_FIELDS),
        "domain_context_binding_v1": _closed(_DOMAIN_CONTEXT_FIELDS),
        "source_manifest_entry_v1": _closed(
            _SOURCE_FIELDS,
            identifier_max_characters=128,
            project_source_record_id_max_characters=160,
            source_kinds=sorted(SOURCE_KINDS),
            source_policies={
                source_kind: [
                    {"provenance": provenance, "trust_state": trust_state}
                    for provenance, trust_state in sorted(policies)
                ]
                for source_kind, policies in sorted(_SOURCE_POLICIES.items())
            },
        ),
        "evidence_manifest_entry_v1": _closed(
            _EVIDENCE_FIELDS,
            evidence_roles=sorted(EVIDENCE_ROLES),
            verification_statuses=sorted(EVIDENCE_VERIFICATION_STATUSES),
        ),
        "independent_review_v1": _closed(
            _INDEPENDENT_REVIEW_FIELDS,
            statuses=sorted(REVIEW_STATUSES),
        ),
        "review_finding_v1": _closed(
            _REVIEW_FINDING_FIELDS,
            severities=sorted(REVIEW_SEVERITIES),
        ),
        "user_boundary_v1": _closed(
            _USER_BOUNDARY_FIELDS,
            statuses=sorted(USER_BOUNDARY_STATUSES),
            outcomes=sorted(USER_BOUNDARY_OUTCOMES),
        ),
        "safety_v1": _closed(
            frozenset(FIXED_RESULT_SAFETY),
            fixed=copy.deepcopy(FIXED_RESULT_SAFETY),
        ),
    },
    "profiles": {
        version: {
            "workflow_kind": RESULT_PROFILE_WORKFLOW_KINDS[version],
            "schema_sha256": COLLABORATION_PROFILE_SCHEMA_SHA256[version],
        }
        for version in sorted(COLLABORATION_PROFILE_SCHEMAS)
    },
}
COLLABORATION_RESULT_SCHEMA_SHA256 = canonical_sha256(
    COLLABORATION_RESULT_SCHEMA
)


class CollaborationResultError(ValueError):
    """Raised when a portable collaboration result fails closed validation."""

    def __init__(self, message: str, *, path: str = "$") -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.code = "COLLABORATION_RESULT_INVALID"


def _fail(path: str, message: str) -> None:
    raise CollaborationResultError(message, path=path)


def _object(value: Any, path: str, fields: frozenset[str] | set[str]) -> dict[str, Any]:
    if type(value) is not dict:
        _fail(path, "must be an exact JSON object")
    if set(value) != set(fields):
        _fail(path, "does not match the closed field set")
    return value


def _array(value: Any, path: str, *, maximum: int, minimum: int = 0) -> list[Any]:
    if type(value) is not list or not minimum <= len(value) <= maximum:
        _fail(path, f"must contain between {minimum} and {maximum} items")
    return value


def _text(
    value: Any,
    path: str,
    *,
    maximum: int,
    minimum: int = 1,
    strip: bool = True,
) -> str:
    if type(value) is not str:
        _fail(path, "must be a string")
    clean = value.strip() if strip else value
    if not minimum <= len(clean) <= maximum:
        _fail(path, f"must contain between {minimum} and {maximum} characters")
    return clean


def _optional_text(value: Any, path: str, *, maximum: int) -> str:
    return _text(value, path, maximum=maximum, minimum=0)


def _identifier(value: Any, path: str, *, maximum: int = 128) -> str:
    clean = _text(value, path, maximum=maximum)
    if not _IDENTIFIER_PATTERN.fullmatch(clean):
        _fail(path, "must be a stable identifier")
    return clean


def _optional_identifier(value: Any, path: str) -> str:
    clean = _optional_text(value, path, maximum=128)
    if clean and not _IDENTIFIER_PATTERN.fullmatch(clean):
        _fail(path, "must be empty or a stable identifier")
    return clean


def _sha256(value: Any, path: str, *, allow_empty: bool = False) -> str:
    clean = _optional_text(value, path, maximum=64) if allow_empty else _text(
        value,
        path,
        maximum=64,
        minimum=64,
    )
    if clean or not allow_empty:
        if not _SHA256_PATTERN.fullmatch(clean):
            _fail(path, "must be a lowercase SHA-256")
    return clean


def _positive_or_zero_integer(value: Any, path: str) -> int:
    if type(value) is not int or value < 0:
        _fail(path, "must be a non-negative integer")
    return value


def _fixed_boolean(value: Any, expected: bool, path: str) -> bool:
    if type(value) is not bool or value is not expected:
        _fail(path, f"must be the fixed boolean {str(expected).lower()}")
    return expected


def _enum(value: Any, allowed: frozenset[str] | set[str], path: str) -> str:
    clean = _text(value, path, maximum=80)
    if clean not in allowed:
        _fail(path, "contains an unsupported enum value")
    return clean


def _string_list(
    value: Any,
    path: str,
    *,
    maximum_items: int,
    maximum_text: int,
    minimum_items: int = 0,
    sort_values: bool = False,
) -> list[str]:
    rows = _array(
        value,
        path,
        maximum=maximum_items,
        minimum=minimum_items,
    )
    clean = [
        _text(item, f"{path}[{index}]", maximum=maximum_text)
        for index, item in enumerate(rows)
    ]
    if len(clean) != len(set(clean)):
        _fail(path, "must not contain duplicates")
    return sorted(clean) if sort_values else clean


def _evidence_ids(
    value: Any,
    path: str,
    evidence_by_id: Mapping[str, dict[str, Any]],
    *,
    minimum: int = 0,
) -> list[str]:
    refs = _string_list(
        value,
        path,
        maximum_items=64,
        maximum_text=128,
        minimum_items=minimum,
        sort_values=True,
    )
    unknown = [reference for reference in refs if reference not in evidence_by_id]
    if unknown:
        _fail(path, "references evidence outside the sealed manifest")
    return refs


def _normalize_invocation_binding(value: Any) -> dict[str, Any]:
    raw = _object(value, "$.invocation_binding", _INVOCATION_BINDING_FIELDS)
    return {
        "client_request_id": _identifier(
            raw.get("client_request_id"),
            "$.invocation_binding.client_request_id",
            maximum=160,
        ),
        "request_sha256": _sha256(
            raw.get("request_sha256"),
            "$.invocation_binding.request_sha256",
        ),
        "room_id": _identifier(
            raw.get("room_id"),
            "$.invocation_binding.room_id",
            maximum=160,
        ),
        "caller_id": _identifier(
            raw.get("caller_id"),
            "$.invocation_binding.caller_id",
            maximum=160,
        ),
        "project_id": _identifier(
            raw.get("project_id"),
            "$.invocation_binding.project_id",
            maximum=160,
        ),
        "source_item_id": _identifier(
            raw.get("source_item_id"),
            "$.invocation_binding.source_item_id",
            maximum=160,
        ),
        "source_revision": _text(
            raw.get("source_revision"),
            "$.invocation_binding.source_revision",
            maximum=160,
        ),
    }


def invocation_binding_from_envelope(envelope: Any) -> dict[str, Any]:
    """Project the exact portable identity from a validated invocation envelope.

    The invocation module remains authoritative for envelope normalization and
    request hashing. This helper deliberately neither accepts nor returns a
    scoped capability secret.
    """

    if type(envelope) is not dict:
        _fail("$.envelope", "must be an exact JSON object")
    source = envelope.get("source")
    if type(source) is not dict:
        _fail("$.envelope.source", "must be an exact JSON object")
    return _normalize_invocation_binding({
        "client_request_id": envelope.get("client_request_id"),
        "request_sha256": envelope.get("request_sha256"),
        "room_id": envelope.get("room_id"),
        "caller_id": envelope.get("caller_id"),
        "project_id": envelope.get("project_id"),
        "source_item_id": source.get("item_id"),
        "source_revision": source.get("revision"),
    })


def _normalize_studio_binding(value: Any) -> dict[str, Any]:
    raw = _object(value, "$.studio_binding", _STUDIO_BINDING_FIELDS)
    result = {
        "round_id": _optional_identifier(
            raw.get("round_id"),
            "$.studio_binding.round_id",
        ),
        "artifact_id": _optional_identifier(
            raw.get("artifact_id"),
            "$.studio_binding.artifact_id",
        ),
        "artifact_version": _positive_or_zero_integer(
            raw.get("artifact_version"),
            "$.studio_binding.artifact_version",
        ),
        "artifact_snapshot_sha256": _sha256(
            raw.get("artifact_snapshot_sha256"),
            "$.studio_binding.artifact_snapshot_sha256",
            allow_empty=True,
        ),
        "manual_chatgpt_session_id": _optional_identifier(
            raw.get("manual_chatgpt_session_id"),
            "$.studio_binding.manual_chatgpt_session_id",
        ),
        "manual_chatgpt_result_sha256": _sha256(
            raw.get("manual_chatgpt_result_sha256"),
            "$.studio_binding.manual_chatgpt_result_sha256",
            allow_empty=True,
        ),
        "decision_card_sha256": _sha256(
            raw.get("decision_card_sha256"),
            "$.studio_binding.decision_card_sha256",
            allow_empty=True,
        ),
    }
    artifact_bound = bool(result["artifact_id"])
    if artifact_bound != bool(
        result["artifact_version"] > 0 and result["artifact_snapshot_sha256"]
    ):
        _fail(
            "$.studio_binding",
            "artifact identity, positive version, and snapshot hash must be bound together",
        )
    session_bound = bool(result["manual_chatgpt_session_id"])
    if session_bound != bool(result["manual_chatgpt_result_sha256"]):
        _fail(
            "$.studio_binding",
            "manual ChatGPT session and result hash must be bound together",
        )
    if result["decision_card_sha256"] and not session_bound:
        _fail(
            "$.studio_binding.decision_card_sha256",
            "requires a bound manual ChatGPT session",
        )
    return result


def _normalize_domain_context(value: Any) -> dict[str, str]:
    raw = _object(value, "$.domain_context", _DOMAIN_CONTEXT_FIELDS)
    return {
        "schema_version": _identifier(
            raw.get("schema_version"),
            "$.domain_context.schema_version",
        ),
        "schema_sha256": _sha256(
            raw.get("schema_sha256"),
            "$.domain_context.schema_sha256",
        ),
        "payload_sha256": _sha256(
            raw.get("payload_sha256"),
            "$.domain_context.payload_sha256",
        ),
    }


def _normalize_sources(
    value: Any,
    *,
    invocation: Mapping[str, Any],
    studio: Mapping[str, Any],
    domain_context: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    rows = _array(value, "$.source_manifest", maximum=256, minimum=2)
    sources: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(rows):
        path = f"$.source_manifest[{index}]"
        raw = _object(item, path, _SOURCE_FIELDS)
        source_id = _identifier(raw.get("source_id"), f"{path}.source_id")
        if source_id in by_id:
            _fail(f"{path}.source_id", "must be unique")
        source_kind = _enum(raw.get("source_kind"), SOURCE_KINDS, f"{path}.source_kind")
        provenance = _text(raw.get("provenance"), f"{path}.provenance", maximum=80)
        trust_state = _text(raw.get("trust_state"), f"{path}.trust_state", maximum=80)
        if (provenance, trust_state) not in _SOURCE_POLICIES[source_kind]:
            _fail(path, "source provenance and trust state do not match source kind")
        normalized = {
            "source_id": source_id,
            "source_kind": source_kind,
            "record_id": _identifier(
                raw.get("record_id"),
                f"{path}.record_id",
                maximum=160 if source_kind == "project_source" else 128,
            ),
            "record_revision": _text(
                raw.get("record_revision"),
                f"{path}.record_revision",
                maximum=160,
            ),
            "record_sha256": _sha256(
                raw.get("record_sha256"),
                f"{path}.record_sha256",
            ),
            "provenance": provenance,
            "trust_state": trust_state,
        }
        by_id[source_id] = normalized
        sources.append(normalized)

    project_sources = [row for row in sources if row["source_kind"] == "project_source"]
    if len(project_sources) != 1:
        _fail("$.source_manifest", "must contain exactly one project_source")
    project_source = project_sources[0]
    if (
        project_source["record_id"] != invocation["source_item_id"]
        or project_source["record_revision"] != invocation["source_revision"]
    ):
        _fail("$.source_manifest", "project_source does not match invocation source identity")

    domain_sources = [row for row in sources if row["source_kind"] == "domain_context"]
    if len(domain_sources) != 1 or domain_sources[0]["record_sha256"] != domain_context[
        "payload_sha256"
    ]:
        _fail("$.source_manifest", "must bind exactly one domain_context payload hash")

    def require_source(
        *,
        source_kind: str,
        record_id: str,
        record_revision: str = "",
        record_sha256: str = "",
        path: str,
    ) -> None:
        if not record_id:
            return
        matches = [
            row
            for row in sources
            if row["source_kind"] == source_kind
            and row["record_id"] == record_id
            and (not record_revision or row["record_revision"] == record_revision)
            and (not record_sha256 or row["record_sha256"] == record_sha256)
        ]
        if len(matches) != 1:
            _fail(path, f"requires one exact {source_kind} source binding")

    require_source(
        source_kind="studio_round_checkpoint",
        record_id=str(studio["round_id"]),
        path="$.studio_binding.round_id",
    )
    require_source(
        source_kind="studio_artifact_version",
        record_id=str(studio["artifact_id"]),
        record_revision=str(studio["artifact_version"]),
        record_sha256=str(studio["artifact_snapshot_sha256"]),
        path="$.studio_binding.artifact_id",
    )
    require_source(
        source_kind="manual_chatgpt_import",
        record_id=str(studio["manual_chatgpt_session_id"]),
        record_sha256=str(studio["manual_chatgpt_result_sha256"]),
        path="$.studio_binding.manual_chatgpt_session_id",
    )
    if studio["decision_card_sha256"]:
        card_sources = [
            row
            for row in sources
            if row["source_kind"] == "manual_chatgpt_decision_card"
            and row["record_id"] == studio["manual_chatgpt_session_id"]
            and row["record_sha256"] == studio["decision_card_sha256"]
        ]
        if len(card_sources) != 1:
            _fail(
                "$.studio_binding.decision_card_sha256",
                "requires one exact manual_chatgpt_decision_card source binding",
            )
    sources.sort(key=lambda row: row["source_id"])
    return sources, {row["source_id"]: row for row in sources}


def _normalize_evidence(
    value: Any,
    source_by_id: Mapping[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    rows = _array(value, "$.evidence_manifest", maximum=1_000)
    evidence: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(rows):
        path = f"$.evidence_manifest[{index}]"
        raw = _object(item, path, _EVIDENCE_FIELDS)
        evidence_id = _identifier(raw.get("evidence_id"), f"{path}.evidence_id")
        if evidence_id in by_id:
            _fail(f"{path}.evidence_id", "must be unique")
        source_id = _identifier(raw.get("source_id"), f"{path}.source_id")
        if source_id not in source_by_id:
            _fail(f"{path}.source_id", "references an unknown source")
        role = _enum(raw.get("evidence_role"), EVIDENCE_ROLES, f"{path}.evidence_role")
        status = _enum(
            raw.get("verification_status"),
            EVIDENCE_VERIFICATION_STATUSES,
            f"{path}.verification_status",
        )
        note = _optional_text(raw.get("review_note"), f"{path}.review_note", maximum=1_000)
        if (role == "counter" or status == "disputed") and not note:
            _fail(f"{path}.review_note", "is required for counter or disputed evidence")
        normalized = {
            "evidence_id": evidence_id,
            "source_id": source_id,
            "evidence_role": role,
            "verification_status": status,
            "review_note": note,
        }
        by_id[evidence_id] = normalized
        evidence.append(normalized)
    evidence.sort(key=lambda row: row["evidence_id"])
    return evidence, {row["evidence_id"]: row for row in evidence}


def _normalize_text_binding(
    value: Any,
    path: str,
    evidence_by_id: Mapping[str, dict[str, Any]],
    *,
    evidence_minimum: int = 0,
    maximum: int = 10_000,
) -> dict[str, Any]:
    raw = _object(value, path, {"text", "evidence_ids"})
    return {
        "text": _text(raw.get("text"), f"{path}.text", maximum=maximum),
        "evidence_ids": _evidence_ids(
            raw.get("evidence_ids"),
            f"{path}.evidence_ids",
            evidence_by_id,
            minimum=evidence_minimum,
        ),
    }


def _normalize_questions(
    value: Any,
    path: str,
    evidence_by_id: Mapping[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = _array(value, path, maximum=30)
    questions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(rows):
        item_path = f"{path}[{index}]"
        raw = _object(item, item_path, {"question_id", "text", "evidence_ids"})
        question_id = _identifier(raw.get("question_id"), f"{item_path}.question_id")
        if question_id in seen:
            _fail(f"{item_path}.question_id", "must be unique")
        seen.add(question_id)
        questions.append({
            "question_id": question_id,
            "text": _text(raw.get("text"), f"{item_path}.text", maximum=3_000),
            "evidence_ids": _evidence_ids(
                raw.get("evidence_ids"),
                f"{item_path}.evidence_ids",
                evidence_by_id,
            ),
        })
    return questions


def _normalize_decision_profile(
    value: Any,
    evidence_by_id: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    fields = {
        "version", "question", "summary", "criteria", "options",
        "recommendation", "open_questions",
    }
    raw = _object(value, "$.profile", fields)
    if raw.get("version") != DECISION_PROFILE_VERSION:
        _fail("$.profile.version", "does not match decision_v1")
    criteria_rows = _array(raw.get("criteria"), "$.profile.criteria", maximum=16)
    criteria: list[dict[str, Any]] = []
    criterion_ids: set[str] = set()
    for index, item in enumerate(criteria_rows):
        path = f"$.profile.criteria[{index}]"
        row = _object(
            item,
            path,
            {"criterion_id", "title", "description", "evidence_ids"},
        )
        criterion_id = _identifier(row.get("criterion_id"), f"{path}.criterion_id")
        if criterion_id in criterion_ids:
            _fail(f"{path}.criterion_id", "must be unique")
        criterion_ids.add(criterion_id)
        criteria.append({
            "criterion_id": criterion_id,
            "title": _text(row.get("title"), f"{path}.title", maximum=240),
            "description": _text(
                row.get("description"),
                f"{path}.description",
                maximum=3_000,
            ),
            "evidence_ids": _evidence_ids(
                row.get("evidence_ids"),
                f"{path}.evidence_ids",
                evidence_by_id,
            ),
        })

    option_rows = _array(raw.get("options"), "$.profile.options", maximum=8, minimum=1)
    options: list[dict[str, Any]] = []
    option_ids: set[str] = set()
    for index, item in enumerate(option_rows):
        path = f"$.profile.options[{index}]"
        row = _object(item, path, {
            "option_id", "title", "description", "benefits", "risks",
            "tradeoffs", "evidence_ids",
        })
        option_id = _identifier(row.get("option_id"), f"{path}.option_id")
        if option_id in option_ids:
            _fail(f"{path}.option_id", "must be unique")
        option_ids.add(option_id)
        options.append({
            "option_id": option_id,
            "title": _text(row.get("title"), f"{path}.title", maximum=240),
            "description": _text(
                row.get("description"),
                f"{path}.description",
                maximum=5_000,
            ),
            "benefits": _string_list(
                row.get("benefits"),
                f"{path}.benefits",
                maximum_items=16,
                maximum_text=1_000,
            ),
            "risks": _string_list(
                row.get("risks"),
                f"{path}.risks",
                maximum_items=16,
                maximum_text=1_000,
            ),
            "tradeoffs": _string_list(
                row.get("tradeoffs"),
                f"{path}.tradeoffs",
                maximum_items=16,
                maximum_text=1_000,
            ),
            "evidence_ids": _evidence_ids(
                row.get("evidence_ids"),
                f"{path}.evidence_ids",
                evidence_by_id,
                minimum=1,
            ),
        })

    recommendation_raw = _object(
        raw.get("recommendation"),
        "$.profile.recommendation",
        {"state", "option_id", "rationale", "evidence_ids"},
    )
    state = _enum(
        recommendation_raw.get("state"),
        {"candidate", "deferred", "withheld"},
        "$.profile.recommendation.state",
    )
    option_id = _optional_identifier(
        recommendation_raw.get("option_id"),
        "$.profile.recommendation.option_id",
    )
    recommendation_evidence = _evidence_ids(
        recommendation_raw.get("evidence_ids"),
        "$.profile.recommendation.evidence_ids",
        evidence_by_id,
        minimum=1 if state in {"candidate", "deferred"} else 0,
    )
    if state == "candidate":
        if len(options) < 2:
            _fail("$.profile.options", "candidate recommendation requires at least two options")
        if option_id not in option_ids:
            _fail("$.profile.recommendation.option_id", "must reference a sealed option")
    elif option_id:
        _fail("$.profile.recommendation.option_id", "must be empty unless state is candidate")

    return {
        "version": DECISION_PROFILE_VERSION,
        "question": _text(raw.get("question"), "$.profile.question", maximum=4_000),
        "summary": _normalize_text_binding(
            raw.get("summary"),
            "$.profile.summary",
            evidence_by_id,
            evidence_minimum=1,
        ),
        "criteria": criteria,
        "options": options,
        "recommendation": {
            "state": state,
            "option_id": option_id,
            "rationale": _text(
                recommendation_raw.get("rationale"),
                "$.profile.recommendation.rationale",
                maximum=5_000,
            ),
            "evidence_ids": recommendation_evidence,
        },
        "open_questions": _normalize_questions(
            raw.get("open_questions"),
            "$.profile.open_questions",
            evidence_by_id,
        ),
    }


def _normalize_research_claims(
    value: Any,
    path: str,
    evidence_by_id: Mapping[str, dict[str, Any]],
    source_by_id: Mapping[str, dict[str, Any]],
    *,
    seen_claim_ids: set[str],
) -> list[dict[str, Any]]:
    rows = _array(value, path, maximum=40)
    claims: list[dict[str, Any]] = []
    for index, item in enumerate(rows):
        item_path = f"{path}[{index}]"
        raw = _object(item, item_path, {
            "claim_id", "statement", "claim_kind", "support_state",
            "evidence_ids", "uncertainty",
        })
        claim_id = _identifier(raw.get("claim_id"), f"{item_path}.claim_id")
        if claim_id in seen_claim_ids:
            _fail(f"{item_path}.claim_id", "must be unique across report claims")
        seen_claim_ids.add(claim_id)
        kind = _enum(
            raw.get("claim_kind"),
            {"deterministic_fact", "sourced_fact", "model_inference", "interpretation"},
            f"{item_path}.claim_kind",
        )
        refs = _evidence_ids(
            raw.get("evidence_ids"),
            f"{item_path}.evidence_ids",
            evidence_by_id,
            minimum=1,
        )
        if kind == "deterministic_fact" and not any(
            source_by_id[evidence_by_id[reference]["source_id"]]["source_kind"]
            == "deterministic_engine_receipt"
            and source_by_id[evidence_by_id[reference]["source_id"]]["trust_state"]
            == "deterministic_contract_verified"
            for reference in refs
        ):
            _fail(
                f"{item_path}.evidence_ids",
                "deterministic_fact requires a verified deterministic engine receipt",
            )
        claims.append({
            "claim_id": claim_id,
            "statement": _text(
                raw.get("statement"),
                f"{item_path}.statement",
                maximum=5_000,
            ),
            "claim_kind": kind,
            "support_state": _enum(
                raw.get("support_state"),
                {"supported", "mixed", "insufficient_evidence"},
                f"{item_path}.support_state",
            ),
            "evidence_ids": refs,
            "uncertainty": _text(
                raw.get("uncertainty"),
                f"{item_path}.uncertainty",
                maximum=2_000,
            ),
        })
    return claims


def _normalize_research_profile(
    value: Any,
    evidence_by_id: Mapping[str, dict[str, Any]],
    source_by_id: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    fields = {
        "version", "title", "scope", "summary", "findings", "counterpoints",
        "limitations", "open_questions", "conclusion",
    }
    raw = _object(value, "$.profile", fields)
    if raw.get("version") != RESEARCH_REPORT_PROFILE_VERSION:
        _fail("$.profile.version", "does not match research_report_v1")
    scope_raw = _object(
        raw.get("scope"),
        "$.profile.scope",
        {"subject", "data_cutoff_utc"},
    )
    cutoff = _optional_text(
        scope_raw.get("data_cutoff_utc"),
        "$.profile.scope.data_cutoff_utc",
        maximum=20,
    )
    if cutoff:
        if not _UTC_PATTERN.fullmatch(cutoff):
            _fail("$.profile.scope.data_cutoff_utc", "must be a canonical UTC timestamp")
        try:
            datetime.strptime(cutoff, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            _fail("$.profile.scope.data_cutoff_utc", "must be a valid UTC timestamp")

    seen_claim_ids: set[str] = set()
    findings = _normalize_research_claims(
        raw.get("findings"),
        "$.profile.findings",
        evidence_by_id,
        source_by_id,
        seen_claim_ids=seen_claim_ids,
    )
    counterpoints = _normalize_research_claims(
        raw.get("counterpoints"),
        "$.profile.counterpoints",
        evidence_by_id,
        source_by_id,
        seen_claim_ids=seen_claim_ids,
    )
    limitation_rows = _array(raw.get("limitations"), "$.profile.limitations", maximum=40)
    limitations: list[dict[str, Any]] = []
    limitation_ids: set[str] = set()
    for index, item in enumerate(limitation_rows):
        path = f"$.profile.limitations[{index}]"
        row = _object(item, path, {"limitation_id", "text", "evidence_ids"})
        limitation_id = _identifier(row.get("limitation_id"), f"{path}.limitation_id")
        if limitation_id in limitation_ids:
            _fail(f"{path}.limitation_id", "must be unique")
        limitation_ids.add(limitation_id)
        limitations.append({
            "limitation_id": limitation_id,
            "text": _text(row.get("text"), f"{path}.text", maximum=3_000),
            "evidence_ids": _evidence_ids(
                row.get("evidence_ids"),
                f"{path}.evidence_ids",
                evidence_by_id,
            ),
        })

    conclusion_raw = _object(
        raw.get("conclusion"),
        "$.profile.conclusion",
        {"state", "text", "evidence_ids"},
    )
    conclusion_state = _enum(
        conclusion_raw.get("state"),
        {"supported", "mixed", "insufficient_evidence", "withheld"},
        "$.profile.conclusion.state",
    )
    conclusion = {
        "state": conclusion_state,
        "text": _text(
            conclusion_raw.get("text"),
            "$.profile.conclusion.text",
            maximum=10_000,
        ),
        "evidence_ids": _evidence_ids(
            conclusion_raw.get("evidence_ids"),
            "$.profile.conclusion.evidence_ids",
            evidence_by_id,
            minimum=1 if conclusion_state in {"supported", "mixed"} else 0,
        ),
    }
    if not findings and conclusion_state not in {"insufficient_evidence", "withheld"}:
        _fail("$.profile.findings", "a supported or mixed report requires findings")

    return {
        "version": RESEARCH_REPORT_PROFILE_VERSION,
        "title": _text(raw.get("title"), "$.profile.title", maximum=240),
        "scope": {
            "subject": _text(
                scope_raw.get("subject"),
                "$.profile.scope.subject",
                maximum=2_000,
            ),
            "data_cutoff_utc": cutoff,
        },
        "summary": _normalize_text_binding(
            raw.get("summary"),
            "$.profile.summary",
            evidence_by_id,
            evidence_minimum=1,
        ),
        "findings": findings,
        "counterpoints": counterpoints,
        "limitations": limitations,
        "open_questions": _normalize_questions(
            raw.get("open_questions"),
            "$.profile.open_questions",
            evidence_by_id,
        ),
        "conclusion": conclusion,
    }


def _normalize_artifact_profile(
    value: Any,
    evidence_by_id: Mapping[str, dict[str, Any]],
    source_by_id: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    fields = {
        "version", "artifact_kind", "title", "audience", "purpose", "sections",
        "asset_briefs", "export_plan", "delivery",
    }
    raw = _object(value, "$.profile", fields)
    if raw.get("version") != ARTIFACT_DRAFT_PROFILE_VERSION:
        _fail("$.profile.version", "does not match artifact_draft_v1")
    artifact_kind = _enum(
        raw.get("artifact_kind"),
        {"presentation", "document"},
        "$.profile.artifact_kind",
    )
    section_rows = _array(raw.get("sections"), "$.profile.sections", maximum=200, minimum=1)
    sections: list[dict[str, Any]] = []
    section_ids: set[str] = set()
    for index, item in enumerate(section_rows):
        path = f"$.profile.sections[{index}]"
        row = _object(item, path, {
            "section_id", "ordinal", "title", "purpose", "body", "bullets",
            "speaker_notes", "evidence_ids",
        })
        section_id = _identifier(row.get("section_id"), f"{path}.section_id")
        if section_id in section_ids:
            _fail(f"{path}.section_id", "must be unique")
        section_ids.add(section_id)
        ordinal = _positive_or_zero_integer(row.get("ordinal"), f"{path}.ordinal")
        if ordinal != index + 1:
            _fail(f"{path}.ordinal", "must be contiguous and match authored order")
        body = _optional_text(row.get("body"), f"{path}.body", maximum=12_000)
        bullets = _string_list(
            row.get("bullets"),
            f"{path}.bullets",
            maximum_items=40,
            maximum_text=2_000,
        )
        if not body and not bullets:
            _fail(path, "must contain body text or at least one bullet")
        sections.append({
            "section_id": section_id,
            "ordinal": ordinal,
            "title": _text(row.get("title"), f"{path}.title", maximum=240),
            "purpose": _text(row.get("purpose"), f"{path}.purpose", maximum=2_000),
            "body": body,
            "bullets": bullets,
            "speaker_notes": _optional_text(
                row.get("speaker_notes"),
                f"{path}.speaker_notes",
                maximum=8_000,
            ),
            "evidence_ids": _evidence_ids(
                row.get("evidence_ids"),
                f"{path}.evidence_ids",
                evidence_by_id,
            ),
        })

    asset_rows = _array(raw.get("asset_briefs"), "$.profile.asset_briefs", maximum=200)
    assets: list[dict[str, Any]] = []
    asset_ids: set[str] = set()
    for index, item in enumerate(asset_rows):
        path = f"$.profile.asset_briefs[{index}]"
        row = _object(
            item,
            path,
            {"asset_id", "asset_kind", "description", "section_id", "evidence_ids"},
        )
        asset_id = _identifier(row.get("asset_id"), f"{path}.asset_id")
        if asset_id in asset_ids:
            _fail(f"{path}.asset_id", "must be unique")
        asset_ids.add(asset_id)
        section_id = _identifier(row.get("section_id"), f"{path}.section_id")
        if section_id not in section_ids:
            _fail(f"{path}.section_id", "must reference a sealed section")
        assets.append({
            "asset_id": asset_id,
            "asset_kind": _enum(
                row.get("asset_kind"),
                {"image", "chart", "table", "diagram"},
                f"{path}.asset_kind",
            ),
            "description": _text(
                row.get("description"),
                f"{path}.description",
                maximum=3_000,
            ),
            "section_id": section_id,
            "evidence_ids": _evidence_ids(
                row.get("evidence_ids"),
                f"{path}.evidence_ids",
                evidence_by_id,
            ),
        })

    export_raw = _object(raw.get("export_plan"), "$.profile.export_plan", {
        "target_format", "suggested_filename", "renderer_id", "renderer_version",
        "user_selected_destination_required", "overwrite_allowed", "render_required",
        "verification_required",
    })
    target_format = _enum(
        export_raw.get("target_format"),
        {"pptx", "docx", "pdf"},
        "$.profile.export_plan.target_format",
    )
    filename = _text(
        export_raw.get("suggested_filename"),
        "$.profile.export_plan.suggested_filename",
        maximum=180,
    )
    if (
        filename in {".", ".."}
        or any(character in filename for character in '<>:"/\\|?*')
        or any(ord(character) < 32 for character in filename)
        or filename.endswith((".", " "))
        or not filename.lower().endswith(f".{target_format}")
    ):
        _fail(
            "$.profile.export_plan.suggested_filename",
            "must be a path-free basename with the target format extension",
        )
    export_plan = {
        "target_format": target_format,
        "suggested_filename": filename,
        "renderer_id": _identifier(
            export_raw.get("renderer_id"),
            "$.profile.export_plan.renderer_id",
        ),
        "renderer_version": _text(
            export_raw.get("renderer_version"),
            "$.profile.export_plan.renderer_version",
            maximum=80,
        ),
        "user_selected_destination_required": _fixed_boolean(
            export_raw.get("user_selected_destination_required"),
            True,
            "$.profile.export_plan.user_selected_destination_required",
        ),
        "overwrite_allowed": _fixed_boolean(
            export_raw.get("overwrite_allowed"),
            False,
            "$.profile.export_plan.overwrite_allowed",
        ),
        "render_required": _fixed_boolean(
            export_raw.get("render_required"),
            True,
            "$.profile.export_plan.render_required",
        ),
        "verification_required": _fixed_boolean(
            export_raw.get("verification_required"),
            True,
            "$.profile.export_plan.verification_required",
        ),
    }

    delivery_raw = _object(raw.get("delivery"), "$.profile.delivery", {
        "render_state", "render_package_sha256", "verification_state",
        "verification_receipt_sha256", "export_state", "export_receipt_sha256",
        "failure_codes",
    })
    render_state = _enum(
        delivery_raw.get("render_state"),
        ARTIFACT_RENDER_STATES,
        "$.profile.delivery.render_state",
    )
    verification_state = _enum(
        delivery_raw.get("verification_state"),
        ARTIFACT_VERIFICATION_STATES,
        "$.profile.delivery.verification_state",
    )
    export_state = _enum(
        delivery_raw.get("export_state"),
        ARTIFACT_EXPORT_STATES,
        "$.profile.delivery.export_state",
    )
    render_hash = _sha256(
        delivery_raw.get("render_package_sha256"),
        "$.profile.delivery.render_package_sha256",
        allow_empty=True,
    )
    verification_hash = _sha256(
        delivery_raw.get("verification_receipt_sha256"),
        "$.profile.delivery.verification_receipt_sha256",
        allow_empty=True,
    )
    export_hash = _sha256(
        delivery_raw.get("export_receipt_sha256"),
        "$.profile.delivery.export_receipt_sha256",
        allow_empty=True,
    )
    failure_codes = _string_list(
        delivery_raw.get("failure_codes"),
        "$.profile.delivery.failure_codes",
        maximum_items=32,
        maximum_text=80,
        sort_values=True,
    )
    if any(code not in ARTIFACT_DELIVERY_FAILURE_CODES for code in failure_codes):
        _fail("$.profile.delivery.failure_codes", "contains an unknown failure code")
    if render_state == "rendered":
        if not render_hash:
            _fail("$.profile.delivery.render_package_sha256", "is required after rendering")
    elif render_hash:
        _fail("$.profile.delivery.render_package_sha256", "must be empty without a rendered package")
    if render_state in {"not_rendered", "failed"} and verification_state != "not_run":
        _fail("$.profile.delivery.verification_state", "cannot verify without a rendered package")
    if verification_state == "not_run":
        if verification_hash:
            _fail("$.profile.delivery.verification_receipt_sha256", "must be empty before verification")
    elif not verification_hash:
        _fail("$.profile.delivery.verification_receipt_sha256", "is required after verification")
    if export_state == "exported":
        if verification_state != "verified" or not export_hash:
            _fail("$.profile.delivery", "export requires a verified render and export receipt")
    elif export_hash:
        _fail("$.profile.delivery.export_receipt_sha256", "must be empty before export")
    if (render_state == "failed" or verification_state == "failed") and not failure_codes:
        _fail("$.profile.delivery.failure_codes", "is required for failed delivery states")
    if failure_codes and render_state != "failed" and verification_state not in {
        "failed", "needs_user_review",
    }:
        _fail("$.profile.delivery.failure_codes", "does not match the delivery state")

    def require_receipt_source(source_kind: str, receipt_sha256: str, path: str) -> None:
        if not receipt_sha256:
            return
        matches = [
            source
            for source in source_by_id.values()
            if source["source_kind"] == source_kind
            and source["record_sha256"] == receipt_sha256
        ]
        if len(matches) != 1:
            _fail(path, f"requires one exact {source_kind} source binding")

    require_receipt_source(
        "artifact_render_package",
        render_hash,
        "$.profile.delivery.render_package_sha256",
    )
    require_receipt_source(
        "render_verification_receipt",
        verification_hash,
        "$.profile.delivery.verification_receipt_sha256",
    )
    require_receipt_source(
        "artifact_export_receipt",
        export_hash,
        "$.profile.delivery.export_receipt_sha256",
    )

    return {
        "version": ARTIFACT_DRAFT_PROFILE_VERSION,
        "artifact_kind": artifact_kind,
        "title": _text(raw.get("title"), "$.profile.title", maximum=240),
        "audience": _text(raw.get("audience"), "$.profile.audience", maximum=2_000),
        "purpose": _text(raw.get("purpose"), "$.profile.purpose", maximum=3_000),
        "sections": sections,
        "asset_briefs": assets,
        "export_plan": export_plan,
        "delivery": {
            "render_state": render_state,
            "render_package_sha256": render_hash,
            "verification_state": verification_state,
            "verification_receipt_sha256": verification_hash,
            "export_state": export_state,
            "export_receipt_sha256": export_hash,
            "failure_codes": failure_codes,
        },
    }


def _normalize_profile(
    profile_version: str,
    value: Any,
    evidence_by_id: Mapping[str, dict[str, Any]],
    source_by_id: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    if profile_version == DECISION_PROFILE_VERSION:
        return _normalize_decision_profile(value, evidence_by_id)
    if profile_version == RESEARCH_REPORT_PROFILE_VERSION:
        return _normalize_research_profile(value, evidence_by_id, source_by_id)
    if profile_version == ARTIFACT_DRAFT_PROFILE_VERSION:
        return _normalize_artifact_profile(value, evidence_by_id, source_by_id)
    _fail("$.result_profile", "is not implemented")


def _normalize_independent_review(
    value: Any,
    source_by_id: Mapping[str, dict[str, Any]],
    evidence_by_id: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    raw = _object(value, "$.independent_review", _INDEPENDENT_REVIEW_FIELDS)
    status = _enum(
        raw.get("status"),
        REVIEW_STATUSES,
        "$.independent_review.status",
    )
    source_ids = _string_list(
        raw.get("source_ids"),
        "$.independent_review.source_ids",
        maximum_items=16,
        maximum_text=128,
        sort_values=True,
    )
    for source_id in source_ids:
        source = source_by_id.get(source_id)
        if not source or source["source_kind"] != "api_review_bundle":
            _fail(
                "$.independent_review.source_ids",
                "must reference only sealed API review sources",
            )
    finding_rows = _array(raw.get("findings"), "$.independent_review.findings", maximum=64)
    findings: list[dict[str, Any]] = []
    finding_ids: set[str] = set()
    for index, item in enumerate(finding_rows):
        path = f"$.independent_review.findings[{index}]"
        row = _object(item, path, _REVIEW_FINDING_FIELDS)
        finding_id = _identifier(row.get("finding_id"), f"{path}.finding_id")
        if finding_id in finding_ids:
            _fail(f"{path}.finding_id", "must be unique")
        finding_ids.add(finding_id)
        findings.append({
            "finding_id": finding_id,
            "severity": _enum(
                row.get("severity"),
                REVIEW_SEVERITIES,
                f"{path}.severity",
            ),
            "statement": _text(
                row.get("statement"),
                f"{path}.statement",
                maximum=3_000,
            ),
            "rationale": _text(
                row.get("rationale"),
                f"{path}.rationale",
                maximum=5_000,
            ),
            "evidence_ids": _evidence_ids(
                row.get("evidence_ids"),
                f"{path}.evidence_ids",
                evidence_by_id,
            ),
        })
    findings.sort(key=lambda row: row["finding_id"])
    open_questions = _normalize_questions(
        raw.get("open_questions"),
        "$.independent_review.open_questions",
        evidence_by_id,
    )
    review_hash = _sha256(
        raw.get("review_bundle_sha256"),
        "$.independent_review.review_bundle_sha256",
        allow_empty=True,
    )
    if status == "not_run":
        if source_ids or findings or open_questions or review_hash:
            _fail("$.independent_review", "not_run review must be empty")
    else:
        if not source_ids:
            _fail("$.independent_review.source_ids", "review requires API review sources")
        expected_hash = canonical_sha256([
            source_by_id[source_id]["record_sha256"] for source_id in source_ids
        ])
        if review_hash != expected_hash:
            _fail("$.independent_review.review_bundle_sha256", "does not bind review sources")
        derived_status = (
            "blocked"
            if any(row["severity"] == "blocking" for row in findings)
            else "concern"
            if findings or open_questions
            else "passed"
        )
        if status != derived_status:
            _fail("$.independent_review.status", "does not match deterministic findings state")
    return {
        "status": status,
        "source_ids": source_ids,
        "findings": findings,
        "open_questions": open_questions,
        "review_bundle_sha256": review_hash,
    }


def _normalize_user_boundary(
    value: Any,
    *,
    profile_version: str,
    profile: Mapping[str, Any],
    independent_review: Mapping[str, Any],
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    raw = _object(value, "$.user_boundary", _USER_BOUNDARY_FIELDS)
    status = _enum(
        raw.get("status"),
        USER_BOUNDARY_STATUSES,
        "$.user_boundary.status",
    )
    outcome = _enum(
        raw.get("outcome"),
        USER_BOUNDARY_OUTCOMES,
        "$.user_boundary.outcome",
    )
    record_id = _optional_identifier(raw.get("record_id"), "$.user_boundary.record_id")
    record_version = _optional_text(
        raw.get("record_version"),
        "$.user_boundary.record_version",
        maximum=80,
    )
    record_sha256 = _sha256(
        raw.get("record_sha256"),
        "$.user_boundary.record_sha256",
        allow_empty=True,
    )
    selected_item_id = _optional_identifier(
        raw.get("selected_item_id"),
        "$.user_boundary.selected_item_id",
    )
    if status == "pending":
        if outcome != "unresolved" or any(
            (record_id, record_version, record_sha256, selected_item_id)
        ):
            _fail("$.user_boundary", "pending boundary must remain unresolved and unbound")
    else:
        if outcome == "unresolved" or not record_id or not record_version or not record_sha256:
            _fail("$.user_boundary", "recorded boundary requires a resolved immutable record")
        decision_sources = [
            source
            for source in sources
            if source["source_kind"] == "user_decision_record"
            and source["record_id"] == record_id
            and source["record_revision"] == record_version
            and source["record_sha256"] == record_sha256
        ]
        if len(decision_sources) != 1:
            _fail("$.user_boundary", "recorded boundary requires one exact decision source")
        if independent_review["status"] == "blocked" and outcome == "accepted":
            _fail("$.user_boundary.outcome", "blocked independent review cannot be accepted")
        if profile_version == DECISION_PROFILE_VERSION and outcome == "accepted":
            option_ids = {
                str(option["option_id"])
                for option in profile.get("options") or []
            }
            if selected_item_id not in option_ids:
                _fail("$.user_boundary.selected_item_id", "must select one sealed decision option")
        elif selected_item_id:
            _fail(
                "$.user_boundary.selected_item_id",
                "is allowed only for an accepted decision_v1 result",
            )
    return {
        "status": status,
        "outcome": outcome,
        "record_id": record_id,
        "record_version": record_version,
        "record_sha256": record_sha256,
        "selected_item_id": selected_item_id,
    }


def _normalize_build_payload(value: Any) -> dict[str, Any]:
    raw = _object(value, "$", _BUILD_FIELDS)
    invocation = _normalize_invocation_binding(raw.get("invocation_binding"))
    studio = _normalize_studio_binding(raw.get("studio_binding"))
    workflow_kind = _enum(
        raw.get("workflow_kind"),
        set(RESULT_PROFILE_WORKFLOW_KINDS.values()),
        "$.workflow_kind",
    )
    profile_version = _enum(
        raw.get("result_profile"),
        set(RESULT_PROFILE_WORKFLOW_KINDS),
        "$.result_profile",
    )
    if workflow_kind != RESULT_PROFILE_WORKFLOW_KINDS[profile_version]:
        _fail("$.workflow_kind", "does not match result_profile")
    domain_context = _normalize_domain_context(raw.get("domain_context"))
    sources, source_by_id = _normalize_sources(
        raw.get("source_manifest"),
        invocation=invocation,
        studio=studio,
        domain_context=domain_context,
    )
    evidence, evidence_by_id = _normalize_evidence(
        raw.get("evidence_manifest"),
        source_by_id,
    )
    profile = _normalize_profile(
        profile_version,
        raw.get("profile"),
        evidence_by_id,
        source_by_id,
    )
    independent_review = _normalize_independent_review(
        raw.get("independent_review"),
        source_by_id,
        evidence_by_id,
    )
    user_boundary = _normalize_user_boundary(
        raw.get("user_boundary"),
        profile_version=profile_version,
        profile=profile,
        independent_review=independent_review,
        sources=sources,
    )
    return {
        "invocation_binding": invocation,
        "studio_binding": studio,
        "workflow_kind": workflow_kind,
        "result_profile": profile_version,
        "domain_context": domain_context,
        "source_manifest": sources,
        "evidence_manifest": evidence,
        "profile": profile,
        "independent_review": independent_review,
        "user_boundary": user_boundary,
    }


def build_collaboration_result(payload: Any) -> dict[str, Any]:
    """Build one deterministic, portable, non-executing collaboration result.

    The function is pure: it has no Store, Provider, filesystem, clock, or
    network dependency. Host-generated identity and hash fields are rejected by
    the closed build input and installed only after full normalization.
    """

    normalized = _normalize_build_payload(payload)
    profile_version = normalized["result_profile"]
    profile = normalized["profile"]
    body: dict[str, Any] = {
        "version": COLLABORATION_RESULT_VERSION,
        "schema_sha256": COLLABORATION_RESULT_SCHEMA_SHA256,
        **normalized,
        "profile_schema_sha256": COLLABORATION_PROFILE_SCHEMA_SHA256[
            profile_version
        ],
        "profile_sha256": canonical_sha256(profile),
        "safety": copy.deepcopy(FIXED_RESULT_SAFETY),
    }
    result_id = "result_" + canonical_sha256(body)[:32]
    result = {**body, "result_id": result_id}
    result["result_sha256"] = canonical_sha256(result)
    return result


def verify_collaboration_result(
    value: Any,
    *,
    expected_envelope: Any | None = None,
) -> dict[str, Any]:
    """Verify all schema, reference, safety, identity, and hash bindings."""

    raw = _object(value, "$", _ROOT_FIELDS)
    if raw.get("version") != COLLABORATION_RESULT_VERSION:
        _fail("$.version", "is unsupported")
    if raw.get("schema_sha256") != COLLABORATION_RESULT_SCHEMA_SHA256:
        _fail("$.schema_sha256", "does not match the compiled result schema")
    safety = raw.get("safety")
    if (
        type(safety) is not dict
        or set(safety) != set(FIXED_RESULT_SAFETY)
        or any(
            type(safety[field]) is not type(expected)
            or safety[field] != expected
            for field, expected in FIXED_RESULT_SAFETY.items()
        )
    ):
        _fail("$.safety", "does not match the fixed no-execution boundary")
    _identifier(raw.get("result_id"), "$.result_id")
    _sha256(raw.get("profile_schema_sha256"), "$.profile_schema_sha256")
    _sha256(raw.get("profile_sha256"), "$.profile_sha256")
    _sha256(raw.get("result_sha256"), "$.result_sha256")

    build_payload = {
        field: copy.deepcopy(raw[field])
        for field in _BUILD_FIELDS
    }
    expected = build_collaboration_result(build_payload)
    if raw != expected:
        _fail("$", "does not match its canonical identity or SHA-256 bindings")
    if expected_envelope is not None:
        expected_binding = invocation_binding_from_envelope(expected_envelope)
        if expected["invocation_binding"] != expected_binding:
            _fail("$.invocation_binding", "does not match the expected invocation envelope")
        envelope_profile = expected_envelope.get("result_profile")
        if envelope_profile != expected["result_profile"]:
            _fail("$.result_profile", "does not match the expected invocation envelope")
        envelope_source = expected_envelope.get("source")
        if type(envelope_source) is not dict:
            _fail("$.envelope.source", "must be an exact JSON object")
        source_content_sha256 = envelope_source.get("content_sha256")
        input_manifest = expected_envelope.get("input_manifest")
        manifest_content_sha256 = (
            input_manifest.get("content_sha256")
            if type(input_manifest) is dict
            else None
        )
        if (
            source_content_sha256 is not None
            and manifest_content_sha256 is not None
            and source_content_sha256 != manifest_content_sha256
        ):
            _fail(
                "$.envelope",
                "source and input manifest content hashes do not match",
            )
        expected_source_sha256 = _sha256(
            (
                manifest_content_sha256
                if manifest_content_sha256 is not None
                else source_content_sha256
            ),
            "$.envelope.input_manifest.content_sha256",
        )
        project_sources = [
            source
            for source in expected["source_manifest"]
            if source["source_kind"] == "project_source"
        ]
        if project_sources[0]["record_sha256"] != expected_source_sha256:
            _fail(
                "$.source_manifest",
                "project_source content hash does not match the expected invocation envelope",
            )
    return copy.deepcopy(expected)


__all__ = [
    "ARTIFACT_DELIVERY_FAILURE_CODES",
    "ARTIFACT_DRAFT_PROFILE_SCHEMA",
    "ARTIFACT_DRAFT_PROFILE_VERSION",
    "COLLABORATION_PROFILE_SCHEMAS",
    "COLLABORATION_PROFILE_SCHEMA_SHA256",
    "COLLABORATION_RESULT_SCHEMA",
    "COLLABORATION_RESULT_SCHEMA_SHA256",
    "COLLABORATION_RESULT_VERSION",
    "CollaborationResultError",
    "DECISION_PROFILE_SCHEMA",
    "DECISION_PROFILE_VERSION",
    "FIXED_RESULT_SAFETY",
    "RESEARCH_REPORT_PROFILE_SCHEMA",
    "RESEARCH_REPORT_PROFILE_VERSION",
    "RESULT_PROFILE_WORKFLOW_KINDS",
    "build_collaboration_result",
    "invocation_binding_from_envelope",
    "verify_collaboration_result",
]
