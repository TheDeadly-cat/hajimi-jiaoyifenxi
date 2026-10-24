from __future__ import annotations

import argparse
import dataclasses
import getpass
import importlib
import json
import math
import os
import sys
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .settings import TelegramSettings, TelegramSettingsError
from .tdlib import (
    CTypesTDLibTransport,
    InteractiveAuthCallbacks,
    TDLibClient,
    TDLibError,
    TDLibSettings,
)


class CLIUsageError(ValueError):
    pass


class CLICommandError(RuntimeError):
    pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Any,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        del request, file_pointer, code, message, headers, new_url
        return None


_LOCAL_STATUS_OPENER = urllib.request.build_opener(_NoRedirectHandler())


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CLIUsageError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="telegram-intelligence")
    commands = parser.add_subparsers(dest="command", required=True)

    login = commands.add_parser("login", help="authorize or reuse a TDLib session")
    login.add_argument("--timeout", type=float, default=180.0)

    chats = commands.add_parser("chats", help="list chats visible to this account")
    chats.add_argument("--limit", type=int, default=100)
    chats.add_argument("--timeout", type=float, default=30.0)

    commands.add_parser("run", help="run the read-only Telegram service")

    history = commands.add_parser("history", help="sync one whitelisted chat")
    history.add_argument("--chat-id", type=int, required=True)
    history.add_argument("--limit", type=int, default=100)
    history.add_argument("--since-time", default="")

    status = commands.add_parser("status", help="read the local health snapshot")
    status.add_argument("--timeout", type=float, default=2.0)
    return parser


def _parse_since_time(value: str) -> datetime | None:
    clean = value.strip()
    if not clean:
        return None
    if clean.endswith("Z"):
        clean = clean[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(clean)
    except ValueError as exc:
        raise CLIUsageError("--since-time must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CLIUsageError("--since-time must include a timezone")
    return parsed.astimezone(timezone.utc)


def build_tdlib_client(
    settings: TelegramSettings,
    *,
    getpass_fn: Any = getpass.getpass,
) -> TDLibClient:
    settings.validate_runtime()
    settings.ensure_runtime_directories()
    transport = CTypesTDLibTransport(settings.tdlib_library_path)
    td_settings = TDLibSettings(
        api_id=settings.api_id,
        api_hash=settings.api_hash,
        database_directory=str(settings.tdlib_database_dir),
        files_directory=str(settings.tdlib_files_dir),
        database_encryption_key=settings.tdlib_database_encryption_key,
    )
    auth = InteractiveAuthCallbacks(
        phone_number=lambda: settings.phone,
        authentication_code=lambda: getpass_fn("Telegram login code: "),
        password=lambda: getpass_fn("Telegram 2FA password: "),
    )
    return TDLibClient(transport, td_settings, auth=auth)


def _load_runtime() -> Any:
    # Runtime is imported only for commands that need the long-running stack.
    return importlib.import_module(".runtime", __package__)


def _status_url(settings: TelegramSettings) -> str:
    host = settings.health_host.strip().lower()
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise CLICommandError("health endpoint is not configured on loopback")
    if host == "localhost":
        host = "127.0.0.1"
    display_host = f"[{host}]" if ":" in host else host
    return f"http://{display_host}:{settings.health_port}/status"


def read_local_status(settings: TelegramSettings, *, timeout: float = 2.0) -> Any:
    if type(timeout) not in {int, float} or isinstance(timeout, bool) or timeout <= 0:
        raise CLIUsageError("--timeout must be positive")
    request = urllib.request.Request(
        _status_url(settings),
        method="GET",
        headers={"Accept": "application/json"},
    )
    try:
        with _LOCAL_STATUS_OPENER.open(request, timeout=float(timeout)) as response:
            if int(getattr(response, "status", 0) or 0) != 200:
                raise CLICommandError("local health endpoint returned a non-200 status")
            raw = response.read(64 * 1024 + 1)
    except CLICommandError:
        raise
    except Exception as exc:
        raise CLICommandError("local health endpoint is unavailable") from exc
    if len(raw) > 64 * 1024:
        raise CLICommandError("local health response is too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise CLICommandError("local health response is invalid") from exc
    if type(payload) is not dict:
        raise CLICommandError("local health response is invalid")
    return payload


_SENSITIVE_KEY_WORDS = {
    "body",
    "code",
    "content",
    "credential",
    "hash",
    "key",
    "password",
    "path",
    "phone",
    "secret",
    "session",
    "text",
    "token",
}


def _safe_key(key: Any) -> str:
    clean = str(key)
    return clean[:120] if clean else "field"


def _is_sensitive_key(key: str) -> bool:
    words = {
        item
        for item in key.lower().replace("-", "_").split("_")
        if item
    }
    return bool(words & _SENSITIVE_KEY_WORDS)


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return "[TRUNCATED]"
    if dataclasses.is_dataclass(value):
        value = dataclasses.asdict(value)
    elif hasattr(value, "as_dict") and callable(value.as_dict):
        value = value.as_dict()
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return ""
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, str):
        return value[:1_000]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:200]:
            key = _safe_key(raw_key)
            if _is_sensitive_key(key):
                continue
            result[key] = _safe_value(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, depth=depth + 1) for item in value[:1_000]]
    return type(value).__name__


def _write_json(stream: Any, payload: dict[str, Any]) -> None:
    stream.write(
        json.dumps(
            _safe_value(payload),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        + "\n"
    )


def _public_error(exc: BaseException) -> dict[str, str]:
    if isinstance(exc, (CLIUsageError, TelegramSettingsError)):
        message = str(exc)[:300]
    elif isinstance(exc, CLICommandError):
        message = str(exc)[:300]
    else:
        message = "command failed"
    return {
        "type": type(exc).__name__,
        "message": message,
    }


def _normalized_argv(argv: list[str]) -> list[str]:
    if argv and argv[0].strip().lower() == "telegram":
        return argv[1:]
    return argv


def main(
    argv: list[str] | None = None,
    *,
    environ: dict[str, str] | None = None,
    stdout: Any = None,
    settings: TelegramSettings | None = None,
    client_factory: Any = None,
    runtime_loader: Any = None,
    status_reader: Any = None,
    getpass_fn: Any = None,
    load_local: bool = True,
) -> int:
    output = stdout or sys.stdout
    args_list = list(sys.argv[1:] if argv is None else argv)
    try:
        args = build_parser().parse_args(_normalized_argv(args_list))
        active_settings = settings or TelegramSettings.from_env(
            os.environ if environ is None else environ,
            load_local=load_local,
        )
        make_client = client_factory or (
            lambda value: build_tdlib_client(
                value,
                getpass_fn=getpass_fn or getpass.getpass,
            )
        )
        load_runtime = runtime_loader or _load_runtime

        if args.command == "login":
            if args.timeout <= 0:
                raise CLIUsageError("--timeout must be positive")
            client = make_client(active_settings)
            try:
                client.start()
                if not client.wait_ready(args.timeout):
                    raise CLICommandError("Telegram authorization did not become ready")
                result: Any = {
                    "ok": True,
                    "command": "login",
                    "authorization_state": client.authorization_state,
                    "network_state": client.connection_state,
                }
            finally:
                client.close()
        elif args.command == "chats":
            if not 1 <= args.limit <= 1_000 or args.timeout <= 0:
                raise CLIUsageError("chat limit/timeout is outside the allowed range")
            client = make_client(active_settings)
            try:
                client.start()
                if not client.wait_ready(args.timeout):
                    raise CLICommandError("Telegram authorization did not become ready")
                chats = client.list_chats(limit=args.limit, timeout=args.timeout)
                result = {
                    "ok": True,
                    "command": "chats",
                    "count": len(chats),
                    "chats": [
                        {
                            "chat_id": chat.chat_id,
                            "chat_title": chat.title,
                            "chat_type": chat.chat_type,
                        }
                        for chat in chats
                    ],
                }
            finally:
                client.close()
        elif args.command == "run":
            runtime = load_runtime()
            runtime_result = runtime.run_service(active_settings)
            result = {
                "ok": True,
                "command": "run",
                "result": runtime_result,
            }
        elif args.command == "history":
            if not 1 <= args.limit <= active_settings.history_max_limit:
                raise CLIUsageError(
                    f"--limit must be between 1 and {active_settings.history_max_limit}"
                )
            since_time = _parse_since_time(args.since_time)
            runtime = load_runtime()
            history_result = runtime.run_history(
                active_settings,
                chat_id=args.chat_id,
                limit=args.limit,
                since_time=since_time,
            )
            result = {
                "ok": True,
                "command": "history",
                "result": history_result,
            }
        else:
            reader = status_reader or read_local_status
            result = {
                "ok": True,
                "command": "status",
                "result": reader(active_settings, timeout=args.timeout),
            }
        _write_json(output, result)
        return 0
    except Exception as exc:
        _write_json(
            output,
            {
                "ok": False,
                "error": _public_error(exc),
            },
        )
        return 2 if isinstance(exc, CLIUsageError) else 1


__all__ = [
    "CLICommandError",
    "CLIUsageError",
    "build_parser",
    "build_tdlib_client",
    "main",
    "read_local_status",
]
