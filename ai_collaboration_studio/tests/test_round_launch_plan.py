from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any

from backend.project_round_focus import (
    PROJECT_ROUND_FOCUS_PORT_ID,
    ProjectRoundFocusService,
)
from backend.round_contexts import (
    RoundContextError,
    build_round_context_authorization_set,
    round_context_authorization_entry,
)
from backend.round_launch_plan import (
    DEFAULT_PROVIDER_CALL_HARD_LIMIT,
    ROUND_LAUNCH_PLAN_VERSION,
    ROUND_LAUNCH_PLAN_VERSION_V5,
    RoundLaunchPlanService,
    validate_authorization,
)
from backend.store import StudioStore


class LocalStatusOnlyRegistry:
    def __init__(
        self,
        statuses: list[dict[str, Any]],
        *,
        disabled_provider_ids: set[str] | None = None,
    ) -> None:
        self._statuses = copy.deepcopy(statuses)
        self.disabled_provider_ids = frozenset(disabled_provider_ids or set())
        self.status_calls = 0
        self.calls: list[str] = []

    def status(self) -> list[dict[str, Any]]:
        self.status_calls += 1
        self.calls.append("status")
        return copy.deepcopy(self._statuses)

    def preflight(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls.append("preflight")
        raise AssertionError("launch planning must never probe a provider")

    def generate(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls.append("generate")
        raise AssertionError("launch planning must never generate text")


class SnapshotStore:
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = snapshot
        self.clean_calls: list[Any] = []
        self.snapshot_calls: list[str] = []
        self.calls: list[str] = []

    def clean_round_objective(self, objective: Any) -> str:
        self.clean_calls.append(objective)
        self.calls.append("clean_round_objective")
        return StudioStore.clean_round_objective(objective)

    def room_snapshot(self, room_id: str) -> dict[str, Any] | None:
        self.snapshot_calls.append(room_id)
        self.calls.append("room_snapshot")
        if room_id != self.snapshot["room"]["id"]:
            return None
        # Deliberately return the owned object so the service's deep-copy
        # boundary is exercised.
        return self.snapshot


def provider_statuses() -> list[dict[str, Any]]:
    return [
        {
            "id": "deepseek",
            "name": "display-only DeepSeek",
            "model": "deepseek-v4-pro",
            "configured": True,
            "policy_disabled": False,
            "api_key": "PROVIDER_STATUS_SECRET",
            "message": "must not enter a launch plan",
        },
        {
            "id": "doubao",
            "name": "display-only Doubao",
            "model": "doubao-seed-2-0-lite-260215",
            "configured": True,
            "policy_disabled": False,
            "base_url": "https://secret.invalid/tenant",
        },
        {
            "id": "openai",
            "name": "display-only OpenAI",
            "model": "gpt-test",
            "configured": True,
            "policy_disabled": True,
        },
    ]


class RoundLaunchPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "round-launch-plan.sqlite3"
        self.real_store = StudioStore(self.database_path)

    def snapshot(self, room_id: str) -> dict[str, Any]:
        snapshot = self.real_store.room_snapshot(room_id)
        self.assertIsNotNone(snapshot)
        return copy.deepcopy(snapshot)

    def storage_snapshot(self) -> dict[str, Any]:
        snapshot = self.snapshot("room_storage")
        enabled = [member for member in snapshot["members"] if member["enabled"]]
        self.assertEqual(len(enabled), 12)
        # Match the working storage committee: nine DeepSeek roles and three
        # Doubao roles, with one concrete moderator frozen by id.
        for index, member in enumerate(enabled):
            if index < 9:
                member["provider"] = "deepseek"
                member["model"] = "deepseek-v4-pro"
            else:
                member["provider"] = "doubao"
                member["model"] = "doubao-seed-2-0-lite-260215"
        snapshot["room"]["moderator_member_id"] = enabled[0]["id"]
        snapshot["room"]["settings_version"] = 4
        return snapshot

    def build_from_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        objective: str = "  Compare MU, SNDK, WDC and STX  ",
        skip_provider_ids: set[str] | None = None,
        statuses: list[dict[str, Any]] | None = None,
        disabled_provider_ids: set[str] | None = None,
    ) -> tuple[dict[str, Any], SnapshotStore, LocalStatusOnlyRegistry, RoundLaunchPlanService]:
        store = SnapshotStore(snapshot)
        providers = LocalStatusOnlyRegistry(
            statuses if statuses is not None else provider_statuses(),
            disabled_provider_ids=disabled_provider_ids,
        )
        service = RoundLaunchPlanService(store, providers)
        plan = service.build(
            snapshot["room"]["id"],
            objective,
            skip_provider_ids or set(),
        )
        return plan, store, providers, service

    def test_context_pack_builds_v5_from_exact_canonical_set_read_only(self) -> None:
        created = self.real_store.create_room(
            "Generic context plan",
            "Freeze an explicitly authorized context.",
            capability_pack_ids=["project_round_focus"],
        )
        room_id = str(created["room"]["id"])
        for member in created.get("members") or []:
            if member.get("enabled") is True:
                self.real_store.update_member(
                    room_id,
                    str(member["id"]),
                    {"provider": "deepseek", "model": "deepseek-v4-pro"},
                    expected_version=int(member["version"]),
                )
        preview = ProjectRoundFocusService(self.real_store).preview(room_id)
        authorization = {
            "version": "project_round_focus_authorization_v1",
            "artifact_binding": {"status": "none"},
            "preview_sha256": preview["preview_sha256"],
            "user_confirmed": True,
        }
        authorization_set = build_round_context_authorization_set([
            round_context_authorization_entry(
                "project_round_focus",
                PROJECT_ROUND_FOCUS_PORT_ID,
                authorization,
            ),
        ])
        providers = LocalStatusOnlyRegistry(provider_statuses())
        service = RoundLaunchPlanService(self.real_store, providers)

        with closing(sqlite3.connect(self.database_path)) as connection:
            before = tuple(
                int(connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0])
                for table in ("rounds", "round_domain_contexts", "messages")
            )
        plan = service.build(
            room_id,
            "Use the exact authorized context.",
            round_context_authorizations=authorization_set,
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            after = tuple(
                int(connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0])
                for table in ("rounds", "round_domain_contexts", "messages")
            )

        self.assertEqual(plan["version"], ROUND_LAUNCH_PLAN_VERSION_V5)
        self.assertEqual(plan["round_context_authorizations"], authorization_set)
        self.assertNotIn("project_round_focus_authorization", plan)
        self.assertEqual(before, after)
        self.assertEqual(providers.calls, ["status"])

        legacy_plan = service.build(
            room_id,
            "Use the exact authorized context.",
            project_round_focus_authorization=authorization,
        )
        self.assertEqual(legacy_plan["version"], ROUND_LAUNCH_PLAN_VERSION_V5)
        self.assertEqual(
            legacy_plan["round_context_authorizations"],
            authorization_set,
        )
        self.assertEqual(legacy_plan["plan_hash"], plan["plan_hash"])

        with self.assertRaises(RoundContextError) as missing:
            service.build(room_id, "Missing exact context authorization.")
        self.assertEqual(missing.exception.code, "ROUND_CONTEXT_AUTHORIZATION_REQUIRED")

        corrupt_snapshot = self.snapshot(room_id)
        corrupt_snapshot["room"]["plugin_registry_snapshot"] = {}
        with self.assertRaises(RoundContextError) as unbound:
            RoundLaunchPlanService(
                SnapshotStore(corrupt_snapshot),
                LocalStatusOnlyRegistry(provider_statuses()),
            ).build(
                room_id,
                "Reject an active context pack without its frozen binding.",
                round_context_authorizations=authorization_set,
            )
        self.assertEqual(unbound.exception.code, "ROUND_CONTEXT_REGISTRY_INVALID")

        configuration_plan = service.build(
            room_id,
            "Inspect local configuration only.",
            project_round_focus_authorization={"malformed": True},
            round_context_authorizations={"also": "malformed"},
            configuration_only=True,
        )
        self.assertEqual(configuration_plan["version"], ROUND_LAUNCH_PLAN_VERSION)
        self.assertNotIn("round_context_authorizations", configuration_plan)

    def test_generic_plan_uses_workflow_call_counts_and_local_status_only(self) -> None:
        snapshot = self.snapshot("room_plan")
        plan, store, providers, _service = self.build_from_snapshot(
            snapshot,
            objective="  Define a verifiable project plan  ",
            skip_provider_ids={"openai"},
        )

        self.assertEqual(store.clean_calls, ["  Define a verifiable project plan  "])
        self.assertEqual(store.calls, ["clean_round_objective", "room_snapshot"])
        self.assertEqual(plan["objective"], "Define a verifiable project plan")
        self.assertEqual(providers.status_calls, 1)
        self.assertEqual(providers.calls, ["status"])
        self.assertEqual(plan["version"], "round_launch_plan_v3")
        self.assertEqual(plan["room"]["workflow_policy"]["minimum_successful_members"], 2)
        self.assertEqual(plan["room"]["capability_pack_ids"], [])
        self.assertEqual(plan["moderator"]["selection_source"], "workflow_stage_fallback")

        calls = plan["calls"]
        self.assertEqual(calls["unit"], "provider_call_count")
        self.assertFalse(calls["is_cost_estimate"])
        self.assertEqual(calls["unique_preflight_route_count"], 1)
        self.assertEqual(calls["projected_preflight_calls"], 1)
        self.assertEqual(calls["workflow_minimum_speaker_calls"], 3)
        self.assertEqual(calls["minimum_speaker_calls"], 3)
        self.assertEqual(calls["minimum_director_calls"], 0)
        self.assertEqual(calls["recommended_director_calls"], 2)
        self.assertEqual(calls["optional_artifact_calls"], 1)
        self.assertEqual(calls["recommended_provider_calls"], 7)
        self.assertEqual(calls["maximum_speaker_calls"], 6)
        self.assertEqual(calls["maximum_director_calls"], 5)
        self.assertEqual(calls["core_success_path_calls"], 4)
        self.assertEqual(calls["formal_path_call_ceiling_with_allowance"], 10)
        self.assertEqual(calls["formal_path_conservative_upper_bound"], 13)
        self.assertEqual(calls["discussion_call_range"], {"minimum": 3, "maximum": 11})
        self.assertEqual(calls["total_call_range"], {"minimum": 7, "maximum": 13})
        self.assertTrue(plan["ready_for_authorization"])

    def test_storage_committee_separates_allowance_from_the_hard_limit(self) -> None:
        snapshot = self.storage_snapshot()
        plan, _store, providers, service = self.build_from_snapshot(
            snapshot,
            skip_provider_ids={"openai"},
        )

        calls = plan["calls"]
        self.assertEqual(providers.status_calls, 1)
        self.assertEqual(calls["unique_preflight_route_count"], 2)
        self.assertEqual(calls["projected_preflight_calls"], 2)
        self.assertEqual(calls["minimum_speaker_calls"], 12)
        self.assertEqual(calls["minimum_director_calls"], 0)
        self.assertEqual(calls["recommended_director_calls"], 6)
        self.assertEqual(calls["optional_artifact_calls"], 1)
        self.assertEqual(calls["contingency_calls"], 0)
        self.assertEqual(calls["recommended_provider_calls"], 21)
        self.assertEqual(calls["projected_provider_calls_total"], 21)
        self.assertEqual(calls["maximum_director_calls"], 17)
        self.assertEqual(calls["core_success_path_calls"], 14)
        self.assertEqual(calls["formal_path_call_ceiling_with_allowance"], 27)
        self.assertEqual(calls["formal_path_conservative_upper_bound"], 38)
        self.assertEqual(calls["discussion_call_range"], {"minimum": 12, "maximum": 35})
        self.assertEqual(calls["total_call_range"], {"minimum": 21, "maximum": 38})
        self.assertEqual(plan["room"]["settings_version"], 4)
        self.assertEqual(plan["moderator"]["selection_source"], "configured")
        self.assertEqual(plan["moderator"]["id"], snapshot["room"]["moderator_member_id"])
        self.assertEqual(plan["safety"], {
            "budget_unit": "provider_call_count",
            "is_cost_estimate": False,
            "execution_capability": "none",
            "live_trading_allowed": False,
            "user_confirmation_required": True,
        })
        self.assertTrue(plan["ready_for_authorization"])

        by_provider = {
            item["provider"]: item
            for item in plan["provider_call_projection"]
        }
        self.assertEqual(by_provider["deepseek"]["projected_provider_calls"], 16)
        self.assertEqual(by_provider["doubao"]["projected_provider_calls"], 5)
        self.assertEqual(by_provider["openai"]["projected_provider_calls"], 0)
        self.assertEqual(by_provider["openai"]["projected_preflight_calls"], 0)
        self.assertTrue(by_provider["openai"]["policy_disabled"])
        self.assertTrue(by_provider["openai"]["skipped"])

        exact = service.validate_authorization(plan["plan_hash"], 28)
        self.assertTrue(exact["valid"])
        self.assertTrue(exact["sufficient"])
        self.assertIsNone(exact["warning"])

    def test_skipped_or_policy_disabled_openai_projects_zero_calls(self) -> None:
        snapshot = self.snapshot("room_plan")
        for member in snapshot["members"]:
            if member["enabled"]:
                member["provider"] = "openai"
                member["model"] = "gpt-test"

        cases = [
            (provider_statuses(), {"openai"}, set()),
            (provider_statuses(), set(), {"openai"}),
        ]
        for statuses, skip_ids, deployment_disabled in cases:
            with self.subTest(skip=skip_ids, deployment_disabled=deployment_disabled):
                if deployment_disabled:
                    statuses = copy.deepcopy(statuses)
                    next(item for item in statuses if item["id"] == "openai")[
                        "policy_disabled"
                    ] = False
                plan, _store, _providers, _service = self.build_from_snapshot(
                    copy.deepcopy(snapshot),
                    skip_provider_ids=skip_ids,
                    statuses=statuses,
                    disabled_provider_ids=deployment_disabled,
                )
                route = plan["preflight_routes"][0]
                self.assertEqual(route["provider"], "openai")
                self.assertEqual(route["projected_preflight_calls"], 0)
                self.assertFalse(route["callable"])
                self.assertEqual(plan["calls"]["projected_preflight_calls"], 0)
                self.assertEqual(plan["calls"]["minimum_speaker_calls"], 0)
                self.assertEqual(plan["calls"]["minimum_director_calls"], 0)
                self.assertEqual(plan["calls"]["optional_artifact_calls"], 0)
                self.assertEqual(plan["calls"]["recommended_provider_calls"], 0)
                self.assertEqual(plan["calls"]["total_call_range"], {
                    "minimum": 0,
                    "maximum": 0,
                })
                by_provider = {
                    item["provider"]: item
                    for item in plan["provider_call_projection"]
                }
                self.assertEqual(by_provider["openai"]["projected_provider_calls"], 0)
                self.assertFalse(plan["ready_for_authorization"])

    def test_recommendation_above_hard_limit_is_an_explainable_blocker(self) -> None:
        snapshot = self.snapshot("room_plan")
        facilitator = next(
            member for member in snapshot["members"]
            if member["workflow_stage"] == "facilitate"
        )
        challenger = next(
            member for member in snapshot["members"]
            if member.get("stance") == "challenger"
        )
        decision = next(
            member for member in snapshot["members"]
            if member["workflow_stage"] == "decision"
        )
        expanded: list[dict[str, Any]] = []
        for template in (facilitator, challenger, decision):
            for index in range(50):
                member = copy.deepcopy(template)
                member["id"] = f"{template['workflow_stage']}_{index:02d}"
                member["position"] = len(expanded) + 1
                member["provider"] = "deepseek"
                member["model"] = "deepseek-v4-pro"
                expanded.append(member)
        snapshot["members"] = expanded
        snapshot["room"]["moderator_member_id"] = expanded[0]["id"]
        policy = snapshot["room"]["workflow_policy"]
        policy["minimum_stage_coverage"] = {
            "facilitate": 50,
            "flexible": 50,
            "decision": 50,
        }
        policy["minimum_successful_members"] = 100
        policy["follow_up_budget"] = 50

        plan, _store, _providers, service = self.build_from_snapshot(snapshot)
        self.assertEqual(plan["calls"]["minimum_speaker_calls"], 150)
        self.assertEqual(plan["calls"]["minimum_director_calls"], 0)
        self.assertEqual(plan["calls"]["recommended_director_calls"], 50)
        self.assertGreater(
            plan["calls"]["recommended_provider_calls"],
            DEFAULT_PROVIDER_CALL_HARD_LIMIT,
        )
        blocker = next(
            item for item in plan["blockers"]
            if item["code"] == "RECOMMENDATION_EXCEEDS_DEPLOYMENT_HARD_LIMIT"
        )
        self.assertEqual(
            blocker["recommended_provider_calls"],
            plan["calls"]["recommended_provider_calls"],
        )
        self.assertEqual(blocker["deployment_hard_limit"], 28)
        self.assertFalse(plan["ready_for_authorization"])
        with self.assertRaises(ValueError):
            service.validate_authorization(plan["plan_hash"], 28)

    def test_hash_tracks_behavior_but_ignores_room_and_provider_display_fields(self) -> None:
        base_snapshot = self.storage_snapshot()
        base, *_ = self.build_from_snapshot(copy.deepcopy(base_snapshot))
        base_hash = base["plan_hash"]

        mutations = {
            "member_identity": lambda value: value["members"][1].__setitem__(
                "identity", "Changed research identity"
            ),
            "member_version": lambda value: value["members"][1].__setitem__("version", 2),
            "member_model": lambda value: value["members"][1].__setitem__(
                "model", "deepseek-alternate"
            ),
            "moderator": lambda value: value["room"].__setitem__(
                "moderator_member_id", value["members"][1]["id"]
            ),
            "workflow": lambda value: value["room"]["workflow_policy"].__setitem__(
                "follow_up_budget", 5
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                changed = copy.deepcopy(base_snapshot)
                mutate(changed)
                plan, *_ = self.build_from_snapshot(changed)
                self.assertNotEqual(plan["plan_hash"], base_hash)

        changed_objective, *_ = self.build_from_snapshot(
            copy.deepcopy(base_snapshot),
            objective="A different objective",
        )
        changed_skip, *_ = self.build_from_snapshot(
            copy.deepcopy(base_snapshot),
            skip_provider_ids={"glm"},
        )
        self.assertNotEqual(changed_objective["plan_hash"], base_hash)
        self.assertNotEqual(changed_skip["plan_hash"], base_hash)

        display_only = copy.deepcopy(base_snapshot)
        display_only["room"]["title"] = "Renamed room"
        display_only["room"]["category"] = "Renamed category"
        display_only["room"]["settings_version"] += 1
        display_statuses = provider_statuses()
        for status in display_statuses:
            status["name"] = "Renamed provider"
            status["message"] = "Changed display message"
        display_plan, *_ = self.build_from_snapshot(
            display_only,
            statuses=display_statuses,
        )
        self.assertEqual(display_plan["plan_hash"], base_hash)
        self.assertNotEqual(display_plan["display"], base["display"])
        self.assertNotEqual(
            display_plan["room"]["settings_version"],
            base["room"]["settings_version"],
        )

    def test_build_returns_deep_copy_and_whitelists_safe_fields(self) -> None:
        snapshot = self.storage_snapshot()
        snapshot["members"][0]["instructions"] = "MEMBER_INSTRUCTION_SECRET"
        snapshot["members"][0]["boundaries"] = "MEMBER_BOUNDARY_SECRET"
        snapshot["members"][0]["responsibilities"] = "MEMBER_RESPONSIBILITY_SECRET"
        plan, store, providers, service = self.build_from_snapshot(snapshot)
        original_hash = plan["plan_hash"]

        encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True)
        for forbidden in (
            "MEMBER_INSTRUCTION_SECRET",
            "MEMBER_BOUNDARY_SECRET",
            "MEMBER_RESPONSIBILITY_SECRET",
            "PROVIDER_STATUS_SECRET",
            "https://secret.invalid/tenant",
            "api_key",
            "instructions",
            "boundaries",
            "responsibilities",
        ):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(set(plan["members"][0]), {
            "id", "version", "name", "identity", "stage", "provider", "model",
        })

        plan["members"][0]["identity"] = "mutated output"
        plan["room"]["workflow_policy"]["follow_up_budget"] = 999
        plan["preflight_routes"][0]["member_ids"].clear()
        self.assertNotEqual(store.snapshot["members"][0]["identity"], "mutated output")
        self.assertNotEqual(
            store.snapshot["room"]["workflow_policy"]["follow_up_budget"],
            999,
        )
        rebuilt = service.build(store.snapshot["room"]["id"], "Compare MU, SNDK, WDC and STX", set())
        self.assertEqual(rebuilt["plan_hash"], original_hash)
        self.assertEqual(providers.status_calls, 2)

    def test_authorization_enforces_hard_limit_and_warns_below_recommendation(self) -> None:
        plan, _store, _providers, service = self.build_from_snapshot(
            self.storage_snapshot()
        )
        below = service.validate_authorization(plan["plan_hash"], 20)
        self.assertTrue(below["valid"])
        self.assertFalse(below["sufficient"])
        self.assertEqual(
            below["warning"]["code"],
            "BELOW_RECOMMENDED_PROVIDER_CALLS",
        )
        self.assertTrue(
            service.validate_authorization(plan["plan_hash"], 28)["sufficient"]
        )
        at_hard_limit = service.validate_authorization(
            plan["plan_hash"], DEFAULT_PROVIDER_CALL_HARD_LIMIT
        )
        self.assertTrue(at_hard_limit["sufficient"])

        for invalid in (0, -1, 29, 101, True, 1.5, "28"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    service.validate_authorization(plan["plan_hash"], invalid)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            service.validate_authorization("0" * 64, 28)
        with self.assertRaises(ValueError):
            validate_authorization(
                plan["plan_hash"],
                28,
                recommended_provider_calls=28,
                expected_plan_hash="0" * 64,
            )


if __name__ == "__main__":
    unittest.main()
