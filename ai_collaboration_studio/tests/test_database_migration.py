from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from backend.database_migration import (
    DatabaseMigrationError,
    apply_authorized_migration,
    assert_database_ready_for_startup,
    build_migration_manifest,
    prepare_migration,
    write_migration_manifest,
)
from backend.instance_ownership import DatabaseInstanceOwner, InstanceAlreadyRunning
from backend.store import StudioStore


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checkpoint(database_path: Path) -> None:
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def require_storage_turn_contract_migration(database_path: Path) -> None:
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute(
            "UPDATE rooms SET capability_packs_json=? WHERE id='room_storage'",
            ('["storage_research_readonly"]',),
        )
        connection.execute(
            "DELETE FROM schema_migrations "
            "WHERE key='storage_turn_contract_capability_pack_v1'"
        )
    checkpoint(database_path)


class DatabaseMigrationGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-database-migration-test-"
        )
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "source.sqlite3"
        StudioStore(self.database_path)
        checkpoint(self.database_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_current_database_startup_preflight_is_source_read_only(self) -> None:
        before_sha256 = file_sha256(self.database_path)
        before_stat = self.database_path.stat()
        before_sidecars = {
            suffix: Path(f"{self.database_path}{suffix}").exists()
            for suffix in ("-wal", "-shm", "-journal")
        }

        readiness = assert_database_ready_for_startup(self.database_path)

        self.assertEqual(readiness["source_sha256"], before_sha256)
        self.assertEqual(readiness["integrity_check"], ["ok"])
        self.assertEqual(readiness["foreign_key_violation_count"], 0)
        self.assertEqual(readiness["wal_size"], 0)
        self.assertEqual(file_sha256(self.database_path), before_sha256)
        self.assertEqual(self.database_path.stat().st_mtime_ns, before_stat.st_mtime_ns)
        self.assertEqual(
            {
                suffix: Path(f"{self.database_path}{suffix}").exists()
                for suffix in ("-wal", "-shm", "-journal")
            },
            before_sidecars,
        )

    def test_formal_style_preflight_uses_only_internal_system_temp_shadow(self) -> None:
        source_sha256 = file_sha256(self.database_path)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_STUDIO_SKIP_LOCAL_ENV", None)
            readiness = assert_database_ready_for_startup(self.database_path)

        self.assertEqual(readiness["source_sha256"], source_sha256)
        self.assertEqual(file_sha256(self.database_path), source_sha256)

    def test_formal_style_prepare_uses_only_internal_system_temp_shadow(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute("DROP INDEX idx_messages_room_keyset")
        checkpoint(self.database_path)
        source_sha256 = file_sha256(self.database_path)

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_STUDIO_SKIP_LOCAL_ENV", None)
            manifest = build_migration_manifest(self.database_path)
            manifest_path = write_migration_manifest(
                manifest,
                self.root / "formal-style-manifest.json",
            )
            prepared = prepare_migration(
                database_path=self.database_path,
                manifest_path=manifest_path,
                backup_path=self.root / "formal-style-backup.sqlite3",
                candidate_path=self.root / "formal-style-candidate.sqlite3",
                prepared_path=self.root / "formal-style-prepared.json",
            )

        self.assertEqual(file_sha256(self.database_path), source_sha256)
        self.assertEqual(
            prepared["candidate"]["snapshot"]["logical"]["logical_sha256"],
            manifest["projected_state"]["logical_sha256"],
        )

    def test_manifest_backup_authorization_and_post_checks_form_a_hard_gate(self) -> None:
        require_storage_turn_contract_migration(self.database_path)
        source_sha256 = file_sha256(self.database_path)

        manifest = build_migration_manifest(self.database_path)

        self.assertTrue(manifest["requires_migration"])
        self.assertEqual(manifest["before"]["sqlite"]["integrity_check"], ["ok"])
        self.assertEqual(
            manifest["before"]["sqlite"]["foreign_key_violation_count"],
            0,
        )
        self.assertEqual(manifest["before"]["sidecars"]["wal"]["size"], 0)
        self.assertEqual(manifest["before"]["file"]["sha256"], source_sha256)
        self.assertIsInstance(manifest["migration_epoch_ms"], int)
        self.assertEqual(
            manifest["projected_state"]["logical_sha256"],
            manifest["projected_after"]["logical"]["logical_sha256"],
        )
        self.assertEqual(
            manifest["projected_state"]["tables"]["rooms"]["content_sha256"],
            manifest["projected_after"]["logical"]["tables"]["rooms"][
                "content_sha256"
            ],
        )
        changed_tables = {
            item["table"] for item in manifest["changes"]["data_changes"]
        }
        self.assertIn("rooms", changed_tables)
        self.assertIn("room_versions", changed_tables)
        self.assertIn("schema_migrations", changed_tables)
        manifest_path = write_migration_manifest(
            manifest,
            self.root / "migration-manifest.json",
        )
        backup_path = self.root / "verified-backup.sqlite3"
        candidate_path = self.root / "verified-candidate.sqlite3"
        prepared_path = self.root / "prepared-migration.json"
        receipt_path = self.root / "migration-receipt.json"

        prepared = prepare_migration(
            database_path=self.database_path,
            manifest_path=manifest_path,
            backup_path=backup_path,
            candidate_path=candidate_path,
            prepared_path=prepared_path,
        )

        self.assertTrue(backup_path.is_file())
        self.assertEqual(file_sha256(backup_path), source_sha256)
        self.assertEqual(file_sha256(self.database_path), source_sha256)
        self.assertEqual(
            prepared["migration_epoch_ms"],
            manifest["migration_epoch_ms"],
        )
        self.assertEqual(
            prepared["manifest_file_sha256"],
            file_sha256(manifest_path),
        )
        self.assertEqual(
            prepared["backup"]["snapshot"]["sqlite"]["integrity_check"],
            ["ok"],
        )
        self.assertEqual(
            prepared["backup"]["snapshot"]["sqlite"][
                "foreign_key_violation_count"
            ],
            0,
        )

        with self.assertRaisesRegex(DatabaseMigrationError, "authorization token"):
            apply_authorized_migration(
                database_path=self.database_path,
                prepared_path=prepared_path,
                authorization_token="not-authorized",
                receipt_path=receipt_path,
            )
        self.assertTrue(backup_path.exists())
        self.assertEqual(file_sha256(self.database_path), source_sha256)

        receipt = apply_authorized_migration(
            database_path=self.database_path,
            prepared_path=prepared_path,
            authorization_token=prepared["authorization_token"],
            receipt_path=receipt_path,
        )

        self.assertEqual(file_sha256(backup_path), source_sha256)
        self.assertTrue(receipt["backup"]["verified_equal_to_source"])
        self.assertEqual(receipt["after"]["integrity_check"], ["ok"])
        self.assertEqual(receipt["after"]["foreign_key_violation_count"], 0)
        self.assertEqual(receipt["after"]["wal_size"], 0)
        self.assertTrue(receipt["after"]["matches_authorized_candidate"])
        self.assertEqual(
            receipt["after"]["sha256"],
            prepared["candidate"]["snapshot"]["file"]["sha256"],
        )
        self.assertEqual(
            receipt["after"]["logical_sha256"],
            prepared["candidate"]["snapshot"]["logical"]["logical_sha256"],
        )
        self.assertEqual(
            json.loads(receipt_path.read_text(encoding="utf-8"))["receipt_sha256"],
            receipt["receipt_sha256"],
        )
        self.assertFalse(build_migration_manifest(self.database_path)["requires_migration"])

    def test_prepare_reuses_manifest_epoch_after_wall_clock_delay(self) -> None:
        require_storage_turn_contract_migration(self.database_path)
        manifest = build_migration_manifest(self.database_path)
        manifest_path = write_migration_manifest(
            manifest,
            self.root / "delayed-manifest.json",
        )

        time.sleep(0.02)
        prepared = prepare_migration(
            database_path=self.database_path,
            manifest_path=manifest_path,
            backup_path=self.root / "delayed-backup.sqlite3",
            candidate_path=self.root / "delayed-candidate.sqlite3",
            prepared_path=self.root / "delayed-prepared.json",
        )

        self.assertEqual(
            prepared["migration_epoch_ms"],
            manifest["migration_epoch_ms"],
        )
        self.assertEqual(
            prepared["candidate"]["snapshot"]["logical"]["logical_sha256"],
            manifest["projected_state"]["logical_sha256"],
        )
        self.assertEqual(
            prepared["candidate"]["snapshot"]["logical"]["tables"],
            manifest["projected_state"]["tables"],
        )

    def test_manifest_plan_digest_seals_projected_table_content_hashes(self) -> None:
        require_storage_turn_contract_migration(self.database_path)
        manifest_path = write_migration_manifest(
            build_migration_manifest(self.database_path),
            self.root / "projected-hash-manifest.json",
        )
        source_sha256 = file_sha256(self.database_path)
        tampered = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertTrue(tampered["changes"]["data_changes"])
        tampered["changes"]["data_changes"][0][
            "content_sha256_projected"
        ] = "0" * 64
        manifest_path.write_text(
            json.dumps(tampered, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(DatabaseMigrationError, "manifest digest"):
            prepare_migration(
                database_path=self.database_path,
                manifest_path=manifest_path,
                backup_path=self.root / "tampered-plan-backup.sqlite3",
                candidate_path=self.root / "tampered-plan-candidate.sqlite3",
                prepared_path=self.root / "tampered-plan-prepared.json",
            )

        self.assertEqual(file_sha256(self.database_path), source_sha256)

    def test_apply_revalidates_exact_manifest_file_bytes(self) -> None:
        require_storage_turn_contract_migration(self.database_path)
        manifest_path = write_migration_manifest(
            build_migration_manifest(self.database_path),
            self.root / "exact-manifest.json",
        )
        prepared_path = self.root / "exact-prepared.json"
        prepared = prepare_migration(
            database_path=self.database_path,
            manifest_path=manifest_path,
            backup_path=self.root / "exact-backup.sqlite3",
            candidate_path=self.root / "exact-candidate.sqlite3",
            prepared_path=prepared_path,
        )
        source_sha256 = file_sha256(self.database_path)
        manifest_path.write_text(
            manifest_path.read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
        receipt_path = self.root / "exact-receipt.json"

        with self.assertRaisesRegex(DatabaseMigrationError, "file digest changed"):
            apply_authorized_migration(
                database_path=self.database_path,
                prepared_path=prepared_path,
                authorization_token=prepared["authorization_token"],
                receipt_path=receipt_path,
            )

        self.assertEqual(file_sha256(self.database_path), source_sha256)
        self.assertFalse(receipt_path.exists())

    def test_preflight_refuses_non_empty_wal_without_checkpointing_it(self) -> None:
        writer = sqlite3.connect(self.database_path)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute(
                "INSERT INTO schema_migrations(key,applied_at) VALUES(?,?)",
                ("test_pending_wal", 1),
            )
            writer.commit()
            wal_path = Path(f"{self.database_path}-wal")
            self.assertGreater(wal_path.stat().st_size, 0)

            with self.assertRaisesRegex(DatabaseMigrationError, "non-empty WAL"):
                build_migration_manifest(self.database_path)

            self.assertGreater(wal_path.stat().st_size, 0)
        finally:
            writer.close()

    def test_preflight_refuses_any_sqlite_sidecar_without_deleting_it(self) -> None:
        for suffix, payload in (
            ("-wal", b""),
            ("-shm", b"shared-memory-marker"),
            ("-journal", b"rollback-journal-marker"),
        ):
            with self.subTest(suffix=suffix):
                sidecar = Path(f"{self.database_path}{suffix}")
                sidecar.write_bytes(payload)
                before = sidecar.read_bytes()
                with self.assertRaisesRegex(
                    DatabaseMigrationError,
                    "SQLite sidecars",
                ):
                    build_migration_manifest(self.database_path)
                self.assertEqual(sidecar.read_bytes(), before)
                sidecar.unlink()

    def test_preflight_refuses_non_regular_sidecar_without_following_it(self) -> None:
        sidecar = Path(f"{self.database_path}-shm")
        sidecar.mkdir()
        try:
            with self.assertRaisesRegex(
                DatabaseMigrationError,
                "not an unaliased regular file",
            ):
                build_migration_manifest(self.database_path)
            self.assertTrue(sidecar.is_dir())
        finally:
            sidecar.rmdir()

        target = self.root / "sidecar-target"
        target.write_bytes(b"sidecar-target")
        try:
            os.symlink(target, sidecar)
        except OSError as exc:  # pragma: no cover - Windows without symlink rights
            self.skipTest(f"symlinks unavailable in system temp: {exc}")
        try:
            with self.assertRaisesRegex(
                DatabaseMigrationError,
                "not an unaliased regular file",
            ):
                build_migration_manifest(self.database_path)
            self.assertTrue(sidecar.is_symlink())
            self.assertEqual(target.read_bytes(), b"sidecar-target")
        finally:
            sidecar.unlink()

    def test_configured_store_requires_authorization_outside_isolated_mode(self) -> None:
        source_sha256 = file_sha256(self.database_path)
        with mock.patch("backend.store.DATABASE_PATH", self.database_path), mock.patch(
            "backend.store.tempfile.gettempdir",
            return_value=str(self.root / "different-system-temp"),
        ), mock.patch.dict(os.environ, {"AI_STUDIO_SKIP_LOCAL_ENV": "1"}):
            with self.assertRaisesRegex(RuntimeError, "isolated tests"):
                StudioStore(self.database_path)
            with self.assertRaises(TypeError):
                StudioStore(self.database_path, initialize_schema=False)
            with self.assertRaises(TypeError):
                StudioStore(self.database_path, schema_change_authorized=True)
        self.assertEqual(file_sha256(self.database_path), source_sha256)

    def test_prepare_refuses_live_owner_and_commit_refuses_source_drift(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute("DROP INDEX idx_messages_room_keyset")
        checkpoint(self.database_path)
        manifest_path = write_migration_manifest(
            build_migration_manifest(self.database_path),
            self.root / "drift-manifest.json",
        )
        owner = DatabaseInstanceOwner(self.database_path).acquire()
        try:
            with self.assertRaises(InstanceAlreadyRunning):
                prepare_migration(
                    database_path=self.database_path,
                    manifest_path=manifest_path,
                    backup_path=self.root / "blocked-backup.sqlite3",
                    candidate_path=self.root / "blocked-candidate.sqlite3",
                    prepared_path=self.root / "blocked-prepared.json",
                )
        finally:
            owner.release()

        prepared_path = self.root / "drift-prepared.json"
        prepared = prepare_migration(
            database_path=self.database_path,
            manifest_path=manifest_path,
            backup_path=self.root / "drift-backup.sqlite3",
            candidate_path=self.root / "drift-candidate.sqlite3",
            prepared_path=prepared_path,
        )
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute(
                "INSERT INTO schema_migrations(key,applied_at) VALUES(?,?)",
                ("source_changed_after_prepare", 1),
            )
        checkpoint(self.database_path)

        with self.assertRaisesRegex(DatabaseMigrationError, "changed after backup"):
            apply_authorized_migration(
                database_path=self.database_path,
                prepared_path=prepared_path,
                authorization_token=prepared["authorization_token"],
                receipt_path=self.root / "drift-receipt.json",
            )

    def test_migration_artifacts_cannot_overlap_source_or_peer_file_families(
        self,
    ) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute("DROP INDEX idx_messages_room_keyset")
        checkpoint(self.database_path)
        source_sha256 = file_sha256(self.database_path)
        manifest = build_migration_manifest(self.database_path, migration_epoch_ms=123456)

        for suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(kind="manifest-source-family", suffix=suffix):
                target = Path(f"{self.database_path}{suffix}")
                with self.assertRaisesRegex(DatabaseMigrationError, "file family overlaps"):
                    write_migration_manifest(manifest, target)
                self.assertFalse(target.exists())
                self.assertEqual(file_sha256(self.database_path), source_sha256)

        manifest_path = write_migration_manifest(
            manifest,
            self.root / "safe-manifest.json",
        )
        source_family_cases = {
            "backup": Path(f"{self.database_path}-wal"),
            "candidate": Path(f"{self.database_path}-shm"),
            "prepared": Path(f"{self.database_path}-journal"),
        }
        for label, target in source_family_cases.items():
            with self.subTest(kind="prepare-source-family", label=label):
                backup = target if label == "backup" else self.root / f"{label}-backup.sqlite3"
                candidate = target if label == "candidate" else self.root / f"{label}-candidate.sqlite3"
                prepared_path = target if label == "prepared" else self.root / f"{label}-prepared.json"
                with self.assertRaisesRegex(DatabaseMigrationError, "file family overlaps"):
                    prepare_migration(
                        database_path=self.database_path,
                        manifest_path=manifest_path,
                        backup_path=backup,
                        candidate_path=candidate,
                        prepared_path=prepared_path,
                    )
                self.assertFalse(target.exists())
                self.assertFalse(backup.exists())
                self.assertFalse(candidate.exists())
                self.assertFalse(prepared_path.exists())
                self.assertEqual(file_sha256(self.database_path), source_sha256)

        with self.assertRaisesRegex(DatabaseMigrationError, "file family overlaps"):
            prepare_migration(
                database_path=self.database_path,
                manifest_path=manifest_path,
                backup_path=self.root / "peer.sqlite3",
                candidate_path=self.root / "peer.sqlite3-wal",
                prepared_path=self.root / "peer-prepared.json",
            )
        self.assertFalse((self.root / "peer.sqlite3").exists())
        self.assertFalse((self.root / "peer.sqlite3-wal").exists())

        occupied_backup = self.root / "occupied-backup.sqlite3"
        occupied_sidecar = Path(f"{occupied_backup}-wal")
        occupied_sidecar.write_bytes(b"preserve recovery evidence")
        with self.assertRaisesRegex(DatabaseMigrationError, "file family already exists"):
            prepare_migration(
                database_path=self.database_path,
                manifest_path=manifest_path,
                backup_path=occupied_backup,
                candidate_path=self.root / "occupied-candidate.sqlite3",
                prepared_path=self.root / "occupied-prepared.json",
            )
        self.assertEqual(occupied_sidecar.read_bytes(), b"preserve recovery evidence")
        self.assertFalse(occupied_backup.exists())
        self.assertEqual(file_sha256(self.database_path), source_sha256)

    def test_migration_artifact_outputs_reject_symlinked_parent_chain(self) -> None:
        parent_alias = self.root / "artifact-parent-symlink"
        try:
            os.symlink(self.root, parent_alias, target_is_directory=True)
        except OSError as exc:  # pragma: no cover - Windows without symlink rights
            self.skipTest(f"directory symlinks unavailable in system temp: {exc}")

        manifest = build_migration_manifest(self.database_path)
        safe_manifest_path = self.root / "artifact-chain-safe-manifest.json"
        write_migration_manifest(manifest, safe_manifest_path)
        source_sha256 = file_sha256(self.database_path)
        try:
            with self.assertRaisesRegex(
                DatabaseMigrationError,
                "path may not contain a symlink or reparse point",
            ):
                write_migration_manifest(manifest, parent_alias / "manifest.json")

            with self.assertRaisesRegex(
                DatabaseMigrationError,
                "path may not contain a symlink or reparse point",
            ):
                prepare_migration(
                    database_path=self.database_path,
                    manifest_path=safe_manifest_path,
                    backup_path=parent_alias / "backup.sqlite3",
                    candidate_path=self.root / "artifact-chain-candidate.sqlite3",
                    prepared_path=self.root / "artifact-chain-prepared.json",
                )

            with self.assertRaisesRegex(
                DatabaseMigrationError,
                "path may not contain a symlink or reparse point",
            ):
                apply_authorized_migration(
                    database_path=self.database_path,
                    prepared_path=parent_alias / "prepared.json",
                    authorization_token="invalid",
                    receipt_path=self.root / "artifact-chain-receipt.json",
                )
            self.assertEqual(file_sha256(self.database_path), source_sha256)
            self.assertFalse((self.root / "manifest.json").exists())
            self.assertFalse((self.root / "backup.sqlite3").exists())
            self.assertFalse((self.root / "prepared.json").exists())
        finally:
            parent_alias.unlink()

    def test_formal_preflight_rejects_hard_linked_source_identity(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute("DROP INDEX idx_messages_room_keyset")
        checkpoint(self.database_path)
        alias_path = self.root / "source-hard-link-alias.sqlite3"
        os.link(self.database_path, alias_path)
        source_sha256 = file_sha256(self.database_path)

        with self.assertRaisesRegex(DatabaseMigrationError, "hard-linked"):
            build_migration_manifest(self.database_path, migration_epoch_ms=123456)

        self.assertEqual(self.database_path.stat().st_nlink, 2)
        self.assertEqual(file_sha256(self.database_path), source_sha256)

    def test_formal_preflight_rejects_symlinked_source_identity(self) -> None:
        alias_path = self.root / "source-symlink-alias.sqlite3"
        try:
            os.symlink(self.database_path, alias_path)
        except OSError as exc:  # pragma: no cover - Windows without symlink rights
            self.skipTest(f"symlinks unavailable in system temp: {exc}")
        source_sha256 = file_sha256(self.database_path)
        with self.assertRaisesRegex(
            DatabaseMigrationError,
            "symlink or reparse point",
        ):
            build_migration_manifest(alias_path)
        self.assertTrue(alias_path.is_symlink())
        self.assertEqual(file_sha256(self.database_path), source_sha256)
        alias_path.unlink()

    def test_formal_preflight_rejects_symlinked_source_parent_chain(self) -> None:
        parent_alias = self.root / "source-parent-symlink"
        try:
            os.symlink(self.database_path.parent, parent_alias, target_is_directory=True)
        except OSError as exc:  # pragma: no cover - Windows without symlink rights
            self.skipTest(f"directory symlinks unavailable in system temp: {exc}")
        alias_path = parent_alias / self.database_path.name
        source_sha256 = file_sha256(self.database_path)
        try:
            with self.assertRaisesRegex(
                DatabaseMigrationError,
                "symlink or reparse point",
            ):
                build_migration_manifest(alias_path)
            self.assertEqual(file_sha256(self.database_path), source_sha256)
        finally:
            parent_alias.unlink()

    def test_missing_database_preflight_does_not_create_it(self) -> None:
        missing = self.root / "missing.sqlite3"
        with self.assertRaisesRegex(DatabaseMigrationError, "does not exist"):
            assert_database_ready_for_startup(missing)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
