from __future__ import annotations

from copy import deepcopy
from typing import Any

from .collaboration_result import (
    COLLABORATION_PROFILE_SCHEMA_SHA256,
    COLLABORATION_RESULT_SCHEMA_SHA256,
    COLLABORATION_RESULT_VERSION,
)
from .decision_lineage import canonical_sha256
from .manual_chatgpt import (
    MANUAL_CHATGPT_BUNDLE_VERSION,
    MANUAL_CHATGPT_IMPORT_CONTRACT_VERSION,
    MANUAL_CHATGPT_RESULT_VERSION,
    MANUAL_CHATGPT_SESSION_VERSION,
    SUPPORTED_MANUAL_CHATGPT_IMPORT_CONTRACT_VERSIONS,
)
from .plugin_registry import (
    ROOM_KERNEL_VERSION,
    plugin_registry_catalog,
    plugin_registry_catalog_v3,
)
from .presentation_package import (
    ARTIFACT_RENDER_PACKAGE_VERSION,
    ARTIFACT_RENDER_VERIFICATION_RECEIPT_VERSION,
    PPTX_CONTENT_TYPE,
    PPTX_INGEST_PACKAGE_VERSION,
)
from .project_invocation import (
    MAX_PROJECT_CAPABILITY_TTL_SECONDS,
    PROJECT_CAPABILITY_AUDIENCE,
    PROJECT_CAPABILITY_VERSION,
    PROJECT_INVOCATION_ACTION_INTAKE,
    PROJECT_INVOCATION_ACTION_RESULT_READ,
    PROJECT_INVOCATION_ENVELOPE_VERSION,
    PROJECT_INVOCATION_INTAKE_PATH,
    PROJECT_INVOCATION_RESULT_PATH_TEMPLATE,
    SUPPORTED_PROJECT_INVOCATION_ACTIONS,
    SUPPORTED_WORKFLOW_RESULT_PROFILES,
)
from .readonly_mcp_gateway import (
    MCP_CAPABILITY_VERSION,
    MCP_ENDPOINT_PATH,
    MCP_GATEWAY_PROJECTION_VERSION,
    MCP_PROTOCOL_VERSION,
    MCP_SERVER_NAME,
    MCP_SERVER_VERSION,
    MCP_SUPPORTED_PROTOCOL_VERSIONS,
    mcp_tool_definitions,
)


STUDIO_INTEGRATION_MANIFEST_VERSION = "studio_integration_manifest_v2"
STUDIO_INTEGRATION_MANIFEST_PATH = "/api/integration/manifest"
PLUGIN_REGISTRY_CATALOG_V3_PATH = "/api/plugin-registry/catalog/v3"

def _capability_pack_summary(pack: dict[str, Any]) -> dict[str, Any]:
    requirements = sorted(
        (
            {
                "port_id": str(item.get("port_id") or ""),
                "version_range": str(item.get("version_range") or ""),
                "requirement": str(item.get("requirement") or ""),
                "cardinality": str(item.get("cardinality") or ""),
            }
            for item in pack.get("domain_adapter_port_requirements") or []
            if isinstance(item, dict)
        ),
        key=lambda item: (
            item["port_id"],
            item["version_range"],
            item["requirement"],
            item["cardinality"],
        ),
    )
    return {
        "id": str(pack.get("id") or ""),
        "manifest_version": str(pack.get("manifest_version") or ""),
        "manifest_sha256": str(pack.get("manifest_sha256") or ""),
        "pack_version": str(pack.get("pack_version") or ""),
        "dependencies": sorted(str(item) for item in pack.get("dependencies") or []),
        "domain_adapter_ids": sorted(
            str(item) for item in pack.get("domain_adapter_ids") or []
        ),
        "required_ports": requirements,
        "capabilities": sorted(
            str(item) for item in pack.get("capabilities") or []
        ),
        "system_managed": pack.get("system_managed") is True,
        "execution_capability": str(pack.get("execution_capability") or ""),
        "live_trading_allowed": pack.get("live_trading_allowed") is True,
    }


def _port_summary(port: dict[str, Any]) -> dict[str, Any]:
    input_schema = port.get("input_schema")
    output_schema = port.get("output_schema")
    return {
        "port_id": str(port.get("port_id") or ""),
        "port_version": str(port.get("port_version") or ""),
        "input_schema_version": str(
            (input_schema.get("version") or "")
            if isinstance(input_schema, dict)
            else ""
        ),
        "input_schema_sha256": str(port.get("input_schema_sha256") or ""),
        "output_schema_version": str(
            (output_schema.get("version") or "")
            if isinstance(output_schema, dict)
            else ""
        ),
        "output_schema_sha256": str(port.get("output_schema_sha256") or ""),
        "provider_call_budget": int(port.get("provider_call_budget") or 0),
        "market_read_budget": int(port.get("market_read_budget") or 0),
        "business_write_budget": int(port.get("business_write_budget") or 0),
        "external_write_allowed": port.get("external_write_allowed") is True,
        "failure_policy": str(port.get("failure_policy") or ""),
        "contract_sha256": str(port.get("contract_sha256") or ""),
    }


def _mcp_tool_contract_summary(tool: dict[str, Any]) -> dict[str, Any]:
    input_schema = tool.get("inputSchema")
    output_schema = tool.get("outputSchema")
    annotations = tool.get("annotations")
    summary = {
        "name": str(tool.get("name") or ""),
        "input_schema_sha256": canonical_sha256(
            input_schema if isinstance(input_schema, dict) else {}
        ),
        "output_schema_sha256": canonical_sha256(
            output_schema if isinstance(output_schema, dict) else {}
        ),
        "annotations_sha256": canonical_sha256(
            annotations if isinstance(annotations, dict) else {}
        ),
        "read_only": (
            isinstance(annotations, dict)
            and annotations.get("readOnlyHint") is True
            and annotations.get("destructiveHint") is False
            and annotations.get("openWorldHint") is False
        ),
    }
    summary["contract_sha256"] = canonical_sha256(summary)
    return summary


def build_studio_integration_manifest(
    *,
    service_id: str,
    service_name: str,
    service_version: str,
    host_api_contract_version: str,
) -> dict[str, Any]:
    """Build the static cross-project discovery contract.

    This function intentionally has no Store, Provider, filesystem, environment,
    or network dependency.  It describes compiled capabilities only; it does not
    claim that the optional MCP process is running or that any Provider is ready.
    """

    catalog = plugin_registry_catalog()
    versioned_catalog = plugin_registry_catalog_v3()
    packs = sorted(
        (
            _capability_pack_summary(pack)
            for pack in catalog.get("capability_packs") or []
            if isinstance(pack, dict)
        ),
        key=lambda item: (item["id"], item["pack_version"]),
    )
    ports = sorted(
        (
            _port_summary(port)
            for port in catalog.get("domain_adapter_ports") or []
            if isinstance(port, dict)
        ),
        key=lambda item: (item["port_id"], item["port_version"]),
    )
    mcp_tool_contracts = sorted(
        (
            _mcp_tool_contract_summary(tool)
            for tool in mcp_tool_definitions()
            if isinstance(tool, dict)
        ),
        key=lambda item: item["name"],
    )
    registry_safety = catalog.get("safety")
    if not isinstance(registry_safety, dict):
        registry_safety = {}

    manifest: dict[str, Any] = {
        "ok": True,
        "schema_version": STUDIO_INTEGRATION_MANIFEST_VERSION,
        "service": {
            "id": str(service_id),
            "name": str(service_name),
            "version": str(service_version),
        },
        "api": {
            "host_contract_version": str(host_api_contract_version),
            "manifest": {
                "method": "GET",
                "path": STUDIO_INTEGRATION_MANIFEST_PATH,
                "query_parameters_allowed": False,
                "cache_policy": "no-store",
            },
            "external_mutation": {
                "available": True,
                "contract_version": PROJECT_INVOCATION_ENVELOPE_VERSION,
                "status": "implemented_runtime_authorization_not_probed",
                "bootstrap_credential_is_external_contract": False,
                "user_confirmation_required": True,
                "execution_capability": "none",
            },
            "project_invocation": {
                "implementation_available": True,
                "runtime_authorization_state": "not_probed",
                "envelope_version": PROJECT_INVOCATION_ENVELOPE_VERSION,
                "intake": {
                    "method": "POST",
                    "path": PROJECT_INVOCATION_INTAKE_PATH,
                    "query_parameters_allowed": False,
                    "content_type": "application/json",
                    "idempotent_replay_status": 200,
                    "created_status": 201,
                },
                "result": {
                    "method": "GET",
                    "path_template": PROJECT_INVOCATION_RESULT_PATH_TEMPLATE,
                    "query_parameters_allowed": False,
                    "cache_policy": "no-store",
                },
                "authorization": {
                    "scheme": "Bearer",
                    "header": "Authorization",
                    "capability_version": PROJECT_CAPABILITY_VERSION,
                    "audience": PROJECT_CAPABILITY_AUDIENCE,
                    "actions": sorted(SUPPORTED_PROJECT_INVOCATION_ACTIONS),
                    "intake_action": PROJECT_INVOCATION_ACTION_INTAKE,
                    "result_read_action": PROJECT_INVOCATION_ACTION_RESULT_READ,
                    "maximum_ttl_seconds": MAX_PROJECT_CAPABILITY_TTL_SECONDS,
                    "bootstrap_credential_accepted": False,
                    "provisioning": "trusted_operator_out_of_band",
                },
                "identity_binding": [
                    "caller_id",
                    "project_id",
                    "client_request_id",
                    "request_sha256",
                    "room_id",
                ],
                "idempotency_scope": [
                    "caller_id",
                    "project_id",
                    "client_request_id",
                ],
                "room_id_derivation": "deterministic_sha256",
                "input_delivery": "hash_manifest_only",
                "source_fetch_performed": False,
                "raw_payload_accepted": False,
                "retention_enforcement": {
                    "no_payload_all_classifications": True,
                    "time_bounded_room_payload_persisted": False,
                    "time_bounded_result_read_expiry_status": 410,
                    "audit_hash_metadata_retained": True,
                },
                "user_confirmation_boundary": "before_room_creation",
            },
            "embedding": {
                "iframe_allowed": False,
                "cross_origin_mutation_allowed": False,
            },
        },
        "kernel": {
            "room_kernel_version": ROOM_KERNEL_VERSION,
            "extension_mode": "builtin_static_contracts",
            "dynamic_code_loading": False,
        },
        "capability_registry": {
            "catalog_version": str(versioned_catalog.get("version") or ""),
            "catalog_sha256": str(
                versioned_catalog.get("catalog_sha256") or ""
            ),
            "catalog_endpoint": {
                "method": "GET",
                "path": PLUGIN_REGISTRY_CATALOG_V3_PATH,
                "query_parameters_allowed": False,
                "cache_policy": "no-store",
            },
            "identity_fields": ["kind", "stable_id", "exact_version"],
            "exact_version_resolution_required": True,
            "append_only_hash_chained_history": True,
            "latest_alias_is_sealed": True,
            "legacy_catalog_version": str(catalog.get("version") or ""),
            "legacy_catalog_sha256": str(catalog.get("catalog_sha256") or ""),
            "runtime_lifecycle_state_evaluated": False,
            "counts": {
                "capability_packs": len(packs),
                "domain_adapters": len(catalog.get("domain_adapters") or []),
                "implemented_ports": len(ports),
                "ui_contributions": len(catalog.get("ui_contributions") or []),
            },
            "capability_packs": packs,
            "implemented_ports": ports,
        },
        "collaboration": {
            "portable_result": {
                "contract_version": COLLABORATION_RESULT_VERSION,
                "schema_sha256": COLLABORATION_RESULT_SCHEMA_SHA256,
                "profiles": [
                    {
                        "workflow_kind": workflow_kind,
                        "profile_version": profile_version,
                        "schema_sha256": COLLABORATION_PROFILE_SCHEMA_SHA256[
                            profile_version
                        ],
                    }
                    for workflow_kind, profile_version in sorted(
                        SUPPORTED_WORKFLOW_RESULT_PROFILES.items()
                    )
                ],
                "independent_review_separate": True,
                "user_final_decision_separate": True,
                "execution_capability": "none",
            },
            "manual_chatgpt": {
                "workflow_available": True,
                "session_contract_version": MANUAL_CHATGPT_SESSION_VERSION,
                "bundle_contract_version": MANUAL_CHATGPT_BUNDLE_VERSION,
                "accepted_import_contract_versions": sorted(
                    SUPPORTED_MANUAL_CHATGPT_IMPORT_CONTRACT_VERSIONS
                ),
                "preferred_import_contract_version": (
                    MANUAL_CHATGPT_IMPORT_CONTRACT_VERSION
                ),
                "result_contract_version": MANUAL_CHATGPT_RESULT_VERSION,
                "interaction_mode": "manual_copy_paste",
                "automatic_chatgpt_dispatch_available": False,
                "declared_model_identity_verified": False,
                "independent_api_review_stage_available": True,
                "independent_api_review_required_before_user_decision": True,
                "api_review_provider_runtime_state": "not_probed",
                "result_import_authority": "host_application_only",
            },
            "presentation": {
                "pptx_content_type": PPTX_CONTENT_TYPE,
                "ingest_contract_version": PPTX_INGEST_PACKAGE_VERSION,
                "render_package_version": ARTIFACT_RENDER_PACKAGE_VERSION,
                "verification_receipt_version": (
                    ARTIFACT_RENDER_VERIFICATION_RECEIPT_VERSION
                ),
                "macro_ole_activex_allowed": False,
                "external_relationships_fetched": False,
                "render_verification_required": True,
                "explicit_user_acceptance_required": True,
            },
            "readonly_mcp": {
                "implementation_available": True,
                "served_by_main_host": False,
                "runtime_state": "not_probed",
                "server_name": MCP_SERVER_NAME,
                "server_version": MCP_SERVER_VERSION,
                "endpoint_path": MCP_ENDPOINT_PATH,
                "preferred_protocol_version": MCP_PROTOCOL_VERSION,
                "supported_protocol_versions": sorted(
                    MCP_SUPPORTED_PROTOCOL_VERSIONS
                ),
                "capability_contract_version": MCP_CAPABILITY_VERSION,
                "projection_version": MCP_GATEWAY_PROJECTION_VERSION,
                "authorization_scope": "room_id_round_id_ttl",
                "tools": [item["name"] for item in mcp_tool_contracts],
                "tool_contracts": mcp_tool_contracts,
                "read_only": True,
                "write_tools_available": False,
                "result_import_authority": "host_application_only",
            },
        },
        "safety": {
            **deepcopy(registry_safety),
            "database_reads_performed": 0,
            "database_writes_performed": 0,
            "provider_calls_performed": 0,
            "market_reads_performed": 0,
            "network_requests_performed": 0,
            "credential_material_returned": False,
            "local_filesystem_locations_returned": False,
            "external_mutation_contract_available": True,
            "external_mutation_scope": "project_room_and_intake_only",
            "bootstrap_credential_accepted_for_external_mutation": False,
            "cross_origin_mutation_allowed": False,
            "iframe_allowed": False,
        },
    }
    manifest["manifest_sha256"] = canonical_sha256(manifest)
    return manifest


__all__ = [
    "PLUGIN_REGISTRY_CATALOG_V3_PATH",
    "STUDIO_INTEGRATION_MANIFEST_PATH",
    "STUDIO_INTEGRATION_MANIFEST_VERSION",
    "build_studio_integration_manifest",
]
