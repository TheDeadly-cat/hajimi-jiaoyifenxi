from __future__ import annotations

import ast
import http.client
import os
import socket
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.market.official_macro import (
    BLS_RELEASE_CALENDAR_URL,
    BLS_SERIES_IDS,
    BLS_SERIES_URLS,
    DEFAULT_OFFICIAL_MACRO_FETCH_BYTES,
    FEDERAL_RESERVE_FOMC_CALENDAR_URL,
    FEDERAL_RESERVE_MONETARY_RSS_URL,
    OFFICIAL_MACRO_MAX_RESPONSE_BYTES,
    OFFICIAL_MACRO_RESPONSE_BODY_DEADLINE_SECONDS,
    OFFICIAL_MACRO_TRANSPORT_IDENTITY,
    OfficialMacroSourceClient,
    TREASURY_DEBT_TO_PENNY_URL,
    TREASURY_RELEASE_CALENDAR_URL,
    _read_official_response_body,
)
from backend.source_monitoring.default_registry import build_official_source_registry
from backend.source_monitoring.adapters.macro_official import (
    OfficialMacroCalendarSourceAdapter,
)


FIXED_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "official_macro"


FIXTURE_NAMES = {
    FEDERAL_RESERVE_MONETARY_RSS_URL: "fed_press_monetary.xml",
    FEDERAL_RESERVE_FOMC_CALENDAR_URL: "fed_fomc_calendar.html",
    BLS_SERIES_URLS["CUSR0000SA0"]: "bls_cpi.json",
    BLS_SERIES_URLS["LNS14000000"]: "bls_unemployment.json",
    BLS_SERIES_URLS["CES0000000001"]: "bls_payrolls.json",
    BLS_RELEASE_CALENDAR_URL: "bls_calendar.ics",
    TREASURY_DEBT_TO_PENNY_URL: "treasury_debt_to_penny.json",
    TREASURY_RELEASE_CALENDAR_URL: "treasury_release_calendar.json",
}


class FixtureFetcher:
    def __init__(self, *, fail_url: str = "") -> None:
        self.fail_url = fail_url
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if url == self.fail_url:
            raise ValueError("fixture source unavailable")
        name = FIXTURE_NAMES.get(url)
        if name is None:
            raise AssertionError(f"unexpected endpoint: {url}")
        return (FIXTURE_ROOT / name).read_bytes()


class FakeHeaders:
    def __init__(self, content_type: str, content_length: str | None = None) -> None:
        self._values = {"Content-Type": content_type}
        if content_length is not None:
            self._values["Content-Length"] = content_length

    def get(self, key: str):
        return self._values.get(key)


class FakeSocket:
    def __init__(self) -> None:
        self.real_close_calls = 0
        self.shutdown_calls = 0
        self.shutdown_event = threading.Event()

    def shutdown(self, _how: int) -> None:
        self.shutdown_calls += 1
        self.shutdown_event.set()

    def _real_close(self) -> None:
        self.real_close_calls += 1
        self.shutdown_event.set()


class FakeRaw:
    def __init__(self, sock: FakeSocket) -> None:
        self._sock = sock


class FakeFile:
    def __init__(self, sock: FakeSocket) -> None:
        self.raw = FakeRaw(sock)


class FakeResponse:
    def __init__(self, body: bytes, content_type: str) -> None:
        self.body = body
        self.headers = FakeHeaders(content_type, str(len(body)))
        self.read_limit = 0
        self.socket = FakeSocket()
        self.fp = FakeFile(self.socket)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int) -> bytes:
        self.read_limit = limit
        return self.body[:limit]


class BlockingResponse(FakeResponse):
    def read(self, limit: int) -> bytes:
        self.read_limit = limit
        if not self.socket.shutdown_event.wait(timeout=1.0):
            raise AssertionError("deadline watchdog did not interrupt the body read")
        raise OSError("simulated socket shutdown")


class OfficialMacroParserTests(unittest.TestCase):
    def _client(self, fetcher: FixtureFetcher | None = None) -> OfficialMacroSourceClient:
        return OfficialMacroSourceClient(
            fetch_bytes=fetcher or FixtureFetcher(),
            clock=lambda: FIXED_NOW,
        )

    def test_release_fixtures_project_fixed_complete_windows(self) -> None:
        fetcher = FixtureFetcher()
        client = self._client(fetcher)

        fed = client.federal_reserve_releases(limit=50)
        bls = client.bls_releases(limit=12)
        treasury = client.treasury_releases(limit=10)

        self.assertEqual(fed["source_errors"], [])
        self.assertEqual(len(fed["rows"]), 1)
        self.assertEqual(fed["rows"][0]["authority"], "federal_reserve")
        self.assertEqual(fed["rows"][0]["released_at"], "2026-07-29T18:00:00Z")
        self.assertEqual(fed["rows"][0]["data"], {})

        self.assertEqual(bls["source_errors"], [])
        self.assertEqual(len(bls["rows"]), 12)
        self.assertEqual(
            {row["data"]["series_id"] for row in bls["rows"]},
            set(BLS_SERIES_IDS),
        )
        self.assertEqual(
            {row["data"]["period"] for row in bls["rows"]},
            {"M05", "M06", "M07", "M08"},
        )
        preliminary = [
            row for row in bls["rows"] if row["data"]["footnotes"]
        ]
        self.assertTrue(preliminary)
        self.assertTrue(all(not row["official_revision"] for row in preliminary))

        self.assertEqual(treasury["source_errors"], [])
        self.assertEqual(len(treasury["rows"]), 2)
        self.assertEqual(treasury["rows"][0]["reference_period"], "2026-08-28")
        self.assertEqual(
            treasury["rows"][0]["data"]["tot_pub_debt_out_amt"],
            "37000000000000.00",
        )
        self.assertEqual(
            fetcher.calls,
            [
                FEDERAL_RESERVE_MONETARY_RSS_URL,
                *(BLS_SERIES_URLS[series_id] for series_id in BLS_SERIES_IDS),
                TREASURY_DEBT_TO_PENNY_URL,
            ],
        )

    def test_three_authority_calendar_is_atomic_and_fomc_is_date_only(self) -> None:
        fetcher = FixtureFetcher()
        result = self._client(fetcher).calendar_events(limit=50)

        self.assertEqual(result["source_errors"], [])
        self.assertEqual(
            {row["authority"] for row in result["rows"]},
            {"federal_reserve", "bls", "treasury"},
        )
        fomc = next(
            row for row in result["rows"] if row["authority"] == "federal_reserve"
        )
        self.assertEqual(fomc["scheduled_at"], "")
        self.assertEqual(
            fomc["data"],
            {
                "scheduled_date_start": "2026-09-15",
                "scheduled_date_end": "2026-09-16",
                "time_precision": "date",
            },
        )
        bls = next(row for row in result["rows"] if row["authority"] == "bls")
        self.assertEqual(bls["scheduled_at"], "2026-09-11T12:30:00Z")
        treasury = next(
            row for row in result["rows"] if row["authority"] == "treasury"
        )
        self.assertEqual(treasury["scheduled_at"], "2026-09-01T20:00:00Z")

        failed_fetcher = FixtureFetcher(fail_url=BLS_RELEASE_CALENDAR_URL)
        failed = self._client(failed_fetcher).calendar_events(limit=50)
        self.assertEqual(failed["rows"], [])
        self.assertTrue(failed["source_errors"])
        self.assertIn("bls", failed["source_errors"][0]["scope"])

        adapter_result = OfficialMacroCalendarSourceAdapter(
            client=self._client(FixtureFetcher())
        ).poll({}, observed_at_ms=int(FIXED_NOW.timestamp() * 1_000))
        self.assertEqual(adapter_result.source_errors, ())
        self.assertEqual(len(adapter_result.observed_items), 3)

    def test_bls_calendar_rejects_non_eastern_timezone_atomically(self) -> None:
        class UnapprovedTimezoneFetcher(FixtureFetcher):
            def __call__(self, url: str) -> bytes:
                raw = super().__call__(url)
                if url == BLS_RELEASE_CALENDAR_URL:
                    return raw.replace(
                        b"TZID=America/New_York",
                        b"TZID=Pacific/Kiritimati",
                    )
                return raw

        result = self._client(UnapprovedTimezoneFetcher()).calendar_events(limit=50)

        self.assertEqual(result["rows"], [])
        self.assertEqual(len(result["source_errors"]), 1)
        self.assertEqual(
            result["source_errors"][0]["scope"],
            "official_macro_calendar:bls",
        )
        self.assertEqual(
            result["source_errors"][0]["code"],
            "OFFICIAL_MACRO_CALENDAR_PARSE_ERROR",
        )
        self.assertIsNone(
            OfficialMacroSourceClient._ics_datetime((
                {"TZID": "Pacific/Kiritimati", "VALUE": "DATE"},
                "20260911",
            ))
        )
        self.assertIsNone(
            OfficialMacroSourceClient._ics_datetime((
                {"TZID": "Pacific/Kiritimati"},
                "20260911T123000Z",
            ))
        )

    def test_bls_calendar_rejects_duplicate_timezone_parameter_atomically(self) -> None:
        class DuplicateTimezoneFetcher(FixtureFetcher):
            def __call__(self, url: str) -> bytes:
                raw = super().__call__(url)
                if url == BLS_RELEASE_CALENDAR_URL:
                    return raw.replace(
                        b"TZID=America/New_York",
                        b"TZID=Pacific/Kiritimati;TZID=America/New_York",
                    )
                return raw

        result = self._client(DuplicateTimezoneFetcher()).calendar_events(limit=50)

        self.assertEqual(result["rows"], [])
        self.assertEqual(len(result["source_errors"]), 1)
        self.assertEqual(
            result["source_errors"][0]["scope"],
            "official_macro_calendar:bls",
        )

    def test_access_denied_html_invalid_json_and_capacity_fail_closed(self) -> None:
        denied = b"<html><title>Access Denied</title></html>"

        class DeniedFetcher(FixtureFetcher):
            def __call__(self, url: str) -> bytes:
                self.calls.append(url)
                if url in set(BLS_SERIES_URLS.values()):
                    return denied
                return super().__call__(url)

        denied_result = self._client(DeniedFetcher()).bls_releases(limit=12)
        self.assertEqual(denied_result["rows"], [])
        self.assertEqual(len(denied_result["source_errors"]), 3)

        xml = (FIXTURE_ROOT / "fed_press_monetary.xml").read_text(encoding="utf-8")
        item = xml[xml.index("    <item>"):xml.index("    </item>") + len("    </item>")]
        flooded = xml.replace(item, item * 51)

        def flood_fetcher(url: str) -> bytes:
            if url != FEDERAL_RESERVE_MONETARY_RSS_URL:
                raise AssertionError(url)
            return flooded.encode("utf-8")

        capacity = OfficialMacroSourceClient(
            fetch_bytes=flood_fetcher,
            clock=lambda: FIXED_NOW,
        ).federal_reserve_releases(limit=50)
        self.assertEqual(capacity["rows"], [])
        self.assertEqual(
            capacity["source_errors"][0]["code"],
            "FEDERAL_RESERVE_CAPACITY_EXCEEDED",
        )

    def test_default_transport_is_class_bound_get_only_and_mime_bounded(self) -> None:
        client = OfficialMacroSourceClient(clock=lambda: FIXED_NOW)
        self.assertIs(client._fetch_bytes, DEFAULT_OFFICIAL_MACRO_FETCH_BYTES)
        self.assertEqual(client.transport_identity, OFFICIAL_MACRO_TRANSPORT_IDENTITY)
        self.assertEqual(client.source_manifest["method"], "GET")
        self.assertEqual(
            client.source_manifest["response_body_deadline_seconds"],
            OFFICIAL_MACRO_RESPONSE_BODY_DEADLINE_SECONDS,
        )

        response = FakeResponse(
            (FIXTURE_ROOT / "fed_press_monetary.xml").read_bytes(),
            "application/rss+xml; charset=utf-8",
        )
        with patch(
            "backend.market.official_macro.open_official_https",
            return_value=response,
        ) as opener:
            raw = DEFAULT_OFFICIAL_MACRO_FETCH_BYTES(
                FEDERAL_RESERVE_MONETARY_RSS_URL
            )
        self.assertTrue(raw.startswith(b"<?xml"))
        request = opener.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(
            response.read_limit,
            OFFICIAL_MACRO_MAX_RESPONSE_BYTES[FEDERAL_RESERVE_MONETARY_RSS_URL] + 1,
        )
        self.assertEqual(response.socket.shutdown_calls, 0)

        wrong_mime = FakeResponse(b"<html>Access Denied</html>", "text/html")
        with patch(
            "backend.market.official_macro.open_official_https",
            return_value=wrong_mime,
        ):
            with self.assertRaisesRegex(ValueError, "Content-Type"):
                DEFAULT_OFFICIAL_MACRO_FETCH_BYTES(
                    FEDERAL_RESERVE_MONETARY_RSS_URL
                )

    def test_default_transport_enforces_absolute_body_deadline(self) -> None:
        response = BlockingResponse(b"never returned", "application/rss+xml")
        with self.assertRaisesRegex(TimeoutError, "sealed deadline"):
            _read_official_response_body(
                response,
                OFFICIAL_MACRO_MAX_RESPONSE_BYTES[
                    FEDERAL_RESERVE_MONETARY_RSS_URL
                ],
                deadline_seconds=0.02,
            )
        self.assertEqual(
            response.read_limit,
            OFFICIAL_MACRO_MAX_RESPONSE_BYTES[FEDERAL_RESERVE_MONETARY_RSS_URL] + 1,
        )
        self.assertEqual(response.socket.shutdown_calls, 1)
        self.assertEqual(response.socket.real_close_calls, 1)

    def test_body_deadline_interrupts_real_partial_chunk_header(self) -> None:
        client_socket, server_socket = socket.socketpair()
        self.addCleanup(server_socket.close)
        self.addCleanup(client_socket.close)
        server_socket.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/rss+xml\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            b"4"
        )
        response = http.client.HTTPResponse(client_socket)
        response.begin()
        started = time.monotonic()

        with self.assertRaisesRegex(TimeoutError, "sealed deadline"):
            _read_official_response_body(
                response,
                512_000,
                deadline_seconds=0.05,
            )

        self.assertLess(time.monotonic() - started, 0.75)

    def test_client_construction_does_not_start_deadline_watchdog(self) -> None:
        with patch("backend.market.official_macro.threading.Timer") as timer:
            client = OfficialMacroSourceClient(clock=lambda: FIXED_NOW)
        self.assertIs(client._fetch_bytes, DEFAULT_OFFICIAL_MACRO_FETCH_BYTES)
        timer.assert_not_called()

    def test_default_registry_rejects_preconstruction_global_transport_swap(self) -> None:
        with patch(
            "backend.market.official_macro._fetch_official_macro_bytes",
            new=lambda _url: b"forged",
        ):
            registry = build_official_source_registry()
        client = registry.require("federal_reserve")._client
        self.assertIs(client._fetch_bytes, DEFAULT_OFFICIAL_MACRO_FETCH_BYTES)

    def test_phase3_modules_have_no_out_of_scope_runtime_imports(self) -> None:
        root = Path(__file__).resolve().parents[1]
        files = (
            root / "backend" / "market" / "official_macro.py",
            root / "backend" / "source_monitoring" / "macro_contracts.py",
            root / "backend" / "source_monitoring" / "adapters" / "macro_official.py",
        )
        forbidden_prefixes = (
            "backend.market.futu",
            "backend.provider",
            "backend.http_server",
            "frontend",
            "subprocess",
        )
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imports: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.append(node.module or "")
            with self.subTest(path=path.name):
                self.assertFalse(
                    any(
                        name == prefix or name.startswith(prefix + ".")
                        for name in imports
                        for prefix in forbidden_prefixes
                    ),
                    imports,
                )


if __name__ == "__main__":
    unittest.main()
