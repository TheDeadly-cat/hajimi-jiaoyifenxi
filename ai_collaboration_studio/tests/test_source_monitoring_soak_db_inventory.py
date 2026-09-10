from __future__ import annotations

import copy
import hashlib
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.source_monitoring.contracts import canonical_json, canonical_sha256  # noqa: E402
from backend.source_inbox_contracts import (  # noqa: E402
    SOURCE_STATUS_VALIDATED,
    build_source_import_receipt,
    normalize_source_import_packet,
)
from backend.source_monitoring.packet_builder import build_source_import_packet  # noqa: E402
import backend.source_monitoring.soak_db_inventory as inventory_module  # noqa: E402
from backend.source_monitoring.soak_db_inventory import (  # noqa: E402
    SOAK_DB_RUN_EVIDENCE_VERSION,
    MAX_SOAK_DB_VERDICT_ISSUES,
    SOAK_DB_INVENTORY_VERDICT_VERSION,
    SOAK_DB_INVENTORY_VERSION,
    SoakDbInventoryError,
    build_soak_db_inventory,
    load_soak_db_inventory,
    read_soak_db_run_evidence,
    validate_soak_db_inventory_delta,
    write_soak_db_inventory_exclusive,
)
from backend.source_monitoring.state_repository import (  # noqa: E402
    RUN_STATUS_SUCCEEDED,
    SourceMonitoringStateRepository,
)
from backend.store import StudioStore  # noqa: E402


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _family_signature(path: Path) -> dict[str, tuple[int, str] | None]:
    result: dict[str, tuple[int, str] | None] = {}
    for suffix in ("", "-wal", "-shm", "-journal"):
        member = Path(f"{path}{suffix}")
        result[suffix or "main"] = (
            (member.stat().st_size, _digest(member)) if member.exists() else None
        )
    return result


class SourceMonitoringSoakDbInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="ai-studio-soak-db-inventory-"
        )
        self.database_path = Path(self.temporary.name) / "studio.sqlite3"
        self._create_schema()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _create_schema(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE source_adapter_runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    receipt_id TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    completed_at_ms INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE source_inbox_imports (
                    id TEXT PRIMARY KEY,
                    record_version TEXT NOT NULL,
                    source_channel TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    external_run_id TEXT NOT NULL,
                    import_key_sha256 TEXT NOT NULL,
                    source_payload_bytes INTEGER NOT NULL,
                    source_payload_sha256 TEXT NOT NULL,
                    normalized_packet_sha256 TEXT NOT NULL,
                    packet_json TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    receipt_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    received_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                """
            )

    def _insert_run(
        self,
        run_id: str,
        *,
        status: str = "SUCCEEDED",
        receipt_id: str = "",
        note: str = "",
        completed_at_ms: int = 1,
    ) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO source_adapter_runs(
                       run_id,status,receipt_id,note,completed_at_ms
                   ) VALUES(?,?,?,?,?)""",
                (run_id, status, receipt_id, note, completed_at_ms),
            )

    def _insert_receipt(self, receipt_id: str, run_id: str) -> str:
        received_at = 1_900_000_000_000
        packet = normalize_source_import_packet(
            build_source_import_packet(
                adapter_key="fixture_adapter",
                external_run_id=run_id,
                captured_at_ms=received_at,
                observed_items=[],
            ),
            received_at_ms=received_at,
        )
        source_payload_sha256 = hashlib.sha256(b"").hexdigest()
        receipt = build_source_import_receipt(
            packet,
            received_at_ms=received_at,
            source_payload_bytes=0,
            source_payload_sha256=source_payload_sha256,
            status=SOURCE_STATUS_VALIDATED,
        )
        receipt_sha256 = receipt["receipt_sha256"]
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO source_inbox_imports(
                       id,record_version,source_channel,source_key,external_run_id,
                       import_key_sha256,source_payload_bytes,source_payload_sha256,
                       normalized_packet_sha256,packet_json,receipt_json,receipt_sha256,
                       status,received_at,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    receipt_id,
                    "source_inbox_import_record_v1",
                    packet["source_channel"],
                    packet["source_key"],
                    run_id,
                    receipt["import_key_sha256"],
                    0,
                    source_payload_sha256,
                    receipt["normalized_packet_sha256"],
                    canonical_json(packet),
                    canonical_json(receipt),
                    receipt_sha256,
                    SOURCE_STATUS_VALIDATED,
                    received_at,
                    received_at,
                ),
            )
        return receipt_sha256

    @staticmethod
    def _declaration(
        inventory: dict[str, object],
        run_id: str,
        *,
        status: str | None = None,
    ) -> dict[str, object]:
        entries = inventory["runs"]
        assert isinstance(entries, list)
        entry = next(item for item in entries if item["run_id"] == run_id)
        return {
            "run_id": run_id,
            "status": entry["status"] if status is None else status,
            "state_recorded": True,
            "run_record_sha256": entry["row_sha256"],
            "import_receipt_sha256": entry["receipt_sha256"],
        }

    def test_full_raw_rows_and_receipts_are_sealed_without_path_disclosure(self) -> None:
        receipt_sha256 = self._insert_receipt("receipt_1", "run_002")
        self._insert_run("run_002", receipt_id="receipt_1", note="second")
        self._insert_run("run_001", note="first")
        before = _family_signature(self.database_path)

        inventory = build_soak_db_inventory(self.database_path, page_size=1)

        self.assertEqual(inventory["version"], SOAK_DB_INVENTORY_VERSION)
        self.assertEqual(inventory["scan_order"], "run_id_asc_keyset_v1")
        self.assertEqual(inventory["scan_page_count"], 2)
        self.assertEqual(inventory["run_count"], 2)
        self.assertEqual(inventory["receipt_count"], 1)
        self.assertEqual(
            [entry["run_id"] for entry in inventory["runs"]],
            ["run_001", "run_002"],
        )
        expected_raw = {
            "run_id": "run_002",
            "status": "SUCCEEDED",
            "receipt_id": "receipt_1",
            "note": "second",
            "completed_at_ms": 1,
        }
        self.assertEqual(
            inventory["runs"][1]["row_sha256"],
            canonical_sha256(expected_raw),
        )
        self.assertEqual(
            inventory["runs"][1]["receipt_sha256"],
            receipt_sha256,
        )
        self.assertNotIn("database_path", inventory)
        self.assertEqual(inventory["safety"]["database_writes_performed"], 0)
        self.assertEqual(inventory["safety"]["network_requests_performed"], 0)
        self.assertEqual(_family_signature(self.database_path), before)

    def test_keyset_scan_is_complete_beyond_repository_list_limit(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                """INSERT INTO source_adapter_runs(
                       run_id,status,receipt_id,note,completed_at_ms
                   ) VALUES(?,?,?,?,?)""",
                (
                    (f"run_{index:04d}", "SUCCEEDED", "", "", index)
                    for index in range(1_005)
                ),
            )

        inventory = build_soak_db_inventory(self.database_path, page_size=500)

        self.assertEqual(inventory["run_count"], 1_005)
        self.assertEqual(inventory["scan_page_count"], 3)
        self.assertEqual(inventory["runs"][0]["run_id"], "run_0000")
        self.assertEqual(inventory["runs"][-1]["run_id"], "run_1004")
        self.assertEqual(len({entry["run_id"] for entry in inventory["runs"]}), 1_005)

    def test_inventory_run_limit_is_checked_before_paged_row_materialization(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                """INSERT INTO source_adapter_runs(
                       run_id,status,receipt_id,note,completed_at_ms
                   ) VALUES(?,?,?,?,?)""",
                (
                    ("run_1", "SUCCEEDED", "", "", 1),
                    ("run_2", "SUCCEEDED", "", "", 2),
                    ("run_3", "SUCCEEDED", "", "", 3),
                ),
            )

        with patch.object(inventory_module, "MAX_SOAK_DB_INVENTORY_RUNS", 2):
            with self.assertRaises(SoakDbInventoryError) as captured:
                build_soak_db_inventory(self.database_path, page_size=1)

        self.assertEqual(
            captured.exception.code,
            "SOAK_DB_INVENTORY_RUN_LIMIT_EXCEEDED",
        )

    def test_keyset_boundary_uses_binary_collation_even_when_column_is_nocase(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DROP TABLE source_adapter_runs")
            connection.execute(
                """CREATE TABLE source_adapter_runs (
                       run_id TEXT COLLATE NOCASE PRIMARY KEY,
                       status TEXT NOT NULL,
                       receipt_id TEXT NOT NULL DEFAULT '',
                       note TEXT NOT NULL DEFAULT '',
                       completed_at_ms INTEGER NOT NULL DEFAULT 0
                   )"""
            )
            connection.executemany(
                """INSERT INTO source_adapter_runs(
                       run_id,status,receipt_id,note,completed_at_ms
                   ) VALUES(?,?,?,?,?)""",
                (
                    ("B", "SUCCEEDED", "", "", 1),
                    ("a", "SUCCEEDED", "", "", 2),
                ),
            )

        inventory = build_soak_db_inventory(self.database_path, page_size=1)

        self.assertEqual(
            [entry["run_id"] for entry in inventory["runs"]],
            ["B", "a"],
        )
        self.assertEqual(inventory["scan_page_count"], 2)

    def test_real_source_monitoring_schema_full_row_hash_matches_sqlite_row(self) -> None:
        actual_path = Path(self.temporary.name) / "actual-studio.sqlite3"
        repository = SourceMonitoringStateRepository(
            StudioStore(actual_path),
            clock_ms=lambda: 1_900_000_000_000,
        )
        repository.set_enabled(
            "fixture_adapter",
            config_version="fixture_config_v1",
            enabled=True,
        )
        started = repository.start_run(
            "fixture_adapter",
            config_version="fixture_config_v1",
            dry_run=False,
        )
        repository.complete_run(
            started["run"]["run_id"],
            next_checkpoint={},
            status=RUN_STATUS_SUCCEEDED,
            observed_count=0,
            accepted_count=0,
            duplicate_count=0,
            rejected_count=0,
            next_due_at_ms=1_900_000_060_000,
        )
        inventory = build_soak_db_inventory(actual_path, page_size=7)
        with closing(sqlite3.connect(actual_path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM source_adapter_runs WHERE run_id=?",
                (started["run"]["run_id"],),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(inventory["run_count"], 1)
        self.assertEqual(
            inventory["runs"][0]["row_sha256"],
            canonical_sha256(dict(row)),
        )
        evidence = read_soak_db_run_evidence(
            actual_path,
            started["run"]["run_id"],
        )
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertEqual(evidence["version"], SOAK_DB_RUN_EVIDENCE_VERSION)
        self.assertEqual(evidence["adapter_key"], "fixture_adapter")
        self.assertEqual(evidence["status"], "SUCCEEDED")
        self.assertEqual(evidence["row_sha256"], inventory["runs"][0]["row_sha256"])
        self.assertEqual(evidence["receipt_sha256"], "")
        self.assertEqual(evidence["observed_count"], 0)
        self.assertNotIn("config_version", evidence)
        self.assertEqual(
            read_soak_db_run_evidence(
                actual_path,
                "source_run_" + "0" * 32,
            ),
            None,
        )
        with self.assertRaises(SoakDbInventoryError) as invalid_id:
            read_soak_db_run_evidence(actual_path, "fixture_run")
        self.assertEqual(invalid_id.exception.code, "SOAK_DB_RUN_ID_INVALID")

        active = repository.start_run(
            "fixture_adapter",
            config_version="fixture_config_v1",
            dry_run=False,
        )
        with self.assertRaises(SoakDbInventoryError) as running:
            read_soak_db_run_evidence(actual_path, active["run"]["run_id"])
        self.assertEqual(running.exception.code, "SOAK_DB_RUN_NOT_TERMINAL")

    def test_empty_inventory_is_closed_and_self_consistent(self) -> None:
        inventory = build_soak_db_inventory(self.database_path, page_size=17)

        self.assertEqual(inventory["run_count"], 0)
        self.assertEqual(inventory["receipt_count"], 0)
        self.assertEqual(inventory["scan_page_count"], 0)
        self.assertEqual(inventory["runs"], [])
        verdict = validate_soak_db_inventory_delta(
            inventory,
            inventory,
            session_terminal_run_ids=[],
            session_run_declarations=[],
        )
        self.assertEqual(verdict["verdict"], "PASS")

    def test_inventory_artifact_is_canonical_durable_and_never_overwritten(self) -> None:
        self._insert_run("run_artifact")
        inventory = build_soak_db_inventory(self.database_path)
        artifact = Path(self.temporary.name) / "baseline-inventory.json"

        write_receipt = write_soak_db_inventory_exclusive(inventory, artifact)

        self.assertEqual(
            write_receipt["inventory_sha256"],
            inventory["inventory_sha256"],
        )
        self.assertEqual(write_receipt["bytes_written"], artifact.stat().st_size)
        self.assertEqual(
            artifact.read_bytes(),
            canonical_json(inventory).encode("utf-8"),
        )
        self.assertEqual(load_soak_db_inventory(artifact), inventory)
        before = artifact.read_bytes()
        with self.assertRaises(SoakDbInventoryError) as collision:
            write_soak_db_inventory_exclusive(inventory, artifact)
        self.assertEqual(
            collision.exception.code,
            "SOAK_DB_INVENTORY_ARTIFACT_EXISTS",
        )
        self.assertEqual(artifact.read_bytes(), before)

    def test_inventory_artifact_path_size_format_and_alias_fail_closed(self) -> None:
        inventory = build_soak_db_inventory(self.database_path)
        wrong_suffix = Path(self.temporary.name) / "inventory.txt"
        with self.assertRaises(SoakDbInventoryError):
            write_soak_db_inventory_exclusive(inventory, wrong_suffix)
        self.assertFalse(wrong_suffix.exists())

        too_large = Path(self.temporary.name) / "too-large.json"
        with patch.object(inventory_module, "MAX_SOAK_DB_INVENTORY_BYTES", 1):
            with self.assertRaises(SoakDbInventoryError) as size_error:
                write_soak_db_inventory_exclusive(inventory, too_large)
        self.assertEqual(
            size_error.exception.code,
            "SOAK_DB_INVENTORY_ARTIFACT_TOO_LARGE",
        )
        self.assertFalse(too_large.exists())

        noncanonical = Path(self.temporary.name) / "noncanonical.json"
        noncanonical.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(SoakDbInventoryError) as format_error:
            load_soak_db_inventory(noncanonical)
        self.assertEqual(
            format_error.exception.code,
            "SOAK_DB_INVENTORY_ARTIFACT_FORMAT_INVALID",
        )

        artifact = Path(self.temporary.name) / "inventory.json"
        alias = Path(self.temporary.name) / "inventory-alias.json"
        write_soak_db_inventory_exclusive(inventory, artifact)
        try:
            os.link(artifact, alias)
        except OSError:
            return
        with self.assertRaises(SoakDbInventoryError) as alias_error:
            load_soak_db_inventory(artifact)
        self.assertEqual(
            alias_error.exception.code,
            "SOAK_DB_INVENTORY_ARTIFACT_PATH_INVALID",
        )

    def test_wal_snapshot_does_not_mutate_the_source_family(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute(
                """INSERT INTO source_adapter_runs(
                       run_id,status,receipt_id,note,completed_at_ms
                   ) VALUES('run_wal','SUCCEEDED','','wal',10)"""
            )
            connection.commit()
            self.assertTrue(Path(f"{self.database_path}-wal").exists())
            before = _family_signature(self.database_path)

            inventory = build_soak_db_inventory(self.database_path)

            self.assertEqual(inventory["run_count"], 1)
            self.assertEqual(inventory["runs"][0]["run_id"], "run_wal")
            self.assertEqual(_family_signature(self.database_path), before)
        finally:
            connection.close()

    def test_fast_path_rejects_wal_family_created_after_initial_check(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
        self.assertFalse(Path(f"{self.database_path}-wal").exists())
        original_connect = inventory_module._connect_immutable_read_only
        retained_writers: list[sqlite3.Connection] = []

        def connect_then_create_wal(path: Path) -> sqlite3.Connection:
            reader = original_connect(path)
            writer = sqlite3.connect(path)
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute(
                """INSERT INTO source_adapter_runs(
                       run_id,status,receipt_id,note,completed_at_ms
                   ) VALUES('run_race','SUCCEEDED','','',1)"""
            )
            writer.commit()
            retained_writers.append(writer)
            return reader

        try:
            with patch.object(
                inventory_module,
                "_connect_immutable_read_only",
                side_effect=connect_then_create_wal,
            ):
                with self.assertRaises(SoakDbInventoryError) as captured:
                    build_soak_db_inventory(self.database_path)
            self.assertEqual(
                captured.exception.code,
                "SOAK_DB_INVENTORY_SNAPSHOT_BUSY",
            )
        finally:
            for writer in retained_writers:
                writer.close()

    def test_database_sidecar_hardlink_is_rejected_before_snapshot_copy(self) -> None:
        target = Path(self.temporary.name) / "foreign-wal"
        target.write_bytes(b"not-a-wal")
        wal_path = Path(f"{self.database_path}-wal")
        try:
            os.link(target, wal_path)
        except OSError as exc:
            self.skipTest(f"hard links unavailable: {exc}")

        with self.assertRaises(SoakDbInventoryError) as captured:
            build_soak_db_inventory(self.database_path)

        self.assertEqual(
            captured.exception.code,
            "SOAK_DB_INVENTORY_SNAPSHOT_BUSY",
        )

    def test_database_sidecar_size_is_bounded_before_snapshot_copy(self) -> None:
        Path(f"{self.database_path}-wal").write_bytes(b"oversized")

        with patch.object(inventory_module, "MAX_SOAK_DB_WAL_FILE_BYTES", 1):
            with self.assertRaises(SoakDbInventoryError) as captured:
                build_soak_db_inventory(self.database_path)

        self.assertEqual(
            captured.exception.code,
            "SOAK_DB_INVENTORY_SNAPSHOT_BUSY",
        )

    def test_nonempty_receipt_must_exist_and_bind_the_same_run(self) -> None:
        self._insert_run("run_missing", receipt_id="receipt_missing")
        with self.assertRaises(SoakDbInventoryError) as missing:
            build_soak_db_inventory(self.database_path)
        self.assertEqual(missing.exception.code, "SOAK_DB_INVENTORY_RECEIPT_MISSING")

        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM source_adapter_runs")
        self._insert_receipt("receipt_wrong", "another_run")
        self._insert_run("run_mismatch", receipt_id="receipt_wrong")
        with self.assertRaises(SoakDbInventoryError) as mismatch:
            build_soak_db_inventory(self.database_path)
        self.assertEqual(mismatch.exception.code, "SOAK_DB_INVENTORY_RECEIPT_MISMATCH")

    def test_stored_receipt_digest_cannot_hide_corrupt_receipt_content(self) -> None:
        self._insert_receipt("receipt_corrupt", "run_corrupt")
        self._insert_run("run_corrupt", receipt_id="receipt_corrupt")
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """UPDATE source_inbox_imports
                      SET receipt_json='{}'
                    WHERE id='receipt_corrupt'"""
            )

        with self.assertRaises(SoakDbInventoryError) as captured:
            build_soak_db_inventory(self.database_path)

        self.assertEqual(
            captured.exception.code,
            "SOAK_DB_INVENTORY_RECEIPT_INVALID",
        )

    def test_path_page_status_and_raw_value_fail_closed(self) -> None:
        for invalid in (None, True, ""):
            with self.subTest(path=repr(invalid)):
                with self.assertRaises(SoakDbInventoryError):
                    build_soak_db_inventory(invalid)  # type: ignore[arg-type]
        for invalid in (True, 0, 501, 1.5, "10"):
            with self.subTest(page_size=invalid):
                with self.assertRaises(SoakDbInventoryError) as captured:
                    build_soak_db_inventory(
                        self.database_path,
                        page_size=invalid,  # type: ignore[arg-type]
                    )
                self.assertEqual(
                    captured.exception.code,
                    "SOAK_DB_INVENTORY_PAGE_SIZE_INVALID",
                )

        self._insert_run("run_bad_status", status="UNKNOWN")
        with self.assertRaises(SoakDbInventoryError) as status_error:
            build_soak_db_inventory(self.database_path)
        self.assertEqual(
            status_error.exception.code,
            "SOAK_DB_INVENTORY_STATUS_INVALID",
        )

        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM source_adapter_runs")
            connection.execute(
                """INSERT INTO source_adapter_runs(
                       run_id,status,receipt_id,note,completed_at_ms
                   ) VALUES(?,?,?,?,?)""",
                ("run_blob", "SUCCEEDED", "", sqlite3.Binary(b"bad"), 1),
            )
        with self.assertRaises(SoakDbInventoryError) as row_error:
            build_soak_db_inventory(self.database_path)
        self.assertEqual(row_error.exception.code, "SOAK_DB_INVENTORY_ROW_INVALID")

        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM source_adapter_runs")
            connection.execute(
                """INSERT INTO source_adapter_runs(
                       run_id,status,receipt_id,note,completed_at_ms
                   ) VALUES('','SUCCEEDED','','',1)"""
            )
        with self.assertRaises(SoakDbInventoryError) as empty_id_error:
            build_soak_db_inventory(self.database_path)
        self.assertEqual(empty_id_error.exception.code, "SOAK_DB_INVENTORY_INVALID")

    def test_expected_session_delta_passes_with_succeeded_and_skipped_runs(self) -> None:
        self._insert_run("baseline")
        baseline = build_soak_db_inventory(self.database_path)
        self._insert_run("session_1", status="SUCCEEDED", completed_at_ms=2)
        self._insert_run("session_2", status="SKIPPED", completed_at_ms=3)
        final = build_soak_db_inventory(self.database_path)

        verdict = validate_soak_db_inventory_delta(
            baseline,
            final,
            session_terminal_run_ids=["session_2", "session_1"],
            session_run_declarations=[
                self._declaration(final, "session_1"),
                self._declaration(final, "session_2"),
            ],
        )

        self.assertEqual(verdict["version"], SOAK_DB_INVENTORY_VERDICT_VERSION)
        self.assertEqual(verdict["verdict"], "PASS")
        self.assertEqual(verdict["issue_count"], 0)
        self.assertFalse(verdict["issues_truncated"])
        self.assertEqual(verdict["counts"]["added_run_count"], 2)
        self.assertEqual(verdict["counts"]["session_missing_count"], 0)
        self.assertRegex(verdict["verdict_sha256"], r"^[0-9a-f]{64}$")

    def test_delta_detects_mutation_deletion_set_and_terminal_failures(self) -> None:
        self._insert_run("base_keep", note="original")
        self._insert_run("base_delete")
        self._insert_run("base_running", status="RUNNING", completed_at_ms=0)
        baseline = build_soak_db_inventory(self.database_path)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE source_adapter_runs SET note='changed' WHERE run_id='base_keep'"
            )
            connection.execute(
                "DELETE FROM source_adapter_runs WHERE run_id='base_delete'"
            )
            connection.executemany(
                """INSERT INTO source_adapter_runs(
                       run_id,status,receipt_id,note,completed_at_ms
                   ) VALUES(?,?,?,?,?)""",
                (
                    ("session_mismatch", "FAILED", "", "", 2),
                    ("session_running", "RUNNING", "", "", 0),
                    ("session_abandoned", "ABANDONED", "", "", 3),
                    ("session_extra", "SUCCEEDED", "", "", 4),
                ),
            )
        final = build_soak_db_inventory(self.database_path)

        verdict = validate_soak_db_inventory_delta(
            baseline,
            final,
            session_terminal_run_ids=[
                "session_mismatch",
                "session_running",
                "session_abandoned",
                "session_missing",
                "terminal_without_declaration",
            ],
            session_run_declarations=[
                self._declaration(final, "session_mismatch", status="SUCCEEDED"),
                {
                    **self._declaration(final, "session_running"),
                    "run_record_sha256": "3" * 64,
                },
                {
                    **self._declaration(final, "session_abandoned"),
                    "import_receipt_sha256": "2" * 64,
                },
                {
                    "run_id": "session_missing",
                    "status": "SUCCEEDED",
                    "state_recorded": True,
                    "run_record_sha256": "0" * 64,
                    "import_receipt_sha256": "",
                },
                {
                    "run_id": "declaration_only",
                    "status": "SUCCEEDED",
                    "state_recorded": True,
                    "run_record_sha256": "1" * 64,
                    "import_receipt_sha256": "",
                },
                {
                    "run_id": "",
                    "status": "FAILED",
                    "state_recorded": False,
                    "run_record_sha256": "",
                    "import_receipt_sha256": "",
                },
            ],
        )

        self.assertEqual(verdict["verdict"], "FAIL")
        counts = verdict["counts"]
        self.assertEqual(counts["removed_run_count"], 1)
        self.assertEqual(counts["modified_baseline_run_count"], 1)
        self.assertEqual(counts["baseline_running_count"], 1)
        self.assertEqual(counts["terminal_declaration_missing_count"], 1)
        self.assertEqual(counts["declaration_not_terminal_count"], 1)
        self.assertEqual(counts["session_missing_count"], 2)
        self.assertEqual(counts["session_extra_count"], 1)
        self.assertEqual(counts["session_status_mismatch_count"], 1)
        self.assertEqual(counts["session_run_hash_mismatch_count"], 1)
        self.assertEqual(counts["session_receipt_hash_mismatch_count"], 1)
        self.assertEqual(counts["session_running_count"], 1)
        self.assertEqual(counts["session_abandoned_count"], 1)
        self.assertEqual(counts["session_state_not_recorded_count"], 1)
        issue_codes = {issue["code"] for issue in verdict["issues"]}
        self.assertTrue({
            "SOAK_DB_BASELINE_RUN_DELETED",
            "SOAK_DB_BASELINE_RUN_MODIFIED",
            "SOAK_DB_BASELINE_RUN_RUNNING",
            "SOAK_DB_TERMINAL_DECLARATION_MISSING",
            "SOAK_DB_DECLARATION_NOT_TERMINAL",
            "SOAK_DB_SESSION_RUN_MISSING",
            "SOAK_DB_SESSION_RUN_EXTRA",
            "SOAK_DB_SESSION_STATUS_MISMATCH",
            "SOAK_DB_SESSION_RUN_HASH_MISMATCH",
            "SOAK_DB_SESSION_RECEIPT_HASH_MISMATCH",
            "SOAK_DB_SESSION_RUN_RUNNING",
            "SOAK_DB_SESSION_RUN_ABANDONED",
            "SOAK_DB_SESSION_STATE_NOT_RECORDED",
        }.issubset(issue_codes))

    def test_verdict_issue_list_is_bounded_but_count_is_exact(self) -> None:
        baseline = build_soak_db_inventory(self.database_path)
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                """INSERT INTO source_adapter_runs(
                       run_id,status,receipt_id,note,completed_at_ms
                   ) VALUES(?,?,?,?,?)""",
                (
                    (f"extra_{index:03d}", "SUCCEEDED", "", "", index)
                    for index in range(MAX_SOAK_DB_VERDICT_ISSUES + 7)
                ),
            )
        final = build_soak_db_inventory(self.database_path)

        verdict = validate_soak_db_inventory_delta(
            baseline,
            final,
            session_terminal_run_ids=[],
            session_run_declarations=[],
        )

        self.assertEqual(verdict["issue_count"], MAX_SOAK_DB_VERDICT_ISSUES + 7)
        self.assertEqual(len(verdict["issues"]), MAX_SOAK_DB_VERDICT_ISSUES)
        self.assertTrue(verdict["issues_truncated"])

    def test_inventory_and_session_inputs_are_strictly_sealed(self) -> None:
        self._insert_run("run_1")
        inventory = build_soak_db_inventory(self.database_path)
        tampered = copy.deepcopy(inventory)
        tampered["runs"][0]["row_sha256"] = "0" * 64
        with self.assertRaises(SoakDbInventoryError) as seal_error:
            validate_soak_db_inventory_delta(
                inventory,
                tampered,
                session_terminal_run_ids=[],
                session_run_declarations=[],
            )
        self.assertEqual(seal_error.exception.code, "SOAK_DB_INVENTORY_SEAL_INVALID")

        extra_field = copy.deepcopy(inventory)
        extra_field["unexpected"] = True
        with self.assertRaises(SoakDbInventoryError):
            validate_soak_db_inventory_delta(
                extra_field,
                inventory,
                session_terminal_run_ids=[],
                session_run_declarations=[],
            )

        bad_inputs = (
            (["run_1", "run_1"], []),
            (["run_1"], [{"run_id": "run_1", "status": "SUCCEEDED"}]),
            (
                ["run_1"],
                [{"run_id": "run_1", "status": "SUCCEEDED", "state_recorded": 1}],
            ),
        )
        for terminal_ids, declarations in bad_inputs:
            with self.subTest(terminal_ids=terminal_ids, declarations=declarations):
                with self.assertRaises(SoakDbInventoryError) as captured:
                    validate_soak_db_inventory_delta(
                        inventory,
                        inventory,
                        session_terminal_run_ids=terminal_ids,
                        session_run_declarations=declarations,
                    )
                self.assertEqual(
                    captured.exception.code,
                    "SOAK_DB_SESSION_DECLARATION_INVALID",
                )


if __name__ == "__main__":
    unittest.main()
