"""Bounded enrichment scheduling, using an isolated synthetic SQLite ledger."""
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.news_review_controller import NewsReviewController


class EnrichmentBatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='bounded-enrichment-offline-')
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)/'synthetic.sqlite3'
        self.store = SimpleNamespace(_lock=threading.RLock(), _connect=self.connect)
        with closing(self.connect()) as db:
            db.executescript("""
                CREATE TABLE news_review_events(
                    policy_id TEXT NOT NULL,item_id TEXT NOT NULL,discovered_at INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'LINKED_REVIEW',error_code TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(policy_id,item_id));
                CREATE TABLE source_document_versions(id TEXT PRIMARY KEY,item_id TEXT NOT NULL);
                CREATE TABLE source_document_jobs(id TEXT,item_id TEXT,session_id TEXT,
                    status TEXT,requested_at INTEGER);
            """)
        self.service = SimpleNamespace(store=self.store, check_clock=Mock(), discover=Mock(),
                                       queue=Mock(return_value='already-deduplicated'), request_document=Mock(),
                                       documents=SimpleNamespace(run=Mock()))
        self.controller = NewsReviewController(self.service, 'current')
        self.enterContext(patch('backend.news_review_controller._policy', return_value=(None, {})))

    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def add_events(self, ids, *, policy='current', status='LINKED_REVIEW'):
        with closing(self.connect()) as db, db:
            db.executemany('INSERT INTO news_review_events(policy_id,item_id,discovered_at,status) VALUES(?,?,?,?)',
                           ((policy, item, 1, status) for item in ids))

    def add_versions(self, ids):
        with closing(self.connect()) as db, db:
            db.executemany('INSERT INTO source_document_versions VALUES(?,?)',
                           ((f'version-{index}-{item}', item) for index, item in enumerate(ids)))

    def queued(self):
        return [call.args[1] for call in self.service.queue.call_args_list]

    def test_large_history_cannot_starve_new_discoveries_or_make_an_unbounded_cycle(self):
        self.add_events(f'old-{index:06d}' for index in range(50000))
        fresh = [f'zz-new-{index:03d}' for index in range(50)]
        self.service.discover.side_effect = lambda _: self.add_events(fresh)
        self.controller.enrich_cycle()
        queued = self.queued()
        self.assertEqual(queued[:50], fresh)
        self.assertEqual(len(queued), 50 + self.controller.HISTORY_BATCH)
        self.assertLessEqual(len(queued), sum((self.controller.NEW_EVENT_BATCH,
                                             self.controller.DOCUMENT_VERSION_BATCH, self.controller.HISTORY_BATCH)))
        self.service.documents.run.assert_not_called()

    def test_new_body_version_prioritizes_an_old_event(self):
        self.add_events(f'old-{index:06d}' for index in range(1000))
        old = 'zz-old-event-with-new-body'
        self.add_events([old])
        self.controller._initialize_scan()
        self.add_versions([old])
        self.controller.enrich_cycle()
        self.assertEqual(self.queued()[0], old)
        self.assertEqual(len(self.queued()), self.controller.HISTORY_BATCH + 1)

    def test_restart_checks_a_recent_body_that_arrived_while_the_controller_was_down(self):
        self.add_events(f'old-{index:06d}' for index in range(1000))
        revised = 'zz-revised-during-downtime'
        self.add_events([revised])
        self.add_versions(['unrelated-before-restart', revised])
        self.controller.enrich_cycle()
        self.assertEqual(self.queued()[0], revised)
        self.assertEqual(len(self.queued()), self.controller.HISTORY_BATCH + 1)

    def test_restart_document_lookback_remains_bounded(self):
        items = [f'old-{index:06d}' for index in range(1000)]
        self.add_events(items)
        self.add_versions(items)
        self.controller.enrich_cycle()
        self.assertEqual(len(self.queued()), self.controller.DOCUMENT_VERSION_BATCH + self.controller.HISTORY_BATCH)
        self.assertEqual(set(self.queued()[:self.controller.DOCUMENT_VERSION_BATCH]),
                         set(items[-self.controller.DOCUMENT_VERSION_BATCH:]))

    def test_one_event_in_all_lanes_is_queued_once(self):
        self.controller._initialize_scan()
        self.add_events(['same-item'], status='WAITING_DOCUMENT')
        self.add_versions(['same-item'])
        self.service.queue.return_value = None
        self.controller.enrich_cycle()
        self.assertEqual(self.queued(), ['same-item'])
        self.service.request_document.assert_called_once_with('current', 'same-item')

    def test_rotating_history_eventually_revisits_every_event_and_wraps(self):
        expected = {f'old-{index:06d}' for index in range(1001)}
        self.add_events(sorted(expected))
        seen = set()
        for _ in range(64):
            self.service.queue.reset_mock()
            self.controller.enrich_cycle()
            seen.update(self.queued())
            self.assertLessEqual(len(self.queued()), self.controller.HISTORY_BATCH)
        self.assertEqual(seen, expected)
        self.assertIn('old-000000', self.queued())

    def test_other_policies_and_unrelated_versions_have_bounded_scan_allowances(self):
        self.add_events(['historical'])
        self.controller._initialize_scan()
        self.add_events((f'foreign-{index:03d}' for index in range(200)), policy='other')
        self.add_versions(f'foreign-{index:03d}' for index in range(200))
        fresh = [f'new-{index:03d}' for index in range(50)]
        self.add_events(fresh)
        seen = set()
        for _ in range(5):
            self.service.queue.reset_mock()
            previous = (self.controller._event_cursor, self.controller._document_cursor)
            self.controller.enrich_cycle()
            seen.update(self.queued())
            self.assertLessEqual(self.controller._event_cursor-previous[0], self.controller.NEW_EVENT_BATCH)
            self.assertLessEqual(self.controller._document_cursor-previous[1], self.controller.DOCUMENT_VERSION_BATCH)
            self.assertFalse(any(item.startswith('foreign') for item in self.queued()))
        self.assertTrue(set(fresh).issubset(seen))

    def test_restart_catches_up_without_deleting_or_replaying_ledger_state(self):
        expected = {f'old-{index:03d}' for index in range(21)}
        self.add_events(sorted(expected))
        self.controller.enrich_cycle()
        restarted = NewsReviewController(self.service, 'current')
        self.service.queue.reset_mock()
        restarted.enrich_cycle()
        restarted.enrich_cycle()
        self.assertEqual(set(self.queued()), expected)
        with closing(self.connect()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM news_review_events').fetchone()[0], len(expected))

    def test_stop_is_checked_between_items(self):
        self.add_events(f'old-{index:03d}' for index in range(100))
        calls = []
        def queue(_policy, item):
            calls.append(item)
            if len(calls) == 3:
                self.controller.request_stop()
            return 'already-deduplicated'
        self.service.queue.side_effect = queue
        self.controller.enrich_cycle()
        self.assertEqual(len(calls), 3)
        self.service.documents.run.assert_not_called()
        self.controller.enrich_cycle()
        self.assertEqual(len(calls), 3)

    def test_failed_item_remains_available_to_a_new_controller(self):
        self.add_events(['failed-item'])
        self.service.queue.side_effect = RuntimeError('synthetic failure')
        with self.assertRaises(RuntimeError):
            self.controller.enrich_cycle()
        self.service.queue.side_effect = None
        self.service.queue.reset_mock()
        NewsReviewController(self.service, 'current').enrich_cycle()
        self.assertEqual(self.queued(), ['failed-item'])


class EnrichmentLedgerIntegrationTests(unittest.TestCase):
    def test_new_event_and_body_revision_keep_real_ledger_deduplication(self):
        from tests import test_news_review as fixtures
        from tests.test_document_evidence import import_event, SEC_HTML
        from backend.document_evidence import RECHECK_MS
        fixture = fixtures.NewsReviewTests()
        fixture.addCleanup = self.addCleanup
        fixture.setUp()
        fixture.approve()
        controller = NewsReviewController(fixture.service, fixture.policy['policy_id'])
        controller.enrich_cycle()
        item = import_event(fixture.store)
        controller.enrich_cycle()
        self.assertEqual(fixture.fetcher.call_count, 1)
        self.assertEqual(fixture.run_one().call_count, 1)
        controller.enrich_cycle()
        self.assertEqual(fixture.run_one().call_count, 0)
        self.assertEqual(fixture.count('provider_call_attempts'), 1)
        fixture.now += RECHECK_MS + 1
        fixture.raw = SEC_HTML.replace(b'$10 million', b'$11 million')
        job = fixture.documents.request(item, session_id='fixture-manual', confirmation=True, refresh=True)
        fixture.documents.run(job['id'], cancel_event=fixture.cancel,
                              deadline_monotonic_ms=int(time.monotonic()*1000)+12000)
        controller.enrich_cycle()
        self.assertEqual(fixture.run_one().call_count, 1)
        controller.enrich_cycle()
        self.assertEqual(fixture.run_one().call_count, 0)
        self.assertEqual(fixture.count('news_review_jobs'), 2)
        self.assertEqual(fixture.count('provider_call_attempts'), 2)
        self.assertEqual(fixture.view(item)['state'], 'REVIEWED')

class EnrichmentBatchNativeIntegrationTests(unittest.TestCase):
    """工作包 B：真实临时 SQLite + 原生服务/控制器/账本，仅外部模型传输为合成。

    每个测试用独立夹具（各自的临时数据库）。外部模型传输是夹具的进程内合成响应
    器，绝不发真实 provider 请求；discover、queue 与账本都使用原生实现。
    原生文档读取预算（滚动窗口内 6 次）限制了单个
    测试可执行的正文刷新次数，故每个场景都按此规模设计。大历史轮转用已链接
    （LINKED_REVIEW）填充事件驱动，它们从不触发正文读取。

    跨策略夹具先通过原生审批建立旧策略，再让它到期后审批当前策略，保留真实
    another_policy_active 与外键约束。旧策略条目只作为物理扫描范围内的背景记录，
    不执行旧策略的采集或模型调用。各项默认批次额度仍由原生选择器执行。
    """

    def _fixture(self, *, previous_policy=False):
        from tests import test_news_review as fixtures
        fixture = fixtures.NewsReviewTests()
        fixture.addCleanup = self.addCleanup
        fixture.setUp()
        if previous_policy:
            from tests.test_document_evidence import import_event
            previous = dict(fixture.policy)
            previous['policy_id'] += '-previous'
            previous['expires_at_ms'] = fixture.now + 1000
            preview = fixture.service.preview(previous)
            fixture.service.approve(previous, approved_policy_sha256=preview['policy_sha256'])
            fixture.previous_policy_id = previous['policy_id']
            fixture.foreign_items = [import_event(fixture.store, index) for index in range(100, 108)]
            # Preserve the native one-active-policy gate. Nothing is force-closed
            # or deleted, and the second approval happens only after expiry.
            fixture.now = previous['expires_at_ms'] + 1
            fixture.policy.update(not_before_ms=fixture.now, expires_at_ms=fixture.now+86_400_000)
        fixture.approve()
        return fixture

    def _refresh(self, fixture, item, raw):
        from backend.document_evidence import RECHECK_MS, current_document
        fixture.now += RECHECK_MS + 1
        fixture.raw = raw
        job = fixture.documents.request(item, session_id='manual-synthetic',
                                        confirmation=True, refresh=True)
        fixture.documents.run(job['id'], cancel_event=fixture.cancel,
                              deadline_monotonic_ms=int(time.monotonic()*1000)+12000)
        return current_document(fixture.documents.view(item))

    def test_B1_large_history_a_b_a_reuses_old_version_within_bounded_rotation(self):
        self._exercise_B1_large_history_a_b_a()

    def _exercise_B1_large_history_a_b_a(self):
        import math
        from contextlib import closing
        from tests.test_document_evidence import import_event, SEC_HTML
        from backend.document_evidence import current_document, bound_source
        from backend.news_review_contracts import event_key

        f = self._fixture()
        pid = f.policy['policy_id']
        item, job_a = f.prepare()
        f.run_one()
        doc_a = current_document(f.documents.view(item))
        receipt_a = f.view(item)['reviews'][0]['receipt']
        self.assertEqual(f.count('provider_call_attempts'), 1)

        # Large already-linked history that never triggers a document read.
        filler = [import_event(f.store, i) for i in range(2, 42)]
        with closing(f.store._connect()) as db, db:
            db.executemany(
                "INSERT OR REPLACE INTO news_review_events"
                "(policy_id,item_id,event_key,discovered_at,status) VALUES(?,?,?,?,?)",
                [(pid, it, event_key(bound_source(f.documents.item(it))), 1, 'LINKED_REVIEW') for it in filler])
        total_events = 1 + len(filler)

        # B: a genuine new body version -> new version row + re-review.
        doc_b = self._refresh(f, item, SEC_HTML.replace(b'$10 million', b'$11 million'))
        self.assertNotEqual(doc_b['id'], doc_a['id'])
        job_b = f.service.queue(pid, item)
        self.assertNotEqual(job_b, job_a)
        f.run_one()
        self.assertEqual(f.count('provider_call_attempts'), 2)
        versions_after_b = f.count('source_document_versions')
        self.assertEqual(versions_after_b, 2)
        reserved_after_b = f.service.snapshot(pid)['calls_reserved']
        self.assertEqual(reserved_after_b, 2)

        controller = NewsReviewController(f.service, pid)
        controller.enrich_cycle()  # Native discovery consumes the filler inbox.
        event_cursor, document_cursor = controller._event_cursor, controller._document_cursor
        controller._history_after = item

        # A again: the restored body reuses the original version, adds no new row,
        # and never rewrites the original receipt.
        doc_a2 = self._refresh(f, item, SEC_HTML)
        self.assertEqual(doc_a2, doc_a)
        self.assertEqual(f.count('source_document_versions'), versions_after_b)
        self.assertEqual(f.run_one().call_count, 0)           # no extra model call
        self.assertEqual(f.count('provider_call_attempts'), 2)
        self.assertEqual(f.view(item)['reviews'][0]['receipt'], receipt_a)

        # Bounded historical rotation reaches the item again without replaying it.
        visited = set()
        first_visit = {}
        real_queue = f.service.queue
        def observe_queue(pol, it):
            visited.add(it)
            first_visit.setdefault(it, cycle)
            return real_queue(pol, it)

        bound = math.ceil(total_events / controller.HISTORY_BATCH) + 1
        with patch.object(f.service, 'queue', side_effect=observe_queue):
            for cycle in range(1, bound + 1):
                controller.enrich_cycle()
                if cycle == 1:
                    self.assertNotIn(item, visited, 'first rotation starts strictly after the target')
            # Freeze only calls made by the controller. Assert reachability before
            # any manual queue call can put the target in the observation set.
            rotation_visits = frozenset(visited)
            rotation_first_visit = dict(first_visit)
            self.assertIn(item, rotation_visits, 'B1 history rotation never queued target')
            self.assertGreater(rotation_first_visit[item], 1)
            self.assertLessEqual(rotation_first_visit[item], bound)
        self.assertEqual(controller._event_cursor, event_cursor)
        self.assertEqual(controller._document_cursor, document_cursor)
        self.assertEqual(real_queue(pid, item), job_a)
        self.assertEqual(f.count('provider_call_attempts'), 2)  # no re-reservation
        self.assertEqual(f.service.snapshot(pid)['calls_reserved'], reserved_after_b)
        self.assertEqual(f.count('news_review_all_jobs'), 2)      # no new job
        self.assertEqual(f.count('source_document_versions'), versions_after_b)
        self.assertEqual(f.view(item)['reviews'][0]['receipt'], receipt_a)
        self.assertEqual(f.view(item)['state'], 'REVIEWED')
        return {'target': item, 'first_visit_cycle': rotation_first_visit[item],
                'bound': bound, 'rotation_visits': rotation_visits}

    def test_B1_reachability_assertion_rejects_omission_then_native_selector_passes(self):
        native_batch = NewsReviewController._event_batch
        targets, removed = {}, []

        def omit_history_target(controller):
            # The scenario consumes append lanes, then starts history strictly
            # after its target. Initial discovery starts with an empty cursor.
            if controller._history_after and controller not in targets:
                targets[controller] = controller._history_after
            events = native_batch(controller)
            target = targets.get(controller)
            removed.extend(row['item_id'] for row in events if row['item_id'] == target)
            return [row for row in events if row['item_id'] != target]

        with patch.object(NewsReviewController, '_event_batch', omit_history_target):
            with self.assertRaisesRegex(AssertionError, 'B1 history rotation never queued target'):
                self._exercise_B1_large_history_a_b_a()
        self.assertEqual(len(targets), 1)
        self.assertTrue(removed, 'the fault must actually omit a native target selection')
        self.assertEqual(set(removed), set(targets.values()))
        self.assertIs(NewsReviewController._event_batch, native_batch)
        # A fresh independent fixture repeats the complete A->B->A scenario
        # with the original selector and all ledger/receipt assertions intact.
        proof = self._exercise_B1_large_history_a_b_a()
        self.assertIn(proof['target'], proof['rotation_visits'])
        self.assertGreater(proof['first_visit_cycle'], 1)
        self.assertLessEqual(proof['first_visit_cycle'], proof['bound'])

    def test_B2_new_event_and_new_body_same_cycle_keep_independent_allowances(self):
        from contextlib import closing
        from unittest.mock import patch
        from tests.test_document_evidence import import_event, SEC_HTML
        from backend.document_evidence import current_document

        f = self._fixture(previous_policy=True)
        pid = f.policy['policy_id']
        self.assertEqual(f.count('news_review_policies'), 2)
        item, _ = f.prepare()
        f.run_one()
        self.assertEqual(f.view(item)['state'], 'REVIEWED')
        doc_a = current_document(f.documents.view(item))
        controller = NewsReviewController(f.service, pid)
        controller._initialize_scan()
        previous_event, previous_doc = controller._event_cursor, controller._document_cursor

        doc_b = self._refresh(f, item, SEC_HTML.replace(b'$10 million', b'$11 million'))
        self.assertNotEqual(doc_b['id'], doc_a['id'])
        with closing(f.store._connect()) as db, db:
            self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            db.executemany("INSERT INTO news_review_events "
                "(policy_id,item_id,event_key,discovered_at,status) VALUES(?,?,?,?,?)",
                [(f.previous_policy_id, foreign, 'foreign-'+foreign, f.now, 'LINKED_REVIEW')
                 for foreign in f.foreign_items])

        # More than one history batch ensures the selected minimum new ID cannot
        # be reached through the history lane. It has no body before this cycle,
        # so only the fresh lane can supply it to the first batch.
        arrivals = [import_event(f.store, index) for index in range(5, 26)]
        f.service.discover(pid)
        item_new = min(arrivals)
        with closing(f.store._connect()) as db, db:
            db.executemany("UPDATE news_review_events SET status='LINKED_REVIEW' "
                           "WHERE policy_id=? AND item_id=?",
                           [(pid, identifier) for identifier in arrivals if identifier != item_new])
            history = db.execute("SELECT item_id FROM news_review_events WHERE policy_id=? "
                                 "AND item_id>? ORDER BY item_id LIMIT ?",
                                 (pid, item_new, controller.HISTORY_BATCH)).fetchall()
        self.assertEqual(len(history), controller.HISTORY_BATCH)
        self.assertIsNone(current_document(f.documents.view(item_new)))
        controller._history_after = item_new
        batches = []
        real_batch = controller._event_batch
        def observe_batch():
            result = real_batch()
            batches.append(result)
            return result

        with patch.object(controller, '_event_batch', side_effect=observe_batch):
            controller.enrich_cycle()
        self.assertEqual(len(batches), 1)
        ids = [event['item_id'] for event in batches[0]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn(item, ids)
        self.assertIn(item_new, ids)
        self.assertFalse(set(ids).intersection(f.foreign_items))
        self.assertEqual(controller._event_cursor-previous_event,
                         len(f.foreign_items)+len(arrivals))
        self.assertGreater(controller._event_cursor-previous_event, 0)
        self.assertLessEqual(controller._event_cursor-previous_event, controller.NEW_EVENT_BATCH)
        self.assertGreater(controller._document_cursor-previous_doc, 0)
        self.assertLessEqual(controller._document_cursor-previous_doc, controller.DOCUMENT_VERSION_BATCH)
        self.assertLessEqual(len(ids), controller.NEW_EVENT_BATCH+controller.DOCUMENT_VERSION_BATCH+controller.HISTORY_BATCH)

        f.run_one()
        f.run_one()
        self.assertEqual(f.view(item)['state'], 'REVIEWED')
        self.assertEqual(f.view(item_new)['state'], 'REVIEWED')
        self.assertEqual(f.count('provider_call_attempts'), 3)
        self.assertEqual(f.service.snapshot(f.previous_policy_id)['calls_reserved'], 0)

    def test_B3_rebuilt_controller_does_not_replay_completed_or_unknown(self):
        from contextlib import closing
        from tests.test_document_evidence import import_event
        from backend.news_review_service import NewsReviewService

        f = self._fixture()
        pid = f.policy['policy_id']
        controller = NewsReviewController(f.service, pid)
        controller.enrich_cycle()
        item_done = import_event(f.store, 1)
        controller.enrich_cycle()
        f.run_one()
        self.assertEqual(f.view(item_done)['state'], 'REVIEWED')
        receipt_done = f.view(item_done)['reviews'][0]['receipt']

        f.now += 1000
        item_unknown = import_event(f.store, 2)
        controller.enrich_cycle()
        f.now += 1000
        item_waiting = import_event(f.store, 3)
        controller.enrich_cycle()
        self.assertEqual(f.view(item_waiting)['state'], 'QUEUED')
        f.run_one(TimeoutError('synthetic-timeout'))
        self.assertEqual(f.view(item_unknown)['state'], 'UNKNOWN')
        attempts_before = f.count('provider_call_attempts')
        jobs_before = f.count('news_review_all_jobs')
        reserved_before = f.service.snapshot(pid)['calls_reserved']
        with closing(f.store._connect()) as db:
            terminal_jobs = [dict(row) for row in db.execute(
                "SELECT * FROM news_review_all_jobs WHERE policy_id=? AND status!='QUEUED' "
                "ORDER BY id", (pid,)).fetchall()]

        f.service = NewsReviewService(f.store, f.providers, instance_owner=f.owner,
                                      documents=f.documents, clock=lambda:f.now,
                                      monotonic_ms=lambda:f.now)
        f.service.recover()
        rebuilt = NewsReviewController(f.service, pid)
        rebuilt.enrich_cycle()
        rebuilt.enrich_cycle()
        # UNKNOWN permanently closes the paid lane. The never-sent reservation
        # stays queued; recovery cannot refund, retry, or reopen that lane.
        self.assertEqual(f.run_one().call_count, 0)
        self.assertEqual(f.count('provider_call_attempts'), attempts_before)
        self.assertEqual(f.count('news_review_all_jobs'), jobs_before)
        self.assertEqual(f.service.snapshot(pid)['calls_reserved'], reserved_before)
        self.assertEqual(f.view(item_done)['state'], 'REVIEWED')
        self.assertEqual(f.view(item_unknown)['state'], 'UNKNOWN')
        self.assertEqual(f.view(item_waiting)['state'], 'QUEUED')
        self.assertEqual(f.view(item_done)['reviews'][0]['receipt'], receipt_done)
        with closing(f.store._connect()) as db:
            after = [dict(row) for row in db.execute(
                "SELECT * FROM news_review_all_jobs WHERE policy_id=? AND status!='QUEUED' "
                "ORDER BY id", (pid,)).fetchall()]
        self.assertEqual(after, terminal_jobs)


if __name__ == '__main__':
    unittest.main()
