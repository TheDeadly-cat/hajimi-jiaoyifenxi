from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.source_inbox_service import SourceInboxService  # noqa: E402
from backend.source_monitoring.operator_service import (  # noqa: E402
    DISABLE_SOURCE_MONITORING_ADAPTER,
    ENABLE_SOURCE_MONITORING_ADAPTER,
    SourceMonitoringOperatorError,
    SourceMonitoringOperatorService,
)
from backend.source_monitoring.registry import SourceAdapterRegistry  # noqa: E402
from backend.source_monitoring.settings import SourceMonitoringSettings  # noqa: E402
from backend.source_monitoring.state_repository import (  # noqa: E402
    SourceMonitoringStateRepository,
)
from backend.source_monitoring.supervisor import SourceMonitoringSupervisor  # noqa: E402
from backend.store import StudioStore  # noqa: E402
from tests.test_source_monitoring_supervisor import FakeAdapter  # noqa: E402


NOW_MS = 1_900_000_000_000


class SourceMonitoringOperatorServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="monitor-operator-service-")
        self.store = StudioStore(Path(self.temporary.name) / "studio.sqlite3")
        self.clock = [NOW_MS]
        self.repository = SourceMonitoringStateRepository(
            self.store,
            clock_ms=lambda: self.clock[0],
        )
        self.adapter = FakeAdapter("operator_fixture")
        self.registry = SourceAdapterRegistry((self.adapter,))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def settings(
        self,
        *,
        dry_run: bool = False,
        environment_preview: str = "",
    ) -> SourceMonitoringSettings:
        return SourceMonitoringSettings(
            enabled=True,
            official_only=True,
            dry_run=dry_run,
            max_items_per_run=50,
            initial_mode="catch_up",
            catch_up_max_items=2,
            initial_preview_sha256=environment_preview,
        )

    def service(
        self,
        *,
        settings: SourceMonitoringSettings | None = None,
    ) -> SourceMonitoringOperatorService:
        return SourceMonitoringOperatorService(
            store=self.store,
            settings=settings or self.settings(),
            registry=self.registry,
            repository=self.repository,
            clock_ms=lambda: self.clock[0],
        )

    def test_control_and_preview_are_exact_bounded_contracts_for_absent_state(self) -> None:
        service = self.service()
        control = service.control_snapshot()
        self.assertEqual(
            set(control),
            {"version", "captured_at_ms", "settings", "adapters", "safety"},
        )
        self.assertEqual(control["version"], "source_monitoring_operator_control_v1")
        adapter = control["adapters"][0]
        self.assertEqual(
            set(adapter),
            {
                "version",
                "adapter_key",
                "config_version",
                "state_version",
                "persisted_state",
                "persisted_enabled",
                "effective_enabled",
                "active_run",
                "source_class",
                "source_channel",
                "official_source",
                "initialization_status",
                "initialization_mode",
                "initialization_preview_sha256",
                "initialization_completed_at_ms",
                "pending_authorization",
                "can_preview",
                "can_enable",
                "can_disable",
                "blocked_reason_codes",
            },
        )
        self.assertEqual(adapter["state_version"], 0)
        self.assertEqual(adapter["initialization_status"], "required")
        self.assertTrue(adapter["can_preview"])

        preview = service.preview(
            self.adapter.adapter_key,
            expected_config_version=self.adapter.config_version,
            expected_state_version=0,
        )
        self.assertEqual(preview["version"], "source_monitoring_operator_preview_v1")
        self.assertEqual(preview["state_version"], 0)
        self.assertEqual(preview["mode"], "catch_up")
        self.assertEqual(preview["candidate_count"], 2)
        self.assertEqual(preview["selected_count"], 2)
        self.assertFalse(preview["initialization_blocked"])
        self.assertEqual(preview["safety"]["database_writes_performed"], 0)
        self.assertEqual(preview["safety"]["market_calls_performed"], 0)
        self.assertIs(type(preview["safety"]["database_writes_performed"]), int)
        self.assertEqual(
            set(preview["safety"]),
            {
                "database_writes_performed",
                "checkpoint_writes_performed",
                "source_inbox_writes_performed",
                "provider_calls_performed",
                "model_calls_performed",
                "network_requests_performed",
                "network_requests_accounting",
                "market_calls_performed",
                "formal_rounds_created",
                "execution_capability",
                "live_trading_allowed",
            },
        )
        self.assertIsNone(self.repository.get_state(self.adapter.adapter_key))

    def test_ui_authorization_survives_restart_and_success_consumes_it(self) -> None:
        settings = self.settings()
        service = self.service(settings=settings)
        preview = service.preview(
            self.adapter.adapter_key,
            expected_config_version=self.adapter.config_version,
            expected_state_version=0,
        )
        enabled = service.set_enablement(
            self.adapter.adapter_key,
            enabled=True,
            expected_config_version=self.adapter.config_version,
            expected_state_version=0,
            confirmation=ENABLE_SOURCE_MONITORING_ADAPTER,
            preview_sha256=preview["preview_sha256"],
        )
        self.assertEqual(enabled["version"], "source_monitoring_enablement_result_v1")
        self.assertTrue(enabled["persisted_enabled"])
        self.assertTrue(enabled["initialization_authorized"])
        self.assertIs(type(enabled["safety"]["database_writes_performed"]), bool)
        self.assertTrue(enabled["safety"]["database_writes_performed"])
        self.assertIs(type(enabled["safety"]["checkpoint_writes_performed"]), bool)
        self.assertFalse(enabled["safety"]["checkpoint_writes_performed"])
        self.assertIs(type(enabled["safety"]["source_inbox_writes_performed"]), bool)
        self.assertFalse(enabled["safety"]["source_inbox_writes_performed"])
        state = self.repository.get_state(self.adapter.adapter_key)
        self.assertIsNotNone(state["pending_initialization_authorization"])

        restarted = SourceMonitoringSupervisor(
            registry=self.registry,
            repository=SourceMonitoringStateRepository(
                self.store,
                clock_ms=lambda: self.clock[0],
            ),
            source_inbox=SourceInboxService(
                self.store,
                clock=lambda: self.clock[0] / 1_000,
            ),
            settings=settings,
            clock_ms=lambda: self.clock[0],
        )
        result = restarted.run_once(self.adapter.adapter_key)
        self.assertEqual(result["status"], "SUCCEEDED")
        state = self.repository.get_state(self.adapter.adapter_key)
        self.assertIsNone(state["pending_initialization_authorization"])
        self.assertIsNotNone(
            self.repository.get_latest_successful_initialization(
                self.adapter.adapter_key,
                config_version=self.adapter.config_version,
            )
        )

    def test_dry_run_preserves_authorization_and_disable_clears_it(self) -> None:
        settings = self.settings(dry_run=True)
        service = self.service(settings=settings)
        preview = service.preview(
            self.adapter.adapter_key,
            expected_config_version=self.adapter.config_version,
            expected_state_version=0,
        )
        service.set_enablement(
            self.adapter.adapter_key,
            enabled=True,
            expected_config_version=self.adapter.config_version,
            expected_state_version=0,
            confirmation=ENABLE_SOURCE_MONITORING_ADAPTER,
            preview_sha256=preview["preview_sha256"],
        )
        supervisor = SourceMonitoringSupervisor(
            registry=self.registry,
            repository=self.repository,
            source_inbox=SourceInboxService(
                self.store,
                clock=lambda: self.clock[0] / 1_000,
            ),
            settings=settings,
            clock_ms=lambda: self.clock[0],
        )
        result = supervisor.run_once(self.adapter.adapter_key)
        self.assertEqual(result["status"], "DRY_RUN")
        state = self.repository.get_state(self.adapter.adapter_key)
        self.assertIsNotNone(state["pending_initialization_authorization"])

        disabled = service.set_enablement(
            self.adapter.adapter_key,
            enabled=False,
            expected_config_version=self.adapter.config_version,
            expected_state_version=state["state_version"],
            confirmation=DISABLE_SOURCE_MONITORING_ADAPTER,
        )
        self.assertFalse(disabled["persisted_enabled"])
        self.assertIsNone(
            self.repository.get_state(self.adapter.adapter_key)[
                "pending_initialization_authorization"
            ]
        )

    def test_environment_and_ui_authorities_conflict_before_enable(self) -> None:
        service = self.service(settings=self.settings(environment_preview="a" * 64))
        preview = service.preview(
            self.adapter.adapter_key,
            expected_config_version=self.adapter.config_version,
            expected_state_version=0,
        )
        with self.assertRaises(SourceMonitoringOperatorError) as caught:
            service.set_enablement(
                self.adapter.adapter_key,
                enabled=True,
                expected_config_version=self.adapter.config_version,
                expected_state_version=0,
                confirmation=ENABLE_SOURCE_MONITORING_ADAPTER,
                preview_sha256=preview["preview_sha256"],
            )
        self.assertEqual(
            caught.exception.code,
            "SOURCE_MONITORING_INITIAL_AUTHORITY_CONFLICT",
        )
        self.assertIsNone(self.repository.get_state(self.adapter.adapter_key))

    def test_completed_receipt_policy_mismatch_blocks_reenable(self) -> None:
        settings = self.settings()
        service = self.service(settings=settings)
        preview = service.preview(
            self.adapter.adapter_key,
            expected_config_version=self.adapter.config_version,
            expected_state_version=0,
        )
        service.set_enablement(
            self.adapter.adapter_key,
            enabled=True,
            expected_config_version=self.adapter.config_version,
            expected_state_version=0,
            confirmation=ENABLE_SOURCE_MONITORING_ADAPTER,
            preview_sha256=preview["preview_sha256"],
        )
        SourceMonitoringSupervisor(
            registry=self.registry,
            repository=self.repository,
            source_inbox=SourceInboxService(
                self.store,
                clock=lambda: self.clock[0] / 1_000,
            ),
            settings=settings,
            clock_ms=lambda: self.clock[0],
        ).run_once(self.adapter.adapter_key)
        state = self.repository.get_state(self.adapter.adapter_key)
        service.set_enablement(
            self.adapter.adapter_key,
            enabled=False,
            expected_config_version=self.adapter.config_version,
            expected_state_version=state["state_version"],
            confirmation=DISABLE_SOURCE_MONITORING_ADAPTER,
        )

        mismatched = self.service(
            settings=SourceMonitoringSettings(
                enabled=True,
                official_only=True,
                dry_run=False,
                max_items_per_run=50,
                initial_mode="seed_only",
            )
        )
        control = mismatched.control_snapshot()["adapters"][0]
        self.assertIn(
            "SOURCE_MONITORING_INITIAL_POLICY_MISMATCH",
            control["blocked_reason_codes"],
        )
        self.assertFalse(control["can_enable"])
        with self.assertRaises(SourceMonitoringOperatorError) as caught:
            mismatched.set_enablement(
                self.adapter.adapter_key,
                enabled=True,
                expected_config_version=self.adapter.config_version,
                expected_state_version=control["state_version"],
                confirmation=ENABLE_SOURCE_MONITORING_ADAPTER,
            )
        self.assertEqual(
            caught.exception.code,
            "SOURCE_MONITORING_INITIAL_POLICY_MISMATCH",
        )

        disabled_state = self.repository.get_state(self.adapter.adapter_key)
        self.repository.set_enabled(
            self.adapter.adapter_key,
            config_version=self.adapter.config_version,
            enabled=True,
            expected_state_version=disabled_state["state_version"],
        )
        enabled_state = self.repository.get_state(self.adapter.adapter_key)
        disabled = mismatched.set_enablement(
            self.adapter.adapter_key,
            enabled=False,
            expected_config_version=self.adapter.config_version,
            expected_state_version=enabled_state["state_version"],
            confirmation=DISABLE_SOURCE_MONITORING_ADAPTER,
        )
        self.assertFalse(disabled["persisted_enabled"])

    def test_poll_plan_and_state_failures_are_redacted_operator_errors(self) -> None:
        secret = "https://secret.example/?token=do-not-leak"

        def exit_with_secret(
            checkpoint: dict[str, object],
            *,
            observed_at_ms: int,
            etag: str = "",
            last_modified: str = "",
            max_items: int = 50,
        ) -> None:
            del checkpoint, observed_at_ms, etag, last_modified, max_items
            raise SystemExit(secret)

        self.adapter.poll = exit_with_secret  # type: ignore[method-assign]
        with self.assertRaises(SourceMonitoringOperatorError) as poll_error:
            self.service().preview(
                self.adapter.adapter_key,
                expected_config_version=self.adapter.config_version,
                expected_state_version=0,
            )
        self.assertEqual(
            poll_error.exception.code,
            "SOURCE_MONITORING_OPERATOR_PREVIEW_FAILED",
        )
        self.assertNotIn(secret, str(poll_error.exception))

        self.adapter = FakeAdapter("operator_fixture")
        self.registry = SourceAdapterRegistry((self.adapter,))
        with patch(
            "backend.source_monitoring.operator_service.plan_initial_poll",
            side_effect=RuntimeError(secret),
        ):
            with self.assertRaises(SourceMonitoringOperatorError) as plan_error:
                self.service().preview(
                    self.adapter.adapter_key,
                    expected_config_version=self.adapter.config_version,
                    expected_state_version=0,
                )
        self.assertEqual(
            plan_error.exception.code,
            "SOURCE_MONITORING_OPERATOR_PLAN_FAILED",
        )
        self.assertNotIn(secret, str(plan_error.exception))

        with patch.object(
            self.repository,
            "read_state_from_connection",
            side_effect=RuntimeError(secret),
        ):
            with self.assertRaises(SourceMonitoringOperatorError) as state_error:
                self.service().control_snapshot()
        self.assertEqual(
            state_error.exception.code,
            "SOURCE_MONITORING_OPERATOR_READ_FAILED",
        )
        self.assertNotIn(secret, str(state_error.exception))

    def test_completed_initialization_preview_is_rejected_before_poll(self) -> None:
        settings = self.settings()
        service = self.service(settings=settings)
        preview = service.preview(
            self.adapter.adapter_key,
            expected_config_version=self.adapter.config_version,
            expected_state_version=0,
        )
        service.set_enablement(
            self.adapter.adapter_key,
            enabled=True,
            expected_config_version=self.adapter.config_version,
            expected_state_version=0,
            confirmation=ENABLE_SOURCE_MONITORING_ADAPTER,
            preview_sha256=preview["preview_sha256"],
        )
        SourceMonitoringSupervisor(
            registry=self.registry,
            repository=self.repository,
            source_inbox=SourceInboxService(
                self.store,
                clock=lambda: self.clock[0] / 1_000,
            ),
            settings=settings,
            clock_ms=lambda: self.clock[0],
        ).run_once(self.adapter.adapter_key)
        state = self.repository.get_state(self.adapter.adapter_key)
        service.set_enablement(
            self.adapter.adapter_key,
            enabled=False,
            expected_config_version=self.adapter.config_version,
            expected_state_version=state["state_version"],
            confirmation=DISABLE_SOURCE_MONITORING_ADAPTER,
        )
        poll_count = self.adapter.poll_count

        with self.assertRaises(SourceMonitoringOperatorError) as caught:
            service.preview(
                self.adapter.adapter_key,
                expected_config_version=self.adapter.config_version,
                expected_state_version=self.repository.get_state(
                    self.adapter.adapter_key
                )["state_version"],
            )
        self.assertEqual(
            caught.exception.code,
            "SOURCE_MONITORING_INITIALIZATION_ALREADY_COMPLETE",
        )
        self.assertEqual(self.adapter.poll_count, poll_count)

    def test_pending_authorization_policy_drift_blocks_effective_run_not_disable(self) -> None:
        service = self.service()
        preview = service.preview(
            self.adapter.adapter_key,
            expected_config_version=self.adapter.config_version,
            expected_state_version=0,
        )
        service.set_enablement(
            self.adapter.adapter_key,
            enabled=True,
            expected_config_version=self.adapter.config_version,
            expected_state_version=0,
            confirmation=ENABLE_SOURCE_MONITORING_ADAPTER,
            preview_sha256=preview["preview_sha256"],
        )
        drifted = self.service(
            settings=SourceMonitoringSettings(
                enabled=True,
                official_only=True,
                dry_run=False,
                max_items_per_run=50,
                initial_mode="catch_up",
                catch_up_max_items=1,
            )
        )
        control = drifted.control_snapshot()["adapters"][0]
        self.assertIn(
            "SOURCE_MONITORING_PENDING_AUTHORIZATION_MISMATCH",
            control["blocked_reason_codes"],
        )
        self.assertFalse(control["effective_enabled"])
        self.assertTrue(control["can_disable"])

        disabled = drifted.set_enablement(
            self.adapter.adapter_key,
            enabled=False,
            expected_config_version=self.adapter.config_version,
            expected_state_version=control["state_version"],
            confirmation=DISABLE_SOURCE_MONITORING_ADAPTER,
        )
        self.assertFalse(disabled["persisted_enabled"])

    def test_config_mismatch_can_be_disabled_but_not_reenabled(self) -> None:
        state = self.repository.get_or_create_state(
            self.adapter.adapter_key,
            config_version=self.adapter.config_version,
        )
        state = self.repository.set_enabled(
            self.adapter.adapter_key,
            config_version=self.adapter.config_version,
            enabled=True,
            expected_state_version=state["state_version"],
        )
        replacement = FakeAdapter(self.adapter.adapter_key)
        replacement.config_version = f"{self.adapter.adapter_key}_config_v2"
        service = SourceMonitoringOperatorService(
            store=self.store,
            settings=self.settings(),
            registry=SourceAdapterRegistry((replacement,)),
            repository=self.repository,
            clock_ms=lambda: self.clock[0],
        )

        control = service.control_snapshot()["adapters"][0]
        self.assertIn(
            "SOURCE_MONITORING_CONFIG_MIGRATION_REQUIRED",
            control["blocked_reason_codes"],
        )
        self.assertTrue(control["can_disable"])
        disabled = service.set_enablement(
            replacement.adapter_key,
            enabled=False,
            expected_config_version=replacement.config_version,
            expected_state_version=state["state_version"],
            confirmation=DISABLE_SOURCE_MONITORING_ADAPTER,
        )
        self.assertFalse(disabled["persisted_enabled"])
        persisted = self.repository.get_state(replacement.adapter_key)
        self.assertEqual(persisted["config_version"], self.adapter.config_version)

        with self.assertRaises(SourceMonitoringOperatorError) as caught:
            service.set_enablement(
                replacement.adapter_key,
                enabled=True,
                expected_config_version=replacement.config_version,
                expected_state_version=persisted["state_version"],
                confirmation=ENABLE_SOURCE_MONITORING_ADAPTER,
            )
        self.assertEqual(
            caught.exception.code,
            "SOURCE_MONITORING_CONFIG_MIGRATION_REQUIRED",
        )


if __name__ == "__main__":
    unittest.main()
