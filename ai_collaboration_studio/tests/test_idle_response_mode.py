from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.orchestrator import DiscussionOrchestrator
from backend.store import MessageRoutingConflict, StudioStore
from tests.test_orchestrator import FakeProvider, FakeRegistry


class IdleResponseModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "idle-response.sqlite3")
        self.provider = FakeProvider()
        self.orchestrator = DiscussionOrchestrator(
            self.store,
            FakeRegistry(self.provider),
            market_service=None,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def set_mode(self, mode: str) -> dict[str, object]:
        room = self.store.room_snapshot("room_plan")["room"]
        updated = self.store.update_room(
            "room_plan",
            {
                "idle_response_mode": mode,
                "expected_settings_version": room["settings_version"],
            },
        )
        self.assertIsNotNone(updated)
        return updated or {}

    @staticmethod
    def mention(member: dict[str, object]) -> dict[str, object]:
        return {
            "member_id": member["id"],
            "expected_member_version": member["version"],
        }

    def test_default_only_routes_structured_mentions(self) -> None:
        room = self.store.room_snapshot("room_plan")["room"]
        self.assertEqual(room["idle_response_mode"], "mention_only")
        plain = self.store.create_user_message_request(
            "room_plan",
            content="普通消息只保存",
            mentions=[],
            client_message_id="idle-default-plain-1",
        )
        self.assertEqual(plain["routing"]["mode"], "stored_only")
        self.assertEqual(len(self.provider.calls), 0)

        member = self.store.enabled_members("room_plan")[0]
        targeted = self.store.create_user_message_request(
            "room_plan",
            content="请被点名成员回答",
            mentions=[self.mention(member)],
            client_message_id="idle-default-mention-1",
        )
        self.assertEqual(targeted["routing"]["mode"], "idle_targeted")

    def test_stored_only_preserves_mention_metadata_without_a_request(self) -> None:
        self.set_mode("stored_only")
        member = self.store.enabled_members("room_plan")[0]
        result = self.store.create_user_message_request(
            "room_plan",
            content="这条点名仅作为记录",
            mentions=[self.mention(member)],
            client_message_id="idle-stored-mention-1",
        )

        self.assertEqual(result["routing"]["mode"], "stored_only")
        self.assertEqual(result["message"]["mentions"][0]["member_id"], member["id"])
        self.assertEqual(len(self.provider.calls), 0)

    def test_moderator_auto_freezes_one_relevant_member_and_is_idempotent(self) -> None:
        members = self.store.enabled_members("room_plan")
        target = self.store.update_member(
            "room_plan",
            members[1]["id"],
            {
                "identity": "技术分析师",
                "responsibilities": "图表与趋势研究",
                "provider": "deepseek",
                "model": "fake-model",
            },
        )
        self.assertIsNotNone(target)
        self.set_mode("moderator_auto")

        payload = {
            "content": "请技术分析师查看图表与趋势",
            "mentions": [],
            "client_message_id": "idle-auto-relevant-1",
            "skip_provider_ids": {"openai"},
        }
        result = self.store.create_user_message_request("room_plan", **payload)
        replay = self.store.create_user_message_request("room_plan", **payload)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(result["message"]["id"], replay["message"]["id"])
        self.assertEqual(result["routing"]["mode"], "idle_auto")
        self.assertEqual(result["routing"]["target_member_ids"], [target["id"]])

        request_id = result["routing"]["request_id"]
        frozen = self.store.get_chat_request("room_plan", request_id)
        self.assertEqual(len(frozen["targets"]), 1)
        self.assertEqual(frozen["targets"][0]["member_id"], target["id"])
        self.assertEqual(frozen["targets"][0]["member_version"], target["version"])

        events = list(self.orchestrator.run_idle_chat_request(
            "room_plan",
            request_id,
            skip_provider_ids={"openai"},
        ))
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(len([event for event in events if event["type"] == "message"]), 1)
        self.assertEqual(events[-1]["type"], "chat_request_completed")

        list(self.orchestrator.run_idle_chat_request(
            "room_plan",
            request_id,
            skip_provider_ids={"openai"},
        ))
        self.assertEqual(len(self.provider.calls), 1)

    def test_mode_is_versioned_and_part_of_routing_fingerprint(self) -> None:
        before = self.store.room_snapshot("room_plan")["room"]
        after = self.set_mode("moderator_auto")
        self.assertEqual(after["settings_version"], before["settings_version"] + 1)
        version_history = self.store.list_room_versions("room_plan")
        self.assertEqual(
            version_history["versions"][0]["version"],
            after["settings_version"],
        )
        latest = self.store.get_room_version_record(
            "room_plan",
            after["settings_version"],
        )
        self.assertEqual(
            latest["room_version"]["snapshot"]["idle_response_mode"],
            "moderator_auto",
        )

        payload = {
            "content": "相同消息 ID 绑定发送时的自动响应策略",
            "mentions": [],
            "client_message_id": "idle-mode-fingerprint-1",
        }
        self.store.create_user_message_request("room_plan", **payload)
        self.set_mode("stored_only")
        with self.assertRaisesRegex(MessageRoutingConflict, "路由策略"):
            self.store.create_user_message_request("room_plan", **payload)

    def test_invalid_mode_fails_without_mutating_room(self) -> None:
        before = self.store.room_snapshot("room_plan")["room"]
        with self.assertRaisesRegex(ValueError, "idle_response_mode"):
            self.store.update_room(
                "room_plan",
                {
                    "idle_response_mode": "always-spend",
                    "expected_settings_version": before["settings_version"],
                },
            )
        after = self.store.room_snapshot("room_plan")["room"]
        self.assertEqual(after["settings_version"], before["settings_version"])
        self.assertEqual(after["idle_response_mode"], before["idle_response_mode"])

    def test_auto_mode_with_no_enabled_member_fails_closed_without_pending_work(self) -> None:
        for member in self.store.enabled_members("room_plan"):
            self.store.update_member(
                "room_plan",
                member["id"],
                {"enabled": False},
            )
        self.set_mode("moderator_auto")

        result = self.store.create_user_message_request(
            "room_plan",
            content="没有成员时不能留下永久待处理请求",
            mentions=[],
            client_message_id="idle-auto-no-member-1",
        )

        self.assertEqual(result["routing"]["mode"], "stored_only")
        self.assertEqual(result["routing"]["status"], "FAILED")
        self.assertEqual(result["routing"]["error_code"], "NO_ENABLED_MEMBER")
        self.assertEqual(self.store.room_snapshot("room_plan")["pending_chat_requests"], [])
        self.assertEqual(len(self.provider.calls), 0)


if __name__ == "__main__":
    unittest.main()
