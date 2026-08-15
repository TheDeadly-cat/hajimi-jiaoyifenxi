from __future__ import annotations

import copy
import unittest

from backend.artifact_governance import (
    ARTIFACT_GOVERNANCE_ATTESTATION_VERSION,
    ARTIFACT_GOVERNANCE_VERSION,
    CAN_AUTONOMOUSLY_DECIDE,
    EXECUTION_CAPABILITY,
    LIVE_TRADING_ALLOWED,
    RISK_DISPOSITIONS_ARE_USER_DECISIONS,
    build_governance_attestation,
    build_governance_snapshot,
    canonical_governance_sha256,
    governance_blocking_issue_codes,
    verify_governance_attestation,
)


def _lineage(candidate_id: str, revision: int = 1) -> dict:
    return {
        "version": "candidate_lineage_v1",
        "origin_message_id": f"message_{candidate_id}_origin",
        "latest_message_id": f"message_{candidate_id}_latest",
        "revision": revision,
    }


def _candidate(candidate_id: str, *, evidence_status: str) -> dict:
    return {
        "id": candidate_id,
        "title": f"Candidate {candidate_id}",
        "description": f"Description {candidate_id}",
        "benefits": [f"Benefit {candidate_id}"],
        "risks": [f"Risk {candidate_id}"],
        "value": f"Value {candidate_id}",
        "cost": f"Cost {candidate_id}",
        "timeline": "30 days",
        "dependencies": [f"Dependency {candidate_id}"],
        "reversibility": "high",
        "evidence": [{
            "type": "message",
            "id": f"evidence_{candidate_id}",
            "verification_status": evidence_status,
            "review_note": f"Audit metadata for {candidate_id}",
        }],
        "lineage": _lineage(candidate_id),
    }


def _decision(*, evidence_status: str) -> dict:
    return {
        "status": "candidate",
        "options": [
            _candidate("candidate_a", evidence_status=evidence_status),
            _candidate("candidate_b", evidence_status=evidence_status),
        ],
        "preferred_option_id": "candidate_a",
        "rationale": "Candidate A has the strongest bounded trade-off.",
        "evidence": [{
            "type": "message",
            "id": "decision_evidence",
            "verification_status": evidence_status,
            "review_note": "Evidence audit metadata must not affect alignment.",
        }],
    }


def _artifact(*, round_id: str = "round_1", status: str = "CONFIRMED") -> dict:
    return {
        "id": "artifact_1",
        "room_id": "room_1",
        "round_id": round_id,
        "title": "Governed research artifact",
        "status": status,
        "version": 3,
        "content": {
            "summary": "A frozen research summary.",
            "decision": _decision(evidence_status="corroborated"),
        },
    }


def _review(candidate_id: str, action: str) -> dict:
    candidate_snapshot = {
        "title": f"Candidate {candidate_id}",
        "symbol": "US.MU",
        "direction": "UP",
        "horizon_days": 30,
        "thesis": f"Thesis {candidate_id}",
        "invalidation": f"Invalidation {candidate_id}",
    }
    return {
        "candidate_id": candidate_id,
        "candidate_revision": 1,
        "candidate_latest_message_id": f"message_{candidate_id}_latest",
        "candidate_snapshot_sha256": canonical_governance_sha256(
            candidate_snapshot
        ),
        "candidate_snapshot": candidate_snapshot,
        "action": action,
        "disposition_only": True,
        "review_message_id": f"review_{candidate_id}",
        "reviewer_member_id": "risk_member",
        "reviewer_member_version": 7,
        "status": "current",
        "current_candidate_revision": 1,
        "risk_ids": [f"risk_{candidate_id}"],
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
    }


def _projection() -> dict:
    return {
        "version": "turn_contract_v1",
        "candidate_lineage": {
            "version": "candidate_lineage_v1",
            "applicable": True,
            "ready": True,
            "status": "ready",
            "decision_message_id": "decision_message",
            "referenced_candidate_ids": ["candidate_a", "candidate_b"],
            "candidates": [
                {"id": "candidate_a", **{
                    key: value for key, value in _lineage("candidate_a").items()
                    if key != "version"
                }},
                {"id": "candidate_b", **{
                    key: value for key, value in _lineage("candidate_b").items()
                    if key != "version"
                }},
            ],
            "issues": [],
        },
        "candidate_risk_reviews": {
            "version": "candidate_risk_review_v1",
            "applicable": True,
            "ready": True,
            "status": "ready",
            "decision_message_id": "decision_message",
            "target_candidate_ids": ["candidate_a", "candidate_b"],
            "reviews": [
                _review("candidate_a", "support"),
                _review("candidate_b", "reject"),
            ],
            "issues": [],
            "review_actions_are_dispositions_only": True,
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
        },
        # The evidence identity is the same as the artifact, while mutable
        # evidence-review metadata deliberately differs.
        "decision": _decision(evidence_status="unreviewed"),
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
    }


def _round_metadata(*, applicable: bool = True) -> dict:
    return {
        "applicable": applicable,
        "status": "ready" if applicable else "legacy_unavailable",
        "round_id": "round_1",
        "round_status": "COMPLETED",
        "turn_contract_version": "turn_contract_v1" if applicable else None,
        "candidate_risk_review_version": (
            "candidate_risk_review_v1" if applicable else None
        ),
        "candidate_risk_review_required": applicable,
        "bundle_valid": True,
        "bundle_issues": [],
        "projection_issues": [],
        "round_governance_input_sha256": "a" * 64 if applicable else "",
    }


def _snapshot(
    *,
    artifact: dict | None = None,
    projection: dict | None = None,
    attestation: dict | None = None,
    user_decisions: list[dict] | None = None,
) -> dict:
    return build_governance_snapshot(
        artifact or _artifact(),
        round_metadata=_round_metadata(),
        bundle_projection=projection or _projection(),
        user_decisions=user_decisions or [],
        attestation=attestation,
    )


class ArtifactGovernanceTests(unittest.TestCase):
    def test_matching_projection_is_ready_and_ignores_evidence_audit_metadata(self) -> None:
        snapshot = _snapshot()

        self.assertEqual(snapshot["version"], ARTIFACT_GOVERNANCE_VERSION)
        self.assertTrue(snapshot["applicable"])
        self.assertTrue(snapshot["ready"])
        self.assertTrue(snapshot["artifact_alignment"]["ready"])
        self.assertEqual(snapshot["issues"], [])

        hash_payload = copy.deepcopy(snapshot)
        expected_hash = hash_payload.pop("snapshot_sha256")
        self.assertEqual(canonical_governance_sha256(hash_payload), expected_hash)

    def test_strict_decision_and_candidate_alignment(self) -> None:
        cases = (
            (
                "status",
                lambda artifact: artifact["content"]["decision"].update(
                    {"status": "deferred"}
                ),
                "ARTIFACT_GOVERNANCE_DECISION_STATUS_MISMATCH",
            ),
            (
                "candidate_set",
                lambda artifact: artifact["content"]["decision"]["options"].pop(),
                "ARTIFACT_GOVERNANCE_CANDIDATE_SET_MISMATCH",
            ),
            (
                "preferred",
                lambda artifact: artifact["content"]["decision"].update(
                    {"preferred_option_id": "candidate_b"}
                ),
                "ARTIFACT_GOVERNANCE_PREFERRED_OPTION_MISMATCH",
            ),
            (
                "candidate_core",
                lambda artifact: artifact["content"]["decision"]["options"][0].update(
                    {"description": "Silently rewritten candidate"}
                ),
                "ARTIFACT_GOVERNANCE_CANDIDATE_FIELDS_MISMATCH",
            ),
            (
                "decision_evidence_identity",
                lambda artifact: artifact["content"]["decision"]["evidence"][0].update(
                    {"id": "different_evidence"}
                ),
                "ARTIFACT_GOVERNANCE_DECISION_EVIDENCE_MISMATCH",
            ),
        )
        for label, mutation, expected_code in cases:
            with self.subTest(label=label):
                artifact = _artifact()
                mutation(artifact)
                snapshot = _snapshot(artifact=artifact)
                self.assertFalse(snapshot["ready"])
                self.assertIn(
                    expected_code,
                    governance_blocking_issue_codes(snapshot),
                )

    def test_projection_candidate_lineage_must_match_projection_index(self) -> None:
        projection = _projection()
        projection["decision"]["options"][0]["lineage"]["revision"] = 2

        snapshot = _snapshot(projection=projection)

        self.assertFalse(snapshot["ready"])
        self.assertIn(
            "PROJECTED_CANDIDATE_LINEAGE_MISMATCH",
            governance_blocking_issue_codes(snapshot),
        )

    def test_attestation_build_verify_and_confirmed_snapshot(self) -> None:
        unattested = _snapshot()
        attestation = build_governance_attestation(unattested)

        self.assertEqual(
            attestation["attestation_version"],
            ARTIFACT_GOVERNANCE_ATTESTATION_VERSION,
        )
        self.assertEqual(attestation["round_governance_input_sha256"], "a" * 64)
        self.assertEqual(attestation["projection_sha256"], unattested["projection_sha256"])
        self.assertEqual(
            attestation["artifact_binding_sha256"],
            unattested["artifact_binding_sha256"],
        )
        self.assertTrue(verify_governance_attestation(unattested, attestation)["valid"])

        verified = _snapshot(attestation=attestation)
        self.assertEqual(verified["status"], "ready")
        self.assertTrue(verified["integrity_ok"])
        self.assertTrue(verified["attestation_integrity_ok"])
        self.assertEqual(governance_blocking_issue_codes(verified), ())

    def test_attestation_rejects_tampering_even_if_outer_hash_is_recomputed(self) -> None:
        snapshot = _snapshot()
        attestation = build_governance_attestation(snapshot)

        for field, replacement in (
            ("round_governance_input_sha256", "b" * 64),
            ("projection_sha256", "c" * 64),
            ("artifact_binding_sha256", "d" * 64),
            ("governance_snapshot_sha256", "e" * 64),
        ):
            with self.subTest(field=field):
                tampered = copy.deepcopy(attestation)
                tampered[field] = replacement
                tampered.pop("attestation_sha256")
                tampered["attestation_sha256"] = canonical_governance_sha256(tampered)
                verification = verify_governance_attestation(snapshot, tampered)
                self.assertFalse(verification["valid"])
                self.assertIn(
                    "GOVERNANCE_ATTESTATION_BINDING_MISMATCH",
                    verification["issues"],
                )

    def test_attestation_rejects_snapshot_projection_tamper(self) -> None:
        snapshot = _snapshot()
        attestation = build_governance_attestation(snapshot)
        tampered_snapshot = copy.deepcopy(snapshot)
        tampered_snapshot["projection"]["decision"]["preferred_option_id"] = (
            "candidate_b"
        )

        verification = verify_governance_attestation(
            tampered_snapshot,
            attestation,
        )

        self.assertFalse(verification["valid"])
        self.assertIn(
            "GOVERNANCE_EXPECTED_ATTESTATION_UNAVAILABLE",
            verification["issues"],
        )

    def test_risk_dispositions_never_create_a_user_decision(self) -> None:
        snapshot = _snapshot()

        self.assertFalse(RISK_DISPOSITIONS_ARE_USER_DECISIONS)
        self.assertEqual(
            snapshot["candidate_risk_reviews"]["action_counts"],
            {"challenge": 0, "reject": 1, "support": 1},
        )
        self.assertEqual(
            snapshot["user_decision_state"]["status"],
            "awaiting_user_decision",
        )
        self.assertFalse(snapshot["user_decision_state"]["is_current"])
        self.assertEqual(snapshot["user_decision_state"]["selected_option_id"], "")
        self.assertFalse(
            snapshot["user_decision_state"]["matches_projected_candidate"]
        )
        risk_semantics = snapshot["layer_semantics"]["candidate_risk_review"]
        self.assertTrue(
            risk_semantics["support_challenge_reject_are_dispositions_only"]
        )
        self.assertTrue(risk_semantics["does_not_imply_user_decision"])
        self.assertTrue(risk_semantics["does_not_imply_approval"])

    def test_v2_user_may_support_non_preferred_projected_candidate(self) -> None:
        snapshot = _snapshot(user_decisions=[{
            "id": "decision_user_selected_b",
            "decision_version": "artifact_user_decision_v2",
            "action": "support",
            "ai_preferred_option_id": "candidate_a",
            "selected_option_id": "candidate_b",
            # v2 must not use this legacy field as the user's selection.
            "preferred_option_id": "candidate_a",
            "is_current": True,
            "integrity_ok": True,
            "artifact_binding_integrity_ok": True,
            "governance_attestation_integrity_ok": True,
        }])

        state = snapshot["user_decision_state"]
        self.assertTrue(state["is_current"])
        self.assertEqual(state["status"], "user_supported")
        self.assertEqual(state["ai_preferred_option_id"], "candidate_a")
        self.assertEqual(state["selected_option_id"], "candidate_b")
        self.assertFalse(state["selected_is_ai_preferred"])
        self.assertTrue(state["matches_projected_candidate"])

    def test_v1_support_falls_back_to_preferred_option_id(self) -> None:
        snapshot = _snapshot(user_decisions=[{
            "id": "legacy_decision_b",
            "decision_version": "artifact_user_decision_v1",
            "action": "support",
            "preferred_option_id": "candidate_b",
            "is_current": True,
            "integrity_ok": True,
        }])

        state = snapshot["user_decision_state"]
        self.assertTrue(state["is_current"])
        self.assertEqual(state["selected_option_id"], "candidate_b")
        self.assertEqual(state["preferred_option_id"], "candidate_b")
        self.assertTrue(state["matches_projected_candidate"])
        self.assertFalse(state["selected_is_ai_preferred"])

    def test_hold_and_return_are_current_only_without_selection(self) -> None:
        statuses = {
            "hold": "user_held",
            "return": "returned_for_revision",
        }
        for action, expected_status in statuses.items():
            with self.subTest(action=action):
                snapshot = _snapshot(user_decisions=[{
                    "id": f"decision_{action}",
                    "decision_version": "artifact_user_decision_v2",
                    "action": action,
                    "selected_option_id": "",
                    "is_current": True,
                    "integrity_ok": True,
                }])
                state = snapshot["user_decision_state"]
                self.assertTrue(state["is_current"])
                self.assertEqual(state["status"], expected_status)
                self.assertEqual(state["selected_option_id"], "")
                self.assertFalse(state["selected_is_ai_preferred"])
                self.assertFalse(state["matches_projected_candidate"])

    def test_v1_hold_and_return_keep_legacy_ai_preferred_context_only(self) -> None:
        for action, expected_status in (
            ("hold", "user_held"),
            ("return", "returned_for_revision"),
        ):
            with self.subTest(action=action):
                snapshot = _snapshot(user_decisions=[{
                    "id": f"legacy_{action}",
                    "decision_version": "artifact_user_decision_v1",
                    "action": action,
                    "preferred_option_id": "candidate_a",
                    "is_current": True,
                    "integrity_ok": True,
                }])
                state = snapshot["user_decision_state"]
                self.assertTrue(state["is_current"])
                self.assertEqual(state["status"], expected_status)
                self.assertEqual(state["ai_preferred_option_id"], "candidate_a")
                self.assertEqual(state["selected_option_id"], "")
                self.assertFalse(state["matches_projected_candidate"])

    def test_non_support_selection_or_unknown_support_candidate_is_not_current(self) -> None:
        cases = (
            ("hold_with_selection", "hold", "candidate_b"),
            ("return_with_selection", "return", "candidate_b"),
            ("support_unknown", "support", "candidate_unknown"),
        )
        for label, action, selected_option_id in cases:
            with self.subTest(label=label):
                snapshot = _snapshot(user_decisions=[{
                    "id": f"decision_{label}",
                    "decision_version": "artifact_user_decision_v2",
                    "action": action,
                    "selected_option_id": selected_option_id,
                    "is_current": True,
                    "integrity_ok": True,
                }])
                state = snapshot["user_decision_state"]
                self.assertFalse(state["is_current"])
                self.assertEqual(state["action"], "")
                self.assertEqual(state["selected_option_id"], "")
                self.assertFalse(state["matches_projected_candidate"])

    def test_not_round_bound_is_explicit_and_non_blocking(self) -> None:
        snapshot = build_governance_snapshot(_artifact(round_id=""))

        self.assertFalse(snapshot["applicable"])
        self.assertTrue(snapshot["ready"])
        self.assertEqual(snapshot["status"], "not_round_bound")
        self.assertFalse(snapshot["candidate_lineage"]["applicable"])
        self.assertTrue(snapshot["candidate_lineage"]["ready"])
        self.assertEqual(governance_blocking_issue_codes(snapshot), ())

    def test_legacy_round_is_explicit_and_non_blocking(self) -> None:
        snapshot = build_governance_snapshot(
            _artifact(),
            round_metadata=_round_metadata(applicable=False),
        )

        self.assertFalse(snapshot["applicable"])
        self.assertTrue(snapshot["ready"])
        self.assertEqual(snapshot["status"], "legacy_unavailable")
        self.assertEqual(snapshot["artifact_alignment"]["status"], "not_applicable")
        self.assertEqual(governance_blocking_issue_codes(snapshot), ())

    def test_safety_boundary_is_repeated_on_every_actionable_layer(self) -> None:
        snapshot = _snapshot()
        layers = [
            snapshot,
            snapshot["candidate_risk_reviews"],
            *snapshot["candidate_risk_reviews"]["reviews"],
            snapshot["user_decision_state"],
        ]
        for layer in layers:
            with self.subTest(layer=layer.get("version") or layer.get("status")):
                self.assertEqual(layer["execution_capability"], EXECUTION_CAPABILITY)
                self.assertIs(layer["live_trading_allowed"], LIVE_TRADING_ALLOWED)
                self.assertIs(
                    layer["can_autonomously_decide"],
                    CAN_AUTONOMOUSLY_DECIDE,
                )
        self.assertTrue(
            snapshot["layer_semantics"]["candidate_risk_review"][
                "does_not_authorize_execution"
            ]
        )
        self.assertTrue(
            snapshot["layer_semantics"]["user_decision"][
                "does_not_authorize_execution"
            ]
        )

    def test_canonical_hash_is_order_independent(self) -> None:
        left = {"b": [2, {"z": 3, "a": 1}], "a": "value"}
        right = {"a": "value", "b": [2, {"a": 1, "z": 3}]}
        self.assertEqual(
            canonical_governance_sha256(left),
            canonical_governance_sha256(right),
        )


if __name__ == "__main__":
    unittest.main()
