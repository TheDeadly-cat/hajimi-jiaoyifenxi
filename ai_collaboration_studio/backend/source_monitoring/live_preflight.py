"""Bounded, one-shot observation of the official macro production code path.

No database, runtime, provider, or execution service is opened. Each fixed
endpoint is fetched once through the default HTTPS path, retained only in
memory, and then passed through the production parser and adapter projection.
The report is not a tamper-resistant network attestation. It never contains
URLs, response content, or exception text.
"""

from __future__ import annotations

import errno
import hashlib
import math
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError

from ..market import official_macro as _official_macro_module
from ..market.official_macro import (
    BLS_RELEASE_CALENDAR_URL,
    BLS_SERIES_IDS,
    BLS_SERIES_URLS,
    DEFAULT_OFFICIAL_MACRO_FETCH_BYTES,
    FEDERAL_RESERVE_FOMC_CALENDAR_URL,
    FEDERAL_RESERVE_MONETARY_RSS_URL,
    OFFICIAL_MACRO_MAX_RESPONSE_BYTES,
    OFFICIAL_MACRO_TRANSPORT_IDENTITY,
    OfficialMacroSourceClient,
    TREASURY_DEBT_TO_PENNY_URL,
    TREASURY_RELEASE_CALENDAR_URL,
)
from .adapters.macro_official import (
    BLS_RELEASES_CANDIDATE_LIMIT,
    FEDERAL_RESERVE_CANDIDATE_LIMIT,
    OFFICIAL_MACRO_CALENDAR_CANDIDATE_LIMIT,
    TREASURY_RELEASES_CANDIDATE_LIMIT,
    BlsReleaseSourceAdapter,
    FederalReserveSourceAdapter,
    OfficialMacroCalendarSourceAdapter,
    TreasuryReleaseSourceAdapter,
)
from .contracts import AdapterPollResult, canonical_sha256


OFFICIAL_SOURCE_LIVE_PREFLIGHT_VERSION = "official_source_live_preflight_v1"
OFFICIAL_SOURCE_LIVE_PREFLIGHT_CONFIRMATION = (
    "RUN_OFFICIAL_SOURCE_LIVE_PREFLIGHT_ONCE"
)
OFFICIAL_SOURCE_LIVE_PREFLIGHT_MAX_OUTPUT_BYTES = 16_384
OFFICIAL_SOURCE_LIVE_PREFLIGHT_SOURCE_MANIFEST_SHA256 = canonical_sha256(
    OfficialMacroSourceClient().config_basis()
)
OFFICIAL_SOURCE_LIVE_PREFLIGHT_TRANSPORT_IDENTITY_SHA256 = hashlib.sha256(
    OFFICIAL_MACRO_TRANSPORT_IDENTITY.encode("utf-8")
).hexdigest()

_MAX_ELAPSED_MS = 1_000_000
_MAX_COUNT = 1_000
_STATUSES = ("passed", "degraded", "failed")
_PRODUCTION_TRANSPORT_GUARD_VERSION = "official_macro_direct_dependency_guard_v1"
_ERROR_CODE_CATEGORIES = {
    "": "none",
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
    "MULTIPLE_TRANSPORT_FAILURES": "network",
    "SOURCE_CAPACITY_EXCEEDED": "source_contract",
    "SOURCE_CONTRACT_FAILED": "source_contract",
    "SOURCE_PAYLOAD_INVALID": "source_payload",
    "PREFLIGHT_DEPENDENCY_GUARD_FAILED": "internal",
    "PREFLIGHT_INTERNAL_ERROR": "internal",
}


class OfficialSourceLivePreflightConfirmationError(ValueError):
    """The exact acknowledgement was missing at the public network boundary."""

    code = "PREFLIGHT_CONFIRMATION_REQUIRED"

    def __init__(self) -> None:
        super().__init__("exact live preflight confirmation is required")


class _ProductionTransportGuardError(ValueError):
    """A production transport dependency changed after module import."""


def _build_guarded_production_fetch(
    *,
    module: Any,
    fetch_token: Callable[[str], bytes],
    client_class: type,
    client_fetch_token: Callable[[str], bytes],
    opener_token: Callable[..., Any],
    request_token: type,
    urlparse_token: Callable[..., Any],
    body_reader_token: Callable[..., bytes],
    response_policies_token: Any,
    source_manifest_token: Any,
    timeout_seconds: int,
    body_deadline_seconds: int,
    user_agent: str,
    transport_identity: str,
    source_manifest_sha256: str,
) -> Callable[[str], bytes]:
    """Guard the production fetcher's direct mutable module dependencies."""

    def dependencies_are_current() -> bool:
        try:
            return (
                module.DEFAULT_OFFICIAL_MACRO_FETCH_BYTES is fetch_token
                and module._fetch_official_macro_bytes is fetch_token
                and module.OfficialMacroSourceClient is client_class
                and client_class._default_fetch_bytes is client_fetch_token
                and module.open_official_https is opener_token
                and module.Request is request_token
                and module.urlparse is urlparse_token
                and module._read_official_response_body is body_reader_token
                and module._RESPONSE_POLICIES is response_policies_token
                and module.OFFICIAL_MACRO_SOURCE_MANIFEST is source_manifest_token
                and type(module.OFFICIAL_MACRO_TIMEOUT_SECONDS) is int
                and module.OFFICIAL_MACRO_TIMEOUT_SECONDS == timeout_seconds
                and type(module.OFFICIAL_MACRO_RESPONSE_BODY_DEADLINE_SECONDS)
                is int
                and module.OFFICIAL_MACRO_RESPONSE_BODY_DEADLINE_SECONDS
                == body_deadline_seconds
                and module.OFFICIAL_MACRO_USER_AGENT == user_agent
                and module.OFFICIAL_MACRO_TRANSPORT_IDENTITY
                == transport_identity
                and canonical_sha256(client_class().config_basis())
                == source_manifest_sha256
            )
        except Exception:
            return False

    def guarded_fetch(endpoint: str) -> bytes:
        if not dependencies_are_current():
            raise _ProductionTransportGuardError(
                "official macro production transport guard changed"
            )
        raw = fetch_token(endpoint)
        if not dependencies_are_current():
            raise _ProductionTransportGuardError(
                "official macro production transport guard changed"
            )
        return raw

    guarded_fetch.__name__ = "_guarded_official_macro_production_fetch"
    return guarded_fetch


_GUARDED_PRODUCTION_FETCH = _build_guarded_production_fetch(
    module=_official_macro_module,
    fetch_token=DEFAULT_OFFICIAL_MACRO_FETCH_BYTES,
    client_class=OfficialMacroSourceClient,
    client_fetch_token=OfficialMacroSourceClient._default_fetch_bytes,
    opener_token=_official_macro_module.open_official_https,
    request_token=_official_macro_module.Request,
    urlparse_token=_official_macro_module.urlparse,
    body_reader_token=_official_macro_module._read_official_response_body,
    response_policies_token=_official_macro_module._RESPONSE_POLICIES,
    source_manifest_token=_official_macro_module.OFFICIAL_MACRO_SOURCE_MANIFEST,
    timeout_seconds=_official_macro_module.OFFICIAL_MACRO_TIMEOUT_SECONDS,
    body_deadline_seconds=(
        _official_macro_module.OFFICIAL_MACRO_RESPONSE_BODY_DEADLINE_SECONDS
    ),
    user_agent=_official_macro_module.OFFICIAL_MACRO_USER_AGENT,
    transport_identity=OFFICIAL_MACRO_TRANSPORT_IDENTITY,
    source_manifest_sha256=(
        OFFICIAL_SOURCE_LIVE_PREFLIGHT_SOURCE_MANIFEST_SHA256
    ),
)


@dataclass(frozen=True, slots=True)
class OfficialSourceLivePreflightDependencies:
    """Test-only in-memory seams; never represent production transport."""

    fetch_bytes: Callable[[str], bytes] | None = None
    clock: Callable[[], datetime] | None = None
    monotonic: Callable[[], float] | None = None


@dataclass(frozen=True, slots=True)
class _AdapterSpec:
    key: str
    endpoints: tuple[str, ...]
    candidate_limit: int
    minimum_pass_records: int
    factory: Callable[..., Any]


_SPECS = (
    _AdapterSpec(
        "federal_reserve",
        (FEDERAL_RESERVE_MONETARY_RSS_URL,),
        FEDERAL_RESERVE_CANDIDATE_LIMIT,
        1,
        FederalReserveSourceAdapter,
    ),
    _AdapterSpec(
        "bls_releases",
        tuple(BLS_SERIES_URLS[item] for item in BLS_SERIES_IDS),
        BLS_RELEASES_CANDIDATE_LIMIT,
        len(BLS_SERIES_IDS),
        BlsReleaseSourceAdapter,
    ),
    _AdapterSpec(
        "treasury_releases",
        (TREASURY_DEBT_TO_PENNY_URL,),
        TREASURY_RELEASES_CANDIDATE_LIMIT,
        1,
        TreasuryReleaseSourceAdapter,
    ),
    _AdapterSpec(
        "official_macro_calendar",
        (
            FEDERAL_RESERVE_FOMC_CALENDAR_URL,
            BLS_RELEASE_CALENDAR_URL,
            TREASURY_RELEASE_CALENDAR_URL,
        ),
        OFFICIAL_MACRO_CALENDAR_CANDIDATE_LIMIT,
        0,
        OfficialMacroCalendarSourceAdapter,
    ),
)
_ENDPOINT_FETCH_ATTEMPT_LIMIT = sum(len(item.endpoints) for item in _SPECS)
OFFICIAL_SOURCE_LIVE_PREFLIGHT_PRODUCTION_EVIDENCE_PROFILE_SHA256 = canonical_sha256({
    "version": OFFICIAL_SOURCE_LIVE_PREFLIGHT_VERSION,
    "evidence_class": "production_path_observation",
    "scope": "official_macro_only",
    "trust_model": "trusted_unmodified_local_process_v1",
    "live_network_attested": False,
    "in_process_tamper_resistant": False,
    "transport_dependency_guard": _PRODUCTION_TRANSPORT_GUARD_VERSION,
    "source_manifest_sha256": (
        OFFICIAL_SOURCE_LIVE_PREFLIGHT_SOURCE_MANIFEST_SHA256
    ),
    "transport_identity_sha256": (
        OFFICIAL_SOURCE_LIVE_PREFLIGHT_TRANSPORT_IDENTITY_SHA256
    ),
    "adapters": [
        {
            "adapter_key": item.key,
            "candidate_limit": item.candidate_limit,
            "minimum_pass_records": item.minimum_pass_records,
            "endpoint_count": len(item.endpoints),
        }
        for item in _SPECS
    ],
})
OFFICIAL_SOURCE_LIVE_PREFLIGHT_INJECTED_EVIDENCE_PROFILE_SHA256 = canonical_sha256({
    "version": OFFICIAL_SOURCE_LIVE_PREFLIGHT_VERSION,
    "evidence_class": "injected_offline_fixture",
    "scope": "official_macro_only",
    "trust_model": "explicit_test_dependencies_v1",
    "live_network_attested": False,
    "in_process_tamper_resistant": False,
})

_ROW_COUNT_KEYS = (
    "endpoint_count",
    "endpoint_success_count",
    "endpoint_fetch_attempt_count",
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
    "network_requests_performed",
    "network_requests_accounting",
    "endpoint_fetch_attempts_performed",
    "endpoint_fetch_attempts_accounting",
    "endpoint_fetch_attempt_limit",
    "retries_performed",
    "endpoint_allowlist_enforced",
    "transport_mode",
    "live_network_attested",
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


def _count(value: Any, maximum: int = _MAX_COUNT) -> int:
    return value if type(value) is int and 0 <= value <= maximum else 0


def _monotonic_value(read: Callable[[], float]) -> float:
    try:
        value = read()
    except Exception:
        return 0.0
    return float(value) if type(value) in {int, float} and math.isfinite(value) else 0.0


def _elapsed(started: float, read: Callable[[], float]) -> int:
    finished = _monotonic_value(read)
    return min(max(0, int(round((finished - started) * 1_000))), _MAX_ELAPSED_MS)


def _classify_transport(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, _ProductionTransportGuardError):
        return "PREFLIGHT_DEPENDENCY_GUARD_FAILED", "internal"
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
    connection_errors = (
        ConnectionAbortedError,
        ConnectionRefusedError,
        ConnectionResetError,
        BrokenPipeError,
    )
    if isinstance(exc, connection_errors):
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
        return (
            ("NETWORK_CONNECTION_FAILED", "network")
            if getattr(exc, "errno", None) in connection_numbers
            else ("NETWORK_TRANSPORT_FAILED", "network")
        )
    if isinstance(exc, ValueError):
        return "TRANSPORT_POLICY_REJECTED", "transport_policy"
    return "PREFLIGHT_INTERNAL_ERROR", "internal"


def _failure_code(failures: list[tuple[str, str]]) -> tuple[str, str]:
    unique = tuple(dict.fromkeys(failures))
    if len(unique) == 1:
        return unique[0]
    return (
        ("MULTIPLE_TRANSPORT_FAILURES", "network")
        if unique
        else ("PREFLIGHT_INTERNAL_ERROR", "internal")
    )


def _poll_failure(result: AdapterPollResult) -> tuple[str, str]:
    codes = {item.code for item in result.source_errors}
    if any("CAPACITY_EXCEEDED" in code for code in codes):
        return "SOURCE_CAPACITY_EXCEEDED", "source_contract"
    contract_tokens = ("PACKET", "PROVENANCE", "CONFIG", "CONTRACT")
    if any(token in code for code in codes for token in contract_tokens):
        return "SOURCE_CONTRACT_FAILED", "source_contract"
    return "SOURCE_PAYLOAD_INVALID", "source_payload"


def _failed_row(
    spec: _AdapterSpec,
    *,
    successes: int,
    attempts: int,
    errors: int,
    code: str,
    category: str,
    elapsed_ms: int,
    force_failed: bool = False,
) -> dict[str, Any]:
    return {
        "adapter_key": spec.key,
        "status": "failed" if force_failed or not successes else "degraded",
        "endpoint_count": len(spec.endpoints),
        "endpoint_success_count": _count(successes),
        "endpoint_fetch_attempt_count": _count(attempts),
        "record_count": 0,
        "duplicate_count": 0,
        "rejected_count": 0,
        "source_error_count": _count(errors),
        "error_code": code,
        "error_category": category,
        "elapsed_ms": elapsed_ms,
    }


def _run_adapter(
    spec: _AdapterSpec,
    *,
    fetch_bytes: Callable[[str], bytes],
    observed_at: datetime,
    captured_at_ms: int,
    monotonic: Callable[[], float],
) -> dict[str, Any]:
    started = _monotonic_value(monotonic)
    payloads: dict[str, bytes] = {}
    failures: list[tuple[str, str]] = []
    for endpoint in spec.endpoints:
        try:
            raw = fetch_bytes(endpoint)
            if (
                type(raw) is not bytes
                or len(raw) > OFFICIAL_MACRO_MAX_RESPONSE_BYTES[endpoint]
            ):
                failures.append(("TRANSPORT_RESPONSE_INVALID", "transport_policy"))
            else:
                payloads[endpoint] = raw
        except Exception as exc:
            failures.append(_classify_transport(exc))
            if isinstance(exc, HTTPError):
                try:
                    exc.close()
                except Exception:
                    pass
    attempts = len(spec.endpoints)
    if failures:
        code, category = _failure_code(failures)
        return _failed_row(
            spec,
            successes=len(payloads),
            attempts=attempts,
            errors=len(failures),
            code=code,
            category=category,
            elapsed_ms=_elapsed(started, monotonic),
        )

    parser_calls: list[str] = []

    def memory_fetch(endpoint: str) -> bytes:
        parser_calls.append(endpoint)
        index = len(parser_calls) - 1
        if (
            index >= len(spec.endpoints)
            or endpoint != spec.endpoints[index]
            or endpoint not in payloads
        ):
            raise RuntimeError("sealed in-memory endpoint order changed")
        return payloads[endpoint]

    try:
        adapter = spec.factory(
            client=OfficialMacroSourceClient(
                fetch_bytes=memory_fetch,
                clock=lambda: observed_at,
            )
        )
        result = adapter.poll(
            {},
            observed_at_ms=captured_at_ms,
            max_items=adapter.max_candidates_per_poll,
        )
        if (
            type(result) is not AdapterPollResult
            or result.adapter_key != spec.key
            or adapter.max_candidates_per_poll != spec.candidate_limit
            or tuple(parser_calls) != spec.endpoints
        ):
            raise RuntimeError("sealed adapter result changed")
    except Exception:
        return _failed_row(
            spec,
            successes=len(payloads),
            attempts=attempts,
            errors=1,
            code="PREFLIGHT_INTERNAL_ERROR",
            category="internal",
            elapsed_ms=_elapsed(started, monotonic),
            force_failed=True,
        )

    source_error_count = len(result.source_errors)
    if result.source_errors:
        code, category = _poll_failure(result)
        status = "degraded" if result.observed_items else "failed"
    elif result.rejected_count:
        code, category, status = (
            "SOURCE_PAYLOAD_INVALID",
            "source_payload",
            "degraded",
        )
    else:
        code, category, status = "", "none", "passed"
    return {
        "adapter_key": spec.key,
        "status": status,
        "endpoint_count": len(spec.endpoints),
        "endpoint_success_count": len(payloads),
        "endpoint_fetch_attempt_count": attempts,
        "record_count": len(result.observed_items),
        "duplicate_count": _count(result.duplicate_count),
        "rejected_count": _count(result.rejected_count),
        "source_error_count": _count(source_error_count),
        "error_code": code,
        "error_category": category,
        "elapsed_ms": _elapsed(started, monotonic),
    }


def _safety(attempts: int, *, production: bool) -> dict[str, Any]:
    return {
        "read_only": True if production else None,
        "one_shot": True,
        "confirmation_required": True,
        "confirmation_verified": True,
        # Redirects remain policy-checked but are not counted at opener level.
        "network_requests_performed": None,
        "network_requests_accounting": "not_instrumented",
        "endpoint_fetch_attempts_performed": attempts,
        "endpoint_fetch_attempts_accounting": "exact",
        "endpoint_fetch_attempt_limit": _ENDPOINT_FETCH_ATTEMPT_LIMIT,
        "retries_performed": 0 if production else None,
        "endpoint_allowlist_enforced": True if production else None,
        "transport_mode": (
            "default_official_https_path" if production else "injected_offline"
        ),
        "live_network_attested": False,
        "in_process_tamper_resistant": False,
        "proxy_configuration_overridden": False if production else None,
        "tls_verification_disabled": False if production else None,
        "application_file_writes_performed": 0 if production else None,
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
    adapters: list[dict[str, Any]],
    *,
    captured_at_ms: int,
    production: bool,
) -> dict[str, Any]:
    status_counts = {
        status: sum(row["status"] == status for row in adapters)
        for status in _STATUSES
    }
    status = (
        "failed"
        if status_counts["failed"]
        else "degraded"
        if status_counts["degraded"]
        else "passed"
    )
    counts = {
        "adapter_count": len(adapters),
        **{f"{status}_count": status_counts[status] for status in _STATUSES},
        **{key: sum(row[key] for row in adapters) for key in _ROW_COUNT_KEYS},
    }
    report = {
        "version": OFFICIAL_SOURCE_LIVE_PREFLIGHT_VERSION,
        "scope": "official_macro_only",
        "sec_included": False,
        "company_ir_included": False,
        "evidence_class": (
            "production_path_observation"
            if production
            else "injected_offline_fixture"
        ),
        "evidence_profile_sha256": (
            OFFICIAL_SOURCE_LIVE_PREFLIGHT_PRODUCTION_EVIDENCE_PROFILE_SHA256
            if production
            else OFFICIAL_SOURCE_LIVE_PREFLIGHT_INJECTED_EVIDENCE_PROFILE_SHA256
        ),
        "ok": status == "passed",
        "status": status,
        "captured_at_ms": captured_at_ms,
        "source_manifest_sha256": (
            OFFICIAL_SOURCE_LIVE_PREFLIGHT_SOURCE_MANIFEST_SHA256
            if production
            else ""
        ),
        "transport_identity_sha256": (
            OFFICIAL_SOURCE_LIVE_PREFLIGHT_TRANSPORT_IDENTITY_SHA256
            if production
            else ""
        ),
        "counts": counts,
        "adapters": adapters,
        "safety": _safety(
            counts["endpoint_fetch_attempt_count"],
            production=production,
        ),
    }
    return validate_live_preflight_report(report)


def _early_failure(*, production: bool) -> dict[str, Any]:
    rows = [
        _failed_row(
            spec,
            successes=0,
            attempts=0,
            errors=1,
            code="PREFLIGHT_INTERNAL_ERROR",
            category="internal",
            elapsed_ms=0,
        )
        for spec in _SPECS
    ]
    return _assemble(rows, captured_at_ms=0, production=production)


def _run_boundary(
    *,
    confirmation: Any,
    fetch_bytes: Any,
    clock: Any,
    monotonic: Any,
    production: bool,
) -> dict[str, Any]:
    if (
        type(confirmation) is not str
        or confirmation != OFFICIAL_SOURCE_LIVE_PREFLIGHT_CONFIRMATION
    ):
        raise OfficialSourceLivePreflightConfirmationError()
    if not all(callable(item) for item in (fetch_bytes, clock, monotonic)):
        return _early_failure(production=production)
    try:
        observed_at = clock()
        if type(observed_at) is not datetime or observed_at.tzinfo is None:
            raise ValueError("invalid clock")
        observed_at = observed_at.astimezone(timezone.utc)
        captured_at_ms = int(observed_at.timestamp() * 1_000)
        if not 0 <= captured_at_ms <= (1 << 63) - 1:
            raise ValueError("invalid clock")
    except Exception:
        return _early_failure(production=production)
    rows = [
        _run_adapter(
            spec,
            fetch_bytes=fetch_bytes,
            observed_at=observed_at,
            captured_at_ms=captured_at_ms,
            monotonic=monotonic,
        )
        for spec in _SPECS
    ]
    return _assemble(rows, captured_at_ms=captured_at_ms, production=production)


def _build_production_boundary(
    guarded_fetch: Callable[[str], bytes],
) -> Callable[..., dict[str, Any]]:
    """Capture the guarded default fetch reference outside mutable globals."""

    def boundary(*, confirmation: Any) -> dict[str, Any]:
        return _run_boundary(
            confirmation=confirmation,
            fetch_bytes=guarded_fetch,
            clock=lambda: datetime.now(timezone.utc),
            monotonic=time.monotonic,
            production=True,
        )

    boundary.__name__ = "run_official_source_live_preflight"
    boundary.__qualname__ = "run_official_source_live_preflight"
    boundary.__doc__ = (
        "Run four fixed production adapters once after exact confirmation."
    )
    return boundary


run_official_source_live_preflight = _build_production_boundary(
    _GUARDED_PRODUCTION_FETCH
)


def _run_official_source_live_preflight_injected(
    *,
    confirmation: Any,
    dependencies: OfficialSourceLivePreflightDependencies,
) -> dict[str, Any]:
    """Exercise the contract with explicitly non-production test dependencies."""

    if type(dependencies) is not OfficialSourceLivePreflightDependencies:
        raise TypeError("exact preflight test dependencies are required")
    return _run_boundary(
        confirmation=confirmation,
        fetch_bytes=dependencies.fetch_bytes,
        clock=(
            (lambda: datetime.now(timezone.utc))
            if dependencies.clock is None
            else dependencies.clock
        ),
        monotonic=(
            time.monotonic
            if dependencies.monotonic is None
            else dependencies.monotonic
        ),
        production=False,
    )


def _valid_int(value: Any, maximum: int = _MAX_COUNT) -> bool:
    return type(value) is int and 0 <= value <= maximum


def _exact_scalar(value: Any, expected: Any) -> bool:
    """Compare JSON scalars without accepting bool/int/float aliases."""

    return type(value) is type(expected) and value == expected


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError("invalid live preflight report")


def validate_live_preflight_report(value: Any) -> dict[str, Any]:
    """Accept only the closed report shape; rejected values are never echoed."""

    top_keys = {
        "version",
        "scope",
        "sec_included",
        "company_ir_included",
        "evidence_class",
        "evidence_profile_sha256",
        "ok",
        "status",
        "captured_at_ms",
        "source_manifest_sha256",
        "transport_identity_sha256",
        "counts",
        "adapters",
        "safety",
    }
    _require(type(value) is dict and set(value) == top_keys)
    _require(
        type(value["version"]) is str
        and value["version"] == OFFICIAL_SOURCE_LIVE_PREFLIGHT_VERSION
    )
    _require(type(value["scope"]) is str and value["scope"] == "official_macro_only")
    _require(value["sec_included"] is False)
    _require(value["company_ir_included"] is False)
    _require(
        type(value["ok"]) is bool
        and type(value["status"]) is str
        and value["status"] in _STATUSES
    )
    _require(value["ok"] is (value["status"] == "passed"))
    _require(_valid_int(value["captured_at_ms"], (1 << 63) - 1))
    _require(
        type(value["evidence_class"]) is str
        and value["evidence_class"]
        in {"production_path_observation", "injected_offline_fixture"}
    )
    production_evidence = (
        value["evidence_class"] == "production_path_observation"
    )
    _require(
        type(value["evidence_profile_sha256"]) is str
        and value["evidence_profile_sha256"]
        == (
            OFFICIAL_SOURCE_LIVE_PREFLIGHT_PRODUCTION_EVIDENCE_PROFILE_SHA256
            if production_evidence
            else OFFICIAL_SOURCE_LIVE_PREFLIGHT_INJECTED_EVIDENCE_PROFILE_SHA256
        )
    )
    _require(
        type(value["source_manifest_sha256"]) is str
        and value["source_manifest_sha256"]
        == (
            OFFICIAL_SOURCE_LIVE_PREFLIGHT_SOURCE_MANIFEST_SHA256
            if production_evidence
            else ""
        )
    )
    _require(
        type(value["transport_identity_sha256"]) is str
        and value["transport_identity_sha256"]
        == (
            OFFICIAL_SOURCE_LIVE_PREFLIGHT_TRANSPORT_IDENTITY_SHA256
            if production_evidence
            else ""
        )
    )

    adapters = value["adapters"]
    _require(type(adapters) is list and len(adapters) == len(_SPECS))
    for spec, row in zip(_SPECS, adapters, strict=True):
        _require(type(row) is dict and set(row) == _ROW_KEYS)
        _require(
            type(row["adapter_key"]) is str
            and row["adapter_key"] == spec.key
            and type(row["status"]) is str
            and row["status"] in _STATUSES
        )
        _require(all(_valid_int(row[key]) for key in _ROW_COUNT_KEYS))
        _require(_valid_int(row["elapsed_ms"], _MAX_ELAPSED_MS))
        _require(row["endpoint_count"] == len(spec.endpoints))
        _require(
            row["record_count"]
            + row["duplicate_count"]
            + row["rejected_count"]
            <= spec.candidate_limit
        )
        _require(row["endpoint_fetch_attempt_count"] <= row["endpoint_count"])
        _require(
            row["endpoint_success_count"]
            <= row["endpoint_fetch_attempt_count"]
        )
        _require(
            type(row["error_code"]) is str
            and type(row["error_category"]) is str
            and _ERROR_CODE_CATEGORIES.get(row["error_code"])
            == row["error_category"]
        )
        _require((row["status"] == "passed") is (row["error_code"] == ""))
        _require(
            (row["status"] == "passed")
            is (
                row["source_error_count"] == 0
                and row["rejected_count"] == 0
            )
        )
        if row["status"] == "passed":
            _require(row["record_count"] >= spec.minimum_pass_records)
            _require(row["endpoint_success_count"] == row["endpoint_count"])
            _require(
                row["endpoint_fetch_attempt_count"] == row["endpoint_count"]
            )
            _require(row["rejected_count"] == 0)

    expected_counts = {
        "adapter_count": len(adapters),
        **{
            f"{status}_count": sum(row["status"] == status for row in adapters)
            for status in _STATUSES
        },
        **{key: sum(row[key] for row in adapters) for key in _ROW_COUNT_KEYS},
    }
    counts = value["counts"]
    _require(type(counts) is dict and set(counts) == set(_COUNT_KEYS))
    _require(all(_valid_int(item) for item in counts.values()))
    _require(counts == expected_counts)
    expected_status = (
        "failed"
        if counts["failed_count"]
        else "degraded"
        if counts["degraded_count"]
        else "passed"
    )
    _require(value["status"] == expected_status)
    if value["status"] == "passed":
        _require(value["captured_at_ms"] > 0)
    attempts = counts["endpoint_fetch_attempt_count"]
    _require(attempts in {0, _ENDPOINT_FETCH_ATTEMPT_LIMIT})
    _require(
        attempts != 0
        or (
            value["captured_at_ms"] == 0
            and all(
                row["error_code"] == "PREFLIGHT_INTERNAL_ERROR"
                for row in adapters
            )
        )
    )
    _require(attempts == 0 or value["captured_at_ms"] > 0)
    _require(
        attempts == 0
        or all(
            row["endpoint_fetch_attempt_count"] == row["endpoint_count"]
            for row in adapters
        )
    )

    safety = value["safety"]
    _require(type(safety) is dict and set(safety) == _SAFETY_KEYS)
    production = safety.get("transport_mode") == "default_official_https_path"
    _require(production is production_evidence)
    expected_safety = {
        "read_only": True if production else None,
        "one_shot": True,
        "confirmation_required": True,
        "confirmation_verified": True,
        "network_requests_performed": None,
        "network_requests_accounting": "not_instrumented",
        "endpoint_fetch_attempts_performed": attempts,
        "endpoint_fetch_attempts_accounting": "exact",
        "endpoint_fetch_attempt_limit": _ENDPOINT_FETCH_ATTEMPT_LIMIT,
        "retries_performed": 0 if production else None,
        "endpoint_allowlist_enforced": True if production else None,
        "transport_mode": (
            "default_official_https_path" if production else "injected_offline"
        ),
        "live_network_attested": False,
        "in_process_tamper_resistant": False,
        "proxy_configuration_overridden": False if production else None,
        "tls_verification_disabled": False if production else None,
        "application_file_writes_performed": 0 if production else None,
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
    _require(
        safety.get("transport_mode")
        in {"default_official_https_path", "injected_offline"}
    )
    _require(
        all(
            _exact_scalar(safety.get(key), expected)
            for key, expected in expected_safety.items()
        )
    )
    return {
        "version": value["version"],
        "scope": value["scope"],
        "sec_included": value["sec_included"],
        "company_ir_included": value["company_ir_included"],
        "evidence_class": value["evidence_class"],
        "evidence_profile_sha256": value["evidence_profile_sha256"],
        "ok": value["ok"],
        "status": value["status"],
        "captured_at_ms": value["captured_at_ms"],
        "source_manifest_sha256": value["source_manifest_sha256"],
        "transport_identity_sha256": value["transport_identity_sha256"],
        "counts": dict(counts),
        "adapters": [dict(row) for row in adapters],
        "safety": dict(safety),
    }


__all__ = [
    "OFFICIAL_SOURCE_LIVE_PREFLIGHT_CONFIRMATION",
    "OFFICIAL_SOURCE_LIVE_PREFLIGHT_MAX_OUTPUT_BYTES",
    "OFFICIAL_SOURCE_LIVE_PREFLIGHT_INJECTED_EVIDENCE_PROFILE_SHA256",
    "OFFICIAL_SOURCE_LIVE_PREFLIGHT_PRODUCTION_EVIDENCE_PROFILE_SHA256",
    "OFFICIAL_SOURCE_LIVE_PREFLIGHT_SOURCE_MANIFEST_SHA256",
    "OFFICIAL_SOURCE_LIVE_PREFLIGHT_TRANSPORT_IDENTITY_SHA256",
    "OFFICIAL_SOURCE_LIVE_PREFLIGHT_VERSION",
    "OfficialSourceLivePreflightConfirmationError",
    "run_official_source_live_preflight",
    "validate_live_preflight_report",
]
