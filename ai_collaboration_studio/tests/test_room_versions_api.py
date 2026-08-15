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
from urllib.request import urlopen

from backend import http_server
from backend.store import StudioStore


def create_versioned_room(store: StudioStore) -> tuple[dict, dict]:
    version_one = store.room_snapshot("room_plan")["room"]
    version_two = store.update_room("room_plan", {
        "expected_settings_version": version_one["settings_version"],
        "title": "方案共创会 v2",
        "category": "项目研究 / 版本测试",
    })
    if version_two is None:
        raise AssertionError("room_plan should be updateable")
    return version_one, version_two


class RoomVersionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "room-versions.sqlite3"
        self.store = StudioStore(self.db_path)
        self.version_one, self.version_two = create_versioned_room(self.store)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_exact_versions_are_frozen_and_noop_does_not_create_a_version(self) -> None:
        listed = self.store.list_room_versions("room_plan")
        first = self.store.get_room_version_record("room_plan", 1)
        second = self.store.get_room_version_record("room_plan", 2)

        self.assertEqual([item["version"] for item in listed["versions"]], [2, 1])
        self.assertTrue(all(item["integrity_ok"] for item in listed["versions"]))
        self.assertEqual(first["room_version"]["snapshot"]["title"], self.version_one["title"])
        self.assertEqual(second["room_version"]["snapshot"]["title"], "方案共创会 v2")
        self.assertEqual(
            second["room_version"]["snapshot"]["category_path"],
            ["项目研究", "版本测试"],
        )

        unchanged = self.store.update_room("room_plan", {
            "expected_settings_version": self.version_two["settings_version"],
            "title": self.version_two["title"],
        })

        self.assertEqual(unchanged["settings_version"], self.version_two["settings_version"])
        self.assertEqual(
            [item["version"] for item in self.store.list_room_versions("room_plan")["versions"]],
            [2, 1],
        )

    def test_room_activity_does_not_invalidate_settings_token(self) -> None:
        before = self.store.room_snapshot("room_plan")["room"]
        self.store.add_message(
            "room_plan",
            sender_type="user",
            sender_id="user",
            sender_name="我",
            content="这条消息只改变房间活动时间。",
        )
        after_activity = self.store.room_snapshot("room_plan")["room"]

        self.assertGreater(after_activity["updated_at"], before["updated_at"])
        self.assertEqual(after_activity["settings_version"], before["settings_version"])

        updated = self.store.update_room("room_plan", {
            "expected_settings_version": before["settings_version"],
            "objective": "活动发生后仍可用原设置令牌安全保存。",
        })

        self.assertEqual(updated["settings_version"], before["settings_version"] + 1)

    def test_explicit_moderator_is_validated_and_versioned(self) -> None:
        current = self.store.room_snapshot("room_plan")
        moderator = current["members"][1]

        updated = self.store.update_room("room_plan", {
            "expected_settings_version": current["room"]["settings_version"],
            "moderator_member_id": moderator["id"],
        })
        frozen = self.store.get_room_version_record(
            "room_plan",
            updated["settings_version"],
        )

        self.assertEqual(updated["moderator_member_id"], moderator["id"])
        self.assertEqual(
            frozen["room_version"]["snapshot"]["moderator_member_id"],
            moderator["id"],
        )
        with self.assertRaisesRegex(ValueError, "主持成员不存在"):
            self.store.update_room("room_plan", {
                "expected_settings_version": updated["settings_version"],
                "moderator_member_id": "member_not_in_room",
            })

    def test_corrupt_snapshot_is_visible_in_list_and_exact_read_fails_closed(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE room_versions SET snapshot_json=? WHERE room_id='room_plan' AND version=1",
                ('{"incomplete":',),
            )

        listed = self.store.list_room_versions("room_plan")
        version_one = next(item for item in listed["versions"] if item["version"] == 1)

        self.assertFalse(version_one["integrity_ok"])
        self.assertIn("ROOM_VERSION_SNAPSHOT_INVALID", version_one["integrity_issues"])
        with self.assertRaisesRegex(ValueError, "房间设置历史版本快照损坏"):
            self.store.get_room_version_record("room_plan", 1)

    def test_missing_and_cross_room_versions_return_none(self) -> None:
        self.assertIsNone(self.store.get_room_version_record("room_plan", 99))
        self.assertIsNone(self.store.list_room_versions("room_missing"))


class RoomVersionHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "room-versions-http.sqlite3"
        self.store = StudioStore(self.db_path)
        create_versioned_room(self.store)
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

    def test_http_list_and_exact_detail_are_read_only(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as monitor:
            monitor.execute("PRAGMA query_only=ON")
            before_data_version = monitor.execute("PRAGMA data_version").fetchone()[0]
            before_rows = monitor.execute(
                "SELECT version,snapshot_json,changed_at FROM room_versions WHERE room_id='room_plan' ORDER BY version",
            ).fetchall()

            list_status, listed = self.get_json("/api/rooms/room_plan/versions")
            detail_status, detail = self.get_json("/api/rooms/room_plan/versions/1")

            after_data_version = monitor.execute("PRAGMA data_version").fetchone()[0]
            after_rows = monitor.execute(
                "SELECT version,snapshot_json,changed_at FROM room_versions WHERE room_id='room_plan' ORDER BY version",
            ).fetchall()

        self.assertEqual(list_status, 200)
        self.assertEqual([item["version"] for item in listed["versions"]], [2, 1])
        self.assertEqual(detail_status, 200)
        self.assertEqual(detail["room_version"]["snapshot"]["settings_version"], 1)
        self.assertEqual(after_data_version, before_data_version)
        self.assertEqual(after_rows, before_rows)

    def test_http_corrupt_detail_returns_409_and_list_keeps_audit_record(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE room_versions SET snapshot_json=? WHERE room_id='room_plan' AND version=1",
                ('{"incomplete":',),
            )

        list_status, listed = self.get_json("/api/rooms/room_plan/versions")
        detail_status, detail = self.get_json("/api/rooms/room_plan/versions/1")

        self.assertEqual(list_status, 200)
        self.assertFalse(next(item for item in listed["versions"] if item["version"] == 1)["integrity_ok"])
        self.assertEqual(detail_status, 409)
        self.assertEqual(detail["error_code"], "ROOM_VERSION_CORRUPT")
        self.assertNotIn("room_version", detail)


if __name__ == "__main__":
    unittest.main()
