from __future__ import annotations

import ipaddress
import json
import socket
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


_MAX_COUNT = 2**63 - 1
_SAFE_STATES = {
    "tdlib_status",
    "authorization_state",
    "network_state",
    "ai_status",
    "bot_status",
    "worker_status",
    "storage_status",
}
_COUNT_FIELDS = {
    "queue_size",
    "retry_jobs",
    "dead_letter_jobs",
    "messages_received",
    "messages_processed",
    "dropped_updates",
    "database_bytes",
    "database_limit_bytes",
}


class HealthStateError(ValueError):
    pass


def _utc(value: datetime, field: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise HealthStateError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_state(value: Any, field: str) -> str:
    if type(value) is not str:
        raise HealthStateError(f"{field} must be text")
    clean = value.strip()
    if not clean or len(clean) > 80:
        raise HealthStateError(f"{field} must be bounded")
    if not all(char.isalnum() or char in "._:-" for char in clean):
        raise HealthStateError(f"{field} contains unsafe characters")
    return clean


def _bounded_count(value: Any, field: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_COUNT:
        raise HealthStateError(f"{field} must be a non-negative bounded integer")
    return value


def _loopback_host(host: str) -> str:
    if type(host) is not str:
        raise HealthStateError("health host must be loopback")
    clean = host.strip().lower()
    if clean == "localhost":
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(clean)
    except ValueError as exc:
        raise HealthStateError("health host must be loopback") from exc
    if not address.is_loopback:
        raise HealthStateError("health host must be loopback")
    return clean


class HealthState:
    """Thread-safe, content-free operational counters for local monitoring."""

    def __init__(
        self,
        *,
        wall_clock: Any = None,
        monotonic_clock: Any = None,
    ) -> None:
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._monotonic_clock = monotonic_clock or time.monotonic
        started_at = self._wall_clock()
        self._started_at = _utc(started_at, "started_at")
        self._started_monotonic = float(self._monotonic_clock())
        self._lock = threading.Lock()
        self._states = {
            "tdlib_status": "not_started",
            "authorization_state": "unknown",
            "network_state": "unknown",
            "ai_status": "disabled",
            "bot_status": "disabled",
            "worker_status": "not_started",
            # Compatibility default for isolated/fake repositories. Production
            # runtimes overwrite this from TelegramRepository.storage_usage().
            "storage_status": "ready",
        }
        self._counts = {
            "queue_size": 0,
            "retry_jobs": 0,
            "dead_letter_jobs": 0,
            "messages_received": 0,
            "messages_processed": 0,
            "dropped_updates": 0,
            "database_bytes": 0,
            "database_limit_bytes": 0,
        }
        self._last_message_time: datetime | None = None
        self._last_alert_time: datetime | None = None

    def set_states(self, **values: str) -> None:
        unknown = set(values) - _SAFE_STATES
        if unknown:
            raise HealthStateError(f"unknown health state: {sorted(unknown)[0]}")
        cleaned = {
            field: _safe_state(value, field) for field, value in values.items()
        }
        with self._lock:
            self._states.update(cleaned)

    def set_counts(self, **values: int) -> None:
        unknown = set(values) - _COUNT_FIELDS
        if unknown:
            raise HealthStateError(f"unknown health count: {sorted(unknown)[0]}")
        cleaned = {
            field: _bounded_count(value, field) for field, value in values.items()
        }
        with self._lock:
            self._counts.update(cleaned)

    def increment(self, field: str, amount: int = 1) -> int:
        if field not in {"messages_received", "messages_processed"}:
            raise HealthStateError("only message counters may be incremented")
        if type(amount) is not int or amount < 0:
            raise HealthStateError("increment must be non-negative")
        with self._lock:
            result = self._counts[field] + amount
            self._counts[field] = _bounded_count(result, field)
            return result

    def record_message(self, timestamp: datetime | None = None) -> None:
        when = _utc(timestamp or self._wall_clock(), "last_message_time")
        with self._lock:
            self._counts["messages_received"] = _bounded_count(
                self._counts["messages_received"] + 1,
                "messages_received",
            )
            if self._last_message_time is None or when > self._last_message_time:
                self._last_message_time = when

    def record_processed(self, amount: int = 1) -> None:
        self.increment("messages_processed", amount)

    def record_alert(self, timestamp: datetime | None = None) -> None:
        when = _utc(timestamp or self._wall_clock(), "last_alert_time")
        with self._lock:
            if self._last_alert_time is None or when > self._last_alert_time:
                self._last_alert_time = when

    def update_tdlib(self, client: Any) -> None:
        authorization = str(getattr(client, "authorization_state", "unknown"))
        network = str(getattr(client, "connection_state", "unknown"))
        if bool(getattr(client, "is_ready", False)):
            tdlib_status = "ready"
        elif bool(getattr(client, "is_running", False)):
            tdlib_status = "running"
        else:
            tdlib_status = "stopped"
        self.set_states(
            tdlib_status=tdlib_status,
            authorization_state=authorization,
            network_state=network,
        )
        dropped = getattr(client, "dropped_update_count", 0)
        self.set_counts(
            dropped_updates=(
                dropped
                if type(dropped) is int and 0 <= dropped <= _MAX_COUNT
                else 0
            )
        )

    def snapshot(self) -> dict[str, Any]:
        now_monotonic = float(self._monotonic_clock())
        uptime = max(0.0, now_monotonic - self._started_monotonic)
        with self._lock:
            states = dict(self._states)
            counts = dict(self._counts)
            last_message = self._last_message_time
            last_alert = self._last_alert_time
        ready = (
            states["tdlib_status"] == "ready"
            and states["authorization_state"] == "authorizationStateReady"
            and states["network_state"] == "connectionStateReady"
            and states["worker_status"] == "ready"
            and states["storage_status"] == "ready"
            and states["ai_status"] in {"disabled", "ready"}
            and states["bot_status"] in {"disabled", "dry_run", "ready"}
            and counts["dropped_updates"] == 0
            and counts["dead_letter_jobs"] == 0
        )
        return {
            "version": "telegram_health_v1",
            "status": "ready" if ready else "degraded",
            **states,
            **counts,
            "last_message_time": _iso(last_message),
            "last_alert_time": _iso(last_alert),
            "started_at": _iso(self._started_at),
            "uptime_seconds": round(uptime, 3),
        }


class _HealthHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _HealthHTTPServerV6(_HealthHTTPServer):
    address_family = socket.AF_INET6


def _handler(state: HealthState) -> type[BaseHTTPRequestHandler]:
    class HealthHandler(BaseHTTPRequestHandler):
        server_version = "TelegramHealth/1"
        sys_version = ""

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path not in {"/health", "/status"}:
                self._send(
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error": "not_found"},
                )
                return
            snapshot = state.snapshot()
            if path == "/health":
                payload = {
                    "ok": snapshot["status"] == "ready",
                    "status": snapshot["status"],
                    "uptime_seconds": snapshot["uptime_seconds"],
                }
            else:
                payload = {"ok": True, **snapshot}
            self._send(HTTPStatus.OK, payload)

        def _send(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            # Request paths and client data are intentionally not logged here.
            return

    return HealthHandler


class HealthServer:
    def __init__(
        self,
        state: HealthState,
        *,
        host: str = "127.0.0.1",
        port: int = 8781,
    ) -> None:
        self.state = state
        self.host = _loopback_host(host)
        if type(port) is not int or not 0 <= port <= 65_535:
            raise HealthStateError("health port must be between 0 and 65535")
        server_class = _HealthHTTPServerV6 if ":" in self.host else _HealthHTTPServer
        self._server = server_class((self.host, port), _handler(state))
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="telegram-health-server",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            self._server.shutdown()
            thread.join(timeout=2.0)
        self._server.server_close()

    def __enter__(self) -> "HealthServer":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


__all__ = ["HealthServer", "HealthState", "HealthStateError"]
