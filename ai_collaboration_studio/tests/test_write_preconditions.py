from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend import http_server
from backend.store import StudioStore


def artifact_content() -> dict:
    return {
        "summary": "并发写入契约测试。",
        "summary_evidence": [],
        "requirements": [],
        "risks": [],
        "conclusions": [],
        "disagreements": [],
        "unknowns": [],
        "actions": [],
        "decision": {
            "status": "undecided",
            "options": [],
            "preferred_option_id": "",
            "rationale": "",
            "evidence": [],
        },
    }


class WritePreconditionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "write-preconditions.sqlite3")
        self.material = self.store.add_material("room_plan", {
            "title": "并发资料 v1",
            "content": "第一版资料。",
        })
        self.artifact = self.store.create_artifact(
            "room_plan",
            title="并发产物 v1",
            content=artifact_content(),
            generation_source="manual",
            created_by="test",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_room_material_and_artifact_updates_require_preconditions(self) -> None:
        room_before = self.store.room_snapshot("room_plan")["room"]
        with self.assertRaisesRegex(ValueError, "expected_settings_version"):
            self.store.update_room("room_plan", {"title": "不得覆盖"})
        with self.assertRaisesRegex(ValueError, "expected_version"):
            self.store.update_material(
                "room_plan",
                self.material["id"],
                {"content": "不得覆盖"},
            )
        with self.assertRaisesRegex(ValueError, "expected_version"):
            self.store.update_artifact(
                "room_plan",
                self.artifact["id"],
                {"title": "不得覆盖"},
            )

        room_after = self.store.room_snapshot("room_plan")["room"]
        material_after = self.store.get_material("room_plan", self.material["id"])
        artifact_after = self.store.get_artifact("room_plan", self.artifact["id"])
        self.assertEqual(room_after["title"], room_before["title"])
        self.assertEqual(room_after["updated_at"], room_before["updated_at"])
        self.assertEqual(material_after["version"], self.material["version"])
        self.assertEqual(material_after["content"], self.material["content"])
        self.assertEqual(artifact_after["version"], self.artifact["version"])
        self.assertEqual(artifact_after["title"], self.artifact["title"])

    def test_valid_token_writes_once_and_stale_retry_is_rejected(self) -> None:
        room = self.store.room_snapshot("room_plan")["room"]
        updated_room = self.store.update_room("room_plan", {
            "expected_settings_version": room["settings_version"],
            "title": "房间 v2",
        })
        updated_material = self.store.update_material("room_plan", self.material["id"], {
            "expected_version": self.material["version"],
            "content": "第二版资料。",
        })
        updated_artifact = self.store.update_artifact("room_plan", self.artifact["id"], {
            "expected_version": self.artifact["version"],
            "title": "并发产物 v2",
        })

        with self.assertRaisesRegex(ValueError, "已被其他操作更新"):
            self.store.update_room("room_plan", {
                "expected_settings_version": room["settings_version"],
                "title": "过期房间写入",
            })
        with self.assertRaisesRegex(ValueError, "版本已变化"):
            self.store.update_material("room_plan", self.material["id"], {
                "expected_version": self.material["version"],
                "content": "过期资料写入",
            })
        with self.assertRaisesRegex(ValueError, "已被其他修改更新"):
            self.store.update_artifact("room_plan", self.artifact["id"], {
                "expected_version": self.artifact["version"],
                "title": "过期产物写入",
            })

        self.assertGreater(updated_room["updated_at"], room["updated_at"])
        self.assertEqual(updated_room["settings_version"], room["settings_version"] + 1)
        self.assertEqual(updated_material["version"], self.material["version"] + 1)
        self.assertEqual(updated_artifact["version"], self.artifact["version"] + 1)


class WritePreconditionHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "write-preconditions-http.sqlite3")
        self.material = self.store.add_material("room_plan", {
            "title": "HTTP 资料 v1",
            "content": "第一版资料。",
        })
        self.artifact = self.store.create_artifact(
            "room_plan",
            title="HTTP 产物 v1",
            content=artifact_content(),
            generation_source="manual",
            created_by="test",
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

    def patch(self, path: str, payload: dict) -> tuple[int, dict]:
        request = Request(
            f"{self.base_url}{path}",
            method="PATCH",
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

    def test_http_missing_tokens_are_400_and_stale_tokens_are_409(self) -> None:
        room = self.store.room_snapshot("room_plan")["room"]
        paths = {
            "room": "/api/rooms/room_plan",
            "material": f"/api/rooms/room_plan/materials/{self.material['id']}",
            "artifact": f"/api/rooms/room_plan/artifacts/{self.artifact['id']}",
        }
        for name, path in paths.items():
            with self.subTest(name=name, condition="missing"):
                status, body = self.patch(path, {"title": "不得覆盖"})
                self.assertEqual(status, 400)
                self.assertFalse(body["ok"])

        self.assertEqual(self.patch(paths["room"], {
            "expected_settings_version": room["settings_version"],
            "title": "HTTP 房间 v2",
        })[0], 200)
        self.assertEqual(self.patch(paths["material"], {
            "expected_version": self.material["version"],
            "content": "HTTP 资料 v2",
        })[0], 200)
        self.assertEqual(self.patch(paths["artifact"], {
            "expected_version": self.artifact["version"],
            "title": "HTTP 产物 v2",
        })[0], 200)

        stale_payloads = {
            "room": {"expected_settings_version": room["settings_version"], "title": "过期房间"},
            "material": {"expected_version": self.material["version"], "content": "过期资料"},
            "artifact": {"expected_version": self.artifact["version"], "title": "过期产物"},
        }
        for name, path in paths.items():
            with self.subTest(name=name, condition="stale"):
                status, body = self.patch(path, stale_payloads[name])
                self.assertEqual(status, 409)
                self.assertFalse(body["ok"])


if __name__ == "__main__":
    unittest.main()
