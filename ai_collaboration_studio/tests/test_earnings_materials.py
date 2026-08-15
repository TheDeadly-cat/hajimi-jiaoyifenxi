from __future__ import annotations

import unittest
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlparse

from backend.market.earnings_materials import OfficialEarningsMaterialsAdapter


FIXED_NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


class FakeMaterialsFetcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, url: str, _allowed_hosts: set[str]) -> bytes:
        self.calls.append(url)
        host = urlparse(url).hostname
        if host == "investors.micron.com":
            return b"""
            <main><h3>2026</h3><h4>Q3</h4>
              <a href='/static-files/mu-q3-presentation'>View Presentation</a>
              <a href='/static-files/mu-q3-remarks'>View Prepared Remarks</a>
              <a href='https://example.com/external'>View Presentation</a>
              <h4>Q2</h4><a href='/static-files/mu-q2-presentation'>View Presentation</a>
            </main>"""
        if host == "investor.sandisk.com":
            return b"""
            <main>
              <a href='/static-files/sndk-q3'>Q3FY26 Earnings Presentation</a>
              <a href='https://cdn.example.com/sndk-q2'>Q2FY26 Earnings Presentation</a>
            </main>"""
        if host == "investor.wdc.com":
            return b"""
            <main>
              <a href='/events/q3'>Western Digital Third Quarter Fiscal 2026 Earnings Call</a>
              <a href='/static-files/wdc-q3'>Third Quarter Fiscal 2026 Earnings Presentation</a>
              <a href='/events/q2'>Western Digital Second Quarter Fiscal 2026 Earnings Call</a>
              <a href='/static-files/wdc-q2'>Presentation</a>
            </main>"""
        return b"<main><p>Financial Summary Table</p></main>"


def successful_material_probe(_url: str, _allowed_hosts: set[str]) -> dict[str, object]:
    return {
        "status_code": 200,
        "content_type": "application/pdf",
        "content_length": 1024,
    }


class OfficialEarningsMaterialsAdapterTests(unittest.TestCase):
    def make_adapter(self, fetcher: FakeMaterialsFetcher) -> OfficialEarningsMaterialsAdapter:
        return OfficialEarningsMaterialsAdapter(
            fetch_bytes=fetcher,
            probe_material=successful_material_probe,
            clock=lambda: FIXED_NOW,
            cache_ttl_seconds=300,
        )

    def test_discovers_direct_official_materials_with_page_context_and_filters_external_links(self) -> None:
        fetcher = FakeMaterialsFetcher()
        payload = self.make_adapter(fetcher).recent_materials_batch(
            ["US.MU", "US.SNDK", "US.WDC", "US.STX"]
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["state"], "ready")
        rows = {row["symbol"]: row for row in payload["rows"]}
        self.assertEqual(rows["US.MU"]["material_count"], 5)
        self.assertEqual(rows["US.MU"]["materials"][0]["fiscal_period"], "FY2026-Q3")
        self.assertEqual(rows["US.MU"]["materials"][1]["material_kind"], "prepared_remarks")
        self.assertTrue(all("example.com" not in item["official_url"] for item in rows["US.MU"]["materials"]))
        self.assertEqual(rows["US.SNDK"]["materials"][0]["fiscal_period"], "FY2026-Q3")
        self.assertEqual(rows["US.WDC"]["materials"][1]["fiscal_period"], "FY2026-Q2")
        self.assertEqual(rows["US.STX"]["discovery_quality"], "curated_verified_accessible")
        self.assertEqual(
            rows["US.STX"]["materials"][0]["material_kind"],
            "earnings_release",
        )
        self.assertEqual(rows["US.STX"]["materials"][0]["fiscal_period"], "FY2026-Q4")
        self.assertIn(
            "corrected_transcript",
            [material["material_kind"] for material in rows["US.STX"]["materials"]],
        )
        self.assertTrue(rows["US.STX"]["fetchable"])
        self.assertEqual(payload["source_errors"], [])
        self.assertFalse(payload["live_trading_allowed"])

    def test_cache_prevents_refetch_and_symbol_whitelist_is_strict(self) -> None:
        fetcher = FakeMaterialsFetcher()
        adapter = self.make_adapter(fetcher)

        first = adapter.recent_materials_batch(["US.MU"])
        second = adapter.recent_materials_batch(["US.MU"])

        self.assertTrue(first["ok"])
        self.assertEqual(len(fetcher.calls), 1)
        self.assertTrue(second["rows"][0]["cache_hit"])
        with self.assertRaisesRegex(ValueError, "材料源白名单"):
            adapter.recent_materials_batch(["US.AAPL"])

    def test_four_symbol_hubs_and_curated_probes_run_concurrently_in_stable_order(self) -> None:
        fetch_barrier = threading.Barrier(4)
        fetcher = FakeMaterialsFetcher()

        def concurrent_fetch(url: str, allowed_hosts: set[str]) -> bytes:
            fetch_barrier.wait(timeout=2)
            return fetcher(url, allowed_hosts)

        adapter = OfficialEarningsMaterialsAdapter(
            fetch_bytes=concurrent_fetch,
            probe_material=successful_material_probe,
            clock=lambda: FIXED_NOW,
        )
        payload = adapter.recent_materials_batch(["US.MU", "US.SNDK", "US.WDC", "US.STX"])

        self.assertEqual(
            [row["symbol"] for row in payload["rows"]],
            ["US.MU", "US.SNDK", "US.WDC", "US.STX"],
        )
        self.assertEqual(len(fetcher.calls), 4)
        self.assertEqual(payload["state"], "ready")

        probe_barrier = threading.Barrier(4)
        probe_calls: list[str] = []

        def concurrent_probe(url: str, allowed_hosts: set[str]) -> dict[str, object]:
            probe_calls.append(url)
            probe_barrier.wait(timeout=2)
            return successful_material_probe(url, allowed_hosts)

        probe_adapter = OfficialEarningsMaterialsAdapter(
            fetch_bytes=lambda _url, _hosts: b"<main><p>Financial Summary Table</p></main>",
            probe_material=concurrent_probe,
            clock=lambda: FIXED_NOW,
        )
        probe_payload = probe_adapter.recent_materials_batch(["US.STX"])

        self.assertEqual(probe_payload["state"], "ready")
        self.assertEqual(len(probe_calls), 4)
        self.assertEqual(probe_payload["rows"][0]["material_count"], 4)

    def test_concurrent_forced_requests_share_one_fetch_and_isolate_mutation(self) -> None:
        fetcher = FakeMaterialsFetcher()
        fetch_started = threading.Event()
        release_fetch = threading.Event()

        def blocked_fetch(url: str, allowed_hosts: set[str]) -> bytes:
            fetch_started.set()
            self.assertTrue(release_fetch.wait(timeout=2))
            return fetcher(url, allowed_hosts)

        adapter = OfficialEarningsMaterialsAdapter(
            fetch_bytes=blocked_fetch,
            probe_material=successful_material_probe,
            clock=lambda: FIXED_NOW,
        )
        second_entered = threading.Event()

        def second_request() -> dict[str, object]:
            second_entered.set()
            return adapter.recent_materials_batch(["US.MU"], force=True)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(adapter.recent_materials_batch, ["US.MU"], force=True)
            self.assertTrue(fetch_started.wait(timeout=2))
            second_future = executor.submit(second_request)
            self.assertTrue(second_entered.wait(timeout=2))
            time.sleep(0.03)
            release_fetch.set()
            results = [first_future.result(), second_future.result()]

        self.assertEqual(len(fetcher.calls), 1)
        self.assertEqual(
            sorted(result["rows"][0]["singleflight_shared"] for result in results),
            [False, True],
        )
        results[0]["rows"][0]["materials"].clear()
        self.assertGreater(len(results[1]["rows"][0]["materials"]), 0)
        cached = adapter.recent_materials_batch(["US.MU"])
        self.assertGreater(len(cached["rows"][0]["materials"]), 0)
        self.assertTrue(cached["rows"][0]["cache_hit"])

    def test_lagging_waiters_reuse_one_catalog_boundary_refresh(self) -> None:
        before_boundary = datetime(2026, 9, 16, 23, 59, 50, tzinfo=timezone.utc)
        after_boundary = datetime(2026, 9, 17, 0, 0, 10, tzinfo=timezone.utc)
        fetch_calls: list[str] = []
        adapter = OfficialEarningsMaterialsAdapter(
            fetch_bytes=lambda url, _hosts: fetch_calls.append(url) or b"<main></main>",
            probe_material=successful_material_probe,
            clock=lambda: before_boundary,
            cache_ttl_seconds=900,
        )
        old_row = adapter._fetch_material_row("US.MU", 24, before_boundary)

        class StaggeredCompletedFlight:
            def __init__(self, row: dict[str, object]) -> None:
                self.row = row
                self.barrier = threading.Barrier(3)
                self.refresh_completed = threading.Event()
                self.lock = threading.Lock()
                self.leader_ident: int | None = None

            def result(self) -> dict[str, object]:
                current_ident = threading.get_ident()
                with self.lock:
                    if self.leader_ident is None:
                        self.leader_ident = current_ident
                    is_leader = self.leader_ident == current_ident
                self.barrier.wait(timeout=2)
                if not is_leader:
                    self.assert_refresh_completed()
                return self.row

            def assert_refresh_completed(self) -> None:
                if not self.refresh_completed.wait(timeout=2):
                    raise AssertionError("catalog refresh did not complete")

            @staticmethod
            def done() -> bool:
                return True

        old_flight = StaggeredCompletedFlight(old_row)
        adapter._inflight[("US.MU", 24)] = old_flight  # type: ignore[assignment]

        def lagging_waiter() -> dict[str, object]:
            try:
                return adapter._material_row(
                    "US.MU",
                    24,
                    after_boundary,
                    force=True,
                )
            finally:
                if threading.get_ident() == old_flight.leader_ident:
                    old_flight.refresh_completed.set()

        with ThreadPoolExecutor(max_workers=3) as executor:
            rows = list(executor.map(lambda _index: lagging_waiter(), range(3)))

        self.assertEqual(len(fetch_calls), 2)
        self.assertTrue(all(row["materials"] == [] for row in rows))
        self.assertTrue(all(
            row["rejected_curated_materials"][0]["access_state"] == "stale"
            for row in rows
        ))

    def test_different_limits_do_not_share_a_flight_and_unexpected_failure_cleans_up(self) -> None:
        fetch_barrier = threading.Barrier(2)
        fetcher = FakeMaterialsFetcher()

        def concurrent_fetch(url: str, allowed_hosts: set[str]) -> bytes:
            fetch_barrier.wait(timeout=2)
            return fetcher(url, allowed_hosts)

        adapter = OfficialEarningsMaterialsAdapter(
            fetch_bytes=concurrent_fetch,
            probe_material=successful_material_probe,
            clock=lambda: FIXED_NOW,
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(adapter.recent_materials_batch, ["US.MU"], limit=1, force=True),
                executor.submit(adapter.recent_materials_batch, ["US.MU"], limit=2, force=True),
            ]
            [future.result() for future in futures]
        self.assertEqual(len(fetcher.calls), 2)

        recovery_adapter = self.make_adapter(FakeMaterialsFetcher())
        original_loader = recovery_adapter._fetch_material_row
        loader_calls = 0

        def flaky_loader(symbol: str, safe_limit: int, captured_at: datetime) -> dict[str, object]:
            nonlocal loader_calls
            loader_calls += 1
            if loader_calls == 1:
                raise RuntimeError("unexpected loader defect")
            return original_loader(symbol, safe_limit, captured_at)

        recovery_adapter._fetch_material_row = flaky_loader
        with self.assertRaisesRegex(RuntimeError, "unexpected loader defect"):
            recovery_adapter.recent_materials_batch(["US.MU"], force=True)
        self.assertEqual(recovery_adapter._inflight, {})
        recovered = recovery_adapter.recent_materials_batch(["US.MU"], force=True)
        self.assertEqual(recovered["state"], "ready")
        self.assertEqual(loader_calls, 2)

    def test_default_fetch_rejects_non_whitelisted_entry(self) -> None:
        with self.assertRaisesRegex(ValueError, "非固定 HTTPS"):
            OfficialEarningsMaterialsAdapter._default_fetch_bytes(
                "https://example.com/presentations",
                {"investors.micron.com"},
            )

        with self.assertRaisesRegex(ValueError, "非白名单 HTTPS"):
            OfficialEarningsMaterialsAdapter._default_probe_material(
                "https://example.com/material.pdf",
                {"investors.micron.com"},
            )

    def test_antibot_challenge_is_reported_instead_of_treated_as_empty_page(self) -> None:
        with self.assertRaisesRegex(ValueError, "反自动化验证页"):
            OfficialEarningsMaterialsAdapter._parse_material_links(
                b"<html>Powered and protected by Akamai bm-verify</html>",
                symbol="US.MU",
                hub_url="https://investors.micron.com/quarterly-results",
                allowed_link_hosts={"investors.micron.com"},
                limit=4,
            )

    def test_cached_curated_fallback_keeps_the_original_upstream_error(self) -> None:
        calls = []

        def failing_fetch(url: str, _hosts: set[str]) -> bytes:
            calls.append(url)
            raise OSError("upstream closed connection")

        adapter = OfficialEarningsMaterialsAdapter(
            fetch_bytes=failing_fetch,
            probe_material=lambda _url, _hosts: (_ for _ in ()).throw(
                OSError("material access timed out")
            ),
            clock=lambda: FIXED_NOW,
            cache_ttl_seconds=300,
        )
        first = adapter.recent_materials_batch(["US.MU"])
        second = adapter.recent_materials_batch(["US.MU"])

        self.assertEqual(len(calls), 1)
        self.assertEqual(first["state"], "empty")
        self.assertEqual(second["state"], "empty")
        self.assertEqual(second["source_errors"][0]["code"], "EARNINGS_MATERIAL_HUB_ERROR")
        adapter.recent_materials_batch(["US.MU"], force=True)
        self.assertEqual(len(calls), 2)

    def test_curated_cache_expires_at_catalog_validity_boundary(self) -> None:
        wall_clock = [datetime(2026, 9, 16, 23, 59, 30, tzinfo=timezone.utc)]
        monotonic_clock = [100.0]
        fetch_calls: list[str] = []
        probe_calls: list[str] = []
        adapter = OfficialEarningsMaterialsAdapter(
            fetch_bytes=lambda url, _hosts: fetch_calls.append(url) or b"<main></main>",
            probe_material=lambda url, hosts: probe_calls.append(url) or successful_material_probe(url, hosts),
            clock=lambda: wall_clock[0],
            monotonic=lambda: monotonic_clock[0],
            cache_ttl_seconds=900,
        )

        first = adapter.recent_materials_batch(["US.MU"])
        self.assertEqual(first["state"], "ready")
        self.assertEqual(len(fetch_calls), 1)
        self.assertEqual(len(probe_calls), 2)

        wall_clock[0] = datetime(2026, 9, 17, 0, 0, 30, tzinfo=timezone.utc)
        monotonic_clock[0] += 60.0
        second = adapter.recent_materials_batch(["US.MU"])

        self.assertEqual(len(fetch_calls), 2)
        self.assertEqual(len(probe_calls), 2)
        self.assertEqual(second["state"], "empty")
        self.assertTrue(all(
            item["access_state"] == "stale"
            for item in second["rows"][0]["rejected_curated_materials"]
        ))
        third = adapter.recent_materials_batch(["US.MU"])
        self.assertEqual(len(fetch_calls), 2)
        self.assertTrue(third["rows"][0]["cache_hit"])

    def test_fetch_crossing_catalog_deadline_revalidates_before_returning_or_caching(self) -> None:
        wall_clock = [datetime(2026, 9, 16, 23, 59, 50, tzinfo=timezone.utc)]
        monotonic_clock = [100.0]
        fetch_calls: list[str] = []
        probe_calls: list[str] = []

        def crossing_fetch(url: str, _hosts: set[str]) -> bytes:
            fetch_calls.append(url)
            if len(fetch_calls) == 1:
                wall_clock[0] = datetime(2026, 9, 17, 0, 0, 10, tzinfo=timezone.utc)
                monotonic_clock[0] += 20.0
            return b"<main></main>"

        adapter = OfficialEarningsMaterialsAdapter(
            fetch_bytes=crossing_fetch,
            probe_material=lambda url, hosts: probe_calls.append(url) or successful_material_probe(url, hosts),
            clock=lambda: wall_clock[0],
            monotonic=lambda: monotonic_clock[0],
            cache_ttl_seconds=900,
        )

        first = adapter.recent_materials_batch(["US.MU"])

        self.assertEqual(first["state"], "empty")
        self.assertEqual(first["captured_at"], "2026-09-17T00:00:10.000Z")
        self.assertEqual(len(fetch_calls), 2)
        self.assertEqual(len(probe_calls), 2)
        self.assertEqual(first["rows"][0]["materials"], [])
        self.assertTrue(all(
            item["access_state"] == "stale"
            for item in first["rows"][0]["rejected_curated_materials"]
        ))
        self.assertTrue(all(
            item["access_checked_at"] == first["captured_at"]
            for item in first["rows"][0]["rejected_curated_materials"]
        ))
        second = adapter.recent_materials_batch(["US.MU"])
        self.assertEqual(len(fetch_calls), 2)
        self.assertTrue(second["rows"][0]["cache_hit"])

    def test_batch_revalidates_fast_row_when_slow_row_crosses_catalog_deadline(self) -> None:
        wall_clock = [datetime(2026, 9, 16, 23, 59, 50, tzinfo=timezone.utc)]
        after_boundary = datetime(2026, 9, 17, 0, 0, 10, tzinfo=timezone.utc)
        fast_row_finished = threading.Event()
        fetch_hosts: list[str] = []

        def staggered_fetch(url: str, _hosts: set[str]) -> bytes:
            host = str(urlparse(url).hostname or "")
            fetch_hosts.append(host)
            if host == "investor.wdc.com" and fetch_hosts.count(host) == 1:
                self.assertTrue(fast_row_finished.wait(timeout=2))
                wall_clock[0] = after_boundary
            return b"<main></main>"

        adapter = OfficialEarningsMaterialsAdapter(
            fetch_bytes=staggered_fetch,
            probe_material=successful_material_probe,
            clock=lambda: wall_clock[0],
            cache_ttl_seconds=900,
        )
        original_material_row = adapter._material_row

        def observed_material_row(
            symbol: str,
            safe_limit: int,
            captured_at: datetime,
            *,
            force: bool,
        ) -> dict[str, object]:
            row = original_material_row(
                symbol,
                safe_limit,
                captured_at,
                force=force,
            )
            if symbol == "US.MU":
                fast_row_finished.set()
            return row

        adapter._material_row = observed_material_row  # type: ignore[method-assign]
        payload = adapter.recent_materials_batch(["US.MU", "US.WDC"])

        self.assertEqual(payload["captured_at"], "2026-09-17T00:00:10.000Z")
        self.assertEqual(fetch_hosts.count("investors.micron.com"), 2)
        self.assertEqual(fetch_hosts.count("investor.wdc.com"), 2)
        self.assertEqual([row["symbol"] for row in payload["rows"]], ["US.MU", "US.WDC"])
        self.assertTrue(all(row["materials"] == [] for row in payload["rows"]))
        self.assertTrue(all(
            adapter._row_catalog_still_valid(row, after_boundary)
            for row in payload["rows"]
        ))
        self.assertTrue(all(
            rejected["access_state"] == "stale"
            and rejected["access_checked_at"] == payload["captured_at"]
            for row in payload["rows"]
            for rejected in row["rejected_curated_materials"]
        ))

    def test_accessible_curated_material_covers_hub_failure_as_warning(self) -> None:
        adapter = OfficialEarningsMaterialsAdapter(
            fetch_bytes=lambda _url, _hosts: (_ for _ in ()).throw(
                OSError("upstream verification page")
            ),
            probe_material=successful_material_probe,
            clock=lambda: FIXED_NOW,
        )

        payload = adapter.recent_materials_batch(["US.MU"])
        row = payload["rows"][0]

        self.assertEqual(payload["state"], "ready")
        self.assertEqual(payload["source_errors"], [])
        self.assertEqual(payload["source_warnings"][0]["severity"], "warning")
        self.assertEqual(row["quality"], "ready")
        self.assertEqual(row["hub_discovery_state"], "blocked")
        self.assertEqual(row["discovery_quality"], "curated_verified_accessible")
        self.assertTrue(all(item["access_state"] == "fetchable" for item in row["materials"]))

    def test_unreachable_curated_material_is_excluded_and_fails_closed(self) -> None:
        adapter = OfficialEarningsMaterialsAdapter(
            fetch_bytes=lambda _url, _hosts: b"<main><p>Financial Summary Table</p></main>",
            probe_material=lambda _url, _hosts: (_ for _ in ()).throw(
                TimeoutError("read operation timed out")
            ),
            clock=lambda: FIXED_NOW,
        )

        payload = adapter.recent_materials_batch(["US.SNDK"])
        row = payload["rows"][0]

        self.assertEqual(payload["state"], "empty")
        self.assertEqual(row["quality"], "limited")
        self.assertFalse(row["fetchable"])
        self.assertEqual(row["materials"], [])
        self.assertEqual(row["rejected_material_count"], 1)
        self.assertEqual(row["rejected_curated_materials"][0]["access_state"], "blocked")
        self.assertEqual(payload["source_errors"][0]["code"], "EARNINGS_MATERIAL_ACCESS_TIMEOUT")

    def test_expired_curated_catalog_blocks_without_probing_old_links(self) -> None:
        probe_calls: list[str] = []
        adapter = OfficialEarningsMaterialsAdapter(
            fetch_bytes=lambda _url, _hosts: b"<main><p>Financial Summary Table</p></main>",
            probe_material=lambda url, _hosts: probe_calls.append(url) or successful_material_probe(url, _hosts),
            clock=lambda: datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc),
        )

        payload = adapter.recent_materials_batch(["US.MU"])

        self.assertEqual(payload["state"], "empty")
        self.assertEqual(probe_calls, [])
        self.assertEqual(payload["source_errors"][0]["code"], "EARNINGS_MATERIAL_CURATED_STALE")
        self.assertEqual(payload["rows"][0]["materials"], [])
        self.assertTrue(all(
            item["access_state"] == "stale"
            for item in payload["rows"][0]["rejected_curated_materials"]
        ))

    def test_live_material_excludes_stale_curated_candidates_without_blocking_the_row(self) -> None:
        fetch_calls: list[str] = []
        adapter = OfficialEarningsMaterialsAdapter(
            fetch_bytes=lambda url, _hosts: fetch_calls.append(url) or b"""
                <main><h3>2026</h3><h4>Q3</h4>
                  <a href='/static-files/live-q3-presentation'>View Presentation</a>
                </main>""",
            probe_material=lambda _url, _hosts: self.fail("stale curated links must not be probed"),
            clock=lambda: datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc),
        )

        payload = adapter.recent_materials_batch(["US.MU"])
        row = payload["rows"][0]

        self.assertEqual(payload["state"], "ready")
        self.assertEqual(row["quality"], "ready")
        self.assertEqual(row["discovery_quality"], "live")
        self.assertEqual(row["material_count"], 1)
        self.assertEqual(row["materials"][0]["discovery_method"], "live_hub_parse")
        self.assertEqual(row["rejected_material_count"], 2)
        self.assertTrue(all(
            item["access_state"] == "stale"
            for item in row["rejected_curated_materials"]
        ))
        self.assertEqual(payload["source_errors"], [])
        self.assertTrue(all(
            warning["excluded_from_evidence"]
            for warning in payload["source_warnings"]
        ))
        cached = adapter.recent_materials_batch(["US.MU"])
        self.assertEqual(len(fetch_calls), 1)
        self.assertTrue(cached["rows"][0]["cache_hit"])

    def test_parser_uses_accessible_anchor_labels_for_supplement_and_transcript(self) -> None:
        raw = b"""
        <main><h3>2026</h3><h4>Q4</h4>
          <a href='/supp.pdf' aria-label='Supplemental Financial Information'></a>
          <a href='/transcript.pdf' title='Corrected Transcript'></a>
        </main>"""

        materials = OfficialEarningsMaterialsAdapter._parse_material_links(
            raw,
            symbol="US.STX",
            hub_url="https://investors.seagate.com/financials/quarterly-results/",
            allowed_link_hosts={"investors.seagate.com"},
            limit=4,
        )

        self.assertEqual(
            [item["material_kind"] for item in materials],
            ["supplemental_financial_information", "corrected_transcript"],
        )


if __name__ == "__main__":
    unittest.main()
