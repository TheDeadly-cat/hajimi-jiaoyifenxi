from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from .create_versioned_source_backup import (
        SourceBackupError,
        _assert_existing_chain_has_no_links,
        _fingerprint,
        _is_within,
        _open_source_file,
        _scan_source,
    )
except ImportError:  # pragma: no cover - direct script execution
    from create_versioned_source_backup import (
        SourceBackupError,
        _assert_existing_chain_has_no_links,
        _fingerprint,
        _is_within,
        _open_source_file,
        _scan_source,
    )


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_DIRECTORY = "ai_collaboration_studio"
PROJECTION_VERSION = "github_source_projection_v1"
CONTENT_VERSION = "github_source_projection_content_v1"
ROOT_ASSET_MAP = {
    ".github/workflows/isolated-validation.yml": (
        ".github/workflows/isolated-validation.yml"
    ),
    "delivery/repository-root/README.md": "README.md",
    "delivery/repository-root/run_ai_collaboration_studio.cmd.template": (
        "run_ai_collaboration_studio.cmd"
    ),
}
ROOT_WORKFLOW_PATH = ".github/workflows/isolated-validation.yml"
_COPY_CHUNK_SIZE = 1024 * 1024
_FORBIDDEN_DIRECTORY_NAMES = frozenset({
    ".git",
    ".npm-cache",
    ".pytest_cache",
    "__pycache__",
    "backups",
    "dist",
    "logs",
    "node_modules",
    "runtime",
    "secrets",
})
_FORBIDDEN_EXACT_NAMES = frozenset({
    ".env",
    ".env.local",
    ".npmrc",
    ".pypirc",
    "source_backup_manifest.json",
})
_FORBIDDEN_SUFFIXES = (
    ".db",
    ".key",
    ".p12",
    ".pem",
    ".pfx",
    ".sqlite",
    ".sqlite3",
    "-shm",
    "-wal",
)


class GitHubProjectionError(RuntimeError):
    """Raised when a source-only repository projection cannot be proven safe."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _content_sha256(files: list[dict[str, Any]]) -> str:
    payload = {"version": CONTENT_VERSION, "files": files}
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _safe_projection_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise GitHubProjectionError("projection path is invalid")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value) is not None
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.as_posix() != value
    ):
        raise GitHubProjectionError(f"projection path escapes its root: {value!r}")
    return value


def _forbidden_projection_reason(value: str) -> str:
    path = _safe_projection_path(value)
    parts = tuple(part.casefold() for part in PurePosixPath(path).parts)
    if any(part in _FORBIDDEN_DIRECTORY_NAMES for part in parts[:-1]):
        return "forbidden_directory"
    name = parts[-1]
    if name in _FORBIDDEN_EXACT_NAMES:
        return "forbidden_filename"
    if name.startswith(".env.") and name not in {
        ".env.example",
        ".env.sample",
        ".env.template",
    }:
        return "forbidden_environment_file"
    if name.endswith(_FORBIDDEN_SUFFIXES):
        return "forbidden_suffix"
    return ""


def _validated_roots(
    source_root: str | os.PathLike[str],
    destination_root: str | os.PathLike[str],
) -> tuple[Path, Path]:
    source = Path(source_root).resolve(strict=True)
    if not source.is_dir():
        raise GitHubProjectionError("source root must be an existing directory")
    _assert_existing_chain_has_no_links(source)

    raw_destination = Path(os.path.abspath(os.fspath(destination_root)))
    if raw_destination.exists() or raw_destination.is_symlink():
        raise GitHubProjectionError("destination root must not already exist")
    parent = raw_destination.parent.resolve(strict=True)
    _assert_existing_chain_has_no_links(parent)
    destination = parent / raw_destination.name
    if not destination.name or destination.name in {".", ".."}:
        raise GitHubProjectionError("destination root name is invalid")
    if _is_within(destination, source) or _is_within(source, destination):
        raise GitHubProjectionError("source and destination roots must be disjoint")
    return source, destination


def _copy_record(record: Any, target: Path, projected_path: str) -> dict[str, Any]:
    safe_path = _safe_projection_path(projected_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_BINARY", 0) or 0)
    descriptor: int | None = None
    source = None
    digest = hashlib.sha256()
    copied = 0
    try:
        descriptor = os.open(target, flags, 0o600)
        source, _metadata = _open_source_file(record)
        with source, os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = None
            while True:
                chunk = source.read(_COPY_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                copied += len(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
            source_end = os.fstat(source.fileno())
        source = None
        if (
            _fingerprint(source_end) != record.fingerprint
            or copied != record.fingerprint[3]
        ):
            raise GitHubProjectionError(
                f"source file changed while projecting: {record.relative_path}"
            )
        try:
            target.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
        except OSError:
            pass
        return {"path": safe_path, "size": copied, "sha256": digest.hexdigest()}
    except (OSError, SourceBackupError) as exc:
        raise GitHubProjectionError(
            f"cannot project source file: {record.relative_path}"
        ) from exc
    finally:
        if source is not None:
            source.close()
        if descriptor is not None:
            os.close(descriptor)


def _validate_root_workflow(workflow_path: Path) -> None:
    try:
        workflow = workflow_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise GitHubProjectionError("root workflow is not valid UTF-8") from exc
    required = (
        "shell: pwsh",
        "working-directory: ai_collaboration_studio",
        "cache-dependency-path: ai_collaboration_studio/frontend/package-lock.json",
    )
    missing = [value for value in required if value not in workflow]
    if missing:
        raise GitHubProjectionError(
            "root workflow is missing repository-layout requirements: "
            + ", ".join(missing)
        )


def create_github_projection(
    *,
    destination_root: str | os.PathLike[str],
    source_root: str | os.PathLike[str] = PROJECT_ROOT,
) -> dict[str, Any]:
    """Create one new source-only repository tree without touching Git state."""

    source, destination = _validated_roots(source_root, destination_root)
    try:
        records = _scan_source(source)
    except SourceBackupError as exc:
        raise GitHubProjectionError(str(exc)) from exc
    by_path = {record.relative_path: record for record in records}
    missing_assets = sorted(set(ROOT_ASSET_MAP) - set(by_path))
    if missing_assets:
        raise GitHubProjectionError(
            "canonical repository-root assets are missing: " + ", ".join(missing_assets)
        )

    prefix = f".{destination.name}.projection-"
    with tempfile.TemporaryDirectory(prefix=prefix, dir=destination.parent) as staging_name:
        staging = Path(staging_name)
        projected_files: list[dict[str, Any]] = []
        source_files: list[dict[str, Any]] = []
        for record in records:
            projected_path = f"{PROJECT_DIRECTORY}/{record.relative_path}"
            entry = _copy_record(record, staging / PurePosixPath(projected_path), projected_path)
            projected_files.append(entry)
            source_files.append({
                "path": record.relative_path,
                "size": entry["size"],
                "sha256": entry["sha256"],
            })

        for source_path, projected_path in ROOT_ASSET_MAP.items():
            record = by_path[source_path]
            projected_files.append(_copy_record(
                record,
                staging / PurePosixPath(projected_path),
                projected_path,
            ))

        try:
            current_records = _scan_source(source)
        except SourceBackupError as exc:
            raise GitHubProjectionError(str(exc)) from exc
        if current_records != records:
            raise GitHubProjectionError("source tree changed while projection was created")
        projected_files.sort(key=lambda item: item["path"])
        source_files.sort(key=lambda item: item["path"])
        forbidden = [
            {"path": item["path"], "reason": reason}
            for item in projected_files
            if (reason := _forbidden_projection_reason(item["path"]))
        ]
        if forbidden:
            raise GitHubProjectionError(
                "projection contains forbidden paths: "
                + ", ".join(item["path"] for item in forbidden[:8])
            )

        root_workflow = staging / PurePosixPath(ROOT_WORKFLOW_PATH)
        _validate_root_workflow(root_workflow)
        nested_workflow = (
            staging / PROJECT_DIRECTORY / PurePosixPath(ROOT_WORKFLOW_PATH)
        )
        if root_workflow.read_bytes() != nested_workflow.read_bytes():
            raise GitHubProjectionError("root workflow drifted from its canonical template")

        receipt = {
            "ok": True,
            "version": PROJECTION_VERSION,
            "layout": "nested_studio_with_root_delivery_v1",
            "source_file_count": len(source_files),
            "source_total_size": sum(item["size"] for item in source_files),
            "source_total_sha256": _content_sha256(source_files),
            "projected_file_count": len(projected_files),
            "projected_total_size": sum(item["size"] for item in projected_files),
            "projected_total_sha256": _content_sha256(projected_files),
            "root_workflow_path": ROOT_WORKFLOW_PATH,
            "root_workflow_sha256": next(
                item["sha256"]
                for item in projected_files
                if item["path"] == ROOT_WORKFLOW_PATH
            ),
            "forbidden_paths": [],
        }
        try:
            os.replace(staging, destination)
        except OSError as exc:
            raise GitHubProjectionError("projection could not be published atomically") from exc
        receipt["destination_root"] = str(destination)
        return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a deterministic source-only GitHub repository projection"
    )
    parser.add_argument(
        "--destination-root",
        required=True,
        help="new directory outside the source tree; it must not already exist",
    )
    parser.add_argument(
        "--source-root",
        default=str(PROJECT_ROOT),
        help="authoritative Studio source directory",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = create_github_projection(
            source_root=args.source_root,
            destination_root=args.destination_root,
        )
        print(_canonical_json(result))
        return 0
    except GitHubProjectionError as exc:
        print(_canonical_json({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
