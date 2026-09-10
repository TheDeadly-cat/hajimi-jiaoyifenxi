from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.source_inbox_contracts import PROJECT_SOURCE_ITEM_VERSION  # noqa: E402
from backend.source_inbox_service import SourceInboxService  # noqa: E402
from backend.source_monitoring.adapters.base import (  # noqa: E402
    SOURCE_ADAPTER_CONTRACT_VERSION,
    SourceAdapterMetadata,
)
from backend.source_monitoring.contracts import (  # noqa: E402
    FUTU_ANOMALY_SOURCE_CHANNEL,
    OFFICIAL_SOURCE_CHANNEL,
    OFFICIAL_SOURCE_CLASS,
    READONLY_MARKET_SOURCE_CLASS,
    AdapterPollResult,
    SourcePollError,
)
from backend.source_monitoring.initialization import (  # noqa: E402
    SourceMonitoringInitializationError,
    build_static_seed_preview,
    plan_initial_poll,
    require_catch_up_confirmation_before_poll,
    require_initial_preview_match,
)
from backend.source_monitoring.registry import SourceAdapterRegistry  # noqa: E402
from backend.source_monitoring.state_repository import (  # noqa: E402
    SourceMonitoringStateRepository,
)
from backend.source_monitoring.supervisor import SourceMonitoringSupervisor  # noqa: E402
from backend.source_monitoring.supervisor import SourceMonitoringSupervisorError  # noqa: E402
from backend.source_monitoring.settings import (  # noqa: E402
    SOURCE_MONITOR_CATCH_UP_MAX_ITEMS_ENV,
    SOURCE_MONITOR_CONTINUOUS_EVENT_CUTOFF_ENV,
    SOURCE_MONITOR_FROM_TIME_ENV,
    SOURCE_MONITOR_INITIAL_MODE_ENV,
    SOURCE_MONITOR_INITIAL_PREVIEW_SHA256_ENV,
    SourceMonitoringSettings,
    SourceMonitoringSettingsError,
)
from backend.store import StudioStore  # noqa: E402


CAPTURED_AT_MS = 1_788_233_400_000


def _item(index: int, occurred_at: str) -> dict[str, object]:
    return {
        "version": PROJECT_SOURCE_ITEM_VERSION,
        "external_item_id": f"initial-{index}",
        "item_type": "sec_filing",
        "severity": "info",
        "occurred_at": occurred_at,
        "published_at": occurred_at,
        "entities": [{"kind": "security", "id": "US.MU", "label": "MU"}],
        "headline": f"Initial fixture {index}",
        "summary": "Deterministic initial-mode fixture.",
        "facts": [{"claim": "Fixture fact.", "source_indexes": [0]}],
        "sources": [
            {
                "url": f"https://www.sec.gov/Archives/fixture/{index}.htm",
                "publisher": "U.S. SEC",
                "source_type": "official_filing",
                "published_at": occurred_at,
                "content_sha256": "",
            }
        ],
        "impact_hypotheses": [],
        "unknowns": ["No model interpretation was performed."],
        "confidence": 1.0,
        "recommended_route": "notify_only",
        "extensions": {"fixture_v1": {"index": index}},
    }


def _metadata(*, official: bool = True) -> SourceAdapterMetadata:
    return SourceAdapterMetadata(
        contract_version=SOURCE_ADAPTER_CONTRACT_VERSION,
        adapter_key="fixture_initial",
        config_version="fixture_initial_v1",
        poll_interval_ms=60_000,
        max_candidates_per_poll=3,
        official_source=official,
        source_class=(OFFICIAL_SOURCE_CLASS if official else READONLY_MARKET_SOURCE_CLASS),
        source_channel=(OFFICIAL_SOURCE_CHANNEL if official else FUTU_ANOMALY_SOURCE_CHANNEL),
        max_market_calls_per_poll=0 if official else 1,
        execution_capability="none",
        live_trading_allowed=False,
    )


def _result(
    items: list[dict[str, object]],
    *,
    source_errors: tuple[SourcePollError, ...] = (),
    rejected_count: int = 0,
) -> AdapterPollResult:
    return AdapterPollResult.build(
        "fixture_initial",
        {},
        {"cursor": 3},
        items,
        source_errors,
        captured_at_ms=CAPTURED_AT_MS,
        rejected_count=rejected_count,
    )


class SourceMonitoringInitialSettingsTests(unittest.TestCase):
    def test_seed_only_defaults_and_old_positional_layout_remain_stable(self) -> None:
        settings = SourceMonitoringSettings(False, False, True, False, True, 7)

        self.assertEqual(settings.initial_mode, "seed_only")
        self.assertEqual(settings.catch_up_max_items, 0)
        self.assertEqual(settings.initial_preview_sha256, "")
        self.assertEqual(settings.from_time, "")
        self.assertEqual(settings.max_items_per_run, 7)

    def test_catch_up_requires_bounded_explicit_maximum(self) -> None:
        with self.assertRaises(SourceMonitoringSettingsError):
            SourceMonitoringSettings.from_environment({
                SOURCE_MONITOR_INITIAL_MODE_ENV: "catch_up",
            })
        settings = SourceMonitoringSettings.from_environment({
            SOURCE_MONITOR_INITIAL_MODE_ENV: "catch_up",
            SOURCE_MONITOR_CATCH_UP_MAX_ITEMS_ENV: "3",
        })
        self.assertEqual(settings.initial_mode, "catch_up")
        self.assertEqual(settings.catch_up_max_items, 3)

    def test_from_time_requires_timezone_and_normalizes_to_utc_milliseconds(self) -> None:
        settings = SourceMonitoringSettings.from_environment({
            SOURCE_MONITOR_INITIAL_MODE_ENV: "from_time",
            SOURCE_MONITOR_FROM_TIME_ENV: "2026-09-01T12:00:00.123000+08:00",
        })
        self.assertEqual(settings.from_time, "2026-09-01T04:00:00.123Z")
        self.assertEqual(settings.from_time_ms, 1_788_235_200_123)

        for invalid in (
            "2026-09-01T04:00:00",
            "2026-09-01T04:00:00.123001Z",
            "2026-02-30T04:00:00Z",
            "1969-12-31T23:59:59Z",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(
                SourceMonitoringSettingsError
            ):
                SourceMonitoringSettings.from_environment({
                    SOURCE_MONITOR_INITIAL_MODE_ENV: "from_time",
                    SOURCE_MONITOR_FROM_TIME_ENV: invalid,
                })

    def test_mode_specific_fields_fail_closed_outside_their_mode(self) -> None:
        for extra in (
            {SOURCE_MONITOR_CATCH_UP_MAX_ITEMS_ENV: "1"},
            {SOURCE_MONITOR_INITIAL_PREVIEW_SHA256_ENV: "0" * 64},
            {SOURCE_MONITOR_FROM_TIME_ENV: "2026-09-01T04:00:00Z"},
        ):
            with self.subTest(extra=extra), self.assertRaises(
                SourceMonitoringSettingsError
            ):
                SourceMonitoringSettings.from_environment(extra)

    def test_dual_source_mode_uses_configured_official_and_forced_market_seed(self) -> None:
        settings = SourceMonitoringSettings(
            official_only=True,
            allow_readonly_market=True,
            initial_mode="catch_up",
            catch_up_max_items=3,
            continuous_event_cutoff="2026-08-31T04:00:00Z",
        )

        official = settings.initialization_policy_for(official_source=True)
        market = settings.initialization_policy_for(official_source=False)

        self.assertEqual(official.mode, "catch_up")
        self.assertEqual(official.catch_up_max_items, 3)
        self.assertEqual(market.mode, "seed_only")
        self.assertEqual(market.catch_up_max_items, 0)
        self.assertEqual(market.initial_from_time, "")
        self.assertEqual(
            market.continuous_event_cutoff,
            "2026-08-31T04:00:00Z",
        )
        with self.assertRaises(SourceMonitoringSettingsError):
            SourceMonitoringSettings(official_only=False, allow_readonly_market=False)

    def test_continuous_event_cutoff_environment_is_explicit_and_canonical(self) -> None:
        settings = SourceMonitoringSettings.from_environment({
            SOURCE_MONITOR_CONTINUOUS_EVENT_CUTOFF_ENV:
                "2026-09-01T12:00:00.123000+08:00",
        })
        self.assertEqual(
            settings.continuous_event_cutoff,
            "2026-09-01T04:00:00.123Z",
        )


class SourceMonitoringInitialPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.items = [
            _item(1, "2026-08-31T03:00:00Z"),
            _item(2, "2026-08-31T04:00:00Z"),
            _item(3, "2026-08-31T05:00:00Z"),
        ]

    def plan(
        self,
        settings: SourceMonitoringSettings,
        *,
        result: AdapterPollResult | None = None,
        metadata: SourceAdapterMetadata | None = None,
        initial_required: bool = True,
        received_at_ms: int = CAPTURED_AT_MS,
    ):
        return plan_initial_poll(
            result or _result(self.items),
            metadata=metadata or _metadata(),
            settings=settings,
            initial_required=initial_required,
            received_at_ms=received_at_ms,
        )

    def test_seed_only_validates_every_item_but_selects_none_and_seals_receipt(self) -> None:
        plan = self.plan(SourceMonitoringSettings())

        self.assertEqual(plan.selected_items, ())
        self.assertEqual(plan.preview["candidate_count"], 3)
        self.assertEqual(plan.preview["selected_count"], 0)
        self.assertEqual(plan.preview["skipped_count"], 3)
        self.assertEqual(len(plan.preview["preview_sha256"]), 64)
        receipt = plan.initialization_receipt()
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["mode"], "seed_only")
        self.assertNotIn("candidate_fingerprints", receipt)
        self.assertNotIn("selected_fingerprints", receipt)

    def test_catch_up_selects_newest_bounded_items_and_binds_confirmation(self) -> None:
        preview_settings = SourceMonitoringSettings(
            initial_mode="catch_up",
            catch_up_max_items=2,
        )
        plan = self.plan(preview_settings)
        self.assertEqual(
            [item["external_item_id"] for item in plan.selected_items],
            ["initial-3", "initial-2"],
        )
        with self.assertRaises(SourceMonitoringInitializationError):
            require_catch_up_confirmation_before_poll(
                preview_settings,
                initial_required=True,
            )
        confirmed = SourceMonitoringSettings(
            initial_mode="catch_up",
            catch_up_max_items=2,
            initial_preview_sha256=plan.preview["preview_sha256"],
        )
        require_catch_up_confirmation_before_poll(confirmed, initial_required=True)
        require_initial_preview_match(plan, confirmed)
        with self.assertRaises(SourceMonitoringInitializationError):
            require_initial_preview_match(
                plan,
                SourceMonitoringSettings(
                    initial_mode="catch_up",
                    catch_up_max_items=2,
                    initial_preview_sha256="0" * 64,
                ),
            )

        later_capture = AdapterPollResult.build(
            "fixture_initial",
            {},
            {"cursor": 3},
            self.items,
            captured_at_ms=CAPTURED_AT_MS + 30_000,
        )
        stable = self.plan(
            preview_settings,
            result=later_capture,
            received_at_ms=CAPTURED_AT_MS + 30_000,
        )
        self.assertEqual(
            stable.preview["preview_sha256"],
            plan.preview["preview_sha256"],
        )

    def test_from_time_is_initial_only_and_continuous_cutoff_is_explicit(self) -> None:
        settings = SourceMonitoringSettings(
            initial_mode="from_time",
            from_time="2026-08-31T04:00:00Z",
        )
        initial = self.plan(settings)
        later = self.plan(settings, initial_required=False)

        self.assertEqual(
            [item["external_item_id"] for item in initial.selected_items],
            ["initial-2", "initial-3"],
        )
        self.assertEqual(
            [item["external_item_id"] for item in later.selected_items],
            ["initial-1", "initial-2", "initial-3"],
        )

        continuous = self.plan(
            SourceMonitoringSettings(
                initial_mode="from_time",
                from_time="2026-08-31T04:00:00Z",
                continuous_event_cutoff="2026-08-31T04:00:00Z",
            ),
            initial_required=False,
        )
        self.assertEqual(
            [item["external_item_id"] for item in continuous.selected_items],
            ["initial-2", "initial-3"],
        )

    def test_revision_uses_poll_observation_for_initial_and_continuous_cutoffs(self) -> None:
        revision = _item(1, "2026-08-31T03:00:00Z")
        revision["extensions"] = {
            "macro_official_v1": {"event_state": "revised"},
        }
        settings = SourceMonitoringSettings(
            initial_mode="from_time",
            from_time="2026-08-31T04:00:00Z",
            continuous_event_cutoff="2026-08-31T04:00:00Z",
        )

        initial = self.plan(settings, result=_result([revision]))
        continuous = self.plan(
            settings,
            result=_result([revision]),
            initial_required=False,
        )

        self.assertEqual(len(initial.selected_items), 1)
        self.assertEqual(len(continuous.selected_items), 1)
        self.assertEqual(
            initial.selected_items[0]["occurred_at"],
            "2026-08-31T03:00:00Z",
        )

    def test_json_ir_revision_uses_observation_cutoff_without_changing_published_time(self) -> None:
        revision = _item(1, "2026-08-31T03:00:00Z")
        revision["extensions"] = {"company_ir_v2": {"is_revision": True}}
        settings = SourceMonitoringSettings(initial_mode="from_time", from_time="2026-08-31T04:00:00Z",
                                            continuous_event_cutoff="2026-08-31T04:00:00Z")
        for initial_required in (True, False):
            plan = self.plan(settings, result=_result([revision]), initial_required=initial_required)
            self.assertEqual(len(plan.selected_items), 1)
            self.assertEqual(plan.selected_items[0]["occurred_at"], "2026-08-31T03:00:00Z")

    def test_static_market_seed_preview_contains_no_snapshot_or_next_checkpoint(self) -> None:
        metadata = _metadata(official=False)
        policy = SourceMonitoringSettings(
            official_only=True,
            allow_readonly_market=True,
        ).initialization_policy_for(official_source=False)
        seed_policy = {
            "version": "source_monitoring_initial_seed_policy_v1",
            "initial_mode": "seed_only",
            "symbol_allowlist": ["US.MU", "US.SNDK", "US.WDC", "US.STX"],
            "execution_capability": "none",
            "live_trading_allowed": False,
            "adapter_key": metadata.adapter_key,
            "config_version": metadata.config_version,
            "adapter_config_sha256": "a" * 64,
            "broker_policy_sha256": "b" * 64,
        }
        from backend.source_monitoring.contracts import canonical_sha256

        seed_policy["source_policy_sha256"] = canonical_sha256(seed_policy)
        preview = build_static_seed_preview(
            metadata=metadata,
            initialization_policy=policy,
            initial_seed_policy=seed_policy,
            starting_checkpoint={},
        )

        self.assertEqual(preview["preview_kind"], "static_seed_policy")
        self.assertEqual(preview["source_policy_sha256"], seed_policy["source_policy_sha256"])
        self.assertNotIn("captured_at_ms", preview)
        self.assertNotIn("next_checkpoint_sha256", preview)

    def test_partial_or_rejected_first_poll_cannot_initialize_or_import(self) -> None:
        result = _result(
            self.items,
            source_errors=(
                SourcePollError.build("FIXTURE_PARTIAL", "partial", "fixture_initial"),
            ),
            rejected_count=1,
        )
        plan = self.plan(SourceMonitoringSettings(), result=result)

        self.assertTrue(plan.initialization_blocked)
        self.assertEqual(plan.selected_items, ())
        self.assertIsNone(plan.initialization_receipt())

    def test_readonly_market_only_allows_seed_initialization(self) -> None:
        settings = SourceMonitoringSettings(
            official_only=False,
            allow_readonly_market=True,
            initial_mode="from_time",
            from_time="2026-08-31T00:00:00Z",
        )
        with self.assertRaises(SourceMonitoringInitializationError):
            self.plan(settings, metadata=_metadata(official=False))

    def test_invalid_full_candidate_fails_before_selection(self) -> None:
        invalid = _item(4, "2026-08-31T05:00:00Z")
        invalid["unexpected"] = True
        with self.assertRaises(Exception):
            self.plan(
                SourceMonitoringSettings(
                    initial_mode="catch_up",
                    catch_up_max_items=1,
                ),
                result=_result([invalid]),
            )


class _InitialModeAdapter:
    contract_version = SOURCE_ADAPTER_CONTRACT_VERSION
    adapter_key = "fixture_initial"
    config_version = "fixture_initial_v1"
    poll_interval_ms = 60_000
    max_candidates_per_poll = 3
    official_source = True
    execution_capability = "none"
    live_trading_allowed = False

    def __init__(self, items: list[dict[str, object]]) -> None:
        self.items = items
        self.source_errors: tuple[SourcePollError, ...] = ()
        self.rejected_count = 0
        self.poll_count = 0

    def poll(
        self,
        checkpoint: dict[str, object],
        *,
        observed_at_ms: int,
        deadline_monotonic_ms: int = 0,
        cancel_event=None,
        etag: str = "",
        last_modified: str = "",
        max_items: int = 50,
    ) -> AdapterPollResult:
        del deadline_monotonic_ms, cancel_event
        self.poll_count += 1
        cursor = checkpoint.get("cursor", 0)
        return AdapterPollResult.build(
            self.adapter_key,
            checkpoint,
            {"cursor": int(cursor) + 1},
            self.items[:max_items],
            self.source_errors,
            captured_at_ms=observed_at_ms,
            rejected_count=self.rejected_count,
        )


class SourceMonitoringInitialSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-monitor-initial-integration-"
        )
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
        self.adapter = _InitialModeAdapter([
            _item(1, "2026-08-31T03:00:00Z"),
            _item(2, "2026-08-31T04:00:00Z"),
            _item(3, "2026-08-31T05:00:00Z"),
        ])
        self.registry = SourceAdapterRegistry((self.adapter,))
        self.repository.set_enabled(
            self.adapter.adapter_key,
            config_version=self.adapter.config_version,
            enabled=True,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def supervisor(self, settings: SourceMonitoringSettings) -> SourceMonitoringSupervisor:
        return SourceMonitoringSupervisor(
            registry=self.registry,
            repository=self.repository,
            source_inbox=self.inbox,
            settings=settings,
            clock_ms=lambda: self.clock[0],
            event_sink=lambda *args, **kwargs: None,
        )

    def inbox_counts(self) -> tuple[int, int]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            return (
                connection.execute("SELECT COUNT(*) FROM source_inbox_imports").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM source_inbox_items").fetchone()[0],
            )

    def test_seed_only_first_run_is_atomic_baseline_and_second_imports(self) -> None:
        supervisor = self.supervisor(
            SourceMonitoringSettings(enabled=True, dry_run=False)
        )

        first = supervisor.run_once(self.adapter.adapter_key)
        self.assertEqual(first["status"], "SUCCEEDED")
        self.assertEqual(first["initialization"]["outcome"], "seeded")
        self.assertEqual(first["state"]["checkpoint"], {"cursor": 1})
        self.assertEqual(self.inbox_counts(), (0, 0))
        evidence = self.repository.get_latest_successful_initialization(
            self.adapter.adapter_key,
            config_version=self.adapter.config_version,
        )
        self.assertEqual(evidence["mode"], "seed_only")

        second = supervisor.run_once(self.adapter.adapter_key)
        self.assertEqual(second["status"], "SUCCEEDED")
        self.assertEqual(second["initialization"]["outcome"], "not_required")
        self.assertEqual(second["state"]["checkpoint"], {"cursor": 2})
        self.assertEqual(self.inbox_counts(), (1, 3))

    def test_catch_up_requires_matching_preview_and_imports_only_newest_cap(self) -> None:
        preview_settings = SourceMonitoringSettings(
            enabled=True,
            dry_run=False,
            initial_mode="catch_up",
            catch_up_max_items=2,
        )
        preview_result = self.adapter.poll(
            {},
            observed_at_ms=self.clock[0],
            max_items=50,
        )
        preview = plan_initial_poll(
            preview_result,
            metadata=self.registry.metadata_for(self.adapter.adapter_key),
            settings=preview_settings,
            initial_required=True,
            received_at_ms=self.clock[0],
        ).preview
        confirmed = SourceMonitoringSettings(
            enabled=True,
            dry_run=False,
            initial_mode="catch_up",
            catch_up_max_items=2,
            initial_preview_sha256=preview["preview_sha256"],
        )

        result = self.supervisor(confirmed).run_once(self.adapter.adapter_key)

        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(result["initialization"]["selected_count"], 2)
        self.assertEqual(result["initialization"]["skipped_count"], 1)
        with closing(sqlite3.connect(self.database_path)) as connection:
            external_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT external_item_id FROM source_inbox_items ORDER BY external_item_id"
                ).fetchall()
            ]
        self.assertEqual(external_ids, ["initial-2", "initial-3"])

    def test_from_time_filters_inclusively_before_import_and_commits_full_cursor(self) -> None:
        settings = SourceMonitoringSettings(
            enabled=True,
            dry_run=False,
            initial_mode="from_time",
            from_time="2026-08-31T04:00:00Z",
        )

        result = self.supervisor(settings).run_once(self.adapter.adapter_key)

        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(result["initialization"]["selected_count"], 2)
        self.assertEqual(result["state"]["checkpoint"], {"cursor": 1})
        self.assertEqual(self.inbox_counts(), (1, 2))

    def test_partial_first_poll_leaves_no_checkpoint_inbox_or_receipt(self) -> None:
        self.adapter.source_errors = (
            SourcePollError.build(
                "FIXTURE_PARTIAL",
                "partial fixture",
                self.adapter.adapter_key,
            ),
        )
        result = self.supervisor(
            SourceMonitoringSettings(enabled=True, dry_run=False)
        ).run_once(self.adapter.adapter_key)

        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(result["initialization"]["outcome"], "blocked")
        self.assertEqual(result["state"]["checkpoint"], {})
        self.assertEqual(self.inbox_counts(), (0, 0))
        self.assertIsNone(
            self.repository.get_latest_successful_initialization(
                self.adapter.adapter_key,
                config_version=self.adapter.config_version,
            )
        )

    def test_dry_run_seed_is_preview_only_and_never_records_receipt(self) -> None:
        result = self.supervisor(
            SourceMonitoringSettings(enabled=True, dry_run=True)
        ).run_once(self.adapter.adapter_key)

        self.assertEqual(result["status"], "DRY_RUN")
        self.assertEqual(result["initialization"]["outcome"], "would_seed")
        self.assertEqual(result["state"]["checkpoint"], {})
        self.assertEqual(self.inbox_counts(), (0, 0))
        self.assertIsNone(
            self.repository.get_latest_successful_initialization(
                self.adapter.adapter_key,
                config_version=self.adapter.config_version,
            )
        )

    def test_restart_policy_drift_fails_before_poll_or_run_creation(self) -> None:
        seeded = self.supervisor(
            SourceMonitoringSettings(enabled=True, dry_run=False)
        )
        seeded.run_once(self.adapter.adapter_key)
        poll_count = self.adapter.poll_count
        run_count = len(
            self.repository.list_runs(adapter_key=self.adapter.adapter_key)
        )

        with self.assertRaises(SourceMonitoringSupervisorError) as raised:
            self.supervisor(
                SourceMonitoringSettings(
                    enabled=True,
                    dry_run=False,
                    initial_mode="from_time",
                    from_time="2026-08-31T00:00:00Z",
                )
            ).run_once(self.adapter.adapter_key)

        self.assertEqual(
            raised.exception.code,
            "SOURCE_MONITORING_INITIAL_POLICY_MISMATCH",
        )
        self.assertEqual(self.adapter.poll_count, poll_count)
        self.assertEqual(
            len(self.repository.list_runs(adapter_key=self.adapter.adapter_key)),
            run_count,
        )


if __name__ == "__main__":
    unittest.main()
