from __future__ import annotations

import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

from backend.source_inbox_service import SourceInboxService
from backend.source_poll_control import ensure_source_poll_active
from backend.source_monitoring.adapters.base import SOURCE_ADAPTER_CONTRACT_VERSION
from backend.source_monitoring.contracts import (
    FUTU_ANOMALY_SOURCE_CHANNEL,
    READONLY_MARKET_SOURCE_CLASS,
    AdapterPollResult,
    canonical_sha256,
)
from backend.source_monitoring.coordinator import (
    OFFICIAL_PIPELINE,
    READONLY_MARKET_PIPELINE,
    SourceMonitoringRuntimeCoordinator,
)
from backend.source_monitoring.registry import SourceAdapterRegistry
from backend.source_monitoring.initialization import build_static_seed_preview
from backend.source_monitoring.runtime import (
    SourceMonitoringRuntime,
    SourceMonitoringRuntimeError,
    build_source_monitoring_runtime,
)
from backend.source_monitoring.scheduler import BackoffPolicy, SourceMonitoringScheduler
from backend.source_monitoring.settings import SourceMonitoringSettings
from backend.source_monitoring.state_repository import (
    SOURCE_MONITORING_PENDING_AUTHORIZATION_VERSION_V2,
    SourceMonitoringStateRepository,
)
from backend.source_monitoring.supervisor import SourceMonitoringSupervisor
from backend.store import StudioStore


class FixtureAdapter:
    contract_version = SOURCE_ADAPTER_CONTRACT_VERSION
    poll_interval_ms = 60_000
    max_candidates_per_poll = 1
    official_source = True
    execution_capability = "none"
    live_trading_allowed = False

    def __init__(
        self,
        adapter_key: str,
        events: list[str],
        concurrency: dict[str, int],
        *,
        fail: bool = False,
        block_until_cancelled: bool = False,
    ) -> None:
        self.adapter_key = adapter_key
        self.config_version = f"{adapter_key}_config_v1"
        self.events = events
        self.concurrency = concurrency
        self.fail = fail
        self.block_until_cancelled = block_until_cancelled
        self.entered = threading.Event()
        self.controls: list[tuple[int, threading.Event | None]] = []

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
        del etag, last_modified, max_items
        self.concurrency["active"] += 1
        self.concurrency["maximum"] = max(
            self.concurrency["maximum"],
            self.concurrency["active"],
        )
        self.events.append(self.adapter_key)
        self.controls.append((deadline_monotonic_ms, cancel_event))
        self.entered.set()
        try:
            if self.block_until_cancelled:
                while True:
                    ensure_source_poll_active(
                        deadline_monotonic_ms=deadline_monotonic_ms,
                        cancel_event=cancel_event,
                    )
                    if cancel_event is not None:
                        cancel_event.wait(0.005)
            if self.fail:
                raise RuntimeError("fixed pipeline failure")
            return AdapterPollResult.build(
                adapter_key=self.adapter_key,
                started_checkpoint=checkpoint,
                next_checkpoint={"cursor": 1},
                observed_items=(),
                captured_at_ms=observed_at_ms,
                market_calls_performed=(0 if self.official_source else 1),
            )
        finally:
            self.concurrency["active"] -= 1


class FixtureMarketAdapter(FixtureAdapter):
    official_source = False
    source_class = READONLY_MARKET_SOURCE_CLASS
    source_channel = FUTU_ANOMALY_SOURCE_CHANNEL
    max_market_calls_per_poll = 1

    def initial_seed_policy(self) -> dict[str, object]:
        manifest: dict[str, object] = {
            "version": "source_monitoring_initial_seed_policy_v1",
            "adapter_key": self.adapter_key,
            "config_version": self.config_version,
            "adapter_config_sha256": canonical_sha256({
                "adapter_key": self.adapter_key,
                "config_version": self.config_version,
            }),
            "broker_policy_sha256": "",
            "initial_mode": "seed_only",
            "symbol_allowlist": ["US.FIXTURE"],
            "execution_capability": "none",
            "live_trading_allowed": False,
        }
        manifest["source_policy_sha256"] = canonical_sha256(manifest)
        return manifest


def wait_until(predicate, *, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


class SourceMonitoringRuntimeCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-runtime-coordinator-"
        )
        self.store = StudioStore(Path(self.temp_dir.name) / "studio.sqlite3")
        self.repository = SourceMonitoringStateRepository(self.store)
        self.source_inbox = SourceInboxService(self.store)
        self.settings = SourceMonitoringSettings(
            enabled=True,
            auto_start=True,
            official_only=True,
            allow_readonly_market=True,
            dry_run=False,
        )
        self.runtimes: list[SourceMonitoringRuntime] = []

    def tearDown(self) -> None:
        for runtime in reversed(self.runtimes):
            runtime.stop()
        self.temp_dir.cleanup()

    def scheduler(
        self,
        adapter: FixtureAdapter,
        *,
        official: bool,
        base_settings: SourceMonitoringSettings | None = None,
    ) -> SourceMonitoringScheduler:
        settings = replace(
            base_settings or self.settings,
            official_only=official,
            allow_readonly_market=not official,
        )
        registry = SourceAdapterRegistry((adapter,), official_only=official)
        supervisor = SourceMonitoringSupervisor(
            registry=registry,
            repository=self.repository,
            source_inbox=self.source_inbox,
            settings=settings,
            backoff_policy=BackoffPolicy(
                initial_delay_ms=30_000,
                maximum_delay_ms=120_000,
                jitter_ratio=0,
                random_source=lambda: 0.5,
            ),
        )
        return SourceMonitoringScheduler(
            registry=registry,
            repository=self.repository,
            supervisor=supervisor,
        )

    def coordinator(
        self,
        official: FixtureAdapter,
        market: FixtureMarketAdapter,
    ) -> SourceMonitoringRuntimeCoordinator:
        official_scheduler = self.scheduler(official, official=True)
        market_scheduler = self.scheduler(market, official=False)
        runtime = SourceMonitoringRuntimeCoordinator(
            pipeline_schedulers=(
                (OFFICIAL_PIPELINE, official_scheduler),
                (READONLY_MARKET_PIPELINE, market_scheduler),
            ),
            settings=self.settings,
            heartbeat_interval_ms=1,
            join_timeout_ms=2_000,
            poll_timeout_ms=1_000,
        )
        self.runtimes.append(runtime)
        return runtime

    def enable(self, *adapters: FixtureAdapter) -> None:
        for adapter in adapters:
            if adapter.official_source:
                self.repository.set_enabled(
                    adapter.adapter_key,
                    config_version=adapter.config_version,
                    enabled=True,
                )
                continue
            market_adapter = adapter
            metadata = SourceAdapterRegistry(
                (market_adapter,),
                official_only=False,
            ).metadata_for(market_adapter.adapter_key)
            seed_policy = market_adapter.initial_seed_policy()
            policy = self.settings.initialization_policy_for(
                official_source=False,
            )
            preview = build_static_seed_preview(
                metadata=metadata,
                initialization_policy=policy,
                initial_seed_policy=seed_policy,
                starting_checkpoint={},
            )
            self.repository.authorize_initialization_and_enable(
                market_adapter.adapter_key,
                config_version=market_adapter.config_version,
                expected_state_version=0,
                authorization={
                    "version": SOURCE_MONITORING_PENDING_AUTHORIZATION_VERSION_V2,
                    "adapter_key": market_adapter.adapter_key,
                    "config_version": market_adapter.config_version,
                    "mode": "seed_only",
                    "catch_up_max_items": 0,
                    "from_time_ms": 0,
                    "starting_checkpoint_sha256": canonical_sha256({}),
                    "preview_sha256": preview["preview_sha256"],
                    "confirmed_at_ms": int(time.time() * 1_000),
                    "authorization_kind": "static_seed_policy",
                    "source_policy_sha256": preview["source_policy_sha256"],
                },
            )

    def test_dual_runtime_runs_globally_serial_and_isolates_pipeline_failure(self) -> None:
        events: list[str] = []
        concurrency = {"active": 0, "maximum": 0}
        official = FixtureAdapter(
            "fixture_official_failure",
            events,
            concurrency,
            fail=True,
        )
        market = FixtureMarketAdapter(
            "fixture_market_healthy",
            events,
            concurrency,
        )
        runtime = self.coordinator(official, market)
        self.enable(official, market)

        self.assertTrue(runtime.start())
        self.assertTrue(wait_until(lambda: len(events) >= 2))
        self.assertEqual(events[:2], [official.adapter_key, market.adapter_key])
        self.assertEqual(concurrency["maximum"], 1)
        self.assertIsNotNone(runtime._thread)
        self.assertFalse(runtime._thread.daemon)
        self.assertIs(runtime.repository, self.repository)
        self.assertEqual(len(runtime.registry_catalog), 2)
        self.assertEqual(runtime.snapshot()["status"], "degraded")
        self.assertGreater(official.controls[0][0], 0)
        self.assertIs(official.controls[0][1], runtime._stop_event)
        self.assertGreater(market.controls[0][0], 0)
        self.assertIs(market.controls[0][1], runtime._stop_event)

    def test_selected_official_state_change_skips_and_market_pipeline_continues(self) -> None:
        events: list[str] = []
        concurrency = {"active": 0, "maximum": 0}
        official = FixtureAdapter("fixture_official_changed", events, concurrency)
        market = FixtureMarketAdapter("fixture_market_continues", events, concurrency)
        runtime = self.coordinator(official, market)
        self.enable(official, market)
        scheduler = runtime.pipeline_schedulers[0][1]
        original = scheduler.run_one_due
        cycles: list[dict[str, Any]] = []

        def change_after_selection(adapter_key, **kwargs):
            if not cycles:
                self.repository.set_enabled(
                    adapter_key, config_version=official.config_version, enabled=False,
                )
                self.repository.set_enabled(
                    adapter_key, config_version=official.config_version, enabled=True,
                )
            cycle = original(adapter_key, **kwargs)
            cycles.append(cycle)
            return cycle

        scheduler.run_one_due = change_after_selection
        self.assertTrue(runtime.start())
        self.assertTrue(wait_until(lambda: len(events) == 2))
        self.assertTrue(runtime.stop())
        self.assertEqual(cycles[0]["run_count"], 0)
        self.assertCountEqual(events, [official.adapter_key, market.adapter_key])
        self.assertEqual(concurrency["maximum"], 1)
        self.assertEqual(runtime.snapshot()["last_fatal_error_code"], "")

    def test_stop_cancels_an_active_poll_and_leaves_no_running_row(self) -> None:
        events: list[str] = []
        concurrency = {"active": 0, "maximum": 0}
        official = FixtureAdapter(
            "fixture_official_blocking",
            events,
            concurrency,
            block_until_cancelled=True,
        )
        market = FixtureMarketAdapter(
            "fixture_market_idle",
            events,
            concurrency,
        )
        runtime = self.coordinator(official, market)
        self.enable(official)

        self.assertTrue(runtime.start())
        self.assertTrue(official.entered.wait(2))
        started = time.monotonic()
        self.assertTrue(runtime.stop())
        self.assertLess(time.monotonic() - started, 1.0)
        runs = self.repository.list_runs(adapter_key=official.adapter_key)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "FAILED")
        self.assertEqual(
            runs[0]["error_code"],
            "SOURCE_MONITORING_POLL_CANCELLED",
        )

    def test_production_builder_uses_coordinator_only_for_dual_mode(self) -> None:
        disabled_dual = replace(
            self.settings,
            enabled=False,
            auto_start=False,
        )
        dual = build_source_monitoring_runtime(
            self.store,
            disabled_dual,
        )
        official_only = build_source_monitoring_runtime(
            self.store,
            replace(
                disabled_dual,
                allow_readonly_market=False,
            ),
        )
        market_only = build_source_monitoring_runtime(
            self.store,
            replace(
                disabled_dual,
                official_only=False,
                allow_readonly_market=True,
            ),
        )

        self.assertIs(type(dual), SourceMonitoringRuntimeCoordinator)
        self.assertIs(type(official_only), SourceMonitoringRuntime)
        self.assertIs(type(market_only), SourceMonitoringRuntime)
        self.assertEqual(len(dual.registry_catalog), 2)
        self.assertEqual(len(official_only.registry_catalog), 1)
        self.assertEqual(len(market_only.registry_catalog), 1)

    def test_coordinator_requires_dual_settings(self) -> None:
        official = FixtureAdapter(
            "fixture_official_dual_gate",
            [],
            {"active": 0, "maximum": 0},
        )
        market = FixtureMarketAdapter(
            "fixture_market_dual_gate",
            [],
            {"active": 0, "maximum": 0},
        )
        pipelines = (
            (OFFICIAL_PIPELINE, self.scheduler(official, official=True)),
            (READONLY_MARKET_PIPELINE, self.scheduler(market, official=False)),
        )

        with self.assertRaises(SourceMonitoringRuntimeError) as raised:
            SourceMonitoringRuntimeCoordinator(
                pipeline_schedulers=pipelines,
                settings=replace(self.settings, allow_readonly_market=False),
            )

        self.assertEqual(
            raised.exception.code,
            "SOURCE_MONITORING_RUNTIME_SETTINGS_MISMATCH",
        )

    def test_coordinator_rejects_swapped_registry_modes(self) -> None:
        official = FixtureAdapter(
            "fixture_official_swapped",
            [],
            {"active": 0, "maximum": 0},
        )
        market = FixtureMarketAdapter(
            "fixture_market_swapped",
            [],
            {"active": 0, "maximum": 0},
        )

        with self.assertRaises(SourceMonitoringRuntimeError) as raised:
            SourceMonitoringRuntimeCoordinator(
                pipeline_schedulers=(
                    (OFFICIAL_PIPELINE, self.scheduler(market, official=False)),
                    (READONLY_MARKET_PIPELINE, self.scheduler(official, official=True)),
                ),
                settings=self.settings,
            )

        self.assertEqual(
            raised.exception.code,
            "SOURCE_MONITORING_RUNTIME_PIPELINES_INVALID",
        )

    def test_coordinator_rejects_each_pipeline_policy_mismatch(self) -> None:
        official = FixtureAdapter(
            "fixture_official_policy",
            [],
            {"active": 0, "maximum": 0},
        )
        market = FixtureMarketAdapter(
            "fixture_market_policy",
            [],
            {"active": 0, "maximum": 0},
        )
        official_policy_mismatch = replace(
            self.settings,
            initial_mode="catch_up",
            catch_up_max_items=1,
        )
        market_policy_mismatch = replace(
            self.settings,
            continuous_event_cutoff="2026-09-01T00:00:00Z",
        )
        cases = (
            (
                self.scheduler(
                    official,
                    official=True,
                    base_settings=official_policy_mismatch,
                ),
                self.scheduler(market, official=False),
            ),
            (
                self.scheduler(official, official=True),
                self.scheduler(
                    market,
                    official=False,
                    base_settings=market_policy_mismatch,
                ),
            ),
        )

        for official_scheduler, market_scheduler in cases:
            with self.subTest(
                official_policy=official_scheduler.supervisor.settings.initial_mode,
                market_cutoff=(
                    market_scheduler.supervisor.settings.continuous_event_cutoff
                ),
            ):
                with self.assertRaises(SourceMonitoringRuntimeError) as raised:
                    SourceMonitoringRuntimeCoordinator(
                        pipeline_schedulers=(
                            (OFFICIAL_PIPELINE, official_scheduler),
                            (READONLY_MARKET_PIPELINE, market_scheduler),
                        ),
                        settings=self.settings,
                    )
                self.assertEqual(
                    raised.exception.code,
                    "SOURCE_MONITORING_RUNTIME_SETTINGS_MISMATCH",
                )


if __name__ == "__main__":
    unittest.main()
