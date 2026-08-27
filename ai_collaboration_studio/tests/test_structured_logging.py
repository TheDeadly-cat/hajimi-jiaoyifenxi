from __future__ import annotations

import io
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

from backend import http_server
from backend import structured_logging
from backend.store import StudioStore


class StructuredLoggingUnitTests(unittest.TestCase):
    def test_jsonl_event_recursively_redacts_sensitive_values(self) -> None:
        output = io.StringIO()
        secret = "sk-proj-" + ("A" * 40)
        database_path = "C:\\Users\\Operator\\formal.sqlite3"
        payload = structured_logging.emit_event(
            "unit_test_event",
            fields={
                "authorization": "Bearer private-value",
                "database_path": database_path,
                "nested": {
                    "api_key": secret,
                    "provider_response": {"content": "private payload"},
                    "safe": "bounded metadata",
                },
                "untrusted_url": "https://user:password@example.invalid/path?q=secret",
            },
            stream=output,
        )

        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        decoded = json.loads(lines[0])
        self.assertEqual(decoded, payload)
        self.assertEqual(decoded["schema_version"], "studio_log_event_v1")
        self.assertEqual(decoded["fields"]["authorization"], "[REDACTED]")
        self.assertEqual(decoded["fields"]["database_path"], "[REDACTED]")
        self.assertEqual(decoded["fields"]["nested"]["api_key"], "[REDACTED]")
        self.assertEqual(
            decoded["fields"]["nested"]["provider_response"],
            "[REDACTED]",
        )
        self.assertEqual(decoded["fields"]["untrusted_url"], "[REDACTED_URL]")
        serialized = json.dumps(decoded, ensure_ascii=False)
        for forbidden in (secret, database_path, "private-value", "private payload"):
            self.assertNotIn(forbidden, serialized)

    def test_request_target_keeps_only_a_bounded_route_class(self) -> None:
        target = "/api/rooms/private-room/rounds/private-round?token=secret#fragment"
        self.assertEqual(
            structured_logging.classify_request_target(target),
            "api:rooms",
        )
        self.assertEqual(
            structured_logging.classify_request_target("/assets/app.js?q=secret"),
            "frontend:asset",
        )
        self.assertEqual(
            structured_logging.classify_request_target(
                "/api/integration/manifest?ignored=secret"
            ),
            "api:integration",
        )


class StructuredHttpLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-structured-http-"
        )
        self.original_store = http_server.STORE
        http_server.STORE = StudioStore(
            Path(self.temp_dir.name) / "structured-http.sqlite3"
        )
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            http_server.StudioRequestHandler,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        http_server.STORE = self.original_store
        self.temp_dir.cleanup()

    def test_http_log_omits_query_ids_headers_and_body(self) -> None:
        target = (
            f"http://127.0.0.1:{self.server.server_port}"
            "/api/unknown/private-id?api_key=private-value"
        )
        with patch.object(http_server, "emit_event") as emit:
            with self.assertRaises(HTTPError) as caught:
                self.opener.open(target, timeout=5)
            caught.exception.close()

        request_calls = [
            call
            for call in emit.call_args_list
            if call.args and call.args[0] == "http_request_completed"
        ]
        self.assertEqual(len(request_calls), 1)
        fields = request_calls[0].kwargs["fields"]
        self.assertEqual(
            fields,
            {"method": "GET", "path_class": "api:other", "status": 404},
        )
        serialized = json.dumps(fields)
        for forbidden in ("private-id", "api_key", "private-value", "headers", "body"):
            self.assertNotIn(forbidden, serialized)

    def test_run_server_emits_only_bounded_lifecycle_metadata(self) -> None:
        database_path = Path(self.temp_dir.name) / "lifecycle.sqlite3"
        store = SimpleNamespace(
            configured_path=database_path,
            path=database_path,
            recover_orphaned_work=lambda **kwargs: {
                "recovered_chat_targets": 2,
                "paused_rounds": 1,
                "cancelled_rounds": 0,
            },
        )
        fake_server = Mock()
        fake_server.server_port = 43123
        owner = Mock(spec=http_server.DatabaseInstanceOwner)

        with (
            patch.object(http_server, "STORE", store),
            patch.object(http_server, "ThreadingHTTPServer", return_value=fake_server),
            patch.object(http_server, "emit_event") as emit,
        ):
            http_server.run_server(
                host="127.0.0.1",
                port=0,
                instance_owner=owner,
            )

        events = [call.args[0] for call in emit.call_args_list]
        self.assertEqual(
            events,
            ["server_started", "server_state_recovered", "server_stopped"],
        )
        serialized = repr(emit.call_args_list)
        self.assertNotIn(str(database_path), serialized)
        self.assertNotIn("http://", serialized)


if __name__ == "__main__":
    unittest.main()
