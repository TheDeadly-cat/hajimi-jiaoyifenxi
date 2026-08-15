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


class RoundObjectiveStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "objective.sqlite3")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def round_count(self) -> int:
        with closing(sqlite3.connect(self.store.path)) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM rounds").fetchone()[0])

    def test_chinese_objective_round_trips_exactly(self) -> None:
        objective = "比较 MU、SNDK、WDC、STX 的证据质量、反证与风险条件。"

        round_row = self.store.create_round("room_plan", objective)

        self.assertEqual(
            self.store.get_round("room_plan", round_row["id"])["objective"],
            objective,
        )

    def test_corrupted_objective_is_rejected_without_creating_round(self) -> None:
        before = self.round_count()

        with self.assertRaisesRegex(ValueError, "编码损坏"):
            self.store.create_round(
                "room_plan",
                "???????????????????????? MU SNDK WDC STX",
            )

        self.assertEqual(self.round_count(), before)

    def test_paused_round_must_be_resumed_before_starting_a_new_round(self) -> None:
        paused = self.store.create_round("room_plan", "先暂停这一轮")
        self.store.complete_round(paused["id"], "PAUSED")
        before = self.round_count()

        with self.assertRaisesRegex(ValueError, "暂停轮次"):
            self.store.create_round("room_plan", "不应覆盖旧恢复入口")

        self.assertEqual(self.round_count(), before)


class RoundObjectiveHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "objective-http.sqlite3")
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

    def round_count(self) -> int:
        with closing(sqlite3.connect(self.store.path)) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM rounds").fetchone()[0])

    def test_stream_rejects_corruption_before_any_round_is_written(self) -> None:
        before = self.round_count()
        request = Request(
            f"{self.base_url}/api/rooms/room_plan/round-launch-plan",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
            },
            data=json.dumps(
                {
                    "objective": "???????????????????????????? MU SNDK WDC STX",
                    "skip_providers": ["openai"],
                }
            ).encode("utf-8"),
        )

        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=5)
        status = caught.exception.code
        payload = json.loads(caught.exception.read().decode("utf-8"))
        caught.exception.close()

        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "ROUND_LAUNCH_PLAN_INVALID")
        self.assertEqual(self.round_count(), before)

    def test_stream_rejects_new_round_while_a_paused_round_exists(self) -> None:
        paused = self.store.create_round("room_plan", "暂停后应保留恢复入口")
        self.store.complete_round(paused["id"], "PAUSED")
        before = self.round_count()
        request = Request(
            f"{self.base_url}/api/rooms/room_plan/rounds/stream",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
            },
            data=json.dumps(
                {
                    "objective": "不应覆盖暂停轮次",
                    "skip_providers": ["openai"],
                }
            ).encode("utf-8"),
        )

        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=5)
        status = caught.exception.code
        payload = json.loads(caught.exception.read().decode("utf-8"))
        caught.exception.close()

        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "ROUND_AUTHORIZATION_REQUIRED")
        self.assertEqual(self.round_count(), before)


if __name__ == "__main__":
    unittest.main()
