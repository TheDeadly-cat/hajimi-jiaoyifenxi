from __future__ import annotations

import hashlib
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

from backend import http_server
from backend.decision_lineage import canonical_sha256
from backend.store import StudioStore


class HostDeliveryEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-host-delivery-"
        )
        self.temp_path = Path(self.temp_dir.name)
        self.database_path = self.temp_path / "host-delivery.sqlite3"
        self.store = StudioStore(self.database_path)
        self.frontend_dist = self.temp_path / "dist"
        self.frontend_dist.mkdir()
        self.index_body = b"<!doctype html><title>host delivery fixture</title>"
        self.index_path = self.frontend_dist / "index.html"
        self.index_path.write_bytes(self.index_body)

        self.original_store = http_server.STORE
        self.original_frontend_dist = http_server.FRONTEND_DIST
        http_server.STORE = self.store
        http_server.FRONTEND_DIST = self.frontend_dist

        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            http_server.StudioRequestHandler,
        )
        self.server.ai_studio_startup_ready = True
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        http_server.STORE = self.original_store
        http_server.FRONTEND_DIST = self.original_frontend_dist
        self.temp_dir.cleanup()

    def get_json(self, path: str) -> tuple[int, dict[str, str], dict]:
        try:
            with self.opener.open(f"{self.base_url}{path}", timeout=5) as response:
                status = response.status
                headers = dict(response.headers.items())
                body = response.read()
        except HTTPError as exc:
            status = exc.code
            headers = dict(exc.headers.items())
            body = exc.read()
        self.assertIn("application/json", headers.get("Content-Type", ""))
        return status, headers, json.loads(body.decode("utf-8"))

    def test_readiness_is_json_and_proves_all_local_startup_checks(self) -> None:
        with patch.object(
            http_server.PROVIDERS,
            "status",
            side_effect=AssertionError("readiness must not inspect providers"),
        ):
            status, headers, payload = self.get_json("/api/readiness")

        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["schema_version"], "host_readiness_v1")
        self.assertEqual(payload["service"]["id"], "ai_collaboration_studio")
        self.assertTrue(payload["checks"]["startup_gate"]["ready"])
        self.assertTrue(payload["checks"]["database"]["ready"])
        frontend = payload["checks"]["frontend_build"]
        self.assertTrue(frontend["ready"])
        self.assertEqual(frontend["index_bytes"], len(self.index_body))
        self.assertEqual(
            frontend["index_sha256"],
            hashlib.sha256(self.index_body).hexdigest(),
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(str(self.database_path), serialized)
        self.assertNotIn("session_token", serialized)

    def test_readiness_fails_closed_for_startup_or_frontend_gap(self) -> None:
        self.server.ai_studio_startup_ready = False
        status, _, payload = self.get_json("/api/readiness")
        self.assertEqual(status, 503)
        self.assertFalse(payload["ready"])
        self.assertFalse(payload["checks"]["startup_gate"]["ready"])
        self.assertFalse(payload["checks"]["database"]["ready"])

        self.server.ai_studio_startup_ready = True
        self.index_path.unlink()
        status, _, payload = self.get_json("/api/readiness")
        self.assertEqual(status, 503)
        self.assertFalse(payload["ready"])
        self.assertTrue(payload["checks"]["database"]["ready"])
        self.assertFalse(payload["checks"]["frontend_build"]["ready"])

    def test_version_matches_package_and_identifies_frontend_build(self) -> None:
        with patch.object(
            http_server.PROVIDERS,
            "status",
            side_effect=AssertionError("version must not inspect providers"),
        ):
            status, headers, payload = self.get_json("/api/version")

        package = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "frontend"
                / "package.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["schema_version"], "host_version_v2")
        self.assertEqual(payload["service"]["version"], package["version"])
        self.assertEqual(
            payload["api"],
            {
                "contract_version": "host_delivery_v1",
                "readiness_schema_version": "host_readiness_v1",
                "version_schema_version": "host_version_v2",
            },
        )
        backend_build = payload["backend_build"]
        self.assertEqual(
            backend_build,
            http_server.BACKEND_BUILD_IDENTITY_AT_STARTUP,
        )
        self.assertTrue(backend_build["available"])
        self.assertGreater(backend_build["source_file_count"], 0)
        self.assertRegex(backend_build["source_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            payload["frontend_build"],
            {
                "available": True,
                "index_bytes": len(self.index_body),
                "index_sha256": hashlib.sha256(self.index_body).hexdigest(),
            },
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(str(self.temp_path), serialized)
        self.assertNotIn("session_token", serialized)

    def test_integration_manifest_is_static_closed_and_hash_stable(self) -> None:
        with (
            patch.object(http_server, "STORE", object()),
            patch.object(
                http_server.PROVIDERS,
                "status",
                side_effect=AssertionError(
                    "integration manifest must not inspect providers"
                ),
            ),
            patch.object(
                http_server,
                "frontend_build_identity",
                side_effect=AssertionError(
                    "integration manifest must not inspect the filesystem"
                ),
            ),
        ):
            first_status, first_headers, first = self.get_json(
                "/api/integration/manifest"
            )
            second_status, second_headers, second = self.get_json(
                "/api/integration/manifest"
            )

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(first_headers.get("Cache-Control"), "no-store")
        self.assertEqual(second_headers.get("Cache-Control"), "no-store")
        self.assertEqual(first, second)
        self.assertEqual(
            set(first),
            {
                "ok",
                "schema_version",
                "service",
                "api",
                "kernel",
                "capability_registry",
                "collaboration",
                "safety",
                "manifest_sha256",
            },
        )
        self.assertTrue(first["ok"])
        self.assertEqual(
            first["schema_version"],
            "studio_integration_manifest_v2",
        )
        self.assertEqual(
            set(first["service"]),
            {"id", "name", "version"},
        )
        self.assertEqual(first["service"]["id"], "ai_collaboration_studio")
        self.assertEqual(
            first["api"]["host_contract_version"],
            "host_delivery_v1",
        )
        self.assertEqual(
            set(first["api"]),
            {
                "host_contract_version",
                "manifest",
                "external_mutation",
                "project_invocation",
                "embedding",
            },
        )
        self.assertEqual(
            first["api"]["manifest"],
            {
                "method": "GET",
                "path": "/api/integration/manifest",
                "query_parameters_allowed": False,
                "cache_policy": "no-store",
            },
        )
        self.assertEqual(
            set(first["api"]["external_mutation"]),
            {
                "available",
                "contract_version",
                "status",
                "bootstrap_credential_is_external_contract",
                "user_confirmation_required",
                "execution_capability",
            },
        )
        self.assertEqual(
            set(first["api"]["embedding"]),
            {"iframe_allowed", "cross_origin_mutation_allowed"},
        )
        self.assertTrue(first["api"]["external_mutation"]["available"])
        self.assertEqual(
            first["api"]["external_mutation"]["contract_version"],
            "project_invocation_envelope_v1",
        )
        self.assertEqual(
            first["api"]["external_mutation"]["execution_capability"],
            "none",
        )
        self.assertFalse(
            first["api"]["external_mutation"][
                "bootstrap_credential_is_external_contract"
            ]
        )
        invocation = first["api"]["project_invocation"]
        self.assertEqual(
            set(invocation),
            {
                "implementation_available",
                "runtime_authorization_state",
                "envelope_version",
                "intake",
                "result",
                "authorization",
                "identity_binding",
                "idempotency_scope",
                "room_id_derivation",
                "input_delivery",
                "source_fetch_performed",
                "raw_payload_accepted",
                "retention_enforcement",
                "user_confirmation_boundary",
            },
        )
        self.assertEqual(
            invocation["intake"]["path"],
            "/api/integration/project-invocations",
        )
        self.assertEqual(
            invocation["result"]["path_template"],
            "/api/integration/project-invocations/{client_request_id}/result",
        )
        self.assertEqual(
            invocation["authorization"]["capability_version"],
            "project_invocation_capability_v1",
        )
        self.assertFalse(
            invocation["authorization"]["bootstrap_credential_accepted"]
        )
        self.assertEqual(invocation["input_delivery"], "hash_manifest_only")
        self.assertFalse(invocation["source_fetch_performed"])
        self.assertFalse(invocation["raw_payload_accepted"])
        self.assertEqual(
            invocation["retention_enforcement"],
            {
                "no_payload_all_classifications": True,
                "time_bounded_room_payload_persisted": False,
                "time_bounded_result_read_expiry_status": 410,
                "audit_hash_metadata_retained": True,
            },
        )
        self.assertFalse(first["api"]["embedding"]["iframe_allowed"])
        self.assertFalse(
            first["api"]["embedding"]["cross_origin_mutation_allowed"]
        )
        self.assertEqual(
            set(first["kernel"]),
            {"room_kernel_version", "extension_mode", "dynamic_code_loading"},
        )
        self.assertFalse(first["kernel"]["dynamic_code_loading"])

        registry = first["capability_registry"]
        self.assertEqual(
            set(registry),
            {
                "catalog_version",
                "catalog_sha256",
                "catalog_endpoint",
                "identity_fields",
                "exact_version_resolution_required",
                "append_only_hash_chained_history",
                "latest_alias_is_sealed",
                "legacy_catalog_version",
                "legacy_catalog_sha256",
                "runtime_lifecycle_state_evaluated",
                "counts",
                "capability_packs",
                "implemented_ports",
            },
        )
        self.assertEqual(registry["catalog_version"], "plugin_registry_catalog_v3")
        self.assertRegex(registry["catalog_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            registry["identity_fields"],
            ["kind", "stable_id", "exact_version"],
        )
        self.assertTrue(registry["exact_version_resolution_required"])
        self.assertTrue(registry["append_only_hash_chained_history"])
        self.assertTrue(registry["latest_alias_is_sealed"])
        self.assertEqual(
            registry["legacy_catalog_version"],
            "plugin_registry_catalog_v2",
        )
        self.assertFalse(registry["runtime_lifecycle_state_evaluated"])
        self.assertEqual(
            set(registry["counts"]),
            {
                "capability_packs",
                "domain_adapters",
                "implemented_ports",
                "ui_contributions",
            },
        )
        self.assertEqual(
            registry["counts"]["capability_packs"],
            len(registry["capability_packs"]),
        )
        self.assertEqual(
            registry["counts"]["implemented_ports"],
            len(registry["implemented_ports"]),
        )
        pack_keys = {
            "id",
            "manifest_version",
            "manifest_sha256",
            "pack_version",
            "dependencies",
            "domain_adapter_ids",
            "required_ports",
            "capabilities",
            "system_managed",
            "execution_capability",
            "live_trading_allowed",
        }
        for pack in registry["capability_packs"]:
            self.assertEqual(set(pack), pack_keys)
            self.assertRegex(pack["manifest_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(pack["execution_capability"], "none")
            self.assertFalse(pack["live_trading_allowed"])
            for requirement in pack["required_ports"]:
                self.assertEqual(
                    set(requirement),
                    {"port_id", "version_range", "requirement", "cardinality"},
                )
        port_keys = {
            "port_id",
            "port_version",
            "input_schema_version",
            "input_schema_sha256",
            "output_schema_version",
            "output_schema_sha256",
            "provider_call_budget",
            "market_read_budget",
            "business_write_budget",
            "external_write_allowed",
            "failure_policy",
            "contract_sha256",
        }
        implemented_port_ids = set()
        for port in registry["implemented_ports"]:
            self.assertEqual(set(port), port_keys)
            self.assertRegex(port["input_schema_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(port["output_schema_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(port["contract_sha256"], r"^[0-9a-f]{64}$")
            self.assertFalse(port["external_write_allowed"])
            self.assertEqual(port["failure_policy"], "fail_closed")
            implemented_port_ids.add(port["port_id"])
        self.assertNotIn("core.turn.payload/v1", implemented_port_ids)
        self.assertNotIn("core.simulation.local/v1", implemented_port_ids)

        collaboration = first["collaboration"]
        self.assertEqual(
            set(collaboration),
            {
                "portable_result",
                "manual_chatgpt",
                "presentation",
                "readonly_mcp",
            },
        )
        portable = collaboration["portable_result"]
        self.assertEqual(portable["contract_version"], "collaboration_result_v1")
        self.assertRegex(portable["schema_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            [item["profile_version"] for item in portable["profiles"]],
            ["artifact_draft_v1", "decision_v1", "research_report_v1"],
        )
        self.assertTrue(portable["independent_review_separate"])
        self.assertTrue(portable["user_final_decision_separate"])
        presentation = collaboration["presentation"]
        self.assertEqual(
            presentation["ingest_contract_version"],
            "pptx_ingest_package_v1",
        )
        self.assertFalse(presentation["macro_ole_activex_allowed"])
        self.assertFalse(presentation["external_relationships_fetched"])
        self.assertTrue(presentation["explicit_user_acceptance_required"])
        manual = collaboration["manual_chatgpt"]
        self.assertEqual(
            set(manual),
            {
                "workflow_available",
                "session_contract_version",
                "bundle_contract_version",
                "accepted_import_contract_versions",
                "preferred_import_contract_version",
                "result_contract_version",
                "interaction_mode",
                "automatic_chatgpt_dispatch_available",
                "declared_model_identity_verified",
                "independent_api_review_stage_available",
                "independent_api_review_required_before_user_decision",
                "api_review_provider_runtime_state",
                "result_import_authority",
            },
        )
        self.assertTrue(manual["workflow_available"])
        self.assertEqual(manual["interaction_mode"], "manual_copy_paste")
        self.assertFalse(manual["automatic_chatgpt_dispatch_available"])
        self.assertFalse(manual["declared_model_identity_verified"])
        self.assertTrue(
            manual["independent_api_review_required_before_user_decision"]
        )
        self.assertEqual(manual["api_review_provider_runtime_state"], "not_probed")
        self.assertEqual(manual["result_import_authority"], "host_application_only")
        readonly_mcp = collaboration["readonly_mcp"]
        self.assertEqual(
            set(readonly_mcp),
            {
                "implementation_available",
                "served_by_main_host",
                "runtime_state",
                "server_name",
                "server_version",
                "endpoint_path",
                "preferred_protocol_version",
                "supported_protocol_versions",
                "capability_contract_version",
                "projection_version",
                "authorization_scope",
                "tools",
                "tool_contracts",
                "read_only",
                "write_tools_available",
                "result_import_authority",
            },
        )
        self.assertTrue(readonly_mcp["implementation_available"])
        self.assertFalse(readonly_mcp["served_by_main_host"])
        self.assertEqual(readonly_mcp["runtime_state"], "not_probed")
        self.assertTrue(readonly_mcp["read_only"])
        self.assertFalse(readonly_mcp["write_tools_available"])
        self.assertEqual(
            readonly_mcp["tools"],
            [
                "get_evidence_chunk",
                "get_import_contract",
                "get_room_bundle",
                "get_round_status",
            ],
        )
        self.assertEqual(
            [item["name"] for item in readonly_mcp["tool_contracts"]],
            readonly_mcp["tools"],
        )
        for tool in readonly_mcp["tool_contracts"]:
            self.assertEqual(
                set(tool),
                {
                    "name",
                    "input_schema_sha256",
                    "output_schema_sha256",
                    "annotations_sha256",
                    "read_only",
                    "contract_sha256",
                },
            )
            self.assertTrue(tool["read_only"])
            for hash_field in (
                "input_schema_sha256",
                "output_schema_sha256",
                "annotations_sha256",
                "contract_sha256",
            ):
                self.assertRegex(tool[hash_field], r"\A[a-f0-9]{64}\Z")

        safety = first["safety"]
        self.assertEqual(
            set(safety),
            {
                "execution_capability",
                "live_trading_allowed",
                "can_autonomously_decide",
                "can_replace_user_decision",
                "arbitrary_code_loading_allowed",
                "user_final_decision_required",
                "database_reads_performed",
                "database_writes_performed",
                "provider_calls_performed",
                "market_reads_performed",
                "network_requests_performed",
                "credential_material_returned",
                "local_filesystem_locations_returned",
                "external_mutation_contract_available",
                "external_mutation_scope",
                "bootstrap_credential_accepted_for_external_mutation",
                "cross_origin_mutation_allowed",
                "iframe_allowed",
            },
        )
        for zero_field in (
            "database_reads_performed",
            "database_writes_performed",
            "provider_calls_performed",
            "market_reads_performed",
            "network_requests_performed",
        ):
            self.assertEqual(safety[zero_field], 0)
        self.assertFalse(safety["credential_material_returned"])
        self.assertFalse(safety["local_filesystem_locations_returned"])
        self.assertTrue(safety["external_mutation_contract_available"])
        self.assertEqual(
            safety["external_mutation_scope"],
            "project_room_and_intake_only",
        )
        self.assertFalse(
            safety["bootstrap_credential_accepted_for_external_mutation"]
        )
        self.assertFalse(safety["cross_origin_mutation_allowed"])
        self.assertFalse(safety["iframe_allowed"])

        hash_basis = dict(first)
        manifest_sha256 = hash_basis.pop("manifest_sha256")
        self.assertEqual(manifest_sha256, canonical_sha256(hash_basis))
        serialized = json.dumps(first, ensure_ascii=False, sort_keys=True)
        self.assertNotIn(str(self.temp_path), serialized)
        self.assertNotIn(str(self.database_path), serialized)
        self.assertNotIn(http_server.LOCAL_SESSION_TOKEN, serialized)
        self.assertNotRegex(serialized, r"(?i)[a-z]:[\\/]")
        for forbidden_key in (
            "session_token",
            "api_key",
            "database_path",
            "file_path",
            "signing_secret",
        ):
            self.assertNotIn(f'"{forbidden_key}"', serialized.lower())

    def test_host_delivery_queries_are_rejected(self) -> None:
        for endpoint in (
            "/api/readiness",
            "/api/version",
            "/api/integration/manifest",
        ):
            with self.subTest(endpoint=endpoint):
                status, _, payload = self.get_json(f"{endpoint}?verbose=1")
                self.assertEqual(status, 400)
                self.assertFalse(payload["ok"])
                self.assertEqual(
                    payload["error_code"],
                    "HOST_ENDPOINT_QUERY_UNSUPPORTED",
                )

    def test_versioned_plugin_catalog_endpoint_is_static_and_closed(self) -> None:
        with (
            patch.object(http_server, "STORE", object()),
            patch.object(
                http_server.PROVIDERS,
                "status",
                side_effect=AssertionError(
                    "plugin catalog must not inspect providers"
                ),
            ),
        ):
            status, headers, payload = self.get_json(
                "/api/plugin-registry/catalog/v3"
            )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(set(payload), {"ok", "catalog"})
        self.assertTrue(payload["ok"])
        catalog = payload["catalog"]
        self.assertEqual(catalog["version"], "plugin_registry_catalog_v3")
        self.assertRegex(catalog["catalog_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            set(catalog),
            {
                "version",
                "room_kernel_version",
                "histories",
                "latest_aliases",
                "safety",
                "catalog_sha256",
            },
        )

        query_status, _, query = self.get_json(
            "/api/plugin-registry/catalog/v3?latest=1"
        )
        self.assertEqual(query_status, 400)
        self.assertEqual(
            query["error_code"],
            "PLUGIN_REGISTRY_QUERY_UNSUPPORTED",
        )

    def test_unknown_api_get_never_falls_through_to_spa_html(self) -> None:
        status, _, payload = self.get_json("/api/not-a-real-endpoint")
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "API_NOT_FOUND")


class LauncherDeliveryContractTests(unittest.TestCase):
    def test_launcher_gates_on_versioned_readiness_not_liveness(self) -> None:
        launcher = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "start_ai_collaboration_studio.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("/api/readiness", launcher)
        self.assertIn("/api/version", launcher)
        self.assertIn("/api/integration/manifest", launcher)
        self.assertIn('"host_readiness_v1"', launcher)
        self.assertIn('"host_version_v1"', launcher)
        self.assertIn('"host_version_v2"', launcher)
        self.assertIn('"studio_integration_manifest_v2"', launcher)
        self.assertIn("Get-BackendSourceSha256", launcher)
        self.assertIn("backend_build.source_sha256", launcher)
        self.assertIn('"ai_collaboration_studio"', launcher)
        self.assertIn(
            'runtime\\bootstrap\\python\\Scripts\\python.exe',
            launcher,
        )
        self.assertNotIn("/api/health", launcher)
        self.assertNotIn("Test-StudioHealth", launcher)

    def test_launcher_classifies_migration_failures_without_automatic_repair(self) -> None:
        launcher = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "start_ai_collaboration_studio.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("DatabaseMigrationRequired", launcher)
        self.assertIn("DatabaseMigrationRecoveryRequired", launcher)
        self.assertIn("previous authorization", launcher)
        self.assertIn("No automatic migration was attempted", launcher)
        self.assertIn("The launcher did not stop or replace it", launcher)
        self.assertNotIn("Stop-Process", launcher)
        self.assertNotIn("database_migration apply", launcher)


if __name__ == "__main__":
    unittest.main()
