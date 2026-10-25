from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from backend import database_startup_recovery as recovery
from backend.database_migration_commit import DatabaseMigrationCommitError
from backend.instance_ownership import DatabaseInstanceOwner, InstanceAlreadyRunning


@unittest.skipUnless(os.name == "nt", "Recovery uses Windows exclusive file-family handles")
class StartupRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ai-studio-wal-recovery-test-")
        self.root = Path(self.temporary.name)
        self.database = self.root / "source.sqlite3"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, body TEXT)")
            connection.commit()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def crash_committed_write(self) -> None:
        result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c",
            "import os,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
            "c.execute('PRAGMA wal_autocheckpoint=0'); "
            "c.execute(\"INSERT INTO items VALUES(1,'committed-before-crash')\"); c.commit(); os._exit(0)",
            str(self.database)], executable=sys.executable, capture_output=True, timeout=15, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertGreater(Path(str(self.database) + "-wal").stat().st_size, 32)

    def family_hashes(self) -> dict[str, str]:
        return {suffix: hashlib.sha256(path.read_bytes()).hexdigest()
                for suffix, path in recovery._family(self.database).items()}

    def test_committed_crash_wal_is_preserved_and_recovered(self) -> None:
        self.crash_committed_write()
        before = self.family_hashes()
        with DatabaseInstanceOwner(self.database) as owner:
            prepared = recovery.prepare_startup_recovery(self.database, self.root / "recovery", instance_owner=owner)
            self.assertEqual(self.family_hashes(), before)
            receipt = recovery.apply_startup_recovery(self.database, self.root / "recovery/prepared.json",
                authorization_token=prepared["authorization_token"], instance_owner=owner)
        self.assertEqual(receipt["status"], "recovered")
        self.assertFalse(receipt["schema_migrated"])
        self.assertEqual(set(self.family_hashes()), {""})
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT body FROM items").fetchall(), [("committed-before-crash",)])
        for suffix, digest in before.items():
            self.assertEqual(hashlib.sha256((self.root / "recovery/original-files" / (self.database.name + suffix)).read_bytes()).hexdigest(), digest)
        self.assertFalse(recovery._pending_path(self.database).exists())

    def test_empty_wal_and_shm_recover_without_business_changes(self) -> None:
        Path(str(self.database) + "-wal").write_bytes(b"")
        Path(str(self.database) + "-shm").write_bytes(bytes(32768))
        with DatabaseInstanceOwner(self.database) as owner:
            receipt = recovery.recover_startup_database(self.database, self.root / "recovery", instance_owner=owner)
        self.assertTrue(receipt["sidecars_absent"])
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM items").fetchone()[0], 0)

    def test_active_owner_and_unheld_owner_refuse_without_touching_source(self) -> None:
        self.crash_committed_write()
        before = self.family_hashes()
        with DatabaseInstanceOwner(self.database):
            with self.assertRaises(InstanceAlreadyRunning):
                DatabaseInstanceOwner(self.database).acquire()
        with self.assertRaises(RuntimeError):
            recovery.prepare_startup_recovery(self.database, self.root / "recovery",
                instance_owner=DatabaseInstanceOwner(self.database))
        self.assertEqual(self.family_hashes(), before)

    def test_active_sqlite_writer_prevents_backup(self) -> None:
        with closing(sqlite3.connect(self.database)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            with DatabaseInstanceOwner(self.database) as owner:
                with self.assertRaises(DatabaseMigrationCommitError):
                    recovery.prepare_startup_recovery(self.database, self.root / "recovery", instance_owner=owner)
            writer.rollback()
        self.assertFalse((self.root / "recovery/prepared.json").exists())

    def test_corrupt_wal_is_rejected_before_source_sqlite_open(self) -> None:
        self.crash_committed_write()
        wal = Path(str(self.database) + "-wal")
        data = bytearray(wal.read_bytes())
        data[-1] ^= 1
        wal.write_bytes(data)
        before = self.family_hashes()
        with DatabaseInstanceOwner(self.database) as owner:
            with patch.object(recovery, "_sqlite_recover", side_effect=AssertionError("must not open SQLite")):
                with self.assertRaisesRegex(recovery.StartupRecoveryError, "checksum"):
                    recovery.prepare_startup_recovery(self.database, self.root / "recovery", instance_owner=owner)
        self.assertEqual(self.family_hashes(), before)

    def test_backup_failure_leaves_source_unchanged(self) -> None:
        self.crash_committed_write()
        before = self.family_hashes()
        with DatabaseInstanceOwner(self.database) as owner:
            with patch.object(recovery, "_copy_locked_reader", side_effect=OSError("synthetic backup failure")):
                with self.assertRaisesRegex(OSError, "synthetic backup failure"):
                    recovery.prepare_startup_recovery(self.database, self.root / "recovery", instance_owner=owner)
        self.assertEqual(self.family_hashes(), before)
        self.assertFalse((self.root / "recovery/prepared.json").exists())

    def test_journal_and_pending_migration_refuse_before_backup(self) -> None:
        Path(str(self.database) + "-journal").write_bytes(b"preserve")
        with DatabaseInstanceOwner(self.database) as owner:
            with self.assertRaisesRegex(recovery.StartupRecoveryError, "Rollback journal"):
                recovery.prepare_startup_recovery(self.database, self.root / "journal", instance_owner=owner)
            with patch.object(recovery, "scan_active_migration_operations", return_value=[{"pending": True}]):
                with self.assertRaisesRegex(recovery.StartupRecoveryError, "Pending database migration"):
                    recovery.prepare_startup_recovery(self.database, self.root / "migration", instance_owner=owner)
        self.assertFalse((self.root / "journal").exists())
        self.assertEqual(Path(str(self.database) + "-journal").read_bytes(), b"preserve")

    def test_wrong_authorization_and_changed_source_are_rejected(self) -> None:
        self.crash_committed_write()
        before = self.family_hashes()
        with DatabaseInstanceOwner(self.database) as owner:
            prepared = recovery.prepare_startup_recovery(self.database, self.root / "recovery", instance_owner=owner)
            with self.assertRaisesRegex(recovery.StartupRecoveryError, "authorization"):
                recovery.apply_startup_recovery(self.database, self.root / "recovery/prepared.json",
                    authorization_token="wrong", instance_owner=owner)
            self.assertEqual(self.family_hashes(), before)
            Path(str(self.database) + "-shm").write_bytes(b"changed")
            with self.assertRaisesRegex(recovery.StartupRecoveryError, "Source file family changed"):
                recovery.apply_startup_recovery(self.database, self.root / "recovery/prepared.json",
                    authorization_token=prepared["authorization_token"], instance_owner=owner)

    def test_apply_failure_retains_pending_marker_and_prevents_automatic_retry(self) -> None:
        self.crash_committed_write()
        with DatabaseInstanceOwner(self.database) as owner:
            prepared = recovery.prepare_startup_recovery(self.database, self.root / "recovery", instance_owner=owner)
            with patch.object(recovery, "_sqlite_recover", side_effect=sqlite3.OperationalError("synthetic busy")):
                with self.assertRaises(sqlite3.OperationalError):
                    recovery.apply_startup_recovery(self.database, self.root / "recovery/prepared.json",
                        authorization_token=prepared["authorization_token"], instance_owner=owner)
            self.assertTrue(recovery._pending_path(self.database).exists())
            with self.assertRaisesRegex(recovery.StartupRecoveryError, "unfinished"):
                recovery.prepare_startup_recovery(self.database, self.root / "retry", instance_owner=owner)
        self.assertFalse((self.root / "recovery/receipt.json").exists())

    def test_existing_recovery_directory_is_preserved(self) -> None:
        self.crash_committed_write()
        target = self.root / "recovery"
        target.mkdir()
        evidence = target / "existing.txt"
        evidence.write_text("retain", encoding="utf-8")
        before = self.family_hashes()
        with DatabaseInstanceOwner(self.database) as owner:
            with self.assertRaisesRegex(recovery.StartupRecoveryError, "fresh unaliased"):
                recovery.prepare_startup_recovery(self.database, target, instance_owner=owner)
        self.assertEqual(evidence.read_text(encoding="utf-8"), "retain")
        self.assertEqual(self.family_hashes(), before)

    def test_hard_link_database_alias_is_rejected(self) -> None:
        alias = self.root / "alias.sqlite3"
        os.link(self.database, alias)
        with self.assertRaisesRegex(RuntimeError, "independent regular"):
            DatabaseInstanceOwner(self.database)
        self.assertTrue(alias.exists())

    def test_foreign_key_violation_prevents_original_recovery(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE children(parent_id REFERENCES items(id))")
            connection.execute("INSERT INTO children VALUES(999)")
            connection.commit()
        self.crash_committed_write()
        before = self.family_hashes()
        with DatabaseInstanceOwner(self.database) as owner:
            with self.assertRaisesRegex(recovery.StartupRecoveryError, "foreign-key"):
                recovery.prepare_startup_recovery(self.database, self.root / "recovery", instance_owner=owner)
        self.assertEqual(self.family_hashes(), before)
        self.assertFalse(recovery._pending_path(self.database).exists())


if __name__ == "__main__":
    unittest.main()
