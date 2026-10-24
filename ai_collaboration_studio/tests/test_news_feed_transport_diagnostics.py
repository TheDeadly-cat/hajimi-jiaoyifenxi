"""Offline-only transport observations and conservative Retry-After parsing."""
from __future__ import annotations

import io
import json
import socket
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import Message
from unittest.mock import patch

from backend.news_feed_transport import (
    SOURCES, FeedResponse, _Diagnostics, _ObservedHTTPSHandler, _ObservedProxyHandler,
    _SameHostRedirect, _safe_url, fetch_feed, parse_retry_after,
)


URL = SOURCES[0]["url"]
NOW = datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc).timestamp()
DATE = "Wed, 21 Oct 2026 00:00:00 GMT"
LATER = "Wed, 21 Oct 2026 01:00:00 GMT"


class RetryAfterTests(unittest.TestCase):
    def test_missing_and_invalid_are_distinct_and_do_not_require_hold(self):
        self.assertEqual(parse_retry_after({}, NOW), {
            "status": "missing", "kind": "none", "retry_at": None,
            "delay_seconds": None, "manual_hold": False,
        })
        for value in ("", "-1", "1.5", "tomorrow", "10\r\nX-Extra: hidden", None):
            with self.subTest(value=value):
                result = parse_retry_after({"Retry-After": value}, NOW)
                self.assertEqual(result["status"], "invalid")
                self.assertFalse(result["manual_hold"])
                self.assertIsNone(result["retry_at"])

    def test_seconds_zero_and_long_valid_waits_are_not_capped_at_six_hours(self):
        for seconds in (0, 60, 3600, 86400, 604800):
            with self.subTest(seconds=seconds):
                result = parse_retry_after({"retry-after": str(seconds)}, NOW)
                self.assertEqual(result["status"], "valid")
                self.assertEqual(result["kind"], "seconds")
                self.assertEqual(result["retry_at"], NOW + seconds)
                self.assertEqual(result["delay_seconds"], seconds)
                self.assertFalse(result["manual_hold"])
        self.assertEqual(parse_retry_after({"Retry-After": "0" * 5000 + "60"}, NOW)["retry_at"], NOW + 60)

    def test_http_date_and_server_date_both_constrain_deadline(self):
        result = parse_retry_after({"Date": DATE, "Retry-After": LATER}, NOW)
        self.assertEqual(result["retry_at"], NOW + 3600)
        self.assertEqual(result["kind"], "date")
        # Local clock ahead: do not shorten the one-hour server-directed wait.
        result = parse_retry_after({"Date": DATE, "Retry-After": LATER}, NOW + 7200)
        self.assertEqual(result["retry_at"], NOW + 10800)
        # Local clock behind: retain the later absolute deadline as well.
        result = parse_retry_after({"Date": DATE, "Retry-After": LATER}, NOW - 7200)
        self.assertEqual(result["retry_at"], NOW + 3600)

    def test_missing_or_invalid_date_uses_local_absolute_deadline(self):
        for headers in ({"Retry-After": LATER}, {"Retry-After": LATER, "Date": "invalid"}):
            self.assertEqual(parse_retry_after(headers, NOW)["retry_at"], NOW + 3600)
        self.assertEqual(parse_retry_after({"Retry-After": DATE}, NOW + 1)["delay_seconds"], 0)

    def test_duplicate_retry_after_is_invalid_without_echoing_values(self):
        headers = Message()
        headers.add_header("Retry-After", "300")
        headers.add_header("Retry-After", "600")
        result = parse_retry_after(headers, NOW)
        self.assertEqual(result["status"], "invalid")
        self.assertNotIn("600", json.dumps(result))

    def test_unrepresentable_delays_require_hold_and_are_not_truncated(self):
        for value in ("9" * 5000, "253402300799", "Wed, 21 Oct 10000 00:00:00 GMT"):
            with self.subTest(length=len(value)):
                result = parse_retry_after({"Retry-After": value}, NOW)
                self.assertEqual(result["status"], "out_of_range")
                self.assertTrue(result["manual_hold"])
                self.assertIsNone(result["retry_at"])
                self.assertIsNone(result["delay_seconds"])


class TrackedBody(io.BytesIO):
    def __init__(self, body=b"response body must not enter diagnostics"):
        super().__init__(body)
        self.read_calls = 0

    def read1(self, *args):
        self.read_calls += 1
        return super().read1(*args)

    def read(self, *args):
        self.read_calls += 1
        return super().read(*args)


class FakeResponse(TrackedBody):
    status = 200

    def __init__(self, body=b"<rss />", headers=None, url=URL):
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body)), **(headers or {})}
        self.url = url

    def geturl(self):
        return self.url


class TransportDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        # A fixture or mocking mistake must fail before any outbound socket opens.
        for patcher in (
            patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")),
            patch.object(socket.socket, "connect_ex", side_effect=AssertionError("network forbidden")),
            patch("backend.news_feed_transport.urllib.request.getproxies", return_value={}),
            patch("backend.news_feed_transport.time.time", return_value=NOW),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def mocked_fetch(self, response, headers=None, proxies=None):
        captured = []
        class Opener:
            def open(self, request, *, timeout):
                captured.append((request, timeout))
                if isinstance(response, BaseException):
                    raise response
                return response
        with patch("backend.news_feed_transport.urllib.request.build_opener", return_value=Opener()) as build:
            with patch("backend.news_feed_transport.urllib.request.getproxies", return_value=proxies or {}):
                value = fetch_feed(URL, headers or {}, 12, 2 * 1024 * 1024)
        return value, captured, build.call_args.args

    def test_original_feed_response_positional_contract_remains_valid(self):
        result = FeedResponse(304, b"", {"ETag": '"cache"'})
        self.assertIsNone(result.diagnostics)
        self.assertEqual(result.headers["ETag"], '"cache"')

    def test_success_diagnostics_are_allowlisted_and_mock_route_is_unknown(self):
        response = FakeResponse(headers={"Date": DATE, "ETag": '"secret-cache-value"',
            "Set-Cookie": "secret-cookie", "X-Untrusted": "secret-arbitrary-header"})
        result, calls, _ = self.mocked_fetch(response,
            {"If-None-Match": '"secret-cache-value"', "If-Modified-Since": DATE},
            {"https": "http://127.0.0.1:12345"})
        self.assertTrue(response.closed)
        self.assertEqual(len(calls), 1)
        diagnostic = result.diagnostics
        self.assertEqual(set(diagnostic), {"schema_version", "route_class", "effective_bypass",
            "redirect_count", "final_host", "elapsed_ms", "conditional_etag",
            "conditional_modified_since", "response_date", "retry_after", "error_kind", "phase", "response_status"})
        self.assertEqual(diagnostic["route_class"], "unknown")
        self.assertIsNone(diagnostic["effective_bypass"])
        self.assertEqual(diagnostic["final_host"], "www.federalreserve.gov")
        self.assertEqual(diagnostic["response_date"], "2026-10-21T00:00:00Z")
        self.assertTrue(diagnostic["conditional_etag"])
        self.assertTrue(diagnostic["conditional_modified_since"])
        serialized = json.dumps(diagnostic)
        for excluded in ("secret", "127.0.0.1", "12345", "<rss", "Set-Cookie", "X-Untrusted"):
            self.assertNotIn(excluded, serialized)

    def test_403_429_are_closed_without_body_read_and_keep_safe_metadata(self):
        for status in (403, 429):
            with self.subTest(status=status):
                headers = Message()
                headers["Retry-After"] = "86400"
                headers["Date"] = DATE
                headers["Set-Cookie"] = "secret-cookie"
                body = TrackedBody()
                failure = urllib.error.HTTPError(URL, status, "secret-error-message", headers, body)
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.mocked_fetch(failure)
                self.assertIs(caught.exception, failure)
                self.assertTrue(body.closed)
                self.assertEqual(body.read_calls, 0)
                diagnostic = failure.news_diagnostics
                self.assertEqual(diagnostic["retry_after"]["retry_at"], NOW + 86400)
                self.assertNotIn("secret", json.dumps(diagnostic))

    def test_304_closes_without_reading_and_keeps_cache_header(self):
        headers = Message()
        headers["ETag"] = '"unchanged"'
        body = TrackedBody()
        response = urllib.error.HTTPError(URL, 304, "not modified", headers, body)
        result, calls, _ = self.mocked_fetch(response)
        self.assertEqual(result.status, 304)
        self.assertEqual(result.body, b"")
        self.assertEqual(result.headers["ETag"], '"unchanged"')
        self.assertTrue(body.closed)
        self.assertEqual(body.read_calls, 0)
        self.assertEqual(len(calls), 1)

    def test_non_http_failure_attaches_no_error_text(self):
        failure = urllib.error.URLError("secret-endpoint-and-credentials")
        with self.assertRaises(urllib.error.URLError) as caught:
            self.mocked_fetch(failure)
        self.assertIs(caught.exception, failure)
        self.assertNotIn("secret", json.dumps(failure.news_diagnostics))
        self.assertEqual(failure.news_diagnostics["route_class"], "unknown")

    def test_proxy_filter_excludes_remote_credential_and_unsupported_entries(self):
        _, _, handlers = self.mocked_fetch(FakeResponse(), proxies={
            "https": "http://user:secret@127.0.0.1:12345", "http": "http://remote.invalid:8000",
            "ftp": "http://127.0.0.1:9999", "all": "socks5://127.0.0.1:1080",
        })
        handler = next(item for item in handlers if isinstance(item, _ObservedProxyHandler))
        self.assertEqual(handler.proxies, {})

    def _observe_selected_https_handler(self, request, diagnostic):
        # Execute selection/observation only, with the actual network call stubbed.
        with patch.object(urllib.request.HTTPSHandler, "https_open", return_value=object()):
            _ObservedHTTPSHandler(diagnostic).https_open(request)
        return diagnostic.snapshot()

    def test_selected_direct_route_is_observed_without_proxy_configuration_claim(self):
        diagnostic = _Diagnostics({})
        observed = self._observe_selected_https_handler(urllib.request.Request(URL), diagnostic)
        self.assertEqual(observed["route_class"], "direct")
        self.assertIsNone(observed["effective_bypass"])

    def test_selected_loopback_proxy_and_actual_bypass_are_distinguished(self):
        proxy = "http://127.0.0.1:12345"
        for bypass in (False, True):
            with self.subTest(bypass=bypass):
                diagnostic = _Diagnostics({})
                request = urllib.request.Request(URL)
                handler = _ObservedProxyHandler({"https": proxy})
                with patch("backend.news_feed_transport.urllib.request.proxy_bypass", return_value=bypass):
                    handler.proxy_open(request, proxy, "https")
                observed = self._observe_selected_https_handler(request, diagnostic)
                self.assertEqual(observed["route_class"], "direct" if bypass else "local_loopback_proxy")
                self.assertIs(observed["effective_bypass"], bypass)
                self.assertNotIn("12345", json.dumps(observed))

    def test_same_host_redirect_count_does_not_include_rejected_target(self):
        diagnostic = _Diagnostics({})
        redirect = _SameHostRedirect(URL, diagnostic)
        request = urllib.request.Request(URL)
        redirected = redirect.redirect_request(request, None, 302, "", {}, URL + "?version=1")
        self.assertIsNotNone(redirected)
        self.assertEqual(diagnostic.snapshot()["redirect_count"], 1)
        for target in ("http://www.federalreserve.gov/feed", "https://remote.invalid/feed",
                       "https://www.federalreserve.gov:8443/feed", "https://user:secret@www.federalreserve.gov/feed"):
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    redirect.redirect_request(request, None, 302, "", {}, target)
                self.assertEqual(_safe_url(target, URL), "")
        self.assertEqual(diagnostic.snapshot()["redirect_count"], 1)
        self.assertEqual(redirect.max_redirections, 3)
        self.assertEqual(redirect.max_repeats, 1)

    def test_final_redirect_host_is_checked_and_body_limit_preserved(self):
        for response in (FakeResponse(url="https://remote.invalid/"),
                         FakeResponse(headers={"Content-Length": str(3 * 1024 * 1024)})):
            with self.assertRaises(ValueError):
                self.mocked_fetch(response)
            self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
