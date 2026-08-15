from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from backend.convergence import ConvergenceService
from backend.orchestrator import DiscussionOrchestrator
from backend.store import StudioStore
from backend.turn_contract import TURN_CONTRACT_VERSION
from backend.workflow_policy import default_workflow_policy


def _candidate(action: str) -> dict[str, Any]:
    return {
        "id": "option_a",
        "title": "候选 A",
        "action": action,
        "symbol": "",
        "direction": "UNSPECIFIED",
        "horizon_days": 20,
        "thesis": "以可逆、只读的模拟步骤验证核心假设。",
        "invalidation": "关键假设被新证据否定。",
        "evidence": [],
    }


def _contract(candidate_updates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": TURN_CONTRACT_VERSION,
        "claims": [],
        "responds_to": [],
        "candidate_updates": candidate_updates,
        "risks": [],
        "next_actions": [],
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


def _formal_message(
    message_id: str,
    round_id: str,
    member: dict[str, Any],
    candidate_updates: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "id": message_id,
        "round_id": round_id,
        "sender_type": "ai",
        "sender_id": str(member["id"]),
        "sender_name": str(member["name"]),
        "member_version": int(member["version"]),
        "is_formal_round_turn": True,
        "turn_contract_version": TURN_CONTRACT_VERSION,
        "turn_contract": _contract(candidate_updates),
        "turn_contract_qualified": True,
        "turn_contract_issues": [],
        "turn_contract_integrity_ok": True,
    }


class ProviderCallForbiddenRegistry:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, _provider_id: str) -> Any:
        self.calls += 1
        raise AssertionError("candidate-lineage scheduling must remain offline")


class CandidateLineageSchedulingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="ai-studio-p20-test-")
        self.store = StudioStore(Path(self.temp_dir.name) / "p20.sqlite3")
        self.service = ConvergenceService(self.store)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _lineage_state(
        self,
        messages: list[dict[str, Any]],
        round_id: str,
    ) -> dict[str, Any]:
        bundle = {
            "applicable": True,
            "valid": True,
            "messages": messages,
            "issues": [],
            "candidate_risk_review_version": "",
        }
        with (
            patch.object(
                self.store,
                "round_turn_contract_bundle",
                return_value=bundle,
            ),
            patch.object(self.store, "round_messages", return_value=messages),
        ):
            return self.service.evaluate("room_plan", round_id=round_id)

    def test_candidate_lineage_focus_routes_quantity_and_decision_gaps(self) -> None:
        members = self.store.enabled_members("room_plan")
        planner = next(
            member
            for member in members
            if member.get("workflow_stage") != "decision"
            and "facilitation" not in (member.get("capabilities") or [])
        )
        decision_member = next(
            member
            for member in members
            if "decision_synthesis" in (member.get("capabilities") or [])
        )
        round_row = self.store.create_round(
            "room_plan",
            "离线验证候选谱系焦点",
            turn_contract_version=TURN_CONTRACT_VERSION,
        )
        proposed = _formal_message(
            "msg_plan_a",
            str(round_row["id"]),
            planner,
            [_candidate("propose")],
        )
        selected = _formal_message(
            "msg_decision_a",
            str(round_row["id"]),
            decision_member,
            [_candidate("select")],
        )

        one_candidate = self._lineage_state(
            [proposed, selected],
            str(round_row["id"]),
        )["candidate_lineage_gate"]

        self.assertFalse(one_candidate["ready"])
        self.assertEqual(one_candidate["candidate_count"], 1)
        self.assertEqual(
            one_candidate["blockers"][0]["code"],
            "CANDIDATE_LINEAGE_COMPARISON_INSUFFICIENT",
        )
        self.assertEqual(one_candidate["focus"], {
            "code": "CANDIDATE_LINEAGE_COMPARISON_INSUFFICIENT",
            "title": "候选方案数量不足",
            "detail": "决策前至少需要两个具有合格来源的候选对象。",
            "target_capabilities": ["simulation_planning"],
            "target_stances": ["trader"],
            "repair_scope": "in_round",
            "coverage_mode": "until_resolved",
            "routing_priority": "candidate_repair",
        })

        decision_missing = self._lineage_state(
            [proposed],
            str(round_row["id"]),
        )["candidate_lineage_gate"]

        self.assertEqual(
            decision_missing["focus"]["code"],
            "CANDIDATE_LINEAGE_DECISION_MISSING",
        )
        self.assertEqual(
            decision_missing["focus"]["target_capabilities"],
            ["decision_synthesis"],
        )
        self.assertEqual(
            decision_missing["focus"]["target_stances"],
            ["portfolio_manager"],
        )
        self.assertEqual(decision_missing["focus"]["repair_scope"], "in_round")
        self.assertEqual(
            decision_missing["focus"]["coverage_mode"],
            "until_resolved",
        )
        self.assertEqual(
            decision_missing["focus"]["routing_priority"],
            "after_project",
        )

    def test_one_candidate_revisits_planner_before_decision_member_without_provider(self) -> None:
        members = self.store.enabled_members("room_plan")
        moderator = self.store.update_member(
            "room_plan",
            str(members[0]["id"]),
            {"provider": "deepseek", "model": "offline-model"},
        )
        planner_source = next(
            member
            for member in self.store.enabled_members("room_plan")
            if "evidence_review" in (member.get("capabilities") or [])
        )
        planner = self.store.update_member(
            "room_plan",
            str(planner_source["id"]),
            {
                "stance": "trader",
                "capabilities": ["evidence_review", "simulation_planning"],
            },
        )
        members = self.store.enabled_members("room_plan")
        decision_member = next(
            member
            for member in members
            if "decision_synthesis" in (member.get("capabilities") or [])
        )
        room = dict(self.store.room_snapshot("room_plan")["room"])
        room.update({
            "discussion_mode": "dynamic",
            "moderator_member_id": str(moderator["id"]),
            "moderator_member_version": int(moderator["version"]),
            "moderator_provider": "deepseek",
            "moderator_model": "offline-model",
            "moderator_approved_route": {},
        })
        round_row = self.store.create_round(
            "room_plan",
            "离线验证单候选回访规划角色",
        )
        lineage_focus = {
            "code": "CANDIDATE_LINEAGE_COMPARISON_INSUFFICIENT",
            "title": "候选方案数量不足",
            "detail": "决策前至少需要两个具有合格来源的候选对象。",
            "target_capabilities": ["simulation_planning"],
            "target_stances": ["trader"],
            "repair_scope": "in_round",
            "coverage_mode": "until_resolved",
            "routing_priority": "candidate_repair",
        }
        convergence = {
            "project_workspace": {
                "focus": {
                    "code": "PROJECT_RECOMMENDATION_INCOMPLETE",
                    "title": "候选首选或理由不完整",
                    "target_capabilities": ["decision_synthesis"],
                },
            },
            "research_evidence_gate": {"focus": None},
            "candidate_lineage_gate": {
                "ready": False,
                "candidate_count": 1,
                "focus": lineage_focus,
                "blockers": [lineage_focus],
            },
            "candidate_risk_review_gate": {
                "focus": {
                    "code": "CANDIDATE_RISK_REVIEW_MISSING",
                    "title": "候选风险复核不足",
                    "target_capabilities": ["critical_review"],
                    "repair_scope": "in_round",
                },
            },
            "can_host_finish": False,
            "discussion_gate": {
                "ready": True,
                "successful_member_count": len(members),
                "required_success_count": len(members),
                "stage_coverage": [],
                "role_coverage": [],
                "blockers": [{"title": "候选方案数量不足"}],
            },
        }
        spoken_counts = {str(member["id"]): 1 for member in members}
        spoken_counts[str(planner["id"])] = 2
        successful_member_ids = set(spoken_counts)
        registry = ProviderCallForbiddenRegistry()
        orchestrator = DiscussionOrchestrator(
            self.store,
            registry,
            market_service=None,
        )

        self.assertFalse(orchestrator._workspace_focus_covered(
            lineage_focus,
            members,
            successful_member_ids,
            spoken_counts=spoken_counts,
        ))
        workflow_policy = default_workflow_policy("open_collaboration")
        # The coverage assertion is independent of the room's hard turn cap;
        # leave one bounded turn available so routing can prove the focus wins.
        workflow_policy["max_turns_per_member"] = 3
        with patch.object(
            orchestrator,
            "_convergence_state",
            return_value=convergence,
        ):
            selection = orchestrator._select_next_member(
                room,
                workflow_policy,
                "离线验证单候选回访规划角色",
                members,
                spoken_counts,
                {str(member.get("stance") or "") for member in members},
                successful_member_ids,
                set(),
                len(members),
                round_id=str(round_row["id"]),
            )

        self.assertEqual(registry.calls, 0)
        self.assertEqual(selection["action"], "speak")
        self.assertEqual(selection["source"], "rules_first")
        self.assertEqual(selection["member"]["id"], planner["id"])
        self.assertNotEqual(selection["member"]["id"], decision_member["id"])
        self.assertEqual(
            selection["workspace_focus"]["code"],
            "CANDIDATE_LINEAGE_COMPARISON_INSUFFICIENT",
        )
        self.assertIn(
            "workspace:candidate_lineage_comparison_insufficient",
            selection["scheduling_context"]["gap_codes"],
        )

        self.assertTrue(orchestrator._workspace_focus_covered(
            None,
            members,
            successful_member_ids,
            spoken_counts=spoken_counts,
        ))

    def test_workspace_priority_defers_only_routine_decision_missing(self) -> None:
        project_focus = {
            "code": "PROJECT_BLOCKING_RISK_OPEN",
            "target_capabilities": ["critical_review"],
        }
        decision_missing = {
            "code": "CANDIDATE_LINEAGE_DECISION_MISSING",
            "routing_priority": "after_project",
        }
        urgent_repair = {
            "code": "CANDIDATE_LINEAGE_COMPARISON_INSUFFICIENT",
            "routing_priority": "candidate_repair",
        }

        self.assertIs(
            DiscussionOrchestrator._prioritized_workspace_focus(
                research_focus=None,
                candidate_lineage_focus=decision_missing,
                candidate_risk_review_focus=None,
                project_focus=project_focus,
            ),
            project_focus,
        )
        self.assertIs(
            DiscussionOrchestrator._prioritized_workspace_focus(
                research_focus=None,
                candidate_lineage_focus=urgent_repair,
                candidate_risk_review_focus=None,
                project_focus=project_focus,
            ),
            urgent_repair,
        )

    def test_project_option_generation_does_not_steal_decision_synthesis_gaps(self) -> None:
        artifact = {
            "id": "artifact_fixture",
            "version": 1,
            "status": "DRAFT",
            "content": {
                "requirements": [{
                    "id": "requirement_a",
                    "text": "验证核心流程。",
                    "status": "confirmed",
                    "acceptance_criteria": "离线用例通过。",
                }],
                "risks": [{
                    "id": "risk_a",
                    "text": "验证样本有限。",
                    "status": "mitigated",
                    "blocking": False,
                    "trigger": "样本不足。",
                    "mitigation": "扩大离线样本。",
                }],
                "decision": {
                    "status": "candidate",
                    "options": [{
                        "id": "option_a",
                        "title": "小范围验证",
                        "description": "验证一条核心流程。",
                        "value": "验证",
                        "cost": "低",
                        "timeline": "两周",
                        "dependencies": ["用户"],
                        "reversibility": "high",
                    }],
                    "preferred_option_id": "option_a",
                    "rationale": "先走可逆路径。",
                },
            },
        }

        one_option = self.service._project_workspace_snapshot(
            artifact,
            applicable=True,
            frozen=True,
        )

        self.assertEqual(
            one_option["focus"]["code"],
            "PROJECT_OPTIONS_INSUFFICIENT",
        )
        self.assertEqual(
            one_option["focus"]["target_capabilities"],
            ["simulation_planning"],
        )

        incomplete_decision = copy.deepcopy(artifact)
        incomplete_decision["content"]["decision"].update({
            "status": "undecided",
            "preferred_option_id": "",
            "rationale": "",
        })
        incomplete_decision["content"]["decision"]["options"].append({
            "id": "option_b",
            "title": "完整交付",
            "description": "实现全部规划范围。",
            "value": "覆盖",
            "cost": "高",
            "timeline": "",
            "dependencies": ["资源"],
            "reversibility": "low",
        })
        decision_gaps = self.service._project_workspace_snapshot(
            incomplete_decision,
            applicable=True,
            frozen=True,
        )["gaps"]
        by_code = {gap["code"]: gap for gap in decision_gaps}

        self.assertEqual(
            by_code["PROJECT_MATRIX_INCOMPLETE"]["target_capabilities"],
            ["decision_synthesis"],
        )
        self.assertEqual(
            by_code["PROJECT_RECOMMENDATION_INCOMPLETE"]["target_capabilities"],
            ["decision_synthesis"],
        )


if __name__ == "__main__":
    unittest.main()
