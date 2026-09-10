"""Read-only, full-run inventories for bounded source-monitoring soak evidence.

The caller must supply the database path explicitly.  This module has no
formal-database discovery, migration, retention, Provider, market, or network
capability.  It reads a stable SQLite snapshot and seals every raw
``source_adapter_runs`` row using keyset pagination.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import stat as statlib
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..path_identity import first_reparse_component
from ..source_inbox_contracts import (
    SOURCE_IMPORT_STATUSES,
    SourceInboxContractError,
    build_source_import_receipt,
)
from .contracts import canonical_json, canonical_sha256


SOAK_DB_INVENTORY_VERSION = "source_monitoring_soak_db_inventory_v1"
SOAK_DB_INVENTORY_VERDICT_VERSION = (
    "source_monitoring_soak_db_inventory_verdict_v1"
)
SOAK_DB_RUN_EVIDENCE_VERSION = "source_monitoring_soak_db_run_evidence_v1"
SOAK_DB_INVENTORY_WRITE_VERSION = "source_monitoring_soak_db_inventory_write_v1"
SOAK_DB_SCAN_ORDER = "run_id_asc_keyset_v1"
MAX_SOAK_DB_SCAN_PAGE_SIZE = 500
MAX_SOAK_DB_VERDICT_ISSUES = 64
MAX_SOAK_SESSION_RUN_IDS = 100_000
MAX_SOAK_DB_INVENTORY_RUNS = 100_000
MAX_SOAK_DB_INVENTORY_BYTES = 64 * 1024 * 1024
MAX_SOAK_DB_RECEIPT_JSON_BYTES = 1024 * 1024
MAX_SOAK_DB_MAIN_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_SOAK_DB_WAL_FILE_BYTES = 512 * 1024 * 1024
MAX_SOAK_DB_SHM_FILE_BYTES = 64 * 1024 * 1024

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_RUN_ID_RE = re.compile(r"source_run_[0-9a-f]{32}\Z")
_ADAPTER_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ERROR_CODE_RE = re.compile(r"(?:[A-Z][A-Z0-9_]{0,159})?\Z")
_RUN_STATUSES = frozenset({
    "RUNNING",
    "SUCCEEDED",
    "DEGRADED",
    "FAILED",
    "DRY_RUN",
    "ABANDONED",
    # Reserved for the separately versioned Futu market-closed vertical slice.
    "SKIPPED",
})
_DECLARED_RUN_STATUSES = _RUN_STATUSES | {"DRY_RUN_FAILED"}
_REQUIRED_RUN_COLUMNS = frozenset({
    "run_id",
    "status",
    "receipt_id",
})
_REQUIRED_RECEIPT_COLUMNS = frozenset({
    "id",
    "record_version",
    "source_channel",
    "source_key",
    "external_run_id",
    "import_key_sha256",
    "source_payload_bytes",
    "source_payload_sha256",
    "normalized_packet_sha256",
    "packet_json",
    "receipt_json",
    "receipt_sha256",
    "status",
    "received_at",
    "created_at",
})
_SOURCE_INBOX_IMPORT_RECORD_VERSION = "source_inbox_import_record_v1"
_REQUIRED_TERMINAL_RUN_COLUMNS = _REQUIRED_RUN_COLUMNS | frozenset({
    "adapter_key",
    "observed_count",
    "accepted_count",
    "duplicate_count",
    "rejected_count",
    "error_code",
})
_SAFETY = {
    "database_writes_performed": 0,
    "network_requests_performed": 0,
    "provider_calls_performed": 0,
    "market_calls_performed": 0,
    "execution_capability": "none",
    "live_trading_allowed": False,
}


class SoakDbInventoryError(ValueError):
    """Raised when an inventory input or persisted evidence fails closed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _raise(code: str, message: str) -> None:
    raise SoakDbInventoryError(code, message)


def _clean_token(value: Any, *, field: str, maximum: int = 160) -> str:
    if type(value) is not str:
        _raise("SOAK_DB_INVENTORY_INVALID", f"{field} must be a native string")
    clean = value.strip()
    if (
        not clean
        or clean != value
        or len(clean) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in clean)
    ):
        _raise("SOAK_DB_INVENTORY_INVALID", f"{field} is invalid")
    return clean


def _clean_optional_token(value: Any, *, field: str, maximum: int = 200) -> str:
    if type(value) is not str:
        _raise("SOAK_DB_INVENTORY_INVALID", f"{field} must be a native string")
    clean = value.strip()
    if (
        clean != value
        or len(clean) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in clean)
    ):
        _raise("SOAK_DB_INVENTORY_INVALID", f"{field} is invalid")
    return clean


def _native_non_negative(value: Any, *, field: str) -> int:
    if type(value) is not int or value < 0:
        _raise(
            "SOAK_DB_INVENTORY_INVALID",
            f"{field} must be a non-negative native integer",
        )
    return value


def _sha256(value: Any, *, field: str, allow_empty: bool = False) -> str:
    if type(value) is not str or (
        not (allow_empty and value == "") and _SHA256_RE.fullmatch(value) is None
    ):
        _raise("SOAK_DB_INVENTORY_INVALID", f"{field} is not a canonical SHA-256")
    return value


def _path(value: Any) -> Path:
    if isinstance(value, bool) or not isinstance(value, (str, os.PathLike)):
        _raise(
            "SOAK_DB_INVENTORY_PATH_INVALID",
            "database_path must be an explicit filesystem path",
        )
    text = os.fspath(value)
    if not isinstance(text, str) or not text.strip():
        _raise(
            "SOAK_DB_INVENTORY_PATH_INVALID",
            "database_path must be an explicit filesystem path",
        )
    requested = Path(text).expanduser()
    if first_reparse_component(requested) is not None:
        _raise(
            "SOAK_DB_INVENTORY_PATH_INVALID",
            "database_path may not contain a symlink or reparse point",
        )
    try:
        metadata = requested.lstat()
        path = requested.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SoakDbInventoryError(
            "SOAK_DB_INVENTORY_PATH_INVALID",
            "database_path does not resolve to an existing file",
        ) from exc
    if (
        not statlib.S_ISREG(metadata.st_mode)
        or int(metadata.st_nlink) != 1
        or _reparse_flag(metadata)
        or not path.is_file()
    ):
        _raise(
            "SOAK_DB_INVENTORY_PATH_INVALID",
            "database_path must be an independent non-reparse regular file",
        )
    return path


def _connect_immutable_read_only(path: Path) -> sqlite3.Connection:
    uri_path = quote(path.as_posix(), safe="/:")
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
    uri_path = quote(path.as_posix(), safe="/:")
    connection = sqlite3.connect(
        f"file:{uri_path}?mode=ro",
        uri=True,
        timeout=10,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _file_signature(
    path: Path,
    *,
    maximum_bytes: int,
) -> tuple[int, int, int, int] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if (
        not statlib.S_ISREG(metadata.st_mode)
        or int(metadata.st_nlink) != 1
        or _reparse_flag(metadata)
        or metadata.st_size < 0
        or metadata.st_size > maximum_bytes
    ):
        _raise(
            "SOAK_DB_INVENTORY_SNAPSHOT_BUSY",
            "a database family member is aliased, non-regular, or oversized",
        )
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


def _database_family_signatures(
    path: Path,
) -> tuple[
    tuple[int, int, int, int] | None,
    tuple[int, int, int, int] | None,
    tuple[int, int, int, int] | None,
    tuple[int, int, int, int] | None,
]:
    return (
        _file_signature(path, maximum_bytes=MAX_SOAK_DB_MAIN_FILE_BYTES),
        _file_signature(
            Path(f"{path}-wal"),
            maximum_bytes=MAX_SOAK_DB_WAL_FILE_BYTES,
        ),
        _file_signature(
            Path(f"{path}-shm"),
            maximum_bytes=MAX_SOAK_DB_SHM_FILE_BYTES,
        ),
        _file_signature(
            Path(f"{path}-journal"),
            maximum_bytes=MAX_SOAK_DB_WAL_FILE_BYTES,
        ),
    )


def _copy_verified_family_member(
    source: Path,
    destination: Path,
    *,
    expected_signature: tuple[int, int, int, int],
    maximum_bytes: int,
) -> None:
    source_flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0) or 0)
    source_flags |= int(getattr(os, "O_NOFOLLOW", 0) or 0)
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    destination_flags |= int(getattr(os, "O_BINARY", 0) or 0)
    destination_flags |= int(getattr(os, "O_NOFOLLOW", 0) or 0)
    source_descriptor = -1
    destination_descriptor = -1
    try:
        source_descriptor = os.open(source, source_flags)
        opened = os.fstat(source_descriptor)
        opened_signature = (
            int(opened.st_dev),
            int(opened.st_ino),
            int(opened.st_size),
            int(opened.st_mtime_ns),
        )
        if (
            not statlib.S_ISREG(opened.st_mode)
            or int(opened.st_nlink) != 1
            or _reparse_flag(opened)
            or opened.st_size > maximum_bytes
            or opened_signature != expected_signature
            or first_reparse_component(source) is not None
        ):
            _raise(
                "SOAK_DB_INVENTORY_SNAPSHOT_BUSY",
                "a database family member changed before snapshot copy",
            )
        destination_descriptor = os.open(destination, destination_flags, 0o600)
        remaining = expected_signature[2]
        while remaining:
            chunk = os.read(source_descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise OSError("database family member ended before its sealed size")
            offset = 0
            while offset < len(chunk):
                written = os.write(destination_descriptor, chunk[offset:])
                if type(written) is not int or written <= 0:
                    raise OSError("snapshot copy made no progress")
                offset += written
            remaining -= len(chunk)
        if os.read(source_descriptor, 1):
            _raise(
                "SOAK_DB_INVENTORY_SNAPSHOT_BUSY",
                "a database family member grew during snapshot copy",
            )
        after_fd = os.fstat(source_descriptor)
        after_fd_signature = (
            int(after_fd.st_dev),
            int(after_fd.st_ino),
            int(after_fd.st_size),
            int(after_fd.st_mtime_ns),
        )
        if (
            after_fd_signature != expected_signature
            or _file_signature(source, maximum_bytes=maximum_bytes)
            != expected_signature
        ):
            _raise(
                "SOAK_DB_INVENTORY_SNAPSHOT_BUSY",
                "a database family member changed during snapshot copy",
            )
    finally:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        if source_descriptor >= 0:
            os.close(source_descriptor)


@contextmanager
def _read_only_snapshot(path: Path):
    """Open a stable view without joining or mutating the source WAL family."""

    journal_path = Path(f"{path}-journal")
    wal_path = Path(f"{path}-wal")
    shm_path = Path(f"{path}-shm")
    signatures_before = _database_family_signatures(path)
    if signatures_before[3] is not None:
        _raise(
            "SOAK_DB_INVENTORY_SNAPSHOT_BUSY",
            "the database has an active rollback journal",
        )
    if signatures_before[1] is None and signatures_before[2] is None:
        with closing(_connect_immutable_read_only(path)) as connection:
            yield connection
        signatures_after = _database_family_signatures(path)
        if signatures_after != signatures_before:
            _raise(
                "SOAK_DB_INVENTORY_SNAPSHOT_BUSY",
                "the database changed during the read-only inventory",
            )
        return

    try:
        with tempfile.TemporaryDirectory(
            prefix="ai-studio-source-monitor-soak-snapshot-"
        ) as temporary_directory:
            snapshot_path = Path(temporary_directory) / "soak.sqlite3"
            main_signature = signatures_before[0]
            if main_signature is None:
                _raise(
                    "SOAK_DB_INVENTORY_SNAPSHOT_BUSY",
                    "the database main file disappeared before snapshot copy",
                )
            _copy_verified_family_member(
                path,
                snapshot_path,
                expected_signature=main_signature,
                maximum_bytes=MAX_SOAK_DB_MAIN_FILE_BYTES,
            )
            if signatures_before[1] is not None:
                _copy_verified_family_member(
                    wal_path,
                    Path(f"{snapshot_path}-wal"),
                    expected_signature=signatures_before[1],
                    maximum_bytes=MAX_SOAK_DB_WAL_FILE_BYTES,
                )
            signatures_after = _database_family_signatures(path)
            if signatures_after != signatures_before:
                _raise(
                    "SOAK_DB_INVENTORY_SNAPSHOT_BUSY",
                    "the database changed during the read-only inventory copy",
                )
            with closing(_connect_read_only(snapshot_path)) as connection:
                yield connection
    except SoakDbInventoryError:
        raise
    except OSError as exc:
        raise SoakDbInventoryError(
            "SOAK_DB_INVENTORY_READ_FAILED",
            "the database WAL family could not be copied read-only",
        ) from exc


def _reparse_flag(metadata: os.stat_result) -> bool:
    flag = int(getattr(statlib, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    return bool(flag and attributes & flag)


def _require_independent_regular(
    metadata: os.stat_result,
    *,
    code: str,
) -> None:
    if (
        not statlib.S_ISREG(metadata.st_mode)
        or int(metadata.st_nlink) != 1
        or _reparse_flag(metadata)
    ):
        _raise(code, "inventory artifact must be an independent regular file")


def _fsync_parent_directory(path: Path) -> None:
    """Persist a newly created directory entry where directory fsync exists."""

    if os.name == "nt":
        return
    flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0) or 0)
    descriptor = os.open(path.parent, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _new_artifact_path(value: Any) -> Path:
    if isinstance(value, bool) or not isinstance(value, (str, os.PathLike)):
        _raise(
            "SOAK_DB_INVENTORY_ARTIFACT_PATH_INVALID",
            "inventory artifact path must be explicit",
        )
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw.strip():
        _raise(
            "SOAK_DB_INVENTORY_ARTIFACT_PATH_INVALID",
            "inventory artifact path must be explicit",
        )
    requested = Path(raw)
    if requested.suffix.lower() != ".json":
        _raise(
            "SOAK_DB_INVENTORY_ARTIFACT_PATH_INVALID",
            "inventory artifact path must end in .json",
        )
    if first_reparse_component(requested) is not None:
        _raise(
            "SOAK_DB_INVENTORY_ARTIFACT_PATH_INVALID",
            "inventory artifact path may not contain an alias",
        )
    parent = requested.parent.resolve(strict=False)
    if not parent.is_dir():
        _raise(
            "SOAK_DB_INVENTORY_ARTIFACT_PATH_INVALID",
            "inventory artifact parent directory must already exist",
        )
    resolved = (parent / requested.name).resolve(strict=False)
    if first_reparse_component(resolved) is not None:
        _raise(
            "SOAK_DB_INVENTORY_ARTIFACT_PATH_INVALID",
            "inventory artifact path may not contain an alias",
        )
    return resolved


def _existing_artifact_path(value: Any) -> Path:
    path = _new_artifact_path(value)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SoakDbInventoryError(
            "SOAK_DB_INVENTORY_ARTIFACT_READ_FAILED",
            "inventory artifact is unavailable",
        ) from exc
    _require_independent_regular(
        metadata,
        code="SOAK_DB_INVENTORY_ARTIFACT_PATH_INVALID",
    )
    if metadata.st_size > MAX_SOAK_DB_INVENTORY_BYTES:
        _raise(
            "SOAK_DB_INVENTORY_ARTIFACT_TOO_LARGE",
            "inventory artifact exceeds the bounded file size",
        )
    return path


def _page_size(value: Any) -> int:
    if (
        type(value) is not int
        or not 1 <= value <= MAX_SOAK_DB_SCAN_PAGE_SIZE
    ):
        _raise(
            "SOAK_DB_INVENTORY_PAGE_SIZE_INVALID",
            "page_size must be a native integer between 1 and 500",
        )
    return value


def _table_columns(
    connection: sqlite3.Connection,
    table: str,
    *,
    required: frozenset[str],
) -> list[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    columns = [str(row["name"]) for row in rows]
    if (
        not columns
        or len(columns) != len(set(columns))
        or not required.issubset(columns)
    ):
        _raise(
            "SOAK_DB_INVENTORY_SCHEMA_INVALID",
            f"{table} does not expose the required closed evidence columns",
        )
    return columns


def _raw_row(row: sqlite3.Row, columns: list[str]) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    for column in columns:
        value = row[column]
        if value is None or type(value) in {str, int}:
            raw[column] = value
            continue
        if type(value) is float and math.isfinite(value):
            raw[column] = value
            continue
        _raise(
            "SOAK_DB_INVENTORY_ROW_INVALID",
            "source_adapter_runs contains a non-canonical SQLite value",
        )
    try:
        canonical_json(raw)
    except Exception as exc:
        raise SoakDbInventoryError(
            "SOAK_DB_INVENTORY_ROW_INVALID",
            "source_adapter_runs row cannot be canonicalized",
        ) from exc
    return raw


def _decode_canonical_object(value: Any, *, field: str) -> dict[str, Any]:
    if type(value) is not str:
        _raise("SOAK_DB_INVENTORY_RECEIPT_INVALID", f"{field} must be text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SoakDbInventoryError(
            "SOAK_DB_INVENTORY_RECEIPT_INVALID",
            f"{field} is not valid UTF-8",
        ) from exc
    if not encoded or len(encoded) > MAX_SOAK_DB_RECEIPT_JSON_BYTES:
        _raise(
            "SOAK_DB_INVENTORY_RECEIPT_INVALID",
            f"{field} exceeds the bounded canonical envelope",
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
        parsed = json.loads(
            value,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
        if type(parsed) is not dict or canonical_json(parsed) != value:
            raise ValueError("receipt JSON is not a canonical object")
    except (OverflowError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SoakDbInventoryError(
            "SOAK_DB_INVENTORY_RECEIPT_INVALID",
            f"{field} is not a canonical JSON object",
        ) from exc
    return parsed


def _receipt_hashes(
    connection: sqlite3.Connection,
    run_receipts: dict[str, str],
) -> dict[str, str]:
    if not run_receipts:
        return {}
    receipt_ids = sorted(set(run_receipts.values()), key=lambda item: item.encode("utf-8"))
    by_id: dict[str, tuple[str, str]] = {}
    for expected_receipt_id in receipt_ids:
        row = connection.execute(
            """SELECT id,record_version,source_channel,source_key,external_run_id,
                      import_key_sha256,source_payload_bytes,source_payload_sha256,
                      normalized_packet_sha256,receipt_sha256,status,received_at,
                      created_at,
                      length(CAST(packet_json AS BLOB)) AS packet_json_bytes,
                      length(CAST(receipt_json AS BLOB)) AS receipt_json_bytes
                 FROM source_inbox_imports
                WHERE id=?""",
            (expected_receipt_id,),
        ).fetchone()
        if row is None:
            continue
        receipt_id = _clean_token(row["id"], field="source_inbox_imports.id", maximum=200)
        if receipt_id != expected_receipt_id:
            _raise(
                "SOAK_DB_INVENTORY_RECEIPT_INVALID",
                "receipt lookup returned an unexpected identity",
            )
        external_run_id = _clean_token(
            row["external_run_id"],
            field="source_inbox_imports.external_run_id",
        )
        stored_receipt_sha256 = _sha256(
            row["receipt_sha256"],
            field="source_inbox_imports.receipt_sha256",
        )
        source_payload_sha256 = _sha256(
            row["source_payload_sha256"],
            field="source_inbox_imports.source_payload_sha256",
        )
        normalized_packet_sha256 = _sha256(
            row["normalized_packet_sha256"],
            field="source_inbox_imports.normalized_packet_sha256",
        )
        import_key_sha256 = _sha256(
            row["import_key_sha256"],
            field="source_inbox_imports.import_key_sha256",
        )
        packet_bytes = _native_non_negative(
            row["packet_json_bytes"],
            field="source_inbox_imports.packet_json_bytes",
        )
        receipt_bytes = _native_non_negative(
            row["receipt_json_bytes"],
            field="source_inbox_imports.receipt_json_bytes",
        )
        if (
            packet_bytes == 0
            or receipt_bytes == 0
            or packet_bytes > MAX_SOAK_DB_RECEIPT_JSON_BYTES
            or receipt_bytes > MAX_SOAK_DB_RECEIPT_JSON_BYTES
        ):
            _raise(
                "SOAK_DB_INVENTORY_RECEIPT_INVALID",
                "receipt JSON exceeds the bounded canonical envelope",
            )
        json_row = connection.execute(
            """SELECT packet_json,receipt_json
                 FROM source_inbox_imports
                WHERE id=?""",
            (receipt_id,),
        ).fetchone()
        if json_row is None:
            _raise(
                "SOAK_DB_INVENTORY_RECEIPT_MISSING",
                "a receipt disappeared from the stable snapshot",
            )
        packet = _decode_canonical_object(
            json_row["packet_json"],
            field="source_inbox_imports.packet_json",
        )
        receipt = _decode_canonical_object(
            json_row["receipt_json"],
            field="source_inbox_imports.receipt_json",
        )
        source_payload_bytes = _native_non_negative(
            row["source_payload_bytes"],
            field="source_inbox_imports.source_payload_bytes",
        )
        received_at = _native_non_negative(
            row["received_at"],
            field="source_inbox_imports.received_at",
        )
        created_at = _native_non_negative(
            row["created_at"],
            field="source_inbox_imports.created_at",
        )
        status = _clean_token(
            row["status"],
            field="source_inbox_imports.status",
            maximum=40,
        )
        try:
            expected_receipt = build_source_import_receipt(
                packet,
                received_at_ms=received_at,
                source_payload_bytes=source_payload_bytes,
                source_payload_sha256=source_payload_sha256,
                status=status,
            )
        except SourceInboxContractError as exc:
            raise SoakDbInventoryError(
                "SOAK_DB_INVENTORY_RECEIPT_INVALID",
                "source inbox receipt could not be independently rebuilt",
            ) from exc
        if (
            row["record_version"] != _SOURCE_INBOX_IMPORT_RECORD_VERSION
            or status not in SOURCE_IMPORT_STATUSES
            or created_at != received_at
            or receipt != expected_receipt
            or stored_receipt_sha256 != expected_receipt.get("receipt_sha256")
            or normalized_packet_sha256 != canonical_sha256(packet)
            or normalized_packet_sha256
            != expected_receipt.get("normalized_packet_sha256")
            or import_key_sha256 != expected_receipt.get("import_key_sha256")
            or source_payload_sha256 != expected_receipt.get("source_payload_sha256")
            or source_payload_bytes != expected_receipt.get("source_payload_bytes")
            or row["source_channel"] != packet.get("source_channel")
            or row["source_key"] != packet.get("source_key")
            or external_run_id != packet.get("external_run_id")
        ):
            _raise(
                "SOAK_DB_INVENTORY_RECEIPT_INVALID",
                "source inbox receipt integrity is invalid",
            )
        if receipt_id in by_id:
            _raise(
                "SOAK_DB_INVENTORY_RECEIPT_INVALID",
                "receipt association is duplicated",
            )
        by_id[receipt_id] = (external_run_id, stored_receipt_sha256)
    if set(by_id) != set(receipt_ids):
        _raise(
            "SOAK_DB_INVENTORY_RECEIPT_MISSING",
            "a non-empty run receipt_id has no associated receipt",
        )
    result: dict[str, str] = {}
    for run_id, receipt_id in run_receipts.items():
        external_run_id, receipt_sha256 = by_id[receipt_id]
        if external_run_id != run_id:
            _raise(
                "SOAK_DB_INVENTORY_RECEIPT_MISMATCH",
                "run receipt_id is not bound to the same external_run_id",
            )
        result[run_id] = receipt_sha256
    return result


def _inventory_hash_basis(inventory: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": inventory["version"],
        "scan_order": inventory["scan_order"],
        "run_row_columns": inventory["run_row_columns"],
        "run_count": inventory["run_count"],
        "receipt_count": inventory["receipt_count"],
        "run_ids_sha256": inventory["run_ids_sha256"],
        "runs_sha256": inventory["runs_sha256"],
    }


def build_soak_db_inventory(
    database_path: str | os.PathLike[str],
    *,
    page_size: int = MAX_SOAK_DB_SCAN_PAGE_SIZE,
) -> dict[str, Any]:
    """Seal every source-monitoring run in one stable, read-only snapshot."""

    path = _path(database_path)
    safe_page_size = _page_size(page_size)
    entries: list[dict[str, Any]] = []
    scan_page_count = 0
    run_columns: list[str] = []
    try:
        with _read_only_snapshot(path) as connection:
            connection.execute("BEGIN")
            run_columns = _table_columns(
                connection,
                "source_adapter_runs",
                required=_REQUIRED_RUN_COLUMNS,
            )
            _table_columns(
                connection,
                "source_inbox_imports",
                required=_REQUIRED_RECEIPT_COLUMNS,
            )
            declared_run_count = connection.execute(
                "SELECT COUNT(*) AS run_count FROM source_adapter_runs"
            ).fetchone()["run_count"]
            if (
                type(declared_run_count) is not int
                or declared_run_count < 0
                or declared_run_count > MAX_SOAK_DB_INVENTORY_RUNS
            ):
                _raise(
                    "SOAK_DB_INVENTORY_RUN_LIMIT_EXCEEDED",
                    "source_adapter_runs exceeds the bounded inventory run limit",
                )
            last_run_id: str | None = None
            while True:
                if last_run_id is None:
                    rows = connection.execute(
                        """SELECT * FROM source_adapter_runs
                            ORDER BY run_id COLLATE BINARY ASC
                            LIMIT ?""",
                        (safe_page_size,),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """SELECT * FROM source_adapter_runs
                            WHERE run_id COLLATE BINARY > ?
                            ORDER BY run_id COLLATE BINARY ASC
                            LIMIT ?""",
                        (last_run_id, safe_page_size),
                    ).fetchall()
                if not rows:
                    break
                scan_page_count += 1
                page_rows: list[tuple[str, str, str, str]] = []
                run_receipts: dict[str, str] = {}
                for row in rows:
                    raw = _raw_row(row, run_columns)
                    run_id = _clean_token(raw.get("run_id"), field="run_id")
                    status = _clean_token(raw.get("status"), field="status")
                    if status not in _RUN_STATUSES:
                        _raise(
                            "SOAK_DB_INVENTORY_STATUS_INVALID",
                            "source_adapter_runs contains an unsupported status",
                        )
                    receipt_id = _clean_optional_token(
                        raw.get("receipt_id"),
                        field="receipt_id",
                    )
                    if entries and run_id.encode("utf-8") <= entries[-1]["run_id"].encode("utf-8"):
                        _raise(
                            "SOAK_DB_INVENTORY_KEYSET_INVALID",
                            "run_id keyset order is not strictly increasing",
                        )
                    row_sha256 = canonical_sha256(raw)
                    page_rows.append((run_id, status, receipt_id, row_sha256))
                    if receipt_id:
                        run_receipts[run_id] = receipt_id
                    # Keep cross-page ordering checks independent of receipt lookup.
                    entries.append({
                        "run_id": run_id,
                        "status": status,
                        "row_sha256": row_sha256,
                        "receipt_id": receipt_id,
                        "receipt_sha256": "",
                    })
                receipt_hashes = _receipt_hashes(connection, run_receipts)
                page_start = len(entries) - len(page_rows)
                for index, (run_id, _status, _receipt_id, _row_sha256) in enumerate(page_rows):
                    entries[page_start + index]["receipt_sha256"] = receipt_hashes.get(
                        run_id,
                        "",
                    )
                last_run_id = page_rows[-1][0]
    except SoakDbInventoryError:
        raise
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        raise SoakDbInventoryError(
            "SOAK_DB_INVENTORY_READ_FAILED",
            "the read-only source-monitoring snapshot could not be inventoried",
        ) from exc

    run_ids = [entry["run_id"] for entry in entries]
    inventory: dict[str, Any] = {
        "version": SOAK_DB_INVENTORY_VERSION,
        "scan_order": SOAK_DB_SCAN_ORDER,
        "scan_page_size": safe_page_size,
        "scan_page_count": scan_page_count,
        "run_row_columns": run_columns,
        "run_count": len(entries),
        "receipt_count": sum(1 for entry in entries if entry["receipt_id"]),
        "run_ids_sha256": canonical_sha256(run_ids),
        "runs_sha256": canonical_sha256(entries),
        "inventory_sha256": "",
        "runs": entries,
        "safety": dict(_SAFETY),
    }
    inventory["inventory_sha256"] = canonical_sha256(
        _inventory_hash_basis(inventory)
    )
    return inventory


def read_soak_db_run_evidence(
    database_path: str | os.PathLike[str],
    run_id: str,
) -> dict[str, Any] | None:
    """Read and seal one terminal run by primary key in a stable snapshot.

    Source-monitoring schema v1 does not persist a per-run ``config_version``;
    this projection deliberately omits it rather than joining the adapter's
    current state and misrepresenting that value as historical evidence.
    """

    path = _path(database_path)
    clean_run_id = _clean_token(run_id, field="run_id")
    if _RUN_ID_RE.fullmatch(clean_run_id) is None:
        _raise(
            "SOAK_DB_RUN_ID_INVALID",
            "run_id must be a canonical source-monitoring run identifier",
        )
    evidence: dict[str, Any] | None = None
    try:
        with _read_only_snapshot(path) as connection:
            connection.execute("BEGIN")
            run_columns = _table_columns(
                connection,
                "source_adapter_runs",
                required=_REQUIRED_TERMINAL_RUN_COLUMNS,
            )
            _table_columns(
                connection,
                "source_inbox_imports",
                required=_REQUIRED_RECEIPT_COLUMNS,
            )
            row = connection.execute(
                "SELECT * FROM source_adapter_runs WHERE run_id=?",
                (clean_run_id,),
            ).fetchone()
            if row is None:
                return None
            raw = _raw_row(row, run_columns)
            if raw.get("run_id") != clean_run_id:
                _raise(
                    "SOAK_DB_RUN_EVIDENCE_INVALID",
                    "the selected run row identity is inconsistent",
                )
            status = _clean_token(raw.get("status"), field="status")
            if status not in _RUN_STATUSES:
                _raise(
                    "SOAK_DB_INVENTORY_STATUS_INVALID",
                    "the selected run status is unsupported",
                )
            if status == "RUNNING":
                _raise(
                    "SOAK_DB_RUN_NOT_TERMINAL",
                    "the selected run has not reached a terminal status",
                )
            adapter_key = _clean_token(
                raw.get("adapter_key"),
                field="adapter_key",
                maximum=64,
            )
            if _ADAPTER_KEY_RE.fullmatch(adapter_key) is None:
                _raise(
                    "SOAK_DB_RUN_EVIDENCE_INVALID",
                    "the selected run adapter_key is not canonical",
                )
            counts = {
                field: _native_non_negative(raw.get(field), field=field)
                for field in (
                    "observed_count",
                    "accepted_count",
                    "duplicate_count",
                    "rejected_count",
                )
            }
            if (
                counts["accepted_count"]
                + counts["duplicate_count"]
                + counts["rejected_count"]
                > counts["observed_count"]
            ):
                _raise(
                    "SOAK_DB_RUN_EVIDENCE_INVALID",
                    "the selected run terminal counts are inconsistent",
                )
            error_code = _clean_optional_token(
                raw.get("error_code"),
                field="error_code",
                maximum=160,
            )
            if _ERROR_CODE_RE.fullmatch(error_code) is None:
                _raise(
                    "SOAK_DB_RUN_EVIDENCE_INVALID",
                    "the selected run error_code is not canonical",
                )
            receipt_id = _clean_optional_token(
                raw.get("receipt_id"),
                field="receipt_id",
            )
            receipt_hashes = _receipt_hashes(
                connection,
                ({clean_run_id: receipt_id} if receipt_id else {}),
            )
            evidence = {
                "version": SOAK_DB_RUN_EVIDENCE_VERSION,
                "run_id": clean_run_id,
                "adapter_key": adapter_key,
                "status": status,
                "row_sha256": canonical_sha256(raw),
                "receipt_id": receipt_id,
                "receipt_sha256": receipt_hashes.get(clean_run_id, ""),
                **counts,
                "error_code": error_code,
                "safety": dict(_SAFETY),
            }
    except SoakDbInventoryError:
        raise
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        raise SoakDbInventoryError(
            "SOAK_DB_INVENTORY_READ_FAILED",
            "the terminal run evidence could not be read",
        ) from exc
    return evidence


def _normalize_inventory(value: Any, *, label: str) -> dict[str, Any]:
    expected_fields = {
        "version",
        "scan_order",
        "scan_page_size",
        "scan_page_count",
        "run_row_columns",
        "run_count",
        "receipt_count",
        "run_ids_sha256",
        "runs_sha256",
        "inventory_sha256",
        "runs",
        "safety",
    }
    if type(value) is not dict or set(value) != expected_fields:
        _raise("SOAK_DB_INVENTORY_INVALID", f"{label} inventory fields are not closed")
    if value.get("version") != SOAK_DB_INVENTORY_VERSION:
        _raise("SOAK_DB_INVENTORY_INVALID", f"{label} inventory version is unsupported")
    if value.get("scan_order") != SOAK_DB_SCAN_ORDER:
        _raise("SOAK_DB_INVENTORY_INVALID", f"{label} scan order is unsupported")
    page_size = _page_size(value.get("scan_page_size"))
    page_count = _native_non_negative(value.get("scan_page_count"), field="scan_page_count")
    run_count = _native_non_negative(value.get("run_count"), field="run_count")
    if run_count > MAX_SOAK_DB_INVENTORY_RUNS:
        _raise(
            "SOAK_DB_INVENTORY_RUN_LIMIT_EXCEEDED",
            "inventory exceeds the bounded run limit",
        )
    receipt_count = _native_non_negative(
        value.get("receipt_count"),
        field="receipt_count",
    )
    columns = value.get("run_row_columns")
    if (
        type(columns) is not list
        or not columns
        or any(type(column) is not str or not column for column in columns)
        or len(columns) != len(set(columns))
        or not _REQUIRED_RUN_COLUMNS.issubset(columns)
    ):
        _raise("SOAK_DB_INVENTORY_INVALID", f"{label} run-row columns are invalid")
    runs_value = value.get("runs")
    if type(runs_value) is not list or len(runs_value) != run_count:
        _raise("SOAK_DB_INVENTORY_INVALID", f"{label} run count is inconsistent")
    runs: list[dict[str, str]] = []
    previous_key: bytes | None = None
    for index, entry in enumerate(runs_value):
        if type(entry) is not dict or set(entry) != {
            "run_id",
            "status",
            "row_sha256",
            "receipt_id",
            "receipt_sha256",
        }:
            _raise("SOAK_DB_INVENTORY_INVALID", f"{label} run entry is not closed")
        run_id = _clean_token(entry.get("run_id"), field=f"runs[{index}].run_id")
        encoded_id = run_id.encode("utf-8")
        if previous_key is not None and encoded_id <= previous_key:
            _raise("SOAK_DB_INVENTORY_INVALID", f"{label} run IDs are not ordered")
        previous_key = encoded_id
        status = _clean_token(entry.get("status"), field=f"runs[{index}].status")
        if status not in _RUN_STATUSES:
            _raise("SOAK_DB_INVENTORY_INVALID", f"{label} run status is invalid")
        row_sha256 = _sha256(
            entry.get("row_sha256"),
            field=f"runs[{index}].row_sha256",
        )
        receipt_id = _clean_optional_token(
            entry.get("receipt_id"),
            field=f"runs[{index}].receipt_id",
        )
        receipt_sha256 = _sha256(
            entry.get("receipt_sha256"),
            field=f"runs[{index}].receipt_sha256",
            allow_empty=True,
        )
        if bool(receipt_id) is not bool(receipt_sha256):
            _raise("SOAK_DB_INVENTORY_INVALID", f"{label} receipt binding is incomplete")
        runs.append({
            "run_id": run_id,
            "status": status,
            "row_sha256": row_sha256,
            "receipt_id": receipt_id,
            "receipt_sha256": receipt_sha256,
        })
    expected_pages = (run_count + page_size - 1) // page_size
    if page_count != expected_pages:
        _raise("SOAK_DB_INVENTORY_INVALID", f"{label} page count is inconsistent")
    if receipt_count != sum(1 for entry in runs if entry["receipt_id"]):
        _raise("SOAK_DB_INVENTORY_INVALID", f"{label} receipt count is inconsistent")
    if value.get("safety") != _SAFETY:
        _raise("SOAK_DB_INVENTORY_INVALID", f"{label} safety boundary is invalid")
    run_ids = [entry["run_id"] for entry in runs]
    normalized = {
        "version": SOAK_DB_INVENTORY_VERSION,
        "scan_order": SOAK_DB_SCAN_ORDER,
        "scan_page_size": page_size,
        "scan_page_count": page_count,
        "run_row_columns": list(columns),
        "run_count": run_count,
        "receipt_count": receipt_count,
        "run_ids_sha256": _sha256(value.get("run_ids_sha256"), field="run_ids_sha256"),
        "runs_sha256": _sha256(value.get("runs_sha256"), field="runs_sha256"),
        "inventory_sha256": _sha256(
            value.get("inventory_sha256"),
            field="inventory_sha256",
        ),
        "runs": runs,
        "safety": dict(_SAFETY),
    }
    if normalized["run_ids_sha256"] != canonical_sha256(run_ids):
        _raise("SOAK_DB_INVENTORY_SEAL_INVALID", f"{label} run-id seal is invalid")
    if normalized["runs_sha256"] != canonical_sha256(runs):
        _raise("SOAK_DB_INVENTORY_SEAL_INVALID", f"{label} run seal is invalid")
    if normalized["inventory_sha256"] != canonical_sha256(
        _inventory_hash_basis(normalized)
    ):
        _raise("SOAK_DB_INVENTORY_SEAL_INVALID", f"{label} inventory seal is invalid")
    return normalized


def write_soak_db_inventory_exclusive(
    inventory: Any,
    artifact_path: str | os.PathLike[str],
) -> dict[str, Any]:
    """Create one canonical inventory artifact durably without overwriting."""

    normalized = _normalize_inventory(inventory, label="write")
    payload = canonical_json(normalized).encode("utf-8")
    if not payload or len(payload) > MAX_SOAK_DB_INVENTORY_BYTES:
        _raise(
            "SOAK_DB_INVENTORY_ARTIFACT_TOO_LARGE",
            "inventory artifact exceeds the bounded file size",
        )
    path = _new_artifact_path(artifact_path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= int(getattr(os, "O_BINARY", 0) or 0)
    flags |= int(getattr(os, "O_NOFOLLOW", 0) or 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags, 0o600)
        opened = os.fstat(descriptor)
        _require_independent_regular(
            opened,
            code="SOAK_DB_INVENTORY_ARTIFACT_PATH_INVALID",
        )
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if type(count) is not int or count <= 0:
                raise OSError("inventory artifact write made no progress")
            written += count
        os.fsync(descriptor)
        final_fd = os.fstat(descriptor)
        final_path = path.lstat()
        _require_independent_regular(
            final_fd,
            code="SOAK_DB_INVENTORY_ARTIFACT_PATH_INVALID",
        )
        _require_independent_regular(
            final_path,
            code="SOAK_DB_INVENTORY_ARTIFACT_PATH_INVALID",
        )
        if (
            first_reparse_component(path) is not None
            or (final_fd.st_dev, final_fd.st_ino)
            != (final_path.st_dev, final_path.st_ino)
            or final_fd.st_size != len(payload)
            or final_path.st_size != len(payload)
        ):
            _raise(
                "SOAK_DB_INVENTORY_ARTIFACT_IDENTITY_CHANGED",
                "inventory artifact identity changed while writing",
            )
        _fsync_parent_directory(path)
    except FileExistsError as exc:
        raise SoakDbInventoryError(
            "SOAK_DB_INVENTORY_ARTIFACT_EXISTS",
            "inventory artifact already exists and will not be overwritten",
        ) from exc
    except SoakDbInventoryError:
        raise
    except OSError as exc:
        raise SoakDbInventoryError(
            "SOAK_DB_INVENTORY_ARTIFACT_WRITE_FAILED",
            "inventory artifact could not be written durably",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return {
        "version": SOAK_DB_INVENTORY_WRITE_VERSION,
        "inventory_sha256": normalized["inventory_sha256"],
        "bytes_written": len(payload),
        "safety": dict(_SAFETY),
    }


def _decode_canonical_inventory(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_SOAK_DB_INVENTORY_BYTES:
        _raise(
            "SOAK_DB_INVENTORY_ARTIFACT_TOO_LARGE",
            "inventory artifact is empty or exceeds the bounded file size",
        )

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite constant: {value}")

    try:
        text = raw.decode("utf-8", errors="strict")
        decoded = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise SoakDbInventoryError(
            "SOAK_DB_INVENTORY_ARTIFACT_FORMAT_INVALID",
            "inventory artifact is not unique canonical JSON",
        ) from exc
    if type(decoded) is not dict:
        _raise(
            "SOAK_DB_INVENTORY_ARTIFACT_FORMAT_INVALID",
            "inventory artifact root must be an object",
        )
    try:
        canonical = canonical_json(decoded).encode("utf-8")
    except Exception as exc:
        raise SoakDbInventoryError(
            "SOAK_DB_INVENTORY_ARTIFACT_FORMAT_INVALID",
            "inventory artifact cannot be canonicalized",
        ) from exc
    if raw != canonical:
        _raise(
            "SOAK_DB_INVENTORY_ARTIFACT_FORMAT_INVALID",
            "inventory artifact bytes are not canonical",
        )
    return decoded


def load_soak_db_inventory(
    artifact_path: str | os.PathLike[str],
) -> dict[str, Any]:
    """Load and strictly revalidate one independent canonical artifact."""

    path = _existing_artifact_path(artifact_path)
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0) or 0)
    descriptor = -1
    try:
        before_path = path.lstat()
        descriptor = os.open(path, flags)
        before_fd = os.fstat(descriptor)
        _require_independent_regular(
            before_path,
            code="SOAK_DB_INVENTORY_ARTIFACT_PATH_INVALID",
        )
        _require_independent_regular(
            before_fd,
            code="SOAK_DB_INVENTORY_ARTIFACT_PATH_INVALID",
        )
        if (
            (before_fd.st_dev, before_fd.st_ino)
            != (before_path.st_dev, before_path.st_ino)
            or before_fd.st_size > MAX_SOAK_DB_INVENTORY_BYTES
        ):
            _raise(
                "SOAK_DB_INVENTORY_ARTIFACT_IDENTITY_CHANGED",
                "inventory artifact identity changed before reading",
            )
        chunks: list[bytes] = []
        remaining = MAX_SOAK_DB_INVENTORY_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after_fd = os.fstat(descriptor)
        after_path = path.lstat()
        _require_independent_regular(
            after_fd,
            code="SOAK_DB_INVENTORY_ARTIFACT_PATH_INVALID",
        )
        _require_independent_regular(
            after_path,
            code="SOAK_DB_INVENTORY_ARTIFACT_PATH_INVALID",
        )
        if (
            first_reparse_component(path) is not None
            or (after_fd.st_dev, after_fd.st_ino)
            != (after_path.st_dev, after_path.st_ino)
            or (
                before_fd.st_dev,
                before_fd.st_ino,
                before_fd.st_size,
                before_fd.st_mtime_ns,
            )
            != (
                after_fd.st_dev,
                after_fd.st_ino,
                after_fd.st_size,
                after_fd.st_mtime_ns,
            )
            or len(raw) != after_fd.st_size
        ):
            _raise(
                "SOAK_DB_INVENTORY_ARTIFACT_IDENTITY_CHANGED",
                "inventory artifact identity or size changed while reading",
            )
    except SoakDbInventoryError:
        raise
    except OSError as exc:
        raise SoakDbInventoryError(
            "SOAK_DB_INVENTORY_ARTIFACT_READ_FAILED",
            "inventory artifact could not be read",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return _normalize_inventory(
        _decode_canonical_inventory(raw),
        label="loaded",
    )


def _normalize_run_ids(value: Any) -> list[str]:
    if type(value) not in {list, tuple} or len(value) > MAX_SOAK_SESSION_RUN_IDS:
        _raise(
            "SOAK_DB_SESSION_DECLARATION_INVALID",
            "session_terminal_run_ids must be a bounded native list or tuple",
        )
    result = [
        _clean_token(item, field=f"session_terminal_run_ids[{index}]")
        for index, item in enumerate(value)
    ]
    if len(result) != len(set(result)):
        _raise(
            "SOAK_DB_SESSION_DECLARATION_INVALID",
            "session_terminal_run_ids contains duplicates",
        )
    return sorted(result, key=lambda item: item.encode("utf-8"))


def _normalize_declarations(
    value: Any,
) -> tuple[dict[str, dict[str, Any]], int]:
    if type(value) not in {list, tuple} or len(value) > MAX_SOAK_SESSION_RUN_IDS:
        _raise(
            "SOAK_DB_SESSION_DECLARATION_INVALID",
            "session_run_declarations must be a bounded native list or tuple",
        )
    result: dict[str, dict[str, Any]] = {}
    unrecorded_count = 0
    for index, item in enumerate(value):
        if type(item) is not dict or set(item) != {
            "run_id",
            "status",
            "state_recorded",
            "run_record_sha256",
            "import_receipt_sha256",
        }:
            _raise(
                "SOAK_DB_SESSION_DECLARATION_INVALID",
                "session run declaration fields are not closed",
            )
        status = _clean_token(item.get("status"), field=f"declarations[{index}].status")
        state_recorded = item.get("state_recorded")
        if status not in _DECLARED_RUN_STATUSES or type(state_recorded) is not bool:
            _raise(
                "SOAK_DB_SESSION_DECLARATION_INVALID",
                "session run declaration status or state_recorded is invalid",
            )
        raw_run_id = item.get("run_id")
        run_record_sha256 = item.get("run_record_sha256")
        import_receipt_sha256 = item.get("import_receipt_sha256")
        if state_recorded is False:
            if (
                raw_run_id != ""
                or run_record_sha256 != ""
                or import_receipt_sha256 != ""
            ):
                _raise(
                    "SOAK_DB_SESSION_DECLARATION_INVALID",
                    "an unrecorded session result cannot claim persisted evidence",
                )
            unrecorded_count += 1
            continue
        run_id = _clean_token(
            raw_run_id,
            field=f"declarations[{index}].run_id",
        )
        row_sha256 = _sha256(
            run_record_sha256,
            field=f"declarations[{index}].run_record_sha256",
        )
        receipt_sha256 = _sha256(
            import_receipt_sha256,
            field=f"declarations[{index}].import_receipt_sha256",
            allow_empty=True,
        )
        if run_id in result:
            _raise(
                "SOAK_DB_SESSION_DECLARATION_INVALID",
                "session run declarations contain duplicate run IDs",
            )
        result[run_id] = {
            "run_id": run_id,
            "status": status,
            "state_recorded": state_recorded,
            "run_record_sha256": row_sha256,
            "import_receipt_sha256": receipt_sha256,
        }
    return result, unrecorded_count


def validate_soak_db_inventory_delta(
    baseline: Any,
    final: Any,
    *,
    session_terminal_run_ids: Any,
    session_run_declarations: Any,
) -> dict[str, Any]:
    """Compare two sealed inventories and return a bounded soak verdict."""

    before = _normalize_inventory(baseline, label="baseline")
    after = _normalize_inventory(final, label="final")
    terminal_ids = _normalize_run_ids(session_terminal_run_ids)
    declarations, unrecorded_declaration_count = _normalize_declarations(
        session_run_declarations
    )
    before_by_id = {entry["run_id"]: entry for entry in before["runs"]}
    after_by_id = {entry["run_id"]: entry for entry in after["runs"]}
    before_ids = set(before_by_id)
    after_ids = set(after_by_id)
    terminal_set = set(terminal_ids)
    declaration_ids = set(declarations)
    added = after_ids - before_ids
    removed = before_ids - after_ids
    modified = {
        run_id
        for run_id in before_ids & after_ids
        if before_by_id[run_id] != after_by_id[run_id]
    }
    baseline_running = {
        run_id
        for run_id, entry in before_by_id.items()
        if entry["status"] == "RUNNING"
    }
    terminal_declaration_missing = terminal_set - declaration_ids
    declaration_not_terminal = declaration_ids - terminal_set
    session_missing = terminal_set - added
    session_extra = added - terminal_set
    status_mismatches = {
        run_id
        for run_id in terminal_set & declaration_ids & after_ids
        if declarations[run_id]["status"] != after_by_id[run_id]["status"]
    }
    row_hash_mismatches = {
        run_id
        for run_id in terminal_set & declaration_ids & after_ids
        if declarations[run_id]["run_record_sha256"]
        != after_by_id[run_id]["row_sha256"]
    }
    receipt_hash_mismatches = {
        run_id
        for run_id in terminal_set & declaration_ids & after_ids
        if declarations[run_id]["import_receipt_sha256"]
        != after_by_id[run_id]["receipt_sha256"]
    }
    inspected_session_ids = terminal_set | added
    running = {
        run_id
        for run_id in inspected_session_ids & after_ids
        if after_by_id[run_id]["status"] == "RUNNING"
    }
    abandoned = {
        run_id
        for run_id in inspected_session_ids & after_ids
        if after_by_id[run_id]["status"] == "ABANDONED"
    }
    issues: list[dict[str, str]] = []
    issue_count = 0

    def add_issue(code: str, run_id: str = "") -> None:
        nonlocal issue_count
        issue_count += 1
        if len(issues) < MAX_SOAK_DB_VERDICT_ISSUES:
            issues.append({"code": code, "run_id": run_id})

    if before["run_row_columns"] != after["run_row_columns"]:
        add_issue("SOAK_DB_RUN_SCHEMA_CHANGED")
    categories = (
        ("SOAK_DB_BASELINE_RUN_DELETED", removed),
        ("SOAK_DB_BASELINE_RUN_MODIFIED", modified),
        ("SOAK_DB_BASELINE_RUN_RUNNING", baseline_running),
        ("SOAK_DB_TERMINAL_DECLARATION_MISSING", terminal_declaration_missing),
        ("SOAK_DB_DECLARATION_NOT_TERMINAL", declaration_not_terminal),
        ("SOAK_DB_SESSION_RUN_MISSING", session_missing),
        ("SOAK_DB_SESSION_RUN_EXTRA", session_extra),
        ("SOAK_DB_SESSION_STATUS_MISMATCH", status_mismatches),
        ("SOAK_DB_SESSION_RUN_HASH_MISMATCH", row_hash_mismatches),
        ("SOAK_DB_SESSION_RECEIPT_HASH_MISMATCH", receipt_hash_mismatches),
        ("SOAK_DB_SESSION_RUN_RUNNING", running),
        ("SOAK_DB_SESSION_RUN_ABANDONED", abandoned),
    )
    for code, run_ids in categories:
        for run_id in sorted(run_ids, key=lambda item: item.encode("utf-8")):
            add_issue(code, run_id)
    for _index in range(unrecorded_declaration_count):
        add_issue("SOAK_DB_SESSION_STATE_NOT_RECORDED")

    counts = {
        "baseline_run_count": before["run_count"],
        "final_run_count": after["run_count"],
        "added_run_count": len(added),
        "removed_run_count": len(removed),
        "modified_baseline_run_count": len(modified),
        "baseline_running_count": len(baseline_running),
        "session_terminal_run_count": len(terminal_set),
        "session_declaration_count": (
            len(declaration_ids) + unrecorded_declaration_count
        ),
        "terminal_declaration_missing_count": len(terminal_declaration_missing),
        "declaration_not_terminal_count": len(declaration_not_terminal),
        "session_missing_count": len(session_missing),
        "session_extra_count": len(session_extra),
        "session_status_mismatch_count": len(status_mismatches),
        "session_run_hash_mismatch_count": len(row_hash_mismatches),
        "session_receipt_hash_mismatch_count": len(receipt_hash_mismatches),
        "session_running_count": len(running),
        "session_abandoned_count": len(abandoned),
        "session_state_not_recorded_count": unrecorded_declaration_count,
    }
    set_hashes = {
        "added_run_ids_sha256": canonical_sha256(
            sorted(added, key=lambda item: item.encode("utf-8"))
        ),
        "removed_run_ids_sha256": canonical_sha256(
            sorted(removed, key=lambda item: item.encode("utf-8"))
        ),
        "modified_run_ids_sha256": canonical_sha256(
            sorted(modified, key=lambda item: item.encode("utf-8"))
        ),
        "session_terminal_run_ids_sha256": canonical_sha256(terminal_ids),
    }
    verdict: dict[str, Any] = {
        "version": SOAK_DB_INVENTORY_VERDICT_VERSION,
        "verdict": "PASS" if issue_count == 0 else "FAIL",
        "baseline_inventory_sha256": before["inventory_sha256"],
        "final_inventory_sha256": after["inventory_sha256"],
        "counts": counts,
        "set_hashes": set_hashes,
        "issue_count": issue_count,
        "issues": issues,
        "issues_truncated": issue_count > len(issues),
        "verdict_sha256": "",
        "safety": dict(_SAFETY),
    }
    verdict["verdict_sha256"] = canonical_sha256({
        key: value for key, value in verdict.items() if key != "verdict_sha256"
    })
    return verdict


__all__ = [
    "MAX_SOAK_DB_INVENTORY_BYTES",
    "MAX_SOAK_DB_INVENTORY_RUNS",
    "MAX_SOAK_DB_MAIN_FILE_BYTES",
    "MAX_SOAK_DB_RECEIPT_JSON_BYTES",
    "MAX_SOAK_DB_SCAN_PAGE_SIZE",
    "MAX_SOAK_DB_SHM_FILE_BYTES",
    "MAX_SOAK_DB_VERDICT_ISSUES",
    "MAX_SOAK_DB_WAL_FILE_BYTES",
    "SOAK_DB_INVENTORY_VERDICT_VERSION",
    "SOAK_DB_INVENTORY_VERSION",
    "SOAK_DB_INVENTORY_WRITE_VERSION",
    "SOAK_DB_RUN_EVIDENCE_VERSION",
    "SoakDbInventoryError",
    "build_soak_db_inventory",
    "load_soak_db_inventory",
    "read_soak_db_run_evidence",
    "validate_soak_db_inventory_delta",
    "write_soak_db_inventory_exclusive",
]
