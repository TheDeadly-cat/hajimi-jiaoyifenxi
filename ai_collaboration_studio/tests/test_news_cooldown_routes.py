"""Public route to real isolated runtime contracts; transport and clock are fake."""
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from backend.news_platform import FeedResponse, NewsPlatform, SOURCES
from backend.news_platform_routes import news_response
from tests.test_news_platform import NOW, rss


class NewsCooldownRouteIntegrationTests(unittest.TestCase):
    def test_refresh_route_coalesces_cooling_sources_and_keeps_reading_available(self):
        now = [NOW]
        calls = []
        responses = {
            'fed': FeedResponse(200, rss()),
            'bls': 403,
            'seagate': 429,
        }

        def transport(url, *args):
            sid = next(s['id'] for s in SOURCES if s['url'] == url)
            calls.append(sid)
            response = responses[sid]
            if isinstance(response, int):
                raise urllib.error.HTTPError(url, response, 'synthetic', {}, None)
            return response

        with tempfile.TemporaryDirectory() as directory:
            app = NewsPlatform(Path(directory) / 'route.db', transport=transport, clock=lambda: now[0])
            try:
                def request(path, method='GET', payload=None):
                    body, code = news_response(app, path, method=method, payload=payload)
                    self.assertEqual(code, 200)
                    # Use precisely the JSON shape delivered to the frontend.
                    return json.loads(json.dumps(body))

                request('/api/news/refresh', 'POST', {})
                app._cycle()
                view = request('/api/news/overview')['overview']
                sources = {s['id']: s for s in view['sources']}
                self.assertEqual(sources['bls']['cooldown_reason'], 'access_denied')
                self.assertEqual(sources['seagate']['cooldown_reason'], 'rate_limited')
                self.assertTrue(sources['seagate']['cooldown_active'])
                self.assertFalse(view['refresh_pending'])

                queued = request('/api/news/refresh', 'POST', {})['overview']
                self.assertTrue(queued['refresh_pending'])
                app._cycle()
                before = list(calls)
                for _ in range(3):
                    request('/api/news/refresh', 'POST', {})
                    app._cycle()
                self.assertEqual(calls, before)
                self.assertEqual(calls.count('fed'), 2)
                self.assertEqual(calls.count('bls'), 1)
                self.assertEqual(calls.count('seagate'), 1)

                items = request('/api/news/items')['items']
                self.assertEqual(len(items), 1)
                saved = request('/api/news/items/' + items[0]['id'] + '/read', 'POST', {'read': True})['item']
                self.assertTrue(saved['read'])

                now[0] += 1800
                responses.update(bls=FeedResponse(304), seagate=FeedResponse(304))
                app._cycle()
                view = request('/api/news/overview')['overview']
                sources = {s['id']: s for s in view['sources']}
                self.assertFalse(view['refresh_pending'])
                for sid in ('bls', 'seagate'):
                    self.assertEqual(calls.count(sid), 2)
                    self.assertTrue(sources[sid]['recovery_pending'])
                    self.assertTrue(sources[sid]['cooldown_active'])
                self.assertTrue(request('/api/news/items/' + saved['id'])['item']['read'])
            finally:
                self.assertTrue(app.stop())
