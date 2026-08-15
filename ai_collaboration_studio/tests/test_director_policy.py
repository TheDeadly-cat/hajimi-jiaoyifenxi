from __future__ import annotations

import unittest

from backend.director_policy import (
    DIRECTOR_CANDIDATE_LIMIT,
    DIRECTOR_SCHEDULING_CONTEXT_VERSION,
    RULES_FIRST_DIRECTOR_VERSION,
    build_director_scheduling_context,
    select_rules_first_director_decision,
    stage_frontier_eligible_members,
)


def member(
    member_id: str,
    name: str,
    *,
    capabilities: list[str] | None = None,
    stance: str = "neutral",
    workflow_stage: str = "analysis",
) -> dict[str, object]:
    return {
        "id": member_id,
        "name": name,
        "capabilities": capabilities or [],
        "stance": stance,
        "workflow_stage": workflow_stage,
    }


class RulesFirstDirectorPolicyTests(unittest.TestCase):
    def select(self, eligible: list[dict[str, object]], **overrides: object):
        inputs = {
            "eligible": eligible,
            "active_stage": "analysis",
            "stage_label": "分析取证",
            "post_coverage": False,
            "can_host_finish": False,
            "workspace_focus": None,
            "focus_covered": True,
            "role_coverage": [],
            "successful_member_ids": set(),
            "force_formal_speaker": False,
        }
        inputs.update(overrides)
        return select_rules_first_director_decision(**inputs)

    def test_safe_convergence_finishes_without_a_model(self) -> None:
        decision = self.select(
            [member("a", "分析师"), member("b", "风控")],
            post_coverage=True,
            can_host_finish=True,
        )

        self.assertEqual(decision["action"], "finish")
        self.assertEqual(decision["rule_id"], "safe_finish")
        self.assertEqual(decision["policy_version"], RULES_FIRST_DIRECTOR_VERSION)

    def test_forced_formal_speaker_prevents_safe_finish(self) -> None:
        only = member("risk", "风控负责人")
        decision = self.select(
            [only],
            post_coverage=True,
            can_host_finish=True,
            force_formal_speaker=True,
        )

        self.assertEqual(decision["action"], "speak")
        self.assertEqual(decision["member"]["id"], "risk")
        self.assertEqual(decision["rule_id"], "single_eligible")

    def test_unique_workspace_focus_match_is_selected(self) -> None:
        evidence = member("evidence", "证据分析师", capabilities=["evidence_review"])
        decision = self.select(
            [evidence, member("writer", "方案撰写员", capabilities=["writing"])],
            workspace_focus={
                "title": "补齐官方证据",
                "target_capabilities": ["evidence_review"],
            },
            focus_covered=False,
        )

        self.assertEqual(decision["member"]["id"], "evidence")
        self.assertEqual(decision["rule_id"], "unique_workspace_focus")

    def test_unique_member_that_closes_role_coverage_is_selected(self) -> None:
        analyst = member("analyst", "分析师")
        reviewer = member("reviewer", "反方审查员")
        decision = self.select(
            [analyst, reviewer],
            role_coverage=[{
                "id": "red_team",
                "label": "反方审查",
                "required_count": 1,
                "configured_count": 1,
                "successful_count": 0,
                "configured_member_ids": ["reviewer"],
                "successful_member_ids": [],
                "ready": False,
            }],
        )

        self.assertEqual(decision["member"]["id"], "reviewer")
        self.assertEqual(decision["rule_id"], "unique_role_coverage_closer")

    def test_semantic_tie_is_left_for_the_configured_moderator_model(self) -> None:
        decision = self.select([
            member("a", "分析师甲", capabilities=["evidence_review"]),
            member("b", "分析师乙", capabilities=["evidence_review"]),
        ], workspace_focus={
            "title": "补齐证据",
            "target_capabilities": ["evidence_review"],
        }, focus_covered=False)

        self.assertIsNone(decision)

    def test_already_successful_member_does_not_close_distinct_role_coverage(self) -> None:
        repeated = member("reviewer", "已发言审查员")
        other = member("analyst", "分析师")
        decision = self.select(
            [repeated, other],
            successful_member_ids={"reviewer"},
            role_coverage=[{
                "id": "red_team",
                "label": "双人反方审查",
                "required_count": 2,
                "configured_count": 2,
                "successful_count": 1,
                "configured_member_ids": ["reviewer", "another_reviewer"],
                "successful_member_ids": ["reviewer"],
                "ready": False,
            }],
        )

        self.assertIsNone(decision)

    def test_underconfigured_requirement_is_not_claimed_as_closable(self) -> None:
        reviewer = member("reviewer", "唯一审查员")
        analyst = member("analyst", "分析师")
        decision = self.select(
            [reviewer, analyst],
            role_coverage=[{
                "id": "red_team",
                "label": "双人反方审查",
                "required_count": 2,
                "configured_count": 1,
                "successful_count": 0,
                "configured_member_ids": ["reviewer"],
                "successful_member_ids": [],
                "ready": False,
            }],
        )

        self.assertIsNone(decision)

    def test_contradictory_already_satisfied_requirement_fails_closed(self) -> None:
        reviewer = member("reviewer", "审查员")
        analyst = member("analyst", "分析师")
        decision = self.select(
            [reviewer, analyst],
            role_coverage=[{
                "id": "red_team",
                "label": "反方审查",
                "required_count": 1,
                "configured_count": 1,
                "successful_count": 1,
                "configured_member_ids": ["reviewer"],
                "successful_member_ids": [],
                "ready": False,
            }],
        )

        self.assertIsNone(decision)

    def test_stage_frontier_blocks_risk_and_decision_before_plan(self) -> None:
        analyst = member(
            "analyst",
            "分析师",
            capabilities=["analysis"],
            workflow_stage="analysis",
        )
        reviewer = member(
            "reviewer",
            "风险审查员",
            capabilities=["risk_review", "evidence_review"],
            workflow_stage="risk",
        )
        planner = member(
            "planner",
            "方案规划员",
            capabilities=["planning"],
            workflow_stage="plan",
        )
        decider = member(
            "decider",
            "决策经理",
            capabilities=["decision_synthesis"],
            workflow_stage="decision",
        )
        stage_coverage = [
            {
                "id": "analysis",
                "required_count": 1,
                "successful_count": 1,
                "configured_member_ids": ["analyst"],
                "ready": True,
            },
            {
                "id": "plan",
                "required_count": 1,
                "successful_count": 0,
                "configured_member_ids": ["planner"],
                "ready": False,
            },
            {
                "id": "risk",
                "required_count": 1,
                "successful_count": 0,
                "configured_member_ids": ["reviewer"],
                "ready": False,
            },
            {
                "id": "decision",
                "required_count": 1,
                "successful_count": 0,
                "configured_member_ids": ["decider"],
                "ready": False,
            },
        ]
        eligible, active_stage = stage_frontier_eligible_members(
            unspoken=[planner, reviewer, decider],
            stage_order=["analysis", "plan", "risk", "decision"],
            stage_coverage=stage_coverage,
        )

        self.assertEqual(active_stage, "plan")
        self.assertEqual([item["id"] for item in eligible], ["planner"])

    def test_satisfied_stage_extras_compete_with_current_frontier(self) -> None:
        extra_analyst = member(
            "extra_analyst",
            "补充分析师",
            workflow_stage="analysis",
        )
        challenger = member(
            "challenger",
            "反方审查员",
            workflow_stage="debate",
        )
        risk = member("risk", "风险经理", workflow_stage="risk")

        eligible, active_stage = stage_frontier_eligible_members(
            unspoken=[extra_analyst, challenger, risk],
            stage_order=["analysis", "debate", "risk"],
            stage_coverage=[
                {"id": "analysis", "ready": True},
                {"id": "debate", "ready": False},
                {"id": "risk", "ready": False},
            ],
        )

        self.assertEqual(active_stage, "debate")
        self.assertEqual(
            [item["id"] for item in eligible],
            ["extra_analyst", "challenger"],
        )

    def test_unrepairable_earlier_stage_does_not_hide_downstream_members(self) -> None:
        analyst = member("analyst", "分析师", workflow_stage="analysis")
        risk = member("risk", "风险经理", workflow_stage="risk")

        eligible, active_stage = stage_frontier_eligible_members(
            unspoken=[analyst, risk],
            stage_order=["facilitate", "analysis", "risk"],
            stage_coverage=[
                {"id": "facilitate", "ready": False},
                {"id": "analysis", "ready": False},
                {"id": "risk", "ready": False},
            ],
        )

        self.assertEqual(active_stage, "analysis")
        self.assertEqual([item["id"] for item in eligible], ["analyst"])

    def test_incomplete_coverage_snapshot_keeps_dynamic_candidates(self) -> None:
        analyst = member("analyst", "分析师", workflow_stage="analysis")
        risk = member("risk", "风险经理", workflow_stage="risk")

        eligible, active_stage = stage_frontier_eligible_members(
            unspoken=[analyst, risk],
            stage_order=["analysis", "risk"],
            stage_coverage=[],
        )

        self.assertEqual(active_stage, "analysis")
        self.assertEqual([item["id"] for item in eligible], ["analyst", "risk"])

    def test_scheduling_context_has_bounded_budget_and_visible_call_plan(self) -> None:
        analyst = member("analyst", "分析师", workflow_stage="analysis")
        reviewer = member("reviewer", "审查员", workflow_stage="risk")
        context = build_director_scheduling_context(
            eligible=[analyst, reviewer],
            callable_members=[analyst, reviewer],
            stage_coverage=[
                {
                    "id": "analysis",
                    "required_count": 1,
                    "successful_count": 0,
                    "configured_member_ids": ["analyst"],
                    "ready": False,
                },
                {
                    "id": "risk",
                    "required_count": 1,
                    "successful_count": 0,
                    "configured_member_ids": ["reviewer"],
                    "ready": False,
                },
            ],
            role_coverage=[],
            successful_member_ids=set(),
            successful_member_count=0,
            required_success_count=2,
            workspace_focus=None,
            focus_covered=True,
            global_remaining_calls=2,
            director_remaining_calls=1,
        )

        self.assertEqual(context["eligible_member_ids"], ["analyst", "reviewer"])
        self.assertEqual(context["global_remaining_calls"], 2)
        self.assertEqual(context["director_remaining_calls"], 1)
        self.assertEqual(context["minimum_remaining_visible_speaker_calls"], 2)
        self.assertTrue(context["remaining_visible_plan_feasible"])

    def test_unresolved_convergence_reserves_one_visible_continuation(self) -> None:
        analyst = member("analyst", "分析师", workflow_stage="analysis")
        context = build_director_scheduling_context(
            eligible=[analyst],
            callable_members=[analyst],
            stage_coverage=[{"id": "analysis", "ready": True}],
            role_coverage=[],
            successful_member_ids={"analyst"},
            successful_member_count=1,
            required_success_count=1,
            workspace_focus=None,
            focus_covered=True,
            global_remaining_calls=1,
            director_remaining_calls=1,
            continuation_required=True,
        )

        self.assertEqual(context["minimum_remaining_visible_speaker_calls"], 1)

    def test_candidate_context_uses_the_sealed_256_route_ceiling(self) -> None:
        candidates = [
            member(f"member_{index:03d}", f"成员 {index}")
            for index in range(DIRECTOR_CANDIDATE_LIMIT + 44)
        ]

        context = build_director_scheduling_context(
            eligible=candidates,
            callable_members=candidates,
            stage_coverage=[],
            role_coverage=[],
            successful_member_ids=set(),
            successful_member_count=0,
            required_success_count=0,
            workspace_focus=None,
            focus_covered=True,
        )

        self.assertEqual(
            len(context["eligible_member_ids"]),
            DIRECTOR_CANDIDATE_LIMIT,
        )
        self.assertEqual(
            len(context["candidate_contributions"]),
            DIRECTOR_CANDIDATE_LIMIT,
        )
        self.assertNotIn(
            f"member_{DIRECTOR_CANDIDATE_LIMIT:03d}",
            context["eligible_member_ids"],
        )

    def test_next_round_only_focus_gets_one_deterministic_explainer(self) -> None:
        evidence_a = member(
            "evidence_a",
            "证据负责人甲",
            capabilities=["evidence_review"],
        )
        evidence_a["position"] = 1
        evidence_b = member(
            "evidence_b",
            "证据负责人乙",
            capabilities=["evidence_review"],
        )
        evidence_b["position"] = 2
        decision = self.select(
            [evidence_b, evidence_a],
            workspace_focus={
                "title": "冻结证据来源报错",
                "target_capabilities": ["evidence_review"],
                "repair_scope": "next_round_only",
            },
            focus_covered=False,
            scheduling_context={
                "workspace_focus_repair_scope": "next_round_only",
                "candidate_contributions": [
                    {
                        "member_id": "evidence_a",
                        "contribution_count": 1,
                        "gap_codes": ["workspace:frozen"],
                    },
                    {
                        "member_id": "evidence_b",
                        "contribution_count": 1,
                        "gap_codes": ["workspace:frozen"],
                    },
                ],
            },
        )

        self.assertEqual(decision["member"]["id"], "evidence_a")
        self.assertEqual(decision["rule_id"], "unrepairable_focus_explanation")
        self.assertIn("不得把文字说明冒充已修复", decision["reason"])


if __name__ == "__main__":
    unittest.main()
