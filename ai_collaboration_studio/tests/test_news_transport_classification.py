"""Offline failure diagnostics: no socket, proxy configuration or database access."""
from __future__ import annotations

import errno
import io
import json
import socket
import ssl
import unittest
import urllib.error
import urllib.request
from email.message import Message
from unittest.mock import patch

from backend.news_feed_transport import (
    SOURCES, FeedValidationError, _Diagnostics, _SameHostRedirect,
    classify_feed_error, fetch_feed,
)
from backend.news_runtime_evidence import _safe_json

URL = next(source["url"] for source in SOURCES if source["id"] == "bls")
PRIVATE = "private response header or exception text"


class Body(io.BytesIO):
    status = 200

    def __init__(self, body=b"<rss/>", *, headers=None, failure=None):
        super().__init__(body)
        self.headers = headers or {}
        self.failure = failure
        self.read_calls = 0

    def geturl(self):
        return URL

    def read1(self, size):
        self.read_calls += 1
        if self.failure is not None:
            raise self.failure
        return super().read1(size)

    def read(self, *args):
        self.read_calls += 1
        return super().read(*args)


class FailureClassificationTests(unittest.TestCase):
    def setUp(self):
        for patcher in (
            patch.object(socket.socket, "connect", side_effect=AssertionError("offline only")),
            patch.object(socket.socket, "connect_ex", side_effect=AssertionError("offline only")),
            patch.object(socket, "getaddrinfo", side_effect=AssertionError("offline only")),
            patch("backend.news_feed_transport.urllib.request.getproxies", return_value={}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.requests = []

    def fetch(self, response, *, max_bytes=1024):
        requests = self.requests

        class Opener:
            def open(self, request, *, timeout):
                requests.append(request)
                if len(requests) != 1:
                    raise AssertionError("transport retried unexpectedly")
                if isinstance(response, BaseException):
                    raise response
                return response

        with patch("backend.news_feed_transport.urllib.request.build_opener", return_value=Opener()):
            return fetch_feed(URL, {}, 12, max_bytes)

    def test_categories_use_types_or_numeric_codes_never_message_guesses(self):
        class UnprintableError(Exception):
            def __str__(self):
                raise AssertionError("exception text inspected")

        cases = (
            (TimeoutError(PRIVATE), "timeout"),
            (urllib.error.URLError(TimeoutError(PRIVATE)), "timeout"),
            (socket.gaierror(socket.EAI_NONAME, PRIVATE), "dns_error"),
            (urllib.error.URLError(ssl.SSLError(PRIVATE)), "tls_error"),
            (ConnectionResetError(PRIVATE), "connection_error"),
            (OSError(errno.ETIMEDOUT, PRIVATE), "timeout"),
            (OSError(errno.EHOSTUNREACH, PRIVATE), "connection_error"),
            (urllib.error.URLError("timeout dns tls " + PRIVATE), "transport_error"),
            (UnprintableError(), "transport_error"),
        )
        for failure, expected in cases:
            with self.subTest(category=expected, failure_type=type(failure).__name__):
                self.assertEqual(classify_feed_error(failure), expected)
        for code in ("redirect_blocked", "feed_too_large", "invalid_feed", "unsafe_feed"):
            self.assertEqual(classify_feed_error(FeedValidationError(code)), code)
        with self.assertRaises(ValueError):
            FeedValidationError(PRIVATE)

    def test_body_timeout_retains_received_200_closes_response_and_does_not_retry(self):
        response = Body(headers={"Set-Cookie": PRIVATE}, failure=TimeoutError(PRIVATE))
        with self.assertRaises(TimeoutError) as caught:
            self.fetch(response)
        diagnostic = caught.exception.news_diagnostics
        self.assertEqual((diagnostic["error_kind"], diagnostic["phase"], diagnostic["response_status"]),
                         ("timeout", "response_body", 200))
        self.assertTrue(response.closed)
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn(PRIVATE, json.dumps(diagnostic))

    def test_http_denials_preserve_native_status_and_close_without_reading_error_body(self):
        for status in (403, 429):
            with self.subTest(status=status):
                self.requests.clear()
                body = Body(PRIVATE.encode())
                headers = Message()
                headers["Retry-After"] = "3600"
                headers["Set-Cookie"] = PRIVATE
                failure = urllib.error.HTTPError(URL, status, PRIVATE, headers, body)
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.fetch(failure)
                self.assertIs(caught.exception, failure)
                diagnostic = failure.news_diagnostics
                self.assertEqual((diagnostic["error_kind"], diagnostic["phase"], diagnostic["response_status"]),
                                 ("http_error", "response_headers", status))
                self.assertEqual(diagnostic["retry_after"]["delay_seconds"], 3600)
                self.assertEqual(body.read_calls, 0)
                self.assertTrue(body.closed)
                self.assertNotIn(PRIVATE, json.dumps(diagnostic))

    def test_success_uses_honest_contact_user_agent_and_304_is_not_an_error(self):
        response = Body()
        result = self.fetch(response)
        user_agent = dict((key.lower(), value) for key, value in self.requests[0].header_items())["user-agent"]
        self.assertIn("AI-Collaboration-Studio-Local-News/1.0", user_agent)
        self.assertIn("https://github.com/TheDeadly-cat/hajimi-jiaoyifenxi/issues", user_agent)
        self.assertNotIn("Mozilla", user_agent)
        self.assertEqual((result.diagnostics["error_kind"], result.diagnostics["phase"], result.diagnostics["response_status"]),
                         (None, "complete", 200))
        self.requests.clear()
        body = Body()
        result = self.fetch(urllib.error.HTTPError(URL, 304, PRIVATE, Message(), body))
        self.assertEqual((result.status, result.diagnostics["error_kind"], result.diagnostics["response_status"]),
                         (304, None, 304))
        self.assertEqual(body.read_calls, 0)
        self.assertTrue(body.closed)

    def test_redirect_and_both_size_limits_are_typed_and_fail_closed(self):
        diagnostic = _Diagnostics({})
        handler = _SameHostRedirect(URL, diagnostic)
        with self.assertRaises(FeedValidationError) as caught:
            handler.redirect_request(urllib.request.Request(URL), None, 302, "", {}, "https://example.com/")
        self.assertEqual(classify_feed_error(caught.exception), "redirect_blocked")
        self.assertEqual((diagnostic.phase, diagnostic.response_status), ("redirect", 302))
        for headers, expected_phase in (({"Content-Length": "20"}, "response_headers"), ({}, "response_body")):
            with self.subTest(phase=expected_phase):
                self.requests.clear()
                response = Body(b"x" * 20, headers=headers)
                with self.assertRaises(FeedValidationError) as caught:
                    self.fetch(response, max_bytes=10)
                self.assertEqual(caught.exception.code, "feed_too_large")
                evidence = caught.exception.news_diagnostics
                self.assertEqual((evidence["phase"], evidence["response_status"]), (expected_phase, 200))
                self.assertTrue(response.closed)

    def test_sanitizer_keeps_safe_categories_rejects_payloads_and_bounds_status(self):
        safe = json.loads(_safe_json({"error_kind": "timeout", "phase": "response_body", "response_status": 200,
                                     "error_message": PRIVATE, "raw_headers": {"Cookie": PRIVATE}}, "transport"))
        self.assertEqual((safe["error_kind"], safe["phase"], safe["response_status"]), ("timeout", "response_body", 200))
        self.assertNotIn(PRIVATE, json.dumps(safe))
        for status in (True, 99, 600, "200", 200.0):
            with self.subTest(status_type=type(status).__name__):
                invalid = json.loads(_safe_json({"error_kind": PRIVATE, "phase": PRIVATE, "response_status": status}, "transport"))
                self.assertNotIn("error_kind", invalid)
                self.assertNotIn("phase", invalid)
                self.assertNotIn("response_status", invalid)
        schedule = json.loads(_safe_json({"rate_recovery_after": "2026-10-24T20:00:00+08:00"}, "schedule"))
        self.assertEqual(schedule["rate_recovery_after"], "2026-10-24T12:00:00Z")
        for date in (PRIVATE, "2026-10-24T20:00:00"):
            self.assertNotIn("rate_recovery_after", json.loads(_safe_json({"rate_recovery_after": date}, "schedule")))


if __name__ == "__main__":
    unittest.main()
