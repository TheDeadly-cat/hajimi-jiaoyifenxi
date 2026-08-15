from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

from backend.orchestrator import DiscussionOrchestrator
from backend.providers.base import ProviderResponse
from backend.store import (
    ROUND_CHECKPOINT_MAX_BYTES,
    ROUND_EVIDENCE_MANIFEST_VERSION,
    StudioStore,
)
from tests.storage_research_fixture import ready_storage_research_evidence
from tests.turn_contract_fixture import append_valid_turn_contract, wrap_turn_envelope


class RecordingCitationProvider:
    provider_id = "openai"

    def __init__(
        self,
        material_id: str,
        *,
        before_first_response: Callable[[], None] | None = None,
        content_prefix: str = "依据冻结资料。",
    ) -> None:
        self.material_id = material_id
        self.before_first_response = before_first_response
        self.content_prefix = content_prefix
        self.inputs: list[str] = []

    def status(self) -> dict[str, object]:
        return {"id": self.provider_id, "configured": True}

    def generate(
        self,
        *,
        instructions: str,
        input_text: str,
        model: str = "",
    ) -> ProviderResponse:
        self.inputs.append(input_text)
        if len(self.inputs) == 1 and self.before_first_response:
            self.before_first_response()
        content = append_valid_turn_contract(
            f"{self.content_prefix}[资料:{self.material_id}]",
            instructions=instructions,
            input_text=input_text,
        )
        return ProviderResponse(
            ok=True,
            provider=self.provider_id,
            model=model or "fake-model",
            content=content,
        )


class StaticRegistry:
    def __init__(self, provider: RecordingCitationProvider) -> None:
        self.provider = provider

    def get(self, _provider_id: str) -> RecordingCitationProvider:
        self.provider.provider_id = str(_provider_id or "openai")
        return self.provider


class FixedMarketService:
    def __init__(self, *, blob_chars: int = 0, prompt_chars: int = 0) -> None:
        self.calls = 0
        self.blob_chars = blob_chars
        self.prompt_chars = prompt_chars

    def snapshot(self) -> dict[str, Any]:
        self.calls += 1
        payload: dict[str, Any] = {
            "ok": True,
            "state": "ready",
            "source": "futu_opend",
            "snapshot_id": "frozen-storage-snapshot",
            "captured_at": "2026-07-26T20:00:00Z",
            "rows": [
                {
                    "symbol": symbol,
                    "last": 125 + index,
                    "quality": "ready",
                    "age_seconds": 60,
                    "quote_is_live": True,
                    "freshness_basis": "live_20m_window",
                    "market_time": "2026-07-26 15:59:00",
                }
                for index, symbol in enumerate(
                    ("US.MU", "US.SNDK", "US.WDC", "US.STX")
                )
            ],
            "missing_symbols": [],
            "source_errors": [],
            "evidence": ready_storage_research_evidence(
                captured_at="2026-07-26T20:00:00Z",
                technical_as_of="2026-07-26 00:00:00",
            ),
            "execution_capability": "none",
            "live_trading_allowed": False,
        }
        if self.blob_chars:
            payload["test_blob"] = "m" * self.blob_chars
        return payload

    def prompt_context(self, snapshot: dict[str, Any]) -> str:
        if self.prompt_chars:
            return "market-context-" + ("x" * self.prompt_chars)
        return f"snapshot_id={snapshot['snapshot_id']}"

    @staticmethod
    def timeline_summary(snapshot: dict[str, Any]) -> str:
        return f"共享快照 {snapshot['snapshot_id']}"


class SnapshotTurnContractProvider:
    provider_id = "openai"

    def __init__(self, snapshot_id: str) -> None:
        self.snapshot_id = snapshot_id
        self.inputs: list[str] = []
        self.instructions: list[str] = []

    def status(self) -> dict[str, object]:
        return {"id": self.provider_id, "configured": True}

    def generate(
        self,
        *,
        instructions: str,
        input_text: str,
        model: str = "",
    ) -> ProviderResponse:
        self.instructions.append(instructions)
        self.inputs.append(input_text)
        is_analysis = "流程阶段：analysis" in instructions
        prior_ai_line = next((
            line
            for line in input_text.splitlines()
            if line.startswith("本轮此前正式 AI 消息ID：")
        ), "")
        prior_ai_text = prior_ai_line.split("：", 1)[1].strip() if prior_ai_line else ""
        prior_ai_ids = [
            item.strip()
            for item in prior_ai_text.split(",")
            if item.strip() and not item.strip().startswith("无（")
        ]
        payload = {
            "version": "turn_contract_v1",
            "claims": [{
                "id": "market_fact" if is_analysis else "round_scope",
                "kind": "fact" if is_analysis else "unknown",
                "text": (
                    "本轮分析使用唯一冻结市场快照。"
                    if is_analysis
                    else "本轮目标是按冻结证据推进只读研究。"
                ),
                "as_of": "2026-07-26T20:00:00Z" if is_analysis else "",
                "evidence": [{
                    "type": "round_market_snapshot",
                    "id": self.snapshot_id,
                    "role": "support" if is_analysis else "context",
                }],
            }],
            "responds_to": ([{
                "type": "message",
                "id": prior_ai_ids[-1],
                "relation": "supports",
                "reason": "承接暂停前已持久化的正式 AI 发言。",
            }] if prior_ai_ids else []),
            "candidate_updates": [],
            "risks": [],
            "next_actions": [{
                "id": "next_review",
                "text": "继续核验本轮冻结证据。",
                "owner": "下一位成员",
                "state": "open",
                "due": "本轮",
                "evidence": [{
                    "type": "round_market_snapshot",
                    "id": self.snapshot_id,
                    "role": "context",
                }],
            }],
            "confidence": {
                "kind": "model_subjective",
                "value": None,
                "label": "unknown",
                "basis": "仅验证冻结证据引用链。",
            },
        }
        content = (
            wrap_turn_envelope("按本轮冻结快照继续只读研究。", payload)
            if "turn_envelope_v1" in instructions
            else (
                "按本轮冻结快照继续只读研究。\n"
                f"<turn_contract>{json.dumps(payload, ensure_ascii=False)}</turn_contract>"
            )
        )
        return ProviderResponse(
            ok=True,
            provider=self.provider_id,
            model=model or "fake-model",
            content=content,
        )


class ObservationCitationProvider(RecordingCitationProvider):
    def generate(
        self,
        *,
        instructions: str,
        input_text: str,
        model: str = "",
    ) -> ProviderResponse:
        self.inputs.append(input_text)
        if len(self.inputs) == 1 and self.before_first_response:
            self.before_first_response()
        proposal = {
            "observations": [{
                "symbol": "US.MU",
                "direction": "UP",
                "horizon_days": 5,
                "threshold_pct": 2,
                "thesis": "使用本轮冻结证据形成可验证观察。",
                "counter_case": "若需求或价格结构转弱则失效。",
                "model_confidence": 62,
                "methodology_id": "frozen_evidence_case",
                "methodology_version": 1,
                "evidence": {"material_ids": [self.material_id]},
            }],
        }
        is_decision = "流程阶段：decision。" in instructions
        visible = f"候选观察依据本轮材料。[资料:{self.material_id}]"
        if is_decision:
            visible += (
                "\n<observation_proposals>"
                f"{json.dumps(proposal, ensure_ascii=False)}"
                "</observation_proposals>"
            )
        content = append_valid_turn_contract(
            visible,
            instructions=instructions,
            input_text=input_text,
        )
        return ProviderResponse(
            ok=True,
            provider=self.provider_id,
            model=model or "fake-model",
            content=content,
        )


class TailCitationProvider(RecordingCitationProvider):
    def generate(
        self,
        *,
        instructions: str,
        input_text: str,
        model: str = "",
    ) -> ProviderResponse:
        self.inputs.append(input_text)
        content = append_valid_turn_contract(
            ("x" * 30000) + f"[资料:{self.material_id}]",
            instructions=instructions,
            input_text=input_text,
        )
        return ProviderResponse(
            ok=True,
            provider=self.provider_id,
            model=model or "fake-model",
            content=content,
        )


class RoundEvidenceManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "studio.sqlite3")
        self.store.update_room("room_plan", {
            "expected_updated_at": self.store.room_snapshot("room_plan")["room"]["updated_at"],
            "discussion_mode": "sequential",
        })
        self.store.update_room("room_storage", {
            "expected_updated_at": self.store.room_snapshot("room_storage")["room"]["updated_at"],
            "discussion_mode": "sequential",
        })

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def add_material(
        self,
        room_id: str = "room_plan",
        *,
        title: str = "冻结证据 v1",
        content: str = "冻结正文-v1",
        source_url: str = "https://example.com/v1",
    ) -> dict[str, Any]:
        material = self.store.add_material(room_id, {
            "title": title,
            "kind": "url",
            "source_url": source_url,
            "content": content,
        })
        assert material is not None
        return material

    def test_mid_round_update_keeps_v1_and_next_round_uses_v2(self) -> None:
        material = self.add_material()

        def update_to_v2() -> None:
            self.store.update_material("room_plan", material["id"], {
                "expected_version": material["version"],
                "title": "更新后的证据 v2",
                "kind": "url",
                "source_url": "https://example.com/v2",
                "content": "新正文-v2",
            })

        provider = RecordingCitationProvider(
            material["id"],
            before_first_response=update_to_v2,
        )
        members = [row["id"] for row in self.store.enabled_members("room_plan")[:2]]
        events = list(DiscussionOrchestrator(
            self.store,
            StaticRegistry(provider),
            market_service=None,
        ).run_round("room_plan", "验证轮中资料升级", members))
        messages = [event["message"] for event in events if event["type"] == "message"]

        self.assertEqual(len(messages), 2)
        self.assertTrue(all("冻结正文-v1" in item for item in provider.inputs))
        self.assertTrue(all("新正文-v2" not in item for item in provider.inputs))
        self.assertTrue(all(message["citations"][0]["version"] == 1 for message in messages))

        snapshot = self.store.room_snapshot("room_plan")
        historical = next(message for message in snapshot["messages"] if message["id"] == messages[0]["id"])
        self.assertEqual(historical["citations"][0]["title"], "冻结证据 v1")
        self.assertEqual(historical["citations"][0]["source_url"], "https://example.com/v1")
        self.assertEqual(snapshot["materials"][0]["version"], 2)

        next_provider = RecordingCitationProvider(material["id"])
        next_events = list(DiscussionOrchestrator(
            self.store,
            StaticRegistry(next_provider),
            market_service=None,
        ).run_round("room_plan", "下一轮使用新版本", [members[0]]))
        next_message = next(event["message"] for event in next_events if event["type"] == "message")
        self.assertIn("新正文-v2", next_provider.inputs[0])
        self.assertIn("版本=v2", next_provider.inputs[0])
        self.assertEqual(next_message["citations"][0]["version"], 2)

    def test_mid_round_disable_keeps_frozen_v1_citation(self) -> None:
        material = self.add_material()

        def disable_material() -> None:
            self.store.update_material("room_plan", material["id"], {
                "expected_version": material["version"],
                "active": False,
            })

        provider = RecordingCitationProvider(
            material["id"],
            before_first_response=disable_material,
        )
        members = [row["id"] for row in self.store.enabled_members("room_plan")[:2]]
        events = list(DiscussionOrchestrator(
            self.store,
            StaticRegistry(provider),
            market_service=None,
        ).run_round("room_plan", "验证轮中停用", members))
        messages = [event["message"] for event in events if event["type"] == "message"]

        self.assertEqual(len(messages), 2)
        self.assertTrue(all(message["citations"][0]["version"] == 1 for message in messages))
        self.assertTrue(all("冻结正文-v1" in item for item in provider.inputs))
        self.assertFalse(self.store.get_material("room_plan", material["id"])["active"])

    def test_resume_after_edit_uses_original_manifest(self) -> None:
        material = self.add_material()
        provider = RecordingCitationProvider(material["id"])
        members = [row["id"] for row in self.store.enabled_members("room_plan")[:2]]
        orchestrator = DiscussionOrchestrator(
            self.store,
            StaticRegistry(provider),
            market_service=None,
        )
        stream = orchestrator.run_round("room_plan", "暂停后恢复", members)
        first_message = None
        for event in stream:
            if event["type"] == "message":
                first_message = event["message"]
                break
        self.assertIsNotNone(first_message)
        stream.close()
        paused = self.store.room_snapshot("room_plan")
        round_id = paused["latest_round"]["id"]

        self.store.update_material("room_plan", material["id"], {
            "expected_version": material["version"],
            "title": "恢复前升级 v2",
            "kind": "url",
            "source_url": "https://example.com/v2",
            "content": "恢复前新正文-v2",
        })
        resumed = list(orchestrator.run_round(
            "room_plan",
            "不会覆盖原目标",
            resume_round_id=round_id,
        ))
        resumed_messages = [
            event["message"] for event in resumed if event["type"] == "message"
        ]

        self.assertTrue(resumed_messages)
        self.assertTrue(all("冻结正文-v1" in item for item in provider.inputs))
        self.assertTrue(all("恢复前新正文-v2" not in item for item in provider.inputs))
        self.assertTrue(all(
            message["citations"][0]["version"] == 1
            for message in resumed_messages
        ))
        checkpoint = self.store.get_round_checkpoint("room_plan", round_id)
        self.assertEqual(
            checkpoint["state"]["round_evidence_manifest"]["version"],
            ROUND_EVIDENCE_MANIFEST_VERSION,
        )

    def test_manifest_hashes_and_truncation_are_deterministic(self) -> None:
        material = self.add_material(content="z" * 4000)

        context_a, manifest_a = self.store.material_prompt_bundle("room_plan")
        context_b, manifest_b = self.store.material_prompt_bundle("room_plan")

        self.assertEqual(context_a, context_b)
        self.assertEqual(manifest_a, manifest_b)
        self.assertEqual(manifest_a["version"], ROUND_EVIDENCE_MANIFEST_VERSION)
        self.assertEqual(manifest_a["materials"][0]["body_chars"], 3500)
        self.assertTrue(manifest_a["materials"][0]["truncated"])
        self.assertEqual(len(manifest_a["materials"][0]["content_sha256"]), 64)
        self.assertEqual(len(manifest_a["materials"][0]["snapshot_sha256"]), 64)
        self.assertEqual(
            manifest_a["material_context_sha256"],
            self.store._sha256_text(context_a),
        )

        empty_context, omitted = self.store.material_prompt_bundle(
            "room_plan",
            max_chars=1,
        )
        self.assertEqual(empty_context, "")
        self.assertEqual(omitted["materials"], [])
        self.assertEqual(omitted["omitted_material_ids"], [material["id"]])

    def test_tampered_context_refuses_resume_without_provider_call(self) -> None:
        material = self.add_material()
        provider = RecordingCitationProvider(material["id"])
        orchestrator = DiscussionOrchestrator(
            self.store,
            StaticRegistry(provider),
            market_service=None,
        )
        stream = orchestrator.run_round(
            "room_plan",
            "建立后立即暂停",
            [self.store.enabled_members("room_plan")[0]["id"]],
        )
        self.assertEqual(next(stream)["type"], "round_started")
        stream.close()
        paused = self.store.room_snapshot("room_plan")
        round_id = paused["latest_round"]["id"]
        checkpoint = self.store.get_round_checkpoint("room_plan", round_id)
        tampered_state = checkpoint["state"]
        tampered_state["shared_context"] += "\n被篡改"
        self.store.save_round_checkpoint("room_plan", round_id, tampered_state)

        events = list(orchestrator.run_round(
            "room_plan",
            "恢复",
            resume_round_id=round_id,
        ))
        round_row = self.store.get_round("room_plan", round_id)

        self.assertEqual(events[0]["code"], "ROUND_EVIDENCE_INVALID")
        self.assertEqual(round_row["status"], "PAUSED")
        self.assertEqual(round_row["resume_count"], 0)
        self.assertEqual(provider.inputs, [])

    def test_unknown_manifest_is_not_downgraded_to_legacy(self) -> None:
        members = self.store.enabled_members("room_plan")
        round_row = self.store.create_round("room_plan", "未知清单")
        self.store.save_round_checkpoint("room_plan", round_row["id"], {
            "member_ids": [members[0]["id"]],
            "next_order": 1,
            "max_turns": 1,
            "shared_context": "legacy text",
            "market_snapshot": None,
            "round_evidence_manifest": {"version": "unknown_manifest_v99"},
        })
        self.store.complete_round(round_row["id"], "PAUSED")
        provider = RecordingCitationProvider("unused")

        events = list(DiscussionOrchestrator(
            self.store,
            StaticRegistry(provider),
            market_service=None,
        ).run_round("room_plan", "恢复", resume_round_id=round_row["id"]))

        self.assertEqual(events[0]["code"], "ROUND_EVIDENCE_INVALID")
        self.assertEqual(provider.inputs, [])
        self.assertEqual(
            self.store.get_round("room_plan", round_row["id"])["resume_count"],
            0,
        )

    def test_large_prompt_is_bounded_before_checkpoint_and_resumes(self) -> None:
        material = self.add_material("room_storage")
        provider = RecordingCitationProvider(material["id"])
        market = FixedMarketService(prompt_chars=100000)
        member_ids = [self.store.enabled_members("room_storage")[0]["id"]]
        orchestrator = DiscussionOrchestrator(
            self.store,
            StaticRegistry(provider),
            market_service=market,
        )
        stream = orchestrator.run_round("room_storage", "超长上下文", member_ids)
        self.assertEqual(next(stream)["type"], "round_started")
        stream.close()
        paused = self.store.room_snapshot("room_storage")
        round_id = paused["latest_round"]["id"]
        checkpoint = self.store.get_round_checkpoint("room_storage", round_id)

        self.assertLessEqual(len(checkpoint["state"]["shared_context"]), 30000)
        resumed = list(orchestrator.run_round(
            "room_storage",
            "恢复",
            resume_round_id=round_id,
        ))
        self.assertEqual(resumed[0]["type"], "round_resumed")
        self.assertEqual(market.calls, 1)
        self.assertTrue(provider.inputs)

    def test_oversized_checkpoint_cancels_before_provider_call(self) -> None:
        material = self.add_material("room_storage")
        provider = RecordingCitationProvider(material["id"])
        market = FixedMarketService(
            blob_chars=ROUND_CHECKPOINT_MAX_BYTES + 100_000,
        )
        events = list(DiscussionOrchestrator(
            self.store,
            StaticRegistry(provider),
            market_service=market,
        ).run_round(
            "room_storage",
            "检查点过大时安全失败",
            [self.store.enabled_members("room_storage")[0]["id"]],
        ))
        latest = self.store.room_snapshot("room_storage")["latest_round"]

        self.assertEqual(events[-1]["code"], "ROUND_CHECKPOINT_INVALID")
        self.assertEqual(latest["status"], "CANCELLED")
        self.assertEqual(provider.inputs, [])

    def test_real_scale_market_snapshot_checkpoint_resumes_with_frozen_hash(self) -> None:
        material = self.add_material("room_storage")
        provider = RecordingCitationProvider(material["id"])
        market = FixedMarketService(blob_chars=190_000)
        member_ids = [self.store.enabled_members("room_storage")[0]["id"]]
        orchestrator = DiscussionOrchestrator(
            self.store,
            StaticRegistry(provider),
            market_service=market,
        )

        stream = orchestrator.run_round(
            "room_storage",
            "Freeze a real-scale Futu market snapshot.",
            member_ids,
        )
        self.assertEqual(next(stream)["type"], "round_started")
        stream.close()
        paused = self.store.room_snapshot("room_storage")
        round_id = paused["latest_round"]["id"]
        checkpoint_before = self.store.get_round_checkpoint(
            "room_storage",
            round_id,
        )
        state_before = checkpoint_before["state"]
        snapshot_before = state_before["market_snapshot"]
        frozen_hash = state_before["round_evidence_manifest"][
            "market_snapshot"
        ]["snapshot_sha256"]
        snapshot_bytes = len(
            json.dumps(snapshot_before, ensure_ascii=False).encode("utf-8")
        )

        self.assertGreaterEqual(snapshot_bytes, 170_000)
        self.assertLessEqual(snapshot_bytes, 200_000)
        self.assertEqual(len(snapshot_before["test_blob"]), 190_000)
        self.assertEqual(
            frozen_hash,
            self.store._canonical_sha256(snapshot_before),
        )
        resumed = list(orchestrator.run_round(
            "room_storage",
            "Resume without recapturing or truncating the snapshot.",
            resume_round_id=round_id,
        ))
        checkpoint_after = self.store.get_round_checkpoint(
            "room_storage",
            round_id,
        )
        state_after = checkpoint_after["state"]

        self.assertEqual(resumed[0]["type"], "round_resumed")
        self.assertEqual(market.calls, 1)
        self.assertEqual(state_after["market_snapshot"], snapshot_before)
        self.assertEqual(
            state_after["round_evidence_manifest"]["market_snapshot"][
                "snapshot_sha256"
            ],
            frozen_hash,
        )

    def test_turn_contract_uses_same_unique_frozen_snapshot_before_and_after_resume(self) -> None:
        room_snapshot = self.store.room_snapshot("room_storage")
        members = self.store.enabled_members("room_storage")
        facilitator = next(
            member for member in members
            if member.get("workflow_stage") == "facilitate"
        )
        analyst = next(
            member for member in members
            if member.get("workflow_stage") == "analysis"
        )
        workflow_policy = {
            "version": 1,
            "stage_order": ["facilitate", "analysis"],
            "minimum_stage_coverage": {"facilitate": 1, "analysis": 1},
            "required_coverage": [
                {
                    "id": "facilitation",
                    "label": "主持",
                    "minimum": 1,
                    "any_of": {"stances": [facilitator["stance"]], "capabilities": []},
                    "is_counterargument": False,
                },
                {
                    "id": "snapshot_analysis",
                    "label": "冻结快照分析",
                    "minimum": 1,
                    "any_of": {"stances": [analyst["stance"]], "capabilities": []},
                    "is_counterargument": False,
                },
            ],
            "minimum_successful_members": 2,
            "max_turns_per_member": 1,
            "follow_up_budget": 0,
            "user_confirmation_required": True,
            "execution_capability": "none",
            "live_trading_allowed": False,
        }
        self.store.update_room("room_storage", {
            "expected_settings_version": room_snapshot["room"]["settings_version"],
            "discussion_mode": "sequential",
            "workflow_policy": workflow_policy,
            "capability_pack_ids": [
                "storage_research_readonly",
                "structured_turn_contract_v1",
            ],
        })
        provider = SnapshotTurnContractProvider("frozen-storage-snapshot")
        market = FixedMarketService()
        orchestrator = DiscussionOrchestrator(
            self.store,
            StaticRegistry(provider),
            market_service=market,
        )

        stream = orchestrator.run_round(
            "room_storage",
            "验证冻结快照合同恢复",
            [facilitator["id"], analyst["id"]],
        )
        first_message = None
        for event in stream:
            if event["type"] == "message":
                first_message = event["message"]
                break
        self.assertIsNotNone(first_message)
        stream.close()
        round_id = self.store.room_snapshot("room_storage")["latest_round"]["id"]

        resumed = list(orchestrator.run_round(
            "room_storage",
            "恢复时不得重取或换用快照",
            resume_round_id=round_id,
        ))
        resumed_messages = [
            event["message"] for event in resumed if event["type"] == "message"
        ]

        self.assertEqual(len(resumed_messages), 1)
        self.assertEqual(market.calls, 1)
        self.assertEqual(len(provider.inputs), 2)
        self.assertTrue(all(
            "本条发言合同允许引用的唯一冻结市场快照ID：frozen-storage-snapshot"
            in input_text
            for input_text in provider.inputs
        ))
        self.assertTrue(all(
            "message|material|round_market_snapshot" in instruction
            for instruction in provider.instructions
        ))
        all_messages = [first_message, *resumed_messages]
        self.assertTrue(all(message["turn_contract_qualified"] for message in all_messages))
        self.assertTrue(all(
            message["turn_contract"]["claims"][0]["evidence"][0]["id"]
            == "frozen-storage-snapshot"
            for message in all_messages
        ))
        bundle = self.store.round_turn_contract_bundle("room_storage", round_id)
        self.assertTrue(bundle["valid"], bundle["issues"])

    def test_oversized_visible_output_fails_before_tail_citation_persistence(self) -> None:
        material = self.add_material()
        provider = TailCitationProvider(material["id"])
        events = list(DiscussionOrchestrator(
            self.store,
            StaticRegistry(provider),
            market_service=None,
        ).run_round(
            "room_plan",
            "尾部引用",
            [self.store.enabled_members("room_plan")[0]["id"]],
        ))
        failure = next(event for event in events if event["type"] == "speaker_failed")
        self.assertEqual(failure["code"], "ROUND_TURN_CONTRACT_INVALID")
        self.assertIn(
            "VISIBLE_CONTENT_TOO_LONG",
            {issue["code"] for issue in failure["turn_contract_issues"]},
        )
        round_messages = self.store.round_messages("room_plan", events[-1]["round_id"])
        self.assertFalse(any(message["sender_type"] == "ai" for message in round_messages))
        self.assertTrue(all(message.get("citations") == [] for message in round_messages))

    def test_invalid_exact_citation_rolls_back_whole_message(self) -> None:
        material = self.add_material()
        before = len(self.store.recent_messages("room_plan", 80))

        with self.assertRaisesRegex(ValueError, "冻结资料引用"):
            self.store.add_message(
                "room_plan",
                sender_type="ai",
                sender_name="测试成员",
                content=f"无效精确版本。[资料:{material['id']}]",
                citations=[{"id": material["id"], "version": 999}],
            )

        self.assertEqual(len(self.store.recent_messages("room_plan", 80)), before)

    def test_observation_binds_frozen_material_and_market_snapshot(self) -> None:
        room = self.store.room_snapshot("room_storage")["room"]
        self.store.update_room("room_storage", {
            "expected_settings_version": room["settings_version"],
            "capability_pack_ids": ["storage_research_readonly"],
        })
        material = self.add_material("room_storage")

        def update_to_v2() -> None:
            self.store.update_material("room_storage", material["id"], {
                "expected_version": material["version"],
                "title": "轮中更新",
                "kind": "url",
                "source_url": "https://example.com/v2",
                "content": "轮中更新正文-v2",
            })

        provider = ObservationCitationProvider(
            material["id"],
            before_first_response=update_to_v2,
        )
        market = FixedMarketService()
        enabled_members = self.store.enabled_members("room_storage")
        protocol_member_ids = [
            next(
                member["id"]
                for member in enabled_members
                if member["workflow_stage"] == stage
            )
            for stage in ("plan", "risk", "decision")
        ]
        events = list(DiscussionOrchestrator(
            self.store,
            StaticRegistry(provider),
            market_service=market,
        ).run_round(
            "room_storage",
            "形成冻结观察",
            protocol_member_ids,
        ))
        observation = self.store.list_observations("room_storage")[0]
        message = [
            event["message"] for event in events if event["type"] == "message"
        ][-1]

        self.assertEqual(message["citations"][0]["version"], 1)
        self.assertEqual(
            observation["evidence"]["material_refs"],
            [{"id": material["id"], "version": 1}],
        )
        self.assertEqual(
            observation["evidence"]["market_snapshot_id"],
            "frozen-storage-snapshot",
        )
        self.assertEqual(
            observation["evidence"]["market_evidence_version"],
            "storage_market_evidence_v6",
        )
        self.assertEqual(
            len(observation["evidence"]["market_snapshot_sha256"]),
            64,
        )
        self.assertFalse(observation["user_confirmed"])


if __name__ == "__main__":
    unittest.main()
