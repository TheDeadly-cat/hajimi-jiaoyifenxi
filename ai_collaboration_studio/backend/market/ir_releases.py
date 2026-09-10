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
from types import MappingProxyType
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse
from urllib.request import Request

from .futu_readonly import STORAGE_SYMBOLS, _utc_iso
from ..source_poll_control import (
    SourcePollCancelled,
    SourcePollDeadlineExceeded,
    ensure_source_poll_active,
    wait_for_source_poll,
)
from .official_http import open_official_https, read_official_https_body
from .micron_ir_json import MICRON_IR_JSON_URL, MicronIrJsonClient


IR_FEEDS = MappingProxyType({
    "US.MU": MappingProxyType({
        "publisher": "Micron Technology Investor Relations",
        "url": "https://investors.micron.com/rss/news-releases.xml?items=30",
        "hosts": frozenset({"investors.micron.com"}),
        "presentation_hub_url": "https://investors.micron.com/quarterly-results",
        "technology_scope": ("DRAM", "NAND"),
    }),
    "US.SNDK": MappingProxyType({
        "publisher": "Sandisk Corporation Investor Relations",
        "url": "https://investor.sandisk.com/rss/news-releases.xml",
        "hosts": frozenset({"investor.sandisk.com", "www.sandisk.com"}),
        "presentation_hub_url": "https://investor.sandisk.com/news-events/presentations",
        "technology_scope": ("NAND",),
    }),
    "US.WDC": MappingProxyType({
        "publisher": "Western Digital Corporation Investor Relations",
        "url": "https://investor.wdc.com/rss/news-releases.xml",
        "hosts": frozenset({"investor.wdc.com", "www.westerndigital.com"}),
        "presentation_hub_url": "https://investor.wdc.com/financial-information/earnings-documents",
        "technology_scope": ("HDD",),
    }),
    "US.STX": MappingProxyType({
        "publisher": "Seagate Technology Investor Relations",
        "url": "https://investors.seagate.com/rss/pressrelease.aspx",
        "hosts": frozenset({"investors.seagate.com"}),
        "presentation_hub_url": "https://investors.seagate.com/financials/quarterly-results/default.aspx",
        "technology_scope": ("HDD",),
    }),
})
IR_MAX_RESPONSE_BYTES = 1_000_000
IR_MONITORING_FEED_SCOPE_VERSION = "company_ir_monitoring_feed_scope_v1"

_FISCAL_QUARTERS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
}


class OfficialIrReleaseAdapter:
    """Fixed-domain IR metadata: Micron Q4 JSON and the other companies' RSS."""

    def __init__(
        self,
        *,
        fetch_bytes: Callable[[str, set[str]], bytes] | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        cache_ttl_seconds: float = 300.0,
        source_format: str = "q4_json",
        micron_fetch_bytes: Callable[..., bytes] | None = None,
    ) -> None:
        if type(source_format) is not str or source_format not in {"q4_json", "rss"}:
            raise ValueError("source_format must be q4_json or explicit legacy rss")
        if source_format == "q4_json" and fetch_bytes is not None and micron_fetch_bytes is None:
            raise ValueError("injected RSS fetch requires explicit source_format='rss' or an injected Micron JSON transport")
        if source_format == "rss" and micron_fetch_bytes is not None:
            raise ValueError("Micron JSON transport is unused in explicit rss mode")
        self._source_format = source_format
        self._fetch_bytes = fetch_bytes or self._default_fetch_bytes
        self._fetch_bytes_is_default = fetch_bytes is None
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self._micron_client = (
            MicronIrJsonClient(fetch_bytes=micron_fetch_bytes, clock=self._clock, monotonic=self._monotonic)
            if source_format == "q4_json" else None
        )
        self.cache_ttl_seconds = max(60.0, float(cache_ttl_seconds))
        self._cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
        self._inflight: dict[tuple[Any, ...], Future[dict[str, Any]]] = {}
        self._lock = threading.RLock()

    @property
    def source_format(self) -> str:
        """The sealed Micron format; other configured publishers remain RSS."""
        return self._source_format

    def feed_url_for(self, symbol: str) -> str:
        return MICRON_IR_JSON_URL if symbol == "US.MU" and self.source_format == "q4_json" else str(IR_FEEDS[symbol]["url"])

    def status(self) -> dict[str, Any]:
        return {
            "source": "official_company_ir",
            "configured": True,
            "state": "available",
            "feed_count": len(IR_FEEDS),
            "allowed_symbols": list(STORAGE_SYMBOLS),
            "micron_source_format": self.source_format,
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
        deadline_monotonic_ms: int = 0,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Return the legacy title-and-URL-deduplicated release view."""

        return self._recent_releases_batch(
            symbols,
            limit=limit,
            force=force,
            monitoring_raw_items=False,
            deadline_monotonic_ms=deadline_monotonic_ms,
            cancel_event=cancel_event,
        )

    def monitoring_releases_batch(
        self,
        symbols: tuple[str, ...] | list[str] = STORAGE_SYMBOLS,
        *,
        limit: int = 8,
        force: bool = False,
        deadline_monotonic_ms: int = 0,
        cancel_event: threading.Event | None = None,
        require_complete_metadata: bool = True,
    ) -> dict[str, Any]:
        """Return validated metadata, preserving incomplete identity evidence."""

        if type(require_complete_metadata) is not bool:
            raise ValueError("require_complete_metadata must be a native boolean")

        return self._recent_releases_batch(
            symbols,
            limit=limit,
            force=force,
            monitoring_raw_items=True,
            deadline_monotonic_ms=deadline_monotonic_ms,
            cancel_event=cancel_event,
            require_complete_metadata=require_complete_metadata,
        )

    def _recent_releases_batch(
        self,
        symbols: tuple[str, ...] | list[str],
        *,
        limit: int,
        force: bool,
        monitoring_raw_items: bool,
        deadline_monotonic_ms: int,
        cancel_event: threading.Event | None,
        require_complete_metadata: bool = True,
    ) -> dict[str, Any]:
        ensure_source_poll_active(
            deadline_monotonic_ms=deadline_monotonic_ms,
            cancel_event=cancel_event,
        )
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
        if len(requested) == 1 or deadline_monotonic_ms or cancel_event is not None:
            outcomes = []
            for symbol in requested:
                ensure_source_poll_active(
                    deadline_monotonic_ms=deadline_monotonic_ms,
                    cancel_event=cancel_event,
                )
                outcomes.append(self._release_outcome(
                    symbol,
                    safe_limit,
                    captured_at,
                    force=force,
                    monitoring_raw_items=monitoring_raw_items,
                    deadline_monotonic_ms=deadline_monotonic_ms,
                    cancel_event=cancel_event,
                    require_complete_metadata=require_complete_metadata,
                ))
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
                        monitoring_raw_items=monitoring_raw_items,
                        deadline_monotonic_ms=0,
                        cancel_event=None,
                        require_complete_metadata=require_complete_metadata,
                    )
                    for symbol in requested
                ]
                outcomes = [future.result() for future in futures]
        for outcome in outcomes:
            row = outcome.get("row")
            if row:
                result["rows"].append(row)
            result["source_errors"].extend(copy.deepcopy(outcome.get("source_errors") or []))
        if monitoring_raw_items:
            result["monitoring_feed_scope_version"] = IR_MONITORING_FEED_SCOPE_VERSION
            result["monitoring_feed_scope_complete"] = (
                not result["source_errors"]
                and [row["symbol"] for row in result["rows"]] == list(requested)
                and all(row.get("feed_scope_complete") is True for row in result["rows"])
            )
        result["ok"] = bool(result["rows"])
        return result

    def _release_outcome(
        self,
        symbol: str,
        safe_limit: int,
        captured_at: datetime,
        *,
        force: bool,
        monitoring_raw_items: bool,
        deadline_monotonic_ms: int,
        cancel_event: threading.Event | None,
        require_complete_metadata: bool = True,
    ) -> dict[str, Any]:
        incremental_json = symbol == "US.MU" and self.source_format == "q4_json" and monitoring_raw_items
        cache_key = (symbol, safe_limit, monitoring_raw_items) + ((require_complete_metadata,) if incremental_json else ())
        with self._lock:
            cached = self._cache.get(cache_key)
            # Monitoring always reads the current Micron list. Only independently
            # verified per-release head metadata can be reused by that client.
            if cached and cached[0] > self._monotonic() and not force and not incremental_json:
                return self._annotate_outcome(cached[1], cache_hit=True, singleflight_shared=False)
            future = self._inflight.get(cache_key)
            owner = future is None
            if future is None:
                future = Future()
                self._inflight[cache_key] = future
        if not owner:
            if deadline_monotonic_ms or cancel_event is not None:
                while not future.done():
                    wait_for_source_poll(
                        0.025,
                        deadline_monotonic_ms=deadline_monotonic_ms,
                        cancel_event=cancel_event,
                    )
            return self._annotate_outcome(
                future.result(),
                cache_hit=False,
                singleflight_shared=True,
            )
        try:
            outcome = self._fetch_release_outcome(
                symbol,
                safe_limit,
                captured_at,
                monitoring_raw_items=monitoring_raw_items,
                deadline_monotonic_ms=deadline_monotonic_ms,
                cancel_event=cancel_event,
                require_complete_metadata=require_complete_metadata,
            )
            cache_ttl = (
                self.cache_ttl_seconds
                if outcome.get("row")
                else min(self.cache_ttl_seconds, 60.0)
            )
            if not incremental_json:
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
        *,
        monitoring_raw_items: bool,
        deadline_monotonic_ms: int,
        cancel_event: threading.Event | None,
        require_complete_metadata: bool = True,
    ) -> dict[str, Any]:
        config = IR_FEEDS[symbol]
        scope_metadata: dict[str, Any] = {}
        json_source = symbol == "US.MU" and self.source_format == "q4_json"
        json_errors = []
        try:
            ensure_source_poll_active(
                deadline_monotonic_ms=deadline_monotonic_ms,
                cancel_event=cancel_event,
            )
            if json_source:
                snapshot = self._micron_client.read_recent(
                    require_complete=require_complete_metadata,
                    deadline_monotonic_ms=deadline_monotonic_ms,
                    cancel_event=cancel_event,
                )
                releases = []
                for raw_release in snapshot["releases"]:
                    title, summary = raw_release["title"], raw_release["summary"]
                    releases.append({
                        **raw_release,
                        "event_type": self._classify_event(title, summary),
                        **self._fiscal_period(title),
                        "published_date": raw_release["published_at"][:10],
                        "source_type": "company_ir", "source_tier": "primary",
                        "claim_status": "company_statement", "symbol": symbol,
                        "technology_scope": list(config["technology_scope"]),
                        "presentation_hub_url": str(config["presentation_hub_url"]),
                        "presentation_discovery_status": "hub_only",
                        "source_locator": "Q4 public JSON and bound NewsArticle head metadata",
                    })
                scope_metadata["complete"] = snapshot["complete"] is True
                if "metadata_progress" in snapshot:
                    scope_metadata["metadata_progress"] = copy.deepcopy(snapshot["metadata_progress"])
                json_errors = [{
                    **error, "source": "official_company_ir", "symbol": symbol,
                    "cache_hit": False, "singleflight_shared": False,
                } for error in snapshot.get("source_errors", [])]
                if not monitoring_raw_items:
                    releases = releases[:safe_limit]
            elif self._fetch_bytes_is_default:
                raw = self._fetch_bytes(
                    str(config["url"]),
                    set(config["hosts"]),
                    deadline_monotonic_ms=deadline_monotonic_ms,
                    cancel_event=cancel_event,
                )
            else:
                raw = self._fetch_bytes(str(config["url"]), set(config["hosts"]))
            ensure_source_poll_active(
                deadline_monotonic_ms=deadline_monotonic_ms,
                cancel_event=cancel_event,
            )
            if not json_source:
                releases = self._parse_rss(
                    raw,
                    symbol=symbol,
                    allowed_hosts=set(config["hosts"]),
                    captured_at=captured_at,
                    limit=None if monitoring_raw_items else safe_limit,
                    presentation_hub_url=str(config["presentation_hub_url"]),
                    technology_scope=list(config["technology_scope"]),
                    deduplicate_legacy=not monitoring_raw_items,
                    scope_metadata=scope_metadata if monitoring_raw_items else None,
                )
        except (SourcePollCancelled, SourcePollDeadlineExceeded):
            raise
        except Exception as exc:
            return {
                "row": None,
                "source_errors": [{
                    "source": "official_company_ir",
                    "symbol": symbol,
                    "code": getattr(exc, "code", "IR_FEED_ERROR") if json_source else "IR_FEED_ERROR",
                    "message": str(exc)[:300],
                    "cache_hit": False,
                    "singleflight_shared": False,
                }],
            }
        if not releases and not (monitoring_raw_items and (scope_metadata.get("complete") is True or json_errors)):
            return {
                "row": None,
                "source_errors": [{
                    "source": "official_company_ir",
                    "symbol": symbol,
                    "code": "IR_RELEASES_EMPTY",
                    "message": "官方 IR 来源没有返回可用的新闻稿元数据",
                    "cache_hit": False,
                    "singleflight_shared": False,
                }],
            }
        return {
            "row": {
                "symbol": symbol,
                "publisher": config["publisher"],
                "feed_url": self.feed_url_for(symbol),
                "source_format": "micron_q4_public_json_v1" if json_source else "rss",
                "presentation_hub_url": config["presentation_hub_url"],
                "technology_scope": list(config["technology_scope"]),
                "quality": "degraded" if json_errors else "ready",
                "cache_hit": False,
                "singleflight_shared": False,
                "release_count": len(releases),
                "releases": releases,
                **({"feed_scope_complete": scope_metadata.get("complete") is True} if monitoring_raw_items else {}),
                **({"metadata_progress": scope_metadata["metadata_progress"]} if "metadata_progress" in scope_metadata else {}),
            },
            "source_errors": json_errors,
        }

    @staticmethod
    def _default_fetch_bytes(
        url: str,
        allowed_hosts: set[str],
        *,
        deadline_monotonic_ms: int = 0,
        cancel_event: threading.Event | None = None,
    ) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
            raise ValueError("官方 IR 适配器拒绝非固定 HTTPS 端点")
        request = Request(url, headers={
            "User-Agent": "AI-Collaboration-Studio/0.1 local-read-only-research",
            "Accept": "application/rss+xml, application/xml, text/xml",
        })
        with open_official_https(
            request,
            allowed_hosts=allowed_hosts,
            timeout=12,
            deadline_monotonic_ms=deadline_monotonic_ms,
            cancel_event=cancel_event,
        ) as response:
            declared_length = response.headers.get("Content-Length")
            if declared_length and int(declared_length) > IR_MAX_RESPONSE_BYTES:
                raise ValueError("官方 IR RSS 超过 1 MB 上限")
            try:
                raw = read_official_https_body(
                    response,
                    IR_MAX_RESPONSE_BYTES,
                    deadline_seconds=12,
                    deadline_monotonic_ms=deadline_monotonic_ms,
                    cancel_event=cancel_event,
                )
            except (SourcePollCancelled, SourcePollDeadlineExceeded):
                raise
            except ValueError as exc:
                raise ValueError("官方 IR RSS 超过 1 MB 上限") from exc
        return raw

    @staticmethod
    def _parse_rss(
        raw: bytes,
        *,
        symbol: str,
        allowed_hosts: set[str],
        captured_at: datetime,
        limit: int | None,
        presentation_hub_url: str,
        technology_scope: list[str],
        deduplicate_legacy: bool = True,
        scope_metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise ValueError(f"官方 IR RSS XML 无法解析：{exc}") from exc
        releases: list[dict[str, Any]] = []
        seen: set[str] = set()
        raw_items = root.findall(".//item")
        for item in raw_items:
            title = OfficialIrReleaseAdapter._node_text(item, "title")[:300]
            guid = OfficialIrReleaseAdapter._node_text(item, "guid")[:1_000]
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
            if deduplicate_legacy:
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
                "guid": guid,
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
        if scope_metadata is not None:
            scope_metadata["complete"] = (
                root.tag == "rss"
                and len(root.findall("./channel")) == 1
                and len(root.findall("./channel/item")) == len(raw_items)
                and len(releases) == len(raw_items)
                and limit is None
                and not deduplicate_legacy
            )
        return releases if limit is None else releases[:limit]

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
