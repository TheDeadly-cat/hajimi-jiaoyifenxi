"""Isolated, process-backed access to the sealed Futu quote snapshot.

The parent-side :class:`FutuReadOnlyBroker` never imports the Futu SDK.  Both
the one-shot live preflight and the managed monitoring runtime use the same
bounded request protocol and the same worker entrypoint.  Only the worker may
load ``futu`` and it receives no account, order, trade, provider, or database
operation.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import hmac
import importlib
import importlib.metadata
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO, Callable

from ..source_poll_control import (
    SourcePollCancelled,
    SourcePollDeadlineExceeded,
    ensure_source_poll_active,
    validate_source_poll_control,
)


FUTU_READONLY_BROKER_VERSION = "futu_readonly_broker_v1"
FUTU_READONLY_BROKER_REQUEST_VERSION = "futu_readonly_broker_request_v1"
FUTU_READONLY_BROKER_RESPONSE_VERSION = "futu_readonly_broker_response_v1"
FUTU_READONLY_BROKER_POLICY_VERSION = "futu_readonly_broker_policy_v1"
FUTU_READONLY_BROKER_HOST = "127.0.0.1"
FUTU_READONLY_BROKER_PORT = 11111
FUTU_READONLY_BROKER_SYMBOLS = ("US.MU", "US.SNDK", "US.WDC", "US.STX")
FUTU_READONLY_BROKER_SDK_DISTRIBUTION = "futu-api"
FUTU_READONLY_BROKER_SDK_VERSION = "10.10.7008"
FUTU_READONLY_BROKER_DEFAULT_TIMEOUT_MS = 15_000
FUTU_READONLY_BROKER_REQUEST_MAX_BYTES = 4_096
FUTU_READONLY_BROKER_SNAPSHOT_MAX_BYTES = 256 * 1024
FUTU_READONLY_BROKER_RESPONSE_MAX_BYTES = 320 * 1024

_WORKER_FLAG = "--futu-readonly-broker-worker"
_WORKER_TOKEN_ENV = "AI_STUDIO_FUTU_BROKER_WORKER_TOKEN"
_WORKER_GUARD_ENV = "AI_STUDIO_FUTU_BROKER_WORKER_GUARD"
_WORKER_GUARD_VALUE = "futu-readonly-broker-isolated-worker-v1"
_SDK_PROFILE_DIRNAME = "futu-sdk-profile"
_BROKER_MODES = frozenset({"one_shot", "managed"})
_HEX = frozenset("0123456789abcdef")
_WORKER_ERROR_CODES = frozenset({
    "FUTU_BROKER_OUTPUT_INVALID",
    "FUTU_BROKER_SDK_BOUNDARY_INVALID",
    "FUTU_BROKER_SNAPSHOT_INVALID",
    "FUTU_BROKER_SNAPSHOT_TOO_LARGE",
    "FUTU_BROKER_WORKER_INTERNAL_ERROR",
    "FUTU_CONTEXT_CLOSE_UNVERIFIED",
    "FUTU_SDK_IMPORT_FAILED",
    "FUTU_SDK_UNAVAILABLE",
    "FUTU_SDK_VERSION_MISMATCH",
})
_PRE_IO_WORKER_ERROR_CODES = frozenset({
    "FUTU_BROKER_SDK_BOUNDARY_INVALID",
    "FUTU_SDK_IMPORT_FAILED",
    "FUTU_SDK_UNAVAILABLE",
    "FUTU_SDK_VERSION_MISMATCH",
})
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_WORKER_SCRIPT = _PROJECT_ROOT / "scripts" / "run_futu_readonly_broker_worker.py"
_DATABASE_SENTINEL = _PROJECT_ROOT / ".futu-readonly-broker-database-must-not-open.sqlite3"

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

_CALL_KEYS = (
    "quote_batch_attempt_count",
    "quote_batch_return_count",
    "socket_probe_attempt_count",
    "socket_probe_success_count",
    "quote_context_open_attempt_count",
    "quote_context_open_success_count",
    "snapshot_call_attempt_count",
    "snapshot_call_return_count",
    "market_state_call_attempt_count",
    "market_state_call_return_count",
    "close_attempt_count",
    "close_success_count",
)


class FutuReadOnlyBrokerError(RuntimeError):
    """A fixed, redacted broker lifecycle or protocol failure."""

    def __init__(self, code: str, message: str = "read-only Futu broker failed") -> None:
        super().__init__(message)
        self.code = code


class _DuplicateJsonKey(ValueError):
    pass


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("ascii")).hexdigest()


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise _DuplicateJsonKey("duplicate or non-string JSON key")
        result[key] = value
    return result


def _strict_json_loads(raw: bytes) -> Any:
    try:
        text = raw.decode("ascii")
        return json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite JSON number")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise FutuReadOnlyBrokerError("FUTU_BROKER_PROTOCOL_INVALID") from exc


def _blank_calls() -> dict[str, int]:
    return {name: 0 for name in _CALL_KEYS}


def futu_readonly_broker_policy_manifest() -> dict[str, Any]:
    return {
        "version": FUTU_READONLY_BROKER_POLICY_VERSION,
        "worker_protocol": FUTU_READONLY_BROKER_VERSION,
        "host": FUTU_READONLY_BROKER_HOST,
        "port": FUTU_READONLY_BROKER_PORT,
        "symbols": list(FUTU_READONLY_BROKER_SYMBOLS),
        "sdk_distribution": FUTU_READONLY_BROKER_SDK_DISTRIBUTION,
        "sdk_version": FUTU_READONLY_BROKER_SDK_VERSION,
        "allowed_operation": "quote_snapshot",
        "market_state_policy": "only_when_snapshot_freshness_requires_it",
        "request_max_bytes": FUTU_READONLY_BROKER_REQUEST_MAX_BYTES,
        "snapshot_max_bytes": FUTU_READONLY_BROKER_SNAPSHOT_MAX_BYTES,
        "response_max_bytes": FUTU_READONLY_BROKER_RESPONSE_MAX_BYTES,
        "sdk_profile_policy": "per_worker_temporary_appdata_v1",
        "sdk_stdio_policy": "captured_never_forwarded_to_studio_v1",
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


FUTU_READONLY_BROKER_POLICY_SHA256 = _canonical_sha256(
    futu_readonly_broker_policy_manifest()
)


def _worker_environment(profile: Path, token: str) -> dict[str, str]:
    resolved = profile.resolve(strict=True)
    if (
        profile.is_symlink()
        or not resolved.is_dir()
        or resolved.name != _SDK_PROFILE_DIRNAME
    ):
        raise FutuReadOnlyBrokerError("FUTU_BROKER_PROFILE_INVALID")
    environment = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in _WORKER_PARENT_ENV_ALLOWLIST
    }
    environment.update({
        "AI_STUDIO_SKIP_LOCAL_ENV": "1",
        "AI_STUDIO_RUNTIME_DIR": str(_PROJECT_ROOT),
        "AI_STUDIO_DATABASE_PATH": str(_DATABASE_SENTINEL),
        "APPDATA": str(resolved),
        "LOCALAPPDATA": str(resolved),
        _WORKER_GUARD_ENV: _WORKER_GUARD_VALUE,
        _WORKER_TOKEN_ENV: token,
    })
    return environment


def _scrub_worker_environment(profile: Path) -> None:
    """Rebuild the private worker environment before any SDK import."""

    safe = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in _WORKER_PARENT_ENV_ALLOWLIST
    }
    safe.update({
        "AI_STUDIO_SKIP_LOCAL_ENV": "1",
        "AI_STUDIO_RUNTIME_DIR": str(_PROJECT_ROOT),
        "AI_STUDIO_DATABASE_PATH": str(_DATABASE_SENTINEL),
        "APPDATA": str(profile),
        "LOCALAPPDATA": str(profile),
        _WORKER_GUARD_ENV: _WORKER_GUARD_VALUE,
    })
    os.environ.clear()
    os.environ.update(safe)


def _worker_command(mode: str) -> list[str]:
    return [
        sys.executable,
        "-I",
        "-B",
        str(_WORKER_SCRIPT),
        _WORKER_FLAG,
        "--mode",
        mode,
    ]


def _read_bounded_line(stream: BinaryIO) -> bytes:
    raw = stream.readline(FUTU_READONLY_BROKER_RESPONSE_MAX_BYTES + 1)
    if not raw or len(raw) > FUTU_READONLY_BROKER_RESPONSE_MAX_BYTES:
        raise FutuReadOnlyBrokerError("FUTU_BROKER_OUTPUT_INVALID")
    if not raw.endswith(b"\n") or b"\r" in raw:
        raise FutuReadOnlyBrokerError("FUTU_BROKER_OUTPUT_INVALID")
    return raw[:-1]


def _validate_calls(value: Any) -> dict[str, int]:
    if type(value) is not dict or set(value) != set(_CALL_KEYS):
        raise FutuReadOnlyBrokerError("FUTU_BROKER_PROTOCOL_INVALID")
    if any(type(value[name]) is not int or not 0 <= value[name] <= 1 for name in _CALL_KEYS):
        raise FutuReadOnlyBrokerError("FUTU_BROKER_PROTOCOL_INVALID")
    for attempted, completed in (
        ("quote_batch_attempt_count", "quote_batch_return_count"),
        ("socket_probe_attempt_count", "socket_probe_success_count"),
        ("quote_context_open_attempt_count", "quote_context_open_success_count"),
        ("snapshot_call_attempt_count", "snapshot_call_return_count"),
        ("market_state_call_attempt_count", "market_state_call_return_count"),
        ("close_attempt_count", "close_success_count"),
    ):
        if value[completed] > value[attempted]:
            raise FutuReadOnlyBrokerError("FUTU_BROKER_PROTOCOL_INVALID")
    if (
        value["socket_probe_attempt_count"] > value["quote_batch_attempt_count"]
        or value["quote_context_open_attempt_count"]
        > value["socket_probe_success_count"]
        or value["snapshot_call_attempt_count"]
        > value["quote_context_open_success_count"]
        or value["market_state_call_attempt_count"]
        > value["snapshot_call_return_count"]
        or value["close_attempt_count"]
        > value["quote_context_open_success_count"]
    ):
        raise FutuReadOnlyBrokerError("FUTU_BROKER_PROTOCOL_INVALID")
    return dict(value)


def _validate_worker_response(value: Any, request_id: str) -> dict[str, Any]:
    required = {
        "version",
        "request_id",
        "ok",
        "error_code",
        "sdk_version",
        "snapshot",
        "calls",
        "policy_sha256",
        "execution_capability",
        "live_trading_allowed",
        "response_sha256",
    }
    if type(value) is not dict or set(value) != required:
        raise FutuReadOnlyBrokerError("FUTU_BROKER_PROTOCOL_INVALID")
    digest = value["response_sha256"]
    if (
        type(digest) is not str
        or len(digest) != 64
        or any(character not in _HEX for character in digest)
    ):
        raise FutuReadOnlyBrokerError("FUTU_BROKER_PROTOCOL_INVALID")
    unsealed = copy.deepcopy(value)
    unsealed.pop("response_sha256")
    if not hmac.compare_digest(digest, _canonical_sha256(unsealed)):
        raise FutuReadOnlyBrokerError("FUTU_BROKER_PROTOCOL_INVALID")
    if (
        value["version"] != FUTU_READONLY_BROKER_RESPONSE_VERSION
        or value["request_id"] != request_id
        or type(value["ok"]) is not bool
        or type(value["error_code"]) is not str
        or len(value["error_code"]) > 96
        or value["error_code"].splitlines() not in ([], [value["error_code"]])
        or (not value["ok"] and value["error_code"] not in _WORKER_ERROR_CODES)
        or type(value["sdk_version"]) is not str
        or len(value["sdk_version"]) > 32
        or value["policy_sha256"] != FUTU_READONLY_BROKER_POLICY_SHA256
        or value["execution_capability"] != "none"
        or value["live_trading_allowed"] is not False
        or value["ok"] is (value["error_code"] != "")
    ):
        raise FutuReadOnlyBrokerError("FUTU_BROKER_PROTOCOL_INVALID")
    calls = _validate_calls(value["calls"])
    snapshot = value["snapshot"]
    if value["ok"]:
        if type(snapshot) is not dict:
            raise FutuReadOnlyBrokerError("FUTU_BROKER_PROTOCOL_INVALID")
        try:
            encoded = _canonical_json(snapshot).encode("ascii")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise FutuReadOnlyBrokerError("FUTU_BROKER_PROTOCOL_INVALID") from exc
        if len(encoded) > FUTU_READONLY_BROKER_SNAPSHOT_MAX_BYTES:
            raise FutuReadOnlyBrokerError("FUTU_BROKER_OUTPUT_INVALID")
        if (
            snapshot.get("execution_capability") != "none"
            or snapshot.get("live_trading_allowed") is not False
            or snapshot.get("symbols") != list(FUTU_READONLY_BROKER_SYMBOLS)
        ):
            raise FutuReadOnlyBrokerError("FUTU_BROKER_PROTOCOL_INVALID")
        if (
            value["sdk_version"] != FUTU_READONLY_BROKER_SDK_VERSION
            or calls["quote_batch_attempt_count"] != 1
            or calls["quote_batch_return_count"] != 1
            or calls["socket_probe_attempt_count"] != 1
        ):
            raise FutuReadOnlyBrokerError("FUTU_BROKER_PROTOCOL_INVALID")
    elif snapshot is not None or (
        value["error_code"] in _PRE_IO_WORKER_ERROR_CODES and any(calls.values())
    ):
        raise FutuReadOnlyBrokerError("FUTU_BROKER_PROTOCOL_INVALID")
    result = copy.deepcopy(value)
    result["calls"] = calls
    return result


class FutuReadOnlyBroker:
    """Parent-side one-shot or managed broker for one fixed quote operation."""

    def __init__(
        self,
        *,
        mode: str = "managed",
        timeout_ms: int = FUTU_READONLY_BROKER_DEFAULT_TIMEOUT_MS,
        monotonic_ms: Callable[[], Any] | None = None,
    ) -> None:
        if type(mode) is not str or mode not in _BROKER_MODES:
            raise ValueError("mode must be exactly one_shot or managed")
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= 120_000:
            raise ValueError("timeout_ms must be a positive native integer")
        if monotonic_ms is not None and not callable(monotonic_ms):
            raise TypeError("monotonic_ms must be callable")
        self.mode = mode
        self.host = FUTU_READONLY_BROKER_HOST
        self.port = FUTU_READONLY_BROKER_PORT
        self.cache_ttl_seconds = 1.0
        self.timeout_ms = timeout_ms
        self._monotonic_ms = monotonic_ms or (lambda: int(time.monotonic() * 1_000))
        self._worker_command_token = _worker_command
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self._request_count = 0

    @property
    def policy_sha256(self) -> str:
        return FUTU_READONLY_BROKER_POLICY_SHA256

    def policy_manifest(self) -> dict[str, Any]:
        return copy.deepcopy(futu_readonly_broker_policy_manifest())

    def _now_ms(self) -> int:
        value = self._monotonic_ms()
        if type(value) is not int or value < 0:
            raise FutuReadOnlyBrokerError("FUTU_BROKER_CLOCK_INVALID")
        return value

    def _effective_deadline(self, deadline_monotonic_ms: int) -> int:
        local = self._now_ms() + self.timeout_ms
        return min(local, deadline_monotonic_ms) if deadline_monotonic_ms else local

    def _start_locked(self) -> subprocess.Popen[bytes]:
        if _worker_command is not self._worker_command_token:
            raise FutuReadOnlyBrokerError("FUTU_BROKER_DEPENDENCY_DRIFT")
        current = self._process
        if current is not None and current.poll() is None:
            return current
        if not self._discard_locked():
            raise FutuReadOnlyBrokerError("FUTU_BROKER_WORKER_LIFECYCLE_FAILED")
        if os.path.lexists(_DATABASE_SENTINEL):
            raise FutuReadOnlyBrokerError("FUTU_BROKER_DATABASE_SENTINEL_OCCUPIED")
        holder = tempfile.TemporaryDirectory(prefix="ai-studio-futu-broker-")
        process: subprocess.Popen[bytes] | None = None
        try:
            root = Path(holder.name)
            profile = root / _SDK_PROFILE_DIRNAME
            profile.mkdir()
            token = secrets.token_hex(32)
            process = subprocess.Popen(
                _worker_command(self.mode),
                cwd=str(root),
                env=_worker_environment(profile, token),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if process.stdin is None or process.stdout is None:
                process.kill()
                process.wait(timeout=2)
                raise FutuReadOnlyBrokerError("FUTU_BROKER_WORKER_START_FAILED")
        except FutuReadOnlyBrokerError:
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=2)
                except BaseException:
                    pass
            try:
                holder.cleanup()
            except OSError:
                pass
            raise
        except BaseException:
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=2)
                except BaseException:
                    pass
            try:
                holder.cleanup()
            except OSError:
                pass
            raise FutuReadOnlyBrokerError("FUTU_BROKER_WORKER_START_FAILED") from None
        self._temporary_directory = holder
        self._process = process
        return process

    def _discard_locked(self) -> bool:
        process = self._process
        holder = self._temporary_directory
        stopped = True
        if process is not None:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    stopped = False
            try:
                process.wait(timeout=2)
            except BaseException:
                stopped = False
            if process.poll() is None:
                return False
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        stopped = False
            self._process = None
        if holder is not None:
            try:
                holder.cleanup()
            except OSError:
                stopped = False
            else:
                self._temporary_directory = None
        return stopped

    def stop(self) -> bool:
        with self._lock:
            return self._discard_locked()

    close = stop

    def quote_batch_observation(
        self,
        symbols: tuple[str, ...] | list[str] = FUTU_READONLY_BROKER_SYMBOLS,
        *,
        force: bool = True,
        deadline_monotonic_ms: int = 0,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        if (
            type(symbols) not in {tuple, list}
            or tuple(symbols) != FUTU_READONLY_BROKER_SYMBOLS
            or any(type(symbol) is not str for symbol in symbols)
            or force is not True
        ):
            raise FutuReadOnlyBrokerError("FUTU_BROKER_REQUEST_INVALID")
        deadline_monotonic_ms, cancel_event = validate_source_poll_control(
            deadline_monotonic_ms=deadline_monotonic_ms,
            cancel_event=cancel_event,
        )
        ensure_source_poll_active(
            deadline_monotonic_ms=deadline_monotonic_ms,
            cancel_event=cancel_event,
            monotonic_ms=self._monotonic_ms,
        )
        with self._lock:
            ensure_source_poll_active(
                deadline_monotonic_ms=deadline_monotonic_ms,
                cancel_event=cancel_event,
                monotonic_ms=self._monotonic_ms,
            )
            if self.mode == "one_shot" and self._request_count:
                raise FutuReadOnlyBrokerError("FUTU_BROKER_ONE_SHOT_CONSUMED")
            process = self._start_locked()
            request_id = secrets.token_hex(16)
            request = {
                "version": FUTU_READONLY_BROKER_REQUEST_VERSION,
                "request_id": request_id,
                "operation": "quote_snapshot",
                "policy_sha256": FUTU_READONLY_BROKER_POLICY_SHA256,
            }
            raw_request = (_canonical_json(request) + "\n").encode("ascii")
            if len(raw_request) > FUTU_READONLY_BROKER_REQUEST_MAX_BYTES:
                self._discard_locked()
                raise FutuReadOnlyBrokerError("FUTU_BROKER_REQUEST_INVALID")
            try:
                assert process.stdin is not None
                assert process.stdout is not None
                process.stdin.write(raw_request)
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                self._discard_locked()
                raise FutuReadOnlyBrokerError("FUTU_BROKER_WORKER_CRASHED") from None

            result: list[bytes] = []
            reader_error: list[BaseException] = []
            done = threading.Event()

            def read_response() -> None:
                try:
                    result.append(_read_bounded_line(process.stdout))
                except BaseException as exc:
                    reader_error.append(exc)
                finally:
                    done.set()

            reader = threading.Thread(
                target=read_response,
                name="futu-broker-bounded-reader",
                daemon=True,
            )
            reader.start()
            effective_deadline = self._effective_deadline(deadline_monotonic_ms)
            failure: BaseException | None = None
            while not done.wait(0.01):
                try:
                    ensure_source_poll_active(
                        deadline_monotonic_ms=effective_deadline,
                        cancel_event=cancel_event,
                        monotonic_ms=self._monotonic_ms,
                    )
                except SourcePollCancelled:
                    failure = SourcePollCancelled(
                        "SOURCE_MONITORING_POLL_CANCELLED",
                        "source poll was cancelled while waiting for Futu broker",
                    )
                    break
                except SourcePollDeadlineExceeded:
                    failure = SourcePollDeadlineExceeded(
                        "SOURCE_MONITORING_POLL_DEADLINE_EXCEEDED",
                        "source poll deadline elapsed while waiting for Futu broker",
                    )
                    break
                if process.poll() is not None:
                    failure = FutuReadOnlyBrokerError("FUTU_BROKER_WORKER_CRASHED")
                    break
            if failure is not None:
                self._discard_locked()
                reader.join(timeout=2)
                raise failure
            reader.join(timeout=2)
            if reader.is_alive() or reader_error or len(result) != 1:
                self._discard_locked()
                raise FutuReadOnlyBrokerError("FUTU_BROKER_OUTPUT_INVALID")
            try:
                ensure_source_poll_active(
                    deadline_monotonic_ms=effective_deadline,
                    cancel_event=cancel_event,
                    monotonic_ms=self._monotonic_ms,
                )
            except (SourcePollCancelled, SourcePollDeadlineExceeded):
                self._discard_locked()
                raise
            try:
                response = _validate_worker_response(
                    _strict_json_loads(result[0]),
                    request_id,
                )
            except FutuReadOnlyBrokerError:
                self._discard_locked()
                raise
            if os.path.lexists(_DATABASE_SENTINEL):
                self._discard_locked()
                raise FutuReadOnlyBrokerError("FUTU_BROKER_DATABASE_SENTINEL_CREATED")
            self._request_count += 1
            if self.mode == "one_shot":
                if process.poll() is None:
                    try:
                        process.wait(timeout=2)
                    except BaseException:
                        self._discard_locked()
                        raise FutuReadOnlyBrokerError(
                            "FUTU_BROKER_WORKER_LIFECYCLE_FAILED"
                        ) from None
                assert process.stdout is not None
                try:
                    trailing_output = process.stdout.read(1)
                except OSError:
                    trailing_output = b"\x00"
                if trailing_output:
                    self._discard_locked()
                    raise FutuReadOnlyBrokerError("FUTU_BROKER_OUTPUT_INVALID")
                if not self._discard_locked():
                    raise FutuReadOnlyBrokerError(
                        "FUTU_BROKER_WORKER_LIFECYCLE_FAILED"
                    )
            return response

    def quote_batch(
        self,
        symbols: tuple[str, ...] | list[str] = FUTU_READONLY_BROKER_SYMBOLS,
        *,
        force: bool = True,
        deadline_monotonic_ms: int = 0,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        observation = self.quote_batch_observation(
            symbols,
            force=force,
            deadline_monotonic_ms=deadline_monotonic_ms,
            cancel_event=cancel_event,
        )
        if observation["ok"] is not True:
            self.stop()
            raise FutuReadOnlyBrokerError(str(observation["error_code"]))
        return copy.deepcopy(observation["snapshot"])


class _CallLedger:
    def __init__(self) -> None:
        self.calls = _blank_calls()

    def increment(self, name: str) -> None:
        if name not in self.calls or self.calls[name] >= 1:
            raise FutuReadOnlyBrokerError("FUTU_BROKER_CALL_BOUND_EXCEEDED")
        self.calls[name] += 1


class _GuardedQuoteContext:
    __slots__ = ("_inner", "_ledger")

    def __init__(self, inner: Any, ledger: _CallLedger) -> None:
        self._inner = inner
        self._ledger = ledger

    def get_market_snapshot(self, symbols: Any) -> Any:
        if type(symbols) is not list or tuple(symbols) != FUTU_READONLY_BROKER_SYMBOLS:
            raise FutuReadOnlyBrokerError("FUTU_BROKER_SYMBOL_POLICY_INVALID")
        self._ledger.increment("snapshot_call_attempt_count")
        result = self._inner.get_market_snapshot(symbols)
        self._ledger.increment("snapshot_call_return_count")
        return result

    def get_market_state(self, symbols: Any) -> Any:
        if (
            type(symbols) is not list
            or not symbols
            or len(symbols) != len(set(symbols))
            or symbols
            != [symbol for symbol in FUTU_READONLY_BROKER_SYMBOLS if symbol in symbols]
        ):
            raise FutuReadOnlyBrokerError("FUTU_BROKER_SYMBOL_POLICY_INVALID")
        self._ledger.increment("market_state_call_attempt_count")
        result = self._inner.get_market_state(symbols)
        self._ledger.increment("market_state_call_return_count")
        return result

    def close(self) -> Any:
        self._ledger.increment("close_attempt_count")
        result = self._inner.close()
        self._ledger.increment("close_success_count")
        return result


class _GuardedSdk:
    __slots__ = ("RET_OK", "_constructor", "_ledger")

    def __init__(self, sdk: Any, ledger: _CallLedger) -> None:
        ret_ok = getattr(sdk, "RET_OK", None)
        constructor = getattr(sdk, "OpenQuoteContext", None)
        if type(ret_ok) is not int or ret_ok != 0 or not callable(constructor):
            raise FutuReadOnlyBrokerError("FUTU_BROKER_SDK_BOUNDARY_INVALID")
        self.RET_OK = ret_ok
        self._constructor = constructor
        self._ledger = ledger

    def OpenQuoteContext(self, *, host: Any, port: Any) -> _GuardedQuoteContext:
        if host != FUTU_READONLY_BROKER_HOST or port != FUTU_READONLY_BROKER_PORT:
            raise FutuReadOnlyBrokerError("FUTU_BROKER_TARGET_INVALID")
        self._ledger.increment("quote_context_open_attempt_count")
        inner = self._constructor(host=host, port=port)
        self._ledger.increment("quote_context_open_success_count")
        return _GuardedQuoteContext(inner, self._ledger)


class _GuardedSocketProbe:
    __slots__ = ("_create_connection", "_ledger")

    def __init__(self, create_connection: Callable[..., Any], ledger: _CallLedger) -> None:
        self._create_connection = create_connection
        self._ledger = ledger

    def __call__(self, host: Any, port: Any) -> bool:
        if host != FUTU_READONLY_BROKER_HOST or port != FUTU_READONLY_BROKER_PORT:
            raise FutuReadOnlyBrokerError("FUTU_BROKER_TARGET_INVALID")
        self._ledger.increment("socket_probe_attempt_count")
        try:
            connection = self._create_connection(
                (FUTU_READONLY_BROKER_HOST, FUTU_READONLY_BROKER_PORT),
                timeout=0.35,
            )
        except OSError:
            return False
        try:
            self._ledger.increment("socket_probe_success_count")
            return True
        finally:
            try:
                connection.close()
            except Exception:
                pass


def _seal_worker_response(payload: dict[str, Any]) -> dict[str, Any]:
    sealed = copy.deepcopy(payload)
    sealed["response_sha256"] = _canonical_sha256(sealed)
    return sealed


def _worker_error(
    request_id: str,
    code: str,
    sdk_version: str = "",
    *,
    calls: dict[str, int] | None = None,
) -> dict[str, Any]:
    return _seal_worker_response({
        "version": FUTU_READONLY_BROKER_RESPONSE_VERSION,
        "request_id": request_id,
        "ok": False,
        "error_code": code,
        "sdk_version": sdk_version,
        "snapshot": None,
        "calls": _blank_calls() if calls is None else dict(calls),
        "policy_sha256": FUTU_READONLY_BROKER_POLICY_SHA256,
        "execution_capability": "none",
        "live_trading_allowed": False,
    })


def _load_worker_sdk() -> tuple[Any | None, str, str]:
    try:
        version = importlib.metadata.version(FUTU_READONLY_BROKER_SDK_DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return None, "", "FUTU_SDK_UNAVAILABLE"
    except BaseException:
        return None, "", "FUTU_SDK_IMPORT_FAILED"
    if version != FUTU_READONLY_BROKER_SDK_VERSION:
        return None, version, "FUTU_SDK_VERSION_MISMATCH"
    try:
        sdk = importlib.import_module("futu")
    except BaseException:
        return None, version, "FUTU_SDK_IMPORT_FAILED"
    return sdk, version, ""


def _worker_quote_response(
    request_id: str,
    *,
    sdk: Any,
    sdk_version: str,
) -> dict[str, Any]:
    from ..market.futu_readonly import FutuUsMarketAdapter
    import socket

    ledger = _CallLedger()
    try:
        guarded_sdk = _GuardedSdk(sdk, ledger)
        guarded_probe = _GuardedSocketProbe(socket.create_connection, ledger)
        adapter = FutuUsMarketAdapter(
            host=FUTU_READONLY_BROKER_HOST,
            port=FUTU_READONLY_BROKER_PORT,
            cache_ttl_seconds=1.0,
            sdk_module=guarded_sdk,
            socket_probe=guarded_probe,
            monotonic_clock=time.monotonic,
            snapshot_id_factory=lambda: f"futu_broker_{request_id}",
        )
        ledger.increment("quote_batch_attempt_count")
        snapshot = adapter.quote_batch(FUTU_READONLY_BROKER_SYMBOLS, force=True)
        ledger.increment("quote_batch_return_count")
    except BaseException:
        return _worker_error(
            request_id,
            "FUTU_BROKER_WORKER_INTERNAL_ERROR",
            sdk_version,
            calls=ledger.calls,
        )
    calls = dict(ledger.calls)
    if calls["quote_context_open_success_count"] and (
        calls["close_attempt_count"] != 1 or calls["close_success_count"] != 1
    ):
        return _worker_error(
            request_id,
            "FUTU_CONTEXT_CLOSE_UNVERIFIED",
            sdk_version,
            calls=calls,
        )
    try:
        encoded = _canonical_json(snapshot).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError):
        return _worker_error(
            request_id,
            "FUTU_BROKER_SNAPSHOT_INVALID",
            sdk_version,
            calls=calls,
        )
    if len(encoded) > FUTU_READONLY_BROKER_SNAPSHOT_MAX_BYTES:
        return _worker_error(
            request_id,
            "FUTU_BROKER_SNAPSHOT_TOO_LARGE",
            sdk_version,
            calls=calls,
        )
    return _seal_worker_response({
        "version": FUTU_READONLY_BROKER_RESPONSE_VERSION,
        "request_id": request_id,
        "ok": True,
        "error_code": "",
        "sdk_version": sdk_version,
        "snapshot": snapshot,
        "calls": calls,
        "policy_sha256": FUTU_READONLY_BROKER_POLICY_SHA256,
        "execution_capability": "none",
        "live_trading_allowed": False,
    })


def _emit_worker_response(output: Any, payload: dict[str, Any]) -> None:
    encoded = (_canonical_json(payload) + "\n").encode("ascii")
    if len(encoded) > FUTU_READONLY_BROKER_RESPONSE_MAX_BYTES:
        encoded = (
            _canonical_json(
                _worker_error(str(payload.get("request_id") or ""), "FUTU_BROKER_OUTPUT_INVALID")
            )
            + "\n"
        ).encode("ascii")
    output.write(encoded)
    output.flush()


def run_futu_readonly_broker_worker(argv: list[str]) -> int:
    """Private worker entry used only by the fixed script wrapper."""

    raw = list(argv)
    if (
        len(raw) != 3
        or raw[0] != _WORKER_FLAG
        or raw[1] != "--mode"
        or raw[2] not in _BROKER_MODES
    ):
        return 2
    mode = raw[2]
    expected = os.environ.pop(_WORKER_TOKEN_ENV, "")
    appdata = os.environ.get("APPDATA", "")
    try:
        raw_profile = Path(appdata)
        if raw_profile.is_symlink():
            return 2
        profile = raw_profile.resolve(strict=True)
        cwd = Path.cwd().resolve(strict=True)
    except (OSError, RuntimeError):
        return 2
    if (
        type(expected) is not str
        or len(expected) != 64
        or any(character not in _HEX for character in expected)
        or os.environ.get(_WORKER_GUARD_ENV) != _WORKER_GUARD_VALUE
        or os.environ.get("AI_STUDIO_SKIP_LOCAL_ENV") != "1"
        or sys.flags.isolated != 1
        or sys.dont_write_bytecode is not True
        or not profile.is_dir()
        or profile.name != _SDK_PROFILE_DIRNAME
        or profile.parent != cwd
        or os.environ.get("LOCALAPPDATA") != appdata
        or os.path.lexists(_DATABASE_SENTINEL)
    ):
        return 2

    _scrub_worker_environment(profile)
    protocol_output = sys.stdout.buffer
    with open(os.devnull, "w", encoding="utf-8", errors="replace") as sink:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            sdk, sdk_version, sdk_error = _load_worker_sdk()
            handled = 0
            while True:
                raw_request = sys.stdin.buffer.readline(
                    FUTU_READONLY_BROKER_REQUEST_MAX_BYTES + 1
                )
                if not raw_request:
                    return 0
                if (
                    len(raw_request) > FUTU_READONLY_BROKER_REQUEST_MAX_BYTES
                    or not raw_request.endswith(b"\n")
                    or b"\r" in raw_request
                ):
                    return 2
                try:
                    request = _strict_json_loads(raw_request[:-1])
                except FutuReadOnlyBrokerError:
                    return 2
                if (
                    type(request) is not dict
                    or set(request)
                    != {"version", "request_id", "operation", "policy_sha256"}
                    or request.get("version") != FUTU_READONLY_BROKER_REQUEST_VERSION
                    or request.get("operation") != "quote_snapshot"
                    or request.get("policy_sha256") != FUTU_READONLY_BROKER_POLICY_SHA256
                    or type(request.get("request_id")) is not str
                    or len(request["request_id"]) != 32
                    or any(character not in _HEX for character in request["request_id"])
                ):
                    return 2
                request_id = request["request_id"]
                response = (
                    _worker_error(request_id, sdk_error, sdk_version)
                    if sdk_error
                    else _worker_quote_response(
                        request_id,
                        sdk=sdk,
                        sdk_version=sdk_version,
                    )
                )
                _emit_worker_response(protocol_output, response)
                handled += 1
                if mode == "one_shot" or handled >= 1_000_000:
                    return 0


__all__ = [
    "FUTU_READONLY_BROKER_DEFAULT_TIMEOUT_MS",
    "FUTU_READONLY_BROKER_HOST",
    "FUTU_READONLY_BROKER_POLICY_SHA256",
    "FUTU_READONLY_BROKER_PORT",
    "FUTU_READONLY_BROKER_SDK_DISTRIBUTION",
    "FUTU_READONLY_BROKER_SDK_VERSION",
    "FUTU_READONLY_BROKER_SYMBOLS",
    "FUTU_READONLY_BROKER_VERSION",
    "FutuReadOnlyBroker",
    "FutuReadOnlyBrokerError",
    "futu_readonly_broker_policy_manifest",
    "run_futu_readonly_broker_worker",
]
