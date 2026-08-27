from __future__ import annotations

import copy
import json
import unittest

from backend.collaboration_result import (
    ARTIFACT_DRAFT_PROFILE_VERSION,
    DECISION_PROFILE_VERSION,
    FIXED_RESULT_SAFETY,
    RESEARCH_REPORT_PROFILE_VERSION,
    CollaborationResultError,
    verify_collaboration_result,
)
from backend.decision_lineage import canonical_sha256
from backend.project_integration_service import (
    ARTIFACT_EVIDENCE_ID,
    DOMAIN_CONTEXT_EVIDENCE_ID,
    PROJECT_EVIDENCE_ID,
    ProjectIntegrationError,
    ProjectIntegrationService,
    project_collaboration_result,
)
from backend.project_invocation import (
    PROJECT_INVOCATION_ENVELOPE_VERSION,
    derive_project_invocation_room_id,
    project_invocation_semantics,
    seal_project_invocation_envelope,
)


def _unsealed_envelope(
    *,
    workflow_kind: str = "research",
    result_profile: str = RESEARCH_REPORT_PROFILE_VERSION,
    domain: str = "project_research",
    category: str = "Project research",
    template_id: str = "project_research",
    content_sha256: str = "3" * 64,
    title: str = "Cross-project collaboration",
    objective: str = "Produce one bounded, provenance-first result.",
) -> dict[str, object]:
    caller_id = "caller_alpha"
    project_id = "project_multi_domain"
    client_request_id = "request_00000001"
    return {
        "version": PROJECT_INVOCATION_ENVELOPE_VERSION,
        "caller_id": caller_id,
        "project_id": project_id,
        "client_request_id": client_request_id,
        "room_id": derive_project_invocation_room_id(
            caller_id,
            project_id,
            client_request_id,
        ),
        "source": {
            "item_id": "source_item_001",
            "revision": "revision-7",
        },
        "workflow_kind": workflow_kind,
        "result_profile": result_profile,
        "room_spec": {
            "title": title,
            "objective": objective,
            "domain": domain,
            "category": category,
            "template_id": template_id,
            "capability_pack_ids": [f"{domain}_readonly"],
        },
        "domain_context": {
            "schema_version": f"{domain}_context_v1",
            "schema_sha256": "1" * 64,
            "payload_sha256": "2" * 64,
        },
        "input_manifest": {
            "content_sha256": content_sha256,
            "content_bytes": 4_096,
        },
        "data_handling": {
            "classification": "internal",
            "retention_policy": "project_default",
            "retention_days": None,
        },
        "budget": {
            "max_provider_calls": 0,
            "max_context_bytes": 100_000,
            "max_result_bytes": 200_000,
        },
        "user_confirmation": {
            "required": True,
            "boundary": "before_room_creation",
        },
        "safety": {
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
        },
    }


def _sealed_envelope(**overrides: object) -> dict[str, object]:
    return seal_project_invocation_envelope(_unsealed_envelope(**overrides))


class ProjectIntegrationServiceTests(unittest.TestCase):
    def test_all_profiles_have_valid_pending_or_withheld_results(self) -> None:
        cases = [
            (
                "decision",
                DECISION_PROFILE_VERSION,
                "trading_research",
                "Trading research",
                "trading_decision",
            ),
            (
                "research",
                RESEARCH_REPORT_PROFILE_VERSION,
                "football_research",
                "Football research",
                "football_report",
            ),
            (
                "artifact_authoring",
                ARTIFACT_DRAFT_PROFILE_VERSION,
                "presentation",
                "PPT presentation",
                "presentation_deck",
            ),
        ]
        for workflow, profile, domain, category, template_id in cases:
            with self.subTest(profile=profile):
                envelope = _sealed_envelope(
                    workflow_kind=workflow,
                    result_profile=profile,
                    domain=domain,
                    category=category,
                    template_id=template_id,
                )
                original = copy.deepcopy(envelope)
                result = project_collaboration_result(envelope)
                expected = project_invocation_semantics(envelope)

                self.assertEqual(envelope, original)
                self.assertEqual(
                    verify_collaboration_result(result, expected_envelope=expected),
                    result,
                )
                self.assertEqual(result["result_profile"], profile)
                self.assertEqual(result["safety"], FIXED_RESULT_SAFETY)
                self.assertEqual(result["safety"]["execution_capability"], "none")
                self.assertFalse(result["safety"]["live_trading_allowed"])
                self.assertFalse(result["safety"]["betting_allowed"])
                self.assertFalse(result["safety"]["external_write_authorized"])
                self.assertEqual(result["user_boundary"]["status"], "pending")
                self.assertEqual(result["user_boundary"]["outcome"], "unresolved")
                self.assertEqual(result["independent_review"]["status"], "not_run")

                if profile == DECISION_PROFILE_VERSION:
                    self.assertEqual(
                        result["profile"]["recommendation"]["state"],
                        "withheld",
                    )
                elif profile == RESEARCH_REPORT_PROFILE_VERSION:
                    self.assertEqual(result["profile"]["findings"], [])
                    self.assertEqual(result["profile"]["conclusion"]["state"], "withheld")
                else:
                    self.assertEqual(result["profile"]["artifact_kind"], "presentation")
                    self.assertEqual(result["profile"]["export_plan"]["target_format"], "pptx")
                    self.assertEqual(result["profile"]["delivery"]["render_state"], "not_rendered")
                    self.assertEqual(result["profile"]["delivery"]["verification_state"], "not_run")
                    self.assertEqual(result["profile"]["delivery"]["export_state"], "not_exported")

    def test_full_envelope_and_intake_projection_are_deterministically_equivalent(self) -> None:
        envelope = _sealed_envelope()
        intake = project_invocation_semantics(envelope)

        from_complete_envelope = project_collaboration_result(envelope)
        from_complete_with_crosscheck = project_collaboration_result(
            envelope,
            intake_projection=intake,
        )
        from_intake = project_collaboration_result(intake)
        from_service = ProjectIntegrationService.project_result(envelope)

        self.assertEqual(from_complete_envelope, from_complete_with_crosscheck)
        self.assertEqual(from_complete_envelope, from_intake)
        self.assertEqual(from_complete_envelope, from_service)
        self.assertEqual(
            verify_collaboration_result(
                from_complete_envelope,
                expected_envelope=envelope,
            ),
            from_complete_envelope,
        )
        project_source = next(
            source
            for source in from_complete_envelope["source_manifest"]
            if source["source_kind"] == "project_source"
        )
        self.assertEqual(
            project_source["record_sha256"],
            envelope["input_manifest"]["content_sha256"],
        )
        self.assertEqual(
            from_complete_envelope["invocation_binding"]["request_sha256"],
            envelope["request_sha256"],
        )

    def test_request_and_source_hashes_are_bound_and_mismatch_fails_closed(self) -> None:
        first = _sealed_envelope(content_sha256="3" * 64)
        second = _sealed_envelope(content_sha256="4" * 64)
        first_result = project_collaboration_result(first)
        second_result = project_collaboration_result(second)

        self.assertNotEqual(first["request_sha256"], second["request_sha256"])
        self.assertNotEqual(first_result["result_sha256"], second_result["result_sha256"])
        self.assertNotEqual(
            first_result["invocation_binding"]["request_sha256"],
            second_result["invocation_binding"]["request_sha256"],
        )
        first_project_source = next(
            source
            for source in first_result["source_manifest"]
            if source["source_kind"] == "project_source"
        )
        second_project_source = next(
            source
            for source in second_result["source_manifest"]
            if source["source_kind"] == "project_source"
        )
        self.assertNotEqual(
            first_project_source["record_sha256"],
            second_project_source["record_sha256"],
        )
        with self.assertRaises(CollaborationResultError):
            verify_collaboration_result(
                first_result,
                expected_envelope=project_invocation_semantics(second),
            )

        mismatched_projection = project_invocation_semantics(first)
        mismatched_projection["source"]["content_sha256"] = "5" * 64
        mismatched_projection["input_manifest"]["content_sha256"] = "5" * 64
        with self.assertRaises(ProjectIntegrationError) as mismatch:
            project_collaboration_result(
                first,
                intake_projection=mismatched_projection,
            )
        self.assertEqual(mismatch.exception.code, "PROJECT_INTEGRATION_INTAKE_MISMATCH")

    def test_full_invocation_identifier_range_is_representable_without_aliasing(self) -> None:
        raw = _unsealed_envelope()
        raw["project_id"] = "p" * 160
        raw["client_request_id"] = "r" * 160
        raw["source"]["item_id"] = "s" * 160
        raw["room_id"] = derive_project_invocation_room_id(
            raw["caller_id"],
            raw["project_id"],
            raw["client_request_id"],
        )
        envelope = seal_project_invocation_envelope(raw)

        result = project_collaboration_result(envelope)

        self.assertEqual(
            result["invocation_binding"]["project_id"],
            raw["project_id"],
        )
        self.assertEqual(
            result["invocation_binding"]["client_request_id"],
            raw["client_request_id"],
        )
        self.assertEqual(
            result["invocation_binding"]["source_item_id"],
            raw["source"]["item_id"],
        )
        self.assertEqual(
            verify_collaboration_result(
                result,
                expected_envelope=project_invocation_semantics(envelope),
            ),
            result,
        )

    def test_only_hash_verified_optional_sources_are_bound_and_secrets_do_not_leak(self) -> None:
        secret = "TOP-SECRET-credential-should-never-appear"
        envelope = _sealed_envelope(
            title=f"Private room {secret}",
            objective=f"Do not echo token {secret}.",
        )
        room_id = envelope["room_id"]

        room_snapshot = {
            "id": room_id,
            "settings_version": 3,
            "title": secret,
            "api_key": secret,
        }
        room_sha256 = canonical_sha256(room_snapshot)
        studio_snapshot = {
            "room_id": room_id,
            "version": 3,
            "snapshot": room_snapshot,
            "snapshot_sha256": room_sha256,
            "stored_snapshot_sha256": room_sha256,
            "integrity_ok": True,
            "snapshot_storage_integrity_ok": True,
            "token": secret,
        }

        artifact_snapshot = {
            "id": "artifact_1",
            "room_id": room_id,
            "round_id": "",
            "version": 2,
            "title": secret,
            "content": {
                "summary": "Safe sealed artifact without a result profile.",
                "password": secret,
            },
        }
        artifact_sha256 = canonical_sha256(artifact_snapshot)
        artifact = {
            "artifact_id": "artifact_1",
            "room_id": room_id,
            "version": 2,
            "snapshot": artifact_snapshot,
            "snapshot_sha256": artifact_sha256,
            "stored_snapshot_sha256": artifact_sha256,
            "integrity_ok": True,
            "snapshot_storage_integrity_ok": True,
            "account_id": secret,
        }

        manual_result = {
            "version": "manual_chatgpt_result_v1",
            "summary": "Advisory manual import.",
            "api_key": secret,
        }
        manual_result_sha256 = canonical_sha256(manual_result)
        decision_card = {
            "version": "manual_chatgpt_decision_card_v1",
            "session_id": "manual_session_1",
            "room_id": room_id,
            "result_sha256": manual_result_sha256,
            "summary": "No user decision.",
            "order_id": secret,
        }
        decision_card_sha256 = canonical_sha256(decision_card)
        manual_session = {
            "id": "manual_session_1",
            "room_id": room_id,
            "result": manual_result,
            "result_sha256": manual_result_sha256,
            "decision_card": decision_card,
            "decision_card_sha256": decision_card_sha256,
            "integrity": {"ok": True, "token": secret},
            "credential": secret,
        }

        result = project_collaboration_result(
            envelope,
            studio_snapshot=studio_snapshot,
            artifact=artifact,
            manual_session=manual_session,
        )
        self.assertEqual(
            verify_collaboration_result(
                result,
                expected_envelope=project_invocation_semantics(envelope),
            ),
            result,
        )
        self.assertEqual(result["studio_binding"]["artifact_id"], "artifact_1")
        self.assertEqual(result["studio_binding"]["artifact_version"], 2)
        self.assertEqual(
            result["studio_binding"]["manual_chatgpt_session_id"],
            "manual_session_1",
        )
        self.assertEqual(
            result["studio_binding"]["decision_card_sha256"],
            decision_card_sha256,
        )
        self.assertIn(ARTIFACT_EVIDENCE_ID, {
            item["evidence_id"] for item in result["evidence_manifest"]
        })
        serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
        self.assertNotIn(secret, serialized)
        for sensitive_name in (
            "api_key",
            "token",
            "password",
            "credential",
            "account_id",
            "order_id",
        ):
            self.assertNotIn(f'"{sensitive_name}"', serialized)

    def test_sealed_profile_is_used_and_invalid_optional_profile_is_rejected(self) -> None:
        envelope = _sealed_envelope(
            workflow_kind="decision",
            result_profile=DECISION_PROFILE_VERSION,
            domain="project_decision",
            category="Project decision",
            template_id="project_decision",
        )
        baseline = project_collaboration_result(envelope)
        profile = copy.deepcopy(baseline["profile"])
        profile["summary"]["text"] = "A sealed artifact supplied this collaboration profile."

        def artifact_with(candidate: dict[str, object]) -> dict[str, object]:
            snapshot = {
                "id": "artifact_profile_1",
                "room_id": envelope["room_id"],
                "round_id": "",
                "version": 1,
                "title": "Profile carrier",
                "content": {"collaboration_profile": candidate},
            }
            snapshot_sha256 = canonical_sha256(snapshot)
            return {
                "artifact_id": "artifact_profile_1",
                "room_id": envelope["room_id"],
                "version": 1,
                "snapshot": snapshot,
                "snapshot_sha256": snapshot_sha256,
                "stored_snapshot_sha256": snapshot_sha256,
                "integrity_ok": True,
                "snapshot_storage_integrity_ok": True,
            }

        projected = project_collaboration_result(
            envelope,
            artifact=artifact_with(profile),
        )
        self.assertEqual(
            projected["profile"]["summary"]["text"],
            "A sealed artifact supplied this collaboration profile.",
        )
        self.assertEqual(
            projected["profile"]["summary"]["evidence_ids"],
            [DOMAIN_CONTEXT_EVIDENCE_ID, PROJECT_EVIDENCE_ID],
        )

        invalid_profile = copy.deepcopy(profile)
        invalid_profile["api_key"] = "must-not-leak"
        with self.assertRaises(ProjectIntegrationError) as raised:
            project_collaboration_result(
                envelope,
                artifact=artifact_with(invalid_profile),
            )
        self.assertEqual(
            raised.exception.code,
            "PROJECT_INTEGRATION_PROFILE_REJECTED",
        )
        self.assertNotIn("must-not-leak", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
