"""Offline HTTP response lifecycle and scheduled recovery for the news reader."""
from __future__ import annotations

import io
import socket
import tempfile
import unittest
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from backend.news_platform import MAX_BYTES, NewsPlatform, SOURCES, fetch_feed


FED_URL = next(source["url"] for source in SOURCES if source["id"] == "fed")
NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc).timestamp()
LAST_MODIFIED = "Thu, 15 Oct 2026 10:00:00 GMT"


def feed(guid="original", title="Synthetic official release"):
    return (
        '<rss version="2.0"><channel><item>'
        f'<guid>{guid}</guid><title>{title}</title>'
        '<link>https://www.federalreserve.gov/newsevents/pressreleases/synthetic.htm</link>'
        '<pubDate>Thu, 15 Oct 2026 10:00:00 GMT</pubDate>'
        '<description>Offline fixture only.</description>'
        '</item></channel></rss>'
    ).encode("utf-8")


class TrackedBody(io.BytesIO):
    def __init__(self, value=b""):
        super().__init__(value)
        self.read_calls = 0

    def read(self, *args):
        self.read_calls += 1
        return super().read(*args)

    def read1(self, *args):
        self.read_calls += 1
        return super().read1(*args)


class FakeResponse(TrackedBody):
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.status = 200
        self.headers = {"Content-Length": str(len(body)), **(headers or {})}

    def geturl(self):
        return FED_URL


def http_error(status, headers=None):
    message = Message()
    for name, value in (headers or {}).items():
        message[name] = value
    body = TrackedBody(b"Synthetic HTTP error body; must not be read.")
    return HTTPError(FED_URL, status, "synthetic status", message, body), body


class FakeOpener:
    def __init__(self):
        self.responses = []
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append({"url": request.full_url, "timeout": timeout,
                           "headers": {name.lower(): value for name, value in request.header_items()}})
        if not self.responses:
            raise AssertionError("unexpected extra transport attempt")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class OfflineFeedCase(unittest.TestCase):
    def setUp(self):
        self.opener = FakeOpener()
        for patcher in (
            patch.object(socket.socket, "connect", side_effect=AssertionError("offline test forbids network")),
            patch("backend.news_platform.urllib.request.getproxies", return_value={}),
            patch("backend.news_platform.urllib.request.build_opener", return_value=self.opener),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


class FeedHTTPResponseTests(OfflineFeedCase):
    def test_successful_200_closes_response_and_preserves_body_headers(self):
        body = feed()
        response = FakeResponse(body, {"ETag": '"v1"', "Last-Modified": LAST_MODIFIED})
        self.opener.responses.append(response)

        result = fetch_feed(FED_URL, {}, 12, MAX_BYTES)

        self.assertEqual(result.status, 200)
        self.assertEqual(result.body, body)
        self.assertEqual(result.headers["ETag"], '"v1"')
        self.assertEqual(result.headers["Last-Modified"], LAST_MODIFIED)
        self.assertTrue(response.closed)
        self.assertEqual(len(self.opener.calls), 1)

    def test_http_error_304_closes_without_reading_and_preserves_headers(self):
        error, body = http_error(304, {"ETag": '"v304"', "Last-Modified": LAST_MODIFIED})
        self.opener.responses.append(error)

        result = fetch_feed(FED_URL, {"If-None-Match": '"v1"'}, 12, MAX_BYTES)

        self.assertEqual(result.status, 304)
        self.assertEqual(result.body, b"")
        self.assertEqual(result.headers, {"ETag": '"v304"', "Last-Modified": LAST_MODIFIED})
        self.assertTrue(body.closed)
        self.assertEqual(body.read_calls, 0)
        self.assertEqual(len(self.opener.calls), 1)

    def test_http_error_404_closes_without_reading_and_propagates_same_error(self):
        error, body = http_error(404, {"Cache-Control": "no-store"})
        self.opener.responses.append(error)

        with self.assertRaises(HTTPError) as caught:
            fetch_feed(FED_URL, {}, 12, MAX_BYTES)

        self.assertIs(caught.exception, error)
        self.assertEqual(caught.exception.code, 404)
        self.assertTrue(body.closed)
        self.assertEqual(body.read_calls, 0)
        self.assertEqual(len(self.opener.calls), 1)


class FeedHTTPScheduledRecoveryTests(OfflineFeedCase):
    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory(prefix="news-feed-http-")
        self.addCleanup(self.temporary.cleanup)
        self.now = NOW
        self.app = NewsPlatform(Path(self.temporary.name) / "news.sqlite3",
                                transport=fetch_feed, clock=lambda: self.now)
        self.addCleanup(lambda: self.assertTrue(self.app.stop(timeout=0)))
        # Keep this synthetic schedule focused on Fed, without starting a worker.
        with self.app._db() as db:
            db.execute("UPDATE sources SET enabled=0 WHERE id!='fed'")
        self.app.control(True)
        self.assertIsNone(self.app._thread)

    def source(self):
        with self.app._db() as db:
            return dict(db.execute("SELECT * FROM sources WHERE id='fed'").fetchone())

    def polls(self):
        # Each _db call opens a new connection, so this observes committed rows.
        with self.app._db() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM poll_runs WHERE source_id='fed' ORDER BY started,rowid")]

    def seed_then_fail_at_normal_due_time(self):
        initial = FakeResponse(feed(), {"ETag": '"v1"', "Last-Modified": LAST_MODIFIED})
        self.error, self.error_body = http_error(404, {"ETag": '"error-page"'})
        self.opener.responses.extend([initial, self.error])
        self.app._cycle()
        self.assertTrue(initial.closed)
        first = self.app.list_items()["items"][0]
        self.app.mark_read(first["id"], True)
        self.saved_items = self.app.list_items()
        self.saved_source = self.source()
        self.saved_item_id = first["id"]

        self.now = self.saved_source["next_poll"]
        self.app._cycle()
        self.assertEqual(len(self.opener.calls), 2)
        self.assertTrue(self.error_body.closed)
        self.assertEqual(self.error_body.read_calls, 0)
        self.assertEqual(self.opener.calls[1]["headers"]["if-none-match"], '"v1"')
        self.assertEqual(self.opener.calls[1]["headers"]["if-modified-since"], LAST_MODIFIED)
        self.assertEqual(self.app._refresh_remaining, set())
        self.assertFalse(self.app._refresh_pending)
        self.assertIsNone(self.app._thread)
        self.failed_poll = self.polls()[-1]

    def assert_failed_state_preserved(self):
        state = self.source()
        self.assertEqual(self.app.list_items(), self.saved_items)
        self.assertTrue(self.app.get_item(self.saved_item_id)["read"])
        for field in ("url", "enabled", "etag", "last_modified", "last_success"):
            self.assertEqual(state[field], self.saved_source[field], field)
        self.assertEqual(state["status"], "error")
        self.assertEqual(state["failures"], 1)
        self.assertEqual(state["last_attempt"], self.now)
        self.assertEqual(state["next_poll"], self.now + self.app.poll_interval)
        self.assertEqual(state["error"], "本次请求返回 HTTP 404，已安排稍后重试。")
        self.assertEqual(self.failed_poll["outcome"], "failed")
        self.assertEqual(self.failed_poll["http_status"], 404)
        self.assertEqual(self.failed_poll["error_code"], "http_error")
        self.assertEqual(self.failed_poll["finished"], self.now)
        self.assertEqual(self.failed_poll["new_items"], 0)
        self.assertEqual(self.failed_poll["updated_items"], 0)
        self.assertEqual(len(self.polls()), 2)

    def recover_when_due(self, response):
        due = self.source()["next_poll"]
        self.opener.responses.append(response)
        self.app._cycle()
        self.assertEqual(len(self.opener.calls), 2, "must not retry immediately after 404")
        self.now = due - 1
        self.app._cycle()
        self.assertEqual(len(self.opener.calls), 2, "must wait for the ordinary next_poll")
        self.assertEqual(len(self.polls()), 2)
        self.now = due
        self.app._cycle()
        self.assertEqual(len(self.opener.calls), 3)
        self.assertEqual(self.opener.calls[2]["url"], FED_URL)
        self.assertEqual(self.opener.calls[2]["headers"]["if-none-match"], '"v1"')
        self.assertEqual(self.opener.calls[2]["headers"]["if-modified-since"], LAST_MODIFIED)
        self.app._cycle()
        self.assertEqual(len(self.opener.calls), 3, "one due poll must not trigger another retry")
        self.assertEqual(self.opener.responses, [])
        state = self.source()
        self.assertEqual(state["status"], "ok")
        self.assertEqual(state["failures"], 0)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_success"], self.now)
        self.assertEqual(state["next_poll"], self.now + self.app.poll_interval)
        self.assertEqual(state["url"], FED_URL)
        self.assertEqual(state["enabled"], 1)
        self.assertEqual(self.app._refresh_remaining, set())
        self.assertIsNone(self.app._thread)
        rows = self.polls()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1], self.failed_poll, "recovery must preserve the original 404 receipt")
        self.assertTrue(self.app.get_item(self.saved_item_id)["read"])
        return rows[-1]

    def test_http_404_preserves_items_read_cache_and_success_with_durable_failure(self):
        self.seed_then_fail_at_normal_due_time()
        self.assert_failed_state_preserved()

    def test_due_http_304_recovers_without_new_items_and_preserves_404_history(self):
        self.seed_then_fail_at_normal_due_time()
        self.assert_failed_state_preserved()
        error, body = http_error(304)

        recovered = self.recover_when_due(error)

        self.assertTrue(body.closed)
        self.assertEqual(body.read_calls, 0)
        self.assertEqual(self.app.list_items(), self.saved_items)
        self.assertEqual(self.source()["etag"], '"v1"')
        self.assertEqual(self.source()["last_modified"], LAST_MODIFIED)
        self.assertEqual(recovered["outcome"], "not_modified")
        self.assertEqual(recovered["http_status"], 304)
        self.assertEqual(recovered["parsed_items"], 0)
        self.assertEqual(recovered["new_items"], 0)
        self.assertEqual(recovered["updated_items"], 0)

    def test_due_http_200_recovers_and_retains_old_read_item_and_404_history(self):
        self.seed_then_fail_at_normal_due_time()
        self.assert_failed_state_preserved()
        modified = "Thu, 15 Oct 2026 10:15:00 GMT"
        response = FakeResponse(feed(guid="later", title="Synthetic later release"),
                                {"ETag": '"v2"', "Last-Modified": modified})

        recovered = self.recover_when_due(response)

        self.assertTrue(response.closed)
        self.assertEqual(self.app.list_items()["total"], 2)
        self.assertEqual(self.app.get_item(self.saved_item_id), self.saved_items["items"][0])
        self.assertEqual(self.source()["etag"], '"v2"')
        self.assertEqual(self.source()["last_modified"], modified)
        self.assertEqual(recovered["outcome"], "success")
        self.assertEqual(recovered["http_status"], 200)
        self.assertEqual(recovered["new_items"], 1)
        self.assertEqual(recovered["updated_items"], 0)


if __name__ == "__main__":
    unittest.main()
