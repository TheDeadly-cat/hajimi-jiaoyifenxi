from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.market.futu_readonly import STORAGE_SYMBOLS
from backend.market.ir_releases import IR_FEEDS, OfficialIrReleaseAdapter
from backend.market.sec_edgar import (
    SEC_MONITOR_SYMBOLS,
    SEC_TICKERS_URL,
    SecEdgarAdapter,
)
from backend.source_monitoring.adapters.base import validate_source_adapter
from backend.source_monitoring.adapters.company_ir import (
    COMPANY_IR_CHECKPOINT_VERSION,
    MAX_IR_PROJECTIONS,
    CompanyIrSourceAdapter,
)
from backend.source_monitoring.adapters.macro_official import (
    BlsReleaseSourceAdapter,
    FederalReserveSourceAdapter,
    OfficialMacroCalendarSourceAdapter,
    TreasuryReleaseSourceAdapter,
)
from backend.source_monitoring.adapters.sec_filings import (
    MAX_SEEN_ACCESSIONS,
    SEC_FILINGS_CHECKPOINT_VERSION,
    SecFilingsSourceAdapter,
)
from backend.source_monitoring.default_registry import build_official_source_registry
from backend.source_monitoring.packet_builder import build_packet_from_poll_result
from backend.source_monitoring.registry import SourceAdapterRegistry
from backend.source_monitoring.settings import SourceMonitoringSettings
from backend.source_monitoring.state_repository import SourceMonitoringStateRepository
from backend.source_monitoring.supervisor import SourceMonitoringSupervisor
from backend.source_inbox_service import SourceInboxService
from backend.store import StudioStore


FIXED_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
FIXED_NOW_MS = int(FIXED_NOW.timestamp() * 1_000)


class SecFixtureFetcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, url: str, _user_agent: str) -> dict:
        self.calls.append(url)
        if url == SEC_TICKERS_URL:
            return {
                "0": {
                    "cik_str": 1_045_810,
                    "ticker": "NVDA",
                    "title": "NVIDIA Corporation",
                }
            }
        return {
            "cik": "0001045810",
            "name": "NVIDIA Corporation",
            "filings": {
                "recent": {
                    "accessionNumber": [
                        "0001045810-26-000001",
                        "not-an-accession",
                        "0001045810-26-000003",
                    ],
                    "form": ["8-K", "8-K", "8-K"],
                    "filingDate": ["2026-08-30", "2026-08-30", "2026-08-30"],
                    "reportDate": ["2026-08-30", "2026-08-30", "2026-08-30"],
                    "acceptanceDateTime": [
                        "2026-08-30T20:00:00Z",
                        "2026-08-30T21:00:00Z",
                        "2026-09-01T00:00:00Z",
                    ],
                    "primaryDocument": ["valid.htm", "invalid.htm", "future.htm"],
                    "primaryDocDescription": [
                        "Current report",
                        "Invalid identity",
                        "Future acceptance",
                    ],
                    "items": ["2.02,9.01", "", ""],
                }
            },
        }


class UnsafeSecPathFetcher(SecFixtureFetcher):
    def __call__(self, url: str, user_agent: str) -> dict:
        if url == SEC_TICKERS_URL:
            return super().__call__(url, user_agent)
        documents = [
            "valid.htm",
            ".",
            "..",
            "bad?query.htm",
            "bad#fragment.htm",
            "bad%2fencoded.htm",
            "bad\x01control.htm",
            "nested/file.htm",
            "nested\\file.htm",
            "valid-mismatched-cik.htm",
        ]
        accessions = [
            "0001045810-26-000010",
            "0001045810-26-000011",
            "0001045810-26-000012",
            "0001045810-26-000013",
            "0001045810-26-000014",
            "0001045810-26-000015",
            "0001045810-26-000016",
            "0001045810-26-000017",
            "0001045810-26-000018",
            "0000000001-26-000019",
        ]
        return {
            "cik": "0001045810",
            "name": "NVIDIA Corporation",
            "filings": {
                "recent": {
                    "accessionNumber": accessions,
                    "form": ["8-K"] * len(accessions),
                    "filingDate": ["2026-08-30"] * len(accessions),
                    "reportDate": ["2026-08-30"] * len(accessions),
                    "acceptanceDateTime": [
                        "2026-08-30T20:00:00Z"
                    ] * len(accessions),
                    "primaryDocument": documents,
                    "primaryDocDescription": ["Current report"] * len(accessions),
                    "items": ["2.02"] * len(accessions),
                }
            },
        }


class WhitespaceAcceptedAtSecFixtureFetcher(SecFixtureFetcher):
    def __call__(self, url: str, user_agent: str) -> dict:
        payload = super().__call__(url, user_agent)
        if url != SEC_TICKERS_URL:
            payload["filings"]["recent"]["acceptanceDateTime"][0] = "   "
        return payload


class ManySecFixtureFetcher(SecFixtureFetcher):
    def __init__(self, *, start_index: int = 1, count: int = 7) -> None:
        super().__init__()
        self.start_index = start_index
        self.count = count

    def __call__(self, url: str, user_agent: str) -> dict:
        if url == SEC_TICKERS_URL:
            return super().__call__(url, user_agent)
        indexes = range(self.start_index, self.start_index + self.count)
        return {
            "cik": "0001045810",
            "name": "NVIDIA Corporation",
            "filings": {
                "recent": {
                    "accessionNumber": [
                        f"0001045810-26-{index:06d}"
                        for index in indexes
                    ],
                    "form": ["8-K"] * self.count,
                    "filingDate": ["2026-08-30"] * self.count,
                    "reportDate": ["2026-08-30"] * self.count,
                    "acceptanceDateTime": [
                        "2026-08-30T20:00:00Z"
                    ] * self.count,
                    "primaryDocument": [
                        f"filing-{index}.htm"
                        for index in range(
                            self.start_index,
                            self.start_index + self.count,
                        )
                    ],
                    "primaryDocDescription": ["Current report"] * self.count,
                    "items": ["2.02"] * self.count,
                }
            },
        }


class MutableIrFixtureFetcher:
    def __init__(self, *, guid: str = "mu-release-guid-1") -> None:
        self.guid = guid
        self.summary = "Initial official RSS summary."
        self.calls = 0

    def __call__(self, _url: str, _allowed_hosts: set[str]) -> bytes:
        self.calls += 1
        guid_xml = f"<guid>{self.guid}</guid>" if self.guid else ""
        second_item = "" if self.guid else """
          <item>
            <title>Second release without GUID</title>
            <link>https://investors.micron.com/news/second-release</link>
            <pubDate>Sun, 30 Aug 2026 19:00:00 +0000</pubDate>
            <description>Second URL identity projection.</description>
          </item>"""
        return f"""<?xml version="1.0" encoding="utf-8"?>
        <rss version="2.0"><channel>
          <item>
            <title>Micron reports financial results for fourth quarter 2026</title>
            {guid_xml}
            <link>https://investors.micron.com/news/official-update</link>
            <pubDate>Sun, 30 Aug 2026 20:00:00 +0000</pubDate>
            <description>{self.summary}</description>
          </item>
          {second_item}
        </channel></rss>""".encode("utf-8")


class ConflictingGuidIrFixtureFetcher:
    def __init__(self) -> None:
        self.reverse = False

    def __call__(self, _url: str, _allowed_hosts: set[str]) -> bytes:
        first = """<item>
            <title>Micron reports financial results for fourth quarter 2026</title>
            <guid>conflicting-guid-1</guid>
            <link>https://investors.micron.com/news/official-update</link>
            <pubDate>Sun, 30 Aug 2026 20:00:00 +0000</pubDate>
            <description>First projection for one GUID.</description>
          </item>"""
        second = """<item>
            <title>Micron reports financial results for fourth quarter 2026</title>
            <guid>conflicting-guid-1</guid>
            <link>https://investors.micron.com/news/official-update</link>
            <pubDate>Sun, 30 Aug 2026 20:00:00 +0000</pubDate>
            <description>Second projection for one GUID.</description>
          </item>"""
        items = (second, first) if self.reverse else (first, second)
        return (
            "<?xml version=\"1.0\" encoding=\"utf-8\"?>"
            "<rss version=\"2.0\"><channel>"
            + "".join(items)
            + "</channel></rss>"
        ).encode("utf-8")


class ManyIrFixtureFetcher:
    def __init__(self, *, count: int = 9) -> None:
        self.count = count

    def __call__(self, _url: str, _allowed_hosts: set[str]) -> bytes:
        items = "".join(
            f"""<item>
              <title>Micron official release {index}</title>
              <guid>many-guid-{index}</guid>
              <link>https://investors.micron.com/news/release-{index}</link>
              <pubDate>Sun, 30 Aug 2026 20:00:00 +0000</pubDate>
              <description>Official RSS projection {index}.</description>
            </item>"""
            for index in range(1, self.count + 1)
        )
        return (
            "<?xml version=\"1.0\" encoding=\"utf-8\"?>"
            f"<rss version=\"2.0\"><channel>{items}</channel></rss>"
        ).encode("utf-8")


class SecMonitoringAdapterTests(unittest.TestCase):
    def make_sec_adapter(self, fetcher: SecFixtureFetcher) -> SecEdgarAdapter:
        return SecEdgarAdapter(
            user_agent="AI Studio monitor@example.com",
            fetch_json=fetcher,
            clock=lambda: FIXED_NOW,
            allowed_symbols=SEC_MONITOR_SYMBOLS,
        )

    def test_default_monitor_allowlist_does_not_expand_storage_symbols(self) -> None:
        fetcher = SecFixtureFetcher()
        adapter = self.make_sec_adapter(fetcher)

        self.assertEqual(
            STORAGE_SYMBOLS,
            ("US.MU", "US.SNDK", "US.WDC", "US.STX"),
        )
        self.assertEqual(SecEdgarAdapter().allowed_symbols, STORAGE_SYMBOLS)
        self.assertEqual(adapter.allowed_symbols, SEC_MONITOR_SYMBOLS)
        self.assertEqual(
            SEC_MONITOR_SYMBOLS,
            (
                "US.MU",
                "US.SNDK",
                "US.WDC",
                "US.STX",
                "US.NVDA",
                "US.MRVL",
                "US.AMD",
            ),
        )

        payload = adapter.recent_filings_batch(["US.NVDA"], forms=["8-K"])

        self.assertTrue(payload["ok"])
        filings = payload["rows"][0]["filings"]
        self.assertEqual(
            [filing["accession_number"] for filing in filings],
            ["0001045810-26-000001"],
        )
        self.assertLessEqual(
            datetime.fromisoformat(filings[0]["accepted_at"].replace("Z", "+00:00")),
            FIXED_NOW,
        )

        restricted = SecEdgarAdapter(
            user_agent="AI Studio monitor@example.com",
            fetch_json=fetcher,
            clock=lambda: FIXED_NOW,
            allowed_symbols=["US.MU"],
        )
        with self.assertRaisesRegex(ValueError, "SEC 适配器白名单"):
            restricted.recent_filings_batch(["US.NVDA"])

    def test_sec_projection_has_empty_content_hash_and_checkpoint_dedupe(self) -> None:
        fetcher = SecFixtureFetcher()
        monitor = SecFilingsSourceAdapter(
            adapter=self.make_sec_adapter(fetcher),
            allowed_symbols=["US.NVDA"],
            allowed_forms=["8-K"],
        )
        metadata = validate_source_adapter(monitor)
        self.assertTrue(metadata.official_source)
        self.assertEqual(metadata.execution_capability, "none")
        self.assertFalse(metadata.live_trading_allowed)
        self.assertEqual(metadata.poll_interval_ms, 300_000)
        self.assertRegex(metadata.config_version, r"\Asec_filings_config_v2_[0-9a-f]{16}\Z")

        first = monitor.poll(
            {},
            observed_at_ms=FIXED_NOW_MS,
            etag='"sec-fixture"',
            last_modified="Sun, 30 Aug 2026 20:00:00 GMT",
            max_items=monitor.max_candidates_per_poll,
        )
        self.assertEqual(len(first.observed_items), 1)
        self.assertEqual(first.etag, '"sec-fixture"')
        self.assertEqual(first.last_modified, "Sun, 30 Aug 2026 20:00:00 GMT")
        item = first.observed_items[0]
        self.assertEqual(item["external_item_id"], "0001045810-26-000001")
        self.assertEqual(item["sources"][0]["content_sha256"], "")
        self.assertEqual(item["extensions"]["sec_v1"]["items"], ["2.02", "9.01"])
        self.assertEqual(item["extensions"]["sec_v1"]["symbol"], "US.NVDA")
        self.assertEqual(
            item["extensions"]["sec_v1"]["discovered_at_ms"],
            int(
                datetime(2026, 8, 30, 20, 0, tzinfo=timezone.utc).timestamp()
                * 1_000
            ),
        )
        self.assertNotIn("execution_capability", item)
        self.assertNotIn("live_trading_allowed", item)
        packet = build_packet_from_poll_result(
            first,
            external_run_id="sec-fixture-run-1",
        )
        self.assertEqual(packet["generation"]["model"], "")

        delayed_crash_replay = monitor.poll(
            {},
            observed_at_ms=FIXED_NOW_MS + 60_000,
        )
        self.assertEqual(delayed_crash_replay.observed_items, first.observed_items)

        second = monitor.poll(
            first.next_checkpoint,
            observed_at_ms=FIXED_NOW_MS,
        )
        self.assertEqual(second.observed_items, ())
        self.assertEqual(second.duplicate_count, 1)
        self.assertEqual(second.next_checkpoint, first.next_checkpoint)

    def test_sec_whitespace_acceptance_uses_honest_date_only_anchor(self) -> None:
        monitor = SecFilingsSourceAdapter(
            adapter=self.make_sec_adapter(WhitespaceAcceptedAtSecFixtureFetcher()),
            allowed_symbols=["US.NVDA"],
            allowed_forms=["8-K"],
        )
        result = monitor.poll(
            {},
            observed_at_ms=FIXED_NOW_MS,
            max_items=50,
        )
        self.assertEqual(len(result.observed_items), 1)
        item = result.observed_items[0]
        self.assertEqual(item["occurred_at"], "2026-08-30T00:00:00Z")
        self.assertEqual(item["extensions"]["sec_v1"]["accepted_at"], "")
        self.assertEqual(
            item["extensions"]["sec_v1"]["discovered_at_ms"],
            int(
                datetime(2026, 8, 30, 0, 0, tzinfo=timezone.utc).timestamp()
                * 1_000
            ),
        )

    def test_sec_unseen_seventh_filing_drains_after_six_duplicates(self) -> None:
        monitor = SecFilingsSourceAdapter(
            adapter=self.make_sec_adapter(ManySecFixtureFetcher()),
            allowed_symbols=["US.NVDA"],
            allowed_forms=["8-K"],
            per_symbol_limit=6,
        )

        first = monitor.poll(
            {},
            observed_at_ms=FIXED_NOW_MS,
            max_items=monitor.max_candidates_per_poll,
        )
        second = monitor.poll(
            first.next_checkpoint,
            observed_at_ms=FIXED_NOW_MS,
            max_items=monitor.max_candidates_per_poll,
        )

        self.assertEqual(len(first.observed_items), 6)
        self.assertEqual(len(second.observed_items), 1)
        self.assertEqual(
            second.observed_items[0]["external_item_id"],
            "0001045810-26-000007",
        )
        self.assertEqual(second.duplicate_count, 6)

    def test_sec_capacity_excess_fails_closed_without_checkpoint_advance(self) -> None:
        monitor = SecFilingsSourceAdapter(
            adapter=self.make_sec_adapter(ManySecFixtureFetcher(count=1001)),
            allowed_symbols=["US.NVDA"],
            allowed_forms=["8-K"],
            per_symbol_limit=6,
        )

        result = monitor.poll(
            {},
            observed_at_ms=FIXED_NOW_MS,
            max_items=monitor.max_candidates_per_poll,
        )

        self.assertEqual(result.observed_items, ())
        self.assertEqual(result.next_checkpoint, {})
        self.assertEqual(result.rejected_count, 1001)
        self.assertEqual(
            result.source_errors[-1].code,
            "SEC_CHECKPOINT_CAPACITY_EXCEEDED",
        )

    def test_sec_full_checkpoint_drops_stale_before_adding_new_identity(self) -> None:
        checkpoint = {
            "version": SEC_FILINGS_CHECKPOINT_VERSION,
            "seen_accessions": [
                f"0001045810-26-{index:06d}"
                for index in range(1, MAX_SEEN_ACCESSIONS + 1)
            ],
        }
        monitor = SecFilingsSourceAdapter(
            adapter=self.make_sec_adapter(ManySecFixtureFetcher(
                start_index=2,
                count=MAX_SEEN_ACCESSIONS,
            )),
            allowed_symbols=["US.NVDA"],
            allowed_forms=["8-K"],
            per_symbol_limit=6,
        )

        first = monitor.poll(
            checkpoint,
            observed_at_ms=FIXED_NOW_MS,
            max_items=monitor.max_candidates_per_poll,
        )
        second = monitor.poll(
            first.next_checkpoint,
            observed_at_ms=FIXED_NOW_MS,
            max_items=monitor.max_candidates_per_poll,
        )

        self.assertEqual(len(first.observed_items), 1)
        self.assertEqual(
            first.observed_items[0]["external_item_id"],
            "0001045810-26-001001",
        )
        self.assertEqual(len(first.next_checkpoint["seen_accessions"]), 1000)
        self.assertNotIn(
            "0001045810-26-000001",
            first.next_checkpoint["seen_accessions"],
        )
        self.assertIn(
            "0001045810-26-001001",
            first.next_checkpoint["seen_accessions"],
        )
        self.assertEqual(second.observed_items, ())
        self.assertEqual(second.duplicate_count, 1000)
        self.assertEqual(second.next_checkpoint, first.next_checkpoint)

    def test_sec_archive_path_is_bound_but_filer_agent_accession_is_allowed(self) -> None:
        source = self.make_sec_adapter(UnsafeSecPathFetcher())
        payload = source.recent_filings_batch(["US.NVDA"], forms=["8-K"])
        self.assertEqual(
            [
                filing["accession_number"]
                for filing in payload["rows"][0]["filings"]
            ],
            ["0001045810-26-000010", "0000000001-26-000019"],
        )

        class ForgedArchiveAdapter:
            def recent_filings_batch(self, *args, **kwargs):
                forged = source.recent_filings_batch(*args, **kwargs)
                forged["rows"][0]["filings"] = [forged["rows"][0]["filings"][0]]
                forged["rows"][0]["filings"][0]["official_url"] += "?download=1"
                return forged

        monitor = SecFilingsSourceAdapter(
            adapter=ForgedArchiveAdapter(),
            allowed_symbols=["US.NVDA"],
            allowed_forms=["8-K"],
        )
        result = monitor.poll({}, observed_at_ms=FIXED_NOW_MS)
        self.assertEqual(result.observed_items, ())
        self.assertEqual(result.rejected_count, 1)

        class ConflictingAccessionAdapter:
            def recent_filings_batch(self, *_args, **_kwargs):
                base = {
                    "accession_number": "0001045810-26-000020",
                    "form": "8-K",
                    "filing_date": "2026-08-30",
                    "accepted_at": "2026-08-30T20:00:00Z",
                    "description": "Current report",
                    "items": "2.02",
                }
                return {
                    "rows": [{
                        "symbol": "US.NVDA",
                        "cik": "0001045810",
                        "company_name": "NVIDIA Corporation",
                        "filings": [
                            {
                                **base,
                                "primary_document": "first.htm",
                                "official_url": (
                                    "https://www.sec.gov/Archives/edgar/data/1045810/"
                                    "000104581026000020/first.htm"
                                ),
                            },
                            {
                                **base,
                                "primary_document": "second.htm",
                                "official_url": (
                                    "https://www.sec.gov/Archives/edgar/data/1045810/"
                                    "000104581026000020/second.htm"
                                ),
                            },
                        ],
                    }],
                    "source_errors": [],
                }

        conflicting = SecFilingsSourceAdapter(
            adapter=ConflictingAccessionAdapter(),
            allowed_symbols=["US.NVDA"],
            allowed_forms=["8-K"],
        ).poll({}, observed_at_ms=FIXED_NOW_MS)
        self.assertEqual(conflicting.observed_items, ())
        self.assertEqual(conflicting.rejected_count, 2)

    def test_sec_config_is_strict_and_versioned(self) -> None:
        fetcher = SecFixtureFetcher()
        source = self.make_sec_adapter(fetcher)
        baseline = SecFilingsSourceAdapter(
            adapter=source,
            allowed_symbols=["US.NVDA"],
            allowed_forms=["8-K"],
        )
        equivalent = SecFilingsSourceAdapter(
            adapter=self.make_sec_adapter(SecFixtureFetcher()),
            allowed_symbols=("US.NVDA",),
            allowed_forms=("8-K",),
        )
        changed = SecFilingsSourceAdapter(
            adapter=source,
            allowed_symbols=["US.NVDA"],
            allowed_forms=["8-K", "10-Q"],
        )
        self.assertEqual(baseline.config_version, equivalent.config_version)
        self.assertNotEqual(baseline.config_version, changed.config_version)
        with self.assertRaises(AttributeError):
            baseline.allowed_symbols = ("US.AMD",)
        for kwargs in (
            {"allowed_symbols": "US.NVDA"},
            {"allowed_symbols": ["us.nvda"]},
            {"allowed_symbols": ["US.NVDA", "US.NVDA"]},
            {"allowed_forms": ["8-k"]},
            {"per_symbol_limit": True},
            {"per_symbol_limit": 8},
            {"force": 1},
            {"poll_interval_ms": True},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                SecFilingsSourceAdapter(adapter=source, **kwargs)
        for context in (
            {"etag": 1},
            {"last_modified": "bad\nheader"},
            {"max_items": True},
            {"max_items": 0},
        ):
            with self.subTest(context=context), self.assertRaises(ValueError):
                baseline.poll({}, observed_at_ms=FIXED_NOW_MS, **context)
        tampered = SecFilingsSourceAdapter(
            adapter=source,
            allowed_symbols=["US.NVDA"],
            allowed_forms=["8-K"],
        )
        object.__setattr__(tampered, "_force", True)
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            tampered.poll({}, observed_at_ms=FIXED_NOW_MS)


class CompanyIrMonitoringAdapterTests(unittest.TestCase):
    def make_monitor(
        self,
        fetcher: MutableIrFixtureFetcher,
    ) -> CompanyIrSourceAdapter:
        adapter = OfficialIrReleaseAdapter(
            fetch_bytes=fetcher,
            clock=lambda: FIXED_NOW,
        )
        return CompanyIrSourceAdapter(
            adapter=adapter,
            symbols=["US.MU"],
            force=True,
        )

    def test_guid_identity_and_rss_projection_revision_are_stable(self) -> None:
        fetcher = MutableIrFixtureFetcher()
        monitor = self.make_monitor(fetcher)
        metadata = validate_source_adapter(monitor)
        self.assertTrue(metadata.official_source)
        self.assertEqual(metadata.poll_interval_ms, 300_000)
        self.assertRegex(metadata.config_version, r"\Acompany_ir_config_v1_[0-9a-f]{16}\Z")
        self.assertEqual(tuple(IR_FEEDS), STORAGE_SYMBOLS)

        first = monitor.poll({}, observed_at_ms=FIXED_NOW_MS)
        self.assertEqual(len(first.observed_items), 1)
        first_item = first.observed_items[0]
        extension = first_item["extensions"]["company_ir_v1"]
        self.assertEqual(extension["event_type"], "earnings_release")
        self.assertEqual(extension["identity_kind"], "guid")
        self.assertEqual(extension["identity_value"], "mu-release-guid-1")
        self.assertFalse(extension["is_revision"])
        self.assertEqual(
            extension["rss_hash_semantics"],
            "normalized_rss_item_not_web_page_body",
        )
        self.assertEqual(first_item["sources"][0]["content_sha256"], "")
        self.assertEqual(
            first_item["sources"][1]["content_sha256"],
            extension["rss_projection_sha256"],
        )
        build_packet_from_poll_result(
            first,
            external_run_id="company-ir-fixture-run-1",
        )

        unchanged = monitor.poll(
            first.next_checkpoint,
            observed_at_ms=FIXED_NOW_MS,
        )
        self.assertEqual(unchanged.observed_items, ())
        self.assertEqual(unchanged.duplicate_count, 1)

        fetcher.summary = "Revised official RSS summary."
        revised = monitor.poll(
            unchanged.next_checkpoint,
            observed_at_ms=FIXED_NOW_MS,
        )
        self.assertEqual(len(revised.observed_items), 1)
        revised_item = revised.observed_items[0]
        revised_extension = revised_item["extensions"]["company_ir_v1"]
        self.assertEqual(revised_item["external_item_id"], first_item["external_item_id"])
        self.assertTrue(revised_extension["is_revision"])
        self.assertEqual(
            revised_extension["previous_rss_projection_sha256"],
            extension["rss_projection_sha256"],
        )
        self.assertNotEqual(
            revised_extension["rss_projection_sha256"],
            extension["rss_projection_sha256"],
        )

    def test_monitoring_path_preserves_items_hidden_by_legacy_dedupe(self) -> None:
        fetcher = ConflictingGuidIrFixtureFetcher()
        source = OfficialIrReleaseAdapter(
            fetch_bytes=fetcher,
            clock=lambda: FIXED_NOW,
        )

        legacy = source.recent_releases_batch(
            ["US.MU"],
            force=True,
        )
        monitoring = source.monitoring_releases_batch(
            ["US.MU"],
            force=True,
        )

        self.assertEqual(legacy["rows"][0]["release_count"], 1)
        self.assertEqual(monitoring["rows"][0]["release_count"], 2)
        self.assertEqual(
            {
                release["summary"]
                for release in monitoring["rows"][0]["releases"]
            },
            {
                "First projection for one GUID.",
                "Second projection for one GUID.",
            },
        )

    def test_missing_guid_falls_back_to_canonical_official_url_identity(self) -> None:
        fetcher = MutableIrFixtureFetcher(guid="")
        monitor = self.make_monitor(fetcher)

        result = monitor.poll({}, observed_at_ms=FIXED_NOW_MS)

        self.assertEqual(len(result.observed_items), 2)
        extension = result.observed_items[0]["extensions"]["company_ir_v1"]
        self.assertEqual(extension["identity_kind"], "url")
        self.assertEqual(
            extension["identity_value"],
            "https://investors.micron.com/news/official-update",
        )
        with self.assertRaisesRegex(ValueError, "fixed feed map"):
            CompanyIrSourceAdapter(adapter=monitor._adapter, symbols=["US.NVDA"])

    def test_ir_unseen_ninth_release_drains_after_eight_duplicates(self) -> None:
        monitor = CompanyIrSourceAdapter(
            adapter=OfficialIrReleaseAdapter(
                fetch_bytes=ManyIrFixtureFetcher(),
                clock=lambda: FIXED_NOW,
            ),
            symbols=["US.MU"],
            per_symbol_limit=8,
            force=True,
        )

        first = monitor.poll(
            {},
            observed_at_ms=FIXED_NOW_MS,
            max_items=monitor.max_candidates_per_poll,
        )
        second = monitor.poll(
            first.next_checkpoint,
            observed_at_ms=FIXED_NOW_MS,
            max_items=monitor.max_candidates_per_poll,
        )

        self.assertEqual(len(first.observed_items), 8)
        self.assertEqual(len(second.observed_items), 1)
        self.assertEqual(
            second.observed_items[0]["extensions"]["company_ir_v1"]["guid"],
            "many-guid-9",
        )
        self.assertEqual(second.duplicate_count, 8)

    def test_ir_capacity_excess_fails_closed_without_checkpoint_advance(self) -> None:
        monitor = CompanyIrSourceAdapter(
            adapter=OfficialIrReleaseAdapter(
                fetch_bytes=ManyIrFixtureFetcher(
                    count=MAX_IR_PROJECTIONS + 1,
                ),
                clock=lambda: FIXED_NOW,
            ),
            symbols=["US.MU"],
            per_symbol_limit=8,
            force=True,
        )

        result = monitor.poll(
            {"version": COMPANY_IR_CHECKPOINT_VERSION, "projections": []},
            observed_at_ms=FIXED_NOW_MS,
            max_items=monitor.max_candidates_per_poll,
        )

        self.assertEqual(result.observed_items, ())
        self.assertEqual(
            result.next_checkpoint,
            {"version": COMPANY_IR_CHECKPOINT_VERSION, "projections": []},
        )
        self.assertEqual(result.rejected_count, MAX_IR_PROJECTIONS + 1)
        self.assertEqual(
            result.source_errors[-1].code,
            "COMPANY_IR_CHECKPOINT_CAPACITY_EXCEEDED",
        )

        source = OfficialIrReleaseAdapter(
            fetch_bytes=ManyIrFixtureFetcher(count=MAX_IR_PROJECTIONS + 1),
            clock=lambda: FIXED_NOW,
        )

        class ErrorFloodIrAdapter:
            def recent_releases_batch(self, *args, **kwargs):
                return self.monitoring_releases_batch(*args, **kwargs)

            def monitoring_releases_batch(self, *args, **kwargs):
                payload = source.monitoring_releases_batch(*args, **kwargs)
                payload["source_errors"] = [
                    {
                        "code": f"IR_UPSTREAM_{index}",
                        "message": "fixture upstream error",
                        "symbol": "US.MU",
                    }
                    for index in range(50)
                ]
                return payload

        flooded = CompanyIrSourceAdapter(
            adapter=ErrorFloodIrAdapter(),
            symbols=["US.MU"],
            per_symbol_limit=8,
            force=True,
        ).poll(
            {},
            observed_at_ms=FIXED_NOW_MS,
            max_items=8,
        )
        self.assertEqual(len(flooded.source_errors), 50)
        self.assertEqual(
            flooded.source_errors[-1].code,
            "COMPANY_IR_CHECKPOINT_CAPACITY_EXCEEDED",
        )

    def test_ir_config_is_strict_versioned_and_low_capacity_fails_closed(self) -> None:
        fetcher = MutableIrFixtureFetcher(guid="")
        source = OfficialIrReleaseAdapter(
            fetch_bytes=fetcher,
            clock=lambda: FIXED_NOW,
        )
        baseline = CompanyIrSourceAdapter(
            adapter=source,
            symbols=["US.MU"],
            force=True,
        )
        equivalent = CompanyIrSourceAdapter(
            adapter=OfficialIrReleaseAdapter(
                fetch_bytes=MutableIrFixtureFetcher(guid=""),
                clock=lambda: FIXED_NOW,
            ),
            symbols=("US.MU",),
            force=True,
        )
        changed = CompanyIrSourceAdapter(
            adapter=source,
            symbols=["US.MU"],
            force=True,
            poll_interval_ms=600_000,
        )
        self.assertEqual(baseline.config_version, equivalent.config_version)
        self.assertNotEqual(baseline.config_version, changed.config_version)
        with self.assertRaises(AttributeError):
            baseline.symbols = ("US.STX",)
        with self.assertRaises(TypeError):
            IR_FEEDS["US.MU"]["url"] = "https://example.com/rss.xml"
        with self.assertRaisesRegex(ValueError, "below the sealed company IR"):
            baseline.poll({}, observed_at_ms=FIXED_NOW_MS, max_items=1)
        for kwargs in (
            {"symbols": "US.MU"},
            {"symbols": ["us.mu"]},
            {"symbols": ["US.MU", "US.MU"]},
            {"per_symbol_limit": False},
            {"force": 1},
            {"poll_interval_ms": 59_999},
            {"per_symbol_limit": 20},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                CompanyIrSourceAdapter(adapter=source, **kwargs)
        tampered = CompanyIrSourceAdapter(
            adapter=source,
            symbols=["US.MU"],
            force=True,
        )
        object.__setattr__(tampered, "_per_symbol_limit", 9)
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            tampered.poll({}, observed_at_ms=FIXED_NOW_MS)


class OfficialAdapterSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="ai-studio-official-monitor-")
        self.database_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.clock = [FIXED_NOW_MS]
        self.store = StudioStore(self.database_path)
        self.repository = SourceMonitoringStateRepository(
            self.store,
            clock_ms=lambda: self.clock[0],
        )
        self.inbox = SourceInboxService(
            self.store,
            clock=lambda: self.clock[0] / 1_000,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _forbidden_side_effect_counts(self) -> tuple[int, int, int, int]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            return (
                connection.execute(
                    "SELECT COUNT(*) FROM provider_execution_runs"
                ).fetchone()[0],
                connection.execute(
                    "SELECT COUNT(*) FROM provider_call_attempts"
                ).fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM rounds").fetchone()[0],
                connection.execute(
                    "SELECT COUNT(*) FROM source_inbox_round_drafts"
                ).fetchone()[0],
            )

    def _supervisor(self, adapter) -> SourceMonitoringSupervisor:
        sec = adapter if adapter.adapter_key == "sec_filings" else SecFilingsSourceAdapter(
                adapter=SecEdgarAdapter(
                    user_agent="AI Studio monitor@example.com",
                    fetch_json=SecFixtureFetcher(),
                    clock=lambda: FIXED_NOW,
                    allowed_symbols=SEC_MONITOR_SYMBOLS,
                ),
                allowed_symbols=["US.NVDA"],
                allowed_forms=["8-K"],
            )
        company_ir = adapter if adapter.adapter_key == "company_ir" else CompanyIrSourceAdapter(
                adapter=OfficialIrReleaseAdapter(
                    fetch_bytes=MutableIrFixtureFetcher(),
                    clock=lambda: FIXED_NOW,
                ),
                symbols=["US.MU"],
                force=True,
            )
        registry = SourceAdapterRegistry((sec, company_ir), official_only=True)
        self.assertIsInstance(registry, SourceAdapterRegistry)
        self.repository.set_enabled(
            adapter.adapter_key,
            config_version=adapter.config_version,
            enabled=True,
        )
        return SourceMonitoringSupervisor(
            registry=registry,
            repository=self.repository,
            source_inbox=self.inbox,
            settings=SourceMonitoringSettings(
                enabled=True,
                auto_start=False,
                official_only=True,
                dry_run=False,
                max_items_per_run=50,
            ),
            clock_ms=lambda: self.clock[0],
        )

    def test_production_official_registry_has_no_injection_surface(self) -> None:
        registry = build_official_source_registry()
        self.assertEqual(
            registry.adapter_keys,
            (
                "bls_releases",
                "company_ir",
                "federal_reserve",
                "official_macro_calendar",
                "sec_filings",
                "treasury_releases",
            ),
        )
        self.assertIs(
            type(registry.require("sec_filings")),
            SecFilingsSourceAdapter,
        )
        self.assertIs(
            type(registry.require("company_ir")),
            CompanyIrSourceAdapter,
        )
        self.assertIs(
            type(registry.require("federal_reserve")),
            FederalReserveSourceAdapter,
        )
        self.assertIs(
            type(registry.require("bls_releases")),
            BlsReleaseSourceAdapter,
        )
        self.assertIs(
            type(registry.require("treasury_releases")),
            TreasuryReleaseSourceAdapter,
        )
        self.assertIs(
            type(registry.require("official_macro_calendar")),
            OfficialMacroCalendarSourceAdapter,
        )
        with self.assertRaises(TypeError):
            build_official_source_registry(sec_filings=object())

        class ForgedSecInner:
            def recent_filings_batch(self, *_args, **_kwargs):
                raise AssertionError("forged SEC inner must never be called")

        sec = registry.require("sec_filings")
        object.__setattr__(sec, "_adapter", ForgedSecInner())
        with self.assertRaisesRegex(ValueError, "inner adapter changed"):
            sec.poll({}, observed_at_ms=FIXED_NOW_MS)

        class ForgedIrInner:
            def recent_releases_batch(self, *_args, **_kwargs):
                raise AssertionError("forged IR inner must never be called")

        company_ir = registry.require("company_ir")
        object.__setattr__(company_ir, "_adapter", ForgedIrInner())
        with self.assertRaisesRegex(ValueError, "inner adapter changed"):
            company_ir.poll({}, observed_at_ms=FIXED_NOW_MS)

        transport_registry = build_official_source_registry()
        transport_sec = transport_registry.require("sec_filings")
        object.__setattr__(
            transport_sec._adapter,
            "_fetch_json",
            lambda *_args, **_kwargs: {},
        )
        with self.assertRaisesRegex(ValueError, "transport changed"):
            transport_sec.poll({}, observed_at_ms=FIXED_NOW_MS)

        transport_ir = transport_registry.require("company_ir")
        object.__setattr__(
            transport_ir._adapter,
            "_fetch_bytes",
            lambda *_args, **_kwargs: b"<rss />",
        )
        with self.assertRaisesRegex(ValueError, "transport changed"):
            transport_ir.poll({}, observed_at_ms=FIXED_NOW_MS)

        transport_macro = transport_registry.require("federal_reserve")
        object.__setattr__(
            transport_macro._client,
            "_fetch_bytes",
            lambda _url: b"<rss />",
        )
        with self.assertRaisesRegex(ValueError, "transport changed"):
            transport_macro.poll({}, observed_at_ms=FIXED_NOW_MS)

    def test_sec_fixture_runs_twice_and_creates_one_inbox_event(self) -> None:
        source = SecEdgarAdapter(
            user_agent="AI Studio monitor@example.com",
            fetch_json=SecFixtureFetcher(),
            clock=lambda: FIXED_NOW,
            allowed_symbols=SEC_MONITOR_SYMBOLS,
        )
        adapter = SecFilingsSourceAdapter(
            adapter=source,
            allowed_symbols=["US.NVDA"],
            allowed_forms=["8-K"],
        )
        supervisor = self._supervisor(adapter)
        side_effects_before = self._forbidden_side_effect_counts()

        first = supervisor.run_once(adapter.adapter_key)
        self.clock[0] += 60_000
        second = supervisor.run_once(adapter.adapter_key)

        listing = self.inbox.list_items()
        self.assertEqual(first["status"], "SUCCEEDED")
        self.assertEqual(first["import"]["created_item_count"], 1)
        self.assertEqual(second["status"], "SUCCEEDED")
        self.assertIsNone(second["import"])
        self.assertEqual(second["run"]["duplicate_count"], 1)
        self.assertEqual(len(listing["items"]), 1)
        record = listing["items"][0]
        self.assertEqual(record["state"], "AWAITING_USER")
        self.assertEqual(record["received_at"], FIXED_NOW_MS)
        self.assertEqual(
            record["item"]["sources"][0]["url"],
            "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000001/valid.htm",
        )
        self.assertEqual(
            self._forbidden_side_effect_counts(),
            side_effects_before,
        )
        self.assertEqual(first["safety"]["provider_calls_performed"], 0)
        self.assertEqual(first["safety"]["formal_rounds_created"], 0)

    def test_ir_earnings_release_enters_inbox_without_provider_call(self) -> None:
        adapter = CompanyIrSourceAdapter(
            adapter=OfficialIrReleaseAdapter(
                fetch_bytes=MutableIrFixtureFetcher(),
                clock=lambda: FIXED_NOW,
            ),
            symbols=["US.MU"],
            force=True,
        )
        supervisor = self._supervisor(adapter)
        side_effects_before = self._forbidden_side_effect_counts()

        first = supervisor.run_once(adapter.adapter_key)
        self.clock[0] += 60_000
        second = supervisor.run_once(adapter.adapter_key)

        listing = self.inbox.list_items()
        self.assertEqual(first["import"]["created_item_count"], 1)
        self.assertEqual(second["run"]["duplicate_count"], 1)
        self.assertEqual(len(listing["items"]), 1)
        item = listing["items"][0]["item"]
        self.assertEqual(
            item["extensions"]["company_ir_v1"]["event_type"],
            "earnings_release",
        )
        self.assertEqual(
            item["sources"][0]["url"],
            "https://investors.micron.com/news/official-update",
        )
        self.assertEqual(
            self._forbidden_side_effect_counts(),
            side_effects_before,
        )

    def test_ir_revision_creates_a_second_explicit_revision_event(self) -> None:
        fetcher = MutableIrFixtureFetcher()
        adapter = CompanyIrSourceAdapter(
            adapter=OfficialIrReleaseAdapter(
                fetch_bytes=fetcher,
                clock=lambda: FIXED_NOW,
            ),
            symbols=["US.MU"],
            force=True,
        )
        supervisor = self._supervisor(adapter)
        side_effects_before = self._forbidden_side_effect_counts()

        first = supervisor.run_once(adapter.adapter_key)
        fetcher.summary = "Revised official RSS summary."
        self.clock[0] += 60_000
        revised = supervisor.run_once(adapter.adapter_key)

        listing = self.inbox.list_items()
        self.assertEqual(first["import"]["created_item_count"], 1)
        self.assertEqual(revised["status"], "SUCCEEDED")
        self.assertEqual(revised["import"]["created_item_count"], 1)
        self.assertEqual(len(listing["items"]), 2)
        revisions = [
            record["item"]["extensions"]["company_ir_v1"]
            for record in listing["items"]
        ]
        self.assertEqual(
            sorted(extension["is_revision"] for extension in revisions),
            [False, True],
        )
        self.assertEqual(
            {record["item"]["external_item_id"] for record in listing["items"]},
            {listing["items"][0]["item"]["external_item_id"]},
        )
        self.assertEqual(
            self._forbidden_side_effect_counts(),
            side_effects_before,
        )

    def test_conflicting_guid_degrades_and_never_advances_checkpoint(self) -> None:
        fetcher = ConflictingGuidIrFixtureFetcher()
        adapter = CompanyIrSourceAdapter(
            adapter=OfficialIrReleaseAdapter(
                fetch_bytes=fetcher,
                clock=lambda: FIXED_NOW,
            ),
            symbols=["US.MU"],
            force=True,
        )
        supervisor = self._supervisor(adapter)
        side_effects_before = self._forbidden_side_effect_counts()

        first = supervisor.run_once(adapter.adapter_key)
        fetcher.reverse = True
        self.clock[0] += 60_000
        second = supervisor.run_once(adapter.adapter_key)

        self.assertEqual(first["status"], "DEGRADED")
        self.assertEqual(first["run"]["rejected_count"], 2)
        self.assertIsNone(first["import"])
        self.assertEqual(first["state"]["checkpoint"], {})
        self.assertEqual(second["status"], "DEGRADED")
        self.assertEqual(second["run"]["rejected_count"], 2)
        self.assertIsNone(second["import"])
        self.assertEqual(second["state"]["checkpoint"], {})
        self.assertEqual(len(self.inbox.list_items()["items"]), 0)
        self.assertEqual(
            self._forbidden_side_effect_counts(),
            side_effects_before,
        )

    def test_checkpoint_capacity_excess_degrades_without_import(self) -> None:
        adapter = CompanyIrSourceAdapter(
            adapter=OfficialIrReleaseAdapter(
                fetch_bytes=ManyIrFixtureFetcher(
                    count=MAX_IR_PROJECTIONS + 1,
                ),
                clock=lambda: FIXED_NOW,
            ),
            symbols=["US.MU"],
            per_symbol_limit=8,
            force=True,
        )
        supervisor = self._supervisor(adapter)
        side_effects_before = self._forbidden_side_effect_counts()

        result = supervisor.run_once(adapter.adapter_key)

        self.assertEqual(result["status"], "DEGRADED")
        self.assertIsNone(result["import"])
        self.assertEqual(result["state"]["checkpoint"], {})
        self.assertEqual(result["run"]["rejected_count"], 251)
        self.assertEqual(len(self.inbox.list_items()["items"]), 0)
        self.assertEqual(
            self._forbidden_side_effect_counts(),
            side_effects_before,
        )


if __name__ == "__main__":
    unittest.main()
