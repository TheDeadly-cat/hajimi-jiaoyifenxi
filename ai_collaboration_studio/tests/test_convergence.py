from __future__ import annotations

import hashlib
import json
import copy
import tempfile
import unittest
from pathlib import Path

from backend.convergence import ConvergenceService
from backend.orchestrator import DiscussionOrchestrator
from backend.paper_portfolio import default_paper_portfolio_plan
from backend.providers.base import ProviderResponse
from backend.store import OBSERVATION_SCORECARD_VERSION, StudioStore
from tests.storage_research_fixture import ready_storage_research_evidence
from tests.turn_contract_fixture import append_valid_turn_contract


LIVE_QUOTE_FRESHNESS = {
    "age_seconds": 60,
    "quote_is_live": True,
    "freshness_basis": "live_20m_window",
}


def mutate_fixture_turn_contract(content: str, mutator) -> str:
    """Mutate the semantic fixture while preserving its selected wire format."""

    if str(content or "").lstrip().startswith("{"):
        envelope = json.loads(content)
        mutator(envelope["turn_contract"])
        return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    visible, raw_contract = content.rsplit("<turn_contract>", 1)
    payload_text, suffix = raw_contract.split("</turn_contract>", 1)
    payload = json.loads(payload_text)
    mutator(payload)
    return (
        f"{visible}<turn_contract>"
        f"{json.dumps(payload, ensure_ascii=False)}"
        f"</turn_contract>{suffix}"
    )


class ConvergenceProvider:
    provider_id = "openai"

    def __init__(
        self,
        *,
        request_early_finish: bool = False,
        fail_members: bool = False,
        decision_self_invents: bool = False,
        omit_risk_candidate_review: bool = False,
    ) -> None:
        self.request_early_finish = request_early_finish
        self.fail_members = fail_members
        self.decision_self_invents = decision_self_invents
        self.omit_risk_candidate_review = omit_risk_candidate_review
        self.director_inputs: list[str] = []

    def status(self) -> dict[str, object]:
        return {"id": self.provider_id, "configured": True}

    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        if "隐藏主持调度器" in instructions:
            self.director_inputs.append(input_text)
            content = json.dumps({
                "action": "finish" if self.request_early_finish else "speak",
                "member_id": "",
                "reason": "测试主持人收敛门。",
            }, ensure_ascii=False)
            return ProviderResponse(ok=True, provider=self.provider_id, model=model or "fake", content=content)
        if self.fail_members:
            return ProviderResponse(ok=False, provider=self.provider_id, model=model or "fake", error="测试失败")
        content = append_valid_turn_contract(
            "区分事实、推断和未知，并提出一个可验证的下一步。",
            instructions=instructions,
            input_text=input_text,
        )
        if self.omit_risk_candidate_review and "candidate_risk_review_v1" in input_text:
            content = mutate_fixture_turn_contract(
                content,
                lambda payload: payload.__setitem__("candidate_updates", []),
            )
        if self.decision_self_invents and "流程阶段：decision。" in instructions:
            def invent_candidates(payload: dict) -> None:
                payload["candidate_updates"][0]["id"] = "invented_option_a"
                payload["candidate_updates"][1]["id"] = "invented_option_b"

            content = mutate_fixture_turn_contract(content, invent_candidates)
        return ProviderResponse(
            ok=True,
            provider=self.provider_id,
            model=model or "fake",
            content=content,
        )


class ConvergenceRegistry:
    def __init__(self, provider: ConvergenceProvider) -> None:
        self.provider = provider

    def get(self, _provider_id: str) -> ConvergenceProvider:
        self.provider.provider_id = str(_provider_id or "openai")
        return self.provider


class StaticStorageMarket:
    def __init__(self, snapshot: dict[str, object]) -> None:
        self._snapshot = copy.deepcopy(snapshot)

    def snapshot(self) -> dict[str, object]:
        return copy.deepcopy(self._snapshot)

    @staticmethod
    def prompt_context(snapshot: dict[str, object]) -> str:
        return f"snapshot_id={snapshot['snapshot_id']}"

    @staticmethod
    def timeline_summary(snapshot: dict[str, object]) -> str:
        return f"共享快照 {snapshot['snapshot_id']}"


class ConvergenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "studio.sqlite3")
        self.service = ConvergenceService(self.store)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _use_text_only_storage_fixture(self) -> None:
        """Keep legacy prose-provider tests focused on convergence and data gates."""
        room = self.store.room_snapshot("room_storage")["room"]
        self.store.update_room("room_storage", {
            "expected_settings_version": room["settings_version"],
            "capability_pack_ids": ["storage_research_readonly"],
        })

    def test_new_room_explains_why_it_has_not_converged(self) -> None:
        state = self.service.evaluate("room_plan")

        self.assertEqual(state["decision_status"], "NOT_STARTED")
        self.assertFalse(state["can_host_finish"])
        self.assertFalse(state["can_autonomously_decide"])
        self.assertEqual(state["execution_capability"], "none")
        self.assertIn("ROUND_NOT_STARTED", [item["code"] for item in state["blockers"]])

    def test_legacy_round_ignores_current_room_turn_contract_pack(self) -> None:
        legacy_round = self.store.create_round("room_plan", "历史轮沿用历史协议")
        self.store.complete_round(legacy_round["id"], "PARTIAL")
        room = self.store.room_snapshot("room_plan")["room"]
        self.store.update_room("room_plan", {
            "expected_settings_version": room["settings_version"],
            "capability_pack_ids": ["structured_turn_contract_v1"],
        })

        state = self.service.evaluate("room_plan", round_id=legacy_round["id"])

        self.assertFalse(state["turn_contract_gate"]["applicable"])
        self.assertIsNone(state["turn_contract_gate"]["version"])
        self.assertTrue(state["turn_contract_gate"]["ready"])

    def test_workflow_configuration_preflight_rejects_missing_decision_stage(self) -> None:
        snapshot = self.store.room_snapshot("room_plan")
        ready = self.service.workflow_configuration_preflight(snapshot)

        self.assertTrue(ready["ready"])
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

        blocked = self.service.workflow_configuration_preflight(
            self.store.room_snapshot("room_plan")
        )

        self.assertFalse(blocked["ready"])
        self.assertIn(
            "WORKFLOW_STAGE_DECISION_MISSING",
            [item["code"] for item in blocked["blockers"]],
        )
        decision_coverage = next(
            item for item in blocked["stage_coverage"] if item["id"] == "decision"
        )
        self.assertEqual(decision_coverage["configured_count"], 0)
        self.assertEqual(decision_coverage["required_count"], 1)

    def test_project_workspace_exposes_auditable_gap_focus(self) -> None:
        self.store.create_artifact(
            "room_project",
            title="项目缺口快照",
            content={
                "summary": "保留开放风险。",
                "summary_evidence": [],
                "requirements": [{
                    "id": "req_one",
                    "text": "验证核心流程。",
                    "status": "confirmed",
                    "acceptance_criteria": "五名用户完成三次任务。",
                    "evidence": [],
                }],
                "risks": [{
                    "id": "risk_one",
                    "text": "资源可能不足。",
                    "status": "open",
                    "blocking": True,
                    "trigger": "首周排期超过十天。",
                    "mitigation": "缩减非核心范围。",
                    "evidence": [],
                }],
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
                "decision": {
                    "status": "candidate",
                    "options": [
                        {"id": "a", "title": "小范围验证", "description": "验证一条核心流程。", "value": "验证", "cost": "低", "timeline": "两周", "dependencies": ["用户"], "reversibility": "high"},
                        {"id": "b", "title": "完整交付", "description": "实现全部规划范围。", "value": "覆盖", "cost": "高", "timeline": "六周", "dependencies": ["资源"], "reversibility": "low"},
                    ],
                    "preferred_option_id": "a",
                    "rationale": "优先选择可逆路径。",
                },
            },
        )

        workspace = self.service.project_workspace_snapshot("room_project", frozen=True)
        state = self.service.evaluate("room_project")

        self.assertTrue(workspace["applicable"])
        self.assertTrue(workspace["frozen"])
        self.assertEqual(workspace["requirement_count"], 1)
        self.assertEqual(workspace["risk_count"], 1)
        self.assertEqual(workspace["option_count"], 2)
        self.assertEqual(workspace["focus"]["code"], "PROJECT_BLOCKING_RISK_OPEN")
        self.assertEqual(workspace["focus"]["target_capabilities"], ["critical_review"])
        self.assertEqual(
            state["project_workspace"]["focus"]["code"],
            "PROJECT_BLOCKING_RISK_OPEN",
        )
        prompt = self.service.project_workspace_prompt_context(workspace)
        self.assertIn("项目研究工作区缺口快照", prompt)
        self.assertIn("适配职责=critical_review", prompt)

    def test_director_cannot_finish_before_required_roles_succeed(self) -> None:
        provider = ConvergenceProvider(request_early_finish=True)
        orchestrator = DiscussionOrchestrator(self.store, ConvergenceRegistry(provider), market_service=None)

        events = list(orchestrator.run_round("room_plan", "形成可审查的候选方案"))
        messages = [event for event in events if event["type"] == "message"]
        final = events[-1]

        # The default generic room requires host, critical review, and a final
        # decision-synthesis stage. An early finish request cannot skip any of them.
        self.assertEqual(len(messages), 4)
        self.assertEqual(final["type"], "round_completed")
        self.assertEqual(final["status"], "COMPLETED")
        self.assertTrue(final["convergence"]["can_host_finish"])
        self.assertTrue(final["convergence"]["candidate_lineage_gate"]["ready"])
        self.assertEqual(
            final["convergence"]["candidate_lineage_gate"]["version"],
            "candidate_lineage_v1",
        )
        self.assertFalse(final["convergence"]["candidate_risk_review_gate"]["applicable"])
        self.assertTrue(final["convergence"]["candidate_risk_review_gate"]["ready"])
        self.assertTrue(all(
            item["ready"]
            for item in final["convergence"]["discussion_gate"]["role_coverage"]
        ))
        self.assertEqual(final["convergence"]["decision_status"], "DRAFT_REQUIRED")

    def test_v1_round_cannot_converge_when_decision_self_invents_candidates(self) -> None:
        provider = ConvergenceProvider(decision_self_invents=True)
        orchestrator = DiscussionOrchestrator(
            self.store,
            ConvergenceRegistry(provider),
            market_service=None,
        )

        final = list(orchestrator.run_round(
            "room_plan",
            "验证决策只能引用决策前候选",
        ))[-1]

        lineage_gate = final["convergence"]["candidate_lineage_gate"]
        self.assertFalse(lineage_gate["ready"])
        self.assertEqual(lineage_gate["status"], "blocked")
        self.assertIn(
            "CANDIDATE_LINEAGE_SOURCE_MISSING",
            [item["code"] for item in lineage_gate["blockers"]],
        )
        self.assertFalse(final["convergence"]["discussion_gate"]["ready"])
        self.assertFalse(final["convergence"]["can_host_finish"])
        self.assertTrue(provider.director_inputs)
        self.assertIn('"ready": false', provider.director_inputs[0].lower())

    def test_failed_model_attempt_does_not_count_as_role_coverage(self) -> None:
        provider = ConvergenceProvider(fail_members=True)
        orchestrator = DiscussionOrchestrator(self.store, ConvergenceRegistry(provider), market_service=None)

        events = list(orchestrator.run_round("room_plan", "失败不能冒充覆盖"))
        final = events[-1]

        self.assertEqual(final["status"], "PARTIAL")
        self.assertEqual(final["convergence"]["discussion_gate"]["successful_member_count"], 0)
        self.assertFalse(final["convergence"]["can_host_finish"])
        checkpoint = self.store.get_round_checkpoint("room_plan", final["round_id"])
        self.assertEqual(checkpoint["state"]["successful_member_ids"], [])

    def test_storage_committee_requires_market_data_before_discussion(self) -> None:
        provider = ConvergenceProvider()
        orchestrator = DiscussionOrchestrator(self.store, ConvergenceRegistry(provider), market_service=None)
        before = self.store.room_snapshot("room_storage")

        events = list(orchestrator.run_round("room_storage", "比较四家公司并给出模拟观察"))
        after = self.store.room_snapshot("room_storage")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["code"], "ROUND_MARKET_PREFLIGHT_FAILED")
        self.assertFalse(events[0]["preflight"]["ready"])
        self.assertEqual(after["latest_round"], before["latest_round"])
        self.assertEqual(after["messages"], before["messages"])
        self.assertEqual(provider.director_inputs, [])

    def test_storage_data_gate_rejects_degraded_or_implicit_safety_snapshot(self) -> None:
        rows = [
            {
                "symbol": symbol,
                "last": 100 + index,
                "quality": "ready",
                **LIVE_QUOTE_FRESHNESS,
                "market_time": "2026-07-20 15:59:00",
            }
            for index, symbol in enumerate(("US.MU", "US.SNDK", "US.WDC", "US.STX"))
        ]
        ready_snapshot = {
            "ok": True,
            "state": "ready",
            "source": "futu_opend",
            "snapshot_id": "strict-ready-snapshot",
            "captured_at": "2026-07-20T20:00:00Z",
            "rows": rows,
            "missing_symbols": [],
            "source_errors": [],
            "evidence": ready_storage_research_evidence(
                captured_at="2026-07-20T20:00:00Z",
                technical_as_of="2026-07-20 00:00:00",
            ),
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

        ready = self.service.evaluate("room_storage", runtime={"market_snapshot": ready_snapshot})
        degraded_snapshot = copy.deepcopy(ready_snapshot)
        degraded_snapshot["state"] = "degraded"
        degraded_snapshot["rows"][0]["quality"] = "stale"
        implicit_safety_snapshot = copy.deepcopy(ready_snapshot)
        implicit_safety_snapshot.pop("execution_capability")
        implicit_safety_snapshot.pop("live_trading_allowed")
        legacy_freshness_snapshot = copy.deepcopy(ready_snapshot)
        for row in legacy_freshness_snapshot["rows"]:
            row.pop("age_seconds")
            row.pop("quote_is_live")
            row.pop("freshness_basis")
        degraded = self.service.evaluate("room_storage", runtime={"market_snapshot": degraded_snapshot})
        implicit = self.service.evaluate("room_storage", runtime={"market_snapshot": implicit_safety_snapshot})
        legacy = self.service.evaluate("room_storage", runtime={"market_snapshot": legacy_freshness_snapshot})

        self.assertTrue(ready["data_gate"]["ready"])
        self.assertFalse(degraded["data_gate"]["ready"])
        self.assertFalse(degraded["data_gate"]["snapshot_quality_ready"])
        self.assertFalse(implicit["data_gate"]["ready"])
        self.assertFalse(implicit["data_gate"]["safety_fields_explicit"])
        self.assertIn("EXECUTION_BOUNDARY_BROKEN", [item["code"] for item in implicit["data_gate"]["blockers"]])
        self.assertFalse(legacy["data_gate"]["ready"])
        self.assertEqual(
            legacy["data_gate"]["invalid_freshness_symbols"],
            ["US.MU", "US.SNDK", "US.STX", "US.WDC"],
        )
        self.assertIn(
            "MARKET_SNAPSHOT_FRESHNESS_INVALID",
            [item["code"] for item in legacy["data_gate"]["blockers"]],
        )

    def test_nested_manual_state_without_room_bound_envelope_blocks_convergence(self) -> None:
        for source_key in ("official_earnings_packs", "official_earnings_materials"):
            with self.subTest(source_key=source_key):
                evidence = ready_storage_research_evidence()
                evidence["state"] = "ready"
                evidence.pop("manual_official_evidence", None)
                evidence.setdefault(source_key, {
                    "rows": [],
                    "source_errors": [],
                })["state"] = "ready_with_manual_substitution"
                gate = self.service._storage_research_evidence_gate(
                    {
                        "captured_at": "2026-07-20T20:00:00Z",
                        "evidence": evidence,
                    },
                    expected_room_id="room_storage",
                )
                self.assertFalse(gate["ready"])
                self.assertIn(
                    "STORAGE_MANUAL_OFFICIAL_EVIDENCE_INVALID",
                    {item["code"] for item in gate["blockers"]},
                )

    def test_missing_or_empty_official_earnings_packs_fail_closed(self) -> None:
        def rebind_identity(
            evidence: dict,
            *,
            release_url: object | None = None,
            fiscal_period: object | None = None,
        ) -> None:
            pack = evidence["official_earnings_packs"]["rows"][0]["packs"][0]
            if release_url is not None:
                pack["release_url"] = release_url
            if fiscal_period is not None:
                pack["fiscal_period"] = fiscal_period
            pack["pack_id"] = "earnings_" + hashlib.sha256(
                (
                    f"{pack['symbol']}|{pack['fiscal_period']}|"
                    f"{pack['release_url']}"
                ).encode("utf-8")
            ).hexdigest()[:24]

        def append_invalid_sibling(evidence: dict) -> None:
            packs = evidence["official_earnings_packs"]["rows"][0]["packs"]
            invalid = copy.deepcopy(packs[0])
            invalid["live_trading_allowed"] = True
            packs.append(invalid)

        cases = {
            "missing_field": lambda evidence: evidence.pop("official_earnings_packs"),
            "empty_pack": lambda evidence: evidence["official_earnings_packs"]["rows"][0].update({"packs": []}),
            "null_pack": lambda evidence: evidence["official_earnings_packs"]["rows"][0].update({"packs": [None]}),
            "empty_object_pack": lambda evidence: evidence["official_earnings_packs"]["rows"][0].update({"packs": [{}]}),
            "legacy_version": lambda evidence: evidence["official_earnings_packs"].update({"version": "official_earnings_pack_v0"}),
            "symbol_mismatch": lambda evidence: evidence["official_earnings_packs"]["rows"][0]["packs"][0].update({"symbol": "US.OTHER"}),
            "numeric_rows": lambda evidence: evidence["official_earnings_packs"].update({"rows": 7}),
            "malformed_ipv6_url": lambda evidence: rebind_identity(evidence, release_url="https://[::1"),
            "arbitrary_https_domain": lambda evidence: rebind_identity(evidence, release_url="https://evil.example/report"),
            "whitespace_hostname": lambda evidence: rebind_identity(evidence, release_url="https://investors.micron.com /report"),
            "invalid_named_port": lambda evidence: rebind_identity(evidence, release_url="https://investors.micron.com:bad/report"),
            "non_https_port": lambda evidence: rebind_identity(evidence, release_url="https://investors.micron.com:444/report"),
            "whitespace_path": lambda evidence: rebind_identity(evidence, release_url="https://investors.micron.com/report name"),
            "control_character_url": lambda evidence: rebind_identity(evidence, release_url="https://investors.micron.com/report\nname"),
            "unresolved_period": lambda evidence: rebind_identity(evidence, fiscal_period="UNRESOLVED"),
            "garbage_period": lambda evidence: rebind_identity(evidence, fiscal_period="garbage"),
            "numeric_pack_id": lambda evidence: evidence["official_earnings_packs"]["rows"][0]["packs"][0].update({"pack_id": 123}),
            "numeric_fiscal_period": lambda evidence: evidence["official_earnings_packs"]["rows"][0]["packs"][0].update({"fiscal_period": 2026}),
            "tampered_pack_id": lambda evidence: evidence["official_earnings_packs"]["rows"][0]["packs"][0].update({"pack_id": "earnings_deadbeef"}),
            "invalid_sibling_pack": append_invalid_sibling,
            "top_level_execution_capability": lambda evidence: evidence["official_earnings_packs"].update({"execution_capability": "orders"}),
            "top_level_live_trading": lambda evidence: evidence["official_earnings_packs"].update({"live_trading_allowed": True}),
            "duplicate_symbol_row": lambda evidence: evidence["official_earnings_packs"]["rows"].append(copy.deepcopy(evidence["official_earnings_packs"]["rows"][0])),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                evidence = ready_storage_research_evidence()
                mutate(evidence)
                gate = self.service._storage_research_evidence_gate({
                    "captured_at": "2026-07-20T20:00:00Z",
                    "evidence": evidence,
                })

                self.assertFalse(gate["ready"])
                self.assertIn(
                    "STORAGE_OFFICIAL_EARNINGS_PACKS_MISSING",
                    {item["code"] for item in gate["blockers"]},
                )

    def test_partial_official_earnings_packs_without_source_errors_fail_closed(self) -> None:
        evidence = ready_storage_research_evidence()
        evidence["official_earnings_packs"]["state"] = "partial"

        gate = self.service._storage_research_evidence_gate({
            "captured_at": "2026-07-20T20:00:00Z",
            "evidence": evidence,
        })

        self.assertFalse(gate["ready"])
        self.assertIn(
            "STORAGE_OFFICIAL_EARNINGS_PACKS_NOT_READY",
            {item["code"] for item in gate["blockers"]},
        )

    def test_official_earnings_pack_source_error_does_not_duplicate_not_ready(self) -> None:
        evidence = ready_storage_research_evidence()
        evidence["official_earnings_packs"]["state"] = "partial"
        evidence["official_earnings_packs"]["source_errors"] = [{
            "code": "EARNINGS_MATERIAL_ACCESS_TIMEOUT",
            "message": "fixture",
        }]

        gate = self.service._storage_research_evidence_gate({
            "captured_at": "2026-07-20T20:00:00Z",
            "evidence": evidence,
        })
        codes = {item["code"] for item in gate["blockers"]}

        self.assertFalse(gate["ready"])
        self.assertIn("STORAGE_OFFICIAL_EARNINGS_PACKS_SOURCE_ERROR", codes)
        self.assertNotIn("STORAGE_OFFICIAL_EARNINGS_PACKS_NOT_READY", codes)

    def test_storage_data_gate_rejects_future_market_rows(self) -> None:
        snapshot = {
            "ok": True,
            "state": "ready",
            "source": "futu_opend",
            "snapshot_id": "future-market-time",
            "captured_at": "2026-07-20T20:00:00Z",
            "rows": [
                {
                    "symbol": symbol,
                    "last": 100 + index,
                    "quality": "ready",
                    **LIVE_QUOTE_FRESHNESS,
                    "market_time": "2026-07-20 16:01:00",
                }
                for index, symbol in enumerate(("US.MU", "US.SNDK", "US.WDC", "US.STX"))
            ],
            "missing_symbols": [],
            "source_errors": [],
            "evidence": ready_storage_research_evidence(),
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

        state = self.service.evaluate("room_storage", runtime={"market_snapshot": snapshot})
        codes = [item["code"] for item in state["data_gate"]["blockers"]]

        self.assertFalse(state["data_gate"]["ready"])
        self.assertEqual(
            state["data_gate"]["future_market_time_symbols"],
            ["US.MU", "US.SNDK", "US.STX", "US.WDC"],
        )
        self.assertIn("MARKET_SNAPSHOT_TIME_FUTURE", codes)

    def test_duplicate_future_canonical_row_cannot_hide_behind_four_ready_rows(self) -> None:
        snapshot = {
            "ok": True,
            "state": "ready",
            "source": "futu_opend",
            "snapshot_id": "duplicate-future-market-time",
            "captured_at": "2026-07-20T20:00:00Z",
            "rows": [
                {
                    "symbol": symbol,
                    "last": 100 + index,
                    "quality": "ready",
                    **LIVE_QUOTE_FRESHNESS,
                    "market_time": "2026-07-20 15:59:00",
                    "updated_at": "2026-07-20T19:59:00Z",
                }
                for index, symbol in enumerate(("US.MU", "US.SNDK", "US.WDC", "US.STX"))
            ] + [{
                "symbol": "US.MU",
                "last": 999,
                "quality": "ready",
                **LIVE_QUOTE_FRESHNESS,
                "market_time": "2026-07-20 15:59:00",
                "updated_at": "2026-07-20T20:01:00Z",
            }],
            "missing_symbols": [],
            "source_errors": [],
            "evidence": ready_storage_research_evidence(),
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

        state = self.service.evaluate("room_storage", runtime={"market_snapshot": snapshot})

        self.assertFalse(state["data_gate"]["ready"])
        self.assertFalse(state["data_gate"]["snapshot_quality_ready"])
        self.assertEqual(state["data_gate"]["future_market_time_symbols"], ["US.MU"])

    def test_malformed_canonical_market_time_does_not_fallback_to_display_time(self) -> None:
        snapshot = {
            "ok": True,
            "state": "ready",
            "source": "futu_opend",
            "snapshot_id": "invalid-canonical-time",
            "captured_at": "2026-07-20T20:00:00Z",
            "rows": [
                {
                    "symbol": symbol,
                    "last": 100 + index,
                    "quality": "ready",
                    **LIVE_QUOTE_FRESHNESS,
                    "market_time": "2026-07-20 15:59:00",
                    "updated_at": "not-a-time" if symbol == "US.MU" else "2026-07-20T19:59:00Z",
                }
                for index, symbol in enumerate(("US.MU", "US.SNDK", "US.WDC", "US.STX"))
            ],
            "missing_symbols": [],
            "source_errors": [],
            "evidence": ready_storage_research_evidence(),
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

        state = self.service.evaluate("room_storage", runtime={"market_snapshot": snapshot})

        self.assertFalse(state["data_gate"]["ready"])
        self.assertEqual(state["data_gate"]["invalid_market_time_symbols"], ["US.MU"])

    def test_nested_supplemental_source_error_blocks_research_convergence(self) -> None:
        evidence = ready_storage_research_evidence()
        evidence["capital_flow"] = {
            "rows": [{"symbol": symbol} for symbol in ("US.MU", "US.SNDK", "US.WDC", "US.STX")],
            "source_errors": [{"code": "CAPITAL_FLOW_PARTIAL", "message": "fixture"}],
        }
        snapshot = {
            "ok": True,
            "state": "ready",
            "source": "futu_opend",
            "snapshot_id": "nested-source-error",
            "captured_at": "2026-07-20T20:00:00Z",
            "rows": [
                {
                    "symbol": symbol,
                    "last": 100 + index,
                    "quality": "ready",
                    **LIVE_QUOTE_FRESHNESS,
                    "market_time": "2026-07-20 15:59:00",
                }
                for index, symbol in enumerate(("US.MU", "US.SNDK", "US.WDC", "US.STX"))
            ],
            "missing_symbols": [],
            "source_errors": [],
            "evidence": evidence,
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

        state = self.service.evaluate("room_storage", runtime={"market_snapshot": snapshot})
        codes = [item["code"] for item in state["research_evidence_gate"]["blockers"]]

        self.assertFalse(state["data_gate"]["ready"])
        self.assertFalse(state["research_evidence_gate"]["ready"])
        self.assertIn("STORAGE_CAPITAL_FLOW_SOURCE_ERROR", codes)
        self.assertIn("STORAGE_RESEARCH_SOURCE_ERROR", codes)

    def test_nested_core_evidence_error_blocks_even_when_outer_state_claims_ready(self) -> None:
        evidence = ready_storage_research_evidence()
        evidence["fundamental"] = {
            "rows": [{
                "symbol": "US.MU",
                "source_errors": [{"code": "NESTED_QUOTE_ERROR", "message": "fixture"}],
            }],
            "source_errors": [],
        }
        snapshot = {
            "ok": True,
            "state": "ready",
            "source": "futu_opend",
            "snapshot_id": "nested-core-error",
            "captured_at": "2026-07-20T20:00:00Z",
            "rows": [
                {
                    "symbol": symbol,
                    "last": 100 + index,
                    "quality": "ready",
                    **LIVE_QUOTE_FRESHNESS,
                    "market_time": "2026-07-20 15:59:00",
                }
                for index, symbol in enumerate(("US.MU", "US.SNDK", "US.WDC", "US.STX"))
            ],
            "missing_symbols": [],
            "source_errors": [],
            "evidence": evidence,
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

        state = self.service.evaluate("room_storage", runtime={"market_snapshot": snapshot})
        codes = [item["code"] for item in state["research_evidence_gate"]["blockers"]]

        self.assertFalse(state["data_gate"]["ready"])
        self.assertIn("STORAGE_RESEARCH_SOURCE_ERROR", codes)

    def test_host_finish_requires_the_complete_data_gate_after_discussion(self) -> None:
        self._use_text_only_storage_fixture()
        ready_snapshot = {
            "ok": True,
            "state": "ready",
            "source": "futu_opend",
            "snapshot_id": "host-finish-ready",
            "captured_at": "2026-07-20T20:00:00Z",
            "rows": [
                {
                    "symbol": symbol,
                    "last": 100 + index,
                    "quality": "ready",
                    **LIVE_QUOTE_FRESHNESS,
                    "market_time": "2026-07-20 15:59:00",
                }
                for index, symbol in enumerate(("US.MU", "US.SNDK", "US.WDC", "US.STX"))
            ],
            "missing_symbols": [],
            "source_errors": [],
            "evidence": ready_storage_research_evidence(),
            "execution_capability": "none",
            "live_trading_allowed": False,
        }
        final = list(DiscussionOrchestrator(
            self.store,
            ConvergenceRegistry(ConvergenceProvider()),
            market_service=StaticStorageMarket(ready_snapshot),
        ).run_round("room_storage", "完成全部角色覆盖并验证数据门"))[-1]
        self.assertTrue(final["convergence"]["discussion_gate"]["ready"])
        self.assertTrue(final["convergence"]["can_host_finish"])
        risk_gate = final["convergence"]["candidate_risk_review_gate"]
        self.assertTrue(risk_gate["applicable"])
        self.assertEqual(risk_gate["version"], "candidate_risk_review_v1")
        self.assertTrue(risk_gate["ready"])
        self.assertEqual(risk_gate["candidate_count"], 2)
        self.assertEqual(risk_gate["reviewed_candidate_count"], 2)
        self.assertEqual(risk_gate["execution_capability"], "none")
        self.assertFalse(risk_gate["live_trading_allowed"])
        self.assertFalse(risk_gate["can_autonomously_decide"])

        unsafe_snapshot = copy.deepcopy(ready_snapshot)
        unsafe_snapshot["execution_capability"] = "orders"
        unsafe_snapshot["live_trading_allowed"] = True
        reevaluated = self.service.evaluate(
            "room_storage",
            runtime={"market_snapshot": unsafe_snapshot},
        )

        self.assertTrue(reevaluated["discussion_gate"]["ready"])
        self.assertFalse(reevaluated["data_gate"]["ready"])
        self.assertFalse(reevaluated["can_host_finish"])

    def test_storage_round_cannot_finish_without_exact_candidate_risk_reviews(self) -> None:
        self._use_text_only_storage_fixture()
        ready_snapshot = {
            "ok": True,
            "state": "ready",
            "source": "futu_opend",
            "snapshot_id": "missing-risk-review",
            "captured_at": "2026-07-20T20:00:00Z",
            "rows": [
                {
                    "symbol": symbol,
                    "last": 100 + index,
                    "quality": "ready",
                    **LIVE_QUOTE_FRESHNESS,
                    "market_time": "2026-07-20 15:59:00",
                }
                for index, symbol in enumerate(("US.MU", "US.SNDK", "US.WDC", "US.STX"))
            ],
            "missing_symbols": [],
            "source_errors": [],
            "evidence": ready_storage_research_evidence(),
            "execution_capability": "none",
            "live_trading_allowed": False,
        }
        final = list(DiscussionOrchestrator(
            self.store,
            ConvergenceRegistry(ConvergenceProvider(omit_risk_candidate_review=True)),
            market_service=StaticStorageMarket(ready_snapshot),
        ).run_round("room_storage", "验证缺少精确候选风险复核时必须阻断收敛"))[-1]

        risk_gate = final["convergence"]["candidate_risk_review_gate"]
        self.assertEqual(final["status"], "PARTIAL")
        self.assertTrue(risk_gate["applicable"])
        self.assertFalse(risk_gate["ready"])
        self.assertEqual(risk_gate["status"], "decision_missing")
        self.assertIn(
            "CANDIDATE_RISK_REVIEW_MISSING",
            [item["code"] for item in risk_gate["blockers"]],
        )
        self.assertEqual(risk_gate["focus"]["target_stances"], ["risk"])
        self.assertFalse(final["convergence"]["discussion_gate"]["ready"])
        self.assertFalse(final["convergence"]["can_host_finish"])

    def test_storage_research_quality_gate_blocks_degraded_stale_and_official_errors(self) -> None:
        self._use_text_only_storage_fixture()
        snapshot = {
            "ok": True,
            "state": "ready",
            "source": "futu_opend",
            "snapshot_id": "quality-gate-snapshot",
            "captured_at": "2026-07-31T21:21:41Z",
            "rows": [
                {
                    "symbol": symbol,
                    "last": 100 + index,
                    "quality": "ready",
                    **LIVE_QUOTE_FRESHNESS,
                    "age_seconds": 41,
                    "market_time": "2026-07-31 17:21:00",
                }
                for index, symbol in enumerate(("US.MU", "US.SNDK", "US.WDC", "US.STX"))
            ],
            "missing_symbols": [],
            "source_errors": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
            "evidence": ready_storage_research_evidence(
                captured_at="2026-07-31T21:21:41Z",
                technical_as_of="2026-01-22 00:00:00",
            ),
        }
        snapshot["evidence"]["state"] = "degraded"
        snapshot["evidence"]["official_filings"]["rows"][0]["filings"] = []
        snapshot["evidence"]["official_filings"]["source_errors"] = [{
            "code": "SEC_USER_AGENT_REQUIRED",
            "message": "fixture",
        }]

        provider = ConvergenceProvider()
        orchestrator = DiscussionOrchestrator(
            self.store,
            ConvergenceRegistry(provider),
            market_service=StaticStorageMarket(snapshot),
        )
        events = list(orchestrator.run_round("room_storage", "比较四股并严格审查证据质量"))
        final = events[-1]
        codes = {
            item["code"]
            for item in final["convergence"]["research_evidence_gate"]["blockers"]
        }

        self.assertEqual(final["type"], "round_completed")
        self.assertEqual(final["status"], "PARTIAL")
        self.assertTrue(final["convergence"]["discussion_gate"]["ready"])
        self.assertFalse(final["convergence"]["can_host_finish"])
        self.assertFalse(final["convergence"]["data_gate"]["ready"])
        self.assertEqual(
            final["convergence"]["decision_status"],
            "RESEARCH_EVIDENCE_REPAIR_REQUIRED",
        )
        self.assertIn("STORAGE_TECHNICAL_EVIDENCE_STALE", codes)
        self.assertIn("STORAGE_OFFICIAL_FILINGS_MISSING", codes)
        self.assertIn("STORAGE_OFFICIAL_FILINGS_SOURCE_ERROR", codes)
        self.assertIn("STORAGE_RESEARCH_EVIDENCE_DEGRADED", codes)
        focus = final["convergence"]["research_evidence_gate"]["focus"]
        self.assertEqual(focus["code"], "STORAGE_TECHNICAL_EVIDENCE_STALE")
        self.assertEqual(focus["repair_scope"], "next_round_only")
        self.assertEqual(
            final["convergence"]["research_evidence_gate"]["repair_scope"],
            "next_round_only",
        )
        self.assertTrue(all(
            blocker.get("repair_scope") == "next_round_only"
            for blocker in final["convergence"]["research_evidence_gate"]["blockers"]
        ))
        self.assertEqual(
            set(focus["target_stances"]),
            {"data_guardian", "technical"},
        )
        decisions = self.store.list_director_decisions(
            "room_storage",
            round_id=final["round_id"],
        )
        focused = [
            item for item in decisions
            if (item.get("workspace_focus") or {}).get("code")
            == "STORAGE_TECHNICAL_EVIDENCE_STALE"
        ]
        self.assertTrue(focused)
        self.assertTrue(any(
            item.get("member_id")
            in {
                member["id"]
                for member in self.store.room_snapshot("room_storage")["members"]
                if member.get("stance") in {"data_guardian", "technical"}
            }
            for item in focused
        ))
        partial_finishes = [
            item for item in decisions
            if item.get("action") == "finish"
            and item.get("source") == "partial_unrepairable"
        ]
        self.assertEqual(len(partial_finishes), 1)
        scheduling = partial_finishes[0]["moderator_context"]["scheduling_context"]
        self.assertEqual(scheduling["finish_mode"], "partial_unrepairable")
        self.assertEqual(
            scheduling["workspace_focus_repair_scope"],
            "next_round_only",
        )
        self.assertTrue(scheduling["unrepairable_focus_explained"])
        self.assertTrue(scheduling["hard_coverage_ready"])
        self.assertTrue(partial_finishes[0]["decision_sha256"])

    def test_generic_room_ignores_storage_research_evidence_contract(self) -> None:
        state = self.service.evaluate(
            "room_plan",
            runtime={
                "market_snapshot": {
                    "evidence": {"state": "degraded"},
                    "execution_capability": "none",
                    "live_trading_allowed": False,
                },
            },
        )

        self.assertTrue(state["research_evidence_gate"]["ready"])
        self.assertFalse(state["research_evidence_gate"]["applicable"])
        self.assertNotIn(
            "STORAGE_RESEARCH_EVIDENCE_DEGRADED",
            [item["code"] for item in state["blockers"]],
        )

    def test_confirmed_current_evidence_allows_candidate_not_autonomous_decision(self) -> None:
        provider = ConvergenceProvider()
        orchestrator = DiscussionOrchestrator(self.store, ConvergenceRegistry(provider), market_service=None)
        final = list(orchestrator.run_round("room_plan", "形成证据充分的候选方案"))[-1]
        round_id = final["round_id"]
        message = next(
            item for item in reversed(self.store.room_snapshot("room_plan")["messages"])
            if item.get("round_id") == round_id and item.get("sender_type") == "ai"
        )
        evidence = [{
            "type": "message",
            "id": message["id"],
            "evidence_role": "support",
            "verification_status": "source_checked",
        }]
        artifact = self.store.create_artifact(
            "room_plan",
            round_id=round_id,
            title="已核验候选方案",
            content={
                "summary": "候选方案摘要",
                "summary_evidence": evidence,
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
                "decision": {
                    "status": "candidate",
                    "options": [
                        {
                            "id": "option_a",
                            "title": "先验证",
                            "description": "先完成可逆验证。",
                            "evidence": evidence,
                        },
                        {
                            "id": "option_b",
                            "title": "直接扩展",
                            "description": "立即扩大范围。",
                            "evidence": evidence,
                        },
                    ],
                    "preferred_option_id": "option_a",
                    "rationale": "现有证据更支持可逆路径。",
                    "evidence": evidence,
                },
            },
        )
        confirmed = self.store.confirm_artifact(
            "room_plan", artifact["id"], expected_version=artifact["version"], confirmed_by="user",
        )

        state = self.service.evaluate("room_plan", round_id=round_id)

        self.assertEqual(confirmed["status"], "CONFIRMED")
        self.assertTrue(state["evidence_gate"]["ready"])
        self.assertTrue(state["can_present_candidate_best"])
        self.assertEqual(state["decision_status"], "READY_FOR_USER_DECISION")
        self.assertFalse(state["can_autonomously_decide"])
        self.assertFalse(state["user_decision_gate"]["ready"])
        self.assertTrue(state["user_confirmation_required"])

        supported = self.store.create_artifact_user_decision(
            "room_plan",
            confirmed["id"],
            expected_version=confirmed["version"],
            action="support",
            rationale="支持当前候选，并保留失效条件。",
            selected_option_id="option_a",
        )
        supported_state = self.service.evaluate("room_plan", round_id=round_id)
        self.assertEqual(supported_state["decision_status"], "USER_SUPPORTED")
        self.assertEqual(supported_state["user_decision_gate"]["decision_id"], supported["id"])
        self.assertFalse(supported_state["user_confirmation_required"])
        self.assertFalse(supported_state["can_autonomously_decide"])

        self.store.create_artifact_user_decision(
            "room_plan",
            confirmed["id"],
            expected_version=confirmed["version"],
            action="hold",
            rationale="等待新增证据后再判断。",
        )
        held_state = self.service.evaluate("room_plan", round_id=round_id)
        self.assertEqual(held_state["decision_status"], "USER_HELD")

        self.store.create_artifact_user_decision(
            "room_plan",
            confirmed["id"],
            expected_version=confirmed["version"],
            action="return",
            rationale="退回补充资源约束。",
        )
        returned_state = self.service.evaluate("room_plan", round_id=round_id)
        self.assertEqual(returned_state["decision_status"], "RETURNED_FOR_REVISION")
        self.assertIn("退回", returned_state["next_actions"][0])

    def test_storage_decision_gate_requires_two_options_preferred_choice_and_reason(self) -> None:
        missing = self.service._decision_gate(
            {"content": {"decision": {"status": "candidate", "options": []}}},
            is_storage=True,
        )
        complete = self.service._decision_gate(
            {
                "content": {
                    "decision": {
                        "status": "candidate",
                        "options": [
                            {"id": "option_a", "title": "方案 A"},
                            {"id": "option_b", "title": "方案 B"},
                        ],
                        "preferred_option_id": "option_a",
                        "rationale": "方案 A 的证据覆盖更完整，同时保留方案 B 作为反证情景。",
                    },
                },
            },
            is_storage=True,
        )
        generic = self.service._decision_gate(None, is_storage=False)
        project = self.service._decision_gate(
            complete_artifact := {
                "content": {
                    "decision": {
                        "status": "candidate",
                        "options": [{"id": "a"}, {"id": "b"}],
                        "preferred_option_id": "a",
                        "rationale": "先选择更可逆的验证路径。",
                    },
                },
            },
            is_storage=False,
            is_project=True,
        )

        self.assertFalse(missing["ready"])
        self.assertIn("DECISION_OPTIONS_INSUFFICIENT", [item["code"] for item in missing["blockers"]])
        self.assertIn("DECISION_RATIONALE_MISSING", [item["code"] for item in missing["blockers"]])
        self.assertTrue(complete["ready"])
        self.assertEqual(complete["option_count"], 2)
        self.assertTrue(generic["ready"])
        self.assertFalse(generic["applicable"])
        self.assertTrue(project["ready"], complete_artifact)
        self.assertTrue(project["applicable"])

    def test_storage_counter_evidence_must_use_qualified_counter_message(self) -> None:
        artifact = {
            "id": "artifact_counter_gate",
            "status": "CONFIRMED",
            "version": 1,
            "content": {
                "summary": "候选方案已经记录支持证据。",
                "summary_evidence": [{
                    "type": "message",
                    "id": "msg_bear",
                    "evidence_role": "support",
                    "verification_status": "source_checked",
                }],
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
                "risks": [],
            },
        }

        missing = self.service._evidence_gate(
            artifact,
            required_counter_message_ids={"msg_bear"},
        )
        artifact["content"]["summary_evidence"][0]["evidence_role"] = "counter"
        qualified = self.service._evidence_gate(
            artifact,
            required_counter_message_ids={"msg_bear"},
        )
        cross_round = self.service._evidence_gate(
            artifact,
            required_counter_message_ids={"msg_other_round"},
        )

        self.assertFalse(missing["ready"])
        self.assertFalse(missing["counter_evidence_ready"])
        self.assertIn(
            "COUNTER_EVIDENCE_MISSING",
            [item["code"] for item in missing["blockers"]],
        )
        self.assertTrue(qualified["ready"])
        self.assertEqual(qualified["qualified_counter_evidence_count"], 1)
        self.assertFalse(cross_round["ready"])
        self.assertEqual(cross_round["qualified_counter_evidence_count"], 0)

    def test_open_blocking_project_risk_prevents_candidate_presentation(self) -> None:
        reviewed = {
            "type": "message",
            "id": "msg_evidence",
            "verification_status": "source_checked",
            "evidence_role": "support",
            "version_status": "current",
            "version_decision": "current",
        }
        artifact = {
            "id": "artifact_project_risk",
            "status": "CONFIRMED",
            "version": 2,
            "content": {
                "summary": "候选方案仍有一项阻断风险。",
                "summary_evidence": [reviewed],
                "requirements": [],
                "risks": [{
                    "id": "risk_resource",
                    "text": "关键资源尚未落实。",
                    "status": "open",
                    "blocking": True,
                    "evidence": [reviewed],
                }],
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
            },
        }

        blocked = self.service._evidence_gate(artifact)
        artifact["content"]["risks"][0].update({
            "status": "accepted",
            "blocking": False,
        })
        accepted = self.service._evidence_gate(artifact)

        self.assertFalse(blocked["ready"])
        self.assertEqual(blocked["risk_count"], 1)
        self.assertEqual(blocked["unresolved_risk_count"], 1)
        self.assertIn("PROJECT_RISK_OPEN", [item["code"] for item in blocked["blockers"]])
        self.assertTrue(accepted["ready"])
        self.assertEqual(accepted["unresolved_risk_count"], 0)

    def test_open_blocking_disagreement_prevents_candidate_until_user_handles_it(self) -> None:
        provider = ConvergenceProvider()
        orchestrator = DiscussionOrchestrator(self.store, ConvergenceRegistry(provider), market_service=None)
        final = list(orchestrator.run_round("room_plan", "保留并处理关键分歧"))[-1]
        round_id = final["round_id"]
        message = next(
            item for item in reversed(self.store.room_snapshot("room_plan")["messages"])
            if item.get("round_id") == round_id and item.get("sender_type") == "ai"
        )
        evidence = [{
            "type": "message",
            "id": message["id"],
            "evidence_role": "support",
            "verification_status": "source_checked",
        }]
        artifact = self.store.create_artifact(
            "room_plan",
            round_id=round_id,
            title="含开放分歧的候选方案",
            content={
                "summary": "讨论形成候选方案，但有一项关键分歧。",
                "summary_evidence": evidence,
                "conclusions": [],
                "disagreements": [{
                    "text": "是否现在扩大验证范围",
                    "positions": ["先验证", "立即扩大"],
                    "status": "open",
                    "blocking": True,
                    "owner": "用户",
                    "resolution": "",
                    "evidence": evidence,
                }],
                "unknowns": [],
                "actions": [],
            },
        )
        confirmed = self.store.confirm_artifact(
            "room_plan",
            artifact["id"],
            expected_version=artifact["version"],
            confirmed_by="user",
        )

        blocked = self.service.evaluate("room_plan", round_id=round_id)

        self.assertEqual(confirmed["status"], "CONFIRMED")
        self.assertFalse(blocked["evidence_gate"]["ready"])
        self.assertEqual(blocked["evidence_gate"]["disagreement_count"], 1)
        self.assertEqual(blocked["evidence_gate"]["unresolved_disagreement_count"], 1)
        self.assertIn(
            "DISAGREEMENT_OPEN",
            [item["code"] for item in blocked["evidence_gate"]["blockers"]],
        )
        self.assertFalse(blocked["can_present_candidate_best"])

        revised = self.store.update_artifact("room_plan", artifact["id"], {
            "expected_version": confirmed["version"],
            "content": {
                **confirmed["content"],
                "disagreements": [{
                    **confirmed["content"]["disagreements"][0],
                    "status": "accepted_risk",
                    "resolution": "用户接受剩余风险，先执行小样本验证并保留停止条件。",
                }],
            },
        })
        rereviewed_content = copy.deepcopy(revised["content"])
        rereviewed_content["disagreements"][0]["evidence"][0].update({
            "verification_status": "source_checked",
            "review_note": "已按接受风险后的决议重新核对原始发言。",
        })
        revised = self.store.update_artifact("room_plan", artifact["id"], {
            "expected_version": revised["version"],
            "content": rereviewed_content,
        })
        reconfirmed = self.store.confirm_artifact(
            "room_plan",
            artifact["id"],
            expected_version=revised["version"],
            confirmed_by="user",
        )
        ready = self.service.evaluate("room_plan", round_id=round_id)

        self.assertEqual(reconfirmed["status"], "CONFIRMED")
        self.assertTrue(ready["evidence_gate"]["ready"])
        self.assertEqual(ready["evidence_gate"]["unresolved_disagreement_count"], 0)
        self.assertTrue(ready["can_present_candidate_best"])

    def test_statistical_claim_stays_locked_below_twenty_samples(self) -> None:
        snapshot = self.store.room_snapshot("room_storage")
        snapshot["observation_scorecard"] = {
            "overall": {"sample_count": 20, "qualified": True},
        }
        unsupported = self.service.evaluate("room_storage", snapshot=snapshot)
        snapshot["observation_scorecard"] = {
            "version": OBSERVATION_SCORECARD_VERSION,
            "overall": {"sample_count": 19, "qualified": False},
        }
        below = self.service.evaluate("room_storage", snapshot=snapshot)
        snapshot["observation_scorecard"] = {
            "version": OBSERVATION_SCORECARD_VERSION,
            "overall": {"sample_count": 20, "qualified": True},
        }
        qualified = self.service.evaluate("room_storage", snapshot=snapshot)

        self.assertFalse(unsupported["simulation_gate"]["statistical_claim_allowed"])
        self.assertEqual(unsupported["simulation_gate"]["status"], "scorecard_version_unsupported")
        self.assertFalse(below["simulation_gate"]["statistical_claim_allowed"])
        self.assertEqual(below["simulation_gate"]["status"], "sample_insufficient")
        self.assertTrue(qualified["simulation_gate"]["statistical_claim_allowed"])
        self.assertEqual(qualified["simulation_gate"]["status"], "qualified_for_statistical_review")

    def test_unlinked_paper_portfolio_never_satisfies_current_decision_gate(self) -> None:
        plan = default_paper_portfolio_plan()
        plan["positions"][0].update({"side": "LONG", "weight_pct": 25})
        evaluation = {
            "version": "paper_portfolio_risk_v1",
            "state": "ready",
            "risk_gate": {"status": "PASS", "ready": True, "blockers": []},
            "execution_capability": "none",
            "live_trading_allowed": False,
        }
        portfolio = self.store.create_paper_portfolio(
            "room_storage",
            plan,
            evaluation,
        )

        draft_state = self.service.evaluate("room_storage")
        self.assertFalse(draft_state["portfolio_gate"]["applicable"])
        self.assertTrue(draft_state["portfolio_gate"]["ready"])
        self.assertEqual(draft_state["portfolio_gate"]["legacy_unlinked_count"], 1)

        self.store.confirm_paper_portfolio(
            "room_storage",
            portfolio["id"],
            expected_version=portfolio["version"],
        )
        confirmed_state = self.service.evaluate("room_storage")
        self.assertFalse(confirmed_state["portfolio_gate"]["applicable"])
        self.assertTrue(confirmed_state["portfolio_gate"]["ready"])
        self.assertEqual(confirmed_state["portfolio_gate"]["confirmed_count"], 0)
        self.assertEqual(confirmed_state["portfolio_gate"]["legacy_unlinked_count"], 1)

    def test_supported_candidate_requires_exact_linked_confirmed_portfolio(self) -> None:
        message = self.store.add_message(
            "room_storage",
            sender_type="user",
            sender_name="User",
            content="只建立可逆、只读的模拟组合。",
        )
        evidence = [{
            "type": "message",
            "id": message["id"],
            "evidence_role": "support",
            "verification_status": "source_checked",
        }]
        artifact = self.store.create_artifact(
            "room_storage",
            title="存储行业候选方案",
            content={
                "summary": "比较两种模拟研究路径。",
                "summary_evidence": evidence,
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
                "decision": {
                    "status": "candidate",
                    "options": [
                        {"id": "small", "title": "小规模模拟", "description": "低权重验证。", "evidence": evidence},
                        {"id": "broad", "title": "广覆盖模拟", "description": "扩大覆盖。", "evidence": evidence},
                    ],
                    "preferred_option_id": "small",
                    "rationale": "小规模路径更可逆。",
                    "evidence": evidence,
                },
            },
        )
        confirmed = self.store.confirm_artifact(
            "room_storage",
            artifact["id"],
            expected_version=artifact["version"],
            confirmed_by="user",
        )
        decision = self.store.create_artifact_user_decision(
            "room_storage",
            confirmed["id"],
            expected_version=confirmed["version"],
            action="support",
            rationale="支持精确候选版本并先做模拟验证。",
            selected_option_id="small",
        )

        missing = self.service.evaluate("room_storage")
        self.assertTrue(missing["portfolio_gate"]["applicable"])
        self.assertFalse(missing["portfolio_gate"]["ready"])
        self.assertIn(
            "DECISION_PACKAGE_PORTFOLIO_MISSING",
            [item["code"] for item in missing["portfolio_gate"]["blockers"]],
        )

        plan = default_paper_portfolio_plan()
        plan["positions"][0].update({"side": "LONG", "weight_pct": 25})
        evaluation = {
            "version": "paper_portfolio_risk_v1",
            "state": "ready",
            "risk_gate": {"status": "PASS", "ready": True, "blockers": []},
            "execution_capability": "none",
            "live_trading_allowed": False,
        }
        portfolio = self.store.create_paper_portfolio(
            "room_storage",
            plan,
            evaluation,
            user_decision_id=decision["id"],
            derivation_note="实现用户支持的精确候选版本。",
        )
        linked_draft = self.service.evaluate("room_storage")
        self.assertFalse(linked_draft["portfolio_gate"]["ready"])
        self.assertIn(
            "DECISION_PACKAGE_PORTFOLIO_NOT_CONFIRMED",
            [item["code"] for item in linked_draft["portfolio_gate"]["blockers"]],
        )

        self.store.confirm_paper_portfolio(
            "room_storage",
            portfolio["id"],
            expected_version=portfolio["version"],
        )
        ready = self.service.evaluate("room_storage")
        self.assertTrue(ready["portfolio_gate"]["ready"])
        self.assertEqual(ready["portfolio_gate"]["confirmed_count"], 1)
        self.assertFalse(ready["can_autonomously_decide"])


if __name__ == "__main__":
    unittest.main()
