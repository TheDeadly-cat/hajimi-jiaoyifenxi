from __future__ import annotations

import unittest
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.parse import urlparse

import backend.market.ir_releases as ir_releases_module
from backend.market.futu_readonly import STORAGE_SYMBOLS
from backend.market.ir_releases import IR_FEEDS, OfficialIrReleaseAdapter
from backend.market.storage_service import StorageResearchMarketService


FIXED_NOW = datetime(2026, 7, 19, 20, 0, tzinfo=timezone.utc)


class FakeIrFetcher:
    def __init__(self, fail_symbol_host: str = "") -> None:
        self.calls: list[tuple[str, set[str]]] = []
        self.fail_symbol_host = fail_symbol_host

    def __call__(self, url: str, allowed_hosts: set[str]) -> bytes:
        self.calls.append((url, allowed_hosts))
        feed_host = urlparse(url).hostname or ""
        if feed_host == self.fail_symbol_host:
            raise OSError("upstream unavailable")
        official_host = feed_host
        return f"""<?xml version="1.0" encoding="utf-8"?>
        <rss version="2.0"><channel>
          <item>
            <title>Official storage update</title>
            <link>http://{official_host}/news/official-update</link>
            <pubDate>Sat, 18 Jul 2026 16:00:00 -0400</pubDate>
            <description><![CDATA[<p>Primary <b>company</b> statement.</p>]]></description>
          </item>
          <item>
            <title>External mirror</title>
            <link>https://example.com/mirror</link>
            <pubDate>Sat, 18 Jul 2026 16:00:00 -0400</pubDate>
          </item>
          <item>
            <title>Future release</title>
            <link>https://{official_host}/news/future</link>
            <pubDate>Mon, 20 Jul 2026 16:00:00 -0400</pubDate>
          </item>
        </channel></rss>""".encode()


class OfficialIrReleaseAdapterTests(unittest.TestCase):
    def make_adapter(self, fetcher: FakeIrFetcher) -> OfficialIrReleaseAdapter:
        return OfficialIrReleaseAdapter(source_format="rss",
            fetch_bytes=fetcher,
            clock=lambda: FIXED_NOW,
            cache_ttl_seconds=300,
        )

    def test_fixed_official_feeds_filter_external_links_and_future_items(self) -> None:
        fetcher = FakeIrFetcher()
        payload = self.make_adapter(fetcher).recent_releases_batch(STORAGE_SYMBOLS)

        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["rows"]), 4)
        self.assertEqual(len(fetcher.calls), 4)
        first = payload["rows"][0]["releases"]
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["title"], "Official storage update")
        self.assertTrue(first[0]["official_url"].startswith("https://"))
        self.assertEqual(first[0]["summary"], "Primary company statement.")
        self.assertEqual(first[0]["source_tier"], "primary")
        self.assertFalse(payload["live_trading_allowed"])

    def test_four_feeds_run_concurrently_and_preserve_requested_order(self) -> None:
        barrier = threading.Barrier(4)
        fetcher = FakeIrFetcher()

        def concurrent_fetch(url: str, allowed_hosts: set[str]) -> bytes:
            barrier.wait(timeout=2)
            return fetcher(url, allowed_hosts)

        adapter = OfficialIrReleaseAdapter(source_format="rss",
            fetch_bytes=concurrent_fetch,
            clock=lambda: FIXED_NOW,
        )
        payload = adapter.recent_releases_batch(STORAGE_SYMBOLS)

        self.assertEqual([row["symbol"] for row in payload["rows"]], list(STORAGE_SYMBOLS))
        self.assertEqual(len(fetcher.calls), 4)
        self.assertEqual(payload["source_errors"], [])

    def test_concurrent_forced_requests_share_one_feed_fetch(self) -> None:
        fetcher = FakeIrFetcher()
        fetch_started = threading.Event()
        release_fetch = threading.Event()

        def blocked_fetch(url: str, allowed_hosts: set[str]) -> bytes:
            fetch_started.set()
            self.assertTrue(release_fetch.wait(timeout=2))
            return fetcher(url, allowed_hosts)

        adapter = OfficialIrReleaseAdapter(source_format="rss",
            fetch_bytes=blocked_fetch,
            clock=lambda: FIXED_NOW,
        )
        second_entered = threading.Event()

        def second_request() -> dict[str, object]:
            second_entered.set()
            return adapter.recent_releases_batch(["US.MU"], force=True)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(adapter.recent_releases_batch, ["US.MU"], force=True)
            self.assertTrue(fetch_started.wait(timeout=2))
            second_future = executor.submit(second_request)
            self.assertTrue(second_entered.wait(timeout=2))
            time.sleep(0.03)
            release_fetch.set()
            results = [first_future.result(), second_future.result()]

        self.assertEqual(len(fetcher.calls), 1)
        self.assertEqual(
            sorted(result["rows"][0]["singleflight_shared"] for result in results),
            [False, True],
        )

    def test_one_company_failure_is_isolated_and_cache_avoids_refetch(self) -> None:
        fetcher = FakeIrFetcher(fail_symbol_host="investor.wdc.com")
        adapter = self.make_adapter(fetcher)

        first = adapter.recent_releases_batch(STORAGE_SYMBOLS)
        first_call_count = len(fetcher.calls)
        second = adapter.recent_releases_batch(STORAGE_SYMBOLS)

        self.assertTrue(first["ok"])
        self.assertEqual(len(first["rows"]), 3)
        self.assertEqual(first["source_errors"][0]["symbol"], "US.WDC")
        self.assertEqual(len(fetcher.calls), first_call_count)
        self.assertEqual(len(second["rows"]), 3)
        self.assertTrue(all(row["cache_hit"] for row in second["rows"]))
        self.assertTrue(second["source_errors"][0]["cache_hit"])

        adapter.recent_releases_batch(["US.WDC"], force=True)
        self.assertEqual(len(fetcher.calls), first_call_count + 1)

    def test_failed_feed_negative_cache_expires_and_empty_feed_is_cached(self) -> None:
        monotonic_clock = [10.0]
        failing_fetcher = FakeIrFetcher(fail_symbol_host="investor.wdc.com")
        failing_adapter = OfficialIrReleaseAdapter(source_format="rss",
            fetch_bytes=failing_fetcher,
            clock=lambda: FIXED_NOW,
            monotonic=lambda: monotonic_clock[0],
            cache_ttl_seconds=300,
        )

        failing_adapter.recent_releases_batch(["US.WDC"])
        failing_adapter.recent_releases_batch(["US.WDC"])
        self.assertEqual(len(failing_fetcher.calls), 1)
        monotonic_clock[0] += 61.0
        failing_adapter.recent_releases_batch(["US.WDC"])
        self.assertEqual(len(failing_fetcher.calls), 2)

        empty_calls: list[str] = []
        empty_adapter = OfficialIrReleaseAdapter(source_format="rss",
            fetch_bytes=lambda url, _hosts: empty_calls.append(url) or b"<rss><channel /></rss>",
            clock=lambda: FIXED_NOW,
        )
        first_empty = empty_adapter.recent_releases_batch(["US.MU"])
        second_empty = empty_adapter.recent_releases_batch(["US.MU"])

        self.assertEqual(first_empty["rows"], [])
        self.assertEqual(second_empty["rows"], [])
        self.assertEqual(len(empty_calls), 1)
        self.assertEqual(second_empty["source_errors"][0]["code"], "IR_RELEASES_EMPTY")
        self.assertTrue(second_empty["source_errors"][0]["cache_hit"])

    def test_symbol_whitelist_and_non_official_url_are_rejected(self) -> None:
        fetcher = FakeIrFetcher()
        adapter = self.make_adapter(fetcher)
        with self.assertRaisesRegex(ValueError, "IR 源白名单"):
            adapter.recent_releases_batch(["US.AAPL"])
        with self.assertRaisesRegex(ValueError, "非固定 HTTPS"):
            OfficialIrReleaseAdapter._default_fetch_bytes("https://example.com/rss.xml", {"official.example"})

    def test_default_fetcher_rejects_declared_and_streaming_oversize(self) -> None:
        class FakeResponse:
            def __init__(self, *, declared_length: str, body: bytes) -> None:
                self.headers = {"Content-Length": declared_length}
                self.body = body
                self.read_calls = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def read(self, _limit: int) -> bytes:
                self.read_calls += 1
                return self.body

        config = IR_FEEDS["US.MU"]
        for response, expected_reads in (
            (FakeResponse(declared_length="9", body=b""), 0),
            (FakeResponse(declared_length="", body=b"123456789"), 1),
        ):
            with (
                self.subTest(expected_reads=expected_reads),
                patch.object(ir_releases_module, "IR_MAX_RESPONSE_BYTES", 8),
                patch.object(
                    ir_releases_module,
                    "open_official_https",
                    return_value=response,
                ),
                self.assertRaisesRegex(ValueError, "1 MB"),
            ):
                OfficialIrReleaseAdapter._default_fetch_bytes(
                    str(config["url"]),
                    set(config["hosts"]),
                )
            self.assertEqual(response.read_calls, expected_reads)

    def test_sec_association_is_a_candidate_not_silent_deduplication(self) -> None:
        releases = {
            "rows": [{
                "symbol": "US.MU",
                "releases": [{
                    "title": "Earnings",
                    "published_date": "2026-07-18",
                    "official_url": "https://investors.micron.com/news/earnings",
                }],
            }],
        }
        filings = {
            "rows": [{
                "symbol": "US.MU",
                "filings": [{
                    "accession_number": "0001",
                    "form": "8-K",
                    "filing_date": "2026-07-19",
                    "official_url": "https://www.sec.gov/Archives/edgar/data/1/filing.htm",
                }],
            }],
        }

        linked = StorageResearchMarketService._associate_ir_with_sec(releases, filings)

        self.assertEqual(len(linked["rows"][0]["releases"]), 1)
        self.assertEqual(
            linked["rows"][0]["releases"][0]["possible_sec_matches"][0]["accession_number"],
            "0001",
        )
        self.assertNotIn("possible_sec_matches", releases["rows"][0]["releases"][0])

    def test_earnings_events_and_fiscal_periods_are_normalized_without_guessing(self) -> None:
        self.assertEqual(
            OfficialIrReleaseAdapter._classify_event(
                "Seagate Technology Reports Fiscal Third Quarter 2026 Financial Results"
            ),
            "earnings_release",
        )
        self.assertEqual(
            OfficialIrReleaseAdapter._classify_event(
                "Seagate Technology to Report Fiscal Fourth Quarter 2026 Financial Results"
            ),
            "earnings_schedule",
        )
        self.assertEqual(
            OfficialIrReleaseAdapter._fiscal_period("Q3FY26 Earnings Presentation")["fiscal_period"],
            "FY2026-Q3",
        )
        fourth = OfficialIrReleaseAdapter._fiscal_period(
            "Reports Fiscal Fourth Quarter and Fiscal Year 2025 Financial Results"
        )
        self.assertEqual(fourth["fiscal_period"], "FY2025-Q4")
        self.assertTrue(fourth["includes_full_year"])
        self.assertEqual(
            OfficialIrReleaseAdapter._fiscal_period("Official storage update")["period_confidence"],
            "unknown",
        )

    def test_earnings_pack_keeps_company_claim_sec_candidates_and_split_boundary(self) -> None:
        releases = {
            "captured_at": "2026-05-01T00:00:00Z",
            "rows": [{
                "symbol": "US.SNDK",
                "publisher": "Sandisk Corporation Investor Relations",
                "presentation_hub_url": "https://investor.sandisk.com/news-events/presentations",
                "technology_scope": ["NAND"],
                "releases": [{
                    "title": "Sandisk Reports Fiscal Third Quarter 2026 Financial Results",
                    "published_at": "2026-04-30T20:00:00Z",
                    "published_date": "2026-04-30",
                    "official_url": "https://investor.sandisk.com/news/q3-2026",
                    "event_type": "earnings_release",
                    "fiscal_period": "FY2026-Q3",
                    "fiscal_year": 2026,
                    "fiscal_quarter": 3,
                    "period_confidence": "title_derived",
                    "technology_scope": ["NAND"],
                    "claim_status": "company_statement",
                    "possible_sec_matches": [{"form": "8-K", "filing_date": "2026-04-30"}],
                }, {
                    "title": "Sandisk to Report Fiscal Fourth Quarter 2026 Financial Results",
                    "official_url": "https://investor.sandisk.com/news/q4-schedule",
                    "event_type": "earnings_schedule",
                }],
            }],
            "source_errors": [],
        }

        materials = {
            "rows": [{
                "symbol": "US.SNDK",
                "materials": [{
                    "fiscal_period": "FY2026-Q3",
                    "material_kind": "earnings_presentation",
                    "official_url": "https://investor.sandisk.com/static-files/q3-deck",
                }],
            }],
            "source_errors": [],
        }
        payload = StorageResearchMarketService._build_official_earnings_packs(releases, materials)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["pack_count"], 1)
        pack = payload["rows"][0]["packs"][0]
        self.assertEqual(pack["fiscal_period"], "FY2026-Q3")
        self.assertTrue(pack["company_claim"])
        self.assertEqual(pack["technology_scope"], ["NAND"])
        self.assertEqual(pack["possible_sec_matches"][0]["form"], "8-K")
        self.assertEqual(pack["presentation_discovery_status"], "direct_official")
        self.assertEqual(pack["presentation_url"], "https://investor.sandisk.com/static-files/q3-deck")
        self.assertEqual(pack["metric_count"], 4)
        self.assertEqual(
            {metric["fact_or_guidance"] for metric in pack["metrics"]},
            {"historical_fact", "company_guidance"},
        )
        self.assertIn("2025-02-21", pack["comparability_notes"][0])
        self.assertEqual(pack["execution_capability"], "none")
        self.assertFalse(pack["live_trading_allowed"])

    def test_earnings_pack_reports_missing_symbols_when_one_ir_source_fails(self) -> None:
        releases = {
            "symbols": ["US.MU", "US.SNDK"],
            "rows": [{
                "symbol": "US.MU",
                "releases": [{
                    "title": "Micron Reports Fiscal Third Quarter 2026 Financial Results",
                    "published_at": "2026-06-24T20:00:00Z",
                    "official_url": "https://investors.micron.com/news/q3",
                    "event_type": "earnings_release",
                    "fiscal_period": "FY2026-Q3",
                }],
            }],
            "source_errors": [{"symbol": "US.SNDK", "code": "IR_FEED_ERROR"}],
        }

        payload = StorageResearchMarketService._build_official_earnings_packs(releases)

        self.assertEqual(payload["state"], "partial")
        self.assertEqual(payload["missing_symbols"], ["US.SNDK"])
        self.assertEqual(len(payload["rows"]), 2)

    def test_earnings_pack_preserves_material_warnings_and_transcript_links(self) -> None:
        releases = {
            "captured_at": "2026-08-02T00:00:00Z",
            "rows": [{
                "symbol": "US.STX",
                "releases": [{
                    "title": "Seagate Reports Fiscal Fourth Quarter 2026 Results",
                    "published_at": "2026-07-28T20:00:00Z",
                    "official_url": "https://investors.seagate.com/news/q4",
                    "event_type": "earnings_release",
                    "fiscal_period": "FY2026-Q4",
                }],
            }],
            "source_errors": [],
        }
        warning = {
            "source": "official_company_ir_materials",
            "symbol": "US.STX",
            "code": "EARNINGS_MATERIAL_HUB_ANTIBOT",
            "severity": "warning",
        }
        materials = {
            "rows": [{
                "symbol": "US.STX",
                "materials": [{
                    "fiscal_period": "FY2026-Q4",
                    "material_kind": "earnings_release",
                    "official_url": "https://s24.q4cdn.com/release.pdf",
                }, {
                    "fiscal_period": "FY2026-Q4",
                    "material_kind": "corrected_transcript",
                    "official_url": "https://s24.q4cdn.com/transcript.pdf",
                }],
            }],
            "source_errors": [],
            "source_warnings": [warning],
        }

        payload = StorageResearchMarketService._build_official_earnings_packs(releases, materials)
        pack = payload["rows"][0]["packs"][0]

        self.assertEqual(payload["state"], "ready")
        self.assertEqual(payload["source_warnings"], [warning])
        self.assertEqual(pack["earnings_release_material_url"], "https://s24.q4cdn.com/release.pdf")
        self.assertEqual(pack["transcript_url"], "https://s24.q4cdn.com/transcript.pdf")
        self.assertEqual(pack["presentation_discovery_status"], "direct_official")


if __name__ == "__main__":
    unittest.main()
