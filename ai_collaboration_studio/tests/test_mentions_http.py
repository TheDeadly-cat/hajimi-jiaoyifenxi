from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from backend import http_server
from backend.instance_ownership import DatabaseInstanceOwner
from backend.orchestrator import DiscussionOrchestrator
from backend.store import StudioStore
from tests.test_orchestrator import FakeProvider, FakeRegistry


class MentionHttpApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "mentions-http.sqlite3")
        for member in self.store.enabled_members("room_plan"):
            self.store.update_member(
                "room_plan",
                member["id"],
                {"provider": "openai", "model": "fake-model"},
            )
        self.provider = FakeProvider()
        self.orchestrator = DiscussionOrchestrator(
            self.store,
            FakeRegistry(self.provider),
            market_service=None,
        )
        self.original_store = http_server.STORE
        self.original_orchestrator = http_server.ORCHESTRATOR
        http_server.STORE = self.store
        http_server.ORCHESTRATOR = self.orchestrator
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), http_server.StudioRequestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        http_server.STORE = self.original_store
        http_server.ORCHESTRATOR = self.original_orchestrator
        self.temp_dir.cleanup()

    def post_stream(self, payload: dict[str, object]) -> tuple[int, list[dict[str, object]]]:
        request = Request(
            f"{self.base_url}/api/rooms/room_plan/messages/stream",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
            },
            data=json.dumps(payload).encode("utf-8"),
        )
        try:
            with urlopen(request, timeout=10) as response:
                events = [
                    json.loads(line)
                    for line in response.read().decode("utf-8").splitlines()
                    if line.strip()
                ]
                return response.status, events
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, [json.loads(exc.read().decode("utf-8"))]
            finally:
                exc.close()

    def post_resume(self, request_id: str) -> tuple[int, list[dict[str, object]]]:
        request = Request(
            f"{self.base_url}/api/rooms/room_plan/chat-requests/{request_id}/resume/stream",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
            },
            data=b"{}",
        )
        with urlopen(request, timeout=10) as response:
            events = [
                json.loads(line)
                for line in response.read().decode("utf-8").splitlines()
                if line.strip()
            ]
            return response.status, events

    @staticmethod
    def mention(member: dict[str, object]) -> dict[str, object]:
        return {
            "member_id": member["id"],
            "expected_member_version": member["version"],
        }

    def test_idle_stream_persists_user_and_only_selected_ai_reply(self) -> None:
        member = self.store.enabled_members("room_plan")[1]

        status, events = self.post_stream({
            "content": f"@{member['name']} 只请你回答",
            "mentions": [self.mention(member)],
            "client_message_id": "http-mention-idle-1",
            "skip_providers": [],
        })

        self.assertEqual(status, 200)
        self.assertEqual([event["type"] for event in events], [
            "user_message", "speaker_started", "message", "chat_request_completed",
        ])
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(events[2]["message"]["sender_id"], member["id"])
        self.assertEqual(
            events[2]["message"]["reply_to_message_id"],
            events[0]["message"]["id"],
        )

    def test_idle_auto_stream_routes_one_unmentioned_reply_when_opted_in(self) -> None:
        room = self.store.room_snapshot("room_plan")["room"]
        self.store.update_room(
            "room_plan",
            {
                "idle_response_mode": "moderator_auto",
                "expected_settings_version": room["settings_version"],
            },
        )

        status, events = self.post_stream({
            "content": "请主持人选择一位最合适的成员回答",
            "mentions": [],
            "client_message_id": "http-idle-auto-1",
            "skip_providers": [],
        })

        self.assertEqual(status, 200)
        self.assertEqual([event["type"] for event in events], [
            "user_message", "speaker_started", "message", "chat_request_completed",
        ])
        self.assertEqual(events[0]["routing"]["mode"], "idle_auto")
        self.assertEqual(len(events[0]["routing"]["target_member_ids"]), 1)
        self.assertEqual(len(self.provider.calls), 1)

    def test_stale_member_version_returns_conflict_without_side_effects(self) -> None:
        member = self.store.enabled_members("room_plan")[0]
        before = len(self.store.room_snapshot("room_plan")["messages"])

        status, events = self.post_stream({
            "content": "旧身份不能被静默替换",
            "mentions": [{
                "member_id": member["id"],
                "expected_member_version": int(member["version"]) - 1,
            }],
            "client_message_id": "http-mention-stale-1",
            "skip_providers": [],
        })

        self.assertEqual(status, 409)
        self.assertIn("身份配置已变化", events[0]["error"])
        self.assertEqual(len(self.store.room_snapshot("room_plan")["messages"]), before)
        self.assertEqual(self.provider.calls, [])

    def test_running_round_message_is_queued_and_never_starts_idle_provider_call(self) -> None:
        members = self.store.enabled_members("room_plan")
        round_row = self.store.create_round("room_plan", "正在运行")
        self.store.save_round_checkpoint("room_plan", round_row["id"], {
            "member_ids": [member["id"] for member in members],
            "spoken_counts": {},
            "failed_member_ids": [],
            "next_order": 1,
            "max_turns": len(members),
        })
        target = members[-1]

        status, events = self.post_stream({
            "content": f"@{target['name']} 在下一节点回应",
            "mentions": [self.mention(target)],
            "expected_round_id": round_row["id"],
            "client_message_id": "http-mention-round-1",
            "skip_providers": [],
        })

        self.assertEqual(status, 200)
        self.assertEqual([event["type"] for event in events], ["user_message", "interjection_queued"])
        self.assertEqual(events[0]["message"]["round_id"], round_row["id"])
        self.assertEqual(events[0]["routing"]["mode"], "round_interjection")
        self.assertEqual(self.provider.calls, [])
        self.store.complete_round(round_row["id"], "CANCELLED")

    def test_recovered_idle_request_resumes_with_persisted_provider_skip_policy(self) -> None:
        member = self.store.enabled_members("room_plan")[0]
        created = self.store.create_user_message_request(
            "room_plan",
            content="服务恢复后也必须继续跳过 OpenAI",
            mentions=[self.mention(member)],
            client_message_id="http-mention-resume-skip-1",
            skip_provider_ids={"openai"},
        )
        request_id = created["routing"]["request_id"]
        self.assertTrue(self.store.claim_chat_target(
            "room_plan",
            request_id,
            str(member["id"]),
            lease_owner="old-http-server",
        ))
        with DatabaseInstanceOwner(self.store.path) as owner:
            self.store.recover_orphaned_work(instance_owner=owner)

        status, events = self.post_resume(request_id)

        self.assertEqual(status, 200)
        self.assertEqual(events[0]["type"], "chat_request_resumed")
        self.assertEqual(events[-1]["type"], "chat_request_completed")
        self.assertEqual(events[-1]["status"], "FAILED")
        self.assertTrue(any(event.get("error_code") == "provider_skipped" for event in events))
        self.assertEqual(self.provider.calls, [])
        request = self.store.get_chat_request("room_plan", request_id)
        self.assertEqual(request["skip_provider_ids"], ["openai"])
        self.assertEqual(request["targets"][0]["status"], "FAILED")

    def test_message_stream_requires_local_session_token_before_any_write(self) -> None:
        before = len(self.store.room_snapshot("room_plan")["messages"])
        request = Request(
            f"{self.base_url}/api/rooms/room_plan/messages/stream",
            method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps({
                "content": "没有会话令牌不能写入",
                "mentions": [],
                "client_message_id": "http-mention-guard-1",
            }).encode("utf-8"),
        )

        with self.assertRaises(urllib.error.HTTPError) as raised:
            urlopen(request, timeout=5)

        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()
        self.assertEqual(len(self.store.room_snapshot("room_plan")["messages"]), before)


if __name__ == "__main__":
    unittest.main()
