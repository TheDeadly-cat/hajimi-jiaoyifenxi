from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

from backend import http_server
from backend.store import (
    MESSAGE_HISTORY_MAX_LIMIT,
    MESSAGE_HISTORY_MAX_QUERY_CHARS,
    StudioStore,
)


class MessageHistoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "message-history.sqlite3"
        self.store = StudioStore(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "DELETE FROM messages WHERE room_id IN ('room_plan','room_storage')"
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def add_message(
        self,
        content: str,
        created_at: int,
        *,
        room_id: str = "room_plan",
        round_id: str = "",
        citations: list[dict] | None = None,
    ) -> dict:
        message = self.store.add_message(
            room_id,
            sender_type="user",
            sender_id="user",
            sender_name="User",
            content=content,
            round_id=round_id,
            citations=citations,
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE messages SET created_at=? WHERE id=? AND room_id=?",
                (created_at, message["id"], room_id),
            )
        return {**message, "created_at": created_at}

    def test_keyset_pages_are_stable_and_do_not_repeat_tied_timestamps(self) -> None:
        created = [
            self.add_message(f"keyset record {index}", 1_000 + index // 2)
            for index in range(7)
        ]
        expected_ids = [
            row[0]
            for row in sorted(
                [(item["id"], item["created_at"]) for item in created],
                key=lambda item: (item[1], item[0]),
            )
        ]

        cursor = ""
        received_ids: list[str] = []
        while True:
            page = self.store.message_history(
                "room_plan",
                limit=2,
                before=cursor,
                query="keyset record",
            )
            self.assertIsNotNone(page)
            received_ids.extend(message["id"] for message in page["messages"])
            if not page["has_more"]:
                self.assertEqual(page["next_cursor"], "")
                break
            self.assertTrue(page["next_cursor"])
            cursor = page["next_cursor"]

        self.assertEqual(len(received_ids), len(set(received_ids)))
        self.assertEqual(received_ids, [
            *expected_ids[-2:],
            *expected_ids[-4:-2],
            *expected_ids[-6:-4],
            *expected_ids[:-6],
        ])

    def test_recent_messages_are_deterministic_for_tied_timestamps(self) -> None:
        created = [
            self.add_message(f"recent tied record {index}", 1_500)
            for index in range(5)
        ]

        recent = self.store.recent_messages("room_plan", limit=5)

        self.assertEqual(
            [message["id"] for message in recent],
            sorted(message["id"] for message in created),
        )

    def test_search_is_literal_decorated_and_isolated_by_room(self) -> None:
        material = self.store.add_material("room_plan", {
            "title": "History evidence",
            "content": "Evidence body",
        })
        matched = self.add_message(
            "needle margin 5%",
            2_000,
            citations=[{"id": material["id"], "version": material["version"]}],
        )
        member = self.store.enabled_members("room_plan")[0]
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """INSERT INTO message_mentions(
                       message_id,room_id,member_id,member_version,sequence_no,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (matched["id"], "room_plan", member["id"], member["version"], 0, 2_000),
            )
        self.add_message("needle margin 50", 2_001)
        self.add_message("needle margin 5%", 2_002, room_id="room_storage")

        percent_page = self.store.message_history("room_plan", query="5%")
        injection_page = self.store.message_history(
            "room_plan",
            query="needle' OR 1=1 --",
        )

        self.assertEqual([item["id"] for item in percent_page["messages"]], [matched["id"]])
        self.assertEqual(percent_page["messages"][0]["citations"][0]["title"], "History evidence")
        self.assertEqual(percent_page["messages"][0]["mentions"][0]["member_id"], member["id"])
        self.assertEqual(injection_page["messages"], [])

    def test_page_returns_only_director_decisions_for_its_room_and_rounds(self) -> None:
        plan_round = self.store.create_round("room_plan", "Plan history round")
        storage_round = self.store.create_round("room_storage", "Storage history round")
        self.add_message("director page target", 3_000, round_id=plan_round["id"])
        self.add_message(
            "director page target",
            3_001,
            room_id="room_storage",
            round_id=storage_round["id"],
        )
        plan_decision = self.store.add_director_decision(
            "room_plan",
            plan_round["id"],
            action="finish",
            reason="plan decision",
        )
        self.store.add_director_decision(
            "room_storage",
            storage_round["id"],
            action="finish",
            reason="storage decision",
        )

        page = self.store.message_history("room_plan", query="director page target")

        self.assertEqual(
            [decision["id"] for decision in page["director_decisions"]],
            [plan_decision["id"]],
        )
        self.assertTrue(all(item["room_id"] == "room_plan" for item in page["messages"]))
        self.assertTrue(all(item["room_id"] == "room_plan" for item in page["director_decisions"]))

    def test_snapshot_exposes_cursor_for_messages_older_than_initial_window(self) -> None:
        for index in range(121):
            self.add_message(f"snapshot history {index}", 4_000 + index)

        snapshot = self.store.room_snapshot("room_plan")
        metadata = snapshot["message_history"]
        older = self.store.message_history(
            "room_plan",
            before=metadata["next_cursor"],
            query="snapshot history",
        )

        self.assertEqual(len(snapshot["messages"]), 120)
        self.assertTrue(metadata["has_more"])
        self.assertEqual(len(older["messages"]), 1)
        self.assertFalse(older["has_more"])

    def test_limits_and_invalid_cursor_fail_closed(self) -> None:
        for index in range(MESSAGE_HISTORY_MAX_LIMIT + 3):
            self.add_message(f"bounded result {index}", 5_000 + index)

        capped = self.store.message_history("room_plan", limit=10_000, query="bounded result")

        self.assertEqual(len(capped["messages"]), MESSAGE_HISTORY_MAX_LIMIT)
        self.assertTrue(capped["has_more"])
        with self.assertRaisesRegex(ValueError, "游标无效"):
            self.store.message_history("room_plan", before="not-a-valid-cursor")
        with self.assertRaisesRegex(ValueError, "最多"):
            self.store.message_history(
                "room_plan",
                query="x" * (MESSAGE_HISTORY_MAX_QUERY_CHARS + 1),
            )
        self.assertIsNone(self.store.message_history("missing-room"))


class MessageHistoryHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "message-history-http.sqlite3")
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("DELETE FROM messages WHERE room_id='room_plan'")
        for index in range(MESSAGE_HISTORY_MAX_LIMIT + 2):
            self.store.add_message(
                "room_plan",
                sender_type="user",
                sender_id="user",
                sender_name="User",
                content=f"http history result {index}",
            )
        self.original_store = http_server.STORE
        http_server.STORE = self.store
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), http_server.StudioRequestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        http_server.STORE = self.original_store
        self.temp_dir.cleanup()

    def get_json(self, path: str) -> tuple[int, dict]:
        try:
            with urlopen(f"{self.base_url}{path}", timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def test_get_messages_supports_search_cursor_and_bounded_limit(self) -> None:
        status, first = self.get_json(
            "/api/rooms/room_plan/messages?" + urlencode({
                "q": "http history result",
                "limit": 10_000,
            })
        )
        second_status, second = self.get_json(
            "/api/rooms/room_plan/messages?" + urlencode({
                "q": "http history result",
                "before": first["next_cursor"],
            })
        )

        self.assertEqual(status, 200)
        self.assertEqual(len(first["messages"]), MESSAGE_HISTORY_MAX_LIMIT)
        self.assertTrue(first["has_more"])
        self.assertEqual(second_status, 200)
        self.assertEqual(len(second["messages"]), 2)
        self.assertFalse(second["has_more"])
        self.assertFalse(
            {item["id"] for item in first["messages"]}
            & {item["id"] for item in second["messages"]}
        )

    def test_get_messages_rejects_bad_input_and_missing_room(self) -> None:
        invalid_cursor_status, _ = self.get_json(
            "/api/rooms/room_plan/messages?before=invalid%21cursor"
        )
        long_query_status, _ = self.get_json(
            "/api/rooms/room_plan/messages?" + urlencode({
                "q": "x" * (MESSAGE_HISTORY_MAX_QUERY_CHARS + 1),
            })
        )
        invalid_limit_status, _ = self.get_json(
            "/api/rooms/room_plan/messages?limit=not-an-integer"
        )
        missing_status, _ = self.get_json("/api/rooms/missing-room/messages")

        self.assertEqual(invalid_cursor_status, 400)
        self.assertEqual(long_query_status, 400)
        self.assertEqual(invalid_limit_status, 400)
        self.assertEqual(missing_status, 404)


if __name__ == "__main__":
    unittest.main()
