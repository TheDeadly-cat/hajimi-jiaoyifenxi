from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.source_monitoring.health_service import (  # noqa: E402
    SOURCE_MONITORING_HEALTH_SERVICE_VERSION,
    SourceMonitoringHealthService,
    SourceMonitoringHealthServiceError,
)
from backend.source_monitoring.settings import SourceMonitoringSettings  # noqa: E402
from backend.source_monitoring.state_repository import (  # noqa: E402
    SourceMonitoringStateError,
    SourceMonitoringStateRepository,
)
from backend.store import StudioStore  # noqa: E402


class _OldStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection
class SourceMonitoringHealthServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-source-monitor-health-"
        )
        self.database_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.store = StudioStore(self.database_path)
        self.settings = SourceMonitoringSettings()
        self.clock = 1_900_000_000_000

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def service(self, store=None) -> SourceMonitoringHealthService:
        return SourceMonitoringHealthService(
            store or self.store,
            clock_ms=lambda: self.clock,
            settings=self.settings,
        )

    def test_default_off_catalog_snapshot_is_read_only_and_nonexecuting(self) -> None:
        before = self.database_path.stat()
        sidecars_before = sorted(path.name for path in self.database_path.parent.iterdir())
        snapshot = self.service().snapshot()
        after = self.database_path.stat()
        sidecars_after = sorted(path.name for path in self.database_path.parent.iterdir())

        self.assertEqual(snapshot["version"], SOURCE_MONITORING_HEALTH_SERVICE_VERSION)
        self.assertEqual(snapshot["adapter_count"], 7)
        self.assertTrue(snapshot["persistence_available"])
        self.assertFalse(snapshot["runtime_liveness_verified"])
        self.assertEqual(snapshot["settings"], self.settings.to_dict())
        self.assertFalse(snapshot["settings"]["enabled"])
        self.assertFalse(snapshot["settings"]["auto_start"])
        self.assertTrue(snapshot["settings"]["dry_run"])
        self.assertEqual(snapshot["operations"]["schema_status"], "current")
        self.assertEqual(
            snapshot["operations"]["retention_mode"],
            "retain_all_evidence",
        )
        self.assertFalse(snapshot["operations"]["evidence_deletion_allowed"])
        self.assertFalse(snapshot["operations"]["runtime_liveness_verified"])
        self.assertTrue(all(
            adapter["state"] == "disabled"
            and adapter["persisted_state"] is False
            and adapter["persisted_enabled"] is False
            and adapter["config_status"] == "absent"
            and adapter["latest_run"] is None
            and adapter["runtime_liveness_verified"] is False
            and adapter["metadata"]["execution_capability"] == "none"
            and adapter["metadata"]["live_trading_allowed"] is False
            for adapter in snapshot["adapters"]
        ))
        self.assertEqual(
            snapshot["safety"],
            {
                "database_writes_performed": 0,
                "provider_calls_performed": 0,
                "network_requests_performed": 0,
                "market_calls_performed": 0,
                "formal_rounds_created": 0,
                "execution_capability": "none",
                "live_trading_allowed": False,
            },
        )
        self.assertEqual(
            (after.st_size, after.st_mtime_ns),
            (before.st_size, before.st_mtime_ns),
        )
        self.assertEqual(sidecars_after, sidecars_before)

    def test_pre_operations_database_reports_migration_required_without_writes(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.executescript(
                """
                DROP TRIGGER trg_source_monitoring_operations_marker_no_update;
                DROP TRIGGER trg_source_monitoring_operations_marker_no_delete;
                DROP TRIGGER trg_source_monitoring_operations_marker_no_replace;
                DROP TRIGGER trg_source_monitoring_retention_receipts_no_update;
                DROP TRIGGER trg_source_monitoring_retention_receipts_no_delete;
                DROP TRIGGER trg_source_monitoring_retention_receipts_no_replace;
                DROP INDEX idx_source_monitoring_retention_receipts_time;
                DROP TABLE source_monitoring_retention_receipts;
                DELETE FROM schema_migrations
                 WHERE key='source_monitoring_operations_v1';
                """
            )
        before_bytes = self.database_path.read_bytes()
        before = self.database_path.stat()
        sidecars_before = sorted(path.name for path in self.database_path.parent.iterdir())

        snapshot = self.service().snapshot()

        after = self.database_path.stat()
        self.assertTrue(snapshot["persistence_available"])
        self.assertEqual(
            snapshot["operations"]["schema_status"],
            "migration_required",
        )
        self.assertEqual(snapshot["operations"]["retention_receipt_count"], 0)
        self.assertEqual(self.database_path.read_bytes(), before_bytes)
        self.assertEqual(
            (after.st_size, after.st_mtime_ns),
            (before.st_size, before.st_mtime_ns),
        )
        self.assertEqual(
            sorted(path.name for path in self.database_path.parent.iterdir()),
            sidecars_before,
        )

    def test_persisted_failure_and_latest_run_are_redacted_read_only_evidence(self) -> None:
        initial = self.service().snapshot()
        sec = next(
            adapter for adapter in initial["adapters"]
            if adapter["adapter_key"] == "sec_filings"
        )
        config_version = sec["metadata"]["config_version"]
        repository = SourceMonitoringStateRepository(
            self.store,
            clock_ms=lambda: self.clock,
        )
        repository.get_or_create_state(
            "sec_filings",
            config_version=config_version,
        )
        repository.set_enabled(
            "sec_filings",
            config_version=config_version,
            enabled=True,
        )
        started = repository.start_run(
            "sec_filings",
            config_version=config_version,
        )
        self.clock += 250
        repository.fail_run(
            started["run"]["run_id"],
            error_code="FIXTURE_FAILURE",
            error_message="fixture detail that the health response need not expose",
            next_due_at_ms=self.clock + 60_000,
            observed_count=1,
            rejected_count=1,
        )
        before = self.database_path.stat()

        snapshot = self.service().snapshot()
        sec_health = next(
            adapter for adapter in snapshot["adapters"]
            if adapter["adapter_key"] == "sec_filings"
        )
        after = self.database_path.stat()

        self.assertTrue(sec_health["persisted_state"])
        self.assertTrue(sec_health["persisted_enabled"])
        self.assertFalse(sec_health["enabled"])
        self.assertEqual(sec_health["state"], "disabled")
        self.assertEqual(sec_health["config_status"], "current")
        self.assertEqual(sec_health["last_error_code"], "FIXTURE_FAILURE")
        self.assertEqual(sec_health["latest_run"]["status"], "FAILED")
        self.assertEqual(sec_health["latest_run"]["error_code"], "FIXTURE_FAILURE")
        self.assertNotIn("checkpoint", sec_health)
        self.assertNotIn("etag", sec_health)
        self.assertNotIn("receipt_id", sec_health["latest_run"])
        self.assertNotIn("error_message", sec_health["latest_run"])
        self.assertFalse(sec_health["runtime_liveness_verified"])
        self.assertEqual(
            (after.st_size, after.st_mtime_ns),
            (before.st_size, before.st_mtime_ns),
        )

    def test_globally_disabled_running_evidence_cannot_project_runtime_running(self) -> None:
        initial = self.service().snapshot()
        sec = next(
            adapter for adapter in initial["adapters"]
            if adapter["adapter_key"] == "sec_filings"
        )
        config_version = sec["metadata"]["config_version"]
        repository = SourceMonitoringStateRepository(
            self.store,
            clock_ms=lambda: self.clock,
        )
        repository.get_or_create_state(
            "sec_filings",
            config_version=config_version,
        )
        repository.set_enabled(
            "sec_filings",
            config_version=config_version,
            enabled=True,
        )
        repository.start_run(
            "sec_filings",
            config_version=config_version,
        )

        snapshot = self.service().snapshot()
        sec_health = next(
            adapter for adapter in snapshot["adapters"]
            if adapter["adapter_key"] == "sec_filings"
        )

        self.assertEqual(snapshot["state"], "disabled")
        self.assertTrue(sec_health["persisted_enabled"])
        self.assertFalse(sec_health["enabled"])
        self.assertFalse(sec_health["running"])
        self.assertEqual(sec_health["state"], "disabled")
        self.assertEqual(sec_health["latest_run"]["status"], "RUNNING")
        self.assertFalse(sec_health["runtime_liveness_verified"])

    def test_source_mode_excludes_other_registered_class_from_effective_running(self) -> None:
        initial = self.service().snapshot()
        by_key = {adapter["adapter_key"]: adapter for adapter in initial["adapters"]}
        repository = SourceMonitoringStateRepository(
            self.store,
            clock_ms=lambda: self.clock,
        )
        for adapter_key in ("sec_filings", "futu_anomaly_signals"):
            config_version = by_key[adapter_key]["metadata"]["config_version"]
            repository.get_or_create_state(
                adapter_key,
                config_version=config_version,
            )
            repository.set_enabled(
                adapter_key,
                config_version=config_version,
                enabled=True,
            )
            repository.start_run(
                adapter_key,
                config_version=config_version,
            )

        snapshot = SourceMonitoringHealthService(
            self.store,
            clock_ms=lambda: self.clock,
            settings=SourceMonitoringSettings(enabled=True),
        ).snapshot()
        projected = {
            adapter["adapter_key"]: adapter for adapter in snapshot["adapters"]
        }

        self.assertEqual(snapshot["state"], "running")
        self.assertTrue(projected["sec_filings"]["enabled"])
        self.assertTrue(projected["sec_filings"]["running"])
        self.assertEqual(projected["sec_filings"]["state"], "running")
        self.assertTrue(projected["futu_anomaly_signals"]["persisted_enabled"])
        self.assertFalse(projected["futu_anomaly_signals"]["enabled"])
        self.assertFalse(projected["futu_anomaly_signals"]["running"])
        self.assertEqual(projected["futu_anomaly_signals"]["state"], "disabled")

    def test_migration_required_state_is_not_effectively_enabled(self) -> None:
        repository = SourceMonitoringStateRepository(
            self.store,
            clock_ms=lambda: self.clock,
        )
        repository.get_or_create_state(
            "sec_filings",
            config_version="retired-config-v0",
        )
        repository.set_enabled(
            "sec_filings",
            config_version="retired-config-v0",
            enabled=True,
        )

        snapshot = SourceMonitoringHealthService(
            self.store,
            clock_ms=lambda: self.clock,
            settings=SourceMonitoringSettings(enabled=True),
        ).snapshot()
        sec_health = next(
            adapter for adapter in snapshot["adapters"]
            if adapter["adapter_key"] == "sec_filings"
        )

        self.assertEqual(sec_health["config_status"], "migration_required")
        self.assertTrue(sec_health["persisted_enabled"])
        self.assertFalse(sec_health["enabled"])
        self.assertEqual(sec_health["state"], "disabled")

    def test_non_schema_sqlite_read_failure_is_bounded(self) -> None:
        broken_path = Path(self.temp_dir.name) / "not-a-database.sqlite3"
        broken_path.write_bytes(b"fixture disk I/O detail must remain bounded")
        with self.assertRaises(SourceMonitoringHealthServiceError) as raised:
            self.service(_OldStore(broken_path)).snapshot()

        self.assertEqual(raised.exception.code, "SOURCE_MONITORING_HEALTH_READ_FAILED")
        self.assertEqual(raised.exception.status, 409)
        self.assertNotIn("file is not a database", str(raised.exception).lower())

    def test_live_wal_snapshot_is_read_from_copy_without_touching_sidecars(self) -> None:
        wal_path = Path(f"{self.database_path}-wal")
        shm_path = Path(f"{self.database_path}-shm")
        initial = self.service().snapshot()
        sec = next(
            adapter for adapter in initial["adapters"]
            if adapter["adapter_key"] == "sec_filings"
        )
        reader = sqlite3.connect(self.database_path)
        try:
            reader.execute("PRAGMA journal_mode=WAL")
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM source_adapter_states").fetchone()
            repository = SourceMonitoringStateRepository(self.store)
            repository.get_or_create_state(
                "sec_filings",
                config_version=sec["metadata"]["config_version"],
            )
            self.assertTrue(wal_path.is_file())
            self.assertTrue(shm_path.is_file())
            before = {
                path.name: path.read_bytes()
                for path in (self.database_path, wal_path, shm_path)
            }

            snapshot = self.service().snapshot()
            projected = next(
                adapter for adapter in snapshot["adapters"]
                if adapter["adapter_key"] == "sec_filings"
            )
            after = {
                path.name: path.read_bytes()
                for path in (self.database_path, wal_path, shm_path)
            }
        finally:
            reader.close()

        self.assertTrue(projected["persisted_state"])
        self.assertEqual(after, before)

    def test_live_rollback_journal_fails_closed_without_touching_sidecar(self) -> None:
        journal_path = Path(f"{self.database_path}-journal")
        self.assertFalse(journal_path.exists())
        journal_path.write_bytes(b"fixture-live-journal")

        with self.assertRaises(SourceMonitoringHealthServiceError) as raised:
            self.service().snapshot()

        self.assertEqual(
            raised.exception.code,
            "SOURCE_MONITORING_HEALTH_SNAPSHOT_BUSY",
        )
        self.assertEqual(journal_path.read_bytes(), b"fixture-live-journal")

    def test_sealed_state_corruption_preserves_state_error(self) -> None:
        initial = self.service().snapshot()
        sec = next(
            adapter for adapter in initial["adapters"]
            if adapter["adapter_key"] == "sec_filings"
        )
        repository = SourceMonitoringStateRepository(self.store)
        repository.get_or_create_state(
            "sec_filings",
            config_version=sec["metadata"]["config_version"],
        )
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute(
                """UPDATE source_adapter_states
                      SET checkpoint_sha256=?
                    WHERE adapter_key='sec_filings'""",
                ("0" * 64,),
            )

        with self.assertRaises(SourceMonitoringStateError) as raised:
            self.service().snapshot()

        self.assertEqual(raised.exception.code, "SOURCE_MONITORING_RECORD_CORRUPT")

    def test_old_database_without_monitoring_schema_is_read_without_writes(self) -> None:
        old_path = Path(self.temp_dir.name) / "old.sqlite3"
        with closing(sqlite3.connect(old_path)) as connection, connection:
            connection.execute("CREATE TABLE legacy_marker(value TEXT NOT NULL)")
            connection.execute("INSERT INTO legacy_marker(value) VALUES('preserve-me')")
        before = old_path.stat()
        sidecars_before = sorted(path.name for path in old_path.parent.iterdir())

        snapshot = self.service(_OldStore(old_path)).snapshot()
        after = old_path.stat()
        sidecars_after = sorted(path.name for path in old_path.parent.iterdir())
        with closing(sqlite3.connect(old_path)) as connection:
            rows = connection.execute("SELECT value FROM legacy_marker").fetchall()
            monitoring_tables = connection.execute(
                """SELECT name FROM sqlite_master
                   WHERE type='table' AND name LIKE 'source_adapter_%'"""
            ).fetchall()

        self.assertFalse(snapshot["persistence_available"])
        self.assertEqual(snapshot["operations"]["schema_status"], "unavailable")
        self.assertEqual(snapshot["adapter_count"], 7)
        self.assertTrue(all(not adapter["persisted_state"] for adapter in snapshot["adapters"]))
        self.assertEqual(rows, [("preserve-me",)])
        self.assertEqual(monitoring_tables, [])
        self.assertEqual(
            (after.st_size, after.st_mtime_ns),
            (before.st_size, before.st_mtime_ns),
        )
        self.assertEqual(sidecars_after, sidecars_before)


if __name__ == "__main__":
    unittest.main()
