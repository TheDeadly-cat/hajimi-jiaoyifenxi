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
from backend.store import StudioStore  # noqa: E402
from tests.test_source_inbox_contracts import _packet  # noqa: E402


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

    def import_for_run(self, run_id: str) -> dict:
        packet = copy.deepcopy(_packet())
        packet["source_channel"] = "official_source_monitor"
        packet["source_key"] = self.adapter_key
        packet["external_run_id"] = run_id
        packet["generation"]["channel"] = "official_source_monitor"
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
        self.repository.fail_run(
            rejected_dry["run"]["run_id"],
            error_code="DRY_RUN_REJECTED",
            error_message="Rejected invalid dry-run completion.",
            next_due_at_ms=self.clock[0],
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
