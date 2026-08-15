from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from .earnings_materials import OfficialEarningsMaterialsAdapter
from .earnings_pack_contract import (
    OFFICIAL_EARNINGS_PACK_VERSION,
    covered_official_earnings_pack_symbols,
)
from .earnings_metrics import official_earnings_metrics
from .futu_readonly import FutuUsMarketAdapter, STORAGE_SYMBOLS, _utc_iso
from .industry_proxies import FredIndustryProxyAdapter
from .ir_releases import OfficialIrReleaseAdapter
from .research_analytics import build_research_analytics
from .sec_edgar import SEC_DEFAULT_FORMS, SecEdgarAdapter
from .technical_metrics import calculate_technical_metrics


STORAGE_COMPARABILITY_NOTES = {
    "US.SNDK": [
        "Sandisk 于 2025-02-21 完成从 Western Digital 分拆；分拆前 Flash 历史不得与分拆后 SNDK 口径无说明拼接。",
    ],
    "US.WDC": [
        "Western Digital 于 2025-02-21 完成 Flash 业务分拆；此前合并口径包含 Flash，跨期比较必须标记业务边界变化。",
    ],
}


class StorageResearchMarketService:
    def __init__(
        self,
        adapter: FutuUsMarketAdapter | None = None,
        sec_adapter: SecEdgarAdapter | None = None,
        ir_adapter: OfficialIrReleaseAdapter | None = None,
        earnings_materials_adapter: OfficialEarningsMaterialsAdapter | None = None,
        industry_adapter: FredIndustryProxyAdapter | None = None,
    ) -> None:
        self.adapter = adapter or FutuUsMarketAdapter()
        self.sec_adapter = sec_adapter or SecEdgarAdapter()
        self.ir_adapter = ir_adapter or OfficialIrReleaseAdapter()
        self.earnings_materials_adapter = earnings_materials_adapter or OfficialEarningsMaterialsAdapter()
        self.industry_adapter = industry_adapter or FredIndustryProxyAdapter()
        self._cache: tuple[float, dict[str, Any]] | None = None
        self._lock = threading.RLock()

    def status(self) -> dict[str, Any]:
        return {
            **self.adapter.status(),
            "sec_edgar": self.sec_adapter.status(),
            "company_ir": self.ir_adapter.status(),
            "earnings_materials": self.earnings_materials_adapter.status(),
            "industry_proxies": self.industry_adapter.status(),
        }

    def snapshot(self, *, force: bool = False) -> dict[str, Any]:
        now_monotonic = time.monotonic()
        with self._lock:
            if self._cache and self._cache[0] > now_monotonic and not force:
                payload = copy.deepcopy(self._cache[1])
                payload["research_cache"] = {"hit": True}
                return payload

            payload = self._build_research_snapshot(force=force)
            ttl_seconds = max(1.0, float(getattr(self.adapter, "cache_ttl_seconds", 15.0)))
            self._cache = (time.monotonic() + ttl_seconds, copy.deepcopy(payload))
            payload["research_cache"] = {"hit": False, "ttl_seconds": ttl_seconds}
            return payload

    @staticmethod
    def _core_market_unavailable(snapshot: dict[str, Any]) -> bool:
        return snapshot.get("state") == "offline" or snapshot.get("ok") is False

    def _offline_research_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Return a complete, explicit evidence envelope without probing secondary sources."""
        symbols = tuple(snapshot.get("symbols") or STORAGE_SYMBOLS)
        skipped_error = {
            "source": "storage_research_snapshot",
            "code": "RESEARCH_ENRICHMENT_SKIPPED_CORE_MARKET_OFFLINE",
            "message": "富途核心行情不可用，已跳过本次聚合快照的补充来源采集；可通过独立端点单独查询官方来源。",
        }
        histories = {
            symbol: {"symbol": symbol, "rows": []}
            for symbol in symbols
        }
        technical_rows = [
            calculate_technical_metrics(symbol, [])
            for symbol in symbols
        ]
        research_analytics = build_research_analytics(histories)

        def skipped_payload(source: str, **extra: Any) -> dict[str, Any]:
            return {
                "ok": False,
                "state": "skipped",
                "source": source,
                "captured_at": snapshot.get("captured_at"),
                "rows": [],
                "source_errors": [copy.deepcopy(skipped_error)],
                "execution_capability": "none",
                "live_trading_allowed": False,
                **extra,
            }

        history_batch = skipped_payload(
            "futu_qfq_daily_history",
            interval="1d",
            price_adjustment="QFQ",
            histories=histories,
        )
        capital_flow = skipped_payload(
            "futu_opend_capital_flow",
            period="DAY",
        )
        financial_statements = skipped_payload(
            "futu_opend_financial_statements",
            statement_type="main_index",
        )
        official_filings = skipped_payload(
            "sec_edgar_submissions",
            source_type="regulatory_filing",
            source_tier="primary",
            forms=list(SEC_DEFAULT_FORMS),
        )
        revenue_breakdown = skipped_payload("futu_opend_revenue_breakdown")
        ir_releases = skipped_payload(
            "official_company_ir",
            source_type="company_ir",
            source_tier="primary",
            symbols=list(symbols),
        )
        official_earnings_materials = skipped_payload(
            "official_company_ir_materials",
            source_type="earnings_material_index",
            source_tier="primary",
            symbols=list(symbols),
        )
        official_earnings_packs = self._build_official_earnings_packs(
            ir_releases,
            official_earnings_materials,
        )
        official_earnings_packs["state"] = "skipped"
        official_earnings_packs["source_errors"] = [copy.deepcopy(skipped_error)]
        industry_supply_demand = skipped_payload(
            "fred_official_public_series",
            derived=[],
        )

        snapshot["evidence"] = {
            "version": "storage_market_evidence_v6",
            "snapshot_id": snapshot.get("snapshot_id"),
            "captured_at": snapshot.get("captured_at"),
            "state": "degraded",
            "enrichment": {
                "state": "skipped",
                "reason_code": skipped_error["code"],
                "message": skipped_error["message"],
            },
            "fundamental": {
                "source": "futu_market_snapshot",
                "rows": [],
                "source_errors": copy.deepcopy(snapshot.get("source_errors") or []),
            },
            "technical": {
                "source": "futu_qfq_daily_history",
                "formula_version": "technical_metrics_v1",
                "rows": technical_rows,
                "source_errors": history_batch["source_errors"],
            },
            "capital_flow": {
                "source": "futu_opend_capital_flow",
                "period": "DAY",
                "interpretation": "资金流是成交资金结构代理，不等同于新闻、社交舆情或未来方向。",
                "rows": [],
                "source_errors": capital_flow["source_errors"],
            },
            "financial_statements": {
                **financial_statements,
                "financial_type": "quarterly_annual",
                "interpretation": "核心行情离线，本次未采集财务指标；不得把空结果解释为公司没有披露。",
            },
            "official_filings": {
                **official_filings,
                "interpretation": "核心行情离线，本次聚合快照未查询 EDGAR；可通过独立官方证据端点查询。",
            },
            "revenue_breakdown": {
                **revenue_breakdown,
                "interpretation": "核心行情离线，本次未采集主营构成；不得把空结果解释为没有收入。",
            },
            "company_ir_releases": {
                **ir_releases,
                "interpretation": "核心行情离线，本次聚合快照未查询公司 IR；可通过独立官方证据端点查询。",
            },
            "official_earnings_packs": official_earnings_packs,
            "official_earnings_materials": official_earnings_materials,
            "industry_supply_demand": industry_supply_demand,
            "research_analytics": research_analytics,
        }
        return snapshot

    def _build_research_snapshot(self, *, force: bool) -> dict[str, Any]:
        snapshot = self.adapter.quote_batch(STORAGE_SYMBOLS, force=force)
        if self._core_market_unavailable(snapshot):
            return self._offline_research_snapshot(snapshot)
        try:
            history_batch = self.adapter.daily_history_batch(STORAGE_SYMBOLS, limit=120)
        except Exception as exc:
            history_batch = {
                "ok": False,
                "histories": {},
                "source_errors": [{
                    "source": "futu_opend",
                    "code": "TECHNICAL_HISTORY_ERROR",
                    "message": str(exc)[:300],
                }],
            }
        technical_rows = []
        histories = history_batch.get("histories") or {}
        for symbol in STORAGE_SYMBOLS:
            history = histories.get(symbol) or {}
            technical_rows.append(calculate_technical_metrics(symbol, history.get("rows") or []))
        research_analytics = build_research_analytics(histories)

        try:
            capital_flow = self.adapter.capital_flow_batch(STORAGE_SYMBOLS, limit_days=20)
        except Exception as exc:
            capital_flow = {
                "ok": False,
                "source": "futu_opend_capital_flow",
                "period": "DAY",
                "rows": [],
                "source_errors": [{
                    "source": "futu_opend",
                    "code": "CAPITAL_FLOW_ERROR",
                    "message": str(exc)[:300],
                }],
                "execution_capability": "none",
                "live_trading_allowed": False,
            }

        try:
            financial_statements = self.adapter.financial_statements_batch(
                STORAGE_SYMBOLS,
                statement_type="main_index",
                limit=2,
            )
        except Exception as exc:
            financial_statements = {
                "ok": False,
                "source": "futu_opend_financial_statements",
                "statement_type": "main_index",
                "rows": [],
                "source_errors": [{
                    "source": "futu_opend",
                    "code": "FINANCIAL_STATEMENTS_ERROR",
                    "message": str(exc)[:300],
                }],
                "execution_capability": "none",
                "live_trading_allowed": False,
            }

        try:
            official_filings = self.sec_adapter.recent_filings_batch(
                STORAGE_SYMBOLS,
                forms=SEC_DEFAULT_FORMS,
                limit=8,
                force=force,
            )
        except Exception as exc:
            official_filings = {
                "ok": False,
                "source": "sec_edgar_submissions",
                "source_type": "regulatory_filing",
                "source_tier": "primary",
                "rows": [],
                "source_errors": [{
                    "source": "sec_edgar",
                    "code": "SEC_FILINGS_ERROR",
                    "message": str(exc)[:300],
                }],
                "execution_capability": "none",
                "live_trading_allowed": False,
            }

        try:
            revenue_breakdown = self.adapter.revenue_breakdown_batch(STORAGE_SYMBOLS)
        except Exception as exc:
            revenue_breakdown = {
                "ok": False,
                "source": "futu_opend_revenue_breakdown",
                "rows": [],
                "source_errors": [{
                    "source": "futu_opend",
                    "code": "REVENUE_BREAKDOWN_ERROR",
                    "message": str(exc)[:300],
                }],
                "execution_capability": "none",
                "live_trading_allowed": False,
            }

        try:
            ir_releases = self.ir_adapter.recent_releases_batch(
                STORAGE_SYMBOLS,
                limit=20,
                force=force,
            )
            ir_releases = self._associate_ir_with_sec(ir_releases, official_filings)
        except Exception as exc:
            ir_releases = {
                "ok": False,
                "source": "official_company_ir",
                "source_type": "company_ir",
                "source_tier": "primary",
                "rows": [],
                "source_errors": [{
                    "source": "official_company_ir",
                    "code": "IR_RELEASES_ERROR",
                    "message": str(exc)[:300],
                }],
                "execution_capability": "none",
                "live_trading_allowed": False,
            }

        try:
            official_earnings_materials = self.earnings_materials_adapter.recent_materials_batch(
                STORAGE_SYMBOLS,
                limit=24,
                force=force,
            )
        except Exception as exc:
            official_earnings_materials = {
                "ok": False,
                "state": "empty",
                "source": "official_company_ir_materials",
                "source_type": "earnings_material_index",
                "source_tier": "primary",
                "rows": [],
                "source_errors": [{
                    "source": "official_company_ir_materials",
                    "code": "EARNINGS_MATERIALS_ERROR",
                    "message": str(exc)[:300],
                }],
                "execution_capability": "none",
                "live_trading_allowed": False,
            }

        official_earnings_packs = self._build_official_earnings_packs(
            ir_releases,
            official_earnings_materials,
        )

        try:
            industry_supply_demand = self.industry_adapter.snapshot(force=force)
        except Exception as exc:
            industry_supply_demand = {
                "ok": False,
                "state": "offline",
                "source": "fred_official_public_series",
                "rows": [],
                "derived": [],
                "source_errors": [{
                    "source": "fred",
                    "code": "INDUSTRY_PROXY_ERROR",
                    "message": str(exc)[:300],
                }],
                "execution_capability": "none",
                "live_trading_allowed": False,
            }

        fundamental_fields = (
            "symbol", "market_time", "quality", "equity_valid", "total_market_value",
            "net_profit", "earnings_per_share", "pe_ratio", "pe_ttm_ratio", "pb_ratio",
            "net_asset_per_share", "dividend_ttm", "dividend_yield_ttm",
        )
        fundamental_rows = [
            {field: row.get(field) for field in fundamental_fields}
            for row in snapshot.get("rows") or []
            if row.get("equity_valid")
        ]
        technical_ready = sum(1 for row in technical_rows if row.get("quality") in {"ready", "limited"})
        earnings_pack_symbols = covered_official_earnings_pack_symbols(
            official_earnings_packs,
            STORAGE_SYMBOLS,
        )
        earnings_pack_coverage_ready = earnings_pack_symbols == set(STORAGE_SYMBOLS)

        def contains_source_errors(value: Any) -> bool:
            if isinstance(value, dict):
                if value.get("source_errors"):
                    return True
                return any(contains_source_errors(item) for item in value.values())
            if isinstance(value, list):
                return any(contains_source_errors(item) for item in value)
            return False

        evidence_structure_ready = (
            len(fundamental_rows) == len(STORAGE_SYMBOLS)
            and technical_ready == len(STORAGE_SYMBOLS)
            and len(capital_flow.get("rows") or []) == len(STORAGE_SYMBOLS)
            and len(financial_statements.get("rows") or []) == len(STORAGE_SYMBOLS)
            and len(official_filings.get("rows") or []) == len(STORAGE_SYMBOLS)
            and len(revenue_breakdown.get("rows") or []) == len(STORAGE_SYMBOLS)
            and len(ir_releases.get("rows") or []) == len(STORAGE_SYMBOLS)
            and earnings_pack_coverage_ready
            and industry_supply_demand.get("state") == "ready"
            and (research_analytics.get("portfolio_risk") or {}).get("state") in {"ready", "limited"}
        )
        evidence_state = (
            "ready"
            if evidence_structure_ready
            and not contains_source_errors(snapshot)
            and not contains_source_errors(history_batch)
            and not contains_source_errors(capital_flow)
            and not contains_source_errors(financial_statements)
            and not contains_source_errors(official_filings)
            and not contains_source_errors(revenue_breakdown)
            and not contains_source_errors(ir_releases)
            and official_earnings_packs.get("state") == "ready"
            and not contains_source_errors(official_earnings_materials)
            and not contains_source_errors(official_earnings_packs)
            and not contains_source_errors(industry_supply_demand)
            and not contains_source_errors(research_analytics)
            else "degraded"
        )
        snapshot["evidence"] = {
            "version": "storage_market_evidence_v6",
            "snapshot_id": snapshot.get("snapshot_id"),
            "captured_at": snapshot.get("captured_at"),
            "state": evidence_state,
            "structure_ready": evidence_structure_ready,
            "fundamental": {
                "source": "futu_market_snapshot",
                "rows": fundamental_rows,
                "source_errors": copy.deepcopy(snapshot.get("source_errors") or []),
            },
            "technical": {
                "source": "futu_qfq_daily_history",
                "formula_version": "technical_metrics_v1",
                "rows": technical_rows,
                "source_errors": history_batch.get("source_errors") or [],
            },
            "capital_flow": {
                "source": "futu_opend_capital_flow",
                "period": "DAY",
                "interpretation": "资金流是成交资金结构代理，不等同于新闻、社交舆情或未来方向。",
                "rows": capital_flow.get("rows") or [],
                "source_errors": capital_flow.get("source_errors") or [],
            },
            "financial_statements": {
                "source": "futu_opend_financial_statements",
                "captured_at": financial_statements.get("captured_at"),
                "statement_type": "main_index",
                "financial_type": "quarterly_annual",
                "interpretation": "报告期、币种、会计准则和字段口径必须随数据保留；关键指标不等同于完整三张财务报表。",
                "rows": financial_statements.get("rows") or [],
                "source_errors": financial_statements.get("source_errors") or [],
            },
            "official_filings": {
                "source": "sec_edgar_submissions",
                "source_type": "regulatory_filing",
                "source_tier": "primary",
                "captured_at": official_filings.get("captured_at"),
                "forms": official_filings.get("forms") or list(SEC_DEFAULT_FORMS),
                "interpretation": "EDGAR 提交记录是一手监管来源；表单类型和提交时间本身不等同于事件影响或交易方向。",
                "rows": official_filings.get("rows") or [],
                "source_errors": official_filings.get("source_errors") or [],
            },
            "revenue_breakdown": {
                "source": "futu_opend_revenue_breakdown",
                "captured_at": revenue_breakdown.get("captured_at"),
                "interpretation": "主营构成是历史报告期收入结构；不同公司的披露维度、命名和币种不一定可直接横向相加。",
                "rows": revenue_breakdown.get("rows") or [],
                "source_errors": revenue_breakdown.get("source_errors") or [],
            },
            "company_ir_releases": {
                "source": "official_company_ir",
                "source_type": "company_ir",
                "source_tier": "primary",
                "captured_at": ir_releases.get("captured_at"),
                "interpretation": "公司新闻稿是一手自述来源但不是独立验证；与 SEC 同日或次日记录只标记关联候选，不静默删重。",
                "rows": ir_releases.get("rows") or [],
                "source_errors": ir_releases.get("source_errors") or [],
            },
            "official_earnings_packs": official_earnings_packs,
            "official_earnings_materials": official_earnings_materials,
            "industry_supply_demand": industry_supply_demand,
            "research_analytics": research_analytics,
        }
        return snapshot

    def history(
        self,
        symbol: str,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 120,
    ) -> dict[str, Any]:
        return self.adapter.daily_history(symbol, start=start, end=end, limit=limit)

    def history_batch(
        self,
        symbols: tuple[str, ...] | list[str] = STORAGE_SYMBOLS,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 120,
    ) -> dict[str, Any]:
        return self.adapter.daily_history_batch(
            symbols,
            start=start,
            end=end,
            limit=limit,
        )

    def financials(
        self,
        symbol: str,
        *,
        statement_type: str = "main_index",
        limit: int = 4,
    ) -> dict[str, Any]:
        return self.adapter.financial_statements_batch(
            [symbol],
            statement_type=statement_type,
            limit=limit,
        )

    def filings(
        self,
        symbol: str,
        *,
        forms: tuple[str, ...] | list[str] | None = None,
        limit: int = 8,
        force: bool = False,
    ) -> dict[str, Any]:
        return self.sec_adapter.recent_filings_batch(
            [symbol],
            forms=forms,
            limit=limit,
            force=force,
        )

    def revenue_breakdown(self, symbol: str) -> dict[str, Any]:
        return self.adapter.revenue_breakdown_batch([symbol])

    def ir_releases(
        self,
        symbol: str,
        *,
        limit: int = 8,
        force: bool = False,
    ) -> dict[str, Any]:
        releases = self.ir_adapter.recent_releases_batch([symbol], limit=limit, force=force)
        filings = self.sec_adapter.recent_filings_batch([symbol], limit=20, force=force)
        return self._associate_ir_with_sec(releases, filings)

    def earnings_packs(
        self,
        symbol: str,
        *,
        limit: int = 12,
        force: bool = False,
    ) -> dict[str, Any]:
        releases = self.ir_releases(symbol, limit=limit, force=force)
        materials = self.earnings_materials_adapter.recent_materials_batch(
            [symbol],
            limit=max(12, limit * 3),
            force=force,
        )
        return self._build_official_earnings_packs(releases, materials)

    def earnings_materials(
        self,
        symbol: str,
        *,
        limit: int = 24,
        force: bool = False,
    ) -> dict[str, Any]:
        return self.earnings_materials_adapter.recent_materials_batch(
            [symbol],
            limit=limit,
            force=force,
        )

    def industry_proxies(self, *, force: bool = False) -> dict[str, Any]:
        return self.industry_adapter.snapshot(force=force)

    def independent_evidence(self, *, force: bool = False) -> dict[str, Any]:
        """Collect public research evidence that does not depend on Futu OpenD.

        This path deliberately stays separate from the round-admission snapshot:
        it can prepare and freeze official material while OpenD is offline, but it
        can never make the Futu four-symbol market gate pass.
        """

        captured_at = _utc_iso(datetime.now(timezone.utc))

        def fetch_official_filings() -> dict[str, Any]:
            return self.sec_adapter.recent_filings_batch(
                STORAGE_SYMBOLS,
                forms=SEC_DEFAULT_FORMS,
                limit=8,
                force=force,
            )

        def fetch_ir_releases() -> dict[str, Any]:
            return self.ir_adapter.recent_releases_batch(
                STORAGE_SYMBOLS,
                limit=20,
                force=force,
            )

        def fetch_earnings_materials() -> dict[str, Any]:
            return self.earnings_materials_adapter.recent_materials_batch(
                STORAGE_SYMBOLS,
                limit=24,
                force=force,
            )

        def fetch_industry_proxies() -> dict[str, Any]:
            return self.industry_adapter.snapshot(force=force)

        # These sources are independent read-only services. Fetching them in
        # parallel keeps a cold readiness check bounded by the slowest source
        # instead of the sum of four network timeouts.
        with ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="storage-evidence",
        ) as executor:
            official_filings_future = executor.submit(fetch_official_filings)
            ir_releases_future = executor.submit(fetch_ir_releases)
            earnings_materials_future = executor.submit(fetch_earnings_materials)
            industry_proxies_future = executor.submit(fetch_industry_proxies)

        try:
            official_filings = official_filings_future.result()
        except Exception as exc:
            official_filings = {
                "ok": False,
                "state": "offline",
                "source": "sec_edgar_submissions",
                "rows": [],
                "source_errors": [{
                    "source": "sec_edgar",
                    "code": "SEC_FILINGS_ERROR",
                    "message": str(exc)[:300],
                }],
                "execution_capability": "none",
                "live_trading_allowed": False,
            }

        try:
            ir_releases = ir_releases_future.result()
            ir_releases = self._associate_ir_with_sec(ir_releases, official_filings)
        except Exception as exc:
            ir_releases = {
                "ok": False,
                "state": "offline",
                "source": "official_company_ir",
                "rows": [],
                "source_errors": [{
                    "source": "official_company_ir",
                    "code": "IR_RELEASES_ERROR",
                    "message": str(exc)[:300],
                }],
                "execution_capability": "none",
                "live_trading_allowed": False,
            }

        try:
            official_earnings_materials = earnings_materials_future.result()
        except Exception as exc:
            official_earnings_materials = {
                "ok": False,
                "state": "offline",
                "source": "official_company_ir_materials",
                "rows": [],
                "source_errors": [{
                    "source": "official_company_ir_materials",
                    "code": "EARNINGS_MATERIALS_ERROR",
                    "message": str(exc)[:300],
                }],
                "execution_capability": "none",
                "live_trading_allowed": False,
            }

        official_earnings_packs = self._build_official_earnings_packs(
            ir_releases,
            official_earnings_materials,
        )

        try:
            industry_supply_demand = industry_proxies_future.result()
        except Exception as exc:
            industry_supply_demand = {
                "ok": False,
                "state": "offline",
                "source": "fred_official_public_series",
                "rows": [],
                "derived": [],
                "source_errors": [{
                    "source": "fred",
                    "code": "INDUSTRY_PROXY_ERROR",
                    "message": str(exc)[:300],
                }],
                "execution_capability": "none",
                "live_trading_allowed": False,
            }

        evidence = {
            "official_filings": {
                **official_filings,
                "interpretation": "EDGAR 是一手监管索引；表单类型和提交时间不等于方向判断。",
            },
            "company_ir_releases": {
                **ir_releases,
                "interpretation": "公司 IR 是一手自述而非独立验证；SEC 日期关联仍需人工核对。",
            },
            "official_earnings_packs": official_earnings_packs,
            "official_earnings_materials": official_earnings_materials,
            "industry_supply_demand": industry_supply_demand,
        }

        def contains_source_errors(value: Any) -> bool:
            if isinstance(value, dict):
                if value.get("source_errors"):
                    return True
                return any(contains_source_errors(item) for item in value.values())
            if isinstance(value, list):
                return any(contains_source_errors(item) for item in value)
            return False

        symbol_set = set(STORAGE_SYMBOLS)

        def covered_symbols(payload: dict[str, Any], child_key: str) -> set[str]:
            return {
                str(row.get("symbol") or "")
                for row in payload.get("rows") or []
                if str(row.get("symbol") or "") in symbol_set and row.get(child_key)
            }

        filings_coverage = covered_symbols(official_filings, "filings")
        ir_coverage = covered_symbols(ir_releases, "releases")
        earnings_coverage = covered_official_earnings_pack_symbols(
            official_earnings_packs,
            STORAGE_SYMBOLS,
        )
        industry_ready = (
            industry_supply_demand.get("state") == "ready"
            and not contains_source_errors(industry_supply_demand)
        )
        fully_ready = all((
            filings_coverage == symbol_set,
            ir_coverage == symbol_set,
            earnings_coverage == symbol_set,
            official_earnings_packs.get("state") == "ready",
            industry_ready,
            not contains_source_errors(evidence),
        ))
        usable = bool(
            filings_coverage
            or ir_coverage
            or earnings_coverage
            or industry_supply_demand.get("rows")
            or industry_supply_demand.get("derived")
        )

        return {
            "ok": usable,
            "version": "storage_independent_evidence_v1",
            "state": "ready" if fully_ready else "partial" if usable else "blocked",
            "source": "public_readonly_research_sources",
            "captured_at": captured_at,
            "symbols": list(STORAGE_SYMBOLS),
            "evidence": evidence,
            "coverage": {
                "official_filings": sorted(filings_coverage),
                "company_ir_releases": sorted(ir_coverage),
                "official_earnings_packs": sorted(earnings_coverage),
                "industry_supply_demand": bool(industry_ready),
            },
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

    def official_evidence_material_payload(
        self,
        *,
        evidence_kind: str,
        symbol: str,
        official_url: str,
    ) -> dict[str, Any]:
        normalized_kind = str(evidence_kind or "").strip().lower()
        normalized_symbol = str(symbol or "").strip().upper()
        normalized_url = str(official_url or "").strip()[:2000]
        if normalized_kind == "sec_filing":
            payload = self.sec_adapter.recent_filings_batch(
                [normalized_symbol],
                forms=SEC_DEFAULT_FORMS,
                limit=40,
            )
            row = (payload.get("rows") or [{}])[0]
            evidence = next(
                (item for item in row.get("filings") or [] if item.get("official_url") == normalized_url),
                None,
            )
            if evidence is None:
                raise ValueError("当前 SEC 官方证据中找不到该文件，请刷新后重试")
            accession = str(evidence.get("accession_number") or "")
            form = str(evidence.get("form") or "")
            description = str(evidence.get("description") or "官方申报")
            return {
                "title": f"[SEC {form}] {normalized_symbol.removeprefix('US.')} · {description}"[:120],
                "kind": "url",
                "source_url": normalized_url,
                "content": (
                    "# SEC EDGAR 官方申报索引\n\n"
                    f"- 标的：{normalized_symbol}\n"
                    f"- 公司：{row.get('company_name') or '未知'}\n"
                    f"- 表单：{form}\n"
                    f"- 提交日：{evidence.get('filing_date') or '未知'}\n"
                    f"- 报告期：{evidence.get('report_date') or '未知'}\n"
                    f"- SEC 接受时间：{evidence.get('accepted_at') or '未知'}\n"
                    f"- 事项：{evidence.get('items') or '未列出'}\n"
                    f"- 说明：{description}\n"
                    f"- 官方原文：{normalized_url}\n\n"
                    "这是一份由用户确认冻结的官方索引资料，不包含对原文的方向解释。"
                    "使用前仍需打开 SEC 原文核对，不得仅凭表单类型推断利好或利空。"
                ),
                "metadata": {
                    "source_type": "regulatory_filing",
                    "event_type": "other",
                    "publisher": "U.S. Securities and Exchange Commission EDGAR",
                    "published_at": evidence.get("published_at") or evidence.get("filing_date"),
                    "symbols": [normalized_symbol],
                    "official_evidence_kind": "sec_filing",
                    "official_evidence_id": accession,
                    "source_captured_at": payload.get("captured_at"),
                },
            }
        if normalized_kind == "ir_release":
            releases = self.ir_releases(normalized_symbol, limit=20)
            row = (releases.get("rows") or [{}])[0]
            evidence = next(
                (item for item in row.get("releases") or [] if item.get("official_url") == normalized_url),
                None,
            )
            if evidence is None:
                raise ValueError("当前公司 IR 官方证据中找不到该新闻稿，请刷新后重试")
            direct_materials = []
            if evidence.get("event_type") == "earnings_release" and evidence.get("fiscal_period"):
                try:
                    material_payload = self.earnings_materials_adapter.recent_materials_batch(
                        [normalized_symbol],
                        limit=24,
                    )
                    material_row = (material_payload.get("rows") or [{}])[0]
                    direct_materials = [
                        item
                        for item in material_row.get("materials") or []
                        if item.get("fiscal_period") == evidence.get("fiscal_period")
                    ]
                except Exception:
                    direct_materials = []
            evidence_id = "ir_" + hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:24]
            matches = evidence.get("possible_sec_matches") or []
            match_lines = "\n".join(
                f"  - {match.get('form')} {match.get('filing_date')} {match.get('official_url')}"
                for match in matches
            ) or "  - 无；这不代表不存在相关申报。"
            direct_material_lines = "\n".join(
                (
                    f"  - {item.get('material_kind') or 'official_material'}："
                    f"{item.get('official_url')}"
                    f"（发现方式 {item.get('discovery_method') or 'unknown'}"
                    f"，核验日 {item.get('verified_at') or '实时入口'}"
                    f"，访问状态 {item.get('access_state') or '未单独探测'}）"
                )
                for item in direct_materials
            ) or "  - 未定位具体文件；保留官方材料入口供人工核验。"
            located_metrics = official_earnings_metrics(
                normalized_symbol,
                str(evidence.get("fiscal_period") or ""),
            )
            located_metric_lines = "\n".join(
                (
                    f"  - [{'历史事实' if metric.get('fact_or_guidance') == 'historical_fact' else '公司指引'}] "
                    f"{metric.get('metric_name')}：{metric.get('value_text')}；"
                    f"定位 {metric.get('source_locator')}；{metric.get('source_url')}"
                )
                for metric in located_metrics
            ) or "  - 当前期间尚无人工页码核验指标。"
            normalized_event_type = (
                "earnings"
                if str(evidence.get("event_type") or "").startswith("earnings")
                else evidence.get("event_type") or "other"
            )
            return {
                "title": f"[公司 IR] {normalized_symbol.removeprefix('US.')} · {evidence.get('title') or '官方新闻稿'}"[:120],
                "kind": "url",
                "source_url": normalized_url,
                "content": (
                    "# 公司投资者关系新闻稿索引\n\n"
                    f"- 标的：{normalized_symbol}\n"
                    f"- 发布者：{row.get('publisher') or '未知'}\n"
                    f"- 标题：{evidence.get('title') or '未知'}\n"
                    f"- 发布时间：{evidence.get('published_at') or '未知'}\n"
                    f"- 财政期间：{evidence.get('fiscal_period') or '标题未能可靠识别'}\n"
                    f"- 事件类型：{evidence.get('event_type') or 'other'}\n"
                    f"- 官方原文：{normalized_url}\n"
                    f"- 演示材料入口：{evidence.get('presentation_hub_url') or '未提供'}\n"
                    f"- 摘要：{evidence.get('summary') or '未提供'}\n"
                    "- 同期官方支持材料：\n"
                    f"{direct_material_lines}\n"
                    "- 已定位关键指标（仍属公司自述）：\n"
                    f"{located_metric_lines}\n"
                    "- 可能关联的 SEC 申报：\n"
                    f"{match_lines}\n\n"
                    "这是一份由用户确认冻结的公司一手自述索引，不是独立核验。"
                    "可能的 SEC 关联只按日期提示，不能视为已确认重复项。"
                ),
                "metadata": {
                    "source_type": "company_ir",
                    "event_type": normalized_event_type,
                    "publisher": row.get("publisher"),
                    "published_at": evidence.get("published_at"),
                    "symbols": [normalized_symbol],
                    "fiscal_period": evidence.get("fiscal_period"),
                    "claim_status": evidence.get("claim_status") or "company_statement",
                    "technology_scope": evidence.get("technology_scope") or [],
                    "presentation_hub_url": evidence.get("presentation_hub_url"),
                    "direct_material_count": len(direct_materials),
                    "located_metric_count": len(located_metrics),
                    "official_evidence_kind": "ir_release",
                    "official_evidence_id": evidence_id,
                    "source_captured_at": releases.get("captured_at"),
                    "possible_sec_matches": matches,
                },
            }
        raise ValueError("官方证据类型必须是 sec_filing 或 ir_release")

    @staticmethod
    def _build_official_earnings_packs(
        releases_payload: dict[str, Any],
        materials_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        materials_by_symbol_period: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for material_row in (materials_payload or {}).get("rows") or []:
            material_symbol = str(material_row.get("symbol") or "")
            for material in material_row.get("materials") or []:
                material_period = str(material.get("fiscal_period") or "")
                if material_symbol and material_period:
                    materials_by_symbol_period.setdefault(
                        (material_symbol, material_period), []
                    ).append(copy.deepcopy(material))
        release_rows_by_symbol = {
            str(row.get("symbol") or ""): row
            for row in releases_payload.get("rows") or []
            if row.get("symbol")
        }
        requested_symbols = [
            str(symbol)
            for symbol in (releases_payload.get("symbols") or release_rows_by_symbol.keys())
            if symbol
        ]
        rows: list[dict[str, Any]] = []
        pack_count = 0
        for symbol in requested_symbols:
            row = release_rows_by_symbol.get(symbol) or {"symbol": symbol, "releases": []}
            packs = []
            for release in row.get("releases") or []:
                if release.get("event_type") != "earnings_release":
                    continue
                release_url = str(release.get("official_url") or "")
                if not release_url:
                    continue
                fiscal_period = release.get("fiscal_period") or "UNRESOLVED"
                pack_id = "earnings_" + hashlib.sha256(
                    f"{symbol}|{fiscal_period}|{release_url}".encode("utf-8")
                ).hexdigest()[:24]
                direct_materials = materials_by_symbol_period.get((symbol, fiscal_period), [])
                presentation = next(
                    (item for item in direct_materials if item.get("material_kind") == "earnings_presentation"),
                    None,
                )
                prepared_remarks = next(
                    (item for item in direct_materials if item.get("material_kind") == "prepared_remarks"),
                    None,
                )
                supplemental = next(
                    (
                        item for item in direct_materials
                        if item.get("material_kind") == "supplemental_financial_information"
                    ),
                    None,
                )
                earnings_release_material = next(
                    (item for item in direct_materials if item.get("material_kind") == "earnings_release"),
                    None,
                )
                transcript = next(
                    (
                        item for item in direct_materials
                        if item.get("material_kind") in {"corrected_transcript", "earnings_transcript"}
                    ),
                    None,
                )
                metrics = official_earnings_metrics(symbol, fiscal_period)
                packs.append({
                    "pack_id": pack_id,
                    "version": OFFICIAL_EARNINGS_PACK_VERSION,
                    "symbol": symbol,
                    "fiscal_period": fiscal_period,
                    "fiscal_year": release.get("fiscal_year"),
                    "fiscal_quarter": release.get("fiscal_quarter"),
                    "period_confidence": release.get("period_confidence") or "unknown",
                    "includes_full_year": bool(release.get("includes_full_year")),
                    "published_at": release.get("published_at"),
                    "published_date": release.get("published_date"),
                    "title": release.get("title"),
                    "release_url": release_url,
                    "presentation_hub_url": (
                        release.get("presentation_hub_url") or row.get("presentation_hub_url")
                    ),
                    "presentation_url": (presentation or {}).get("official_url"),
                    "prepared_remarks_url": (prepared_remarks or {}).get("official_url"),
                    "supplemental_url": (supplemental or {}).get("official_url"),
                    "earnings_release_material_url": (earnings_release_material or {}).get("official_url"),
                    "transcript_url": (transcript or {}).get("official_url"),
                    "direct_materials": direct_materials,
                    "metric_count": len(metrics),
                    "metrics": metrics,
                    "presentation_discovery_status": (
                        "direct_official" if direct_materials else "hub_only"
                    ),
                    "source_kind": "company_ir_release",
                    "source_type": "company_ir",
                    "source_tier": "primary",
                    "source_locator": release.get("source_locator") or "IR RSS title and summary",
                    "claim_status": release.get("claim_status") or "company_statement",
                    "company_claim": True,
                    "technology_scope": (
                        release.get("technology_scope") or row.get("technology_scope") or []
                    ),
                    "summary": release.get("summary") or "",
                    "possible_sec_matches": release.get("possible_sec_matches") or [],
                    "comparability_notes": STORAGE_COMPARABILITY_NOTES.get(symbol, []),
                    "rights_boundary": "保存官方链接、时期、定位和少量索引信息；不镜像整份新闻稿或演示材料。",
                    "execution_capability": "none",
                    "live_trading_allowed": False,
                })
            packs.sort(key=lambda item: str(item.get("published_at") or ""), reverse=True)
            pack_count += len(packs)
            rows.append({
                "symbol": symbol,
                "publisher": row.get("publisher"),
                "presentation_hub_url": row.get("presentation_hub_url"),
                "technology_scope": row.get("technology_scope") or [],
                "pack_count": len(packs),
                "packs": packs,
            })
        covered_symbols = covered_official_earnings_pack_symbols(
            {
                "version": OFFICIAL_EARNINGS_PACK_VERSION,
                "rows": rows,
                "execution_capability": "none",
                "live_trading_allowed": False,
            },
            requested_symbols,
        )
        symbols_with_packs = len(covered_symbols)
        missing_symbols = [
            symbol for symbol in requested_symbols if symbol.upper() not in covered_symbols
        ]
        source_errors = (
            (releases_payload.get("source_errors") or [])
            + ((materials_payload or {}).get("source_errors") or [])
        )
        source_warnings = (
            (releases_payload.get("source_warnings") or [])
            + ((materials_payload or {}).get("source_warnings") or [])
        )
        pack_state = (
            "empty"
            if not pack_count
            else "partial"
            if missing_symbols or source_errors
            else "ready"
        )
        return {
            "ok": pack_count > 0,
            "version": OFFICIAL_EARNINGS_PACK_VERSION,
            "state": pack_state,
            "source": "official_company_ir_and_sec",
            "source_type": "earnings_material_index",
            "source_tier": "primary",
            "captured_at": releases_payload.get("captured_at"),
            "interpretation": (
                "业绩材料包是公司自述、演示材料入口与 SEC 日期候选的索引；"
                "它不等于独立验证，也不自动产生方向或胜率。"
            ),
            "rights_boundary": "不复制整份受版权保护材料，只保留必要索引、少量摘要和官方链接。",
            "pack_count": pack_count,
            "missing_symbols": missing_symbols,
            "rows": rows,
            "source_errors": source_errors,
            "source_warnings": source_warnings,
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

    @staticmethod
    def _associate_ir_with_sec(
        releases_payload: dict[str, Any],
        filings_payload: dict[str, Any],
    ) -> dict[str, Any]:
        linked = copy.deepcopy(releases_payload)
        filings_by_symbol = {
            row.get("symbol"): row.get("filings") or []
            for row in filings_payload.get("rows") or []
        }
        for row in linked.get("rows") or []:
            candidates = filings_by_symbol.get(row.get("symbol")) or []
            for release in row.get("releases") or []:
                try:
                    release_date = datetime.strptime(release.get("published_date") or "", "%Y-%m-%d").date()
                except (TypeError, ValueError):
                    release["possible_sec_matches"] = []
                    continue
                matches = []
                for filing in candidates:
                    try:
                        filing_date = datetime.strptime(filing.get("filing_date") or "", "%Y-%m-%d").date()
                    except (TypeError, ValueError):
                        continue
                    if abs((filing_date - release_date).days) <= 1:
                        matches.append({
                            "accession_number": filing.get("accession_number"),
                            "form": filing.get("form"),
                            "filing_date": filing.get("filing_date"),
                            "official_url": filing.get("official_url"),
                        })
                release["possible_sec_matches"] = matches[:5]
        return linked

    @staticmethod
    def prompt_context(snapshot: dict[str, Any]) -> str:
        rows = snapshot.get("rows") or []
        compact_rows = [
            {
                "symbol": row.get("symbol"),
                "market_time": row.get("market_time"),
                "updated_at": row.get("updated_at"),
                "age_seconds": row.get("age_seconds"),
                "quality": row.get("quality"),
                "research_ready": row.get("research_ready"),
                "quote_is_live": row.get("quote_is_live"),
                "market_state": row.get("market_state"),
                "freshness_basis": row.get("freshness_basis"),
                "security_status": row.get("security_status"),
                "suspended": row.get("suspended"),
                "last": row.get("last"),
                "change_rate": row.get("change_rate"),
                "volume": row.get("volume"),
                "bid": row.get("bid"),
                "ask": row.get("ask"),
                "pe_ttm_ratio": row.get("pe_ttm_ratio"),
                "pb_ratio": row.get("pb_ratio"),
                "total_market_value": row.get("total_market_value"),
                "net_profit": row.get("net_profit"),
                "earnings_per_share": row.get("earnings_per_share"),
            }
            for row in rows
        ]
        evidence = snapshot.get("evidence") or {}
        technical = (evidence.get("technical") or {}).get("rows") or []
        capital_flow = evidence.get("capital_flow") or {}
        financial_statements = evidence.get("financial_statements") or {}
        official_filings = evidence.get("official_filings") or {}
        revenue_breakdown = evidence.get("revenue_breakdown") or {}
        company_ir_releases = evidence.get("company_ir_releases") or {}
        official_earnings_packs = evidence.get("official_earnings_packs") or {}
        industry_supply_demand = evidence.get("industry_supply_demand") or {}
        research_analytics = evidence.get("research_analytics") or {}
        compact_financials = []
        for row in financial_statements.get("rows") or []:
            reports = row.get("reports") or []
            latest = reports[-1] if reports else {}
            items = latest.get("items") or []
            compact_financials.append({
                "symbol": row.get("symbol"),
                "statement_type": row.get("statement_type"),
                "period_end": latest.get("period_end"),
                "period_text": latest.get("period_text"),
                "currency_code": latest.get("currency_code"),
                "accounting_standards": latest.get("accounting_standards"),
                "auditor_report": latest.get("auditor_report"),
                "item_count": latest.get("item_count"),
                "items_in_context": items[:40],
                "context_truncated": len(items) > 40,
            })
        compact_filings = []
        for row in official_filings.get("rows") or []:
            compact_filings.append({
                "symbol": row.get("symbol"),
                "cik": row.get("cik"),
                "company_name": row.get("company_name"),
                "filings": [
                    {
                        "form": filing.get("form"),
                        "filing_date": filing.get("filing_date"),
                        "report_date": filing.get("report_date"),
                        "accepted_at": filing.get("accepted_at"),
                        "description": filing.get("description"),
                        "items": filing.get("items"),
                        "official_url": filing.get("official_url"),
                    }
                    for filing in (row.get("filings") or [])[:5]
                ],
            })
        compact_revenue_breakdown = []
        for row in revenue_breakdown.get("rows") or []:
            compact_revenue_breakdown.append({
                "symbol": row.get("symbol"),
                "period": row.get("period"),
                "period_time": row.get("period_time"),
                "currency_code": row.get("currency_code"),
                "dimensions": [
                    {
                        "type": dimension.get("type"),
                        "item_count": dimension.get("item_count"),
                        "items_in_context": (dimension.get("items") or [])[:20],
                        "context_truncated": len(dimension.get("items") or []) > 20,
                    }
                    for dimension in row.get("dimensions") or []
                ],
            })
        compact_ir_releases = []
        for row in company_ir_releases.get("rows") or []:
            compact_ir_releases.append({
                "symbol": row.get("symbol"),
                "publisher": row.get("publisher"),
                "releases": [
                    {
                        "title": release.get("title"),
                        "published_at": release.get("published_at"),
                        "official_url": release.get("official_url"),
                        "summary": release.get("summary"),
                        "possible_sec_matches": release.get("possible_sec_matches") or [],
                    }
                    for release in (row.get("releases") or [])[:5]
                ],
            })
        compact_earnings_packs = []
        for row in official_earnings_packs.get("rows") or []:
            compact_earnings_packs.extend([
                {
                    "pack_id": pack.get("pack_id"),
                    "symbol": pack.get("symbol"),
                    "fiscal_period": pack.get("fiscal_period"),
                    "period_confidence": pack.get("period_confidence"),
                    "published_at": pack.get("published_at"),
                    "title": pack.get("title"),
                    "release_url": pack.get("release_url"),
                    "presentation_hub_url": pack.get("presentation_hub_url"),
                    "presentation_url": pack.get("presentation_url"),
                    "prepared_remarks_url": pack.get("prepared_remarks_url"),
                    "supplemental_url": pack.get("supplemental_url"),
                    "presentation_discovery_status": pack.get("presentation_discovery_status"),
                    "technology_scope": pack.get("technology_scope") or [],
                    "claim_status": pack.get("claim_status"),
                    "possible_sec_matches": pack.get("possible_sec_matches") or [],
                    "comparability_notes": pack.get("comparability_notes") or [],
                    "metrics": [
                        {
                            "metric_id": metric.get("metric_id"),
                            "metric_name": metric.get("metric_name"),
                            "value_text": metric.get("value_text"),
                            "unit": metric.get("unit"),
                            "direction": metric.get("direction"),
                            "comparison_period": metric.get("comparison_period"),
                            "fact_or_guidance": metric.get("fact_or_guidance"),
                            "technology": metric.get("technology"),
                            "source_url": metric.get("source_url"),
                            "source_locator": metric.get("source_locator"),
                            "claim_status": metric.get("claim_status"),
                            "verified_at": metric.get("verified_at"),
                        }
                        for metric in (pack.get("metrics") or [])[:12]
                    ],
                }
                for pack in (row.get("packs") or [])[:3]
            ])
        compact_industry = {
            "rows": [
                {
                    "series_id": row.get("series_id"),
                    "label": row.get("label"),
                    "scope": row.get("scope"),
                    "units": row.get("units"),
                    "as_of": row.get("as_of"),
                    "latest": row.get("latest"),
                    "change_1_observation_pct": row.get("change_1_observation_pct"),
                    "change_3_observations_pct": row.get("change_3_observations_pct"),
                    "change_12_observations_pct": row.get("change_12_observations_pct"),
                    "source_url": row.get("source_url"),
                }
                for row in industry_supply_demand.get("rows") or []
            ],
            "derived": [
                {key: value for key, value in row.items() if key != "observations"}
                for row in industry_supply_demand.get("derived") or []
            ],
            "interpretation": industry_supply_demand.get("interpretation"),
        }
        return (
            "本轮共享的富途只读证据快照如下。所有成员必须使用同一 snapshot_id，"
            "不得把缺失或过期数据当作实时事实，不得据此执行交易。quality=ready 只表示行情新鲜度可用于研究；"
            "research_ready=true 还要求证券未停牌；security_status 显式提供时必须为 NORMAL，"
            "缺失状态只用于兼容旧冻结快照；"
            "只有 quote_is_live=true 才是 20 分钟实时窗，quote_is_live=false 的闭市截面不是实时行情。\n"
            f"snapshot_id={snapshot.get('snapshot_id')} captured_at={snapshot.get('captured_at')} "
            f"quote_state={snapshot.get('state')} evidence_state={evidence.get('state')} "
            f"missing={snapshot.get('missing_symbols')}\n"
            f"quotes={json.dumps(compact_rows, ensure_ascii=False)}\n"
            "technical_metrics 是用复权日线向后计算的确定性指标，不是预测；"
            "资金流向只是成交资金结构代理，不等同于新闻或社交情绪。\n"
            f"technical_metrics={json.dumps(technical, ensure_ascii=False)}\n"
            f"capital_flow={json.dumps(capital_flow.get('rows') or [], ensure_ascii=False)}\n"
            "financial_statements 使用富途只读财报接口；当前共同截面只放最新关键指标，"
            "不能把它冒充完整利润表、资产负债表或现金流量表。\n"
            f"financial_statements={json.dumps(compact_financials, ensure_ascii=False)}\n"
            "official_filings 来自 SEC EDGAR 官方提交记录；必须按表单、提交时间和官方链接引用，"
            "不得仅凭表单出现推断利好或利空。\n"
            f"official_filings={json.dumps(compact_filings, ensure_ascii=False)}\n"
            "revenue_breakdown 是历史报告期主营构成；比较时必须保留期间、币种和维度，"
            "不同公司披露标签不自动视为同一产品。\n"
            f"revenue_breakdown={json.dumps(compact_revenue_breakdown, ensure_ascii=False)}\n"
            "company_ir_releases 是公司一手自述，不是独立核验；与 SEC 只做日期关联候选，"
            "必须阅读标题、时间和官方链接后再判断事件性质。\n"
            f"company_ir_releases={json.dumps(compact_ir_releases, ensure_ascii=False)}\n"
            "official_earnings_packs 归一化财政期间、官方材料入口、SEC 日期候选、业务口径断点，"
            "以及少量带页码定位的人工核验指标；必须区分 historical_fact 与 company_guidance。"
            "所有公司说法仍是 company_statement，不能把 direction 字段直接当成交易方向或胜率。\n"
            f"official_earnings_packs={json.dumps(compact_earnings_packs, ensure_ascii=False)}\n"
            "industry_supply_demand 是美国官方月度行业代理，不是 DRAM、NAND 或 HDD 即时报价；"
            "必须保留 device / broad_semiconductor 范围和未季调等限制，不得直接外推到单家公司。\n"
            f"industry_supply_demand={json.dumps(compact_industry, ensure_ascii=False)}\n"
            "research_analytics 使用相同复权历史数据计算非重叠窗口基准率和等权模拟组合风险；"
            "它不是策略回测、未来胜率、真实仓位或交易建议。\n"
            f"research_analytics={json.dumps(research_analytics, ensure_ascii=False)}\n"
            f"quote_errors={json.dumps(snapshot.get('source_errors') or [], ensure_ascii=False)}\n"
            f"technical_errors={json.dumps((evidence.get('technical') or {}).get('source_errors') or [], ensure_ascii=False)}\n"
            f"capital_flow_errors={json.dumps(capital_flow.get('source_errors') or [], ensure_ascii=False)}\n"
            f"financial_statement_errors={json.dumps(financial_statements.get('source_errors') or [], ensure_ascii=False)}\n"
            f"official_filing_errors={json.dumps(official_filings.get('source_errors') or [], ensure_ascii=False)}\n"
            f"revenue_breakdown_errors={json.dumps(revenue_breakdown.get('source_errors') or [], ensure_ascii=False)}\n"
            f"company_ir_errors={json.dumps(company_ir_releases.get('source_errors') or [], ensure_ascii=False)}\n"
            f"official_earnings_pack_errors={json.dumps(official_earnings_packs.get('source_errors') or [], ensure_ascii=False)}\n"
            f"industry_proxy_errors={json.dumps(industry_supply_demand.get('source_errors') or [], ensure_ascii=False)}"
        )

    @staticmethod
    def timeline_summary(snapshot: dict[str, Any]) -> str:
        if not snapshot.get("rows"):
            errors = snapshot.get("source_errors") or []
            reason = errors[0].get("message") if errors else "行情不可用"
            return f"富途只读行情快照未取得数据：{reason}。本轮不得假设实时价格。"
        row_text = "；".join(
            f"{row.get('symbol')} {row.get('last')}（{row.get('quality')}，"
            f"{'20分钟实时窗' if row.get('quote_is_live') is True else '非实时研究截面'}，"
            f"{row.get('freshness_basis') or '旧版新鲜度未知'}，"
            f"market_state={row.get('market_state') or '未查询'}，"
            f"security_status={row.get('security_status') or '旧版未提供'}，"
            f"suspended={str(bool(row.get('suspended'))).lower()}，{row.get('market_time')}）"
            for row in snapshot.get("rows") or []
        )
        missing = snapshot.get("missing_symbols") or []
        suffix = f"；缺失：{', '.join(missing)}" if missing else ""
        evidence = snapshot.get("evidence") or {}
        technical_count = len((evidence.get("technical") or {}).get("rows") or [])
        flow_count = len((evidence.get("capital_flow") or {}).get("rows") or [])
        financial_count = len((evidence.get("financial_statements") or {}).get("rows") or [])
        filing_count = sum(
            len(row.get("filings") or [])
            for row in (evidence.get("official_filings") or {}).get("rows") or []
        )
        breakdown_count = len((evidence.get("revenue_breakdown") or {}).get("rows") or [])
        ir_count = sum(
            len(row.get("releases") or [])
            for row in (evidence.get("company_ir_releases") or {}).get("rows") or []
        )
        earnings_pack_count = int((evidence.get("official_earnings_packs") or {}).get("pack_count") or 0)
        earnings_metric_count = sum(
            int(pack.get("metric_count") or 0)
            for row in (evidence.get("official_earnings_packs") or {}).get("rows") or []
            for pack in row.get("packs") or []
        )
        industry_proxy_count = len((evidence.get("industry_supply_demand") or {}).get("rows") or [])
        return (
            f"共享快照 {snapshot.get('snapshot_id')}：{row_text}{suffix}。"
            f"已附 {technical_count} 组复算技术指标、{flow_count} 组资金流证据、"
            f"{financial_count} 组只读财报关键指标、{filing_count} 条 SEC 官方申报记录；"
            f"已附 {breakdown_count} 组主营构成；"
            f"已附 {ir_count} 条公司 IR 新闻稿；"
            f"已归一化 {earnings_pack_count} 个官方季度业绩材料包和 {earnings_metric_count} 条带定位指标；"
            f"已附 {industry_proxy_count} 组官方月度行业代理；"
            "仅供研究，不具备交易执行能力。"
        )


STORAGE_MARKET = StorageResearchMarketService()
