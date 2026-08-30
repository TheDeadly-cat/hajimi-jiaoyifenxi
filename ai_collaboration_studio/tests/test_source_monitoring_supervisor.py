from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.source_inbox_contracts import PROJECT_SOURCE_ITEM_VERSION  # noqa: E402
from backend.source_inbox_service import SourceInboxService  # noqa: E402
from backend.source_monitoring.adapters.base import (  # noqa: E402
    SOURCE_ADAPTER_CONTRACT_VERSION,
)
from backend.source_monitoring.contracts import (  # noqa: E402
    AdapterPollResult,
    SourcePollError,
)
from backend.source_monitoring.registry import SourceAdapterRegistry  # noqa: E402
from backend.source_monitoring.scheduler import (  # noqa: E402
    BackoffPolicy,
    SourceMonitoringScheduler,
)
from backend.source_monitoring.settings import SourceMonitoringSettings  # noqa: E402
from backend.source_monitoring.state_repository import (  # noqa: E402
    RUN_STATUS_ABANDONED,
    RUN_STATUS_FAILED,
    SourceMonitoringStateRepository,
)
from backend.source_monitoring.supervisor import (  # noqa: E402
    SourceMonitoringSupervisor,
    SourceMonitoringSupervisorError,
)
from backend.store import StudioStore  # noqa: E402


CAPTURED_AT_MS = int(
    datetime(2026, 8, 31, 4, 0, tzinfo=timezone.utc).timestamp() * 1_000
)


def _item(adapter_key: str, index: int) -> dict[str, object]:
    timestamp = "2026-08-31T03:55:00Z"
    return {
        "version": PROJECT_SOURCE_ITEM_VERSION,
        "external_item_id": f"{adapter_key}-{index}",
        "item_type": "sec_filing",
        "severity": "info",
        "occurred_at": timestamp,
        "published_at": timestamp,
        "entities": [
            {"kind": "security", "id": "US.MU", "label": "MU"},
        ],
        "headline": f"Official fixture {adapter_key} event {index}",
        "summary": "A deterministic official-source fixture event.",
        "facts": [
            {
                "claim": "The fixed official fixture contains this event.",
                "source_indexes": [0],
            }
        ],
        "sources": [
            {
                "url": (
                    "https://www.sec.gov/Archives/edgar/data/723125/"
                    f"{adapter_key}{index}.htm"
                ),
                "publisher": "U.S. SEC",
                "source_type": "official_filing",
                "published_at": timestamp,
                "content_sha256": "",
            }
        ],
        "impact_hypotheses": [],
        "unknowns": ["No model interpretation was performed."],
        "confidence": 1.0,
        "recommended_route": "notify_only",
        "extensions": {"fixture_v1": {"sequence": index}},
    }


class FakeAdapter:
    contract_version = SOURCE_ADAPTER_CONTRACT_VERSION
    official_source = True
    execution_capability = "none"
    live_trading_allowed = False
    poll_interval_ms = 60_000

    def __init__(self, adapter_key: str, *, mode: str = "normal") -> None:
        self.adapter_key = adapter_key
        self.config_version = f"{adapter_key}_config_v1"
        self.mode = mode
        self.poll_count = 0
        self.response_etag = '"fixture-v1"'
        self.response_last_modified = "Sun, 31 Aug 2026 03:55:00 GMT"
        self.last_poll_context: dict[str, object] = {}

    def poll(
        self,
        checkpoint: dict[str, object],
        *,
        observed_at_ms: int,
        etag: str = "",
        last_modified: str = "",
        max_items: int = 50,
    ) -> AdapterPollResult:
        self.poll_count += 1
        self.last_poll_context = {
            "etag": etag,
            "last_modified": last_modified,
            "max_items": max_items,
        }
        if self.mode == "raise":
            raise RuntimeError("fixture source unavailable")
        cursor = checkpoint.get("cursor", 0)
        next_cursor = cursor + 1 if type(cursor) is int else 1
        items = [_item(self.adapter_key, 1), _item(self.adapter_key, 2)][:max_items]
        if self.mode == "invalid_item":
            items[0]["unexpected_field"] = True
        errors = (
            (SourcePollError.build(
                "FIXTURE_PARTIAL_FAILURE",
                "One fixed source failed while other sources succeeded.",
                self.adapter_key,
            ),)
            if self.mode == "degraded"
            else ()
        )
        return AdapterPollResult.build(
            adapter_key=self.adapter_key,
            started_checkpoint=checkpoint,
            next_checkpoint={"cursor": next_cursor},
            observed_items=items,
            source_errors=errors,
            retry_after_ms=45_000 if errors else 0,
            captured_at_ms=observed_at_ms,
            etag=self.response_etag,
            last_modified=self.response_last_modified,
            rejected_count=1 if self.mode == "rejected" else 0,
        )


class SourceMonitoringSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="ai-studio-monitor-supervisor-")
        self.database_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.clock = [CAPTURED_AT_MS]
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
            initial_delay_ms=30_000,
            maximum_delay_ms=120_000,
            jitter_ratio=0.2,
            random_source=lambda: 0.5,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def settings(*, dry_run: bool = False, auto_start: bool = False) -> SourceMonitoringSettings:
        return SourceMonitoringSettings(
            enabled=True,
            auto_start=auto_start,
            official_only=True,
            dry_run=dry_run,
            max_items_per_run=50,
        )

    def supervisor(
        self,
        adapters: tuple[FakeAdapter, ...],
        *,
        dry_run: bool = False,
        auto_start: bool = False,
        after_import_hook=None,
    ) -> SourceMonitoringSupervisor:
        return SourceMonitoringSupervisor(
            registry=SourceAdapterRegistry(adapters),
            repository=self.repository,
            source_inbox=self.inbox,
            settings=self.settings(dry_run=dry_run, auto_start=auto_start),
            backoff_policy=self.backoff,
            clock_ms=lambda: self.clock[0],
            after_import_hook=after_import_hook,
        )

    def enable(self, adapter: FakeAdapter) -> None:
        self.repository.set_enabled(
            adapter.adapter_key,
            config_version=adapter.config_version,
            enabled=True,
        )

    def provider_counts(self) -> tuple[int, int]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            return (
                connection.execute(
                    "SELECT COUNT(*) FROM provider_execution_runs"
                ).fetchone()[0],
                connection.execute(
                    "SELECT COUNT(*) FROM provider_call_attempts"
                ).fetchone()[0],
            )

    def inbox_counts(self) -> tuple[int, int]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            return (
                connection.execute(
                    "SELECT COUNT(*) FROM source_inbox_imports"
                ).fetchone()[0],
                connection.execute(
                    "SELECT COUNT(*) FROM source_inbox_items"
                ).fetchone()[0],
            )

    def test_two_items_import_once_replay_duplicate_and_provider_ledger_unchanged(self) -> None:
        adapter = FakeAdapter("fake_official")
        supervisor = self.supervisor((adapter,))
        self.enable(adapter)
        before_provider = self.provider_counts()

        first = supervisor.run_once(adapter.adapter_key)
        self.clock[0] += 60_000
        second = supervisor.run_once(adapter.adapter_key)

        self.assertEqual(first["status"], "SUCCEEDED")
        self.assertEqual(first["import"]["created_item_count"], 2)
        self.assertEqual(first["state"]["checkpoint"], {"cursor": 1})
        self.assertEqual(second["status"], "SUCCEEDED")
        self.assertEqual(second["import"]["created_item_count"], 0)
        self.assertEqual(second["import"]["duplicate_item_count"], 2)
        self.assertEqual(second["run"]["duplicate_count"], 2)
        self.assertEqual(second["state"]["checkpoint"], {"cursor": 2})
        self.assertEqual(adapter.last_poll_context["etag"], '"fixture-v1"')
        self.assertEqual(
            adapter.last_poll_context["last_modified"],
            "Sun, 31 Aug 2026 03:55:00 GMT",
        )
        self.assertEqual(adapter.last_poll_context["max_items"], 50)
        self.assertEqual(self.inbox_counts(), (2, 2))
        self.assertEqual(self.provider_counts(), before_provider)
        self.assertEqual(first["safety"]["provider_calls_performed"], 0)
        self.assertEqual(first["safety"]["formal_rounds_created"], 0)

    def test_adapter_exception_and_invalid_packet_preserve_checkpoint(self) -> None:
        for mode in ("raise", "invalid_item"):
            with self.subTest(mode=mode):
                adapter = FakeAdapter(f"fake_{mode}", mode=mode)
                supervisor = self.supervisor((adapter,))
                self.enable(adapter)
                result = supervisor.run_once(adapter.adapter_key)
                state = self.repository.get_state(adapter.adapter_key)
                self.assertEqual(result["status"], RUN_STATUS_FAILED)
                self.assertTrue(result["state_recorded"])
                self.assertEqual(state["checkpoint"], {})
                self.assertEqual(state["consecutive_failures"], 1)
                self.assertEqual(state["next_due_at_ms"], self.clock[0] + 30_000)
        room = self.store.create_room(
            "Worker isolation room",
            "The main store remains usable after monitoring failures.",
            capability_pack_ids=[],
        )["room"]
        self.assertTrue(room["id"])

    def test_dry_run_validates_candidates_without_inbox_or_operational_checkpoint(self) -> None:
        adapter = FakeAdapter("fake_dry")
        supervisor = self.supervisor((adapter,), dry_run=True)
        self.enable(adapter)
        state_before = self.repository.get_state(adapter.adapter_key)
        result = supervisor.run_once(adapter.adapter_key)

        self.assertEqual(result["status"], "DRY_RUN")
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["run"]["next_checkpoint"], {"cursor": 1})
        self.assertEqual(result["state"]["checkpoint"], {})
        self.assertEqual(result["state"]["last_success_at_ms"], 0)
        self.assertEqual(result["state"], state_before)
        self.assertEqual(self.inbox_counts(), (0, 0))
        self.assertEqual(
            result["state"]["next_due_at_ms"],
            state_before["next_due_at_ms"],
        )

    def test_dry_run_failures_create_only_terminal_run_receipts(self) -> None:
        for mode in ("raise", "invalid_item"):
            with self.subTest(mode=mode):
                adapter = FakeAdapter(f"fake_dry_{mode}", mode=mode)
                supervisor = self.supervisor((adapter,), dry_run=True)
                self.enable(adapter)
                state_before = self.repository.get_state(adapter.adapter_key)
                result = supervisor.run_once(adapter.adapter_key)
                state_after = self.repository.get_state(adapter.adapter_key)
                self.assertEqual(result["status"], "DRY_RUN_FAILED")
                self.assertTrue(result["state_recorded"])
                self.assertEqual(result["run"]["status"], "DRY_RUN")
                self.assertTrue(result["run"]["dry_run"])
                self.assertTrue(result["run"]["error_code"])
                self.assertEqual(state_after, state_before)
        self.assertEqual(self.inbox_counts(), (0, 0))

    def test_degraded_partial_import_keeps_checkpoint_until_clean_replay(self) -> None:
        adapter = FakeAdapter("fake_degraded", mode="degraded")
        supervisor = self.supervisor((adapter,))
        self.enable(adapter)

        degraded = supervisor.run_once(adapter.adapter_key)
        self.assertEqual(degraded["status"], "DEGRADED")
        self.assertEqual(degraded["import"]["created_item_count"], 2)
        self.assertEqual(degraded["state"]["checkpoint"], {})
        self.assertEqual(degraded["state"]["consecutive_failures"], 1)

        adapter.mode = "normal"
        self.clock[0] += 45_000
        recovered = supervisor.run_once(adapter.adapter_key)
        self.assertEqual(recovered["status"], "SUCCEEDED")
        self.assertEqual(recovered["import"]["duplicate_item_count"], 2)
        self.assertEqual(recovered["state"]["checkpoint"], {"cursor": 1})
        self.assertEqual(recovered["state"]["consecutive_failures"], 0)
        self.assertEqual(self.inbox_counts()[1], 2)

        rejected = FakeAdapter("fake_rejected", mode="rejected")
        rejected_supervisor = self.supervisor((rejected,))
        self.enable(rejected)
        rejected_result = rejected_supervisor.run_once(rejected.adapter_key)
        self.assertEqual(rejected_result["status"], "DEGRADED")
        self.assertEqual(rejected_result["run"]["rejected_count"], 1)
        self.assertEqual(rejected_result["state"]["checkpoint"], {})

    def test_crash_after_import_is_recovered_and_replayed_at_least_once(self) -> None:
        adapter = FakeAdapter("fake_crash")

        def crash_after_import(_run_id: str, _result: dict) -> None:
            raise SystemExit("simulated process crash")

        first_supervisor = self.supervisor(
            (adapter,),
            after_import_hook=crash_after_import,
        )
        self.enable(adapter)
        with self.assertRaises(SystemExit):
            first_supervisor.run_once(adapter.adapter_key)
        active = self.repository.list_runs(adapter_key=adapter.adapter_key)
        self.assertEqual(active[0]["status"], "RUNNING")
        self.assertEqual(self.repository.get_state(adapter.adapter_key)["checkpoint"], {})
        self.assertEqual(self.inbox_counts(), (1, 2))

        restarted = self.supervisor((adapter,))
        self.clock[0] += 1_000
        replay = restarted.run_once(adapter.adapter_key)
        runs = self.repository.list_runs(adapter_key=adapter.adapter_key)
        self.assertEqual(replay["status"], "SUCCEEDED")
        self.assertEqual(replay["import"]["duplicate_item_count"], 2)
        self.assertEqual(replay["state"]["checkpoint"], {"cursor": 1})
        self.assertIn(RUN_STATUS_ABANDONED, {run["status"] for run in runs})
        self.assertEqual(self.inbox_counts(), (2, 2))

    def test_scheduler_isolates_adapters_and_respects_auto_start(self) -> None:
        healthy = FakeAdapter("fake_a_healthy")
        failing = FakeAdapter("fake_b_failing", mode="raise")
        supervisor = self.supervisor(
            (healthy, failing),
            auto_start=True,
        )
        self.enable(healthy)
        self.enable(failing)
        scheduler = SourceMonitoringScheduler(
            registry=supervisor.registry,
            repository=self.repository,
            supervisor=supervisor,
            clock_ms=lambda: self.clock[0],
        )

        cycle = scheduler.run_due()
        statuses = {result["adapter_key"]: result["status"] for result in cycle["results"]}
        self.assertEqual(statuses[healthy.adapter_key], "SUCCEEDED")
        self.assertEqual(statuses[failing.adapter_key], "FAILED")
        self.assertEqual(cycle["run_count"], 2)
        self.assertEqual(self.inbox_counts()[1], 2)

        inert_supervisor = self.supervisor((healthy,), auto_start=False)
        inert_scheduler = SourceMonitoringScheduler(
            registry=inert_supervisor.registry,
            repository=self.repository,
            supervisor=inert_supervisor,
            clock_ms=lambda: self.clock[0],
        )
        self.assertEqual(inert_scheduler.run_due()["run_count"], 0)

    def test_scheduler_uses_ephemeral_due_time_for_dry_run(self) -> None:
        adapter = FakeAdapter("fake_dry_schedule")
        supervisor = self.supervisor(
            (adapter,),
            dry_run=True,
            auto_start=True,
        )
        self.enable(adapter)
        scheduler = SourceMonitoringScheduler(
            registry=supervisor.registry,
            repository=self.repository,
            supervisor=supervisor,
            clock_ms=lambda: self.clock[0],
        )
        self.assertEqual(scheduler.run_due()["run_count"], 1)
        self.assertEqual(scheduler.run_due()["run_count"], 0)
        self.clock[0] += adapter.poll_interval_ms
        self.assertEqual(scheduler.run_due()["run_count"], 1)

    def test_failure_recording_fallback_prevents_permanent_running_rows(self) -> None:
        adapter = FakeAdapter("fake_recording_fallback", mode="raise")
        bad_backoff = BackoffPolicy(
            initial_delay_ms=30_000,
            maximum_delay_ms=120_000,
            jitter_ratio=0.2,
            random_source=lambda: "invalid-random",
        )
        supervisor = SourceMonitoringSupervisor(
            registry=SourceAdapterRegistry((adapter,)),
            repository=self.repository,
            source_inbox=self.inbox,
            settings=self.settings(),
            backoff_policy=bad_backoff,
            clock_ms=lambda: self.clock[0],
        )
        self.enable(adapter)
        first = supervisor.run_once(adapter.adapter_key)
        self.assertEqual(first["status"], "FAILED")
        self.assertTrue(first["state_recorded"])
        self.assertEqual(first["state"]["next_due_at_ms"], self.clock[0] + 120_000)

        original_fail_run = self.repository.fail_run

        def fail_recording_once(*args, **kwargs):
            raise RuntimeError("simulated run receipt write failure")

        self.repository.fail_run = fail_recording_once
        adapter.mode = "raise"
        second = supervisor.run_once(adapter.adapter_key)
        self.repository.fail_run = original_fail_run
        self.assertEqual(second["status"], "FAILED")
        self.assertTrue(second["state_recorded"])
        self.assertEqual(second["run"]["status"], RUN_STATUS_ABANDONED)

        adapter.mode = "normal"
        third = supervisor.run_once(adapter.adapter_key)
        self.assertEqual(third["status"], "SUCCEEDED")
        self.assertFalse(any(
            run["status"] == "RUNNING"
            for run in self.repository.list_runs(adapter_key=adapter.adapter_key)
        ))

    def test_global_disable_fails_before_state_creation_or_recovery(self) -> None:
        adapter = FakeAdapter("fake_disabled")
        supervisor = SourceMonitoringSupervisor(
            registry=SourceAdapterRegistry((adapter,)),
            repository=self.repository,
            source_inbox=self.inbox,
            settings=SourceMonitoringSettings(),
            backoff_policy=self.backoff,
            clock_ms=lambda: self.clock[0],
        )
        with self.assertRaises(SourceMonitoringSupervisorError) as captured:
            supervisor.run_once(adapter.adapter_key)
        self.assertEqual(captured.exception.code, "SOURCE_MONITORING_DISABLED")
        self.assertIsNone(self.repository.get_state(adapter.adapter_key))


class SourceMonitoringBackoffTests(unittest.TestCase):
    def test_backoff_is_exponential_capped_retry_aware_and_deterministic(self) -> None:
        policy = BackoffPolicy(
            initial_delay_ms=10_000,
            maximum_delay_ms=40_000,
            jitter_ratio=0.2,
            random_source=lambda: 0.5,
        )
        self.assertEqual(policy.delay_ms(1), 10_000)
        self.assertEqual(policy.delay_ms(2), 20_000)
        self.assertEqual(policy.delay_ms(3), 40_000)
        self.assertEqual(policy.delay_ms(9), 40_000)
        self.assertEqual(policy.delay_ms(1, retry_after_ms=35_000), 35_000)
        self.assertEqual(policy.delay_ms(1, retry_after_ms=90_000), 90_000)
        self.assertEqual(policy.failure_due_at_ms(1_000, 2), 21_000)


if __name__ == "__main__":
    unittest.main()
