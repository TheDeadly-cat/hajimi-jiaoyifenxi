"""The trial scope is shared by production construction, preview and execution."""
from __future__ import annotations

import copy
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from backend.market.ir_releases import OfficialIrReleaseAdapter
from backend.market.sec_edgar import SecEdgarAdapter
from backend.source_inbox_service import SourceInboxService
from backend.source_monitoring.adapters.company_ir import CompanyIrSourceAdapter
from backend.source_monitoring.adapters.sec_filings import SecFilingsSourceAdapter
from backend.source_monitoring.contracts import SourceMonitoringContractError, canonical_sha256
from backend.source_monitoring.default_registry import build_official_source_registry
from backend.source_monitoring.health_service import SourceMonitoringHealthService
from backend.source_monitoring.operator_service import SourceMonitoringOperatorService
from backend.source_monitoring.profiles import SEC_MICRON_TRIAL_PROFILE, require_profile_registry, source_profile_manifest
from backend.source_monitoring.registry import SourceAdapterRegistry
from backend.source_monitoring.runtime import build_source_monitoring_runtime
from backend.source_monitoring.settings import SOURCE_MONITOR_PROFILE_ENV, SourceMonitoringSettings, SourceMonitoringSettingsError
from backend.source_monitoring.state_repository import SourceMonitoringStateRepository
from backend.source_monitoring.supervisor import SourceMonitoringSupervisor
from backend.source_monitoring_cli import SourceMonitoringCliDependencies, _preview
from backend.store import StudioStore
from tests.test_source_monitoring_micron_json import MicronJsonFixtureTransport
from tests.test_source_monitoring_sec_baseline import MutableSecRecentFetcher, NOW, NOW_MS


class MonitoringProfileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="studio-profile-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "fixture.sqlite3"
        self.store = StudioStore(self.path)
        self.settings = SourceMonitoringSettings(
            enabled=True, dry_run=False, source_profile=SEC_MICRON_TRIAL_PROFILE,
        )

    def registry(self, *, symbols=("US.NVDA",), forms=("8-K",), ir_limit=8):
        self.sec_fetcher = MutableSecRecentFetcher(13)
        self.ir_fetcher = MicronJsonFixtureTransport(30)
        sec = SecFilingsSourceAdapter(
            adapter=SecEdgarAdapter(
                user_agent="Offline profile fixture@example.com", fetch_json=self.sec_fetcher,
                allowed_symbols=symbols, clock=lambda: NOW,
            ), allowed_symbols=symbols, allowed_forms=forms, per_symbol_limit=3,
            poll_interval_ms=300_000, force=True,
        )
        ir = CompanyIrSourceAdapter(
            adapter=OfficialIrReleaseAdapter(
                source_format="q4_json", micron_fetch_bytes=self.ir_fetcher, clock=lambda: NOW,
            ), symbols=["US.MU"], per_symbol_limit=ir_limit, poll_interval_ms=300_000,
            force=True, receipt_clock=lambda: NOW,
        )
        return SourceAdapterRegistry((sec, ir), official_only=True)

    def counts(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("source_inbox_items", "provider_execution_runs", "provider_call_attempts", "rounds")}

    def test_only_the_closed_profile_name_is_accepted_and_conflicts_do_not_widen_it(self):
        self.assertEqual(SourceMonitoringSettings.from_environment({SOURCE_MONITOR_PROFILE_ENV: SEC_MICRON_TRIAL_PROFILE}).source_profile, SEC_MICRON_TRIAL_PROFILE)
        for changes in (
            {"source_profile": "https://example.com"}, {"source_profile": True},
            {"allow_readonly_market": True}, {"official_only": False, "allow_readonly_market": True},
            {"initial_mode": "from_time", "from_time": "2026-09-01T00:00:00Z"},
            {"continuous_event_cutoff": "2026-09-01T00:00:00Z"}, {"max_items_per_run": 7},
        ):
            with self.subTest(changes=changes), self.assertRaises(SourceMonitoringSettingsError):
                SourceMonitoringSettings(**{"source_profile": SEC_MICRON_TRIAL_PROFILE, **changes})
        self.assertNotIn("source_profile", SourceMonitoringSettings().to_dict())

    def test_production_registry_constructs_only_the_profile_scope_without_polling(self):
        with patch.object(SecEdgarAdapter, "monitoring_filings_batch", side_effect=AssertionError("no network construction")), \
             patch.object(OfficialIrReleaseAdapter, "monitoring_releases_batch", side_effect=AssertionError("no network construction")):
            registry = build_official_source_registry(source_profile=SEC_MICRON_TRIAL_PROFILE)
            require_profile_registry(registry, SEC_MICRON_TRIAL_PROFILE)
        self.assertEqual(set(registry.adapter_keys), {"sec_filings", "company_ir"})
        manifest = source_profile_manifest(SEC_MICRON_TRIAL_PROFILE)
        unsigned = {key: value for key, value in manifest.items() if key != "scope_sha256"}
        self.assertEqual(canonical_sha256(unsigned), manifest["scope_sha256"])
        clone = copy.deepcopy(manifest)
        clone["sources"][0]["symbols"].append("US.MU")
        self.assertNotEqual(clone, source_profile_manifest(SEC_MICRON_TRIAL_PROFILE))

    def test_runtime_operator_and_health_have_the_same_two_production_adapters(self):
        runtime = build_source_monitoring_runtime(self.store, self.settings, clock_ms=lambda: NOW_MS)
        self.assertEqual(set(runtime.scheduler.registry.adapter_keys), {"sec_filings", "company_ir"})
        control = SourceMonitoringOperatorService(
            store=self.store, settings=runtime.settings, registry=runtime.scheduler.registry,
            repository=runtime.repository, clock_ms=lambda: NOW_MS,
        ).control_snapshot()
        health = SourceMonitoringHealthService(self.store, settings=runtime.settings, clock_ms=lambda: NOW_MS).snapshot()
        self.assertEqual(control["version"], "source_monitoring_operator_control_v3")
        self.assertEqual(control["profile"], source_profile_manifest(SEC_MICRON_TRIAL_PROFILE))
        self.assertEqual(health["version"], "source_monitoring_health_service_v4")
        self.assertEqual({row["adapter_key"] for row in health["adapters"]}, set(runtime.scheduler.registry.adapter_keys))
        self.assertEqual(health["settings"]["source_profile"], SEC_MICRON_TRIAL_PROFILE)
        self.assertEqual(self.counts(), {key: 0 for key in self.counts()})

    def test_wrong_scope_is_rejected_before_polling_even_for_injected_registries(self):
        for registry in (self.registry(forms=("10-K",)), self.registry(ir_limit=9), build_official_source_registry()):
            with self.subTest(keys=registry.adapter_keys), self.assertRaises(SourceMonitoringContractError):
                SourceMonitoringOperatorService(store=self.store, settings=self.settings, registry=registry)
            with self.assertRaises(SourceMonitoringContractError):
                SourceMonitoringSupervisor(
                    registry=registry, repository=SourceMonitoringStateRepository(self.store),
                    source_inbox=SourceInboxService(self.store), settings=self.settings,
                )

    def test_existing_cli_preflight_and_runtime_share_profile_and_complete_seed(self):
        registry = self.registry()
        repository = SourceMonitoringStateRepository(self.store, clock_ms=lambda: NOW_MS)
        for key in registry.adapter_keys:
            repository.set_enabled(key, config_version=registry.require(key).config_version, enabled=True)
        before = self.counts()
        deps = SourceMonitoringCliDependencies(
            settings_loader=lambda: self.settings, registry_builder=lambda _settings: registry,
            repository_builder=lambda _store: repository, clock_ms=lambda: NOW_MS,
        )
        previews = {}
        for key in registry.adapter_keys:
            code, preview = _preview(self.store, key, deps)
            self.assertEqual(code, 0, preview)
            self.assertEqual(preview["profile"], source_profile_manifest(SEC_MICRON_TRIAL_PROFILE))
            self.assertEqual(preview["selected_count"], 0)
            previews[key] = preview
        self.assertEqual(self.counts(), before)
        with patch("backend.source_monitoring.runtime.build_official_source_registry", return_value=registry):
            runtime = build_source_monitoring_runtime(self.store, self.settings, clock_ms=lambda: NOW_MS)
        for key in registry.adapter_keys:
            result = runtime.scheduler.supervisor.run_once(key)
            self.assertEqual(result["status"], "SUCCEEDED", result)
            self.assertEqual(result["initialization"]["preview_sha256"], previews[key]["preview_sha256"])
        self.assertEqual(len(repository.get_state("sec_filings")["checkpoint"]["seen_accessions"]), 13)
        self.assertEqual(len(repository.get_state("company_ir")["checkpoint"]["projections"]), 30)
        self.assertEqual(self.counts(), before)

    def test_legacy_all_source_rss_probe_refuses_named_profile_before_runner(self):
        from scripts.run_sec_ir_live_preflight import PREFLIGHT_CONFIRMATION, _run_cli
        output = io.StringIO()
        with patch.dict(os.environ, {SOURCE_MONITOR_PROFILE_ENV: SEC_MICRON_TRIAL_PROFILE}):
            code = _run_cli(["--confirm", PREFLIGHT_CONFIRMATION], output=output,
                            runner=lambda _confirmation: self.fail("must not run broad RSS probe"),
                            production_runner=False, require_isolated_process=False)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["error_code"], "PREFLIGHT_PROFILE_REQUIRES_OPERATOR_PREVIEW")
        from backend.source_monitoring.sec_ir_live_preflight import run_sec_ir_live_preflight, SecIrLivePreflightProfileError
        with patch.dict(os.environ, {SOURCE_MONITOR_PROFILE_ENV: SEC_MICRON_TRIAL_PROFILE}), \
             self.assertRaises(SecIrLivePreflightProfileError):
            run_sec_ir_live_preflight(confirmation=PREFLIGHT_CONFIRMATION)


if __name__ == "__main__":
    unittest.main()
