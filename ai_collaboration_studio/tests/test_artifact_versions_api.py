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


def create_versioned_artifact(store: StudioStore) -> tuple[dict, dict]:
    version_one = store.create_artifact(
        "room_plan",
        title="方案纪要 v1",
        content={
            "summary": "先验证需求，再确定实施范围。",
            "summary_evidence": [],
            "requirements": [{
                "id": "requirement_history_case",
                "text": "保留人工确认入口。",
                "status": "pending",
                "owner": "产品负责人",
                "acceptance_criteria": "能够查看第一版要求。",
                "evidence": [],
            }],
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
        },
        generation_source="manual",
        created_by="test_user",
    )
    if version_one is None:
        raise AssertionError("room_plan should exist in the seeded temporary store")
    version_two = store.update_artifact(
        "room_plan",
        version_one["id"],
        {
            "expected_version": version_one["version"],
            "title": "方案纪要 v2",
            "content": {
                **version_one["content"],
                "summary": "需求已澄清，继续保留人工确认入口。",
                "requirements": [{
                    **version_one["content"]["requirements"][0],
                    "status": "confirmed",
                    "acceptance_criteria": "第一版和第二版均可精确读取并比较。",
                }],
            },
        },
    )
    if version_two is None:
        raise AssertionError("the seeded artifact should be updateable")
    return version_one, version_two


class ArtifactVersionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "artifact-versions.sqlite3"
        self.store = StudioStore(self.db_path)
        self.version_one, self.version_two = create_versioned_artifact(self.store)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_list_is_newest_first_and_detail_returns_exact_immutable_snapshots(self) -> None:
        result = self.store.list_artifact_versions("room_plan", self.version_one["id"])

        self.assertIsNotNone(result)
        self.assertEqual(result["artifact"]["id"], self.version_one["id"])
        self.assertEqual(result["artifact"]["current_version"], 2)
        self.assertEqual([item["version"] for item in result["versions"]], [2, 1])
        self.assertTrue(all(item["integrity_ok"] for item in result["versions"]))
        self.assertNotIn("snapshot", result["versions"][0])

        first = self.store.get_artifact_version("room_plan", self.version_one["id"], 1)
        second = self.store.get_artifact_version("room_plan", self.version_one["id"], 2)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first["artifact"]["current_version"], 2)
        self.assertEqual(second["artifact"]["current_version"], 2)
        first_version = first["artifact_version"]
        second_version = second["artifact_version"]
        self.assertEqual(first_version["version"], 1)
        self.assertEqual(second_version["version"], 2)
        self.assertTrue(first_version["integrity_ok"])
        self.assertTrue(second_version["integrity_ok"])
        self.assertEqual(first_version["snapshot"]["title"], "方案纪要 v1")
        self.assertEqual(second_version["snapshot"]["title"], "方案纪要 v2")
        self.assertEqual(
            first_version["snapshot"]["content"]["summary"],
            "先验证需求，再确定实施范围。",
        )
        self.assertEqual(
            second_version["snapshot"]["content"]["summary"],
            "需求已澄清，继续保留人工确认入口。",
        )
        self.assertEqual(
            first_version["snapshot"]["content"]["requirements"][0]["acceptance_criteria"],
            "能够查看第一版要求。",
        )
        self.assertEqual(
            second_version["snapshot"]["content"]["requirements"][0]["acceptance_criteria"],
            "第一版和第二版均可精确读取并比较。",
        )
        self.assertEqual(
            first_version["snapshot"]["content"]["requirements"][0]["id"],
            "requirement_history_case",
        )
        self.assertEqual(
            second_version["snapshot"]["content"]["requirements"][0]["id"],
            "requirement_history_case",
        )
        self.assertRegex(first_version["snapshot_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(first_version["binding_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(first_version["snapshot_sha256"], second_version["snapshot_sha256"])

    def test_missing_or_cross_room_store_reads_return_none(self) -> None:
        self.assertIsNone(self.store.list_artifact_versions("room_storage", self.version_one["id"]))
        self.assertIsNone(
            self.store.get_artifact_version("room_plan", self.version_one["id"], 99),
        )

    def test_corrupt_snapshot_remains_visible_in_list_with_failed_integrity(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """UPDATE artifact_versions SET snapshot_json=?
                   WHERE artifact_id=? AND version=1""",
                ('{"incomplete":', self.version_one["id"]),
            )

        result = self.store.list_artifact_versions("room_plan", self.version_one["id"])
        by_version = {item["version"]: item for item in result["versions"]}

        self.assertEqual(set(by_version), {1, 2})
        self.assertFalse(by_version[1]["integrity_ok"])
        self.assertIn("ARTIFACT_VERSION_SNAPSHOT_INVALID", by_version[1]["integrity_issues"])
        self.assertTrue(by_version[2]["integrity_ok"])


class ArtifactVersionHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "artifact-versions-http.sqlite3"
        self.store = StudioStore(self.db_path)
        self.version_one, self.version_two = create_versioned_artifact(self.store)
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
        artifact_id = self.version_one["id"]
        with closing(sqlite3.connect(self.db_path)) as monitor:
            monitor.execute("PRAGMA query_only=ON")
            before_data_version = monitor.execute("PRAGMA data_version").fetchone()[0]
            before_rows = monitor.execute(
                """SELECT version,snapshot_json,changed_at FROM artifact_versions
                   WHERE artifact_id=? ORDER BY version""",
                (artifact_id,),
            ).fetchall()

            list_status, listed = self.get_json(
                f"/api/rooms/room_plan/artifacts/{artifact_id}/versions",
            )
            detail_status, detail = self.get_json(
                f"/api/rooms/room_plan/artifacts/{artifact_id}/versions/1",
            )

            after_data_version = monitor.execute("PRAGMA data_version").fetchone()[0]
            after_rows = monitor.execute(
                """SELECT version,snapshot_json,changed_at FROM artifact_versions
                   WHERE artifact_id=? ORDER BY version""",
                (artifact_id,),
            ).fetchall()

        self.assertEqual(list_status, 200)
        self.assertTrue(listed["ok"])
        self.assertEqual([item["version"] for item in listed["versions"]], [2, 1])
        self.assertEqual(detail_status, 200)
        self.assertTrue(detail["ok"])
        self.assertEqual(detail["artifact_version"]["version"], 1)
        self.assertEqual(
            detail["artifact_version"]["snapshot"]["content"]["summary"],
            "先验证需求，再确定实施范围。",
        )
        self.assertEqual(after_data_version, before_data_version)
        self.assertEqual(after_rows, before_rows)

    def test_http_missing_artifact_cross_room_and_missing_version_return_404(self) -> None:
        artifact_id = self.version_one["id"]
        cases = [
            "/api/rooms/room_plan/artifacts/artifact_missing/versions",
            f"/api/rooms/room_storage/artifacts/{artifact_id}/versions",
            f"/api/rooms/room_plan/artifacts/{artifact_id}/versions/99",
        ]

        for path in cases:
            with self.subTest(path=path):
                status, payload = self.get_json(path)
                self.assertEqual(status, 404)
                self.assertFalse(payload["ok"])

    def test_http_corrupt_detail_fails_closed_but_list_exposes_integrity_failure(self) -> None:
        artifact_id = self.version_one["id"]
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """UPDATE artifact_versions SET snapshot_json=?
                   WHERE artifact_id=? AND version=1""",
                ('{"incomplete":', artifact_id),
            )

        list_status, listed = self.get_json(
            f"/api/rooms/room_plan/artifacts/{artifact_id}/versions",
        )
        detail_status, detail = self.get_json(
            f"/api/rooms/room_plan/artifacts/{artifact_id}/versions/1",
        )
        by_version = {item["version"]: item for item in listed["versions"]}

        self.assertEqual(list_status, 200)
        self.assertFalse(by_version[1]["integrity_ok"])
        self.assertEqual(detail_status, 409)
        self.assertFalse(detail["ok"])
        self.assertEqual(detail["error_code"], "ARTIFACT_VERSION_CORRUPT")
        self.assertNotIn("snapshot", detail)


if __name__ == "__main__":
    unittest.main()
