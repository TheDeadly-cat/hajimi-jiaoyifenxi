from __future__ import annotations

import copy
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.market.futu_readonly import FutuUsMarketAdapter, STORAGE_SYMBOLS  # noqa: E402
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
    canonical_sha256,
)
from backend.source_monitoring.default_registry import (  # noqa: E402
    build_futu_anomaly_registry,
    build_official_source_registry,
)
from backend.source_monitoring.futu_readonly_broker import (  # noqa: E402
    FUTU_READONLY_BROKER_POLICY_SHA256,
    FutuReadOnlyBroker,
)
from backend.source_monitoring.operator_service import (  # noqa: E402
    ENABLE_SOURCE_MONITORING_ADAPTER,
    SourceMonitoringOperatorService,
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
    RUN_STATUS_SUCCEEDED,
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


class AdvancingQuoteSdk:
    """Only the quote SDK boundary is fake; production normalization still runs."""

    RET_OK = 0

    def __init__(self, clock_ms: list[int]) -> None:
        self.clock_ms = clock_ms
        self.quote_offset_ms = -500
        self.snapshot_calls: list[list[str]] = []
        self.close_calls = 0

    def OpenQuoteContext(self, **_kwargs):
        return self

    def get_market_snapshot(self, symbols):
        self.snapshot_calls.append(list(symbols))
        self.clock_ms[0] += 1_500
        quote_updated_at = datetime.fromtimestamp(
            (self.clock_ms[0] + self.quote_offset_ms) / 1_000, tz=timezone.utc
        ).isoformat(timespec="milliseconds")
        return self.RET_OK, [{
            "code": symbol,
            "update_time": quote_updated_at,
            "last_price": 106 if symbol == "US.MU" else 100,
            "prev_close_price": 100,
            "amplitude": 1,
            "volume_ratio": 1,
            "sec_status": "NORMAL",
            "suspension": False,
        } for symbol in symbols]

    def close(self):
        self.close_calls += 1


class FutuAnomalyTimeIntegrationTests(unittest.TestCase):
    def _adapter(self, clock_ms, sdk, *, market_clock_offset_ms=None):
        offset = [0] if market_clock_offset_ms is None else market_clock_offset_ms
        market = FutuUsMarketAdapter(
            sdk_module=sdk,
            socket_probe=lambda *_args: True,
            clock=lambda: datetime.fromtimestamp(
                (clock_ms[0] + offset[0]) / 1_000, tz=timezone.utc
            ),
            monotonic_clock=lambda: 0,
        )
        return FutuAnomalySourceAdapter(
            market_adapter=market, clock_ms=lambda: clock_ms[0]
        )

    def test_real_market_adapter_accepts_clock_advance_during_sdk_call(self) -> None:
        clock_ms = [OBSERVED_AT_MS]
        sdk = AdvancingQuoteSdk(clock_ms)
        market = FutuUsMarketAdapter(
            sdk_module=sdk,
            socket_probe=lambda *_args: True,
            clock=lambda: datetime.fromtimestamp(clock_ms[0] / 1_000, tz=timezone.utc),
            monotonic_clock=lambda: 0,
        )
        adapter = FutuAnomalySourceAdapter(market_adapter=market)
        with patch(
            "backend.source_monitoring.adapters.futu_anomaly.datetime", wraps=datetime
        ) as local_datetime:
            local_datetime.now.side_effect = lambda _zone: datetime.fromtimestamp(
                clock_ms[0] / 1_000, tz=timezone.utc
            )
            result = adapter.poll({}, observed_at_ms=OBSERVED_AT_MS)

        self.assertEqual(clock_ms[0], OBSERVED_AT_MS + 1_500)
        self.assertEqual(sdk.snapshot_calls, [list(STORAGE_SYMBOLS)])
        self.assertEqual(sdk.close_calls, 1)
        self.assertEqual(result.source_errors, ())
        self.assertEqual(len(result.observed_items), 1)
        self.assertEqual(result.captured_at_ms, OBSERVED_AT_MS)
        self.assertTrue(all(
            entry["last_observed_at"] == "2026-08-31T14:00:01.000Z"
            for entry in result.next_checkpoint["symbols"]
        ))

    def test_future_sdk_quote_is_rejected_without_advancing_checkpoint(self) -> None:
        clock_ms = [OBSERVED_AT_MS]
        sdk = AdvancingQuoteSdk(clock_ms)
        adapter = self._adapter(clock_ms, sdk)
        seeded = adapter.poll({}, observed_at_ms=clock_ms[0])
        self.assertEqual(seeded.source_errors, ())
        checkpoint_before = canonical_json(seeded.next_checkpoint)

        sdk.quote_offset_ms = 1
        rejected = adapter.poll(seeded.next_checkpoint, observed_at_ms=clock_ms[0])

        # Production normalization rejects even one millisecond of future quote
        # data before projection can admit the returned snapshot.
        self.assertEqual(rejected.observed_items, ())
        self.assertEqual(rejected.source_errors[0].code, "FUTU_ANOMALY_SNAPSHOT_INVALID")
        self.assertEqual(canonical_json(rejected.next_checkpoint), checkpoint_before)
        self.assertEqual(canonical_json(seeded.next_checkpoint), checkpoint_before)
        self.assertEqual(len(sdk.snapshot_calls), 2)
        self.assertEqual(sdk.close_calls, 2)

    def test_future_snapshot_capture_cannot_supply_its_own_reception_time(self) -> None:
        clock_ms = [OBSERVED_AT_MS]
        market_clock_offset_ms = [0]
        sdk = AdvancingQuoteSdk(clock_ms)
        adapter = self._adapter(clock_ms, sdk, market_clock_offset_ms=market_clock_offset_ms)
        seeded = adapter.poll({}, observed_at_ms=clock_ms[0])
        self.assertEqual(seeded.source_errors, ())
        checkpoint_before = canonical_json(seeded.next_checkpoint)

        market_clock_offset_ms[0] = 1
        rejected = adapter.poll(seeded.next_checkpoint, observed_at_ms=clock_ms[0])

        self.assertEqual(rejected.observed_items, ())
        self.assertEqual(rejected.source_errors[0].code, "FUTU_ANOMALY_OBSERVATION_FUTURE")
        self.assertEqual(canonical_json(rejected.next_checkpoint), checkpoint_before)
        self.assertEqual(canonical_json(seeded.next_checkpoint), checkpoint_before)

    def test_invalid_or_reversed_reception_clock_preserves_checkpoint(self) -> None:
        class IntegerSubclass(int):
            pass

        received_at_ms = [OBSERVED_AT_MS]
        adapter = FutuAnomalySourceAdapter(
            market_adapter=FakeQuoteClient(_fixture("live_normal_snapshot.json")),
            clock_ms=lambda: received_at_ms[0],
        )
        seeded = adapter.poll({}, observed_at_ms=OBSERVED_AT_MS)
        self.assertEqual(seeded.source_errors, ())
        checkpoint_before = canonical_json(seeded.next_checkpoint)
        for invalid in (True, -1, 1.5, "1", IntegerSubclass(OBSERVED_AT_MS), 10**30):
            with self.subTest(received_at_ms=invalid):
                received_at_ms[0] = invalid
                rejected = adapter.poll(seeded.next_checkpoint, observed_at_ms=OBSERVED_AT_MS)
                self.assertEqual(rejected.source_errors[0].code, "FUTU_ANOMALY_OBSERVED_TIME_INVALID")
                self.assertEqual(rejected.observed_items, ())
                self.assertEqual(canonical_json(rejected.next_checkpoint), checkpoint_before)
        received_at_ms[0] = OBSERVED_AT_MS - 1
        reversed_clock = adapter.poll(seeded.next_checkpoint, observed_at_ms=OBSERVED_AT_MS)
        self.assertEqual(reversed_clock.source_errors[0].code, "FUTU_ANOMALY_RECEIVED_TIME_REVERSED")
        self.assertEqual(reversed_clock.observed_items, ())
        self.assertEqual(canonical_json(reversed_clock.next_checkpoint), checkpoint_before)


class FutuAnomalyAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = [OBSERVED_AT_MS]

    def test_construction_is_zero_io_and_metadata_is_truthful(self) -> None:
        client = FakeQuoteClient(_fixture("live_anomaly_snapshot.json"))
        adapter = FutuAnomalySourceAdapter(market_adapter=client, clock_ms=lambda: self.clock[0])
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
        adapter = FutuAnomalySourceAdapter(market_adapter=client, clock_ms=lambda: self.clock[0])

        first = adapter.poll({}, observed_at_ms=OBSERVED_AT_MS)
        self.clock[0] += 60_000
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
        adapter = FutuAnomalySourceAdapter(market_adapter=client, clock_ms=lambda: self.clock[0])

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
        seed_adapter = FutuAnomalySourceAdapter(market_adapter=seed_client, clock_ms=lambda: self.clock[0])
        seed = seed_adapter.poll({}, observed_at_ms=OBSERVED_AT_MS)
        checkpoint = seed.next_checkpoint
        self.clock[0] += 60_000

        for response, expected_code in (
            (_fixture("opend_offline_snapshot.json"), "FUTU_ANOMALY_SNAPSHOT_INVALID"),
            (RuntimeError("fixture OpenD failure"), "FUTU_ANOMALY_POLL_ERROR"),
        ):
            with self.subTest(expected_code=expected_code):
                client = FakeQuoteClient(response)
                result = FutuAnomalySourceAdapter(
                    market_adapter=client, clock_ms=lambda: self.clock[0]
                ).poll(checkpoint, observed_at_ms=OBSERVED_AT_MS + 60_000)
                self.assertEqual(result.observed_items, ())
                self.assertEqual(result.next_checkpoint, checkpoint)
                self.assertEqual(result.source_errors[0].code, expected_code)
                self.assertEqual(result.market_calls_performed, 1)

    def test_capacity_and_config_drift_fail_before_quote_read(self) -> None:
        client = FakeQuoteClient(_fixture("live_anomaly_snapshot.json"))
        adapter = FutuAnomalySourceAdapter(market_adapter=client, clock_ms=lambda: self.clock[0])

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
        loaded_before = {name for name in sys.modules if name == "futu" or name.startswith("futu.")}
        official = build_official_source_registry()
        market = build_futu_anomaly_registry()

        self.assertEqual(len(official), 6)
        self.assertTrue(official.official_only)
        self.assertNotIn(FUTU_ANOMALY_ADAPTER_KEY, official.adapter_keys)
        self.assertFalse(market.official_only)
        self.assertEqual(market.adapter_keys, (FUTU_ANOMALY_ADAPTER_KEY,))
        market_adapter = market.require(FUTU_ANOMALY_ADAPTER_KEY)
        broker = market_adapter._market_adapter
        self.assertIs(type(broker), FutuReadOnlyBroker)
        self.assertEqual(broker.mode, "managed")
        self.assertEqual(broker.policy_sha256, FUTU_READONLY_BROKER_POLICY_SHA256)
        self.assertIsNone(broker._process)
        self.assertIsNone(broker._temporary_directory)
        self.assertEqual(
            {name for name in sys.modules if name == "futu" or name.startswith("futu.")},
            loaded_before,
        )

    def test_initial_seed_policy_is_static_self_sealed_and_defensive(self) -> None:
        adapter = FutuAnomalySourceAdapter(
            market_adapter=FakeQuoteClient(_fixture("live_normal_snapshot.json")),
            clock_ms=lambda: self.clock[0],
        )

        first = adapter.initial_seed_policy()
        unsigned = {key: copy.deepcopy(value) for key, value in first.items() if key != "source_policy_sha256"}
        self.assertEqual(first["source_policy_sha256"], canonical_sha256(unsigned))
        self.assertEqual(first["symbol_allowlist"], list(STORAGE_SYMBOLS))
        self.assertEqual(first["initial_mode"], "seed_only")
        self.assertEqual(first["execution_capability"], "none")
        self.assertFalse(first["live_trading_allowed"])
        self.assertNotIn("snapshot", first)
        first["symbol_allowlist"].clear()
        self.assertEqual(adapter.initial_seed_policy()["symbol_allowlist"], list(STORAGE_SYMBOLS))

    def test_production_registry_rejects_nonliteral_or_nonloopback_host(self) -> None:
        class RemoteFutuClient:
            def __init__(self, *, mode="managed") -> None:
                self.host = "remote.invalid"
                self.port = 11111
                self.cache_ttl_seconds = 5.0
                self.mode = mode
                self.timeout_ms = 15_000
                self.policy_sha256 = FUTU_READONLY_BROKER_POLICY_SHA256
                self._monotonic_ms = lambda: 0

            def quote_batch(self, symbols, *, force=False, **_kwargs):
                raise AssertionError((symbols, force))

        with (
            patch(
                "backend.source_monitoring.adapters.futu_anomaly.FutuReadOnlyBroker",
                RemoteFutuClient,
            ),
            patch(
                "backend.source_monitoring.default_registry.FutuReadOnlyBroker",
                RemoteFutuClient,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "sealed local read-only"):
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

    def _mark_legacy_initialized(self, adapter) -> None:
        started = self.repository.start_run(
            adapter.adapter_key,
            config_version=adapter.config_version,
            dry_run=False,
        )["run"]
        self.repository.complete_run(
            started["run_id"],
            next_checkpoint={},
            status=RUN_STATUS_SUCCEEDED,
            observed_count=0,
            accepted_count=0,
            duplicate_count=0,
            rejected_count=0,
            next_due_at_ms=self.clock[0] + 60_000,
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

    def test_real_sdk_chain_seeds_then_imports_anomaly_across_market_open(self) -> None:
        class MutableQuoteSdk(AdvancingQuoteSdk):
            anomaly = False

            def get_market_snapshot(self, symbols):
                ret, rows = super().get_market_snapshot(symbols)
                if not self.anomaly:
                    for row in rows:
                        row["last_price"] = 100
                return ret, rows

        open_ms = int(datetime(2026, 8, 31, 13, 30, tzinfo=timezone.utc).timestamp() * 1_000)
        self.clock[0] = open_ms - 5_000
        sdk = MutableQuoteSdk(self.clock)
        market = FutuUsMarketAdapter(
            sdk_module=sdk,
            socket_probe=lambda *_args: True,
            clock=lambda: datetime.fromtimestamp(self.clock[0] / 1_000, tz=timezone.utc),
            monotonic_clock=lambda: 0,
        )
        adapter = FutuAnomalySourceAdapter(market_adapter=market, clock_ms=lambda: self.clock[0])
        operator = SourceMonitoringOperatorService(
            store=self.store,
            settings=self._settings(),
            registry=SourceAdapterRegistry((adapter,), official_only=False),
            repository=self.repository,
            clock_ms=lambda: self.clock[0],
        )
        preview = operator.preview(
            adapter.adapter_key, expected_config_version=adapter.config_version, expected_state_version=0,
        )
        operator.set_enablement(
            adapter.adapter_key, enabled=True, expected_config_version=adapter.config_version,
            expected_state_version=0, confirmation=ENABLE_SOURCE_MONITORING_ADAPTER,
            preview_sha256=preview["preview_sha256"],
        )
        self.assertEqual(sdk.snapshot_calls, [])
        supervisor = self._supervisor(adapter)
        side_effects_before = self._side_effect_counts()
        seeded = supervisor.run_once(adapter.adapter_key)
        self.assertEqual(seeded["status"], "SUCCEEDED")
        self.assertEqual(seeded["initialization"]["outcome"], "seeded")
        self.assertIsNone(seeded["import"])
        self.assertTrue(seeded["state"]["checkpoint"])

        self.clock[0] = open_ms - 1_000
        sdk.anomaly = True
        imported = supervisor.run_once(adapter.adapter_key)
        self.assertEqual(imported["status"], "SUCCEEDED")
        self.assertEqual(imported["import"]["created_item_count"], 1)
        self.assertEqual(self.clock[0], open_ms + 500)
        self.assertEqual(imported["run"]["duration_ms"], 1_500)
        self.assertEqual(self._side_effect_counts(), side_effects_before)
        self.assertEqual(sdk.snapshot_calls, [list(STORAGE_SYMBOLS), list(STORAGE_SYMBOLS)])
        self.assertEqual(sdk.close_calls, 2)
        with closing(sqlite3.connect(self.database_path)) as connection:
            packet_text, received_at = connection.execute(
                "SELECT packet_json,received_at FROM source_inbox_imports"
            ).fetchone()
            packet = json.loads(packet_text)
            # checked_at keeps the poll-start identity. occurred_at is the
            # sealed session-open episode anchor, not the quote update time.
            # Generic Source Inbox also accepts scheduled-event timestamps;
            # Futu's strict future quote gate runs before this import layer.
            self.assertEqual(packet["checked_at"], "2026-08-31T13:29:59Z")
            self.assertEqual(packet["items"][0]["occurred_at"], "2026-08-31T13:30:00Z")
            self.assertEqual(received_at, open_ms + 500)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_inbox_items").fetchone()[0], 1)

    def test_import_once_repeat_suppressed_and_side_effect_ledgers_unchanged(self) -> None:
        adapter = FutuAnomalySourceAdapter(
            market_adapter=FakeQuoteClient(_fixture("live_anomaly_snapshot.json")),
            clock_ms=lambda: self.clock[0],
        )
        supervisor = self._supervisor(adapter)
        self._enable(adapter)
        self._mark_legacy_initialized(adapter)
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
            market_adapter=FakeQuoteClient(first_snapshot, _next_tick(first_snapshot)),
            clock_ms=lambda: self.clock[0],
        )

        def crash_after_import(_run_id, _result):
            raise SystemExit("fixture crash after Source Inbox import")

        crashing = self._supervisor(adapter, hook=crash_after_import)
        self._enable(adapter)
        self._mark_legacy_initialized(adapter)
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
                adapter = FutuAnomalySourceAdapter(market_adapter=client, clock_ms=lambda: self.clock[0])
                supervisor = self._supervisor(adapter, enabled=global_enabled)
                if enable_adapter:
                    self._enable(adapter)
                with self.assertRaises(SourceMonitoringSupervisorError):
                    supervisor.run_once(adapter.adapter_key)
                self.assertEqual(client.calls, [])
                self.assertIsNone(self.repository.get_state(adapter.adapter_key))

    def test_scheduler_aggregates_one_exact_readonly_market_call(self) -> None:
        adapter = FutuAnomalySourceAdapter(
            market_adapter=FakeQuoteClient(_fixture("live_anomaly_snapshot.json")),
            clock_ms=lambda: self.clock[0],
        )
        supervisor = self._supervisor(adapter, auto_start=True)
        self._enable(adapter)
        self._mark_legacy_initialized(adapter)
        self.clock[0] += 60_000
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
