from __future__ import annotations

import io
import json
import threading
import time
import unittest
from datetime import datetime, timezone, tzinfo
from unittest.mock import patch

from backend.market.micron_ir_json import (
    MICRON_IR_JSON_URL,
    MICRON_TIME_METADATA_HASH_SEMANTICS,
    MicronIrJsonClient,
    MicronIrJsonError,
    is_micron_detail_url,
    micron_time_metadata_sha256,
)
from backend.source_poll_control import SourcePollCancelled, SourcePollDeadlineExceeded


HOST = "https://investors.micron.com"
PATH = "/news/press-release/2026/Micron-Technology-to-Report-Fiscal-Fourth-Quarter-Results-on-September-30-2026/default.aspx"
TITLE = "Micron Technology to Report Fiscal Fourth Quarter Results on September 30, 2026"
STAMP = "2026-08-26T15:01:00Z"
NOW = datetime(2026, 9, 5, tzinfo=timezone.utc)
EXPECTED_LIST_URL = (
    "https://investors.micron.com/feed/PressRelease.svc/GetPressReleaseList"
    "?LanguageId=1&bodyType=0&pressReleaseDateFilter=1"
    "&categoryId=00000000-0000-0000-0000-000000000000"
    "&pageSize=30&pageNumber=0&tagList=&includeTags=true&year=-1&excludeSelection=1"
)


def record(identity=4945):
    return {
        "PressReleaseId": identity, "RevisionNumber": 55457,
        "Headline": TITLE, "LinkToDetailPage": PATH,
        "PressReleaseDate": "08/26/2026 16:01:00", "ShortDescription": "",
        "Body": None, "LinkToUrl": "https://not-requested.example/",
        "MediaCollection": [{"SourceUrl": "https://not-requested.example/image.png"}],
        "Attachments": [{"Url": "https://not-requested.example/attachment.pdf"}],
    }


def metadata(row=None, **overrides):
    row = row or record()
    value = {
        "@context": "https://schema.org", "@type": "NewsArticle",
        "mainEntityOfPage": {"@type": "WebPage", "@id": HOST + row["LinkToDetailPage"]},
        "headline": row["Headline"], "datePublished": STAMP, "dateModified": STAMP,
    }
    value.update(overrides)
    return value


def head(value=None):
    value = value or metadata()
    return (
        '<html><head><script src="https://not-requested.example/code.js"></script>'
        '<script type="application/ld+json">' + json.dumps(value) + '</script></head>'
    ).encode()


def listing(rows):
    return json.dumps({"GetPressReleaseListResult": rows}).encode()


class FixtureFetch:
    def __init__(self, rows=None, heads=None):
        self.rows = [record()] if rows is None else rows
        self.heads = heads or {}
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, url, **controls):
        with self.lock:
            self.calls.append((url, controls))
        if url == EXPECTED_LIST_URL:
            return listing(self.rows)
        return self.heads.get(url, head())


class MicronIrJsonTests(unittest.TestCase):
    def test_fixed_recent30_query_and_metadata_projection(self):
        fetch = FixtureFetch()
        client = MicronIrJsonClient(fetch_bytes=fetch, clock=lambda: NOW)
        self.assertEqual(fetch.calls, [])
        value = client.read_recent()
        self.assertEqual(MICRON_IR_JSON_URL, EXPECTED_LIST_URL)
        self.assertEqual([call[0] for call in fetch.calls], [EXPECTED_LIST_URL, HOST + PATH])
        self.assertEqual(fetch.calls[0][1]["max_bytes"], 1_000_000)
        self.assertFalse(fetch.calls[0][1]["head_only"])
        self.assertEqual(fetch.calls[1][1]["max_bytes"], 128 * 1024)
        self.assertTrue(fetch.calls[1][1]["head_only"])
        self.assertIs(value["complete"], True)
        release = value["releases"][0]
        self.assertEqual(release["published_at"], STAMP)
        self.assertEqual(release["source_declared_time_raw"], "08/26/2026 16:01:00")
        self.assertEqual(release["q4_press_release_id"], 4945)
        self.assertEqual(release["q4_revision_number"], 55457)
        self.assertEqual(release["source_format"], "micron_q4_public_json_v1")
        self.assertEqual(release["guid"], "")
        self.assertEqual(release["summary"], "")
        self.assertEqual(release["time_metadata_sha256"], micron_time_metadata_sha256(
            official_url=HOST + PATH, title=TITLE, published_at=STAMP,
            metadata_date_modified=STAMP,
        ))
        self.assertEqual(MICRON_TIME_METADATA_HASH_SEMANTICS,
                         "normalized_newsarticle_head_metadata_not_html_body")
        self.assertIs(client.transport_identity, fetch)
        with self.assertRaises(AttributeError):
            client.transport_identity = lambda: None

    def test_empty_recent_scope_is_complete(self):
        fetch = FixtureFetch([])
        self.assertEqual(MicronIrJsonClient(fetch_bytes=fetch).read_recent(),
                         {"releases": [], "complete": True})
        self.assertEqual(len(fetch.calls), 1)

    def test_unsafe_detail_links_are_rejected_before_any_metadata_fetch(self):
        for link in (
            "//investors.micron.com" + PATH, HOST + PATH,
            "https://user:pass@investors.micron.com" + PATH,
            PATH + "?next=1", PATH + "#part", PATH.replace("/2026/", "/../"),
            PATH.replace("/2026/", "/%2e%2e/"), PATH.replace("/2026/", "/%252e%252e/"),
            PATH.replace("/2026/", "/2026\\/"), "/news/default.aspx", PATH + "\n",
            "/news/press-release/2026/a%20b/default.aspx",
            "/news/press-release/2026/a.b/default.aspx",
        ):
            with self.subTest(link=link):
                bad = record(4946)
                bad["LinkToDetailPage"] = link
                fetch = FixtureFetch([record(), bad])
                with self.assertRaises(MicronIrJsonError):
                    MicronIrJsonClient(fetch_bytes=fetch, clock=lambda: NOW).read_recent()
                self.assertEqual(len(fetch.calls), 1)
        self.assertTrue(is_micron_detail_url(HOST + PATH))
        for value in (PATH, HOST + ":443" + PATH, HOST + PATH + "?x=1",
                      HOST.replace("https", "http") + PATH, None):
            self.assertFalse(is_micron_detail_url(value))

    def test_list_identity_shape_and_lengths_fail_before_metadata(self):
        cases = []
        for field, value in (("PressReleaseId", True), ("PressReleaseId", "4945"),
                             ("PressReleaseId", 0), ("RevisionNumber", False),
                             ("RevisionNumber", -1), ("Headline", "  "),
                             ("Headline", "a" * 301), ("ShortDescription", "b" * 801),
                             ("ShortDescription", 3), ("PressReleaseDate", None)):
            row = record()
            row[field] = value
            cases.append([row])
        cases.append([record(), record()])
        for rows in cases:
            with self.subTest(rows=rows):
                fetch = FixtureFetch(rows)
                with self.assertRaises(MicronIrJsonError):
                    MicronIrJsonClient(fetch_bytes=fetch, clock=lambda: NOW).read_recent()
                self.assertEqual(len(fetch.calls), 1)

    def test_ids_fit_exact_json_integer_identity_range(self):
        for field in ("PressReleaseId", "RevisionNumber"):
            row = record()
            row[field] = 1 << 53
            fetch = FixtureFetch([row])
            with self.subTest(field=field):
                with self.assertRaises(MicronIrJsonError):
                    MicronIrJsonClient(fetch_bytes=fetch, clock=lambda: NOW).read_recent()
                self.assertEqual(len(fetch.calls), 1)

    def test_every_raw_list_time_has_exact_wall_time_grammar_before_any_head(self):
        for raw_time in ("not-a-date", "8/26/2026 16:01:00", "02/30/2026 16:01:00",
                         "08/26/2026 25:01:00", "08/26/2026 16:01:00Z",
                         " 08/26/2026 16:01:00", "08/26/2026 16:01:00 "):
            row = record(4946)
            row["LinkToDetailPage"] = "/news/press-release/2026/second/default.aspx"
            row["PressReleaseDate"] = raw_time
            fetch = FixtureFetch([record(), row], {HOST + row["LinkToDetailPage"]: head(metadata(row))})
            with self.subTest(raw_time=raw_time):
                with self.assertRaises(MicronIrJsonError):
                    MicronIrJsonClient(fetch_bytes=fetch, clock=lambda: NOW).read_recent()
                self.assertEqual(len(fetch.calls), 1)

    def test_fractional_official_times_have_consumer_canonical_form(self):
        value = metadata(datePublished="2026-08-26T15:01:00.100000Z",
                         dateModified="2026-08-26T15:01:00.120000Z")
        result = MicronIrJsonClient(fetch_bytes=FixtureFetch(heads={HOST + PATH: head(value)}),
                                   clock=lambda: NOW).read_recent()["releases"][0]
        self.assertEqual(result["published_at"], "2026-08-26T15:01:00.1Z")
        self.assertEqual(result["metadata_date_modified"], "2026-08-26T15:01:00.12Z")
        self.assertEqual(result["time_metadata_sha256"], micron_time_metadata_sha256(
            official_url=HOST + PATH, title=TITLE, published_at="2026-08-26T15:01:00.1Z",
            metadata_date_modified="2026-08-26T15:01:00.12Z",
        ))

    def test_timezone_object_without_offset_is_not_a_receipt_clock(self):
        class NoOffset(tzinfo):
            def utcoffset(self, _dt):
                return None
        with self.assertRaises(MicronIrJsonError):
            MicronIrJsonClient(fetch_bytes=FixtureFetch(),
                               clock=lambda: datetime(2026, 9, 5, tzinfo=NoOffset())).read_recent()

    def test_invalid_offset_minutes_and_conflicting_type_array_are_rejected(self):
        for value in (metadata(datePublished="2026-08-26T15:01:00+00:99"),
                      [metadata(), metadata(**{"@type": ["NewsArticle"]})]):
            with self.subTest(value=value):
                with self.assertRaises(MicronIrJsonError):
                    MicronIrJsonClient(fetch_bytes=FixtureFetch(heads={HOST + PATH: head(value)}),
                                       clock=lambda: NOW).read_recent()

    def test_list_cap_and_invalid_envelopes_do_not_request_metadata(self):
        for raw in (b"x" * 1_000_001, b"{}", b"[]", b'{"GetPressReleaseListResult":null}',
                    b'{"GetPressReleaseListResult":[],"GetPressReleaseListResult":[]}',
                    listing([record(index + 1) for index in range(31)])):
            calls = []
            def fetch(url, **controls):
                calls.append(url)
                return raw
            with self.subTest(size=len(raw)):
                with self.assertRaises(MicronIrJsonError):
                    MicronIrJsonClient(fetch_bytes=fetch, clock=lambda: NOW).read_recent()
                self.assertEqual(calls, [EXPECTED_LIST_URL])

    def test_exact_30_scope_gets_only_30_metadata_documents(self):
        rows, heads = [], {}
        for identity in range(30):
            row = record(identity + 1)
            row["LinkToDetailPage"] = f"/news/press-release/2026/release-{identity}/default.aspx"
            rows.append(row)
            heads[HOST + row["LinkToDetailPage"]] = head(metadata(row))
        fetch = FixtureFetch(rows, heads)
        result = MicronIrJsonClient(fetch_bytes=fetch, clock=lambda: NOW).read_recent()
        self.assertEqual(len(result["releases"]), 30)
        self.assertEqual(len(fetch.calls), 31)
        self.assertEqual([r["q4_press_release_id"] for r in result["releases"]], list(range(1, 31)))

    def test_metadata_requires_one_matching_newsarticle(self):
        for value in (
            metadata(headline="Different"), metadata(mainEntityOfPage={"@id": HOST + PATH + "?x=1"}),
            metadata(mainEntityOfPage=HOST + PATH), metadata(**{"@type": "Article"}),
            [metadata(), metadata()], {"@graph": [metadata(), metadata(headline="Conflict")]},
        ):
            with self.subTest(value=value):
                with self.assertRaises(MicronIrJsonError):
                    MicronIrJsonClient(fetch_bytes=FixtureFetch(heads={HOST + PATH: head(value)}),
                                       clock=lambda: NOW).read_recent()

    def test_unrelated_jsonld_is_allowed_but_body_metadata_is_not_used(self):
        raw = head().replace(b"</head>",
                            b'<script type="application/ld+json">{"@type":"Organization"}</script></head>')
        raw += b'<body><script type="application/ld+json">{"@type":"NewsArticle"}</script>'
        result = MicronIrJsonClient(fetch_bytes=FixtureFetch(heads={HOST + PATH: raw}),
                                   clock=lambda: NOW).read_recent()
        self.assertTrue(result["complete"])
        body_only = b'<html><head></head><body>' + head()
        with self.assertRaises(MicronIrJsonError):
            MicronIrJsonClient(fetch_bytes=FixtureFetch(heads={HOST + PATH: body_only}),
                               clock=lambda: NOW).read_recent()

    def test_naive_missing_malformed_and_true_future_times_fail(self):
        cases = [metadata(datePublished=value) for value in (
            None, "", "2026-08-26T15:01:00", "08/26/2026 15:01:00Z",
            "2026-09-05T00:00:01Z", "2026-08-26T15:01:00-00:00",
        )]
        missing = metadata()
        del missing["datePublished"]
        cases += [missing, metadata(dateModified="2026-08-26T15:01:00"),
                  metadata(dateModified="2026-09-05T00:00:01Z")]
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(MicronIrJsonError):
                    MicronIrJsonClient(fetch_bytes=FixtureFetch(heads={HOST + PATH: head(value)}),
                                       clock=lambda: NOW).read_recent()

    def test_clock_is_taken_after_metadata_response_and_offset_is_preserved_correctly(self):
        instant = [datetime(2026, 8, 26, 15, 0, tzinfo=timezone.utc)]
        fetch = FixtureFetch(heads={HOST + PATH: head(metadata(datePublished="2026-08-26T17:01:00+02:00"))})
        def advance(url, **controls):
            result = fetch(url, **controls)
            if controls["head_only"]:
                instant[0] = datetime(2026, 8, 26, 15, 2, tzinfo=timezone.utc)
            return result
        value = MicronIrJsonClient(fetch_bytes=advance, clock=lambda: instant[0]).read_recent()
        self.assertEqual(value["releases"][0]["published_at"], STAMP)

    def test_optional_modified_and_unicode_whitespace_normalization(self):
        row = record()
        row["Headline"] = "  Cafe\u0301\u00a0launch  "
        row["ShortDescription"] = "  short\n summary "
        value = metadata(row, headline="Café launch")
        del value["dateModified"]
        result = MicronIrJsonClient(fetch_bytes=FixtureFetch([row], {HOST + PATH: head(value)}),
                                   clock=lambda: NOW).read_recent()["releases"][0]
        self.assertEqual(result["title"], "Café launch")
        self.assertEqual(result["summary"], "short summary")
        self.assertEqual(result["metadata_date_modified"], "")

    def test_truncated_head_overcap_invalid_utf8_and_jsonld_fail(self):
        for raw in (head()[:-7], b"<head>" + b"a" * (128 * 1024) + b"</head>",
                    b"<head>\xff</head>", b'<head><script type="application/ld+json">nope</script></head>',
                    b'<head><script type="application/ld+json">{"@type":"NewsArticle","@type":"NewsArticle"}</script></head>'):
            with self.subTest(size=len(raw)):
                with self.assertRaises(MicronIrJsonError):
                    MicronIrJsonClient(fetch_bytes=FixtureFetch(heads={HOST + PATH: raw}),
                                       clock=lambda: NOW).read_recent()

    def test_cancellation_and_deadline_before_fetch_and_after_response(self):
        cancelled = threading.Event()
        cancelled.set()
        fetch = FixtureFetch()
        client = MicronIrJsonClient(fetch_bytes=fetch, clock=lambda: NOW)
        with self.assertRaises(SourcePollCancelled):
            client.read_recent(cancel_event=cancelled)
        with self.assertRaises(SourcePollDeadlineExceeded):
            client.read_recent(deadline_monotonic_ms=max(1, int(time.monotonic() * 1000) - 1))
        self.assertEqual(fetch.calls, [])
        event = threading.Event()
        def late(url, **controls):
            value = fetch(url, **controls)
            event.set()
            return value
        with self.assertRaises(SourcePollCancelled):
            MicronIrJsonClient(fetch_bytes=late).read_recent(cancel_event=event)
        self.assertEqual(len(fetch.calls), 1)

    def test_metadata_failure_returns_no_partial_snapshot(self):
        other = record(4946)
        other["LinkToDetailPage"] = "/news/press-release/2026/second/default.aspx"
        fetch = FixtureFetch([record(), other], {HOST + other["LinkToDetailPage"]: head(metadata(other, datePublished="bad"))})
        with self.assertRaises(MicronIrJsonError):
            MicronIrJsonClient(fetch_bytes=fetch, clock=lambda: NOW).read_recent()
        fetch.heads[HOST + other["LinkToDetailPage"]] = head(metadata(other))
        result = MicronIrJsonClient(fetch_bytes=fetch, clock=lambda: NOW).read_recent()
        self.assertEqual(len(result["releases"]), 2)

    def test_default_transport_uses_fixed_redirect_validator_and_bounded_head_read(self):
        class Response(io.BytesIO):
            headers = {}
            def __init__(self, value):
                super().__init__(value)
                self.read_sizes = []
                self.bytes_read = 0
            def read1(self, size):
                self.read_sizes.append(size)
                raw = super().read(size)
                self.bytes_read += len(raw)
                return raw
        responses = [Response(listing([record()])), Response(head() + b"<body>" + b"x" * 500_000)]
        calls = []
        def opener(request, **controls):
            index = len(calls)
            calls.append((request, controls))
            validator = controls["url_validator"]
            self.assertTrue(validator(request.full_url))
            self.assertFalse(validator(request.full_url + "?other=1"))
            self.assertFalse(validator(HOST + "/news/default.aspx"))
            self.assertEqual(request.get_method(), "GET")
            return responses[index]
        with patch("backend.market.micron_ir_json.open_official_https", side_effect=opener):
            value = MicronIrJsonClient(clock=lambda: NOW).read_recent()
        self.assertTrue(value["complete"])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(response.closed for response in responses))
        self.assertLess(responses[1].bytes_read, 128 * 1024)
        self.assertTrue(all(size <= 4096 for size in responses[1].read_sizes))

    def test_default_head_reader_honors_cancellation_while_blocked(self):
        event = threading.Event()
        closed = threading.Event()
        class BlockingResponse:
            headers = {}
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                self.close()
            def close(self):
                closed.set()
            def read(self, _size):
                event.set()
                if not closed.wait(1):
                    raise AssertionError("cancel watcher did not close blocked metadata response")
                raise OSError("closed")
        calls = []
        def opener(request, **controls):
            calls.append(request.full_url)
            if len(calls) == 1:
                response = io.BytesIO(listing([record()]))
                response.headers = {}
                return response
            return BlockingResponse()
        with patch("backend.market.micron_ir_json.open_official_https", side_effect=opener):
            with self.assertRaises(SourcePollCancelled):
                MicronIrJsonClient(clock=lambda: NOW).read_recent(cancel_event=event)
        self.assertTrue(closed.is_set())

    def test_default_head_reader_honors_absolute_deadline_while_blocked(self):
        closed = threading.Event()
        class BlockingResponse:
            headers = {}
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                self.close()
            def close(self):
                closed.set()
            def read(self, _size):
                if not closed.wait(1):
                    raise AssertionError("deadline watcher did not close metadata response")
                raise OSError("closed")
        responses = [io.BytesIO(listing([record()])), BlockingResponse()]
        responses[0].headers = {}
        with patch("backend.market.micron_ir_json.open_official_https", side_effect=responses):
            with self.assertRaises(SourcePollDeadlineExceeded):
                MicronIrJsonClient(clock=lambda: NOW).read_recent(
                    deadline_monotonic_ms=int(time.monotonic() * 1000) + 100,
                )
        self.assertTrue(closed.is_set())

    def test_default_head_reader_rejects_truncation_and_overcap_and_accepts_case(self):
        for raw, valid in ((b"<head>truncated", False),
                           (b"<head>" + b"a" * (128 * 1024) + b"</head>", False),
                           (head().replace(b"</head>", b"</HEAD>"), True)):
            with self.subTest(valid=valid, size=len(raw)):
                responses = [io.BytesIO(listing([record()])), io.BytesIO(raw)]
                for response in responses:
                    response.headers = {}
                with patch("backend.market.micron_ir_json.open_official_https", side_effect=responses):
                    if valid:
                        self.assertTrue(MicronIrJsonClient(clock=lambda: NOW).read_recent()["complete"])
                    else:
                        with self.assertRaises(MicronIrJsonError):
                            MicronIrJsonClient(clock=lambda: NOW).read_recent()
                self.assertTrue(all(response.closed for response in responses))

    def test_explicit_metadata_concurrency_remains_bounded_to_two(self):
        rows = [record(index + 1) for index in range(4)]
        for index, row in enumerate(rows):
            row["LinkToDetailPage"] = f"/news/press-release/2026/item-{index}/default.aspx"
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        active, peak = [0], [0]
        def fetch(url, **controls):
            if not controls["head_only"]:
                return listing(rows)
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            try:
                barrier.wait(timeout=1)
                row = next(row for row in rows if HOST + row["LinkToDetailPage"] == url)
                return head(metadata(row))
            finally:
                with lock:
                    active[0] -= 1
        result = MicronIrJsonClient(fetch_bytes=fetch, clock=lambda: NOW, max_workers=2).read_recent()
        self.assertEqual(len(result["releases"]), 4)
        self.assertEqual(peak[0], 2)
        self.assertEqual(active[0], 0)

    def test_default_metadata_concurrency_is_four_with_readonly_config(self):
        rows = [record(index + 1) for index in range(8)]
        for index, row in enumerate(rows):
            row["LinkToDetailPage"] = f"/news/press-release/2026/four-way-{index}/default.aspx"
        barrier = threading.Barrier(4)
        lock = threading.Lock()
        active, peak, calls = [0], [0], []
        def fetch(url, **controls):
            with lock:
                calls.append(url)
            if not controls["head_only"]:
                return listing(rows)
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            try:
                barrier.wait(timeout=1)
                row = next(row for row in rows if HOST + row["LinkToDetailPage"] == url)
                return head(metadata(row))
            finally:
                with lock:
                    active[0] -= 1
        client = MicronIrJsonClient(fetch_bytes=fetch, clock=lambda: NOW)
        result = client.read_recent()
        self.assertEqual(len(result["releases"]), 8)
        self.assertEqual(len(calls), 9)
        self.assertEqual(peak[0], 4)
        self.assertEqual(active[0], 0)
        self.assertEqual(client.max_workers, 4)
        with self.assertRaises(AttributeError):
            client.max_workers = 1
        with self.assertRaises(ValueError):
            MicronIrJsonClient(max_workers=5)

    def test_four_head_cancellation_and_shared_deadline_close_all_workers(self):
        for cancelled in (True, False):
            with self.subTest(cancelled=cancelled):
                rows = [record(index + 1) for index in range(8)]
                for index, row in enumerate(rows):
                    row["LinkToDetailPage"] = f"/news/press-release/2026/blocked-{index}/default.aspx"
                event = threading.Event()
                barrier = threading.Barrier(4)
                closed = [threading.Event() for _ in range(4)]
                calls = []
                lock = threading.Lock()
                class BlockingResponse:
                    headers = {}
                    def __init__(self, index):
                        self.index = index
                    def __enter__(self):
                        return self
                    def __exit__(self, *_args):
                        self.close()
                    def close(self):
                        closed[self.index].set()
                    def read(self, _size):
                        barrier.wait(timeout=1)
                        if cancelled:
                            event.set()
                        if not closed[self.index].wait(1):
                            raise AssertionError("poll control did not close every active head")
                        raise OSError("closed")
                def opener(request, **controls):
                    with lock:
                        index = len(calls)
                        calls.append(request.full_url)
                    if index == 0:
                        response = io.BytesIO(listing(rows))
                        response.headers = {}
                        return response
                    if index > 4:
                        raise AssertionError("failed first batch must not schedule later head requests")
                    return BlockingResponse(index - 1)
                client = MicronIrJsonClient(clock=lambda: NOW, max_workers=4)
                with patch("backend.market.micron_ir_json.open_official_https", side_effect=opener):
                    error = SourcePollCancelled if cancelled else SourcePollDeadlineExceeded
                    with self.assertRaises(error):
                        client.read_recent(cancel_event=event, deadline_monotonic_ms=(
                            int(time.monotonic() * 1000) + (2_000 if cancelled else 200)
                        ))
                self.assertEqual(len(calls), 5)
                self.assertTrue(all(item.is_set() for item in closed))
                self.assertFalse(any(thread.name.startswith("micron-ir-head") or
                                     thread.name == "official-source-body-control"
                                     for thread in threading.enumerate()))


class MicronIrIncrementalTests(unittest.TestCase):
    @staticmethod
    def fixture(count=30):
        rows, heads = [], {}
        for identity in range(1, count + 1):
            row = record(identity)
            row["LinkToDetailPage"] = f"/news/press-release/2026/cached-{identity}/default.aspx"
            rows.append(row)
            heads[HOST + row["LinkToDetailPage"]] = head(metadata(row))
        return FixtureFetch(rows, heads)

    def test_repeated_cycle_drops_from_thirty_one_to_five_requests_and_rotates_every_identity(self):
        fetch = self.fixture()
        client = MicronIrJsonClient(fetch_bytes=fetch, clock=lambda: NOW)
        initial = client.read_recent()
        self.assertEqual(len(fetch.calls), 31)
        reviewed = set()
        for _ in range(8):
            before = len(fetch.calls)
            repeated = client.read_recent(require_complete=False)
            self.assertEqual(len(fetch.calls) - before, 5)
            self.assertTrue(repeated["complete"])
            self.assertEqual(repeated["releases"], initial["releases"])
            reviewed.update(repeated["metadata_progress"]["requested_ids"])
        self.assertEqual(reviewed, set(range(1, 31)))

    def test_new_identity_is_fetched_before_failed_old_revalidation_and_remains_deliverable(self):
        fetch = self.fixture()
        client = MicronIrJsonClient(fetch_bytes=fetch, clock=lambda: NOW)
        client.read_recent()
        new_row = record(31)
        new_row["LinkToDetailPage"] = "/news/press-release/2026/new-31/default.aspx"
        fetch.rows = fetch.rows[:-1] + [new_row]
        fetch.heads[HOST + new_row["LinkToDetailPage"]] = head(metadata(new_row))
        failed_url = HOST + fetch.rows[0]["LinkToDetailPage"]
        fetch.heads[failed_url] = head(metadata(fetch.rows[0], datePublished="invalid"))
        before = len(fetch.calls)
        result = client.read_recent(require_complete=False)
        self.assertEqual(fetch.calls[before + 1][0], HOST + new_row["LinkToDetailPage"])
        self.assertFalse(result["complete"])
        self.assertIn(31, {row["q4_press_release_id"] for row in result["releases"]})
        self.assertNotIn(1, {row["q4_press_release_id"] for row in result["releases"]})
        self.assertEqual(result["metadata_progress"]["failed"][0]["press_release_id"], 1)
        self.assertEqual(result["metadata_progress"]["failed"][0]["attempt_count"], 2)

    def test_strict_initial_read_revalidates_all_cached_heads_and_keeps_failure_closed(self):
        fetch = self.fixture()
        client = MicronIrJsonClient(fetch_bytes=fetch, clock=lambda: NOW)
        client.read_recent()
        self.assertTrue(client.read_recent(require_complete=False)["complete"])
        before = len(fetch.calls)
        client.read_recent(require_complete=True)
        self.assertEqual(len(fetch.calls) - before, 31)
        row = fetch.rows[-1]
        fetch.heads[HOST + row["LinkToDetailPage"]] = head(metadata(row, datePublished="invalid"))
        with self.assertRaises(MicronIrJsonError):
            client.read_recent(require_complete=True)

    def test_corrupt_or_missing_cache_and_changed_list_binding_require_new_metadata(self):
        for change in ("corrupt", "missing", "revision", "summary", "url"):
            with self.subTest(change=change):
                fetch = self.fixture()
                client = MicronIrJsonClient(fetch_bytes=fetch, clock=lambda: NOW)
                client.read_recent()
                row = fetch.rows[10]
                if change == "corrupt":
                    client._metadata_cache[11]["time_metadata_sha256"] = "0" * 64
                elif change == "missing":
                    client._metadata_cache.pop(11)
                elif change == "revision":
                    row["RevisionNumber"] += 1
                elif change == "summary":
                    row["ShortDescription"] = "Updated official metadata."
                else:
                    row["LinkToDetailPage"] = "/news/press-release/2026/changed-11/default.aspx"
                    fetch.heads[HOST + row["LinkToDetailPage"]] = head(metadata(row))
                before = len(fetch.calls)
                result = client.read_recent(require_complete=False)
                self.assertEqual(fetch.calls[before + 1][0], HOST + row["LinkToDetailPage"])
                self.assertEqual(len(fetch.calls) - before, 6)
                self.assertTrue(result["complete"])

    def test_expired_metadata_is_withheld_until_bounded_rotation_revalidates_it(self):
        elapsed = [0.0]
        fetch = self.fixture()
        client = MicronIrJsonClient(fetch_bytes=fetch, clock=lambda: NOW, monotonic=lambda: elapsed[0])
        client.read_recent()
        elapsed[0] += client.cache_policy["maximum_age_ms"] / 1000 + 1
        before = len(fetch.calls)
        result = client.read_recent(require_complete=False)
        self.assertEqual(len(fetch.calls) - before, 5)
        self.assertFalse(result["complete"])
        self.assertEqual(len(result["releases"]), 4)
        self.assertEqual(len(result["metadata_progress"]["failed"]), 26)
        self.assertEqual({error["code"] for error in result["source_errors"]}, {"MICRON_IR_METADATA_CACHE_EXPIRED"})
        for _ in range(7):
            result = client.read_recent(require_complete=False)
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["releases"]), 30)

    def test_parser_policy_change_invalidates_cache_and_restart_is_cold(self):
        fetch = self.fixture()
        client = MicronIrJsonClient(fetch_bytes=fetch, clock=lambda: NOW)
        client.read_recent()
        policy = client.cache_policy
        policy["parser_version"] = "different_parser_fixture"
        before = len(fetch.calls)
        with patch("backend.market.micron_ir_json.micron_metadata_cache_policy", return_value=policy):
            self.assertTrue(client.read_recent(require_complete=False)["complete"])
        self.assertEqual(len(fetch.calls) - before, 31)
        before = len(fetch.calls)
        restarted = MicronIrJsonClient(fetch_bytes=fetch, clock=lambda: NOW)
        self.assertTrue(restarted.read_recent(require_complete=False)["complete"])
        self.assertEqual(len(fetch.calls) - before, 31)

    def test_old_revalidation_timeout_closes_response_and_keeps_new_metadata_before_global_deadline(self):
        fetch = self.fixture()
        block_old = [False]
        closed = threading.Event()

        class BlockedHead:
            headers = {}
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                self.close()
            def close(self):
                closed.set()
            def read(self, _size):
                if not closed.wait(1):
                    raise AssertionError("old revalidation did not honor its short deadline")
                raise OSError("closed by bounded metadata deadline")

        old_url = HOST + fetch.rows[0]["LinkToDetailPage"]
        dispatched = []
        def opener(request, **_controls):
            dispatched.append(request.full_url)
            if block_old[0] and request.full_url == old_url:
                return BlockedHead()
            raw = listing(fetch.rows) if request.full_url == EXPECTED_LIST_URL else fetch.heads[request.full_url]
            response = io.BytesIO(raw)
            response.headers = {}
            return response

        with patch("backend.market.micron_ir_json.MICRON_METADATA_REVALIDATION_TIMEOUT_MS", 80), \
             patch("backend.market.micron_ir_json.open_official_https", side_effect=opener):
            client = MicronIrJsonClient(clock=lambda: NOW)
            client.read_recent()
            new_row = record(31)
            new_row["LinkToDetailPage"] = "/news/press-release/2026/new-before-timeout/default.aspx"
            fetch.rows = fetch.rows[:-1] + [new_row]
            fetch.heads[HOST + new_row["LinkToDetailPage"]] = head(metadata(new_row))
            block_old[0] = True
            before = len(dispatched)
            started = time.monotonic()
            result = client.read_recent(require_complete=False, deadline_monotonic_ms=int(started * 1000) + 2_000)
            self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(closed.is_set())
        self.assertEqual(dispatched[before + 1], HOST + new_row["LinkToDetailPage"])
        self.assertIn(31, {row["q4_press_release_id"] for row in result["releases"]})
        self.assertFalse(result["complete"])
        self.assertIn("MICRON_IR_REVALIDATION_TIMEOUT", {error["code"] for error in result["source_errors"]})
        self.assertFalse(any(thread.name.startswith("micron-ir-head") or thread.name == "official-source-body-control"
                             for thread in threading.enumerate()))

    def test_old_revalidation_is_deferred_when_the_remaining_budget_belongs_to_commit(self):
        fetch = self.fixture()
        with patch("backend.market.micron_ir_json.MICRON_METADATA_COMMIT_RESERVE_MS", 2_000):
            client = MicronIrJsonClient(fetch_bytes=fetch, clock=lambda: NOW)
            client.read_recent()
            row = record(31)
            row["LinkToDetailPage"] = "/news/press-release/2026/new-before-reserve/default.aspx"
            fetch.rows = fetch.rows[:-1] + [row]
            fetch.heads[HOST + row["LinkToDetailPage"]] = head(metadata(row))
            before = len(fetch.calls)
            result = client.read_recent(require_complete=False,
                                        deadline_monotonic_ms=int(time.monotonic() * 1000) + 2_000)
        self.assertEqual(len(fetch.calls) - before, 2)
        self.assertIn(31, {release["q4_press_release_id"] for release in result["releases"]})
        self.assertFalse(result["complete"])
        self.assertEqual({error["code"] for error in result["source_errors"]}, {"MICRON_IR_REVALIDATION_DEFERRED"})
        self.assertEqual(result["metadata_progress"]["next_revalidation_ids"], [1, 2, 3, 4])

    def test_cancellation_during_cached_revalidation_returns_no_partial_snapshot(self):
        fetch = self.fixture()
        event = threading.Event()
        cancel_old = [False]
        def controlled(url, **controls):
            value = fetch(url, **controls)
            if cancel_old[0] and controls["head_only"]:
                event.set()
            return value
        client = MicronIrJsonClient(fetch_bytes=controlled, clock=lambda: NOW)
        client.read_recent()
        cancel_old[0] = True
        with self.assertRaises(SourcePollCancelled):
            client.read_recent(require_complete=False, cancel_event=event)
        self.assertFalse(any(thread.name.startswith("micron-ir-head") for thread in threading.enumerate()))


if __name__ == "__main__":
    unittest.main()
