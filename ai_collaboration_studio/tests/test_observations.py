from __future__ import annotations

import copy
import tempfile
import threading
import unittest
import json
import sqlite3
from contextlib import closing
from datetime import datetime, time, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from backend import http_server
from backend.convergence import ConvergenceService
from backend.market.futu_readonly import STORAGE_SYMBOLS
from backend.observation_service import ObservationService
from backend.paper_portfolio import default_paper_portfolio_plan
from backend.storage_sample_acceptance import StorageSampleAcceptance
from backend.store import StudioStore


NOW = datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc)
SNAPSHOT_CAPTURED_AT = "2026-07-01T20:00:00Z"
SNAPSHOT_UPDATED_AT = "2026-07-01T19:59:30Z"
SNAPSHOT_MARKET_TIME = "2026-07-01 15:59:30"
SNAPSHOT_PRICES = {
    "US.MU": 100.0,
    "US.SNDK": 50.0,
    "US.WDC": 200.0,
    "US.STX": 25.0,
}


def ready_observation_row(symbol: str, **overrides) -> dict:
    row = {
        "symbol": symbol,
        "last": SNAPSHOT_PRICES[symbol],
        "quality": "ready",
        "research_ready": True,
        "suspended": False,
        "security_status": "NORMAL",
        "market_time": SNAPSHOT_MARKET_TIME,
        "updated_at": SNAPSHOT_UPDATED_AT,
        "age_seconds": 30,
        "quote_is_live": True,
        "market_state": None,
        "freshness_basis": "live_20m_window",
    }
    row.update(overrides)
    return row


def ready_observation_snapshot(rows: list[dict] | None = None) -> dict:
    normalized_rows = (
        [ready_observation_row(symbol) for symbol in STORAGE_SYMBOLS]
        if rows is None
        else [
            ready_observation_row(
                str(row.get("symbol") or ""),
                **{key: value for key, value in row.items() if key != "symbol"},
            )
            for row in rows
        ]
    )
    return {
        "ok": True,
        "state": "ready",
        "source": "futu_opend",
        "snapshot_id": "futu-real-snapshot",
        "captured_at": SNAPSHOT_CAPTURED_AT,
        "symbols": list(STORAGE_SYMBOLS),
        "rows": normalized_rows,
        "missing_symbols": [],
        "source_errors": [],
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


def readonly_qfq_history(symbol: str, rows: list[dict], **overrides) -> dict:
    normalized_rows: list[dict] = []
    for raw_row in rows:
        market_time = str(raw_row.get("market_time") or "")
        parsed = datetime.fromisoformat(market_time)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("America/New_York"))
        close = float(raw_row["close"])
        normalized_rows.append({
            "symbol": symbol,
            "market_time": market_time,
            "time": parsed.astimezone(timezone.utc).isoformat(),
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 0.0,
            "turnover": 0.0,
        })
    sessions = [str(row["market_time"])[:10] for row in normalized_rows]
    payload = {
        "ok": bool(normalized_rows),
        "source": "futu_opend",
        "interval": "1d",
        "price_adjustment": "QFQ",
        "captured_at": NOW.isoformat().replace("+00:00", "Z"),
        "as_of_date": NOW.astimezone(ZoneInfo("America/New_York")).date().isoformat(),
        "last_completed_session": sessions[-1] if sessions else "",
        "actual_start": sessions[0] if sessions else "",
        "actual_end": sessions[-1] if sessions else "",
        "symbol": symbol,
        "rows": normalized_rows,
        "source_errors": [] if normalized_rows else [{"code": "OFFLINE"}],
        "execution_capability": "none",
        "live_trading_allowed": False,
    }
    payload.update(overrides)
    return payload


class FakeObservationMarket:
    def __init__(
        self,
        *,
        online: bool = True,
        history_rows: list[dict] | None = None,
        snapshot_rows: list[dict] | None = None,
        snapshot_payload: dict | None = None,
        history_by_symbol: dict[str, list[dict]] | None = None,
        history_payload_by_symbol: dict[str, dict] | None = None,
    ) -> None:
        self.online = online
        self.history_rows = history_rows or []
        self.snapshot_rows = snapshot_rows
        self.snapshot_payload = copy.deepcopy(snapshot_payload)
        self.history_by_symbol = history_by_symbol or {}
        self.history_payload_by_symbol = history_payload_by_symbol or {}
        self.snapshot_calls = 0
        self.history_calls: list[dict] = []

    def snapshot(self, *, force: bool = False) -> dict:
        self.snapshot_calls += 1
        if not self.online:
            return {"ok": False, "rows": [], "source_errors": [{"code": "OFFLINE"}]}
        if self.snapshot_payload is not None:
            return copy.deepcopy(self.snapshot_payload)
        return ready_observation_snapshot(self.snapshot_rows)

    def history(self, symbol: str, **kwargs) -> dict:
        self.history_calls.append({"symbol": symbol, **kwargs})
        if symbol in self.history_payload_by_symbol:
            return copy.deepcopy(self.history_payload_by_symbol[symbol])
        rows = (
            self.history_by_symbol[symbol]
            if symbol in self.history_by_symbol
            else self.history_rows
            if symbol == "US.MU"
            else []
        )
        return readonly_qfq_history(
            symbol,
            rows,
            start=kwargs.get("start"),
            end=kwargs.get("end"),
            limit=kwargs.get("limit"),
        )


def observation_payload(**overrides) -> dict:
    payload = {
        "symbol": "MU",
        "direction": "UP",
        "horizon_days": 5,
        "threshold_pct": 2,
        "thesis": "若需求和价格保持强势，五个交易日后收盘涨幅应至少达到 2%。",
        "counter_case": "现货价格快速回落或公司指引下修。",
        "model_confidence": 70,
        "evidence": {},
    }
    payload.update(overrides)
    return payload


def resolve_qfq_observation(
    store: StudioStore,
    room_id: str,
    observation_id: str,
    **kwargs,
) -> dict | None:
    observation = store.get_observation(room_id, observation_id)
    if not observation:
        raise AssertionError("observation fixture is missing")
    return store.resolve_observation(
        room_id,
        observation_id,
        measurement_method="qfq_close_to_close_v2",
        scoring_baseline_price=float(observation["baseline_price"]),
        scoring_baseline_time=str(observation["baseline_time"]),
        **kwargs,
    )


def create_scorecard_lineage_fixture(
    store: StudioStore,
    *,
    round_id: str = "",
) -> dict[str, Any]:
    if round_id:
        snapshot = store.room_snapshot("room_storage")
        members = snapshot["members"]
        messages = [
            store.add_message(
                "room_storage",
                sender_type="ai",
                sender_id=member["id"],
                sender_name=member["name"],
                identity=member["identity"],
                provider=member["provider"],
                model=member["model"],
                content=f"{member['name']} completed the frozen paper-only research role.",
                round_id=round_id,
                member_version=member["version"],
            )
            for member in members
        ]
        shared_context, manifest = store.material_prompt_bundle("room_storage")
        manifest = store.finalize_round_evidence_manifest(
            manifest,
            shared_context=shared_context,
            market_snapshot=None,
        )
        room = snapshot["room"]
        store.save_round_checkpoint(
            "room_storage",
            round_id,
            {
                "member_ids": [member["id"] for member in members],
                "spoken_counts": {member["id"]: 1 for member in members},
                "spoken_stances": [member["stance"] for member in members],
                "successful_member_ids": [member["id"] for member in members],
                "failed_member_ids": [],
                "previous_name": members[-1]["name"],
                "completed": len(members),
                "failures": 0,
                "skipped": 0,
                "proposals_created": 0,
                "next_order": len(members) + 1,
                "max_turns": len(members),
                "workflow_policy": room["workflow_policy"],
                "capability_pack_ids": room.get("capability_pack_ids") or [],
                "shared_context": shared_context,
                "market_snapshot": None,
                "frozen_market": None,
                "round_evidence_manifest": manifest,
                "project_workspace": None,
                "skip_provider_ids": [],
            },
        )
        store.complete_round(round_id, "COMPLETED")
        message = messages[0]
    else:
        message = store.add_message(
            "room_storage",
            sender_type="user",
            sender_name="User",
            content="Keep the selected candidate paper-only and verify its outcomes.",
        )
    evidence = [{
        "type": "message",
        "id": message["id"],
        "evidence_role": "support",
        "verification_status": "source_checked",
        "review_note": "",
    }]
    artifact = store.create_artifact(
        "room_storage",
        title="Observation lineage candidate",
        content={
            "summary": "Compare reversible paper-only candidates.",
            "summary_evidence": evidence,
            "requirements": [],
            "risks": [],
            "conclusions": [],
            "disagreements": [],
            "unknowns": [],
            "actions": [],
            "decision": {
                "status": "candidate",
                "options": [
                    {
                        "id": "paper_small",
                        "title": "Small paper candidate",
                        "description": "A reversible candidate used only for validation.",
                        "benefits": ["Reversible"],
                        "risks": ["Limited sample"],
                        "evidence": evidence,
                    },
                    {
                        "id": "paper_broad",
                        "title": "Broad paper candidate",
                        "description": "A broader paper-only comparison candidate.",
                        "benefits": ["Broader coverage"],
                        "risks": ["More variables"],
                        "evidence": evidence,
                    },
                ],
                "preferred_option_id": "paper_small",
                "rationale": "Use the smallest reversible validation surface.",
                "evidence": evidence,
            },
        },
        round_id=round_id,
        created_by="user",
    )
    artifact = store.confirm_artifact(
        "room_storage",
        artifact["id"],
        expected_version=artifact["version"],
        confirmed_by="user",
    )
    decision = store.create_artifact_user_decision(
        "room_storage",
        artifact["id"],
        expected_version=artifact["version"],
        action="support",
        rationale="Support this exact candidate only for paper validation.",
        selected_option_id="paper_small",
    )
    plan = default_paper_portfolio_plan()
    plan["name"] = "Scorecard lineage portfolio"
    plan["positions"][0].update({
        "side": "LONG",
        "weight_pct": 20,
        "thesis": "Paper-only validation hypothesis.",
        "invalidation": "Return to the user when the condition fails.",
    })
    evaluation = {
        "version": "paper_portfolio_risk_v1",
        "state": "ready",
        "evaluated_at": "2026-01-01T00:00:00Z",
        "input_fingerprint": "scorecard-lineage-fixture",
        "exposures": {"gross_exposure_pct": 20, "net_exposure_pct": 20},
        "metrics": {"annualized_volatility_pct": 10, "max_drawdown_pct": 5},
        "stress_results": [],
        "risk_gate": {"status": "PASS", "ready": True, "blockers": []},
        "execution_capability": "none",
        "live_trading_allowed": False,
    }
    portfolio = store.create_paper_portfolio(
        "room_storage",
        plan,
        evaluation,
        created_by="user",
        user_decision_id=decision["id"],
        derivation_note="Implements the exact supported paper candidate.",
    )
    portfolio = store.confirm_paper_portfolio(
        "room_storage",
        portfolio["id"],
        expected_version=portfolio["version"],
        confirmed_by="user",
    )
    return {
        "artifact": artifact,
        "decision": decision,
        "portfolio": portfolio,
        "round_id": round_id,
    }


def linked_observation_payload(lineage: dict[str, Any], **overrides) -> dict:
    payload = observation_payload(
        user_decision_id=lineage["decision"]["id"],
        source_portfolio_id=lineage["portfolio"]["id"],
        source_portfolio_version=lineage["portfolio"]["version"],
        derivation_note="Validate this exact confirmed portfolio version prospectively.",
    )
    payload.update(overrides)
    return payload


class ObservationServiceTests(unittest.TestCase):
    def make_store(self, temp_dir: str) -> StudioStore:
        return StudioStore(Path(temp_dir) / "studio.sqlite3")

    def test_proposal_requires_user_confirmation_before_real_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            market = FakeObservationMarket()
            service = ObservationService(store, market, clock=lambda: NOW)

            proposed = service.create("room_storage", observation_payload())
            self.assertEqual(proposed["status"], "PROPOSED")
            self.assertFalse(proposed["user_confirmed"])
            self.assertIsNone(proposed["baseline_price"])
            self.assertEqual(market.snapshot_calls, 0)

            confirmed = service.confirm("room_storage", proposed["id"])
            self.assertEqual(confirmed["status"], "OPEN")
            self.assertTrue(confirmed["user_confirmed"])
            self.assertEqual(confirmed["baseline_price"], 100.0)
            self.assertEqual(confirmed["baseline_snapshot_id"], "futu-real-snapshot")
            self.assertEqual(market.snapshot_calls, 1)

    def test_ai_proposal_can_bind_exact_same_round_decision_lineage_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            market = FakeObservationMarket()
            service = ObservationService(store, market, clock=lambda: NOW)
            research_round = store.create_round(
                "room_storage",
                "Evaluate one paper-only storage research candidate.",
            )
            lineage = create_scorecard_lineage_fixture(
                store,
                round_id=research_round["id"],
            )
            ai_member = store.room_snapshot("room_storage")["members"][0]
            proposal = store.create_observation(
                "room_storage",
                observation_payload(
                    created_by=ai_member["id"],
                    round_id=research_round["id"],
                    methodology_id="agent_same_round_test",
                    methodology_version=3,
                    model_confidence=83,
                ),
            )
            preserved = {
                key: proposal[key]
                for key in (
                    "created_by",
                    "member_version",
                    "methodology_id",
                    "methodology_version",
                    "confidence_source",
                    "model_confidence",
                )
            }
            binding_payload = {
                "user_decision_id": lineage["decision"]["id"],
                "source_portfolio_id": lineage["portfolio"]["id"],
                "source_portfolio_version": lineage["portfolio"]["version"],
                "derivation_note": "User accepts this AI proposal as the exact prospective test.",
            }

            first = service.bind_decision_lineage(
                "room_storage",
                proposal["id"],
                binding_payload,
            )
            bound = first["observation"]
            event = first["decision_lineage"]["event"]

            self.assertEqual(bound["status"], "PROPOSED")
            self.assertFalse(bound["user_confirmed"])
            self.assertEqual(bound["round_id"], research_round["id"])
            self.assertEqual(bound["artifact_id"], lineage["artifact"]["id"])
            self.assertEqual(bound["artifact_version"], lineage["artifact"]["version"])
            self.assertEqual(
                {key: bound[key] for key in preserved},
                preserved,
            )
            self.assertEqual(event["relation_type"], "tests")
            self.assertEqual(event["resource_state"], "PROPOSED")
            self.assertEqual(event["resource_snapshot"]["created_by"], ai_member["id"])
            self.assertEqual(event["created_by"], "user")
            self.assertEqual(
                first["decision_lineage"]["artifact_round_id"],
                research_round["id"],
            )
            package = next(
                item
                for item in store.list_decision_packages("room_storage")
                if item["package_id"] == lineage["decision"]["id"]
            )
            self.assertEqual(
                package["anchor"]["artifact_round_id"],
                research_round["id"],
            )

            second = service.bind_decision_lineage(
                "room_storage",
                proposal["id"],
                binding_payload,
            )
            self.assertEqual(
                second["decision_lineage"]["event"]["id"],
                event["id"],
            )
            tests_events = [
                item
                for item in store.list_decision_packages("room_storage")[0]["lineage"]
                if item["relation_type"] == "tests"
                and item["resource_id"] == proposal["id"]
            ]
            self.assertEqual(len(tests_events), 1)
            store.confirm_observation(
                "room_storage",
                proposal["id"],
                {
                    "price": 100,
                    "time": "2026-07-01 16:00:00",
                    "snapshot_id": "bound-agent-baseline",
                },
            )
            resolved = resolve_qfq_observation(store,
                "room_storage",
                proposal["id"],
                outcome_price=104,
                outcome_time="2026-07-02 16:00:00",
                return_pct=4,
                hit=True,
            )
            scorecard = store.observation_scorecard("room_storage")
            agent_methodology = scorecard["by_agent_methodology"][0]

            self.assertEqual(resolved["model_confidence"], 83)
            self.assertEqual(scorecard["lineage_grouping"]["linked_observation_count"], 1)
            self.assertEqual(scorecard["overall"]["sample_count"], 1)
            self.assertEqual(scorecard["overall"]["minimum_samples"], 20)
            self.assertFalse(scorecard["overall"]["qualified"])
            self.assertEqual(agent_methodology["member_id"], ai_member["id"])
            self.assertEqual(agent_methodology["member_version"], proposal["member_version"])
            self.assertEqual(agent_methodology["methodology_id"], "agent_same_round_test")
            self.assertEqual(agent_methodology["confidence_sample_count"], 1)
            self.assertAlmostEqual(agent_methodology["brier_score"], 0.0289)
            self.assertEqual(market.snapshot_calls, 0)
            self.assertEqual(market.history_calls, [])

    def test_same_binding_retry_remains_idempotent_after_support_decision_becomes_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            research_round = store.create_round(
                "room_storage",
                "Keep an existing AI validation auditable after a later user decision.",
            )
            lineage = create_scorecard_lineage_fixture(
                store,
                round_id=research_round["id"],
            )
            ai_member = store.room_snapshot("room_storage")["members"][0]
            proposal = store.create_observation(
                "room_storage",
                observation_payload(
                    created_by=ai_member["id"],
                    round_id=research_round["id"],
                    methodology_id="stale_retry_test",
                    model_confidence=76,
                ),
            )
            binding_payload = {
                "user_decision_id": lineage["decision"]["id"],
                "source_portfolio_id": lineage["portfolio"]["id"],
                "source_portfolio_version": lineage["portfolio"]["version"],
                "derivation_note": "Bind this exact AI proposal before the user changes direction.",
            }
            service = ObservationService(store, FakeObservationMarket(), clock=lambda: NOW)
            first = service.bind_decision_lineage(
                "room_storage",
                proposal["id"],
                binding_payload,
            )
            later_decision = store.create_artifact_user_decision(
                "room_storage",
                lineage["artifact"]["id"],
                expected_version=lineage["artifact"]["version"],
                action="hold",
                rationale="The user now holds; the existing prospective test remains historical.",
            )

            retried = service.bind_decision_lineage(
                "room_storage",
                proposal["id"],
                binding_payload,
            )

            self.assertEqual(
                retried["decision_lineage"]["event"]["id"],
                first["decision_lineage"]["event"]["id"],
            )
            support_package = next(
                package
                for package in store.list_decision_packages("room_storage")
                if package["package_id"] == lineage["decision"]["id"]
            )
            tests_events = [
                event
                for event in support_package["lineage"]
                if event["relation_type"] == "tests"
                and event["resource_id"] == proposal["id"]
            ]
            self.assertEqual(support_package["state"], "stale")
            self.assertEqual(len(tests_events), 1)
            with self.assertRaisesRegex(ValueError, "另一用户决定"):
                service.bind_decision_lineage(
                    "room_storage",
                    proposal["id"],
                    {**binding_payload, "user_decision_id": later_decision["id"]},
                )

            store.confirm_observation(
                "room_storage",
                proposal["id"],
                {
                    "price": 100,
                    "time": "2026-07-01 16:00:00",
                    "snapshot_id": "stale-retry-baseline",
                },
            )
            resolve_qfq_observation(store,
                "room_storage",
                proposal["id"],
                outcome_price=103,
                outcome_time="2026-07-02 16:00:00",
                return_pct=3,
                hit=True,
            )
            scorecard = store.observation_scorecard("room_storage")
            self.assertEqual(scorecard["lineage_grouping"]["linked_observation_count"], 1)
            self.assertEqual(scorecard["overall"]["sample_count"], 1)
            self.assertFalse(scorecard["overall"]["qualified"])

    def test_two_store_instances_serialize_the_same_binding_without_duplicate_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "studio.sqlite3"
            first_store = StudioStore(database_path)
            second_store = StudioStore(database_path)
            research_round = first_store.create_round(
                "room_storage",
                "Serialize one exact AI proposal binding across store instances.",
            )
            lineage = create_scorecard_lineage_fixture(
                first_store,
                round_id=research_round["id"],
            )
            ai_member = first_store.room_snapshot("room_storage")["members"][0]
            proposal = first_store.create_observation(
                "room_storage",
                observation_payload(
                    created_by=ai_member["id"],
                    round_id=research_round["id"],
                    methodology_id="multi_store_binding_test",
                ),
            )
            binding_kwargs = {
                "user_decision_id": lineage["decision"]["id"],
                "source_portfolio_id": lineage["portfolio"]["id"],
                "source_portfolio_version": lineage["portfolio"]["version"],
                "derivation_note": "Both callers bind the same exact immutable proposal.",
                "bound_by": "user",
            }
            start = threading.Barrier(2)
            result_lock = threading.Lock()
            results: list[dict] = []
            errors: list[Exception] = []

            def bind(store: StudioStore) -> None:
                start.wait(timeout=5)
                try:
                    result = store.bind_observation_decision_lineage(
                        "room_storage",
                        proposal["id"],
                        **binding_kwargs,
                    )
                except Exception as exc:
                    with result_lock:
                        errors.append(exc)
                else:
                    with result_lock:
                        results.append(result)

            threads = [
                threading.Thread(target=bind, args=(first_store,)),
                threading.Thread(target=bind, args=(second_store,)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(
                {result["decision_lineage"]["event"]["id"] for result in results},
                {results[0]["decision_lineage"]["event"]["id"]},
            )
            support_package = next(
                package
                for package in first_store.list_decision_packages("room_storage")
                if package["package_id"] == lineage["decision"]["id"]
            )
            tests_events = [
                event
                for event in support_package["lineage"]
                if event["relation_type"] == "tests"
                and event["resource_id"] == proposal["id"]
            ]
            self.assertEqual(len(tests_events), 1)
            persisted = first_store.get_observation("room_storage", proposal["id"])
            self.assertEqual(persisted["artifact_id"], lineage["artifact"]["id"])
            self.assertEqual(persisted["created_by"], ai_member["id"])

    def test_decision_lineage_binding_fails_closed_for_non_ai_confirmed_cross_round_or_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            market = FakeObservationMarket()
            service = ObservationService(store, market, clock=lambda: NOW)
            research_round = store.create_round(
                "room_storage",
                "Freeze one exact paper-only research chain.",
            )
            lineage = create_scorecard_lineage_fixture(
                store,
                round_id=research_round["id"],
            )
            ai_member = store.room_snapshot("room_storage")["members"][0]
            binding_payload = {
                "user_decision_id": lineage["decision"]["id"],
                "source_portfolio_id": lineage["portfolio"]["id"],
                "source_portfolio_version": lineage["portfolio"]["version"],
                "derivation_note": "Bind only this exact confirmed decision chain.",
            }

            user_proposal = store.create_observation(
                "room_storage",
                observation_payload(round_id=research_round["id"]),
            )
            with self.assertRaisesRegex(ValueError, "AI"):
                service.bind_decision_lineage(
                    "room_storage",
                    user_proposal["id"],
                    binding_payload,
                )

            confirmed_ai = store.create_observation(
                "room_storage",
                observation_payload(
                    created_by=ai_member["id"],
                    round_id=research_round["id"],
                ),
            )
            store.confirm_observation(
                "room_storage",
                confirmed_ai["id"],
                {
                    "price": 100,
                    "time": "2026-07-01 16:00:00",
                    "snapshot_id": "confirmed-before-binding",
                },
            )
            with self.assertRaisesRegex(ValueError, "尚未确认"):
                service.bind_decision_lineage(
                    "room_storage",
                    confirmed_ai["id"],
                    binding_payload,
                )

            store.complete_round(research_round["id"])
            other_round = store.create_round(
                "room_storage",
                "A separate research chain must not reuse the frozen anchor.",
            )
            cross_round_ai = store.create_observation(
                "room_storage",
                observation_payload(
                    created_by=ai_member["id"],
                    round_id=other_round["id"],
                ),
            )
            with self.assertRaisesRegex(ValueError, "同一研究轮次"):
                service.bind_decision_lineage(
                    "room_storage",
                    cross_round_ai["id"],
                    binding_payload,
                )
            self.assertFalse(store.get_observation("room_storage", cross_round_ai["id"])["artifact_id"])

            same_round_ai = store.create_observation(
                "room_storage",
                observation_payload(
                    created_by=ai_member["id"],
                    round_id=research_round["id"],
                ),
            )
            service.bind_decision_lineage(
                "room_storage",
                same_round_ai["id"],
                binding_payload,
            )
            conflicting_payload = {
                **binding_payload,
                "source_portfolio_version": lineage["portfolio"]["version"] + 1,
            }
            with self.assertRaisesRegex(ValueError, "另一精确模拟组合版本"):
                service.bind_decision_lineage(
                    "room_storage",
                    same_round_ai["id"],
                    conflicting_payload,
                )
            with store._lock, closing(store._connect()) as connection, connection:
                connection.execute(
                    "UPDATE observations SET methodology_id=? WHERE room_id=? AND id=?",
                    ("tampered_methodology", "room_storage", same_round_ai["id"]),
                )
            with self.assertRaisesRegex(ValueError, "快照不一致"):
                service.bind_decision_lineage(
                    "room_storage",
                    same_round_ai["id"],
                    binding_payload,
                )
            with store._lock, closing(store._connect()) as connection, connection:
                connection.execute(
                    "UPDATE observations SET methodology_id=? WHERE room_id=? AND id=?",
                    (same_round_ai["methodology_id"], "room_storage", same_round_ai["id"]),
                )
            self.assertEqual(market.snapshot_calls, 0)
            self.assertEqual(market.history_calls, [])

    def test_offline_market_never_fabricates_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            service = ObservationService(store, FakeObservationMarket(online=False), clock=lambda: NOW)
            proposed = service.create("room_storage", observation_payload())

            confirmed = service.confirm("room_storage", proposed["id"])

            self.assertEqual(confirmed["status"], "PENDING_BASELINE")
            self.assertIsNone(confirmed["baseline_price"])
            self.assertIn("未生成或猜测基准价", confirmed["resolution_note"])

    def test_confirmation_fails_closed_for_noncanonical_futu_snapshots(self) -> None:
        invalid_snapshots: dict[str, dict] = {}

        wrong_source = ready_observation_snapshot()
        wrong_source["source"] = "fixture"
        invalid_snapshots["wrong source"] = wrong_source

        degraded = ready_observation_snapshot()
        degraded["state"] = "degraded"
        invalid_snapshots["degraded state"] = degraded

        source_error = ready_observation_snapshot()
        source_error["source_errors"] = [{"code": "PARTIAL"}]
        invalid_snapshots["source error"] = source_error

        missing_symbol = ready_observation_snapshot()
        missing_symbol["missing_symbols"] = ["US.STX"]
        missing_symbol["rows"] = missing_symbol["rows"][:-1]
        invalid_snapshots["missing row"] = missing_symbol

        implicit_errors = ready_observation_snapshot()
        implicit_errors.pop("source_errors")
        invalid_snapshots["implicit source errors"] = implicit_errors

        execution_capable = ready_observation_snapshot()
        execution_capable["execution_capability"] = "orders"
        invalid_snapshots["execution capable"] = execution_capable

        live_trading = ready_observation_snapshot()
        live_trading["live_trading_allowed"] = True
        invalid_snapshots["live trading allowed"] = live_trading

        legacy_freshness = ready_observation_snapshot()
        legacy_freshness["rows"][0].pop("quote_is_live")
        invalid_snapshots["legacy freshness"] = legacy_freshness

        future_row = ready_observation_snapshot()
        future_row["rows"][0].update({
            "updated_at": "2026-07-01T20:01:00Z",
            "age_seconds": -60,
        })
        invalid_snapshots["future row"] = future_row

        stale_row = ready_observation_snapshot()
        stale_row["rows"][0].update({
            "updated_at": "2026-07-01T19:39:59Z",
            "age_seconds": 1201,
        })
        invalid_snapshots["stale live row"] = stale_row

        suspended_row = ready_observation_snapshot()
        suspended_row["rows"][0].update({
            "security_status": "HALTED",
            "suspended": True,
        })
        invalid_snapshots["suspended row"] = suspended_row

        for case_name, snapshot in invalid_snapshots.items():
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as temp_dir:
                store = self.make_store(temp_dir)
                market = FakeObservationMarket(snapshot_payload=snapshot)
                service = ObservationService(store, market, clock=lambda: NOW)
                proposed = service.create("room_storage", observation_payload())

                confirmed = service.confirm("room_storage", proposed["id"])

                self.assertEqual(confirmed["status"], "PENDING_BASELINE")
                self.assertTrue(confirmed["user_confirmed"])
                self.assertIsNone(confirmed["baseline_price"])
                self.assertFalse(confirmed["baseline_snapshot_id"])
                self.assertEqual(market.snapshot_calls, 1)

    def test_reconcile_keeps_pending_until_full_read_only_snapshot_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            market = FakeObservationMarket(online=False)
            service = ObservationService(store, market, clock=lambda: NOW)
            proposed = service.create("room_storage", observation_payload())
            pending = service.confirm("room_storage", proposed["id"])
            self.assertEqual(pending["status"], "PENDING_BASELINE")

            unsafe_snapshot = ready_observation_snapshot()
            unsafe_snapshot["live_trading_allowed"] = True
            market.online = True
            market.snapshot_payload = unsafe_snapshot
            first = service.reconcile("room_storage")
            still_pending = next(
                row for row in first["observations"] if row["id"] == proposed["id"]
            )
            self.assertEqual(still_pending["status"], "PENDING_BASELINE")
            self.assertIsNone(still_pending["baseline_price"])

            market.snapshot_payload = ready_observation_snapshot()
            second = service.reconcile("room_storage")
            opened = next(
                row for row in second["observations"] if row["id"] == proposed["id"]
            )
            self.assertEqual(opened["status"], "OPEN")
            self.assertEqual(opened["baseline_price"], 100.0)
            self.assertEqual(opened["baseline_snapshot_id"], "futu-real-snapshot")

    def test_fifth_later_trading_close_resolves_observation(self) -> None:
        rows = [
            {"market_time": f"2026-07-{day:02d} 16:00:00", "close": close}
            for day, close in [(1, 100), (2, 101), (3, 102), (6, 103), (7, 104), (8, 110)]
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            service = ObservationService(store, FakeObservationMarket(history_rows=rows), clock=lambda: NOW)
            proposed = service.create("room_storage", observation_payload())
            confirmed = service.confirm("room_storage", proposed["id"])

            result = service.reconcile("room_storage")
            resolved = next(row for row in result["observations"] if row["id"] == confirmed["id"])

            self.assertEqual(resolved["status"], "RESOLVED")
            self.assertEqual(resolved["outcome_price"], 110.0)
            self.assertAlmostEqual(resolved["return_pct"], 10.0)
            self.assertTrue(resolved["hit"])
            self.assertEqual(resolved["measurement_method"], "qfq_close_to_close_v2")
            self.assertEqual(resolved["scoring_baseline_price"], 100.0)
            self.assertEqual(result["scorecard"]["version"], "observation_scorecard_v3")
            self.assertEqual(result["scorecard"]["overall"]["sample_count"], 0)
            self.assertFalse(result["scorecard"]["overall"]["qualified"])
            self.assertEqual(result["scorecard"]["overall"]["metric_label"], "样本不足")
            self.assertEqual(
                result["scorecard"]["scoring_population"]["unlinked_or_legacy_count"],
                1,
            )
            self.assertEqual(resolved["benchmark_result"]["state"], "unavailable")
            self.assertIsNone(resolved["relative_return_pct"])
            self.assertIsNone(resolved["relative_hit"])
            reflection = next(row for row in result["reflections"] if row["observation_id"] == resolved["id"])
            self.assertEqual(reflection["status"], "DRAFT")
            self.assertEqual(reflection["source_snapshot"]["return_pct"], 10.0)
            self.assertTrue(reflection["source_snapshot"]["hit"])
            self.assertEqual(len(reflection["source_snapshot_hash"]), 64)

    def test_split_adjusted_qfq_baseline_not_raw_snapshot_drives_return(self) -> None:
        rows = [
            {"market_time": "2026-07-01 16:00:00", "close": 50},
            {"market_time": "2026-07-02 16:00:00", "close": 55},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            service = ObservationService(
                store,
                FakeObservationMarket(history_rows=rows),
                clock=lambda: NOW,
            )
            proposed = service.create(
                "room_storage",
                observation_payload(horizon_days=1, threshold_pct=2),
            )
            confirmed = service.confirm("room_storage", proposed["id"])

            result = service.reconcile("room_storage")
            resolved = next(
                row for row in result["observations"] if row["id"] == confirmed["id"]
            )

            self.assertEqual(resolved["baseline_price"], 100.0)
            self.assertEqual(resolved["scoring_baseline_price"], 50.0)
            self.assertEqual(resolved["outcome_price"], 55.0)
            self.assertAlmostEqual(resolved["return_pct"], 10.0)
            self.assertEqual(
                resolved["measurement_method"],
                "qfq_close_to_close_v2",
            )

    def test_delayed_reconcile_uses_bounded_forward_window_not_latest_tail(self) -> None:
        baseline_day = datetime(2026, 1, 2).date()
        sessions = []
        cursor = baseline_day
        while len(sessions) < 100:
            if cursor.weekday() < 5:
                sessions.append(cursor)
            cursor += timedelta(days=1)
        rows = [
            {
                "market_time": f"{session.isoformat()} 16:00:00",
                "close": 100 + index,
            }
            for index, session in enumerate(sessions)
        ]

        class TailKeepingMarket(FakeObservationMarket):
            def history(self, symbol: str, **kwargs) -> dict:
                payload = super().history(symbol, **kwargs)
                limit = int(kwargs.get("limit") or len(payload.get("rows") or []))
                payload["rows"] = list(payload.get("rows") or [])[-limit:]
                if payload["rows"]:
                    payload["actual_start"] = payload["rows"][0]["market_time"][:10]
                    payload["actual_end"] = payload["rows"][-1]["market_time"][:10]
                    payload["last_completed_session"] = payload["actual_end"]
                return payload

        snapshot = ready_observation_snapshot()
        snapshot["captured_at"] = "2026-01-02T20:00:00Z"
        for row in snapshot["rows"]:
            row.update({
                "updated_at": "2026-01-02T19:59:30Z",
                "market_time": "2026-01-02 14:59:30",
            })
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            market = TailKeepingMarket(
                history_rows=rows,
                snapshot_payload=snapshot,
            )
            service = ObservationService(store, market, clock=lambda: NOW)
            proposed = service.create("room_storage", observation_payload())
            service.confirm("room_storage", proposed["id"])

            result = service.reconcile("room_storage")
            resolved = next(
                row for row in result["observations"] if row["id"] == proposed["id"]
            )

            self.assertEqual(resolved["status"], "RESOLVED")
            self.assertEqual(resolved["scoring_baseline_price"], 100.0)
            self.assertEqual(resolved["outcome_price"], 105.0)
            self.assertEqual(market.history_calls[0]["limit"], 256)
            self.assertEqual(market.history_calls[0]["end"], "2026-07-01")

    def test_untrusted_history_envelope_cannot_resolve_observation(self) -> None:
        invalid = readonly_qfq_history(
            "US.MU",
            [
                {"market_time": "2026-07-01 16:00:00", "close": 100},
                {"market_time": "2026-07-02 16:00:00", "close": 110},
            ],
            source="untrusted",
            execution_capability="broker",
            live_trading_allowed=True,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            service = ObservationService(
                store,
                FakeObservationMarket(history_payload_by_symbol={"US.MU": invalid}),
                clock=lambda: NOW,
            )
            proposed = service.create(
                "room_storage",
                observation_payload(horizon_days=1),
            )
            service.confirm("room_storage", proposed["id"])

            result = service.reconcile("room_storage")
            observation = next(
                row for row in result["observations"] if row["id"] == proposed["id"]
            )

            self.assertEqual(observation["status"], "OPEN")
            self.assertIsNone(observation["outcome_price"])

    def test_peer_benchmark_uses_same_snapshot_and_target_outcome_date(self) -> None:
        snapshot_rows = [
            {
                "symbol": symbol,
                "last": price,
                "quality": "ready",
                "suspended": False,
                "market_time": "2026-07-01 15:59:30",
            }
            for symbol, price in (("US.MU", 100), ("US.SNDK", 50), ("US.WDC", 200), ("US.STX", 25))
        ]
        history_by_symbol = {
            "US.MU": [
                {"market_time": "2026-07-01 16:00:00", "close": 100},
                {"market_time": "2026-07-02 16:00:00", "close": 110},
            ],
            "US.SNDK": [
                {"market_time": "2026-07-01 16:00:00", "close": 25},
                {"market_time": "2026-07-02 16:00:00", "close": 26.25},
            ],
            "US.WDC": [
                {"market_time": "2026-07-01 16:00:00", "close": 100},
                {"market_time": "2026-07-02 16:00:00", "close": 100},
            ],
            "US.STX": [
                {"market_time": "2026-07-01 16:00:00", "close": 12.5},
                {"market_time": "2026-07-02 16:00:00", "close": 11.875},
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            lineage = create_scorecard_lineage_fixture(store)
            market = FakeObservationMarket(snapshot_rows=snapshot_rows, history_by_symbol=history_by_symbol)
            service = ObservationService(store, market, clock=lambda: NOW)
            proposed = service.create(
                "room_storage",
                linked_observation_payload(lineage, horizon_days=1, threshold_pct=2),
            )
            confirmed = service.confirm("room_storage", proposed["id"])

            result = service.reconcile("room_storage")
            resolved = next(row for row in result["observations"] if row["id"] == confirmed["id"])

            self.assertEqual(len(confirmed["benchmark_baseline"]["peers"]), 3)
            self.assertEqual(confirmed["benchmark_baseline"]["snapshot_id"], confirmed["baseline_snapshot_id"])
            self.assertEqual(resolved["benchmark_result"]["state"], "ready")
            self.assertEqual(resolved["benchmark_result"]["peer_count"], 3)
            self.assertEqual(
                resolved["benchmark_result"]["peers"][0]["baseline_price"],
                25.0,
            )
            self.assertAlmostEqual(resolved["benchmark_result"]["peer_equal_weight_return_pct"], 0.0)
            self.assertAlmostEqual(resolved["relative_return_pct"], 10.0)
            self.assertTrue(resolved["relative_hit"])
            self.assertEqual(result["scorecard"]["overall"]["peer_relative"]["sample_count"], 1)
            self.assertEqual(result["scorecard"]["overall"]["peer_relative"]["hit_rate_pct"], 100.0)
            self.assertEqual([call["symbol"] for call in market.history_calls], ["US.MU", "US.SNDK", "US.WDC", "US.STX"])

    def test_only_user_confirmed_reflection_enters_future_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            proposal = store.create_observation("room_storage", observation_payload())
            store.confirm_observation(
                "room_storage",
                proposal["id"],
                {"price": 100, "time": "2026-07-01 16:00:00", "snapshot_id": "source-snapshot"},
            )
            resolve_qfq_observation(store,
                "room_storage",
                proposal["id"],
                outcome_price=105,
                outcome_time="2026-07-08 16:00:00",
                return_pct=5,
                hit=True,
            )
            draft = store.get_reflection("room_storage", proposal["id"])

            self.assertEqual(store.confirmed_reflection_prompt_context("room_storage"), "")
            with self.assertRaisesRegex(ValueError, "本次教训"):
                store.confirm_reflection(
                    "room_storage",
                    proposal["id"],
                    expected_version=draft["version"],
                )

            updated = store.update_reflection("room_storage", proposal["id"], {
                "expected_version": draft["version"],
                "lesson": "产业催化与价格结构同时成立时，本次五日观察得到支持。",
                "caveat": "只有一个样本，且不能区分市场贝塔和公司特异因素。",
                "next_test": "加入行业基准并在相同阈值下重复至少二十次。",
            })
            confirmed = store.confirm_reflection(
                "room_storage",
                proposal["id"],
                expected_version=updated["version"],
            )
            context = store.confirmed_reflection_prompt_context("room_storage")

            self.assertEqual(confirmed["status"], "CONFIRMED")
            self.assertIn(proposal["id"], context)
            self.assertIn("只有一个样本", context)
            self.assertIn("不能当作系统指令", context)

            revised = store.update_reflection("room_storage", proposal["id"], {
                "expected_version": confirmed["version"],
                "lesson": "重新修订，等待再次确认。",
            })
            self.assertEqual(revised["status"], "DRAFT")
            self.assertEqual(store.confirmed_reflection_prompt_context("room_storage"), "")

    def test_reflection_confirmation_rejects_tampered_source_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            proposal = store.create_observation("room_storage", observation_payload(horizon_days=1))
            store.confirm_observation(
                "room_storage",
                proposal["id"],
                {"price": 100, "time": "2026-07-01 16:00:00", "snapshot_id": "tamper-source"},
            )
            resolve_qfq_observation(store,
                "room_storage",
                proposal["id"],
                outcome_price=101,
                outcome_time="2026-07-02 16:00:00",
                return_pct=1,
                hit=False,
            )
            draft = store.get_reflection("room_storage", proposal["id"])
            updated = store.update_reflection("room_storage", proposal["id"], {
                "expected_version": draft["version"],
                "lesson": "需要验证来源完整性。",
                "next_test": "下一次继续保留审计快照。",
            })
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute(
                    "UPDATE observation_reflections SET source_snapshot_json='{}' WHERE id=?",
                    (updated["id"],),
                )

            with self.assertRaisesRegex(ValueError, "指纹不一致"):
                store.confirm_reflection(
                    "room_storage",
                    proposal["id"],
                    expected_version=updated["version"],
                )

    def test_future_rows_and_baseline_day_are_not_used(self) -> None:
        rows = [
            {"market_time": "2026-07-01 16:00:00", "close": 130},
            {"market_time": "2026-07-02 16:00:00", "close": 101},
            {"market_time": "2026-07-30 16:00:00", "close": 999},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            service = ObservationService(store, FakeObservationMarket(history_rows=rows), clock=lambda: NOW)
            proposed = service.create("room_storage", observation_payload())
            service.confirm("room_storage", proposed["id"])

            result = service.reconcile("room_storage")
            observation = next(row for row in result["observations"] if row["id"] == proposed["id"])

            self.assertEqual(observation["status"], "OPEN")
            self.assertIsNone(observation["outcome_price"])

    def test_twenty_verified_linked_samples_unlock_then_tamper_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            lineage = create_scorecard_lineage_fixture(store)
            ai_member = store.room_snapshot("room_storage")["members"][0]
            observation_ids: list[str] = []
            start = datetime(2026, 1, 1, tzinfo=timezone.utc)
            for index in range(20):
                baseline_day = start + timedelta(days=index * 2)
                outcome_day = baseline_day + timedelta(days=1)
                proposal = store.create_observation(
                    "room_storage",
                    linked_observation_payload(
                        lineage,
                        created_by=ai_member["id"],
                        horizon_days=1,
                        model_confidence=80 if index % 2 == 0 else 60,
                    ),
                )
                observation_ids.append(proposal["id"])
                store.confirm_observation(
                    "room_storage",
                    proposal["id"],
                    {
                        "price": 100,
                        "time": baseline_day.strftime("%Y-%m-%d 16:00:00"),
                        "snapshot_id": f"snap-{index}",
                    },
                )
                resolve_qfq_observation(store,
                    "room_storage",
                    proposal["id"],
                    outcome_price=105 if index < 15 else 95,
                    outcome_time=outcome_day.strftime("%Y-%m-%d 16:00:00"),
                    return_pct=5 if index < 15 else -5,
                    hit=index < 15,
                )
                if index == 18:
                    early = store.observation_scorecard("room_storage")["overall"]
                    self.assertFalse(early["qualified"])
                    self.assertEqual(early["metric_label"], "样本不足")

            final = store.observation_scorecard("room_storage")["overall"]
            self.assertTrue(final["qualified"])
            self.assertEqual(final["metric_label"], "统计胜率")
            self.assertEqual(final["sample_count"], 20)
            self.assertEqual(final["hit_rate_pct"], 75.0)
            self.assertIsNotNone(final["brier_score"])
            scorecard = store.observation_scorecard("room_storage")
            self.assertEqual(scorecard["rolling"]["last_20"]["sample_count"], 20)
            self.assertEqual(scorecard["rolling"]["last_50"]["sample_count"], 20)
            bands = {row["band"]: row for row in scorecard["confidence_calibration"]}
            self.assertEqual(bands["50-69"]["sample_count"], 10)
            self.assertEqual(bands["70-84"]["sample_count"], 10)
            self.assertFalse(bands["70-84"]["qualified"])
            agent_rows = scorecard["by_agent_methodology"]
            self.assertEqual(len(agent_rows), 1)
            self.assertEqual(agent_rows[0]["member_id"], ai_member["id"])
            self.assertEqual(agent_rows[0]["member_version"], ai_member["version"])
            self.assertEqual(agent_rows[0]["member_name"], ai_member["name"])
            self.assertEqual(agent_rows[0]["methodology_id"], "directional_threshold")
            self.assertTrue(agent_rows[0]["qualified"])
            self.assertEqual(agent_rows[0]["hit_rate_pct"], 75.0)
            self.assertEqual(scorecard["scoring_population"]["lineage_verified_resolved_count"], 20)
            self.assertEqual(scorecard["scoring_population"]["excluded_from_scoring_count"], 0)
            self.assertTrue(StorageSampleAcceptance._statistical_validation(scorecard)["ready"])
            self.assertTrue(ConvergenceService._simulation_gate(
                {
                    "observations": store.list_observations("room_storage"),
                    "observation_scorecard": scorecard,
                },
                True,
            )["statistical_claim_allowed"])

            with store._lock, closing(store._connect()) as connection, connection:
                connection.execute(
                    "UPDATE observations SET artifact_id=? WHERE room_id=? AND id=?",
                    ("tampered-artifact", "room_storage", observation_ids[0]),
                )

            tampered = store.observation_scorecard("room_storage")
            self.assertEqual(tampered["overall"]["sample_count"], 19)
            self.assertFalse(tampered["overall"]["qualified"])
            self.assertEqual(tampered["rolling"]["last_20"]["sample_count"], 19)
            self.assertEqual(tampered["scoring_population"]["invalid_lineage_count"], 1)
            self.assertEqual(tampered["scoring_population"]["excluded_from_scoring_count"], 1)
            self.assertFalse(StorageSampleAcceptance._statistical_validation(tampered)["ready"])
            self.assertFalse(ConvergenceService._simulation_gate(
                {
                    "observations": store.list_observations("room_storage"),
                    "observation_scorecard": tampered,
                },
                True,
            )["statistical_claim_allowed"])

    def test_twenty_unlinked_samples_remain_auditable_but_never_unlock_scoring(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            ai_member = store.room_snapshot("room_storage")["members"][0]
            start = datetime(2026, 3, 1, tzinfo=timezone.utc)
            for index in range(20):
                baseline_day = start + timedelta(days=index * 2)
                outcome_day = baseline_day + timedelta(days=1)
                proposal = store.create_observation(
                    "room_storage",
                    observation_payload(
                        created_by=ai_member["id"],
                        horizon_days=1,
                        model_confidence=80,
                    ),
                )
                store.confirm_observation(
                    "room_storage",
                    proposal["id"],
                    {
                        "price": 100,
                        "time": baseline_day.strftime("%Y-%m-%d 16:00:00"),
                        "snapshot_id": f"legacy-{index}",
                    },
                )
                resolve_qfq_observation(store,
                    "room_storage",
                    proposal["id"],
                    outcome_price=105,
                    outcome_time=outcome_day.strftime("%Y-%m-%d 16:00:00"),
                    return_pct=5,
                    hit=True,
                )

            scorecard = store.observation_scorecard("room_storage")

            self.assertEqual(scorecard["overall"]["sample_count"], 0)
            self.assertFalse(scorecard["overall"]["qualified"])
            self.assertEqual(scorecard["rolling"]["last_20"]["sample_count"], 0)
            self.assertEqual(scorecard["by_agent_methodology"], [])
            self.assertTrue(all(row["sample_count"] == 0 for row in scorecard["confidence_calibration"]))
            self.assertEqual(scorecard["scoring_population"]["resolved_user_confirmed_count"], 20)
            self.assertEqual(scorecard["scoring_population"]["lineage_verified_resolved_count"], 0)
            self.assertEqual(scorecard["scoring_population"]["unlinked_or_legacy_count"], 20)
            self.assertEqual(scorecard["scoring_population"]["excluded_from_scoring_count"], 20)
            self.assertFalse(StorageSampleAcceptance._statistical_validation(scorecard)["ready"])
            self.assertFalse(ConvergenceService._simulation_gate(
                {
                    "observations": store.list_observations("room_storage"),
                    "observation_scorecard": scorecard,
                },
                True,
            )["statistical_claim_allowed"])

    def test_legacy_snapshot_to_qfq_measurement_never_mixes_with_v2_scorecard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            lineage = create_scorecard_lineage_fixture(store)
            ai_member = store.room_snapshot("room_storage")["members"][0]
            observation_ids = []
            for index, baseline_day in enumerate((1, 3)):
                proposal = store.create_observation(
                    "room_storage",
                    linked_observation_payload(
                        lineage,
                        created_by=ai_member["id"],
                        horizon_days=1,
                    ),
                )
                observation_ids.append(proposal["id"])
                store.confirm_observation(
                    "room_storage",
                    proposal["id"],
                    {
                        "price": 100,
                        "time": f"2026-01-{baseline_day:02d} 16:00:00",
                        "snapshot_id": f"measurement-{index}",
                    },
                )
                resolve_qfq_observation(
                    store,
                    "room_storage",
                    proposal["id"],
                    outcome_price=105,
                    outcome_time=f"2026-01-{baseline_day + 1:02d} 16:00:00",
                    return_pct=5,
                    hit=True,
                )

            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute(
                    "UPDATE observations SET measurement_method=? WHERE id=?",
                    ("snapshot_last_to_qfq_close_v1", observation_ids[1]),
                )

            scorecard = store.observation_scorecard("room_storage")

            self.assertEqual(scorecard["version"], "observation_scorecard_v3")
            self.assertEqual(scorecard["overall"]["sample_count"], 1)
            self.assertEqual(
                scorecard["scoring_population"]["legacy_measurement_count"],
                1,
            )
            self.assertEqual(
                scorecard["scoring_population"]["required_measurement_method"],
                "qfq_close_to_close_v2",
            )
            self.assertEqual(scorecard["scoring_population"]["excluded_from_scoring_count"], 1)

    def test_store_migrates_legacy_observation_rows_to_audit_only_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            proposal = store.create_observation(
                "room_storage",
                observation_payload(thesis="legacy schema migration fixture"),
            )

            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("ALTER TABLE observations DROP COLUMN measurement_method")
                connection.execute("ALTER TABLE observations DROP COLUMN scoring_baseline_price")
                connection.execute("ALTER TABLE observations DROP COLUMN scoring_baseline_time")

            migrated_store = self.make_store(temp_dir)
            migrated = migrated_store.get_observation("room_storage", proposal["id"])

            self.assertIsNotNone(migrated)
            self.assertEqual(
                migrated["measurement_method"],
                "snapshot_last_to_qfq_close_v1",
            )
            self.assertIsNone(migrated["scoring_baseline_price"])
            self.assertEqual(migrated["scoring_baseline_time"], "")

    def test_mixed_symbol_conditions_are_descriptive_not_statistical_win_rate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            lineage = create_scorecard_lineage_fixture(store)
            ai_member = store.room_snapshot("room_storage")["members"][0]
            for index in range(20):
                symbol = "MU" if index % 2 == 0 else "WDC"
                proposal = store.create_observation(
                    "room_storage",
                    linked_observation_payload(
                        lineage,
                        symbol=symbol,
                        created_by=ai_member["id"],
                        horizon_days=1,
                    ),
                )
                store.confirm_observation(
                    "room_storage",
                    proposal["id"],
                    {
                        "price": 100,
                        "time": f"2026-08-{index + 1:02d} 16:00:00",
                        "snapshot_id": f"mixed-{index}",
                    },
                )
                resolve_qfq_observation(store,
                    "room_storage",
                    proposal["id"],
                    outcome_price=103,
                    outcome_time=f"2026-08-{index + 2:02d} 16:00:00",
                    return_pct=3,
                    hit=True,
                )

            scorecard = store.observation_scorecard("room_storage")
            overall = scorecard["overall"]
            methodology = scorecard["by_methodology"]["directional_threshold@v1"]

            self.assertEqual(overall["sample_count"], 20)
            self.assertFalse(overall["qualified"])
            self.assertTrue(overall["descriptive_only"])
            self.assertEqual(overall["metric_label"], "混合条件描述命中率")
            self.assertEqual(overall["comparison_group_count"], 2)
            self.assertFalse(methodology["qualified"])
            self.assertTrue(methodology["mixed_conditions"])
            self.assertEqual(len(scorecard["by_comparison_group"]), 2)
            self.assertTrue(all(
                row["sample_count"] == 10 and not row["qualified"]
                for row in scorecard["by_comparison_group"].values()
            ))

    def test_scorecard_groups_only_verified_persisted_decision_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            lineage = create_scorecard_lineage_fixture(store)
            portfolio = lineage["portfolio"]
            decision = lineage["decision"]
            artifact = lineage["artifact"]
            linked_observation_ids: list[str] = []

            for index, symbol in enumerate(("MU", "WDC")):
                proposal = store.create_observation(
                    "room_storage",
                    observation_payload(
                        symbol=symbol,
                        horizon_days=1,
                        user_decision_id=decision["id"],
                        source_portfolio_id=portfolio["id"],
                        source_portfolio_version=portfolio["version"],
                        derivation_note=(
                            "Validate this exact confirmed portfolio version against "
                            "the selected candidate."
                        ),
                    ),
                )
                linked_observation_ids.append(proposal["id"])
                baseline_day = datetime(2026, 1, 2 + index * 3, tzinfo=timezone.utc)
                outcome_day = baseline_day + timedelta(days=1)
                store.confirm_observation(
                    "room_storage",
                    proposal["id"],
                    {
                        "price": 100,
                        "time": baseline_day.strftime("%Y-%m-%d 16:00:00"),
                        "snapshot_id": f"linked-{index}",
                    },
                )
                resolve_qfq_observation(store,
                    "room_storage",
                    proposal["id"],
                    outcome_price=103,
                    outcome_time=outcome_day.strftime("%Y-%m-%d 16:00:00"),
                    return_pct=3,
                    hit=True,
                )

            unlinked = store.create_observation(
                "room_storage",
                observation_payload(
                    symbol="STX",
                    horizon_days=1,
                    artifact_id=artifact["id"],
                    decision_package_id=decision["id"],
                    candidate_option_id="paper_small",
                ),
            )
            store.confirm_observation(
                "room_storage",
                unlinked["id"],
                {
                    "price": 100,
                    "time": "2026-01-10 16:00:00",
                    "snapshot_id": "unlinked-client-claims",
                },
            )
            resolve_qfq_observation(store,
                "room_storage",
                unlinked["id"],
                outcome_price=103,
                outcome_time="2026-01-11 16:00:00",
                return_pct=3,
                hit=True,
            )

            scorecard = store.observation_scorecard("room_storage")
            decision_key = decision["id"]
            portfolio_key = f"{portfolio['id']}@v{portfolio['version']}"
            candidate_key = (
                f"{artifact['id']}@v{artifact['version']}:paper_small"
            )

            self.assertEqual(scorecard["overall"]["sample_count"], 2)
            lineage_grouping = scorecard["lineage_grouping"]
            self.assertEqual(lineage_grouping["resolved_observation_count"], 3)
            self.assertEqual(lineage_grouping["linked_observation_count"], 2)
            self.assertEqual(lineage_grouping["unlinked_observation_count"], 1)
            self.assertEqual(lineage_grouping["invalid_lineage_observation_count"], 0)
            self.assertEqual(lineage_grouping["excluded_from_scoring_count"], 1)
            self.assertEqual(scorecard["scoring_population"]["lineage_verified_resolved_count"], 2)
            self.assertEqual(set(scorecard["by_decision_package"]), {decision_key})
            self.assertEqual(set(scorecard["by_portfolio_version"]), {portfolio_key})
            self.assertEqual(set(scorecard["by_candidate_option"]), {candidate_key})
            json.dumps(scorecard, ensure_ascii=False)

            decision_group = scorecard["by_decision_package"][decision_key]
            portfolio_group = scorecard["by_portfolio_version"][portfolio_key]
            candidate_group = scorecard["by_candidate_option"][candidate_key]
            self.assertEqual(decision_group["artifact_id"], artifact["id"])
            self.assertEqual(decision_group["artifact_version"], artifact["version"])
            self.assertEqual(decision_group["candidate_option_id"], "paper_small")
            self.assertEqual(portfolio_group["portfolio_id"], portfolio["id"])
            self.assertEqual(portfolio_group["portfolio_version"], portfolio["version"])
            self.assertEqual(portfolio_group["decision_package_id"], decision["id"])
            self.assertEqual(candidate_group["decision_package_ids"], [decision["id"]])
            for group in (decision_group, portfolio_group, candidate_group):
                self.assertEqual(group["sample_count"], 2)
                self.assertFalse(group["qualified"])
                self.assertTrue(group["mixed_conditions"])
                self.assertEqual(group["metric_label"], "混合条件描述命中率")
                self.assertEqual(group["independence"]["raw_resolved_count"], 2)
                self.assertEqual(group["independence"]["independent_sample_count"], 2)

            with store._lock, closing(store._connect()) as connection, connection:
                connection.execute(
                    "UPDATE observations SET artifact_id=? WHERE room_id=? AND id=?",
                    ("client_tampered_artifact", "room_storage", linked_observation_ids[0]),
                )

            tampered = store.observation_scorecard("room_storage")
            self.assertEqual(tampered["lineage_grouping"]["linked_observation_count"], 1)
            self.assertEqual(tampered["lineage_grouping"]["unlinked_observation_count"], 1)
            self.assertEqual(tampered["lineage_grouping"]["invalid_lineage_observation_count"], 1)
            self.assertEqual(tampered["overall"]["sample_count"], 1)
            self.assertEqual(tampered["scoring_population"]["excluded_from_scoring_count"], 2)
            self.assertEqual(
                tampered["by_decision_package"][decision_key]["sample_count"],
                1,
            )

    def test_lineage_group_metrics_apply_independence_and_comparison_gates(self) -> None:
        rows: list[dict] = []
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for index in range(20):
            baseline_day = start + timedelta(days=index * 2)
            outcome_day = baseline_day + timedelta(days=1)
            rows.append({
                "methodology_id": "directional_threshold",
                "methodology_version": 1,
                "symbol": "US.MU",
                "horizon_days": 1,
                "direction": "UP",
                "threshold_pct": 2,
                "baseline_time": baseline_day.strftime("%Y-%m-%d 16:00:00"),
                "outcome_time": outcome_day.strftime("%Y-%m-%d 16:00:00"),
                "sample_key": f"independent-{index}",
                "confirmed_at": index,
                "created_at": index,
                "hit": index < 15,
                "model_confidence": None,
                "confidence_source": "user",
            })

        comparable = StudioStore._lineage_group_score_metrics(rows)
        self.assertTrue(comparable["qualified"])
        self.assertEqual(comparable["metric_label"], "统计胜率")
        self.assertEqual(comparable["sample_count"], 20)
        self.assertEqual(comparable["independence"]["independent_sample_count"], 20)

        mixed_conditions_rows = [dict(row) for row in rows]
        for index, row in enumerate(mixed_conditions_rows):
            row["symbol"] = "US.MU" if index % 2 == 0 else "US.WDC"
        mixed_conditions = StudioStore._lineage_group_score_metrics(
            mixed_conditions_rows
        )
        self.assertFalse(mixed_conditions["qualified"])
        self.assertTrue(mixed_conditions["mixed_conditions"])
        self.assertEqual(mixed_conditions["metric_label"], "混合条件描述命中率")
        self.assertFalse(mixed_conditions["peer_relative"]["qualified"])

        mixed_methodology_rows = [dict(row) for row in rows]
        for index, row in enumerate(mixed_methodology_rows):
            row["methodology_id"] = (
                "directional_threshold" if index % 2 == 0 else "alternate_method"
            )
        mixed_methodology = StudioStore._lineage_group_score_metrics(
            mixed_methodology_rows
        )
        self.assertFalse(mixed_methodology["qualified"])
        self.assertTrue(mixed_methodology["mixed_methodology"])
        self.assertEqual(mixed_methodology["metric_label"], "混合方法样本")
        self.assertFalse(mixed_methodology["peer_relative"]["qualified"])

        duplicate_rows = [dict(rows[0]) for _ in range(20)]
        duplicate_metrics = StudioStore._lineage_group_score_metrics(duplicate_rows)
        self.assertFalse(duplicate_metrics["qualified"])
        self.assertEqual(duplicate_metrics["sample_count"], 1)
        self.assertEqual(duplicate_metrics["independence"]["duplicate_window"], 19)
        self.assertEqual(
            duplicate_metrics["independence"]["independent_sample_count"],
            1,
        )

    def test_agent_scorecard_separates_identity_versions_and_excludes_user_observations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            lineage = create_scorecard_lineage_fixture(store)
            member_v1 = store.room_snapshot("room_storage")["members"][0]

            first = store.create_observation(
                "room_storage",
                linked_observation_payload(
                    lineage,
                    created_by=member_v1["id"],
                    horizon_days=1,
                    methodology_id="role_calibration",
                ),
            )
            store.confirm_observation(
                "room_storage",
                first["id"],
                {"price": 100, "time": "2026-01-01 16:00:00", "snapshot_id": "agent-v1"},
            )
            resolve_qfq_observation(store,
                "room_storage",
                first["id"],
                outcome_price=104,
                outcome_time="2026-01-02 16:00:00",
                return_pct=4,
                hit=True,
            )

            member_v2 = store.update_member(
                "room_storage",
                member_v1["id"],
                {"name": "主持人新版", "identity": "新版身份边界"},
            )
            second = store.create_observation(
                "room_storage",
                linked_observation_payload(
                    lineage,
                    created_by=member_v2["id"],
                    horizon_days=1,
                    methodology_id="role_calibration",
                ),
            )
            store.confirm_observation(
                "room_storage",
                second["id"],
                {"price": 100, "time": "2026-01-03 16:00:00", "snapshot_id": "agent-v2"},
            )
            resolve_qfq_observation(store,
                "room_storage",
                second["id"],
                outcome_price=96,
                outcome_time="2026-01-04 16:00:00",
                return_pct=-4,
                hit=False,
            )

            user_observation = store.create_observation(
                "room_storage",
                linked_observation_payload(
                    lineage,
                    horizon_days=1,
                    methodology_id="role_calibration",
                ),
            )
            store.confirm_observation(
                "room_storage",
                user_observation["id"],
                {"price": 100, "time": "2026-01-05 16:00:00", "snapshot_id": "user"},
            )
            resolve_qfq_observation(store,
                "room_storage",
                user_observation["id"],
                outcome_price=103,
                outcome_time="2026-01-06 16:00:00",
                return_pct=3,
                hit=True,
            )

            scorecard = store.observation_scorecard("room_storage")
            rows = scorecard["by_agent_methodology"]

            self.assertEqual(scorecard["overall"]["sample_count"], 3)
            self.assertEqual(len(rows), 2)
            by_version = {row["member_version"]: row for row in rows}
            self.assertEqual(by_version[member_v1["version"]]["member_name"], member_v1["name"])
            self.assertEqual(by_version[member_v1["version"]]["hit_rate_pct"], 100.0)
            self.assertEqual(by_version[member_v2["version"]]["member_name"], "主持人新版")
            self.assertEqual(by_version[member_v2["version"]]["hit_rate_pct"], 0.0)
            self.assertTrue(all(row["sample_count"] == 1 for row in rows))
            self.assertTrue(all(not row["qualified"] for row in rows))

    def test_duplicate_or_overlapping_windows_do_not_unlock_win_rate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            lineage = create_scorecard_lineage_fixture(store)
            ai_member = store.room_snapshot("room_storage")["members"][0]
            for index in range(20):
                proposal = store.create_observation(
                    "room_storage",
                    linked_observation_payload(
                        lineage,
                        created_by=ai_member["id"],
                        horizon_days=5,
                        methodology_id="same_window_test",
                        methodology_version=1,
                    ),
                )
                store.confirm_observation(
                    "room_storage",
                    proposal["id"],
                    {"price": 100, "time": "2026-07-01 16:00:00", "snapshot_id": f"duplicate-{index}"},
                )
                resolve_qfq_observation(store,
                    "room_storage",
                    proposal["id"],
                    outcome_price=105,
                    outcome_time="2026-07-08 16:00:00",
                    return_pct=5,
                    hit=True,
                )

            scorecard = store.observation_scorecard("room_storage")

            self.assertEqual(scorecard["independence"]["raw_resolved_count"], 20)
            self.assertEqual(scorecard["independence"]["independent_sample_count"], 1)
            self.assertEqual(scorecard["independence"]["duplicate_window"], 19)
            self.assertFalse(scorecard["overall"]["qualified"])
            self.assertEqual(scorecard["overall"]["sample_count"], 1)

    def test_invalid_scope_and_neutral_threshold_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir)
            service = ObservationService(store, FakeObservationMarket(), clock=lambda: NOW)
            with self.assertRaisesRegex(ValueError, "仅支持"):
                service.create("room_storage", observation_payload(symbol="AAPL"))
            with self.assertRaisesRegex(ValueError, "中性观察"):
                service.create("room_storage", observation_payload(direction="NEUTRAL", threshold_pct=0))
            with self.assertRaisesRegex(ValueError, "未启用模拟观察能力包"):
                service.create("room_plan", observation_payload())


class ObservationHttpApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "studio.sqlite3")
        self.service = ObservationService(self.store, FakeObservationMarket(), clock=lambda: NOW)
        self.original_store = http_server.STORE
        self.original_observations = http_server.OBSERVATIONS
        http_server.STORE = self.store
        http_server.OBSERVATIONS = self.service
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), http_server.StudioRequestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        http_server.STORE = self.original_store
        http_server.OBSERVATIONS = self.original_observations
        self.temp_dir.cleanup()

    def request(self, path: str, payload: dict | None = None, *, method: str | None = None) -> tuple[int, dict]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
            },
            method=method or ("POST" if payload is not None else "GET"),
        )
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            status = exc.code
            response_payload = json.loads(exc.read().decode("utf-8"))
            exc.close()
            return status, response_payload

    def test_create_confirm_and_list_routes(self) -> None:
        status, created = self.request("/api/rooms/room_storage/observations", observation_payload())
        self.assertEqual(status, 201)
        self.assertEqual(created["observation"]["status"], "PROPOSED")

        observation_id = created["observation"]["id"]
        status, confirmed = self.request(
            f"/api/rooms/room_storage/observations/{observation_id}/confirm",
            {},
        )
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["observation"]["status"], "OPEN")
        self.assertFalse(confirmed["scorecard"]["live_trading_allowed"])

        status, listed = self.request("/api/rooms/room_storage/observations")
        self.assertEqual(status, 200)
        self.assertEqual(listed["observations"][0]["id"], observation_id)
        self.assertEqual(listed["scorecard"]["overall"]["sample_count"], 0)

    def test_bind_ai_proposal_decision_lineage_route_returns_current_scorecard(self) -> None:
        research_round = self.store.create_round(
            "room_storage",
            "Bind one AI proposal to one frozen paper-only decision chain.",
        )
        lineage = create_scorecard_lineage_fixture(
            self.store,
            round_id=research_round["id"],
        )
        ai_member = self.store.room_snapshot("room_storage")["members"][0]
        proposal = self.store.create_observation(
            "room_storage",
            observation_payload(
                created_by=ai_member["id"],
                round_id=research_round["id"],
                methodology_id="http_lineage_binding",
            ),
        )
        payload = {
            "user_decision_id": lineage["decision"]["id"],
            "source_portfolio_id": lineage["portfolio"]["id"],
            "source_portfolio_version": lineage["portfolio"]["version"],
            "derivation_note": "User binds this exact AI proposal for prospective testing.",
        }

        status, result = self.request(
            f"/api/rooms/room_storage/observations/{proposal['id']}/decision-lineage",
            payload,
        )

        self.assertEqual(status, 200)
        self.assertEqual(set(result), {"ok", "observation", "scorecard"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["observation"]["created_by"], ai_member["id"])
        self.assertEqual(result["observation"]["status"], "PROPOSED")
        self.assertEqual(result["observation"]["artifact_id"], lineage["artifact"]["id"])
        self.assertEqual(result["scorecard"]["overall"]["sample_count"], 0)
        self.assertFalse(result["scorecard"]["live_trading_allowed"])
        self.assertEqual(self.service.market_service.snapshot_calls, 0)
        self.assertEqual(self.service.market_service.history_calls, [])

        missing_status, missing = self.request(
            "/api/rooms/room_storage/observations/missing-observation/decision-lineage",
            payload,
        )
        self.assertEqual(missing_status, 404)
        self.assertFalse(missing["ok"])

    def test_reflection_update_confirm_and_list_routes(self) -> None:
        proposal = self.store.create_observation("room_storage", observation_payload())
        self.store.confirm_observation(
            "room_storage",
            proposal["id"],
            {"price": 100, "time": "2026-07-01 16:00:00", "snapshot_id": "route-snapshot"},
        )
        resolve_qfq_observation(self.store,
            "room_storage",
            proposal["id"],
            outcome_price=95,
            outcome_time="2026-07-08 16:00:00",
            return_pct=-5,
            hit=False,
        )
        draft = self.store.get_reflection("room_storage", proposal["id"])

        status, updated = self.request(
            f"/api/rooms/room_storage/observations/{proposal['id']}/reflection",
            {
                "expected_version": draft["version"],
                "lesson": "本次方向判断失败，原依据不足。",
                "caveat": "单样本且受市场整体下跌影响。",
                "next_test": "加入行业基准和相对收益条件。",
            },
            method="PATCH",
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["reflection"]["status"], "DRAFT")

        status, confirmed = self.request(
            f"/api/rooms/room_storage/observations/{proposal['id']}/reflection/confirm",
            {"expected_version": updated["reflection"]["version"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["reflection"]["status"], "CONFIRMED")

        status, listed = self.request("/api/rooms/room_storage/observations")
        self.assertEqual(status, 200)
        self.assertEqual(listed["reflections"][0]["observation_id"], proposal["id"])
        self.assertEqual(listed["reflections"][0]["status"], "CONFIRMED")


if __name__ == "__main__":
    unittest.main()
