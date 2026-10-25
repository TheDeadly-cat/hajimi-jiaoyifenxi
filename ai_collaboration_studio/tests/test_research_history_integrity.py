from __future__ import annotations

import copy
import unittest
from datetime import date, timedelta

from backend.market.futu_readonly import FutuUsMarketAdapter, STORAGE_SYMBOLS, validate_readonly_daily_history
from backend.market.research_analytics import build_research_analytics, historical_base_rates
from backend.market.storage_service import StorageResearchMarketService
from backend.market.technical_metrics import calculate_technical_metrics
from tests.test_market_data import (
    FIXED_NOW, FakeFutuSdk, FakeIndustryProxyAdapter, FakeOfficialEarningsMaterialsAdapter,
    FakeOfficialIrAdapter, FakeSecEdgarAdapter,
)


def weekdays(count: int = 62) -> list[dict]:
    day = date(2026, 1, 5)
    rows = []
    while len(rows) < count:
        if day.weekday() < 5:
            rows.append({"market_time": f"{day} 16:00:00", "time": f"{day}T21:00:00Z",
                         "close": 100.0, "volume": 1000})
        day += timedelta(days=1)
    return rows


class ResearchHistoryIntegrityTests(unittest.TestCase):
    def assert_rejected(self, rows: list) -> None:
        technical = calculate_technical_metrics("US.MU", rows)
        analytics = build_research_analytics({"US.MU": {"rows": rows}, "US.SNDK": {"rows": rows}})
        self.assertEqual(technical["quality"], "unavailable")
        self.assertIsNone(technical["return_1d_pct"])
        self.assertTrue(technical["source_errors"])
        self.assertTrue(all(row["quality"] == "unavailable" for row in analytics["base_rates"]["rows"]))
        self.assertTrue(analytics["base_rates"]["source_errors"])
        self.assertEqual(analytics["portfolio_risk"]["state"], "offline")
        self.assertTrue(analytics["portfolio_risk"]["source_errors"])

    def test_missing_nonfinite_or_nonpositive_close_never_collapses_session_positions(self) -> None:
        for index in (0, 30, -2, -1):
            for value in (None, float("nan"), float("inf"), -float("inf"), 0, -1, True, "bad"):
                rows = weekdays()
                rows[-1]["close"] = 121.0
                rows[index]["close"] = value
                with self.subTest(index=index, value=str(value)):
                    self.assert_rejected(rows)

    def test_duplicate_same_session_and_invalid_row_are_not_silently_deduplicated(self) -> None:
        base = weekdays()
        duplicate = copy.deepcopy(base)
        duplicate.insert(20, {**duplicate[19], "market_time": duplicate[19]["market_time"][:10] + " 17:00:00"})
        malformed = copy.deepcopy(base)
        malformed[20] = None
        bad_date = copy.deepcopy(base)
        bad_date[20]["market_time"] = "2026-02-30 16:00:00"
        for rows in (duplicate, malformed, bad_date):
            with self.subTest(kind=type(rows[20]).__name__):
                self.assert_rejected(rows)

    def test_complete_history_keeps_numeric_results_and_friday_monday_is_one_session(self) -> None:
        rows = weekdays(61)  # Ends Monday 2026-03-30, following Friday.
        rows[-1]["close"] = 121.0
        result = calculate_technical_metrics("US.MU", rows)
        self.assertEqual(rows[-2]["market_time"][:10], "2026-03-27")
        self.assertEqual(rows[-1]["market_time"][:10], "2026-03-30")
        self.assertEqual(result["quality"], "ready")
        self.assertEqual(result["return_1d_pct"], 21.0)
        self.assertEqual(result["sma20"], 101.05)
        self.assertEqual(result["source_errors"], [])
        rates = historical_base_rates({"US.MU": {"rows": rows}}, horizons=(1,))
        self.assertEqual(rates["rows"][0]["sample_count"], 60)
        self.assertEqual(rates["rows"][0]["average_return_pct"], 0.35)
        self.assertEqual(rates["source_errors"], [])

    def test_portfolio_does_not_intersect_away_internal_or_trailing_missing_session(self) -> None:
        complete = weekdays()
        for position in (20, -1):
            missing = copy.deepcopy(complete)
            missing.pop(position)
            result = build_research_analytics({"US.MU": {"rows": complete}, "US.SNDK": {"rows": missing}})
            risk = result["portfolio_risk"]
            with self.subTest(position=position):
                self.assertEqual(risk["state"], "offline")
                self.assertEqual(risk["sample_count"], 0)
                self.assertIn("PORTFOLIO_HISTORY_SESSION_MISMATCH", [error["code"] for error in risk["source_errors"]])

    def test_portfolio_allows_different_inception_and_does_not_change_weighted_members(self) -> None:
        rows = weekdays()
        result = build_research_analytics({"US.MU": {"rows": rows}, "US.SNDK": {"rows": rows[5:]}})
        self.assertEqual(result["portfolio_risk"]["sample_count"], 56)
        self.assertEqual(result["portfolio_risk"]["source_errors"], [])
        partial = build_research_analytics({"US.MU": {"rows": rows}, "US.SNDK": {"rows": rows}, "US.WDC": {"rows": []}})
        self.assertEqual(partial["portfolio_risk"]["state"], "offline")
        self.assertEqual(partial["portfolio_risk"]["sample_count"], 0)

    def test_explicit_partial_source_failure_is_not_ignored_by_pure_projections(self) -> None:
        history = {"rows": weekdays(), "ok": False, "source_errors": [{"source": "fixture", "code": "PARTIAL_PAGE", "message": "synthetic"}]}
        result = build_research_analytics({"US.MU": history, "US.SNDK": history})
        self.assertEqual(result["base_rates"]["state"], "offline")
        self.assertEqual(result["portfolio_risk"]["state"], "offline")
        self.assertIn("PARTIAL_PAGE", [error["code"] for error in result["base_rates"]["source_errors"]])


class ResearchSnapshotIntegrityTests(unittest.TestCase):
    def service(self, mutation=None):
        class MutatingAdapter(FutuUsMarketAdapter):
            def daily_history_batch(self, *args, **kwargs):
                payload = super().daily_history_batch(*args, **kwargs)
                if mutation:
                    mutation(payload["histories"]["US.MU"])
                return payload
        adapter = MutatingAdapter(sdk_module=FakeFutuSdk(), socket_probe=lambda _host, _port: True,
                                  clock=lambda: FIXED_NOW)
        return StorageResearchMarketService(adapter, sec_adapter=FakeSecEdgarAdapter(),
                                            ir_adapter=FakeOfficialIrAdapter(),
                                            earnings_materials_adapter=FakeOfficialEarningsMaterialsAdapter(),
                                            industry_adapter=FakeIndustryProxyAdapter())

    def test_snapshot_reuses_strict_envelope_gate_and_publishes_failure_reason(self) -> None:
        mutations = [lambda h: h.update(source="untrusted"),
                     lambda h: h.update(source_errors=[{"code": "PARTIAL_PAGE"}]),
                     lambda h: h.update(source_errors={"code": "MALFORMED_ERROR_SHAPE"}),
                     lambda h: h["rows"][0].update(close=None),
                     lambda h: h["rows"][-1].update(high=float("nan")),
                     lambda h: h.update(live_trading_allowed=True)]
        for mutation in mutations:
            evidence = self.service(mutation).snapshot(force=True)["evidence"]
            technical = next(row for row in evidence["technical"]["rows"] if row["symbol"] == "US.MU")
            with self.subTest(issues=technical["source_errors"]):
                self.assertEqual(evidence["state"], "degraded")
                self.assertFalse(evidence["structure_ready"])
                self.assertEqual(technical["quality"], "unavailable")
                self.assertIsNone(technical["return_1d_pct"])
                self.assertIn("HISTORY_VALIDATION_FAILED", [e["code"] for e in evidence["technical"]["source_errors"]])
                self.assertEqual(evidence["research_analytics"]["portfolio_risk"]["state"], "offline")

    def test_snapshot_complete_control_retains_existing_ready_structure(self) -> None:
        evidence = self.service().snapshot(force=True)["evidence"]
        self.assertTrue(evidence["structure_ready"])
        self.assertTrue(all(row["quality"] == "limited" for row in evidence["technical"]["rows"]))
        self.assertEqual(evidence["technical"]["source_errors"], [])

    def test_strict_gate_rejects_different_times_in_same_daily_session(self) -> None:
        service = self.service()
        history = service.adapter.daily_history_batch(STORAGE_SYMBOLS)["histories"]["US.MU"]
        duplicate = copy.deepcopy(history["rows"][0])
        duplicate["market_time"] = "2026-07-17 01:00:00"
        duplicate["time"] = "2026-07-17T05:00:00.000Z"
        history["rows"].insert(1, duplicate)
        report = validate_readonly_daily_history(history, expected_symbol="US.MU")
        self.assertFalse(report["ready"])
        self.assertIn("HISTORY_ROW_1_DUPLICATE_SESSION", report["issues"])


if __name__ == "__main__":
    unittest.main()
