import copy
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend import http_server
from backend.candidate_comparison import (
    CANDIDATE_COMPARISON_PREVIEW_VERSION,
    CANDIDATE_COMPARISON_REQUEST_VERSION,
    CandidateComparisonError,
    CandidateComparisonService,
    build_candidate_comparison_preview,
    normalize_candidate_comparison_request,
)
from backend.candidate_simulation_contract import (
    CANDIDATE_SIMULATION_CONFIRMATION_VERSION,
    CANDIDATE_SIMULATION_RULE_ID,
    build_candidate_simulation_contract,
    build_candidate_simulation_seed,
)
from backend.decision_lineage import canonical_sha256
from backend.market.futu_readonly import STORAGE_SYMBOLS
from backend.paper_portfolio import default_paper_portfolio_plan
from backend.paper_portfolio_service import PaperPortfolioService
from backend.store import StudioStore
from backend.walk_forward import (
    CONFIG_VERSION_V2,
    ENGINE_VERSION_V3,
    INPUT_SNAPSHOT_VERSION_V2,
    RESULT_VERSION_V3,
)
from backend.walk_forward_friction import (
    STORAGE_FRICTION_SCENARIOS_VERSION,
    UNFILLABLE_POLICY,
    get_storage_friction_scenarios,
)
from tests.test_walk_forward_integration import (
    FakeWalkForwardMarket,
    create_actionable_portfolio,
    futu_history,
)


def request_payload(*run_ids: str) -> dict:
    return {
        "version": CANDIDATE_COMPARISON_REQUEST_VERSION,
        "run_ids": list(run_ids),
        "user_confirmed_historical_only": True,
    }


def candidate_anchor(
    candidate_id: str,
    symbol: str,
    direction: str,
    *,
    horizon_days: int = 20,
) -> dict:
    snapshot = {
        "title": f"{candidate_id} historical replay",
        "symbol": symbol,
        "direction": direction,
        "horizon_days": horizon_days,
        "thesis": f"{candidate_id} bounded research thesis",
        "invalidation": f"{candidate_id} explicit invalidation",
    }
    return {
        "user_decision_id": f"decision_{candidate_id}",
        "artifact_id": "artifact_comparison",
        "artifact_version": 3,
        "artifact_snapshot_sha256": "1" * 64,
        "decision_version": "artifact_user_decision_v2",
        "decision_record_sha256": "2" * 64,
        "action": "support",
        "ai_preferred_option_id": "candidate_a",
        "selected_option_id": candidate_id,
        "selected_option_revision": 1,
        "selected_option_origin_message_id": f"message_{candidate_id}_origin",
        "selected_option_latest_message_id": f"message_{candidate_id}_latest",
        "selected_option_snapshot_sha256": "3" * 64,
        "governance_attestation_sha256": "4" * 64,
        "selected_candidate_snapshot": snapshot,
        "selected_candidate_snapshot_sha256": canonical_sha256(snapshot),
        "integrity_ok": True,
        "current": True,
    }


def candidate_plan(anchor: dict, *, weight: float = 25) -> dict:
    snapshot = anchor["selected_candidate_snapshot"]
    target_symbol = snapshot["symbol"]
    target_side = "LONG" if snapshot["direction"] == "UP" else "SHORT"
    plan = default_paper_portfolio_plan()
    plan["name"] = f"{anchor['selected_option_id']} paper replay"
    for position in plan["positions"]:
        if position["symbol"] == target_symbol:
            position.update({
                "side": target_side,
                "weight_pct": weight,
                "thesis": snapshot["thesis"],
                "invalidation": snapshot["invalidation"],
            })
    return plan


def candidate_contract(anchor: dict, *, weight: float = 25) -> dict:
    seed = build_candidate_simulation_seed(anchor)
    return build_candidate_simulation_contract(
        anchor,
        candidate_plan(anchor, weight=weight),
        {
            "version": CANDIDATE_SIMULATION_CONFIRMATION_VERSION,
            "expected_source_sha256": seed["source_sha256"],
            "expected_candidate_revision": seed["candidate_revision"],
            "expected_candidate_snapshot_sha256": seed[
                "candidate_snapshot_sha256"
            ],
            "expected_target_weight_pct": weight,
            "strategy_rule_id": CANDIDATE_SIMULATION_RULE_ID,
            "user_confirmed": True,
        },
    )


def shared_histories() -> dict:
    return {
        symbol: futu_history(
            symbol,
            days=500,
            daily_return=0.0004 * (index + 1),
        )
        for index, symbol in enumerate(STORAGE_SYMBOLS)
    }


def comparison_record(
    candidate_id: str,
    symbol: str,
    direction: str,
    *,
    histories: dict,
    weight: float = 25,
    horizon_days: int = 20,
    return_offset: float = 0,
) -> dict:
    anchor = candidate_anchor(
        candidate_id,
        symbol,
        direction,
        horizon_days=horizon_days,
    )
    contract = candidate_contract(anchor, weight=weight)
    config = {
        "version": CONFIG_VERSION_V2,
        "train_days": 99,
        "test_days": horizon_days,
        "step_days": horizon_days,
        "price_adjustment": "QFQ",
        "friction_scenario_set": STORAGE_FRICTION_SCENARIOS_VERSION,
        "unfillable_policy": UNFILLABLE_POLICY,
    }
    scenario_results = []
    for index, scenario_id in enumerate(("baseline", "stressed", "severe")):
        scenario_results.append({
            "scenario_id": scenario_id,
            "state": "sufficient",
            "blocked": False,
            "formal_unfillable_fold_count": 0,
            "summary": {
                "portfolio_cumulative_return_pct": 8 + return_offset - index,
                "historical_positive_fold_ratio": 0.55 + return_offset / 100,
                "max_drawdown_pct": 4 + index,
                "mean_return_pct": 0.6 + return_offset / 10,
                "worst_return_pct": -1.5 - index,
            },
        })
    run_id = f"run_{candidate_id}"
    return {
        "run": {
            "id": run_id,
            "room_id": "room_storage",
            "portfolio_id": f"portfolio_{candidate_id}",
            "portfolio_version": 1,
            "record_version": 2,
            "engine_version": ENGINE_VERSION_V3,
            "result_version": RESULT_VERSION_V3,
            "candidate_simulation_contract_sha256": contract[
                "contract_sha256"
            ],
            "candidate_evaluation_basis_sha256": contract[
                "evaluation_basis_sha256"
            ],
            "config": config,
            "result": {
                "version": RESULT_VERSION_V3,
                "engine_version": ENGINE_VERSION_V3,
                "scenario_results": scenario_results,
                "execution_capability": "none",
                "live_trading_allowed": False,
                "can_autonomously_decide": False,
            },
            "fully_verified": True,
            "candidate_simulation_binding_verified": True,
            "candidate_simulation_lineage_verified": True,
            "candidate_simulation_marker_binding_verified": True,
            "integrity_profile_verified": True,
            "walk_forward_v3_lineage_verified": True,
            "source_decision_current": True,
            "actionable_now": True,
        },
        "input_snapshot": {
            "version": INPUT_SNAPSHOT_VERSION_V2,
            "portfolio_snapshot": {
                "id": f"portfolio_{candidate_id}",
                "version": 1,
                "candidate_simulation_contract": contract,
            },
            "config": config,
            "histories": copy.deepcopy(histories),
            "manifest": {
                "assumptions": {
                    "friction_scenario_set": get_storage_friction_scenarios(),
                },
                "execution_capability": "none",
                "live_trading_allowed": False,
            },
        },
    }


class CandidateComparisonUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        histories = shared_histories()
        self.first = comparison_record(
            "candidate_a",
            "US.MU",
            "UP",
            histories=histories,
            return_offset=1,
        )
        self.second = comparison_record(
            "candidate_b",
            "US.WDC",
            "DOWN",
            histories=histories,
            return_offset=-1,
        )

    def test_request_requires_unique_runs_and_historical_acknowledgement(self) -> None:
        with self.assertRaises(CandidateComparisonError) as duplicate:
            normalize_candidate_comparison_request(
                request_payload("run_candidate_a", "run_candidate_a")
            )
        self.assertEqual(
            duplicate.exception.code,
            "CANDIDATE_COMPARISON_DUPLICATE_RUN",
        )
        missing_ack = request_payload("run_candidate_a", "run_candidate_b")
        missing_ack["user_confirmed_historical_only"] = False
        with self.assertRaises(CandidateComparisonError) as acknowledgement:
            normalize_candidate_comparison_request(missing_ack)
        self.assertEqual(
            acknowledgement.exception.code,
            "CANDIDATE_COMPARISON_ACKNOWLEDGEMENT_REQUIRED",
        )

    def test_exact_same_basis_returns_metrics_without_winner_claim(self) -> None:
        preview = build_candidate_comparison_preview(
            "room_storage",
            request_payload("run_candidate_a", "run_candidate_b"),
            [self.first, self.second],
        )

        self.assertEqual(preview["version"], CANDIDATE_COMPARISON_PREVIEW_VERSION)
        self.assertTrue(preview["ready"])
        self.assertTrue(preview["metrics_visible"])
        self.assertEqual(preview["provider_calls_total"], 0)
        self.assertEqual(preview["market_data_reads"], 0)
        self.assertFalse(preview["metric_semantics"]["ranking_produced"])
        self.assertFalse(preview["metric_semantics"]["winner_claim"])
        self.assertEqual(
            [item["candidate_id"] for item in preview["candidates"]],
            ["candidate_a", "candidate_b"],
        )
        self.assertEqual(
            preview["candidates"][0]["scenarios"][0]["scenario_id"],
            "baseline",
        )
        self.assertEqual(len(preview["comparison_basis_sha256"]), 64)
        self.assertEqual(len(preview["preview_sha256"]), 64)

    def test_dataset_change_blocks_every_metric(self) -> None:
        changed = copy.deepcopy(self.second)
        changed["input_snapshot"]["histories"]["US.MU"]["rows"][5][
            "close"
        ] += 1
        preview = build_candidate_comparison_preview(
            "room_storage",
            request_payload("run_candidate_a", "run_candidate_b"),
            [self.first, changed],
        )

        self.assertFalse(preview["ready"])
        self.assertFalse(preview["metrics_visible"])
        self.assertIn(
            "CANDIDATE_COMPARISON_DATASET_MISMATCH",
            {item["code"] for item in preview["issues"]},
        )
        self.assertTrue(all(not item["metrics_visible"] for item in preview["candidates"]))
        self.assertTrue(all(not item["scenarios"] for item in preview["candidates"]))

    def test_weight_or_integrity_mismatch_fails_closed(self) -> None:
        different_weight = comparison_record(
            "candidate_c",
            "US.STX",
            "UP",
            histories=shared_histories(),
            weight=30,
        )
        preview = build_candidate_comparison_preview(
            "room_storage",
            request_payload("run_candidate_a", "run_candidate_c"),
            [self.first, different_weight],
        )
        self.assertIn(
            "CANDIDATE_COMPARISON_WEIGHT_MISMATCH",
            {item["code"] for item in preview["issues"]},
        )

        tampered = copy.deepcopy(self.second)
        tampered["run"]["fully_verified"] = False
        blocked = build_candidate_comparison_preview(
            "room_storage",
            request_payload("run_candidate_a", "run_candidate_b"),
            [self.first, tampered],
        )
        self.assertFalse(blocked["ready"])
        self.assertIn(
            "CANDIDATE_COMPARISON_RUN_UNVERIFIED",
            {item["code"] for item in blocked["issues"]},
        )


class FakeComparisonStore:
    def __init__(self, records: list[dict]) -> None:
        self.records = records
        self.calls = []

    def candidate_comparison_run_records(self, room_id, run_ids):
        self.calls.append((room_id, list(run_ids)))
        return copy.deepcopy(self.records)


class ForbiddenDependency:
    def __init__(self, label: str) -> None:
        self.label = label

    def __getattr__(self, name: str):
        raise AssertionError(
            f"candidate comparison must not access {self.label}.{name}"
        )


class CandidateComparisonServiceTests(unittest.TestCase):
    def test_service_uses_one_store_read_and_preserves_selection_order(self) -> None:
        histories = shared_histories()
        records = [
            comparison_record(
                "candidate_a", "US.MU", "UP", histories=histories
            ),
            comparison_record(
                "candidate_b", "US.WDC", "DOWN", histories=histories
            ),
        ]
        store = FakeComparisonStore(records)
        result = CandidateComparisonService(store).preview(
            "room_storage",
            request_payload("run_candidate_a", "run_candidate_b"),
        )
        self.assertTrue(result["ready"])
        self.assertEqual(
            store.calls,
            [("room_storage", ["run_candidate_a", "run_candidate_b"])],
        )


class CandidateComparisonStoreTests(unittest.TestCase):
    def test_store_returns_requested_order_from_one_read_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StudioStore(Path(temp_dir) / "comparison.sqlite3")
            portfolio = create_actionable_portfolio(store)
            service = PaperPortfolioService(store, FakeWalkForwardMarket())
            config = {
                "version": CONFIG_VERSION_V2,
                "train_days": 99,
                "test_days": 20,
                "step_days": 20,
                "price_adjustment": "QFQ",
                "friction_scenario_set": STORAGE_FRICTION_SCENARIOS_VERSION,
                "unfillable_policy": UNFILLABLE_POLICY,
            }
            first = service.walk_forward(
                "room_storage",
                portfolio["id"],
                config,
                expected_portfolio_version=portfolio["version"],
            )
            second = service.walk_forward(
                "room_storage",
                portfolio["id"],
                config,
                expected_portfolio_version=portfolio["version"],
            )
            records = store.candidate_comparison_run_records(
                "room_storage",
                [second["id"], first["id"]],
            )

            self.assertEqual(
                [item["run"]["id"] for item in records],
                [second["id"], first["id"]],
            )
            self.assertTrue(all(item["input_snapshot"] for item in records))
            self.assertEqual(
                store.candidate_comparison_run_records(
                    "missing_room",
                    [second["id"], first["id"]],
                ),
                None,
            )


class CandidateComparisonHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        histories = shared_histories()
        self.original_store = http_server.STORE
        self.original_storage_market = http_server.STORAGE_MARKET
        self.original_providers = http_server.PROVIDERS
        self.store = FakeComparisonStore([
            comparison_record(
                "candidate_a", "US.MU", "UP", histories=histories
            ),
            comparison_record(
                "candidate_b", "US.WDC", "DOWN", histories=histories
            ),
        ])
        http_server.STORE = self.store
        http_server.STORAGE_MARKET = ForbiddenDependency("STORAGE_MARKET")
        http_server.PROVIDERS = ForbiddenDependency("PROVIDERS")
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            http_server.StudioRequestHandler,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        http_server.STORE = self.original_store
        http_server.STORAGE_MARKET = self.original_storage_market
        http_server.PROVIDERS = self.original_providers

    def request(self, payload: dict) -> tuple[int, dict]:
        request = Request(
            f"{self.base_url}/api/rooms/room_storage/candidate-comparisons/preview",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
            },
            data=json.dumps(payload).encode("utf-8"),
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def test_preview_endpoint_is_read_only_and_typed(self) -> None:
        status, payload = self.request(
            request_payload("run_candidate_a", "run_candidate_b")
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["comparison"]["ready"])
        self.assertEqual(payload["comparison"]["provider_calls_total"], 0)
        self.assertEqual(
            self.store.calls,
            [("room_storage", ["run_candidate_a", "run_candidate_b"])],
        )
        response_text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn('"histories"', response_text)
        self.assertNotIn('"input_snapshot"', response_text)

        invalid = request_payload("run_candidate_a", "run_candidate_b")
        invalid["user_confirmed_historical_only"] = False
        status, payload = self.request(invalid)
        self.assertEqual(status, 400)
        self.assertEqual(
            payload["code"],
            "CANDIDATE_COMPARISON_ACKNOWLEDGEMENT_REQUIRED",
        )

        self.store.records = self.store.records[:1]
        status, payload = self.request(
            request_payload("run_candidate_a", "run_candidate_b")
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["code"], "CANDIDATE_COMPARISON_RUN_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
