from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable


MANIFEST_NAME = "SOURCE_BACKUP_MANIFEST.json"
MANIFEST_VERSION = "source_backup_manifest_v1"
CONTENT_HASH_VERSION = "source_backup_content_v1"

_MANIFEST_KEYS = {
    "version",
    "backup_version",
    "created_at_utc",
    "source_root_label",
    "file_count",
    "total_size",
    "files",
    "total_sha256",
}
_FILE_KEYS = {"path", "size", "sha256"}
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_UTC_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
_BACKUP_VERSION_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}")

_EXCLUDED_DIRECTORY_NAMES = frozenset({
    ".git",
    ".npm-cache",
    "runtime",
    "node_modules",
    "dist",
    "__pycache__",
    "secrets",
})
_SECRET_EXACT_NAMES = frozenset({
    ".env",
    ".env.local",
    ".netrc",
    "_netrc",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "credential.json",
    "credentials.yml",
    "credentials.yaml",
    "secrets.json",
    "secret.json",
    "secrets.yml",
    "secrets.yaml",
    "secret.yml",
    "secret.yaml",
    "token.json",
    "tokens.json",
    "api_tokens.json",
    "apikeys.json",
    "oauth.json",
    "auth.json",
    "service-account.json",
    "service_account.json",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
})
_SECRET_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
)
_DATABASE_SUFFIXES = (
    ".db",
    ".sqlite",
    ".sqlite3",
    "-wal",
    "-shm",
    "-journal",
)
_MAX_FILES = 200_000
_MAX_MANIFEST_BYTES = 32 * 1024 * 1024
_COPY_CHUNK_SIZE = 1024 * 1024


class SourceBackupError(RuntimeError):
    """Raised when a source backup cannot be created or verified safely."""


@dataclass(frozen=True)
class _SourceFile:
    absolute_path: Path
    relative_path: str
    fingerprint: tuple[int, int, int, int, int, int]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(_COPY_CHUNK_SIZE), b""):
                digest.update(chunk)
    except OSError as exc:
        raise SourceBackupError(f"cannot hash archive: {path}") from exc
    return digest.hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _validate_utc(value: Any) -> str:
    if not isinstance(value, str) or not _UTC_RE.fullmatch(value):
        raise SourceBackupError("created_at_utc must be canonical second-precision UTC")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise SourceBackupError("created_at_utc is not a real UTC timestamp") from exc
    if parsed.year < 1980 or parsed.year > 2107:
        raise SourceBackupError("created_at_utc is outside the ZIP timestamp range")
    return value


def _validate_label(value: Any) -> str:
    label = str(value or "").strip()
    if not _LABEL_RE.fullmatch(label) or label in {".", ".."}:
        raise SourceBackupError(
            "source_root_label must contain only letters, digits, dot, dash, or underscore"
        )
    return label


def _is_reparse_or_symlink(path: Path, metadata: os.stat_result | None = None) -> bool:
    try:
        current = metadata if metadata is not None else path.lstat()
    except OSError as exc:
        raise SourceBackupError(f"cannot inspect path metadata: {path}") from exc
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
    attributes = int(getattr(current, "st_file_attributes", 0) or 0)
    return stat.S_ISLNK(current.st_mode) or bool(reparse_flag and attributes & reparse_flag)


def _assert_existing_chain_has_no_links(path: Path) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = absolute.parts
    if not parts:
        raise SourceBackupError("path is empty")
    current = Path(parts[0])
    for part in parts[1:]:
        current /= part
        if not current.exists() and not current.is_symlink():
            continue
        if _is_reparse_or_symlink(current):
            raise SourceBackupError(f"path contains a symlink or reparse point: {current}")


def _assert_destination_chain_is_creatable(path: Path) -> None:
    """Reject a missing destination whose nearest existing ancestor is a file."""

    current = Path(os.path.abspath(os.fspath(path)))
    while not current.exists() and not current.is_symlink():
        parent = current.parent
        if parent == current:
            return
        current = parent
    if current.exists() and not current.is_dir():
        raise SourceBackupError(
            f"destination root has a non-directory path component: {current}"
        )


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _safe_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise SourceBackupError("archive path is invalid")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value) is not None
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.as_posix() != value
    ):
        raise SourceBackupError(f"archive path escapes its root: {value!r}")
    return value


def _is_secret_filename(name: str) -> bool:
    lowered = name.casefold()
    if lowered in _SECRET_EXACT_NAMES:
        return True
    if lowered.startswith(".env."):
        # Keep explicitly safe templates/examples, but never archive a
        # deployment-specific environment file that may contain credentials.
        if lowered not in {".env.example", ".env.sample", ".env.template"}:
            return True
    if lowered.endswith(_SECRET_SUFFIXES):
        return True
    if lowered.endswith((".secret", ".secret.json", ".secret.yml", ".secret.yaml")):
        return True
    if lowered in {"api_key.txt", "apikey.txt", "access_token.txt", "token.txt"}:
        return True
    return "密钥" in name or "私钥" in name


def _is_excluded(relative_parts: tuple[str, ...], *, is_directory: bool) -> bool:
    lowered = tuple(part.casefold() for part in relative_parts)
    if any(part in _EXCLUDED_DIRECTORY_NAMES for part in lowered[:-1]):
        return True
    name = lowered[-1]
    if is_directory and name in _EXCLUDED_DIRECTORY_NAMES:
        return True
    if not is_directory and (
        name.endswith(".pyc")
        or name.endswith(_DATABASE_SUFFIXES)
        or _is_secret_filename(relative_parts[-1])
    ):
        return True
    return False


def _fingerprint(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(metadata.st_size),
        int(getattr(metadata, "st_mtime_ns", int(metadata.st_mtime * 1_000_000_000))),
        int(metadata.st_nlink),
    )


def _scan_source(source_root: Path) -> list[_SourceFile]:
    records: list[_SourceFile] = []
    identities: dict[tuple[int, int], str] = {}
    stack: list[Path] = [source_root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise SourceBackupError(f"cannot scan source directory: {directory}") from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                # On Windows, DirEntry.stat(follow_symlinks=False) may expose
                # zero-valued fast metadata for st_ino/st_nlink.  A direct
                # lstat is required before making file-identity decisions.
                metadata = path.lstat()
                relative = path.relative_to(source_root)
            except (OSError, ValueError) as exc:
                raise SourceBackupError(f"source path escaped or changed: {path}") from exc
            parts = tuple(relative.parts)
            is_directory = stat.S_ISDIR(metadata.st_mode)
            if _is_excluded(parts, is_directory=is_directory):
                continue
            if _is_reparse_or_symlink(path, metadata):
                raise SourceBackupError(
                    f"source contains a symlink or reparse point: {relative.as_posix()}"
                )
            if is_directory:
                stack.append(path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise SourceBackupError(
                    f"source contains a non-regular file: {relative.as_posix()}"
                )
            if int(metadata.st_nlink) != 1:
                raise SourceBackupError(
                    f"source file has ambiguous hard links: {relative.as_posix()}"
                )
            relative_posix = _safe_relative_path(relative.as_posix())
            if relative_posix == MANIFEST_NAME:
                raise SourceBackupError(
                    f"source uses the reserved manifest path: {MANIFEST_NAME}"
                )
            identity = (int(metadata.st_dev), int(metadata.st_ino))
            if identity in identities:
                raise SourceBackupError(
                    "two source paths resolve to one file identity: "
                    f"{identities[identity]} and {relative_posix}"
                )
            identities[identity] = relative_posix
            records.append(_SourceFile(
                absolute_path=path,
                relative_path=relative_posix,
                fingerprint=_fingerprint(metadata),
            ))
            if len(records) > _MAX_FILES:
                raise SourceBackupError("source contains too many files")
    records.sort(key=lambda item: item.relative_path)
    return records


def _open_source_file(record: _SourceFile) -> tuple[BinaryIO, os.stat_result]:
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0) or 0)
    flags |= int(getattr(os, "O_NOFOLLOW", 0) or 0)
    try:
        descriptor = os.open(record.absolute_path, flags)
    except OSError as exc:
        raise SourceBackupError(
            f"cannot open source file safely: {record.relative_path}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or int(metadata.st_nlink) != 1
            or _fingerprint(metadata) != record.fingerprint
        ):
            raise SourceBackupError(
                f"source file changed before reading: {record.relative_path}"
            )
        return os.fdopen(descriptor, "rb", closefd=True), metadata
    except Exception:
        os.close(descriptor)
        raise


def _hash_source_file(record: _SourceFile) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    stream, _metadata = _open_source_file(record)
    with stream:
        while True:
            chunk = stream.read(_COPY_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        end = os.fstat(stream.fileno())
    if _fingerprint(end) != record.fingerprint or size != record.fingerprint[3]:
        raise SourceBackupError(f"source file changed while hashing: {record.relative_path}")
    return size, digest.hexdigest()


def _content_sha256(files: list[dict[str, Any]]) -> str:
    return _sha256_json({
        "version": CONTENT_HASH_VERSION,
        "files": files,
    })


def _build_manifest(
    records: Iterable[_SourceFile],
    *,
    source_root_label: str,
    created_at_utc: str,
) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for record in records:
        size, digest = _hash_source_file(record)
        files.append({
            "path": record.relative_path,
            "size": size,
            "sha256": digest,
        })
    total_sha256 = _content_sha256(files)
    compact_utc = created_at_utc.replace("-", "").replace(":", "")
    backup_version = f"{compact_utc}-{total_sha256[:12]}"
    manifest = {
        "version": MANIFEST_VERSION,
        "backup_version": backup_version,
        "created_at_utc": created_at_utc,
        "source_root_label": source_root_label,
        "file_count": len(files),
        "total_size": sum(int(row["size"]) for row in files),
        "files": files,
        "total_sha256": total_sha256,
    }
    return _validate_manifest(manifest)


def _zip_info(name: str, created_at_utc: str) -> zipfile.ZipInfo:
    parsed = datetime.strptime(created_at_utc, "%Y-%m-%dT%H:%M:%SZ")
    info = zipfile.ZipInfo(
        filename=name,
        date_time=(
            parsed.year,
            parsed.month,
            parsed.day,
            parsed.hour,
            parsed.minute,
            parsed.second,
        ),
    )
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info


def _write_archive(
    output: BinaryIO,
    *,
    records: list[_SourceFile],
    manifest: dict[str, Any],
) -> None:
    expected = {row["path"]: row for row in manifest["files"]}
    created_at_utc = str(manifest["created_at_utc"])
    manifest_bytes = (_canonical_json(manifest) + "\n").encode("utf-8")
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        allowZip64=True,
    ) as archive:
        archive.writestr(
            _zip_info(MANIFEST_NAME, created_at_utc),
            manifest_bytes,
            compress_type=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        )
        for record in records:
            expected_entry = expected[record.relative_path]
            digest = hashlib.sha256()
            copied = 0
            source, _metadata = _open_source_file(record)
            with source, archive.open(
                _zip_info(record.relative_path, created_at_utc),
                mode="w",
                force_zip64=True,
            ) as target:
                while True:
                    chunk = source.read(_COPY_CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
                    copied += len(chunk)
                    target.write(chunk)
                end = os.fstat(source.fileno())
            if (
                _fingerprint(end) != record.fingerprint
                or copied != expected_entry["size"]
                or digest.hexdigest() != expected_entry["sha256"]
            ):
                raise SourceBackupError(
                    f"source file changed while archiving: {record.relative_path}"
                )


def _validate_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _MANIFEST_KEYS:
        raise SourceBackupError("backup manifest is not a closed v1 object")
    manifest = json.loads(json.dumps(value, ensure_ascii=False))
    if manifest.get("version") != MANIFEST_VERSION:
        raise SourceBackupError("backup manifest version is unsupported")
    created_at_utc = _validate_utc(manifest.get("created_at_utc"))
    label = _validate_label(manifest.get("source_root_label"))
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) > _MAX_FILES:
        raise SourceBackupError("backup manifest file list is invalid")
    normalized_files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in files:
        if not isinstance(raw, dict) or set(raw) != _FILE_KEYS:
            raise SourceBackupError("backup manifest file entry is not closed")
        path = _safe_relative_path(raw.get("path"))
        if PurePosixPath(path).name.casefold().endswith(_DATABASE_SUFFIXES):
            raise SourceBackupError("backup manifest contains a database-family file")
        if path == MANIFEST_NAME or path in seen:
            raise SourceBackupError("backup manifest contains a duplicate or reserved path")
        seen.add(path)
        size = raw.get("size")
        digest = raw.get("sha256")
        if type(size) is not int or size < 0:
            raise SourceBackupError("backup manifest file size is invalid")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise SourceBackupError("backup manifest file hash is invalid")
        normalized_files.append({"path": path, "size": size, "sha256": digest})
    if normalized_files != sorted(normalized_files, key=lambda row: row["path"]):
        raise SourceBackupError("backup manifest file entries are not sorted")
    file_count = manifest.get("file_count")
    total_size = manifest.get("total_size")
    if type(file_count) is not int or file_count != len(normalized_files):
        raise SourceBackupError("backup manifest file_count does not match")
    if (
        type(total_size) is not int
        or total_size < 0
        or total_size != sum(row["size"] for row in normalized_files)
    ):
        raise SourceBackupError("backup manifest total_size does not match")
    total_sha256 = manifest.get("total_sha256")
    if (
        not isinstance(total_sha256, str)
        or not _SHA256_RE.fullmatch(total_sha256)
        or total_sha256 != _content_sha256(normalized_files)
    ):
        raise SourceBackupError("backup manifest total hash does not match")
    expected_version = (
        f"{created_at_utc.replace('-', '').replace(':', '')}-"
        f"{total_sha256[:12]}"
    )
    if (
        manifest.get("backup_version") != expected_version
        or not _BACKUP_VERSION_RE.fullmatch(str(manifest.get("backup_version") or ""))
    ):
        raise SourceBackupError("backup manifest backup_version does not match")
    manifest["source_root_label"] = label
    manifest["files"] = normalized_files
    return manifest


def _validate_zip_member_name(name: str) -> str:
    if name == MANIFEST_NAME:
        return name
    return _safe_relative_path(name)


def _read_exact_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    expected_size: int,
    expected_sha256: str,
) -> None:
    if info.file_size != expected_size:
        raise SourceBackupError(f"ZIP size differs from manifest: {info.filename}")
    digest = hashlib.sha256()
    total = 0
    try:
        with archive.open(info, "r") as source:
            while True:
                chunk = source.read(_COPY_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > expected_size:
                    raise SourceBackupError(
                        f"ZIP member exceeds declared size: {info.filename}"
                    )
                digest.update(chunk)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise SourceBackupError(f"cannot read ZIP member: {info.filename}") from exc
    if total != expected_size or digest.hexdigest() != expected_sha256:
        raise SourceBackupError(f"ZIP member hash differs from manifest: {info.filename}")


def verify_backup(archive_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Verify a source backup without extracting or restoring any member."""

    raw_path = Path(archive_path)
    _assert_existing_chain_has_no_links(raw_path)
    try:
        path = raw_path.resolve(strict=True)
        metadata = path.lstat()
    except OSError as exc:
        raise SourceBackupError("backup archive does not exist") from exc
    if (
        _is_reparse_or_symlink(path, metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or int(metadata.st_nlink) != 1
    ):
        raise SourceBackupError("backup archive has an ambiguous file identity")
    try:
        with zipfile.ZipFile(path, "r", allowZip64=True) as archive:
            infos = archive.infolist()
            if len(infos) > _MAX_FILES + 1:
                raise SourceBackupError("backup ZIP contains too many members")
            by_name: dict[str, zipfile.ZipInfo] = {}
            for info in infos:
                name = _validate_zip_member_name(info.filename)
                if name in by_name:
                    raise SourceBackupError(f"backup ZIP contains duplicate member: {name}")
                if info.is_dir() or info.flag_bits & 0x1:
                    raise SourceBackupError("backup ZIP contains a directory or encrypted member")
                unix_mode = int(info.external_attr >> 16)
                if unix_mode and not stat.S_ISREG(unix_mode):
                    raise SourceBackupError("backup ZIP contains a non-regular member")
                by_name[name] = info
            manifest_info = by_name.get(MANIFEST_NAME)
            if manifest_info is None or manifest_info.file_size > _MAX_MANIFEST_BYTES:
                raise SourceBackupError("backup ZIP manifest is missing or oversized")
            try:
                manifest_bytes = archive.read(manifest_info)
                manifest_raw = json.loads(manifest_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
                raise SourceBackupError("backup ZIP manifest cannot be decoded") from exc
            manifest = _validate_manifest(manifest_raw)
            expected_manifest_bytes = (_canonical_json(manifest) + "\n").encode("utf-8")
            if manifest_bytes != expected_manifest_bytes:
                raise SourceBackupError("backup ZIP manifest is not canonical")
            expected_names = {MANIFEST_NAME, *(row["path"] for row in manifest["files"])}
            if set(by_name) != expected_names:
                raise SourceBackupError("backup ZIP members do not exactly match the manifest")
            for row in manifest["files"]:
                _read_exact_member(
                    archive,
                    by_name[row["path"]],
                    expected_size=int(row["size"]),
                    expected_sha256=str(row["sha256"]),
                )
    except SourceBackupError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise SourceBackupError("backup archive is not a valid ZIP") from exc
    return {
        "ok": True,
        "version": MANIFEST_VERSION,
        "backup_version": manifest["backup_version"],
        "archive_size": int(metadata.st_size),
        "archive_sha256": _file_sha256(path),
        "source_root_label": manifest["source_root_label"],
        "file_count": manifest["file_count"],
        "total_size": manifest["total_size"],
        "total_sha256": manifest["total_sha256"],
    }


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0) or 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _prepare_backup_inputs(
    *,
    destination_root: str | os.PathLike[str],
    source_root: str | os.PathLike[str] | None,
    source_root_label: str | None,
    created_at_utc: str | None,
    create_destination: bool,
) -> tuple[Path, Path, str, str, list[_SourceFile], dict[str, Any]]:
    if destination_root is None or not str(destination_root).strip():
        raise SourceBackupError("destination_root must be explicit")
    raw_source = (
        Path(source_root)
        if source_root is not None
        else Path(__file__).resolve().parents[1]
    )
    raw_destination = Path(destination_root)
    _assert_existing_chain_has_no_links(raw_source)
    _assert_existing_chain_has_no_links(raw_destination)
    try:
        source = raw_source.resolve(strict=True)
    except OSError as exc:
        raise SourceBackupError("source root does not exist") from exc
    if not source.is_dir() or _is_reparse_or_symlink(source):
        raise SourceBackupError("source root must be one real directory")
    destination = raw_destination.resolve(strict=False)
    if destination == source or _is_within(destination, source):
        raise SourceBackupError("destination root must be outside the source root")
    if destination.exists() and not destination.is_dir():
        raise SourceBackupError("destination root is not a directory")
    _assert_destination_chain_is_creatable(destination)
    if create_destination:
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SourceBackupError("destination root could not be created") from exc
        _assert_existing_chain_has_no_links(destination)
        destination = destination.resolve(strict=True)

    label = _validate_label(
        source_root_label if source_root_label is not None else source.name
    )
    timestamp = _validate_utc(created_at_utc or _utc_now())
    records = _scan_source(source)
    manifest = _build_manifest(
        records,
        source_root_label=label,
        created_at_utc=timestamp,
    )
    return source, destination, label, timestamp, records, manifest


def preflight_backup(
    *,
    destination_root: str | os.PathLike[str],
    source_root: str | os.PathLike[str] | None = None,
    source_root_label: str | None = None,
    created_at_utc: str | None = None,
) -> dict[str, Any]:
    """Inspect a future backup target without creating directories or files."""

    (
        source,
        destination,
        label,
        _timestamp,
        _records,
        manifest,
    ) = _prepare_backup_inputs(
        destination_root=destination_root,
        source_root=source_root,
        source_root_label=source_root_label,
        created_at_utc=created_at_utc,
        create_destination=False,
    )
    archive_name = f"{label}-source-{manifest['backup_version']}.zip"
    archive_path = destination / archive_name
    archive_exists = archive_path.exists() or archive_path.is_symlink()
    return {
        "ok": True,
        "version": MANIFEST_VERSION,
        "ready": not archive_exists,
        "source_root_label": label,
        "source_file_count": manifest["file_count"],
        "source_total_size": manifest["total_size"],
        "source_total_sha256": manifest["total_sha256"],
        "backup_version": manifest["backup_version"],
        "archive_path": str(archive_path),
        "archive_exists": archive_exists,
        "destination_exists": destination.is_dir(),
        "destination_requires_creation": not destination.exists(),
        "source_path": str(source),
    }


def create_backup(
    *,
    destination_root: str | os.PathLike[str],
    source_root: str | os.PathLike[str] | None = None,
    source_root_label: str | None = None,
    created_at_utc: str | None = None,
) -> Path:
    """Create one immutable, versioned source ZIP outside the source tree."""

    source, destination, label, _timestamp, records, manifest = _prepare_backup_inputs(
        destination_root=destination_root,
        source_root=source_root,
        source_root_label=source_root_label,
        created_at_utc=created_at_utc,
        create_destination=True,
    )
    archive_name = f"{label}-source-{manifest['backup_version']}.zip"
    final_path = destination / archive_name
    if final_path.exists() or final_path.is_symlink():
        raise SourceBackupError(f"backup version already exists: {archive_name}")

    temp_path = destination / f".{archive_name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    published = False
    try:
        descriptor = os.open(
            temp_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_BINARY", 0) or 0),
            0o600,
        )
        with os.fdopen(descriptor, "w+b", closefd=True) as output:
            descriptor = None
            _write_archive(output, records=records, manifest=manifest)
            output.flush()
            os.fsync(output.fileno())
        if _scan_source(source) != records:
            raise SourceBackupError("source tree changed while the backup was created")
        verify_backup(temp_path)
        try:
            os.link(temp_path, final_path, follow_symlinks=False)
        except FileExistsError as exc:
            raise SourceBackupError(f"backup version already exists: {archive_name}") from exc
        except OSError as exc:
            raise SourceBackupError("atomic no-clobber publication is unavailable") from exc
        published = True
        _fsync_directory(destination)
        temp_path.unlink()
        _fsync_directory(destination)
        return final_path
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                if not published:
                    raise SourceBackupError("temporary backup could not be removed")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preflight, create, or verify an immutable versioned source backup"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser(
        "preflight",
        help="inspect a future target without creating files",
    )
    preflight.add_argument(
        "--destination-root",
        required=True,
        help="explicit directory outside the source tree",
    )
    preflight.add_argument(
        "--source-root",
        help="source directory (defaults to the project root)",
    )
    preflight.add_argument(
        "--source-root-label",
        help="non-path label stored in the manifest (defaults to source basename)",
    )
    create = subparsers.add_parser("create", help="create one new source ZIP")
    create.add_argument(
        "--destination-root",
        required=True,
        help="explicit directory outside the source tree",
    )
    create.add_argument(
        "--source-root",
        help="source directory (defaults to the project root)",
    )
    create.add_argument(
        "--source-root-label",
        help="non-path label stored in the manifest (defaults to source basename)",
    )
    verify = subparsers.add_parser("verify", help="verify without restoring files")
    verify.add_argument("archive", help="versioned source ZIP to verify")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "preflight":
            result = preflight_backup(
                destination_root=args.destination_root,
                source_root=args.source_root,
                source_root_label=args.source_root_label,
            )
        elif args.command == "create":
            path = create_backup(
                destination_root=args.destination_root,
                source_root=args.source_root,
                source_root_label=args.source_root_label,
            )
            result = {"ok": True, "archive": str(path)}
        else:
            result = verify_backup(args.archive)
        print(_canonical_json(result))
        return 0
    except SourceBackupError as exc:
        print(_canonical_json({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
