from __future__ import annotations

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.request import Request, build_opener

from backend.market.official_http import OfficialHttpsRedirectHandler
from backend.market.sec_edgar import SEC_TICKERS_URL, _is_allowed_sec_fetch_url
from backend.source_poll_control import SourcePollCancelled


class _QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args) -> None:
        return


class OfficialHttpRedirectPolicyTests(unittest.TestCase):
    class _FakeRedirectResponse:
        def __init__(self) -> None:
            self.read_calls = 0
            self.close_calls = 0

        def read(self, *_args, **_kwargs):
            self.read_calls += 1
            raise AssertionError("redirect response body must not be drained")

        def close(self) -> None:
            self.close_calls += 1

    class _FakeParent:
        def __init__(self) -> None:
            self.requests = []

        def open(self, request, *, timeout):
            self.requests.append((request, timeout))
            return "redirected"

    def test_cross_policy_redirect_is_rejected_before_target_request(self) -> None:
        target_hits = [0]

        class TargetHandler(_QuietHandler):
            def do_GET(self) -> None:
                target_hits[0] += 1
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"unexpected")

        target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        target_url = f"http://127.0.0.1:{target.server_port}/redirect-target"

        class RedirectHandler(_QuietHandler):
            def do_GET(self) -> None:
                self.send_response(302)
                self.send_header("Location", target_url)
                self.end_headers()

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        threads = [
            threading.Thread(target=server.serve_forever, daemon=True)
            for server in (target, redirect)
        ]
        for thread in threads:
            thread.start()
        try:
            opener = build_opener(
                OfficialHttpsRedirectHandler({"www.sec.gov"})
            )
            with self.assertRaisesRegex(ValueError, "fixed HTTPS policy"):
                opener.open(
                    Request(f"http://127.0.0.1:{redirect.server_port}/start"),
                    timeout=2,
                )
            self.assertEqual(target_hits[0], 0)
        finally:
            for server in (redirect, target):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)

    def test_allowed_and_rejected_redirect_responses_close_without_read(self) -> None:
        policy = OfficialHttpsRedirectHandler({"official.example"})
        parent = self._FakeParent()
        policy.parent = parent
        request = Request("https://official.example/start")
        request.timeout = 3
        allowed_response = self._FakeRedirectResponse()

        result = policy.http_error_302(
            request,
            allowed_response,
            302,
            "Found",
            {"location": "/next"},
        )

        self.assertEqual(result, "redirected")
        self.assertEqual(allowed_response.read_calls, 0)
        self.assertEqual(allowed_response.close_calls, 1)
        self.assertEqual(parent.requests[0][0].full_url, "https://official.example/next")
        self.assertEqual(parent.requests[0][1], 3)

        rejected_response = self._FakeRedirectResponse()
        with self.assertRaisesRegex(ValueError, "fixed HTTPS policy"):
            policy.http_error_302(
                request,
                rejected_response,
                302,
                "Found",
                {"location": "http://127.0.0.1/blocked"},
            )
        self.assertEqual(rejected_response.read_calls, 0)
        self.assertEqual(rejected_response.close_calls, 1)

    def test_each_redirect_reclips_timeout_and_observes_cancellation(self) -> None:
        cancel = threading.Event()
        policy = OfficialHttpsRedirectHandler(
            {"official.example"},
            deadline_monotonic_ms=5_000,
            cancel_event=cancel,
        )
        parent = self._FakeParent()
        policy.parent = parent
        request = Request("https://official.example/start")
        request.timeout = 3
        response = self._FakeRedirectResponse()

        with patch(
            "backend.market.official_http.time.monotonic",
            return_value=4.75,
        ):
            result = policy.http_error_302(
                request,
                response,
                302,
                "Found",
                {"location": "/next"},
            )

        self.assertEqual(result, "redirected")
        self.assertEqual(response.close_calls, 1)
        self.assertAlmostEqual(parent.requests[0][1], 0.25, places=3)

        cancelled_response = self._FakeRedirectResponse()
        cancel.set()
        with self.assertRaises(SourcePollCancelled):
            policy.http_error_302(
                request,
                cancelled_response,
                302,
                "Found",
                {"location": "/cancelled"},
            )
        self.assertEqual(cancelled_response.close_calls, 1)
        self.assertEqual(len(parent.requests), 1)

    def test_sec_endpoint_policy_binds_host_and_path(self) -> None:
        self.assertTrue(_is_allowed_sec_fetch_url(SEC_TICKERS_URL))
        self.assertTrue(
            _is_allowed_sec_fetch_url(
                "https://data.sec.gov/submissions/CIK0001045810.json"
            )
        )
        for value in (
            "https://www.sec.gov/not-the-fixed-endpoint.json",
            "https://data.sec.gov/submissions/CIK0001045810.json?next=1",
            "https://data.sec.gov/submissions/CIK0001045810.json#fragment",
            "https://data.sec.gov:444/submissions/CIK0001045810.json",
            "https://user@data.sec.gov/submissions/CIK0001045810.json",
            "http://data.sec.gov/submissions/CIK0001045810.json",
        ):
            with self.subTest(value=value):
                self.assertFalse(_is_allowed_sec_fetch_url(value))

    def test_sec_redirect_cannot_switch_the_requested_cik(self) -> None:
        expected = "https://data.sec.gov/submissions/CIK0001045810.json"
        policy = OfficialHttpsRedirectHandler(
            {"data.sec.gov"},
            url_validator=lambda candidate: candidate == expected,
        )
        parent = self._FakeParent()
        policy.parent = parent
        request = Request(expected)
        request.timeout = 3
        response = self._FakeRedirectResponse()

        with self.assertRaisesRegex(ValueError, "fixed endpoint policy"):
            policy.http_error_302(
                request,
                response,
                302,
                "Found",
                {
                    "location": (
                        "https://data.sec.gov/submissions/CIK0000000001.json"
                    )
                },
            )

        self.assertEqual(parent.requests, [])
        self.assertEqual(response.read_calls, 0)
        self.assertEqual(response.close_calls, 1)


if __name__ == "__main__":
    unittest.main()
