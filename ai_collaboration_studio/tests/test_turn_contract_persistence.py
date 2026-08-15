from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend.convergence import ConvergenceService
from backend.store import StudioStore
from backend.turn_contract import TURN_CONTRACT_VERSION
from backend.workflow_policy import default_workflow_policy


def checkpoint_state(
    members: list[dict],
    *,
    successful_member_ids: list[str] | None = None,
    next_order: int = 1,
    market_snapshot: dict | None = None,
    round_evidence_manifest: dict | None = None,
) -> dict:
    successful = list(successful_member_ids or [])
    return {
        "member_ids": [str(member["id"]) for member in members],
        "spoken_counts": {member_id: 1 for member_id in successful},
        "spoken_stances": [
            str(member.get("stance") or "neutral")
            for member in members
            if str(member.get("id")) in successful
        ],
        "successful_member_ids": successful,
        "failed_member_ids": [],
        "previous_name": "主持人" if successful else "我",
        "completed": len(successful),
        "failures": 0,
        "skipped": 0,
        "proposals_created": 0,
        "next_order": next_order,
        "max_turns": max(1, len(members)),
        "shared_context": "",
        "market_snapshot": market_snapshot,
        "round_evidence_manifest": round_evidence_manifest,
        "skip_provider_ids": ["openai"],
        "workflow_policy": default_workflow_policy("open_collaboration"),
        "capability_pack_ids": [],
        "project_workspace": None,
        "turn_contract_version": TURN_CONTRACT_VERSION,
        "turn_contract_required": True,
    }


def facilitator_contract(
    market_snapshot_id: str = "",
    *,
    responds_to: list[dict] | None = None,
) -> dict:
    return {
        "version": TURN_CONTRACT_VERSION,
        "claims": [
            {
                "id": "scope_1",
                "kind": "unknown",
                "text": "需要先统一本轮评价口径。",
                "as_of": "",
                "evidence": ([{
                    "type": "round_market_snapshot",
                    "id": market_snapshot_id,
                    "role": "context",
                }] if market_snapshot_id else []),
            }
        ],
        "responds_to": list(responds_to or []),
        "candidate_updates": [],
        "risks": [],
        "next_actions": [
            {
                "id": "action_1",
                "text": "请下一位成员核验共同证据。",
                "owner": "下一位分析成员",
                "state": "open",
                "due": "本轮",
                "evidence": [],
            }
        ],
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


def frozen_market_snapshot(snapshot_id: str) -> dict:
    return {
        "snapshot_id": snapshot_id,
        "captured_at": "2026-08-01T20:00:00Z",
        "state": "ready",
        "evidence": {"version": "test_market_evidence_v1"},
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


class TurnContractPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.store = StudioStore(self.db_path)
        self.members = self.store.enabled_members("room_plan")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_legacy_round_and_messages_remain_null_after_reopen(self) -> None:
        round_row = self.store.create_round("room_plan", "历史兼容")
        message = self.store.add_message(
            "room_plan",
            sender_type="ai",
            sender_id=self.members[0]["id"],
            sender_name=self.members[0]["name"],
            content="历史普通发言。",
            round_id=round_row["id"],
        )
        self.assertIsNone(round_row["turn_contract_version"])
        self.assertIsNone(message["turn_contract_version"])
        self.assertIsNone(message["turn_contract"])
        self.assertIsNone(message["turn_contract_qualified"])
        room = self.store.room_snapshot("room_plan")["room"]
        self.store.update_room("room_plan", {
            "expected_settings_version": room["settings_version"],
            "capability_pack_ids": ["structured_turn_contract_v1"],
        })

        reopened = StudioStore(self.db_path)
        restored = reopened.round_messages("room_plan", round_row["id"])[0]
        self.assertIsNone(restored["turn_contract_version"])
        self.assertIsNone(restored["turn_contract"])
        self.assertIsNone(restored["turn_contract_qualified"])
        with closing(sqlite3.connect(self.db_path)) as connection:
            raw = connection.execute(
                "SELECT turn_contract_json,turn_contract_qualified,turn_contract_issues_json "
                "FROM messages WHERE id=?",
                (message["id"],),
            ).fetchone()
        self.assertEqual(raw, (None, None, None))
        legacy_bundle = reopened.round_turn_contract_bundle("room_plan", round_row["id"])
        self.assertFalse(legacy_bundle["applicable"])

    def test_formal_round_factory_enforces_current_contract_but_legacy_factory_does_not(self) -> None:
        legacy = self.store.create_round("room_plan", "旧入口兼容")
        self.store.complete_round(legacy["id"], "CANCELLED")
        formal = self.store.create_formal_round("room_plan", "生产正式轮")

        self.assertIsNone(legacy["turn_contract_version"])
        self.assertEqual(formal["turn_contract_version"], TURN_CONTRACT_VERSION)

    def test_missing_fairness_counter_defaults_to_zero_and_is_checkpoint_sealed(self) -> None:
        round_row = self.store.create_round(
            "room_plan",
            "公平检查点封印",
            turn_contract_version=TURN_CONTRACT_VERSION,
        )
        legacy_state = checkpoint_state(self.members)
        self.assertNotIn("consecutive_interjections", legacy_state)

        saved = self.store.save_round_checkpoint(
            "room_plan", round_row["id"], legacy_state
        )
        self.assertEqual(saved["state"]["consecutive_interjections"], 0)

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            raw = connection.execute(
                "SELECT state_json FROM round_checkpoints WHERE round_id=?",
                (round_row["id"],),
            ).fetchone()
            tampered_state = json.loads(raw[0])
            tampered_state["consecutive_interjections"] = 1
            connection.execute(
                "UPDATE round_checkpoints SET state_json=? WHERE round_id=?",
                (json.dumps(tampered_state, ensure_ascii=False), round_row["id"]),
            )

        self.assertIsNone(
            self.store.get_round_checkpoint("room_plan", round_row["id"])
        )

    def test_v1_contract_message_and_terminal_turn_commit_atomically(self) -> None:
        self.assertEqual(
            self.store.room_snapshot("room_plan")["room"]["capability_pack_ids"],
            [],
        )
        member = self.members[0]
        round_row = self.store.create_round(
            "room_plan",
            "结构化发言",
            turn_contract_version=TURN_CONTRACT_VERSION,
        )
        turn = self.store.begin_round_turn("room_plan", round_row["id"], 1, member)
        state = checkpoint_state(
            self.members,
            successful_member_ids=[member["id"]],
            next_order=2,
        )
        message = self.store.add_message(
            "room_plan",
            sender_type="ai",
            sender_id=member["id"],
            sender_name=member["name"],
            content="先统一目标与证据边界。",
            round_id=round_row["id"],
            round_turn_id=turn["id"],
            round_turn_status="RESPONDED",
            round_checkpoint_state=state,
            turn_contract=facilitator_contract(),
            turn_contract_version=TURN_CONTRACT_VERSION,
            turn_contract_qualified=True,
            turn_contract_issues=[],
        )
        self.assertTrue(message["is_formal_round_turn"])
        self.assertTrue(message["turn_contract_qualified"])
        self.assertTrue(message["turn_contract_integrity_ok"])
        self.assertEqual(message["turn_contract"]["execution_capability"], "none")
        self.assertFalse(message["turn_contract"]["live_trading_allowed"])
        terminal = self.store.get_round_turn("room_plan", round_row["id"], 1)
        checkpoint = self.store.get_round_checkpoint("room_plan", round_row["id"])
        self.assertEqual(terminal["status"], "RESPONDED")
        self.assertEqual(terminal["message"]["id"], message["id"])
        self.assertEqual(checkpoint["state"]["turn_contract_version"], TURN_CONTRACT_VERSION)
        bundle = self.store.round_turn_contract_bundle("room_plan", round_row["id"])
        self.assertTrue(bundle["applicable"])
        self.assertTrue(bundle["valid"], bundle["issues"])

    def test_bundle_rejects_reply_projection_tampered_away_from_response_graph(self) -> None:
        member = self.members[0]
        round_row = self.store.create_round(
            "room_plan",
            "回复图持久化复核",
            turn_contract_version=TURN_CONTRACT_VERSION,
        )
        first_turn = self.store.begin_round_turn(
            "room_plan", round_row["id"], 1, member,
        )
        first = self.store.add_message(
            "room_plan",
            sender_type="ai",
            sender_id=member["id"],
            sender_name=member["name"],
            content="先统一目标与证据边界。",
            round_id=round_row["id"],
            round_turn_id=first_turn["id"],
            round_turn_status="RESPONDED",
            round_checkpoint_state=checkpoint_state(
                self.members,
                successful_member_ids=[member["id"]],
                next_order=2,
            ),
            turn_contract=facilitator_contract(),
            turn_contract_version=TURN_CONTRACT_VERSION,
            turn_contract_qualified=True,
            turn_contract_issues=[],
        )
        second_turn = self.store.begin_round_turn(
            "room_plan", round_row["id"], 2, member,
        )
        second = self.store.add_message(
            "room_plan",
            sender_type="ai",
            sender_id=member["id"],
            sender_name=member["name"],
            content="承接前序边界，补充下一项核验任务。",
            reply_to=member["name"],
            reply_to_message_id=first["id"],
            round_id=round_row["id"],
            round_turn_id=second_turn["id"],
            round_turn_status="RESPONDED",
            round_checkpoint_state=checkpoint_state(
                self.members,
                successful_member_ids=[member["id"]],
                next_order=3,
            ),
            turn_contract=facilitator_contract(responds_to=[{
                "type": "message",
                "id": first["id"],
                "relation": "supports",
                "reason": "继续沿用已经明确的目标与证据边界。",
            }]),
            turn_contract_version=TURN_CONTRACT_VERSION,
            turn_contract_qualified=True,
            turn_contract_issues=[],
        )
        self.store.complete_round(round_row["id"], "COMPLETED")

        valid = self.store.round_turn_contract_bundle("room_plan", round_row["id"])
        self.assertTrue(valid["valid"], valid["issues"])
        self.assertEqual(valid["messages"][1]["reply_to_message_id"], first["id"])

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE messages SET reply_to_message_id='' WHERE id=?",
                (second["id"],),
            )
        tampered = StudioStore(self.db_path).round_turn_contract_bundle(
            "room_plan",
            round_row["id"],
        )
        self.assertFalse(tampered["valid"])
        self.assertTrue(any(
            "群聊回复边与持久化发言合同不一致" in issue
            for issue in tampered["issues"]
        ))

    def test_persisted_market_snapshot_reference_revalidates_only_against_same_round_manifest(self) -> None:
        member = self.members[0]
        snapshot_a = frozen_market_snapshot("snapshot-round-a")
        shared_context, base_manifest = self.store.material_prompt_bundle("room_plan")
        manifest_a = self.store.finalize_round_evidence_manifest(
            base_manifest,
            shared_context=shared_context,
            market_snapshot=snapshot_a,
        )
        round_a = self.store.create_round(
            "room_plan",
            "冻结快照同轮引用",
            turn_contract_version=TURN_CONTRACT_VERSION,
        )
        turn_a = self.store.begin_round_turn("room_plan", round_a["id"], 1, member)
        state_a = checkpoint_state(
            self.members,
            successful_member_ids=[member["id"]],
            next_order=2,
            market_snapshot=snapshot_a,
            round_evidence_manifest=manifest_a,
        )
        self.store.add_message(
            "room_plan",
            sender_type="ai",
            sender_id=member["id"],
            sender_name=member["name"],
            content="只引用本轮冻结快照。",
            round_id=round_a["id"],
            round_turn_id=turn_a["id"],
            round_turn_status="RESPONDED",
            round_checkpoint_state=state_a,
            turn_contract=facilitator_contract("snapshot-round-a"),
            turn_contract_version=TURN_CONTRACT_VERSION,
            turn_contract_qualified=True,
            turn_contract_issues=[],
        )

        reopened = StudioStore(self.db_path)
        valid_bundle = reopened.round_turn_contract_bundle(
            "room_plan",
            round_a["id"],
        )
        self.assertTrue(valid_bundle["valid"], valid_bundle["issues"])
        evidence = valid_bundle["messages"][0]["turn_contract"]["claims"][0]["evidence"]
        self.assertEqual(evidence[0]["id"], "snapshot-round-a")
        self.assertNotIn("source_revision", evidence[0])
        self.assertNotIn("source_snapshot_sha256", evidence[0])

        snapshot_b = frozen_market_snapshot("snapshot-round-b")
        manifest_b = self.store.finalize_round_evidence_manifest(
            base_manifest,
            shared_context=shared_context,
            market_snapshot=snapshot_b,
        )
        round_b = self.store.create_round(
            "room_plan",
            "拒绝跨轮快照引用",
            turn_contract_version=TURN_CONTRACT_VERSION,
        )
        turn_b = self.store.begin_round_turn("room_plan", round_b["id"], 1, member)
        state_b = checkpoint_state(
            self.members,
            successful_member_ids=[member["id"]],
            next_order=2,
            market_snapshot=snapshot_b,
            round_evidence_manifest=manifest_b,
        )
        self.store.add_message(
            "room_plan",
            sender_type="ai",
            sender_id=member["id"],
            sender_name=member["name"],
            content="伪造引用上一轮快照。",
            round_id=round_b["id"],
            round_turn_id=turn_b["id"],
            round_turn_status="RESPONDED",
            round_checkpoint_state=state_b,
            turn_contract=facilitator_contract("snapshot-round-a"),
            turn_contract_version=TURN_CONTRACT_VERSION,
            turn_contract_qualified=True,
            turn_contract_issues=[],
        )

        invalid_bundle = self.store.round_turn_contract_bundle(
            "room_plan",
            round_b["id"],
        )
        self.assertFalse(invalid_bundle["valid"])
        self.assertTrue(any(
            "REFERENCE_NOT_ALLOWED" in issue
            for issue in invalid_bundle["issues"]
        ))

    def test_v1_checkpoint_mismatch_rolls_back_message_and_turn(self) -> None:
        member = self.members[0]
        round_row = self.store.create_round(
            "room_plan",
            "拒绝版本漂移",
            turn_contract_version=TURN_CONTRACT_VERSION,
        )
        turn = self.store.begin_round_turn("room_plan", round_row["id"], 1, member)
        state = checkpoint_state(self.members, next_order=2)
        state["turn_contract_version"] = None
        state["turn_contract_required"] = False
        with self.assertRaisesRegex(ValueError, "版本"):
            self.store.add_message(
                "room_plan",
                sender_type="ai",
                sender_id=member["id"],
                sender_name=member["name"],
                content="不应落库。",
                round_id=round_row["id"],
                round_turn_id=turn["id"],
                round_turn_status="RESPONDED",
                round_checkpoint_state=state,
                turn_contract=facilitator_contract(),
                turn_contract_version=TURN_CONTRACT_VERSION,
                turn_contract_qualified=True,
                turn_contract_issues=[],
            )
        self.assertEqual(self.store.round_messages("room_plan", round_row["id"]), [])
        self.assertEqual(
            self.store.get_round_turn("room_plan", round_row["id"], 1)["status"],
            "STARTED",
        )

    def test_v1_convergence_ignores_forged_checkpoint_and_runtime_successes(self) -> None:
        member = self.members[0]
        round_row = self.store.create_round(
            "room_plan",
            "伪造成功集合不得绕过",
            turn_contract_version=TURN_CONTRACT_VERSION,
        )
        turn = self.store.begin_round_turn("room_plan", round_row["id"], 1, member)
        first_state = checkpoint_state(
            self.members,
            successful_member_ids=[member["id"]],
            next_order=2,
        )
        self.store.add_message(
            "room_plan",
            sender_type="ai",
            sender_id=member["id"],
            sender_name=member["name"],
            content="只有这一条发言具有合格合同。",
            round_id=round_row["id"],
            round_turn_id=turn["id"],
            round_turn_status="RESPONDED",
            round_checkpoint_state=first_state,
            turn_contract=facilitator_contract(),
            turn_contract_version=TURN_CONTRACT_VERSION,
            turn_contract_qualified=True,
            turn_contract_issues=[],
        )
        forged_ids = [str(member_row["id"]) for member_row in self.members]
        forged_state = checkpoint_state(
            self.members,
            successful_member_ids=forged_ids,
            next_order=2,
        )
        self.store.save_round_checkpoint("room_plan", round_row["id"], forged_state)
        convergence = ConvergenceService(self.store).evaluate(
            "room_plan",
            round_id=round_row["id"],
            runtime={"successful_member_ids": forged_ids},
        )
        self.assertEqual(convergence["discussion_gate"]["successful_member_count"], 1)
        self.assertEqual(convergence["turn_contract_gate"]["qualified_message_count"], 1)
        self.assertFalse(convergence["discussion_gate"]["ready"])


if __name__ == "__main__":
    unittest.main()
