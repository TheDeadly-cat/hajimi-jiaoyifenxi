from __future__ import annotations

import json
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from backend import http_server
from backend.convergence import ConvergenceService
from backend.decision_lineage import DECISION_PACKAGE_VERSION
from backend.paper_portfolio_service import PaperPortfolioService
from backend.store import StudioStore, WALK_FORWARD_DECISION_BINDING_VERSION
from backend.user_decision import USER_DECISION_VERSION
from backend.walk_forward import (
    CONFIG_VERSION_V3,
    ENGINE_VERSION_V4,
    INPUT_SNAPSHOT_VERSION_V3,
    RESULT_VERSION_V4,
    RULE_ID,
    STRATEGY_RULE_CONTRACT_VERSION,
)
from tests.test_walk_forward_integration import (
    FakeWalkForwardMarket,
    create_actionable_portfolio,
    disable_profiled_run_storage_seal,
)
from tests.test_observations import observation_payload, resolve_qfq_observation


def v4_request() -> dict:
    return {
        "version": CONFIG_VERSION_V3,
        "strategy_rule_id": RULE_ID,
    }


class MutatingDecisionMarket(FakeWalkForwardMarket):
    def __init__(self, store: StudioStore, artifact_id: str) -> None:
        super().__init__()
        self.store = store
        self.artifact_id = artifact_id
        self.mutated = False

    def history_batch(self, symbols, **kwargs) -> dict:
        result = super().history_batch(symbols, **kwargs)
        if not self.mutated:
            artifact = self.store.get_artifact("room_storage", self.artifact_id)
            self.store.create_artifact_user_decision(
                "room_storage",
                self.artifact_id,
                expected_version=artifact["version"],
                action="hold",
                rationale="Pause the candidate before the validation commit.",
            )
            self.mutated = True
        return result


class WalkForwardV4StoreServiceIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "v4.sqlite3")
        self.portfolio = create_actionable_portfolio(self.store)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_v4(self) -> dict:
        return PaperPortfolioService(
            self.store,
            FakeWalkForwardMarket(),
        ).walk_forward(
            "room_storage",
            self.portfolio["id"],
            v4_request(),
            expected_portfolio_version=self.portfolio["version"],
        )

    def raw_snapshot(self, run_id: str) -> dict:
        with closing(self.store._connect()) as connection:
            row = connection.execute(
                """SELECT input_snapshot_json
                   FROM paper_portfolio_walk_forward_runs WHERE id=?""",
                (run_id,),
            ).fetchone()
        return json.loads(row["input_snapshot_json"])

    def test_v4_roundtrip_binds_contract_decision_lineage_and_full_portfolio(self) -> None:
        run = self.run_v4()

        self.assertEqual(run["record_version"], 3)
        self.assertEqual(run["engine_version"], ENGINE_VERSION_V4)
        self.assertEqual(run["result_version"], RESULT_VERSION_V4)
        self.assertEqual(run["result"]["input_snapshot_version"], INPUT_SNAPSHOT_VERSION_V3)
        self.assertEqual(
            run["result"]["strategy_rule_contract"]["version"],
            STRATEGY_RULE_CONTRACT_VERSION,
        )
        self.assertTrue(run["strategy_contract_hash_verified"])
        self.assertTrue(run["decision_anchor_hash_verified"])
        self.assertTrue(run["decision_binding_verified"])
        self.assertTrue(run["lineage_binding_verified"])
        self.assertTrue(run["portfolio_snapshot_hash_verified"])
        self.assertTrue(run["result_recomputed_verified"])
        self.assertTrue(run["fully_verified"])
        self.assertTrue(run["source_decision_current"])
        self.assertTrue(run["actionable_now"])

        snapshot = self.raw_snapshot(run["id"])
        self.assertEqual(snapshot["portfolio_snapshot"], self.portfolio)
        self.assertEqual(
            snapshot["strategy_rule_contract"],
            snapshot["config"]["strategy_rule_contract"],
        )
        self.assertEqual(snapshot["provider_calls_total"], 0)
        self.assertEqual(snapshot["openai_calls"], 0)
        self.assertEqual(snapshot["execution_capability"], "none")
        self.assertFalse(snapshot["live_trading_allowed"])

    def test_non_ai_preferred_selection_flows_through_every_downstream_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StudioStore(Path(temp_dir) / "selected-b.sqlite3")
            portfolio = create_actionable_portfolio(
                store,
                selected_option_id="paper_flat",
            )
            package = store.list_decision_packages("room_storage")[0]
            anchor = package["anchor"]
            artifact_id = anchor["artifact_id"]
            artifact_version = anchor["artifact_version"]
            decision_id = anchor["user_decision_id"]

            self.assertEqual(package["version"], DECISION_PACKAGE_VERSION)
            self.assertEqual(anchor["decision_version"], USER_DECISION_VERSION)
            self.assertEqual(anchor["ai_preferred_option_id"], "paper_small")
            self.assertEqual(anchor["selected_option_id"], "paper_flat")
            self.assertEqual(anchor["preferred_option_id"], "paper_flat")
            self.assertFalse(anchor["selected_is_ai_preferred"])
            implements = [
                event
                for event in package["lineage"]
                if event["relation_type"] == "implements"
                and event["resource_id"] == portfolio["id"]
            ]
            self.assertEqual(len(implements), 1)

            convergence = ConvergenceService(store).evaluate("room_storage")
            user_gate = convergence["user_decision_gate"]
            self.assertTrue(user_gate["ready"])
            self.assertEqual(user_gate["decision_version"], USER_DECISION_VERSION)
            self.assertEqual(user_gate["ai_preferred_option_id"], "paper_small")
            self.assertEqual(user_gate["selected_option_id"], "paper_flat")
            self.assertTrue(convergence["portfolio_gate"]["ready"])

            graph = store.artifact_evidence_graph("room_storage", artifact_id)
            nodes = {node["node_id"]: node for node in graph["nodes"]}
            selects = [
                edge for edge in graph["edges"] if edge["edge_type"] == "selects"
            ]
            self.assertEqual(len(selects), 1)
            self.assertEqual(
                nodes[selects[0]["to_node_id"]]["item_key"],
                "decision_options:paper_flat",
            )

            run = PaperPortfolioService(
                store,
                FakeWalkForwardMarket(),
            ).walk_forward(
                "room_storage",
                portfolio["id"],
                v4_request(),
                expected_portfolio_version=portfolio["version"],
            )
            with closing(store._connect()) as connection:
                row = connection.execute(
                    "SELECT input_snapshot_json FROM paper_portfolio_walk_forward_runs WHERE id=?",
                    (run["id"],),
                ).fetchone()
            binding = json.loads(row["input_snapshot_json"])["decision_binding"]
            self.assertEqual(binding["version"], WALK_FORWARD_DECISION_BINDING_VERSION)
            self.assertEqual(binding["ai_preferred_option_id"], "paper_small")
            self.assertEqual(binding["selected_option_id"], "paper_flat")
            self.assertEqual(binding["selected_option"]["id"], "paper_flat")
            reread = store.list_paper_portfolio_walk_forward_runs(
                "room_storage",
                portfolio["id"],
            )[0]
            self.assertTrue(reread["decision_binding_verified"])
            self.assertTrue(reread["fully_verified"])

            observation = store.create_observation(
                "room_storage",
                observation_payload(
                    user_decision_id=decision_id,
                    source_portfolio_id=portfolio["id"],
                    source_portfolio_version=portfolio["version"],
                    derivation_note="Test the exact user-selected B candidate.",
                ),
            )
            store.confirm_observation(
                "room_storage",
                observation["id"],
                {
                    "price": 100,
                    "time": "2026-07-01 16:00:00",
                    "snapshot_id": "selected-b-baseline",
                },
            )
            resolve_qfq_observation(
                store,
                "room_storage",
                observation["id"],
                outcome_price=104,
                outcome_time="2026-07-02 16:00:00",
                return_pct=4,
                hit=True,
            )
            scorecard = store.observation_scorecard("room_storage")
            candidate_key = (
                f"{artifact_id}@v{artifact_version}:paper_flat"
            )
            self.assertEqual(
                scorecard["by_candidate_option"][candidate_key][
                    "candidate_option_id"
                ],
                "paper_flat",
            )
            self.assertEqual(
                scorecard["by_candidate_option"][candidate_key][
                    "decision_package_ids"
                ],
                [decision_id],
            )

    def test_decision_becoming_stale_only_disables_actionable_now(self) -> None:
        run = self.run_v4()
        snapshot = self.raw_snapshot(run["id"])
        artifact_id = snapshot["decision_binding"]["artifact_id"]
        artifact = self.store.get_artifact("room_storage", artifact_id)
        self.store.create_artifact_user_decision(
            "room_storage",
            artifact_id,
            expected_version=artifact["version"],
            action="hold",
            rationale="Keep the historical validation but pause new actions.",
        )

        reread = self.store.list_paper_portfolio_walk_forward_runs(
            "room_storage",
            self.portfolio["id"],
        )[0]
        self.assertTrue(reread["fully_verified"])
        self.assertTrue(reread["lineage_binding_verified"])
        self.assertFalse(reread["source_decision_current"])
        self.assertFalse(reread["actionable_now"])

    def test_tampered_contract_hash_fails_read_integrity(self) -> None:
        run = self.run_v4()
        with closing(self.store._connect()) as connection, connection:
            disable_profiled_run_storage_seal(connection)
            row = connection.execute(
                """SELECT input_snapshot_json
                   FROM paper_portfolio_walk_forward_runs WHERE id=?""",
                (run["id"],),
            ).fetchone()
            snapshot = json.loads(row["input_snapshot_json"])
            snapshot["strategy_contract_sha256"] = "0" * 64
            connection.execute(
                """UPDATE paper_portfolio_walk_forward_runs
                   SET input_snapshot_json=?,input_snapshot_sha256=? WHERE id=?""",
                (
                    json.dumps(snapshot, ensure_ascii=False),
                    self.store._canonical_sha256(snapshot),
                    run["id"],
                ),
            )

        reread = self.store.list_paper_portfolio_walk_forward_runs(
            "room_storage",
            self.portfolio["id"],
        )[0]
        self.assertFalse(reread["strategy_contract_hash_verified"])
        self.assertFalse(reread["fully_verified"])
        self.assertEqual(reread["integrity_status"], "failed")

    def test_decision_toctou_fails_without_persisting(self) -> None:
        package = self.store.list_decision_packages("room_storage")[0]
        market = MutatingDecisionMarket(
            self.store,
            package["anchor"]["artifact_id"],
        )
        service = PaperPortfolioService(self.store, market)

        with self.assertRaises(ValueError):
            service.walk_forward(
                "room_storage",
                self.portfolio["id"],
                v4_request(),
                expected_portfolio_version=self.portfolio["version"],
            )
        self.assertEqual(
            self.store.list_paper_portfolio_walk_forward_runs(
                "room_storage",
                self.portfolio["id"],
            ),
            [],
        )

    def test_v4_store_write_cannot_bypass_workflow_gate(self) -> None:
        run = self.run_v4()
        snapshot = self.raw_snapshot(run["id"])
        with self.assertRaisesRegex(ValueError, "cannot be bypassed"):
            self.store.create_paper_portfolio_walk_forward_run(
                "room_storage",
                self.portfolio["id"],
                run["result"],
                snapshot,
                expected_portfolio_version=self.portfolio["version"],
                enforce_workflow_gate=False,
            )

    def test_client_cannot_submit_a_full_strategy_contract(self) -> None:
        market = FakeWalkForwardMarket()
        service = PaperPortfolioService(self.store, market)
        with self.assertRaisesRegex(ValueError, "strategy_rule_contract"):
            service.walk_forward(
                "room_storage",
                self.portfolio["id"],
                {
                    **v4_request(),
                    "strategy_rule_contract": {"rule_id": "client_override"},
                },
                expected_portfolio_version=self.portfolio["version"],
            )
        self.assertEqual(market.calls, [])


class WalkForwardV4HttpIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_store = http_server.STORE
        self.original_market = http_server.STORAGE_MARKET
        http_server.STORE = StudioStore(Path(self.temp_dir.name) / "http-v4.sqlite3")
        http_server.STORAGE_MARKET = FakeWalkForwardMarket()
        self.portfolio = create_actionable_portfolio(http_server.STORE)
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
        http_server.STORAGE_MARKET = self.original_market
        self.temp_dir.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}",
            method=method,
            headers=headers,
            data=body,
        )
        with urlopen(request, timeout=15) as response:
            self.assertEqual(response.status, 201 if method == "POST" else 200)
            return json.loads(response.read().decode("utf-8"))

    def test_existing_endpoint_accepts_v3_request_and_returns_verified_v4(self) -> None:
        path = (
            "/api/rooms/room_storage/paper-portfolios/"
            f"{self.portfolio['id']}/walk-forward"
        )
        created = self.request("POST", path, {
            "expected_portfolio_version": self.portfolio["version"],
            **v4_request(),
        })["walk_forward_run"]
        self.assertEqual(created["record_version"], 3)
        self.assertTrue(created["fully_verified"])
        self.assertTrue(created["lineage_binding_verified"])

        listed = self.request("GET", path)["walk_forward_runs"]
        self.assertEqual([item["id"] for item in listed], [created["id"]])
        self.assertTrue(listed[0]["strategy_contract_hash_verified"])
        self.assertTrue(listed[0]["actionable_now"])


if __name__ == "__main__":
    unittest.main()
