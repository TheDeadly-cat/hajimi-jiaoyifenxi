from __future__ import annotations

"""Persist source-adapter checkpoints and append-only run receipts.

The repository deliberately shares :class:`StudioStore`'s SQLite database but
does not initialize it at runtime.  ``ensure_source_monitoring_schema`` is
called only from the existing controlled schema initializer, so formal
databases continue to require the normal preview/prepare/apply migration gate.
"""

import json
import sqlite3
import time
import uuid
from contextlib import closing
from typing import TYPE_CHECKING, Any

from .contracts import (
    MAX_ETAG_CHARS,
    MAX_LAST_MODIFIED_CHARS,
    MAX_NATIVE_INTEGER,
    MAX_SOURCE_ERRORS_PER_POLL,
    SourceMonitoringContractError,
    SourcePollError,
    canonical_json,
    canonical_sha256,
    normalize_adapter_key,
    normalize_checkpoint,
)

if TYPE_CHECKING:
    from ..store import StudioStore


SOURCE_ADAPTER_STATE_VERSION = "source_adapter_state_v1"
SOURCE_ADAPTER_RUN_VERSION = "source_adapter_run_v1"
SOURCE_MONITORING_MIGRATION_KEY = "source_monitoring_state_v1"

RUN_STATUS_RUNNING = "RUNNING"
RUN_STATUS_SUCCEEDED = "SUCCEEDED"
RUN_STATUS_DEGRADED = "DEGRADED"
RUN_STATUS_FAILED = "FAILED"
RUN_STATUS_DRY_RUN = "DRY_RUN"
RUN_STATUS_ABANDONED = "ABANDONED"

RUN_STATUSES = frozenset({
    RUN_STATUS_RUNNING,
    RUN_STATUS_SUCCEEDED,
    RUN_STATUS_DEGRADED,
    RUN_STATUS_FAILED,
    RUN_STATUS_DRY_RUN,
    RUN_STATUS_ABANDONED,
})
RUN_COMPLETION_STATUSES = frozenset({
    RUN_STATUS_SUCCEEDED,
    RUN_STATUS_DEGRADED,
    RUN_STATUS_DRY_RUN,
})


class SourceMonitoringStateError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def ensure_source_monitoring_schema(
    connection: sqlite3.Connection,
    *,
    applied_at_ms: int,
) -> None:
    """Create the monitoring schema inside Studio's controlled initializer."""

    if (
        type(applied_at_ms) is not int
        or not 0 <= applied_at_ms <= MAX_NATIVE_INTEGER
    ):
        raise ValueError(
            "applied_at_ms must be a non-negative native signed 64-bit integer"
        )
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS source_adapter_states (
            adapter_key TEXT PRIMARY KEY,
            record_version TEXT NOT NULL
                CHECK(record_version='source_adapter_state_v1'),
            enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
            config_version TEXT NOT NULL,
            checkpoint_json TEXT NOT NULL DEFAULT '{}',
            checkpoint_sha256 TEXT NOT NULL CHECK(length(checkpoint_sha256)=64),
            etag TEXT NOT NULL DEFAULT '',
            last_modified TEXT NOT NULL DEFAULT '',
            last_started_at_ms INTEGER NOT NULL DEFAULT 0
                CHECK(last_started_at_ms>=0),
            last_success_at_ms INTEGER NOT NULL DEFAULT 0
                CHECK(last_success_at_ms>=0),
            last_event_at_ms INTEGER NOT NULL DEFAULT 0
                CHECK(last_event_at_ms>=0),
            next_due_at_ms INTEGER NOT NULL DEFAULT 0
                CHECK(next_due_at_ms>=0),
            consecutive_failures INTEGER NOT NULL DEFAULT 0
                CHECK(consecutive_failures>=0),
            last_error_code TEXT NOT NULL DEFAULT '',
            last_error_message TEXT NOT NULL DEFAULT '',
            state_version INTEGER NOT NULL DEFAULT 1 CHECK(state_version>0),
            updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=0)
        );

        CREATE TABLE IF NOT EXISTS source_adapter_runs (
            run_id TEXT PRIMARY KEY,
            record_version TEXT NOT NULL
                CHECK(record_version='source_adapter_run_v1'),
            adapter_key TEXT NOT NULL,
            started_state_version INTEGER NOT NULL CHECK(started_state_version>0),
            started_checkpoint_json TEXT NOT NULL,
            started_checkpoint_sha256 TEXT NOT NULL
                CHECK(length(started_checkpoint_sha256)=64),
            next_checkpoint_json TEXT NOT NULL DEFAULT '{}',
            next_checkpoint_sha256 TEXT NOT NULL DEFAULT '',
            started_at_ms INTEGER NOT NULL CHECK(started_at_ms>=0),
            completed_at_ms INTEGER NOT NULL DEFAULT 0 CHECK(completed_at_ms>=0),
            status TEXT NOT NULL CHECK(status IN (
                'RUNNING','SUCCEEDED','DEGRADED','FAILED','DRY_RUN','ABANDONED'
            )),
            observed_count INTEGER NOT NULL DEFAULT 0 CHECK(observed_count>=0),
            accepted_count INTEGER NOT NULL DEFAULT 0 CHECK(accepted_count>=0),
            duplicate_count INTEGER NOT NULL DEFAULT 0 CHECK(duplicate_count>=0),
            rejected_count INTEGER NOT NULL DEFAULT 0 CHECK(rejected_count>=0),
            duration_ms INTEGER NOT NULL DEFAULT 0 CHECK(duration_ms>=0),
            error_code TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            source_errors_json TEXT NOT NULL DEFAULT '[]',
            receipt_id TEXT NOT NULL DEFAULT '',
            dry_run INTEGER NOT NULL DEFAULT 0 CHECK(dry_run IN (0,1)),
            FOREIGN KEY(adapter_key) REFERENCES source_adapter_states(adapter_key)
        );

        CREATE INDEX IF NOT EXISTS idx_source_adapter_states_due
            ON source_adapter_states(enabled,next_due_at_ms,adapter_key);
        CREATE INDEX IF NOT EXISTS idx_source_adapter_runs_adapter_time
            ON source_adapter_runs(adapter_key,started_at_ms DESC,run_id DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_source_adapter_running
            ON source_adapter_runs(adapter_key) WHERE status='RUNNING';
        """
    )
    connection.execute(
        """INSERT OR IGNORE INTO schema_migrations(key,applied_at)
           VALUES(?,?)""",
        (SOURCE_MONITORING_MIGRATION_KEY, applied_at_ms),
    )


def _clean_token(value: Any, label: str, *, maximum: int = 160) -> str:
    if type(value) is not str:
        raise SourceMonitoringStateError(
            f"{label} must be a native string",
            code="SOURCE_MONITORING_STATE_INVALID",
        )
    clean = value.strip()
    if not clean or len(clean) > maximum or any(ord(char) < 32 for char in clean):
        raise SourceMonitoringStateError(
            f"{label} is invalid",
            code="SOURCE_MONITORING_STATE_INVALID",
        )
    return clean


def _clean_optional_text(value: Any, label: str, *, maximum: int) -> str:
    if type(value) is not str:
        raise SourceMonitoringStateError(
            f"{label} must be a native string",
            code="SOURCE_MONITORING_STATE_INVALID",
        )
    clean = value.strip()
    if (
        len(clean) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in clean)
    ):
        raise SourceMonitoringStateError(
            f"{label} is invalid or too long",
            code="SOURCE_MONITORING_STATE_INVALID",
        )
    return clean


def _native_non_negative(value: Any, label: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_NATIVE_INTEGER:
        raise SourceMonitoringStateError(
            f"{label} must be a non-negative native signed 64-bit integer",
            code="SOURCE_MONITORING_STATE_INVALID",
        )
    return value


def _normalize_source_error_records(value: Any) -> list[dict[str, str]]:
    if type(value) not in {list, tuple}:
        raise SourceMonitoringStateError(
            "source_errors must be a native list or tuple",
            code="SOURCE_MONITORING_STATE_INVALID",
        )
    if len(value) > MAX_SOURCE_ERRORS_PER_POLL:
        raise SourceMonitoringStateError(
            f"source_errors cannot exceed {MAX_SOURCE_ERRORS_PER_POLL} entries",
            code="SOURCE_MONITORING_STATE_INVALID",
        )
    normalized: list[dict[str, str]] = []
    for index, item in enumerate(value):
        try:
            if type(item) is SourcePollError:
                error = SourcePollError.build(item.code, item.message, item.scope)
            elif (
                type(item) is dict
                and all(type(key) is str for key in item)
                and set(item) == {"code", "message", "scope"}
            ):
                error = SourcePollError.build(
                    item["code"],
                    item["message"],
                    item["scope"],
                )
            else:
                raise SourceMonitoringStateError(
                    f"source_errors[{index}] is not an exact source error record",
                    code="SOURCE_MONITORING_STATE_INVALID",
                )
        except SourceMonitoringContractError as exc:
            raise SourceMonitoringStateError(
                f"source_errors[{index}] violates the source error contract",
                code="SOURCE_MONITORING_STATE_INVALID",
            ) from exc
        normalized.append(error.to_dict())
    return normalized


def _decode_json_object(value: Any, label: str) -> dict[str, Any]:
    if type(value) is not str:
        raise SourceMonitoringStateError(
            f"{label} is not stored text",
            code="SOURCE_MONITORING_RECORD_CORRUPT",
        )
    try:
        decoded = json.loads(value, parse_constant=lambda item: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON constant: {item}")
        ))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SourceMonitoringStateError(
            f"{label} is invalid JSON",
            code="SOURCE_MONITORING_RECORD_CORRUPT",
        ) from exc
    try:
        canonical = canonical_json(decoded)
        normalized = normalize_checkpoint(decoded)
    except SourceMonitoringContractError as exc:
        raise SourceMonitoringStateError(
            f"{label} violates the persisted JSON contract",
            code="SOURCE_MONITORING_RECORD_CORRUPT",
        ) from exc
    if type(decoded) is not dict or canonical != value:
        raise SourceMonitoringStateError(
            f"{label} is not a canonical JSON object",
            code="SOURCE_MONITORING_RECORD_CORRUPT",
        )
    return normalized


def _decode_json_array(value: Any, label: str) -> list[dict[str, Any]]:
    if type(value) is not str:
        raise SourceMonitoringStateError(
            f"{label} is not stored text",
            code="SOURCE_MONITORING_RECORD_CORRUPT",
        )
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SourceMonitoringStateError(
            f"{label} is invalid JSON",
            code="SOURCE_MONITORING_RECORD_CORRUPT",
        ) from exc
    try:
        canonical = canonical_json(decoded)
    except SourceMonitoringContractError as exc:
        raise SourceMonitoringStateError(
            f"{label} violates the persisted JSON contract",
            code="SOURCE_MONITORING_RECORD_CORRUPT",
        ) from exc
    if type(decoded) is not list or canonical != value:
        raise SourceMonitoringStateError(
            f"{label} is not a canonical JSON array",
            code="SOURCE_MONITORING_RECORD_CORRUPT",
        )
    if any(type(item) is not dict for item in decoded):
        raise SourceMonitoringStateError(
            f"{label} contains an invalid entry",
            code="SOURCE_MONITORING_RECORD_CORRUPT",
        )
    try:
        normalized = _normalize_source_error_records(decoded)
    except SourceMonitoringStateError as exc:
        raise SourceMonitoringStateError(
            f"{label} contains a source error outside the persisted contract",
            code="SOURCE_MONITORING_RECORD_CORRUPT",
        ) from exc
    if normalized != decoded:
        raise SourceMonitoringStateError(
            f"{label} is not a normalized source error array",
            code="SOURCE_MONITORING_RECORD_CORRUPT",
        )
    return normalized


def _row_mapping(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return dict(row)


class SourceMonitoringStateRepository:
    def __init__(
        self,
        store: StudioStore,
        *,
        clock_ms: Any = None,
    ) -> None:
        self.store = store
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def _now_ms(self) -> int:
        value = self._clock_ms()
        if (
            type(value) is not int
            or not 0 <= value <= MAX_NATIVE_INTEGER
        ):
            raise SourceMonitoringStateError(
                "monitoring clock must return a non-negative native signed 64-bit integer",
                code="SOURCE_MONITORING_CLOCK_INVALID",
            )
        return value

    @staticmethod
    def _state_projection(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        data = _row_mapping(row)
        checkpoint = _decode_json_object(data.get("checkpoint_json"), "checkpoint_json")
        enabled = data.get("enabled")
        if enabled not in {0, 1} or type(enabled) is not int:
            raise SourceMonitoringStateError(
                "source adapter enabled flag is corrupt",
                code="SOURCE_MONITORING_RECORD_CORRUPT",
            )
        if (
            data.get("record_version") != SOURCE_ADAPTER_STATE_VERSION
            or data.get("checkpoint_sha256") != canonical_sha256(checkpoint)
        ):
            raise SourceMonitoringStateError(
                "source adapter state seal is invalid",
                code="SOURCE_MONITORING_RECORD_CORRUPT",
            )
        projection = {
            "version": SOURCE_ADAPTER_STATE_VERSION,
            "adapter_key": normalize_adapter_key(data.get("adapter_key")),
            "enabled": enabled == 1,
            "config_version": _clean_token(data.get("config_version"), "config_version"),
            "checkpoint": checkpoint,
            "checkpoint_sha256": str(data.get("checkpoint_sha256") or ""),
            "etag": _clean_optional_text(
                data.get("etag"),
                "etag",
                maximum=MAX_ETAG_CHARS,
            ),
            "last_modified": _clean_optional_text(
                data.get("last_modified"),
                "last_modified",
                maximum=MAX_LAST_MODIFIED_CHARS,
            ),
            "last_started_at_ms": _native_non_negative(
                data.get("last_started_at_ms"),
                "last_started_at_ms",
            ),
            "last_success_at_ms": _native_non_negative(
                data.get("last_success_at_ms"),
                "last_success_at_ms",
            ),
            "last_event_at_ms": _native_non_negative(
                data.get("last_event_at_ms"),
                "last_event_at_ms",
            ),
            "next_due_at_ms": _native_non_negative(
                data.get("next_due_at_ms"),
                "next_due_at_ms",
            ),
            "consecutive_failures": _native_non_negative(
                data.get("consecutive_failures"),
                "consecutive_failures",
            ),
            "last_error_code": _clean_optional_text(
                data.get("last_error_code"),
                "last_error_code",
                maximum=160,
            ),
            "last_error_message": _clean_optional_text(
                data.get("last_error_message"),
                "last_error_message",
                maximum=500,
            ),
            "state_version": _native_non_negative(
                data.get("state_version"),
                "state_version",
            ),
            "updated_at_ms": _native_non_negative(
                data.get("updated_at_ms"),
                "updated_at_ms",
            ),
        }
        if projection["state_version"] < 1:
            raise SourceMonitoringStateError(
                "source adapter state version is invalid",
                code="SOURCE_MONITORING_RECORD_CORRUPT",
            )
        return projection

    @staticmethod
    def _run_projection(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        data = _row_mapping(row)
        started_checkpoint = _decode_json_object(
            data.get("started_checkpoint_json"),
            "started_checkpoint_json",
        )
        next_checkpoint_text = data.get("next_checkpoint_json")
        next_checkpoint = _decode_json_object(
            next_checkpoint_text,
            "next_checkpoint_json",
        )
        source_errors = _decode_json_array(
            data.get("source_errors_json"),
            "source_errors_json",
        )
        status = str(data.get("status") or "")
        dry_run = data.get("dry_run")
        if (
            data.get("record_version") != SOURCE_ADAPTER_RUN_VERSION
            or status not in RUN_STATUSES
            or type(dry_run) is not int
            or dry_run not in {0, 1}
            or data.get("started_checkpoint_sha256")
            != canonical_sha256(started_checkpoint)
            or (
                status != RUN_STATUS_RUNNING
                and data.get("next_checkpoint_sha256")
                != canonical_sha256(next_checkpoint)
            )
            or (
                status == RUN_STATUS_RUNNING
                and data.get("next_checkpoint_sha256") not in {"", canonical_sha256(next_checkpoint)}
            )
        ):
            raise SourceMonitoringStateError(
                "source adapter run seal is invalid",
                code="SOURCE_MONITORING_RECORD_CORRUPT",
            )
        projection = {
            "version": SOURCE_ADAPTER_RUN_VERSION,
            "run_id": _clean_token(data.get("run_id"), "run_id"),
            "adapter_key": normalize_adapter_key(data.get("adapter_key")),
            "started_state_version": _native_non_negative(
                data.get("started_state_version"),
                "started_state_version",
            ),
            "started_checkpoint": started_checkpoint,
            "started_checkpoint_sha256": str(data.get("started_checkpoint_sha256") or ""),
            "next_checkpoint": next_checkpoint,
            "next_checkpoint_sha256": str(data.get("next_checkpoint_sha256") or ""),
            "started_at_ms": _native_non_negative(data.get("started_at_ms"), "started_at_ms"),
            "completed_at_ms": _native_non_negative(
                data.get("completed_at_ms"),
                "completed_at_ms",
            ),
            "status": status,
            "observed_count": _native_non_negative(data.get("observed_count"), "observed_count"),
            "accepted_count": _native_non_negative(data.get("accepted_count"), "accepted_count"),
            "duplicate_count": _native_non_negative(
                data.get("duplicate_count"),
                "duplicate_count",
            ),
            "rejected_count": _native_non_negative(data.get("rejected_count"), "rejected_count"),
            "duration_ms": _native_non_negative(data.get("duration_ms"), "duration_ms"),
            "error_code": _clean_optional_text(data.get("error_code"), "error_code", maximum=160),
            "error_message": _clean_optional_text(
                data.get("error_message"),
                "error_message",
                maximum=500,
            ),
            "source_errors": source_errors,
            "receipt_id": _clean_optional_text(data.get("receipt_id"), "receipt_id", maximum=200),
            "dry_run": dry_run == 1,
        }
        if projection["started_state_version"] < 1:
            raise SourceMonitoringStateError(
                "source adapter run state version is invalid",
                code="SOURCE_MONITORING_RECORD_CORRUPT",
            )
        return projection

    @staticmethod
    def _insert_state(
        connection: sqlite3.Connection,
        *,
        adapter_key: str,
        config_version: str,
        now_ms: int,
    ) -> None:
        checkpoint: dict[str, Any] = {}
        checkpoint_json = canonical_json(checkpoint)
        connection.execute(
            """INSERT OR IGNORE INTO source_adapter_states(
                   adapter_key,record_version,enabled,config_version,
                   checkpoint_json,checkpoint_sha256,updated_at_ms
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                adapter_key,
                SOURCE_ADAPTER_STATE_VERSION,
                0,
                config_version,
                checkpoint_json,
                canonical_sha256(checkpoint),
                now_ms,
            ),
        )

    def get_or_create_state(
        self,
        adapter_key: Any,
        *,
        config_version: Any,
    ) -> dict[str, Any]:
        clean_key = normalize_adapter_key(adapter_key)
        clean_config = _clean_token(config_version, "config_version")
        timestamp = self._now_ms()
        with self.store._lock, closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._insert_state(
                connection,
                adapter_key=clean_key,
                config_version=clean_config,
                now_ms=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM source_adapter_states WHERE adapter_key=?",
                (clean_key,),
            ).fetchone()
            assert row is not None
            state = self._state_projection(row)
            if state["config_version"] != clean_config:
                raise SourceMonitoringStateError(
                    "adapter config version differs from persisted state",
                    code="SOURCE_MONITORING_CONFIG_CONFLICT",
                )
            return state

    def get_state(self, adapter_key: Any) -> dict[str, Any] | None:
        clean_key = normalize_adapter_key(adapter_key)
        with closing(self.store._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM source_adapter_states WHERE adapter_key=?",
                (clean_key,),
            ).fetchone()
            return self._state_projection(row) if row is not None else None

    def list_states(self) -> list[dict[str, Any]]:
        with closing(self.store._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM source_adapter_states ORDER BY adapter_key"
            ).fetchall()
            return [self._state_projection(row) for row in rows]

    def set_enabled(
        self,
        adapter_key: Any,
        *,
        config_version: Any,
        enabled: Any,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        if type(enabled) is not bool:
            raise SourceMonitoringStateError(
                "enabled must be a native boolean",
                code="SOURCE_MONITORING_STATE_INVALID",
            )
        if expected_state_version is not None and (
            type(expected_state_version) is not int or expected_state_version < 1
        ):
            raise SourceMonitoringStateError(
                "expected_state_version is invalid",
                code="SOURCE_MONITORING_STATE_INVALID",
            )
        clean_key = normalize_adapter_key(adapter_key)
        clean_config = _clean_token(config_version, "config_version")
        timestamp = self._now_ms()
        with self.store._lock, closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._insert_state(
                connection,
                adapter_key=clean_key,
                config_version=clean_config,
                now_ms=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM source_adapter_states WHERE adapter_key=?",
                (clean_key,),
            ).fetchone()
            assert row is not None
            state = self._state_projection(row)
            if state["config_version"] != clean_config:
                raise SourceMonitoringStateError(
                    "adapter config version differs from persisted state",
                    code="SOURCE_MONITORING_CONFIG_CONFLICT",
                )
            if expected_state_version is not None and state["state_version"] != expected_state_version:
                raise SourceMonitoringStateError(
                    "adapter state changed before enablement update",
                    code="SOURCE_MONITORING_STATE_CONFLICT",
                )
            if state["enabled"] != enabled:
                active = connection.execute(
                    "SELECT 1 FROM source_adapter_runs WHERE adapter_key=? AND status='RUNNING'",
                    (clean_key,),
                ).fetchone()
                if active is not None:
                    raise SourceMonitoringStateError(
                        "adapter enablement cannot change during an active run",
                        code="SOURCE_MONITORING_RUN_ACTIVE",
                    )
                next_due = timestamp if enabled else state["next_due_at_ms"]
                cursor = connection.execute(
                    """UPDATE source_adapter_states
                       SET enabled=?,next_due_at_ms=?,state_version=state_version+1,
                           updated_at_ms=?
                       WHERE adapter_key=? AND state_version=?""",
                    (
                        int(enabled),
                        next_due,
                        timestamp,
                        clean_key,
                        state["state_version"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise SourceMonitoringStateError(
                        "adapter state changed before enablement update",
                        code="SOURCE_MONITORING_STATE_CONFLICT",
                    )
            updated = connection.execute(
                "SELECT * FROM source_adapter_states WHERE adapter_key=?",
                (clean_key,),
            ).fetchone()
            assert updated is not None
            return self._state_projection(updated)

    def migrate_config(
        self,
        adapter_key: Any,
        *,
        expected_config_version: Any,
        new_config_version: Any,
        expected_state_version: Any,
        next_checkpoint: Any,
    ) -> dict[str, Any]:
        """Explicitly adopt a new adapter config while disabled.

        The caller must provide both the old config identity and the exact
        replacement checkpoint. Conditional request headers and failure state
        are reset because they may not be meaningful under the new config.
        """

        clean_key = normalize_adapter_key(adapter_key)
        expected_config = _clean_token(
            expected_config_version,
            "expected_config_version",
        )
        new_config = _clean_token(new_config_version, "new_config_version")
        if expected_config == new_config:
            raise SourceMonitoringStateError(
                "new_config_version must differ from expected_config_version",
                code="SOURCE_MONITORING_STATE_INVALID",
            )
        if type(expected_state_version) is not int or expected_state_version < 1:
            raise SourceMonitoringStateError(
                "expected_state_version is invalid",
                code="SOURCE_MONITORING_STATE_INVALID",
            )
        checkpoint = normalize_checkpoint(next_checkpoint)
        checkpoint_json = canonical_json(checkpoint)
        checkpoint_sha256 = canonical_sha256(checkpoint)
        timestamp = self._now_ms()
        with self.store._lock, closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM source_adapter_states WHERE adapter_key=?",
                (clean_key,),
            ).fetchone()
            if row is None:
                raise SourceMonitoringStateError(
                    "adapter state does not exist",
                    code="SOURCE_MONITORING_STATE_NOT_FOUND",
                )
            state = self._state_projection(row)
            if state["config_version"] != expected_config:
                raise SourceMonitoringStateError(
                    "adapter config version differs from the expected version",
                    code="SOURCE_MONITORING_CONFIG_CONFLICT",
                )
            if state["state_version"] != expected_state_version:
                raise SourceMonitoringStateError(
                    "adapter state changed before config migration",
                    code="SOURCE_MONITORING_STATE_CONFLICT",
                )
            if state["enabled"]:
                raise SourceMonitoringStateError(
                    "adapter must be disabled before config migration",
                    code="SOURCE_MONITORING_ADAPTER_ENABLED",
                )
            active = connection.execute(
                "SELECT 1 FROM source_adapter_runs WHERE adapter_key=? AND status='RUNNING'",
                (clean_key,),
            ).fetchone()
            if active is not None:
                raise SourceMonitoringStateError(
                    "adapter config cannot migrate during an active run",
                    code="SOURCE_MONITORING_RUN_ACTIVE",
                )
            cursor = connection.execute(
                """UPDATE source_adapter_states
                   SET config_version=?,checkpoint_json=?,checkpoint_sha256=?,
                       etag='',last_modified='',last_started_at_ms=0,
                       last_success_at_ms=0,last_event_at_ms=0,next_due_at_ms=0,
                       consecutive_failures=0,last_error_code='',last_error_message='',
                       state_version=state_version+1,updated_at_ms=?
                   WHERE adapter_key=? AND enabled=0 AND state_version=?
                         AND config_version=?""",
                (
                    new_config,
                    checkpoint_json,
                    checkpoint_sha256,
                    timestamp,
                    clean_key,
                    expected_state_version,
                    expected_config,
                ),
            )
            if cursor.rowcount != 1:
                raise SourceMonitoringStateError(
                    "adapter state changed before config migration",
                    code="SOURCE_MONITORING_STATE_CONFLICT",
                )
            updated = connection.execute(
                "SELECT * FROM source_adapter_states WHERE adapter_key=?",
                (clean_key,),
            ).fetchone()
            assert updated is not None
            return self._state_projection(updated)

    def start_run(
        self,
        adapter_key: Any,
        *,
        config_version: Any,
        dry_run: Any = False,
    ) -> dict[str, Any]:
        if type(dry_run) is not bool:
            raise SourceMonitoringStateError(
                "dry_run must be a native boolean",
                code="SOURCE_MONITORING_STATE_INVALID",
            )
        clean_key = normalize_adapter_key(adapter_key)
        clean_config = _clean_token(config_version, "config_version")
        timestamp = self._now_ms()
        run_id = f"source_run_{uuid.uuid4().hex}"
        with self.store._lock, closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._insert_state(
                connection,
                adapter_key=clean_key,
                config_version=clean_config,
                now_ms=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM source_adapter_states WHERE adapter_key=?",
                (clean_key,),
            ).fetchone()
            assert row is not None
            state = self._state_projection(row)
            if state["config_version"] != clean_config:
                raise SourceMonitoringStateError(
                    "adapter config version differs from persisted state",
                    code="SOURCE_MONITORING_CONFIG_CONFLICT",
                )
            if not state["enabled"]:
                raise SourceMonitoringStateError(
                    "adapter is disabled",
                    code="SOURCE_MONITORING_ADAPTER_DISABLED",
                )
            next_state_version = (
                state["state_version"] if dry_run else state["state_version"] + 1
            )
            checkpoint_json = canonical_json(state["checkpoint"])
            try:
                connection.execute(
                    """INSERT INTO source_adapter_runs(
                           run_id,record_version,adapter_key,started_state_version,
                           started_checkpoint_json,started_checkpoint_sha256,
                           next_checkpoint_json,started_at_ms,status,dry_run
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        SOURCE_ADAPTER_RUN_VERSION,
                        clean_key,
                        next_state_version,
                        checkpoint_json,
                        state["checkpoint_sha256"],
                        checkpoint_json,
                        timestamp,
                        RUN_STATUS_RUNNING,
                        int(dry_run),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise SourceMonitoringStateError(
                    "adapter already has an active run",
                    code="SOURCE_MONITORING_RUN_ACTIVE",
                ) from exc
            if not dry_run:
                cursor = connection.execute(
                    """UPDATE source_adapter_states
                       SET last_started_at_ms=?,state_version=?,updated_at_ms=?
                       WHERE adapter_key=? AND state_version=?""",
                    (
                        timestamp,
                        next_state_version,
                        timestamp,
                        clean_key,
                        state["state_version"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise SourceMonitoringStateError(
                        "adapter state changed before the run started",
                        code="SOURCE_MONITORING_STATE_CONFLICT",
                    )
            run_row = connection.execute(
                "SELECT * FROM source_adapter_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            state_row = connection.execute(
                "SELECT * FROM source_adapter_states WHERE adapter_key=?",
                (clean_key,),
            ).fetchone()
            assert run_row is not None and state_row is not None
            return {
                "run": self._run_projection(run_row),
                "state": self._state_projection(state_row),
            }

    @staticmethod
    def _run_for_update(
        connection: sqlite3.Connection,
        run_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        run_row = connection.execute(
            "SELECT * FROM source_adapter_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if run_row is None:
            raise SourceMonitoringStateError(
                "adapter run does not exist",
                code="SOURCE_MONITORING_RUN_NOT_FOUND",
            )
        run = SourceMonitoringStateRepository._run_projection(run_row)
        if run["status"] != RUN_STATUS_RUNNING:
            raise SourceMonitoringStateError(
                "adapter run is already terminal",
                code="SOURCE_MONITORING_RUN_TERMINAL",
            )
        state_row = connection.execute(
            "SELECT * FROM source_adapter_states WHERE adapter_key=?",
            (run["adapter_key"],),
        ).fetchone()
        if state_row is None:
            raise SourceMonitoringStateError(
                "adapter state is missing",
                code="SOURCE_MONITORING_RECORD_CORRUPT",
            )
        state = SourceMonitoringStateRepository._state_projection(state_row)
        if state["state_version"] != run["started_state_version"]:
            raise SourceMonitoringStateError(
                "adapter state changed while the run was active",
                code="SOURCE_MONITORING_STATE_CONFLICT",
            )
        return run, state

    def complete_run(
        self,
        run_id: Any,
        *,
        next_checkpoint: Any,
        status: str,
        observed_count: int,
        accepted_count: int,
        duplicate_count: int,
        rejected_count: int,
        next_due_at_ms: int,
        source_errors: Any = (),
        receipt_id: str = "",
        etag: str = "",
        last_modified: str = "",
        error_code: str = "",
        error_message: str = "",
    ) -> dict[str, Any]:
        clean_run_id = _clean_token(run_id, "run_id")
        if type(status) is not str or status not in RUN_COMPLETION_STATUSES:
            raise SourceMonitoringStateError(
                "completion status is invalid",
                code="SOURCE_MONITORING_STATE_INVALID",
            )
        counts = {
            "observed_count": _native_non_negative(observed_count, "observed_count"),
            "accepted_count": _native_non_negative(accepted_count, "accepted_count"),
            "duplicate_count": _native_non_negative(duplicate_count, "duplicate_count"),
            "rejected_count": _native_non_negative(rejected_count, "rejected_count"),
        }
        if (
            counts["accepted_count"]
            + counts["duplicate_count"]
            + counts["rejected_count"]
            > counts["observed_count"]
        ):
            raise SourceMonitoringStateError(
                "terminal item counts cannot exceed observed_count",
                code="SOURCE_MONITORING_STATE_INVALID",
            )
        next_due = _native_non_negative(next_due_at_ms, "next_due_at_ms")
        checkpoint = normalize_checkpoint(next_checkpoint)
        checkpoint_json = canonical_json(checkpoint)
        checkpoint_sha256 = canonical_sha256(checkpoint)
        normalized_errors = _normalize_source_error_records(source_errors)
        source_errors_json = canonical_json(normalized_errors)
        clean_receipt = _clean_optional_text(receipt_id, "receipt_id", maximum=200)
        clean_etag = _clean_optional_text(etag, "etag", maximum=MAX_ETAG_CHARS)
        clean_last_modified = _clean_optional_text(
            last_modified,
            "last_modified",
            maximum=MAX_LAST_MODIFIED_CHARS,
        )
        clean_error_code = _clean_optional_text(error_code, "error_code", maximum=160)
        clean_error_message = _clean_optional_text(
            error_message,
            "error_message",
            maximum=500,
        )
        if status == RUN_STATUS_DRY_RUN and (
            counts["accepted_count"] != 0 or clean_receipt
        ):
            raise SourceMonitoringStateError(
                "dry-run completion cannot record accepted items or an import receipt",
                code="SOURCE_MONITORING_STATE_INVALID",
            )
        completed_at = self._now_ms()
        with self.store._lock, closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            run, state = self._run_for_update(connection, clean_run_id)
            if clean_receipt:
                receipt_row = connection.execute(
                    """SELECT id,source_channel,source_key,external_run_id
                       FROM source_inbox_imports WHERE id=?""",
                    (clean_receipt,),
                ).fetchone()
                if (
                    receipt_row is None
                    or str(receipt_row["source_channel"]) != "official_source_monitor"
                    or str(receipt_row["source_key"]) != run["adapter_key"]
                    or str(receipt_row["external_run_id"]) != run["run_id"]
                ):
                    raise SourceMonitoringStateError(
                        "source import receipt is missing or not bound to this adapter run",
                        code="SOURCE_MONITORING_RECEIPT_INVALID",
                    )
                created_row = connection.execute(
                    """SELECT COUNT(*) AS created_count
                       FROM source_inbox_import_items
                       WHERE import_id=? AND disposition='CREATED'""",
                    (clean_receipt,),
                ).fetchone()
                if (
                    created_row is None
                    or int(created_row["created_count"]) != counts["accepted_count"]
                ):
                    raise SourceMonitoringStateError(
                        "source import receipt accepted count does not match this run",
                        code="SOURCE_MONITORING_RECEIPT_INVALID",
                    )
            elif counts["accepted_count"] > 0:
                raise SourceMonitoringStateError(
                    "accepted source items require a bound source import receipt",
                    code="SOURCE_MONITORING_RECEIPT_REQUIRED",
                )
            dry_run = status == RUN_STATUS_DRY_RUN
            if run["dry_run"] is not dry_run:
                raise SourceMonitoringStateError(
                    "run dry-run mode does not match its terminal status",
                    code="SOURCE_MONITORING_STATE_INVALID",
                )
            checkpoint_committed = status == RUN_STATUS_SUCCEEDED
            degraded = status == RUN_STATUS_DEGRADED
            consecutive_failures = state["consecutive_failures"] + 1 if degraded else 0
            state_error_code = clean_error_code if degraded else ""
            state_error_message = clean_error_message if degraded else ""
            last_event_at = (
                completed_at if counts["accepted_count"] > 0 else state["last_event_at_ms"]
            )
            if not dry_run:
                persisted_checkpoint = checkpoint if checkpoint_committed else state["checkpoint"]
                cursor = connection.execute(
                    """UPDATE source_adapter_states
                       SET checkpoint_json=?,checkpoint_sha256=?,etag=?,last_modified=?,
                           last_success_at_ms=?,last_event_at_ms=?,next_due_at_ms=?,
                           consecutive_failures=?,last_error_code=?,last_error_message=?,
                           state_version=state_version+1,updated_at_ms=?
                       WHERE adapter_key=? AND state_version=?""",
                    (
                        canonical_json(persisted_checkpoint),
                        canonical_sha256(persisted_checkpoint),
                        clean_etag if checkpoint_committed else state["etag"],
                        clean_last_modified if checkpoint_committed else state["last_modified"],
                        completed_at if checkpoint_committed else state["last_success_at_ms"],
                        last_event_at,
                        next_due,
                        consecutive_failures,
                        state_error_code,
                        state_error_message,
                        completed_at,
                        run["adapter_key"],
                        state["state_version"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise SourceMonitoringStateError(
                        "adapter state changed before checkpoint commit",
                        code="SOURCE_MONITORING_STATE_CONFLICT",
                    )
            duration_ms = max(0, completed_at - run["started_at_ms"])
            connection.execute(
                """UPDATE source_adapter_runs
                   SET next_checkpoint_json=?,next_checkpoint_sha256=?,completed_at_ms=?,
                       status=?,observed_count=?,accepted_count=?,duplicate_count=?,
                       rejected_count=?,duration_ms=?,error_code=?,error_message=?,
                       source_errors_json=?,receipt_id=?,dry_run=?
                   WHERE run_id=? AND status='RUNNING'""",
                (
                    checkpoint_json,
                    checkpoint_sha256,
                    completed_at,
                    status,
                    counts["observed_count"],
                    counts["accepted_count"],
                    counts["duplicate_count"],
                    counts["rejected_count"],
                    duration_ms,
                    clean_error_code,
                    clean_error_message,
                    source_errors_json,
                    clean_receipt,
                    int(dry_run),
                    clean_run_id,
                ),
            )
            run_row = connection.execute(
                "SELECT * FROM source_adapter_runs WHERE run_id=?",
                (clean_run_id,),
            ).fetchone()
            state_row = connection.execute(
                "SELECT * FROM source_adapter_states WHERE adapter_key=?",
                (run["adapter_key"],),
            ).fetchone()
            assert run_row is not None and state_row is not None
            return {
                "run": self._run_projection(run_row),
                "state": self._state_projection(state_row),
            }

    def fail_run(
        self,
        run_id: Any,
        *,
        error_code: str,
        error_message: str,
        next_due_at_ms: int,
        observed_count: int = 0,
        rejected_count: int = 0,
    ) -> dict[str, Any]:
        clean_run_id = _clean_token(run_id, "run_id")
        clean_error_code = _clean_token(error_code, "error_code")
        clean_error_message = _clean_optional_text(
            error_message,
            "error_message",
            maximum=500,
        )
        next_due = _native_non_negative(next_due_at_ms, "next_due_at_ms")
        observed = _native_non_negative(observed_count, "observed_count")
        rejected = _native_non_negative(rejected_count, "rejected_count")
        if rejected > observed:
            raise SourceMonitoringStateError(
                "rejected_count cannot exceed observed_count",
                code="SOURCE_MONITORING_STATE_INVALID",
            )
        completed_at = self._now_ms()
        with self.store._lock, closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            run, state = self._run_for_update(connection, clean_run_id)
            if run["dry_run"]:
                raise SourceMonitoringStateError(
                    "dry-run failures must be closed with a dry-run receipt",
                    code="SOURCE_MONITORING_STATE_INVALID",
                )
            cursor = connection.execute(
                """UPDATE source_adapter_states
                   SET next_due_at_ms=?,consecutive_failures=consecutive_failures+1,
                       last_error_code=?,last_error_message=?,
                       state_version=state_version+1,updated_at_ms=?
                   WHERE adapter_key=? AND state_version=?""",
                (
                    next_due,
                    clean_error_code,
                    clean_error_message,
                    completed_at,
                    run["adapter_key"],
                    state["state_version"],
                ),
            )
            if cursor.rowcount != 1:
                raise SourceMonitoringStateError(
                    "adapter state changed before failure commit",
                    code="SOURCE_MONITORING_STATE_CONFLICT",
                )
            duration_ms = max(0, completed_at - run["started_at_ms"])
            empty_checkpoint = canonical_json(run["started_checkpoint"])
            connection.execute(
                """UPDATE source_adapter_runs
                   SET next_checkpoint_json=?,next_checkpoint_sha256=?,completed_at_ms=?,
                       status='FAILED',observed_count=?,rejected_count=?,duration_ms=?,
                       error_code=?,error_message=?
                   WHERE run_id=? AND status='RUNNING'""",
                (
                    empty_checkpoint,
                    canonical_sha256(run["started_checkpoint"]),
                    completed_at,
                    observed,
                    rejected,
                    duration_ms,
                    clean_error_code,
                    clean_error_message,
                    clean_run_id,
                ),
            )
            run_row = connection.execute(
                "SELECT * FROM source_adapter_runs WHERE run_id=?",
                (clean_run_id,),
            ).fetchone()
            state_row = connection.execute(
                "SELECT * FROM source_adapter_states WHERE adapter_key=?",
                (run["adapter_key"],),
            ).fetchone()
            assert run_row is not None and state_row is not None
            return {
                "run": self._run_projection(run_row),
                "state": self._state_projection(state_row),
            }

    def recover_incomplete_runs(
        self,
        *,
        error_code: str = "worker_restarted",
        next_due_at_ms: int | None = None,
    ) -> int:
        clean_error_code = _clean_token(error_code, "error_code")
        completed_at = self._now_ms()
        next_due = completed_at if next_due_at_ms is None else _native_non_negative(
            next_due_at_ms,
            "next_due_at_ms",
        )
        recovered = 0
        with self.store._lock, closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM source_adapter_runs WHERE status='RUNNING' ORDER BY started_at_ms,run_id"
            ).fetchall()
            for row in rows:
                run = self._run_projection(row)
                state_row = connection.execute(
                    "SELECT * FROM source_adapter_states WHERE adapter_key=?",
                    (run["adapter_key"],),
                ).fetchone()
                if state_row is None:
                    raise SourceMonitoringStateError(
                        "active adapter run has no state",
                        code="SOURCE_MONITORING_RECORD_CORRUPT",
                    )
                state = self._state_projection(state_row)
                if not run["dry_run"]:
                    connection.execute(
                        """UPDATE source_adapter_states
                           SET next_due_at_ms=?,consecutive_failures=consecutive_failures+1,
                               last_error_code=?,last_error_message='Worker stopped before checkpoint commit.',
                               state_version=state_version+1,updated_at_ms=?
                           WHERE adapter_key=? AND state_version=?""",
                        (
                            next_due,
                            clean_error_code,
                            completed_at,
                            run["adapter_key"],
                            state["state_version"],
                        ),
                    )
                checkpoint_json = canonical_json(run["started_checkpoint"])
                connection.execute(
                    """UPDATE source_adapter_runs
                       SET next_checkpoint_json=?,next_checkpoint_sha256=?,completed_at_ms=?,
                           status='ABANDONED',duration_ms=?,error_code=?,
                           error_message='Worker stopped before checkpoint commit.'
                       WHERE run_id=? AND status='RUNNING'""",
                    (
                        checkpoint_json,
                        canonical_sha256(run["started_checkpoint"]),
                        completed_at,
                        max(0, completed_at - run["started_at_ms"]),
                        clean_error_code,
                        run["run_id"],
                    ),
                )
                recovered += 1
        return recovered

    def get_run(self, run_id: Any) -> dict[str, Any] | None:
        clean_run_id = _clean_token(run_id, "run_id")
        with closing(self.store._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM source_adapter_runs WHERE run_id=?",
                (clean_run_id,),
            ).fetchone()
            return self._run_projection(row) if row is not None else None

    def list_runs(
        self,
        *,
        adapter_key: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise SourceMonitoringStateError(
                "limit must be between 1 and 1000",
                code="SOURCE_MONITORING_STATE_INVALID",
            )
        parameters: list[Any] = []
        where = ""
        if adapter_key:
            where = "WHERE adapter_key=?"
            parameters.append(normalize_adapter_key(adapter_key))
        parameters.append(limit)
        with closing(self.store._connect()) as connection:
            rows = connection.execute(
                f"""SELECT * FROM source_adapter_runs {where}
                    ORDER BY started_at_ms DESC,run_id DESC LIMIT ?""",
                parameters,
            ).fetchall()
            return [self._run_projection(row) for row in rows]


__all__ = [
    "RUN_STATUS_ABANDONED",
    "RUN_STATUS_DEGRADED",
    "RUN_STATUS_DRY_RUN",
    "RUN_STATUS_FAILED",
    "RUN_STATUS_RUNNING",
    "RUN_STATUS_SUCCEEDED",
    "SOURCE_ADAPTER_RUN_VERSION",
    "SOURCE_ADAPTER_STATE_VERSION",
    "SOURCE_MONITORING_MIGRATION_KEY",
    "SourceMonitoringStateError",
    "SourceMonitoringStateRepository",
    "ensure_source_monitoring_schema",
]
