"""Pre-connect redirect policy for fixed official HTTPS sources."""

from __future__ import annotations

import string
import math
import socket
import threading
import time
from collections.abc import Callable, Collection
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urljoin, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..source_poll_control import (
    SourcePollCancelled,
    SourcePollDeadlineExceeded,
    ensure_source_poll_active,
    source_poll_timeout_seconds,
    validate_source_poll_control,
)


class OfficialHttpsRedirectHandler(HTTPRedirectHandler):
    """Reject an unsafe redirect before urllib sends the redirected request."""

    max_redirections = 5
    max_repeats = 2

    def __init__(
        self,
        allowed_hosts: Collection[str],
        *,
        url_validator: Callable[[str], bool] | None = None,
        deadline_monotonic_ms: int = 0,
        cancel_event: threading.Event | None = None,
    ) -> None:
        super().__init__()
        if type(allowed_hosts) not in {set, frozenset, list, tuple}:
            raise TypeError("allowed_hosts must be a native collection")
        hosts: list[str] = []
        for host in allowed_hosts:
            if (
                type(host) is not str
                or not host
                or host != host.lower()
                or urlparse(f"https://{host}").hostname != host
                or host in hosts
            ):
                raise ValueError("allowed_hosts contains a noncanonical host")
            hosts.append(host)
        if not hosts:
            raise ValueError("allowed_hosts must not be empty")
        if url_validator is not None and not callable(url_validator):
            raise TypeError("url_validator must be callable")
        deadline, event = validate_source_poll_control(
            deadline_monotonic_ms=deadline_monotonic_ms,
            cancel_event=cancel_event,
        )
        self._allowed_hosts = frozenset(hosts)
        self._url_validator = url_validator
        self._deadline_monotonic_ms = deadline
        self._cancel_event = event

    def validate_url(self, value: Any) -> str:
        if type(value) is not str or not value or value != value.strip():
            raise ValueError("official source URL must be a canonical native string")
        try:
            parsed = urlparse(value)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("official source URL has an invalid port") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname not in self._allowed_hosts
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or not parsed.path.startswith("/")
            or parsed.fragment
        ):
            raise ValueError("official source URL is outside the fixed HTTPS policy")
        if self._url_validator is not None and self._url_validator(value) is not True:
            raise ValueError("official source URL is outside the fixed endpoint policy")
        return value

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

    @staticmethod
    def _close_redirect_response(fp: Any) -> None:
        try:
            fp.close()
        except Exception:
            pass

    def http_error_302(self, req, fp, code, msg, headers):
        """Follow one checked redirect without draining its unbounded body."""

        try:
            if "location" in headers:
                newurl = headers["location"]
            elif "uri" in headers:
                newurl = headers["uri"]
            else:
                raise ValueError("official source redirect is missing Location")
            if type(newurl) is not str:
                raise ValueError("official source redirect Location must be text")
            urlparts = urlparse(newurl)
            if not urlparts.path and urlparts.netloc:
                mutable_parts = list(urlparts)
                mutable_parts[2] = "/"
                urlparts = urlparse(urlunparse(mutable_parts))
            normalized = quote(
                urlunparse(urlparts),
                encoding="iso-8859-1",
                safe=string.punctuation,
            )
            normalized = urljoin(req.full_url, normalized)
            self.validate_url(normalized)
            new_request = self.redirect_request(
                req,
                fp,
                code,
                msg,
                headers,
                normalized,
            )
            if new_request is None:
                raise ValueError("official source redirect could not be represented")
            if hasattr(req, "redirect_dict"):
                visited = new_request.redirect_dict = req.redirect_dict
                if (
                    visited.get(normalized, 0) >= self.max_repeats
                    or len(visited) >= self.max_redirections
                ):
                    raise HTTPError(
                        req.full_url,
                        code,
                        self.inf_msg + msg,
                        headers,
                        fp,
                    )
            else:
                visited = new_request.redirect_dict = req.redirect_dict = {}
            visited[normalized] = visited.get(normalized, 0) + 1
            redirect_timeout = source_poll_timeout_seconds(
                req.timeout,
                deadline_monotonic_ms=self._deadline_monotonic_ms,
                cancel_event=self._cancel_event,
            )
        except Exception:
            self._close_redirect_response(fp)
            raise

        self._close_redirect_response(fp)
        return self.parent.open(new_request, timeout=redirect_timeout)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def open_official_https(
    request: Request,
    *,
    allowed_hosts: Collection[str],
    timeout: float,
    url_validator: Callable[[str], bool] | None = None,
    deadline_monotonic_ms: int = 0,
    cancel_event: threading.Event | None = None,
):
    """Open one fixed official request with every redirect checked first."""

    if type(request) is not Request:
        raise TypeError("request must be an exact urllib.request.Request")
    if type(timeout) not in {int, float} or type(timeout) is bool or timeout <= 0:
        raise ValueError("timeout must be a positive native number")
    deadline, event = validate_source_poll_control(
        deadline_monotonic_ms=deadline_monotonic_ms,
        cancel_event=cancel_event,
    )
    effective_timeout = source_poll_timeout_seconds(
        timeout,
        deadline_monotonic_ms=deadline,
        cancel_event=event,
    )
    redirect_policy = OfficialHttpsRedirectHandler(
        allowed_hosts,
        url_validator=url_validator,
        deadline_monotonic_ms=deadline,
        cancel_event=event,
    )
    redirect_policy.validate_url(request.full_url)
    opener = build_opener(redirect_policy)
    response = opener.open(request, timeout=effective_timeout)
    try:
        ensure_source_poll_active(
            deadline_monotonic_ms=deadline,
            cancel_event=event,
        )
        redirect_policy.validate_url(response.geturl())
    except Exception:
        response.close()
        raise
    return response


def _response_socket(response: Any) -> Any | None:
    fp = getattr(response, "fp", None)
    raw = getattr(fp, "raw", None)
    sock = getattr(raw, "_sock", None)
    if sock is None:
        return None
    if not callable(getattr(sock, "shutdown", None)):
        return None
    return sock


def _interrupt_response(response: Any) -> None:
    sock = _response_socket(response)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        closer = getattr(sock, "_real_close", None)
        if callable(closer):
            try:
                closer()
            except OSError:
                pass
    closer = getattr(response, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            pass


def read_official_https_body(
    response: Any,
    maximum: int,
    *,
    deadline_seconds: int | float,
    deadline_monotonic_ms: int = 0,
    cancel_event: threading.Event | None = None,
) -> bytes:
    """Read a bounded body while a watcher can close its socket on cancel.

    The per-body deadline and the outer poll deadline are both absolute wall
    bounds.  Closing the active HTTPS socket interrupts a blocking ``read``;
    no helper survives after a successful or failed read.
    """

    if type(maximum) is not int or maximum <= 0:
        raise ValueError("official source response byte limit is invalid")
    if (
        type(deadline_seconds) not in {int, float}
        or isinstance(deadline_seconds, bool)
        or not math.isfinite(float(deadline_seconds))
        or float(deadline_seconds) <= 0
    ):
        raise ValueError("official source response body deadline is invalid")
    reader = getattr(response, "read", None)
    if not callable(reader):
        raise ValueError("official source response lacks a bounded body reader")
    deadline, event = validate_source_poll_control(
        deadline_monotonic_ms=deadline_monotonic_ms,
        cancel_event=cancel_event,
    )
    now_ms = ensure_source_poll_active(
        deadline_monotonic_ms=deadline,
        cancel_event=event,
    )
    body_deadline_ms = now_ms + int(float(deadline_seconds) * 1_000)
    effective_deadline_ms = (
        min(body_deadline_ms, deadline) if deadline else body_deadline_ms
    )
    completed = threading.Event()
    interrupted = threading.Event()
    reason: list[str] = []

    def watch() -> None:
        while not completed.wait(0.025):
            if event is not None and event.is_set():
                reason.append("cancelled")
                interrupted.set()
                _interrupt_response(response)
                return
            if int(time.monotonic() * 1_000) >= effective_deadline_ms:
                reason.append("deadline")
                interrupted.set()
                _interrupt_response(response)
                return

    watcher = threading.Thread(
        target=watch,
        name="official-source-body-control",
        daemon=True,
    )
    watcher.start()
    try:
        try:
            body = reader(maximum + 1)
        except Exception as exc:
            if interrupted.is_set() and reason[:1] == ["cancelled"]:
                raise SourcePollCancelled(
                    "SOURCE_MONITORING_POLL_CANCELLED",
                    "official source body read was cancelled",
                ) from exc
            if interrupted.is_set():
                raise SourcePollDeadlineExceeded(
                    "SOURCE_MONITORING_POLL_DEADLINE_EXCEEDED",
                    "official source response body exceeded its sealed deadline",
                ) from exc
            raise
    finally:
        completed.set()
        watcher.join(timeout=0.2)
    if watcher.is_alive():
        _interrupt_response(response)
        raise TimeoutError("official source body watcher did not stop")
    if interrupted.is_set() and reason[:1] == ["cancelled"]:
        raise SourcePollCancelled(
            "SOURCE_MONITORING_POLL_CANCELLED",
            "official source body read was cancelled",
        )
    if interrupted.is_set() or int(time.monotonic() * 1_000) >= effective_deadline_ms:
        raise SourcePollDeadlineExceeded(
            "SOURCE_MONITORING_POLL_DEADLINE_EXCEEDED",
            "official source response body exceeded its sealed deadline",
        )
    ensure_source_poll_active(
        deadline_monotonic_ms=deadline,
        cancel_event=event,
    )
    if type(body) is not bytes:
        raise TypeError("official source response returned non-bytes data")
    if len(body) > maximum:
        raise ValueError("official source response exceeds its sealed byte limit")
    return body


__all__ = [
    "OfficialHttpsRedirectHandler",
    "open_official_https",
    "read_official_https_body",
]
