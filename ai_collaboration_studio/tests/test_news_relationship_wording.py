"""Narrow enforcement-with regression and saved-v2 compatibility; offline only."""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.news_content_quality import (
    INSTRUCTIONS, QUALITY_VERSION, RSS_V2_INSTRUCTIONS, RSS_V2_QUALITY_VERSION,
    RSS_V2_TEXT_FORMAT, TEXT_FORMAT, build_input, inspect_material, validate_output,
)
from backend.news_platform_ai import NewsAnalysis

NOW = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
FAKE_KEY = "SYNTHETIC_NEVER_VALID_KEY"


def synthetic_item(item_id="synthetic-with", relation="with"):
    sentence = "Synthetic regulator announces termination of enforcement actions " + relation + " Synthetic Alder Bank and Synthetic Birch Bank"
    return {"id": item_id, "title": sentence, "excerpt": sentence,
            "source_name": "Synthetic regulator", "url": "https://example.com/synthetic-only",
            "published_at": "2026-10-16T15:00:00Z", "discovered_at": "2026-10-16T15:08:00Z",
            "updated_at": "2026-10-16T15:08:00Z"}


def neutral_output(item):
    return {"summary": "合成监管机构宣布终止涉及两家合成银行的执法行动。此为旧公告，仅阅读RSS摘录。",
            "key_points": [{"claim": "公告涉及两家合成银行的执法行动终止。", "evidence_field": "excerpt", "quote": item["excerpt"]}],
            "uncertainties": ["仅阅读RSS摘录，未核对全文。", "with不能确认是否针对这些主体；具体法律关系仍不明确。"]}


def validate(item, output):
    return validate_output(json.dumps(output, ensure_ascii=False), build_input(item, checked_at=NOW))


class RelationshipWordingTests(unittest.TestCase):
    def test_summary_upgrade_rejected_despite_exact_quote_and_uncertainty(self):
        item = synthetic_item()
        for term in ("针对", "針對", "执法对象", "处罚对象", "制裁对象", "AGAINST"):
            with self.subTest(term=term):
                value = neutral_output(item)
                value["summary"] = "合成机构终止" + term + "合成银行的执法行动。"
                with self.assertRaisesRegex(ValueError, "unsupported_relationship_direction"):
                    validate(item, value)

    def test_each_claim_checked_even_with_neutral_summary_and_valid_quotes(self):
        item = synthetic_item()
        value = neutral_output(item)
        value["key_points"].append({"claim": "行动针对合成银行。", "evidence_field": "title", "quote": item["title"]})
        with self.assertRaisesRegex(ValueError, "unsupported_relationship_direction"):
            validate(item, value)

    def test_neutral_result_keeps_uncertainty_scope_and_original_material(self):
        item = synthetic_item()
        value = neutral_output(item)
        original_item, original_value = copy.deepcopy(item), copy.deepcopy(value)
        result = validate(item, value)
        self.assertEqual(result["summary"], value["summary"])
        self.assertEqual(result["uncertainties"], value["uncertainties"])
        self.assertEqual(result["quality_version"], "rss_excerpt_reading_v3")
        self.assertTrue(result["semantic_review_required"])
        self.assertNotIn("quality_gate_passed", result)
        self.assertEqual(result["freshness"]["publication_age"], "older_than_24h")
        self.assertFalse(result["scope"]["full_text_read"])
        self.assertEqual((item, value), (original_item, original_value))
        self.assertTrue(any("未明确法律关系" in warning for warning in inspect_material(item, NOW)["warnings"]))

    def test_guard_is_specific_to_enforcement_with_not_generic_with_or_against(self):
        item = synthetic_item(relation="against")
        value = neutral_output(item)
        value["summary"] = "合成监管机构宣布终止针对合成银行的执法行动。"
        self.assertEqual(validate(item, value)["summary"], value["summary"])
        item["title"] = item["excerpt"] = "Synthetic company supplies parts with extended warranties."
        value = neutral_output(item)
        value["summary"] = "合成公司针对产品提供保修。"
        self.assertEqual(validate(item, value)["summary"], value["summary"])

    def test_singular_case_and_whitespace_match_but_uncertainty_cannot_waive(self):
        item = synthetic_item()
        item["title"] = item["excerpt"] = "Synthetic regulator ends ENFORCEMENT ACTION\nWITH Synthetic Bank."
        value = neutral_output(item)
        value["summary"] = "针对合成银行的执法行动终止；法律关系仍需确认。"
        with self.assertRaisesRegex(ValueError, "unsupported_relationship_direction"):
            validate(item, value)

    def test_new_contract_and_exact_frozen_v2_values(self):
        self.assertEqual(QUALITY_VERSION, "rss_excerpt_reading_v3")
        self.assertEqual(TEXT_FORMAT["name"], QUALITY_VERSION)
        self.assertEqual(RSS_V2_QUALITY_VERSION, "rss_excerpt_reading_v2")
        self.assertIn("summary与每条key_points.claim", INSTRUCTIONS)
        self.assertIsNot(TEXT_FORMAT["schema"], RSS_V2_TEXT_FORMAT["schema"])
        # Extracted from d79b9fe before any v3 edits, using a synthetic item.
        self.assertEqual(hashlib.sha256(RSS_V2_INSTRUCTIONS.encode()).hexdigest(),
                         "a88081ab8910ed8118e9a09dc45e3170323b113074d6d9236a1db6fe5069fc08")
        self.assertEqual(hashlib.sha256(json.dumps(RSS_V2_TEXT_FORMAT, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                         "bb3939937b1e754dc53f1bf288f210af8aeb4e7139b72b2a35642950e9920556")


class SnapshotNews:
    def __init__(self):
        self.items = {}

    def get_item(self, item_id):
        return copy.deepcopy(self.items.get(item_id))

    def overview(self):
        return {"enabled": True}


class SavedV2CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.news, self.calls = SnapshotNews(), []
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError("network_forbidden"))
        self.network.start()
        self.app = NewsAnalysis(Path(self.temp.name) / "analysis.sqlite3", self.news,
                                generate=lambda *_: self.fail("saved_v2_must_not_call_provider"))

    def tearDown(self):
        self.assertTrue(self.app.stop(timeout=4))
        self.network.stop()
        self.temp.cleanup()

    def test_exact_previous_fingerprint_stays_distinct_from_v3(self):
        item = {"title": "Issuer Q2 results", "excerpt": "Revenue was USD 10 million in Q2 2026.",
                "source_name": "Synthetic issuer", "url": "https://example.com/release",
                "published_at": "2026-10-19T10:00:00Z", "discovered_at": "2026-10-19T10:05:00Z"}
        self.assertEqual(self.app._rss_v2_fingerprint(item), "b53f105b3dcc924d8437972e08d89f633e09835b0a6ab102f2a64bb57f249ea4")
        self.assertNotEqual(self.app._fingerprint(item), self.app._rss_v2_fingerprint(item))

    def test_completed_failed_pending_unknown_v2_stay_readable_unchanged_and_spent(self):
        self.app.configure(FAKE_KEY, True, True)
        before = []
        for status in ("completed", "failed", "pending", "unknown"):
            item = synthetic_item("old-v2-" + status)
            item["url"] += "/" + status
            self.news.items[item["id"]] = item
            raw = '{ "summary" : "旧版针对措辞原样保留", "quality_version" : "rss_excerpt_reading_v2", "semantic_review_required" : true }'
            row = (self.app._rss_v2_fingerprint(item), item["id"], time.time(), 1234, status, raw)
            before.append(row)
            with self.app._db() as db:
                db.execute("INSERT INTO runs(fingerprint,item_id,created,reserved,status,result) VALUES(?,?,?,?,?,?)", row)
            saved = self.app.result(item)
            self.assertEqual(saved["quality_version"], "rss_excerpt_reading_v2")
            self.assertTrue(saved["semantic_review_required"])
            self.assertIn("未经过v3", saved["legacy_notice"])
            self.assertEqual(saved["summary"], json.loads(raw)["summary"])
            self.assertEqual(self.app.request(item["id"])["status"], status)
            self.app._analyze(item["id"])
            self.app._analyze(item["id"], automatic=True)
        with self.app._db() as db:
            after = [tuple(db.execute("SELECT fingerprint,item_id,created,reserved,status,result FROM runs WHERE fingerprint=?", (row[0],)).fetchone()) for row in before]
            self.assertEqual(db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 4)
        self.assertEqual(after, before)
        self.assertTrue(self.app._queue.empty())
        self.assertEqual(self.app.status()["calls_reserved"], 4)

    def test_new_unsupported_summary_is_failed_and_still_not_retried(self):
        item = synthetic_item()
        self.news.items[item["id"]] = item
        def fake_generate(*_):
            self.calls.append("synthetic_attempt")
            output = neutral_output(item)
            output["summary"] = "监管机构宣布终止针对合成银行的行动。"
            return SimpleNamespace(ok=True, model="doubao-seed-2-1-pro-260915", content=json.dumps(output, ensure_ascii=False))
        self.app._generate = fake_generate
        self.app.configure(FAKE_KEY, False, True)
        self.app._analyze(item["id"])
        saved = self.app.result(item)
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["error_code"], "analysis_failed")
        self.assertEqual(saved["quality_version"], QUALITY_VERSION)
        self.app._analyze(item["id"])
        self.assertEqual(self.app.request(item["id"])["status"], "failed")
        self.assertEqual(self.calls, ["synthetic_attempt"])
        self.assertEqual(self.app.status()["calls_reserved"], 1)


if __name__ == "__main__":
    unittest.main()
