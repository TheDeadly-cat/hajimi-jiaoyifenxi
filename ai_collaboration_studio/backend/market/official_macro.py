"""Bounded read-only clients for fixed U.S. official macro sources.

The client performs only GET requests to an exact endpoint allowlist.  It has
no cache, credentials, provider/model integration, persistence, or execution
capability.  Construction is side-effect free and a byte fetcher can be
injected for deterministic offline parser tests.
"""

from __future__ import annotations

import copy
import hashlib
import html
import json
import re
import socket
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from types import MappingProxyType
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import Request
from zoneinfo import ZoneInfo

from ..source_poll_control import (
    SourcePollCancelled,
    SourcePollDeadlineExceeded,
    ensure_source_poll_active,
)
from .official_http import open_official_https, read_official_https_body


FEDERAL_RESERVE_MONETARY_RSS_URL = (
    "https://www.federalreserve.gov/feeds/press_monetary.xml"
)
FEDERAL_RESERVE_FOMC_CALENDAR_URL = (
    "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
)
BLS_SERIES_IDS = (
    "CUSR0000SA0",
    "LNS14000000",
    "CES0000000001",
)
BLS_SERIES_URLS = MappingProxyType({
    series_id: f"https://api.bls.gov/publicAPI/v2/timeseries/data/{series_id}"
    for series_id in BLS_SERIES_IDS
})
BLS_RELEASE_CALENDAR_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
TREASURY_DEBT_TO_PENNY_URL = (
    "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v2/"
    "accounting/od/debt_to_penny?fields=record_date,debt_held_public_amt,"
    "intragov_hold_amt,tot_pub_debt_out_amt&sort=-record_date&"
    "page%5Bsize%5D=10"
)
TREASURY_DEBT_TO_PENNY_PAGE_URL = (
    "https://fiscaldata.treasury.gov/datasets/debt-to-the-penny/"
)
TREASURY_RELEASE_CALENDAR_URL = (
    "https://api.fiscaldata.treasury.gov/services/calendar/release"
)
TREASURY_CALENDAR_DATASET_IDS = ("015-BFS-2014Q3-065",)

OFFICIAL_MACRO_USER_AGENT = (
    "AI-Collaboration-Studio/0.1 local-read-only-research"
)
OFFICIAL_MACRO_TIMEOUT_SECONDS = 12
OFFICIAL_MACRO_RESPONSE_BODY_DEADLINE_SECONDS = 12
OFFICIAL_MACRO_MAX_LIMIT = 50
OFFICIAL_MACRO_MAX_SOURCE_ERRORS = 8
OFFICIAL_MACRO_PARSER_VERSION = "official_macro_parser_v1"
OFFICIAL_MACRO_TRANSPORT_IDENTITY = "official_macro_default_https_v1"
OFFICIAL_MACRO_INJECTED_TRANSPORT_IDENTITY = "official_macro_injected_bytes_v1"
BLS_PERIODS_PER_SERIES = 4
TREASURY_DEBT_PAGE_SIZE = 10
CALENDAR_LOOKBACK_DAYS = 7
CALENDAR_LOOKAHEAD_DAYS = 31

OFFICIAL_MACRO_ENDPOINTS = (
    FEDERAL_RESERVE_MONETARY_RSS_URL,
    FEDERAL_RESERVE_FOMC_CALENDAR_URL,
    *(BLS_SERIES_URLS[series_id] for series_id in BLS_SERIES_IDS),
    BLS_RELEASE_CALENDAR_URL,
    TREASURY_DEBT_TO_PENNY_URL,
    TREASURY_RELEASE_CALENDAR_URL,
)

_RESPONSE_POLICIES = MappingProxyType({
    FEDERAL_RESERVE_MONETARY_RSS_URL: (
        512_000,
        ("application/rss+xml", "application/xml", "text/xml"),
    ),
    FEDERAL_RESERVE_FOMC_CALENDAR_URL: (
        1_000_000,
        ("text/html", "application/xhtml+xml"),
    ),
    **{
        BLS_SERIES_URLS[series_id]: (
            512_000,
            ("application/json", "application/vnd.api+json"),
        )
        for series_id in BLS_SERIES_IDS
    },
    BLS_RELEASE_CALENDAR_URL: (1_000_000, ("text/calendar",)),
    TREASURY_DEBT_TO_PENNY_URL: (
        512_000,
        ("application/json", "application/vnd.api+json"),
    ),
    TREASURY_RELEASE_CALENDAR_URL: (
        2_000_000,
        ("application/json", "application/vnd.api+json"),
    ),
})

OFFICIAL_MACRO_MAX_RESPONSE_BYTES = MappingProxyType({
    endpoint: policy[0] for endpoint, policy in _RESPONSE_POLICIES.items()
})
OFFICIAL_MACRO_ALLOWED_CONTENT_TYPES = MappingProxyType({
    endpoint: policy[1] for endpoint, policy in _RESPONSE_POLICIES.items()
})
OFFICIAL_MACRO_ALLOWED_HOSTS = frozenset(
    str(urlparse(endpoint).hostname) for endpoint in OFFICIAL_MACRO_ENDPOINTS
)

OFFICIAL_MACRO_SOURCE_MANIFEST = MappingProxyType({
    "version": "official_macro_sources_v1",
    "parser_version": OFFICIAL_MACRO_PARSER_VERSION,
    "transport_identity": OFFICIAL_MACRO_TRANSPORT_IDENTITY,
    "method": "GET",
    "user_agent": OFFICIAL_MACRO_USER_AGENT,
    "timeout_seconds": OFFICIAL_MACRO_TIMEOUT_SECONDS,
    "response_body_deadline_seconds": (
        OFFICIAL_MACRO_RESPONSE_BODY_DEADLINE_SECONDS
    ),
    "endpoints": OFFICIAL_MACRO_ENDPOINTS,
    "bls_series_ids": BLS_SERIES_IDS,
    "bls_periods_per_series": BLS_PERIODS_PER_SERIES,
    "treasury_calendar_dataset_ids": TREASURY_CALENDAR_DATASET_IDS,
    "treasury_debt_page_size": TREASURY_DEBT_PAGE_SIZE,
    "calendar_lookback_days": CALENDAR_LOOKBACK_DAYS,
    "calendar_lookahead_days": CALENDAR_LOOKAHEAD_DAYS,
    "max_limit": OFFICIAL_MACRO_MAX_LIMIT,
    "max_source_errors": OFFICIAL_MACRO_MAX_SOURCE_ERRORS,
    "response_policies": tuple(
        (
            endpoint,
            OFFICIAL_MACRO_MAX_RESPONSE_BYTES[endpoint],
            OFFICIAL_MACRO_ALLOWED_CONTENT_TYPES[endpoint],
        )
        for endpoint in OFFICIAL_MACRO_ENDPOINTS
    ),
})

_BLS_SERIES = MappingProxyType({
    "CUSR0000SA0": MappingProxyType({
        "family": "consumer_price_index",
        "label": "Consumer Price Index for All Urban Consumers",
    }),
    "LNS14000000": MappingProxyType({
        "family": "employment_situation",
        "label": "Civilian Unemployment Rate",
    }),
    "CES0000000001": MappingProxyType({
        "family": "employment_situation",
        "label": "Total Nonfarm Payroll Employment",
    }),
})

_BLS_CALENDAR_TZIDS = MappingProxyType({
    "America/New_York": "America/New_York",
    "US-Eastern": "America/New_York",
    "Eastern Standard Time": "America/New_York",
})

_MONTHS = MappingProxyType({
    name.casefold(): index
    for index, name in enumerate(
        (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        start=1,
    )
})
_DECIMAL_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_YEAR_RE = re.compile(r"(?:19|20)[0-9]{2}\Z")
_BLS_PERIOD_RE = re.compile(r"M(?:0[1-9]|1[0-2])\Z")
_DATE_RE = re.compile(r"(?:19|20)[0-9]{2}-[0-9]{2}-[0-9]{2}\Z")
_TIME_RE = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")
_REVISION_RE = re.compile(r"\b(?:correct(?:ed|ion)|revis(?:e|ed|ion)|updated?)\b", re.I)
_US_EASTERN = ZoneInfo("America/New_York")


def _validate_limit(limit: Any) -> int:
    if type(limit) is not int or not 1 <= limit <= OFFICIAL_MACRO_MAX_LIMIT:
        raise ValueError(
            f"limit must be a native integer from 1 to {OFFICIAL_MACRO_MAX_LIMIT}"
        )
    return limit


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _bounded_text(value: Any, maximum: int, *, allow_empty: bool = True) -> str:
    if type(value) is not str:
        return ""
    clean = " ".join(value.split())
    if (not clean and not allow_empty) or any(
        ord(character) < 32 or ord(character) == 127 for character in clean
    ):
        return ""
    return clean[:maximum]


def _plain_text(value: Any, maximum: int) -> str:
    if type(value) is not str:
        return ""
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return _bounded_text(html.unescape(without_tags), maximum)


def _safe_error_message(exc: Exception) -> str:
    message = _bounded_text(str(exc), 600)
    return message or type(exc).__name__


def _source_error(code: str, message: str, scope: str) -> dict[str, str]:
    return {
        "code": code[:80],
        "message": _bounded_text(message, 1_000) or "official source failure",
        "scope": _bounded_text(scope, 160) or "official_macro",
    }


def _capacity_error(
    *,
    code: str,
    scope: str,
    row_count: int,
    limit: int,
) -> dict[str, str]:
    return _source_error(
        code,
        (
            f"complete normalized window contains {row_count} rows, "
            f"exceeding the requested capacity {limit}"
        ),
        scope,
    )


def _safe_authority_url(
    value: Any,
    *,
    host: str,
    base_url: str | None = None,
) -> str:
    if type(value) is not str or not value or value != value.strip():
        return ""
    candidate = urljoin(base_url, value) if base_url else value
    try:
        parsed = urlparse(candidate)
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or parsed.hostname != host
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not parsed.path.startswith("/")
        or parsed.fragment
    ):
        return ""
    netloc = host if port is None else f"{host}:443"
    return urlunparse(("https", netloc, parsed.path, parsed.params, parsed.query, ""))


def _finite_decimal(value: Any, *, nonnegative: bool = False) -> str:
    if type(value) is not str or len(value) > 200 or not _DECIMAL_RE.fullmatch(value):
        return ""
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return ""
    if not parsed.is_finite() or (nonnegative and parsed < 0):
        return ""
    return value


def _json_bytes(raw: bytes, *, endpoint: str) -> Any:
    _validate_injected_bytes(raw, endpoint)
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("official JSON payload could not be parsed") from exc


def _validate_injected_bytes(raw: Any, endpoint: str) -> bytes:
    if type(raw) is not bytes:
        raise TypeError("fetch_bytes must return exact bytes")
    maximum = OFFICIAL_MACRO_MAX_RESPONSE_BYTES[endpoint]
    if len(raw) > maximum:
        raise ValueError("official source response exceeds its sealed byte limit")
    return raw


def _official_response_socket(response: Any) -> Any:
    """Return the HTTPS socket needed to enforce a wall-clock body deadline."""

    fp = getattr(response, "fp", None)
    raw = getattr(fp, "raw", None)
    sock = getattr(raw, "_sock", None)
    if (
        sock is None
        or not callable(getattr(sock, "shutdown", None))
        or not callable(getattr(sock, "_real_close", None))
    ):
        raise ValueError(
            "official source response lacks sealed deadline control"
        )
    return sock


def _expire_official_response_body(sock: Any, expired: threading.Event) -> None:
    """Interrupt a blocking urllib body read when its absolute deadline expires."""

    expired.set()
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock._real_close()
    except OSError:
        pass


def _read_official_response_body(
    response: Any,
    maximum: int,
    deadline_seconds: int | float = OFFICIAL_MACRO_RESPONSE_BODY_DEADLINE_SECONDS,
    *,
    deadline_monotonic_ms: int = 0,
    cancel_event: threading.Event | None = None,
) -> bytes:
    """Read one body within both a byte cap and an absolute elapsed deadline.

    ``urllib``'s request timeout is a socket-inactivity limit.  A short-lived
    watchdog shuts down the current HTTPS socket at the monotonic deadline, so
    neither a content body nor chunked-transfer framing can extend the read by
    continuously trickling bytes.  The watchdog exists only during this
    explicit fetch; constructing a client starts no thread.
    """

    return read_official_https_body(
        response,
        maximum,
        deadline_seconds=deadline_seconds,
        deadline_monotonic_ms=deadline_monotonic_ms,
        cancel_event=cancel_event,
    )


def _fetch_official_macro_bytes(
    url: str,
    *,
    deadline_monotonic_ms: int = 0,
    cancel_event: threading.Event | None = None,
) -> bytes:
    """Fetch one exact endpoint through the shared redirect-safe transport."""

    if type(url) is not str or url not in _RESPONSE_POLICIES:
        raise ValueError("official macro client rejected a non-allowlisted endpoint")
    maximum, allowed_content_types = _RESPONSE_POLICIES[url]
    parsed = urlparse(url)
    host = parsed.hostname
    if type(host) is not str:
        raise ValueError("official macro endpoint has no canonical host")
    accepted = ", ".join(allowed_content_types)
    request = Request(
        url,
        headers={
            "User-Agent": OFFICIAL_MACRO_USER_AGENT,
            "Accept": accepted,
        },
        method="GET",
    )
    if request.get_method() != "GET" or request.data is not None:
        raise ValueError("official macro transport is GET-only")
    with open_official_https(
        request,
        allowed_hosts={host},
        timeout=OFFICIAL_MACRO_TIMEOUT_SECONDS,
        url_validator=lambda candidate: type(candidate) is str and candidate == url,
        deadline_monotonic_ms=deadline_monotonic_ms,
        cancel_event=cancel_event,
    ) as response:
        content_type = response.headers.get("Content-Type")
        if type(content_type) is not str:
            raise ValueError("official source omitted Content-Type")
        media_type = content_type.partition(";")[0].strip().lower()
        if media_type not in allowed_content_types:
            raise ValueError("official source returned an unexpected Content-Type")
        declared_length = response.headers.get("Content-Length")
        if declared_length is not None:
            if type(declared_length) is not str or not declared_length.strip().isdigit():
                raise ValueError("official source returned an invalid Content-Length")
            if int(declared_length.strip()) > maximum:
                raise ValueError("official source response exceeds its sealed byte limit")
        raw = _read_official_response_body(
            response,
            maximum,
            deadline_monotonic_ms=deadline_monotonic_ms,
            cancel_event=cancel_event,
        )
    if type(raw) is not bytes or len(raw) > maximum:
        raise ValueError("official source response exceeds its sealed byte limit")
    return raw


DEFAULT_OFFICIAL_MACRO_FETCH_BYTES = _fetch_official_macro_bytes


class _FomcCalendarParser(HTMLParser):
    """Extract the year/month/date/link cells from the Fed's fixed calendar."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meetings: list[dict[str, Any]] = []
        self._depth = 0
        self._year: int | None = None
        self._meeting_depth: int | None = None
        self._capture_depth: int | None = None
        self._capture_field = ""
        self._current: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._depth += 1
        attributes = {key: value for key, value in attrs if type(key) is str}
        classes = set(str(attributes.get("class") or "").split())
        if tag == "div" and "fomc-meeting" in classes and self._current is None:
            self._meeting_depth = self._depth
            self._current = {
                "year": self._year,
                "month": [],
                "date": [],
                "links": [],
            }
        if self._current is not None and tag == "div":
            if "fomc-meeting__month" in classes:
                self._capture_field = "month"
                self._capture_depth = self._depth
            elif "fomc-meeting__date" in classes:
                self._capture_field = "date"
                self._capture_depth = self._depth
        if self._current is not None and tag == "a":
            href = attributes.get("href")
            if type(href) is str:
                self._current["links"].append(href)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._capture_depth == self._depth:
            self._capture_depth = None
            self._capture_field = ""
        if tag == "div" and self._meeting_depth == self._depth:
            if self._current is not None:
                self.meetings.append(self._current)
            self._current = None
            self._meeting_depth = None
            self._capture_depth = None
            self._capture_field = ""
        self._depth = max(0, self._depth - 1)

    def handle_data(self, data: str) -> None:
        match = re.search(r"\b((?:19|20)[0-9]{2})\s+FOMC\s+Meetings\b", data, re.I)
        if match:
            self._year = int(match.group(1))
        if self._current is not None and self._capture_field:
            self._current[self._capture_field].append(data)


class OfficialMacroSourceClient:
    """Parse fixed official macro release and calendar endpoints."""

    _default_fetch_bytes = staticmethod(_fetch_official_macro_bytes)

    def __init__(
        self,
        *,
        fetch_bytes: Callable[[str], bytes] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if fetch_bytes is not None and not callable(fetch_bytes):
            raise TypeError("fetch_bytes must be callable")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._fetch_bytes = (
            self._default_fetch_bytes if fetch_bytes is None else fetch_bytes
        )
        self._clock = (
            (lambda: datetime.now(timezone.utc)) if clock is None else clock
        )
        self._transport_identity = (
            OFFICIAL_MACRO_TRANSPORT_IDENTITY
            if fetch_bytes is None
            else OFFICIAL_MACRO_INJECTED_TRANSPORT_IDENTITY
        )

    @property
    def transport_identity(self) -> str:
        return self._transport_identity

    @property
    def source_manifest(self) -> dict[str, Any]:
        return self.config_basis()

    def config_basis(self) -> dict[str, Any]:
        """Return a detached JSON-compatible copy of the sealed source basis."""

        def thaw(value: Any) -> Any:
            if isinstance(value, Mapping):
                return {str(key): thaw(item) for key, item in value.items()}
            if type(value) in {tuple, list}:
                return [thaw(item) for item in value]
            if type(value) in {set, frozenset}:
                return sorted(thaw(item) for item in value)
            return copy.deepcopy(value)

        basis = thaw(OFFICIAL_MACRO_SOURCE_MANIFEST)
        if type(basis) is not dict:
            raise AssertionError("official macro manifest is not a mapping")
        basis["transport_identity"] = self._transport_identity
        return basis

    def _now(self) -> datetime:
        value = self._clock()
        if type(value) is not datetime or value.tzinfo is None:
            raise ValueError("clock must return a timezone-aware native datetime")
        return value.astimezone(timezone.utc)

    def _fetch_with_control(
        self,
        url: str,
        *,
        deadline_monotonic_ms: int,
        cancel_event: threading.Event | None,
    ) -> bytes:
        ensure_source_poll_active(
            deadline_monotonic_ms=deadline_monotonic_ms,
            cancel_event=cancel_event,
        )
        if self._transport_identity == OFFICIAL_MACRO_TRANSPORT_IDENTITY:
            raw = self._fetch_bytes(
                url,
                deadline_monotonic_ms=deadline_monotonic_ms,
                cancel_event=cancel_event,
            )
        else:
            raw = self._fetch_bytes(url)
        ensure_source_poll_active(
            deadline_monotonic_ms=deadline_monotonic_ms,
            cancel_event=cancel_event,
        )
        return raw

    def federal_reserve_releases(
        self,
        limit: int = OFFICIAL_MACRO_MAX_LIMIT,
        *,
        deadline_monotonic_ms: int = 0,
        cancel_event: threading.Event | None = None,
    ) -> dict:
        safe_limit = _validate_limit(limit)
        try:
            now = self._now()
            raw = self._fetch_with_control(
                FEDERAL_RESERVE_MONETARY_RSS_URL,
                deadline_monotonic_ms=deadline_monotonic_ms,
                cancel_event=cancel_event,
            )
            rows = self._parse_federal_reserve_rss(raw, now=now)
            if not rows:
                raise ValueError("official monetary policy RSS contained no valid releases")
            rows.sort(key=lambda row: (row["released_at"], row["official_id"]), reverse=True)
            if len(rows) > safe_limit:
                return {
                    "rows": [],
                    "source_errors": [_capacity_error(
                        code="FEDERAL_RESERVE_CAPACITY_EXCEEDED",
                        scope="federal_reserve",
                        row_count=len(rows),
                        limit=safe_limit,
                    )],
                }
            return {"rows": rows, "source_errors": []}
        except (SourcePollCancelled, SourcePollDeadlineExceeded):
            raise
        except Exception as exc:
            return {
                "rows": [],
                "source_errors": [_source_error(
                    "FEDERAL_RESERVE_RELEASES_ERROR",
                    _safe_error_message(exc),
                    "federal_reserve",
                )],
            }

    def bls_releases(
        self,
        limit: int = OFFICIAL_MACRO_MAX_LIMIT,
        *,
        deadline_monotonic_ms: int = 0,
        cancel_event: threading.Event | None = None,
    ) -> dict:
        safe_limit = _validate_limit(limit)
        try:
            now = self._now()
        except Exception as exc:
            return {
                "rows": [],
                "source_errors": [_source_error(
                    "BLS_RELEASES_ERROR",
                    _safe_error_message(exc),
                    "bls",
                )],
            }
        rows: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for series_id in BLS_SERIES_IDS:
            ensure_source_poll_active(
                deadline_monotonic_ms=deadline_monotonic_ms,
                cancel_event=cancel_event,
            )
            endpoint = BLS_SERIES_URLS[series_id]
            try:
                raw = self._fetch_with_control(
                    endpoint,
                    deadline_monotonic_ms=deadline_monotonic_ms,
                    cancel_event=cancel_event,
                )
                series_rows = self._parse_bls_series(raw, series_id=series_id, now=now)
                if not series_rows:
                    raise ValueError("official BLS series contained no valid observations")
                rows.extend(series_rows)
            except (SourcePollCancelled, SourcePollDeadlineExceeded):
                raise
            except Exception as exc:
                if len(errors) < OFFICIAL_MACRO_MAX_SOURCE_ERRORS:
                    errors.append(_source_error(
                        "BLS_SERIES_ERROR",
                        _safe_error_message(exc),
                        f"bls:{series_id}",
                    ))
        order = {series_id: index for index, series_id in enumerate(BLS_SERIES_IDS)}
        rows.sort(
            key=lambda row: (
                row["reference_period"],
                -order[row["data"]["series_id"]],
            ),
            reverse=True,
        )
        if len(rows) > safe_limit:
            if len(errors) < OFFICIAL_MACRO_MAX_SOURCE_ERRORS:
                errors.append(_capacity_error(
                    code="BLS_RELEASES_CAPACITY_EXCEEDED",
                    scope="bls",
                    row_count=len(rows),
                    limit=safe_limit,
                ))
            return {"rows": [], "source_errors": errors}
        return {"rows": rows, "source_errors": errors}

    def treasury_releases(
        self,
        limit: int = OFFICIAL_MACRO_MAX_LIMIT,
        *,
        deadline_monotonic_ms: int = 0,
        cancel_event: threading.Event | None = None,
    ) -> dict:
        safe_limit = _validate_limit(limit)
        try:
            now = self._now()
            raw = self._fetch_with_control(
                TREASURY_DEBT_TO_PENNY_URL,
                deadline_monotonic_ms=deadline_monotonic_ms,
                cancel_event=cancel_event,
            )
            rows = self._parse_treasury_debt(raw, now=now)
            if not rows:
                raise ValueError("official Treasury dataset contained no valid records")
            rows.sort(key=lambda row: row["reference_period"], reverse=True)
            if len(rows) > safe_limit:
                return {
                    "rows": [],
                    "source_errors": [_capacity_error(
                        code="TREASURY_RELEASES_CAPACITY_EXCEEDED",
                        scope="treasury",
                        row_count=len(rows),
                        limit=safe_limit,
                    )],
                }
            return {"rows": rows, "source_errors": []}
        except (SourcePollCancelled, SourcePollDeadlineExceeded):
            raise
        except Exception as exc:
            return {
                "rows": [],
                "source_errors": [_source_error(
                    "TREASURY_RELEASES_ERROR",
                    _safe_error_message(exc),
                    "treasury",
                )],
            }

    def calendar_events(
        self,
        limit: int = OFFICIAL_MACRO_MAX_LIMIT,
        *,
        deadline_monotonic_ms: int = 0,
        cancel_event: threading.Event | None = None,
    ) -> dict:
        safe_limit = _validate_limit(limit)
        try:
            now = self._now()
        except Exception as exc:
            return {
                "rows": [],
                "source_errors": [_source_error(
                    "OFFICIAL_MACRO_CALENDAR_ERROR",
                    _safe_error_message(exc),
                    "official_macro_calendar",
                )],
            }
        sources = (
            (
                "federal_reserve",
                FEDERAL_RESERVE_FOMC_CALENDAR_URL,
                lambda raw: self._parse_fomc_calendar(raw, now=now),
            ),
            (
                "bls",
                BLS_RELEASE_CALENDAR_URL,
                lambda raw: self._parse_bls_calendar(raw, now=now),
            ),
            (
                "treasury",
                TREASURY_RELEASE_CALENDAR_URL,
                lambda raw: self._parse_treasury_calendar(raw, now=now),
            ),
        )
        payloads: dict[str, bytes] = {}
        errors: list[dict[str, str]] = []
        for scope, endpoint, _parser in sources:
            ensure_source_poll_active(
                deadline_monotonic_ms=deadline_monotonic_ms,
                cancel_event=cancel_event,
            )
            try:
                payloads[scope] = self._fetch_with_control(
                    endpoint,
                    deadline_monotonic_ms=deadline_monotonic_ms,
                    cancel_event=cancel_event,
                )
            except (SourcePollCancelled, SourcePollDeadlineExceeded):
                raise
            except Exception as exc:
                if len(errors) < OFFICIAL_MACRO_MAX_SOURCE_ERRORS:
                    errors.append(_source_error(
                        "OFFICIAL_MACRO_CALENDAR_FETCH_ERROR",
                        _safe_error_message(exc),
                        f"official_macro_calendar:{scope}",
                    ))
        if errors:
            return {"rows": [], "source_errors": errors}

        parsed_rows: list[dict[str, Any]] = []
        for scope, _endpoint, parser in sources:
            ensure_source_poll_active(
                deadline_monotonic_ms=deadline_monotonic_ms,
                cancel_event=cancel_event,
            )
            try:
                source_rows = parser(payloads[scope])
                parsed_rows.extend(source_rows)
            except Exception as exc:
                if len(errors) < OFFICIAL_MACRO_MAX_SOURCE_ERRORS:
                    errors.append(_source_error(
                        "OFFICIAL_MACRO_CALENDAR_PARSE_ERROR",
                        _safe_error_message(exc),
                        f"official_macro_calendar:{scope}",
                    ))
        if errors:
            return {"rows": [], "source_errors": errors}
        parsed_rows.sort(
            key=lambda row: (
                row["scheduled_at"],
                row["authority"],
                row["official_id"],
            )
        )
        if len(parsed_rows) > safe_limit:
            return {
                "rows": [],
                "source_errors": [_capacity_error(
                    code="OFFICIAL_MACRO_CALENDAR_CAPACITY_EXCEEDED",
                    scope="official_macro_calendar",
                    row_count=len(parsed_rows),
                    limit=safe_limit,
                )],
            }
        return {"rows": parsed_rows, "source_errors": []}

    @staticmethod
    def _parse_federal_reserve_rss(raw: bytes, *, now: datetime) -> list[dict[str, Any]]:
        _validate_injected_bytes(raw, FEDERAL_RESERVE_MONETARY_RSS_URL)
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise ValueError("official Federal Reserve RSS could not be parsed") from exc
        items = root.findall(".//item")
        if len(items) > 500:
            raise ValueError("official Federal Reserve RSS exceeded the item bound")
        rows: list[dict[str, Any]] = []
        for item in items:
            title = _bounded_text(OfficialMacroSourceClient._xml_text(item, "title"), 500)
            link = _safe_authority_url(
                OfficialMacroSourceClient._xml_text(item, "link"),
                host="www.federalreserve.gov",
            )
            guid = _bounded_text(OfficialMacroSourceClient._xml_text(item, "guid"), 1_000)
            if not title or not link:
                continue
            identity_basis = link
            if guid:
                if guid.lower().startswith(("http://", "https://")):
                    safe_guid = _safe_authority_url(guid, host="www.federalreserve.gov")
                    if not safe_guid or safe_guid != link:
                        continue
                    identity_basis = safe_guid
                else:
                    identity_basis = guid
            published_raw = OfficialMacroSourceClient._xml_text(item, "pubDate")
            try:
                published = parsedate_to_datetime(published_raw)
            except (TypeError, ValueError, OverflowError):
                continue
            if published.tzinfo is None:
                continue
            published = published.astimezone(timezone.utc)
            if published > now:
                continue
            summary = _plain_text(
                OfficialMacroSourceClient._xml_text(item, "description"),
                8_000,
            )
            official_id = "fed-monetary-guid-v1:" + hashlib.sha256(
                identity_basis.encode("utf-8")
            ).hexdigest()
            rows.append({
                "official_id": official_id,
                "authority": "federal_reserve",
                "family": "monetary_policy",
                "reference_period": "",
                "title": title,
                "summary": summary,
                "official_url": link,
                "source_url": FEDERAL_RESERVE_MONETARY_RSS_URL,
                "scheduled_at": "",
                "released_at": _utc_iso(published),
                "official_revision": bool(_REVISION_RE.search(f"{title} {summary}")),
                "data": {},
            })
        return rows

    @staticmethod
    def _xml_text(parent: ET.Element, tag: str) -> str:
        node = parent.find(tag)
        if node is None:
            return ""
        return "".join(node.itertext()).strip()

    @staticmethod
    def _parse_bls_series(
        raw: bytes,
        *,
        series_id: str,
        now: datetime,
    ) -> list[dict[str, Any]]:
        endpoint = BLS_SERIES_URLS[series_id]
        payload = _json_bytes(raw, endpoint=endpoint)
        if type(payload) is not dict or payload.get("status") != "REQUEST_SUCCEEDED":
            raise ValueError("official BLS response status was not successful")
        results = payload.get("Results")
        if type(results) is list:
            if len(results) != 1 or type(results[0]) is not dict:
                raise ValueError("official BLS Results shape was invalid")
            results = results[0]
        if type(results) is not dict or type(results.get("series")) is not list:
            raise ValueError("official BLS Results did not contain a series list")
        series_entries = results["series"]
        if not 1 <= len(series_entries) <= 16:
            raise ValueError("official BLS response contained an invalid series count")
        candidates: list[tuple[tuple[int, int], int, dict[str, Any]]] = []
        ordinal = 0
        for series in series_entries:
            if type(series) is not dict or series.get("seriesID") != series_id:
                raise ValueError("official BLS response series did not match the endpoint")
            observations = series.get("data")
            if type(observations) is not list or len(observations) > 500:
                raise ValueError("official BLS observations violated the item bound")
            for observation in observations:
                ordinal += 1
                if type(observation) is not dict:
                    continue
                year = observation.get("year")
                period = observation.get("period")
                period_name = observation.get("periodName")
                value = _finite_decimal(observation.get("value"))
                if (
                    type(year) is not str
                    or not _YEAR_RE.fullmatch(year)
                    or type(period) is not str
                    or not _BLS_PERIOD_RE.fullmatch(period)
                    or type(period_name) is not str
                    or not _bounded_text(period_name, 80, allow_empty=False)
                    or not value
                ):
                    continue
                year_number = int(year)
                month_number = int(period[1:])
                if (year_number, month_number) > (now.year, now.month):
                    continue
                footnotes = OfficialMacroSourceClient._bls_footnotes(
                    observation.get("footnotes")
                )
                metadata = _BLS_SERIES[series_id]
                reference_period = f"{year}-{period}"
                candidates.append((
                    (year_number, month_number),
                    ordinal,
                    {
                        "official_id": f"bls-series-v1:{series_id}:{reference_period}",
                        "authority": "bls",
                        "family": str(metadata["family"]),
                        "reference_period": reference_period,
                        "title": (
                            f"{metadata['label']} - "
                            f"{_bounded_text(period_name, 80)} {year}"
                        )[:500],
                        "summary": (
                            f"Official BLS observation for series {series_id}, "
                            f"{_bounded_text(period_name, 80)} {year}."
                        ),
                        "official_url": endpoint,
                        "source_url": endpoint,
                        "scheduled_at": "",
                        "released_at": "",
                        "official_revision": False,
                        "data": {
                            "series_id": series_id,
                            "year": year,
                            "period": period,
                            "period_name": _bounded_text(period_name, 80),
                            "value": value,
                            "footnotes": footnotes,
                        },
                    },
                ))
        if not candidates:
            return []
        latest_periods = sorted({item[0] for item in candidates}, reverse=True)[
            :BLS_PERIODS_PER_SERIES
        ]
        allowed_periods = set(latest_periods)
        selected = [item for item in candidates if item[0] in allowed_periods]
        selected.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        return [item[2] for item in selected]

    @staticmethod
    def _bls_footnotes(value: Any) -> str:
        if value is None:
            return ""
        if type(value) is not list or len(value) > 20:
            raise ValueError("official BLS footnotes violated the item bound")
        notes: list[str] = []
        for raw in value:
            if type(raw) is not dict:
                raise ValueError("official BLS footnote was not an object")
            code = _bounded_text(raw.get("code"), 40)
            text = _bounded_text(raw.get("text"), 500)
            if code and text:
                notes.append(f"{code}: {text}")
            elif code or text:
                notes.append(code or text)
        return " | ".join(notes)[:1_000]

    @staticmethod
    def _parse_treasury_debt(raw: bytes, *, now: datetime) -> list[dict[str, Any]]:
        payload = _json_bytes(raw, endpoint=TREASURY_DEBT_TO_PENNY_URL)
        if type(payload) is not dict or type(payload.get("data")) is not list:
            raise ValueError("official Treasury response did not contain a data list")
        records = payload["data"]
        if len(records) > TREASURY_DEBT_PAGE_SIZE:
            raise ValueError("official Treasury response exceeded the sealed page size")
        rows: list[dict[str, Any]] = []
        for record in records:
            if type(record) is not dict:
                raise ValueError("official Treasury record was not an object")
            record_date = record.get("record_date")
            if type(record_date) is not str or not _DATE_RE.fullmatch(record_date):
                raise ValueError("official Treasury record_date was invalid")
            try:
                parsed_date = datetime.strptime(record_date, "%Y-%m-%d").date()
            except ValueError as exc:
                raise ValueError("official Treasury record_date was invalid") from exc
            if parsed_date > now.date():
                raise ValueError("official Treasury record_date was in the future")
            data = {"record_date": record_date}
            for field in (
                "debt_held_public_amt",
                "intragov_hold_amt",
                "tot_pub_debt_out_amt",
            ):
                amount = _finite_decimal(record.get(field), nonnegative=True)
                if not amount:
                    raise ValueError(f"official Treasury {field} was not a finite decimal")
                data[field] = amount
            rows.append({
                "official_id": f"treasury-debt-to-penny-v1:{record_date}",
                "authority": "treasury",
                "family": "debt_to_penny",
                "reference_period": record_date,
                "title": f"Debt to the Penny - {record_date}",
                "summary": "Official Treasury Fiscal Data debt observation.",
                "official_url": TREASURY_DEBT_TO_PENNY_PAGE_URL,
                "source_url": TREASURY_DEBT_TO_PENNY_URL,
                "scheduled_at": "",
                "released_at": "",
                "official_revision": False,
                "data": data,
            })
        return rows

    @staticmethod
    def _in_calendar_window(value: datetime, now: datetime) -> bool:
        return now - timedelta(days=CALENDAR_LOOKBACK_DAYS) <= value <= now + timedelta(
            days=CALENDAR_LOOKAHEAD_DAYS
        )

    @staticmethod
    def _parse_fomc_calendar(raw: bytes, *, now: datetime) -> list[dict[str, Any]]:
        _validate_injected_bytes(raw, FEDERAL_RESERVE_FOMC_CALENDAR_URL)
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("official FOMC calendar was not UTF-8") from exc
        parser = _FomcCalendarParser()
        parser.feed(text)
        parser.close()
        if not parser.meetings:
            raise ValueError("official FOMC calendar contained no meeting structures")
        if len(parser.meetings) > 200:
            raise ValueError("official FOMC calendar exceeded the meeting bound")
        year_ordinals: dict[int, int] = {}
        rows: list[dict[str, Any]] = []
        recognized_count = 0
        for meeting in parser.meetings:
            year = meeting.get("year")
            month_text = _bounded_text(" ".join(meeting.get("month") or []), 40)
            date_text = _bounded_text(" ".join(meeting.get("date") or []), 40)
            if type(year) is not int or month_text.casefold() not in _MONTHS:
                continue
            days = [
                int(value)
                for value in re.findall(
                    r"(?<![0-9])[0-3]?[0-9](?![0-9])",
                    date_text,
                )
            ]
            if not days or len(days) > 2:
                continue
            month = _MONTHS[month_text.casefold()]
            try:
                start_local = datetime(year, month, days[0], tzinfo=_US_EASTERN)
                end_local = datetime(year, month, days[-1], tzinfo=_US_EASTERN)
            except ValueError:
                continue
            recognized_count += 1
            year_ordinals[year] = year_ordinals.get(year, 0) + 1
            ordinal = year_ordinals[year]
            scheduled = end_local.astimezone(timezone.utc)
            if not OfficialMacroSourceClient._in_calendar_window(scheduled, now):
                continue
            links = [
                _safe_authority_url(
                    value,
                    host="www.federalreserve.gov",
                    base_url=FEDERAL_RESERVE_FOMC_CALENDAR_URL,
                )
                for value in meeting.get("links") or []
            ]
            links = [value for value in links if value]
            official_url = next(
                (
                    value
                    for value in links
                    if "/newsevents/pressreleases/monetary" in value
                    and value.endswith(".htm")
                ),
                links[0] if links else FEDERAL_RESERVE_FOMC_CALENDAR_URL,
            )
            reference_period = f"{year}-fomc-{ordinal:02d}"
            projection_note = (
                " Includes a Summary of Economic Projections marker."
                if "*" in date_text
                else ""
            )
            rows.append({
                "official_id": f"fomc-calendar-v1:{year}:{ordinal:02d}",
                "authority": "federal_reserve",
                "family": "fomc_meeting",
                "reference_period": reference_period,
                "title": f"FOMC meeting - {month_text} {date_text}, {year}"[:500],
                "summary": (
                    "Official Federal Reserve FOMC meeting date; the source page "
                    "does not publish an exact meeting time." + projection_note
                ),
                "official_url": official_url,
                "source_url": FEDERAL_RESERVE_FOMC_CALENDAR_URL,
                "scheduled_at": "",
                "released_at": "",
                "official_revision": False,
                "data": {
                    "scheduled_date_start": start_local.date().isoformat(),
                    "scheduled_date_end": end_local.date().isoformat(),
                    "time_precision": "date",
                },
            })
        if recognized_count == 0:
            raise ValueError("official FOMC calendar contained no valid meeting dates")
        return rows

    @staticmethod
    def _parse_bls_calendar(raw: bytes, *, now: datetime) -> list[dict[str, Any]]:
        _validate_injected_bytes(raw, BLS_RELEASE_CALENDAR_URL)
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("official BLS calendar was not UTF-8") from exc
        physical_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        lines: list[str] = []
        for line in physical_lines:
            if line.startswith((" ", "\t")):
                if not lines:
                    raise ValueError("official BLS calendar began with a folded line")
                lines[-1] += line[1:]
            else:
                lines.append(line)
        events: list[dict[str, tuple[dict[str, str], str]]] = []
        current: dict[str, tuple[dict[str, str], str]] | None = None
        for line in lines:
            if line == "BEGIN:VEVENT":
                if current is not None:
                    raise ValueError("official BLS calendar nested a VEVENT")
                current = {}
                continue
            if line == "END:VEVENT":
                if current is None:
                    raise ValueError("official BLS calendar closed an absent VEVENT")
                events.append(current)
                current = None
                if len(events) > 2_000:
                    raise ValueError("official BLS calendar exceeded the event bound")
                continue
            if current is None or ":" not in line:
                continue
            head, value = line.split(":", 1)
            parts = head.split(";")
            name = parts[0].upper()
            parameters: dict[str, str] = {}
            for parameter in parts[1:]:
                if "=" in parameter:
                    key, parameter_value = parameter.split("=", 1)
                    parameter_key = key.upper()
                    if parameter_key in parameters:
                        raise ValueError(
                            "official BLS calendar repeated a property parameter"
                        )
                    parameters[parameter_key] = parameter_value
            if name in {"UID", "SUMMARY", "DESCRIPTION", "URL", "DTSTART", "SEQUENCE", "STATUS"}:
                current[name] = (parameters, value)
        if current is not None:
            raise ValueError("official BLS calendar ended inside a VEVENT")
        if not events:
            raise ValueError("official BLS calendar contained no VEVENT structures")
        rows: list[dict[str, Any]] = []
        recognized_count = 0
        for event in events:
            summary = OfficialMacroSourceClient._ics_text(event.get("SUMMARY"), 500)
            summary_folded = summary.casefold()
            if "consumer price index" in summary_folded:
                family = "consumer_price_index"
            elif "employment situation" in summary_folded and "veteran" not in summary_folded:
                family = "employment_situation"
            else:
                continue
            uid = OfficialMacroSourceClient._ics_text(event.get("UID"), 1_000)
            if not uid:
                raise ValueError("official BLS calendar event omitted UID")
            scheduled = OfficialMacroSourceClient._ics_datetime(event.get("DTSTART"))
            if scheduled is None:
                raise ValueError("official BLS calendar event omitted DTSTART")
            recognized_count += 1
            if not OfficialMacroSourceClient._in_calendar_window(scheduled, now):
                continue
            description = OfficialMacroSourceClient._ics_text(
                event.get("DESCRIPTION"), 8_000
            )
            reference_period = OfficialMacroSourceClient._calendar_reference_period(
                f"{summary} {description}"
            )
            url = OfficialMacroSourceClient._ics_text(event.get("URL"), 2_000)
            official_url = _safe_authority_url(url, host="www.bls.gov") or BLS_RELEASE_CALENDAR_URL
            sequence_text = OfficialMacroSourceClient._ics_text(event.get("SEQUENCE"), 20)
            status = OfficialMacroSourceClient._ics_text(event.get("STATUS"), 40).upper()
            sequence = int(sequence_text) if sequence_text.isdigit() else 0
            official_id = "bls-calendar-uid-v1:" + hashlib.sha256(
                uid.encode("utf-8")
            ).hexdigest()
            rows.append({
                "official_id": official_id,
                "authority": "bls",
                "family": family,
                "reference_period": reference_period,
                "title": summary,
                "summary": description,
                "official_url": official_url,
                "source_url": BLS_RELEASE_CALENDAR_URL,
                "scheduled_at": _utc_iso(scheduled),
                "released_at": "",
                "official_revision": bool(sequence > 0 or status == "CANCELLED"),
                "data": {},
            })
        if recognized_count == 0:
            raise ValueError("official BLS calendar contained no fixed-family events")
        return rows

    @staticmethod
    def _ics_text(
        property_value: tuple[dict[str, str], str] | None,
        maximum: int,
    ) -> str:
        if property_value is None:
            return ""
        raw = property_value[1]
        if type(raw) is not str:
            return ""
        value = raw.replace("\\n", "\n").replace("\\N", "\n")
        value = value.replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
        return _bounded_text(value, maximum)

    @staticmethod
    def _ics_datetime(
        property_value: tuple[dict[str, str], str] | None,
    ) -> datetime | None:
        if property_value is None:
            return None
        parameters, raw = property_value
        if type(raw) is not str:
            return None
        tzid = parameters.get("TZID")
        canonical_tzid = None
        if tzid is not None:
            canonical_tzid = _BLS_CALENDAR_TZIDS.get(tzid)
            if canonical_tzid is None:
                return None
        try:
            if parameters.get("VALUE", "").upper() == "DATE" or re.fullmatch(
                r"[0-9]{8}", raw
            ):
                if tzid is not None:
                    return None
                parsed = datetime.strptime(raw, "%Y%m%d").replace(tzinfo=_US_EASTERN)
            elif raw.endswith("Z"):
                if tzid is not None:
                    return None
                parsed = datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=timezone.utc
                )
            else:
                parsed = datetime.strptime(raw, "%Y%m%dT%H%M%S").replace(
                    tzinfo=ZoneInfo(canonical_tzid or "America/New_York")
                )
        except (ValueError, KeyError):
            return None
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _calendar_reference_period(value: str) -> str:
        month_pattern = "|".join(name.title() for name in _MONTHS)
        match = re.search(
            rf"\b({month_pattern})\s+((?:19|20)[0-9]{{2}})\b",
            value,
            re.I,
        )
        if not match:
            return ""
        month = _MONTHS[match.group(1).casefold()]
        return f"{match.group(2)}-{month:02d}"

    @staticmethod
    def _parse_treasury_calendar(raw: bytes, *, now: datetime) -> list[dict[str, Any]]:
        payload = _json_bytes(raw, endpoint=TREASURY_RELEASE_CALENDAR_URL)
        if type(payload) is not list or len(payload) > 20_000:
            raise ValueError("official Treasury calendar shape or size was invalid")
        rows: list[dict[str, Any]] = []
        recognized_count = 0
        for event in payload:
            if type(event) is not dict:
                raise ValueError("official Treasury calendar event was not an object")
            dataset_id = event.get("datasetId")
            if dataset_id not in TREASURY_CALENDAR_DATASET_IDS:
                continue
            date_value = event.get("date")
            time_value = event.get("time")
            released = event.get("released")
            if (
                type(date_value) is not str
                or not _DATE_RE.fullmatch(date_value)
                or type(time_value) is not str
                or not _TIME_RE.fullmatch(time_value)
                or type(released) is not str
                or released not in {"true", "false"}
            ):
                raise ValueError("official Treasury calendar event fields were invalid")
            try:
                scheduled = datetime.strptime(
                    f"{date_value}T{time_value}", "%Y-%m-%dT%H:%M"
                ).replace(tzinfo=timezone.utc)
            except ValueError as exc:
                raise ValueError("official Treasury calendar event time was invalid") from exc
            recognized_count += 1
            if not OfficialMacroSourceClient._in_calendar_window(scheduled, now):
                continue
            rows.append({
                "official_id": (
                    f"treasury-calendar-v1:{dataset_id}:{date_value}"
                ),
                "authority": "treasury",
                "family": "debt_to_penny",
                "reference_period": date_value,
                "title": "Debt to the Penny scheduled release",
                "summary": "Official Treasury Fiscal Data release calendar entry.",
                "official_url": TREASURY_DEBT_TO_PENNY_PAGE_URL,
                "source_url": TREASURY_RELEASE_CALENDAR_URL,
                "scheduled_at": _utc_iso(scheduled),
                "released_at": "",
                "official_revision": False,
                "data": {},
            })
        if recognized_count == 0:
            raise ValueError(
                "official Treasury calendar contained no fixed-dataset events"
            )
        return rows


__all__ = [
    "BLS_PERIODS_PER_SERIES",
    "BLS_RELEASE_CALENDAR_URL",
    "BLS_SERIES_IDS",
    "BLS_SERIES_URLS",
    "CALENDAR_LOOKAHEAD_DAYS",
    "CALENDAR_LOOKBACK_DAYS",
    "DEFAULT_OFFICIAL_MACRO_FETCH_BYTES",
    "FEDERAL_RESERVE_FOMC_CALENDAR_URL",
    "FEDERAL_RESERVE_MONETARY_RSS_URL",
    "OFFICIAL_MACRO_ALLOWED_CONTENT_TYPES",
    "OFFICIAL_MACRO_ALLOWED_HOSTS",
    "OFFICIAL_MACRO_ENDPOINTS",
    "OFFICIAL_MACRO_INJECTED_TRANSPORT_IDENTITY",
    "OFFICIAL_MACRO_MAX_LIMIT",
    "OFFICIAL_MACRO_MAX_RESPONSE_BYTES",
    "OFFICIAL_MACRO_MAX_SOURCE_ERRORS",
    "OFFICIAL_MACRO_PARSER_VERSION",
    "OFFICIAL_MACRO_RESPONSE_BODY_DEADLINE_SECONDS",
    "OFFICIAL_MACRO_SOURCE_MANIFEST",
    "OFFICIAL_MACRO_TIMEOUT_SECONDS",
    "OFFICIAL_MACRO_TRANSPORT_IDENTITY",
    "OFFICIAL_MACRO_USER_AGENT",
    "OfficialMacroSourceClient",
    "TREASURY_CALENDAR_DATASET_IDS",
    "TREASURY_DEBT_PAGE_SIZE",
    "TREASURY_DEBT_TO_PENNY_PAGE_URL",
    "TREASURY_DEBT_TO_PENNY_URL",
    "TREASURY_RELEASE_CALENDAR_URL",
]
