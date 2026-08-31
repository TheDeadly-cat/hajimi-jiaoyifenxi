from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.source_monitoring.contracts import (  # noqa: E402
    FUTU_ANOMALY_SOURCE_CHANNEL,
    OFFICIAL_SOURCE_CHANNEL,
)
from backend.market.sec_edgar import SecEdgarAdapter  # noqa: E402
from backend.source_monitoring.adapters.sec_filings import (  # noqa: E402
    SecFilingsSourceAdapter,
)

from backend.source_monitoring.state_repository import (  # noqa: E402
    RUN_STATUS_ABANDONED,
    RUN_STATUS_DEGRADED,
    RUN_STATUS_DRY_RUN,
    RUN_STATUS_FAILED,
    RUN_STATUS_SUCCEEDED,
    SOURCE_MONITORING_MIGRATION_KEY,
    SourceMonitoringStateError,
    SourceMonitoringStateRepository,
)
from backend.source_inbox_service import SourceInboxService  # noqa: E402
from backend.source_inbox_contracts import SOURCE_IMPORT_PACKET_VERSION  # noqa: E402
from backend.store import StudioStore  # noqa: E402
from tests.test_source_inbox_contracts import _packet  # noqa: E402
from tests.test_source_monitoring_official_adapters import (  # noqa: E402
    FIXED_NOW,
    FIXED_NOW_MS,
    SecFixtureFetcher,
)
from tests.test_trading_impact_rules import _sec  # noqa: E402


class SourceMonitoringStateRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="ai-studio-source-monitor-state-")
        self.database_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.clock = [1_900_000_000_000]
        self.store = StudioStore(self.database_path)
        self.repository = SourceMonitoringStateRepository(
            self.store,
            clock_ms=lambda: self.clock[0],
        )
        self.adapter_key = "fake_official"
        self.config_version = "fake_official_config_v1"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def enable(self) -> dict:
        return self.repository.set_enabled(
            self.adapter_key,
            config_version=self.config_version,
            enabled=True,
        )

    def import_for_run(
        self,
        run_id: str,
        *,
        source_channel: str = OFFICIAL_SOURCE_CHANNEL,
    ) -> dict:
        packet = copy.deepcopy(_packet())
        packet["source_channel"] = source_channel
        packet["source_key"] = self.adapter_key
        packet["external_run_id"] = run_id
        packet["generation"]["channel"] = source_channel
        packet["generation"]["correlated_output"] = False
        packet["items"][0]["external_item_id"] = run_id
        packet["items"][0]["headline"] = f"Official source event for {run_id}"
        service = SourceInboxService(
            self.store,
            clock=lambda: self.clock[0] / 1_000,
        )
        return service.import_packet(
            json.dumps(packet, ensure_ascii=False),
            actor="source_monitoring_worker",
        )

    def import_sec_for_run(
        self,
        run_id: str,
        *,
        accession: str = "0001045810-26-000001",
    ) -> dict:
        item, _item_sha256 = _sec("8-K", "US.NVDA")
        raw_item = copy.deepcopy(item)
        for derived in (
            "external_claims_verification",
            "server_fingerprint",
            "server_fingerprint_version",
        ):
            raw_item.pop(derived)
        raw_item["external_item_id"] = accession
        raw_item["headline"] = f"US.NVDA filed SEC 8-K ({accession})"
        raw_item["extensions"]["sec_v1"]["accession_number"] = accession
        packet = {
            "version": SOURCE_IMPORT_PACKET_VERSION,
            "source_channel": OFFICIAL_SOURCE_CHANNEL,
            "source_key": "sec_filings",
            "external_run_id": run_id,
            "checked_at": "2026-08-31T04:00:00Z",
            "cutoff_at": "2026-08-31T04:00:00Z",
            "meaningful_change": True,
            "items": [raw_item],
            "generation": {
                "channel": OFFICIAL_SOURCE_CHANNEL,
                "model": "",
                "cost": {
                    "status": "unavailable",
                    "amount": None,
                    "currency": "",
                    "usage_source": "not_applicable",
                },
                "correlated_output": False,
            },
        }
        return SourceInboxService(
            self.store,
            clock=lambda: self.clock[0] / 1_000,
        ).import_packet(
            json.dumps(packet, ensure_ascii=False),
            actor="source_monitoring_worker",
        )

    def test_schema_is_registered_and_adapters_default_disabled(self) -> None:
        state = self.repository.get_or_create_state(
            self.adapter_key,
            config_version=self.config_version,
        )

        self.assertFalse(state["enabled"])
        self.assertEqual(state["checkpoint"], {})
        self.assertEqual(state["consecutive_failures"], 0)
        with closing(sqlite3.connect(self.database_path)) as connection:
            marker = connection.execute(
                "SELECT applied_at FROM schema_migrations WHERE key=?",
                (SOURCE_MONITORING_MIGRATION_KEY,),
            ).fetchone()
            state_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(source_adapter_states)")
            }
            run_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(source_adapter_runs)")
            }
        self.assertIsNotNone(marker)
        self.assertTrue({
            "adapter_key",
            "enabled",
            "config_version",
            "checkpoint_json",
            "etag",
            "last_modified",
            "last_started_at_ms",
            "last_success_at_ms",
            "last_event_at_ms",
            "next_due_at_ms",
            "consecutive_failures",
            "last_error_code",
            "last_error_message",
            "state_version",
            "updated_at_ms",
        }.issubset(state_columns))
        self.assertTrue({
            "run_id",
            "adapter_key",
            "started_at_ms",
            "completed_at_ms",
            "status",
            "observed_count",
            "accepted_count",
            "duplicate_count",
            "rejected_count",
            "duration_ms",
            "error_code",
            "error_message",
            "receipt_id",
        }.issubset(run_columns))

    def test_success_advances_checkpoint_only_at_terminal_commit(self) -> None:
        enabled = self.enable()
        started = self.repository.start_run(
            self.adapter_key,
            config_version=self.config_version,
        )

        self.assertEqual(started["run"]["started_checkpoint"], {})
        self.assertEqual(started["state"]["checkpoint"], {})
        self.assertGreater(started["state"]["state_version"], enabled["state_version"])
        with self.assertRaises(SourceMonitoringStateError) as active_error:
            self.repository.start_run(
                self.adapter_key,
                config_version=self.config_version,
            )
        self.assertEqual(active_error.exception.code, "SOURCE_MONITORING_RUN_ACTIVE")

        self.clock[0] += 250
        imported = self.import_for_run(started["run"]["run_id"])
        completed = self.repository.complete_run(
            started["run"]["run_id"],
            next_checkpoint={"cursor": 2},
            status=RUN_STATUS_SUCCEEDED,
            observed_count=1,
            accepted_count=1,
            duplicate_count=0,
            rejected_count=0,
            next_due_at_ms=self.clock[0] + 60_000,
            receipt_id=imported["import_id"],
            etag='"official-etag"',
            last_modified="Sun, 30 Aug 2026 16:00:00 GMT",
        )

        self.assertEqual(completed["run"]["status"], RUN_STATUS_SUCCEEDED)
        self.assertEqual(completed["run"]["accepted_count"], 1)
        self.assertEqual(completed["state"]["checkpoint"], {"cursor": 2})
        self.assertEqual(completed["state"]["etag"], '"official-etag"')
        self.assertEqual(completed["state"]["last_event_at_ms"], self.clock[0])
        self.assertEqual(completed["state"]["consecutive_failures"], 0)

    def test_active_run_blocks_enablement_change_and_accepts_bounded_etag(self) -> None:
        self.enable()
        started = self.repository.start_run(
            self.adapter_key,
            config_version=self.config_version,
        )

        with self.assertRaises(SourceMonitoringStateError) as active_error:
            self.repository.set_enabled(
                self.adapter_key,
                config_version=self.config_version,
                enabled=False,
            )
        self.assertEqual(active_error.exception.code, "SOURCE_MONITORING_RUN_ACTIVE")

        bounded_etag = "e" * 1_024
        completed = self.repository.complete_run(
            started["run"]["run_id"],
            next_checkpoint={"cursor": 1},
            status=RUN_STATUS_SUCCEEDED,
            observed_count=0,
            accepted_count=0,
            duplicate_count=0,
            rejected_count=0,
            next_due_at_ms=self.clock[0] + 60_000,
            etag=bounded_etag,
        )
        self.assertEqual(completed["state"]["etag"], bounded_etag)

    def test_failure_and_dry_run_preserve_last_committed_checkpoint(self) -> None:
        self.enable()
        seed = self.repository.start_run(
            self.adapter_key,
            config_version=self.config_version,
        )
        self.clock[0] += 10
        seed_import = self.import_for_run(seed["run"]["run_id"])
        self.repository.complete_run(
            seed["run"]["run_id"],
            next_checkpoint={"cursor": 7},
            status=RUN_STATUS_SUCCEEDED,
            observed_count=1,
            accepted_count=1,
            duplicate_count=0,
            rejected_count=0,
            next_due_at_ms=self.clock[0],
            receipt_id=seed_import["import_id"],
        )

        failed = self.repository.start_run(
            self.adapter_key,
            config_version=self.config_version,
        )
        self.clock[0] += 20
        failure = self.repository.fail_run(
            failed["run"]["run_id"],
            error_code="ADAPTER_POLL_FAILED",
            error_message="Adapter poll failed closed.",
            next_due_at_ms=self.clock[0] + 120_000,
            observed_count=1,
            rejected_count=1,
        )
        self.assertEqual(failure["run"]["status"], RUN_STATUS_FAILED)
        self.assertEqual(failure["state"]["checkpoint"], {"cursor": 7})
        self.assertEqual(failure["state"]["consecutive_failures"], 1)

        dry = self.repository.start_run(
            self.adapter_key,
            config_version=self.config_version,
            dry_run=True,
        )
        dry_started_state = dry["state"]
        self.clock[0] += 20
        dry_result = self.repository.complete_run(
            dry["run"]["run_id"],
            next_checkpoint={"cursor": 9},
            status=RUN_STATUS_DRY_RUN,
            observed_count=2,
            accepted_count=0,
            duplicate_count=0,
            rejected_count=0,
            next_due_at_ms=self.clock[0] + 60_000,
            etag='"dry-run-etag"',
        )
        self.assertTrue(dry_result["run"]["dry_run"])
        self.assertEqual(dry_result["run"]["next_checkpoint"], {"cursor": 9})
        self.assertEqual(dry_result["state"]["checkpoint"], {"cursor": 7})
        self.assertNotEqual(dry_result["state"]["etag"], '"dry-run-etag"')
        for field in (
            "last_success_at_ms",
            "last_event_at_ms",
            "next_due_at_ms",
            "consecutive_failures",
            "last_error_code",
            "last_error_message",
            "state_version",
            "updated_at_ms",
        ):
            self.assertEqual(dry_result["state"][field], dry_started_state[field])

        rejected_dry = self.repository.start_run(
            self.adapter_key,
            config_version=self.config_version,
            dry_run=True,
        )
        with self.assertRaises(SourceMonitoringStateError) as dry_error:
            self.repository.complete_run(
                rejected_dry["run"]["run_id"],
                next_checkpoint={"cursor": 10},
                status=RUN_STATUS_DRY_RUN,
                observed_count=1,
                accepted_count=1,
                duplicate_count=0,
                rejected_count=0,
                next_due_at_ms=self.clock[0],
                receipt_id="forbidden_dry_run_receipt",
            )
        self.assertEqual(dry_error.exception.code, "SOURCE_MONITORING_STATE_INVALID")
        self.repository.complete_run(
            rejected_dry["run"]["run_id"],
            next_checkpoint=rejected_dry["run"]["started_checkpoint"],
            status=RUN_STATUS_DRY_RUN,
            observed_count=0,
            accepted_count=0,
            duplicate_count=0,
            rejected_count=0,
            next_due_at_ms=self.clock[0],
            error_code="DRY_RUN_REJECTED",
            error_message="Rejected invalid dry-run completion.",
        )

    def test_degraded_run_records_partial_import_without_advancing_checkpoint(self) -> None:
        self.enable()
        seed = self.repository.start_run(
            self.adapter_key,
            config_version=self.config_version,
        )
        self.repository.complete_run(
            seed["run"]["run_id"],
            next_checkpoint={"cursor": 4},
            status=RUN_STATUS_SUCCEEDED,
            observed_count=1,
            accepted_count=0,
            duplicate_count=1,
            rejected_count=0,
            next_due_at_ms=self.clock[0],
            etag='"committed"',
        )
        started = self.repository.start_run(
            self.adapter_key,
            config_version=self.config_version,
        )
        self.clock[0] += 50
        partial_import = self.import_for_run(started["run"]["run_id"])
        degraded = self.repository.complete_run(
            started["run"]["run_id"],
            next_checkpoint={"cursor": 8},
            status=RUN_STATUS_DEGRADED,
            observed_count=2,
            accepted_count=1,
            duplicate_count=0,
            rejected_count=1,
            next_due_at_ms=self.clock[0] + 120_000,
            receipt_id=partial_import["import_id"],
            etag='"uncommitted"',
            error_code="PARTIAL_SOURCE_FAILURE",
            error_message="One source failed.",
        )
        self.assertEqual(degraded["run"]["next_checkpoint"], {"cursor": 8})
        self.assertEqual(degraded["state"]["checkpoint"], {"cursor": 4})
        self.assertEqual(degraded["state"]["etag"], '"committed"')
        self.assertEqual(degraded["state"]["consecutive_failures"], 1)
        self.assertEqual(degraded["state"]["last_error_code"], "PARTIAL_SOURCE_FAILURE")

    def test_receipt_binding_and_strict_completion_inputs_fail_closed(self) -> None:
        self.enable()
        started = self.repository.start_run(
            self.adapter_key,
            config_version=self.config_version,
        )
        common = {
            "next_checkpoint": {"cursor": 1},
            "status": RUN_STATUS_SUCCEEDED,
            "observed_count": 1,
            "accepted_count": 1,
            "duplicate_count": 0,
            "rejected_count": 0,
            "next_due_at_ms": self.clock[0],
        }
        with self.assertRaises(SourceMonitoringStateError) as missing_receipt:
            self.repository.complete_run(started["run"]["run_id"], **common)
        self.assertEqual(
            missing_receipt.exception.code,
            "SOURCE_MONITORING_RECEIPT_REQUIRED",
        )
        with self.assertRaises(SourceMonitoringStateError) as fake_receipt:
            self.repository.complete_run(
                started["run"]["run_id"],
                **common,
                receipt_id="source_import_missing",
            )
        self.assertEqual(
            fake_receipt.exception.code,
            "SOURCE_MONITORING_RECEIPT_INVALID",
        )
        with self.assertRaises(SourceMonitoringStateError) as invalid_status:
            self.repository.complete_run(
                started["run"]["run_id"],
                **{**common, "status": [RUN_STATUS_SUCCEEDED]},
            )
        self.assertEqual(invalid_status.exception.code, "SOURCE_MONITORING_STATE_INVALID")
        with self.assertRaises(SourceMonitoringStateError) as invalid_errors:
            self.repository.complete_run(
                started["run"]["run_id"],
                **{**common, "accepted_count": 0},
                source_errors=({"code": "SOURCE_FAILURE"},),
            )
        self.assertEqual(invalid_errors.exception.code, "SOURCE_MONITORING_STATE_INVALID")
        self.repository.fail_run(
            started["run"]["run_id"],
            error_code="TEST_CLEANUP",
            error_message="Close the intentionally rejected run.",
            next_due_at_ms=self.clock[0],
        )

    def test_receipt_binding_rejects_cross_channel_and_accepts_exact_channel(self) -> None:
        self.enable()
        started = self.repository.start_run(
            self.adapter_key,
            config_version=self.config_version,
        )
        imported = self.import_for_run(
            started["run"]["run_id"],
            source_channel=FUTU_ANOMALY_SOURCE_CHANNEL,
        )
        common = {
            "next_checkpoint": {"cursor": 1},
            "status": RUN_STATUS_SUCCEEDED,
            "observed_count": 1,
            "accepted_count": 1,
            "duplicate_count": 0,
            "rejected_count": 0,
            "next_due_at_ms": self.clock[0],
            "receipt_id": imported["import_id"],
        }

        with self.assertRaises(SourceMonitoringStateError) as mismatch:
            self.repository.complete_run(started["run"]["run_id"], **common)
        self.assertEqual(
            mismatch.exception.code,
            "SOURCE_MONITORING_RECEIPT_INVALID",
        )

        completed = self.repository.complete_run(
            started["run"]["run_id"],
            **common,
            source_channel=FUTU_ANOMALY_SOURCE_CHANNEL,
        )
        self.assertEqual(completed["run"]["status"], RUN_STATUS_SUCCEEDED)

    def test_restart_recovery_abandons_run_without_advancing_checkpoint(self) -> None:
        self.enable()
        started = self.repository.start_run(
            self.adapter_key,
            config_version=self.config_version,
        )
        self.clock[0] += 1_000

        self.assertEqual(self.repository.recover_incomplete_runs(), 1)
        recovered = self.repository.get_run(started["run"]["run_id"])
        state = self.repository.get_state(self.adapter_key)
        assert recovered is not None and state is not None
        self.assertEqual(recovered["status"], RUN_STATUS_ABANDONED)
        self.assertEqual(recovered["next_checkpoint"], {})
        self.assertEqual(state["checkpoint"], {})
        self.assertEqual(state["consecutive_failures"], 1)

        next_run = self.repository.start_run(
            self.adapter_key,
            config_version=self.config_version,
        )
        self.assertEqual(next_run["run"]["started_checkpoint"], {})

    def test_dry_run_start_and_restart_recovery_leave_adapter_state_unchanged(self) -> None:
        self.enable()
        before = self.repository.get_state(self.adapter_key)
        started = self.repository.start_run(
            self.adapter_key,
            config_version=self.config_version,
            dry_run=True,
        )
        self.assertTrue(started["run"]["dry_run"])
        self.assertEqual(started["state"], before)

        self.clock[0] += 1_000
        self.assertEqual(self.repository.recover_incomplete_runs(), 1)
        recovered = self.repository.get_run(started["run"]["run_id"])
        after = self.repository.get_state(self.adapter_key)
        self.assertEqual(recovered["status"], RUN_STATUS_ABANDONED)
        self.assertTrue(recovered["dry_run"])
        self.assertEqual(after, before)

    def test_sec_v1_to_v2_migration_requires_persisted_accession_union(self) -> None:
        old_config = "sec_filings_config_v1_fixture"
        new_config = "sec_filings_config_v2_fixture"
        self.repository.get_or_create_state(
            "sec_filings",
            config_version=old_config,
        )
        self.repository.set_enabled(
            "sec_filings",
            config_version=old_config,
            enabled=True,
        )
        started = self.repository.start_run(
            "sec_filings",
            config_version=old_config,
        )
        imported = self.import_sec_for_run(started["run"]["run_id"])
        self.assertEqual(imported["created_item_count"], 1)
        self.clock[0] += 1_000
        self.assertEqual(self.repository.recover_incomplete_runs(), 1)
        self.repository.set_enabled(
            "sec_filings",
            config_version=old_config,
            enabled=False,
        )
        forged = self.import_sec_for_run(
            "manual-forged-reserved-history",
            accession="0001045810-26-000099",
        )
        self.assertEqual(forged["created_item_count"], 1)
        state = self.repository.get_state("sec_filings")
        with closing(sqlite3.connect(self.database_path)) as connection:
            before = connection.execute(
                "SELECT item_json,item_sha256 FROM source_inbox_items"
            ).fetchall()
        database_before_preview = self.database_path.stat()

        preview = self.repository.preview_sec_filings_v1_to_v2_migration(
            expected_config_version=old_config,
            new_config_version=new_config,
            expected_state_version=state["state_version"],
        )
        self.assertEqual(
            preview["next_checkpoint"],
            {
                "version": "sec_filings_checkpoint_v1",
                "seen_accessions": ["0001045810-26-000001"],
            },
        )
        self.assertEqual(preview["persisted_accession_count"], 1)
        self.assertEqual(preview["safety"]["database_writes_performed"], 0)
        database_after_preview = self.database_path.stat()
        self.assertEqual(
            (
                database_after_preview.st_size,
                database_after_preview.st_mtime_ns,
            ),
            (
                database_before_preview.st_size,
                database_before_preview.st_mtime_ns,
            ),
        )
        self.assertEqual(
            self.repository.get_state("sec_filings")["state_version"],
            state["state_version"],
        )

        with self.assertRaises(SourceMonitoringStateError) as mismatch:
            self.repository.migrate_config(
                "sec_filings",
                expected_config_version=old_config,
                new_config_version=new_config,
                expected_state_version=state["state_version"],
                next_checkpoint={},
            )
        self.assertEqual(
            mismatch.exception.code,
            "SOURCE_MONITORING_SEC_MIGRATION_CHECKPOINT_MISMATCH",
        )
        self.assertEqual(
            self.repository.get_state("sec_filings")["config_version"],
            old_config,
        )

        migrated = self.repository.migrate_config(
            "sec_filings",
            expected_config_version=old_config,
            new_config_version=new_config,
            expected_state_version=state["state_version"],
            next_checkpoint=preview["next_checkpoint"],
        )
        self.assertEqual(migrated["config_version"], new_config)
        self.assertEqual(migrated["checkpoint"], preview["next_checkpoint"])
        with closing(sqlite3.connect(self.database_path)) as connection:
            after = connection.execute(
                "SELECT item_json,item_sha256 FROM source_inbox_items"
            ).fetchall()
        self.assertEqual(after, before)

        fetcher = SecFixtureFetcher()
        monitor = SecFilingsSourceAdapter(
            adapter=SecEdgarAdapter(
                user_agent="AI Studio monitor@example.com",
                fetch_json=fetcher,
                clock=lambda: FIXED_NOW,
                allowed_symbols=["US.NVDA"],
            ),
            allowed_symbols=["US.NVDA"],
            allowed_forms=["8-K"],
        )
        replay = monitor.poll(
            migrated["checkpoint"],
            observed_at_ms=FIXED_NOW_MS,
            max_items=50,
        )
        self.assertEqual(replay.observed_items, ())
        self.assertEqual(replay.duplicate_count, 1)
        self.assertEqual(replay.next_checkpoint, migrated["checkpoint"])

    def test_sec_v1_to_v2_migration_accepts_a_verified_duplicate_observation(self) -> None:
        old_config = "sec_filings_config_v1_fixture"
        new_config = "sec_filings_config_v2_fixture"
        accession = "0001045810-26-000001"
        forged_origin = self.import_sec_for_run(
            "manual-forged-reserved-origin",
            accession=accession,
        )
        self.assertEqual(forged_origin["created_item_count"], 1)

        self.repository.get_or_create_state(
            "sec_filings",
            config_version=old_config,
        )
        self.repository.set_enabled(
            "sec_filings",
            config_version=old_config,
            enabled=True,
        )
        started = self.repository.start_run(
            "sec_filings",
            config_version=old_config,
        )
        genuine_observation = self.import_sec_for_run(
            started["run"]["run_id"],
            accession=accession,
        )
        self.assertEqual(genuine_observation["created_item_count"], 0)
        self.assertEqual(genuine_observation["duplicate_item_count"], 1)
        with closing(sqlite3.connect(self.database_path)) as connection:
            item_origin = connection.execute(
                "SELECT origin_import_id FROM source_inbox_items WHERE external_item_id=?",
                (accession,),
            ).fetchone()
            genuine_link = connection.execute(
                """SELECT disposition FROM source_inbox_import_items
                   WHERE import_id=?""",
                (genuine_observation["import_id"],),
            ).fetchone()
        self.assertEqual(item_origin, (forged_origin["import_id"],))
        self.assertEqual(genuine_link, ("DUPLICATE",))

        self.clock[0] += 1_000
        self.assertEqual(self.repository.recover_incomplete_runs(), 1)
        recovered = self.repository.get_run(started["run"]["run_id"])
        assert recovered is not None
        self.assertEqual(recovered["status"], RUN_STATUS_ABANDONED)
        self.repository.set_enabled(
            "sec_filings",
            config_version=old_config,
            enabled=False,
        )
        forged_only = self.import_sec_for_run(
            "manual-forged-reserved-history",
            accession="0001045810-26-000099",
        )
        self.assertEqual(forged_only["created_item_count"], 1)

        state = self.repository.get_state("sec_filings")
        assert state is not None
        preview = self.repository.preview_sec_filings_v1_to_v2_migration(
            expected_config_version=old_config,
            new_config_version=new_config,
            expected_state_version=state["state_version"],
        )
        self.assertEqual(
            preview["next_checkpoint"],
            {
                "version": "sec_filings_checkpoint_v1",
                "seen_accessions": [accession],
            },
        )
        self.assertEqual(preview["persisted_accession_count"], 1)

        migrated = self.repository.migrate_config(
            "sec_filings",
            expected_config_version=old_config,
            new_config_version=new_config,
            expected_state_version=state["state_version"],
            next_checkpoint=preview["next_checkpoint"],
        )
        fetcher = SecFixtureFetcher()
        monitor = SecFilingsSourceAdapter(
            adapter=SecEdgarAdapter(
                user_agent="AI Studio monitor@example.com",
                fetch_json=fetcher,
                clock=lambda: FIXED_NOW,
                allowed_symbols=["US.NVDA"],
            ),
            allowed_symbols=["US.NVDA"],
            allowed_forms=["8-K"],
        )
        replay = monitor.poll(
            migrated["checkpoint"],
            observed_at_ms=FIXED_NOW_MS,
            max_items=50,
        )
        self.assertEqual(replay.observed_items, ())
        self.assertEqual(replay.duplicate_count, 1)
        self.assertEqual(replay.next_checkpoint, migrated["checkpoint"])

    def test_sec_v1_to_v2_migration_prefers_an_exact_receipt_over_wall_clock_order(self) -> None:
        old_config = "sec_filings_config_v1_fixture"
        new_config = "sec_filings_config_v2_fixture"
        accession = "0001045810-26-000001"
        self.repository.get_or_create_state(
            "sec_filings",
            config_version=old_config,
        )
        self.repository.set_enabled(
            "sec_filings",
            config_version=old_config,
            enabled=True,
        )
        started_at = self.clock[0]
        started = self.repository.start_run(
            "sec_filings",
            config_version=old_config,
        )
        self.clock[0] = started_at + 500
        imported = self.import_sec_for_run(
            started["run"]["run_id"],
            accession=accession,
        )
        self.assertEqual(imported["created_item_count"], 1)

        self.clock[0] = started_at - 1
        completed = self.repository.complete_run(
            started["run"]["run_id"],
            next_checkpoint={
                "version": "sec_filings_checkpoint_v1",
                "seen_accessions": [accession],
            },
            status=RUN_STATUS_DEGRADED,
            observed_count=1,
            accepted_count=1,
            duplicate_count=0,
            rejected_count=0,
            next_due_at_ms=started_at + 60_000,
            receipt_id=imported["import_id"],
        )
        self.assertLess(
            completed["run"]["completed_at_ms"],
            imported["receipt"]["received_at_ms"],
        )
        self.assertEqual(completed["state"]["checkpoint"], {})
        disabled = self.repository.set_enabled(
            "sec_filings",
            config_version=old_config,
            enabled=False,
        )

        preview = self.repository.preview_sec_filings_v1_to_v2_migration(
            expected_config_version=old_config,
            new_config_version=new_config,
            expected_state_version=disabled["state_version"],
        )
        self.assertEqual(
            preview["next_checkpoint"],
            {
                "version": "sec_filings_checkpoint_v1",
                "seen_accessions": [accession],
            },
        )
        self.assertEqual(preview["persisted_accession_count"], 1)

    def test_config_drift_and_checkpoint_tamper_fail_closed(self) -> None:
        original = self.repository.get_or_create_state(
            self.adapter_key,
            config_version=self.config_version,
        )
        with self.assertRaises(SourceMonitoringStateError) as config_error:
            self.repository.get_or_create_state(
                self.adapter_key,
                config_version="fake_official_config_v2",
            )
        self.assertEqual(config_error.exception.code, "SOURCE_MONITORING_CONFIG_CONFLICT")

        migrated = self.repository.migrate_config(
            self.adapter_key,
            expected_config_version=self.config_version,
            new_config_version="fake_official_config_v2",
            expected_state_version=original["state_version"],
            next_checkpoint={"cursor": "explicitly_migrated"},
        )
        self.assertFalse(migrated["enabled"])
        self.assertEqual(migrated["config_version"], "fake_official_config_v2")
        self.assertEqual(
            migrated["checkpoint"],
            {"cursor": "explicitly_migrated"},
        )
        self.assertEqual(migrated["etag"], "")
        self.assertEqual(migrated["last_modified"], "")

        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute(
                "UPDATE source_adapter_states SET checkpoint_json='{\"cursor\":1}' WHERE adapter_key=?",
                (self.adapter_key,),
            )
        with self.assertRaises(SourceMonitoringStateError) as tamper_error:
            self.repository.get_state(self.adapter_key)
        self.assertEqual(tamper_error.exception.code, "SOURCE_MONITORING_RECORD_CORRUPT")


if __name__ == "__main__":
    unittest.main()
