from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.database_migration import (  # noqa: E402
    DatabaseMigrationError,
    DatabaseMigrationRecoveryRequired,
    DatabaseMigrationRequired,
    apply_authorized_migration,
    assert_database_ready_for_startup,
    build_migration_manifest,
    prepare_migration,
    recover_pending_database_migration,
    write_migration_manifest,
)
from backend.source_inbox_service import SourceInboxService  # noqa: E402
from backend.source_monitoring.operations import (  # noqa: E402
    SOURCE_MONITORING_OPERATIONS_MIGRATION_KEY,
)
from backend.source_monitoring.state_repository import (  # noqa: E402
    RUN_STATUS_DRY_RUN,
    SourceMonitoringStateRepository,
)
from backend.store import StudioStore  # noqa: E402
from tests.test_source_inbox_contracts import RECEIVED_AT_MS, _packet  # noqa: E402


EXPECTED_OPERATIONS_SCHEMA_OBJECTS = {
    ("table", "source_monitoring_retention_receipts"),
    ("index", "idx_source_monitoring_retention_receipts_time"),
    ("trigger", "trg_source_monitoring_retention_receipts_no_update"),
    ("trigger", "trg_source_monitoring_retention_receipts_no_delete"),
    ("trigger", "trg_source_monitoring_retention_receipts_no_replace"),
    ("trigger", "trg_source_monitoring_operations_marker_no_update"),
    ("trigger", "trg_source_monitoring_operations_marker_no_delete"),
    ("trigger", "trg_source_monitoring_operations_marker_no_replace"),
}

PROTECTED_TABLES = (
    "source_adapter_runs",
    "source_adapter_states",
    "source_inbox_attachments",
    "source_inbox_import_items",
    "source_inbox_imports",
    "source_inbox_items",
    "source_inbox_round_drafts",
    "source_inbox_state_events",
    "source_inbox_trading_impact_projections",
)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _checkpoint(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _table_content(path: Path, table: str) -> str:
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = [dict(row) for row in connection.execute(
            f"SELECT * FROM {table} ORDER BY rowid"
        ).fetchall()]
    encoded = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class SourceMonitoringOperationsMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-source-monitor-operations-migration-"
        )
        self.root = Path(self.temp_dir.name).resolve()
        self.database_path = self.root / "source.sqlite3"
        self.clock = RECEIVED_AT_MS + 100_000
        self.store = StudioStore(self.database_path)
        self.store.create_room(
            "Phase 8 migration fixture",
            "Existing monitoring and Source Inbox evidence must remain unchanged.",
            capability_pack_ids=[],
        )
        SourceInboxService(
            self.store,
            clock=lambda: RECEIVED_AT_MS / 1_000,
        ).import_packet(json.dumps(_packet(), ensure_ascii=False))
        repository = SourceMonitoringStateRepository(
            self.store,
            clock_ms=lambda: self.clock,
        )
        repository.get_or_create_state(
            "phase8_fixture",
            config_version="phase8_fixture_config_v1",
        )
        repository.set_enabled(
            "phase8_fixture",
            config_version="phase8_fixture_config_v1",
            enabled=True,
        )
        started = repository.start_run(
            "phase8_fixture",
            config_version="phase8_fixture_config_v1",
            dry_run=True,
        )
        self.clock += 1
        repository.complete_run(
            started["run"]["run_id"],
            next_checkpoint={"cursor": 1},
            status=RUN_STATUS_DRY_RUN,
            observed_count=1,
            accepted_count=0,
            duplicate_count=0,
            rejected_count=0,
            next_due_at_ms=self.clock + 60_000,
        )
        self._remove_phase8_schema()
        _checkpoint(self.database_path)
        self.protected_before = {
            table: _table_content(self.database_path, table)
            for table in PROTECTED_TABLES
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _remove_phase8_schema(self) -> None:
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

    def _prepare(
        self,
        label: str,
        manifest: dict | None = None,
    ) -> tuple[dict, Path, Path, dict]:
        active_manifest = manifest or build_migration_manifest(self.database_path)
        manifest_path = write_migration_manifest(
            active_manifest,
            self.root / f"{label}-manifest.json",
        )
        prepared_path = self.root / f"{label}-prepared.json"
        prepared = prepare_migration(
            database_path=self.database_path,
            manifest_path=manifest_path,
            backup_path=self.root / f"{label}-backup.sqlite3",
            candidate_path=self.root / f"{label}-candidate.sqlite3",
            prepared_path=prepared_path,
        )
        return (
            prepared,
            prepared_path,
            self.root / f"{label}-receipt.json",
            active_manifest,
        )

    def _assert_protected_content_unchanged(self) -> None:
        self.assertEqual(
            {
                table: _table_content(self.database_path, table)
                for table in PROTECTED_TABLES
            },
            self.protected_before,
        )

    def test_preview_prepare_and_apply_are_exactly_additive(self) -> None:
        source_before_sha256 = _file_sha256(self.database_path)
        source_before_stat = self.database_path.stat()
        with self.assertRaises(DatabaseMigrationRequired) as required:
            assert_database_ready_for_startup(self.database_path)
        manifest = required.exception.manifest

        self.assertTrue(manifest["requires_migration"])
        self.assertEqual(_file_sha256(self.database_path), source_before_sha256)
        self.assertEqual(
            self.database_path.stat().st_mtime_ns,
            source_before_stat.st_mtime_ns,
        )
        additions = {
            (entry["type"], entry["name"])
            for entry in manifest["changes"]["schema_changes"]
            if entry["action"] == "add"
        }
        self.assertEqual(additions, EXPECTED_OPERATIONS_SCHEMA_OBJECTS)
        self.assertTrue(all(
            entry["action"] == "add"
            for entry in manifest["changes"]["schema_changes"]
        ))
        self.assertEqual(
            manifest["changes"]["migration_keys_added"],
            [SOURCE_MONITORING_OPERATIONS_MIGRATION_KEY],
        )
        self.assertEqual(manifest["changes"]["migration_keys_removed"], [])
        self.assertEqual(
            {entry["table"] for entry in manifest["changes"]["data_changes"]},
            {"schema_migrations", "source_monitoring_retention_receipts"},
        )
        for table in PROTECTED_TABLES:
            self.assertEqual(
                manifest["before"]["logical"]["tables"][table]["content_sha256"],
                manifest["projected_state"]["tables"][table]["content_sha256"],
            )
        self._assert_protected_content_unchanged()

        prepared, prepared_path, receipt_path, prepared_manifest = self._prepare(
            "apply",
            manifest,
        )
        self.assertEqual(prepared_manifest["plan_sha256"], manifest["plan_sha256"])
        backup_path = Path(prepared["backup"]["path"])
        self.assertEqual(_file_sha256(backup_path), source_before_sha256)
        self.assertEqual(_file_sha256(self.database_path), source_before_sha256)
        self.assertEqual(
            prepared["candidate"]["snapshot"]["logical"]["logical_sha256"],
            manifest["projected_state"]["logical_sha256"],
        )

        with self.assertRaisesRegex(DatabaseMigrationError, "authorization token"):
            apply_authorized_migration(
                database_path=self.database_path,
                prepared_path=prepared_path,
                authorization_token="wrong-token",
                receipt_path=receipt_path,
            )
        self.assertEqual(_file_sha256(self.database_path), source_before_sha256)

        receipt = apply_authorized_migration(
            database_path=self.database_path,
            prepared_path=prepared_path,
            authorization_token=str(prepared["authorization_token"]),
            receipt_path=receipt_path,
        )
        self.assertEqual(receipt["after"]["integrity_check"], ["ok"])
        self.assertEqual(receipt["after"]["foreign_key_violation_count"], 0)
        self.assertEqual(receipt["after"]["wal_size"], 0)
        self.assertTrue(receipt["after"]["matches_authorized_candidate"])
        self.assertEqual(
            receipt["after"]["sha256"],
            prepared["candidate"]["snapshot"]["file"]["sha256"],
        )
        readiness = assert_database_ready_for_startup(self.database_path)
        self.assertEqual(readiness["integrity_check"], ["ok"])
        self.assertEqual(readiness["foreign_key_violation_count"], 0)
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM source_monitoring_retention_receipts"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE key=?",
                    (SOURCE_MONITORING_OPERATIONS_MIGRATION_KEY,),
                ).fetchone()[0],
                1,
            )
        self._assert_protected_content_unchanged()
        self.assertFalse(build_migration_manifest(self.database_path)["requires_migration"])

    def test_pending_candidate_can_exactly_rollback_to_pre_phase8_image(self) -> None:
        prepared, prepared_path, receipt_path, manifest = self._prepare("rollback")
        source_before_sha256 = str(prepared["source"]["file"]["sha256"])
        source_before_logical_sha256 = str(
            prepared["source"]["logical"]["logical_sha256"]
        )
        candidate_sha256 = str(
            prepared["candidate"]["snapshot"]["file"]["sha256"]
        )
        with mock.patch(
            "backend.database_migration._write_json_exclusive",
            side_effect=OSError("injected Phase 8 receipt publication failure"),
        ):
            with self.assertRaises(DatabaseMigrationRecoveryRequired):
                apply_authorized_migration(
                    database_path=self.database_path,
                    prepared_path=prepared_path,
                    authorization_token=str(prepared["authorization_token"]),
                    receipt_path=receipt_path,
                )

        operation_id = str(prepared["prepared_sha256"])
        status = recover_pending_database_migration(
            database_path=self.database_path,
            operation_id=operation_id,
            action="inspect",
        )
        self.assertEqual(status["classification"], "CANDIDATE")
        self.assertEqual(_file_sha256(self.database_path), candidate_sha256)
        with self.assertRaisesRegex(DatabaseMigrationError, "authorization token"):
            recover_pending_database_migration(
                database_path=self.database_path,
                operation_id=operation_id,
                action="rollback",
                authorization_token="wrong-token",
            )
        result = recover_pending_database_migration(
            database_path=self.database_path,
            operation_id=operation_id,
            action="rollback",
            authorization_token=str(prepared["authorization_token"]),
        )

        self.assertEqual(result["outcome"], "rolled_back")
        self.assertEqual(_file_sha256(self.database_path), source_before_sha256)
        rolled_back_manifest = build_migration_manifest(
            self.database_path,
            migration_epoch_ms=manifest["migration_epoch_ms"],
        )
        self.assertTrue(rolled_back_manifest["requires_migration"])
        self.assertEqual(
            rolled_back_manifest["before"]["logical"]["logical_sha256"],
            source_before_logical_sha256,
        )
        self.assertEqual(
            rolled_back_manifest["plan_sha256"],
            manifest["plan_sha256"],
        )
        rollback_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(
            rollback_receipt["version"],
            "database_migration_rollback_receipt_v2",
        )
        self.assertEqual(
            rollback_receipt["restored_source_sha256"],
            source_before_sha256,
        )
        failed_candidate = Path(rollback_receipt["failed_candidate_path"])
        self.assertEqual(_file_sha256(failed_candidate), candidate_sha256)
        self._assert_protected_content_unchanged()
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertFalse(any(
            Path(f"{self.database_path}{suffix}").exists()
            for suffix in ("-wal", "-shm", "-journal")
        ))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
