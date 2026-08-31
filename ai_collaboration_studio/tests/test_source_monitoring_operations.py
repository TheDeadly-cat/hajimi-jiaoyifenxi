from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.source_inbox_service import SourceInboxService  # noqa: E402
from backend.source_monitoring.operations import (  # noqa: E402
    SOURCE_MONITORING_OPERATIONS_MIGRATION_KEY,
    SOURCE_MONITORING_RETENTION_CONFIRMATION,
    SOURCE_MONITORING_RETENTION_POLICY_VERSION,
    SourceMonitoringOperationsError,
    SourceMonitoringRetentionService,
    ensure_source_monitoring_operations_schema,
    source_monitoring_operations_health,
)
from backend.source_monitoring.state_repository import (  # noqa: E402
    SourceMonitoringStateRepository,
)
from backend.store import StudioStore  # noqa: E402
from tests.test_source_inbox_contracts import RECEIVED_AT_MS, _packet  # noqa: E402


class SourceMonitoringOperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-source-monitor-operations-"
        )
        self.database_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.store = StudioStore(self.database_path)
        self.clock = RECEIVED_AT_MS + 50_000
        self.events: list[tuple[str, str, dict]] = []
        self.service = SourceMonitoringRetentionService(
            self.store,
            clock_ms=lambda: self.clock,
            id_factory=lambda: "source_retention_0123456789abcdef0123456789abcdef",
            event_sink=lambda event, *, severity, fields: self.events.append(
                (event, severity, fields)
            ),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _import_fixture(self) -> None:
        SourceInboxService(
            self.store,
            clock=lambda: RECEIVED_AT_MS / 1_000,
        ).import_packet(json.dumps(_packet(), ensure_ascii=False))

    def _protected_rows(self) -> dict[str, list[tuple]]:
        tables = (
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
        with closing(sqlite3.connect(self.database_path)) as connection:
            return {
                table: connection.execute(
                    f"SELECT * FROM {table} ORDER BY rowid"
                ).fetchall()
                for table in tables
            }

    def test_preview_is_read_only_sealed_and_retain_all(self) -> None:
        self._import_fixture()
        before_bytes = self.database_path.read_bytes()
        before_stat = self.database_path.stat()
        before_family = sorted(path.name for path in self.database_path.parent.iterdir())

        preview = self.service.preview()

        after_stat = self.database_path.stat()
        self.assertEqual(self.database_path.read_bytes(), before_bytes)
        self.assertEqual(
            (after_stat.st_size, after_stat.st_mtime_ns),
            (before_stat.st_size, before_stat.st_mtime_ns),
        )
        self.assertEqual(
            sorted(path.name for path in self.database_path.parent.iterdir()),
            before_family,
        )
        self.assertEqual(
            preview["policy"]["version"],
            SOURCE_MONITORING_RETENTION_POLICY_VERSION,
        )
        self.assertEqual(preview["policy"]["mode"], "retain_all_evidence")
        self.assertFalse(preview["policy"]["evidence_deletion_allowed"])
        self.assertEqual(preview["plan"]["eligible_rows"], 0)
        self.assertEqual(preview["plan"]["deleted_rows"], 0)
        self.assertEqual(preview["safety"]["retention_receipts_appended"], 0)
        self.assertEqual(
            preview["inventory"]["table_rows"]["source_inbox_imports"],
            1,
        )
        self.assertGreater(
            preview["inventory"]["retained_normalized_packet_and_receipt_bytes"],
            0,
        )
        self.assertEqual(
            self.events,
            [(
                "source_monitoring_retention_previewed",
                "info",
                {
                    "policy_version": SOURCE_MONITORING_RETENTION_POLICY_VERSION,
                    "policy_sha256": preview["policy_sha256"],
                    "eligible_rows": 0,
                    "deleted_rows": 0,
                },
            )],
        )

    def test_attestation_is_append_only_zero_delete_and_idempotent(self) -> None:
        self._import_fixture()
        repository = SourceMonitoringStateRepository(
            self.store,
            clock_ms=lambda: self.clock,
        )
        repository.get_or_create_state(
            "fixture_adapter",
            config_version="fixture_config_v1",
        )
        protected_before = self._protected_rows()
        preview = self.service.preview()
        self.clock += 1

        first = self.service.attest(
            preview,
            confirmation=SOURCE_MONITORING_RETENTION_CONFIRMATION,
        )
        replay = self.service.attest(
            preview,
            confirmation=SOURCE_MONITORING_RETENTION_CONFIRMATION,
        )

        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(first["receipt"], replay["receipt"])
        self.assertEqual(first["receipt"]["decision"], "RETAIN_ALL")
        self.assertEqual(first["receipt"]["eligible_rows"], 0)
        self.assertEqual(first["receipt"]["deleted_rows"], 0)
        self.assertEqual(first["receipt"]["source_rows_updated"], 0)
        self.assertEqual(
            first["receipt"]["safety"]["retention_receipts_appended"],
            1,
        )
        self.assertEqual(self._protected_rows(), protected_before)
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM source_monitoring_retention_receipts"
                ).fetchone()[0],
                1,
            )
        attested_events = [event for event in self.events if event[0].endswith("attested")]
        self.assertEqual(len(attested_events), 2)
        self.assertFalse(attested_events[0][2]["idempotent_replay"])
        self.assertTrue(attested_events[1][2]["idempotent_replay"])
        self.assertNotIn("receipt_id", attested_events[0][2])
        self.assertNotIn("inventory", attested_events[0][2])

    def test_stale_or_tampered_preview_and_wrong_confirmation_fail_closed(self) -> None:
        preview = self.service.preview()
        with self.assertRaises(SourceMonitoringOperationsError) as captured:
            self.service.attest(preview, confirmation="DELETE_OLD_ROWS")
        self.assertEqual(
            captured.exception.code,
            "SOURCE_MONITORING_RETENTION_CONFIRMATION_REQUIRED",
        )

        tampered = copy.deepcopy(preview)
        tampered["plan"]["deleted_rows"] = 1
        with self.assertRaises(SourceMonitoringOperationsError) as captured:
            self.service.attest(
                tampered,
                confirmation=SOURCE_MONITORING_RETENTION_CONFIRMATION,
            )
        self.assertEqual(
            captured.exception.code,
            "SOURCE_MONITORING_RETENTION_PREVIEW_INVALID",
        )

        SourceMonitoringStateRepository(
            self.store,
            clock_ms=lambda: self.clock,
        ).get_or_create_state(
            "fixture_stale",
            config_version="fixture_config_v1",
        )
        with self.assertRaises(SourceMonitoringOperationsError) as captured:
            self.service.attest(
                preview,
                confirmation=SOURCE_MONITORING_RETENTION_CONFIRMATION,
            )
        self.assertEqual(
            captured.exception.code,
            "SOURCE_MONITORING_RETENTION_PREVIEW_STALE",
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM source_monitoring_retention_receipts"
                ).fetchone()[0],
                0,
            )

    def test_deep_untrusted_preview_returns_bounded_contract_error(self) -> None:
        preview = self.service.preview()
        nested: object = {}
        for _index in range(600):
            nested = {"nested": nested}
        preview["inventory"] = nested

        with self.assertRaises(SourceMonitoringOperationsError) as captured:
            self.service.attest(
                preview,
                confirmation=SOURCE_MONITORING_RETENTION_CONFIRMATION,
            )

        self.assertEqual(
            captured.exception.code,
            "SOURCE_MONITORING_RETENTION_PREVIEW_INVALID",
        )
        self.assertEqual(captured.exception.status, 400)

    def test_receipt_and_operations_marker_are_immutable(self) -> None:
        preview = self.service.preview()
        self.clock += 1
        self.service.attest(
            preview,
            confirmation=SOURCE_MONITORING_RETENTION_CONFIRMATION,
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            for statement in (
                "UPDATE source_monitoring_retention_receipts SET deleted_rows=0",
                "DELETE FROM source_monitoring_retention_receipts",
                (
                    "INSERT OR REPLACE INTO source_monitoring_retention_receipts "
                    "SELECT * FROM source_monitoring_retention_receipts"
                ),
                (
                    "UPDATE schema_migrations SET applied_at=applied_at "
                    f"WHERE key='{SOURCE_MONITORING_OPERATIONS_MIGRATION_KEY}'"
                ),
                (
                    "DELETE FROM schema_migrations "
                    f"WHERE key='{SOURCE_MONITORING_OPERATIONS_MIGRATION_KEY}'"
                ),
                (
                    "INSERT OR REPLACE INTO schema_migrations(key,applied_at) "
                    f"VALUES('{SOURCE_MONITORING_OPERATIONS_MIGRATION_KEY}',999)"
                ),
            ):
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(statement)
                connection.rollback()
            marker_rowid = connection.execute(
                "SELECT rowid FROM schema_migrations WHERE key=?",
                (SOURCE_MONITORING_OPERATIONS_MIGRATION_KEY,),
            ).fetchone()[0]
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT OR REPLACE INTO schema_migrations(
                           rowid,key,applied_at
                       ) VALUES(?,?,?)""",
                    (marker_rowid, "replacement_marker", 999),
                )
            connection.rollback()

    def test_logging_failure_cannot_change_attestation_result(self) -> None:
        service = SourceMonitoringRetentionService(
            self.store,
            clock_ms=lambda: self.clock,
            id_factory=lambda: "source_retention_fedcba9876543210fedcba9876543210",
            event_sink=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("fixture log sink failure")
            ),
        )
        preview = service.preview()
        self.clock += 1
        result = service.attest(
            preview,
            confirmation=SOURCE_MONITORING_RETENTION_CONFIRMATION,
        )
        self.assertFalse(result["idempotent_replay"])
        self.assertEqual(result["receipt"]["deleted_rows"], 0)

    def test_new_attestations_require_strictly_monotonic_receipt_time(self) -> None:
        clock = [self.clock]
        identities = iter((
            "source_retention_11111111111111111111111111111111",
            "source_retention_22222222222222222222222222222222",
        ))
        service = SourceMonitoringRetentionService(
            self.store,
            clock_ms=lambda: clock[0],
            id_factory=lambda: next(identities),
            event_sink=lambda *_args, **_kwargs: None,
        )
        first_preview = service.preview()
        clock[0] += 1
        first = service.attest(
            first_preview,
            confirmation=SOURCE_MONITORING_RETENTION_CONFIRMATION,
        )
        clock[0] = first_preview["captured_at_ms"] - 1
        replay = service.attest(
            first_preview,
            confirmation=SOURCE_MONITORING_RETENTION_CONFIRMATION,
        )
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["receipt"], first["receipt"])
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM source_monitoring_retention_receipts"
                ).fetchone()[0],
                1,
            )

        clock[0] = first["receipt"]["attested_at_ms"]
        second_preview = service.preview()

        with self.assertRaises(SourceMonitoringOperationsError) as equal_time:
            service.attest(
                second_preview,
                confirmation=SOURCE_MONITORING_RETENTION_CONFIRMATION,
            )
        self.assertEqual(
            equal_time.exception.code,
            "SOURCE_MONITORING_RETENTION_CLOCK_INVALID",
        )

        clock[0] -= 2
        rollback_preview = service.preview()
        with self.assertRaises(SourceMonitoringOperationsError) as rolled_back:
            service.attest(
                rollback_preview,
                confirmation=SOURCE_MONITORING_RETENTION_CONFIRMATION,
            )
        self.assertEqual(
            rolled_back.exception.code,
            "SOURCE_MONITORING_RETENTION_CLOCK_INVALID",
        )

        clock[0] += 3
        second = service.attest(
            second_preview,
            confirmation=SOURCE_MONITORING_RETENTION_CONFIRMATION,
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            health = source_monitoring_operations_health(connection)
        self.assertEqual(health["retention_receipt_count"], 2)
        self.assertEqual(
            health["latest_retention_receipt_sha256"],
            second["receipt"]["receipt_sha256"],
        )
        self.assertNotEqual(
            second["receipt"]["receipt_sha256"],
            first["receipt"]["receipt_sha256"],
        )

    def test_health_projects_current_schema_without_attesting(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            before = connection.execute(
                "SELECT COUNT(*) FROM source_monitoring_retention_receipts"
            ).fetchone()[0]
            health = source_monitoring_operations_health(connection)
            after = connection.execute(
                "SELECT COUNT(*) FROM source_monitoring_retention_receipts"
            ).fetchone()[0]
        self.assertEqual(health["schema_status"], "current")
        self.assertEqual(health["retention_mode"], "retain_all_evidence")
        self.assertFalse(health["evidence_deletion_allowed"])
        self.assertFalse(health["runtime_liveness_verified"])
        self.assertEqual((before, after), (0, 0))

    def test_unmarked_same_name_schema_fails_closed_without_marker(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.execute(
            "CREATE TABLE schema_migrations(key TEXT PRIMARY KEY,applied_at INTEGER)"
        )
        connection.execute(
            "CREATE TABLE source_monitoring_retention_receipts(id TEXT PRIMARY KEY)"
        )
        with self.assertRaises(SourceMonitoringOperationsError) as captured:
            ensure_source_monitoring_operations_schema(
                connection,
                applied_at_ms=1,
            )
        self.assertEqual(
            captured.exception.code,
            "SOURCE_MONITORING_OPERATIONS_SCHEMA_INVALID",
        )
        self.assertIsNone(connection.execute(
            "SELECT 1 FROM schema_migrations WHERE key=?",
            (SOURCE_MONITORING_OPERATIONS_MIGRATION_KEY,),
        ).fetchone())

    def test_marked_same_name_noop_trigger_is_not_accepted_as_current(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.executescript(
                """
                DROP TRIGGER trg_source_monitoring_retention_receipts_no_delete;
                CREATE TRIGGER trg_source_monitoring_retention_receipts_no_delete
                BEFORE DELETE ON source_monitoring_retention_receipts
                BEGIN
                    SELECT 1;
                END;
                """
            )
            with self.assertRaises(SourceMonitoringOperationsError) as captured:
                source_monitoring_operations_health(connection)
        self.assertEqual(
            captured.exception.code,
            "SOURCE_MONITORING_OPERATIONS_SCHEMA_INVALID",
        )

    def test_schema_identity_does_not_casefold_quoted_marker_literal(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.executescript(
                """
                DROP TRIGGER trg_source_monitoring_operations_marker_no_replace;
                CREATE TRIGGER trg_source_monitoring_operations_marker_no_replace
                BEFORE INSERT ON schema_migrations
                WHEN EXISTS (
                    SELECT 1 FROM schema_migrations existing
                     WHERE existing.key='SOURCE_MONITORING_OPERATIONS_V1'
                       AND (
                           existing.rowid=NEW.rowid
                           OR NEW.key='SOURCE_MONITORING_OPERATIONS_V1'
                       )
                )
                BEGIN
                    SELECT RAISE(ABORT,'source monitoring operations marker is immutable');
                END;
                """
            )
            with self.assertRaises(SourceMonitoringOperationsError) as captured:
                source_monitoring_operations_health(connection)
        self.assertEqual(
            captured.exception.code,
            "SOURCE_MONITORING_OPERATIONS_SCHEMA_INVALID",
        )

    def test_deep_corrupt_latest_receipt_is_bounded_as_record_corruption(self) -> None:
        receipt_json = ("[" * 2_000) + ("0") + ("]" * 2_000)
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute(
                """INSERT INTO source_monitoring_retention_receipts(
                       id,record_version,policy_version,decision,
                       policy_sha256,preview_sha256,inventory_sha256,
                       eligible_rows,deleted_rows,source_rows_updated,
                       receipt_json,receipt_sha256,attested_at_ms
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "source_retention_33333333333333333333333333333333",
                    "source_monitoring_retention_receipt_v1",
                    "source_monitoring_retention_policy_v1",
                    "RETAIN_ALL",
                    "0" * 64,
                    "1" * 64,
                    "2" * 64,
                    0,
                    0,
                    0,
                    receipt_json,
                    "3" * 64,
                    self.clock,
                ),
            )
            with self.assertRaises(SourceMonitoringOperationsError) as captured:
                source_monitoring_operations_health(connection)
        self.assertEqual(
            captured.exception.code,
            "SOURCE_MONITORING_RETENTION_RECEIPT_CORRUPT",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
