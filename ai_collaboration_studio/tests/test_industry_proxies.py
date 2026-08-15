from __future__ import annotations

import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from backend.market.industry_proxies import FredIndustryProxyAdapter, INDUSTRY_PROXY_SERIES


FIXED_NOW = datetime(2026, 7, 19, 20, 0, tzinfo=timezone.utc)


class FakeFredFetcher:
    def __init__(self, fail_series: str = "") -> None:
        self.fail_series = fail_series
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        series_id = (parse_qs(urlparse(url).query).get("id") or [""])[0]
        self.calls.append(series_id)
        if series_id == self.fail_series:
            raise OSError("official source unavailable")
        base = 200 if series_id == "U34BTI" else 100
        rows = ["observation_date," + series_id]
        for index, month in enumerate(range(6, 20)):
            year = 2025 + (month - 1) // 12
            normalized_month = (month - 1) % 12 + 1
            rows.append(f"{year:04d}-{normalized_month:02d}-01,{base + index}")
        rows.append("2026-08-01,9999")
        rows.append("invalid-date,.")
        return ("\n".join(rows) + "\n").encode("utf-8")


class FredIndustryProxyAdapterTests(unittest.TestCase):
    @staticmethod
    def make_adapter(fetcher: FakeFredFetcher) -> FredIndustryProxyAdapter:
        return FredIndustryProxyAdapter(
            fetch_bytes=fetcher,
            clock=lambda: FIXED_NOW,
            cache_ttl_seconds=3600,
        )

    def test_fixed_official_series_produce_scoped_metrics_and_inventory_ratio(self) -> None:
        fetcher = FakeFredFetcher()
        adapter = self.make_adapter(fetcher)

        first = adapter.snapshot()
        second = adapter.snapshot()

        self.assertEqual(first["state"], "ready")
        self.assertEqual(len(first["rows"]), len(INDUSTRY_PROXY_SERIES))
        self.assertEqual(len(fetcher.calls), len(INDUSTRY_PROXY_SERIES))
        self.assertFalse(first["cache"]["hit"])
        self.assertTrue(second["cache"]["hit"])
        self.assertTrue(all(row["as_of"] == "2026-07-01" for row in first["rows"]))
        self.assertTrue(all(row["latest"] != 9999 for row in first["rows"]))
        self.assertEqual(first["derived"][0]["metric_id"], "storage_device_inventory_to_shipments")
        self.assertEqual(first["derived"][0]["as_of"], "2026-07-01")
        self.assertFalse(first["live_trading_allowed"])
        self.assertIn("不是 DRAM/NAND/HDD 即时报价", first["interpretation"])

    def test_one_series_failure_is_isolated_without_substitute_values(self) -> None:
        payload = self.make_adapter(FakeFredFetcher(fail_series="U34BTI")).snapshot()

        self.assertEqual(payload["state"], "degraded")
        self.assertEqual(len(payload["rows"]), len(INDUSTRY_PROXY_SERIES) - 1)
        self.assertEqual(payload["derived"], [])
        self.assertEqual(payload["source_errors"][0]["series_id"], "U34BTI")

    def test_default_fetcher_rejects_non_whitelisted_endpoint_before_network(self) -> None:
        with self.assertRaisesRegex(ValueError, "固定 FRED"):
            FredIndustryProxyAdapter._default_fetch_bytes("https://example.com/series.csv?id=U34BVS")
        with self.assertRaisesRegex(ValueError, "固定 FRED"):
            FredIndustryProxyAdapter._default_fetch_bytes(
                "https://fred.stlouisfed.org/graph/fredgraph.csv?id=NOT_ALLOWED"
            )


if __name__ == "__main__":
    unittest.main()
