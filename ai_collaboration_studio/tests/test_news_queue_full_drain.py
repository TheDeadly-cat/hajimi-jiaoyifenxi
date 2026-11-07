"""Synthetic offline queue acceptance; no provider, network, or worker is run.

The production consumer, reservation ledger, request queue, and state transitions
run against temporary databases. Only the execution callback returns fixed local
text. Advancing the test clock does not change either production budget limit.
REPORTS is available to an external offline runner for immutable test receipts.
"""
import json
import socket
import tempfile
import threading
import unittest
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from xml.sax.saxutils import escape

from backend import news_analysis_providers as providers
from backend import news_platform_ai as analysis_module
from backend.news_feed_transport import FeedResponse
from backend.news_platform import NewsPlatform
from backend.news_platform_ai import BUDGET_MICRO_CNY, MAX_CALLS, NewsAnalysis


REPORTS = {}
FAKE_KEY = "SYNTHETIC_QUEUE_TEST_NEVER_VALID"
EXCERPT = "This synthetic local announcement includes a stated condition."


class SyntheticProcessExit(BaseException):
    """Leave the real committed reservation pending, like a process interruption."""


class ForbiddenExternalExecution(BaseException):
    pass


class NewsQueueFullDrainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="news-queue-drain-")
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026, 10, 29, tzinfo=timezone.utc).timestamp()
        self.monotonic = 1000.0
        self.calls = []
        self.engines = []
        self.crash_next = False
        self.body = b"<rss><channel/></rss>"
        self.report = {"synthetic": True, "stages": [], "forbidden_attempts": {},
                       "production_limits": {"max_calls": MAX_CALLS,
                                             "budget_micro_cny": BUDGET_MICRO_CNY}}
        REPORTS[self._testMethodName] = self.report

        def deny(kind):
            def blocked(*args, **kwargs):
                counts = self.report["forbidden_attempts"]
                counts[kind] = counts.get(kind, 0) + 1
                raise ForbiddenExternalExecution(kind)
            return blocked

        for target, name, kind in (
            (socket.socket, "connect", "network"),
            (socket.socket, "connect_ex", "network"),
            (socket, "getaddrinfo", "network"),
            (socket, "create_connection", "network"),
            (urllib.request.OpenerDirector, "open", "network"),
            (providers, "generate_response", "provider_execution"),
            (threading.Thread, "start", "worker_start"),
        ):
            guard = patch.object(target, name, side_effect=deny(kind))
            guard.start()
            self.addCleanup(guard.stop)
        clock = patch.object(analysis_module, "time", SimpleNamespace(
            time=lambda: self.now, monotonic=lambda: self.monotonic))
        clock.start()
        self.addCleanup(clock.stop)
        self.assertEqual(MAX_CALLS, 36)
        self.assertEqual(BUDGET_MICRO_CNY, 3_000_000)
        self.news_path = Path(self.temp.name) / "news.sqlite3"
        self.analysis_path = Path(self.temp.name) / "analysis.sqlite3"
        self._open()

    def _open(self):
        self.news = NewsPlatform(self.news_path,
            transport=lambda *args: FeedResponse(200, self.body), clock=lambda: self.now)
        self.ai = NewsAnalysis(self.analysis_path, self.news, generate=self._fixed_result)
        self.engines.append((self.news, self.ai))

    def _advance(self, seconds):
        self.now += seconds
        self.monotonic += seconds

    def _configure(self, automatic=True, model="deepseek-flash"):
        status = self.ai.configure(FAKE_KEY, automatic, True, provider="deepseek", model=model)
        self.assertEqual(status["max_calls"], 36)
        self.assertEqual(status["budget_cny"], 3)
        self.assertTrue(status["configured"])
        self.assertFalse(status["supplier_bill_verified"])
        return status

    def _discover(self, numbers, *, excerpt=EXCERPT):
        self._advance(1)
        publication = format_datetime(datetime.fromtimestamp(self.now, timezone.utc), usegmt=True)
        entries = [f"<item><guid>queue-{number:03d}</guid>"
                   f"<title>Synthetic inflation report {number:03d}</title>"
                   f"<link>https://www.federalreserve.gov/queue-{number:03d}.htm</link>"
                   f"<pubDate>{publication}</pubDate>"
                   f"<description>{escape(excerpt)}</description></item>" for number in numbers]
        self.body = ("<rss><channel>" + "".join(entries) + "</channel></rss>").encode()
        with self.news._db() as db:
            source = db.execute("SELECT * FROM sources WHERE id='fed'").fetchone()
        self.assertTrue(self.news._fetch(source))
        wanted = {f"https://www.federalreserve.gov/queue-{number:03d}.htm" for number in numbers}
        return {item["id"] for item in self.news.list_items(limit=500)["items"] if item["url"] in wanted}

    def _fixed_result(self, key, text):
        self.assertEqual(key, FAKE_KEY)
        material = json.loads(text)
        with self.ai._db() as db:
            pending = db.execute("SELECT * FROM runs WHERE status='pending'").fetchall()
        self.assertEqual(len(pending), 1)
        row = dict(pending[0])
        task = self.news.analysis_task_for_item(row["item_id"])
        self.assertEqual(task["status"], "running")
        call = {"sequence": len(self.calls) + 1, "item_id": row["item_id"],
                "fingerprint": row["fingerprint"], "task_id": task["id"],
                "version_id": task["version_id"], "reserved": row["reserved"],
                "created": row["created"], "title": material["source_text"]["title"],
                "synthetic": True, "outcome": "fixed_local_result"}
        self.calls.append(call)
        self.assertTrue(material["source_text"]["excerpt"].startswith(EXCERPT))
        if self.crash_next:
            self.crash_next = False
            call["outcome"] = "synthetic_process_interruption_after_reservation"
            raise SyntheticProcessExit()
        return SimpleNamespace(ok=True, provider=self.ai._provider, model=self.ai._model,
            requested_model=self.ai._model, reported_model=self.ai._model,
            content=json.dumps({"summary": "合成本地测试摘要。", "key_points": [
                {"claim": "给定材料中的信息。", "evidence_field": "excerpt", "quote": EXCERPT}],
                "uncertainties": ["仅为固定合成结果，未核对全文或真实供应商质量。"]}, ensure_ascii=False))

    def _auto_once(self):
        return self.ai._next_persistent_task(self.ai._configured_at, self.ai._generation)

    def _explicit_once(self, item_id):
        self.assertEqual(self.ai.request(item_id)["status"], "queued")
        queued = self.ai._queue.get_nowait()
        # Exact dispatch used by _loop, without starting a service thread.
        target, generation, *task_ids = queued
        self.ai._analyze(target, generation=generation,
                         task_id=task_ids[0] if task_ids else None,
                         market_context_id=task_ids[1] if len(task_ids) > 1 else None)
        self.assertTrue(self.ai._queue.empty())

    def _task_rows(self):
        with self.news._db() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM news_analysis_tasks ORDER BY version_seen_at,id")]

    def _stage(self, name, **extra):
        status = self.ai.status()
        self.report["stages"].append({"name": name, "test_clock": self.now,
            "calls_reserved_in_window": status["calls_reserved"],
            "reserved_cny_in_window": status["reserved_cny"],
            "configured": status["configured"], "automatic": status["automatic"],
            "task_counts": dict(Counter(row["status"] for row in self._task_rows())), **extra})

    def test_111_items_drain_with_insertions_restart_unknown_and_unchanged_limits(self):
        self._configure()
        self.news.control(True)
        initial = self._discover(range(111))
        self.assertEqual(len(initial), 111)
        self.assertEqual(self.news.pending_analysis_tasks()["total"], 111)
        self.assertEqual(len(self.news.pending_analysis_tasks(offset=100)["tasks"]), 11)
        self.report["initial_item_ids"] = sorted(initial)
        self._stage("111_persistent_items_before_consumption")

        for _ in range(8):
            self._auto_once()
        self.assertEqual(len(self.calls), 8)
        inserted = self._discover(range(111, 114))
        self.assertEqual(len(inserted), 3)
        self.assertEqual({row["item_id"] for row in self._task_rows()}, initial | inserted)
        self.report["inserted_item_ids"] = sorted(inserted)
        self._stage("three_new_items_do_not_displace_old_tasks")
        for _ in range(7):
            self._auto_once()
        self.assertEqual(Counter(row["status"] for row in self._task_rows())["completed"], 15)
        self.crash_next = True
        with self.assertRaises(SyntheticProcessExit):
            self._auto_once()
        interrupted_id = self.calls[-1]["item_id"]
        self.assertIn(interrupted_id, initial)
        self.assertEqual(self.news.analysis_task_for_item(interrupted_id)["status"], "running")
        with self.ai._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM runs WHERE status='pending'").fetchone()[0], 1)
        self._stage("interrupted_after_committed_reservation")

        # Construction performs the real restart recovery; no task/ledger is seeded.
        self._advance(1)
        self._open()
        self.assertFalse(self.ai.status()["configured"])
        self.assertEqual(self.ai.result(self.news.get_item(interrupted_id))["status"], "unknown")
        self.assertEqual(self.news.analysis_task_for_item(interrupted_id)["status"], "unknown")
        self._configure()
        calls_before = len(self.calls)
        self._auto_once()
        self.assertEqual(len(self.calls), calls_before)
        self.assertEqual(self.news.pending_analysis_tasks()["total"], 98)
        self.report["cross_session_policy"] = (
            "New sessions exclude old version_seen_at values from automatic selection; "
            "this test explicitly requests the saved backlog after reauthorization.")
        self._stage("restart_preserves_unknown_and_requires_explicit_old_backlog_request")

        exhausted_windows = 0
        batches = 0
        while self.news.pending_analysis_tasks()["total"]:
            batches += 1
            self.assertLess(batches, 30, "bounded drain must make progress")
            page = self.news.pending_analysis_tasks(limit=7)
            for task in page["tasks"]:
                used = self.ai.status()["calls_reserved"]
                if used == MAX_CALLS:
                    attempts_before = len(self.calls)
                    self._explicit_once(task["item_id"])
                    self.assertEqual(len(self.calls), attempts_before)
                    self.assertEqual(self.news.analysis_task_for_item(task["item_id"])["status"], "pending")
                    self.assertEqual(self.ai.status()["calls_reserved"], MAX_CALLS)
                    self._configure(automatic=False)
                    self._explicit_once(task["item_id"])
                    self.assertEqual(len(self.calls), attempts_before)
                    self._stage("count_cap_and_same_window_reauthorization_do_not_bypass", batch=batches)
                    self._advance(86401)
                    self.assertFalse(self.ai.status()["configured"])
                    self.assertEqual(self.ai.status()["calls_reserved"], 0)
                    with self.assertRaisesRegex(ValueError, "analysis_session_required"):
                        self.ai.request(task["item_id"])
                    self.assertEqual(len(self.calls), attempts_before)
                    self._configure()
                    self._auto_once()
                    self.assertEqual(len(self.calls), attempts_before)
                    exhausted_windows += 1
                self._explicit_once(task["item_id"])
                self.assertEqual(self.news.analysis_task_for_item(task["item_id"])["status"], "completed")
            self._stage("explicit_saved_backlog_batch", batch=batches)

        self.assertEqual(exhausted_windows, 3)
        tasks = self._task_rows()
        by_item = {row["item_id"]: row for row in tasks}
        self.assertEqual(set(by_item), initial | inserted)
        self.assertEqual(Counter(by_item[key]["status"] for key in initial),
                         {"completed": 110, "unknown": 1})
        self.assertEqual(Counter(by_item[key]["status"] for key in inserted), {"completed": 3})
        self.assertEqual(len(self.calls), 114)
        self.assertTrue(all(count == 1 for count in Counter(call["item_id"] for call in self.calls).values()))
        for call in self.calls:
            window = [row for row in self.calls if call["created"] - 86400 <= row["created"] <= call["created"]]
            self.assertLessEqual(len(window), MAX_CALLS)
            self.assertLessEqual(sum(row["reserved"] for row in window), BUDGET_MICRO_CNY)

        self._open()
        for item_id, task in by_item.items():
            self.assertEqual(self.ai.request(item_id)["status"], task["status"])
            self.ai._analyze(item_id, task_id=task["id"])
        self.assertTrue(self.ai._queue.empty())
        self.assertEqual(len(self.calls), 114)
        self.assertEqual(self.news.pending_analysis_tasks()["total"], 0)
        self.assertEqual(Counter(row["status"] for row in self._task_rows()), {"completed": 113, "unknown": 1})
        self.report.update({"initial_count": 111, "inserted_count": 3,
                            "initial_terminal_counts": {"completed": 110, "unknown": 1},
                            "all_terminal_counts": {"completed": 113, "unknown": 1},
                            "simulated_windows": 4, "explicit_batches": batches})
        self._stage("all_114_terminal_and_second_restart_never_reexecutes")

    def test_money_cap_blocks_real_consumer_before_count_cap_and_reconfigure_cannot_reset_it(self):
        self._configure(model="deepseek-v4-pro")
        self.news.control(True)
        long_excerpt = EXCERPT + " Supplemental synthetic context preserves this stated condition." * 60
        self._discover(range(36), excerpt=long_excerpt)
        for _ in range(MAX_CALLS + 1):
            previous = len(self.calls)
            self._auto_once()
            if len(self.calls) == previous:
                break
        self.assertGreater(len(self.calls), 0)
        self.assertLess(len(self.calls), MAX_CALLS)
        with self.ai._db() as db:
            ledger = [dict(row) for row in db.execute("SELECT * FROM runs")]
        self.assertTrue(all(row["status"] == "completed" for row in ledger))
        used = sum(row["reserved"] for row in ledger)
        self.assertLessEqual(used, BUDGET_MICRO_CNY)
        self.assertGreater(used + min(row["reserved"] for row in ledger), BUDGET_MICRO_CNY)
        self.assertFalse(self.ai.status()["automatic"])
        blocked = self.news.pending_analysis_tasks(limit=1)["tasks"][0]
        previous = len(self.calls)
        self._configure(automatic=False, model="deepseek-v4-pro")
        self._explicit_once(blocked["item_id"])
        self.assertEqual(len(self.calls), previous)
        self.assertEqual(self.news.analysis_task_for_item(blocked["item_id"])["status"], "pending")
        self.report.update({"completed_before_money_cap": previous, "reserved_micro_cny": used,
                            "next_reservation_micro_cny": min(row["reserved"] for row in ledger)})
        self._stage("money_cap_precedes_count_cap_and_reauthorization_cannot_reset_it")

    def tearDown(self):
        self.report["synthetic_execution_calls"] = self.calls
        self.report["task_rows"] = self._task_rows()
        with self.ai._db() as db:
            self.report["reservation_rows"] = [dict(row) for row in db.execute("SELECT * FROM runs ORDER BY created,fingerprint")]
        self.report["analysis_controller_instances"] = len(self.engines)
        self.report["project_model_calls"] = 0
        self.report["real_source_requests"] = 0
        for news, ai in self.engines:
            self.assertIsNone(ai._thread)
            self.assertIsNone(news._thread)
            self.assertIsNone(news._article_thread)
            self.assertTrue(ai.stop())
            news.stop()
        self.assertEqual(self.report["forbidden_attempts"], {})
        self.assertNotIn(FAKE_KEY.encode(), self.analysis_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
