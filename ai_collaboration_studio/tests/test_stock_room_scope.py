from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.decision_lineage import canonical_sha256
from backend.stock_research import (
    STOCK_RESEARCH_CAPABILITY_PACK_ID,
    STOCK_ROOM_SCOPE_VERSION,
    StockResearchContractError,
)
from backend.store import StudioStore


def scope(*symbols: str) -> dict[str, object]:
    return {
        "version": STOCK_ROOM_SCOPE_VERSION,
        "symbols": list(symbols),
    }


class StockRoomScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-stock-room-scope-",
            ignore_cleanup_errors=True,
        )
        self.database_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.store = StudioStore(self.database_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create_stock_room(self) -> dict[str, object]:
        return self.store.create_room(
            "Explicit stock pool",
            "Research only the sealed room stock pool.",
            capability_pack_ids=[STOCK_RESEARCH_CAPABILITY_PACK_ID],
            stock_room_scope=scope("us:msft", "US:AAPL"),
        )["room"]

    def test_create_canonicalizes_and_seals_scope_in_room_and_version(self) -> None:
        room = self.create_stock_room()
        expected_scope = scope("US:AAPL", "US:MSFT")
        expected_sha256 = canonical_sha256(expected_scope)

        self.assertEqual(room["stock_room_scope"], expected_scope)
        self.assertEqual(room["stock_room_scope_sha256"], expected_sha256)
        self.assertTrue(room["stock_room_scope_integrity_ok"])
        self.assertEqual(room["stock_room_scope_integrity_issues"], [])
        self.assertEqual(room["settings_version"], 1)

        version = self.store.get_room_version_record(room["id"], 1)
        self.assertIsNotNone(version)
        record = (version or {})["room_version"]
        self.assertTrue(record["integrity_ok"])
        self.assertEqual(record["stock_room_scope"], expected_scope)
        self.assertEqual(record["stock_room_scope_sha256"], expected_sha256)
        self.assertEqual(record["snapshot"]["stock_room_scope"], expected_scope)

        with closing(sqlite3.connect(self.database_path)) as connection:
            persisted = connection.execute(
                """SELECT stock_room_scope_json,stock_room_scope_sha256
                     FROM rooms WHERE id=?""",
                (room["id"],),
            ).fetchone()
        self.assertEqual(
            persisted[0],
            json.dumps(
                expected_scope,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        self.assertEqual(persisted[1], expected_sha256)

    def test_update_is_cas_versioned_and_pack_removal_clears_scope(self) -> None:
        created = self.create_stock_room()
        updated = self.store.update_room(created["id"], {
            "expected_settings_version": 1,
            "stock_room_scope": scope("US:NVDA", "us:aapl"),
        })
        self.assertEqual((updated or {})["settings_version"], 2)
        self.assertEqual(
            (updated or {})["stock_room_scope"],
            scope("US:AAPL", "US:NVDA"),
        )
        with self.assertRaises(ValueError):
            self.store.update_room(created["id"], {
                "expected_settings_version": 1,
                "stock_room_scope": scope("US:AMD"),
            })

        removed = self.store.update_room(created["id"], {
            "expected_settings_version": 2,
            "capability_pack_ids": [],
        })
        self.assertEqual((removed or {})["settings_version"], 3)
        self.assertEqual((removed or {})["stock_room_scope"], {})
        self.assertEqual((removed or {})["stock_room_scope_sha256"], "")
        self.assertTrue((removed or {})["stock_room_scope_integrity_ok"])

        version_two = self.store.get_room_version_record(created["id"], 2)
        version_three = self.store.get_room_version_record(created["id"], 3)
        self.assertEqual(
            (version_two or {})["room_version"]["stock_room_scope"],
            scope("US:AAPL", "US:NVDA"),
        )
        self.assertEqual(
            (version_three or {})["room_version"]["stock_room_scope"],
            {},
        )

        with self.assertRaises(StockResearchContractError):
            self.store.update_room(created["id"], {
                "expected_settings_version": 3,
                "capability_pack_ids": [STOCK_RESEARCH_CAPABILITY_PACK_ID],
            })
        with self.assertRaises(ValueError):
            self.store.update_room(created["id"], {
                "expected_settings_version": 3,
                "capability_pack_ids": [],
                "stock_room_scope": scope("US:AAPL"),
            })

        restored = self.store.update_room(created["id"], {
            "expected_settings_version": 3,
            "capability_pack_ids": [STOCK_RESEARCH_CAPABILITY_PACK_ID],
            "stock_room_scope": scope("US:AMD"),
        })
        self.assertEqual((restored or {})["settings_version"], 4)
        self.assertEqual((restored or {})["stock_room_scope"], scope("US:AMD"))

    def test_missing_illegal_and_corrupt_scopes_fail_closed(self) -> None:
        with self.assertRaises(StockResearchContractError):
            self.store.create_room(
                "Missing stock pool",
                "Must fail closed.",
                capability_pack_ids=[STOCK_RESEARCH_CAPABILITY_PACK_ID],
            )
        with self.assertRaises(StockResearchContractError):
            self.store.create_room(
                "Empty stock pool",
                "Must fail closed.",
                capability_pack_ids=[STOCK_RESEARCH_CAPABILITY_PACK_ID],
                stock_room_scope=scope(),
            )
        with self.assertRaises(StockResearchContractError):
            self.store.create_room(
                "Duplicate stock pool",
                "Must fail closed.",
                capability_pack_ids=[STOCK_RESEARCH_CAPABILITY_PACK_ID],
                stock_room_scope=scope("us:aapl", "US:AAPL"),
            )
        with self.assertRaises(ValueError):
            self.store.create_room(
                "Generic room with hidden scope",
                "Must not retain a stock scope.",
                capability_pack_ids=[],
                stock_room_scope=scope("US:AAPL"),
            )

        room = self.create_stock_room()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """UPDATE rooms
                      SET stock_room_scope_json='{}',stock_room_scope_sha256=''
                    WHERE id=?""",
                (room["id"],),
            )
            connection.commit()
        corrupted = self.store.room_snapshot(room["id"])["room"]
        self.assertFalse(corrupted["stock_room_scope_integrity_ok"])
        self.assertEqual(corrupted["stock_room_scope"], {})
        self.assertEqual(
            corrupted["stock_room_scope_integrity_issues"],
            ["ROOM_STOCK_SCOPE_INVALID"],
        )
        with self.assertRaises(StockResearchContractError):
            self.store.update_room(room["id"], {
                "expected_settings_version": 1,
                "title": "Cannot edit around missing stock scope",
            })

        repaired = self.store.update_room(room["id"], {
            "expected_settings_version": 1,
            "stock_room_scope": scope("US:AMD"),
        })
        self.assertTrue((repaired or {})["stock_room_scope_integrity_ok"])
        self.assertEqual((repaired or {})["stock_room_scope"], scope("US:AMD"))

    def test_generic_and_storage_rooms_keep_empty_valid_scope(self) -> None:
        generic = self.store.create_room(
            "Generic",
            "No stock semantics.",
            capability_pack_ids=[],
        )["room"]
        storage = self.store.create_room(
            "Storage",
            "Existing storage pack semantics remain unchanged.",
            capability_pack_ids=["storage_research_readonly"],
        )["room"]

        for room in (generic, storage):
            self.assertEqual(room["stock_room_scope"], {})
            self.assertEqual(room["stock_room_scope_sha256"], "")
            self.assertTrue(room["stock_room_scope_integrity_ok"])
            self.assertEqual(room["settings_version"], 1)


if __name__ == "__main__":
    unittest.main()
