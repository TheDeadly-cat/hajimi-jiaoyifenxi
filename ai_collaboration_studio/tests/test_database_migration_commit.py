from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest import mock

import backend.database_migration_commit as commit_module
from backend.database_migration_commit import (
    DatabaseMigrationCommitError,
    MigrationIntentJournal,
    MigrationIntentJournalError,
    SQLiteSidecarPresent,
    _append_migration_gate_event,
    copy_to_same_directory_staging,
    hold_sqlite_file_lease,
    locked_raw_copy,
    publish_bytes_exclusive_durable,
    replace_file_with_backup,
    require_no_sqlite_sidecars,
    scan_active_migration_operations,
)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_sqlite(path: Path, value: str) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE evidence(value TEXT NOT NULL)")
        connection.execute("INSERT INTO evidence(value) VALUES(?)", (value,))


def create_wal_sqlite(path: Path, value: str) -> None:
    with closing(sqlite3.connect(path)) as connection:
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        if mode != ("wal",):
            raise AssertionError(f"WAL mode was not enabled: {mode!r}")
        connection.execute("CREATE TABLE evidence(value TEXT NOT NULL)")
        connection.execute("INSERT INTO evidence(value) VALUES(?)", (value,))
        connection.commit()
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        if checkpoint != [(0, 0, 0)]:
            raise AssertionError(f"WAL checkpoint did not finish: {checkpoint!r}")


def read_value(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection:
        return str(connection.execute("SELECT value FROM evidence").fetchone()[0])


@unittest.skipUnless(os.name == "nt", "Windows commit primitives")
class WindowsDatabaseMigrationCommitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-migration-commit-test-"
        )
        self.root = Path(self.temp_dir.name).resolve()
        self.source = self.root / "source.sqlite3"
        self.candidate = self.root / "candidate.sqlite3"
        create_sqlite(self.source, "before")
        create_sqlite(self.candidate, "candidate")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_locked_raw_copy_is_exact_and_durable(self) -> None:
        backup = self.root / "verified-backup.sqlite3"

        result = locked_raw_copy(self.source, backup)

        self.assertTrue(result["verified_equal_to_source"])
        self.assertEqual(result["source_sha256"], file_sha256(self.source))
        self.assertEqual(result["destination_sha256"], file_sha256(backup))
        self.assertEqual(file_sha256(backup), file_sha256(self.source))
        self.assertEqual(read_value(backup), "before")
        with closing(sqlite3.connect(backup)) as connection:
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchall(),
                [("ok",)],
            )

    def test_copy_and_staging_reject_hard_linked_database_images(self) -> None:
        source_alias = self.root / "source-hard-link.sqlite3"
        os.link(self.source, source_alias)
        blocked_backup = self.root / "hard-link-blocked-backup.sqlite3"
        with self.assertRaisesRegex(DatabaseMigrationCommitError, "hard-linked"):
            locked_raw_copy(self.source, blocked_backup)
        self.assertFalse(blocked_backup.exists())
        self.assertEqual(read_value(self.source), "before")

        candidate_alias = self.root / "candidate-hard-link.sqlite3"
        os.link(self.candidate, candidate_alias)
        with self.assertRaisesRegex(DatabaseMigrationCommitError, "hard-linked"):
            copy_to_same_directory_staging(self.candidate, source_alias)
        self.assertEqual(read_value(self.candidate), "candidate")

    def test_commit_primitives_reject_symlinked_parent_chain(self) -> None:
        parent_alias = self.root / "commit-parent-symlink"
        try:
            os.symlink(self.root, parent_alias, target_is_directory=True)
        except OSError as exc:  # pragma: no cover - Windows without symlink rights
            self.skipTest(f"directory symlinks unavailable in system temp: {exc}")
        try:
            with self.assertRaisesRegex(
                DatabaseMigrationCommitError,
                "path may not contain a symlink or reparse point",
            ):
                locked_raw_copy(self.source, parent_alias / "backup.sqlite3")
            with self.assertRaisesRegex(
                DatabaseMigrationCommitError,
                "path may not contain a symlink or reparse point",
            ):
                copy_to_same_directory_staging(parent_alias / self.candidate.name, self.source)
            with self.assertRaisesRegex(
                DatabaseMigrationCommitError,
                "path may not contain a symlink or reparse point",
            ):
                require_no_sqlite_sidecars(parent_alias / self.source.name)
            with self.assertRaisesRegex(
                DatabaseMigrationCommitError,
                "path may not contain a symlink or reparse point",
            ):
                publish_bytes_exclusive_durable(parent_alias / "marker.json", b"marker")
            self.assertFalse((self.root / "backup.sqlite3").exists())
            self.assertFalse((self.root / "marker.json").exists())
        finally:
            parent_alias.unlink()

    def test_open_sqlite_handle_blocks_locked_raw_copy(self) -> None:
        blocked_backup = self.root / "blocked-backup.sqlite3"
        writer = sqlite3.connect(self.source, timeout=0)
        try:
            writer.execute("SELECT COUNT(*) FROM evidence").fetchone()
            with self.assertRaisesRegex(
                DatabaseMigrationCommitError,
                "Exclusive open",
            ):
                locked_raw_copy(self.source, blocked_backup)
        finally:
            writer.close()

        self.assertFalse(blocked_backup.exists())
        self.assertEqual(read_value(self.source), "before")

    def test_all_sqlite_sidecar_names_fail_closed_without_deletion(self) -> None:
        for suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix):
                sidecar = Path(f"{self.source}{suffix}")
                sidecar.write_bytes(b"recovery evidence")
                try:
                    with self.assertRaises(SQLiteSidecarPresent):
                        require_no_sqlite_sidecars(self.source)
                    self.assertEqual(sidecar.read_bytes(), b"recovery evidence")
                finally:
                    sidecar.unlink()

    def test_candidate_copy_uses_source_directory_and_exact_hash(self) -> None:
        staging = copy_to_same_directory_staging(self.candidate, self.source)

        self.assertEqual(staging.parent, self.source.parent)
        self.assertTrue(staging.name.startswith(f".{self.source.name}.migration-stage-"))
        self.assertEqual(file_sha256(staging), file_sha256(self.candidate))
        self.assertEqual(read_value(staging), "candidate")

    def test_replace_file_with_backup_preserves_both_verified_images(self) -> None:
        source_before_sha256 = file_sha256(self.source)
        candidate_sha256 = file_sha256(self.candidate)
        staging = copy_to_same_directory_staging(self.candidate, self.source)
        atomic_backup = self.root / ".source.sqlite3.atomic-rollback.sqlite3"

        with replace_file_with_backup(
            self.source,
            staging,
            atomic_backup,
            expected_replaced_sha256=source_before_sha256,
            expected_replacement_sha256=candidate_sha256,
        ) as result:
            self.assertTrue(result["matches_verified_images"])
            self.assertEqual(
                result["replaced_after"]["file_id"],
                result["replacement_before"]["file_id"],
            )
            self.assertEqual(
                result["backup_after"]["file_id"],
                result["replaced_before"]["file_id"],
            )
            self.assertNotEqual(
                result["replaced_after"]["file_id"],
                result["backup_after"]["file_id"],
            )
            self.assertEqual(result["replaced_after"]["link_count"], 1)
            self.assertEqual(result["backup_after"]["link_count"], 1)
            self.assertEqual(
                result["sqlite_lock_byte_range"],
                {
                    "offset": 0x40000000,
                    "length": 512,
                    "exclusive": True,
                    "old_source_held_through_context_exit": True,
                    "new_source_held_through_context_exit": True,
                },
            )

            # Ordinary SQLite must honor the still-held staging lease, which
            # moved with that file to the new source during ReplaceFileW.
            blocked = sqlite3.connect(self.source, timeout=0)
            try:
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    blocked.execute("SELECT value FROM evidence").fetchone()
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    blocked.execute(
                        "INSERT INTO evidence(value) VALUES('not-committed')"
                    )
            finally:
                blocked.close()

            # Host postchecks deliberately use immutable=1 while the lease is
            # retained, so they can verify the sealed file without joining the
            # ordinary SQLite locking protocol.
            immutable_uri = f"{self.source.as_uri()}?mode=ro&immutable=1"
            with closing(sqlite3.connect(immutable_uri, uri=True)) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM evidence").fetchone(),
                    ("candidate",),
                )
                self.assertEqual(
                    connection.execute("PRAGMA integrity_check").fetchall(),
                    [("ok",)],
                )

        self.assertFalse(staging.exists())
        self.assertEqual(file_sha256(self.source), candidate_sha256)
        self.assertEqual(file_sha256(atomic_backup), source_before_sha256)
        self.assertEqual(read_value(self.source), "candidate")
        self.assertEqual(read_value(atomic_backup), "before")

    def test_exclusive_publication_never_replaces_complete_destination(self) -> None:
        destination = self.root / "receipt.json"
        publish_bytes_exclusive_durable(destination, b'{"complete":true}\n')
        self.assertEqual(destination.read_bytes(), b'{"complete":true}\n')

        with self.assertRaisesRegex(DatabaseMigrationCommitError, "already exists"):
            publish_bytes_exclusive_durable(destination, b"partial")

        self.assertEqual(destination.read_bytes(), b'{"complete":true}\n')

    def test_active_source_transaction_blocks_lease_and_preserves_staging(self) -> None:
        source_sha256 = file_sha256(self.source)
        candidate_sha256 = file_sha256(self.candidate)
        staging = copy_to_same_directory_staging(self.candidate, self.source)
        atomic_backup = self.root / ".source.sqlite3.blocked-rollback.sqlite3"
        writer = sqlite3.connect(self.source, timeout=0)
        try:
            writer.execute("BEGIN")
            writer.execute("SELECT COUNT(*) FROM evidence").fetchone()
            with self.assertRaisesRegex(
                DatabaseMigrationCommitError,
                "byte-range lease.*WinError 33",
            ):
                with replace_file_with_backup(
                    self.source,
                    staging,
                    atomic_backup,
                    expected_replaced_sha256=source_sha256,
                    expected_replacement_sha256=candidate_sha256,
                ):
                    self.fail("ReplaceFileW ran despite an active source transaction")
        finally:
            writer.close()

        self.assertEqual(read_value(self.source), "before")
        self.assertTrue(staging.exists())
        self.assertEqual(read_value(staging), "candidate")
        self.assertFalse(atomic_backup.exists())

    def test_active_staging_transaction_blocks_lease_and_preserves_source(self) -> None:
        source_sha256 = file_sha256(self.source)
        candidate_sha256 = file_sha256(self.candidate)
        staging = copy_to_same_directory_staging(self.candidate, self.source)
        atomic_backup = self.root / ".source.sqlite3.blocked-staging.sqlite3"
        reader = sqlite3.connect(staging, timeout=0)
        try:
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM evidence").fetchone()
            with self.assertRaisesRegex(
                DatabaseMigrationCommitError,
                "Exclusive open.*WinError 32",
            ):
                with replace_file_with_backup(
                    self.source,
                    staging,
                    atomic_backup,
                    expected_replaced_sha256=source_sha256,
                    expected_replacement_sha256=candidate_sha256,
                ):
                    self.fail("ReplaceFileW ran despite an active staging transaction")
        finally:
            reader.close()

        self.assertEqual(read_value(self.source), "before")
        self.assertTrue(staging.exists())
        self.assertEqual(read_value(staging), "candidate")
        self.assertFalse(atomic_backup.exists())

    def test_source_hash_drift_is_rejected_before_replace(self) -> None:
        expected_source_sha256 = file_sha256(self.source)
        candidate_sha256 = file_sha256(self.candidate)
        staging = copy_to_same_directory_staging(self.candidate, self.source)
        atomic_backup = self.root / ".source.sqlite3.source-drift.sqlite3"
        with closing(sqlite3.connect(self.source)) as connection, connection:
            connection.execute("UPDATE evidence SET value='drifted-source'")

        with self.assertRaisesRegex(
            DatabaseMigrationCommitError,
            "source hash drifted.*no replacement",
        ):
            with replace_file_with_backup(
                self.source,
                staging,
                atomic_backup,
                expected_replaced_sha256=expected_source_sha256,
                expected_replacement_sha256=candidate_sha256,
            ):
                self.fail("ReplaceFileW ran despite source hash drift")

        self.assertEqual(read_value(self.source), "drifted-source")
        self.assertEqual(read_value(staging), "candidate")
        self.assertFalse(atomic_backup.exists())

    def test_replacement_hash_drift_is_rejected_before_replace(self) -> None:
        source_sha256 = file_sha256(self.source)
        expected_replacement_sha256 = file_sha256(self.candidate)
        staging = copy_to_same_directory_staging(self.candidate, self.source)
        atomic_backup = self.root / ".source.sqlite3.replacement-drift.sqlite3"
        with closing(sqlite3.connect(staging)) as connection, connection:
            connection.execute("UPDATE evidence SET value='drifted-staging'")

        with self.assertRaisesRegex(
            DatabaseMigrationCommitError,
            "staging hash drifted.*no.*replacement",
        ):
            with replace_file_with_backup(
                self.source,
                staging,
                atomic_backup,
                expected_replaced_sha256=source_sha256,
                expected_replacement_sha256=expected_replacement_sha256,
            ):
                self.fail("ReplaceFileW ran despite replacement hash drift")

        self.assertEqual(read_value(self.source), "before")
        self.assertEqual(read_value(staging), "drifted-staging")
        self.assertFalse(atomic_backup.exists())

    def test_generic_file_lease_revalidates_hash_and_blocks_sqlite(self) -> None:
        source_sha256 = file_sha256(self.source)

        with hold_sqlite_file_lease(
            self.source,
            expected_sha256=source_sha256,
        ) as lease:
            self.assertEqual(lease["identity"]["sha256"], source_sha256)
            blocked = sqlite3.connect(self.source, timeout=0)
            try:
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    blocked.execute("SELECT value FROM evidence").fetchone()
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    blocked.execute("INSERT INTO evidence VALUES('blocked')")
            finally:
                blocked.close()

        with self.assertRaisesRegex(DatabaseMigrationCommitError, "hash drifted"):
            with hold_sqlite_file_lease(
                self.source,
                expected_sha256="0" * 64,
            ):
                self.fail("Mismatched file hash acquired a migration lease")

    def test_wal_mode_new_source_is_locked_without_creating_sidecars(self) -> None:
        source = self.root / "wal-source.sqlite3"
        candidate = self.root / "wal-candidate.sqlite3"
        create_wal_sqlite(source, "wal-before")
        create_wal_sqlite(candidate, "wal-candidate")
        for database in (source, candidate):
            for suffix in ("-wal", "-shm", "-journal"):
                self.assertFalse(Path(f"{database}{suffix}").exists())

        source_sha256 = file_sha256(source)
        candidate_sha256 = file_sha256(candidate)
        staging = copy_to_same_directory_staging(candidate, source)
        atomic_backup = self.root / ".wal-source.sqlite3.atomic.sqlite3"
        with replace_file_with_backup(
            source,
            staging,
            atomic_backup,
            expected_replaced_sha256=source_sha256,
            expected_replacement_sha256=candidate_sha256,
        ):
            blocked = sqlite3.connect(source, timeout=0)
            try:
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    blocked.execute("SELECT value FROM evidence").fetchone()
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    blocked.execute("INSERT INTO evidence VALUES('blocked-wal')")
            finally:
                blocked.close()
            for suffix in ("-wal", "-shm", "-journal"):
                self.assertFalse(Path(f"{source}{suffix}").exists())
            immutable_uri = f"{source.as_uri()}?mode=ro&immutable=1"
            with closing(sqlite3.connect(immutable_uri, uri=True)) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM evidence").fetchone(),
                    ("wal-candidate",),
                )

        self.assertEqual(read_value(source), "wal-candidate")
        self.assertEqual(read_value(atomic_backup), "wal-before")

    def test_post_replace_active_reader_race_fails_with_both_images_preserved(self) -> None:
        source_sha256 = file_sha256(self.source)
        candidate_sha256 = file_sha256(self.candidate)
        staging = copy_to_same_directory_staging(self.candidate, self.source)
        atomic_backup = self.root / ".source.sqlite3.race-rollback.sqlite3"
        real_lease = commit_module._windows_sqlite_byte_range_lease
        lease_calls = 0

        @contextmanager
        def lease_with_post_replace_reader(path: Path):
            nonlocal lease_calls
            lease_calls += 1
            if lease_calls != 2:
                with real_lease(path) as source:
                    yield source
                return
            reader = sqlite3.connect(path, timeout=0)
            try:
                reader.execute("BEGIN")
                reader.execute("SELECT value FROM evidence").fetchone()
                with real_lease(path) as source:
                    yield source
            finally:
                reader.close()

        with mock.patch.object(
            commit_module,
            "_windows_sqlite_byte_range_lease",
            lease_with_post_replace_reader,
        ), self.assertRaisesRegex(
            DatabaseMigrationCommitError,
            "ReplaceFileW succeeded.*new source byte-range lease.*WinError 33",
        ):
            with replace_file_with_backup(
                self.source,
                staging,
                atomic_backup,
                expected_replaced_sha256=source_sha256,
                expected_replacement_sha256=candidate_sha256,
            ):
                self.fail("Post-replace writer race was not rejected")

        self.assertEqual(lease_calls, 2)
        self.assertFalse(staging.exists())
        self.assertEqual(read_value(self.source), "candidate")
        self.assertEqual(read_value(atomic_backup), "before")

    def test_post_replace_committed_drift_is_detected_under_second_lease(self) -> None:
        source_sha256 = file_sha256(self.source)
        candidate_sha256 = file_sha256(self.candidate)
        staging = copy_to_same_directory_staging(self.candidate, self.source)
        atomic_backup = self.root / ".source.sqlite3.commit-race.sqlite3"
        real_lease = commit_module._windows_sqlite_byte_range_lease
        lease_calls = 0

        @contextmanager
        def lease_after_committed_drift(path: Path):
            nonlocal lease_calls
            lease_calls += 1
            if lease_calls == 2:
                with closing(sqlite3.connect(path)) as connection, connection:
                    connection.execute(
                        "INSERT INTO evidence(value) VALUES('raced-commit')"
                    )
            with real_lease(path) as source:
                yield source

        with mock.patch.object(
            commit_module,
            "_windows_sqlite_byte_range_lease",
            lease_after_committed_drift,
        ), self.assertRaisesRegex(
            DatabaseMigrationCommitError,
            "Replacement hash changed during the commit window",
        ):
            with replace_file_with_backup(
                self.source,
                staging,
                atomic_backup,
                expected_replaced_sha256=source_sha256,
                expected_replacement_sha256=candidate_sha256,
            ):
                self.fail("Committed post-replace drift was not rejected")

        self.assertEqual(lease_calls, 2)
        self.assertEqual(
            read_value(self.source),
            "candidate",
        )
        with closing(sqlite3.connect(self.source)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT value FROM evidence ORDER BY rowid"
                ).fetchall(),
                [("candidate",), ("raced-commit",)],
            )
        self.assertEqual(read_value(atomic_backup), "before")


class MigrationIntentJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-migration-journal-test-"
        )
        self.root = Path(self.temp_dir.name).resolve()
        self.database = self.root / "studio.sqlite3"
        self.database.write_bytes(b"database identity")
        self.operation_id = hashlib.sha256(b"operation-one").hexdigest()
        self.journal = MigrationIntentJournal(
            self.database,
            self.operation_id,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_markers_are_hash_chained_and_terminal_operations_are_not_active(self) -> None:
        intent = self.journal.append(
            "intent",
            {
                "prepared_sha256": self.operation_id,
                "source_sha256": file_sha256(self.database),
            },
        )
        started = self.journal.append(
            "replace_started",
            {"staging": "candidate.sqlite3"},
        )

        active = self.journal.inspect()
        self.assertTrue(active["valid"])
        self.assertTrue(active["active"])
        self.assertEqual(active["last_event"], "replace_started")
        self.assertEqual(started["previous_marker_sha256"], intent["marker_sha256"])
        self.assertEqual(
            [item["operation_id"] for item in scan_active_migration_operations(self.database)],
            [self.operation_id],
        )

        returned = self.journal.append("replace_returned", {"replaced": True})
        verified = _append_migration_gate_event(
            self.journal,
            "verified",
            {"source_sha256": "candidate"},
        )
        receipt_details = {
            "receipt_path": "receipt.json",
            "receipt_sha256": hashlib.sha256(b"receipt").hexdigest(),
        }
        committed = _append_migration_gate_event(
            self.journal,
            "receipt_committed",
            receipt_details,
        )
        complete = _append_migration_gate_event(
            self.journal,
            "complete",
            receipt_details,
        )
        terminal = self.journal.inspect()
        self.assertFalse(terminal["active"])
        self.assertEqual(terminal["terminal_event"], "complete")
        self.assertEqual(
            complete["previous_marker_sha256"],
            committed["marker_sha256"],
        )
        self.assertEqual(scan_active_migration_operations(self.database), [])
        with self.assertRaisesRegex(MigrationIntentJournalError, "already terminal"):
            _append_migration_gate_event(self.journal, "receipt_committed", {})

    def test_marker_tamper_is_reported_as_an_active_invalid_operation(self) -> None:
        intent = self.journal.append("intent", {"source_sha256": "original"})
        marker_path = Path(intent["marker_path"])
        tampered = json.loads(marker_path.read_text(encoding="utf-8"))
        tampered["details"]["source_sha256"] = "tampered"
        marker_path.write_text(
            json.dumps(tampered, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(MigrationIntentJournalError, "digest is invalid"):
            self.journal.inspect()
        scan = MigrationIntentJournal.scan_active(self.database)
        self.assertEqual(len(scan), 1)
        self.assertFalse(scan[0]["valid"])
        self.assertTrue(scan[0]["active"])
        self.assertIn("digest is invalid", scan[0]["error"])

    def test_invalid_utf8_marker_is_reported_as_active_invalid(self) -> None:
        intent = self.journal.append("intent", {"source_sha256": "original"})
        marker_path = Path(intent["marker_path"])
        marker_path.write_bytes(b"\xff\xfe\x80")

        with self.assertRaisesRegex(MigrationIntentJournalError, "Cannot read"):
            self.journal.inspect()
        scan = MigrationIntentJournal.scan_active(self.database)
        self.assertEqual(len(scan), 1)
        self.assertFalse(scan[0]["valid"])
        self.assertTrue(scan[0]["active"])
        self.assertIn("Cannot read", scan[0]["error"])

    def test_marker_symlink_or_hardlink_is_reported_as_active_invalid(self) -> None:
        intent = self.journal.append("intent", {"source_sha256": "original"})
        marker_path = Path(intent["marker_path"])
        target = self.root / "marker-target.json"
        target.write_bytes(marker_path.read_bytes())
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind):
                if marker_path.exists() or marker_path.is_symlink():
                    marker_path.unlink()
                try:
                    if kind == "symlink":
                        os.symlink(target, marker_path)
                    else:
                        os.link(target, marker_path)
                except OSError as exc:  # pragma: no cover - filesystem dependent
                    self.skipTest(f"{kind} unavailable in system temp: {exc}")

                with self.assertRaisesRegex(
                    MigrationIntentJournalError,
                    "marker (may not be a symlink|must be an independent)",
                ):
                    self.journal.inspect()
                scan = MigrationIntentJournal.scan_active(self.database)
                self.assertEqual(len(scan), 1)
                self.assertFalse(scan[0]["valid"])
                self.assertTrue(scan[0]["active"])
                marker_path.unlink()

        target.unlink()

    def test_case_variant_marker_namespace_is_reported_as_active_invalid(self) -> None:
        intent = self.journal.append("intent", {"source_sha256": "original"})
        marker_path = Path(intent["marker_path"])
        variant = self.root / (
            f".{self.database.name.swapcase()}.MiGrAtIoN-"
            f"{self.operation_id.upper()}.000000-InTeNt.json"
        )
        marker_bytes = marker_path.read_bytes()
        marker_path.unlink()
        variant.write_bytes(marker_bytes)
        try:
            with self.assertRaisesRegex(
                MigrationIntentJournalError,
                "filename is not canonical",
            ):
                self.journal.inspect()
            scan = MigrationIntentJournal.scan_active(self.database)
            self.assertEqual(len(scan), 1)
            self.assertFalse(scan[0]["valid"])
            self.assertTrue(scan[0]["active"])
        finally:
            variant.unlink()

    def test_scan_uses_the_exact_database_name_and_ignores_terminal_other_files(self) -> None:
        self.journal.append("intent", {})
        other_database = self.root / "other-studio.sqlite3"
        other_database.write_bytes(b"other database")
        other_operation = hashlib.sha256(b"operation-two").hexdigest()
        other_journal = MigrationIntentJournal(other_database, other_operation)
        other_journal.append("intent", {})
        _append_migration_gate_event(other_journal, "abort_verified", {})
        _append_migration_gate_event(other_journal, "aborted", {})

        decoy = self.root / (
            f".{self.database.name}x.migration-{other_operation}.000000-intent.json"
        )
        decoy.write_text("{}", encoding="utf-8")

        studio_active = MigrationIntentJournal.scan_active(self.database)
        other_active = MigrationIntentJournal.scan_active(other_database)
        self.assertEqual(
            [item["operation_id"] for item in studio_active],
            [self.operation_id],
        )
        self.assertEqual(other_active, [])

    def test_first_marker_must_be_intent_and_operation_id_is_exact_sha256(self) -> None:
        with self.assertRaisesRegex(MigrationIntentJournalError, "first.*intent"):
            self.journal.append("replace_started", {})
        with self.assertRaisesRegex(MigrationIntentJournalError, "lowercase SHA-256"):
            MigrationIntentJournal(self.database, "not-a-sha")

    def test_terminal_events_cannot_skip_verified_state(self) -> None:
        self.journal.append("intent", {})
        for event in ("complete", "rolled_back", "aborted"):
            with self.subTest(event=event), self.assertRaisesRegex(
                MigrationIntentJournalError,
                "reserved for the authorized gate",
            ):
                self.journal.append(event, {})
        self.assertTrue(self.journal.inspect()["active"])
        self.assertEqual(
            [item["operation_id"] for item in scan_active_migration_operations(self.database)],
            [self.operation_id],
        )


if __name__ == "__main__":
    unittest.main()
