from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from backend import http_server
from backend.convergence import ConvergenceService
from backend.paper_portfolio import (
    default_paper_portfolio_plan,
    evaluate_paper_portfolio,
)
from backend.paper_portfolio_service import (
    DEFAULT_WALK_FORWARD_CONFIG,
    WALK_FORWARD_HISTORY_LIMIT,
    PaperPortfolioService,
)
from backend.store import StudioStore
from backend.walk_forward import (
    CONFIG_VERSION,
    CONFIG_VERSION_V1,
    CONFIG_VERSION_V2,
    ENGINE_VERSION_V2,
    ENGINE_VERSION_V3,
    INPUT_SNAPSHOT_VERSION_V1,
    INPUT_SNAPSHOT_VERSION_V2,
    INSUFFICIENT_WINDOWS_REASON,
    PLAN_VERSION,
    RESULT_VERSION_V2,
    RESULT_VERSION_V3,
    calculate_walk_forward_feasibility,
    run_walk_forward_backtest,
)
from backend.walk_forward_friction import (
    PAPER_FRICTION_MODEL_VERSION,
    PAPER_LIQUIDITY_PROXY_VERSION,
    STORAGE_FRICTION_SCENARIOS_VERSION,
    UNFILLABLE_POLICY,
    get_storage_friction_scenarios,
)


SYMBOLS = ("US.MU", "US.SNDK", "US.WDC", "US.STX")


def disable_profiled_run_storage_seal(
    connection,
) -> None:
    """Let corruption fixtures reach the independent readback verifier."""

    connection.execute(
        "DROP TRIGGER IF EXISTS trg_walk_forward_profiled_run_immutable"
    )


def futu_history(
    symbol: str,
    *,
    days: int = 500,
    daily_return: float = 0.001,
) -> dict:
    first_day = date(2025, 1, 1)
    close = 100.0
    rows = []
    for index in range(days):
        open_price = close
        if index:
            close *= 1 + daily_return
        close_price = round(close, 8)
        open_price = round(open_price, 8)
        high_price = round(max(open_price, close_price) * 1.01, 8)
        low_price = round(min(open_price, close_price) * 0.99, 8)
        volume = 50_000_000.0
        rows.append({
            "symbol": symbol,
            "market_time": f"{first_day + timedelta(days=index)} 16:00:00",
            "time": datetime.combine(
                first_day + timedelta(days=index),
                time(16),
                tzinfo=ZoneInfo("America/New_York"),
            ).astimezone(timezone.utc).isoformat(),
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": volume,
            "turnover": round(close_price * volume, 8),
        })
    return {
        "ok": True,
        "source": "futu_opend",
        "interval": "1d",
        "price_adjustment": "QFQ",
        "captured_at": "2026-07-30T20:00:00.000Z",
        "as_of_date": "2026-07-30",
        "last_completed_session": (
            first_day + timedelta(days=days - 1)
        ).isoformat(),
        "actual_start": first_day.isoformat(),
        "actual_end": (first_day + timedelta(days=days - 1)).isoformat(),
        "symbol": symbol,
        "rows": rows,
        "source_errors": [],
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


def paper_plan() -> dict:
    plan = default_paper_portfolio_plan()
    for position, side, weight in zip(
        plan["positions"],
        ("LONG", "LONG", "SHORT", "FLAT"),
        (25, 20, 10, 0),
    ):
        position["side"] = side
        position["weight_pct"] = weight
        if side != "FLAT":
            position["thesis"] = "仅用于历史纸面检验"
            position["invalidation"] = "样本外失效后退回用户复核"
    return plan


def walk_forward_plan(portfolio: dict | None = None) -> dict:
    plan = paper_plan() if portfolio is None else portfolio
    return {
        "version": PLAN_VERSION,
        "portfolio_id": (
            portfolio["id"] if portfolio else "portfolio_test_fixture"
        ),
        "portfolio_version": (
            portfolio["version"] if portfolio else 1
        ),
        "strategy_created_at": (
            portfolio["created_at"] if portfolio else 1_750_000_000_000
        ),
        "mode": "retroactive_fixed_plan_replay",
        "strategy_provenance": "current_plan_retroactive",
        "out_of_sample_claim": False,
        "evaluation_as_of_date": "2026-07-30",
        "data_snapshot_cutoff": "2026-05-15",
        "name": plan["name"],
        "positions": plan["positions"],
    }


def evaluation() -> dict:
    return evaluate_paper_portfolio(
        paper_plan(),
        {
            symbol: futu_history(symbol, daily_return=0.001 * (index + 1))
            for index, symbol in enumerate(SYMBOLS[:3])
        },
        evaluated_at="2026-07-30T00:00:00Z",
    )


def create_portfolio(store: StudioStore) -> dict:
    portfolio = store.create_paper_portfolio(
        "room_storage",
        paper_plan(),
        evaluation(),
        created_by="user",
    )
    if not portfolio:
        raise AssertionError("seeded storage room missing")
    return portfolio


def create_actionable_portfolio(
    store: StudioStore,
    *,
    selected_option_id: str = "paper_small",
) -> dict:
    message = store.add_message(
        "room_storage",
        sender_type="user",
        sender_name="User",
        content="Confirm a reversible paper-only storage research plan.",
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
        title="Walk-forward candidate",
        content={
            "summary": "Compare reversible paper-only storage allocations.",
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
                        "title": "Small paper allocation",
                        "description": "Use bounded paper weights only.",
                        "benefits": ["Reversible"],
                        "risks": ["Historical proxy"],
                        "evidence": evidence,
                    },
                    {
                        "id": "paper_flat",
                        "title": "Remain flat",
                        "description": "Keep all paper weights at zero.",
                        "benefits": ["No exposure"],
                        "risks": ["No comparison"],
                        "evidence": evidence,
                    },
                ],
                "preferred_option_id": "paper_small",
                "rationale": "The bounded paper plan is easier to invalidate.",
                "evidence": evidence,
            },
        },
        created_by="user",
    )
    confirmed_artifact = store.confirm_artifact(
        "room_storage",
        artifact["id"],
        expected_version=artifact["version"],
        confirmed_by="user",
    )
    decision = store.create_artifact_user_decision(
        "room_storage",
        confirmed_artifact["id"],
        expected_version=confirmed_artifact["version"],
        action="support",
        rationale="Use this exact candidate for paper-only validation.",
        selected_option_id=selected_option_id,
    )
    portfolio = store.create_paper_portfolio(
        "room_storage",
        paper_plan(),
        evaluation(),
        created_by="user",
        user_decision_id=decision["id"],
        derivation_note="Bind the exact supported candidate to paper weights.",
    )
    return store.confirm_paper_portfolio(
        "room_storage",
        portfolio["id"],
        expected_version=portfolio["version"],
        confirmed_by="user",
    )


class FakeWalkForwardMarket:
    def __init__(self, *, ready: bool = True, days: int = 500) -> None:
        self.ready = ready
        self.days = days
        self.calls: list[tuple[str, int | None]] = []
        self.last_batch_kwargs: dict = {}

    def history(self, symbol: str, **kwargs) -> dict:
        self.calls.append((symbol, kwargs.get("limit")))
        if not self.ready:
            return {
                **futu_history(symbol),
                "ok": False,
                "rows": [],
                "source_errors": [{
                    "source": "futu_opend",
                    "code": "OFFLINE",
                    "message": "offline test fixture",
                }],
            }
        return futu_history(
            symbol,
            days=self.days,
            daily_return=0.0005 * (SYMBOLS.index(symbol) + 1),
        )

    def history_batch(self, symbols, **kwargs) -> dict:
        requested = tuple(symbols)
        self.last_batch_kwargs = dict(kwargs)
        self.calls.append(("BATCH", kwargs.get("limit")))
        histories = {
            symbol: (
                futu_history(
                    symbol,
                    days=self.days,
                    daily_return=0.0005 * (SYMBOLS.index(symbol) + 1),
                )
                if self.ready
                else {
                    **futu_history(symbol),
                    "ok": False,
                    "rows": [],
                    "source_errors": [{
                        "source": "futu_opend",
                        "code": "OFFLINE",
                        "message": "offline test fixture",
                    }],
                }
            )
            for symbol in requested
        }
        source_errors = [
            error
            for history in histories.values()
            for error in history["source_errors"]
        ]
        return {
            "ok": self.ready,
            "source": "futu_opend",
            "interval": "1d",
            "price_adjustment": "QFQ",
            "captured_at": "2026-07-30T00:00:00.000Z",
            "as_of_date": "2026-07-30",
            "symbols": list(requested),
            "histories": histories,
            "source_errors": source_errors,
            "execution_capability": "none",
            "live_trading_allowed": False,
        }


class WalkForwardStoreIntegrationTests(unittest.TestCase):
    def test_immutable_audit_record_is_versioned_hashed_and_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StudioStore(Path(temp_dir) / "store.sqlite3")
            portfolio = create_portfolio(store)
            histories = {
                symbol: futu_history(
                    symbol,
                    daily_return=0.0005 * (index + 1),
                )
                for index, symbol in enumerate(SYMBOLS)
            }
            frozen_plan = walk_forward_plan(portfolio)
            result = run_walk_forward_backtest(
                histories,
                frozen_plan,
                DEFAULT_WALK_FORWARD_CONFIG,
            )
            input_snapshot = PaperPortfolioService._walk_forward_input_snapshot(
                "room_storage",
                portfolio,
                frozen_plan,
                result["config"],
                histories,
            )

            run = store.create_paper_portfolio_walk_forward_run(
                "room_storage",
                portfolio["id"],
                result,
                input_snapshot,
                expected_portfolio_version=portfolio["version"],
                enforce_workflow_gate=False,
            )

            self.assertIsNotNone(run)
            self.assertEqual(run["record_version"], 2)
            self.assertEqual(run["portfolio_version"], portfolio["version"])
            self.assertEqual(run["engine_version"], ENGINE_VERSION_V3)
            self.assertEqual(run["result_version"], RESULT_VERSION_V3)
            self.assertEqual(run["config"]["version"], CONFIG_VERSION_V2)
            self.assertEqual(
                input_snapshot["version"],
                INPUT_SNAPSHOT_VERSION_V2,
            )
            self.assertEqual(run["result"]["execution_capability"], "none")
            self.assertFalse(run["result"]["live_trading_allowed"])
            self.assertEqual(len(run["portfolio_snapshot_sha256"]), 64)
            self.assertEqual(len(run["input_hash"]), 64)
            self.assertEqual(len(run["result_sha256"]), 64)
            self.assertTrue(run["integrity_ok"])
            self.assertTrue(run["fully_verified"])
            self.assertEqual(run["integrity_status"], "verified")
            self.assertTrue(run["result_hash_verified"])
            self.assertTrue(run["input_snapshot_hash_verified"])
            self.assertTrue(run["input_binding_verified"])
            self.assertEqual(run["integrity_issues"], [])
            self.assertEqual(run["integrity_warnings"], [])
            serialized = json.dumps(run, ensure_ascii=False).lower()
            for forbidden in ("api_key", "\"prompt\"", "account_id", "order_id"):
                self.assertNotIn(forbidden, serialized)

            listed = store.list_paper_portfolio_walk_forward_runs(
                "room_storage",
                portfolio["id"],
            )
            self.assertEqual([item["id"] for item in listed], [run["id"]])
            self.assertEqual(
                store.get_paper_portfolio("room_storage", portfolio["id"])["version"],
                portfolio["version"],
            )

            unsafe = dict(result)
            unsafe["prompt"] = "must not persist"
            with self.assertRaisesRegex(ValueError, "敏感字段"):
                store.create_paper_portfolio_walk_forward_run(
                    "room_storage",
                    portfolio["id"],
                    unsafe,
                    input_snapshot,
                    expected_portfolio_version=portfolio["version"],
                    enforce_workflow_gate=False,
                )
            tampered = copy.deepcopy(result)
            tampered["folds"][0]["net_return_pct"] += 1
            tampered["scenario_results"][0]["folds"][0]["net_return_pct"] += 1
            with self.assertRaisesRegex(ValueError, "冻结输入重算不一致"):
                store.create_paper_portfolio_walk_forward_run(
                    "room_storage",
                    portfolio["id"],
                    tampered,
                    input_snapshot,
                    expected_portfolio_version=portfolio["version"],
                    enforce_workflow_gate=False,
                )
            self.assertEqual(
                len(store.list_paper_portfolio_walk_forward_runs(
                    "room_storage",
                    portfolio["id"],
                )),
                1,
            )

    def test_read_version_matrix_verifies_exact_v3_and_rejects_mixes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StudioStore(Path(temp_dir) / "version_matrix.sqlite3")
            portfolio = create_portfolio(store)
            histories = {
                symbol: futu_history(symbol)
                for symbol in SYMBOLS
            }
            frozen_plan = walk_forward_plan(portfolio)
            result = run_walk_forward_backtest(
                histories,
                frozen_plan,
                DEFAULT_WALK_FORWARD_CONFIG,
            )
            input_snapshot = PaperPortfolioService._walk_forward_input_snapshot(
                "room_storage",
                portfolio,
                frozen_plan,
                result["config"],
                histories,
            )
            run = store.create_paper_portfolio_walk_forward_run(
                "room_storage",
                portfolio["id"],
                result,
                input_snapshot,
                expected_portfolio_version=portfolio["version"],
                enforce_workflow_gate=False,
            )

            def persist_fixture(
                *,
                engine_version: str,
                result_version: str,
                fixture_config: dict,
                fixture_result: dict,
                fixture_snapshot: dict,
            ) -> dict:
                with closing(store._connect()) as connection, connection:
                    disable_profiled_run_storage_seal(connection)
                    connection.execute(
                        """UPDATE paper_portfolio_walk_forward_runs
                           SET engine_version=?,result_version=?,config_json=?,
                               result_json=?,result_sha256=?,input_snapshot_json=?,
                               input_snapshot_sha256=? WHERE id=?""",
                        (
                            engine_version,
                            result_version,
                            json.dumps(fixture_config, ensure_ascii=False),
                            json.dumps(fixture_result, ensure_ascii=False),
                            store._canonical_sha256(fixture_result),
                            json.dumps(fixture_snapshot, ensure_ascii=False),
                            store._canonical_sha256(fixture_snapshot),
                            run["id"],
                        ),
                    )
                return store.list_paper_portfolio_walk_forward_runs(
                    "room_storage",
                    portfolio["id"],
                )[0]

            v3_config = copy.deepcopy(result["config"])
            v3_config["version"] = CONFIG_VERSION_V2
            v3_result = copy.deepcopy(result)
            v3_result["version"] = RESULT_VERSION_V3
            v3_result["engine_version"] = ENGINE_VERSION_V3
            v3_result["config"] = copy.deepcopy(v3_config)
            v3_snapshot = copy.deepcopy(input_snapshot)
            v3_snapshot["version"] = INPUT_SNAPSHOT_VERSION_V2
            v3_snapshot["config"] = copy.deepcopy(v3_config)

            exact_v3 = persist_fixture(
                engine_version=ENGINE_VERSION_V3,
                result_version=RESULT_VERSION_V3,
                fixture_config=v3_config,
                fixture_result=v3_result,
                fixture_snapshot=v3_snapshot,
            )
            self.assertTrue(exact_v3["fully_verified"])
            self.assertEqual(exact_v3["integrity_status"], "verified")
            self.assertEqual(exact_v3["integrity_issues"], [])
            self.assertEqual(exact_v3["integrity_warnings"], [])

            mixed_config = copy.deepcopy(v3_config)
            mixed_config["version"] = CONFIG_VERSION_V1
            mixed_config_result = copy.deepcopy(v3_result)
            mixed_config_result["config"] = copy.deepcopy(mixed_config)
            mixed_config_snapshot = copy.deepcopy(v3_snapshot)
            mixed_config_snapshot["config"] = copy.deepcopy(mixed_config)
            mixed_v3 = persist_fixture(
                engine_version=ENGINE_VERSION_V3,
                result_version=RESULT_VERSION_V3,
                fixture_config=mixed_config,
                fixture_result=mixed_config_result,
                fixture_snapshot=mixed_config_snapshot,
            )
            self.assertEqual(mixed_v3["integrity_status"], "failed")
            self.assertFalse(mixed_v3["fully_verified"])
            self.assertIn(
                "WALK_FORWARD_V3_VERSION_BINDING_MISMATCH",
                mixed_v3["integrity_issues"],
            )

            mixed_pair_result = copy.deepcopy(v3_result)
            mixed_pair_result["version"] = RESULT_VERSION_V2
            mixed_pair = persist_fixture(
                engine_version=ENGINE_VERSION_V3,
                result_version=RESULT_VERSION_V2,
                fixture_config=v3_config,
                fixture_result=mixed_pair_result,
                fixture_snapshot=v3_snapshot,
            )
            self.assertEqual(mixed_pair["integrity_status"], "failed")
            self.assertFalse(mixed_pair["fully_verified"])
            self.assertIn(
                "WALK_FORWARD_RESULT_VERSION_UNSUPPORTED",
                mixed_pair["integrity_issues"],
            )

    def test_read_fails_closed_when_result_input_or_binding_is_tampered(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StudioStore(Path(temp_dir) / "tamper.sqlite3")
            portfolio = create_portfolio(store)
            histories = {
                symbol: futu_history(
                    symbol,
                    daily_return=0.0005 * (index + 1),
                )
                for index, symbol in enumerate(SYMBOLS)
            }
            frozen_plan = walk_forward_plan(portfolio)
            result = run_walk_forward_backtest(
                histories,
                frozen_plan,
                DEFAULT_WALK_FORWARD_CONFIG,
            )
            input_snapshot = PaperPortfolioService._walk_forward_input_snapshot(
                "room_storage",
                portfolio,
                frozen_plan,
                result["config"],
                histories,
            )
            run = store.create_paper_portfolio_walk_forward_run(
                "room_storage",
                portfolio["id"],
                result,
                input_snapshot,
                expected_portfolio_version=portfolio["version"],
                enforce_workflow_gate=False,
            )
            self.assertTrue(run["integrity_ok"])

            tampered_result = copy.deepcopy(result)
            tampered_result["summary"]["portfolio_cumulative_return_pct"] = 999
            with closing(store._connect()) as connection, connection:
                disable_profiled_run_storage_seal(connection)
                connection.execute(
                    "UPDATE paper_portfolio_walk_forward_runs SET result_json=? WHERE id=?",
                    (json.dumps(tampered_result, ensure_ascii=False), run["id"]),
                )
            checked = store.list_paper_portfolio_walk_forward_runs(
                "room_storage",
                portfolio["id"],
            )[0]
            self.assertFalse(checked["integrity_ok"])
            self.assertFalse(checked["fully_verified"])
            self.assertFalse(checked["result_hash_verified"])
            self.assertEqual(checked["integrity_status"], "failed")
            self.assertIn(
                "WALK_FORWARD_RESULT_HASH_MISMATCH",
                checked["integrity_issues"],
            )

            tampered_input = copy.deepcopy(input_snapshot)
            tampered_input["manifest"]["data_snapshot_cutoff"] = "1900-01-01"
            with closing(store._connect()) as connection, connection:
                connection.execute(
                    """UPDATE paper_portfolio_walk_forward_runs
                       SET result_json=?,input_snapshot_json=? WHERE id=?""",
                    (
                        json.dumps(result, ensure_ascii=False),
                        json.dumps(tampered_input, ensure_ascii=False),
                        run["id"],
                    ),
                )
            checked = store.list_paper_portfolio_walk_forward_runs(
                "room_storage",
                portfolio["id"],
            )[0]
            self.assertTrue(checked["result_hash_verified"])
            self.assertFalse(checked["input_snapshot_hash_verified"])
            self.assertFalse(checked["input_binding_verified"])
            self.assertFalse(checked["integrity_ok"])
            self.assertIn(
                "WALK_FORWARD_INPUT_SNAPSHOT_HASH_MISMATCH",
                checked["integrity_issues"],
            )
            self.assertIn(
                "WALK_FORWARD_INPUT_BINDING_MISMATCH",
                checked["integrity_issues"],
            )

            with closing(store._connect()) as connection, connection:
                connection.execute(
                    """UPDATE paper_portfolio_walk_forward_runs
                       SET input_snapshot_json=?,input_hash=? WHERE id=?""",
                    (
                        json.dumps(input_snapshot, ensure_ascii=False),
                        "0" * 64,
                        run["id"],
                    ),
                )
            checked = store.list_paper_portfolio_walk_forward_runs(
                "room_storage",
                portfolio["id"],
            )[0]
            self.assertTrue(checked["result_hash_verified"])
            self.assertTrue(checked["input_snapshot_hash_verified"])
            self.assertFalse(checked["integrity_ok"])
            self.assertIn(
                "WALK_FORWARD_INPUT_HASH_MISMATCH",
                checked["integrity_issues"],
            )

    def test_read_recomputes_result_and_verifies_frozen_portfolio_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StudioStore(Path(temp_dir) / "rehash_tamper.sqlite3")
            portfolio = create_portfolio(store)
            histories = {symbol: futu_history(symbol) for symbol in SYMBOLS}
            frozen_plan = walk_forward_plan(portfolio)
            result = run_walk_forward_backtest(
                histories,
                frozen_plan,
                DEFAULT_WALK_FORWARD_CONFIG,
            )
            input_snapshot = PaperPortfolioService._walk_forward_input_snapshot(
                "room_storage",
                portfolio,
                frozen_plan,
                result["config"],
                histories,
            )
            run = store.create_paper_portfolio_walk_forward_run(
                "room_storage",
                portfolio["id"],
                result,
                input_snapshot,
                expected_portfolio_version=portfolio["version"],
                enforce_workflow_gate=False,
            )
            self.assertTrue(run["result_recomputed_verified"])
            self.assertTrue(run["portfolio_snapshot_hash_verified"])

            forged_result = copy.deepcopy(result)
            forged_result["summary"]["portfolio_cumulative_return_pct"] = 987654
            with closing(store._connect()) as connection, connection:
                disable_profiled_run_storage_seal(connection)
                connection.execute(
                    """UPDATE paper_portfolio_walk_forward_runs
                       SET result_json=?,result_sha256=? WHERE id=?""",
                    (
                        json.dumps(forged_result, ensure_ascii=False),
                        store._canonical_sha256(forged_result),
                        run["id"],
                    ),
                )
            checked = store.list_paper_portfolio_walk_forward_runs(
                "room_storage",
                portfolio["id"],
            )[0]
            self.assertTrue(checked["result_hash_verified"])
            self.assertFalse(checked["result_recomputed_verified"])
            self.assertFalse(checked["fully_verified"])
            self.assertIn(
                "WALK_FORWARD_RESULT_RECOMPUTE_MISMATCH",
                checked["integrity_issues"],
            )

            with closing(store._connect()) as connection, connection:
                connection.execute(
                    """UPDATE paper_portfolio_walk_forward_runs
                       SET result_json=?,result_sha256=?,portfolio_snapshot_sha256=?
                       WHERE id=?""",
                    (
                        json.dumps(result, ensure_ascii=False),
                        store._canonical_sha256(result),
                        "0" * 64,
                        run["id"],
                    ),
                )
            checked = store.list_paper_portfolio_walk_forward_runs(
                "room_storage",
                portfolio["id"],
            )[0]
            self.assertTrue(checked["result_recomputed_verified"])
            self.assertFalse(checked["portfolio_snapshot_hash_verified"])
            self.assertFalse(checked["fully_verified"])
            self.assertIn(
                "WALK_FORWARD_PORTFOLIO_SNAPSHOT_HASH_INVALID",
                checked["integrity_issues"],
            )

    def test_cross_store_update_cannot_commit_between_final_check_and_insert(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "cross_store_race.sqlite3"
            writer_store = StudioStore(database_path)
            updater_store = StudioStore(database_path)
            portfolio = create_portfolio(writer_store)
            histories = {symbol: futu_history(symbol) for symbol in SYMBOLS}
            frozen_plan = walk_forward_plan(portfolio)
            result = run_walk_forward_backtest(
                histories,
                frozen_plan,
                DEFAULT_WALK_FORWARD_CONFIG,
            )
            input_snapshot = PaperPortfolioService._walk_forward_input_snapshot(
                "room_storage",
                portfolio,
                frozen_plan,
                result["config"],
                histories,
            )

            final_version_read = threading.Event()
            release_writer = threading.Event()
            update_attempted = threading.Event()
            update_finished = threading.Event()
            outcomes: dict[str, object] = {}
            writer_state = {"portfolio_selects": 0}
            original_writer_connect = writer_store._connect
            original_updater_connect = updater_store._connect

            class CursorProxy:
                def __init__(self, cursor, *, pause_after_fetch: bool = False):
                    self._cursor = cursor
                    self._pause_after_fetch = pause_after_fetch

                def fetchone(self):
                    row = self._cursor.fetchone()
                    if self._pause_after_fetch:
                        final_version_read.set()
                        if not release_writer.wait(timeout=10):
                            raise RuntimeError("writer release timed out")
                    return row

                def __getattr__(self, name):
                    return getattr(self._cursor, name)

            class WriterConnectionProxy:
                def __init__(self, connection):
                    self._connection = connection

                def execute(self, sql, parameters=()):
                    cursor = self._connection.execute(sql, parameters)
                    normalized = " ".join(str(sql).split()).lower()
                    pause_after_fetch = False
                    if normalized.startswith(
                        "select * from paper_portfolios where room_id=? and id=?"
                    ):
                        writer_state["portfolio_selects"] += 1
                        pause_after_fetch = writer_state["portfolio_selects"] == 2
                    return CursorProxy(
                        cursor,
                        pause_after_fetch=pause_after_fetch,
                    )

                def __enter__(self):
                    self._connection.__enter__()
                    return self

                def __exit__(self, *args):
                    return self._connection.__exit__(*args)

                def close(self):
                    return self._connection.close()

                def __getattr__(self, name):
                    return getattr(self._connection, name)

            class UpdaterConnectionProxy:
                def __init__(self, connection):
                    self._connection = connection

                def execute(self, sql, parameters=()):
                    normalized = " ".join(str(sql).split()).lower()
                    # The writer now reserves the SQLite write lock before its
                    # final version read. Signal the updater's transaction
                    # attempt before BEGIN IMMEDIATE blocks, not only after it
                    # reaches the UPDATE statement.
                    if normalized == "begin immediate" or normalized.startswith(
                        "update paper_portfolios set"
                    ):
                        update_attempted.set()
                    return self._connection.execute(sql, parameters)

                def __enter__(self):
                    self._connection.__enter__()
                    return self

                def __exit__(self, *args):
                    return self._connection.__exit__(*args)

                def close(self):
                    return self._connection.close()

                def __getattr__(self, name):
                    return getattr(self._connection, name)

            writer_store._connect = lambda: WriterConnectionProxy(
                original_writer_connect()
            )
            updater_store._connect = lambda: UpdaterConnectionProxy(
                original_updater_connect()
            )

            def create_run() -> None:
                try:
                    outcomes["run"] = (
                        writer_store.create_paper_portfolio_walk_forward_run(
                            "room_storage",
                            portfolio["id"],
                            result,
                            input_snapshot,
                            expected_portfolio_version=portfolio["version"],
                            enforce_workflow_gate=False,
                        )
                    )
                except Exception as exc:  # pragma: no cover - asserted below
                    outcomes["writer_error"] = exc

            def update_portfolio() -> None:
                try:
                    outcomes["updated"] = updater_store.update_paper_portfolio(
                        "room_storage",
                        portfolio["id"],
                        paper_plan(),
                        evaluation(),
                        expected_version=portfolio["version"],
                    )
                except Exception as exc:  # pragma: no cover - asserted below
                    outcomes["updater_error"] = exc
                finally:
                    update_finished.set()

            writer_thread = threading.Thread(target=create_run)
            updater_thread = threading.Thread(target=update_portfolio)
            writer_thread.start()
            update_finished_while_writer_paused = False
            try:
                self.assertTrue(
                    final_version_read.wait(timeout=10),
                    "writer did not reach its final portfolio version read",
                )
                updater_thread.start()
                self.assertTrue(
                    update_attempted.wait(timeout=10),
                    "second store did not attempt its portfolio update",
                )
                update_finished_while_writer_paused = update_finished.wait(
                    timeout=0.5
                )
            finally:
                release_writer.set()
                writer_thread.join(timeout=15)
                if updater_thread.ident is not None:
                    updater_thread.join(timeout=15)
                writer_store._connect = original_writer_connect
                updater_store._connect = original_updater_connect

            self.assertFalse(writer_thread.is_alive())
            self.assertFalse(updater_thread.is_alive())
            self.assertFalse(
                update_finished_while_writer_paused,
                "the second store committed while the writer was paused between "
                "its final version check and insert",
            )
            self.assertNotIn("writer_error", outcomes)
            self.assertNotIn("updater_error", outcomes)
            self.assertIsNotNone(outcomes.get("run"))
            updated = outcomes.get("updated")
            self.assertIsInstance(updated, dict)
            self.assertEqual(updated["version"], portfolio["version"] + 1)

            current = updater_store.get_paper_portfolio(
                "room_storage",
                portfolio["id"],
            )
            runs = writer_store.list_paper_portfolio_walk_forward_runs(
                "room_storage",
                portfolio["id"],
            )
            self.assertEqual(current["version"], portfolio["version"] + 1)
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["portfolio_version"], portfolio["version"])
            self.assertEqual(runs[0]["integrity_status"], "verified")

    def test_legacy_v1_without_input_snapshot_is_explicitly_unverifiable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StudioStore(Path(temp_dir) / "legacy.sqlite3")
            portfolio = create_portfolio(store)
            histories = {
                symbol: futu_history(symbol)
                for symbol in SYMBOLS
            }
            frozen_plan = walk_forward_plan(portfolio)
            result = run_walk_forward_backtest(
                histories,
                frozen_plan,
                DEFAULT_WALK_FORWARD_CONFIG,
            )
            input_snapshot = PaperPortfolioService._walk_forward_input_snapshot(
                "room_storage",
                portfolio,
                frozen_plan,
                result["config"],
                histories,
            )
            run = store.create_paper_portfolio_walk_forward_run(
                "room_storage",
                portfolio["id"],
                result,
                input_snapshot,
                expected_portfolio_version=portfolio["version"],
                enforce_workflow_gate=False,
            )
            legacy_config = {
                "version": CONFIG_VERSION_V1,
                "train_days": 99,
                "test_days": 20,
                "step_days": 20,
                "transaction_cost_bps": 10,
                "price_adjustment": "QFQ",
            }
            legacy_result = run_walk_forward_backtest(
                histories,
                frozen_plan,
                legacy_config,
            )
            legacy_result["version"] = "walk_forward_result_v1"
            legacy_result["engine_version"] = "walk_forward_engine_v1"
            legacy_result_hash = store._canonical_sha256(legacy_result)
            with closing(store._connect()) as connection, connection:
                # Reproduce a row that truly predates the immutable provenance
                # profile.  Rewriting a current profiled row is intentionally
                # blocked and covered by the candidate contract tests.
                connection.execute(
                    "DROP TRIGGER trg_walk_forward_integrity_profile_immutable"
                )
                connection.execute(
                    "DROP TRIGGER trg_walk_forward_profiled_run_immutable"
                )
                connection.execute(
                    """UPDATE paper_portfolio_walk_forward_runs
                       SET record_version=1,
                           engine_version='walk_forward_engine_v1',
                           result_version='walk_forward_result_v1',
                            input_hash=?,config_json=?,
                            input_snapshot_sha256='',input_snapshot_json='{}',
                            integrity_profile_json='{}',
                            integrity_profile_sha256='',
                            result_sha256=?,result_json=?
                       WHERE id=?""",
                    (
                        legacy_result["input_hash"],
                        json.dumps(legacy_config, ensure_ascii=False),
                        legacy_result_hash,
                        json.dumps(legacy_result, ensure_ascii=False),
                        run["id"],
                    ),
                )

            legacy = store.list_paper_portfolio_walk_forward_runs(
                "room_storage",
                portfolio["id"],
            )[0]
            self.assertTrue(legacy["result_hash_verified"])
            self.assertIsNone(legacy["input_snapshot_hash_verified"])
            self.assertFalse(legacy["integrity_ok"])
            self.assertFalse(legacy["fully_verified"])
            self.assertEqual(
                legacy["integrity_status"],
                "legacy_unverifiable",
            )
            self.assertEqual(
                legacy["integrity_issues"],
                ["WALK_FORWARD_INPUT_SNAPSHOT_LEGACY_UNVERIFIABLE"],
            )
            self.assertEqual(legacy["integrity_warnings"], [])

            legacy_result["summary"]["portfolio_cumulative_return_pct"] = 999
            with closing(store._connect()) as connection, connection:
                connection.execute(
                    "UPDATE paper_portfolio_walk_forward_runs SET result_json=? WHERE id=?",
                    (json.dumps(legacy_result, ensure_ascii=False), run["id"]),
                )
            tampered_legacy = store.list_paper_portfolio_walk_forward_runs(
                "room_storage",
                portfolio["id"],
            )[0]
            self.assertFalse(tampered_legacy["result_hash_verified"])
            self.assertFalse(tampered_legacy["integrity_ok"])
            self.assertEqual(tampered_legacy["integrity_status"], "failed")


class WalkForwardServiceIntegrationTests(unittest.TestCase):
    def test_default_config_reaches_exact_minimum_at_500_row_limit(self) -> None:
        diagnostic = calculate_walk_forward_feasibility(
            WALK_FORWARD_HISTORY_LIMIT,
            DEFAULT_WALK_FORWARD_CONFIG,
        )

        self.assertEqual(DEFAULT_WALK_FORWARD_CONFIG["train_days"], 99)
        self.assertEqual(DEFAULT_WALK_FORWARD_CONFIG["test_days"], 20)
        self.assertEqual(DEFAULT_WALK_FORWARD_CONFIG["step_days"], 20)
        self.assertEqual(DEFAULT_WALK_FORWARD_CONFIG["version"], CONFIG_VERSION_V2)
        self.assertEqual(DEFAULT_WALK_FORWARD_CONFIG["price_adjustment"], "QFQ")
        self.assertEqual(
            DEFAULT_WALK_FORWARD_CONFIG["friction_scenario_set"],
            STORAGE_FRICTION_SCENARIOS_VERSION,
        )
        self.assertEqual(
            DEFAULT_WALK_FORWARD_CONFIG["unfillable_policy"],
            UNFILLABLE_POLICY,
        )
        self.assertNotIn("transaction_cost_bps", DEFAULT_WALK_FORWARD_CONFIG)
        self.assertEqual(diagnostic["status"], "ready")
        self.assertEqual(diagnostic["maximum_candidate_fold_count"], 20)
        self.assertEqual(
            diagnostic["maximum_non_overlapping_test_fold_count"],
            20,
        )
        self.assertEqual(diagnostic["minimum_common_trading_days"], 500)

    def test_snapshot_v2_freezes_complete_friction_and_raw_row_assumptions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StudioStore(Path(temp_dir) / "snapshot.sqlite3")
            portfolio = create_portfolio(store)
            histories = {
                symbol: futu_history(
                    symbol,
                    daily_return=0.0005 * (index + 1),
                )
                for index, symbol in enumerate(SYMBOLS)
            }
            raw_histories = copy.deepcopy(histories)
            frozen_plan = PaperPortfolioService._walk_forward_plan(
                portfolio,
                evaluation_as_of_date="2026-07-30",
                data_snapshot_cutoff="2026-05-15",
            )

            snapshot = PaperPortfolioService._walk_forward_input_snapshot(
                "room_storage",
                portfolio,
                frozen_plan,
                copy.deepcopy(DEFAULT_WALK_FORWARD_CONFIG),
                histories,
            )

            self.assertEqual(snapshot["version"], INPUT_SNAPSHOT_VERSION_V2)
            self.assertEqual(snapshot["histories"], raw_histories)
            self.assertEqual(snapshot["portfolio_snapshot"], portfolio)
            for history in snapshot["histories"].values():
                self.assertTrue(
                    {"open", "high", "low", "close", "volume", "turnover"}
                    <= set(history["rows"][0])
                )
            assumptions = snapshot["manifest"]["assumptions"]
            self.assertEqual(
                assumptions["friction_scenario_set"],
                get_storage_friction_scenarios(),
            )
            self.assertEqual(
                assumptions["friction_model_version"],
                PAPER_FRICTION_MODEL_VERSION,
            )
            self.assertEqual(
                assumptions["liquidity_proxy_version"],
                PAPER_LIQUIDITY_PROXY_VERSION,
            )
            self.assertEqual(assumptions["unfillable_policy"], UNFILLABLE_POLICY)
            self.assertFalse(assumptions["custom_overrides_allowed"])
            self.assertFalse(assumptions["partial_fills_allowed"])
            self.assertFalse(assumptions["actual_execution_observed"])
            self.assertEqual(
                snapshot["manifest"]["execution_capability"],
                "none",
            )
            self.assertFalse(snapshot["manifest"]["live_trading_allowed"])
            self.assertFalse(snapshot["manifest"]["can_autonomously_decide"])

            histories["US.MU"]["rows"][0]["turnover"] = 1
            portfolio["name"] = "mutated after freeze"
            self.assertEqual(
                snapshot["histories"]["US.MU"]["rows"][0],
                raw_histories["US.MU"]["rows"][0],
            )
            self.assertNotEqual(
                snapshot["portfolio_snapshot"]["name"],
                portfolio["name"],
            )

    def test_alignment_allows_only_leading_history_difference(self) -> None:
        histories = {symbol: futu_history(symbol) for symbol in SYMBOLS}
        histories["US.SNDK"]["rows"] = histories["US.SNDK"]["rows"][40:]
        histories["US.SNDK"]["actual_start"] = histories["US.SNDK"]["rows"][0][
            "market_time"
        ][:10]

        aligned = PaperPortfolioService._align_walk_forward_histories(histories)

        calendars = [
            [str(row["market_time"])[:10] for row in history["rows"]]
            for history in aligned.values()
        ]
        self.assertTrue(all(calendar == calendars[0] for calendar in calendars[1:]))
        self.assertEqual(aligned["US.MU"]["alignment_dropped_leading_rows"], 40)
        self.assertEqual(aligned["US.SNDK"]["alignment_dropped_leading_rows"], 0)
        self.assertEqual(aligned["US.MU"]["actual_start"], aligned["US.SNDK"]["actual_start"])

        with_gap = copy.deepcopy(histories)
        del with_gap["US.WDC"]["rows"][80]
        with self.assertRaisesRegex(ValueError, "存在缺口"):
            PaperPortfolioService._align_walk_forward_histories(with_gap)

    def test_service_reads_all_four_qfq_histories_and_does_not_revise_portfolio(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StudioStore(Path(temp_dir) / "service.sqlite3")
            portfolio = create_actionable_portfolio(store)
            market = FakeWalkForwardMarket()
            service = PaperPortfolioService(store, market)

            run = service.walk_forward(
                "room_storage",
                portfolio["id"],
                {},
                expected_portfolio_version=portfolio["version"],
            )

            self.assertEqual(
                market.calls,
                [("BATCH", WALK_FORWARD_HISTORY_LIMIT)],
            )
            requested_start = date.fromisoformat(market.last_batch_kwargs["start"])
            requested_end = date.fromisoformat(market.last_batch_kwargs["end"])
            self.assertEqual((requested_end - requested_start).days, 1460)
            self.assertEqual(market.last_batch_kwargs["limit"], 500)
            self.assertEqual(run["result"]["symbols"], list(SYMBOLS))
            self.assertEqual(run["result"]["source"], "futu_qfq_daily_history")
            self.assertEqual(run["result"]["price_adjustment"], "QFQ")
            self.assertEqual(run["result"]["summary"]["status"], "sufficient")
            self.assertEqual(
                [
                    scenario["scenario_id"]
                    for scenario in run["result"]["scenario_results"]
                ],
                ["baseline", "stressed", "severe"],
            )
            self.assertTrue(
                all(
                    not scenario["blocked"]
                    for scenario in run["result"]["scenario_results"]
                )
            )
            self.assertEqual(
                run["result"]["summary"]["non_overlapping_test_fold_count"],
                20,
            )
            self.assertEqual(
                run["result"]["summary"]["minimum_non_overlapping_test_folds"],
                20,
            )
            self.assertFalse(run["result"]["out_of_sample_claim"])
            current = store.get_paper_portfolio("room_storage", portfolio["id"])
            self.assertEqual(current["version"], portfolio["version"])
            self.assertEqual(current["status"], portfolio["status"])

    def test_invalid_or_stale_request_fails_before_market_and_offline_is_not_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StudioStore(Path(temp_dir) / "fail_closed.sqlite3")
            portfolio = create_actionable_portfolio(store)
            market = FakeWalkForwardMarket()
            service = PaperPortfolioService(store, market)

            for legacy_config in (
                {"transaction_cost_bps": 10},
                {"transaction_cost": {"bps": 10}},
                {
                    "version": CONFIG_VERSION_V1,
                    "transaction_cost_bps": 10,
                },
            ):
                with self.assertRaisesRegex(ValueError, "未知字段"):
                    service.walk_forward(
                        "room_storage",
                        portfolio["id"],
                        legacy_config,
                        expected_portfolio_version=portfolio["version"],
                    )
                self.assertEqual(market.calls, [])

            with self.assertRaisesRegex(ValueError, "未知字段"):
                service.walk_forward(
                    "room_storage",
                    portfolio["id"],
                    {"account_id": "forbidden"},
                    expected_portfolio_version=portfolio["version"],
                )
            self.assertEqual(market.calls, [])

            with self.assertRaisesRegex(ValueError, "版本已变化"):
                service.walk_forward(
                    "room_storage",
                    portfolio["id"],
                    {},
                    expected_portfolio_version=portfolio["version"] + 1,
                )
            self.assertEqual(market.calls, [])

            offline_market = FakeWalkForwardMarket(ready=False)
            with self.assertRaisesRegex(ValueError, "尚未就绪"):
                PaperPortfolioService(store, offline_market).walk_forward(
                    "room_storage",
                    portfolio["id"],
                    {},
                    expected_portfolio_version=portfolio["version"],
                )
            self.assertEqual(
                store.list_paper_portfolio_walk_forward_runs(
                    "room_storage",
                    portfolio["id"],
                ),
                [],
            )

    def test_unconfirmed_or_unlinked_portfolio_fails_before_market(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StudioStore(Path(temp_dir) / "workflow_gate.sqlite3")
            portfolio = create_portfolio(store)
            market = FakeWalkForwardMarket()
            service = PaperPortfolioService(store, market)

            with self.assertRaisesRegex(ValueError, "用户已确认"):
                service.walk_forward(
                    "room_storage",
                    portfolio["id"],
                    {},
                    expected_portfolio_version=portfolio["version"],
                )
            self.assertEqual(market.calls, [])

            confirmed = store.confirm_paper_portfolio(
                "room_storage",
                portfolio["id"],
                expected_version=portfolio["version"],
                confirmed_by="user",
            )
            with self.assertRaisesRegex(ValueError, "未关联"):
                service.walk_forward(
                    "room_storage",
                    confirmed["id"],
                    {},
                    expected_portfolio_version=confirmed["version"],
                )
            self.assertEqual(market.calls, [])


class WalkForwardHttpIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_store = http_server.STORE
        self.original_market = http_server.STORAGE_MARKET
        http_server.STORE = StudioStore(Path(self.temp_dir.name) / "http.sqlite3")
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

    def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
    ) -> tuple[int, dict]:
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
        try:
            with urlopen(request, timeout=5) as response:
                return (
                    response.status,
                    json.loads(response.read().decode("utf-8")),
                )
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()
            return exc.code, payload

    def test_post_persists_run_get_lists_it_and_stale_version_conflicts(self) -> None:
        path = (
            f"/api/rooms/room_storage/paper-portfolios/"
            f"{self.portfolio['id']}/walk-forward"
        )
        status, created = self.request(
            "POST",
            path,
            {"expected_portfolio_version": self.portfolio["version"]},
        )

        self.assertEqual(status, 201)
        run = created["walk_forward_run"]
        self.assertEqual(run["portfolio_id"], self.portfolio["id"])
        self.assertEqual(run["result"]["execution_capability"], "none")

        status, listed = self.request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(listed["portfolio"]["version"], self.portfolio["version"])
        self.assertEqual(
            [item["id"] for item in listed["walk_forward_runs"]],
            [run["id"]],
        )

        status, conflict = self.request(
            "POST",
            path,
            {"expected_portfolio_version": self.portfolio["version"] + 1},
        )
        self.assertEqual(status, 409)
        self.assertFalse(conflict["ok"])
        self.assertEqual(
            len(http_server.STORE.list_paper_portfolio_walk_forward_runs(
                "room_storage",
                self.portfolio["id"],
            )),
            1,
        )

    def test_one_row_short_returns_auditable_422_without_persisting(self) -> None:
        http_server.STORAGE_MARKET = FakeWalkForwardMarket(days=499)
        path = (
            f"/api/rooms/room_storage/paper-portfolios/"
            f"{self.portfolio['id']}/walk-forward"
        )

        status, failed = self.request(
            "POST",
            path,
            {"expected_portfolio_version": self.portfolio["version"]},
        )

        self.assertEqual(status, 422)
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["code"], INSUFFICIENT_WINDOWS_REASON)
        diagnostic = failed["diagnostic"]
        self.assertEqual(diagnostic["failure_stage"], "pre_fold_generation")
        self.assertEqual(diagnostic["common_trading_days"], 499)
        self.assertEqual(
            diagnostic["maximum_non_overlapping_test_fold_count"],
            19,
        )
        self.assertEqual(diagnostic["minimum_common_trading_days"], 500)
        self.assertEqual(diagnostic["history_row_shortfall"], 1)
        self.assertFalse(diagnostic["window_shortening_allowed"])
        self.assertFalse(diagnostic["synthetic_padding_allowed"])
        self.assertIn("未生成 fold", failed["error"])
        self.assertEqual(
            http_server.STORE.list_paper_portfolio_walk_forward_runs(
                "room_storage",
                self.portfolio["id"],
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
