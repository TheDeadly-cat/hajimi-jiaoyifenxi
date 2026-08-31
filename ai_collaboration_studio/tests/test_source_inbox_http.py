from __future__ import annotations

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
from urllib.request import Request, urlopen

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend import http_server  # noqa: E402
from backend.store import StudioStore  # noqa: E402
from tests.test_source_inbox_contracts import _packet  # noqa: E402


class SourceInboxHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="ai-studio-source-inbox-http-")
        self.store = StudioStore(Path(self.temp_dir.name) / "studio.sqlite3")
        self.room = self.store.create_room(
            "HTTP 来源复核",
            "只读复核公共 CI 事件。",
            capability_pack_ids=[],
        )["room"]
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

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict | None = None,
        include_token: bool = True,
    ) -> tuple[int, dict]:
        status, response, _headers = self.request_with_headers(
            path,
            method=method,
            payload=payload,
            include_token=include_token,
        )
        return status, response

    def request_with_headers(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict | None = None,
        raw_body: str | None = None,
        include_token: bool = True,
    ) -> tuple[int, dict, dict[str, str]]:
        headers = {}
        data = None
        if raw_body is not None:
            headers["Content-Type"] = "application/json"
            data = raw_body.encode("utf-8")
        elif payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if include_token:
            headers["X-AI-Studio-Token"] = http_server.LOCAL_SESSION_TOKEN
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=3) as response:
                return (
                    response.status,
                    json.loads(response.read().decode("utf-8")),
                    dict(response.headers.items()),
                )
        except HTTPError as exc:
            try:
                return (
                    exc.code,
                    json.loads(exc.read().decode("utf-8")),
                    dict(exc.headers.items()),
                )
            finally:
                exc.close()

    def import_item(self) -> dict:
        status, payload = self.request(
            "/api/monitoring/imports/chatgpt",
            method="POST",
            payload={"content": json.dumps(_packet(), ensure_ascii=False)},
        )
        self.assertEqual(status, 201)
        return payload["source_import"]["items"][0]

    def test_import_list_detail_and_replay_routes(self) -> None:
        empty_status, empty = self.request("/api/monitoring/inbox")
        self.assertEqual(empty_status, 200)
        self.assertEqual(empty["source_inbox"]["items"], [])

        item = self.import_item()
        replay_status, replay = self.request(
            "/api/monitoring/imports/chatgpt",
            method="POST",
            payload={"content": json.dumps(_packet(), ensure_ascii=False)},
        )
        self.assertEqual(replay_status, 200)
        self.assertTrue(replay["source_import"]["idempotent_replay"])

        list_status, listing = self.request(
            "/api/monitoring/inbox?state=AWAITING_USER&limit=10"
        )
        self.assertEqual(list_status, 200)
        self.assertEqual([entry["id"] for entry in listing["source_inbox"]["items"]], [item["id"]])

        detail_status, detail = self.request(f"/api/monitoring/events/{item['id']}")
        self.assertEqual(detail_status, 200)
        self.assertEqual(detail["source_item"]["events"][-1]["to_state"], "AWAITING_USER")

    def test_user_sequence_creates_material_and_draft_but_no_formal_round(self) -> None:
        item = self.import_item()
        ack_status, ack = self.request(
            f"/api/monitoring/events/{item['id']}/acknowledge",
            method="POST",
            payload={
                "expected_state_version": item["state_version"],
                "acknowledgement": True,
            },
        )
        self.assertEqual(ack_status, 200)
        acknowledged = ack["source_item"]
        self.assertTrue(acknowledged["acknowledged"])

        attach_status, attach = self.request(
            f"/api/monitoring/events/{item['id']}/attach",
            method="POST",
            payload={
                "room_id": self.room["id"],
                "expected_state_version": acknowledged["state_version"],
            },
        )
        self.assertEqual(attach_status, 201)
        self.assertEqual(attach["item"]["state"], "ATTACHED")

        draft_status, draft = self.request(
            f"/api/monitoring/events/{item['id']}/round-draft",
            method="POST",
            payload={
                "room_id": self.room["id"],
                "expected_state_version": attach["item"]["state_version"],
            },
        )
        self.assertEqual(draft_status, 201)
        self.assertFalse(draft["round_draft"]["formal_round_created"])
        self.assertEqual(draft["round_draft"]["provider_calls_performed"], 0)
        self.assertEqual(draft["round_draft"]["market_calls_performed"], 0)

        with closing(sqlite3.connect(self.store.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM materials").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM rounds").fetchone()[0], 0)

    def test_invalid_packet_and_missing_ui_token_fail_without_writes(self) -> None:
        invalid_status, invalid = self.request(
            "/api/monitoring/imports/chatgpt",
            method="POST",
            payload={"content": '{"version":"x","version":"y"}'},
        )
        self.assertEqual(invalid_status, 400)
        self.assertEqual(invalid["code"], "SOURCE_IMPORT_DUPLICATE_KEY")

        denied_status, _ = self.request(
            "/api/monitoring/imports/chatgpt",
            method="POST",
            payload={"content": json.dumps(_packet(), ensure_ascii=False)},
            include_token=False,
        )
        self.assertEqual(denied_status, 403)

        reserved = _packet()
        reserved["source_channel"] = "official_source_monitor"
        reserved["source_key"] = "sec_filings"
        reserved["external_run_id"] = "manual-reserved-channel-forgery"
        reserved["generation"]["channel"] = "official_source_monitor"
        reserved["generation"]["correlated_output"] = False
        reserved_status, reserved_payload = self.request(
            "/api/monitoring/imports/chatgpt",
            method="POST",
            payload={"content": json.dumps(reserved, ensure_ascii=False)},
        )
        self.assertEqual(reserved_status, 403)
        self.assertEqual(
            reserved_payload["code"],
            "SOURCE_INBOX_MONITORING_CHANNEL_UNAUTHORIZED",
        )

        with closing(sqlite3.connect(self.store.path)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM source_inbox_imports").fetchone()[0],
                0,
            )

    def test_attach_requires_acknowledgement_and_exact_fields(self) -> None:
        item = self.import_item()
        status, payload = self.request(
            f"/api/monitoring/events/{item['id']}/attach",
            method="POST",
            payload={
                "room_id": self.room["id"],
                "expected_state_version": item["state_version"],
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "SOURCE_INBOX_ACKNOWLEDGEMENT_REQUIRED")

        extra_status, extra = self.request(
            f"/api/monitoring/events/{item['id']}/acknowledge",
            method="POST",
            payload={
                "expected_state_version": item["state_version"],
                "acknowledgement": True,
                "fact_confirmed": True,
            },
        )
        self.assertEqual(extra_status, 400)
        self.assertEqual(extra["code"], "SOURCE_INBOX_REQUEST_INVALID")

    def test_outer_json_duplicate_and_nonfinite_values_fail_closed_without_writes(self) -> None:
        valid_content = json.dumps(_packet(), ensure_ascii=False)
        cases = (
            '{"content":"first","content":"second"}',
            '{"content":NaN}',
            '{"content":Infinity}',
            '{"content":"x","nested":{"value":1,"value":2}}',
        )
        for raw_body in cases:
            with self.subTest(raw_body=raw_body):
                status, _payload, headers = self.request_with_headers(
                    "/api/monitoring/imports/chatgpt",
                    method="POST",
                    raw_body=raw_body,
                )
                self.assertEqual(status, 400)
                self.assertEqual(headers.get("Cache-Control"), "no-store")
        status, payload, _headers = self.request_with_headers(
            "/api/monitoring/imports/chatgpt",
            method="POST",
            raw_body=json.dumps({"content": valid_content}, ensure_ascii=False),
        )
        self.assertEqual(status, 201)
        self.assertTrue(payload["ok"])
        with closing(sqlite3.connect(self.store.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_inbox_imports").fetchone()[0], 1)

    def test_round_draft_objective_requires_exact_native_string(self) -> None:
        item = self.import_item()
        ack_status, ack = self.request(
            f"/api/monitoring/events/{item['id']}/acknowledge",
            method="POST",
            payload={
                "expected_state_version": item["state_version"],
                "acknowledgement": True,
            },
        )
        self.assertEqual(ack_status, 200)
        attach_status, attached = self.request(
            f"/api/monitoring/events/{item['id']}/attach",
            method="POST",
            payload={
                "room_id": self.room["id"],
                "expected_state_version": ack["source_item"]["state_version"],
            },
        )
        self.assertEqual(attach_status, 201)
        for objective in (None, True, 7, [], {}):
            with self.subTest(objective=objective):
                status, payload = self.request(
                    f"/api/monitoring/events/{item['id']}/round-draft",
                    method="POST",
                    payload={
                        "room_id": self.room["id"],
                        "expected_state_version": attached["item"]["state_version"],
                        "objective": objective,
                    },
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["code"], "SOURCE_INBOX_REQUEST_INVALID")
        with closing(sqlite3.connect(self.store.path)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM source_inbox_round_drafts").fetchone()[0],
                0,
            )
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM rounds").fetchone()[0], 0)

    def test_all_source_inbox_responses_are_no_store(self) -> None:
        for path, method, payload, include_token, expected_status in (
            ("/api/monitoring/inbox", "GET", None, True, 200),
            ("/api/monitoring/inbox?limit=1&limit=2", "GET", None, True, 400),
            ("/api/monitoring/events/missing", "GET", None, True, 404),
            (
                "/api/monitoring/imports/chatgpt",
                "POST",
                {"content": json.dumps(_packet(), ensure_ascii=False)},
                False,
                403,
            ),
        ):
            with self.subTest(path=path, expected_status=expected_status):
                status, _response, headers = self.request_with_headers(
                    path,
                    method=method,
                    payload=payload,
                    include_token=include_token,
                )
                self.assertEqual(status, expected_status)
                self.assertEqual(headers.get("Cache-Control"), "no-store")


if __name__ == "__main__":
    unittest.main()
