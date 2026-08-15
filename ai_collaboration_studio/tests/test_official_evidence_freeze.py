from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend import http_server
from backend.market.storage_service import StorageResearchMarketService
from backend.store import StudioStore


SEC_URL = "https://www.sec.gov/Archives/edgar/data/723125/000072312526000001/mu-20260718.htm"
IR_URL = "https://investors.micron.com/news-releases/news-release-details/storage-update"


class FakeSecAdapter:
    @staticmethod
    def recent_filings_batch(symbols, **_kwargs):
        return {
            "ok": True,
            "captured_at": "2026-07-19T20:00:00.123Z",
            "rows": [{
                "symbol": symbols[0],
                "company_name": "Micron Technology, Inc.",
                "filings": [{
                    "accession_number": "0000723125-26-000001",
                    "form": "8-K",
                    "filing_date": "2026-07-19",
                    "report_date": "2026-07-18",
                    "accepted_at": "2026-07-19T08:30:00.123Z",
                    "items": "2.02",
                    "description": "Results of Operations",
                    "official_url": SEC_URL,
                }],
            }],
        }


class FakeIrAdapter:
    @staticmethod
    def recent_releases_batch(symbols, **_kwargs):
        return {
            "ok": True,
            "captured_at": "2026-07-19T20:00:00.456Z",
            "rows": [{
                "symbol": symbols[0],
                "publisher": "Micron Investor Relations",
                "releases": [{
                    "title": "Official storage update",
                    "published_at": "2026-07-18T20:00:00.456Z",
                    "published_date": "2026-07-18",
                    "official_url": IR_URL,
                    "summary": "Company statement.",
                    "event_type": "earnings_release",
                    "fiscal_period": "FY2026-Q3",
                    "presentation_hub_url": "https://investors.micron.com/quarterly-results",
                }],
            }],
        }


class FakeEarningsMaterialsAdapter:
    @staticmethod
    def recent_materials_batch(symbols, **_kwargs):
        return {
            "rows": [{
                "symbol": symbols[0],
                "materials": [{
                    "fiscal_period": "FY2026-Q3",
                    "material_kind": "earnings_presentation",
                    "official_url": "https://investors.micron.com/static-files/q3-deck",
                    "discovery_method": "curated_verified",
                    "verified_at": "2026-07-20",
                }],
            }],
        }


class OfficialEvidencePayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = StorageResearchMarketService(
            adapter=object(),
            sec_adapter=FakeSecAdapter(),
            ir_adapter=FakeIrAdapter(),
            earnings_materials_adapter=FakeEarningsMaterialsAdapter(),
        )

    def test_sec_and_ir_payloads_only_use_exact_current_official_records(self) -> None:
        sec_payload = self.service.official_evidence_material_payload(
            evidence_kind="sec_filing",
            symbol="US.MU",
            official_url=SEC_URL,
        )
        ir_payload = self.service.official_evidence_material_payload(
            evidence_kind="ir_release",
            symbol="US.MU",
            official_url=IR_URL,
        )

        self.assertEqual(sec_payload["metadata"]["official_evidence_id"], "0000723125-26-000001")
        self.assertEqual(sec_payload["metadata"]["source_type"], "regulatory_filing")
        self.assertIn("不得仅凭表单类型", sec_payload["content"])
        self.assertEqual(ir_payload["metadata"]["official_evidence_kind"], "ir_release")
        self.assertEqual(ir_payload["metadata"]["event_type"], "earnings")
        self.assertEqual(ir_payload["metadata"]["direct_material_count"], 1)
        self.assertEqual(ir_payload["metadata"]["located_metric_count"], 6)
        self.assertEqual(len(ir_payload["metadata"]["possible_sec_matches"]), 1)
        self.assertIn("不是独立核验", ir_payload["content"])
        self.assertIn("static-files/q3-deck", ir_payload["content"])
        self.assertIn("DRAM ASP QoQ", ir_payload["content"])
        cleaned_metadata = StudioStore._clean_material_metadata(ir_payload["metadata"])
        self.assertEqual(cleaned_metadata["fiscal_period"], "FY2026-Q3")
        self.assertEqual(cleaned_metadata["direct_material_count"], 1)
        self.assertEqual(cleaned_metadata["located_metric_count"], 6)

        with self.assertRaisesRegex(ValueError, "找不到"):
            self.service.official_evidence_material_payload(
                evidence_kind="sec_filing",
                symbol="US.MU",
                official_url="https://www.sec.gov/Archives/edgar/data/not-current.htm",
            )


class FakeOfficialEvidenceService:
    def official_evidence_material_payload(self, *, evidence_kind: str, symbol: str, official_url: str):
        if evidence_kind != "sec_filing" or symbol != "US.MU" or official_url != SEC_URL:
            raise ValueError("当前官方证据中找不到该记录")
        return {
            "title": "[SEC 8-K] MU · Results of Operations",
            "kind": "url",
            "source_url": SEC_URL,
            "content": "用户确认冻结的 SEC 官方申报索引。",
            "metadata": {
                "source_type": "regulatory_filing",
                "event_type": "other",
                "publisher": "U.S. Securities and Exchange Commission EDGAR",
                "published_at": "2026-07-19T08:30:00.123Z",
                "symbols": ["US.MU"],
                "official_evidence_kind": "sec_filing",
                "official_evidence_id": "0000723125-26-000001",
                "source_captured_at": "2026-07-19T20:00:00.456Z",
            },
        }


class OfficialEvidenceHttpApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "studio.sqlite3")
        self.original_store = http_server.STORE
        self.original_market = http_server.STORAGE_MARKET
        http_server.STORE = self.store
        http_server.STORAGE_MARKET = FakeOfficialEvidenceService()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), http_server.StudioRequestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        http_server.STORE = self.original_store
        http_server.STORAGE_MARKET = self.original_market
        self.temp_dir.cleanup()

    def request(self, room_id: str, payload: dict) -> tuple[int, dict]:
        request = Request(
            f"{self.base_url}/api/rooms/{room_id}/materials/freeze-official-evidence",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def test_user_freeze_is_versioned_and_duplicate_click_is_idempotent(self) -> None:
        payload = {"evidence_kind": "sec_filing", "symbol": "US.MU", "official_url": SEC_URL}
        first_status, first = self.request("room_storage", payload)
        second_status, second = self.request("room_storage", payload)

        self.assertEqual(first_status, 201)
        self.assertTrue(first["created"])
        self.assertEqual(second_status, 200)
        self.assertFalse(second["created"])
        self.assertEqual(first["material"]["id"], second["material"]["id"])
        self.assertEqual(first["material"]["version"], 1)
        self.assertEqual(first["material"]["metadata"]["source_tier"], "primary")
        self.assertEqual(first["material"]["metadata"]["published_at"], "2026-07-19T08:30:00.123Z")
        self.assertEqual(len(self.store.list_materials("room_storage")), 1)

    def test_unknown_room_and_untrusted_record_are_rejected(self) -> None:
        payload = {"evidence_kind": "sec_filing", "symbol": "US.MU", "official_url": SEC_URL}
        missing_status, _missing = self.request("room_missing", payload)
        invalid_status, invalid = self.request("room_storage", {**payload, "official_url": "https://example.com/fake"})

        self.assertEqual(missing_status, 404)
        self.assertEqual(invalid_status, 400)
        self.assertIn("找不到", invalid["error"])


if __name__ == "__main__":
    unittest.main()
