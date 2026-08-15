from __future__ import annotations

import copy
import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ..config import SEC_CACHE_TTL_SECONDS, SEC_USER_AGENT
from .futu_readonly import STORAGE_SYMBOLS, US_EASTERN, _utc_iso


SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
SEC_ALLOWED_FORMS = frozenset({"10-K", "10-Q", "8-K", "20-F", "40-F", "6-K"})
SEC_DEFAULT_FORMS = ("10-K", "10-Q", "8-K", "20-F", "40-F", "6-K")
SEC_MAX_RESPONSE_BYTES = 2_000_000


class SecEdgarAdapter:
    """Read-only adapter for SEC ticker mappings and recent EDGAR submissions."""

    def __init__(
        self,
        user_agent: str = SEC_USER_AGENT,
        cache_ttl_seconds: float = SEC_CACHE_TTL_SECONDS,
        *,
        fetch_json: Callable[[str, str], dict[str, Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
        min_request_interval_seconds: float = 0.11,
    ) -> None:
        self.user_agent = str(user_agent or "").strip()[:300]
        self.cache_ttl_seconds = max(60.0, float(cache_ttl_seconds))
        self._fetch_json = fetch_json or self._default_fetch_json
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._min_interval = max(0.11, float(min_request_interval_seconds))
        self._last_request_monotonic = 0.0
        self._request_lock = threading.Lock()
        self._cache_lock = threading.RLock()
        self._ticker_cache: tuple[float, dict[str, dict[str, str]]] | None = None
        self._filings_cache: dict[tuple[str, tuple[str, ...], int], tuple[float, dict[str, Any]]] = {}

    def status(self) -> dict[str, Any]:
        configured = self._is_declared_user_agent()
        return {
            "source": "sec_edgar",
            "configured": configured,
            "state": "ready" if configured else "unconfigured",
            "reason": "" if configured else "需要在本机 SEC_USER_AGENT 中填写产品或组织名与联系邮箱",
            "official_submissions": True,
            "authentication_required": False,
            "user_agent_declared": configured,
            "allowed_symbols": list(STORAGE_SYMBOLS),
            "allowed_forms": list(SEC_DEFAULT_FORMS),
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

    def _is_declared_user_agent(self) -> bool:
        return (
            "@" in self.user_agent
            and len(self.user_agent) >= 10
            and "\r" not in self.user_agent
            and "\n" not in self.user_agent
        )

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
            raise ValueError("至少需要一个 SEC 标的代码")
        return tuple(requested)

    @staticmethod
    def _normalize_forms(forms: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
        requested = forms or SEC_DEFAULT_FORMS
        normalized: list[str] = []
        for raw_form in requested:
            form = str(raw_form or "").strip().upper()
            if form and form not in normalized:
                normalized.append(form)
        unsupported = [form for form in normalized if form not in SEC_ALLOWED_FORMS]
        if unsupported:
            raise ValueError(f"不支持的 SEC 表单类型：{', '.join(unsupported)}")
        if not normalized:
            raise ValueError("至少需要一个 SEC 表单类型")
        return tuple(normalized)

    def recent_filings_batch(
        self,
        symbols: tuple[str, ...] | list[str] = STORAGE_SYMBOLS,
        *,
        forms: tuple[str, ...] | list[str] | None = None,
        limit: int = 8,
        force: bool = False,
    ) -> dict[str, Any]:
        requested = self._normalize_symbols(symbols)
        normalized_forms = self._normalize_forms(forms)
        safe_limit = min(40, max(1, int(limit)))
        captured_at = self._clock().astimezone(timezone.utc)
        result: dict[str, Any] = {
            "ok": False,
            "source": "sec_edgar_submissions",
            "source_type": "regulatory_filing",
            "source_tier": "primary",
            "captured_at": _utc_iso(captured_at),
            "forms": list(normalized_forms),
            "symbols": list(requested),
            "rows": [],
            "source_errors": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
        }
        if not self._is_declared_user_agent():
            result["source_errors"].append({
                "source": "sec_edgar",
                "code": "SEC_USER_AGENT_REQUIRED",
                "message": "SEC 自动访问需要在本机声明产品或组织名与联系邮箱",
            })
            return result

        try:
            ticker_map = self._ticker_map(force=force)
        except Exception as exc:
            result["source_errors"].append({
                "source": "sec_edgar",
                "code": "SEC_TICKER_MAP_ERROR",
                "message": str(exc)[:300],
            })
            return result

        today = captured_at.astimezone(US_EASTERN).date()
        for symbol in requested:
            ticker = symbol.removeprefix("US.")
            company = ticker_map.get(ticker)
            if not company:
                result["source_errors"].append({
                    "source": "sec_edgar",
                    "symbol": symbol,
                    "code": "SEC_CIK_NOT_FOUND",
                    "message": "SEC 官方 ticker/CIK 映射中未找到该标的",
                })
                continue
            cache_key = (symbol, normalized_forms, safe_limit)
            with self._cache_lock:
                cached = self._filings_cache.get(cache_key)
                if cached and cached[0] > time.monotonic() and not force:
                    row = copy.deepcopy(cached[1])
                    row["cache_hit"] = True
                    result["rows"].append(row)
                    continue
            try:
                submissions = self._request_json(SEC_SUBMISSIONS_URL.format(cik=company["cik"]))
                filings = self._normalize_recent_filings(
                    submissions,
                    cik=company["cik"],
                    forms=normalized_forms,
                    limit=safe_limit,
                    today=today,
                )
            except Exception as exc:
                result["source_errors"].append({
                    "source": "sec_edgar",
                    "symbol": symbol,
                    "code": "SEC_SUBMISSIONS_ERROR",
                    "message": str(exc)[:300],
                })
                continue
            if not filings:
                result["source_errors"].append({
                    "source": "sec_edgar",
                    "symbol": symbol,
                    "code": "SEC_FILINGS_EMPTY",
                    "message": "SEC 最近申报中没有符合筛选条件的文件",
                })
                continue
            row = {
                "symbol": symbol,
                "ticker": ticker,
                "cik": company["cik"],
                "company_name": str(submissions.get("name") or company["title"])[:240],
                "quality": "ready",
                "cache_hit": False,
                "filing_count": len(filings),
                "filings": filings,
            }
            with self._cache_lock:
                self._filings_cache[cache_key] = (
                    time.monotonic() + self.cache_ttl_seconds,
                    copy.deepcopy(row),
                )
            result["rows"].append(row)
        result["ok"] = bool(result["rows"])
        return result

    def _ticker_map(self, *, force: bool) -> dict[str, dict[str, str]]:
        with self._cache_lock:
            if self._ticker_cache and self._ticker_cache[0] > time.monotonic() and not force:
                return copy.deepcopy(self._ticker_cache[1])
        payload = self._request_json(SEC_TICKERS_URL)
        ticker_map: dict[str, dict[str, str]] = {}
        for item in payload.values():
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker") or "").strip().upper()
            try:
                cik = f"{int(item.get('cik_str')):010d}"
            except (TypeError, ValueError):
                continue
            if ticker and ticker in {symbol.removeprefix("US.") for symbol in STORAGE_SYMBOLS}:
                ticker_map[ticker] = {
                    "cik": cik,
                    "title": str(item.get("title") or "")[:240],
                }
        with self._cache_lock:
            self._ticker_cache = (time.monotonic() + 86_400, copy.deepcopy(ticker_map))
        return ticker_map

    def _request_json(self, url: str) -> dict[str, Any]:
        with self._request_lock:
            remaining = self._min_interval - (time.monotonic() - self._last_request_monotonic)
            if remaining > 0:
                time.sleep(remaining)
            try:
                payload = self._fetch_json(url, self.user_agent)
            finally:
                self._last_request_monotonic = time.monotonic()
        if not isinstance(payload, dict):
            raise ValueError("SEC 返回的 JSON 顶层不是对象")
        return payload

    @staticmethod
    def _default_fetch_json(url: str, user_agent: str) -> dict[str, Any]:
        if url != SEC_TICKERS_URL and not url.startswith("https://data.sec.gov/submissions/CIK"):
            raise ValueError("SEC 适配器拒绝非官方固定端点")
        request = Request(url, headers={
            "User-Agent": user_agent,
            "Accept": "application/json",
        })
        with urlopen(request, timeout=12) as response:
            final_url = str(response.geturl() or "")
            parsed = urlparse(final_url)
            if parsed.scheme != "https" or parsed.hostname not in {"www.sec.gov", "data.sec.gov"}:
                raise ValueError("SEC 响应重定向到了非官方端点")
            declared_length = response.headers.get("Content-Length")
            if declared_length and int(declared_length) > SEC_MAX_RESPONSE_BYTES:
                raise ValueError("SEC 响应超过 2 MB 上限")
            raw = response.read(SEC_MAX_RESPONSE_BYTES + 1)
        if len(raw) > SEC_MAX_RESPONSE_BYTES:
            raise ValueError("SEC 响应超过 2 MB 上限")
        return json.loads(raw.decode("utf-8"))

    @staticmethod
    def _normalize_recent_filings(
        payload: dict[str, Any],
        *,
        cik: str,
        forms: tuple[str, ...],
        limit: int,
        today: Any,
    ) -> list[dict[str, Any]]:
        recent = ((payload.get("filings") or {}).get("recent") or {})
        if not isinstance(recent, dict):
            return []
        accessions = recent.get("accessionNumber") if isinstance(recent.get("accessionNumber"), list) else []
        filings: list[dict[str, Any]] = []
        for index, accession in enumerate(accessions):
            form = SecEdgarAdapter._column_value(recent, "form", index).upper()
            if form not in forms:
                continue
            filing_date = SecEdgarAdapter._column_value(recent, "filingDate", index)
            try:
                filing_day = datetime.strptime(filing_date, "%Y-%m-%d").date()
            except ValueError:
                continue
            if filing_day > today:
                continue
            accession_number = str(accession or "").strip()
            primary_document = SecEdgarAdapter._column_value(recent, "primaryDocument", index)
            if not accession_number or not primary_document or "/" in primary_document or "\\" in primary_document:
                continue
            accession_compact = accession_number.replace("-", "")
            filing_url = f"{SEC_ARCHIVES_BASE}/{int(cik)}/{accession_compact}/{primary_document}"
            acceptance_time = SecEdgarAdapter._column_value(recent, "acceptanceDateTime", index)
            filings.append({
                "accession_number": accession_number[:40],
                "form": form,
                "filing_date": filing_date,
                "report_date": SecEdgarAdapter._column_value(recent, "reportDate", index)[:20],
                "accepted_at": acceptance_time[:40],
                "published_at": acceptance_time[:40] or filing_date,
                "primary_document": primary_document[:240],
                "description": SecEdgarAdapter._column_value(recent, "primaryDocDescription", index)[:300],
                "items": SecEdgarAdapter._column_value(recent, "items", index)[:240],
                "official_url": filing_url,
                "source_type": "regulatory_filing",
                "source_tier": "primary",
            })
            if len(filings) >= limit:
                break
        return filings

    @staticmethod
    def _column_value(columns: dict[str, Any], key: str, index: int) -> str:
        values = columns.get(key)
        if not isinstance(values, list) or index >= len(values):
            return ""
        return str(values[index] or "").strip()
