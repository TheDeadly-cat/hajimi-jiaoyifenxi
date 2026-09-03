from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.source_monitoring.contracts import canonical_sha256  # noqa: E402
from backend.source_monitoring.state_repository import (  # noqa: E402
    RUN_STATUS_SUCCEEDED,
    SOURCE_MONITORING_INITIALIZATION_MIGRATION_KEY,
    SOURCE_MONITORING_INITIALIZATION_VERSION,
    SOURCE_MONITORING_PENDING_AUTHORIZATION_MIGRATION_KEY,
    SourceMonitoringStateError,
    SourceMonitoringStateRepository,
    ensure_source_monitoring_schema,
    source_monitoring_pending_authorization_schema_state,
)
from backend.store import StudioStore  # noqa: E402


INITIALIZATION_COLUMNS = {
    "initialization_mode",
    "initialization_config_version",
    "initialization_preview_sha256",
    "initialization_receipt_json",
    "initialization_receipt_sha256",
}
INITIALIZATION_OBJECTS = {
    "idx_source_adapter_runs_initialization_time",
    "uq_source_adapter_runs_initialization_receipt",
    "trg_source_adapter_runs_initialization_no_update",
    "trg_source_adapter_runs_initialization_no_delete",
    "trg_source_monitoring_initialization_marker_no_update",
    "trg_source_monitoring_initialization_marker_no_delete",
    "trg_source_monitoring_initialization_marker_no_replace",
}
PENDING_AUTHORIZATION_COLUMNS = {
    "pending_initialization_authorization_json",
    "pending_initialization_authorization_sha256",
}
PENDING_AUTHORIZATION_OBJECTS = {
    "trg_source_monitoring_pending_authorization_marker_no_update",
    "trg_source_monitoring_pending_authorization_marker_no_delete",
    "trg_source_monitoring_pending_authorization_marker_no_replace",
}


def _legacy_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE schema_migrations (
            key TEXT PRIMARY KEY,
            applied_at INTEGER NOT NULL
        );
        CREATE TABLE source_adapter_states (
            adapter_key TEXT PRIMARY KEY,
            record_version TEXT NOT NULL
                CHECK(record_version='source_adapter_state_v1'),
            enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
            config_version TEXT NOT NULL,
            checkpoint_json TEXT NOT NULL DEFAULT '{}',
            checkpoint_sha256 TEXT NOT NULL CHECK(length(checkpoint_sha256)=64),
            etag TEXT NOT NULL DEFAULT '',
            last_modified TEXT NOT NULL DEFAULT '',
            last_started_at_ms INTEGER NOT NULL DEFAULT 0 CHECK(last_started_at_ms>=0),
            last_success_at_ms INTEGER NOT NULL DEFAULT 0 CHECK(last_success_at_ms>=0),
            last_event_at_ms INTEGER NOT NULL DEFAULT 0 CHECK(last_event_at_ms>=0),
            next_due_at_ms INTEGER NOT NULL DEFAULT 0 CHECK(next_due_at_ms>=0),
            consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK(consecutive_failures>=0),
            last_error_code TEXT NOT NULL DEFAULT '',
            last_error_message TEXT NOT NULL DEFAULT '',
            state_version INTEGER NOT NULL DEFAULT 1 CHECK(state_version>0),
            updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=0)
        );
        CREATE TABLE source_adapter_runs (
            run_id TEXT PRIMARY KEY,
            record_version TEXT NOT NULL
                CHECK(record_version='source_adapter_run_v1'),
            adapter_key TEXT NOT NULL,
            started_state_version INTEGER NOT NULL CHECK(started_state_version>0),
            started_checkpoint_json TEXT NOT NULL,
            started_checkpoint_sha256 TEXT NOT NULL CHECK(length(started_checkpoint_sha256)=64),
            next_checkpoint_json TEXT NOT NULL DEFAULT '{}',
            next_checkpoint_sha256 TEXT NOT NULL DEFAULT '',
            started_at_ms INTEGER NOT NULL CHECK(started_at_ms>=0),
            completed_at_ms INTEGER NOT NULL DEFAULT 0 CHECK(completed_at_ms>=0),
            status TEXT NOT NULL CHECK(status IN (
                'RUNNING','SUCCEEDED','DEGRADED','FAILED','DRY_RUN','ABANDONED'
            )),
            observed_count INTEGER NOT NULL DEFAULT 0 CHECK(observed_count>=0),
            accepted_count INTEGER NOT NULL DEFAULT 0 CHECK(accepted_count>=0),
            duplicate_count INTEGER NOT NULL DEFAULT 0 CHECK(duplicate_count>=0),
            rejected_count INTEGER NOT NULL DEFAULT 0 CHECK(rejected_count>=0),
            duration_ms INTEGER NOT NULL DEFAULT 0 CHECK(duration_ms>=0),
            error_code TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            source_errors_json TEXT NOT NULL DEFAULT '[]',
            receipt_id TEXT NOT NULL DEFAULT '',
            dry_run INTEGER NOT NULL DEFAULT 0 CHECK(dry_run IN (0,1)),
            FOREIGN KEY(adapter_key) REFERENCES source_adapter_states(adapter_key)
        );
        INSERT INTO schema_migrations(key,applied_at)
        VALUES('source_monitoring_state_v1',1);
        """
    )
    return connection


class SourceMonitoringInitializationPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-source-monitor-init-receipt-"
        )
        self.database_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.clock = [1_900_000_000_000]
        self.store = StudioStore(self.database_path)
        self.repository = SourceMonitoringStateRepository(
            self.store,
            clock_ms=lambda: self.clock[0],
        )
        self.adapter_key = "fake_official"
        self.config_version = "fake_official_config_v1"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _start(self) -> dict:
        self.repository.set_enabled(
            self.adapter_key,
            config_version=self.config_version,
            enabled=True,
        )
        return self.repository.start_run(
            self.adapter_key,
            config_version=self.config_version,
        )["run"]

    def _initialization(
        self,
        run: dict,
        next_checkpoint: dict,
        *,
        candidate_count: int = 2,
    ) -> dict:
        return {
            "version": SOURCE_MONITORING_INITIALIZATION_VERSION,
            "mode": "seed_only",
            "config_version": self.config_version,
            "preview_sha256": "a" * 64,
            "candidate_count": candidate_count,
            "adapter_duplicate_count": 0,
            "selected_count": 0,
            "skipped_count": candidate_count,
            "catch_up_max_items": 0,
            "from_time_ms": 0,
            "earliest_occurred_at": (
                "2026-08-31T04:00:00Z" if candidate_count else ""
            ),
            "latest_occurred_at": (
                "2026-08-31T05:00:00.000Z" if candidate_count else ""
            ),
            "starting_checkpoint_sha256": run["started_checkpoint_sha256"],
            "next_checkpoint_sha256": canonical_sha256(next_checkpoint),
            "captured_at_ms": self.clock[0],
        }

    def _complete_seed(self, run: dict, next_checkpoint: dict | None = None) -> dict:
        checkpoint = next_checkpoint or {"cursor": 2}
        self.clock[0] += 10
        return self.repository.complete_run(
            run["run_id"],
            next_checkpoint=checkpoint,
            status=RUN_STATUS_SUCCEEDED,
            observed_count=2,
            accepted_count=0,
            duplicate_count=0,
            rejected_count=0,
            next_due_at_ms=self.clock[0] + 60_000,
            initialization=self._initialization(run, checkpoint),
        )

    def test_fresh_schema_has_constrained_columns_and_immutable_objects(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            columns = {
                row[1]: row for row in connection.execute(
                    "PRAGMA table_info(source_adapter_runs)"
                )
            }
            objects = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE name LIKE '%initialization%'"
                )
            }
            marker = connection.execute(
                "SELECT applied_at FROM schema_migrations WHERE key=?",
                (SOURCE_MONITORING_INITIALIZATION_MIGRATION_KEY,),
            ).fetchone()

        self.assertEqual(set(columns).intersection(INITIALIZATION_COLUMNS), INITIALIZATION_COLUMNS)
        for name in INITIALIZATION_COLUMNS:
            self.assertEqual(columns[name][2], "TEXT")
            self.assertEqual(columns[name][3], 1)
            self.assertEqual(columns[name][4], "''")
        self.assertTrue(INITIALIZATION_OBJECTS.issubset(objects))
        self.assertIsNotNone(marker)

        with closing(sqlite3.connect(self.database_path)) as connection:
            state_columns = {
                row[1]: row for row in connection.execute(
                    "PRAGMA table_info(source_adapter_states)"
                )
            }
            pending_objects = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE name LIKE '%pending_authorization%'"
                )
            }
            pending_marker = connection.execute(
                "SELECT applied_at FROM schema_migrations WHERE key=?",
                (SOURCE_MONITORING_PENDING_AUTHORIZATION_MIGRATION_KEY,),
            ).fetchone()
            self.assertEqual(
                source_monitoring_pending_authorization_schema_state(connection),
                "current",
            )
        self.assertEqual(
            set(state_columns).intersection(PENDING_AUTHORIZATION_COLUMNS),
            PENDING_AUTHORIZATION_COLUMNS,
        )
        self.assertTrue(PENDING_AUTHORIZATION_OBJECTS.issubset(pending_objects))
        self.assertIsNotNone(pending_marker)

        run = self._start()
        with closing(sqlite3.connect(self.database_path)) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE source_adapter_runs SET initialization_mode='bogus' WHERE run_id=?",
                    (run["run_id"],),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE source_adapter_runs
                       SET initialization_mode='seed_only' WHERE run_id=?""",
                    (run["run_id"],),
                )

    def test_current_schema_rejects_missing_or_redefined_objects(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute(
                "DROP TRIGGER trg_source_adapter_runs_initialization_no_delete"
            )
            with self.assertRaises(SourceMonitoringStateError) as invalid:
                ensure_source_monitoring_schema(connection, applied_at_ms=2)
        self.assertEqual(
            invalid.exception.code,
            "SOURCE_MONITORING_INITIALIZATION_SCHEMA_INVALID",
        )

    def test_legacy_schema_migration_is_additive_and_preserves_rows(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy.sqlite3"
        with closing(_legacy_connection(legacy_path)) as connection:
            empty_sha = canonical_sha256({})
            connection.execute(
                """INSERT INTO source_adapter_states(
                       adapter_key,record_version,config_version,
                       checkpoint_json,checkpoint_sha256,updated_at_ms
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    "legacy",
                    "source_adapter_state_v1",
                    "legacy_config_v1",
                    "{}",
                    empty_sha,
                    1,
                ),
            )
            connection.commit()
            ensure_source_monitoring_schema(connection, applied_at_ms=2)
            row = connection.execute(
                "SELECT * FROM source_adapter_states WHERE adapter_key='legacy'"
            ).fetchone()
            run_columns = {
                item[1] for item in connection.execute(
                    "PRAGMA table_info(source_adapter_runs)"
                )
            }
            marker = connection.execute(
                "SELECT applied_at FROM schema_migrations WHERE key=?",
                (SOURCE_MONITORING_INITIALIZATION_MIGRATION_KEY,),
            ).fetchone()
            state_columns = {
                item[1] for item in connection.execute(
                    "PRAGMA table_info(source_adapter_states)"
                )
            }
            pending_marker = connection.execute(
                "SELECT applied_at FROM schema_migrations WHERE key=?",
                (SOURCE_MONITORING_PENDING_AUTHORIZATION_MIGRATION_KEY,),
            ).fetchone()

        self.assertIsNotNone(row)
        self.assertTrue(INITIALIZATION_COLUMNS.issubset(run_columns))
        self.assertEqual(marker[0], 2)
        self.assertTrue(PENDING_AUTHORIZATION_COLUMNS.issubset(state_columns))
        self.assertEqual(pending_marker[0], 2)

    def test_pending_authorization_migration_rolls_back_its_partial_unit(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "pending-atomic.sqlite3"
        with closing(_legacy_connection(legacy_path)) as connection:
            def deny_pending_trigger(action, arg1, _arg2, _database, _source):
                if (
                    action == sqlite3.SQLITE_CREATE_TRIGGER
                    and "pending_authorization" in str(arg1 or "")
                ):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(deny_pending_trigger)
            with self.assertRaises(sqlite3.DatabaseError):
                ensure_source_monitoring_schema(connection, applied_at_ms=2)
            connection.set_authorizer(None)
            state_columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(source_adapter_states)"
                )
            }
            marker = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE key=?",
                (SOURCE_MONITORING_PENDING_AUTHORIZATION_MIGRATION_KEY,),
            ).fetchone()
            objects = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE name LIKE '%pending_authorization%'"
                )
            }
        self.assertFalse(PENDING_AUTHORIZATION_COLUMNS.intersection(state_columns))
        self.assertFalse(PENDING_AUTHORIZATION_OBJECTS.intersection(objects))
        self.assertIsNone(marker)

    def test_incremental_migration_rolls_back_all_objects_on_failure(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "atomic.sqlite3"
        with closing(_legacy_connection(legacy_path)) as connection:
            def deny_trigger(action, _arg1, _arg2, _database, _source):
                if action == sqlite3.SQLITE_CREATE_TRIGGER:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(deny_trigger)
            with self.assertRaises(sqlite3.DatabaseError):
                ensure_source_monitoring_schema(connection, applied_at_ms=2)
            connection.set_authorizer(None)
            columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(source_adapter_runs)"
                )
            }
            objects = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE name LIKE '%initialization%'"
                )
            }
            marker = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE key=?",
                (SOURCE_MONITORING_INITIALIZATION_MIGRATION_KEY,),
            ).fetchone()

        self.assertFalse(INITIALIZATION_COLUMNS.intersection(columns))
        self.assertFalse(INITIALIZATION_OBJECTS.intersection(objects))
        self.assertIsNone(marker)

    def test_initialization_receipt_and_checkpoint_commit_atomically(self) -> None:
        run = self._start()
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute(
                """CREATE TRIGGER test_reject_initialization
                   BEFORE UPDATE ON source_adapter_runs
                   WHEN NEW.initialization_mode<>''
                   BEGIN SELECT RAISE(ABORT,'injected failure'); END"""
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self._complete_seed(run)

        persisted_run = self.repository.get_run(run["run_id"])
        state = self.repository.get_state(self.adapter_key)
        self.assertEqual(persisted_run["status"], "RUNNING")
        self.assertIsNone(persisted_run["initialization"])
        self.assertEqual(state["checkpoint"], {})

    def test_seed_receipt_distinguishes_adapter_duplicates_from_selected_items(self) -> None:
        run = self._start()
        checkpoint = {"cursor": 2}
        initialization = self._initialization(run, checkpoint)
        initialization["adapter_duplicate_count"] = 1
        self.clock[0] += 10
        completed = self.repository.complete_run(
            run["run_id"],
            next_checkpoint=checkpoint,
            status=RUN_STATUS_SUCCEEDED,
            observed_count=3,
            accepted_count=0,
            duplicate_count=1,
            rejected_count=0,
            next_due_at_ms=self.clock[0] + 60_000,
            initialization=initialization,
        )
        self.assertEqual(completed["run"]["status"], RUN_STATUS_SUCCEEDED)
        self.assertEqual(completed["run"]["initialization"]["mode"], "seed_only")

    def test_mode_policy_fields_are_sealed_and_fail_closed_on_drift(self) -> None:
        run = self._start()
        checkpoint = {"cursor": 2}
        invalid_seed = self._initialization(run, checkpoint)
        invalid_seed["catch_up_max_items"] = 1
        with self.assertRaises(SourceMonitoringStateError) as seed_error:
            self.repository.complete_run(
                run["run_id"],
                next_checkpoint=checkpoint,
                status=RUN_STATUS_SUCCEEDED,
                observed_count=2,
                accepted_count=0,
                duplicate_count=0,
                rejected_count=0,
                next_due_at_ms=self.clock[0] + 60_000,
                initialization=invalid_seed,
            )
        self.assertEqual(
            seed_error.exception.code,
            "SOURCE_MONITORING_INITIALIZATION_INVALID",
        )

        invalid_catch_up = self._initialization(run, checkpoint)
        invalid_catch_up.update({
            "mode": "catch_up",
            "catch_up_max_items": 1,
        })
        with self.assertRaises(SourceMonitoringStateError) as catch_up_error:
            self.repository.complete_run(
                run["run_id"],
                next_checkpoint=checkpoint,
                status=RUN_STATUS_SUCCEEDED,
                observed_count=2,
                accepted_count=0,
                duplicate_count=0,
                rejected_count=0,
                next_due_at_ms=self.clock[0] + 60_000,
                initialization=invalid_catch_up,
            )
        self.assertEqual(
            catch_up_error.exception.code,
            "SOURCE_MONITORING_INITIALIZATION_INVALID",
        )

        valid_from_time = self._initialization(run, checkpoint, candidate_count=0)
        valid_from_time.update({
            "mode": "from_time",
            "from_time_ms": 1_800_000_000_000,
        })
        self.clock[0] += 10
        completed = self.repository.complete_run(
            run["run_id"],
            next_checkpoint=checkpoint,
            status=RUN_STATUS_SUCCEEDED,
            observed_count=0,
            accepted_count=0,
            duplicate_count=0,
            rejected_count=0,
            next_due_at_ms=self.clock[0] + 60_000,
            initialization=valid_from_time,
        )
        self.assertEqual(completed["run"]["initialization"]["catch_up_max_items"], 0)
        self.assertEqual(
            completed["run"]["initialization"]["from_time_ms"],
            1_800_000_000_000,
        )

    def test_sealed_receipt_is_verified_and_never_leaked_in_projection(self) -> None:
        run = self._start()
        completed = self._complete_seed(run)
        initialization = completed["run"]["initialization"]

        self.assertEqual(
            set(initialization),
            {
                "mode",
                "config_version",
                "preview_sha256",
                "receipt_sha256",
                "catch_up_max_items",
                "from_time_ms",
            },
        )
        self.assertNotIn("initialization_receipt_json", completed["run"])
        self.assertNotIn("terminal_counts", json.dumps(completed["run"]))
        self.assertEqual(
            self.repository.get_latest_successful_initialization(
                self.adapter_key,
                config_version=self.config_version,
            ),
            initialization,
        )

        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute(
                "DROP TRIGGER trg_source_adapter_runs_initialization_no_update"
            )
            connection.execute(
                """UPDATE source_adapter_runs
                   SET initialization_receipt_json=? WHERE run_id=?""",
                ('{"tampered":true}', run["run_id"]),
            )
        with self.assertRaises(SourceMonitoringStateError) as corrupt:
            self.repository.get_run(run["run_id"])
        self.assertEqual(
            corrupt.exception.code,
            "SOURCE_MONITORING_INITIALIZATION_RECEIPT_CORRUPT",
        )

    def test_sealed_receipt_cannot_be_updated_or_deleted(self) -> None:
        run = self._start()
        self._complete_seed(run)
        with closing(sqlite3.connect(self.database_path)) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE source_adapter_runs
                       SET initialization_preview_sha256=? WHERE run_id=?""",
                    ("b" * 64, run["run_id"]),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM source_adapter_runs WHERE run_id=?",
                    (run["run_id"],),
                )

    def test_complete_run_without_initialization_remains_compatible(self) -> None:
        run = self._start()
        self.clock[0] += 10
        completed = self.repository.complete_run(
            run["run_id"],
            next_checkpoint={"cursor": 1},
            status=RUN_STATUS_SUCCEEDED,
            observed_count=0,
            accepted_count=0,
            duplicate_count=0,
            rejected_count=0,
            next_due_at_ms=self.clock[0] + 60_000,
        )
        self.assertIsNone(completed["run"]["initialization"])
        self.assertIsNone(
            self.repository.get_latest_successful_initialization(
                self.adapter_key,
                config_version=self.config_version,
            )
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                """SELECT initialization_mode,initialization_config_version,
                          initialization_preview_sha256,initialization_receipt_json,
                          initialization_receipt_sha256
                     FROM source_adapter_runs WHERE run_id=?""",
                (run["run_id"],),
            ).fetchone()
        self.assertEqual(row, ("", "", "", "", ""))


if __name__ == "__main__":
    unittest.main()
