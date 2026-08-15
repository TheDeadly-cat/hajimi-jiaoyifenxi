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

from backend.football_research import validate_football_research_contract
from backend.football_research_service import (
    FOOTBALL_RESEARCH_VIEW_MODEL_VERSION,
    FootballResearchError,
    FootballResearchService,
)
from backend.plugin_registry import HOST_UI_VIEW_MODEL_SCHEMAS
from backend.store import StudioStore
from tests.test_football_research import payload as football_payload


def iter_sources(value):
    if isinstance(value, dict):
        source = value.get("source")
        if isinstance(source, dict) and isinstance(
            source.get("material_binding"), dict
        ):
            yield source
        for child in value.values():
            yield from iter_sources(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_sources(child)


class FootballResearchServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_skip = os.environ.get("AI_STUDIO_SKIP_LOCAL_ENV")
        os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-football-service-"
        )
        self.database_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.store = StudioStore(self.database_path)
        self.room = self.store.create_room(
            "Football material seal",
            "Inspect one exact pre-kickoff material snapshot.",
            capability_pack_ids=["football_research_readonly"],
        )["room"]
        self.payload = self._material_bound_payload(str(self.room["id"]))
        self.service = FootballResearchService(self.store)

    def tearDown(self) -> None:
        if self.previous_skip is None:
            os.environ.pop("AI_STUDIO_SKIP_LOCAL_ENV", None)
        else:
            os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = self.previous_skip
        self.temp_dir.cleanup()

    def _material_bound_payload(self, room_id: str) -> dict:
        value = football_payload()
        for index, source in enumerate(iter_sources(value), start=1):
            content = f"Exact football evidence material {index}."
            content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
            material = self.store.add_material(
                room_id,
                {
                    "title": f"Football evidence {index}",
                    "kind": "note",
                    "content": content,
                    "metadata": {
                        "content_sha256": content_sha256,
                        "extraction_method": "manual",
                    },
                },
            )
            self.assertIsNotNone(material)
            source["source_uri"] = (
                f"urn:ai-studio:material:{material['id']}:v{material['version']}"
            )
            source["source_sha256"] = content_sha256
            source["material_binding"] = {
                "material_id": str(material["id"]),
                "material_version": int(material["version"]),
                "content_sha256": content_sha256,
                "snapshot_sha256": str(material["source_snapshot_sha256"]),
            }
        return value

    @staticmethod
    def _first_source(value: dict) -> dict:
        return next(iter(iter_sources(value)))

    def _business_table_counts(self) -> dict[str, int]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            return self._table_counts(connection)

    @staticmethod
    def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
        names = [
            str(row[0])
            for row in connection.execute(
                """SELECT name FROM sqlite_schema
                    WHERE type='table' AND name NOT LIKE 'sqlite_%'
                    ORDER BY name"""
            ).fetchall()
        ]
        return {
            name: int(
                connection.execute(
                    f'SELECT COUNT(*) FROM "{name.replace(chr(34), chr(34) * 2)}"'
                ).fetchone()[0]
            )
            for name in names
        }

    def test_inspect_returns_closed_material_bound_readonly_view(self) -> None:
        before = self._business_table_counts()
        with patch(
            "socket.create_connection",
            side_effect=AssertionError("network access is forbidden"),
        ):
            result = self.service.inspect(str(self.room["id"]), self.payload)
        after = self._business_table_counts()

        expected_schema = HOST_UI_VIEW_MODEL_SCHEMAS[
            FOOTBALL_RESEARCH_VIEW_MODEL_VERSION
        ]
        self.assertEqual(result["version"], FOOTBALL_RESEARCH_VIEW_MODEL_VERSION)
        self.assertEqual(set(result), set(expected_schema["required"]))
        self.assertEqual(set(result), set(expected_schema["fields"]))
        self.assertIs(expected_schema["additional_properties"], False)
        self.assertEqual(validate_football_research_contract(result["contract"]), result["contract"])
        self.assertEqual(result["contract_sha256"], result["contract"]["contract_sha256"])
        self.assertEqual(result["data_cutoff_utc"], result["contract"]["data_cutoff_utc"])
        self.assertIs(result["integrity_ok"], True)
        self.assertIs(result["metrics_visible"], False)
        self.assertIs(result["future_probability_available"], False)
        self.assertIs(result["probability_metrics_visible"], False)
        self.assertIs(result["odds_are_proxy_only"], True)
        self.assertEqual(result["provider_calls_performed"], 0)
        self.assertEqual(result["market_reads_performed"], 0)
        self.assertEqual(result["business_writes_performed"], 0)
        self.assertEqual(result["execution_capability"], "none")
        self.assertIs(result["live_trading_allowed"], False)
        self.assertIs(result["betting_allowed"], False)
        self.assertIs(result["automatic_betting_allowed"], False)
        self.assertIs(result["wallet_connection_allowed"], False)
        self.assertIs(result["order_placement_allowed"], False)
        self.assertIs(result["can_autonomously_decide"], False)
        self.assertIs(result["can_replace_user_decision"], False)
        self.assertIs(result["user_final_decision_required"], True)
        self.assertEqual(before, after)

        # A caller may submit the already sealed contract for a second exact
        # inspection; it is verified rather than re-hashed as an unsealed draft.
        repeated = self.service.inspect(str(self.room["id"]), result["contract"])
        self.assertEqual(repeated, result)

    def test_inspect_from_connection_reuses_caller_snapshot_without_writes(self) -> None:
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN")
            before_counts = self._table_counts(connection)
            before_changes = int(connection.total_changes)

            result = self.service.inspect_from_connection(
                connection,
                str(self.room["id"]),
                self.payload,
            )

            self.assertIs(connection.in_transaction, True)
            self.assertEqual(connection.total_changes, before_changes)
            self.assertEqual(self._table_counts(connection), before_counts)
            self.assertEqual(result["business_writes_performed"], 0)
            self.assertEqual(result["provider_calls_performed"], 0)
            self.assertEqual(result["market_reads_performed"], 0)

    def test_caller_snapshot_does_not_silently_advance_to_new_material_version(self) -> None:
        updated_payload = copy.deepcopy(self.payload)
        source = self._first_source(updated_payload)
        binding = source["material_binding"]
        new_content = "A newer football source version outside the frozen transaction."
        new_content_sha256 = hashlib.sha256(new_content.encode("utf-8")).hexdigest()

        with closing(self.store._connect()) as frozen_connection:
            frozen_connection.execute("BEGIN")
            # Establish the read snapshot before the separate writer publishes v2.
            frozen_connection.execute(
                "SELECT id FROM rooms WHERE id=?",
                (self.room["id"],),
            ).fetchone()
            changes_before = int(frozen_connection.total_changes)
            updated = self.store.update_material(
                str(self.room["id"]),
                str(binding["material_id"]),
                {
                    "expected_version": int(binding["material_version"]),
                    "content": new_content,
                    "metadata": {
                        "content_sha256": new_content_sha256,
                        "extraction_method": "manual",
                    },
                },
            )
            self.assertIsNotNone(updated)
            source["source_uri"] = (
                f"urn:ai-studio:material:{updated['id']}:v{updated['version']}"
            )
            source["source_sha256"] = new_content_sha256
            source["material_binding"] = {
                "material_id": str(updated["id"]),
                "material_version": int(updated["version"]),
                "content_sha256": new_content_sha256,
                "snapshot_sha256": str(updated["source_snapshot_sha256"]),
            }

            with self.assertRaises(FootballResearchError) as raised:
                self.service.inspect_from_connection(
                    frozen_connection,
                    str(self.room["id"]),
                    updated_payload,
                )
            self.assertEqual(
                raised.exception.code,
                "FOOTBALL_RESEARCH_MATERIAL_VERSION_NOT_FOUND",
            )
            self.assertIs(frozen_connection.in_transaction, True)
            self.assertEqual(frozen_connection.total_changes, changes_before)

        # A fresh read-only snapshot can see and verify the newly published v2.
        fresh = self.service.inspect(str(self.room["id"]), updated_payload)
        self.assertEqual(fresh["contract"]["contract_sha256"], fresh["contract_sha256"])

    def test_material_version_content_and_snapshot_drift_fail_closed(self) -> None:
        wrong_version = copy.deepcopy(self.payload)
        source = self._first_source(wrong_version)
        source["material_binding"]["material_version"] = 2
        source["source_uri"] = (
            f"urn:ai-studio:material:{source['material_binding']['material_id']}:v2"
        )
        with self.assertRaises(FootballResearchError) as raised:
            self.service.inspect(str(self.room["id"]), wrong_version)
        self.assertEqual(
            raised.exception.code,
            "FOOTBALL_RESEARCH_MATERIAL_VERSION_NOT_FOUND",
        )

        wrong_content = copy.deepcopy(self.payload)
        source = self._first_source(wrong_content)
        source["material_binding"]["content_sha256"] = "f" * 64
        source["source_sha256"] = "f" * 64
        with self.assertRaises(FootballResearchError) as raised:
            self.service.inspect(str(self.room["id"]), wrong_content)
        self.assertEqual(
            raised.exception.code,
            "FOOTBALL_RESEARCH_MATERIAL_CONTENT_DRIFT",
        )

        wrong_snapshot = copy.deepcopy(self.payload)
        source = self._first_source(wrong_snapshot)
        source["material_binding"]["snapshot_sha256"] = "e" * 64
        with self.assertRaises(FootballResearchError) as raised:
            self.service.inspect(str(self.room["id"]), wrong_snapshot)
        self.assertEqual(
            raised.exception.code,
            "FOOTBALL_RESEARCH_MATERIAL_SNAPSHOT_DRIFT",
        )

    def test_duplicate_material_version_identity_is_rejected(self) -> None:
        binding = self._first_source(self.payload)["material_binding"]
        with closing(self.store._connect()) as connection, connection:
            connection.execute("DROP INDEX uq_material_versions_identity")
            connection.execute("DROP TRIGGER trg_material_versions_identity_insert")
            connection.execute("DROP TRIGGER trg_material_versions_identity_update")
            row = connection.execute(
                """SELECT * FROM material_versions
                    WHERE room_id=? AND material_id=? AND version=?""",
                (
                    self.room["id"],
                    binding["material_id"],
                    binding["material_version"],
                ),
            ).fetchone()
            connection.execute(
                """INSERT INTO material_versions(
                       id,material_id,room_id,version,snapshot_json,changed_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    "material_version_duplicate_for_football_test",
                    row["material_id"],
                    row["room_id"],
                    row["version"],
                    row["snapshot_json"],
                    row["changed_at"],
                ),
            )

        with self.assertRaises(FootballResearchError) as raised:
            self.service.inspect(str(self.room["id"]), self.payload)
        self.assertEqual(
            raised.exception.code,
            "FOOTBALL_RESEARCH_MATERIAL_VERSION_AMBIGUOUS",
        )

    def test_room_without_football_pack_cannot_inspect(self) -> None:
        plain_room = self.store.create_room(
            "Generic room",
            "No football capability pack.",
            capability_pack_ids=[],
        )["room"]
        with self.assertRaises(FootballResearchError) as raised:
            self.service.inspect(str(plain_room["id"]), self.payload)
        self.assertEqual(
            raised.exception.code,
            "FOOTBALL_RESEARCH_ACTION_UNAVAILABLE",
        )


if __name__ == "__main__":
    unittest.main()
