from __future__ import annotations

"""Persist source-adapter checkpoints and append-only run receipts.

The repository deliberately shares :class:`StudioStore`'s SQLite database but
does not initialize it at runtime.  ``ensure_source_monitoring_schema`` is
called only from the existing controlled schema initializer, so formal
databases continue to require the normal preview/prepare/apply migration gate.
"""

import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from .contracts import (
    MAX_ETAG_CHARS,
    MAX_LAST_MODIFIED_CHARS,
    MAX_NATIVE_INTEGER,
    MAX_SOURCE_ERRORS_PER_POLL,
    OFFICIAL_SOURCE_CHANNEL,
    SOURCE_MONITORING_SOURCE_CHANNELS,
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
SOURCE_MONITORING_INITIALIZATION_MIGRATION_KEY = (
    "source_monitoring_initialization_receipt_v1"
)
SOURCE_MONITORING_INITIALIZATION_RECEIPT_VERSION = (
    "source_monitoring_initialization_receipt_v1"
)
SOURCE_MONITORING_INITIALIZATION_VERSION = "source_monitoring_initialization_v1"

INITIALIZATION_MODES = frozenset({"seed_only", "catch_up", "from_time"})
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

_INITIALIZATION_COLUMNS = (
    "initialization_mode",
    "initialization_config_version",
    "initialization_preview_sha256",
    "initialization_receipt_json",
    "initialization_receipt_sha256",
)
_INITIALIZATION_COLUMN_DEFINITIONS = {
    "initialization_mode": (
        "TEXT NOT NULL DEFAULT '' CHECK(initialization_mode IN "
        "('','seed_only','catch_up','from_time'))"
    ),
    "initialization_config_version": (
        "TEXT NOT NULL DEFAULT '' CHECK(length(initialization_config_version)<=160)"
    ),
    "initialization_preview_sha256": (
        "TEXT NOT NULL DEFAULT '' CHECK(initialization_preview_sha256='' OR "
        "(length(initialization_preview_sha256)=64 AND "
        "initialization_preview_sha256 NOT GLOB '*[^0-9a-f]*'))"
    ),
    "initialization_receipt_json": (
        "TEXT NOT NULL DEFAULT '' CHECK(length(initialization_receipt_json)<=4096)"
    ),
    "initialization_receipt_sha256": (
        "TEXT NOT NULL DEFAULT '' CHECK((initialization_receipt_sha256='' AND "
        "initialization_mode='' AND initialization_config_version='' AND "
        "initialization_preview_sha256='' AND initialization_receipt_json='') OR "
        "(length(initialization_receipt_sha256)=64 AND "
        "initialization_receipt_sha256 NOT GLOB '*[^0-9a-f]*' AND "
        "initialization_mode<>'' AND initialization_config_version<>'' AND "
        "initialization_preview_sha256<>'' AND initialization_receipt_json<>'' AND "
        "status='SUCCEEDED' AND dry_run=0))"
    ),
}

_INITIALIZATION_TIME_INDEX = "idx_source_adapter_runs_initialization_time"
_INITIALIZATION_RECEIPT_INDEX = "uq_source_adapter_runs_initialization_receipt"
_INITIALIZATION_UPDATE_TRIGGER = "trg_source_adapter_runs_initialization_no_update"
_INITIALIZATION_DELETE_TRIGGER = "trg_source_adapter_runs_initialization_no_delete"
_INITIALIZATION_MIGRATION_UPDATE_TRIGGER = (
    "trg_source_monitoring_initialization_marker_no_update"
)
_INITIALIZATION_MIGRATION_DELETE_TRIGGER = (
    "trg_source_monitoring_initialization_marker_no_delete"
)
_INITIALIZATION_MIGRATION_INSERT_TRIGGER = (
    "trg_source_monitoring_initialization_marker_no_replace"
)

_INITIALIZATION_TIME_INDEX_DDL = f"""CREATE INDEX {_INITIALIZATION_TIME_INDEX}
ON source_adapter_runs(
    adapter_key,initialization_config_version,completed_at_ms DESC,run_id DESC
)
WHERE status='SUCCEEDED' AND initialization_mode<>''"""
_INITIALIZATION_RECEIPT_INDEX_DDL = (
    f"""CREATE UNIQUE INDEX {_INITIALIZATION_RECEIPT_INDEX}
ON source_adapter_runs(initialization_receipt_sha256)
WHERE initialization_receipt_sha256<>''"""
)
_INITIALIZATION_UPDATE_TRIGGER_DDL = f"""CREATE TRIGGER {_INITIALIZATION_UPDATE_TRIGGER}
BEFORE UPDATE ON source_adapter_runs
WHEN OLD.initialization_receipt_sha256<>''
BEGIN
    SELECT RAISE(ABORT,'source monitoring initialization receipts are immutable');
END"""
_INITIALIZATION_DELETE_TRIGGER_DDL = f"""CREATE TRIGGER {_INITIALIZATION_DELETE_TRIGGER}
BEFORE DELETE ON source_adapter_runs
WHEN OLD.initialization_receipt_sha256<>''
BEGIN
    SELECT RAISE(ABORT,'source monitoring initialization receipts are immutable');
END"""
_INITIALIZATION_MIGRATION_UPDATE_TRIGGER_DDL = (
    f"""CREATE TRIGGER {_INITIALIZATION_MIGRATION_UPDATE_TRIGGER}
BEFORE UPDATE ON schema_migrations
WHEN OLD.key='{SOURCE_MONITORING_INITIALIZATION_MIGRATION_KEY}'
  OR NEW.key='{SOURCE_MONITORING_INITIALIZATION_MIGRATION_KEY}'
BEGIN
    SELECT RAISE(ABORT,'source monitoring initialization marker is immutable');
END"""
)
_INITIALIZATION_MIGRATION_DELETE_TRIGGER_DDL = (
    f"""CREATE TRIGGER {_INITIALIZATION_MIGRATION_DELETE_TRIGGER}
BEFORE DELETE ON schema_migrations
WHEN OLD.key='{SOURCE_MONITORING_INITIALIZATION_MIGRATION_KEY}'
BEGIN
    SELECT RAISE(ABORT,'source monitoring initialization marker is immutable');
END"""
)
_INITIALIZATION_MIGRATION_INSERT_TRIGGER_DDL = (
    f"""CREATE TRIGGER {_INITIALIZATION_MIGRATION_INSERT_TRIGGER}
BEFORE INSERT ON schema_migrations
WHEN EXISTS (
    SELECT 1 FROM schema_migrations existing
     WHERE existing.key='{SOURCE_MONITORING_INITIALIZATION_MIGRATION_KEY}'
       AND (existing.rowid=NEW.rowid
            OR NEW.key='{SOURCE_MONITORING_INITIALIZATION_MIGRATION_KEY}')
)
BEGIN
    SELECT RAISE(ABORT,'source monitoring initialization marker is immutable');
END"""
)
_INITIALIZATION_SCHEMA_DDL = (
    _INITIALIZATION_TIME_INDEX_DDL,
    _INITIALIZATION_RECEIPT_INDEX_DDL,
    _INITIALIZATION_UPDATE_TRIGGER_DDL,
    _INITIALIZATION_DELETE_TRIGGER_DDL,
    _INITIALIZATION_MIGRATION_UPDATE_TRIGGER_DDL,
    _INITIALIZATION_MIGRATION_DELETE_TRIGGER_DDL,
    _INITIALIZATION_MIGRATION_INSERT_TRIGGER_DDL,
)
_INITIALIZATION_SCHEMA_OBJECTS = {
    _INITIALIZATION_TIME_INDEX: ("index", _INITIALIZATION_TIME_INDEX_DDL),
    _INITIALIZATION_RECEIPT_INDEX: ("index", _INITIALIZATION_RECEIPT_INDEX_DDL),
    _INITIALIZATION_UPDATE_TRIGGER: ("trigger", _INITIALIZATION_UPDATE_TRIGGER_DDL),
    _INITIALIZATION_DELETE_TRIGGER: ("trigger", _INITIALIZATION_DELETE_TRIGGER_DDL),
    _INITIALIZATION_MIGRATION_UPDATE_TRIGGER: (
        "trigger",
        _INITIALIZATION_MIGRATION_UPDATE_TRIGGER_DDL,
    ),
    _INITIALIZATION_MIGRATION_DELETE_TRIGGER: (
        "trigger",
        _INITIALIZATION_MIGRATION_DELETE_TRIGGER_DDL,
    ),
    _INITIALIZATION_MIGRATION_INSERT_TRIGGER: (
        "trigger",
        _INITIALIZATION_MIGRATION_INSERT_TRIGGER_DDL,
    ),
}

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

SEC_FILINGS_V1_CONFIG_PREFIX = "sec_filings_config_v1_"
SEC_FILINGS_V2_CONFIG_PREFIX = "sec_filings_config_v2_"
SEC_FILINGS_CHECKPOINT_VERSION = "sec_filings_checkpoint_v1"
SEC_FILINGS_MIGRATION_PREVIEW_VERSION = "sec_filings_v1_to_v2_migration_preview_v1"
MAX_SEC_FILINGS_SEEN_ACCESSIONS = 1_000
_SEC_ACCESSION_RE = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}\Z")


def _is_sec_filings_v1_to_v2_migration(
    adapter_key: str,
    old_config: str,
    new_config: str,
) -> bool:
    return (
        adapter_key == "sec_filings"
        and old_config.startswith(SEC_FILINGS_V1_CONFIG_PREFIX)
        and new_config.startswith(SEC_FILINGS_V2_CONFIG_PREFIX)
    )


def _strict_sec_checkpoint(value: Any) -> list[str]:
    checkpoint = normalize_checkpoint(value)
    if checkpoint == {}:
        return []
    if (
        set(checkpoint) != {"version", "seen_accessions"}
        or checkpoint.get("version") != SEC_FILINGS_CHECKPOINT_VERSION
        or type(checkpoint.get("seen_accessions")) is not list
        or len(checkpoint["seen_accessions"]) > MAX_SEC_FILINGS_SEEN_ACCESSIONS
    ):
        raise SourceMonitoringStateError(
            "SEC checkpoint is invalid for the stable-time migration",
            code="SOURCE_MONITORING_SEC_MIGRATION_CHECKPOINT_INVALID",
        )
    seen: list[str] = []
    for accession in checkpoint["seen_accessions"]:
        if (
            type(accession) is not str
            or _SEC_ACCESSION_RE.fullmatch(accession) is None
            or accession in seen
        ):
            raise SourceMonitoringStateError(
                "SEC checkpoint contains an invalid or duplicate accession",
                code="SOURCE_MONITORING_SEC_MIGRATION_CHECKPOINT_INVALID",
            )
        seen.append(accession)
    return seen


def _required_sec_v2_checkpoint(
    connection: sqlite3.Connection,
    current_checkpoint: Any,
) -> tuple[dict[str, Any], int]:
    seen = _strict_sec_checkpoint(current_checkpoint)
    rows = connection.execute(
        """SELECT item.item_json,item.item_sha256,item.server_fingerprint
             FROM source_inbox_items item
             JOIN (
                 SELECT DISTINCT link.item_id
                   FROM source_inbox_import_items link
                   JOIN source_inbox_imports observation
                     ON observation.id=link.import_id
                   JOIN source_adapter_runs run
                     ON run.run_id=observation.external_run_id
                    AND run.adapter_key=observation.source_key
                  WHERE link.disposition IN ('CREATED','DUPLICATE')
                    AND observation.source_channel=?
                    AND observation.source_key=?
                    AND run.dry_run=0
                    AND run.status IN (
                        'SUCCEEDED','DEGRADED','FAILED','ABANDONED'
                    )
                    AND (
                        (
                            run.status IN ('SUCCEEDED','DEGRADED')
                            AND run.receipt_id=observation.id
                        )
                        OR (
                            run.status IN ('FAILED','ABANDONED')
                            AND run.receipt_id=''
                            AND observation.received_at>=run.started_at_ms
                            AND observation.received_at<=run.completed_at_ms
                        )
                    )
             ) trusted_observation ON trusted_observation.item_id=item.id
             ORDER BY item.created_at,item.id""",
        (OFFICIAL_SOURCE_CHANNEL, "sec_filings"),
    ).fetchall()
    persisted: list[str] = []
    for row in rows:
        try:
            item = json.loads(str(row["item_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SourceMonitoringStateError(
                "persisted SEC item JSON is invalid",
                code="SOURCE_MONITORING_SEC_MIGRATION_SOURCE_CORRUPT",
            ) from exc
        if type(item) is not dict:
            raise SourceMonitoringStateError(
                "persisted SEC item is not an object",
                code="SOURCE_MONITORING_SEC_MIGRATION_SOURCE_CORRUPT",
            )
        extensions = item.get("extensions")
        sec = extensions.get("sec_v1") if type(extensions) is dict else None
        accession = item.get("external_item_id")
        if (
            type(sec) is not dict
            or type(accession) is not str
            or _SEC_ACCESSION_RE.fullmatch(accession) is None
            or sec.get("accession_number") != accession
            or item.get("item_type") != "sec_filing"
            or str(row["item_sha256"]) != canonical_sha256(item)
            or str(row["server_fingerprint"])
            != str(item.get("server_fingerprint") or "")
            or str(row["item_json"]) != canonical_json(item)
        ):
            raise SourceMonitoringStateError(
                "persisted SEC item failed its immutable migration binding",
                code="SOURCE_MONITORING_SEC_MIGRATION_SOURCE_CORRUPT",
            )
        if accession not in persisted:
            persisted.append(accession)
    for accession in sorted(persisted):
        if accession not in seen:
            seen.append(accession)
    if len(seen) > MAX_SEC_FILINGS_SEEN_ACCESSIONS:
        raise SourceMonitoringStateError(
            "SEC migration checkpoint exceeds the admitted capacity",
            code="SOURCE_MONITORING_SEC_MIGRATION_CAPACITY_EXCEEDED",
        )
    checkpoint = (
        {
            "version": SEC_FILINGS_CHECKPOINT_VERSION,
            "seen_accessions": seen,
        }
        if seen
        else {}
    )
    return checkpoint, len(persisted)


class SourceMonitoringStateError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _compact_sql(value: str) -> str:
    return "".join(value.lower().split())


def _source_monitoring_initialization_schema_state(
    connection: sqlite3.Connection,
) -> str:
    table_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='source_adapter_runs'"
    ).fetchone()
    if table_row is None:
        raise SourceMonitoringStateError(
            "source adapter run table is missing",
            code="SOURCE_MONITORING_INITIALIZATION_SCHEMA_INVALID",
        )
    table_sql = str(
        table_row["sql"] if isinstance(table_row, sqlite3.Row) else table_row[0]
    )
    column_rows = connection.execute(
        "PRAGMA table_info(source_adapter_runs)"
    ).fetchall()
    column_info = {
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1]): {
            "type": str(row["type"] if isinstance(row, sqlite3.Row) else row[2]),
            "notnull": row["notnull"] if isinstance(row, sqlite3.Row) else row[3],
            "default": row["dflt_value"] if isinstance(row, sqlite3.Row) else row[4],
            "pk": row["pk"] if isinstance(row, sqlite3.Row) else row[5],
        }
        for row in column_rows
    }
    present_columns = set(_INITIALIZATION_COLUMNS).intersection(column_info)

    object_names = tuple(_INITIALIZATION_SCHEMA_OBJECTS)
    placeholders = ",".join("?" for _name in object_names)
    object_rows = connection.execute(
        f"SELECT type,name,sql FROM sqlite_master WHERE name IN ({placeholders})",
        object_names,
    ).fetchall()
    objects = {
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1]): {
            "type": str(row["type"] if isinstance(row, sqlite3.Row) else row[0]),
            "sql": str(row["sql"] if isinstance(row, sqlite3.Row) else row[2]),
        }
        for row in object_rows
    }
    marker = connection.execute(
        "SELECT applied_at FROM schema_migrations WHERE key=?",
        (SOURCE_MONITORING_INITIALIZATION_MIGRATION_KEY,),
    ).fetchone()

    no_objects = not objects and marker is None
    if no_objects and (
        not present_columns or present_columns == set(_INITIALIZATION_COLUMNS)
    ):
        return "migration_required"
    if (
        present_columns != set(_INITIALIZATION_COLUMNS)
        or set(objects) != set(_INITIALIZATION_SCHEMA_OBJECTS)
        or marker is None
    ):
        raise SourceMonitoringStateError(
            "source monitoring initialization schema is incomplete",
            code="SOURCE_MONITORING_INITIALIZATION_SCHEMA_INVALID",
        )

    compact_table_sql = _compact_sql(table_sql)
    for name, definition in _INITIALIZATION_COLUMN_DEFINITIONS.items():
        metadata = column_info[name]
        if (
            metadata != {
                "type": "TEXT",
                "notnull": 1,
                "default": "''",
                "pk": 0,
            }
            or _compact_sql(f"{name} {definition}") not in compact_table_sql
        ):
            raise SourceMonitoringStateError(
                f"source monitoring initialization column {name} is invalid",
                code="SOURCE_MONITORING_INITIALIZATION_SCHEMA_INVALID",
            )
    for name, (expected_type, expected_sql) in _INITIALIZATION_SCHEMA_OBJECTS.items():
        if objects[name] != {"type": expected_type, "sql": expected_sql}:
            raise SourceMonitoringStateError(
                f"source monitoring initialization object {name} is invalid",
                code="SOURCE_MONITORING_INITIALIZATION_SCHEMA_INVALID",
            )
    applied_at = marker["applied_at"] if isinstance(marker, sqlite3.Row) else marker[0]
    if type(applied_at) is not int or not 0 <= applied_at <= MAX_NATIVE_INTEGER:
        raise SourceMonitoringStateError(
            "source monitoring initialization migration marker is invalid",
            code="SOURCE_MONITORING_INITIALIZATION_SCHEMA_INVALID",
        )
    return "current"


def source_monitoring_initialization_schema_state(
    connection: sqlite3.Connection,
) -> str:
    """Project the additive initialization schema without mutating SQLite.

    Health and migration diagnostics need to distinguish an intact legacy
    monitoring schema from a corrupt partial migration before they deserialize
    run rows whose five initialization columns may not exist yet.
    """

    if not isinstance(connection, sqlite3.Connection):
        raise SourceMonitoringStateError(
            "source monitoring initialization connection is invalid",
            code="SOURCE_MONITORING_INITIALIZATION_SCHEMA_INVALID",
        )
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_adapter_runs'"
    ).fetchone()
    if table is None:
        return "unavailable"
    return _source_monitoring_initialization_schema_state(connection)


def _ensure_source_monitoring_initialization_schema(
    connection: sqlite3.Connection,
    *,
    applied_at_ms: int,
) -> None:
    if _source_monitoring_initialization_schema_state(connection) == "current":
        return

    savepoint = "source_monitoring_initialization_receipt_v1"
    connection.execute(f"SAVEPOINT {savepoint}")
    try:
        existing_columns = {
            str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
            for row in connection.execute(
                "PRAGMA table_info(source_adapter_runs)"
            ).fetchall()
        }
        for name in _INITIALIZATION_COLUMNS:
            if name not in existing_columns:
                connection.execute(
                    f"ALTER TABLE source_adapter_runs ADD COLUMN "
                    f"{name} {_INITIALIZATION_COLUMN_DEFINITIONS[name]}"
                )
        for statement in _INITIALIZATION_SCHEMA_DDL:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations(key,applied_at) VALUES(?,?)",
            (SOURCE_MONITORING_INITIALIZATION_MIGRATION_KEY, applied_at_ms),
        )
        if _source_monitoring_initialization_schema_state(connection) != "current":
            raise SourceMonitoringStateError(
                "source monitoring initialization migration did not complete",
                code="SOURCE_MONITORING_INITIALIZATION_SCHEMA_INVALID",
            )
    except BaseException:
        connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    connection.execute(f"RELEASE SAVEPOINT {savepoint}")


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
            initialization_mode TEXT NOT NULL DEFAULT '' CHECK(
                initialization_mode IN ('','seed_only','catch_up','from_time')
            ),
            initialization_config_version TEXT NOT NULL DEFAULT ''
                CHECK(length(initialization_config_version)<=160),
            initialization_preview_sha256 TEXT NOT NULL DEFAULT '' CHECK(
                initialization_preview_sha256='' OR (
                    length(initialization_preview_sha256)=64
                    AND initialization_preview_sha256 NOT GLOB '*[^0-9a-f]*'
                )
            ),
            initialization_receipt_json TEXT NOT NULL DEFAULT ''
                CHECK(length(initialization_receipt_json)<=4096),
            initialization_receipt_sha256 TEXT NOT NULL DEFAULT '' CHECK(
                (
                    initialization_receipt_sha256=''
                    AND initialization_mode=''
                    AND initialization_config_version=''
                    AND initialization_preview_sha256=''
                    AND initialization_receipt_json=''
                ) OR (
                    length(initialization_receipt_sha256)=64
                    AND initialization_receipt_sha256 NOT GLOB '*[^0-9a-f]*'
                    AND initialization_mode<>''
                    AND initialization_config_version<>''
                    AND initialization_preview_sha256<>''
                    AND initialization_receipt_json<>''
                    AND status='SUCCEEDED'
                    AND dry_run=0
                )
            ),
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
    _ensure_source_monitoring_initialization_schema(
        connection,
        applied_at_ms=applied_at_ms,
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


def _sha256_digest(value: Any, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise SourceMonitoringStateError(
            f"{label} must be a lowercase SHA-256 digest",
            code="SOURCE_MONITORING_STATE_INVALID",
        )
    return value


def _canonical_initialization_time(value: Any, label: str) -> str:
    if type(value) is not str or len(value) > 32:
        raise SourceMonitoringStateError(
            f"{label} must be a bounded canonical UTC timestamp or empty",
            code="SOURCE_MONITORING_INITIALIZATION_INVALID",
        )
    if value == "":
        return value
    if re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{3})?Z",
        value,
    ) is None:
        raise SourceMonitoringStateError(
            f"{label} must be a canonical millisecond-or-coarser UTC timestamp",
            code="SOURCE_MONITORING_INITIALIZATION_INVALID",
        )
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise SourceMonitoringStateError(
            f"{label} is not a real UTC timestamp",
            code="SOURCE_MONITORING_INITIALIZATION_INVALID",
        ) from exc
    if parsed.tzinfo != timezone.utc:
        raise SourceMonitoringStateError(
            f"{label} must use UTC",
            code="SOURCE_MONITORING_INITIALIZATION_INVALID",
        )
    return value


def _normalise_initialization_request(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    expected_fields = {
        "version",
        "mode",
        "config_version",
        "preview_sha256",
        "candidate_count",
        "adapter_duplicate_count",
        "selected_count",
        "skipped_count",
        "catch_up_max_items",
        "from_time_ms",
        "earliest_occurred_at",
        "latest_occurred_at",
        "starting_checkpoint_sha256",
        "next_checkpoint_sha256",
        "captured_at_ms",
    }
    if type(value) is not dict or set(value) != expected_fields:
        raise SourceMonitoringStateError(
            "initialization does not match the closed v1 projection",
            code="SOURCE_MONITORING_INITIALIZATION_INVALID",
        )
    if value["version"] != SOURCE_MONITORING_INITIALIZATION_VERSION:
        raise SourceMonitoringStateError(
            "initialization version is invalid",
            code="SOURCE_MONITORING_INITIALIZATION_INVALID",
        )
    mode = _clean_token(value["mode"], "initialization.mode", maximum=32)
    if mode not in INITIALIZATION_MODES:
        raise SourceMonitoringStateError(
            "initialization mode is invalid",
            code="SOURCE_MONITORING_INITIALIZATION_INVALID",
        )
    candidate_count = _native_non_negative(
        value["candidate_count"],
        "initialization.candidate_count",
    )
    adapter_duplicate_count = _native_non_negative(
        value["adapter_duplicate_count"],
        "initialization.adapter_duplicate_count",
    )
    selected_count = _native_non_negative(
        value["selected_count"],
        "initialization.selected_count",
    )
    skipped_count = _native_non_negative(
        value["skipped_count"],
        "initialization.skipped_count",
    )
    if selected_count + skipped_count != candidate_count:
        raise SourceMonitoringStateError(
            "initialization selection counts do not cover the candidate set",
            code="SOURCE_MONITORING_INITIALIZATION_INVALID",
        )
    catch_up_max_items = _native_non_negative(
        value["catch_up_max_items"],
        "initialization.catch_up_max_items",
    )
    from_time_ms = _native_non_negative(
        value["from_time_ms"],
        "initialization.from_time_ms",
    )
    invalid_mode_policy = (
        (
            mode == "seed_only"
            and (catch_up_max_items != 0 or from_time_ms != 0)
        )
        or (
            mode == "catch_up"
            and (
                not 1 <= catch_up_max_items <= 50
                or from_time_ms != 0
                or selected_count != min(candidate_count, catch_up_max_items)
            )
        )
        or (
            mode == "from_time"
            and catch_up_max_items != 0
        )
    )
    if invalid_mode_policy:
        raise SourceMonitoringStateError(
            "initialization mode policy fields are inconsistent",
            code="SOURCE_MONITORING_INITIALIZATION_INVALID",
        )
    earliest = _canonical_initialization_time(
        value["earliest_occurred_at"],
        "initialization.earliest_occurred_at",
    )
    latest = _canonical_initialization_time(
        value["latest_occurred_at"],
        "initialization.latest_occurred_at",
    )
    if (candidate_count == 0) is not (earliest == "" and latest == ""):
        raise SourceMonitoringStateError(
            "initialization occurrence bounds do not match candidate_count",
            code="SOURCE_MONITORING_INITIALIZATION_INVALID",
        )
    if earliest and datetime.fromisoformat(earliest[:-1] + "+00:00") > datetime.fromisoformat(
        latest[:-1] + "+00:00"
    ):
        raise SourceMonitoringStateError(
            "initialization occurrence bounds are reversed",
            code="SOURCE_MONITORING_INITIALIZATION_INVALID",
        )
    return {
        "version": SOURCE_MONITORING_INITIALIZATION_VERSION,
        "mode": mode,
        "config_version": _clean_token(
            value["config_version"],
            "initialization.config_version",
        ),
        "preview_sha256": _sha256_digest(
            value["preview_sha256"],
            "initialization.preview_sha256",
        ),
        "candidate_count": candidate_count,
        "adapter_duplicate_count": adapter_duplicate_count,
        "selected_count": selected_count,
        "skipped_count": skipped_count,
        "catch_up_max_items": catch_up_max_items,
        "from_time_ms": from_time_ms,
        "earliest_occurred_at": earliest,
        "latest_occurred_at": latest,
        "starting_checkpoint_sha256": _sha256_digest(
            value["starting_checkpoint_sha256"],
            "initialization.starting_checkpoint_sha256",
        ),
        "next_checkpoint_sha256": _sha256_digest(
            value["next_checkpoint_sha256"],
            "initialization.next_checkpoint_sha256",
        ),
        "captured_at_ms": _native_non_negative(
            value["captured_at_ms"],
            "initialization.captured_at_ms",
        ),
    }


def _load_canonical_initialization_receipt(value: Any) -> dict[str, Any]:
    if type(value) is not str or not value or len(value) > 4096:
        raise SourceMonitoringStateError(
            "initialization receipt JSON is invalid",
            code="SOURCE_MONITORING_INITIALIZATION_RECEIPT_CORRUPT",
        )

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    try:
        decoded = json.loads(
            value,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
        canonical = canonical_json(decoded)
    except (
        RecursionError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        SourceMonitoringContractError,
    ) as exc:
        raise SourceMonitoringStateError(
            "initialization receipt JSON is corrupt",
            code="SOURCE_MONITORING_INITIALIZATION_RECEIPT_CORRUPT",
        ) from exc
    if type(decoded) is not dict or canonical != value:
        raise SourceMonitoringStateError(
            "initialization receipt JSON is not a canonical object",
            code="SOURCE_MONITORING_INITIALIZATION_RECEIPT_CORRUPT",
        )
    return decoded


def _build_initialization_receipt(
    *,
    run: dict[str, Any],
    request: dict[str, Any],
    completed_at_ms: int,
    counts: dict[str, int],
) -> dict[str, Any]:
    return {
        "version": SOURCE_MONITORING_INITIALIZATION_RECEIPT_VERSION,
        "adapter_key": run["adapter_key"],
        "run_id": run["run_id"],
        "initialization": request,
        "terminal_counts": counts,
        "completed_at_ms": completed_at_ms,
    }


def _initialization_projection(
    data: dict[str, Any],
    run: dict[str, Any],
) -> dict[str, Any] | None:
    stored = {
        "mode": data.get("initialization_mode"),
        "config_version": data.get("initialization_config_version"),
        "preview_sha256": data.get("initialization_preview_sha256"),
        "receipt_json": data.get("initialization_receipt_json"),
        "receipt_sha256": data.get("initialization_receipt_sha256"),
    }
    if all(value == "" for value in stored.values()):
        return None
    if any(type(value) is not str or not value for value in stored.values()):
        raise SourceMonitoringStateError(
            "initialization receipt fields are incomplete",
            code="SOURCE_MONITORING_INITIALIZATION_RECEIPT_CORRUPT",
        )
    receipt = _load_canonical_initialization_receipt(stored["receipt_json"])
    try:
        request = _normalise_initialization_request(receipt.get("initialization"))
    except SourceMonitoringStateError as exc:
        raise SourceMonitoringStateError(
            "initialization receipt projection is corrupt",
            code="SOURCE_MONITORING_INITIALIZATION_RECEIPT_CORRUPT",
        ) from exc
    if request is None:
        raise SourceMonitoringStateError(
            "initialization receipt projection is missing",
            code="SOURCE_MONITORING_INITIALIZATION_RECEIPT_CORRUPT",
        )
    expected = _build_initialization_receipt(
        run=run,
        request=request,
        completed_at_ms=run["completed_at_ms"],
        counts={
            "observed_count": run["observed_count"],
            "accepted_count": run["accepted_count"],
            "duplicate_count": run["duplicate_count"],
            "rejected_count": run["rejected_count"],
        },
    )
    if (
        run["status"] != RUN_STATUS_SUCCEEDED
        or run["dry_run"]
        or request["mode"] != stored["mode"]
        or request["config_version"] != stored["config_version"]
        or request["preview_sha256"] != stored["preview_sha256"]
        or receipt != expected
        or stored["receipt_sha256"] != canonical_sha256(receipt)
    ):
        raise SourceMonitoringStateError(
            "initialization receipt seal is invalid",
            code="SOURCE_MONITORING_INITIALIZATION_RECEIPT_CORRUPT",
        )
    return {
        "mode": request["mode"],
        "config_version": request["config_version"],
        "preview_sha256": request["preview_sha256"],
        "receipt_sha256": stored["receipt_sha256"],
        "catch_up_max_items": request["catch_up_max_items"],
        "from_time_ms": request["from_time_ms"],
    }


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

    def _connect_read_only(self) -> sqlite3.Connection:
        uri_path = quote(self.store.path.resolve().as_posix(), safe="/:")
        connection = sqlite3.connect(
            f"file:{uri_path}?mode=ro",
            uri=True,
            timeout=10,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

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
        projection["initialization"] = _initialization_projection(data, projection)
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
        with self.store._lock, closing(self._connect_read_only()) as connection:
            return self.read_state_from_connection(connection, adapter_key)

    def read_state_from_connection(
        self,
        connection: sqlite3.Connection,
        adapter_key: Any,
    ) -> dict[str, Any] | None:
        """Read and seal-validate one state from an existing snapshot connection."""

        clean_key = normalize_adapter_key(adapter_key)
        row = connection.execute(
            "SELECT * FROM source_adapter_states WHERE adapter_key=?",
            (clean_key,),
        ).fetchone()
        return self._state_projection(row) if row is not None else None

    def list_states(self) -> list[dict[str, Any]]:
        with self.store._lock, closing(self._connect_read_only()) as connection:
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

    def preview_sec_filings_v1_to_v2_migration(
        self,
        *,
        expected_config_version: Any,
        new_config_version: Any,
        expected_state_version: Any,
    ) -> dict[str, Any]:
        """Derive the only safe SEC stable-time replacement checkpoint."""

        expected_config = _clean_token(
            expected_config_version,
            "expected_config_version",
        )
        new_config = _clean_token(new_config_version, "new_config_version")
        if not _is_sec_filings_v1_to_v2_migration(
            "sec_filings",
            expected_config,
            new_config,
        ):
            raise SourceMonitoringStateError(
                "preview only supports the SEC v1 to v2 stable-time migration",
                code="SOURCE_MONITORING_SEC_MIGRATION_UNSUPPORTED",
            )
        if type(expected_state_version) is not int or expected_state_version < 1:
            raise SourceMonitoringStateError(
                "expected_state_version is invalid",
                code="SOURCE_MONITORING_STATE_INVALID",
            )
        with self.store._lock, closing(self._connect_read_only()) as connection:
            row = connection.execute(
                "SELECT * FROM source_adapter_states WHERE adapter_key=?",
                ("sec_filings",),
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
                    "adapter state changed before config migration preview",
                    code="SOURCE_MONITORING_STATE_CONFLICT",
                )
            if state["enabled"]:
                raise SourceMonitoringStateError(
                    "adapter must be disabled before config migration preview",
                    code="SOURCE_MONITORING_ADAPTER_ENABLED",
                )
            active = connection.execute(
                "SELECT 1 FROM source_adapter_runs WHERE adapter_key=? AND status='RUNNING'",
                ("sec_filings",),
            ).fetchone()
            if active is not None:
                raise SourceMonitoringStateError(
                    "adapter config cannot preview migration during an active run",
                    code="SOURCE_MONITORING_RUN_ACTIVE",
                )
            checkpoint, persisted_count = _required_sec_v2_checkpoint(
                connection,
                state["checkpoint"],
            )
        return {
            "version": SEC_FILINGS_MIGRATION_PREVIEW_VERSION,
            "adapter_key": "sec_filings",
            "expected_config_version": expected_config,
            "new_config_version": new_config,
            "expected_state_version": expected_state_version,
            "next_checkpoint": checkpoint,
            "next_checkpoint_sha256": canonical_sha256(checkpoint),
            "persisted_accession_count": persisted_count,
            "safety": {
                "database_writes_performed": 0,
                "provider_calls_performed": 0,
                "network_requests_performed": 0,
                "market_calls_performed": 0,
                "formal_rounds_created": 0,
            },
        }

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
            if _is_sec_filings_v1_to_v2_migration(
                clean_key,
                expected_config,
                new_config,
            ):
                required_checkpoint, _persisted_count = _required_sec_v2_checkpoint(
                    connection,
                    state["checkpoint"],
                )
                if checkpoint != required_checkpoint:
                    raise SourceMonitoringStateError(
                        "SEC stable-time migration checkpoint differs from the read-only preview",
                        code="SOURCE_MONITORING_SEC_MIGRATION_CHECKPOINT_MISMATCH",
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
        initialization: Any = None,
        source_channel: str = OFFICIAL_SOURCE_CHANNEL,
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
        clean_initialization = _normalise_initialization_request(initialization)
        if (
            type(source_channel) is not str
            or source_channel not in SOURCE_MONITORING_SOURCE_CHANNELS
        ):
            raise SourceMonitoringStateError(
                "source channel is not in the closed monitoring channel set",
                code="SOURCE_MONITORING_SOURCE_CHANNEL_INVALID",
            )
        clean_source_channel = source_channel
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
            counts["accepted_count"] != 0 or clean_receipt or clean_initialization
        ):
            raise SourceMonitoringStateError(
                "dry-run completion cannot record accepted items or an import receipt",
                code="SOURCE_MONITORING_STATE_INVALID",
            )
        completed_at = self._now_ms()
        with self.store._lock, closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            run, state = self._run_for_update(connection, clean_run_id)
            initialization_receipt_json = ""
            initialization_receipt_sha256 = ""
            if clean_initialization is not None:
                if (
                    status != RUN_STATUS_SUCCEEDED
                    or run["dry_run"]
                    or normalized_errors
                    or counts["rejected_count"] != 0
                    or clean_error_code
                    or clean_error_message
                    or clean_initialization["config_version"]
                    != state["config_version"]
                    or clean_initialization["starting_checkpoint_sha256"]
                    != run["started_checkpoint_sha256"]
                    or clean_initialization["next_checkpoint_sha256"]
                    != checkpoint_sha256
                    or clean_initialization["adapter_duplicate_count"]
                    > counts["duplicate_count"]
                    or (
                        clean_initialization["candidate_count"]
                        + clean_initialization["adapter_duplicate_count"]
                        + counts["rejected_count"]
                        != counts["observed_count"]
                    )
                    or clean_initialization["selected_count"]
                    != (
                        counts["accepted_count"]
                        + counts["duplicate_count"]
                        - clean_initialization["adapter_duplicate_count"]
                    )
                    or clean_initialization["captured_at_ms"] > completed_at
                    or (
                        clean_initialization["mode"] == "seed_only"
                        and (
                            clean_initialization["selected_count"] != 0
                            or bool(clean_receipt)
                        )
                    )
                ):
                    raise SourceMonitoringStateError(
                        "initialization projection is not bound to the successful run",
                        code="SOURCE_MONITORING_INITIALIZATION_INVALID",
                    )
                initialization_receipt = _build_initialization_receipt(
                    run=run,
                    request=clean_initialization,
                    completed_at_ms=completed_at,
                    counts=counts,
                )
                initialization_receipt_json = canonical_json(initialization_receipt)
                if len(initialization_receipt_json) > 4096:
                    raise SourceMonitoringStateError(
                        "initialization receipt exceeds the bounded storage contract",
                        code="SOURCE_MONITORING_INITIALIZATION_INVALID",
                    )
                initialization_receipt_sha256 = canonical_sha256(
                    initialization_receipt
                )
            if clean_receipt:
                receipt_row = connection.execute(
                    """SELECT id,source_channel,source_key,external_run_id
                       FROM source_inbox_imports WHERE id=?""",
                    (clean_receipt,),
                ).fetchone()
                if (
                    receipt_row is None
                    or str(receipt_row["source_channel"]) != clean_source_channel
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
                       source_errors_json=?,receipt_id=?,dry_run=?,
                       initialization_mode=?,initialization_config_version=?,
                       initialization_preview_sha256=?,initialization_receipt_json=?,
                       initialization_receipt_sha256=?
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
                    clean_initialization["mode"] if clean_initialization else "",
                    (
                        clean_initialization["config_version"]
                        if clean_initialization
                        else ""
                    ),
                    (
                        clean_initialization["preview_sha256"]
                        if clean_initialization
                        else ""
                    ),
                    initialization_receipt_json,
                    initialization_receipt_sha256,
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

    def get_latest_successful_initialization(
        self,
        adapter_key: Any,
        *,
        config_version: Any,
    ) -> dict[str, Any] | None:
        """Return bounded, fully seal-verified initialization evidence."""

        with self.store._lock, closing(self._connect_read_only()) as connection:
            return self.read_latest_successful_initialization_from_connection(
                connection,
                adapter_key,
                config_version=config_version,
            )

    def read_latest_successful_initialization_from_connection(
        self,
        connection: sqlite3.Connection,
        adapter_key: Any,
        *,
        config_version: Any,
    ) -> dict[str, Any] | None:
        """Read sealed initialization evidence from an existing snapshot."""

        clean_key = normalize_adapter_key(adapter_key)
        clean_config = _clean_token(config_version, "config_version")
        rows = connection.execute(
            """SELECT * FROM source_adapter_runs
                WHERE adapter_key=? AND (
                      initialization_mode<>''
                      OR initialization_preview_sha256<>''
                      OR initialization_receipt_json<>''
                      OR initialization_receipt_sha256<>''
                  )
                ORDER BY completed_at_ms DESC,run_id DESC""",
            (clean_key,),
        ).fetchall()
        for row in rows:
            run = self._run_projection(row)
            initialization = run["initialization"]
            if initialization is None:  # pragma: no cover - guarded by query and seal
                raise SourceMonitoringStateError(
                    "initialization evidence is incomplete",
                    code="SOURCE_MONITORING_INITIALIZATION_RECEIPT_CORRUPT",
                )
            if initialization["config_version"] == clean_config:
                return initialization
        return None

    def get_run(self, run_id: Any) -> dict[str, Any] | None:
        clean_run_id = _clean_token(run_id, "run_id")
        with self.store._lock, closing(self._connect_read_only()) as connection:
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
        with self.store._lock, closing(self._connect_read_only()) as connection:
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
    "SOURCE_MONITORING_INITIALIZATION_MIGRATION_KEY",
    "SOURCE_MONITORING_INITIALIZATION_RECEIPT_VERSION",
    "SOURCE_MONITORING_INITIALIZATION_VERSION",
    "SOURCE_MONITORING_MIGRATION_KEY",
    "SourceMonitoringStateError",
    "SourceMonitoringStateRepository",
    "source_monitoring_initialization_schema_state",
    "ensure_source_monitoring_schema",
]
