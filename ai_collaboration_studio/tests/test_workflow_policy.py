from __future__ import annotations

import copy
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
from unittest.mock import patch

from backend import http_server
from backend.convergence import ConvergenceService
from backend.orchestrator import DiscussionOrchestrator
from backend.providers.base import ProviderResponse
from backend.store import StudioStore
from backend.templates import ROOM_TEMPLATES
from backend.workflow_policy import (
    LEGACY_STORAGE_WORKFLOW_POLICY_V1,
    default_workflow_policy,
)
from tests.turn_contract_fixture import append_valid_turn_contract


class PolicyProvider:
    provider_id = "openai"

    def status(self) -> dict[str, object]:
        return {"id": self.provider_id, "configured": True}

    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        if "隐藏主持调度器" in instructions:
            marker = '"member_id": "'
            member_id = input_text.split(marker, 1)[1].split('"', 1)[0] if marker in input_text else ""
            return ProviderResponse(
                ok=True,
                provider=self.provider_id,
                model=model or "policy-test",
                content=json.dumps({
                    "action": "speak",
                    "member_id": member_id,
                    "reason": "按冻结政策继续覆盖。",
                }, ensure_ascii=False),
            )
        content = append_valid_turn_contract(
            "按当前身份完成一次可审查发言。",
            instructions=instructions,
            input_text=input_text,
        )
        return ProviderResponse(
            ok=True,
            provider=self.provider_id,
            model=model or "policy-test",
            content=content,
        )


class PolicyRegistry:
    def __init__(self) -> None:
        self.provider = PolicyProvider()

    def get(self, _provider_id: str) -> PolicyProvider:
        self.provider.provider_id = str(_provider_id or "openai")
        return self.provider


def policy_variant(
    *,
    stages: list[str],
    stage_coverage: dict[str, int],
    required_coverage: list[dict] | None = None,
    minimum_successful_members: int = 1,
    max_turns_per_member: int = 1,
    follow_up_budget: int = 0,
) -> dict:
    return {
        "version": 1,
        "stage_order": stages,
        "minimum_stage_coverage": stage_coverage,
        "required_coverage": required_coverage or [],
        "minimum_successful_members": minimum_successful_members,
        "max_turns_per_member": max_turns_per_member,
        "follow_up_budget": follow_up_budget,
        "user_confirmation_required": True,
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


class WorkflowPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "studio.sqlite3")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_defaults_and_catalog_return_structured_policies(self) -> None:
        bootstrap = self.store.bootstrap("room_storage")
        room = bootstrap["active"]["room"]
        self.assertNotIn("workflow_policy_json", room)
        self.assertEqual(
            room["workflow_policy"]["stage_order"],
            ["facilitate", "analysis", "debate", "plan", "risk", "decision"],
        )
        self.assertEqual(room["workflow_policy"]["minimum_successful_members"], 12)
        self.assertEqual(room["workflow_policy"]["minimum_stage_coverage"]["analysis"], 6)
        self.assertIn(
            "data_quality",
            [item["id"] for item in room["workflow_policy"]["required_coverage"]],
        )
        self.assertFalse(room["workflow_policy"]["live_trading_allowed"])
        self.assertIn("market.storage.readonly", room["capabilities"])
        self.assertIn("simulation.observations", room["capabilities"])
        self.assertIn("simulation.paper_portfolio", room["capabilities"])
        self.assertEqual(
            room["capability_pack_ids"],
            ["storage_research_readonly", "structured_turn_contract_v1"],
        )
        self.assertIn("discussion.turn_contract_v1", room["capabilities"])
        self.assertEqual(room["category_path"], ["交易研究", "美股"])

        storage_template = next(
            item for item in bootstrap["templates"] if item["id"] == "us_storage_committee"
        )
        generic_template = next(
            item for item in bootstrap["templates"] if item["id"] == "open_collaboration"
        )
        project_template = next(
            item for item in bootstrap["templates"] if item["id"] == "project_research"
        )
        self.assertEqual(storage_template["workflow_policy"], room["workflow_policy"])
        self.assertEqual(storage_template["capabilities"], room["capabilities"])
        self.assertEqual(
            storage_template["capability_pack_ids"],
            ["storage_research_readonly", "structured_turn_contract_v1"],
        )
        self.assertIn("discussion.turn_contract_v1", storage_template["capabilities"])
        self.assertEqual(storage_template["category_path"], ["交易研究", "美股"])
        self.assertNotIn("market.storage.readonly", generic_template["capabilities"])
        self.assertEqual(generic_template["capability_pack_ids"], [])
        self.assertEqual(project_template["capability_pack_ids"], ["structured_project_research"])
        self.assertIn("research.project.option_matrix", project_template["capabilities"])
        self.assertNotIn("market.storage.readonly", project_template["capabilities"])
        self.assertEqual(
            {pack["id"] for pack in bootstrap["capability_packs"]},
            {
                "football_research_readonly",
                "stock_research_readonly",
                "storage_research_readonly",
                "structured_project_research",
                "structured_turn_contract_v1",
                "project_readiness_review",
                "project_round_focus",
            },
        )
        self.assertTrue(all(
            pack["execution_capability"] == "none"
            and pack["live_trading_allowed"] is False
            for pack in bootstrap["capability_packs"]
        ))
        project_pack = next(
            pack for pack in bootstrap["capability_packs"]
            if pack["id"] == "structured_project_research"
        )
        core_protocol = next(
            pack for pack in bootstrap["capability_packs"]
            if pack["id"] == "structured_turn_contract_v1"
        )
        self.assertTrue(core_protocol["system_managed"])
        self.assertEqual(core_protocol["scope"], "formal_round_core")
        self.assertIn("discussion.turn_contract_v1", core_protocol["capabilities"])
        self.assertEqual(project_pack["mode_label"], "仅研究")
        self.assertEqual(project_pack["discussion_protocol"]["title"], "结构化项目研究协议")
        self.assertEqual(generic_template["workflow_policy"]["minimum_successful_members"], 2)
        self.assertEqual(
            generic_template["workflow_policy"]["stage_order"],
            ["facilitate", "flexible", "decision"],
        )
        self.assertIn(
            "decision_synthesis",
            [
                item["id"]
                for item in generic_template["workflow_policy"]["required_coverage"]
            ],
        )
        generic_members = self.store.enabled_members("room_plan")
        decision_member = next(
            member for member in generic_members if member["workflow_stage"] == "decision"
        )
        self.assertIn("decision_synthesis", decision_member["capabilities"])
        instructions = DiscussionOrchestrator(self.store, PolicyRegistry())._instructions(
            room=self.store.room_snapshot("room_plan")["room"],
            member=decision_member,
            previous_name="反方审查员",
        )
        self.assertIn("候选首选：方案名", instructions)
        self.assertTrue(all("workflow_policy" in listed for listed in bootstrap["rooms"]))
        self.assertTrue(all("workflow_policy_json" not in listed for listed in bootstrap["rooms"]))
        self.assertTrue(all("capabilities" in listed for listed in bootstrap["rooms"]))
        self.assertTrue(all("category_path" in listed for listed in bootstrap["rooms"]))

    def test_new_storage_template_room_enables_readonly_research_and_turn_contract(self) -> None:
        created = self.store.create_room(
            "新建存储产业委员会",
            "验证模板创建时立即冻结完整的默认能力边界。",
            template_id="us_storage_committee",
        )
        room = created["room"]

        self.assertEqual(
            room["capability_pack_ids"],
            ["storage_research_readonly", "structured_turn_contract_v1"],
        )
        self.assertIn("market.storage.readonly", room["capabilities"])
        self.assertIn("discussion.turn_contract_v1", room["capabilities"])
        self.assertEqual(room["workflow_policy"]["execution_capability"], "none")
        self.assertFalse(room["workflow_policy"]["live_trading_allowed"])

    def test_new_general_room_defaults_to_structured_turns_without_migrating_seed(self) -> None:
        seeded = self.store.room_snapshot("room_plan")["room"]
        created = self.store.create_room(
            "新建开放共创房间",
            "默认产生可审计的结构化专业发言。",
            template_id="open_collaboration",
        )

        self.assertEqual(seeded["capability_pack_ids"], [])
        self.assertEqual(
            created["room"]["capability_pack_ids"],
            ["structured_turn_contract_v1"],
        )
        self.assertIn("discussion.turn_contract_v1", created["room"]["capabilities"])

    def test_legacy_pack_id_can_be_omitted_without_becoming_a_protocol_opt_out(self) -> None:
        created = self.store.create_room(
            "纯文本开放共创房间",
            "用户显式关闭结构化发言合同。",
            template_id="open_collaboration",
            capability_pack_ids=[],
        )

        self.assertEqual(created["room"]["capability_pack_ids"], [])
        self.assertNotIn("discussion.turn_contract_v1", created["room"]["capabilities"])

    def test_room_capability_pack_is_independent_from_template_and_category_hierarchy(self) -> None:
        created = self.store.create_room(
            "存储小群聊",
            "用四位通用成员研究存储行业。",
            category=" 交易研究／美股 / 存储小组 ",
            template_id="open_collaboration",
            capability_pack_ids=["storage_research_readonly"],
        )
        room = created["room"]

        self.assertEqual(room["template_id"], "open_collaboration")
        self.assertEqual(room["category"], "交易研究 / 美股 / 存储小组")
        self.assertEqual(room["category_path"], ["交易研究", "美股", "存储小组"])
        self.assertEqual(room["capability_pack_ids"], ["storage_research_readonly"])
        self.assertIn("market.storage.readonly", room["capabilities"])
        self.assertEqual(len(created["members"]), 4)

        observation = self.store.create_observation(room["id"], {
            "symbol": "MU",
            "direction": "UP",
            "horizon_days": 1,
            "threshold_pct": 2,
            "thesis": "房间能力包独立启用了模拟观察。",
            "counter_case": "若实际方向相反则失效。",
            "evidence": {},
        })
        self.assertEqual(observation["status"], "PROPOSED")

    def test_unknown_room_capability_pack_is_rejected_before_room_creation(self) -> None:
        before = len(self.store.list_rooms())
        with self.assertRaisesRegex(ValueError, "未知领域能力包"):
            self.store.create_room(
                "非法能力包",
                "不应创建",
                template_id="open_collaboration",
                capability_pack_ids=["live_trading"],
            )
        self.assertEqual(len(self.store.list_rooms()), before)

    def test_capability_pack_with_execution_permission_fails_closed(self) -> None:
        unsafe_pack = {
            "id": "unsafe_execution",
            "name": "不安全执行包",
            "capabilities": ["orders.write"],
            "execution_capability": "orders",
            "live_trading_allowed": True,
        }
        with patch.dict("backend.capability_packs.CAPABILITY_PACKS", {"unsafe_execution": unsafe_pack}):
            with self.assertRaisesRegex(ValueError, "违反不可执行安全边界"):
                self.store.create_room(
                    "不安全房间",
                    "不应创建",
                    template_id="open_collaboration",
                    capability_pack_ids=["unsafe_execution"],
                )

    def test_room_settings_update_metadata_packs_and_reject_stale_or_unknown_changes(self) -> None:
        original = self.store.room_snapshot("room_plan")["room"]
        updated = self.store.update_room("room_plan", {
            "expected_updated_at": original["updated_at"],
            "title": "可编辑研究小组",
            "objective": "持续核验房间设置和能力边界。",
            "category": "项目研究／AI / 评审",
            "discussion_mode": "sequential",
            "capability_pack_ids": ["storage_research_readonly"],
        })

        self.assertEqual(updated["title"], "可编辑研究小组")
        self.assertEqual(updated["category"], "项目研究 / AI / 评审")
        self.assertEqual(updated["category_path"], ["项目研究", "AI", "评审"])
        self.assertEqual(updated["discussion_mode"], "sequential")
        self.assertEqual(updated["capability_pack_ids"], ["storage_research_readonly"])
        self.assertIn("market.storage.readonly", updated["capabilities"])
        self.assertGreater(updated["updated_at"], original["updated_at"])

        with self.assertRaisesRegex(ValueError, "已被其他操作更新"):
            self.store.update_room("room_plan", {
                "expected_updated_at": original["updated_at"],
                "title": "过期写入",
            })
        with self.assertRaisesRegex(ValueError, "未知领域能力包"):
            self.store.update_room("room_plan", {
                "expected_updated_at": updated["updated_at"],
                "capability_pack_ids": ["trade_execution"],
            })
        self.assertEqual(self.store.room_snapshot("room_plan")["room"]["title"], "可编辑研究小组")

    def test_paused_round_keeps_frozen_capability_packs_after_room_edit(self) -> None:
        orchestrator = DiscussionOrchestrator(self.store, PolicyRegistry(), market_service=None)
        paused_stream = orchestrator.run_round("room_plan", "冻结通用能力")
        first = next(paused_stream)
        self.assertEqual(first["type"], "round_started")
        paused_stream.close()
        round_id = first["round"]["id"]

        checkpoint = self.store.get_round_checkpoint("room_plan", round_id)
        self.assertEqual(checkpoint["state"]["version"], 9)
        self.assertIn(
            checkpoint["state"]["moderator_member_id"],
            checkpoint["state"]["member_ids"],
        )
        self.assertEqual(checkpoint["state"]["capability_pack_ids"], [])
        self.assertNotIn("market.storage.readonly", checkpoint["state"]["room_capabilities"])

        current = self.store.room_snapshot("room_plan")["room"]
        self.store.update_room("room_plan", {
            "expected_updated_at": current["updated_at"],
            "capability_pack_ids": ["storage_research_readonly"],
        })
        self.assertIn(
            "market.storage.readonly",
            self.store.room_snapshot("room_plan")["room"]["capabilities"],
        )
        self.assertEqual(
            self.store.get_round_checkpoint("room_plan", round_id)["state"]["capability_pack_ids"],
            [],
        )
        checkpoint_summary = self.store.room_snapshot("room_plan")["round_checkpoint"]
        self.assertEqual(checkpoint_summary["version"], 9)
        self.assertTrue(checkpoint_summary["moderator_member_id"])
        self.assertEqual(checkpoint_summary["capability_pack_ids"], [])
        self.assertNotIn("market.storage.readonly", checkpoint_summary["room_capabilities"])

        resumed = list(orchestrator.run_round("room_plan", "", resume_round_id=round_id))
        self.assertFalse(any(event.get("code") == "ROUND_MARKET_PREFLIGHT_FAILED" for event in resumed))
        self.assertEqual(self.store.get_round("room_plan", round_id)["status"], "COMPLETED")

        next_round = list(orchestrator.run_round("room_plan", "下一轮使用新能力包"))
        self.assertEqual(next_round[0]["code"], "ROUND_MARKET_PREFLIGHT_FAILED")

    def test_domain_features_follow_capabilities_not_a_specific_template_id(self) -> None:
        alias_template = copy.deepcopy(ROOM_TEMPLATES["us_storage_committee"])
        alias_template["id"] = "storage_capability_alias"
        alias_template["name"] = "能力别名房间"
        with patch.dict(ROOM_TEMPLATES, {alias_template["id"]: alias_template}):
            created = self.store.create_room(
                "能力驱动测试",
                "验证领域功能不依赖固定模板名",
                template_id=alias_template["id"],
            )
            room_id = created["room"]["id"]
            self.assertEqual(created["room"]["template_id"], alias_template["id"])
            self.assertIn("market.storage.readonly", created["room"]["capabilities"])

            observation = self.store.create_observation(room_id, {
                "symbol": "MU",
                "direction": "UP",
                "horizon_days": 1,
                "threshold_pct": 2,
                "thesis": "能力包允许创建待确认模拟观察。",
                "counter_case": "若实际方向相反则失效。",
                "evidence": {},
            })
            self.assertEqual(observation["status"], "PROPOSED")

            market_gate, market_snapshot = DiscussionOrchestrator(
                self.store,
                PolicyRegistry(),
                market_service=None,
            ).preflight_market(room_id, snapshot=created)
            self.assertTrue(market_gate["applicable"])
            self.assertFalse(market_gate["ready"])
            self.assertIsNone(market_snapshot)
            self.assertEqual(
                market_gate["capture_error"]["code"],
                "MARKET_SERVICE_UNAVAILABLE",
            )

    def test_policy_validation_preserves_non_bypassable_safety_fields(self) -> None:
        expected_updated_at = self.store.room_snapshot("room_plan")["room"]["updated_at"]
        unsafe = default_workflow_policy("open_collaboration")
        unsafe["live_trading_allowed"] = True
        with self.assertRaisesRegex(ValueError, "live_trading_allowed"):
            self.store.update_room("room_plan", {
                "expected_updated_at": expected_updated_at,
                "workflow_policy": unsafe,
            })

        unsafe = default_workflow_policy("open_collaboration")
        unsafe["execution_capability"] = "orders"
        with self.assertRaisesRegex(ValueError, "execution_capability"):
            self.store.update_room("room_plan", {
                "expected_updated_at": expected_updated_at,
                "workflow_policy": unsafe,
            })

        unsafe = default_workflow_policy("open_collaboration")
        unsafe["user_confirmation_required"] = False
        with self.assertRaisesRegex(ValueError, "user_confirmation_required"):
            self.store.update_room("room_plan", {
                "expected_updated_at": expected_updated_at,
                "workflow_policy": unsafe,
            })

        invalid_stage = default_workflow_policy("open_collaboration")
        invalid_stage["stage_order"] = ["facilitate", "follow_up"]
        invalid_stage["minimum_stage_coverage"] = {"facilitate": 1, "follow_up": 1}
        with self.assertRaisesRegex(ValueError, "保留"):
            self.store.update_room("room_plan", {
                "expected_updated_at": expected_updated_at,
                "workflow_policy": invalid_stage,
            })

        missing_stage_coverage = default_workflow_policy("open_collaboration")
        del missing_stage_coverage["minimum_stage_coverage"]["flexible"]
        with self.assertRaisesRegex(ValueError, "minimum_stage_coverage 缺少"):
            self.store.update_room(
                "room_plan",
                {
                    "expected_updated_at": expected_updated_at,
                    "workflow_policy": missing_stage_coverage,
                },
            )

    def test_capabilities_are_public_versioned_and_can_satisfy_generic_requirements(self) -> None:
        members = self.store.enabled_members("room_plan")
        chair = self.store.update_member("room_plan", members[0]["id"], {
            "stance": "neutral",
            "workflow_stage": "kickoff",
            "capabilities": ["facilitation"],
        })
        reviewer = self.store.update_member("room_plan", members[1]["id"], {
            "stance": "neutral",
            "workflow_stage": "review",
            "capabilities": ["red_team"],
        })
        for member in members[2:]:
            self.store.update_member("room_plan", member["id"], {"enabled": False})
        policy = policy_variant(
            stages=["kickoff", "review"],
            stage_coverage={"kickoff": 1, "review": 1},
            minimum_successful_members=2,
            required_coverage=[
                {
                    "id": "chair",
                    "label": "主持",
                    "minimum": 1,
                    "any_of": {"stances": [], "capabilities": ["facilitation"]},
                    "is_counterargument": False,
                },
                {
                    "id": "red_team",
                    "label": "反证",
                    "minimum": 1,
                    "any_of": {"stances": [], "capabilities": ["red_team"]},
                    "is_counterargument": True,
                },
            ],
        )
        self.store.update_room("room_plan", {
            "expected_updated_at": self.store.room_snapshot("room_plan")["room"]["updated_at"],
            "workflow_policy": policy,
        })
        round_row = self.store.create_round("room_plan", "验证自定义能力覆盖")
        for member in (chair, reviewer):
            self.store.add_message(
                "room_plan",
                sender_type="ai",
                sender_id=member["id"],
                sender_name=member["name"],
                content="完成本身份要求。",
                round_id=round_row["id"],
                member_version=member["version"],
            )
        state = ConvergenceService(self.store).evaluate("room_plan", round_id=round_row["id"])
        self.assertTrue(state["discussion_gate"]["ready"])
        self.assertTrue(state["counterargument_gate"]["ready"])
        self.assertEqual(state["discussion_gate"]["stage_coverage"][1]["id"], "review")

        revised = self.store.update_member("room_plan", reviewer["id"], {
            "capabilities": ["research"],
        })
        self.assertEqual(revised["capabilities"], ["research"])
        state = ConvergenceService(self.store).evaluate("room_plan", round_id=round_row["id"])
        self.assertFalse(state["discussion_gate"]["ready"])
        red_team = next(
            item for item in state["discussion_gate"]["role_coverage"] if item["id"] == "red_team"
        )
        self.assertEqual(red_team["successful_count"], 0)

        with closing(self.store._connect()) as connection:
            raw_version = connection.execute(
                """SELECT snapshot_json FROM member_versions
                   WHERE member_id=? ORDER BY version DESC LIMIT 1""",
                (reviewer["id"],),
            ).fetchone()[0]
        version_snapshot = json.loads(raw_version)
        self.assertEqual(version_snapshot["capabilities"], ["research"])
        self.assertNotIn("capabilities_json", version_snapshot)

    def test_round_freezes_policy_while_room_edits_apply_to_next_round(self) -> None:
        policy_a = policy_variant(
            stages=["facilitate", "flexible"],
            stage_coverage={"facilitate": 1, "flexible": 1},
            minimum_successful_members=2,
            max_turns_per_member=1,
            follow_up_budget=0,
        )
        policy_b = copy.deepcopy(policy_a)
        policy_b["max_turns_per_member"] = 2
        policy_b["follow_up_budget"] = 4
        self.store.update_room("room_plan", {
            "expected_updated_at": self.store.room_snapshot("room_plan")["room"]["updated_at"],
            "workflow_policy": policy_a,
        })

        orchestrator = DiscussionOrchestrator(self.store, PolicyRegistry(), market_service=None)
        paused_stream = orchestrator.run_round("room_plan", "冻结政策 A")
        first = next(paused_stream)
        self.assertEqual(first["type"], "round_started")
        paused_stream.close()
        round_id = first["round"]["id"]
        self.assertEqual(self.store.get_round("room_plan", round_id)["status"], "PAUSED")

        self.store.update_room("room_plan", {
            "expected_updated_at": self.store.room_snapshot("room_plan")["room"]["updated_at"],
            "workflow_policy": policy_b,
        })
        checkpoint = self.store.get_round_checkpoint("room_plan", round_id)
        self.assertEqual(checkpoint["state"]["workflow_policy"], policy_a)
        self.assertEqual(
            self.store.room_snapshot("room_plan")["room"]["workflow_policy"],
            policy_b,
        )

        resumed = list(orchestrator.run_round("room_plan", "", resume_round_id=round_id))
        old_round_messages = [event for event in resumed if event["type"] == "message"]
        self.assertEqual(len(old_round_messages), 4)

        new_round = list(orchestrator.run_round("room_plan", "新轮使用政策 B"))
        new_round_messages = [event for event in new_round if event["type"] == "message"]
        # max_turns_per_member and follow_up_budget are ceilings, not targets.
        # With all gates satisfied, rules-first ends after one pass instead of
        # spending four extra hidden-moderator and speaker calls.
        self.assertEqual(len(new_round_messages), 4)
        self.assertEqual(
            new_round[-1]["convergence"]["workflow_policy"],
            policy_b,
        )


class WorkflowPolicyLegacyMigrationTests(unittest.TestCase):
    def test_initial_pack_migration_versions_current_storage_and_project_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "initial-pack-versioning.sqlite3"
            StudioStore(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    "DELETE FROM room_versions WHERE room_id IN ('room_storage','room_project')"
                )
                connection.execute(
                    """UPDATE rooms
                       SET capability_packs_json='[]',settings_version=7,updated_at=700
                       WHERE id='room_storage'"""
                )
                connection.execute(
                    """UPDATE rooms
                       SET capability_packs_json='[]',settings_version=5,updated_at=500
                       WHERE id='room_project'"""
                )
                connection.execute(
                    """DELETE FROM schema_migrations WHERE key IN (
                        'room_capability_packs_v1',
                        'storage_turn_contract_capability_pack_v1',
                        'project_default_capability_pack_v2'
                    )"""
                )

            migrated = StudioStore(path)
            storage = migrated.room_snapshot("room_storage")["room"]
            project = migrated.room_snapshot("room_project")["room"]

            self.assertEqual(
                storage["capability_pack_ids"],
                ["storage_research_readonly", "structured_turn_contract_v1"],
            )
            self.assertEqual(storage["settings_version"], 8)
            self.assertEqual(project["capability_pack_ids"], ["structured_project_research"])
            self.assertEqual(project["settings_version"], 6)
            self.assertEqual(
                [item["version"] for item in migrated.list_room_versions("room_storage")["versions"]],
                [8, 7],
            )
            self.assertEqual(
                [item["version"] for item in migrated.list_room_versions("room_project")["versions"]],
                [6, 5],
            )
            self.assertEqual(
                migrated.get_room_version_record("room_storage", 7)["room_version"]["snapshot"][
                    "capability_pack_ids"
                ],
                [],
            )
            self.assertEqual(
                migrated.get_room_version_record("room_project", 5)["room_version"]["snapshot"][
                    "capability_pack_ids"
                ],
                [],
            )

    def test_storage_turn_contract_pack_migration_versions_exact_legacy_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "storage-turn-contract-pack.sqlite3"
            StudioStore(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    "DELETE FROM room_versions WHERE room_id='room_storage'"
                )
                connection.execute(
                    """UPDATE rooms
                       SET capability_packs_json=?,settings_version=7,updated_at=700
                       WHERE id='room_storage'""",
                    (json.dumps(["storage_research_readonly"]),),
                )
                connection.execute(
                    """DELETE FROM schema_migrations
                       WHERE key='storage_turn_contract_capability_pack_v1'"""
                )

            migrated = StudioStore(path)
            room = migrated.room_snapshot("room_storage")["room"]
            self.assertEqual(
                room["capability_pack_ids"],
                ["storage_research_readonly", "structured_turn_contract_v1"],
            )
            self.assertEqual(room["settings_version"], 8)

            history = migrated.list_room_versions("room_storage")["versions"]
            self.assertEqual([item["version"] for item in history], [8, 7])
            snapshots = {
                version: migrated.get_room_version_record("room_storage", version)[
                    "room_version"
                ]["snapshot"]
                for version in (7, 8)
            }
            self.assertEqual(
                snapshots[7]["capability_pack_ids"],
                ["storage_research_readonly"],
            )
            self.assertEqual(
                snapshots[8]["capability_pack_ids"],
                ["storage_research_readonly", "structured_turn_contract_v1"],
            )

    def test_storage_turn_contract_pack_migration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "storage-turn-contract-idempotent.sqlite3"
            StudioStore(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    "DELETE FROM room_versions WHERE room_id='room_storage'"
                )
                connection.execute(
                    """UPDATE rooms
                       SET capability_packs_json=?,settings_version=3,updated_at=300
                       WHERE id='room_storage'""",
                    (json.dumps(["storage_research_readonly"]),),
                )
                connection.execute(
                    """DELETE FROM schema_migrations
                       WHERE key='storage_turn_contract_capability_pack_v1'"""
                )

            first = StudioStore(path)
            first_room = first.room_snapshot("room_storage")["room"]
            first_history = first.list_room_versions("room_storage")["versions"]
            with closing(sqlite3.connect(path)) as connection:
                first_applied_at = connection.execute(
                    """SELECT applied_at FROM schema_migrations
                       WHERE key='storage_turn_contract_capability_pack_v1'"""
                ).fetchone()[0]

            second = StudioStore(path)
            second_room = second.room_snapshot("room_storage")["room"]
            second_history = second.list_room_versions("room_storage")["versions"]
            with closing(sqlite3.connect(path)) as connection:
                second_applied_at = connection.execute(
                    """SELECT applied_at FROM schema_migrations
                       WHERE key='storage_turn_contract_capability_pack_v1'"""
                ).fetchone()[0]

            self.assertEqual(second_room, first_room)
            self.assertEqual(second_history, first_history)
            self.assertEqual(second_applied_at, first_applied_at)
            self.assertEqual(second_room["settings_version"], 4)

    def test_storage_turn_contract_pack_migration_preserves_custom_pack_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "storage-turn-contract-custom.sqlite3"
            initial = StudioStore(path)
            customized = initial.update_room(
                "room_storage",
                {
                    "expected_settings_version": initial.room_snapshot("room_storage")[
                        "room"
                    ]["settings_version"],
                    "capability_pack_ids": [],
                },
            )
            self.assertIsNotNone(customized)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    """DELETE FROM schema_migrations
                       WHERE key='storage_turn_contract_capability_pack_v1'"""
                )

            migrated = StudioStore(path)
            preserved = migrated.room_snapshot("room_storage")["room"]
            self.assertEqual(preserved["capability_pack_ids"], [])
            self.assertEqual(
                preserved["settings_version"],
                customized["settings_version"],
            )

            later_removal = migrated.update_room(
                "room_storage",
                {
                    "expected_settings_version": preserved["settings_version"],
                    "capability_pack_ids": ["storage_research_readonly"],
                },
            )
            reopened = StudioStore(path).room_snapshot("room_storage")["room"]
            self.assertEqual(
                reopened["capability_pack_ids"],
                ["storage_research_readonly"],
            )
            self.assertEqual(
                reopened["settings_version"],
                later_removal["settings_version"],
            )

    def test_project_pack_migration_upgrades_only_untouched_seed_room_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "project-pack.sqlite3"
            StudioStore(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                prior_applied_at = connection.execute(
                    "SELECT applied_at FROM schema_migrations WHERE key='room_capability_packs_v1'"
                ).fetchone()[0]
                connection.execute(
                    "UPDATE rooms SET capability_packs_json='[]',updated_at=? WHERE id='room_project'",
                    (prior_applied_at,),
                )
                connection.execute(
                    "DELETE FROM schema_migrations WHERE key='project_default_capability_pack_v2'"
                )

            migrated = StudioStore(path)
            migrated_room = migrated.room_snapshot("room_project")["room"]
            self.assertEqual(
                migrated_room["capability_pack_ids"],
                ["structured_project_research"],
            )

            migrated.update_room("room_project", {
                "expected_updated_at": migrated_room["updated_at"],
                "capability_pack_ids": [],
            })
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    "DELETE FROM schema_migrations WHERE key='project_default_capability_pack_v2'"
                )

            reopened = StudioStore(path)
            self.assertEqual(
                reopened.room_snapshot("room_project")["room"]["capability_pack_ids"],
                [],
            )

    def test_storage_guardian_migration_upgrades_only_untouched_legacy_policy_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "storage.sqlite3"
            StudioStore(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    "DELETE FROM members WHERE room_id='room_storage' AND stance='data_guardian'"
                )
                connection.execute(
                    "UPDATE rooms SET workflow_policy_json=? WHERE id='room_storage'",
                    (json.dumps(LEGACY_STORAGE_WORKFLOW_POLICY_V1, ensure_ascii=False),),
                )
                connection.execute(
                    "DELETE FROM schema_migrations WHERE key='storage_data_guardian_role_v1'"
                )

            migrated = StudioStore(path)
            room = migrated.room_snapshot("room_storage")

            self.assertIn("data_guardian", [member["stance"] for member in room["members"]])
            self.assertEqual(
                room["room"]["workflow_policy"],
                default_workflow_policy("us_storage_committee"),
            )

            custom_policy = copy.deepcopy(room["room"]["workflow_policy"])
            custom_policy["follow_up_budget"] = 1
            migrated.update_room("room_storage", {
                "expected_updated_at": room["room"]["updated_at"],
                "workflow_policy": custom_policy,
            })
            guardian = next(
                member for member in migrated.room_snapshot("room_storage")["members"]
                if member["stance"] == "data_guardian"
            )
            migrated.delete_member("room_storage", guardian["id"])

            reopened = StudioStore(path)
            reopened_room = reopened.room_snapshot("room_storage")
            self.assertNotIn("data_guardian", [member["stance"] for member in reopened_room["members"]])
            self.assertEqual(reopened_room["room"]["workflow_policy"], custom_policy)

    def test_storage_guardian_migration_preserves_custom_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "custom.sqlite3"
            initial = StudioStore(path)
            custom_policy = default_workflow_policy("us_storage_committee")
            custom_policy["follow_up_budget"] = 1
            initial.update_room("room_storage", {
                "expected_updated_at": initial.room_snapshot("room_storage")["room"]["updated_at"],
                "workflow_policy": custom_policy,
            })
            guardian = next(
                member for member in initial.room_snapshot("room_storage")["members"]
                if member["stance"] == "data_guardian"
            )
            initial.delete_member("room_storage", guardian["id"])
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    "DELETE FROM schema_migrations WHERE key='storage_data_guardian_role_v1'"
                )

            migrated = StudioStore(path)
            room = migrated.room_snapshot("room_storage")

            self.assertIn("data_guardian", [member["stance"] for member in room["members"]])
            self.assertEqual(room["room"]["workflow_policy"], custom_policy)

    def test_malformed_legacy_policy_is_repaired_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy.sqlite3"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    """CREATE TABLE rooms(
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        objective TEXT NOT NULL DEFAULT '',
                        domain TEXT NOT NULL DEFAULT 'open_collaboration',
                        category TEXT NOT NULL DEFAULT '通用共创',
                        template_id TEXT NOT NULL DEFAULT 'open_collaboration',
                        discussion_mode TEXT NOT NULL DEFAULT 'dynamic',
                        workflow_policy_json TEXT NOT NULL DEFAULT '{}',
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL
                    )"""
                )
                connection.execute(
                    """INSERT INTO rooms(
                        id,title,objective,domain,category,template_id,discussion_mode,
                        workflow_policy_json,created_at,updated_at
                    ) VALUES('legacy','旧房间','旧目标','market_research','交易研究',
                             'us_storage_committee','dynamic','{bad json',1,1)"""
                )
            store = StudioStore(path)
            room = store.room_snapshot("legacy")["room"]
            self.assertEqual(room["workflow_policy"], default_workflow_policy("us_storage_committee"))
            self.assertEqual(
                room["capability_pack_ids"],
                ["storage_research_readonly", "structured_turn_contract_v1"],
            )
            self.assertIn("market.storage.readonly", room["capabilities"])
            self.assertIn("discussion.turn_contract_v1", room["capabilities"])
            with closing(sqlite3.connect(path)) as connection:
                persisted = connection.execute(
                    "SELECT workflow_policy_json FROM rooms WHERE id='legacy'"
                ).fetchone()[0]
            self.assertEqual(
                json.loads(persisted),
                default_workflow_policy("us_storage_committee"),
            )


class WorkflowPolicyHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_store = http_server.STORE
        http_server.STORE = StudioStore(Path(self.temp_dir.name) / "http.sqlite3")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), http_server.StudioRequestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        http_server.STORE = self.original_store
        self.temp_dir.cleanup()

    def patch_policy(self, policy: dict) -> tuple[int, dict]:
        room = http_server.STORE.room_snapshot("room_plan")["room"]
        request = Request(
            f"{self.base_url}/api/rooms/room_plan",
            method="PATCH",
            headers={
                "Content-Type": "application/json",
                "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
            },
            data=json.dumps({
                "expected_updated_at": room["updated_at"],
                "workflow_policy": policy,
            }).encode("utf-8"),
        )
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def patch_room(self, room_id: str, payload: dict) -> tuple[int, dict]:
        request = Request(
            f"{self.base_url}/api/rooms/{room_id}",
            method="PATCH",
            headers={
                "Content-Type": "application/json",
                "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
            },
            data=json.dumps(payload).encode("utf-8"),
        )
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def post_room(self, payload: dict) -> tuple[int, dict]:
        request = Request(
            f"{self.base_url}/api/rooms",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
            },
            data=json.dumps(payload).encode("utf-8"),
        )
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def test_patch_room_returns_structured_policy_and_rejects_boundary_bypass(self) -> None:
        policy = policy_variant(
            stages=["kickoff", "review"],
            stage_coverage={"kickoff": 1, "review": 1},
            minimum_successful_members=2,
        )
        status, payload = self.patch_policy(policy)
        self.assertEqual(status, 200)
        self.assertEqual(payload["room"]["workflow_policy"], policy)
        self.assertNotIn("workflow_policy_json", payload["room"])

        unsafe = copy.deepcopy(policy)
        unsafe["user_confirmation_required"] = False
        status, payload = self.patch_policy(unsafe)
        self.assertEqual(status, 400)
        self.assertIn("不可关闭", payload["error"])

    def test_create_room_accepts_known_pack_and_rejects_unknown_pack(self) -> None:
        status, payload = self.post_room({
            "title": "HTTP 存储小组",
            "objective": "验证领域能力包合同",
            "category": "交易研究 / 美股 / 自定义",
            "template_id": "open_collaboration",
            "capability_pack_ids": ["storage_research_readonly"],
        })
        self.assertEqual(status, 201)
        self.assertEqual(payload["room"]["template_id"], "open_collaboration")
        self.assertEqual(payload["room"]["category_path"], ["交易研究", "美股", "自定义"])
        self.assertIn("market.storage.readonly", payload["room"]["capabilities"])

        status, payload = self.post_room({
            "title": "危险能力",
            "objective": "不得创建",
            "template_id": "open_collaboration",
            "capability_pack_ids": ["order_execution"],
        })
        self.assertEqual(status, 400)
        self.assertIn("未知领域能力包", payload["error"])

    def test_room_settings_http_update_uses_conflict_for_stale_version(self) -> None:
        room = http_server.STORE.room_snapshot("room_plan")["room"]
        status, payload = self.patch_room("room_plan", {
            "expected_updated_at": room["updated_at"],
            "title": "HTTP 可编辑房间",
            "category": "项目研究 / HTTP",
            "capability_pack_ids": ["storage_research_readonly"],
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["room"]["title"], "HTTP 可编辑房间")
        self.assertEqual(payload["room"]["category_path"], ["项目研究", "HTTP"])
        self.assertIn("market.storage.readonly", payload["room"]["capabilities"])

        status, payload = self.patch_room("room_plan", {
            "expected_updated_at": room["updated_at"],
            "title": "过期 HTTP 写入",
        })
        self.assertEqual(status, 409)
        self.assertIn("已被其他操作更新", payload["error"])


if __name__ == "__main__":
    unittest.main()
