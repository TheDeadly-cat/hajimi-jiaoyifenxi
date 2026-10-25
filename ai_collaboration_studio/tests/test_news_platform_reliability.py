import contextlib
import sqlite3
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from backend.news_platform import FeedResponse, NewsPlatform
from backend import news_runtime_evidence as evidence
from tests.test_news_platform import NOW, rss


class NewsPlatformReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'news.db'
        self.now = NOW
        self.calls = []
        self.apps = []
        self.app = self.new_app()

    def tearDown(self):
        for app in reversed(self.apps):
            self.assertTrue(app.stop(timeout=4))
        self.temp.cleanup()

    def new_app(self, transport=None):
        def default_transport(*args):
            self.calls.append(args)
            return FeedResponse(200, rss())
        app = NewsPlatform(self.path, transport=transport or default_transport, clock=lambda:self.now)
        self.apps.append(app)
        return app

    def source(self, source_id='fed'):
        with self.app._db() as db:
            return db.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()

    def fetch(self):
        self.app._fetch(self.source())

    def wait_for(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail('worker condition not reached')

    def test_constructor_and_overview_do_not_invent_a_session_or_complete_fetching(self):
        with self.app._db() as db:
            db.execute("UPDATE sources SET status='fetching' WHERE id='fed'")
        reader = self.new_app()
        state = reader.overview()
        self.assertIsNone(state['continuity']['session_started_at'])
        self.assertFalse(state['continuity']['continuous_24h_proven'])
        self.assertEqual(next(s['status'] for s in state['sources'] if s['id']=='fed'), 'fetching')
        with reader._db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM monitor_sessions').fetchone()[0], 0)
        self.assertEqual(self.calls, [])

    def test_started_receipt_is_durable_before_transport_and_counts_are_actual_changes(self):
        def transport(*args):
            with contextlib.closing(sqlite3.connect(self.path)) as independent:
                self.assertEqual(independent.execute("SELECT COUNT(*) FROM poll_runs WHERE outcome='started'").fetchone()[0], 1)
            return FeedResponse(200, rss())
        self.app.transport = transport
        self.fetch()
        self.fetch()
        self.app.transport = lambda *args: FeedResponse(200, rss(title='Changed factual title'))
        self.fetch()
        self.app.transport = lambda *args: FeedResponse(304)
        self.fetch()
        state = self.app.overview()
        self.assertEqual(state['checks_24h'], {'started':4,'completed':4,'successful':4,'failed':0,'interrupted':0,'not_modified':1,'new_items':1,'updated_items':1})
        self.assertEqual(state['recent_checks'][0]['outcome'], 'not_modified')
        self.assertEqual(state['recent_checks'][0]['new_items'], 0)
        self.assertEqual(state['recent_checks'][0]['updated_items'], 0)

    def test_restart_marks_pending_and_fetching_unknown_without_fabricating_clean_stop(self):
        self.app.control(True)
        self.app._ensure_session()
        old_id = self.app._session_id
        with self.app._db() as db:
            db.execute("UPDATE sources SET status='fetching',last_success=? WHERE id='fed'", (self.now-100,))
            evidence.begin_poll(db, old_id, 'fed', self.now)
        self.app._session_id = None  # Simulated lost process: no normal close occurs.
        self.now += 400
        replacement = self.new_app()
        replacement._ensure_session()
        state = replacement.overview()
        self.assertEqual(state['recent_checks'][0]['outcome'], 'interrupted_unknown')
        self.assertTrue(state['continuity']['previous_session_unclean'])
        self.assertEqual(state['continuity']['last_gap']['kind'], 'unclean_restart')
        self.assertEqual(state['continuity']['last_gap']['duration_seconds'], 400)
        with replacement._db() as db:
            old = db.execute('SELECT state,stopped FROM monitor_sessions WHERE id=?', (old_id,)).fetchone()
            self.assertEqual(tuple(old), ('interrupted_unknown', None))
            source = db.execute("SELECT status,last_success FROM sources WHERE id='fed'").fetchone()
            self.assertEqual(tuple(source), ('interrupted_unknown', NOW-100))

    def test_heartbeat_gap_survives_restart_and_never_certifies_24h(self):
        self.app.control(True)
        self.app._ensure_session()
        self.now += 91
        self.app._record_tick()
        state = self.app.overview()
        self.assertEqual(state['continuity']['last_gap']['kind'], 'heartbeat_gap')
        self.assertEqual(state['continuity']['last_gap']['duration_seconds'], 91)
        self.now += 86400
        self.app._record_tick()
        self.assertFalse(self.app.overview()['continuity']['continuous_24h_proven'])
        with self.new_app()._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM monitor_gaps WHERE kind='heartbeat_gap'").fetchone()[0], 2)

    def test_clean_restart_and_pause_have_distinct_persistent_intervals(self):
        self.app.control(True)
        self.app._ensure_session()
        self.now += 30
        self.app.control(False)
        self.now += 20
        self.assertEqual(self.app.overview()['continuity']['last_gap']['kind'], 'monitor_paused')
        self.app.control(True)
        self.app.stop()
        self.now += 40
        replacement = self.new_app()
        replacement._ensure_session()
        state = replacement.overview()
        self.assertFalse(state['continuity']['previous_session_unclean'])
        self.assertEqual(state['continuity']['last_gap']['kind'], 'clean_restart')
        self.assertEqual(state['continuity']['last_gap']['duration_seconds'], 40)
        with replacement._db() as db:
            self.assertEqual(db.execute("SELECT ended-started FROM monitor_gaps WHERE kind='monitor_paused'").fetchone()[0], 20)

    def test_failure_keeps_old_content_and_backoff_and_durable_failure(self):
        self.fetch()
        old = self.app.list_items()
        self.app.transport = lambda *args: FeedResponse(503)
        self.fetch()
        self.assertEqual(self.app.list_items(), old)
        state = self.app.overview()
        self.assertEqual(state['checks_24h']['failed'], 1)
        self.assertEqual(state['checks_24h']['new_items'], 1)
        self.assertEqual(state['recent_checks'][0]['http_status'], 503)
        self.assertEqual(self.source()['next_poll'], self.now+900)

    def test_old_success_is_stale_and_overdue_without_counting_it_healthy(self):
        self.app.control(True)
        self.fetch()
        self.now += 1000
        state = self.app.overview()
        source = next(s for s in state['sources'] if s['id']=='fed')
        self.assertTrue(source['stale'])
        self.assertTrue(source['overdue'])
        self.assertEqual(source['overdue_seconds'], 100)
        self.assertEqual(state['counts']['sources_ok'], 0)

    def test_transient_database_failure_keeps_worker_alive_and_recovers(self):
        original = self.app._db
        failures = []
        @contextlib.contextmanager
        def intermittent():
            if threading.current_thread().name == 'official-news-poller' and not failures:
                failures.append('injected')
                raise sqlite3.OperationalError('synthetic temporary failure')
            with original() as db:
                yield db
        self.app._db = intermittent
        self.app.start()
        self.wait_for(lambda:self.app._worker_failures == 1)
        state = self.app.overview()
        self.assertEqual(state['worker_health']['status'], 'recovering')
        self.assertEqual(state['worker_health']['last_error_code'], 'database_unavailable')
        self.assertTrue(self.app._thread.is_alive())
        self.wait_for(lambda:self.app._session_id is not None and self.app._worker_failures == 0)
        self.assertEqual(self.app.overview()['worker_health']['status'], 'paused')
        self.assertEqual(self.calls, [])

    def test_persistent_worker_failures_back_off_with_a_30_second_cap(self):
        delays = []
        class Wake:
            def clear(inner): pass
            def wait(inner, delay):
                delays.append(delay)
                if len(delays) == 8:
                    self.app._stop.set()
        original_wake = self.app._wake
        self.app._wake = Wake()
        try:
            with patch.object(self.app, '_cycle', side_effect=sqlite3.OperationalError('synthetic')):
                self.app._loop()
        finally:
            self.app._wake = original_wake
        self.assertEqual(delays, [1,2,4,8,16,30,30,30])
        self.assertEqual(self.app._worker_failures, 8)

    def test_live_thread_with_stale_heartbeat_is_not_reported_healthy(self):
        self.app.control(True)
        self.app._ensure_session()
        self.now += 91
        self.app._thread = SimpleNamespace(is_alive=lambda:True)
        try:
            state = self.app.overview()
            self.assertTrue(state['worker_health']['alive'])
            self.assertFalse(state['worker_health']['heartbeat_fresh'])
            self.assertEqual(state['worker_health']['status'], 'unresponsive')
        finally:
            self.app._thread = None

    def test_manual_refresh_survives_local_write_failure_without_refetching_completed_sources(self):
        original = evidence.finish_poll
        attempts = []
        def finish(*args, **kwargs):
            attempts.append(args[3])
            if len(attempts) == 2:
                raise sqlite3.OperationalError('synthetic second-source write failure')
            return original(*args, **kwargs)
        with patch.object(evidence, 'finish_poll', side_effect=finish):
            self.app.start()
            self.app.refresh()
            self.wait_for(lambda:self.app.overview()['checks_24h']['successful']==3)
        state = self.app.overview()
        self.assertFalse(state['enabled'])
        self.assertFalse(state['refresh_pending'])
        self.assertEqual(state['checks_24h']['interrupted'], 1)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(sum('bls.gov' in call[0] for call in self.calls), 1)

    def test_dead_thread_is_not_retroactively_recorded_as_clean_when_stop_is_called(self):
        self.app._ensure_session()
        session = self.app._session_id
        dead = threading.Thread(target=lambda:None)
        dead.start()
        dead.join()
        self.app._thread = dead
        self.assertTrue(self.app.stop())
        with self.app._db() as db:
            row = db.execute('SELECT state,stopped FROM monitor_sessions WHERE id=?', (session,)).fetchone()
            self.assertEqual(tuple(row), ('interrupted_unknown',None))

    def test_control_true_recovers_a_dead_existing_thread_and_only_fetches_due_once(self):
        dead = threading.Thread(target=lambda:None)
        dead.start()
        dead.join()
        self.app._thread = dead
        self.app.control(True)
        self.wait_for(lambda:self.app.overview()['checks_24h']['successful']==3)
        self.assertIsNot(self.app._thread, dead)
        self.assertTrue(self.app._thread.is_alive())
        self.app._wake.set()
        time.sleep(.1)
        self.assertEqual(len(self.calls), 3)

    def test_refresh_recovers_a_dead_thread_without_enabling_monitor(self):
        dead = threading.Thread(target=lambda:None)
        dead.start()
        dead.join()
        self.app._thread = dead
        self.app.refresh()
        self.wait_for(lambda:self.app.overview()['checks_24h']['successful']==3)
        self.assertFalse(self.app.overview()['enabled'])
        self.assertEqual(len(self.calls), 3)

    def test_message_and_terminal_receipt_rollback_together(self):
        with patch.object(evidence, 'finish_poll', side_effect=sqlite3.OperationalError('synthetic commit-stage failure')):
            with self.assertRaises(sqlite3.OperationalError):
                self.fetch()
        self.assertEqual(self.app.list_items()['total'], 0)
        self.assertEqual(self.app.overview()['recent_checks'][0]['outcome'], 'started')
        self.assertEqual(self.source()['status'], 'fetching')
        self.app._record_tick()
        self.assertEqual(self.app.overview()['recent_checks'][0]['outcome'], 'interrupted_unknown')
        self.fetch()
        self.assertEqual(self.app.list_items()['total'], 1)
        self.assertEqual(self.app.overview()['checks_24h']['new_items'], 1)

    def test_stop_is_only_clean_after_the_inflight_poll_drains(self):
        entered, release = threading.Event(), threading.Event()
        def transport(*args):
            entered.set()
            if not release.wait(3):
                raise AssertionError('release missing')
            return FeedResponse(200, rss())
        self.app.transport = transport
        self.app.start()
        self.app.refresh()
        try:
            self.assertTrue(entered.wait(3))
            self.assertFalse(self.app.stop(timeout=0))
            with self.app._db() as db:
                self.assertEqual(db.execute('SELECT state FROM monitor_sessions').fetchone()[0], 'running')
        finally:
            release.set()
        self.assertTrue(self.app.stop(timeout=4))
        with self.app._db() as db:
            self.assertEqual(db.execute('SELECT state FROM monitor_sessions').fetchone()[0], 'stopped')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM poll_runs WHERE outcome='started'").fetchone()[0], 0)

    def test_recent_checks_are_limited_and_retention_does_not_delete_news(self):
        self.fetch()
        session = self.app._session_id
        with self.app._db() as db:
            for index in range(12):
                poll = evidence.begin_poll(db, session, 'fed', self.now+index)
                evidence.finish_poll(db, poll, self.now+index, 'not_modified', http_status=304)
            old = evidence.begin_poll(db, session, 'fed', self.now-31*86400)
            evidence.finish_poll(db, old, self.now-31*86400, 'failed')
        self.assertEqual(len(self.app.overview()['recent_checks']), 10)
        with patch.object(evidence, 'MAX_EVIDENCE_ROWS', 5), self.app._db() as db:
            final = evidence.begin_poll(db, session, 'fed', self.now+15)
            evidence.finish_poll(db, final, self.now+15, 'not_modified', http_status=304)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM poll_runs').fetchone()[0], 5)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM poll_runs WHERE id=?', (old,)).fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM items').fetchone()[0], 1)

    def test_network_error_then_due_recovery_is_recorded_without_deleting_items(self):
        failed = []
        def transport(url, *args):
            if 'federalreserve' in url and not failed:
                failed.append(True)
                raise TimeoutError('synthetic offline')
            return FeedResponse(200, rss())
        self.app.transport = transport
        self.app.control(True)
        self.app.start()
        self.wait_for(lambda:self.app.overview()['checks_24h']['completed']==3)
        before = self.app.list_items()['total']
        self.now += 901
        self.app._wake.set()
        self.wait_for(lambda:self.app.overview()['checks_24h']['completed']==6)
        state = self.app.overview()
        self.assertEqual(state['checks_24h']['failed'], 1)
        self.assertEqual(state['checks_24h']['successful'], 5)
        self.assertGreaterEqual(self.app.list_items()['total'], before)
        self.assertEqual(self.source()['failures'], 0)


if __name__ == '__main__':
    unittest.main()
