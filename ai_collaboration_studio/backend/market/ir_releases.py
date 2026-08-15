from __future__ import annotations

import copy
import html
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

from .futu_readonly import STORAGE_SYMBOLS, _utc_iso


IR_FEEDS = {
    "US.MU": {
        "publisher": "Micron Technology Investor Relations",
        "url": "https://investors.micron.com/rss/news-releases.xml?items=30",
        "hosts": {"investors.micron.com"},
        "presentation_hub_url": "https://investors.micron.com/quarterly-results",
        "technology_scope": ["DRAM", "NAND"],
    },
    "US.SNDK": {
        "publisher": "Sandisk Corporation Investor Relations",
        "url": "https://investor.sandisk.com/rss/news-releases.xml",
        "hosts": {"investor.sandisk.com", "www.sandisk.com"},
        "presentation_hub_url": "https://investor.sandisk.com/news-events/presentations",
        "technology_scope": ["NAND"],
    },
    "US.WDC": {
        "publisher": "Western Digital Corporation Investor Relations",
        "url": "https://investor.wdc.com/rss/news-releases.xml",
        "hosts": {"investor.wdc.com", "www.westerndigital.com"},
        "presentation_hub_url": "https://investor.wdc.com/financial-information/earnings-documents",
        "technology_scope": ["HDD"],
    },
    "US.STX": {
        "publisher": "Seagate Technology Investor Relations",
        "url": "https://investors.seagate.com/rss/pressrelease.aspx",
        "hosts": {"investors.seagate.com"},
        "presentation_hub_url": "https://investors.seagate.com/financials/quarterly-results/default.aspx",
        "technology_scope": ["HDD"],
    },
}
IR_MAX_RESPONSE_BYTES = 1_000_000

_FISCAL_QUARTERS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
}


class OfficialIrReleaseAdapter:
    """Fixed-domain, read-only RSS adapter for the four companies' IR releases."""

    def __init__(
        self,
        *,
        fetch_bytes: Callable[[str, set[str]], bytes] | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        cache_ttl_seconds: float = 300.0,
    ) -> None:
        self._fetch_bytes = fetch_bytes or self._default_fetch_bytes
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self.cache_ttl_seconds = max(60.0, float(cache_ttl_seconds))
        self._cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
        self._inflight: dict[tuple[str, int], Future[dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def status(self) -> dict[str, Any]:
        return {
            "source": "official_company_ir",
            "configured": True,
            "state": "available",
            "feed_count": len(IR_FEEDS),
            "allowed_symbols": list(STORAGE_SYMBOLS),
            "source_type": "company_ir",
            "source_tier": "primary",
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

    @staticmethod
    def _normalize_symbols(symbols: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        requested: list[str] = []
        for raw_symbol in symbols:
            symbol = str(raw_symbol or "").strip().upper()
            if symbol and symbol not in requested:
                requested.append(symbol)
        unsupported = [symbol for symbol in requested if symbol not in IR_FEEDS]
        if unsupported:
            raise ValueError(f"不在官方 IR 源白名单：{', '.join(unsupported)}")
        if not requested:
            raise ValueError("至少需要一个官方 IR 标的代码")
        return tuple(requested)

    def recent_releases_batch(
        self,
        symbols: tuple[str, ...] | list[str] = STORAGE_SYMBOLS,
        *,
        limit: int = 8,
        force: bool = False,
    ) -> dict[str, Any]:
        requested = self._normalize_symbols(symbols)
        safe_limit = min(20, max(1, int(limit)))
        captured_at = self._clock().astimezone(timezone.utc)
        result: dict[str, Any] = {
            "ok": False,
            "source": "official_company_ir",
            "source_type": "company_ir",
            "source_tier": "primary",
            "captured_at": _utc_iso(captured_at),
            "symbols": list(requested),
            "rows": [],
            "source_errors": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
        }
        if len(requested) == 1:
            outcomes = [self._release_outcome(requested[0], safe_limit, captured_at, force=force)]
        else:
            with ThreadPoolExecutor(
                max_workers=min(4, len(requested)),
                thread_name_prefix="company-ir-symbol",
            ) as executor:
                futures = [
                    executor.submit(
                        self._release_outcome,
                        symbol,
                        safe_limit,
                        captured_at,
                        force=force,
                    )
                    for symbol in requested
                ]
                outcomes = [future.result() for future in futures]
        for outcome in outcomes:
            row = outcome.get("row")
            if row:
                result["rows"].append(row)
            result["source_errors"].extend(copy.deepcopy(outcome.get("source_errors") or []))
        result["ok"] = bool(result["rows"])
        return result

    def _release_outcome(
        self,
        symbol: str,
        safe_limit: int,
        captured_at: datetime,
        *,
        force: bool,
    ) -> dict[str, Any]:
        cache_key = (symbol, safe_limit)
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached[0] > self._monotonic() and not force:
                return self._annotate_outcome(cached[1], cache_hit=True, singleflight_shared=False)
            future = self._inflight.get(cache_key)
            owner = future is None
            if future is None:
                future = Future()
                self._inflight[cache_key] = future
        if not owner:
            return self._annotate_outcome(
                future.result(),
                cache_hit=False,
                singleflight_shared=True,
            )
        try:
            outcome = self._fetch_release_outcome(symbol, safe_limit, captured_at)
            cache_ttl = (
                self.cache_ttl_seconds
                if outcome.get("row")
                else min(self.cache_ttl_seconds, 60.0)
            )
            with self._lock:
                self._cache[cache_key] = (
                    self._monotonic() + cache_ttl,
                    copy.deepcopy(outcome),
                )
            future.set_result(copy.deepcopy(outcome))
            return outcome
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                if self._inflight.get(cache_key) is future:
                    self._inflight.pop(cache_key, None)

    @staticmethod
    def _annotate_outcome(
        outcome: dict[str, Any],
        *,
        cache_hit: bool,
        singleflight_shared: bool,
    ) -> dict[str, Any]:
        annotated = copy.deepcopy(outcome)
        row = annotated.get("row")
        if row:
            row["cache_hit"] = cache_hit
            row["singleflight_shared"] = singleflight_shared
        for error in annotated.get("source_errors") or []:
            error["cache_hit"] = cache_hit
            error["singleflight_shared"] = singleflight_shared
        return annotated

    def _fetch_release_outcome(
        self,
        symbol: str,
        safe_limit: int,
        captured_at: datetime,
    ) -> dict[str, Any]:
        config = IR_FEEDS[symbol]
        try:
            raw = self._fetch_bytes(str(config["url"]), set(config["hosts"]))
            releases = self._parse_rss(
                raw,
                symbol=symbol,
                allowed_hosts=set(config["hosts"]),
                captured_at=captured_at,
                limit=safe_limit,
                presentation_hub_url=str(config["presentation_hub_url"]),
                technology_scope=list(config["technology_scope"]),
            )
        except Exception as exc:
            return {
                "row": None,
                "source_errors": [{
                    "source": "official_company_ir",
                    "symbol": symbol,
                    "code": "IR_FEED_ERROR",
                    "message": str(exc)[:300],
                    "cache_hit": False,
                    "singleflight_shared": False,
                }],
            }
        if not releases:
            return {
                "row": None,
                "source_errors": [{
                    "source": "official_company_ir",
                    "symbol": symbol,
                    "code": "IR_RELEASES_EMPTY",
                    "message": "官方 IR RSS 没有返回可用且不晚于当前时点的新闻稿",
                    "cache_hit": False,
                    "singleflight_shared": False,
                }],
            }
        return {
            "row": {
                "symbol": symbol,
                "publisher": config["publisher"],
                "feed_url": config["url"],
                "presentation_hub_url": config["presentation_hub_url"],
                "technology_scope": list(config["technology_scope"]),
                "quality": "ready",
                "cache_hit": False,
                "singleflight_shared": False,
                "release_count": len(releases),
                "releases": releases,
            },
            "source_errors": [],
        }

    @staticmethod
    def _default_fetch_bytes(url: str, allowed_hosts: set[str]) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
            raise ValueError("官方 IR 适配器拒绝非固定 HTTPS 端点")
        request = Request(url, headers={
            "User-Agent": "AI-Collaboration-Studio/0.1 local-read-only-research",
            "Accept": "application/rss+xml, application/xml, text/xml",
        })
        with urlopen(request, timeout=12) as response:
            final_url = str(response.geturl() or "")
            final = urlparse(final_url)
            if final.scheme != "https" or final.hostname not in allowed_hosts:
                raise ValueError("官方 IR RSS 重定向到了非白名单端点")
            declared_length = response.headers.get("Content-Length")
            if declared_length and int(declared_length) > IR_MAX_RESPONSE_BYTES:
                raise ValueError("官方 IR RSS 超过 1 MB 上限")
            raw = response.read(IR_MAX_RESPONSE_BYTES + 1)
        if len(raw) > IR_MAX_RESPONSE_BYTES:
            raise ValueError("官方 IR RSS 超过 1 MB 上限")
        return raw

    @staticmethod
    def _parse_rss(
        raw: bytes,
        *,
        symbol: str,
        allowed_hosts: set[str],
        captured_at: datetime,
        limit: int,
        presentation_hub_url: str,
        technology_scope: list[str],
    ) -> list[dict[str, Any]]:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise ValueError(f"官方 IR RSS XML 无法解析：{exc}") from exc
        releases: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in root.findall(".//item"):
            title = OfficialIrReleaseAdapter._node_text(item, "title")[:300]
            link = OfficialIrReleaseAdapter._safe_official_link(
                OfficialIrReleaseAdapter._node_text(item, "link"),
                allowed_hosts,
            )
            published_raw = OfficialIrReleaseAdapter._node_text(item, "pubDate")
            try:
                published = parsedate_to_datetime(published_raw)
            except (TypeError, ValueError, OverflowError):
                continue
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            published = published.astimezone(timezone.utc)
            if not title or not link or published > captured_at:
                continue
            dedupe_key = f"{title.casefold()}|{link}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            description = OfficialIrReleaseAdapter._plain_text(
                OfficialIrReleaseAdapter._node_text(item, "description")
            )[:800]
            event_type = OfficialIrReleaseAdapter._classify_event(title, description)
            fiscal_period = OfficialIrReleaseAdapter._fiscal_period(title)
            releases.append({
                "title": title,
                "published_at": _utc_iso(published),
                "published_date": published.date().isoformat(),
                "official_url": link,
                "summary": description,
                "source_type": "company_ir",
                "source_tier": "primary",
                "event_type": event_type,
                "claim_status": "company_statement",
                "technology_scope": list(technology_scope),
                "presentation_hub_url": presentation_hub_url,
                "presentation_discovery_status": "hub_only",
                "source_locator": "IR RSS title and summary",
                "fiscal_period": fiscal_period.get("fiscal_period"),
                "fiscal_year": fiscal_period.get("fiscal_year"),
                "fiscal_quarter": fiscal_period.get("fiscal_quarter"),
                "period_confidence": fiscal_period.get("period_confidence"),
                "includes_full_year": fiscal_period.get("includes_full_year", False),
                "symbol": symbol,
            })
        releases.sort(key=lambda release: release["published_at"], reverse=True)
        return releases[:limit]

    @staticmethod
    def _classify_event(title: str, summary: str = "") -> str:
        text = " ".join(f"{title} {summary}".casefold().split())
        if re.search(r"\b(?:to|will) report\b.*\b(?:financial )?results\b", text):
            return "earnings_schedule"
        if re.search(r"\b(?:announces?|sets?)\b.*\bearnings (?:call|date)\b", text):
            return "earnings_schedule"
        earnings_result_patterns = (
            r"\breports?\b.*\bfinancial results\b",
            r"\breports? results\b.*\bquarter\b",
            r"\bresults for (?:the )?.*\bquarter\b",
            r"\bquarterly results\b",
        )
        if any(re.search(pattern, text) for pattern in earnings_result_patterns):
            return "earnings_release"
        if "earnings presentation" in text or "quarterly presentation" in text:
            return "earnings_material"
        return "other"

    @staticmethod
    def _fiscal_period(title: str) -> dict[str, Any]:
        text = " ".join(str(title or "").casefold().replace("’", "'").split())
        match = re.search(r"\bq([1-4])\s*fy\s*'?([0-9]{2,4})\b", text)
        if match:
            quarter = int(match.group(1))
            year = OfficialIrReleaseAdapter._four_digit_year(match.group(2))
            return OfficialIrReleaseAdapter._period_payload(text, year, quarter)

        word_pattern = "|".join(_FISCAL_QUARTERS)
        patterns = (
            rf"\bfiscal\s+({word_pattern})\s+quarter(?:\s+and\s+fiscal\s+year|\s+of)?\s+(20\d{{2}})\b",
            rf"\b({word_pattern})\s+quarter(?:\s+of)?\s+fiscal\s+(20\d{{2}})\b",
            rf"\b({word_pattern})\s+quarter\s+fiscal\s+(20\d{{2}})\b",
            rf"\b(20\d{{2}})\s+fiscal\s+({word_pattern})\s+quarter\b",
        )
        for index, pattern in enumerate(patterns):
            match = re.search(pattern, text)
            if not match:
                continue
            if index == 3:
                year = int(match.group(1))
                quarter = _FISCAL_QUARTERS[match.group(2)]
            else:
                quarter = _FISCAL_QUARTERS[match.group(1)]
                year = int(match.group(2))
            return OfficialIrReleaseAdapter._period_payload(text, year, quarter)
        return {
            "fiscal_period": None,
            "fiscal_year": None,
            "fiscal_quarter": None,
            "period_confidence": "unknown",
            "includes_full_year": False,
        }

    @staticmethod
    def _four_digit_year(value: str) -> int:
        year = int(value)
        if len(value) == 2:
            return 2000 + year if year < 70 else 1900 + year
        return year

    @staticmethod
    def _period_payload(text: str, year: int, quarter: int) -> dict[str, Any]:
        return {
            "fiscal_period": f"FY{year}-Q{quarter}",
            "fiscal_year": year,
            "fiscal_quarter": quarter,
            "period_confidence": "title_derived",
            "includes_full_year": "fiscal year" in text and quarter == 4,
        }

    @staticmethod
    def _node_text(item: ET.Element, tag: str) -> str:
        node = item.find(tag)
        return str(node.text or "").strip() if node is not None else ""

    @staticmethod
    def _safe_official_link(value: str, allowed_hosts: set[str]) -> str:
        parsed = urlparse(str(value or "").strip())
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in allowed_hosts:
            return ""
        return urlunparse(("https", parsed.netloc, parsed.path, parsed.params, parsed.query, ""))

    @staticmethod
    def _plain_text(value: str) -> str:
        without_tags = re.sub(r"<[^>]+>", " ", str(value or ""))
        return " ".join(html.unescape(without_tags).split())
