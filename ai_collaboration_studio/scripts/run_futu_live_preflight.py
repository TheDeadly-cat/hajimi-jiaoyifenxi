"""Emit one bounded JSON-only Futu/OpenD live-preflight report."""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence, TextIO


PREFLIGHT_VERSION = "futu_live_preflight_v1"
PREFLIGHT_CONFIRMATION = "RUN_FUTU_LIVE_PREFLIGHT_ONCE"
MAX_OUTPUT_BYTES = 16_384
WATCHDOG_TIMEOUT_SECONDS = 15
_WORKER_FLAG = "--futu-live-preflight-internal-worker"
_WORKER_TOKEN_ENV = "AI_STUDIO_FUTU_PREFLIGHT_WORKER_TOKEN"
_IMPORT_GUARD_ENV = "AI_STUDIO_FUTU_PREFLIGHT_IMPORT_GUARD"
_IMPORT_GUARD_VALUE = "futu-live-preflight-isolated-import-v1"
_WATCHDOG_GUARD_ENV = "AI_STUDIO_FUTU_PREFLIGHT_WATCHDOG_GUARD"
_WATCHDOG_GUARD_VALUE = "futu-live-preflight-watchdog-v1"
_SDK_PROFILE_DIRNAME = "futu-sdk-profile"
_FIXED_SYMBOLS = ("US.MU", "US.SNDK", "US.WDC", "US.STX")
_FIXED_HOST = "127.0.0.1"
_FIXED_PORT = 11111
_FIXED_SDK_DISTRIBUTION = "futu-api"
_FIXED_SDK_VERSION = "10.10.7008"
_FIXED_BROKER_POLICY_SHA256 = (
    "48bb9c5b2e669eb86e947d57a76c4085ea155f52a16fbdc33228fab54ce1828e"
)

# The worker receives an allowlisted environment rather than a copy of the
# operator's session.  These variables are sufficient for the installed
# Windows Python runtime and temporary-directory handling; application,
# cloud, source-control, proxy, and database credentials are not inherited.
_WORKER_PARENT_ENV_ALLOWLIST = frozenset({
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "WINDIR",
})

_CREDENTIAL_AND_CONFIG_ENV = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "ARK_API_KEY",
    "ARK_BASE_URL",
    "ARK_MODEL",
    "GLM_API_KEY",
    "ZHIPUAI_API_KEY",
    "GLM_BASE_URL",
    "GLM_MODEL",
    "AI_STUDIO_DEFAULT_PROVIDER",
    "AI_STUDIO_DISABLED_PROVIDERS",
    "AI_STUDIO_PROJECT_CAPABILITY_SIGNING_SECRET",
    "AI_STUDIO_HOST",
    "AI_STUDIO_PORT",
    "SEC_USER_AGENT",
    "SEC_CACHE_TTL_SECONDS",
    "FUTU_HOST",
    "FUTU_PORT",
    "FUTU_CACHE_TTL_SECONDS",
)


class _ArgumentsInvalid(ValueError):
    pass


class _WorkerReportInvalid(ValueError):
    pass


class _WorkerStartFailed(OSError):
    pass


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


_SOURCE_MANIFEST_SHA256 = _canonical_sha256({
    "version": "futu_live_preflight_source_manifest_v2",
    "host_policy": "fixed_ipv4_loopback_literal_v1",
    "port": _FIXED_PORT,
    "symbols": list(_FIXED_SYMBOLS),
    "quote_batch_calls": 1,
    "broker_mode": "one_shot",
    "broker_protocol": "futu_readonly_broker_v1",
    "broker_policy_sha256": _FIXED_BROKER_POLICY_SHA256,
    "sdk_profile_policy": "parent_temp_appdata_v1",
    "sdk_import_failure_policy": "closed_worker_receipt_v1",
    "sdk_python_stdio_policy": "devnull_during_import_and_calls_v1",
    "sdk_logical_call_upper_bounds": {
        "socket_probe": 1,
        "quote_context_open": 1,
        "get_market_snapshot": 1,
        "get_market_state": 1,
        "close": 1,
    },
    "execution_capability": "none",
    "live_trading_allowed": False,
})
_WORKER_EVIDENCE_PROFILE_SHA256 = _canonical_sha256({
    "version": PREFLIGHT_VERSION,
    "evidence_class": "watchdog_worker_observation",
    "transport": "futu_readonly_broker_one_shot_v1",
    "sdk_distribution": _FIXED_SDK_DISTRIBUTION,
    "sdk_version": _FIXED_SDK_VERSION,
    "source_manifest_sha256": _SOURCE_MANIFEST_SHA256,
    "watchdog_parent_promoted": False,
    "live_network_attested": False,
    "in_process_tamper_resistant": False,
    "dependency_file_integrity_attested": False,
})
_PRODUCTION_EVIDENCE_PROFILE_SHA256 = _canonical_sha256({
    "version": PREFLIGHT_VERSION,
    "evidence_class": "production_path_observation",
    "transport": "futu_readonly_broker_one_shot_v1",
    "sdk_distribution": _FIXED_SDK_DISTRIBUTION,
    "sdk_version": _FIXED_SDK_VERSION,
    "source_manifest_sha256": _SOURCE_MANIFEST_SHA256,
    "watchdog_parent_promoted": True,
    "live_network_attested": False,
    "in_process_tamper_resistant": False,
    "dependency_file_integrity_attested": False,
})


class _BoundedArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise _ArgumentsInvalid("invalid Futu live-preflight arguments")

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if status:
            raise _ArgumentsInvalid(message or "invalid arguments")


def _zero_calls() -> dict[str, int]:
    return {
        "quote_batch_attempt_count": 0,
        "quote_batch_return_count": 0,
        "socket_probe_attempt_count": 0,
        "socket_probe_success_count": 0,
        "quote_context_open_attempt_count": 0,
        "quote_context_open_success_count": 0,
        "snapshot_call_attempt_count": 0,
        "snapshot_call_return_count": 0,
        "market_state_call_attempt_count": 0,
        "market_state_call_return_count": 0,
        "close_attempt_count": 0,
        "close_success_count": 0,
    }


def _error_payload(
    code: str,
    *,
    confirmation_verified: bool = False,
    watchdog_enforced: bool = False,
    indeterminate: bool = False,
) -> dict[str, Any]:
    return {
        "version": PREFLIGHT_VERSION,
        "scope": "futu_storage_quotes_only",
        "ok": False,
        "status": "indeterminate" if indeterminate else "not_started",
        "error_code": code,
        "calls": _zero_calls(),
        "safety": {
            "read_only": None if indeterminate else True,
            "one_shot": None if indeterminate else True,
            "confirmation_required": True,
            "confirmation_verified": confirmation_verified,
            "watchdog_required": True,
            "watchdog_enforced": watchdog_enforced,
            "worker_process_tree_termination_attested": False,
            "loopback_only": None if indeterminate else True,
            "network_requests_performed": None,
            "network_requests_accounting": "unknown",
            "application_file_writes_performed": None if indeterminate else 0,
            "third_party_filesystem_activity_attested": False,
            "database_reads_performed": None if indeterminate else 0,
            "database_writes_performed": None if indeterminate else 0,
            "provider_calls_performed": None if indeterminate else 0,
            "account_calls_performed": None if indeterminate else 0,
            "order_calls_performed": None if indeterminate else 0,
            "trade_calls_performed": None if indeterminate else 0,
            "execution_capability": "unknown" if indeterminate else "none",
            "live_trading_allowed": None if indeterminate else False,
            "opend_started": None if indeterminate else False,
            "login_performed": None if indeterminate else False,
        },
    }


def _help_payload() -> dict[str, Any]:
    return {
        "version": PREFLIGHT_VERSION,
        "scope": "futu_storage_quotes_only",
        "ok": True,
        "status": "help",
        "required_confirmation": PREFLIGHT_CONFIRMATION,
        "fixed_target": "127.0.0.1:11111",
        "fixed_symbols": ["US.MU", "US.SNDK", "US.WDC", "US.STX"],
        "safety": _error_payload("HELP")["safety"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = _BoundedArgumentParser(
        prog="python -I -B scripts/run_futu_live_preflight.py",
        add_help=False,
        allow_abbrev=False,
    )
    parser.add_argument("-h", "--help", action="store_true")
    parser.add_argument("--confirm", default=None)
    return parser


def _parse(argv: Sequence[str]) -> tuple[str, str]:
    raw = list(argv)
    if any(type(value) is not str for value in raw):
        raise _ArgumentsInvalid("arguments must be native strings")
    help_count = sum(value in {"-h", "--help"} for value in raw)
    confirm_count = sum(
        value == "--confirm" or value.startswith("--confirm=") for value in raw
    )
    if help_count:
        if len(raw) == 1 and help_count == 1:
            return "help", ""
        raise _ArgumentsInvalid("help must be the only argument")
    if confirm_count > 1:
        raise _ArgumentsInvalid("confirmation must be supplied once")
    parsed = _parser().parse_args(raw)
    confirmation = parsed.confirm if type(parsed.confirm) is str else ""
    return "run", confirmation


def _bounded_json(payload: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"
    except (TypeError, ValueError):
        encoded = ""
    if not encoded or len(encoded.encode("ascii")) >= MAX_OUTPUT_BYTES:
        safety = payload.get("safety") if type(payload) is dict else None
        encoded = json.dumps(
            _error_payload(
                "PREFLIGHT_OUTPUT_BOUND_EXCEEDED",
                confirmation_verified=(
                    type(safety) is dict
                    and safety.get("confirmation_verified") is True
                ),
                watchdog_enforced=(
                    type(safety) is dict
                    and safety.get("watchdog_enforced") is True
                ),
                indeterminate=True,
            ),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"
    return encoded


def _emit(output: TextIO, payload: dict[str, Any]) -> None:
    output.write(_bounded_json(payload))


def _backend_or_futu_modules_preloaded() -> bool:
    return any(
        name == "backend"
        or name.startswith("backend.")
        or name == "futu"
        or name.startswith("futu.")
        for name in tuple(sys.modules)
    )


def _sanitized_worker_environment(
    project_root: Path,
    token: str,
    *,
    sdk_profile_root: Path | None = None,
) -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in _WORKER_PARENT_ENV_ALLOWLIST
    }
    environment["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
    environment["AI_STUDIO_RUNTIME_DIR"] = str(project_root)
    environment["AI_STUDIO_DATABASE_PATH"] = str(
        project_root / ".futu-live-preflight-database-must-not-open.sqlite3"
    )
    environment[_IMPORT_GUARD_ENV] = _IMPORT_GUARD_VALUE
    environment[_WATCHDOG_GUARD_ENV] = _WATCHDOG_GUARD_VALUE
    environment[_WORKER_TOKEN_ENV] = token
    if sdk_profile_root is not None:
        if sdk_profile_root.is_symlink():
            raise ValueError("SDK profile root cannot be a symlink")
        resolved_profile = sdk_profile_root.resolve(strict=True)
        if (
            resolved_profile.name != _SDK_PROFILE_DIRNAME
            or not resolved_profile.is_dir()
        ):
            raise ValueError("SDK profile root is invalid")
        environment["APPDATA"] = str(resolved_profile)
        environment["LOCALAPPDATA"] = str(resolved_profile)
    return environment


def _scrub_current_worker_environment(project_root: Path) -> None:
    """Rebuild the worker environment even for a direct private-mode attempt."""

    appdata = os.environ.get("APPDATA")
    local_appdata = os.environ.get("LOCALAPPDATA")
    validated_profile: Path | None = None
    if (
        type(appdata) is str
        and appdata
        and type(local_appdata) is str
        and local_appdata == appdata
    ):
        try:
            raw_profile = Path(appdata)
            if not raw_profile.is_symlink():
                candidate = raw_profile.resolve(strict=True)
                cwd = Path.cwd().resolve(strict=True)
                if (
                    candidate.is_dir()
                    and candidate.name == _SDK_PROFILE_DIRNAME
                    and candidate.parent == cwd
                ):
                    validated_profile = candidate
        except (OSError, RuntimeError):
            validated_profile = None
    safe = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in _WORKER_PARENT_ENV_ALLOWLIST
    }
    safe["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
    safe["AI_STUDIO_RUNTIME_DIR"] = str(project_root)
    safe["AI_STUDIO_DATABASE_PATH"] = str(
        project_root / ".futu-live-preflight-database-must-not-open.sqlite3"
    )
    safe[_IMPORT_GUARD_ENV] = _IMPORT_GUARD_VALUE
    safe[_WATCHDOG_GUARD_ENV] = _WATCHDOG_GUARD_VALUE
    if validated_profile is not None:
        safe["APPDATA"] = str(validated_profile)
        safe["LOCALAPPDATA"] = str(validated_profile)
    os.environ.clear()
    os.environ.update(safe)


def _expected_worker_safety() -> dict[str, Any]:
    return {
        "read_only": True,
        "one_shot": True,
        "confirmation_required": True,
        "confirmation_verified": True,
        "isolated_cli_import_guard_attested": True,
        "sdk_profile_path_checked_at_import": True,
        "sdk_profile_policy": "parent_temp_appdata_v1",
        "watchdog_required": True,
        "watchdog_guard_attested": True,
        "watchdog_enforced": False,
        "watchdog_parent_promoted": False,
        "worker_process_tree_termination_attested": False,
        "loopback_only": True,
        "host_policy": "fixed_ipv4_loopback_literal_v1",
        "transport_mode": "guarded_loopback_futu_quote_path_v1",
        "network_requests_performed": None,
        "network_requests_accounting": "sdk_transport_not_instrumented",
        "logical_sdk_calls_accounting": "exact_guarded_boundary",
        "sdk_background_activity_attested": False,
        "dependency_file_integrity_attested": False,
        "application_file_writes_performed": 0,
        "third_party_filesystem_activity_attested": False,
        "database_reads_performed": 0,
        "database_writes_performed": 0,
        "provider_calls_performed": 0,
        "model_calls_performed": 0,
        "account_calls_performed": 0,
        "order_calls_performed": 0,
        "trade_calls_performed": 0,
        "http_listener_started": False,
        "execution_capability": "none",
        "live_trading_allowed": False,
        "opend_started": False,
        "login_performed": False,
    }


def _promote_worker_report(value: Any) -> dict[str, Any]:
    """Validate worker evidence and seal the parent-watchdog production receipt."""

    if type(value) is not dict:
        raise _WorkerReportInvalid("worker report must be an exact object")
    required = {
        "version",
        "scope",
        "ok",
        "status",
        "error_code",
        "evidence_class",
        "receipt_class",
        "captured_at_ms",
        "symbols",
        "sdk_distribution",
        "sdk_version",
        "source_manifest_sha256",
        "evidence_profile_sha256",
        "calls",
        "result",
        "safety",
        "receipt_sha256",
    }
    if set(value) != required:
        raise _WorkerReportInvalid("worker report fields changed")
    if (
        value["version"] != PREFLIGHT_VERSION
        or value["scope"] != "futu_storage_quotes_only"
        or value["evidence_class"] != "watchdog_worker_observation"
        or value["receipt_class"] != "watchdog_worker_observation_not_attestation"
        or value["source_manifest_sha256"] != _SOURCE_MANIFEST_SHA256
        or value["evidence_profile_sha256"] != _WORKER_EVIDENCE_PROFILE_SHA256
        or value["sdk_distribution"] != _FIXED_SDK_DISTRIBUTION
        or value["symbols"] != list(_FIXED_SYMBOLS)
        or value["safety"] != _expected_worker_safety()
    ):
        raise _WorkerReportInvalid("worker evidence boundary changed")
    if (
        type(value["ok"]) is not bool
        or type(value["status"]) is not str
        or value["status"] not in {"passed", "failed", "indeterminate"}
        or value["ok"] is not (value["status"] == "passed")
        or type(value["error_code"]) is not str
        or len(value["error_code"]) > 96
        or value["error_code"].splitlines() not in ([value["error_code"]], [])
        or (value["ok"] and value["error_code"] != "")
        or (not value["ok"] and not value["error_code"])
        or type(value["sdk_version"]) is not str
        or len(value["sdk_version"]) > 32
    ):
        raise _WorkerReportInvalid("worker status is invalid")
    if value["captured_at_ms"] is not None and (
        type(value["captured_at_ms"]) is not int or value["captured_at_ms"] < 0
    ):
        raise _WorkerReportInvalid("worker capture time is invalid")
    expected_calls = set(_zero_calls())
    if type(value["calls"]) is not dict or set(value["calls"]) != expected_calls:
        raise _WorkerReportInvalid("worker call ledger changed")
    calls = value["calls"]
    if any(type(count) is not int or not 0 <= count <= 1 for count in calls.values()):
        raise _WorkerReportInvalid("worker call count is invalid")
    for attempted, completed in (
        ("quote_batch_attempt_count", "quote_batch_return_count"),
        ("socket_probe_attempt_count", "socket_probe_success_count"),
        ("quote_context_open_attempt_count", "quote_context_open_success_count"),
        ("snapshot_call_attempt_count", "snapshot_call_return_count"),
        ("market_state_call_attempt_count", "market_state_call_return_count"),
        ("close_attempt_count", "close_success_count"),
    ):
        if calls[completed] > calls[attempted]:
            raise _WorkerReportInvalid("worker call completion is invalid")
    if (
        calls["socket_probe_attempt_count"] > calls["quote_batch_attempt_count"]
        or calls["quote_context_open_attempt_count"] > calls["socket_probe_success_count"]
        or calls["snapshot_call_attempt_count"] > calls["quote_context_open_success_count"]
        or calls["market_state_call_attempt_count"] > calls["snapshot_call_return_count"]
        or calls["close_attempt_count"] > calls["quote_context_open_success_count"]
    ):
        raise _WorkerReportInvalid("worker call order is invalid")
    result = value["result"]
    if type(result) is not dict or set(result) != {
        "snapshot_state",
        "coverage_complete",
        "quality_ready",
        "ready_symbol_count",
        "source_error_count",
    }:
        raise _WorkerReportInvalid("worker result fields changed")
    if (
        type(result["snapshot_state"]) is not str
        or result["snapshot_state"] not in {"ready", "degraded", "offline", "unavailable"}
        or type(result["coverage_complete"]) is not bool
        or type(result["quality_ready"]) is not bool
        or type(result["ready_symbol_count"]) is not int
        or not 0 <= result["ready_symbol_count"] <= len(_FIXED_SYMBOLS)
        or type(result["source_error_count"]) is not int
        or not 0 <= result["source_error_count"] <= 16
    ):
        raise _WorkerReportInvalid("worker result is invalid")
    digest = value["receipt_sha256"]
    if (
        type(digest) is not str
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise _WorkerReportInvalid("worker receipt digest is invalid")
    unsealed = copy.deepcopy(value)
    unsealed.pop("receipt_sha256")
    if not hmac.compare_digest(digest, _canonical_sha256(unsealed)):
        raise _WorkerReportInvalid("worker receipt digest mismatch")
    if value["ok"] and (
        value["sdk_version"] != _FIXED_SDK_VERSION
        or value["captured_at_ms"] is None
        or result != {
            "snapshot_state": "ready",
            "coverage_complete": True,
            "quality_ready": True,
            "ready_symbol_count": len(_FIXED_SYMBOLS),
            "source_error_count": 0,
        }
        or any(calls[name] != 1 for name in (
            "quote_batch_attempt_count",
            "quote_batch_return_count",
            "socket_probe_attempt_count",
            "socket_probe_success_count",
            "quote_context_open_attempt_count",
            "quote_context_open_success_count",
            "snapshot_call_attempt_count",
            "snapshot_call_return_count",
            "close_attempt_count",
            "close_success_count",
        ))
    ):
        raise _WorkerReportInvalid("worker success is incomplete")

    promoted = copy.deepcopy(value)
    promoted.pop("receipt_sha256")
    promoted["evidence_class"] = "production_path_observation"
    promoted["receipt_class"] = "production_path_observation_not_attestation"
    promoted["evidence_profile_sha256"] = _PRODUCTION_EVIDENCE_PROFILE_SHA256
    promoted["safety"]["watchdog_enforced"] = True
    promoted["safety"]["watchdog_parent_promoted"] = True
    promoted["receipt_sha256"] = _canonical_sha256(promoted)
    return promoted


def _worker_command(token: str) -> list[str]:
    return [
        sys.executable,
        "-I",
        "-B",
        str(Path(__file__).resolve()),
        _WORKER_FLAG,
        token,
        "--confirm",
        PREFLIGHT_CONFIRMATION,
    ]


def _run_bounded_worker(
    command: list[str],
    *,
    cwd: str,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[bytes]:
    """Run one worker with a bounded stdout reader and fixed wall timeout.

    Killing the direct worker terminates all SDK threads in that process.  This
    does not claim an OS-level proof that an arbitrary dependency could not
    create a descendant process; the report states that limitation explicitly.
    """

    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise _WorkerStartFailed("worker process could not start") from exc
    if process.stdout is None:
        process.kill()
        process.wait()
        raise subprocess.SubprocessError("worker stdout pipe was unavailable")

    output = bytearray()
    reader_error: list[BaseException] = []
    overflow = threading.Event()

    def read_stdout() -> None:
        try:
            while len(output) <= MAX_OUTPUT_BYTES:
                remaining = MAX_OUTPUT_BYTES + 1 - len(output)
                chunk = process.stdout.read(min(4096, remaining))
                if not chunk:
                    return
                output.extend(chunk)
                if len(output) > MAX_OUTPUT_BYTES:
                    overflow.set()
                    return
        except BaseException as exc:  # converted to one fixed parent error
            reader_error.append(exc)

    reader = threading.Thread(
        target=read_stdout,
        name="futu-preflight-bounded-stdout",
        daemon=True,
    )
    reader.start()
    deadline = time.monotonic() + WATCHDOG_TIMEOUT_SECONDS
    timed_out = False
    try:
        while process.poll() is None:
            if overflow.is_set():
                process.kill()
                break
            if time.monotonic() >= deadline:
                timed_out = True
                process.kill()
                break
            time.sleep(0.01)
        process.wait(timeout=2)
    except BaseException:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except BaseException:
            pass
        raise
    finally:
        reader.join(timeout=2)
        try:
            process.stdout.close()
        except OSError:
            pass
        if reader.is_alive():
            reader.join(timeout=0.25)

    if timed_out:
        raise subprocess.TimeoutExpired(command, WATCHDOG_TIMEOUT_SECONDS)
    if reader.is_alive() or reader_error:
        raise subprocess.SubprocessError("worker output reader failed")
    if overflow.is_set() or len(output) >= MAX_OUTPUT_BYTES:
        raise subprocess.SubprocessError("worker output exceeded bound")
    return subprocess.CompletedProcess(
        command,
        process.returncode,
        stdout=bytes(output),
        stderr=b"",
    )


def _run_watchdog_child(_confirmation: str) -> dict[str, Any]:
    if sys.flags.isolated != 1 or _backend_or_futu_modules_preloaded():
        return _error_payload(
            "PREFLIGHT_PARENT_ISOLATION_INVALID",
            confirmation_verified=True,
            indeterminate=True,
        )
    project_root = Path(__file__).resolve().parents[1]
    sentinel = project_root / ".futu-live-preflight-database-must-not-open.sqlite3"
    if os.path.lexists(sentinel):
        return _error_payload(
            "PREFLIGHT_DATABASE_SENTINEL_OCCUPIED",
            confirmation_verified=True,
            indeterminate=True,
        )
    token = secrets.token_hex(32)
    try:
        command = _worker_command(token)
    except OSError:
        return _error_payload(
            "PREFLIGHT_WORKER_START_FAILED",
            confirmation_verified=True,
            indeterminate=True,
        )
    worker_invoked = False
    try:
        with tempfile.TemporaryDirectory(prefix="ai-studio-futu-preflight-") as temp_dir:
            sdk_profile_root = Path(temp_dir) / _SDK_PROFILE_DIRNAME
            sdk_profile_root.mkdir()
            environment = _sanitized_worker_environment(
                project_root,
                token,
                sdk_profile_root=sdk_profile_root,
            )
            worker_invoked = True
            completed = _run_bounded_worker(
                command,
                cwd=temp_dir,
                environment=environment,
            )
    except _WorkerStartFailed:
        return _error_payload(
            "PREFLIGHT_WORKER_START_FAILED",
            confirmation_verified=True,
            indeterminate=True,
        )
    except subprocess.TimeoutExpired:
        return _error_payload(
            "PREFLIGHT_WATCHDOG_TIMEOUT",
            confirmation_verified=True,
            watchdog_enforced=True,
            indeterminate=True,
        )
    except (OSError, subprocess.SubprocessError):
        return _error_payload(
            (
                "PREFLIGHT_WORKER_LIFECYCLE_FAILED"
                if worker_invoked
                else "PREFLIGHT_WORKER_START_FAILED"
            ),
            confirmation_verified=True,
            watchdog_enforced=worker_invoked,
            indeterminate=True,
        )
    if os.path.lexists(sentinel):
        return _error_payload(
            "PREFLIGHT_DATABASE_SENTINEL_CREATED",
            confirmation_verified=True,
            watchdog_enforced=True,
            indeterminate=True,
        )
    if len(completed.stdout) >= MAX_OUTPUT_BYTES:
        return _error_payload(
            "PREFLIGHT_WORKER_OUTPUT_INVALID",
            confirmation_verified=True,
            watchdog_enforced=True,
            indeterminate=True,
        )
    try:
        decoded = completed.stdout.decode("ascii")
        report = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error_payload(
            "PREFLIGHT_WORKER_OUTPUT_INVALID",
            confirmation_verified=True,
            watchdog_enforced=True,
            indeterminate=True,
        )
    if type(report) is not dict:
        return _error_payload(
            "PREFLIGHT_WORKER_OUTPUT_INVALID",
            confirmation_verified=True,
            watchdog_enforced=True,
            indeterminate=True,
        )
    try:
        return _promote_worker_report(report)
    except _WorkerReportInvalid:
        return _error_payload(
            "PREFLIGHT_WORKER_REPORT_INVALID",
            confirmation_verified=True,
            watchdog_enforced=True,
            indeterminate=True,
        )


def _load_backend_runner(project_root: Path) -> tuple[Callable[..., dict[str, Any]], Callable[[Any], dict[str, Any]]]:
    root = str(project_root.resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    from backend.source_monitoring.futu_live_preflight import (
        run_futu_live_preflight,
        validate_futu_live_preflight_report,
    )

    return run_futu_live_preflight, validate_futu_live_preflight_report


def _run_backend_quietly(project_root: Path, confirmation: str) -> dict[str, Any]:
    """Keep third-party Python output off the worker's strict JSON channel."""

    with open(os.devnull, "w", encoding="utf-8", errors="replace") as sink:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            runner, validator = _load_backend_runner(project_root)
            return validator(runner(confirmation=confirmation))


def _internal_worker(argv: Sequence[str]) -> int:
    raw = list(argv)
    if len(raw) != 4 or raw[0] != _WORKER_FLAG or raw[2] != "--confirm":
        _emit(sys.stdout, _error_payload("PREFLIGHT_WORKER_ARGUMENTS_INVALID"))
        return 2
    token, confirmation = raw[1], raw[3]
    expected_token = os.environ.pop(_WORKER_TOKEN_ENV, "")
    if (
        type(token) is not str
        or type(expected_token) is not str
        or len(token) != 64
        or not secrets.compare_digest(token, expected_token)
        or confirmation != PREFLIGHT_CONFIRMATION
        or sys.flags.isolated != 1
        or sys.dont_write_bytecode is not True
        or _backend_or_futu_modules_preloaded()
    ):
        _emit(
            sys.stdout,
            _error_payload(
                "PREFLIGHT_WORKER_GUARD_INVALID",
                confirmation_verified=False,
                indeterminate=True,
            ),
        )
        return 2
    project_root = Path(__file__).resolve().parents[1]
    sentinel = project_root / ".futu-live-preflight-database-must-not-open.sqlite3"
    if os.path.lexists(sentinel):
        _emit(
            sys.stdout,
            _error_payload(
                "PREFLIGHT_DATABASE_SENTINEL_OCCUPIED",
                confirmation_verified=True,
                indeterminate=True,
            ),
        )
        return 2
    _scrub_current_worker_environment(project_root)
    try:
        report = _run_backend_quietly(project_root, confirmation)
    except BaseException:
        _emit(
            sys.stdout,
            _error_payload(
                "PREFLIGHT_WORKER_INTERNAL_ERROR",
                confirmation_verified=True,
                indeterminate=True,
            ),
        )
        return 1
    _emit(sys.stdout, report)
    return 0 if report["ok"] else 1


def _run_cli(
    argv: Sequence[str] | None = None,
    *,
    output: TextIO | None = None,
    runner: Callable[[str], dict[str, Any]],
    production_runner: bool,
    require_isolated_process: bool,
) -> int:
    stream = sys.stdout if output is None else output
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        mode, confirmation = _parse(raw)
    except (argparse.ArgumentError, _ArgumentsInvalid):
        _emit(stream, _error_payload("PREFLIGHT_ARGUMENTS_INVALID"))
        return 2
    if mode == "help":
        _emit(stream, _help_payload())
        return 0
    if confirmation != PREFLIGHT_CONFIRMATION:
        _emit(stream, _error_payload("PREFLIGHT_CONFIRMATION_REQUIRED"))
        return 2
    if require_isolated_process and sys.flags.isolated != 1:
        _emit(
            stream,
            _error_payload(
                "PREFLIGHT_ISOLATED_PROCESS_REQUIRED",
                confirmation_verified=True,
            ),
        )
        return 2
    if production_runner and _backend_or_futu_modules_preloaded():
        _emit(
            stream,
            _error_payload(
                "PREFLIGHT_PARENT_PRELOADED",
                confirmation_verified=True,
                indeterminate=True,
            ),
        )
        return 2
    try:
        report = runner(confirmation)
    except BaseException:
        report = _error_payload(
            "PREFLIGHT_INTERNAL_ERROR",
            confirmation_verified=True,
            indeterminate=True,
        )
    if type(report) is not dict:
        report = _error_payload(
            "PREFLIGHT_REPORT_INVALID",
            confirmation_verified=True,
            indeterminate=True,
        )
    _emit(stream, report)
    return 0 if report.get("ok") is True else 1


def main(
    argv: Sequence[str] | None = None,
    *,
    output: TextIO | None = None,
) -> int:
    return _run_cli(
        argv,
        output=output,
        runner=_run_watchdog_child,
        production_runner=True,
        require_isolated_process=True,
    )


def _main_injected(
    argv: Sequence[str] | None = None,
    *,
    output: TextIO | None = None,
    runner: Callable[[str], dict[str, Any]],
) -> int:
    return _run_cli(
        argv,
        output=output,
        runner=runner,
        production_runner=False,
        require_isolated_process=False,
    )


if __name__ == "__main__":
    if sys.argv[1:2] == [_WORKER_FLAG]:
        raise SystemExit(_internal_worker(sys.argv[1:]))
    raise SystemExit(main())


__all__ = [
    "MAX_OUTPUT_BYTES",
    "PREFLIGHT_CONFIRMATION",
    "PREFLIGHT_VERSION",
    "WATCHDOG_TIMEOUT_SECONDS",
    "_main_injected",
    "main",
]
