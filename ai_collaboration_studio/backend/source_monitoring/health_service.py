"""Read-only Source Monitoring health aggregation for local UI consumers."""

from __future__ import annotations

import re
import sqlite3
import shutil
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote

from .contracts import (
    MAX_NATIVE_INTEGER,
    OFFICIAL_SOURCE_CLASS,
    READONLY_MARKET_SOURCE_CLASS,
)
from .default_registry import (
    build_futu_anomaly_registry,
    build_official_source_registry,
)
from .health import project_monitoring_health
from .operations import (
    SourceMonitoringOperationsError,
    source_monitoring_operations_health,
)
from .runtime_state import (
    DEFAULT_RUNTIME_STALL_AFTER_MS,
    SOURCE_MONITORING_RUNTIME_HEALTH_VERSION,
    SOURCE_MONITORING_RUNTIME_STATUSES,
)
from .settings import SourceMonitoringSettings, load_source_monitoring_settings
from .state_repository import (
    SourceMonitoringStateRepository,
    source_monitoring_initialization_schema_state,
    source_monitoring_pending_authorization_schema_state,
)


SOURCE_MONITORING_HEALTH_SERVICE_VERSION = "source_monitoring_health_service_v2"

_RUNTIME_ID_RE = re.compile(r"source_monitor_runtime_[0-9a-f]{32}\Z")
_RUNTIME_ERROR_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,99}\Z")
_RUNTIME_ADAPTER_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class SourceMonitoringHealthServiceError(RuntimeError):
    def __init__(self, message: str, *, code: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _runtime_without_host(settings: SourceMonitoringSettings) -> dict[str, Any]:
    return {
        "version": SOURCE_MONITORING_RUNTIME_HEALTH_VERSION,
        "status": "disabled" if not settings.enabled else "stopped",
        "runtime_id": "",
        "started_at": 0,
        "heartbeat_at": 0,
        "last_loop_at": 0,
        "active_adapter": "",
        "next_due_at": 0,
        "thread_alive": False,
        "last_fatal_error_code": "",
        "heartbeat_age_ms": 0,
        "stall_after_ms": DEFAULT_RUNTIME_STALL_AFTER_MS,
        "liveness_verified": False,
        "enabled": settings.enabled,
        "auto_start": settings.auto_start,
        "dry_run": settings.dry_run,
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


def _validated_runtime_snapshot(
    value: Any,
    *,
    settings: SourceMonitoringSettings,
) -> dict[str, Any]:
    expected_fields = {
        "version",
        "status",
        "runtime_id",
        "started_at",
        "heartbeat_at",
        "last_loop_at",
        "active_adapter",
        "next_due_at",
        "thread_alive",
        "last_fatal_error_code",
        "heartbeat_age_ms",
        "stall_after_ms",
        "liveness_verified",
        "enabled",
        "auto_start",
        "dry_run",
        "execution_capability",
        "live_trading_allowed",
    }
    invalid = type(value) is not dict or set(value) != expected_fields
    if invalid:
        raise SourceMonitoringHealthServiceError(
            "Source Monitoring runtime health is invalid.",
            code="SOURCE_MONITORING_RUNTIME_HEALTH_INVALID",
        )
    status = value.get("status")
    runtime_id = value.get("runtime_id")
    active_adapter = value.get("active_adapter")
    fatal_code = value.get("last_fatal_error_code")
    integers = (
        "started_at",
        "heartbeat_at",
        "last_loop_at",
        "next_due_at",
        "heartbeat_age_ms",
        "stall_after_ms",
    )
    invalid = (
        value.get("version") != SOURCE_MONITORING_RUNTIME_HEALTH_VERSION
        or status not in SOURCE_MONITORING_RUNTIME_STATUSES
        or type(runtime_id) is not str
        or (
            bool(runtime_id)
            and _RUNTIME_ID_RE.fullmatch(runtime_id) is None
        )
        or (
            status in {"starting", "running", "degraded", "stalled", "stopping"}
            and not runtime_id
        )
        or type(active_adapter) is not str
        or (
            bool(active_adapter)
            and _RUNTIME_ADAPTER_KEY_RE.fullmatch(active_adapter) is None
        )
        or type(fatal_code) is not str
        or (
            bool(fatal_code)
            and _RUNTIME_ERROR_CODE_RE.fullmatch(fatal_code) is None
        )
        or any(
            type(value.get(field)) is not int
            or not 0 <= value[field] <= MAX_NATIVE_INTEGER
            for field in integers
        )
        or value.get("stall_after_ms", 0) < 1
        or any(
            type(value.get(field)) is not bool
            for field in (
                "thread_alive",
                "liveness_verified",
                "enabled",
                "auto_start",
                "dry_run",
                "live_trading_allowed",
            )
        )
        or value.get("enabled") is not settings.enabled
        or value.get("auto_start") is not settings.auto_start
        or value.get("dry_run") is not settings.dry_run
        or value.get("execution_capability") != "none"
        or value.get("live_trading_allowed") is not False
        or (
            value.get("liveness_verified") is True
            and (
                value.get("thread_alive") is not True
                or status not in {"running", "degraded"}
                or value["heartbeat_age_ms"] > value["stall_after_ms"]
            )
        )
        or (
            status in {"running", "degraded"}
            and (
                value.get("thread_alive") is not True
                or value["heartbeat_age_ms"] > value["stall_after_ms"]
            )
        )
        or (
            status == "stalled"
            and (
                value.get("thread_alive") is not True
                or value.get("liveness_verified") is not False
                or value["heartbeat_age_ms"] <= value["stall_after_ms"]
            )
        )
        or (
            status in {"disabled", "stopped"}
            and value.get("thread_alive") is not False
        )
        or (status in {"starting", "stopping"} and value.get("thread_alive") is not True)
        or (status == "failed" and not fatal_code)
        or (status != "failed" and bool(fatal_code))
        or (
            bool(active_adapter)
            and status not in {"running", "degraded", "stalled"}
        )
        or (status == "disabled" and bool(runtime_id))
    )
    if invalid:
        raise SourceMonitoringHealthServiceError(
            "Source Monitoring runtime health is invalid.",
            code="SOURCE_MONITORING_RUNTIME_HEALTH_INVALID",
        )
    return dict(value)


def _catalog_metadata() -> list[dict[str, Any]]:
    """Construct sealed adapter metadata without polling or opening transports."""

    rows = [
        *build_official_source_registry().to_dict()["adapters"],
        *build_futu_anomaly_registry().to_dict()["adapters"],
    ]
    rows.sort(key=lambda row: str(row.get("adapter_key") or ""))
    keys = [row.get("adapter_key") for row in rows]
    if len(rows) != 7 or any(type(key) is not str or not key for key in keys):
        raise SourceMonitoringHealthServiceError(
            "Source Monitoring adapter catalog is incomplete.",
            code="SOURCE_MONITORING_HEALTH_CATALOG_INVALID",
        )
    if len(set(keys)) != len(keys):
        raise SourceMonitoringHealthServiceError(
            "Source Monitoring adapter catalog contains duplicates.",
            code="SOURCE_MONITORING_HEALTH_CATALOG_INVALID",
        )
    return [dict(row) for row in rows]


def _latest_run_projection(run: dict[str, Any] | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "version": run["version"],
        "run_id": run["run_id"],
        "status": run["status"],
        "started_at_ms": run["started_at_ms"],
        "completed_at_ms": run["completed_at_ms"],
        "observed_count": run["observed_count"],
        "accepted_count": run["accepted_count"],
        "duplicate_count": run["duplicate_count"],
        "rejected_count": run["rejected_count"],
        "duration_ms": run["duration_ms"],
        "error_code": run["error_code"],
        "dry_run": run["dry_run"],
    }


def _connect_immutable_read_only(path: Path) -> sqlite3.Connection:
    uri_path = quote(path.resolve().as_posix(), safe="/:")
    connection = sqlite3.connect(
        f"file:{uri_path}?mode=ro&immutable=1",
        uri=True,
        timeout=10,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _connect_read_only(path: Path) -> sqlite3.Connection:
    uri_path = quote(path.resolve().as_posix(), safe="/:")
    connection = sqlite3.connect(
        f"file:{uri_path}?mode=ro",
        uri=True,
        timeout=10,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _file_signature(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_size, stat.st_mtime_ns, getattr(stat, "st_ino", 0)


@contextmanager
def source_monitoring_read_only_snapshot(path: Path):
    """Read a stable source snapshot without joining or mutating its WAL family."""

    journal_path = Path(f"{path}-journal")
    if journal_path.exists():
        raise SourceMonitoringHealthServiceError(
            "Source Monitoring persisted health snapshot is busy.",
            code="SOURCE_MONITORING_HEALTH_SNAPSHOT_BUSY",
        )
    wal_path = Path(f"{path}-wal")
    shm_path = Path(f"{path}-shm")
    source_family = (path, wal_path)
    signatures_before = tuple(_file_signature(member) for member in source_family)
    if not wal_path.exists() and not shm_path.exists():
        main_signature_before = signatures_before[0]
        with closing(_connect_immutable_read_only(path)) as connection:
            yield connection
        # A concurrent read may legitimately materialize an empty WAL/SHM family.
        # The immutable main-file view remains a valid point-in-time snapshot;
        # only a main-file change or rollback journal invalidates it.
        if (
            journal_path.exists()
            or _file_signature(path) != main_signature_before
        ):
            raise SourceMonitoringHealthServiceError(
                "Source Monitoring persisted health snapshot changed while reading.",
                code="SOURCE_MONITORING_HEALTH_SNAPSHOT_BUSY",
            )
        return

    try:
        with tempfile.TemporaryDirectory(
            prefix="ai-studio-source-monitor-health-snapshot-"
        ) as temporary_directory:
            snapshot_path = Path(temporary_directory) / "health.sqlite3"
            shutil.copyfile(path, snapshot_path)
            if signatures_before[1] is not None:
                shutil.copyfile(wal_path, Path(f"{snapshot_path}-wal"))
            signatures_after = tuple(
                _file_signature(member) for member in source_family
            )
            if journal_path.exists() or signatures_after != signatures_before:
                raise SourceMonitoringHealthServiceError(
                    "Source Monitoring persisted health snapshot changed while reading.",
                    code="SOURCE_MONITORING_HEALTH_SNAPSHOT_BUSY",
                )
            with closing(_connect_read_only(snapshot_path)) as connection:
                yield connection
    except SourceMonitoringHealthServiceError:
        raise
    except OSError as exc:
        raise SourceMonitoringHealthServiceError(
            "Source Monitoring persisted health snapshot could not be copied.",
            code="SOURCE_MONITORING_HEALTH_READ_FAILED",
        ) from exc


# Compatibility for the operations preflight's lazy import. New consumers use
# the public name above; retaining the alias avoids a cross-module flag day.
_health_snapshot_connection = source_monitoring_read_only_snapshot


def read_source_monitoring_adapter_evidence(
    store: Any,
    adapter_key: Any,
    *,
    config_version: Any,
    repository: SourceMonitoringStateRepository | None = None,
) -> dict[str, Any]:
    """Read one adapter's sealed state and initialization from one safe snapshot.

    The source database and its WAL/SHM family are never opened by SQLite.  If
    sidecars exist, main+WAL are copied first and SQLite joins only the
    disposable copy.
    """

    try:
        path = Path(store.path)
        lock = store._lock
    except (AttributeError, TypeError, ValueError) as exc:
        raise SourceMonitoringHealthServiceError(
            "Source Monitoring database path is invalid.",
            code="SOURCE_MONITORING_HEALTH_STORE_INVALID",
        ) from exc
    if not path.is_file():
        raise SourceMonitoringHealthServiceError(
            "Source Monitoring database is unavailable.",
            code="SOURCE_MONITORING_HEALTH_READ_FAILED",
        )
    resolved_repository = repository or SourceMonitoringStateRepository(store)
    try:
        with lock:
            with source_monitoring_read_only_snapshot(path) as connection:
                connection.execute("BEGIN")
                state = resolved_repository.read_state_from_connection(
                    connection,
                    adapter_key,
                )
                initialization = (
                    resolved_repository.read_latest_successful_initialization_from_connection(
                        connection,
                        adapter_key,
                        config_version=config_version,
                    )
                )
    except SourceMonitoringHealthServiceError:
        raise
    except sqlite3.Error as exc:
        raise SourceMonitoringHealthServiceError(
            "Source Monitoring adapter evidence could not be read.",
            code="SOURCE_MONITORING_HEALTH_READ_FAILED",
        ) from exc
    return {
        "state": state,
        "initialization": initialization,
    }


class SourceMonitoringHealthService:
    """Combine code metadata and persisted evidence without starting monitoring."""

    def __init__(
        self,
        store: Any,
        *,
        clock_ms: Callable[[], Any] | None = None,
        environment: Mapping[str, str] | None = None,
        settings: SourceMonitoringSettings | None = None,
        catalog: list[dict[str, Any]] | None = None,
        runtime_snapshot: Callable[[], Any] | None = None,
    ) -> None:
        self.store = store
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1_000))
        if settings is not None and type(settings) is not SourceMonitoringSettings:
            raise SourceMonitoringHealthServiceError(
                "Source Monitoring settings type is invalid.",
                code="SOURCE_MONITORING_HEALTH_SETTINGS_INVALID",
            )
        try:
            self.settings = settings or load_source_monitoring_settings(environment)
        except Exception as exc:
            raise SourceMonitoringHealthServiceError(
                "Source Monitoring settings are invalid.",
                code=getattr(exc, "code", "SOURCE_MONITORING_HEALTH_SETTINGS_INVALID"),
            ) from exc
        if catalog is not None and type(catalog) is not list:
            raise SourceMonitoringHealthServiceError(
                "Source Monitoring catalog type is invalid.",
                code="SOURCE_MONITORING_HEALTH_CATALOG_INVALID",
            )
        try:
            catalog_rows = _catalog_metadata() if catalog is None else catalog
            self._catalog = [dict(row) for row in catalog_rows]
        except SourceMonitoringHealthServiceError:
            raise
        except Exception as exc:
            raise SourceMonitoringHealthServiceError(
                "Source Monitoring adapter catalog could not be projected.",
                code="SOURCE_MONITORING_HEALTH_CATALOG_INVALID",
            ) from exc
        if runtime_snapshot is not None and not callable(runtime_snapshot):
            raise SourceMonitoringHealthServiceError(
                "Source Monitoring runtime snapshot provider is invalid.",
                code="SOURCE_MONITORING_RUNTIME_HEALTH_INVALID",
            )
        self._runtime_snapshot = runtime_snapshot

    def _now(self) -> int:
        value = self._clock_ms()
        if type(value) is not int or not 0 <= value <= MAX_NATIVE_INTEGER:
            raise SourceMonitoringHealthServiceError(
                "Source Monitoring health clock is invalid.",
                code="SOURCE_MONITORING_HEALTH_CLOCK_INVALID",
            )
        return value

    def _persisted_evidence(
        self,
    ) -> tuple[
        bool,
        list[dict[str, Any]],
        dict[str, dict[str, Any] | None],
        dict[str, dict[str, Any] | None],
        dict[str, Any],
    ]:
        operations = source_monitoring_operations_health(None)
        try:
            path = Path(self.store.path)
        except (AttributeError, TypeError, ValueError) as exc:
            raise SourceMonitoringHealthServiceError(
                "Source Monitoring database path is invalid.",
                code="SOURCE_MONITORING_HEALTH_STORE_INVALID",
            ) from exc
        if not path.is_file():
            return False, [], {}, {}, operations
        repository = SourceMonitoringStateRepository(self.store)
        try:
            with self.store._lock:
                # A fully checkpointed database is safe to open immutable. If a
                # legitimate WAL family exists, copy main+WAL under the store lock
                # and query only the disposable snapshot. The source SHM is never
                # joined, created, deleted, or mutated by this health read.
                with source_monitoring_read_only_snapshot(path) as connection:
                    connection.execute("BEGIN")
                    initialization_schema_status = (
                        source_monitoring_initialization_schema_state(connection)
                    )
                    pending_authorization_schema_status = (
                        source_monitoring_pending_authorization_schema_state(
                            connection
                        )
                    )
                    operations = source_monitoring_operations_health(connection)
                    if (
                        initialization_schema_status != "current"
                        or pending_authorization_schema_status != "current"
                    ):
                        operations = {
                            **operations,
                            "schema_status": "migration_required",
                        }
                    state_rows = connection.execute(
                        "SELECT * FROM source_adapter_states ORDER BY adapter_key"
                    ).fetchall()
                    states = [
                        repository._state_projection(row) for row in state_rows
                    ]
                    latest_runs = {}
                    initializations = {}
                    for state in states:
                        if initialization_schema_status == "current":
                            run_row = connection.execute(
                                """SELECT * FROM source_adapter_runs
                                    WHERE adapter_key=?
                                    ORDER BY started_at_ms DESC,run_id DESC LIMIT 1""",
                                (state["adapter_key"],),
                            ).fetchone()
                            latest_runs[state["adapter_key"]] = (
                                repository._run_projection(run_row)
                                if run_row is not None
                                else None
                            )
                            initializations[state["adapter_key"]] = (
                                repository.read_latest_successful_initialization_from_connection(
                                    connection,
                                    state["adapter_key"],
                                    config_version=state["config_version"],
                                )
                            )
                        else:
                            # Legacy run rows predate the initialization columns.
                            # Do not deserialize them as corrupt receipts while the
                            # additive migration is still pending.
                            latest_runs[state["adapter_key"]] = None
                            initializations[state["adapter_key"]] = None
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return False, [], {}, {}, source_monitoring_operations_health(None)
            raise SourceMonitoringHealthServiceError(
                "Source Monitoring persisted health evidence could not be read.",
                code="SOURCE_MONITORING_HEALTH_READ_FAILED",
            ) from exc
        except sqlite3.Error as exc:
            raise SourceMonitoringHealthServiceError(
                "Source Monitoring persisted health evidence could not be read.",
                code="SOURCE_MONITORING_HEALTH_READ_FAILED",
            ) from exc
        except SourceMonitoringOperationsError as exc:
            raise SourceMonitoringHealthServiceError(
                "Source Monitoring operations health evidence is invalid.",
                code=exc.code,
                status=exc.status,
            ) from exc
        return True, states, latest_runs, initializations, operations

    def snapshot(self) -> dict[str, Any]:
        captured_at_ms = self._now()
        try:
            runtime_value = (
                _runtime_without_host(self.settings)
                if self._runtime_snapshot is None
                else self._runtime_snapshot()
            )
            runtime = _validated_runtime_snapshot(
                runtime_value,
                settings=self.settings,
            )
        except SourceMonitoringHealthServiceError:
            raise
        except Exception as exc:
            raise SourceMonitoringHealthServiceError(
                "Source Monitoring runtime health could not be read.",
                code="SOURCE_MONITORING_RUNTIME_HEALTH_READ_FAILED",
            ) from exc
        (
            persistence_available,
            states,
            latest_runs,
            initializations,
            operations,
        ) = self._persisted_evidence()
        state_by_key = {state["adapter_key"]: state for state in states}
        metadata_by_key = {
            str(metadata.get("adapter_key") or ""): metadata
            for metadata in self._catalog
        }
        if "" in metadata_by_key or len(metadata_by_key) != len(self._catalog):
            raise SourceMonitoringHealthServiceError(
                "Source Monitoring catalog identities are invalid.",
                code="SOURCE_MONITORING_HEALTH_CATALOG_INVALID",
            )

        projection_states: list[dict[str, Any]] = []
        effective_enabled_by_key: dict[str, bool] = {}
        for adapter_key in sorted(set(metadata_by_key) | set(state_by_key)):
            state = state_by_key.get(adapter_key)
            metadata = metadata_by_key.get(adapter_key)
            source_class = (
                metadata.get("source_class") if metadata is not None else None
            )
            source_mode_enabled = (
                source_class == OFFICIAL_SOURCE_CLASS
                and self.settings.official_only
            ) or (
                source_class == READONLY_MARKET_SOURCE_CLASS
                and self.settings.allow_readonly_market
            )
            initialization = initializations.get(adapter_key)
            pending_authorization = (
                state.get("pending_initialization_authorization")
                if state is not None
                else None
            )
            initialization_policy_current = bool(
                initialization is None
                or (
                    initialization["mode"] == self.settings.initial_mode
                    and initialization["catch_up_max_items"]
                    == self.settings.catch_up_max_items
                    and initialization["from_time_ms"]
                    == self.settings.from_time_ms
                )
            )
            pending_authorization_policy_current = bool(
                pending_authorization is None
                or (
                    pending_authorization["mode"] == self.settings.initial_mode
                    and pending_authorization["catch_up_max_items"]
                    == self.settings.catch_up_max_items
                    and pending_authorization["from_time_ms"]
                    == self.settings.from_time_ms
                )
            )
            effective_enabled = bool(
                self.settings.enabled
                and metadata is not None
                and state is not None
                and state.get("enabled") is True
                and state.get("config_version") == metadata.get("config_version")
                and source_mode_enabled
                and initialization_policy_current
                and pending_authorization_policy_current
            )
            effective_enabled_by_key[adapter_key] = effective_enabled
            projection_states.append(
                {**state, "enabled": effective_enabled}
                if state is not None
                else {
                    "adapter_key": adapter_key,
                    "enabled": False,
                    "consecutive_failures": 0,
                    "last_started_at_ms": 0,
                    "last_success_at_ms": 0,
                    "last_event_at_ms": 0,
                    "next_due_at_ms": 0,
                    "last_error_code": "",
                }
            )
        active_adapter = runtime["active_adapter"]
        running_keys = (
            [active_adapter]
            if (
                runtime["liveness_verified"] is True
                and active_adapter
                and effective_enabled_by_key.get(active_adapter) is True
            )
            else []
        )
        projected = project_monitoring_health(
            projection_states,
            captured_at_ms,
            running_adapter_keys=running_keys,
        )
        projected_by_key = {
            adapter["adapter_key"]: adapter for adapter in projected["adapters"]
        }
        adapters = []
        for adapter_key in sorted(projected_by_key):
            metadata = metadata_by_key.get(adapter_key)
            state = state_by_key.get(adapter_key)
            persisted_config = state["config_version"] if state is not None else ""
            current_config = (
                str(metadata.get("config_version") or "")
                if metadata is not None
                else ""
            )
            config_status = (
                "absent"
                if state is None
                else "unregistered"
                if metadata is None
                else "current"
                if persisted_config == current_config
                else "migration_required"
            )
            adapters.append({
                **projected_by_key[adapter_key],
                "catalog_registered": metadata is not None,
                "persisted_state": state is not None,
                "persisted_enabled": (
                    state is not None and state.get("enabled") is True
                ),
                "config_status": config_status,
                "persisted_config_version": persisted_config,
                "metadata": (
                    {
                        "contract_version": metadata["contract_version"],
                        "config_version": metadata["config_version"],
                        "poll_interval_ms": metadata["poll_interval_ms"],
                        "max_candidates_per_poll": metadata["max_candidates_per_poll"],
                        "official_source": metadata["official_source"],
                        "source_class": metadata["source_class"],
                        "source_channel": metadata["source_channel"],
                        "max_market_calls_per_poll": metadata["max_market_calls_per_poll"],
                        "execution_capability": metadata["execution_capability"],
                        "live_trading_allowed": metadata["live_trading_allowed"],
                    }
                    if metadata is not None
                    else None
                ),
                "latest_run": _latest_run_projection(latest_runs.get(adapter_key)),
                "runtime_liveness_verified": bool(
                    runtime["liveness_verified"]
                    and effective_enabled_by_key.get(adapter_key) is True
                ),
            })
        return {
            "version": SOURCE_MONITORING_HEALTH_SERVICE_VERSION,
            "health_projection_version": projected["version"],
            "captured_at_ms": projected["captured_at_ms"],
            "state": projected["state"],
            "adapter_count": projected["adapter_count"],
            "counts": projected["counts"],
            "adapters": adapters,
            "settings": self.settings.to_dict(),
            "persistence_available": persistence_available,
            "runtime_liveness_verified": runtime["liveness_verified"],
            "runtime": runtime,
            "operations": operations,
            "safety": {
                "database_writes_performed": 0,
                "provider_calls_performed": 0,
                "network_requests_performed": 0,
                "market_calls_performed": 0,
                "formal_rounds_created": 0,
                "execution_capability": "none",
                "live_trading_allowed": False,
            },
        }


__all__ = [
    "SOURCE_MONITORING_HEALTH_SERVICE_VERSION",
    "SourceMonitoringHealthService",
    "SourceMonitoringHealthServiceError",
    "read_source_monitoring_adapter_evidence",
    "source_monitoring_read_only_snapshot",
]
