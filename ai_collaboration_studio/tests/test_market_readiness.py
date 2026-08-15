import copy
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

from backend import http_server
from backend.market.futu_readonly import STORAGE_SYMBOLS
from backend.market.ir_releases import IR_FEEDS
from backend.market.readiness import StorageResearchReadinessService
from backend.market.storage_service import StorageResearchMarketService


class FakeFutuAdapter:
    def __init__(self, *, online: bool) -> None:
        self.online = online
        self.quote_calls = 0
        self.explicit_freshness = True
        self.quote_mutator = None

    def status(self):
        return {
            "configured": True,
            "sdk_available": True,
            "sdk_installed": True,
            "sdk_state": "ready",
            "sdk_error": None,
            "opend_reachable": self.online,
            "state": "ready" if self.online else "offline",
            "host": "127.0.0.1",
            "port": 11111,
            "source": "futu_opend",
            "execution_capability": "none",
            "live_trading_allowed": False,
            "allowed_symbols": list(STORAGE_SYMBOLS),
        }

    def quote_batch(self, symbols, *, force=False):
        self.quote_calls += 1
        if not self.online:
            raise AssertionError("OpenD 离线时 readiness 不得调用 Futu quote_batch")
        payload = {
            "ok": True,
            "state": "ready",
            "source": "futu_opend",
            "snapshot_id": "futu_readiness_test",
            "captured_at": "2026-08-01T12:00:00Z",
            "rows": [
                {
                    "symbol": symbol,
                    "quality": "ready",
                    "last": 100 + index,
                    "market_time": "2026-08-01T11:59:00Z",
                    **({
                        "age_seconds": 60,
                        "quote_is_live": True,
                        "freshness_basis": "live_20m_window",
                    } if self.explicit_freshness else {}),
                }
                for index, symbol in enumerate(symbols)
            ],
            "missing_symbols": [],
            "source_errors": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
            "cache": {"forced": force},
        }
        if self.quote_mutator:
            self.quote_mutator(payload)
        return payload


class FakeSecAdapter:
    def __init__(self, *, configured: bool) -> None:
        self.configured = configured
        self.calls = 0

    def status(self):
        return {
            "source": "sec_edgar",
            "configured": self.configured,
            "state": "ready" if self.configured else "unconfigured",
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

    def recent_filings_batch(self, symbols, **_kwargs):
        self.calls += 1
        if not self.configured:
            return {
                "ok": False,
                "state": "unconfigured",
                "rows": [],
                "source_errors": [{
                    "source": "sec_edgar",
                    "code": "SEC_USER_AGENT_REQUIRED",
                    "message": "需要声明产品或组织名与联系邮箱",
                }],
                "execution_capability": "none",
                "live_trading_allowed": False,
            }
        return {
            "ok": True,
            "state": "ready",
            "rows": [
                {"symbol": symbol, "filings": [{"form": "10-Q", "official_url": f"https://sec.test/{symbol}"}]}
                for symbol in symbols
            ],
            "source_errors": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
        }


class FakeIrAdapter:
    def status(self):
        return {
            "source": "official_company_ir",
            "configured": True,
            "state": "available",
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

    def recent_releases_batch(self, symbols, **_kwargs):
        return {
            "ok": True,
            "state": "ready",
            "symbols": list(symbols),
            "rows": [
                {
                    "symbol": symbol,
                    "publisher": symbol,
                    "releases": [{
                        "title": f"{symbol} quarterly results",
                        "official_url": (
                            str(IR_FEEDS[symbol]["presentation_hub_url"]).rstrip("/")
                            + "/fixture-quarterly-results"
                        ),
                        "published_at": "2026-07-20T20:00:00Z",
                        "event_type": "earnings_release",
                        "fiscal_period": "FY2026-Q3",
                    }],
                }
                for symbol in symbols
            ],
            "source_errors": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
        }


class FakeMaterialsAdapter:
    def __init__(self, *, limited: bool) -> None:
        self.limited = limited

    def status(self):
        return {
            "source": "official_company_ir_materials",
            "configured": True,
            "state": "available",
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

    def recent_materials_batch(self, symbols, **_kwargs):
        return {
            "ok": True,
            "state": "partial" if self.limited else "ready",
            "rows": [
                {
                    "symbol": symbol,
                    "materials": [{
                        "fiscal_period": "FY2026-Q3",
                        "material_kind": "earnings_presentation",
                        "official_url": f"https://ir.test/{symbol}/deck",
                    }],
                }
                for symbol in symbols
            ],
            "source_errors": ([{
                "source": "official_company_ir_materials",
                "code": "EARNINGS_MATERIAL_HUB_ERROR",
                "message": "官方材料中心当前受限，使用已核验索引回退",
            }] if self.limited else []),
            "execution_capability": "none",
            "live_trading_allowed": False,
        }


class FakeIndustryAdapter:
    def status(self):
        return {
            "source": "fred_official_public_series",
            "configured": True,
            "state": "available",
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

    def snapshot(self, **_kwargs):
        return {
            "ok": True,
            "state": "ready",
            "series_count": 5,
            "rows": [{"series_id": f"series-{index}"} for index in range(5)],
            "derived": [{"metric": "inventory_proxy"}],
            "source_errors": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
        }


class ConcurrentCallGate:
    def __init__(self, expected: int) -> None:
        self.expected = expected
        self.count = 0
        self.lock = threading.Lock()
        self.all_entered = threading.Event()

    def enter(self) -> None:
        with self.lock:
            self.count += 1
            if self.count >= self.expected:
                self.all_entered.set()
        if not self.all_entered.wait(timeout=2):
            raise AssertionError("独立只读证据源没有并行启动")


class CoordinatedSecAdapter(FakeSecAdapter):
    def __init__(self, gate: ConcurrentCallGate) -> None:
        super().__init__(configured=True)
        self.gate = gate

    def recent_filings_batch(self, symbols, **kwargs):
        self.gate.enter()
        return super().recent_filings_batch(symbols, **kwargs)


class CoordinatedIrAdapter(FakeIrAdapter):
    def __init__(self, gate: ConcurrentCallGate) -> None:
        self.gate = gate

    def recent_releases_batch(self, symbols, **kwargs):
        self.gate.enter()
        return super().recent_releases_batch(symbols, **kwargs)


class CoordinatedMaterialsAdapter(FakeMaterialsAdapter):
    def __init__(self, gate: ConcurrentCallGate) -> None:
        super().__init__(limited=False)
        self.gate = gate

    def recent_materials_batch(self, symbols, **kwargs):
        self.gate.enter()
        return super().recent_materials_batch(symbols, **kwargs)


class CoordinatedIndustryAdapter(FakeIndustryAdapter):
    def __init__(self, gate: ConcurrentCallGate) -> None:
        self.gate = gate

    def snapshot(self, **kwargs):
        self.gate.enter()
        return super().snapshot(**kwargs)


def build_service(*, online: bool, sec_configured: bool, materials_limited: bool):
    futu = FakeFutuAdapter(online=online)
    market = StorageResearchMarketService(
        futu,
        sec_adapter=FakeSecAdapter(configured=sec_configured),
        ir_adapter=FakeIrAdapter(),
        earnings_materials_adapter=FakeMaterialsAdapter(limited=materials_limited),
        industry_adapter=FakeIndustryAdapter(),
    )
    return futu, market, StorageResearchReadinessService(market)


class StorageResearchReadinessTests(unittest.TestCase):
    def assert_round_blocked_with(
        self,
        payload,
        code: str,
    ) -> None:
        self.assertFalse(payload["round_admission"]["ready"])
        self.assertEqual(payload["round_admission"]["state"], "blocked")
        self.assertEqual(payload["round_admission"]["reason_code"], code)
        futu_source = next(
            source for source in payload["sources"]
            if source["id"] == "futu_opend"
        )
        self.assertFalse(futu_source["ready"])
        self.assertIn(code, futu_source["error_codes"])

    def test_independent_sources_start_concurrently(self) -> None:
        gate = ConcurrentCallGate(expected=4)
        market = StorageResearchMarketService(
            FakeFutuAdapter(online=False),
            sec_adapter=CoordinatedSecAdapter(gate),
            ir_adapter=CoordinatedIrAdapter(gate),
            earnings_materials_adapter=CoordinatedMaterialsAdapter(gate),
            industry_adapter=CoordinatedIndustryAdapter(gate),
        )

        payload = market.independent_evidence(force=True)

        self.assertEqual(gate.count, 4)
        self.assertEqual(payload["state"], "ready")
        self.assertEqual(payload["coverage"]["official_filings"], sorted(STORAGE_SYMBOLS))
        self.assertEqual(payload["coverage"]["company_ir_releases"], sorted(STORAGE_SYMBOLS))
        self.assertTrue(payload["coverage"]["industry_supply_demand"])

    def test_opend_offline_still_prepares_independent_public_evidence(self) -> None:
        futu, market, readiness_service = build_service(
            online=False,
            sec_configured=False,
            materials_limited=True,
        )

        payload = readiness_service.inspect(force=True)

        self.assertEqual(futu.quote_calls, 0)
        self.assertFalse(payload["round_admission"]["ready"])
        self.assertEqual(payload["round_admission"]["coverage_ready"], 0)
        self.assertTrue(payload["convergence_readiness"]["preparation_usable"])
        sources = {source["id"]: source for source in payload["sources"]}
        self.assertEqual(sources["company_ir"]["coverage_ready"], 4)
        self.assertEqual(sources["company_ir"]["state"], "ready")
        self.assertEqual(sources["company_ir"]["group"], "convergence")
        self.assertEqual(sources["earnings_materials"]["coverage_ready"], 4)
        self.assertEqual(sources["earnings_materials"]["state"], "partial")
        self.assertEqual(sources["earnings_materials"]["group"], "convergence")
        self.assertEqual(sources["sec_edgar"]["coverage_ready"], 0)
        self.assertEqual(sources["sec_edgar"]["state"], "blocked")
        self.assertEqual(sources["industry_proxies"]["coverage_ready"], 5)
        self.assertEqual(sources["industry_proxies"]["group"], "convergence")
        expected_blocker_ids = [
            source["id"]
            for source in payload["sources"]
            if source["group"] in {"round_admission", "convergence"}
            and not source["ready"]
        ]
        blockers = payload["convergence_readiness"]["blockers"]
        self.assertEqual(
            [blocker["source_id"] for blocker in blockers],
            expected_blocker_ids,
        )
        earnings_blocker = next(
            blocker for blocker in blockers
            if blocker["source_id"] == "earnings_materials"
        )
        self.assertEqual(
            earnings_blocker["error_codes"],
            ["EARNINGS_MATERIAL_HUB_ERROR"],
        )
        self.assertTrue(payload["safety"]["ready"])
        self.assertEqual(payload["safety"]["execution_capability"], "none")
        self.assertFalse(payload["safety"]["live_trading_allowed"])
        encoded = json.dumps(payload, ensure_ascii=False).lower()
        self.assertNotIn("api_key", encoded)
        self.assertNotIn("authorization", encoded)
        self.assertNotIn("bearer ", encoded)

        independent = market.independent_evidence(force=True)
        self.assertEqual(independent["state"], "partial")
        self.assertEqual(independent["coverage"]["company_ir_releases"], sorted(STORAGE_SYMBOLS))
        self.assertEqual(independent["coverage"]["official_earnings_packs"], sorted(STORAGE_SYMBOLS))

    def test_full_ready_requires_four_quotes_and_clean_independent_sources(self) -> None:
        futu, _market, readiness_service = build_service(
            online=True,
            sec_configured=True,
            materials_limited=False,
        )

        payload = readiness_service.inspect(force=True)

        self.assertEqual(futu.quote_calls, 1)
        self.assertTrue(payload["round_admission"]["ready"])
        self.assertTrue(payload["convergence_readiness"]["ready"])
        self.assertTrue(payload["safety"]["ready"])
        self.assertTrue(payload["ready"])

    def test_nested_manual_state_without_room_bound_envelope_fails_closed(self) -> None:
        for source_key in ("official_earnings_packs", "official_earnings_materials"):
            with self.subTest(source_key=source_key):
                _futu, market, readiness_service = build_service(
                    online=True,
                    sec_configured=True,
                    materials_limited=False,
                )
                independent = market.independent_evidence(force=True)
                independent["evidence"]["state"] = "ready"
                independent["evidence"].pop("manual_official_evidence", None)
                independent["evidence"][source_key]["state"] = "ready_with_manual_substitution"
                market.independent_evidence = lambda **_kwargs: copy.deepcopy(independent)

                payload = readiness_service.inspect(
                    force=True,
                    room_snapshot={"room": {"id": "room_storage"}},
                )
                earnings_source = next(
                    source for source in payload["sources"]
                    if source["id"] == "earnings_materials"
                )
                self.assertFalse(earnings_source["ready"])
                self.assertIn(
                    "MANUAL_OFFICIAL_EVIDENCE_INVALID",
                    earnings_source["error_codes"],
                )
                self.assertFalse(payload["convergence_readiness"]["ready"])

    def test_legacy_quality_ready_rows_without_freshness_contract_fail_closed(self) -> None:
        futu, _market, readiness_service = build_service(
            online=True,
            sec_configured=True,
            materials_limited=False,
        )
        futu.explicit_freshness = False

        payload = readiness_service.inspect(force=True)

        self.assertFalse(payload["round_admission"]["ready"])
        self.assertEqual(payload["round_admission"]["coverage_ready"], 0)
        self.assertEqual(
            payload["round_admission"]["reason_code"],
            "FUTU_FRESHNESS_INVALID",
        )

    def test_missing_snapshot_id_fails_closed(self) -> None:
        futu, _market, readiness_service = build_service(
            online=True,
            sec_configured=True,
            materials_limited=False,
        )
        futu.quote_mutator = lambda payload: payload.pop("snapshot_id")

        payload = readiness_service.inspect(force=True)

        self.assert_round_blocked_with(payload, "FUTU_SNAPSHOT_ID_REQUIRED")

    def test_missing_captured_at_fails_closed(self) -> None:
        futu, _market, readiness_service = build_service(
            online=True,
            sec_configured=True,
            materials_limited=False,
        )
        futu.quote_mutator = lambda payload: payload.pop("captured_at")

        payload = readiness_service.inspect(force=True)

        self.assert_round_blocked_with(payload, "FUTU_CAPTURED_AT_REQUIRED")

    def test_invalid_captured_at_has_explicit_failure_code(self) -> None:
        futu, _market, readiness_service = build_service(
            online=True,
            sec_configured=True,
            materials_limited=False,
        )
        futu.quote_mutator = lambda payload: payload.update({
            "captured_at": "not-an-iso-time",
        })

        payload = readiness_service.inspect(force=True)

        self.assert_round_blocked_with(payload, "FUTU_CAPTURED_AT_INVALID")
        futu_source = next(
            source for source in payload["sources"]
            if source["id"] == "futu_opend"
        )
        self.assertNotIn("FUTU_MARKET_TIME_INVALID", futu_source["error_codes"])

    def test_duplicate_symbol_fails_closed(self) -> None:
        futu, _market, readiness_service = build_service(
            online=True,
            sec_configured=True,
            materials_limited=False,
        )
        futu.quote_mutator = lambda payload: payload["rows"].append(
            dict(payload["rows"][0])
        )

        payload = readiness_service.inspect(force=True)

        self.assert_round_blocked_with(payload, "FUTU_DUPLICATE_SYMBOLS")

    def test_non_positive_price_fails_closed(self) -> None:
        futu, _market, readiness_service = build_service(
            online=True,
            sec_configured=True,
            materials_limited=False,
        )
        futu.quote_mutator = lambda payload: payload["rows"][1].update({"last": 0})

        payload = readiness_service.inspect(force=True)

        self.assert_round_blocked_with(payload, "FUTU_QUOTE_VALUE_INVALID")

    def test_future_market_time_fails_closed(self) -> None:
        futu, _market, readiness_service = build_service(
            online=True,
            sec_configured=True,
            materials_limited=False,
        )
        futu.quote_mutator = lambda payload: payload["rows"][2].update({
            "market_time": "2026-08-01T12:01:00Z",
        })

        payload = readiness_service.inspect(force=True)

        self.assert_round_blocked_with(payload, "FUTU_MARKET_TIME_FUTURE")

    def test_missing_read_only_safety_fields_fail_closed(self) -> None:
        futu, _market, readiness_service = build_service(
            online=True,
            sec_configured=True,
            materials_limited=False,
        )

        def remove_safety(payload):
            payload.pop("execution_capability")
            payload.pop("live_trading_allowed")

        futu.quote_mutator = remove_safety

        payload = readiness_service.inspect(force=True)

        self.assert_round_blocked_with(payload, "FUTU_READ_ONLY_BOUNDARY_REQUIRED")

    def test_source_error_does_not_hide_read_only_boundary_failure(self) -> None:
        futu, _market, readiness_service = build_service(
            online=True,
            sec_configured=True,
            materials_limited=False,
        )

        def combine_source_and_safety_failure(payload):
            payload["execution_capability"] = "order"
            payload["live_trading_allowed"] = True
            payload["source_errors"] = [{
                "source": "futu_opend",
                "code": "UPSTREAM_QUOTE_ERROR",
                "message": "untrusted upstream details",
            }]

        futu.quote_mutator = combine_source_and_safety_failure

        payload = readiness_service.inspect(force=True)

        self.assert_round_blocked_with(payload, "FUTU_READ_ONLY_BOUNDARY_REQUIRED")
        futu_source = next(
            source for source in payload["sources"]
            if source["id"] == "futu_opend"
        )
        self.assertIn("UPSTREAM_QUOTE_ERROR", futu_source["error_codes"])
        self.assertNotIn("untrusted upstream details", json.dumps(payload))

    def test_untrusted_source_error_code_cannot_become_public_text_channel(self) -> None:
        futu, _market, readiness_service = build_service(
            online=True,
            sec_configured=True,
            materials_limited=False,
        )

        def inject_untrusted_code(payload):
            payload["source_errors"] = [{
                "source": "futu_opend",
                "code": "Bearer fake-code-sentinel",
                "message": "untrusted details",
            }]

        futu.quote_mutator = inject_untrusted_code

        payload = readiness_service.inspect(force=True)

        self.assert_round_blocked_with(payload, "FUTU_SOURCE_ERROR")
        encoded = json.dumps(payload, ensure_ascii=False).lower()
        self.assertNotIn("fake-code-sentinel", encoded)
        self.assertNotIn("bearer ", encoded)
        self.assertNotIn("untrusted details", encoded)

    def test_quote_exception_is_redacted_with_stable_public_message(self) -> None:
        futu, _market, readiness_service = build_service(
            online=True,
            sec_configured=True,
            materials_limited=False,
        )

        def raise_sensitive_error(*_args, **_kwargs):
            raise RuntimeError("authorization=Bearer fake-sentinel")

        futu.quote_batch = raise_sensitive_error

        payload = readiness_service.inspect(force=True)

        self.assert_round_blocked_with(payload, "FUTU_READINESS_ERROR")
        self.assertEqual(
            payload["round_admission"]["reason"],
            "Futu OpenD 只读行情读取失败，请检查本机行情连接与服务状态。",
        )
        encoded = json.dumps(payload, ensure_ascii=False).lower()
        self.assertNotIn("fake-sentinel", encoded)
        self.assertNotIn("authorization", encoded)
        self.assertNotIn("bearer ", encoded)

    def test_sdk_status_error_message_is_not_exposed(self) -> None:
        futu, _market, readiness_service = build_service(
            online=True,
            sec_configured=True,
            materials_limited=False,
        )
        base_status = futu.status()

        def sensitive_sdk_status():
            return {
                **base_status,
                "sdk_available": False,
                "sdk_state": "import_error",
                "sdk_error": {
                    "code": "FUTU_SDK_IMPORT_ERROR",
                    "message": "authorization=Bearer fake-sdk-sentinel",
                },
            }

        futu.status = sensitive_sdk_status

        payload = readiness_service.inspect(force=True)

        self.assert_round_blocked_with(payload, "FUTU_SDK_IMPORT_ERROR")
        encoded = json.dumps(payload, ensure_ascii=False).lower()
        self.assertNotIn("fake-sdk-sentinel", encoded)
        self.assertNotIn("authorization", encoded)
        self.assertNotIn("bearer ", encoded)

    def test_earnings_pack_with_complete_coverage_and_source_error_is_partial(self) -> None:
        _futu, market, _readiness_service = build_service(
            online=False,
            sec_configured=True,
            materials_limited=True,
        )

        payload = market.independent_evidence(force=True)
        packs = payload["evidence"]["official_earnings_packs"]

        self.assertEqual(len(packs["rows"]), 4)
        self.assertEqual(packs["missing_symbols"], [])
        self.assertEqual(packs["state"], "partial")
        self.assertEqual(packs["source_errors"][0]["code"], "EARNINGS_MATERIAL_HUB_ERROR")


class FakeHttpReadiness:
    def __init__(self) -> None:
        self.force_values = []

    def inspect(self, *, force=False):
        self.force_values.append(force)
        return {
            "version": "storage_research_readiness_v1",
            "ready": False,
            "round_admission": {"ready": False, "coverage_ready": 0, "coverage_total": 4},
            "safety": {
                "ready": True,
                "execution_capability": "none",
                "live_trading_allowed": False,
            },
        }


class StorageResearchReadinessHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original = http_server.STORAGE_READINESS
        self.fake = FakeHttpReadiness()
        http_server.STORAGE_READINESS = self.fake
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), http_server.StudioRequestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        http_server.STORAGE_READINESS = self.original

    def test_http_readiness_is_get_only_read_only_and_secret_free(self) -> None:
        with urlopen(f"{self.base_url}/api/market/storage/readiness?force=1", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertTrue(payload["ok"])
        self.assertEqual(self.fake.force_values, [True])
        self.assertEqual(payload["readiness"]["safety"]["execution_capability"], "none")
        self.assertFalse(payload["readiness"]["safety"]["live_trading_allowed"])
        encoded = json.dumps(payload, ensure_ascii=False).lower()
        self.assertNotIn("api_key", encoded)
        self.assertNotIn("authorization", encoded)
        self.assertNotIn("bearer ", encoded)


if __name__ == "__main__":
    unittest.main()
