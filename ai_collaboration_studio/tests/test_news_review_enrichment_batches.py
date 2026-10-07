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


if __name__ == '__main__':
    unittest.main()
