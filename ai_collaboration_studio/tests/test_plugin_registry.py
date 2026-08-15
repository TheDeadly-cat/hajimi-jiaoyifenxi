from __future__ import annotations

from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from backend.domain_adapters import DomainAdapterRegistry
from backend.orchestrator import DiscussionOrchestrator
from backend.plugin_registry import (
    PLUGIN_REGISTRY_CATALOG_VERSION,
    PLUGIN_REGISTRY_SNAPSHOT_VERSION,
    PluginRegistryError,
    build_room_plugin_registry_snapshot,
    plugin_registry_catalog,
    validate_room_plugin_registry_snapshot,
)
from backend.store import StudioStore
from backend.templates import default_workflow_policy
from backend.turn_contract import TURN_CONTRACT_VERSION
from backend.turn_envelope import TURN_ENVELOPE_SCHEMA_SHA256, TURN_ENVELOPE_VERSION


class PluginRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="ai-studio-p24-registry-")
        self.store = StudioStore(Path(self.temp_dir.name) / "studio.sqlite3")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_catalog_is_versioned_hashed_host_owned_and_non_executable(self) -> None:
        catalog = plugin_registry_catalog()

        self.assertEqual(catalog["version"], PLUGIN_REGISTRY_CATALOG_VERSION)
        self.assertEqual(len(catalog["catalog_sha256"]), 64)
        self.assertEqual(len(catalog["capability_packs"]), 7)
        self.assertEqual(len(catalog["domain_adapters"]), 5)
        self.assertEqual(len(catalog["domain_adapter_ports"]), 4)
        self.assertEqual(len(catalog["ui_contributions"]), 8)
        self.assertTrue(all(len(row["manifest_sha256"]) == 64 for row in catalog["capability_packs"]))
        self.assertTrue(all(row["mode"] == "host_owned_component" for row in catalog["ui_contributions"]))
        self.assertTrue(all("user_decision" not in row["slot_id"] for row in catalog["ui_contributions"]))
        self.assertEqual(catalog["safety"]["execution_capability"], "none")
        self.assertFalse(catalog["safety"]["live_trading_allowed"])
        self.assertFalse(catalog["safety"]["arbitrary_code_loading_allowed"])
        self.assertTrue(catalog["safety"]["user_final_decision_required"])

        football_pack = next(
            row
            for row in catalog["capability_packs"]
            if row["id"] == "football_research_readonly"
        )
        football_adapter = next(
            row
            for row in catalog["domain_adapters"]
            if row["adapter_id"] == "football_research"
        )
        self.assertEqual(
            [row["port_id"] for row in football_adapter["ports"]],
            ["core.football.match_context/v1"],
        )
        self.assertNotIn("simulation.paper_portfolio", football_pack["capabilities"])
        self.assertEqual(football_pack["execution_capability"], "none")
        self.assertFalse(football_pack["live_trading_allowed"])

        stock_pack = next(
            row
            for row in catalog["capability_packs"]
            if row["id"] == "stock_research_readonly"
        )
        stock_adapter = next(
            row
            for row in catalog["domain_adapters"]
            if row["adapter_id"] == "stock_research"
        )
        self.assertEqual(stock_pack["dependencies"], ["structured_project_research"])
        self.assertEqual(
            [row["port_id"] for row in stock_adapter["ports"]],
            ["core.market.readonly_context/v1"],
        )
        self.assertNotIn("simulation.paper_portfolio", stock_pack["capabilities"])
        self.assertTrue(all("candidate" not in item for item in stock_pack["capabilities"]))

    def test_room_freezes_registry_and_only_pack_change_re_resolves_it(self) -> None:
        created = self.store.create_room(
            "P24 registry",
            "Freeze exact built-in contracts",
            capability_pack_ids=[],
        )["room"]
        original_hash = created["plugin_registry_snapshot_sha256"]
        self.assertTrue(created["plugin_registry_integrity_ok"])
        self.assertEqual(
            created["plugin_registry_snapshot"]["version"],
            PLUGIN_REGISTRY_SNAPSHOT_VERSION,
        )
        self.assertEqual(
            [row["id"] for row in created["plugin_registry_snapshot"]["capability_packs"]],
            ["structured_turn_contract_v1"],
        )

        metadata_update = self.store.update_room(created["id"], {
            "title": "P24 registry renamed",
            "expected_settings_version": created["settings_version"],
        })
        self.assertEqual(metadata_update["plugin_registry_snapshot_sha256"], original_hash)

        pack_update = self.store.update_room(created["id"], {
            "capability_pack_ids": ["storage_research_readonly"],
            "expected_settings_version": metadata_update["settings_version"],
        })
        self.assertNotEqual(pack_update["plugin_registry_snapshot_sha256"], original_hash)
        self.assertEqual(
            [row["adapter_id"] for row in pack_update["plugin_registry_snapshot"]["domain_adapters"]],
            ["storage_research"],
        )
        self.assertEqual(len(pack_update["plugin_registry_snapshot"]["ui_contributions"]), 3)
        self.assertEqual(self.store.room_plugin_registry(created["id"])["status"], "ready")

    def test_snapshot_validation_and_persisted_tamper_fail_closed(self) -> None:
        snapshot = build_room_plugin_registry_snapshot(["structured_project_research"])
        self.assertEqual(
            validate_room_plugin_registry_snapshot(
                snapshot,
                ["structured_project_research"],
            ),
            snapshot,
        )
        room = self.store.create_room(
            "P24 tamper",
            "fail closed",
            capability_pack_ids=["structured_project_research"],
        )["room"]
        with closing(self.store._connect()) as connection:
            connection.execute(
                "UPDATE rooms SET plugin_registry_snapshot_json='{}' WHERE id=?",
                (room["id"],),
            )
            connection.commit()

        current = self.store.room_snapshot(room["id"])["room"]
        self.assertFalse(current["plugin_registry_integrity_ok"])
        self.assertEqual(current["plugin_registry_snapshot"], {})
        self.assertEqual(self.store.room_plugin_registry(room["id"])["status"], "integrity_failed")

        generic = self.store.create_room(
            "Malformed packs",
            "do not restore template defaults",
            capability_pack_ids=[],
        )["room"]
        with closing(self.store._connect()) as connection:
            connection.execute(
                "UPDATE rooms SET capability_packs_json='not-json' WHERE id=?",
                (generic["id"],),
            )
            connection.commit()
        corrupted = self.store.room_snapshot(generic["id"])["room"]
        self.assertFalse(corrupted["capability_pack_integrity_ok"])
        self.assertFalse(corrupted["plugin_registry_integrity_ok"])
        self.assertEqual(corrupted["capability_pack_ids"], [])

    def test_frozen_snapshot_prevents_new_adapter_auto_activation(self) -> None:
        sports_adapter = SimpleNamespace(
            adapter_id="sports_context",
            activation_capabilities=frozenset({"market.sports.readonly"}),
            execution_capability="none",
            live_trading_allowed=False,
            provides_market_context=True,
        )
        for name in DomainAdapterRegistry._required_methods:
            setattr(sports_adapter, name, lambda *_args, **_kwargs: None)
        registry = DomainAdapterRegistry((sports_adapter,))
        room = self.store.create_room(
            "Frozen generic room",
            "No silent adapter activation",
            capability_pack_ids=[],
        )["room"]
        room["capabilities"] = [*room["capabilities"], "market.sports.readonly"]

        self.assertEqual(registry.active_for_room(room), ())

    def test_new_checkpoint_carries_the_exact_room_registry_snapshot(self) -> None:
        room = self.store.create_room(
            "Checkpoint registry",
            "Freeze into formal round state",
            capability_pack_ids=["structured_project_research"],
        )["room"]
        state = DiscussionOrchestrator._checkpoint_state(
            [{"id": "member_one"}],
            {},
            set(),
            set(),
            set(),
            "我",
            0,
            0,
            0,
            0,
            0,
            1,
            1,
            "",
            None,
            None,
            None,
            {
                "discussion_mode": "dynamic",
                "domain": "open_collaboration",
                "moderator_member_id": "member_one",
                "moderator_member_version": 1,
                "moderator_provider": "deepseek",
                "moderator_model": "fixture-model",
            },
            default_workflow_policy("open_collaboration"),
            room["capability_pack_ids"],
            room["plugin_registry_snapshot"],
            None,
            set(),
            TURN_CONTRACT_VERSION,
            True,
            TURN_ENVELOPE_VERSION,
            TURN_ENVELOPE_SCHEMA_SHA256,
            {"member_one": "json_object"},
            9,
            None,
            False,
        )
        clean = self.store._clean_checkpoint_state(state)

        self.assertEqual(
            clean["plugin_registry_snapshot"]["registry_snapshot_sha256"],
            room["plugin_registry_snapshot_sha256"],
        )

    def test_formal_round_atomically_binds_current_room_registry(self) -> None:
        room = self.store.create_room(
            "Atomic round registry",
            "Bind before any provider call",
            capability_pack_ids=["structured_project_research"],
        )["room"]
        old_version = room["settings_version"]
        old_hash = room["plugin_registry_snapshot_sha256"]
        updated = self.store.update_room(room["id"], {
            "title": "Atomic round registry updated",
            "expected_settings_version": old_version,
        })

        with self.assertRaises(PluginRegistryError):
            self.store.create_formal_round(
                room["id"],
                "stale launch",
                expected_settings_version=old_version,
                expected_plugin_registry_snapshot_sha256=old_hash,
            )

        formal = self.store.create_formal_round(
            room["id"],
            "current launch",
            expected_settings_version=updated["settings_version"],
            expected_plugin_registry_snapshot_sha256=updated[
                "plugin_registry_snapshot_sha256"
            ],
        )
        reread = self.store.get_round(room["id"], formal["id"])

        self.assertEqual(reread["plugin_registry_status"], "ready")
        self.assertTrue(reread["plugin_registry_integrity_ok"])
        self.assertEqual(
            reread["plugin_registry_snapshot_sha256"],
            updated["plugin_registry_snapshot_sha256"],
        )


if __name__ == "__main__":
    unittest.main()
