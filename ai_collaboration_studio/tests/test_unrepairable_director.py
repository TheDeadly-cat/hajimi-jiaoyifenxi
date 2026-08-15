from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from typing import Any

from backend.orchestrator import DiscussionOrchestrator
from backend.providers.base import ProviderResponse
from backend.store import StudioStore
from backend.workflow_policy import default_workflow_policy


class NoCallProvider:
    provider_id = "deepseek"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, **kwargs: Any) -> ProviderResponse:
        self.calls += 1
        return ProviderResponse(
            ok=True,
            provider=self.provider_id,
            model=str(kwargs.get("model") or "offline-model"),
            # Deliberately invalid moderator JSON exercises the deterministic
            # continuation fallback without any network or real provider.
            content="{}",
        )


class NoCallRegistry:
    def __init__(self, provider: NoCallProvider) -> None:
        self.provider = provider

    def get(self, provider_id: str) -> NoCallProvider:
        self.provider.provider_id = str(provider_id or "deepseek")
        return self.provider


class FixedGateOrchestrator(DiscussionOrchestrator):
    def __init__(self, *args: Any, convergence_state: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fixed_convergence_state = copy.deepcopy(convergence_state)

    def _convergence_state(
        self,
        _room_id: str,
        _round_id: str,
        _successful_member_ids: set[str],
        _market_snapshot: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return copy.deepcopy(self.fixed_convergence_state)


def fixed_state(*, repair_scope: str, discussion_ready: bool) -> dict[str, Any]:
    focus = {
        "code": "FROZEN_EVIDENCE_ERROR",
        "title": "冻结证据来源报错",
        "detail": "本轮只能说明影响，下一新轮重新冻结。",
        "target_capabilities": ["evidence_review"],
        "target_stances": ["data_guardian"],
        "repair_scope": repair_scope,
    }
    return {
        "project_workspace": {},
        "research_evidence_gate": {
            "ready": False,
            "repair_scope": repair_scope,
            "focus": focus,
            "blockers": [focus],
        },
        "candidate_risk_review_gate": {},
        "can_host_finish": False,
        "discussion_gate": {
            "ready": discussion_ready,
            "successful_member_count": 1,
            "required_success_count": 1,
            "stage_coverage": [],
            "role_coverage": [],
            "blockers": [] if discussion_ready else [{"title": "强制覆盖未完成"}],
        },
    }


class UnrepairableDirectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="ai-studio-p18-test-")
        self.store = StudioStore(Path(self.temp_dir.name) / "p18.sqlite3")
        members = self.store.enabled_members("room_plan")
        self.moderator = self.store.update_member(
            "room_plan",
            str(members[0]["id"]),
            {"provider": "deepseek", "model": "offline-model"},
        )
        evidence_member = next(
            member for member in self.store.enabled_members("room_plan")
            if "evidence_review" in (member.get("capabilities") or [])
        )
        self.evidence_member_id = str(evidence_member["id"])
        room = dict(self.store.room_snapshot("room_plan")["room"])
        room.update({
            "discussion_mode": "dynamic",
            "moderator_member_id": str(self.moderator["id"]),
            "moderator_member_version": int(self.moderator["version"]),
            "moderator_provider": "deepseek",
            "moderator_model": "offline-model",
            "moderator_approved_route": {},
        })
        self.room = room
        self.round_row = self.store.create_round(
            "room_plan",
            "offline frozen-focus scheduling fixture",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def select(
        self,
        state: dict[str, Any],
        *,
        spoken_counts: dict[str, int],
        successful_member_ids: set[str],
        skip_provider_ids: set[str] | None = None,
    ) -> tuple[dict[str, Any], NoCallProvider]:
        provider = NoCallProvider()
        orchestrator = FixedGateOrchestrator(
            self.store,
            NoCallRegistry(provider),
            market_service=None,
            convergence_state=state,
        )
        selection = orchestrator._select_next_member(
            self.room,
            default_workflow_policy("open_collaboration"),
            "offline frozen-focus scheduling fixture",
            self.store.enabled_members("room_plan"),
            spoken_counts,
            set(),
            successful_member_ids,
            set(),
            len(successful_member_ids),
            round_id=str(self.round_row["id"]),
            skip_provider_ids=skip_provider_ids,
        )
        return selection, provider

    def test_explained_frozen_focus_finishes_partial_without_any_provider_call(self) -> None:
        selection, provider = self.select(
            fixed_state(
                repair_scope="next_round_only",
                discussion_ready=True,
            ),
            spoken_counts={self.evidence_member_id: 1},
            successful_member_ids={self.evidence_member_id},
        )

        self.assertEqual(provider.calls, 0)
        self.assertEqual(selection["action"], "finish")
        self.assertEqual(selection["source"], "partial_unrepairable")
        self.assertEqual(selection["finish_mode"], "partial_unrepairable")
        self.assertIn("下一新轮", selection["reason"])
        self.assertFalse(selection["convergence"]["can_host_finish"])
        self.assertEqual(
            selection["scheduling_context"]["finish_mode"],
            "partial_unrepairable",
        )

        persisted = DiscussionOrchestrator(
            self.store,
            NoCallRegistry(provider),
            market_service=None,
        )._persist_director_decision(
            "room_plan",
            str(self.round_row["id"]),
            selection,
            self.room,
        )
        self.assertEqual(
            persisted["workspace_focus"]["target_stances"],
            ["data_guardian"],
        )
        self.assertEqual(
            persisted["workspace_focus"]["repair_scope"],
            "next_round_only",
        )
        self.assertEqual(
            persisted["moderator_context"]["scheduling_context"]["finish_mode"],
            "partial_unrepairable",
        )
        self.assertTrue(str(persisted["decision_sha256"] or ""))
        trace = self.store.round_execution_trace(
            "room_plan",
            str(self.round_row["id"]),
        )
        self.assertNotIn(
            "DIRECTOR_DECISION_SEAL_MISMATCH",
            {
                str(issue.get("code") or "")
                for issue in trace["integrity"].get("issues") or []
                if isinstance(issue, dict)
            },
        )

    def test_unexplained_frozen_focus_selects_matching_member_without_director_call(self) -> None:
        other_member_id = next(
            str(member["id"])
            for member in self.store.enabled_members("room_plan")
            if str(member["id"]) != self.evidence_member_id
        )
        selection, provider = self.select(
            fixed_state(
                repair_scope="next_round_only",
                discussion_ready=False,
            ),
            spoken_counts={other_member_id: 1},
            successful_member_ids={other_member_id},
        )

        self.assertEqual(provider.calls, 0)
        self.assertEqual(selection["action"], "speak")
        self.assertEqual(selection["member"]["id"], self.evidence_member_id)
        self.assertEqual(selection["rule_id"], "unrepairable_focus_explanation")

    def test_in_round_focus_continues_instead_of_partial_finish(self) -> None:
        selection, provider = self.select(
            fixed_state(repair_scope="in_round", discussion_ready=True),
            spoken_counts={self.evidence_member_id: 1},
            successful_member_ids={self.evidence_member_id},
        )

        self.assertEqual(provider.calls, 1)
        self.assertEqual(selection["action"], "speak")
        self.assertNotEqual(selection.get("source"), "partial_unrepairable")
        self.assertNotEqual(selection.get("finish_mode"), "partial_unrepairable")

    def test_skipped_provider_routes_are_removed_before_ordinary_selection(self) -> None:
        for member in self.store.enabled_members("room_plan"):
            if str(member["id"]) in {
                str(self.moderator["id"]),
                self.evidence_member_id,
            }:
                continue
            self.store.update_member(
                "room_plan",
                str(member["id"]),
                {"provider": "openai", "model": "skipped-model"},
            )
        self.store.update_member(
            "room_plan",
            self.evidence_member_id,
            {"provider": "openai", "model": "skipped-model"},
        )
        moderator_id = str(self.moderator["id"])
        selection, provider = self.select(
            fixed_state(repair_scope="in_round", discussion_ready=False),
            spoken_counts={moderator_id: 1},
            successful_member_ids={moderator_id},
            skip_provider_ids={"openai"},
        )

        self.assertEqual(provider.calls, 0)
        self.assertEqual(selection["action"], "speak")
        self.assertEqual(selection["member"]["id"], moderator_id)
        self.assertNotEqual(
            str(selection["member"].get("provider") or "").lower(),
            "openai",
        )


if __name__ == "__main__":
    unittest.main()
