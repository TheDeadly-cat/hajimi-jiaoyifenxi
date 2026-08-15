from __future__ import annotations

import unittest
from datetime import datetime, timezone

from backend.market.futu_readonly import STORAGE_SYMBOLS
from backend.market.sec_edgar import SEC_TICKERS_URL, SecEdgarAdapter


FIXED_NOW = datetime(2026, 7, 19, 20, 0, tzinfo=timezone.utc)


class FakeSecFetcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url: str, user_agent: str) -> dict:
        self.calls.append((url, user_agent))
        if url == SEC_TICKERS_URL:
            return {
                str(index): {
                    "cik_str": 1_000_000 + index,
                    "ticker": symbol.removeprefix("US."),
                    "title": f"{symbol} Corp",
                }
                for index, symbol in enumerate(STORAGE_SYMBOLS)
            }
        return {
            "name": "Official Company Name",
            "filings": {
                "recent": {
                    "accessionNumber": [
                        "0001000000-26-000003",
                        "0001000000-26-000002",
                        "0001000000-26-000001",
                        "0001000000-25-000099",
                    ],
                    "form": ["8-K", "10-Q", "S-1", "10-K"],
                    "filingDate": ["2026-07-18", "2026-07-20", "2026-06-01", "2025-12-31"],
                    "reportDate": ["2026-07-18", "2026-06-30", "", "2025-11-30"],
                    "acceptanceDateTime": [
                        "2026-07-18T21:10:00Z",
                        "2026-07-20T10:00:00Z",
                        "2026-06-01T10:00:00Z",
                        "2025-12-31T20:00:00Z",
                    ],
                    "primaryDocument": ["event.htm", "quarter.htm", "s1.htm", "annual.htm"],
                    "primaryDocDescription": ["Current report", "Quarterly report", "Registration", "Annual report"],
                    "items": ["2.02,9.01", "", "", ""],
                },
            },
        }


class SecEdgarAdapterTests(unittest.TestCase):
    def make_adapter(self, fetcher: FakeSecFetcher, user_agent: str = "AI Studio contact@example.com") -> SecEdgarAdapter:
        return SecEdgarAdapter(
            user_agent=user_agent,
            fetch_json=fetcher,
            clock=lambda: FIXED_NOW,
            min_request_interval_seconds=0.11,
        )

    def test_batch_uses_official_mapping_and_filters_forms_and_future_dates(self) -> None:
        fetcher = FakeSecFetcher()
        payload = self.make_adapter(fetcher).recent_filings_batch(STORAGE_SYMBOLS, limit=8)

        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["rows"]), 4)
        self.assertEqual(len(fetcher.calls), 5)
        self.assertTrue(all(call[1] == "AI Studio contact@example.com" for call in fetcher.calls))
        first = payload["rows"][0]
        self.assertEqual([item["form"] for item in first["filings"]], ["8-K", "10-K"])
        self.assertEqual(first["filings"][0]["source_tier"], "primary")
        self.assertIn("https://www.sec.gov/Archives/edgar/data/", first["filings"][0]["official_url"])
        self.assertFalse(payload["live_trading_allowed"])

    def test_cache_avoids_duplicate_official_requests(self) -> None:
        fetcher = FakeSecFetcher()
        adapter = self.make_adapter(fetcher)

        first = adapter.recent_filings_batch(STORAGE_SYMBOLS)
        call_count = len(fetcher.calls)
        second = adapter.recent_filings_batch(STORAGE_SYMBOLS)

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(len(fetcher.calls), call_count)
        self.assertTrue(all(row["cache_hit"] for row in second["rows"]))

    def test_unconfigured_user_agent_never_calls_sec(self) -> None:
        fetcher = FakeSecFetcher()
        payload = self.make_adapter(fetcher, user_agent="").recent_filings_batch(["US.MU"])

        self.assertFalse(payload["ok"])
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(payload["source_errors"][0]["code"], "SEC_USER_AGENT_REQUIRED")

        injected = self.make_adapter(fetcher, user_agent="AI Studio contact@example.com\r\nX-Test: bad")
        self.assertFalse(injected.status()["configured"])

    def test_symbol_and_form_whitelists_are_strict(self) -> None:
        fetcher = FakeSecFetcher()
        adapter = self.make_adapter(fetcher)
        with self.assertRaisesRegex(ValueError, "白名单"):
            adapter.recent_filings_batch(["US.AAPL"])
        with self.assertRaisesRegex(ValueError, "SEC 表单"):
            adapter.recent_filings_batch(["US.MU"], forms=["S-1"])
        self.assertEqual(fetcher.calls, [])

    def test_default_fetcher_rejects_non_sec_endpoint_before_network(self) -> None:
        with self.assertRaisesRegex(ValueError, "非官方固定端点"):
            SecEdgarAdapter._default_fetch_json("https://example.com/data.json", "AI Studio contact@example.com")


if __name__ == "__main__":
    unittest.main()
