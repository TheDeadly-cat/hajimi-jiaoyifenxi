from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat as statlib
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import DATABASE_PATH
from .database_migration_commit import (
    DatabaseMigrationCommitError,
    MigrationIntentJournal,
    MigrationIntentJournalError,
    _append_migration_gate_event,
    copy_to_same_directory_staging,
    hold_sqlite_file_lease,
    locked_raw_copy,
    publish_bytes_exclusive_durable,
    replace_file_with_backup,
    require_distinct_file_identities,
    require_no_sqlite_sidecars,
    require_unaliased_regular_file,
    scan_active_migration_operations,
)
from .instance_ownership import DatabaseInstanceOwner
from .path_identity import first_reparse_component
from .store import _initialize_migration_shadow


MIGRATION_MANIFEST_VERSION = "database_migration_manifest_v2"
MIGRATION_PREPARED_VERSION = "database_migration_prepared_v2"
MIGRATION_RECEIPT_VERSION = "database_migration_receipt_v2"
MIGRATION_ROLLBACK_RECEIPT_VERSION = "database_migration_rollback_receipt_v2"
AUTHORIZATION_PREFIX = "AUTHORIZE-MIGRATION-"


class DatabaseMigrationError(RuntimeError):
    pass


class DatabaseMigrationRequired(DatabaseMigrationError):
    def __init__(self, manifest: dict[str, Any]) -> None:
        self.manifest = manifest
        super().__init__(
            "Database migration is required; startup made no database changes. "
            f"Generate and review manifest {manifest['plan_sha256']} before authorizing it."
        )


class DatabaseMigrationRecoveryRequired(DatabaseMigrationError):
    def __init__(self, operations: list[dict[str, Any]]) -> None:
        self.operations = operations
        operation_ids = ", ".join(
            str(item.get("operation_id") or "unknown") for item in operations
        )
        super().__init__(
            "An incomplete or invalid database migration intent blocks startup. "
            f"Inspect and explicitly reconcile: {operation_ids}."
        )


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_value(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _normalize_migration_epoch_ms(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DatabaseMigrationError(
            "migration_epoch_ms must be a non-negative integer"
        )
    return value


def _resolve_unaliased_database_path(value: str | Path) -> Path:
    """Resolve a database path without accepting a symlink/reparse alias."""

    requested = Path(value).expanduser()
    offending_component = first_reparse_component(requested)
    if offending_component is not None:
        raise DatabaseMigrationError(
            "Database path may not contain a symlink or reparse point: "
            f"{offending_component}"
        )
    if os.path.lexists(os.fspath(requested)):
        try:
            metadata = requested.lstat()
        except OSError as exc:
            raise DatabaseMigrationError(
                f"Cannot inspect database path identity: {requested}"
            ) from exc
        reparse_flag = int(getattr(statlib, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
        if requested.is_symlink() or bool(
            reparse_flag
            and int(getattr(metadata, "st_file_attributes", 0) or 0) & reparse_flag
        ):
            raise DatabaseMigrationError(
                f"Database path may not be a symlink or reparse point: {requested}"
            )
    return requested.resolve()


def _resolve_unaliased_artifact_path(
    value: str | Path,
    *,
    label: str,
    require_existing_independent_file: bool = False,
) -> Path:
    """Resolve a migration artifact only after checking its raw path chain.

    Artifact paths are user-supplied CLI/API inputs. Resolving first would turn
    a path below a junction into an apparently ordinary destination and could
    make the manifest, backup, prepared file, or receipt land outside the
    reviewed namespace. Existing input artifacts are also required to be
    independent regular files so a hardlink cannot masquerade as a sealed
    artifact.
    """

    requested = Path(value).expanduser()
    offending_component = first_reparse_component(requested)
    if offending_component is not None:
        raise DatabaseMigrationError(
            f"{label} path may not contain a symlink or reparse point: "
            f"{offending_component}"
        )
    clean_path = requested.resolve()
    if require_existing_independent_file:
        try:
            require_unaliased_regular_file(clean_path, label=label)
        except DatabaseMigrationCommitError as exc:
            raise DatabaseMigrationError(str(exc)) from exc
    return clean_path


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sidecar_state(database_path: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for suffix in ("-wal", "-shm", "-journal"):
        path = Path(f"{database_path}{suffix}")
        exists = os.path.lexists(path)
        if exists:
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise DatabaseMigrationError(
                    f"Cannot inspect SQLite sidecar identity: {path}"
                ) from exc
            reparse_flag = int(
                getattr(statlib, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0
            )
            if (
                path.is_symlink()
                or bool(
                    reparse_flag
                    and int(getattr(metadata, "st_file_attributes", 0) or 0)
                    & reparse_flag
                )
                or not statlib.S_ISREG(metadata.st_mode)
            ):
                raise DatabaseMigrationError(
                    f"SQLite sidecar is not an unaliased regular file: {path}"
                )
        size = path.stat().st_size if exists else 0
        result[suffix[1:]] = {
            "exists": exists,
            "size": size,
            "sha256": _file_sha256(path) if exists else "",
        }
    return result


def _require_copyable_source(database_path: Path) -> dict[str, dict[str, Any]]:
    sidecars = _sidecar_state(database_path)
    if int(sidecars["wal"]["size"]) != 0:
        raise DatabaseMigrationError(
            "Read-only preflight refused a database with a non-empty WAL. "
            "Stop all writers and use an explicitly authorized checkpoint workflow first."
        )
    present = [name for name, state in sidecars.items() if state["exists"]]
    if present:
        raise DatabaseMigrationError(
            "Read-only preflight refused SQLite sidecars: "
            + ", ".join(sorted(present))
            + ". Stop all writers and recover or checkpoint through an explicitly "
            "authorized SQLite workflow first."
        )
    return sidecars


def _readonly_connection(database_path: Path) -> sqlite3.Connection:
    uri_path = quote(database_path.as_posix(), safe="/:")
    connection = sqlite3.connect(
        f"file:{uri_path}?mode=ro&immutable=1",
        uri=True,
        timeout=10,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _cell_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return {"float": repr(value)}
    if isinstance(value, bytes):
        return {"blob_hex": value.hex()}
    return {"text": str(value)}


def _table_content_snapshot(
    connection: sqlite3.Connection,
    table_name: str,
) -> dict[str, Any]:
    columns = [
        str(row[1])
        for row in connection.execute(
            f"PRAGMA table_info({_quoted_identifier(table_name)})"
        ).fetchall()
    ]
    row_hashes: list[str] = []
    query = f"SELECT * FROM {_quoted_identifier(table_name)}"
    for row in connection.execute(query):
        values = [_cell_value(row[index]) for index in range(len(columns))]
        row_hashes.append(_sha256_value(values))
    row_hashes.sort()
    digest = hashlib.sha256()
    for row_hash in row_hashes:
        digest.update(row_hash.encode("ascii"))
        digest.update(b"\n")
    return {
        "columns": columns,
        "row_count": len(row_hashes),
        "content_sha256": digest.hexdigest(),
    }


def _logical_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    objects = [
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table_name": str(row[2]),
            "sql": str(row[3] or ""),
        }
        for row in connection.execute(
            """SELECT type,name,tbl_name,sql
                 FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type,name"""
        ).fetchall()
    ]
    table_names = sorted(
        item["name"] for item in objects if item["type"] == "table"
    )
    tables = {
        table_name: _table_content_snapshot(connection, table_name)
        for table_name in table_names
    }
    migration_keys: list[str] = []
    if "schema_migrations" in tables:
        migration_keys = [
            str(row[0])
            for row in connection.execute(
                "SELECT key FROM schema_migrations ORDER BY key"
            ).fetchall()
        ]
    schema_sha256 = _sha256_value(objects)
    logical_payload = {
        "schema_sha256": schema_sha256,
        "migration_keys": migration_keys,
        "tables": tables,
    }
    return {
        **logical_payload,
        "logical_sha256": _sha256_value(logical_payload),
    }


def _database_snapshot(database_path: Path) -> dict[str, Any]:
    try:
        file_identity_before = require_unaliased_regular_file(
            database_path,
            label="SQLite migration image",
        )
    except DatabaseMigrationCommitError as exc:
        raise DatabaseMigrationError(str(exc)) from exc
    sidecars_before = _require_copyable_source(database_path)
    before_sha256 = _file_sha256(database_path)
    with closing(_readonly_connection(database_path)) as connection:
        integrity_rows = [
            str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall()
        ]
        foreign_key_rows = [
            list(row) for row in connection.execute("PRAGMA foreign_key_check").fetchall()
        ]
        journal_mode_row = connection.execute("PRAGMA journal_mode").fetchone()
        logical = _logical_snapshot(connection)
    after_sha256 = _file_sha256(database_path)
    sidecars_after = _sidecar_state(database_path)
    try:
        file_identity_after = require_unaliased_regular_file(
            database_path,
            label="SQLite migration image",
        )
    except DatabaseMigrationCommitError as exc:
        raise DatabaseMigrationError(str(exc)) from exc
    if (
        file_identity_before["device_id"] != file_identity_after["device_id"]
        or file_identity_before["file_id"] != file_identity_after["file_id"]
        or file_identity_before["link_count"] != file_identity_after["link_count"]
    ):
        raise DatabaseMigrationError(
            "Database physical file identity changed during read-only preflight"
        )
    if before_sha256 != after_sha256:
        raise DatabaseMigrationError("Database changed during read-only preflight")
    if sidecars_before != sidecars_after:
        raise DatabaseMigrationError(
            "Database sidecars changed during read-only preflight"
        )
    if integrity_rows != ["ok"]:
        raise DatabaseMigrationError(
            "SQLite integrity_check failed: " + "; ".join(integrity_rows[:20])
        )
    if foreign_key_rows:
        raise DatabaseMigrationError(
            f"SQLite foreign_key_check reported {len(foreign_key_rows)} violation(s)"
        )
    return {
        "file": {
            "size": database_path.stat().st_size,
            "sha256": before_sha256,
            "mtime_ns": database_path.stat().st_mtime_ns,
            "device_id": file_identity_after["device_id"],
            "file_id": file_identity_after["file_id"],
            "link_count": file_identity_after["link_count"],
        },
        "sidecars": sidecars_before,
        "sqlite": {
            "integrity_check": integrity_rows,
            "foreign_key_violation_count": len(foreign_key_rows),
            "journal_mode": str(journal_mode_row[0] if journal_mode_row else ""),
        },
        "logical": logical,
    }


def _schema_objects(database_path: Path) -> list[dict[str, str]]:
    with closing(_readonly_connection(database_path)) as connection:
        return [
            {
                "type": str(row[0]),
                "name": str(row[1]),
                "table_name": str(row[2]),
                "sql": str(row[3] or ""),
            }
            for row in connection.execute(
                """SELECT type,name,tbl_name,sql
                     FROM sqlite_master
                    WHERE name NOT LIKE 'sqlite_%'
                    ORDER BY type,name"""
            ).fetchall()
        ]


def _diff_snapshots(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    before_objects: list[dict[str, str]],
    after_objects: list[dict[str, str]],
) -> dict[str, Any]:
    before_map = {(item["type"], item["name"]): item for item in before_objects}
    after_map = {(item["type"], item["name"]): item for item in after_objects}
    schema_changes: list[dict[str, str]] = []
    for key in sorted(set(before_map) | set(after_map)):
        old = before_map.get(key)
        new = after_map.get(key)
        if old is None and new is not None:
            schema_changes.append({"action": "add", **new})
        elif old is not None and new is None:
            schema_changes.append({"action": "remove", **old})
        elif old != new and old is not None and new is not None:
            schema_changes.append(
                {
                    "action": "replace",
                    "type": key[0],
                    "name": key[1],
                    "table_name": new["table_name"],
                    "before_sql": old["sql"],
                    "after_sql": new["sql"],
                }
            )
    before_tables = before["logical"]["tables"]
    after_tables = after["logical"]["tables"]
    data_changes: list[dict[str, Any]] = []
    for table_name in sorted(set(before_tables) | set(after_tables)):
        old = before_tables.get(table_name, {})
        new = after_tables.get(table_name, {})
        if old.get("content_sha256") == new.get("content_sha256"):
            continue
        data_changes.append(
            {
                "table": table_name,
                "row_count_before": int(old.get("row_count", 0)),
                "row_count_after": int(new.get("row_count", 0)),
                "content_sha256_before": str(old.get("content_sha256", "")),
                "content_sha256_projected": str(new.get("content_sha256", "")),
            }
        )
    before_keys = set(before["logical"]["migration_keys"])
    after_keys = set(after["logical"]["migration_keys"])
    return {
        "schema_changes": schema_changes,
        "migration_keys_added": sorted(after_keys - before_keys),
        "migration_keys_removed": sorted(before_keys - after_keys),
        "data_changes": data_changes,
    }


def _projected_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    logical = snapshot["logical"]
    return {
        "schema_sha256": logical["schema_sha256"],
        "logical_sha256": logical["logical_sha256"],
        "migration_keys": logical["migration_keys"],
        "tables": {
            name: {
                "columns": list(table["columns"]),
                "row_count": int(table["row_count"]),
                "content_sha256": table["content_sha256"],
            }
            for name, table in sorted(logical["tables"].items())
        },
    }


def _authorization_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    before = manifest["before"]
    return {
        "version": manifest["version"],
        "database_path": manifest["database_path"],
        "migration_epoch_ms": manifest["migration_epoch_ms"],
        "requires_migration": manifest["requires_migration"],
        "source_file": before["file"],
        "source_sidecars": before["sidecars"],
        "source_logical_sha256": before["logical"]["logical_sha256"],
        "changes": {
            "schema_changes": manifest["changes"]["schema_changes"],
            "migration_keys_added": manifest["changes"]["migration_keys_added"],
            "migration_keys_removed": manifest["changes"]["migration_keys_removed"],
            "data_changes": [
                {
                    "table": item["table"],
                    "row_count_before": item["row_count_before"],
                    "row_count_after": item["row_count_after"],
                    "content_sha256_before": item["content_sha256_before"],
                    "content_sha256_projected": item[
                        "content_sha256_projected"
                    ],
                }
                for item in manifest["changes"]["data_changes"]
            ],
        },
        "projected_state": manifest["projected_state"],
    }


def build_migration_manifest(
    database_path: str | Path,
    *,
    migration_epoch_ms: int | None = None,
) -> dict[str, Any]:
    source_path = _resolve_unaliased_database_path(database_path)
    if not source_path.is_file():
        raise DatabaseMigrationError(
            f"Database does not exist: {source_path}. Formal startup will not create it."
        )
    pending_operations = scan_active_migration_operations(source_path)
    if pending_operations:
        raise DatabaseMigrationRecoveryRequired(pending_operations)
    epoch_ms = _normalize_migration_epoch_ms(
        int(time.time() * 1000)
        if migration_epoch_ms is None
        else migration_epoch_ms
    )
    before = _database_snapshot(source_path)
    before_objects = _schema_objects(source_path)
    with tempfile.TemporaryDirectory(prefix="ai-studio-migration-preview-") as temp_dir:
        preview_path = Path(temp_dir) / "preview.sqlite3"
        try:
            locked_raw_copy(source_path, preview_path)
        except DatabaseMigrationCommitError as exc:
            raise DatabaseMigrationError(
                f"Cannot take an exclusive read-only migration preview copy: {exc}"
            ) from exc
        if _file_sha256(preview_path) != before["file"]["sha256"]:
            raise DatabaseMigrationError("Preview copy hash does not match source database")
        _initialize_migration_shadow(preview_path, epoch_ms)
        with closing(sqlite3.connect(preview_path)) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        after = _database_snapshot(preview_path)
        after_objects = _schema_objects(preview_path)
    changes = _diff_snapshots(
        before,
        after,
        before_objects=before_objects,
        after_objects=after_objects,
    )
    manifest: dict[str, Any] = {
        "version": MIGRATION_MANIFEST_VERSION,
        "database_path": str(source_path),
        "migration_epoch_ms": epoch_ms,
        "generated_at_epoch_ms": int(time.time() * 1000),
        "requires_migration": before["logical"]["logical_sha256"]
        != after["logical"]["logical_sha256"],
        "before": before,
        "projected_after": after,
        "changes": changes,
        "projected_state": _projected_state(after),
    }
    manifest["plan_sha256"] = _sha256_value(_authorization_payload(manifest))
    return manifest


def assert_database_ready_for_startup(database_path: str | Path) -> dict[str, Any]:
    manifest = build_migration_manifest(database_path)
    if manifest["requires_migration"]:
        raise DatabaseMigrationRequired(manifest)
    return {
        "database_path": manifest["database_path"],
        "source_sha256": manifest["before"]["file"]["sha256"],
        "integrity_check": manifest["before"]["sqlite"]["integrity_check"],
        "foreign_key_violation_count": manifest["before"]["sqlite"][
            "foreign_key_violation_count"
        ],
        "wal_size": manifest["before"]["sidecars"]["wal"]["size"],
        "schema_sha256": manifest["before"]["logical"]["schema_sha256"],
        "plan_sha256": manifest["plan_sha256"],
        "startup_identity": {
            "main": {
                "exists": True,
                "size": manifest["before"]["file"]["size"],
                "sha256": manifest["before"]["file"]["sha256"],
            },
            "wal": manifest["before"]["sidecars"]["wal"],
            "shm": manifest["before"]["sidecars"]["shm"],
            "journal": manifest["before"]["sidecars"]["journal"],
        },
    }


def write_migration_manifest(manifest: dict[str, Any], path: str | Path) -> Path:
    output_path = _resolve_unaliased_artifact_path(
        path,
        label="Migration manifest",
    )
    database_path_value = str(manifest.get("database_path") or "").strip()
    if not database_path_value:
        raise DatabaseMigrationError(
            "Migration manifest must bind an explicit source database path"
        )
    source_path = _resolve_unaliased_database_path(database_path_value)
    _require_safe_migration_artifact_outputs(
        source_path,
        existing_inputs=[],
        outputs=[("Migration manifest", output_path)],
    )
    return _write_json_exclusive(manifest, output_path)


def _read_and_validate_manifest(
    path: str | Path,
) -> tuple[dict[str, Any], str]:
    manifest_path = _resolve_unaliased_artifact_path(
        path,
        label="Migration manifest",
        require_existing_independent_file=True,
    )
    try:
        raw_manifest = manifest_path.read_bytes()
        manifest = json.loads(raw_manifest.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DatabaseMigrationError(f"Cannot read migration manifest: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != MIGRATION_MANIFEST_VERSION:
        raise DatabaseMigrationError("Unsupported migration manifest")
    _normalize_migration_epoch_ms(manifest.get("migration_epoch_ms"))
    expected = _sha256_value(_authorization_payload(manifest))
    if manifest.get("plan_sha256") != expected:
        raise DatabaseMigrationError("Migration manifest digest is invalid")
    return manifest, hashlib.sha256(raw_manifest).hexdigest()


def _load_and_validate_manifest(path: str | Path) -> dict[str, Any]:
    manifest, _manifest_file_sha256 = _read_and_validate_manifest(path)
    return manifest


def _durable_copy(source: Path, destination: Path) -> dict[str, Any]:
    if destination.exists():
        raise DatabaseMigrationError(f"Backup path already exists: {destination}")
    if source == destination:
        raise DatabaseMigrationError("Backup path must differ from database path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as source_file, destination.open("xb") as output_file:
        shutil.copyfileobj(source_file, output_file, length=1024 * 1024)
        output_file.flush()
        os.fsync(output_file.fileno())
    source_sha256 = _file_sha256(source)
    destination_sha256 = _file_sha256(destination)
    if source_sha256 != destination_sha256:
        raise DatabaseMigrationError("Durable copy hash verification failed")
    return {
        "path": str(destination),
        "size": destination.stat().st_size,
        "sha256": destination_sha256,
        "verified_equal_to_source": True,
    }


def _post_migration_checkpoint(database_path: Path) -> None:
    with closing(sqlite3.connect(database_path, timeout=20)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _stable_changes(changes: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_changes": changes["schema_changes"],
        "migration_keys_added": changes["migration_keys_added"],
        "migration_keys_removed": changes["migration_keys_removed"],
        "data_changes": [
            {
                "table": item["table"],
                "row_count_before": item["row_count_before"],
                "row_count_after": item["row_count_after"],
                "content_sha256_before": item["content_sha256_before"],
                "content_sha256_projected": item[
                    "content_sha256_projected"
                ],
            }
            for item in changes["data_changes"]
        ],
    }


def _write_json_exclusive(payload: dict[str, Any], path: Path) -> Path:
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    try:
        return publish_bytes_exclusive_durable(path, encoded)
    except DatabaseMigrationCommitError as exc:
        raise DatabaseMigrationError(f"Cannot publish output {path}: {exc}") from exc


def _prepared_payload(prepared: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": prepared["version"],
        "database_path": prepared["database_path"],
        "migration_epoch_ms": prepared["migration_epoch_ms"],
        "manifest_path": prepared["manifest_path"],
        "manifest_file_sha256": prepared["manifest_file_sha256"],
        "plan_sha256": prepared["plan_sha256"],
        "changes": prepared["changes"],
        "source": prepared["source"],
        "backup": prepared["backup"],
        "candidate": prepared["candidate"],
    }


def prepare_migration(
    *,
    database_path: str | Path,
    manifest_path: str | Path,
    backup_path: str | Path,
    candidate_path: str | Path,
    prepared_path: str | Path,
) -> dict[str, Any]:
    """Create and verify backup plus migrated shadow before any authorization."""

    source_path = _resolve_unaliased_database_path(database_path)
    clean_manifest_path = _resolve_unaliased_artifact_path(
        manifest_path,
        label="Migration manifest",
        require_existing_independent_file=True,
    )
    clean_backup_path = _resolve_unaliased_artifact_path(
        backup_path,
        label="Verified backup",
    )
    clean_candidate_path = _resolve_unaliased_artifact_path(
        candidate_path,
        label="Migrated candidate",
    )
    clean_prepared_path = _resolve_unaliased_artifact_path(
        prepared_path,
        label="Prepared migration",
    )
    _require_safe_migration_artifact_outputs(
        source_path,
        existing_inputs=[("Migration manifest", clean_manifest_path)],
        outputs=[
            ("Verified backup", clean_backup_path),
            ("Migrated candidate", clean_candidate_path),
            ("Prepared migration", clean_prepared_path),
        ],
    )
    stored_manifest, manifest_file_sha256 = _read_and_validate_manifest(
        clean_manifest_path
    )
    if stored_manifest["database_path"] != str(source_path):
        raise DatabaseMigrationError("Manifest is bound to a different database path")
    if not stored_manifest.get("requires_migration"):
        raise DatabaseMigrationError("Manifest reports that no migration is required")
    migration_epoch_ms = _normalize_migration_epoch_ms(
        stored_manifest.get("migration_epoch_ms")
    )

    owner = DatabaseInstanceOwner(source_path).acquire(
        metadata={"operation": "migration_prepare"}
    )
    try:
        current_manifest = build_migration_manifest(
            source_path,
            migration_epoch_ms=migration_epoch_ms,
        )
        if current_manifest["plan_sha256"] != stored_manifest["plan_sha256"]:
            raise DatabaseMigrationError(
                "Database or migration plan changed after preview; generate a new manifest"
            )
        try:
            locked_raw_copy(source_path, clean_backup_path)
        except DatabaseMigrationCommitError as exc:
            raise DatabaseMigrationError(
                f"Cannot create the exclusive verified source backup: {exc}"
            ) from exc
        backup_snapshot = _database_snapshot(clean_backup_path)
        try:
            require_distinct_file_identities(
                {
                    "Source database": source_path,
                    "Verified backup": clean_backup_path,
                }
            )
        except DatabaseMigrationCommitError as exc:
            raise DatabaseMigrationError(
                f"Source and backup file identities are unsafe: {exc}"
            ) from exc
        source_snapshot = current_manifest["before"]
        if (
            backup_snapshot["file"]["sha256"] != source_snapshot["file"]["sha256"]
            or backup_snapshot["logical"] != source_snapshot["logical"]
            or backup_snapshot["sqlite"]["integrity_check"] != ["ok"]
            or backup_snapshot["sqlite"]["foreign_key_violation_count"] != 0
        ):
            raise DatabaseMigrationError("Verified backup does not match the source snapshot")

        with tempfile.TemporaryDirectory(
            prefix="ai-studio-migration-prepare-"
        ) as migration_temp_dir:
            shadow_path = Path(migration_temp_dir) / "candidate-shadow.sqlite3"
            _durable_copy(clean_backup_path, shadow_path)
            _initialize_migration_shadow(shadow_path, migration_epoch_ms)
            _post_migration_checkpoint(shadow_path)
            shadow_snapshot = _database_snapshot(shadow_path)
            if (
                shadow_snapshot["sqlite"]["integrity_check"] != ["ok"]
                or shadow_snapshot["sqlite"]["foreign_key_violation_count"] != 0
                or int(shadow_snapshot["sidecars"]["wal"]["size"]) != 0
            ):
                raise DatabaseMigrationError("Migrated shadow verification failed")
            _durable_copy(shadow_path, clean_candidate_path)

        candidate_snapshot = _database_snapshot(clean_candidate_path)
        try:
            require_distinct_file_identities(
                {
                    "Source database": source_path,
                    "Verified backup": clean_backup_path,
                    "Migrated candidate": clean_candidate_path,
                }
            )
        except DatabaseMigrationCommitError as exc:
            raise DatabaseMigrationError(
                f"Prepared SQLite images do not have distinct file identities: {exc}"
            ) from exc
        if (
            candidate_snapshot["file"]["sha256"]
            != shadow_snapshot["file"]["sha256"]
            or candidate_snapshot["logical"] != shadow_snapshot["logical"]
        ):
            raise DatabaseMigrationError(
                "Durable candidate does not match the verified migrated shadow"
            )
        candidate_changes = _diff_snapshots(
            source_snapshot,
            candidate_snapshot,
            before_objects=_schema_objects(clean_backup_path),
            after_objects=_schema_objects(clean_candidate_path),
        )
        if _stable_changes(candidate_changes) != _stable_changes(
            stored_manifest["changes"]
        ):
            raise DatabaseMigrationError(
                "Migrated candidate does not match the reviewed migration change list"
            )
        if _projected_state(candidate_snapshot) != stored_manifest["projected_state"]:
            raise DatabaseMigrationError(
                "Migrated candidate does not match the reviewed exact logical projection"
            )
        if (
            candidate_snapshot["sqlite"]["integrity_check"] != ["ok"]
            or candidate_snapshot["sqlite"]["foreign_key_violation_count"] != 0
            or int(candidate_snapshot["sidecars"]["wal"]["size"]) != 0
        ):
            raise DatabaseMigrationError("Migrated candidate verification failed")

        prepared: dict[str, Any] = {
            "version": MIGRATION_PREPARED_VERSION,
            "database_path": str(source_path),
            "migration_epoch_ms": migration_epoch_ms,
            "manifest_path": str(clean_manifest_path),
            "manifest_file_sha256": manifest_file_sha256,
            "plan_sha256": stored_manifest["plan_sha256"],
            "prepared_at_epoch_ms": int(time.time() * 1000),
            "changes": candidate_changes,
            "source": source_snapshot,
            "backup": {
                "path": str(clean_backup_path),
                "snapshot": backup_snapshot,
            },
            "candidate": {
                "path": str(clean_candidate_path),
                "snapshot": candidate_snapshot,
            },
        }
        prepared["prepared_sha256"] = _sha256_value(_prepared_payload(prepared))
        prepared["authorization_token"] = (
            AUTHORIZATION_PREFIX + prepared["prepared_sha256"]
        )
        _write_json_exclusive(prepared, clean_prepared_path)
        return prepared
    finally:
        owner.release()


def _load_and_validate_prepared(path: str | Path) -> dict[str, Any]:
    prepared_path = _resolve_unaliased_artifact_path(
        path,
        label="Prepared migration",
        require_existing_independent_file=True,
    )
    try:
        prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DatabaseMigrationError(f"Cannot read prepared migration: {exc}") from exc
    if not isinstance(prepared, dict) or prepared.get("version") != MIGRATION_PREPARED_VERSION:
        raise DatabaseMigrationError("Unsupported prepared migration")
    _normalize_migration_epoch_ms(prepared.get("migration_epoch_ms"))
    manifest_file_sha256 = str(
        prepared.get("manifest_file_sha256") or ""
    ).strip().lower()
    if (
        len(manifest_file_sha256) != 64
        or any(character not in "0123456789abcdef" for character in manifest_file_sha256)
    ):
        raise DatabaseMigrationError("Prepared migration manifest digest is invalid")
    expected = _sha256_value(_prepared_payload(prepared))
    if prepared.get("prepared_sha256") != expected:
        raise DatabaseMigrationError("Prepared migration digest is invalid")
    if prepared.get("authorization_token") != AUTHORIZATION_PREFIX + expected:
        raise DatabaseMigrationError("Prepared migration authorization token is invalid")
    return prepared


def _revalidate_prepared_manifest(prepared: dict[str, Any]) -> dict[str, Any]:
    manifest, manifest_file_sha256 = _read_and_validate_manifest(
        prepared["manifest_path"]
    )
    if manifest_file_sha256 != prepared["manifest_file_sha256"]:
        raise DatabaseMigrationError(
            "Migration manifest file digest changed after preparation"
        )
    if manifest["database_path"] != prepared["database_path"]:
        raise DatabaseMigrationError(
            "Prepared migration manifest is bound to another database path"
        )
    if manifest["plan_sha256"] != prepared["plan_sha256"]:
        raise DatabaseMigrationError(
            "Prepared migration manifest plan digest changed"
        )
    if manifest["migration_epoch_ms"] != prepared["migration_epoch_ms"]:
        raise DatabaseMigrationError(
            "Prepared migration manifest epoch changed"
        )
    if not manifest.get("requires_migration"):
        raise DatabaseMigrationError(
            "Prepared migration manifest no longer requires migration"
        )
    if _stable_changes(manifest["changes"]) != _stable_changes(
        prepared["changes"]
    ):
        raise DatabaseMigrationError(
            "Prepared migration changes do not match the exact manifest"
        )
    if manifest["projected_state"] != _projected_state(
        prepared["candidate"]["snapshot"]
    ):
        raise DatabaseMigrationError(
            "Prepared migration candidate does not match the exact manifest projection"
        )
    return manifest


def _snapshot_identity(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "file": snapshot["file"],
        "sidecars": snapshot["sidecars"],
        "sqlite": snapshot["sqlite"],
        "logical": snapshot["logical"],
    }


def _snapshot_matches_expected_image(
    snapshot: dict[str, Any],
    expected: dict[str, Any],
) -> bool:
    return bool(
        snapshot["file"]["size"] == expected["file"]["size"]
        and snapshot["file"]["sha256"] == expected["file"]["sha256"]
        and snapshot["logical"] == expected["logical"]
        and snapshot["sqlite"]["integrity_check"] == ["ok"]
        and snapshot["sqlite"]["foreign_key_violation_count"] == 0
        and all(
            not component["exists"]
            for component in snapshot["sidecars"].values()
        )
    )


def _require_closed_database_image(path: Path, *, label: str) -> None:
    try:
        require_no_sqlite_sidecars(path)
    except DatabaseMigrationCommitError as exc:
        raise DatabaseMigrationError(f"{label} is not a closed SQLite image: {exc}") from exc


def _path_entry_exists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _file_family_paths(path: Path) -> tuple[Path, ...]:
    return (path, *(Path(f"{path}{suffix}") for suffix in ("-wal", "-shm", "-journal")))


def _normalized_path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _file_family_keys(path: Path) -> set[str]:
    return {_normalized_path_key(item) for item in _file_family_paths(path)}


def _require_disjoint_file_families(
    entries: list[tuple[str, Path]],
) -> None:
    families = [
        (label, path, _file_family_keys(path))
        for label, path in entries
    ]
    for index, (label, path, family) in enumerate(families):
        for other_label, other_path, other_family in families[index + 1 :]:
            overlap = family & other_family
            if overlap:
                raise DatabaseMigrationError(
                    f"{label} file family overlaps {other_label}: "
                    f"{path} / {other_path}"
                )


def _require_safe_migration_artifact_outputs(
    source_path: Path,
    *,
    existing_inputs: list[tuple[str, Path]],
    outputs: list[tuple[str, Path]],
) -> None:
    owner_path = source_path.with_name(f"{source_path.name}.owner.lock")
    _require_disjoint_file_families(
        [
            ("Source database", source_path),
            ("Source owner lock", owner_path),
            *existing_inputs,
            *outputs,
        ]
    )
    source_parent_key = _normalized_path_key(source_path.parent)
    reserved_prefix = f".{source_path.name}.migration-".casefold()
    for label, path in outputs:
        if (
            _normalized_path_key(path.parent) == source_parent_key
            and path.name.casefold().startswith(reserved_prefix)
        ):
            raise DatabaseMigrationError(
                f"{label} uses the reserved migration recovery namespace: {path}"
            )
        _require_unused_file_family(path, label=label)


def _require_unused_file_family(path: Path, *, label: str) -> None:
    conflicts = [item for item in _file_family_paths(path) if _path_entry_exists(item)]
    if conflicts:
        raise DatabaseMigrationError(
            f"{label} file family already exists; nothing was removed: "
            + "; ".join(str(item) for item in conflicts)
        )


def _require_safe_receipt_path(
    receipt_path: Path,
    *,
    source_path: Path,
    prepared_path: Path,
    prepared: dict[str, Any],
) -> None:
    bound_paths = {
        source_path,
        source_path.with_name(f"{source_path.name}.owner.lock"),
        prepared_path,
        Path(prepared["manifest_path"]).expanduser().resolve(),
        Path(prepared["backup"]["path"]).expanduser().resolve(),
        Path(prepared["candidate"]["path"]).expanduser().resolve(),
    }
    reserved_paths = {
        os.path.normcase(os.path.abspath(os.fspath(item)))
        for path in bound_paths
        for item in _file_family_paths(path)
    }
    reserved_migration_name = (
        os.path.normcase(os.path.abspath(os.fspath(receipt_path.parent)))
        == os.path.normcase(os.path.abspath(os.fspath(source_path.parent)))
        and receipt_path.name.casefold().startswith(
            f".{source_path.name}.migration-".casefold()
        )
    )
    receipt_key = os.path.normcase(os.path.abspath(os.fspath(receipt_path)))
    if receipt_key in reserved_paths or reserved_migration_name:
        raise DatabaseMigrationError(
            "Receipt path overlaps a database, prepared artifact, SQLite sidecar, "
            "or migration recovery namespace"
        )


def _build_migration_receipt(
    *,
    prepared: dict[str, Any],
    authorization_token: str,
    receipt_path: Path,
    source_before: dict[str, Any],
    backup: dict[str, Any],
    candidate: dict[str, Any],
    after: dict[str, Any],
    atomic_rollback_path: Path,
    atomic_rollback: dict[str, Any],
    verified_marker: dict[str, Any],
    recovered: bool = False,
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "version": MIGRATION_RECEIPT_VERSION,
        "prepared_sha256": prepared["prepared_sha256"],
        "plan_sha256": prepared["plan_sha256"],
        "migration_epoch_ms": prepared["migration_epoch_ms"],
        "database_path": prepared["database_path"],
        "receipt_path": str(receipt_path),
        "completed_at_epoch_ms": int(time.time() * 1000),
        "recovered_from_pending_intent": bool(recovered),
        "authorization_token_sha256": hashlib.sha256(
            authorization_token.encode("utf-8")
        ).hexdigest(),
        "manifest": {
            "path": prepared["manifest_path"],
            "file_sha256": prepared["manifest_file_sha256"],
        },
        "intent_chain": {
            "operation_id": prepared["prepared_sha256"],
            "verified_marker_path": verified_marker["marker_path"],
            "verified_marker_sha256": verified_marker["marker_sha256"],
        },
        "source_before": {
            "sha256": source_before["file"]["sha256"],
            "logical_sha256": source_before["logical"]["logical_sha256"],
        },
        "backup": {
            "path": prepared["backup"]["path"],
            "sha256": backup["file"]["sha256"],
            "logical_sha256": backup["logical"]["logical_sha256"],
            "integrity_check": backup["sqlite"]["integrity_check"],
            "foreign_key_violation_count": backup["sqlite"][
                "foreign_key_violation_count"
            ],
            "verified_equal_to_source": True,
        },
        "candidate": {
            "path": prepared["candidate"]["path"],
            "sha256": candidate["file"]["sha256"],
            "logical_sha256": candidate["logical"]["logical_sha256"],
        },
        "atomic_rollback": {
            "path": str(atomic_rollback_path),
            "sha256": atomic_rollback["file"]["sha256"],
            "logical_sha256": atomic_rollback["logical"]["logical_sha256"],
            "matches_source_before": True,
        },
        "after": {
            "sha256": after["file"]["sha256"],
            "size": after["file"]["size"],
            "integrity_check": after["sqlite"]["integrity_check"],
            "foreign_key_violation_count": after["sqlite"][
                "foreign_key_violation_count"
            ],
            "wal_size": after["sidecars"]["wal"]["size"],
            "schema_sha256": after["logical"]["schema_sha256"],
            "logical_sha256": after["logical"]["logical_sha256"],
            "matches_authorized_candidate": True,
        },
    }
    receipt["receipt_sha256"] = _sha256_value(receipt)
    return receipt


def _complete_authorized_replacement_under_lease(
    *,
    source_path: Path,
    prepared: dict[str, Any],
    authorization_token: str,
    receipt_path: Path,
    source_snapshot: dict[str, Any],
    backup_snapshot: dict[str, Any],
    candidate_snapshot: dict[str, Any],
    atomic_rollback_path: Path,
    journal: MigrationIntentJournal,
    replacement_result: dict[str, Any],
) -> dict[str, Any]:
    journal.append("replace_returned", replacement_result)
    after = _database_snapshot(source_path)
    atomic_rollback_snapshot = _database_snapshot(atomic_rollback_path)
    if (
        not _snapshot_matches_expected_image(after, candidate_snapshot)
        or not _snapshot_matches_expected_image(
            atomic_rollback_snapshot,
            source_snapshot,
        )
    ):
        raise DatabaseMigrationRecoveryRequired([journal.inspect()])
    verified_marker = _append_migration_gate_event(
        journal,
        "verified",
        {
            "source_sha256": after["file"]["sha256"],
            "source_logical_sha256": after["logical"]["logical_sha256"],
            "atomic_rollback_sha256": atomic_rollback_snapshot["file"]["sha256"],
            "atomic_rollback_logical_sha256": atomic_rollback_snapshot["logical"][
                "logical_sha256"
            ],
        },
    )
    receipt = _build_migration_receipt(
        prepared=prepared,
        authorization_token=authorization_token,
        receipt_path=receipt_path,
        source_before=source_snapshot,
        backup=backup_snapshot,
        candidate=candidate_snapshot,
        after=after,
        atomic_rollback_path=atomic_rollback_path,
        atomic_rollback=atomic_rollback_snapshot,
        verified_marker=verified_marker,
    )
    _write_json_exclusive(receipt, receipt_path)
    receipt_details = {
        "receipt_path": str(receipt_path),
        "receipt_sha256": receipt["receipt_sha256"],
    }
    _append_migration_gate_event(journal, "receipt_committed", receipt_details)
    _append_migration_gate_event(journal, "complete", receipt_details)
    return receipt


def apply_authorized_migration(
    *,
    database_path: str | Path,
    prepared_path: str | Path,
    authorization_token: str,
    receipt_path: str | Path,
) -> dict[str, Any]:
    source_path = _resolve_unaliased_database_path(database_path)
    output_path = _resolve_unaliased_artifact_path(
        receipt_path,
        label="Migration receipt",
    )
    if _path_entry_exists(output_path):
        raise DatabaseMigrationError(f"Receipt path already exists: {output_path}")
    clean_prepared_path = _resolve_unaliased_artifact_path(
        prepared_path,
        label="Prepared migration",
        require_existing_independent_file=True,
    )
    prepared = _load_and_validate_prepared(clean_prepared_path)
    if str(source_path) != prepared["database_path"]:
        raise DatabaseMigrationError("Prepared migration is bound to another database path")
    _require_safe_receipt_path(
        output_path,
        source_path=source_path,
        prepared_path=clean_prepared_path,
        prepared=prepared,
    )
    expected_token = AUTHORIZATION_PREFIX + prepared["prepared_sha256"]
    if authorization_token != expected_token:
        raise DatabaseMigrationError(
            "Explicit authorization token does not match the prepared source, backup, "
            "candidate, and migration plan"
        )
    _revalidate_prepared_manifest(prepared)
    backup_path = _resolve_unaliased_artifact_path(
        prepared["backup"]["path"],
        label="Verified backup",
        require_existing_independent_file=True,
    )
    candidate_path = _resolve_unaliased_artifact_path(
        prepared["candidate"]["path"],
        label="Migrated candidate",
        require_existing_independent_file=True,
    )
    operation_id = str(prepared["prepared_sha256"])
    journal = MigrationIntentJournal(source_path, operation_id)
    atomic_rollback_path = source_path.parent / (
        f".{source_path.name}.migration-{operation_id}.source-before.sqlite3"
    )
    failed_candidate_path = source_path.parent / (
        f".{source_path.name}.migration-{operation_id}.failed-candidate.sqlite3"
    )
    existing_operation = journal.inspect()
    if existing_operation["exists"]:
        if existing_operation["active"]:
            raise DatabaseMigrationRecoveryRequired([existing_operation])
        raise DatabaseMigrationError(
            "This prepared migration already has a terminal intent journal; "
            "generate a new prepared artifact for another attempt"
        )
    active_operations = scan_active_migration_operations(source_path)
    if active_operations:
        raise DatabaseMigrationRecoveryRequired(active_operations)
    owner = DatabaseInstanceOwner(source_path).acquire(
        metadata={"operation": "migration_commit"}
    )
    intent_published = False
    try:
        _require_unused_file_family(
            atomic_rollback_path,
            label="Atomic rollback",
        )
        _require_unused_file_family(
            failed_candidate_path,
            label="Failed-candidate quarantine",
        )
        source_snapshot = _database_snapshot(source_path)
        backup_snapshot = _database_snapshot(backup_path)
        candidate_snapshot = _database_snapshot(candidate_path)
        if _snapshot_identity(source_snapshot) != _snapshot_identity(prepared["source"]):
            raise DatabaseMigrationError(
                "Source database changed after backup preparation; prepare again"
            )
        if _snapshot_identity(backup_snapshot) != _snapshot_identity(
            prepared["backup"]["snapshot"]
        ):
            raise DatabaseMigrationError("Verified backup changed after preparation")
        if _snapshot_identity(candidate_snapshot) != _snapshot_identity(
            prepared["candidate"]["snapshot"]
        ):
            raise DatabaseMigrationError("Migrated candidate changed after preparation")
        if (
            backup_snapshot["file"]["sha256"] != source_snapshot["file"]["sha256"]
            or backup_snapshot["logical"] != source_snapshot["logical"]
        ):
            raise DatabaseMigrationError("Backup no longer exactly represents the source")
        if (
            backup_snapshot["sqlite"]["integrity_check"] != ["ok"]
            or backup_snapshot["sqlite"]["foreign_key_violation_count"] != 0
            or candidate_snapshot["sqlite"]["integrity_check"] != ["ok"]
            or candidate_snapshot["sqlite"]["foreign_key_violation_count"] != 0
        ):
            raise DatabaseMigrationError("Backup or candidate SQLite verification failed")

        for path, label in (
            (source_path, "Source database"),
            (backup_path, "Verified backup"),
            (candidate_path, "Migrated candidate"),
        ):
            _require_closed_database_image(path, label=label)
        try:
            staging_path = copy_to_same_directory_staging(candidate_path, source_path)
        except DatabaseMigrationCommitError as exc:
            raise DatabaseMigrationError(
                f"Cannot create the same-directory migration staging image: {exc}"
            ) from exc
        staging_snapshot = _database_snapshot(staging_path)
        if (
            staging_snapshot["file"]["sha256"]
            != candidate_snapshot["file"]["sha256"]
            or staging_snapshot["logical"] != candidate_snapshot["logical"]
        ):
            raise DatabaseMigrationError("Atomic-commit staging verification failed")

        try:
            # The user-authorized evidence images remain immutable throughout
            # the swap, post-check, receipt publication, and terminal marker.
            # Re-snapshot only after both leases are held so the receipt never
            # records stale backup/candidate identities.
            with (
                hold_sqlite_file_lease(
                    backup_path,
                    expected_sha256=backup_snapshot["file"]["sha256"],
                ),
                hold_sqlite_file_lease(
                    candidate_path,
                    expected_sha256=candidate_snapshot["file"]["sha256"],
                ),
            ):
                locked_backup_snapshot = _database_snapshot(backup_path)
                locked_candidate_snapshot = _database_snapshot(candidate_path)
                if (
                    _snapshot_identity(locked_backup_snapshot)
                    != _snapshot_identity(backup_snapshot)
                    or _snapshot_identity(locked_candidate_snapshot)
                    != _snapshot_identity(candidate_snapshot)
                ):
                    raise DatabaseMigrationError(
                        "Verified backup or migrated candidate changed while acquiring leases"
                    )

                intent_details = {
                    "prepared_path": str(clean_prepared_path),
                    "prepared_sha256": operation_id,
                    "plan_sha256": prepared["plan_sha256"],
                    "manifest_path": prepared["manifest_path"],
                    "manifest_file_sha256": prepared["manifest_file_sha256"],
                    "authorization_token_sha256": hashlib.sha256(
                        authorization_token.encode("utf-8")
                    ).hexdigest(),
                    "receipt_path": str(output_path),
                    "paths": {
                        "staging": str(staging_path),
                        "atomic_rollback": str(atomic_rollback_path),
                        "failed_candidate": str(failed_candidate_path),
                        "verified_backup": str(backup_path),
                        "candidate": str(candidate_path),
                    },
                    "expected": {
                        "source_before": {
                            "size": source_snapshot["file"]["size"],
                            "sha256": source_snapshot["file"]["sha256"],
                            "logical_sha256": source_snapshot["logical"]["logical_sha256"],
                        },
                        "candidate": {
                            "size": locked_candidate_snapshot["file"]["size"],
                            "sha256": locked_candidate_snapshot["file"]["sha256"],
                            "logical_sha256": locked_candidate_snapshot["logical"]["logical_sha256"],
                        },
                    },
                }
                journal.append("intent", intent_details)
                intent_published = True
                journal.append("replace_started", {"staging": str(staging_path)})
                replacement_lease = replace_file_with_backup(
                    source_path,
                    staging_path,
                    atomic_rollback_path,
                    expected_replaced_sha256=source_snapshot["file"]["sha256"],
                    expected_replacement_sha256=staging_snapshot["file"]["sha256"],
                )
                with replacement_lease as replacement_result:
                    return _complete_authorized_replacement_under_lease(
                        source_path=source_path,
                        prepared=prepared,
                        authorization_token=authorization_token,
                        receipt_path=output_path,
                        source_snapshot=source_snapshot,
                        backup_snapshot=locked_backup_snapshot,
                        candidate_snapshot=locked_candidate_snapshot,
                        atomic_rollback_path=atomic_rollback_path,
                        journal=journal,
                        replacement_result=replacement_result,
                    )
        except DatabaseMigrationCommitError as exc:
            if intent_published:
                raise DatabaseMigrationRecoveryRequired([journal.inspect()]) from exc
            raise DatabaseMigrationError(
                "Cannot lock the verified backup and candidate through commit"
            ) from exc
    except DatabaseMigrationRecoveryRequired:
        raise
    except Exception as exc:
        if intent_published:
            pending_operations = scan_active_migration_operations(source_path)
            if pending_operations:
                raise DatabaseMigrationRecoveryRequired(pending_operations) from exc
        raise
    finally:
        owner.release()


def _load_migration_receipt(path: Path) -> dict[str, Any]:
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DatabaseMigrationError(f"Cannot read migration receipt: {exc}") from exc
    if not isinstance(receipt, dict) or receipt.get("version") != MIGRATION_RECEIPT_VERSION:
        raise DatabaseMigrationError("Migration receipt is invalid or unsupported")
    stored_sha256 = str(receipt.get("receipt_sha256") or "")
    payload = dict(receipt)
    payload.pop("receipt_sha256", None)
    if stored_sha256 != _sha256_value(payload):
        raise DatabaseMigrationError("Migration receipt digest is invalid")
    return receipt


def _validate_existing_migration_receipt(
    receipt: dict[str, Any],
    *,
    receipt_path: Path,
    prepared: dict[str, Any],
    authorization_token: str,
    backup_snapshot: dict[str, Any],
    candidate_snapshot: dict[str, Any],
    source_snapshot: dict[str, Any],
    rollback_path: Path,
    rollback_snapshot: dict[str, Any],
    journal: MigrationIntentJournal,
) -> None:
    expected_top_level = {
        "version",
        "prepared_sha256",
        "plan_sha256",
        "migration_epoch_ms",
        "database_path",
        "receipt_path",
        "completed_at_epoch_ms",
        "recovered_from_pending_intent",
        "authorization_token_sha256",
        "manifest",
        "intent_chain",
        "source_before",
        "backup",
        "candidate",
        "atomic_rollback",
        "after",
        "receipt_sha256",
    }
    if set(receipt) != expected_top_level:
        raise DatabaseMigrationError("Existing migration receipt schema is not closed")
    completed_at = receipt.get("completed_at_epoch_ms")
    if isinstance(completed_at, bool) or not isinstance(completed_at, int) or completed_at < 0:
        raise DatabaseMigrationError("Existing migration receipt timestamp is invalid")
    expected_values = {
        "prepared_sha256": prepared["prepared_sha256"],
        "plan_sha256": prepared["plan_sha256"],
        "migration_epoch_ms": prepared["migration_epoch_ms"],
        "database_path": prepared["database_path"],
        "receipt_path": str(receipt_path),
        "authorization_token_sha256": hashlib.sha256(
            authorization_token.encode("utf-8")
        ).hexdigest(),
        "manifest": {
            "path": prepared["manifest_path"],
            "file_sha256": prepared["manifest_file_sha256"],
        },
        "source_before": {
            "sha256": prepared["source"]["file"]["sha256"],
            "logical_sha256": prepared["source"]["logical"]["logical_sha256"],
        },
        "backup": {
            "path": prepared["backup"]["path"],
            "sha256": backup_snapshot["file"]["sha256"],
            "logical_sha256": backup_snapshot["logical"]["logical_sha256"],
            "integrity_check": backup_snapshot["sqlite"]["integrity_check"],
            "foreign_key_violation_count": backup_snapshot["sqlite"][
                "foreign_key_violation_count"
            ],
            "verified_equal_to_source": True,
        },
        "candidate": {
            "path": prepared["candidate"]["path"],
            "sha256": candidate_snapshot["file"]["sha256"],
            "logical_sha256": candidate_snapshot["logical"]["logical_sha256"],
        },
        "atomic_rollback": {
            "path": str(rollback_path),
            "sha256": rollback_snapshot["file"]["sha256"],
            "logical_sha256": rollback_snapshot["logical"]["logical_sha256"],
            "matches_source_before": True,
        },
        "after": {
            "sha256": source_snapshot["file"]["sha256"],
            "size": source_snapshot["file"]["size"],
            "integrity_check": source_snapshot["sqlite"]["integrity_check"],
            "foreign_key_violation_count": source_snapshot["sqlite"][
                "foreign_key_violation_count"
            ],
            "wal_size": source_snapshot["sidecars"]["wal"]["size"],
            "schema_sha256": source_snapshot["logical"]["schema_sha256"],
            "logical_sha256": source_snapshot["logical"]["logical_sha256"],
            "matches_authorized_candidate": True,
        },
    }
    for key, expected in expected_values.items():
        if receipt.get(key) != expected:
            raise DatabaseMigrationError(
                f"Existing migration receipt binding is invalid: {key}"
            )
    intent_chain = receipt.get("intent_chain")
    if not isinstance(intent_chain, dict) or set(intent_chain) != {
        "operation_id",
        "verified_marker_path",
        "verified_marker_sha256",
    }:
        raise DatabaseMigrationError("Existing migration receipt intent binding is invalid")
    if intent_chain.get("operation_id") != prepared["prepared_sha256"]:
        raise DatabaseMigrationError("Existing migration receipt operation is invalid")
    marker = next(
        (
            item
            for item in journal.inspect()["markers"]
            if item.get("marker_path") == intent_chain.get("verified_marker_path")
            and item.get("marker_sha256") == intent_chain.get("verified_marker_sha256")
        ),
        None,
    )
    if marker is None or marker.get("event") not in {"verified", "recovery_verified"}:
        raise DatabaseMigrationError("Existing migration receipt verified marker is invalid")
    marker_details = marker.get("details")
    if not isinstance(marker_details, dict):
        raise DatabaseMigrationError("Existing migration receipt marker details are invalid")
    if marker.get("event") == "verified":
        exact_marker_values = {
            "source_sha256": source_snapshot["file"]["sha256"],
            "source_logical_sha256": source_snapshot["logical"]["logical_sha256"],
            "atomic_rollback_sha256": rollback_snapshot["file"]["sha256"],
            "atomic_rollback_logical_sha256": rollback_snapshot["logical"][
                "logical_sha256"
            ],
        }
    else:
        exact_marker_values = {
            "source_sha256": source_snapshot["file"]["sha256"],
            "rollback_sha256": rollback_snapshot["file"]["sha256"],
        }
    if any(marker_details.get(key) != value for key, value in exact_marker_values.items()):
        raise DatabaseMigrationError("Existing migration receipt marker hashes are invalid")
    if receipt.get("recovered_from_pending_intent") is not (
        marker.get("event") == "recovery_verified"
    ):
        raise DatabaseMigrationError("Existing migration receipt recovery state is invalid")


def _build_rollback_receipt(
    *,
    operation_id: str,
    prepared: dict[str, Any],
    source_path: Path,
    receipt_path: Path,
    authorization_token: str,
    restored: dict[str, Any],
    failed_candidate_path: Path,
    quarantined: dict[str, Any],
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "version": MIGRATION_ROLLBACK_RECEIPT_VERSION,
        "operation_id": operation_id,
        "plan_sha256": prepared["plan_sha256"],
        "database_path": str(source_path),
        "receipt_path": str(receipt_path),
        "completed_at_epoch_ms": int(time.time() * 1000),
        "authorization_token_sha256": hashlib.sha256(
            authorization_token.encode("utf-8")
        ).hexdigest(),
        "restored_source_sha256": restored["file"]["sha256"],
        "restored_logical_sha256": restored["logical"]["logical_sha256"],
        "failed_candidate_path": str(failed_candidate_path),
        "failed_candidate_sha256": quarantined["file"]["sha256"],
        "integrity_check": restored["sqlite"]["integrity_check"],
        "foreign_key_violation_count": restored["sqlite"][
            "foreign_key_violation_count"
        ],
    }
    receipt["receipt_sha256"] = _sha256_value(receipt)
    return receipt


def _load_and_validate_rollback_receipt(
    path: Path,
    *,
    expected: dict[str, Any],
) -> dict[str, Any]:
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DatabaseMigrationError(f"Cannot read rollback receipt: {exc}") from exc
    if not isinstance(receipt, dict) or receipt.get("version") != MIGRATION_ROLLBACK_RECEIPT_VERSION:
        raise DatabaseMigrationError("Rollback receipt is invalid or unsupported")
    stored_sha256 = str(receipt.get("receipt_sha256") or "")
    payload = dict(receipt)
    payload.pop("receipt_sha256", None)
    if stored_sha256 != _sha256_value(payload):
        raise DatabaseMigrationError("Rollback receipt digest is invalid")
    if set(receipt) != set(expected) or any(
        receipt.get(key) != value
        for key, value in expected.items()
        if key not in {"completed_at_epoch_ms", "receipt_sha256"}
    ):
        raise DatabaseMigrationError("Rollback receipt binding is invalid")
    completed_at = receipt.get("completed_at_epoch_ms")
    if isinstance(completed_at, bool) or not isinstance(completed_at, int) or completed_at < 0:
        raise DatabaseMigrationError("Rollback receipt timestamp is invalid")
    return receipt


def _pending_operation_context(
    database_path: Path,
    operation_id: str,
) -> dict[str, Any]:
    journal = MigrationIntentJournal(database_path, operation_id)
    try:
        state = journal.inspect()
    except MigrationIntentJournalError as exc:
        raise DatabaseMigrationError(f"Migration intent journal is invalid: {exc}") from exc
    if not state["exists"] or not state["active"]:
        raise DatabaseMigrationError("The requested migration operation is not pending")
    intent = state["markers"][0]
    details = intent.get("details")
    if not isinstance(details, dict):
        raise DatabaseMigrationError("Pending migration intent details are invalid")
    prepared_path = _resolve_unaliased_artifact_path(
        str(details.get("prepared_path") or ""),
        label="Prepared migration",
        require_existing_independent_file=True,
    )
    prepared = _load_and_validate_prepared(prepared_path)
    if (
        prepared["prepared_sha256"] != operation_id
        or prepared["database_path"] != str(database_path)
        or details.get("prepared_sha256") != operation_id
        or details.get("plan_sha256") != prepared["plan_sha256"]
        or details.get("manifest_path") != prepared["manifest_path"]
        or details.get("manifest_file_sha256") != prepared["manifest_file_sha256"]
    ):
        raise DatabaseMigrationError("Pending migration intent is not bound to prepared data")
    _revalidate_prepared_manifest(prepared)
    paths = details.get("paths")
    if not isinstance(paths, dict):
        raise DatabaseMigrationError("Pending migration recovery paths are invalid")
    backup_path = _resolve_unaliased_artifact_path(
        str(paths.get("verified_backup") or ""),
        label="Verified backup",
        require_existing_independent_file=True,
    )
    candidate_path = _resolve_unaliased_artifact_path(
        str(paths.get("candidate") or ""),
        label="Migrated candidate",
        require_existing_independent_file=True,
    )
    rollback_path = _resolve_unaliased_artifact_path(
        str(paths.get("atomic_rollback") or ""),
        label="Atomic rollback",
    )
    failed_candidate_path = _resolve_unaliased_artifact_path(
        str(paths.get("failed_candidate") or ""),
        label="Failed-candidate quarantine",
    )
    staging_path = _resolve_unaliased_artifact_path(
        str(paths.get("staging") or ""),
        label="Migration staging",
    )
    receipt_path = _resolve_unaliased_artifact_path(
        str(details.get("receipt_path") or ""),
        label="Migration receipt",
    )
    expected_rollback_path = database_path.parent / (
        f".{database_path.name}.migration-{operation_id}.source-before.sqlite3"
    )
    expected_failed_candidate_path = database_path.parent / (
        f".{database_path.name}.migration-{operation_id}.failed-candidate.sqlite3"
    )
    expected_images = details.get("expected")
    expected_source_before = {
        "size": prepared["source"]["file"]["size"],
        "sha256": prepared["source"]["file"]["sha256"],
        "logical_sha256": prepared["source"]["logical"]["logical_sha256"],
    }
    expected_candidate = {
        "size": prepared["candidate"]["snapshot"]["file"]["size"],
        "sha256": prepared["candidate"]["snapshot"]["file"]["sha256"],
        "logical_sha256": prepared["candidate"]["snapshot"]["logical"][
            "logical_sha256"
        ],
    }
    _require_safe_receipt_path(
        receipt_path,
        source_path=database_path,
        prepared_path=prepared_path,
        prepared=prepared,
    )
    if (
        backup_path != Path(prepared["backup"]["path"]).expanduser().resolve()
        or candidate_path != Path(prepared["candidate"]["path"]).expanduser().resolve()
        or rollback_path != expected_rollback_path
        or failed_candidate_path != expected_failed_candidate_path
        or os.path.normcase(os.path.abspath(os.fspath(staging_path.parent)))
        != os.path.normcase(os.path.abspath(os.fspath(database_path.parent)))
        or not staging_path.name.casefold().startswith(
            f".{database_path.name}.migration-stage-".casefold()
        )
        or staging_path.suffix.casefold() != ".sqlite3"
        or receipt_path == database_path
        or not isinstance(expected_images, dict)
        or expected_images.get("source_before") != expected_source_before
        or expected_images.get("candidate") != expected_candidate
        or details.get("authorization_token_sha256")
        != hashlib.sha256(prepared["authorization_token"].encode("utf-8")).hexdigest()
    ):
        raise DatabaseMigrationError("Pending migration bindings drifted from prepared data")
    bound_recovery_paths = (
        database_path,
        prepared_path,
        Path(prepared["manifest_path"]).expanduser().resolve(),
        backup_path,
        candidate_path,
        rollback_path,
        failed_candidate_path,
        staging_path,
        receipt_path,
    )
    normalized_recovery_paths = {
        os.path.normcase(os.path.abspath(os.fspath(path)))
        for path in bound_recovery_paths
    }
    if len(normalized_recovery_paths) != len(bound_recovery_paths):
        raise DatabaseMigrationError("Pending migration recovery paths are not distinct")

    rollback_started_marker = next(
        (
            marker
            for marker in reversed(state["markers"])
            if marker.get("event") == "rollback_started"
        ),
        None,
    )
    if rollback_started_marker is not None:
        rollback_details = rollback_started_marker.get("details")
        if (
            not isinstance(rollback_details, dict)
            or Path(str(rollback_details.get("failed_candidate_path") or ""))
            .expanduser()
            .resolve()
            != failed_candidate_path
        ):
            raise DatabaseMigrationError(
                "Pending rollback marker is not bound to its exact failed-candidate path"
            )

    classification = "UNKNOWN"
    source_snapshot: dict[str, Any] | None = None
    rollback_snapshot: dict[str, Any] | None = None
    failed_candidate_snapshot: dict[str, Any] | None = None
    issue = ""
    try:
        _require_closed_database_image(database_path, label="Pending source database")
        source_snapshot = _database_snapshot(database_path)
        rollback_family_exists = any(
            _path_entry_exists(path) for path in _file_family_paths(rollback_path)
        )
        rollback_exists = _path_entry_exists(rollback_path)
        if rollback_family_exists and not rollback_exists:
            raise DatabaseMigrationError(
                "Atomic rollback sidecar exists without its main image"
            )
        if rollback_exists:
            _require_closed_database_image(rollback_path, label="Atomic rollback image")
            rollback_snapshot = _database_snapshot(rollback_path)
        failed_candidate_family_exists = any(
            _path_entry_exists(path)
            for path in _file_family_paths(failed_candidate_path)
        )
        failed_candidate_exists = _path_entry_exists(failed_candidate_path)
        if failed_candidate_family_exists and not failed_candidate_exists:
            raise DatabaseMigrationError(
                "Failed-candidate sidecar exists without its main image"
            )
        if failed_candidate_exists:
            _require_closed_database_image(
                failed_candidate_path,
                label="Failed candidate quarantine image",
            )
            failed_candidate_snapshot = _database_snapshot(failed_candidate_path)
        if _snapshot_matches_expected_image(source_snapshot, prepared["source"]):
            if (
                rollback_started_marker is not None
                and not rollback_exists
                and failed_candidate_snapshot is not None
                and _snapshot_matches_expected_image(
                    failed_candidate_snapshot,
                    prepared["candidate"]["snapshot"],
                )
            ):
                classification = "ROLLED_BACK_PENDING_RECEIPT"
            elif (
                rollback_started_marker is None
                and not rollback_exists
                and not failed_candidate_exists
            ):
                classification = "SOURCE_BEFORE"
        elif (
            _snapshot_matches_expected_image(
                source_snapshot,
                prepared["candidate"]["snapshot"],
            )
            and rollback_snapshot is not None
            and _snapshot_matches_expected_image(rollback_snapshot, prepared["source"])
            and not failed_candidate_exists
        ):
            classification = "CANDIDATE"
    except (DatabaseMigrationError, OSError, sqlite3.Error) as exc:
        issue = str(exc)
    return {
        "journal": journal,
        "state": state,
        "details": details,
        "prepared_path": prepared_path,
        "prepared": prepared,
        "backup_path": backup_path,
        "candidate_path": candidate_path,
        "rollback_path": rollback_path,
        "failed_candidate_path": failed_candidate_path,
        "receipt_path": receipt_path,
        "classification": classification,
        "classification_issue": issue,
        "source_snapshot": source_snapshot,
        "rollback_snapshot": rollback_snapshot,
        "failed_candidate_snapshot": failed_candidate_snapshot,
        "rollback_started": rollback_started_marker is not None,
    }


def recover_pending_database_migration(
    *,
    database_path: str | Path,
    operation_id: str,
    action: str,
    authorization_token: str | None = None,
) -> dict[str, Any]:
    """Explicitly inspect, finalize, roll back, or abort one pending commit.

    Startup never invokes this function.  Every mutating recovery action holds
    database ownership, validates the exact prepared artifact, and appends to
    the original immutable marker chain.
    """

    source_path = _resolve_unaliased_database_path(database_path)
    clean_action = str(action or "").strip().lower()
    if clean_action not in {"inspect", "abort", "finalize", "rollback"}:
        raise DatabaseMigrationError("Recovery action must be inspect, abort, finalize, or rollback")
    owner = DatabaseInstanceOwner(source_path).acquire(
        metadata={"operation": f"migration_recovery_{clean_action}"}
    )
    recovery_mutation_active = False
    try:
        context = _pending_operation_context(source_path, operation_id)
        public_state = {
            "operation_id": operation_id,
            "database_path": str(source_path),
            "classification": context["classification"],
            "classification_issue": context["classification_issue"],
            "last_event": context["state"].get("last_event"),
            "receipt_path": str(context["receipt_path"]),
            "atomic_rollback_path": str(context["rollback_path"]),
            "failed_candidate_path": str(context["failed_candidate_path"]),
        }
        if clean_action == "inspect":
            return {"version": "database_migration_recovery_status_v1", **public_state}

        prepared = context["prepared"]
        expected_token = AUTHORIZATION_PREFIX + prepared["prepared_sha256"]
        if clean_action in {"finalize", "rollback"} and authorization_token != expected_token:
            raise DatabaseMigrationError(
                "Recovery authorization token does not match the exact prepared migration"
            )
        journal: MigrationIntentJournal = context["journal"]
        classification = context["classification"]
        receipt_path: Path = context["receipt_path"]

        if clean_action == "abort":
            if classification != "SOURCE_BEFORE" or _path_entry_exists(receipt_path):
                raise DatabaseMigrationError(
                    "A pending migration may be aborted only while the source is exactly unchanged"
                )
            recovery_mutation_active = True
            with hold_sqlite_file_lease(
                source_path,
                expected_sha256=prepared["source"]["file"]["sha256"],
            ):
                current_journal = journal.inspect()
                if current_journal.get("last_event") == "abort_verified":
                    abort_details = current_journal["markers"][-1].get("details")
                    if not isinstance(abort_details, dict):
                        raise DatabaseMigrationError("Abort verification marker is invalid")
                else:
                    abort_details = public_state
                    _append_migration_gate_event(
                        journal,
                        "abort_verified",
                        abort_details,
                    )
                marker = _append_migration_gate_event(
                    journal,
                    "aborted",
                    abort_details,
                )
                return {
                    "version": "database_migration_recovery_result_v1",
                    "outcome": "aborted",
                    **public_state,
                    "terminal_marker_sha256": marker["marker_sha256"],
                }

        if classification not in {"CANDIDATE", "ROLLED_BACK_PENDING_RECEIPT"}:
            raise DatabaseMigrationError(
                "Pending migration files are not an exact recoverable candidate/source pair; "
                "all artifacts were preserved"
            )
        backup_snapshot = _database_snapshot(context["backup_path"])
        candidate_snapshot = _database_snapshot(context["candidate_path"])
        if (
            not _snapshot_matches_expected_image(backup_snapshot, prepared["source"])
            or not _snapshot_matches_expected_image(
                candidate_snapshot,
                prepared["candidate"]["snapshot"],
            )
        ):
            raise DatabaseMigrationError("Prepared backup or candidate drifted during recovery")
        recovery_mutation_active = True

        if clean_action == "finalize":
            if classification != "CANDIDATE" or context["rollback_started"]:
                raise DatabaseMigrationError(
                    "A rollback has started or completed; only exact rollback recovery is allowed"
                )
            with (
                hold_sqlite_file_lease(
                    source_path,
                    expected_sha256=context["source_snapshot"]["file"]["sha256"],
                ),
                hold_sqlite_file_lease(
                    context["rollback_path"],
                    expected_sha256=context["rollback_snapshot"]["file"]["sha256"],
                ),
                hold_sqlite_file_lease(
                    context["backup_path"],
                    expected_sha256=backup_snapshot["file"]["sha256"],
                ),
                hold_sqlite_file_lease(
                    context["candidate_path"],
                    expected_sha256=candidate_snapshot["file"]["sha256"],
                ),
            ):
                locked_source = _database_snapshot(source_path)
                locked_rollback = _database_snapshot(context["rollback_path"])
                if (
                    _snapshot_identity(locked_source)
                    != _snapshot_identity(context["source_snapshot"])
                    or _snapshot_identity(locked_rollback)
                    != _snapshot_identity(context["rollback_snapshot"])
                ):
                    raise DatabaseMigrationError(
                        "Recoverable migration images changed while acquiring leases"
                    )
                if _path_entry_exists(receipt_path):
                    receipt = _load_migration_receipt(receipt_path)
                    _validate_existing_migration_receipt(
                        receipt,
                        receipt_path=receipt_path,
                        prepared=prepared,
                        authorization_token=str(authorization_token),
                        backup_snapshot=backup_snapshot,
                        candidate_snapshot=candidate_snapshot,
                        source_snapshot=locked_source,
                        rollback_path=context["rollback_path"],
                        rollback_snapshot=locked_rollback,
                        journal=journal,
                    )
                else:
                    if journal.inspect().get("last_event") == "receipt_committed":
                        raise DatabaseMigrationError(
                            "Receipt marker exists but the exact receipt is missing"
                        )
                    verified_marker = _append_migration_gate_event(
                        journal,
                        "recovery_verified",
                        {
                            **public_state,
                            "source_sha256": locked_source["file"]["sha256"],
                            "rollback_sha256": locked_rollback["file"]["sha256"],
                        },
                    )
                    receipt = _build_migration_receipt(
                        prepared=prepared,
                        authorization_token=str(authorization_token),
                        receipt_path=receipt_path,
                        source_before=prepared["source"],
                        backup=backup_snapshot,
                        candidate=candidate_snapshot,
                        after=locked_source,
                        atomic_rollback_path=context["rollback_path"],
                        atomic_rollback=locked_rollback,
                        verified_marker=verified_marker,
                        recovered=True,
                    )
                    _write_json_exclusive(receipt, receipt_path)
                receipt_details = {
                    "receipt_path": str(receipt_path),
                    "receipt_sha256": receipt["receipt_sha256"],
                }
                if journal.inspect().get("last_event") != "receipt_committed":
                    _append_migration_gate_event(
                        journal,
                        "receipt_committed",
                        receipt_details,
                    )
                terminal = _append_migration_gate_event(
                    journal,
                    "complete",
                    receipt_details,
                )
                return {
                    "version": "database_migration_recovery_result_v1",
                    "outcome": "finalized",
                    **public_state,
                    "receipt_sha256": receipt["receipt_sha256"],
                    "terminal_marker_sha256": terminal["marker_sha256"],
                }

        failed_candidate_path: Path = context["failed_candidate_path"]

        def finish_rollback_receipt(
            restored: dict[str, Any],
            quarantined: dict[str, Any],
        ) -> dict[str, Any]:
            expected_rollback_receipt = _build_rollback_receipt(
                operation_id=operation_id,
                prepared=prepared,
                source_path=source_path,
                receipt_path=receipt_path,
                authorization_token=str(authorization_token),
                restored=restored,
                failed_candidate_path=failed_candidate_path,
                quarantined=quarantined,
            )
            if _path_entry_exists(receipt_path):
                rollback_receipt = _load_and_validate_rollback_receipt(
                    receipt_path,
                    expected=expected_rollback_receipt,
                )
            else:
                rollback_receipt = expected_rollback_receipt
                _write_json_exclusive(rollback_receipt, receipt_path)
            rollback_committed_details = {
                "receipt_path": str(receipt_path),
                "receipt_sha256": rollback_receipt["receipt_sha256"],
                "failed_candidate_path": str(failed_candidate_path),
            }
            current_journal = journal.inspect()
            if current_journal.get("last_event") == "rollback_receipt_committed":
                if (
                    current_journal["markers"][-1].get("details")
                    != rollback_committed_details
                ):
                    raise DatabaseMigrationError(
                        "Rollback receipt marker conflicts with the exact receipt"
                    )
            else:
                _append_migration_gate_event(
                    journal,
                    "rollback_receipt_committed",
                    rollback_committed_details,
                )
            terminal = _append_migration_gate_event(
                journal,
                "rolled_back",
                rollback_committed_details,
            )
            return {
                "version": "database_migration_recovery_result_v1",
                "outcome": "rolled_back",
                **public_state,
                "receipt_sha256": rollback_receipt["receipt_sha256"],
                "terminal_marker_sha256": terminal["marker_sha256"],
            }

        if classification == "CANDIDATE":
            if _path_entry_exists(receipt_path):
                raise DatabaseMigrationError(
                    "A committed receipt already exists; finalize rather than roll back"
                )
            if not context["rollback_started"]:
                _require_unused_file_family(
                    failed_candidate_path,
                    label="Failed-candidate quarantine",
                )
                _append_migration_gate_event(
                    journal,
                    "rollback_started",
                    {
                        **public_state,
                        "failed_candidate_path": str(failed_candidate_path),
                    },
                )
            try:
                replacement_lease = replace_file_with_backup(
                    source_path,
                    context["rollback_path"],
                    failed_candidate_path,
                    expected_replaced_sha256=context["source_snapshot"]["file"][
                        "sha256"
                    ],
                    expected_replacement_sha256=context["rollback_snapshot"]["file"][
                        "sha256"
                    ],
                )
                with replacement_lease:
                    restored = _database_snapshot(source_path)
                    quarantined = _database_snapshot(failed_candidate_path)
                    if (
                        not _snapshot_matches_expected_image(restored, prepared["source"])
                        or not _snapshot_matches_expected_image(
                            quarantined,
                            prepared["candidate"]["snapshot"],
                        )
                    ):
                        raise DatabaseMigrationRecoveryRequired([journal.inspect()])
                    with (
                        hold_sqlite_file_lease(
                            context["backup_path"],
                            expected_sha256=backup_snapshot["file"]["sha256"],
                        ),
                        hold_sqlite_file_lease(
                            context["candidate_path"],
                            expected_sha256=candidate_snapshot["file"]["sha256"],
                        ),
                    ):
                        return finish_rollback_receipt(restored, quarantined)
            except DatabaseMigrationCommitError as exc:
                raise DatabaseMigrationRecoveryRequired([journal.inspect()]) from exc
        with (
            hold_sqlite_file_lease(
                source_path,
                expected_sha256=context["source_snapshot"]["file"]["sha256"],
            ),
            hold_sqlite_file_lease(
                failed_candidate_path,
                expected_sha256=context["failed_candidate_snapshot"]["file"]["sha256"],
            ),
            hold_sqlite_file_lease(
                context["backup_path"],
                expected_sha256=backup_snapshot["file"]["sha256"],
            ),
            hold_sqlite_file_lease(
                context["candidate_path"],
                expected_sha256=candidate_snapshot["file"]["sha256"],
            ),
        ):
            restored = _database_snapshot(source_path)
            quarantined = _database_snapshot(failed_candidate_path)
            if (
                not _snapshot_matches_expected_image(restored, prepared["source"])
                or not _snapshot_matches_expected_image(
                    quarantined,
                    prepared["candidate"]["snapshot"],
                )
            ):
                raise DatabaseMigrationError(
                    "Rolled-back recovery images changed while acquiring leases"
                )
            return finish_rollback_receipt(restored, quarantined)
    except DatabaseMigrationRecoveryRequired:
        raise
    except Exception as exc:
        if recovery_mutation_active:
            pending_operations = scan_active_migration_operations(source_path)
            if pending_operations:
                raise DatabaseMigrationRecoveryRequired(pending_operations) from exc
        raise
    finally:
        owner.release()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only preview and explicitly authorized SQLite migration gate"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    preview = subparsers.add_parser("preview")
    preview.add_argument("--database", required=True)
    preview.add_argument("--manifest", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--database", required=True)
    prepare.add_argument("--manifest", required=True)
    prepare.add_argument("--backup", required=True)
    prepare.add_argument("--candidate", required=True)
    prepare.add_argument("--prepared", required=True)
    apply = subparsers.add_parser("apply")
    apply.add_argument("--database", required=True)
    apply.add_argument("--prepared", required=True)
    apply.add_argument("--receipt", required=True)
    apply.add_argument("--authorize", required=True)
    reconcile = subparsers.add_parser(
        "reconcile",
        help="explicitly inspect or reconcile one interrupted migration commit",
    )
    reconcile.add_argument("--database", required=True)
    reconcile.add_argument("--operation", required=True)
    reconcile.add_argument(
        "--action",
        required=True,
        choices=("inspect", "abort", "finalize", "rollback"),
    )
    reconcile.add_argument(
        "--authorize",
        help="exact prepared authorization token; required for finalize or rollback",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "preview":
        database_path = Path(args.database).expanduser().resolve()
        if not database_path.is_file():
            raise DatabaseMigrationError(f"Database does not exist: {database_path}")
        with DatabaseInstanceOwner(database_path):
            manifest = build_migration_manifest(database_path)
        output_path = write_migration_manifest(manifest, args.manifest)
        print(
            json.dumps(
                {
                    "manifest": str(output_path),
                    "requires_migration": manifest["requires_migration"],
                    "plan_sha256": manifest["plan_sha256"],
                    "migration_epoch_ms": manifest["migration_epoch_ms"],
                    "source_sha256": manifest["before"]["file"]["sha256"],
                    "projected_logical_sha256": manifest["projected_state"][
                        "logical_sha256"
                    ],
                    "integrity_check": manifest["before"]["sqlite"][
                        "integrity_check"
                    ],
                    "foreign_key_violation_count": manifest["before"]["sqlite"][
                        "foreign_key_violation_count"
                    ],
                    "wal_size": manifest["before"]["sidecars"]["wal"]["size"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "prepare":
        prepared = prepare_migration(
            database_path=args.database,
            manifest_path=args.manifest,
            backup_path=args.backup,
            candidate_path=args.candidate,
            prepared_path=args.prepared,
        )
        print(
            json.dumps(
                {
                    "prepared": str(Path(args.prepared).expanduser().resolve()),
                    "prepared_sha256": prepared["prepared_sha256"],
                    "migration_epoch_ms": prepared["migration_epoch_ms"],
                    "manifest_file_sha256": prepared["manifest_file_sha256"],
                    "authorization_token": prepared["authorization_token"],
                    "backup": prepared["backup"]["path"],
                    "backup_sha256": prepared["backup"]["snapshot"]["file"][
                        "sha256"
                    ],
                    "candidate": prepared["candidate"]["path"],
                    "candidate_sha256": prepared["candidate"]["snapshot"]["file"][
                        "sha256"
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "reconcile":
        result = recover_pending_database_migration(
            database_path=args.database,
            operation_id=args.operation,
            action=args.action,
            authorization_token=args.authorize,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    receipt = apply_authorized_migration(
        database_path=args.database,
        prepared_path=args.prepared,
        authorization_token=args.authorize,
        receipt_path=args.receipt,
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
