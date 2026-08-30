from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.source_inbox_contracts import (
    PROJECT_SOURCE_ITEM_VERSION,
    SOURCE_IMPORT_PACKET_VERSION,
    normalize_source_import_packet,
)
from backend.source_monitoring.adapters.base import (
    SOURCE_ADAPTER_CONTRACT_VERSION,
    SourceAdapter,
    SourceAdapterContractError,
    validate_source_adapter,
)
from backend.source_monitoring.contracts import AdapterPollResult
from backend.source_monitoring.packet_builder import (
    OFFICIAL_SOURCE_CHANNEL,
    SourcePacketBuildError,
    build_source_import_payload,
    build_packet_from_poll_result,
    build_source_import_packet,
)
from backend.source_monitoring.registry import (
    SourceAdapterRegistry,
    SourceAdapterRegistryError,
)
from backend.source_monitoring.settings import (
    SOURCE_MONITOR_AUTO_START_ENV,
    SOURCE_MONITOR_DRY_RUN_ENV,
    SOURCE_MONITOR_ENABLED_ENV,
    SOURCE_MONITOR_MAX_ITEMS_ENV,
    SOURCE_MONITOR_OFFICIAL_ONLY_ENV,
    SourceMonitoringSettings,
    SourceMonitoringSettingsError,
)
from backend.source_inbox_service import SourceInboxService
from backend.store import StudioStore


CAPTURED_AT_MS = 1_787_845_600_000


def _item() -> dict[str, object]:
    published_at = "2026-08-30T17:06:40Z"
    return {
        "version": PROJECT_SOURCE_ITEM_VERSION,
        "external_item_id": "0001234567-26-000001",
        "item_type": "sec_filing",
        "severity": "info",
        "occurred_at": published_at,
        "published_at": published_at,
        "entities": [
            {"kind": "company", "id": "US.MU", "label": "Micron"}
        ],
        "headline": "Official fixture filing",
        "summary": "Fixture content from a fixed official endpoint.",
        "facts": [{"claim": "A fixture filing was published.", "source_indexes": [0]}],
        "sources": [
            {
                "url": "https://www.sec.gov/Archives/edgar/data/723125/fixture.htm",
                "publisher": "U.S. SEC",
                "source_type": "official_filing",
                "published_at": published_at,
                "content_sha256": "",
            }
        ],
        "impact_hypotheses": [],
        "unknowns": ["No interpretation has been performed."],
        "confidence": 1.0,
        "recommended_route": "notify_only",
        "extensions": {},
    }


class FakeOfficialAdapter:
    contract_version = SOURCE_ADAPTER_CONTRACT_VERSION
    adapter_key = "fixture_official"
    config_version = "fixture_official_config_v1"
    poll_interval_ms = 60_000
    official_source = True
    execution_capability = "none"
    live_trading_allowed = False

    def poll(
        self,
        checkpoint: dict[str, object],
        *,
        observed_at_ms: int,
        etag: str = "",
        last_modified: str = "",
        max_items: int = 50,
    ) -> AdapterPollResult:
        return AdapterPollResult.build(
            self.adapter_key,
            checkpoint,
            {"cursor": "next"},
            [_item()],
            captured_at_ms=observed_at_ms,
        )


class SourceAdapterFoundationTests(unittest.TestCase):
    def test_protocol_metadata_and_registry_are_closed(self) -> None:
        adapter = FakeOfficialAdapter()
        self.assertIsInstance(adapter, SourceAdapter)
        metadata = validate_source_adapter(adapter)
        self.assertEqual(metadata.adapter_key, "fixture_official")
        self.assertEqual(metadata.config_version, "fixture_official_config_v1")
        self.assertEqual(metadata.poll_interval_ms, 60_000)
        self.assertEqual(metadata.execution_capability, "none")
        self.assertIs(metadata.live_trading_allowed, False)

        registry = SourceAdapterRegistry((adapter,))
        self.assertEqual(registry.adapter_keys, ("fixture_official",))
        self.assertIs(registry.require("fixture_official"), adapter)
        self.assertEqual(registry.to_dict()["adapter_count"], 1)

        with self.assertRaises(SourceAdapterRegistryError):
            SourceAdapterRegistry((adapter, adapter))

    def test_non_official_and_unsafe_metadata_fail_closed(self) -> None:
        adapter = FakeOfficialAdapter()
        adapter.official_source = False
        with self.assertRaises(SourceAdapterRegistryError):
            SourceAdapterRegistry((adapter,))

        adapter.official_source = True
        adapter.execution_capability = "write"
        with self.assertRaises(SourceAdapterContractError):
            validate_source_adapter(adapter)

        adapter.execution_capability = "none"
        adapter.live_trading_allowed = True
        with self.assertRaises(SourceAdapterContractError):
            validate_source_adapter(adapter)

    def test_registry_detects_metadata_drift(self) -> None:
        adapter = FakeOfficialAdapter()
        registry = SourceAdapterRegistry((adapter,))
        adapter.official_source = False
        with self.assertRaises(SourceAdapterRegistryError):
            registry.require("fixture_official")


class SourceMonitoringSettingsTests(unittest.TestCase):
    def test_defaults_are_disabled_official_only_and_dry_run(self) -> None:
        settings = SourceMonitoringSettings.from_environment({})
        self.assertEqual(
            settings.to_dict(),
            {
                "enabled": False,
                "auto_start": False,
                "official_only": True,
                "dry_run": True,
                "max_items_per_run": 50,
            },
        )

    def test_explicit_canonical_environment_values_are_parsed(self) -> None:
        settings = SourceMonitoringSettings.from_environment({
            SOURCE_MONITOR_ENABLED_ENV: "1",
            SOURCE_MONITOR_AUTO_START_ENV: "1",
            SOURCE_MONITOR_OFFICIAL_ONLY_ENV: "1",
            SOURCE_MONITOR_DRY_RUN_ENV: "0",
            SOURCE_MONITOR_MAX_ITEMS_ENV: "7",
        })
        self.assertTrue(settings.enabled)
        self.assertTrue(settings.auto_start)
        self.assertTrue(settings.official_only)
        self.assertFalse(settings.dry_run)
        self.assertEqual(settings.max_items_per_run, 7)

    def test_malformed_environment_values_are_rejected(self) -> None:
        for value in ("", "true", " false ", "01", "2"):
            with self.subTest(value=value), self.assertRaises(
                SourceMonitoringSettingsError
            ):
                SourceMonitoringSettings.from_environment({
                    SOURCE_MONITOR_ENABLED_ENV: value
                })
        for value in ("0", "01", "51", "1.0", " 2 "):
            with self.subTest(value=value), self.assertRaises(
                SourceMonitoringSettingsError
            ):
                SourceMonitoringSettings.from_environment({
                    SOURCE_MONITOR_MAX_ITEMS_ENV: value
                })
        with self.assertRaises(SourceMonitoringSettingsError):
            SourceMonitoringSettings.from_environment({
                SOURCE_MONITOR_OFFICIAL_ONLY_ENV: "0",
            })
        with self.assertRaises(SourceMonitoringSettingsError):
            SourceMonitoringSettings.from_environment({
                SOURCE_MONITOR_ENABLED_ENV: "0",
                SOURCE_MONITOR_AUTO_START_ENV: "1",
            })


class SourceMonitoringPacketBuilderTests(unittest.TestCase):
    def test_builder_emits_only_the_fixed_raw_packet_contract(self) -> None:
        item = _item()
        packet = build_source_import_packet(
            adapter_key="fixture_official",
            external_run_id="run-fixture-1",
            captured_at_ms=CAPTURED_AT_MS,
            observed_items=[item],
        )
        item["headline"] = "mutated after build"

        self.assertEqual(packet["version"], SOURCE_IMPORT_PACKET_VERSION)
        self.assertEqual(packet["source_channel"], OFFICIAL_SOURCE_CHANNEL)
        self.assertEqual(packet["source_key"], "fixture_official")
        self.assertIs(packet["meaningful_change"], True)
        self.assertEqual(packet["items"][0]["headline"], "Official fixture filing")
        self.assertEqual(
            set(packet),
            {
                "version",
                "source_channel",
                "source_key",
                "external_run_id",
                "checked_at",
                "cutoff_at",
                "meaningful_change",
                "items",
                "generation",
            },
        )
        self.assertEqual(packet["generation"], {
            "channel": OFFICIAL_SOURCE_CHANNEL,
            "model": "",
            "cost": {
                "status": "unavailable",
                "amount": None,
                "currency": "",
                "usage_source": "not_applicable",
            },
            "correlated_output": False,
        })
        normalized = normalize_source_import_packet(
            packet,
            received_at_ms=CAPTURED_AT_MS,
        )
        self.assertEqual(normalized["safety"]["provider_calls_performed"], 0)
        self.assertEqual(normalized["safety"]["execution_capability"], "none")

    def test_empty_poll_and_poll_result_projection_remain_deterministic(self) -> None:
        empty = build_source_import_packet(
            adapter_key="fixture_official",
            external_run_id="run-empty",
            captured_at_ms=CAPTURED_AT_MS,
            observed_items=(),
        )
        self.assertIs(empty["meaningful_change"], False)
        self.assertEqual(empty["items"], [])

        result = FakeOfficialAdapter().poll(
            {"cursor": "before"},
            observed_at_ms=CAPTURED_AT_MS,
        )
        packet = build_packet_from_poll_result(
            result,
            external_run_id="run-from-result",
        )
        self.assertEqual(packet["items"], [_item()])

    def test_item_limits_and_unknown_fields_fail_closed(self) -> None:
        with self.assertRaises(SourcePacketBuildError):
            build_source_import_packet(
                adapter_key="fixture_official",
                external_run_id="run-too-many",
                captured_at_ms=CAPTURED_AT_MS,
                observed_items=[_item() for _ in range(51)],
            )
        invalid = _item()
        invalid["dynamic_extension"] = {"arbitrary": True}
        with self.assertRaises(SourcePacketBuildError):
            build_source_import_packet(
                adapter_key="fixture_official",
                external_run_id="run-unknown-field",
                captured_at_ms=CAPTURED_AT_MS,
                observed_items=[invalid],
            )

        too_deep = _item()
        nested: dict[str, object] = {"leaf": True}
        for _ in range(16):
            nested = {"nested": nested}
        too_deep["extensions"] = {"fixture_v1": nested}
        with self.assertRaises(SourcePacketBuildError):
            build_source_import_packet(
                adapter_key="fixture_official",
                external_run_id="run-too-deep",
                captured_at_ms=CAPTURED_AT_MS,
                observed_items=[too_deep],
            )

    def test_canonical_payload_is_accepted_by_the_real_source_inbox_entry(self) -> None:
        payload = build_source_import_payload(
            adapter_key="fixture_official",
            external_run_id="run-real-service-entry",
            captured_at_ms=CAPTURED_AT_MS,
            observed_items=[_item()],
        )
        self.assertIs(type(payload), str)
        with tempfile.TemporaryDirectory(prefix="source-monitor-payload-") as temp_dir:
            store = StudioStore(Path(temp_dir) / "studio.sqlite3")
            service = SourceInboxService(
                store,
                clock=lambda: CAPTURED_AT_MS / 1_000,
            )
            imported = service.import_packet(
                payload,
                actor="source_monitoring_worker",
            )
        self.assertEqual(imported["created_item_count"], 1)
        self.assertEqual(imported["duplicate_item_count"], 0)


if __name__ == "__main__":
    unittest.main()
