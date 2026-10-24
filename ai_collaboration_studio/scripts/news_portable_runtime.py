"""Boundaries shared by the portable news entry and clean-checkout verifier.

This module is stdlib-only and never imports the application on import.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import subprocess
from unittest.mock import patch

DEFAULT_PORT = 8780
PROTECTED_PORTS = frozenset({8770, 11111, 18787})
DATABASE_NAMES = ("collaboration_studio.sqlite3", "news_platform.sqlite3", "news_analysis.sqlite3")
PROVIDER_ENVIRONMENT_NAMES = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "GOOGLE_API_KEY",
    "GEMINI_API_KEY", "ARK_API_KEY", "DOUBAO_API_KEY", "QWEN_API_KEY",
    "DASHSCOPE_API_KEY", "GLM_API_KEY", "ZHIPU_API_KEY", "ZHIPUAI_API_KEY",
    "FUTU_PASSWORD_MD5", "AI_STUDIO_PROJECT_CAPABILITY_SIGNING_SECRET",
    "AI_STUDIO_MCP_TOKEN_SIGNING_SECRET",
)
DISABLED_PROVIDERS = "openai,anthropic,deepseek,doubao,ark,qwen,glm,zhipu,google,gemini"
EXPECTED_TOOLCHAIN = {"node": "v24.16.0", "npm": "11.13.0"}


class PortableError(RuntimeError):
    pass


def safe_environment(base=None):
    environment = dict(os.environ if base is None else base)
    secrets = {name.upper() for name in PROVIDER_ENVIRONMENT_NAMES}
    for name in tuple(environment):
        upper = name.upper()
        if (upper in secrets or upper.startswith("AI_STUDIO_")
                or upper in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
                or upper in {"PYTHONPATH", "PYTHONHOME"}):
            environment.pop(name, None)
    environment.update({
        "AI_STUDIO_SKIP_LOCAL_ENV": "1", "AI_STUDIO_DISABLED_PROVIDERS": DISABLED_PROVIDERS,
        "FUTU_HOST": "127.0.0.1", "FUTU_PORT": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1",
        "npm_config_audit": "false", "npm_config_fund": "false",
    })
    return environment


def unaliased_path(value):
    path = Path(os.path.abspath(os.fspath(Path(value).expanduser())))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise PortableError("path_alias_refused")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise PortableError("file_hardlink_refused")
    return path


def data_root(project, value=None):
    root = unaliased_path(value if value is not None else Path(project) / "runtime" / "news-workspace")
    if root == Path(project).resolve() or root == Path(project).resolve().parent:
        raise PortableError("data_root_must_be_separate")
    return root


def validate_port(port):
    if type(port) is not int or not 1024 <= port <= 65535 or port in PROTECTED_PORTS:
        raise PortableError("port_must_be_unprivileged_and_not_protected")
    return port


def reserve_port(port):
    validate_port(port)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
    except OSError:
        listener.close()
        raise PortableError("port_unavailable_no_existing_process_touched") from None
    return listener


def require_empty_data(root):
    root = unaliased_path(root)
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise PortableError("initialize_new_requires_empty_data_directory")
    return root


NEWS_COLUMNS = {
    "settings": "key value",
    "sources": "id name url enabled status last_success last_attempt next_poll error failures etag last_modified not_before cooldown_level recovery_successes access_denied_level access_recovery_successes cooldown_reason manual_hold refresh_queued rate_recovery_not_before",
    "items": "id source_id identity title url excerpt published discovered updated read analysis",
    "monitor_sessions": "id started heartbeat stopped state stop_reason",
    "monitor_gaps": "id session_id kind started ended",
    "poll_runs": "id session_id source_id started finished outcome http_status parsed_items new_items updated_items error_code trigger_kind schedule_basis diagnostics",
}
NEWS_INDEXES = {"items_discovered", "items_published", "monitor_sessions_started", "monitor_gaps_ended", "poll_runs_started", "poll_runs_source_started", "poll_runs_outcome"}


def closed_database(path):
    path = unaliased_path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise PortableError("existing_database_missing_or_empty")
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise PortableError("database_sidecars_require_explicit_recovery")
    return path


def preflight_news_schema(path, *, analysis=False):
    path = closed_database(path)
    expected = {"runs": "fingerprint item_id created reserved status result"} if analysis else NEWS_COLUMNS
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        app_id = db.execute("PRAGMA application_id").fetchone()[0]
        if app_id != (0x4E414931 if analysis else 0x4E455753):
            raise PortableError("database_application_id_mismatch")
        for table, columns in expected.items():
            actual = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
            if not set(columns.split()).issubset(actual):
                raise PortableError("news_schema_requires_explicit_migration")
        if not analysis:
            indexes = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            if not NEWS_INDEXES.issubset(indexes):
                raise PortableError("news_indexes_require_explicit_migration")
            row = db.execute("SELECT value FROM settings WHERE key='enabled'").fetchone()
            if row is None or row[0] not in {"true", "false"}:
                raise PortableError("news_settings_require_explicit_migration")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise PortableError("database_integrity_check_failed")
    return {"schema_ready": True, "readonly": True, "application_id": app_id}


def backend_identity(project):
    project = Path(project)
    paths = [project / "server.py", *(project / "backend").rglob("*.py")]
    digest = hashlib.sha256()
    for name in sorted(path.relative_to(project).as_posix() for path in paths):
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256((project / name).read_bytes()).hexdigest().encode("ascii") + b"\n")
    return digest.hexdigest()


def verify_toolchain(node, npm, *, environment, cwd, runner=subprocess.run):
    versions = {}
    for name, executable in (("node", node), ("npm", npm)):
        try:
            completed = runner([str(executable), "--version"], cwd=cwd, env=environment,
                               capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            raise PortableError(f"{name}_toolchain_version_unavailable") from None
        observed = completed.stdout.strip()
        if completed.returncode != 0 or observed != EXPECTED_TOOLCHAIN[name]:
            raise PortableError(f"{name}_toolchain_version_mismatch")
        versions[name] = observed
    return {"expected": dict(EXPECTED_TOOLCHAIN), "observed": versions, "matches": True}


@contextmanager
def offline_guard():
    """No outgoing connection, DNS, datagram or child-process escape.

    Listening and replies on accepted sockets remain available for local GET
    verification. This is an application acceptance guard, not an OS sandbox.
    """
    def denied(*args, **kwargs):
        raise PortableError("offline_acceptance_outbound_blocked")

    def local_name(address):
        if address in {"127.0.0.1", "::1", "localhost"}:
            return ("localhost", [], [address])
        return denied()

    with ExitStack() as stack:
        for name in ("connect", "connect_ex", "sendto", "sendmsg"):
            if not hasattr(socket.socket, name):
                continue
            stack.enter_context(patch.object(socket.socket, name, denied))
        for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex"):
            stack.enter_context(patch.object(socket, name, denied))
        stack.enter_context(patch.object(socket, "gethostbyaddr", local_name))
        stack.enter_context(patch.object(subprocess, "Popen", denied))
        stack.enter_context(patch.object(os, "system", denied))
        yield


def write_receipt(path, payload):
    path = unaliased_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
