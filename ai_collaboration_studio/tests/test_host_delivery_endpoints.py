from __future__ import annotations

import hashlib
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

from backend import http_server
from backend.store import StudioStore


class HostDeliveryEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-host-delivery-"
        )
        self.temp_path = Path(self.temp_dir.name)
        self.database_path = self.temp_path / "host-delivery.sqlite3"
        self.store = StudioStore(self.database_path)
        self.frontend_dist = self.temp_path / "dist"
        self.frontend_dist.mkdir()
        self.index_body = b"<!doctype html><title>host delivery fixture</title>"
        self.index_path = self.frontend_dist / "index.html"
        self.index_path.write_bytes(self.index_body)

        self.original_store = http_server.STORE
        self.original_frontend_dist = http_server.FRONTEND_DIST
        http_server.STORE = self.store
        http_server.FRONTEND_DIST = self.frontend_dist

        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            http_server.StudioRequestHandler,
        )
        self.server.ai_studio_startup_ready = True
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        http_server.STORE = self.original_store
        http_server.FRONTEND_DIST = self.original_frontend_dist
        self.temp_dir.cleanup()

    def get_json(self, path: str) -> tuple[int, dict[str, str], dict]:
        try:
            with self.opener.open(f"{self.base_url}{path}", timeout=5) as response:
                status = response.status
                headers = dict(response.headers.items())
                body = response.read()
        except HTTPError as exc:
            status = exc.code
            headers = dict(exc.headers.items())
            body = exc.read()
        self.assertIn("application/json", headers.get("Content-Type", ""))
        return status, headers, json.loads(body.decode("utf-8"))

    def test_readiness_is_json_and_proves_all_local_startup_checks(self) -> None:
        with patch.object(
            http_server.PROVIDERS,
            "status",
            side_effect=AssertionError("readiness must not inspect providers"),
        ):
            status, headers, payload = self.get_json("/api/readiness")

        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["schema_version"], "host_readiness_v1")
        self.assertEqual(payload["service"]["id"], "ai_collaboration_studio")
        self.assertTrue(payload["checks"]["startup_gate"]["ready"])
        self.assertTrue(payload["checks"]["database"]["ready"])
        frontend = payload["checks"]["frontend_build"]
        self.assertTrue(frontend["ready"])
        self.assertEqual(frontend["index_bytes"], len(self.index_body))
        self.assertEqual(
            frontend["index_sha256"],
            hashlib.sha256(self.index_body).hexdigest(),
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(str(self.database_path), serialized)
        self.assertNotIn("session_token", serialized)

    def test_readiness_fails_closed_for_startup_or_frontend_gap(self) -> None:
        self.server.ai_studio_startup_ready = False
        status, _, payload = self.get_json("/api/readiness")
        self.assertEqual(status, 503)
        self.assertFalse(payload["ready"])
        self.assertFalse(payload["checks"]["startup_gate"]["ready"])
        self.assertFalse(payload["checks"]["database"]["ready"])

        self.server.ai_studio_startup_ready = True
        self.index_path.unlink()
        status, _, payload = self.get_json("/api/readiness")
        self.assertEqual(status, 503)
        self.assertFalse(payload["ready"])
        self.assertTrue(payload["checks"]["database"]["ready"])
        self.assertFalse(payload["checks"]["frontend_build"]["ready"])

    def test_version_matches_package_and_identifies_frontend_build(self) -> None:
        with patch.object(
            http_server.PROVIDERS,
            "status",
            side_effect=AssertionError("version must not inspect providers"),
        ):
            status, headers, payload = self.get_json("/api/version")

        package = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "frontend"
                / "package.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["schema_version"], "host_version_v1")
        self.assertEqual(payload["service"]["version"], package["version"])
        self.assertEqual(
            payload["api"],
            {
                "contract_version": "host_delivery_v1",
                "readiness_schema_version": "host_readiness_v1",
                "version_schema_version": "host_version_v1",
            },
        )
        self.assertEqual(
            payload["frontend_build"],
            {
                "available": True,
                "index_bytes": len(self.index_body),
                "index_sha256": hashlib.sha256(self.index_body).hexdigest(),
            },
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(str(self.temp_path), serialized)
        self.assertNotIn("session_token", serialized)

    def test_host_delivery_queries_are_rejected(self) -> None:
        for endpoint in ("/api/readiness", "/api/version"):
            with self.subTest(endpoint=endpoint):
                status, _, payload = self.get_json(f"{endpoint}?verbose=1")
                self.assertEqual(status, 400)
                self.assertFalse(payload["ok"])
                self.assertEqual(
                    payload["error_code"],
                    "HOST_ENDPOINT_QUERY_UNSUPPORTED",
                )

    def test_unknown_api_get_never_falls_through_to_spa_html(self) -> None:
        status, _, payload = self.get_json("/api/not-a-real-endpoint")
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "API_NOT_FOUND")


class LauncherDeliveryContractTests(unittest.TestCase):
    def test_launcher_gates_on_versioned_readiness_not_liveness(self) -> None:
        launcher = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "start_ai_collaboration_studio.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("/api/readiness", launcher)
        self.assertIn('"host_readiness_v1"', launcher)
        self.assertIn('"ai_collaboration_studio"', launcher)
        self.assertIn(
            'runtime\\bootstrap\\python\\Scripts\\python.exe',
            launcher,
        )
        self.assertNotIn("/api/health", launcher)
        self.assertNotIn("Test-StudioHealth", launcher)


if __name__ == "__main__":
    unittest.main()
