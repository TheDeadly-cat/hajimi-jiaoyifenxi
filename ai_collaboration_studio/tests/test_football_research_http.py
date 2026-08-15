from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend import http_server
from backend.football_research_service import FootballResearchError
from backend.store import StudioStore


class RecordingFootballResearchService:
    calls: list[tuple[str, object]] = []
    error: FootballResearchError | None = None

    def __init__(self, store: StudioStore) -> None:
        self.store = store

    def inspect(self, room_id: str, payload: object) -> dict[str, object]:
        type(self).calls.append((room_id, payload))
        if type(self).error is not None:
            raise type(self).error
        return {
            "version": "football_research_view_model_v1",
            "integrity_ok": True,
            "room_id": room_id,
            "contract": payload,
        }


class FootballResearchHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-football-http-test-"
        )
        self.addCleanup(self.temp_dir.cleanup)
        self.store = StudioStore(Path(self.temp_dir.name) / "studio.sqlite3")
        RecordingFootballResearchService.calls = []
        RecordingFootballResearchService.error = None
        self.store_patch = mock.patch.object(http_server, "STORE", self.store)
        self.service_patch = mock.patch.object(
            http_server,
            "FootballResearchService",
            RecordingFootballResearchService,
        )
        self.store_patch.start()
        self.service_patch.start()
        self.addCleanup(self.store_patch.stop)
        self.addCleanup(self.service_patch.stop)
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            http_server.StudioRequestHandler,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def _post(self, body: dict[str, object]) -> tuple[int, dict[str, object]]:
        request = Request(
            self.base_url + "/api/rooms/room_fixture/football-research/inspect",
            method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps(body).encode("utf-8"),
        )
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def test_readonly_post_routes_closed_payload_without_session_token(self) -> None:
        contract_payload = {"match_identity": {"match_id": "fixture"}}
        status, response = self._post({"payload": contract_payload})

        self.assertEqual(status, 200, response)
        self.assertEqual(
            RecordingFootballResearchService.calls,
            [("room_fixture", contract_payload)],
        )
        self.assertEqual(
            response["football_research"]["version"],
            "football_research_view_model_v1",
        )

    def test_request_shape_and_typed_service_error_fail_closed(self) -> None:
        status, response = self._post({"payload": {}, "unexpected": True})
        self.assertEqual(status, 400)
        self.assertEqual(response["code"], "FOOTBALL_RESEARCH_REQUEST_INVALID")
        self.assertEqual(RecordingFootballResearchService.calls, [])

        RecordingFootballResearchService.error = FootballResearchError(
            "material binding drifted",
            code="FOOTBALL_RESEARCH_MATERIAL_DRIFT",
            status=409,
        )
        status, response = self._post({"payload": {}})
        self.assertEqual(status, 409)
        self.assertEqual(response["code"], "FOOTBALL_RESEARCH_MATERIAL_DRIFT")


if __name__ == "__main__":
    unittest.main()
