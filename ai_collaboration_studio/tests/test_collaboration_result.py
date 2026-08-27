from __future__ import annotations

import copy
import unittest

from backend.collaboration_result import (
    ARTIFACT_DRAFT_PROFILE_VERSION,
    COLLABORATION_PROFILE_SCHEMA_SHA256,
    COLLABORATION_RESULT_SCHEMA_SHA256,
    CollaborationResultError,
    DECISION_PROFILE_VERSION,
    FIXED_RESULT_SAFETY,
    RESEARCH_REPORT_PROFILE_VERSION,
    build_collaboration_result,
    invocation_binding_from_envelope,
    verify_collaboration_result,
)
from backend.decision_lineage import canonical_sha256


SHA_REQUEST = "1" * 64
SHA_PROJECT = "2" * 64
SHA_DOMAIN_SCHEMA = "3" * 64
SHA_DOMAIN_PAYLOAD = "4" * 64
SHA_ROUND = "5" * 64
SHA_ENGINE = "6" * 64
SHA_REVIEW = "7" * 64
SHA_DECISION = "8" * 64


def _base_sources() -> list[dict[str, object]]:
    return [
        {
            "source_id": "source_project",
            "source_kind": "project_source",
            "record_id": "source_item_1",
            "record_revision": "revision_4",
            "record_sha256": SHA_PROJECT,
            "provenance": "caller_supplied",
            "trust_state": "hash_bound_only",
        },
        {
            "source_id": "source_domain",
            "source_kind": "domain_context",
            "record_id": "domain_context_1",
            "record_revision": "1",
            "record_sha256": SHA_DOMAIN_PAYLOAD,
            "provenance": "caller_supplied",
            "trust_state": "hash_bound_only",
        },
        {
            "source_id": "source_round",
            "source_kind": "studio_round_checkpoint",
            "record_id": "round_1",
            "record_revision": "1",
            "record_sha256": SHA_ROUND,
            "provenance": "studio_sealed",
            "trust_state": "host_verified_binding",
        },
    ]


def _base_evidence() -> list[dict[str, str]]:
    return [
        {
            "evidence_id": "evidence_project",
            "source_id": "source_project",
            "evidence_role": "support",
            "verification_status": "source_checked",
            "review_note": "Caller source identity and exact content hash were checked.",
        },
        {
            "evidence_id": "evidence_domain",
            "source_id": "source_domain",
            "evidence_role": "context",
            "verification_status": "source_checked",
            "review_note": "Domain context is hash-bound; domain truth remains separate.",
        },
    ]


def _decision_profile() -> dict[str, object]:
    return {
        "version": DECISION_PROFILE_VERSION,
        "question": "Which bounded research option should the user inspect next?",
        "summary": {
            "text": "Two evidence-bound options remain available for human review.",
            "evidence_ids": ["evidence_project"],
        },
        "criteria": [{
            "criterion_id": "criterion_reversibility",
            "title": "Reversibility",
            "description": "Prefer an option that can be rolled back without external execution.",
            "evidence_ids": ["evidence_project"],
        }],
        "options": [
            {
                "option_id": "option_a",
                "title": "Continue bounded research",
                "description": "Run another deterministic, read-only evidence pass.",
                "benefits": ["Adds reviewable evidence."],
                "risks": ["Consumes more research time."],
                "tradeoffs": ["More evidence in exchange for delay."],
                "evidence_ids": ["evidence_domain", "evidence_project"],
            },
            {
                "option_id": "option_b",
                "title": "Defer the comparison",
                "description": "Keep the current result frozen until inputs improve.",
                "benefits": ["Avoids unsupported inference."],
                "risks": ["Leaves the question unresolved."],
                "tradeoffs": ["Lower error risk but no immediate answer."],
                "evidence_ids": ["evidence_project"],
            },
        ],
        "recommendation": {
            "state": "candidate",
            "option_id": "option_a",
            "rationale": "It improves the evidence record while preserving every safety boundary.",
            "evidence_ids": ["evidence_project"],
        },
        "open_questions": [{
            "question_id": "question_1",
            "text": "Does the source project have a newer exact revision?",
            "evidence_ids": ["evidence_project"],
        }],
    }


def _base_payload(
    *,
    profile_version: str = DECISION_PROFILE_VERSION,
) -> dict[str, object]:
    workflow_kind = {
        DECISION_PROFILE_VERSION: "decision",
        RESEARCH_REPORT_PROFILE_VERSION: "research",
        ARTIFACT_DRAFT_PROFILE_VERSION: "artifact_authoring",
    }[profile_version]
    return {
        "invocation_binding": {
            "client_request_id": "request_1",
            "request_sha256": SHA_REQUEST,
            "room_id": "room_1",
            "caller_id": "caller_bazi",
            "project_id": "project_bazi",
            "source_item_id": "source_item_1",
            "source_revision": "revision_4",
        },
        "studio_binding": {
            "round_id": "round_1",
            "artifact_id": "",
            "artifact_version": 0,
            "artifact_snapshot_sha256": "",
            "manual_chatgpt_session_id": "",
            "manual_chatgpt_result_sha256": "",
            "decision_card_sha256": "",
        },
        "workflow_kind": workflow_kind,
        "result_profile": profile_version,
        "domain_context": {
            "schema_version": "fixture_domain_context_v1",
            "schema_sha256": SHA_DOMAIN_SCHEMA,
            "payload_sha256": SHA_DOMAIN_PAYLOAD,
        },
        "source_manifest": _base_sources(),
        "evidence_manifest": _base_evidence(),
        "profile": _decision_profile(),
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


def _research_payload() -> dict[str, object]:
    payload = _base_payload(profile_version=RESEARCH_REPORT_PROFILE_VERSION)
    payload["source_manifest"].append({
        "source_id": "source_engine",
        "source_kind": "deterministic_engine_receipt",
        "record_id": "engine_receipt_1",
        "record_revision": "engine_v3",
        "record_sha256": SHA_ENGINE,
        "provenance": "deterministic_engine",
        "trust_state": "deterministic_contract_verified",
    })
    payload["evidence_manifest"].append({
        "evidence_id": "evidence_engine",
        "source_id": "source_engine",
        "evidence_role": "support",
        "verification_status": "source_checked",
        "review_note": "The exact deterministic receipt was verified.",
    })
    payload["profile"] = {
        "version": RESEARCH_REPORT_PROFILE_VERSION,
        "title": "Bounded cross-domain research report",
        "scope": {
            "subject": "One deterministic fact and its advisory interpretation.",
            "data_cutoff_utc": "2026-08-26T10:00:00Z",
        },
        "summary": {
            "text": "The deterministic fact is separated from model interpretation.",
            "evidence_ids": ["evidence_engine", "evidence_project"],
        },
        "findings": [{
            "claim_id": "claim_engine",
            "statement": "The deterministic engine produced the sealed fixture value.",
            "claim_kind": "deterministic_fact",
            "support_state": "supported",
            "evidence_ids": ["evidence_engine"],
            "uncertainty": "This proves the engine output, not external domain truth.",
        }],
        "counterpoints": [{
            "claim_id": "claim_interpretation",
            "statement": "The meaning of that value remains interpretive.",
            "claim_kind": "interpretation",
            "support_state": "mixed",
            "evidence_ids": ["evidence_project"],
            "uncertainty": "Expert interpretation has not been established.",
        }],
        "limitations": [{
            "limitation_id": "limitation_1",
            "text": "No external expert validation was performed.",
            "evidence_ids": [],
        }],
        "open_questions": [],
        "conclusion": {
            "state": "supported",
            "text": "Only the deterministic engine output is established by this result.",
            "evidence_ids": ["evidence_engine"],
        },
    }
    return payload


def _artifact_payload() -> dict[str, object]:
    payload = _base_payload(profile_version=ARTIFACT_DRAFT_PROFILE_VERSION)
    payload["profile"] = {
        "version": ARTIFACT_DRAFT_PROFILE_VERSION,
        "artifact_kind": "presentation",
        "title": "Evidence-bound presentation draft",
        "audience": "The source project's human reviewer.",
        "purpose": "Prepare a reviewable outline without writing an output file.",
        "sections": [
            {
                "section_id": "slide_1",
                "ordinal": 1,
                "title": "Scope",
                "purpose": "State the bounded question.",
                "body": "This draft is advisory and has no execution authority.",
                "bullets": [],
                "speaker_notes": "Confirm the evidence cutoff before export.",
                "evidence_ids": ["evidence_project"],
            },
            {
                "section_id": "slide_2",
                "ordinal": 2,
                "title": "Evidence",
                "purpose": "Show the source binding.",
                "body": "",
                "bullets": ["Project source is SHA-256 bound."],
                "speaker_notes": "",
                "evidence_ids": ["evidence_project"],
            },
        ],
        "asset_briefs": [{
            "asset_id": "asset_1",
            "asset_kind": "diagram",
            "description": "A simple source-to-result provenance diagram.",
            "section_id": "slide_2",
            "evidence_ids": ["evidence_project"],
        }],
        "export_plan": {
            "target_format": "pptx",
            "suggested_filename": "bounded-research.pptx",
            "renderer_id": "host_pptx_renderer",
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
    return payload


class CollaborationResultTests(unittest.TestCase):
    def test_all_three_closed_profiles_build_and_verify(self) -> None:
        payloads = [
            _base_payload(),
            _research_payload(),
            _artifact_payload(),
        ]
        for payload in payloads:
            with self.subTest(profile=payload["result_profile"]):
                original = copy.deepcopy(payload)
                result = build_collaboration_result(payload)
                verified = verify_collaboration_result(result)

                self.assertEqual(payload, original)
                self.assertEqual(verified, result)
                self.assertEqual(result["schema_sha256"], COLLABORATION_RESULT_SCHEMA_SHA256)
                self.assertEqual(
                    result["profile_schema_sha256"],
                    COLLABORATION_PROFILE_SCHEMA_SHA256[payload["result_profile"]],
                )
                self.assertEqual(result["profile_sha256"], canonical_sha256(result["profile"]))
                self.assertEqual(result["safety"], FIXED_RESULT_SAFETY)
                self.assertTrue(result["result_id"].startswith("result_"))
                sealed = copy.deepcopy(result)
                stored_hash = sealed.pop("result_sha256")
                self.assertEqual(stored_hash, canonical_sha256(sealed))

    def test_set_like_order_is_canonical_but_authored_option_order_is_semantic(self) -> None:
        left = _base_payload()
        right = copy.deepcopy(left)
        right["source_manifest"].reverse()
        right["evidence_manifest"].reverse()
        right["profile"]["options"][0]["evidence_ids"].reverse()

        left_result = build_collaboration_result(left)
        right_result = build_collaboration_result(right)
        self.assertEqual(left_result, right_result)

        reordered_options = copy.deepcopy(left)
        reordered_options["profile"]["options"].reverse()
        reordered_result = build_collaboration_result(reordered_options)
        self.assertNotEqual(left_result["profile_sha256"], reordered_result["profile_sha256"])
        self.assertNotEqual(left_result["result_sha256"], reordered_result["result_sha256"])

    def test_unknown_fields_fixed_safety_and_hash_tampering_fail_closed(self) -> None:
        payload = _base_payload()
        payload["profile"]["order_id"] = "forbidden"
        with self.assertRaises(CollaborationResultError):
            build_collaboration_result(payload)

        result = build_collaboration_result(_base_payload())
        tampered_profile = copy.deepcopy(result)
        tampered_profile["profile"]["summary"]["text"] = "Changed after sealing."
        with self.assertRaises(CollaborationResultError):
            verify_collaboration_result(tampered_profile)

        tampered_safety = copy.deepcopy(result)
        tampered_safety["safety"]["live_trading_allowed"] = True
        with self.assertRaises(CollaborationResultError):
            verify_collaboration_result(tampered_safety)

        aliased_safety = copy.deepcopy(result)
        aliased_safety["safety"]["advisory_only"] = 1
        with self.assertRaises(CollaborationResultError):
            verify_collaboration_result(aliased_safety)

        unknown_root = copy.deepcopy(result)
        unknown_root["extra"] = True
        with self.assertRaises(CollaborationResultError):
            verify_collaboration_result(unknown_root)

    def test_invocation_projection_and_expected_envelope_binding(self) -> None:
        envelope = {
            "version": "project_invocation_envelope_v1",
            "client_request_id": "request_1",
            "request_sha256": SHA_REQUEST,
            "room_id": "room_1",
            "caller_id": "caller_bazi",
            "project_id": "project_bazi",
            "result_profile": DECISION_PROFILE_VERSION,
            "source": {
                "item_id": "source_item_1",
                "revision": "revision_4",
                "content_sha256": SHA_PROJECT,
            },
        }
        binding = invocation_binding_from_envelope(envelope)
        self.assertEqual(binding, _base_payload()["invocation_binding"])
        result = build_collaboration_result(_base_payload())
        self.assertEqual(
            verify_collaboration_result(result, expected_envelope=envelope),
            result,
        )

        changed = copy.deepcopy(envelope)
        changed["source"]["revision"] = "revision_5"
        with self.assertRaises(CollaborationResultError):
            verify_collaboration_result(result, expected_envelope=changed)

        changed_hash = copy.deepcopy(envelope)
        changed_hash["source"]["content_sha256"] = "f" * 64
        with self.assertRaisesRegex(CollaborationResultError, "content hash"):
            verify_collaboration_result(result, expected_envelope=changed_hash)

    def test_research_deterministic_fact_requires_verified_engine_receipt(self) -> None:
        valid = _research_payload()
        result = build_collaboration_result(valid)
        self.assertEqual(result["result_profile"], RESEARCH_REPORT_PROFILE_VERSION)

        invalid = _research_payload()
        invalid["profile"]["findings"][0]["evidence_ids"] = ["evidence_project"]
        with self.assertRaisesRegex(
            CollaborationResultError,
            "deterministic_fact requires",
        ):
            build_collaboration_result(invalid)

    def test_review_and_user_decision_boundaries_are_independent_and_fail_closed(self) -> None:
        payload = _base_payload()
        payload["source_manifest"].extend([
            {
                "source_id": "source_review",
                "source_kind": "api_review_bundle",
                "record_id": "review_bundle_1",
                "record_revision": "1",
                "record_sha256": SHA_REVIEW,
                "provenance": "provider_review",
                "trust_state": "provider_output_advisory",
            },
            {
                "source_id": "source_user_decision",
                "source_kind": "user_decision_record",
                "record_id": "user_decision_1",
                "record_revision": "artifact_user_decision_v2",
                "record_sha256": SHA_DECISION,
                "provenance": "studio_sealed",
                "trust_state": "host_verified_binding",
            },
        ])
        payload["independent_review"] = {
            "status": "passed",
            "source_ids": ["source_review"],
            "findings": [],
            "open_questions": [],
            "review_bundle_sha256": canonical_sha256([SHA_REVIEW]),
        }
        payload["user_boundary"] = {
            "status": "recorded",
            "outcome": "accepted",
            "record_id": "user_decision_1",
            "record_version": "artifact_user_decision_v2",
            "record_sha256": SHA_DECISION,
            "selected_item_id": "option_b",
        }
        accepted = build_collaboration_result(payload)
        self.assertEqual(accepted["user_boundary"]["selected_item_id"], "option_b")

        blocked = copy.deepcopy(payload)
        blocked["independent_review"] = {
            "status": "blocked",
            "source_ids": ["source_review"],
            "findings": [{
                "finding_id": "finding_1",
                "severity": "blocking",
                "statement": "The evidence cutoff is stale.",
                "rationale": "The recommendation cannot be adopted against a changed source.",
                "evidence_ids": ["evidence_project"],
            }],
            "open_questions": [],
            "review_bundle_sha256": canonical_sha256([SHA_REVIEW]),
        }
        with self.assertRaisesRegex(CollaborationResultError, "cannot be accepted"):
            build_collaboration_result(blocked)

        invalid_selection = copy.deepcopy(payload)
        invalid_selection["user_boundary"]["selected_item_id"] = "missing_option"
        with self.assertRaisesRegex(CollaborationResultError, "sealed decision option"):
            build_collaboration_result(invalid_selection)

    def test_artifact_plan_requires_path_free_filename_and_verified_export(self) -> None:
        payload = _artifact_payload()
        payload["profile"]["export_plan"]["suggested_filename"] = "..\\output.pptx"
        with self.assertRaisesRegex(CollaborationResultError, "path-free basename"):
            build_collaboration_result(payload)

        payload = _artifact_payload()
        payload["profile"]["sections"][1]["ordinal"] = 3
        with self.assertRaisesRegex(CollaborationResultError, "contiguous"):
            build_collaboration_result(payload)

        payload = _artifact_payload()
        payload["source_manifest"].extend([
            {
                "source_id": "source_render_package",
                "source_kind": "artifact_render_package",
                "record_id": "render_package_1",
                "record_revision": "1",
                "record_sha256": "a" * 64,
                "provenance": "document_pipeline",
                "trust_state": "host_verified_binding",
            },
            {
                "source_id": "source_render_verification",
                "source_kind": "render_verification_receipt",
                "record_id": "render_verification_1",
                "record_revision": "1",
                "record_sha256": "b" * 64,
                "provenance": "document_pipeline",
                "trust_state": "host_verified_binding",
            },
            {
                "source_id": "source_export_receipt",
                "source_kind": "artifact_export_receipt",
                "record_id": "export_receipt_1",
                "record_revision": "1",
                "record_sha256": "c" * 64,
                "provenance": "document_pipeline",
                "trust_state": "host_verified_binding",
            },
        ])
        delivery = payload["profile"]["delivery"]
        delivery.update({
            "render_state": "rendered",
            "render_package_sha256": "a" * 64,
            "verification_state": "verified",
            "verification_receipt_sha256": "b" * 64,
            "export_state": "exported",
            "export_receipt_sha256": "c" * 64,
        })
        exported = build_collaboration_result(payload)
        self.assertEqual(exported["profile"]["delivery"]["export_state"], "exported")

        unverified = _artifact_payload()
        unverified["profile"]["delivery"].update({
            "render_state": "rendered",
            "render_package_sha256": "a" * 64,
            "export_state": "exported",
            "export_receipt_sha256": "c" * 64,
        })
        with self.assertRaisesRegex(CollaborationResultError, "verified render"):
            build_collaboration_result(unverified)

        overwrite = _artifact_payload()
        overwrite["profile"]["export_plan"]["overwrite_allowed"] = True
        with self.assertRaises(CollaborationResultError):
            build_collaboration_result(overwrite)


if __name__ == "__main__":
    unittest.main()
