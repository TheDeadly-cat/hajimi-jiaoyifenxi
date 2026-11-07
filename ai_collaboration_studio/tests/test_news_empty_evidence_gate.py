"""Offline final-input gates; every execution callback is fixed synthetic text."""
import json
import socket
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import news_analysis_providers as providers
from backend import news_evidence_reading as reading
from backend import news_platform_ai as analysis_module
from backend.news_article_evidence import extract_article
from backend.news_market_context import project_result
from backend.news_market_tasks import fingerprint
from backend.news_platform import NewsPlatform, analyze
from backend.news_platform_ai import NewsAnalysis
from backend.news_platform_routes import news_response
from tests.test_news_market_context import ASOF, FrozenStore


URL = "https://investors.micron.com/quarter"
NOW = datetime.fromisoformat(ASOF.replace("Z", "+00:00")).timestamp()
TEXT = "This is synthetic saved evidence with an explicit condition."
LARGE_TABLE = ("<article><p>All amounts require the following complete context.</p>"
               "<table><tr><th>Measure</th><th>Period</th></tr><tr><td>"
               + "conditions " * 3000 + "</td><td>Quarter</td></tr></table></article>").encode()
REPORTS = {}


class ForbiddenExecution(BaseException):
    pass


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.fromtimestamp(NOW, tz or timezone.utc)


class EmptyEvidenceGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="news-empty-evidence-")
        self.addCleanup(self.temp.cleanup)
        self.calls = []
        self.report = {"synthetic": True, "forbidden_attempts": {}}
        REPORTS[self._testMethodName] = self.report
        def deny(kind):
            def blocked(*args, **kwargs):
                counts = self.report["forbidden_attempts"]
                counts[kind] = counts.get(kind, 0) + 1
                raise ForbiddenExecution(kind)
            return blocked
        for target, name, kind in (
            (socket.socket, "connect", "network"), (socket.socket, "connect_ex", "network"),
            (socket, "getaddrinfo", "network"), (socket, "create_connection", "network"),
            (urllib.request.OpenerDirector, "open", "network"),
            (providers, "generate_response", "provider"), (threading.Thread, "start", "worker"),
        ):
            guard = patch.object(target, name, side_effect=deny(kind))
            guard.start(); self.addCleanup(guard.stop)
        for target, value in (("time", SimpleNamespace(time=lambda: NOW, monotonic=lambda: 1000)),
                              ("datetime", FrozenDateTime)):
            guard = patch.object(analysis_module, target, value)
            guard.start(); self.addCleanup(guard.stop)
        self.news = NewsPlatform(Path(self.temp.name) / "news.sqlite", clock=lambda: NOW,
                                 transport=deny("source"), article_transport=deny("article"))
        self.ai = NewsAnalysis(Path(self.temp.name) / "analysis.sqlite", self.news, generate=self.fixed)
        self.news.ai = self.ai
        self.ai.configure("PUBLIC_SYNTHETIC_ONLY", False, True)

    def fixed(self, key, text):
        material = json.loads(text)
        self.calls.append(material)
        if material.get("scope") == "market_impact_from_saved_context":
            value = project_result()
        else:
            source = material["source_text"]
            field = "body" if source.get("body") else "excerpt" if source.get("excerpt") else "title"
            value = {"summary": "合成本地固定摘要。", "key_points": [
                {"claim": "给定合成材料中的说明。", "evidence_field": field,
                 "quote": source[field].split("\n")[0][:100]}], "uncertainties": ["合成测试，不代表模型质量。"]}
        return SimpleNamespace(ok=True, model=self.ai._model, content=json.dumps(value, ensure_ascii=False))

    def save(self, raw, item_id="fixture", excerpt=TEXT):
        with self.news._db() as db:
            db.execute("INSERT INTO items(id,source_id,identity,title,url,excerpt,published,discovered,updated,analysis) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (item_id, "ir_mu", item_id, "Synthetic announcement", URL + "/" + item_id, excerpt,
                 NOW - 900, NOW - 1, NOW, json.dumps(analyze("Synthetic announcement", excerpt, NOW - 900, NOW, "ir_mu"))))
        evidence = extract_article(raw, URL + "/" + item_id, "ir_mu", now=NOW) if raw else {
            "content_status": "excerpt_only", "body_text": "", "segments": [], "attachments": []}
        return self.news.save_article_evidence(item_id, evidence)

    def ledger(self):
        with self.ai._db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM runs ORDER BY fingerprint")]

    def run_fact(self, item):
        task = self.news.analysis_task_for_item(item["id"])
        self.ai._analyze(item["id"], task_id=task["id"])
        return self.news.analysis_task_for_item(item["id"])

    def test_default_empty_complete_table_package_never_reserves_and_api_reports_insufficient(self):
        item = self.save(LARGE_TABLE)
        material = reading.build_input(item, checked_at=NOW)
        self.assertEqual(material["source_text"]["body"], "")
        self.assertEqual(material["source_text"]["excerpt"], "")
        self.assertEqual(material["evidence_package"]["selection"], [])
        self.report["initial_material"] = material
        task = self.run_fact(item)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.ledger(), [])
        self.assertEqual(task["status"], "needs_review")
        self.assertIn("no_send", task["reason"])
        self.assertEqual(self.ai.request(item["id"])["status"], "material_insufficient")
        value, status = news_response(self.news, "/api/news/items/fixture")
        self.assertEqual(status, 200)
        self.assertEqual(value["item"]["ai_analysis"]["status"], "material_insufficient")
        self.assertFalse(value["item"]["ai_analysis"]["analysis_performed"])

    def test_actual_wire_budget_reduction_to_zero_selected_body_never_uses_excerpt(self):
        # Nested JSON escaping exceeds the real unchanged 16,000-byte wire cap.
        raw = ("<article><p>" + TEXT + " " + "\\" * 3600 + "</p></article>").encode()
        item = self.save(raw)
        material = reading.build_input(item, checked_at=NOW)
        wire = providers.encode_body(providers.build_body(self.ai._provider, self.ai._model,
            json.dumps(material, ensure_ascii=False, sort_keys=True), reading.INSTRUCTIONS, reading.TEXT_FORMAT))
        self.assertTrue(material["source_text"]["body"])
        self.assertGreater(len(wire), providers.MAX_REQUEST_BYTES)
        budgets = []
        original = reading.build_input
        def observed(*args, **kwargs):
            result = original(*args, **kwargs)
            budgets.append({"text_budget": kwargs.get("text_budget", 6500),
                            "selected": len(result.get("evidence_package", {}).get("selection", []))})
            return result
        with patch.object(reading, "build_input", side_effect=observed):
            task = self.run_fact(item)
        self.report.update(initial_wire_bytes=len(wire), selected_by_budget=budgets)
        self.assertTrue(any(row["text_budget"] < 6500 and row["selected"] == 0 for row in budgets))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.ledger(), [])
        self.assertEqual(task["status"], "needs_review")
        self.assertEqual(self.ai.result(item)["status"], "material_insufficient")

    def test_completed_unknown_and_failed_saved_identities_precede_new_gate(self):
        for state in ("completed", "unknown", "failed"):
            item = self.save(LARGE_TABLE, state)
            identity = self.ai._version_fingerprint(item)
            with self.ai._db() as db:
                db.execute("INSERT INTO runs(fingerprint,item_id,created,reserved,status,result) VALUES(?,?,?,?,?,?)",
                    (identity, item["id"], NOW - 1, 123, state, json.dumps({"fixture_only": True, "summary": "Preserved synthetic legacy result"})))
            task = self.news.analysis_task_for_item(item["id"])
            self.news.transition_analysis_task(task["id"], state, expected_status="pending", result_ref=identity)
        before = self.ledger()
        for state in ("completed", "unknown", "failed"):
            self.assertEqual(self.ai.request(state)["status"], state)
            self.run_fact(self.news.get_item(state))
        self.assertEqual(self.ledger(), before)
        self.assertEqual(self.calls, [])

    def test_readable_body_and_legacy_excerpt_still_execute_once(self):
        for item in (self.save(("<article><p>" + TEXT + "</p></article>").encode(), "body"),
                     self.save(None, "rss")):
            self.assertEqual(self.run_fact(item)["status"], "completed")
            self.assertEqual(self.ai.request(item["id"])["status"], "completed")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.ledger()), 2)

    def test_market_title_and_quote_without_textual_source_evidence_never_reserves(self):
        item = self.save(LARGE_TABLE)
        self.news.research_store = FrozenStore()
        context = self.ai.prepare_market_context(item["id"], room_id="room1", round_id="round1", material_refs=[])
        self.assertEqual(context["market"]["status"], "available")
        self.assertEqual(self.ai.request_market(item["id"], context["context_id"])["status"], "queued")
        self.ai._analyze(item["id"], task_id=fingerprint(context["context_id"]), market_context_id=context["context_id"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.ledger(), [])
        result = self.ai.market_result(item, context["context_id"])
        self.assertEqual(result["status"], "needs_review")
        self.assertFalse(result["analysis_performed"])
        self.assertEqual(self.ai.request_market(item["id"], context["context_id"])["status"], "needs_review")
        historical = self.ai.prepare_market_context(item["id"], room_id="room1", round_id="round1",
            material_refs=[], version_id=item['current_version_id'], as_of=ASOF)
        self.ai.request_market(item["id"], historical["context_id"], acknowledge_historical=True)
        self.ai._analyze(item["id"], task_id=fingerprint(historical["context_id"]),
                         market_context_id=historical["context_id"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.ledger(), [])
        responses = {}
        for name, path, query in (
            ("item_detail", "/api/news/items/fixture", {}),
            ("current_context", "/api/news/items/fixture/market/context", {"context_id": [context["context_id"]]}),
            ("historical_context", "/api/news/items/fixture/market/context", {"context_id": [historical["context_id"]]}),
            ("versions", "/api/news/items/fixture/versions", {}),
            ("replays", "/api/news/items/fixture/market/replays", {"version_id": [item["current_version_id"]]}),
            ("overview", "/api/news/overview", {}), ("items", "/api/news/items", {}),
        ):
            value, status = news_response(self.news, path, query=query)
            self.assertEqual(status, 200)
            responses[name] = {"path": path, "query": query, "status": status, "response": value}
        self.assertEqual(responses["historical_context"]["response"]["market_context"]["mode"], "historical_replay")
        for key in ("current_context", "historical_context"):
            self.assertTrue(responses[key]["response"]["market_impact"]["material_insufficient"])
            self.assertFalse(responses[key]["response"]["market_impact"]["analysis_performed"])
        self.report['api_responses'] = responses

    def test_market_saved_original_text_is_still_eligible_without_full_article(self):
        item = self.save(LARGE_TABLE)
        self.news.research_store = FrozenStore()
        context = self.ai.prepare_market_context(item["id"], room_id="room1", round_id="round1",
                                                material_refs=[{"id": "consensus1", "version": 1}])
        self.ai.request_market(item["id"], context["context_id"])
        self.ai._analyze(item["id"], task_id=fingerprint(context["context_id"]), market_context_id=context["context_id"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.ai.market_result(item, context["context_id"])["status"], "completed")

    def tearDown(self):
        self.report["local_fixed_execution_count"] = len(self.calls)
        self.report["inputs_at_local_execution"] = self.calls
        self.report["reservation_rows"] = self.ledger()
        with self.news._db() as db:
            self.report["tasks"] = [dict(row) for row in db.execute("SELECT * FROM news_analysis_tasks")]
        self.assertEqual(self.report["forbidden_attempts"], {})
        self.assertIsNone(self.ai._thread)
        self.assertTrue(self.ai.stop())
        self.news.stop()


if __name__ == "__main__":
    unittest.main()
