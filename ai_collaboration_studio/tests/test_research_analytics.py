from __future__ import annotations

import math
import unittest
from datetime import date, timedelta

from backend.market.research_analytics import build_research_analytics, historical_base_rates


def history(symbol: str, *, days: int = 121, phase: float = 0.0) -> dict:
    start = date(2026, 1, 1)
    rows = []
    close = 100.0
    for index in range(days):
        close *= 1 + (0.0015 + 0.012 * math.sin(index / 4 + phase))
        rows.append({
            "symbol": symbol,
            "market_time": f"{start + timedelta(days=index)} 16:00:00",
            "close": round(close, 6),
        })
    return {"ok": True, "symbol": symbol, "rows": rows}


class ResearchAnalyticsTests(unittest.TestCase):
    def test_base_rates_use_non_overlapping_windows(self) -> None:
        rows = []
        close = 100.0
        start = date(2026, 1, 1)
        for index in range(101):
            rows.append({"market_time": f"{start + timedelta(days=index)} 16:00:00", "close": close})
            close *= 1.01

        payload = historical_base_rates(
            {"US.MU": {"rows": rows}},
            horizons=(5,),
            threshold_pct=2,
        )
        result = payload["rows"][0]

        self.assertEqual(result["sample_count"], 20)
        self.assertEqual(result["up_base_rate_pct"], 100.0)
        self.assertEqual(result["down_base_rate_pct"], 0.0)
        self.assertEqual(payload["execution_capability"], "none")
        self.assertFalse(payload["live_trading_allowed"])

    def test_equal_weight_risk_is_deterministic_and_read_only(self) -> None:
        histories = {
            symbol: history(symbol, phase=index * 0.7)
            for index, symbol in enumerate(("US.MU", "US.SNDK", "US.WDC", "US.STX"))
        }

        first = build_research_analytics(histories)
        second = build_research_analytics(histories)
        risk = first["portfolio_risk"]

        self.assertEqual(first, second)
        self.assertEqual(risk["state"], "ready")
        self.assertEqual(risk["symbols"], ["US.MU", "US.SNDK", "US.STX", "US.WDC"])
        self.assertEqual(risk["sample_count"], 120)
        self.assertGreater(risk["annualized_volatility_pct"], 0)
        self.assertLessEqual(risk["max_drawdown_pct"], 0)
        self.assertGreaterEqual(risk["historical_var_95_1d_pct"], 0)
        self.assertEqual(len(risk["correlations"]), 6)
        self.assertEqual(first["execution_capability"], "none")
        self.assertFalse(first["live_trading_allowed"])

    def test_missing_history_stays_explicitly_offline(self) -> None:
        payload = build_research_analytics({"US.MU": {"rows": []}})

        self.assertEqual(payload["portfolio_risk"]["state"], "offline")
        self.assertTrue(payload["portfolio_risk"]["source_errors"])
        self.assertFalse(any(row["sample_count"] for row in payload["base_rates"]["rows"]))


if __name__ == "__main__":
    unittest.main()
