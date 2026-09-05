from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import sqlite3
import subprocess
import sys
from contextlib import ExitStack, closing
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


HISTORICAL_READER_COMMITS = (
    "67fdb4ad548059506302298ee4d87846abfcece9",
    "25f61d00e3ec49e9034dfc3139033e4ff3b3487e",
)
REQUIRED_READER_TEST_IDS = tuple(
    "tests.test_release_drill.ReleaseReaderDataContractTests." + name
    for name in (
        "test_actual_current_reader_preserves_rooms_materials_q4_neutral_and_research_draft",
        "test_real_67_and_25_readers_distinguish_legacy_data_from_current_formats_and_block_both_switches",
        "test_committed_wal_record_is_checked_without_changing_source_family",
    )
)
_HISTORICAL_READERS_REQUIRED = False
_READER_MATRIX_ROWS: list[dict] = []


def _reader_git(source: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *arguments], cwd=source.parent,
        env={**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0"},
        capture_output=True, text=True, encoding="utf-8", check=False, timeout=20,
    )


def _historical_reader_available(source: Path, commit: str) -> bool:
    return _reader_git(source, "cat-file", "-e", commit + "^{commit}").returncode == 0


def prepare_required_reader_matrix(source: Path) -> dict:
    """Check objects in the actual isolated runner source, without fetching."""
    global _HISTORICAL_READERS_REQUIRED
    _HISTORICAL_READERS_REQUIRED = True
    _READER_MATRIX_ROWS.clear()
    for commit in HISTORICAL_READER_COMMITS:
        if not _historical_reader_available(source, commit):
            raise AssertionError("HISTORICAL_READER_OBJECT_REQUIRED:" + commit)
    head = _reader_git(source, "rev-parse", "--verify", "HEAD")
    if head.returncode or len(head.stdout.strip()) != 40:
        raise AssertionError("HISTORICAL_READER_CANDIDATE_REQUIRED")
    tested_commit = head.stdout.strip()
    candidate = os.environ.get("AI_STUDIO_READER_CANDIDATE_SHA", tested_commit)
    if (len(candidate) != 40 or any(char not in "0123456789abcdef" for char in candidate)
            or not _historical_reader_available(source, candidate)
            or _reader_git(source, "merge-base", "--is-ancestor", candidate, tested_commit).returncode):
        raise AssertionError("HISTORICAL_READER_CANDIDATE_MISMATCH")
    return {
        "candidate_sha": candidate,
        "tested_commit_sha": tested_commit,
        "tested_tree_sha": _reader_git(source, "rev-parse", "HEAD^{tree}").stdout.strip(),
        "source_worktree_clean": _reader_git(source, "diff", "--quiet", "HEAD", "--", ".").returncode == 0,
        "historical_reader_shas": list(HISTORICAL_READER_COMMITS),
        "fixture_id": "release_reader_data_contract_v1",
        "fixture_generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_objects_verified_in_runner": True,
    }


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


    @unittest.skipUnless(os.name == "nt", "Windows native short-path normalization")
    def test_native_windows_short_path_is_same_unaliased_temp_database(self) -> None:
        import ctypes
        from ctypes import wintypes
        from scripts.run_isolated_release_drill import _temporary_database
        from scripts.create_versioned_source_backup import _assert_existing_chain_has_no_links

        get_short_path = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
        get_short_path.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
        get_short_path.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = get_short_path(str(self.database_path), buffer, len(buffer))
        self.assertGreater(length, 0, "native Windows short-path lookup failed")
        self.assertLess(length, len(buffer))
        short = Path(buffer.value)
        canonical = self.database_path.resolve()
        self.assertNotEqual(short, canonical, "this Windows regression needs an actual 8.3 spelling")
        self.assertTrue(os.path.samefile(short, canonical))
        _assert_existing_chain_has_no_links(short)
        print("windows_short_path_evidence=same_file_with_no_reparse_components", flush=True)
        before = _database_family_state(canonical)
        self.assertEqual(_temporary_database(short), canonical)
        # Reproduce the hosted-runner TEMP spelling for the snapshot and its
        # fresh reader process as well as the caller's database path.
        with patch("scripts.run_isolated_release_drill.tempfile.tempdir", str(short.parent)):
            result = check_release_reader(Path(__file__).resolve().parents[1], short)
        self.assertTrue(result["compatible"], result)
        archive = self.make_archive("short-path", "2026-09-06T00:00:00Z", "same data")
        release_root = self.root / "release-root"
        installed = install_release(archive, release_root)
        with patch("scripts.run_isolated_release_drill.check_release_reader", return_value={"compatible": True}):
            first = activate_release(release_root, installed["release_id"],
                                     expected_active_release_id=None, database_path=canonical)
            second = activate_release(release_root, installed["release_id"],
                                      expected_active_release_id=installed["release_id"], database_path=short)
        self.assertEqual(first["database_binding_sha256"], second["database_binding_sha256"])
        self.assertEqual(_database_family_state(canonical), before)

    @unittest.skipUnless(os.name == "nt", "Windows database family link boundaries")
    def test_temp_database_rejects_parent_reparse_and_hardlinked_or_symlinked_family(self) -> None:
        from scripts.run_isolated_release_drill import _temporary_database

        alias_parent = self.root / "alias-parent"
        alias_parent.symlink_to(self.root, target_is_directory=True)
        try:
            with self.assertRaises(ReleaseDrillError):
                _temporary_database(alias_parent / self.database_path.name)
        finally:
            alias_parent.unlink()
        hardlink = self.root / "hardlinked.sqlite3"
        os.link(self.database_path, hardlink)
        try:
            with self.assertRaisesRegex(ReleaseDrillError, "RELEASE_READER_DATABASE_ALIAS_INVALID"):
                _temporary_database(self.database_path)
        finally:
            hardlink.unlink()
        target = self.root / "ordinary-sidecar-target"
        target.write_bytes(b"sidecar fixture")
        wal = Path(str(self.database_path) + "-wal")
        self.assertFalse(wal.exists())
        wal.symlink_to(target)
        try:
            with self.assertRaisesRegex(ReleaseDrillError, "RELEASE_READER_DATABASE_ALIAS_INVALID"):
                _temporary_database(self.database_path)
        finally:
            wal.unlink()


class ReleaseReaderDataContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ai-studio-reader-matrix-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "data.sqlite3"
        self.source = Path(__file__).resolve().parents[1]
        self.provider_stack = ExitStack()
        self.addCleanup(self.provider_stack.close)
        from backend.providers.compatible_chat_provider import CompatibleChatProvider
        from backend.providers.deepseek_provider import DeepSeekProvider
        from backend.providers.doubao_provider import DoubaoProvider
        from backend.providers.openai_provider import OpenAIProvider
        self.provider_spies = [self.provider_stack.enter_context(patch.object(
            cls, method, side_effect=AssertionError("Provider forbidden in reader matrix"),
        )) for cls, method in (
            (CompatibleChatProvider, "generate"), (CompatibleChatProvider, "probe"),
            (OpenAIProvider, "generate"), (OpenAIProvider, "probe"),
            (DoubaoProvider, "generate"), (DoubaoProvider, "generate_json"),
            (DoubaoProvider, "probe"), (DeepSeekProvider, "generate_json"),
        )]
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

    def observe_reader(self, source: Path, reader_sha: str, scenario: str, *, rejected=None) -> dict:
        result = check_release_reader(source, self.database)
        expected = rejected or {}
        with closing(sqlite3.connect(self.database)) as connection:
            schema = connection.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
            ).fetchall()
            counts = {table: connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                      for table in ("provider_execution_runs", "provider_call_attempts", "rounds")}
        row = {
            "scenario": scenario, "reader_sha": reader_sha,
            "schema_sha256": hashlib.sha256(json.dumps(schema, separators=(",", ":")).encode()).hexdigest(),
            "reader_files_sha256": result["reader_files_sha256"],
            "checks": [{"check": key, "status": (
                "EXPECTED_REJECTION" if not value["ok"] and expected.get(key) == value["error_code"]
                else "PASS" if value["ok"] and key not in expected else "FAIL"
            ), "error_code": value["error_code"]} for key, value in result["checks"].items()],
            "network": result["network"], "provider_ledger_and_rounds": counts,
            "provider_attempts": sum(spy.call_count for spy in self.provider_spies),
        }
        _READER_MATRIX_ROWS.append(row)
        self.assertTrue(all(value == 0 for value in counts.values()))
        self.assertEqual(row["provider_attempts"], 0)
        self.assertTrue(all(check["status"] != "FAIL" for check in row["checks"]), row["checks"])
        self.assertEqual(set(expected), {key for key, value in result["checks"].items() if not value["ok"]})
        return result

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
        if not _historical_reader_available(self.source, commit):
            if _HISTORICAL_READERS_REQUIRED:
                self.fail("HISTORICAL_READER_OBJECT_REQUIRED:" + commit)
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
        result = self.observe_reader(self.source, "current", "current_formats")
        self.assertTrue(result["compatible"], result)
        for key in ("rooms", "unfiltered_inbox", "checkpoint:sec_filings", "checkpoint:company_ir",
                    "inbox_item:" + self.legacy_item["id"], "inbox_item:" + self.q4["id"],
                    "material:" + self.attachment["material_id"]):
            self.assertTrue(result["checks"][key]["ok"], key)
        self.assertEqual(_database_family_state(self.database), before)

    def test_real_67_and_25_readers_distinguish_legacy_data_from_current_formats_and_block_both_switches(self) -> None:
        old = self.historical_source("67fdb4ad548059506302298ee4d87846abfcece9")
        minimum = self.historical_source("25f61d00e3ec49e9034dfc3139033e4ff3b3487e")
        self.assertTrue(self.observe_reader(old, HISTORICAL_READER_COMMITS[0], "legacy_data")["compatible"])
        self.assertTrue(self.observe_reader(minimum, HISTORICAL_READER_COMMITS[1], "legacy_data")["compatible"])
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
        rejected = self.observe_reader(old, HISTORICAL_READER_COMMITS[0], "current_formats", rejected={
            "checkpoint:sec_filings": "SEC_FILINGS_CHECKPOINT_INVALID",
            "checkpoint:company_ir": "COMPANY_IR_CHECKPOINT_INVALID",
            "unfiltered_inbox": "SOURCE_INBOX_RECORD_CORRUPT",
            "inbox_item:" + self.q4["id"]: "SOURCE_INBOX_RECORD_CORRUPT",
        })
        self.assertFalse(rejected["compatible"])
        checks = rejected["checks"]
        self.assertEqual(checks["checkpoint:sec_filings"]["error_code"], "SEC_FILINGS_CHECKPOINT_INVALID")
        self.assertEqual(checks["checkpoint:company_ir"]["error_code"], "COMPANY_IR_CHECKPOINT_INVALID")
        self.assertEqual(checks["unfiltered_inbox"]["error_code"], "SOURCE_INBOX_RECORD_CORRUPT")
        self.assertEqual(checks["inbox_item:" + self.q4["id"]]["error_code"], "SOURCE_INBOX_RECORD_CORRUPT")
        for key in ("rooms", "material:" + self.material["id"], "inbox_item:" + self.legacy_item["id"],
                    "material:" + self.attachment["material_id"]):
            self.assertTrue(checks[key]["ok"], checks[key])
        self.assertTrue(self.observe_reader(minimum, HISTORICAL_READER_COMMITS[1], "current_formats")["compatible"])

        upgraded = activate_release(release_root, receipts[1]["release_id"],
                                    expected_active_release_id=first["active_release_id"], database_path=self.database)
        receipt_files = sorted(path.name for path in (release_root / "receipts").iterdir())
        release_files = [release_root / "current-release.json", *(release_root / "receipts").iterdir()]
        release_bytes = {str(path.relative_to(release_root)): path.read_bytes() for path in release_files}
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
        self.assertEqual({str(path.relative_to(release_root)): path.read_bytes() for path in release_files}, release_bytes)
        self.assertEqual(_database_family_state(self.database), before)
        _READER_MATRIX_ROWS.append({
            "scenario": "activation_and_rollback_rejections", "reader_sha": HISTORICAL_READER_COMMITS[0],
            "checks": [{"check": check, "status": "EXPECTED_REJECTION"} for check in (
                "incompatible_rollback", "incompatible_activate", "unrelated_empty_database")],
            "pointer_unchanged": True, "receipts_unchanged": True, "database_family_unchanged": True,
        })

    def test_committed_wal_record_is_checked_without_changing_source_family(self) -> None:
        with closing(self.store._connect()) as writer:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            wal_room = self.store.create_room(
                "Committed WAL reader fixture", "Only the WAL contains this room", capability_pack_ids=[],
            )["room"]
            self.assertGreater(Path(str(self.database) + "-wal").stat().st_size, 0)
            before = _database_family_state(self.database)
            result = self.observe_reader(self.source, "current", "committed_wal")
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


class RequiredHistoricalReaderModeTests(unittest.TestCase):
    def test_missing_objects_are_optional_only_outside_required_mode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ai-studio-missing-history-") as directory:
            root = Path(directory)
            source = root / "ai_collaboration_studio"
            source.mkdir()
            subprocess.run(["git", "init", "--quiet", str(root)], check=True, capture_output=True)
            case = ReleaseReaderDataContractTests()
            case.source, case.root = source, root
            with patch(__name__ + "._HISTORICAL_READERS_REQUIRED", False):
                with self.assertRaises(unittest.SkipTest):
                    case.historical_source(HISTORICAL_READER_COMMITS[0])
            with patch(__name__ + "._HISTORICAL_READERS_REQUIRED", True):
                with self.assertRaisesRegex(AssertionError, "HISTORICAL_READER_OBJECT_REQUIRED"):
                    case.historical_source(HISTORICAL_READER_COMMITS[0])

    def test_required_cli_fails_in_real_source_only_archive_with_a_failure_receipt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ai-studio-required-history-package-") as directory:
            root = Path(directory)
            archive = create_backup(
                source_root=Path(__file__).resolve().parents[1], destination_root=root / "source-only",
                source_root_label="required_history_negative_fixture", created_at_utc="2026-09-06T00:00:00Z",
            )
            from scripts.run_fresh_source_smoke import safe_extract
            source = root / "unpacked"
            safe_extract(archive, source)
            report = root / "matrix.json"
            completed = subprocess.run(
                [sys.executable, "-B", "scripts/run_backend_tests_isolated.py",
                 "tests.test_release_drill.ReleaseReaderDataContractTests",
                 "--require-historical-readers", "--historical-reader-report", str(report)],
                cwd=source, capture_output=True, text=True, encoding="utf-8", timeout=30,
                env={**os.environ, "GIT_NO_LAZY_FETCH": "1",
                     "AI_STUDIO_READER_CANDIDATE_SHA": "deliberately-invalid-private-value"}, check=False,
            )
            self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
            receipt = json.loads(report.read_text(encoding="utf-8"))
            self.assertFalse(receipt["ok"])
            self.assertEqual(receipt["candidate_sha"], "unavailable")
            self.assertNotIn("deliberately-invalid-private-value", report.read_text(encoding="utf-8"))
            self.assertEqual(receipt["suite_tests_run"], 0)
            self.assertEqual(receipt["skip_count"], 0)
            self.assertTrue(receipt["preparation_error"].startswith("HISTORICAL_READER_OBJECT_REQUIRED:"))
            self.assertEqual(receipt["matrix"], [])
            self.assertEqual(receipt["network"]["blocked_attempt_count"], 0)
            self.assertEqual(receipt["network"]["child_blocked_attempt_count"], 0)


if __name__ == "__main__":
    unittest.main()
