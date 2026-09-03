"""Local, owner-exclusive operator commands for Source Monitoring.

The command surface is intentionally small.  It never starts an HTTP server,
accepts no database-path override, and emits only bounded machine-readable
summaries.  Production dependencies are imported only after the configured
database ownership lock has been acquired.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence, TextIO


SOURCE_MONITORING_CLI_VERSION = "source_monitoring_operator_cli_v1"
SOURCE_MONITORING_INSTANCE_ACTIVE = "SOURCE_MONITORING_INSTANCE_ACTIVE"
_CONFIRM_RUN_ONCE = "RUN_ONCE"
_ERROR_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")
_SUCCESS_STATUSES = frozenset({"SUCCEEDED", "DRY_RUN"})
_TERMINAL_CHECKPOINT_STATUSES = frozenset({"SUCCEEDED"})
_RUN_STATUSES = frozenset(
    {"SUCCEEDED", "DRY_RUN", "DEGRADED", "FAILED", "DRY_RUN_FAILED", "ABANDONED"}
)
_IMPORT_MODES = frozenset({"seed_only", "catch_up", "from_time", "continuous"})
_INITIALIZATION_OUTCOMES = frozenset(
    {
        "blocked",
        "not_required",
        "continuous_filter",
        "seeded",
        "initialized",
        "would_seed",
        "would_import",
        "not_committed",
        "unknown",
    }
)


class _CliContractError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _CliRunBoundaryError(RuntimeError):
    """A write-capable supervisor boundary failed with an unknown outcome."""


@dataclass(frozen=True, slots=True)
class SourceMonitoringCliDependencies:
    """Optional dependency seams used by isolated, network-free tests."""

    database_path: str | Path | None = None
    owner_factory: Callable[[str | Path], Any] | None = None
    preflight: Callable[[str | Path], Any] | None = None
    store_opener: Callable[[Path], Any] | None = None
    settings_loader: Callable[[], Any] | None = None
    registry_builder: Callable[[Any], Any] | None = None
    repository_builder: Callable[[Any], Any] | None = None
    health_builder: Callable[[Any, Any], Any] | None = None
    supervisor_builder: Callable[[Any, Any, Any, Any], Any] | None = None
    clock_ms: Callable[[], Any] | None = None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m backend.source_monitoring_cli")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="read the bounded monitoring health snapshot")
    preview = commands.add_parser("preview", help="preview one enabled adapter poll")
    preview.add_argument("adapter_key")
    run_once = commands.add_parser("run-once", help="run one enabled adapter immediately")
    run_once.add_argument("adapter_key")
    run_once.add_argument("--confirm", default="")
    return parser


def _emit(stream: TextIO, payload: dict[str, Any]) -> None:
    stream.write(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )


def _safe_error_code(exc: Exception, *, fallback: str) -> str:
    value = getattr(exc, "code", "")
    return value if type(value) is str and _ERROR_CODE_RE.fullmatch(value) else fallback


def _error_payload(command: str, code: str) -> dict[str, Any]:
    clean_code = code if _ERROR_CODE_RE.fullmatch(code) else "SOURCE_MONITORING_CLI_FAILED"
    return {
        "version": SOURCE_MONITORING_CLI_VERSION,
        "command": command,
        "ok": False,
        "error_code": clean_code,
        "safety": _zero_side_effect_safety(),
    }


def _zero_side_effect_safety() -> dict[str, Any]:
    return {
        "database_writes_performed": 0,
        "checkpoint_writes_performed": 0,
        "source_inbox_writes_performed": 0,
        "provider_calls_performed": 0,
        "model_calls_performed": 0,
        "network_requests_performed": 0,
        "formal_rounds_created": 0,
        "execution_capability": "none",
        "live_trading_allowed": False,
        "http_listener_started": False,
    }


def _runtime_safety(
    *,
    database_writes_performed: bool | None,
    checkpoint_writes_performed: bool | None,
    source_inbox_writes_performed: bool | None,
    market_calls_performed: int | None,
) -> dict[str, Any]:
    return {
        "database_writes_performed": database_writes_performed,
        "checkpoint_writes_performed": checkpoint_writes_performed,
        "provider_calls_performed": 0,
        "model_calls_performed": 0,
        "network_requests_performed": None,
        "network_requests_accounting": "not_instrumented",
        "market_calls_performed": market_calls_performed,
        "market_calls_accounting": (
            "exact" if type(market_calls_performed) is int else "unknown"
        ),
        "formal_rounds_created": 0,
        "source_inbox_writes_performed": source_inbox_writes_performed,
        "execution_capability": "none",
        "live_trading_allowed": False,
        "http_listener_started": False,
    }


def _database_path(dependencies: SourceMonitoringCliDependencies) -> str | Path:
    if dependencies.database_path is not None:
        return dependencies.database_path
    from .config import DATABASE_PATH

    return DATABASE_PATH


def _owner_factory(dependencies: SourceMonitoringCliDependencies) -> Callable[[str | Path], Any]:
    if dependencies.owner_factory is not None:
        return dependencies.owner_factory
    from .instance_ownership import DatabaseInstanceOwner

    return DatabaseInstanceOwner


def _open_verified_store(
    database_path: str | Path,
    dependencies: SourceMonitoringCliDependencies,
) -> Any:
    if dependencies.preflight is None:
        from .database_migration import assert_database_ready_for_startup

        preflight = assert_database_ready_for_startup
    else:
        preflight = dependencies.preflight
    preflight(database_path)

    if dependencies.store_opener is None:
        from .store import StudioStore

        opener = StudioStore._open_existing_schema
    else:
        opener = dependencies.store_opener
    return opener(Path(database_path))


def _settings(dependencies: SourceMonitoringCliDependencies) -> Any:
    if dependencies.settings_loader is not None:
        return dependencies.settings_loader()
    from .source_monitoring.settings import load_source_monitoring_settings

    return load_source_monitoring_settings()


def _registry(settings: Any, dependencies: SourceMonitoringCliDependencies) -> Any:
    if dependencies.registry_builder is not None:
        return dependencies.registry_builder(settings)
    from .source_monitoring.default_registry import (
        build_futu_anomaly_registry,
        build_official_source_registry,
    )

    return (
        build_official_source_registry()
        if settings.official_only
        else build_futu_anomaly_registry()
    )


def _repository(store: Any, dependencies: SourceMonitoringCliDependencies) -> Any:
    if dependencies.repository_builder is not None:
        return dependencies.repository_builder(store)
    from .source_monitoring.state_repository import SourceMonitoringStateRepository

    return SourceMonitoringStateRepository(store)


def _health(store: Any, settings: Any, dependencies: SourceMonitoringCliDependencies) -> dict[str, Any]:
    if dependencies.health_builder is not None:
        service = dependencies.health_builder(store, settings)
    else:
        from .source_monitoring.health_service import SourceMonitoringHealthService

        service = SourceMonitoringHealthService(store, settings=settings)
    snapshot = service.snapshot()
    if type(snapshot) is not dict:
        raise RuntimeError("invalid health snapshot")
    return snapshot


def _supervisor(
    store: Any,
    settings: Any,
    registry: Any,
    repository: Any,
    dependencies: SourceMonitoringCliDependencies,
) -> Any:
    if dependencies.supervisor_builder is not None:
        return dependencies.supervisor_builder(store, settings, registry, repository)
    from .source_inbox_service import SourceInboxService
    from .source_monitoring.supervisor import SourceMonitoringSupervisor
    from .source_monitoring.trading_impact_rules import TradingImpactRulesV1

    return SourceMonitoringSupervisor(
        registry=registry,
        repository=repository,
        source_inbox=SourceInboxService(store),
        settings=settings,
        impact_rules=(TradingImpactRulesV1() if settings.trading_impact_rules_enabled else None),
    )


def _now_ms(dependencies: SourceMonitoringCliDependencies) -> int:
    value = (
        dependencies.clock_ms()
        if dependencies.clock_ms is not None
        else int(time.time() * 1_000)
    )
    if type(value) is not int or value < 0:
        raise RuntimeError("invalid clock")
    return value


def _require_enabled_current(
    adapter_key: str,
    *,
    settings: Any,
    registry: Any,
    repository: Any,
) -> tuple[Any, Any, dict[str, Any]]:
    adapter, metadata = _require_registered_adapter(
        adapter_key,
        settings=settings,
        registry=registry,
    )
    state = repository.get_state(metadata.adapter_key)
    _require_enabled_state(state, metadata=metadata)
    return adapter, metadata, state


def _require_registered_adapter(
    adapter_key: str,
    *,
    settings: Any,
    registry: Any,
) -> tuple[Any, Any]:
    if getattr(settings, "enabled", None) is not True:
        raise _CliContractError("SOURCE_MONITORING_DISABLED")
    adapter = registry.require(adapter_key)
    return adapter, registry.metadata_for(adapter.adapter_key)


def _require_enabled_state(state: Any, *, metadata: Any) -> None:
    if state is None or state.get("enabled") is not True:
        raise _CliContractError("SOURCE_MONITORING_ADAPTER_DISABLED")
    if state.get("config_version") != metadata.config_version:
        raise _CliContractError("SOURCE_MONITORING_CONFIG_CONFLICT")


def _bounded_status(snapshot: dict[str, Any]) -> dict[str, Any]:
    runtime = snapshot.get("runtime") if type(snapshot.get("runtime")) is dict else {}
    adapters_value = snapshot.get("adapters")
    adapters = []
    if type(adapters_value) is list:
        for row in adapters_value[:50]:
            if type(row) is not dict:
                continue
            adapters.append({
                "adapter_key": row.get("adapter_key") if type(row.get("adapter_key")) is str else "",
                "state": row.get("state") if type(row.get("state")) is str else "unknown",
                "persisted_enabled": row.get("persisted_enabled") is True,
                "config_status": row.get("config_status") if type(row.get("config_status")) is str else "unknown",
                "last_error_code": (
                    row.get("last_error_code")
                    if type(row.get("last_error_code")) is str
                    and _ERROR_CODE_RE.fullmatch(row.get("last_error_code"))
                    else ""
                ),
                "runtime_liveness_verified": row.get("runtime_liveness_verified") is True,
            })
    counts = snapshot.get("counts") if type(snapshot.get("counts")) is dict else {}
    bounded_counts = {
        key: value
        for key, value in counts.items()
        if type(key) is str and type(value) is int and not isinstance(value, bool) and value >= 0
    }
    return {
        "version": SOURCE_MONITORING_CLI_VERSION,
        "command": "status",
        "ok": True,
        "captured_at_ms": snapshot.get("captured_at_ms", 0),
        "state": snapshot.get("state") if type(snapshot.get("state")) is str else "unknown",
        "persistence_available": snapshot.get("persistence_available") is True,
        "counts": bounded_counts,
        "runtime": {
            "status": runtime.get("status") if type(runtime.get("status")) is str else "unknown",
            "started_at": runtime.get("started_at") if type(runtime.get("started_at")) is int else 0,
            "heartbeat_at": runtime.get("heartbeat_at") if type(runtime.get("heartbeat_at")) is int else 0,
            "last_loop_at": runtime.get("last_loop_at") if type(runtime.get("last_loop_at")) is int else 0,
            "active_adapter": runtime.get("active_adapter") if type(runtime.get("active_adapter")) is str else "",
            "next_due_at": runtime.get("next_due_at") if type(runtime.get("next_due_at")) is int else 0,
            "thread_alive": runtime.get("thread_alive") is True,
            "liveness_verified": runtime.get("liveness_verified") is True,
            "last_fatal_error_code": (
                runtime.get("last_fatal_error_code")
                if type(runtime.get("last_fatal_error_code")) is str
                and (
                    runtime.get("last_fatal_error_code") == ""
                    or _ERROR_CODE_RE.fullmatch(runtime.get("last_fatal_error_code"))
                )
                else ""
            ),
        },
        "adapters": adapters,
        "safety": _zero_side_effect_safety(),
    }


def _status(
    store: Any,
    dependencies: SourceMonitoringCliDependencies,
) -> tuple[int, dict[str, Any]]:
    settings = _settings(dependencies)
    return 0, _bounded_status(_health(store, settings, dependencies))


def _preview(
    store: Any,
    adapter_key: str,
    dependencies: SourceMonitoringCliDependencies,
) -> tuple[int, dict[str, Any]]:
    from .source_monitoring.contracts import AdapterPollResult
    from .source_monitoring.initialization import plan_initial_poll

    settings = _settings(dependencies)
    registry = _registry(settings, dependencies)
    repository = _repository(store, dependencies)
    adapter, metadata = _require_registered_adapter(
        adapter_key,
        settings=settings,
        registry=registry,
    )
    from .source_monitoring.health_service import (
        read_source_monitoring_adapter_evidence,
    )

    evidence = read_source_monitoring_adapter_evidence(
        store,
        metadata.adapter_key,
        config_version=metadata.config_version,
        repository=repository,
    )
    state = evidence["state"]
    _require_enabled_state(state, metadata=metadata)
    initialization = evidence["initialization"]
    if initialization is not None and (
        initialization.get("mode") != settings.initial_mode
        or initialization.get("catch_up_max_items") != settings.catch_up_max_items
        or initialization.get("from_time_ms") != settings.from_time_ms
    ):
        raise _CliContractError("SOURCE_MONITORING_INITIAL_POLICY_MISMATCH")
    initial_required = initialization is None and not bool(
        state.get("checkpoint") != {} or state.get("last_success_at_ms", 0) > 0
    )
    observed_at_ms = _now_ms(dependencies)
    result = adapter.poll(
        state["checkpoint"],
        observed_at_ms=observed_at_ms,
        etag=state["etag"],
        last_modified=state["last_modified"],
        max_items=settings.max_items_per_run,
    )
    if type(result) is not AdapterPollResult:
        raise _CliContractError("SOURCE_MONITORING_POLL_RESULT_INVALID")
    if result.adapter_key != metadata.adapter_key:
        raise _CliContractError("SOURCE_MONITORING_ADAPTER_RESULT_MISMATCH")
    from .source_monitoring.contracts import canonical_json

    if canonical_json(result.started_checkpoint) != canonical_json(state["checkpoint"]):
        raise _CliContractError("SOURCE_MONITORING_CHECKPOINT_START_MISMATCH")
    if result.market_calls_performed > metadata.max_market_calls_per_poll:
        raise _CliContractError("SOURCE_MONITORING_MARKET_CALL_BOUND_EXCEEDED")
    plan = plan_initial_poll(
        result,
        metadata=metadata,
        settings=settings,
        initial_required=initial_required,
        received_at_ms=max(_now_ms(dependencies), result.captured_at_ms),
    )
    preview = plan.public_preview()
    error_codes = [
        error.code
        for error in result.source_errors[:50]
        if type(error.code) is str and _ERROR_CODE_RE.fullmatch(error.code)
    ]
    payload = {
        "version": SOURCE_MONITORING_CLI_VERSION,
        "command": "preview",
        "ok": not (
            preview["initialization_blocked"]
            or bool(result.source_errors)
            or result.rejected_count > 0
        ),
        "adapter_key": metadata.adapter_key,
        "mode": preview["mode"],
        "initial_required": preview["initial_required"],
        "initialization_blocked": preview["initialization_blocked"],
        "candidate_count": preview["candidate_count"],
        "selected_count": preview["selected_count"],
        "skipped_count": preview["skipped_count"],
        "adapter_duplicate_count": preview["adapter_duplicate_count"],
        "rejected_count": preview["rejected_count"],
        "earliest_occurred_at": preview["earliest_occurred_at"],
        "latest_occurred_at": preview["latest_occurred_at"],
        "preview_sha256": preview["preview_sha256"],
        "starting_checkpoint_sha256": preview["starting_checkpoint_sha256"],
        "next_checkpoint_sha256": preview["next_checkpoint_sha256"],
        "error_codes": error_codes,
        "safety": {
            **_zero_side_effect_safety(),
            "network_requests_performed": None,
            "network_requests_accounting": "not_instrumented",
        },
    }
    return (0 if payload["ok"] else 2), payload


def _run_once(
    store: Any,
    adapter_key: str,
    confirmation: str,
    dependencies: SourceMonitoringCliDependencies,
) -> tuple[int, dict[str, Any]]:
    if confirmation != _CONFIRM_RUN_ONCE:
        return 2, _error_payload("run-once", "SOURCE_MONITORING_CONFIRMATION_REQUIRED")
    settings = _settings(dependencies)
    registry = _registry(settings, dependencies)
    repository = _repository(store, dependencies)
    _require_enabled_current(
        adapter_key,
        settings=settings,
        registry=registry,
        repository=repository,
    )
    try:
        result = _supervisor(
            store,
            settings,
            registry,
            repository,
            dependencies,
        ).run_once(adapter_key)
    except BaseException:
        raise _CliRunBoundaryError() from None
    if type(result) is not dict:
        raise RuntimeError("invalid supervisor result")
    status_value = result.get("status")
    status = (
        status_value
        if type(status_value) is str and status_value in _RUN_STATUSES
        else "FAILED"
    )
    run = result.get("run") if type(result.get("run")) is dict else {}
    initialization = (
        result.get("initialization")
        if type(result.get("initialization")) is dict
        else {}
    )
    import_result = result.get("import") if type(result.get("import")) is dict else None
    idempotent_replay = (
        import_result.get("idempotent_replay")
        if import_result is not None and type(import_result.get("idempotent_replay")) is bool
        else None
    )
    reported_inbox_writes = result.get("source_inbox_writes_performed", ...)
    inbox_writes = (
        reported_inbox_writes
        if reported_inbox_writes is None or type(reported_inbox_writes) is bool
        else bool(import_result is not None and idempotent_replay is not True)
    )
    dry_run = bool(getattr(settings, "dry_run", False))
    result_safety = result.get("safety") if type(result.get("safety")) is dict else {}
    market_calls_value = result_safety.get("market_calls_performed")
    market_calls_performed = (
        market_calls_value
        if type(market_calls_value) is int and 0 <= market_calls_value <= 50
        else None
    )
    error_code = result.get("error_code", "")
    if not (type(error_code) is str and (error_code == "" or _ERROR_CODE_RE.fullmatch(error_code))):
        error_code = "SOURCE_MONITORING_RUN_FAILED"
    import_mode_value = initialization.get("mode")
    import_mode = (
        import_mode_value
        if type(import_mode_value) is str and import_mode_value in _IMPORT_MODES
        else "continuous"
    )
    initialization_outcome_value = initialization.get("outcome")
    initialization_outcome = (
        initialization_outcome_value
        if type(initialization_outcome_value) is str
        and initialization_outcome_value in _INITIALIZATION_OUTCOMES
        else "unknown"
    )
    counts = {}
    for key in ("observed_count", "accepted_count", "duplicate_count", "rejected_count"):
        value = run.get(key, 0)
        counts[key] = value if type(value) is int and not isinstance(value, bool) and value >= 0 else 0
    payload = {
        "version": SOURCE_MONITORING_CLI_VERSION,
        "command": "run-once",
        "ok": status in _SUCCESS_STATUSES,
        "adapter_key": adapter_key,
        "status": status,
        "dry_run": dry_run,
        "import_mode": import_mode,
        "initialization_outcome": initialization_outcome,
        "checkpoint_committed": bool(
            not dry_run and status in _TERMINAL_CHECKPOINT_STATUSES
        ),
        "source_inbox_writes_performed": inbox_writes,
        "idempotent_replay": idempotent_replay,
        "counts": counts,
        "error_code": error_code,
        "safety": _runtime_safety(
            # A returned Supervisor result exists only after start_run wrote a
            # run receipt, even if terminal-state recording later failed.
            database_writes_performed=True,
            checkpoint_writes_performed=bool(
                not dry_run and status in _TERMINAL_CHECKPOINT_STATUSES
            ),
            source_inbox_writes_performed=inbox_writes,
            market_calls_performed=market_calls_performed,
        ),
    }
    return (0 if payload["ok"] else 2), payload


def _dispatch(
    arguments: argparse.Namespace,
    database_path: str | Path,
    dependencies: SourceMonitoringCliDependencies,
) -> tuple[int, dict[str, Any]]:
    if arguments.command == "run-once" and arguments.confirm != _CONFIRM_RUN_ONCE:
        return 2, _error_payload(
            "run-once",
            "SOURCE_MONITORING_CONFIRMATION_REQUIRED",
        )
    store = _open_verified_store(database_path, dependencies)
    if arguments.command == "status":
        return _status(store, dependencies)
    if arguments.command == "preview":
        return _preview(store, arguments.adapter_key, dependencies)
    return _run_once(
        store,
        arguments.adapter_key,
        arguments.confirm,
        dependencies,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    dependencies: SourceMonitoringCliDependencies | None = None,
    output: TextIO | None = None,
) -> int:
    """Execute one bounded command and return its process exit code."""

    arguments = _parser().parse_args(argv)
    resolved_dependencies = dependencies or SourceMonitoringCliDependencies()
    stream = output or sys.stdout
    database_path = _database_path(resolved_dependencies)

    # Importing and acquiring the OS owner happens before migration/store,
    # registry, repository, health, adapter, or supervisor resolution.
    from .instance_ownership import InstanceAlreadyRunning

    owner = None
    try:
        try:
            owner = _owner_factory(resolved_dependencies)(database_path)
            owner.acquire()
        except InstanceAlreadyRunning:
            _emit(stream, {"error_code": SOURCE_MONITORING_INSTANCE_ACTIVE})
            return 3

        try:
            exit_code, payload = _dispatch(
                arguments,
                database_path,
                resolved_dependencies,
            )
        except FileNotFoundError:
            exit_code = 2
            payload = _error_payload(
                arguments.command,
                "SOURCE_MONITORING_DATABASE_UNAVAILABLE",
            )
        except BaseException as exc:
            if not isinstance(exc, Exception):
                exit_code = 1
                payload = _error_payload(
                    arguments.command,
                    "SOURCE_MONITORING_CLI_UNEXPECTED",
                )
                exc = None
            if exc is None:
                pass
            else:
                from .database_migration import DatabaseMigrationRequired

                if isinstance(exc, _CliRunBoundaryError):
                    exit_code = 1
                    payload = {
                        **_error_payload(
                            arguments.command,
                            "SOURCE_MONITORING_CLI_UNEXPECTED",
                        ),
                        "safety": _runtime_safety(
                            database_writes_performed=None,
                            checkpoint_writes_performed=None,
                            source_inbox_writes_performed=None,
                            market_calls_performed=None,
                        ),
                    }
                elif isinstance(exc, DatabaseMigrationRequired):
                    exit_code = 2
                    payload = _error_payload(
                        arguments.command,
                        "SOURCE_MONITORING_DATABASE_MIGRATION_REQUIRED",
                    )
                else:
                    error_code = _safe_error_code(exc, fallback="")
                    exit_code = 2 if error_code else 1
                    payload = _error_payload(
                        arguments.command,
                        error_code or "SOURCE_MONITORING_CLI_UNEXPECTED",
                    )
    except BaseException:
        exit_code = 1
        payload = _error_payload(
            arguments.command,
            "SOURCE_MONITORING_CLI_UNEXPECTED",
        )
    finally:
        if owner is not None:
            try:
                owner.release()
            except BaseException:
                exit_code = 1
                payload = _error_payload(
                    arguments.command,
                    "SOURCE_MONITORING_OWNER_RELEASE_FAILED",
                )

    _emit(stream, payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "SOURCE_MONITORING_CLI_VERSION",
    "SOURCE_MONITORING_INSTANCE_ACTIVE",
    "SourceMonitoringCliDependencies",
    "main",
]
