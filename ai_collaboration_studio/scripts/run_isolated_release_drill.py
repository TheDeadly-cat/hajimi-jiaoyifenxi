from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import zipfile
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


if __package__:
    from scripts import create_versioned_source_backup as source_backup
    from scripts.run_fresh_source_smoke import safe_extract
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import create_versioned_source_backup as source_backup
    from scripts.run_fresh_source_smoke import safe_extract


DRILL_VERSION = "isolated_release_drill_v1"
INSTALL_RECEIPT_VERSION = "release_install_receipt_v1"
POINTER_VERSION = "release_activation_pointer_v1"
ACTIVATION_RECEIPT_VERSION = "release_activation_receipt_v1"
FAILURE_RECEIPT_VERSION = "release_readiness_failure_v1"
_RELEASE_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PROTECTED_PORTS = (8770, 11111, 18787)


class ReleaseDrillError(RuntimeError):
    """Raised when an isolated release lifecycle invariant fails."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _value_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sealed(value: dict[str, Any], hash_field: str) -> dict[str, Any]:
    payload = json.loads(json.dumps(value, ensure_ascii=False))
    payload[hash_field] = _value_sha256(payload)
    return payload


def _validate_seal(value: dict[str, Any], hash_field: str) -> None:
    digest = value.get(hash_field)
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise ReleaseDrillError(f"{hash_field} is invalid")
    unsigned = dict(value)
    unsigned.pop(hash_field, None)
    if _value_sha256(unsigned) != digest:
        raise ReleaseDrillError(f"{hash_field} does not match")


def _write_json_atomic(
    path: Path,
    payload: dict[str, Any],
    *,
    replace: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not replace and path.exists():
        raise ReleaseDrillError("receipt already exists")
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    body = (json.dumps(payload, ensure_ascii=True, indent=2) + "\n").encode(
        "ascii"
    )
    try:
        with temporary.open("xb") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        if not replace and path.exists():
            raise ReleaseDrillError("receipt appeared before publication")
        if replace:
            os.replace(temporary, path)
        else:
            os.rename(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_manifest(archive_path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            value = json.loads(
                archive.read(source_backup.MANIFEST_NAME).decode("utf-8")
            )
    except (KeyError, UnicodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise ReleaseDrillError("release archive manifest is unreadable") from exc
    return source_backup._validate_manifest(value)


def _validate_installed_tree(
    release_path: Path,
    manifest: dict[str, Any],
) -> None:
    expected = {
        source_backup.MANIFEST_NAME,
        *(str(row["path"]) for row in manifest["files"]),
    }
    actual = {
        path.relative_to(release_path).as_posix()
        for path in release_path.rglob("*")
        if path.is_file()
    }
    if actual != expected:
        raise ReleaseDrillError("installed release file inventory drifted")
    for row in manifest["files"]:
        path = release_path / str(row["path"])
        if path.stat().st_size != int(row["size"]):
            raise ReleaseDrillError("installed release file size drifted")
        if _file_sha256(path) != str(row["sha256"]):
            raise ReleaseDrillError("installed release file hash drifted")


def install_release(archive_path: Path, release_root: Path) -> dict[str, Any]:
    archive = Path(os.path.abspath(os.fspath(archive_path)))
    root = Path(os.path.abspath(os.fspath(release_root)))
    verification = source_backup.verify_backup(archive)
    manifest = _load_manifest(archive)
    release_id = str(manifest["backup_version"])
    if _RELEASE_ID_RE.fullmatch(release_id) is None:
        raise ReleaseDrillError("release id is invalid")
    releases = root / "releases"
    receipts = root / "receipts"
    releases.mkdir(parents=True, exist_ok=True)
    receipts.mkdir(parents=True, exist_ok=True)
    target = releases / release_id
    receipt_path = receipts / f"install-{release_id}.json"
    if target.exists() or receipt_path.exists():
        raise ReleaseDrillError("release installation is immutable and already exists")
    staging = releases / f".{release_id}.{uuid4().hex}.staging"
    try:
        safe_extract(archive, staging)
        _validate_installed_tree(staging, manifest)
        if target.exists():
            raise ReleaseDrillError("release target appeared before publication")
        os.rename(staging, target)
    finally:
        if staging.exists():
            if staging.parent.resolve() != releases.resolve():
                raise ReleaseDrillError("release staging cleanup escaped its root")
            shutil.rmtree(staging)
    receipt = _sealed(
        {
            "version": INSTALL_RECEIPT_VERSION,
            "release_id": release_id,
            "archive_sha256": str(verification["archive_sha256"]),
            "source_total_sha256": str(manifest["total_sha256"]),
            "file_count": int(manifest["file_count"]),
            "total_size": int(manifest["total_size"]),
            "release_directory": f"releases/{release_id}",
        },
        "receipt_sha256",
    )
    _write_json_atomic(receipt_path, receipt, replace=False)
    return receipt


def _read_install_receipt(release_root: Path, release_id: str) -> dict[str, Any]:
    if _RELEASE_ID_RE.fullmatch(release_id) is None:
        raise ReleaseDrillError("requested release id is invalid")
    root = Path(os.path.abspath(os.fspath(release_root)))
    path = root / "receipts" / f"install-{release_id}.json"
    try:
        receipt = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseDrillError("install receipt is unavailable") from exc
    if not isinstance(receipt, dict) or set(receipt) != {
        "version",
        "release_id",
        "archive_sha256",
        "source_total_sha256",
        "file_count",
        "total_size",
        "release_directory",
        "receipt_sha256",
    }:
        raise ReleaseDrillError("install receipt is not closed")
    if receipt["version"] != INSTALL_RECEIPT_VERSION or receipt["release_id"] != release_id:
        raise ReleaseDrillError("install receipt identity drifted")
    _validate_seal(receipt, "receipt_sha256")
    release_path = root / "releases" / release_id
    manifest_path = release_path / source_backup.MANIFEST_NAME
    try:
        manifest = source_backup._validate_manifest(
            json.loads(manifest_path.read_text(encoding="utf-8"))
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseDrillError("installed manifest is unavailable") from exc
    if (
        receipt["source_total_sha256"] != manifest["total_sha256"]
        or receipt["file_count"] != manifest["file_count"]
        or receipt["total_size"] != manifest["total_size"]
    ):
        raise ReleaseDrillError("install receipt no longer matches its manifest")
    _validate_installed_tree(release_path, manifest)
    return receipt


def read_activation_pointer(release_root: Path) -> dict[str, Any] | None:
    path = Path(os.path.abspath(os.fspath(release_root))) / "current-release.json"
    if not path.exists():
        return None
    try:
        pointer = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseDrillError("activation pointer is unreadable") from exc
    if not isinstance(pointer, dict) or set(pointer) != {
        "version",
        "generation",
        "action",
        "active_release_id",
        "previous_release_id",
        "source_total_sha256",
        "pointer_sha256",
    }:
        raise ReleaseDrillError("activation pointer is not closed")
    if pointer["version"] != POINTER_VERSION:
        raise ReleaseDrillError("activation pointer version drifted")
    if type(pointer["generation"]) is not int or pointer["generation"] < 1:
        raise ReleaseDrillError("activation pointer generation is invalid")
    _validate_seal(pointer, "pointer_sha256")
    return pointer


def _publish_pointer(
    release_root: Path,
    *,
    action: str,
    active_release_id: str,
    previous_release_id: str | None,
    generation: int,
) -> dict[str, Any]:
    root = Path(os.path.abspath(os.fspath(release_root)))
    install = _read_install_receipt(root, active_release_id)
    pointer = _sealed(
        {
            "version": POINTER_VERSION,
            "generation": generation,
            "action": action,
            "active_release_id": active_release_id,
            "previous_release_id": previous_release_id,
            "source_total_sha256": install["source_total_sha256"],
        },
        "pointer_sha256",
    )
    _write_json_atomic(root / "current-release.json", pointer, replace=True)
    activation_receipt = _sealed(
        {
            "version": ACTIVATION_RECEIPT_VERSION,
            "generation": generation,
            "action": action,
            "active_release_id": active_release_id,
            "previous_release_id": previous_release_id,
            "pointer_sha256": pointer["pointer_sha256"],
        },
        "receipt_sha256",
    )
    _write_json_atomic(
        root / "receipts" / f"activation-{generation:04d}-{action}.json",
        activation_receipt,
        replace=False,
    )
    return pointer


def activate_release(
    release_root: Path,
    release_id: str,
    *,
    expected_active_release_id: str | None,
) -> dict[str, Any]:
    root = Path(os.path.abspath(os.fspath(release_root)))
    current = read_activation_pointer(root)
    actual = current["active_release_id"] if current is not None else None
    if actual != expected_active_release_id:
        raise ReleaseDrillError("activation pointer changed before upgrade")
    return _publish_pointer(
        root,
        action="activate",
        active_release_id=release_id,
        previous_release_id=actual,
        generation=(int(current["generation"]) + 1 if current else 1),
    )


def build_synthetic_failure_receipt(release_id: str) -> dict[str, Any]:
    return _sealed(
        {
            "version": FAILURE_RECEIPT_VERSION,
            "status": "not_ready",
            "release_id": release_id,
            "reason_code": "DRILL_INJECTED_NOT_READY",
            "synthetic": True,
        },
        "receipt_sha256",
    )


def rollback_release(
    release_root: Path,
    *,
    failed_release_id: str,
    target_release_id: str,
    expected_generation: int,
    failure_receipt: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(failure_receipt, dict) or set(failure_receipt) != {
        "version",
        "status",
        "release_id",
        "reason_code",
        "synthetic",
        "receipt_sha256",
    }:
        raise ReleaseDrillError("failure receipt is not closed")
    _validate_seal(failure_receipt, "receipt_sha256")
    if (
        failure_receipt["version"] != FAILURE_RECEIPT_VERSION
        or failure_receipt["status"] != "not_ready"
        or failure_receipt["release_id"] != failed_release_id
        or failure_receipt["synthetic"] is not True
    ):
        raise ReleaseDrillError("failure receipt does not authorize this rollback")
    root = Path(os.path.abspath(os.fspath(release_root)))
    current = read_activation_pointer(root)
    if current is None:
        raise ReleaseDrillError("rollback requires an active release")
    if (
        current["generation"] != expected_generation
        or current["active_release_id"] != failed_release_id
        or current["previous_release_id"] != target_release_id
    ):
        raise ReleaseDrillError("rollback target is not the exact previous release")
    return _publish_pointer(
        root,
        action="rollback",
        active_release_id=target_release_id,
        previous_release_id=failed_release_id,
        generation=expected_generation + 1,
    )


def _database_family_state(database_path: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = Path(str(database_path) + suffix)
        if path.is_file():
            result[path.name] = {
                "bytes": path.stat().st_size,
                "sha256": _file_sha256(path),
            }
    return result


class _MibTcpRowOwnerPid(ctypes.Structure):
    _fields_ = [
        ("dwState", wintypes.DWORD),
        ("dwLocalAddr", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwRemoteAddr", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


class _MibTcp6RowOwnerPid(ctypes.Structure):
    _fields_ = [
        ("ucLocalAddr", ctypes.c_ubyte * 16),
        ("dwLocalScopeId", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("ucRemoteAddr", ctypes.c_ubyte * 16),
        ("dwRemoteScopeId", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwState", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


def _windows_listener_ports(
    address_family: int,
    row_type: type[ctypes.Structure],
) -> set[int]:
    if os.name != "nt":
        raise ReleaseDrillError("passive TCP listener inspection requires Windows")
    get_table = ctypes.WinDLL("iphlpapi.dll").GetExtendedTcpTable
    get_table.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.ULONG),
        wintypes.BOOL,
        wintypes.ULONG,
        ctypes.c_int,
        wintypes.ULONG,
    )
    get_table.restype = wintypes.DWORD
    error_insufficient_buffer = 122
    tcp_table_owner_pid_listener = 3
    for _attempt in range(3):
        size = wintypes.ULONG(0)
        status = get_table(
            None,
            ctypes.byref(size),
            False,
            address_family,
            tcp_table_owner_pid_listener,
            0,
        )
        if status not in (0, error_insufficient_buffer) or size.value < 4:
            raise ReleaseDrillError(
                f"passive TCP listener size query failed: status={status}"
            )
        buffer = ctypes.create_string_buffer(size.value)
        status = get_table(
            buffer,
            ctypes.byref(size),
            False,
            address_family,
            tcp_table_owner_pid_listener,
            0,
        )
        if status == error_insufficient_buffer:
            continue
        if status != 0:
            raise ReleaseDrillError(
                f"passive TCP listener query failed: status={status}"
            )
        count = wintypes.DWORD.from_buffer(buffer).value
        row_size = ctypes.sizeof(row_type)
        if 4 + count * row_size > len(buffer):
            raise ReleaseDrillError("passive TCP listener table is truncated")
        return {
            socket.ntohs(row_type.from_buffer(buffer, 4 + index * row_size).dwLocalPort & 0xFFFF)
            for index in range(count)
        }
    raise ReleaseDrillError("passive TCP listener table changed repeatedly")


def _protected_port_state() -> dict[str, bool]:
    listening = _windows_listener_ports(socket.AF_INET, _MibTcpRowOwnerPid)
    listening.update(
        _windows_listener_ports(socket.AF_INET6, _MibTcp6RowOwnerPid)
    )
    return {str(port): port in listening for port in _PROTECTED_PORTS}


def _utc_second(offset_seconds: int = 0) -> str:
    value = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(
        seconds=offset_seconds
    )
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def run_drill(source_root: Path) -> dict[str, Any]:
    if os.environ.get("AI_STUDIO_SKIP_LOCAL_ENV", "").strip() != "1":
        raise ReleaseDrillError("AI_STUDIO_SKIP_LOCAL_ENV=1 is required")
    source = Path(os.path.abspath(os.fspath(source_root)))
    required = (
        source / "server.py",
        source / "requirements-lock-win-py314.txt",
        source / "frontend" / "package.json",
    )
    if not source.is_dir() or not all(path.is_file() for path in required):
        raise ReleaseDrillError("source root is not a complete Studio release source")
    protected_before = _protected_port_state()
    if any(protected_before.values()):
        raise ReleaseDrillError("a protected port already has a listener")

    system_temp = Path(tempfile.gettempdir()).resolve()
    with tempfile.TemporaryDirectory(
        prefix="ai-studio-release-drill-",
        dir=system_temp,
        ignore_cleanup_errors=True,
    ) as work_text:
        work = Path(work_text).resolve()
        try:
            work.relative_to(system_temp)
        except ValueError as exc:
            raise ReleaseDrillError("release drill escaped system temp") from exc
        current_archive = source_backup.create_backup(
            source_root=source,
            destination_root=work / "current-archive",
            source_root_label="ai_collaboration_studio_current",
            created_at_utc=_utc_second(),
        )
        baseline_source = work / "synthetic-baseline-source"
        safe_extract(current_archive, baseline_source)
        (baseline_source / source_backup.MANIFEST_NAME).unlink()
        readme = baseline_source / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8")
            + "\n<!-- isolated synthetic release baseline -->\n",
            encoding="utf-8",
        )
        baseline_archive = source_backup.create_backup(
            source_root=baseline_source,
            destination_root=work / "baseline-archive",
            source_root_label="ai_collaboration_studio_synthetic_baseline",
            created_at_utc=_utc_second(-2),
        )
        release_root = work / "release-root"
        baseline_install = install_release(baseline_archive, release_root)
        current_install = install_release(current_archive, release_root)
        if baseline_install["source_total_sha256"] == current_install["source_total_sha256"]:
            raise ReleaseDrillError("synthetic baseline did not differ from current source")

        database_path = work / "application-data" / "studio.sqlite3"
        database_path.parent.mkdir(parents=True)
        prior_runtime = os.environ.get("AI_STUDIO_RUNTIME_DIR")
        prior_database = os.environ.get("AI_STUDIO_DATABASE_PATH")
        os.environ["AI_STUDIO_RUNTIME_DIR"] = str(database_path.parent)
        os.environ["AI_STUDIO_DATABASE_PATH"] = str(database_path)
        try:
            from backend.store import StudioStore

            StudioStore(database_path)
            with sqlite3.connect(database_path) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            database_before = _database_family_state(database_path)

            first = activate_release(
                release_root,
                str(baseline_install["release_id"]),
                expected_active_release_id=None,
            )
            upgraded = activate_release(
                release_root,
                str(current_install["release_id"]),
                expected_active_release_id=str(baseline_install["release_id"]),
            )
            failure = build_synthetic_failure_receipt(
                str(current_install["release_id"])
            )
            rolled_back = rollback_release(
                release_root,
                failed_release_id=str(current_install["release_id"]),
                target_release_id=str(baseline_install["release_id"]),
                expected_generation=int(upgraded["generation"]),
                failure_receipt=failure,
            )
            database_after = _database_family_state(database_path)
        finally:
            if prior_runtime is None:
                os.environ.pop("AI_STUDIO_RUNTIME_DIR", None)
            else:
                os.environ["AI_STUDIO_RUNTIME_DIR"] = prior_runtime
            if prior_database is None:
                os.environ.pop("AI_STUDIO_DATABASE_PATH", None)
            else:
                os.environ["AI_STUDIO_DATABASE_PATH"] = prior_database

        if database_before != database_after:
            raise ReleaseDrillError("release activation changed application data")
        if (
            first["generation"] != 1
            or upgraded["generation"] != 2
            or rolled_back["generation"] != 3
            or rolled_back["active_release_id"] != baseline_install["release_id"]
        ):
            raise ReleaseDrillError("release pointer sequence drifted")
        reinstall_blocked = False
        try:
            install_release(baseline_archive, release_root)
        except ReleaseDrillError:
            reinstall_blocked = True
        stale_activation_blocked = False
        try:
            activate_release(
                release_root,
                str(current_install["release_id"]),
                expected_active_release_id=str(current_install["release_id"]),
            )
        except ReleaseDrillError:
            stale_activation_blocked = True
        if not reinstall_blocked or not stale_activation_blocked:
            raise ReleaseDrillError("release immutability or concurrency guard failed")
        protected_after = _protected_port_state()
        if any(protected_after.values()):
            raise ReleaseDrillError("release drill opened a protected listener")
        package = json.loads(
            (source / "frontend" / "package.json").read_text(encoding="utf-8")
        )
        result = {
            "schema_version": DRILL_VERSION,
            "ok": True,
            "project": {
                "id": str(package.get("name") or ""),
                "version": str(package.get("version") or ""),
            },
            "install": {
                "release_count": 2,
                "baseline_release_id": baseline_install["release_id"],
                "baseline_source_sha256": baseline_install["source_total_sha256"],
                "current_release_id": current_install["release_id"],
                "current_source_sha256": current_install["source_total_sha256"],
                "current_file_count": current_install["file_count"],
                "reinstall_blocked": reinstall_blocked,
            },
            "activation": {
                "initial_generation": first["generation"],
                "upgrade_generation": upgraded["generation"],
                "rollback_generation": rolled_back["generation"],
                "final_active_release_id": rolled_back["active_release_id"],
                "stale_activation_blocked": stale_activation_blocked,
                "failure_reason_code": failure["reason_code"],
            },
            "application_data": {
                "family_unchanged": True,
                "files": database_after,
            },
            "ports": {
                "before": protected_before,
                "after": protected_after,
            },
            "boundaries": {
                "system_temp_only": True,
                "synthetic_baseline": True,
                "historical_upgrade_compatibility_proven": False,
                "application_started": False,
                "dependency_installation_performed": False,
                "database_migration_executed": False,
                "formal_database_opened": False,
                "external_network_requests": 0,
                "autonomous_release_authority": False,
            },
        }
    return result


def write_report(path: Path, payload: dict[str, Any]) -> None:
    target = Path(os.path.abspath(os.fspath(path)))
    if target.exists():
        raise ReleaseDrillError("release drill report already exists")
    _write_json_atomic(target, payload, replace=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an isolated synthetic install, upgrade, and rollback drill"
    )
    parser.add_argument("--source-root")
    parser.add_argument("--report")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    source_root = Path(
        arguments.source_root or Path(__file__).resolve().parents[1]
    )
    report_path = Path(
        arguments.report
        or Path(tempfile.gettempdir()) / f"ai-studio-release-drill-{uuid4().hex}.json"
    )
    try:
        result = run_drill(source_root)
        write_report(report_path, result)
    except (
        ReleaseDrillError,
        source_backup.SourceBackupError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": "ISOLATED_RELEASE_DRILL_FAILED",
                    "exception_type": type(exc).__name__,
                },
                ensure_ascii=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {"ok": True, "report_path": str(report_path), **result},
            ensure_ascii=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
