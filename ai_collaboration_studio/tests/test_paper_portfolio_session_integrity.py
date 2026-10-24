"""Supplied-session alignment for paper risk; no exchange-calendar claim."""
from __future__ import annotations

import copy
from datetime import date, datetime, time, timedelta, timezone
import math
import tempfile
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

from backend.market.futu_readonly import validate_readonly_daily_history
from backend.paper_portfolio import default_paper_portfolio_plan, evaluate_paper_portfolio
from backend.paper_portfolio_service import PaperPortfolioService
from backend.store import StudioStore


SYMBOLS = ("US.MU", "US.SNDK", "US.WDC")
EVALUATED_AT = "2026-08-01T00:00:00Z"


def plan():
    value = default_paper_portfolio_plan()
    for position, side, weight in zip(value["positions"], ("LONG", "LONG", "SHORT", "FLAT"), (25, 20, 10, 0)):
        position.update(side=side, weight_pct=weight)
    return value


def histories():
    sessions = []
    day = date(2026, 1, 5)
    while len(sessions) < 140:
        if day.weekday() < 5:
            sessions.append(day)
        day += timedelta(days=1)
    output = {}
    for number, symbol in enumerate(SYMBOLS):
        close, rows = 100.0, []
        for index, day in enumerate(sessions):
            close *= 1 + (0.001 + 0.011 * math.sin(index / 5 + number * 0.6))
            price = round(close, 6)
            rows.append({"symbol": symbol, "market_time": f"{day} 16:00:00",
                "time": datetime.combine(day, time(16), tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc).isoformat(),
                "open": price, "high": price, "low": price, "close": price, "volume": 0.0, "turnover": 0.0})
        as_of = sessions[-1] + timedelta(days=1)
        output[symbol] = {"ok": True, "source": "futu_opend", "symbol": symbol,
            "interval": "1d", "price_adjustment": "QFQ", "rows": rows,
            "actual_start": str(sessions[0]), "actual_end": str(sessions[-1]),
            "last_completed_session": str(sessions[-1]), "as_of_date": str(as_of),
            "captured_at": f"{as_of}T20:00:00Z", "source_errors": [],
            "execution_capability": "none", "live_trading_allowed": False}
    return output


def keep_rows(value, rows):
    result = copy.deepcopy(value)
    result["rows"] = copy.deepcopy(rows)
    result["actual_start"] = rows[0]["market_time"][:10]
    result["actual_end"] = result["last_completed_session"] = rows[-1]["market_time"][:10]
    return result


def evaluate(values, plan_value=None):
    return evaluate_paper_portfolio(plan_value or plan(), values, evaluated_at=EVALUATED_AT)


class PaperPortfolioSessionIntegrityTests(unittest.TestCase):
    def assert_session_rejected(self, values):
        for symbol, value in values.items():
            self.assertTrue(validate_readonly_daily_history(value, expected_symbol=symbol)["ready"])
        result = evaluate(values)
        self.assertEqual(result["state"], "offline")
        self.assertEqual(result["risk_gate"]["status"], "BLOCKED")
        self.assertFalse(result["history_contract_ready"])
        self.assertEqual(result["sample_count"], 0)
        self.assertEqual(result["first_return_date"], "")
        self.assertEqual(result["last_return_date"], "")
        for name, value in result["metrics"].items():
            self.assertEqual(value, "" if name == "worst_day_date" else None)
        self.assertIn("PORTFOLIO_HISTORY_SESSION_MISMATCH", {error["code"] for error in result["source_errors"]})
        self.assertEqual(result["active_symbols"], list(SYMBOLS))
        self.assertEqual(result["exposures"], evaluate(histories())["exposures"])
        for item in result["budget_checks"]:
            if item["code"] in {"ANNUALIZED_VOLATILITY", "HISTORICAL_VAR_95_1D", "MAX_DRAWDOWN", "WORST_5D_LOSS"}:
                self.assertEqual(item["status"], "UNAVAILABLE")
                self.assertIsNone(item["actual_pct"])
        self.assertEqual(result["execution_capability"], "none")
        self.assertFalse(result["live_trading_allowed"])
        return result

    def test_each_active_long_and_short_rejects_internal_or_trailing_session_gap(self):
        for symbol in SYMBOLS:
            for index in (1, 50, -2, -1):
                with self.subTest(symbol=symbol, index=index):
                    values = histories()
                    rows = values[symbol]["rows"]
                    rows.pop(index)
                    values[symbol] = keep_rows(values[symbol], rows)
                    self.assert_session_rejected(values)

    def test_equal_row_counts_with_different_missing_sessions_are_rejected(self):
        values = histories()
        for symbol, index in zip(SYMBOLS, (20, 21, 22)):
            values[symbol]["rows"].pop(index)
        self.assert_session_rejected(values)

    def test_complete_weekday_histories_preserve_one_session_weekend_returns(self):
        values = histories()
        result = evaluate(values)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["sample_count"], 139)
        self.assertEqual(result["risk_gate"]["status"], "PASS")
        self.assertEqual(result["source_errors"], [])
        self.assertEqual(values["US.MU"]["rows"][4]["market_time"][:10], "2026-01-09")
        self.assertEqual(values["US.MU"]["rows"][5]["market_time"][:10], "2026-01-12")

    def test_inception_prefix_may_be_trimmed_without_changing_weights(self):
        values = histories()
        values["US.SNDK"] = keep_rows(values["US.SNDK"], values["US.SNDK"]["rows"][10:])
        result = evaluate(values)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["sample_count"], 129)
        self.assertEqual(result["risk_gate"]["status"], "PASS")
        self.assertEqual(result["exposures"], evaluate(histories())["exposures"])
        self.assertEqual(result["source_errors"], [])

    def test_inactive_symbol_history_does_not_change_the_portfolio(self):
        values = histories()
        before = evaluate(values)
        values["US.STX"] = {"ok": False, "rows": [], "source_errors": [{"code": "UNAVAILABLE"}]}
        self.assertEqual(evaluate(values), before)

    def test_single_active_symbol_does_not_require_unowned_positions(self):
        value = plan()
        value["positions"][1].update(side="FLAT", weight_pct=0)
        value["positions"][2].update(side="FLAT", weight_pct=0)
        result = evaluate({"US.MU": histories()["US.MU"]}, value)
        self.assertEqual(result["active_symbols"], ["US.MU"])
        self.assertEqual(result["sample_count"], 139)
        self.assertEqual(result["source_errors"], [])

    def test_shared_missing_day_remains_an_explicit_unverified_calendar_limit(self):
        # Agreement cannot detect a row missing from every supplied symbol.
        # This control prevents the scoped fix being claimed as calendar proof.
        values = histories()
        for history in values.values():
            history["rows"].pop(50)
        result = evaluate(values)
        self.assertEqual(result["sample_count"], 138)
        self.assertEqual(result["source_errors"], [])
        self.assertNotIn("calendar_completeness_verified", result)

    def test_service_cannot_confirm_gap_evaluation_and_keeps_draft(self):
        values = histories()
        values["US.SNDK"]["rows"].pop(50)
        class Market:
            def history(self, symbol, **kwargs):
                return copy.deepcopy(values[symbol])
        with tempfile.TemporaryDirectory(prefix="paper-session-gap-") as directory:
            store = StudioStore(Path(directory) / "synthetic.sqlite3")
            service = PaperPortfolioService(store, Market())
            created = service.create("room_storage", plan())
            self.assertEqual(created["status"], "DRAFT")
            self.assertEqual(created["evaluation"]["risk_gate"]["status"], "BLOCKED")
            with self.assertRaisesRegex(ValueError, "数据缺口"):
                service.confirm("room_storage", created["id"], expected_version=created["version"])
            self.assertEqual(store.get_paper_portfolio("room_storage", created["id"])["status"], "DRAFT")


if __name__ == "__main__":
    unittest.main()
