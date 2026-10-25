"""Offline transport-to-scheduler regression; every response and clock is fake."""
import io
import json
import sqlite3
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest.mock import patch

from backend import news_feed_transport as transport
from backend import news_runtime_evidence as evidence
from tests import test_news_cooldown as fixture


class Body(io.BytesIO):
    status = 200

    def __init__(self, body=b'<rss/>', *, headers=None, failure=None):
        super().__init__(body)
        self.headers = headers or {}
        self.failure = failure

    def geturl(self):
        return transport.SOURCES[2]['url']

    def read1(self, size):
        if self.failure:
            raise self.failure
        return super().read1(size)


class RetryAfterFailureTests(unittest.TestCase):
    for _helper in ('setUp', 'tearDown', 'transport', 'new_app', 'source', 'view', 'fetch', 'due'):
        locals()[_helper] = getattr(fixture.NewsCooldownTests, _helper)

    def fetch_response(self, response, *, received_at=None, max_bytes=1024):
        attempts = []
        class Opener:
            def open(_self, request, *, timeout):
                attempts.append(request)
                if isinstance(response, BaseException):
                    raise response
                return response
        self.app.transport = lambda url, headers, timeout, size: transport.fetch_feed(url, headers, timeout, max_bytes)
        with patch.object(transport.urllib.request, 'getproxies', return_value={}), \
             patch.object(transport.urllib.request, 'build_opener', return_value=Opener()), \
             patch.object(transport.time, 'time', return_value=self.now if received_at is None else received_at):
            self.assertTrue(self.app._fetch(self.source()))
        self.assertEqual(len(attempts), 1)
        return self.app.overview()['recent_checks'][0]

    def test_body_timeout_keeps_received_deadline_and_existing_records(self):
        self.fetch(200, {'ETag': 'preserved-cache'})
        with self.app._db() as db:
            db.execute('UPDATE items SET read=1')
            old_items = [tuple(row) for row in db.execute('SELECT * FROM items')]
            old_runs = [tuple(row) for row in db.execute('SELECT * FROM poll_runs')]
        body = Body(headers={'Retry-After': '86400', 'Set-Cookie': 'private-cookie'}, failure=TimeoutError('private-error'))
        received = self.now - 5
        check = self.fetch_response(body, received_at=received)
        self.assertEqual(self.source()['not_before'], received + 86400)
        self.assertEqual((check['http_status'], check['error_code']), (200, 'timeout'))
        self.assertEqual(check['schedule_basis']['decision'], 'retry_after')
        self.assertEqual(check['diagnostics']['retry_after']['retry_at'], received + 86400)
        self.assertTrue(body.closed)
        self.assertNotIn('private-', json.dumps(check))
        with self.app._db() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM items')], old_items)
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM poll_runs ORDER BY rowid')][:-1], old_runs)
        self.assertEqual(self.source()['etag'], 'preserved-cache')
        self.app.stop()
        self.app = self.new_app()
        self.assertEqual(self.source()['not_before'], received + 86400)
        self.assertFalse(self.app._fetch(self.source()))

    def test_header_size_rejection_keeps_server_deadline(self):
        check = self.fetch_response(Body(headers={'Retry-After': '86400', 'Content-Length': '2048'}))
        self.assertEqual(self.source()['not_before'], self.now + 86400)
        self.assertEqual((check['error_code'], check['diagnostics']['phase']), ('feed_too_large', 'response_headers'))

    def test_stream_size_rejection_keeps_server_deadline(self):
        check = self.fetch_response(Body(b'x' * 1025, headers={'Retry-After': '86400'}))
        self.assertEqual(self.source()['not_before'], self.now + 86400)
        self.assertEqual((check['error_code'], check['diagnostics']['phase']), ('feed_too_large', 'response_body'))

    def test_invalid_xml_still_keeps_header_deadline(self):
        check = self.fetch_response(Body(b'<rss>', headers={'Retry-After': '86400'}))
        self.assertEqual(self.source()['not_before'], self.now + 86400)
        self.assertEqual(check['error_code'], 'invalid_feed')

    def test_429_still_keeps_header_deadline_and_recovery_pressure(self):
        failure = urllib.error.HTTPError(transport.SOURCES[2]['url'], 429, 'private-error', {'Retry-After': '86400'}, None)
        check = self.fetch_response(failure)
        self.assertEqual(self.source()['not_before'], self.now + 86400)
        self.assertEqual(self.source()['cooldown_level'], 1)
        self.assertEqual(check['error_code'], 'rate_limited')

    def test_out_of_range_wait_survives_reopen_and_blocks_manual_request(self):
        check = self.fetch_response(Body(headers={'Retry-After': '9' * 100}, failure=TimeoutError()))
        self.assertEqual(check['schedule_basis']['decision'], 'manual_hold')
        self.assertTrue(self.source()['manual_hold'])
        self.app.stop()
        self.app = self.new_app()
        self.now += 30 * 86400
        self.app.refresh()
        self.assertFalse(self.app._fetch(self.source()))
        self.assertTrue(self.view()['manual_hold'])

    def test_short_server_wait_does_not_reduce_existing_pressure(self):
        self.fetch(429)
        self.due()
        self.fetch(429)
        self.due()
        check = self.fetch_response(Body(headers={'Retry-After': '60'}, failure=TimeoutError()))
        self.assertEqual(self.source()['not_before'], self.now + 3600)
        self.assertEqual(self.source()['cooldown_level'], 2)
        expected = datetime.fromtimestamp(self.now + 60, timezone.utc).isoformat().replace('+00:00', 'Z')
        self.assertEqual(check['schedule_basis']['server_not_before'], expected)

    def test_failed_transaction_retains_deadline_without_another_transport(self):
        original = evidence.finish_poll
        def fail(*args, **kwargs):
            if args[3] == 'failed':
                raise sqlite3.OperationalError('synthetic failed receipt')
            return original(*args, **kwargs)
        with patch.object(evidence, 'finish_poll', side_effect=fail):
            with self.assertRaises(sqlite3.OperationalError):
                self.fetch_response(Body(headers={'Retry-After': '86400'}, failure=TimeoutError()))
        self.assertEqual(self.app._pending_cooldowns['seagate']['not_before'], self.now + 86400)
        self.app.transport = lambda *args: self.fail('retry attempted before server deadline')
        self.assertFalse(self.app._fetch(self.source()))
        self.assertEqual(self.source()['not_before'], self.now + 86400)

    def test_available_header_and_diagnostic_constraints_never_shorten_each_other(self):
        for header_wait, observed_wait in ((90000, 86400), (3600, 86400)):
            observed = transport.parse_retry_after({'Retry-After': str(observed_wait)}, self.now)
            failure = urllib.error.HTTPError(transport.SOURCES[2]['url'], 429, '', {'Retry-After': str(header_wait)}, None)
            failure.news_diagnostics = {'response_status': 429, 'retry_after': observed}
            self.responses['seagate'] = failure
            self.assertTrue(self.app._fetch(self.source()))
            self.assertEqual(self.source()['not_before'], self.now + max(header_wait, observed_wait))
            self.due()
        failure = urllib.error.HTTPError(transport.SOURCES[2]['url'], 429, '', {'Retry-After': '60'}, None)
        failure.news_diagnostics = {'response_status': 429, 'retry_after': transport.parse_retry_after({'Retry-After': '9' * 100}, self.now)}
        self.responses['seagate'] = failure
        self.assertTrue(self.app._fetch(self.source()))
        self.assertTrue(self.source()['manual_hold'])

    def test_invalid_diagnostic_deadlines_do_not_become_scheduling_constraints(self):
        for key in ('retry_at', 'delay_seconds'):
            observed = transport.parse_retry_after({'Retry-After': '86400'}, self.now)
            observed[key] = 10 ** 1000
            self.assertIsNone(transport.failure_retry_after(None, {'retry_after': observed}, self.now)['retry_at'])
        for value in (True, float('nan'), float('inf'), '86400', 253402300800):
            failure = TimeoutError()
            failure.news_diagnostics = {'response_status': 200, 'retry_after': {
                'status': 'valid', 'kind': 'seconds', 'retry_at': value,
                'delay_seconds': 86400, 'manual_hold': False}}
            self.responses['seagate'] = failure
            self.assertTrue(self.app._fetch(self.source()))
            check = self.app.overview()['recent_checks'][0]
            self.assertIsNone(check['schedule_basis']['server_not_before'])
            self.assertFalse(self.source()['manual_hold'])
            self.assertLessEqual(self.source()['not_before'], self.now + 21600)
            self.due()


if __name__ == '__main__':
    unittest.main()
