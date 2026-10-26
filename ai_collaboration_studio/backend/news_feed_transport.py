"""Bounded official-feed transport and allowlisted, non-secret diagnostics.

The response body is returned only for the caller's RSS parser. No response body,
raw header collection, proxy URL or exception text is retained in diagnostics.
Importing this module performs no network, configuration or filesystem access.
"""
from __future__ import annotations

import errno
import math
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate, parsedate_to_datetime
from urllib.parse import urljoin, urlsplit


SOURCES = (
    {"id": "fed", "name": "美联储", "url": "https://www.federalreserve.gov/feeds/press_all.xml"},
    {"id": "bls", "name": "美国劳工统计局", "url": "https://www.bls.gov/feed/bls_latest.rss"},
    {"id": "seagate", "name": "希捷公司公告", "url": "https://investors.seagate.com/rss/pressrelease.aspx"},
)
_HOSTS = frozenset(urlsplit(source["url"]).hostname for source in SOURCES)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MIN_TIMESTAMP = -62135596800.0
_MAX_TIMESTAMP = 253402300799.0
FEED_ERROR_KINDS = frozenset({
    "timeout", "dns_error", "tls_error", "connection_error", "redirect_blocked",
    "feed_too_large", "invalid_feed", "unsafe_feed", "transport_error", "http_error", "proxy_configuration_error",
})
_VALIDATION_ERROR_KINDS = frozenset({
    "redirect_blocked", "feed_too_large", "invalid_feed", "unsafe_feed", "proxy_configuration_error",
})
PROXY_CONFIGURATION_INTENTS = frozenset({
    "https_proxy", "all_proxy_fallback", "https_not_configured", "not_configured",
    "invalid_configuration", "configuration_unavailable", "unknown",
})
_CONNECTION_ERRNOS = frozenset({
    errno.ECONNREFUSED, errno.ECONNRESET, errno.ECONNABORTED, errno.ENETUNREACH,
    errno.EHOSTUNREACH, errno.ENETDOWN, errno.EPIPE, 10051, 10053, 10054, 10061, 10065,
})


class FeedValidationError(ValueError):
    """Only a reviewed category may cross the feed-validation boundary."""
    def __init__(self, code):
        if not isinstance(code, str) or code not in _VALIDATION_ERROR_KINDS:
            raise ValueError("invalid_feed_validation_code")
        self.code = code
        super().__init__(code)


def validate_loopback_proxy(value):
    """Validate in memory only; never return configuration text in an error."""
    try:
        if (not isinstance(value, str) or not value or len(value) > 2048
                or any(ord(char) <= 32 or ord(char) >= 127 for char in value) or "\\" in value):
            raise ValueError
        proxy = urlsplit(value)
        if (proxy.scheme not in {"http", "https"} or proxy.hostname not in _LOOPBACK_HOSTS
                or proxy.port is None or not 1 <= proxy.port <= 65535
                or proxy.username is not None or proxy.password is not None
                or proxy.path not in {"", "/"} or proxy.query or proxy.fragment):
            raise ValueError
    except (TypeError, ValueError):
        raise FeedValidationError("proxy_configuration_error") from None
    return value


def validate_bypass_value(value):
    # Keep explicit NO_PROXY semantics verbatim; do not persist its rules.
    if (not isinstance(value, str) or len(value) > 8192
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise FeedValidationError("proxy_configuration_error")
    return value


def feed_proxy_configuration(view):
    """Resolve one effective stdlib getproxies() view without silent fallback.

    HTTPS wins by key presence, including invalid values. ALL is considered only
    without HTTPS. HTTP-only configuration is not an HTTPS proxy. No values from
    this view are copied into the environment: doing so would hide Windows
    registry ProxyOverride rules from urllib's standard proxy_bypass decision.
    """
    if not isinstance(view, Mapping):
        raise FeedValidationError("proxy_configuration_error")
    if "no" in view:
        validate_bypass_value(view["no"])
    if "https" in view:
        proxies = {"https": validate_loopback_proxy(view["https"])}
        intent = "https_proxy"
    elif "all" in view:
        proxies = {"https": validate_loopback_proxy(view["all"])}
        intent = "all_proxy_fallback"
    else:
        proxies = {}
        intent = "https_not_configured" if "http" in view else "not_configured"
    return proxies, {"proxy_configuration_intent": intent,
                     "bypass_configured": "no" in view}


def classify_feed_error(exc):
    """Classify exception types/codes without reading arbitrary exception text."""
    seen = set()
    for _ in range(4):
        if id(exc) in seen:
            break
        seen.add(id(exc))
        if isinstance(exc, FeedValidationError):
            return exc.code if exc.code in _VALIDATION_ERROR_KINDS else "transport_error"
        if isinstance(exc, urllib.error.HTTPError):
            return "http_error"
        if isinstance(exc, TimeoutError):
            return "timeout"
        if isinstance(exc, socket.gaierror):
            return "dns_error"
        if isinstance(exc, ssl.SSLError):
            return "tls_error"
        if isinstance(exc, ConnectionError):
            return "connection_error"
        if isinstance(exc, OSError):
            if exc.errno in (errno.ETIMEDOUT, 10060):
                return "timeout"
            if exc.errno in _CONNECTION_ERRNOS:
                return "connection_error"
        if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
            exc = exc.reason
            continue
        break
    return "transport_error"


def _header_values(headers, name):
    if headers is None:
        return []
    if hasattr(headers, "get_all"):
        return list(headers.get_all(name, []))
    if not hasattr(headers, "items"):
        return []
    return [value for key, value in headers.items() if isinstance(key, str) and key.lower() == name.lower()]


def _single_header(headers, name):
    values = _header_values(headers, name)
    if not values:
        return "missing", None
    if len(values) != 1 or not isinstance(values[0], str):
        return "invalid", None
    value = values[0]
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return "invalid", None
    return "present", value.strip()


def _http_date(value):
    if not isinstance(value, str) or not value or len(value) > 512:
        return "invalid", None
    try:
        # parsedate also exposes an otherwise valid date with an unrepresentable year.
        parts = parsedate(value)
        if parts is not None and not 1 <= parts[0] <= 9999:
            return "out_of_range", None
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        stamp = (parsed - _EPOCH).total_seconds()
    except (ValueError, TypeError, IndexError):
        return "invalid", None
    except OverflowError:
        return "out_of_range", None
    if not math.isfinite(stamp) or not _MIN_TIMESTAMP <= stamp <= _MAX_TIMESTAMP:
        return "out_of_range", None
    return "valid", stamp


def parse_retry_after(headers, now):
    """Normalize Retry-After without shortening a representable server deadline.

    For HTTP dates, use both the local deadline and the server Date-relative wait.
    Taking their maximum is deliberately conservative when the two clocks differ.
    An unrepresentable valid delay requires an operator decision, not a capped wait.
    """
    result = {"status": "missing", "kind": "none", "retry_at": None,
              "delay_seconds": None, "manual_hold": False}
    present, value = _single_header(headers, "Retry-After")
    if present == "missing":
        return result
    result["status"] = "invalid"
    if present != "present" or not value:
        return result
    is_seconds = re.fullmatch(r"[0-9]+", value) is not None
    result["kind"] = "seconds" if is_seconds else "date"
    if not isinstance(now, (int, float)) or isinstance(now, bool):
        result.update(status="out_of_range", manual_hold=True)
        return result
    if not _MIN_TIMESTAMP <= now <= _MAX_TIMESTAMP or not math.isfinite(now):
        result.update(status="out_of_range", manual_hold=True)
        return result
    if is_seconds:
        digits = value.lstrip("0") or "0"
        if len(digits) > 18:
            result.update(status="out_of_range", manual_hold=True)
            return result
        delay = int(digits)
    else:
        status, deadline = _http_date(value)
        if status != "valid":
            result.update(status=status, manual_hold=status == "out_of_range")
            return result
        delay = max(0.0, deadline - now)
        date_present, date_value = _single_header(headers, "Date")
        date_status, server_now = _http_date(date_value) if date_present == "present" else ("invalid", None)
        if date_status == "valid":
            delay = max(delay, deadline - server_now)
    retry_at = now + delay
    if not math.isfinite(retry_at) or not _MIN_TIMESTAMP <= retry_at <= _MAX_TIMESTAMP:
        result.update(status="out_of_range", manual_hold=True)
        return result
    result.update(status="valid", retry_at=retry_at, delay_seconds=delay)
    return result


def failure_retry_after(headers, diagnostics, now):
    """Keep a parsed deadline when failure prevents returning the response.

    Only the normalized parser result is consumed; raw headers and exception
    text are never recovered from diagnostics. A received absolute deadline is
    not restarted at failure time. Any available valid constraints are merged
    conservatively, and an out-of-range hold cannot be replaced by a timer.
    """
    result = parse_retry_after(headers, now)
    observed = diagnostics.get("retry_after") if isinstance(diagnostics, dict) else None
    if not isinstance(observed, dict) or observed.get("kind") not in ("seconds", "date"):
        return result
    if (observed.get("status") == "out_of_range" and observed.get("manual_hold") is True
            and observed.get("retry_at") is None and observed.get("delay_seconds") is None):
        result.update(status="out_of_range", kind=observed["kind"], manual_hold=True)
        return result
    deadline, delay = observed.get("retry_at"), observed.get("delay_seconds")
    if (observed.get("status") != "valid" or observed.get("manual_hold") is not False
            or type(deadline) not in (int, float) or type(delay) not in (int, float)
            or not _MIN_TIMESTAMP <= deadline <= _MAX_TIMESTAMP
            or not 0 <= delay <= _MAX_TIMESTAMP - _MIN_TIMESTAMP
            or not math.isfinite(deadline) or not math.isfinite(delay)):
        return result
    if result["retry_at"] is None or deadline > result["retry_at"]:
        result.update(kind=observed["kind"], retry_at=deadline, delay_seconds=delay)
        if not result["manual_hold"]:
            result["status"] = "valid"
    return result


def _safe_url(value, base):
    if not value or any(ord(char) < 32 for char in value):
        return ""
    try:
        result = urljoin(base, value.strip())
        target, origin = urlsplit(result), urlsplit(base)
        if (target.scheme != "https" or target.hostname != origin.hostname
                or target.port not in (None, 443) or target.username or target.password):
            return ""
        return result
    except ValueError:
        return ""


class _Diagnostics:
    def __init__(self, request_headers):
        names = {key.lower() for key in request_headers if isinstance(key, str)}
        self.started = time.monotonic()
        self.routes = set()
        self.bypasses = set()
        self.redirect_count = 0
        self.final_host = None
        self.response_date = None
        self.error_kind = None
        self.phase = "proxy_configuration"
        self.proxy_configuration_intent = "unknown"
        # This records an explicit NO_PROXY key in the effective view, not
        # whether Windows has additional registry ProxyOverride rules.
        self.bypass_configured = None
        self.response_status = None
        self.retry_after = parse_retry_after(None, 0)
        self.conditional_etag = "if-none-match" in names
        self.conditional_modified_since = "if-modified-since" in names

    def observe_route(self, route, bypass):
        self.routes.add(route)
        if bypass is not None:
            self.bypasses.add(bypass)

    def observe_response(self, url, origin, headers, status=None):
        if type(status) is int and 100 <= status <= 599:
            self.response_status = status
        if _safe_url(url, origin):
            host = urlsplit(url).hostname
            self.final_host = host if host in _HOSTS else None
        present, value = _single_header(headers, "Date")
        status, stamp = _http_date(value) if present == "present" else ("invalid", None)
        if status == "valid":
            self.response_date = (_EPOCH + timedelta(seconds=stamp)).isoformat().replace("+00:00", "Z")
        self.retry_after = parse_retry_after(headers, time.time())

    def snapshot(self):
        route = next(iter(self.routes)) if len(self.routes) == 1 else "unknown"
        bypass = next(iter(self.bypasses)) if len(self.bypasses) == 1 else None
        return {"schema_version": "news_feed_diagnostics_v1", "route_class": route,
                "proxy_configuration_intent": self.proxy_configuration_intent,
                "bypass_configured": self.bypass_configured,
                "effective_bypass": bypass, "redirect_count": self.redirect_count,
                "final_host": self.final_host, "elapsed_ms": max(0, round((time.monotonic() - self.started) * 1000)),
                "conditional_etag": self.conditional_etag,
                "conditional_modified_since": self.conditional_modified_since,
                "response_date": self.response_date, "retry_after": dict(self.retry_after),
                "error_kind": self.error_kind, "phase": self.phase,
                "response_status": self.response_status}


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 1

    def __init__(self, origin, diagnostics=None):
        super().__init__()
        self.origin = origin
        self.diagnostics = diagnostics

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _safe_url(newurl, self.origin):
            if self.diagnostics is not None:
                self.diagnostics.phase = "redirect"
                if type(code) is int and 100 <= code <= 599:
                    self.diagnostics.response_status = code
            raise FeedValidationError("redirect_blocked")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and self.diagnostics is not None:
            self.diagnostics.redirect_count += 1
        return redirected


class _ObservedProxyHandler(urllib.request.ProxyHandler):
    """Observe the base handler's actual bypass decision without re-evaluating it."""
    def proxy_open(self, req, proxy, type):
        previous_host = req.host
        result = super().proxy_open(req, proxy, type)
        # All accepted proxies are loopback and all feed hosts are remote HTTPS.
        # An unchanged host therefore means the base handler bypassed this proxy.
        req._news_effective_bypass = req.host == previous_host
        return result


class _ObservedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, diagnostics):
        super().__init__()
        self.diagnostics = diagnostics

    def https_open(self, req):
        # This is the selected network handler, not just configured proxy intent.
        # It identifies the attempted route; it does not claim the peer was reached.
        host = urlsplit("https://" + req.host).hostname if req.host else None
        tunnel = getattr(req, "_tunnel_host", None)
        tunnel_host = urlsplit("https://" + tunnel).hostname if tunnel else None
        if host in _LOOPBACK_HOSTS and tunnel_host in _HOSTS:
            route = "local_loopback_proxy"
        elif host in _HOSTS and tunnel_host is None:
            route = "direct"
        else:
            route = "unknown"
        self.diagnostics.observe_route(route, getattr(req, "_news_effective_bypass", None))
        return super().https_open(req)


@dataclass(frozen=True)
class FeedResponse:
    status: int
    body: bytes = b""
    headers: dict | None = None
    diagnostics: dict | None = None


def fetch_feed(url, headers, timeout, max_bytes):
    """Fixed official origins, bounded same-host redirects and validated local proxy."""
    if url not in {source["url"] for source in SOURCES}:
        raise ValueError("source_not_allowed")
    diagnostics = _Diagnostics(headers)
    try:
        try:
            proxy_view = urllib.request.getproxies()
        except Exception:
            diagnostics.proxy_configuration_intent = "configuration_unavailable"
            raise FeedValidationError("proxy_configuration_error") from None
        diagnostics.bypass_configured = "no" in proxy_view if isinstance(proxy_view, Mapping) else None
        try:
            proxies, configuration = feed_proxy_configuration(proxy_view)
        except FeedValidationError:
            diagnostics.proxy_configuration_intent = "invalid_configuration"
            raise
        diagnostics.proxy_configuration_intent = configuration["proxy_configuration_intent"]
        opener = urllib.request.build_opener(_ObservedProxyHandler(proxies),
            _SameHostRedirect(url, diagnostics), _ObservedHTTPSHandler(diagnostics))
        request = urllib.request.Request(url, headers={
            "User-Agent": "AI-Collaboration-Studio-Local-News/1.0 (+https://github.com/TheDeadly-cat/hajimi-jiaoyifenxi/issues)",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
            "Accept-Encoding": "identity", **headers,
        })
        diagnostics.phase = "request"
        with opener.open(request, timeout=timeout) as response:
            diagnostics.phase = "response_headers"
            diagnostics.observe_response(response.geturl(), url, response.headers, response.status)
            if not _safe_url(response.geturl(), url):
                raise FeedValidationError("redirect_blocked")
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise FeedValidationError("feed_too_large")
            diagnostics.phase = "response_body"
            chunks, size, deadline = [], 0, time.monotonic() + timeout
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("feed_read_deadline")
                chunk = response.read1(min(65536, max_bytes + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise FeedValidationError("feed_too_large")
                chunks.append(chunk)
            diagnostics.phase = "complete"
            return FeedResponse(response.status, b"".join(chunks), dict(response.headers.items()), diagnostics.snapshot())
    except urllib.error.HTTPError as exc:
        try:
            diagnostics.phase = "response_headers"
            diagnostics.observe_response(exc.geturl(), url, exc.headers, exc.code)
            if exc.code == 304:
                diagnostics.phase = "complete"
                return FeedResponse(304, headers=dict(exc.headers.items()), diagnostics=diagnostics.snapshot())
            diagnostics.error_kind = classify_feed_error(exc)
            exc.news_diagnostics = diagnostics.snapshot()
            raise
        finally:
            exc.close()
    except Exception as exc:
        # Preserve the exception type for existing callers; never copy its text.
        try:
            diagnostics.error_kind = classify_feed_error(exc)
            exc.news_diagnostics = diagnostics.snapshot()
        except Exception:
            pass
        raise
