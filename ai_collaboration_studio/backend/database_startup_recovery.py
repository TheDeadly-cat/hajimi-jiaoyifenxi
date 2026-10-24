"""Recover ordinary crashed WAL sessions without migrating or deleting data.

The normal migration preflight stays read-only and unchanged. Recovery is an
explicit separate stage, under the server's database ownership lock. Raw source
files are preserved before SQLite is allowed to open the source. A failed apply
leaves an adjacent pending marker so the next startup cannot silently retry it.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import time
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .database_migration import _logical_snapshot
from .database_migration_commit import (
    _copy_locked_reader,
    _windows_exclusive_reader,
    publish_bytes_exclusive_durable,
    require_no_sqlite_sidecars,
    require_unaliased_regular_file,
    scan_active_migration_operations,
)
from .instance_ownership import DatabaseInstanceOwner
from .path_identity import first_reparse_component


class StartupRecoveryError(RuntimeError):
    """Recovery was refused or failed; retained evidence must not be discarded."""


_SUFFIXES = ("", "-wal", "-shm", "-journal")
_VERSION = "database_startup_recovery_v1"


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    publish_bytes_exclusive_durable(
        path, (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    )


def _file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _pending_path(path: Path) -> Path:
    return path.with_name(path.name + ".startup-recovery.pending.json")


def assert_no_pending_startup_recovery(database_path: str | Path) -> None:
    path = Path(database_path).absolute()
    if os.path.lexists(_pending_path(path)):
        raise StartupRecoveryError("An unfinished startup recovery requires inspection; automatic retry refused")


def _source(database_path: str | Path, owner: DatabaseInstanceOwner) -> Path:
    requested = Path(database_path).expanduser().absolute()
    require_unaliased_regular_file(requested, label="Recovery database")
    owner.assert_held_for(requested)
    assert_no_pending_startup_recovery(requested)
    if scan_active_migration_operations(requested):
        raise StartupRecoveryError("Pending database migration must be reconciled separately")
    if os.path.lexists(Path(str(requested) + "-journal")):
        raise StartupRecoveryError("Rollback journal recovery is not authorized by ordinary WAL recovery")
    return requested.resolve()


def _family(path: Path) -> dict[str, Path]:
    return {suffix: Path(str(path) + suffix) for suffix in _SUFFIXES
            if os.path.lexists(Path(str(path) + suffix))}


def _identity(path: Path, stream: Any) -> dict[str, Any]:
    identity = require_unaliased_regular_file(path, label="Recovery file")
    opened = os.fstat(stream.fileno())
    if (identity["device_id"], identity["file_id"]) != (int(opened.st_dev), int(opened.st_ino)):
        raise StartupRecoveryError("Recovery file changed while opening")
    stream.seek(0)
    digest = hashlib.file_digest(stream, "sha256").hexdigest()
    stream.seek(0)
    return {"size": int(opened.st_size), "sha256": digest,
            "device_id": int(opened.st_dev), "file_id": int(opened.st_ino)}


def _capture_family(path: Path, destination: Path | None = None) -> dict[str, Any]:
    """Keep all source handles share-zero until every copy and hash is verified."""
    files = _family(path)
    if "" not in files or "-journal" in files:
        raise StartupRecoveryError("Recovery requires an existing database without rollback journal")
    with ExitStack() as stack:
        handles = {}
        for suffix, member in files.items():
            require_unaliased_regular_file(member, label="Recovery file")
            handles[suffix] = stack.enter_context(_windows_exclusive_reader(member))
        if _family(path) != files:
            raise StartupRecoveryError("SQLite file family changed during exclusive capture")
        before = {suffix: _identity(files[suffix], stream) for suffix, stream in handles.items()}
        if destination is not None:
            for suffix, stream in handles.items():
                target = destination / (path.name + suffix)
                with target.open("xb") as output:
                    size, digest = _copy_locked_reader(stream, output)
                if size != before[suffix]["size"] or digest != before[suffix]["sha256"]:
                    raise StartupRecoveryError("Recovery backup copy differs from source")
                if _file_digest(target) != digest:
                    raise StartupRecoveryError("Recovery backup readback differs from source")
        after = {suffix: _identity(files[suffix], stream) for suffix, stream in handles.items()}
        if before != after or _family(path) != files:
            raise StartupRecoveryError("SQLite file family changed during exclusive capture")
        return before


def _wal_checksum(data: bytes, state: tuple[int, int], order: str) -> tuple[int, int]:
    if len(data) % 8:
        raise StartupRecoveryError("WAL checksum input is incomplete")
    first, second = state
    for offset in range(0, len(data), 8):
        left, right = struct.unpack_from(order + "II", data, offset)
        first = (first + left + second) & 0xFFFFFFFF
        second = (second + right + first) & 0xFFFFFFFF
    return first, second


def _validate_wal(path: Path) -> None:
    """Reject damaged/truncated WAL instead of allowing silent SQLite fallback."""
    wal = Path(str(path) + "-wal")
    if not wal.exists() or wal.stat().st_size == 0:
        return
    size = wal.stat().st_size
    if size < 32:
        raise StartupRecoveryError("WAL header is incomplete")
    with wal.open("rb") as stream:
        header = stream.read(32)
        magic, version, page_size = struct.unpack_from(">III", header)
        if magic not in (0x377F0682, 0x377F0683) or version != 3007000:
            raise StartupRecoveryError("WAL header is invalid")
        if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
            raise StartupRecoveryError("WAL page size is invalid")
        if (size - 32) % (24 + page_size):
            raise StartupRecoveryError("WAL has an incomplete frame; manual inspection required")
        order = "<" if magic == 0x377F0682 else ">"
        state = _wal_checksum(header[:24], (0, 0), order)
        if state != struct.unpack_from(">II", header, 24):
            raise StartupRecoveryError("WAL header checksum is invalid")
        salt = header[16:24]
        for _ in range((size - 32) // (24 + page_size)):
            frame = stream.read(24 + page_size)
            if len(frame) != 24 + page_size:
                raise StartupRecoveryError("WAL frame changed during validation")
            if frame[8:16] != salt or struct.unpack_from(">I", frame)[0] == 0:
                raise StartupRecoveryError("WAL frame identity is invalid")
            state = _wal_checksum(frame[:8] + frame[24:], state, order)
            if state != struct.unpack_from(">II", frame, 16):
                raise StartupRecoveryError("WAL frame checksum is invalid")
        if stream.read(1):
            raise StartupRecoveryError("WAL changed during validation")


def _logical(connection: sqlite3.Connection) -> dict[str, Any]:
    if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
        raise StartupRecoveryError("Recovered database integrity check failed")
    if list(connection.execute("PRAGMA foreign_key_check")):
        raise StartupRecoveryError("Recovered database has foreign-key violations")
    return _logical_snapshot(connection)


def _sqlite_recover(path: Path, expected: dict[str, Any] | None = None) -> dict[str, Any]:
    uri = "file:" + quote(path.as_posix(), safe="/:") + "?mode=rw"
    with closing(sqlite3.connect(uri, uri=True, timeout=2, isolation_level=None)) as connection:
        connection.execute("PRAGMA busy_timeout=2000")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA locking_mode=EXCLUSIVE")
        # Acquire SQLite ownership before checking the recovered logical view.
        connection.execute("BEGIN EXCLUSIVE")
        before = _logical(connection)
        if expected is not None and before != expected:
            connection.execute("ROLLBACK")
            raise StartupRecoveryError("Source logical contents differ from the verified recovery copy")
        connection.execute("COMMIT")
        checkpoint = tuple(connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
        if checkpoint not in ((0, 0, 0), (0, -1, -1)):
            raise StartupRecoveryError("SQLite checkpoint was busy or incomplete")
        if _logical(connection) != before:
            raise StartupRecoveryError("Database logical contents changed during checkpoint")
    require_no_sqlite_sidecars(path)
    return before


def _closed_logical(path: Path) -> dict[str, Any]:
    require_unaliased_regular_file(path, label="Recovered database")
    require_no_sqlite_sidecars(path)
    uri = "file:" + quote(path.as_posix(), safe="/:") + "?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        return _logical(connection)


def prepare_startup_recovery(database_path: str | Path, recovery_directory: str | Path,
                             *, instance_owner: DatabaseInstanceOwner) -> dict[str, Any]:
    source = _source(database_path, instance_owner)
    if not any(suffix in _family(source) for suffix in ("-wal", "-shm")):
        raise StartupRecoveryError("No ordinary WAL/SHM recovery is needed")
    directory = Path(recovery_directory).expanduser().absolute()
    if first_reparse_component(directory) is not None or os.path.lexists(directory):
        raise StartupRecoveryError("Recovery directory must be a fresh unaliased path")
    directory.mkdir(parents=True, exist_ok=False)
    originals = directory / "original-files"
    verification = directory / "verification"
    originals.mkdir()
    verification.mkdir()
    before = _capture_family(source, originals)
    backup = originals / source.name
    _validate_wal(backup)
    for suffix in before:
        with (originals / (source.name + suffix)).open("rb") as input_file:
            with (verification / (source.name + suffix)).open("xb") as output_file:
                _copy_locked_reader(input_file, output_file)
    verified = verification / source.name
    logical = _sqlite_recover(verified)
    if _closed_logical(verified) != logical or _capture_family(source) != before:
        raise StartupRecoveryError("Source or recovery-copy identity drifted during preparation")
    payload = {"version": _VERSION, "database_path": str(source),
               "recovery_directory": str(directory.resolve()), "source_files": before,
               "recovered_logical": logical, "prepared_at_epoch_ms": int(time.time() * 1000)}
    plan_sha256 = _digest(payload)
    prepared = {**payload, "plan_sha256": plan_sha256,
                "authorization_token": "startup-recovery:" + plan_sha256}
    _write_json(directory / "prepared.json", prepared)
    return prepared


def apply_startup_recovery(database_path: str | Path, prepared_path: str | Path, *,
                           authorization_token: str, instance_owner: DatabaseInstanceOwner) -> dict[str, Any]:
    source = _source(database_path, instance_owner)
    plan_path = Path(prepared_path).expanduser().absolute()
    require_unaliased_regular_file(plan_path, label="Recovery plan")
    prepared = json.loads(plan_path.read_text(encoding="utf-8"))
    expected_keys = {"version", "database_path", "recovery_directory", "source_files",
                     "recovered_logical", "prepared_at_epoch_ms", "plan_sha256", "authorization_token"}
    if not isinstance(prepared, dict) or set(prepared) != expected_keys:
        raise StartupRecoveryError("Recovery plan has an unsupported schema")
    payload = {key: value for key, value in prepared.items() if key not in {"plan_sha256", "authorization_token"}}
    digest = _digest(payload)
    if (prepared["version"] != _VERSION or prepared["plan_sha256"] != digest
            or prepared["authorization_token"] != "startup-recovery:" + digest
            or authorization_token != prepared["authorization_token"]):
        raise StartupRecoveryError("Recovery authorization does not match the exact prepared plan")
    directory = plan_path.parent
    if (Path(prepared["database_path"]) != source or plan_path.name != "prepared.json"
            or Path(prepared["recovery_directory"]) != directory.resolve()):
        raise StartupRecoveryError("Recovery plan path binding is invalid")
    receipt_path = directory / "receipt.json"
    if os.path.lexists(receipt_path):
        raise StartupRecoveryError("Recovery receipt already exists; repeated apply refused")
    backup = directory / "original-files" / source.name
    original_files = _capture_family(backup)
    if set(original_files) != set(prepared["source_files"]):
        raise StartupRecoveryError("Backup file family changed")
    for suffix, identity in original_files.items():
        original = prepared["source_files"][suffix]
        if (identity["size"], identity["sha256"]) != (original["size"], original["sha256"]):
            raise StartupRecoveryError("Original recovery backup changed")
    _validate_wal(backup)
    logical = _closed_logical(directory / "verification" / source.name)
    if logical != prepared["recovered_logical"]:
        raise StartupRecoveryError("Verified recovery copy changed")
    if _capture_family(source) != prepared["source_files"]:
        raise StartupRecoveryError("Source file family changed after preparation")
    instance_owner.assert_held_for(source)
    pending = _pending_path(source)
    _write_json(pending, {"version": _VERSION, "plan_sha256": digest,
                         "prepared_path": str(plan_path), "receipt_path": str(receipt_path)})
    # After this point even an error may have changed SQLite's own recovery
    # files. Retain the pending marker and originals; never retry automatically.
    _sqlite_recover(source, logical)
    if _closed_logical(source) != logical:
        raise StartupRecoveryError("Recovered source differs from verified copy")
    receipt = {"version": _VERSION, "status": "recovered", "database_path": str(source),
               "plan_sha256": digest, "backup_directory": str(directory / "original-files"),
               "logical_sha256": logical["logical_sha256"], "integrity_check": "ok",
               "foreign_key_violation_count": 0, "sidecars_absent": True,
               "schema_migrated": False, "finished_at_epoch_ms": int(time.time() * 1000),
               "source_after": _capture_family(source)}
    _write_json(receipt_path, receipt)
    pending.unlink()  # This is our recovery marker, never an SQLite sidecar.
    return receipt


def recover_startup_database(database_path: str | Path, recovery_directory: str | Path,
                             *, instance_owner: DatabaseInstanceOwner) -> dict[str, Any]:
    """Startup integration for a caller authorized to recover ordinary WAL."""
    prepared = prepare_startup_recovery(database_path, recovery_directory, instance_owner=instance_owner)
    return apply_startup_recovery(database_path, Path(recovery_directory) / "prepared.json",
                                 authorization_token=prepared["authorization_token"], instance_owner=instance_owner)
