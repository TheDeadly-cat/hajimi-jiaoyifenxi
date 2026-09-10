from __future__ import annotations

import json
import math
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit


LOG_SCHEMA_VERSION = "studio_log_event_v1"
REDACTED = "[REDACTED]"
REDACTED_PATH = "[REDACTED_PATH]"
REDACTED_URL = "[REDACTED_URL]"
TRUNCATED = "[TRUNCATED]"

_MAX_DEPTH = 4
_MAX_ITEMS = 24
_MAX_KEY_LENGTH = 64
_MAX_STRING_LENGTH = 256
_EVENT_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_WINDOWS_PATH_RE = re.compile(
    r"(?:^|[\s\"'])(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/])"
)
_AUTHORIZATION_VALUE_RE = re.compile(
    r"\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}",
    flags=re.IGNORECASE,
)
_HIGH_CONFIDENCE_SECRET_RE = re.compile(
    r"(?:"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    r"|github_pat_[A-Za-z0-9_]{40,}"
    r"|gh[pousr]_[A-Za-z0-9]{30,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{35}"
    r"|sk-(?:proj-)?[A-Za-z0-9_-]{32,}"
    r"|xox[baprs]-[A-Za-z0-9-]{20,}"
    r")"
)
_SEVERITIES = frozenset({"debug", "info", "warning", "error", "critical"})
_HTTP_METHODS = frozenset({"GET", "POST", "PATCH", "DELETE", "OPTIONS", "HEAD"})
_API_RESOURCE_CLASSES = frozenset({
    "action-desk",
    "bootstrap",
    "health",
    "integration",
    "market",
    "materials",
    "models",
    "monitoring",
    "observations",
    "plugins",
    "providers",
    "readiness",
    "research",
    "rooms",
    "version",
})
_WRITE_LOCK = threading.Lock()


def _is_sensitive_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    tokens = set(normalized.split("_")) if normalized else set()
    if tokens.intersection({
        "authorization",
        "body",
        "cookie",
        "credential",
        "credentials",
        "header",
        "headers",
        "password",
        "secret",
        "secrets",
        "token",
        "tokens",
    }):
        return True
    if "api_key" in normalized or "private_key" in normalized:
        return True
    if normalized in {"database", "database_path", "db", "db_path", "sqlite_path"}:
        return True
    return "provider" in tokens and bool(
        tokens.intersection({"payload", "request", "response"})
    )


def _safe_key(value: Any) -> str:
    clean = _SAFE_KEY_RE.sub("_", str(value or "field")).strip("_.-")
    return (clean or "field")[:_MAX_KEY_LENGTH]


def _safe_string(value: str) -> str:
    if _AUTHORIZATION_VALUE_RE.search(value) or _HIGH_CONFIDENCE_SECRET_RE.search(value):
        return REDACTED
    if _WINDOWS_PATH_RE.search(value) or value.startswith(("/home/", "/Users/")):
        return REDACTED_PATH
    if "://" in value:
        return REDACTED_URL
    if len(value) > _MAX_STRING_LENGTH:
        return value[:_MAX_STRING_LENGTH] + TRUNCATED
    return value


def _sanitize_value(value: Any, *, depth: int) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else "[NON_FINITE_NUMBER]"
    if isinstance(value, Path):
        return REDACTED_PATH
    if isinstance(value, BaseException):
        return {"exception_type": type(value).__name__}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "[BINARY_REDACTED]"
    if isinstance(value, str):
        return _safe_string(value)
    if depth >= _MAX_DEPTH:
        return TRUNCATED
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        items = sorted(value.items(), key=lambda item: str(item[0]))
        for raw_key, raw_value in items[:_MAX_ITEMS]:
            original_key = str(raw_key)
            key = _safe_key(original_key)
            if key in result:
                continue
            result[key] = (
                REDACTED
                if _is_sensitive_key(original_key)
                else _sanitize_value(raw_value, depth=depth + 1)
            )
        if len(items) > _MAX_ITEMS:
            result["truncated_items"] = len(items) - _MAX_ITEMS
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        values = list(value)
        result = [
            _sanitize_value(item, depth=depth + 1)
            for item in values[:_MAX_ITEMS]
        ]
        if len(values) > _MAX_ITEMS:
            result.append(TRUNCATED)
        return result
    return {"value_type": type(value).__name__}


def sanitize_fields(fields: dict[str, Any] | None) -> dict[str, Any]:
    """Return bounded JSON fields without credential or payload material."""

    if not fields:
        return {}
    sanitized = _sanitize_value(fields, depth=0)
    return sanitized if isinstance(sanitized, dict) else {}


def classify_request_target(target: Any) -> str:
    """Classify a request without retaining IDs, query strings, or fragments."""

    try:
        path = urlsplit(str(target or "")).path
    except ValueError:
        return "invalid"
    if path == "/":
        return "frontend:index"
    if path.startswith("/assets/"):
        return "frontend:asset"
    segments = [segment for segment in path.split("/") if segment]
    if not segments or segments[0] != "api":
        return "frontend:route" if path.startswith("/") else "invalid"
    if len(segments) == 1:
        return "api:root"
    resource = segments[1].casefold()
    return f"api:{resource}" if resource in _API_RESOURCE_CLASSES else "api:other"


def safe_http_method(value: Any) -> str:
    method = str(value or "").strip().upper()
    return method if method in _HTTP_METHODS else "OTHER"


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def emit_event(
    event: str,
    *,
    severity: str = "info",
    fields: dict[str, Any] | None = None,
    stream: TextIO | None = None,
) -> dict[str, Any]:
    """Write one bounded JSONL event and return its sanitized payload."""

    clean_event = str(event or "").strip().casefold()
    if _EVENT_RE.fullmatch(clean_event) is None:
        clean_event = "invalid_event_name"
    clean_severity = str(severity or "").strip().casefold()
    if clean_severity not in _SEVERITIES:
        clean_severity = "info"
    payload: dict[str, Any] = {
        "schema_version": LOG_SCHEMA_VERSION,
        "timestamp_utc": _utc_now(),
        "severity": clean_severity,
        "event": clean_event,
    }
    safe_fields = sanitize_fields(fields)
    if safe_fields:
        payload["fields"] = safe_fields
    line = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    target = stream if stream is not None else sys.stdout
    try:
        with _WRITE_LOCK:
            target.write(line + "\n")
            target.flush()
    except (OSError, ValueError):
        pass
    return payload
