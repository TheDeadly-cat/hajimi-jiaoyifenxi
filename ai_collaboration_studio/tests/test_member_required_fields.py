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
from urllib.request import Request, urlopen

from backend import http_server
from backend.store import StudioStore


REQUIRED_MEMBER_FIELDS = (
    "name",
    "identity",
    "responsibilities",
    "boundaries",
)


def valid_member_payload() -> dict[str, str]:
    return {
        "name": "质量审查员",
        "identity": "负责身份完整性的协作成员",
        "responsibilities": "核对证据、假设和结论是否一致。",
        "boundaries": "不代替用户决策，不执行真实交易。",
    }


def persisted_member_state(
    store: StudioStore,
    room_id: str,
    member_id: str,
) -> dict[str, object]:
    with closing(sqlite3.connect(store.path)) as connection:
        connection.row_factory = sqlite3.Row
        room = connection.execute(
            "SELECT updated_at FROM rooms WHERE id=?",
            (room_id,),
        ).fetchone()
        member = connection.execute(
            "SELECT * FROM members WHERE room_id=? AND id=?",
            (room_id, member_id),
        ).fetchone()
        versions = connection.execute(
            """SELECT version,snapshot_json,changed_at FROM member_versions
               WHERE room_id=? AND member_id=? ORDER BY version""",
            (room_id, member_id),
        ).fetchall()
        counts = connection.execute(
            """SELECT
                   (SELECT COUNT(*) FROM members WHERE room_id=?) AS members,
                   (SELECT COUNT(*) FROM member_versions WHERE room_id=?) AS versions""",
            (room_id, room_id),
        ).fetchone()
    return {
        "room_updated_at": int(room["updated_at"]),
        "member": dict(member),
        "versions": [tuple(row) for row in versions],
        "member_count": int(counts["members"]),
        "version_count": int(counts["versions"]),
    }


class MemberRequiredFieldStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "member-required-store.sqlite3")
        created = self.store.create_room(
            "成员必填字段测试",
            "验证绕过前端的 Store 写入也会失败关闭",
            template_id="open_collaboration",
        )
        self.room_id = str(created["room"]["id"])
        self.member = self.store.room_snapshot(self.room_id)["members"][0]

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_store_rejects_blank_required_fields_without_persisted_side_effects(self) -> None:
        before = persisted_member_state(
            self.store,
            self.room_id,
            str(self.member["id"]),
        )

        for field in REQUIRED_MEMBER_FIELDS:
            with self.subTest(operation="add", field=field):
                payload = valid_member_payload()
                payload[field] = " \t\r\n "
                with self.assertRaisesRegex(ValueError, "不能为空"):
                    self.store.add_member(self.room_id, payload)

            with self.subTest(operation="patch", field=field):
                with self.assertRaisesRegex(ValueError, "不能为空"):
                    self.store.update_member(
                        self.room_id,
                        str(self.member["id"]),
                        {field: " \t\r\n "},
                        expected_version=int(self.member["version"]),
                    )

        after = persisted_member_state(
            self.store,
            self.room_id,
            str(self.member["id"]),
        )
        self.assertEqual(after, before)


class MemberRequiredFieldHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "member-required-http.sqlite3")
        created = self.store.create_room(
            "HTTP 成员必填字段测试",
            "验证绕过前端的 HTTP 写入也会失败关闭",
            template_id="open_collaboration",
        )
        self.room_id = str(created["room"]["id"])
        self.member = self.store.room_snapshot(self.room_id)["members"][0]
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

    def request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, object],
    ) -> tuple[int, dict[str, object]]:
        request = Request(
            f"{self.base_url}{path}",
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
            },
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def test_http_rejects_blank_required_fields_without_persisted_side_effects(self) -> None:
        before = persisted_member_state(
            self.store,
            self.room_id,
            str(self.member["id"]),
        )

        for field in REQUIRED_MEMBER_FIELDS:
            with self.subTest(operation="post", field=field):
                payload = valid_member_payload()
                payload[field] = " \t\r\n "
                status, body = self.request_json(
                    "POST",
                    f"/api/rooms/{self.room_id}/members",
                    payload,
                )
                self.assertEqual(status, 400)
                self.assertFalse(body["ok"])
                self.assertIn("不能为空", str(body["error"]))

            with self.subTest(operation="patch", field=field):
                status, body = self.request_json(
                    "PATCH",
                    f"/api/rooms/{self.room_id}/members/{self.member['id']}",
                    {
                        "expected_version": int(self.member["version"]),
                        field: " \t\r\n ",
                    },
                )
                self.assertEqual(status, 400)
                self.assertFalse(body["ok"])
                self.assertIn("不能为空", str(body["error"]))

        after = persisted_member_state(
            self.store,
            self.room_id,
            str(self.member["id"]),
        )
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
