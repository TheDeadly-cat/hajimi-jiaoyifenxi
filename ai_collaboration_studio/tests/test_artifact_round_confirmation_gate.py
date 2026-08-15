from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend import http_server
from backend.store import StudioStore


def freeze_round(
    store: StudioStore,
    room_id: str,
    *,
    status: str,
    successful_count: int | None = None,
) -> tuple[dict, list[dict]]:
    snapshot = store.room_snapshot(room_id)
    members = snapshot["members"]
    selected = members if successful_count is None else members[:successful_count]
    round_row = store.create_round(room_id, "验证冻结职责覆盖后才允许确认产物")
    messages = [
        store.add_message(
            room_id,
            sender_type="ai",
            sender_id=member["id"],
            sender_name=member["name"],
            identity=member["identity"],
            provider=member["provider"],
            model=member["model"],
            content=f"{member['name']} 已按冻结身份完成本轮职责。",
            round_id=round_row["id"],
            member_version=member["version"],
        )
        for member in selected
    ]
    shared_context, manifest = store.material_prompt_bundle(room_id)
    manifest = store.finalize_round_evidence_manifest(
        manifest,
        shared_context=shared_context,
        market_snapshot=None,
    )
    room = snapshot["room"]
    store.save_round_checkpoint(
        room_id,
        round_row["id"],
        {
            "member_ids": [member["id"] for member in members],
            "spoken_counts": {member["id"]: 1 for member in selected},
            "spoken_stances": [member["stance"] for member in selected],
            "successful_member_ids": [member["id"] for member in selected],
            "failed_member_ids": [],
            "previous_name": selected[-1]["name"] if selected else "我",
            "completed": len(selected),
            "failures": 0,
            "skipped": 0,
            "proposals_created": 0,
            "next_order": len(selected) + 1,
            "max_turns": max(1, len(members)),
            "workflow_policy": room["workflow_policy"],
            "capability_pack_ids": room.get("capability_pack_ids") or [],
            "shared_context": shared_context,
            "market_snapshot": None,
            "frozen_market": None,
            "round_evidence_manifest": manifest,
            "project_workspace": None,
            "skip_provider_ids": ["openai"],
        },
    )
    store.complete_round(round_row["id"], status)
    return round_row, messages


def create_reviewed_artifact(
    store: StudioStore,
    room_id: str,
    round_id: str,
    evidence_message_id: str,
) -> dict:
    return store.create_artifact(
        room_id,
        round_id=round_id,
        title="轮次确认门测试产物",
        content={
            "summary": "本轮冻结身份职责已完成核验。",
            "summary_evidence": [
                {
                    "type": "message",
                    "id": evidence_message_id,
                    "evidence_role": "support",
                    "verification_status": "source_checked",
                    "review_note": "已核对同轮原文。",
                }
            ],
            "requirements": [],
            "risks": [],
            "conclusions": [],
            "disagreements": [],
            "unknowns": [],
            "actions": [],
            "decision": {
                "status": "undecided",
                "options": [],
                "preferred_option_id": "",
                "rationale": "",
                "evidence": [],
            },
        },
    )


class ArtifactRoundConfirmationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "round-gate.sqlite3")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_completed_generic_round_with_full_frozen_coverage_can_confirm(self) -> None:
        round_row, messages = freeze_round(
            self.store,
            "room_plan",
            status="COMPLETED",
        )
        artifact = create_reviewed_artifact(
            self.store,
            "room_plan",
            round_row["id"],
            messages[0]["id"],
        )

        confirmed = self.store.confirm_artifact(
            "room_plan",
            artifact["id"],
            expected_version=artifact["version"],
        )

        self.assertEqual(confirmed["status"], "CONFIRMED")

    def test_non_completed_rounds_cannot_confirm_even_with_full_coverage(self) -> None:
        for status in ("PAUSED", "PARTIAL", "CANCELLED"):
            with self.subTest(status=status):
                round_row, messages = freeze_round(
                    self.store,
                    "room_plan",
                    status=status,
                )
                artifact = create_reviewed_artifact(
                    self.store,
                    "room_plan",
                    round_row["id"],
                    messages[0]["id"],
                )

                with self.assertRaisesRegex(ValueError, "COMPLETED"):
                    self.store.confirm_artifact(
                        "room_plan",
                        artifact["id"],
                        expected_version=artifact["version"],
                    )
                self.assertEqual(
                    self.store.get_artifact("room_plan", artifact["id"])["status"],
                    "DRAFT",
                )
                if status == "PAUSED":
                    self.store.cancel_paused_round("room_plan", round_row["id"])

    def test_completed_storage_round_with_one_missing_role_cannot_confirm(self) -> None:
        members = self.store.room_snapshot("room_storage")["members"]
        round_row, messages = freeze_round(
            self.store,
            "room_storage",
            status="COMPLETED",
            successful_count=len(members) - 1,
        )
        artifact = create_reviewed_artifact(
            self.store,
            "room_storage",
            round_row["id"],
            messages[0]["id"],
        )

        with self.assertRaisesRegex(ValueError, "有效成功成员覆盖"):
            self.store.confirm_artifact(
                "room_storage",
                artifact["id"],
                expected_version=artifact["version"],
            )

    def test_corrupt_or_missing_frozen_workflow_policy_cannot_fall_back_to_defaults(self) -> None:
        for raw_policy in (
            None,
            {"version": "corrupt", "stage_order": "not-a-list"},
        ):
            with self.subTest(raw_policy=raw_policy):
                round_row, messages = freeze_round(
                    self.store,
                    "room_plan",
                    status="COMPLETED",
                )
                artifact = create_reviewed_artifact(
                    self.store,
                    "room_plan",
                    round_row["id"],
                    messages[0]["id"],
                )
                with closing(sqlite3.connect(self.store.path)) as connection, connection:
                    row = connection.execute(
                        "SELECT state_json FROM round_checkpoints WHERE round_id=?",
                        (round_row["id"],),
                    ).fetchone()
                    state = json.loads(row[0])
                    if raw_policy is None:
                        state.pop("workflow_policy", None)
                    else:
                        state["workflow_policy"] = raw_policy
                    connection.execute(
                        "UPDATE round_checkpoints SET state_json=? WHERE round_id=?",
                        (json.dumps(state, ensure_ascii=False), round_row["id"]),
                    )

                with self.assertRaisesRegex(ValueError, "完整性|冻结流程政策"):
                    self.store.confirm_artifact(
                        "room_plan",
                        artifact["id"],
                        expected_version=artifact["version"],
                    )
                self.assertEqual(
                    self.store.get_artifact("room_plan", artifact["id"])["status"],
                    "DRAFT",
                )

    def test_malformed_frozen_member_collections_fail_closed(self) -> None:
        mutations = (
            (
                "member_ids_object",
                lambda state: state.__setitem__(
                    "member_ids",
                    {member_id: True for member_id in state["member_ids"]},
                ),
            ),
            (
                "successful_member_ids_object",
                lambda state: state.__setitem__(
                    "successful_member_ids",
                    {member_id: True for member_id in state["successful_member_ids"]},
                ),
            ),
            (
                "blank_member_id",
                lambda state: state.__setitem__(
                    "member_ids",
                    [*state["member_ids"], " "],
                ),
            ),
            (
                "non_string_success_id",
                lambda state: state.__setitem__(
                    "successful_member_ids",
                    [*state["successful_member_ids"], 7],
                ),
            ),
            (
                "duplicate_member_id",
                lambda state: state.__setitem__(
                    "member_ids",
                    [*state["member_ids"], state["member_ids"][0]],
                ),
            ),
            (
                "duplicate_success_id",
                lambda state: state.__setitem__(
                    "successful_member_ids",
                    [*state["successful_member_ids"], state["successful_member_ids"][0]],
                ),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(case=label):
                round_row, messages = freeze_round(
                    self.store,
                    "room_plan",
                    status="COMPLETED",
                )
                artifact = create_reviewed_artifact(
                    self.store,
                    "room_plan",
                    round_row["id"],
                    messages[0]["id"],
                )
                with closing(sqlite3.connect(self.store.path)) as connection, connection:
                    row = connection.execute(
                        "SELECT state_json FROM round_checkpoints WHERE round_id=?",
                        (round_row["id"],),
                    ).fetchone()
                    state = json.loads(row[0])
                    mutate(state)
                    connection.execute(
                        "UPDATE round_checkpoints SET state_json=? WHERE round_id=?",
                        (json.dumps(state, ensure_ascii=False), round_row["id"]),
                    )

                with self.assertRaisesRegex(ValueError, "完整性|成员集合"):
                    self.store.confirm_artifact(
                        "room_plan",
                        artifact["id"],
                        expected_version=artifact["version"],
                    )
                self.assertEqual(
                    self.store.get_artifact("room_plan", artifact["id"])["status"],
                    "DRAFT",
                )


class ArtifactRoundConfirmationHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "round-gate-http.sqlite3")
        self.original_store = http_server.STORE
        http_server.STORE = self.store
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
        self.thread.join(timeout=3)
        http_server.STORE = self.original_store
        self.temp_dir.cleanup()

    def post_confirm(self, room_id: str, artifact: dict) -> tuple[int, dict]:
        request = Request(
            f"{self.base_url}/api/rooms/{room_id}/artifacts/{artifact['id']}/confirm",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
            },
            data=json.dumps({"expected_version": artifact["version"]}).encode("utf-8"),
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def test_paused_generic_and_incomplete_storage_rounds_return_409(self) -> None:
        generic_round, generic_messages = freeze_round(
            self.store,
            "room_plan",
            status="PAUSED",
        )
        generic_artifact = create_reviewed_artifact(
            self.store,
            "room_plan",
            generic_round["id"],
            generic_messages[0]["id"],
        )
        storage_count = len(self.store.room_snapshot("room_storage")["members"])
        storage_round, storage_messages = freeze_round(
            self.store,
            "room_storage",
            status="COMPLETED",
            successful_count=storage_count - 1,
        )
        storage_artifact = create_reviewed_artifact(
            self.store,
            "room_storage",
            storage_round["id"],
            storage_messages[0]["id"],
        )

        for room_id, artifact in (
            ("room_plan", generic_artifact),
            ("room_storage", storage_artifact),
        ):
            with self.subTest(room_id=room_id):
                status, payload = self.post_confirm(room_id, artifact)
                self.assertEqual(status, 409)
                self.assertFalse(payload["ok"])
                self.assertIn("产物绑定轮次不满足确认条件", payload["error"])
                self.assertEqual(
                    self.store.get_artifact(room_id, artifact["id"])["status"],
                    "DRAFT",
                )


if __name__ == "__main__":
    unittest.main()
