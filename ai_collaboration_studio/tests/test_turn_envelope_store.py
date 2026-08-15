from __future__ import annotations

import copy
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend.store import StudioStore
from backend.turn_contract import (
    CANDIDATE_RISK_REVIEW_VERSION,
    TURN_CONTRACT_VERSION,
)
from backend.turn_envelope import (
    TURN_ENVELOPE_SCHEMA_SHA256,
    TURN_ENVELOPE_VERSION,
)
from backend.workflow_policy import default_workflow_policy


def checkpoint_v9(
    members: list[dict],
    round_row: dict,
    *,
    modes: dict[str, str] | None = None,
    next_order: int = 1,
) -> dict:
    member_ids = [str(member["id"]) for member in members]
    return {
        "version": 9,
        "member_ids": member_ids,
        "moderator_member_id": member_ids[0],
        "spoken_counts": {},
        "spoken_stances": [],
        "successful_member_ids": [],
        "failed_member_ids": [],
        "previous_name": "host",
        "completed": 0,
        "failures": 0,
        "skipped": 0,
        "proposals_created": 0,
        "next_order": next_order,
        "max_turns": max(1, len(member_ids)),
        "shared_context": "",
        "market_snapshot": None,
        "round_evidence_manifest": None,
        "skip_provider_ids": [],
        "workflow_policy": default_workflow_policy("open_collaboration"),
        "capability_pack_ids": [],
        "project_workspace": None,
        "turn_contract_version": TURN_CONTRACT_VERSION,
        "turn_contract_required": True,
        "candidate_risk_review_version": round_row.get(
            "candidate_risk_review_version"
        ),
        "candidate_risk_review_required": (
            round_row.get("candidate_risk_review_version")
            == CANDIDATE_RISK_REVIEW_VERSION
        ),
        "turn_envelope_version": TURN_ENVELOPE_VERSION,
        "turn_envelope_schema_sha256": TURN_ENVELOPE_SCHEMA_SHA256,
        "turn_output_modes_by_member": modes
        or {member_id: "json_schema" for member_id in member_ids},
    }


def checkpoint_v8(members: list[dict]) -> dict:
    state = checkpoint_v9(
        members,
        {"candidate_risk_review_version": CANDIDATE_RISK_REVIEW_VERSION},
    )
    state["version"] = 8
    state.pop("turn_envelope_version")
    state.pop("turn_envelope_schema_sha256")
    state.pop("turn_output_modes_by_member")
    return state


def approved_route(member: dict, *, mode: str = "json_schema") -> dict:
    return {
        "member_id": str(member["id"]),
        "approved_member_version": int(member["version"]),
        "provider": str(member["provider"]).strip().lower(),
        "model": str(member["model"]),
        "turn_output_mode": mode,
        "turn_envelope_version": TURN_ENVELOPE_VERSION,
        "turn_envelope_schema_sha256": TURN_ENVELOPE_SCHEMA_SHA256,
    }


class TurnEnvelopeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="turn-envelope-store-")
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.store = StudioStore(self.db_path)
        self.members = self.store.enabled_members("room_storage")

    def test_nullable_migration_does_not_backfill_historical_rounds(self) -> None:
        legacy = self.store.create_round("room_storage", "legacy")
        self.store.complete_round(legacy["id"], "CANCELLED")
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("ALTER TABLE rounds DROP COLUMN turn_envelope_version")
            connection.execute(
                "ALTER TABLE rounds DROP COLUMN turn_envelope_schema_sha256"
            )

        reopened = StudioStore(self.db_path)
        historical = reopened.get_round("room_storage", legacy["id"])
        self.assertIsNone(historical["turn_envelope_version"])
        self.assertIsNone(historical["turn_envelope_schema_sha256"])
        self.assertFalse(historical["turn_envelope_required"])

        formal = reopened.create_formal_round("room_storage", "formal")
        self.assertEqual(formal["turn_envelope_version"], TURN_ENVELOPE_VERSION)
        self.assertEqual(
            formal["turn_envelope_schema_sha256"],
            TURN_ENVELOPE_SCHEMA_SHA256,
        )

    def test_create_round_normalizes_protocol_as_an_atomic_pair(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create_round(
                "room_storage",
                "missing hash",
                turn_contract_version=TURN_CONTRACT_VERSION,
                turn_envelope_version=TURN_ENVELOPE_VERSION,
            )
        with self.assertRaises(ValueError):
            self.store.create_round(
                "room_storage",
                "bad hash",
                turn_contract_version=TURN_CONTRACT_VERSION,
                turn_envelope_version=TURN_ENVELOPE_VERSION,
                turn_envelope_schema_sha256="0" * 64,
            )
        with self.assertRaises(ValueError):
            self.store.create_round(
                "room_storage",
                "missing contract",
                turn_envelope_version=TURN_ENVELOPE_VERSION,
                turn_envelope_schema_sha256=TURN_ENVELOPE_SCHEMA_SHA256,
            )

    def test_v9_checkpoint_is_public_and_v8_history_is_not_backfilled(self) -> None:
        formal = self.store.create_formal_round("room_storage", "v9")
        saved = self.store.save_round_checkpoint(
            "room_storage",
            formal["id"],
            checkpoint_v9(self.members, formal),
        )
        self.assertEqual(saved["state"]["version"], 9)
        checkpoint = self.store.get_round_checkpoint(
            "room_storage", formal["id"]
        )
        self.assertTrue(checkpoint["protocol_integrity_ok"])
        self.assertEqual(checkpoint["turn_envelope_version"], TURN_ENVELOPE_VERSION)
        self.assertEqual(
            set(checkpoint["turn_output_modes_by_member"]),
            {str(member["id"]) for member in self.members},
        )
        summary = self.store.room_snapshot("room_storage")["round_checkpoint"]
        self.assertEqual(summary["turn_envelope_version"], TURN_ENVELOPE_VERSION)

        legacy = self.store.create_round(
            "room_storage",
            "v8 history",
            turn_contract_version=TURN_CONTRACT_VERSION,
            candidate_risk_review_version=CANDIDATE_RISK_REVIEW_VERSION,
        )
        self.store.save_round_checkpoint(
            "room_storage", legacy["id"], checkpoint_v8(self.members)
        )
        historical = self.store.get_round_checkpoint(
            "room_storage", legacy["id"]
        )
        self.assertTrue(historical["protocol_integrity_ok"])
        self.assertIsNone(historical["turn_envelope_version"])
        self.assertEqual(historical["turn_output_modes_by_member"], {})

    def test_v9_checkpoint_rejects_missing_extra_and_noncanonical_modes(self) -> None:
        formal = self.store.create_formal_round("room_storage", "coverage")
        state = checkpoint_v9(self.members, formal)
        missing = copy.deepcopy(state)
        missing["turn_output_modes_by_member"].pop(str(self.members[-1]["id"]))
        with self.assertRaises(ValueError):
            self.store.save_round_checkpoint("room_storage", formal["id"], missing)

        extra = copy.deepcopy(state)
        extra["turn_output_modes_by_member"]["unknown"] = "json_schema"
        with self.assertRaises(ValueError):
            self.store.save_round_checkpoint("room_storage", formal["id"], extra)

        invalid = copy.deepcopy(state)
        invalid["turn_output_modes_by_member"][str(self.members[0]["id"])] = "text"
        with self.assertRaises(ValueError):
            self.store.save_round_checkpoint("room_storage", formal["id"], invalid)

        v8_with_marker = checkpoint_v8(self.members)
        v8_with_marker["turn_output_modes_by_member"] = {}
        with self.assertRaises(ValueError):
            self.store._clean_checkpoint_state(v8_with_marker)

    def test_round_checkpoint_cross_validation_fails_closed_after_tamper(self) -> None:
        formal = self.store.create_formal_round("room_storage", "tamper")
        state = checkpoint_v9(self.members, formal)
        self.store.save_round_checkpoint("room_storage", formal["id"], state)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE rounds SET turn_envelope_schema_sha256=? WHERE id=?",
                ("0" * 64, formal["id"]),
            )
        checkpoint = self.store.get_round_checkpoint(
            "room_storage", formal["id"]
        )
        self.assertFalse(checkpoint["protocol_integrity_ok"])
        with self.assertRaises(ValueError):
            self.store.save_round_checkpoint("room_storage", formal["id"], state)

    def test_provider_route_v2_is_sealed_and_speaker_checks_frozen_mode(self) -> None:
        member = self.store.update_member(
            "room_storage",
            str(self.members[0]["id"]),
            {"model": "turn-envelope-fixture"},
        )
        self.members = self.store.enabled_members("room_storage")
        route = approved_route(member)
        manifest = self.store._provider_member_routes_manifest({
            "version": "provider_member_routes_v2",
            "members": [route],
        })
        self.assertEqual(manifest["members"][0], route)
        incomplete = copy.deepcopy(route)
        incomplete.pop("turn_envelope_schema_sha256")
        with self.assertRaises(ValueError):
            self.store._provider_member_routes_manifest({
                "version": "provider_member_routes_v2",
                "members": [incomplete],
            })

        formal = self.store.create_formal_round("room_storage", "speaker route")
        modes = {
            str(item["id"]): "json_schema"
            for item in self.members
        }
        self.store.save_round_checkpoint(
            "room_storage",
            formal["id"],
            checkpoint_v9(self.members, formal, modes=modes),
        )
        mismatched = approved_route(member, mode="json_object")
        with self.assertRaisesRegex(ValueError, "output mode"):
            self.store.begin_round_turn(
                "room_storage", formal["id"], 1, member,
                approved_route=mismatched,
            )
        turn = self.store.begin_round_turn(
            "room_storage", formal["id"], 1, member,
            approved_route=route,
        )
        self.assertEqual(turn["status"], "STARTED")

        run = self.store.create_provider_execution_run(
            "room_storage",
            scope="turn_envelope_test",
            client_request_id="turn-envelope-v2",
            plan_hash="a" * 64,
            max_calls=4,
            member_routes={
                "version": "provider_member_routes_v2",
                "members": [route],
            },
        )
        self.assertTrue(run["member_routes_integrity_ok"])
        bound = self.store.bind_provider_execution_round(run["id"], formal["id"])
        self.assertEqual(
            bound["member_routes"]["members"][0]["turn_output_mode"],
            "json_schema",
        )

        historical_route = {
            key: value
            for key, value in route.items()
            if key not in {
                "turn_output_mode",
                "turn_envelope_version",
                "turn_envelope_schema_sha256",
            }
        }
        legacy_run = self.store.create_provider_execution_run(
            "room_storage",
            scope="turn_envelope_test",
            client_request_id="turn-envelope-v1",
            plan_hash="b" * 64,
            max_calls=4,
            member_routes={
                "version": "provider_member_routes_v1",
                "members": [historical_route],
            },
        )
        with self.assertRaisesRegex(ValueError, "routes v2"):
            self.store.bind_provider_execution_round(
                legacy_run["id"], formal["id"]
            )

        moderator_attempt = self.store.begin_director_attempt(
            "room_storage",
            formal["id"],
            moderator_member_id=str(member["id"]),
            moderator_member_version=int(member["version"]),
            provider=str(member["provider"]),
            model=str(member["model"]),
            approved_route=approved_route(member, mode="prompt_json"),
        )
        self.assertEqual(moderator_attempt["status"], "STARTED")


if __name__ == "__main__":
    unittest.main()
