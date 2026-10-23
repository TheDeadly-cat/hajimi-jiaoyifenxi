"""Offline behavioral checks for source cooldowns; all transports are fakes."""
import contextlib
import json
import socket
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

from backend.news_platform import APPLICATION_ID, FeedResponse, NewsPlatform, SOURCES
from backend import news_runtime_evidence as evidence
from tests.test_news_platform import NOW, rss


class NewsCooldownTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'news.db'
        self.now = NOW
        self.calls = []
        self.responses = {}
        self.apps = []
        self.guard = patch.object(socket.socket, 'connect', side_effect=AssertionError('offline_only'))
        self.guard.start()
        self.app = self.new_app()

    def tearDown(self):
        for app in reversed(self.apps):
            self.assertTrue(app.stop(timeout=2))
        self.guard.stop()
        self.temp.cleanup()

    def transport(self, url, *args):
        source_id = next(source['id'] for source in SOURCES if source['url'] == url)
        self.calls.append((source_id, self.now))
        response = self.responses.get(source_id, FeedResponse(304))
        if isinstance(response, Exception):
            raise response
        return response

    def new_app(self, **kwargs):
        app = NewsPlatform(self.path, transport=self.transport, clock=lambda:self.now, **kwargs)
        self.apps.append(app)
        return app

    def source(self, source_id='seagate'):
        with self.app._db() as db:
            return db.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()

    def view(self, source_id='seagate'):
        return next(source for source in self.app.overview()['sources'] if source['id'] == source_id)

    def fetch(self, status=304, headers=None, source_id='seagate'):
        self.responses[source_id] = FeedResponse(status, rss() if status == 200 else b'', headers)
        return self.app._fetch(self.source(source_id))

    def due(self, source_id='seagate'):
        self.now = self.source(source_id)['not_before']

    def test_429_single_success_then_relapse_retains_and_increases_pressure(self):
        self.fetch(429)
        self.assertEqual(self.source()['not_before'], self.now + 1800)
        self.assertEqual(self.source()['cooldown_level'], 1)
        self.due()
        self.fetch(304)
        state = self.source()
        self.assertEqual((state['cooldown_level'], state['recovery_successes'], state['failures']), (1, 1, 0))
        self.assertEqual(state['next_poll'], self.now + 1800)
        self.assertTrue(self.view()['recovery_pending'])
        before = len(self.calls)
        self.now += 900
        self.assertFalse(self.fetch(429))
        self.assertEqual(len(self.calls), before)
        self.due()
        self.fetch(429)
        self.assertEqual((self.source()['cooldown_level'], self.source()['recovery_successes']), (2, 0))
        self.assertEqual(self.source()['next_poll'], self.now + 3600)

    def test_three_successes_and_full_recovery_wait_lower_only_one_level(self):
        self.fetch(429)
        self.due()
        self.fetch(429)
        for index, status in enumerate((200, 304, 304)):
            self.due()
            self.fetch(status)
            self.assertEqual(self.source()['cooldown_level'], 2)
        self.now = self.source()['rate_recovery_not_before']
        self.fetch(304)
        self.assertEqual(self.source()['cooldown_level'], 1)
        self.assertEqual(self.source()['recovery_successes'], 0)
        for _ in range(3):
            self.due()
            self.fetch(304)
        self.assertEqual(self.source()['cooldown_level'], 1)
        self.now = self.source()['rate_recovery_not_before']
        self.fetch(304)
        state = self.source()
        self.assertEqual((state['cooldown_level'], state['not_before']), (0, 0))
        self.assertEqual(state['next_poll'], self.now + 900)
        self.assertFalse(self.view()['recovery_pending'])
        self.assertTrue(self.fetch(304))  # Normal manual refresh is still allowed.

    def test_retry_after_seconds_above_six_hours_is_not_truncated(self):
        self.fetch(429, {'rEtRy-AfTeR':'28800'})
        self.assertEqual(self.source()['not_before'], self.now + 28800)
        check = self.app.overview()['recent_checks'][0]
        self.assertEqual(check['schedule_basis']['decision'], 'retry_after')
        self.assertEqual(check['schedule_basis']['retry_after_kind'], 'seconds')
        self.app.refresh()
        self.now += 21600
        self.app._cycle()
        self.assertEqual(sum(source == 'seagate' for source, _ in self.calls), 1)
        self.assertTrue(self.view()['refresh_queued'])

    def test_retry_after_http_date_is_persisted_as_a_deadline(self):
        deadline = self.now + 32400
        self.fetch(429, {'Retry-After':format_datetime(datetime.fromtimestamp(deadline, timezone.utc), usegmt=True)})
        self.assertEqual(self.source()['not_before'], deadline)
        self.app.stop()
        self.app = self.new_app()
        self.assertEqual(self.source()['not_before'], deadline)
        self.assertFalse(self.fetch(200))

    def test_missing_invalid_and_zero_headers_use_local_backoff(self):
        for headers, expected in ((None, 'missing'), ({'Retry-After':'nonsense'}, 'invalid'), ({'Retry-After':'0'}, 'valid')):
            with self.subTest(headers=headers):
                self.fetch(429, headers)
                state = self.source()
                self.assertEqual(state['not_before'], self.now + min(21600, 900 * 2 ** state['cooldown_level']))
                self.assertEqual(self.app.overview()['recent_checks'][0]['schedule_basis']['retry_after_status'], expected)
                self.due()

    def test_unrepresentable_deadline_holds_only_affected_source_across_rebuild(self):
        self.fetch(429, {'Retry-After':'9' * 5000})
        self.assertTrue(self.view()['manual_hold'])
        self.assertIsNone(self.view()['cooldown_until'])
        self.app.refresh()
        self.app._cycle()
        self.assertEqual({source for source, _ in self.calls}, {'seagate','fed','bls'})
        self.assertEqual(sum(source == 'seagate' for source, _ in self.calls), 1)
        self.assertFalse(self.view()['refresh_queued'])
        self.assertFalse(self.app.overview()['refresh_pending'])
        self.app.stop()
        self.app = self.new_app()
        self.now += 30 * 86400
        self.app.control(True)
        self.app.refresh()
        self.app._cycle()
        self.assertTrue(self.view()['manual_hold'])
        self.assertEqual(sum(source == 'seagate' for source, _ in self.calls), 1)
        self.assertEqual(self.app.overview()['counts']['sources_ok'], 2)

    def test_manual_refresh_is_coalesced_queued_and_survives_rebuild(self):
        self.fetch(429)
        deadline = self.source()['not_before']
        for _ in range(5):
            self.app.refresh()
        self.app._cycle()
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(self.view()['refresh_queued'])
        for _ in range(5):
            self.app.refresh()
            self.app._cycle()
        self.assertEqual(len(self.calls), 3)
        self.app.stop()
        self.app = self.new_app()
        self.assertEqual(self.source()['not_before'], deadline)
        self.assertTrue(self.view()['refresh_queued'])
        self.responses['seagate'] = FeedResponse(304)
        self.now = deadline
        self.app._cycle()
        self.assertEqual(len(self.calls), 4)
        self.assertFalse(self.app.overview()['refresh_pending'])
        self.assertFalse(self.view()['refresh_queued'])

    def test_pause_cancels_queue_but_does_not_erase_cooldown(self):
        self.fetch(429)
        deadline = self.source()['not_before']
        self.app.refresh()
        self.app.control(False)
        self.assertFalse(self.view()['refresh_queued'])
        self.assertEqual(self.source()['not_before'], deadline)
        self.app.control(True)
        self.app._cycle()
        self.assertEqual(sum(source == 'seagate' for source, _ in self.calls), 1)
        self.assertEqual(self.source()['cooldown_level'], 1)

    def test_403_is_distinct_and_conservative_and_does_not_claim_origin(self):
        self.fetch(403, source_id='bls')
        state, view = self.source('bls'), self.view('bls')
        self.assertEqual((state['access_denied_level'], state['cooldown_level']), (1, 0))
        self.assertEqual(state['not_before'], self.now + 1800)
        self.assertEqual(view['cooldown_reason'], 'access_denied')
        self.assertIn('原因待确认', view['error'])
        self.assertEqual(self.app.overview()['recent_checks'][0]['error_code'], 'access_denied')
        self.due('bls')
        self.fetch(304, source_id='bls')
        self.assertTrue(self.view('bls')['recovery_pending'])
        self.assertEqual(self.source('bls')['not_before'], self.now + 1800)

    def test_403_and_generic_errors_do_not_erase_429_pressure(self):
        self.fetch(429)
        self.due()
        self.fetch(403)
        self.assertEqual((self.source()['cooldown_level'], self.source()['access_denied_level']), (1, 1))
        self.due()
        self.fetch(503)
        self.assertEqual((self.source()['cooldown_level'], self.source()['access_denied_level']), (1, 1))
        self.assertGreaterEqual(self.source()['not_before'], self.now + 1800)

    def test_backoff_caps_local_delay_and_small_interval_scales(self):
        self.app.stop()
        self.app = self.new_app(poll_interval=60)
        self.fetch(429)
        self.assertEqual(self.source()['not_before'], self.now + 120)
        for _ in range(12):
            self.due()
            self.fetch(429)
        self.assertEqual(self.source()['not_before'], self.now + 21600)
        self.assertEqual(self.source()['cooldown_level'], 9)
        for _ in range(3):
            self.due()
            self.fetch(304)
        self.now = self.source()['rate_recovery_not_before']
        self.fetch(304)
        self.assertEqual(self.source()['cooldown_level'], 8)
        self.assertEqual(self.source()['not_before'], self.now + 15360)

    def test_disabled_queued_source_does_not_block_manual_refresh(self):
        with self.app._db() as db:
            db.execute("UPDATE sources SET enabled=0,refresh_queued=1 WHERE id='seagate'")
        self.assertFalse(self.app.overview()['refresh_pending'])
        self.assertFalse(self.view()['refresh_queued'])
        self.app.refresh()
        self.app._cycle()
        self.assertEqual({source for source, _ in self.calls}, {'fed','bls'})
        self.assertFalse(self.app.overview()['refresh_pending'])

    def test_http_error_headers_and_diagnostics_are_recorded_without_secrets(self):
        exc = urllib.error.HTTPError(SOURCES[2]['url'],429,'secret-exception',{'Retry-After':'25200'},None)
        exc.news_diagnostics = {'schema_version':'news_feed_diagnostics_v1', 'route_class':'local_loopback_proxy',
            'redirect_count':1,'final_host':'investors.seagate.com','conditional_etag':True,
            'elapsed_ms':8,'authorization':'secret-value','response_date':'secret-date'}
        self.responses['seagate'] = exc
        self.app._fetch(self.source())
        self.assertEqual(self.source()['not_before'], self.now + 25200)
        check = self.app.overview()['recent_checks'][0]
        self.assertEqual(check['diagnostics']['route_class'], 'local_loopback_proxy')
        self.assertEqual(check['trigger_kind'], 'manual')
        self.assertNotIn('secret', json.dumps(check))
        self.assertNotIn('authorization', json.dumps(check))

    def test_scheduled_trigger_and_new_failure_records_are_not_fabricated(self):
        self.app.control(True)
        self.responses['bls'] = FeedResponse(403)
        self.app._cycle()
        state = self.app.overview()
        self.assertEqual(state['checks_24h']['started'], 3)
        self.assertTrue(all(check['trigger_kind'] == 'scheduled' for check in state['recent_checks']))
        self.app.refresh()
        self.app._cycle()
        self.assertEqual(self.app.overview()['checks_24h']['failed'], 1)
        self.assertTrue(self.view('bls')['refresh_queued'])
        self.assertEqual(self.app.overview()['counts']['sources_ok'], 2)

    def test_inflight_manual_clicks_merge_without_followup_batch(self):
        entered, release = threading.Event(), threading.Event()
        def transport(url, *args):
            self.calls.append((url,self.now))
            if 'seagate' in url:
                entered.set()
                if not release.wait(2):
                    raise AssertionError('test_release_timeout')
            return FeedResponse(304)
        self.app.transport = transport
        self.app.refresh()
        thread = threading.Thread(target=self.app._cycle)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            for _ in range(4):
                self.app.refresh()
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.app._cycle()
        self.assertEqual(len(self.calls), 3)
        self.assertFalse(self.app.overview()['refresh_pending'])

    def test_clock_rollback_cannot_unlock_cooldown_or_count_success(self):
        self.fetch(429)
        deadline = self.source()['not_before']
        self.now -= 3600
        self.app.refresh()
        self.app._cycle()
        self.assertEqual(sum(source == 'seagate' for source, _ in self.calls), 1)
        self.assertEqual(self.source()['not_before'], deadline)
        self.assertEqual(self.source()['recovery_successes'], 0)

    def test_429_write_failure_restores_server_deadline_without_refetching_completed_source(self):
        self.responses['fed'] = FeedResponse(429, headers={'Retry-After':'28800'})
        original_finish = evidence.finish_poll
        failed_once = False
        def finish(*args, **kwargs):
            nonlocal failed_once
            if args[3] == 'failed' and not failed_once:
                failed_once = True
                raise sqlite3.OperationalError('synthetic failed-result transaction')
            return original_finish(*args, **kwargs)
        self.app.refresh()
        with patch.object(evidence, 'finish_poll', side_effect=finish):
            with self.assertRaises(sqlite3.OperationalError):
                self.app._cycle()
        self.assertEqual([source for source, _ in self.calls], ['bls','fed'])
        self.assertEqual(set(self.app._pending_cooldowns), {'fed'})
        self.assertEqual(set(self.app._pending_cooldowns['fed']),
            {'not_before','cooldown_level','access_denied_level','cooldown_reason','manual_hold','rate_recovery_not_before'})
        self.assertEqual(self.source('fed')['not_before'], 0)  # Failed write really rolled back.
        self.app._cycle()
        self.assertEqual([source for source, _ in self.calls], ['bls','fed','seagate'])
        self.assertEqual(self.source('fed')['not_before'], self.now + 28800)
        self.assertEqual(self.source('fed')['cooldown_level'], 1)
        state = self.app.overview()
        self.assertEqual((state['checks_24h']['successful'],state['checks_24h']['interrupted'],state['checks_24h']['failed']), (2,1,0))
        self.assertTrue(self.view('fed')['refresh_queued'])
        old = next(check for check in state['recent_checks'] if check['source_id']=='fed')
        self.assertEqual(old['outcome'],'interrupted_unknown')
        self.assertIsNone(old['http_status'])
        self.now += 21600
        self.app._cycle()
        self.assertEqual(len(self.calls), 3)
        self.due('fed')
        self.responses['fed'] = FeedResponse(304)
        self.app._cycle()
        self.assertEqual([source for source, _ in self.calls], ['bls','fed','seagate','fed'])
        self.assertEqual(self.app.overview()['checks_24h']['successful'], 3)
        self.assertFalse(self.app.overview()['refresh_pending'])

    def test_403_pending_deadline_blocks_transport_until_recovery_commit_succeeds(self):
        self.responses['bls'] = FeedResponse(403)
        with patch.object(evidence, 'finish_poll', side_effect=sqlite3.OperationalError('synthetic')):
            with self.assertRaises(sqlite3.OperationalError):
                self.app._fetch(self.source('bls'))
        pending = dict(self.app._pending_cooldowns['bls'])
        with patch.object(self.app, '_db', side_effect=sqlite3.OperationalError('still unavailable')):
            for _ in range(2):
                with self.assertRaises(sqlite3.OperationalError):
                    self.app._fetch({'id':'bls'})
        self.assertEqual(self.calls, [('bls',self.now)])
        self.assertEqual(self.app._pending_cooldowns['bls'], pending)
        self.assertFalse(self.app._fetch(self.source('bls')))
        self.assertEqual(self.source('bls')['not_before'], self.now + 1800)
        self.assertEqual(self.source('bls')['access_denied_level'], 1)
        self.assertEqual(self.source('bls')['recovery_successes'], 0)
        self.assertEqual(self.app._pending_cooldowns, {})
        self.assertEqual(self.app.overview()['checks_24h']['interrupted'], 1)

    def test_graceful_stop_flushes_uncommitted_manual_hold_before_rebuild(self):
        self.responses['seagate'] = FeedResponse(429, headers={'Retry-After':'9' * 5000})
        with patch.object(evidence, 'finish_poll', side_effect=sqlite3.OperationalError('synthetic')):
            with self.assertRaises(sqlite3.OperationalError):
                self.app._fetch(self.source())
        self.assertTrue(self.app._pending_cooldowns['seagate']['manual_hold'])
        self.assertTrue(self.app.stop())
        self.app = self.new_app()
        self.assertTrue(self.view()['manual_hold'])
        self.app.refresh()
        self.app._cycle()
        self.assertEqual(sum(source == 'seagate' for source, _ in self.calls), 1)
        self.assertEqual(self.app.overview()['checks_24h']['interrupted'], 1)
        self.assertEqual(self.app.overview()['checks_24h']['failed'], 0)
        self.assertFalse(self.app.overview()['refresh_pending'])

    def test_uncommitted_successful_probe_keeps_pressure_and_waits_without_counting_success(self):
        self.fetch(429)
        for _ in range(2):
            self.due()
            self.fetch(304)
        self.assertEqual(self.source()['recovery_successes'], 2)
        self.due()
        self.app.refresh()
        with patch.object(evidence, 'finish_poll', side_effect=sqlite3.OperationalError('synthetic recovery write')):
            with self.assertRaises(sqlite3.OperationalError):
                self.fetch(304)
        self.assertEqual(self.source()['cooldown_level'], 1)
        self.assertEqual(self.source()['recovery_successes'], 2)
        self.assertEqual(self.app._pending_cooldowns['seagate']['cooldown_level'], 1)
        self.assertEqual(self.app._pending_cooldowns['seagate']['not_before'], self.now + 1800)
        attempts = len(self.calls)
        self.assertFalse(self.fetch(304))
        self.assertEqual(len(self.calls), attempts)
        self.assertEqual(self.source()['recovery_successes'], 0)
        self.assertEqual(self.source()['cooldown_level'], 1)
        self.assertTrue(self.view()['refresh_queued'])
        state = self.app.overview()
        self.assertEqual((state['checks_24h']['successful'],state['checks_24h']['interrupted']), (2,1))
        self.due()
        self.fetch(304)
        self.assertEqual(self.source()['recovery_successes'], 1)
        self.assertEqual(self.source()['cooldown_level'], 1)

    def test_migration_preserves_old_items_cache_headers_history_and_deadline(self):
        self.app.stop()
        legacy = Path(self.temp.name) / 'legacy.db'
        with contextlib.closing(sqlite3.connect(legacy)) as db, db:
            db.execute(f'PRAGMA application_id={APPLICATION_ID}')
            db.executescript("""
                CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT INTO settings VALUES('enabled','false');
                CREATE TABLE sources(id TEXT PRIMARY KEY,name TEXT NOT NULL,url TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'never',last_success REAL,last_attempt REAL,next_poll REAL NOT NULL DEFAULT 0,
                    error TEXT,failures INTEGER NOT NULL DEFAULT 0,etag TEXT,last_modified TEXT);
                CREATE TABLE items(id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources(id),identity TEXT NOT NULL,title TEXT NOT NULL,
                    url TEXT NOT NULL,excerpt TEXT NOT NULL,published REAL,discovered REAL NOT NULL,updated REAL NOT NULL,
                    read INTEGER NOT NULL DEFAULT 0,analysis TEXT NOT NULL,UNIQUE(source_id,identity));
                CREATE TABLE poll_runs(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,source_id TEXT NOT NULL,started REAL NOT NULL,
                    finished REAL,outcome TEXT NOT NULL,http_status INTEGER,parsed_items INTEGER NOT NULL DEFAULT 0,
                    new_items INTEGER NOT NULL DEFAULT 0,updated_items INTEGER NOT NULL DEFAULT 0,error_code TEXT);
                CREATE TABLE cached_analysis(id TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT INTO cached_analysis VALUES('old','immutable paid output');
            """)
            source = next(item for item in SOURCES if item['id']=='seagate')
            db.execute("INSERT INTO sources(id,name,url,status,next_poll,failures,error,etag,last_modified) VALUES(?,?,?,'error',?,2,'old error','old-etag','old-last-modified')",
                (source['id'],source['name'],source['url'],self.now+3600))
            db.execute("INSERT INTO items VALUES('old','seagate','old-guid','old title','https://investors.seagate.com/old','old excerpt',?,?,?,?,?)",
                (self.now-60,self.now-50,self.now-40,1,'{}'))
            db.execute("INSERT INTO poll_runs(id,session_id,source_id,started,finished,outcome,http_status,error_code) VALUES('old-check','old-session','seagate',?,?,'failed',429,'http_error')",(self.now-10,self.now-9))
            old_item = db.execute('SELECT * FROM items').fetchone()
            old_check = db.execute('SELECT * FROM poll_runs').fetchone()
        app = NewsPlatform(legacy,transport=self.transport,clock=lambda:self.now)
        self.apps.append(app)
        with app._db() as db:
            self.assertEqual(tuple(db.execute('SELECT * FROM items').fetchone()),old_item)
            current = db.execute('SELECT * FROM poll_runs').fetchone()
            self.assertEqual(tuple(current)[:len(old_check)],old_check)
            self.assertIsNone(current['trigger_kind'])
            self.assertIsNone(current['diagnostics'])
            self.assertIsNone(current['schedule_basis'])
            self.assertEqual(db.execute('SELECT value FROM cached_analysis').fetchone()[0],'immutable paid output')
            source = db.execute("SELECT * FROM sources WHERE id='seagate'").fetchone()
            self.assertEqual((source['etag'],source['last_modified']),('old-etag','old-last-modified'))
            self.assertEqual(source['not_before'],self.now+3600)
            self.assertEqual(source['cooldown_level'],0)  # No invented historical pressure.
        self.assertFalse(app._fetch(source))
        self.assertEqual(self.calls,[])


if __name__ == '__main__':
    unittest.main()
