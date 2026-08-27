from __future__ import annotations

from copy import deepcopy
import unittest
from unittest.mock import patch

import backend.plugin_registry as plugin_registry_module
from backend.decision_lineage import canonical_sha256
from backend.plugin_registry import (
    PLUGIN_REGISTRY_CATALOG_VERSION,
    PLUGIN_REGISTRY_CATALOG_VERSION_V2,
    PLUGIN_REGISTRY_CATALOG_VERSION_V3,
    PLUGIN_REGISTRY_CONTRACT_KINDS,
    PLUGIN_REGISTRY_SNAPSHOT_VERSION_V1,
    PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2,
    PluginRegistryError,
    build_room_plugin_registry_snapshot,
    plugin_registry_catalog,
    plugin_registry_catalog_v3,
    resolve_plugin_registry_contract_exact,
    resolve_plugin_registry_contract_latest,
    validate_plugin_registry_catalog_v3,
)


_CURRENT_KIND_FIELDS = {
    "capability_pack": (
        "capability_packs",
        "id",
        "pack_version",
        "manifest_sha256",
    ),
    "domain_adapter": (
        "domain_adapters",
        "adapter_id",
        "adapter_version",
        "contract_sha256",
    ),
    "domain_adapter_port": (
        "domain_adapter_ports",
        "port_id",
        "port_version",
        "contract_sha256",
    ),
    "ui_contribution": (
        "ui_contributions",
        "contribution_id",
        "contribution_version",
        "contract_sha256",
    ),
    "ui_view_model_schema": (
        "ui_view_model_schemas",
        "component_key",
        "schema_version",
        "schema_sha256",
    ),
}


def _empty_history() -> dict[str, list[dict[str, object]]]:
    return {kind: [] for kind in PLUGIN_REGISTRY_CONTRACT_KINDS}


def _current_contract(kind: str, stable_id: str) -> dict[str, object]:
    collection, stable_field, _version_field, _hash_field = _CURRENT_KIND_FIELDS[kind]
    return deepcopy(next(
        row
        for row in plugin_registry_catalog()[collection]
        if row[stable_field] == stable_id
    ))


def _reseal(contract: dict[str, object], hash_field: str) -> dict[str, object]:
    sealed = deepcopy(contract)
    sealed.pop(hash_field, None)
    sealed[hash_field] = canonical_sha256(sealed)
    return sealed


class _StringSubclass(str):
    pass


class PluginRegistryVersionTests(unittest.TestCase):
    def test_v3_is_parallel_to_legacy_catalog_and_room_snapshot_v1_v2(self) -> None:
        legacy_catalog = plugin_registry_catalog()
        legacy_snapshot_v1 = build_room_plugin_registry_snapshot([])
        legacy_snapshot_v2 = build_room_plugin_registry_snapshot(
            ["football_research_readonly"]
        )

        versioned_catalog = plugin_registry_catalog_v3()

        self.assertEqual(PLUGIN_REGISTRY_CATALOG_VERSION, PLUGIN_REGISTRY_CATALOG_VERSION_V2)
        self.assertEqual(versioned_catalog["version"], PLUGIN_REGISTRY_CATALOG_VERSION_V3)
        self.assertEqual(plugin_registry_catalog(), legacy_catalog)
        self.assertEqual(build_room_plugin_registry_snapshot([]), legacy_snapshot_v1)
        self.assertEqual(
            build_room_plugin_registry_snapshot(["football_research_readonly"]),
            legacy_snapshot_v2,
        )
        self.assertEqual(legacy_snapshot_v1["version"], PLUGIN_REGISTRY_SNAPSHOT_VERSION_V1)
        self.assertEqual(legacy_snapshot_v2["version"], PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2)

    def test_every_current_contract_has_exact_and_latest_resolution(self) -> None:
        legacy_catalog = plugin_registry_catalog()
        versioned_catalog = plugin_registry_catalog_v3()
        self.assertEqual(
            validate_plugin_registry_catalog_v3(versioned_catalog),
            versioned_catalog,
        )

        for kind, fields in _CURRENT_KIND_FIELDS.items():
            collection, stable_field, version_field, hash_field = fields
            for contract in legacy_catalog[collection]:
                stable_id = contract[stable_field]
                exact_version = contract[version_field]
                exact = resolve_plugin_registry_contract_exact(
                    kind,
                    stable_id,
                    exact_version,
                    catalog=versioned_catalog,
                )
                latest = resolve_plugin_registry_contract_latest(
                    kind,
                    stable_id,
                    catalog=versioned_catalog,
                )
                self.assertEqual(exact["contract"], contract)
                self.assertEqual(exact["contract_sha256"], contract[hash_field])
                self.assertEqual(latest, exact)

    def test_append_only_history_keeps_old_exact_version_and_latest_alias(self) -> None:
        current = _current_contract("domain_adapter", "football_research")
        old = deepcopy(current)
        old["adapter_version"] = "0.9.0"
        old = _reseal(old, "contract_sha256")
        history = _empty_history()
        history["domain_adapter"].append(old)

        with patch.object(
            plugin_registry_module,
            "_PLUGIN_REGISTRY_HISTORICAL_CONTRACTS",
            history,
        ):
            versioned_catalog = plugin_registry_catalog_v3()
            old_resolution = resolve_plugin_registry_contract_exact(
                "domain_adapter",
                "football_research",
                "0.9.0",
                catalog=versioned_catalog,
            )
            latest = resolve_plugin_registry_contract_latest(
                "domain_adapter",
                "football_research",
                catalog=versioned_catalog,
            )

        self.assertEqual(old_resolution["contract"], old)
        self.assertEqual(latest["exact_version"], current["adapter_version"])
        self.assertEqual(latest["contract"], current)
        football_history = next(
            row
            for row in versioned_catalog["histories"]["domain_adapter"]
            if row["stable_id"] == "football_research"
        )
        self.assertEqual(
            [row["exact_version"] for row in football_history["versions"]],
            ["0.9.0", "1.0.0"],
        )
        self.assertEqual(football_history["versions"][0]["previous_entry_sha256"], "")
        self.assertEqual(
            football_history["versions"][1]["previous_entry_sha256"],
            football_history["versions"][0]["entry_sha256"],
        )
        self.assertEqual(
            football_history["history_head_sha256"],
            football_history["versions"][1]["entry_sha256"],
        )

    def test_duplicate_exact_identity_is_rejected_even_when_hash_matches(self) -> None:
        current = _current_contract("domain_adapter", "football_research")
        history = _empty_history()
        history["domain_adapter"].append(current)

        with patch.object(
            plugin_registry_module,
            "_PLUGIN_REGISTRY_HISTORICAL_CONTRACTS",
            history,
        ):
            with self.assertRaisesRegex(PluginRegistryError, "duplicated"):
                plugin_registry_catalog_v3()

    def test_same_exact_identity_with_different_hash_is_rejected(self) -> None:
        current = _current_contract("domain_adapter", "football_research")
        conflicting = deepcopy(current)
        conflicting["activation_capabilities"] = [
            *conflicting["activation_capabilities"],
            "research.football.historical_contract_test",
        ]
        conflicting = _reseal(conflicting, "contract_sha256")
        history = _empty_history()
        history["domain_adapter"].append(conflicting)

        with patch.object(
            plugin_registry_module,
            "_PLUGIN_REGISTRY_HISTORICAL_CONTRACTS",
            history,
        ):
            with self.assertRaisesRegex(PluginRegistryError, "conflicting hashes"):
                plugin_registry_catalog_v3()

    def test_semver_history_cannot_move_backwards_to_current_pointer(self) -> None:
        current = _current_contract("domain_adapter", "football_research")
        future = deepcopy(current)
        future["adapter_version"] = "2.0.0"
        future = _reseal(future, "contract_sha256")
        history = _empty_history()
        history["domain_adapter"].append(future)

        with patch.object(
            plugin_registry_module,
            "_PLUGIN_REGISTRY_HISTORICAL_CONTRACTS",
            history,
        ):
            with self.assertRaisesRegex(PluginRegistryError, "not append-only"):
                plugin_registry_catalog_v3()

    def test_unknown_kind_stable_id_and_exact_version_fail_closed(self) -> None:
        catalog = plugin_registry_catalog_v3()
        with self.assertRaisesRegex(PluginRegistryError, "Unknown plugin registry contract kind"):
            resolve_plugin_registry_contract_exact(
                "unknown_kind",
                "football_research",
                "1.0.0",
                catalog=catalog,
            )
        with self.assertRaisesRegex(PluginRegistryError, "Unknown plugin registry stable identity"):
            resolve_plugin_registry_contract_exact(
                "domain_adapter",
                "missing_adapter",
                "1.0.0",
                catalog=catalog,
            )
        with self.assertRaisesRegex(PluginRegistryError, "Unknown exact plugin registry version"):
            resolve_plugin_registry_contract_exact(
                "domain_adapter",
                "football_research",
                "9.9.9",
                catalog=catalog,
            )

    def test_catalog_chain_alias_and_exact_native_inputs_are_fail_closed(self) -> None:
        catalog = plugin_registry_catalog_v3()
        tampered_chain = deepcopy(catalog)
        first_history = tampered_chain["histories"]["domain_adapter"][0]
        first_history["history_head_sha256"] = "0" * 64
        with self.assertRaisesRegex(PluginRegistryError, "history head"):
            validate_plugin_registry_catalog_v3(tampered_chain)

        tampered_alias = deepcopy(catalog)
        tampered_alias["latest_aliases"]["domain_adapter"][0]["exact_version"] = "9.9.9"
        with self.assertRaisesRegex(PluginRegistryError, "latest aliases"):
            validate_plugin_registry_catalog_v3(tampered_alias)

        uppercase_hash = deepcopy(catalog)
        uppercase_hash["catalog_sha256"] = uppercase_hash["catalog_sha256"].upper()
        with self.assertRaisesRegex(PluginRegistryError, "hash is invalid"):
            validate_plugin_registry_catalog_v3(uppercase_hash)

        with self.assertRaisesRegex(PluginRegistryError, "exact non-empty text"):
            resolve_plugin_registry_contract_exact(
                "domain_adapter",
                "football_research",
                _StringSubclass("1.0.0"),
                catalog=catalog,
            )


if __name__ == "__main__":
    unittest.main()
