from __future__ import annotations

import sys
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import http_server  # noqa: E402


class SourceMonitoringRuntimeHostTests(unittest.TestCase):
    def test_non_daemon_block_on_close_really_drains_active_handler(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class BlockingHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                entered.set()
                release.wait(2)
                self.send_response(204)
                self.end_headers()

            def log_message(self, _format: str, *args: object) -> None:
                del args

        server = ThreadingHTTPServer(("127.0.0.1", 0), BlockingHandler)
        server.daemon_threads = False
        server.block_on_close = True
        serving = threading.Thread(target=server.serve_forever, daemon=False)
        request_finished = threading.Event()

        def request() -> None:
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            try:
                connection.request("GET", "/")
                connection.getresponse().read()
            finally:
                connection.close()
                request_finished.set()

        serving.start()
        client = threading.Thread(target=request, daemon=False)
        client.start()
        try:
            self.assertTrue(entered.wait(2))
            server.shutdown()
            serving.join(2)
            self.assertFalse(serving.is_alive())

            closed = threading.Event()

            def close_server() -> None:
                server.server_close()
                closed.set()

            closing = threading.Thread(target=close_server, daemon=False)
            closing.start()
            time.sleep(0.05)
            self.assertFalse(closed.is_set())
            release.set()
            closing.join(2)
            client.join(2)
            self.assertTrue(closed.is_set())
            self.assertTrue(request_finished.is_set())
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            serving.join(2)
            client.join(2)

    def test_runtime_starts_after_bind_and_recovery_then_stops_after_drain(self) -> None:
        events: list[str] = []
        database_path = Path("fixture-runtime-host.sqlite3")
        case = self

        class FakeOwner:
            def assert_held_for(self, path: object) -> None:
                self.asserted_path = path

        class FakeStore:
            configured_path = database_path
            path = database_path

            @staticmethod
            def recover_orphaned_work(*, instance_owner: object) -> dict[str, int]:
                case.assertIs(instance_owner, owner)
                events.append("recover")
                return {}

        class FakeServer:
            server_port = 41731

            def __init__(self) -> None:
                self.ai_studio_startup_ready = None
                self.ai_studio_instance_owner = None
                self.ai_studio_source_monitoring_runtime = None

            def serve_forever(self) -> None:
                case.assertTrue(self.ai_studio_startup_ready)
                case.assertIs(self.ai_studio_instance_owner, owner)
                case.assertIs(self.daemon_threads, False)
                case.assertIs(self.block_on_close, True)
                events.append("serve")

            def server_close(self) -> None:
                case.assertFalse(self.ai_studio_startup_ready)
                events.append("close")

        class FakeRuntime:
            def start(self) -> bool:
                case.assertIs(
                    fake_server.ai_studio_source_monitoring_runtime,
                    self,
                )
                events.append("start")
                return True

            def stop(self) -> bool:
                events.append("stop")
                return True

        owner = FakeOwner()
        fake_server = FakeServer()
        runtime = FakeRuntime()

        def make_server(address: object, handler: object) -> FakeServer:
            self.assertEqual(address, ("127.0.0.1", 0))
            self.assertIs(handler, http_server.StudioRequestHandler)
            events.append("bind")
            return fake_server

        def make_runtime(store: object) -> FakeRuntime:
            self.assertIs(store, fake_store)
            events.append("factory")
            return runtime

        fake_store = FakeStore()
        with (
            patch.object(http_server, "STORE", fake_store),
            patch.object(http_server, "ThreadingHTTPServer", side_effect=make_server),
            patch.object(http_server, "emit_event"),
        ):
            http_server.run_server(
                host="127.0.0.1",
                port=0,
                instance_owner=owner,
                runtime_factory=make_runtime,
            )

        self.assertEqual(
            events,
            ["bind", "recover", "factory", "start", "serve", "close", "stop"],
        )

    def test_stop_timeout_retains_control_until_runtime_has_stopped(self) -> None:
        events: list[str] = []
        database_path = Path("fixture-runtime-timeout.sqlite3")
        case = self
        owner = Mock(spec=http_server.DatabaseInstanceOwner)
        store = SimpleNamespace(
            configured_path=database_path,
            path=database_path,
            recover_orphaned_work=lambda **kwargs: events.append("recover") or {},
        )

        class FakeServer:
            server_port = 41732

            def serve_forever(self) -> None:
                events.append("serve")

            def server_close(self) -> None:
                case.assertFalse(self.ai_studio_startup_ready)
                events.append("close")

        class SlowRuntime:
            def start(self) -> bool:
                events.append("start")
                return True

            def stop(self) -> bool:
                case.assertIn("close", events)
                events.append("stop_timeout")
                return False

            def wait_until_stopped(self) -> None:
                events.append("wait_until_stopped")

        fake_server = FakeServer()
        runtime = SlowRuntime()
        with (
            patch.object(http_server, "STORE", store),
            patch.object(http_server, "ThreadingHTTPServer", return_value=fake_server),
            patch.object(http_server, "emit_event") as emit,
        ):
            http_server.run_server(
                host="127.0.0.1",
                port=0,
                instance_owner=owner,
                runtime_factory=lambda supplied_store: runtime,
            )

        self.assertEqual(
            events,
            [
                "recover",
                "start",
                "serve",
                "close",
                "stop_timeout",
                "wait_until_stopped",
            ],
        )
        timeout_event = next(
            call
            for call in emit.call_args_list
            if call.args == ("source_monitoring_runtime_stop_timeout",)
        )
        self.assertEqual(timeout_event.kwargs["severity"], "critical")
        self.assertEqual(
            timeout_event.kwargs["fields"],
            {
                "database_owner_retained": True,
                "execution_capability": "none",
                "live_trading_allowed": False,
            },
        )

    def test_health_route_uses_only_the_attached_runtime_snapshot(self) -> None:
        case = self
        runtime_snapshot = {
            "version": "source_monitoring_runtime_health_v1",
            "status": "running",
        }
        runtime = SimpleNamespace(snapshot=Mock(return_value=runtime_snapshot))
        handler = object.__new__(http_server.StudioRequestHandler)
        handler.server = SimpleNamespace(
            ai_studio_source_monitoring_runtime=runtime,
        )
        handler.path = "/api/monitoring/health"
        response = {"version": "fixture-health"}
        captured: dict[str, object] = {}

        class FakeHealthService:
            def __init__(self, store: object, *, runtime_snapshot: object) -> None:
                captured["store"] = store
                captured["runtime_snapshot"] = runtime_snapshot

            def snapshot(self) -> dict[str, str]:
                callback = captured["runtime_snapshot"]
                case.assertTrue(callable(callback))
                captured["runtime_value"] = callback()
                return response

        with (
            patch.object(handler, "_guard_request", return_value=True),
            patch.object(handler, "_send_json") as send_json,
            patch.object(http_server, "SourceMonitoringHealthService", FakeHealthService),
        ):
            handler.do_GET()

        self.assertIs(captured["store"], http_server.STORE)
        self.assertIs(captured["runtime_value"], runtime_snapshot)
        runtime.snapshot.assert_called_once_with()
        send_json.assert_called_once_with(
            {"ok": True, "source_monitoring_health": response},
        )


if __name__ == "__main__":
    unittest.main()
