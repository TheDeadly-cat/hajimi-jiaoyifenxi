"""Focused offline tests. All generators are local fakes and all DBs are temporary."""
import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.news_platform_ai import BUDGET_MICRO_CNY, MAX_CALLS, MODEL, NewsAnalysis
from backend.news_content_quality import QUALITY_VERSION, TEXT_FORMAT, build_input


FAKE_KEY = "SYNTHETIC_KEY_NEVER_VALID"


def item(item_id="one", discovered=None):
    return {"id": item_id, "title": f"Official release {item_id}", "excerpt": "A synthetic local test excerpt.",
            "source_name": "Test fixture", "url": "https://www.federalreserve.gov/example.htm",
            "published_at": "2026-10-15T00:00:00Z",
            "discovered_at": datetime.fromtimestamp(discovered or time.time(), timezone.utc).isoformat()}


def valid_response(**changes):
    value = {"summary": "测试摘要", "key_points": [{"claim": "给定材料中的信息",
             "evidence_field": "excerpt", "quote": "A synthetic local test excerpt."}],
             "uncertainties": ["未核对全文"]}
    value.update(changes)
    return SimpleNamespace(ok=True, content=json.dumps(value, ensure_ascii=False), model=MODEL)


class FakeNews:
    def __init__(self):
        self.enabled = False
        self.items = {"one": item()}

    def get_item(self, item_id):
        return self.items.get(item_id)

    def overview(self):
        return {"enabled": self.enabled}

    def list_items(self, limit=100):
        return {"items": list(self.items.values())[:limit]}


class NewsAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "analysis.db"
        self.news = FakeNews()
        self.calls = []
        self.network_guard = patch.object(socket.socket, "connect", side_effect=AssertionError("real_network_forbidden"))
        self.network_guard.start()

        def generate(key, text):
            self.calls.append((key, text))
            return valid_response()

        self.app = NewsAnalysis(self.path, self.news, generate=generate)

    def tearDown(self):
        self.assertTrue(self.app.stop(timeout=4))
        self.network_guard.stop()
        self.temp.cleanup()

    def configure(self, automatic=False):
        return self.app.configure(FAKE_KEY, automatic, True)

    def seed(self, count, reserved=1, age=0, status="failed"):
        with self.app._db() as db:
            db.executemany("INSERT INTO runs(fingerprint,item_id,created,reserved,status) VALUES(?,?,?,?,?)",
                           [(f"fixture-{i}", f"old-{i}", time.time() - age, reserved, status) for i in range(count)])

    def wait_for(self, check, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(0.01)
        self.fail("background condition not reached")

    def test_explicit_configuration_is_required_and_key_never_persisted(self):
        self.app._analyze("one")
        self.assertEqual(self.calls, [])
        with self.assertRaisesRegex(ValueError, "session_required"):
            self.app.request("one")
        with self.assertRaises(ValueError):
            self.app.configure(FAKE_KEY, False, False)
        status = self.configure()
        self.assertNotIn(FAKE_KEY, json.dumps(status))
        self.app._analyze("one")
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn(FAKE_KEY.encode(), self.path.read_bytes())
        self.assertEqual(self.app.result(self.news.items["one"])["status"], "completed")

    def test_reservation_is_committed_before_provider_call(self):
        self.configure()
        def inspect_reservation(key, text):
            with self.app._db() as db:
                rows = db.execute("SELECT status,reserved FROM runs").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "pending")
            self.assertGreater(rows[0]["reserved"], 0)
            self.assertTrue(self.app.status()["busy"])
            return valid_response()
        self.app._generate = inspect_reservation
        self.app._analyze("one")
        self.assertEqual(self.app.result(self.news.items["one"])["status"], "completed")

    def test_concurrent_duplicate_requests_send_only_once(self):
        self.configure()
        barrier = threading.Barrier(8)
        def analyze_once():
            barrier.wait()
            self.app._analyze("one")
        workers = [threading.Thread(target=analyze_once) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(4)
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.app.status()["calls_reserved"], 1)

    def test_failure_keeps_reserved_budget_and_never_retries(self):
        self.configure()
        def fail(key, text):
            self.calls.append((key, text))
            raise TimeoutError("synthetic unknown provider outcome")
        self.app._generate = fail
        self.app._analyze("one")
        reservation = self.app.status()["reserved_cny"]
        self.assertGreater(reservation, 0)
        self.assertIn(self.app.result(self.news.items["one"])["status"], {"failed", "unknown"})
        self.app._analyze("one")
        self.assertNotEqual(self.app.request("one")["status"], "queued")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.app.status()["reserved_cny"], reservation)

    def test_call_count_cap_includes_failed_and_pending_reservations(self):
        self.seed(MAX_CALLS - 1)
        self.configure()
        self.app._analyze("one")
        self.news.items["two"] = item("two")
        self.app._analyze("two")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.app.status()["calls_reserved"], MAX_CALLS)
        self.assertIsNone(self.app.result(self.news.items["two"]))

    def test_cost_cap_rejects_before_provider_and_counts_age_out(self):
        self.seed(1, reserved=BUDGET_MICRO_CNY - 1)
        self.configure()
        self.app._analyze("one")
        self.assertEqual(self.calls, [])
        self.assertLessEqual(self.app.status()["reserved_cny"], 3)
        with self.app._db() as db:
            db.execute("UPDATE runs SET created=?", (time.time() - 86401,))
        self.app._analyze("one")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.app.status()["calls_reserved"], 1)

    def test_clear_removes_queue_and_configuration_without_losing_results(self):
        self.configure()
        self.app._analyze("one")
        self.news.items["two"] = item("two")
        self.assertEqual(self.app.request("two"), {"status": "queued"})
        state = self.app.clear()
        self.assertFalse(state["configured"])
        self.assertTrue(self.app._queue.empty())
        self.app._analyze("two")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.app.result(self.news.items["one"])["status"], "completed")

    def test_wall_clock_or_monotonic_expiry_each_revokes_configuration(self):
        for expiry_kind in ("wall", "monotonic"):
            with self.subTest(expiry_kind=expiry_kind):
                self.configure(automatic=True)
                if expiry_kind == "wall":
                    self.app._expires_at = time.time() - 1
                else:
                    self.app._deadline = time.monotonic() - 1
                self.assertFalse(self.app.status()["configured"])
                self.assertFalse(self.app.status()["automatic"])
                self.app._analyze("one")
                self.assertEqual(self.calls, [])

    def test_restart_turns_pending_into_unknown_and_does_not_retry(self):
        self.configure()
        fingerprint = self.app._fingerprint(self.news.items["one"])
        with self.app._db() as db:
            db.execute("INSERT INTO runs(fingerprint,item_id,created,reserved,status) VALUES(?,?,?,?,?)",
                       (fingerprint, "one", time.time(), 123456, "pending"))
        self.app = NewsAnalysis(self.path, self.news, generate=lambda *args: self.fail("pending must not retry"))
        self.assertFalse(self.app.status()["configured"])
        self.assertEqual(self.app.result(self.news.items["one"])["status"], "unknown")
        self.configure()
        self.app._analyze("one")
        self.assertEqual(self.app.status()["reserved_cny"], 0.123456)
        self.assertEqual(self.app.result(self.news.items["one"])["status"], "unknown")

    def test_invalid_output_fields_types_and_length_are_rejected(self):
        self.configure()
        invalid = [
            {"summary": "ok", "key_points": [], "uncertainties": [], "invented": "field"},
            {"summary": "ok", "key_points": "not-a-list", "uncertainties": []},
            {"summary": "x" * 501, "key_points": [], "uncertainties": ["未核对全文"]},
            {"summary": "ok", "key_points": ["x"] * 7, "uncertainties": ["未核对全文"]},
            {"summary": "   ", "key_points": [], "uncertainties": ["未核对全文"]},
            {"summary": "ok", "key_points": [], "uncertainties": []},
        ]
        for index, payload in enumerate(invalid):
            with self.subTest(index=index):
                item_id = f"bad-{index}"
                self.news.items[item_id] = item(item_id)
                self.app._generate = lambda *args, value=payload: SimpleNamespace(ok=True, content=json.dumps(value))
                self.app._analyze(item_id)
                self.assertEqual(self.app.result(self.news.items[item_id])["status"], "failed")
        self.assertEqual(self.app.status()["calls_reserved"], len(invalid))

    def test_start_is_idempotent_and_stop_drains_only_worker(self):
        self.app.start()
        first = self.app._thread
        try:
            self.app.start()
            self.assertIs(self.app._thread, first)
        finally:
            self.app.stop(timeout=4)
            first.join(4)

    def test_automatic_analysis_requires_enabled_monitor_and_new_item(self):
        self.news.items["old"] = item("old", discovered=time.time() - 100)
        self.configure(automatic=True)
        self.news.items["new"] = item("new", discovered=time.time() + 0.1)
        self.app.start()
        # Wake the loop without depending on a fixed sleep; automatic path remains gated.
        self.app._queue.put_nowait(None)
        self.wait_for(lambda: self.app._queue.empty())
        self.assertEqual(self.calls, [])
        self.news.enabled = True
        self.app._queue.put_nowait(None)
        # Generator entry precedes result validation and the completion commit.
        # Wait for the durable result, then still enforce exactly one call.
        self.wait_for(lambda: (self.app.result(self.news.items["new"]) or {}).get("status") == "completed")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.app.result(self.news.items["new"])["status"], "completed")
        self.assertIsNone(self.app.result(self.news.items["old"]))
        self.assertIsNone(self.app.result(self.news.items["one"]))

    def test_pause_between_automatic_scan_and_admission_prevents_new_call(self):
        self.configure(automatic=True)
        self.news.enabled = True
        self.news.items = {"new": item("new", discovered=time.time() + 0.1)}
        def pause_during_scan(limit=100):
            self.news.enabled = False
            return {"items": list(self.news.items.values())}
        self.news.list_items = pause_during_scan
        # Drive exactly one loop iteration without starting a background thread.
        with patch.object(self.app._queue, "get", side_effect=[None, StopIteration]):
            with self.assertRaises(StopIteration):
                self.app._loop()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.app.status()["calls_reserved"], 0)

    def test_existing_foreign_or_aliased_database_is_refused_without_changes(self):
        foreign = Path(self.temp.name) / "foreign.db"
        connection = sqlite3.connect(foreign)
        try:
            with connection:
                connection.execute("CREATE TABLE runs(status TEXT,result TEXT)")
                connection.execute("INSERT INTO runs VALUES('pending','preserve')")
        finally:
            connection.close()
        before = foreign.read_bytes()
        with self.assertRaisesRegex(ValueError, "independent"):
            NewsAnalysis(foreign, self.news)
        self.assertEqual(before, foreign.read_bytes())
        linked = Path(self.temp.name) / "linked.db"
        os.link(self.path, linked)
        with self.assertRaisesRegex(ValueError, "independent_regular"):
            NewsAnalysis(linked, self.news)
        with patch("backend.news_platform.first_reparse_component", return_value=Path(self.temp.name)):
            with patch("backend.news_platform_ai.sqlite3.connect", side_effect=AssertionError("must refuse before SQLite")):
                with self.assertRaisesRegex(ValueError, "alias"):
                    NewsAnalysis(Path(self.temp.name) / "uncreated.db", self.news)

    def test_missing_excerpt_is_free_even_without_key_and_never_queued_or_reserved(self):
        for index, excerpt in enumerate(('', '   ', 'Read more', 'https://example.com/full')):
            item_id = f'empty-{index}'
            self.news.items[item_id] = {**item(item_id), 'excerpt': excerpt}
            result = self.app.request(item_id)
            self.assertEqual(result['status'], 'material_insufficient')
            self.assertFalse(result['analysis_performed'])
            self.app._analyze(item_id)
            self.assertEqual(self.app.result(self.news.items[item_id])['status'], 'material_insufficient')
        self.assertEqual(self.calls, [])
        self.assertTrue(self.app._queue.empty())
        self.assertEqual(self.app.status()['calls_reserved'], 0)

    def test_legacy_completed_failed_and_unknown_records_remain_visible_unchanged_without_retry(self):
        self.configure(automatic=True)
        old_rows = []
        for status in ('completed', 'failed', 'unknown'):
            item_id = 'legacy-' + status
            source = item(item_id)
            self.news.items[item_id] = source
            fingerprint = self.app._legacy_fingerprint(source)
            raw = '{ "summary" : "旧结果原样保留", "key_points" : ["原要点"], "uncertainties" : ["原未知"] }'
            old_rows.append((fingerprint, raw, status))
            with self.app._db() as db:
                db.execute('INSERT INTO runs(fingerprint,item_id,created,reserved,status,result) VALUES(?,?,?,?,?,?)',
                           (fingerprint, item_id, time.time(), 1234, status, raw))
            saved = self.app.result(source)
            self.assertEqual(saved['status'], status)
            self.assertTrue(saved['legacy'])
            self.assertEqual(saved['quality_version'], 'legacy_unchecked')
            for key, value in json.loads(raw).items():
                self.assertEqual(saved[key], value)
            self.assertEqual(self.app.request(item_id)['status'], status)
            self.app._analyze(item_id, automatic=True)
            self.app._analyze(item_id)
        with self.app._db() as db:
            for fingerprint, raw, status in old_rows:
                row = db.execute('SELECT result,status FROM runs WHERE fingerprint=?', (fingerprint,)).fetchone()
                self.assertEqual((row['result'], row['status']), (raw, status))
        self.assertEqual(self.calls, [])
        self.assertTrue(self.app._queue.empty())
        self.assertEqual(self.app.status()['calls_reserved'], 3)

    def test_legacy_fingerprint_matches_frozen_pre_upgrade_formula(self):
        source = {'title': 'Issuer Q2 results', 'excerpt': 'Revenue was USD 10 million in Q2 2026.',
                  'source_name': 'Synthetic issuer', 'url': 'https://example.com/release',
                  'published_at': '2026-10-19T10:00:00Z', 'discovered_at': '2026-10-19T10:05:00Z'}
        # Independently extracted from installed r2 source without importing it.
        self.assertEqual(self.app._legacy_fingerprint(source),
                         '72552632faf5a4049f8a6c390b1eb3f2ac1b1ced2bd2896e013000d6245bd185')

    def test_reading_clock_changes_do_not_change_cache_identity_or_saved_result(self):
        source = self.news.items['one']
        fingerprint = self.app._fingerprint(source)
        first = build_input(source, checked_at=1792400000)
        second = build_input(source, checked_at=1792400900)
        self.assertNotEqual(first['checked_at'], second['checked_at'])
        self.assertEqual(self.app._fingerprint(source), fingerprint)
        self.configure()
        self.app._analyze('one')
        saved = self.app.result(source)
        self.assertEqual(saved['quality_version'], QUALITY_VERSION)
        self.assertEqual(saved['key_points'], ['给定材料中的信息'])
        self.assertEqual(saved['evidence'][0]['quote'], source['excerpt'])
        with patch('backend.news_platform_ai.time.time', return_value=time.time() + 60):
            self.assertEqual(self.app.result(source), saved)
            self.app._analyze('one')
        self.assertEqual(len(self.calls), 1)

    def test_wrong_quote_is_failed_and_not_recharged_on_another_request(self):
        self.configure()
        def invalid(key, text):
            self.calls.append((key, text))
            return valid_response(key_points=[{'claim': '捏造数字', 'evidence_field': 'excerpt',
                                                'quote': 'USD 999 million'}])
        self.app._generate = invalid
        self.app._analyze('one')
        self.assertEqual(self.app.result(self.news.items['one'])['status'], 'failed')
        self.assertEqual(self.app.request('one')['status'], 'failed')
        self.app._analyze('one')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.app.status()['calls_reserved'], 1)

    def test_provider_adapter_uses_same_strict_contract_and_existing_output_cap(self):
        observed = {}
        def factory(**configuration):
            observed['configuration'] = configuration
            def generate(**request):
                observed['request'] = request
                return valid_response()
            return SimpleNamespace(_generate=generate)
        fake_module = SimpleNamespace(DoubaoProvider=factory)
        with patch.dict('sys.modules', {'backend.providers.doubao_provider': fake_module}):
            NewsAnalysis._doubao(FAKE_KEY, 'isolated fixture input')
        self.assertEqual(observed['request']['text_format'], TEXT_FORMAT)
        self.assertIs(observed['request']['text_format']['strict'], True)
        self.assertEqual(observed['request']['max_output_tokens'], 900)
        self.assertEqual(observed['request']['timeout_seconds'], 45)
        self.assertEqual(observed['request']['model'], MODEL)
        self.assertEqual(MAX_CALLS, 36)
        self.assertEqual(BUDGET_MICRO_CNY, 3_000_000)


if __name__ == "__main__":
    unittest.main()
