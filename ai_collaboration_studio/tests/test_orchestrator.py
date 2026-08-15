from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend.orchestrator import DiscussionOrchestrator
from backend.provider_call_ledger import ProviderCallLedger
from backend.providers.base import ProviderProbeResult, ProviderResponse
from backend.providers.openai_provider import _http_error_text
from backend.providers.registry import ProviderRegistry
from backend.round_contexts import build_round_context_authorization_set
from backend.round_launch_plan import RoundLaunchPlanService
from backend.store import StudioStore
from backend.turn_contract import TURN_CONTRACT_VERSION
from backend.turn_envelope import (
    TURN_ENVELOPE_SCHEMA_SHA256,
    TURN_ENVELOPE_VERSION,
)
from tests.storage_research_fixture import ready_storage_research_evidence
from tests.turn_contract_fixture import append_valid_turn_contract


class FakeProvider:
    provider_id = "openai"

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def status(self) -> dict[str, object]:
        return {
            "id": self.provider_id,
            "name": "Fake OpenAI",
            "configured": True,
            "model": "fake-model",
        }

    def probe(self, *, model: str = "") -> ProviderProbeResult:
        return ProviderProbeResult(
            provider=self.provider_id,
            model=model or "fake-model",
            configured=True,
            reachable=True,
            model_access=True,
            latency_ms=0,
            message="测试 Provider 可用。",
        )

    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        if "隐藏主持调度器" in instructions:
            return ProviderResponse(
                ok=True,
                provider=self.provider_id,
                model=model or "fake-model",
                content="测试中使用安全回退顺序",
            )
        self.calls.append({"instructions": instructions, "input_text": input_text, "model": model})
        content = append_valid_turn_contract(
            f"第 {len(self.calls)} 位成员发言，并回应前序观点。",
            instructions=instructions,
            input_text=input_text,
        )
        return ProviderResponse(
            ok=True,
            provider=self.provider_id,
            model=model or "fake-model",
            content=content,
        )


class FakeRegistry(ProviderRegistry):
    def __init__(self, provider: FakeProvider) -> None:
        self.provider = provider
        super().__init__({"openai": provider})

    def get(self, _provider_id: str) -> FakeProvider:
        self.provider.provider_id = str(_provider_id or "openai")
        return self.provider


class PlainTextProvider(FakeProvider):
    """Offline fixture for explicit invalid-contract fail-closed coverage."""

    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        if "隐藏主持调度器" in instructions:
            return super().generate(
                instructions=instructions,
                input_text=input_text,
                model=model,
            )
        self.calls.append({"instructions": instructions, "input_text": input_text, "model": model})
        return ProviderResponse(
            ok=True,
            provider=self.provider_id,
            model=model or "fake-model",
            content="这条测试发言故意缺少发言合同。",
        )


class LegacyXmlProvider(FakeProvider):
    """Offline fixture proving new envelope rounds never downgrade to XML."""

    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        if '"action":"speak|finish"' in instructions:
            return super().generate(
                instructions=instructions,
                input_text=input_text,
                model=model,
            )
        self.calls.append({"instructions": instructions, "input_text": input_text, "model": model})
        return ProviderResponse(
            ok=True,
            provider=self.provider_id,
            model=model or "fake-model",
            content=append_valid_turn_contract(
                "Legacy XML fixture output.",
                instructions=instructions,
                input_text=input_text,
                output_format="legacy_xml",
            ),
        )


class CountingProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.generate_call_count = 0

    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        self.generate_call_count += 1
        return super().generate(
            instructions=instructions,
            input_text=input_text,
            model=model,
        )


class ReadyMarketService:
    def snapshot(self) -> dict[str, object]:
        return {
            "ok": True,
            "state": "ready",
            "source": "futu_opend",
            "snapshot_id": "orchestrator-ready-snapshot",
            "captured_at": "2026-07-20T20:00:00Z",
            "rows": [
                {
                    "symbol": symbol,
                    "last": 100 + index,
                    "quality": "ready",
                    "age_seconds": 60,
                    "quote_is_live": True,
                    "freshness_basis": "live_20m_window",
                    "market_time": "2026-07-20 15:59:00",
                }
                for index, symbol in enumerate(
                    ("US.MU", "US.SNDK", "US.WDC", "US.STX")
                )
            ],
            "missing_symbols": [],
            "source_errors": [],
            "evidence": ready_storage_research_evidence(
                captured_at="2026-07-20T20:00:00Z",
                technical_as_of="2026-07-20 00:00:00",
            ),
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

    @staticmethod
    def prompt_context(snapshot: dict[str, object]) -> str:
        return f"snapshot_id={snapshot['snapshot_id']}"

    @staticmethod
    def timeline_summary(snapshot: dict[str, object]) -> str:
        return f"共享快照 {snapshot['snapshot_id']}"


class FailingProvider(FakeProvider):
    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        return ProviderResponse(
            ok=False,
            provider=self.provider_id,
            model=model,
            error="测试配额不足",
            error_code="http_status",
        )


class ExplodingProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.speaker_attempts = 0

    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        if "隐藏主持调度器" in instructions:
            return super().generate(
                instructions=instructions,
                input_text=input_text,
                model=model,
            )
        self.speaker_attempts += 1
        raise TimeoutError("Bearer upstream-secret must not escape")


class UnsafeSelectiveFailureProvider(FakeProvider):
    def __init__(self, target_name: str) -> None:
        super().__init__()
        self.target_name = target_name
        self.speaker_attempts: dict[str, int] = {}
        self.director_calls = 0

    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        if "隐藏主持调度器" in instructions:
            self.director_calls += 1
            return super().generate(
                instructions=instructions,
                input_text=input_text,
                model=model,
            )
        member_name = instructions.split("「", 1)[1].split("」", 1)[0]
        self.speaker_attempts[member_name] = self.speaker_attempts.get(member_name, 0) + 1
        if member_name == self.target_name:
            return ProviderResponse(
                ok=False,
                provider="untrusted-upstream-provider",
                model="untrusted-upstream-model",
                error="raw upstream body with upstream-error-secret",
                error_code="http_status",
            )
        return super().generate(
            instructions=instructions,
            input_text=input_text,
            model=model,
        )


class EditingProvider(FakeProvider):
    def __init__(self, store: StudioStore, room_id: str, member: dict[str, object]) -> None:
        super().__init__()
        self.store = store
        self.room_id = room_id
        self.member = member

    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        if "隐藏主持调度器" in instructions:
            return super().generate(instructions=instructions, input_text=input_text, model=model)
        response = super().generate(instructions=instructions, input_text=input_text, model=model)
        if len(self.calls) == 1:
            self.store.update_member(self.room_id, str(self.member["id"]), {
                **self.member,
                "identity": "讨论中刚刚调整的新身份",
                "responsibilities": "从下一次发言开始使用新的职责。",
            })
        return response


class RouteRecordingProvider(FakeProvider):
    def __init__(self, provider_id: str, on_first_speaker_call=None) -> None:
        super().__init__()
        self.provider_id = provider_id
        self.on_first_speaker_call = on_first_speaker_call

    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        if "隐藏主持调度器" in instructions:
            return ProviderResponse(
                ok=True,
                provider=self.provider_id,
                model=model or "fixture-model",
                content="测试中使用顺序调度。",
            )
        self.calls.append({"instructions": instructions, "input_text": input_text, "model": model})
        if len(self.calls) == 1 and self.on_first_speaker_call:
            self.on_first_speaker_call()
        content = append_valid_turn_contract(
            f"{self.provider_id} 使用 {model} 完成发言。",
            instructions=instructions,
            input_text=input_text,
        )
        return ProviderResponse(
            ok=True,
            provider=self.provider_id,
            model=model or "fixture-model",
            content=content,
        )


class MappingFakeRegistry:
    def __init__(self, providers: dict[str, RouteRecordingProvider]) -> None:
        self.providers = providers

    def get(self, provider_id: str):
        return self.providers.get(str(provider_id or "").lower())


class DirectedProvider(FakeProvider):
    def __init__(self, selected_member_id: str) -> None:
        super().__init__()
        self.selected_member_id = selected_member_id
        self.director_calls = 0
        self.director_instructions: list[str] = []
        self.director_inputs: list[str] = []

    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        if "隐藏主持调度器" in instructions:
            self.director_calls += 1
            self.director_instructions.append(instructions)
            self.director_inputs.append(input_text)
            return ProviderResponse(
                ok=True,
                provider=self.provider_id,
                model=model or "fake-model",
                content=json.dumps({
                    "action": "speak",
                    "member_id": self.selected_member_id,
                    "reason": "该成员最适合回应当前证据缺口。",
                }, ensure_ascii=False),
            )
        return super().generate(instructions=instructions, input_text=input_text, model=model)


class ObservationProposalProvider(FakeProvider):
    def __init__(self, symbol: str = "US.MU") -> None:
        super().__init__()
        self.symbol = symbol

    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        if "隐藏主持调度器" in instructions:
            return super().generate(instructions=instructions, input_text=input_text, model=model)
        if "流程阶段：decision" in instructions:
            self.calls.append({"instructions": instructions, "input_text": input_text, "model": model})
            payload = {
                "observations": [{
                    "symbol": self.symbol,
                    "direction": "UP",
                    "horizon_days": 5,
                    "threshold_pct": 2,
                    "thesis": "统一证据支持五个交易日观察。",
                    "counter_case": "若价格结构失效则推翻。",
                    "model_confidence": 68,
                    "evidence": {"material_ids": []},
                }]
            }
            content = append_valid_turn_contract(
                f"投委会建议记录为待确认观察，最终决定权属于用户。\n<observation_proposals>{json.dumps(payload, ensure_ascii=False)}</observation_proposals>",
                instructions=instructions,
                input_text=input_text,
            )
            return ProviderResponse(
                ok=True,
                provider=self.provider_id,
                model=model or "fake-model",
                content=content,
            )
        return super().generate(instructions=instructions, input_text=input_text, model=model)


class FailingFinishLedger:
    def __init__(self, delegate: ProviderCallLedger) -> None:
        self.delegate = delegate

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)

    def finish(self, *args, **kwargs):
        raise RuntimeError("Bearer ledger-secret must not escape")


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "studio.sqlite3")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def envelope_member_routes(
        self,
        room_id: str,
        *,
        default_model: str = "fake-model",
    ) -> dict[str, object]:
        return {
            "version": "provider_member_routes_v2",
            "members": sorted(
                [
                    {
                        "member_id": str(member["id"]),
                        "approved_member_version": int(member["version"]),
                        "provider": str(member.get("provider") or "deepseek"),
                        "model": str(member.get("model") or default_model),
                        "turn_output_mode": "prompt_json",
                        "turn_envelope_version": TURN_ENVELOPE_VERSION,
                        "turn_envelope_schema_sha256": (
                            TURN_ENVELOPE_SCHEMA_SHA256
                        ),
                    }
                    for member in self.store.enabled_members(room_id)
                ],
                key=lambda item: item["member_id"],
            ),
        }

    def make_flexible_candidates_semantically_tied(
        self,
        room_id: str = "room_plan",
    ) -> None:
        """Force the fixture onto the hidden-model arbitration path."""
        for member in self.store.enabled_members(room_id):
            if member.get("workflow_stage") != "flexible":
                continue
            capabilities = sorted({
                *list(member.get("capabilities") or []),
                "critical_review",
                "evidence_review",
            })
            self.store.update_member(
                room_id,
                str(member["id"]),
                {"capabilities": capabilities},
            )

    def _use_text_only_storage_fixture(self) -> None:
        """Keep legacy prose-provider tests focused on their non-contract behavior."""
        room = self.store.room_snapshot("room_storage")["room"]
        self.store.update_room("room_storage", {
            "expected_settings_version": room["settings_version"],
            "capability_pack_ids": ["storage_research_readonly"],
        })

    def test_members_speak_in_order_and_read_prior_ai_message(self) -> None:
        provider = FakeProvider()
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(provider))

        events = list(orchestrator.run_round("room_plan", "讨论一个可落地的新方案"))

        messages = [event for event in events if event["type"] == "message"]
        expected_members = len(self.store.enabled_members("room_plan"))
        self.assertEqual(len(messages), expected_members)
        self.assertEqual([event["order"] for event in messages], list(range(1, expected_members + 1)))
        self.assertIn("第 1 位成员发言", provider.calls[1]["input_text"])
        self.assertIn("战略主持人", provider.calls[1]["instructions"])
        self.assertEqual(events[-1]["type"], "round_completed")
        self.assertEqual(events[-1]["status"], "COMPLETED")

        round_id = events[-1]["round_id"]
        round_messages = self.store.round_messages("room_plan", round_id)
        user_message = next(
            message for message in round_messages
            if message["sender_type"] == "user"
        )
        ai_messages = [
            message for message in round_messages
            if message["sender_type"] == "ai"
        ]
        self.assertEqual(ai_messages[0]["reply_to_message_id"], user_message["id"])
        prior_ai_ids = {ai_messages[0]["id"]}
        for current in ai_messages[1:]:
            self.assertIn(current["reply_to_message_id"], prior_ai_ids)
            self.assertIn(
                current["reply_to_message_id"],
                {
                    response["id"]
                    for response in current["turn_contract"]["responds_to"]
                },
            )
            prior_ai_ids.add(current["id"])

    def test_only_formal_decision_turn_receives_canonical_candidate_snapshot(self) -> None:
        provider = FakeProvider()
        events = list(DiscussionOrchestrator(
            self.store,
            FakeRegistry(provider),
        ).run_round("room_plan", "验证决策提示只使用规范候选快照"))

        self.assertEqual(events[-1]["status"], "COMPLETED")
        marker = "服务端规范候选只读快照（candidate_lineage_v1，仅供决策角色）：\n"
        decision_calls = [
            call for call in provider.calls
            if "流程阶段：decision。" in call["instructions"]
        ]
        non_decision_calls = [
            call for call in provider.calls
            if "流程阶段：decision。" not in call["instructions"]
        ]
        self.assertEqual(len(decision_calls), 1)
        self.assertTrue(non_decision_calls)
        self.assertTrue(all(marker not in call["input_text"] for call in non_decision_calls))
        self.assertIn(marker, decision_calls[0]["input_text"])
        snapshot_line = decision_calls[0]["input_text"].split(marker, 1)[1].splitlines()[0]
        snapshot = json.loads(snapshot_line)
        self.assertTrue(snapshot["read_only"])
        self.assertTrue(snapshot["ready"])
        self.assertEqual(
            [item["id"] for item in snapshot["candidates"]],
            ["fixture_option_a", "fixture_option_b"],
        )
        self.assertEqual(snapshot["candidates"][0]["title"], "Fixture option A")
        self.assertEqual(
            snapshot["candidates"][0]["thesis"],
            "Continue the read-only research workflow.",
        )
        self.assertFalse(snapshot["live_trading_allowed"])

    def test_new_formal_round_freezes_core_protocol_without_legacy_pack(self) -> None:
        room = self.store.room_snapshot("room_plan")["room"]
        self.assertEqual(room["capability_pack_ids"], [])
        self.assertNotIn("discussion.turn_contract_v1", room["capabilities"])

        events = list(DiscussionOrchestrator(
            self.store,
            FakeRegistry(FakeProvider()),
        ).run_round("room_plan", "验证新正式轮内核协议"))
        round_id = events[-1]["round_id"]
        round_row = self.store.get_round("room_plan", round_id)
        checkpoint = self.store.get_round_checkpoint("room_plan", round_id)["state"]
        ai_messages = [
            message for message in self.store.round_messages("room_plan", round_id)
            if message["sender_type"] == "ai"
        ]

        self.assertEqual(round_row["turn_contract_version"], TURN_CONTRACT_VERSION)
        self.assertEqual(checkpoint["turn_contract_version"], TURN_CONTRACT_VERSION)
        self.assertTrue(checkpoint["turn_contract_required"])
        self.assertEqual(checkpoint["capability_pack_ids"], [])
        self.assertTrue(ai_messages)
        self.assertTrue(all(message["turn_contract_qualified"] for message in ai_messages))
        self.assertTrue(all(message["turn_contract_integrity_ok"] for message in ai_messages))

    def test_formal_turn_prompt_publishes_risk_and_action_enums(self) -> None:
        room = self.store.room_snapshot("room_plan")["room"]
        member = self.store.enabled_members("room_plan")[0]
        instructions = DiscussionOrchestrator(
            self.store,
            FakeRegistry(FakeProvider()),
        )._instructions(
            room,
            member,
            "我",
            turn_contract_required=True,
        )

        self.assertIn(
            "severity 只能是 unknown、low、medium、high、critical",
            instructions,
        )
        self.assertIn(
            "status 只能是 open、monitoring、mitigated、accepted",
            instructions,
        )
        self.assertIn(
            "state 只能是 open、in_progress、blocked、done",
            instructions,
        )
        self.assertIn(
            "direction 只能是 UP、DOWN、NEUTRAL、FLAT、UNSPECIFIED",
            instructions,
        )
        self.assertIn(
            "horizon_days 只能是 null 或 1 到 3650 的整数",
            instructions,
        )
        self.assertIn(
            "blocking 必须是 JSON 布尔值 true 或 false，不能是字符串",
            instructions,
        )
        self.assertIn(
            "所有 id 必须以英文字母开头，仅含字母、数字、下划线或短横线，最长 80",
            instructions,
        )
        self.assertIn(
            "claims、responds_to、risks、next_actions 各最多 12 项，candidate_updates 最多 8 项，每个 evidence 最多 8 项",
            instructions,
        )

    def test_formal_new_round_accepts_exact_current_launch_plan_hash(self) -> None:
        provider = CountingProvider()
        registry = FakeRegistry(provider)
        objective = "verify exact launch plan binding"
        plan = RoundLaunchPlanService(self.store, registry).build(
            "room_plan",
            objective,
        )
        orchestrator = DiscussionOrchestrator(self.store, registry)

        events = list(orchestrator.run_round(
            "room_plan",
            objective,
            expected_launch_plan_hash=plan["plan_hash"],
        ))

        self.assertEqual(events[-1]["type"], "round_completed")
        self.assertGreater(provider.generate_call_count, 0)
        self.assertFalse(any(
            event.get("code") == "ROUND_LAUNCH_PLAN_DRIFT"
            for event in events
        ))

    def test_generic_empty_round_context_set_is_accepted_and_frozen(self) -> None:
        provider = FakeProvider()
        events = list(DiscussionOrchestrator(
            self.store,
            FakeRegistry(provider),
        ).run_round(
            "room_plan",
            "freeze the provider-neutral empty context set",
            round_context_authorizations=build_round_context_authorization_set([]),
        ))

        started = next(event for event in events if event["type"] == "round_started")
        frozen = self.store.get_round_contexts(
            "room_plan",
            started["round"]["id"],
        )
        self.assertEqual(events[-1]["type"], "round_completed")
        self.assertIsNotNone(frozen)
        self.assertTrue((frozen or {}).get("integrity_ok"))
        self.assertEqual((frozen or {}).get("round_domain_context_count"), 0)
        self.assertEqual((frozen or {}).get("contexts"), [])

    def test_round_context_prompt_renderer_uses_canonical_json(self) -> None:
        section = {
            "title": "generic context",
            "version": "round_context_prompt_section_v1",
            "payload_sha256": "a" * 64,
            "payload": {"z": 2, "a": {"value": 1}},
            "port_id": "core.example.context/v1",
            "owner_pack_id": "example_readonly",
        }

        rendered = DiscussionOrchestrator._render_round_context_prompt_sections(
            [section]
        )

        expected = json.dumps(
            section,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        self.assertEqual(rendered, f"[Frozen round context 1]\n{expected}")

    def test_formal_new_round_rejects_plan_drift_before_provider_or_round_writes(self) -> None:
        provider = CountingProvider()
        registry = FakeRegistry(provider)
        objective = "freeze the reviewed launch plan"
        plan = RoundLaunchPlanService(self.store, registry).build(
            "room_plan",
            objective,
        )
        member = self.store.enabled_members("room_plan")[0]
        self.store.update_member(
            "room_plan",
            member["id"],
            {"identity": f"{member['identity']} changed after confirmation"},
            expected_version=member["version"],
        )
        with closing(sqlite3.connect(self.store.path)) as connection:
            counts_before = tuple(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE room_id=?",
                    ("room_plan",),
                ).fetchone()[0]
                for table in ("rounds", "messages")
            )

        events = list(DiscussionOrchestrator(self.store, registry).run_round(
            "room_plan",
            objective,
            expected_launch_plan_hash=plan["plan_hash"],
        ))

        with closing(sqlite3.connect(self.store.path)) as connection:
            counts_after = tuple(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE room_id=?",
                    ("room_plan",),
                ).fetchone()[0]
                for table in ("rounds", "messages")
            )
        self.assertEqual(events, [{
            "type": "error",
            "code": "ROUND_LAUNCH_PLAN_DRIFT",
            "error": "The confirmed round launch plan no longer matches current room settings.",
        }])
        self.assertEqual(provider.generate_call_count, 0)
        self.assertEqual(counts_after, counts_before)

    def test_formal_launch_plan_failure_does_not_leak_exception_text(self) -> None:
        provider = CountingProvider()
        events = list(DiscussionOrchestrator(
            self.store,
            FakeRegistry(provider),
        ).run_round(
            "room_plan",
            "\ufffdBearer launch-plan-secret",
            expected_launch_plan_hash="a" * 64,
        ))

        serialized = json.dumps(events, ensure_ascii=False)
        self.assertEqual(events[-1]["code"], "ROUND_LAUNCH_PLAN_DRIFT")
        self.assertNotIn("launch-plan-secret", serialized)
        self.assertEqual(provider.generate_call_count, 0)

    def test_resume_ignores_new_round_launch_plan_binding(self) -> None:
        room = self.store.room_snapshot("room_plan")["room"]
        self.store.update_room("room_plan", {
            "expected_settings_version": room["settings_version"],
            "discussion_mode": "sequential",
        })
        provider = CountingProvider()
        registry = FakeRegistry(provider)
        ledger = ProviderCallLedger.create(
            self.store,
            "room_plan",
            scope="round",
            client_request_id="resume-plan-binding-unaffected",
            plan_hash="8" * 64,
            max_calls=1,
            skip_provider_ids=[],
            member_routes=self.envelope_member_routes("room_plan"),
        )
        first_events = list(DiscussionOrchestrator(self.store, registry).run_round(
            "room_plan",
            "create a safely resumable round",
            provider_call_ledger=ledger,
        ))
        round_id = next(
            event["round"]["id"]
            for event in first_events
            if event["type"] == "round_started"
        )

        resumed = list(DiscussionOrchestrator(self.store, registry).run_round(
            "room_plan",
            "",
            resume_round_id=round_id,
            expected_launch_plan_hash="not-a-current-plan-hash",
        ))

        self.assertEqual(resumed[-1]["code"], "PROVIDER_CALL_BUDGET_EXCEEDED")
        self.assertFalse(any(
            event.get("code") == "ROUND_LAUNCH_PLAN_DRIFT"
            for event in resumed
        ))

    def test_resume_rejects_even_empty_new_round_context_authorization(self) -> None:
        orchestrator = DiscussionOrchestrator(
            self.store,
            FakeRegistry(FakeProvider()),
        )
        stream = orchestrator.run_round("room_plan", "pause with frozen contexts")
        started = next(stream)
        self.assertEqual(started["type"], "round_started")
        stream.close()
        round_id = started["round"]["id"]

        events = list(orchestrator.run_round(
            "room_plan",
            "",
            resume_round_id=round_id,
            round_context_authorizations=build_round_context_authorization_set([]),
        ))

        self.assertEqual(events, [{
            "type": "error",
            "code": "ROUND_CONTEXT_AUTHORIZATION_NOT_ALLOWED_ON_RESUME",
            "error": "Paused rounds resume only with their frozen round contexts.",
        }])
        self.assertEqual(self.store.get_round("room_plan", round_id)["status"], "PAUSED")

    def test_resume_fails_closed_when_frozen_round_context_anchor_is_tampered(self) -> None:
        orchestrator = DiscussionOrchestrator(
            self.store,
            FakeRegistry(FakeProvider()),
        )
        stream = orchestrator.run_round("room_plan", "pause before context tamper")
        started = next(stream)
        self.assertEqual(started["type"], "round_started")
        stream.close()
        round_id = started["round"]["id"]
        with closing(sqlite3.connect(self.store.path)) as connection:
            # Simulate offline file corruption after bypassing the normal
            # immutability trigger; application writes cannot reach this state.
            connection.execute(
                "DROP TRIGGER trg_round_domain_context_anchor_no_update"
            )
            connection.execute(
                """UPDATE rounds
                   SET round_domain_context_count=1,
                       round_domain_contexts_sha256=?
                   WHERE room_id=? AND id=?""",
                ("f" * 64, "room_plan", round_id),
            )
            connection.commit()

        events = list(orchestrator.run_round(
            "room_plan",
            "",
            resume_round_id=round_id,
        ))

        self.assertEqual(events, [{
            "type": "error",
            "code": "ROUND_CONTEXT_INTEGRITY_FAILED",
            "error": "Frozen round contexts failed integrity checks.",
        }])
        frozen = self.store.get_round_contexts("room_plan", round_id)
        self.assertFalse((frozen or {}).get("integrity_ok"))
        self.assertEqual(self.store.get_round("room_plan", round_id)["resume_count"], 0)

    def test_provider_call_ledger_pauses_before_exceeding_sequential_limit(self) -> None:
        room = self.store.room_snapshot("room_plan")["room"]
        self.store.update_room("room_plan", {
            "expected_settings_version": room["settings_version"],
            "discussion_mode": "sequential",
        })
        provider = FakeProvider()
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(provider))
        ledger = ProviderCallLedger.create(
            self.store,
            "room_plan",
            scope="round",
            client_request_id="sequential-budget-one",
            plan_hash="a" * 64,
            max_calls=1,
            skip_provider_ids=[],
            member_routes=self.envelope_member_routes("room_plan"),
        )

        events = list(orchestrator.run_round(
            "room_plan",
            "验证 Provider 调用次数硬上限",
            provider_call_ledger=ledger,
        ))

        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(events[-1]["code"], "PROVIDER_CALL_BUDGET_EXCEEDED")
        round_id = next(
            event["round"]["id"]
            for event in events
            if event["type"] == "round_started"
        )
        self.assertEqual(self.store.get_round("room_plan", round_id)["status"], "PAUSED")
        attempts = ledger.attempts()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["kind"], "round_speaker")
        self.assertEqual(attempts[0]["status"], "RESPONDED")
        self.assertEqual(ledger.snapshot()["remaining_calls"], 0)
        self.assertEqual(
            ProviderCallLedger.resume_for_round(
                self.store,
                "room_plan",
                round_id,
                scope="round",
            ).run_id,
            ledger.run_id,
        )

    def test_dynamic_round_preserves_last_call_for_visible_speaker(self) -> None:
        provider = FakeProvider()
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(provider))
        ledger = ProviderCallLedger.create(
            self.store,
            "room_plan",
            scope="round",
            client_request_id="dynamic-budget-two",
            plan_hash="b" * 64,
            max_calls=2,
            skip_provider_ids=[],
            member_routes=self.envelope_member_routes("room_plan"),
        )

        events = list(orchestrator.run_round(
            "room_plan",
            "额度不足时优先保留可见成员发言",
            provider_call_ledger=ledger,
        ))

        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(events[-1]["code"], "PROVIDER_CALL_BUDGET_EXCEEDED")
        attempts = ledger.attempts()
        self.assertEqual(
            [attempt["kind"] for attempt in attempts],
            ["round_speaker", "round_speaker"],
        )
        self.assertTrue(all(attempt["status"] == "RESPONDED" for attempt in attempts))

    def test_dynamic_director_call_is_charged_to_the_same_round_ledger(self) -> None:
        self.make_flexible_candidates_semantically_tied()
        provider = FakeProvider()
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(provider))
        ledger = ProviderCallLedger.create(
            self.store,
            "room_plan",
            scope="round",
            client_request_id="dynamic-budget-three",
            plan_hash="c" * 64,
            # One opening call + two minimum visible gap-closing calls leaves
            # exactly one independently auditable director call.
            max_calls=4,
            skip_provider_ids=[],
            member_routes=self.envelope_member_routes("room_plan"),
        )

        events = list(orchestrator.run_round(
            "room_plan",
            "主持调度与成员发言共用同一上限",
            provider_call_ledger=ledger,
        ))

        self.assertEqual(
            [attempt["kind"] for attempt in ledger.attempts()],
            [
                "round_speaker",
                "round_director",
                "round_speaker",
                "round_speaker",
            ],
        )
        self.assertEqual(
            [attempt["status"] for attempt in ledger.attempts()],
            ["RESPONDED", "INVALID", "RESPONDED", "RESPONDED"],
        )

    def test_formal_dynamic_moderator_uses_approved_resolved_default_model(self) -> None:
        self.make_flexible_candidates_semantically_tied()
        provider = FakeProvider()
        members = self.store.enabled_members("room_plan")
        member_routes = {
            "version": "provider_member_routes_v2",
            "members": sorted(
                [
                    {
                        "member_id": str(member["id"]),
                        "approved_member_version": int(member["version"]),
                        "provider": str(member.get("provider") or "deepseek"),
                        "model": "fake-model",
                        "turn_output_mode": "prompt_json",
                        "turn_envelope_version": TURN_ENVELOPE_VERSION,
                        "turn_envelope_schema_sha256": (
                            TURN_ENVELOPE_SCHEMA_SHA256
                        ),
                    }
                    for member in members
                ],
                key=lambda item: item["member_id"],
            ),
        }
        ledger = ProviderCallLedger.create(
            self.store,
            "room_plan",
            scope="round",
            client_request_id="dynamic-resolved-default-model",
            plan_hash="9" * 64,
            max_calls=4,
            skip_provider_ids=[],
            member_routes=member_routes,
        )

        events = list(DiscussionOrchestrator(
            self.store,
            FakeRegistry(provider),
        ).run_round(
            "room_plan",
            "解析后的默认模型也必须纳入主持路由封印",
            provider_call_ledger=ledger,
        ))

        self.assertFalse(any(
            event.get("code") == "PROVIDER_CALL_LEDGER_INVALID"
            for event in events
        ))
        self.assertEqual(
            [attempt["model"] for attempt in ledger.attempts()],
            ["fake-model", "fake-model", "fake-model", "fake-model"],
        )
        round_id = next(
            event["round"]["id"]
            for event in events
            if event["type"] == "round_started"
        )
        director_attempts = self.store.list_director_attempts(
            "room_plan",
            round_id=round_id,
        )
        self.assertEqual(director_attempts[0]["model"], "fake-model")

    def test_resume_without_explicit_ledger_reuses_exhausted_round_authorization(self) -> None:
        room = self.store.room_snapshot("room_plan")["room"]
        self.store.update_room("room_plan", {
            "expected_settings_version": room["settings_version"],
            "discussion_mode": "sequential",
        })
        provider = FakeProvider()
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(provider))
        ledger = ProviderCallLedger.create(
            self.store,
            "room_plan",
            scope="round",
            client_request_id="resume-omitted-ledger",
            plan_hash="e" * 64,
            max_calls=1,
            skip_provider_ids=[],
            member_routes=self.envelope_member_routes("room_plan"),
        )
        first_events = list(orchestrator.run_round(
            "room_plan",
            "验证恢复不能绕过调用上限",
            provider_call_ledger=ledger,
        ))
        round_id = next(
            event["round"]["id"]
            for event in first_events
            if event["type"] == "round_started"
        )

        resumed = list(orchestrator.run_round(
            "room_plan",
            "",
            resume_round_id=round_id,
        ))

        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(ledger.attempts()), 1)
        self.assertEqual(resumed[-1]["code"], "PROVIDER_CALL_BUDGET_EXCEEDED")
        self.assertEqual(ledger.snapshot()["remaining_calls"], 0)

    def test_resume_abandons_started_provider_attempt_without_refund(self) -> None:
        provider = FakeProvider()
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(provider))
        ledger = ProviderCallLedger.create(
            self.store,
            "room_plan",
            scope="round",
            client_request_id="resume-orphaned-attempt",
            plan_hash="f" * 64,
            max_calls=1,
            skip_provider_ids=[],
            member_routes=self.envelope_member_routes("room_plan"),
        )
        stream = orchestrator.run_round(
            "room_plan",
            "模拟 Provider 请求发出后进程中断",
            provider_call_ledger=ledger,
        )
        started = next(stream)
        self.assertEqual(started["type"], "round_started")
        approved_route = ledger.snapshot()["member_routes"]["members"][0]
        ledger.reserve(
            kind="round_speaker",
            provider=str(approved_route["provider"]),
            model=str(approved_route["model"]),
            member_id=str(approved_route["member_id"]),
            member_version=int(approved_route["approved_member_version"]),
        )
        stream.close()

        resumed = list(orchestrator.run_round(
            "room_plan",
            "",
            resume_round_id=started["round"]["id"],
        ))

        self.assertEqual(provider.calls, [])
        self.assertEqual(ledger.attempts()[0]["status"], "ABANDONED")
        self.assertEqual(ledger.snapshot()["remaining_calls"], 0)
        self.assertEqual(resumed[-1]["code"], "PROVIDER_CALL_BUDGET_EXCEEDED")

    def test_invalid_turn_contract_is_recorded_as_invalid_provider_response(self) -> None:
        provider = PlainTextProvider()
        orchestrator = DiscussionOrchestrator(
            self.store,
            FakeRegistry(provider),
            ReadyMarketService(),
        )
        ledger = ProviderCallLedger.create(
            self.store,
            "room_storage",
            scope="round",
            client_request_id="invalid-turn-contract-ledger",
            plan_hash="1" * 64,
            max_calls=1,
            skip_provider_ids=[],
            member_routes=self.envelope_member_routes("room_storage"),
        )

        events = list(orchestrator.run_round(
            "room_storage",
            "无发言合同的回复必须记为无效",
            provider_call_ledger=ledger,
        ))

        self.assertTrue(any(
            event.get("code") == "ROUND_TURN_CONTRACT_INVALID"
            for event in events
        ))
        self.assertEqual(ledger.attempts()[0]["status"], "INVALID")
        self.assertEqual(ledger.attempts()[0]["error_code"], "invalid_response")

    def test_new_envelope_round_rejects_legacy_xml_without_retry_or_downgrade(self) -> None:
        provider = LegacyXmlProvider()
        orchestrator = DiscussionOrchestrator(
            self.store,
            FakeRegistry(provider),
            ReadyMarketService(),
        )
        ledger = ProviderCallLedger.create(
            self.store,
            "room_storage",
            scope="round",
            client_request_id="legacy-xml-no-downgrade-ledger",
            plan_hash="a" * 64,
            max_calls=1,
            skip_provider_ids=[],
            member_routes=self.envelope_member_routes("room_storage"),
        )

        events = list(orchestrator.run_round(
            "room_storage",
            "New envelope rounds must not downgrade to legacy XML.",
            provider_call_ledger=ledger,
        ))

        self.assertEqual(len(provider.calls), 1)
        self.assertTrue(any(
            event.get("code") == "ROUND_TURN_CONTRACT_INVALID"
            for event in events
        ))
        self.assertEqual(len(ledger.attempts()), 1)
        self.assertEqual(ledger.attempts()[0]["status"], "INVALID")

    def test_ledger_internal_error_text_never_enters_round_stream(self) -> None:
        provider = FakeProvider()
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(provider))
        ledger = ProviderCallLedger.create(
            self.store,
            "room_plan",
            scope="round",
            client_request_id="ledger-error-redaction",
            plan_hash="2" * 64,
            max_calls=1,
            skip_provider_ids=[],
            member_routes=self.envelope_member_routes("room_plan"),
        )

        events = list(orchestrator.run_round(
            "room_plan",
            "账本异常必须脱敏",
            provider_call_ledger=FailingFinishLedger(ledger),
        ))

        serialized = json.dumps(events, ensure_ascii=False)
        self.assertIn("PROVIDER_CALL_LEDGER_FINALIZE_FAILED", serialized)
        self.assertNotIn("ledger-secret", serialized)

    def test_speaker_exception_consumes_one_slot_without_persisting_raw_error(self) -> None:
        provider = ExplodingProvider()
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(provider))
        ledger = ProviderCallLedger.create(
            self.store,
            "room_plan",
            scope="round",
            client_request_id="speaker-timeout-budget",
            plan_hash="d" * 64,
            max_calls=1,
            skip_provider_ids=[],
            member_routes=self.envelope_member_routes("room_plan"),
        )

        events = list(orchestrator.run_round(
            "room_plan",
            "异常调用也必须永久占用额度",
            provider_call_ledger=ledger,
        ))

        attempts = ledger.attempts()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "FAILED")
        self.assertEqual(attempts[0]["error_code"], "timeout")
        self.assertNotIn("upstream-secret", json.dumps(attempts, ensure_ascii=False))
        self.assertEqual(events[-1]["code"], "PROVIDER_CALL_BUDGET_EXCEEDED")

    def test_long_sequential_round_keeps_earliest_member_in_late_context(self) -> None:
        room = self.store.room_snapshot("room_plan")["room"]
        self.store.update_room("room_plan", {
            "expected_settings_version": room["settings_version"],
            "discussion_mode": "sequential",
        })
        for index in range(36):
            self.store.add_member("room_plan", {
                "name": f"扩展成员 {index + 1}",
                "identity": "长轮次上下文测试成员",
                "responsibilities": "读取并回应本轮全部前序发言。",
                "boundaries": "不得忽略较早发言。",
                "provider": "openai",
                "model": "fake-model",
            })
        provider = FakeProvider()
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(provider))

        events = list(orchestrator.run_round("room_plan", "验证长轮次前序可见性"))

        self.assertEqual(events[-1]["type"], "round_completed")
        self.assertEqual(len(provider.calls), 40)
        self.assertIn("第 1 位成员发言", provider.calls[-1]["input_text"])

    def test_bounded_transcript_indexes_every_early_message(self) -> None:
        transcript = [
            {
                "id": f"message-{index:03d}",
                "sender_name": f"成员 {index}",
                "identity": "测试身份",
                "content": (
                    f"EARLY-{index:03d} " + ("长文本 " * 180)
                    if index < 79
                    else "LAST-SENTINEL 最近发言必须保留原文"
                ),
            }
            for index in range(80)
        ]

        rendered = DiscussionOrchestrator._bounded_transcript_text(
            transcript,
            max_chars=18000,
            allowed_message_ids={item["id"] for item in transcript},
        )

        self.assertLessEqual(len(rendered), 18000)
        self.assertIn("较早发言压缩索引", rendered)
        self.assertIn("1. [成员 0]", rendered)
        self.assertIn("LAST-SENTINEL 最近发言必须保留原文", rendered)

    def test_round_provider_skip_is_enforced_after_member_hot_edit(self) -> None:
        member = self.store.enabled_members("room_plan")[0]
        self.store.update_member(
            "room_plan",
            member["id"],
            {"provider": "deepseek", "model": "fake-deepseek"},
        )
        provider = FakeProvider()
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(provider))
        stream = orchestrator.run_round(
            "room_plan",
            "会前检查后也不能热改绕过禁用策略",
            [member["id"]],
            skip_provider_ids={"openai"},
        )
        started = next(stream)
        self.assertEqual(started["type"], "round_started")
        current = self.store.get_member("room_plan", member["id"])
        self.store.update_member(
            "room_plan",
            member["id"],
            {**current, "provider": "openai", "model": "must-not-run"},
        )

        remaining = list(stream)
        failures = [event for event in remaining if event["type"] == "speaker_failed"]
        checkpoint = self.store.get_round_checkpoint("room_plan", started["round"]["id"])

        self.assertEqual(provider.calls, [])
        self.assertEqual(failures, [])
        self.assertFalse(any(event["type"] == "message" for event in remaining))
        self.assertTrue(any(
            event.get("type") == "director_decision"
            and event.get("action") == "finish"
            and event.get("source") == "provider_route_unavailable"
            for event in remaining
        ))
        self.assertEqual(remaining[-1]["status"], "PARTIAL")
        self.assertIn("openai", checkpoint["state"]["skip_provider_ids"])

    def test_resume_cannot_weaken_persisted_round_provider_skip(self) -> None:
        member = self.store.enabled_members("room_plan")[0]
        member = self.store.update_member("room_plan", member["id"], {
            **member,
            "provider": "openai",
            "model": "must-not-run",
        })
        provider = FakeProvider()
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(provider))
        stream = orchestrator.run_round(
            "room_plan",
            "恢复时继承禁用策略",
            [member["id"]],
            skip_provider_ids={"openai"},
        )
        started = next(stream)
        stream.close()

        resumed = list(orchestrator.run_round(
            "room_plan",
            "客户端不能通过空列表削弱原策略",
            resume_round_id=started["round"]["id"],
            skip_provider_ids=set(),
        ))

        self.assertEqual(provider.calls, [])
        self.assertFalse(any(
            event["type"] in {"speaker_failed", "message"}
            for event in resumed
        ))
        self.assertTrue(any(
            event.get("type") == "director_decision"
            and event.get("source") == "provider_route_unavailable"
            for event in resumed
        ))
        self.assertEqual(resumed[-1]["status"], "PARTIAL")
        checkpoint = self.store.get_round_checkpoint("room_plan", started["round"]["id"])
        self.assertEqual(checkpoint["state"]["skip_provider_ids"], ["openai"])

    def test_dynamic_director_never_calls_a_skipped_provider(self) -> None:
        members = self.store.enabled_members("room_plan")
        director_member = members[0]
        self.store.update_member(
            "room_plan",
            director_member["id"],
            {"provider": "openai", "model": "must-not-run"},
        )
        for member in members[1:]:
            self.store.update_member(
                "room_plan",
                member["id"],
                {"provider": "deepseek", "model": "fake-deepseek"},
            )
        provider = DirectedProvider(members[-1]["id"])
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(provider))

        events = list(orchestrator.run_round(
            "room_plan",
            "隐藏主持也必须遵守 Provider 禁用策略",
            skip_provider_ids={"openai"},
        ))

        self.assertEqual(provider.director_calls, 0)
        self.assertFalse(any(
            event["type"] in {"speaker_failed", "message"}
            and event.get("member", {}).get("id") == director_member["id"]
            for event in events
        ))
        self.assertTrue(any(event["type"] == "message" for event in events))

    def test_sequential_mode_is_explicitly_persisted_as_policy_scheduling(self) -> None:
        snapshot = self.store.room_snapshot("room_plan")
        self.store.update_room("room_plan", {
            "expected_updated_at": snapshot["room"]["updated_at"],
            "discussion_mode": "sequential",
        })
        provider = FakeProvider()
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(provider))

        events = list(orchestrator.run_round("room_plan", "验证规则顺序不会伪装成 AI 主持"))
        decisions = [event for event in events if event["type"] == "director_decision"]
        member_count = len(self.store.enabled_members("room_plan"))

        self.assertEqual(len(decisions), member_count)
        self.assertTrue(all(event["source"] == "policy" for event in decisions))
        self.assertTrue(all("规则顺序调度" in event["reason"] for event in decisions))
        self.assertTrue(all(
            event["decision"]["moderator_context"]["decision_authority"]
            == "service_policy"
            for event in decisions
        ))
        self.assertTrue(all(
            event["decision"]["moderator_context"]["model_used"] is False
            for event in decisions
        ))
        self.assertTrue(all(
            event["decision"]["moderator_context"]["discussion_mode"]
            == "sequential"
            for event in decisions
        ))
        self.assertEqual(
            [event["decision"]["sequence_no"] for event in decisions],
            list(range(1, member_count + 1)),
        )
        refreshed = self.store.room_snapshot("room_plan")["director_decisions"]
        self.assertEqual([item["id"] for item in refreshed], [event["decision"]["id"] for event in decisions])

    def test_impossible_workflow_stops_before_provider_calls_or_round_writes(self) -> None:
        snapshot = self.store.room_snapshot("room_plan")
        decision_member = next(
            member
            for member in snapshot["members"]
            if member["workflow_stage"] == "decision"
        )
        self.store.update_member(
            "room_plan",
            decision_member["id"],
            {"workflow_stage": "flexible"},
        )
        before = self.store.room_snapshot("room_plan")
        provider = FakeProvider()
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(provider))

        events = list(orchestrator.run_round("room_plan", "这轮不应被创建"))
        after = self.store.room_snapshot("room_plan")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["code"], "ROUND_WORKFLOW_PREFLIGHT_FAILED")
        self.assertIn(
            "WORKFLOW_STAGE_DECISION_MISSING",
            [item["code"] for item in events[0]["preflight"]["blockers"]],
        )
        self.assertEqual(provider.calls, [])
        self.assertEqual(after["latest_round"], before["latest_round"])
        self.assertEqual(after["round_checkpoint"], before["round_checkpoint"])
        self.assertEqual(after["messages"], before["messages"])

    def test_room_and_identity_changes_are_persistent(self) -> None:
        created = self.store.create_room(
            "新项目",
            "研究目标",
            domain="project_research",
            template_id="project_research",
        )
        member = created["members"][0]

        self.assertEqual(
            created["room"]["capability_pack_ids"],
            ["structured_project_research"],
        )
        self.assertIn("research.project.risk_register", created["room"]["capabilities"])
        self.assertNotIn("market.storage.readonly", created["room"]["capabilities"])

        updated = self.store.update_member(created["room"]["id"], member["id"], {
            **member,
            "name": "需求审查员",
            "identity": "检查真实需求",
            "instructions": "寻找伪需求和缺失用户证据。",
        })
        reloaded = self.store.room_snapshot(created["room"]["id"])

        self.assertEqual(updated["name"], "需求审查员")
        self.assertEqual(reloaded["members"][0]["identity"], "检查真实需求")
        self.assertEqual(updated["version"], 2)

    def test_storage_committee_is_a_room_template_not_the_product_boundary(self) -> None:
        snapshot = self.store.room_snapshot("room_storage")

        self.assertEqual(snapshot["room"]["category"], "交易研究 / 美股")
        self.assertEqual(snapshot["room"]["template_id"], "us_storage_committee")
        self.assertIn("MU、SNDK、WDC、STX", snapshot["room"]["objective"])
        self.assertEqual(len(snapshot["members"]), 12)
        self.assertIn("风险经理", [member["name"] for member in snapshot["members"]])
        self.assertIn("投委会决策经理", [member["name"] for member in snapshot["members"]])
        sentiment = next(member for member in snapshot["members"] if member["stance"] == "sentiment")
        self.assertEqual(sentiment["name"], "新闻与情绪分析师")
        self.assertEqual(sentiment["workflow_stage"], "analysis")
        self.assertIn("资金流不等于情绪", sentiment["boundaries"])
        guardian = next(member for member in snapshot["members"] if member["stance"] == "data_guardian")
        self.assertEqual(guardian["name"], "数据质量官")
        self.assertEqual(guardian["workflow_stage"], "analysis")
        self.assertIn("data_quality_review", guardian["capabilities"])
        self.assertEqual(
            next(member for member in snapshot["members"] if member["stance"] == "paper_trader")["workflow_stage"],
            "plan",
        )

    def test_sentiment_role_migration_does_not_override_later_user_removal(self) -> None:
        sentiment = next(
            member for member in self.store.room_snapshot("room_storage")["members"]
            if member["stance"] == "sentiment"
        )
        self.assertTrue(self.store.delete_member("room_storage", sentiment["id"]))

        reopened = StudioStore(self.store.path)

        self.assertNotIn("sentiment", [member["stance"] for member in reopened.room_snapshot("room_storage")["members"]])

    def test_members_can_be_added_reordered_archived_and_restored(self) -> None:
        created = self.store.create_room(
            "新研究群",
            "验证成员生命周期",
            template_id="open_collaboration",
        )
        room_id = created["room"]["id"]
        added = self.store.add_member(room_id, {
            "name": "临时专家",
            "identity": "用户自定义身份",
            "responsibilities": "补充特殊领域证据。",
            "boundaries": "只在授权范围内研究。",
        })

        old_order = [member["id"] for member in self.store.room_snapshot(room_id)["members"]]
        new_order = [added["id"], *[member_id for member_id in old_order if member_id != added["id"]]]
        reordered = self.store.reorder_members(
            room_id,
            new_order,
            expected_member_ids=old_order,
        )
        self.assertEqual(reordered[0]["id"], added["id"])
        archived = self.store.archive_member(
            room_id,
            added["id"],
            expected_version=added["version"],
        )
        snapshot = self.store.room_snapshot(room_id)
        self.assertNotIn(added["id"], [member["id"] for member in snapshot["members"]])
        self.assertIn(added["id"], [member["id"] for member in snapshot["archived_members"]])
        restored = self.store.restore_member(
            room_id,
            added["id"],
            expected_version=archived["version"],
        )
        self.assertFalse(restored["archived"])
        self.assertIsNotNone(self.store.get_member(room_id, added["id"]))

    def test_identity_change_during_round_applies_to_members_next_turn(self) -> None:
        snapshot = self.store.room_snapshot("room_plan")
        # The stage frontier keeps decision synthesis closed while flexible
        # evidence/counter roles speak. The counter role uniquely closes the
        # current flexible-stage and counterargument gaps, so edit that member.
        second_member = snapshot["members"][2]
        provider = EditingProvider(self.store, "room_plan", second_member)
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(provider))

        events = list(orchestrator.run_round("room_plan", "边讨论边调整身份"))

        self.assertIn("讨论中刚刚调整的新身份", provider.calls[1]["instructions"])
        second_message = [event for event in events if event["type"] == "message"][1]["message"]
        self.assertEqual(second_message["identity"], "讨论中刚刚调整的新身份")
        self.assertEqual(second_message["member_version"], 2)

    def test_identity_provider_and_model_hot_edit_route_the_members_next_turn(self) -> None:
        room = self.store.room_snapshot("room_plan")["room"]
        self.store.update_room("room_plan", {
            "expected_settings_version": room["settings_version"],
            "discussion_mode": "sequential",
        })
        first, second = self.store.enabled_members("room_plan")[:2]
        first = self.store.update_member("room_plan", first["id"], {
            "provider": "deepseek",
            "model": "deepseek-before-edit",
        })
        second = self.store.update_member("room_plan", second["id"], {
            "provider": "deepseek",
            "model": "deepseek-before-hot-edit",
        })
        revised_second = {}

        def hot_edit_second_member() -> None:
            revised_second.update(self.store.update_member("room_plan", second["id"], {
                "identity": "轮中切换后的反证审查员",
                "responsibilities": "从下一次发言起使用新 Provider 和模型核对反证。",
                "provider": "doubao",
                "model": "doubao-after-hot-edit",
            }))

        deepseek = RouteRecordingProvider("deepseek", hot_edit_second_member)
        doubao = RouteRecordingProvider("doubao")
        orchestrator = DiscussionOrchestrator(
            self.store,
            MappingFakeRegistry({"deepseek": deepseek, "doubao": doubao}),
        )

        events = list(orchestrator.run_round(
            "room_plan",
            "验证下一次发言立即采用新身份与模型路由",
            [first["id"], second["id"]],
        ))

        messages = [event["message"] for event in events if event["type"] == "message"]
        self.assertEqual(len(deepseek.calls), 1)
        self.assertEqual(deepseek.calls[0]["model"], "deepseek-before-edit")
        self.assertEqual(len(doubao.calls), 1)
        self.assertEqual(doubao.calls[0]["model"], "doubao-after-hot-edit")
        self.assertIn("轮中切换后的反证审查员", doubao.calls[0]["instructions"])
        self.assertEqual([message["provider"] for message in messages], ["deepseek", "doubao"])
        self.assertEqual(messages[1]["model"], "doubao-after-hot-edit")
        self.assertEqual(messages[1]["identity"], "轮中切换后的反证审查员")
        self.assertEqual(messages[1]["member_version"], revised_second["version"])

    def test_formal_ledger_keeps_confirmed_route_across_hot_edit_and_resume(self) -> None:
        room = self.store.room_snapshot("room_plan")["room"]
        self.store.update_room("room_plan", {
            "expected_settings_version": room["settings_version"],
            "discussion_mode": "sequential",
        })
        first, second = self.store.enabled_members("room_plan")[:2]
        first = self.store.update_member("room_plan", first["id"], {
            "provider": "deepseek",
            "model": "approved-first-model",
        })
        second = self.store.update_member("room_plan", second["id"], {
            "provider": "deepseek",
            "model": "approved-second-model",
        })
        approved_routes = {
            "version": "provider_member_routes_v2",
            "members": sorted(
                [
                    {
                        "member_id": str(member["id"]),
                        "approved_member_version": int(member["version"]),
                        "provider": "deepseek",
                        "model": str(member["model"]),
                        "turn_output_mode": "prompt_json",
                        "turn_envelope_version": TURN_ENVELOPE_VERSION,
                        "turn_envelope_schema_sha256": (
                            TURN_ENVELOPE_SCHEMA_SHA256
                        ),
                    }
                    for member in (first, second)
                ],
                key=lambda item: item["member_id"],
            ),
        }
        revised_second: dict[str, object] = {}

        def hot_edit_second_member() -> None:
            revised_second.update(self.store.update_member(
                "room_plan",
                second["id"],
                {
                    "identity": "本轮使用新身份但沿用已确认模型",
                    "responsibilities": "新职责立即用于下一次发言。",
                    "provider": "doubao",
                    "model": "next-round-only-model",
                },
            ))

        deepseek = RouteRecordingProvider("deepseek", hot_edit_second_member)
        doubao = RouteRecordingProvider("doubao")
        registry = MappingFakeRegistry({"deepseek": deepseek, "doubao": doubao})
        ledger = ProviderCallLedger.create(
            self.store,
            "room_plan",
            scope="round",
            client_request_id="sealed-route-resume",
            plan_hash="7" * 64,
            max_calls=2,
            skip_provider_ids=[],
            member_routes=approved_routes,
        )
        orchestrator = DiscussionOrchestrator(self.store, registry)
        stream = orchestrator.run_round(
            "room_plan",
            "验证正式轮路由封印和恢复",
            [first["id"], second["id"]],
            provider_call_ledger=ledger,
        )
        first_message = next(
            event for event in stream if event["type"] == "message"
        )
        round_id = str(first_message["message"]["round_id"])
        stream.close()
        self.assertEqual(self.store.get_round("room_plan", round_id)["status"], "PAUSED")

        resumed_events = list(DiscussionOrchestrator(
            self.store,
            registry,
        ).run_round(
            "room_plan",
            "",
            resume_round_id=round_id,
        ))

        messages = [
            event["message"]
            for event in resumed_events
            if event["type"] == "message"
        ]
        self.assertEqual(len(deepseek.calls), 2)
        self.assertEqual(deepseek.calls[1]["model"], "approved-second-model")
        self.assertIn("本轮使用新身份但沿用已确认模型", deepseek.calls[1]["instructions"])
        self.assertEqual(doubao.calls, [])
        self.assertEqual(messages[0]["provider"], "deepseek")
        self.assertEqual(messages[0]["model"], "approved-second-model")
        self.assertEqual(messages[0]["member_version"], revised_second["version"])
        self.assertEqual(
            [attempt["model"] for attempt in ledger.attempts()],
            ["approved-first-model", "approved-second-model"],
        )
        self.assertEqual(resumed_events[-1]["type"], "round_completed")

    def test_dynamic_director_can_choose_the_next_speaker(self) -> None:
        self.make_flexible_candidates_semantically_tied()
        members = self.store.room_snapshot("room_plan")["members"]
        selected = members[2]
        provider = DirectedProvider(selected["id"])
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(provider))

        events = list(orchestrator.run_round("room_plan", "让主持人动态选择下一位"))
        messages = [event for event in events if event["type"] == "message"]
        decisions = [event for event in events if event["type"] == "director_decision"]

        self.assertEqual(messages[0]["message"]["sender_id"], members[0]["id"])
        self.assertEqual(messages[1]["message"]["sender_id"], selected["id"])
        self.assertEqual(decisions[1]["source"], "ai")
        self.assertEqual(
            sum(
                1
                for event in messages
                if event["message"]["sender_id"] == selected["id"]
            ),
            1,
        )
        self.assertIn("证据缺口", decisions[1]["reason"])
        self.assertGreater(provider.director_calls, 0)

    def test_explicit_moderator_drives_dynamic_routing_and_is_frozen(self) -> None:
        self.make_flexible_candidates_semantically_tied()
        members = self.store.enabled_members("room_plan")
        opening_member = members[0]
        for member in members[:-1]:
            self.store.update_member(
                "room_plan",
                member["id"],
                {**member, "provider": "openai", "model": "speaker-model"},
            )
        moderator = self.store.update_member(
            "room_plan",
            members[-1]["id"],
            {
                **members[-1],
                "provider": "deepseek",
                "model": "moderator-model",
                "identity": "争议主持人",
                "responsibilities": "优先点名能回应反证的成员。",
                "boundaries": "不得跳过用户确认。",
            },
        )
        room = self.store.room_snapshot("room_plan")["room"]
        self.store.update_room("room_plan", {
            "expected_settings_version": room["settings_version"],
            "moderator_member_id": moderator["id"],
        })
        speaker_provider = DirectedProvider(members[1]["id"])
        moderator_provider = DirectedProvider(members[1]["id"])
        moderator_provider.provider_id = "deepseek"
        orchestrator = DiscussionOrchestrator(
            self.store,
            ProviderRegistry({
                "openai": speaker_provider,
                "deepseek": moderator_provider,
            }),
        )

        events = list(orchestrator.run_round("room_plan", "验证显式主持调度"))

        messages = [event for event in events if event["type"] == "message"]
        self.assertEqual(messages[0]["member"]["id"], opening_member["id"])
        self.assertGreater(moderator_provider.director_calls, 0)
        self.assertEqual(speaker_provider.director_calls, 0)
        self.assertIn("争议主持人", moderator_provider.director_inputs[0])
        self.assertIn("不得跳过用户确认", moderator_provider.director_inputs[0])
        round_id = events[-1]["round_id"]
        checkpoint = self.store.get_round_checkpoint("room_plan", round_id)
        self.assertEqual(checkpoint["state"]["version"], 9)
        self.assertEqual(
            checkpoint["state"]["turn_envelope_version"],
            TURN_ENVELOPE_VERSION,
        )
        self.assertEqual(checkpoint["state"]["moderator_member_id"], moderator["id"])

    def test_paused_round_keeps_frozen_moderator_after_room_change(self) -> None:
        members = self.store.enabled_members("room_plan")
        room = self.store.room_snapshot("room_plan")["room"]
        room = self.store.update_room("room_plan", {
            "expected_settings_version": room["settings_version"],
            "moderator_member_id": members[1]["id"],
        })
        orchestrator = DiscussionOrchestrator(
            self.store,
            FakeRegistry(FakeProvider()),
        )
        stream = orchestrator.run_round("room_plan", "冻结主持后暂停")
        started = next(stream)
        stream.close()
        self.store.update_room("room_plan", {
            "expected_settings_version": room["settings_version"],
            "moderator_member_id": members[2]["id"],
        })

        list(orchestrator.run_round(
            "room_plan",
            "恢复时不得换主持",
            resume_round_id=started["round"]["id"],
        ))

        checkpoint = self.store.get_round_checkpoint(
            "room_plan",
            started["round"]["id"],
        )
        self.assertEqual(
            checkpoint["state"]["moderator_member_id"],
            members[1]["id"],
        )

    def test_director_decisions_persist_across_refresh_with_full_sse_record(self) -> None:
        orchestrator = DiscussionOrchestrator(
            self.store,
            FakeRegistry(FakeProvider()),
        )

        events = list(orchestrator.run_round("room_plan", "保留完整主持调度审计"))
        round_id = next(
            event["round"]["id"]
            for event in events
            if event["type"] == "round_started"
        )
        decision_events = [
            event for event in events if event["type"] == "director_decision"
        ]
        persisted = self.store.list_director_decisions(
            "room_plan",
            round_id=round_id,
        )
        refreshed = StudioStore(self.store.path).room_snapshot("room_plan")
        refreshed_round = [
            item
            for item in refreshed["director_decisions"]
            if item["round_id"] == round_id
        ]

        self.assertGreaterEqual(len(decision_events), 2)
        self.assertEqual(
            [item["sequence_no"] for item in persisted],
            list(range(1, len(persisted) + 1)),
        )
        self.assertEqual(
            [event["decision"] for event in decision_events],
            persisted,
        )
        self.assertEqual(refreshed_round, persisted)
        for event in decision_events:
            self.assertEqual(event["action"], event["decision"]["action"])
            self.assertEqual(event["reason"], event["decision"]["reason"])
            self.assertEqual(event["source"], event["decision"]["source"])
            self.assertEqual(event["stage"], event["decision"]["stage"])
            moderator_context = event["decision"]["moderator_context"]
            self.assertEqual(
                moderator_context["version"],
                "director_moderator_context_v1",
            )
            self.assertTrue(moderator_context["member_id"])
            self.assertTrue(moderator_context["member_name"])
            self.assertGreaterEqual(moderator_context["member_version"], 1)
            self.assertTrue(moderator_context["provider"])
        finish = decision_events[-1]
        self.assertEqual(finish["action"], "finish")
        self.assertEqual(finish["decision"]["member_id"], "")
        self.assertEqual(finish["decision"]["member_name"], "")
        self.assertEqual(finish["decision"]["stage"], "follow_up")
        self.assertIn("送交用户复核", finish["reason"])

    def test_director_decision_safely_cleans_focus_and_bounded_text(self) -> None:
        round_row = self.store.create_round("room_project", "测试主持审计清洗")
        member = self.store.enabled_members("room_project")[0]
        focus = {
            "code": " PROJECT_GAP\n" + "C" * 100,
            "title": "T" * 300,
            "detail": "D" * 1200,
            "target_capabilities": [
                " Evidence_Review ",
                "evidence_review",
                *[f"CAP_{index}" for index in range(20)],
            ],
            "target_stances": [
                " Data_Guardian ",
                "data_guardian",
                *[f"STANCE_{index}" for index in range(20)],
            ],
            "repair_scope": "next_round_only",
            "prompt": "不得持久化的隐藏提示",
            "upstream_error": "不得持久化的上游错误体",
            "nested": {"secret": "不得持久化"},
        }

        decision = self.store.add_director_decision(
            "room_project",
            round_row["id"],
            action="SPEAK",
            member_id=member["id"],
            member_name=member["name"] + "\x00" + "N" * 200,
            reason="理由\n" + "R" * 1200,
            source="safe_fallback_" + "S" * 80,
            stage="analysis_" + "G" * 80,
            workspace_focus=focus,
            moderator_context={
                "version": "director_moderator_context_v1",
                "decision_authority": "moderator_model",
                "model_used": True,
                "discussion_mode": "dynamic",
                "member_id": member["id"],
                "member_name": member["name"],
                "identity": "主持身份\n" + "I" * 600,
                "member_version": member["version"],
                "provider": "openai",
                "model": "fake-model\n" + "M" * 200,
                "secret": "不得持久化",
            },
        )
        serialized = json.dumps(decision, ensure_ascii=False)

        self.assertEqual(decision["action"], "speak")
        self.assertLessEqual(len(decision["member_name"]), 160)
        self.assertLessEqual(len(decision["reason"]), 1000)
        self.assertLessEqual(len(decision["source"]), 40)
        self.assertLessEqual(len(decision["stage"]), 40)
        self.assertEqual(
            set(decision["workspace_focus"]),
            {
                "code",
                "title",
                "detail",
                "target_capabilities",
                "target_stances",
                "repair_scope",
            },
        )
        self.assertEqual(
            set(decision["moderator_context"]),
            {
                "version",
                "decision_authority",
                "model_used",
                "discussion_mode",
                "member_id",
                "member_name",
                "identity",
                "member_version",
                "provider",
                "model",
            },
        )
        self.assertNotIn("secret", serialized)
        self.assertLessEqual(len(decision["moderator_context"]["identity"]), 500)
        self.assertLessEqual(len(decision["moderator_context"]["model"]), 160)
        self.assertLessEqual(len(decision["workspace_focus"]["code"]), 80)
        self.assertLessEqual(len(decision["workspace_focus"]["title"]), 240)
        self.assertLessEqual(len(decision["workspace_focus"]["detail"]), 1000)
        self.assertEqual(
            decision["workspace_focus"]["target_capabilities"][0],
            "evidence_review",
        )
        self.assertEqual(
            len(decision["workspace_focus"]["target_capabilities"]),
            12,
        )
        self.assertEqual(
            decision["workspace_focus"]["target_stances"][0],
            "data_guardian",
        )
        self.assertEqual(
            len(decision["workspace_focus"]["target_stances"]),
            12,
        )
        self.assertEqual(
            decision["workspace_focus"]["repair_scope"],
            "next_round_only",
        )
        roundtrip = self.store.list_director_decisions(
            "room_project",
            round_id=round_row["id"],
        )[0]
        self.assertEqual(roundtrip, decision)
        self.assertTrue(str(decision["decision_sha256"] or ""))
        trace = self.store.round_execution_trace(
            "room_project",
            round_row["id"],
        )
        self.assertNotIn(
            "DIRECTOR_DECISION_SEAL_MISMATCH",
            {
                str(issue.get("code") or "")
                for issue in trace["integrity"].get("issues") or []
                if isinstance(issue, dict)
            },
        )
        self.assertNotIn("prompt", serialized)
        self.assertNotIn("upstream_error", serialized)
        self.assertNotIn("secret", serialized)
        with self.assertRaisesRegex(ValueError, "speak 或 finish"):
            self.store.add_director_decision(
                "room_project",
                round_row["id"],
                action="execute",
            )

    def test_director_decision_sequence_continues_after_resume(self) -> None:
        orchestrator = DiscussionOrchestrator(
            self.store,
            FakeRegistry(FakeProvider()),
        )
        stream = orchestrator.run_round("room_plan", "暂停后继续主持审计序号")
        first_message = None
        for event in stream:
            if event["type"] == "message":
                first_message = event["message"]
                break
        self.assertIsNotNone(first_message)
        stream.close()
        paused = self.store.room_snapshot("room_plan")
        round_id = paused["latest_round"]["id"]
        before_resume = self.store.list_director_decisions(
            "room_plan",
            round_id=round_id,
        )

        resumed = list(orchestrator.run_round(
            "room_plan",
            "不得覆盖原目标",
            resume_round_id=round_id,
        ))
        resumed_decisions = [
            event for event in resumed if event["type"] == "director_decision"
        ]
        after_resume = self.store.list_director_decisions(
            "room_plan",
            round_id=round_id,
        )

        self.assertEqual(len(before_resume), 1)
        self.assertTrue(resumed_decisions)
        self.assertEqual(
            resumed_decisions[0]["decision"]["sequence_no"],
            before_resume[-1]["sequence_no"] + 1,
        )
        self.assertEqual(
            [item["sequence_no"] for item in after_resume],
            list(range(1, len(after_resume) + 1)),
        )
        self.assertEqual(len({item["sequence_no"] for item in after_resume}), len(after_resume))

    def test_room_snapshot_limits_director_audit_to_most_recent_200(self) -> None:
        round_row = self.store.create_round("room_plan", "测试审计窗口上限")
        member = self.store.enabled_members("room_plan")[0]
        for index in range(205):
            self.store.add_director_decision(
                "room_plan",
                round_row["id"],
                action="speak",
                member_id=member["id"],
                member_name=member["name"],
                reason=f"调度 {index + 1}",
                source="test",
                stage="facilitate",
            )

        snapshot_rows = [
            item
            for item in self.store.room_snapshot("room_plan")["director_decisions"]
            if item["round_id"] == round_row["id"]
        ]

        self.assertEqual(len(snapshot_rows), 200)
        self.assertEqual(snapshot_rows[0]["sequence_no"], 6)
        self.assertEqual(snapshot_rows[-1]["sequence_no"], 205)

    def test_project_research_pack_guides_director_and_speakers(self) -> None:
        self.make_flexible_candidates_semantically_tied("room_project")
        members = self.store.room_snapshot("room_project")["members"]
        selected = members[1]
        provider = DirectedProvider(selected["id"])
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(provider))

        events = list(orchestrator.run_round("room_project", "评估一个新项目是否值得继续"))

        self.assertTrue(any(event["type"] == "round_completed" for event in events))
        self.assertGreater(provider.director_calls, 0)
        self.assertIn("需求证据", provider.director_instructions[0])
        self.assertIn("PROJECT_WORKSPACE_MISSING", provider.director_inputs[0])
        self.assertTrue(provider.calls)
        self.assertIn("结构化项目研究协议", provider.calls[0]["instructions"])
        self.assertIn("可逆性", provider.calls[0]["instructions"])
        self.assertIn("项目研究工作区缺口快照", provider.calls[0]["input_text"])
        self.assertNotIn("Futu 只读行情", provider.calls[0]["instructions"])

    def test_project_workspace_blocking_risk_prioritizes_critical_reviewer_fallback(self) -> None:
        self.store.create_artifact(
            "room_project",
            title="项目风险工作区",
            content={
                "summary": "需求已经明确，但仍有阻断性资源风险。",
                "summary_evidence": [],
                "requirements": [{
                    "id": "req_scope",
                    "text": "先验证核心流程。",
                    "status": "confirmed",
                    "acceptance_criteria": "五名用户完成核心流程。",
                    "evidence": [],
                }],
                "risks": [{
                    "id": "risk_capacity",
                    "text": "关键开发资源尚未落实。",
                    "status": "monitoring",
                    "blocking": True,
                    "trigger": "排期超过十个开发日。",
                    "mitigation": "缩减为单一核心流程。",
                    "evidence": [],
                }],
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
                "decision": {
                    "status": "candidate",
                    "options": [
                        {
                            "id": "option_small",
                            "title": "小范围验证",
                            "value": "验证核心需求",
                            "cost": "两名开发",
                            "timeline": "两周",
                            "dependencies": ["目标用户"],
                            "reversibility": "high",
                            "evidence": [],
                        },
                        {
                            "id": "option_full",
                            "title": "完整交付",
                            "value": "覆盖完整范围",
                            "cost": "四名开发",
                            "timeline": "六周",
                            "dependencies": ["新增资源"],
                            "reversibility": "low",
                            "evidence": [],
                        },
                    ],
                    "preferred_option_id": "option_small",
                    "rationale": "优先选择可逆方案。",
                    "evidence": [],
                },
            },
        )
        members = self.store.room_snapshot("room_project")["members"]
        critical_reviewer = next(
            member for member in members if "critical_review" in member.get("capabilities", [])
        )
        provider = FakeProvider()
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(provider))

        events = list(orchestrator.run_round("room_project", "优先处理阻断性资源风险"))
        messages = [event for event in events if event["type"] == "message"]
        decisions = [
            event for event in events
            if event["type"] == "director_decision" and event["action"] == "speak"
        ]
        checkpoint = self.store.get_round_checkpoint(
            "room_project",
            next(event["round"]["id"] for event in events if event["type"] == "round_started"),
        )

        self.assertEqual(messages[1]["message"]["sender_id"], critical_reviewer["id"])
        self.assertEqual(decisions[1]["workspace_focus"]["code"], "PROJECT_BLOCKING_RISK_OPEN")
        self.assertIn("阻断性风险", decisions[1]["reason"])
        self.assertEqual(checkpoint["state"]["version"], 9)
        self.assertEqual(
            checkpoint["state"]["turn_envelope_schema_sha256"],
            TURN_ENVELOPE_SCHEMA_SHA256,
        )
        self.assertIn(
            checkpoint["state"]["moderator_member_id"],
            checkpoint["state"]["member_ids"],
        )
        self.assertTrue(checkpoint["state"]["project_workspace"]["frozen"])
        self.assertEqual(
            checkpoint["state"]["project_workspace"]["focus"]["code"],
            "PROJECT_BLOCKING_RISK_OPEN",
        )

    def test_paused_project_round_does_not_read_a_newer_workspace_on_resume(self) -> None:
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(FakeProvider()))
        stream = orchestrator.run_round("room_project", "冻结当前项目缺口")
        started = next(stream)
        self.assertEqual(started["type"], "round_started")
        stream.close()
        round_id = started["round"]["id"]
        frozen_before = self.store.get_round_checkpoint("room_project", round_id)["state"]["project_workspace"]
        self.assertEqual(frozen_before["focus"]["code"], "PROJECT_WORKSPACE_MISSING")

        self.store.create_artifact(
            "room_project",
            title="暂停后新增的完整工作区",
            content={
                "summary": "当前产物结构已补齐。",
                "summary_evidence": [],
                "requirements": [{
                    "id": "req_ready", "text": "验证核心流程。", "status": "confirmed",
                    "acceptance_criteria": "五名用户完成三次任务。", "evidence": [],
                }],
                "risks": [{
                    "id": "risk_ready", "text": "资源风险。", "status": "mitigated",
                    "blocking": False, "trigger": "排期超限。", "mitigation": "缩减范围。", "evidence": [],
                }],
                "conclusions": [], "disagreements": [], "unknowns": [], "actions": [],
                "decision": {
                    "status": "candidate",
                    "options": [
                        {"id": "a", "title": "小范围", "description": "先验证。", "value": "验证", "cost": "低", "timeline": "两周", "dependencies": ["用户"], "reversibility": "high"},
                        {"id": "b", "title": "完整范围", "description": "完整交付。", "value": "覆盖", "cost": "高", "timeline": "六周", "dependencies": ["资源"], "reversibility": "low"},
                    ],
                    "preferred_option_id": "a",
                    "rationale": "选择更可逆的路径。",
                },
            },
        )
        current_workspace = orchestrator.convergence.project_workspace_snapshot("room_project")
        self.assertTrue(current_workspace["ready"])

        resumed = list(orchestrator.run_round("room_project", "", resume_round_id=round_id))
        first_decision = next(event for event in resumed if event["type"] == "director_decision")
        frozen_after = self.store.get_round_checkpoint("room_project", round_id)["state"]["project_workspace"]

        self.assertEqual(first_decision["workspace_focus"]["code"], "PROJECT_WORKSPACE_MISSING")
        self.assertEqual(frozen_after, frozen_before)

    def test_same_room_rejects_concurrent_round_and_requires_resume_after_close(self) -> None:
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(FakeProvider()))
        first_stream = orchestrator.run_round("room_plan", "第一轮仍在进行")
        started = next(first_stream)
        self.assertEqual(started["type"], "round_started")

        competing = list(orchestrator.run_round("room_plan", "不应并发写入"))

        self.assertEqual(competing[0]["type"], "error")
        self.assertEqual(competing[0]["code"], "ROUND_ALREADY_RUNNING")
        first_stream.close()
        after_close = list(orchestrator.run_round("room_plan", "关闭后不应覆盖暂停轮次"))
        self.assertEqual(after_close[0]["type"], "error")
        self.assertEqual(after_close[0]["code"], "PAUSED_ROUND_PENDING")
        self.assertEqual(after_close[0]["round_id"], started["round"]["id"])

        resumed = list(
            orchestrator.run_round(
                "room_plan",
                "",
                resume_round_id=started["round"]["id"],
            )
        )
        self.assertEqual(resumed[0]["type"], "round_resumed")

    def test_storage_room_uses_editable_company_stage_gates(self) -> None:
        self._use_text_only_storage_fixture()
        provider = FakeProvider()
        orchestrator = DiscussionOrchestrator(
            self.store,
            FakeRegistry(provider),
            ReadyMarketService(),
        )

        events = list(orchestrator.run_round("room_storage", "形成一份经过风控的模拟观察方案"))
        decisions = [event for event in events if event["type"] == "director_decision" and event["action"] == "speak"]
        stages = [event["stage"] for event in decisions]
        ranks = {"facilitate": 0, "analysis": 1, "debate": 2, "plan": 3, "risk": 4, "decision": 5}

        self.assertEqual(stages[0], "facilitate")
        self.assertTrue(set(ranks).issubset(stages))
        self.assertIn("plan", stages)
        self.assertIn("risk", stages)
        self.assertIn("decision", stages)
        self.assertEqual(
            len({event["member"]["id"] for event in decisions}),
            len(self.store.enabled_members("room_storage")),
        )

    def test_confirmed_reflection_is_shared_as_auditable_history_case(self) -> None:
        proposal = self.store.create_observation("room_storage", {
            "symbol": "US.MU",
            "direction": "UP",
            "horizon_days": 1,
            "threshold_pct": 1,
            "thesis": "验证反思上下文。",
            "counter_case": "若下跌则失效。",
            "evidence": {},
        })
        self.store.confirm_observation(
            "room_storage",
            proposal["id"],
            {"price": 100, "time": "2026-07-01 16:00:00", "snapshot_id": "reflection-source"},
        )
        self.store.resolve_observation(
            "room_storage",
            proposal["id"],
            outcome_price=103,
            outcome_time="2026-07-02 16:00:00",
            return_pct=3,
            measurement_method="qfq_close_to_close_v2",
            scoring_baseline_price=100,
            scoring_baseline_time="2026-07-01 16:00:00",
            hit=True,
        )
        draft = self.store.get_reflection("room_storage", proposal["id"])
        updated = self.store.update_reflection("room_storage", proposal["id"], {
            "expected_version": draft["version"],
            "lesson": "confirmed-history-case-marker",
            "caveat": "单样本不构成规律。",
            "next_test": "用行业基准再次验证。",
        })
        self.store.confirm_reflection(
            "room_storage",
            proposal["id"],
            expected_version=updated["version"],
        )
        provider = FakeProvider()
        member_ids = [member["id"] for member in self.store.room_snapshot("room_storage")["members"][:2]]

        list(DiscussionOrchestrator(self.store, FakeRegistry(provider), ReadyMarketService()).run_round(
            "room_storage",
            "读取已确认的历史案例",
            member_ids,
        ))

        self.assertTrue(provider.calls)
        self.assertTrue(all("confirmed-history-case-marker" in call["input_text"] for call in provider.calls))
        self.assertTrue(all(proposal["id"] in call["input_text"] for call in provider.calls))

    def test_decision_stage_creates_only_unconfirmed_structured_observation(self) -> None:
        self._use_text_only_storage_fixture()
        provider = ObservationProposalProvider()
        orchestrator = DiscussionOrchestrator(
            self.store,
            FakeRegistry(provider),
            ReadyMarketService(),
        )

        events = list(orchestrator.run_round("room_storage", "形成一个可验证观察"))
        proposal_events = [event for event in events if event["type"] == "observation_proposed"]
        proposal_event_index = next(
            index for index, event in enumerate(events)
            if event["type"] == "observation_proposed"
        )
        proposal_message = next(
            event["message"] for event in reversed(events[:proposal_event_index])
            if event["type"] == "message"
        )
        observations = self.store.list_observations("room_storage")
        checkpoint = self.store.get_round_checkpoint(
            "room_storage",
            events[-1]["round_id"],
        )

        self.assertEqual(len(proposal_events), 1)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["status"], "PROPOSED")
        self.assertFalse(observations[0]["user_confirmed"])
        self.assertIsNone(observations[0]["baseline_price"])
        self.assertNotEqual(observations[0]["created_by"], "user")
        self.assertEqual(observations[0]["evidence"]["message_ids"], [proposal_message["id"]])
        self.assertEqual(observations[0]["round_id"], proposal_message["round_id"])
        self.assertGreater(observations[0]["member_version"], 0)
        self.assertEqual(observations[0]["confidence_source"], "ai")
        self.assertNotIn("observation_proposals", proposal_message["content"])
        self.assertEqual(events[-1]["observation_proposals"], 1)
        self.assertEqual(checkpoint["state"]["frozen_market"], {
            "present": True,
            "ready": True,
            "state": "ready",
            "snapshot_id": "orchestrator-ready-snapshot",
            "captured_at": "2026-07-20T20:00:00Z",
        })

    def test_rejected_observation_does_not_overwrite_frozen_market_checkpoint(self) -> None:
        self._use_text_only_storage_fixture()
        provider = ObservationProposalProvider("US.INVALID")
        events = list(DiscussionOrchestrator(
            self.store,
            FakeRegistry(provider),
            ReadyMarketService(),
        ).run_round("room_storage", "拒绝无效观察但保留冻结行情"))
        rejected = [
            event for event in events
            if event["type"] == "observation_proposal_rejected"
        ]
        checkpoint = self.store.get_round_checkpoint(
            "room_storage",
            events[-1]["round_id"],
        )

        self.assertEqual(len(rejected), 1)
        self.assertEqual(self.store.list_observations("room_storage"), [])
        self.assertEqual(checkpoint["state"]["frozen_market"], {
            "present": True,
            "ready": True,
            "state": "ready",
            "snapshot_id": "orchestrator-ready-snapshot",
            "captured_at": "2026-07-20T20:00:00Z",
        })

    def test_openai_quota_error_is_user_readable(self) -> None:
        raw = '{"error":{"message":"quota details","code":"insufficient_quota"}}'
        self.assertEqual(_http_error_text(raw, 429), "OpenAI 配额不足，请检查该项目的余额或账单设置。")

    def test_closed_stream_marks_round_paused_with_checkpoint(self) -> None:
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(FakeProvider()))
        events = orchestrator.run_round("room_plan", "先暂停这一轮")

        self.assertEqual(next(events)["type"], "round_started")
        events.close()

        snapshot = self.store.room_snapshot("room_plan")
        self.assertEqual(snapshot["latest_round"]["status"], "PAUSED")
        self.assertEqual(snapshot["round_checkpoint"]["completed"], 0)

    def test_legacy_paused_round_is_not_backfilled_after_room_enables_pack(self) -> None:
        provider = FakeProvider()
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(provider))
        stream = orchestrator.run_round("room_plan", "模拟升级前暂停轮")
        started = next(stream)
        stream.close()
        round_id = started["round"]["id"]

        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute(
                """UPDATE rounds
                      SET turn_contract_version=NULL,
                          turn_envelope_version=NULL,
                          turn_envelope_schema_sha256=NULL
                    WHERE id=?""",
                (round_id,),
            )
        checkpoint = self.store.get_round_checkpoint("room_plan", round_id)["state"]
        checkpoint["version"] = 7
        checkpoint["turn_contract_version"] = None
        checkpoint["turn_contract_required"] = False
        checkpoint.pop("candidate_risk_review_version", None)
        checkpoint.pop("candidate_risk_review_required", None)
        checkpoint.pop("turn_envelope_version", None)
        checkpoint.pop("turn_envelope_schema_sha256", None)
        checkpoint.pop("turn_output_modes_by_member", None)
        checkpoint.pop("turn_envelope_required", None)
        self.store.save_round_checkpoint("room_plan", round_id, checkpoint)

        room = self.store.room_snapshot("room_plan")["room"]
        self.store.update_room("room_plan", {
            "expected_settings_version": room["settings_version"],
            "capability_pack_ids": ["structured_turn_contract_v1"],
        })
        resumed = list(orchestrator.run_round(
            "room_plan",
            "",
            resume_round_id=round_id,
        ))
        restored_checkpoint = self.store.get_round_checkpoint("room_plan", round_id)["state"]
        ai_messages = [
            message for message in self.store.round_messages("room_plan", round_id)
            if message["sender_type"] == "ai"
        ]

        self.assertEqual(resumed[-1]["status"], "COMPLETED")
        self.assertIsNone(self.store.get_round("room_plan", round_id)["turn_contract_version"])
        self.assertIsNone(restored_checkpoint["turn_contract_version"])
        self.assertFalse(restored_checkpoint["turn_contract_required"])
        self.assertTrue(ai_messages)
        self.assertTrue(all(message["turn_contract_version"] is None for message in ai_messages))
        self.assertTrue(all(message["turn_contract"] is None for message in ai_messages))
        self.assertTrue(all(
            "<turn_contract>{JSON" not in call["instructions"]
            for call in provider.calls
        ))

    def test_legacy_xml_paused_round_resumes_with_frozen_contract_transport(self) -> None:
        provider = FakeProvider()
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(provider))
        stream = orchestrator.run_round(
            "room_plan",
            "Simulate an XML contract round paused before the envelope upgrade.",
        )
        started = next(stream)
        stream.close()
        round_id = started["round"]["id"]

        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute(
                """UPDATE rounds
                      SET turn_envelope_version=NULL,
                          turn_envelope_schema_sha256=NULL
                    WHERE id=?""",
                (round_id,),
            )
        checkpoint = self.store.get_round_checkpoint("room_plan", round_id)["state"]
        checkpoint["version"] = 8
        checkpoint.pop("turn_envelope_version", None)
        checkpoint.pop("turn_envelope_schema_sha256", None)
        checkpoint.pop("turn_output_modes_by_member", None)
        self.store.save_round_checkpoint("room_plan", round_id, checkpoint)

        resumed = list(orchestrator.run_round(
            "room_plan",
            "",
            resume_round_id=round_id,
        ))
        ai_messages = [
            message for message in self.store.round_messages("room_plan", round_id)
            if message["sender_type"] == "ai"
        ]

        self.assertEqual(resumed[-1]["status"], "COMPLETED")
        self.assertTrue(ai_messages)
        self.assertTrue(all(
            message["turn_contract_version"] == TURN_CONTRACT_VERSION
            and message["turn_contract_qualified"] is True
            for message in ai_messages
        ))
        self.assertTrue(all(
            "<turn_contract>{JSON" in call["instructions"]
            and "turn_envelope_v1" not in call["instructions"]
            for call in provider.calls
        ))

    def test_paused_round_resumes_without_duplicate_user_message(self) -> None:
        provider = FakeProvider()
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(provider))
        stream = orchestrator.run_round("room_plan", "从检查点继续")
        first_message = None
        for event in stream:
            if event["type"] == "message":
                first_message = event["message"]
                break
        self.assertIsNotNone(first_message)
        stream.close()

        paused = self.store.room_snapshot("room_plan")
        round_id = paused["latest_round"]["id"]
        self.assertEqual(paused["latest_round"]["status"], "PAUSED")
        self.assertEqual(paused["round_checkpoint"]["completed"], 1)

        resumed_events = list(orchestrator.run_round(
            "room_plan",
            "这段文字不应覆盖原目标",
            resume_round_id=round_id,
        ))
        resumed_messages = [event for event in resumed_events if event["type"] == "message"]
        final = self.store.room_snapshot("room_plan")
        round_messages = [message for message in final["messages"] if message["round_id"] == round_id]

        self.assertEqual(resumed_events[0]["type"], "round_resumed")
        self.assertTrue(resumed_messages)
        self.assertEqual(final["latest_round"]["id"], round_id)
        self.assertEqual(final["latest_round"]["status"], "COMPLETED")
        self.assertEqual(final["latest_round"]["resume_count"], 1)
        self.assertEqual(sum(1 for message in round_messages if message["sender_type"] == "user"), 1)
        self.assertEqual(final["round_checkpoint"]["completed"], len(self.store.enabled_members("room_plan")))
        ai_round_messages = [
            message for message in round_messages
            if message["sender_type"] == "ai"
        ]
        self.assertGreaterEqual(len(ai_round_messages), 2)
        prior_ai_ids = {ai_round_messages[0]["id"]}
        for current in ai_round_messages[1:]:
            self.assertIn(current["reply_to_message_id"], prior_ai_ids)
            self.assertIn(
                current["reply_to_message_id"],
                {
                    response["id"]
                    for response in current["turn_contract"]["responds_to"]
                },
            )
            prior_ai_ids.add(current["id"])

    def test_malformed_failed_member_checkpoint_fails_before_resume_state_change(self) -> None:
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(FakeProvider()))
        stream = orchestrator.run_round("room_plan", "建立待篡改检查点")
        started = next(stream)
        stream.close()
        round_id = started["round"]["id"]
        checkpoint = self.store.get_round_checkpoint("room_plan", round_id)
        tampered_state = checkpoint["state"]
        tampered_state["failed_member_ids"] = 7
        connection = sqlite3.connect(self.store.path)
        try:
            connection.execute(
                "UPDATE round_checkpoints SET state_json=? WHERE round_id=?",
                (json.dumps(tampered_state, ensure_ascii=False), round_id),
            )
            connection.commit()
        finally:
            connection.close()

        events = list(orchestrator.run_round(
            "room_plan",
            "畸形检查点不得进入运行态",
            resume_round_id=round_id,
        ))
        round_row = self.store.get_round("room_plan", round_id)

        self.assertEqual(events[0]["code"], "ROUND_CHECKPOINT_INVALID")
        self.assertEqual(round_row["status"], "PAUSED")
        self.assertEqual(round_row["resume_count"], 0)

    def test_v3_checkpoint_without_failed_members_remains_backward_compatible(self) -> None:
        self.assertEqual(
            DiscussionOrchestrator.checkpoint_failed_member_ids(
                {"version": 3},
                ["member-a"],
            ),
            set(),
        )
        with self.assertRaises(ValueError):
            DiscussionOrchestrator.checkpoint_failed_member_ids(
                {"version": 4},
                ["member-a"],
            )

    def test_provider_failures_are_persisted_as_system_events(self) -> None:
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(FailingProvider()))
        member_id = self.store.room_snapshot("room_plan")["members"][0]["id"]

        events = list(orchestrator.run_round("room_plan", "验证失败证据链", [member_id]))
        snapshot = self.store.room_snapshot("room_plan")
        system_messages = [message for message in snapshot["messages"] if message["sender_type"] == "system"]
        failures = [event for event in events if event["type"] == "speaker_failed"]

        self.assertEqual(events[-1]["status"], "PARTIAL")
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["error_code"], "http_status")
        self.assertEqual(failures[0]["provider"], snapshot["members"][0]["provider"])
        self.assertEqual(failures[0]["model"], snapshot["members"][0]["model"])
        self.assertEqual(len(system_messages), 1)
        self.assertIn("未完成发言：", system_messages[0]["content"])
        self.assertIn("请求失败", system_messages[0]["content"])
        self.assertNotIn("测试配额不足", system_messages[0]["content"])
        self.assertEqual(system_messages[0]["provider"], snapshot["members"][0]["provider"])
        self.assertEqual(system_messages[0]["model"], snapshot["members"][0]["model"])

    def test_provider_exception_is_classified_without_retry_or_detail_leak(self) -> None:
        provider = ExplodingProvider()
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(provider))
        member_id = self.store.room_snapshot("room_plan")["members"][0]["id"]

        events = list(orchestrator.run_round("room_plan", "验证异常隔离", [member_id]))
        failure = next(event for event in events if event["type"] == "speaker_failed")
        snapshot = self.store.room_snapshot("room_plan")
        system_message = next(
            message for message in snapshot["messages"]
            if message["sender_type"] == "system"
        )

        self.assertEqual(provider.speaker_attempts, 1)
        self.assertEqual(failure["error_code"], "timeout")
        self.assertEqual(failure["provider"], snapshot["members"][0]["provider"])
        self.assertIn("请求超时", failure["error"])
        self.assertNotIn("upstream-secret", json.dumps(failure, ensure_ascii=False))
        self.assertNotIn("upstream-secret", system_message["content"])

    def test_missing_adapter_failure_has_complete_diagnostics(self) -> None:
        member = self.store.room_snapshot("room_plan")["members"][0]
        orchestrator = DiscussionOrchestrator(self.store, ProviderRegistry({}))

        events = list(orchestrator.run_round(
            "room_plan",
            "验证缺少适配器时的诊断字段",
            [member["id"]],
        ))
        failure = next(event for event in events if event["type"] == "speaker_failed")
        checkpoint = self.store.get_round_checkpoint(
            "room_plan",
            events[-1]["round_id"],
        )

        self.assertEqual(failure["error_code"], "provider_error")
        self.assertEqual(failure["provider"], member["provider"])
        self.assertEqual(failure["model"], member["model"])
        self.assertGreaterEqual(failure["elapsed_ms"], 0)
        self.assertIn(member["id"], checkpoint["state"]["failed_member_ids"])

    def test_failed_member_is_not_retried_after_resume_and_adapter_error_is_not_trusted(self) -> None:
        members = self.store.room_snapshot("room_plan")["members"]
        target = members[0]
        provider = UnsafeSelectiveFailureProvider(target["name"])
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(provider))
        stream = orchestrator.run_round("room_plan", "失败成员不得在同轮自动重试")
        failure = None
        for event in stream:
            if event["type"] == "speaker_failed":
                failure = event
                break
        self.assertIsNotNone(failure)
        stream.close()

        paused = self.store.room_snapshot("room_plan")
        round_id = paused["latest_round"]["id"]
        checkpoint = self.store.get_round_checkpoint("room_plan", round_id)
        self.assertEqual(paused["latest_round"]["status"], "PAUSED")
        self.assertIn(target["id"], checkpoint["state"]["failed_member_ids"])
        self.assertIn(target["id"], paused["round_checkpoint"]["failed_member_ids"])
        self.assertEqual(provider.speaker_attempts[target["name"]], 1)

        resumed = list(orchestrator.run_round(
            "room_plan",
            "恢复时也不得重试失败成员",
            resume_round_id=round_id,
        ))
        serialized = json.dumps([failure, *resumed], ensure_ascii=False)
        final_snapshot = self.store.room_snapshot("room_plan")
        round_system_messages = [
            message
            for message in final_snapshot["messages"]
            if message["round_id"] == round_id and message["sender_type"] == "system"
        ]

        self.assertEqual(resumed[0]["type"], "round_resumed")
        self.assertIn(target["id"], resumed[0]["checkpoint"]["failed_member_ids"])
        self.assertEqual(provider.speaker_attempts[target["name"]], 1)
        self.assertGreater(provider.director_calls, 0)
        self.assertFalse(any(
            attempt["status"] == "STARTED"
            for attempt in self.store.list_director_attempts(
                "room_plan",
                round_id=round_id,
            )
        ))
        self.assertTrue(any(
            attempts > 1
            for name, attempts in provider.speaker_attempts.items()
            if name != target["name"]
        ))
        self.assertEqual(failure["provider"], target["provider"])
        self.assertEqual(failure["model"], target["model"])
        self.assertEqual(failure["error_code"], "invalid_response")
        self.assertTrue(failure["error"])
        self.assertNotIn("upstream-", serialized)
        self.assertTrue(round_system_messages)
        self.assertNotIn(
            "upstream-",
            json.dumps(round_system_messages, ensure_ascii=False),
        )

    def test_terminal_round_turn_repairs_stale_checkpoint_without_second_provider_call(self) -> None:
        snapshot = self.store.room_snapshot("room_plan")
        self.store.update_room("room_plan", {
            "expected_updated_at": snapshot["room"]["updated_at"],
            "discussion_mode": "sequential",
        })
        member = self.store.enabled_members("room_plan")[0]
        provider = FakeProvider()
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(provider))
        stream = orchestrator.run_round(
            "room_plan",
            "验证普通轮次消息与检查点的崩溃恢复",
            [member["id"]],
        )
        persisted_message = None
        for event in stream:
            if event["type"] == "message":
                persisted_message = event["message"]
                break
        self.assertIsNotNone(persisted_message)
        stream.close()

        paused = self.store.room_snapshot("room_plan")
        round_id = paused["latest_round"]["id"]
        terminal_turn = self.store.get_round_turn("room_plan", round_id, 1)
        self.assertEqual(terminal_turn["status"], "RESPONDED")
        self.assertEqual(terminal_turn["message_id"], persisted_message["id"])
        self.assertEqual(terminal_turn["checkpoint_state"]["next_order"], 2)
        speak_decision_ids = [
            decision["id"]
            for decision in self.store.list_director_decisions(
                "room_plan",
                round_id=round_id,
            )
            if decision["action"] == "speak"
        ]

        # Simulate the historical crash window: the message and terminal turn
        # landed, but an older checkpoint is the only visible checkpoint.
        stale_state = json.loads(json.dumps(
            terminal_turn["checkpoint_state"],
            ensure_ascii=False,
        ))
        stale_state.update({
            "spoken_counts": {},
            "spoken_stances": [],
            "successful_member_ids": [],
            "failed_member_ids": [],
            "previous_name": "我",
            "completed": 0,
            "failures": 0,
            "next_order": 1,
        })
        self.store.save_round_checkpoint("room_plan", round_id, stale_state)
        calls_before_resume = len(provider.calls)

        resumed = list(orchestrator.run_round(
            "room_plan",
            "恢复不得覆盖原目标或重放模型",
            resume_round_id=round_id,
        ))
        messages = self.store.round_messages("room_plan", round_id)
        ai_messages = [message for message in messages if message["sender_type"] == "ai"]
        repaired_checkpoint = self.store.get_round_checkpoint("room_plan", round_id)
        repaired_turn = self.store.get_round_turn("room_plan", round_id, 1)
        recovered_speak_decision_ids = [
            decision["id"]
            for decision in self.store.list_director_decisions(
                "room_plan",
                round_id=round_id,
            )
            if decision["action"] == "speak"
        ]

        self.assertEqual(resumed[0]["type"], "round_resumed")
        self.assertEqual(len(provider.calls), calls_before_resume)
        self.assertEqual(len(ai_messages), 1)
        self.assertEqual(ai_messages[0]["id"], persisted_message["id"])
        self.assertEqual(repaired_turn["message_id"], persisted_message["id"])
        self.assertEqual(repaired_checkpoint["state"]["next_order"], 2)
        self.assertEqual(repaired_checkpoint["state"]["completed"], 1)
        self.assertEqual(recovered_speak_decision_ids, speak_decision_ids)

    def test_started_round_turn_is_failed_closed_on_resume_without_provider_replay(self) -> None:
        snapshot = self.store.room_snapshot("room_plan")
        self.store.update_room("room_plan", {
            "expected_updated_at": snapshot["room"]["updated_at"],
            "discussion_mode": "sequential",
        })
        member = self.store.enabled_members("room_plan")[0]
        provider = FakeProvider()
        orchestrator = DiscussionOrchestrator(self.store, FakeRegistry(provider))
        stream = orchestrator.run_round(
            "room_plan",
            "在 provider 调用边界暂停",
            [member["id"]],
        )
        persisted_decision = None
        for event in stream:
            if event["type"] == "director_decision" and event["action"] == "speak":
                persisted_decision = event["decision"]
                break
        self.assertIsNotNone(persisted_decision)
        self.assertEqual(provider.calls, [])
        stream.close()

        round_id = self.store.room_snapshot("room_plan")["latest_round"]["id"]
        started_turn = self.store.get_round_turn("room_plan", round_id, 1)
        self.assertEqual(started_turn["status"], "STARTED")

        resumed = list(orchestrator.run_round(
            "room_plan",
            "恢复时不允许重放",
            resume_round_id=round_id,
        ))
        failure = next(event for event in resumed if event["type"] == "speaker_failed")
        terminal_turn = self.store.get_round_turn("room_plan", round_id, 1)
        messages = self.store.round_messages("room_plan", round_id)
        recovery_messages = [
            message for message in messages
            if message["sender_type"] == "system" and "避免重复调用" in message["content"]
        ]

        self.assertEqual(provider.calls, [])
        self.assertEqual(failure["error_code"], "provider_result_unknown")
        self.assertTrue(failure["recovered"])
        self.assertEqual(terminal_turn["status"], "FAILED")
        self.assertEqual(len(recovery_messages), 1)
        self.assertEqual(terminal_turn["message_id"], recovery_messages[0]["id"])
        self.assertEqual(terminal_turn["checkpoint_state"]["next_order"], 2)
        speak_decisions = [
            decision
            for decision in self.store.list_director_decisions(
                "room_plan",
                round_id=round_id,
            )
            if decision["action"] == "speak"
        ]
        self.assertEqual([decision["id"] for decision in speak_decisions], [persisted_decision["id"]])


if __name__ == "__main__":
    unittest.main()
