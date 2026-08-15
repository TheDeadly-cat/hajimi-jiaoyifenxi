from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.football_research_service import FootballResearchService
from backend.project_round_focus import ProjectRoundFocusService
from backend.round_contexts import (
    FOOTBALL_ROUND_CONTEXT_AUTHORIZATION_VERSION,
    FOOTBALL_ROUND_CONTEXT_REQUEST_VERSION,
    DEFAULT_ROUND_CONTEXT_PROVIDERS,
    ROUND_CONTEXT_AUTHORIZATION_SET_VERSION,
    ROUND_CONTEXT_PROVIDER_REGISTRY_VERSION,
    STOCK_ROUND_CONTEXT_AUTHORIZATION_VERSION,
    STOCK_ROUND_CONTEXT_REQUEST_VERSION,
    RoundContextError,
    build_round_context_authorization_set,
    build_round_context_prepared,
    coerce_round_context_authorization_set,
    prepare_authorized_set,
    prepare_football_round_context,
    prepare_stock_round_context,
    prompt_sections,
    round_context_authorization_entry,
    round_context_prepared_entry,
)
from backend.stock_research import STOCK_ROOM_SCOPE_VERSION, canonical_sha256
from backend.stock_research_service import StockResearchService
from backend.store import StudioStore
from tests.test_football_research import payload as football_payload
from tests.test_stock_research import payload as stock_payload


def _iter_sources(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        source = value.get("source")
        if isinstance(source, dict) and isinstance(
            source.get("material_binding"), dict
        ):
            yield source
        for child in value.values():
            yield from _iter_sources(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_sources(child)


class RoundContextFrameworkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-round-contexts-",
            ignore_cleanup_errors=True,
        )
        self.database_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.store = StudioStore(self.database_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _room(
        self,
        packs: list[str],
        *,
        stock_room_scope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.store.create_room(
            "Round contexts",
            "Freeze every selected read-only context in one round.",
            capability_pack_ids=packs,
            stock_room_scope=stock_room_scope,
        )["room"]

    def _project_prepared(self, room_id: str) -> dict[str, Any]:
        service = ProjectRoundFocusService(self.store)
        preview = service.preview(room_id)
        artifact = preview["artifact_binding"]
        authorization = {
            "version": "project_round_focus_authorization_v1",
            "artifact_binding": (
                {"status": "none"}
                if artifact["status"] == "none"
                else {
                    "status": "exact",
                    "artifact_id": artifact["artifact_id"],
                    "artifact_version": artifact["artifact_version"],
                }
            ),
            "preview_sha256": preview["preview_sha256"],
            "user_confirmed": True,
        }
        return service.prepare_authorized(room_id, authorization)

    def _football_payload(self, room_id: str) -> dict[str, Any]:
        value = football_payload()
        for index, source in enumerate(_iter_sources(value), start=1):
            content = f"Exact frozen football material {index}."
            content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
            material = self.store.add_material(room_id, {
                "title": f"Football evidence {index}",
                "kind": "note",
                "content": content,
                "metadata": {
                    "content_sha256": content_sha256,
                    "extraction_method": "manual",
                },
            })
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
    def _football_authorization(preview: dict[str, Any]) -> dict[str, Any]:
        return {
            "version": FOOTBALL_ROUND_CONTEXT_AUTHORIZATION_VERSION,
            "owner_pack_id": "football_research_readonly",
            "port_id": "core.football.match_context/v1",
            "contract_sha256": preview["contract_sha256"],
            "data_cutoff_utc": preview["data_cutoff_utc"],
            "match_id": preview["contract"]["match_identity"]["match_id"]["value"],
            "user_confirmed": True,
        }

    def _football_entry(
        self,
        room_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        preview = FootballResearchService(self.store).inspect(room_id, payload)
        return prepare_football_round_context(
            self.store,
            room_id,
            payload,
            self._football_authorization(preview),
        )

    def _stock_payload(self, room_id: str) -> dict[str, Any]:
        value = stock_payload()
        for index, source in enumerate(_iter_sources(value), start=1):
            content = f"Exact frozen stock material {index}."
            content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
            material = self.store.add_material(room_id, {
                "title": f"Stock evidence {index}",
                "kind": "note",
                "content": content,
                "metadata": {
                    "content_sha256": content_sha256,
                    "extraction_method": "manual",
                },
            })
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
    def _stock_authorization(preview: dict[str, Any]) -> dict[str, Any]:
        stock_scope = preview["stock_room_scope"]
        return {
            "version": STOCK_ROUND_CONTEXT_AUTHORIZATION_VERSION,
            "owner_pack_id": "stock_research_readonly",
            "port_id": "core.market.readonly_context/v1",
            "contract_sha256": preview["contract_sha256"],
            "stock_room_scope_sha256": canonical_sha256(stock_scope),
            "data_cutoff_utc": preview["data_cutoff_utc"],
            "user_confirmed": True,
        }

    def _stock_entry(
        self,
        room_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        preview = StockResearchService(self.store).inspect(room_id, payload)
        return prepare_stock_round_context(
            self.store,
            room_id,
            payload,
            self._stock_authorization(preview),
        )

    def test_zero_one_and_multiple_context_anchors(self) -> None:
        self.assertEqual(
            DEFAULT_ROUND_CONTEXT_PROVIDERS.version,
            ROUND_CONTEXT_PROVIDER_REGISTRY_VERSION,
        )
        empty_room = self._room([])
        empty_round = self.store.create_formal_round(
            empty_room["id"],
            "No registered round context is selected.",
        )
        self.assertEqual(empty_round["round_domain_context_count"], 0)
        self.assertEqual(empty_round["round_domain_contexts_sha256"], "")
        empty_record = self.store.get_round_contexts(
            empty_room["id"], empty_round["id"]
        )
        self.assertTrue(empty_record["integrity_ok"])
        self.assertEqual(empty_record["contexts"], [])

        focus_room = self._room(["project_round_focus"])
        focus_round = self.store.create_formal_round(
            focus_room["id"],
            "Freeze one project focus.",
            project_round_focus_prepared=self._project_prepared(focus_room["id"]),
        )
        self.assertEqual(focus_round["round_domain_context_count"], 1)
        self.assertRegex(focus_round["round_domain_contexts_sha256"], r"^[0-9a-f]{64}$")

        multi_room = self._room([
            "project_round_focus",
            "football_research_readonly",
        ])
        football_payload_value = self._football_payload(multi_room["id"])
        prepared_set = build_round_context_prepared([
            round_context_prepared_entry(
                "project_round_focus",
                "core.round.context/v1",
                self._project_prepared(multi_room["id"]),
            ),
            self._football_entry(multi_room["id"], football_payload_value),
        ])
        multi_round = self.store.create_formal_round(
            multi_room["id"],
            "Freeze project and football contexts together.",
            round_context_prepared=prepared_set,
        )
        self.assertEqual(multi_round["round_domain_context_count"], 2)
        self.assertRegex(multi_round["round_domain_contexts_sha256"], r"^[0-9a-f]{64}$")
        record = self.store.get_round_contexts(multi_room["id"], multi_round["id"])
        self.assertTrue(record["integrity_ok"])
        self.assertEqual(
            [(row["owner_pack_id"], row["port_id"]) for row in record["contexts"]],
            [
                ("football_research_readonly", "core.football.match_context/v1"),
                ("project_round_focus", "core.round.context/v1"),
            ],
        )
        self.assertEqual(len(prompt_sections(record)), 2)

    def test_authorization_set_is_closed_exact_and_prepares_canonically(self) -> None:
        room = self._room(["football_research_readonly"])
        payload = self._football_payload(room["id"])
        preview = FootballResearchService(self.store).inspect(room["id"], payload)
        request = {
            "version": FOOTBALL_ROUND_CONTEXT_REQUEST_VERSION,
            "payload": payload,
            "authorization": self._football_authorization(preview),
        }
        authorization_set = build_round_context_authorization_set([
            round_context_authorization_entry(
                "football_research_readonly",
                "core.football.match_context/v1",
                request,
            )
        ])
        self.assertEqual(
            authorization_set["version"], ROUND_CONTEXT_AUTHORIZATION_SET_VERSION
        )
        prepared = prepare_authorized_set(
            self.store,
            room["id"],
            authorization_set,
        )
        self.assertEqual(len(prepared["contexts"]), 1)
        self.assertEqual(len(prompt_sections(prepared)), 1)

        empty = build_round_context_authorization_set([])
        with self.assertRaises(RoundContextError) as missing:
            prepare_authorized_set(self.store, room["id"], empty)
        self.assertEqual(missing.exception.code, "ROUND_CONTEXT_AUTHORIZATION_REQUIRED")

        extra = build_round_context_authorization_set([
            authorization_set["contexts"][0],
            round_context_authorization_entry(
                "project_round_focus",
                "core.round.context/v1",
                {},
            ),
        ])
        with self.assertRaises(RoundContextError) as extra_error:
            prepare_authorized_set(self.store, room["id"], extra)
        self.assertEqual(
            extra_error.exception.code,
            "ROUND_CONTEXT_AUTHORIZATION_NOT_APPLICABLE",
        )

        malformed = dict(authorization_set)
        malformed["unexpected"] = True
        with self.assertRaises(RoundContextError):
            prepare_authorized_set(self.store, room["id"], malformed)

        broken_request = json.loads(json.dumps(authorization_set))
        broken_request["contexts"][0]["request"]["payload"] = {}
        with self.assertRaises(RoundContextError) as domain_error:
            prepare_authorized_set(self.store, room["id"], broken_request)
        self.assertEqual(
            domain_error.exception.code,
            "FOOTBALL_RESEARCH_CONTRACT_INVALID",
        )

        legacy = {"version": "project_round_focus_authorization_v1"}
        mapped = coerce_round_context_authorization_set(
            None,
            legacy_project_round_focus_authorization=legacy,
        )
        self.assertEqual(mapped["contexts"][0]["request"], legacy)
        self.assertEqual(
            coerce_round_context_authorization_set(None)["contexts"], []
        )
        with self.assertRaises(RoundContextError) as ambiguous:
            coerce_round_context_authorization_set(
                authorization_set,
                legacy_project_round_focus_authorization=legacy,
            )
        self.assertEqual(
            ambiguous.exception.code,
            "ROUND_CONTEXT_AUTHORIZATION_AMBIGUOUS",
        )

    def test_stock_context_revalidates_materials_in_round_insert_transaction(self) -> None:
        stock_scope = {
            "version": STOCK_ROOM_SCOPE_VERSION,
            "symbols": ["US:AAPL", "US:MSFT"],
        }
        room = self._room(
            ["stock_research_readonly"],
            stock_room_scope=stock_scope,
        )
        payload = self._stock_payload(room["id"])
        preview = StockResearchService(self.store).inspect(room["id"], payload)
        request = {
            "version": STOCK_ROUND_CONTEXT_REQUEST_VERSION,
            "payload": payload,
            "authorization": self._stock_authorization(preview),
        }
        authorization_set = build_round_context_authorization_set([
            round_context_authorization_entry(
                "stock_research_readonly",
                "core.market.readonly_context/v1",
                request,
            ),
        ])
        prepared = prepare_authorized_set(
            self.store,
            room["id"],
            authorization_set,
        )
        trace: list[str] = []
        connection_ids: list[int] = []
        original = StockResearchService.inspect_from_connection

        def traced_inspect(
            service: StockResearchService,
            connection: sqlite3.Connection,
            room_id: str,
            value: Any,
        ) -> dict[str, Any]:
            self.assertTrue(connection.in_transaction)
            connection_ids.append(id(connection))
            connection.set_trace_callback(trace.append)
            return original(service, connection, room_id, value)

        with patch.object(
            StockResearchService,
            "inspect_from_connection",
            new=traced_inspect,
        ):
            round_row = self.store.create_formal_round(
                room["id"],
                "Freeze the exact stock context.",
                round_context_prepared=prepared,
            )

        normalized_trace = [statement.upper() for statement in trace]
        material_read = next(
            index
            for index, statement in enumerate(normalized_trace)
            if "FROM MATERIAL_VERSIONS" in statement
        )
        round_insert = next(
            index
            for index, statement in enumerate(normalized_trace)
            if "INSERT INTO ROUNDS" in statement
        )
        context_insert = next(
            index
            for index, statement in enumerate(normalized_trace)
            if "INSERT INTO ROUND_DOMAIN_CONTEXTS" in statement
        )
        self.assertEqual(len(connection_ids), 1)
        self.assertLess(material_read, round_insert)
        self.assertLess(material_read, context_insert)
        self.assertEqual(round_row["round_domain_context_count"], 1)
        frozen = self.store.get_round_contexts(room["id"], round_row["id"])
        self.assertTrue((frozen or {})["integrity_ok"])
        self.assertEqual(
            (frozen or {})["contexts"][0]["owner_pack_id"],
            "stock_research_readonly",
        )
        section = prompt_sections(frozen)[0]
        self.assertEqual(section["owner_pack_id"], "stock_research_readonly")
        self.assertEqual(
            section["payload"]["view_model"]["stock_room_scope"],
            stock_scope,
        )

    def test_stock_scope_drift_and_exact_authorization_set_fail_closed(self) -> None:
        stock_scope = {
            "version": STOCK_ROOM_SCOPE_VERSION,
            "symbols": ["US:AAPL", "US:MSFT"],
        }
        room = self._room(
            ["stock_research_readonly"],
            stock_room_scope=stock_scope,
        )
        payload = self._stock_payload(room["id"])
        preview = StockResearchService(self.store).inspect(room["id"], payload)
        request = {
            "version": STOCK_ROUND_CONTEXT_REQUEST_VERSION,
            "payload": payload,
            "authorization": self._stock_authorization(preview),
        }
        stock_entry = round_context_authorization_entry(
            "stock_research_readonly",
            "core.market.readonly_context/v1",
            request,
        )
        with self.assertRaises(RoundContextError) as missing:
            prepare_authorized_set(
                self.store,
                room["id"],
                build_round_context_authorization_set([]),
            )
        self.assertEqual(
            missing.exception.code,
            "ROUND_CONTEXT_AUTHORIZATION_REQUIRED",
        )
        with self.assertRaises(RoundContextError) as extra:
            prepare_authorized_set(
                self.store,
                room["id"],
                build_round_context_authorization_set([
                    stock_entry,
                    round_context_authorization_entry(
                        "project_round_focus",
                        "core.round.context/v1",
                        {},
                    ),
                ]),
            )
        self.assertEqual(
            extra.exception.code,
            "ROUND_CONTEXT_AUTHORIZATION_NOT_APPLICABLE",
        )

        prepared = prepare_authorized_set(
            self.store,
            room["id"],
            build_round_context_authorization_set([stock_entry]),
        )
        updated = self.store.update_room(room["id"], {
            "expected_settings_version": room["settings_version"],
            "stock_room_scope": {
                "version": STOCK_ROOM_SCOPE_VERSION,
                "symbols": ["US:AAPL"],
            },
        })
        self.assertIsNotNone(updated)
        with self.assertRaises(RoundContextError) as drift:
            self.store.create_formal_round(
                room["id"],
                "Reject the stale stock scope.",
                round_context_prepared=prepared,
            )
        self.assertEqual(drift.exception.code, "STOCK_RESEARCH_SCOPE_MISMATCH")
        with closing(sqlite3.connect(self.database_path)) as connection:
            count = int(connection.execute(
                "SELECT COUNT(*) FROM rounds WHERE room_id=?",
                (room["id"],),
            ).fetchone()[0])
        self.assertEqual(count, 0)

    def test_project_football_and_stock_contexts_sort_and_anchor_together(self) -> None:
        stock_scope = {
            "version": STOCK_ROOM_SCOPE_VERSION,
            "symbols": ["US:AAPL", "US:MSFT"],
        }
        room = self._room(
            [
                "stock_research_readonly",
                "project_round_focus",
                "football_research_readonly",
            ],
            stock_room_scope=stock_scope,
        )
        football_value = self._football_payload(room["id"])
        stock_value = self._stock_payload(room["id"])
        prepared = build_round_context_prepared([
            self._stock_entry(room["id"], stock_value),
            round_context_prepared_entry(
                "project_round_focus",
                "core.round.context/v1",
                self._project_prepared(room["id"]),
            ),
            self._football_entry(room["id"], football_value),
        ])
        round_row = self.store.create_formal_round(
            room["id"],
            "Freeze three provider-neutral contexts.",
            round_context_prepared=prepared,
        )
        frozen = self.store.get_round_contexts(room["id"], round_row["id"])
        expected_keys = [
            ("football_research_readonly", "core.football.match_context/v1"),
            ("project_round_focus", "core.round.context/v1"),
            ("stock_research_readonly", "core.market.readonly_context/v1"),
        ]

        self.assertEqual(round_row["round_domain_context_count"], 3)
        self.assertRegex(
            round_row["round_domain_contexts_sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertTrue((frozen or {})["integrity_ok"])
        self.assertEqual(
            [
                (row["owner_pack_id"], row["port_id"])
                for row in (frozen or {})["contexts"]
            ],
            expected_keys,
        )
        self.assertEqual(
            [
                (section["owner_pack_id"], section["port_id"])
                for section in prompt_sections(frozen)
            ],
            expected_keys,
        )

    def test_project_getter_accepts_second_context_and_any_tamper_fails_closed(self) -> None:
        room = self._room([
            "project_round_focus",
            "football_research_readonly",
        ])
        football_value = self._football_payload(room["id"])
        prepared = build_round_context_prepared([
            round_context_prepared_entry(
                "project_round_focus",
                "core.round.context/v1",
                self._project_prepared(room["id"]),
            ),
            self._football_entry(room["id"], football_value),
        ])
        round_row = self.store.create_formal_round(
            room["id"],
            "Verify both frozen contexts.",
            round_context_prepared=prepared,
        )
        focus = self.store.get_round_project_focus(room["id"], round_row["id"])
        self.assertTrue(focus["integrity_ok"])

        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute("DROP TRIGGER trg_round_domain_contexts_no_update")
            football_row = connection.execute(
                """SELECT id,preview_json FROM round_domain_contexts
                     WHERE round_id=? AND owner_pack_id=? AND port_id=?""",
                (
                    round_row["id"],
                    "football_research_readonly",
                    "core.football.match_context/v1",
                ),
            ).fetchone()
            preview = json.loads(football_row[1])
            preview["future_probability_available"] = True
            connection.execute(
                "UPDATE round_domain_contexts SET preview_json=? WHERE id=?",
                (json.dumps(preview, ensure_ascii=False), football_row[0]),
            )

        frozen = self.store.get_round_contexts(room["id"], round_row["id"])
        self.assertFalse(frozen["integrity_ok"])
        self.assertEqual(frozen["contexts"], [])
        redacted_focus = self.store.get_round_project_focus(
            room["id"], round_row["id"]
        )
        self.assertFalse(redacted_focus["integrity_ok"])
        self.assertFalse(redacted_focus["metrics_visible"])
        trace = self.store.round_execution_trace(room["id"], round_row["id"])
        issue_codes = {
            issue["code"] for issue in trace["integrity"]["issues"]
        }
        self.assertIn("ROUND_DOMAIN_CONTEXT_INTEGRITY_FAILED", issue_codes)


if __name__ == "__main__":
    unittest.main()
