from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from backend.database_migration import (
    DatabaseMigrationError,
    DatabaseMigrationRecoveryRequired,
    apply_authorized_migration,
    assert_database_ready_for_startup,
    build_migration_manifest,
    prepare_migration,
    recover_pending_database_migration,
    write_migration_manifest,
)
from backend.database_migration_commit import (
    DatabaseMigrationCommitError,
    MigrationIntentJournal,
)
from backend.store import StudioStore


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _value_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _checkpoint(database_path: Path) -> None:
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


class DatabaseMigrationRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-database-migration-recovery-test-"
        )
        self.root = Path(self.temp_dir.name).resolve()
        self.database_path = self.root / "source.sqlite3"
        StudioStore(self.database_path)
        _checkpoint(self.database_path)
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute("DROP INDEX idx_messages_room_keyset")
        _checkpoint(self.database_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _prepare(self, label: str) -> tuple[dict[str, object], Path, Path]:
        manifest_path = write_migration_manifest(
            build_migration_manifest(self.database_path),
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
        return prepared, prepared_path, self.root / f"{label}-receipt.json"

    def _leave_candidate_pending(
        self,
        label: str,
    ) -> tuple[dict[str, object], Path, Path]:
        prepared, prepared_path, receipt_path = self._prepare(label)
        with mock.patch(
            "backend.database_migration._write_json_exclusive",
            side_effect=OSError("injected exclusive receipt write failure"),
        ):
            with self.assertRaises(DatabaseMigrationRecoveryRequired) as caught:
                apply_authorized_migration(
                    database_path=self.database_path,
                    prepared_path=prepared_path,
                    authorization_token=str(prepared["authorization_token"]),
                    receipt_path=receipt_path,
                )
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertIn(
            "injected exclusive receipt write failure",
            str(caught.exception.__cause__),
        )
        self.assertFalse(receipt_path.exists())
        return prepared, prepared_path, receipt_path

    def test_pre_replace_failure_blocks_startup_and_can_only_abort_unchanged_source(
        self,
    ) -> None:
        prepared, prepared_path, receipt_path = self._prepare("pre-replace")
        source_before_sha256 = _file_sha256(self.database_path)

        with mock.patch(
            "backend.database_migration.replace_file_with_backup",
            side_effect=DatabaseMigrationCommitError(
                "injected failure before ReplaceFileW"
            ),
        ):
            with self.assertRaises(DatabaseMigrationRecoveryRequired) as caught:
                apply_authorized_migration(
                    database_path=self.database_path,
                    prepared_path=prepared_path,
                    authorization_token=str(prepared["authorization_token"]),
                    receipt_path=receipt_path,
                )

        operation_id = str(prepared["prepared_sha256"])
        self.assertEqual(
            [item["operation_id"] for item in caught.exception.operations],
            [operation_id],
        )
        self.assertEqual(_file_sha256(self.database_path), source_before_sha256)
        self.assertFalse(receipt_path.exists())
        with self.assertRaises(DatabaseMigrationRecoveryRequired):
            build_migration_manifest(self.database_path)
        with self.assertRaises(DatabaseMigrationRecoveryRequired):
            assert_database_ready_for_startup(self.database_path)

        status = recover_pending_database_migration(
            database_path=self.database_path,
            operation_id=operation_id,
            action="inspect",
        )
        self.assertEqual(status["classification"], "SOURCE_BEFORE")
        self.assertEqual(status["last_event"], "replace_started")

        result = recover_pending_database_migration(
            database_path=self.database_path,
            operation_id=operation_id,
            action="abort",
        )
        self.assertEqual(result["outcome"], "aborted")
        self.assertEqual(_file_sha256(self.database_path), source_before_sha256)
        terminal = MigrationIntentJournal(
            self.database_path,
            operation_id,
        ).inspect()
        self.assertFalse(terminal["active"])
        self.assertEqual(terminal["terminal_event"], "aborted")
        self.assertTrue(build_migration_manifest(self.database_path)["requires_migration"])

    def test_post_replace_receipt_failure_requires_exact_token_to_finalize(self) -> None:
        prepared, _prepared_path, receipt_path = self._leave_candidate_pending(
            "finalize"
        )
        operation_id = str(prepared["prepared_sha256"])
        candidate_sha256 = prepared["candidate"]["snapshot"]["file"]["sha256"]
        source_before_sha256 = prepared["source"]["file"]["sha256"]

        status = recover_pending_database_migration(
            database_path=self.database_path,
            operation_id=operation_id,
            action="inspect",
        )
        self.assertEqual(status["classification"], "CANDIDATE")
        self.assertEqual(_file_sha256(self.database_path), candidate_sha256)
        rollback_path = Path(status["atomic_rollback_path"])
        self.assertEqual(_file_sha256(rollback_path), source_before_sha256)

        with self.assertRaisesRegex(DatabaseMigrationError, "authorization token"):
            recover_pending_database_migration(
                database_path=self.database_path,
                operation_id=operation_id,
                action="finalize",
                authorization_token="wrong-token",
            )
        self.assertFalse(receipt_path.exists())
        self.assertTrue(
            MigrationIntentJournal(self.database_path, operation_id).inspect()["active"]
        )

        result = recover_pending_database_migration(
            database_path=self.database_path,
            operation_id=operation_id,
            action="finalize",
            authorization_token=str(prepared["authorization_token"]),
        )
        self.assertEqual(result["outcome"], "finalized")
        self.assertEqual(_file_sha256(self.database_path), candidate_sha256)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        stored_receipt_sha256 = receipt.pop("receipt_sha256")
        self.assertEqual(stored_receipt_sha256, _value_sha256(receipt))
        self.assertEqual(stored_receipt_sha256, result["receipt_sha256"])
        self.assertTrue(receipt["recovered_from_pending_intent"])
        self.assertEqual(receipt["prepared_sha256"], operation_id)
        self.assertEqual(receipt["source_before"]["sha256"], source_before_sha256)
        self.assertEqual(receipt["after"]["sha256"], candidate_sha256)
        self.assertEqual(receipt["after"]["integrity_check"], ["ok"])
        self.assertEqual(receipt["after"]["foreign_key_violation_count"], 0)
        terminal = MigrationIntentJournal(
            self.database_path,
            operation_id,
        ).inspect()
        self.assertFalse(terminal["active"])
        self.assertEqual(terminal["terminal_event"], "complete")
        self.assertFalse(build_migration_manifest(self.database_path)["requires_migration"])

    def test_post_replace_receipt_failure_can_explicitly_rollback(self) -> None:
        prepared, _prepared_path, receipt_path = self._leave_candidate_pending(
            "rollback"
        )
        operation_id = str(prepared["prepared_sha256"])
        source_before_sha256 = prepared["source"]["file"]["sha256"]
        candidate_sha256 = prepared["candidate"]["snapshot"]["file"]["sha256"]

        with self.assertRaisesRegex(DatabaseMigrationError, "authorization token"):
            recover_pending_database_migration(
                database_path=self.database_path,
                operation_id=operation_id,
                action="rollback",
                authorization_token="wrong-token",
            )
        self.assertEqual(_file_sha256(self.database_path), candidate_sha256)

        result = recover_pending_database_migration(
            database_path=self.database_path,
            operation_id=operation_id,
            action="rollback",
            authorization_token=str(prepared["authorization_token"]),
        )
        self.assertEqual(result["outcome"], "rolled_back")
        self.assertEqual(_file_sha256(self.database_path), source_before_sha256)
        rollback_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        stored_receipt_sha256 = rollback_receipt.pop("receipt_sha256")
        self.assertEqual(stored_receipt_sha256, _value_sha256(rollback_receipt))
        self.assertEqual(stored_receipt_sha256, result["receipt_sha256"])
        self.assertEqual(
            rollback_receipt["version"],
            "database_migration_rollback_receipt_v2",
        )
        self.assertEqual(
            rollback_receipt["restored_source_sha256"],
            source_before_sha256,
        )
        failed_candidate_path = Path(rollback_receipt["failed_candidate_path"])
        self.assertTrue(failed_candidate_path.is_file())
        self.assertEqual(_file_sha256(failed_candidate_path), candidate_sha256)
        self.assertEqual(
            rollback_receipt["failed_candidate_sha256"],
            candidate_sha256,
        )
        terminal = MigrationIntentJournal(
            self.database_path,
            operation_id,
        ).inspect()
        self.assertFalse(terminal["active"])
        self.assertEqual(terminal["terminal_event"], "rolled_back")
        self.assertTrue(build_migration_manifest(self.database_path)["requires_migration"])

    def test_unknown_pending_image_refuses_finalize_rollback_and_abort(self) -> None:
        prepared, _prepared_path, receipt_path = self._leave_candidate_pending(
            "unknown"
        )
        operation_id = str(prepared["prepared_sha256"])
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute(
                "INSERT INTO schema_migrations(key, applied_at) VALUES(?, ?)",
                ("injected_unknown_recovery_image", 1),
            )
        _checkpoint(self.database_path)

        status = recover_pending_database_migration(
            database_path=self.database_path,
            operation_id=operation_id,
            action="inspect",
        )
        self.assertEqual(status["classification"], "UNKNOWN")
        self.assertFalse(receipt_path.exists())
        for action in ("finalize", "rollback"):
            with self.subTest(action=action):
                with self.assertRaisesRegex(
                    DatabaseMigrationError,
                    "not an exact recoverable candidate/source pair",
                ):
                    recover_pending_database_migration(
                        database_path=self.database_path,
                        operation_id=operation_id,
                        action=action,
                        authorization_token=str(prepared["authorization_token"]),
                    )
        with self.assertRaisesRegex(
            DatabaseMigrationError,
            "only while the source is exactly unchanged",
        ):
            recover_pending_database_migration(
                database_path=self.database_path,
                operation_id=operation_id,
                action="abort",
            )
        pending = MigrationIntentJournal(
            self.database_path,
            operation_id,
        ).inspect()
        self.assertTrue(pending["active"])
        self.assertIsNone(pending["terminal_event"])
        with self.assertRaises(DatabaseMigrationRecoveryRequired):
            assert_database_ready_for_startup(self.database_path)


if __name__ == "__main__":
    unittest.main()
