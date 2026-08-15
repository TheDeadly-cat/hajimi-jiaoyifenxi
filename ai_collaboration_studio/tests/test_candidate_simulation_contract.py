import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.candidate_simulation_contract import (
    CANDIDATE_SIMULATION_CONFIRMATION_VERSION,
    CANDIDATE_SIMULATION_RULE_ID,
    CandidateSimulationContractError,
    build_candidate_simulation_contract,
    build_candidate_simulation_seed,
    verify_candidate_simulation_contract,
)
from backend.convergence import ConvergenceService
from backend.decision_lineage import canonical_sha256
from backend.paper_portfolio import default_paper_portfolio_plan
from backend.paper_portfolio_service import PaperPortfolioService
from backend.store import StudioStore
from backend.walk_forward import (
    CONFIG_VERSION_V2,
    ENGINE_VERSION_V2,
    RESULT_VERSION_V2,
)
from backend.walk_forward_friction import (
    STORAGE_FRICTION_SCENARIOS_VERSION,
    UNFILLABLE_POLICY,
)
from tests.test_walk_forward_integration import (
    FakeWalkForwardMarket,
    create_actionable_portfolio,
)


THESIS = "MU 的结构化基本面与价格条件支持一个可撤销的纸面做多测试"
INVALIDATION = "MU 关键经营假设失效或正式风险复核要求退出"


def strict_plan() -> dict:
    plan = default_paper_portfolio_plan()
    plan["name"] = "用户选择 B · MU 固定方向模拟"
    for position in plan["positions"]:
        if position["symbol"] == "US.MU":
            position.update({
                "side": "LONG",
                "weight_pct": 25,
                "thesis": THESIS,
                "invalidation": INVALIDATION,
            })
    return plan


def strict_anchor(user_decision_id: str = "decision_b") -> dict:
    snapshot = {
        "title": "候选 B：MU 做多 20 个交易日",
        "symbol": "US.MU",
        "direction": "UP",
        "horizon_days": 20,
        "thesis": THESIS,
        "invalidation": INVALIDATION,
    }
    return {
        "user_decision_id": user_decision_id,
        "artifact_id": "artifact_candidate_b",
        "artifact_version": 3,
        "artifact_snapshot_sha256": "1" * 64,
        "decision_version": "artifact_user_decision_v2",
        "decision_record_sha256": "2" * 64,
        "action": "support",
        "ai_preferred_option_id": "candidate_a",
        "selected_option_id": "candidate_b",
        "selected_option_revision": 2,
        "selected_option_origin_message_id": "message_candidate_b_origin",
        "selected_option_latest_message_id": "message_candidate_b_revision_2",
        "selected_option_snapshot_sha256": "3" * 64,
        "governance_attestation_sha256": "4" * 64,
        "selected_candidate_snapshot": snapshot,
        "selected_candidate_snapshot_sha256": canonical_sha256(snapshot),
        "integrity_ok": True,
        "current": True,
    }


def confirmation(seed: dict) -> dict:
    return {
        "version": CANDIDATE_SIMULATION_CONFIRMATION_VERSION,
        "expected_source_sha256": seed["source_sha256"],
        "expected_candidate_revision": seed["candidate_revision"],
        "expected_candidate_snapshot_sha256": seed[
            "candidate_snapshot_sha256"
        ],
        "expected_target_weight_pct": 25,
        "strategy_rule_id": CANDIDATE_SIMULATION_RULE_ID,
        "user_confirmed": True,
    }


class CountingMarket(FakeWalkForwardMarket):
    def __init__(self) -> None:
        super().__init__()
        self.history_call_count = 0
        self.batch_call_count = 0

    def history(self, symbol: str, **kwargs) -> dict:
        self.history_call_count += 1
        return super().history(symbol, **kwargs)

    def history_batch(self, symbols, **kwargs) -> dict:
        self.batch_call_count += 1
        return super().history_batch(symbols, **kwargs)


class CandidateSimulationContractUnitTests(unittest.TestCase):
    def test_user_selected_b_builds_exact_single_name_contract(self) -> None:
        anchor = strict_anchor()
        seed = build_candidate_simulation_seed(anchor)
        contract = build_candidate_simulation_contract(
            anchor,
            strict_plan(),
            confirmation(seed),
        )

        self.assertTrue(seed["ready"])
        self.assertEqual(seed["candidate_id"], "candidate_b")
        self.assertEqual(seed["target_side"], "LONG")
        self.assertEqual(contract["source"]["candidate_id"], "candidate_b")
        self.assertNotEqual(contract["source"]["candidate_id"], "candidate_a")
        self.assertEqual(
            contract["implementation"]["target_symbol"],
            "US.MU",
        )
        self.assertEqual(contract["evaluation"]["horizon_days"], 20)
        self.assertEqual(
            verify_candidate_simulation_contract(
                contract,
                anchor,
                strict_plan(),
            ),
            contract,
        )

    def test_extra_position_and_stale_seed_fail_closed(self) -> None:
        anchor = strict_anchor()
        seed = build_candidate_simulation_seed(anchor)
        extra = strict_plan()
        extra["positions"][1].update({
            "side": "LONG",
            "weight_pct": 5,
            "thesis": "unauthorized hedge",
            "invalidation": "none",
        })
        with self.assertRaisesRegex(
            CandidateSimulationContractError,
            "只有候选标的一个非观望仓位",
        ):
            build_candidate_simulation_contract(
                anchor,
                extra,
                confirmation(seed),
            )

        stale = confirmation(seed)
        stale["expected_source_sha256"] = "f" * 64
        with self.assertRaisesRegex(
            CandidateSimulationContractError,
            "候选来源已变化",
        ):
            build_candidate_simulation_contract(anchor, strict_plan(), stale)

        wrong_weight = confirmation(seed)
        wrong_weight["expected_target_weight_pct"] = 30
        with self.assertRaises(CandidateSimulationContractError) as raised:
            build_candidate_simulation_contract(
                anchor,
                strict_plan(),
                wrong_weight,
            )
        self.assertEqual(
            raised.exception.code,
            "CANDIDATE_SIMULATION_WEIGHT_CONFIRMATION_MISMATCH",
        )

    def test_neutral_is_not_silently_mapped_to_flat(self) -> None:
        anchor = strict_anchor()
        anchor["selected_candidate_snapshot"]["direction"] = "NEUTRAL"
        anchor["selected_candidate_snapshot_sha256"] = canonical_sha256(
            anchor["selected_candidate_snapshot"]
        )
        seed = build_candidate_simulation_seed(anchor)
        self.assertFalse(seed["ready"])
        self.assertEqual(
            seed["issues"][0]["code"],
            "CANDIDATE_SIMULATION_NEUTRAL_BAND_REQUIRED",
        )


class CandidateSimulationStoreServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = StudioStore(Path(self.temp_dir.name) / "candidate.sqlite3")
        self.legacy_portfolio = create_actionable_portfolio(self.store)
        package = self.store.list_decision_packages("room_storage")[0]
        self.anchor = strict_anchor(package["anchor"]["user_decision_id"])
        self.seed = build_candidate_simulation_seed(self.anchor)

    def test_invalid_mapping_fails_before_market_and_writes_nothing(self) -> None:
        market = CountingMarket()
        service = PaperPortfolioService(self.store, market)
        invalid = strict_plan()
        invalid["positions"][1].update({
            "side": "SHORT",
            "weight_pct": 5,
            "thesis": "extra position",
            "invalidation": "extra invalidation",
        })
        before = len(self.store.list_paper_portfolios("room_storage"))

        with patch.object(
            StudioStore,
            "_candidate_simulation_decision_anchor",
            return_value=copy.deepcopy(self.anchor),
        ):
            with self.assertRaises(CandidateSimulationContractError) as raised:
                service.create(
                    "room_storage",
                    {
                        **invalid,
                        "user_decision_id": self.anchor["user_decision_id"],
                        "derivation_note": "invalid extra position",
                        "candidate_simulation_confirmation": confirmation(
                            self.seed
                        ),
                    },
                )

        self.assertEqual(
            raised.exception.code,
            "CANDIDATE_SIMULATION_EXTRA_ACTIVE_POSITION",
        )
        self.assertEqual(market.history_call_count, 0)
        self.assertEqual(
            len(self.store.list_paper_portfolios("room_storage")),
            before,
        )

    def test_update_confirm_and_fixed_replay_preserve_exact_contract(self) -> None:
        market = CountingMarket()
        service = PaperPortfolioService(self.store, market)
        with patch.object(
            StudioStore,
            "_candidate_simulation_decision_anchor",
            return_value=copy.deepcopy(self.anchor),
        ):
            updated = service.update(
                "room_storage",
                self.legacy_portfolio["id"],
                {
                    **strict_plan(),
                    "candidate_simulation_confirmation": confirmation(
                        self.seed
                    ),
                },
                expected_version=self.legacy_portfolio["version"],
            )
            self.assertEqual(
                updated["candidate_simulation_binding"]["status"],
                "verified",
            )
            self.assertEqual(
                updated["candidate_simulation_contract"]["source"][
                    "candidate_id"
                ],
                "candidate_b",
            )
            confirmed = service.confirm(
                "room_storage",
                updated["id"],
                expected_version=updated["version"],
            )
            lineage_count = len(
                self.store.list_decision_packages("room_storage")[0][
                    "lineage"
                ]
            )
            reconfirmed = service.confirm(
                "room_storage",
                updated["id"],
                expected_version=updated["version"],
            )
            self.assertTrue(
                reconfirmed["candidate_simulation_binding"]["ready"]
            )
            self.assertEqual(
                len(
                    self.store.list_decision_packages("room_storage")[0][
                        "lineage"
                    ]
                ),
                lineage_count,
            )
            run = service.walk_forward(
                "room_storage",
                confirmed["id"],
                {
                    "version": CONFIG_VERSION_V2,
                    "train_days": 99,
                    "test_days": 20,
                    "step_days": 20,
                    "price_adjustment": "QFQ",
                    "friction_scenario_set": (
                        STORAGE_FRICTION_SCENARIOS_VERSION
                    ),
                    "unfillable_policy": UNFILLABLE_POLICY,
                },
                expected_portfolio_version=confirmed["version"],
            )

        self.assertEqual(run["result_version"], "walk_forward_result_v3")
        self.assertTrue(run["candidate_simulation_binding_verified"])
        self.assertTrue(run["candidate_simulation_lineage_verified"])
        self.assertTrue(
            run["candidate_simulation_marker_binding_verified"]
        )
        self.assertTrue(run["walk_forward_v3_lineage_verified"])
        self.assertTrue(run["integrity_profile_verified"])
        self.assertTrue(
            run["integrity_profile"]["candidate_simulation_required"]
        )
        self.assertTrue(run["integrity_profile"]["lineage_required"])
        self.assertEqual(
            run["result"]["active_symbols"],
            ["US.MU"],
        )
        self.assertGreater(market.history_call_count, 0)
        self.assertEqual(market.batch_call_count, 1)

        connection = self.store._connect()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                with connection:
                    connection.execute(
                        """UPDATE paper_portfolio_walk_forward_runs
                           SET integrity_profile_json='{}',
                               integrity_profile_sha256=''
                           WHERE room_id=? AND id=?""",
                        ("room_storage", run["id"]),
                    )
        finally:
            connection.close()

        connection = self.store._connect()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                with connection:
                    connection.execute(
                        """UPDATE paper_portfolio_walk_forward_runs
                           SET created_at=created_at + 1
                           WHERE room_id=? AND id=?""",
                        ("room_storage", run["id"]),
                    )
        finally:
            connection.close()

        # The remaining mutations deliberately remove only the database-level
        # whole-row seal so the independent readback verifier is exercised.
        connection = self.store._connect()
        try:
            with connection:
                connection.execute(
                    "DROP TRIGGER trg_walk_forward_profiled_run_immutable"
                )
        finally:
            connection.close()

        connection = self.store._connect()
        try:
            connection.execute("SAVEPOINT update_or_replace_probe")
            stored_row = connection.execute(
                """SELECT * FROM paper_portfolio_walk_forward_runs
                   WHERE room_id=? AND id=?""",
                ("room_storage", run["id"]),
            ).fetchone()
            temp_row = dict(stored_row)
            temp_row["id"] = "walk_forward_update_or_replace_probe"
            columns = list(temp_row)
            connection.execute(
                "INSERT INTO paper_portfolio_walk_forward_runs ("
                + ",".join(columns)
                + ") VALUES ("
                + ",".join("?" for _ in columns)
                + ")",
                tuple(temp_row[column] for column in columns),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE OR REPLACE
                           paper_portfolio_walk_forward_runs
                       SET id=? WHERE id=?""",
                    (run["id"], temp_row["id"]),
                )
            connection.execute("ROLLBACK TO update_or_replace_probe")
            connection.execute("RELEASE update_or_replace_probe")
        finally:
            connection.close()

        connection = self.store._connect()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                with connection:
                    connection.execute(
                        """INSERT OR REPLACE INTO
                               paper_portfolio_walk_forward_runs
                           SELECT * FROM paper_portfolio_walk_forward_runs
                           WHERE room_id=? AND id=?""",
                        ("room_storage", run["id"]),
                    )
        finally:
            connection.close()

        connection = self.store._connect()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                with connection:
                    connection.execute(
                        """DELETE FROM paper_portfolio_walk_forward_runs
                           WHERE room_id=? AND id=?""",
                        ("room_storage", run["id"]),
                    )
        finally:
            connection.close()

        connection = self.store._connect()
        try:
            with connection:
                connection.execute(
                    """UPDATE paper_portfolio_walk_forward_runs
                       SET engine_version=?,result_version=?
                       WHERE room_id=? AND id=?""",
                    (
                        ENGINE_VERSION_V2,
                        RESULT_VERSION_V2,
                        "room_storage",
                        run["id"],
                    ),
                )
        finally:
            connection.close()
        generation_downgrade = next(
            item
            for item in self.store.list_paper_portfolio_walk_forward_runs(
                "room_storage",
                confirmed["id"],
            )
            if item["id"] == run["id"]
        )
        self.assertFalse(generation_downgrade["fully_verified"])
        self.assertFalse(
            generation_downgrade["integrity_profile_verified"]
        )
        self.assertIn(
            "WALK_FORWARD_INTEGRITY_PROFILE_MISMATCH",
            generation_downgrade["integrity_issues"],
        )
        connection = self.store._connect()
        try:
            with connection:
                connection.execute(
                    """UPDATE paper_portfolio_walk_forward_runs
                       SET engine_version=?,result_version=?
                       WHERE room_id=? AND id=?""",
                    (
                        run["engine_version"],
                        run["result_version"],
                        "room_storage",
                        run["id"],
                    ),
                )
        finally:
            connection.close()

        connection = self.store._connect()
        try:
            with connection:
                stored_run = connection.execute(
                    """SELECT input_snapshot_json,
                              input_snapshot_sha256,
                              portfolio_snapshot_sha256
                       FROM paper_portfolio_walk_forward_runs
                       WHERE room_id=? AND id=?""",
                    ("room_storage", run["id"]),
                ).fetchone()
                downgraded_input = json.loads(
                    stored_run["input_snapshot_json"]
                )
                downgraded_portfolio = downgraded_input[
                    "portfolio_snapshot"
                ]
                downgraded_portfolio.pop(
                    "candidate_simulation_contract",
                    None,
                )
                downgraded_portfolio.pop(
                    "candidate_simulation_contract_sha256",
                    None,
                )
                downgraded_input_json = json.dumps(
                    downgraded_input,
                    ensure_ascii=False,
                    allow_nan=False,
                )
                connection.execute(
                    """UPDATE paper_portfolio_walk_forward_runs
                       SET input_snapshot_json=?,
                           input_snapshot_sha256=?,
                           portfolio_snapshot_sha256=?
                       WHERE room_id=? AND id=?""",
                    (
                        downgraded_input_json,
                        canonical_sha256(downgraded_input),
                        canonical_sha256(downgraded_portfolio),
                        "room_storage",
                        run["id"],
                    ),
                )
        finally:
            connection.close()
        downgraded = next(
            item
            for item in self.store.list_paper_portfolio_walk_forward_runs(
                "room_storage",
                confirmed["id"],
            )
            if item["id"] == run["id"]
        )
        self.assertFalse(downgraded["fully_verified"])
        self.assertFalse(
            downgraded["candidate_simulation_marker_binding_verified"]
        )
        self.assertIn(
            "CANDIDATE_SIMULATION_WALK_FORWARD_MARKER_MISMATCH",
            downgraded["integrity_issues"],
        )

        connection = self.store._connect()
        try:
            with connection:
                version_row = connection.execute(
                    """SELECT snapshot_json FROM paper_portfolio_versions
                       WHERE room_id=? AND portfolio_id=? AND version=?""",
                    (
                        "room_storage",
                        confirmed["id"],
                        run["portfolio_version"],
                    ),
                ).fetchone()
                version_snapshot = json.loads(version_row["snapshot_json"])
                version_snapshot.pop("candidate_simulation_contract", None)
                version_snapshot.pop(
                    "candidate_simulation_contract_sha256",
                    None,
                )
                connection.execute(
                    """UPDATE paper_portfolio_versions SET snapshot_json=?
                       WHERE room_id=? AND portfolio_id=? AND version=?""",
                    (
                        json.dumps(
                            version_snapshot,
                            ensure_ascii=False,
                            allow_nan=False,
                        ),
                        "room_storage",
                        confirmed["id"],
                        run["portfolio_version"],
                    ),
                )
                registry = connection.execute(
                    """SELECT creation_event_id
                       FROM decision_lineage_resources
                       WHERE room_id=?
                         AND resource_type='validation.walk_forward'
                         AND resource_id=?""",
                    ("room_storage", run["id"]),
                ).fetchone()
                self.assertIsNotNone(registry)
                connection.execute(
                    """UPDATE paper_portfolio_walk_forward_runs
                       SET candidate_simulation_contract_sha256='',
                           candidate_evaluation_basis_sha256=''
                       WHERE room_id=? AND id=?""",
                    ("room_storage", run["id"]),
                )
                connection.execute(
                    """DELETE FROM decision_lineage_resources
                       WHERE room_id=? AND (
                           (resource_type='validation.walk_forward'
                            AND resource_id=?) OR
                           (resource_type='simulation.paper_portfolio'
                            AND resource_id=?)
                       )""",
                    ("room_storage", run["id"], confirmed["id"]),
                )
                connection.execute(
                    """DELETE FROM decision_lineage_events
                       WHERE room_id=? AND (
                           (resource_type='validation.walk_forward'
                            AND resource_id=?) OR
                           (resource_type='simulation.paper_portfolio'
                            AND resource_id=?)
                       )""",
                    ("room_storage", run["id"], confirmed["id"]),
                )
        finally:
            connection.close()
        tampered = next(
            item
            for item in self.store.list_paper_portfolio_walk_forward_runs(
                "room_storage",
                confirmed["id"],
            )
            if item["id"] == run["id"]
        )
        self.assertFalse(tampered["fully_verified"])
        self.assertFalse(tampered["candidate_simulation_binding_verified"])
        self.assertFalse(
            tampered["candidate_simulation_marker_binding_verified"]
        )
        self.assertFalse(tampered["walk_forward_v3_lineage_verified"])
        self.assertIn(
            "WALK_FORWARD_LINEAGE_BINDING_MISMATCH",
            tampered["integrity_issues"],
        )

    def test_cross_sectional_rule_is_rejected_before_batch_market_read(self) -> None:
        market = CountingMarket()
        service = PaperPortfolioService(self.store, market)
        with patch.object(
            StudioStore,
            "_candidate_simulation_decision_anchor",
            return_value=copy.deepcopy(self.anchor),
        ):
            updated = service.update(
                "room_storage",
                self.legacy_portfolio["id"],
                {
                    **strict_plan(),
                    "candidate_simulation_confirmation": confirmation(
                        self.seed
                    ),
                },
                expected_version=self.legacy_portfolio["version"],
            )
            confirmed = service.confirm(
                "room_storage",
                updated["id"],
                expected_version=updated["version"],
            )
            market.batch_call_count = 0
            with self.assertRaises(CandidateSimulationContractError) as raised:
                service.walk_forward(
                    "room_storage",
                    confirmed["id"],
                    {
                        "version": "walk_forward_config_v3",
                        "strategy_rule_id": (
                            "cross_sectional_total_return_rank_v1"
                        ),
                    },
                    expected_portfolio_version=confirmed["version"],
                )

        self.assertEqual(
            raised.exception.code,
            "CANDIDATE_SIMULATION_WALK_FORWARD_RULE_MISMATCH",
        )
        self.assertEqual(market.batch_call_count, 0)

    def test_store_commit_rejects_a_direct_v4_candidate_bypass(self) -> None:
        service = PaperPortfolioService(self.store, CountingMarket())
        with patch.object(
            StudioStore,
            "_candidate_simulation_decision_anchor",
            return_value=copy.deepcopy(self.anchor),
        ):
            updated = service.update(
                "room_storage",
                self.legacy_portfolio["id"],
                {
                    **strict_plan(),
                    "candidate_simulation_confirmation": confirmation(
                        self.seed
                    ),
                },
                expected_version=self.legacy_portfolio["version"],
            )
            confirmed = service.confirm(
                "room_storage",
                updated["id"],
                expected_version=updated["version"],
            )
            portfolio_snapshot = self.store.get_paper_portfolio_snapshot(
                "room_storage",
                confirmed["id"],
            )
            decision_binding = {"fixture": "coherent-v4-binding"}
            fake_result = {
                "version": "walk_forward_result_v4",
                "engine_version": "walk_forward_engine_v4",
                "config": {},
            }
            fake_input = {
                "portfolio_snapshot": portfolio_snapshot,
                "plan": {
                    "strategy_created_at": portfolio_snapshot["created_at"],
                    "name": portfolio_snapshot["name"],
                    "positions": portfolio_snapshot["positions"],
                },
                "decision_binding": decision_binding,
                "decision_anchor_sha256": canonical_sha256(
                    decision_binding
                ),
            }
            before = len(
                self.store.list_paper_portfolio_walk_forward_runs(
                    "room_storage",
                    confirmed["id"],
                )
            )
            with patch.object(
                StudioStore,
                "_clean_walk_forward_result",
                return_value=fake_result,
            ), patch.object(
                StudioStore,
                "_clean_walk_forward_input_snapshot",
                return_value=fake_input,
            ), patch.object(
                StudioStore,
                "_paper_portfolio_walk_forward_decision_binding",
                return_value=decision_binding,
            ):
                with self.assertRaises(
                    CandidateSimulationContractError
                ) as raised:
                    self.store.create_paper_portfolio_walk_forward_run(
                        "room_storage",
                        confirmed["id"],
                        {},
                        {},
                        expected_portfolio_version=confirmed["version"],
                    )

        self.assertEqual(
            raised.exception.code,
            "CANDIDATE_SIMULATION_WALK_FORWARD_BINDING_MISMATCH",
        )
        self.assertEqual(
            len(
                self.store.list_paper_portfolio_walk_forward_runs(
                    "room_storage",
                    confirmed["id"],
                )
            ),
            before,
        )

    def test_convergence_requires_the_verified_candidate_mapping(self) -> None:
        market = CountingMarket()
        service = PaperPortfolioService(self.store, market)
        with patch.object(
            StudioStore,
            "_candidate_simulation_decision_anchor",
            return_value=copy.deepcopy(self.anchor),
        ):
            updated = service.update(
                "room_storage",
                self.legacy_portfolio["id"],
                {
                    **strict_plan(),
                    "candidate_simulation_confirmation": confirmation(
                        self.seed
                    ),
                },
                expected_version=self.legacy_portfolio["version"],
            )
            service.confirm(
                "room_storage",
                updated["id"],
                expected_version=updated["version"],
            )
            ready = ConvergenceService(self.store).evaluate("room_storage")
            self.assertTrue(ready["portfolio_gate"]["ready"])

            snapshot = self.store.room_snapshot("room_storage")
            valid_portfolio = snapshot["paper_portfolios"][0]
            invalid_portfolio = copy.deepcopy(valid_portfolio)
            invalid_portfolio["id"] = "portfolio_invalid_contract"
            invalid_portfolio["candidate_simulation_binding"] = {
                "applicable": True,
                "ready": False,
                "status": "invalid",
                "issues": [{
                    "code": "CANDIDATE_SIMULATION_CONTRACT_HASH_MISMATCH",
                    "message": "fixture drift",
                }],
            }
            current_package = next(
                package
                for package in snapshot["decision_packages"]
                if package["package_id"]
                == ready["user_decision_gate"]["decision_id"]
            )
            valid_event = next(
                event
                for event in reversed(current_package["lineage"])
                if event.get("resource_type")
                == "simulation.paper_portfolio"
                and event.get("relation_type") == "confirms"
            )
            invalid_event = copy.deepcopy(valid_event)
            invalid_event["resource_id"] = invalid_portfolio["id"]
            invalid_event["resource_snapshot"] = (
                StudioStore._paper_portfolio_snapshot(invalid_portfolio)
            )
            current_package["lineage"].append(invalid_event)
            snapshot["paper_portfolios"].append(invalid_portfolio)
            mixed = ConvergenceService._portfolio_gate(
                snapshot,
                True,
                ready["user_decision_gate"],
            )
            self.assertFalse(mixed["ready"])
            self.assertEqual(
                mixed["candidate_mapping_invalid_count"],
                1,
            )

            with patch.object(
                StudioStore,
                "_verify_persisted_candidate_simulation_binding",
                side_effect=CandidateSimulationContractError(
                    "CANDIDATE_SIMULATION_CONTRACT_HASH_MISMATCH",
                    "fixture contract drift",
                    status=409,
                ),
            ):
                blocked = ConvergenceService(self.store).evaluate(
                    "room_storage"
                )

        self.assertFalse(blocked["portfolio_gate"]["ready"])
        self.assertEqual(
            blocked["portfolio_gate"]["candidate_mapping_invalid_count"],
            1,
        )
        self.assertIn(
            "CANDIDATE_SIMULATION_BINDING_FAILED",
            [
                item["code"]
                for item in blocked["portfolio_gate"]["blockers"]
            ],
        )


if __name__ == "__main__":
    unittest.main()
