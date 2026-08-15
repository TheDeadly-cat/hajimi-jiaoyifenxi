from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend.artifact_service import ArtifactService
from backend.store import StudioStore
from backend.turn_contract import CANDIDATE_RISK_REVIEW_VERSION, TURN_CONTRACT_VERSION
from backend.turn_contract_artifact import (
    candidate_risk_review_prompt_snapshot,
    decision_candidate_prompt_snapshot,
    project_turn_contract_artifact,
)


def contract(candidate_updates: list[dict], *, risks: list[dict] | None = None) -> dict:
    return {
        "version": TURN_CONTRACT_VERSION,
        "claims": [],
        "responds_to": [],
        "candidate_updates": candidate_updates,
        "risks": list(risks or []),
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


def candidate(
    candidate_id: str,
    action: str,
    *,
    title: str,
    thesis: str,
    evidence_id: str = "",
    evidence_type: str = "message",
) -> dict:
    return {
        "id": candidate_id,
        "title": title,
        "action": action,
        "symbol": "",
        "direction": "UNSPECIFIED",
        "horizon_days": 20,
        "thesis": thesis,
        "invalidation": f"{title} 的关键假设不再成立",
        "evidence": (
            [{"type": evidence_type, "id": evidence_id, "role": "support"}]
            if evidence_id
            else []
        ),
    }


def formal_message(
    message_id: str,
    sender_id: str,
    member_version: int,
    payload: dict,
) -> dict:
    return {
        "id": message_id,
        "sender_type": "ai",
        "sender_id": sender_id,
        "sender_name": sender_id,
        "member_version": member_version,
        "is_formal_round_turn": True,
        "turn_contract_version": TURN_CONTRACT_VERSION,
        "turn_contract": payload,
        "turn_contract_qualified": True,
        "turn_contract_issues": [],
        "turn_contract_integrity_ok": True,
    }


def responding(payload: dict, *message_ids: str) -> dict:
    payload["responds_to"] = [
        {
            "type": "message",
            "id": message_id,
            "relation": "qualifies",
            "reason": "精确复核该候选当前版本。",
        }
        for message_id in message_ids
    ]
    return payload


class DisabledProvider:
    def status(self) -> dict:
        return {"configured": False}

    def generate(self, **_: object) -> object:
        raise AssertionError("disabled provider must not be called")


class DisabledRegistry:
    def get(self, _: str) -> DisabledProvider:
        return DisabledProvider()


class TurnContractArtifactTests(unittest.TestCase):
    def test_decision_prompt_snapshot_uses_only_sealed_non_decision_candidates(self) -> None:
        proposed = formal_message(
            "msg_plan",
            "planner",
            1,
            contract([
                candidate("option_a", "propose", title="方案 A", thesis="冻结方案 A"),
                candidate("option_b", "propose", title="方案 B", thesis="冻结方案 B"),
            ]),
        )
        revised = formal_message(
            "msg_revision",
            "planner",
            1,
            contract([
                candidate("option_a", "revise", title="方案 A v2", thesis="冻结方案 A 第二版"),
            ]),
        )
        corrupt = formal_message(
            "msg_corrupt",
            "planner",
            1,
            contract([
                candidate("option_corrupt", "propose", title="损坏候选", thesis="不得进入提示"),
            ]),
        )
        corrupt["turn_contract_integrity_ok"] = False
        decision_origin = formal_message(
            "msg_old_decision",
            "decision",
            1,
            contract([
                candidate("option_invented", "propose", title="决策自造", thesis="不得成为来源"),
            ]),
        )
        resolver = lambda member_id, _: {
            "workflow_stage": "decision" if member_id == "decision" else "plan"
        }

        snapshot = decision_candidate_prompt_snapshot(
            [proposed, revised, corrupt, decision_origin],
            target_member={"workflow_stage": "decision"},
            member_resolver=resolver,
        )

        self.assertIsNotNone(snapshot)
        self.assertTrue(snapshot["read_only"])
        self.assertTrue(snapshot["ready"])
        self.assertEqual(snapshot["candidate_count"], 2)
        self.assertEqual(snapshot["source_message_ids"], ["msg_plan", "msg_revision"])
        self.assertEqual(
            [item["id"] for item in snapshot["candidates"]],
            ["option_a", "option_b"],
        )
        option_a = snapshot["candidates"][0]
        self.assertEqual(option_a["revision"], 2)
        self.assertEqual(option_a["origin_message_id"], "msg_plan")
        self.assertEqual(option_a["latest_message_id"], "msg_revision")
        self.assertEqual(option_a["title"], "方案 A v2")
        self.assertEqual(option_a["thesis"], "冻结方案 A 第二版")
        self.assertNotIn("option_corrupt", {item["id"] for item in snapshot["candidates"]})
        self.assertNotIn("option_invented", {item["id"] for item in snapshot["candidates"]})
        self.assertIsNone(decision_candidate_prompt_snapshot(
            [proposed],
            target_member={"workflow_stage": "plan"},
            member_resolver=resolver,
        ))

    def test_risk_prompt_snapshot_is_canonical_and_risk_audience_only(self) -> None:
        proposed = formal_message(
            "msg_plan",
            "planner",
            1,
            contract([
                candidate("option_a", "propose", title="方案 A", thesis="冻结方案 A"),
            ]),
        )
        revised = formal_message(
            "msg_revision",
            "planner",
            1,
            contract([
                candidate("option_a", "revise", title="方案 A v2", thesis="冻结方案 A 第二版"),
            ]),
        )
        resolver = lambda *_: {"workflow_stage": "plan"}

        snapshot = candidate_risk_review_prompt_snapshot(
            [proposed, revised],
            target_member={"workflow_stage": "risk", "capabilities": ["risk_review"]},
            member_resolver=resolver,
        )

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["version"], CANDIDATE_RISK_REVIEW_VERSION)
        self.assertTrue(snapshot["read_only"])
        self.assertEqual(snapshot["allowed_review_actions"], ["support", "challenge", "reject"])
        self.assertEqual(snapshot["responds_to_required"], "candidate.latest_message_id")
        self.assertEqual(
            snapshot["immutable_fields"],
            ["id", "title", "symbol", "direction", "horizon_days", "thesis", "invalidation"],
        )
        self.assertEqual(snapshot["candidates"][0]["revision"], 2)
        self.assertEqual(snapshot["candidates"][0]["latest_message_id"], "msg_revision")
        self.assertEqual(snapshot["candidates"][0]["title"], "方案 A v2")
        self.assertFalse(snapshot["can_autonomously_decide"])
        self.assertIsNone(candidate_risk_review_prompt_snapshot(
            [proposed],
            target_member={"workflow_stage": "plan"},
            member_resolver=resolver,
        ))
        self.assertIsNone(candidate_risk_review_prompt_snapshot(
            [proposed],
            target_member={"workflow_stage": "decision"},
            member_resolver=resolver,
        ))

    def test_decision_snapshot_exposes_only_current_sealed_risk_reviews(self) -> None:
        proposed = formal_message(
            "msg_plan",
            "planner",
            1,
            contract([
                candidate("option_a", "propose", title="方案 A", thesis="A 第一版"),
                candidate("option_b", "propose", title="方案 B", thesis="B 第一版"),
            ]),
        )
        valid_review = formal_message(
            "msg_risk",
            "risk",
            3,
            responding(
                contract([
                    candidate("option_a", "support", title="方案 A", thesis="A 第一版"),
                    candidate("option_b", "challenge", title="方案 B", thesis="B 第一版"),
                ]),
                "msg_plan",
            ),
        )
        valid_review["member_snapshot"] = {
            "workflow_stage": "risk",
            "stance": "risk",
            "capabilities": ["risk_review"],
        }
        valid_review["member_snapshot_integrity_ok"] = True
        nonrisk_review = formal_message(
            "msg_nonrisk",
            "analyst",
            1,
            responding(
                contract([
                    candidate("option_b", "support", title="方案 B", thesis="B 第一版"),
                ]),
                "msg_plan",
            ),
        )
        nonrisk_review["member_snapshot"] = {
            "workflow_stage": "analysis",
            "capabilities": ["evidence_review"],
        }
        nonrisk_review["member_snapshot_integrity_ok"] = True
        revised = formal_message(
            "msg_revision",
            "planner",
            1,
            contract([
                candidate("option_a", "revise", title="方案 A v2", thesis="A 第二版"),
            ]),
        )
        stale_reference = formal_message(
            "msg_stale_risk",
            "risk",
            4,
            responding(
                contract([
                    candidate("option_a", "reject", title="方案 A", thesis="A 第一版"),
                ]),
                "msg_revision",
            ),
        )
        stale_reference["member_snapshot"] = {
            "workflow_stage": "risk",
            "capabilities": ["risk_review"],
        }
        stale_reference["member_snapshot_integrity_ok"] = True

        snapshot = decision_candidate_prompt_snapshot(
            [proposed, valid_review, nonrisk_review, revised, stale_reference],
            target_member={"workflow_stage": "decision"},
            member_resolver=lambda *_: {"workflow_stage": "plan"},
        )

        self.assertEqual(
            snapshot["candidate_risk_review_version"],
            CANDIDATE_RISK_REVIEW_VERSION,
        )
        by_id = {item["id"]: item for item in snapshot["candidates"]}
        self.assertEqual(by_id["option_a"]["revision"], 2)
        self.assertEqual(by_id["option_a"]["current_risk_reviews"], [])
        self.assertEqual(by_id["option_b"]["current_risk_reviews"], [{
            "review_message_id": "msg_risk",
            "action": "challenge",
            "reviewer_member_id": "risk",
            "candidate_revision": 1,
        }])
        self.assertNotIn(
            "msg_nonrisk",
            {
                review["review_message_id"]
                for item in snapshot["candidates"]
                for review in item["current_risk_reviews"]
            },
        )
        self.assertNotIn(
            "msg_stale_risk",
            {
                review["review_message_id"]
                for item in snapshot["candidates"]
                for review in item["current_risk_reviews"]
            },
        )

    def test_exact_current_risk_reviews_bind_revision_and_unlock_decision(self) -> None:
        proposed = formal_message(
            "msg_plan",
            "planner",
            1,
            contract([
                candidate("option_a", "propose", title="方案 A", thesis="执行 A"),
                candidate("option_b", "propose", title="方案 B", thesis="执行 B"),
            ]),
        )
        risk_review = formal_message(
            "msg_risk",
            "risk",
            2,
            responding(
                contract([
                    candidate("option_a", "support", title="方案 A", thesis="执行 A"),
                    candidate("option_b", "challenge", title="方案 B", thesis="执行 B"),
                ], risks=[{
                    "id": "risk_a",
                    "text": "A 仍需观察",
                    "severity": "medium",
                    "status": "monitoring",
                    "trigger": "假设失效",
                    "mitigation": "停止模拟",
                    "blocking": True,
                    "evidence": [],
                }]),
                "msg_plan",
            ),
        )
        decision = formal_message(
            "msg_decision",
            "decision",
            1,
            contract([
                candidate(
                    "option_a",
                    "select",
                    title="方案 A",
                    thesis="执行 A",
                    evidence_id="msg_risk",
                ),
                candidate("option_b", "reject", title="方案 B", thesis="执行 B"),
            ]),
        )
        resolver = lambda member_id, _: {
            "workflow_stage": (
                "risk" if member_id == "risk" else "decision" if member_id == "decision" else "plan"
            ),
            "capabilities": ["risk_review"] if member_id == "risk" else [],
        }

        projection = project_turn_contract_artifact(
            [proposed, risk_review, decision],
            member_resolver=resolver,
            candidate_risk_review_required=True,
        )

        review_gate = projection["candidate_risk_reviews"]
        self.assertTrue(review_gate["applicable"])
        self.assertTrue(review_gate["ready"], review_gate["issues"])
        self.assertEqual(review_gate["status"], "ready")
        self.assertEqual(review_gate["target_candidate_ids"], ["option_a", "option_b"])
        self.assertEqual(review_gate["reviewed_candidate_count"], 2)
        self.assertEqual(review_gate["review_count"], 2)
        self.assertEqual(review_gate["stale_review_count"], 0)
        self.assertEqual(review_gate["action_counts"], {
            "support": 1,
            "challenge": 1,
            "reject": 0,
        })
        self.assertTrue(all(item["candidate_revision"] == 1 for item in review_gate["reviews"]))
        self.assertTrue(all(len(item["candidate_snapshot_sha256"]) == 64 for item in review_gate["reviews"]))
        self.assertTrue(review_gate["review_actions_are_dispositions_only"])
        self.assertFalse(review_gate["can_autonomously_decide"])
        self.assertEqual(projection["decision"]["status"], "candidate")
        self.assertEqual(projection["decision"]["preferred_option_id"], "option_a")

    def test_only_exact_decision_role_can_select_projected_candidate(self) -> None:
        messages = [
            formal_message(
                "msg_plan",
                "planner",
                1,
                contract(
                    [
                        candidate("option_small", "propose", title="小范围验证", thesis="先验证核心假设"),
                        candidate("option_full", "propose", title="完整范围", thesis="一次覆盖全部需求"),
                    ]
                ),
            ),
            formal_message(
                "msg_decision",
                "decision",
                3,
                contract(
                    [
                        candidate("option_small", "select", title="小范围验证", thesis="先验证核心假设"),
                        candidate("option_full", "reject", title="完整范围", thesis="一次覆盖全部需求"),
                    ],
                    risks=[{
                        "id": "risk_scope",
                        "text": "小范围验证可能遗漏边界需求",
                        "severity": "medium",
                        "status": "open",
                        "trigger": "验证范围无法覆盖关键路径",
                        "mitigation": "在进入下一阶段前补充边界用例",
                        "blocking": True,
                        "evidence": [],
                    }],
                ),
            ),
        ]

        projection = project_turn_contract_artifact(
            messages,
            member_resolver=lambda member_id, version: (
                {"workflow_stage": "decision", "capabilities": ["decision_synthesis"]}
                if member_id == "decision" and version == 3
                else {"workflow_stage": "plan", "capabilities": ["simulation_planning"]}
            ),
        )

        decision = projection["decision"]
        self.assertEqual(projection["qualified_message_count"], 2)
        self.assertTrue(projection["candidate_lineage"]["ready"])
        self.assertEqual(projection["candidate_lineage"]["version"], "candidate_lineage_v1")
        self.assertEqual({item["id"] for item in decision["options"]}, {"option_small", "option_full"})
        self.assertEqual(decision["status"], "candidate")
        self.assertEqual(decision["preferred_option_id"], "option_small")
        self.assertIn("先验证核心假设", decision["rationale"])
        self.assertEqual(
            next(item for item in decision["options"] if item["id"] == "option_small")["lineage"],
            {
                "version": "candidate_lineage_v1",
                "origin_message_id": "msg_plan",
                "latest_message_id": "msg_plan",
                "revision": 1,
            },
        )
        self.assertEqual(projection["risks"][0]["impact"], "medium")
        self.assertFalse(projection["live_trading_allowed"])

    def test_decision_cannot_create_or_rewrite_candidate_objects(self) -> None:
        self_invented = project_turn_contract_artifact(
            [formal_message(
                "msg_decision",
                "decision",
                1,
                contract([
                    candidate("option_a", "select", title="方案 A", thesis="决策现场创建 A"),
                    candidate("option_b", "reject", title="方案 B", thesis="决策现场创建 B"),
                ]),
            )],
            member_resolver=lambda *_: {"workflow_stage": "decision"},
        )
        invented_codes = {
            issue["code"] for issue in self_invented["candidate_lineage"]["issues"]
        }
        self.assertFalse(self_invented["candidate_lineage"]["ready"])
        self.assertIn("CANDIDATE_LINEAGE_SOURCE_MISSING", invented_codes)
        self.assertEqual(self_invented["decision"]["status"], "undecided")
        self.assertEqual(self_invented["decision"]["options"], [])

        rewritten = project_turn_contract_artifact(
            [
                formal_message(
                    "msg_plan",
                    "planner",
                    1,
                    contract([
                        candidate("option_a", "propose", title="方案 A", thesis="冻结方案 A"),
                        candidate("option_b", "propose", title="方案 B", thesis="冻结方案 B"),
                    ]),
                ),
                formal_message(
                    "msg_decision",
                    "decision",
                    1,
                    contract([
                        candidate("option_a", "select", title="方案 A", thesis="被决策者改写的 A"),
                        candidate("option_b", "reject", title="方案 B", thesis="冻结方案 B"),
                    ]),
                ),
            ],
            member_resolver=lambda member_id, _: {
                "workflow_stage": "decision" if member_id == "decision" else "plan"
            },
        )
        rewrite_issue = next(
            issue
            for issue in rewritten["candidate_lineage"]["issues"]
            if issue["code"] == "CANDIDATE_LINEAGE_REWRITE_FORBIDDEN"
        )
        self.assertEqual(rewrite_issue["candidate_id"], "option_a")
        self.assertEqual(rewrite_issue["changed_fields"], ["thesis"])
        self.assertFalse(rewritten["candidate_lineage"]["ready"])
        self.assertEqual(rewritten["decision"]["status"], "undecided")

    def test_risk_review_rewrite_and_self_invention_fail_closed(self) -> None:
        proposed = formal_message(
            "msg_plan",
            "planner",
            1,
            contract([
                candidate("option_a", "propose", title="方案 A", thesis="冻结 A"),
                candidate("option_b", "propose", title="方案 B", thesis="冻结 B"),
            ]),
        )
        invalid_review = formal_message(
            "msg_risk",
            "risk",
            1,
            responding(
                contract([
                    candidate("option_a", "challenge", title="方案 A", thesis="风控擅自改写 A"),
                    candidate("option_ghost", "reject", title="幽灵方案", thesis="没有来源"),
                ]),
                "msg_plan",
            ),
        )
        decision = formal_message(
            "msg_decision",
            "decision",
            1,
            contract([
                candidate("option_a", "select", title="方案 A", thesis="冻结 A", evidence_id="msg_risk"),
                candidate("option_b", "reject", title="方案 B", thesis="冻结 B"),
            ]),
        )
        projection = project_turn_contract_artifact(
            [proposed, invalid_review, decision],
            member_resolver=lambda member_id, _: {
                "workflow_stage": (
                    "risk" if member_id == "risk" else "decision" if member_id == "decision" else "plan"
                )
            },
            candidate_risk_review_required=True,
        )

        gate = projection["candidate_risk_reviews"]
        codes = {item["code"] for item in gate["issues"]}
        self.assertIn("CANDIDATE_RISK_REVIEW_REWRITE_FORBIDDEN", codes)
        self.assertIn("CANDIDATE_RISK_REVIEW_SOURCE_MISSING", codes)
        self.assertIn("CANDIDATE_RISK_REVIEW_MISSING", codes)
        rewrite = next(
            item for item in gate["issues"]
            if item["code"] == "CANDIDATE_RISK_REVIEW_REWRITE_FORBIDDEN"
        )
        self.assertEqual(rewrite["changed_fields"], ["thesis"])
        self.assertFalse(gate["ready"])
        self.assertEqual(gate["review_count"], 0)
        self.assertEqual(projection["decision"]["status"], "undecided")

    def test_risk_review_becomes_stale_after_candidate_revision(self) -> None:
        proposed = formal_message(
            "msg_plan",
            "planner",
            1,
            contract([
                candidate("option_a", "propose", title="方案 A", thesis="A 第一版"),
                candidate("option_b", "propose", title="方案 B", thesis="B 第一版"),
            ]),
        )
        first_review = formal_message(
            "msg_risk_v1",
            "risk",
            1,
            responding(
                contract([
                    candidate("option_a", "support", title="方案 A", thesis="A 第一版"),
                    candidate("option_b", "challenge", title="方案 B", thesis="B 第一版"),
                ]),
                "msg_plan",
            ),
        )
        revised = formal_message(
            "msg_revision",
            "planner",
            1,
            contract([
                candidate("option_a", "revise", title="方案 A v2", thesis="A 第二版"),
            ]),
        )
        decision = formal_message(
            "msg_decision",
            "decision",
            1,
            contract([
                candidate(
                    "option_a",
                    "select",
                    title="方案 A v2",
                    thesis="A 第二版",
                    evidence_id="msg_risk_v1",
                ),
                candidate("option_b", "reject", title="方案 B", thesis="B 第一版"),
            ]),
        )
        resolver = lambda member_id, _: {
            "workflow_stage": (
                "risk" if member_id == "risk" else "decision" if member_id == "decision" else "plan"
            )
        }
        projection = project_turn_contract_artifact(
            [proposed, first_review, revised, decision],
            member_resolver=resolver,
            candidate_risk_review_required=True,
        )

        gate = projection["candidate_risk_reviews"]
        self.assertFalse(gate["ready"])
        self.assertEqual(gate["stale_review_count"], 1)
        stale = next(item for item in gate["reviews"] if item["candidate_id"] == "option_a")
        self.assertEqual(stale["candidate_revision"], 1)
        self.assertEqual(stale["current_candidate_revision"], 2)
        self.assertEqual(stale["status"], "stale")
        stale_issue = next(
            item for item in gate["issues"]
            if item["code"] == "CANDIDATE_RISK_REVIEW_STALE"
        )
        self.assertEqual(stale_issue["candidate_id"], "option_a")
        self.assertEqual(stale_issue["reviewed_revisions"], [1])
        self.assertEqual(stale_issue["current_revision"], 2)
        self.assertEqual(projection["decision"]["status"], "undecided")

        old_reference = formal_message(
            "msg_risk_old",
            "risk",
            1,
            responding(
                contract([
                    candidate("option_a", "challenge", title="方案 A", thesis="A 第一版"),
                ]),
                "msg_revision",
            ),
        )
        old_projection = project_turn_contract_artifact(
            [proposed, revised, old_reference, decision],
            member_resolver=resolver,
            candidate_risk_review_required=True,
        )
        old_issue = next(
            item for item in old_projection["candidate_risk_reviews"]["issues"]
            if item["code"] == "CANDIDATE_RISK_REVIEW_STALE_REFERENCE"
        )
        self.assertEqual(old_issue["referenced_revision"], 1)
        self.assertEqual(old_issue["current_revision"], 2)

    def test_post_decision_candidate_or_review_activity_requires_decision_revisit(self) -> None:
        proposed = formal_message(
            "msg_plan",
            "planner",
            1,
            contract([
                candidate("option_a", "propose", title="方案 A", thesis="执行 A"),
                candidate("option_b", "propose", title="方案 B", thesis="执行 B"),
            ]),
        )
        risk_review = formal_message(
            "msg_risk",
            "risk",
            1,
            responding(
                contract([
                    candidate("option_a", "support", title="方案 A", thesis="执行 A"),
                    candidate("option_b", "challenge", title="方案 B", thesis="执行 B"),
                ]),
                "msg_plan",
            ),
        )
        decision = formal_message(
            "msg_decision",
            "decision",
            1,
            contract([
                candidate("option_a", "select", title="方案 A", thesis="执行 A", evidence_id="msg_risk"),
                candidate("option_b", "reject", title="方案 B", thesis="执行 B"),
            ]),
        )
        repeated_equivalent_proposal = formal_message(
            "msg_repeated_proposal",
            "planner",
            1,
            contract([
                candidate("option_a", "propose", title="方案 A", thesis="执行 A"),
            ]),
        )
        late_revision = formal_message(
            "msg_late_revision",
            "planner",
            1,
            contract([
                candidate("option_a", "revise", title="方案 A v2", thesis="决策后修订 A"),
            ]),
        )
        resolver = lambda member_id, _: {
            "workflow_stage": (
                "risk" if member_id == "risk" else "decision" if member_id == "decision" else "plan"
            )
        }

        unchanged = project_turn_contract_artifact(
            [proposed, risk_review, decision, repeated_equivalent_proposal],
            member_resolver=resolver,
            candidate_risk_review_required=True,
        )
        unchanged_gate = unchanged["candidate_risk_reviews"]
        self.assertTrue(unchanged_gate["ready"], unchanged_gate["issues"])
        self.assertNotIn(
            "CANDIDATE_RISK_REVIEW_DECISION_REVISIT_REQUIRED",
            {item["code"] for item in unchanged_gate["issues"]},
        )
        self.assertEqual(unchanged["decision"]["status"], "candidate")

        projection = project_turn_contract_artifact(
            [
                proposed,
                risk_review,
                decision,
                repeated_equivalent_proposal,
                late_revision,
            ],
            member_resolver=resolver,
            candidate_risk_review_required=True,
        )

        gate = projection["candidate_risk_reviews"]
        revisit = next(
            item for item in gate["issues"]
            if item["code"] == "CANDIDATE_RISK_REVIEW_DECISION_REVISIT_REQUIRED"
        )
        self.assertEqual(revisit["message_id"], "msg_late_revision")
        self.assertEqual(revisit["actions"], ["revise"])
        self.assertFalse(gate["ready"])
        self.assertEqual(projection["decision"]["status"], "undecided")

        rereview = formal_message(
            "msg_risk_v2",
            "risk",
            1,
            responding(
                contract([
                    candidate("option_a", "support", title="方案 A v2", thesis="决策后修订 A"),
                ]),
                "msg_late_revision",
            ),
        )
        revisited_decision = formal_message(
            "msg_decision_v2",
            "decision",
            1,
            contract([
                candidate(
                    "option_a",
                    "select",
                    title="方案 A v2",
                    thesis="决策后修订 A",
                    evidence_id="msg_risk_v2",
                ),
                candidate("option_b", "reject", title="方案 B", thesis="执行 B"),
            ]),
        )
        revisited = project_turn_contract_artifact(
            [
                proposed,
                risk_review,
                decision,
                repeated_equivalent_proposal,
                late_revision,
                rereview,
                revisited_decision,
            ],
            member_resolver=resolver,
            candidate_risk_review_required=True,
        )
        self.assertTrue(
            revisited["candidate_risk_reviews"]["ready"],
            revisited["candidate_risk_reviews"]["issues"],
        )
        self.assertEqual(revisited["candidate_risk_reviews"]["stale_review_count"], 1)
        self.assertEqual(revisited["decision"]["status"], "candidate")

    def test_nonrisk_unqualified_and_wrong_response_target_do_not_count(self) -> None:
        proposed = formal_message(
            "msg_plan",
            "planner",
            1,
            contract([
                candidate("option_a", "propose", title="方案 A", thesis="执行 A"),
                candidate("option_b", "propose", title="方案 B", thesis="执行 B"),
            ]),
        )
        nonrisk = formal_message(
            "msg_nonrisk",
            "analyst",
            1,
            responding(
                contract([
                    candidate("option_a", "support", title="方案 A", thesis="执行 A"),
                ]),
                "msg_plan",
            ),
        )
        unqualified = formal_message(
            "msg_unqualified",
            "risk",
            1,
            responding(
                contract([
                    candidate("option_b", "challenge", title="方案 B", thesis="执行 B"),
                ]),
                "msg_plan",
            ),
        )
        unqualified["turn_contract_integrity_ok"] = False
        wrong_response = formal_message(
            "msg_wrong_response",
            "risk",
            1,
            responding(
                contract([
                    candidate("option_a", "challenge", title="方案 A", thesis="执行 A"),
                ]),
                "msg_unrelated",
            ),
        )
        decision = formal_message(
            "msg_decision",
            "decision",
            1,
            contract([
                candidate("option_a", "select", title="方案 A", thesis="执行 A", evidence_id="msg_wrong_response"),
                candidate("option_b", "reject", title="方案 B", thesis="执行 B"),
            ]),
        )
        projection = project_turn_contract_artifact(
            [proposed, nonrisk, unqualified, wrong_response, decision],
            member_resolver=lambda member_id, _: {
                "workflow_stage": (
                    "risk" if member_id == "risk" else "decision" if member_id == "decision" else "analysis"
                )
            },
            candidate_risk_review_required=True,
        )

        gate = projection["candidate_risk_reviews"]
        self.assertEqual(gate["review_count"], 0)
        self.assertEqual(gate["reviewed_candidate_count"], 0)
        codes = {item["code"] for item in gate["issues"]}
        self.assertIn("CANDIDATE_RISK_REVIEW_RESPONSE_TARGET_MISSING", codes)
        self.assertIn("CANDIDATE_RISK_REVIEW_MISSING", codes)
        self.assertEqual(projection["decision"]["status"], "undecided")

    def test_selected_candidate_must_cite_current_risk_review_message(self) -> None:
        proposed = formal_message(
            "msg_plan",
            "planner",
            1,
            contract([
                candidate("option_a", "propose", title="方案 A", thesis="执行 A"),
                candidate("option_b", "propose", title="方案 B", thesis="执行 B"),
            ]),
        )
        risk_review = formal_message(
            "msg_risk",
            "risk",
            1,
            responding(
                contract([
                    candidate("option_a", "support", title="方案 A", thesis="执行 A"),
                    candidate("option_b", "challenge", title="方案 B", thesis="执行 B"),
                ]),
                "msg_plan",
            ),
        )
        decision = formal_message(
            "msg_decision",
            "decision",
            1,
            contract([
                candidate("option_a", "select", title="方案 A", thesis="执行 A"),
                candidate("option_b", "reject", title="方案 B", thesis="执行 B"),
            ]),
        )
        projection = project_turn_contract_artifact(
            [proposed, risk_review, decision],
            member_resolver=lambda member_id, _: {
                "workflow_stage": (
                    "risk" if member_id == "risk" else "decision" if member_id == "decision" else "plan"
                )
            },
            candidate_risk_review_required=True,
        )

        gate = projection["candidate_risk_reviews"]
        reference_issue = next(
            item for item in gate["issues"]
            if item["code"] == "CANDIDATE_RISK_REVIEW_DECISION_REFERENCE_MISSING"
        )
        self.assertEqual(reference_issue["candidate_id"], "option_a")
        self.assertEqual(reference_issue["required_review_message_ids"], ["msg_risk"])
        self.assertFalse(gate["ready"])
        self.assertEqual(projection["decision"]["status"], "undecided")

    def test_unqualified_or_non_decision_selection_fails_closed(self) -> None:
        non_decision = formal_message(
            "msg_plan",
            "planner",
            1,
            contract(
                [
                    candidate("option_a", "select", title="方案 A", thesis="选择 A"),
                    candidate("option_b", "reject", title="方案 B", thesis="拒绝 B"),
                ]
            ),
        )
        corrupt = formal_message(
            "msg_corrupt",
            "decision",
            1,
            contract([candidate("option_c", "select", title="方案 C", thesis="选择 C")]),
        )
        corrupt["turn_contract_integrity_ok"] = False

        projection = project_turn_contract_artifact(
            [non_decision, corrupt],
            member_resolver=lambda *_: {"workflow_stage": "plan"},
        )

        self.assertEqual(projection["qualified_message_count"], 1)
        self.assertEqual(projection["decision"]["status"], "undecided")
        self.assertEqual(projection["decision"]["preferred_option_id"], "")
        self.assertNotIn("option_c", {item["id"] for item in projection["decision"]["options"]})

    def test_contract_projection_replaces_model_decision_sections(self) -> None:
        payload = contract(
            [candidate("option_a", "propose", title="方案 A", thesis="只保留合同中的候选")],
            risks=[{
                "id": "risk_critical",
                "text": "关键证据失效",
                "severity": "critical",
                "status": "open",
                "trigger": "来源不可复核",
                "mitigation": "暂停并补充来源",
                "blocking": True,
                "evidence": [],
            }],
        )
        projection = project_turn_contract_artifact(
            [formal_message("msg_plan", "planner", 1, payload)],
            member_resolver=lambda *_: {"workflow_stage": "plan"},
        )
        projected = ArtifactService._apply_turn_contract_projection(
            {
                "risks": [{"id": "model_risk", "text": "模型自由文本风险"}],
                "actions": [{"id": "model_action", "text": "模型自由文本行动"}],
                "decision": {
                    "status": "candidate",
                    "options": [{"id": "model_option", "title": "模型自由文本方案"}],
                    "preferred_option_id": "model_option",
                },
                "generation_notes": "模型草稿。",
            },
            projection,
            round_status="COMPLETED",
        )

        self.assertEqual(len(projected["risks"]), 1)
        self.assertEqual(projected["risks"][0]["impact"], "high")
        self.assertNotEqual(projected["risks"][0]["id"], "model_risk")
        self.assertEqual(projected["actions"], [])
        self.assertEqual(projected["decision"]["status"], "undecided")
        self.assertNotIn(
            "model_option",
            {item["id"] for item in projected["decision"]["options"]},
        )

    def test_snapshot_contract_evidence_is_projected_then_server_bound_by_round(self) -> None:
        snapshot_id = "snapshot-project-round"
        payload = contract([
            candidate(
                "option_snapshot",
                "propose",
                title="冻结快照方案",
                thesis="只使用本轮冻结快照形成候选。",
                evidence_id=snapshot_id,
                evidence_type="round_market_snapshot",
            ),
        ])
        projection = project_turn_contract_artifact(
            [formal_message("msg_plan", "planner", 1, payload)],
            member_resolver=lambda *_: {"workflow_stage": "plan"},
        )
        raw_reference = projection["decision"]["options"][0]["evidence"][1]
        self.assertEqual(raw_reference, {
            "type": "round_market_snapshot",
            "id": snapshot_id,
            "evidence_role": "support",
            "verification_status": "unreviewed",
        })
        self.assertNotIn("source_revision", raw_reference)
        self.assertNotIn("source_snapshot_sha256", raw_reference)

        with tempfile.TemporaryDirectory() as temp_dir:
            store = StudioStore(Path(temp_dir) / "studio.sqlite3")
            round_row = store.create_round("room_plan", "服务端绑定冻结快照")
            market_snapshot = {
                "snapshot_id": snapshot_id,
                "captured_at": "2026-08-01T20:00:00Z",
                "state": "ready",
                "evidence": {"version": "test_market_evidence_v1"},
                "execution_capability": "none",
                "live_trading_allowed": False,
            }
            shared_context, manifest = store.material_prompt_bundle("room_plan")
            manifest = store.finalize_round_evidence_manifest(
                manifest,
                shared_context=shared_context,
                market_snapshot=market_snapshot,
            )
            store.save_round_checkpoint("room_plan", round_row["id"], {
                "member_ids": [],
                "next_order": 1,
                "max_turns": 1,
                "shared_context": shared_context,
                "market_snapshot": market_snapshot,
                "round_evidence_manifest": manifest,
            })
            content = ArtifactService._apply_turn_contract_projection(
                {
                    "summary": "冻结快照候选。",
                    "summary_evidence": [],
                    "requirements": [],
                    "risks": [],
                    "conclusions": [],
                    "disagreements": [],
                    "unknowns": [],
                    "actions": [],
                    "decision": {},
                },
                projection,
                round_status="RUNNING",
            )
            artifact = store.create_artifact(
                "room_plan",
                title="冻结快照合同投影",
                content=content,
                round_id=round_row["id"],
                generation_source="test",
            )

        bound_reference = next(
            ref
            for ref in artifact["content"]["decision"]["options"][0]["evidence"]
            if ref["type"] == "round_market_snapshot"
        )
        self.assertEqual(bound_reference["round_id"], round_row["id"])
        self.assertEqual(bound_reference["snapshot_id"], snapshot_id)
        self.assertEqual(bound_reference["source_revision"], "test_market_evidence_v1")
        self.assertEqual(len(bound_reference["source_snapshot_sha256"]), 64)
        self.assertEqual(bound_reference["execution_capability"], "none")
        self.assertFalse(bound_reference["live_trading_allowed"])

    def test_artifact_service_projects_v1_contract_without_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StudioStore(Path(temp_dir) / "studio.sqlite3")
            room = store.room_snapshot("room_plan")
            decision_member = next(
                member
                for member in room["members"]
                if member.get("workflow_stage") == "decision"
                or "decision_synthesis" in (member.get("capabilities") or [])
            )
            round_row = store.create_round(
                "room_plan",
                "以发言合同整理两个候选",
                turn_contract_version=TURN_CONTRACT_VERSION,
            )
            source_message = store.add_message(
                "room_plan",
                sender_type="user",
                sender_name="我",
                content="比较小范围验证与完整范围两个候选。",
                round_id=round_row["id"],
            )
            turn = store.begin_round_turn(
                "room_plan",
                round_row["id"],
                1,
                decision_member,
            )
            payload = contract(
                [
                    candidate(
                        "option_small",
                        "select",
                        title="小范围验证",
                        thesis="先验证核心假设，保留可逆性",
                        evidence_id=source_message["id"],
                    ),
                    candidate(
                        "option_full",
                        "reject",
                        title="完整范围",
                        thesis="当前成本与依赖尚未闭合",
                        evidence_id=source_message["id"],
                    ),
                ],
                risks=[{
                    "id": "risk_coverage",
                    "text": "小范围验证的覆盖不足",
                    "severity": "medium",
                    "status": "open",
                    "trigger": "关键路径未进入验证范围",
                    "mitigation": "增加边界用例",
                    "blocking": True,
                    "evidence": [],
                }],
            )
            shared_context, manifest = store.material_prompt_bundle("room_plan")
            manifest = store.finalize_round_evidence_manifest(
                manifest,
                shared_context=shared_context,
                market_snapshot=None,
            )
            checkpoint = {
                "member_ids": [decision_member["id"]],
                "spoken_counts": {decision_member["id"]: 1},
                "spoken_stances": [decision_member["stance"]],
                "successful_member_ids": [decision_member["id"]],
                "failed_member_ids": [],
                "previous_name": decision_member["name"],
                "completed": 1,
                "failures": 0,
                "skipped": 0,
                "proposals_created": 0,
                "next_order": 2,
                "max_turns": 1,
                "shared_context": shared_context,
                "market_snapshot": None,
                "workflow_policy": room["room"]["workflow_policy"],
                "capability_pack_ids": [],
                "room_capabilities": [
                    "collaboration.chat",
                    "materials.shared",
                    "artifacts.meeting",
                ],
                "round_evidence_manifest": manifest,
                "turn_contract_version": TURN_CONTRACT_VERSION,
                "turn_contract_required": True,
            }
            decision_message = store.add_message(
                "room_plan",
                sender_type="ai",
                sender_id=decision_member["id"],
                sender_name=decision_member["name"],
                identity=decision_member["identity"],
                provider=decision_member["provider"],
                model=decision_member["model"],
                member_version=decision_member["version"],
                content="建议先做小范围、可逆验证，并保留完整范围作为对照。",
                round_id=round_row["id"],
                round_turn_id=turn["id"],
                round_turn_status="RESPONDED",
                round_checkpoint_state=checkpoint,
                turn_contract=payload,
                turn_contract_version=TURN_CONTRACT_VERSION,
                turn_contract_qualified=True,
                turn_contract_issues=[],
            )
            store.complete_round(round_row["id"], "COMPLETED")

            artifact = ArtifactService(store, DisabledRegistry()).generate_minutes(
                "room_plan",
                round_row["id"],
                synthesizer_member_id=decision_member["id"],
            )

            decision = artifact["content"]["decision"]
            self.assertEqual(artifact["generation_source"], "template_fallback+turn_contract_v1")
            self.assertEqual(decision["status"], "undecided")
            self.assertEqual(decision["preferred_option_id"], "")
            self.assertEqual(decision["options"], [])
            self.assertEqual(decision["evidence"], [])
            self.assertFalse(artifact["evidence_review"]["confirmation_ready"])
            self.assertIn("只来自已校验机器合同", artifact["content"]["generation_notes"])

            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute(
                    "UPDATE rounds SET turn_contract_version=NULL WHERE id=?",
                    (round_row["id"],),
                )
            downgraded = store.round_turn_contract_bundle("room_plan", round_row["id"])
            self.assertTrue(downgraded["applicable"])
            self.assertFalse(downgraded["valid"])
            self.assertTrue(any("拒绝降级" in item for item in downgraded["issues"]))
            with self.assertRaisesRegex(ValueError, "发言合同审计失败"):
                ArtifactService(store, DisabledRegistry()).generate_minutes(
                    "room_plan",
                    round_row["id"],
                    synthesizer_member_id=decision_member["id"],
                )
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute(
                    "UPDATE rounds SET turn_contract_version=? WHERE id=?",
                    (TURN_CONTRACT_VERSION, round_row["id"]),
                )

            legal_replacement = json.loads(json.dumps(payload, ensure_ascii=False))
            legal_replacement["candidate_updates"][0]["action"] = "reject"
            legal_replacement["candidate_updates"][1]["action"] = "select"
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute(
                    "UPDATE messages SET turn_contract_json=? WHERE id=?",
                    (
                        json.dumps(legal_replacement, ensure_ascii=False),
                        decision_message["id"],
                    ),
                )
            bundle = store.round_turn_contract_bundle("room_plan", round_row["id"])
            self.assertFalse(bundle["valid"])
            self.assertTrue(any("合同封印" in item for item in bundle["issues"]))
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute(
                    "UPDATE messages SET turn_contract_json=? WHERE id=?",
                    (
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        decision_message["id"],
                    ),
                )

            with closing(sqlite3.connect(store.path)) as connection, connection:
                source_content = connection.execute(
                    "SELECT content FROM messages WHERE id=?",
                    (source_message["id"],),
                ).fetchone()[0]
                connection.execute(
                    "UPDATE messages SET content=? WHERE id=?",
                    ("被事后改写的证据正文", source_message["id"]),
                )
            bundle = store.round_turn_contract_bundle("room_plan", round_row["id"])
            self.assertFalse(bundle["valid"])
            self.assertTrue(any("合同封印" in item for item in bundle["issues"]))
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute(
                    "UPDATE messages SET content=? WHERE id=?",
                    (source_content, source_message["id"]),
                )

            with closing(sqlite3.connect(store.path)) as connection, connection:
                version_row = connection.execute(
                    """SELECT id,snapshot_json FROM member_versions
                       WHERE room_id=? AND member_id=? AND version=?
                       ORDER BY changed_at DESC,rowid DESC LIMIT 1""",
                    (
                        "room_plan",
                        decision_member["id"],
                        decision_member["version"],
                    ),
                ).fetchone()
                original_snapshot_json = version_row[1]
                replaced_snapshot = json.loads(original_snapshot_json)
                replaced_snapshot["name"] = "被事后改写的决策者"
                connection.execute(
                    "UPDATE member_versions SET snapshot_json=? WHERE id=?",
                    (json.dumps(replaced_snapshot, ensure_ascii=False), version_row[0]),
                )
            bundle = store.round_turn_contract_bundle("room_plan", round_row["id"])
            self.assertFalse(bundle["valid"])
            self.assertTrue(any("身份版本快照损坏" in item for item in bundle["issues"]))
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute(
                    "UPDATE member_versions SET snapshot_json=? WHERE id=?",
                    (original_snapshot_json, version_row[0]),
                )

            with closing(sqlite3.connect(store.path)) as connection, connection:
                checkpoint_row = connection.execute(
                    "SELECT state_json FROM round_checkpoints WHERE round_id=?",
                    (round_row["id"],),
                ).fetchone()
                original_checkpoint_json = checkpoint_row[0]
                replaced_checkpoint = json.loads(original_checkpoint_json)
                replaced_checkpoint["previous_name"] = "被事后改写的检查点"
                connection.execute(
                    "UPDATE round_checkpoints SET state_json=? WHERE round_id=?",
                    (json.dumps(replaced_checkpoint, ensure_ascii=False), round_row["id"]),
                )
            bundle = store.round_turn_contract_bundle("room_plan", round_row["id"])
            self.assertFalse(bundle["valid"])
            self.assertTrue(any("检查点完整性" in item for item in bundle["issues"]))
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute(
                    "UPDATE round_checkpoints SET state_json=? WHERE round_id=?",
                    (original_checkpoint_json, round_row["id"]),
                )

            tampered = json.loads(json.dumps(payload, ensure_ascii=False))
            tampered["candidate_updates"][1]["action"] = "defer"
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute(
                    "UPDATE messages SET turn_contract_json=? WHERE id=?",
                    (json.dumps(tampered, ensure_ascii=False), decision_message["id"]),
                )
            bundle = store.round_turn_contract_bundle("room_plan", round_row["id"])
            self.assertFalse(bundle["valid"])
            self.assertTrue(any("DECISION_SELECTION_REQUIRED" in item for item in bundle["issues"]))
            artifact_count = len(store.room_snapshot("room_plan")["artifacts"])
            with self.assertRaisesRegex(ValueError, "发言合同审计失败"):
                ArtifactService(store, DisabledRegistry()).generate_minutes(
                    "room_plan",
                    round_row["id"],
                    synthesizer_member_id=decision_member["id"],
                )
            self.assertEqual(len(store.room_snapshot("room_plan")["artifacts"]), artifact_count)
            with self.assertRaisesRegex(ValueError, "发言合同审计"):
                store.confirm_artifact(
                    "room_plan",
                    artifact["id"],
                    expected_version=artifact["version"],
                    confirmed_by="user",
                )


if __name__ == "__main__":
    unittest.main()
