from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Callable

from backend.orchestrator import DiscussionOrchestrator
from backend.providers.base import ProviderResponse
from backend.store import StudioStore
from tests.turn_contract_fixture import append_valid_turn_contract


class DirectorScenarioProvider:
    provider_id = "deepseek"

    def __init__(
        self,
        selected_member_id: str,
        *,
        mode: str = "success",
        on_speaker: Callable[[], None] | None = None,
        on_director: Callable[[], None] | None = None,
    ) -> None:
        self.selected_member_id = selected_member_id
        self.mode = mode
        self.on_speaker = on_speaker
        self.on_director = on_director
        self.director_calls = 0
        self.speaker_calls = 0
        self.director_models: list[str] = []
        self.speaker_models: list[str] = []
        self.active_provider_id = self.provider_id

    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        is_director = '"action":"speak|finish"' in instructions
        if not is_director:
            self.speaker_calls += 1
            self.speaker_models.append(model)
            if self.on_speaker and self.speaker_calls == 1:
                self.on_speaker()
            content = append_valid_turn_contract(
                f"speaker response {self.speaker_calls}",
                instructions=instructions,
                input_text=input_text,
            )
            return ProviderResponse(
                ok=True,
                provider=self.active_provider_id,
                model=model or "speaker-model",
                content=content,
            )

        self.director_calls += 1
        self.director_models.append(model)
        if self.on_director:
            self.on_director()
        if self.mode == "exception":
            raise TimeoutError("raw upstream body must not be persisted")
        if self.mode == "invalid_object":
            return None  # type: ignore[return-value]
        if self.mode == "not_ok":
            return ProviderResponse(
                ok=False,
                provider=self.active_provider_id,
                model=model,
                error="raw upstream body must not be persisted",
                error_code="http_status",
            )
        if self.mode == "invalid_json":
            return ProviderResponse(
                ok=True,
                provider=self.active_provider_id,
                model=model,
                content="not-json",
            )
        if self.mode == "invalid_member":
            return ProviderResponse(
                ok=True,
                provider=self.active_provider_id,
                model=model,
                content=json.dumps({
                    "action": "speak",
                    "member_id": "member_outside_eligible_set",
                    "reason": "invalid member",
                }),
            )
        if self.mode == "premature_finish":
            return ProviderResponse(
                ok=True,
                provider=self.active_provider_id,
                model=model,
                content=json.dumps({
                    "action": "finish",
                    "member_id": "",
                    "reason": "finish before hard gates pass",
                }),
            )
        response_provider = (
            "wrong-provider"
            if self.mode == "wrong_identity"
            else self.active_provider_id
        )
        return ProviderResponse(
            ok=True,
            provider=response_provider,
            model=model or "director-model",
            content=json.dumps(
                {
                    "action": "speak",
                    "member_id": self.selected_member_id,
                    "reason": "selected by the hidden director",
                }
            ),
        )


class ScenarioRegistry:
    def __init__(self, provider: DirectorScenarioProvider) -> None:
        self.provider = provider

    def get(self, provider_id: str):
        self.provider.active_provider_id = str(provider_id or "deepseek").lower()
        return self.provider


class DirectorRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "director-runtime.sqlite3"
        self.store = StudioStore(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def configure_moderator(
        self,
        *,
        member_index: int = -1,
        provider: str = "deepseek",
        model: str = "director-model",
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        members = self.store.enabled_members("room_plan")
        # Runtime tests below intentionally exercise the hidden-model path.
        # Give both flexible-stage candidates the same unmet hard-coverage
        # capability so the rules-first policy sees a real semantic tie.
        for candidate in members:
            if candidate.get("workflow_stage") != "flexible":
                continue
            capabilities = sorted({
                *list(candidate.get("capabilities") or []),
                "critical_review",
            })
            self.store.update_member(
                "room_plan",
                str(candidate["id"]),
                {"capabilities": capabilities},
            )
        members = self.store.enabled_members("room_plan")
        moderator = self.store.update_member(
            "room_plan",
            str(members[member_index]["id"]),
            {"provider": provider, "model": model},
        )
        self.assertIsNotNone(moderator)
        room = self.store.room_snapshot("room_plan")["room"]
        self.store.update_room(
            "room_plan",
            {
                "expected_settings_version": room["settings_version"],
                "moderator_member_id": moderator["id"],
            },
        )
        return moderator or {}, self.store.enabled_members("room_plan")

    @staticmethod
    def round_id(events: list[dict[str, object]]) -> str:
        return str(next(
            event["round"]["id"]
            for event in events
            if event.get("type") in {"round_started", "round_resumed"}
        ))

    def run_scenario(
        self,
        mode: str,
    ) -> tuple[DirectorScenarioProvider, list[dict[str, object]], str]:
        moderator, members = self.configure_moderator()
        selected = next(
            member
            for member in members
            if member["id"] != moderator["id"]
            and member.get("workflow_stage") == "flexible"
        )
        provider = DirectorScenarioProvider(str(selected["id"]), mode=mode)
        orchestrator = DiscussionOrchestrator(
            self.store,
            ScenarioRegistry(provider),
            market_service=None,
        )
        events = list(orchestrator.run_round("room_plan", f"director scenario {mode}"))
        return provider, events, self.round_id(events)

    def assert_circuit_breaker(
        self,
        mode: str,
        expected_status: str,
        expected_error_code: str,
    ) -> None:
        provider, events, round_id = self.run_scenario(mode)
        attempts = self.store.list_director_attempts(
            "room_plan", round_id=round_id
        )
        self.assertEqual(provider.director_calls, 1)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], expected_status)
        self.assertEqual(attempts[0]["error_code"], expected_error_code)
        self.assertNotIn("STARTED", {attempt["status"] for attempt in attempts})
        self.assertTrue(any(
            event.get("type") == "director_decision"
            and event.get("source") == "director_circuit_breaker"
            for event in events
        ))
        fallback_decisions = [
            event["decision"]
            for event in events
            if event.get("type") == "director_decision"
            and event.get("source") == "director_circuit_breaker"
        ]
        self.assertTrue(fallback_decisions)
        self.assertTrue(all(
            decision["moderator_context"]["decision_authority"]
            == "safety_fallback"
            and decision["moderator_context"]["model_used"] is False
            for decision in fallback_decisions
        ))

    def test_unambiguous_room_uses_rules_first_without_hidden_model_calls(self) -> None:
        members = self.store.enabled_members("room_plan")
        # v2 considers all remaining stages together. Make one member the
        # unique highest contributor across the open flexible-stage and role
        # gaps instead of relying on the former earliest-stage candidate filter.
        selected_for_rules = members[1]
        self.store.update_member(
            "room_plan",
            str(selected_for_rules["id"]),
            {
                "capabilities": sorted({
                    *list(selected_for_rules.get("capabilities") or []),
                    "critical_review",
                    "decision_synthesis",
                }),
            },
        )
        members = self.store.enabled_members("room_plan")
        moderator = self.store.update_member(
            "room_plan",
            str(members[-1]["id"]),
            {"provider": "deepseek", "model": "director-model"},
        )
        room = self.store.room_snapshot("room_plan")["room"]
        self.store.update_room(
            "room_plan",
            {
                "expected_settings_version": room["settings_version"],
                "moderator_member_id": moderator["id"],
            },
        )
        provider = DirectorScenarioProvider(str(selected_for_rules["id"]))
        orchestrator = DiscussionOrchestrator(
            self.store,
            ScenarioRegistry(provider),
            market_service=None,
        )

        events = list(orchestrator.run_round(
            "room_plan",
            "rules-first unambiguous scheduling",
        ))
        round_id = self.round_id(events)
        rules_first = [
            event["decision"]
            for event in events
            if event.get("type") == "director_decision"
            and event.get("source") == "rules_first"
        ]

        self.assertEqual(provider.director_calls, 0)
        self.assertEqual(
            self.store.list_director_attempts("room_plan", round_id=round_id),
            [],
        )
        self.assertTrue(rules_first)
        self.assertTrue(all(
            decision["moderator_context"]["decision_authority"]
            == "service_policy"
            and decision["moderator_context"]["model_used"] is False
            for decision in rules_first
        ))
        max_gap_contexts = [
            decision["moderator_context"]["scheduling_context"]
            for decision in rules_first
            if (
                decision["moderator_context"].get("scheduling_context") or {}
            ).get("rule_id") == "max_required_gap_contribution"
        ]
        self.assertTrue(max_gap_contexts)
        self.assertTrue(all(
            context["policy_version"] == "rules_first_director_v2"
            and context["selected_gap_codes"]
            for context in max_gap_contexts
        ))

    def test_failed_director_call_opens_circuit_and_is_not_retried(self) -> None:
        self.assert_circuit_breaker(
            "not_ok", "FAILED", "director_provider_http_status"
        )

    def test_director_exception_is_normalized_and_not_retried(self) -> None:
        self.assert_circuit_breaker(
            "exception", "FAILED", "director_provider_timeout"
        )

    def test_invalid_provider_response_object_is_terminal_and_opens_circuit(self) -> None:
        self.assert_circuit_breaker(
            "invalid_object", "INVALID", "director_response_invalid_object"
        )

    def test_invalid_json_is_terminal_and_opens_circuit(self) -> None:
        self.assert_circuit_breaker(
            "invalid_json", "INVALID", "director_response_invalid_json"
        )

    def test_provider_identity_mismatch_is_terminal_and_opens_circuit(self) -> None:
        self.assert_circuit_breaker(
            "wrong_identity", "INVALID", "director_provider_identity_mismatch"
        )

    def test_ineligible_member_is_invalid_and_opens_circuit(self) -> None:
        self.assert_circuit_breaker(
            "invalid_member",
            "INVALID",
            "director_selected_member_not_eligible",
        )

    def test_premature_finish_is_invalid_and_opens_circuit(self) -> None:
        self.assert_circuit_breaker(
            "premature_finish",
            "INVALID",
            "director_finish_not_allowed",
        )

    def test_success_is_linked_after_decision_and_turn_reservation(self) -> None:
        provider, events, round_id = self.run_scenario("success")
        attempts = self.store.list_director_attempts(
            "room_plan", round_id=round_id
        )
        linked = [
            attempt for attempt in attempts
            if attempt["status"] == "RESPONDED" and int(attempt["turn_order"]) > 0
        ]
        self.assertGreater(provider.director_calls, 0)
        self.assertTrue(linked)
        self.assertFalse(any(attempt["status"] == "STARTED" for attempt in attempts))
        decision_ids = {
            str(event["decision"]["id"])
            for event in events
            if event.get("type") == "director_decision"
        }
        ai_decisions = [
            event["decision"]
            for event in events
            if event.get("type") == "director_decision"
            and event.get("source") == "ai"
        ]
        self.assertTrue(ai_decisions)
        self.assertTrue(all(
            decision["moderator_context"]["decision_authority"]
            == "moderator_model"
            and decision["moderator_context"]["model_used"] is True
            for decision in ai_decisions
        ))
        for attempt in linked:
            self.assertIn(attempt["director_decision_id"], decision_ids)
            turn = self.store.get_round_turn(
                "room_plan", round_id, int(attempt["turn_order"])
            )
            self.assertIsNotNone(turn)
            self.assertEqual(turn["member_id"], attempt["selected_member_id"])
            self.assertEqual(
                turn["director_decision_id"], attempt["director_decision_id"]
            )

    def test_disabled_frozen_moderator_is_not_replaced(self) -> None:
        moderator, members = self.configure_moderator()
        replacement = next(member for member in members if member["id"] != moderator["id"])

        def disable_frozen_moderator() -> None:
            room = self.store.room_snapshot("room_plan")["room"]
            self.store.update_room(
                "room_plan",
                {
                    "expected_settings_version": room["settings_version"],
                    "moderator_member_id": replacement["id"],
                },
            )
            self.store.update_member(
                "room_plan", str(moderator["id"]), {"enabled": False}
            )

        provider = DirectorScenarioProvider(
            str(replacement["id"]), on_speaker=disable_frozen_moderator
        )
        orchestrator = DiscussionOrchestrator(
            self.store, ScenarioRegistry(provider), market_service=None
        )
        events = list(orchestrator.run_round("room_plan", "disable frozen moderator"))
        round_id = self.round_id(events)

        unavailable = [
            event for event in events
            if event.get("code") == "ROUND_MODERATOR_UNAVAILABLE"
        ]
        self.assertTrue(unavailable)
        self.assertEqual(provider.director_calls, 0)
        self.assertEqual(
            self.store.list_director_attempts("room_plan", round_id=round_id), []
        )
        self.assertEqual(
            self.store.get_round("room_plan", round_id)["status"], "PAUSED"
        )

    def test_pause_during_hidden_call_cancels_started_attempt(self) -> None:
        moderator, members = self.configure_moderator()
        selected = next(
            member
            for member in members
            if member["id"] != moderator["id"]
            and member.get("workflow_stage") == "flexible"
        )

        def request_pause() -> None:
            latest = self.store.room_snapshot("room_plan")["latest_round"]
            self.store.request_round_pause("room_plan", str(latest["id"]))

        provider = DirectorScenarioProvider(
            str(selected["id"]), on_director=request_pause
        )
        orchestrator = DiscussionOrchestrator(
            self.store, ScenarioRegistry(provider), market_service=None
        )
        events = list(orchestrator.run_round("room_plan", "pause hidden director"))
        round_id = self.round_id(events)
        attempts = self.store.list_director_attempts(
            "room_plan", round_id=round_id
        )

        self.assertTrue(any(event.get("type") == "round_paused" for event in events))
        self.assertEqual(provider.director_calls, 1)
        self.assertEqual([attempt["status"] for attempt in attempts], ["CANCELLED"])
        self.assertEqual(attempts[0]["error_code"], "director_pause_requested")
        self.assertFalse(any(attempt["status"] == "STARTED" for attempt in attempts))

    def test_candidate_hot_edit_during_director_call_uses_latest_skipped_route(self) -> None:
        moderator, members = self.configure_moderator()
        selected = next(
            member for member in members[2:] if member["id"] != moderator["id"]
        )
        selected = self.store.update_member(
            "room_plan",
            str(selected["id"]),
            {"provider": "deepseek", "model": "target-before-edit"},
        )
        edited = False

        def hot_edit_selected() -> None:
            nonlocal edited
            if edited:
                return
            edited = True
            self.store.update_member(
                "room_plan",
                str(selected["id"]),
                {"provider": "openai", "model": "must-be-skipped"},
            )

        provider = DirectorScenarioProvider(
            str(selected["id"]),
            on_director=hot_edit_selected,
        )
        orchestrator = DiscussionOrchestrator(
            self.store, ScenarioRegistry(provider), market_service=None
        )

        events = list(orchestrator.run_round(
            "room_plan",
            "hot edit selected route during director",
            skip_provider_ids={"openai"},
        ))

        current = self.store.get_member("room_plan", str(selected["id"]))
        self.assertEqual(current["provider"], "openai")
        self.assertNotIn("target-before-edit", provider.speaker_models)
        self.assertTrue(any(
            event.get("type") == "speaker_failed"
            and (event.get("member") or {}).get("id") == selected["id"]
            and event.get("error_code") == "provider_skipped"
            for event in events
        ))
        round_id = self.round_id(events)
        self.assertFalse(any(
            attempt["status"] == "STARTED"
            for attempt in self.store.list_director_attempts(
                "room_plan", round_id=round_id
            )
        ))

    def test_candidate_hot_edit_after_turn_reservation_never_calls_old_route(self) -> None:
        moderator, members = self.configure_moderator()
        selected = next(
            member for member in members[2:] if member["id"] != moderator["id"]
        )
        selected = self.store.update_member(
            "room_plan",
            str(selected["id"]),
            {"provider": "deepseek", "model": "reserved-route"},
        )
        provider = DirectorScenarioProvider(str(selected["id"]))
        orchestrator = DiscussionOrchestrator(
            self.store, ScenarioRegistry(provider), market_service=None
        )
        stream = orchestrator.run_round(
            "room_plan",
            "hot edit after reservation",
            skip_provider_ids={"openai"},
        )
        events: list[dict[str, object]] = []
        edited = False
        for event in stream:
            events.append(event)
            if (
                not edited
                and event.get("type") == "speaker_started"
                and (event.get("member") or {}).get("id") == selected["id"]
            ):
                edited = True
                self.store.update_member(
                    "room_plan",
                    str(selected["id"]),
                    {"provider": "openai", "model": "must-be-skipped"},
                )

        self.assertTrue(edited)
        self.assertNotIn("reserved-route", provider.speaker_models)
        self.assertTrue(any(
            event.get("type") == "speaker_failed"
            and (event.get("member") or {}).get("id") == selected["id"]
            and event.get("error_code") == "member_changed_before_provider"
            for event in events
        ))

    def test_selected_member_disabled_during_director_call_fails_closed(self) -> None:
        moderator, members = self.configure_moderator()
        selected = next(
            member for member in members[2:] if member["id"] != moderator["id"]
        )
        disabled = False

        def disable_selected() -> None:
            nonlocal disabled
            if disabled:
                return
            disabled = True
            self.store.update_member(
                "room_plan", str(selected["id"]), {"enabled": False}
            )

        provider = DirectorScenarioProvider(
            str(selected["id"]), on_director=disable_selected
        )
        orchestrator = DiscussionOrchestrator(
            self.store, ScenarioRegistry(provider), market_service=None
        )
        events = list(orchestrator.run_round(
            "room_plan", "disable selected member during director"
        ))
        round_id = self.round_id(events)
        attempts = self.store.list_director_attempts(
            "room_plan", round_id=round_id
        )

        self.assertTrue(any(
            event.get("code") == "ROUND_SELECTED_MEMBER_UNAVAILABLE"
            for event in events
        ))
        self.assertEqual([attempt["status"] for attempt in attempts], ["CANCELLED"])
        self.assertEqual(
            attempts[0]["error_code"], "director_selected_member_unavailable"
        )
        self.assertEqual(
            self.store.get_round("room_plan", round_id)["status"], "PAUSED"
        )

    def test_moderator_disabled_during_hidden_call_cancels_decision(self) -> None:
        moderator, members = self.configure_moderator()
        replacement = next(member for member in members if member["id"] != moderator["id"])
        selected = next(
            member for member in members[2:]
            if member["id"] not in {moderator["id"], replacement["id"]}
        )
        disabled = False

        def disable_moderator() -> None:
            nonlocal disabled
            if disabled:
                return
            disabled = True
            room = self.store.room_snapshot("room_plan")["room"]
            self.store.update_room(
                "room_plan",
                {
                    "expected_settings_version": room["settings_version"],
                    "moderator_member_id": replacement["id"],
                },
            )
            self.store.update_member(
                "room_plan", str(moderator["id"]), {"enabled": False}
            )

        provider = DirectorScenarioProvider(
            str(selected["id"]), on_director=disable_moderator
        )
        orchestrator = DiscussionOrchestrator(
            self.store, ScenarioRegistry(provider), market_service=None
        )
        events = list(orchestrator.run_round(
            "room_plan", "disable moderator during hidden call"
        ))
        round_id = self.round_id(events)
        attempts = self.store.list_director_attempts(
            "room_plan", round_id=round_id
        )

        self.assertTrue(any(
            event.get("code") == "ROUND_MODERATOR_UNAVAILABLE"
            for event in events
        ))
        self.assertEqual([attempt["status"] for attempt in attempts], ["CANCELLED"])
        self.assertEqual(
            attempts[0]["error_code"], "director_moderator_unavailable"
        )
        self.assertFalse(any(
            event.get("type") == "message"
            and (event.get("member") or {}).get("id") == selected["id"]
            for event in events
        ))

    def test_ordinary_moderator_failure_does_not_disable_frozen_director_route(self) -> None:
        moderator, members = self.configure_moderator(member_index=0)
        selected = next(member for member in members[2:] if member["id"] != moderator["id"])
        provider = DirectorScenarioProvider(str(selected["id"]))
        orchestrator = DiscussionOrchestrator(
            self.store, ScenarioRegistry(provider), market_service=None
        )
        stream = orchestrator.run_round(
            "room_plan",
            "ordinary moderator failure isolation",
            skip_provider_ids={"openai"},
        )
        started = next(stream)
        self.store.update_member(
            "room_plan",
            str(moderator["id"]),
            {"provider": "openai", "model": "must-be-skipped"},
        )
        events = [started, *list(stream)]
        round_id = self.round_id(events)

        self.assertFalse(any(
            event.get("type") in {"speaker_failed", "message"}
            and (event.get("member") or {}).get("id") == moderator["id"]
            for event in events
        ))
        self.assertFalse(any(
            event.get("code") == "ROUND_MODERATOR_UNAVAILABLE"
            for event in events
        ))
        decisions = self.store.list_director_decisions(
            "room_plan", round_id=round_id
        )
        self.assertTrue(decisions)
        self.assertTrue(all(
            decision["moderator_context"]["provider"] == "deepseek"
            and decision["moderator_context"]["model"] == "director-model"
            for decision in decisions
        ))
        self.assertTrue(all(
            model == "director-model" for model in provider.director_models
        ))

    def test_checkpoint_freezes_room_and_moderator_route_across_resume(self) -> None:
        moderator, members = self.configure_moderator(model="frozen-director-model")
        selected = next(member for member in members if member["id"] != moderator["id"])
        provider = DirectorScenarioProvider(str(selected["id"]))
        orchestrator = DiscussionOrchestrator(
            self.store, ScenarioRegistry(provider), market_service=None
        )
        stream = orchestrator.run_round("room_plan", "freeze complete director route")
        started = next(stream)
        stream.close()
        round_id = str(started["round"]["id"])
        frozen = self.store.get_round_checkpoint("room_plan", round_id)["state"]

        expected = {
            "discussion_mode": "dynamic",
            "domain": "open_collaboration",
            "moderator_member_id": moderator["id"],
            "moderator_member_version": moderator["version"],
            "moderator_provider": "deepseek",
            "moderator_model": "frozen-director-model",
        }
        for key, value in expected.items():
            self.assertEqual(frozen[key], value)

        room = self.store.room_snapshot("room_plan")["room"]
        self.store.update_room(
            "room_plan",
            {
                "expected_settings_version": room["settings_version"],
                "discussion_mode": "sequential",
                "domain": "changed_domain",
                "moderator_member_id": selected["id"],
            },
        )
        self.store.update_member(
            "room_plan",
            str(moderator["id"]),
            {"provider": "doubao", "model": "changed-model"},
        )

        resumed = list(orchestrator.run_round(
            "room_plan", "", resume_round_id=round_id
        ))
        restored = self.store.get_round_checkpoint("room_plan", round_id)["state"]

        self.assertEqual(resumed[0]["type"], "round_resumed")
        self.assertGreater(provider.director_calls, 0)
        self.assertTrue(all(model == "frozen-director-model" for model in provider.director_models))
        for key, value in expected.items():
            self.assertEqual(restored[key], value)

    def test_legacy_v7_without_complete_route_fails_closed_without_provider(self) -> None:
        moderator, members = self.configure_moderator()
        selected = next(member for member in members if member["id"] != moderator["id"])
        provider = DirectorScenarioProvider(str(selected["id"]))
        orchestrator = DiscussionOrchestrator(
            self.store, ScenarioRegistry(provider), market_service=None
        )
        stream = orchestrator.run_round("room_plan", "legacy v7 compatibility")
        started = next(stream)
        stream.close()
        round_id = str(started["round"]["id"])
        state = self.store.get_round_checkpoint("room_plan", round_id)["state"]
        state["version"] = 7
        for field in (
            "candidate_risk_review_version",
            "candidate_risk_review_required",
            "turn_envelope_version",
            "turn_envelope_schema_sha256",
            "turn_output_modes_by_member",
            "discussion_mode",
            "domain",
            "moderator_member_version",
            "moderator_provider",
            "moderator_model",
        ):
            state.pop(field, None)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """UPDATE rounds
                      SET candidate_risk_review_version=NULL,
                          turn_envelope_version=NULL,
                          turn_envelope_schema_sha256=NULL
                    WHERE room_id=? AND id=?""",
                ("room_plan", round_id),
            )
        self.store.save_round_checkpoint("room_plan", round_id, state)

        restored_legacy = self.store.get_round_checkpoint("room_plan", round_id)["state"]
        self.assertEqual(restored_legacy["version"], 7)
        self.assertNotIn("discussion_mode", restored_legacy)
        resumed = list(orchestrator.run_round(
            "room_plan", "", resume_round_id=round_id
        ))

        self.assertEqual(resumed[0]["type"], "error")
        self.assertEqual(resumed[0]["code"], "ROUND_CHECKPOINT_INVALID")
        self.assertEqual(provider.director_calls, 0)
        self.assertEqual(provider.speaker_calls, 0)


if __name__ == "__main__":
    unittest.main()
