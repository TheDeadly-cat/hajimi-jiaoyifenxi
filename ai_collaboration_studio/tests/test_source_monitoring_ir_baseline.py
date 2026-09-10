from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from xml.sax.saxutils import escape

from backend.market.ir_releases import OfficialIrReleaseAdapter
from backend.source_inbox_service import SourceInboxService
from backend.source_monitoring.adapters.company_ir import CompanyIrSourceAdapter
from backend.source_monitoring.contracts import SourceMonitoringContractError
from backend.source_monitoring.registry import SourceAdapterRegistry
from backend.source_monitoring.settings import SourceMonitoringSettings
from backend.source_monitoring.state_repository import SourceMonitoringStateRepository
from backend.source_monitoring.supervisor import SourceMonitoringSupervisor
from backend.store import StudioStore
from tests.test_source_monitoring_sec_baseline import NOW, NOW_MS


class MutableIrRecentFetcher:
    def __init__(self, count: int = 30) -> None:
        self.calls = 0
        self.records = [self.record(index) for index in range(1, count + 1)]
        self.after_snapshot = None

    @staticmethod
    def record(index: int, *, published: str = "Fri, 04 Sep 2026 20:00:00 +0000") -> dict:
        return {"index": index, "published": published, "summary": f"Official release {index}."}

    def __call__(self, _url: str, _hosts: set[str]) -> bytes:
        self.calls += 1
        records = copy.deepcopy(self.records)
        items = "".join(
            f"<item><title>Micron release {record['index']}</title>"
            f"<guid>baseline-guid-{record['index']}</guid>"
            f"<link>https://investors.micron.com/news/release-{record['index']}</link>"
            f"<pubDate>{escape(record['published'])}</pubDate>"
            f"<description>{escape(record['summary'])}</description></item>"
            for record in records
        )
        if self.after_snapshot is not None:
            callback, self.after_snapshot = self.after_snapshot, None
            callback()
        return f'<rss version="2.0"><channel>{items}</channel></rss>'.encode()


class IrCompleteBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="studio-ir-baseline-")
        self.addCleanup(directory.cleanup)
        self.database_path = Path(directory.name) / "isolated.sqlite3"
        self.store = StudioStore(self.database_path)
        self.repository = SourceMonitoringStateRepository(self.store, clock_ms=lambda: NOW_MS)
        self.inbox = SourceInboxService(self.store, clock=lambda: NOW_MS / 1_000)

    def adapter(self, fetcher: MutableIrRecentFetcher, *, limit: int = 8) -> CompanyIrSourceAdapter:
        return CompanyIrSourceAdapter(
            adapter=OfficialIrReleaseAdapter(source_format="rss", fetch_bytes=fetcher, clock=lambda: NOW),
            symbols=["US.MU"], per_symbol_limit=limit, force=True,
        )

    def supervisor(self, monitor, *, settings=None) -> SourceMonitoringSupervisor:
        self.repository.set_enabled(monitor.adapter_key, config_version=monitor.config_version, enabled=True)
        return SourceMonitoringSupervisor(
            registry=SourceAdapterRegistry((monitor,)), repository=self.repository,
            source_inbox=self.inbox,
            settings=settings or SourceMonitoringSettings(enabled=True, dry_run=False),
            clock_ms=lambda: NOW_MS, event_sink=lambda *_args, **_kwargs: None,
        )

    def items(self) -> list[dict]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            return [json.loads(row[0]) for row in connection.execute("SELECT item_json FROM source_inbox_items ORDER BY id")]

    def check_30_record_baseline(self, limit: int) -> None:
        monitor = self.adapter(MutableIrRecentFetcher(), limit=limit)
        supervisor = self.supervisor(monitor)
        seeded = supervisor.run_once(monitor.adapter_key)
        self.assertEqual(seeded["status"], "SUCCEEDED")
        self.assertEqual(seeded["initialization"]["outcome"], "seeded")
        self.assertEqual(len(seeded["state"]["checkpoint"]["projections"]), 30)
        for _ in range(5):
            self.assertEqual(supervisor.run_once(monitor.adapter_key)["status"], "SUCCEEDED")
        self.assertEqual(self.items(), [])

    def test_seed_all_30_records_with_default_eight_delivery_limit(self) -> None:
        self.check_30_record_baseline(8)

    def test_seed_all_30_records_with_maximum_twenty_delivery_limit(self) -> None:
        self.check_30_record_baseline(20)

    def test_post_snapshot_same_time_late_and_revision_beyond_limit_are_delivered_once(self) -> None:
        fetcher = MutableIrRecentFetcher()

        def change_after_snapshot():
            fetcher.records[-1]["summary"] = "Official revision after the baseline snapshot."
            fetcher.records.extend([
                fetcher.record(31),
                fetcher.record(32, published="Tue, 01 Sep 2026 10:00:00 +0000"),
            ])

        fetcher.after_snapshot = change_after_snapshot
        monitor = self.adapter(fetcher)
        supervisor = self.supervisor(monitor)
        self.assertEqual(supervisor.run_once(monitor.adapter_key)["status"], "SUCCEEDED")
        self.assertEqual(self.items(), [])
        self.assertEqual(supervisor.run_once(monitor.adapter_key)["status"], "SUCCEEDED")
        extensions = {item["extensions"]["company_ir_v1"]["guid"]: item["extensions"]["company_ir_v1"] for item in self.items()}
        self.assertEqual(set(extensions), {"baseline-guid-30", "baseline-guid-31", "baseline-guid-32"})
        self.assertTrue(extensions["baseline-guid-30"]["is_revision"])
        self.assertTrue(extensions["baseline-guid-30"]["previous_rss_projection_sha256"])
        self.assertFalse(extensions["baseline-guid-31"]["is_revision"])
        restarted = self.supervisor(self.adapter(fetcher))
        for _ in range(5):
            self.assertEqual(restarted.run_once(monitor.adapter_key)["status"], "SUCCEEDED")
        self.assertEqual(len(self.items()), 3)

    def test_temporarily_omitted_baseline_ids_cannot_reappear_as_new(self) -> None:
        fetcher = MutableIrRecentFetcher()
        monitor = self.adapter(fetcher)
        supervisor = self.supervisor(monitor)
        supervisor.run_once(monitor.adapter_key)
        original = copy.deepcopy(fetcher.records)
        fetcher.records = fetcher.records[:3]
        supervisor.run_once(monitor.adapter_key)
        fetcher.records = original
        for _ in range(5):
            supervisor.run_once(monitor.adapter_key)
        self.assertEqual(self.items(), [])

    def test_filtered_malformed_rss_item_prevents_partial_baseline(self) -> None:
        fetcher = MutableIrRecentFetcher()
        fetcher.records[-1]["published"] = "not a date"
        monitor = self.adapter(fetcher)
        result = self.supervisor(monitor).run_once(monitor.adapter_key)
        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(result["initialization"]["outcome"], "blocked")
        self.assertEqual(result["state"]["checkpoint"], {})
        self.assertEqual(self.items(), [])

    def test_empty_valid_rss_can_seal_empty_baseline(self) -> None:
        monitor = self.adapter(MutableIrRecentFetcher(0))
        result = self.supervisor(monitor).run_once(monitor.adapter_key)
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(result["initialization"]["outcome"], "seeded")
        self.assertEqual(result["state"]["checkpoint"]["projections"], [])

    def test_lifetime_capacity_blocks_without_evicting_retained_projections(self) -> None:
        fetcher = MutableIrRecentFetcher(250)
        monitor = self.adapter(fetcher)
        supervisor = self.supervisor(monitor)
        seeded = supervisor.run_once(monitor.adapter_key)
        baseline = copy.deepcopy(seeded["state"]["checkpoint"])
        fetcher.records = [fetcher.record(251)]
        result = supervisor.run_once(monitor.adapter_key)
        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(result["state"]["last_error_code"], "COMPANY_IR_CHECKPOINT_CAPACITY_EXCEEDED")
        self.assertEqual(result["state"]["checkpoint"], baseline)
        self.assertEqual(self.items(), [])

    def test_legacy_checkpoint_blocks_before_request_and_explicit_migration_keeps_inbox(self) -> None:
        fetcher = MutableIrRecentFetcher()
        old_monitor = self.adapter(fetcher, limit=2)
        old = self.supervisor(old_monitor, settings=SourceMonitoringSettings(
            enabled=True, dry_run=False, initial_mode="from_time", from_time="1970-01-01T00:00:00Z"
        ))
        old_result = old.run_once(old_monitor.adapter_key)
        preserved_items = self.items()
        legacy_checkpoint = {**old_result["state"]["checkpoint"], "version": "company_ir_checkpoint_v1"}
        calls = fetcher.calls
        with self.assertRaises(SourceMonitoringContractError) as caught:
            old_monitor.poll(legacy_checkpoint, observed_at_ms=NOW_MS)
        self.assertEqual(caught.exception.code, "COMPANY_IR_BASELINE_UPGRADE_REQUIRED")
        self.assertEqual(fetcher.calls, calls)
        disabled = self.repository.set_enabled(old_monitor.adapter_key, config_version=old_monitor.config_version, enabled=False)
        legacy = self.repository.migrate_config(
            old_monitor.adapter_key, expected_config_version=old_monitor.config_version,
            new_config_version="company_ir_config_v1_legacy_fixture",
            expected_state_version=disabled["state_version"], next_checkpoint=legacy_checkpoint,
        )
        self.assertEqual(legacy["checkpoint"], legacy_checkpoint)
        monitor = self.adapter(fetcher)
        migrated = self.repository.migrate_config(
            monitor.adapter_key, expected_config_version=legacy["config_version"],
            new_config_version=monitor.config_version, expected_state_version=legacy["state_version"], next_checkpoint={},
        )
        self.assertEqual(migrated["checkpoint"], {})
        self.assertEqual(self.supervisor(monitor).run_once(monitor.adapter_key)["initialization"]["outcome"], "seeded")
        self.assertEqual(self.items(), preserved_items)


if __name__ == "__main__":
    unittest.main()
