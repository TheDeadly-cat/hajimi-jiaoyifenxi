from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import re
import stat as statlib
import tempfile
import time
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Mapping

from .path_identity import first_reparse_component


MIGRATION_INTENT_MARKER_VERSION = "database_migration_intent_marker_v1"
TERMINAL_MIGRATION_EVENTS = frozenset({"complete", "rolled_back", "aborted"})
_GATE_ONLY_MIGRATION_EVENTS = frozenset(
    {
        "verified",
        "recovery_verified",
        "receipt_committed",
        "complete",
        "rollback_started",
        "rollback_receipt_committed",
        "rolled_back",
        "abort_verified",
        "aborted",
    }
)
_MIGRATION_GATE_CAPABILITY = object()
_OPERATION_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_EVENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_SQLITE_PENDING_BYTE = 0x40000000
_SQLITE_LOCK_RANGE_SIZE = 512


class DatabaseMigrationCommitError(RuntimeError):
    """A commit primitive failed without discarding recovery artifacts."""


class SQLiteSidecarPresent(DatabaseMigrationCommitError):
    """A SQLite sidecar makes a raw copy or file replacement unsafe."""


class MigrationIntentJournalError(DatabaseMigrationCommitError):
    """An append-only migration marker chain is missing or invalid."""


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


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_identity(path: Path) -> dict[str, Any]:
    requested_path = _resolve_unaliased_commit_path(
        path,
        label="Migration file",
    )
    try:
        metadata = requested_path.lstat()
    except OSError as exc:
        raise DatabaseMigrationCommitError(
            f"Cannot inspect file identity: {requested_path}"
        ) from exc
    reparse_flag = int(getattr(statlib, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
    if requested_path.is_symlink() or bool(
        reparse_flag and int(getattr(metadata, "st_file_attributes", 0) or 0) & reparse_flag
    ):
        raise DatabaseMigrationCommitError(
            f"Migration file may not be a symlink or reparse point: {requested_path}"
        )
    clean_path = requested_path
    if not clean_path.is_file():
        raise DatabaseMigrationCommitError(f"File does not exist: {clean_path}")
    stat = clean_path.stat()
    return {
        "path": str(clean_path),
        "size": int(stat.st_size),
        "sha256": _file_sha256(clean_path),
    }


def require_unaliased_regular_file(
    path: str | Path,
    *,
    label: str = "Migration file",
) -> dict[str, Any]:
    """Return one physical file identity and reject hard-link aliases.

    Path inequality is not a sufficient migration boundary on Windows: two
    database names can address the same NTFS file while using different owner
    locks.  Every SQLite image handled by the formal gate must therefore have
    exactly one link and a stable device/file-id pair.
    """

    requested_path = Path(path).expanduser()
    offending_component = first_reparse_component(requested_path)
    if offending_component is not None:
        raise DatabaseMigrationCommitError(
            f"{label} may not contain a symlink or reparse point: "
            f"{offending_component}"
        )
    try:
        metadata = requested_path.lstat()
    except OSError as exc:
        raise DatabaseMigrationCommitError(
            f"Cannot inspect {label} identity: {requested_path}: {exc}"
        ) from exc
    reparse_flag = int(getattr(statlib, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
    if requested_path.is_symlink() or bool(
        reparse_flag and int(getattr(metadata, "st_file_attributes", 0) or 0) & reparse_flag
    ):
        raise DatabaseMigrationCommitError(
            f"{label} may not be a symlink or reparse point: {requested_path}"
        )
    clean_path = requested_path.resolve()
    try:
        stat = clean_path.stat()
    except OSError as exc:
        raise DatabaseMigrationCommitError(
            f"Cannot inspect {label} identity: {clean_path}: {exc}"
        ) from exc
    if not clean_path.is_file():
        raise DatabaseMigrationCommitError(
            f"{label} is not a regular file: {clean_path}"
        )
    link_count = int(stat.st_nlink)
    if link_count != 1:
        raise DatabaseMigrationCommitError(
            f"{label} refuses a hard-linked file ({link_count} links): {clean_path}"
        )
    return {
        "path": str(clean_path),
        "device_id": int(stat.st_dev),
        "file_id": int(stat.st_ino),
        "link_count": link_count,
        "size": int(stat.st_size),
    }


def _physical_identity_key(identity: Mapping[str, Any]) -> tuple[int, int]:
    return (int(identity["device_id"]), int(identity["file_id"]))


def require_distinct_file_identities(
    files: Mapping[str, str | Path],
) -> dict[str, dict[str, Any]]:
    """Reject distinct path strings that resolve to one physical file."""

    identities = {
        str(label): require_unaliased_regular_file(path, label=str(label))
        for label, path in files.items()
    }
    seen: dict[tuple[int, int], str] = {}
    for label, identity in identities.items():
        key = _physical_identity_key(identity)
        prior = seen.get(key)
        if prior is not None:
            raise DatabaseMigrationCommitError(
                f"{label} and {prior} address the same physical file"
            )
        seen[key] = label
    return identities


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _resolve_unaliased_commit_path(
    value: str | Path,
    *,
    label: str,
) -> Path:
    """Resolve a commit primitive input only after checking its raw path chain."""

    requested = Path(value).expanduser()
    offending_component = first_reparse_component(requested)
    if offending_component is not None:
        raise DatabaseMigrationCommitError(
            f"{label} path may not contain a symlink or reparse point: "
            f"{offending_component}"
        )
    return requested.resolve()


def require_no_sqlite_sidecars(database_path: str | Path) -> None:
    """Fail closed if any WAL, shared-memory, or rollback journal path exists.

    The function deliberately never tries to decide whether a sidecar is stale.
    Deleting or moving a hot sidecar can lose committed data or make recovery
    impossible, so formal file-copy/file-replacement primitives require all
    three names to be absent.
    """

    clean_path = _resolve_unaliased_commit_path(
        database_path,
        label="SQLite database",
    )
    if not clean_path.is_file():
        raise DatabaseMigrationCommitError(f"Database does not exist: {clean_path}")
    present: list[str] = []
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        sidecar = Path(f"{clean_path}{suffix}")
        if not _path_lexists(sidecar):
            continue
        try:
            size = sidecar.stat().st_size
            present.append(f"{sidecar} ({size} bytes)")
        except OSError:
            present.append(str(sidecar))
    if present:
        raise SQLiteSidecarPresent(
            "SQLite sidecar paths must be absent; none were deleted: "
            + "; ".join(present)
        )


def _require_windows(operation: str) -> None:
    if os.name != "nt":
        raise DatabaseMigrationCommitError(
            f"{operation} requires the audited Windows commit path; "
            "no weaker file-locking fallback was used"
        )


def _win32_path(path: Path) -> str:
    value = str(path.expanduser().resolve())
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def _win32_kernel32() -> Any:
    _require_windows("Win32 database commit")
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
    ]
    kernel32.LockFileEx.restype = wintypes.BOOL
    kernel32.UnlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
    ]
    kernel32.UnlockFileEx.restype = wintypes.BOOL
    kernel32.ReplaceFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    kernel32.ReplaceFileW.restype = wintypes.BOOL
    kernel32.MoveFileExW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
    ]
    kernel32.MoveFileExW.restype = wintypes.BOOL
    return kernel32


def _raise_last_win32_error(operation: str) -> None:
    error_code = ctypes.get_last_error()
    detail = ctypes.FormatError(error_code).strip()
    raise DatabaseMigrationCommitError(
        f"{operation} failed with WinError {error_code}: {detail}"
    )


@contextlib.contextmanager
def _windows_exclusive_reader(path: Path) -> Iterator[BinaryIO]:
    """Open one file for read with share mode zero.

    Share mode zero rejects every existing open handle and prevents new opens
    until this context exits. The returned Python file owns the Win32 handle.
    """

    _require_windows("Exclusive raw database read")
    import msvcrt
    from ctypes import wintypes

    clean_path = path.expanduser().resolve()
    kernel32 = _win32_kernel32()
    generic_read = 0x80000000
    open_existing = 3
    file_attribute_normal = 0x00000080
    file_flag_sequential_scan = 0x08000000
    invalid_handle_value = ctypes.c_void_p(-1).value
    handle = kernel32.CreateFileW(
        _win32_path(clean_path),
        generic_read,
        0,
        None,
        open_existing,
        file_attribute_normal | file_flag_sequential_scan,
        None,
    )
    if handle == invalid_handle_value:
        _raise_last_win32_error(f"Exclusive open of {clean_path}")

    descriptor: int | None = None
    handle_owned_by_descriptor = False
    try:
        binary_flag = getattr(os, "O_BINARY", 0)
        descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | binary_flag)
        handle_owned_by_descriptor = True
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            descriptor = None
            yield source
    finally:
        if descriptor is not None:
            os.close(descriptor)
        elif not handle_owned_by_descriptor:
            kernel32.CloseHandle(wintypes.HANDLE(handle))


def _copy_locked_reader(
    source: BinaryIO,
    destination: BinaryIO,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        destination.write(chunk)
        digest.update(chunk)
        total += len(chunk)
    destination.flush()
    os.fsync(destination.fileno())
    return total, digest.hexdigest()


@contextlib.contextmanager
def _windows_sqlite_byte_range_lease(path: Path) -> Iterator[BinaryIO]:
    """Exclusively hold SQLite's pending, reserved, and shared lock bytes."""

    _require_windows("SQLite migration byte-range lease")
    import msvcrt
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    clean_path = path.expanduser().resolve()
    kernel32 = _win32_kernel32()
    invalid_handle_value = ctypes.c_void_p(-1).value
    handle = kernel32.CreateFileW(
        _win32_path(clean_path),
        0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
        0x00000001 | 0x00000002 | 0x00000004,  # share read/write/delete
        None,
        3,  # OPEN_EXISTING
        0x00000080,  # FILE_ATTRIBUTE_NORMAL
        None,
    )
    if handle == invalid_handle_value:
        _raise_last_win32_error(f"Migration lease open of {clean_path}")

    overlapped = _Overlapped()
    overlapped.Offset = _SQLITE_PENDING_BYTE
    descriptor: int | None = None
    stream: BinaryIO | None = None
    handle_owned_by_descriptor = False
    locked = False
    completed = False
    try:
        if not kernel32.LockFileEx(
            handle,
            0x00000002 | 0x00000001,  # exclusive | fail immediately
            0,
            _SQLITE_LOCK_RANGE_SIZE,
            0,
            ctypes.byref(overlapped),
        ):
            error_code = ctypes.get_last_error()
            detail = ctypes.FormatError(error_code).strip()
            raise DatabaseMigrationCommitError(
                f"SQLite migration byte-range lease of {clean_path} failed "
                f"with WinError {error_code}: {detail}"
            )
        locked = True
        descriptor = msvcrt.open_osfhandle(
            int(handle),
            os.O_RDWR | getattr(os, "O_BINARY", 0),
        )
        handle_owned_by_descriptor = True
        stream = os.fdopen(descriptor, "r+b", closefd=True)
        descriptor = None
        yield stream
        completed = True
    finally:
        unlock_error: tuple[int, str] | None = None
        if locked and not kernel32.UnlockFileEx(
            handle,
            0,
            _SQLITE_LOCK_RANGE_SIZE,
            0,
            ctypes.byref(overlapped),
        ):
            error_code = ctypes.get_last_error()
            unlock_error = (error_code, ctypes.FormatError(error_code).strip())
        if stream is not None:
            stream.close()
        elif descriptor is not None:
            os.close(descriptor)
        elif not handle_owned_by_descriptor:
            kernel32.CloseHandle(wintypes.HANDLE(handle))
        if completed and unlock_error is not None:
            error_code, detail = unlock_error
            raise DatabaseMigrationCommitError(
                f"SQLite migration byte-range lease release of {clean_path} "
                f"failed with WinError {error_code}: {detail}"
            )


def locked_raw_copy(
    source: str | Path,
    destination: str | Path,
) -> dict[str, Any]:
    """Create an exact durable copy while a Win32 share-zero handle is held.

    A partially created destination is intentionally preserved if an I/O error
    occurs. It is recovery evidence and is never silently removed.
    """

    source_path = _resolve_unaliased_commit_path(source, label="Locked-copy source")
    destination_path = _resolve_unaliased_commit_path(
        destination,
        label="Locked-copy destination",
    )
    if source_path == destination_path:
        raise DatabaseMigrationCommitError("Source and destination must differ")
    if not source_path.is_file():
        raise DatabaseMigrationCommitError(f"Source does not exist: {source_path}")
    if _path_lexists(destination_path):
        raise DatabaseMigrationCommitError(
            f"Destination already exists: {destination_path}"
        )
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    with _windows_exclusive_reader(source_path) as source_file:
        source_identity = require_unaliased_regular_file(
            source_path,
            label="Locked-copy source",
        )
        require_no_sqlite_sidecars(source_path)
        with destination_path.open("xb") as destination_file:
            size, source_sha256 = _copy_locked_reader(
                source_file,
                destination_file,
            )
        destination_sha256 = _file_sha256(destination_path)
        if source_sha256 != destination_sha256:
            raise DatabaseMigrationCommitError(
                "Locked raw copy hash mismatch; destination was preserved"
            )
        if destination_path.stat().st_size != size:
            raise DatabaseMigrationCommitError(
                "Locked raw copy size mismatch; destination was preserved"
            )
        source_identity_after = require_unaliased_regular_file(
            source_path,
            label="Locked-copy source",
        )
        if _physical_identity_key(source_identity_after) != _physical_identity_key(
            source_identity
        ):
            raise DatabaseMigrationCommitError(
                "Locked-copy source file identity changed; destination was preserved"
            )
    copied_identities = require_distinct_file_identities(
        {
            "Locked-copy source": source_path,
            "Locked-copy destination": destination_path,
        }
    )
    return {
        "source_path": str(source_path),
        "destination_path": str(destination_path),
        "size": size,
        "source_sha256": source_sha256,
        "destination_sha256": destination_sha256,
        "verified_equal_to_source": True,
        "source_file_id": copied_identities["Locked-copy source"]["file_id"],
        "destination_file_id": copied_identities["Locked-copy destination"][
            "file_id"
        ],
    }


def copy_to_same_directory_staging(
    candidate: str | Path,
    source: str | Path,
) -> Path:
    """Copy a closed candidate into a durable, same-directory staging file."""

    candidate_path = _resolve_unaliased_commit_path(
        candidate,
        label="Migration candidate",
    )
    source_path = _resolve_unaliased_commit_path(
        source,
        label="Migration source",
    )
    if not candidate_path.is_file():
        raise DatabaseMigrationCommitError(
            f"Candidate does not exist: {candidate_path}"
        )
    if not source_path.is_file():
        raise DatabaseMigrationCommitError(f"Source does not exist: {source_path}")
    if candidate_path == source_path:
        raise DatabaseMigrationCommitError("Candidate and source must differ")
    require_distinct_file_identities(
        {
            "Migration candidate": candidate_path,
            "Migration source": source_path,
        }
    )

    descriptor, staging_name = tempfile.mkstemp(
        prefix=f".{source_path.name}.migration-stage-",
        suffix=".sqlite3",
        dir=source_path.parent,
    )
    staging_path = Path(staging_name).resolve()
    descriptor_open = True
    try:
        with _windows_exclusive_reader(candidate_path) as candidate_file:
            require_unaliased_regular_file(
                candidate_path,
                label="Migration candidate",
            )
            require_no_sqlite_sidecars(candidate_path)
            with os.fdopen(descriptor, "wb", closefd=True) as staging_file:
                descriptor_open = False
                size, candidate_sha256 = _copy_locked_reader(
                    candidate_file,
                    staging_file,
                )
        if staging_path.stat().st_size != size:
            raise DatabaseMigrationCommitError(
                f"Staging size mismatch; artifact preserved at {staging_path}"
            )
        if _file_sha256(staging_path) != candidate_sha256:
            raise DatabaseMigrationCommitError(
                f"Staging hash mismatch; artifact preserved at {staging_path}"
            )
        require_no_sqlite_sidecars(staging_path)
        require_distinct_file_identities(
            {
                "Migration candidate": candidate_path,
                "Migration source": source_path,
                "Migration staging": staging_path,
            }
        )
        return staging_path
    finally:
        if descriptor_open:
            os.close(descriptor)


def _same_windows_volume(first: Path, second: Path) -> bool:
    first_drive = os.path.splitdrive(str(first))[0].casefold()
    second_drive = os.path.splitdrive(str(second))[0].casefold()
    return bool(first_drive) and first_drive == second_drive


def _identity_from_exclusive_reader(path: Path) -> dict[str, Any]:
    with _windows_exclusive_reader(path) as source:
        require_unaliased_regular_file(path, label="Exclusively opened SQLite file")
        require_no_sqlite_sidecars(path)
        return _identity_from_open_reader(path, source)


def _identity_from_open_reader(path: Path, source: BinaryIO) -> dict[str, Any]:
    stat_before = os.fstat(source.fileno())
    if int(stat_before.st_nlink) != 1:
        raise DatabaseMigrationCommitError(
            f"SQLite migration refuses a hard-linked open file: {path}"
        )
    source.seek(0)
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(chunk)
        size += len(chunk)
    stat_after = os.fstat(source.fileno())
    before_identity = (
        int(stat_before.st_dev),
        int(stat_before.st_ino),
        int(stat_before.st_nlink),
        int(stat_before.st_size),
    )
    after_identity = (
        int(stat_after.st_dev),
        int(stat_after.st_ino),
        int(stat_after.st_nlink),
        int(stat_after.st_size),
    )
    if before_identity != after_identity:
        raise DatabaseMigrationCommitError(
            f"SQLite file identity changed while hashing: {path}"
        )
    return {
        "path": str(path),
        "size": size,
        "sha256": digest.hexdigest(),
        "device_id": int(stat_after.st_dev),
        "file_id": int(stat_after.st_ino),
        "link_count": int(stat_after.st_nlink),
    }


def _validate_expected_sha256(value: str, label: str) -> str:
    clean_value = str(value)
    if not re.fullmatch(r"[0-9a-f]{64}", clean_value):
        raise DatabaseMigrationCommitError(
            f"{label} must be one lowercase SHA-256 digest"
        )
    return clean_value


@contextlib.contextmanager
def hold_sqlite_file_lease(
    path: str | Path,
    *,
    expected_sha256: str,
) -> Iterator[dict[str, Any]]:
    """Hold one verified SQLite image against ordinary readers and writers."""

    clean_path = _resolve_unaliased_commit_path(
        path,
        label="Leased SQLite file",
    )
    expected_hash = _validate_expected_sha256(
        expected_sha256,
        "expected_sha256",
    )
    if not clean_path.is_file():
        raise DatabaseMigrationCommitError(f"SQLite file is missing: {clean_path}")
    with _windows_sqlite_byte_range_lease(clean_path) as source:
        path_identity = require_unaliased_regular_file(
            clean_path,
            label="Leased SQLite file",
        )
        require_no_sqlite_sidecars(clean_path)
        identity = _identity_from_open_reader(clean_path, source)
        if _physical_identity_key(identity) != _physical_identity_key(path_identity):
            raise DatabaseMigrationCommitError(
                f"SQLite file identity drifted while acquiring migration lease: {clean_path}"
            )
        if identity["sha256"] != expected_hash:
            raise DatabaseMigrationCommitError(
                f"SQLite file hash drifted while acquiring migration lease: {clean_path}"
            )
        yield {
            "identity": identity,
            "sqlite_lock_byte_range": {
                "offset": _SQLITE_PENDING_BYTE,
                "length": _SQLITE_LOCK_RANGE_SIZE,
                "exclusive": True,
                "held_through_context_exit": True,
            },
        }


@contextlib.contextmanager
def replace_file_with_backup(
    replaced: str | Path,
    replacement: str | Path,
    backup: str | Path,
    *,
    expected_replaced_sha256: str,
    expected_replacement_sha256: str,
) -> Iterator[dict[str, Any]]:
    """Replace a SQLite file under a fail-closed two-phase byte lease.

    The old source lock-byte lease is acquired before its final identity and
    sidecar checks. The closed staging file is then probed exclusively and
    ``ReplaceFileW`` runs immediately. While retaining the old-image lease, the
    function acquires a second lease on the new source. That lease remains held
    for caller postchecks and receipt publication until context exit. A writer
    winning the narrow post-replace race makes entry fail with both source
    images preserved for explicit recovery.
    """

    _require_windows("Atomic SQLite file replacement")
    replaced_path = _resolve_unaliased_commit_path(
        replaced,
        label="Replaced SQLite source",
    )
    replacement_path = _resolve_unaliased_commit_path(
        replacement,
        label="Replacement SQLite staging",
    )
    backup_path = _resolve_unaliased_commit_path(
        backup,
        label="Atomic rollback",
    )
    expected_source_hash = _validate_expected_sha256(
        expected_replaced_sha256,
        "expected_replaced_sha256",
    )
    expected_replacement_hash = _validate_expected_sha256(
        expected_replacement_sha256,
        "expected_replacement_sha256",
    )
    if len({replaced_path, replacement_path, backup_path}) != 3:
        raise DatabaseMigrationCommitError(
            "Replaced, replacement, and backup paths must be distinct"
        )
    if not replaced_path.is_file():
        raise DatabaseMigrationCommitError(f"Replaced file is missing: {replaced_path}")
    if not replacement_path.is_file():
        raise DatabaseMigrationCommitError(
            f"Replacement file is missing: {replacement_path}"
        )
    if _path_lexists(backup_path):
        raise DatabaseMigrationCommitError(f"Backup path already exists: {backup_path}")
    if not _same_windows_volume(replaced_path, replacement_path) or not _same_windows_volume(
        replaced_path, backup_path
    ):
        raise DatabaseMigrationCommitError(
            "ReplaceFileW requires replaced, replacement, and backup on one volume"
        )
    if replacement_path.parent != replaced_path.parent or backup_path.parent != replaced_path.parent:
        raise DatabaseMigrationCommitError(
            "Formal migration staging and atomic backup must share the database directory"
        )
    require_distinct_file_identities(
        {
            "Replaced SQLite source": replaced_path,
            "Replacement SQLite staging": replacement_path,
        }
    )

    with _windows_sqlite_byte_range_lease(replaced_path) as old_source_handle:
        replaced_path_identity = require_unaliased_regular_file(
            replaced_path,
            label="Replaced SQLite source",
        )
        require_no_sqlite_sidecars(replaced_path)
        replaced_before = _identity_from_open_reader(
            replaced_path,
            old_source_handle,
        )
        if _physical_identity_key(replaced_before) != _physical_identity_key(
            replaced_path_identity
        ):
            raise DatabaseMigrationCommitError(
                "Replaced source file identity drifted before ReplaceFileW"
            )
        if replaced_before["sha256"] != expected_source_hash:
            raise DatabaseMigrationCommitError(
                "Replaced source hash drifted before ReplaceFileW; no replacement "
                "was attempted"
            )

        # ReplaceFileW rejects an open staging handle even when it grants delete
        # sharing. Probe it share-zero, verify the expected bytes and sidecars,
        # close it, and invoke ReplaceFileW immediately while the old source
        # remains protected by its SQLite byte lease.
        replacement_before = _identity_from_exclusive_reader(replacement_path)
        if replacement_before["sha256"] != expected_replacement_hash:
            raise DatabaseMigrationCommitError(
                "Replacement staging hash drifted before ReplaceFileW; no "
                "replacement was attempted"
            )
        kernel32 = _win32_kernel32()
        succeeded = kernel32.ReplaceFileW(
            _win32_path(replaced_path),
            _win32_path(replacement_path),
            _win32_path(backup_path),
            0,
            None,
            None,
        )
        if not succeeded:
            _raise_last_win32_error(
                f"ReplaceFileW({replaced_path}, {replacement_path}, {backup_path})"
            )

        if not replaced_path.is_file() or not backup_path.is_file():
            raise DatabaseMigrationCommitError(
                "ReplaceFileW returned success with an unexpected file layout; "
                "all artifacts were preserved"
            )
        if _path_lexists(replacement_path):
            raise DatabaseMigrationCommitError(
                "Replacement path still exists after ReplaceFileW; artifacts preserved"
            )
        try:
            new_source_lease = _windows_sqlite_byte_range_lease(replaced_path)
            new_source_handle = new_source_lease.__enter__()
        except (DatabaseMigrationCommitError, OSError) as exc:
            raise DatabaseMigrationCommitError(
                "ReplaceFileW succeeded but the new source byte-range lease could "
                "not be acquired; source, atomic backup, and every sidecar were "
                f"preserved for explicit recovery: {exc}"
            ) from exc
        try:
            require_no_sqlite_sidecars(replaced_path)
            replaced_after_path_identity = require_unaliased_regular_file(
                replaced_path,
                label="Replaced SQLite source after ReplaceFileW",
            )
            backup_after_path_identity = require_unaliased_regular_file(
                backup_path,
                label="Atomic rollback after ReplaceFileW",
            )
            replaced_after = _identity_from_open_reader(
                replaced_path,
                new_source_handle,
            )
            backup_after = _identity_from_open_reader(
                backup_path,
                old_source_handle,
            )
            if _physical_identity_key(replaced_after) != _physical_identity_key(
                replaced_after_path_identity
            ) or _physical_identity_key(backup_after) != _physical_identity_key(
                backup_after_path_identity
            ):
                raise DatabaseMigrationCommitError(
                    "ReplaceFileW path/file-id binding changed during verification; "
                    "artifacts preserved"
                )
            if _physical_identity_key(replaced_after) != _physical_identity_key(
                replacement_before
            ):
                raise DatabaseMigrationCommitError(
                    "ReplaceFileW source file-id does not match the verified staging image; "
                    "artifacts preserved"
                )
            if _physical_identity_key(backup_after) != _physical_identity_key(
                replaced_before
            ):
                raise DatabaseMigrationCommitError(
                    "ReplaceFileW atomic backup file-id does not match the original source; "
                    "artifacts preserved"
                )
            if _physical_identity_key(replaced_after) == _physical_identity_key(
                backup_after
            ):
                raise DatabaseMigrationCommitError(
                    "ReplaceFileW produced aliased source and backup images; artifacts preserved"
                )
            if replaced_after["link_count"] != 1 or backup_after["link_count"] != 1:
                raise DatabaseMigrationCommitError(
                    "ReplaceFileW produced a hard-linked source or backup; artifacts preserved"
                )
            if replaced_after["sha256"] != expected_replacement_hash:
                raise DatabaseMigrationCommitError(
                    "Replacement hash changed during the commit window; artifacts "
                    "preserved"
                )
            if backup_after["sha256"] != expected_source_hash:
                raise DatabaseMigrationCommitError(
                    "Original database changed during the commit window; atomic "
                    "backup preserved"
                )
            yield {
                "replaced_before": replaced_before,
                "replacement_before": replacement_before,
                "replaced_after": replaced_after,
                "backup_after": backup_after,
                "sqlite_lock_byte_range": {
                    "offset": _SQLITE_PENDING_BYTE,
                    "length": _SQLITE_LOCK_RANGE_SIZE,
                    "exclusive": True,
                    "old_source_held_through_context_exit": True,
                    "new_source_held_through_context_exit": True,
                },
                "matches_verified_images": True,
            }
        finally:
            new_source_lease.__exit__(None, None, None)


def publish_bytes_exclusive_durable(
    destination: str | Path,
    payload: bytes,
) -> Path:
    """Publish complete bytes without ever exposing a partial destination.

    Bytes are first written and fsynced under a unique temporary name in the
    destination directory.  Publication is fail-if-exists.  If publication
    fails, the completed temporary artifact is deliberately preserved for
    diagnosis; an existing destination is never replaced.
    """

    _require_windows("Exclusive durable migration artifact publication")
    destination_path = _resolve_unaliased_commit_path(
        destination,
        label="Migration artifact",
    )
    if _path_lexists(destination_path):
        raise DatabaseMigrationCommitError(
            f"Destination already exists: {destination_path}"
        )
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.pending-",
        suffix=".tmp",
        dir=destination_path.parent,
    )
    temp_path = Path(temp_name).resolve()
    descriptor_open = True
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor_open = False
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        kernel32 = _win32_kernel32()
        movefile_write_through = 0x00000008
        if not kernel32.MoveFileExW(
            _win32_path(temp_path),
            _win32_path(destination_path),
            movefile_write_through,
        ):
            _raise_last_win32_error(
                f"Publishing exclusive file {destination_path}"
            )
        return destination_path
    finally:
        if descriptor_open:
            os.close(descriptor)
        # A failed publish is recovery evidence and is intentionally kept.


class MigrationIntentJournal:
    """Append-only, fsynced and hash-chained migration intent markers."""

    def __init__(self, database_path: str | Path, operation_id: str) -> None:
        requested_database_path = Path(database_path).expanduser()
        offending_component = first_reparse_component(requested_database_path)
        if offending_component is not None:
            raise MigrationIntentJournalError(
                "Migration journal database path may not contain a symlink or "
                f"reparse point: {offending_component}"
            )
        self.database_path = requested_database_path.resolve()
        self.operation_id = str(operation_id)
        if not _OPERATION_ID_RE.fullmatch(self.operation_id):
            raise MigrationIntentJournalError(
                "Migration operation_id must be one lowercase SHA-256 digest"
            )
        escaped_name = re.escape(self.database_path.name)
        escaped_operation = re.escape(self.operation_id)
        self._operation_pattern = re.compile(
            rf"^\.{escaped_name}\.migration-{escaped_operation}\."
            rf"(?P<sequence>[0-9]{{6}})-(?P<event>[a-z][a-z0-9_]*)\.json$"
        )
        self._operation_pattern_casefold = re.compile(
            rf"^\.{escaped_name}\.migration-{escaped_operation}\."
            rf"(?P<sequence>[0-9]{{6}})-(?P<event>[a-z][a-z0-9_]*)\.json$",
            re.IGNORECASE,
        )

    @classmethod
    def _all_operations_pattern(cls, database_path: Path) -> re.Pattern[str]:
        escaped_name = re.escape(database_path.name)
        return re.compile(
            rf"^\.{escaped_name}\.migration-"
            rf"(?P<operation>[0-9a-f]{{64}})\."
            rf"(?P<sequence>[0-9]{{6}})-(?P<event>[a-z][a-z0-9_]*)\.json$",
            re.IGNORECASE,
        )

    def _marker_path(self, sequence: int, event: str) -> Path:
        return self.database_path.parent / (
            f".{self.database_path.name}.migration-{self.operation_id}."
            f"{sequence:06d}-{event}.json"
        )

    def _marker_files(self) -> list[tuple[int, str, Path]]:
        if not self.database_path.parent.is_dir():
            return []
        result: list[tuple[int, str, Path]] = []
        for child in self.database_path.parent.iterdir():
            match = self._operation_pattern.fullmatch(child.name)
            if match is None:
                casefold_match = self._operation_pattern_casefold.fullmatch(child.name)
                if casefold_match is not None:
                    raise MigrationIntentJournalError(
                        f"Migration marker filename is not canonical: {child}"
                    )
                continue
            sequence = int(match.group("sequence"))
            event = match.group("event")
            canonical = self._marker_path(sequence, event)
            if child.name != canonical.name:
                raise MigrationIntentJournalError(
                    f"Migration marker filename is not canonical: {child}"
                )
            try:
                metadata = child.lstat()
            except OSError as exc:
                raise MigrationIntentJournalError(
                    f"Cannot inspect migration marker identity: {child}: {exc}"
                ) from exc
            reparse_flag = int(
                getattr(statlib, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0
            )
            if child.is_symlink() or bool(
                reparse_flag
                and int(getattr(metadata, "st_file_attributes", 0) or 0)
                & reparse_flag
            ):
                raise MigrationIntentJournalError(
                    f"Migration marker may not be a symlink or reparse point: {child}"
                )
            if not statlib.S_ISREG(metadata.st_mode) or int(metadata.st_nlink) != 1:
                raise MigrationIntentJournalError(
                    f"Migration marker must be an independent regular file: {child}"
                )
            result.append((sequence, event, child))
        result.sort(key=lambda item: (item[0], item[1], item[2].name))
        return result

    @staticmethod
    def _load_marker(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MigrationIntentJournalError(
                f"Cannot read migration intent marker {path}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise MigrationIntentJournalError(
                f"Migration intent marker is not an object: {path}"
            )
        return value

    @staticmethod
    def _marker_digest(marker: Mapping[str, Any]) -> str:
        payload = dict(marker)
        payload.pop("marker_sha256", None)
        return _sha256_value(payload)

    @staticmethod
    def _validate_event_transition(
        previous: Mapping[str, Any] | None,
        event: str,
        details: Mapping[str, Any],
    ) -> None:
        if previous is None:
            if event != "intent":
                raise MigrationIntentJournalError(
                    "The first migration marker must be an intent"
                )
            return
        previous_event = str(previous.get("event") or "")
        allowed_previous = {
            "replace_started": {"intent"},
            "replace_returned": {"replace_started"},
            "verified": {"replace_returned"},
            "recovery_verified": {
                "replace_started",
                "replace_returned",
                "verified",
                "recovery_verified",
            },
            "receipt_committed": {"verified", "recovery_verified"},
            "complete": {"receipt_committed"},
            "rollback_started": {
                "replace_started",
                "replace_returned",
                "verified",
                "recovery_verified",
            },
            "rollback_receipt_committed": {"rollback_started"},
            "rolled_back": {"rollback_receipt_committed"},
            "abort_verified": {"intent", "replace_started"},
            "aborted": {"abort_verified"},
        }
        if event not in allowed_previous:
            raise MigrationIntentJournalError(
                f"Unsupported migration marker event: {event}"
            )
        if previous_event not in allowed_previous[event]:
            raise MigrationIntentJournalError(
                f"Invalid migration marker transition: {previous_event} -> {event}"
            )
        if event in TERMINAL_MIGRATION_EVENTS and dict(details) != dict(
            previous.get("details") or {}
        ):
            raise MigrationIntentJournalError(
                f"Terminal migration marker {event} does not bind the verified prior marker"
            )

    def inspect(self) -> dict[str, Any]:
        files = self._marker_files()
        if not files:
            return {
                "database_path": str(self.database_path),
                "operation_id": self.operation_id,
                "exists": False,
                "valid": True,
                "active": False,
                "terminal_event": None,
                "markers": [],
            }

        markers: list[dict[str, Any]] = []
        prior_sha256 = ""
        seen_sequences: set[int] = set()
        terminal_event: str | None = None
        for expected_sequence, (sequence, filename_event, path) in enumerate(files):
            if sequence in seen_sequences or sequence != expected_sequence:
                raise MigrationIntentJournalError(
                    f"Migration marker sequence is duplicated or non-contiguous at {path}"
                )
            seen_sequences.add(sequence)
            marker = self._load_marker(path)
            if marker.get("version") != MIGRATION_INTENT_MARKER_VERSION:
                raise MigrationIntentJournalError(
                    f"Unsupported migration marker version at {path}"
                )
            if marker.get("database_path") != str(self.database_path):
                raise MigrationIntentJournalError(
                    f"Migration marker is bound to another database at {path}"
                )
            if marker.get("operation_id") != self.operation_id:
                raise MigrationIntentJournalError(
                    f"Migration marker operation mismatch at {path}"
                )
            if marker.get("sequence") != sequence or marker.get("event") != filename_event:
                raise MigrationIntentJournalError(
                    f"Migration marker filename/payload mismatch at {path}"
                )
            if marker.get("marker_path") != str(path):
                raise MigrationIntentJournalError(
                    f"Migration marker path binding mismatch at {path}"
                )
            if marker.get("previous_marker_sha256") != prior_sha256:
                raise MigrationIntentJournalError(
                    f"Migration marker hash chain is broken at {path}"
                )
            expected_digest = self._marker_digest(marker)
            if marker.get("marker_sha256") != expected_digest:
                raise MigrationIntentJournalError(
                    f"Migration marker digest is invalid at {path}"
                )
            self._validate_event_transition(
                markers[-1] if markers else None,
                filename_event,
                marker.get("details") if isinstance(marker.get("details"), dict) else {},
            )
            if terminal_event is not None:
                raise MigrationIntentJournalError(
                    f"Migration marker was appended after terminal event {terminal_event}"
                )
            if filename_event in TERMINAL_MIGRATION_EVENTS:
                terminal_event = filename_event
            prior_sha256 = expected_digest
            markers.append(marker)

        return {
            "database_path": str(self.database_path),
            "operation_id": self.operation_id,
            "exists": True,
            "valid": True,
            "active": terminal_event is None,
            "terminal_event": terminal_event,
            "last_event": str(markers[-1]["event"]),
            "last_marker_sha256": prior_sha256,
            "markers": markers,
        }

    def append(
        self,
        event: str,
        details: Mapping[str, Any] | None = None,
        *,
        _gate_capability: object | None = None,
    ) -> dict[str, Any]:
        clean_event = str(event)
        if not _EVENT_RE.fullmatch(clean_event):
            raise MigrationIntentJournalError(
                "Migration marker event must be a lowercase identifier"
            )
        if (
            clean_event in _GATE_ONLY_MIGRATION_EVENTS
            and _gate_capability is not _MIGRATION_GATE_CAPABILITY
        ):
            raise MigrationIntentJournalError(
                f"Migration marker event {clean_event} is reserved for the authorized gate"
            )
        state = self.inspect()
        if state["exists"] and not state["active"]:
            raise MigrationIntentJournalError(
                f"Migration operation is already terminal: {state['terminal_event']}"
            )
        sequence = len(state["markers"])
        if sequence == 0 and clean_event != "intent":
            raise MigrationIntentJournalError(
                "The first migration marker must be an intent"
            )
        if sequence > 0 and clean_event == "intent":
            raise MigrationIntentJournalError("Migration intent already exists")
        clean_details = dict(details or {})
        self._validate_event_transition(
            state["markers"][-1] if state["markers"] else None,
            clean_event,
            clean_details,
        )

        marker_path = self._marker_path(sequence, clean_event).resolve()
        marker: dict[str, Any] = {
            "version": MIGRATION_INTENT_MARKER_VERSION,
            "database_path": str(self.database_path),
            "operation_id": self.operation_id,
            "sequence": sequence,
            "event": clean_event,
            "created_at_epoch_ms": int(time.time() * 1000),
            "marker_path": str(marker_path),
            "previous_marker_sha256": str(
                state.get("last_marker_sha256") or ""
            ),
            "details": clean_details,
        }
        marker["marker_sha256"] = self._marker_digest(marker)
        self._write_marker_exclusive(marker_path, marker)
        return marker

    @staticmethod
    def _write_marker_exclusive(path: Path, marker: Mapping[str, Any]) -> None:
        encoded = json.dumps(
            marker,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
        publish_bytes_exclusive_durable(path, encoded)

    @classmethod
    def scan_active(cls, database_path: str | Path) -> list[dict[str, Any]]:
        clean_database_path = _resolve_unaliased_commit_path(
            database_path,
            label="Migration journal database",
        )
        parent = clean_database_path.parent
        if not parent.is_dir():
            return []
        pattern = cls._all_operations_pattern(clean_database_path)
        operation_ids: set[str] = set()
        for child in parent.iterdir():
            match = pattern.fullmatch(child.name)
            if match is not None:
                operation_ids.add(match.group("operation"))

        active: list[dict[str, Any]] = []
        for operation_id in sorted(operation_ids):
            operation_id = operation_id.casefold()
            journal = cls(clean_database_path, operation_id)
            try:
                state = journal.inspect()
            except MigrationIntentJournalError as exc:
                active.append(
                    {
                        "database_path": str(clean_database_path),
                        "operation_id": operation_id,
                        "exists": True,
                        "valid": False,
                        "active": True,
                        "terminal_event": None,
                        "error": str(exc),
                    }
                )
                continue
            if state["active"]:
                active.append(state)
        return active


def _append_migration_gate_event(
    journal: MigrationIntentJournal,
    event: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a state-changing marker only from the migration gate module."""

    return journal.append(
        event,
        details,
        _gate_capability=_MIGRATION_GATE_CAPABILITY,
    )


def scan_active_migration_operations(
    database_path: str | Path,
) -> list[dict[str, Any]]:
    return MigrationIntentJournal.scan_active(database_path)


__all__ = [
    "DatabaseMigrationCommitError",
    "MIGRATION_INTENT_MARKER_VERSION",
    "MigrationIntentJournal",
    "MigrationIntentJournalError",
    "SQLiteSidecarPresent",
    "TERMINAL_MIGRATION_EVENTS",
    "copy_to_same_directory_staging",
    "hold_sqlite_file_lease",
    "locked_raw_copy",
    "publish_bytes_exclusive_durable",
    "require_distinct_file_identities",
    "replace_file_with_backup",
    "require_no_sqlite_sidecars",
    "require_unaliased_regular_file",
    "scan_active_migration_operations",
]
