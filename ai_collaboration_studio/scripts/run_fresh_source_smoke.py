from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from typing import Any
from uuid import uuid4
from zipfile import ZipFile


SMOKE_VERSION = "fresh_source_smoke_v1"
PROTECTED_PORTS = (8770, 11111, 18787)
PROVIDER_AND_PROXY_NAMES = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "FUTU_PASSWORD_MD5",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


class SmokeError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def file_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False, "bytes": 0, "sha256": None}
    return {
        "exists": True,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def formal_database_state(source_root: Path) -> dict[str, Any]:
    main = source_root / "runtime" / "collaboration_studio.sqlite3"
    return {
        "main": file_state(main),
        "wal": file_state(Path(f"{main}-wal")),
        "shm": file_state(Path(f"{main}-shm")),
    }


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
        raise SmokeError("passive TCP listener inspection requires Windows")
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
            raise SmokeError(
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
            raise SmokeError(
                f"passive TCP listener query failed: status={status}"
            )
        count = wintypes.DWORD.from_buffer(buffer).value
        row_size = ctypes.sizeof(row_type)
        if 4 + count * row_size > len(buffer):
            raise SmokeError("passive TCP listener table is truncated")
        return {
            socket.ntohs(
                row_type.from_buffer(
                    buffer,
                    4 + index * row_size,
                ).dwLocalPort
                & 0xFFFF
            )
            for index in range(count)
        }
    raise SmokeError("passive TCP listener table changed repeatedly")


def protected_port_state() -> dict[str, bool]:
    listening = _windows_listener_ports(socket.AF_INET, _MibTcpRowOwnerPid)
    listening.update(
        _windows_listener_ports(socket.AF_INET6, _MibTcp6RowOwnerPid)
    )
    return {str(port): port in listening for port in PROTECTED_PORTS}


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.15)
        return client.connect_ex(("127.0.0.1", port)) == 0


def isolated_app_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    environment = dict(base if base is not None else os.environ)
    environment["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
    environment["FUTU_HOST"] = "127.0.0.1"
    environment["FUTU_PORT"] = "1"
    for name in PROVIDER_AND_PROXY_NAMES:
        environment.pop(name, None)
    environment["FUTU_HOST"] = "127.0.0.1"
    environment["FUTU_PORT"] = "1"
    return environment


def run_command(
    label: str,
    arguments: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: int,
) -> dict[str, Any]:
    started = time.monotonic()
    completed = subprocess.run(
        arguments,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    result = {
        "label": label,
        "exit_code": completed.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
        "stdout_tail": completed.stdout[-16000:],
        "stderr_tail": completed.stderr[-16000:],
    }
    test_summary = parse_test_summary(completed.stdout + "\n" + completed.stderr)
    if any(value is not None for value in test_summary.values()):
        result["test_summary"] = test_summary
    if completed.returncode != 0:
        raise SmokeError(
            f"{label} failed with exit code {completed.returncode}: "
            f"{completed.stderr[-3000:] or completed.stdout[-3000:]}"
        )
    return result


def safe_extract(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    destination_root = destination.resolve()
    with ZipFile(archive_path) as archive:
        for member in archive.infolist():
            name = PurePosixPath(member.filename)
            if name.is_absolute() or ".." in name.parts:
                raise SmokeError(f"archive member escapes destination: {member.filename}")
            mode = (member.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise SmokeError(f"archive contains a symbolic link: {member.filename}")
            target = (destination / Path(*name.parts)).resolve()
            if target != destination_root and destination_root not in target.parents:
                raise SmokeError(f"archive member escapes destination: {member.filename}")
        archive.extractall(destination)


def clean_source_exclusion_snapshot(source_root: Path) -> dict[str, bool]:
    candidates = {
        ".git": source_root / ".git",
        "runtime": source_root / "runtime",
        "node_modules": source_root / "frontend" / "node_modules",
        "dist": source_root / "frontend" / "dist",
    }
    return {name: not path.exists() for name, path in candidates.items()}


def fetch(opener: Any, base_url: str, path: str) -> dict[str, Any]:
    try:
        with opener.open(base_url + path, timeout=5) as response:
            body = response.read()
            return {
                "status": response.status,
                "content_type": response.headers.get("Content-Type", ""),
                "body": body.decode("utf-8", "replace"),
            }
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return {
            "status": exc.code,
            "content_type": exc.headers.get("Content-Type", ""),
            "body": body.decode("utf-8", "replace"),
        }


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeError(message)


def parse_test_summary(output: str) -> dict[str, int | None]:
    def values(name: str) -> list[int]:
        return [
            int(value)
            for value in re.findall(
                rf"(?:^|\n)[^A-Za-z0-9\r\n]*{name}\s+(\d+)",
                output,
            )
        ]

    ran = re.findall(r"Ran\s+(\d+)\s+tests?", output)
    tests = values("tests")
    passed = values("pass")
    failed = values("fail")
    test_count = sum(tests) if tests else (int(ran[-1]) if ran else None)
    pass_count = sum(passed) if passed else None
    fail_count = sum(failed) if failed else None
    if test_count is not None and re.search(r"(?:^|\n)OK(?:\r?\n|$)", output):
        if pass_count is None:
            pass_count = test_count
        if fail_count is None:
            fail_count = 0
    return {
        "tests": test_count,
        "pass": pass_count,
        "fail": fail_count,
    }


def managed_python(runtime_root: Path) -> Path:
    if os.name == "nt":
        return runtime_root / "python" / "Scripts" / "python.exe"
    return runtime_root / "python" / "bin" / "python"


def run_smoke(source_root: Path) -> dict[str, Any]:
    protected_before = protected_port_state()
    if any(protected_before.values()):
        raise SmokeError(f"protected port already has a listener: {protected_before}")
    formal_before = formal_database_state(source_root)
    work_path: Path | None = None
    server_process: subprocess.Popen[bytes] | None = None
    with tempfile.TemporaryDirectory(
        prefix="ai-studio-fresh-source-",
        dir=tempfile.gettempdir(),
    ) as work_text:
        work_path = Path(work_text).resolve()
        archive_root = work_path / "archive"
        archive_root.mkdir()
        source_copy = work_path / "source"
        bootstrap_runtime = work_path / "bootstrap-runtime"
        bootstrap_report = work_path / "bootstrap-report.json"
        startup_runtime = work_path / "startup-runtime"
        startup_runtime.mkdir()
        application_environment = isolated_app_environment()
        dependency_environment = os.environ.copy()
        dependency_environment["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
        for name in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "DEEPSEEK_API_KEY",
            "GOOGLE_API_KEY",
            "GEMINI_API_KEY",
            "FUTU_PASSWORD_MD5",
        ):
            dependency_environment.pop(name, None)

        backup_create = run_command(
            "create_verified_source_archive",
            [
                sys.executable,
                str(source_root / "scripts" / "create_versioned_source_backup.py"),
                "create",
                "--source-root",
                str(source_root),
                "--source-root-label",
                "ai_collaboration_studio_fresh_source",
                "--destination-root",
                str(archive_root),
            ],
            cwd=source_root,
            environment=application_environment,
            timeout=300,
        )
        archives = sorted(archive_root.rglob("*.zip"))
        require(len(archives) == 1, f"expected one source archive, found {len(archives)}")
        archive_path = archives[0]
        backup_verify = run_command(
            "verify_source_archive_offline",
            [
                sys.executable,
                str(source_root / "scripts" / "create_versioned_source_backup.py"),
                "verify",
                str(archive_path),
            ],
            cwd=source_root,
            environment=application_environment,
            timeout=300,
        )
        safe_extract(archive_path, source_copy)
        exclusions_before_bootstrap = clean_source_exclusion_snapshot(source_copy)
        require(
            all(exclusions_before_bootstrap.values()),
            "clean source archive contains excluded generated state",
        )

        bootstrap_command = run_command(
            "bootstrap_clean_source",
            [
                sys.executable,
                str(source_copy / "scripts" / "bootstrap_ai_collaboration_studio.py"),
                "--project-root",
                str(source_copy),
                "--runtime-root",
                str(bootstrap_runtime),
                "--report",
                str(bootstrap_report),
                "--allow-dependency-downloads",
            ],
            cwd=source_copy,
            environment=dependency_environment,
            timeout=2100,
        )
        require(bootstrap_report.is_file(), "bootstrap did not produce its receipt")
        bootstrap_receipt = json.loads(bootstrap_report.read_text(encoding="utf-8"))
        python_path = managed_python(bootstrap_runtime)
        require(python_path.is_file(), "managed Python was not created")

        backend_test = run_command(
            "targeted_backend_contracts",
            [
                str(python_path),
                "scripts/run_backend_tests_isolated.py",
                "--start-directory",
                "tests",
                "--pattern",
                "test_host_delivery_endpoints.py",
                "--verbosity",
                "1",
            ],
            cwd=source_copy,
            environment=application_environment,
            timeout=300,
        )
        npm_path = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        require(bool(npm_path), "npm disappeared after bootstrap")
        frontend_test = run_command(
            "guarded_frontend_regression",
            [str(npm_path), "--prefix", "frontend", "test"],
            cwd=source_copy,
            environment=application_environment,
            timeout=1200,
        )

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        database_path = startup_runtime / "collaboration_studio.sqlite3"
        server_environment = application_environment.copy()
        server_environment.update(
            {
                "AI_STUDIO_RUNTIME_DIR": str(startup_runtime),
                "AI_STUDIO_DATABASE_PATH": str(database_path),
                "AI_STUDIO_HOST": "127.0.0.1",
                "AI_STUDIO_PORT": str(port),
                "PYTHONUNBUFFERED": "1",
            }
        )
        initialize_database = run_command(
            "initialize_temporary_database",
            [
                str(python_path),
                "-c",
                "from pathlib import Path; from backend.store import StudioStore; "
                "StudioStore(Path(__import__('os').environ['AI_STUDIO_DATABASE_PATH']))",
            ],
            cwd=source_copy,
            environment=server_environment,
            timeout=180,
        )
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        stdout_data = b""
        stderr_data = b""
        try:
            server_process = subprocess.Popen(
                [str(python_path), "server.py"],
                cwd=source_copy,
                env=server_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base_url = f"http://127.0.0.1:{port}"
            readiness = None
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if server_process.poll() is not None:
                    break
                try:
                    readiness = fetch(opener, base_url, "/api/readiness")
                    if readiness["status"] == 200:
                        break
                except (OSError, urllib.error.URLError):
                    pass
                time.sleep(0.25)
            require(readiness is not None and readiness["status"] == 200, "clean source server did not become ready")
            version = fetch(opener, base_url, "/api/version")
            frontend = fetch(opener, base_url, "/")
            unknown = fetch(opener, base_url, "/api/not-a-real-endpoint")
            readiness_json = json.loads(readiness["body"])
            version_json = json.loads(version["body"])
            unknown_json = json.loads(unknown["body"])
            require(readiness_json.get("ready") is True, "readiness contract is not ready")
            require(version_json.get("schema_version") == "host_version_v2", "version contract drifted")
            require(frontend["status"] == 200 and frontend["content_type"].startswith("text/html"), "production frontend was not served")
            require(unknown["status"] == 404 and unknown_json.get("error_code") == "API_NOT_FOUND", "unknown API fallback regressed")
            runtime_result = {
                "host": "127.0.0.1",
                "port": port,
                "readiness": {
                    "status": readiness["status"],
                    "schema_version": readiness_json["schema_version"],
                    "ready": readiness_json["ready"],
                },
                "version": {
                    "status": version["status"],
                    "schema_version": version_json["schema_version"],
                    "service": version_json["service"],
                    "frontend_build": version_json["frontend_build"],
                },
                "frontend_status": frontend["status"],
                "unknown_api_status": unknown["status"],
                "listener_while_running": port_open(port),
                "protected_ports_while_running": protected_port_state(),
            }
        finally:
            if server_process is not None:
                if server_process.poll() is None:
                    server_process.terminate()
                try:
                    stdout_data, stderr_data = server_process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    server_process.kill()
                    stdout_data, stderr_data = server_process.communicate(timeout=10)
                server_process = None
        runtime_result["listener_after_stop"] = port_open(port)
        runtime_result["server_stdout_bytes"] = len(stdout_data)
        runtime_result["server_stderr_bytes"] = len(stderr_data)
        with ZipFile(archive_path) as archive:
            archive_entry_count = len(archive.infolist())
        verify_payload = json.loads(backup_verify["stdout_tail"])
        result = {
            "version": SMOKE_VERSION,
            "source_archive": {
                "archive_entries": archive_entry_count,
                "verified_manifest_file_count": verify_payload["file_count"],
                "verified_total_bytes": verify_payload["total_size"],
                "verified_total_sha256": verify_payload["total_sha256"],
                "zip_bytes": archive_path.stat().st_size,
                "zip_sha256": sha256_file(archive_path),
                "create": backup_create,
                "verify": backup_verify,
            },
            "clean_source_exclusions_before_bootstrap": exclusions_before_bootstrap,
            "bootstrap": {
                "receipt_version": bootstrap_receipt["receipt_version"],
                "project": bootstrap_receipt["project"],
                "inputs": bootstrap_receipt["inputs"],
                "python_resolution": bootstrap_receipt["python_resolution"],
                "frontend_build": bootstrap_receipt["frontend_build"],
                "command": bootstrap_command,
            },
            "backend_tests": {
                **backend_test["test_summary"],
                "command": backend_test,
            },
            "frontend_tests": {
                **frontend_test["test_summary"],
                "command": frontend_test,
            },
            "database_initialization": initialize_database,
            "runtime": runtime_result,
            "protected_ports_before": protected_before,
        }
    require(work_path is not None and not work_path.exists(), "temporary clean-source workdir was not deleted")
    formal_after = formal_database_state(source_root)
    result["temporary_workdir_deleted"] = True
    result["formal_database_unchanged"] = formal_before == formal_after
    result["formal_database_after"] = formal_after
    result["protected_ports_after"] = protected_port_state()
    require(result["formal_database_unchanged"], "formal database state changed")
    require(not any(result["protected_ports_after"].values()), "protected listener remained")
    return result


def write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise SmokeError(f"report already exists: {path}")
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a clean source archive through install, test, build, and local startup"
    )
    parser.add_argument("--source-root")
    parser.add_argument("--report")
    parser.add_argument("--allow-dependency-downloads", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if not arguments.allow_dependency_downloads:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "clean-source smoke requires --allow-dependency-downloads",
                }
            ),
            file=sys.stderr,
        )
        return 1
    source_root = Path(
        arguments.source_root or Path(__file__).resolve().parents[1]
    ).expanduser().resolve()
    report_path = Path(arguments.report).expanduser().resolve() if arguments.report else (
        Path(tempfile.gettempdir())
        / f"ai-studio-fresh-source-smoke-{uuid4().hex}.json"
    )
    try:
        result = run_smoke(source_root)
        write_report(report_path, result)
    except (SmokeError, OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True), file=sys.stderr)
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
