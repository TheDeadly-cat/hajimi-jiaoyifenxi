from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.market.futu_readonly import STORAGE_SYMBOLS  # noqa: E402
from backend.source_inbox_service import SourceInboxService  # noqa: E402
from backend.source_monitoring.adapters.base import (  # noqa: E402
    validate_source_adapter,
)
from backend.source_monitoring.adapters.futu_anomaly import (  # noqa: E402
    FUTU_ANOMALY_ADAPTER_KEY,
    FUTU_ANOMALY_CANDIDATE_LIMIT,
    FutuAnomalySourceAdapter,
)
from backend.source_monitoring.contracts import (  # noqa: E402
    FUTU_ANOMALY_SOURCE_CHANNEL,
    READONLY_MARKET_SOURCE_CLASS,
    canonical_json,
)
from backend.source_monitoring.default_registry import (  # noqa: E402
    build_futu_anomaly_registry,
    build_official_source_registry,
)
from backend.source_monitoring.registry import (  # noqa: E402
    SourceAdapterRegistry,
    SourceAdapterRegistryError,
)
from backend.source_monitoring.scheduler import (  # noqa: E402
    BackoffPolicy,
    SourceMonitoringScheduler,
)
from backend.source_monitoring.settings import SourceMonitoringSettings  # noqa: E402
from backend.source_monitoring.state_repository import (  # noqa: E402
    RUN_STATUS_ABANDONED,
    SourceMonitoringStateRepository,
)
from backend.source_monitoring.supervisor import (  # noqa: E402
    SourceMonitoringSupervisor,
    SourceMonitoringSupervisorError,
)
from backend.store import StudioStore  # noqa: E402


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "futu_anomaly"
OBSERVED_AT_MS = 1_788_184_800_000


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _next_tick(snapshot: dict[str, object]) -> dict[str, object]:
    updated = copy.deepcopy(snapshot)
    updated["snapshot_id"] = "futu_fixture_changed_tick"
    updated["captured_at"] = "2026-08-31T14:01:00.000Z"
    updated["captured_at_ms"] = OBSERVED_AT_MS + 60_000
    for row in updated["rows"]:
        row["market_time"] = "2026-08-31 10:00:30"
        row["updated_at"] = "2026-08-31T14:00:30.000Z"
        row["age_seconds"] = 30
        if row["symbol"] == "US.MU":
            row["last"] = 105.5
            row["change_rate"] = 5.5
    return updated


class FakeQuoteClient:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[tuple[str, ...], bool]] = []

    def quote_batch(self, symbols, *, force=False):
        self.calls.append((tuple(symbols), force))
        selected = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(selected, BaseException):
            raise selected
        return copy.deepcopy(selected)


class FutuAnomalyAdapterTests(unittest.TestCase):
    def test_construction_is_zero_io_and_metadata_is_truthful(self) -> None:
        client = FakeQuoteClient(_fixture("live_anomaly_snapshot.json"))
        adapter = FutuAnomalySourceAdapter(market_adapter=client)
        metadata = validate_source_adapter(adapter)

        self.assertEqual(client.calls, [])
        self.assertFalse(metadata.official_source)
        self.assertEqual(metadata.source_class, READONLY_MARKET_SOURCE_CLASS)
        self.assertEqual(metadata.source_channel, FUTU_ANOMALY_SOURCE_CHANNEL)
        self.assertEqual(metadata.max_market_calls_per_poll, 1)
        self.assertEqual(metadata.max_candidates_per_poll, FUTU_ANOMALY_CANDIDATE_LIMIT)
        self.assertEqual(metadata.execution_capability, "none")
        self.assertFalse(metadata.live_trading_allowed)
        with self.assertRaises(SourceAdapterRegistryError):
            SourceAdapterRegistry((adapter,))
        self.assertIs(
            SourceAdapterRegistry((adapter,), official_only=False).require(
                adapter.adapter_key
            ),
            adapter,
        )

    def test_poll_reads_one_fixed_batch_and_replay_is_deterministic(self) -> None:
        first_snapshot = _fixture("live_anomaly_snapshot.json")
        changed_tick = _next_tick(first_snapshot)
        client = FakeQuoteClient(first_snapshot, changed_tick)
        adapter = FutuAnomalySourceAdapter(market_adapter=client)

        first = adapter.poll({}, observed_at_ms=OBSERVED_AT_MS)
        replay_from_same_checkpoint = adapter.poll(
            {},
            observed_at_ms=OBSERVED_AT_MS + 60_000,
        )

        self.assertEqual(client.calls, [
            (tuple(STORAGE_SYMBOLS), True),
            (tuple(STORAGE_SYMBOLS), True),
        ])
        self.assertEqual(first.market_calls_performed, 1)
        self.assertEqual(replay_from_same_checkpoint.market_calls_performed, 1)
        self.assertEqual(len(first.observed_items), 1)
        self.assertEqual(
            canonical_json(list(first.observed_items)),
            canonical_json(list(replay_from_same_checkpoint.observed_items)),
        )
        item = first.observed_items[0]
        self.assertEqual(item["impact_hypotheses"], [])
        self.assertEqual(item["recommended_route"], "notify_only")
        extension = item["extensions"]["futu_anomaly_v1"]
        self.assertFalse(extension["news_attribution_performed"])
        self.assertEqual(extension["causal_attribution"], "none")
        self.assertTrue(extension["signal_only"])

    def test_committed_checkpoint_suppresses_same_episode(self) -> None:
        snapshot = _fixture("live_anomaly_snapshot.json")
        client = FakeQuoteClient(snapshot)
        adapter = FutuAnomalySourceAdapter(market_adapter=client)

        first = adapter.poll({}, observed_at_ms=OBSERVED_AT_MS)
        duplicate = adapter.poll(
            first.next_checkpoint,
            observed_at_ms=OBSERVED_AT_MS,
        )

        self.assertEqual(len(first.observed_items), 1)
        self.assertEqual(duplicate.observed_items, ())
        self.assertEqual(duplicate.duplicate_count, 1)
        self.assertEqual(duplicate.next_checkpoint, first.next_checkpoint)

    def test_source_failures_are_atomic_and_preserve_checkpoint(self) -> None:
        seed_client = FakeQuoteClient(_fixture("live_normal_snapshot.json"))
        seed_adapter = FutuAnomalySourceAdapter(market_adapter=seed_client)
        seed = seed_adapter.poll({}, observed_at_ms=OBSERVED_AT_MS)
        checkpoint = seed.next_checkpoint

        for response, expected_code in (
            (_fixture("opend_offline_snapshot.json"), "FUTU_ANOMALY_SNAPSHOT_INVALID"),
            (RuntimeError("fixture OpenD failure"), "FUTU_ANOMALY_POLL_ERROR"),
        ):
            with self.subTest(expected_code=expected_code):
                client = FakeQuoteClient(response)
                result = FutuAnomalySourceAdapter(
                    market_adapter=client
                ).poll(checkpoint, observed_at_ms=OBSERVED_AT_MS + 60_000)
                self.assertEqual(result.observed_items, ())
                self.assertEqual(result.next_checkpoint, checkpoint)
                self.assertEqual(result.source_errors[0].code, expected_code)
                self.assertEqual(result.market_calls_performed, 1)

    def test_capacity_and_config_drift_fail_before_quote_read(self) -> None:
        client = FakeQuoteClient(_fixture("live_anomaly_snapshot.json"))
        adapter = FutuAnomalySourceAdapter(market_adapter=client)

        with self.assertRaisesRegex(
            Exception,
            "candidate bound",
        ):
            adapter.poll(
                {},
                observed_at_ms=OBSERVED_AT_MS,
                max_items=FUTU_ANOMALY_CANDIDATE_LIMIT - 1,
            )
        self.assertEqual(client.calls, [])

        original = client.quote_batch
        client.quote_batch = lambda *_args, **_kwargs: _fixture(
            "live_anomaly_snapshot.json"
        )
        with self.assertRaisesRegex(Exception, "quote callable"):
            adapter.poll({}, observed_at_ms=OBSERVED_AT_MS)
        self.assertEqual(client.calls, [])
        client.quote_batch = original

    def test_production_registries_remain_separate_and_construction_only(self) -> None:
        official = build_official_source_registry()
        market = build_futu_anomaly_registry()

        self.assertEqual(len(official), 6)
        self.assertTrue(official.official_only)
        self.assertNotIn(FUTU_ANOMALY_ADAPTER_KEY, official.adapter_keys)
        self.assertFalse(market.official_only)
        self.assertEqual(market.adapter_keys, (FUTU_ANOMALY_ADAPTER_KEY,))
        market_adapter = market.require(FUTU_ANOMALY_ADAPTER_KEY)
        self.assertEqual(market_adapter._market_adapter._cache, {})
        self.assertFalse(market_adapter._market_adapter._sdk_installed)
        self.assertEqual(market_adapter._market_adapter._sdk_import_error, "")

    def test_production_registry_rejects_nonliteral_or_nonloopback_host(self) -> None:
        class RemoteFutuClient:
            def __init__(self) -> None:
                self.host = "remote.invalid"
                self.port = 11111
                self.cache_ttl_seconds = 5.0
                self._socket_probe = lambda *_args: True
                self._clock = lambda: None
                self._monotonic_clock = lambda: 0.0
                self._snapshot_id_factory = lambda: "fixture"

            def quote_batch(self, symbols, *, force=False):
                raise AssertionError((symbols, force))

        with (
            patch(
                "backend.source_monitoring.adapters.futu_anomaly.FutuUsMarketAdapter",
                RemoteFutuClient,
            ),
            patch(
                "backend.source_monitoring.default_registry.FutuUsMarketAdapter",
                RemoteFutuClient,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "literal loopback"):
                build_futu_anomaly_registry()


class FutuAnomalySupervisorIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="ai-studio-futu-anomaly-")
        self.database_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.clock = [OBSERVED_AT_MS]
        self.store = StudioStore(self.database_path)
        self.repository = SourceMonitoringStateRepository(
            self.store,
            clock_ms=lambda: self.clock[0],
        )
        self.inbox = SourceInboxService(
            self.store,
            clock=lambda: self.clock[0] / 1_000,
        )
        self.backoff = BackoffPolicy(
            initial_delay_ms=60_000,
            maximum_delay_ms=300_000,
            jitter_ratio=0,
            random_source=lambda: 0.5,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _settings(self, *, enabled=True, auto_start=False):
        return SourceMonitoringSettings(
            enabled=enabled,
            auto_start=auto_start,
            official_only=False,
            allow_readonly_market=True,
            dry_run=False,
            max_items_per_run=50,
        )

    def _supervisor(self, adapter, *, enabled=True, auto_start=False, hook=None):
        return SourceMonitoringSupervisor(
            registry=SourceAdapterRegistry((adapter,), official_only=False),
            repository=self.repository,
            source_inbox=self.inbox,
            settings=self._settings(enabled=enabled, auto_start=auto_start),
            backoff_policy=self.backoff,
            clock_ms=lambda: self.clock[0],
            after_import_hook=hook,
        )

    def _enable(self, adapter) -> None:
        self.repository.set_enabled(
            adapter.adapter_key,
            config_version=adapter.config_version,
            enabled=True,
        )

    def _side_effect_counts(self) -> tuple[int, int, int, int]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            return tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "provider_execution_runs",
                    "provider_call_attempts",
                    "rounds",
                    "source_inbox_round_drafts",
                )
            )

    def test_import_once_repeat_suppressed_and_side_effect_ledgers_unchanged(self) -> None:
        adapter = FutuAnomalySourceAdapter(
            market_adapter=FakeQuoteClient(_fixture("live_anomaly_snapshot.json"))
        )
        supervisor = self._supervisor(adapter)
        self._enable(adapter)
        side_effects_before = self._side_effect_counts()

        first = supervisor.run_once(adapter.adapter_key)
        self.clock[0] += 60_000
        second = supervisor.run_once(adapter.adapter_key)

        self.assertEqual(first["status"], "SUCCEEDED")
        self.assertEqual(first["import"]["created_item_count"], 1)
        self.assertEqual(first["safety"]["market_calls_performed"], 1)
        self.assertEqual(second["status"], "SUCCEEDED")
        self.assertIsNone(second["import"])
        self.assertEqual(second["run"]["duplicate_count"], 1)
        self.assertEqual(self._side_effect_counts(), side_effects_before)
        with closing(sqlite3.connect(self.database_path)) as connection:
            channel, import_count, item_count = connection.execute(
                "SELECT source_channel, "
                "(SELECT COUNT(*) FROM source_inbox_imports), "
                "(SELECT COUNT(*) FROM source_inbox_items) "
                "FROM source_inbox_imports LIMIT 1"
            ).fetchone()
        self.assertEqual(channel, FUTU_ANOMALY_SOURCE_CHANNEL)
        self.assertEqual((import_count, item_count), (1, 1))

    def test_crash_replay_with_changed_tick_is_source_inbox_duplicate(self) -> None:
        first_snapshot = _fixture("live_anomaly_snapshot.json")
        adapter = FutuAnomalySourceAdapter(
            market_adapter=FakeQuoteClient(first_snapshot, _next_tick(first_snapshot))
        )

        def crash_after_import(_run_id, _result):
            raise SystemExit("fixture crash after Source Inbox import")

        crashing = self._supervisor(adapter, hook=crash_after_import)
        self._enable(adapter)
        with self.assertRaises(SystemExit):
            crashing.run_once(adapter.adapter_key)
        self.assertEqual(
            self.repository.get_state(adapter.adapter_key)["checkpoint"],
            {},
        )

        self.clock[0] += 60_000
        restarted = self._supervisor(adapter)
        replay = restarted.run_once(adapter.adapter_key)

        self.assertEqual(replay["status"], "SUCCEEDED")
        self.assertEqual(replay["import"]["created_item_count"], 0)
        self.assertEqual(replay["import"]["duplicate_item_count"], 1)
        self.assertNotEqual(replay["state"]["checkpoint"], {})
        self.assertIn(
            RUN_STATUS_ABANDONED,
            {run["status"] for run in self.repository.list_runs(adapter_key=adapter.adapter_key)},
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM source_inbox_items").fetchone()[0],
                1,
            )

    def test_disabled_modes_do_not_poll_or_create_monitoring_rows(self) -> None:
        for global_enabled, enable_adapter in ((False, False), (True, False)):
            with self.subTest(
                global_enabled=global_enabled,
                enable_adapter=enable_adapter,
            ):
                client = FakeQuoteClient(_fixture("live_anomaly_snapshot.json"))
                adapter = FutuAnomalySourceAdapter(market_adapter=client)
                supervisor = self._supervisor(adapter, enabled=global_enabled)
                if enable_adapter:
                    self._enable(adapter)
                with self.assertRaises(SourceMonitoringSupervisorError):
                    supervisor.run_once(adapter.adapter_key)
                self.assertEqual(client.calls, [])
                if global_enabled:
                    self.assertIsNotNone(self.repository.get_state(adapter.adapter_key))
                else:
                    self.assertIsNone(self.repository.get_state(adapter.adapter_key))

    def test_scheduler_aggregates_one_exact_readonly_market_call(self) -> None:
        adapter = FutuAnomalySourceAdapter(
            market_adapter=FakeQuoteClient(_fixture("live_anomaly_snapshot.json"))
        )
        supervisor = self._supervisor(adapter, auto_start=True)
        self._enable(adapter)
        scheduler = SourceMonitoringScheduler(
            registry=supervisor.registry,
            repository=self.repository,
            supervisor=supervisor,
            clock_ms=lambda: self.clock[0],
        )

        cycle = scheduler.run_due()

        self.assertEqual(cycle["run_count"], 1)
        self.assertEqual(cycle["safety"]["market_calls_performed"], 1)
        self.assertEqual(cycle["safety"]["market_calls_accounting"], "exact")
        self.assertEqual(cycle["safety"]["market_calls_possible_max"], 1)


if __name__ == "__main__":
    unittest.main()
