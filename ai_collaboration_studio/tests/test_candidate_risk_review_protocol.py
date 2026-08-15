from __future__ import annotations

import copy
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import backend.round_launch_plan as round_launch_plan
from backend.round_launch_plan import RoundLaunchPlanService
from backend.providers.output import OUTPUT_CAPABILITIES_VERSION
from backend.store import StudioStore
from backend.turn_contract import (
    CANDIDATE_RISK_REVIEW_VERSION,
    TURN_CONTRACT_VERSION,
)
from backend.workflow_policy import default_workflow_policy
from backend.turn_envelope import (
    TURN_ENVELOPE_SCHEMA_SHA256,
    TURN_ENVELOPE_VERSION,
)


class _LocalRegistry:
    disabled_provider_ids = frozenset()

    def status(self) -> list[dict]:
        return [{
            "id": "openai",
            "name": "OpenAI",
            "model": "gpt-4.1-mini",
            "configured": True,
            "policy_disabled": False,
        }]

    def preflight(self, *_args, **_kwargs) -> None:
        raise AssertionError("launch planning must not call a provider")

    def generate(self, *_args, **_kwargs) -> None:
        raise AssertionError("launch planning must not call a provider")


def _checkpoint(
    members: list[dict],
    *,
    version: int,
    next_order: int = 1,
    successful_member_ids: list[str] | None = None,
    turn_contract_version: str | None = TURN_CONTRACT_VERSION,
    candidate_risk_review_version: str | None | object = CANDIDATE_RISK_REVIEW_VERSION,
    include_candidate_marker: bool = True,
) -> dict:
    successful = list(successful_member_ids or [])
    state = {
        "version": version,
        "member_ids": [str(member["id"]) for member in members],
        "moderator_member_id": str(members[0]["id"]),
        "spoken_counts": {member_id: 1 for member_id in successful},
        "spoken_stances": [],
        "successful_member_ids": successful,
        "failed_member_ids": [],
        "previous_name": "host",
        "completed": len(successful),
        "failures": 0,
        "skipped": 0,
        "proposals_created": 0,
        "next_order": next_order,
        "max_turns": max(1, len(members)),
        "shared_context": "",
        "market_snapshot": None,
        "round_evidence_manifest": None,
        "skip_provider_ids": [],
        "workflow_policy": default_workflow_policy("open_collaboration"),
        "capability_pack_ids": [],
        "project_workspace": None,
        "turn_contract_version": turn_contract_version,
        "turn_contract_required": turn_contract_version == TURN_CONTRACT_VERSION,
    }
    if include_candidate_marker:
        state["candidate_risk_review_version"] = candidate_risk_review_version
        state["candidate_risk_review_required"] = (
            candidate_risk_review_version == CANDIDATE_RISK_REVIEW_VERSION
        )
    if version >= 9:
        state["turn_envelope_version"] = TURN_ENVELOPE_VERSION
        state["turn_envelope_schema_sha256"] = TURN_ENVELOPE_SCHEMA_SHA256
        state["turn_output_modes_by_member"] = {
            str(member["id"]): "json_schema"
            for member in members
        }
    return state


def _valid_contract() -> dict:
    return {
        "version": TURN_CONTRACT_VERSION,
        "claims": [{
            "id": "claim_1",
            "kind": "unknown",
            "text": "The protocol fixture has no decision candidate.",
            "as_of": "",
            "evidence": [],
        }],
        "responds_to": [],
        "candidate_updates": [],
        "risks": [],
        "next_actions": [{
            "id": "action_1",
            "text": "Continue protocol validation.",
            "owner": "host",
            "state": "open",
            "due": "this round",
            "evidence": [],
        }],
        "confidence": {
            "kind": "model_subjective",
            "value": None,
            "label": "unknown",
            "basis": "",
        },
        "confidence_is_not_win_rate": True,
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


class CandidateRiskReviewProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.store = StudioStore(self.db_path)
        self.members = self.store.enabled_members("room_storage")

    def test_formal_round_freezes_marker_without_retrofitting_legacy_rows(self) -> None:
        legacy = self.store.create_round("room_storage", "legacy round")
        self.store.complete_round(legacy["id"], "CANCELLED")
        self.assertIsNone(legacy["candidate_risk_review_version"])
        self.assertFalse(legacy["candidate_risk_review_required"])

        # Recreate the pre-migration rounds schema, then reopen the store. The
        # migration must add the nullable column without rewriting this row.
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "ALTER TABLE rounds DROP COLUMN candidate_risk_review_version"
            )
        reopened = StudioStore(self.db_path)
        migrated_legacy = reopened.get_round("room_storage", legacy["id"])
        self.assertIsNone(migrated_legacy["candidate_risk_review_version"])
        self.assertFalse(migrated_legacy["candidate_risk_review_required"])

        formal = reopened.create_formal_round("room_storage", "formal round")
        self.assertEqual(
            formal["candidate_risk_review_version"],
            CANDIDATE_RISK_REVIEW_VERSION,
        )
        self.assertTrue(formal["candidate_risk_review_required"])

        self.assertIsNone(
            reopened.get_round("room_storage", legacy["id"])[
                "candidate_risk_review_version"
            ]
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            rows = connection.execute(
                "SELECT id,candidate_risk_review_version FROM rounds "
                "WHERE id IN (?,?) ORDER BY id",
                (legacy["id"], formal["id"]),
            ).fetchall()
        by_id = {row[0]: row[1] for row in rows}
        self.assertIsNone(by_id[legacy["id"]])
        self.assertEqual(by_id[formal["id"]], CANDIDATE_RISK_REVIEW_VERSION)

    def test_v6_v7_cleaning_does_not_add_candidate_protocol_keys(self) -> None:
        for version in (6, 7):
            state = _checkpoint(
                self.members,
                version=version,
                turn_contract_version=None,
                include_candidate_marker=False,
            )
            if version == 6:
                state.pop("moderator_member_id")
            clean = self.store._clean_checkpoint_state(state)
            self.assertEqual(clean["version"], version)
            self.assertNotIn("candidate_risk_review_version", clean)
            self.assertNotIn("candidate_risk_review_required", clean)

    def test_v9_checkpoint_keeps_candidate_marker_exact_and_public(self) -> None:
        formal = self.store.create_formal_round("room_storage", "v9 marker")
        saved = self.store.save_round_checkpoint(
            "room_storage",
            formal["id"],
            _checkpoint(self.members, version=9),
        )
        self.assertEqual(saved["state"]["version"], 9)
        self.assertEqual(
            saved["state"]["candidate_risk_review_version"],
            CANDIDATE_RISK_REVIEW_VERSION,
        )
        self.assertTrue(saved["state"]["candidate_risk_review_required"])

        checkpoint = self.store.get_round_checkpoint("room_storage", formal["id"])
        self.assertTrue(checkpoint["protocol_integrity_ok"])
        self.assertEqual(
            checkpoint["candidate_risk_review_version"],
            CANDIDATE_RISK_REVIEW_VERSION,
        )
        self.assertTrue(checkpoint["candidate_risk_review_required"])
        summary = self.store.room_snapshot("room_storage")["round_checkpoint"]
        self.assertEqual(
            summary["candidate_risk_review_version"],
            CANDIDATE_RISK_REVIEW_VERSION,
        )
        self.assertTrue(summary["candidate_risk_review_required"])
        bundle = self.store.round_turn_contract_bundle("room_storage", formal["id"])
        self.assertEqual(
            bundle["candidate_risk_review_version"],
            CANDIDATE_RISK_REVIEW_VERSION,
        )
        self.assertTrue(bundle["candidate_risk_review_required"])

        missing = _checkpoint(
            self.members,
            version=9,
            include_candidate_marker=False,
        )
        with self.assertRaises(ValueError):
            self.store.save_round_checkpoint("room_storage", formal["id"], missing)

        unknown = _checkpoint(self.members, version=9)
        unknown["candidate_risk_review_version"] = "candidate_risk_review_v999"
        unknown["candidate_risk_review_required"] = True
        with self.assertRaises(ValueError):
            self.store.save_round_checkpoint("room_storage", formal["id"], unknown)

        mismatch = _checkpoint(
            self.members,
            version=9,
            candidate_risk_review_version=None,
        )
        with self.assertRaises(ValueError):
            self.store.save_round_checkpoint("room_storage", formal["id"], mismatch)

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE rounds SET candidate_risk_review_version=? WHERE id=?",
                ("candidate_risk_review_v999", formal["id"]),
            )
        with self.assertRaises(ValueError):
            self.store.save_round_checkpoint(
                "room_storage",
                formal["id"],
                _checkpoint(self.members, version=9),
            )

    def test_legacy_round_accepts_v7_but_formal_round_rejects_protocol_downgrade(self) -> None:
        legacy = self.store.create_round("room_storage", "legacy checkpoint")
        legacy_state = _checkpoint(
            self.members,
            version=7,
            turn_contract_version=None,
            include_candidate_marker=False,
        )
        saved = self.store.save_round_checkpoint(
            "room_storage", legacy["id"], legacy_state
        )
        self.assertNotIn("candidate_risk_review_version", saved["state"])
        self.store.complete_round(legacy["id"], "CANCELLED")

        formal = self.store.create_formal_round("room_storage", "no downgrade")
        with self.assertRaises(ValueError):
            self.store.save_round_checkpoint(
                "room_storage",
                formal["id"],
                _checkpoint(
                    self.members,
                    version=7,
                    include_candidate_marker=False,
                ),
            )

    def test_terminal_turn_and_restore_validate_the_frozen_candidate_marker(self) -> None:
        member = self.members[0]
        formal = self.store.create_formal_round("room_storage", "terminal marker")
        turn = self.store.begin_round_turn(
            "room_storage", formal["id"], 1, member
        )
        wrong = _checkpoint(
            self.members,
            version=9,
            next_order=2,
            successful_member_ids=[member["id"]],
            candidate_risk_review_version=None,
        )
        with self.assertRaises(ValueError):
            self.store.add_message(
                "room_storage",
                sender_type="ai",
                sender_id=member["id"],
                sender_name=member["name"],
                content="must roll back",
                round_id=formal["id"],
                round_turn_id=turn["id"],
                round_turn_status="RESPONDED",
                round_checkpoint_state=wrong,
                turn_contract=_valid_contract(),
                turn_contract_version=TURN_CONTRACT_VERSION,
                turn_contract_qualified=True,
                turn_contract_issues=[],
            )
        self.assertEqual(self.store.round_messages("room_storage", formal["id"]), [])
        self.assertEqual(
            self.store.get_round_turn("room_storage", formal["id"], 1)["status"],
            "STARTED",
        )

        self.store.add_message(
            "room_storage",
            sender_type="ai",
            sender_id=member["id"],
            sender_name=member["name"],
            content="sealed terminal turn",
            round_id=formal["id"],
            round_turn_id=turn["id"],
            round_turn_status="RESPONDED",
            round_checkpoint_state=_checkpoint(
                self.members,
                version=9,
                next_order=2,
                successful_member_ids=[member["id"]],
            ),
            turn_contract=_valid_contract(),
            turn_contract_version=TURN_CONTRACT_VERSION,
            turn_contract_qualified=True,
            turn_contract_issues=[],
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "DELETE FROM round_checkpoints WHERE round_id=?", (formal["id"],)
            )
            connection.execute(
                "UPDATE rounds SET candidate_risk_review_version=NULL WHERE id=?",
                (formal["id"],),
            )
        with self.assertRaises(ValueError):
            self.store.restore_round_turn_checkpoint("room_storage", formal["id"], 1)

    def test_pause_checkpoint_validates_candidate_marker_atomically(self) -> None:
        formal = self.store.create_formal_round("room_storage", "pause marker")
        self.store.request_round_pause("room_storage", formal["id"])
        with self.assertRaises(ValueError):
            self.store.pause_round_at_checkpoint(
                "room_storage",
                formal["id"],
                _checkpoint(
                    self.members,
                    version=9,
                    candidate_risk_review_version=None,
                ),
            )
        still_running = self.store.get_round("room_storage", formal["id"])
        self.assertEqual(still_running["status"], "RUNNING")
        self.assertTrue(still_running["pause_requested"])

        self.assertTrue(self.store.pause_round_at_checkpoint(
            "room_storage", formal["id"], _checkpoint(self.members, version=9)
        ))
        self.assertEqual(
            self.store.get_round("room_storage", formal["id"])["status"], "PAUSED"
        )

    def test_marked_round_confirmation_requires_ready_candidate_risk_reviews(self) -> None:
        member = self.members[0]
        formal = self.store.create_formal_round("room_storage", "confirmation gate")
        turn = self.store.begin_round_turn("room_storage", formal["id"], 1, member)
        self.store.add_message(
            "room_storage",
            sender_type="ai",
            sender_id=member["id"],
            sender_name=member["name"],
            content="no reviewed candidates yet",
            round_id=formal["id"],
            round_turn_id=turn["id"],
            round_turn_status="RESPONDED",
            round_checkpoint_state=_checkpoint(
                self.members,
                version=9,
                next_order=2,
                successful_member_ids=[member["id"]],
            ),
            turn_contract=_valid_contract(),
            turn_contract_version=TURN_CONTRACT_VERSION,
            turn_contract_qualified=True,
            turn_contract_issues=[],
        )
        self.store.complete_round(formal["id"], "COMPLETED")

        with closing(self.store._connect()) as connection:
            issues = self.store._artifact_round_confirmation_issues(
                connection, "room_storage", formal["id"]
            )
        self.assertTrue(any(
            "CANDIDATE_RISK_REVIEW" in issue for issue in issues
        ), issues)

    def test_launch_plan_freezes_protocol_markers_into_the_hash(self) -> None:
        service = RoundLaunchPlanService(self.store, _LocalRegistry())
        first = service.build("room_storage", "protocol hash")
        self.assertEqual(first["room"]["protocols"], {
            "turn_contract_version": TURN_CONTRACT_VERSION,
            "turn_contract_required": True,
            "candidate_risk_review_version": CANDIDATE_RISK_REVIEW_VERSION,
            "candidate_risk_review_required": True,
            "turn_envelope_version": TURN_ENVELOPE_VERSION,
            "turn_envelope_schema_sha256": TURN_ENVELOPE_SCHEMA_SHA256,
            "provider_output_capabilities_version": OUTPUT_CAPABILITIES_VERSION,
        })

        original = round_launch_plan.CANDIDATE_RISK_REVIEW_VERSION
        try:
            round_launch_plan.CANDIDATE_RISK_REVIEW_VERSION = "candidate_risk_review_v2"
            second = RoundLaunchPlanService(self.store, _LocalRegistry()).build(
                "room_storage", "protocol hash"
            )
        finally:
            round_launch_plan.CANDIDATE_RISK_REVIEW_VERSION = original
        self.assertNotEqual(first["plan_hash"], second["plan_hash"])


if __name__ == "__main__":
    unittest.main()
