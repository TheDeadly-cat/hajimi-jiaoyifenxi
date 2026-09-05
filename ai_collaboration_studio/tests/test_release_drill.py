from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
import sqlite3
import subprocess
import sys
from contextlib import closing
from unittest.mock import patch

from scripts.create_versioned_source_backup import create_backup
from scripts.run_isolated_release_drill import (
    ReleaseDrillError,
    activate_release,
    build_synthetic_failure_receipt,
    check_release_reader,
    install_release,
    read_activation_pointer,
    rollback_release,
    run_drill,
    _database_family_state,
)


class ReleaseLifecycleContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-release-lifecycle-test-",
            ignore_cleanup_errors=True,
        )
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "application.sqlite3"
        from backend.store import StudioStore
        StudioStore(self.database_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def make_archive(self, name: str, created_at: str, marker: str) -> Path:
        source = self.root / f"source-{name}"
        (source / "frontend").mkdir(parents=True)
        (source / "server.py").write_text("pass\n", encoding="utf-8")
        (source / "README.md").write_text(marker + "\n", encoding="utf-8")
        (source / "requirements-lock-win-py314.txt").write_text(
            "fixture==1 --hash=sha256:" + ("0" * 64) + "\n",
            encoding="ascii",
        )
        (source / "frontend" / "package.json").write_text(
            json.dumps({"name": "fixture", "version": name}) + "\n",
            encoding="utf-8",
        )
        return create_backup(
            source_root=source,
            destination_root=self.root / f"archive-{name}",
            source_root_label=f"fixture_{name}",
            created_at_utc=created_at,
        )

    @patch("scripts.run_isolated_release_drill.check_release_reader", return_value={"compatible": True})
    def test_install_upgrade_and_explicit_rollback_publish_exact_generations(self, reader_check) -> None:
        baseline = self.make_archive("baseline", "2026-08-20T00:00:00Z", "one")
        current = self.make_archive("current", "2026-08-20T00:00:02Z", "two")
        release_root = self.root / "release-root"
        baseline_receipt = install_release(baseline, release_root)
        current_receipt = install_release(current, release_root)

        first = activate_release(
            release_root,
            baseline_receipt["release_id"],
            expected_active_release_id=None,
            database_path=self.database_path,
        )
        upgraded = activate_release(
            release_root,
            current_receipt["release_id"],
            expected_active_release_id=baseline_receipt["release_id"],
            database_path=self.database_path,
        )
        rolled_back = rollback_release(
            release_root,
            failed_release_id=current_receipt["release_id"],
            target_release_id=baseline_receipt["release_id"],
            expected_generation=2,
            failure_receipt=build_synthetic_failure_receipt(
                current_receipt["release_id"]
            ),
            database_path=self.database_path,
        )

        self.assertEqual(first["generation"], 1)
        self.assertEqual(upgraded["generation"], 2)
        self.assertEqual(rolled_back["generation"], 3)
        self.assertEqual(
            read_activation_pointer(release_root),
            rolled_back,
        )
        self.assertEqual(
            rolled_back["active_release_id"],
            baseline_receipt["release_id"],
        )
        self.assertEqual(reader_check.call_count, 3)
        self.assertTrue(all(call.args[1] == self.database_path for call in reader_check.call_args_list))
        with self.assertRaisesRegex(ReleaseDrillError, "already exists"):
            install_release(baseline, release_root)

    @patch("scripts.run_isolated_release_drill.check_release_reader", return_value={"compatible": True})
    def test_stale_activation_and_inexact_failure_receipt_fail_closed(self, _reader_check) -> None:
        baseline = self.make_archive("baseline", "2026-08-20T00:00:00Z", "one")
        current = self.make_archive("current", "2026-08-20T00:00:02Z", "two")
        release_root = self.root / "release-root"
        baseline_receipt = install_release(baseline, release_root)
        current_receipt = install_release(current, release_root)
        activate_release(
            release_root,
            baseline_receipt["release_id"],
            expected_active_release_id=None,
            database_path=self.database_path,
        )
        with self.assertRaisesRegex(ReleaseDrillError, "changed before upgrade"):
            activate_release(
                release_root,
                current_receipt["release_id"],
                expected_active_release_id=None,
                database_path=self.database_path,
            )
        upgraded = activate_release(
            release_root,
            current_receipt["release_id"],
            expected_active_release_id=baseline_receipt["release_id"],
            database_path=self.database_path,
        )
        wrong = build_synthetic_failure_receipt(baseline_receipt["release_id"])
        with self.assertRaisesRegex(ReleaseDrillError, "does not authorize"):
            rollback_release(
                release_root,
                failed_release_id=current_receipt["release_id"],
                target_release_id=baseline_receipt["release_id"],
                expected_generation=upgraded["generation"],
                failure_receipt=wrong,
                database_path=self.database_path,
            )
        self.assertEqual(
            read_activation_pointer(release_root)["active_release_id"],
            current_receipt["release_id"],
        )

    def test_project_drill_is_synthetic_offline_and_preserves_application_data(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        with patch.dict(os.environ, {"AI_STUDIO_SKIP_LOCAL_ENV": "1"}):
            result = run_drill(project_root)

        self.assertTrue(result["ok"])
        self.assertEqual(result["schema_version"], "isolated_release_drill_v1")
        self.assertTrue(result["install"]["reinstall_blocked"])
        self.assertTrue(result["activation"]["stale_activation_blocked"])
        self.assertEqual(result["activation"]["rollback_generation"], 3)
        self.assertTrue(result["application_data"]["family_unchanged"])
        boundaries = result["boundaries"]
        self.assertTrue(boundaries["system_temp_only"])
        self.assertTrue(boundaries["synthetic_baseline"])
        self.assertFalse(boundaries["historical_upgrade_compatibility_proven"])
        self.assertFalse(boundaries["application_started"])
        self.assertFalse(boundaries["database_migration_executed"])
        self.assertFalse(boundaries["formal_database_opened"])
        self.assertEqual(boundaries["external_network_requests"], 0)

    @patch("scripts.run_isolated_release_drill.check_release_reader", return_value={"compatible": True})
    def test_database_is_mandatory_and_a_v1_pointer_cannot_claim_a_new_data_binding(self, _reader_check) -> None:
        archive = self.make_archive("current", "2026-09-05T00:00:00Z", "current")
        release_root = self.root / "release-root"
        installed = install_release(archive, release_root)
        with self.assertRaises(TypeError):
            activate_release(release_root, installed["release_id"], expected_active_release_id=None)
        self.assertIsNone(read_activation_pointer(release_root))
        pointer = activate_release(
            release_root, installed["release_id"], expected_active_release_id=None,
            database_path=self.database_path,
        )
        from scripts.run_isolated_release_drill import _sealed
        legacy = {key: value for key, value in pointer.items()
                  if key not in {"database_binding_sha256", "pointer_sha256"}}
        legacy["version"] = "release_activation_pointer_v1"
        path = release_root / "current-release.json"
        path.write_text(json.dumps(_sealed(legacy, "pointer_sha256")), encoding="ascii")
        before = path.read_bytes()
        with self.assertRaisesRegex(ReleaseDrillError, "not closed"):
            activate_release(
                release_root, installed["release_id"], expected_active_release_id=installed["release_id"],
                database_path=self.database_path,
            )
        self.assertEqual(path.read_bytes(), before)


class ReleaseReaderDataContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ai-studio-reader-matrix-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "data.sqlite3"
        self.source = Path(__file__).resolve().parents[1]
        from backend.store import StudioStore
        from backend.source_inbox_service import SourceInboxService
        from tests.test_source_inbox_contracts import _packet
        from tests.test_source_monitoring_sec_baseline import NOW_MS
        self.now_ms = NOW_MS
        self.store = StudioStore(self.database)
        self.inbox = SourceInboxService(self.store, clock=lambda: self.now_ms / 1_000)
        self.room = self.store.create_room(
            "Existing reader matrix room", "Preserve existing research", capability_pack_ids=[],
        )["room"]
        self.material = self.store.add_material(
            self.room["id"], {"title": "Existing ordinary material", "content": "Read this unchanged."},
        )
        self.legacy_item = self.inbox.import_packet(json.dumps(_packet()))["items"][0]

    def seed_current_formats(self) -> None:
        from backend.market.ir_releases import OfficialIrReleaseAdapter
        from backend.source_monitoring.adapters.company_ir import CompanyIrSourceAdapter
        from backend.source_monitoring.packet_builder import build_source_import_payload
        from backend.source_monitoring.state_repository import SourceMonitoringStateRepository
        from backend.source_monitoring.trading_impact_rules import TradingImpactRulesV1
        from tests.test_source_monitoring_micron_json import MicronJsonFixtureTransport
        from tests.test_source_monitoring_sec_baseline import NOW
        adapter = CompanyIrSourceAdapter(
            adapter=OfficialIrReleaseAdapter(
                source_format="q4_json", micron_fetch_bytes=MicronJsonFixtureTransport(1),
                clock=lambda: NOW,
            ),
            symbols=["US.MU"], per_symbol_limit=1, force=True, receipt_clock=lambda: NOW,
        )
        poll = adapter.poll({}, observed_at_ms=self.now_ms)
        self.assertEqual(len(poll.observed_items), 1)
        self.assertEqual(poll.source_errors, ())
        payload = build_source_import_payload(
            adapter_key=adapter.adapter_key, external_run_id="reader-q4-fixture",
            captured_at_ms=self.now_ms, observed_items=poll.observed_items,
        )
        self.q4 = self.inbox.import_packet(
            payload, actor="source_monitoring_worker", impact_rules=TradingImpactRulesV1(),
        )["items"][0]
        ack = self.inbox.acknowledge(
            self.q4["id"], expected_state_version=self.q4["state_version"], acknowledgement=True,
        )
        attached = self.inbox.attach_to_room(
            self.q4["id"], room_id=self.room["id"], expected_state_version=ack["state_version"],
        )
        self.attachment = attached["attachment"]
        draft = self.inbox.create_round_draft(
            self.q4["id"], room_id=self.room["id"],
            expected_state_version=attached["item"]["state_version"],
            objective="Review this fixture without starting research.",
        )
        self.draft = draft["round_draft"]
        self.repository = SourceMonitoringStateRepository(self.store, clock_ms=lambda: self.now_ms)
        for key, checkpoint in {
            "company_ir": poll.next_checkpoint,
            "sec_filings": {"version": "sec_filings_checkpoint_v2", "seen_accessions": ["0001045810-26-000001"]},
        }.items():
            self.repository.set_enabled(key, config_version="reader_fixture_v2", enabled=True)
            run = self.repository.start_run(key, config_version="reader_fixture_v2", dry_run=False)["run"]
            self.repository.complete_run(
                run["run_id"], next_checkpoint=checkpoint, status="SUCCEEDED",
                observed_count=0, accepted_count=0, duplicate_count=0, rejected_count=0,
                next_due_at_ms=self.now_ms + 60_000,
            )
            self.repository.set_enabled(key, config_version="reader_fixture_v2", enabled=False)
        with closing(sqlite3.connect(self.database)) as connection:
            for table in ("provider_execution_runs", "provider_call_attempts", "rounds"):
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_inbox_round_drafts").fetchone()[0], 1)
        projected = self.inbox.get_item(self.q4["id"])
        self.assertEqual(projected["impact_rule_projections"][0]["status"], "NO_MATCH")
        projection = projected["impact_rule_projections"][0]["projection"]
        self.assertEqual(projection["evaluation"], "no_match")
        self.assertEqual(projection["hypotheses"], [])
        self.assertEqual(projection["source_item_binding"]["source_semantic_binding"]["version"],
                         "trading_impact_source_semantics_v2")

    def historical_source(self, commit: str) -> Path:
        environment = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0"}
        exists = subprocess.run(
            ["git", "cat-file", "-e", commit + "^{commit}"], cwd=self.source.parent,
            env=environment, capture_output=True, check=False,
        )
        if exists.returncode:
            self.skipTest("Exact historical reader object is not present locally; no fetch is allowed")
        archive = self.root / (commit + ".zip")
        subprocess.run(
            ["git", "archive", "--format=zip", "--output", str(archive), commit + ":ai_collaboration_studio"],
            cwd=self.source.parent, env=environment, capture_output=True, check=True,
        )
        from scripts.run_fresh_source_smoke import safe_extract
        source = self.root / commit
        safe_extract(archive, source)
        return source

    def test_actual_current_reader_preserves_rooms_materials_q4_neutral_and_research_draft(self) -> None:
        self.seed_current_formats()
        before = _database_family_state(self.database)
        result = check_release_reader(self.source, self.database)
        self.assertTrue(result["compatible"], result)
        for key in ("rooms", "unfiltered_inbox", "checkpoint:sec_filings", "checkpoint:company_ir",
                    "inbox_item:" + self.legacy_item["id"], "inbox_item:" + self.q4["id"],
                    "material:" + self.attachment["material_id"]):
            self.assertTrue(result["checks"][key]["ok"], key)
        self.assertEqual(_database_family_state(self.database), before)

    def test_real_67_and_25_readers_distinguish_legacy_data_from_current_formats_and_block_both_switches(self) -> None:
        old = self.historical_source("67fdb4ad548059506302298ee4d87846abfcece9")
        minimum = self.historical_source("25f61d00e3ec49e9034dfc3139033e4ff3b3487e")
        self.assertTrue(check_release_reader(old, self.database)["compatible"])
        self.assertTrue(check_release_reader(minimum, self.database)["compatible"])
        release_root = self.root / "release-root"
        receipts = []
        for index, source in enumerate((old, minimum)):
            archive = create_backup(
                source_root=source, destination_root=self.root / f"source-archive-{index}",
                source_root_label=f"actual_reader_{index}", created_at_utc=f"2026-09-05T00:00:0{index}Z",
            )
            receipts.append(install_release(archive, release_root))
        first = activate_release(release_root, receipts[0]["release_id"],
                                 expected_active_release_id=None, database_path=self.database)
        # The same application's new writer produces new persisted formats.
        self.seed_current_formats()
        before = _database_family_state(self.database)
        rejected = check_release_reader(old, self.database)
        self.assertFalse(rejected["compatible"])
        checks = rejected["checks"]
        self.assertEqual(checks["checkpoint:sec_filings"]["error_code"], "SEC_FILINGS_CHECKPOINT_INVALID")
        self.assertEqual(checks["checkpoint:company_ir"]["error_code"], "COMPANY_IR_CHECKPOINT_INVALID")
        self.assertEqual(checks["unfiltered_inbox"]["error_code"], "SOURCE_INBOX_RECORD_CORRUPT")
        self.assertEqual(checks["inbox_item:" + self.q4["id"]]["error_code"], "SOURCE_INBOX_RECORD_CORRUPT")
        for key in ("rooms", "material:" + self.material["id"], "inbox_item:" + self.legacy_item["id"],
                    "material:" + self.attachment["material_id"]):
            self.assertTrue(checks[key]["ok"], checks[key])
        self.assertTrue(check_release_reader(minimum, self.database)["compatible"])

        upgraded = activate_release(release_root, receipts[1]["release_id"],
                                    expected_active_release_id=first["active_release_id"], database_path=self.database)
        receipt_files = sorted(path.name for path in (release_root / "receipts").iterdir())
        with self.assertRaisesRegex(ReleaseDrillError, "RELEASE_READER_INCOMPATIBLE"):
            rollback_release(
                release_root, failed_release_id=receipts[1]["release_id"],
                target_release_id=receipts[0]["release_id"], expected_generation=2,
                failure_receipt=build_synthetic_failure_receipt(receipts[1]["release_id"]),
                database_path=self.database,
            )
        with self.assertRaisesRegex(ReleaseDrillError, "RELEASE_READER_INCOMPATIBLE"):
            activate_release(release_root, receipts[0]["release_id"],
                             expected_active_release_id=upgraded["active_release_id"], database_path=self.database)
        # An unrelated empty database cannot be offered as rollback evidence.
        from backend.store import StudioStore
        empty_database = self.root / "unrelated-empty.sqlite3"
        StudioStore(empty_database)
        with self.assertRaisesRegex(ReleaseDrillError, "RELEASE_READER_DATABASE_BINDING_MISMATCH"):
            rollback_release(
                release_root, failed_release_id=receipts[1]["release_id"],
                target_release_id=receipts[0]["release_id"], expected_generation=2,
                failure_receipt=build_synthetic_failure_receipt(receipts[1]["release_id"]),
                database_path=empty_database,
            )
        self.assertEqual(read_activation_pointer(release_root), upgraded)
        self.assertEqual(sorted(path.name for path in (release_root / "receipts").iterdir()), receipt_files)
        self.assertEqual(_database_family_state(self.database), before)

    def test_committed_wal_record_is_checked_without_changing_source_family(self) -> None:
        with closing(self.store._connect()) as writer:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            wal_room = self.store.create_room(
                "Committed WAL reader fixture", "Only the WAL contains this room", capability_pack_ids=[],
            )["room"]
            self.assertGreater(Path(str(self.database) + "-wal").stat().st_size, 0)
            before = _database_family_state(self.database)
            result = check_release_reader(self.source, self.database)
            self.assertTrue(result["compatible"], result)
            self.assertTrue(result["checks"]["room_snapshot:" + wal_room["id"]]["ok"])
            self.assertEqual(_database_family_state(self.database), before)
            command = subprocess.run(
                [sys.executable, "-I", "-B", str(self.source / "scripts" / "run_isolated_release_drill.py"),
                 "--reader-source-root", str(self.source), "--reader-database", str(self.database)],
                capture_output=True, text=True, encoding="utf-8", check=False,
            )
            self.assertEqual(command.returncode, 0, command.stdout + command.stderr)
            cli = json.loads(command.stdout)
            self.assertTrue(cli["checks"]["room_snapshot:" + wal_room["id"]]["ok"])
            self.assertEqual(_database_family_state(self.database), before)

    def test_missing_data_and_unknown_reader_do_not_become_empty_compatible_databases(self) -> None:
        absent = self.root / "missing.sqlite3"
        with self.assertRaisesRegex(ReleaseDrillError, "RELEASE_READER_DATABASE_REQUIRED"):
            check_release_reader(self.source, absent)
        self.assertFalse(absent.exists())
        with self.assertRaisesRegex(ReleaseDrillError, "RELEASE_READER_UNAVAILABLE"):
            check_release_reader(self.root / "missing-reader", self.database)

    def test_legacy_checkpoint_upgrade_is_readable_only_while_source_remains_disabled(self) -> None:
        from backend.source_monitoring.state_repository import SourceMonitoringStateRepository
        repository = SourceMonitoringStateRepository(self.store, clock_ms=lambda: self.now_ms)
        for key, checkpoint in {
            "sec_filings": {"version": "sec_filings_checkpoint_v1", "seen_accessions": ["0001045810-26-000001"]},
            "company_ir": {"version": "company_ir_checkpoint_v1", "projections": [
                {"identity_sha256": "1" * 64, "rss_projection_sha256": "2" * 64},
            ]},
        }.items():
            repository.set_enabled(key, config_version="reader_legacy_v1", enabled=True)
            run = repository.start_run(key, config_version="reader_legacy_v1", dry_run=False)["run"]
            repository.complete_run(
                run["run_id"], next_checkpoint=checkpoint, status="SUCCEEDED",
                observed_count=0, accepted_count=0, duplicate_count=0, rejected_count=0,
                next_due_at_ms=self.now_ms + 60_000,
            )
            repository.set_enabled(key, config_version="reader_legacy_v1", enabled=False)
        before = _database_family_state(self.database)
        result = check_release_reader(self.source, self.database)
        self.assertTrue(result["compatible"], result)
        for key, code in (("sec_filings", "SEC_BASELINE_UPGRADE_REQUIRED"),
                          ("company_ir", "COMPANY_IR_BASELINE_UPGRADE_REQUIRED")):
            self.assertEqual(result["checks"]["checkpoint:" + key]["error_code"], code)
            self.assertTrue(result["checks"]["checkpoint:" + key]["baseline_upgrade_required"])
        self.assertEqual(_database_family_state(self.database), before)
        repository.set_enabled("sec_filings", config_version="reader_legacy_v1", enabled=True)
        active = check_release_reader(self.source, self.database)
        self.assertFalse(active["compatible"])
        self.assertFalse(active["checks"]["checkpoint:sec_filings"]["ok"])


if __name__ == "__main__":
    unittest.main()
