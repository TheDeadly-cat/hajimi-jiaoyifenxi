from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.instance_ownership import InstanceAlreadyRunning  # noqa: E402
from backend.database_migration import DatabaseMigrationRequired  # noqa: E402
from backend.source_inbox_contracts import PROJECT_SOURCE_ITEM_VERSION  # noqa: E402
from backend.source_monitoring.adapters.base import (  # noqa: E402
    SOURCE_ADAPTER_CONTRACT_VERSION,
)
from backend.source_monitoring.contracts import (  # noqa: E402
    FUTU_ANOMALY_SOURCE_CHANNEL,
    READONLY_MARKET_SOURCE_CLASS,
    AdapterPollResult,
    SourcePollError,
    canonical_sha256,
)
from backend.source_monitoring.registry import SourceAdapterRegistry  # noqa: E402
from backend.source_monitoring.settings import SourceMonitoringSettings  # noqa: E402
from backend.source_monitoring.state_repository import (  # noqa: E402
    SourceMonitoringStateRepository,
)
from backend.source_monitoring_cli import (  # noqa: E402
    SOURCE_MONITORING_CLI_POLL_TIMEOUT_MS,
    SOURCE_MONITORING_INSTANCE_ACTIVE,
    SourceMonitoringCliDependencies,
    main,
)
from backend.store import StudioStore  # noqa: E402


CAPTURED_AT_MS = 1_788_150_000_000


def _item() -> dict[str, object]:
    occurred_at = "2026-08-30T03:00:00Z"
    return {
        "version": PROJECT_SOURCE_ITEM_VERSION,
        "external_item_id": "operator-cli-fixture-1",
        "item_type": "sec_filing",
        "severity": "info",
        "occurred_at": occurred_at,
        "published_at": occurred_at,
        "entities": [{"kind": "security", "id": "US.MU", "label": "MU"}],
        "headline": "SECRET_HEADLINE must never leave preview",
        "summary": "SECRET_SUMMARY must never leave preview",
        "facts": [
            {
                "claim": "A fixed test filing exists.",
                "source_indexes": [0],
            }
        ],
        "sources": [
            {
                "url": "https://secret.invalid/SECRET_URL",
                "publisher": "U.S. SEC",
                "source_type": "official_filing",
                "published_at": occurred_at,
                "content_sha256": "",
            }
        ],
        "impact_hypotheses": [],
        "unknowns": ["No model call was performed."],
        "confidence": 1.0,
        "recommended_route": "notify_only",
        "extensions": {"fixture_v1": {"sequence": 1}},
    }


class FakeOwner:
    def __init__(self, _path: str | Path, *, events: list[str] | None = None) -> None:
        self.events = events

    def acquire(self) -> "FakeOwner":
        if self.events is not None:
            self.events.append("owner_acquire")
        return self

    def release(self) -> None:
        if self.events is not None:
            self.events.append("owner_release")


class ConflictingOwner(FakeOwner):
    def acquire(self) -> "FakeOwner":
        raise InstanceAlreadyRunning("SECRET owner metadata and database path")


class FakeAdapter:
    contract_version = SOURCE_ADAPTER_CONTRACT_VERSION
    adapter_key = "fixture_adapter"
    config_version = "fixture_adapter_config_v1"
    poll_interval_ms = 60_000
    max_candidates_per_poll = 2
    official_source = True
    execution_capability = "none"
    live_trading_allowed = False

    def __init__(self, *, mode: str = "normal") -> None:
        self.mode = mode
        self.poll_count = 0
        self.poll_controls: list[tuple[int, object]] = []

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
        self.poll_controls.append((deadline_monotonic_ms, cancel_event))
        self.poll_count += 1
        if self.mode == "secret_exception":
            raise RuntimeError("SECRET_TOKEN=operator-preview-secret")
        errors = (
            (
                SourcePollError.build(
                    "FIXTURE_PARTIAL_FAILURE",
                    "SECRET upstream detail",
                    self.adapter_key,
                ),
            )
            if self.mode == "degraded"
            else ()
        )
        return AdapterPollResult.build(
            (
                "other_adapter"
                if self.mode == "wrong_adapter"
                else self.adapter_key
            ),
            ({"tampered": 1} if self.mode == "wrong_checkpoint" else checkpoint),
            {"cursor": 1},
            [_item()][:max_items],
            errors,
            captured_at_ms=observed_at_ms,
            etag='"SECRET_ETAG"',
            last_modified="Sun, 30 Aug 2026 03:00:00 GMT",
            market_calls_performed=(1 if self.mode == "market_overflow" else 0),
        )


class FakeMarketSeedAdapter(FakeAdapter):
    adapter_key = "fixture_market"
    config_version = "fixture_market_config_v1"
    official_source = False
    source_class = READONLY_MARKET_SOURCE_CLASS
    source_channel = FUTU_ANOMALY_SOURCE_CHANNEL
    max_market_calls_per_poll = 1

    def initial_seed_policy(self) -> dict[str, object]:
        manifest: dict[str, object] = {
            "version": "source_monitoring_initial_seed_policy_v1",
            "initial_mode": "seed_only",
            "symbol_allowlist": ["US.MU", "US.SNDK", "US.WDC", "US.STX"],
            "execution_capability": "none",
            "live_trading_allowed": False,
            "adapter_key": self.adapter_key,
            "config_version": self.config_version,
            "adapter_config_sha256": "a" * 64,
            "broker_policy_sha256": "b" * 64,
        }
        manifest["source_policy_sha256"] = canonical_sha256(manifest)
        return manifest


class FakeHealth:
    def __init__(self, snapshot: dict[str, object]) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> dict[str, object]:
        return self._snapshot


class FakeSupervisor:
    def __init__(self, result: dict[str, object] | BaseException) -> None:
        self.result = result
        self.call_count = 0
        self.poll_controls: list[tuple[int, object]] = []

    def run_once(
        self,
        _adapter_key: str,
        *,
        deadline_monotonic_ms: int = 0,
        cancel_event=None,
    ) -> dict[str, object]:
        self.call_count += 1
        self.poll_controls.append((deadline_monotonic_ms, cancel_event))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _logical_fingerprint(database_path: Path) -> str:
    with closing(sqlite3.connect(database_path)) as connection:
        objects = connection.execute(
            """SELECT type,name,sql FROM sqlite_master
                 WHERE name NOT LIKE 'sqlite_%'
                 ORDER BY type,name"""
        ).fetchall()
        payload: list[object] = [objects]
        for (table_name,) in connection.execute(
            """SELECT name FROM sqlite_master
                 WHERE type='table' AND name NOT LIKE 'sqlite_%'
                 ORDER BY name"""
        ):
            rows = connection.execute(
                f'SELECT * FROM "{table_name}" ORDER BY rowid'
            ).fetchall()
            payload.append((table_name, rows))
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _file_family_snapshot(database_path: Path) -> dict[str, tuple[bytes, int]]:
    return {
        candidate.name: (candidate.read_bytes(), candidate.stat().st_mtime_ns)
        for candidate in (
            database_path,
            Path(f"{database_path}-wal"),
            Path(f"{database_path}-shm"),
            Path(f"{database_path}-journal"),
        )
        if candidate.is_file()
    }


class SourceMonitoringCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ai-studio-monitor-cli-")
        self.database_path = Path(self.temporary.name) / "studio.sqlite3"
        self.store = StudioStore(self.database_path)
        self.repository = SourceMonitoringStateRepository(
            self.store,
            clock_ms=lambda: CAPTURED_AT_MS,
        )
        self.adapter = FakeAdapter()
        self.registry = SourceAdapterRegistry((self.adapter,))
        self.settings = SourceMonitoringSettings(
            enabled=True,
            official_only=True,
            dry_run=True,
            max_items_per_run=50,
            initial_mode="from_time",
            from_time="1970-01-01T00:00:00Z",
        )
        self.repository.set_enabled(
            self.adapter.adapter_key,
            config_version=self.adapter.config_version,
            enabled=True,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def dependencies(self, **overrides: object) -> SourceMonitoringCliDependencies:
        values: dict[str, object] = {
            "database_path": self.database_path,
            "owner_factory": lambda path: FakeOwner(path),
            "preflight": lambda _path: None,
            "store_opener": lambda _path: self.store,
            "settings_loader": lambda: self.settings,
            "registry_builder": lambda _settings: self.registry,
            "repository_builder": lambda _store: self.repository,
            "clock_ms": lambda: CAPTURED_AT_MS,
        }
        values.update(overrides)
        return SourceMonitoringCliDependencies(**values)

    def invoke(
        self,
        arguments: list[str],
        *,
        dependencies: SourceMonitoringCliDependencies | None = None,
    ) -> tuple[int, str, dict[str, object]]:
        output = io.StringIO()
        exit_code = main(
            arguments,
            dependencies=dependencies or self.dependencies(),
            output=output,
        )
        text = output.getvalue()
        return exit_code, text, json.loads(text)

    def test_all_commands_fail_closed_on_owner_conflict_before_other_resolution(self) -> None:
        for arguments in (
            ["status"],
            ["preview", self.adapter.adapter_key],
            ["run-once", self.adapter.adapter_key, "--confirm", "RUN_ONCE"],
            [
                "enable",
                self.adapter.adapter_key,
                "--expected-config-version",
                self.adapter.config_version,
                "--expected-state-version",
                "1",
                "--confirm",
                "ENABLE_SOURCE_MONITORING_ADAPTER",
            ],
            [
                "disable",
                self.adapter.adapter_key,
                "--expected-config-version",
                self.adapter.config_version,
                "--expected-state-version",
                "1",
                "--confirm",
                "DISABLE_SOURCE_MONITORING_ADAPTER",
            ],
        ):
            calls: list[str] = []

            def forbidden(*_args: object, **_kwargs: object) -> object:
                calls.append("forbidden")
                raise AssertionError("must not resolve after owner conflict")

            dependencies = SourceMonitoringCliDependencies(
                database_path=self.database_path,
                owner_factory=lambda path: ConflictingOwner(path),
                preflight=forbidden,
                store_opener=forbidden,
                settings_loader=forbidden,
                registry_builder=forbidden,
                repository_builder=forbidden,
                health_builder=forbidden,
                supervisor_builder=forbidden,
            )
            with self.subTest(command=arguments[0]):
                exit_code, text, payload = self.invoke(
                    arguments,
                    dependencies=dependencies,
                )
                self.assertEqual(exit_code, 3)
                self.assertEqual(payload, {"error_code": SOURCE_MONITORING_INSTANCE_ACTIVE})
                self.assertNotIn(str(self.database_path), text)
                self.assertNotIn("SECRET", text)
                self.assertEqual(calls, [])
                self.assertEqual(self.adapter.poll_count, 0)

    def test_operator_preview_enable_disable_flow_uses_exact_contracts(self) -> None:
        disabled = self.repository.set_enabled(
            self.adapter.adapter_key,
            config_version=self.adapter.config_version,
            enabled=False,
        )
        state_version = disabled["state_version"]
        exit_code, _text, preview = self.invoke(
            [
                "preview",
                self.adapter.adapter_key,
                "--expected-config-version",
                self.adapter.config_version,
                "--expected-state-version",
                str(state_version),
            ]
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            set(preview),
            {
                "version",
                "adapter_key",
                "config_version",
                "state_version",
                "mode",
                "initial_required",
                "initialization_blocked",
                "catch_up_max_items",
                "from_time",
                "candidate_count",
                "selected_count",
                "skipped_count",
                "adapter_duplicate_count",
                "source_error_count",
                "rejected_count",
                "earliest_occurred_at",
                "latest_occurred_at",
                "preview_sha256",
                "starting_checkpoint_sha256",
                "next_checkpoint_sha256",
                "captured_at_ms",
                "safety",
            },
        )
        exit_code, _text, enabled = self.invoke(
            [
                "enable",
                self.adapter.adapter_key,
                "--expected-config-version",
                self.adapter.config_version,
                "--expected-state-version",
                str(state_version),
                "--preview-sha256",
                preview["preview_sha256"],
                "--confirm",
                "ENABLE_SOURCE_MONITORING_ADAPTER",
            ]
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(enabled["version"], "source_monitoring_enablement_result_v1")
        self.assertTrue(enabled["persisted_enabled"])
        self.assertTrue(enabled["initialization_authorized"])

        exit_code, _text, disabled_result = self.invoke(
            [
                "disable",
                self.adapter.adapter_key,
                "--expected-config-version",
                self.adapter.config_version,
                "--expected-state-version",
                str(enabled["state_version"]),
                "--confirm",
                "DISABLE_SOURCE_MONITORING_ADAPTER",
            ]
        )
        self.assertEqual(exit_code, 0)
        self.assertFalse(disabled_result["persisted_enabled"])
        self.assertFalse(disabled_result["initialization_authorized"])
        state = self.repository.get_state(self.adapter.adapter_key)
        self.assertIsNone(state["pending_initialization_authorization"])

    def test_status_is_read_only_network_free_and_does_not_build_registry(self) -> None:
        before = _logical_fingerprint(self.database_path)
        registry_calls = [0]
        health = {
            "captured_at_ms": CAPTURED_AT_MS,
            "state": "healthy",
            "persistence_available": True,
            "counts": {"healthy": 1},
            "runtime": {
                "status": "stopped",
                "started_at": 0,
                "heartbeat_at": 0,
                "last_loop_at": 0,
                "active_adapter": "",
                "next_due_at": 0,
                "thread_alive": False,
                "liveness_verified": False,
                "last_fatal_error_code": "",
            },
            "adapters": [
                {
                    "adapter_key": self.adapter.adapter_key,
                    "state": "healthy",
                    "persisted_enabled": True,
                    "config_status": "current",
                    "last_error_code": "",
                    "runtime_liveness_verified": False,
                }
            ],
        }

        def forbidden_registry(_settings: object) -> object:
            registry_calls[0] += 1
            raise AssertionError("status must not build a source registry")

        exit_code, _text, payload = self.invoke(
            ["status"],
            dependencies=self.dependencies(
                registry_builder=forbidden_registry,
                health_builder=lambda _store, _settings: FakeHealth(health),
            ),
        )
        after = _logical_fingerprint(self.database_path)
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["safety"]["network_requests_performed"], 0)
        self.assertEqual(payload["safety"]["database_writes_performed"], 0)
        self.assertFalse(payload["safety"]["http_listener_started"])
        self.assertEqual(registry_calls, [0])
        self.assertEqual(self.adapter.poll_count, 0)
        self.assertEqual(before, after)

    def test_owner_precedes_preflight_and_missing_or_stale_schema_is_bounded(self) -> None:
        events: list[str] = []

        def owner_factory(path: str | Path) -> FakeOwner:
            return FakeOwner(path, events=events)

        def missing(_path: str | Path) -> None:
            events.append("preflight")
            raise FileNotFoundError("SECRET_DATABASE_PATH")

        exit_code, text, payload = self.invoke(
            ["status"],
            dependencies=self.dependencies(
                owner_factory=owner_factory,
                preflight=missing,
            ),
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(
            payload["error_code"],
            "SOURCE_MONITORING_DATABASE_UNAVAILABLE",
        )
        self.assertEqual(events, ["owner_acquire", "preflight", "owner_release"])
        self.assertNotIn("SECRET_DATABASE_PATH", text)

        def migration_required(_path: str | Path) -> None:
            raise DatabaseMigrationRequired(
                {"plan_sha256": "a" * 64}
            )

        exit_code, text, payload = self.invoke(
            ["status"],
            dependencies=self.dependencies(preflight=migration_required),
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(
            payload["error_code"],
            "SOURCE_MONITORING_DATABASE_MIGRATION_REQUIRED",
        )
        self.assertNotIn("a" * 64, text)

    def test_preview_preserves_database_fingerprint_and_redacts_content(self) -> None:
        before = _logical_fingerprint(self.database_path)
        physical_before = _file_family_snapshot(self.database_path)
        exit_code, text, payload = self.invoke(
            ["preview", self.adapter.adapter_key]
        )
        after = _logical_fingerprint(self.database_path)
        physical_after = _file_family_snapshot(self.database_path)
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(payload["selected_count"], 1)
        self.assertEqual(payload["safety"]["database_writes_performed"], 0)
        self.assertEqual(payload["safety"]["checkpoint_writes_performed"], 0)
        self.assertEqual(payload["safety"]["source_inbox_writes_performed"], 0)
        self.assertEqual(payload["safety"]["provider_calls_performed"], 0)
        self.assertIsNone(payload["safety"]["network_requests_performed"])
        self.assertEqual(
            payload["safety"]["network_requests_accounting"],
            "not_instrumented",
        )
        self.assertEqual(before, after)
        self.assertEqual(physical_before, physical_after)
        for secret in ("SECRET_HEADLINE", "SECRET_SUMMARY", "SECRET_URL", "SECRET_ETAG"):
            self.assertNotIn(secret, text)

    def test_sec_cli_preview_authorizes_the_same_complete_history_across_run_once_batches(self) -> None:
        from backend.market.sec_edgar import SecEdgarAdapter
        from backend.source_inbox_service import SourceInboxService
        from backend.source_monitoring.adapters.sec_filings import SecFilingsSourceAdapter
        from backend.source_monitoring.supervisor import SourceMonitoringSupervisor
        from tests.test_source_monitoring_sec_baseline import MutableSecRecentFetcher, NOW, NOW_MS

        fetcher = MutableSecRecentFetcher(7)
        fetcher.records = [
            (index, f"2026-09-04T{index:02d}:00:00Z") for index in range(1, 8)
        ]
        self.adapter = SecFilingsSourceAdapter(
            adapter=SecEdgarAdapter(
                user_agent="Offline CLI fixture fixture@example.com",
                fetch_json=fetcher, clock=lambda: NOW, allowed_symbols=["US.NVDA"],
            ),
            allowed_symbols=["US.NVDA"], allowed_forms=["8-K"],
            per_symbol_limit=2, force=True,
        )
        self.registry = SourceAdapterRegistry((self.adapter,))
        self.repository = SourceMonitoringStateRepository(self.store, clock_ms=lambda: NOW_MS)
        self.repository.set_enabled(
            self.adapter.adapter_key, config_version=self.adapter.config_version, enabled=True,
        )
        self.settings = SourceMonitoringSettings(
            enabled=True, dry_run=False, initial_mode="catch_up", catch_up_max_items=5,
        )

        def supervisor_builder(store, settings, registry, repository):
            return SourceMonitoringSupervisor(
                registry=registry, repository=repository,
                source_inbox=SourceInboxService(store, clock=lambda: NOW_MS / 1_000),
                settings=settings, clock_ms=lambda: NOW_MS,
                event_sink=lambda *_args, **_kwargs: None,
            )

        dependencies = self.dependencies(
            clock_ms=lambda: NOW_MS, supervisor_builder=supervisor_builder,
        )
        before = _logical_fingerprint(self.database_path)
        exit_code, _text, preview = self.invoke(
            ["preview", self.adapter.adapter_key], dependencies=dependencies,
        )
        self.assertEqual(exit_code, 0, preview)
        self.assertEqual(preview["candidate_count"], 2)
        self.assertEqual(preview["selected_count"], 2)
        self.assertEqual(_logical_fingerprint(self.database_path), before)
        self.assertEqual(self.repository.get_state(self.adapter.adapter_key)["checkpoint"], {})

        # Reuse exactly the CLI-issued confirmation in the real Supervisor.
        # Five authorized identities require three polls at the sealed limit.
        self.settings = SourceMonitoringSettings(
            enabled=True, dry_run=False, initial_mode="catch_up", catch_up_max_items=5,
            initial_preview_sha256=preview["preview_sha256"],
        )
        for expected_count in (2, 2, 1, 0):
            exit_code, _text, result = self.invoke(
                ["run-once", self.adapter.adapter_key, "--confirm", "RUN_ONCE"],
                dependencies=dependencies,
            )
            self.assertEqual(exit_code, 0, result)
            self.assertEqual(result["status"], "SUCCEEDED", result)
            self.assertEqual(result["counts"]["accepted_count"], expected_count)
        receipt = self.repository.get_latest_successful_initialization(
            self.adapter.adapter_key, config_version=self.adapter.config_version,
        )
        self.assertEqual(receipt["preview_sha256"], preview["preview_sha256"])
        with closing(sqlite3.connect(self.database_path)) as connection:
            ids = [row[0] for row in connection.execute(
                "SELECT external_item_id FROM source_inbox_items ORDER BY external_item_id"
            )]
            self.assertEqual(ids, [f"0001045810-26-{index:06d}" for index in range(3, 8)])
            for table in ("provider_execution_runs", "provider_call_attempts", "rounds", "source_inbox_round_drafts"):
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)

    def test_preview_disabled_and_config_mismatch_fail_before_poll(self) -> None:
        disabled = SourceMonitoringSettings(
            enabled=False,
            official_only=True,
            dry_run=True,
            initial_mode="seed_only",
        )
        exit_code, _text, payload = self.invoke(
            ["preview", self.adapter.adapter_key],
            dependencies=self.dependencies(settings_loader=lambda: disabled),
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["error_code"], "SOURCE_MONITORING_DISABLED")
        self.assertEqual(self.adapter.poll_count, 0)

        drifted = FakeAdapter()
        drifted.config_version = "fixture_adapter_config_v2"
        drifted_registry = SourceAdapterRegistry((drifted,))
        exit_code, _text, payload = self.invoke(
            ["preview", drifted.adapter_key],
            dependencies=self.dependencies(
                registry_builder=lambda _settings: drifted_registry,
            ),
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["error_code"], "SOURCE_MONITORING_CONFIG_CONFLICT")
        self.assertEqual(drifted.poll_count, 0)

    def test_preview_initial_policy_drift_fails_before_poll(self) -> None:
        initialization = {
            "mode": "catch_up",
            "catch_up_max_items": 1,
            "from_time_ms": 0,
        }
        with mock.patch.object(
            self.repository,
            "read_latest_successful_initialization_from_connection",
            return_value=initialization,
        ):
            exit_code, _text, payload = self.invoke(
                ["preview", self.adapter.adapter_key]
            )
        self.assertEqual(exit_code, 2)
        self.assertEqual(
            payload["error_code"],
            "SOURCE_MONITORING_INITIAL_POLICY_MISMATCH",
        )
        self.assertEqual(self.adapter.poll_count, 0)

    def test_preview_revalidates_poll_identity_checkpoint_and_market_bound(self) -> None:
        cases = (
            ("wrong_adapter", "SOURCE_MONITORING_ADAPTER_RESULT_MISMATCH"),
            ("wrong_checkpoint", "SOURCE_MONITORING_CHECKPOINT_START_MISMATCH"),
            ("market_overflow", "SOURCE_MONITORING_MARKET_CALL_BOUND_EXCEEDED"),
        )
        for mode, expected_code in cases:
            adapter = FakeAdapter(mode=mode)
            registry = SourceAdapterRegistry((adapter,))
            with self.subTest(mode=mode):
                exit_code, _text, payload = self.invoke(
                    ["preview", adapter.adapter_key],
                    dependencies=self.dependencies(
                        registry_builder=lambda _settings, registry=registry: registry,
                    ),
                )
                self.assertEqual(exit_code, 2)
                self.assertEqual(payload["error_code"], expected_code)
                self.assertEqual(adapter.poll_count, 1)

    def test_preview_passes_fixed_absolute_monotonic_deadline(self) -> None:
        with mock.patch(
            "backend.source_monitoring_cli.time.monotonic",
            return_value=100.0,
        ):
            exit_code, _text, payload = self.invoke(
                ["preview", self.adapter.adapter_key]
            )

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            self.adapter.poll_controls,
            [(100_000 + SOURCE_MONITORING_CLI_POLL_TIMEOUT_MS, None)],
        )

    def test_secret_exception_is_reduced_to_generic_code(self) -> None:
        secret_adapter = FakeAdapter(mode="secret_exception")
        secret_registry = SourceAdapterRegistry((secret_adapter,))
        exit_code, text, payload = self.invoke(
            ["preview", secret_adapter.adapter_key],
            dependencies=self.dependencies(
                registry_builder=lambda _settings: secret_registry,
            ),
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["error_code"], "SOURCE_MONITORING_CLI_UNEXPECTED")
        self.assertNotIn("SECRET", text)
        self.assertNotIn("operator-preview-secret", text)

    def test_market_initial_preview_is_static_and_performs_zero_quote_calls(self) -> None:
        adapter = FakeMarketSeedAdapter()
        registry = SourceAdapterRegistry((adapter,), official_only=False)
        settings = SourceMonitoringSettings(
            enabled=True,
            official_only=False,
            allow_readonly_market=True,
            dry_run=True,
        )
        self.repository.set_enabled(
            adapter.adapter_key,
            config_version=adapter.config_version,
            enabled=True,
        )
        dependencies = self.dependencies(
            settings_loader=lambda: settings,
            registry_builder=lambda _settings: registry,
        )

        exit_code, _text, payload = self.invoke(
            ["preview", adapter.adapter_key],
            dependencies=dependencies,
        )

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["preview_kind"], "static_seed_policy")
        self.assertEqual(
            payload["candidate_evidence"],
            "deferred_to_first_runtime_poll",
        )
        self.assertEqual(payload["candidate_count"], 0)
        self.assertEqual(payload["safety"]["market_calls_performed"], 0)
        self.assertEqual(payload["safety"]["network_requests_performed"], 0)
        self.assertEqual(adapter.poll_count, 0)

    def test_combined_run_once_selects_market_registry_with_derived_pipeline_settings(self) -> None:
        market_adapter = FakeMarketSeedAdapter()
        market_registry = SourceAdapterRegistry(
            (market_adapter,),
            official_only=False,
        )
        settings = SourceMonitoringSettings(
            enabled=True,
            official_only=True,
            allow_readonly_market=True,
            dry_run=True,
            initial_mode="catch_up",
            catch_up_max_items=1,
        )
        self.repository.set_enabled(
            market_adapter.adapter_key,
            config_version=market_adapter.config_version,
            enabled=True,
        )
        captured: dict[str, object] = {}

        def supervisor_builder(_store, pipeline_settings, selected_registry, _repository):
            captured["settings"] = pipeline_settings
            captured["registry"] = selected_registry
            return FakeSupervisor({
                "status": "DRY_RUN",
                "run": {
                    "observed_count": 1,
                    "accepted_count": 0,
                    "duplicate_count": 0,
                    "rejected_count": 0,
                },
                "initialization": {
                    "mode": "seed_only",
                    "outcome": "would_seed",
                },
                "import": None,
                "source_inbox_writes_performed": False,
                "safety": {"market_calls_performed": 1},
            })

        exit_code, _text, payload = self.invoke(
            ["run-once", market_adapter.adapter_key, "--confirm", "RUN_ONCE"],
            dependencies=self.dependencies(
                settings_loader=lambda: settings,
                registry_builder=lambda _settings: (self.registry, market_registry),
                supervisor_builder=supervisor_builder,
            ),
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["import_mode"], "seed_only")
        self.assertIs(captured["registry"], market_registry)
        pipeline_settings = captured["settings"]
        self.assertFalse(pipeline_settings.official_only)
        self.assertTrue(pipeline_settings.allow_readonly_market)
        self.assertEqual(pipeline_settings.initial_mode, "catch_up")
        self.assertEqual(
            pipeline_settings.initialization_policy_for(
                official_source=False,
            ).mode,
            "seed_only",
        )

    def test_preview_degraded_is_bounded_and_non_success(self) -> None:
        degraded = FakeAdapter(mode="degraded")
        degraded_registry = SourceAdapterRegistry((degraded,))
        initialization = {
            "mode": "from_time",
            "catch_up_max_items": 0,
            "from_time_ms": 0,
        }
        with mock.patch.object(
            self.repository,
            "read_latest_successful_initialization_from_connection",
            return_value=initialization,
        ):
            exit_code, text, payload = self.invoke(
                ["preview", degraded.adapter_key],
                dependencies=self.dependencies(
                    registry_builder=lambda _settings: degraded_registry,
                ),
            )
        self.assertEqual(exit_code, 2)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["initialization_blocked"])
        self.assertEqual(payload["error_codes"], ["FIXTURE_PARTIAL_FAILURE"])
        self.assertNotIn("SECRET upstream detail", text)

    def test_preview_preserves_nonempty_wal_and_shm_bytes_size_and_mtime(self) -> None:
        writer = self.store._connect()
        try:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(
                """UPDATE source_adapter_states
                      SET updated_at_ms=updated_at_ms+1
                    WHERE adapter_key=?""",
                (self.adapter.adapter_key,),
            )
            writer.commit()
            wal_path = Path(f"{self.database_path}-wal")
            shm_path = Path(f"{self.database_path}-shm")
            self.assertTrue(wal_path.is_file())
            self.assertGreater(wal_path.stat().st_size, 0)
            self.assertTrue(shm_path.is_file())

            physical_before = _file_family_snapshot(self.database_path)
            exit_code, _text, payload = self.invoke(
                ["preview", self.adapter.adapter_key]
            )
            physical_after = _file_family_snapshot(self.database_path)

            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(physical_before, physical_after)
        finally:
            writer.close()

    def test_run_once_requires_exact_confirmation_before_supervisor(self) -> None:
        supervisor = FakeSupervisor({})
        for confirmation in ("", "run_once", "RUN-ONCE"):
            arguments = ["run-once", self.adapter.adapter_key]
            if confirmation:
                arguments.extend(["--confirm", confirmation])
            preflight_calls = [0]
            store_calls = [0]

            def forbidden_preflight(_path: str | Path) -> None:
                preflight_calls[0] += 1
                raise AssertionError("confirmation must precede preflight")

            def forbidden_store(_path: Path) -> object:
                store_calls[0] += 1
                raise AssertionError("confirmation must precede store open")

            with self.subTest(confirmation=confirmation):
                exit_code, _text, payload = self.invoke(
                    arguments,
                    dependencies=self.dependencies(
                        preflight=forbidden_preflight,
                        store_opener=forbidden_store,
                        supervisor_builder=lambda *_args: supervisor,
                    ),
                )
                self.assertEqual(exit_code, 2)
                self.assertEqual(
                    payload["error_code"],
                    "SOURCE_MONITORING_CONFIRMATION_REQUIRED",
                )
                self.assertEqual(preflight_calls, [0])
                self.assertEqual(store_calls, [0])
        self.assertEqual(supervisor.call_count, 0)
        self.assertEqual(self.adapter.poll_count, 0)

    def test_run_once_passes_fixed_absolute_monotonic_deadline(self) -> None:
        supervisor = FakeSupervisor({
            "status": "DRY_RUN",
            "run": {
                "observed_count": 1,
                "accepted_count": 0,
                "duplicate_count": 0,
                "rejected_count": 0,
            },
            "initialization": {
                "mode": "from_time",
                "outcome": "would_import",
            },
            "import": None,
            "source_inbox_writes_performed": False,
            "safety": {"market_calls_performed": 0},
        })
        with mock.patch(
            "backend.source_monitoring_cli.time.monotonic",
            return_value=200.0,
        ):
            exit_code, _text, payload = self.invoke(
                ["run-once", self.adapter.adapter_key, "--confirm", "RUN_ONCE"],
                dependencies=self.dependencies(
                    supervisor_builder=lambda *_args: supervisor,
                ),
            )

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            supervisor.poll_controls,
            [(200_000 + SOURCE_MONITORING_CLI_POLL_TIMEOUT_MS, None)],
        )
        self.assertEqual(supervisor.call_count, 1)

    def test_enable_and_disable_confirmation_precede_preflight_and_store(self) -> None:
        for command in ("enable", "disable"):
            preflight_calls = [0]
            store_calls = [0]

            def forbidden_preflight(_path: str | Path) -> None:
                preflight_calls[0] += 1
                raise AssertionError("confirmation must precede preflight")

            def forbidden_store(_path: Path) -> object:
                store_calls[0] += 1
                raise AssertionError("confirmation must precede store open")

            with self.subTest(command=command):
                exit_code, _text, payload = self.invoke(
                    [
                        command,
                        self.adapter.adapter_key,
                        "--expected-config-version",
                        self.adapter.config_version,
                        "--expected-state-version",
                        "0",
                        "--confirm",
                        "WRONG_CONFIRMATION",
                    ],
                    dependencies=self.dependencies(
                        preflight=forbidden_preflight,
                        store_opener=forbidden_store,
                    ),
                )
                self.assertEqual(exit_code, 2)
                self.assertEqual(
                    payload["error_code"],
                    "SOURCE_MONITORING_CONFIRMATION_REQUIRED",
                )
                self.assertEqual(preflight_calls, [0])
                self.assertEqual(store_calls, [0])

    def test_run_once_exit_codes_cover_dry_live_and_degraded_results(self) -> None:
        cases = (
            (
                "DRY_RUN",
                True,
                0,
                False,
                {"mode": "seed_only", "outcome": "would_seed"},
            ),
            (
                "SUCCEEDED",
                False,
                0,
                True,
                {"mode": "from_time", "outcome": "initialized"},
            ),
            (
                "DEGRADED",
                False,
                2,
                False,
                {"mode": "from_time", "outcome": "blocked"},
            ),
        )
        for status, dry_run, expected_exit, expected_checkpoint, initialization in cases:
            settings = SourceMonitoringSettings(
                enabled=True,
                official_only=True,
                dry_run=dry_run,
                max_items_per_run=50,
                initial_mode="from_time" if initialization["mode"] == "from_time" else "seed_only",
                from_time=(
                    "1970-01-01T00:00:00Z"
                    if initialization["mode"] == "from_time"
                    else ""
                ),
            )
            result = {
                "status": status,
                "run": {
                    "observed_count": 2,
                    "accepted_count": 1,
                    "duplicate_count": 1,
                    "rejected_count": 0,
                },
                "initialization": initialization,
                "import": {
                    "created_item_count": 1,
                    "duplicate_item_count": 0,
                    "idempotent_replay": False,
                    "SECRET": "must not be serialized",
                },
                "error_code": (
                    "FIXTURE_PARTIAL_FAILURE" if status == "DEGRADED" else ""
                ),
                "error_message": "SECRET error detail",
                "recording_error": "SECRET recording detail",
            }
            supervisor = FakeSupervisor(result)
            with self.subTest(status=status):
                exit_code, text, payload = self.invoke(
                    [
                        "run-once",
                        self.adapter.adapter_key,
                        "--confirm",
                        "RUN_ONCE",
                    ],
                    dependencies=self.dependencies(
                        settings_loader=lambda settings=settings: settings,
                        supervisor_builder=lambda *_args, supervisor=supervisor: supervisor,
                    ),
                )
                self.assertEqual(exit_code, expected_exit)
                self.assertEqual(payload["checkpoint_committed"], expected_checkpoint)
                self.assertEqual(payload["dry_run"], dry_run)
                self.assertFalse(payload["safety"]["http_listener_started"])
                self.assertEqual(payload["safety"]["provider_calls_performed"], 0)
                self.assertEqual(payload["safety"]["model_calls_performed"], 0)
                self.assertFalse(payload["safety"]["live_trading_allowed"])
                self.assertNotIn("SECRET", text)

    def test_run_once_does_not_serialize_secret_supervisor_exception(self) -> None:
        supervisor = FakeSupervisor(SystemExit("SECRET_API_TOKEN=abc123"))
        exit_code, text, payload = self.invoke(
            ["run-once", self.adapter.adapter_key, "--confirm", "RUN_ONCE"],
            dependencies=self.dependencies(
                supervisor_builder=lambda *_args: supervisor,
            ),
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["error_code"], "SOURCE_MONITORING_CLI_UNEXPECTED")
        self.assertIsNone(payload["safety"]["database_writes_performed"])
        self.assertIsNone(payload["safety"]["checkpoint_writes_performed"])
        self.assertIsNone(payload["safety"]["source_inbox_writes_performed"])
        self.assertNotIn("SECRET", text)
        self.assertNotIn("abc123", text)

    def test_run_failure_preserves_known_source_inbox_write_evidence(self) -> None:
        result = {
            "status": "FAILED",
            "state_recorded": False,
            "run": {
                "observed_count": 1,
                "accepted_count": 1,
                "duplicate_count": 0,
                "rejected_count": 0,
            },
            "import": {
                "created_item_count": 1,
                "duplicate_item_count": 0,
                "idempotent_replay": False,
            },
            "source_inbox_writes_performed": True,
            "error_code": "FIXTURE_AFTER_IMPORT_FAILED",
        }

        exit_code, _text, payload = self.invoke(
            ["run-once", self.adapter.adapter_key, "--confirm", "RUN_ONCE"],
            dependencies=self.dependencies(
                supervisor_builder=lambda *_args: FakeSupervisor(result),
            ),
        )

        self.assertEqual(exit_code, 2)
        self.assertTrue(payload["source_inbox_writes_performed"])
        self.assertTrue(payload["safety"]["source_inbox_writes_performed"])
        self.assertTrue(payload["safety"]["database_writes_performed"])
        self.assertFalse(payload["checkpoint_committed"])


if __name__ == "__main__":
    unittest.main()
