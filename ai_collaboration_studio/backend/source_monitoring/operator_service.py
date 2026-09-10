"""Fail-closed local operator controls for Source Monitoring adapters."""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..source_poll_control import (
    SourcePollCancelled,
    SourcePollDeadlineExceeded,
    validate_source_poll_control,
)

from .contracts import (
    MAX_NATIVE_INTEGER,
    AdapterPollResult,
    SourceMonitoringContractError,
    canonical_json,
)
from .default_registry import (
    build_futu_anomaly_registry,
    build_official_source_registry,
)
from .health_service import (
    SourceMonitoringHealthServiceError,
    source_monitoring_read_only_snapshot,
)
from .initialization import build_static_seed_preview, plan_initial_poll, poll_for_initialization
from .registry import SourceAdapterRegistry
from .settings import (
    SourceMonitoringSettings,
    load_source_monitoring_settings,
)
from .state_repository import (
    SOURCE_MONITORING_PENDING_AUTHORIZATION_VERSION,
    SOURCE_MONITORING_PENDING_AUTHORIZATION_VERSION_V2,
    SourceMonitoringStateError,
    SourceMonitoringStateRepository,
)


SOURCE_MONITORING_OPERATOR_CONTROL_VERSION = "source_monitoring_operator_control_v2"
SOURCE_MONITORING_ADAPTER_CONTROL_VERSION = "source_monitoring_adapter_control_v1"
SOURCE_MONITORING_OPERATOR_PREVIEW_VERSION = "source_monitoring_operator_preview_v1"
SOURCE_MONITORING_STATIC_SEED_OPERATOR_PREVIEW_VERSION = (
    "source_monitoring_operator_static_seed_preview_v2"
)
SOURCE_MONITORING_ENABLEMENT_RESULT_VERSION = "source_monitoring_enablement_result_v1"
ENABLE_SOURCE_MONITORING_ADAPTER = "ENABLE_SOURCE_MONITORING_ADAPTER"
DISABLE_SOURCE_MONITORING_ADAPTER = "DISABLE_SOURCE_MONITORING_ADAPTER"
SOURCE_MONITORING_OPERATOR_PREVIEW_TIMEOUT_MS = 120_000

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class SourceMonitoringOperatorError(SourceMonitoringContractError):
    def __init__(self, code: str, message: str, *, status: int = 409) -> None:
        super().__init__(code, message)
        self.status = status


def _operator_error(code: str, message: str, *, status: int = 409) -> SourceMonitoringOperatorError:
    return SourceMonitoringOperatorError(code, message, status=status)


def _read_safety() -> dict[str, Any]:
    return {
        "database_writes_performed": 0,
        "checkpoint_writes_performed": 0,
        "source_inbox_writes_performed": 0,
        "provider_calls_performed": 0,
        "model_calls_performed": 0,
        "network_requests_performed": 0,
        "market_calls_performed": 0,
        "formal_rounds_created": 0,
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


class SourceMonitoringOperatorService:
    """Preview and toggle only code-registered adapters; never run the worker."""

    def __init__(
        self,
        *,
        store: Any,
        settings: SourceMonitoringSettings | None = None,
        registry: SourceAdapterRegistry | None = None,
        registry_catalog: tuple[SourceAdapterRegistry, ...] | None = None,
        repository: SourceMonitoringStateRepository | None = None,
        clock_ms: Callable[[], Any] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        resolved_settings = settings or load_source_monitoring_settings()
        if type(resolved_settings) is not SourceMonitoringSettings:
            raise _operator_error(
                "SOURCE_MONITORING_OPERATOR_SETTINGS_INVALID",
                "operator settings must be SourceMonitoringSettings",
                status=400,
            )
        if registry is not None and registry_catalog is not None:
            raise _operator_error(
                "SOURCE_MONITORING_OPERATOR_REGISTRY_INVALID",
                "provide registry or registry_catalog, not both",
                status=400,
            )
        if registry_catalog is None:
            if registry is not None:
                resolved_catalog = (registry,)
            else:
                resolved_catalog = tuple(
                    candidate
                    for enabled, candidate in (
                        (
                            resolved_settings.official_only,
                            build_official_source_registry(**({"source_profile": resolved_settings.source_profile} if resolved_settings.source_profile else {}))
                            if resolved_settings.official_only
                            else None,
                        ),
                        (
                            resolved_settings.allow_readonly_market,
                            build_futu_anomaly_registry()
                            if resolved_settings.allow_readonly_market
                            else None,
                        ),
                    )
                    if enabled and candidate is not None
                )
        else:
            resolved_catalog = registry_catalog
        if (
            type(resolved_catalog) is not tuple
            or not resolved_catalog
            or any(type(item) is not SourceAdapterRegistry for item in resolved_catalog)
        ):
            raise _operator_error(
                "SOURCE_MONITORING_OPERATOR_REGISTRY_INVALID",
                "operator registry catalog must contain exact registries",
                status=400,
            )
        from .profiles import require_profile_registry

        if resolved_settings.source_profile:
            if len(resolved_catalog) != 1:
                raise _operator_error("SOURCE_MONITORING_PROFILE_SCOPE_MISMATCH", "trial profile requires exactly its official registry", status=400)
            require_profile_registry(resolved_catalog[0], resolved_settings.source_profile)
        all_keys = [
            adapter_key
            for item in resolved_catalog
            for adapter_key in item.adapter_keys
        ]
        if len(all_keys) != len(set(all_keys)):
            raise _operator_error(
                "SOURCE_MONITORING_OPERATOR_REGISTRY_INVALID",
                "operator registry catalog contains duplicate adapter keys",
                status=400,
            )
        for item in resolved_catalog:
            if (
                item.official_only and not resolved_settings.official_only
            ) or (
                not item.official_only
                and not resolved_settings.allow_readonly_market
            ):
                raise _operator_error(
                    "SOURCE_MONITORING_SOURCE_MODE_MISMATCH",
                    "operator registry is disabled by monitoring settings",
                    status=400,
                )
        resolved_repository = repository or SourceMonitoringStateRepository(store)
        if type(resolved_repository) is not SourceMonitoringStateRepository:
            raise _operator_error(
                "SOURCE_MONITORING_OPERATOR_REPOSITORY_INVALID",
                "operator repository must be SourceMonitoringStateRepository",
                status=400,
            )
        if resolved_repository.store is not store:
            raise _operator_error(
                "SOURCE_MONITORING_OPERATOR_STORE_MISMATCH",
                "operator repository belongs to a different store",
                status=400,
            )
        if clock_ms is not None and not callable(clock_ms):
            raise _operator_error(
                "SOURCE_MONITORING_OPERATOR_CLOCK_INVALID",
                "operator clock must be callable",
                status=400,
            )
        try:
            _deadline, resolved_cancel_event = validate_source_poll_control(
                deadline_monotonic_ms=0,
                cancel_event=cancel_event,
            )
        except ValueError as exc:
            raise _operator_error(
                "SOURCE_MONITORING_OPERATOR_CANCEL_EVENT_INVALID",
                "operator cancel event is invalid",
                status=400,
            ) from exc
        self.store = store
        self.settings = resolved_settings
        self.registry_catalog = resolved_catalog
        self.registry = resolved_catalog[0] if len(resolved_catalog) == 1 else None
        self.repository = resolved_repository
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1_000))
        self._cancel_event = resolved_cancel_event

    def _now_ms(self) -> int:
        value = self._clock_ms()
        if type(value) is not int or not 0 <= value <= MAX_NATIVE_INTEGER:
            raise _operator_error(
                "SOURCE_MONITORING_OPERATOR_CLOCK_INVALID",
                "operator clock returned an invalid value",
                status=400,
            )
        return value

    def _metadata(self, adapter_key: Any) -> tuple[Any, Any]:
        last_error: SourceMonitoringContractError | None = None
        for registry in self.registry_catalog:
            try:
                adapter = registry.require(adapter_key)
                return adapter, registry.metadata_for(adapter.adapter_key)
            except SourceMonitoringContractError as exc:
                last_error = exc
        raise _operator_error(
            getattr(last_error, "code", "SOURCE_MONITORING_ADAPTER_NOT_FOUND"),
            "adapter is not registered",
            status=400,
        ) from last_error

    @staticmethod
    def _initial_seed_policy(adapter: Any) -> dict[str, Any]:
        policy_factory = getattr(adapter, "initial_seed_policy", None)
        if not callable(policy_factory):
            raise _operator_error(
                "SOURCE_MONITORING_INITIAL_SEED_POLICY_MISSING",
                "read-only market adapter lacks a static seed policy",
            )
        try:
            policy = policy_factory()
        except SourceMonitoringContractError as exc:
            raise _operator_error(
                exc.code,
                "read-only market seed policy is invalid",
            ) from exc
        except (KeyboardInterrupt, GeneratorExit):
            raise
        except BaseException as exc:
            raise _operator_error(
                "SOURCE_MONITORING_INITIAL_SEED_POLICY_INVALID",
                "read-only market seed policy is unavailable",
            ) from exc
        if type(policy) is not dict:
            raise _operator_error(
                "SOURCE_MONITORING_INITIAL_SEED_POLICY_INVALID",
                "read-only market seed policy is invalid",
            )
        return policy

    def _evidence(self, adapter_key: str, config_version: str) -> dict[str, Any]:
        path = Path(self.store.path)
        try:
            with self.store._lock:
                with source_monitoring_read_only_snapshot(path) as connection:
                    connection.execute("BEGIN")
                    state = self.repository.read_state_from_connection(
                        connection,
                        adapter_key,
                    )
                    initialization = (
                        self.repository.read_latest_successful_initialization_from_connection(
                            connection,
                            adapter_key,
                            config_version=config_version,
                        )
                    )
                    initialization_completed_at_ms = 0
                    if initialization is not None:
                        rows = connection.execute(
                            """SELECT * FROM source_adapter_runs
                                WHERE adapter_key=? AND initialization_mode<>''
                                ORDER BY completed_at_ms DESC,run_id DESC""",
                            (adapter_key,),
                        ).fetchall()
                        for row in rows:
                            run = self.repository._run_projection(row)
                            candidate = run["initialization"]
                            if (
                                candidate is not None
                                and candidate["config_version"] == config_version
                            ):
                                initialization_completed_at_ms = run["completed_at_ms"]
                                break
                    active_run = connection.execute(
                        "SELECT 1 FROM source_adapter_runs WHERE adapter_key=? AND status='RUNNING'",
                        (adapter_key,),
                    ).fetchone() is not None
        except (SourceMonitoringStateError, SourceMonitoringHealthServiceError) as exc:
            raise _operator_error(
                getattr(exc, "code", "SOURCE_MONITORING_OPERATOR_READ_FAILED"),
                "operator state evidence could not be read",
                status=getattr(exc, "status", 409),
            ) from exc
        except sqlite3.Error as exc:
            raise _operator_error(
                "SOURCE_MONITORING_OPERATOR_READ_FAILED",
                "operator state evidence could not be read",
            ) from exc
        except (KeyboardInterrupt, GeneratorExit):
            raise
        except BaseException as exc:
            raise _operator_error(
                "SOURCE_MONITORING_OPERATOR_READ_FAILED",
                "operator state evidence could not be read",
            ) from exc
        return {
            "state": state,
            "initialization": initialization,
            "initialization_completed_at_ms": initialization_completed_at_ms,
            "active_run": active_run,
        }

    def _context(
        self,
        adapter_key: Any,
        *,
        expected_config_version: Any,
        expected_state_version: Any,
        allow_persisted_config_mismatch: bool = False,
    ) -> tuple[Any, Any, dict[str, Any]]:
        if type(expected_config_version) is not str or not expected_config_version:
            raise _operator_error(
                "SOURCE_MONITORING_CONFIG_VERSION_REQUIRED",
                "expected_config_version is required",
                status=400,
            )
        if (
            type(expected_state_version) is not int
            or not 0 <= expected_state_version <= MAX_NATIVE_INTEGER
        ):
            raise _operator_error(
                "SOURCE_MONITORING_STATE_VERSION_INVALID",
                "expected_state_version must be a non-negative native integer",
                status=400,
            )
        adapter, metadata = self._metadata(adapter_key)
        if expected_config_version != metadata.config_version:
            raise _operator_error(
                "SOURCE_MONITORING_CONFIG_CONFLICT",
                "adapter code config differs from the expected version",
            )
        evidence = self._evidence(metadata.adapter_key, metadata.config_version)
        state = evidence["state"]
        actual_state_version = 0 if state is None else state["state_version"]
        if actual_state_version != expected_state_version:
            raise _operator_error(
                "SOURCE_MONITORING_STATE_CONFLICT",
                "adapter state changed before the operator action",
            )
        if (
            state is not None
            and state["config_version"] != metadata.config_version
            and not allow_persisted_config_mismatch
        ):
            raise _operator_error(
                "SOURCE_MONITORING_CONFIG_MIGRATION_REQUIRED",
                "persisted adapter config requires migration",
            )
        return adapter, metadata, evidence

    @staticmethod
    def _initialization_status(evidence: dict[str, Any]) -> str:
        state = evidence["state"]
        if evidence["initialization"] is not None:
            return "complete"
        if state is not None and (
            state["checkpoint"] != {} or state["last_success_at_ms"] > 0
        ):
            return "legacy"
        if state is not None and state["pending_initialization_authorization"] is not None:
            return "authorized"
        return "required"

    def _initialization_policy_mismatch(
        self,
        initialization: dict[str, Any] | None,
        *,
        official_source: bool,
    ) -> bool:
        policy = self.settings.initialization_policy_for(
            official_source=official_source,
        )
        return bool(
            initialization is not None
            and (
                initialization["mode"] != policy.mode
                or initialization["catch_up_max_items"]
                != policy.catch_up_max_items
                or initialization["from_time_ms"] != policy.initial_from_time_ms
            )
        )

    def _pending_authorization_mismatch(
        self,
        pending: dict[str, Any] | None,
        *,
        official_source: bool,
    ) -> bool:
        policy = self.settings.initialization_policy_for(
            official_source=official_source,
        )
        return bool(
            pending is not None
            and (
                pending["mode"] != policy.mode
                or pending["catch_up_max_items"]
                != policy.catch_up_max_items
                or pending["from_time_ms"] != policy.initial_from_time_ms
            )
        )

    def control_snapshot(self) -> dict[str, Any]:
        from .profiles import require_profile_registry, source_profile_manifest

        profile = source_profile_manifest(self.settings.source_profile)
        if profile is not None:
            require_profile_registry(self.registry_catalog[0], self.settings.source_profile)
        captured_at_ms = self._now_ms()
        adapters = []
        catalog = [
            (registry, adapter_key)
            for registry in self.registry_catalog
            for adapter_key in registry.adapter_keys
        ]
        for registry, adapter_key in sorted(catalog, key=lambda item: item[1]):
            metadata = registry.metadata_for(adapter_key)
            policy = self.settings.initialization_policy_for(
                official_source=metadata.official_source,
            )
            evidence = self._evidence(adapter_key, metadata.config_version)
            state = evidence["state"]
            initialization = evidence["initialization"]
            pending = (
                state["pending_initialization_authorization"]
                if state is not None
                else None
            )
            config_current = state is None or state["config_version"] == metadata.config_version
            persisted_enabled = bool(state is not None and state["enabled"])
            active_run = evidence["active_run"]
            status = self._initialization_status(evidence)
            policy_mismatch = self._initialization_policy_mismatch(
                initialization,
                official_source=metadata.official_source,
            )
            pending_mismatch = self._pending_authorization_mismatch(
                pending,
                official_source=metadata.official_source,
            )
            blocked = []
            if not self.settings.enabled:
                blocked.append("SOURCE_MONITORING_DISABLED")
            if not config_current:
                blocked.append("SOURCE_MONITORING_CONFIG_MIGRATION_REQUIRED")
            if active_run:
                blocked.append("SOURCE_MONITORING_RUN_ACTIVE")
            if policy_mismatch:
                blocked.append("SOURCE_MONITORING_INITIAL_POLICY_MISMATCH")
            if pending_mismatch:
                blocked.append("SOURCE_MONITORING_PENDING_AUTHORIZATION_MISMATCH")
            if persisted_enabled:
                blocked.append("SOURCE_MONITORING_ADAPTER_ENABLED")
            initialization_mode = ""
            preview_sha256 = ""
            if initialization is not None:
                initialization_mode = initialization["mode"]
                preview_sha256 = initialization["preview_sha256"]
            elif pending is not None:
                initialization_mode = pending["mode"]
                preview_sha256 = pending["preview_sha256"]
            elif status == "required":
                initialization_mode = policy.mode
            adapters.append({
                "version": SOURCE_MONITORING_ADAPTER_CONTROL_VERSION,
                "adapter_key": adapter_key,
                "config_version": metadata.config_version,
                "state_version": 0 if state is None else state["state_version"],
                "persisted_state": state is not None,
                "persisted_enabled": persisted_enabled,
                "effective_enabled": bool(
                    self.settings.enabled
                    and persisted_enabled
                    and config_current
                    and not policy_mismatch
                    and not pending_mismatch
                ),
                "active_run": active_run,
                "source_class": metadata.source_class,
                "source_channel": metadata.source_channel,
                "official_source": metadata.official_source,
                "initialization_status": status,
                "initialization_mode": initialization_mode,
                "initialization_preview_sha256": preview_sha256,
                "initialization_completed_at_ms": evidence[
                    "initialization_completed_at_ms"
                ],
                "pending_authorization": pending is not None,
                "can_preview": bool(
                    self.settings.enabled
                    and config_current
                    and not persisted_enabled
                    and not active_run
                    and status == "required"
                ),
                "can_enable": bool(
                    self.settings.enabled
                    and config_current
                    and not persisted_enabled
                    and not active_run
                    and not policy_mismatch
                    and not pending_mismatch
                ),
                "can_disable": bool(persisted_enabled and not active_run),
                "blocked_reason_codes": sorted(set(blocked)),
            })
        return {
            "version": "source_monitoring_operator_control_v3" if profile is not None else SOURCE_MONITORING_OPERATOR_CONTROL_VERSION,
            **({"profile": profile} if profile is not None else {}),
            "captured_at_ms": captured_at_ms,
            "settings": {
                "global_enabled": self.settings.enabled,
                "auto_start": self.settings.auto_start,
                "dry_run": self.settings.dry_run,
                "initial_mode": self.settings.initial_mode,
                "catch_up_max_items": self.settings.catch_up_max_items,
                "from_time": self.settings.from_time,
                "continuous_event_cutoff": self.settings.continuous_event_cutoff,
            },
            "adapters": adapters,
            "safety": _read_safety(),
        }

    def preview(
        self,
        adapter_key: Any,
        *,
        expected_config_version: Any,
        expected_state_version: Any,
    ) -> dict[str, Any]:
        if not self.settings.enabled:
            raise _operator_error(
                "SOURCE_MONITORING_DISABLED",
                "source monitoring is globally disabled",
            )
        adapter, metadata, evidence = self._context(
            adapter_key,
            expected_config_version=expected_config_version,
            expected_state_version=expected_state_version,
        )
        state = evidence["state"]
        if state is not None and state["enabled"]:
            raise _operator_error(
                "SOURCE_MONITORING_ADAPTER_ENABLED",
                "disable the adapter before initial preview",
            )
        if evidence["active_run"]:
            raise _operator_error(
                "SOURCE_MONITORING_RUN_ACTIVE",
                "adapter has an active run",
            )
        initialization = evidence["initialization"]
        legacy_initialized = bool(
            state is not None
            and (state["checkpoint"] != {} or state["last_success_at_ms"] > 0)
        )
        initial_required = initialization is None and not legacy_initialized
        policy = self.settings.initialization_policy_for(
            official_source=metadata.official_source,
        )
        if not initial_required:
            raise _operator_error(
                "SOURCE_MONITORING_INITIALIZATION_ALREADY_COMPLETE",
                "initialization preview is unavailable after initialization completes",
            )
        checkpoint = {} if state is None else state["checkpoint"]
        observed_at_ms = self._now_ms()
        if metadata.official_source is False:
            try:
                static_preview = build_static_seed_preview(
                    metadata=metadata,
                    initialization_policy=policy,
                    initial_seed_policy=self._initial_seed_policy(adapter),
                    starting_checkpoint=checkpoint,
                )
            except SourceMonitoringContractError as exc:
                raise _operator_error(
                    exc.code,
                    "static seed preview failed its bounded contract",
                ) from exc
            return {
                "version": SOURCE_MONITORING_STATIC_SEED_OPERATOR_PREVIEW_VERSION,
                "preview_kind": "static_seed_policy",
                "candidate_evidence": "deferred_to_first_runtime_poll",
                "adapter_key": metadata.adapter_key,
                "config_version": metadata.config_version,
                "state_version": 0 if state is None else state["state_version"],
                "mode": policy.mode,
                "initial_required": True,
                "initialization_blocked": False,
                "catch_up_max_items": policy.catch_up_max_items,
                "from_time": policy.initial_from_time,
                "candidate_count": 0,
                "selected_count": 0,
                "skipped_count": 0,
                "adapter_duplicate_count": 0,
                "source_error_count": 0,
                "rejected_count": 0,
                "earliest_occurred_at": "",
                "latest_occurred_at": "",
                "preview_sha256": static_preview["preview_sha256"],
                "source_policy_sha256": static_preview["source_policy_sha256"],
                "symbol_allowlist": static_preview["symbol_allowlist"],
                "starting_checkpoint_sha256": static_preview[
                    "starting_checkpoint_sha256"
                ],
                "next_checkpoint_sha256": static_preview[
                    "starting_checkpoint_sha256"
                ],
                "captured_at_ms": observed_at_ms,
                "safety": {
                    **_read_safety(),
                    "network_requests_accounting": "exact",
                },
            }
        try:
            preview_deadline_monotonic_ms = (
                int(time.monotonic() * 1_000)
                + SOURCE_MONITORING_OPERATOR_PREVIEW_TIMEOUT_MS
            )
            result = poll_for_initialization(
                adapter,
                checkpoint,
                initial_required=initial_required,
                initialization_policy=policy,
                observed_at_ms=observed_at_ms,
                deadline_monotonic_ms=preview_deadline_monotonic_ms,
                cancel_event=self._cancel_event,
                etag="" if state is None else state["etag"],
                last_modified="" if state is None else state["last_modified"],
                max_items=self.settings.max_items_per_run,
            )
        except (SourcePollCancelled, SourcePollDeadlineExceeded) as exc:
            raise _operator_error(
                exc.code,
                "adapter preview was cancelled or exceeded its deadline",
            ) from exc
        except SourceMonitoringContractError as exc:
            raise _operator_error(
                exc.code,
                "adapter preview failed its bounded source contract",
            ) from exc
        except (KeyboardInterrupt, GeneratorExit):
            raise
        except BaseException as exc:
            raise _operator_error(
                "SOURCE_MONITORING_OPERATOR_PREVIEW_FAILED",
                "adapter preview failed",
            ) from exc
        if type(result) is not AdapterPollResult:
            raise _operator_error(
                "SOURCE_MONITORING_POLL_RESULT_INVALID",
                "adapter returned an invalid poll result",
            )
        if result.adapter_key != metadata.adapter_key:
            raise _operator_error(
                "SOURCE_MONITORING_ADAPTER_RESULT_MISMATCH",
                "poll result adapter identity changed",
            )
        if canonical_json(result.started_checkpoint) != canonical_json(checkpoint):
            raise _operator_error(
                "SOURCE_MONITORING_CHECKPOINT_START_MISMATCH",
                "poll result checkpoint differs from the persisted state",
            )
        if result.market_calls_performed > metadata.max_market_calls_per_poll:
            raise _operator_error(
                "SOURCE_MONITORING_MARKET_CALL_BOUND_EXCEEDED",
                "adapter exceeded its sealed read-only market call bound",
            )
        try:
            plan = plan_initial_poll(
                result,
                metadata=metadata,
                initialization_policy=policy,
                initial_required=initial_required,
                received_at_ms=max(self._now_ms(), result.captured_at_ms),
            )
        except SourceMonitoringContractError as exc:
            raise _operator_error(
                exc.code,
                "initialization preview failed its bounded contract",
            ) from exc
        except (KeyboardInterrupt, GeneratorExit):
            raise
        except BaseException as exc:
            raise _operator_error(
                "SOURCE_MONITORING_OPERATOR_PLAN_FAILED",
                "initialization preview planning failed",
            ) from exc
        preview = plan.public_preview()
        return {
            "version": SOURCE_MONITORING_OPERATOR_PREVIEW_VERSION,
            "adapter_key": metadata.adapter_key,
            "config_version": metadata.config_version,
            "state_version": 0 if state is None else state["state_version"],
            "mode": preview["mode"],
            "initial_required": preview["initial_required"],
            "initialization_blocked": bool(
                preview["initialization_blocked"]
                or result.source_errors
                or result.rejected_count > 0
            ),
            "catch_up_max_items": preview["catch_up_max_items"],
            "from_time": self.settings.from_time,
            "candidate_count": preview["candidate_count"],
            "selected_count": preview["selected_count"],
            "skipped_count": preview["skipped_count"],
            "adapter_duplicate_count": preview["adapter_duplicate_count"],
            "source_error_count": preview["source_error_count"],
            "rejected_count": preview["rejected_count"],
            "earliest_occurred_at": preview["earliest_occurred_at"],
            "latest_occurred_at": preview["latest_occurred_at"],
            "preview_sha256": preview["preview_sha256"],
            "starting_checkpoint_sha256": preview["starting_checkpoint_sha256"],
            "next_checkpoint_sha256": preview["next_checkpoint_sha256"],
            "captured_at_ms": preview["captured_at_ms"],
            "safety": {
                **_read_safety(),
                "network_requests_performed": None,
                "network_requests_accounting": "not_instrumented",
                "market_calls_performed": result.market_calls_performed,
            },
        }

    def set_enablement(
        self,
        adapter_key: Any,
        *,
        enabled: Any,
        expected_config_version: Any,
        expected_state_version: Any,
        confirmation: Any,
        preview_sha256: Any = "",
    ) -> dict[str, Any]:
        if type(enabled) is not bool:
            raise _operator_error(
                "SOURCE_MONITORING_ENABLEMENT_INVALID",
                "enabled must be a native boolean",
                status=400,
            )
        expected_confirmation = (
            ENABLE_SOURCE_MONITORING_ADAPTER
            if enabled
            else DISABLE_SOURCE_MONITORING_ADAPTER
        )
        if confirmation != expected_confirmation:
            raise _operator_error(
                "SOURCE_MONITORING_CONFIRMATION_REQUIRED",
                "exact source monitoring confirmation is required",
                status=400,
            )
        adapter, metadata, evidence = self._context(
            adapter_key,
            expected_config_version=expected_config_version,
            expected_state_version=expected_state_version,
            allow_persisted_config_mismatch=not enabled,
        )
        state = evidence["state"]
        initialization = evidence["initialization"]
        legacy_initialized = bool(
            state is not None
            and (state["checkpoint"] != {} or state["last_success_at_ms"] > 0)
        )
        initial_required = initialization is None and not legacy_initialized
        policy = self.settings.initialization_policy_for(
            official_source=metadata.official_source,
        )
        initialization_authorized = False
        confirmed_preview = ""
        market_calls_performed = 0
        network_requests_performed: int | None = 0
        if enabled:
            if self._initialization_policy_mismatch(
                initialization,
                official_source=metadata.official_source,
            ):
                raise _operator_error(
                    "SOURCE_MONITORING_INITIAL_POLICY_MISMATCH",
                    "configured initial policy differs from the sealed adapter receipt",
                )
            if not self.settings.enabled:
                raise _operator_error(
                    "SOURCE_MONITORING_DISABLED",
                    "source monitoring is globally disabled",
                )
            if state is not None and state["enabled"]:
                raise _operator_error(
                    "SOURCE_MONITORING_ADAPTER_ENABLED",
                    "adapter is already enabled",
                )
            if initial_required:
                if type(preview_sha256) is not str or _SHA256_RE.fullmatch(preview_sha256) is None:
                    raise _operator_error(
                        "SOURCE_MONITORING_INITIAL_PREVIEW_INVALID",
                        "a lowercase initial preview SHA-256 is required",
                        status=400,
                    )
                verified_preview = self.preview(
                    metadata.adapter_key,
                    expected_config_version=metadata.config_version,
                    expected_state_version=expected_state_version,
                )
                if verified_preview["initialization_blocked"]:
                    raise _operator_error(
                        "SOURCE_MONITORING_INITIALIZATION_BLOCKED",
                        "initialization preview contains source errors or rejections",
                    )
                if verified_preview["preview_sha256"] != preview_sha256:
                    raise _operator_error(
                        "SOURCE_MONITORING_INITIAL_PREVIEW_MISMATCH",
                        "source evidence changed after operator preview",
                    )
                if (
                    policy.mode == "catch_up"
                    and policy.initial_preview_sha256
                    and policy.initial_preview_sha256 != preview_sha256
                ):
                    raise _operator_error(
                        "SOURCE_MONITORING_INITIAL_AUTHORITY_CONFLICT",
                        "environment and UI catch-up authorities conflict",
                    )
                static_seed = verified_preview.get("preview_kind") == "static_seed_policy"
                authorization = {
                    "version": (
                        SOURCE_MONITORING_PENDING_AUTHORIZATION_VERSION_V2
                        if static_seed
                        else SOURCE_MONITORING_PENDING_AUTHORIZATION_VERSION
                    ),
                    "adapter_key": metadata.adapter_key,
                    "config_version": metadata.config_version,
                    "mode": policy.mode,
                    "catch_up_max_items": policy.catch_up_max_items,
                    "from_time_ms": policy.initial_from_time_ms,
                    "starting_checkpoint_sha256": verified_preview[
                        "starting_checkpoint_sha256"
                    ],
                    "preview_sha256": preview_sha256,
                    "confirmed_at_ms": self._now_ms(),
                }
                if static_seed:
                    authorization.update({
                        "authorization_kind": "static_seed_policy",
                        "source_policy_sha256": verified_preview[
                            "source_policy_sha256"
                        ],
                    })
                try:
                    updated = self.repository.authorize_initialization_and_enable(
                        metadata.adapter_key,
                        config_version=metadata.config_version,
                        expected_state_version=expected_state_version,
                        authorization=authorization,
                    )
                except SourceMonitoringStateError as exc:
                    raise _operator_error(
                        exc.code,
                        "adapter initialization authorization failed",
                    ) from exc
                except (KeyboardInterrupt, GeneratorExit):
                    raise
                except BaseException as exc:
                    raise _operator_error(
                        "SOURCE_MONITORING_OPERATOR_WRITE_FAILED",
                        "adapter initialization authorization failed",
                    ) from exc
                initialization_authorized = True
                confirmed_preview = preview_sha256
                market_calls_performed = verified_preview["safety"][
                    "market_calls_performed"
                ]
                network_requests_performed = verified_preview["safety"][
                    "network_requests_performed"
                ]
            else:
                if preview_sha256 != "":
                    raise _operator_error(
                        "SOURCE_MONITORING_INITIAL_PREVIEW_UNEXPECTED",
                        "initialized adapters do not accept an initial preview hash",
                        status=400,
                    )
                if state is None:  # guarded by initialization evidence/legacy relation
                    raise _operator_error(
                        "SOURCE_MONITORING_STATE_CONFLICT",
                        "initialized adapter state is unavailable",
                    )
                try:
                    updated = self.repository.set_enabled(
                        metadata.adapter_key,
                        config_version=metadata.config_version,
                        enabled=True,
                        expected_state_version=expected_state_version,
                    )
                except SourceMonitoringStateError as exc:
                    raise _operator_error(
                        exc.code,
                        "adapter enable failed",
                    ) from exc
                except (KeyboardInterrupt, GeneratorExit):
                    raise
                except BaseException as exc:
                    raise _operator_error(
                        "SOURCE_MONITORING_OPERATOR_WRITE_FAILED",
                        "adapter enable failed",
                    ) from exc
        else:
            del adapter
            if preview_sha256 != "":
                raise _operator_error(
                    "SOURCE_MONITORING_INITIAL_PREVIEW_UNEXPECTED",
                    "disable does not accept an initial preview hash",
                    status=400,
                )
            if state is None:
                raise _operator_error(
                    "SOURCE_MONITORING_STATE_NOT_FOUND",
                    "adapter state does not exist",
                )
            if not state["enabled"]:
                raise _operator_error(
                    "SOURCE_MONITORING_ADAPTER_DISABLED",
                    "adapter is already disabled",
                )
            try:
                updated = self.repository.set_enabled(
                    metadata.adapter_key,
                    config_version=state["config_version"],
                    enabled=False,
                    expected_state_version=expected_state_version,
                )
            except SourceMonitoringStateError as exc:
                raise _operator_error(exc.code, "adapter disable failed") from exc
            except (KeyboardInterrupt, GeneratorExit):
                raise
            except BaseException as exc:
                raise _operator_error(
                    "SOURCE_MONITORING_OPERATOR_WRITE_FAILED",
                    "adapter disable failed",
                ) from exc
        return {
            "version": SOURCE_MONITORING_ENABLEMENT_RESULT_VERSION,
            "adapter_key": metadata.adapter_key,
            "config_version": metadata.config_version,
            "state_version": updated["state_version"],
            "persisted_enabled": updated["enabled"],
            "initialization_authorized": initialization_authorized,
            "preview_sha256": confirmed_preview,
            "safety": {
                "database_writes_performed": True,
                "checkpoint_writes_performed": False,
                "source_inbox_writes_performed": False,
                "provider_calls_performed": 0,
                "model_calls_performed": 0,
                "network_requests_performed": network_requests_performed,
                "network_requests_accounting": (
                    "not_instrumented"
                    if network_requests_performed is None
                    else "exact"
                ),
                "market_calls_performed": market_calls_performed,
                "formal_rounds_created": 0,
                "execution_capability": "none",
                "live_trading_allowed": False,
            },
        }


__all__ = [
    "DISABLE_SOURCE_MONITORING_ADAPTER",
    "ENABLE_SOURCE_MONITORING_ADAPTER",
    "SOURCE_MONITORING_ADAPTER_CONTROL_VERSION",
    "SOURCE_MONITORING_ENABLEMENT_RESULT_VERSION",
    "SOURCE_MONITORING_OPERATOR_CONTROL_VERSION",
    "SOURCE_MONITORING_OPERATOR_PREVIEW_VERSION",
    "SOURCE_MONITORING_OPERATOR_PREVIEW_TIMEOUT_MS",
    "SOURCE_MONITORING_STATIC_SEED_OPERATOR_PREVIEW_VERSION",
    "SourceMonitoringOperatorError",
    "SourceMonitoringOperatorService",
]
