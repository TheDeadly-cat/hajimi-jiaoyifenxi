"""Offline composition: real SEC/IR parsers, worker, SQLite, HTTP and drafts.

Only source transports are fixtures. No fixture is evidence of an online release.
Run through scripts/run_backend_tests_isolated.py for the socket audit.
"""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from backend import http_server
from backend.market.ir_releases import OfficialIrReleaseAdapter
from backend.market.sec_edgar import SecEdgarAdapter
from backend.providers.compatible_chat_provider import CompatibleChatProvider
from backend.providers.deepseek_provider import DeepSeekProvider
from backend.providers.doubao_provider import DoubaoProvider
from backend.providers.openai_provider import OpenAIProvider
from backend.source_inbox_service import SourceInboxService
from backend.source_monitoring.adapters.company_ir import CompanyIrSourceAdapter
from backend.source_monitoring.adapters.sec_filings import SecFilingsSourceAdapter
from backend.source_monitoring.registry import SourceAdapterRegistry
from backend.source_monitoring.runtime import SourceMonitoringRuntime
from backend.source_monitoring.scheduler import SourceMonitoringScheduler
from backend.source_monitoring.settings import SourceMonitoringSettings
from backend.source_monitoring.state_repository import SourceMonitoringStateRepository
from backend.source_monitoring.supervisor import SourceMonitoringSupervisor
from backend.store import StudioStore
from tests import test_source_inbox_http as http_helpers
from tests.test_source_monitoring_micron_json import MicronJsonFixtureTransport
from tests.test_source_monitoring_official_adapters import MutableIrFixtureFetcher
from tests.test_source_monitoring_sec_baseline import MutableSecRecentFetcher, NOW_MS


class OfficialDeliveryCompositionTests(unittest.TestCase):
    request = http_helpers.SourceInboxHttpTests.request
    request_with_headers = http_helpers.SourceInboxHttpTests.request_with_headers

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="studio-official-delivery-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "isolated.sqlite3"
        self.store = StudioStore(self.path)
        self.room = self.store.create_room(
            "离线官方消息复核", "人工选择材料和研究草稿。", capability_pack_ids=[],
        )["room"]
        self.clock = NOW_MS
        self.sec = MutableSecRecentFetcher(count=13)
        self.ir = MutableIrFixtureFetcher()
        self.micron = None
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.provider_spies = [
            self.stack.enter_context(patch.object(cls, method, side_effect=AssertionError("Provider forbidden")))
            for cls, method in (
                (CompatibleChatProvider, "generate"), (CompatibleChatProvider, "probe"),
                (OpenAIProvider, "generate"), (OpenAIProvider, "probe"),
                (DoubaoProvider, "generate"), (DoubaoProvider, "generate_json"),
                (DoubaoProvider, "probe"), (DeepSeekProvider, "generate_json"),
            )
        ]
        self.stack.enter_context(patch.object(http_server, "STORE", self.store))
        self.stack.enter_context(patch.object(
            http_server, "SourceInboxService",
            side_effect=lambda store: SourceInboxService(store, clock=lambda: self.clock / 1_000),
        ))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), http_server.StudioRequestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def close_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def counts(self) -> dict[str, int]:
        with closing(sqlite3.connect(self.path)) as connection:
            return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("provider_execution_runs", "provider_call_attempts", "rounds",
                                  "source_inbox_items", "source_inbox_round_drafts", "materials")}

    def build_runtime(self, *, enable: bool = False, after_import_hook=None):
        # Fresh source, registry, repository and worker model an actual restart;
        # only the same temporary persisted database and external fixtures survive.
        clock = lambda: datetime.fromtimestamp(self.clock / 1_000, tz=timezone.utc)
        ir_adapter = (
            OfficialIrReleaseAdapter(source_format="rss", fetch_bytes=self.ir, clock=clock)
            if self.micron is None else
            OfficialIrReleaseAdapter(source_format="q4_json", micron_fetch_bytes=self.micron, clock=clock)
        )
        adapters = (
            SecFilingsSourceAdapter(adapter=SecEdgarAdapter(
                user_agent="Offline fixture fixture@example.com", fetch_json=self.sec,
                clock=clock, allowed_symbols=["US.NVDA"],
            ), allowed_symbols=["US.NVDA"], allowed_forms=["8-K"], per_symbol_limit=3, force=True),
            CompanyIrSourceAdapter(adapter=ir_adapter, symbols=["US.MU"], force=True,
                                   receipt_clock=clock),
        )
        store = StudioStore(self.path)
        repository = SourceMonitoringStateRepository(store, clock_ms=lambda: self.clock)
        registry = SourceAdapterRegistry(adapters)
        settings = SourceMonitoringSettings(enabled=True, auto_start=True, dry_run=False)
        supervisor = SourceMonitoringSupervisor(
            registry=registry, repository=repository,
            source_inbox=SourceInboxService(store, clock=lambda: self.clock / 1_000),
            settings=settings, clock_ms=lambda: self.clock,
            event_sink=lambda *_args, **_kwargs: None, after_import_hook=after_import_hook,
        )
        if enable:
            for adapter in adapters:
                repository.set_enabled(adapter.adapter_key, config_version=adapter.config_version, enabled=True)
        scheduler = SourceMonitoringScheduler(registry=registry, repository=repository,
                                             supervisor=supervisor, clock_ms=lambda: self.clock)
        observations = []
        def observe(receipt):
            observations.append(receipt)
            if len(observations) == 2:
                runtime.request_stop()
        runtime = SourceMonitoringRuntime(scheduler=scheduler, settings=settings,
                                          clock_ms=lambda: self.clock, cycle_observer=observe,
                                          heartbeat_interval_ms=1, join_timeout_ms=2_000)
        return runtime, repository, observations

    def poll_both(self, *, enable=False, after_import_hook=None):
        runtime, repository, observations = self.build_runtime(enable=enable, after_import_hook=after_import_hook)
        try:
            runtime.start()
            self.assertTrue(runtime.wait_until_stopped(20), "two source cycles did not finish")
            self.assertEqual(len(observations), 2)
            self.assertEqual({row["adapter_key"] for row in observations}, {"sec_filings", "company_ir"})
        finally:
            self.assertTrue(runtime.stop())
        self.clock += 30 * 60 * 1_000
        return repository, observations

    def test_official_event_restart_notification_and_user_draft_have_zero_model_calls(self) -> None:
        self.assert_official_event_restart_notification_and_user_draft()

    def test_micron_json_event_restart_notification_and_user_draft_have_zero_model_calls(self) -> None:
        self.micron = MicronJsonFixtureTransport()
        self.assert_official_event_restart_notification_and_user_draft()

    def assert_official_event_restart_notification_and_user_draft(self) -> None:
        before = self.counts()
        repository, seeded = self.poll_both(enable=True)
        self.assertTrue(all(row["status"] == "SUCCEEDED" for row in seeded), seeded)
        self.assertEqual(len(repository.get_state("sec_filings")["checkpoint"]["seen_accessions"]), 13)
        if self.micron is not None:
            self.assertEqual(len(repository.get_state("company_ir")["checkpoint"]["projections"]), 30)
        self.assertEqual(self.counts(), before)
        status, baseline = self.request("/api/monitoring/notifications")
        self.assertEqual(status, 200)
        cursor = baseline["source_notifications"]["cursor"]
        self.sec.records.append((14, "2026-09-04T20:00:00Z"))  # Same-time, new ID.
        if self.micron is None:
            self.ir.guid = "mu-post-baseline-release"
        else:
            self.micron.records = self.micron.records[1:] + [self.micron.record(31)]
        repository, observed = self.poll_both()
        self.assertTrue(all(row["status"] == "SUCCEEDED" for row in observed), observed)
        status, listing = self.request("/api/monitoring/inbox")
        self.assertEqual(status, 200)
        items = listing["source_inbox"]["items"]
        self.assertEqual(len(items), 2)
        if self.micron is not None:
            json_items = [row["item"] for row in items if "company_ir_v2" in row["item"]["extensions"]]
            self.assertEqual(len(json_items), 1)
            self.assertEqual(json_items[0]["extensions"]["company_ir_v2"]["press_release_id"], 31)
            self.assertEqual([source["source_type"] for source in json_items[0]["sources"]],
                             ["company_ir_time_metadata", "company_ir_json_projection"])
        status, notifications = self.request(f"/api/monitoring/notifications?after={cursor}")
        self.assertEqual(status, 200)
        feed = notifications["source_notifications"]
        self.assertEqual({row["id"] for row in feed["notifications"]}, {item["id"] for item in items})
        cursor = feed["cursor"]
        for item in items:
            for action in ("acknowledge", "attach", "round-draft"):
                payload = ({"acknowledgement": True} if action == "acknowledge" else {"room_id": self.room["id"]})
                payload["expected_state_version"] = item["state_version"]
                denied_before = self.counts()
                status, _ = self.request(f"/api/monitoring/events/{item['id']}/{action}", method="POST",
                                         payload=payload, include_token=False)
                self.assertEqual(status, 403)
                self.assertEqual(self.counts(), denied_before)
                status, result = self.request(f"/api/monitoring/events/{item['id']}/{action}", method="POST", payload=payload)
                self.assertEqual(status, 200 if action == "acknowledge" else 201, result)
                item = result["source_item"] if action == "acknowledge" else result["item"]
            self.assertEqual(item["state"], "ROUND_DRAFTED")
        drafted = self.counts()
        with closing(sqlite3.connect(self.path)) as connection:
            materials = connection.execute("SELECT id,content FROM materials ORDER BY id").fetchall()
        self.assertEqual(drafted["materials"] - before["materials"], 2)
        self.assertEqual(drafted["source_inbox_round_drafts"] - before["source_inbox_round_drafts"], 2)
        checkpoints = {key: repository.get_state(key)["checkpoint"] for key in ("sec_filings", "company_ir")}
        restarted, repeated = self.poll_both()  # persisted checkpoint, fresh worker, repeated source bodies
        self.assertTrue(all(row["status"] == "SUCCEEDED" for row in repeated), repeated)
        self.assertEqual({key: restarted.get_state(key)["checkpoint"] for key in checkpoints}, checkpoints)
        self.assertEqual(self.counts(), drafted)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT id,content FROM materials ORDER BY id").fetchall(), materials)
        _, replay = self.request(f"/api/monitoring/notifications?after={cursor}")
        self.assertEqual(replay["source_notifications"]["notifications"], [])
        self.assertGreater(len(self.sec.calls), 0)
        if self.micron is None:
            self.assertGreater(self.ir.calls, 0)
        else:
            self.assertEqual(sum(not head_only for _url, head_only in self.micron.calls), 3)
            self.assertEqual(sum(head_only for _url, head_only in self.micron.calls), 90)
        for spy in self.provider_spies:
            spy.assert_not_called()
        for table in ("provider_execution_runs", "provider_call_attempts", "rounds"):
            self.assertEqual(drafted[table], before[table])
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertGreater(connection.execute(
                "SELECT SUM(duplicate_count) FROM source_adapter_runs"
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT SUM(formal_round_created),SUM(provider_calls_performed) FROM source_inbox_round_drafts"
            ).fetchone(), (0, 0))

    def test_import_failure_rolls_back_both_checkpoint_and_inbox_then_replays_once(self) -> None:
        before = self.counts()
        repository, _ = self.poll_both(enable=True)
        checkpoints = {key: repository.get_state(key)["checkpoint"] for key in ("sec_filings", "company_ir")}
        self.sec.records.append((14, "2026-09-03T20:00:00Z"))  # Late, genuinely new ID.
        self.ir.guid = "mu-late-release"
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("""CREATE TRIGGER fail_official_delivery_insert
                AFTER INSERT ON source_inbox_items
                BEGIN SELECT RAISE(ABORT,'injected import transaction failure'); END""")
        repository, failed = self.poll_both()
        self.assertTrue(all(row["status"] == "FAILED" for row in failed), failed)
        self.assertEqual(self.counts()["source_inbox_items"], 0)
        self.assertEqual({key: repository.get_state(key)["checkpoint"] for key in checkpoints}, checkpoints)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_inbox_imports").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_inbox_state_events").fetchone()[0], 0)
            connection.execute("DROP TRIGGER fail_official_delivery_insert")
        _repo, recovered = self.poll_both()
        self.assertTrue(all(row["status"] == "SUCCEEDED" for row in recovered), recovered)
        self.assertEqual(self.counts()["source_inbox_items"], 2)
        self.poll_both()
        self.assertEqual(self.counts()["source_inbox_items"], 2)
        for spy in self.provider_spies:
            spy.assert_not_called()
        for table in ("provider_execution_runs", "provider_call_attempts", "rounds"):
            self.assertEqual(self.counts()[table], before[table])
