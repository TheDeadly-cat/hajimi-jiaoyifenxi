from __future__ import annotations

import ast
import inspect
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from backend.artifact_service import ArtifactService
from backend.domain_adapters import (
    DomainAdapterError,
    DomainAdapterRegistry,
    FootballResearchDomainAdapter,
    StorageResearchDomainAdapter,
    UnknownDomainAdapterError,
)
from backend.orchestrator import DiscussionOrchestrator
from backend.providers.base import ProviderResponse
from backend.store import StudioStore


FORBIDDEN_GENERIC_PROMPT_TERMS = (
    "Futu",
    "富途",
    "US.MU",
    "SNDK",
    "WDC",
    "STX",
    "交易研究",
    "投委会",
)


class RecordingProvider:
    provider_id = "deepseek"

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        self.calls.append({
            "instructions": instructions,
            "input_text": input_text,
            "model": model,
        })
        if "隐藏主持调度器" in instructions:
            match = re.search(r"候选成员：(\[.*?\])\n\n共享证据", input_text, re.DOTALL)
            candidates = json.loads(match.group(1)) if match else []
            content = json.dumps({
                "action": "speak" if candidates else "finish",
                "member_id": str((candidates[0] if candidates else {}).get("member_id") or ""),
                "reason": "补齐当前通用协作阶段。",
            }, ensure_ascii=False)
        else:
            content = "基于当前讨论补充一个可验证观点，并保留反方意见。"
        return ProviderResponse(
            ok=True,
            provider=self.provider_id,
            model=model or "fixture-model",
            content=content,
        )


class RecordingRegistry:
    def __init__(self, provider: RecordingProvider) -> None:
        self.provider = provider

    def get(self, provider_id: str) -> RecordingProvider:
        self.provider.provider_id = str(provider_id or "deepseek")
        return self.provider


class FixtureMarketService:
    def __init__(self) -> None:
        self.calls = 0

    def snapshot(self) -> dict[str, Any]:
        self.calls += 1
        return {
            "snapshot_id": "adapter-fixture-snapshot",
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

    @staticmethod
    def prompt_context(snapshot: dict[str, Any]) -> str:
        return f"adapter-context={snapshot['snapshot_id']}"

    @staticmethod
    def timeline_summary(snapshot: dict[str, Any]) -> str:
        return f"adapter-timeline={snapshot['snapshot_id']}"


class FixtureConvergence:
    @staticmethod
    def market_preflight(
        _room_snapshot: dict[str, Any],
        snapshot: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "applicable": True,
            "ready": bool(snapshot),
            "state": "ready" if snapshot else "blocked",
            "blockers": [],
        }


class DomainAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "studio.sqlite3")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_orchestrator_has_no_direct_storage_service_import(self) -> None:
        source_path = Path(inspect.getsourcefile(DiscussionOrchestrator) or "")
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imported_modules = {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        imported_modules.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )

        self.assertFalse(any(
            module.endswith("market.storage_service")
            for module in imported_modules
        ))
        self.assertNotIn("StorageResearchMarketService", source_path.read_text(encoding="utf-8"))

    def test_football_adapter_is_versioned_pure_readonly_and_port_bound(self) -> None:
        adapter = FootballResearchDomainAdapter()
        registry = DomainAdapterRegistry((adapter,))

        self.assertIs(registry.require("football_research", "1.0.0"), adapter)
        self.assertEqual(
            adapter.declared_ports,
            frozenset({"core.football.match_context/v1"}),
        )
        self.assertEqual(adapter.execution_capability, "none")
        self.assertFalse(adapter.live_trading_allowed)
        self.assertFalse(adapter.provides_market_context)

    def test_generic_speaker_director_and_artifact_prompts_have_no_storage_semantics(self) -> None:
        provider = RecordingProvider()
        orchestrator = DiscussionOrchestrator(
            self.store,
            RecordingRegistry(provider),
            market_service=None,
        )
        snapshot = self.store.room_snapshot("room_plan")
        self.assertIsNotNone(snapshot)
        room = snapshot["room"]
        member = snapshot["members"][0]
        # This test inspects the hidden-director prompt, so keep the two
        # flexible candidates semantically tied under the rules-first policy.
        for candidate in snapshot["members"]:
            if candidate.get("workflow_stage") != "flexible":
                continue
            self.store.update_member(
                "room_plan",
                str(candidate["id"]),
                {"capabilities": sorted({
                    *list(candidate.get("capabilities") or []),
                    "critical_review",
                })},
            )

        speaker_prompt = orchestrator._instructions(room, member, "我")
        artifact_prompt = ArtifactService._instructions(room)
        events = list(orchestrator.run_round("room_plan", "比较两个通用方案并保留分歧"))
        director_prompts = [
            call["instructions"]
            for call in provider.calls
            if "隐藏主持调度器" in call["instructions"]
        ]

        self.assertTrue(any(event.get("type") == "round_started" for event in events))
        self.assertTrue(director_prompts)
        combined = "\n".join([speaker_prompt, artifact_prompt, *director_prompts])
        for term in FORBIDDEN_GENERIC_PROMPT_TERMS:
            self.assertNotIn(term, combined)
        self.assertNotIn("round_market_snapshot", artifact_prompt)

    def test_storage_adapter_owns_preflight_prompt_timeline_and_proposal_protocol(self) -> None:
        market = FixtureMarketService()
        adapter = StorageResearchDomainAdapter(market)
        room = {
            "capabilities": [
                "market.storage.readonly",
                "decision.observation_proposals",
            ],
        }
        member = {"workflow_stage": "decision"}
        policy = {"stage_order": ["analysis", "decision"]}

        preflight = adapter.preflight_market(
            {"room": room},
            FixtureConvergence(),
        )
        speaker_rule = adapter.speaker_prompt_rule(
            room,
            member,
            policy,
            direct_mention=False,
        )
        visible, payloads = adapter.extract_speaker_payloads(
            room,
            member,
            policy,
            (
                "可见结论"
                '<observation_proposals>{"observations":[{"symbol":"US.MU"}]}'
                "</observation_proposals>"
            ),
        )
        timeline = adapter.timeline_message(preflight.snapshot)

        self.assertEqual(market.calls, 1)
        self.assertTrue(preflight.gate["ready"])
        self.assertEqual(adapter.prompt_context(preflight.snapshot), "adapter-context=adapter-fixture-snapshot")
        self.assertEqual(timeline["sender_id"], "futu_opend")
        self.assertEqual(timeline["content"], "adapter-timeline=adapter-fixture-snapshot")
        self.assertIn("US.MU|US.SNDK|US.WDC|US.STX", speaker_rule)
        self.assertEqual(adapter.machine_block_names(
            room,
            member,
            policy,
            direct_mention=False,
        ), ("observation_proposals",))
        self.assertEqual(visible, "可见结论")
        self.assertEqual(payloads, [{"symbol": "US.MU"}])

    def test_market_service_constructor_override_remains_backward_compatible(self) -> None:
        market = FixtureMarketService()
        orchestrator = DiscussionOrchestrator(
            self.store,
            RecordingRegistry(RecordingProvider()),
            market_service=market,
        )
        room = self.store.room_snapshot("room_storage")["room"]
        adapter = orchestrator.domain_adapters.market_adapter_for(room)

        self.assertIs(orchestrator.market_service, market)
        self.assertIs(adapter.market_service, market)

    def test_market_adapter_selection_is_capability_driven_not_storage_hardcoded(self) -> None:
        sports_adapter = SimpleNamespace(
            adapter_id="sports_context",
            activation_capabilities=frozenset({"market.sports.readonly"}),
            execution_capability="none",
            live_trading_allowed=False,
            provides_market_context=True,
        )
        sports_adapter.with_market_service = lambda _service: sports_adapter
        sports_adapter.preflight_market = lambda *_args, **_kwargs: None
        sports_adapter.prompt_context = lambda *_args, **_kwargs: ""
        sports_adapter.timeline_message = lambda *_args, **_kwargs: None
        sports_adapter.speaker_prompt_rule = lambda *_args, **_kwargs: ""
        sports_adapter.machine_block_names = lambda *_args, **_kwargs: ()
        sports_adapter.extract_speaker_payloads = lambda *_args, **_kwargs: ("", [])
        sports_adapter.persist_speaker_payloads = lambda *_args, **_kwargs: None
        sports_adapter.artifact_prompt_rule = lambda *_args, **_kwargs: ""
        sports_adapter.artifact_evidence_types = lambda *_args, **_kwargs: ()
        registry = DomainAdapterRegistry((sports_adapter,))

        selected = registry.market_adapter_for({
            "capabilities": ["market.sports.readonly"],
        })

        self.assertIs(selected, sports_adapter)
        self.assertIsNone(registry.market_adapter_for({
            "capabilities": [],
            "domain_adapter_ids": ["sports_context"],
        }))

    def test_registry_rejects_unsafe_missing_and_unknown_adapters(self) -> None:
        unsafe_execution = SimpleNamespace(
            adapter_id="unsafe_execution",
            activation_capabilities=frozenset({"unsafe.capability"}),
            execution_capability="orders",
            live_trading_allowed=False,
        )
        unsafe_live = SimpleNamespace(
            adapter_id="unsafe_live",
            activation_capabilities=frozenset({"unsafe.live"}),
            execution_capability="none",
            live_trading_allowed=True,
        )

        with self.assertRaisesRegex(DomainAdapterError, "不可执行边界"):
            DomainAdapterRegistry((unsafe_execution,))
        with self.assertRaisesRegex(DomainAdapterError, "禁止真实交易边界"):
            DomainAdapterRegistry((unsafe_live,))
        with self.assertRaises(UnknownDomainAdapterError):
            DomainAdapterRegistry().require("not_registered")
        with self.assertRaises(UnknownDomainAdapterError):
            DomainAdapterRegistry().active_for_room({
                "capabilities": ["market.storage.readonly"],
            })
        with self.assertRaises(UnknownDomainAdapterError):
            DomainAdapterRegistry((StorageResearchDomainAdapter(None),)).active_for_room({
                "capabilities": [],
                "domain_adapter_ids": ["not_registered"],
            })

    def test_missing_required_adapter_blocks_round_before_market_or_provider_calls(self) -> None:
        market = FixtureMarketService()
        provider = RecordingProvider()
        orchestrator = DiscussionOrchestrator(
            self.store,
            RecordingRegistry(provider),
            market_service=market,
            domain_adapters=DomainAdapterRegistry(),
        )

        events = list(orchestrator.run_round("room_storage", "缺少领域适配器必须失败关闭"))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["code"], "ROUND_MARKET_PREFLIGHT_FAILED")
        self.assertEqual(
            events[0]["preflight"]["blockers"][0]["code"],
            "DOMAIN_ADAPTER_INVALID",
        )
        self.assertEqual(market.calls, 0)
        self.assertEqual(provider.calls, [])


if __name__ == "__main__":
    unittest.main()
