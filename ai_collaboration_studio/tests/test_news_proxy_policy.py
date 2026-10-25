"""Synthetic proxy views and fake transports only; no live configuration reads."""
import io
import json
import socket
import unittest
import urllib.request
from unittest.mock import Mock, patch

from backend import news_feed_transport as transport
from backend.news_runtime_evidence import _safe_json
from scripts import news_portable_runtime as portable
from scripts import run_news_workspace as entry
from tests import test_news_cooldown as cooldown_fixture

URL = transport.SOURCES[0]["url"]
PROXY = "http://127.0.0.1:18234"
PRIVATE = "synthetic-private-value"


class Response(io.BytesIO):
    status = 200
    headers = {}

    def geturl(self):
        return URL


class FeedProxyPolicyTests(unittest.TestCase):
    def setUp(self):
        for guard in (
            patch.object(socket.socket, "connect", side_effect=AssertionError("offline_only")),
            patch.object(socket.socket, "connect_ex", side_effect=AssertionError("offline_only")),
            patch.object(socket, "getaddrinfo", side_effect=AssertionError("offline_only")),
            patch.object(urllib.request, "getproxies", side_effect=AssertionError("no_real_proxy_lookup")),
        ):
            guard.start()
            self.addCleanup(guard.stop)

    def fetch(self, view):
        opener = Mock()
        opener.open.return_value = Response(b"<rss/>")
        with patch.object(urllib.request, "getproxies", return_value=view), \
                patch.object(urllib.request, "build_opener", return_value=opener) as build:
            result = transport.fetch_feed(URL, {}, 1, 1024)
        handler = next(value for value in build.call_args.args if isinstance(value, transport._ObservedProxyHandler))
        self.assertEqual(opener.open.call_count, 1)
        return result.diagnostics, handler.proxies

    def test_invalid_https_cannot_fall_back_to_valid_all_or_direct(self):
        invalid = ("", None, "broken", "socks5://127.0.0.1:1080", "http://remote.invalid:80",
                   "http://user:secret@127.0.0.1:80", "http://@127.0.0.1:80",
                   "http://127.0.0.1", "http://127.0.0.1:0", "http://127.0.0.1:65536",
                   "http://127.0.0.1:80/path", "http://127.0.0.1:80?secret=yes",
                   "http://127.0.0.1:80#secret", " http://127.0.0.1:80", "http://127.0.0.1:80\n")
        for value in invalid:
            with self.subTest(value_type=type(value).__name__), \
                    patch.object(urllib.request, "getproxies", return_value={"https": value, "all": PROXY}), \
                    patch.object(urllib.request, "build_opener") as build:
                with self.assertRaises(transport.FeedValidationError) as caught:
                    transport.fetch_feed(URL, {}, 1, 1024)
                self.assertEqual(caught.exception.code, "proxy_configuration_error")
                self.assertEqual(caught.exception.news_diagnostics["proxy_configuration_intent"], "invalid_configuration")
                self.assertEqual(caught.exception.news_diagnostics["route_class"], "unknown")
                self.assertEqual(caught.exception.news_diagnostics["phase"], "proxy_configuration")
                build.assert_not_called()

    def test_all_fallback_is_safe_only_and_https_presence_wins(self):
        for proxy in (PROXY, "https://localhost:18234", "http://[::1]:18234"):
            diagnostic, selected = self.fetch({"all": proxy})
            self.assertEqual(selected, {"https": proxy})
            self.assertEqual(diagnostic["proxy_configuration_intent"], "all_proxy_fallback")
        diagnostic, selected = self.fetch({"https": PROXY, "all": "socks5://127.0.0.1:1080"})
        self.assertEqual(selected, {"https": PROXY})
        self.assertEqual(diagnostic["proxy_configuration_intent"], "https_proxy")
        for proxy in ("socks5://127.0.0.1:1080", "http://remote.invalid:80", "http://a:b@127.0.0.1:80", "broken"):
            with patch.object(urllib.request, "getproxies", return_value={"all": proxy}), \
                    patch.object(urllib.request, "build_opener") as build:
                with self.assertRaises(transport.FeedValidationError):
                    transport.fetch_feed(URL, {}, 1, 1024)
                build.assert_not_called()

    def test_http_only_is_not_an_https_proxy_and_absence_is_explicit(self):
        for view, expected in (({"http": PROXY}, "https_not_configured"), ({}, "not_configured")):
            diagnostic, selected = self.fetch(view)
            self.assertEqual(selected, {})
            self.assertEqual(diagnostic["proxy_configuration_intent"], expected)
            self.assertFalse(diagnostic["bypass_configured"])
            # A fake opener does not prove which network route was used.
            self.assertEqual(diagnostic["route_class"], "unknown")

    def test_lookup_failure_or_malformed_view_fails_before_network(self):
        with patch.object(urllib.request, "getproxies", side_effect=OSError(PRIVATE)), \
                patch.object(urllib.request, "build_opener") as build:
            with self.assertRaises(transport.FeedValidationError) as caught:
                transport.fetch_feed(URL, {}, 1, 1024)
            self.assertEqual(caught.exception.news_diagnostics["proxy_configuration_intent"], "configuration_unavailable")
            self.assertNotIn(PRIVATE, str(caught.exception))
            build.assert_not_called()
        for view in (None, [], {"no": "bad\nrule"}):
            with self.assertRaises(transport.FeedValidationError):
                transport.feed_proxy_configuration(view)

    def test_bypass_intent_is_redacted_and_actual_handler_still_decides(self):
        diagnostic, selected = self.fetch({"https": PROXY, "no": PRIVATE})
        self.assertTrue(diagnostic["bypass_configured"])
        self.assertNotIn(PRIVATE, json.dumps(diagnostic))
        self.assertNotIn("18234", json.dumps(diagnostic))
        for bypass in (True, False):
            observed = transport._Diagnostics({})
            request = urllib.request.Request(URL)
            handler = transport._ObservedProxyHandler(selected)
            with patch.object(urllib.request, "proxy_bypass", return_value=bypass) as decision:
                handler.proxy_open(request, PROXY, "https")
            decision.assert_called_once()
            with patch.object(urllib.request.HTTPSHandler, "https_open", return_value=object()):
                transport._ObservedHTTPSHandler(observed).https_open(request)
            self.assertEqual(observed.snapshot()["route_class"], "direct" if bypass else "local_loopback_proxy")
            self.assertIs(observed.snapshot()["effective_bypass"], bypass)

    def test_second_allowlist_keeps_only_safe_new_fields_and_legacy_is_unknown(self):
        source = {"proxy_configuration_intent": "invalid_configuration", "bypass_configured": True,
                  "error_kind": "proxy_configuration_error", "phase": "proxy_configuration",
                  "proxy_url": PRIVATE, "no_proxy_rules": PRIVATE}
        safe = json.loads(_safe_json(source, "transport"))
        self.assertEqual(safe["proxy_configuration_intent"], "invalid_configuration")
        self.assertTrue(safe["bypass_configured"])
        self.assertEqual(safe["error_kind"], "proxy_configuration_error")
        self.assertNotIn(PRIVATE, json.dumps(safe))
        invalid = json.loads(_safe_json({"proxy_configuration_intent": PRIVATE, "bypass_configured": PRIVATE}, "transport"))
        self.assertNotIn("proxy_configuration_intent", invalid)
        self.assertNotIn("bypass_configured", invalid)
        legacy = json.loads(_safe_json({"route_class": "direct"}, "transport"))
        self.assertIsNone(legacy["proxy_configuration_intent"])
        self.assertIsNone(legacy["bypass_configured"])


class LauncherProxyTests(unittest.TestCase):
    def test_online_keeps_original_safe_proxy_and_bypass_but_never_keys(self):
        original = {"https_proxy": PROXY, "HTTP_PROXY": "https://localhost:18234", "NO_PROXY": "localhost,.synthetic.invalid",
                    "ARK_API_KEY": PRIVATE, "OPENAI_API_KEY": PRIVATE, "AI_STUDIO_DATABASE_PATH": PRIVATE}
        before = dict(original)
        clean = portable.safe_environment(original, feed_proxy_view={"https": PROXY, "http": original["HTTP_PROXY"], "no": original["NO_PROXY"]})
        for name in ("https_proxy", "HTTP_PROXY", "NO_PROXY"):
            self.assertEqual(clean[name], original[name])
        for name in ("ARK_API_KEY", "OPENAI_API_KEY", "AI_STUDIO_DATABASE_PATH"):
            self.assertNotIn(name, clean)
        self.assertEqual(original, before)
        self.assertNotIn("HTTPS_PROXY", clean)

    def test_registry_view_is_validated_without_creating_environment_proxy(self):
        clean = portable.safe_environment({"KEEP": "yes"}, feed_proxy_view={"https": PROXY})
        self.assertFalse(any(name.upper().endswith("_PROXY") for name in clean))
        self.assertEqual(clean["KEEP"], "yes")
        with self.assertRaisesRegex(portable.PortableError, "proxy_configuration_error"):
            portable.safe_environment({}, feed_proxy_view={"https": "socks5://127.0.0.1:1080"})

    def test_online_cannot_drop_unsafe_unused_env_configuration(self):
        for name in ("HTTP_PROXY", "ALL_PROXY", "https_proxy"):
            with self.subTest(name=name), self.assertRaisesRegex(portable.PortableError, "proxy_configuration_error"):
                portable.safe_environment({name: "http://user:private@127.0.0.1:18234"}, feed_proxy_view={"https": PROXY})
        with self.assertRaises(portable.PortableError):
            portable.safe_environment({}, feed_proxy_view=None)
        clean = portable.safe_environment({"HTTPS_PROXY": "", "NO_PROXY": ""}, feed_proxy_view={})
        self.assertEqual(clean["HTTPS_PROXY"], "")
        self.assertEqual(clean["NO_PROXY"], "")

    def test_offline_bootstrap_default_strips_proxies_without_effective_lookup(self):
        original = {name: PRIVATE for name in ("HTTP_PROXY", "https_proxy", "ALL_PROXY", "NO_PROXY", "ARK_API_KEY")}
        with patch.object(urllib.request, "getproxies", side_effect=AssertionError("offline_no_lookup")):
            clean = portable.safe_environment(original)
        self.assertFalse(any(name in clean for name in original))

    def test_entry_looks_up_effective_view_before_cleaning_only_online(self):
        for offline in (False, True):
            view = {"https": PROXY}
            with patch.object(entry.portable, "data_root", return_value=entry.PROJECT / "synthetic-never-created"), \
                    patch.object(entry.portable, "require_empty_data"), patch.object(entry.portable, "reserve_port", return_value=Mock()), \
                    patch.object(entry.urllib.request, "getproxies", return_value=view) as lookup, \
                    patch.object(entry.portable, "safe_environment", side_effect=portable.PortableError("synthetic_stop_before_environment_mutation")) as clean:
                self.assertEqual(entry.main(["--initialize-new"] + (["--offline"] if offline else [])), 1)
            if offline:
                lookup.assert_not_called()
                clean.assert_called_once_with()
            else:
                lookup.assert_called_once_with()
                clean.assert_called_once_with(feed_proxy_view=view)


class ProxyEvidenceTests(unittest.TestCase):
    for _helper in ('setUp', 'tearDown', 'transport', 'new_app', 'source', 'view', 'fetch', 'due'):
        locals()[_helper] = getattr(cooldown_fixture.NewsCooldownTests, _helper)

    def test_proxy_configuration_failure_keeps_data_and_rate_pressure(self):
        self.fetch(200, {'ETag': 'synthetic-retained-cache'})
        self.fetch(429)
        before = self.source()
        with self.app._db() as db:
            items = [tuple(row) for row in db.execute('SELECT * FROM items')]
        self.due()
        failure = transport.FeedValidationError('proxy_configuration_error')
        failure.news_diagnostics = {'error_kind': 'proxy_configuration_error', 'phase': 'proxy_configuration',
            'proxy_configuration_intent': 'invalid_configuration', 'bypass_configured': False, 'raw_proxy': PRIVATE}
        self.responses['seagate'] = failure
        self.app._fetch(self.source())
        check = self.app.overview()['recent_checks'][0]
        self.assertEqual(check['error_code'], 'proxy_configuration_error')
        self.assertIsNone(check['http_status'])
        self.assertEqual(check['diagnostics']['proxy_configuration_intent'], 'invalid_configuration')
        self.assertNotIn(PRIVATE, json.dumps(check))
        self.assertEqual(self.source()['cooldown_level'], before['cooldown_level'])
        self.assertEqual(self.source()['rate_recovery_not_before'], before['rate_recovery_not_before'])
        self.assertEqual(self.source()['etag'], before['etag'])
        with self.app._db() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM items')], items)

    def test_access_and_rate_recovery_have_separate_safe_counters(self):
        self.fetch(403, source_id='bls')
        self.due('bls')
        self.fetch(304, source_id='bls')
        basis = self.app.overview()['recent_checks'][0]['schedule_basis']
        self.assertEqual((basis['recovery_successes'], basis['rate_recovery_successes'], basis['access_recovery_successes']), (0, 0, 1))
        self.fetch(429)
        self.due()
        self.fetch(304)
        basis = self.app.overview()['recent_checks'][0]['schedule_basis']
        self.assertEqual((basis['recovery_successes'], basis['rate_recovery_successes'], basis['access_recovery_successes']), (1, 1, 0))


if __name__ == '__main__':
    unittest.main()
