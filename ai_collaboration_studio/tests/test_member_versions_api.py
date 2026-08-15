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


def create_versioned_member(store: StudioStore) -> tuple[str, dict, dict, dict, dict]:
    created = store.create_room(
        "成员身份历史测试",
        "验证精确身份快照、生命周期状态和只读审计",
        template_id="open_collaboration",
    )
    room_id = str(created["room"]["id"])
    version_one = store.room_snapshot(room_id)["members"][0]
    version_two = store.update_member(
        room_id,
        version_one["id"],
        {
            "identity": "版本二身份",
            "responsibilities": "核验第二版职责。",
            "boundaries": "不得覆盖第一版历史。",
            "stance": "evidence",
            "workflow_stage": "analysis",
            "capabilities": ["evidence_review", "critical_review"],
            "provider": "deepseek",
            "model": "deepseek-history-test",
        },
        expected_version=version_one["version"],
    )
    if version_two is None:
        raise AssertionError("the seeded member should be updateable")
    version_three = store.archive_member(
        room_id,
        version_one["id"],
        expected_version=version_two["version"],
    )
    if version_three is None:
        raise AssertionError("the seeded member should be archivable")
    version_four = store.restore_member(
        room_id,
        version_one["id"],
        expected_version=version_three["version"],
    )
    if version_four is None:
        raise AssertionError("the seeded member should be restorable")
    return room_id, version_one, version_two, version_three, version_four


class MemberVersionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "member-versions.sqlite3"
        self.store = StudioStore(self.db_path)
        (
            self.room_id,
            self.version_one,
            self.version_two,
            self.version_three,
            self.version_four,
        ) = create_versioned_member(self.store)
        self.member_id = str(self.version_one["id"])

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_list_and_detail_expose_exact_lifecycle_snapshots_newest_first(self) -> None:
        listed = self.store.list_member_versions(self.room_id, self.member_id)

        self.assertIsNotNone(listed)
        self.assertEqual(listed["member"]["id"], self.member_id)
        self.assertEqual(listed["member"]["current_version"], 4)
        self.assertFalse(listed["member"]["archived"])
        self.assertEqual([item["version"] for item in listed["versions"]], [4, 3, 2, 1])
        self.assertTrue(all(item["integrity_ok"] for item in listed["versions"]))
        self.assertTrue(all("snapshot" not in item for item in listed["versions"]))
        by_version = {item["version"]: item for item in listed["versions"]}
        self.assertFalse(by_version[1]["archived"])
        self.assertTrue(by_version[1]["enabled"])
        self.assertEqual(by_version[2]["identity"], "版本二身份")
        self.assertEqual(by_version[2]["provider"], "deepseek")
        self.assertTrue(by_version[3]["archived"])
        self.assertFalse(by_version[3]["enabled"])
        self.assertFalse(by_version[4]["archived"])
        self.assertTrue(by_version[4]["enabled"])
        self.assertRegex(by_version[1]["snapshot_sha256"], r"^[0-9a-f]{64}$")

        first = self.store.get_member_version_record(self.room_id, self.member_id, 1)
        second = self.store.get_member_version_record(self.room_id, self.member_id, 2)
        archived = self.store.get_member_version_record(self.room_id, self.member_id, 3)
        restored = self.store.get_member_version_record(self.room_id, self.member_id, 4)

        self.assertEqual(first["member_version"]["snapshot"]["identity"], self.version_one["identity"])
        self.assertEqual(second["member_version"]["snapshot"]["identity"], "版本二身份")
        self.assertEqual(
            second["member_version"]["snapshot"]["boundaries"],
            "不得覆盖第一版历史。",
        )
        self.assertEqual(
            second["member_version"]["snapshot"]["capabilities"],
            ["evidence_review", "critical_review"],
        )
        self.assertTrue(archived["member_version"]["snapshot"]["archived"])
        self.assertFalse(restored["member_version"]["snapshot"]["archived"])
        self.assertNotEqual(
            first["member_version"]["snapshot_sha256"],
            second["member_version"]["snapshot_sha256"],
        )

        compatible_snapshot = self.store.get_member_version(self.room_id, self.member_id, 2)
        self.assertIsInstance(compatible_snapshot, dict)
        self.assertEqual(compatible_snapshot["identity"], "版本二身份")
        self.assertNotIn("member_version", compatible_snapshot)

    def test_limit_missing_version_and_cross_room_reads(self) -> None:
        limited = self.store.list_member_versions(self.room_id, self.member_id, limit=2)
        self.assertEqual([item["version"] for item in limited["versions"]], [4, 3])
        other = self.store.create_room(
            "另一个房间",
            "跨房间身份历史必须不可见",
            template_id="open_collaboration",
        )
        other_room_id = str(other["room"]["id"])

        self.assertIsNone(self.store.list_member_versions(other_room_id, self.member_id))
        self.assertIsNone(self.store.list_member_versions(self.room_id, "member_missing"))
        self.assertIsNone(
            self.store.get_member_version_record(self.room_id, self.member_id, 99)
        )
        self.assertIsNone(
            self.store.get_member_version_record(other_room_id, self.member_id, 1)
        )

    def test_invalid_json_is_visible_in_list_and_exact_detail_fails_closed(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """UPDATE member_versions SET snapshot_json=?
                   WHERE member_id=? AND version=1""",
                ('{"incomplete":', self.member_id),
            )

        listed = self.store.list_member_versions(self.room_id, self.member_id)
        by_version = {item["version"]: item for item in listed["versions"]}

        self.assertFalse(by_version[1]["integrity_ok"])
        self.assertIn("MEMBER_VERSION_SNAPSHOT_INVALID", by_version[1]["integrity_issues"])
        self.assertNotIn("snapshot", by_version[1])
        self.assertTrue(by_version[2]["integrity_ok"])
        with self.assertRaisesRegex(ValueError, "成员身份历史版本快照损坏"):
            self.store.get_member_version_record(self.room_id, self.member_id, 1)

    def test_identity_mismatch_is_an_integrity_failure(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            row = connection.execute(
                """SELECT snapshot_json FROM member_versions
                   WHERE member_id=? AND version=2""",
                (self.member_id,),
            ).fetchone()
            snapshot = json.loads(row[0])
            snapshot["id"] = "member_from_another_history"
            connection.execute(
                """UPDATE member_versions SET snapshot_json=?
                   WHERE member_id=? AND version=2""",
                (json.dumps(snapshot, ensure_ascii=False), self.member_id),
            )

        listed = self.store.list_member_versions(self.room_id, self.member_id)
        version_two = next(item for item in listed["versions"] if item["version"] == 2)

        self.assertFalse(version_two["integrity_ok"])
        self.assertIn("MEMBER_VERSION_IDENTITY_MISMATCH", version_two["integrity_issues"])
        with self.assertRaisesRegex(ValueError, "MEMBER_VERSION_IDENTITY_MISMATCH"):
            self.store.get_member_version_record(self.room_id, self.member_id, 2)


class MemberVersionHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "member-versions-http.sqlite3"
        self.store = StudioStore(self.db_path)
        (
            self.room_id,
            self.version_one,
            self.version_two,
            self.version_three,
            self.version_four,
        ) = create_versioned_member(self.store)
        self.member_id = str(self.version_one["id"])
        other = self.store.create_room(
            "HTTP 跨房间测试",
            "跨房间读取必须返回 404",
            template_id="open_collaboration",
        )
        self.other_room_id = str(other["room"]["id"])
        self.original_store = http_server.STORE
        http_server.STORE = self.store
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            http_server.StudioRequestHandler,
        )
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
            with urlopen(f"{self.base_url}{path}", timeout=5) as response:
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
            before_member = monitor.execute(
                "SELECT * FROM members WHERE id=?",
                (self.member_id,),
            ).fetchone()
            before_versions = monitor.execute(
                """SELECT version,snapshot_json,changed_at FROM member_versions
                   WHERE member_id=? ORDER BY version""",
                (self.member_id,),
            ).fetchall()

            list_status, listed = self.get_json(
                f"/api/rooms/{self.room_id}/members/{self.member_id}/versions?limit=2"
            )
            detail_status, detail = self.get_json(
                f"/api/rooms/{self.room_id}/members/{self.member_id}/versions/2"
            )

            after_data_version = monitor.execute("PRAGMA data_version").fetchone()[0]
            after_member = monitor.execute(
                "SELECT * FROM members WHERE id=?",
                (self.member_id,),
            ).fetchone()
            after_versions = monitor.execute(
                """SELECT version,snapshot_json,changed_at FROM member_versions
                   WHERE member_id=? ORDER BY version""",
                (self.member_id,),
            ).fetchall()

        self.assertEqual(list_status, 200)
        self.assertTrue(listed["ok"])
        self.assertEqual([item["version"] for item in listed["versions"]], [4, 3])
        self.assertEqual(detail_status, 200)
        self.assertTrue(detail["ok"])
        self.assertEqual(detail["member_version"]["version"], 2)
        self.assertEqual(detail["member_version"]["snapshot"]["identity"], "版本二身份")
        self.assertEqual(after_data_version, before_data_version)
        self.assertEqual(after_member, before_member)
        self.assertEqual(after_versions, before_versions)

    def test_http_missing_cross_room_and_bad_limit_contracts(self) -> None:
        cases = [
            (
                f"/api/rooms/{self.room_id}/members/member_missing/versions",
                404,
            ),
            (
                f"/api/rooms/{self.other_room_id}/members/{self.member_id}/versions",
                404,
            ),
            (
                f"/api/rooms/{self.room_id}/members/{self.member_id}/versions/99",
                404,
            ),
            (
                f"/api/rooms/{self.other_room_id}/members/{self.member_id}/versions/1",
                404,
            ),
            (
                f"/api/rooms/{self.room_id}/members/{self.member_id}/versions?limit=invalid",
                400,
            ),
        ]

        for path, expected_status in cases:
            with self.subTest(path=path):
                status, payload = self.get_json(path)
                self.assertEqual(status, expected_status)
                self.assertFalse(payload["ok"])

    def test_http_corrupt_detail_returns_member_version_error_code(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """UPDATE member_versions SET snapshot_json=?
                   WHERE member_id=? AND version=1""",
                ('{"incomplete":', self.member_id),
            )

        list_status, listed = self.get_json(
            f"/api/rooms/{self.room_id}/members/{self.member_id}/versions"
        )
        detail_status, detail = self.get_json(
            f"/api/rooms/{self.room_id}/members/{self.member_id}/versions/1"
        )
        by_version = {item["version"]: item for item in listed["versions"]}

        self.assertEqual(list_status, 200)
        self.assertFalse(by_version[1]["integrity_ok"])
        self.assertEqual(detail_status, 409)
        self.assertFalse(detail["ok"])
        self.assertEqual(detail["error_code"], "MEMBER_VERSION_CORRUPT")
        self.assertNotIn("member_version", detail)
        self.assertNotIn("snapshot", detail)


if __name__ == "__main__":
    unittest.main()
