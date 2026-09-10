"""SEC/IR initialization across delivery batches, using real adapters and persistence.

Run with scripts/run_backend_tests_isolated.py. Only the official HTTP transport
is an in-memory fixture; no online-source acceptance is claimed.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from backend.market.ir_releases import OfficialIrReleaseAdapter
from backend.market.sec_edgar import SecEdgarAdapter
from backend.providers.compatible_chat_provider import CompatibleChatProvider
from backend.providers.deepseek_provider import DeepSeekProvider
from backend.providers.doubao_provider import DoubaoProvider
from backend.providers.openai_provider import OpenAIProvider
from backend.source_inbox_service import SourceInboxService
from backend.source_monitoring.adapters.company_ir import CompanyIrSourceAdapter
from backend.source_monitoring.adapters.sec_filings import SecFilingsSourceAdapter
from backend.source_monitoring.contracts import AdapterPollResult, SourceMonitoringContractError
from backend.source_monitoring.initialization import plan_initial_poll, poll_for_initialization
from backend.source_monitoring.registry import SourceAdapterRegistry
from backend.source_monitoring.settings import SourceMonitoringSettings
from backend.source_monitoring.state_repository import SourceMonitoringStateRepository
from backend.source_monitoring.supervisor import SourceMonitoringSupervisor
from backend.store import StudioStore
from tests.test_source_monitoring_ir_baseline import MutableIrRecentFetcher
from tests.test_source_monitoring_micron_json import MicronJsonFixtureTransport
from tests.test_source_monitoring_sec_baseline import MutableSecRecentFetcher, NOW, NOW_MS


class OfficialInitializationCrossBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="studio-sec-initial-cross-batch-")
        self.addCleanup(directory.cleanup)
        self.database_path = Path(directory.name) / "isolated.sqlite3"
        self.store = StudioStore(self.database_path)
        self.source_kind = "sec"
        self.fetcher = MutableSecRecentFetcher(13)
        self.fetcher.records = [
            (index, f"2026-09-04T{index:02d}:00:00Z") for index in range(1, 14)
        ]
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.provider_spies = [
            self.stack.enter_context(patch.object(
                cls, method, side_effect=AssertionError("Provider forbidden in initialization"),
            ))
            for cls, method in (
                (CompatibleChatProvider, "generate"), (CompatibleChatProvider, "probe"),
                (OpenAIProvider, "generate"), (OpenAIProvider, "probe"),
                (DoubaoProvider, "generate"), (DoubaoProvider, "generate_json"),
                (DoubaoProvider, "probe"), (DeepSeekProvider, "generate_json"),
            )
        ]
        self.addCleanup(self.assert_no_models_or_formal_rounds)

    def build(self, settings: SourceMonitoringSettings, *, enable: bool = False, after_import_hook=None):
        # A new adapter, Store, repository and Supervisor share only the persisted
        # temporary database and fake upstream response across a simulated restart.
        store = StudioStore(self.database_path)
        repository = SourceMonitoringStateRepository(store, clock_ms=lambda: NOW_MS)
        if self.source_kind == "sec":
            adapter = SecFilingsSourceAdapter(
                adapter=SecEdgarAdapter(
                    user_agent="Offline fixture fixture@example.com", fetch_json=self.fetcher,
                    clock=lambda: NOW, allowed_symbols=["US.NVDA"],
                ),
                allowed_symbols=["US.NVDA"], allowed_forms=["8-K"],
                per_symbol_limit=3, force=True,
            )
        else:
            transport_kwargs = (
                {"source_format": "rss", "fetch_bytes": self.fetcher}
                if self.source_kind == "rss" else
                {"source_format": "q4_json", "micron_fetch_bytes": self.fetcher}
            )
            adapter = CompanyIrSourceAdapter(
                adapter=OfficialIrReleaseAdapter(clock=lambda: NOW, **transport_kwargs),
                symbols=["US.MU"], per_symbol_limit=3, force=True, receipt_clock=lambda: NOW,
            )
        if enable:
            repository.set_enabled(adapter.adapter_key, config_version=adapter.config_version, enabled=True)
        supervisor = SourceMonitoringSupervisor(
            registry=SourceAdapterRegistry((adapter,)), repository=repository,
            source_inbox=SourceInboxService(store, clock=lambda: NOW_MS / 1_000),
            settings=settings, clock_ms=lambda: NOW_MS,
            event_sink=lambda *_args, **_kwargs: None, after_import_hook=after_import_hook,
        )
        return supervisor, repository, adapter

    def confirmed_catch_up(self, maximum: int = 2) -> SourceMonitoringSettings:
        supervisor, repository, adapter = self.build(SourceMonitoringSettings(
            enabled=True, dry_run=True, initial_mode="catch_up", catch_up_max_items=maximum,
        ), enable=True)
        preview = supervisor.run_once(adapter.adapter_key)
        self.assertEqual(preview["status"], "DRY_RUN", preview)
        self.assertEqual(repository.get_state(adapter.adapter_key)["checkpoint"], {})
        self.assertEqual(self.inbox_ids(), [])
        return SourceMonitoringSettings(
            enabled=True, dry_run=False, initial_mode="catch_up", catch_up_max_items=maximum,
            initial_preview_sha256=preview["initialization"]["preview_sha256"],
        )

    def run_successfully(self, supervisor, count: int = 1) -> None:
        for _ in range(count):
            before = len(self.inbox_ids())
            result = supervisor.run_once("sec_filings" if self.source_kind == "sec" else "company_ir")
            self.assertEqual(result["status"], "SUCCEEDED", result)
            self.assertLessEqual(len(self.inbox_ids()) - before, 3, "initial policy must preserve the delivery cap")

    @staticmethod
    def accession(index: int) -> str:
        return f"0001045810-26-{index:06d}"

    def inbox_ids(self) -> list[str]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            return [row[0] for row in connection.execute(
                "SELECT external_item_id FROM source_inbox_items ORDER BY external_item_id"
            )]

    def persisted_items(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            return connection.execute(
                "SELECT id,server_fingerprint,item_sha256,item_json FROM source_inbox_items ORDER BY id"
            ).fetchall()

    def use_rss(self) -> None:
        self.source_kind = "rss"
        self.fetcher = MutableIrRecentFetcher(13)
        for row in self.fetcher.records:
            row["published"] = f"Fri, 04 Sep 2026 {row['index']:02d}:00:00 +0000"

    def use_json(self) -> None:
        self.source_kind = "json"
        self.fetcher = MicronJsonFixtureTransport(13)

    def ir_items(self):
        return [json.loads(row[3]) for row in self.persisted_items()]

    def ir_index(self, item) -> int:
        if self.source_kind == "rss":
            return int(item["extensions"]["company_ir_v1"]["guid"].rsplit("-", 1)[1])
        return item["extensions"]["company_ir_v2"]["press_release_id"]

    def assert_no_models_or_formal_rounds(self) -> None:
        for spy in self.provider_spies:
            spy.assert_not_called()
        with closing(sqlite3.connect(self.database_path)) as connection:
            for table in ("provider_execution_runs", "provider_call_attempts", "rounds", "source_inbox_round_drafts"):
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)

    def test_catch_up_total_authority_two_excludes_other_batches_and_delivers_late_new_id_once(self) -> None:
        settings = self.confirmed_catch_up()
        supervisor, _repository, _adapter = self.build(settings)
        self.run_successfully(supervisor, 5)
        self.assertEqual(self.inbox_ids(), [self.accession(12), self.accession(13)])

        # A new accession first observed after initialization remains new even
        # when its declared publication precedes every initialized record.
        self.fetcher.records.append((14, "2026-09-01T09:00:00Z"))
        restarted, repository, _adapter = self.build(settings)
        self.run_successfully(restarted)
        self.assertEqual(self.inbox_ids(), [self.accession(index) for index in (12, 13, 14)])
        items = self.persisted_items()
        checkpoint = repository.get_state("sec_filings")["checkpoint"]
        restarted, repository, _adapter = self.build(settings)
        self.run_successfully(restarted, 3)
        self.assertEqual(self.persisted_items(), items)
        self.assertEqual(repository.get_state("sec_filings")["checkpoint"], checkpoint)

    def test_from_time_filters_all_old_batches_without_losing_eligible_backlog_or_late_new_id(self) -> None:
        settings = SourceMonitoringSettings(
            enabled=True, dry_run=False, initial_mode="from_time", from_time="2026-09-04T09:00:00Z",
        )
        supervisor, _repository, _adapter = self.build(settings, enable=True)
        self.run_successfully(supervisor)
        restarted, _repository, _adapter = self.build(settings)
        self.run_successfully(restarted, 4)
        self.assertEqual(self.inbox_ids(), [self.accession(index) for index in range(9, 14)])

        self.fetcher.records.append((14, "2026-09-01T09:00:00Z"))
        self.run_successfully(restarted)
        self.assertEqual(self.inbox_ids(), [self.accession(index) for index in range(9, 15)])
        items = self.persisted_items()
        restarted, _repository, _adapter = self.build(settings)
        self.run_successfully(restarted, 3)
        self.assertEqual(self.persisted_items(), items)

    def test_failed_catch_up_import_preserves_authority_and_checkpoint_then_restarts_without_old_flood(self) -> None:
        settings = self.confirmed_catch_up()
        supervisor, repository, adapter = self.build(settings)
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute("""CREATE TRIGGER fail_cross_batch_initial_import
                AFTER INSERT ON source_inbox_items
                BEGIN SELECT RAISE(ABORT,'injected initialization import failure'); END""")
        failed = supervisor.run_once(adapter.adapter_key)
        self.assertEqual(failed["status"], "FAILED", failed)
        self.assertIn("injected initialization import failure", failed["run"]["error_message"])
        self.assertEqual(repository.get_state(adapter.adapter_key)["checkpoint"], {})
        self.assertIsNone(repository.get_latest_successful_initialization(
            adapter.adapter_key, config_version=adapter.config_version,
        ))
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            for table in ("source_inbox_imports", "source_inbox_items", "source_inbox_state_events"):
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
            connection.execute("DROP TRIGGER fail_cross_batch_initial_import")

        restarted, _repository, _adapter = self.build(settings)
        self.run_successfully(restarted, 5)
        self.assertEqual(self.inbox_ids(), [self.accession(12), self.accession(13)])
        items = self.persisted_items()
        restarted, repository, adapter = self.build(settings)
        self.run_successfully(restarted, 3)
        self.assertEqual(self.persisted_items(), items)
        evidence = repository.get_latest_successful_initialization(
            adapter.adapter_key, config_version=adapter.config_version,
        )
        self.assertEqual(evidence["catch_up_max_items"], 2)
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertGreater(connection.execute(
                "SELECT SUM(duplicate_count) FROM source_adapter_runs WHERE status='SUCCEEDED'"
            ).fetchone()[0], 0)

    def test_sec_catch_up_five_selects_global_newest_and_resumes_three_plus_two_after_restart(self) -> None:
        # Deliberately unsorted upstream order: newest records are not all in the
        # first delivery-sized prefix of the complete initial source snapshot.
        self.fetcher.records = [self.fetcher.records[index - 1] for index in (2, 12, 4, 7, 1, 13, 3, 10, 5, 9, 6, 11, 8)]
        settings = self.confirmed_catch_up(maximum=5)
        supervisor, _repository, _adapter = self.build(settings)
        self.run_successfully(supervisor)
        self.assertEqual(len(self.inbox_ids()), 3)
        self.assertTrue(set(self.inbox_ids()).issubset({self.accession(index) for index in range(9, 14)}))
        restarted, _repository, _adapter = self.build(settings)
        self.run_successfully(restarted)
        self.assertEqual(self.inbox_ids(), [self.accession(index) for index in range(9, 14)])
        rows = self.persisted_items()
        self.run_successfully(restarted, 4)
        self.assertEqual(self.persisted_items(), rows)

    def test_sec_catch_up_preview_seals_authorized_items_beyond_the_first_delivery_batch(self) -> None:
        self.fetcher.records.reverse()
        settings = self.confirmed_catch_up(maximum=5)
        # ID 10 is in the authorized five but outside the first three. Changing
        # its publication after preview must invalidate the same authorization.
        self.fetcher.records = [
            (index, "2026-09-04T10:30:00Z" if index == 10 else published)
            for index, published in self.fetcher.records
        ]
        supervisor, repository, adapter = self.build(settings)
        result = supervisor.run_once(adapter.adapter_key)
        self.assertEqual(result["status"], "FAILED", result)
        self.assertEqual(result["run"]["error_code"], "SOURCE_MONITORING_CATCH_UP_PREVIEW_MISMATCH")
        self.assertEqual(repository.get_state(adapter.adapter_key)["checkpoint"], {})
        self.assertEqual(self.inbox_ids(), [])

    def test_rss_catch_up_five_keeps_total_cap_across_batches_and_restart(self) -> None:
        self.use_rss()
        settings = self.confirmed_catch_up(maximum=5)
        supervisor, _repository, _adapter = self.build(settings)
        self.run_successfully(supervisor)
        self.assertEqual(len(self.ir_items()), 3)
        restarted, _repository, _adapter = self.build(settings)
        self.run_successfully(restarted)
        self.assertEqual(sorted(self.ir_index(item) for item in self.ir_items()), list(range(9, 14)))
        rows = self.persisted_items()
        self.run_successfully(restarted, 4)
        self.assertEqual(self.persisted_items(), rows)

    def test_rss_from_time_exclusions_survive_omission_but_old_publication_revision_is_delivered(self) -> None:
        self.use_rss()
        settings = SourceMonitoringSettings(
            enabled=True, dry_run=False, initial_mode="from_time", from_time="2026-09-04T09:00:00Z",
        )
        supervisor, _repository, _adapter = self.build(settings, enable=True)
        self.run_successfully(supervisor)
        self.assertEqual(len(self.ir_items()), 3)
        restarted, _repository, _adapter = self.build(settings)
        self.run_successfully(restarted, 4)
        self.assertEqual(sorted(self.ir_index(item) for item in self.ir_items()), list(range(9, 14)))
        rows = self.persisted_items()
        original = self.fetcher.records
        self.fetcher.records = original[8:]
        self.run_successfully(restarted)
        self.fetcher.records = original
        self.run_successfully(restarted, 4)
        self.assertEqual(self.persisted_items(), rows)

        self.fetcher.records[1]["summary"] = "Official correction of the older published release."
        self.run_successfully(restarted)
        revision = [item for item in self.ir_items() if self.ir_index(item) == 2]
        self.assertEqual(len(revision), 1)
        self.assertEqual(revision[0]["published_at"], "2026-09-04T02:00:00Z")
        self.assertTrue(revision[0]["extensions"]["company_ir_v1"]["is_revision"])
        self.assertTrue(revision[0]["extensions"]["company_ir_v1"]["previous_rss_projection_sha256"])
        rows = self.persisted_items()
        restarted, _repository, _adapter = self.build(settings)
        self.run_successfully(restarted, 3)
        self.assertEqual(self.persisted_items(), rows)

    def test_json_catch_up_total_two_does_not_replay_other_initial_batches(self) -> None:
        self.use_json()
        settings = self.confirmed_catch_up()
        supervisor, _repository, _adapter = self.build(settings)
        self.run_successfully(supervisor)
        self.assertEqual(len(self.ir_items()), 2)
        rows = self.persisted_items()
        restarted, _repository, _adapter = self.build(settings)
        self.run_successfully(restarted, 5)
        self.assertEqual(self.persisted_items(), rows)

    def test_json_from_time_exclusions_survive_omission_and_old_reference_revision_replays_once(self) -> None:
        self.use_json()
        # JSON fixture timestamps come from bound NewsArticle metadata, not the
        # timezone-less PressReleaseDate in the list response.
        settings = SourceMonitoringSettings(
            enabled=True, dry_run=False, initial_mode="from_time", from_time="2026-09-04T16:00:00Z",
        )
        supervisor, _repository, _adapter = self.build(settings, enable=True)
        self.run_successfully(supervisor)
        self.assertEqual(self.ir_items(), [])
        restarted, _repository, _adapter = self.build(settings)
        self.run_successfully(restarted, 4)
        self.assertEqual(self.ir_items(), [])
        original = self.fetcher.records
        self.fetcher.records = original[8:]
        self.run_successfully(restarted)
        self.fetcher.records = original
        self.run_successfully(restarted, 4)
        self.assertEqual(self.ir_items(), [])

        self.fetcher.records[1]["RevisionNumber"] = 2
        self.fetcher.records[1]["ShortDescription"] = "Official correction of an older reporting reference."
        self.run_successfully(restarted)
        self.assertEqual(len(self.ir_items()), 1)
        revision = self.ir_items()[0]
        self.assertEqual(self.ir_index(revision), 2)
        self.assertEqual(revision["published_at"], "2026-09-04T15:01:00Z")
        self.assertTrue(revision["extensions"]["company_ir_v2"]["is_revision"])
        self.assertTrue(revision["extensions"]["company_ir_v2"]["previous_projection_sha256"])
        rows = self.persisted_items()
        restarted, _repository, _adapter = self.build(settings)
        self.run_successfully(restarted, 3)
        self.assertEqual(self.persisted_items(), rows)

    def crash_after_committed_catch_up_import(self):
        settings = self.confirmed_catch_up()

        def crash(_run_id, _import):
            raise SystemExit("injected crash after committed initial import")

        supervisor, repository, adapter = self.build(settings, after_import_hook=crash)
        with self.assertRaisesRegex(SystemExit, "injected crash after committed initial import"):
            supervisor.run_once(adapter.adapter_key)
        self.assertEqual(self.inbox_ids(), [self.accession(12), self.accession(13)])
        self.assertEqual(repository.get_state(adapter.adapter_key)["checkpoint"], {})
        self.assertIsNone(repository.get_latest_successful_initialization(
            adapter.adapter_key, config_version=adapter.config_version,
        ))
        self.assertIn("RUNNING", {run["status"] for run in repository.list_runs(adapter_key=adapter.adapter_key)})
        return settings

    def test_initial_catch_up_crash_after_import_replays_same_authority_without_duplicate_main_items(self) -> None:
        settings = self.crash_after_committed_catch_up_import()
        rows = self.persisted_items()
        restarted, repository, adapter = self.build(settings)
        replay = restarted.run_once(adapter.adapter_key)
        self.assertEqual(replay["status"], "SUCCEEDED", replay)
        self.assertEqual(replay["import"]["created_item_count"], 0)
        self.assertEqual(replay["import"]["duplicate_item_count"], 2)
        self.assertEqual(self.persisted_items(), rows)
        self.assertIn("ABANDONED", {run["status"] for run in repository.list_runs(adapter_key=adapter.adapter_key)})
        self.run_successfully(restarted, 5)
        self.assertEqual(self.persisted_items(), rows)

    def test_initial_catch_up_crash_rejects_changed_undelivered_scope_before_reimport(self) -> None:
        settings = self.crash_after_committed_catch_up_import()
        rows = self.persisted_items()
        with closing(sqlite3.connect(self.database_path)) as connection:
            imports_before = connection.execute("SELECT COUNT(*) FROM source_inbox_imports").fetchone()[0]
        self.fetcher.records = [
            (index, "2026-09-04T10:30:00Z" if index == 10 else published)
            for index, published in self.fetcher.records
        ]
        restarted, repository, adapter = self.build(settings)
        replay = restarted.run_once(adapter.adapter_key)
        self.assertEqual(replay["status"], "FAILED", replay)
        self.assertEqual(replay["run"]["error_code"], "SOURCE_MONITORING_CATCH_UP_PREVIEW_MISMATCH")
        self.assertEqual(self.persisted_items(), rows)
        self.assertEqual(repository.get_state(adapter.adapter_key)["checkpoint"], {})
        self.assertIsNone(repository.get_latest_successful_initialization(
            adapter.adapter_key, config_version=adapter.config_version,
        ))
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_inbox_imports").fetchone()[0], imports_before)

    def test_initial_history_hash_accepts_only_empty_or_native_lowercase_sha256(self) -> None:
        class DigestText(str):
            pass

        for digest in (None, False, 123, b"a" * 64, "a" * 63, "a" * 65, "A" * 64,
                       "g" * 64, "a" * 63 + "\n", DigestText("a" * 64)):
            with self.subTest(digest_type=type(digest).__name__, digest_length=len(digest) if hasattr(digest, "__len__") else None):
                with self.assertRaises(SourceMonitoringContractError) as caught:
                    AdapterPollResult.build(
                        "sec_filings", {}, {}, (), captured_at_ms=NOW_MS,
                        initial_history_sha256=digest,
                    )
                self.assertEqual(caught.exception.code, "SOURCE_MONITORING_INITIAL_HISTORY_INVALID")
        for digest in ("", "a" * 64, "0" * 64):
            result = AdapterPollResult.build(
                "sec_filings", {}, {}, (), captured_at_ms=NOW_MS,
                initial_history_sha256=digest,
            )
            self.assertEqual(result.initial_history_sha256, digest)

    def test_healthy_initial_sec_and_ir_planner_rejects_a_missing_complete_history_hash(self) -> None:
        for source_kind in ("sec", "rss", "json"):
            if source_kind == "rss":
                self.use_rss()
            elif source_kind == "json":
                self.use_json()
            for mode in ("catch_up", "from_time"):
                with self.subTest(source_kind=source_kind, mode=mode):
                    settings = SourceMonitoringSettings(
                        enabled=True, dry_run=False, initial_mode=mode,
                        **({"catch_up_max_items": 2} if mode == "catch_up" else
                           {"from_time": "2026-09-04T09:00:00Z"}),
                    )
                    supervisor, _repository, adapter = self.build(settings)
                    result = poll_for_initialization(
                        adapter, {}, initial_required=True,
                        initialization_policy=settings.initialization_policy_for(official_source=True),
                        observed_at_ms=NOW_MS,
                    )
                    self.assertEqual(result.source_errors, ())
                    self.assertRegex(result.initial_history_sha256, r"^[0-9a-f]{64}$")
                    without_seal = replace(result, initial_history_sha256="")
                    with self.assertRaises(SourceMonitoringContractError) as caught:
                        plan_initial_poll(
                            without_seal, metadata=supervisor.registry.metadata_for(adapter.adapter_key),
                            settings=settings, initial_required=True, received_at_ms=NOW_MS,
                        )
                    self.assertEqual(caught.exception.code, "SOURCE_MONITORING_INITIAL_HISTORY_INVALID")

    def assert_initial_source_error_degrades_without_a_history_hash(self) -> None:
        settings = SourceMonitoringSettings(
            enabled=True, dry_run=False, initial_mode="from_time", from_time="2026-09-04T09:00:00Z",
        )
        supervisor, repository, adapter = self.build(settings, enable=True)
        result = supervisor.run_once(adapter.adapter_key)
        self.assertEqual(result["status"], "DEGRADED", result)
        self.assertEqual(result["initialization"]["outcome"], "blocked")
        self.assertTrue(result["run"]["source_errors"])
        self.assertNotEqual(result["run"]["error_code"], "SOURCE_MONITORING_INITIAL_HISTORY_INVALID")
        self.assertEqual(repository.get_state(adapter.adapter_key)["checkpoint"], {})
        self.assertEqual(self.inbox_ids(), [])

    def test_initial_sec_source_error_is_degraded_without_a_complete_history_hash(self) -> None:
        self.fetcher.truncate_documents = True
        self.assert_initial_source_error_degrades_without_a_history_hash()

    def test_initial_ir_source_error_is_degraded_without_a_complete_history_hash(self) -> None:
        self.use_rss()
        self.fetcher.records[0]["published"] = "invalid publication date"
        self.assert_initial_source_error_degrades_without_a_history_hash()

    def test_sec_history_over_fifty_keeps_cutoff_backlog_and_post_snapshot_late_accession_after_restart(self) -> None:
        self.fetcher = MutableSecRecentFetcher(63)
        self.fetcher.records = [
            (index, "2026-09-01T09:00:00Z") for index in range(1, 51)
        ] + [
            (index, f"2026-09-04T09:{index - 51:02d}:00Z") for index in range(51, 64)
        ]
        self.fetcher.after_snapshot = lambda: self.fetcher.records.append(
            (64, "2026-09-01T09:00:00Z")
        )
        settings = SourceMonitoringSettings(
            enabled=True, dry_run=False, initial_mode="from_time", from_time="2026-09-04T09:00:00Z",
        )
        supervisor, repository, _adapter = self.build(settings, enable=True)
        self.run_successfully(supervisor)
        self.assertEqual(len(self.inbox_ids()), 3)
        first_seen = repository.get_state("sec_filings")["checkpoint"]["seen_accessions"]
        self.assertTrue({self.accession(index) for index in range(1, 51)}.issubset(first_seen))
        self.assertNotIn(self.accession(64), first_seen)

        restarted, repository, _adapter = self.build(settings)
        self.run_successfully(restarted, 6)
        self.assertEqual(self.inbox_ids(), [self.accession(index) for index in range(51, 65)])
        checkpoint = repository.get_state("sec_filings")["checkpoint"]
        self.assertEqual(set(checkpoint["seen_accessions"]), {self.accession(index) for index in range(1, 65)})
        self.assertEqual(len(checkpoint["seen_accessions"]), 64)
        rows = self.persisted_items()
        restarted, repository, _adapter = self.build(settings)
        self.run_successfully(restarted, 2)
        self.assertEqual(self.persisted_items(), rows)
        self.assertEqual(repository.get_state("sec_filings")["checkpoint"], checkpoint)
