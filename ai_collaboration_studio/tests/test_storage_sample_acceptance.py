from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.request import urlopen

from backend import http_server
from backend.decision_lineage import artifact_binding_payload, canonical_sha256
from backend.storage_sample_acceptance import (
    STORAGE_SAMPLE_ACCEPTANCE_PREVIOUS_VERSION,
    STORAGE_SAMPLE_ACCEPTANCE_VERSION,
    StorageSampleAcceptance,
)
from backend.store import OBSERVATION_SCORECARD_VERSION, StudioStore
from backend.templates import (
    STORAGE_RESEARCH_CAPABILITY_PACKS,
    get_room_template,
)
from backend.turn_contract import TURN_CONTRACT_VERSION
from backend.user_decision import USER_DECISION_VERSION, USER_DECISION_VERSION_V1


ROUND_ID = "round_storage_acceptance"
ARTIFACT_ID = "artifact_storage_acceptance"
DECISION_ID = "decision_storage_acceptance"


class ReadOnlyFixtureStore:
    def __init__(
        self,
        *,
        snapshot: dict[str, Any],
        checkpoint: dict[str, Any],
        bundle: dict[str, Any],
        artifacts: list[dict[str, Any]],
    ) -> None:
        self.snapshot = copy.deepcopy(snapshot)
        self.checkpoint = copy.deepcopy(checkpoint)
        self.bundle = copy.deepcopy(bundle)
        self.artifacts = copy.deepcopy(artifacts)
        self.calls: list[tuple[str, str]] = []

    def room_snapshot(self, room_id: str) -> dict[str, Any]:
        self.calls.append(("room_snapshot", room_id))
        return copy.deepcopy(self.snapshot)

    def get_round_checkpoint(
        self,
        room_id: str,
        round_id: str,
    ) -> dict[str, Any]:
        self.calls.append(("get_round_checkpoint", f"{room_id}:{round_id}"))
        return copy.deepcopy(self.checkpoint)

    def round_turn_contract_bundle(
        self,
        room_id: str,
        round_id: str,
    ) -> dict[str, Any]:
        self.calls.append(("round_turn_contract_bundle", f"{room_id}:{round_id}"))
        return copy.deepcopy(self.bundle)

    def list_artifacts(self, room_id: str) -> list[dict[str, Any]]:
        self.calls.append(("list_artifacts", room_id))
        return copy.deepcopy(self.artifacts)


class FrozenConvergence:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = copy.deepcopy(state)
        self.calls: list[dict[str, Any]] = []

    def evaluate(
        self,
        room_id: str,
        *,
        round_id: str = "",
        snapshot: dict[str, Any] | None = None,
        runtime: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append({
            "room_id": room_id,
            "round_id": round_id,
            "snapshot_supplied": snapshot is not None,
            "runtime_supplied": runtime is not None,
        })
        return copy.deepcopy(self.state)


def ready_fixture(
    *,
    sample_count: int = 19,
    scorecard_qualified: bool = False,
) -> tuple[ReadOnlyFixtureStore, FrozenConvergence]:
    template = get_room_template("us_storage_committee")
    members = []
    messages = []
    for index, raw_member in enumerate(template["members"], start=1):
        member = copy.deepcopy(raw_member)
        member_id = f"member_{index:02d}"
        member.update({
            "id": member_id,
            "room_id": "room_storage",
            "version": 1,
            "enabled": True,
        })
        members.append(member)
        messages.append({
            "id": f"message_{index:02d}",
            "round_id": ROUND_ID,
            "round_turn_id": f"turn_{index:02d}",
            "turn_order": index,
            "sender_type": "ai",
            "sender_id": member_id,
            "sender_name": member["name"],
            "member_version": 1,
            "is_formal_round_turn": True,
            "turn_contract_version": TURN_CONTRACT_VERSION,
            "turn_contract": {},
            "turn_contract_qualified": True,
            "turn_contract_integrity_ok": True,
            "member_snapshot": member,
            "member_snapshot_integrity_ok": True,
        })
    if len(members) != 12:
        raise AssertionError("storage template fixture must have exactly twelve members")

    artifact: dict[str, Any] = {
        "id": ARTIFACT_ID,
        "room_id": "room_storage",
        "round_id": ROUND_ID,
        "title": "Storage research sample",
        "status": "CONFIRMED",
        "version": 3,
        "content": {
            "summary": "A reviewed storage-sector comparison.",
            "summary_evidence": [{
                "type": "message",
                "id": "message_01",
                "verification_status": "verified",
                "version_status": "current",
                "evidence_role": "support",
            }],
            "decision": {
                "status": "candidate",
                "options": [
                    {"id": "option_a", "title": "Option A"},
                    {"id": "option_b", "title": "Option B"},
                ],
                "preferred_option_id": "option_a",
                "rationale": "Option A has the clearest reviewed evidence.",
            },
        },
        "evidence_review": {
            "relation_count": 1,
            "reviewed_relation_count": 1,
            "unreviewed_relation_count": 0,
            "confirmation_ready": True,
        },
    }
    artifact["user_decision"] = {
        "id": DECISION_ID,
        "decision_version": USER_DECISION_VERSION,
        "room_id": "room_storage",
        "artifact_id": ARTIFACT_ID,
        "artifact_version": 3,
        "action": "support",
        "rationale": "The reviewed candidate is suitable for simulation.",
        "ai_preferred_option_id": "option_a",
        "selected_option_id": "option_a",
        "preferred_option_id": "option_a",
        "selected_option_revision": None,
        "selected_option_origin_message_id": "",
        "selected_option_latest_message_id": "",
        "selected_option_snapshot_sha256": "",
        "selected_option_risk_review_required": False,
        "artifact_snapshot_sha256": canonical_sha256(
            artifact_binding_payload(artifact)
        ),
        "created_by": "user",
        "is_current": True,
        "integrity_ok": True,
        "artifact_binding_integrity_ok": True,
        "candidate_binding_integrity_ok": True,
        "decision_record_integrity_ok": True,
    }
    artifact["user_decision_history"] = [artifact["user_decision"]]

    checkpoint_state = {
        "version": 7,
        "member_ids": [member["id"] for member in members],
        "moderator_member_id": members[0]["id"],
        "successful_member_ids": [member["id"] for member in members],
        "failed_member_ids": [],
        "workflow_policy": template["workflow_policy"],
        "capability_pack_ids": list(STORAGE_RESEARCH_CAPABILITY_PACKS),
        "turn_contract_version": TURN_CONTRACT_VERSION,
        "turn_contract_required": True,
    }
    snapshot = {
        "room": {
            "id": "room_storage",
            "template_id": "us_storage_committee",
            "capability_pack_ids": list(STORAGE_RESEARCH_CAPABILITY_PACKS),
            "workflow_policy": template["workflow_policy"],
        },
        "members": members,
        "latest_round": {
            "id": ROUND_ID,
            "room_id": "room_storage",
            "objective": "Compare MU, SNDK, WDC, and STX.",
            "status": "COMPLETED",
            "turn_contract_version": TURN_CONTRACT_VERSION,
        },
        "artifacts": [artifact],
        "observation_scorecard": {
            "version": OBSERVATION_SCORECARD_VERSION,
            "overall": {
                "sample_count": sample_count,
                "minimum_samples": 20,
                "qualified": scorecard_qualified,
                "mixed_methodology": False,
                "mixed_conditions": False,
                "descriptive_only": not scorecard_qualified,
                "metric_label": (
                    "statistical hit rate"
                    if scorecard_qualified
                    else "sample insufficient"
                ),
            },
            "independence": {
                "raw_resolved_count": sample_count,
                "independent_sample_count": sample_count,
            },
        },
    }
    bundle = {
        "applicable": True,
        "valid": True,
        "round_status": "COMPLETED",
        "turn_contract_version": TURN_CONTRACT_VERSION,
        "messages": messages,
        "successful_member_ids": [member["id"] for member in members],
        "issues": [],
        "execution_capability": "none",
        "live_trading_allowed": False,
    }
    convergence_state = {
        "round_id": ROUND_ID,
        "can_host_finish": True,
        "can_present_candidate_best": True,
        "data_gate": {
            "ready": True,
            "status": "ready",
            "market_snapshot_complete": True,
            "ready_market_symbols": ["US.MU", "US.SNDK", "US.WDC", "US.STX"],
            "blockers": [],
        },
        "research_evidence_gate": {
            "ready": True,
            "status": "ready",
            "blockers": [],
        },
        "turn_contract_gate": {
            "applicable": True,
            "ready": True,
            "version": TURN_CONTRACT_VERSION,
            "qualified_message_count": 12,
            "blockers": [],
        },
        "discussion_gate": {
            "ready": True,
            "successful_member_count": 12,
            "required_success_count": 12,
            "blockers": [],
        },
        "evidence_gate": {
            "ready": True,
            "status": "current_confirmed",
            "artifact_id": ARTIFACT_ID,
            "artifact_version": 3,
            "artifact_status": "CONFIRMED",
            "evidence_count": 1,
            "unreviewed_evidence_count": 0,
            "blockers": [],
        },
        "decision_gate": {
            "ready": True,
            "status": "candidate_selected",
            "option_count": 2,
            "preferred_option_id": "option_a",
            "blockers": [],
        },
        "user_decision_gate": {
            "ready": True,
            "status": "user_supported",
            "decision_id": DECISION_ID,
            "decision_version": USER_DECISION_VERSION,
            "artifact_id": ARTIFACT_ID,
            "artifact_version": 3,
            "ai_preferred_option_id": "option_a",
            "selected_option_id": "option_a",
            "preferred_option_id": "option_a",
            "action": "support",
            "blockers": [],
        },
        "portfolio_gate": {
            "applicable": True,
            "ready": True,
            "status": "linked_confirmed",
            "confirmed_count": 1,
            "draft_count": 0,
            "linked_count": 1,
            "legacy_unlinked_count": 0,
            "decision_id": DECISION_ID,
            "blockers": [],
            "warnings": [],
        },
        "research_ready": True,
        "execution_capability": "none",
        "live_trading_allowed": False,
    }
    return (
        ReadOnlyFixtureStore(
            snapshot=snapshot,
            checkpoint={"round_id": ROUND_ID, "state": checkpoint_state},
            bundle=bundle,
            artifacts=[artifact],
        ),
        FrozenConvergence(convergence_state),
    )


def set_user_decision_action(
    store: ReadOnlyFixtureStore,
    convergence: FrozenConvergence,
    action: str,
) -> None:
    artifact = store.artifacts[0]
    decision = artifact["user_decision"]
    decision["action"] = action
    if action != "support":
        decision["selected_option_id"] = ""
        decision["preferred_option_id"] = ""
        decision["selected_option_revision"] = None
        decision["selected_option_origin_message_id"] = ""
        decision["selected_option_latest_message_id"] = ""
        decision["selected_option_snapshot_sha256"] = ""
        decision["selected_option_risk_review_required"] = False
    decision["artifact_snapshot_sha256"] = canonical_sha256(
        artifact_binding_payload(artifact)
    )
    artifact["user_decision_history"] = [copy.deepcopy(decision)]

    user_gate = convergence.state["user_decision_gate"]
    user_gate["action"] = action
    if action != "support":
        user_gate["selected_option_id"] = ""
        user_gate["preferred_option_id"] = ""
    user_gate["status"] = {
        "hold": "user_held",
        "return": "user_returned",
    }.get(action, "user_supported")
    convergence.state["portfolio_gate"] = {
        "applicable": False,
        "ready": True,
        "status": "awaiting_user_support",
        "confirmed_count": 0,
        "draft_count": 0,
        "linked_count": 0,
        "legacy_unlinked_count": 0,
        "decision_id": DECISION_ID,
        "blockers": [],
        "warnings": [],
    }
    convergence.state["research_ready"] = False


class StorageSampleAcceptanceTests(unittest.TestCase):
    def test_accepts_current_round_while_statistics_remain_separate(self) -> None:
        store, convergence = ready_fixture(
            sample_count=19,
            scorecard_qualified=False,
        )
        service = StorageSampleAcceptance(store, convergence)  # type: ignore[arg-type]

        first = service.evaluate("room_storage")
        second = service.evaluate("room_storage")

        self.assertEqual(first, second)
        self.assertEqual(first["version"], STORAGE_SAMPLE_ACCEPTANCE_VERSION)
        self.assertEqual(first["state"], "accepted")
        self.assertTrue(first["acceptance_ready"])
        self.assertTrue(first["meeting_reviewed"])
        self.assertTrue(first["research_sample_ready"])
        self.assertEqual(first["user_decision_action"], "support")
        self.assertTrue(first["paper_portfolio_gate"]["ready"])
        self.assertEqual(first["market_snapshot_gate"], {
            "id": "market_snapshot",
            "state": "ready",
            "ready": True,
            "current": 4,
            "required": 4,
            "detail": "MU、SNDK、WDC、STX 的同轮 Futu 只读行情快照均已就绪。",
        })
        self.assertTrue(first["research_evidence_gate"]["ready"])
        self.assertEqual(first["research_evidence_gate"]["current"], 1)
        self.assertEqual(first["research_evidence_gate"]["required"], 1)
        self.assertFalse(first["statistical_validation_ready"])
        self.assertEqual(first["statistics"], {
            "sample_count": 19,
            "minimum_samples": 20,
            "qualified": False,
        })
        self.assertEqual(
            [item["id"] for item in first["stages"]],
            [
                "market_data",
                "discussion",
                "artifact",
                "evidence",
                "user_decision",
                "paper_portfolio",
                "simulation",
            ],
        )
        self.assertTrue(all(
            item["ready"] for item in first["stages"] if item["id"] != "simulation"
        ))
        self.assertFalse(first["stages"][-1]["ready"])
        self.assertEqual(
            {
                item["id"]: (item["current"], item["required"])
                for item in first["stages"]
            },
            {
                "market_data": (4, 4),
                "discussion": (12, 12),
                "artifact": (1, 1),
                "evidence": (1, 1),
                "user_decision": (1, 1),
                "paper_portfolio": (1, 1),
                "simulation": (19, 20),
            },
        )
        self.assertEqual(first["role_audit"]["unique_member_count"], 12)
        self.assertEqual(first["role_audit"]["matched_role_slot_count"], 12)
        self.assertEqual(len(first["role_audit"]["data_guardian_member_ids"]), 1)
        self.assertEqual(first["provider_calls"], 0)
        self.assertEqual(first["market_calls"], 0)
        self.assertFalse(first["live_trading_allowed"])
        self.assertEqual(
            {name for name, _ in store.calls},
            {
                "room_snapshot",
                "get_round_checkpoint",
                "round_turn_contract_bundle",
                "list_artifacts",
            },
        )
        self.assertEqual(len(convergence.calls), 2)

    def test_reports_market_snapshot_ready_when_official_evidence_is_blocked(
        self,
    ) -> None:
        store, convergence = ready_fixture()
        convergence.state["data_gate"].update({
            "ready": False,
            "status": "blocked",
            "market_snapshot_complete": True,
        })
        convergence.state["research_evidence_gate"].update({
            "ready": False,
            "status": "blocked",
            "blockers": [{"code": "STORAGE_OFFICIAL_FILINGS_MISSING"}],
        })
        convergence.state["research_ready"] = False

        result = StorageSampleAcceptance(  # type: ignore[arg-type]
            store,
            convergence,
        ).evaluate("room_storage")

        self.assertEqual(result["state"], "blocked")
        self.assertFalse(result["acceptance_ready"])
        self.assertEqual(
            (result["market_snapshot_gate"]["ready"],
             result["market_snapshot_gate"]["current"],
             result["market_snapshot_gate"]["required"]),
            (True, 4, 4),
        )
        self.assertFalse(result["research_evidence_gate"]["ready"])
        self.assertEqual(
            result["research_evidence_gate"]["blocker_codes"],
            ["STORAGE_OFFICIAL_FILINGS_MISSING"],
        )
        legacy_market_stage = next(
            item for item in result["stages"] if item["id"] == "market_data"
        )
        self.assertFalse(legacy_market_stage["ready"])
        self.assertEqual(legacy_market_stage["current"], 4)
        self.assertIn("官方研究证据仍未通过", legacy_market_stage["detail"])

    def test_twenty_comparable_samples_enable_only_statistical_validation(self) -> None:
        store, convergence = ready_fixture(
            sample_count=20,
            scorecard_qualified=True,
        )

        result = StorageSampleAcceptance(  # type: ignore[arg-type]
            store,
            convergence,
        ).evaluate("room_storage")

        self.assertTrue(result["acceptance_ready"])
        self.assertTrue(result["statistical_validation_ready"])
        self.assertTrue(result["statistics"]["qualified"])
        simulation = next(
            item for item in result["stages"] if item["id"] == "simulation"
        )
        self.assertTrue(simulation["ready"])
        self.assertEqual(simulation["current"], 20)
        self.assertEqual(simulation["required"], 20)

    def test_legacy_scorecard_contract_cannot_unlock_statistical_validation(self) -> None:
        store, convergence = ready_fixture(
            sample_count=20,
            scorecard_qualified=True,
        )
        store.snapshot["observation_scorecard"].pop("version")

        result = StorageSampleAcceptance(  # type: ignore[arg-type]
            store,
            convergence,
        ).evaluate("room_storage")

        self.assertFalse(result["statistical_validation_ready"])
        self.assertFalse(result["statistics"]["qualified"])
        self.assertFalse(result["statistical_validation"]["scorecard_contract_ready"])
        self.assertEqual(result["statistical_validation"]["sample_count"], 0)

    def test_reuses_supplied_snapshot_and_convergence_state(self) -> None:
        store, convergence = ready_fixture()
        supplied_snapshot = copy.deepcopy(store.snapshot)

        result = StorageSampleAcceptance(  # type: ignore[arg-type]
            store,
            convergence,
        ).evaluate(
            "room_storage",
            snapshot=supplied_snapshot,
            convergence_state=convergence.state,
        )

        self.assertTrue(result["acceptance_ready"])
        self.assertNotIn("room_snapshot", {name for name, _ in store.calls})
        self.assertEqual(convergence.calls, [])

    def test_storage_acceptance_scope_follows_capability_not_template_id(self) -> None:
        store, convergence = ready_fixture()
        store.snapshot["room"]["template_id"] = "open_collaboration"

        result = StorageSampleAcceptance(  # type: ignore[arg-type]
            store,
            convergence,
        ).evaluate("room_storage")

        self.assertTrue(result["applicable"])
        self.assertEqual(result["state"], "accepted")

    def test_storage_room_without_round_uses_no_round_state(self) -> None:
        store, convergence = ready_fixture()
        store.snapshot["latest_round"] = None
        store.snapshot["artifacts"] = []
        store.artifacts = []

        result = StorageSampleAcceptance(  # type: ignore[arg-type]
            store,
            convergence,
        ).evaluate("room_storage")

        self.assertEqual(result["state"], "no_round")
        self.assertIsNone(result["latest_round_id"])
        self.assertFalse(result["research_sample_ready"])
        self.assertEqual(convergence.calls, [])

    def test_support_without_current_confirmed_portfolio_requires_review(self) -> None:
        store, convergence = ready_fixture()
        convergence.state["portfolio_gate"] = {
            "applicable": True,
            "ready": False,
            "status": "review_required",
            "confirmed_count": 0,
            "draft_count": 1,
            "linked_count": 1,
            "legacy_unlinked_count": 0,
            "decision_id": DECISION_ID,
            "blockers": [{
                "code": "DECISION_PACKAGE_PORTFOLIO_NOT_CONFIRMED",
                "title": "Portfolio revision is not confirmed",
            }],
            "warnings": [],
        }
        convergence.state["research_ready"] = False

        result = StorageSampleAcceptance(  # type: ignore[arg-type]
            store,
            convergence,
        ).evaluate("room_storage")

        self.assertTrue(result["meeting_reviewed"])
        self.assertFalse(result["research_sample_ready"])
        self.assertFalse(result["acceptance_ready"])
        self.assertEqual(result["state"], "review_required")
        self.assertFalse(result["paper_portfolio_gate"]["ready"])
        self.assertEqual(
            result["paper_portfolio_gate"]["blocker_codes"],
            ["DECISION_PACKAGE_PORTFOLIO_NOT_CONFIRMED"],
        )
        stage = next(
            item for item in result["stages"]
            if item["id"] == "paper_portfolio"
        )
        self.assertEqual(stage["state"], "pending")
        self.assertEqual((stage["current"], stage["required"]), (0, 1))
        self.assertEqual(result["provider_calls"], 0)
        self.assertEqual(result["market_calls"], 0)

    def test_hold_and_return_are_explicit_non_accepted_terminal_states(self) -> None:
        for action, expected_state in (("hold", "deferred"), ("return", "returned")):
            with self.subTest(action=action):
                store, convergence = ready_fixture()
                set_user_decision_action(store, convergence, action)

                result = StorageSampleAcceptance(  # type: ignore[arg-type]
                    store,
                    convergence,
                ).evaluate("room_storage")

                self.assertTrue(result["meeting_reviewed"])
                self.assertFalse(result["research_sample_ready"])
                self.assertFalse(result["acceptance_ready"])
                self.assertEqual(result["state"], expected_state)
                self.assertEqual(result["user_decision_action"], action)
                self.assertFalse(result["paper_portfolio_gate"]["ready"])
                stage = next(
                    item for item in result["stages"]
                    if item["id"] == "paper_portfolio"
                )
                self.assertEqual(stage["state"], expected_state)
                self.assertEqual((stage["current"], stage["required"]), (0, 1))

    def test_portfolio_gate_must_bind_the_exact_current_decision(self) -> None:
        store, convergence = ready_fixture()
        convergence.state["portfolio_gate"]["decision_id"] = "decision_stale"

        result = StorageSampleAcceptance(  # type: ignore[arg-type]
            store,
            convergence,
        ).evaluate("room_storage")

        self.assertTrue(result["meeting_reviewed"])
        self.assertFalse(result["research_sample_ready"])
        self.assertEqual(result["state"], "review_required")
        self.assertFalse(result["paper_portfolio_gate"]["decision_id_exact"])
        self.assertFalse(result["checks"]["paper_portfolio_gate"]["ready"])

    def test_v3_compatibility_contract_preserves_legacy_field_surface(self) -> None:
        store, convergence = ready_fixture()

        result = StorageSampleAcceptance(  # type: ignore[arg-type]
            store,
            convergence,
        ).evaluate("room_storage")

        self.assertEqual(result["version"], STORAGE_SAMPLE_ACCEPTANCE_VERSION)
        self.assertEqual(result["schema_version"], result["version"])
        self.assertTrue(result["compatibility"]["legacy_fields_preserved"])
        self.assertEqual(
            result["compatibility"]["previous_version"],
            STORAGE_SAMPLE_ACCEPTANCE_PREVIOUS_VERSION,
        )
        self.assertTrue(
            result["compatibility"]["legacy_v1_meeting_acceptance_ready"]
        )
        self.assertFalse(
            result["compatibility"]["legacy_v1_projection_authoritative"]
        )
        for legacy_field in (
            "acceptance_ready",
            "blocked",
            "checks",
            "blockers",
            "stages",
            "next_actions",
        ):
            self.assertIn(legacy_field, result)

    def test_v3_rejects_legacy_v1_user_decision_as_explicit_selection(self) -> None:
        store, convergence = ready_fixture()
        decision = store.artifacts[0]["user_decision"]
        decision["decision_version"] = USER_DECISION_VERSION_V1
        convergence.state["user_decision_gate"][
            "decision_version"
        ] = USER_DECISION_VERSION_V1

        result = StorageSampleAcceptance(  # type: ignore[arg-type]
            store,
            convergence,
        ).evaluate("room_storage")

        binding = result["checks"]["exact_user_decision_binding"]["actual"]
        self.assertFalse(result["research_sample_ready"])
        self.assertFalse(binding["ready"])
        self.assertFalse(binding["decision_version_exact"])
        self.assertEqual(binding["decision_version"], USER_DECISION_VERSION_V1)

    def test_duplicate_artifact_and_decision_hash_mismatch_fail_closed(self) -> None:
        store, convergence = ready_fixture()
        duplicate = copy.deepcopy(store.artifacts[0])
        duplicate["id"] = "artifact_duplicate"
        store.artifacts.append(duplicate)

        duplicate_result = StorageSampleAcceptance(  # type: ignore[arg-type]
            store,
            convergence,
        ).evaluate("room_storage")

        self.assertEqual(duplicate_result["state"], "blocked")
        self.assertFalse(duplicate_result["acceptance_ready"])
        self.assertIn(
            "ROUND_ARTIFACT_COUNT_INVALID",
            [item["code"] for item in duplicate_result["blockers"]],
        )

        store, convergence = ready_fixture()
        store.artifacts[0]["user_decision"]["artifact_snapshot_sha256"] = "0" * 64
        store.artifacts[0]["user_decision"]["artifact_binding_integrity_ok"] = False
        binding_result = StorageSampleAcceptance(  # type: ignore[arg-type]
            store,
            convergence,
        ).evaluate("room_storage")

        self.assertEqual(binding_result["state"], "review_required")
        self.assertFalse(binding_result["acceptance_ready"])
        self.assertFalse(
            binding_result["checks"]["exact_user_decision_binding"]["ready"]
        )
        self.assertIn(
            "USER_DECISION_BINDING_INVALID",
            [item["code"] for item in binding_result["blockers"]],
        )

    def test_role_overlap_cannot_replace_independent_data_guardian(self) -> None:
        store, convergence = ready_fixture()
        second_snapshot = store.bundle["messages"][1]["member_snapshot"]
        second_snapshot["capabilities"] = list(
            second_snapshot.get("capabilities") or []
        ) + ["data_quality_review"]

        result = StorageSampleAcceptance(  # type: ignore[arg-type]
            store,
            convergence,
        ).evaluate("room_storage")

        self.assertFalse(result["acceptance_ready"])
        self.assertFalse(result["role_audit"]["data_guardian_ready"])
        self.assertEqual(
            len(result["role_audit"]["data_guardian_candidate_ids"]),
            2,
        )
        self.assertIn(
            "DATA_GUARDIAN_NOT_INDEPENDENT",
            [item["code"] for item in result["blockers"]],
        )

    def test_terminal_pre_v7_round_is_legacy_and_sqlite_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "studio.sqlite3"
            store = StudioStore(database_path)
            round_row = store.create_round(
                "room_storage",
                "Historical storage round without a structured turn contract.",
            )
            store.complete_round(round_row["id"], "PARTIAL")
            before = hashlib.sha256(database_path.read_bytes()).hexdigest()

            result = StorageSampleAcceptance(store).evaluate("room_storage")

            after = hashlib.sha256(database_path.read_bytes()).hexdigest()
            self.assertEqual(before, after)
            self.assertEqual(result["state"], "legacy")
            self.assertTrue(result["blocked"])
            self.assertFalse(result["acceptance_ready"])
            self.assertFalse(result["historical_backfill_performed"])
            self.assertIsNone(store.get_round_checkpoint("room_storage", round_row["id"]))
            self.assertIn(
                "LEGACY_CHECKPOINT_MISSING",
                [item["code"] for item in result["blockers"]],
            )
            self.assertIn(
                "LEGACY_TURN_CONTRACT_MISSING",
                [item["code"] for item in result["blockers"]],
            )
            self.assertTrue(all(
                any("\u4e00" <= character <= "\u9fff" for character in item["detail"])
                for item in result["blockers"]
            ))


class StorageSampleAcceptanceHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, self.convergence = ready_fixture()
        self.original_store = http_server.STORE
        self.original_orchestrator = http_server.ORCHESTRATOR
        http_server.STORE = self.store  # type: ignore[assignment]
        http_server.ORCHESTRATOR = SimpleNamespace(convergence=self.convergence)
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            http_server.StudioRequestHandler,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        http_server.STORE = self.original_store
        http_server.ORCHESTRATOR = self.original_orchestrator

    def test_get_endpoint_returns_read_only_acceptance_without_secret_fields(self) -> None:
        with urlopen(
            f"{self.base_url}/api/rooms/room_storage/storage-sample-acceptance",
            timeout=5,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))

        acceptance = payload["storage_sample_acceptance"]
        self.assertTrue(payload["ok"])
        self.assertEqual(acceptance["version"], STORAGE_SAMPLE_ACCEPTANCE_VERSION)
        self.assertEqual(acceptance["state"], "accepted")
        self.assertTrue(acceptance["read_only"])
        self.assertEqual(acceptance["provider_calls"], 0)
        self.assertEqual(acceptance["market_calls"], 0)
        self.assertEqual(
            {name for name, _ in self.store.calls},
            {
                "room_snapshot",
                "get_round_checkpoint",
                "round_turn_contract_bundle",
                "list_artifacts",
            },
        )
        encoded = json.dumps(payload, ensure_ascii=False).lower()
        self.assertNotIn("api_key", encoded)
        self.assertNotIn("authorization", encoded)
        self.assertNotIn("bearer ", encoded)


if __name__ == "__main__":
    unittest.main()
