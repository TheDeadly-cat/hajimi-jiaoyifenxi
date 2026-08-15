from __future__ import annotations

import unittest

from backend.market.earnings_metrics import official_earnings_metrics


class OfficialEarningsMetricsTests(unittest.TestCase):
    def test_all_four_storage_companies_have_located_q3_metrics(self) -> None:
        metrics_by_symbol = {
            symbol: official_earnings_metrics(symbol, "FY2026-Q3")
            for symbol in ("US.MU", "US.SNDK", "US.WDC", "US.STX")
        }

        self.assertEqual(len(metrics_by_symbol["US.MU"]), 6)
        self.assertEqual(len(metrics_by_symbol["US.SNDK"]), 4)
        self.assertEqual(len(metrics_by_symbol["US.WDC"]), 4)
        self.assertEqual(len(metrics_by_symbol["US.STX"]), 3)
        self.assertEqual(
            next(item for item in metrics_by_symbol["US.WDC"] if item["metric_name"] == "Nearline HDD exabytes shipped")["numeric_value"],
            199.0,
        )
        self.assertEqual(
            next(item for item in metrics_by_symbol["US.STX"] if item["metric_name"] == "Total HDD exabytes shipped")["value_text"],
            "199 EB; up 39% YoY",
        )

    def test_metrics_are_company_claims_with_locators_not_trade_signals(self) -> None:
        metrics = official_earnings_metrics("US.MU", "FY2026-Q3")

        self.assertTrue(metrics)
        self.assertEqual(len({item["metric_id"] for item in metrics}), len(metrics))
        for metric in metrics:
            self.assertTrue(metric["source_url"].startswith("https://"))
            self.assertIn("PDF page", metric["source_locator"])
            self.assertIn(metric["fact_or_guidance"], {"historical_fact", "company_guidance"})
            self.assertEqual(metric["claim_status"], "company_statement")
            self.assertEqual(metric["verification_method"], "manual_source_locator_review")
            self.assertEqual(metric["execution_capability"], "none")
            self.assertFalse(metric["live_trading_allowed"])
            for forbidden in ("score", "signal", "win_rate", "position_size", "order"):
                self.assertNotIn(forbidden, metric)

    def test_unknown_period_does_not_fabricate_metrics(self) -> None:
        self.assertEqual(official_earnings_metrics("US.MU", "FY2025-Q4"), [])


if __name__ == "__main__":
    unittest.main()
