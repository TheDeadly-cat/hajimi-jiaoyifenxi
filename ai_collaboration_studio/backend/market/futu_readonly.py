from __future__ import annotations

import copy
import importlib.util
import math
import socket
import threading
import time
import uuid
from datetime import date, datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ..config import FUTU_CACHE_TTL_SECONDS, FUTU_HOST, FUTU_PORT


STORAGE_SYMBOLS = ("US.MU", "US.SNDK", "US.WDC", "US.STX")
MAX_HISTORY_PAGES = 50
MAX_VALIDATED_HISTORY_ROWS = 500
MAX_HISTORY_VALIDATION_ISSUES = 64
LIVE_QUOTE_MAX_AGE_SECONDS = 20 * 60
# Friday's regular-session close to Tuesday's regular-session open is about
# 89.5 hours on a US three-day weekend. 96 hours covers that normal long-
# weekend gap while still failing closed on an older, potentially abandoned
# snapshot. This is not a calendar inference: promotion also requires Futu to
# report an explicit non-trading market state for that exact symbol.
CLOSED_SESSION_MAX_AGE_SECONDS = 96 * 60 * 60
CLOSED_SESSION_MARKET_STATES = frozenset({
    "AFTER_HOURS_END",
    "CLOSED",
    "WAITING_OPEN",
})
FINANCIAL_STATEMENT_TYPES = {
    "income": 1,
    "balance_sheet": 2,
    "cash_flow": 3,
    "main_index": 4,
}
REVENUE_BREAKDOWN_TYPES = {
    1: "product",
    2: "industry",
    4: "region",
    8: "business",
}
US_EASTERN = ZoneInfo("America/New_York")
_AUTO_SDK = object()


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _records(data: Any) -> list[dict[str, Any]]:
    if hasattr(data, "to_dict"):
        result = data.to_dict("records")
        return [dict(row) for row in result]
    if isinstance(data, list):
        return [dict(row) for row in data if isinstance(row, dict)]
    if isinstance(data, tuple):
        return [dict(row) for row in data if isinstance(row, dict)]
    return []


def _utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _security_status_allows_research(
    security_status: Any,
    suspended: Any,
) -> bool:
    """Fail closed on an explicit abnormal security state.

    Older frozen fixtures did not carry these Futu fields, so a missing status
    remains compatible. Futu may expose the normal enum either as ``NORMAL``
    or as its stringified ``SecurityStatus.NORMAL`` representation.
    """
    if suspended in (True, 1, "true", "TRUE", "1"):
        return False
    status = str(security_status or "").strip().upper()
    if not status:
        return True
    return status == "NORMAL" or status.endswith(".NORMAL")


def quote_research_ready(
    row: dict[str, Any],
    *,
    actual_age_seconds: float | None = None,
) -> bool:
    """Validate the explicit v2 quote-freshness contract.

    ``quality=ready`` alone is intentionally insufficient. This keeps legacy
    or hand-built rows without provenance from bypassing round admission.
    """
    if str(row.get("quality") or "") != "ready":
        return False
    if not _security_status_allows_research(
        row.get("security_status"),
        row.get("suspended"),
    ):
        return False
    age_seconds = row.get("age_seconds")
    if isinstance(age_seconds, bool) or not isinstance(age_seconds, int):
        return False
    effective_age: float = float(age_seconds)
    if actual_age_seconds is not None:
        if (
            not math.isfinite(actual_age_seconds)
            or abs(actual_age_seconds - age_seconds) > 1.0
        ):
            return False
        effective_age = actual_age_seconds
    if row.get("quote_is_live") is True:
        return bool(
            row.get("freshness_basis") == "live_20m_window"
            and 0 <= effective_age <= LIVE_QUOTE_MAX_AGE_SECONDS
        )
    if row.get("quote_is_live") is False:
        return bool(
            row.get("freshness_basis") == "closed_session_latest_snapshot"
            and str(row.get("market_state") or "").strip().upper()
            in CLOSED_SESSION_MARKET_STATES
            and LIVE_QUOTE_MAX_AGE_SECONDS < effective_age <= CLOSED_SESSION_MAX_AGE_SECONDS
        )
    return False


def _parse_snapshot_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_quote_row_time(row: dict[str, Any]) -> datetime | None:
    # ``updated_at`` is the adapter's canonical UTC field. If it is present but
    # malformed, never fall back to the display-oriented ``market_time`` value.
    if "updated_at" in row:
        return _parse_snapshot_time(row.get("updated_at"))
    text = str(row.get("market_time") or "").strip()
    if not text:
        return None
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=US_EASTERN)
    return parsed.astimezone(timezone.utc)


def validate_storage_quote_snapshot(snapshot: Any) -> dict[str, Any]:
    """Validate one canonical, read-only Futu snapshot for the storage universe.

    This pure admission gate performs no network or account action. It rejects
    partial, legacy, stale, future-dated, suspended, and execution-capable
    payloads so all downstream research features consume the same contract.
    """
    payload = snapshot if isinstance(snapshot, dict) else {}
    rows = [row for row in payload.get("rows") or [] if isinstance(row, dict)]
    expected_symbols = frozenset(STORAGE_SYMBOLS)
    market_symbols = {
        str(row.get("symbol") or "").strip()
        for row in rows
        if str(row.get("symbol") or "").strip()
    }
    symbol_counts: dict[str, int] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
        if symbol:
            symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1

    captured_time = _parse_snapshot_time(payload.get("captured_at"))
    invalid_market_time_symbols: set[str] = set()
    future_market_time_symbols: set[str] = set()
    invalid_freshness_symbols: set[str] = set()
    ready_symbols: set[str] = set()

    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
        if symbol not in expected_symbols:
            continue
        row_time = _parse_quote_row_time(row)
        if captured_time is None or row_time is None:
            invalid_market_time_symbols.add(symbol)
            continue
        if row_time > captured_time:
            future_market_time_symbols.add(symbol)
            continue

        actual_age_seconds = (captured_time - row_time).total_seconds()
        if not quote_research_ready(
            row,
            actual_age_seconds=actual_age_seconds,
        ):
            invalid_freshness_symbols.add(symbol)
            continue

        last = row.get("last")
        price_ready = bool(
            not isinstance(last, bool)
            and isinstance(last, (int, float))
            and math.isfinite(float(last))
            and float(last) > 0
        )
        if price_ready and bool(str(row.get("market_time") or "").strip()):
            ready_symbols.add(symbol)

    safety_fields_explicit = (
        "execution_capability" in payload
        and "live_trading_allowed" in payload
    )
    safe_snapshot = bool(payload) and bool(
        safety_fields_explicit
        and payload.get("execution_capability") == "none"
        and payload.get("live_trading_allowed") is False
    )
    snapshot_quality_ready = bool(payload) and all((
        payload.get("source") == "futu_opend",
        payload.get("ok") is True,
        payload.get("state") == "ready",
        bool(str(payload.get("snapshot_id") or "").strip()),
        captured_time is not None,
        isinstance(payload.get("source_errors"), list),
        not payload.get("source_errors"),
        isinstance(payload.get("missing_symbols"), list),
        not payload.get("missing_symbols"),
        not invalid_market_time_symbols,
        not future_market_time_symbols,
        not invalid_freshness_symbols,
        len(rows) == len(STORAGE_SYMBOLS),
        market_symbols == expected_symbols,
        expected_symbols.issubset(ready_symbols),
    ))
    return {
        "ready": snapshot_quality_ready and safe_snapshot,
        "snapshot_quality_ready": snapshot_quality_ready,
        "safe_snapshot": safe_snapshot,
        "safety_fields_explicit": safety_fields_explicit,
        "market_symbols": sorted(market_symbols),
        "ready_symbols": sorted(ready_symbols),
        "invalid_market_time_symbols": sorted(invalid_market_time_symbols),
        "future_market_time_symbols": sorted(future_market_time_symbols),
        "invalid_freshness_symbols": sorted(invalid_freshness_symbols),
        "duplicate_symbols": sorted(
            symbol for symbol, count in symbol_counts.items() if count > 1
        ),
    }


def validate_readonly_daily_history(
    history: Any,
    *,
    expected_symbol: str | None = None,
    expected_start: str | None = None,
    expected_end: str | None = None,
) -> dict[str, Any]:
    """Validate one complete Futu read-only 1d/QFQ history envelope.

    Consumers must not infer provenance or safety from a non-empty ``rows``
    collection.  This pure gate verifies the adapter envelope, exact symbol,
    completed-session metadata, chronological row integrity, and the explicit
    no-execution boundary before any return or risk calculation is allowed.
    """

    payload = history if isinstance(history, dict) else {}
    symbol = str(payload.get("symbol") or "").strip().upper()
    clean_expected = str(expected_symbol or symbol).strip().upper()
    safety_fields_explicit = (
        "execution_capability" in payload
        and "live_trading_allowed" in payload
    )
    safe_history = bool(payload) and bool(
        safety_fields_explicit
        and payload.get("execution_capability") == "none"
        and payload.get("live_trading_allowed") is False
    )
    issues: list[str] = []
    issues_truncated = False

    def reject(condition: bool, code: str) -> None:
        nonlocal issues_truncated
        if condition and code not in issues:
            if len(issues) < MAX_HISTORY_VALIDATION_ISSUES:
                issues.append(code)
            else:
                issues_truncated = True

    reject(not payload, "HISTORY_NOT_OBJECT")
    reject(clean_expected not in STORAGE_SYMBOLS, "EXPECTED_SYMBOL_NOT_ALLOWED")
    reject(symbol != clean_expected, "HISTORY_SYMBOL_MISMATCH")
    reject(payload.get("source") != "futu_opend", "HISTORY_SOURCE_INVALID")
    reject(payload.get("interval") != "1d", "HISTORY_INTERVAL_INVALID")
    reject(payload.get("price_adjustment") != "QFQ", "HISTORY_ADJUSTMENT_INVALID")
    reject(payload.get("execution_capability") != "none", "HISTORY_EXECUTION_BOUNDARY_INVALID")
    reject(payload.get("live_trading_allowed") is not False, "HISTORY_LIVE_TRADING_BOUNDARY_INVALID")
    reject(payload.get("ok") is not True, "HISTORY_NOT_READY")
    reject(not isinstance(payload.get("source_errors"), list), "HISTORY_SOURCE_ERRORS_INVALID")
    reject(bool(payload.get("source_errors")), "HISTORY_SOURCE_ERRORS_PRESENT")

    requested_bounds: dict[str, date | None] = {}
    for field, expected in (("start", expected_start), ("end", expected_end)):
        if expected is None:
            requested_bounds[field] = None
            continue
        try:
            expected_date = date.fromisoformat(str(expected))
        except ValueError:
            expected_date = None
        requested_bounds[field] = expected_date
        reject(expected_date is None, f"HISTORY_EXPECTED_{field.upper()}_INVALID")
        reject(
            str(payload.get(field) or "") != str(expected),
            f"HISTORY_{field.upper()}_MISMATCH",
        )

    captured_at = _parse_snapshot_time(payload.get("captured_at"))
    reject(captured_at is None, "HISTORY_CAPTURED_AT_INVALID")
    try:
        as_of_date = date.fromisoformat(str(payload.get("as_of_date") or ""))
    except ValueError:
        as_of_date = None
    reject(as_of_date is None, "HISTORY_AS_OF_DATE_INVALID")
    if captured_at is not None and as_of_date is not None:
        reject(
            captured_at.astimezone(US_EASTERN).date() != as_of_date,
            "HISTORY_AS_OF_DATE_MISMATCH",
        )

    metadata_dates: dict[str, date | None] = {}
    for field in ("last_completed_session", "actual_start", "actual_end"):
        try:
            parsed_date = date.fromisoformat(str(payload.get(field) or ""))
        except ValueError:
            parsed_date = None
        metadata_dates[field] = parsed_date
        reject(parsed_date is None, f"HISTORY_{field.upper()}_INVALID")

    rows = payload.get("rows")
    reject(not isinstance(rows, list) or not rows, "HISTORY_ROWS_EMPTY")
    reject(
        isinstance(rows, list) and len(rows) > MAX_VALIDATED_HISTORY_ROWS,
        "HISTORY_ROWS_LIMIT_EXCEEDED",
    )
    row_dates: list[date] = []
    previous_time: datetime | None = None
    seen_times: set[str] = set()
    if isinstance(rows, list):
        for index, raw_row in enumerate(rows[:MAX_VALIDATED_HISTORY_ROWS]):
            prefix = f"HISTORY_ROW_{index}"
            if not isinstance(raw_row, dict):
                reject(True, f"{prefix}_INVALID")
                continue
            reject(
                str(raw_row.get("symbol") or "").strip().upper() != clean_expected,
                f"{prefix}_SYMBOL_MISMATCH",
            )
            market_text = str(raw_row.get("market_time") or "").strip()
            canonical_text = str(raw_row.get("time") or "").strip()
            market_time = FutuUsMarketAdapter._parse_market_time(market_text)
            canonical_time = _parse_snapshot_time(canonical_text)
            reject(market_time is None, f"{prefix}_MARKET_TIME_INVALID")
            reject(canonical_time is None, f"{prefix}_TIME_INVALID")
            if market_time is not None:
                market_utc = market_time.astimezone(timezone.utc)
                row_date = market_time.astimezone(US_EASTERN).date()
                row_dates.append(row_date)
                reject(
                    canonical_time is not None
                    and abs((canonical_time - market_utc).total_seconds()) > 1,
                    f"{prefix}_TIME_MISMATCH",
                )
                reject(
                    previous_time is not None and market_utc <= previous_time,
                    f"{prefix}_NOT_STRICTLY_ASCENDING",
                )
                reject(market_utc.isoformat() in seen_times, f"{prefix}_DUPLICATE_TIME")
                previous_time = market_utc
                seen_times.add(market_utc.isoformat())
                reject(
                    captured_at is not None and market_utc > captured_at,
                    f"{prefix}_AFTER_CAPTURE",
                )
                reject(
                    as_of_date is not None and row_date >= as_of_date,
                    f"{prefix}_NOT_COMPLETED",
                )
                reject(
                    requested_bounds.get("start") is not None
                    and row_date < requested_bounds["start"],
                    f"{prefix}_BEFORE_REQUEST_START",
                )
                reject(
                    requested_bounds.get("end") is not None
                    and row_date > requested_bounds["end"],
                    f"{prefix}_AFTER_REQUEST_END",
                )
            prices: dict[str, float] = {}
            for field in ("open", "high", "low", "close"):
                value = _finite_number(raw_row.get(field))
                reject(value is None or value <= 0, f"{prefix}_{field.upper()}_INVALID")
                if value is not None:
                    prices[field] = value
            if len(prices) == 4:
                reject(
                    prices["high"] < max(prices["open"], prices["close"], prices["low"]),
                    f"{prefix}_HIGH_INVALID",
                )
                reject(
                    prices["low"] > min(prices["open"], prices["close"], prices["high"]),
                    f"{prefix}_LOW_INVALID",
                )
            for field in ("volume", "turnover"):
                if raw_row.get(field) is None:
                    continue
                value = _finite_number(raw_row.get(field))
                reject(value is None or value < 0, f"{prefix}_{field.upper()}_INVALID")

    if row_dates:
        reject(
            metadata_dates.get("actual_start") != row_dates[0],
            "HISTORY_ACTUAL_START_MISMATCH",
        )
        reject(
            metadata_dates.get("actual_end") != row_dates[-1],
            "HISTORY_ACTUAL_END_MISMATCH",
        )
        reject(
            metadata_dates.get("last_completed_session") != row_dates[-1],
            "HISTORY_LAST_COMPLETED_SESSION_MISMATCH",
        )
    if as_of_date is not None and metadata_dates.get("last_completed_session") is not None:
        reject(
            metadata_dates["last_completed_session"] >= as_of_date,
            "HISTORY_LAST_SESSION_NOT_COMPLETED",
        )

    return {
        "ready": not issues,
        "symbol": symbol,
        "expected_symbol": clean_expected,
        "row_count": len(rows) if isinstance(rows, list) else 0,
        "rows_checked": min(len(rows), MAX_VALIDATED_HISTORY_ROWS) if isinstance(rows, list) else 0,
        "first_session": row_dates[0].isoformat() if row_dates else "",
        "last_session": row_dates[-1].isoformat() if row_dates else "",
        "issues": issues,
        "issues_truncated": issues_truncated,
        "safe_history": safe_history,
        "safety_fields_explicit": safety_fields_explicit,
        "execution_capability": payload.get("execution_capability"),
        "live_trading_allowed": payload.get("live_trading_allowed"),
    }


class FutuUsMarketAdapter:
    """Strictly read-only Futu quote adapter for the storage research universe."""

    def __init__(
        self,
        host: str = FUTU_HOST,
        port: int = FUTU_PORT,
        cache_ttl_seconds: float = FUTU_CACHE_TTL_SECONDS,
        *,
        sdk_module: Any = _AUTO_SDK,
        socket_probe: Callable[[str, int], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.cache_ttl_seconds = max(1.0, float(cache_ttl_seconds))
        self._sdk_module = sdk_module
        self._sdk_installed = sdk_module is not _AUTO_SDK and sdk_module is not None
        self._sdk_import_error = ""
        self._socket_probe = socket_probe or self._default_socket_probe
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._cache: dict[tuple[str, ...], tuple[float, dict[str, Any]]] = {}
        self._revenue_request_times: list[float] = []
        self._lock = threading.RLock()

    @staticmethod
    def _default_socket_probe(host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.35):
                return True
        except OSError:
            return False

    def _sdk(self) -> Any | None:
        if self._sdk_module is _AUTO_SDK:
            self._sdk_installed = importlib.util.find_spec("futu") is not None
            try:
                import futu as futu_sdk  # type: ignore
            except (ImportError, OSError) as exc:
                self._sdk_import_error = f"{type(exc).__name__}: {str(exc)[:240]}"
                self._sdk_module = None
            else:
                self._sdk_installed = True
                self._sdk_import_error = ""
                self._sdk_module = futu_sdk
        return self._sdk_module

    def capabilities(self) -> dict[str, Any]:
        return {
            "source": "futu_opend",
            "market": "US",
            "quote_context": True,
            "snapshot": True,
            "daily_history": True,
            "fundamental_snapshot": True,
            "financial_statements": True,
            "revenue_breakdown": True,
            "capital_flow": True,
            "execution_capability": "none",
            "live_trading_allowed": False,
            "allowed_symbols": list(STORAGE_SYMBOLS),
            "quote_freshness": {
                "live_max_age_seconds": LIVE_QUOTE_MAX_AGE_SECONDS,
                "closed_session_max_age_seconds": CLOSED_SESSION_MAX_AGE_SECONDS,
                "closed_session_market_states": sorted(CLOSED_SESSION_MARKET_STATES),
            },
        }

    def status(self) -> dict[str, Any]:
        sdk_available = self._sdk() is not None
        opend_reachable = bool(sdk_available and self._socket_probe(self.host, self.port))
        sdk_state = (
            "ready"
            if sdk_available
            else "import_error"
            if self._sdk_installed and self._sdk_import_error
            else "missing"
        )
        return {
            "configured": True,
            "sdk_available": sdk_available,
            "sdk_installed": self._sdk_installed,
            "sdk_state": sdk_state,
            "sdk_error": (
                {
                    "code": "FUTU_SDK_IMPORT_ERROR",
                    "message": self._sdk_import_error,
                }
                if sdk_state == "import_error"
                else None
            ),
            "opend_reachable": opend_reachable,
            "state": "ready" if opend_reachable else "offline",
            "host": self.host,
            "port": self.port,
            **self.capabilities(),
        }

    @staticmethod
    def _normalize_symbols(symbols: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        requested: list[str] = []
        for raw_symbol in symbols:
            symbol = str(raw_symbol or "").strip().upper()
            if symbol and symbol not in requested:
                requested.append(symbol)
        unsupported = [symbol for symbol in requested if symbol not in STORAGE_SYMBOLS]
        if unsupported:
            raise ValueError(f"不在存储产业研究白名单：{', '.join(unsupported)}")
        if not requested:
            raise ValueError("至少需要一个行情代码")
        return tuple(requested)

    def quote_batch(
        self,
        symbols: tuple[str, ...] | list[str] = STORAGE_SYMBOLS,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        requested = self._normalize_symbols(symbols)
        cache_key = tuple(requested)
        now_monotonic = time.monotonic()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached[0] > now_monotonic and not force:
                payload = copy.deepcopy(cached[1])
                payload["cache"] = {"hit": True, "ttl_seconds": self.cache_ttl_seconds}
                return payload

            payload = self._fetch_quote_batch(requested)
            payload["cache"] = {"hit": False, "ttl_seconds": self.cache_ttl_seconds}
            self._cache[cache_key] = (time.monotonic() + self.cache_ttl_seconds, copy.deepcopy(payload))
            return payload

    def _fetch_quote_batch(self, symbols: tuple[str, ...]) -> dict[str, Any]:
        captured_at = self._clock().astimezone(timezone.utc)
        base = {
            "snapshot_id": f"futu_{uuid.uuid4().hex[:16]}",
            "source": "futu_opend",
            "market": "US",
            "symbols": list(symbols),
            "captured_at": _utc_iso(captured_at),
            "captured_at_ms": int(captured_at.timestamp() * 1000),
            "rows": [],
            "missing_symbols": list(symbols),
            "source_errors": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
        }
        sdk = self._sdk()
        if sdk is None:
            if self._sdk_installed and self._sdk_import_error:
                return self._offline(
                    base,
                    "FUTU_SDK_IMPORT_ERROR",
                    f"本机已安装 Futu OpenAPI SDK，但导入失败：{self._sdk_import_error}",
                )
            return self._offline(base, "FUTU_SDK_UNAVAILABLE", "本机未安装 Futu OpenAPI SDK")
        if not self._socket_probe(self.host, self.port):
            return self._offline(base, "FUTU_OPEND_OFFLINE", "本机 Futu OpenD 未连接")

        quote_context = None
        try:
            quote_context = sdk.OpenQuoteContext(host=self.host, port=self.port)
            ret, data = quote_context.get_market_snapshot(list(symbols))
            if ret != getattr(sdk, "RET_OK", 0):
                return self._offline(base, "FUTU_SNAPSHOT_FAILED", str(data)[:300])
            captured_at = self._clock().astimezone(timezone.utc)
            base["captured_at"] = _utc_iso(captured_at)
            base["captured_at_ms"] = int(captured_at.timestamp() * 1000)
            by_symbol = {
                str(row.get("code") or "").upper(): row
                for row in _records(data)
                if str(row.get("code") or "").upper() in symbols
            }
            rows = [self._normalize_quote(by_symbol[symbol], captured_at) for symbol in symbols if symbol in by_symbol]
            stale_symbols = [
                str(normalized.get("symbol") or "")
                for normalized in rows
                if self._needs_market_state(normalized)
            ]
            market_states, market_state_lookup_basis = self._market_states_for_symbols(
                quote_context,
                sdk,
                stale_symbols,
            )
            for normalized in rows:
                self._apply_closed_session_freshness(
                    normalized,
                    market_states=market_states,
                    lookup_basis=market_state_lookup_basis,
                )
            missing = [symbol for symbol in symbols if symbol not in by_symbol]
            unready_count = sum(1 for row in rows if not quote_research_ready(row))
            if missing:
                base["source_errors"].append({
                    "source": "futu_opend",
                    "code": "MISSING_SYMBOLS",
                    "message": f"未返回 {', '.join(missing)}",
                })
            base.update({
                "ok": bool(rows),
                "state": "ready" if rows and not missing and unready_count == 0 else "degraded",
                "rows": rows,
                "missing_symbols": missing,
                "data_quality": {
                    "requested": len(symbols),
                    "received": len(rows),
                    "ready": len(rows) - unready_count,
                    "stale_or_invalid": unready_count,
                },
            })
            return base
        except Exception as exc:
            return self._offline(base, "FUTU_CONNECTION_ERROR", str(exc)[:300])
        finally:
            if quote_context is not None:
                try:
                    quote_context.close()
                except Exception:
                    pass

    @staticmethod
    def _offline(base: dict[str, Any], code: str, message: str) -> dict[str, Any]:
        base.update({
            "ok": False,
            "state": "offline",
            "data_quality": {
                "requested": len(base["symbols"]),
                "received": 0,
                "ready": 0,
                "stale_or_invalid": 0,
            },
        })
        base["source_errors"].append({"source": "futu_opend", "code": code, "message": message})
        return base

    def _normalize_quote(self, row: dict[str, Any], captured_at: datetime) -> dict[str, Any]:
        updated_at = self._parse_market_time(row.get("update_time"))
        raw_age_seconds = (
            (captured_at - updated_at.astimezone(timezone.utc)).total_seconds()
            if updated_at
            else None
        )
        # Round away from zero so a sub-second future timestamp cannot be
        # truncated to age=0 and mislabeled ready. This keeps the adapter's
        # classification aligned with the convergence gate's exact timestamp
        # comparison while retaining an integer field for the UI.
        age_seconds = (
            math.floor(raw_age_seconds)
            if raw_age_seconds is not None and raw_age_seconds < 0
            else math.ceil(raw_age_seconds)
            if raw_age_seconds is not None
            else None
        )
        last_price = _finite_number(row.get("last_price"))
        previous_close = _finite_number(row.get("prev_close_price"))
        change_value = last_price - previous_close if last_price is not None and previous_close not in (None, 0) else None
        change_rate = change_value / previous_close * 100 if change_value is not None and previous_close else None
        valid = last_price is not None and last_price > 0 and updated_at is not None
        quality = (
            "future"
            if valid and raw_age_seconds is not None and raw_age_seconds < 0
            else "ready"
            if valid and raw_age_seconds is not None and raw_age_seconds <= LIVE_QUOTE_MAX_AGE_SECONDS
            else "stale"
        )
        freshness_basis = (
            "future_timestamp"
            if quality == "future"
            else "live_20m_window"
            if quality == "ready"
            else "age_exceeds_live_window"
            if valid
            else "invalid_or_missing_quote"
        )
        security_status = str(row.get("sec_status") or "")
        suspended = bool(row.get("suspension", False))
        security_status_ready = _security_status_allows_research(
            security_status,
            suspended,
        )
        equity_valid = bool(row.get("equity_valid", False))
        fundamental = (lambda field: _finite_number(row.get(field)) if equity_valid else None)
        return {
            "symbol": str(row.get("code") or "").upper(),
            "name": str(row.get("name") or ""),
            "updated_at": _utc_iso(updated_at) if updated_at else None,
            "market_time": str(row.get("update_time") or ""),
            "age_seconds": age_seconds,
            "quality": quality,
            "research_ready": quality == "ready" and security_status_ready,
            "quote_is_live": quality == "ready",
            "market_state": None,
            "freshness_basis": freshness_basis,
            "last": last_price,
            "open": _finite_number(row.get("open_price")),
            "high": _finite_number(row.get("high_price")),
            "low": _finite_number(row.get("low_price")),
            "previous_close": previous_close,
            "change": change_value,
            "change_rate": change_rate,
            "volume": _finite_number(row.get("volume")),
            "turnover": _finite_number(row.get("turnover")),
            "turnover_rate": _finite_number(row.get("turnover_rate")),
            "amplitude": _finite_number(row.get("amplitude")),
            "average_price": _finite_number(row.get("avg_price")),
            "volume_ratio": _finite_number(row.get("volume_ratio")),
            "bid": _finite_number(row.get("bid_price")),
            "ask": _finite_number(row.get("ask_price")),
            "highest_52_weeks": _finite_number(row.get("highest52weeks_price")),
            "lowest_52_weeks": _finite_number(row.get("lowest52weeks_price")),
            "equity_valid": equity_valid,
            "issued_shares": fundamental("issued_shares"),
            "total_market_value": fundamental("total_market_val"),
            "net_asset": fundamental("net_asset"),
            "net_profit": fundamental("net_profit"),
            "earnings_per_share": fundamental("earning_per_share"),
            "outstanding_shares": fundamental("outstanding_shares"),
            "circular_market_value": fundamental("circular_market_val"),
            "net_asset_per_share": fundamental("net_asset_per_share"),
            "earnings_yield": fundamental("ey_ratio"),
            "pe_ratio": fundamental("pe_ratio"),
            "pb_ratio": fundamental("pb_ratio"),
            "pe_ttm_ratio": fundamental("pe_ttm_ratio"),
            "dividend_ttm": fundamental("dividend_ttm"),
            "dividend_yield_ttm": fundamental("dividend_ratio_ttm"),
            "dividend_lfy": fundamental("dividend_lfy"),
            "dividend_yield_lfy": fundamental("dividend_lfy_ratio"),
            "security_status": security_status,
            "suspended": suspended,
        }

    def _apply_closed_session_freshness(
        self,
        quote: dict[str, Any],
        *,
        market_states: dict[str, str],
        lookup_basis: str,
    ) -> None:
        """Classify a stale valid quote without ever treating it as live.

        All eligible rows share one batched market-state lookup through the
        already-open ``OpenQuoteContext``. Unknown, failed, active, or overly
        old states remain stale.
        """
        if not self._needs_market_state(quote):
            return
        market_state = market_states.get(str(quote.get("symbol") or ""))
        quote["market_state"] = market_state
        quote["quote_is_live"] = False
        quote["research_ready"] = False

        if lookup_basis != "market_state_available":
            quote["freshness_basis"] = lookup_basis
            return
        if not market_state:
            quote["freshness_basis"] = "market_state_missing"
            return
        if market_state not in CLOSED_SESSION_MARKET_STATES:
            quote["freshness_basis"] = "market_state_not_closed"
            return

        age_seconds = quote.get("age_seconds")
        if (
            not isinstance(age_seconds, int)
            or age_seconds <= LIVE_QUOTE_MAX_AGE_SECONDS
            or age_seconds > CLOSED_SESSION_MAX_AGE_SECONDS
        ):
            quote["freshness_basis"] = "closed_session_age_exceeded"
            return

        quote["quality"] = "ready"
        quote["research_ready"] = _security_status_allows_research(
            quote.get("security_status"),
            quote.get("suspended"),
        )
        quote["freshness_basis"] = "closed_session_latest_snapshot"

    @staticmethod
    def _needs_market_state(quote: dict[str, Any]) -> bool:
        last_price = _finite_number(quote.get("last"))
        return bool(
            quote.get("quality") == "stale"
            and last_price is not None
            and last_price > 0
            and str(quote.get("updated_at") or "").strip()
        )

    @staticmethod
    def _market_states_for_symbols(
        quote_context: Any,
        sdk: Any,
        symbols: list[str],
    ) -> tuple[dict[str, str], str]:
        if not symbols:
            return {}, "not_required"
        try:
            ret, data = quote_context.get_market_state(symbols)
        except Exception:
            return {}, "market_state_lookup_failed"
        if ret != getattr(sdk, "RET_OK", 0):
            return {}, "market_state_lookup_failed"

        market_states = {
            str(row.get("code") or "").strip().upper(): str(
                row.get("market_state") or ""
            ).strip().upper()
            for row in _records(data)
            if str(row.get("code") or "").strip().upper() in symbols
            and str(row.get("market_state") or "").strip()
        }
        return market_states, "market_state_available"

    @staticmethod
    def _parse_market_time(value: Any) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=US_EASTERN)
        return parsed

    def daily_history(
        self,
        symbol: str,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 120,
    ) -> dict[str, Any]:
        normalized_symbol = self._normalize_symbols([symbol])[0]
        batch = self.daily_history_batch(
            [normalized_symbol],
            start=start,
            end=end,
            limit=limit,
        )
        return batch["histories"][normalized_symbol]

    @staticmethod
    def _history_base(
        symbol: str,
        start: str | None,
        end: str | None,
        limit: int,
        *,
        captured_at: str,
        as_of_date: str,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "source": "futu_opend",
            "interval": "1d",
            "price_adjustment": "QFQ",
            "captured_at": captured_at,
            "as_of_date": as_of_date,
            "last_completed_session": "",
            "actual_start": "",
            "actual_end": "",
            "symbol": symbol,
            "start": start,
            "end": end,
            "limit": limit,
            "page_count": 0,
            "rows": [],
            "source_errors": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

    def daily_history_batch(
        self,
        symbols: tuple[str, ...] | list[str] = STORAGE_SYMBOLS,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 120,
    ) -> dict[str, Any]:
        requested = self._normalize_symbols(symbols)
        safe_limit = min(500, max(1, int(limit)))
        captured_moment = self._clock()
        captured_market = captured_moment.astimezone(US_EASTERN)
        captured_at = _utc_iso(captured_moment)
        as_of_date = captured_market.date().isoformat()
        histories = {
            symbol: self._history_base(
                symbol,
                start,
                end,
                safe_limit,
                captured_at=captured_at,
                as_of_date=as_of_date,
            )
            for symbol in requested
        }
        result = {
            "ok": False,
            "source": "futu_opend",
            "interval": "1d",
            "price_adjustment": "QFQ",
            "captured_at": captured_at,
            "as_of_date": as_of_date,
            "symbols": list(requested),
            "histories": histories,
            "source_errors": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
        }
        sdk = self._sdk()
        if sdk is None:
            error = {"source": "futu_opend", "code": "FUTU_SDK_UNAVAILABLE", "message": "本机未安装 Futu OpenAPI SDK"}
            result["source_errors"].append(error)
            for history in histories.values():
                history["source_errors"].append(error)
            return result
        if not self._socket_probe(self.host, self.port):
            error = {"source": "futu_opend", "code": "FUTU_OPEND_OFFLINE", "message": "本机 Futu OpenD 未连接"}
            result["source_errors"].append(error)
            for history in histories.values():
                history["source_errors"].append(error)
            return result

        quote_context = None
        try:
            quote_context = sdk.OpenQuoteContext(host=self.host, port=self.port)
            for symbol in requested:
                history = histories[symbol]
                try:
                    rows: list[dict[str, Any]] = []
                    page_req_key: Any | None = None
                    seen_page_keys: set[str] = set()
                    page_count = 0
                    pagination_complete = False
                    while page_count < MAX_HISTORY_PAGES:
                        request_kwargs = {
                            "start": start,
                            "end": end,
                            "ktype": getattr(getattr(sdk, "KLType", object()), "K_DAY", "K_DAY"),
                            "autype": getattr(getattr(sdk, "AuType", object()), "QFQ", "QFQ"),
                            # A small caller-facing limit must not force hundreds
                            # of old-first pages before the latest session is
                            # reached. Fetch the largest bounded page, then retain
                            # only the requested tail after pagination completes.
                            "max_count": 500,
                            "extended_time": False,
                        }
                        if page_req_key is not None:
                            request_kwargs["page_req_key"] = page_req_key
                        response = quote_context.request_history_kline(
                            symbol,
                            **request_kwargs,
                        )
                        page_count += 1
                        ret, data = response[0], response[1]
                        if ret != getattr(sdk, "RET_OK", 0):
                            history["source_errors"].append({
                                "source": "futu_opend",
                                "symbol": symbol,
                                "code": "FUTU_HISTORY_FAILED",
                                "message": str(data)[:300],
                            })
                            break
                        for row in _records(data):
                            market_time = self._parse_market_time(row.get("time_key"))
                            if (
                                market_time is None
                                or market_time.astimezone(US_EASTERN).date()
                                >= captured_market.date()
                            ):
                                continue
                            rows.append({
                                "symbol": symbol,
                                "market_time": str(row.get("time_key") or ""),
                                "time": _utc_iso(market_time),
                                "open": _finite_number(row.get("open")),
                                "close": _finite_number(row.get("close")),
                                "high": _finite_number(row.get("high")),
                                "low": _finite_number(row.get("low")),
                                "volume": _finite_number(row.get("volume")),
                                "turnover": _finite_number(row.get("turnover")),
                                "change_rate": _finite_number(row.get("change_rate")),
                            })
                        next_page_req_key = response[2] if len(response) > 2 else None
                        if next_page_req_key in (None, ""):
                            pagination_complete = True
                            break
                        page_key_token = repr(next_page_req_key)
                        if page_key_token in seen_page_keys:
                            history["source_errors"].append({
                                "source": "futu_opend",
                                "symbol": symbol,
                                "code": "FUTU_HISTORY_PAGINATION_LOOP",
                                "message": "Futu 日线分页键重复，无法证明已读取到最新页",
                            })
                            break
                        seen_page_keys.add(page_key_token)
                        page_req_key = next_page_req_key
                    if not pagination_complete and page_count >= MAX_HISTORY_PAGES:
                        history["source_errors"].append({
                            "source": "futu_opend",
                            "symbol": symbol,
                            "code": "FUTU_HISTORY_PAGINATION_LIMIT",
                            "message": f"Futu 日线分页超过 {MAX_HISTORY_PAGES} 页硬上限，无法证明已读取到最新页",
                        })
                    rows = list({row["time"]: row for row in rows}.values())
                    rows.sort(key=lambda item: item["time"])
                    rows = rows[-safe_limit:]
                    history.update({
                        "ok": bool(rows) and pagination_complete,
                        "page_count": page_count,
                        "rows": rows,
                        "last_completed_session": (
                            str(rows[-1]["market_time"])[:10] if rows else ""
                        ),
                        "actual_start": (
                            str(rows[0]["market_time"])[:10] if rows else ""
                        ),
                        "actual_end": (
                            str(rows[-1]["market_time"])[:10] if rows else ""
                        ),
                    })
                    if not rows:
                        history["source_errors"].append({
                            "source": "futu_opend",
                            "symbol": symbol,
                            "code": "FUTU_HISTORY_EMPTY",
                            "message": "未返回可用且早于美东当前日期的已完成日线数据",
                        })
                except Exception as exc:
                    history["source_errors"].append({
                        "source": "futu_opend",
                        "symbol": symbol,
                        "code": "FUTU_HISTORY_ERROR",
                        "message": str(exc)[:300],
                    })
        except Exception as exc:
            error = {"source": "futu_opend", "code": "FUTU_CONNECTION_ERROR", "message": str(exc)[:300]}
            result["source_errors"].append(error)
            for history in histories.values():
                if not history["source_errors"]:
                    history["source_errors"].append(error)
        finally:
            if quote_context is not None:
                try:
                    quote_context.close()
                except Exception:
                    pass
        result["ok"] = any(history["ok"] for history in histories.values())
        result["source_errors"].extend(
            error
            for history in histories.values()
            for error in history["source_errors"]
            if error not in result["source_errors"]
        )
        return result

    def capital_flow_batch(
        self,
        symbols: tuple[str, ...] | list[str] = STORAGE_SYMBOLS,
        *,
        limit_days: int = 20,
    ) -> dict[str, Any]:
        requested = self._normalize_symbols(symbols)
        safe_limit = min(120, max(1, int(limit_days)))
        result: dict[str, Any] = {
            "ok": False,
            "source": "futu_opend_capital_flow",
            "period": "DAY",
            "symbols": list(requested),
            "rows": [],
            "source_errors": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
        }
        sdk = self._sdk()
        if sdk is None:
            result["source_errors"].append({"source": "futu_opend", "code": "FUTU_SDK_UNAVAILABLE", "message": "本机未安装 Futu OpenAPI SDK"})
            return result
        if not self._socket_probe(self.host, self.port):
            result["source_errors"].append({"source": "futu_opend", "code": "FUTU_OPEND_OFFLINE", "message": "本机 Futu OpenD 未连接"})
            return result

        quote_context = None
        try:
            quote_context = sdk.OpenQuoteContext(host=self.host, port=self.port)
            getter = getattr(quote_context, "get_capital_flow", None)
            if not callable(getter):
                result["source_errors"].append({
                    "source": "futu_opend",
                    "code": "FUTU_CAPITAL_FLOW_UNAVAILABLE",
                    "message": "当前 Futu SDK 不提供资金流向只读接口",
                })
                return result
            period = getattr(getattr(sdk, "PeriodType", object()), "DAY", "DAY")
            now_market = self._clock().astimezone(US_EASTERN)
            for symbol in requested:
                try:
                    ret, data = getter(symbol, period_type=period)
                    if ret != getattr(sdk, "RET_OK", 0):
                        result["source_errors"].append({
                            "source": "futu_opend",
                            "symbol": symbol,
                            "code": "FUTU_CAPITAL_FLOW_FAILED",
                            "message": str(data)[:300],
                        })
                        continue
                    flow_rows: list[dict[str, Any]] = []
                    for row in _records(data):
                        market_time = self._parse_market_time(row.get("capital_flow_item_time"))
                        if market_time is None or market_time > now_market:
                            continue
                        flow_rows.append({
                            "time": _utc_iso(market_time),
                            "market_time": str(row.get("capital_flow_item_time") or ""),
                            "net_inflow": _finite_number(row.get("in_flow")),
                            "main_net_inflow": _finite_number(row.get("main_in_flow")),
                        })
                    flow_rows.sort(key=lambda item: item["time"])
                    flow_rows = flow_rows[-safe_limit:]
                    if not flow_rows:
                        result["source_errors"].append({
                            "source": "futu_opend",
                            "symbol": symbol,
                            "code": "FUTU_CAPITAL_FLOW_EMPTY",
                            "message": "未返回可用且不晚于当前时点的日度资金流数据",
                        })
                        continue

                    def rolling_sum(field: str, count: int) -> float | None:
                        values = [
                            float(row[field])
                            for row in flow_rows[-count:]
                            if row.get(field) is not None
                        ]
                        return sum(values) if values else None

                    latest = flow_rows[-1]
                    result["rows"].append({
                        "symbol": symbol,
                        "source": "futu_opend_capital_flow",
                        "period": "DAY",
                        "as_of": latest["market_time"],
                        "sample_count": len(flow_rows),
                        "quality": "ready" if len(flow_rows) >= min(20, safe_limit) else "limited",
                        "net_inflow_1d": latest.get("net_inflow"),
                        "net_inflow_5d": rolling_sum("net_inflow", 5),
                        "net_inflow_20d": rolling_sum("net_inflow", 20),
                        "main_net_inflow_5d": rolling_sum("main_net_inflow", 5),
                        "main_net_inflow_20d": rolling_sum("main_net_inflow", 20),
                    })
                except Exception as exc:
                    result["source_errors"].append({
                        "source": "futu_opend",
                        "symbol": symbol,
                        "code": "FUTU_CAPITAL_FLOW_ERROR",
                        "message": str(exc)[:300],
                    })
        except Exception as exc:
            result["source_errors"].append({"source": "futu_opend", "code": "FUTU_CONNECTION_ERROR", "message": str(exc)[:300]})
        finally:
            if quote_context is not None:
                try:
                    quote_context.close()
                except Exception:
                    pass
        result["ok"] = bool(result["rows"])
        return result

    def financial_statements_batch(
        self,
        symbols: tuple[str, ...] | list[str] = STORAGE_SYMBOLS,
        *,
        statement_type: str = "main_index",
        limit: int = 2,
    ) -> dict[str, Any]:
        requested = self._normalize_symbols(symbols)
        normalized_type = str(statement_type or "").strip().lower()
        if normalized_type not in FINANCIAL_STATEMENT_TYPES:
            raise ValueError(
                "财务报表类型必须是 income、balance_sheet、cash_flow 或 main_index"
            )
        safe_limit = min(10, max(1, int(limit)))
        captured_at = self._clock().astimezone(timezone.utc)
        result: dict[str, Any] = {
            "ok": False,
            "source": "futu_opend_financial_statements",
            "captured_at": _utc_iso(captured_at),
            "statement_type": normalized_type,
            "financial_type": "quarterly_annual",
            "symbols": list(requested),
            "rows": [],
            "source_errors": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
        }
        sdk = self._sdk()
        if sdk is None:
            result["source_errors"].append({
                "source": "futu_opend",
                "code": "FUTU_SDK_UNAVAILABLE",
                "message": "本机未安装 Futu OpenAPI SDK",
            })
            return result
        if not self._socket_probe(self.host, self.port):
            result["source_errors"].append({
                "source": "futu_opend",
                "code": "FUTU_OPEND_OFFLINE",
                "message": "本机 Futu OpenD 未连接",
            })
            return result

        quote_context = None
        try:
            quote_context = sdk.OpenQuoteContext(host=self.host, port=self.port)
            getter = getattr(quote_context, "get_financials_statements", None)
            if not callable(getter):
                result["source_errors"].append({
                    "source": "futu_opend",
                    "code": "FUTU_FINANCIALS_UNAVAILABLE",
                    "message": "当前 Futu SDK 不提供财务报表只读接口",
                })
                return result
            now_market_date = captured_at.astimezone(US_EASTERN).date()
            for symbol in requested:
                try:
                    ret, data = getter(
                        symbol,
                        statement_type=FINANCIAL_STATEMENT_TYPES[normalized_type],
                        financial_type=10,
                        num=safe_limit,
                    )
                    if ret != getattr(sdk, "RET_OK", 0):
                        result["source_errors"].append({
                            "source": "futu_opend",
                            "symbol": symbol,
                            "code": "FUTU_FINANCIALS_FAILED",
                            "message": str(data)[:300],
                        })
                        continue
                    payload = data if isinstance(data, dict) else {}
                    reports = self._normalize_financial_reports(
                        payload.get("report_list"),
                        now_market_date=now_market_date,
                        limit=safe_limit,
                    )
                    if not reports:
                        result["source_errors"].append({
                            "source": "futu_opend",
                            "symbol": symbol,
                            "code": "FUTU_FINANCIALS_EMPTY",
                            "message": "未返回可用且报告期不晚于当前日期的财务报表",
                        })
                        continue
                    result["rows"].append({
                        "symbol": symbol,
                        "statement_type": normalized_type,
                        "quality": "ready",
                        "report_count": len(reports),
                        "next_key": str(payload.get("next_key") or "-1")[:120],
                        "reports": reports,
                    })
                except Exception as exc:
                    result["source_errors"].append({
                        "source": "futu_opend",
                        "symbol": symbol,
                        "code": "FUTU_FINANCIALS_ERROR",
                        "message": str(exc)[:300],
                    })
        except Exception as exc:
            result["source_errors"].append({
                "source": "futu_opend",
                "code": "FUTU_CONNECTION_ERROR",
                "message": str(exc)[:300],
            })
        finally:
            if quote_context is not None:
                try:
                    quote_context.close()
                except Exception:
                    pass
        result["ok"] = bool(result["rows"])
        return result

    @staticmethod
    def _normalize_financial_reports(
        value: Any,
        *,
        now_market_date: Any,
        limit: int,
    ) -> list[dict[str, Any]]:
        raw_reports = value if isinstance(value, list) else []
        reports: list[dict[str, Any]] = []
        for raw_report in raw_reports:
            if not isinstance(raw_report, dict):
                continue
            period_end = str(raw_report.get("date_time_str") or "").strip()
            try:
                period_date = datetime.strptime(period_end, "%Y-%m-%d").date()
            except ValueError:
                continue
            if period_date > now_market_date:
                continue
            items: list[dict[str, Any]] = []
            raw_items = raw_report.get("item_list") if isinstance(raw_report.get("item_list"), list) else []
            for raw_item in raw_items[:200]:
                if not isinstance(raw_item, dict):
                    continue
                data_value = _finite_number(raw_item.get("data"))
                if data_value is None:
                    continue
                try:
                    field_id = int(raw_item.get("field_id") or 0)
                except (TypeError, ValueError):
                    continue
                if field_id <= 0:
                    continue
                item: dict[str, Any] = {
                    "field_id": field_id,
                    "display_name": str(raw_item.get("display_name") or "")[:200],
                    "data": data_value,
                }
                yoy = _finite_number(raw_item.get("yoy"))
                qoq = _finite_number(raw_item.get("qoq"))
                if yoy is not None:
                    item["yoy"] = yoy
                if qoq is not None:
                    item["qoq"] = qoq
                items.append(item)
            if not items:
                continue
            try:
                fiscal_year = int(raw_report.get("fiscal_year") or 0)
                financial_type = int(raw_report.get("financial_type") or 0)
            except (TypeError, ValueError):
                fiscal_year = 0
                financial_type = 0
            reports.append({
                "period_end": period_end,
                "fiscal_year": fiscal_year,
                "financial_type": financial_type,
                "period_text": str(raw_report.get("period_text") or "")[:80],
                "currency_code": str(raw_report.get("currency_code") or "")[:20],
                "currency_info": str(raw_report.get("currency_info") or "")[:80],
                "accounting_standards": str(raw_report.get("accounting_standards") or "")[:120],
                "auditor_report": str(raw_report.get("auditor_report") or "")[:120],
                "item_count": len(items),
                "items_truncated": len(raw_items) > 200,
                "items": items,
            })
        reports.sort(key=lambda item: item["period_end"])
        return reports[-limit:]

    def revenue_breakdown_batch(
        self,
        symbols: tuple[str, ...] | list[str] = STORAGE_SYMBOLS,
    ) -> dict[str, Any]:
        requested = self._normalize_symbols(symbols)
        captured_at = self._clock().astimezone(timezone.utc)
        result: dict[str, Any] = {
            "ok": False,
            "source": "futu_opend_revenue_breakdown",
            "captured_at": _utc_iso(captured_at),
            "symbols": list(requested),
            "rows": [],
            "source_errors": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
        }
        sdk = self._sdk()
        if sdk is None:
            result["source_errors"].append({
                "source": "futu_opend",
                "code": "FUTU_SDK_UNAVAILABLE",
                "message": "本机未安装 Futu OpenAPI SDK",
            })
            return result
        if not self._socket_probe(self.host, self.port):
            result["source_errors"].append({
                "source": "futu_opend",
                "code": "FUTU_OPEND_OFFLINE",
                "message": "本机 Futu OpenD 未连接",
            })
            return result

        quote_context = None
        try:
            quote_context = sdk.OpenQuoteContext(host=self.host, port=self.port)
            getter = getattr(quote_context, "get_financials_revenue_breakdown", None)
            if not callable(getter):
                result["source_errors"].append({
                    "source": "futu_opend",
                    "code": "FUTU_REVENUE_BREAKDOWN_UNAVAILABLE",
                    "message": "当前 Futu SDK 不提供主营构成只读接口",
                })
                return result
            for symbol in requested:
                try:
                    self._reserve_revenue_request()
                    ret, data = getter(symbol)
                    if ret != getattr(sdk, "RET_OK", 0):
                        result["source_errors"].append({
                            "source": "futu_opend",
                            "symbol": symbol,
                            "code": "FUTU_REVENUE_BREAKDOWN_FAILED",
                            "message": str(data)[:300],
                        })
                        continue
                    row = self._normalize_revenue_breakdown(
                        data,
                        symbol=symbol,
                        captured_at=captured_at,
                    )
                    if row is None:
                        result["source_errors"].append({
                            "source": "futu_opend",
                            "symbol": symbol,
                            "code": "FUTU_REVENUE_BREAKDOWN_EMPTY",
                            "message": "未返回带有效报告期和维度数据的主营构成",
                        })
                        continue
                    result["rows"].append(row)
                except Exception as exc:
                    result["source_errors"].append({
                        "source": "futu_opend",
                        "symbol": symbol,
                        "code": "FUTU_REVENUE_BREAKDOWN_ERROR",
                        "message": str(exc)[:300],
                    })
        except Exception as exc:
            result["source_errors"].append({
                "source": "futu_opend",
                "code": "FUTU_CONNECTION_ERROR",
                "message": str(exc)[:300],
            })
        finally:
            if quote_context is not None:
                try:
                    quote_context.close()
                except Exception:
                    pass
        result["ok"] = bool(result["rows"])
        return result

    def _reserve_revenue_request(self) -> None:
        now_monotonic = time.monotonic()
        with self._lock:
            self._revenue_request_times = [
                moment for moment in self._revenue_request_times
                if moment > now_monotonic - 30.0
            ]
            if len(self._revenue_request_times) >= 30:
                raise RuntimeError("富途主营构成接口已达到 30 秒 30 次的本地安全上限")
            self._revenue_request_times.append(now_monotonic)

    @staticmethod
    def _normalize_revenue_breakdown(
        value: Any,
        *,
        symbol: str,
        captured_at: datetime,
    ) -> dict[str, Any] | None:
        payload = value if isinstance(value, dict) else {}
        period = str(payload.get("period") or "").strip()[:80]
        raw_dates = payload.get("screen_date_list") if isinstance(payload.get("screen_date_list"), list) else []
        screen_dates: list[dict[str, Any]] = []
        for raw_date in raw_dates[:100]:
            if not isinstance(raw_date, dict):
                continue
            try:
                timestamp = int(raw_date.get("date") or 0)
                financial_type = int(raw_date.get("financial_type") or 0)
            except (TypeError, ValueError):
                continue
            if timestamp <= 0 or timestamp > int(captured_at.timestamp()):
                continue
            screen_dates.append({
                "timestamp": timestamp,
                "time": _utc_iso(datetime.fromtimestamp(timestamp, tz=timezone.utc)),
                "period_text": str(raw_date.get("period_text") or "")[:80],
                "financial_type": financial_type,
            })
        selected_date = next(
            (item for item in screen_dates if item["period_text"] == period),
            None,
        )
        if not period or selected_date is None:
            return None

        raw_groups = payload.get("breakdown_list") if isinstance(payload.get("breakdown_list"), list) else []
        dimensions: list[dict[str, Any]] = []
        for raw_group in raw_groups:
            if not isinstance(raw_group, dict):
                continue
            try:
                type_code = int(raw_group.get("type") or 0)
            except (TypeError, ValueError):
                continue
            dimension = REVENUE_BREAKDOWN_TYPES.get(type_code)
            if not dimension:
                continue
            raw_items = raw_group.get("item_list") if isinstance(raw_group.get("item_list"), list) else []
            items: list[dict[str, Any]] = []
            for raw_item in raw_items[:100]:
                if not isinstance(raw_item, dict):
                    continue
                name = str(raw_item.get("name") or "").strip()[:240]
                income = _finite_number(raw_item.get("main_oper_income"))
                ratio = _finite_number(raw_item.get("ratio"))
                if not name or (income is None and ratio is None):
                    continue
                item: dict[str, Any] = {"name": name}
                if income is not None:
                    item["operating_revenue"] = income
                if ratio is not None:
                    item["ratio_pct"] = ratio
                items.append(item)
            if items:
                dimensions.append({
                    "type": dimension,
                    "type_code": type_code,
                    "item_count": len(items),
                    "items_truncated": len(raw_items) > 100,
                    "items": items,
                })
        if not dimensions:
            return None
        return {
            "symbol": symbol,
            "quality": "ready",
            "period": period,
            "period_timestamp": selected_date["timestamp"],
            "period_time": selected_date["time"],
            "financial_type": selected_date["financial_type"],
            "currency_code": str(payload.get("currency_code") or "")[:20],
            "dimension_count": len(dimensions),
            "dimensions": dimensions,
            "available_periods": screen_dates,
        }
