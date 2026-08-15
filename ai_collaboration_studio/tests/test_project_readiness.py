from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from copy import deepcopy
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend import http_server
from backend import capability_packs as capability_pack_module
from backend import plugin_registry as plugin_registry_module
from backend.decision_lineage import canonical_sha256
from backend.domain_adapters import (
    DEFAULT_DOMAIN_ADAPTERS,
    ProjectReadinessDomainAdapter,
)
from backend.plugin_registry import (
    PluginRegistryError,
    PLUGIN_REGISTRY_SNAPSHOT_VERSION_V1,
    PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2,
    build_room_plugin_registry_snapshot,
    plugin_registry_catalog,
    validate_room_plugin_registry_snapshot,
)
from backend.plugin_lifecycle import PluginLifecycleError
from backend.project_readiness import ProjectReadinessError, ProjectReadinessService
from backend.store import StudioStore


class ProjectReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="ai-studio-p26-")
        self.db_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.store = StudioStore(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create_confirmed_artifact(self) -> tuple[dict, dict, dict]:
        room = self.store.create_room(
            "P26 project readiness",
            "Deterministic project review",
            capability_pack_ids=["project_readiness_review"],
        )["room"]
        material = self.store.add_material(room["id"], {
            "title": "Confirmed requirement evidence",
            "kind": "note",
            "content": "A local, exact source for readiness verification.",
        })
        evidence = [{
            "type": "material",
            "id": material["id"],
            "evidence_role": "support",
            "verification_status": "source_checked",
            "review_note": "checked",
        }]
        artifact = self.store.create_artifact(
            room["id"],
            title="Project plan",
            content={
                "summary": "A bounded project plan.",
                "summary_evidence": evidence,
                "requirements": [{
                    "id": "req_one",
                    "text": "Ship the bounded prototype.",
                    "status": "confirmed",
                    "owner": "owner_one",
                    "acceptance_criteria": "All isolated tests pass.",
                    "evidence": evidence,
                }],
                "risks": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
            },
        )
        confirmed = self.store.confirm_artifact(
            room["id"],
            artifact["id"],
            expected_version=artifact["version"],
            confirmed_by="user",
        )
        return room, material, confirmed

    def business_counts(self) -> tuple[int, ...]:
        with closing(sqlite3.connect(self.db_path)) as connection:
            return tuple(
                int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "artifacts",
                    "artifact_versions",
                    "artifact_evidence_review_events",
                    "plugin_lifecycle_events",
                    "messages",
                    "materials",
                )
            )

    @staticmethod
    def reseal_registry_snapshot(snapshot: dict) -> dict:
        value = deepcopy(snapshot)
        resolved_catalog = {
            "version": (
                "plugin_registry_resolution_v2"
                if value["version"] == PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2
                else "plugin_registry_resolution_v1"
            ),
            "room_kernel_version": value["room_kernel_version"],
            "selected_capability_pack_ids": value["selected_capability_pack_ids"],
            "capability_packs": value["capability_packs"],
            "domain_adapters": value["domain_adapters"],
            "ui_contributions": value["ui_contributions"],
            **({"port_resolutions": value["port_resolutions"]}
               if value["version"] == PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2 else {}),
            "safety": value["safety"],
        }
        value["resolved_catalog_sha256"] = canonical_sha256(resolved_catalog)
        value.pop("registry_snapshot_sha256", None)
        value["registry_snapshot_sha256"] = canonical_sha256(value)
        return value

    def test_v1_storage_is_stable_and_v2_freezes_exact_port_resolution(self) -> None:
        storage = build_room_plugin_registry_snapshot(["storage_research_readonly"])
        self.assertEqual(storage["version"], PLUGIN_REGISTRY_SNAPSHOT_VERSION_V1)
        self.assertEqual(
            storage["registry_snapshot_sha256"],
            "1a26b135ba0dfb6305cfe2167e6e91d6247750f717da7c3717c08496a5707f4a",
        )
        self.assertNotIn("port_resolutions", storage)
        self.assertNotIn("ports", storage["domain_adapters"][0])

        readiness = build_room_plugin_registry_snapshot(["project_readiness_review"])
        self.assertEqual(readiness["version"], PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2)
        self.assertEqual(len(readiness["port_resolutions"]), 1)
        adapter = readiness["domain_adapters"][0]
        resolution = readiness["port_resolutions"][0]["resolved"][0]
        self.assertEqual(adapter["ports"][0]["contract_sha256"], resolution["port_contract_sha256"])
        self.assertEqual(adapter["contract_sha256"], resolution["adapter_contract_sha256"])
        self.assertEqual(resolution["provider_call_budget"], 0)
        self.assertEqual(resolution["market_read_budget"], 0)
        self.assertEqual(resolution["business_write_budget"], 0)

        mixed = build_room_plugin_registry_snapshot([
            "storage_research_readonly",
            "project_readiness_review",
        ])
        self.assertEqual(
            validate_room_plugin_registry_snapshot(
                mixed,
                ["storage_research_readonly", "project_readiness_review"],
            ),
            mixed,
        )
        contribution = next(
            row
            for row in readiness["ui_contributions"]
            if row["contribution_id"] == "project_readiness.artifact_workspace/v1"
        )
        self.assertEqual(contribution["contract_version"], "ui_contribution_contract_v2")
        self.assertEqual(
            contribution["source_port"],
            {
                "owner_pack_id": "project_readiness_review",
                "port_id": "core.artifact.projection/v1",
                "requirement": "required",
                "cardinality": "one",
            },
        )
        self.assertEqual(
            contribution["source_port_resolution"]["port_contract_sha256"],
            resolution["port_contract_sha256"],
        )
        self.assertEqual(
            contribution["source_port_resolution"]["output_schema_sha256"],
            resolution["output_schema_sha256"],
        )

    def test_catalog_contracts_are_closed_and_bounded_before_sealing(self) -> None:
        catalog = plugin_registry_catalog()
        self.assertEqual(catalog["version"], "plugin_registry_catalog_v2")
        self.assertEqual(
            {
                row["schema_version"]
                for row in catalog["ui_view_model_schemas"]
            },
            {
                "football_research_view_model_v1",
                "project_readiness_view_model_v1",
                "project_round_focus_view_model_v1",
                "stock_research_view_model_v1",
            },
        )
        mutations = [
            (capability_pack_module.CAPABILITY_PACKS["structured_project_research"], {"unknown_top": True}),
            (capability_pack_module.CAPABILITY_PACKS["project_readiness_review"], {"unknown_top": True}),
            (plugin_registry_module.DOMAIN_ADAPTER_CONTRACTS["project_readiness"], {"unknown_top": True}),
            (plugin_registry_module.DOMAIN_ADAPTER_PORT_CONTRACTS["core.artifact.projection/v1"], {"unknown_top": True}),
            (plugin_registry_module.DOMAIN_ADAPTER_PORT_CONTRACTS["core.artifact.projection/v1"]["input_schema"], {"unknown_schema": True}),
            (plugin_registry_module.DOMAIN_ADAPTER_PORT_CONTRACTS["core.artifact.projection/v1"]["input_schema"], {"required": "artifact"}),
            (plugin_registry_module.DOMAIN_ADAPTER_PORT_CONTRACTS["core.artifact.projection/v1"]["output_schema"], {"additional_properties": True}),
            (plugin_registry_module.UI_CONTRIBUTION_CONTRACTS["project_readiness.artifact_workspace/v1"], {"unknown_top": True}),
            (plugin_registry_module.HOST_UI_VIEW_MODEL_SCHEMAS["project_readiness_view_model_v1"], {"unknown_schema": True}),
            (plugin_registry_module.HOST_UI_VIEW_MODEL_SCHEMAS["project_readiness_view_model_v1"], {"additional_properties": True}),
        ]
        for target, mutation in mutations:
            with self.subTest(mutation=mutation), patch.dict(target, mutation, clear=False):
                with self.assertRaises(PluginRegistryError):
                    plugin_registry_catalog()

        requirement = capability_pack_module.CAPABILITY_PACKS[
            "project_readiness_review"
        ]["domain_adapter_port_requirements"][0]
        with patch.dict(requirement, {"version_range": ">=1.0.0"}, clear=False):
            with self.assertRaises(PluginRegistryError):
                plugin_registry_catalog()
        port_contract = plugin_registry_module.DOMAIN_ADAPTER_PORT_CONTRACTS[
            "core.artifact.projection/v1"
        ]
        with patch.dict(port_contract, {"handler_method": "no_such_handler"}, clear=False):
            with self.assertRaises(PluginRegistryError):
                plugin_registry_catalog()

    def test_rehashed_snapshot_tamper_cannot_change_exact_bindings(self) -> None:
        original = build_room_plugin_registry_snapshot(["project_readiness_review"])
        mutation_paths = (
            ("resolution_extra",),
            ("binding_extra",),
            ("adapter_schema_hash",),
            ("ui_source_hash",),
        )
        for (mutation,) in mutation_paths:
            value = deepcopy(original)
            if mutation == "resolution_extra":
                value["port_resolutions"][0]["unexpected"] = True
            elif mutation == "binding_extra":
                value["port_resolutions"][0]["resolved"][0]["unexpected"] = True
            elif mutation == "adapter_schema_hash":
                value["domain_adapters"][0]["ports"][0]["output_schema_sha256"] = "f" * 64
                value["port_resolutions"][0]["resolved"][0]["output_schema_sha256"] = "f" * 64
            else:
                contribution = next(
                    row for row in value["ui_contributions"]
                    if row.get("contract_version") == "ui_contribution_contract_v2"
                )
                contribution["source_port_resolution"]["output_schema_sha256"] = "f" * 64
            value = self.reseal_registry_snapshot(value)
            with self.subTest(mutation=mutation), self.assertRaises(PluginRegistryError):
                validate_room_plugin_registry_snapshot(
                    value,
                    ["project_readiness_review"],
                    require_current=False,
                )

    def test_historical_v2_manifest_semantics_are_anchored_to_lifecycle_ledger(self) -> None:
        room = self.store.create_room(
            "P26 ledger anchor",
            "Historical contracts remain exact",
            capability_pack_ids=["project_readiness_review"],
        )["room"]
        original = room["plugin_registry_snapshot"]
        lifecycle = room["plugin_lifecycle_resolution"]
        for field, value in (("version_range", ">=0.0.0 <9.0.0"),):
            tampered = deepcopy(original)
            pack = next(
                row for row in tampered["capability_packs"]
                if row["id"] == "project_readiness_review"
            )
            pack["domain_adapter_port_requirements"][0][field] = value
            tampered["port_resolutions"][0][field] = value
            tampered = self.reseal_registry_snapshot(tampered)
            self.assertEqual(
                validate_room_plugin_registry_snapshot(
                    tampered,
                    ["project_readiness_review"],
                    require_current=False,
                ),
                tampered,
            )
            with self.subTest(field=field), closing(self.store._connect()) as connection:
                with self.assertRaises(PluginLifecycleError):
                    self.store._validate_lifecycle_resolution_against_ledger(
                        connection,
                        lifecycle,
                        tampered,
                    )
        tampered = deepcopy(original)
        pack = next(
            row for row in tampered["capability_packs"]
            if row["id"] == "project_readiness_review"
        )
        pack["domain_adapter_port_requirements"][0]["requirement"] = "optional"
        tampered["port_resolutions"][0]["requirement"] = "optional"
        tampered = self.reseal_registry_snapshot(tampered)
        with self.assertRaises(PluginRegistryError):
            validate_room_plugin_registry_snapshot(
                tampered,
                ["project_readiness_review"],
                require_current=False,
            )

    def test_port_only_adapter_has_no_market_or_turn_noops(self) -> None:
        adapter = ProjectReadinessDomainAdapter()
        self.assertEqual(
            adapter.declared_ports,
            frozenset({"core.artifact.projection/v1"}),
        )
        for method in (
            "with_market_service",
            "preflight_market",
            "prompt_context",
            "timeline_message",
            "speaker_prompt_rule",
            "machine_block_names",
            "extract_speaker_payloads",
            "persist_speaker_payloads",
            "artifact_prompt_rule",
            "artifact_evidence_types",
        ):
            self.assertFalse(hasattr(adapter, method), method)
        self.assertIs(
            DEFAULT_DOMAIN_ADAPTERS.require("project_readiness", "1.0.0"),
            DEFAULT_DOMAIN_ADAPTERS.require("project_readiness", "1.0.0"),
        )

    def test_service_is_exact_read_only_and_does_not_leak_inputs(self) -> None:
        room, _material, artifact = self.create_confirmed_artifact()
        before = self.business_counts()
        projection = ProjectReadinessService(self.store).inspect(
            room["id"], artifact["id"], artifact["version"]
        )
        after = self.business_counts()

        self.assertEqual(before, after)
        self.assertEqual(projection["version"], "project_readiness_projection_v1")
        self.assertTrue(projection["integrity_ok"])
        self.assertTrue(projection["metrics_visible"])
        self.assertEqual(projection["provider_calls_performed"], 0)
        self.assertEqual(projection["market_reads_performed"], 0)
        self.assertEqual(projection["business_writes_performed"], 0)
        self.assertFalse(projection["ranking_produced"])
        self.assertFalse(projection["winner_claim"])
        self.assertFalse(projection["approval_produced"])
        self.assertTrue(projection["user_final_decision_required"])
        self.assertFalse(projection["can_replace_user_decision"])
        self.assertFalse(projection["arbitrary_code_loading_allowed"])
        self.assertEqual(
            projection["resolution"]["contribution"]["component_key"],
            "project_readiness_review",
        )
        self.assertEqual(
            projection["resolution"]["port"]["port_id"],
            "core.artifact.projection/v1",
        )
        self.assertEqual(projection["resolution"]["port"]["provider_call_budget"], 0)
        self.assertEqual(projection["resolution"]["port"]["market_read_budget"], 0)
        self.assertEqual(projection["resolution"]["port"]["business_write_budget"], 0)
        self.assertEqual(projection["resolution"]["port"]["failure_policy"], "fail_closed")
        encoded = json.dumps(projection, ensure_ascii=False)
        self.assertNotIn("evidence_relations", encoded)
        self.assertNotIn("A local, exact source", encoded)
        self.assertNotIn('"content"', encoded)

    def test_adapter_output_is_exact_schema_validated_before_public_projection(self) -> None:
        room, _material, artifact = self.create_confirmed_artifact()
        real_adapter = ProjectReadinessDomainAdapter()

        class FakeRegistry:
            def __init__(self, mutate):
                self.mutate = mutate

            def require_port_resolution(self, _resolution):
                mutate = self.mutate

                class FakeAdapter:
                    def project_artifact(self, **kwargs):
                        output = real_adapter.project_artifact(**kwargs)
                        return mutate(output)

                return FakeAdapter()

        mutations = {
            "unexpected root": lambda row: {**row, "unexpected_raw": "secret"},
            "missing required": lambda row: {
                key: value for key, value in row.items() if key != "counts"
            },
            "malformed gaps": lambda row: {
                **row,
                "requirement_gaps": [{"item_id": "req_one", "codes": "bad"}],
            },
            "lying counts": lambda row: {
                **row,
                "counts": {**row["counts"], "requirement_gap_count": True},
            },
            "lying state": lambda row: {**row, "state": "blocked"},
        }
        before = self.business_counts()
        for name, mutate in mutations.items():
            service = ProjectReadinessService(self.store, FakeRegistry(mutate))
            with self.subTest(name=name), self.assertRaises(ProjectReadinessError) as error:
                service.inspect(room["id"], artifact["id"], artifact["version"])
            self.assertEqual(error.exception.code, "PROJECT_READINESS_OUTPUT_INVALID")
        self.assertEqual(before, self.business_counts())

    def test_unconfirmed_or_tampered_exact_inputs_fail_before_projection(self) -> None:
        room = self.store.create_room(
            "P26 draft",
            "Drafts are not inspectable",
            capability_pack_ids=["project_readiness_review"],
        )["room"]
        draft = self.store.create_artifact(
            room["id"],
            title="Draft",
            content={"summary": "draft", "summary_evidence": []},
        )
        with self.assertRaisesRegex(ProjectReadinessError, "已确认") as draft_error:
            ProjectReadinessService(self.store).inspect(
                room["id"], draft["id"], draft["version"]
            )
        self.assertEqual(draft_error.exception.code, "PROJECT_READINESS_ARTIFACT_NOT_CONFIRMED")

        room, _material, artifact = self.create_confirmed_artifact()
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            row = connection.execute(
                "SELECT snapshot_json FROM artifact_versions WHERE artifact_id=? AND version=?",
                (artifact["id"], artifact["version"]),
            ).fetchone()
            snapshot = json.loads(row[0])
            snapshot["title"] = "tampered title"
            connection.execute(
                "UPDATE artifact_versions SET snapshot_json=? WHERE artifact_id=? AND version=?",
                (json.dumps(snapshot), artifact["id"], artifact["version"]),
            )
        with self.assertRaises(ProjectReadinessError) as tampered:
            ProjectReadinessService(self.store).inspect(
                room["id"], artifact["id"], artifact["version"]
            )
        self.assertEqual(
            tampered.exception.code,
            "PROJECT_READINESS_ARTIFACT_VERSION_INTEGRITY_FAILED",
        )


class ProjectReadinessHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_store = http_server.STORE
        self.temp_dir = tempfile.TemporaryDirectory(prefix="ai-studio-p26-http-")
        self.store = StudioStore(Path(self.temp_dir.name) / "studio.sqlite3")
        http_server.STORE = self.store
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            http_server.StudioRequestHandler,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        http_server.STORE = self.original_store
        self.temp_dir.cleanup()

    def test_get_returns_strict_projection_without_provider_or_market(self) -> None:
        harness = ProjectReadinessTests(methodName="runTest")
        harness.store = self.store
        room, _material, artifact = harness.create_confirmed_artifact()
        url = (
            f"{self.base_url}/api/rooms/{room['id']}/artifacts/{artifact['id']}"
            f"/versions/{artifact['version']}/project-readiness"
        )
        with patch.object(
            http_server.PROVIDERS,
            "get",
            side_effect=AssertionError("provider must remain unused"),
        ), patch.object(
            http_server.STORAGE_MARKET,
            "snapshot",
            side_effect=AssertionError("market must remain unused"),
        ):
            with urlopen(url, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(
            payload["projection"]["artifact_version"],
            artifact["version"],
        )
        self.assertEqual(payload["projection"]["provider_calls_performed"], 0)
        self.assertEqual(payload["projection"]["market_reads_performed"], 0)

        self.store.update_room(room["id"], {
            "capability_pack_ids": [],
            "expected_settings_version": self.store.room_snapshot(room["id"])["room"]["settings_version"],
        })
        with self.assertRaises(HTTPError) as error:
            urlopen(url, timeout=5)
        self.assertEqual(error.exception.code, 409)
        body = json.loads(error.exception.read().decode("utf-8"))
        error.exception.close()
        self.assertEqual(body["code"], "PROJECT_READINESS_ACTION_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
