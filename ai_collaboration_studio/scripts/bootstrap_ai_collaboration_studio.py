from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


BOOTSTRAP_RECEIPT_VERSION = "studio_bootstrap_receipt_v1"
PROJECT_ID = "ai_collaboration_studio"
REPARSE_POINT_ATTRIBUTE = 0x400
PROVIDER_ENVIRONMENT_NAMES = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "FUTU_PASSWORD_MD5",
)
PROJECT_MARKERS = (
    "server.py",
    "requirements.txt",
    "requirements-lock-win-py314.txt",
    "frontend/package.json",
    "frontend/package-lock.json",
)


class BootstrapError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value.strip()).lower()


def parse_hashed_lock(path: Path) -> dict[str, str]:
    requirements: dict[str, str] = {}
    pattern = re.compile(
        r"^([A-Za-z0-9_.-]+)==([^\s]+)\s+--hash=sha256:([0-9a-f]{64})$"
    )
    for line_number, raw_line in enumerate(
        path.read_text(encoding="ascii").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = pattern.fullmatch(line)
        if match is None:
            raise BootstrapError(
                f"invalid fully-hashed lock entry at line {line_number}"
            )
        name = canonical_distribution_name(match.group(1))
        if name in requirements:
            raise BootstrapError(f"duplicate distribution in lock: {name}")
        requirements[name] = match.group(2)
    if not requirements:
        raise BootstrapError("Python dependency lock is empty")
    return requirements


def parse_frozen_requirements(lines: list[str]) -> dict[str, str]:
    requirements: dict[str, str] = {}
    for line in lines:
        if "==" not in line:
            raise BootstrapError(f"unexpected pip freeze entry: {line}")
        name, version = line.split("==", 1)
        canonical_name = canonical_distribution_name(name)
        if canonical_name in requirements:
            raise BootstrapError(
                f"duplicate distribution in pip freeze: {canonical_name}"
            )
        requirements[canonical_name] = version
    return requirements


def python_lock_profile() -> dict[str, Any]:
    machine = platform.machine().strip().lower()
    compatible = bool(
        sys.platform == "win32"
        and sys.version_info[:2] == (3, 14)
        and machine in {"amd64", "x86_64"}
    )
    return {
        "id": "windows_x64_cpython_3_14",
        "compatible": compatible,
        "host_platform": sys.platform,
        "host_python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "host_machine": machine,
    }


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def first_reparse_component(path: Path) -> Path | None:
    absolute = _absolute_path(path)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            details = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(details.st_mode) or (
            getattr(details, "st_file_attributes", 0) & REPARSE_POINT_ATTRIBUTE
        ):
            return current
    return None


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def resolve_project_root(value: str | Path | None) -> Path:
    candidate = _absolute_path(
        Path(value) if value is not None else Path(__file__).resolve().parents[1]
    )
    offending = first_reparse_component(candidate)
    if offending is not None:
        raise BootstrapError(f"project root contains a reparse point: {offending}")
    root = candidate.resolve()
    if not root.is_dir():
        raise BootstrapError(f"project root does not exist: {root}")
    missing = [marker for marker in PROJECT_MARKERS if not (root / marker).is_file()]
    if missing:
        raise BootstrapError(
            "project root is missing required files: " + ", ".join(missing)
        )
    return root


def resolve_runtime_root(project_root: Path, value: str | Path | None) -> Path:
    candidate = _absolute_path(
        Path(value)
        if value is not None
        else project_root / "runtime" / "bootstrap"
    )
    offending = first_reparse_component(candidate)
    if offending is not None:
        raise BootstrapError(f"runtime root contains a reparse point: {offending}")
    runtime_root = candidate.resolve(strict=False)
    project_runtime = (project_root / "runtime").resolve(strict=False)
    system_temp = Path(tempfile.gettempdir()).resolve()
    if is_relative_to(runtime_root, project_root) and not is_relative_to(
        runtime_root,
        project_runtime,
    ):
        raise BootstrapError(
            "runtime root inside the source tree must remain under project/runtime"
        )
    if not (
        is_relative_to(runtime_root, project_runtime)
        or is_relative_to(runtime_root, system_temp)
    ):
        raise BootstrapError(
            "runtime root must be under project/runtime or the system temp directory"
        )
    return runtime_root


def managed_python_path(runtime_root: Path) -> Path:
    if os.name == "nt":
        return runtime_root / "python" / "Scripts" / "python.exe"
    return runtime_root / "python" / "bin" / "python"


def safe_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    environment["PIP_NO_INPUT"] = "1"
    environment["npm_config_audit"] = "false"
    environment["npm_config_fund"] = "false"
    for name in PROVIDER_ENVIRONMENT_NAMES:
        environment.pop(name, None)
    return environment


def command_result(
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
        "stdout_tail": completed.stdout[-12000:],
        "stderr_tail": completed.stderr[-12000:],
    }
    if completed.returncode != 0:
        raise BootstrapError(
            f"{label} failed with exit code {completed.returncode}: "
            f"{completed.stderr[-2000:] or completed.stdout[-2000:]}"
        )
    return result


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def bootstrap(
    *,
    project_root: Path,
    runtime_root: Path,
    allow_dependency_downloads: bool,
    check_only: bool,
    report_path: Path | None,
) -> dict[str, Any]:
    npm_name = "npm.cmd" if os.name == "nt" else "npm"
    npm_path = shutil.which(npm_name)
    if not npm_path:
        raise BootstrapError("npm was not found on PATH")
    frontend = project_root / "frontend"
    requirements = project_root / "requirements.txt"
    requirements_lock = project_root / "requirements-lock-win-py314.txt"
    package_json = frontend / "package.json"
    package_lock = frontend / "package-lock.json"
    package = json.loads(package_json.read_text(encoding="utf-8"))
    locked_requirements = parse_hashed_lock(requirements_lock)
    lock_profile = python_lock_profile()
    plan = {
        "receipt_version": BOOTSTRAP_RECEIPT_VERSION,
        "project": {
            "id": PROJECT_ID,
            "version": str(package.get("version") or ""),
        },
        "project_root": str(project_root),
        "runtime_root": str(runtime_root),
        "inputs": {
            "requirements_sha256": sha256_file(requirements),
            "requirements_lock_sha256": sha256_file(requirements_lock),
            "package_json_sha256": sha256_file(package_json),
            "package_lock_sha256": sha256_file(package_lock),
            "python_requirements_fully_pinned": True,
            "python_locked_distribution_count": len(locked_requirements),
            "python_lock_profile": lock_profile,
        },
        "tools": {
            "bootstrap_python": sys.executable,
            "npm": npm_path,
        },
        "safety": {
            "local_env_skipped": True,
            "provider_credentials_removed": True,
            "application_started": False,
            "formal_database_opened": False,
        },
        "check_only": check_only,
        "dependency_downloads_authorized": allow_dependency_downloads,
    }
    if check_only:
        return plan
    if not allow_dependency_downloads:
        raise BootstrapError(
            "dependency installation requires --allow-dependency-downloads"
        )
    if not lock_profile["compatible"]:
        raise BootstrapError(
            "the available Python dependency lock requires Windows x64 CPython 3.14"
        )

    runtime_root.mkdir(parents=True, exist_ok=True)
    environment = safe_environment()
    python_path = managed_python_path(runtime_root)
    commands: list[dict[str, Any]] = []
    if not python_path.is_file():
        commands.append(
            command_result(
                "create_python_venv",
                [sys.executable, "-m", "venv", str(runtime_root / "python")],
                cwd=project_root,
                environment=environment,
                timeout=180,
            )
        )
    commands.append(
        command_result(
            "install_python_dependencies",
            [
                str(python_path),
                "-m",
                "pip",
                "install",
                "--no-input",
                "--require-hashes",
                "--requirement",
                str(requirements_lock),
            ],
            cwd=project_root,
            environment=environment,
            timeout=900,
        )
    )
    commands.append(
        command_result(
            "install_frontend_dependencies",
            [
                npm_path,
                "ci",
                "--cache",
                str(runtime_root / "npm-cache"),
                "--no-audit",
                "--no-fund",
            ],
            cwd=frontend,
            environment=environment,
            timeout=900,
        )
    )
    commands.append(
        command_result(
            "build_frontend_production",
            [npm_path, "run", "build"],
            cwd=frontend,
            environment=environment,
            timeout=600,
        )
    )
    freeze = command_result(
        "capture_python_resolution",
        [str(python_path), "-m", "pip", "freeze"],
        cwd=project_root,
        environment=environment,
        timeout=120,
    )
    commands.append(freeze)
    index_path = frontend / "dist" / "index.html"
    if not index_path.is_file():
        raise BootstrapError("production frontend index was not created")
    frozen_requirements = freeze["stdout_tail"].strip().splitlines()
    frozen_resolution = parse_frozen_requirements(frozen_requirements)
    if frozen_resolution != locked_requirements:
        raise BootstrapError(
            "managed Python resolution differs from the fully-hashed lock"
        )
    receipt = {
        **plan,
        "check_only": False,
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "managed_python": str(python_path),
        "python_resolution": {
            "packages": frozen_requirements,
            "matches_lock": True,
            "sha256": hashlib.sha256(
                ("\n".join(frozen_requirements) + "\n").encode("utf-8")
            ).hexdigest(),
        },
        "frontend_build": {
            "index_bytes": index_path.stat().st_size,
            "index_sha256": sha256_file(index_path),
        },
        "commands": commands,
    }
    default_receipt = runtime_root / "bootstrap-receipt.json"
    write_json(default_receipt, receipt)
    if report_path is not None and report_path.resolve() != default_receipt.resolve():
        write_json(report_path.resolve(), receipt)
    receipt["receipt_path"] = str(default_receipt)
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install isolated local dependencies and build the production frontend"
    )
    parser.add_argument("--project-root")
    parser.add_argument("--runtime-root")
    parser.add_argument("--report")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--allow-dependency-downloads", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        project_root = resolve_project_root(arguments.project_root)
        runtime_root = resolve_runtime_root(project_root, arguments.runtime_root)
        result = bootstrap(
            project_root=project_root,
            runtime_root=runtime_root,
            allow_dependency_downloads=arguments.allow_dependency_downloads,
            check_only=arguments.check_only,
            report_path=Path(arguments.report) if arguments.report else None,
        )
    except (BootstrapError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc)},
                ensure_ascii=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
