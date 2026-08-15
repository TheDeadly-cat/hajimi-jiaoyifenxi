from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import patch


os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.plugin_registry import HOST_UI_VIEW_MODEL_SCHEMAS
from backend.stock_research import (
    STOCK_PREFLIGHT_SOURCE_TYPES,
    STOCK_ROOM_SCOPE_VERSION,
    canonical_sha256,
    validate_stock_research_contract,
)
from backend.stock_research_service import (
    STOCK_RESEARCH_ACTION_ID,
    STOCK_RESEARCH_ADAPTER_ID,
    STOCK_RESEARCH_CONTRIBUTION_ID,
    STOCK_RESEARCH_PORT_ID,
    STOCK_RESEARCH_VIEW_MODEL_VERSION,
    STOCK_SYMBOL_PREFLIGHT_VIEW_VERSION,
    StockResearchError,
    StockResearchService,
)
from backend.store import StudioStore


class StockResearchServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-stock-service-",
            ignore_cleanup_errors=True,
        )
        self.database_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.store = StudioStore(self.database_path)
        self.scope = {
            "version": STOCK_ROOM_SCOPE_VERSION,
            "symbols": ["US:AAPL", "US:MSFT"],
        }
        created = self.store.create_room(
            "Explicit stock pool",
            "Inspect only the room's exact read-only stock research seal.",
            capability_pack_ids=["stock_research_readonly"],
            stock_room_scope=self.scope,
        )
        self.room = created["room"]
        self.payload = self._payload(str(self.room["id"]), self.scope["symbols"])
        self.service = StockResearchService(self.store)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _source(
        self,
        room_id: str,
        *,
        identity: str,
        publisher: str,
    ) -> dict[str, Any]:
        content = f"Exact offline stock evidence for {identity}."
        content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        material = self.store.add_material(room_id, {
            "title": f"Stock evidence {identity}",
            "kind": "note",
            "content": content,
            "metadata": {
                "content_sha256": content_sha256,
                "extraction_method": "manual",
            },
        })
        self.assertIsNotNone(material)
        material_id = str(material["id"])
        version = int(material["version"])
        self.assertIs(type(version), int)
        return {
            "source_id": f"source-{identity}",
            "publisher": publisher,
            "source_uri": f"urn:ai-studio:material:{material_id}:v{version}",
            "source_sha256": content_sha256,
            "material_binding": {
                "material_id": material_id,
                "material_version": version,
                "content_sha256": content_sha256,
                "snapshot_sha256": str(material["source_snapshot_sha256"]),
            },
            "published_at_utc": "2026-08-12T08:00:00Z",
            "retrieved_at_utc": "2026-08-12T09:30:00Z",
        }

    def _payload(
        self,
        room_id: str,
        symbols: list[str],
    ) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        publisher_by_type = {
            "futu": "Futu snapshot material",
            "sec": "SEC filing material",
            "investor_relations": "Issuer investor relations material",
            "price_adjustment": "Price adjustment material",
            "corporate_actions": "Corporate action material",
        }
        for symbol in sorted(symbols):
            slug = symbol.lower().replace(":", "-")
            preflight: dict[str, Any] = {}
            for source_type in STOCK_PREFLIGHT_SOURCE_TYPES:
                preflight[source_type] = {
                    "version": "stock_source_preflight_v1",
                    "source_type": source_type,
                    "status": "ready",
                    "as_of_utc": "2026-08-12T09:00:00Z",
                    "reason": "",
                    "source": self._source(
                        room_id,
                        identity=f"{slug}-{source_type.replace('_', '-')}",
                        publisher=publisher_by_type[source_type],
                    ),
                }
            rows.append({
                "symbol": symbol,
                "issuer_name": (
                    "Apple Inc." if symbol == "US:AAPL" else "Microsoft Corp."
                ),
                "exchange": "US",
                "currency": "USD",
                "preflight": preflight,
                "evidence": [{
                    "claim_id": f"claim-{slug}-official",
                    "symbol": symbol,
                    "claim": "This fixture is an exact offline official fact.",
                    "evidence_class": "official_fact",
                    "as_of_utc": "2026-08-12T09:00:00Z",
                    "source": self._source(
                        room_id,
                        identity=f"{slug}-official-claim",
                        publisher="Official issuer fixture",
                    ),
                    "inference": None,
                }],
            })
        return {
            "stock_room_scope": {
                "version": STOCK_ROOM_SCOPE_VERSION,
                "symbols": sorted(symbols),
            },
            "data_cutoff_utc": "2026-08-12T10:00:00Z",
            "symbols": rows,
            "research_ready": True,
        }

    @staticmethod
    def _first_source(payload: dict[str, Any]) -> dict[str, Any]:
        return payload["symbols"][0]["preflight"]["futu"]["source"]

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
            name: int(connection.execute(
                f'SELECT COUNT(*) FROM "{name.replace(chr(34), chr(34) * 2)}"'
            ).fetchone()[0])
            for name in names
        }

    def _business_table_counts(self) -> dict[str, int]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            return self._table_counts(connection)

    def test_inspect_returns_closed_room_scoped_readonly_view(self) -> None:
        before = self._business_table_counts()
        network_calls: list[tuple[Any, ...]] = []

        def forbidden_network(*args: Any, **_kwargs: Any) -> None:
            network_calls.append(args)
            raise AssertionError("stock research service forbids network access")

        with patch("socket.create_connection", new=forbidden_network):
            result = self.service.inspect(str(self.room["id"]), self.payload)
        after = self._business_table_counts()

        expected_schema = HOST_UI_VIEW_MODEL_SCHEMAS[
            STOCK_RESEARCH_VIEW_MODEL_VERSION
        ]
        self.assertEqual(result["version"], STOCK_RESEARCH_VIEW_MODEL_VERSION)
        self.assertEqual(set(result), set(expected_schema["required"]))
        self.assertEqual(set(result), set(expected_schema["fields"]))
        self.assertIs(expected_schema["additional_properties"], False)
        self.assertEqual(
            validate_stock_research_contract(result["contract"]),
            result["contract"],
        )
        self.assertEqual(result["stock_room_scope"], self.scope)
        self.assertEqual(result["contract_sha256"], result["contract"]["contract_sha256"])
        self.assertEqual(result["data_cutoff_utc"], "2026-08-12T10:00:00Z")
        self.assertIs(result["integrity_ok"], True)
        self.assertIs(result["metrics_visible"], True)
        self.assertIs(result["research_ready"], True)
        self.assertEqual(
            [row["symbol"] for row in result["symbol_preflights"]],
            self.scope["symbols"],
        )
        for row in result["symbol_preflights"]:
            self.assertEqual(row["version"], STOCK_SYMBOL_PREFLIGHT_VIEW_VERSION)
            self.assertIs(row["research_ready"], True)
            self.assertEqual(
                set(row),
                {
                    "version",
                    "symbol",
                    "research_ready",
                    *STOCK_PREFLIGHT_SOURCE_TYPES,
                },
            )
            for source_type in STOCK_PREFLIGHT_SOURCE_TYPES:
                self.assertEqual(row[source_type], {
                    "status": "ready",
                    "as_of_utc": "2026-08-12T09:00:00Z",
                    "reason": "",
                })
        self.assertEqual(result["provider_calls_performed"], 0)
        self.assertEqual(result["market_reads_performed"], 0)
        self.assertEqual(result["business_writes_performed"], 0)
        self.assertEqual(result["execution_capability"], "none")
        for field in (
            "live_trading_allowed",
            "order_placement_allowed",
            "wallet_connection_allowed",
            "automatic_trading_allowed",
            "can_autonomously_decide",
            "can_replace_user_decision",
        ):
            self.assertIs(result[field], False)
        self.assertIs(result["user_final_decision_required"], True)
        self.assertEqual(before, after)
        self.assertEqual(network_calls, [])

        sources = list(self.service._iter_sources(result["contract"]))
        self.assertEqual(len(sources), len(self.scope["symbols"]) * 6)
        self.assertTrue(all(
            type(source["material_binding"]["material_version"]) is int
            for source in sources
        ))
        repeated = self.service.inspect(
            str(self.room["id"]), result["contract"]
        )
        self.assertEqual(repeated, result)

    def test_owned_and_caller_transactions_are_readonly_and_write_free(self) -> None:
        with closing(self.service._readonly_connection()) as readonly:
            self.assertTrue(readonly.in_transaction)
            self.assertEqual(int(readonly.execute("PRAGMA query_only").fetchone()[0]), 1)

        with closing(self.store._connect()) as connection:
            with self.assertRaises(StockResearchError) as missing_transaction:
                self.service.inspect_from_connection(
                    connection,
                    str(self.room["id"]),
                    self.payload,
                )
            self.assertEqual(
                missing_transaction.exception.code,
                "STOCK_RESEARCH_SNAPSHOT_REQUIRED",
            )

            connection.execute("BEGIN")
            before = self._table_counts(connection)
            changes_before = int(connection.total_changes)
            result = self.service.inspect_from_connection(
                connection,
                str(self.room["id"]),
                self.payload,
            )
            self.assertTrue(connection.in_transaction)
            self.assertEqual(connection.total_changes, changes_before)
            self.assertEqual(self._table_counts(connection), before)
            self.assertEqual(result["provider_calls_performed"], 0)
            self.assertEqual(result["market_reads_performed"], 0)
            self.assertEqual(result["business_writes_performed"], 0)

    def test_room_scope_contract_sha_and_action_fail_closed(self) -> None:
        narrower = copy.deepcopy(self.payload)
        narrower["stock_room_scope"]["symbols"] = ["US:AAPL"]
        narrower["symbols"] = [narrower["symbols"][0]]
        with self.assertRaises(StockResearchError) as scope_mismatch:
            self.service.inspect(str(self.room["id"]), narrower)
        self.assertEqual(
            scope_mismatch.exception.code, "STOCK_RESEARCH_SCOPE_MISMATCH"
        )

        sealed = self.service.inspect(str(self.room["id"]), self.payload)["contract"]
        tampered = copy.deepcopy(sealed)
        tampered["contract_sha256"] = "f" * 64
        with self.assertRaises(StockResearchError) as bad_hash:
            self.service.inspect(str(self.room["id"]), tampered)
        self.assertEqual(bad_hash.exception.code, "STOCK_RESEARCH_CONTRACT_INVALID")

        plain_room = self.store.create_room(
            "No stock pack",
            "This room cannot inspect stock research.",
            capability_pack_ids=[],
        )["room"]
        with self.assertRaises(StockResearchError) as unavailable:
            self.service.inspect(str(plain_room["id"]), self.payload)
        self.assertEqual(
            unavailable.exception.code, "STOCK_RESEARCH_ACTION_UNAVAILABLE"
        )

    def test_material_int_version_content_snapshot_and_identity_fail_closed(self) -> None:
        wrong_version = copy.deepcopy(self.payload)
        source = self._first_source(wrong_version)
        source["material_binding"]["material_version"] = 2
        source["source_uri"] = (
            f"urn:ai-studio:material:"
            f"{source['material_binding']['material_id']}:v2"
        )
        with self.assertRaises(StockResearchError) as version_missing:
            self.service.inspect(str(self.room["id"]), wrong_version)
        self.assertEqual(
            version_missing.exception.code,
            "STOCK_RESEARCH_MATERIAL_VERSION_NOT_FOUND",
        )

        text_version = copy.deepcopy(self.payload)
        self._first_source(text_version)["material_binding"]["material_version"] = "1"
        with self.assertRaises(StockResearchError) as invalid_contract:
            self.service.inspect(str(self.room["id"]), text_version)
        self.assertEqual(
            invalid_contract.exception.code, "STOCK_RESEARCH_CONTRACT_INVALID"
        )

        wrong_content = copy.deepcopy(self.payload)
        source = self._first_source(wrong_content)
        source["material_binding"]["content_sha256"] = "e" * 64
        source["source_sha256"] = "e" * 64
        with self.assertRaises(StockResearchError) as content_drift:
            self.service.inspect(str(self.room["id"]), wrong_content)
        self.assertEqual(
            content_drift.exception.code,
            "STOCK_RESEARCH_MATERIAL_CONTENT_DRIFT",
        )

        wrong_snapshot = copy.deepcopy(self.payload)
        self._first_source(wrong_snapshot)["material_binding"][
            "snapshot_sha256"
        ] = "d" * 64
        with self.assertRaises(StockResearchError) as snapshot_drift:
            self.service.inspect(str(self.room["id"]), wrong_snapshot)
        self.assertEqual(
            snapshot_drift.exception.code,
            "STOCK_RESEARCH_MATERIAL_SNAPSHOT_DRIFT",
        )

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
                    "material_version_duplicate_for_stock_service_test",
                    row["material_id"],
                    row["room_id"],
                    row["version"],
                    row["snapshot_json"],
                    row["changed_at"],
                ),
            )
        with self.assertRaises(StockResearchError) as ambiguous:
            self.service.inspect(str(self.room["id"]), self.payload)
        self.assertEqual(
            ambiguous.exception.code,
            "STOCK_RESEARCH_MATERIAL_VERSION_AMBIGUOUS",
        )

    def test_registry_binding_is_exact_for_pack_action_ui_adapter_and_port(self) -> None:
        current = self.store.room_snapshot(str(self.room["id"]))["room"]
        port = self.service._single_registry_binding(current)
        self.assertEqual(port["adapter_id"], STOCK_RESEARCH_ADAPTER_ID)
        self.assertEqual(port["port_id"], STOCK_RESEARCH_PORT_ID)
        self.assertEqual(port["handler_method"], "project_market_readonly_context")
        self.assertEqual(port["provider_call_budget"], 0)
        self.assertEqual(port["market_read_budget"], 0)
        self.assertEqual(port["business_write_budget"], 0)
        contribution = next(
            row
            for row in current["plugin_registry_snapshot"]["ui_contributions"]
            if row["contribution_id"] == STOCK_RESEARCH_CONTRIBUTION_ID
        )
        self.assertEqual(contribution["component_key"], "stock_research_inspector")
        self.assertIn(
            STOCK_RESEARCH_ACTION_ID,
            current["plugin_lifecycle_current"]["available_action_ids"],
        )

        corrupted = copy.deepcopy(current)
        next(
            row
            for row in corrupted["plugin_registry_snapshot"]["ui_contributions"]
            if row["contribution_id"] == STOCK_RESEARCH_CONTRIBUTION_ID
        )["component_key"] = "wrong_component"
        with self.assertRaises(StockResearchError) as invalid:
            self.service._single_registry_binding(corrupted)
        self.assertEqual(invalid.exception.code, "STOCK_RESEARCH_BINDING_INVALID")

        no_action = copy.deepcopy(current)
        no_action["plugin_lifecycle_current"]["available_action_ids"].remove(
            STOCK_RESEARCH_ACTION_ID
        )
        with self.assertRaises(StockResearchError) as action:
            self.service._single_registry_binding(no_action)
        self.assertEqual(action.exception.code, "STOCK_RESEARCH_ACTION_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
