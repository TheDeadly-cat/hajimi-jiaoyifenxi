"""Fail-closed Source Monitoring operations and retention contracts.

Retention v1 deliberately retains every evidence row.  The only mutating
operation is an explicit, append-only policy attestation; it never deletes or
updates Source Inbox, adapter-state, checkpoint, or run-receipt data.
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..structured_logging import LOG_SCHEMA_VERSION, emit_event
from .contracts import MAX_NATIVE_INTEGER, canonical_json, canonical_sha256

if TYPE_CHECKING:
    from ..store import StudioStore


SOURCE_MONITORING_OPERATIONS_MIGRATION_KEY = "source_monitoring_operations_v1"
SOURCE_MONITORING_RETENTION_POLICY_VERSION = (
    "source_monitoring_retention_policy_v1"
)
SOURCE_MONITORING_RETENTION_PREVIEW_VERSION = (
    "source_monitoring_retention_preview_v1"
)
SOURCE_MONITORING_RETENTION_RECEIPT_VERSION = (
    "source_monitoring_retention_receipt_v1"
)
SOURCE_MONITORING_OPERATIONS_HEALTH_VERSION = (
    "source_monitoring_operations_health_v1"
)
SOURCE_MONITORING_RETENTION_CONFIRMATION = "RETAIN_ALL_EVIDENCE"

_RETENTION_TABLE = "source_monitoring_retention_receipts"
_RETENTION_TIME_INDEX = "idx_source_monitoring_retention_receipts_time"
_RETENTION_UPDATE_TRIGGER = "trg_source_monitoring_retention_receipts_no_update"
_RETENTION_DELETE_TRIGGER = "trg_source_monitoring_retention_receipts_no_delete"
_RETENTION_INSERT_GUARD_TRIGGER = (
    "trg_source_monitoring_retention_receipts_no_replace"
)
_MIGRATION_UPDATE_TRIGGER = "trg_source_monitoring_operations_marker_no_update"
_MIGRATION_DELETE_TRIGGER = "trg_source_monitoring_operations_marker_no_delete"
_MIGRATION_INSERT_GUARD_TRIGGER = (
    "trg_source_monitoring_operations_marker_no_replace"
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_RECEIPT_ID_RE = re.compile(r"source_retention_[0-9a-f]{32}\Z")

_RETENTION_COLUMNS = (
    "id",
    "record_version",
    "policy_version",
    "decision",
    "policy_sha256",
    "preview_sha256",
    "inventory_sha256",
    "eligible_rows",
    "deleted_rows",
    "source_rows_updated",
    "receipt_json",
    "receipt_sha256",
    "attested_at_ms",
)

_INVENTORY_TABLES = (
    "source_adapter_runs",
    "source_adapter_states",
    "source_inbox_attachments",
    "source_inbox_import_items",
    "source_inbox_imports",
    "source_inbox_items",
    "source_inbox_round_drafts",
    "source_inbox_state_events",
    "source_inbox_trading_impact_projections",
    _RETENTION_TABLE,
)

_RETENTION_TABLE_DDL = f"""CREATE TABLE {_RETENTION_TABLE} (
    id TEXT PRIMARY KEY,
    record_version TEXT NOT NULL
        CHECK(record_version='{SOURCE_MONITORING_RETENTION_RECEIPT_VERSION}'),
    policy_version TEXT NOT NULL
        CHECK(policy_version='{SOURCE_MONITORING_RETENTION_POLICY_VERSION}'),
    decision TEXT NOT NULL CHECK(decision='RETAIN_ALL'),
    policy_sha256 TEXT NOT NULL CHECK(length(policy_sha256)=64),
    preview_sha256 TEXT NOT NULL UNIQUE CHECK(length(preview_sha256)=64),
    inventory_sha256 TEXT NOT NULL CHECK(length(inventory_sha256)=64),
    eligible_rows INTEGER NOT NULL DEFAULT 0 CHECK(eligible_rows=0),
    deleted_rows INTEGER NOT NULL DEFAULT 0 CHECK(deleted_rows=0),
    source_rows_updated INTEGER NOT NULL DEFAULT 0 CHECK(source_rows_updated=0),
    receipt_json TEXT NOT NULL,
    receipt_sha256 TEXT NOT NULL UNIQUE CHECK(length(receipt_sha256)=64),
    attested_at_ms INTEGER NOT NULL CHECK(attested_at_ms>=0)
)"""
_RETENTION_TIME_INDEX_DDL = f"""CREATE INDEX {_RETENTION_TIME_INDEX}
ON {_RETENTION_TABLE}(attested_at_ms DESC,id DESC)"""
_RETENTION_UPDATE_TRIGGER_DDL = f"""CREATE TRIGGER {_RETENTION_UPDATE_TRIGGER}
BEFORE UPDATE ON {_RETENTION_TABLE}
BEGIN
    SELECT RAISE(ABORT,'source monitoring retention receipts are immutable');
END"""
_RETENTION_DELETE_TRIGGER_DDL = f"""CREATE TRIGGER {_RETENTION_DELETE_TRIGGER}
BEFORE DELETE ON {_RETENTION_TABLE}
BEGIN
    SELECT RAISE(ABORT,'source monitoring retention receipts are immutable');
END"""
_RETENTION_INSERT_GUARD_TRIGGER_DDL = (
    f"""CREATE TRIGGER {_RETENTION_INSERT_GUARD_TRIGGER}
BEFORE INSERT ON {_RETENTION_TABLE}
WHEN EXISTS (
    SELECT 1 FROM {_RETENTION_TABLE} existing
     WHERE existing.rowid=NEW.rowid
        OR existing.id=NEW.id
        OR existing.preview_sha256=NEW.preview_sha256
        OR existing.receipt_sha256=NEW.receipt_sha256
)
BEGIN
    SELECT RAISE(ABORT,'source monitoring retention receipts are immutable');
END"""
)
_MIGRATION_UPDATE_TRIGGER_DDL = f"""CREATE TRIGGER {_MIGRATION_UPDATE_TRIGGER}
BEFORE UPDATE ON schema_migrations
WHEN OLD.key='{SOURCE_MONITORING_OPERATIONS_MIGRATION_KEY}'
  OR NEW.key='{SOURCE_MONITORING_OPERATIONS_MIGRATION_KEY}'
BEGIN
    SELECT RAISE(ABORT,'source monitoring operations marker is immutable');
END"""
_MIGRATION_DELETE_TRIGGER_DDL = f"""CREATE TRIGGER {_MIGRATION_DELETE_TRIGGER}
BEFORE DELETE ON schema_migrations
WHEN OLD.key='{SOURCE_MONITORING_OPERATIONS_MIGRATION_KEY}'
BEGIN
    SELECT RAISE(ABORT,'source monitoring operations marker is immutable');
END"""
_MIGRATION_INSERT_GUARD_TRIGGER_DDL = (
    f"""CREATE TRIGGER {_MIGRATION_INSERT_GUARD_TRIGGER}
BEFORE INSERT ON schema_migrations
WHEN EXISTS (
    SELECT 1 FROM schema_migrations existing
     WHERE existing.key='{SOURCE_MONITORING_OPERATIONS_MIGRATION_KEY}'
       AND (
           existing.rowid=NEW.rowid
           OR NEW.key='{SOURCE_MONITORING_OPERATIONS_MIGRATION_KEY}'
       )
)
BEGIN
    SELECT RAISE(ABORT,'source monitoring operations marker is immutable');
END"""
)
_OPERATIONS_SCHEMA_DDL = (
    _RETENTION_TABLE_DDL,
    _RETENTION_TIME_INDEX_DDL,
    _RETENTION_UPDATE_TRIGGER_DDL,
    _RETENTION_DELETE_TRIGGER_DDL,
    _RETENTION_INSERT_GUARD_TRIGGER_DDL,
    _MIGRATION_UPDATE_TRIGGER_DDL,
    _MIGRATION_DELETE_TRIGGER_DDL,
    _MIGRATION_INSERT_GUARD_TRIGGER_DDL,
)
_EXPECTED_SCHEMA_OBJECTS = {
    _RETENTION_TABLE: ("table", _RETENTION_TABLE_DDL),
    _RETENTION_TIME_INDEX: ("index", _RETENTION_TIME_INDEX_DDL),
    _RETENTION_UPDATE_TRIGGER: ("trigger", _RETENTION_UPDATE_TRIGGER_DDL),
    _RETENTION_DELETE_TRIGGER: ("trigger", _RETENTION_DELETE_TRIGGER_DDL),
    _RETENTION_INSERT_GUARD_TRIGGER: (
        "trigger",
        _RETENTION_INSERT_GUARD_TRIGGER_DDL,
    ),
    _MIGRATION_UPDATE_TRIGGER: ("trigger", _MIGRATION_UPDATE_TRIGGER_DDL),
    _MIGRATION_DELETE_TRIGGER: ("trigger", _MIGRATION_DELETE_TRIGGER_DDL),
    _MIGRATION_INSERT_GUARD_TRIGGER: (
        "trigger",
        _MIGRATION_INSERT_GUARD_TRIGGER_DDL,
    ),
}


class SourceMonitoringOperationsError(RuntimeError):
    def __init__(self, message: str, *, code: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _native_non_negative(value: Any, label: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_NATIVE_INTEGER:
        raise SourceMonitoringOperationsError(
            f"{label} must be a non-negative native signed 64-bit integer",
            code="SOURCE_MONITORING_OPERATIONS_INTEGER_INVALID",
            status=400,
        )
    return value


def _sha256(value: Any, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise SourceMonitoringOperationsError(
            f"{label} is not a canonical SHA-256 digest",
            code="SOURCE_MONITORING_RETENTION_PREVIEW_INVALID",
            status=400,
        )
    return value


def _verified_inventory(
    value: Any,
    *,
    code: str,
    status: int,
) -> dict[str, Any]:
    invalid = (
        type(value) is not dict
        or set(value)
        != {
            "table_rows",
            "retained_normalized_packet_and_receipt_bytes",
            "evidence_rows_eligible_for_deletion",
        }
    )
    table_rows = value.get("table_rows") if type(value) is dict else None
    invalid = invalid or (
        type(table_rows) is not dict
        or set(table_rows) != set(_INVENTORY_TABLES)
        or any(type(count) is not int or count < 0 for count in table_rows.values())
        or type(value.get("retained_normalized_packet_and_receipt_bytes")) is not int
        or value.get("retained_normalized_packet_and_receipt_bytes") < 0
        or value.get("evidence_rows_eligible_for_deletion") != 0
    )
    if invalid:
        raise SourceMonitoringOperationsError(
            "Source Monitoring retention inventory is invalid.",
            code=code,
            status=status,
        )
    return copy.deepcopy(value)


def _load_canonical_object(raw: Any) -> dict[str, Any]:
    if type(raw) is not str:
        raise SourceMonitoringOperationsError(
            "Retention receipt JSON is corrupt.",
            code="SOURCE_MONITORING_RETENTION_RECEIPT_CORRUPT",
        )

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    try:
        value = json.loads(
            raw,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (RecursionError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SourceMonitoringOperationsError(
            "Retention receipt JSON is corrupt.",
            code="SOURCE_MONITORING_RETENTION_RECEIPT_CORRUPT",
        ) from exc
    if type(value) is not dict:
        raise SourceMonitoringOperationsError(
            "Retention receipt is corrupt.",
            code="SOURCE_MONITORING_RETENTION_RECEIPT_CORRUPT",
        )
    return value


def source_monitoring_retention_policy() -> dict[str, Any]:
    """Return the closed, deterministic retain-all v1 policy."""

    return {
        "version": SOURCE_MONITORING_RETENTION_POLICY_VERSION,
        "mode": "retain_all_evidence",
        "decision": "RETAIN_ALL",
        "database_records": [
            {
                "table": table,
                "disposition": "retain",
            }
            for table in _INVENTORY_TABLES
        ],
        "normalized_packets_retained": True,
        "sealed_import_receipts_retained": True,
        "adapter_checkpoints_retained": True,
        "adapter_run_receipts_append_only": True,
        "automatic_cleanup_enabled": False,
        "scheduled_cleanup_enabled": False,
        "evidence_deletion_allowed": False,
        "future_deletion_requires_new_policy_version": True,
        "future_deletion_requires_explicit_user_authorization": True,
        "structured_log_storage": "host_managed_stdout_jsonl",
        "structured_log_schema_version": LOG_SCHEMA_VERSION,
        "migration_artifact_retention": "operator_managed_after_verification",
    }


def _policy_sha256() -> str:
    return canonical_sha256(source_monitoring_retention_policy())


def _schema_objects(connection: sqlite3.Connection) -> dict[str, dict[str, str]]:
    names = tuple(_EXPECTED_SCHEMA_OBJECTS)
    placeholders = ",".join("?" for _name in names)
    rows = connection.execute(
        f"SELECT type,name,sql FROM sqlite_master WHERE name IN ({placeholders})",
        names,
    ).fetchall()
    return {
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1]): {
            "type": str(row["type"] if isinstance(row, sqlite3.Row) else row[0]),
            "sql": str(row["sql"] if isinstance(row, sqlite3.Row) else row[2]),
        }
        for row in rows
    }


def _operations_object_state(connection: sqlite3.Connection) -> str:
    objects = _schema_objects(connection)
    marker = connection.execute(
        "SELECT applied_at FROM schema_migrations WHERE key=?",
        (SOURCE_MONITORING_OPERATIONS_MIGRATION_KEY,),
    ).fetchone()
    if not objects and marker is None:
        return "migration_required"
    if set(objects) != set(_EXPECTED_SCHEMA_OBJECTS) or marker is None:
        raise SourceMonitoringOperationsError(
            "Source Monitoring operations schema is incomplete.",
            code="SOURCE_MONITORING_OPERATIONS_SCHEMA_INVALID",
        )
    for name, (expected_type, expected_sql) in _EXPECTED_SCHEMA_OBJECTS.items():
        if (
            objects[name]["type"] != expected_type
            or objects[name]["sql"] != expected_sql
        ):
            raise SourceMonitoringOperationsError(
                "Source Monitoring operations schema definition is invalid.",
                code="SOURCE_MONITORING_OPERATIONS_SCHEMA_INVALID",
            )
    columns = tuple(
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        for row in connection.execute(
            f"PRAGMA table_info({_RETENTION_TABLE})"
        ).fetchall()
    )
    if columns != _RETENTION_COLUMNS:
        raise SourceMonitoringOperationsError(
            "Source Monitoring operations schema columns are invalid.",
            code="SOURCE_MONITORING_OPERATIONS_SCHEMA_INVALID",
        )
    applied_at = marker["applied_at"] if isinstance(marker, sqlite3.Row) else marker[0]
    _native_non_negative(applied_at, "operations migration timestamp")
    return "current"


def ensure_source_monitoring_operations_schema(
    connection: sqlite3.Connection,
    *,
    applied_at_ms: int,
) -> None:
    """Add only the append-only policy-attestation schema."""

    _native_non_negative(applied_at_ms, "applied_at_ms")
    existing_names = set(_schema_objects(connection))
    existing_marker = connection.execute(
        "SELECT applied_at FROM schema_migrations WHERE key=?",
        (SOURCE_MONITORING_OPERATIONS_MIGRATION_KEY,),
    ).fetchone()
    if existing_marker is not None:
        if _operations_object_state(connection) != "current":  # pragma: no cover
            raise SourceMonitoringOperationsError(
                "Source Monitoring operations schema is invalid.",
                code="SOURCE_MONITORING_OPERATIONS_SCHEMA_INVALID",
            )
        return
    if existing_names:
        raise SourceMonitoringOperationsError(
            "Unmarked Source Monitoring operations schema objects already exist.",
            code="SOURCE_MONITORING_OPERATIONS_SCHEMA_INVALID",
        )
    connection.executescript(";\n".join(_OPERATIONS_SCHEMA_DDL) + ";")
    # Detect a pre-created same-name table with weakened columns before marking
    # the migration complete.  The surrounding migration candidate transaction
    # remains authoritative for publication.
    if set(_schema_objects(connection)) != set(_EXPECTED_SCHEMA_OBJECTS):
        raise SourceMonitoringOperationsError(
            "Source Monitoring operations schema objects are invalid.",
            code="SOURCE_MONITORING_OPERATIONS_SCHEMA_INVALID",
        )
    columns = tuple(
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        for row in connection.execute(
            f"PRAGMA table_info({_RETENTION_TABLE})"
        ).fetchall()
    )
    if columns != _RETENTION_COLUMNS:
        raise SourceMonitoringOperationsError(
            "Source Monitoring operations schema columns are invalid.",
            code="SOURCE_MONITORING_OPERATIONS_SCHEMA_INVALID",
        )
    connection.execute(
        """INSERT INTO schema_migrations(key,applied_at)
           VALUES(?,?)""",
        (SOURCE_MONITORING_OPERATIONS_MIGRATION_KEY, applied_at_ms),
    )
    if _operations_object_state(connection) != "current":  # pragma: no cover
        raise SourceMonitoringOperationsError(
            "Source Monitoring operations migration did not complete.",
            code="SOURCE_MONITORING_OPERATIONS_SCHEMA_INVALID",
        )


def _read_inventory(connection: sqlite3.Connection) -> dict[str, Any]:
    if _operations_object_state(connection) != "current":
        raise SourceMonitoringOperationsError(
            "Source Monitoring retention migration is required.",
            code="SOURCE_MONITORING_RETENTION_MIGRATION_REQUIRED",
        )
    table_rows: dict[str, int] = {}
    for table in _INVENTORY_TABLES:
        row = connection.execute(f"SELECT COUNT(*) AS value FROM {table}").fetchone()
        value = row["value"] if isinstance(row, sqlite3.Row) else row[0]
        table_rows[table] = _native_non_negative(value, f"{table} row count")
    payload_row = connection.execute(
        """SELECT COALESCE(SUM(
                   length(CAST(packet_json AS BLOB))
                   + length(CAST(receipt_json AS BLOB))
               ),0) AS value
             FROM source_inbox_imports"""
    ).fetchone()
    payload_bytes = (
        payload_row["value"] if isinstance(payload_row, sqlite3.Row) else payload_row[0]
    )
    return {
        "table_rows": table_rows,
        "retained_normalized_packet_and_receipt_bytes": _native_non_negative(
            payload_bytes,
            "retained packet and receipt bytes",
        ),
        "evidence_rows_eligible_for_deletion": 0,
    }


def _retention_safety(*, receipt_appended: bool) -> dict[str, Any]:
    return {
        "retention_receipts_appended": 1 if receipt_appended else 0,
        "evidence_rows_deleted": 0,
        "evidence_rows_updated": 0,
        "provider_calls_performed": 0,
        "network_requests_performed": 0,
        "market_calls_performed": 0,
        "formal_rounds_created": 0,
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


def _build_preview(
    connection: sqlite3.Connection,
    *,
    captured_at_ms: int,
) -> dict[str, Any]:
    captured_at = _native_non_negative(captured_at_ms, "captured_at_ms")
    policy = source_monitoring_retention_policy()
    inventory = _read_inventory(connection)
    preview: dict[str, Any] = {
        "version": SOURCE_MONITORING_RETENTION_PREVIEW_VERSION,
        "captured_at_ms": captured_at,
        "policy": policy,
        "policy_sha256": canonical_sha256(policy),
        "inventory": inventory,
        "inventory_sha256": canonical_sha256(inventory),
        "plan": {
            "decision": "RETAIN_ALL",
            "eligible_rows": 0,
            "deleted_rows": 0,
            "source_rows_updated": 0,
            "automatic": False,
            "requires_new_policy_version_for_deletion": True,
        },
        "required_confirmation": SOURCE_MONITORING_RETENTION_CONFIRMATION,
        "safety": _retention_safety(receipt_appended=False),
    }
    preview["preview_sha256"] = canonical_sha256(preview)
    return preview


def _verified_preview(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        raise SourceMonitoringOperationsError(
            "Retention preview must be an exact object.",
            code="SOURCE_MONITORING_RETENTION_PREVIEW_INVALID",
            status=400,
        )
    expected_fields = {
        "version",
        "captured_at_ms",
        "policy",
        "policy_sha256",
        "inventory",
        "inventory_sha256",
        "plan",
        "required_confirmation",
        "safety",
        "preview_sha256",
    }
    if set(value) != expected_fields:
        raise SourceMonitoringOperationsError(
            "Retention preview fields are invalid.",
            code="SOURCE_MONITORING_RETENTION_PREVIEW_INVALID",
            status=400,
        )
    try:
        captured_at_ms = _native_non_negative(
            value.get("captured_at_ms"),
            "captured_at_ms",
        )
        policy = source_monitoring_retention_policy()
        inventory = _verified_inventory(
            value.get("inventory"),
            code="SOURCE_MONITORING_RETENTION_PREVIEW_INVALID",
            status=400,
        )
        plan = {
            "decision": "RETAIN_ALL",
            "eligible_rows": 0,
            "deleted_rows": 0,
            "source_rows_updated": 0,
            "automatic": False,
            "requires_new_policy_version_for_deletion": True,
        }
        safety = _retention_safety(receipt_appended=False)
        policy_sha256 = _sha256(value.get("policy_sha256"), "policy_sha256")
        inventory_sha256 = _sha256(
            value.get("inventory_sha256"),
            "inventory_sha256",
        )
        preview_sha256 = _sha256(value.get("preview_sha256"), "preview_sha256")
        normalized = {
            "version": SOURCE_MONITORING_RETENTION_PREVIEW_VERSION,
            "captured_at_ms": captured_at_ms,
            "policy": policy,
            "policy_sha256": policy_sha256,
            "inventory": inventory,
            "inventory_sha256": inventory_sha256,
            "plan": plan,
            "required_confirmation": SOURCE_MONITORING_RETENTION_CONFIRMATION,
            "safety": safety,
            "preview_sha256": preview_sha256,
        }
        if (
            value.get("version") != SOURCE_MONITORING_RETENTION_PREVIEW_VERSION
            or value.get("policy") != policy
            or policy_sha256 != canonical_sha256(policy)
            or inventory_sha256 != canonical_sha256(inventory)
            or value.get("plan") != plan
            or value.get("required_confirmation")
            != SOURCE_MONITORING_RETENTION_CONFIRMATION
            or value.get("safety") != safety
        ):
            raise SourceMonitoringOperationsError(
                "Retention preview does not match the closed v1 policy.",
                code="SOURCE_MONITORING_RETENTION_PREVIEW_INVALID",
                status=400,
            )
        unsigned = {
            key: item
            for key, item in normalized.items()
            if key != "preview_sha256"
        }
    except SourceMonitoringOperationsError:
        raise
    except (RecursionError, TypeError, ValueError, OverflowError) as exc:
        raise SourceMonitoringOperationsError(
            "Retention preview contains unsupported nested data.",
            code="SOURCE_MONITORING_RETENTION_PREVIEW_INVALID",
            status=400,
        ) from exc
    if preview_sha256 != canonical_sha256(unsigned):
        raise SourceMonitoringOperationsError(
            "Retention preview seal is invalid.",
            code="SOURCE_MONITORING_RETENTION_PREVIEW_INVALID",
            status=400,
        )
    return normalized


def _verified_receipt_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    receipt = _load_canonical_object(data.get("receipt_json"))
    expected_fields = {
            "version",
            "receipt_id",
            "policy_version",
            "decision",
            "policy_sha256",
            "preview_sha256",
            "inventory_sha256",
            "inventory",
            "eligible_rows",
            "deleted_rows",
            "source_rows_updated",
            "attested_at_ms",
            "safety",
            "receipt_sha256",
        }
    if set(receipt) != expected_fields:
        raise SourceMonitoringOperationsError(
            "Retention receipt fields are corrupt.",
            code="SOURCE_MONITORING_RETENTION_RECEIPT_CORRUPT",
        )
    inventory = _verified_inventory(
        receipt.get("inventory"),
        code="SOURCE_MONITORING_RETENTION_RECEIPT_CORRUPT",
        status=409,
    )
    receipt_id = receipt.get("receipt_id")
    policy_sha256 = receipt.get("policy_sha256")
    preview_sha256 = receipt.get("preview_sha256")
    inventory_sha256 = receipt.get("inventory_sha256")
    receipt_sha256 = receipt.get("receipt_sha256")
    string_hashes = (
        policy_sha256,
        preview_sha256,
        inventory_sha256,
        receipt_sha256,
        data.get("policy_sha256"),
        data.get("preview_sha256"),
        data.get("inventory_sha256"),
        data.get("receipt_sha256"),
    )
    unsigned = {
        key: item for key, item in receipt.items() if key != "receipt_sha256"
    }
    if (
        type(receipt_id) is not str
        or _RECEIPT_ID_RE.fullmatch(receipt_id) is None
        or any(type(digest) is not str or _SHA256_RE.fullmatch(digest) is None for digest in string_hashes)
        or receipt.get("version") != SOURCE_MONITORING_RETENTION_RECEIPT_VERSION
        or receipt.get("receipt_id") != data.get("id")
        or receipt.get("policy_version")
        != SOURCE_MONITORING_RETENTION_POLICY_VERSION
        or receipt.get("decision") != "RETAIN_ALL"
        or policy_sha256 != _policy_sha256()
        or preview_sha256 != data.get("preview_sha256")
        or inventory_sha256 != data.get("inventory_sha256")
        or inventory_sha256 != canonical_sha256(inventory)
        or receipt.get("eligible_rows") != 0
        or receipt.get("deleted_rows") != 0
        or receipt.get("source_rows_updated") != 0
        or type(receipt.get("attested_at_ms")) is not int
        or receipt.get("attested_at_ms") < 0
        or receipt.get("attested_at_ms") != data.get("attested_at_ms")
        or receipt.get("safety") != _retention_safety(receipt_appended=True)
        or receipt_sha256 != canonical_sha256(unsigned)
        or data.get("record_version")
        != SOURCE_MONITORING_RETENTION_RECEIPT_VERSION
        or data.get("policy_version")
        != SOURCE_MONITORING_RETENTION_POLICY_VERSION
        or data.get("decision") != "RETAIN_ALL"
        or policy_sha256 != data.get("policy_sha256")
        or any(
            type(data.get(field)) is not int or data.get(field) != 0
            for field in ("eligible_rows", "deleted_rows", "source_rows_updated")
        )
        or type(data.get("attested_at_ms")) is not int
        or canonical_json(receipt) != data.get("receipt_json")
    ):
        raise SourceMonitoringOperationsError(
            "Retention receipt seal is corrupt.",
            code="SOURCE_MONITORING_RETENTION_RECEIPT_CORRUPT",
        )
    return receipt


def source_monitoring_operations_health(
    connection: sqlite3.Connection | None,
) -> dict[str, Any]:
    policy = source_monitoring_retention_policy()
    if connection is None:
        schema_status = "unavailable"
        receipt_count = 0
        latest_attested_at_ms = 0
        latest_receipt_sha256 = ""
    else:
        schema_status = _operations_object_state(connection)
        receipt_count = 0
        latest_attested_at_ms = 0
        latest_receipt_sha256 = ""
        if schema_status == "current":
            count_row = connection.execute(
                f"SELECT COUNT(*) AS value FROM {_RETENTION_TABLE}"
            ).fetchone()
            receipt_count = _native_non_negative(
                count_row["value"] if isinstance(count_row, sqlite3.Row) else count_row[0],
                "retention receipt count",
            )
            latest = connection.execute(
                f"""SELECT * FROM {_RETENTION_TABLE}
                     ORDER BY attested_at_ms DESC,id DESC LIMIT 1"""
            ).fetchone()
            if latest is not None:
                receipt = _verified_receipt_row(latest)
                latest_attested_at_ms = _native_non_negative(
                    receipt["attested_at_ms"],
                    "latest retention attestation timestamp",
                )
                latest_receipt_sha256 = str(receipt["receipt_sha256"])
    return {
        "version": SOURCE_MONITORING_OPERATIONS_HEALTH_VERSION,
        "schema_status": schema_status,
        "migration_key": SOURCE_MONITORING_OPERATIONS_MIGRATION_KEY,
        "structured_log_schema_version": LOG_SCHEMA_VERSION,
        "retention_policy_version": SOURCE_MONITORING_RETENTION_POLICY_VERSION,
        "retention_policy_sha256": canonical_sha256(policy),
        "retention_mode": policy["mode"],
        "automatic_cleanup_enabled": False,
        "scheduled_cleanup_enabled": False,
        "evidence_deletion_allowed": False,
        "retention_receipt_count": receipt_count,
        "latest_retention_attested_at_ms": latest_attested_at_ms,
        "latest_retention_receipt_sha256": latest_receipt_sha256,
        "runtime_liveness_verified": False,
    }


class SourceMonitoringRetentionService:
    """Preview and explicitly attest the retain-all policy."""

    def __init__(
        self,
        store: StudioStore,
        *,
        clock_ms: Callable[[], Any] | None = None,
        id_factory: Callable[[], Any] | None = None,
        event_sink: Callable[..., Any] | None = None,
    ) -> None:
        self.store = store
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1_000))
        self._id_factory = id_factory or (
            lambda: f"source_retention_{uuid.uuid4().hex}"
        )
        self._event_sink = event_sink or emit_event
        if not callable(self._clock_ms) or not callable(self._id_factory):
            raise SourceMonitoringOperationsError(
                "Retention service dependencies are invalid.",
                code="SOURCE_MONITORING_RETENTION_SERVICE_INVALID",
                status=500,
            )
        if not callable(self._event_sink):
            raise SourceMonitoringOperationsError(
                "Retention event sink is invalid.",
                code="SOURCE_MONITORING_RETENTION_EVENT_SINK_INVALID",
                status=500,
            )

    def _now_ms(self) -> int:
        return _native_non_negative(self._clock_ms(), "retention service clock")

    def _emit(self, event: str, *, severity: str, fields: dict[str, Any]) -> None:
        try:
            self._event_sink(event, severity=severity, fields=fields)
        except Exception:
            # Operational logging is deliberately outside the authoritative DB
            # transaction and can never change the retention decision.
            return

    def preview(self) -> dict[str, Any]:
        try:
            path = Path(self.store.path)
        except (AttributeError, TypeError, ValueError) as exc:
            raise SourceMonitoringOperationsError(
                "Source Monitoring retention store path is invalid.",
                code="SOURCE_MONITORING_RETENTION_STORE_INVALID",
                status=500,
            ) from exc
        if not path.is_file():
            raise SourceMonitoringOperationsError(
                "Source Monitoring retention database is unavailable.",
                code="SOURCE_MONITORING_RETENTION_UNAVAILABLE",
                status=503,
            )
        try:
            # Local import avoids a module cycle while sharing the same
            # sidecar-safe, immutable/copy snapshot contract as health reads.
            from .health_service import _health_snapshot_connection

            with self.store._lock:
                with _health_snapshot_connection(path) as connection:
                    connection.execute("BEGIN")
                    preview = _build_preview(
                        connection,
                        captured_at_ms=self._now_ms(),
                    )
        except SourceMonitoringOperationsError:
            raise
        except Exception as exc:
            raise SourceMonitoringOperationsError(
                "Source Monitoring retention preview could not be read.",
                code=getattr(
                    exc,
                    "code",
                    "SOURCE_MONITORING_RETENTION_READ_FAILED",
                ),
                status=getattr(exc, "status", 409),
            ) from exc
        self._emit(
            "source_monitoring_retention_previewed",
            severity="info",
            fields={
                "policy_version": SOURCE_MONITORING_RETENTION_POLICY_VERSION,
                "policy_sha256": preview["policy_sha256"],
                "eligible_rows": 0,
                "deleted_rows": 0,
            },
        )
        return preview

    def attest(self, preview: Any, *, confirmation: Any) -> dict[str, Any]:
        verified = _verified_preview(preview)
        if confirmation != SOURCE_MONITORING_RETENTION_CONFIRMATION:
            raise SourceMonitoringOperationsError(
                "Exact retain-all confirmation is required.",
                code="SOURCE_MONITORING_RETENTION_CONFIRMATION_REQUIRED",
                status=400,
            )
        receipt: dict[str, Any]
        idempotent_replay = False
        try:
            with self.store._lock, closing(self.store._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                if _operations_object_state(connection) != "current":
                    raise SourceMonitoringOperationsError(
                        "Source Monitoring retention migration is required.",
                        code="SOURCE_MONITORING_RETENTION_MIGRATION_REQUIRED",
                    )
                existing = connection.execute(
                    f"SELECT * FROM {_RETENTION_TABLE} WHERE preview_sha256=?",
                    (verified["preview_sha256"],),
                ).fetchone()
                if existing is not None:
                    receipt = _verified_receipt_row(existing)
                    idempotent_replay = True
                else:
                    # A sealed existing receipt is authoritative for an exact
                    # replay and must not depend on the current wall clock.
                    attested_at_ms = self._now_ms()
                    if attested_at_ms < verified["captured_at_ms"]:
                        raise SourceMonitoringOperationsError(
                            "Retention service clock moved behind the sealed preview.",
                            code="SOURCE_MONITORING_RETENTION_CLOCK_INVALID",
                        )
                    latest_row = connection.execute(
                        f"""SELECT * FROM {_RETENTION_TABLE}
                             ORDER BY attested_at_ms DESC,id DESC LIMIT 1"""
                    ).fetchone()
                    if latest_row is not None:
                        latest_receipt = _verified_receipt_row(latest_row)
                        latest_attested_at_ms = _native_non_negative(
                            latest_receipt["attested_at_ms"],
                            "latest retention attestation timestamp",
                        )
                        if attested_at_ms <= latest_attested_at_ms:
                            raise SourceMonitoringOperationsError(
                                "Retention attestation clock must advance monotonically.",
                                code="SOURCE_MONITORING_RETENTION_CLOCK_INVALID",
                            )
                    current_inventory = _read_inventory(connection)
                    if (
                        current_inventory != verified["inventory"]
                        or canonical_sha256(current_inventory)
                        != verified["inventory_sha256"]
                    ):
                        raise SourceMonitoringOperationsError(
                            "Retention inventory changed after preview.",
                            code="SOURCE_MONITORING_RETENTION_PREVIEW_STALE",
                        )
                    receipt_id = self._id_factory()
                    if (
                        type(receipt_id) is not str
                        or _RECEIPT_ID_RE.fullmatch(receipt_id) is None
                    ):
                        raise SourceMonitoringOperationsError(
                            "Retention receipt identity is invalid.",
                            code="SOURCE_MONITORING_RETENTION_ID_INVALID",
                            status=500,
                        )
                    receipt = {
                        "version": SOURCE_MONITORING_RETENTION_RECEIPT_VERSION,
                        "receipt_id": receipt_id,
                        "policy_version": SOURCE_MONITORING_RETENTION_POLICY_VERSION,
                        "decision": "RETAIN_ALL",
                        "policy_sha256": verified["policy_sha256"],
                        "preview_sha256": verified["preview_sha256"],
                        "inventory_sha256": verified["inventory_sha256"],
                        "inventory": verified["inventory"],
                        "eligible_rows": 0,
                        "deleted_rows": 0,
                        "source_rows_updated": 0,
                        "attested_at_ms": attested_at_ms,
                        "safety": _retention_safety(receipt_appended=True),
                    }
                    receipt["receipt_sha256"] = canonical_sha256(receipt)
                    connection.execute(
                        f"""INSERT INTO {_RETENTION_TABLE}(
                               id,record_version,policy_version,decision,
                               policy_sha256,preview_sha256,inventory_sha256,
                               eligible_rows,deleted_rows,source_rows_updated,
                               receipt_json,receipt_sha256,attested_at_ms
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            receipt_id,
                            SOURCE_MONITORING_RETENTION_RECEIPT_VERSION,
                            SOURCE_MONITORING_RETENTION_POLICY_VERSION,
                            "RETAIN_ALL",
                            verified["policy_sha256"],
                            verified["preview_sha256"],
                            verified["inventory_sha256"],
                            0,
                            0,
                            0,
                            canonical_json(receipt),
                            receipt["receipt_sha256"],
                            attested_at_ms,
                        ),
                    )
                    inserted = connection.execute(
                        f"SELECT * FROM {_RETENTION_TABLE} WHERE id=?",
                        (receipt_id,),
                    ).fetchone()
                    if inserted is None:
                        raise SourceMonitoringOperationsError(
                            "Retention receipt could not be read back.",
                            code="SOURCE_MONITORING_RETENTION_WRITE_FAILED",
                            status=500,
                        )
                    receipt = _verified_receipt_row(inserted)
        except SourceMonitoringOperationsError:
            raise
        except sqlite3.Error as exc:
            raise SourceMonitoringOperationsError(
                "Retention attestation failed closed.",
                code="SOURCE_MONITORING_RETENTION_WRITE_FAILED",
                status=409,
            ) from exc
        self._emit(
            "source_monitoring_retention_attested",
            severity="info",
            fields={
                "policy_version": SOURCE_MONITORING_RETENTION_POLICY_VERSION,
                "policy_sha256": receipt["policy_sha256"],
                "decision": "RETAIN_ALL",
                "eligible_rows": 0,
                "deleted_rows": 0,
                "source_rows_updated": 0,
                "idempotent_replay": idempotent_replay,
            },
        )
        return {
            "receipt": receipt,
            "idempotent_replay": idempotent_replay,
        }


__all__ = [
    "SOURCE_MONITORING_OPERATIONS_HEALTH_VERSION",
    "SOURCE_MONITORING_OPERATIONS_MIGRATION_KEY",
    "SOURCE_MONITORING_RETENTION_CONFIRMATION",
    "SOURCE_MONITORING_RETENTION_POLICY_VERSION",
    "SOURCE_MONITORING_RETENTION_PREVIEW_VERSION",
    "SOURCE_MONITORING_RETENTION_RECEIPT_VERSION",
    "SourceMonitoringOperationsError",
    "SourceMonitoringRetentionService",
    "ensure_source_monitoring_operations_schema",
    "source_monitoring_operations_health",
    "source_monitoring_retention_policy",
]
