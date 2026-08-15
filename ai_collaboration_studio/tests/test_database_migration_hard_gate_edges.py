from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest import mock

import backend.database_migration as migration_module
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
    MigrationIntentJournal,
    MigrationIntentJournalError,
    scan_active_migration_operations,
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


@unittest.skipUnless(os.name == "nt", "Windows migration hard-gate edges")
class WindowsDatabaseMigrationHardGateEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-migration-hard-gate-edge-test-"
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

    def _operation_markers(self, operation_id: str) -> dict[str, bytes]:
        prefix = (
            f".{self.database_path.name}.migration-{operation_id}.".casefold()
        )
        return {
            child.name: child.read_bytes()
            for child in self.root.iterdir()
            if child.is_file()
            and child.name.casefold().startswith(prefix)
            and child.suffix.casefold() == ".json"
        }

    def _leave_candidate_pending(
        self,
        label: str,
    ) -> tuple[dict[str, object], Path, Path]:
        prepared, prepared_path, receipt_path = self._prepare(label)
        with mock.patch(
            "backend.database_migration._write_json_exclusive",
            side_effect=OSError("injected receipt publication failure"),
        ):
            with self.assertRaises(DatabaseMigrationRecoveryRequired) as caught:
                apply_authorized_migration(
                    database_path=self.database_path,
                    prepared_path=prepared_path,
                    authorization_token=str(prepared["authorization_token"]),
                    receipt_path=receipt_path,
                )
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertFalse(receipt_path.exists())
        status = recover_pending_database_migration(
            database_path=self.database_path,
            operation_id=str(prepared["prepared_sha256"]),
            action="inspect",
        )
        self.assertEqual(status["classification"], "CANDIDATE")
        self.assertEqual(status["last_event"], "verified")
        return prepared, prepared_path, receipt_path

    def _leave_existing_receipt_pending(
        self,
        label: str,
    ) -> tuple[dict[str, object], Path, Path]:
        prepared, prepared_path, receipt_path = self._leave_candidate_pending(label)
        operation_id = str(prepared["prepared_sha256"])
        original_append = migration_module._append_migration_gate_event

        def fail_after_receipt_publication(
            journal: MigrationIntentJournal,
            event: str,
            details: dict[str, object] | None = None,
        ) -> dict[str, object]:
            if event == "receipt_committed":
                raise OSError("injected receipt marker publication failure")
            return original_append(journal, event, details)

        with mock.patch(
            "backend.database_migration._append_migration_gate_event",
            new=fail_after_receipt_publication,
        ):
            with self.assertRaises(DatabaseMigrationRecoveryRequired):
                recover_pending_database_migration(
                    database_path=self.database_path,
                    operation_id=operation_id,
                    action="finalize",
                    authorization_token=str(prepared["authorization_token"]),
                )
        self.assertTrue(receipt_path.is_file())
        self.assertEqual(
            MigrationIntentJournal(self.database_path, operation_id)
            .inspect()
            .get("last_event"),
            "recovery_verified",
        )
        return prepared, prepared_path, receipt_path

    def test_apply_rejects_reserved_receipt_targets_before_intent_without_mutation(
        self,
    ) -> None:
        prepared, prepared_path, _receipt_path = self._prepare("reserved-receipt")
        operation_id = str(prepared["prepared_sha256"])
        atomic_rollback = self.root / (
            f".{self.database_path.name}.migration-{operation_id}.source-before.sqlite3"
        )
        failed_candidate = self.root / (
            f".{self.database_path.name}.migration-{operation_id}.failed-candidate.sqlite3"
        )
        case_variant_marker = self.root / (
            f".{self.database_path.name.swapcase()}.MiGrAtIoN-{operation_id.upper()}."
            "000000-InTeNt.json"
        )
        targets = {
            "source": self.database_path,
            "source_wal": Path(f"{self.database_path}-wal"),
            "source_shm": Path(f"{self.database_path}-shm"),
            "source_journal": Path(f"{self.database_path}-journal"),
            "atomic_rollback": atomic_rollback,
            "failed_candidate": failed_candidate,
            "case_variant_marker_namespace": case_variant_marker,
        }
        source_before = _file_sha256(self.database_path)
        markers_before = self._operation_markers(operation_id)

        for label, target in targets.items():
            with self.subTest(target=label):
                target_existed = target.exists()
                with self.assertRaises(DatabaseMigrationError):
                    apply_authorized_migration(
                        database_path=self.database_path,
                        prepared_path=prepared_path,
                        authorization_token=str(prepared["authorization_token"]),
                        receipt_path=target,
                    )
                self.assertEqual(_file_sha256(self.database_path), source_before)
                self.assertEqual(
                    self._operation_markers(operation_id),
                    markers_before,
                )
                self.assertEqual(target.exists(), target_existed)

    def test_apply_rejects_preexisting_recovery_file_families_before_intent(
        self,
    ) -> None:
        prepared, prepared_path, receipt_path = self._prepare("reserved-family")
        operation_id = str(prepared["prepared_sha256"])
        bases = {
            "atomic_rollback": self.root
            / (
                f".{self.database_path.name}.migration-{operation_id}."
                "source-before.sqlite3"
            ),
            "failed_candidate": self.root
            / (
                f".{self.database_path.name}.migration-{operation_id}."
                "failed-candidate.sqlite3"
            ),
        }
        source_before = _file_sha256(self.database_path)

        for base_label, base_path in bases.items():
            for suffix in ("", "-wal", "-shm", "-journal"):
                conflict = Path(f"{base_path}{suffix}")
                sentinel = f"{base_label}{suffix or '-main'}".encode("utf-8")
                with self.subTest(family=base_label, suffix=suffix or "main"):
                    conflict.write_bytes(sentinel)
                    try:
                        markers_before = self._operation_markers(operation_id)
                        with self.assertRaisesRegex(
                            DatabaseMigrationError,
                            "file family already exists",
                        ):
                            apply_authorized_migration(
                                database_path=self.database_path,
                                prepared_path=prepared_path,
                                authorization_token=str(
                                    prepared["authorization_token"]
                                ),
                                receipt_path=receipt_path,
                            )
                        self.assertEqual(
                            _file_sha256(self.database_path),
                            source_before,
                        )
                        self.assertEqual(conflict.read_bytes(), sentinel)
                        self.assertEqual(
                            self._operation_markers(operation_id),
                            markers_before,
                        )
                        self.assertFalse(receipt_path.exists())
                    finally:
                        conflict.unlink(missing_ok=True)

    def test_apply_holds_backup_and_candidate_leases_through_receipt(self) -> None:
        original_lease = migration_module.hold_sqlite_file_lease

        for target_name in ("backup", "candidate"):
            with self.subTest(target=target_name):
                prepared, prepared_path, receipt_path = self._prepare(
                    f"lease-drift-{target_name}"
                )
                target_path = Path(
                    prepared[target_name]["path"]
                ).expanduser().resolve()
                operation_id = str(prepared["prepared_sha256"])
                source_before = _file_sha256(self.database_path)
                injected = False

                @contextmanager
                def mutate_then_lease(
                    path: str | Path,
                    *,
                    expected_sha256: str,
                ):
                    nonlocal injected
                    clean_path = Path(path).expanduser().resolve()
                    if clean_path == target_path and not injected:
                        injected = True
                        with closing(sqlite3.connect(clean_path)) as connection, connection:
                            connection.execute(
                                "CREATE TABLE injected_lease_drift(value TEXT)"
                            )
                        _checkpoint(clean_path)
                    with original_lease(
                        clean_path,
                        expected_sha256=expected_sha256,
                    ) as lease:
                        yield lease

                with mock.patch(
                    "backend.database_migration.hold_sqlite_file_lease",
                    new=mutate_then_lease,
                ):
                    with self.assertRaises(DatabaseMigrationError):
                        apply_authorized_migration(
                            database_path=self.database_path,
                            prepared_path=prepared_path,
                            authorization_token=str(
                                prepared["authorization_token"]
                            ),
                            receipt_path=receipt_path,
                        )
                self.assertTrue(injected)
                self.assertEqual(_file_sha256(self.database_path), source_before)
                self.assertFalse(receipt_path.exists())
                self.assertEqual(self._operation_markers(operation_id), {})

    def test_rollback_receipt_failure_is_classified_and_exact_retry_closes_once(
        self,
    ) -> None:
        prepared, _prepared_path, receipt_path = self._leave_candidate_pending(
            "rollback-receipt-retry"
        )
        operation_id = str(prepared["prepared_sha256"])
        token = str(prepared["authorization_token"])
        source_before_sha256 = str(prepared["source"]["file"]["sha256"])
        candidate_sha256 = str(
            prepared["candidate"]["snapshot"]["file"]["sha256"]
        )

        with mock.patch(
            "backend.database_migration._write_json_exclusive",
            side_effect=OSError("injected rollback receipt publication failure"),
        ):
            with self.assertRaises(DatabaseMigrationRecoveryRequired):
                recover_pending_database_migration(
                    database_path=self.database_path,
                    operation_id=operation_id,
                    action="rollback",
                    authorization_token=token,
                )

        self.assertEqual(_file_sha256(self.database_path), source_before_sha256)
        self.assertFalse(receipt_path.exists())
        status = recover_pending_database_migration(
            database_path=self.database_path,
            operation_id=operation_id,
            action="inspect",
        )
        self.assertEqual(status["classification"], "ROLLED_BACK_PENDING_RECEIPT")
        failed_candidate_path = Path(status["failed_candidate_path"])
        self.assertEqual(_file_sha256(failed_candidate_path), candidate_sha256)
        markers_before_rejections = self._operation_markers(operation_id)

        for action in ("abort", "finalize"):
            with self.subTest(action=action):
                with self.assertRaises(DatabaseMigrationError):
                    recover_pending_database_migration(
                        database_path=self.database_path,
                        operation_id=operation_id,
                        action=action,
                        authorization_token=token,
                    )
                self.assertEqual(
                    self._operation_markers(operation_id),
                    markers_before_rejections,
                )
                self.assertEqual(
                    _file_sha256(self.database_path),
                    source_before_sha256,
                )

        with mock.patch(
            "backend.database_migration.replace_file_with_backup",
            side_effect=AssertionError("rollback retry must not replace either image again"),
        ) as replace_again:
            result = recover_pending_database_migration(
                database_path=self.database_path,
                operation_id=operation_id,
                action="rollback",
                authorization_token=token,
            )
        replace_again.assert_not_called()
        self.assertEqual(result["outcome"], "rolled_back")
        self.assertEqual(_file_sha256(self.database_path), source_before_sha256)
        self.assertEqual(_file_sha256(failed_candidate_path), candidate_sha256)
        rollback_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        stored_sha256 = rollback_receipt.pop("receipt_sha256")
        self.assertEqual(stored_sha256, _value_sha256(rollback_receipt))
        terminal = MigrationIntentJournal(
            self.database_path,
            operation_id,
        ).inspect()
        self.assertFalse(terminal["active"])
        self.assertEqual(terminal["terminal_event"], "rolled_back")

    def test_existing_receipt_rehash_cannot_hide_binding_tamper(self) -> None:
        prepared, _prepared_path, receipt_path = self._leave_existing_receipt_pending(
            "existing-receipt-binding"
        )
        operation_id = str(prepared["prepared_sha256"])
        token = str(prepared["authorization_token"])
        valid_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        candidate_sha256 = str(
            prepared["candidate"]["snapshot"]["file"]["sha256"]
        )

        def alter_plan(receipt: dict[str, object]) -> None:
            receipt["plan_sha256"] = "0" * 64

        def alter_backup(receipt: dict[str, object]) -> None:
            backup = receipt["backup"]
            assert isinstance(backup, dict)
            backup["path"] = str(prepared["candidate"]["path"])

        def alter_intent_marker(receipt: dict[str, object]) -> None:
            intent_chain = receipt["intent_chain"]
            assert isinstance(intent_chain, dict)
            intent_chain["verified_marker_sha256"] = "f" * 64

        tamperers = {
            "plan": alter_plan,
            "backup": alter_backup,
            "intent_marker": alter_intent_marker,
        }

        for label, tamper in tamperers.items():
            with self.subTest(binding=label):
                modified = copy.deepcopy(valid_receipt)
                tamper(modified)
                modified.pop("receipt_sha256", None)
                modified["receipt_sha256"] = _value_sha256(modified)
                receipt_path.write_text(
                    json.dumps(
                        modified,
                        ensure_ascii=False,
                        sort_keys=True,
                        indent=2,
                        allow_nan=False,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                markers_before = self._operation_markers(operation_id)
                try:
                    with self.assertRaises(DatabaseMigrationError):
                        recover_pending_database_migration(
                            database_path=self.database_path,
                            operation_id=operation_id,
                            action="finalize",
                            authorization_token=token,
                        )
                    self.assertEqual(
                        self._operation_markers(operation_id),
                        markers_before,
                    )
                    self.assertEqual(
                        _file_sha256(self.database_path),
                        candidate_sha256,
                    )
                    self.assertEqual(
                        [
                            item["operation_id"]
                            for item in scan_active_migration_operations(
                                self.database_path
                            )
                        ],
                        [operation_id],
                    )
                finally:
                    receipt_path.write_text(
                        json.dumps(
                            valid_receipt,
                            ensure_ascii=False,
                            sort_keys=True,
                            indent=2,
                            allow_nan=False,
                        )
                        + "\n",
                        encoding="utf-8",
                    )

        result = recover_pending_database_migration(
            database_path=self.database_path,
            operation_id=operation_id,
            action="finalize",
            authorization_token=token,
        )
        self.assertEqual(result["outcome"], "finalized")

    def test_direct_terminal_marker_cannot_release_scan_or_startup(self) -> None:
        prepared, _prepared_path, receipt_path = self._leave_candidate_pending(
            "direct-terminal"
        )
        operation_id = str(prepared["prepared_sha256"])
        journal = MigrationIntentJournal(self.database_path, operation_id)
        fake_receipt_details = {
            "receipt_path": str(receipt_path),
            "receipt_sha256": hashlib.sha256(b"missing receipt").hexdigest(),
        }

        try:
            journal.append("receipt_committed", fake_receipt_details)
            journal.append("complete", fake_receipt_details)
        except MigrationIntentJournalError:
            pass

        self.assertFalse(receipt_path.exists())
        self.assertEqual(
            [
                item["operation_id"]
                for item in scan_active_migration_operations(self.database_path)
            ],
            [operation_id],
        )
        with self.assertRaises(DatabaseMigrationRecoveryRequired):
            assert_database_ready_for_startup(self.database_path)


if __name__ == "__main__":
    unittest.main()
