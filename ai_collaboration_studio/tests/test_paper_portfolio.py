from __future__ import annotations

import math
import json
import tempfile
import threading
import unittest
from datetime import date, datetime, time, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from backend import http_server
from backend.paper_portfolio import (
    default_paper_portfolio_plan,
    evaluate_paper_portfolio,
    normalize_paper_portfolio_plan,
)
from backend.paper_portfolio_service import PaperPortfolioService
from backend.store import StudioStore


def history(symbol: str, *, days: int = 140, phase: float = 0.0) -> dict:
    start = date(2026, 1, 1)
    close = 100.0
    rows = []
    for index in range(days):
        close *= 1 + (0.001 + 0.011 * math.sin(index / 5 + phase))
        rows.append({
            "symbol": symbol,
            "market_time": f"{start + timedelta(days=index)} 16:00:00",
            "time": datetime.combine(
                start + timedelta(days=index),
                time(16),
                tzinfo=ZoneInfo("America/New_York"),
            ).astimezone(timezone.utc).isoformat(),
            "open": round(close, 6),
            "high": round(close, 6),
            "low": round(close, 6),
            "close": round(close, 6),
            "volume": 0.0,
            "turnover": 0.0,
        })
    first_session = start
    last_session = start + timedelta(days=days - 1)
    as_of = last_session + timedelta(days=1)
    return {
        "ok": True,
        "source": "futu_opend",
        "interval": "1d",
        "price_adjustment": "QFQ",
        "captured_at": f"{as_of.isoformat()}T20:00:00Z",
        "as_of_date": as_of.isoformat(),
        "last_completed_session": last_session.isoformat(),
        "actual_start": first_session.isoformat(),
        "actual_end": last_session.isoformat(),
        "symbol": symbol,
        "rows": rows,
        "source_errors": [],
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


def valid_plan() -> dict:
    plan = default_paper_portfolio_plan()
    for position, side, weight in zip(
        plan["positions"],
        ("LONG", "LONG", "SHORT", "FLAT"),
        (25, 20, 10, 0),
    ):
        position["side"] = side
        position["weight_pct"] = weight
        if side != "FLAT":
            position["thesis"] = "用于验证纸面组合风险，不代表真实仓位。"
            position["invalidation"] = "关键假设失效时退回用户重新评估。"
    return plan


class FakePortfolioMarket:
    def __init__(self, *, online: bool = True) -> None:
        self.online = online
        self.history_calls: list[str] = []

    def history(self, symbol: str, **_kwargs) -> dict:
        self.history_calls.append(symbol)
        return history(symbol, phase=SYMBOL_PHASES[symbol]) if self.online else {
            "ok": False,
            "symbol": symbol,
            "rows": [],
        }


SYMBOL_PHASES = {
    "US.MU": 0.0,
    "US.SNDK": 0.5,
    "US.WDC": 1.0,
    "US.STX": 1.5,
}


class PaperPortfolioRiskTests(unittest.TestCase):
    def test_production_sources_have_no_futu_order_or_trade_context_calls(self) -> None:
        project = Path(__file__).resolve().parents[1]
        source_files = [
            *project.joinpath("backend").rglob("*.py"),
            *project.joinpath("frontend", "src").rglob("*.js"),
            *project.joinpath("frontend", "src").rglob("*.jsx"),
        ]
        forbidden = (
            "opensecuritytradecontext",
            "opentradecontext",
            "place_order",
            "unlock_trade",
            "modify_order",
            "cancel_order",
        )
        combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in source_files)
        for token in forbidden:
            self.assertNotIn(token, combined)

    def test_weighted_risk_is_deterministic_and_has_no_execution_capability(self) -> None:
        histories = {
            symbol: history(symbol, phase=index * 0.6)
            for index, symbol in enumerate(("US.MU", "US.SNDK", "US.WDC", "US.STX"))
        }

        first = evaluate_paper_portfolio(
            valid_plan(),
            histories,
            evaluated_at="2026-07-25T00:00:00Z",
        )
        second = evaluate_paper_portfolio(
            valid_plan(),
            histories,
            evaluated_at="2026-07-25T00:00:00Z",
        )

        self.assertEqual(first, second)
        self.assertEqual(first["state"], "ready")
        self.assertEqual(first["sample_count"], 139)
        self.assertEqual(first["exposures"]["gross_exposure_pct"], 55)
        self.assertEqual(first["exposures"]["net_exposure_pct"], 35)
        self.assertEqual(first["risk_gate"]["status"], "PASS")
        self.assertEqual(len(first["stress_results"]), 3)
        self.assertEqual(first["execution_capability"], "none")
        self.assertFalse(first["live_trading_allowed"])

    def test_exposure_and_stress_budget_breaches_are_explicit(self) -> None:
        plan = valid_plan()
        plan["budgets"]["max_single_name_pct"] = 20
        plan["budgets"]["max_stress_loss_pct"] = 1
        histories = {
            symbol: history(symbol, phase=index)
            for index, symbol in enumerate(("US.MU", "US.SNDK", "US.WDC"))
        }

        result = evaluate_paper_portfolio(plan, histories)
        by_code = {check["code"]: check for check in result["budget_checks"]}

        self.assertEqual(result["risk_gate"]["status"], "BLOCKED")
        self.assertEqual(by_code["SINGLE_NAME"]["status"], "BREACH")
        self.assertEqual(by_code["STRESS_LOSS"]["status"], "BREACH")

    def test_missing_history_blocks_confirmation_without_fabricated_metrics(self) -> None:
        result = evaluate_paper_portfolio(
            valid_plan(),
            {
                "US.MU": history("US.MU"),
                "US.SNDK": {"ok": False, "rows": []},
                "US.WDC": history("US.WDC"),
            },
        )

        self.assertEqual(result["state"], "offline")
        self.assertEqual(result["risk_gate"]["status"], "BLOCKED")
        self.assertIsNone(result["metrics"]["annualized_volatility_pct"])
        self.assertTrue(result["source_errors"])

    def test_rows_from_untrusted_or_unsafe_history_never_pass_risk_gate(self) -> None:
        histories = {
            symbol: {
                **history(symbol, phase=SYMBOL_PHASES[symbol]),
                "ok": False,
                "source": "untrusted",
                "source_errors": [{"code": "PARTIAL_PAGE"}],
                "execution_capability": "broker",
                "live_trading_allowed": True,
            }
            for symbol in ("US.MU", "US.SNDK", "US.WDC")
        }

        result = evaluate_paper_portfolio(valid_plan(), histories)

        self.assertEqual(result["state"], "offline")
        self.assertEqual(result["risk_gate"]["status"], "BLOCKED")
        self.assertFalse(result["history_contract_ready"])
        self.assertEqual(result["source"], "unverified_history_rejected")
        self.assertEqual(result["sample_count"], 0)
        self.assertTrue(all(
            error["code"] == "PORTFOLIO_HISTORY_CONTRACT_INVALID"
            for error in result["source_errors"]
        ))

    def test_plan_validation_rejects_orders_unknown_symbols_and_empty_positions(self) -> None:
        empty = default_paper_portfolio_plan()
        with self.assertRaisesRegex(ValueError, "至少需要一个"):
            normalize_paper_portfolio_plan(empty)

        plan = valid_plan()
        plan["positions"][0]["symbol"] = "US.AAPL"
        with self.assertRaisesRegex(ValueError, "仅支持"):
            normalize_paper_portfolio_plan(plan)

        plan = valid_plan()
        plan["account_id"] = "should-never-exist"
        with self.assertRaisesRegex(ValueError, "未知字段"):
            normalize_paper_portfolio_plan(plan)


class PaperPortfolioStoreTests(unittest.TestCase):
    def test_non_storage_capability_room_rejects_paper_portfolio(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StudioStore(Path(temp_dir) / "studio.sqlite3")
            evaluation = evaluate_paper_portfolio(
                valid_plan(),
                {
                    symbol: history(symbol, phase=index)
                    for index, symbol in enumerate(("US.MU", "US.SNDK", "US.WDC"))
                },
                evaluated_at="2026-07-25T00:00:00Z",
            )
            with self.assertRaisesRegex(ValueError, "未启用模拟组合能力包"):
                store.create_paper_portfolio(
                    "room_plan",
                    valid_plan(),
                    evaluation,
                    created_by="user",
                )

    def test_store_versions_confirmation_and_prompt_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StudioStore(Path(temp_dir) / "studio.sqlite3")
            evaluation = evaluate_paper_portfolio(
                valid_plan(),
                {
                    symbol: history(symbol, phase=index)
                    for index, symbol in enumerate(("US.MU", "US.SNDK", "US.WDC"))
                },
                evaluated_at="2026-07-25T00:00:00Z",
            )

            created = store.create_paper_portfolio(
                "room_storage",
                valid_plan(),
                evaluation,
                created_by="user",
            )
            self.assertEqual(created["status"], "DRAFT")
            self.assertEqual(created["version"], 1)

            revised_plan = valid_plan()
            revised_plan["name"] = "修订后的模拟组合"
            updated = store.update_paper_portfolio(
                "room_storage",
                created["id"],
                revised_plan,
                evaluation,
                expected_version=1,
            )
            self.assertEqual(updated["version"], 2)
            self.assertEqual(updated["status"], "DRAFT")

            confirmed = store.confirm_paper_portfolio(
                "room_storage",
                created["id"],
                expected_version=2,
                confirmed_by="user",
            )
            self.assertEqual(confirmed["status"], "CONFIRMED")
            self.assertTrue(confirmed["user_confirmed"])
            self.assertIn(created["id"], store.paper_portfolio_prompt_context("room_storage"))
            self.assertIn("execution_capability", store.paper_portfolio_prompt_context("room_storage"))

            snapshot = store.room_snapshot("room_storage")
            self.assertEqual(snapshot["paper_portfolios"][0]["id"], created["id"])
            self.assertNotIn("positions_json", snapshot["paper_portfolios"][0])
            self.assertEqual(
                len(store.list_paper_portfolio_versions("room_storage", created["id"])),
                2,
            )

    def test_service_reads_only_active_futu_histories_and_blocks_offline_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StudioStore(Path(temp_dir) / "studio.sqlite3")
            market = FakePortfolioMarket()
            service = PaperPortfolioService(store, market)

            created = service.create("room_storage", valid_plan())

            self.assertEqual(
                market.history_calls,
                ["US.MU", "US.SNDK", "US.WDC"],
            )
            self.assertEqual(created["evaluation"]["risk_gate"]["status"], "PASS")
            confirmed = service.confirm(
                "room_storage",
                created["id"],
                expected_version=created["version"],
            )
            self.assertEqual(confirmed["status"], "CONFIRMED")

            offline_service = PaperPortfolioService(store, FakePortfolioMarket(online=False))
            offline = offline_service.create("room_storage", {
                **valid_plan(),
                "name": "离线组合",
            })
            with self.assertRaisesRegex(ValueError, "数据缺口"):
                offline_service.confirm(
                    "room_storage",
                    offline["id"],
                    expected_version=offline["version"],
                )


class PaperPortfolioHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_store = http_server.STORE
        self.original_market = http_server.STORAGE_MARKET
        http_server.STORE = StudioStore(Path(self.temp_dir.name) / "http.sqlite3")
        http_server.STORAGE_MARKET = FakePortfolioMarket()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), http_server.StudioRequestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        http_server.STORE = self.original_store
        http_server.STORAGE_MARKET = self.original_market
        self.temp_dir.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
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
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_http_create_update_list_and_confirm(self) -> None:
        status, created_payload = self.request(
            "POST",
            "/api/rooms/room_storage/paper-portfolios",
            valid_plan(),
        )
        portfolio = created_payload["portfolio"]
        self.assertEqual(status, 201)
        self.assertEqual(portfolio["status"], "DRAFT")

        status, list_payload = self.request(
            "GET",
            "/api/rooms/room_storage/paper-portfolios",
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(list_payload["paper_portfolios"]), 1)

        updated_plan = valid_plan()
        updated_plan["name"] = "HTTP 修订组合"
        status, updated_payload = self.request(
            "PATCH",
            f"/api/rooms/room_storage/paper-portfolios/{portfolio['id']}",
            {**updated_plan, "expected_version": portfolio["version"]},
        )
        updated = updated_payload["portfolio"]
        self.assertEqual(status, 200)
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["name"], "HTTP 修订组合")

        status, confirmed_payload = self.request(
            "POST",
            f"/api/rooms/room_storage/paper-portfolios/{portfolio['id']}/confirm",
            {"expected_version": updated["version"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(confirmed_payload["portfolio"]["status"], "CONFIRMED")


if __name__ == "__main__":
    unittest.main()
