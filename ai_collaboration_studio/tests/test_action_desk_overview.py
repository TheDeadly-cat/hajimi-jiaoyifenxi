from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest.mock import patch


os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend import http_server  # noqa: E402
from backend.action_desk import (  # noqa: E402
    ACTION_DESK_COUNT_FIELDS,
    ACTION_DESK_ITEM_FIELDS,
    ACTION_DESK_OVERVIEW_MAX_ITEMS,
    ACTION_DESK_OVERVIEW_MAX_ITEMS_PER_ROOM,
    ACTION_DESK_OVERVIEW_MAX_ROOMS,
    ACTION_DESK_OVERVIEW_COUNT_FIELDS,
    ACTION_DESK_OVERVIEW_VERSION,
    ACTION_DESK_ROOM_SUMMARY_VERSION,
    ACTION_TRANSITION_REQUEST_VERSION,
    FIXED_ACTION_DESK_OVERVIEW_SAFETY,
    ActionDeskError,
)
from backend.store import StudioStore  # noqa: E402


class CountingStudioStore(StudioStore):
    def __init__(self, path: Path) -> None:
        self.connect_calls = 0
        super().__init__(path)

    def _connect(self) -> sqlite3.Connection:
        self.connect_calls += 1
        return super()._connect()


class ActionDeskOverviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "action-desk-overview.sqlite3"
        self.store = CountingStudioStore(self.db_path)
        self.alpha = self.store.create_room(
            "Alpha room",
            "First room in the stable overview.",
            capability_pack_ids=[],
        )["room"]
        self.beta = self.store.create_room(
            "beta room",
            "Second room in the stable overview.",
            capability_pack_ids=[],
        )["room"]
        self.room_count = len(self.store.list_rooms())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_shared_v1_fixture_matches_empty_backend_projection(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "action_desk_overview_v1.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.assertEqual(set(fixture), {"ok", "action_desk_overview"})
        self.assertIs(fixture["ok"], True)

        empty_store = StudioStore(Path(self.temp_dir.name) / "empty-contract.sqlite3")
        actual = empty_store.action_desk_overview()
        fixture_overview = fixture["action_desk_overview"]
        self.assertEqual(actual, fixture_overview)
        self.assertEqual(
            [room["room_id"] for room in actual["rooms"]],
            ["room_sports", "room_plan", "room_storage", "room_market", "room_project"],
        )
        for field in FIXED_ACTION_DESK_OVERVIEW_SAFETY:
            self.assertEqual(actual[field], fixture_overview[field])

    def _adopt_action(
        self,
        room: dict,
        *,
        action_id: str,
        text: str,
        state: str,
        note: str,
    ) -> dict:
        material = self.store.add_material(room["id"], {
            "title": f"Evidence for {action_id}",
            "kind": "note",
            "content": f"Local isolated evidence for {action_id}.",
        })
        evidence = [{
            "type": "material",
            "id": material["id"],
            "evidence_role": "support",
            "verification_status": "source_checked",
            "review_note": "checked",
        }]
        artifact = self.store.create_artifact(
            room["id"],
            title=f"Plan {action_id}",
            content={
                "summary": "A bounded follow-through plan.",
                "summary_evidence": evidence,
                "requirements": [],
                "risks": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [{
                    "id": action_id,
                    "text": text,
                    "owner": "User owner",
                    "due": "not-a-ranked-date",
                    "state": "open",
                    "evidence": evidence,
                }],
            },
        )
        confirmed = self.store.confirm_artifact(
            room["id"],
            artifact["id"],
            expected_version=artifact["version"],
            confirmed_by="user",
        )
        assert confirmed is not None
        candidate = self.store.action_desk(room["id"])["candidates"][0]
        request = {
            "version": ACTION_TRANSITION_REQUEST_VERSION,
            "client_request_id": f"adopt_{action_id}",
            "artifact_id": candidate["artifact_id"],
            "artifact_version": candidate["artifact_version"],
            "action_id": candidate["action_id"],
            "expected_action_snapshot_sha256": candidate["action_snapshot_sha256"],
            "expected_revision": 0,
            "transition": "adopt",
            "patch": {
                "owner": "User owner",
                "due": "not-a-ranked-date",
                "state": state,
                "note": note,
            },
            "user_confirmed": True,
        }
        result, created = self.store.transition_artifact_action(room["id"], request)
        self.assertTrue(created)
        return result

    def _table_counts(self) -> dict[str, int]:
        tables = (
            "rooms",
            "artifacts",
            "artifact_versions",
            "artifact_action_events",
            "artifact_action_heads",
            "artifact_action_anchors",
            "artifact_action_anchor_heads",
            "artifact_user_decisions",
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            return {
                table: int(connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0])
                for table in tables
            }

    def test_one_read_snapshot_projects_all_rooms_in_stable_closed_shape_without_writes(self) -> None:
        self._adopt_action(
            self.beta,
            action_id="beta_action",
            text="Prepare the beta acceptance note.",
            state="blocked",
            note="Waiting for user input.",
        )
        self._adopt_action(
            self.alpha,
            action_id="alpha_action",
            text="Prepare the alpha acceptance note.",
            state="in_progress",
            note="User explicitly started this item.",
        )
        before_counts = self._table_counts()
        connection_samples: list[tuple[int, bool]] = []
        original_projection = CountingStudioStore._action_desk_connection.__func__

        def observed_projection(
            store_type: type[CountingStudioStore],
            connection: sqlite3.Connection,
            room_id: str,
            *,
            room_exists_already: bool = False,
        ) -> dict:
            connection_samples.append((id(connection), connection.in_transaction))
            return original_projection(
                store_type,
                connection,
                room_id,
                room_exists_already=room_exists_already,
            )

        with closing(sqlite3.connect(self.db_path)) as watcher:
            before_data_version = watcher.execute("PRAGMA data_version").fetchone()[0]
            self.store.connect_calls = 0
            with patch.object(
                CountingStudioStore,
                "_action_desk_connection",
                classmethod(observed_projection),
            ):
                overview = self.store.action_desk_overview()
            after_data_version = watcher.execute("PRAGMA data_version").fetchone()[0]

        self.assertEqual(self.store.connect_calls, 1)
        self.assertEqual(len(connection_samples), self.room_count)
        self.assertEqual(len({sample[0] for sample in connection_samples}), 1)
        self.assertTrue(all(sample[1] for sample in connection_samples))
        self.assertEqual(before_data_version, after_data_version)
        self.assertEqual(before_counts, self._table_counts())
        self.assertEqual(overview["version"], ACTION_DESK_OVERVIEW_VERSION)
        self.assertTrue(overview["integrity_ok"])
        self.assertEqual(overview["issues"], [])
        self.assertEqual(
            set(overview),
            {
                "version",
                "integrity_ok",
                "rooms",
                "counts",
                "issues",
                *FIXED_ACTION_DESK_OVERVIEW_SAFETY,
            },
        )
        room_order = [
            (room["room_title"], room["room_id"])
            for room in overview["rooms"]
        ]
        self.assertEqual(
            room_order,
            sorted(room_order, key=lambda room: (room[0].casefold(), room[0], room[1])),
        )
        self.assertEqual(set(overview["counts"]), set(ACTION_DESK_OVERVIEW_COUNT_FIELDS))
        self.assertEqual(overview["counts"]["room_count"], self.room_count)
        self.assertEqual(overview["counts"]["healthy_room_count"], self.room_count)
        self.assertEqual(overview["counts"]["failed_room_count"], 0)
        self.assertEqual(overview["counts"]["item_count"], 2)
        self.assertEqual(overview["counts"]["in_progress_count"], 1)
        self.assertEqual(overview["counts"]["blocked_count"], 1)
        for room in overview["rooms"]:
            self.assertEqual(room["version"], ACTION_DESK_ROOM_SUMMARY_VERSION)
            self.assertEqual(
                set(room),
                {"version", "room_id", "room_title", "integrity_ok", "items", "counts", "issues"},
            )
            self.assertTrue(room["integrity_ok"])
            self.assertEqual(room["issues"], [])
            self.assertEqual(set(room["counts"]), set(ACTION_DESK_COUNT_FIELDS))
            if room["room_id"] in {self.alpha["id"], self.beta["id"]}:
                self.assertEqual(len(room["items"]), 1)
                self.assertEqual(set(room["items"][0]), set(ACTION_DESK_ITEM_FIELDS))
        for key, value in FIXED_ACTION_DESK_OVERVIEW_SAFETY.items():
            self.assertEqual(overview[key], value)

    def test_corrupt_room_is_fully_hidden_without_polluting_healthy_room_or_counts(self) -> None:
        self._adopt_action(
            self.alpha,
            action_id="healthy_action",
            text="HEALTHY_VISIBLE_ACTION",
            state="open",
            note="Healthy visible note.",
        )
        self._adopt_action(
            self.beta,
            action_id="bad_action",
            text="LEAK_ME_BAD_ACTION",
            state="blocked",
            note="LEAK_ME_BAD_NOTE",
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DROP TRIGGER trg_artifact_action_events_no_update")
            connection.execute(
                """UPDATE artifact_action_events
                      SET item_snapshot_json='{"note":"LEAK_ME_TAMPERED"}'
                    WHERE room_id=?""",
                (self.beta["id"],),
            )

        overview = self.store.action_desk_overview()
        self.assertFalse(overview["integrity_ok"])
        self.assertEqual(
            overview["counts"]["healthy_room_count"],
            self.room_count - 1,
        )
        self.assertEqual(overview["counts"]["failed_room_count"], 1)
        self.assertEqual(overview["counts"]["item_count"], 1)
        self.assertEqual(overview["counts"]["open_count"], 1)
        self.assertEqual(overview["counts"]["blocked_count"], 0)
        summaries = {room["room_id"]: room for room in overview["rooms"]}
        self.assertTrue(summaries[self.alpha["id"]]["integrity_ok"])
        self.assertEqual(
            summaries[self.alpha["id"]]["items"][0]["text"],
            "HEALTHY_VISIBLE_ACTION",
        )
        failed = summaries[self.beta["id"]]
        self.assertFalse(failed["integrity_ok"])
        self.assertEqual(failed["items"], [])
        self.assertEqual(failed["counts"], {field: 0 for field in ACTION_DESK_COUNT_FIELDS})
        self.assertEqual(
            failed["issues"],
            [{
                "code": "ACTION_DESK_ROOM_INTEGRITY_FAILED",
                "message": "This room's Action Desk failed integrity verification and was hidden.",
            }],
        )
        encoded = json.dumps(overview, ensure_ascii=False)
        self.assertNotIn("LEAK_ME_BAD_ACTION", encoded)
        self.assertNotIn("LEAK_ME_BAD_NOTE", encoded)
        self.assertNotIn("LEAK_ME_TAMPERED", encoded)

    def test_orphan_room_lineage_marks_overview_failed_without_projecting_orphan_identity(self) -> None:
        self._adopt_action(
            self.alpha,
            action_id="orphan_source",
            text="A healthy action remains visible.",
            state="done",
            note="Completed by the user.",
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                """INSERT INTO artifact_action_heads(
                       room_id,artifact_id,artifact_version,action_id,
                       action_snapshot_sha256,head_version,revision,sequence_no,
                       event_count,head_event_sha256,created_at,updated_at,head_sha256
                   )
                   SELECT 'orphan_room',artifact_id,artifact_version,action_id,
                          action_snapshot_sha256,head_version,revision,sequence_no,
                          event_count,head_event_sha256,created_at,updated_at,head_sha256
                     FROM artifact_action_heads
                    WHERE room_id=?""",
                (self.alpha["id"],),
            )

        overview = self.store.action_desk_overview()
        self.assertFalse(overview["integrity_ok"])
        self.assertEqual(
            overview["counts"]["healthy_room_count"],
            self.room_count,
        )
        self.assertEqual(overview["counts"]["failed_room_count"], 0)
        self.assertEqual(overview["counts"]["item_count"], 1)
        self.assertIn(
            "ACTION_DESK_ORPHAN_ROOM_LINEAGE",
            [issue["code"] for issue in overview["issues"]],
        )
        self.assertNotIn("orphan_room", json.dumps(overview, ensure_ascii=False))

    def test_corrupt_room_title_is_sanitized_and_does_not_break_healthy_rooms(self) -> None:
        corrupt_title = "T" * 501
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE rooms SET title=? WHERE id=?",
                (corrupt_title, self.beta["id"]),
            )
        overview = self.store.action_desk_overview()
        self.assertFalse(overview["integrity_ok"])
        self.assertEqual(
            overview["counts"]["healthy_room_count"],
            self.room_count - 1,
        )
        self.assertEqual(overview["counts"]["failed_room_count"], 1)
        summaries = {room["room_id"]: room for room in overview["rooms"]}
        self.assertTrue(summaries[self.alpha["id"]]["integrity_ok"])
        self.assertFalse(summaries[self.beta["id"]]["integrity_ok"])
        self.assertEqual(summaries[self.beta["id"]]["room_title"], "Unavailable room")
        self.assertNotIn(corrupt_title, json.dumps(overview, ensure_ascii=False))

    def test_http_endpoint_is_get_only_closed_and_rejects_every_v1_query(self) -> None:
        self._adopt_action(
            self.alpha,
            action_id="http_action",
            text="Read the Action Desk overview over local HTTP.",
            state="open",
            note="",
        )
        original_store = http_server.STORE
        http_server.STORE = self.store
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            http_server.StudioRequestHandler,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with patch.object(
                http_server.PROVIDERS,
                "status",
                side_effect=AssertionError("Provider status must not be read by Action Desk overview."),
            ):
                with urlopen(f"{base_url}/api/action-desk/overview", timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(set(payload), {"ok", "action_desk_overview"})
            self.assertTrue(payload["ok"])
            self.assertEqual(
                payload["action_desk_overview"]["version"],
                ACTION_DESK_OVERVIEW_VERSION,
            )
            for raw_query in ("state=open", "limit=1", "source=", "foo"):
                with self.assertRaises(HTTPError) as raised:
                    urlopen(
                        f"{base_url}/api/action-desk/overview?{raw_query}",
                        timeout=5,
                    )
                self.assertEqual(raised.exception.code, 400)
                error = json.loads(raised.exception.read().decode("utf-8"))
                self.assertEqual(
                    error["code"],
                    "ACTION_DESK_OVERVIEW_QUERY_UNSUPPORTED",
                )
                raised.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            http_server.STORE = original_store

    def test_v1_fails_closed_before_returning_an_oversized_overview(self) -> None:
        oversized_items = [
            {"artifact_id": "artifact", "artifact_version": 1, "action_id": str(index)}
            for index in range(ACTION_DESK_OVERVIEW_MAX_ITEMS_PER_ROOM + 1)
        ]
        oversized_summary = {
            "version": ACTION_DESK_ROOM_SUMMARY_VERSION,
            "room_id": self.alpha["id"],
            "room_title": "Alpha room",
            "integrity_ok": True,
            "items": oversized_items,
            "counts": {field: 0 for field in ACTION_DESK_COUNT_FIELDS},
            "issues": [],
        }
        with patch.object(
            CountingStudioStore,
            "_action_desk_connection",
            return_value={},
        ), patch(
            "backend.store.verified_action_desk_room_summary",
            return_value=oversized_summary,
        ):
            with self.assertRaises(ActionDeskError) as raised:
                self.store.action_desk_overview()
        self.assertEqual(raised.exception.code, "ACTION_DESK_OVERVIEW_LIMIT_EXCEEDED")
        self.assertEqual(raised.exception.status, 409)

    def test_http_translates_overview_limit_to_typed_409_without_partial_payload(self) -> None:
        original_store = http_server.STORE
        http_server.STORE = self.store
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            http_server.StudioRequestHandler,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            error = ActionDeskError(
                "The Action Desk overview exceeds its v1 item limit.",
                code="ACTION_DESK_OVERVIEW_LIMIT_EXCEEDED",
                status=409,
            )
            with patch.object(
                self.store,
                "action_desk_overview",
                side_effect=error,
            ):
                with self.assertRaises(HTTPError) as raised:
                    urlopen(f"{base_url}/api/action-desk/overview", timeout=5)
            self.assertEqual(raised.exception.code, 409)
            payload = json.loads(raised.exception.read().decode("utf-8"))
            self.assertEqual(
                payload,
                {
                    "ok": False,
                    "error": "The Action Desk overview exceeds its v1 item limit.",
                    "code": "ACTION_DESK_OVERVIEW_LIMIT_EXCEEDED",
                },
            )
            raised.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            http_server.STORE = original_store

    def test_v1_fails_closed_before_projecting_too_many_rooms(self) -> None:
        extra_count = ACTION_DESK_OVERVIEW_MAX_ROOMS + 1 - self.room_count
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.executemany(
                """INSERT INTO rooms(id,title,created_at,updated_at)
                   VALUES(?,?,?,?)""",
                [
                    (f"overview_limit_room_{index}", "Temporary room", 0, 0)
                    for index in range(extra_count)
                ],
            )

        with self.assertRaises(ActionDeskError) as raised:
            self.store.action_desk_overview()
        self.assertEqual(raised.exception.code, "ACTION_DESK_OVERVIEW_LIMIT_EXCEEDED")
        self.assertEqual(raised.exception.status, 409)

    def test_v1_fails_closed_before_projecting_too_many_total_items(self) -> None:
        room_target = (ACTION_DESK_OVERVIEW_MAX_ITEMS // ACTION_DESK_OVERVIEW_MAX_ITEMS_PER_ROOM) + 1
        extra_count = max(0, room_target - self.room_count)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.executemany(
                """INSERT INTO rooms(id,title,created_at,updated_at)
                   VALUES(?,?,?,?)""",
                [
                    (f"overview_total_limit_room_{index}", "Temporary room", 0, 0)
                    for index in range(extra_count)
                ],
            )

        def oversized_summary(*, room_id: str, room_title: str, desk: dict) -> dict:
            return {
                "version": ACTION_DESK_ROOM_SUMMARY_VERSION,
                "room_id": room_id,
                "room_title": room_title,
                "integrity_ok": True,
                "items": [{} for _ in range(ACTION_DESK_OVERVIEW_MAX_ITEMS_PER_ROOM)],
                "counts": {field: 0 for field in ACTION_DESK_COUNT_FIELDS},
                "issues": [],
            }

        with patch.object(
            CountingStudioStore,
            "_action_desk_connection",
            return_value={},
        ), patch(
            "backend.store.verified_action_desk_room_summary",
            side_effect=oversized_summary,
        ):
            with self.assertRaises(ActionDeskError) as raised:
                self.store.action_desk_overview()
        self.assertEqual(raised.exception.code, "ACTION_DESK_OVERVIEW_LIMIT_EXCEEDED")
        self.assertEqual(raised.exception.status, 409)


if __name__ == "__main__":
    unittest.main()
