import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from backend.news_platform import (
    FeedResponse, MAX_BYTES, NewsPlatform, SOURCES, _SameHostRedirect,
    _safe_url, analyze, fetch_feed, parse_feed, plain_text,
)


NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc).timestamp()


def rss(title="FOMC interest rate statement", guid="one", date="Thu, 15 Oct 2026 10:00:00 GMT", excerpt="Policy remains unchanged.", link="https://www.federalreserve.gov/newsevents/pressreleases/example.htm"):
    return (f'<rss version="2.0"><channel><title>Official</title><item><guid>{guid}</guid><title>{title}</title>'
            f'<link>{link}</link><pubDate>{date}</pubDate><description><![CDATA[{excerpt}]]></description>'
            '</item></channel></rss>').encode()


class NewsPlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "news.db"
        self.now = NOW
        self.calls = []
        self.socket_guard = patch.object(socket.socket, "connect", side_effect=AssertionError("test_network_forbidden"))
        self.socket_guard.start()

        def transport(url, headers, timeout, size):
            self.calls.append((url, headers, timeout, size))
            return FeedResponse(200, rss(), {"ETag": '"v1"', "Last-Modified": "Thu, 15 Oct 2026 10:00:00 GMT"})

        self.app = NewsPlatform(self.path, transport=transport, clock=lambda: self.now)

    def tearDown(self):
        self.assertTrue(self.app.stop())
        self.socket_guard.stop()
        self.temp.cleanup()

    def source(self, source_id="fed"):
        with self.app._db() as db:
            return db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()

    def fetch(self, source_id="fed"):
        self.app._fetch(self.source(source_id))

    def wait_for(self, check, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(0.01)
        self.fail("background condition not reached")

    def test_constructor_and_paused_start_do_not_fetch(self):
        self.assertEqual(self.calls, [])
        self.app.start()
        self.app.start()
        self.assertFalse(self.app.overview()["enabled"])
        self.assertTrue(self.app.overview()["running"])
        self.assertEqual(self.app.overview()["counts"]["total"], 0)
        self.assertEqual(self.calls, [])

    def test_repeated_fetch_deduplicates_and_preserves_read_state(self):
        self.fetch()
        first = self.app.list_items()["items"][0]
        self.app.mark_read(first["id"], True)
        self.now += 100
        self.fetch()
        result = self.app.list_items()
        self.assertEqual(result["total"], 1)
        self.assertTrue(result["items"][0]["read"])
        self.assertEqual(first["discovered_at"], result["items"][0]["discovered_at"])
        self.assertEqual(first["updated_at"], result["items"][0]["updated_at"])
        self.assertEqual(self.calls[-1][1]["If-None-Match"], '"v1"')

    def test_changed_feed_updates_existing_item_without_losing_read(self):
        self.fetch()
        first = self.app.list_items()["items"][0]
        self.app.mark_read(first["id"])
        self.now += 100
        self.app.transport = lambda *args: FeedResponse(200, rss(excerpt="Revised policy excerpt."))
        self.fetch()
        changed = self.app.get_item(first["id"])
        self.assertEqual(changed["excerpt"], "Revised policy excerpt.")
        self.assertTrue(changed["read"])
        self.assertNotEqual(changed["updated_at"], first["updated_at"])

    def test_old_and_future_publication_are_not_recent_events(self):
        for guid, published in (("old", "Thu, 01 Oct 2026 10:00:00 GMT"), ("future", "Thu, 22 Oct 2026 10:00:00 GMT"), ("unknown", "not-a-date")):
            self.app.transport = lambda *args, g=guid, d=published: FeedResponse(200, rss(guid=g, date=d))
            self.fetch()
        overview = self.app.overview()
        self.assertEqual(overview["counts"]["last_24h"], 0)
        self.assertEqual(overview["counts"]["discovered_24h"], 3)
        self.assertTrue(any("未来" in text for item in self.app.list_items()["items"] for text in item["limitations"]))
        unknown = next(item for item in self.app.list_items()["items"] if item["published_at"] is None)
        self.assertTrue(any("发现时间不等于" in text for text in unknown["limitations"]))

    def test_search_source_category_importance_unread_and_pagination(self):
        self.fetch()
        self.app.transport = lambda *args: FeedResponse(200, rss(title="Release 100% done", guid="second", excerpt="Ordinary announcement."))
        self.fetch()
        self.assertEqual(self.app.list_items(query="%", source="fed")["total"], 1)
        self.assertEqual(self.app.list_items(query="' OR 1=1 --")["total"], 0)
        self.assertEqual(self.app.list_items(category="monetary")["total"], 1)
        self.assertEqual(self.app.list_items(importance="attention")["total"], 1)
        self.assertEqual(self.app.list_items(source="missing")["total"], 0)
        result = self.app.list_items(limit=1, offset=1)
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["items"]), 1)
        self.app.mark_read(result["items"][0]["id"])
        self.assertEqual(self.app.list_items(unread_only=True)["total"], 1)

    def test_304_preserves_items_and_advances_success_schedule(self):
        self.fetch()
        first = self.app.list_items()
        self.now += 900
        self.app.transport = lambda *args: FeedResponse(304)
        self.fetch()
        self.assertEqual(first, self.app.list_items())
        state = self.source()
        self.assertEqual(state["last_success"], self.now)
        self.assertEqual(state["next_poll"], self.now + 900)
        self.assertEqual(state["failures"], 0)

    def test_failure_backoff_is_durable_and_does_not_delete_items(self):
        self.fetch()
        self.app.transport = lambda *args: FeedResponse(503)
        self.fetch()
        self.assertEqual(self.source()["next_poll"], self.now + 900)
        with self.app._db() as db:
            checks_before = db.execute('SELECT COUNT(*) FROM poll_runs').fetchone()[0]
        self.app.refresh()
        self.fetch()  # Manual/internal entry points share the durable deadline.
        self.assertEqual(self.source()['failures'], 1)
        self.assertTrue(self.source()['refresh_queued'])
        with self.app._db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM poll_runs').fetchone()[0], checks_before)
        self.now = self.source()['next_poll']
        self.fetch()
        self.assertEqual(self.source()["next_poll"], self.now + 1800)
        replacement = NewsPlatform(self.path, transport=lambda *args: self.fail("no network"), clock=lambda: self.now)
        self.assertEqual(replacement.overview()["counts"]["sources_error"], 1)
        self.assertEqual(replacement.list_items()["total"], 1)
        self.assertEqual(replacement.overview()["sources"][1]["id"], "fed")
        replacement.stop()

    def test_refresh_fetches_all_once_without_enabling_continuous(self):
        self.app.start()
        self.app.refresh()
        self.wait_for(lambda: self.app.overview()["counts"]["sources_ok"] == 3)
        self.assertEqual(len(self.calls), 3)
        self.assertFalse(self.app.overview()["enabled"])

    def test_failed_source_does_not_prevent_other_sources(self):
        def transport(url, *args):
            if "bls.gov" in url:
                raise TimeoutError("simulated")
            return FeedResponse(200, rss())
        self.app.transport = transport
        self.app.start()
        self.app.control(True)
        self.wait_for(lambda: self.app.overview()["counts"]["sources_ok"] == 2)
        self.assertEqual(self.app.overview()["counts"]["sources_error"], 1)

    def test_enabled_and_schedule_survive_restart_without_extra_fetch(self):
        self.app.control(True)
        for source in SOURCES:
            self.fetch(source["id"])
        count = len(self.calls)
        self.app.stop()
        self.app = NewsPlatform(self.path, transport=lambda *args: self.fail("schedule not due"), clock=lambda: self.now)
        self.app.start()
        self.assertTrue(self.app.overview()["enabled"])
        self.assertEqual(len(self.calls), count)

    def test_pause_during_request_drains_current_and_prevents_next(self):
        entered, release = threading.Event(), threading.Event()
        def transport(*args):
            self.calls.append(args)
            entered.set()
            self.assertTrue(release.wait(3))
            return FeedResponse(200, rss())
        self.app.transport = transport
        self.app.start()
        self.app.refresh()
        self.assertTrue(entered.wait(3))
        self.app.control(False)
        release.set()
        self.wait_for(lambda: self.app.overview()["counts"]["sources_ok"] == 1)
        self.assertEqual(len(self.calls), 1)

    def test_stop_reports_inflight_and_drains_after_transport_returns(self):
        entered, release = threading.Event(), threading.Event()
        def transport(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return FeedResponse(200, rss())
        self.app.transport = transport
        self.app.start()
        self.app.refresh()
        self.assertTrue(entered.wait(3))
        self.assertFalse(self.app.stop(timeout=0))
        release.set()
        self.assertTrue(self.app.stop(timeout=3))
        self.assertFalse(self.app.overview()["running"])

    def test_foreign_database_is_refused_without_changes(self):
        foreign = Path(self.temp.name) / "studio.db"
        db = sqlite3.connect(foreign)
        try:
            with db:
                db.execute("CREATE TABLE user_data(value TEXT)")
                db.execute("INSERT INTO user_data VALUES('preserve')")
        finally:
            db.close()
        original = foreign.read_bytes()
        with self.assertRaisesRegex(ValueError, "independent"):
            NewsPlatform(foreign)
        self.assertEqual(original, foreign.read_bytes())

    def test_hardlinked_database_and_reparse_parent_are_rejected_before_open(self):
        linked = Path(self.temp.name) / "linked.db"
        os.link(self.path, linked)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "independent_regular"):
            NewsPlatform(linked)
        self.assertEqual(before, self.path.read_bytes())
        with patch("backend.news_platform.first_reparse_component", return_value=Path(self.temp.name)):
            with patch("backend.news_platform.sqlite3.connect", side_effect=AssertionError("must refuse before SQLite")):
                with self.assertRaisesRegex(ValueError, "alias"):
                    NewsPlatform(Path(self.temp.name) / "uncreated.db")

    def test_no_fake_ai_and_no_untrusted_markup(self):
        self.app.transport = lambda *args: FeedResponse(200, rss(excerpt='<p>Hello <b>world</b></p><script>alert(1)</script><img src="x" onerror="bad()">'))
        self.fetch()
        item = self.app.list_items()["items"][0]
        self.assertEqual(item["excerpt"], "Hello world")
        self.assertEqual(item["analysis_mode"], "local_rules")
        self.assertFalse(self.app.overview()["analysis"]["ai_configured"])
        self.assertTrue(any("未抓取" in text for text in item["limitations"]))

    def test_invalid_mutations_and_unknown_items(self):
        with self.assertRaises(ValueError):
            self.app.control("true")
        with self.assertRaises(ValueError):
            self.app.mark_read("missing", "true")
        self.assertIsNone(self.app.get_item("missing"))
        self.assertIsNone(self.app.mark_read("missing"))


class FeedSafetyTests(unittest.TestCase):
    def test_fixed_sources_only_before_network_access(self):
        with patch("urllib.request.build_opener", side_effect=AssertionError("must not create transport")):
            with self.assertRaisesRegex(ValueError, "source_not_allowed"):
                fetch_feed("http://127.0.0.1/private", {}, 1, MAX_BYTES)

    def test_redirect_and_item_url_are_restricted(self):
        base = SOURCES[0]["url"]
        for value in ("http://www.federalreserve.gov/a", "https://evil.example/a", "https://www.federalreserve.gov:444/a", "https://user@www.federalreserve.gov/a", "javascript:alert(1)"):
            self.assertEqual(_safe_url(value, base), "")
            with self.assertRaises(ValueError):
                _SameHostRedirect(base).redirect_request(None, None, 302, "", {}, value)
        self.assertEqual(_safe_url("/official.html", base), "https://www.federalreserve.gov/official.html")

    def test_atom_and_rss_without_guid_are_supported(self):
        body = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom release</title><link href="https://www.federalreserve.gov/release.htm"/><updated>2026-10-15T10:00:00Z</updated><summary>&lt;b&gt;Summary&lt;/b&gt;</summary></entry></feed>'
        entries = parse_feed(body, SOURCES[0])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["excerpt"], "Summary")
        self.assertIsNotNone(entries[0]["published"])
        self.assertEqual(entries, parse_feed(body, SOURCES[0]))

    def test_xml_entity_declarations_and_oversized_bodies_are_rejected(self):
        dangerous = '<!DOCTYPE rss [<!ENTITY x "bad">]><rss><channel/></rss>'
        for body in (dangerous.encode(), dangerous.encode("utf-16")):
            with self.assertRaisesRegex(ValueError, "document_type"):
                parse_feed(body, SOURCES[0])
        with self.assertRaisesRegex(ValueError, "too_large"):
            parse_feed(b" " * (MAX_BYTES + 1), SOURCES[0])

    def test_company_category_and_rule_keywords(self):
        analysis = analyze("Seagate announces quarterly earnings", "Revenue update.", NOW, NOW, "seagate")
        self.assertEqual(analysis["category"], "company")
        self.assertEqual(analysis["importance"], "attention")
        self.assertIn("earnings", analysis["keywords"])
        self.assertEqual(analyze("New product", "Announcement", NOW, NOW, "seagate")["importance"], "normal")


if __name__ == "__main__":
    unittest.main()
