from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.database_migration import (  # noqa: E402
    DatabaseMigrationRequired,
    assert_database_ready_for_startup,
    build_migration_manifest,
)
from backend.source_inbox_trading_impact import (  # noqa: E402
    SOURCE_INBOX_TRADING_IMPACT_MIGRATION_KEY,
)
from backend.store import StudioStore  # noqa: E402


EXPECTED_SCHEMA_OBJECTS = {
    ("table", "source_inbox_trading_impact_projections"),
    ("index", "uq_source_inbox_trading_impact_item_ruleset"),
    ("index", "uq_source_inbox_trading_impact_projection_key"),
    ("index", "uq_source_inbox_trading_impact_receipt_sha256"),
    ("index", "idx_source_inbox_trading_impact_import"),
    ("index", "idx_source_inbox_trading_impact_ruleset_status"),
    ("trigger", "trg_source_inbox_trading_impact_no_update"),
    ("trigger", "trg_source_inbox_trading_impact_no_delete"),
}


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TradingImpactMigrationPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-impact-migration-preview-"
        )
        self.database_path = Path(self.temp_dir.name) / "studio.sqlite3"
        store = StudioStore(self.database_path)
        store.create_room(
            "Phase 5 migration fixture",
            "Existing records must remain byte-stable across additive preview.",
            capability_pack_ids=[],
        )
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute(
                "DROP TABLE source_inbox_trading_impact_projections"
            )
            connection.execute(
                "DELETE FROM schema_migrations WHERE key=?",
                (SOURCE_INBOX_TRADING_IMPACT_MIGRATION_KEY,),
            )
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_preview_is_read_only_additive_and_has_no_historical_backfill(self) -> None:
        before_sha256 = _file_sha256(self.database_path)
        before_stat = self.database_path.stat()
        with closing(sqlite3.connect(self.database_path)) as connection:
            room_count = int(connection.execute("SELECT COUNT(*) FROM rooms").fetchone()[0])
            item_count = int(
                connection.execute("SELECT COUNT(*) FROM source_inbox_items").fetchone()[0]
            )
            provider_count = int(
                connection.execute("SELECT COUNT(*) FROM provider_call_attempts").fetchone()[0]
            )
            round_count = int(connection.execute("SELECT COUNT(*) FROM rounds").fetchone()[0])

        with self.assertRaises(DatabaseMigrationRequired) as required:
            assert_database_ready_for_startup(self.database_path)
        manifest = required.exception.manifest
        independent = build_migration_manifest(
            self.database_path,
            migration_epoch_ms=manifest["migration_epoch_ms"],
        )

        self.assertTrue(manifest["requires_migration"])
        self.assertEqual(manifest["plan_sha256"], independent["plan_sha256"])
        self.assertEqual(_file_sha256(self.database_path), before_sha256)
        self.assertEqual(self.database_path.stat().st_mtime_ns, before_stat.st_mtime_ns)
        self.assertEqual(manifest["before"]["file"]["sha256"], before_sha256)
        self.assertEqual(manifest["before"]["sqlite"]["integrity_check"], ["ok"])
        self.assertEqual(manifest["before"]["sqlite"]["foreign_key_violation_count"], 0)

        additions = {
            (entry["type"], entry["name"])
            for entry in manifest["changes"]["schema_changes"]
            if entry["action"] == "add"
        }
        self.assertEqual(additions, EXPECTED_SCHEMA_OBJECTS)
        self.assertTrue(all(
            entry["action"] == "add"
            for entry in manifest["changes"]["schema_changes"]
        ))
        self.assertEqual(
            manifest["changes"]["migration_keys_added"],
            [SOURCE_INBOX_TRADING_IMPACT_MIGRATION_KEY],
        )
        self.assertEqual(manifest["changes"]["migration_keys_removed"], [])
        changed_tables = {
            entry["table"] for entry in manifest["changes"]["data_changes"]
        }
        self.assertEqual(
            changed_tables,
            {"schema_migrations", "source_inbox_trading_impact_projections"},
        )
        self.assertEqual(
            manifest["projected_state"]["tables"]
            ["source_inbox_trading_impact_projections"]["row_count"],
            0,
        )

        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM rooms").fetchone()[0], room_count)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM source_inbox_items").fetchone()[0],
                item_count,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM provider_call_attempts").fetchone()[0],
                provider_count,
            )
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM rounds").fetchone()[0], round_count)


if __name__ == "__main__":
    unittest.main()
