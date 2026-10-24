"""Focused offline replay of recovery pressure and content-error persistence."""
import json
import unittest
from backend.news_platform import FeedResponse, FeedValidationError, RATE_RECOVERY_SECONDS
from tests import test_news_cooldown as cooldown_fixture


class SourceRecoveryTests(unittest.TestCase):
    # Reuse only the isolated fixture, not its historical test methods.
    for _helper in ('setUp', 'tearDown', 'transport', 'new_app', 'source', 'view', 'fetch', 'due'):
        locals()[_helper] = getattr(cooldown_fixture.NewsCooldownTests, _helper)

    def test_three_successes_cannot_repeat_the_observed_fast_recovery(self):
        self.fetch(429)
        self.due()
        self.fetch(429)
        deadline = self.source()['rate_recovery_not_before']
        for _ in range(6):
            self.due()
            self.fetch(304)
        self.assertEqual(self.source()['cooldown_level'], 2)
        self.assertEqual(self.source()['recovery_successes'], 3)
        self.assertEqual(self.source()['next_poll'], self.now + 3600)
        self.app.stop()
        self.app = self.new_app()
        self.assertEqual(self.source()['rate_recovery_not_before'], deadline)
        self.now = deadline
        self.fetch(304)
        self.assertEqual(self.source()['cooldown_level'], 1)
        self.assertEqual(self.source()['rate_recovery_not_before'], self.now + RATE_RECOVERY_SECONDS)
        for _ in range(3):
            self.due()
            self.fetch(304)
        self.assertEqual(self.source()['cooldown_level'], 1)
        self.now = self.source()['rate_recovery_not_before']
        self.fetch(304)
        self.assertEqual(self.source()['cooldown_level'], 0)
        self.assertEqual(self.source()['rate_recovery_not_before'], 0)

    def test_relapse_extends_recovery_wait_and_manual_cannot_bypass_retry_after(self):
        self.fetch(429)
        old = self.source()['rate_recovery_not_before']
        self.due()
        self.fetch(429, {'Retry-After':'172800'})
        self.assertGreater(self.source()['rate_recovery_not_before'], old)
        deadline = self.source()['not_before']
        calls = len(self.calls)
        self.now += RATE_RECOVERY_SECONDS
        self.app.refresh()
        self.assertFalse(self.fetch(304))
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(self.source()['not_before'], deadline)

    def test_legacy_pressure_migration_preserves_items_read_cache_history_and_deadline(self):
        self.fetch(200, {'ETag':'retained-tag'})
        with self.app._db() as db:
            db.execute('UPDATE items SET read=1')
        self.fetch(429, {'Retry-After':'90000'})
        with self.app._db() as db:
            before_items = [tuple(row) for row in db.execute('SELECT * FROM items')]
            before_history = [tuple(row) for row in db.execute('SELECT * FROM poll_runs')]
            deadline = self.source()['not_before']
            db.execute('ALTER TABLE sources DROP COLUMN rate_recovery_not_before')
        self.app.stop()
        self.app = self.new_app()
        with self.app._db() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM items')], before_items)
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM poll_runs')], before_history)
        self.assertEqual(self.source()['not_before'], deadline)
        self.assertEqual(self.source()['etag'], 'retained-tag')
        self.assertGreaterEqual(self.source()['rate_recovery_not_before'], self.now + RATE_RECOVERY_SECONDS)

    def test_invalid_200_content_is_not_misreported_as_http_200_failure(self):
        self.fetch(200)
        with self.app._db() as db:
            before = [tuple(row) for row in db.execute('SELECT * FROM items')]
        for body, code in [(b'<html/>','invalid_feed'),
                           (b'<!DOCTYPE rss><rss/>','unsafe_feed'),
                           (b'<rss>','invalid_feed')]:
            self.responses['seagate'] = FeedResponse(200, body)
            self.assertTrue(self.app._fetch(self.source()))
            check = self.app.overview()['recent_checks'][0]
            self.assertEqual((check['http_status'],check['error_code']), (200,code))
            self.assertEqual(check['diagnostics']['phase'], 'parse')
            self.assertNotIn('HTTP 200', self.view()['error'])
            with self.app._db() as db:
                self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM items')], before)
            self.due()

    def test_body_timeout_retains_http_status_and_safe_category(self):
        failure = TimeoutError('must-not-be-retained')
        failure.news_diagnostics = {'response_status':200,'error_kind':'timeout','phase':'response_body'}
        self.responses['seagate'] = failure
        self.app._fetch(self.source())
        check = self.app.overview()['recent_checks'][0]
        self.assertEqual((check['http_status'],check['error_code']), (200,'timeout'))
        self.assertEqual(check['diagnostics']['phase'], 'response_body')
        self.assertNotIn('must-not-be-retained', json.dumps(self.app.overview()))

    def test_rejected_redirect_preserves_transport_phase_and_status(self):
        failure = FeedValidationError('redirect_blocked')
        failure.news_diagnostics = {'response_status':302,'error_kind':'redirect_blocked','phase':'redirect'}
        self.responses['seagate'] = failure
        self.app._fetch(self.source())
        check = self.app.overview()['recent_checks'][0]
        self.assertEqual((check['http_status'],check['error_code']), (302,'redirect_blocked'))
        self.assertEqual(check['diagnostics']['phase'], 'redirect')

