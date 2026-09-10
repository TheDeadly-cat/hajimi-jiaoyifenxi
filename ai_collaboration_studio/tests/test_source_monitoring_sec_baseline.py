from __future__ import annotations

import copy
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from backend.market.sec_edgar import SEC_TICKERS_URL, SecEdgarAdapter
from backend.source_inbox_service import SourceInboxService
from backend.source_monitoring.adapters.sec_filings import SecFilingsSourceAdapter
from backend.source_monitoring.contracts import SourceMonitoringContractError
from backend.source_monitoring.operator_service import SourceMonitoringOperatorService
from backend.source_monitoring.registry import SourceAdapterRegistry
from backend.source_monitoring.settings import SourceMonitoringSettings
from backend.source_monitoring.state_repository import SourceMonitoringStateRepository
from backend.source_monitoring.supervisor import SourceMonitoringSupervisor
from backend.store import StudioStore


NOW = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1_000)


class MutableSecRecentFetcher:
    """One bounded official recent snapshot; only the in-memory source changes."""

    def __init__(self, count: int = 13) -> None:
        self.calls: list[str] = []
        self.records = [(index, "2026-09-04T20:00:00Z") for index in range(1, count + 1)]
        self.after_snapshot = None
        self.truncate_documents = False

    def __call__(self, url: str, _user_agent: str) -> dict:
        self.calls.append(url)
        if url == SEC_TICKERS_URL:
            return {"0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA"}}
        records = copy.deepcopy(self.records)
        recent = {
            "accessionNumber": [f"0001045810-26-{index:06d}" for index, _ in records],
            "form": ["8-K"] * len(records),
            "filingDate": [accepted[:10] for _, accepted in records],
            "acceptanceDateTime": [accepted for _, accepted in records],
            "primaryDocument": [f"filing-{index}.htm" for index, _ in records],
        }
        if self.truncate_documents:
            recent["primaryDocument"] = recent["primaryDocument"][:-1]
        if self.after_snapshot is not None:
            callback, self.after_snapshot = self.after_snapshot, None
            callback()
        return {"cik": "0001045810", "name": "NVIDIA", "filings": {"recent": recent}}


class SecCompleteBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="studio-sec-baseline-")
        self.addCleanup(self.directory.cleanup)
        self.database_path = Path(self.directory.name) / "isolated.sqlite3"
        self.store = StudioStore(self.database_path)
        self.repository = SourceMonitoringStateRepository(self.store, clock_ms=lambda: NOW_MS)
        self.inbox = SourceInboxService(self.store, clock=lambda: NOW_MS / 1_000)

    def adapter(self, fetcher: MutableSecRecentFetcher, *, limit: int = 3) -> SecFilingsSourceAdapter:
        return SecFilingsSourceAdapter(
            adapter=SecEdgarAdapter(
                user_agent="Studio fixture fixture@example.com",
                fetch_json=fetcher,
                clock=lambda: NOW,
                allowed_symbols=["US.NVDA"],
            ),
            allowed_symbols=["US.NVDA"],
            allowed_forms=["8-K"],
            per_symbol_limit=limit,
            force=True,
        )

    def supervisor(self, adapter, *, settings=None) -> SourceMonitoringSupervisor:
        self.repository.set_enabled(adapter.adapter_key, config_version=adapter.config_version, enabled=True)
        return SourceMonitoringSupervisor(
            registry=SourceAdapterRegistry((adapter,)),
            repository=self.repository,
            source_inbox=self.inbox,
            settings=settings or SourceMonitoringSettings(enabled=True, dry_run=False),
            clock_ms=lambda: NOW_MS,
            event_sink=lambda *_args, **_kwargs: None,
        )

    def inbox_ids(self) -> list[str]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            return [row[0] for row in connection.execute(
                "SELECT external_item_id FROM source_inbox_items ORDER BY external_item_id"
            )]

    def test_seed_covers_more_than_four_delivery_batches_without_historical_import(self) -> None:
        monitor = self.adapter(MutableSecRecentFetcher())
        supervisor = self.supervisor(monitor)
        first = supervisor.run_once(monitor.adapter_key)
        self.assertEqual(first["status"], "SUCCEEDED")
        self.assertEqual(first["initialization"]["outcome"], "seeded")
        self.assertEqual(len(first["state"]["checkpoint"]["seen_accessions"]), 13)
        for _ in range(5):
            self.assertEqual(supervisor.run_once(monitor.adapter_key)["status"], "SUCCEEDED")
        self.assertEqual(self.inbox_ids(), [])

    def test_new_ids_after_first_snapshot_include_same_time_and_late_records_once(self) -> None:
        fetcher = MutableSecRecentFetcher()
        fetcher.after_snapshot = lambda: fetcher.records.extend([
            (14, "2026-09-05T09:00:00Z"),
            (15, "2026-09-04T20:00:00Z"),
            (16, "2026-09-01T10:00:00Z"),
        ])
        monitor = self.adapter(fetcher)
        supervisor = self.supervisor(monitor)
        self.assertEqual(supervisor.run_once(monitor.adapter_key)["status"], "SUCCEEDED")
        self.assertEqual(self.inbox_ids(), [])
        self.assertEqual(supervisor.run_once(monitor.adapter_key)["status"], "SUCCEEDED")
        expected = [f"0001045810-26-{index:06d}" for index in (14, 15, 16)]
        self.assertEqual(self.inbox_ids(), expected)
        restarted = self.supervisor(self.adapter(fetcher))
        for _ in range(5):
            self.assertEqual(restarted.run_once(monitor.adapter_key)["status"], "SUCCEEDED")
        self.assertEqual(self.inbox_ids(), expected)

    def test_regular_from_time_delivery_still_drains_unseen_items_by_limit(self) -> None:
        monitor = self.adapter(MutableSecRecentFetcher(7))
        supervisor = self.supervisor(monitor, settings=SourceMonitoringSettings(
            enabled=True, dry_run=False, initial_mode="from_time", from_time="1970-01-01T00:00:00Z"
        ))
        for expected_count in (3, 6, 7):
            self.assertEqual(supervisor.run_once(monitor.adapter_key)["status"], "SUCCEEDED")
            self.assertEqual(len(self.inbox_ids()), expected_count)

    def test_seeded_ids_survive_temporary_snapshot_omission_and_restoration(self) -> None:
        fetcher = MutableSecRecentFetcher()
        monitor = self.adapter(fetcher)
        supervisor = self.supervisor(monitor)
        self.assertEqual(supervisor.run_once(monitor.adapter_key)["status"], "SUCCEEDED")
        baseline = copy.deepcopy(self.repository.get_state(monitor.adapter_key)["checkpoint"])
        original = copy.deepcopy(fetcher.records)
        fetcher.records = fetcher.records[:6]
        self.assertEqual(supervisor.run_once(monitor.adapter_key)["status"], "SUCCEEDED")
        fetcher.records = original
        for _ in range(4):
            self.assertEqual(supervisor.run_once(monitor.adapter_key)["status"], "SUCCEEDED")
        self.assertEqual(self.inbox_ids(), [])
        self.assertEqual(self.repository.get_state(monitor.adapter_key)["checkpoint"], baseline)

    def test_lifetime_seen_capacity_blocks_new_delivery_without_evicting_baseline_ids(self) -> None:
        fetcher = MutableSecRecentFetcher(1000)
        monitor = self.adapter(fetcher)
        supervisor = self.supervisor(monitor)
        seeded = supervisor.run_once(monitor.adapter_key)
        baseline = copy.deepcopy(seeded["state"]["checkpoint"])
        fetcher.records = [(1001, "2026-09-05T09:00:00Z")]
        result = supervisor.run_once(monitor.adapter_key)
        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(result["state"]["last_error_code"], "SEC_CHECKPOINT_CAPACITY_EXCEEDED")
        self.assertEqual(result["state"]["checkpoint"], baseline)
        self.assertEqual(self.inbox_ids(), [])

    def test_truncated_recent_scope_cannot_seal_a_partial_seed(self) -> None:
        fetcher = MutableSecRecentFetcher()
        fetcher.truncate_documents = True
        monitor = self.adapter(fetcher)
        result = self.supervisor(monitor).run_once(monitor.adapter_key)
        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(result["initialization"]["outcome"], "blocked")
        self.assertEqual(result["state"]["checkpoint"], {})
        self.assertEqual(self.inbox_ids(), [])
        self.assertIsNone(self.repository.get_latest_successful_initialization(
            monitor.adapter_key, config_version=monitor.config_version
        ))

    def test_empty_complete_recent_scope_can_initialize_without_a_source_failure(self) -> None:
        monitor = self.adapter(MutableSecRecentFetcher(0))
        result = self.supervisor(monitor).run_once(monitor.adapter_key)
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(result["initialization"]["outcome"], "seeded")
        self.assertEqual(result["state"]["checkpoint"]["seen_accessions"], [])

    def test_scope_over_capacity_cannot_advance_or_initialize(self) -> None:
        monitor = self.adapter(MutableSecRecentFetcher(1001))
        result = self.supervisor(monitor).run_once(monitor.adapter_key)
        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(result["initialization"]["outcome"], "blocked")
        self.assertEqual(result["state"]["checkpoint"], {})
        self.assertEqual(self.inbox_ids(), [])

    def test_legacy_partial_checkpoint_requires_explicit_upgrade_before_any_fetch(self) -> None:
        fetcher = MutableSecRecentFetcher()
        monitor = self.adapter(fetcher)
        checkpoint = {"version": "sec_filings_checkpoint_v1", "seen_accessions": ["0001045810-26-000001"]}
        original = copy.deepcopy(checkpoint)
        with self.assertRaises(SourceMonitoringContractError) as caught:
            monitor.poll(checkpoint, observed_at_ms=NOW_MS)
        self.assertEqual(caught.exception.code, "SEC_BASELINE_UPGRADE_REQUIRED")
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(checkpoint, original)

    def test_legacy_limited_batch_without_scope_evidence_cannot_seed(self) -> None:
        class LimitedBatch:
            def recent_filings_batch(self, *_args, **_kwargs):
                return {"rows": [], "source_errors": []}

        monitor = SecFilingsSourceAdapter(adapter=LimitedBatch(), allowed_symbols=["US.NVDA"])
        result = self.supervisor(monitor).run_once(monitor.adapter_key)
        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(result["initialization"]["outcome"], "blocked")
        self.assertEqual(result["state"]["checkpoint"], {})

    def test_operator_seed_preview_binds_ids_beyond_the_delivery_limit(self) -> None:
        fetcher = MutableSecRecentFetcher()
        monitor = self.adapter(fetcher)
        state = self.repository.get_or_create_state(monitor.adapter_key, config_version=monitor.config_version)
        operator = SourceMonitoringOperatorService(
            store=self.store,
            settings=SourceMonitoringSettings(enabled=True, dry_run=False),
            registry=SourceAdapterRegistry((monitor,)),
            repository=self.repository,
            clock_ms=lambda: NOW_MS,
        )
        kwargs = {"expected_config_version": monitor.config_version, "expected_state_version": state["state_version"]}
        first = operator.preview(monitor.adapter_key, **kwargs)
        fetcher.records.append((14, "2026-09-04T20:00:00Z"))
        second = operator.preview(monitor.adapter_key, **kwargs)
        self.assertNotEqual(first["preview_sha256"], second["preview_sha256"])
        self.assertEqual(self.repository.get_state(monitor.adapter_key), state)
        self.assertEqual(self.inbox_ids(), [])

    def test_explicit_config_rebaseline_preserves_prior_inbox_rooms_and_materials(self) -> None:
        fetcher = MutableSecRecentFetcher()
        old_monitor = self.adapter(fetcher, limit=2)
        old = self.supervisor(old_monitor, settings=SourceMonitoringSettings(
            enabled=True, dry_run=False, initial_mode="from_time", from_time="1970-01-01T00:00:00Z"
        ))
        self.assertEqual(old.run_once(old_monitor.adapter_key)["status"], "SUCCEEDED")
        room = self.store.create_room("Existing research", "Preserve user research")
        material = self.store.add_material(room["room"]["id"], {"title": "Existing evidence", "content": "User-authored material"})
        self.assertIsNotNone(material)

        def existing_rows():
            with closing(sqlite3.connect(self.database_path)) as connection:
                return {
                    table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                    for table in ("source_inbox_items", "rooms", "materials")
                }

        preserved = existing_rows()
        disabled = self.repository.set_enabled(old_monitor.adapter_key, config_version=old_monitor.config_version, enabled=False)
        legacy = self.repository.migrate_config(
            old_monitor.adapter_key,
            expected_config_version=old_monitor.config_version,
            new_config_version="sec_filings_config_v2_legacy_fixture",
            expected_state_version=disabled["state_version"],
            next_checkpoint={"version": "sec_filings_checkpoint_v1", "seen_accessions": self.inbox_ids()},
        )
        monitor = self.adapter(fetcher)
        migrated = self.repository.migrate_config(
            monitor.adapter_key,
            expected_config_version=legacy["config_version"],
            new_config_version=monitor.config_version,
            expected_state_version=legacy["state_version"],
            next_checkpoint={},
        )
        self.assertEqual(migrated["checkpoint"], {})
        self.assertEqual(migrated["last_success_at_ms"], 0)
        self.assertIsNone(self.repository.get_latest_successful_initialization(monitor.adapter_key, config_version=monitor.config_version))
        supervisor = self.supervisor(monitor)
        seeded = supervisor.run_once(monitor.adapter_key)
        self.assertEqual(seeded["initialization"]["outcome"], "seeded")
        self.assertEqual(len(seeded["state"]["checkpoint"]["seen_accessions"]), 13)
        self.assertEqual(supervisor.run_once(monitor.adapter_key)["status"], "SUCCEEDED")
        self.assertEqual(existing_rows(), preserved)


if __name__ == "__main__":
    unittest.main()
