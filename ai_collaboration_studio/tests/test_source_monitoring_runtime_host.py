from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import http_server  # noqa: E402
from backend.source_inbox_service import SourceInboxService  # noqa: E402
from backend.source_monitoring.adapters.base import (  # noqa: E402
    SOURCE_ADAPTER_CONTRACT_VERSION,
)
from backend.source_monitoring.contracts import AdapterPollResult  # noqa: E402
from backend.source_monitoring.registry import SourceAdapterRegistry  # noqa: E402
from backend.source_monitoring.runtime import SourceMonitoringRuntime  # noqa: E402
from backend.source_monitoring.scheduler import (  # noqa: E402
    SourceMonitoringScheduler,
)
from backend.source_monitoring.settings import SourceMonitoringSettings  # noqa: E402
from backend.source_monitoring.state_repository import (  # noqa: E402
    SourceMonitoringStateRepository,
)
from backend.source_monitoring.supervisor import (  # noqa: E402
    SourceMonitoringSupervisor,
)
from backend.store import StudioStore  # noqa: E402


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

    def test_requested_runtime_start_failure_keeps_core_host_serving(self) -> None:
        events: list[str] = []
        database_path = Path("fixture-runtime-start-degraded.sqlite3")
        owner = Mock(spec=http_server.DatabaseInstanceOwner)
        store = SimpleNamespace(
            configured_path=database_path,
            path=database_path,
            recover_orphaned_work=lambda **kwargs: {},
        )

        class FakeServer:
            server_port = 41734

            def serve_forever(self) -> None:
                self.served_while_ready = self.ai_studio_startup_ready
                events.append("serve")

            def server_close(self) -> None:
                events.append("close")

        class FailedRuntime:
            settings = SimpleNamespace(enabled=True, auto_start=True)

            def start(self) -> bool:
                events.append("start_failed")
                return False

            def snapshot(self) -> dict[str, object]:
                return {
                    "status": "failed",
                    "thread_alive": False,
                    "liveness_verified": False,
                    "last_fatal_error_code": (
                        "SOURCE_MONITORING_RUNTIME_INITIALIZE_FAILED"
                    ),
                }

            def stop(self) -> bool:
                events.append("stop")
                return True

        fake_server = FakeServer()
        runtime = FailedRuntime()
        with (
            patch.object(http_server, "STORE", store),
            patch.object(
                http_server,
                "ThreadingHTTPServer",
                return_value=fake_server,
            ),
            patch.object(http_server, "emit_event") as emit,
        ):
            http_server.run_server(
                host="127.0.0.1",
                port=0,
                instance_owner=owner,
                runtime_factory=lambda supplied_store: runtime,
            )

        self.assertTrue(fake_server.served_while_ready)
        self.assertEqual(
            events,
            ["start_failed", "serve", "close", "stop"],
        )
        degraded = next(
            call
            for call in emit.call_args_list
            if call.args == ("source_monitoring_runtime_start_degraded",)
        )
        self.assertEqual(degraded.kwargs["severity"], "error")
        self.assertEqual(
            degraded.kwargs["fields"]["error_code"],
            "SOURCE_MONITORING_RUNTIME_INITIALIZE_FAILED",
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
            join_timeout_ms = 10

            def __init__(self) -> None:
                self.stop_calls = 0

            def start(self) -> bool:
                events.append("start")
                return True

            def request_stop(self) -> None:
                events.append("request_stop")

            def stop(self) -> bool:
                case.assertIn("close", events)
                self.stop_calls += 1
                events.append(
                    "stop_timeout" if self.stop_calls == 1 else "stop_complete"
                )
                return self.stop_calls > 1

            def wait_until_stopped(self, timeout: float) -> bool:
                case.assertEqual(timeout, 0.01)
                events.append("wait_until_stopped")
                return True

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
                "request_stop",
                "close",
                "stop_timeout",
                "request_stop",
                "wait_until_stopped",
                "stop_complete",
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

    def test_second_bounded_stop_failure_raises_fail_stop_signal(self) -> None:
        events: list[str] = []
        database_path = Path("fixture-runtime-fail-stop.sqlite3")
        owner = Mock(spec=http_server.DatabaseInstanceOwner)
        store = SimpleNamespace(
            configured_path=database_path,
            path=database_path,
            recover_orphaned_work=lambda **kwargs: {},
        )

        class FakeServer:
            server_port = 41733

            def serve_forever(self) -> None:
                events.append("serve")

            def server_close(self) -> None:
                events.append("close")

        class StuckRuntime:
            join_timeout_ms = 10

            def start(self) -> bool:
                return True

            def request_stop(self) -> None:
                events.append("request_stop")

            def stop(self) -> bool:
                events.append("stop")
                return False

            def wait_until_stopped(self, timeout: float) -> bool:
                self.timeout = timeout
                events.append("bounded_wait")
                return False

        runtime = StuckRuntime()
        with (
            patch.object(http_server, "STORE", store),
            patch.object(
                http_server,
                "ThreadingHTTPServer",
                return_value=FakeServer(),
            ),
            patch.object(http_server, "emit_event"),
        ):
            with self.assertRaises(http_server.RuntimeShutdownIncomplete):
                http_server.run_server(
                    host="127.0.0.1",
                    port=0,
                    instance_owner=owner,
                    runtime_factory=lambda supplied_store: runtime,
                )

        self.assertEqual(runtime.timeout, 0.01)
        self.assertEqual(
            events,
            [
                "serve",
                "request_stop",
                "close",
                "stop",
                "request_stop",
                "bounded_wait",
            ],
        )

    def test_locked_adapter_stop_does_not_block_host_fail_stop(self) -> None:
        case = self
        poll_entered = threading.Event()
        poll_release = threading.Event()
        stop_attempted = threading.Event()
        resource_lock = threading.Lock()
        request_stop_elapsed: list[float] = []

        class LockedAdapter:
            contract_version = SOURCE_ADAPTER_CONTRACT_VERSION
            adapter_key = "fixture_host_locked_adapter"
            config_version = "fixture_host_locked_adapter_config_v1"
            poll_interval_ms = 60_000
            max_candidates_per_poll = 1
            official_source = True
            execution_capability = "none"
            live_trading_allowed = False

            def poll(
                self,
                checkpoint: dict[str, Any],
                *,
                observed_at_ms: int,
                deadline_monotonic_ms: int = 0,
                cancel_event: threading.Event | None = None,
                etag: str = "",
                last_modified: str = "",
                max_items: int = 50,
            ) -> AdapterPollResult:
                del deadline_monotonic_ms, cancel_event
                del etag, last_modified, max_items
                with resource_lock:
                    poll_entered.set()
                    if not poll_release.wait(2):
                        raise RuntimeError("fixture poll release timed out")
                    return AdapterPollResult.build(
                        adapter_key=self.adapter_key,
                        started_checkpoint=checkpoint,
                        next_checkpoint={"cursor": 1},
                        observed_items=(),
                        captured_at_ms=observed_at_ms,
                    )

            def stop(self) -> bool:
                stop_attempted.set()
                with resource_lock:
                    return True

        with tempfile.TemporaryDirectory(
            prefix="ai-studio-host-fail-stop-"
        ) as temp_dir:
            store = StudioStore(Path(temp_dir) / "studio.sqlite3")
            repository = SourceMonitoringStateRepository(store)
            registry = SourceAdapterRegistry((LockedAdapter(),))
            settings = SourceMonitoringSettings(
                enabled=True,
                auto_start=True,
                official_only=True,
                dry_run=False,
            )
            supervisor = SourceMonitoringSupervisor(
                registry=registry,
                repository=repository,
                source_inbox=SourceInboxService(store),
                settings=settings,
            )
            scheduler = SourceMonitoringScheduler(
                registry=registry,
                repository=repository,
                supervisor=supervisor,
            )
            runtime = SourceMonitoringRuntime(
                scheduler=scheduler,
                settings=settings,
                heartbeat_interval_ms=1,
                join_timeout_ms=500,
                poll_timeout_ms=500,
            )
            repository.set_enabled(
                "fixture_host_locked_adapter",
                config_version="fixture_host_locked_adapter_config_v1",
                enabled=True,
            )
            owner = Mock(spec=http_server.DatabaseInstanceOwner)

            class FakeServer:
                server_port = 41736

                def serve_forever(self) -> None:
                    case.assertTrue(poll_entered.wait(2))
                    started = time.monotonic()
                    runtime.request_stop()
                    request_stop_elapsed.append(time.monotonic() - started)

                def server_close(self) -> None:
                    return None

            try:
                with (
                    patch.object(http_server, "STORE", store),
                    patch.object(
                        http_server,
                        "ThreadingHTTPServer",
                        return_value=FakeServer(),
                    ),
                    patch.object(http_server, "emit_event"),
                ):
                    with self.assertRaises(
                        http_server.RuntimeShutdownIncomplete
                    ):
                        http_server.run_server(
                            host="127.0.0.1",
                            port=0,
                            instance_owner=owner,
                            runtime_factory=lambda supplied_store: runtime,
                        )

                self.assertEqual(len(request_stop_elapsed), 1)
                self.assertLess(request_stop_elapsed[0], 0.25)
                self.assertFalse(stop_attempted.is_set())
                self.assertTrue(runtime.snapshot()["thread_alive"])
            finally:
                poll_release.set()
                runtime.wait_until_stopped(2)
                runtime.stop()

            self.assertTrue(stop_attempted.is_set())

    def test_shutdown_callback_exceptions_also_raise_fail_stop_signal(self) -> None:
        events: list[str] = []
        database_path = Path("fixture-runtime-shutdown-exception.sqlite3")
        owner = Mock(spec=http_server.DatabaseInstanceOwner)
        store = SimpleNamespace(
            configured_path=database_path,
            path=database_path,
            recover_orphaned_work=lambda **kwargs: {},
        )

        class FakeServer:
            server_port = 41735

            def serve_forever(self) -> None:
                events.append("serve")

            def server_close(self) -> None:
                events.append("close")

        class RaisingRuntime:
            join_timeout_ms = 10

            def start(self) -> bool:
                return True

            def request_stop(self) -> None:
                events.append("request_stop_error")
                raise RuntimeError("fixed request-stop failure")

            def stop(self) -> bool:
                events.append("stop_error")
                raise RuntimeError("fixed stop failure")

            def wait_until_stopped(self, timeout: float) -> bool:
                self.timeout = timeout
                events.append("bounded_wait_error")
                raise RuntimeError("fixed join failure")

        runtime = RaisingRuntime()
        with (
            patch.object(http_server, "STORE", store),
            patch.object(
                http_server,
                "ThreadingHTTPServer",
                return_value=FakeServer(),
            ),
            patch.object(http_server, "emit_event"),
        ):
            with self.assertRaises(http_server.RuntimeShutdownIncomplete):
                http_server.run_server(
                    host="127.0.0.1",
                    port=0,
                    instance_owner=owner,
                    runtime_factory=lambda supplied_store: runtime,
                )

        self.assertEqual(runtime.timeout, 0.01)
        self.assertEqual(
            events,
            [
                "serve",
                "request_stop_error",
                "close",
                "stop_error",
                "request_stop_error",
                "bounded_wait_error",
            ],
        )

    def test_health_route_uses_only_the_attached_runtime_snapshot(self) -> None:
        case = self
        runtime_snapshot = {
            "version": "source_monitoring_runtime_health_v1",
            "status": "running",
        }
        runtime_settings = object()
        runtime = SimpleNamespace(
            settings=runtime_settings,
            snapshot=Mock(return_value=runtime_snapshot),
        )
        handler = object.__new__(http_server.StudioRequestHandler)
        handler.server = SimpleNamespace(
            ai_studio_source_monitoring_runtime=runtime,
        )
        handler.path = "/api/monitoring/health"
        response = {"version": "fixture-health"}
        captured: dict[str, object] = {}

        class FakeHealthService:
            def __init__(
                self, store: object, *, settings: object, runtime_snapshot: object,
            ) -> None:
                captured["store"] = store
                captured["settings"] = settings
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
        self.assertIs(captured["settings"], runtime.settings)
        self.assertIs(captured["runtime_snapshot"], runtime.snapshot)
        self.assertIs(captured["runtime_value"], runtime_snapshot)
        runtime.snapshot.assert_called_once_with()
        send_json.assert_called_once_with(
            {"ok": True, "source_monitoring_health": response},
        )

    def test_dual_runtime_operator_uses_registry_catalog_and_shared_repository(self) -> None:
        database_path = Path("fixture-dual-operator.sqlite3")
        fake_store = SimpleNamespace(path=database_path)
        owner = Mock(spec=http_server.DatabaseInstanceOwner)
        shutdown_event = threading.Event()
        settings = object()
        registry_catalog = (object(), object())
        repository = object()
        runtime = SimpleNamespace(
            settings=settings,
            registry_catalog=registry_catalog,
            repository=repository,
        )
        handler = object.__new__(http_server.StudioRequestHandler)
        handler.server = SimpleNamespace(
            ai_studio_instance_owner=owner,
            ai_studio_source_monitoring_runtime=runtime,
            ai_studio_shutdown_event=shutdown_event,
        )

        with (
            patch.object(http_server, "STORE", fake_store),
            patch.object(http_server, "SourceMonitoringOperatorService") as service,
        ):
            result = handler._source_monitoring_operator_service()

        self.assertIs(result, service.return_value)
        owner.assert_held_for.assert_called_once_with(database_path)
        service.assert_called_once_with(
            store=fake_store,
            settings=settings,
            registry_catalog=registry_catalog,
            repository=repository,
            cancel_event=shutdown_event,
        )


if __name__ == "__main__":
    unittest.main()
