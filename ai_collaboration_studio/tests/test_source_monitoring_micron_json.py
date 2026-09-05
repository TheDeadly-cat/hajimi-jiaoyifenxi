from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from backend.market.ir_releases import OfficialIrReleaseAdapter
from backend.source_inbox_service import SourceInboxService
from backend.source_monitoring.adapters.company_ir import CompanyIrSourceAdapter
from backend.source_monitoring.registry import SourceAdapterRegistry
from backend.source_monitoring.settings import SourceMonitoringSettings
from backend.source_monitoring.state_repository import SourceMonitoringStateRepository
from backend.source_monitoring.supervisor import SourceMonitoringSupervisor
from backend.source_monitoring.trading_impact_rules import TradingImpactRulesV1
from backend.store import StudioStore
from tests.test_source_monitoring_sec_baseline import NOW_MS


class MicronJsonFixtureTransport:
    def __init__(self, count: int = 30) -> None:
        self.records = [self.record(index) for index in range(1, count + 1)]
        self.calls: list[tuple[str, bool]] = []
        self.after_list = None
        self.after_head = None
        self.missing_metadata_id = 0
        self.published_at = "2026-09-04T15:01:00Z"

    @staticmethod
    def record(index: int) -> dict:
        return {
            "PressReleaseId": index, "RevisionNumber": 1,
            "Headline": f"Micron official announcement {index}",
            "LinkToDetailPage": f"/news/press-release/2026/Announcement-{index}/default.aspx",
            "PressReleaseDate": "09/04/2026 16:01:00", "ShortDescription": "Official list metadata.",
        }

    def __call__(self, url, *, deadline_monotonic_ms=0, cancel_event=None, max_bytes, head_only):
        del deadline_monotonic_ms, cancel_event, max_bytes
        self.calls.append((url, head_only))
        if not head_only:
            rows = copy.deepcopy(self.records)
            if self.after_list is not None:
                callback, self.after_list = self.after_list, None
                callback()
            return json.dumps({"GetPressReleaseListResult": rows}).encode()
        row = next(row for row in self.records if urljoin("https://investors.micron.com", row["LinkToDetailPage"]) == url)
        metadata = {
            "@type": "NewsArticle", "mainEntityOfPage": {"@id": url},
            "headline": row["Headline"], "datePublished": self.published_at,
            "dateModified": self.published_at,
        }
        if row["PressReleaseId"] == self.missing_metadata_id:
            metadata.pop("datePublished")
        if self.after_head is not None:
            self.after_head()
        return ('<html><head><script type="application/ld+json">' + json.dumps(metadata) + '</script></head>').encode()


class MicronJsonCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="studio-micron-json-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "isolated.sqlite3"
        self.clock_ms = NOW_MS
        self.store = StudioStore(self.path)
        self.repository = SourceMonitoringStateRepository(self.store, clock_ms=lambda: self.clock_ms)
        self.inbox = SourceInboxService(self.store, clock=lambda: self.clock_ms / 1_000)

    def clock(self):
        return datetime.fromtimestamp(self.clock_ms / 1_000, tz=timezone.utc)

    def adapter(self, transport):
        return CompanyIrSourceAdapter(
            adapter=OfficialIrReleaseAdapter(source_format="q4_json", micron_fetch_bytes=transport, clock=self.clock),
            symbols=["US.MU"], per_symbol_limit=8, force=True, receipt_clock=self.clock,
        )

    def supervisor(self, adapter, *, from_time=False, impact=False):
        self.repository.set_enabled(adapter.adapter_key, config_version=adapter.config_version, enabled=True)
        settings = SourceMonitoringSettings(enabled=True, dry_run=False, trading_impact_rules_enabled=impact, **(
            {"initial_mode": "from_time", "from_time": "1970-01-01T00:00:00Z"} if from_time else {}
        ))
        return SourceMonitoringSupervisor(
            registry=SourceAdapterRegistry((adapter,)), repository=self.repository,
            source_inbox=self.inbox, settings=settings, clock_ms=lambda: self.clock_ms,
            event_sink=lambda *_args, **_kwargs: None,
            impact_rules=TradingImpactRulesV1() if impact else None,
        )

    def items(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return [json.loads(row[0]) for row in connection.execute("SELECT item_json FROM source_inbox_items ORDER BY id")]

    def test_default_micron_format_is_explicit_json_and_legacy_injection_requires_rss(self):
        self.assertEqual(OfficialIrReleaseAdapter().source_format, "q4_json")
        with self.assertRaises(ValueError):
            OfficialIrReleaseAdapter(fetch_bytes=lambda *_args: b"<rss/>")

    def test_json_projection_uses_bound_utc_head_metadata_and_never_claims_rss_or_body(self):
        transport = MicronJsonFixtureTransport(1)
        adapter = self.adapter(transport)
        result = adapter.poll({}, observed_at_ms=self.clock_ms)
        self.assertEqual(result.source_errors, ())
        item = result.observed_items[0]
        extension = item["extensions"]["company_ir_v2"]
        self.assertEqual(extension["source_declared_time_raw"], "09/04/2026 16:01:00")
        self.assertEqual(item["published_at"], "2026-09-04T15:01:00Z")
        self.assertNotIn("company_ir_v1", item["extensions"])
        self.assertEqual([source["source_type"] for source in item["sources"]], ["company_ir_time_metadata", "company_ir_json_projection"])
        self.assertEqual(item["sources"][0]["content_sha256"], extension["time_metadata_sha256"])
        self.assertEqual(item["sources"][1]["content_sha256"], extension["projection_sha256"])
        self.assertEqual(len(transport.calls), 2)
        self.assertRegex(adapter.config_version, r"^company_ir_config_v3_[0-9a-f]{16}$")

    def test_full_recent30_baseline_then_new_and_revision_survive_restart_once(self):
        transport = MicronJsonFixtureTransport()
        adapter = self.adapter(transport)
        supervisor = self.supervisor(adapter)
        seed = supervisor.run_once(adapter.adapter_key)
        self.assertEqual(seed["status"], "SUCCEEDED")
        self.assertEqual(len(seed["state"]["checkpoint"]["projections"]), 30)
        self.assertEqual(self.items(), [])
        transport.records[29]["RevisionNumber"] = 2
        transport.records[29]["ShortDescription"] = "Revised official list metadata."
        transport.records = transport.records[1:] + [transport.record(31)]
        self.assertEqual(supervisor.run_once(adapter.adapter_key)["status"], "SUCCEEDED")
        extensions = {item["extensions"]["company_ir_v2"]["press_release_id"]: item["extensions"]["company_ir_v2"] for item in self.items()}
        self.assertEqual(set(extensions), {30, 31})
        self.assertTrue(extensions[30]["is_revision"])
        self.assertFalse(extensions[31]["is_revision"])
        restarted = self.supervisor(self.adapter(transport))
        self.assertEqual(restarted.run_once(adapter.adapter_key)["status"], "SUCCEEDED")
        self.assertEqual(len(self.items()), 2)

    def test_one_missing_head_timestamp_blocks_complete_seed_without_import(self):
        transport = MicronJsonFixtureTransport()
        transport.missing_metadata_id = 30
        adapter = self.adapter(transport)
        result = self.supervisor(adapter).run_once(adapter.adapter_key)
        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(result["state"]["checkpoint"], {})
        self.assertEqual(result["initialization"]["outcome"], "blocked")
        self.assertEqual(self.items(), [])

    def test_publication_during_metadata_request_uses_trusted_receipt_clock(self):
        transport = MicronJsonFixtureTransport(1)
        start_ms = self.clock_ms
        transport.published_at = "2026-09-05T12:00:01Z"
        transport.after_head = lambda: setattr(self, "clock_ms", start_ms + 2_000)
        result = self.adapter(transport).poll({}, observed_at_ms=start_ms)
        self.assertEqual(result.source_errors, ())
        self.assertEqual(result.captured_at_ms, start_ms)
        self.assertEqual(result.observed_items[0]["published_at"], transport.published_at)

    def test_true_future_metadata_does_not_advance_checkpoint(self):
        transport = MicronJsonFixtureTransport(1)
        transport.published_at = "2026-09-05T12:01:00Z"
        result = self.adapter(transport).poll({}, observed_at_ms=self.clock_ms)
        self.assertTrue(result.source_errors)
        self.assertEqual(result.observed_items, ())
        self.assertEqual(result.next_checkpoint, {})

    def test_delayed_uncommitted_replay_has_identical_json_projection(self):
        transport = MicronJsonFixtureTransport(1)
        adapter = self.adapter(transport)
        first = adapter.poll({}, observed_at_ms=self.clock_ms)
        self.clock_ms += 60_000
        replay = self.adapter(transport).poll({}, observed_at_ms=self.clock_ms)
        self.assertEqual(first.observed_items, replay.observed_items)

    def test_real_json_producer_imports_with_independently_validated_neutral_sidecar(self):
        transport = MicronJsonFixtureTransport(1)
        transport.records[0]["Headline"] = "Micron Technology to Report Fiscal Fourth Quarter Results"
        transport.records[0]["ShortDescription"] = ""
        adapter = self.adapter(transport)
        result = self.supervisor(adapter, from_time=True, impact=True).run_once(adapter.adapter_key)
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(result["import"]["created_item_count"], 1)
        accounting = result["trading_impact_rules"]
        self.assertEqual(accounting["evaluated_count"], 1)
        self.assertEqual(accounting["no_match_count"], 1)
        self.assertEqual(accounting["matched_count"], 0)
        self.assertEqual(result["safety"]["provider_calls_performed"], 0)
        item = self.items()[0]
        self.assertEqual(item["extensions"]["company_ir_v2"]["event_type"], "earnings_schedule")
        self.assertEqual(item["impact_hypotheses"], [])
        self.assertTrue(item["summary"].startswith("Official Micron press-release metadata:"))

    def mixed_adapter(self, transport, *, rss_fails):
        def rss_fetch(_url, _hosts):
            if rss_fails:
                raise OSError("fixture Seagate RSS unavailable")
            return b'<rss><channel><item><title>Seagate official release</title><guid>stx-1</guid><link>https://investors.seagate.com/news/release-1</link><pubDate>Fri, 04 Sep 2026 20:00:00 +0000</pubDate><description>Official RSS metadata.</description></item></channel></rss>'
        return CompanyIrSourceAdapter(
            adapter=OfficialIrReleaseAdapter(
                source_format="q4_json", micron_fetch_bytes=transport,
                fetch_bytes=rss_fetch, clock=self.clock,
            ),
            symbols=["US.MU", "US.STX"], force=True, receipt_clock=self.clock,
        )

    def test_normal_mixed_rss_failure_imports_healthy_micron_and_preserves_checkpoint(self):
        monitor = self.mixed_adapter(MicronJsonFixtureTransport(1), rss_fails=True)
        supervisor = self.supervisor(monitor)
        run = self.repository.start_run(monitor.adapter_key, config_version=monitor.config_version)["run"]
        self.repository.complete_run(run["run_id"], next_checkpoint={}, status="SUCCEEDED", observed_count=0, accepted_count=0, duplicate_count=0, rejected_count=0, next_due_at_ms=self.clock_ms)
        result = supervisor.run_once(monitor.adapter_key)
        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(result["state"]["checkpoint"], {})
        self.assertEqual([item["entities"][0]["id"] for item in self.items()], ["US.MU"])

    def test_normal_mixed_bad_micron_metadata_imports_only_healthy_rss(self):
        transport = MicronJsonFixtureTransport(1)
        transport.missing_metadata_id = 1
        monitor = self.mixed_adapter(transport, rss_fails=False)
        supervisor = self.supervisor(monitor)
        run = self.repository.start_run(monitor.adapter_key, config_version=monitor.config_version)["run"]
        self.repository.complete_run(run["run_id"], next_checkpoint={}, status="SUCCEEDED", observed_count=0, accepted_count=0, duplicate_count=0, rejected_count=0, next_due_at_ms=self.clock_ms)
        result = supervisor.run_once(monitor.adapter_key)
        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(result["state"]["checkpoint"], {})
        self.assertEqual([item["entities"][0]["id"] for item in self.items()], ["US.STX"])

    def test_mixed_seed_rss_failure_cannot_import_or_advance_any_source(self):
        monitor = self.mixed_adapter(MicronJsonFixtureTransport(1), rss_fails=True)
        result = self.supervisor(monitor).run_once(monitor.adapter_key)
        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(result["initialization"]["outcome"], "blocked")
        self.assertEqual(result["state"]["checkpoint"], {})
        self.assertEqual(self.items(), [])


if __name__ == "__main__":
    unittest.main()
