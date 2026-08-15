from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend import http_server
from backend.orchestrator import DiscussionOrchestrator
from backend.providers.base import ProviderResponse
from backend.store import StudioStore
from tests.turn_contract_fixture import append_valid_turn_contract


class BlockingProvider:
    """Block only the first speaker call so a pause can race with real work."""

    provider_id = "deepseek"

    def __init__(self) -> None:
        self.first_call_started = threading.Event()
        self.release_first_call = threading.Event()
        self._lock = threading.Lock()
        self.calls: list[dict[str, str]] = []

    def generate(
        self,
        *,
        instructions: str,
        input_text: str,
        model: str = "",
    ) -> ProviderResponse:
        with self._lock:
            call_number = len(self.calls) + 1
            self.calls.append({
                "instructions": instructions,
                "input_text": input_text,
                "model": model,
                "provider": self.provider_id,
            })
        if call_number == 1:
            self.first_call_started.set()
            if not self.release_first_call.wait(timeout=10):
                raise TimeoutError("test did not release the blocking provider")
        content = append_valid_turn_contract(
            f"第 {call_number} 位成员完成本轮发言。",
            instructions=instructions,
            input_text=input_text,
        )
        return ProviderResponse(
            ok=True,
            provider=self.provider_id,
            model=model,
            content=content,
        )


class BlockingRegistry:
    def __init__(self, provider: BlockingProvider) -> None:
        self.provider = provider

    def get(self, provider_id: str) -> BlockingProvider:
        self.provider.provider_id = str(provider_id or "deepseek").strip().lower()
        return self.provider


def checkpoint_state(store: StudioStore, room_id: str) -> dict[str, Any]:
    members = store.enabled_members(room_id)
    return {
        "member_ids": [str(member["id"]) for member in members],
        "spoken_counts": {},
        "spoken_stances": [],
        "successful_member_ids": [],
        "failed_member_ids": [],
        "previous_name": "我",
        "completed": 0,
        "failures": 0,
        "skipped": 0,
        "proposals_created": 0,
        "next_order": 1,
        "max_turns": len(members),
        "shared_context": "pause-test-context",
        "market_snapshot": None,
        "skip_provider_ids": [],
    }


class StoreRoundPauseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "studio.sqlite3")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_pause_request_is_idempotent_terminal_safe_and_resume_clears_it(self) -> None:
        round_row = self.store.create_round("room_plan", "服务端暂停契约")
        saved = self.store.save_round_checkpoint(
            "room_plan",
            round_row["id"],
            checkpoint_state(self.store, "room_plan"),
        )

        self.assertFalse(self.store.round_pause_requested("room_plan", round_row["id"]))
        self.store.request_round_pause("room_plan", round_row["id"])
        first_requested_at = self.store.get_round(
            "room_plan",
            round_row["id"],
        )["pause_requested_at"]
        self.store.request_round_pause("room_plan", round_row["id"])
        self.assertTrue(self.store.round_pause_requested("room_plan", round_row["id"]))
        self.assertGreater(first_requested_at, 0)
        self.assertEqual(
            self.store.get_round("room_plan", round_row["id"])["pause_requested_at"],
            first_requested_at,
        )

        self.assertTrue(self.store.pause_round_at_checkpoint(
            "room_plan",
            round_row["id"],
            saved["state"],
        ))
        self.assertEqual(
            self.store.get_round("room_plan", round_row["id"])["status"],
            "PAUSED",
        )
        self.assertFalse(self.store.round_pause_requested("room_plan", round_row["id"]))

        # Simulate a stale flag left by an older/interrupted implementation;
        # resume must clear it even though the normal pause acknowledgement does.
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute(
                "UPDATE rounds SET pause_requested=1,pause_requested_at=123 WHERE id=?",
                (round_row["id"],),
            )
        self.assertTrue(
            self.store.get_round("room_plan", round_row["id"])["pause_requested"]
        )
        resumed = self.store.resume_round("room_plan", round_row["id"])
        self.assertEqual(resumed["status"], "RUNNING")
        self.assertFalse(resumed["pause_requested"])
        self.assertEqual(resumed["pause_requested_at"], 0)
        self.assertFalse(self.store.round_pause_requested("room_plan", round_row["id"]))

        for terminal_status in ("COMPLETED", "PARTIAL", "CANCELLED"):
            with self.subTest(status=terminal_status):
                terminal = self.store.create_round(
                    "room_plan",
                    f"终态拒绝暂停：{terminal_status}",
                )
                self.store.complete_round(terminal["id"], terminal_status)
                with self.assertRaises(ValueError):
                    self.store.request_round_pause("room_plan", terminal["id"])
                self.assertFalse(
                    self.store.round_pause_requested("room_plan", terminal["id"])
                )

    def test_legacy_paused_round_remains_visible_when_a_newer_terminal_round_exists(self) -> None:
        paused = self.store.create_round("room_plan", "旧版遗留暂停轮")
        saved = self.store.save_round_checkpoint(
            "room_plan",
            paused["id"],
            checkpoint_state(self.store, "room_plan"),
        )
        self.store.request_round_pause("room_plan", paused["id"])
        self.assertTrue(self.store.pause_round_at_checkpoint(
            "room_plan",
            paused["id"],
            saved["state"],
        ))

        # Simulate a legacy build that incorrectly allowed a later round to supersede it.
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute(
                "UPDATE rounds SET status='CANCELLED' WHERE id=?",
                (paused["id"],),
            )
        newer = self.store.create_round("room_plan", "旧版错误创建的新轮")
        self.store.complete_round(newer["id"], "COMPLETED")
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute(
                "UPDATE rounds SET status='PAUSED' WHERE id=?",
                (paused["id"],),
            )

        snapshot = self.store.room_snapshot("room_plan")
        self.assertEqual(snapshot["latest_round"]["id"], newer["id"])
        self.assertEqual(snapshot["pending_round"]["id"], paused["id"])
        self.assertEqual(snapshot["pending_round_checkpoint"]["round_id"], paused["id"])
        with self.assertRaisesRegex(ValueError, "暂停轮次"):
            self.store.create_round("room_plan", "不得继续覆盖")

        cancelled = self.store.cancel_paused_round("room_plan", paused["id"])
        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertIsNone(self.store.pending_paused_round("room_plan"))


class HttpRoundPauseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "pause-http.sqlite3")
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
        self.thread.join(timeout=2)
        http_server.STORE = self.original_store
        self.temp_dir.cleanup()

    def post_pause(self, round_id: str) -> tuple[int, dict[str, Any]]:
        return self.post_round_action(round_id, "pause")

    def post_round_action(self, round_id: str, action: str) -> tuple[int, dict[str, Any]]:
        request = Request(
            f"{self.base_url}/api/rooms/room_plan/rounds/{round_id}/{action}",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
            },
            data=b"{}",
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def test_pause_endpoint_is_idempotent_and_terminal_safe(self) -> None:
        round_row = self.store.create_round("room_plan", "HTTP pause contract")

        first_status, first = self.post_pause(round_row["id"])
        second_status, second = self.post_pause(round_row["id"])

        self.assertEqual(first_status, 202)
        self.assertEqual(second_status, 202)
        self.assertTrue(first["accepted"])
        self.assertTrue(second["round"]["pause_requested"])

        saved_checkpoint = self.store.save_round_checkpoint(
            "room_plan",
            round_row["id"],
            checkpoint_state(self.store, "room_plan"),
        )
        self.store.pause_round_at_checkpoint(
            "room_plan",
            round_row["id"],
            saved_checkpoint["state"],
        )
        paused_status, paused = self.post_pause(round_row["id"])
        self.assertEqual(paused_status, 200)
        self.assertFalse(paused["accepted"])
        self.assertEqual(paused["round"]["status"], "PAUSED")

        cancel_status, cancelled = self.post_round_action(round_row["id"], "cancel")
        self.assertEqual(cancel_status, 200)
        self.assertEqual(cancelled["round"]["status"], "CANCELLED")
        self.assertIsNone(self.store.room_snapshot("room_plan")["pending_round"])

        terminal = self.store.create_round("room_plan", "HTTP terminal pause")
        self.store.complete_round(terminal["id"], "COMPLETED")
        terminal_status, terminal_payload = self.post_pause(terminal["id"])
        self.assertEqual(terminal_status, 409)
        self.assertFalse(terminal_payload["ok"])


class OrchestratorRoundPauseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "studio.sqlite3")
        snapshot = self.store.room_snapshot("room_plan")
        self.store.update_room("room_plan", {
            "expected_updated_at": snapshot["room"]["updated_at"],
            "discussion_mode": "sequential",
        })
        self.members = self.store.enabled_members("room_plan")
        self.provider = BlockingProvider()
        self.orchestrator = DiscussionOrchestrator(
            self.store,
            BlockingRegistry(self.provider),
            market_service=None,
        )

    def tearDown(self) -> None:
        self.provider.release_first_call.set()
        self.temp_dir.cleanup()

    def test_inflight_result_lands_pause_stops_next_member_and_resume_does_not_repeat(self) -> None:
        events: list[dict[str, Any]] = []
        errors: list[BaseException] = []

        def run_round() -> None:
            try:
                events.extend(self.orchestrator.run_round(
                    "room_plan",
                    "当前成员完成后在安全边界暂停",
                    [str(member["id"]) for member in self.members],
                ))
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        worker = threading.Thread(target=run_round, daemon=True)
        worker.start()
        self.assertTrue(
            self.provider.first_call_started.wait(timeout=5),
            "first provider call did not start",
        )
        running_round = self.store.room_snapshot("room_plan")["latest_round"]
        round_id = str(running_round["id"])

        try:
            self.store.request_round_pause("room_plan", round_id)
            self.assertTrue(self.store.round_pause_requested("room_plan", round_id))
        finally:
            self.provider.release_first_call.set()

        worker.join(timeout=10)
        self.assertFalse(worker.is_alive(), "round did not reach the pause boundary")
        self.assertEqual(errors, [])
        self.assertEqual(len(self.provider.calls), 1, "a second member started after pause was requested")

        paused_round = self.store.get_round("room_plan", round_id)
        paused_messages = self.store.round_messages("room_plan", round_id)
        paused_ai_messages = [
            message for message in paused_messages if message["sender_type"] == "ai"
        ]
        self.assertEqual(paused_round["status"], "PAUSED")
        self.assertFalse(paused_round["pause_requested"])
        self.assertFalse(self.store.round_pause_requested("room_plan", round_id))
        self.assertEqual(len(paused_ai_messages), 1)
        self.assertEqual(paused_ai_messages[0]["sender_id"], self.members[0]["id"])

        resumed_events = list(self.orchestrator.run_round(
            "room_plan",
            "恢复不得覆盖原目标",
            resume_round_id=round_id,
        ))
        final_messages = self.store.round_messages("room_plan", round_id)
        final_ai_messages = [
            message for message in final_messages if message["sender_type"] == "ai"
        ]

        self.assertEqual(resumed_events[0]["type"], "round_resumed")
        self.assertFalse(self.store.round_pause_requested("room_plan", round_id))
        self.assertEqual(len(self.provider.calls), len(self.members))
        self.assertEqual(len(final_ai_messages), len(self.members))
        self.assertEqual(
            sum(message["sender_id"] == self.members[0]["id"] for message in final_ai_messages),
            1,
        )


if __name__ == "__main__":
    unittest.main()
