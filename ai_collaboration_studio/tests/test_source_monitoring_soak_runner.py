from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any


os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.source_monitoring.contracts import canonical_sha256  # noqa: E402
from backend.source_inbox_service import SourceInboxService  # noqa: E402
from backend.source_monitoring.adapters.base import (  # noqa: E402
    SOURCE_ADAPTER_CONTRACT_VERSION,
)
from backend.source_monitoring.contracts import AdapterPollResult  # noqa: E402
from backend.instance_ownership import DatabaseInstanceOwner  # noqa: E402
from backend.source_monitoring.registry import SourceAdapterRegistry  # noqa: E402
from backend.source_monitoring.runtime import SourceMonitoringRuntime  # noqa: E402
from backend.source_monitoring.scheduler import SourceMonitoringScheduler  # noqa: E402
from backend.source_monitoring.settings import SourceMonitoringSettings  # noqa: E402
from backend.source_monitoring.soak_evidence import (  # noqa: E402
    SOAK_EVENT_RUN_TERMINAL,
    SOAK_EVENT_SESSION_ENDED,
    load_soak_evidence,
)
from backend.source_monitoring.soak_runner import (  # noqa: E402
    SoakRuntimeObserver,
    SourceMonitoringSoakRunner,
    SourceMonitoringSoakRunnerError,
)
from backend.source_monitoring.state_repository import (  # noqa: E402
    SourceMonitoringStateRepository,
)
from backend.source_monitoring.supervisor import (  # noqa: E402
    SourceMonitoringSupervisor,
)
from backend.store import StudioStore  # noqa: E402


RUNTIME_ID = "source_monitor_runtime_" + "a" * 32
CAMPAIGN_ID = "source_soak_campaign_" + "b" * 32
SESSION_ID = "source_soak_session_" + "c" * 32
RUN_ID = "source_run_" + "d" * 32
ZERO_SHA256 = "0" * 64


class IntegrationAdapter:
    contract_version = SOURCE_ADAPTER_CONTRACT_VERSION
    adapter_key = "fixture_adapter"
    config_version = "fixture_config_v1"
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
        etag: str = "",
        last_modified: str = "",
        max_items: int = 50,
    ) -> AdapterPollResult:
        del etag, last_modified, max_items
        return AdapterPollResult.build(
            adapter_key=self.adapter_key,
            started_checkpoint=checkpoint,
            next_checkpoint={"cursor": 1},
            observed_items=(),
            captured_at_ms=observed_at_ms,
        )


class MutableNanosecondClock:
    def __init__(self, value: int = 0) -> None:
        self.value = value
        self.lock = threading.Lock()

    def __call__(self) -> int:
        with self.lock:
            return self.value

    def advance(self, value: int) -> None:
        with self.lock:
            self.value += value


class FakeOwner:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.absolute()
        self.held = True
        self.assertions = 0

    def assert_held_for(self, database_path: str | Path) -> None:
        self.assertions += 1
        if not self.held or Path(database_path).absolute() != self.database_path:
            raise RuntimeError("owner is not held")


class FakeSettings:
    enabled = True
    auto_start = True
    dry_run = False
    trading_impact_rules_enabled = False
    official_only = True
    allow_readonly_market = False

    @staticmethod
    def to_dict() -> dict[str, Any]:
        return {
            "enabled": True,
            "auto_start": True,
            "dry_run": False,
            "trading_impact_rules_enabled": False,
            "official_only": True,
            "allow_readonly_market": False,
        }


class FakeRegistry:
    adapter_keys = ("fixture_adapter",)

    @staticmethod
    def metadata_for(adapter_key: str) -> Any:
        if adapter_key != "fixture_adapter":
            raise ValueError("unknown adapter")
        return SimpleNamespace(config_version="fixture_config_v1")

    @staticmethod
    def to_dict() -> dict[str, Any]:
        return {
            "official_only": True,
            "adapter_count": 1,
            "adapters": [
                {
                    "adapter_key": "fixture_adapter",
                    "config_version": "fixture_config_v1",
                }
            ],
        }


class FakeSupervisor:
    def __init__(self, recovered: int = 0) -> None:
        self.recovered = recovered
        self.calls = 0
        self.repository = SimpleNamespace(
            get_state=lambda adapter_key: {
                "enabled": adapter_key == "fixture_adapter",
                "config_version": "fixture_config_v1",
                "state_version": 1,
                "checkpoint_sha256": ZERO_SHA256,
            }
        )

    def initialize(self) -> int:
        self.calls += 1
        return self.recovered if self.calls == 1 else 0


class FakeRuntime:
    def __init__(
        self,
        observer: SoakRuntimeObserver,
        registry: FakeRegistry,
        *,
        observation: dict[str, Any] | None = None,
        recovered: int = 0,
        start_result: bool = True,
        stop_within_bound: bool = True,
    ) -> None:
        self._cycle_observer = observer
        self._start_gate = observer.await_activation
        self.settings = FakeSettings()
        self.scheduler = SimpleNamespace(
            registry=registry,
            supervisor=FakeSupervisor(recovered),
        )
        self.observation = observation
        self.start_result = start_result
        self.stop_within_bound = stop_within_bound
        self.wait_until_stopped_called = False
        self._alive = False
        self._started = False
        self._worker_stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._failure: BaseException | None = None

    def start(self) -> bool:
        if not self.start_result:
            return False
        self._started = True
        self._worker = threading.Thread(target=self._work, daemon=False)
        self._worker.start()
        return True

    def _work(self) -> None:
        try:
            self._start_gate()
            self._alive = True
            if self.observation is not None:
                self._cycle_observer(self.observation)
            self._worker_stop.wait(2)
        except BaseException as exc:
            self._failure = exc
            self._alive = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "runtime_id": RUNTIME_ID,
            "status": (
                "failed"
                if self._failure is not None
                else "running"
                if self._alive
                else "starting"
                if self._started
                else "stopped"
            ),
            "thread_alive": bool(
                self._worker is not None and self._worker.is_alive()
            ),
            "liveness_verified": self._alive,
            "heartbeat_age_ms": 0,
            "active_adapter": "",
            "last_loop_at": 1_900_000_000_000,
        }

    def stop(self) -> bool:
        self._worker_stop.set()
        if not self.stop_within_bound:
            return False
        worker = self._worker
        if worker is not None:
            worker.join(2)
            if worker.is_alive():
                return False
        self._alive = False
        self._started = False
        return True

    def wait_until_stopped(self) -> bool:
        self.wait_until_stopped_called = True
        worker = self._worker
        if worker is not None:
            worker.join(2)
            if worker.is_alive():
                return False
        self._alive = False
        self._started = False
        return True


def _inventory(entries: list[dict[str, str]]) -> dict[str, Any]:
    ordered = sorted(entries, key=lambda item: item["run_id"].encode("utf-8"))
    run_ids = [item["run_id"] for item in ordered]
    inventory: dict[str, Any] = {
        "version": "source_monitoring_soak_db_inventory_v1",
        "scan_order": "run_id_asc_keyset_v1",
        "scan_page_size": 500,
        "scan_page_count": 1 if ordered else 0,
        "run_row_columns": ["run_id", "status", "receipt_id"],
        "run_count": len(ordered),
        "receipt_count": sum(1 for item in ordered if item["receipt_id"]),
        "run_ids_sha256": canonical_sha256(run_ids),
        "runs_sha256": canonical_sha256(ordered),
        "inventory_sha256": "",
        "runs": ordered,
        "safety": {
            "database_writes_performed": 0,
            "network_requests_performed": 0,
            "provider_calls_performed": 0,
            "market_calls_performed": 0,
            "execution_capability": "none",
            "live_trading_allowed": False,
        },
    }
    inventory["inventory_sha256"] = canonical_sha256(
        {
            key: inventory[key]
            for key in (
                "version",
                "scan_order",
                "run_row_columns",
                "run_count",
                "receipt_count",
                "run_ids_sha256",
                "runs_sha256",
            )
        }
    )
    return inventory


class SourceMonitoringSoakRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ai-studio-soak-runner-")
        self.root = Path(self.temporary.name)
        self.database_path = self.root / "studio.sqlite3"
        self.database_path.write_bytes(b"fixture")
        self.ledger_path = self.root / "soak.jsonl"
        self.owner = FakeOwner(self.database_path)
        self.clock = MutableNanosecondClock()
        self.registry = FakeRegistry()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _waiter(self, event: threading.Event, timeout_seconds: float) -> bool:
        if event.is_set():
            return True
        time.sleep(0.001)
        self.clock.advance(round(timeout_seconds * 1_000_000_000))
        return event.is_set()

    @staticmethod
    def _run_entry() -> dict[str, str]:
        return {
            "run_id": RUN_ID,
            "status": "SUCCEEDED",
            "row_sha256": "1" * 64,
            "receipt_id": "",
            "receipt_sha256": "",
        }

    @staticmethod
    def _terminal_reader(_path: str | Path, run_id: str) -> dict[str, Any] | None:
        if run_id != RUN_ID:
            return None
        return {
            "run_id": RUN_ID,
            "adapter_key": "fixture_adapter",
            "status": "SUCCEEDED",
            "row_sha256": "1" * 64,
            "receipt_id": "",
            "receipt_sha256": "",
            "observed_count": 0,
            "accepted_count": 0,
            "duplicate_count": 0,
            "rejected_count": 0,
            "error_code": "",
        }

    def _runner(
        self,
        *,
        recovered: int = 0,
        start_result: bool = True,
        observation: dict[str, Any] | None = None,
        inventories: list[dict[str, Any]] | None = None,
        stop_within_bound: bool = True,
        baseline_inventory_sink: Any = None,
        final_inventory_sink: Any = None,
    ) -> SourceMonitoringSoakRunner:
        observer = SoakRuntimeObserver(
            self.database_path,
            self.registry,
            terminal_reader=self._terminal_reader,
            wall_time_ms=lambda: 1_900_000_000_000,
            monotonic_ns=self.clock,
            bind_timeout_seconds=2,
        )
        runtime = FakeRuntime(
            observer,
            self.registry,
            observation=observation,
            recovered=recovered,
            start_result=start_result,
            stop_within_bound=stop_within_bound,
        )
        supplied = list(inventories or [_inventory([]), _inventory([])])

        def inventory_builder(_path: str | Path) -> dict[str, Any]:
            return supplied.pop(0)

        runner = SourceMonitoringSoakRunner(
            runtime=runtime,
            observer=observer,
            database_path=self.database_path,
            ledger_path=self.ledger_path,
            campaign_id=CAMPAIGN_ID,
            session_id=SESSION_ID,
            preview_sha256=ZERO_SHA256,
            expected_enabled_adapters=(
                {
                    "adapter_key": "fixture_adapter",
                    "config_version": "fixture_config_v1",
                    "state_version": 1,
                    "checkpoint_sha256": ZERO_SHA256,
                },
            ),
            database_owner=self.owner,
            code_identity_sha256=ZERO_SHA256,
            code_identity_checker=lambda: ZERO_SHA256,
            db_startup_identity_sha256=ZERO_SHA256,
            db_schema_sha256=ZERO_SHA256,
            inventory_builder=inventory_builder,
            baseline_inventory_sink=baseline_inventory_sink,
            final_inventory_sink=final_inventory_sink,
            monotonic_ns=self.clock,
            waiter=self._waiter,
            _required_duration_ns=20,
            _sample_interval_ns=10,
            _maximum_sample_gap_ns=20,
        )
        runner._test_runtime = runtime
        return runner

    def test_fixed_lifecycle_seals_start_samples_terminal_run_and_end(self) -> None:
        observation = {
            "runtime_id": RUNTIME_ID,
            "adapter_key": "fixture_adapter",
            "run_id": RUN_ID,
            "status": "SUCCEEDED",
            "state_recorded": True,
            "market_calls_performed": 0,
        }
        runner = self._runner(
            observation=observation,
            inventories=[_inventory([]), _inventory([self._run_entry()])],
        )

        result = runner.run()
        records = load_soak_evidence(self.ledger_path)

        self.assertEqual(result["end_reason"], "duration_reached")
        self.assertTrue(result["runtime_stopped_cleanly"])
        self.assertEqual(result["database_verdict"]["verdict"], "PASS")
        self.assertEqual(result["source_acceptance_verdict"], "NOT_EVALUATED")
        self.assertEqual(result["overall_acceptance"], "NOT_CLAIMED")
        terminal = [
            record
            for record in records
            if record["event_type"] == SOAK_EVENT_RUN_TERMINAL
        ]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["payload"]["run_record_sha256"], "1" * 64)
        self.assertEqual(
            terminal[0]["payload"]["config_version"],
            "fixture_config_v1",
        )
        self.assertEqual(records[0]["monotonic_elapsed_ns"], 0)
        self.assertEqual(records[-1]["payload"]["session_run_count"], 1)

    def test_runtime_setup_time_is_not_counted_toward_live_duration(self) -> None:
        runner = self._runner()
        original_start = runner._test_runtime.start

        def delayed_start() -> bool:
            self.clock.advance(1_000)
            return original_start()

        runner._test_runtime.start = delayed_start

        result = runner.run()
        records = load_soak_evidence(self.ledger_path)

        self.assertEqual(result["end_reason"], "duration_reached")
        self.assertEqual(records[-1]["payload"]["elapsed_ns"], 20)

    def test_real_runtime_and_sqlite_row_are_bound_end_to_end(self) -> None:
        database_path = self.root / "integration.sqlite3"
        store = StudioStore(database_path)
        settings = SourceMonitoringSettings(
            enabled=True,
            auto_start=True,
            official_only=True,
            allow_readonly_market=False,
            dry_run=False,
            trading_impact_rules_enabled=False,
        )
        registry = SourceAdapterRegistry((IntegrationAdapter(),))
        repository = SourceMonitoringStateRepository(store)
        repository.set_enabled(
            "fixture_adapter",
            config_version="fixture_config_v1",
            enabled=True,
        )
        enabled_state = repository.get_state("fixture_adapter")
        assert enabled_state is not None
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
        observer = SoakRuntimeObserver(database_path, registry)
        runtime = SourceMonitoringRuntime(
            scheduler=scheduler,
            settings=settings,
            heartbeat_interval_ms=5,
            join_timeout_ms=2_000,
            cycle_observer=observer,
            start_gate=observer.await_activation,
        )
        with DatabaseInstanceOwner(database_path) as owner:
            runner = SourceMonitoringSoakRunner(
                runtime=runtime,
                observer=observer,
                database_path=database_path,
                ledger_path=self.root / "integration.jsonl",
                campaign_id=CAMPAIGN_ID,
                session_id=SESSION_ID,
                preview_sha256=ZERO_SHA256,
                expected_enabled_adapters=(
                    {
                        "adapter_key": "fixture_adapter",
                        "config_version": "fixture_config_v1",
                        "state_version": enabled_state["state_version"],
                        "checkpoint_sha256": enabled_state["checkpoint_sha256"],
                    },
                ),
                database_owner=owner,
                code_identity_sha256=ZERO_SHA256,
                code_identity_checker=lambda: ZERO_SHA256,
                db_startup_identity_sha256=ZERO_SHA256,
                db_schema_sha256=ZERO_SHA256,
                _required_duration_ns=500_000_000,
                _sample_interval_ns=100_000_000,
                _maximum_sample_gap_ns=300_000_000,
            )

            result = runner.run()

        self.assertEqual(result["end_reason"], "duration_reached")
        self.assertEqual(result["database_verdict"]["verdict"], "PASS")
        self.assertEqual(result["database_verdict"]["counts"]["added_run_count"], 1)
        self.assertEqual(result["final_run_count"], 1)
        self.assertEqual(len(observer.run_ids), 1)
        self.assertEqual(repository.get_run(observer.run_ids[0])["status"], "SUCCEEDED")

    def test_code_identity_drift_after_quiescence_refuses_terminal_seal(self) -> None:
        runner = self._runner()
        observations = iter((ZERO_SHA256, "1" * 64))
        runner._code_identity_checker = lambda: next(observations)

        with self.assertRaisesRegex(
            SourceMonitoringSoakRunnerError,
            "code identity differs",
        ) as captured:
            runner.run()

        self.assertEqual(
            captured.exception.code,
            "SOURCE_MONITORING_SOAK_CODE_IDENTITY_DRIFT",
        )
        self.assertNotEqual(
            load_soak_evidence(self.ledger_path)[-1]["event_type"],
            SOAK_EVENT_SESSION_ENDED,
        )
        self.assertFalse(runner._test_runtime.snapshot()["thread_alive"])

    def test_code_identity_is_rechecked_after_runtime_construction_before_start(self) -> None:
        runner = self._runner()
        runner._code_identity_checker = lambda: "1" * 64

        with self.assertRaises(SourceMonitoringSoakRunnerError) as captured:
            runner.run()

        self.assertEqual(
            captured.exception.code,
            "SOURCE_MONITORING_SOAK_CODE_IDENTITY_DRIFT",
        )
        self.assertFalse(runner._test_runtime._started)
        self.assertFalse(self.ledger_path.exists())

    def test_recovered_running_row_refuses_start_and_creates_no_ledger(self) -> None:
        runner = self._runner(recovered=1)

        with self.assertRaisesRegex(
            SourceMonitoringSoakRunnerError,
            "cannot begin after recovering incomplete runs",
        ):
            runner.run()

        self.assertFalse(self.ledger_path.exists())

    def test_preexisting_running_row_is_rejected_before_recovery_mutation(self) -> None:
        running = self._run_entry()
        running["status"] = "RUNNING"
        runner = self._runner(inventories=[_inventory([running])])

        with self.assertRaisesRegex(
            SourceMonitoringSoakRunnerError,
            "refuses pre-existing RUNNING rows",
        ):
            runner.run()

        self.assertEqual(runner._test_runtime.scheduler.supervisor.calls, 0)
        self.assertFalse(self.ledger_path.exists())

    def test_runtime_start_failure_creates_no_resumable_ledger(self) -> None:
        runner = self._runner(start_result=False)

        with self.assertRaisesRegex(
            SourceMonitoringSoakRunnerError,
            "managed runtime did not start",
        ):
            runner.run()

        self.assertFalse(self.ledger_path.exists())

    def test_matching_database_owner_is_mandatory_before_any_work(self) -> None:
        runner = self._runner()
        self.owner.held = False

        with self.assertRaisesRegex(
            SourceMonitoringSoakRunnerError,
            "ownership is not held",
        ):
            runner.run()

        self.assertEqual(runner._test_runtime.scheduler.supervisor.calls, 0)
        self.assertFalse(self.ledger_path.exists())

    def test_confirmed_state_and_checkpoint_drift_fail_before_baseline(self) -> None:
        runner = self._runner()
        runner._test_runtime.scheduler.supervisor.repository.get_state = (
            lambda _adapter_key: {
                "enabled": True,
                "config_version": "fixture_config_v1",
                "state_version": 2,
                "checkpoint_sha256": "9" * 64,
            }
        )

        with self.assertRaisesRegex(
            SourceMonitoringSoakRunnerError,
            "differs from the confirmed preview",
        ):
            runner.run()

        self.assertEqual(runner._test_runtime.scheduler.supervisor.calls, 0)
        self.assertFalse(self.ledger_path.exists())

    def test_v1_runner_refuses_futu_mode_before_runtime_start(self) -> None:
        runner = self._runner()
        runner._test_runtime.settings.official_only = False
        runner._test_runtime.settings.allow_readonly_market = True

        with self.assertRaisesRegex(
            SourceMonitoringSoakRunnerError,
            "official-only",
        ):
            runner.run()

        self.assertFalse(self.ledger_path.exists())

    def test_operator_interrupt_is_terminal_but_never_claims_acceptance(self) -> None:
        stop = threading.Event()
        stop.set()
        runner = self._runner()
        runner.stop_event = stop

        result = runner.run()
        records = load_soak_evidence(self.ledger_path)

        self.assertEqual(result["end_reason"], "operator_interrupted")
        self.assertEqual(result["overall_acceptance"], "NOT_CLAIMED")
        self.assertEqual(records[-1]["payload"]["reason"], "operator_interrupted")

    def test_inventory_sinks_run_at_baseline_and_after_runtime_quiescence(self) -> None:
        order: list[str] = []
        runner = self._runner(
            stop_within_bound=False,
            baseline_inventory_sink=lambda _inventory: order.append("baseline"),
            final_inventory_sink=lambda _inventory: order.append(
                "final_after_wait"
                if runner._test_runtime.wait_until_stopped_called
                else "final_while_live"
            ),
        )

        result = runner.run()

        self.assertEqual(order, ["baseline", "final_after_wait"])
        self.assertTrue(runner._test_runtime.wait_until_stopped_called)
        self.assertFalse(result["runtime_stopped_cleanly"])
        self.assertEqual(result["end_reason"], "runtime_failed")

    def test_post_sample_fatal_state_cannot_seal_a_clean_duration_end(self) -> None:
        runner = self._runner()
        original_stop = runner._test_runtime.stop

        def fatal_stop() -> bool:
            stopped = original_stop()
            runner._test_runtime._failure = RuntimeError("late fatal")
            return stopped

        runner._test_runtime.stop = fatal_stop

        result = runner.run()
        records = load_soak_evidence(self.ledger_path)

        self.assertEqual(result["end_reason"], "runtime_failed")
        self.assertFalse(result["runtime_stopped_cleanly"])
        self.assertEqual(records[-1]["payload"]["reason"], "runtime_failed")

    def test_terminal_row_mismatch_fail_stops_runtime_and_fails_database_verdict(self) -> None:
        observation = {
            "runtime_id": RUNTIME_ID,
            "adapter_key": "fixture_adapter",
            "run_id": RUN_ID,
            "status": "FAILED",
            "state_recorded": True,
            "market_calls_performed": 0,
        }
        runner = self._runner(
            observation=observation,
            inventories=[_inventory([]), _inventory([self._run_entry()])],
        )

        result = runner.run()
        records = load_soak_evidence(self.ledger_path)

        self.assertEqual(result["end_reason"], "runtime_failed")
        self.assertEqual(result["database_verdict"]["verdict"], "FAIL")
        self.assertEqual(records[-1]["event_type"], "SESSION_ENDED")
        self.assertEqual(result["overall_acceptance"], "NOT_CLAIMED")


if __name__ == "__main__":
    unittest.main()
