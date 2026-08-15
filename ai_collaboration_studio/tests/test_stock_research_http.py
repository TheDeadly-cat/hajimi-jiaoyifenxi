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
from backend.stock_research_service import StockResearchError
from backend.store import StudioStore


class RecordingStockResearchService:
    calls: list[tuple[str, object]] = []
    error: StockResearchError | None = None

    def __init__(self, store: StudioStore) -> None:
        self.store = store

    def inspect(self, room_id: str, payload: object) -> dict[str, object]:
        type(self).calls.append((room_id, payload))
        if type(self).error is not None:
            raise type(self).error
        return {
            "version": "stock_research_view_model_v1",
            "integrity_ok": True,
            "room_id": room_id,
            "contract": payload,
        }


class StockResearchHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-stock-http-test-"
        )
        self.addCleanup(self.temp_dir.cleanup)
        self.store = StudioStore(Path(self.temp_dir.name) / "studio.sqlite3")
        RecordingStockResearchService.calls = []
        RecordingStockResearchService.error = None
        self.store_patch = mock.patch.object(http_server, "STORE", self.store)
        self.service_patch = mock.patch.object(
            http_server,
            "StockResearchService",
            RecordingStockResearchService,
        )
        self.store_patch.start()
        self.service_patch.start()
        self.addCleanup(self.store_patch.stop)
        self.addCleanup(self.service_patch.stop)
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            http_server.StudioRequestHandler,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def _post(self, body: dict[str, object]) -> tuple[int, dict[str, object]]:
        request = Request(
            self.base_url + "/api/rooms/room_fixture/stock-research/inspect",
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

    def test_readonly_route_accepts_only_closed_payload_without_session_token(self) -> None:
        contract_payload = {
            "stock_room_scope": {
                "version": "stock_room_scope_v1",
                "symbols": ["US:AAPL"],
            }
        }
        status, response = self._post({"payload": contract_payload})

        self.assertEqual(status, 200, response)
        self.assertEqual(
            RecordingStockResearchService.calls,
            [("room_fixture", contract_payload)],
        )
        self.assertEqual(
            response["stock_research"]["version"],
            "stock_research_view_model_v1",
        )

        status, response = self._post({"payload": {}, "unexpected": True})
        self.assertEqual(status, 400)
        self.assertEqual(response["code"], "STOCK_RESEARCH_REQUEST_INVALID")
        self.assertEqual(len(RecordingStockResearchService.calls), 1)

    def test_typed_service_error_fails_closed(self) -> None:
        RecordingStockResearchService.error = StockResearchError(
            "stock material binding drifted",
            code="STOCK_RESEARCH_MATERIAL_DRIFT",
            status=409,
        )
        status, response = self._post({"payload": {}})
        self.assertEqual(status, 409)
        self.assertEqual(response["code"], "STOCK_RESEARCH_MATERIAL_DRIFT")

    def test_room_creation_forwards_the_explicit_canonical_stock_pool(self) -> None:
        scope = {
            "version": "stock_room_scope_v1",
            "symbols": ["US:AAPL", "US:MSFT"],
        }
        request = Request(
            self.base_url + "/api/rooms",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
            },
            data=json.dumps({
                "title": "Explicit stock pool",
                "objective": "Research the sealed pool only.",
                "template_id": "stock_research",
                "capability_pack_ids": ["stock_research_readonly"],
                "stock_room_scope": scope,
            }).encode("utf-8"),
        )
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 201)

        self.assertEqual(payload["room"]["stock_room_scope"], scope)
        self.assertTrue(payload["room"]["stock_room_scope_integrity_ok"])
        self.assertEqual(
            payload["room"]["capability_pack_ids"],
            ["stock_research_readonly"],
        )
        self.assertEqual(
            payload["room"]["active_capability_pack_ids"],
            ["stock_research_readonly"],
        )
        self.assertEqual(
            {
                item["id"]
                for item in payload["room"]["plugin_registry_snapshot"][
                    "capability_packs"
                ]
            },
            {
                "stock_research_readonly",
                "structured_project_research",
                "structured_turn_contract_v1",
            },
        )
        contribution_ids = {
            item["contribution_id"]
            for item in payload["room"]["plugin_registry_snapshot"][
                "ui_contributions"
            ]
        }
        self.assertIn("stock_research.room_inspector/v1", contribution_ids)
        self.assertIn("project_research.artifact_workspace/v1", contribution_ids)


if __name__ == "__main__":
    unittest.main()
