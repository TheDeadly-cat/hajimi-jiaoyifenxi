from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from backend.market.futu_readonly import (
    FutuUsMarketAdapter,
    STORAGE_SYMBOLS,
    quote_research_ready,
    validate_readonly_daily_history,
)
from backend.market.ir_releases import IR_FEEDS
from backend.market.storage_service import StorageResearchMarketService
from backend.market.technical_metrics import calculate_technical_metrics
from backend.orchestrator import DiscussionOrchestrator
from backend.providers.base import ProviderResponse
from tests.turn_contract_fixture import append_valid_turn_contract
from backend.store import StudioStore
from tests.storage_research_fixture import ready_storage_research_evidence


FIXED_NOW = datetime(2026, 7, 19, 20, 0, tzinfo=timezone.utc)


class FakeQuoteContext:
    def __init__(self, sdk: "FakeFutuSdk") -> None:
        self.sdk = sdk
        self.closed = False

    def get_market_snapshot(self, symbols: list[str]):
        self.sdk.snapshot_calls.append(symbols)
        if self.sdk.snapshot_error:
            return 1, "无行情权限"
        return 0, [
            {
                "code": symbol,
                "name": symbol.removeprefix("US."),
                "update_time": self.sdk.snapshot_update_time,
                "last_price": 100 + index,
                "open_price": 98 + index,
                "high_price": 101 + index,
                "low_price": 97 + index,
                "prev_close_price": 99 + index,
                "volume": 1000 + index,
                "turnover": 100000 + index,
                "bid_price": 99.9 + index,
                "ask_price": 100.1 + index,
                "sec_status": self.sdk.security_status,
                "suspension": self.sdk.suspended,
                "turnover_rate": 1.5 + index,
                "amplitude": 3.2 + index,
                "avg_price": 99.5 + index,
                "volume_ratio": 1.1 + index,
                "highest52weeks_price": 130 + index,
                "lowest52weeks_price": 70 + index,
                "equity_valid": True,
                "issued_shares": 1000000 + index,
                "total_market_val": 100000000 + index,
                "net_asset": 50000000 + index,
                "net_profit": 10000000 + index,
                "earning_per_share": 4.5 + index,
                "outstanding_shares": 900000 + index,
                "circular_market_val": 90000000 + index,
                "net_asset_per_share": 25 + index,
                "ey_ratio": 4.2 + index,
                "pe_ratio": 20 + index,
                "pb_ratio": 4 + index,
                "pe_ttm_ratio": 21 + index,
                "dividend_ttm": 0.5 + index,
                "dividend_ratio_ttm": 0.8 + index,
            }
            for index, symbol in enumerate(symbols)
        ]

    def get_market_state(self, symbols: list[str]):
        self.sdk.market_state_calls.append(symbols)
        if self.sdk.market_state_error:
            return 1, "market state unavailable"
        if not self.sdk.market_state:
            return 0, []
        return 0, [
            {
                "code": symbol,
                "stock_name": symbol.removeprefix("US."),
                "market_state": self.sdk.market_state,
            }
            for symbol in symbols
        ]

    def request_history_kline(self, symbol: str, **kwargs):
        self.sdk.history_calls.append({"symbol": symbol, **kwargs})
        if self.sdk.history_pages is not None:
            raw_page_key = kwargs.get("page_req_key")
            page_index = int(raw_page_key) if raw_page_key is not None else 0
            rows = self.sdk.history_pages[page_index]
            next_page_key = (
                str(page_index + 1)
                if page_index + 1 < len(self.sdk.history_pages)
                else None
            )
            return 0, rows, next_page_key
        return 0, [
            {"time_key": "2026-07-17 00:00:00", "open": 10, "close": 11, "high": 12, "low": 9, "volume": 10},
            {"time_key": "2026-07-18 00:00:00", "open": 11, "close": 12, "high": 13, "low": 10, "volume": 11},
            {"time_key": "2026-07-20 00:00:00", "open": 99, "close": 99, "high": 99, "low": 99, "volume": 0},
        ], None

    def get_capital_flow(self, symbol: str, **kwargs):
        self.sdk.flow_calls.append({"symbol": symbol, **kwargs})
        return 0, [
            {"capital_flow_item_time": "2026-07-17 00:00:00", "in_flow": 100, "main_in_flow": 60},
            {"capital_flow_item_time": "2026-07-18 00:00:00", "in_flow": -20, "main_in_flow": -10},
            {"capital_flow_item_time": "2026-07-20 00:00:00", "in_flow": 999, "main_in_flow": 999},
        ]

    def get_financials_statements(self, symbol: str, **kwargs):
        self.sdk.financial_calls.append({"symbol": symbol, **kwargs})
        return 0, {
            "next_key": "-1",
            "report_list": [
                {
                    "date_time_str": "2026-05-31",
                    "fiscal_year": 2026,
                    "financial_type": 2,
                    "period_text": "2026/Q2",
                    "currency_code": "USD",
                    "accounting_standards": "US GAAP",
                    "auditor_report": "",
                    "item_list": [
                        {"field_id": 5001, "display_name": "Revenue", "data": 1000, "yoy": 12.5},
                        {"field_id": 5002, "display_name": "Invalid", "data": float("nan")},
                    ],
                },
                {
                    "date_time_str": "2026-07-20",
                    "fiscal_year": 2026,
                    "financial_type": 3,
                    "period_text": "future",
                    "currency_code": "USD",
                    "item_list": [{"field_id": 9999, "display_name": "Future", "data": 999}],
                },
            ],
        }

    def get_financials_revenue_breakdown(self, symbol: str, **kwargs):
        self.sdk.revenue_breakdown_calls.append({"symbol": symbol, **kwargs})
        return 0, {
            "period": "2025/FY",
            "currency_code": "USD",
            "breakdown_list": [
                {
                    "type": 1,
                    "item_list": [
                        {"name": "NAND", "main_oper_income": 700, "ratio": 70},
                        {"name": "Invalid", "main_oper_income": float("nan"), "ratio": float("nan")},
                    ],
                },
                {
                    "type": 4,
                    "item_list": [{"name": "United States", "main_oper_income": 300, "ratio": 30}],
                },
            ],
            "screen_date_list": [
                {"date": 1_760_000_000, "period_text": "2025/FY", "financial_type": 7},
                {"date": 1_800_000_000, "period_text": "future", "financial_type": 7},
            ],
        }

    def close(self) -> None:
        self.closed = True
        self.sdk.close_calls += 1


class FakeFutuSdk:
    RET_OK = 0

    class KLType:
        K_DAY = "K_DAY"

    class AuType:
        QFQ = "QFQ"

    class PeriodType:
        DAY = "DAY"

    def __init__(
        self,
        *,
        snapshot_error: bool = False,
        snapshot_update_time: str = "2026-07-19 15:59:30",
        market_state: str | None = None,
        market_state_error: bool = False,
        history_pages: list[list[dict[str, object]]] | None = None,
        security_status: object = "NORMAL",
        suspended: bool = False,
    ) -> None:
        self.snapshot_error = snapshot_error
        self.snapshot_update_time = snapshot_update_time
        self.market_state = market_state
        self.market_state_error = market_state_error
        self.history_pages = history_pages
        self.security_status = security_status
        self.suspended = suspended
        self.open_calls = 0
        self.close_calls = 0
        self.snapshot_calls: list[list[str]] = []
        self.market_state_calls: list[list[str]] = []
        self.history_calls: list[dict[str, object]] = []
        self.flow_calls: list[dict[str, object]] = []
        self.financial_calls: list[dict[str, object]] = []
        self.revenue_breakdown_calls: list[dict[str, object]] = []

    def OpenQuoteContext(self, **_kwargs) -> FakeQuoteContext:
        self.open_calls += 1
        return FakeQuoteContext(self)


class FakeProvider:
    provider_id = "openai"

    def __init__(self) -> None:
        self.inputs: list[str] = []

    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        if "隐藏主持调度器" in instructions:
            return ProviderResponse(ok=True, provider=self.provider_id, model=model, content="使用安全回退")
        self.inputs.append(input_text)
        content = append_valid_turn_contract(
            "基于共享快照进行讨论。",
            instructions=instructions,
            input_text=input_text,
        )
        return ProviderResponse(ok=True, provider=self.provider_id, model=model, content=content)


class FakeRegistry:
    def __init__(self, provider: FakeProvider) -> None:
        self.provider = provider

    def get(self, _provider_id: str) -> FakeProvider:
        self.provider.provider_id = str(_provider_id or "openai")
        return self.provider


class FakeMarketService:
    def __init__(self) -> None:
        self.calls = 0
        self.raise_on_snapshot = False

    def snapshot(self) -> dict[str, object]:
        self.calls += 1
        if self.raise_on_snapshot:
            raise RuntimeError("Futu OpenD offline")
        return {
            "ok": True,
            "state": "ready",
            "snapshot_id": "same-snapshot-for-round",
            "captured_at": "2026-07-19T20:00:00Z",
            "source": "futu_opend",
            "rows": [
                {
                    "symbol": symbol,
                    "last": 100 + index,
                    "market_time": "2026-07-19 15:59:30",
                    "quality": "ready",
                    "age_seconds": 30,
                    "quote_is_live": True,
                    "freshness_basis": "live_20m_window",
                }
                for index, symbol in enumerate(STORAGE_SYMBOLS)
            ],
            "missing_symbols": [],
            "source_errors": [],
            "evidence": ready_storage_research_evidence(
                captured_at="2026-07-19T20:00:00Z",
                technical_as_of="2026-07-19 00:00:00",
            ),
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

    @staticmethod
    def prompt_context(snapshot: dict[str, object]) -> str:
        return f"snapshot_id={snapshot['snapshot_id']}"

    @staticmethod
    def timeline_summary(snapshot: dict[str, object]) -> str:
        return f"共享快照 {snapshot['snapshot_id']}"


class FakeOfficialIrAdapter:
    @staticmethod
    def status() -> dict[str, object]:
        return {"source": "official_company_ir", "state": "available"}

    @staticmethod
    def recent_releases_batch(symbols, **_kwargs) -> dict[str, object]:
        return {
            "ok": True,
            "source": "official_company_ir",
            "captured_at": "2026-07-19T20:00:00Z",
            "rows": [
                {
                    "symbol": symbol,
                    "publisher": f"{symbol} IR",
                    "releases": [{
                        "title": "Official update",
                        "event_type": "earnings_release",
                        "fiscal_period": "FY2026-Q3",
                        "fiscal_year": 2026,
                        "fiscal_quarter": 3,
                        "published_at": "2026-07-18T20:00:00Z",
                        "published_date": "2026-07-18",
                        "official_url": (
                            str(IR_FEEDS[symbol]["presentation_hub_url"]).rstrip("/")
                            + "/fixture-official-release"
                        ),
                        "summary": "Official company statement.",
                    }],
                }
                for symbol in symbols
            ],
            "source_errors": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
        }


class FakeSecEdgarAdapter:
    @staticmethod
    def status() -> dict[str, object]:
        return {"source": "sec_edgar_submissions", "state": "available"}

    @staticmethod
    def recent_filings_batch(symbols, **_kwargs) -> dict[str, object]:
        return {
            "ok": True,
            "source": "sec_edgar_submissions",
            "captured_at": "2026-07-19T20:00:00Z",
            "forms": ["10-K", "10-Q", "8-K"],
            "rows": [
                {
                    "symbol": symbol,
                    "cik": f"fixture-{index}",
                    "company_name": symbol,
                    "filings": [{
                        "form": "10-Q",
                        "filing_date": "2026-07-18",
                        "official_url": "https://example.invalid/filing",
                    }],
                }
                for index, symbol in enumerate(symbols)
            ],
            "source_errors": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
        }


class FakeOfficialEarningsMaterialsAdapter:
    @staticmethod
    def status() -> dict[str, object]:
        return {"source": "official_company_ir_materials", "state": "available"}

    @staticmethod
    def recent_materials_batch(symbols, **_kwargs) -> dict[str, object]:
        return {
            "ok": True,
            "state": "ready",
            "source": "official_company_ir_materials",
            "captured_at": "2026-07-19T20:00:00Z",
            "rows": [
                {
                    "symbol": symbol,
                    "publisher": f"{symbol} IR",
                    "hub_url": "https://example.invalid/presentations",
                    "technology_scope": [],
                    "quality": "ready",
                    "material_count": 0,
                    "materials": [],
                }
                for symbol in symbols
            ],
            "source_errors": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
        }


class FakeIndustryProxyAdapter:
    @staticmethod
    def status() -> dict[str, object]:
        return {"source": "fred_official_public_series", "state": "available"}

    @staticmethod
    def snapshot(**_kwargs) -> dict[str, object]:
        return {
            "ok": True,
            "state": "ready",
            "source": "fred_official_public_series",
            "captured_at": "2026-07-19T20:00:00Z",
            "rows": [{
                "series_id": "U34BVS",
                "label": "美国计算机存储设备制造商出货额",
                "scope": "device",
                "units": "百万美元",
                "as_of": "2026-06-01",
                "latest": 612,
                "change_12_observations_pct": 2.0,
            }],
            "derived": [],
            "source_errors": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
        }


class OfflineCoreAdapter:
    cache_ttl_seconds = 30

    def __init__(self) -> None:
        self.enrichment_calls = 0

    def quote_batch(self, symbols, **_kwargs) -> dict[str, object]:
        return {
            "ok": False,
            "state": "offline",
            "snapshot_id": "futu_offline_test",
            "source": "futu_opend",
            "market": "US",
            "symbols": list(symbols),
            "captured_at": "2026-07-19T20:00:00Z",
            "rows": [],
            "missing_symbols": list(symbols),
            "source_errors": [{
                "source": "futu_opend",
                "code": "FUTU_OPEND_OFFLINE",
                "message": "本机 Futu OpenD 未连接",
            }],
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

    def _unexpected(self, *_args, **_kwargs):
        self.enrichment_calls += 1
        raise AssertionError("核心行情离线时不得继续调用富途补充接口")

    daily_history_batch = _unexpected
    capital_flow_batch = _unexpected
    financial_statements_batch = _unexpected
    revenue_breakdown_batch = _unexpected


class FailIfCalledResearchAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def _unexpected(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("核心行情离线时不得继续调用外部补充来源")

    recent_filings_batch = _unexpected
    recent_releases_batch = _unexpected
    recent_materials_batch = _unexpected
    snapshot = _unexpected


class FutuReadOnlyAdapterTests(unittest.TestCase):
    def make_adapter(self, sdk: FakeFutuSdk) -> FutuUsMarketAdapter:
        return FutuUsMarketAdapter(
            sdk_module=sdk,
            socket_probe=lambda _host, _port: True,
            clock=lambda: FIXED_NOW,
            cache_ttl_seconds=30,
        )

    def test_installed_sdk_import_failure_is_not_reported_as_missing(self) -> None:
        adapter = FutuUsMarketAdapter(
            sdk_module=None,
            socket_probe=lambda _host, _port: False,
            clock=lambda: FIXED_NOW,
        )
        adapter._sdk_installed = True
        adapter._sdk_import_error = "PermissionError: log path unavailable"

        status = adapter.status()
        snapshot = adapter.quote_batch(["US.MU"])

        self.assertEqual(status["sdk_state"], "import_error")
        self.assertEqual(status["sdk_error"]["code"], "FUTU_SDK_IMPORT_ERROR")
        self.assertEqual(snapshot["source_errors"][0]["code"], "FUTU_SDK_IMPORT_ERROR")

    def test_storage_symbols_use_one_read_only_snapshot_call(self) -> None:
        sdk = FakeFutuSdk()
        payload = self.make_adapter(sdk).quote_batch(STORAGE_SYMBOLS)

        self.assertTrue(payload["ok"])
        self.assertEqual([row["symbol"] for row in payload["rows"]], list(STORAGE_SYMBOLS))
        self.assertEqual(sdk.snapshot_calls, [list(STORAGE_SYMBOLS)])
        self.assertEqual(sdk.open_calls, 1)
        self.assertEqual(sdk.close_calls, 1)
        self.assertFalse(payload["live_trading_allowed"])
        self.assertEqual(payload["execution_capability"], "none")
        self.assertEqual(payload["rows"][0]["pe_ttm_ratio"], 21)
        self.assertEqual(payload["rows"][0]["pb_ratio"], 4)
        self.assertEqual(payload["rows"][0]["total_market_value"], 100000000)

    def test_cache_prevents_duplicate_upstream_snapshot_calls(self) -> None:
        sdk = FakeFutuSdk()
        adapter = self.make_adapter(sdk)

        first = adapter.quote_batch(STORAGE_SYMBOLS)
        second = adapter.quote_batch(STORAGE_SYMBOLS)

        self.assertFalse(first["cache"]["hit"])
        self.assertTrue(second["cache"]["hit"])
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(len(sdk.snapshot_calls), 1)

    def test_upstream_failure_never_creates_fake_quotes(self) -> None:
        payload = self.make_adapter(FakeFutuSdk(snapshot_error=True)).quote_batch(STORAGE_SYMBOLS)

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["rows"], [])
        self.assertEqual(payload["missing_symbols"], list(STORAGE_SYMBOLS))
        self.assertEqual(payload["source_errors"][0]["code"], "FUTU_SNAPSHOT_FAILED")

    def test_future_market_timestamp_is_never_reported_ready(self) -> None:
        sdk = FakeFutuSdk(market_state="AFTER_HOURS_END")
        adapter = FutuUsMarketAdapter(
            sdk_module=sdk,
            socket_probe=lambda _host, _port: True,
            clock=lambda: datetime(2026, 7, 19, 19, 0, tzinfo=timezone.utc),
            cache_ttl_seconds=30,
        )

        payload = adapter.quote_batch(["US.MU"])

        self.assertEqual(payload["state"], "degraded")
        self.assertEqual(payload["rows"][0]["quality"], "future")
        self.assertLess(payload["rows"][0]["age_seconds"], 0)
        self.assertFalse(payload["rows"][0]["quote_is_live"])
        self.assertFalse(payload["rows"][0]["research_ready"])
        self.assertEqual(payload["rows"][0]["freshness_basis"], "future_timestamp")
        self.assertEqual(sdk.market_state_calls, [])

    def test_subsecond_future_market_timestamp_is_not_truncated_to_ready(self) -> None:
        adapter = FutuUsMarketAdapter(
            sdk_module=FakeFutuSdk(
                snapshot_update_time="2026-07-19 16:00:00.500",
            ),
            socket_probe=lambda _host, _port: True,
            clock=lambda: datetime(2026, 7, 19, 20, 0, tzinfo=timezone.utc),
            cache_ttl_seconds=30,
        )

        payload = adapter.quote_batch(["US.MU"])

        self.assertEqual(payload["state"], "degraded")
        self.assertEqual(payload["rows"][0]["quality"], "future")
        self.assertEqual(payload["rows"][0]["age_seconds"], -1)

    def test_quote_age_boundaries_are_explicit(self) -> None:
        cases = (
            (datetime(2026, 7, 19, 19, 59, 30, tzinfo=timezone.utc), 0, "ready"),
            (datetime(2026, 7, 19, 20, 19, 30, tzinfo=timezone.utc), 1200, "ready"),
            (datetime(2026, 7, 19, 20, 19, 31, tzinfo=timezone.utc), 1201, "stale"),
        )
        for captured_at, expected_age, expected_quality in cases:
            with self.subTest(expected_age=expected_age):
                payload = FutuUsMarketAdapter(
                    sdk_module=FakeFutuSdk(),
                    socket_probe=lambda _host, _port: True,
                    clock=lambda captured_at=captured_at: captured_at,
                    cache_ttl_seconds=30,
                ).quote_batch(["US.MU"])
                self.assertEqual(payload["rows"][0]["age_seconds"], expected_age)
                self.assertEqual(payload["rows"][0]["quality"], expected_quality)

    def test_explicit_freshness_contract_rejects_legacy_and_inconsistent_age(self) -> None:
        live = {
            "quality": "ready",
            "age_seconds": 60,
            "quote_is_live": True,
            "freshness_basis": "live_20m_window",
        }
        closed = {
            "quality": "ready",
            "age_seconds": 60_000,
            "quote_is_live": False,
            "market_state": "AFTER_HOURS_END",
            "freshness_basis": "closed_session_latest_snapshot",
        }

        self.assertTrue(quote_research_ready(live, actual_age_seconds=60.5))
        self.assertTrue(quote_research_ready(closed, actual_age_seconds=60_000.5))
        self.assertFalse(quote_research_ready({"quality": "ready"}))
        self.assertFalse(quote_research_ready(live, actual_age_seconds=3600))
        self.assertFalse(quote_research_ready({**closed, "market_state": "MORNING"}))

    def test_security_status_and_suspension_gate_quote_research_readiness(self) -> None:
        live = {
            "quality": "ready",
            "age_seconds": 60,
            "quote_is_live": True,
            "freshness_basis": "live_20m_window",
        }

        self.assertTrue(quote_research_ready(live))
        self.assertTrue(quote_research_ready({**live, "security_status": "NORMAL"}))
        self.assertTrue(quote_research_ready({**live, "security_status": "SecurityStatus.NORMAL"}))
        self.assertFalse(quote_research_ready({**live, "suspended": True}))
        self.assertFalse(quote_research_ready({**live, "security_status": "SecurityStatus.SUSPENDED"}))

    def test_adapter_preserves_security_state_and_blocks_abnormal_rows(self) -> None:
        for sdk, expected_status, expected_suspended, expected_ready in (
            (FakeFutuSdk(security_status="SecurityStatus.NORMAL"), "SecurityStatus.NORMAL", False, True),
            (FakeFutuSdk(security_status="DELISTED"), "DELISTED", False, False),
            (FakeFutuSdk(security_status="NORMAL", suspended=True), "NORMAL", True, False),
        ):
            with self.subTest(status=expected_status, suspended=expected_suspended):
                payload = self.make_adapter(sdk).quote_batch(["US.MU"])
                row = payload["rows"][0]

                self.assertEqual(row["security_status"], expected_status)
                self.assertEqual(row["suspended"], expected_suspended)
                self.assertEqual(row["research_ready"], expected_ready)
                self.assertEqual(quote_research_ready(row), expected_ready)
                self.assertEqual(payload["state"], "ready" if expected_ready else "degraded")
                self.assertEqual(payload["data_quality"]["ready"], int(expected_ready))

    def test_closed_session_security_status_cannot_bypass_research_gate(self) -> None:
        sdk = FakeFutuSdk(
            snapshot_update_time="2026-07-17 15:59:30",
            market_state="AFTER_HOURS_END",
            security_status="SecurityStatus.SUSPENDED",
        )
        payload = FutuUsMarketAdapter(
            sdk_module=sdk,
            socket_probe=lambda _host, _port: True,
            clock=lambda: datetime(2026, 7, 19, 20, 0, tzinfo=timezone.utc),
            cache_ttl_seconds=30,
        ).quote_batch(["US.MU"])

        row = payload["rows"][0]
        self.assertEqual(row["quality"], "ready")
        self.assertEqual(row["freshness_basis"], "closed_session_latest_snapshot")
        self.assertFalse(row["quote_is_live"])
        self.assertFalse(row["research_ready"])
        self.assertFalse(quote_research_ready(row))
        self.assertEqual(payload["state"], "degraded")
        self.assertEqual(payload["data_quality"]["ready"], 0)

    def test_weekend_closed_session_rows_are_research_ready_but_never_live(self) -> None:
        sdk = FakeFutuSdk(
            snapshot_update_time="2026-07-17 15:59:30",
            market_state="AFTER_HOURS_END",
        )
        payload = FutuUsMarketAdapter(
            sdk_module=sdk,
            socket_probe=lambda _host, _port: True,
            clock=lambda: datetime(2026, 7, 19, 20, 0, tzinfo=timezone.utc),
            cache_ttl_seconds=30,
        ).quote_batch(STORAGE_SYMBOLS)

        self.assertEqual(payload["state"], "ready")
        self.assertEqual(
            sdk.market_state_calls,
            [list(STORAGE_SYMBOLS)],
        )
        self.assertEqual(len(sdk.market_state_calls), 1)
        for row in payload["rows"]:
            self.assertEqual(row["quality"], "ready")
            self.assertTrue(row["research_ready"])
            self.assertFalse(row["quote_is_live"])
            self.assertEqual(row["market_state"], "AFTER_HOURS_END")
            self.assertEqual(row["freshness_basis"], "closed_session_latest_snapshot")

    def test_open_market_state_does_not_promote_a_stale_row(self) -> None:
        sdk = FakeFutuSdk(
            snapshot_update_time="2026-07-20 09:30:00",
            market_state="MORNING",
        )
        payload = FutuUsMarketAdapter(
            sdk_module=sdk,
            socket_probe=lambda _host, _port: True,
            clock=lambda: datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc),
            cache_ttl_seconds=30,
        ).quote_batch(["US.MU"])

        row = payload["rows"][0]
        self.assertEqual(payload["state"], "degraded")
        self.assertEqual(row["quality"], "stale")
        self.assertFalse(row["research_ready"])
        self.assertFalse(row["quote_is_live"])
        self.assertEqual(row["market_state"], "MORNING")
        self.assertEqual(row["freshness_basis"], "market_state_not_closed")
        self.assertEqual(sdk.market_state_calls, [["US.MU"]])

    def test_closed_session_row_older_than_96_hours_fails_closed(self) -> None:
        sdk = FakeFutuSdk(
            snapshot_update_time="2026-07-17 15:59:30",
            market_state="AFTER_HOURS_END",
        )
        payload = FutuUsMarketAdapter(
            sdk_module=sdk,
            socket_probe=lambda _host, _port: True,
            clock=lambda: datetime(2026, 7, 22, 20, 0, tzinfo=timezone.utc),
            cache_ttl_seconds=30,
        ).quote_batch(["US.MU"])

        row = payload["rows"][0]
        self.assertGreater(row["age_seconds"], 96 * 60 * 60)
        self.assertEqual(payload["state"], "degraded")
        self.assertEqual(row["quality"], "stale")
        self.assertFalse(row["research_ready"])
        self.assertFalse(row["quote_is_live"])
        self.assertEqual(row["market_state"], "AFTER_HOURS_END")
        self.assertEqual(row["freshness_basis"], "closed_session_age_exceeded")
        self.assertEqual(sdk.market_state_calls, [["US.MU"]])

    def test_missing_or_failed_market_state_keeps_stale_row(self) -> None:
        for sdk, expected_basis in (
            (FakeFutuSdk(), "market_state_missing"),
            (FakeFutuSdk(market_state_error=True), "market_state_lookup_failed"),
        ):
            with self.subTest(expected_basis=expected_basis):
                payload = FutuUsMarketAdapter(
                    sdk_module=sdk,
                    socket_probe=lambda _host, _port: True,
                    clock=lambda: datetime(2026, 7, 19, 20, 30, tzinfo=timezone.utc),
                    cache_ttl_seconds=30,
                ).quote_batch(["US.MU"])

                row = payload["rows"][0]
                self.assertEqual(payload["state"], "degraded")
                self.assertEqual(row["quality"], "stale")
                self.assertFalse(row["research_ready"])
                self.assertFalse(row["quote_is_live"])
                self.assertIsNone(row["market_state"])
                self.assertEqual(row["freshness_basis"], expected_basis)
                self.assertEqual(sdk.market_state_calls, [["US.MU"]])

    def test_closed_session_freshness_is_explicit_in_model_and_timeline_context(self) -> None:
        snapshot = {
            "snapshot_id": "futu_closed_session",
            "captured_at": "2026-07-19T20:00:00Z",
            "state": "ready",
            "rows": [{
                "symbol": "US.MU",
                "last": 100,
                "market_time": "2026-07-17 15:59:30",
                "updated_at": "2026-07-17T19:59:30Z",
                "age_seconds": 172830,
                "quality": "ready",
                "research_ready": True,
                "quote_is_live": False,
                "market_state": "AFTER_HOURS_END",
                "freshness_basis": "closed_session_latest_snapshot",
                "security_status": "SecurityStatus.NORMAL",
                "suspended": False,
            }],
            "missing_symbols": [],
            "source_errors": [],
            "evidence": {},
        }

        prompt = StorageResearchMarketService.prompt_context(snapshot)
        timeline = StorageResearchMarketService.timeline_summary(snapshot)

        self.assertIn('"quote_is_live": false', prompt)
        self.assertIn('"market_state": "AFTER_HOURS_END"', prompt)
        self.assertIn('"freshness_basis": "closed_session_latest_snapshot"', prompt)
        self.assertIn('"security_status": "SecurityStatus.NORMAL"', prompt)
        self.assertIn('"suspended": false', prompt)
        self.assertIn("quality=ready 只表示行情新鲜度可用于研究", prompt)
        self.assertIn("非实时研究截面", timeline)
        self.assertIn("market_state=AFTER_HOURS_END", timeline)
        self.assertIn("security_status=SecurityStatus.NORMAL", timeline)
        self.assertIn("suspended=false", timeline)

    def test_history_filters_future_rows_and_accepts_sndk(self) -> None:
        sdk = FakeFutuSdk()
        history = self.make_adapter(sdk).daily_history("US.SNDK", limit=20)

        self.assertTrue(history["ok"])
        self.assertEqual(len(history["rows"]), 2)
        self.assertEqual(history["symbol"], "US.SNDK")
        self.assertEqual(history["interval"], "1d")
        self.assertEqual(history["price_adjustment"], "QFQ")
        self.assertEqual(history["as_of_date"], "2026-07-19")
        self.assertEqual(history["last_completed_session"], "2026-07-18")
        self.assertEqual(history["actual_start"], "2026-07-17")
        self.assertEqual(history["actual_end"], "2026-07-18")
        self.assertTrue(history["captured_at"].endswith("Z"))
        self.assertTrue(all(
            row["market_time"][:10] < history["as_of_date"]
            for row in history["rows"]
        ))
        self.assertEqual(sdk.history_calls[0]["extended_time"], False)
        self.assertTrue(validate_readonly_daily_history(
            history,
            expected_symbol="US.SNDK",
        )["ready"])

    def test_daily_history_contract_rejects_provenance_safety_and_row_tampering(self) -> None:
        sdk = FakeFutuSdk()
        valid = self.make_adapter(sdk).daily_history("US.MU", limit=20)

        mutations = (
            {"source": "untrusted"},
            {"interval": "1m"},
            {"price_adjustment": "NONE"},
            {"source_errors": [{"code": "PARTIAL_PAGE"}]},
            {"execution_capability": "broker"},
            {"live_trading_allowed": True},
        )
        for mutation in mutations:
            candidate = {**valid, **mutation}
            with self.subTest(mutation=mutation):
                self.assertFalse(validate_readonly_daily_history(
                    candidate,
                    expected_symbol="US.MU",
                )["ready"])

        bad_row = copy.deepcopy(valid)
        bad_row["rows"][0]["symbol"] = "US.WDC"
        self.assertFalse(validate_readonly_daily_history(
            bad_row,
            expected_symbol="US.MU",
        )["ready"])

        unsafe = {
            **valid,
            "execution_capability": "broker",
            "live_trading_allowed": True,
        }
        unsafe_report = validate_readonly_daily_history(
            unsafe,
            expected_symbol="US.MU",
        )
        self.assertFalse(unsafe_report["safe_history"])
        self.assertEqual(unsafe_report["execution_capability"], "broker")
        self.assertTrue(unsafe_report["live_trading_allowed"])

    def test_daily_history_contract_bounds_rows_and_reported_issues(self) -> None:
        sdk = FakeFutuSdk()
        oversized = self.make_adapter(sdk).daily_history("US.MU", limit=20)
        oversized["rows"] = [None] * 501

        report = validate_readonly_daily_history(
            oversized,
            expected_symbol="US.MU",
        )

        self.assertFalse(report["ready"])
        self.assertEqual(report["row_count"], 501)
        self.assertEqual(report["rows_checked"], 500)
        self.assertIn("HISTORY_ROWS_LIMIT_EXCEEDED", report["issues"])
        self.assertLessEqual(len(report["issues"]), 64)
        self.assertTrue(report["issues_truncated"])

    def test_daily_history_batch_reuses_one_read_only_context(self) -> None:
        sdk = FakeFutuSdk()
        histories = self.make_adapter(sdk).daily_history_batch(STORAGE_SYMBOLS, limit=20)

        self.assertTrue(histories["ok"])
        self.assertEqual(len(sdk.history_calls), 4)
        self.assertEqual(sdk.open_calls, 1)
        self.assertEqual(sdk.close_calls, 1)
        self.assertTrue(all(len(item["rows"]) == 2 for item in histories["histories"].values()))

    def test_daily_history_follows_pagination_and_keeps_latest_completed_rows(self) -> None:
        sdk = FakeFutuSdk(history_pages=[
            [
                {"time_key": "2026-01-02 00:00:00", "open": 8, "close": 9, "high": 10, "low": 7, "volume": 8},
                {"time_key": "2026-01-05 00:00:00", "open": 9, "close": 10, "high": 11, "low": 8, "volume": 9},
            ],
            [
                {"time_key": "2026-07-17 00:00:00", "open": 10, "close": 11, "high": 12, "low": 9, "volume": 10},
                {"time_key": "2026-07-18 00:00:00", "open": 11, "close": 12, "high": 13, "low": 10, "volume": 11},
            ],
        ])

        history = self.make_adapter(sdk).daily_history("US.MU", limit=2)

        self.assertTrue(history["ok"])
        self.assertEqual(history["page_count"], 2)
        self.assertEqual(history["actual_start"], "2026-07-17")
        self.assertEqual(history["actual_end"], "2026-07-18")
        self.assertEqual(
            [row["market_time"][:10] for row in history["rows"]],
            ["2026-07-17", "2026-07-18"],
        )
        self.assertNotIn("page_req_key", sdk.history_calls[0])
        self.assertEqual(sdk.history_calls[1]["page_req_key"], "1")
        self.assertTrue(all(
            call["max_count"] == 500
            for call in sdk.history_calls
        ))

    def test_daily_history_repeated_page_key_fails_closed(self) -> None:
        class RepeatingPageQuoteContext(FakeQuoteContext):
            def request_history_kline(self, symbol: str, **kwargs):
                self.sdk.history_calls.append({"symbol": symbol, **kwargs})
                return 0, [{
                    "time_key": "2026-01-02 00:00:00",
                    "open": 8,
                    "close": 9,
                    "high": 10,
                    "low": 7,
                    "volume": 8,
                }], "repeated-page"

        class RepeatingPageSdk(FakeFutuSdk):
            def OpenQuoteContext(self, **_kwargs) -> FakeQuoteContext:
                self.open_calls += 1
                return RepeatingPageQuoteContext(self)

        sdk = RepeatingPageSdk()

        history = self.make_adapter(sdk).daily_history("US.MU", limit=2)

        self.assertFalse(history["ok"])
        self.assertEqual(history["page_count"], 2)
        self.assertEqual(len(sdk.history_calls), 2)
        self.assertIn(
            "FUTU_HISTORY_PAGINATION_LOOP",
            {error["code"] for error in history["source_errors"]},
        )

    def test_capital_flow_is_read_only_backward_looking_evidence(self) -> None:
        sdk = FakeFutuSdk()
        flows = self.make_adapter(sdk).capital_flow_batch(["US.MU"], limit_days=20)

        self.assertTrue(flows["ok"])
        self.assertEqual(len(flows["rows"]), 1)
        self.assertEqual(flows["rows"][0]["net_inflow_1d"], -20)
        self.assertEqual(flows["rows"][0]["net_inflow_5d"], 80)
        self.assertEqual(flows["rows"][0]["sample_count"], 2)
        self.assertEqual(sdk.flow_calls[0]["period_type"], "DAY")
        self.assertFalse(flows["live_trading_allowed"])

    def test_financial_statements_reuse_one_context_and_filter_future_periods(self) -> None:
        sdk = FakeFutuSdk()
        financials = self.make_adapter(sdk).financial_statements_batch(
            STORAGE_SYMBOLS,
            statement_type="main_index",
            limit=2,
        )

        self.assertTrue(financials["ok"])
        self.assertEqual(len(financials["rows"]), 4)
        self.assertEqual(len(sdk.financial_calls), 4)
        self.assertEqual(sdk.open_calls, 1)
        self.assertEqual(sdk.close_calls, 1)
        self.assertEqual(sdk.financial_calls[0]["statement_type"], 4)
        self.assertEqual(sdk.financial_calls[0]["financial_type"], 10)
        latest = financials["rows"][0]["reports"][-1]
        self.assertEqual(latest["period_end"], "2026-05-31")
        self.assertEqual(latest["items"], [{
            "field_id": 5001,
            "display_name": "Revenue",
            "data": 1000.0,
            "yoy": 12.5,
        }])
        self.assertFalse(financials["live_trading_allowed"])

    def test_financial_statement_type_is_strictly_validated(self) -> None:
        sdk = FakeFutuSdk()
        with self.assertRaisesRegex(ValueError, "财务报表类型"):
            self.make_adapter(sdk).financial_statements_batch(["US.MU"], statement_type="orders")
        self.assertEqual(sdk.open_calls, 0)

    def test_revenue_breakdown_preserves_period_currency_and_dimensions(self) -> None:
        sdk = FakeFutuSdk()
        breakdown = self.make_adapter(sdk).revenue_breakdown_batch(STORAGE_SYMBOLS)

        self.assertTrue(breakdown["ok"])
        self.assertEqual(len(breakdown["rows"]), 4)
        self.assertEqual(len(sdk.revenue_breakdown_calls), 4)
        self.assertEqual(sdk.open_calls, 1)
        self.assertEqual(sdk.close_calls, 1)
        first = breakdown["rows"][0]
        self.assertEqual(first["period"], "2025/FY")
        self.assertEqual(first["currency_code"], "USD")
        self.assertEqual([item["type"] for item in first["dimensions"]], ["product", "region"])
        self.assertEqual(first["dimensions"][0]["items"], [{
            "name": "NAND",
            "operating_revenue": 700.0,
            "ratio_pct": 70.0,
        }])
        self.assertEqual(len(first["available_periods"]), 1)
        self.assertFalse(breakdown["live_trading_allowed"])

    def test_market_package_contains_no_trade_context_or_order_api(self) -> None:
        market_dir = Path(__file__).resolve().parents[1] / "backend" / "market"
        source = "\n".join(path.read_text(encoding="utf-8") for path in market_dir.glob("*.py"))
        for forbidden in ["OpenSecTradeContext", "OpenUSTradeContext", "place_order", "unlock_trade"]:
            self.assertNotIn(forbidden, source)


class TechnicalMetricTests(unittest.TestCase):
    @staticmethod
    def rows(count: int) -> list[dict[str, object]]:
        return [
            {
                "time": f"2026-01-{index + 1:02d}",
                "market_time": f"2026-01-{index + 1:02d} 00:00:00",
                "close": 100 + index,
                "volume": 200 if index == count - 1 else 100,
            }
            for index in range(count)
        ]

    def test_metrics_are_deterministic_and_backward_looking(self) -> None:
        metrics = calculate_technical_metrics("US.MU", self.rows(60))

        self.assertEqual(metrics["quality"], "ready")
        self.assertEqual(metrics["sample_count"], 60)
        self.assertAlmostEqual(metrics["return_1d_pct"], (159 / 158 - 1) * 100, places=6)
        self.assertAlmostEqual(metrics["return_20d_pct"], (159 / 139 - 1) * 100, places=6)
        self.assertEqual(metrics["sma20"], 149.5)
        self.assertEqual(metrics["sma50"], 134.5)
        self.assertEqual(metrics["rsi14"], 100)
        self.assertEqual(metrics["latest_volume_ratio_20d"], 2)
        self.assertEqual(metrics["max_drawdown_pct"], 0)

    def test_insufficient_history_is_explicit_not_fabricated(self) -> None:
        metrics = calculate_technical_metrics("US.SNDK", self.rows(2))

        self.assertEqual(metrics["quality"], "limited")
        self.assertIsNone(metrics["return_5d_pct"])
        self.assertIsNone(metrics["sma20"])
        self.assertIsNone(metrics["rsi14"])
        self.assertIn("sma20", metrics["missing_metrics"])


class StorageResearchEvidenceTests(unittest.TestCase):
    def test_offline_core_market_returns_complete_evidence_without_secondary_probes(self) -> None:
        core_adapter = OfflineCoreAdapter()
        external_adapter = FailIfCalledResearchAdapter()
        service = StorageResearchMarketService(
            core_adapter,
            sec_adapter=external_adapter,
            ir_adapter=external_adapter,
            earnings_materials_adapter=external_adapter,
            industry_adapter=external_adapter,
        )

        snapshot = service.snapshot(force=True)

        self.assertFalse(snapshot["ok"])
        self.assertEqual(snapshot["state"], "offline")
        self.assertEqual(snapshot["evidence"]["state"], "degraded")
        self.assertEqual(snapshot["evidence"]["enrichment"]["state"], "skipped")
        self.assertEqual(
            snapshot["evidence"]["enrichment"]["reason_code"],
            "RESEARCH_ENRICHMENT_SKIPPED_CORE_MARKET_OFFLINE",
        )
        self.assertEqual(len(snapshot["evidence"]["technical"]["rows"]), 4)
        self.assertTrue(all(
            row["quality"] == "unavailable"
            for row in snapshot["evidence"]["technical"]["rows"]
        ))
        self.assertEqual(snapshot["evidence"]["official_filings"]["state"], "skipped")
        self.assertEqual(snapshot["evidence"]["industry_supply_demand"]["state"], "skipped")
        self.assertEqual(snapshot["execution_capability"], "none")
        self.assertFalse(snapshot["live_trading_allowed"])
        self.assertEqual(core_adapter.enrichment_calls, 0)
        self.assertEqual(external_adapter.calls, 0)

    def test_snapshot_freezes_quote_fundamental_technical_and_flow_evidence(self) -> None:
        sdk = FakeFutuSdk()
        adapter = FutuUsMarketAdapter(
            sdk_module=sdk,
            socket_probe=lambda _host, _port: True,
            clock=lambda: FIXED_NOW,
            cache_ttl_seconds=30,
        )
        service = StorageResearchMarketService(
            adapter,
            sec_adapter=FakeSecEdgarAdapter(),
            ir_adapter=FakeOfficialIrAdapter(),
            earnings_materials_adapter=FakeOfficialEarningsMaterialsAdapter(),
            industry_adapter=FakeIndustryProxyAdapter(),
        )

        first = service.snapshot()
        second = service.snapshot()

        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertFalse(first["research_cache"]["hit"])
        self.assertTrue(second["research_cache"]["hit"])
        self.assertEqual(len(first["evidence"]["fundamental"]["rows"]), 4)
        self.assertEqual(len(first["evidence"]["technical"]["rows"]), 4)
        self.assertEqual(len(first["evidence"]["capital_flow"]["rows"]), 4)
        self.assertEqual(len(first["evidence"]["financial_statements"]["rows"]), 4)
        self.assertEqual(len(first["evidence"]["revenue_breakdown"]["rows"]), 4)
        self.assertEqual(len(first["evidence"]["company_ir_releases"]["rows"]), 4)
        self.assertEqual(first["evidence"]["official_earnings_packs"]["version"], "official_earnings_pack_v1")
        self.assertEqual(len(first["evidence"]["official_earnings_packs"]["rows"]), 4)
        self.assertEqual(first["evidence"]["official_earnings_packs"]["state"], "ready")
        self.assertTrue(first["evidence"]["structure_ready"])
        self.assertEqual(len(first["evidence"]["official_earnings_materials"]["rows"]), 4)
        self.assertEqual(len(first["evidence"]["industry_supply_demand"]["rows"]), 1)
        self.assertEqual(first["evidence"]["research_analytics"]["version"], "storage_research_analytics_v1")
        self.assertEqual(first["evidence"]["research_analytics"]["execution_capability"], "none")
        self.assertIn("research_analytics=", service.prompt_context(first))
        self.assertIn("不等同于新闻", first["evidence"]["capital_flow"]["interpretation"])
        self.assertEqual(sdk.snapshot_calls, [list(STORAGE_SYMBOLS)])
        self.assertEqual(len(sdk.history_calls), 4)
        self.assertEqual(len(sdk.flow_calls), 4)
        self.assertEqual(len(sdk.financial_calls), 4)
        self.assertEqual(len(sdk.revenue_breakdown_calls), 4)

    def test_nested_supplemental_source_error_forces_composite_state_degraded(self) -> None:
        class IndustryWithError(FakeIndustryProxyAdapter):
            @staticmethod
            def snapshot(**_kwargs) -> dict[str, object]:
                payload = FakeIndustryProxyAdapter.snapshot()
                payload["source_errors"] = [{
                    "source": "fred",
                    "code": "FIXTURE_PARTIAL_ERROR",
                    "message": "部分序列失败",
                }]
                return payload

        service = StorageResearchMarketService(
            FutuUsMarketAdapter(
                sdk_module=FakeFutuSdk(),
                socket_probe=lambda _host, _port: True,
                clock=lambda: FIXED_NOW,
                cache_ttl_seconds=30,
            ),
            sec_adapter=FakeSecEdgarAdapter(),
            ir_adapter=FakeOfficialIrAdapter(),
            earnings_materials_adapter=FakeOfficialEarningsMaterialsAdapter(),
            industry_adapter=IndustryWithError(),
        )

        snapshot = service.snapshot()

        self.assertEqual(snapshot["state"], "ready")
        self.assertEqual(snapshot["evidence"]["state"], "degraded")

    def test_missing_earnings_pack_coverage_fails_composite_structure(self) -> None:
        class MissingPackIrAdapter(FakeOfficialIrAdapter):
            @staticmethod
            def recent_releases_batch(symbols, **kwargs) -> dict[str, object]:
                payload = FakeOfficialIrAdapter.recent_releases_batch(symbols, **kwargs)
                payload["rows"][0]["releases"] = []
                return payload

        service = StorageResearchMarketService(
            FutuUsMarketAdapter(
                sdk_module=FakeFutuSdk(),
                socket_probe=lambda _host, _port: True,
                clock=lambda: FIXED_NOW,
                cache_ttl_seconds=30,
            ),
            sec_adapter=FakeSecEdgarAdapter(),
            ir_adapter=MissingPackIrAdapter(),
            earnings_materials_adapter=FakeOfficialEarningsMaterialsAdapter(),
            industry_adapter=FakeIndustryProxyAdapter(),
        )

        snapshot = service.snapshot()

        self.assertFalse(snapshot["evidence"]["structure_ready"])
        self.assertEqual(snapshot["evidence"]["state"], "degraded")
        self.assertEqual(snapshot["evidence"]["official_earnings_packs"]["state"], "partial")
        self.assertEqual(snapshot["evidence"]["official_earnings_packs"]["missing_symbols"], ["US.MU"])


class SharedSnapshotOrchestratorTests(unittest.TestCase):
    def test_offline_storage_market_blocks_round_without_persistent_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StudioStore(Path(temp_dir) / "studio.sqlite3")
            room = store.room_snapshot("room_storage")["room"]
            store.update_room("room_storage", {
                "expected_settings_version": room["settings_version"],
                "capability_pack_ids": ["storage_research_readonly"],
            })
            provider = FakeProvider()
            market_service = FakeMarketService()
            market_service.raise_on_snapshot = True
            orchestrator = DiscussionOrchestrator(store, FakeRegistry(provider), market_service)
            member_ids = [member["id"] for member in store.room_snapshot("room_storage")["members"][:2]]
            before = store.room_snapshot("room_storage")

            events = list(orchestrator.run_round("room_storage", "离线时不得创建轮次", member_ids))
            after = store.room_snapshot("room_storage")

        self.assertEqual(market_service.calls, 1)
        self.assertEqual(provider.inputs, [])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["code"], "ROUND_MARKET_PREFLIGHT_FAILED")
        self.assertFalse(events[0]["preflight"]["ready"])
        self.assertEqual(
            events[0]["preflight"]["capture_error"]["code"],
            "MARKET_SERVICE_ERROR",
        )
        self.assertEqual(after["latest_round"], before["latest_round"])
        self.assertEqual(after["round_checkpoint"], before["round_checkpoint"])
        self.assertEqual(after["messages"], before["messages"])

    def test_all_storage_room_agents_receive_one_shared_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StudioStore(Path(temp_dir) / "studio.sqlite3")
            provider = FakeProvider()
            market_service = FakeMarketService()
            orchestrator = DiscussionOrchestrator(store, FakeRegistry(provider), market_service)
            member_ids = [member["id"] for member in store.room_snapshot("room_storage")["members"][:2]]

            events = list(orchestrator.run_round("room_storage", "比较四家公司", member_ids))

        self.assertEqual(market_service.calls, 1)
        self.assertTrue(provider.inputs)
        self.assertTrue(all("same-snapshot-for-round" in input_text for input_text in provider.inputs))
        market_events = [event for event in events if event["type"] == "market_snapshot"]
        self.assertEqual(len(market_events), 1)
        self.assertFalse(market_events[0]["snapshot"]["live_trading_allowed"])

    def test_prefetched_ready_snapshot_is_reused_without_second_futu_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StudioStore(Path(temp_dir) / "studio.sqlite3")
            provider = FakeProvider()
            market_service = FakeMarketService()
            orchestrator = DiscussionOrchestrator(store, FakeRegistry(provider), market_service)
            member_ids = [member["id"] for member in store.room_snapshot("room_storage")["members"][:2]]

            preflight, frozen_snapshot = orchestrator.preflight_market("room_storage")
            events = list(orchestrator.run_round(
                "room_storage",
                "复用会前行情",
                member_ids,
                prefetched_market_snapshot=frozen_snapshot,
            ))

        self.assertTrue(preflight["ready"])
        self.assertEqual(market_service.calls, 1)
        self.assertTrue(any(event["type"] == "round_started" for event in events))
        self.assertTrue(provider.inputs)

    def test_non_storage_room_never_calls_market_service(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StudioStore(Path(temp_dir) / "studio.sqlite3")
            provider = FakeProvider()
            market_service = FakeMarketService()
            market_service.raise_on_snapshot = True
            orchestrator = DiscussionOrchestrator(store, FakeRegistry(provider), market_service)
            member_ids = [member["id"] for member in store.room_snapshot("room_plan")["members"][:2]]

            events = list(orchestrator.run_round("room_plan", "通用房间不需要行情", member_ids))
            checkpoint_summary = store.room_snapshot("room_plan")["round_checkpoint"]

        self.assertEqual(market_service.calls, 0)
        self.assertTrue(any(event["type"] == "round_started" for event in events))
        self.assertFalse(any(event.get("code") == "ROUND_MARKET_PREFLIGHT_FAILED" for event in events))
        self.assertTrue(provider.inputs)
        self.assertFalse(checkpoint_summary["frozen_market"]["present"])
        self.assertFalse(checkpoint_summary["frozen_market"]["ready"])

    def test_resume_reuses_frozen_market_and_material_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StudioStore(Path(temp_dir) / "studio.sqlite3")
            room = store.room_snapshot("room_storage")["room"]
            store.update_room("room_storage", {
                "expected_settings_version": room["settings_version"],
                "capability_pack_ids": ["storage_research_readonly"],
            })
            provider = FakeProvider()
            market_service = FakeMarketService()
            orchestrator = DiscussionOrchestrator(store, FakeRegistry(provider), market_service)
            member_ids = [member["id"] for member in store.room_snapshot("room_storage")["members"][:3]]
            stream = orchestrator.run_round("room_storage", "冻结本轮证据", member_ids)
            for event in stream:
                if event["type"] == "message":
                    break
            stream.close()
            paused = store.room_snapshot("room_storage")
            round_id = paused["latest_round"]["id"]
            store.add_material("room_storage", {
                "title": "暂停后新增资料",
                "content": "resume-new-material-must-not-enter-frozen-round",
            })
            market_service.raise_on_snapshot = True

            resumed = list(orchestrator.run_round(
                "room_storage",
                "不能覆盖原目标",
                resume_round_id=round_id,
            ))

        self.assertEqual(market_service.calls, 1)
        self.assertEqual(paused["round_checkpoint"]["frozen_market"], {
            "present": True,
            "ready": True,
            "state": "ready",
            "snapshot_id": "same-snapshot-for-round",
            "captured_at": "2026-07-19T20:00:00Z",
        })
        resumed_market = [event for event in resumed if event["type"] == "market_snapshot"]
        self.assertEqual(resumed_market[0]["snapshot"]["snapshot_id"], "same-snapshot-for-round")
        self.assertTrue(all("resume-new-material" not in input_text for input_text in provider.inputs))

    def test_resume_old_offline_checkpoint_fails_closed_without_live_recapture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StudioStore(Path(temp_dir) / "studio.sqlite3")
            provider = FakeProvider()
            market_service = FakeMarketService()
            orchestrator = DiscussionOrchestrator(store, FakeRegistry(provider), market_service)
            members = store.enabled_members("room_storage")[:2]
            round_row = store.create_round("room_storage", "旧版离线检查点")
            store.add_message(
                "room_storage",
                sender_type="user",
                sender_id="user",
                sender_name="我",
                content=round_row["objective"],
                round_id=round_row["id"],
            )
            store.save_round_checkpoint("room_storage", round_row["id"], {
                "member_ids": [member["id"] for member in members],
                "next_order": 1,
                "max_turns": len(members),
                "shared_context": "legacy-offline-context",
                "market_snapshot": {
                    "ok": False,
                    "state": "offline",
                    "source": "futu_opend",
                    "snapshot_id": "legacy-offline-snapshot",
                    "captured_at": "2026-07-18T20:00:00Z",
                    "rows": [],
                    "missing_symbols": list(STORAGE_SYMBOLS),
                    "source_errors": [{
                        "code": "FUTU_OPEND_OFFLINE",
                        "message": "OpenD was offline when this round was frozen.",
                    }],
                    "execution_capability": "none",
                    "live_trading_allowed": False,
                },
            })
            store.complete_round(round_row["id"], "PAUSED")
            before = store.room_snapshot("room_storage")
            market_service.raise_on_snapshot = True

            events = list(orchestrator.run_round(
                "room_storage",
                "不得覆盖旧目标",
                resume_round_id=round_row["id"],
            ))
            after = store.room_snapshot("room_storage")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["code"], "ROUND_MARKET_PREFLIGHT_FAILED")
        self.assertEqual(
            events[0]["preflight"]["snapshot_origin"],
            "frozen_checkpoint",
        )
        self.assertEqual(market_service.calls, 0)
        self.assertEqual(provider.inputs, [])
        self.assertEqual(after["latest_round"]["status"], "PAUSED")
        self.assertEqual(after["latest_round"]["resume_count"], 0)
        self.assertEqual(after["messages"], before["messages"])


if __name__ == "__main__":
    unittest.main()
