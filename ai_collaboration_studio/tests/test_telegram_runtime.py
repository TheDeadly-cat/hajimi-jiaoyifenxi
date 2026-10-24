from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import backend.telegram_intelligence.runtime as runtime_module
from backend.telegram_intelligence.config import ChatPolicy, TelegramCollectorConfig
from backend.telegram_intelligence.health import HealthState
from backend.telegram_intelligence.models import SourceMessage
from backend.telegram_intelligence.pipeline import PipelineService
from backend.telegram_intelligence.runtime import (
    RuntimeDependencies,
    run_history,
    run_service,
)
from backend.telegram_intelligence.repository import TelegramRepository
from backend.telegram_intelligence.settings import TelegramSettings


def source_message(message_id: int = 1) -> SourceMessage:
    timestamp = datetime(2026, 8, 29, 5, 0, tzinfo=timezone.utc)
    return SourceMessage(
        source="telegram",
        chat_id=-100123,
        chat_title="Fixture source",
        chat_type="channel",
        message_id=message_id,
        sender_id=7,
        sender_name="Fixture",
        timestamp=timestamp,
        edit_timestamp=None,
        text="NVIDIA GPU fixture",
        caption="",
        urls=(),
        entities=(),
        reply_to_message_id=None,
        forward_origin=None,
        media_type="",
        media_file_id="",
        telegram_link="https://t.me/fixture/1",
        raw_language="en",
        is_edited=False,
        is_forwarded=False,
        ingested_at=timestamp,
    )


class FakeClient:
    def __init__(self) -> None:
        self.is_running = False
        self.is_ready = False
        self.authorization_state = "authorizationStateWaitTdlibParameters"
        self.connection_state = "connectionStateConnecting"
        self.closed = False
        self.list_chats_calls = 0

    def start(self) -> None:
        self.is_running = True
        self.is_ready = True
        self.authorization_state = "authorizationStateReady"
        self.connection_state = "connectionStateReady"

    def wait_ready(self, timeout: float) -> bool:
        return self.is_ready

    def close(self) -> None:
        self.closed = True
        self.is_running = False
        self.is_ready = False

    def list_chats(self, *args, **kwargs):
        self.list_chats_calls += 1
        raise AssertionError("history must not enumerate account chats")


class CapturingHealth(HealthState):
    def __init__(self) -> None:
        super().__init__()
        self.ready_seen = threading.Event()
        self.updates: list[tuple[str, str, str]] = []

    def update_tdlib(self, client) -> None:
        super().update_tdlib(client)
        row = (
            "ready" if client.is_ready else "running" if client.is_running else "stopped",
            client.authorization_state,
            client.connection_state,
        )
        self.updates.append(row)
        if row == (
            "ready",
            "authorizationStateReady",
            "connectionStateReady",
        ):
            self.ready_seen.set()


class FakeHealthServer:
    def __init__(self, state, *, host: str, port: int) -> None:
        self.state = state
        self.host = host
        self.port = port
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True


class FalsyDisabledRegistry:
    disabled_provider_ids = frozenset({"openai"})

    def __init__(self) -> None:
        self.get_calls: list[str] = []

    def __bool__(self) -> bool:
        return False

    def get(self, provider_id: str):
        self.get_calls.append(provider_id)
        return None

    def resolved_model(self, provider_id: str, model: str = "") -> str:
        raise AssertionError("disabled provider must not resolve a model")


class EnabledRegistry:
    disabled_provider_ids = frozenset()

    class Provider:
        provider_id = "openai"

    def get(self, provider_id: str):
        return self.Provider()

    def resolved_model(self, provider_id: str, model: str = "") -> str:
        return "fixture-model"


@dataclass(frozen=True)
class FakeHistoryResult:
    chat_id: int
    requested_limit: int

    def as_dict(self):
        return {
            "chat_id": self.chat_id,
            "requested_limit": self.requested_limit,
            "fetched_count": 1,
            "processed_count": 1,
        }


class TelegramRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.library = root / "tdjson.dll"
        self.library.write_bytes(b"fixture")
        self.settings = TelegramSettings(
            api_id=12345,
            api_hash="fixture-api-hash",
            phone="+10000000000",
            tdlib_library_path=self.library,
            tdlib_database_dir=root / "tdlib-data",
            tdlib_files_dir=root / "tdlib-files",
            bot_token="1234567890:fixture_token",
            destination_chat_id="-100999",
            chats_config_path=root / "chats.yaml",
            keywords_config_path=root / "keywords.yaml",
            database_path=root / "telegram-runtime.sqlite3",
            health_host="127.0.0.1",
            health_port=8781,
            history_max_limit=1000,
            pre_score_light_threshold=30,
            pre_score_full_threshold=60,
            alert_cooldown_seconds=300,
            analyzer_provider="openai",
            enable_ai=True,
            enable_alerts=False,
            dry_run=True,
        )
        self.chat_config = TelegramCollectorConfig(
            (
                ChatPolicy(
                    chat_id=-100123,
                    enabled=True,
                    name="fixture",
                    category="market",
                    priority="high",
                    trust_score=90,
                ),
            )
        )
        self.keyword_mapping = {
            "groups": {
                "AI": {
                    "keywords": ["NVIDIA", "GPU"],
                    "regex": [],
                    "tickers": ["NVDA"],
                }
            },
            "ticker_whitelist": {"NVDA": ["NVIDIA"]},
            "breaking_keywords": ["BREAKING"],
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_service_uses_falsy_injected_registry_and_refreshes_live_health(self) -> None:
        client = FakeClient()
        registry = FalsyDisabledRegistry()
        health_rows: list[CapturingHealth] = []
        servers: list[FakeHealthServer] = []

        def health_factory():
            health = CapturingHealth()
            health_rows.append(health)
            return health

        def server_factory(state, *, host, port):
            server = FakeHealthServer(state, host=host, port=port)
            servers.append(server)
            return server

        def collector_factory(client_arg, config, **kwargs):
            self.assertIs(client_arg, client)

            class Collector:
                def run(self_inner):
                    client.start()
                    self.assertTrue(health_rows[0].ready_seen.wait(2.0))

                def stop(self_inner):
                    pass

            return Collector()

        dependencies = RuntimeDependencies(
            chat_loader=lambda path: self.chat_config,
            keyword_loader=lambda path: self.keyword_mapping,
            client_factory=lambda settings: client,
            collector_factory=collector_factory,
            analyzer_factory=lambda **kwargs: (_ for _ in ()).throw(
                AssertionError("hard-disabled OpenAI must not construct an analyzer")
            ),
            bot_factory=lambda **kwargs: (_ for _ in ()).throw(
                AssertionError("alerts are disabled")
            ),
            health_state_factory=health_factory,
            health_server_factory=server_factory,
            provider_registry=registry,
        )

        result = run_service(self.settings, dependencies=dependencies)

        self.assertEqual(registry.get_calls, ["openai"])
        self.assertTrue(client.closed)
        self.assertTrue(servers[0].started)
        self.assertTrue(servers[0].closed)
        self.assertIn(
            ("ready", "authorizationStateReady", "connectionStateReady"),
            health_rows[0].updates,
        )
        self.assertEqual(result["health"]["ai_status"], "policy_disabled")
        self.assertEqual(result["repository"]["messages"], 0)

    def test_history_is_single_chat_and_never_constructs_or_sends_bot(self) -> None:
        client = FakeClient()
        history_calls: list[dict[str, object]] = []
        alerting_settings = TelegramSettings(
            **{
                field: getattr(self.settings, field)
                for field in self.settings.__dataclass_fields__
                if field not in {"enable_ai", "enable_alerts", "dry_run"}
            },
            enable_ai=False,
            enable_alerts=True,
            dry_run=False,
        )

        def history_sync(client_arg, collector, chat_id, **kwargs):
            history_calls.append({"chat_id": chat_id, **kwargs})
            collector.message_sink(source_message())
            return FakeHistoryResult(chat_id, int(kwargs["limit"]))

        dependencies = RuntimeDependencies(
            chat_loader=lambda path: self.chat_config,
            keyword_loader=lambda path: self.keyword_mapping,
            client_factory=lambda settings: client,
            bot_factory=lambda **kwargs: (_ for _ in ()).throw(
                AssertionError("history must never construct a delivery Bot")
            ),
            history_sync=history_sync,
            provider_registry=FalsyDisabledRegistry(),
        )

        result = run_history(
            alerting_settings,
            chat_id=-100123,
            limit=25,
            since_time=datetime(2026, 8, 28, tzinfo=timezone.utc),
            dependencies=dependencies,
        )

        self.assertTrue(client.closed)
        self.assertEqual(client.list_chats_calls, 0)
        self.assertEqual(history_calls[0]["chat_id"], -100123)
        self.assertEqual(history_calls[0]["limit"], 25)
        self.assertEqual(result["processed_count"], 1)
        self.assertEqual(result["repository"]["messages"], 1)
        self.assertEqual(result["repository"]["jobs"]["COMPLETED"], 1)
        self.assertEqual(result["repository"]["alerts"], {})

    def test_runtime_maps_tdlib_database_encryption_key_without_logging_it(self) -> None:
        encrypted = replace(
            self.settings,
            tdlib_database_encryption_key="fixture-database-encryption-secret",
        )
        transport = object()
        captured = {}

        def client_constructor(transport_arg, td_settings, *, auth):
            captured["transport"] = transport_arg
            captured["settings"] = td_settings
            captured["auth"] = auth
            return object()

        dependencies = RuntimeDependencies(
            transport_factory=lambda path: transport,
        )
        with patch.object(runtime_module, "TDLibClient", side_effect=client_constructor):
            runtime_module._build_client(encrypted, dependencies)
        self.assertIs(captured["transport"], transport)
        self.assertEqual(
            captured["settings"].database_encryption_key,
            "fixture-database-encryption-secret",
        )
        self.assertNotIn("fixture-database-encryption-secret", repr(captured["settings"]))

    def test_empty_history_does_not_recover_or_drain_unrelated_database_rows(self) -> None:
        repository = TelegramRepository(self.settings.database_path)
        repository.store_message(source_message(88))
        client = FakeClient()

        dependencies = RuntimeDependencies(
            chat_loader=lambda path: self.chat_config,
            keyword_loader=lambda path: self.keyword_mapping,
            client_factory=lambda settings: client,
            history_sync=lambda *args, **kwargs: {
                "chat_id": -100123,
                "requested_limit": 25,
                "fetched_count": 0,
                "processed_count": 0,
            },
            provider_registry=FalsyDisabledRegistry(),
        )

        result = run_history(
            self.settings,
            chat_id=-100123,
            limit=25,
            dependencies=dependencies,
        )

        self.assertEqual(result["repository"]["messages"], 1)
        self.assertTrue(all(count == 0 for count in result["repository"]["jobs"].values()))

    def test_analyzer_construction_failure_falls_back_to_local_pipeline(self) -> None:
        client = FakeClient()

        def collector_factory(client_arg, config, **kwargs):
            class Collector:
                def run(self_inner):
                    client.start()

                def stop(self_inner):
                    pass

            return Collector()

        dependencies = RuntimeDependencies(
            chat_loader=lambda path: self.chat_config,
            keyword_loader=lambda path: self.keyword_mapping,
            client_factory=lambda settings: client,
            collector_factory=collector_factory,
            analyzer_factory=lambda **kwargs: (_ for _ in ()).throw(
                RuntimeError("fixture analyzer construction failure")
            ),
            health_server_factory=FakeHealthServer,
            provider_registry=EnabledRegistry(),
        )
        result = run_service(self.settings, dependencies=dependencies)
        self.assertEqual(result["health"]["ai_status"], "unavailable")
        self.assertTrue(client.closed)

    def test_service_freezes_client_and_drains_updates_before_stopping_worker(self) -> None:
        order: list[str] = []

        class OrderedClient(FakeClient):
            def close(self_inner) -> None:
                order.append("client_closed")
                super().close()

        client = OrderedClient()

        class RecordingPipeline(PipelineService):
            def stop(self_inner, *, timeout: float = 5.0) -> bool:
                order.append("pipeline_stopped")
                return super().stop(timeout=timeout)

        def collector_factory(client_arg, config, **kwargs):
            class Collector:
                def run(self_inner):
                    client_arg.start()

                def stop(self_inner):
                    order.append("collector_stopped")

                def drain_pending_updates(self_inner):
                    self.assertTrue(client_arg.closed)
                    order.append("updates_drained")
                    kwargs["message_sink"](source_message(901))
                    return 1

            return Collector()

        dependencies = RuntimeDependencies(
            chat_loader=lambda path: self.chat_config,
            keyword_loader=lambda path: self.keyword_mapping,
            client_factory=lambda settings: client,
            collector_factory=collector_factory,
            pipeline_factory=RecordingPipeline,
            health_server_factory=FakeHealthServer,
            provider_registry=FalsyDisabledRegistry(),
        )

        result = run_service(self.settings, dependencies=dependencies)

        self.assertLess(order.index("client_closed"), order.index("updates_drained"))
        self.assertLess(order.index("updates_drained"), order.index("pipeline_stopped"))
        self.assertEqual(result["drained_updates"], 1)
        self.assertEqual(result["repository"]["messages"], 1)

    def test_service_never_reports_stopped_while_worker_is_alive(self) -> None:
        client = FakeClient()
        release = threading.Event()
        self.addCleanup(release.set)

        class DeadlinePipeline:
            def __init__(self, *args, **kwargs):
                del args, kwargs
                self.message_sink = lambda message: None
                self.delete_sink = lambda event: None
                self._worker = None

            def start_worker(self, *, stop_event=None):
                del stop_event
                self._worker = threading.Thread(
                    target=release.wait,
                    name="fixture-stuck-telegram-worker",
                    daemon=True,
                )
                self._worker.start()
                return self._worker

            def stop(self, *, timeout=5.0):
                del timeout
                return False

        def collector_factory(client_arg, config, **kwargs):
            del config, kwargs

            class Collector:
                def run(self_inner):
                    client_arg.start()

                def stop(self_inner):
                    return None

                def drain_pending_updates(self_inner):
                    return 0

            return Collector()

        dependencies = RuntimeDependencies(
            chat_loader=lambda path: self.chat_config,
            keyword_loader=lambda path: self.keyword_mapping,
            client_factory=lambda settings: client,
            collector_factory=collector_factory,
            pipeline_factory=DeadlinePipeline,
            health_server_factory=FakeHealthServer,
            provider_registry=FalsyDisabledRegistry(),
        )

        with self.assertRaisesRegex(RuntimeError, "graceful shutdown deadline"):
            run_service(self.settings, dependencies=dependencies)


if __name__ == "__main__":
    unittest.main()
