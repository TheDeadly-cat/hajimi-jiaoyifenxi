from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from .futu_readonly import STORAGE_SYMBOLS, _utc_iso
from .ir_releases import IR_FEEDS, OfficialIrReleaseAdapter


EARNINGS_MATERIAL_MAX_RESPONSE_BYTES = 2_000_000
CURATED_MATERIAL_MAX_AGE_DAYS = 45
EARNINGS_MATERIAL_LINK_HOSTS = {
    "US.MU": {"investors.micron.com"},
    "US.SNDK": {"investor.sandisk.com"},
    "US.WDC": {"investor.wdc.com"},
    "US.STX": {"investors.seagate.com", "s24.q4cdn.com"},
}

CURATED_OFFICIAL_MATERIALS = {
    "US.MU": [
        {
            "fiscal_period": "FY2026-Q3",
            "material_kind": "earnings_presentation",
            "title": "FQ3 2026 Financial Results Presentation",
            "official_url": "https://investors.micron.com/static-files/2354ecda-77a0-4ddd-8462-a631eb491356",
        },
        {
            "fiscal_period": "FY2026-Q3",
            "material_kind": "prepared_remarks",
            "title": "Fiscal Q3 2026 Earnings Call Prepared Remarks",
            "official_url": "https://investors.micron.com/static-files/631b1a32-5537-46ae-8f40-82e42fc79dfe",
        },
    ],
    "US.SNDK": [{
        "fiscal_period": "FY2026-Q3",
        "material_kind": "earnings_presentation",
        "title": "Q3FY26 Earnings Presentation",
        "official_url": "https://investor.sandisk.com/static-files/8ea78860-f8e5-4f1c-ada3-c554437d6281",
    }],
    "US.WDC": [{
        "fiscal_period": "FY2026-Q3",
        "material_kind": "earnings_presentation",
        "title": "Third Quarter Fiscal 2026 Earnings Presentation",
        "official_url": "https://investor.wdc.com/static-files/5b2d41c1-7d45-4575-b9ea-c51424dbffeb",
    }],
    "US.STX": [
        {
            "fiscal_period": "FY2026-Q4",
            "material_kind": "earnings_release",
            "title": "STX FQ4 2026 Earnings Release",
            "official_url": "https://s24.q4cdn.com/101481333/files/doc_financials/2026/q4/STX-FQ4-26-Press-Release.pdf",
        },
        {
            "fiscal_period": "FY2026-Q4",
            "material_kind": "supplemental_financial_information",
            "title": "STX FQ4 2026 Supplemental Financial Information",
            "official_url": "https://s24.q4cdn.com/101481333/files/doc_financials/2026/q4/v2/STX-FQ4-26-Supplemental.pdf",
        },
        {
            "fiscal_period": "FY2026-Q4",
            "material_kind": "corrected_transcript",
            "title": "STX FQ4 2026 Earnings Call Corrected Transcript",
            "official_url": "https://s24.q4cdn.com/101481333/files/doc_financials/2026/q4/STX-US-CORRECTED-TRANSCRIPT-Seagate-Technology-Holdings-PlcSTXUS-Q4-2026-Earnings-Call-28-ET-PM-27-07-2026-2026-07-27-0.pdf",
        },
        {
            "fiscal_period": "FY2026-Q3",
            "material_kind": "supplemental_financial_information",
            "title": "STX FQ3 2026 Supplemental Financial Information",
            "official_url": "https://s24.q4cdn.com/101481333/files/doc_financials/2026/q3/STX-FQ3-26-Supplemental.pdf",
        },
    ],
}
CURATED_MATERIALS_VERIFIED_AT = "2026-08-02"
CURATED_MATERIALS_VALID_UNTIL = (
    datetime.fromisoformat(CURATED_MATERIALS_VERIFIED_AT)
    + timedelta(days=CURATED_MATERIAL_MAX_AGE_DAYS)
).date().isoformat()


def curated_official_material_candidate(
    symbol: str,
    official_url: str,
) -> dict[str, Any] | None:
    """Return one exact built-in earnings-material candidate without networking.

    A matching publisher host is deliberately insufficient.  Manual evidence may
    only cover a candidate whose complete HTTPS URL is already present in the
    reviewed built-in catalog.
    """

    normalized_symbol = str(symbol or "").strip().upper()
    normalized_url = str(official_url or "").strip()
    if normalized_symbol not in EARNINGS_MATERIAL_LINK_HOSTS or not normalized_url:
        return None
    parsed = urlparse(normalized_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in EARNINGS_MATERIAL_LINK_HOSTS[normalized_symbol]
        or parsed.fragment
    ):
        return None
    canonical_url = urlunparse(("https", parsed.netloc, parsed.path, parsed.params, parsed.query, ""))
    for raw in CURATED_OFFICIAL_MATERIALS.get(normalized_symbol, []):
        if str(raw.get("official_url") or "") != canonical_url:
            continue
        fiscal_period = str(raw.get("fiscal_period") or "")
        match = re.fullmatch(r"FY(20\d{2})-Q([1-4])", fiscal_period)
        if not match:
            return None
        candidate = {
            "version": "curated_official_material_candidate_v1",
            "symbol": normalized_symbol,
            "fiscal_period": fiscal_period,
            "fiscal_year": int(match.group(1)),
            "fiscal_quarter": int(match.group(2)),
            "material_kind": str(raw.get("material_kind") or ""),
            "title": str(raw.get("title") or "")[:300],
            "official_url": canonical_url,
            "verified_at": CURATED_MATERIALS_VERIFIED_AT,
            "valid_until": CURATED_MATERIALS_VALID_UNTIL,
            "source_type": "company_ir",
            "source_tier": "primary",
        }
        candidate["candidate_sha256"] = curated_candidate_sha256(candidate)
        return candidate
    return None


def curated_candidate_sha256(candidate: dict[str, Any]) -> str:
    canonical = {
        key: candidate.get(key)
        for key in (
            "version",
            "symbol",
            "fiscal_period",
            "fiscal_year",
            "fiscal_quarter",
            "material_kind",
            "title",
            "official_url",
            "verified_at",
            "valid_until",
            "source_type",
            "source_tier",
        )
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class _AnchorSequenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[dict[str, str]] = []
        self._anchor_href = ""
        self._anchor_label = ""
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a" or self._anchor_href:
            return
        attributes = {str(key).casefold(): str(value or "") for key, value in attrs}
        self._anchor_href = attributes.get("href", "")
        self._anchor_label = attributes.get("aria-label", "") or attributes.get("title", "")
        self._anchor_text = []

    def handle_data(self, data: str) -> None:
        text = " ".join(str(data or "").split())
        if not text:
            return
        if self._anchor_href:
            self._anchor_text.append(text)
        else:
            self.tokens.append({"type": "text", "text": text[:300]})

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "a" or not self._anchor_href:
            return
        self.tokens.append({
            "type": "anchor",
            "text": (" ".join(self._anchor_text) or self._anchor_label)[:300],
            "href": self._anchor_href[:2000],
        })
        self._anchor_href = ""
        self._anchor_label = ""
        self._anchor_text = []


class OfficialEarningsMaterialsAdapter:
    """Read-only discovery of earnings presentation links on fixed official IR hubs."""

    def __init__(
        self,
        *,
        fetch_bytes: Callable[[str, set[str]], bytes] | None = None,
        probe_material: Callable[[str, set[str]], dict[str, Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        cache_ttl_seconds: float = 900.0,
    ) -> None:
        self._fetch_bytes = fetch_bytes or self._default_fetch_bytes
        self._probe_material = probe_material or self._default_probe_material
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self.cache_ttl_seconds = max(60.0, float(cache_ttl_seconds))
        self._cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
        self._inflight: dict[tuple[str, int], Future[dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def status(self) -> dict[str, Any]:
        return {
            "source": "official_company_ir_materials",
            "configured": True,
            "state": "available",
            "allowed_symbols": list(STORAGE_SYMBOLS),
            "source_type": "earnings_material_index",
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
            raise ValueError(f"不在官方业绩材料源白名单：{', '.join(unsupported)}")
        if not requested:
            raise ValueError("至少需要一个官方业绩材料标的代码")
        return tuple(requested)

    def recent_materials_batch(
        self,
        symbols: tuple[str, ...] | list[str] = STORAGE_SYMBOLS,
        *,
        limit: int = 24,
        force: bool = False,
    ) -> dict[str, Any]:
        requested = self._normalize_symbols(symbols)
        safe_limit = min(80, max(1, int(limit)))
        captured_at = self._clock().astimezone(timezone.utc)
        payload: dict[str, Any] = {
            "ok": False,
            "state": "empty",
            "source": "official_company_ir_materials",
            "source_type": "earnings_material_index",
            "source_tier": "primary",
            "captured_at": _utc_iso(captured_at),
            "symbols": list(requested),
            "rows": [],
            "source_errors": [],
            "source_warnings": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
        }
        rows = self._material_rows(
            requested,
            safe_limit,
            captured_at,
            force=force,
        )
        while True:
            snapshot_at = self._clock().astimezone(timezone.utc)
            invalid_indexes = [
                index
                for index, row in enumerate(rows)
                if not self._row_catalog_still_valid(row, snapshot_at)
            ]
            if not invalid_indexes:
                break
            refreshed_rows = self._material_rows(
                tuple(requested[index] for index in invalid_indexes),
                safe_limit,
                snapshot_at,
                # A batch-consistency refresh can reuse a valid cache or a
                # refresh already in flight; it is not a second caller force.
                force=False,
            )
            for index, refreshed_row in zip(invalid_indexes, refreshed_rows):
                rows[index] = refreshed_row
        total_materials = 0
        for row in rows:
            payload["source_errors"].extend(copy.deepcopy(row.get("source_errors") or []))
            payload["source_warnings"].extend(copy.deepcopy(row.get("source_warnings") or []))
            payload["rows"].append(row)
            total_materials += int(row.get("material_count") or 0)
        # Publish the exact instant against which every accepted curated item
        # above was revalidated. This keeps the batch timestamp and evidence
        # validity consistent even when one company fetch crosses midnight.
        payload["captured_at"] = _utc_iso(snapshot_at)
        payload["ok"] = total_materials > 0
        symbols_with_materials = sum(1 for row in payload["rows"] if row.get("materials"))
        payload["state"] = (
            "ready"
            if payload["rows"] and symbols_with_materials == len(payload["rows"]) and not payload["source_errors"]
            else "partial" if total_materials else "empty"
        )
        return payload

    def _material_rows(
        self,
        requested: tuple[str, ...],
        safe_limit: int,
        captured_at: datetime,
        *,
        force: bool,
    ) -> list[dict[str, Any]]:
        if len(requested) == 1:
            return [
                self._material_row(
                    requested[0],
                    safe_limit,
                    captured_at,
                    force=force,
                )
            ]
        with ThreadPoolExecutor(
            max_workers=min(4, len(requested)),
            thread_name_prefix="earnings-material-symbol",
        ) as executor:
            futures = [
                executor.submit(
                    self._material_row,
                    symbol,
                    safe_limit,
                    captured_at,
                    force=force,
                )
                for symbol in requested
            ]
            return [future.result() for future in futures]

    def _material_row(
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
            if (
                cached
                and cached[0] > self._monotonic()
                and not force
                and self._row_catalog_still_valid(cached[1], captured_at)
            ):
                row = copy.deepcopy(cached[1])
                row["cache_hit"] = True
                row["singleflight_shared"] = False
                return row
            future = self._inflight.get(cache_key)
            owner = future is None
            if future is None:
                future = Future()
                self._inflight[cache_key] = future
        if not owner:
            row = copy.deepcopy(future.result())
            if not self._row_catalog_still_valid(row, captured_at):
                with self._lock:
                    if self._inflight.get(cache_key) is future and future.done():
                        self._inflight.pop(cache_key, None)
                return self._material_row(
                    symbol,
                    safe_limit,
                    captured_at,
                    # This is an internal validity retry, not a new caller
                    # force request. Re-check cache/in-flight state so lagging
                    # followers can reuse a refresh another follower finished.
                    force=False,
                )
            row["cache_hit"] = False
            row["singleflight_shared"] = True
            return row
        try:
            row = self._fetch_material_row(symbol, safe_limit, captured_at)
            completed_at = self._clock().astimezone(timezone.utc)
            if not self._row_catalog_still_valid(row, completed_at):
                row = self._fetch_material_row(symbol, safe_limit, completed_at)
                completed_at = self._clock().astimezone(timezone.utc)
            cache_ttl = self._cache_ttl_for_row(row, completed_at)
            with self._lock:
                self._cache[cache_key] = (
                    self._monotonic() + cache_ttl,
                    copy.deepcopy(row),
                )
            future.set_result(copy.deepcopy(row))
            return row
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                if self._inflight.get(cache_key) is future:
                    self._inflight.pop(cache_key, None)

    @staticmethod
    def _row_catalog_still_valid(row: dict[str, Any], captured_at: datetime) -> bool:
        for material in row.get("materials") or []:
            if (
                material.get("discovery_method") != "curated_verified"
                or material.get("access_state") != "fetchable"
            ):
                continue
            try:
                valid_through = datetime.fromisoformat(
                    str(material.get("valid_until") or "")
                ).date()
            except ValueError:
                return False
            if captured_at.date() > valid_through:
                return False
        return True

    def _cache_ttl_for_row(self, row: dict[str, Any], completed_at: datetime) -> float:
        ttl = (
            self.cache_ttl_seconds
            if row.get("quality") == "ready"
            else min(self.cache_ttl_seconds, 120.0)
        )
        for material in row.get("materials") or []:
            if (
                material.get("discovery_method") != "curated_verified"
                or material.get("access_state") != "fetchable"
            ):
                continue
            try:
                valid_through = datetime.fromisoformat(str(material.get("valid_until") or ""))
            except ValueError:
                return 0.0
            expires_at = valid_through.replace(tzinfo=timezone.utc) + timedelta(days=1)
            ttl = min(ttl, max(0.0, (expires_at - completed_at).total_seconds()))
        return max(0.0, ttl)

    def _fetch_material_row(
        self,
        symbol: str,
        safe_limit: int,
        captured_at: datetime,
    ) -> dict[str, Any]:
        config = IR_FEEDS[symbol]
        hub_url = str(config["presentation_hub_url"])
        fetch_hosts = {str(urlparse(hub_url).hostname or "")}
        hub_error = ""
        row_errors: list[dict[str, Any]] = []
        row_warnings: list[dict[str, Any]] = []
        try:
            raw = self._fetch_bytes(hub_url, fetch_hosts)
            materials = self._parse_material_links(
                raw,
                symbol=symbol,
                hub_url=hub_url,
                allowed_link_hosts=EARNINGS_MATERIAL_LINK_HOSTS[symbol],
                limit=safe_limit,
            )
        except Exception as exc:
            materials = []
            hub_error = str(exc)[:300]
        live_materials = materials
        live_material_count = len(live_materials)
        merged_materials = self._merge_curated_materials(symbol, hub_url, live_materials)
        curated_materials = [
            material for material in merged_materials
            if material.get("discovery_method") == "curated_verified"
        ]
        curated_candidate_count = len(curated_materials)
        probe_errors: list[dict[str, Any]] = []
        if curated_materials:
            probe_errors = self._probe_curated_materials(
                symbol,
                curated_materials,
                captured_at=captured_at,
            )
        accepted_curated_materials = [
            material
            for material in curated_materials
            if material.get("access_state") == "fetchable"
        ]
        rejected_curated_materials = [
            material
            for material in curated_materials
            if material.get("access_state") != "fetchable"
        ]
        curated_access_ready = bool(curated_materials) and not probe_errors and (
            len(accepted_curated_materials) == len(curated_materials)
        )
        if probe_errors:
            if live_material_count:
                for issue in probe_errors:
                    warning = copy.deepcopy(issue)
                    warning["severity"] = "warning"
                    warning["excluded_from_evidence"] = True
                    row_warnings.append(warning)
            else:
                row_errors.extend(probe_errors)
        materials = [*live_materials, *accepted_curated_materials][:safe_limit]
        if hub_error:
            hub_issue = {
                "source": "official_company_ir_materials",
                "symbol": symbol,
                "code": (
                    "EARNINGS_MATERIAL_HUB_ANTIBOT"
                    if "反自动化验证页" in hub_error
                    else "EARNINGS_MATERIAL_HUB_TIMEOUT"
                    if "timed out" in hub_error.casefold()
                    else "EARNINGS_MATERIAL_HUB_ERROR"
                ),
                "message": hub_error,
            }
            if curated_access_ready:
                hub_issue["severity"] = "warning"
                hub_issue["covered_by"] = "direct_official_material_probe"
                row_warnings.append(hub_issue)
            else:
                row_errors.insert(0, hub_issue)
        row_ready = bool(materials) and (bool(live_material_count) or curated_access_ready) and not row_errors
        row = {
            "symbol": symbol,
            "publisher": config["publisher"],
            "hub_url": hub_url,
            "technology_scope": list(config["technology_scope"]),
            "quality": "ready" if row_ready else "limited",
            "discovery_quality": (
                "live_and_curated" if live_material_count and accepted_curated_materials
                else "live" if live_material_count
                else "curated_verified_accessible" if curated_access_ready
                else "curated_partially_accessible" if accepted_curated_materials
                else "curated_unreachable" if rejected_curated_materials
                else "unavailable"
            ),
            "hub_discovery_state": "blocked" if hub_error else "ready" if live_material_count else "empty",
            "discovered": bool(materials or curated_candidate_count),
            "fetchable": curated_access_ready if not live_material_count and curated_candidate_count else None,
            "cache_hit": False,
            "singleflight_shared": False,
            "material_count": len(materials),
            "usable_material_count": len(materials),
            "rejected_material_count": len(rejected_curated_materials),
            "materials": materials,
            "rejected_curated_materials": rejected_curated_materials,
            "source_errors": row_errors,
            "source_warnings": row_warnings,
        }
        if not materials and not hub_error:
            row_errors.append({
                "source": "official_company_ir_materials",
                "symbol": symbol,
                "code": "EARNINGS_MATERIALS_EMPTY",
                "message": "官方材料入口没有返回可归属到财政季度的业绩演示、管理层讲稿、补充信息或文字记录链接",
            })
        return row

    @staticmethod
    def _merge_curated_materials(
        symbol: str,
        hub_url: str,
        discovered: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        materials = [copy.deepcopy(item) for item in discovered]
        seen = {str(item.get("official_url") or "") for item in materials}
        for seed in CURATED_OFFICIAL_MATERIALS.get(symbol, []):
            official_url = OfficialEarningsMaterialsAdapter._safe_material_link(
                hub_url,
                str(seed.get("official_url") or ""),
                EARNINGS_MATERIAL_LINK_HOSTS[symbol],
            )
            if not official_url or official_url in seen:
                continue
            fiscal_period = str(seed.get("fiscal_period") or "")
            match = re.fullmatch(r"FY(20\d{2})-Q([1-4])", fiscal_period)
            if not match:
                continue
            seen.add(official_url)
            materials.append({
                **copy.deepcopy(seed),
                "official_url": official_url,
                "symbol": symbol,
                "fiscal_year": int(match.group(1)),
                "fiscal_quarter": int(match.group(2)),
                "period_confidence": "curated_verified",
                "hub_url": hub_url,
                "source_type": "company_ir",
                "source_tier": "primary",
                "claim_status": "company_statement",
                "source_locator": "curated official IR hub link",
                "discovery_method": "curated_verified",
                "verified_at": CURATED_MATERIALS_VERIFIED_AT,
                "valid_until": CURATED_MATERIALS_VALID_UNTIL,
                "access_state": "unchecked",
                "rights_boundary": "只保存链接和标题，不下载或镜像文件正文。",
                "execution_capability": "none",
                "live_trading_allowed": False,
            })
        return materials

    def _probe_curated_materials(
        self,
        symbol: str,
        materials: list[dict[str, Any]],
        *,
        captured_at: datetime,
    ) -> list[dict[str, Any]]:
        allowed_hosts = EARNINGS_MATERIAL_LINK_HOSTS[symbol]
        checked_at = _utc_iso(captured_at)

        def probe_one(material: dict[str, Any]) -> dict[str, Any] | None:
            official_url = str(material.get("official_url") or "")
            valid_until = str(material.get("valid_until") or "")
            try:
                expiry = datetime.fromisoformat(valid_until).date()
            except ValueError:
                expiry = datetime.min.date()
            if captured_at.date() > expiry:
                material["access_state"] = "stale"
                material["access_checked_at"] = checked_at
                return {
                    "source": "official_company_ir_materials",
                    "symbol": symbol,
                    "code": "EARNINGS_MATERIAL_CURATED_STALE",
                    "message": f"人工核验目录已超过 {CURATED_MATERIAL_MAX_AGE_DAYS} 天有效期",
                    "official_url": official_url,
                }
            try:
                probe = self._probe_material(official_url, allowed_hosts)
            except Exception as exc:
                message = str(exc)[:300]
                material["access_state"] = "blocked"
                material["access_checked_at"] = checked_at
                material["access_obstacle"] = message
                return {
                    "source": "official_company_ir_materials",
                    "symbol": symbol,
                    "code": (
                        "EARNINGS_MATERIAL_ACCESS_TIMEOUT"
                        if "timed out" in message.casefold()
                        else "EARNINGS_MATERIAL_ACCESS_ERROR"
                    ),
                    "message": message,
                    "official_url": official_url,
                }
            material["access_state"] = "fetchable"
            material["access_checked_at"] = checked_at
            material["access_status_code"] = int(probe.get("status_code") or 0)
            material["content_type"] = str(probe.get("content_type") or "")[:120]
            content_length = probe.get("content_length")
            material["content_length"] = int(content_length) if content_length is not None else None
            return None

        if len(materials) == 1:
            results = [probe_one(materials[0])]
        else:
            with ThreadPoolExecutor(
                max_workers=min(4, len(materials)),
                thread_name_prefix=f"earnings-material-probe-{symbol.removeprefix('US.')}",
            ) as executor:
                results = list(executor.map(probe_one, materials))
        return [error for error in results if error is not None]

    @staticmethod
    def _default_probe_material(url: str, allowed_hosts: set[str]) -> dict[str, Any]:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
            raise ValueError("官方业绩材料探针拒绝非白名单 HTTPS 链接")
        headers = {
            "User-Agent": "AI-Collaboration-Studio/0.1 local-read-only-research",
            "Accept": "application/pdf,application/octet-stream;q=0.8,*/*;q=0.1",
            "Accept-Encoding": "identity",
        }
        response = None
        try:
            try:
                response = urlopen(Request(url, headers=headers, method="HEAD"), timeout=6)
            except HTTPError as exc:
                if exc.code not in {405, 501}:
                    raise
                exc.close()
                response = urlopen(
                    Request(url, headers={**headers, "Range": "bytes=0-0"}),
                    timeout=6,
                )
            with response:
                final = urlparse(str(response.geturl() or ""))
                if final.scheme != "https" or final.hostname not in allowed_hosts:
                    raise ValueError("官方业绩材料链接重定向到了非白名单端点")
                status_code = int(getattr(response, "status", 0) or response.getcode() or 0)
                if status_code < 200 or status_code >= 300:
                    raise ValueError(f"官方业绩材料链接返回 HTTP {status_code}")
                content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().casefold()
                if content_type.startswith("text/html"):
                    raise ValueError("官方业绩材料链接返回 HTML 验证页而不是材料文件")
                declared_length = response.headers.get("Content-Length")
                return {
                    "status_code": status_code,
                    "content_type": content_type,
                    "content_length": int(declared_length) if declared_length else None,
                }
        finally:
            if response is not None:
                response.close()

    @staticmethod
    def _default_fetch_bytes(url: str, allowed_hosts: set[str]) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
            raise ValueError("官方业绩材料适配器拒绝非固定 HTTPS 入口")
        request = Request(url, headers={
            "User-Agent": "AI-Collaboration-Studio/0.1 local-read-only-research",
            "Accept": "text/html,application/xhtml+xml",
        })
        with urlopen(request, timeout=12) as response:
            final = urlparse(str(response.geturl() or ""))
            if final.scheme != "https" or final.hostname not in allowed_hosts:
                raise ValueError("官方业绩材料入口重定向到了非白名单端点")
            declared_length = response.headers.get("Content-Length")
            if declared_length and int(declared_length) > EARNINGS_MATERIAL_MAX_RESPONSE_BYTES:
                raise ValueError("官方业绩材料入口超过 2 MB 上限")
            raw = response.read(EARNINGS_MATERIAL_MAX_RESPONSE_BYTES + 1)
        if len(raw) > EARNINGS_MATERIAL_MAX_RESPONSE_BYTES:
            raise ValueError("官方业绩材料入口超过 2 MB 上限")
        return raw

    @staticmethod
    def _parse_material_links(
        raw: bytes,
        *,
        symbol: str,
        hub_url: str,
        allowed_link_hosts: set[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        decoded = raw.decode("utf-8", errors="replace")
        lowered = decoded.casefold()
        if "bm-verify" in lowered or "powered and protected by" in lowered:
            raise ValueError("官方材料入口返回了反自动化验证页，保留入口并降级为人工核验")
        parser = _AnchorSequenceParser()
        parser.feed(decoded)
        year: int | None = None
        quarter: int | None = None
        materials: list[dict[str, Any]] = []
        seen: set[str] = set()
        for token in parser.tokens:
            text = str(token.get("text") or "").strip()
            period = OfficialIrReleaseAdapter._fiscal_period(text)
            if period.get("fiscal_year") and period.get("fiscal_quarter"):
                year = int(period["fiscal_year"])
                quarter = int(period["fiscal_quarter"])
            elif token.get("type") == "text":
                if text.isdigit() and len(text) == 4 and 2000 <= int(text) <= 2100:
                    year = int(text)
                elif len(text) == 2 and text[0].casefold() == "q" and text[1] in "1234":
                    quarter = int(text[1])
            if token.get("type") != "anchor":
                continue
            material_kind = OfficialEarningsMaterialsAdapter._material_kind(text)
            if not material_kind:
                continue
            href = OfficialEarningsMaterialsAdapter._safe_material_link(
                hub_url,
                str(token.get("href") or ""),
                allowed_link_hosts,
            )
            if not href or href in seen:
                continue
            explicit_period = OfficialIrReleaseAdapter._fiscal_period(text)
            fiscal_year = explicit_period.get("fiscal_year") or year
            fiscal_quarter = explicit_period.get("fiscal_quarter") or quarter
            if not fiscal_year or not fiscal_quarter:
                continue
            seen.add(href)
            fiscal_period = f"FY{int(fiscal_year)}-Q{int(fiscal_quarter)}"
            materials.append({
                "symbol": symbol,
                "fiscal_period": fiscal_period,
                "fiscal_year": int(fiscal_year),
                "fiscal_quarter": int(fiscal_quarter),
                "period_confidence": "anchor_derived" if explicit_period.get("fiscal_period") else "page_context_derived",
                "material_kind": material_kind,
                "title": text or material_kind.replace("_", " ").title(),
                "official_url": href,
                "hub_url": hub_url,
                "source_type": "company_ir",
                "source_tier": "primary",
                "claim_status": "company_statement",
                "source_locator": "official IR hub anchor",
                "discovery_method": "live_hub_parse",
                "rights_boundary": "只保存链接和锚文本，不下载或镜像文件正文。",
                "execution_capability": "none",
                "live_trading_allowed": False,
            })
            if len(materials) >= limit:
                break
        return materials

    @staticmethod
    def _material_kind(title: str) -> str:
        text = " ".join(str(title or "").casefold().split())
        if "prepared remarks" in text:
            return "prepared_remarks"
        if "corrected transcript" in text:
            return "corrected_transcript"
        if "transcript" in text:
            return "earnings_transcript"
        if "supplemental" in text and ("financial" in text or "information" in text):
            return "supplemental_financial_information"
        if "earnings release" in text or "press release" in text:
            return "earnings_release"
        if "presentation" in text and "investor day" not in text and "conference" not in text:
            return "earnings_presentation"
        return ""

    @staticmethod
    def _safe_material_link(base_url: str, value: str, allowed_hosts: set[str]) -> str:
        parsed = urlparse(urljoin(base_url, str(value or "").strip()))
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in allowed_hosts:
            return ""
        return urlunparse(("https", parsed.netloc, parsed.path, parsed.params, parsed.query, ""))
