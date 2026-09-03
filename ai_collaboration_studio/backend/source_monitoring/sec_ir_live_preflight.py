"""One-shot, bounded SEC and company-IR production-path observation.

The supported production entry is the isolated CLI wrapper.  It selects the
code-defined HTTPS transports and exercises the two production
source-monitoring adapters exactly once.  Direct production calls outside that
clean bootstrap fail before network work.  The bootstrap disables local-env
loading, so ``SEC_USER_AGENT`` must already exist in the process environment.
It does not open SQLite, Source Inbox, provider, Futu, model, or execution
services.  Reports contain no endpoint, response, exception, or User-Agent
text.  The inherited IR transport admits redirects only within its fixed host
set; the report explicitly declines to attest an exact final redirect URL.

This is deliberately not a network or source-truth attestation.  An injected
fixture run has a distinct evidence class and cannot validate as production.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import socket
import ssl
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError

_PREIMPORT_FORBIDDEN_EXACT = frozenset({
    "backend.config",
    "backend.store",
    "backend.source_inbox_service",
    "backend.provider_gateway",
    "backend.source_monitoring.runtime",
    "backend.source_monitoring.state_repository",
    "backend.source_monitoring.supervisor",
})
_PREIMPORT_FORBIDDEN_PREFIXES = (
    "backend.market.",
    "backend.source_monitoring.adapters.",
)
_PREIMPORT_DEPENDENCIES_CLEAN = not any(
    name in _PREIMPORT_FORBIDDEN_EXACT
    or any(name.startswith(prefix) for prefix in _PREIMPORT_FORBIDDEN_PREFIXES)
    for name in tuple(sys.modules)
)

from .. import config as _config_module
from ..market import ir_releases as _ir_module
from ..market import official_http as _official_http_module
from ..market import sec_edgar as _sec_module
from ..market.ir_releases import IR_FEEDS, IR_MAX_RESPONSE_BYTES, OfficialIrReleaseAdapter
from ..market.sec_edgar import (
    SEC_ARCHIVES_BASE,
    SEC_DEFAULT_FORMS,
    SEC_MAX_RESPONSE_BYTES,
    SEC_MONITOR_SYMBOLS,
    SEC_SUBMISSIONS_URL,
    SEC_TICKERS_URL,
    SecEdgarAdapter,
)
from .adapters import company_ir as _company_ir_module
from .adapters import sec_filings as _sec_filings_module
from .adapters.company_ir import CompanyIrSourceAdapter
from .adapters.sec_filings import SecFilingsSourceAdapter
from .contracts import AdapterPollResult, canonical_sha256


SEC_IR_LIVE_PREFLIGHT_VERSION = "sec_ir_live_preflight_v1"
SEC_IR_LIVE_PREFLIGHT_CONFIRMATION = "RUN_SEC_IR_LIVE_PREFLIGHT_ONCE"
SEC_IR_LIVE_PREFLIGHT_MAX_OUTPUT_BYTES = 16_384

_SEC_SYMBOLS = tuple(SEC_MONITOR_SYMBOLS)
_SEC_FORMS = tuple(SEC_DEFAULT_FORMS)
_SEC_TICKER_ENDPOINT = SEC_TICKERS_URL
_SEC_SUBMISSIONS_TEMPLATE = SEC_SUBMISSIONS_URL
_SEC_ARCHIVES_ENDPOINT_BASE = SEC_ARCHIVES_BASE
_IR_FEED_SNAPSHOT = tuple(
    (
        symbol,
        str(IR_FEEDS[symbol]["url"]),
        tuple(sorted(IR_FEEDS[symbol]["hosts"])),
    )
    for symbol in tuple(IR_FEEDS)
)
_IR_SYMBOLS = tuple(item[0] for item in _IR_FEED_SNAPSHOT)
_SEC_ENDPOINT_COUNT = 1 + len(_SEC_SYMBOLS)
_IR_ENDPOINT_COUNT = len(_IR_FEED_SNAPSHOT)
_ENDPOINT_FETCH_ATTEMPT_LIMIT = _SEC_ENDPOINT_COUNT + _IR_ENDPOINT_COUNT
_SEC_PER_SYMBOL_LIMIT = 6
_IR_PER_SYMBOL_LIMIT = 8
_MAX_COUNT = 1_000
_MAX_ELAPSED_MS = 1_000_000
_STATUSES = ("passed", "degraded", "failed")
_PRODUCTION_TRANSPORT_IDENTITY = "guarded_sec_ir_default_https_v1"
_DEPENDENCY_GUARD_VERSION = "sec_ir_direct_dependency_guard_v1"
_NETWORK_REDIRECT_LIMIT_PER_FETCH = 5
_NETWORK_REDIRECT_REPEAT_LIMIT_PER_FETCH = 2
_NETWORK_REQUESTS_PER_FETCH_UPPER_BOUND = 1 + max(
    _NETWORK_REDIRECT_LIMIT_PER_FETCH,
    _NETWORK_REDIRECT_REPEAT_LIMIT_PER_FETCH,
)
_IMPORT_GUARD_ENV = "AI_STUDIO_SEC_IR_PREFLIGHT_IMPORT_GUARD"
_IMPORT_GUARD_VALUE = "sec-ir-isolated-import-v1"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_IMPORT_GUARD_DATABASE_PATH = (
    _PROJECT_ROOT / ".sec-ir-preflight-database-must-not-open.sqlite3"
)
_ISOLATED_CLI_IMPORT_GUARD_ATTESTED = bool(
    os.getenv(_IMPORT_GUARD_ENV) == _IMPORT_GUARD_VALUE
    and os.getenv("AI_STUDIO_SKIP_LOCAL_ENV") == "1"
    and sys.flags.isolated == 1
    and sys.dont_write_bytecode is True
    and _PREIMPORT_DEPENDENCIES_CLEAN
    and _config_module.RUNTIME_DIR == _PROJECT_ROOT
    and _config_module.DATABASE_PATH == _IMPORT_GUARD_DATABASE_PATH
    and _PROJECT_ROOT.is_dir()
    and not _IMPORT_GUARD_DATABASE_PATH.exists()
)

_SEC_DEFAULT_FETCH_TOKEN = SecEdgarAdapter._default_fetch_json
_IR_DEFAULT_FETCH_TOKEN = OfficialIrReleaseAdapter._default_fetch_bytes
_SEC_POLL_TOKEN = SecFilingsSourceAdapter.poll
_IR_POLL_TOKEN = CompanyIrSourceAdapter.poll
_SEC_BATCH_TOKEN = SecEdgarAdapter.monitoring_filings_batch
_IR_BATCH_TOKEN = OfficialIrReleaseAdapter.monitoring_releases_batch
_SEC_URL_VALIDATOR_TOKEN = _sec_module._is_allowed_sec_fetch_url
_SEC_OPENER_TOKEN = _sec_module.open_official_https
_IR_OPENER_TOKEN = _ir_module.open_official_https
_SEC_REQUEST_TOKEN = _sec_module.Request
_IR_REQUEST_TOKEN = _ir_module.Request
_SEC_URLPARSE_TOKEN = _sec_module.urlparse
_IR_URLPARSE_TOKEN = _ir_module.urlparse
_SEC_JSON_LOADS_TOKEN = _sec_module.json.loads
_HTTPS_REDIRECT_HANDLER_TOKEN = _official_http_module.OfficialHttpsRedirectHandler
_HTTPS_BUILD_OPENER_TOKEN = _official_http_module.build_opener
_SEC_FEED_MAP_TOKEN = _ir_module.IR_FEEDS
_SEC_USER_AGENT_TOKEN = _sec_module.SEC_USER_AGENT

SEC_IR_LIVE_PREFLIGHT_SOURCE_MANIFEST_SHA256 = canonical_sha256({
    "version": "sec_ir_source_manifest_v1",
    "sec": {
        "symbols": list(_SEC_SYMBOLS),
        "forms": list(_SEC_FORMS),
        "ticker_endpoint": _SEC_TICKER_ENDPOINT,
        "submissions_template": _SEC_SUBMISSIONS_TEMPLATE,
        "archives_base_not_fetched": _SEC_ARCHIVES_ENDPOINT_BASE,
        "maximum_response_bytes": SEC_MAX_RESPONSE_BYTES,
        "per_symbol_limit": _SEC_PER_SYMBOL_LIMIT,
    },
    "company_ir": {
        "feeds": [
            {"symbol": symbol, "url": url, "hosts": list(hosts)}
            for symbol, url, hosts in _IR_FEED_SNAPSHOT
        ],
        "maximum_response_bytes": IR_MAX_RESPONSE_BYTES,
        "per_symbol_limit": _IR_PER_SYMBOL_LIMIT,
    },
})
SEC_IR_LIVE_PREFLIGHT_TRANSPORT_IDENTITY_SHA256 = hashlib.sha256(
    _PRODUCTION_TRANSPORT_IDENTITY.encode("ascii")
).hexdigest()
SEC_IR_LIVE_PREFLIGHT_PRODUCTION_EVIDENCE_PROFILE_SHA256 = canonical_sha256({
    "version": SEC_IR_LIVE_PREFLIGHT_VERSION,
    "scope": "sec_and_company_ir_only",
    "evidence_class": "production_path_observation",
    "transport": _PRODUCTION_TRANSPORT_IDENTITY,
    "dependency_guard": _DEPENDENCY_GUARD_VERSION,
    "source_manifest_sha256": SEC_IR_LIVE_PREFLIGHT_SOURCE_MANIFEST_SHA256,
    "live_network_attested": False,
    "source_truth_verified": False,
    "network_redirect_limit_per_fetch": _NETWORK_REDIRECT_LIMIT_PER_FETCH,
    "network_redirect_repeat_limit_per_fetch": (
        _NETWORK_REDIRECT_REPEAT_LIMIT_PER_FETCH
    ),
    "redirect_policy_scope": "production_default_fixed_hosts_not_exact_final_url",
    "configuration_scope": "explicit_process_sec_user_agent_only",
    "production_local_env_configuration_attested": False,
})
SEC_IR_LIVE_PREFLIGHT_INJECTED_EVIDENCE_PROFILE_SHA256 = canonical_sha256({
    "version": SEC_IR_LIVE_PREFLIGHT_VERSION,
    "scope": "sec_and_company_ir_only",
    "evidence_class": "injected_offline_fixture",
    "transport": "explicit_test_dependencies_v1",
    "live_network_attested": False,
    "source_truth_verified": False,
})

_ERROR_CODE_CATEGORIES = {
    "": "none",
    "SEC_USER_AGENT_REQUIRED": "configuration",
    "TLS_HANDSHAKE_EOF": "tls",
    "TLS_CERTIFICATE_VERIFY_FAILED": "tls",
    "TLS_HANDSHAKE_FAILED": "tls",
    "DNS_RESOLUTION_FAILED": "dns",
    "NETWORK_TIMEOUT": "timeout",
    "NETWORK_CONNECTION_FAILED": "network",
    "NETWORK_TRANSPORT_FAILED": "network",
    "HTTP_ACCESS_DENIED": "http",
    "HTTP_NOT_FOUND": "http",
    "HTTP_RATE_LIMITED": "http",
    "HTTP_CLIENT_ERROR": "http",
    "HTTP_SERVER_ERROR": "http",
    "HTTP_STATUS_ERROR": "http",
    "TRANSPORT_POLICY_REJECTED": "transport_policy",
    "TRANSPORT_RESPONSE_INVALID": "transport_policy",
    "ENDPOINT_COVERAGE_INCOMPLETE": "transport_policy",
    "MULTIPLE_TRANSPORT_FAILURES": "network",
    "SOURCE_PAYLOAD_INVALID": "source_payload",
    "SOURCE_EMPTY": "source_payload",
    "PREFLIGHT_DEPENDENCY_GUARD_FAILED": "internal",
    "PREFLIGHT_ADAPTER_CONSTRUCTION_FAILED": "internal",
    "PREFLIGHT_INTERNAL_ERROR": "internal",
}
_ROW_COUNT_KEYS = (
    "endpoint_count",
    "endpoint_success_count",
    "endpoint_fetch_attempt_count",
    "transport_failure_count",
    "record_count",
    "duplicate_count",
    "rejected_count",
    "source_error_count",
)
_ROW_KEYS = frozenset({
    "adapter_key",
    "status",
    *_ROW_COUNT_KEYS,
    "error_code",
    "error_category",
    "elapsed_ms",
})
_COUNT_KEYS = (
    "adapter_count",
    "passed_count",
    "degraded_count",
    "failed_count",
    *_ROW_COUNT_KEYS,
)
_SAFETY_KEYS = frozenset({
    "read_only",
    "one_shot",
    "confirmation_required",
    "confirmation_verified",
    "isolated_cli_import_guard_attested",
    "sec_user_agent_declared",
    "sec_user_agent_source",
    "local_env_loading_disabled",
    "runtime_path_configuration_overridden",
    "production_local_env_configuration_attested",
    "network_requests_performed",
    "network_requests_accounting",
    "network_request_upper_bound",
    "network_redirect_limit_per_fetch",
    "network_redirect_repeat_limit_per_fetch",
    "endpoint_fetch_attempts_performed",
    "endpoint_fetch_attempts_accounting",
    "endpoint_fetch_attempt_limit",
    "retries_performed",
    "initial_endpoint_allowlist_enforced",
    "final_endpoint_identity_attested",
    "redirect_policy_scope",
    "transport_mode",
    "live_network_attested",
    "source_truth_verified",
    "production_acceptance_verdict",
    "overall_acceptance",
    "in_process_tamper_resistant",
    "proxy_configuration_overridden",
    "tls_verification_disabled",
    "application_file_writes_performed",
    "database_reads_performed",
    "database_writes_performed",
    "checkpoint_writes_performed",
    "source_inbox_writes_performed",
    "provider_calls_performed",
    "model_calls_performed",
    "futu_calls_performed",
    "formal_rounds_created",
    "execution_capability",
    "live_trading_allowed",
    "http_listener_started",
})


class SecIrLivePreflightConfirmationError(ValueError):
    code = "PREFLIGHT_CONFIRMATION_REQUIRED"

    def __init__(self) -> None:
        super().__init__("exact SEC/IR live preflight confirmation is required")


class SecIrLivePreflightEnvironmentError(RuntimeError):
    code = "PREFLIGHT_IMPORT_GUARD_REQUIRED"

    def __init__(self) -> None:
        super().__init__("the isolated SEC/IR CLI import guard is required")


class SecIrLivePreflightIndeterminateError(RuntimeError):
    """Post-action state cannot be represented with exact accounting."""


class _BoundaryError(ValueError):
    """A closed internal transport-boundary failure."""


@dataclass(frozen=True, slots=True)
class SecIrLivePreflightDependencies:
    """Explicit offline seams; their reports are never production evidence."""

    sec_fetch_json: Callable[[str, str], dict[str, Any]] | None = None
    ir_fetch_bytes: Callable[[str, set[str]], bytes] | None = None
    sec_user_agent: str = "AI Studio fixture preflight@example.invalid"
    clock: Callable[[], datetime] | None = None
    monotonic: Callable[[], float] | None = None


def _dependencies_are_current() -> bool:
    try:
        return bool(
            sys.flags.isolated == 1
            and sys.dont_write_bytecode is True
            and _PREIMPORT_DEPENDENCIES_CLEAN
            and _sec_module.SecEdgarAdapter is SecEdgarAdapter
            and _ir_module.OfficialIrReleaseAdapter is OfficialIrReleaseAdapter
            and _sec_filings_module.SecFilingsSourceAdapter is SecFilingsSourceAdapter
            and _company_ir_module.CompanyIrSourceAdapter is CompanyIrSourceAdapter
            and SecEdgarAdapter._default_fetch_json is _SEC_DEFAULT_FETCH_TOKEN
            and OfficialIrReleaseAdapter._default_fetch_bytes is _IR_DEFAULT_FETCH_TOKEN
            and SecFilingsSourceAdapter.poll is _SEC_POLL_TOKEN
            and CompanyIrSourceAdapter.poll is _IR_POLL_TOKEN
            and SecEdgarAdapter.monitoring_filings_batch is _SEC_BATCH_TOKEN
            and OfficialIrReleaseAdapter.monitoring_releases_batch is _IR_BATCH_TOKEN
            and _sec_module._is_allowed_sec_fetch_url is _SEC_URL_VALIDATOR_TOKEN
            and _sec_module.open_official_https is _SEC_OPENER_TOKEN
            and _ir_module.open_official_https is _IR_OPENER_TOKEN
            and _sec_module.Request is _SEC_REQUEST_TOKEN
            and _ir_module.Request is _IR_REQUEST_TOKEN
            and _sec_module.urlparse is _SEC_URLPARSE_TOKEN
            and _ir_module.urlparse is _IR_URLPARSE_TOKEN
            and _sec_module.json.loads is _SEC_JSON_LOADS_TOKEN
            and _official_http_module.OfficialHttpsRedirectHandler
            is _HTTPS_REDIRECT_HANDLER_TOKEN
            and _official_http_module.build_opener is _HTTPS_BUILD_OPENER_TOKEN
            and _official_http_module.open_official_https is _SEC_OPENER_TOKEN
            and _HTTPS_REDIRECT_HANDLER_TOKEN.max_redirections
            == _NETWORK_REDIRECT_LIMIT_PER_FETCH
            and _HTTPS_REDIRECT_HANDLER_TOKEN.max_repeats
            == _NETWORK_REDIRECT_REPEAT_LIMIT_PER_FETCH
            and (
                not _ISOLATED_CLI_IMPORT_GUARD_ATTESTED
                or (
                    _config_module.RUNTIME_DIR == _PROJECT_ROOT
                    and _config_module.DATABASE_PATH
                    == _IMPORT_GUARD_DATABASE_PATH
                    and not _IMPORT_GUARD_DATABASE_PATH.exists()
                )
            )
            and _ir_module.IR_FEEDS is _SEC_FEED_MAP_TOKEN
            and _company_ir_module.IR_FEEDS is _SEC_FEED_MAP_TOKEN
            and tuple(_sec_module.SEC_MONITOR_SYMBOLS) == _SEC_SYMBOLS
            and tuple(_sec_filings_module.SEC_MONITOR_SYMBOLS) == _SEC_SYMBOLS
            and tuple(_sec_module.SEC_DEFAULT_FORMS) == _SEC_FORMS
            and tuple(_sec_filings_module.SEC_DEFAULT_FORMS) == _SEC_FORMS
            and _sec_module.SEC_TICKERS_URL == _SEC_TICKER_ENDPOINT
            and _sec_module.SEC_SUBMISSIONS_URL == _SEC_SUBMISSIONS_TEMPLATE
            and _sec_module.SEC_ARCHIVES_BASE == _SEC_ARCHIVES_ENDPOINT_BASE
            and _sec_filings_module.SEC_ARCHIVES_BASE == _SEC_ARCHIVES_ENDPOINT_BASE
            and _sec_module.SEC_MAX_RESPONSE_BYTES == SEC_MAX_RESPONSE_BYTES
            and _ir_module.IR_MAX_RESPONSE_BYTES == IR_MAX_RESPONSE_BYTES
            and _sec_module.SEC_USER_AGENT == _SEC_USER_AGENT_TOKEN
            and tuple(
                (
                    symbol,
                    str(_ir_module.IR_FEEDS[symbol]["url"]),
                    tuple(sorted(_ir_module.IR_FEEDS[symbol]["hosts"])),
                )
                for symbol in tuple(_ir_module.IR_FEEDS)
            ) == _IR_FEED_SNAPSHOT
        )
    except Exception:
        return False


def _declared_user_agent(value: Any) -> bool:
    return bool(
        type(value) is str
        and value == value.strip()
        and 10 <= len(value) <= 300
        and "@" in value
        and "\r" not in value
        and "\n" not in value
    )


def _classify_transport(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, _BoundaryError):
        return "TRANSPORT_POLICY_REJECTED", "transport_policy"
    if isinstance(exc, HTTPError):
        status = exc.code if type(exc.code) is int else 0
        if status in {401, 403}:
            return "HTTP_ACCESS_DENIED", "http"
        if status == 404:
            return "HTTP_NOT_FOUND", "http"
        if status == 429:
            return "HTTP_RATE_LIMITED", "http"
        if 400 <= status <= 499:
            return "HTTP_CLIENT_ERROR", "http"
        if 500 <= status <= 599:
            return "HTTP_SERVER_ERROR", "http"
        return "HTTP_STATUS_ERROR", "http"
    if isinstance(exc, URLError):
        reason = getattr(exc, "reason", None)
        return (
            _classify_transport(reason)
            if isinstance(reason, BaseException) and reason is not exc
            else ("NETWORK_TRANSPORT_FAILED", "network")
        )
    if isinstance(exc, ssl.SSLEOFError):
        return "TLS_HANDSHAKE_EOF", "tls"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "TLS_CERTIFICATE_VERIFY_FAILED", "tls"
    if isinstance(exc, ssl.SSLError):
        return "TLS_HANDSHAKE_FAILED", "tls"
    if isinstance(exc, socket.gaierror):
        return "DNS_RESOLUTION_FAILED", "dns"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "NETWORK_TIMEOUT", "timeout"
    if isinstance(
        exc,
        (ConnectionAbortedError, ConnectionRefusedError, ConnectionResetError, BrokenPipeError),
    ):
        return "NETWORK_CONNECTION_FAILED", "network"
    if isinstance(exc, OSError):
        connection_numbers = {
            errno.ECONNABORTED,
            errno.ECONNREFUSED,
            errno.ECONNRESET,
            errno.ENETDOWN,
            errno.ENETUNREACH,
            errno.EHOSTDOWN,
            errno.EHOSTUNREACH,
        }
        if getattr(exc, "errno", None) in connection_numbers:
            return "NETWORK_CONNECTION_FAILED", "network"
        return "NETWORK_TRANSPORT_FAILED", "network"
    return "PREFLIGHT_INTERNAL_ERROR", "internal"


def _json_response_is_bounded(value: Any) -> bool:
    if type(value) is not dict:
        return False
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError):
        return False
    return len(encoded) <= SEC_MAX_RESPONSE_BYTES


def _submission_urls_from_ticker_payload(payload: dict[str, Any]) -> frozenset[str]:
    wanted = {symbol.removeprefix("US.") for symbol in _SEC_SYMBOLS}
    urls: set[str] = set()
    for item in payload.values():
        if type(item) is not dict:
            continue
        ticker = item.get("ticker")
        if type(ticker) is not str or ticker.strip().upper() not in wanted:
            continue
        raw_cik = item.get("cik_str")
        if type(raw_cik) is not int or not 0 <= raw_cik <= 9_999_999_999:
            continue
        cik = f"{raw_cik:010d}"
        if len(cik) != 10 or not cik.isascii() or not cik.isdigit():
            continue
        urls.add(_SEC_SUBMISSIONS_TEMPLATE.format(cik=cik))
    return frozenset(urls)


class _SecFetchBoundary:
    def __init__(
        self,
        fetch: Callable[[str, str], dict[str, Any]],
        *,
        user_agent: str,
        production: bool,
    ) -> None:
        self._fetch = fetch
        self._user_agent = user_agent
        self._production = production
        self._attempted: set[str] = set()
        self._allowed_submissions: frozenset[str] | None = None
        self.attempts = 0
        self.successes = 0
        self.failures: list[tuple[str, str]] = []

    def _fail(self, code: str, category: str) -> None:
        self.failures.append((code, category))

    def __call__(self, endpoint: str, user_agent: str) -> dict[str, Any]:
        try:
            if self._production and not _dependencies_are_current():
                raise _BoundaryError("production dependencies changed")
            if (
                type(endpoint) is not str
                or type(user_agent) is not str
                or user_agent != self._user_agent
                or not _declared_user_agent(user_agent)
                or self.attempts >= _SEC_ENDPOINT_COUNT
                or endpoint in self._attempted
            ):
                raise _BoundaryError("SEC fetch boundary rejected the call")
            if endpoint == _SEC_TICKER_ENDPOINT:
                if self.attempts != 0 or self._allowed_submissions is not None:
                    raise _BoundaryError("SEC ticker endpoint order changed")
            elif (
                self._allowed_submissions is None
                or endpoint not in self._allowed_submissions
            ):
                raise _BoundaryError("SEC submissions endpoint is not response-bound")
            self._attempted.add(endpoint)
            self.attempts += 1
            payload = self._fetch(endpoint, user_agent)
            if not _json_response_is_bounded(payload):
                raise _BoundaryError("SEC response is invalid or oversized")
            if endpoint == _SEC_TICKER_ENDPOINT:
                self._allowed_submissions = _submission_urls_from_ticker_payload(payload)
            if self._production and not _dependencies_are_current():
                raise _BoundaryError("production dependencies changed")
            self.successes += 1
            return payload
        except BaseException as exc:
            self._fail(*_classify_transport(exc))
            raise


class _IrFetchBoundary:
    def __init__(
        self,
        fetch: Callable[[str, set[str]], bytes],
        *,
        production: bool,
    ) -> None:
        self._fetch = fetch
        self._production = production
        self._allowed = {
            url: frozenset(hosts) for _symbol, url, hosts in _IR_FEED_SNAPSHOT
        }
        self._attempted: set[str] = set()
        self._lock = threading.Lock()
        self.attempts = 0
        self.successes = 0
        self.failures: list[tuple[str, str]] = []

    def __call__(self, endpoint: str, allowed_hosts: set[str]) -> bytes:
        try:
            if self._production and not _dependencies_are_current():
                raise _BoundaryError("production dependencies changed")
            with self._lock:
                expected_hosts = self._allowed.get(endpoint) if type(endpoint) is str else None
                if (
                    type(allowed_hosts) is not set
                    or any(type(host) is not str for host in allowed_hosts)
                    or expected_hosts is None
                    or frozenset(allowed_hosts) != expected_hosts
                    or endpoint in self._attempted
                    or self.attempts >= _IR_ENDPOINT_COUNT
                ):
                    raise _BoundaryError("company IR fetch boundary rejected the call")
                self._attempted.add(endpoint)
                self.attempts += 1
            raw = self._fetch(endpoint, allowed_hosts)
            if type(raw) is not bytes or len(raw) > IR_MAX_RESPONSE_BYTES:
                raise _BoundaryError("company IR response is invalid or oversized")
            if self._production and not _dependencies_are_current():
                raise _BoundaryError("production dependencies changed")
            with self._lock:
                self.successes += 1
            return raw
        except BaseException as exc:
            with self._lock:
                self.failures.append(_classify_transport(exc))
            raise


def _monotonic_value(read: Callable[[], float]) -> float:
    try:
        value = read()
    except Exception:
        return 0.0
    return float(value) if type(value) in {int, float} and math.isfinite(value) else 0.0


def _elapsed(started: float, read: Callable[[], float]) -> int:
    finished = _monotonic_value(read)
    return min(max(0, int(round((finished - started) * 1_000))), _MAX_ELAPSED_MS)


def _failure_code(failures: list[tuple[str, str]]) -> tuple[str, str]:
    unique = tuple(dict.fromkeys(failures))
    if len(unique) == 1:
        return unique[0]
    if unique and all(category in {"network", "dns", "timeout", "tls", "http"} for _, category in unique):
        return "MULTIPLE_TRANSPORT_FAILURES", "network"
    return "PREFLIGHT_INTERNAL_ERROR", "internal"


def _count(value: Any) -> int:
    return value if type(value) is int and 0 <= value <= _MAX_COUNT else 0


def _failed_row(
    adapter_key: str,
    endpoint_count: int,
    boundary: Any | None,
    *,
    code: str,
    category: str,
    elapsed_ms: int,
) -> dict[str, Any]:
    return {
        "adapter_key": adapter_key,
        "status": "failed",
        "endpoint_count": endpoint_count,
        "endpoint_success_count": _count(getattr(boundary, "successes", 0)),
        "endpoint_fetch_attempt_count": _count(getattr(boundary, "attempts", 0)),
        "transport_failure_count": _count(len(getattr(boundary, "failures", ()))),
        "record_count": 0,
        "duplicate_count": 0,
        "rejected_count": 0,
        "source_error_count": 1,
        "error_code": code,
        "error_category": category,
        "elapsed_ms": elapsed_ms,
    }


def _result_row(
    *,
    adapter_key: str,
    endpoint_count: int,
    result: AdapterPollResult,
    boundary: Any,
    elapsed_ms: int,
) -> dict[str, Any]:
    records = len(result.observed_items)
    source_errors = len(result.source_errors)
    failures = list(boundary.failures)
    complete = boundary.attempts == endpoint_count and boundary.successes == endpoint_count
    if failures:
        code, category = _failure_code(failures)
    elif not complete:
        code, category = "ENDPOINT_COVERAGE_INCOMPLETE", "transport_policy"
    elif source_errors or result.rejected_count:
        code, category = "SOURCE_PAYLOAD_INVALID", "source_payload"
    elif not records:
        code, category = "SOURCE_EMPTY", "source_payload"
    else:
        code, category = "", "none"
    if not code:
        status = "passed"
    elif records:
        status = "degraded"
    else:
        status = "failed"
    return {
        "adapter_key": adapter_key,
        "status": status,
        "endpoint_count": endpoint_count,
        "endpoint_success_count": _count(boundary.successes),
        "endpoint_fetch_attempt_count": _count(boundary.attempts),
        "transport_failure_count": _count(len(failures)),
        "record_count": _count(records),
        "duplicate_count": _count(result.duplicate_count),
        "rejected_count": _count(result.rejected_count),
        "source_error_count": _count(source_errors),
        "error_code": code,
        "error_category": category,
        "elapsed_ms": elapsed_ms,
    }


def _run_sec(
    *,
    fetch: Callable[[str, str], dict[str, Any]],
    user_agent: str,
    observed_at: datetime,
    captured_at_ms: int,
    monotonic: Callable[[], float],
    production: bool,
) -> dict[str, Any]:
    started = _monotonic_value(monotonic)
    if not _declared_user_agent(user_agent):
        return _failed_row(
            "sec_filings",
            _SEC_ENDPOINT_COUNT,
            None,
            code="SEC_USER_AGENT_REQUIRED",
            category="configuration",
            elapsed_ms=_elapsed(started, monotonic),
        )
    boundary = _SecFetchBoundary(fetch, user_agent=user_agent, production=production)
    try:
        inner = SecEdgarAdapter(
            user_agent=user_agent,
            cache_ttl_seconds=60.0,
            fetch_json=boundary,
            clock=lambda: observed_at,
            min_request_interval_seconds=0.11,
            allowed_symbols=list(_SEC_SYMBOLS),
        )
        adapter = SecFilingsSourceAdapter(
            adapter=inner,
            allowed_symbols=list(_SEC_SYMBOLS),
            allowed_forms=list(_SEC_FORMS),
            per_symbol_limit=_SEC_PER_SYMBOL_LIMIT,
            force=True,
        )
        if (
            type(inner) is not SecEdgarAdapter
            or type(adapter) is not SecFilingsSourceAdapter
            or inner._fetch_json is not boundary
            or inner.user_agent != user_agent
            or tuple(inner.allowed_symbols) != _SEC_SYMBOLS
            or tuple(adapter.allowed_symbols) != _SEC_SYMBOLS
            or tuple(adapter.allowed_forms) != _SEC_FORMS
            or adapter.per_symbol_limit != _SEC_PER_SYMBOL_LIMIT
            or adapter.force is not True
            or adapter._inner_transport_mode != "sec_injected_transport_v1"
            or adapter.official_source is not True
            or adapter.execution_capability != "none"
            or adapter.live_trading_allowed is not False
        ):
            raise _BoundaryError("SEC adapter construction changed")
        result = adapter.poll(
            {},
            observed_at_ms=captured_at_ms,
            max_items=adapter.max_candidates_per_poll,
        )
        if type(result) is not AdapterPollResult or result.adapter_key != "sec_filings":
            raise _BoundaryError("SEC adapter result changed")
        return _result_row(
            adapter_key="sec_filings",
            endpoint_count=_SEC_ENDPOINT_COUNT,
            result=result,
            boundary=boundary,
            elapsed_ms=_elapsed(started, monotonic),
        )
    except BaseException as exc:
        code, category = _classify_transport(exc)
        if isinstance(exc, _BoundaryError) and not boundary.failures:
            code, category = "PREFLIGHT_ADAPTER_CONSTRUCTION_FAILED", "internal"
        return _failed_row(
            "sec_filings",
            _SEC_ENDPOINT_COUNT,
            boundary,
            code=code,
            category=category,
            elapsed_ms=_elapsed(started, monotonic),
        )


def _run_ir(
    *,
    fetch: Callable[[str, set[str]], bytes],
    observed_at: datetime,
    captured_at_ms: int,
    monotonic: Callable[[], float],
    production: bool,
) -> dict[str, Any]:
    started = _monotonic_value(monotonic)
    boundary = _IrFetchBoundary(fetch, production=production)
    try:
        inner = OfficialIrReleaseAdapter(
            fetch_bytes=boundary,
            clock=lambda: observed_at,
            monotonic=monotonic,
            cache_ttl_seconds=60.0,
        )
        adapter = CompanyIrSourceAdapter(
            adapter=inner,
            symbols=list(_IR_SYMBOLS),
            per_symbol_limit=_IR_PER_SYMBOL_LIMIT,
            force=True,
        )
        if (
            type(inner) is not OfficialIrReleaseAdapter
            or type(adapter) is not CompanyIrSourceAdapter
            or inner._fetch_bytes is not boundary
            or tuple(adapter.symbols) != _IR_SYMBOLS
            or adapter.per_symbol_limit != _IR_PER_SYMBOL_LIMIT
            or adapter.force is not True
            or adapter._inner_transport_mode != "company_ir_injected_transport_v1"
            or adapter.official_source is not True
            or adapter.execution_capability != "none"
            or adapter.live_trading_allowed is not False
        ):
            raise _BoundaryError("company IR adapter construction changed")
        result = adapter.poll(
            {},
            observed_at_ms=captured_at_ms,
            max_items=adapter.max_candidates_per_poll,
        )
        if type(result) is not AdapterPollResult or result.adapter_key != "company_ir":
            raise _BoundaryError("company IR adapter result changed")
        return _result_row(
            adapter_key="company_ir",
            endpoint_count=_IR_ENDPOINT_COUNT,
            result=result,
            boundary=boundary,
            elapsed_ms=_elapsed(started, monotonic),
        )
    except BaseException as exc:
        code, category = _classify_transport(exc)
        if isinstance(exc, _BoundaryError) and not boundary.failures:
            code, category = "PREFLIGHT_ADAPTER_CONSTRUCTION_FAILED", "internal"
        return _failed_row(
            "company_ir",
            _IR_ENDPOINT_COUNT,
            boundary,
            code=code,
            category=category,
            elapsed_ms=_elapsed(started, monotonic),
        )


def _safety(attempts: int, *, production: bool, user_agent_declared: bool) -> dict[str, Any]:
    import_guard_attested = production
    return {
        "read_only": True if import_guard_attested else None,
        "one_shot": True,
        "confirmation_required": True,
        "confirmation_verified": True,
        "isolated_cli_import_guard_attested": import_guard_attested,
        "sec_user_agent_declared": user_agent_declared,
        "sec_user_agent_source": (
            "explicit_process_environment_only" if production else "injected_fixture"
        ),
        "local_env_loading_disabled": True if production else None,
        "runtime_path_configuration_overridden": True if production else None,
        "production_local_env_configuration_attested": False,
        "network_requests_performed": None,
        "network_requests_accounting": "not_instrumented",
        "network_request_upper_bound": (
            attempts * _NETWORK_REQUESTS_PER_FETCH_UPPER_BOUND
            if production
            else None
        ),
        "network_redirect_limit_per_fetch": (
            _NETWORK_REDIRECT_LIMIT_PER_FETCH if production else None
        ),
        "network_redirect_repeat_limit_per_fetch": (
            _NETWORK_REDIRECT_REPEAT_LIMIT_PER_FETCH if production else None
        ),
        "endpoint_fetch_attempts_performed": attempts,
        "endpoint_fetch_attempts_accounting": "exact",
        "endpoint_fetch_attempt_limit": _ENDPOINT_FETCH_ATTEMPT_LIMIT,
        "retries_performed": 0 if production else None,
        "initial_endpoint_allowlist_enforced": True,
        "final_endpoint_identity_attested": False,
        "redirect_policy_scope": (
            "production_default_fixed_hosts_not_exact_final_url"
            if production
            else "not_applicable_offline_fixture"
        ),
        "transport_mode": "guarded_default_sec_ir_https_path" if production else "injected_offline",
        "live_network_attested": False,
        "source_truth_verified": False,
        "production_acceptance_verdict": "NOT_EVALUATED",
        "overall_acceptance": "NOT_CLAIMED",
        "in_process_tamper_resistant": False,
        "proxy_configuration_overridden": False if production else None,
        "tls_verification_disabled": False if production else None,
        "application_file_writes_performed": 0 if import_guard_attested else None,
        "database_reads_performed": 0 if production else None,
        "database_writes_performed": 0 if production else None,
        "checkpoint_writes_performed": 0 if production else None,
        "source_inbox_writes_performed": 0 if production else None,
        "provider_calls_performed": 0 if production else None,
        "model_calls_performed": 0 if production else None,
        "futu_calls_performed": 0 if production else None,
        "formal_rounds_created": 0 if production else None,
        "execution_capability": "none" if production else "unknown",
        "live_trading_allowed": False if production else None,
        "http_listener_started": False if production else None,
    }


def _assemble(
    rows: list[dict[str, Any]],
    *,
    captured_at_ms: int,
    production: bool,
    user_agent_declared: bool,
) -> dict[str, Any]:
    status_counts = {status: sum(row["status"] == status for row in rows) for status in _STATUSES}
    status = "failed" if status_counts["failed"] else "degraded" if status_counts["degraded"] else "passed"
    counts = {
        "adapter_count": len(rows),
        **{f"{status_name}_count": status_counts[status_name] for status_name in _STATUSES},
        **{key: sum(row[key] for row in rows) for key in _ROW_COUNT_KEYS},
    }
    report = {
        "version": SEC_IR_LIVE_PREFLIGHT_VERSION,
        "scope": "sec_and_company_ir_only",
        "sec_included": True,
        "company_ir_included": True,
        "official_macro_included": False,
        "evidence_class": "production_path_observation" if production else "injected_offline_fixture",
        "receipt_class": (
            "production_path_observation_not_attestation"
            if production
            else "injected_offline_fixture_not_receipt"
        ),
        "evidence_profile_sha256": (
            SEC_IR_LIVE_PREFLIGHT_PRODUCTION_EVIDENCE_PROFILE_SHA256
            if production
            else SEC_IR_LIVE_PREFLIGHT_INJECTED_EVIDENCE_PROFILE_SHA256
        ),
        "source_manifest_sha256": SEC_IR_LIVE_PREFLIGHT_SOURCE_MANIFEST_SHA256 if production else "",
        "transport_identity_sha256": SEC_IR_LIVE_PREFLIGHT_TRANSPORT_IDENTITY_SHA256 if production else "",
        "ok": status == "passed",
        "status": status,
        "captured_at_ms": captured_at_ms,
        "counts": counts,
        "adapters": rows,
        "safety": _safety(
            counts["endpoint_fetch_attempt_count"],
            production=production,
            user_agent_declared=user_agent_declared,
        ),
    }
    return validate_sec_ir_live_preflight_report(report)


def _early_failure(
    *,
    production: bool,
    user_agent_declared: bool,
    code: str = "PREFLIGHT_DEPENDENCY_GUARD_FAILED",
    category: str = "internal",
) -> dict[str, Any]:
    if production and not _ISOLATED_CLI_IMPORT_GUARD_ATTESTED:
        raise SecIrLivePreflightEnvironmentError()
    rows = [
        _failed_row(
            "sec_filings",
            _SEC_ENDPOINT_COUNT,
            None,
            code=code,
            category=category,
            elapsed_ms=0,
        ),
        _failed_row(
            "company_ir",
            _IR_ENDPOINT_COUNT,
            None,
            code=code,
            category=category,
            elapsed_ms=0,
        ),
    ]
    return _assemble(
        rows,
        captured_at_ms=0,
        production=production,
        user_agent_declared=user_agent_declared,
    )


def _run_boundary(
    *,
    confirmation: Any,
    sec_fetch: Any,
    ir_fetch: Any,
    sec_user_agent: Any,
    clock: Any,
    monotonic: Any,
    production: bool,
) -> dict[str, Any]:
    if type(confirmation) is not str or confirmation != SEC_IR_LIVE_PREFLIGHT_CONFIRMATION:
        raise SecIrLivePreflightConfirmationError()
    if production and not _ISOLATED_CLI_IMPORT_GUARD_ATTESTED:
        raise SecIrLivePreflightEnvironmentError()
    user_agent_declared = _declared_user_agent(sec_user_agent)
    if not all(callable(item) for item in (sec_fetch, ir_fetch, clock, monotonic)):
        return _early_failure(production=production, user_agent_declared=user_agent_declared)
    if production and (
        sec_fetch is not _SEC_DEFAULT_FETCH_TOKEN
        or ir_fetch is not _IR_DEFAULT_FETCH_TOKEN
        or sec_user_agent is not _SEC_USER_AGENT_TOKEN
    ):
        return _early_failure(production=True, user_agent_declared=user_agent_declared)
    if production and not _dependencies_are_current():
        return _early_failure(production=True, user_agent_declared=user_agent_declared)
    try:
        observed_at = clock()
        if type(observed_at) is not datetime or observed_at.tzinfo is None:
            raise ValueError("invalid clock")
        observed_at = observed_at.astimezone(timezone.utc)
        captured_at_ms = int(observed_at.timestamp() * 1_000)
        if not 0 <= captured_at_ms <= (1 << 63) - 1:
            raise ValueError("invalid clock")
    except Exception:
        return _early_failure(production=production, user_agent_declared=user_agent_declared)
    rows = [
        _run_sec(
            fetch=sec_fetch,
            user_agent=sec_user_agent,
            observed_at=observed_at,
            captured_at_ms=captured_at_ms,
            monotonic=monotonic,
            production=production,
        ),
        _run_ir(
            fetch=ir_fetch,
            observed_at=observed_at,
            captured_at_ms=captured_at_ms,
            monotonic=monotonic,
            production=production,
        ),
    ]
    if production and not _dependencies_are_current():
        raise SecIrLivePreflightIndeterminateError(
            "production dependencies changed after endpoint activity"
        )
    return _assemble(
        rows,
        captured_at_ms=captured_at_ms,
        production=production,
        user_agent_declared=user_agent_declared,
    )


def _build_production_boundary(
    sec_fetch_token: Callable[[str, str], dict[str, Any]],
    ir_fetch_token: Callable[[str, set[str]], bytes],
    user_agent_token: str,
) -> Callable[..., dict[str, Any]]:
    def boundary(*, confirmation: Any) -> dict[str, Any]:
        return _run_boundary(
            confirmation=confirmation,
            sec_fetch=sec_fetch_token,
            ir_fetch=ir_fetch_token,
            sec_user_agent=user_agent_token,
            clock=lambda: datetime.now(timezone.utc),
            monotonic=time.monotonic,
            production=True,
        )

    boundary.__name__ = "run_sec_ir_live_preflight"
    boundary.__qualname__ = "run_sec_ir_live_preflight"
    boundary.__doc__ = "Run the fixed SEC and company-IR production adapters once."
    return boundary


run_sec_ir_live_preflight = _build_production_boundary(
    _SEC_DEFAULT_FETCH_TOKEN,
    _IR_DEFAULT_FETCH_TOKEN,
    _SEC_USER_AGENT_TOKEN,
)


def _run_sec_ir_live_preflight_injected(
    *,
    confirmation: Any,
    dependencies: SecIrLivePreflightDependencies,
) -> dict[str, Any]:
    if type(dependencies) is not SecIrLivePreflightDependencies:
        raise TypeError("exact SEC/IR preflight test dependencies are required")
    return _run_boundary(
        confirmation=confirmation,
        sec_fetch=dependencies.sec_fetch_json,
        ir_fetch=dependencies.ir_fetch_bytes,
        sec_user_agent=dependencies.sec_user_agent,
        clock=dependencies.clock or (lambda: datetime.now(timezone.utc)),
        monotonic=dependencies.monotonic or time.monotonic,
        production=False,
    )


def _valid_int(value: Any, maximum: int = _MAX_COUNT) -> bool:
    return type(value) is int and 0 <= value <= maximum


def _exact_scalar(value: Any, expected: Any) -> bool:
    return type(value) is type(expected) and value == expected


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError("invalid SEC/IR live preflight report")


def validate_sec_ir_live_preflight_report(value: Any) -> dict[str, Any]:
    """Accept only the closed report shape; rejected content is never echoed."""

    top_keys = {
        "version",
        "scope",
        "sec_included",
        "company_ir_included",
        "official_macro_included",
        "evidence_class",
        "receipt_class",
        "evidence_profile_sha256",
        "source_manifest_sha256",
        "transport_identity_sha256",
        "ok",
        "status",
        "captured_at_ms",
        "counts",
        "adapters",
        "safety",
    }
    _require(type(value) is dict and set(value) == top_keys)
    _require(value["version"] == SEC_IR_LIVE_PREFLIGHT_VERSION and type(value["version"]) is str)
    _require(value["scope"] == "sec_and_company_ir_only" and type(value["scope"]) is str)
    _require(value["sec_included"] is True and value["company_ir_included"] is True)
    _require(value["official_macro_included"] is False)
    _require(type(value["status"]) is str and value["status"] in _STATUSES)
    _require(type(value["ok"]) is bool and value["ok"] is (value["status"] == "passed"))
    _require(_valid_int(value["captured_at_ms"], (1 << 63) - 1))
    _require(
        type(value["evidence_class"]) is str
        and value["evidence_class"] in {"production_path_observation", "injected_offline_fixture"}
    )
    production = value["evidence_class"] == "production_path_observation"
    _require(
        type(value["receipt_class"]) is str
        and value["receipt_class"]
        == (
            "production_path_observation_not_attestation"
            if production
            else "injected_offline_fixture_not_receipt"
        )
    )
    _require(
        type(value["evidence_profile_sha256"]) is str
        and value["evidence_profile_sha256"]
        == (
            SEC_IR_LIVE_PREFLIGHT_PRODUCTION_EVIDENCE_PROFILE_SHA256
            if production
            else SEC_IR_LIVE_PREFLIGHT_INJECTED_EVIDENCE_PROFILE_SHA256
        )
    )
    _require(
        type(value["source_manifest_sha256"]) is str
        and value["source_manifest_sha256"]
        == (SEC_IR_LIVE_PREFLIGHT_SOURCE_MANIFEST_SHA256 if production else "")
    )
    _require(
        type(value["transport_identity_sha256"]) is str
        and value["transport_identity_sha256"]
        == (SEC_IR_LIVE_PREFLIGHT_TRANSPORT_IDENTITY_SHA256 if production else "")
    )

    adapters = value["adapters"]
    specs = (("sec_filings", _SEC_ENDPOINT_COUNT), ("company_ir", _IR_ENDPOINT_COUNT))
    _require(type(adapters) is list and len(adapters) == len(specs))
    for row, (adapter_key, endpoint_count) in zip(adapters, specs, strict=True):
        _require(type(row) is dict and set(row) == _ROW_KEYS)
        _require(row["adapter_key"] == adapter_key and type(row["adapter_key"]) is str)
        _require(type(row["status"]) is str and row["status"] in _STATUSES)
        _require(all(_valid_int(row[key]) for key in _ROW_COUNT_KEYS))
        _require(_valid_int(row["elapsed_ms"], _MAX_ELAPSED_MS))
        _require(row["endpoint_count"] == endpoint_count)
        _require(row["endpoint_fetch_attempt_count"] <= endpoint_count)
        _require(row["endpoint_success_count"] <= row["endpoint_fetch_attempt_count"])
        _require(row["transport_failure_count"] <= endpoint_count)
        if row["transport_failure_count"] == 0:
            _require(
                row["endpoint_success_count"]
                == row["endpoint_fetch_attempt_count"]
            )
        else:
            _require(
                row["endpoint_success_count"]
                < row["endpoint_fetch_attempt_count"]
                and row["endpoint_success_count"]
                + row["transport_failure_count"]
                >= row["endpoint_fetch_attempt_count"]
            )
        candidate_limit = (
            len(_SEC_SYMBOLS) * _SEC_PER_SYMBOL_LIMIT
            if adapter_key == "sec_filings"
            else len(_IR_SYMBOLS) * _IR_PER_SYMBOL_LIMIT
        )
        _require(
            row["record_count"] + row["duplicate_count"] + row["rejected_count"]
            <= candidate_limit
        )
        _require(
            type(row["error_code"]) is str
            and type(row["error_category"]) is str
            and _ERROR_CODE_CATEGORIES.get(row["error_code"]) == row["error_category"]
        )
        _require((row["status"] == "passed") is (row["error_code"] == ""))
        _require((row["status"] == "failed") is (row["record_count"] == 0))
        _require((row["status"] == "degraded") is (row["record_count"] > 0 and bool(row["error_code"])))
        complete = (
            row["endpoint_fetch_attempt_count"] == endpoint_count
            and row["endpoint_success_count"] == endpoint_count
        )
        code = row["error_code"]
        if code == "":
            _require(
                complete
                and row["record_count"] > 0
                and row["transport_failure_count"] == 0
                and row["source_error_count"] == 0
                and row["rejected_count"] == 0
            )
        elif code == "SOURCE_EMPTY":
            _require(
                complete
                and row["record_count"] == 0
                and row["transport_failure_count"] == 0
                and row["source_error_count"] == 0
                and row["rejected_count"] == 0
            )
        elif code == "ENDPOINT_COVERAGE_INCOMPLETE":
            _require(not complete and row["transport_failure_count"] == 0)
        elif code == "SOURCE_PAYLOAD_INVALID":
            _require(
                complete
                and row["transport_failure_count"] == 0
                and (row["source_error_count"] > 0 or row["rejected_count"] > 0)
            )
        elif code == "SEC_USER_AGENT_REQUIRED":
            _require(
                adapter_key == "sec_filings"
                and row["endpoint_fetch_attempt_count"] == 0
                and row["endpoint_success_count"] == 0
                and row["transport_failure_count"] == 0
            )
        elif row["error_category"] in {
            "tls",
            "dns",
            "timeout",
            "network",
            "http",
            "transport_policy",
        }:
            _require(row["transport_failure_count"] > 0 and not complete)
        if row["status"] == "passed":
            _require(row["record_count"] > 0)
            _require(row["source_error_count"] == 0 and row["rejected_count"] == 0)
            _require(row["endpoint_fetch_attempt_count"] == endpoint_count)
            _require(row["endpoint_success_count"] == endpoint_count)
            _require(row["transport_failure_count"] == 0)

    expected_counts = {
        "adapter_count": len(adapters),
        **{
            f"{status_name}_count": sum(row["status"] == status_name for row in adapters)
            for status_name in _STATUSES
        },
        **{key: sum(row[key] for row in adapters) for key in _ROW_COUNT_KEYS},
    }
    counts = value["counts"]
    _require(type(counts) is dict and set(counts) == set(_COUNT_KEYS))
    _require(all(_valid_int(item) for item in counts.values()))
    _require(counts == expected_counts)
    expected_status = "failed" if counts["failed_count"] else "degraded" if counts["degraded_count"] else "passed"
    _require(value["status"] == expected_status)
    _require(counts["endpoint_fetch_attempt_count"] <= _ENDPOINT_FETCH_ATTEMPT_LIMIT)
    if value["status"] == "passed":
        _require(value["captured_at_ms"] > 0)
        _require(counts["endpoint_fetch_attempt_count"] == _ENDPOINT_FETCH_ATTEMPT_LIMIT)

    safety = value["safety"]
    _require(type(safety) is dict and set(safety) == _SAFETY_KEYS)
    import_guard_attested = production
    _require(
        safety["transport_mode"]
        == ("guarded_default_sec_ir_https_path" if production else "injected_offline")
    )
    expected_safety = {
        "read_only": True if import_guard_attested else None,
        "one_shot": True,
        "confirmation_required": True,
        "confirmation_verified": True,
        "isolated_cli_import_guard_attested": import_guard_attested,
        "sec_user_agent_declared": safety["sec_user_agent_declared"],
        "sec_user_agent_source": (
            "explicit_process_environment_only" if production else "injected_fixture"
        ),
        "local_env_loading_disabled": True if production else None,
        "runtime_path_configuration_overridden": True if production else None,
        "production_local_env_configuration_attested": False,
        "network_requests_performed": None,
        "network_requests_accounting": "not_instrumented",
        "network_request_upper_bound": (
            counts["endpoint_fetch_attempt_count"]
            * _NETWORK_REQUESTS_PER_FETCH_UPPER_BOUND
            if production
            else None
        ),
        "network_redirect_limit_per_fetch": (
            _NETWORK_REDIRECT_LIMIT_PER_FETCH if production else None
        ),
        "network_redirect_repeat_limit_per_fetch": (
            _NETWORK_REDIRECT_REPEAT_LIMIT_PER_FETCH if production else None
        ),
        "endpoint_fetch_attempts_performed": counts["endpoint_fetch_attempt_count"],
        "endpoint_fetch_attempts_accounting": "exact",
        "endpoint_fetch_attempt_limit": _ENDPOINT_FETCH_ATTEMPT_LIMIT,
        "retries_performed": 0 if production else None,
        "initial_endpoint_allowlist_enforced": True,
        "final_endpoint_identity_attested": False,
        "redirect_policy_scope": (
            "production_default_fixed_hosts_not_exact_final_url"
            if production
            else "not_applicable_offline_fixture"
        ),
        "transport_mode": "guarded_default_sec_ir_https_path" if production else "injected_offline",
        "live_network_attested": False,
        "source_truth_verified": False,
        "production_acceptance_verdict": "NOT_EVALUATED",
        "overall_acceptance": "NOT_CLAIMED",
        "in_process_tamper_resistant": False,
        "proxy_configuration_overridden": False if production else None,
        "tls_verification_disabled": False if production else None,
        "application_file_writes_performed": 0 if import_guard_attested else None,
        "database_reads_performed": 0 if production else None,
        "database_writes_performed": 0 if production else None,
        "checkpoint_writes_performed": 0 if production else None,
        "source_inbox_writes_performed": 0 if production else None,
        "provider_calls_performed": 0 if production else None,
        "model_calls_performed": 0 if production else None,
        "futu_calls_performed": 0 if production else None,
        "formal_rounds_created": 0 if production else None,
        "execution_capability": "none" if production else "unknown",
        "live_trading_allowed": False if production else None,
        "http_listener_started": False if production else None,
    }
    _require(type(safety["sec_user_agent_declared"]) is bool)
    _require(all(_exact_scalar(safety.get(key), expected) for key, expected in expected_safety.items()))
    sec_row = adapters[0]
    if sec_row["error_code"] == "SEC_USER_AGENT_REQUIRED":
        _require(safety["sec_user_agent_declared"] is False)
        _require(sec_row["endpoint_fetch_attempt_count"] == 0)
    if (
        sec_row["status"] in {"passed", "degraded"}
        or sec_row["endpoint_fetch_attempt_count"] > 0
        or sec_row["record_count"] > 0
    ):
        _require(safety["sec_user_agent_declared"] is True)
    if safety["sec_user_agent_declared"] is False:
        _require(
            sec_row["error_code"]
            in {
                "SEC_USER_AGENT_REQUIRED",
                "PREFLIGHT_DEPENDENCY_GUARD_FAILED",
            }
        )
    return {
        "version": value["version"],
        "scope": value["scope"],
        "sec_included": value["sec_included"],
        "company_ir_included": value["company_ir_included"],
        "official_macro_included": value["official_macro_included"],
        "evidence_class": value["evidence_class"],
        "receipt_class": value["receipt_class"],
        "evidence_profile_sha256": value["evidence_profile_sha256"],
        "source_manifest_sha256": value["source_manifest_sha256"],
        "transport_identity_sha256": value["transport_identity_sha256"],
        "ok": value["ok"],
        "status": value["status"],
        "captured_at_ms": value["captured_at_ms"],
        "counts": dict(counts),
        "adapters": [dict(row) for row in adapters],
        "safety": dict(safety),
    }


__all__ = [
    "SEC_IR_LIVE_PREFLIGHT_CONFIRMATION",
    "SEC_IR_LIVE_PREFLIGHT_INJECTED_EVIDENCE_PROFILE_SHA256",
    "SEC_IR_LIVE_PREFLIGHT_MAX_OUTPUT_BYTES",
    "SEC_IR_LIVE_PREFLIGHT_PRODUCTION_EVIDENCE_PROFILE_SHA256",
    "SEC_IR_LIVE_PREFLIGHT_SOURCE_MANIFEST_SHA256",
    "SEC_IR_LIVE_PREFLIGHT_TRANSPORT_IDENTITY_SHA256",
    "SEC_IR_LIVE_PREFLIGHT_VERSION",
    "SecIrLivePreflightConfirmationError",
    "SecIrLivePreflightEnvironmentError",
    "SecIrLivePreflightIndeterminateError",
    "run_sec_ir_live_preflight",
    "validate_sec_ir_live_preflight_report",
]
