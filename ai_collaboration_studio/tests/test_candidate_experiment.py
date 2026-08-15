from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch


# Keep this module safe when it is run directly with unittest as well as under
# pytest.  Application imports below can create a module-level default store.
_MODULE_RUNTIME = tempfile.TemporaryDirectory(
    prefix="ai-studio-candidate-experiment-tests-",
)
_MODULE_RUNTIME_PATH = Path(_MODULE_RUNTIME.name).resolve()
os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
os.environ["AI_STUDIO_RUNTIME_DIR"] = str(_MODULE_RUNTIME_PATH)
os.environ["AI_STUDIO_DATABASE_PATH"] = str(
    _MODULE_RUNTIME_PATH / "collection-default.sqlite3"
)

from backend import candidate_experiment as experiment_module  # noqa: E402
from backend.candidate_experiment import (  # noqa: E402
    CANDIDATE_EXPERIMENT_REQUEST_VERSION,
    CandidateExperimentError,
    CandidateExperimentService,
    normalize_candidate_experiment_request,
)
from backend.decision_lineage import canonical_sha256  # noqa: E402
from backend.market.futu_readonly import STORAGE_SYMBOLS  # noqa: E402
from backend.store import StudioStore  # noqa: E402
from backend.walk_forward import ENGINE_VERSION_V3, RESULT_VERSION_V3  # noqa: E402


ROOM_ID = "room_storage"
ATTESTATION_SHA256 = "a" * 64
EXPERIMENT_TABLES = (
    "candidate_experiment_authorizations",
    "candidate_experiment_cohorts",
    "candidate_experiment_input_seals",
    "candidate_experiment_arms",
)
SAFETY_FIELDS = {
    "execution_capability": "none",
    "live_trading_allowed": False,
    "can_autonomously_decide": False,
    "ranking_produced": False,
    "winner_claim": False,
    "user_final_decision_required": True,
}


def _candidate_fixture(index: int) -> dict[str, Any]:
    candidate_id = f"candidate_{index + 1}"
    symbol = STORAGE_SYMBOLS[index % len(STORAGE_SYMBOLS)]
    direction = "UP" if index % 2 == 0 else "DOWN"
    snapshot = {
        "title": f"候选 {index + 1}",
        "symbol": symbol,
        "direction": direction,
        "horizon_days": 20,
        "thesis": f"{candidate_id} 的离线历史研究论点",
        "invalidation": f"{candidate_id} 的明确失效条件",
    }
    snapshot_sha256 = canonical_sha256(snapshot)
    option = {
        "id": candidate_id,
        "title": snapshot["title"],
        "symbol": symbol,
        "direction": direction,
        "horizon_days": 20,
        "thesis": snapshot["thesis"],
        "invalidation": snapshot["invalidation"],
        "evidence": [{"id": f"evidence_{candidate_id}", "type": "message"}],
        "risks": [{"id": f"risk_{candidate_id}", "detail": "离线反证"}],
    }
    candidate = {
        "candidate_id": candidate_id,
        "candidate_revision": 1,
        "candidate_origin_message_id": f"message_{candidate_id}_origin",
        "candidate_latest_message_id": f"message_{candidate_id}_latest",
        "candidate_snapshot": snapshot,
        "candidate_snapshot_sha256": snapshot_sha256,
        "artifact_option_snapshot": option,
        "artifact_option_snapshot_sha256": canonical_sha256(option),
        "title": snapshot["title"],
        "symbol": symbol,
        "direction": direction,
        "side": "LONG" if direction == "UP" else "SHORT",
        "horizon_days": 20,
        "thesis": snapshot["thesis"],
        "invalidation": snapshot["invalidation"],
        "evidence": copy.deepcopy(option["evidence"]),
        "counterevidence": copy.deepcopy(option["risks"]),
        "risk_review": {
            "action": "challenge" if index % 2 else "support",
            "review_message_id": f"risk_review_{candidate_id}",
            "reviewer_member_id": "offline_risk_reviewer",
            "reviewer_member_version": 1,
            "risk_ids": [f"risk_{candidate_id}"],
            "disposition_only": True,
        },
        **SAFETY_FIELDS,
    }
    candidate["candidate_binding_sha256"] = canonical_sha256(candidate)
    return candidate


CANDIDATE_POOL = tuple(_candidate_fixture(index) for index in range(6))
CANDIDATES_BY_ID = {
    str(candidate["candidate_id"]): candidate for candidate in CANDIDATE_POOL
}


def make_request(
    artifact: Mapping[str, Any],
    arm_count: int,
    *,
    client_request_id: str,
    reverse: bool = False,
) -> dict[str, Any]:
    selected = list(CANDIDATE_POOL[:arm_count])
    if reverse:
        selected.reverse()
    return {
        "version": CANDIDATE_EXPERIMENT_REQUEST_VERSION,
        "client_request_id": client_request_id,
        "artifact_id": str(artifact["id"]),
        "expected_artifact_version": int(artifact["version"]),
        "expected_governance_attestation_sha256": ATTESTATION_SHA256,
        "candidate_selections": [
            {
                "candidate_id": candidate["candidate_id"],
                "expected_candidate_revision": candidate["candidate_revision"],
                "expected_candidate_origin_message_id": candidate[
                    "candidate_origin_message_id"
                ],
                "expected_candidate_latest_message_id": candidate[
                    "candidate_latest_message_id"
                ],
                "expected_candidate_snapshot_sha256": candidate[
                    "candidate_snapshot_sha256"
                ],
            }
            for candidate in selected
        ],
        "user_authorized_historical_comparison": True,
    }


class TinyBatchMarket:
    """Deterministic in-memory Futu-shaped data; it never opens a connection."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.history_batch_calls = 0

    @staticmethod
    def _history(symbol: str) -> dict[str, Any]:
        first_day = date(2025, 1, 1)
        rows = []
        for index in range(130):
            day = first_day + timedelta(days=index)
            close = float(100 + index)
            rows.append({
                "symbol": symbol,
                "market_time": f"{day.isoformat()} 16:00:00",
                "open": close,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": 10_000_000.0,
                "turnover": close * 10_000_000.0,
            })
        last_day = first_day + timedelta(days=len(rows) - 1)
        return {
            "ok": True,
            "source": "futu_opend",
            "interval": "1d",
            "price_adjustment": "QFQ",
            "captured_at": "2026-08-01T00:00:00.000Z",
            "as_of_date": "2026-08-01",
            "last_completed_session": last_day.isoformat(),
            "actual_start": first_day.isoformat(),
            "actual_end": last_day.isoformat(),
            "symbol": symbol,
            "rows": rows,
            "source_errors": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

    def history_batch(self, symbols: Any, **_kwargs: Any) -> dict[str, Any]:
        with self._lock:
            self.history_batch_calls += 1
        requested = tuple(symbols)
        return {
            "ok": True,
            "source": "futu_opend",
            "interval": "1d",
            "price_adjustment": "QFQ",
            "captured_at": "2026-08-01T00:00:00.000Z",
            "as_of_date": "2026-08-01",
            "symbols": list(requested),
            "histories": {
                symbol: self._history(symbol) for symbol in requested
            },
            "source_errors": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
        }


class RecordingEngine:
    """Pure deterministic engine double with object-identity recording."""

    def __init__(self, *, fail_on_call: int = 0) -> None:
        self.fail_on_call = int(fail_on_call)
        self._lock = threading.Lock()
        self.history_objects: list[Any] = []
        self.history_sha256s: list[str] = []

    def __call__(
        self,
        histories: Any,
        plan: Mapping[str, Any],
        config: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            self.history_objects.append(histories)
            self.history_sha256s.append(canonical_sha256(histories))
            call_number = len(self.history_objects)
        if self.fail_on_call and call_number == self.fail_on_call:
            raise RuntimeError(f"injected engine failure at arm call {call_number}")
        active = next(
            position
            for position in plan.get("positions") or []
            if float(position.get("weight_pct") or 0) > 0
        )
        symbol = str(active["symbol"])
        side = str(active["side"])
        score = (STORAGE_SYMBOLS.index(symbol) + 1) * (
            1 if side == "LONG" else -1
        )
        scenarios = []
        for friction_index, scenario_id in enumerate(
            ("baseline", "stressed", "severe")
        ):
            scenarios.append({
                "scenario_id": scenario_id,
                "state": "sufficient",
                "blocked": False,
                "formal_unfillable_fold_count": 0,
                "summary": {
                    "portfolio_cumulative_return_pct": score - friction_index,
                    "historical_positive_fold_ratio": 0.5,
                    "max_drawdown_pct": 2 + friction_index,
                    "mean_return_pct": score / 10,
                    "worst_return_pct": -3 - friction_index,
                },
            })
        return {
            "version": RESULT_VERSION_V3,
            "engine_version": ENGINE_VERSION_V3,
            "config": copy.deepcopy(dict(config)),
            "scenario_results": scenarios,
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
        }


class HarnessCandidateExperimentService(CandidateExperimentService):
    """Keeps governance deterministic while exercising the real P23 ledger."""

    def __init__(
        self,
        store: StudioStore,
        market_service: TinyBatchMarket,
        artifact: Mapping[str, Any],
        *,
        engine_runner: RecordingEngine,
        fault_injector: Any = None,
    ) -> None:
        super().__init__(
            store,
            market_service,
            engine_runner=engine_runner,
            fault_injector=fault_injector,
        )
        self.artifact = copy.deepcopy(dict(artifact))
        self.binding_epoch = 0
        self.context_calls = 0
        self._context_lock = threading.Lock()

    def _authorization_context(
        self,
        _connection: sqlite3.Connection,
        room_id: str,
        request: Mapping[str, Any],
        *,
        require_current: bool,
    ) -> dict[str, Any]:
        del require_current
        with self._context_lock:
            self.context_calls += 1
            epoch = self.binding_epoch
        candidates = []
        for selection in request.get("candidate_selections") or []:
            candidate = copy.deepcopy(
                CANDIDATES_BY_ID[str(selection["candidate_id"])]
            )
            if epoch:
                candidate["test_binding_epoch"] = epoch
                candidate["candidate_binding_sha256"] = canonical_sha256(
                    candidate
                )
            candidates.append(candidate)
        artifact_snapshot_sha256 = canonical_sha256({
            "artifact_id": self.artifact["id"],
            "artifact_version": self.artifact["version"],
            "test_binding_epoch": epoch,
        })
        binding = {
            "version": experiment_module.CANDIDATE_EXPERIMENT_AUTHORIZATION_BINDING_VERSION,
            "room_id": room_id,
            "artifact_id": str(request["artifact_id"]),
            "artifact_version": int(request["expected_artifact_version"]),
            "artifact_snapshot_sha256": artifact_snapshot_sha256,
            "governance_attestation_sha256": str(
                request["expected_governance_attestation_sha256"]
            ),
            "governance_projection_sha256": "b" * 64,
            "candidate_bindings": copy.deepcopy(candidates),
            "common_horizon_days": 20,
            "invalidation_conditions": [
                "artifact_exact_version_or_confirmation_changes_before_commit",
                "governance_attestation_or_projection_changes_before_commit",
                "candidate_revision_origin_latest_or_snapshot_changes_before_commit",
                "server_common_spec_or_dataset_seal_mismatch",
                "any_arm_or_aggregate_integrity_failure",
            ],
            "does_not_imply_artifact_support": True,
            "does_not_create_artifact_user_decision": True,
            **SAFETY_FIELDS,
        }
        return {
            "artifact": copy.deepcopy(self.artifact),
            "source_current": True,
            "governance": {
                "attestation_sha256": ATTESTATION_SHA256,
                "projection_sha256": "b" * 64,
            },
            "candidates": candidates,
            "common_horizon_days": 20,
            "artifact_snapshot_sha256": artifact_snapshot_sha256,
            "authorization_binding": binding,
            "authorization_binding_sha256": canonical_sha256(binding),
        }


class CandidateExperimentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="candidate-experiment-case-",
        )
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "candidate-experiment.sqlite3"
        self.store = StudioStore(self.db_path)
        artifact = self.store.create_artifact(
            ROOM_ID,
            title="P23 离线原子实验产物",
            content={
                "summary": "只用于 P23 临时 SQLite 测试。",
                "requirements": [],
                "risks": [],
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
            },
            created_by="offline_p23_test",
        )
        self.assertIsNotNone(artifact)
        self.artifact = artifact or {}

    def _service(
        self,
        *,
        market: TinyBatchMarket | None = None,
        engine: RecordingEngine | None = None,
        fault_injector: Any = None,
    ) -> tuple[HarnessCandidateExperimentService, TinyBatchMarket, RecordingEngine]:
        selected_market = market or TinyBatchMarket()
        selected_engine = engine or RecordingEngine()
        return (
            HarnessCandidateExperimentService(
                self.store,
                selected_market,
                self.artifact,
                engine_runner=selected_engine,
                fault_injector=fault_injector,
            ),
            selected_market,
            selected_engine,
        )

    def test_mixed_football_room_is_rejected_before_artifact_or_market_access(self) -> None:
        mixed = self.store.create_room(
            "Mixed football and storage",
            "Fail closed at the domain boundary",
            capability_pack_ids=[
                "storage_research_readonly",
                "football_research_readonly",
            ],
        )["room"]
        market = TinyBatchMarket()
        service = CandidateExperimentService(self.store, market)
        request = {
            "artifact_id": "football_artifact_must_not_be_read",
            "expected_artifact_version": 1,
        }

        with patch.object(
            service,
            "_artifact_for_version",
            side_effect=AssertionError("artifact access must remain sealed"),
        ) as artifact_access, closing(self.store._connect()) as connection:
            with self.assertRaises(CandidateExperimentError) as caught:
                service._authorization_context(
                    connection,
                    mixed["id"],
                    request,
                    require_current=True,
                )

        artifact_access.assert_not_called()
        self.assertEqual(
            caught.exception.code,
            "CANDIDATE_EXPERIMENT_DOMAIN_NOT_STORAGE_ONLY",
        )
        self.assertEqual(market.history_batch_calls, 0)

    def test_mixed_generic_stock_room_is_rejected_before_artifact_or_market_access(self) -> None:
        mixed = self.store.create_room(
            "Mixed generic-stock and storage",
            "The generic read-only stock pack is not a storage experiment authority.",
            capability_pack_ids=[
                "storage_research_readonly",
                "stock_research_readonly",
            ],
            stock_room_scope={
                "version": "stock_room_scope_v1",
                "symbols": ["US:AAPL"],
            },
        )["room"]
        market = TinyBatchMarket()
        service = CandidateExperimentService(self.store, market)

        with patch.object(
            service,
            "_artifact_for_version",
            side_effect=AssertionError("artifact access must remain sealed"),
        ) as artifact_access, closing(self.store._connect()) as connection:
            with self.assertRaises(CandidateExperimentError) as caught:
                service._authorization_context(
                    connection,
                    mixed["id"],
                    {"artifact_id": "stock_artifact_must_not_be_read", "expected_artifact_version": 1},
                    require_current=True,
                )

        artifact_access.assert_not_called()
        self.assertEqual(
            caught.exception.code,
            "CANDIDATE_EXPERIMENT_DOMAIN_NOT_STORAGE_ONLY",
        )
        self.assertEqual(market.history_batch_calls, 0)

    def test_frozen_mixed_artifact_remains_rejected_after_football_pack_removal(self) -> None:
        mixed = self.store.create_room(
            "Frozen mixed football artifact",
            "Removing a pack later must not rewrite artifact provenance",
            capability_pack_ids=[
                "storage_research_readonly",
                "football_research_readonly",
            ],
        )["room"]
        artifact = self.store.create_artifact(
            mixed["id"],
            title="Frozen mixed-domain artifact",
            content={
                "summary": "A mixed-domain provenance fixture.",
                "requirements": [],
                "risks": [],
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
            },
            created_by="offline_candidate_domain_test",
        )
        self.assertIsNotNone(artifact)
        with closing(self.store._connect()) as connection:
            artifact_row = connection.execute(
                "SELECT * FROM artifacts WHERE room_id=? AND id=?",
                (mixed["id"], str((artifact or {})["id"])),
            ).fetchone()
            self.assertIsNotNone(artifact_row)
            frozen_artifact = self.store._artifact_dict(artifact_row)
        updated = self.store.update_room(
            mixed["id"],
            {
                "expected_settings_version": mixed["settings_version"],
                "capability_pack_ids": ["storage_research_readonly"],
            },
        )
        self.assertNotIn(
            "football_research_readonly",
            (updated or {}).get("active_capability_pack_ids") or [],
        )

        market = TinyBatchMarket()
        service = CandidateExperimentService(self.store, market)
        request = {
            "artifact_id": str((artifact or {})["id"]),
            "expected_artifact_version": int((artifact or {})["version"]),
        }
        with patch.object(
            service,
            "_artifact_for_version",
            return_value=(copy.deepcopy(frozen_artifact), False),
        ), closing(self.store._connect()) as connection:
            with self.assertRaises(CandidateExperimentError) as caught:
                service._authorization_context(
                    connection,
                    mixed["id"],
                    request,
                    require_current=False,
                )

        self.assertEqual(
            caught.exception.code,
            "CANDIDATE_EXPERIMENT_DOMAIN_NOT_STORAGE_ONLY",
        )
        self.assertEqual(market.history_batch_calls, 0)

    def test_frozen_generic_stock_artifact_remains_rejected_after_pack_removal(self) -> None:
        mixed = self.store.create_room(
            "Frozen mixed generic-stock artifact",
            "Removing the stock pack must not rewrite artifact provenance.",
            capability_pack_ids=[
                "storage_research_readonly",
                "stock_research_readonly",
            ],
            stock_room_scope={
                "version": "stock_room_scope_v1",
                "symbols": ["US:AAPL"],
            },
        )["room"]
        artifact = self.store.create_artifact(
            mixed["id"],
            title="Frozen mixed generic-stock artifact",
            content={
                "summary": "A mixed-domain provenance fixture.",
                "requirements": [],
                "risks": [],
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
            },
            created_by="offline_candidate_stock_domain_test",
        )
        self.assertIsNotNone(artifact)
        with closing(self.store._connect()) as connection:
            artifact_row = connection.execute(
                "SELECT * FROM artifacts WHERE room_id=? AND id=?",
                (mixed["id"], str((artifact or {})["id"])),
            ).fetchone()
            self.assertIsNotNone(artifact_row)
            frozen_artifact = self.store._artifact_dict(artifact_row)
        updated = self.store.update_room(
            mixed["id"],
            {
                "expected_settings_version": mixed["settings_version"],
                "capability_pack_ids": ["storage_research_readonly"],
            },
        )
        self.assertNotIn(
            "stock_research_readonly",
            (updated or {}).get("active_capability_pack_ids") or [],
        )

        market = TinyBatchMarket()
        service = CandidateExperimentService(self.store, market)
        with patch.object(
            service,
            "_artifact_for_version",
            return_value=(copy.deepcopy(frozen_artifact), False),
        ), closing(self.store._connect()) as connection:
            with self.assertRaises(CandidateExperimentError) as caught:
                service._authorization_context(
                    connection,
                    mixed["id"],
                    {
                        "artifact_id": str((artifact or {})["id"]),
                        "expected_artifact_version": int((artifact or {})["version"]),
                    },
                    require_current=False,
                )

        self.assertEqual(
            caught.exception.code,
            "CANDIDATE_EXPERIMENT_DOMAIN_NOT_STORAGE_ONLY",
        )
        self.assertEqual(market.history_batch_calls, 0)

    def _table_counts(self) -> dict[str, int]:
        with closing(self.store._connect()) as connection:
            return {
                table: int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table}"  # noqa: S608
                    ).fetchone()[0]
                )
                for table in EXPERIMENT_TABLES
            }

    def _provider_counts(self) -> tuple[int, int]:
        with closing(self.store._connect()) as connection:
            return (
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM provider_execution_runs"
                    ).fetchone()[0]
                ),
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM provider_call_attempts"
                    ).fetchone()[0]
                ),
            )

    def _decision_count(self) -> int:
        with closing(self.store._connect()) as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM artifact_user_decisions"
                ).fetchone()[0]
            )

    def assert_all_metrics_hidden(self, experiment: Mapping[str, Any]) -> None:
        self.assertFalse(experiment["integrity_ok"])
        self.assertFalse(experiment["metrics_visible"])
        self.assertEqual(experiment["status"], "integrity_failed")
        self.assertEqual(experiment["authorization"], {})
        self.assertEqual(experiment["common_spec"], {})
        self.assertEqual(experiment["dataset_seal"], {})
        for key, expected in SAFETY_FIELDS.items():
            self.assertEqual(experiment[key], expected, key)
        self.assertGreaterEqual(len(experiment["arms"]), 2)
        for arm in experiment["arms"]:
            self.assertFalse(arm["integrity_ok"])
            self.assertFalse(arm["metrics_visible"])
            for key, expected in SAFETY_FIELDS.items():
                self.assertEqual(arm[key], expected, key)
            for forbidden in (
                "attacker_payload",
                "future_win_rate",
                "histories",
                "plan",
                "result",
            ):
                self.assertNotIn(forbidden, arm)
            self.assertEqual(
                [scenario["scenario_id"] for scenario in arm["scenarios"]],
                ["baseline", "stressed", "severe"],
            )
            for scenario in arm.get("scenarios") or []:
                self.assertFalse(scenario["metrics_visible"])
                self.assertIsNone(scenario["capacity_gap_usd"])
                self.assertIsNone(scenario["first_blocker"])
                self.assertTrue(
                    all(
                        value is None
                        for value in (scenario.get("metrics") or {}).values()
                    )
                )

    def test_request_uses_strict_top_level_and_selection_whitelists(self) -> None:
        request = make_request(
            self.artifact,
            2,
            client_request_id="p23-strict-request",
        )
        self.assertEqual(normalize_candidate_experiment_request(request), request)

        forbidden_fields = (
            "cutoff_date",
            "engine",
            "paper_weight_pct",
            "provider",
            "ranking_rule",
            "user_decision",
        )
        for field in forbidden_fields:
            with self.subTest(field=field):
                invalid = copy.deepcopy(request)
                invalid[field] = "client-controlled"
                with self.assertRaises(CandidateExperimentError) as raised:
                    normalize_candidate_experiment_request(invalid)
                self.assertEqual(
                    raised.exception.code,
                    "CANDIDATE_EXPERIMENT_REQUEST_INVALID",
                )
                self.assertEqual(raised.exception.status, 400)

        invalid_selection = copy.deepcopy(request)
        invalid_selection["candidate_selections"][0]["target_weight_pct"] = 90
        with self.assertRaises(CandidateExperimentError) as raised:
            normalize_candidate_experiment_request(invalid_selection)
        self.assertEqual(
            raised.exception.code,
            "CANDIDATE_EXPERIMENT_SELECTION_INVALID",
        )
        self.assertEqual(raised.exception.status, 400)

        for target, field, expected_code in (
            (
                request,
                "expected_artifact_version",
                "CANDIDATE_EXPERIMENT_ARTIFACT_VERSION_INVALID",
            ),
            (
                request["candidate_selections"][0],
                "expected_candidate_revision",
                "CANDIDATE_EXPERIMENT_CANDIDATE_REVISION_INVALID",
            ),
        ):
            with self.subTest(oversized_integer_field=field):
                oversized = copy.deepcopy(request)
                destination = (
                    oversized
                    if target is request
                    else oversized["candidate_selections"][0]
                )
                destination[field] = "9" * 1000
                with self.assertRaises(CandidateExperimentError) as raised:
                    normalize_candidate_experiment_request(oversized)
                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(raised.exception.status, 400)

    def test_two_three_and_six_arms_share_exact_common_fingerprints(self) -> None:
        service, market, engine = self._service()
        provider_before = self._provider_counts()
        shared_spec_sha256s = set()
        shared_dataset_sha256s = set()
        previous_market_reads = 0

        for arm_count in (2, 3, 6):
            with self.subTest(arm_count=arm_count):
                engine_start = len(engine.history_objects)
                result = service.run(
                    ROOM_ID,
                    make_request(
                        self.artifact,
                        arm_count,
                        client_request_id=f"p23-fingerprint-{arm_count}",
                    ),
                )
                self.assertTrue(result["integrity_ok"])
                self.assertEqual(len(result["arms"]), arm_count)
                self.assertEqual(market.history_batch_calls, previous_market_reads + 1)
                previous_market_reads = market.history_batch_calls
                shared_spec_sha256s.add(result["spec_sha256"])
                shared_dataset_sha256s.add(result["dataset_seal_sha256"])
                self.assertEqual(
                    {arm["shared_spec_sha256"] for arm in result["arms"]},
                    {result["spec_sha256"]},
                )
                self.assertEqual(
                    {
                        arm["shared_dataset_seal_sha256"]
                        for arm in result["arms"]
                    },
                    {result["dataset_seal_sha256"]},
                )
                self.assertEqual(
                    len({arm["candidate_id"] for arm in result["arms"]}),
                    arm_count,
                )
                self.assertEqual(result["market_data_reads"], 1)
                self.assertEqual(result["provider_calls_total"], 0)
                self.assertEqual(result["openai_calls"], 0)
                for field, expected in SAFETY_FIELDS.items():
                    self.assertEqual(result[field], expected)
                    self.assertTrue(
                        all(arm[field] == expected for arm in result["arms"])
                    )

                # run() first computes N arms from one frozen object, then its
                # integrity readback recomputes N arms from one rehydrated
                # object.  Both batches share the exact dataset fingerprint.
                calls = engine.history_objects[engine_start:]
                hashes = engine.history_sha256s[engine_start:]
                self.assertEqual(len(calls), arm_count * 2)
                self.assertEqual(len({id(value) for value in calls[:arm_count]}), 1)
                self.assertEqual(len({id(value) for value in calls[arm_count:]}), 1)
                self.assertEqual(set(hashes), {result["dataset_seal"]["dataset_content_sha256"]})

        self.assertEqual(len(shared_spec_sha256s), 1)
        self.assertEqual(len(shared_dataset_sha256s), 1)
        self.assertEqual(self._provider_counts(), provider_before)

    def test_idempotent_retry_skips_market_and_changed_semantics_conflict(self) -> None:
        service, market, _engine = self._service()
        request = make_request(
            self.artifact,
            3,
            client_request_id="p23-idempotent-request",
        )
        first = service.run(ROOM_ID, request)
        self.assertFalse(first["idempotent_replay"])
        self.assertEqual(market.history_batch_calls, 1)

        replay = service.run(ROOM_ID, copy.deepcopy(request))
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["id"], first["id"])
        self.assertEqual(
            replay["request_semantics_sha256"],
            first["request_semantics_sha256"],
        )
        self.assertEqual(market.history_batch_calls, 1)
        self.assertEqual(self._table_counts()["candidate_experiment_cohorts"], 1)

        conflict = make_request(
            self.artifact,
            3,
            client_request_id="p23-idempotent-request",
            reverse=True,
        )
        with self.assertRaises(CandidateExperimentError) as raised:
            service.run(ROOM_ID, conflict)
        self.assertEqual(
            raised.exception.code,
            "CANDIDATE_EXPERIMENT_IDEMPOTENCY_CONFLICT",
        )
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(market.history_batch_calls, 1)
        self.assertEqual(self._table_counts()["candidate_experiment_cohorts"], 1)

    def test_binding_drift_after_market_freeze_writes_nothing(self) -> None:
        service, market, _engine = self._service()

        def drift_after_freeze(stage: str, _context: Mapping[str, Any]) -> None:
            if stage == "after_market_freeze":
                service.binding_epoch += 1

        service.fault_injector = drift_after_freeze
        with self.assertRaises(CandidateExperimentError) as raised:
            service.run(
                ROOM_ID,
                make_request(
                    self.artifact,
                    2,
                    client_request_id="p23-drift-after-freeze",
                ),
            )
        self.assertEqual(raised.exception.code, "CANDIDATE_EXPERIMENT_BINDING_DRIFT")
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(market.history_batch_calls, 1)
        self.assertEqual(self._table_counts(), {table: 0 for table in EXPERIMENT_TABLES})
        self.assertEqual(self._decision_count(), 0)

    def test_engine_failure_on_nth_arm_leaves_no_business_rows(self) -> None:
        engine = RecordingEngine(fail_on_call=3)
        service, market, _engine = self._service(engine=engine)
        with self.assertRaisesRegex(RuntimeError, "arm call 3"):
            service.run(
                ROOM_ID,
                make_request(
                    self.artifact,
                    6,
                    client_request_id="p23-engine-failure-arm-three",
                ),
            )
        self.assertEqual(market.history_batch_calls, 1)
        self.assertEqual(len(engine.history_objects), 3)
        self.assertEqual(self._table_counts(), {table: 0 for table in EXPERIMENT_TABLES})
        self.assertEqual(self._provider_counts(), (0, 0))
        self.assertEqual(self._decision_count(), 0)

    def test_faults_after_each_atomic_insert_stage_roll_back_every_table(self) -> None:
        stages = (
            "after_authorization_insert",
            "after_cohort_insert",
            "after_input_seal_insert",
            "after_arm_insert",
        )
        for index, target_stage in enumerate(stages):
            with self.subTest(stage=target_stage):
                def inject(
                    stage: str,
                    _context: Mapping[str, Any],
                    *,
                    expected: str = target_stage,
                ) -> None:
                    if stage == expected:
                        raise RuntimeError(f"injected transaction fault: {stage}")

                service, _market, _engine = self._service(
                    fault_injector=inject,
                )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected transaction fault",
                ):
                    service.run(
                        ROOM_ID,
                        make_request(
                            self.artifact,
                            3,
                            client_request_id=f"p23-transaction-fault-{index}",
                        ),
                    )
                self.assertEqual(
                    self._table_counts(),
                    {table: 0 for table in EXPERIMENT_TABLES},
                )
                self.assertEqual(self._decision_count(), 0)

    def test_concurrent_same_request_id_creates_one_cohort(self) -> None:
        service, market, _engine = self._service()
        request = make_request(
            self.artifact,
            3,
            client_request_id="p23-concurrent-request",
        )
        gate = threading.Barrier(3)
        results: list[dict[str, Any]] = []
        errors: list[BaseException] = []
        result_lock = threading.Lock()

        def worker() -> None:
            gate.wait(timeout=5)
            try:
                result = service.run(ROOM_ID, copy.deepcopy(request))
                with result_lock:
                    results.append(result)
            except BaseException as exc:  # pragma: no cover - asserted below
                with result_lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        gate.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=15)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual({result["id"] for result in results}, {results[0]["id"]})
        self.assertEqual(
            sorted(result["idempotent_replay"] for result in results),
            [False, True],
        )
        self.assertEqual(market.history_batch_calls, 1)
        counts = self._table_counts()
        self.assertEqual(counts["candidate_experiment_authorizations"], 1)
        self.assertEqual(counts["candidate_experiment_cohorts"], 1)
        self.assertEqual(counts["candidate_experiment_input_seals"], 1)
        self.assertEqual(counts["candidate_experiment_arms"], 3)
        self.assertEqual(self._provider_counts(), (0, 0))

    def test_tampering_input_arm_result_or_aggregate_hides_all_metrics(self) -> None:
        service, _market, _engine = self._service()
        experiments = {}
        for tamper_kind in (
            "input",
            "arm",
            "result",
            "aggregate",
            "dataset_shape",
            "dataset_infinite",
            "huge_metric",
            "nonfinite_json",
            "timestamp_authorization",
            "timestamp_cohort",
            "timestamp_input",
            "timestamp_arm",
            "deep_json",
        ):
            experiments[tamper_kind] = service.run(
                ROOM_ID,
                make_request(
                    self.artifact,
                    2,
                    client_request_id=f"p23-tamper-{tamper_kind}",
                ),
            )

        with closing(self.store._connect()) as connection, connection:
            for trigger in (
                "trg_candidate_experiment_authorizations_no_update",
                "trg_candidate_experiment_input_seals_no_update",
                "trg_candidate_experiment_arms_no_update",
                "trg_candidate_experiment_cohorts_no_update",
            ):
                connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")

            input_id = experiments["input"]["id"]
            input_row = connection.execute(
                "SELECT spec_json FROM candidate_experiment_input_seals WHERE cohort_id=?",
                (input_id,),
            ).fetchone()
            input_spec = json.loads(str(input_row["spec_json"]))
            input_spec["tampered"] = True
            connection.execute(
                "UPDATE candidate_experiment_input_seals SET spec_json=? WHERE cohort_id=?",
                (json.dumps(input_spec, sort_keys=True), input_id),
            )

            arm_id = experiments["arm"]["id"]
            arm_row = connection.execute(
                "SELECT id,arm_json FROM candidate_experiment_arms WHERE cohort_id=? ORDER BY sequence_no LIMIT 1",
                (arm_id,),
            ).fetchone()
            arm_json = json.loads(str(arm_row["arm_json"]))
            arm_json["public"]["title"] = "tampered arm projection"
            arm_json["public"]["execution_capability"] = "orders"
            arm_json["public"]["live_trading_allowed"] = True
            arm_json["public"]["can_autonomously_decide"] = True
            arm_json["public"]["ranking_produced"] = True
            arm_json["public"]["winner_claim"] = True
            arm_json["public"]["user_final_decision_required"] = False
            arm_json["public"]["future_win_rate"] = 0.99
            arm_json["public"]["attacker_payload"] = {
                "histories": ["must not be reflected"],
                "result": {"raw": True},
            }
            arm_json["public"]["scenarios"] = ["malformed scenario"]
            connection.execute(
                "UPDATE candidate_experiment_arms SET arm_json=? WHERE id=?",
                (json.dumps(arm_json, sort_keys=True), arm_row["id"]),
            )

            result_id = experiments["result"]["id"]
            result_row = connection.execute(
                "SELECT id,result_json FROM candidate_experiment_arms WHERE cohort_id=? ORDER BY sequence_no LIMIT 1",
                (result_id,),
            ).fetchone()
            result_json = json.loads(str(result_row["result_json"]))
            result_json["scenario_results"][0]["summary"][
                "portfolio_cumulative_return_pct"
            ] = 999999
            connection.execute(
                "UPDATE candidate_experiment_arms SET result_json=? WHERE id=?",
                (json.dumps(result_json, sort_keys=True), result_row["id"]),
            )

            aggregate_id = experiments["aggregate"]["id"]
            connection.execute(
                "UPDATE candidate_experiment_cohorts SET aggregate_sha256=? WHERE id=?",
                ("f" * 64, aggregate_id),
            )

            dataset_id = experiments["dataset_shape"]["id"]
            dataset_row = connection.execute(
                "SELECT dataset_json FROM candidate_experiment_input_seals WHERE cohort_id=?",
                (dataset_id,),
            ).fetchone()
            dataset_json = json.loads(str(dataset_row["dataset_json"]))
            dataset_json["histories"]["US.MU"]["rows"][0] = (
                "malformed persisted row"
            )
            connection.execute(
                "UPDATE candidate_experiment_input_seals SET dataset_json=? WHERE cohort_id=?",
                (json.dumps(dataset_json, sort_keys=True), dataset_id),
            )

            infinite_id = experiments["dataset_infinite"]["id"]
            infinite_row = connection.execute(
                "SELECT dataset_json FROM candidate_experiment_input_seals WHERE cohort_id=?",
                (infinite_id,),
            ).fetchone()
            infinite_json = str(infinite_row["dataset_json"])
            infinite_replacement = infinite_json.replace(
                '"close":100.0',
                '"close":1e999',
                1,
            )
            self.assertNotEqual(infinite_replacement, infinite_json)
            connection.execute(
                "UPDATE candidate_experiment_input_seals SET dataset_json=? WHERE cohort_id=?",
                (infinite_replacement, infinite_id),
            )

            huge_metric_id = experiments["huge_metric"]["id"]
            huge_metric_row = connection.execute(
                "SELECT id,result_json FROM candidate_experiment_arms WHERE cohort_id=? ORDER BY sequence_no LIMIT 1",
                (huge_metric_id,),
            ).fetchone()
            huge_metric_json = json.loads(str(huge_metric_row["result_json"]))
            huge_metric_json["scenario_results"][0]["summary"][
                "portfolio_cumulative_return_pct"
            ] = 10 ** 1000
            connection.execute(
                "UPDATE candidate_experiment_arms SET result_json=? WHERE id=?",
                (
                    json.dumps(huge_metric_json, sort_keys=True),
                    huge_metric_row["id"],
                ),
            )

            nonfinite_id = experiments["nonfinite_json"]["id"]
            connection.execute(
                "UPDATE candidate_experiment_cohorts SET aggregate_json=? WHERE id=?",
                ('{"version":NaN,"future_win_rate":1}', nonfinite_id),
            )

            timestamp_authorization_id = experiments[
                "timestamp_authorization"
            ]["id"]
            connection.execute(
                """UPDATE candidate_experiment_authorizations
                   SET created_at=created_at+1
                   WHERE id=(SELECT authorization_id
                             FROM candidate_experiment_cohorts WHERE id=?)""",
                (timestamp_authorization_id,),
            )
            timestamp_cohort_id = experiments["timestamp_cohort"]["id"]
            connection.execute(
                "UPDATE candidate_experiment_cohorts SET created_at=created_at+1 WHERE id=?",
                (timestamp_cohort_id,),
            )
            timestamp_input_id = experiments["timestamp_input"]["id"]
            connection.execute(
                "UPDATE candidate_experiment_input_seals SET created_at=created_at+1 WHERE cohort_id=?",
                (timestamp_input_id,),
            )
            timestamp_arm_id = experiments["timestamp_arm"]["id"]
            connection.execute(
                """UPDATE candidate_experiment_arms SET created_at=created_at+1
                   WHERE id=(SELECT id FROM candidate_experiment_arms
                             WHERE cohort_id=? ORDER BY sequence_no LIMIT 1)""",
                (timestamp_arm_id,),
            )

            deep_json_id = experiments["deep_json"]["id"]
            deep_json = ('{"nested":' * 1200) + "0" + ("}" * 1200)
            connection.execute(
                """UPDATE candidate_experiment_arms SET arm_json=?
                   WHERE id=(SELECT id FROM candidate_experiment_arms
                             WHERE cohort_id=? ORDER BY sequence_no LIMIT 1)""",
                (deep_json, deep_json_id),
            )

        for tamper_kind, original in experiments.items():
            with self.subTest(tamper_kind=tamper_kind):
                corrupted = service.get(ROOM_ID, original["id"])
                self.assert_all_metrics_hidden(corrupted)

    def test_experiment_authorization_is_independent_from_user_decision(self) -> None:
        before = self._decision_count()
        service, _market, _engine = self._service()
        result = service.run(
            ROOM_ID,
            make_request(
                self.artifact,
                3,
                client_request_id="p23-decision-independence",
            ),
        )
        self.assertTrue(result["integrity_ok"])
        self.assertEqual(self._decision_count(), before)
        self.assertTrue(
            result["authorization"]["does_not_imply_artifact_support"]
        )
        self.assertTrue(
            result["authorization"]["does_not_create_artifact_user_decision"]
        )
        self.assertTrue(result["user_final_decision_required"])
        self.assertFalse(result["ranking_produced"])
        self.assertFalse(result["winner_claim"])

    def test_user_can_still_support_the_lower_historical_arm_after_experiment(
        self,
    ) -> None:
        from tests import test_artifact_governance_store as governance_fixture

        governance_case = governance_fixture.ArtifactGovernanceStoreTests(
            "test_user_can_select_non_ai_preferred_governed_candidate"
        )
        governance_case.setUp()
        original_candidate = governance_fixture._candidate

        def experiment_candidate(*args: Any, **kwargs: Any) -> dict[str, Any]:
            candidate = original_candidate(*args, **kwargs)
            candidate_id = str(candidate.get("id") or "")
            candidate["direction"] = (
                "UP" if candidate_id == "candidate_a" else "DOWN"
            )
            return candidate

        try:
            with patch.object(
                governance_fixture,
                "_candidate",
                side_effect=experiment_candidate,
            ):
                artifact, context, attestation = (
                    governance_case._confirm_governed()
                )
                with governance_case._governance_patches(context):
                    projected = governance_case.store.get_artifact(
                        "room_storage",
                        artifact["id"],
                    )["governance_snapshot"]
                    bindings = [
                        governance_case.store._governed_exact_candidate_binding(
                            projected,
                            candidate_id,
                        )
                        for candidate_id in ("candidate_a", "candidate_b")
                    ]
                    request = {
                        "version": CANDIDATE_EXPERIMENT_REQUEST_VERSION,
                        "client_request_id": "p23-lower-arm-user-decision",
                        "artifact_id": artifact["id"],
                        "expected_artifact_version": artifact["version"],
                        "expected_governance_attestation_sha256": attestation[
                            "attestation_sha256"
                        ],
                        "candidate_selections": [
                            {
                                "candidate_id": candidate_id,
                                "expected_candidate_revision": binding[
                                    "selected_option_revision"
                                ],
                                "expected_candidate_origin_message_id": binding[
                                    "selected_option_origin_message_id"
                                ],
                                "expected_candidate_latest_message_id": binding[
                                    "selected_option_latest_message_id"
                                ],
                                "expected_candidate_snapshot_sha256": binding[
                                    "selected_candidate_snapshot_sha256"
                                ],
                            }
                            for candidate_id, binding in zip(
                                ("candidate_a", "candidate_b"),
                                bindings,
                                strict=True,
                            )
                        ],
                        "user_authorized_historical_comparison": True,
                    }
                    engine = RecordingEngine()
                    experiment = CandidateExperimentService(
                        governance_case.store,
                        TinyBatchMarket(),
                        engine_runner=engine,
                    ).run("room_storage", request)
                    baseline_by_candidate = {
                        arm["candidate_id"]: arm["scenarios"][0]["metrics"][
                            "portfolio_cumulative_return_pct"
                        ]
                        for arm in experiment["arms"]
                    }
                    self.assertGreater(
                        baseline_by_candidate["candidate_a"],
                        baseline_by_candidate["candidate_b"],
                    )

                    decision = governance_case.store.create_artifact_user_decision(
                        "room_storage",
                        artifact["id"],
                        expected_version=artifact["version"],
                        action="support",
                        rationale=(
                            "用户基于自身约束选择候选 B，历史收益不是自动排名。"
                        ),
                        **governance_case._governed_support_tokens(
                            context,
                            attestation,
                            "candidate_b",
                        ),
                    )
                    reread = CandidateExperimentService(
                        governance_case.store,
                        TinyBatchMarket(),
                        engine_runner=RecordingEngine(),
                    ).get("room_storage", experiment["id"])

                self.assertEqual(decision["action"], "support")
                self.assertEqual(decision["selected_option_id"], "candidate_b")
                self.assertTrue(reread["integrity_ok"])
                self.assertTrue(reread["metrics_visible"])
                self.assertFalse(reread["ranking_produced"])
                self.assertFalse(reread["winner_claim"])
        finally:
            governance_case.doCleanups()


if __name__ == "__main__":
    unittest.main()
