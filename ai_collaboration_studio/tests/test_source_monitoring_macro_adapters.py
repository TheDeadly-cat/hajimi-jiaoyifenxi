from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.source_monitoring.adapters.base import validate_source_adapter
from backend.source_monitoring.adapters.macro_official import (
    BlsReleaseSourceAdapter,
    FederalReserveSourceAdapter,
    OfficialMacroCalendarSourceAdapter,
    TreasuryReleaseSourceAdapter,
)
from backend.source_monitoring.contracts import canonical_sha256
from backend.source_monitoring.packet_builder import build_packet_from_poll_result
from backend.source_monitoring.registry import SourceAdapterRegistry
from backend.source_monitoring.settings import SourceMonitoringSettings
from backend.source_monitoring.state_repository import SourceMonitoringStateRepository
from backend.source_monitoring.supervisor import SourceMonitoringSupervisor
from backend.source_inbox_service import SourceInboxService
from backend.store import StudioStore


FIXED_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
FIXED_NOW_MS = int(FIXED_NOW.timestamp() * 1_000)


def fed_release_row(*, summary: str = "Official statement metadata.") -> dict:
    return {
        "authority": "federal_reserve",
        "family": "fomc_statement",
        "reference_period": "2026-07-28/2026-07-29",
        "official_id": "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260729a.htm",
        "title": "Federal Reserve issues FOMC statement",
        "summary": summary,
        "official_url": "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260729a.htm",
        "source_url": "https://www.federalreserve.gov/feeds/press_monetary.xml",
        "scheduled_at": "",
        "released_at": "2026-07-29T18:00:00Z",
        "official_revision": False,
        "data": {},
    }


def bls_release_row(
    *,
    period: str = "M07",
    value: str = "3.4",
    official_revision: bool = False,
) -> dict:
    return {
        "authority": "bls",
        "family": "consumer_price_index",
        "reference_period": f"2026-{period}",
        "official_id": f"CUSR0000SA0:2026:{period}",
        "title": "Consumer Price Index official observation",
        "summary": "Official BLS public data observation.",
        "official_url": "https://api.bls.gov/publicAPI/v2/timeseries/data/CUSR0000SA0",
        "source_url": "https://api.bls.gov/publicAPI/v2/timeseries/data/CUSR0000SA0",
        "scheduled_at": "",
        "released_at": "",
        "official_revision": official_revision,
        "data": {
            "period": period,
            "series_id": "CUSR0000SA0",
            "value": value,
            "year": "2026",
        },
    }


def treasury_release_row(*, record_date: str = "2026-08-28") -> dict:
    endpoint = (
        "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v2/"
        "accounting/od/debt_to_penny?fields=record_date,debt_held_public_amt,"
        "intragov_hold_amt,tot_pub_debt_out_amt&sort=-record_date&page%5Bsize%5D=10"
    )
    return {
        "authority": "treasury",
        "family": "debt_to_penny",
        "reference_period": record_date,
        "official_id": f"debt_to_penny:{record_date}",
        "title": f"Debt to the Penny official observation for {record_date}",
        "summary": "Official Treasury Fiscal Data observation.",
        "official_url": endpoint,
        "source_url": endpoint,
        "scheduled_at": "",
        "released_at": "",
        "official_revision": False,
        "data": {
            "debt_held_public_amt": "30000000000000.00",
            "intragov_hold_amt": "7000000000000.00",
            "record_date": record_date,
            "tot_pub_debt_out_amt": "37000000000000.00",
        },
    }


def calendar_row(*, scheduled_at: str = "2026-09-16T18:00:00Z") -> dict:
    return {
        "authority": "federal_reserve",
        "family": "fomc_meeting",
        "reference_period": "2026-09",
        "official_id": "fomc:2026:6",
        "title": "Federal Open Market Committee meeting",
        "summary": "Tentative official FOMC meeting date.",
        "official_url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        "source_url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        "scheduled_at": scheduled_at,
        "released_at": "",
        "official_revision": False,
        "data": {},
    }


class MutableMacroClient:
    transport_identity = "fixture_official_macro_v1"

    def __init__(self) -> None:
        self.fed_rows = [fed_release_row()]
        self.bls_rows = [bls_release_row()]
        self.treasury_rows = [treasury_release_row()]
        self.calendar_rows = [calendar_row()]
        self.errors: list[dict] = []
        self.calls: list[tuple[str, int]] = []
        self.manifest_version = "fixture_macro_manifest_v1"

    @property
    def source_manifest(self) -> dict:
        return {"version": self.manifest_version}

    def _payload(self, name: str, rows: list[dict], limit: int) -> dict:
        self.calls.append((name, limit))
        return {"rows": list(rows), "source_errors": list(self.errors)}

    def federal_reserve_releases(self, *, limit: int) -> dict:
        return self._payload("federal_reserve_releases", self.fed_rows, limit)

    def bls_releases(self, *, limit: int) -> dict:
        return self._payload("bls_releases", self.bls_rows, limit)

    def treasury_releases(self, *, limit: int) -> dict:
        return self._payload("treasury_releases", self.treasury_rows, limit)

    def calendar_events(self, *, limit: int) -> dict:
        return self._payload("calendar_events", self.calendar_rows, limit)


class BoundMethodTransportMacroClient(MutableMacroClient):
    def _fetch_bytes(self, _url: str) -> bytes:
        return b"fixture"


class OfficialMacroAdapterContractTests(unittest.TestCase):
    def test_four_adapters_expose_the_closed_official_protocol(self) -> None:
        cases = (
            (FederalReserveSourceAdapter, "federal_reserve", "release", 50),
            (BlsReleaseSourceAdapter, "bls_releases", "release", 12),
            (TreasuryReleaseSourceAdapter, "treasury_releases", "release", 10),
            (
                OfficialMacroCalendarSourceAdapter,
                "official_macro_calendar",
                "schedule",
                50,
            ),
        )
        for adapter_type, key, phase, maximum in cases:
            with self.subTest(adapter=key):
                adapter = adapter_type(client=MutableMacroClient())
                metadata = validate_source_adapter(adapter)
                self.assertEqual(metadata.adapter_key, key)
                self.assertEqual(metadata.max_candidates_per_poll, maximum)
                self.assertTrue(metadata.official_source)
                self.assertEqual(metadata.execution_capability, "none")
                self.assertFalse(metadata.live_trading_allowed)
                self.assertEqual(adapter.subject_phase, phase)

    def test_release_duplicate_and_revision_are_distinct(self) -> None:
        client = MutableMacroClient()
        adapter = FederalReserveSourceAdapter(client=client)

        first = adapter.poll({}, observed_at_ms=FIXED_NOW_MS)
        duplicate = adapter.poll(
            first.next_checkpoint,
            observed_at_ms=FIXED_NOW_MS,
        )
        client.fed_rows = [fed_release_row(summary="Corrected official statement metadata.")]
        revised = adapter.poll(
            first.next_checkpoint,
            observed_at_ms=FIXED_NOW_MS,
        )

        self.assertEqual(len(first.observed_items), 1)
        first_item = first.observed_items[0]
        self.assertEqual(
            first_item["extensions"]["macro_official_v1"]["event_state"],
            "released",
        )
        self.assertEqual(len(duplicate.observed_items), 0)
        self.assertEqual(duplicate.duplicate_count, 1)
        self.assertEqual(len(revised.observed_items), 1)
        revised_item = revised.observed_items[0]
        revision = revised_item["extensions"]["macro_official_v1"]
        self.assertEqual(revision["event_state"], "revised")
        self.assertEqual(revision["subject_phase"], "release")
        self.assertEqual(revision["revision_target"], "document")
        self.assertEqual(
            first_item["external_item_id"],
            revised_item["external_item_id"],
        )
        self.assertNotEqual(
            first_item["sources"][-1]["content_sha256"],
            revised_item["sources"][-1]["content_sha256"],
        )
        build_packet_from_poll_result(first, external_run_id="macro-first")
        build_packet_from_poll_result(revised, external_run_id="macro-revised")

    def test_schedule_change_is_revised_schedule_not_release(self) -> None:
        client = MutableMacroClient()
        adapter = OfficialMacroCalendarSourceAdapter(client=client)
        first = adapter.poll({}, observed_at_ms=FIXED_NOW_MS)
        client.calendar_rows = [calendar_row(scheduled_at="2026-09-17T18:00:00Z")]
        revised = adapter.poll(first.next_checkpoint, observed_at_ms=FIXED_NOW_MS)

        initial = first.observed_items[0]["extensions"]["macro_official_v1"]
        changed = revised.observed_items[0]["extensions"]["macro_official_v1"]
        self.assertEqual(initial["event_state"], "scheduled")
        self.assertEqual(initial["subject_phase"], "schedule")
        self.assertEqual(changed["event_state"], "revised")
        self.assertEqual(changed["subject_phase"], "schedule")
        self.assertEqual(changed["revision_target"], "schedule_time")
        self.assertEqual(
            first.observed_items[0]["external_item_id"],
            revised.observed_items[0]["external_item_id"],
        )
        build_packet_from_poll_result(first, external_run_id="calendar-first")
        build_packet_from_poll_result(revised, external_run_id="calendar-revised")

    def test_official_revision_marker_can_classify_first_observation(self) -> None:
        client = MutableMacroClient()
        client.bls_rows = [bls_release_row(official_revision=True)]
        result = BlsReleaseSourceAdapter(client=client).poll(
            {}, observed_at_ms=FIXED_NOW_MS
        )
        extension = result.observed_items[0]["extensions"]["macro_official_v1"]
        self.assertEqual(extension["event_state"], "revised")
        self.assertEqual(extension["revision_target"], "data")
        self.assertEqual(extension["previous_projection_sha256"], "")

    def test_future_release_fails_closed_but_future_schedule_is_valid(self) -> None:
        release_client = MutableMacroClient()
        row = fed_release_row()
        row["released_at"] = "2026-09-01T00:00:00Z"
        release_client.fed_rows = [row]
        release = FederalReserveSourceAdapter(client=release_client).poll(
            {}, observed_at_ms=FIXED_NOW_MS
        )
        schedule = OfficialMacroCalendarSourceAdapter(
            client=MutableMacroClient()
        ).poll({}, observed_at_ms=FIXED_NOW_MS)

        self.assertEqual(release.next_checkpoint, {})
        self.assertEqual(release.observed_items, ())
        self.assertEqual(release.source_errors[0].code, "OFFICIAL_MACRO_RECORD_REJECTED")
        self.assertEqual(len(schedule.observed_items), 1)

    def test_conflict_source_error_and_capacity_are_atomic(self) -> None:
        conflicting_client = MutableMacroClient()
        changed = fed_release_row(summary="Conflicting projection.")
        conflicting_client.fed_rows = [fed_release_row(), changed]
        conflict = FederalReserveSourceAdapter(client=conflicting_client).poll(
            {}, observed_at_ms=FIXED_NOW_MS
        )
        self.assertEqual(conflict.next_checkpoint, {})
        self.assertEqual(conflict.observed_items, ())
        self.assertEqual(conflict.source_errors[0].code, "OFFICIAL_MACRO_IDENTITY_CONFLICT")

        error_client = MutableMacroClient()
        error_client.errors = [{
            "code": "BLS_HTTP_FORBIDDEN",
            "message": "official source denied the request",
            "scope": "bls",
        }]
        source_error = BlsReleaseSourceAdapter(client=error_client).poll(
            {}, observed_at_ms=FIXED_NOW_MS
        )
        self.assertEqual(source_error.next_checkpoint, {})
        self.assertEqual(source_error.observed_items, ())
        self.assertEqual(source_error.source_errors[0].code, "BLS_HTTP_FORBIDDEN")

        capacity_client = MutableMacroClient()
        capacity_client.bls_rows = [
            bls_release_row(period=f"M{index:02d}", value=str(index))
            for index in range(1, 4)
        ]
        capacity = BlsReleaseSourceAdapter(
            client=capacity_client,
            candidate_limit=2,
        ).poll({}, observed_at_ms=FIXED_NOW_MS, max_items=2)
        self.assertEqual(capacity.next_checkpoint, {})
        self.assertEqual(capacity.observed_items, ())
        self.assertEqual(
            capacity.source_errors[0].code,
            "OFFICIAL_MACRO_CANDIDATE_CAPACITY_EXCEEDED",
        )

    def test_checkpoint_capacity_and_config_tamper_fail_before_import(self) -> None:
        client = MutableMacroClient()
        client.bls_rows = []
        for index in range(121):
            year = 1900 + index // 12
            period = f"M{index % 12 + 1:02d}"
            row = bls_release_row(period=period, value=str(index))
            row["reference_period"] = f"{year}-{period}"
            row["official_id"] = f"CUSR0000SA0:{year}:{period}"
            row["data"]["year"] = str(year)
            client.bls_rows.append(row)
        capacity = BlsReleaseSourceAdapter(
            client=client,
            candidate_limit=2,
        ).poll({}, observed_at_ms=FIXED_NOW_MS)
        self.assertEqual(capacity.next_checkpoint, {})
        self.assertEqual(capacity.observed_items, ())
        self.assertEqual(
            capacity.source_errors[0].code,
            "OFFICIAL_MACRO_CHECKPOINT_CAPACITY_EXCEEDED",
        )

        sealed_client = MutableMacroClient()
        adapter = FederalReserveSourceAdapter(client=sealed_client)
        adapter._client = MutableMacroClient()
        with self.assertRaisesRegex(ValueError, "inner client changed"):
            adapter.poll({}, observed_at_ms=FIXED_NOW_MS)

    def test_closed_rows_urls_packet_budget_and_provenance_fail_closed(self) -> None:
        malformed_cases = []
        extra = fed_release_row()
        extra["order"] = "forbidden"
        malformed_cases.append(extra)
        mixed_key = fed_release_row()
        mixed_key["data"] = {"value": "1", 1: "invalid"}
        malformed_cases.append(mixed_key)
        compact = fed_release_row()
        compact["data"] = {"toolchoice": "forbidden"}
        malformed_cases.append(compact)
        for row in malformed_cases:
            with self.subTest(row=list(row)):
                client = MutableMacroClient()
                client.fed_rows = [row]
                result = FederalReserveSourceAdapter(client=client).poll(
                    {}, observed_at_ms=FIXED_NOW_MS
                )
                self.assertEqual(result.next_checkpoint, {})
                self.assertEqual(result.observed_items, ())
                self.assertTrue(result.source_errors)

        canonical_client = MutableMacroClient()
        canonical = bls_release_row()
        canonical["official_url"] = (
            "https://API.BLS.GOV:443/publicAPI/v2/timeseries/data/CUSR0000SA0"
        )
        canonical_client.bls_rows = [canonical]
        canonical_result = BlsReleaseSourceAdapter(client=canonical_client).poll(
            {}, observed_at_ms=FIXED_NOW_MS
        )
        self.assertEqual(canonical_result.source_errors, ())
        self.assertEqual(len(canonical_result.observed_items[0]["sources"]), 1)

        oversized_client = MutableMacroClient()
        oversized_client.calendar_rows = []
        for index in range(24):
            row = calendar_row()
            row["official_id"] = f"fomc:oversized:{index}"
            row["reference_period"] = f"oversized-{index}"
            row["summary"] = "界" * 8_000
            oversized_client.calendar_rows.append(row)
        oversized = OfficialMacroCalendarSourceAdapter(
            client=oversized_client
        ).poll({}, observed_at_ms=FIXED_NOW_MS)
        self.assertEqual(oversized.next_checkpoint, {})
        self.assertEqual(oversized.observed_items, ())
        self.assertEqual(
            oversized.source_errors[0].code,
            "OFFICIAL_MACRO_PACKET_REJECTED",
        )

        client = MutableMacroClient()
        sealed = FederalReserveSourceAdapter(client=client)
        client.federal_reserve_releases = lambda *, limit: {
            "rows": [],
            "source_errors": [],
        }
        with self.assertRaisesRegex(ValueError, "batch callable changed"):
            sealed.poll({}, observed_at_ms=FIXED_NOW_MS)

        manifest_client = MutableMacroClient()
        manifest_adapter = FederalReserveSourceAdapter(client=manifest_client)
        manifest_client.manifest_version = "fixture_macro_manifest_v2"
        with self.assertRaisesRegex(ValueError, "source manifest changed"):
            manifest_adapter.poll({}, observed_at_ms=FIXED_NOW_MS)

        resealed = FederalReserveSourceAdapter(client=MutableMacroClient())
        resealed._candidate_limit = 1
        forged_digest = canonical_sha256(resealed._config_basis())
        resealed._config_version = f"federal_reserve_config_v1_{forged_digest[:16]}"
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            resealed.poll({}, observed_at_ms=FIXED_NOW_MS)

    def test_malformed_client_envelopes_and_date_only_schedule_are_atomic(self) -> None:
        class ExtraPayloadClient(MutableMacroClient):
            def federal_reserve_releases(self, *, limit: int) -> dict:
                payload = super().federal_reserve_releases(limit=limit)
                payload["extra"] = "forbidden"
                return payload

        extra = FederalReserveSourceAdapter(client=ExtraPayloadClient()).poll(
            {}, observed_at_ms=FIXED_NOW_MS
        )
        self.assertEqual(extra.next_checkpoint, {})
        self.assertEqual(extra.observed_items, ())
        self.assertEqual(extra.source_errors[0].code, "OFFICIAL_MACRO_PAYLOAD_INVALID")

        malformed_errors = MutableMacroClient()
        malformed_errors.errors = [{
            "code": "SOURCE_FAILED",
            "message": "failed",
            "scope": "source",
            "order": "forbidden",
        }]
        invalid = FederalReserveSourceAdapter(client=malformed_errors).poll(
            {}, observed_at_ms=FIXED_NOW_MS
        )
        self.assertEqual(invalid.next_checkpoint, {})
        self.assertEqual(invalid.observed_items, ())
        self.assertEqual(
            invalid.source_errors[0].code,
            "OFFICIAL_MACRO_SOURCE_ERROR_INVALID",
        )

        date_client = MutableMacroClient()
        date_only = calendar_row(scheduled_at="")
        date_only["data"] = {
            "scheduled_date_end": "2026-09-16",
            "scheduled_date_start": "2026-09-15",
            "time_precision": "date",
        }
        date_client.calendar_rows = [date_only]
        date_result = OfficialMacroCalendarSourceAdapter(client=date_client).poll(
            {}, observed_at_ms=FIXED_NOW_MS
        )
        self.assertEqual(date_result.source_errors, ())
        extension = date_result.observed_items[0]["extensions"]["macro_official_v1"]
        self.assertEqual(extension["event_state"], "scheduled")
        self.assertEqual(extension["scheduled_at"], "")
        self.assertEqual(extension["status_basis"], "official_schedule_date_projection")

    def test_identity_phase_transport_and_checkpoint_determinism_are_sealed(self) -> None:
        control_client = MutableMacroClient()
        control = fed_release_row()
        control["official_id"] = "abc\nxyz"
        control_client.fed_rows = [control]
        rejected = FederalReserveSourceAdapter(client=control_client).poll(
            {}, observed_at_ms=FIXED_NOW_MS
        )
        self.assertEqual(rejected.next_checkpoint, {})
        self.assertEqual(rejected.observed_items, ())

        composed_client = MutableMacroClient()
        composed = fed_release_row()
        composed["official_id"] = "r\u00e9lease"
        composed_client.fed_rows = [composed]
        decomposed_client = MutableMacroClient()
        decomposed = fed_release_row()
        decomposed["official_id"] = "re\u0301lease"
        decomposed_client.fed_rows = [decomposed]
        composed_result = FederalReserveSourceAdapter(client=composed_client).poll(
            {}, observed_at_ms=FIXED_NOW_MS
        )
        decomposed_result = FederalReserveSourceAdapter(client=decomposed_client).poll(
            {}, observed_at_ms=FIXED_NOW_MS
        )
        self.assertEqual(
            composed_result.observed_items[0]["external_item_id"],
            decomposed_result.observed_items[0]["external_item_id"],
        )

        phase_client = MutableMacroClient()
        conflicting_phase = calendar_row()
        conflicting_phase["released_at"] = "2026-08-30T12:00:00Z"
        phase_client.calendar_rows = [conflicting_phase]
        phase = OfficialMacroCalendarSourceAdapter(client=phase_client).poll(
            {}, observed_at_ms=FIXED_NOW_MS
        )
        self.assertEqual(phase.next_checkpoint, {})
        self.assertEqual(phase.observed_items, ())

        transport_client = MutableMacroClient()
        transport_client._fetch_bytes = lambda _url: b"original"
        transport_adapter = FederalReserveSourceAdapter(client=transport_client)
        transport_client._fetch_bytes = lambda _url: b"replacement"
        with self.assertRaisesRegex(ValueError, "transport changed"):
            transport_adapter.poll({}, observed_at_ms=FIXED_NOW_MS)

        method_transport_client = BoundMethodTransportMacroClient()
        method_transport_adapter = FederalReserveSourceAdapter(
            client=method_transport_client
        )
        clean_method_transport = method_transport_adapter.poll(
            {}, observed_at_ms=FIXED_NOW_MS
        )
        self.assertEqual(len(clean_method_transport.observed_items), 1)
        method_transport_client._fetch_bytes = lambda _url: b"replacement"
        with self.assertRaisesRegex(ValueError, "transport changed"):
            method_transport_adapter.poll({}, observed_at_ms=FIXED_NOW_MS)

        order_client = MutableMacroClient()
        order_client.bls_rows = [
            bls_release_row(period="M06", value="1"),
            bls_release_row(period="M07", value="2"),
        ]
        adapter = BlsReleaseSourceAdapter(client=order_client)
        first = adapter.poll({}, observed_at_ms=FIXED_NOW_MS)
        order_client.bls_rows.reverse()
        replay = adapter.poll(first.next_checkpoint, observed_at_ms=FIXED_NOW_MS)
        self.assertEqual(replay.observed_items, ())
        self.assertEqual(replay.duplicate_count, 2)
        self.assertEqual(replay.next_checkpoint, first.next_checkpoint)

    def test_checkpoint_replay_uses_stable_official_occurrence_anchors(self) -> None:
        cases = []
        bls_client = MutableMacroClient()
        cases.append(BlsReleaseSourceAdapter(client=bls_client))
        treasury_client = MutableMacroClient()
        cases.append(TreasuryReleaseSourceAdapter(client=treasury_client))
        calendar_client = MutableMacroClient()
        date_only = calendar_row(scheduled_at="")
        date_only["data"] = {
            "scheduled_date_end": "2026-09-16",
            "scheduled_date_start": "2026-09-15",
            "time_precision": "date",
        }
        calendar_client.calendar_rows = [date_only]
        cases.append(OfficialMacroCalendarSourceAdapter(client=calendar_client))

        for adapter in cases:
            with self.subTest(adapter=adapter.adapter_key):
                first = adapter.poll({}, observed_at_ms=FIXED_NOW_MS)
                replay = adapter.poll({}, observed_at_ms=FIXED_NOW_MS + 60_000)
                self.assertEqual(
                    first.observed_items[0]["occurred_at"],
                    replay.observed_items[0]["occurred_at"],
                )
                self.assertEqual(
                    first.observed_items[0]["sources"][-1]["content_sha256"],
                    replay.observed_items[0]["sources"][-1]["content_sha256"],
                )


class OfficialMacroSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="ai-studio-macro-monitor-")
        self.database_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.clock = [FIXED_NOW_MS]
        self.store = StudioStore(self.database_path)
        self.repository = SourceMonitoringStateRepository(
            self.store,
            clock_ms=lambda: self.clock[0],
        )
        self.inbox = SourceInboxService(
            self.store,
            clock=lambda: self.clock[0] / 1_000,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _forbidden_side_effect_counts(self) -> tuple[int, int, int, int]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            return (
                connection.execute(
                    "SELECT COUNT(*) FROM provider_execution_runs"
                ).fetchone()[0],
                connection.execute(
                    "SELECT COUNT(*) FROM provider_call_attempts"
                ).fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM rounds").fetchone()[0],
                connection.execute(
                    "SELECT COUNT(*) FROM source_inbox_round_drafts"
                ).fetchone()[0],
            )

    def test_release_revision_enters_inbox_without_any_forbidden_side_effect(self) -> None:
        client = MutableMacroClient()
        adapter = FederalReserveSourceAdapter(client=client)
        registry = SourceAdapterRegistry((adapter,), official_only=True)
        self.repository.set_enabled(
            adapter.adapter_key,
            config_version=adapter.config_version,
            enabled=True,
        )
        supervisor = SourceMonitoringSupervisor(
            registry=registry,
            repository=self.repository,
            source_inbox=self.inbox,
            settings=SourceMonitoringSettings(
                enabled=True,
                auto_start=False,
                official_only=True,
                dry_run=False,
                max_items_per_run=50,
                initial_mode="from_time",
                from_time="1970-01-01T00:00:00Z",
            ),
            clock_ms=lambda: self.clock[0],
        )
        side_effects_before = self._forbidden_side_effect_counts()

        first = supervisor.run_once(adapter.adapter_key)
        client.fed_rows = [fed_release_row(summary="Corrected official projection.")]
        self.clock[0] += 60_000
        revised = supervisor.run_once(adapter.adapter_key)

        listing = self.inbox.list_items()
        self.assertEqual(first["status"], "SUCCEEDED")
        self.assertEqual(revised["status"], "SUCCEEDED")
        self.assertEqual(first["import"]["created_item_count"], 1)
        self.assertEqual(revised["import"]["created_item_count"], 1)
        self.assertEqual(len(listing["items"]), 2)
        self.assertEqual(
            {
                row["item"]["extensions"]["macro_official_v1"]["event_state"]
                for row in listing["items"]
            },
            {"released", "revised"},
        )
        self.assertEqual(
            {row["item"]["external_item_id"] for row in listing["items"]},
            {listing["items"][0]["item"]["external_item_id"]},
        )
        for row in listing["items"]:
            self.assertEqual(row["state"], "AWAITING_USER")
            self.assertEqual(
                row["item"]["external_claims_verification"],
                "external_unverified",
            )
            self.assertEqual(row["item"]["recommended_route"], "notify_only")
            self.assertEqual(row["item"]["impact_hypotheses"], [])
        self.assertEqual(
            self._forbidden_side_effect_counts(),
            side_effects_before,
        )
        self.assertEqual(first["safety"]["provider_calls_performed"], 0)
        self.assertEqual(first["safety"]["formal_rounds_created"], 0)


if __name__ == "__main__":
    unittest.main()
