from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from backend.orchestrator import DiscussionOrchestrator
from backend.instance_ownership import DatabaseInstanceOwner
from backend.providers.base import ProviderResponse
from backend.store import MessageRoutingConflict, StudioStore
from tests.test_orchestrator import CountingProvider, FakeProvider, FakeRegistry


class SelectiveMentionProvider(FakeProvider):
    def __init__(self, failing_name: str) -> None:
        super().__init__()
        self.failing_name = failing_name

    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        if f"「{self.failing_name}」" in instructions:
            return ProviderResponse(
                ok=False,
                provider="untrusted-provider-name",
                model="untrusted-model-name",
                error="Bearer upstream-secret must never be stored",
                error_code="http_status",
            )
        return super().generate(instructions=instructions, input_text=input_text, model=model)


def saved_checkpoint_state(
    store: StudioStore,
    room_id: str,
    round_id: str,
    *,
    next_order: int = 1,
    consecutive_interjections: int = 0,
) -> dict[str, object]:
    members = store.enabled_members(room_id)
    saved = store.save_round_checkpoint(
        room_id,
        round_id,
        {
            "member_ids": [str(member["id"]) for member in members],
            "next_order": next_order,
            "max_turns": max(1, len(members)),
            "consecutive_interjections": consecutive_interjections,
        },
    )
    return saved["state"]


class RecordingProviderCallLedger:
    def __init__(self) -> None:
        self.snapshot_calls = 0
        self.reserve_calls: list[dict[str, object]] = []
        self.finish_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def snapshot(self) -> dict[str, object]:
        self.snapshot_calls += 1
        return {"remaining_calls": 100}

    def reserve(self, **kwargs: object) -> dict[str, object]:
        self.reserve_calls.append(dict(kwargs))
        return {"id": "provider_attempt_test", "attempt_token": "attempt_token_test"}

    def finish(self, *args: object, **kwargs: object) -> dict[str, object]:
        self.finish_calls.append((args, dict(kwargs)))
        return {"status": str(kwargs.get("status") or "")}


class StructuredMentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "mentions.sqlite3")
        for member in self.store.enabled_members("room_plan"):
            self.store.update_member(
                "room_plan",
                member["id"],
                {"provider": "openai", "model": "fake-model"},
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def mention(self, member: dict[str, object]) -> dict[str, object]:
        return {
            "member_id": member["id"],
            "expected_member_version": member["version"],
        }

    def test_validation_is_atomic_version_bound_and_idempotent(self) -> None:
        members = self.store.enabled_members("room_plan")
        cross_room_member = self.store.enabled_members("room_storage")[0]
        before = len(self.store.room_snapshot("room_plan")["messages"])

        with self.assertRaises(MessageRoutingConflict):
            self.store.create_user_message_request(
                "room_plan",
                content="跨房间点名必须失败",
                mentions=[self.mention(cross_room_member)],
                client_message_id="mention-cross-room-1",
            )
        with self.assertRaises(MessageRoutingConflict):
            self.store.create_user_message_request(
                "room_plan",
                content="旧身份版本必须失败",
                mentions=[{
                    "member_id": members[0]["id"],
                    "expected_member_version": int(members[0]["version"]) - 1,
                }],
                client_message_id="mention-stale-version-1",
            )
        self.assertEqual(len(self.store.room_snapshot("room_plan")["messages"]), before)

        result = self.store.create_user_message_request(
            "room_plan",
            content=f"@{members[0]['name']} 请回应",
            mentions=[self.mention(members[0]), self.mention(members[0])],
            client_message_id="mention-idempotent-1",
        )
        replay = self.store.create_user_message_request(
            "room_plan",
            content=f"@{members[0]['name']} 请回应",
            mentions=[self.mention(members[0])],
            client_message_id="mention-idempotent-1",
        )

        self.assertEqual(result["message"]["id"], replay["message"]["id"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(len(result["message"]["mentions"]), 1)
        self.assertEqual(result["routing"]["mode"], "idle_targeted")
        self.assertEqual(len(self.store.room_snapshot("room_plan")["messages"]), before + 1)

    def test_plain_text_at_name_without_structured_mention_never_routes(self) -> None:
        member = self.store.enabled_members("room_plan")[0]
        result = self.store.create_user_message_request(
            "room_plan",
            content=f"纯文本 @{member['name']} 只用于显示",
            mentions=[],
            client_message_id="mention-plain-text-1",
        )

        self.assertEqual(result["routing"]["mode"], "stored_only")
        self.assertEqual(result["message"]["mentions"], [])

    def test_idempotency_key_conflicts_when_target_or_execution_policy_changes(self) -> None:
        members = self.store.enabled_members("room_plan")[:2]
        payload = {
            "content": "相同正文也必须绑定相同路由",
            "mentions": [self.mention(members[0])],
            "client_message_id": "mention-routing-fingerprint-1",
            "skip_provider_ids": {"openai"},
        }
        first = self.store.create_user_message_request("room_plan", **payload)
        replay = self.store.create_user_message_request("room_plan", **payload)
        self.assertEqual(first["message"]["id"], replay["message"]["id"])
        with self.assertRaisesRegex(MessageRoutingConflict, "路由策略"):
            self.store.create_user_message_request(
                "room_plan",
                **{**payload, "mentions": [self.mention(members[1])]},
            )
        with self.assertRaisesRegex(MessageRoutingConflict, "路由策略"):
            self.store.create_user_message_request(
                "room_plan",
                **{**payload, "skip_provider_ids": set()},
            )

    def test_legacy_nonterminal_request_without_provider_policy_is_cancelled(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy-skip-policy.sqlite3"
        legacy_store = StudioStore(legacy_path)
        member = legacy_store.enabled_members("room_plan")[0]
        created = legacy_store.create_user_message_request(
            "room_plan",
            content="旧版未记录 Provider 策略的请求不得自动恢复",
            mentions=[{
                "member_id": member["id"],
                "expected_member_version": member["version"],
            }],
            client_message_id="legacy-provider-policy-1",
        )
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.execute("ALTER TABLE chat_requests DROP COLUMN skip_providers_json")
            connection.commit()

        migrated = StudioStore(legacy_path)
        request = migrated.get_chat_request(
            "room_plan",
            created["routing"]["request_id"],
        )

        self.assertEqual(request["status"], "CANCELLED")
        self.assertEqual(request["targets"][0]["status"], "CANCELLED")
        self.assertEqual(
            request["targets"][0]["error_code"],
            "provider_policy_unknown",
        )

    def test_idle_close_after_claim_releases_owned_target_and_retry_completes(self) -> None:
        member = self.store.enabled_members("room_plan")[0]
        provider = FakeProvider()
        orchestrator = DiscussionOrchestrator(
            self.store,
            FakeRegistry(provider),
            market_service=None,
        )
        result = self.store.create_user_message_request(
            "room_plan",
            content="在 speaker_started 后模拟浏览器断开",
            mentions=[self.mention(member)],
            client_message_id="mention-close-release-1",
        )
        request_id = result["routing"]["request_id"]
        stream = orchestrator.run_idle_chat_request("room_plan", request_id, skip_provider_ids=set())
        self.assertEqual(next(stream)["type"], "speaker_started")
        stream.close()

        released = self.store.get_chat_request("room_plan", request_id)
        self.assertEqual(released["status"], "PENDING")
        self.assertEqual(released["targets"][0]["status"], "PENDING")
        self.assertEqual(provider.calls, [])

        events = list(orchestrator.run_idle_chat_request("room_plan", request_id, skip_provider_ids=set()))
        self.assertEqual(events[-1]["type"], "chat_request_completed")
        self.assertEqual(self.store.get_chat_request("room_plan", request_id)["status"], "COMPLETED")
        self.assertEqual(len(provider.calls), 1)
        with closing(sqlite3.connect(self.store.path)) as connection:
            statuses = [
                row[0]
                for row in connection.execute(
                    "SELECT status FROM chat_request_attempts WHERE request_id=? ORDER BY attempt_no",
                    (request_id,),
                ).fetchall()
            ]
        self.assertEqual(statuses, ["ABANDONED", "RESPONDED"])

    def test_idle_runner_cannot_weaken_persisted_provider_skip(self) -> None:
        member = self.store.enabled_members("room_plan")[0]
        provider = FakeProvider()
        orchestrator = DiscussionOrchestrator(
            self.store,
            FakeRegistry(provider),
            market_service=None,
        )
        result = self.store.create_user_message_request(
            "room_plan",
            content="该请求已冻结为不得调用 OpenAI",
            mentions=[self.mention(member)],
            client_message_id="mention-persisted-skip-runner-1",
            skip_provider_ids={"openai"},
        )
        request_id = result["routing"]["request_id"]

        events = list(orchestrator.run_idle_chat_request(
            "room_plan",
            request_id,
            skip_provider_ids=set(),
        ))
        failure = next(event for event in events if event["type"] == "speaker_failed")
        request = self.store.get_chat_request("room_plan", request_id)

        self.assertEqual(provider.calls, [])
        self.assertEqual(failure["error_code"], "provider_skipped")
        self.assertEqual(request["skip_provider_ids"], ["openai"])
        self.assertEqual(request["targets"][0]["status"], "FAILED")
        self.assertEqual(request["status"], "FAILED")

    def test_expired_claim_is_reclaimed_and_stale_worker_is_fenced(self) -> None:
        member = self.store.enabled_members("room_plan")[0]
        result = self.store.create_user_message_request(
            "room_plan",
            content="租约过期后只允许新持有者落库",
            mentions=[self.mention(member)],
            client_message_id="mention-claim-fence-1",
        )
        request_id = result["routing"]["request_id"]
        token_a = self.store.claim_chat_target(
            "room_plan", request_id, member["id"], lease_owner="worker-a", lease_ms=1_000, at_ms=1_000,
        )
        self.assertTrue(token_a)
        self.assertFalse(self.store.claim_chat_target(
            "room_plan", request_id, member["id"], lease_owner="worker-b", lease_ms=1_000, at_ms=1_999,
        ))
        token_b = self.store.claim_chat_target(
            "room_plan", request_id, member["id"], lease_owner="worker-b", lease_ms=1_000, at_ms=2_001,
        )
        self.assertTrue(token_b)
        self.assertNotEqual(token_a, token_b)
        before = len(self.store.room_snapshot("room_plan")["messages"])
        with self.assertRaisesRegex(ValueError, "租约已失效"):
            self.store.add_message(
                "room_plan",
                sender_type="ai",
                sender_id=member["id"],
                sender_name=member["name"],
                content="旧 worker 不得落库",
                reply_to_message_id=result["message"]["id"],
                chat_request_id=request_id,
                chat_target_member_id=member["id"],
                chat_target_status="RESPONDED",
                chat_claim_token=token_a,
            )
        self.assertEqual(len(self.store.room_snapshot("room_plan")["messages"]), before)
        response = self.store.add_message(
            "room_plan",
            sender_type="ai",
            sender_id=member["id"],
            sender_name=member["name"],
            content="新 worker 的唯一持久化回复",
            reply_to_message_id=result["message"]["id"],
            chat_request_id=request_id,
            chat_target_member_id=member["id"],
            chat_target_status="RESPONDED",
            chat_claim_token=token_b,
        )
        request = self.store.get_chat_request("room_plan", request_id)
        self.assertEqual(request["status"], "COMPLETED")
        self.assertEqual(request["targets"][0]["response_message_id"], response["id"])
        self.assertEqual(len(request["responses"]), 1)

    def test_boot_recovery_pauses_running_round_and_requeues_claim(self) -> None:
        member = self.store.enabled_members("room_plan")[0]
        round_row = self.store.create_round("room_plan", "模拟服务进程中断")
        room = self.store.room_snapshot("room_plan")["room"]
        self.store.save_round_checkpoint("room_plan", round_row["id"], {
            "member_ids": [member["id"]],
            "workflow_policy": room["workflow_policy"],
            "capability_pack_ids": room["capability_pack_ids"],
            "next_order": 1,
            "max_turns": 1,
        })
        result = self.store.create_user_message_request(
            "room_plan",
            content="这条轮次插话必须在重启后保留",
            mentions=[self.mention(member)],
            expected_round_id=round_row["id"],
            client_message_id="mention-boot-recovery-1",
        )
        request_id = result["routing"]["request_id"]
        self.assertTrue(self.store.claim_chat_target(
            "room_plan", request_id, member["id"], lease_owner="old-server",
        ))

        with DatabaseInstanceOwner(self.store.path) as owner:
            recovery = self.store.recover_orphaned_work(instance_owner=owner)
        request = self.store.get_chat_request("room_plan", request_id)
        snapshot = self.store.room_snapshot("room_plan")
        self.assertEqual(recovery, {
            "recovered_chat_targets": 1,
            "paused_rounds": 1,
            "cancelled_rounds": 0,
        })
        self.assertEqual(request["status"], "PENDING")
        self.assertEqual(request["targets"][0]["status"], "PENDING")
        self.assertEqual(snapshot["latest_round"]["status"], "PAUSED")
        self.assertEqual(snapshot["pending_chat_requests"][0]["id"], request_id)

    def test_idle_multi_mention_calls_only_targets_in_order_and_chains_context(self) -> None:
        members = self.store.enabled_members("room_plan")[:2]
        provider = FakeProvider()
        orchestrator = DiscussionOrchestrator(
            self.store,
            FakeRegistry(provider),
            market_service=None,
        )
        result = self.store.create_user_message_request(
            "room_plan",
            content="请两位按点名顺序独立回应",
            mentions=[self.mention(member) for member in members],
            client_message_id="mention-idle-order-1",
        )

        events = list(orchestrator.run_idle_chat_request(
            "room_plan",
            result["routing"]["request_id"],
            skip_provider_ids=set(),
        ))
        messages = [event["message"] for event in events if event["type"] == "message"]
        request = self.store.get_chat_request("room_plan", result["routing"]["request_id"])

        self.assertEqual(len(provider.calls), 2)
        self.assertEqual([message["sender_id"] for message in messages], [member["id"] for member in members])
        self.assertIn("第 1 位成员发言", provider.calls[1]["input_text"])
        self.assertTrue(all(message["reply_to_message_id"] == result["message"]["id"] for message in messages))
        self.assertEqual([message["member_version"] for message in messages], [member["version"] for member in members])
        self.assertEqual(request["status"], "COMPLETED")
        self.assertEqual([target["status"] for target in request["targets"]], ["RESPONDED", "RESPONDED"])
        self.assertEqual(events[-1]["type"], "chat_request_completed")

    def test_idle_failure_is_isolated_and_upstream_metadata_is_not_trusted(self) -> None:
        members = self.store.enabled_members("room_plan")[:2]
        provider = SelectiveMentionProvider(str(members[0]["name"]))
        orchestrator = DiscussionOrchestrator(
            self.store,
            FakeRegistry(provider),
            market_service=None,
        )
        result = self.store.create_user_message_request(
            "room_plan",
            content="第一位失败时第二位仍应继续",
            mentions=[self.mention(member) for member in members],
            client_message_id="mention-failure-isolation-1",
        )

        events = list(orchestrator.run_idle_chat_request(
            "room_plan",
            result["routing"]["request_id"],
            skip_provider_ids=set(),
        ))
        request = self.store.get_chat_request("room_plan", result["routing"]["request_id"])
        serialized = json.dumps(self.store.room_snapshot("room_plan"), ensure_ascii=False).lower()

        self.assertEqual([target["status"] for target in request["targets"]], ["FAILED", "RESPONDED"])
        self.assertEqual(request["status"], "PARTIAL")
        self.assertEqual(len([event for event in events if event["type"] == "speaker_failed"]), 1)
        self.assertEqual(len([event for event in events if event["type"] == "message"]), 1)
        self.assertNotIn("upstream-secret", serialized)
        self.assertNotIn("untrusted-provider-name", serialized)
        self.assertNotIn("untrusted-model-name", serialized)

    def test_running_round_interjection_prioritizes_structured_target(self) -> None:
        members = self.store.enabled_members("room_plan")
        provider = FakeProvider()
        orchestrator = DiscussionOrchestrator(
            self.store,
            FakeRegistry(provider),
            market_service=None,
        )
        stream = orchestrator.run_round("room_plan", "先开始动态讨论")
        round_id = ""
        for event in stream:
            if event["type"] == "round_started":
                round_id = event["round"]["id"]
                break
        self.assertTrue(round_id)
        checkpoint_before_interjection = self.store.get_round_checkpoint(
            "room_plan", round_id,
        )["state"]
        target = members[-1]
        result = self.store.create_user_message_request(
            "room_plan",
            content=f"@{target['name']} 请先回应这条插话",
            mentions=[self.mention(target)],
            expected_round_id=round_id,
            client_message_id="mention-active-round-1",
        )

        interjection_prefix = []
        interjection_message = None
        for event in stream:
            interjection_prefix.append(event)
            if (
                event["type"] == "message"
                and event.get("chat_request_id") == result["routing"]["request_id"]
            ):
                interjection_message = event["message"]
                break
        self.assertIsNotNone(interjection_message)
        checkpoint_after_interjection = self.store.get_round_checkpoint(
            "room_plan", round_id,
        )["state"]
        remaining = [*interjection_prefix, *list(stream)]
        decisions = [
            event for event in remaining
            if event["type"] == "director_decision" and event["action"] == "speak"
        ]
        responses = [
            event["message"] for event in remaining
            if event["type"] == "message" and event.get("chat_request_id") == result["routing"]["request_id"]
        ]
        request = self.store.get_chat_request("room_plan", result["routing"]["request_id"])

        self.assertEqual(result["routing"]["mode"], "round_interjection")
        self.assertEqual(decisions[0]["source"], "user_mention")
        self.assertEqual(decisions[0]["member"]["id"], target["id"])
        self.assertEqual(responses[0]["sender_id"], target["id"])
        self.assertEqual(responses[0]["reply_to_message_id"], result["message"]["id"])
        self.assertFalse(responses[0]["is_formal_round_turn"])
        self.assertIsNone(responses[0]["turn_contract_version"])
        self.assertIsNone(responses[0]["turn_contract"])
        for field in (
            "next_order",
            "completed",
            "failures",
            "skipped",
            "spoken_counts",
            "spoken_stances",
            "successful_member_ids",
            "failed_member_ids",
        ):
            self.assertEqual(
                checkpoint_after_interjection[field],
                checkpoint_before_interjection[field],
            )
        self.assertEqual(request["status"], "COMPLETED")
        self.assertEqual(self.store.room_snapshot("room_plan")["latest_round"]["id"], round_id)
        self.assertEqual(
            self.store.get_round("room_plan", round_id)["status"],
            "COMPLETED",
        )
        bundle = self.store.round_turn_contract_bundle("room_plan", round_id)
        self.assertTrue(bundle["valid"], bundle["issues"])
        self.assertNotIn(
            responses[0]["id"],
            {message["id"] for message in bundle["messages"]},
        )

    def test_interjection_terminal_and_fairness_checkpoint_commit_atomically(self) -> None:
        target = self.store.enabled_members("room_plan")[-1]
        stream = DiscussionOrchestrator(
            self.store,
            FakeRegistry(FakeProvider()),
            market_service=None,
        ).run_round("room_plan", "插话终态与公平检查点必须原子提交")
        round_id = next(stream)["round"]["id"]
        request = self.store.create_user_message_request(
            "room_plan",
            content=f"@{target['name']} 原子提交测试",
            mentions=[self.mention(target)],
            expected_round_id=round_id,
            client_message_id="mention-atomic-fairness-checkpoint",
        )
        request_id = request["routing"]["request_id"]
        claim_token = self.store.claim_chat_target(
            "room_plan",
            request_id,
            target["id"],
            lease_owner="atomic-test",
        )
        self.assertTrue(claim_token)
        original_checkpoint = self.store.get_round_checkpoint(
            "room_plan", round_id,
        )["state"]
        terminal_checkpoint = {
            **original_checkpoint,
            "consecutive_interjections": 1,
        }
        message_count = len(self.store.round_messages("room_plan", round_id))

        with self.assertRaises(ValueError):
            self.store.add_message(
                "room_plan",
                sender_type="ai",
                sender_id=target["id"],
                sender_name=target["name"],
                content="错误租约不得留下半个终态",
                round_id=round_id,
                chat_request_id=request_id,
                chat_target_member_id=target["id"],
                chat_target_status="RESPONDED",
                chat_claim_token="wrong-claim-token",
                round_checkpoint_state=terminal_checkpoint,
            )
        self.assertEqual(
            len(self.store.round_messages("room_plan", round_id)),
            message_count,
        )
        self.assertEqual(
            self.store.get_round_checkpoint("room_plan", round_id)["state"][
                "consecutive_interjections"
            ],
            0,
        )
        self.assertEqual(
            self.store.get_chat_request("room_plan", request_id)["targets"][0][
                "status"
            ],
            "PROCESSING",
        )

        self.store.add_message(
            "room_plan",
            sender_type="ai",
            sender_id=target["id"],
            sender_name=target["name"],
            content="正确租约同时提交消息、目标终态和公平检查点",
            round_id=round_id,
            chat_request_id=request_id,
            chat_target_member_id=target["id"],
            chat_target_status="RESPONDED",
            chat_claim_token=str(claim_token),
            round_checkpoint_state=terminal_checkpoint,
        )
        self.assertEqual(
            self.store.get_chat_request("room_plan", request_id)["status"],
            "COMPLETED",
        )
        self.assertEqual(
            self.store.get_round_checkpoint("room_plan", round_id)["state"][
                "consecutive_interjections"
            ],
            1,
        )
        stream.close()

    def test_chat_target_terminal_rejects_cross_round_and_cross_room_scope(self) -> None:
        members = self.store.enabled_members("room_plan")
        target = members[0]
        source_round = self.store.create_round("room_plan", "请求来源轮次")
        saved_checkpoint_state(
            self.store,
            "room_plan",
            source_round["id"],
        )
        created = self.store.create_user_message_request(
            "room_plan",
            content=f"@{target['name']} 作用域必须保持一致",
            mentions=[self.mention(target)],
            expected_round_id=source_round["id"],
            client_message_id="mention-terminal-scope",
        )
        request_id = created["routing"]["request_id"]
        claim_token = self.store.claim_chat_target(
            "room_plan",
            request_id,
            target["id"],
            lease_owner="scope-test",
        )
        self.assertTrue(claim_token)

        self.store.complete_round(source_round["id"], "CANCELLED")
        other_round = self.store.create_round("room_plan", "同房间另一轮")
        other_round_state = saved_checkpoint_state(
            self.store,
            "room_plan",
            other_round["id"],
        )
        other_round_message_count = len(
            self.store.round_messages("room_plan", other_round["id"])
        )
        with self.assertRaisesRegex(ValueError, "不属于当前房间和轮次"):
            self.store.add_message(
                "room_plan",
                sender_type="ai",
                sender_id=target["id"],
                sender_name=target["name"],
                content="跨轮终态不得写入",
                round_id=other_round["id"],
                chat_request_id=request_id,
                chat_target_member_id=target["id"],
                chat_target_status="RESPONDED",
                chat_claim_token=str(claim_token),
                round_checkpoint_state={
                    **other_round_state,
                    "consecutive_interjections": 1,
                },
            )
        self.assertEqual(
            len(self.store.round_messages("room_plan", other_round["id"])),
            other_round_message_count,
        )
        self.assertEqual(
            self.store.get_round_checkpoint(
                "room_plan", other_round["id"]
            )["state"]["consecutive_interjections"],
            0,
        )

        other_room_snapshot = self.store.create_room(
            "作用域测试房间",
            "跨房间请求不得终结",
        )
        other_room_id = str(other_room_snapshot["room"]["id"])
        cross_room_round = self.store.create_round(other_room_id, "另一房间轮次")
        cross_room_state = saved_checkpoint_state(
            self.store,
            other_room_id,
            cross_room_round["id"],
        )
        with self.assertRaisesRegex(ValueError, "不属于当前房间和轮次"):
            self.store.add_message(
                other_room_id,
                sender_type="ai",
                sender_id=target["id"],
                sender_name=target["name"],
                content="跨房间终态不得写入",
                round_id=cross_room_round["id"],
                chat_request_id=request_id,
                chat_target_member_id=target["id"],
                chat_target_status="RESPONDED",
                chat_claim_token=str(claim_token),
                round_checkpoint_state={
                    **cross_room_state,
                    "consecutive_interjections": 1,
                },
            )

        persisted_request = self.store.get_chat_request("room_plan", request_id)
        self.assertEqual(persisted_request["status"], "PROCESSING")
        self.assertEqual(persisted_request["targets"][0]["status"], "PROCESSING")
        self.assertEqual(
            self.store.get_round_checkpoint(
                other_room_id, cross_room_round["id"]
            )["state"]["consecutive_interjections"],
            0,
        )
        self.assertFalse(any(
            message["content"] == "跨房间终态不得写入"
            for message in self.store.round_messages(
                other_room_id, cross_room_round["id"]
            )
        ))

    def test_legacy_terminal_turn_checkpoint_verifies_raw_seal_then_normalizes(self) -> None:
        members = self.store.enabled_members("room_plan")
        member = members[0]
        round_row = self.store.create_round("room_plan", "旧终态检查点恢复")
        base_state = saved_checkpoint_state(
            self.store,
            "room_plan",
            round_row["id"],
            next_order=2,
        )
        turn = self.store.begin_round_turn(
            "room_plan",
            round_row["id"],
            1,
            member,
        )
        self.store.add_message(
            "room_plan",
            sender_type="system",
            sender_id=member["id"],
            sender_name="系统",
            content="模拟旧版本已终结发言",
            round_id=round_row["id"],
            round_turn_id=turn["id"],
            round_turn_status="FAILED",
            round_checkpoint_state=base_state,
        )

        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            raw_state = json.loads(connection.execute(
                "SELECT checkpoint_state_json FROM round_turns WHERE id=?",
                (turn["id"],),
            ).fetchone()[0])
            raw_state.pop("consecutive_interjections", None)
            raw_seal = self.store._round_checkpoint_state_sha256(
                room_id="room_plan",
                round_id=round_row["id"],
                step_number=int(raw_state["next_order"]) - 1,
                state=raw_state,
            )
            connection.execute(
                """UPDATE round_turns
                      SET checkpoint_state_json=?,checkpoint_state_sha256=?
                    WHERE id=?""",
                (json.dumps(raw_state, ensure_ascii=False), raw_seal, turn["id"]),
            )
            connection.execute(
                """UPDATE round_checkpoints
                      SET state_json=?,state_sha256=?
                    WHERE round_id=?""",
                (json.dumps(raw_state, ensure_ascii=False), raw_seal, round_row["id"]),
            )

        legacy_checkpoint = self.store.get_round_checkpoint(
            "room_plan", round_row["id"]
        )
        self.assertEqual(legacy_checkpoint["state"]["consecutive_interjections"], 0)
        restored = self.store.restore_round_turn_checkpoint(
            "room_plan", round_row["id"], 1
        )
        self.assertTrue(restored["checkpoint_integrity_ok"])
        self.assertEqual(restored["checkpoint_state"]["consecutive_interjections"], 0)
        self.assertEqual(
            self.store.get_round_checkpoint(
                "room_plan", round_row["id"]
            )["state"]["consecutive_interjections"],
            0,
        )

        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            tampered_state = dict(raw_state)
            tampered_state["completed"] = int(tampered_state.get("completed") or 0) + 1
            connection.execute(
                "UPDATE round_turns SET checkpoint_state_json=? WHERE id=?",
                (json.dumps(tampered_state, ensure_ascii=False), turn["id"]),
            )
        with self.assertRaisesRegex(ValueError, "完整性校验失败"):
            self.store.restore_round_turn_checkpoint(
                "room_plan", round_row["id"], 1
            )

    def test_active_interjection_uses_frozen_member_version_and_persisted_skip(self) -> None:
        members = self.store.enabled_members("room_plan")
        target = members[-1]
        provider = FakeProvider()
        orchestrator = DiscussionOrchestrator(
            self.store,
            FakeRegistry(provider),
            market_service=None,
        )
        stream = orchestrator.run_round("room_plan", "先开始讨论再验证冻结点名")
        round_id = ""
        for event in stream:
            if event["type"] == "round_started":
                round_id = event["round"]["id"]
            if event["type"] == "message":
                break
        checkpoint_before_interjection = self.store.get_round_checkpoint(
            "room_plan", round_id,
        )["state"]
        result = self.store.create_user_message_request(
            "room_plan",
            content=f"@{target['name']} 请按点名时身份回应",
            mentions=[self.mention(target)],
            expected_round_id=round_id,
            client_message_id="mention-active-frozen-skip-1",
            skip_provider_ids={"openai"},
        )
        current = self.store.get_member("room_plan", target["id"])
        self.store.update_member(
            "room_plan",
            target["id"],
            {
                **current,
                "identity": "点名后才修改的新身份",
                "provider": "deepseek",
                "model": "new-model-must-not-replace-frozen-route",
            },
        )

        failure_prefix = []
        for event in stream:
            failure_prefix.append(event)
            if (
                event["type"] == "speaker_failed"
                and event.get("member", {}).get("id") == target["id"]
                and event.get("error_code") == "provider_skipped"
            ):
                break
        checkpoint_after_interjection = self.store.get_round_checkpoint(
            "room_plan", round_id,
        )["state"]
        provider_calls_after_interjection = list(provider.calls)
        remaining = [*failure_prefix, *list(stream)]
        request = self.store.get_chat_request(
            "room_plan",
            result["routing"]["request_id"],
        )
        target_failures = [
            event
            for event in remaining
            if event["type"] == "speaker_failed"
            and event.get("member", {}).get("id") == target["id"]
        ]

        self.assertEqual(request["status"], "FAILED")
        self.assertEqual(target_failures[0]["error_code"], "provider_skipped")
        self.assertEqual(target_failures[0]["provider"], "openai")
        self.assertEqual(target_failures[0]["member"]["version"], target["version"])
        for field in (
            "next_order",
            "completed",
            "failures",
            "skipped",
            "spoken_counts",
            "spoken_stances",
            "successful_member_ids",
            "failed_member_ids",
        ):
            self.assertEqual(
                checkpoint_after_interjection[field],
                checkpoint_before_interjection[field],
            )
        self.assertFalse(any(
            f"「{target['name']}」" in call["instructions"]
            for call in provider_calls_after_interjection
        ))
        final_round = self.store.get_round("room_plan", round_id)
        self.assertEqual(final_round["status"], "COMPLETED")
        bundle = self.store.round_turn_contract_bundle("room_plan", round_id)
        self.assertTrue(bundle["valid"], bundle["issues"])

    def test_continuous_successful_interjections_have_bounded_wait_and_preserve_fifo(self) -> None:
        members = self.store.enabled_members("room_plan")
        target = members[-1]
        provider = FakeProvider()
        stream = DiscussionOrchestrator(
            self.store,
            FakeRegistry(provider),
            market_service=None,
        ).run_round("room_plan", "连续成功插话必须让正式讨论有界等待")
        started = next(stream)
        round_id = started["round"]["id"]
        requests = []
        for index in range(5):
            request = self.store.create_user_message_request(
                "room_plan",
                content=f"@{target['name']} 插话 {index + 1}",
                mentions=[self.mention(target)],
                expected_round_id=round_id,
                client_message_id=f"mention-bounded-success-{index + 1}",
            )
            requests.append(request)

        terminal_interjections = []
        while len(terminal_interjections) < 2:
            event = next(stream)
            if event["type"] == "message" and event.get("chat_request_id"):
                terminal_interjections.append(event)
        checkpoint_at_limit = self.store.get_round_checkpoint(
            "room_plan", round_id,
        )["state"]

        formal_event = next(
            event for event in stream
            if event["type"] == "message"
            and event.get("message", {}).get("is_formal_round_turn")
        )
        checkpoint_after_formal = self.store.get_round_checkpoint(
            "room_plan", round_id,
        )["state"]
        pending = self.store.pending_round_chat_request("room_plan", round_id)

        self.assertEqual(
            [event["chat_request_id"] for event in terminal_interjections],
            [request["routing"]["request_id"] for request in requests[:2]],
        )
        self.assertEqual(checkpoint_at_limit["consecutive_interjections"], 2)
        self.assertEqual(checkpoint_at_limit["completed"], 0)
        self.assertEqual(formal_event["order"], 1)
        self.assertEqual(checkpoint_after_formal["consecutive_interjections"], 0)
        self.assertEqual(checkpoint_after_formal["completed"], 1)
        self.assertEqual(
            pending["id"],
            requests[2]["routing"]["request_id"],
        )

        remaining = list(stream)
        final_round = self.store.get_round("room_plan", round_id)
        formal_messages = [
            message for message in self.store.round_messages("room_plan", round_id)
            if message["sender_type"] == "ai" and message["is_formal_round_turn"]
        ]
        bundle = self.store.round_turn_contract_bundle("room_plan", round_id)

        self.assertTrue(any(event["type"] == "round_completed" for event in remaining))
        self.assertEqual(final_round["status"], "COMPLETED")
        self.assertEqual(len(formal_messages), len(members))
        self.assertTrue(bundle["valid"], bundle["issues"])
        self.assertTrue(all(
            self.store.get_chat_request(
                "room_plan", request["routing"]["request_id"]
            )["status"] == "COMPLETED"
            for request in requests
        ))

    def test_failed_interjections_count_toward_the_same_fairness_limit(self) -> None:
        members = self.store.enabled_members("room_plan")
        target = members[-1]
        stream = DiscussionOrchestrator(
            self.store,
            FakeRegistry(SelectiveMentionProvider(str(target["name"]))),
            market_service=None,
        ).run_round("room_plan", "失败插话不能饿死正式讨论")
        round_id = next(stream)["round"]["id"]
        requests = [
            self.store.create_user_message_request(
                "room_plan",
                content=f"@{target['name']} 失败插话 {index + 1}",
                mentions=[self.mention(target)],
                expected_round_id=round_id,
                client_message_id=f"mention-bounded-failure-{index + 1}",
            )
            for index in range(3)
        ]

        failures = []
        while len(failures) < 2:
            event = next(stream)
            if (
                event["type"] == "speaker_failed"
                and event.get("message", {}).get("is_formal_round_turn") is False
            ):
                failures.append(event)
        checkpoint_at_limit = self.store.get_round_checkpoint(
            "room_plan", round_id,
        )["state"]
        formal_event = next(
            event for event in stream
            if event.get("message", {}).get("is_formal_round_turn")
        )

        self.assertTrue(all(
            self.store.get_chat_request(
                "room_plan", request["routing"]["request_id"]
            )["status"] == "FAILED"
            for request in requests[:2]
        ))
        self.assertEqual(checkpoint_at_limit["consecutive_interjections"], 2)
        self.assertEqual(checkpoint_at_limit["failures"], 0)
        self.assertTrue(formal_event["message"]["is_formal_round_turn"])
        self.assertEqual(
            self.store.pending_round_chat_request("room_plan", round_id)["id"],
            requests[2]["routing"]["request_id"],
        )
        self.assertEqual(
            self.store.get_round_checkpoint("room_plan", round_id)["state"][
                "consecutive_interjections"
            ],
            0,
        )
        list(stream)

    def test_provider_skipped_interjections_count_toward_the_same_fairness_limit(self) -> None:
        members = self.store.enabled_members("room_plan")
        target = members[-1]
        stream = DiscussionOrchestrator(
            self.store,
            FakeRegistry(FakeProvider()),
            market_service=None,
        ).run_round("room_plan", "跳过 Provider 的插话也不能饿死正式讨论")
        round_id = next(stream)["round"]["id"]
        requests = [
            self.store.create_user_message_request(
                "room_plan",
                content=f"@{target['name']} 跳过插话 {index + 1}",
                mentions=[self.mention(target)],
                expected_round_id=round_id,
                client_message_id=f"mention-bounded-skipped-{index + 1}",
                skip_provider_ids={"openai"},
            )
            for index in range(3)
        ]

        skipped_failures = []
        while len(skipped_failures) < 2:
            event = next(stream)
            if (
                event["type"] == "speaker_failed"
                and event.get("error_code") == "provider_skipped"
            ):
                skipped_failures.append(event)
        checkpoint_at_limit = self.store.get_round_checkpoint(
            "room_plan", round_id,
        )["state"]
        formal_event = next(
            event for event in stream
            if event.get("message", {}).get("is_formal_round_turn")
        )

        self.assertTrue(all(
            self.store.get_chat_request(
                "room_plan", request["routing"]["request_id"]
            )["status"] == "FAILED"
            for request in requests[:2]
        ))
        self.assertEqual(checkpoint_at_limit["consecutive_interjections"], 2)
        self.assertEqual(checkpoint_at_limit["failures"], 0)
        self.assertEqual(checkpoint_at_limit["skipped"], 0)
        self.assertTrue(formal_event["message"]["is_formal_round_turn"])
        self.assertEqual(
            self.store.pending_round_chat_request("room_plan", round_id)["id"],
            requests[2]["routing"]["request_id"],
        )
        list(stream)

    def test_pause_resume_preserves_the_interjection_fairness_counter(self) -> None:
        members = self.store.enabled_members("room_plan")
        target = members[-1]
        orchestrator = DiscussionOrchestrator(
            self.store,
            FakeRegistry(FakeProvider()),
            market_service=None,
        )
        stream = orchestrator.run_round(
            "room_plan", "暂停恢复不能重置插话公平计数"
        )
        round_id = next(stream)["round"]["id"]
        requests = [
            self.store.create_user_message_request(
                "room_plan",
                content=f"@{target['name']} 暂停测试插话 {index + 1}",
                mentions=[self.mention(target)],
                expected_round_id=round_id,
                client_message_id=f"mention-pause-fairness-{index + 1}",
            )
            for index in range(3)
        ]

        first_interjection = next(
            event for event in stream
            if event["type"] == "message"
            and event.get("chat_request_id") == requests[0]["routing"]["request_id"]
        )
        self.assertFalse(first_interjection["message"]["is_formal_round_turn"])
        self.assertEqual(
            self.store.get_round_checkpoint("room_plan", round_id)["state"][
                "consecutive_interjections"
            ],
            1,
        )

        self.store.request_round_pause("room_plan", round_id)
        paused_event = next(
            event for event in stream if event["type"] == "round_paused"
        )
        self.assertEqual(paused_event["status"], "PAUSED")
        self.assertEqual(list(stream), [])
        self.assertEqual(
            self.store.get_round_checkpoint("room_plan", round_id)["state"][
                "consecutive_interjections"
            ],
            1,
        )

        resumed = orchestrator.run_round(
            "room_plan", "", resume_round_id=round_id
        )
        self.assertEqual(next(resumed)["type"], "round_resumed")
        second_interjection = next(
            event for event in resumed
            if event["type"] == "message"
            and event.get("chat_request_id") == requests[1]["routing"]["request_id"]
        )
        self.assertFalse(second_interjection["message"]["is_formal_round_turn"])
        self.assertEqual(
            self.store.get_round_checkpoint("room_plan", round_id)["state"][
                "consecutive_interjections"
            ],
            2,
        )
        formal_event = next(
            event for event in resumed
            if event.get("message", {}).get("is_formal_round_turn")
        )
        self.assertEqual(formal_event["order"], 1)
        self.assertEqual(
            self.store.pending_round_chat_request("room_plan", round_id)["id"],
            requests[2]["routing"]["request_id"],
        )
        self.assertEqual(
            self.store.get_round_checkpoint("room_plan", round_id)["state"][
                "consecutive_interjections"
            ],
            0,
        )
        list(resumed)

    def test_converged_round_does_not_buy_an_extra_formal_follow_up_for_fairness(self) -> None:
        members = self.store.enabled_members("room_plan")
        target = members[-1]
        orchestrator = DiscussionOrchestrator(
            self.store,
            FakeRegistry(FakeProvider()),
            market_service=None,
        )
        stream = orchestrator.run_round(
            "room_plan", "收敛后插话不应触发额外正式追问"
        )
        round_id = next(stream)["round"]["id"]

        while True:
            event = next(stream)
            convergence = (
                event.get("convergence")
                if event["type"] == "convergence_updated"
                and isinstance(event.get("convergence"), dict)
                else None
            )
            if not convergence or not convergence.get("can_host_finish"):
                continue
            research_gate = (
                convergence.get("research_evidence_gate")
                if isinstance(convergence.get("research_evidence_gate"), dict)
                else {}
            )
            project_workspace = (
                convergence.get("project_workspace")
                if isinstance(convergence.get("project_workspace"), dict)
                else {}
            )
            workspace_focus = (
                research_gate.get("focus")
                if isinstance(research_gate.get("focus"), dict)
                else project_workspace.get("focus")
                if isinstance(project_workspace.get("focus"), dict)
                else None
            )
            successful_member_ids = {
                str(message.get("sender_id") or "")
                for message in self.store.round_messages("room_plan", round_id)
                if message.get("sender_type") == "ai"
                and message.get("is_formal_round_turn")
            }
            if orchestrator._workspace_focus_covered(
                workspace_focus,
                members,
                successful_member_ids,
            ):
                break
        formal_before = [
            message
            for message in self.store.round_messages("room_plan", round_id)
            if message["sender_type"] == "ai" and message["is_formal_round_turn"]
        ]
        requests = [
            self.store.create_user_message_request(
                "room_plan",
                content=f"@{target['name']} 收敛后插话 {index + 1}",
                mentions=[self.mention(target)],
                expected_round_id=round_id,
                client_message_id=f"mention-after-convergence-{index + 1}",
            )
            for index in range(3)
        ]

        remaining = list(stream)
        formal_after = [
            message
            for message in self.store.round_messages("room_plan", round_id)
            if message["sender_type"] == "ai" and message["is_formal_round_turn"]
        ]
        interjection_ids = [
            event["chat_request_id"]
            for event in remaining
            if event["type"] == "message" and event.get("chat_request_id")
        ]

        self.assertEqual(
            interjection_ids,
            [request["routing"]["request_id"] for request in requests],
        )
        self.assertEqual(len(formal_after), len(formal_before))
        self.assertEqual(
            self.store.get_round_checkpoint("room_plan", round_id)["state"][
                "consecutive_interjections"
            ],
            3,
        )
        self.assertEqual(
            self.store.get_round("room_plan", round_id)["status"],
            "COMPLETED",
        )

    def test_moderated_interjection_drains_after_all_formal_member_caps_are_spent(self) -> None:
        room = self.store.room_snapshot("room_plan")["room"]
        workflow_policy = {
            **room["workflow_policy"],
            "max_turns_per_member": 1,
            "follow_up_budget": 0,
        }
        self.store.update_room("room_plan", {
            "expected_updated_at": room["updated_at"],
            "workflow_policy": workflow_policy,
        })
        stream = DiscussionOrchestrator(
            self.store,
            FakeRegistry(FakeProvider()),
            market_service=None,
        ).run_round("room_plan", "正式发言额度耗尽后仍要终结主持分配的插话")
        round_id = next(stream)["round"]["id"]

        while True:
            event = next(stream)
            if not event.get("message", {}).get("is_formal_round_turn"):
                continue
            checkpoint = self.store.get_round_checkpoint(
                "room_plan", round_id,
            )["state"]
            if (
                checkpoint["completed"]
                + checkpoint["failures"]
                + checkpoint["skipped"]
                >= checkpoint["max_turns"]
            ):
                break

        request = self.store.create_user_message_request(
            "room_plan",
            content="请主持人分配并回应这条额度外的非正式插话",
            mentions=[],
            expected_round_id=round_id,
            client_message_id="mention-moderated-after-formal-cap",
        )
        remaining = list(stream)

        self.assertEqual(request["routing"]["mode"], "round_interjection")
        self.assertTrue(any(
            event["type"] == "message"
            and event.get("chat_request_id") == request["routing"]["request_id"]
            and not event["message"]["is_formal_round_turn"]
            for event in remaining
        ))
        self.assertFalse(any(
            event.get("message", {}).get("is_formal_round_turn")
            for event in remaining
        ))
        self.assertEqual(
            self.store.get_chat_request(
                "room_plan", request["routing"]["request_id"]
            )["status"],
            "COMPLETED",
        )
        self.assertIsNone(
            self.store.pending_round_chat_request("room_plan", round_id)
        )

    def test_interjection_only_race_finishes_before_hidden_director_call(self) -> None:
        members = self.store.enabled_members("room_plan")
        moderator = members[0]
        room = {
            **self.store.room_snapshot("room_plan")["room"],
            "moderator_member_id": moderator["id"],
            "moderator_member_version": moderator["version"],
            "moderator_provider": moderator["provider"],
            "moderator_model": moderator["model"],
        }
        round_row = self.store.create_round(
            "room_plan", "interjection-only scheduling race"
        )
        spoken_counts = {str(member["id"]): 0 for member in members}
        spoken_counts[str(moderator["id"])] = 1
        pending_states = {
            "disappeared": None,
            "processing_only": {
                "id": "request_processing_elsewhere",
                "target_mode": "explicit",
                "targets": [{
                    "member_id": members[-1]["id"],
                    "member_version": members[-1]["version"],
                    "status": "PROCESSING",
                }],
            },
        }

        for case, pending_request in pending_states.items():
            with self.subTest(case=case):
                provider = CountingProvider()
                ledger = RecordingProviderCallLedger()
                orchestrator = DiscussionOrchestrator(
                    self.store,
                    FakeRegistry(provider),
                    market_service=None,
                )
                with patch.object(
                    self.store,
                    "pending_round_chat_request",
                    return_value=pending_request,
                ):
                    selection = orchestrator._select_next_member(
                        room,
                        room["workflow_policy"],
                        "finish safely after the outer pending request changed",
                        members,
                        spoken_counts,
                        {str(moderator.get("stance") or "neutral")},
                        {str(moderator["id"])},
                        set(),
                        1,
                        round_id=str(round_row["id"]),
                        provider_call_ledger=ledger,
                        interjection_only_mode=True,
                    )

                self.assertEqual(selection["action"], "finish")
                self.assertEqual(selection["source"], "interjection_queue")
                self.assertEqual(provider.generate_call_count, 0)
                self.assertEqual(ledger.snapshot_calls, 0)
                self.assertEqual(ledger.reserve_calls, [])
                self.assertEqual(ledger.finish_calls, [])
                self.assertEqual(
                    self.store.list_director_attempts(
                        "room_plan", round_id=str(round_row["id"])
                    ),
                    [],
                )

    def test_large_skipped_interjection_backlog_cannot_finalize_round_partial(self) -> None:
        members = self.store.enabled_members("room_plan")
        target = members[-1]
        orchestrator = DiscussionOrchestrator(
            self.store,
            FakeRegistry(FakeProvider()),
            market_service=None,
        )
        with patch.object(
            orchestrator,
            "_convergence_state",
            wraps=orchestrator._convergence_state,
        ) as convergence_state:
            stream = orchestrator.run_round(
                "room_plan",
                "大量失败插话后仍需完成正式讨论",
            )
            started = next(stream)
            round_id = started["round"]["id"]
            request_ids = []
            for index in range(103):
                request = self.store.create_user_message_request(
                    "room_plan",
                    content=f"@{target['name']} 跳过插话 {index + 1}",
                    mentions=[self.mention(target)],
                    expected_round_id=round_id,
                    client_message_id=f"mention-skipped-backlog-{index + 1}",
                    skip_provider_ids={"openai"},
                )
                request_ids.append(request["routing"]["request_id"])

            events = list(stream)
        self.assertLessEqual(
            convergence_state.call_count,
            (4 * len(members)) + 20,
        )
        final_round = self.store.get_round("room_plan", round_id)
        checkpoint = self.store.get_round_checkpoint("room_plan", round_id)["state"]
        formal_messages = [
            message for message in self.store.round_messages("room_plan", round_id)
            if message["sender_type"] == "ai" and message["is_formal_round_turn"]
        ]
        bundle = self.store.round_turn_contract_bundle("room_plan", round_id)

        consecutive_interjection_terminals = 0
        formal_terminals = 0
        for event in events:
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            if message.get("chat_request_id") and event["type"] in {
                "message",
                "speaker_failed",
            }:
                consecutive_interjection_terminals += 1
                if formal_terminals < len(members):
                    self.assertLessEqual(consecutive_interjection_terminals, 2)
            elif message.get("is_formal_round_turn") and event["type"] in {
                "message",
                "speaker_failed",
            }:
                formal_terminals += 1
                consecutive_interjection_terminals = 0
            elif event["type"] == "speaker_skipped":
                formal_terminals += 1
                consecutive_interjection_terminals = 0

        self.assertEqual(
            sum(
                event["type"] == "speaker_failed"
                and event.get("error_code") == "provider_skipped"
                for event in events
            ),
            len(request_ids),
        )
        self.assertTrue(all(
            self.store.get_chat_request("room_plan", request_id)["status"] == "FAILED"
            for request_id in request_ids
        ))
        self.assertIsNone(self.store.pending_round_chat_request("room_plan", round_id))
        self.assertEqual(final_round["status"], "COMPLETED")
        self.assertEqual(checkpoint["completed"], len(members))
        self.assertEqual(checkpoint["failures"], 0)
        self.assertEqual(checkpoint["skipped"], 0)
        self.assertEqual(len(checkpoint["successful_member_ids"]), len(members))
        self.assertEqual(len(formal_messages), len(members))
        self.assertTrue(bundle["valid"], bundle["issues"])

    def test_interjection_convergence_cache_is_invalidated_by_yielded_database_change(
        self,
    ) -> None:
        members = self.store.enabled_members("room_plan")
        target = members[-1]
        orchestrator = DiscussionOrchestrator(
            self.store,
            FakeRegistry(FakeProvider()),
            market_service=None,
        )
        stream = orchestrator.run_round(
            "room_plan",
            "楠岃瘉鎻掕瘽浜嬩欢闂寸殑骞跺彂鍙樻洿浼氫娇缂撳瓨澶辨晥",
        )
        with patch.object(
            orchestrator,
            "_convergence_state",
            wraps=orchestrator._convergence_state,
        ) as convergence_state:
            started = next(stream)
            round_id = started["round"]["id"]
            self.store.create_user_message_request(
                "room_plan",
                content=f"@{target['name']} 璺宠繃杩欐潯鎻掕瘽",
                mentions=[self.mention(target)],
                expected_round_id=round_id,
                client_message_id="mention-cache-invalidation-1",
                skip_provider_ids={"openai"},
            )

            while True:
                event = next(stream)
                if (
                    event["type"] == "speaker_failed"
                    and event.get("error_code") == "provider_skipped"
                ):
                    break

            calls_before_change = convergence_state.call_count
            self.store.add_message(
                "room_plan",
                sender_type="user",
                sender_id="user",
                sender_name="鎴?",
                content="鍦ㄦ祦浜嬩欢涔嬮棿鎻掑叆鐨勬柊璇存槑",
                round_id=round_id,
            )
            convergence_event = next(stream)
            self.assertEqual(convergence_event["type"], "convergence_updated")
            self.assertGreater(convergence_state.call_count, calls_before_change)
        stream.close()

    def test_unavailable_active_target_is_failed_instead_of_pausing_forever(self) -> None:
        members = self.store.enabled_members("room_plan")
        target = members[-1]
        provider = FakeProvider()
        orchestrator = DiscussionOrchestrator(
            self.store,
            FakeRegistry(provider),
            market_service=None,
        )
        stream = orchestrator.run_round("room_plan", "验证不可调度插话会终结")
        round_id = ""
        for event in stream:
            if event["type"] == "round_started":
                round_id = event["round"]["id"]
            if event["type"] == "message":
                break
        result = self.store.create_user_message_request(
            "room_plan",
            content=f"@{target['name']} 停用后不应永久卡住",
            mentions=[self.mention(target)],
            expected_round_id=round_id,
            client_message_id="mention-active-disabled-1",
        )
        current = self.store.get_member("room_plan", target["id"])
        self.store.update_member(
            "room_plan",
            target["id"],
            {**current, "enabled": False},
        )

        remaining = list(stream)
        request = self.store.get_chat_request(
            "room_plan",
            result["routing"]["request_id"],
        )
        final_round = self.store.get_round("room_plan", round_id)

        self.assertEqual(request["status"], "FAILED")
        self.assertEqual(request["targets"][0]["status"], "FAILED")
        self.assertFalse(any(
            event.get("chat_request_id") == result["routing"]["request_id"]
            and event["type"] == "message"
            for event in remaining
        ))
        self.assertNotEqual(final_round["status"], "PAUSED")


if __name__ == "__main__":
    unittest.main()
