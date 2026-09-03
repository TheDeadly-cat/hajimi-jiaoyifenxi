"""Bounded, one-shot observation of the read-only Futu quote path.

The public production entry is ``scripts/run_futu_live_preflight.py``.  It
boots this module in an isolated watchdog child after clearing application
credentials and disabling local-env loading.  This module never opens SQLite,
an HTTP listener, a Provider, an account context, or a trade context.

The guarded SDK facade deliberately exposes only ``OpenQuoteContext``,
``get_market_snapshot``, optional ``get_market_state``, and ``close``.  Logical
calls at that facade are counted exactly.  Futu SDK transport activity below
those calls is not instrumented and is never reported as an exact network
request count.
"""

from __future__ import annotations

import copy
import hmac
import importlib
import importlib.metadata
import json
import os
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


_IMPORT_GUARD_ENV = "AI_STUDIO_FUTU_PREFLIGHT_IMPORT_GUARD"
_IMPORT_GUARD_VALUE = "futu-live-preflight-isolated-import-v1"
_WATCHDOG_GUARD_ENV = "AI_STUDIO_FUTU_PREFLIGHT_WATCHDOG_GUARD"
_WATCHDOG_GUARD_VALUE = "futu-live-preflight-watchdog-v1"
_SDK_PROFILE_DIRNAME = "futu-sdk-profile"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DATABASE_SENTINEL = _PROJECT_ROOT / ".futu-live-preflight-database-must-not-open.sqlite3"

_PREIMPORT_FORBIDDEN_EXACT = frozenset({
    "backend.config",
    "backend.store",
    "backend.provider_gateway",
    "backend.source_inbox_service",
    "backend.source_monitoring.runtime",
    "backend.source_monitoring.state_repository",
    "backend.source_monitoring.supervisor",
    "futu",
})
_PREIMPORT_FORBIDDEN_PREFIXES = ("backend.market.", "futu.")
_PREIMPORT_DEPENDENCIES_CLEAN = not any(
    name in _PREIMPORT_FORBIDDEN_EXACT
    or any(name.startswith(prefix) for prefix in _PREIMPORT_FORBIDDEN_PREFIXES)
    for name in tuple(sys.modules)
)

from .. import config as _config_module
from ..market import futu_readonly as _futu_module
from ..market.futu_readonly import (
    FutuUsMarketAdapter,
    STORAGE_SYMBOLS,
    validate_storage_quote_snapshot,
)
from .contracts import canonical_sha256


FUTU_LIVE_PREFLIGHT_VERSION = "futu_live_preflight_v1"
FUTU_LIVE_PREFLIGHT_CONFIRMATION = "RUN_FUTU_LIVE_PREFLIGHT_ONCE"
FUTU_LIVE_PREFLIGHT_MAX_OUTPUT_BYTES = 16_384
FUTU_LIVE_PREFLIGHT_HOST = "127.0.0.1"
FUTU_LIVE_PREFLIGHT_PORT = 11111
FUTU_LIVE_PREFLIGHT_SYMBOLS = ("US.MU", "US.SNDK", "US.WDC", "US.STX")
FUTU_LIVE_PREFLIGHT_SDK_DISTRIBUTION = "futu-api"
FUTU_LIVE_PREFLIGHT_SDK_VERSION = "10.10.7008"


class FutuLivePreflightError(RuntimeError):
    """Base class for closed preflight failures."""


class FutuLivePreflightConfirmationError(FutuLivePreflightError):
    pass


class FutuLivePreflightDependencyError(FutuLivePreflightError):
    pass


def _sdk_profile_path_checked_at_import() -> bool:
    appdata = os.getenv("APPDATA")
    local_appdata = os.getenv("LOCALAPPDATA")
    if (
        type(appdata) is not str
        or not appdata
        or type(local_appdata) is not str
        or local_appdata != appdata
    ):
        return False
    try:
        raw_profile = Path(appdata)
        if raw_profile.is_symlink():
            return False
        profile = raw_profile.resolve(strict=True)
        cwd = Path.cwd().resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return bool(
        profile.is_dir()
        and profile.name == _SDK_PROFILE_DIRNAME
        and profile.parent == cwd
    )


_SDK_PROFILE_PATH_CHECKED_AT_IMPORT = _sdk_profile_path_checked_at_import()
_ISOLATED_CLI_IMPORT_GUARD_ATTESTED = bool(
    os.getenv(_IMPORT_GUARD_ENV) == _IMPORT_GUARD_VALUE
    and os.getenv(_WATCHDOG_GUARD_ENV) == _WATCHDOG_GUARD_VALUE
    and os.getenv("AI_STUDIO_SKIP_LOCAL_ENV") == "1"
    and sys.flags.isolated == 1
    and sys.dont_write_bytecode is True
    and _PREIMPORT_DEPENDENCIES_CLEAN
    and _SDK_PROFILE_PATH_CHECKED_AT_IMPORT
    and _config_module.RUNTIME_DIR == _PROJECT_ROOT
    and _config_module.DATABASE_PATH == _DATABASE_SENTINEL
    and _PROJECT_ROOT.is_dir()
    and not os.path.lexists(_DATABASE_SENTINEL)
)

_FUTU_MODULE_TOKEN = _futu_module
_ADAPTER_CLASS_TOKEN = FutuUsMarketAdapter
_QUOTE_BATCH_TOKEN = FutuUsMarketAdapter.quote_batch
_FETCH_QUOTE_BATCH_TOKEN = FutuUsMarketAdapter._fetch_quote_batch
_SDK_TOKEN = FutuUsMarketAdapter._sdk
_NORMALIZE_SYMBOLS_TOKEN = FutuUsMarketAdapter._normalize_symbols
_MARKET_STATES_TOKEN = FutuUsMarketAdapter._market_states_for_symbols
_VALIDATOR_TOKEN = validate_storage_quote_snapshot
_STORAGE_SYMBOLS_TOKEN = STORAGE_SYMBOLS
_SOCKET_MODULE_TOKEN = socket
_CREATE_CONNECTION_TOKEN = socket.create_connection
_IMPORT_MODULE_TOKEN = importlib.import_module
_METADATA_VERSION_TOKEN = importlib.metadata.version


def _native_exact_symbols(value: Any) -> bool:
    return bool(
        type(value) is tuple
        and value == FUTU_LIVE_PREFLIGHT_SYMBOLS
        and all(type(symbol) is str for symbol in value)
    )


def _dependency_seal_intact() -> bool:
    return bool(
        _futu_module is _FUTU_MODULE_TOKEN
        and FutuUsMarketAdapter is _ADAPTER_CLASS_TOKEN
        and FutuUsMarketAdapter.quote_batch is _QUOTE_BATCH_TOKEN
        and FutuUsMarketAdapter._fetch_quote_batch is _FETCH_QUOTE_BATCH_TOKEN
        and FutuUsMarketAdapter._sdk is _SDK_TOKEN
        and FutuUsMarketAdapter._normalize_symbols is _NORMALIZE_SYMBOLS_TOKEN
        and FutuUsMarketAdapter._market_states_for_symbols is _MARKET_STATES_TOKEN
        and validate_storage_quote_snapshot is _VALIDATOR_TOKEN
        and STORAGE_SYMBOLS is _STORAGE_SYMBOLS_TOKEN
        and _native_exact_symbols(STORAGE_SYMBOLS)
        and socket is _SOCKET_MODULE_TOKEN
        and socket.create_connection is _CREATE_CONNECTION_TOKEN
        and importlib.import_module is _IMPORT_MODULE_TOKEN
        and importlib.metadata.version is _METADATA_VERSION_TOKEN
    )


def _blank_calls() -> dict[str, int]:
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


class _CallLedger:
    def __init__(self) -> None:
        self.calls = _blank_calls()

    def increment(self, name: str) -> None:
        if name not in self.calls or type(self.calls[name]) is not int:
            raise FutuLivePreflightDependencyError("invalid call-ledger key")
        self.calls[name] += 1
        if self.calls[name] > 1:
            raise FutuLivePreflightDependencyError("logical call limit exceeded")

    def snapshot(self) -> dict[str, int]:
        return dict(self.calls)


def _validate_snapshot_symbols(value: Any) -> list[str]:
    if type(value) is not list or len(value) != len(FUTU_LIVE_PREFLIGHT_SYMBOLS):
        raise FutuLivePreflightDependencyError("snapshot symbol boundary changed")
    if any(type(symbol) is not str for symbol in value):
        raise FutuLivePreflightDependencyError("snapshot symbols must be native strings")
    if tuple(value) != FUTU_LIVE_PREFLIGHT_SYMBOLS:
        raise FutuLivePreflightDependencyError("snapshot symbols changed")
    return value


def _validate_state_symbols(value: Any) -> list[str]:
    if type(value) is not list or not value:
        raise FutuLivePreflightDependencyError("market-state symbols are invalid")
    if any(type(symbol) is not str for symbol in value):
        raise FutuLivePreflightDependencyError("market-state symbols must be native strings")
    if len(value) != len(set(value)):
        raise FutuLivePreflightDependencyError("market-state symbols contain duplicates")
    expected_order = [symbol for symbol in FUTU_LIVE_PREFLIGHT_SYMBOLS if symbol in value]
    if value != expected_order:
        raise FutuLivePreflightDependencyError("market-state symbols changed")
    return value


class _GuardedQuoteContext:
    __slots__ = ("_inner", "_ledger")

    def __init__(self, inner: Any, ledger: _CallLedger) -> None:
        self._inner = inner
        self._ledger = ledger

    def get_market_snapshot(self, symbols: Any) -> Any:
        clean = _validate_snapshot_symbols(symbols)
        self._ledger.increment("snapshot_call_attempt_count")
        result = self._inner.get_market_snapshot(clean)
        self._ledger.increment("snapshot_call_return_count")
        return result

    def get_market_state(self, symbols: Any) -> Any:
        clean = _validate_state_symbols(symbols)
        self._ledger.increment("market_state_call_attempt_count")
        result = self._inner.get_market_state(clean)
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
            raise FutuLivePreflightDependencyError("Futu quote SDK boundary is invalid")
        self.RET_OK = ret_ok
        self._constructor = constructor
        self._ledger = ledger

    def OpenQuoteContext(self, *, host: Any, port: Any) -> _GuardedQuoteContext:
        if type(host) is not str or host != FUTU_LIVE_PREFLIGHT_HOST:
            raise FutuLivePreflightDependencyError("Futu host changed")
        if type(port) is not int or port != FUTU_LIVE_PREFLIGHT_PORT:
            raise FutuLivePreflightDependencyError("Futu port changed")
        self._ledger.increment("quote_context_open_attempt_count")
        inner = self._constructor(host=host, port=port)
        self._ledger.increment("quote_context_open_success_count")
        return _GuardedQuoteContext(inner, self._ledger)


class _GuardedSocketProbe:
    __slots__ = ("_create_connection", "_ledger")

    def __init__(
        self,
        create_connection: Callable[..., Any],
        ledger: _CallLedger,
    ) -> None:
        if not callable(create_connection):
            raise FutuLivePreflightDependencyError("socket dependency is invalid")
        self._create_connection = create_connection
        self._ledger = ledger

    def __call__(self, host: Any, port: Any) -> bool:
        if type(host) is not str or host != FUTU_LIVE_PREFLIGHT_HOST:
            raise FutuLivePreflightDependencyError("socket host changed")
        if type(port) is not int or port != FUTU_LIVE_PREFLIGHT_PORT:
            raise FutuLivePreflightDependencyError("socket port changed")
        self._ledger.increment("socket_probe_attempt_count")
        try:
            connection = self._create_connection(
                (FUTU_LIVE_PREFLIGHT_HOST, FUTU_LIVE_PREFLIGHT_PORT),
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


@dataclass(frozen=True)
class FutuLivePreflightDependencies:
    sdk_module: Any
    sdk_version: str
    create_connection: Callable[..., Any]
    clock: Callable[[], datetime]
    monotonic: Callable[[], float]
    snapshot_id_factory: Callable[[], str]
    evidence_class: str = "injected_offline_fixture"
    sdk_load_error_code: str = ""


def _source_manifest() -> dict[str, Any]:
    return {
        "version": "futu_live_preflight_source_manifest_v1",
        "host_policy": "fixed_ipv4_loopback_literal_v1",
        "port": FUTU_LIVE_PREFLIGHT_PORT,
        "symbols": list(FUTU_LIVE_PREFLIGHT_SYMBOLS),
        "quote_batch_calls": 1,
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
    }


FUTU_LIVE_PREFLIGHT_SOURCE_MANIFEST_SHA256 = canonical_sha256(_source_manifest())
FUTU_LIVE_PREFLIGHT_WORKER_EVIDENCE_PROFILE_SHA256 = canonical_sha256({
    "version": FUTU_LIVE_PREFLIGHT_VERSION,
    "evidence_class": "watchdog_worker_observation",
    "transport": "guarded_loopback_futu_quote_path_v1",
    "sdk_distribution": FUTU_LIVE_PREFLIGHT_SDK_DISTRIBUTION,
    "sdk_version": FUTU_LIVE_PREFLIGHT_SDK_VERSION,
    "source_manifest_sha256": FUTU_LIVE_PREFLIGHT_SOURCE_MANIFEST_SHA256,
    "watchdog_parent_promoted": False,
    "live_network_attested": False,
    "in_process_tamper_resistant": False,
    "dependency_file_integrity_attested": False,
})
FUTU_LIVE_PREFLIGHT_PRODUCTION_EVIDENCE_PROFILE_SHA256 = canonical_sha256({
    "version": FUTU_LIVE_PREFLIGHT_VERSION,
    "evidence_class": "production_path_observation",
    "transport": "guarded_loopback_futu_quote_path_v1",
    "sdk_distribution": FUTU_LIVE_PREFLIGHT_SDK_DISTRIBUTION,
    "sdk_version": FUTU_LIVE_PREFLIGHT_SDK_VERSION,
    "source_manifest_sha256": FUTU_LIVE_PREFLIGHT_SOURCE_MANIFEST_SHA256,
    "watchdog_parent_promoted": True,
    "live_network_attested": False,
    "in_process_tamper_resistant": False,
    "dependency_file_integrity_attested": False,
})


def _fixed_error_code(snapshot: Any) -> str:
    if not isinstance(snapshot, dict):
        return "FUTU_SNAPSHOT_INVALID"
    source_errors = snapshot.get("source_errors")
    if not isinstance(source_errors, list) or not source_errors:
        return "FUTU_SNAPSHOT_NOT_READY"
    allowed = {
        "FUTU_OPEND_OFFLINE",
        "FUTU_SNAPSHOT_FAILED",
        "FUTU_CONNECTION_ERROR",
        "MISSING_SYMBOLS",
    }
    for entry in source_errors:
        if isinstance(entry, dict) and entry.get("code") in allowed:
            return str(entry["code"])
    return "FUTU_SOURCE_ERROR"


def _base_safety(
    *,
    production: bool,
    confirmation_verified: bool,
    watchdog_parent_promoted: bool = False,
    import_guard_attested: bool | None = None,
    watchdog_guard_attested: bool | None = None,
    sdk_profile_path_checked_at_import: bool | None = None,
) -> dict[str, Any]:
    effective_import_guard = (
        _ISOLATED_CLI_IMPORT_GUARD_ATTESTED
        if import_guard_attested is None and production
        else False
        if import_guard_attested is None
        else import_guard_attested
    )
    effective_watchdog_guard = (
        os.getenv(_WATCHDOG_GUARD_ENV) == _WATCHDOG_GUARD_VALUE
        if watchdog_guard_attested is None and production
        else False
        if watchdog_guard_attested is None
        else watchdog_guard_attested
    )
    effective_sdk_profile_path_check = (
        _SDK_PROFILE_PATH_CHECKED_AT_IMPORT
        if sdk_profile_path_checked_at_import is None and production
        else False
        if sdk_profile_path_checked_at_import is None
        else sdk_profile_path_checked_at_import
    )
    return {
        "read_only": True,
        "one_shot": True,
        "confirmation_required": True,
        "confirmation_verified": confirmation_verified,
        "isolated_cli_import_guard_attested": effective_import_guard,
        "sdk_profile_path_checked_at_import": effective_sdk_profile_path_check,
        "sdk_profile_policy": (
            "parent_temp_appdata_v1" if production else "injected_offline"
        ),
        "watchdog_required": True,
        "watchdog_guard_attested": effective_watchdog_guard,
        "watchdog_enforced": watchdog_parent_promoted,
        "watchdog_parent_promoted": watchdog_parent_promoted,
        "worker_process_tree_termination_attested": False,
        "loopback_only": True,
        "host_policy": "fixed_ipv4_loopback_literal_v1",
        "transport_mode": (
            "guarded_loopback_futu_quote_path_v1"
            if production
            else "injected_offline"
        ),
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


def _seal_report(payload: dict[str, Any]) -> dict[str, Any]:
    sealed = copy.deepcopy(payload)
    sealed["receipt_sha256"] = canonical_sha256(sealed)
    return sealed


def _receipt_class(evidence_class: str) -> str:
    if evidence_class == "production_path_observation":
        return "production_path_observation_not_attestation"
    if evidence_class == "watchdog_worker_observation":
        return "watchdog_worker_observation_not_attestation"
    return "injected_offline_fixture_not_receipt"


def _error_report(
    code: str,
    *,
    production: bool,
    status: str = "failed",
    confirmation_verified: bool = True,
    sdk_version: str = "",
    calls: dict[str, int] | None = None,
) -> dict[str, Any]:
    return _seal_report({
        "version": FUTU_LIVE_PREFLIGHT_VERSION,
        "scope": "futu_storage_quotes_only",
        "ok": False,
        "status": status,
        "error_code": code,
        "evidence_class": (
            "watchdog_worker_observation" if production else "injected_offline_fixture"
        ),
        "receipt_class": _receipt_class(
            "watchdog_worker_observation" if production else "injected_offline_fixture"
        ),
        "captured_at_ms": None,
        "symbols": list(FUTU_LIVE_PREFLIGHT_SYMBOLS),
        "sdk_distribution": FUTU_LIVE_PREFLIGHT_SDK_DISTRIBUTION,
        "sdk_version": sdk_version,
        "source_manifest_sha256": FUTU_LIVE_PREFLIGHT_SOURCE_MANIFEST_SHA256,
        "evidence_profile_sha256": (
            FUTU_LIVE_PREFLIGHT_WORKER_EVIDENCE_PROFILE_SHA256 if production else ""
        ),
        "calls": _blank_calls() if calls is None else dict(calls),
        "result": {
            "snapshot_state": "unavailable",
            "coverage_complete": False,
            "quality_ready": False,
            "ready_symbol_count": 0,
            "source_error_count": 1,
        },
        "safety": _base_safety(
            production=production,
            confirmation_verified=confirmation_verified,
        ),
    })


def _validate_dependencies(dependencies: FutuLivePreflightDependencies) -> None:
    if type(dependencies) is not FutuLivePreflightDependencies:
        raise FutuLivePreflightDependencyError("dependencies must be exact")
    if type(dependencies.sdk_version) is not str:
        raise FutuLivePreflightDependencyError("SDK version must be a native string")
    if (
        type(dependencies.sdk_load_error_code) is not str
        or dependencies.sdk_load_error_code not in {"", "FUTU_SDK_IMPORT_FAILED"}
    ):
        raise FutuLivePreflightDependencyError("SDK load error is invalid")
    if type(dependencies.evidence_class) is not str or dependencies.evidence_class not in {
        "production_path_observation",
        "watchdog_worker_observation",
        "injected_offline_fixture",
    }:
        raise FutuLivePreflightDependencyError("evidence class is invalid")
    for value in (
        dependencies.create_connection,
        dependencies.clock,
        dependencies.monotonic,
        dependencies.snapshot_id_factory,
    ):
        if not callable(value):
            raise FutuLivePreflightDependencyError("dependency callable is invalid")


def _run(
    *,
    confirmation: Any,
    dependencies: FutuLivePreflightDependencies,
    production: bool,
) -> dict[str, Any]:
    if type(confirmation) is not str or confirmation != FUTU_LIVE_PREFLIGHT_CONFIRMATION:
        raise FutuLivePreflightConfirmationError("exact confirmation is required")
    _validate_dependencies(dependencies)
    if not _native_exact_symbols(FUTU_LIVE_PREFLIGHT_SYMBOLS):
        return _error_report(
            "FUTU_PREFLIGHT_SYMBOL_POLICY_INVALID",
            production=production,
            status="indeterminate",
        )
    if production and (
        not _ISOLATED_CLI_IMPORT_GUARD_ATTESTED or not _dependency_seal_intact()
    ):
        return _error_report(
            "FUTU_PREFLIGHT_DEPENDENCY_DRIFT",
            production=True,
            status="indeterminate",
        )
    if dependencies.sdk_load_error_code:
        return _error_report(
            dependencies.sdk_load_error_code,
            production=production,
            sdk_version=dependencies.sdk_version,
        )
    if dependencies.sdk_version != FUTU_LIVE_PREFLIGHT_SDK_VERSION:
        code = (
            "FUTU_SDK_UNAVAILABLE"
            if not dependencies.sdk_version
            else "FUTU_SDK_VERSION_MISMATCH"
        )
        return _error_report(
            code,
            production=production,
            sdk_version=dependencies.sdk_version,
        )

    ledger = _CallLedger()
    try:
        guarded_sdk = _GuardedSdk(dependencies.sdk_module, ledger)
        guarded_probe = _GuardedSocketProbe(dependencies.create_connection, ledger)
        adapter = FutuUsMarketAdapter(
            host=FUTU_LIVE_PREFLIGHT_HOST,
            port=FUTU_LIVE_PREFLIGHT_PORT,
            cache_ttl_seconds=1.0,
            sdk_module=guarded_sdk,
            socket_probe=guarded_probe,
            clock=dependencies.clock,
            monotonic_clock=dependencies.monotonic,
            snapshot_id_factory=dependencies.snapshot_id_factory,
        )
        if (
            type(adapter) is not FutuUsMarketAdapter
            or type(adapter.host) is not str
            or adapter.host != FUTU_LIVE_PREFLIGHT_HOST
            or type(adapter.port) is not int
            or adapter.port != FUTU_LIVE_PREFLIGHT_PORT
            or adapter._sdk_module is not guarded_sdk
            or adapter._socket_probe is not guarded_probe
        ):
            raise FutuLivePreflightDependencyError("adapter boundary changed")
        if production and not _dependency_seal_intact():
            raise FutuLivePreflightDependencyError("dependencies changed before I/O")
        ledger.increment("quote_batch_attempt_count")
        snapshot = adapter.quote_batch(FUTU_LIVE_PREFLIGHT_SYMBOLS, force=True)
        ledger.increment("quote_batch_return_count")
    except FutuLivePreflightDependencyError:
        return _error_report(
            "FUTU_PREFLIGHT_DEPENDENCY_DRIFT",
            production=production,
            status="indeterminate",
            sdk_version=dependencies.sdk_version,
            calls=ledger.snapshot(),
        )
    except BaseException:
        return _error_report(
            "FUTU_PREFLIGHT_INTERNAL_ERROR",
            production=production,
            status="indeterminate",
            sdk_version=dependencies.sdk_version,
            calls=ledger.snapshot(),
        )

    calls = ledger.snapshot()
    if production and not _dependency_seal_intact():
        return _error_report(
            "FUTU_PREFLIGHT_POST_IO_DEPENDENCY_DRIFT",
            production=True,
            status="indeterminate",
            sdk_version=dependencies.sdk_version,
            calls=calls,
        )
    if calls["quote_context_open_success_count"] and (
        calls["close_attempt_count"] != 1 or calls["close_success_count"] != 1
    ):
        return _error_report(
            "FUTU_CONTEXT_CLOSE_UNVERIFIED",
            production=production,
            status="indeterminate",
            sdk_version=dependencies.sdk_version,
            calls=calls,
        )

    validation = validate_storage_quote_snapshot(snapshot)
    captured_at_ms = snapshot.get("captured_at_ms") if isinstance(snapshot, dict) else None
    if type(captured_at_ms) is not int or captured_at_ms < 0:
        captured_at_ms = None
    rows = snapshot.get("rows") if isinstance(snapshot, dict) else None
    source_errors = snapshot.get("source_errors") if isinstance(snapshot, dict) else None
    ready_symbol_count = len(validation.get("ready_symbols") or [])
    quality_ready = bool(validation.get("snapshot_quality_ready") is True)
    coverage_complete = bool(
        isinstance(rows, list)
        and len(rows) == len(FUTU_LIVE_PREFLIGHT_SYMBOLS)
        and validation.get("market_symbols") == sorted(FUTU_LIVE_PREFLIGHT_SYMBOLS)
    )
    ok = bool(validation.get("ready") is True)
    payload = {
        "version": FUTU_LIVE_PREFLIGHT_VERSION,
        "scope": "futu_storage_quotes_only",
        "ok": ok,
        "status": "passed" if ok else "failed",
        "error_code": "" if ok else _fixed_error_code(snapshot),
        "evidence_class": dependencies.evidence_class,
        "receipt_class": _receipt_class(dependencies.evidence_class),
        "captured_at_ms": captured_at_ms,
        "symbols": list(FUTU_LIVE_PREFLIGHT_SYMBOLS),
        "sdk_distribution": FUTU_LIVE_PREFLIGHT_SDK_DISTRIBUTION,
        "sdk_version": dependencies.sdk_version,
        "source_manifest_sha256": FUTU_LIVE_PREFLIGHT_SOURCE_MANIFEST_SHA256,
        "evidence_profile_sha256": (
            FUTU_LIVE_PREFLIGHT_WORKER_EVIDENCE_PROFILE_SHA256 if production else ""
        ),
        "calls": calls,
        "result": {
            "snapshot_state": (
                str(snapshot.get("state") or "unavailable")
                if isinstance(snapshot, dict)
                else "unavailable"
            ),
            "coverage_complete": coverage_complete,
            "quality_ready": quality_ready,
            "ready_symbol_count": ready_symbol_count,
            "source_error_count": len(source_errors) if isinstance(source_errors, list) else 1,
        },
        "safety": _base_safety(
            production=production,
            confirmation_verified=True,
        ),
    }
    return _seal_report(payload)


def _production_dependencies() -> FutuLivePreflightDependencies:
    sdk_load_error_code = ""
    try:
        version = importlib.metadata.version(FUTU_LIVE_PREFLIGHT_SDK_DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        version = ""
        sdk_module = None
    else:
        sdk_module = None
        if version == FUTU_LIVE_PREFLIGHT_SDK_VERSION:
            try:
                sdk_module = importlib.import_module("futu")
            except (Exception, SystemExit):
                sdk_module = None
                sdk_load_error_code = "FUTU_SDK_IMPORT_FAILED"
    return FutuLivePreflightDependencies(
        sdk_module=sdk_module,
        sdk_version=version,
        create_connection=socket.create_connection,
        clock=lambda: datetime.now(timezone.utc),
        monotonic=time.monotonic,
        snapshot_id_factory=lambda: "futu_live_preflight_snapshot_v1",
        evidence_class="watchdog_worker_observation",
        sdk_load_error_code=sdk_load_error_code,
    )


def run_futu_live_preflight(*, confirmation: Any) -> dict[str, Any]:
    """Run the production observation inside the guarded watchdog child."""

    if type(confirmation) is not str or confirmation != FUTU_LIVE_PREFLIGHT_CONFIRMATION:
        raise FutuLivePreflightConfirmationError("exact confirmation is required")
    if not _ISOLATED_CLI_IMPORT_GUARD_ATTESTED or not _dependency_seal_intact():
        return _error_report(
            "FUTU_PREFLIGHT_IMPORT_GUARD_INVALID",
            production=True,
            status="indeterminate",
        )
    dependencies = _production_dependencies()
    return _run(
        confirmation=confirmation,
        dependencies=dependencies,
        production=True,
    )


def _run_futu_live_preflight_injected(
    *,
    confirmation: Any,
    dependencies: FutuLivePreflightDependencies,
) -> dict[str, Any]:
    """Offline-only seam; its reports can never validate as production."""

    return _run(
        confirmation=confirmation,
        dependencies=dependencies,
        production=False,
    )


def validate_futu_live_preflight_report(value: Any) -> dict[str, Any]:
    """Validate the closed, bounded report schema and receipt digest."""

    if type(value) is not dict:
        raise FutuLivePreflightError("report must be an exact object")
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
        raise FutuLivePreflightError("report fields are invalid")
    if value["version"] != FUTU_LIVE_PREFLIGHT_VERSION:
        raise FutuLivePreflightError("report version is invalid")
    if value["scope"] != "futu_storage_quotes_only":
        raise FutuLivePreflightError("report scope is invalid")
    if type(value["ok"]) is not bool or type(value["status"]) is not str or value["status"] not in {
        "passed",
        "failed",
        "indeterminate",
    }:
        raise FutuLivePreflightError("report status is invalid")
    if value["ok"] is not (value["status"] == "passed"):
        raise FutuLivePreflightError("report success is inconsistent")
    if (
        type(value["error_code"]) is not str
        or len(value["error_code"]) > 96
        or value["error_code"].splitlines() not in ([value["error_code"]], [])
        or (value["ok"] and value["error_code"])
        or (not value["ok"] and not value["error_code"])
    ):
        raise FutuLivePreflightError("report error code is invalid")
    if value["receipt_class"] != _receipt_class(value["evidence_class"]):
        raise FutuLivePreflightError("receipt class is invalid")
    if value["sdk_distribution"] != FUTU_LIVE_PREFLIGHT_SDK_DISTRIBUTION:
        raise FutuLivePreflightError("SDK distribution is invalid")
    if (
        type(value["sdk_version"]) is not str
        or len(value["sdk_version"]) > 32
        or value["sdk_version"].splitlines() not in ([value["sdk_version"]], [])
    ):
        raise FutuLivePreflightError("SDK version is invalid")
    if value["captured_at_ms"] is not None and (
        type(value["captured_at_ms"]) is not int or value["captured_at_ms"] < 0
    ):
        raise FutuLivePreflightError("capture time is invalid")
    if not _native_exact_symbols(tuple(value["symbols"]) if type(value["symbols"]) is list else None):
        raise FutuLivePreflightError("report symbols are invalid")
    if value["source_manifest_sha256"] != FUTU_LIVE_PREFLIGHT_SOURCE_MANIFEST_SHA256:
        raise FutuLivePreflightError("source manifest changed")
    if type(value["calls"]) is not dict or set(value["calls"]) != set(_blank_calls()):
        raise FutuLivePreflightError("call ledger is invalid")
    for name, count in value["calls"].items():
        if type(name) is not str or type(count) is not int or not 0 <= count <= 1:
            raise FutuLivePreflightError("call count is invalid")
    calls = value["calls"]
    for attempted, completed in (
        ("quote_batch_attempt_count", "quote_batch_return_count"),
        ("socket_probe_attempt_count", "socket_probe_success_count"),
        ("quote_context_open_attempt_count", "quote_context_open_success_count"),
        ("snapshot_call_attempt_count", "snapshot_call_return_count"),
        ("market_state_call_attempt_count", "market_state_call_return_count"),
        ("close_attempt_count", "close_success_count"),
    ):
        if calls[completed] > calls[attempted]:
            raise FutuLivePreflightError("call completion exceeds attempts")
    if (
        calls["socket_probe_attempt_count"] > calls["quote_batch_attempt_count"]
        or calls["quote_context_open_attempt_count"] > calls["socket_probe_success_count"]
        or calls["snapshot_call_attempt_count"] > calls["quote_context_open_success_count"]
        or calls["market_state_call_attempt_count"] > calls["snapshot_call_return_count"]
        or calls["close_attempt_count"] > calls["quote_context_open_success_count"]
    ):
        raise FutuLivePreflightError("logical call order is invalid")
    if type(value["result"]) is not dict or set(value["result"]) != {
        "snapshot_state",
        "coverage_complete",
        "quality_ready",
        "ready_symbol_count",
        "source_error_count",
    }:
        raise FutuLivePreflightError("result is invalid")
    result = value["result"]
    if (
        type(result["snapshot_state"]) is not str
        or result["snapshot_state"] not in {"ready", "degraded", "offline", "unavailable"}
        or type(result["coverage_complete"]) is not bool
        or type(result["quality_ready"]) is not bool
        or type(result["ready_symbol_count"]) is not int
        or not 0 <= result["ready_symbol_count"] <= len(FUTU_LIVE_PREFLIGHT_SYMBOLS)
        or type(result["source_error_count"]) is not int
        or not 0 <= result["source_error_count"] <= 16
    ):
        raise FutuLivePreflightError("result values are invalid")
    expected_digest = value.get("receipt_sha256")
    if (
        type(expected_digest) is not str
        or len(expected_digest) != 64
        or any(character not in "0123456789abcdef" for character in expected_digest)
    ):
        raise FutuLivePreflightError("receipt digest is invalid")
    unsealed = copy.deepcopy(value)
    unsealed.pop("receipt_sha256")
    if not hmac.compare_digest(expected_digest, canonical_sha256(unsealed)):
        raise FutuLivePreflightError("receipt digest mismatch")
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    if len(encoded) >= FUTU_LIVE_PREFLIGHT_MAX_OUTPUT_BYTES:
        raise FutuLivePreflightError("report exceeds output bound")
    if value["evidence_class"] == "production_path_observation":
        expected_safety = _base_safety(
            production=True,
            confirmation_verified=True,
            watchdog_parent_promoted=True,
            import_guard_attested=True,
            watchdog_guard_attested=True,
            sdk_profile_path_checked_at_import=True,
        )
        if (
            value["evidence_profile_sha256"]
            != FUTU_LIVE_PREFLIGHT_PRODUCTION_EVIDENCE_PROFILE_SHA256
            or value["safety"] != expected_safety
        ):
            raise FutuLivePreflightError("production evidence boundary is invalid")
    elif value["evidence_class"] == "watchdog_worker_observation":
        if (
            value["evidence_profile_sha256"]
            != FUTU_LIVE_PREFLIGHT_WORKER_EVIDENCE_PROFILE_SHA256
            or value["safety"]
            != _base_safety(
                production=True,
                confirmation_verified=True,
                import_guard_attested=True,
                watchdog_guard_attested=True,
                sdk_profile_path_checked_at_import=True,
            )
        ):
            raise FutuLivePreflightError("worker evidence boundary is invalid")
    elif value["evidence_class"] == "injected_offline_fixture":
        if (
            value["evidence_profile_sha256"] != ""
            or value["safety"]
            != _base_safety(production=False, confirmation_verified=True)
        ):
            raise FutuLivePreflightError("injected evidence boundary is invalid")
    else:
        raise FutuLivePreflightError("evidence class is invalid")
    if value["ok"] and (
        value["sdk_version"] != FUTU_LIVE_PREFLIGHT_SDK_VERSION
        or value["captured_at_ms"] is None
        or result["snapshot_state"] != "ready"
        or result["coverage_complete"] is not True
        or result["quality_ready"] is not True
        or result["ready_symbol_count"] != len(FUTU_LIVE_PREFLIGHT_SYMBOLS)
        or result["source_error_count"] != 0
        or calls["quote_batch_attempt_count"] != 1
        or calls["quote_batch_return_count"] != 1
        or calls["socket_probe_attempt_count"] != 1
        or calls["socket_probe_success_count"] != 1
        or calls["quote_context_open_attempt_count"] != 1
        or calls["quote_context_open_success_count"] != 1
        or calls["snapshot_call_attempt_count"] != 1
        or calls["snapshot_call_return_count"] != 1
        or calls["close_attempt_count"] != 1
        or calls["close_success_count"] != 1
    ):
        raise FutuLivePreflightError("successful report is incomplete")
    return copy.deepcopy(value)


__all__ = [
    "FUTU_LIVE_PREFLIGHT_CONFIRMATION",
    "FUTU_LIVE_PREFLIGHT_HOST",
    "FUTU_LIVE_PREFLIGHT_MAX_OUTPUT_BYTES",
    "FUTU_LIVE_PREFLIGHT_PORT",
    "FUTU_LIVE_PREFLIGHT_PRODUCTION_EVIDENCE_PROFILE_SHA256",
    "FUTU_LIVE_PREFLIGHT_SDK_VERSION",
    "FUTU_LIVE_PREFLIGHT_SOURCE_MANIFEST_SHA256",
    "FUTU_LIVE_PREFLIGHT_WORKER_EVIDENCE_PROFILE_SHA256",
    "FUTU_LIVE_PREFLIGHT_SYMBOLS",
    "FUTU_LIVE_PREFLIGHT_VERSION",
    "FutuLivePreflightConfirmationError",
    "FutuLivePreflightDependencies",
    "_run_futu_live_preflight_injected",
    "run_futu_live_preflight",
    "validate_futu_live_preflight_report",
]
