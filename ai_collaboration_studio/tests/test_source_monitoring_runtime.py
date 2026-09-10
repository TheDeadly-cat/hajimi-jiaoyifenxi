from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any, Callable


os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.source_inbox_service import SourceInboxService  # noqa: E402
from backend.source_monitoring.adapters.base import (  # noqa: E402
    SOURCE_ADAPTER_CONTRACT_VERSION,
)
from backend.source_monitoring.contracts import AdapterPollResult  # noqa: E402
from backend.source_monitoring.registry import SourceAdapterRegistry  # noqa: E402
from backend.source_monitoring.runtime import (  # noqa: E402
    SOURCE_MONITORING_RUNTIME_CYCLE_OBSERVATION_VERSION,
    SOURCE_MONITORING_RUNTIME_FATAL,
    SOURCE_MONITORING_RUNTIME_OBSERVER_FAILED,
    SOURCE_MONITORING_RUNTIME_START_GATE_FAILED,
    SourceMonitoringRuntime,
)
from backend.source_monitoring.runtime_state import (  # noqa: E402
    SourceMonitoringRuntimeState,
)
from backend.source_monitoring.scheduler import (  # noqa: E402
    BackoffPolicy,
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


START_MS = 1_788_150_000_000


class MutableClock:
    def __init__(self, value: int = START_MS) -> None:
        self._value = value
        self._lock = threading.Lock()

    def __call__(self) -> int:
        with self._lock:
            return self._value

    def advance(self, milliseconds: int) -> None:
        with self._lock:
            self._value += milliseconds


class FakeAdapter:
    contract_version = SOURCE_ADAPTER_CONTRACT_VERSION
    official_source = True
    execution_capability = "none"
    live_trading_allowed = False
    poll_interval_ms = 60_000
    max_candidates_per_poll = 2

    def __init__(
        self,
        adapter_key: str,
        *,
        raises: bool = False,
        entered: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        self.adapter_key = adapter_key
        self.config_version = f"{adapter_key}_config_v1"
        self.raises = raises
        self.entered = entered
        self.release = release
        self.poll_count = 0
        self.started_checkpoints: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def count(self) -> int:
        with self._lock:
            return self.poll_count

    def poll(
        self,
        checkpoint: dict[str, Any],
        *,
        observed_at_ms: int,
        deadline_monotonic_ms: int = 0,
        cancel_event=None,
        etag: str = "",
        last_modified: str = "",
        max_items: int = 50,
    ) -> AdapterPollResult:
        del deadline_monotonic_ms, cancel_event, etag, last_modified, max_items
        with self._lock:
            self.poll_count += 1
            self.started_checkpoints.append(dict(checkpoint))
            poll_number = self.poll_count
        if self.entered is not None:
            self.entered.set()
        if self.release is not None and not self.release.wait(2):
            raise RuntimeError("test release event timed out")
        if self.raises:
            raise RuntimeError("fixed fake adapter failure")
        return AdapterPollResult.build(
            adapter_key=self.adapter_key,
            started_checkpoint=checkpoint,
            next_checkpoint={"cursor": poll_number},
            observed_items=(),
            captured_at_ms=observed_at_ms,
        )


class LockBlockedStopAdapter(FakeAdapter):
    """Hold the poll resource lock while ignoring cooperative cancellation."""

    def __init__(self, adapter_key: str) -> None:
        self.poll_entered = threading.Event()
        self.poll_release = threading.Event()
        self.stop_attempted = threading.Event()
        self.stop_calls = 0
        self._resource_lock = threading.Lock()
        super().__init__(
            adapter_key,
            entered=self.poll_entered,
            release=self.poll_release,
        )

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
        with self._resource_lock:
            return super().poll(
                checkpoint,
                observed_at_ms=observed_at_ms,
                deadline_monotonic_ms=deadline_monotonic_ms,
                cancel_event=cancel_event,
                etag=etag,
                last_modified=last_modified,
                max_items=max_items,
            )

    def stop(self) -> bool:
        self.stop_attempted.set()
        with self._resource_lock:
            self.stop_calls += 1
        return True


def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 3,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class SourceMonitoringRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-monitor-runtime-"
        )
        self.database_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.store = StudioStore(self.database_path)
        self.clock = MutableClock()
        self.runtimes: list[SourceMonitoringRuntime] = []

    def tearDown(self) -> None:
        for runtime in reversed(self.runtimes):
            runtime.stop()
            runtime.wait_until_stopped(2)
        self.temp_dir.cleanup()

    @staticmethod
    def settings(
        *,
        enabled: bool = True,
        auto_start: bool = True,
        dry_run: bool = False,
    ) -> SourceMonitoringSettings:
        return SourceMonitoringSettings(
            enabled=enabled,
            auto_start=auto_start,
            official_only=True,
            dry_run=dry_run,
            max_items_per_run=50,
        )

    def build_runtime(
        self,
        adapters: tuple[FakeAdapter, ...],
        *,
        settings: SourceMonitoringSettings | None = None,
        enable: tuple[str, ...] = (),
        heartbeat_interval_ms: int = 1,
        join_timeout_ms: int = 2_000,
        poll_timeout_ms: int | None = None,
        monotonic_ms: Callable[[], Any] | None = None,
        cycle_observer: Callable[[dict[str, Any]], Any] | None = None,
        start_gate: Callable[[], Any] | None = None,
    ) -> tuple[
        SourceMonitoringRuntime,
        SourceMonitoringStateRepository,
        SourceMonitoringScheduler,
    ]:
        resolved_settings = settings or self.settings()
        repository = SourceMonitoringStateRepository(
            self.store,
            clock_ms=self.clock,
        )
        registry = SourceAdapterRegistry(adapters)
        supervisor = SourceMonitoringSupervisor(
            registry=registry,
            repository=repository,
            source_inbox=SourceInboxService(
                self.store,
                clock=lambda: self.clock() / 1_000,
            ),
            settings=resolved_settings,
            backoff_policy=BackoffPolicy(
                initial_delay_ms=30_000,
                maximum_delay_ms=120_000,
                jitter_ratio=0,
                random_source=lambda: 0.5,
            ),
            clock_ms=self.clock,
        )
        scheduler = SourceMonitoringScheduler(
            registry=registry,
            repository=repository,
            supervisor=supervisor,
            clock_ms=self.clock,
        )
        by_key = {adapter.adapter_key: adapter for adapter in adapters}
        for adapter_key in enable:
            adapter = by_key[adapter_key]
            repository.set_enabled(
                adapter_key,
                config_version=adapter.config_version,
                enabled=True,
            )
        runtime = SourceMonitoringRuntime(
            scheduler=scheduler,
            settings=resolved_settings,
            heartbeat_interval_ms=heartbeat_interval_ms,
            join_timeout_ms=join_timeout_ms,
            poll_timeout_ms=poll_timeout_ms,
            clock_ms=self.clock,
            monotonic_ms=monotonic_ms,
            cycle_observer=cycle_observer,
            start_gate=start_gate,
        )
        self.runtimes.append(runtime)
        return runtime, repository, scheduler

    def test_enabled_auto_start_runs_due_adapter_in_two_cycles(self) -> None:
        adapter = FakeAdapter("runtime_two_cycles")
        runtime, repository, _scheduler = self.build_runtime(
            (adapter,),
            enable=(adapter.adapter_key,),
        )

        self.assertTrue(runtime.start())
        self.assertTrue(
            wait_until(
                lambda: repository.get_state(adapter.adapter_key)["checkpoint"]
                == {"cursor": 1}
            )
        )
        self.clock.advance(adapter.poll_interval_ms)
        self.assertTrue(
            wait_until(
                lambda: repository.get_state(adapter.adapter_key)["checkpoint"]
                == {"cursor": 2}
            )
        )
        self.assertTrue(runtime.stop())

        self.assertEqual(adapter.started_checkpoints[:2], [{}, {"cursor": 1}])

    def test_new_alphabetic_peer_becomes_due_after_selection(self) -> None:
        first = FakeAdapter("a_later_due")
        selected = FakeAdapter("b_selected_due")
        runtime, _repository, scheduler = self.build_runtime(
            (first, selected), enable=(first.adapter_key, selected.adapter_key),
        )
        with self.store._lock, closing(self.store._connect()) as connection, connection:
            connection.execute(
                "UPDATE source_adapter_states SET next_due_at_ms=? WHERE adapter_key=?",
                (self.clock() + 1, first.adapter_key),
            )
        original = scheduler.run_one_due
        cycles: list[dict[str, Any]] = []

        def advance_after_selection(adapter_key, **kwargs):
            self.clock.advance(1)
            cycle = original(adapter_key, **kwargs)
            cycles.append(cycle)
            return cycle

        scheduler.run_one_due = advance_after_selection
        self.assertTrue(runtime.start())
        self.assertTrue(wait_until(lambda: len(cycles) >= 2))
        self.assertTrue(runtime.stop())
        self.assertEqual(cycles[0]["results"][0]["adapter_key"], selected.adapter_key)
        self.assertEqual(cycles[1]["results"][0]["adapter_key"], first.adapter_key)
        self.assertEqual(runtime.snapshot()["last_fatal_error_code"], "")

    def test_state_version_changes_after_selection_are_skipped_then_reselected(self) -> None:
        adapter = FakeAdapter("selected_state_change")
        runtime, repository, scheduler = self.build_runtime(
            (adapter,), enable=(adapter.adapter_key,),
        )
        original = scheduler.run_one_due
        cycles: list[dict[str, Any]] = []

        def change_after_selection(adapter_key, **kwargs):
            if not cycles:
                repository.set_enabled(
                    adapter_key, config_version=adapter.config_version, enabled=False,
                )
                repository.set_enabled(
                    adapter_key, config_version=adapter.config_version, enabled=True,
                )
            cycle = original(adapter_key, **kwargs)
            cycles.append(cycle)
            return cycle

        scheduler.run_one_due = change_after_selection
        self.assertTrue(runtime.start())
        self.assertTrue(wait_until(lambda: bool(cycles)))
        self.assertTrue(wait_until(lambda: adapter.count() == 1))
        self.assertTrue(runtime.stop())
        self.assertEqual(cycles[0]["run_count"], 0)
        self.assertEqual(len(repository.list_runs(adapter_key=adapter.adapter_key)), 1)
        self.assertEqual(runtime.snapshot()["last_fatal_error_code"], "")

    def test_selection_is_checked_atomically_before_starting_a_request(self) -> None:
        for race in ("disable", "state_version", "config_version"):
            with self.subTest(race=race):
                selected = FakeAdapter(f"a_transaction_{race}")
                peer = FakeAdapter(f"b_transaction_{race}")
                runtime, repository, scheduler = self.build_runtime(
                    (selected, peer), enable=(selected.adapter_key, peer.adapter_key),
                )
                original = repository.start_run
                raced = False
                cycles: list[dict[str, Any]] = []
                original_execute = scheduler.run_one_due

                def change_before_transaction(adapter_key, **kwargs):
                    nonlocal raced
                    if adapter_key == selected.adapter_key and not raced:
                        raced = True
                        disabled = repository.set_enabled(
                            adapter_key, config_version=selected.config_version, enabled=False,
                        )
                        if race == "state_version":
                            repository.set_enabled(
                                adapter_key, config_version=selected.config_version, enabled=True,
                            )
                        elif race == "config_version":
                            repository.migrate_config(
                                adapter_key,
                                expected_config_version=selected.config_version,
                                new_config_version=f"{adapter_key}_config_v2",
                                expected_state_version=disabled["state_version"],
                                next_checkpoint={},
                            )
                    return original(adapter_key, **kwargs)

                def record_cycle(adapter_key, **kwargs):
                    cycle = original_execute(adapter_key, **kwargs)
                    cycles.append(cycle)
                    return cycle

                repository.start_run = change_before_transaction
                scheduler.run_one_due = record_cycle
                self.assertTrue(runtime.start())
                self.assertTrue(wait_until(lambda: bool(cycles)))
                self.assertTrue(wait_until(lambda: peer.count() == 1))
                self.assertTrue(runtime.stop())
                self.assertEqual(cycles[0]["run_count"], 0)
                self.assertEqual(runtime.snapshot()["last_fatal_error_code"], "")
                if race != "state_version":
                    self.assertEqual(selected.count(), 0)
                    self.assertEqual(repository.list_runs(adapter_key=selected.adapter_key), [])

    def test_corrupt_scheduler_result_identity_remains_fatal(self) -> None:
        adapter = FakeAdapter("selected_identity")
        runtime, _repository, scheduler = self.build_runtime(
            (adapter,), enable=(adapter.adapter_key,),
        )

        def corrupt_identity(adapter_key, **kwargs):
            return {
                "run_count": 1,
                "results": [{"adapter_key": "unselected_identity", "status": "SUCCEEDED"}],
            }

        scheduler.run_one_due = corrupt_identity
        self.assertTrue(runtime.start())
        self.assertTrue(wait_until(lambda: runtime.snapshot()["status"] == "failed"))
        self.assertEqual(adapter.count(), 0)
        self.assertEqual(
            runtime.snapshot()["last_fatal_error_code"],
            SOURCE_MONITORING_RUNTIME_FATAL,
        )

    def test_disabled_or_not_auto_started_creates_no_thread_and_never_polls(self) -> None:
        cases = (
            self.settings(enabled=True, auto_start=False),
            self.settings(enabled=False, auto_start=False),
        )
        for index, settings in enumerate(cases):
            with self.subTest(settings=settings):
                adapter = FakeAdapter(f"inert_runtime_{index}")
                runtime, _repository, _scheduler = self.build_runtime(
                    (adapter,),
                    settings=settings,
                )
                self.assertFalse(runtime.start())
                self.assertEqual(adapter.count(), 0)
                self.assertFalse(runtime.snapshot()["thread_alive"])
                self.assertIsNone(runtime._thread)

    def test_dry_run_ephemeral_due_prevents_busy_polling(self) -> None:
        adapter = FakeAdapter("dry_runtime")
        runtime, _repository, scheduler = self.build_runtime(
            (adapter,),
            settings=self.settings(dry_run=True),
            enable=(adapter.adapter_key,),
        )

        self.assertTrue(runtime.start())
        self.assertTrue(wait_until(lambda: adapter.count() == 1))
        time.sleep(0.08)

        self.assertEqual(adapter.count(), 1)
        self.assertEqual(
            scheduler.effective_next_due_at_ms(),
            self.clock() + adapter.poll_interval_ms,
        )

    def test_active_adapter_visible_during_blocking_poll_then_cleared(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        adapter = FakeAdapter(
            "blocking_runtime",
            entered=entered,
            release=release,
        )
        runtime, _repository, _scheduler = self.build_runtime(
            (adapter,),
            enable=(adapter.adapter_key,),
        )

        self.assertTrue(runtime.start())
        self.assertTrue(entered.wait(2))
        self.assertEqual(runtime.snapshot()["active_adapter"], adapter.adapter_key)
        release.set()
        self.assertTrue(
            wait_until(lambda: runtime.snapshot()["active_adapter"] == "")
        )

    def test_adapter_failure_degrades_runtime_but_next_adapter_still_runs(self) -> None:
        failing = FakeAdapter("a_runtime_failure", raises=True)
        succeeding = FakeAdapter("b_runtime_success")
        runtime, _repository, _scheduler = self.build_runtime(
            (failing, succeeding),
            enable=(failing.adapter_key, succeeding.adapter_key),
        )

        self.assertTrue(runtime.start())
        self.assertTrue(wait_until(lambda: failing.count() == 1))
        self.assertTrue(wait_until(lambda: succeeding.count() == 1))
        self.assertTrue(
            wait_until(
                lambda: runtime.snapshot()["status"] == "degraded"
                and runtime.snapshot()["active_adapter"] == ""
            )
        )

        snapshot = runtime.snapshot()
        self.assertEqual(snapshot["status"], "degraded")
        self.assertTrue(snapshot["liveness_verified"])

    def test_worker_fatal_error_is_projected_as_failed_and_not_propagated(self) -> None:
        runtime, _repository, scheduler = self.build_runtime(())

        def fail_due_selection() -> tuple[str, ...]:
            raise RuntimeError("secret fatal detail")

        scheduler.due_run_selections = fail_due_selection  # type: ignore[method-assign]
        self.assertTrue(runtime.start())
        self.assertTrue(
            wait_until(
                lambda: runtime.snapshot()["status"] == "failed"
                and not runtime.snapshot()["thread_alive"]
            )
        )

        snapshot = runtime.snapshot()
        self.assertFalse(snapshot["thread_alive"])
        self.assertEqual(
            snapshot["last_fatal_error_code"],
            SOURCE_MONITORING_RUNTIME_FATAL,
        )
        self.assertNotIn("secret", repr(snapshot))
        self.assertTrue(runtime.stop())

    def test_cycle_observer_receives_one_closed_safe_terminal_receipt(self) -> None:
        adapter = FakeAdapter("observed_runtime")
        observations: list[dict[str, Any]] = []
        runtime, repository, _scheduler = self.build_runtime(
            (adapter,),
            enable=(adapter.adapter_key,),
            heartbeat_interval_ms=60_000,
            cycle_observer=observations.append,
        )

        self.assertTrue(runtime.start())
        self.assertTrue(wait_until(lambda: len(observations) == 1))
        self.assertTrue(runtime.stop())

        observation = observations[0]
        self.assertEqual(
            set(observation),
            {
                "version",
                "runtime_id",
                "adapter_key",
                "run_id",
                "status",
                "state_recorded",
                "source_inbox_writes_performed",
                "market_calls_performed",
                "market_calls_possible_max",
                "provider_calls_performed",
                "formal_rounds_created",
                "execution_capability",
                "live_trading_allowed",
            },
        )
        self.assertEqual(
            observation["version"],
            SOURCE_MONITORING_RUNTIME_CYCLE_OBSERVATION_VERSION,
        )
        self.assertEqual(observation["adapter_key"], adapter.adapter_key)
        self.assertEqual(observation["status"], "SUCCEEDED")
        self.assertTrue(observation["state_recorded"])
        self.assertFalse(observation["source_inbox_writes_performed"])
        self.assertEqual(observation["market_calls_performed"], 0)
        self.assertEqual(observation["market_calls_possible_max"], 0)
        self.assertEqual(observation["provider_calls_performed"], 0)
        self.assertEqual(observation["formal_rounds_created"], 0)
        self.assertEqual(observation["execution_capability"], "none")
        self.assertFalse(observation["live_trading_allowed"])
        self.assertEqual(
            repository.get_run(observation["run_id"])["status"],
            "SUCCEEDED",
        )

    def test_cycle_observer_failure_is_fail_stop_and_redacted(self) -> None:
        adapter = FakeAdapter("observer_failure_runtime")
        calls: list[dict[str, Any]] = []

        def failing_observer(observation: dict[str, Any]) -> None:
            calls.append(observation)
            raise RuntimeError("SECRET_SOAK_LEDGER_PATH")

        runtime, repository, _scheduler = self.build_runtime(
            (adapter,),
            enable=(adapter.adapter_key,),
            cycle_observer=failing_observer,
        )
        self.assertTrue(runtime.start())
        self.assertTrue(
            wait_until(
                lambda: runtime.snapshot()["status"] == "failed"
                and not runtime.snapshot()["thread_alive"]
            )
        )

        snapshot = runtime.snapshot()
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            snapshot["last_fatal_error_code"],
            SOURCE_MONITORING_RUNTIME_OBSERVER_FAILED,
        )
        self.assertNotIn("SECRET", repr(snapshot))
        runs = repository.list_runs(adapter_key=adapter.adapter_key)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "SUCCEEDED")
        self.clock.advance(adapter.poll_interval_ms)
        time.sleep(0.02)
        self.assertEqual(adapter.count(), 1)

    def test_cycle_observer_must_be_callable(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "cycle_observer must be callable",
        ):
            self.build_runtime(
                (),
                cycle_observer="not-callable",  # type: ignore[arg-type]
            )

    def test_start_gate_blocks_first_scheduler_cycle_until_released(self) -> None:
        release = threading.Event()
        adapter = FakeAdapter("start_gate_runtime")

        def await_release() -> None:
            if not release.wait(2):
                raise RuntimeError("test start gate timed out")

        runtime, repository, _scheduler = self.build_runtime(
            (adapter,),
            enable=(adapter.adapter_key,),
            start_gate=await_release,
        )

        self.assertTrue(runtime.start())
        self.assertTrue(
            wait_until(lambda: runtime.snapshot()["status"] == "starting")
        )
        time.sleep(0.02)
        self.assertEqual(adapter.count(), 0)
        self.assertEqual(repository.list_runs(adapter_key=adapter.adapter_key), [])

        release.set()
        self.assertTrue(wait_until(lambda: adapter.count() == 1))

    def test_start_gate_must_be_callable(self) -> None:
        with self.assertRaisesRegex(ValueError, "start_gate must be callable"):
            self.build_runtime((), start_gate="not-callable")  # type: ignore[arg-type]

    def test_start_gate_failure_stops_before_any_adapter_run(self) -> None:
        adapter = FakeAdapter("failed_start_gate_runtime")

        def fail_gate() -> None:
            raise RuntimeError("secret gate detail")

        runtime, repository, _scheduler = self.build_runtime(
            (adapter,),
            enable=(adapter.adapter_key,),
            start_gate=fail_gate,
        )

        self.assertTrue(runtime.start())
        self.assertTrue(
            wait_until(
                lambda: runtime.snapshot()["status"] == "failed"
                and runtime.snapshot()["thread_alive"] is False
            )
        )
        self.assertEqual(adapter.count(), 0)
        self.assertEqual(repository.list_runs(adapter_key=adapter.adapter_key), [])
        self.assertEqual(
            runtime.snapshot()["last_fatal_error_code"],
            SOURCE_MONITORING_RUNTIME_START_GATE_FAILED,
        )
        self.assertNotIn("secret", repr(runtime.snapshot()))

    def test_start_gate_non_none_return_fails_closed(self) -> None:
        adapter = FakeAdapter("invalid_start_gate_result")
        runtime, repository, _scheduler = self.build_runtime(
            (adapter,),
            enable=(adapter.adapter_key,),
            start_gate=lambda: True,
        )

        self.assertTrue(runtime.start())
        self.assertTrue(
            wait_until(
                lambda: runtime.snapshot()["status"] == "failed"
                and runtime.snapshot()["thread_alive"] is False
            )
        )
        self.assertEqual(adapter.count(), 0)
        self.assertEqual(repository.list_runs(adapter_key=adapter.adapter_key), [])

    def test_stop_wakes_idle_event_wait_and_joins_worker(self) -> None:
        runtime, _repository, _scheduler = self.build_runtime(
            (),
            heartbeat_interval_ms=60_000,
        )
        self.assertTrue(runtime.start())
        self.assertTrue(
            wait_until(lambda: runtime.snapshot()["status"] == "running")
        )

        started = time.monotonic()
        self.assertTrue(runtime.stop())
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1)
        self.assertFalse(runtime.snapshot()["thread_alive"])
        self.assertEqual(runtime.snapshot()["status"], "stopped")

    def test_request_stop_never_waits_for_an_adapter_resource_lock(self) -> None:
        adapter = LockBlockedStopAdapter("runtime_locked_resource")
        runtime, _repository, _scheduler = self.build_runtime(
            (adapter,),
            enable=(adapter.adapter_key,),
            join_timeout_ms=500,
            poll_timeout_ms=500,
        )
        requester = threading.Thread(target=runtime.request_stop, daemon=False)
        try:
            self.assertTrue(runtime.start())
            self.assertTrue(adapter.poll_entered.wait(2))

            requester.start()
            requester.join(0.25)
            self.assertFalse(requester.is_alive())
            self.assertFalse(adapter.stop_attempted.is_set())

            self.assertFalse(runtime.stop())
            self.assertFalse(adapter.stop_attempted.is_set())
        finally:
            adapter.poll_release.set()
            if requester.ident is not None:
                requester.join(2)

        self.assertTrue(runtime.wait_until_stopped(2))
        self.assertTrue(runtime.stop())
        self.assertTrue(adapter.stop_attempted.is_set())
        self.assertEqual(adapter.stop_calls, 1)

    def test_resource_stop_failure_is_retried_after_worker_join(self) -> None:
        adapter = FakeAdapter("runtime_resource_retry")
        attempts: list[int] = []

        def stop_resource() -> bool:
            attempts.append(len(attempts) + 1)
            return len(attempts) > 1

        adapter.stop = stop_resource  # type: ignore[attr-defined]
        runtime, _repository, _scheduler = self.build_runtime((adapter,))

        self.assertFalse(runtime.stop())
        self.assertTrue(runtime.stop())
        self.assertEqual(attempts, [1, 2])

    def test_poll_deadline_uses_platform_clock_not_injected_liveness_clock(self) -> None:
        liveness_clock = MutableClock(1)
        runtime, _repository, _scheduler = self.build_runtime(
            (),
            poll_timeout_ms=1_000,
            monotonic_ms=liveness_clock,
        )

        before_ms = int(time.monotonic() * 1_000)
        deadline_ms = runtime._poll_deadline_monotonic_ms()
        after_ms = int(time.monotonic() * 1_000)

        self.assertGreaterEqual(deadline_ms, before_ms + 1_000)
        self.assertLessEqual(deadline_ms, after_ms + 1_000)
        self.assertNotEqual(deadline_ms, liveness_clock() + 1_000)

    def test_stall_boundary_is_strictly_greater_than_threshold(self) -> None:
        epoch = MutableClock()
        monotonic = MutableClock(10_000)
        state = SourceMonitoringRuntimeState(
            self.settings(),
            clock_ms=epoch,
            monotonic_ms=monotonic,
            stall_after_ms=120_000,
        )
        state.mark_starting("source_monitor_runtime_" + "a" * 32)
        state.mark_running()
        state.begin_loop()

        monotonic.advance(120_000)
        exact = state.snapshot(thread_alive=True)
        self.assertEqual(exact["heartbeat_age_ms"], 120_000)
        self.assertEqual(exact["status"], "running")
        self.assertTrue(exact["liveness_verified"])

        monotonic.advance(1)
        stale = state.snapshot(thread_alive=True)
        self.assertEqual(stale["heartbeat_age_ms"], 120_001)
        self.assertEqual(stale["status"], "stalled")
        self.assertFalse(stale["liveness_verified"])

    def test_start_stop_are_idempotent_and_only_one_worker_exists(self) -> None:
        runtime, _repository, _scheduler = self.build_runtime(())

        self.assertTrue(runtime.start())
        first_worker = runtime._thread
        self.assertIsNotNone(first_worker)
        self.assertFalse(runtime.start())
        self.assertIs(runtime._thread, first_worker)
        self.assertEqual(
            sum(
                thread.name == "source-monitoring-runtime"
                for thread in threading.enumerate()
            ),
            1,
        )
        self.assertTrue(runtime.stop())
        self.assertTrue(runtime.stop())

    def test_restart_restores_checkpoint_and_does_not_auto_enable_adapter(self) -> None:
        first = FakeAdapter("persisted_runtime")
        runtime_one, repository_one, _scheduler_one = self.build_runtime(
            (first,),
            enable=(first.adapter_key,),
        )
        self.assertTrue(runtime_one.start())
        self.assertTrue(wait_until(lambda: first.count() == 1))
        self.assertTrue(runtime_one.stop())
        self.assertEqual(
            repository_one.get_state(first.adapter_key)["checkpoint"],
            {"cursor": 1},
        )

        restarted = FakeAdapter("persisted_runtime")
        never_enabled = FakeAdapter("new_disabled_runtime")
        self.clock.advance(first.poll_interval_ms)
        runtime_two, repository_two, _scheduler_two = self.build_runtime(
            (restarted, never_enabled),
        )
        self.assertTrue(runtime_two.start())
        self.assertTrue(wait_until(lambda: restarted.count() == 1))
        self.assertTrue(runtime_two.stop())

        self.assertEqual(restarted.started_checkpoints, [{"cursor": 1}])
        self.assertEqual(never_enabled.count(), 0)
        self.assertIsNone(repository_two.get_state(never_enabled.adapter_key))


if __name__ == "__main__":
    unittest.main()
