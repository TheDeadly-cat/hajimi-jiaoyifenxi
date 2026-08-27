from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Iterable

from .capability_packs import capability_pack_catalog, clean_capability_pack_ids
from .decision_lineage import canonical_sha256


PLUGIN_REGISTRY_CATALOG_VERSION_V1 = "plugin_registry_catalog_v1"
PLUGIN_REGISTRY_CATALOG_VERSION_V2 = "plugin_registry_catalog_v2"
PLUGIN_REGISTRY_CATALOG_VERSION_V3 = "plugin_registry_catalog_v3"
PLUGIN_REGISTRY_CATALOG_VERSION = PLUGIN_REGISTRY_CATALOG_VERSION_V2
PLUGIN_REGISTRY_SNAPSHOT_VERSION = "plugin_registry_snapshot_v1"
PLUGIN_REGISTRY_SNAPSHOT_VERSION_V1 = PLUGIN_REGISTRY_SNAPSHOT_VERSION
PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2 = "plugin_registry_snapshot_v2"
DOMAIN_ADAPTER_CONTRACT_VERSION = "domain_adapter_contract_v1"
DOMAIN_ADAPTER_CONTRACT_VERSION_V1 = DOMAIN_ADAPTER_CONTRACT_VERSION
DOMAIN_ADAPTER_CONTRACT_VERSION_V2 = "domain_adapter_contract_v2"
DOMAIN_ADAPTER_PORT_CONTRACT_VERSION = "domain_adapter_port_contract_v1"
UI_CONTRIBUTION_CONTRACT_VERSION = "ui_contribution_contract_v1"
UI_CONTRIBUTION_CONTRACT_VERSION_V1 = UI_CONTRIBUTION_CONTRACT_VERSION
UI_CONTRIBUTION_CONTRACT_VERSION_V2 = "ui_contribution_contract_v2"
ROOM_KERNEL_VERSION = "1.0.0"
CAPABILITY_PACK_MANIFEST_VERSION_V1 = "capability_pack_manifest_v1"
CAPABILITY_PACK_MANIFEST_VERSION_V2 = "capability_pack_manifest_v2"

SEMVER_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
FROZEN_PACK_ID_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,79})")

FIXED_PLUGIN_SAFETY = {
    "execution_capability": "none",
    "live_trading_allowed": False,
    "can_autonomously_decide": False,
    "can_replace_user_decision": False,
    "arbitrary_code_loading_allowed": False,
    "user_final_decision_required": True,
}

PLUGIN_REGISTRY_CONTRACT_KINDS = (
    "capability_pack",
    "domain_adapter",
    "domain_adapter_port",
    "ui_contribution",
    "ui_view_model_schema",
)

# Historical contracts are deliberately separate from the mutable current
# implementation maps above.  When a built-in contract advances, its exact
# sealed predecessor is appended here before the current pointer changes.  The
# v2 catalog and room snapshot builders never consult this ledger.
_PLUGIN_REGISTRY_HISTORICAL_CONTRACTS: dict[str, list[dict[str, Any]]] = {
    kind: [] for kind in PLUGIN_REGISTRY_CONTRACT_KINDS
}

HOST_UI_VIEW_MODEL_SCHEMAS: dict[str, dict[str, Any]] = {
    "project_readiness_view_model_v1": {
        "schema_version": "project_readiness_view_model_v1",
        "component_key": "project_readiness_review",
        "type": "object",
        "required": [
            "version",
            "integrity_ok",
            "metrics_visible",
            "room_id",
            "artifact_id",
            "artifact_version",
            "artifact_snapshot_sha256",
            "evidence_graph_sha256",
            "plugin_registry_snapshot_sha256",
            "resolution",
            "state",
            "structural_gaps",
            "blockers",
            "evidence_gaps",
            "provider_calls_performed",
            "market_reads_performed",
            "business_writes_performed",
            "execution_capability",
            "live_trading_allowed",
            "can_autonomously_decide",
            "can_replace_user_decision",
            "arbitrary_code_loading_allowed",
            "ranking_produced",
            "winner_claim",
            "approval_produced",
            "user_final_decision_required",
        ],
        "fields": {
            "version": "project_readiness_projection_v1",
            "integrity_ok": "boolean:true",
            "metrics_visible": "boolean:true",
            "room_id": "nonempty_string",
            "artifact_id": "nonempty_string",
            "artifact_version": "positive_integer",
            "artifact_snapshot_sha256": "sha256",
            "evidence_graph_sha256": "sha256",
            "plugin_registry_snapshot_sha256": "sha256",
            "resolution": "project_readiness_resolution_v1",
            "state": "ready|gaps_present|blocked",
            "structural_gaps": "array<project_readiness_gap_v1>",
            "blockers": "array<project_readiness_gap_v1>",
            "evidence_gaps": "array<project_readiness_gap_v1>",
            "provider_calls_performed": "integer:0",
            "market_reads_performed": "integer:0",
            "business_writes_performed": "integer:0",
            "execution_capability": "none",
            "live_trading_allowed": "boolean:false",
            "can_autonomously_decide": "boolean:false",
            "can_replace_user_decision": "boolean:false",
            "arbitrary_code_loading_allowed": "boolean:false",
            "ranking_produced": "boolean:false",
            "winner_claim": "boolean:false",
            "approval_produced": "boolean:false",
            "user_final_decision_required": "boolean:true",
        },
        "additional_properties": False,
    },
    "project_round_focus_view_model_v1": {
        "schema_version": "project_round_focus_view_model_v1",
        "component_key": "project_round_focus",
        "type": "object",
        "required": [
            "version",
            "integrity_ok",
            "metrics_visible",
            "room_id",
            "artifact_binding",
            "plugin_registry_snapshot_sha256",
            "input_seal_sha256",
            "resolution",
            "state",
            "counts",
            "focus_items",
            "suggested_objective",
            "preview_sha256",
            "provider_calls_performed",
            "market_reads_performed",
            "adapter_business_writes_performed",
            "host_lineage_write_required",
            "execution_capability",
            "live_trading_allowed",
            "can_autonomously_decide",
            "can_replace_user_decision",
            "arbitrary_code_loading_allowed",
            "ranking_produced",
            "winner_claim",
            "approval_produced",
            "member_assignment_produced",
            "workflow_mutation_performed",
            "user_final_decision_required",
        ],
        "fields": {
            "version": "project_round_focus_preview_v1",
            "integrity_ok": "boolean:true",
            "metrics_visible": "boolean:true",
            "room_id": "nonempty_string",
            "artifact_binding": "project_round_focus_artifact_binding_v1",
            "plugin_registry_snapshot_sha256": "sha256",
            "input_seal_sha256": "sha256",
            "resolution": "project_round_focus_resolution_v1",
            "state": "bootstrap|ready|gaps_present|blocked",
            "counts": "project_round_focus_counts_v1",
            "focus_items": "array<project_round_focus_item_v1>",
            "suggested_objective": "nonempty_string",
            "preview_sha256": "sha256",
            "provider_calls_performed": "integer:0",
            "market_reads_performed": "integer:0",
            "adapter_business_writes_performed": "integer:0",
            "host_lineage_write_required": "boolean:true",
            "execution_capability": "none",
            "live_trading_allowed": "boolean:false",
            "can_autonomously_decide": "boolean:false",
            "can_replace_user_decision": "boolean:false",
            "arbitrary_code_loading_allowed": "boolean:false",
            "ranking_produced": "boolean:false",
            "winner_claim": "boolean:false",
            "approval_produced": "boolean:false",
            "member_assignment_produced": "boolean:false",
            "workflow_mutation_performed": "boolean:false",
            "user_final_decision_required": "boolean:true",
        },
        "additional_properties": False,
    },
    "football_research_view_model_v1": {
        "schema_version": "football_research_view_model_v1",
        "component_key": "football_research_inspector",
        "type": "object",
        "required": [
            "version",
            "integrity_ok",
            "metrics_visible",
            "room_id",
            "contract",
            "contract_sha256",
            "data_cutoff_utc",
            "probability_state",
            "future_probability_available",
            "probability_metrics_visible",
            "odds_are_proxy_only",
            "provider_calls_performed",
            "market_reads_performed",
            "business_writes_performed",
            "execution_capability",
            "live_trading_allowed",
            "betting_allowed",
            "automatic_betting_allowed",
            "wallet_connection_allowed",
            "order_placement_allowed",
            "can_autonomously_decide",
            "can_replace_user_decision",
            "user_final_decision_required",
        ],
        "fields": {
            "version": "football_research_view_model_v1",
            "integrity_ok": "boolean:true",
            "metrics_visible": "boolean:false",
            "room_id": "nonempty_string",
            "contract": "football_research_contract_v1",
            "contract_sha256": "sha256",
            "data_cutoff_utc": "canonical_utc_timestamp",
            "probability_state": "withheld_no_calibration",
            "future_probability_available": "boolean:false",
            "probability_metrics_visible": "boolean:false",
            "odds_are_proxy_only": "boolean:true",
            "provider_calls_performed": "integer:0",
            "market_reads_performed": "integer:0",
            "business_writes_performed": "integer:0",
            "execution_capability": "none",
            "live_trading_allowed": "boolean:false",
            "betting_allowed": "boolean:false",
            "automatic_betting_allowed": "boolean:false",
            "wallet_connection_allowed": "boolean:false",
            "order_placement_allowed": "boolean:false",
            "can_autonomously_decide": "boolean:false",
            "can_replace_user_decision": "boolean:false",
            "user_final_decision_required": "boolean:true",
        },
        "additional_properties": False,
    },
    "stock_research_view_model_v1": {
        "schema_version": "stock_research_view_model_v1",
        "component_key": "stock_research_inspector",
        "type": "object",
        "required": [
            "version",
            "integrity_ok",
            "metrics_visible",
            "room_id",
            "stock_room_scope",
            "contract",
            "contract_sha256",
            "data_cutoff_utc",
            "research_ready",
            "symbol_preflights",
            "provider_calls_performed",
            "market_reads_performed",
            "business_writes_performed",
            "execution_capability",
            "live_trading_allowed",
            "order_placement_allowed",
            "wallet_connection_allowed",
            "automatic_trading_allowed",
            "can_autonomously_decide",
            "can_replace_user_decision",
            "user_final_decision_required",
        ],
        "fields": {
            "version": "stock_research_view_model_v1",
            "integrity_ok": "boolean:true",
            "metrics_visible": "boolean:true",
            "room_id": "nonempty_string",
            "stock_room_scope": "stock_room_scope_v1",
            "contract": "stock_research_contract_v1",
            "contract_sha256": "sha256",
            "data_cutoff_utc": "canonical_utc_timestamp",
            "research_ready": "boolean",
            "symbol_preflights": "array<stock_symbol_preflight_view_v1>",
            "provider_calls_performed": "integer:0",
            "market_reads_performed": "integer:0",
            "business_writes_performed": "integer:0",
            "execution_capability": "none",
            "live_trading_allowed": "boolean:false",
            "order_placement_allowed": "boolean:false",
            "wallet_connection_allowed": "boolean:false",
            "automatic_trading_allowed": "boolean:false",
            "can_autonomously_decide": "boolean:false",
            "can_replace_user_decision": "boolean:false",
            "user_final_decision_required": "boolean:true",
        },
        "additional_properties": False,
    },
}

HOST_DOMAIN_ADAPTER_PORT_IDS = frozenset({
    "core.artifact.projection/v1",
    "core.round.context/v1",
    "core.turn.payload/v1",
    "core.market.readonly_context/v1",
    "core.football.match_context/v1",
    "core.simulation.local/v1",
})
HOST_DOMAIN_ADAPTER_PORT_HANDLERS = {
    "core.artifact.projection/v1": "project_artifact",
    "core.round.context/v1": "project_round_context",
    "core.football.match_context/v1": "project_football_match_context",
    "core.market.readonly_context/v1": "project_market_readonly_context",
}

DOMAIN_ADAPTER_PORT_CONTRACTS: dict[str, dict[str, Any]] = {
    "core.football.match_context/v1": {
        "contract_version": DOMAIN_ADAPTER_PORT_CONTRACT_VERSION,
        "port_id": "core.football.match_context/v1",
        "port_version": "1.0.0",
        "handler_method": "project_football_match_context",
        "cardinality": "multiple",
        "input_schema": {
            "version": "football_match_context_input_v1",
            "type": "object",
            "required": ["contract"],
            "fields": {
                "contract": "football_research_contract_v1",
            },
            "additional_properties": False,
        },
        "output_schema": {
            "version": "football_match_context_output_v1",
            "type": "object",
            "required": [
                "version",
                "capability_pack_id",
                "match_identity",
                "data_cutoff_utc",
                "teams",
                "odds_proxies",
                "probability_state",
                "future_probability_available",
                "probability_metrics_visible",
                "odds_are_proxy_only",
                "execution_capability",
                "betting_allowed",
                "live_betting_allowed",
                "automatic_betting_allowed",
                "wallet_connection_allowed",
                "order_placement_allowed",
                "can_autonomously_decide",
                "can_replace_user_decision",
                "user_final_decision_required",
                "contract_sha256",
            ],
            "fields": {
                "version": "football_research_contract_v1",
                "capability_pack_id": "football_research_readonly",
                "match_identity": "football_match_identity_v1",
                "data_cutoff_utc": "canonical_utc_timestamp",
                "teams": "football_home_away_context_v1",
                "odds_proxies": "array<football_odds_proxy_field_v1>",
                "probability_state": "withheld_no_calibration",
                "future_probability_available": "boolean:false",
                "probability_metrics_visible": "boolean:false",
                "odds_are_proxy_only": "boolean:true",
                "execution_capability": "none",
                "betting_allowed": "boolean:false",
                "live_betting_allowed": "boolean:false",
                "automatic_betting_allowed": "boolean:false",
                "wallet_connection_allowed": "boolean:false",
                "order_placement_allowed": "boolean:false",
                "can_autonomously_decide": "boolean:false",
                "can_replace_user_decision": "boolean:false",
                "user_final_decision_required": "boolean:true",
                "contract_sha256": "sha256",
            },
            "additional_properties": False,
        },
        "read_surfaces": [
            "room.material.version.exact",
            "room.material.content_sha256.exact",
            "room.material.snapshot_sha256.exact",
        ],
        "local_write_surfaces": [],
        "provider_call_budget": 0,
        "market_read_budget": 0,
        "business_write_budget": 0,
        "external_write_allowed": False,
        "failure_policy": "fail_closed",
        **FIXED_PLUGIN_SAFETY,
    },
    "core.market.readonly_context/v1": {
        "contract_version": DOMAIN_ADAPTER_PORT_CONTRACT_VERSION,
        "port_id": "core.market.readonly_context/v1",
        "port_version": "1.0.0",
        "handler_method": "project_market_readonly_context",
        "cardinality": "multiple",
        "input_schema": {
            "version": "stock_market_readonly_context_input_v1",
            "type": "object",
            "required": ["contract"],
            "fields": {
                "contract": "stock_research_contract_v1",
            },
            "additional_properties": False,
        },
        "output_schema": {
            "version": "stock_market_readonly_context_output_v1",
            "type": "object",
            "required": [
                "version",
                "capability_pack_id",
                "stock_room_scope",
                "data_cutoff_utc",
                "symbols",
                "research_ready",
                "execution_capability",
                "live_trading_allowed",
                "order_placement_allowed",
                "wallet_connection_allowed",
                "automatic_trading_allowed",
                "can_autonomously_decide",
                "can_replace_user_decision",
                "user_final_decision_required",
                "contract_sha256",
            ],
            "fields": {
                "version": "stock_research_contract_v1",
                "capability_pack_id": "stock_research_readonly",
                "stock_room_scope": "stock_room_scope_v1",
                "data_cutoff_utc": "canonical_utc_timestamp",
                "symbols": "array<stock_symbol_research_v1>",
                "research_ready": "boolean",
                "execution_capability": "none",
                "live_trading_allowed": "boolean:false",
                "order_placement_allowed": "boolean:false",
                "wallet_connection_allowed": "boolean:false",
                "automatic_trading_allowed": "boolean:false",
                "can_autonomously_decide": "boolean:false",
                "can_replace_user_decision": "boolean:false",
                "user_final_decision_required": "boolean:true",
                "contract_sha256": "sha256",
            },
            "additional_properties": False,
        },
        "read_surfaces": [
            "room.stock_room_scope.exact",
            "room.material.version.exact",
            "room.material.content_sha256.exact",
            "room.material.snapshot_sha256.exact",
        ],
        "local_write_surfaces": [],
        "provider_call_budget": 0,
        "market_read_budget": 0,
        "business_write_budget": 0,
        "external_write_allowed": False,
        "failure_policy": "fail_closed",
        **FIXED_PLUGIN_SAFETY,
    },
    "core.artifact.projection/v1": {
        "contract_version": DOMAIN_ADAPTER_PORT_CONTRACT_VERSION,
        "port_id": "core.artifact.projection/v1",
        "port_version": "1.0.0",
        "handler_method": "project_artifact",
        "cardinality": "multiple",
        "input_schema": {
            "version": "artifact_projection_input_v1",
            "type": "object",
            "required": ["artifact", "evidence_relations"],
            "fields": {
                "artifact": "object",
                "evidence_relations": "array<object>",
            },
            "additional_properties": False,
        },
        "output_schema": {
            "version": "artifact_projection_output_v1",
            "type": "object",
            "required": [
                "version",
                "state",
                "requirement_gaps",
                "evidence_gaps",
                "risk_gaps",
                "blockers",
                "counts",
                "provider_calls_performed",
                "market_reads_performed",
                "business_writes_performed",
                "ranking_produced",
                "winner_claim",
                "approval_produced",
                "user_final_decision_required",
                "can_replace_user_decision",
                "arbitrary_code_loading_allowed",
            ],
            "fields": {
                "version": "project_readiness_projection_v1",
                "state": "ready|gaps_present|blocked",
                "requirement_gaps": "array<requirement_gap_v1>",
                "evidence_gaps": "array<evidence_gap_v1>",
                "risk_gaps": "array<risk_gap_v1>",
                "blockers": "array<project_blocker_v1>",
                "counts": "project_readiness_counts_v1",
                "provider_calls_performed": "integer:0",
                "market_reads_performed": "integer:0",
                "business_writes_performed": "integer:0",
                "ranking_produced": "boolean:false",
                "winner_claim": "boolean:false",
                "approval_produced": "boolean:false",
                "user_final_decision_required": "boolean:true",
                "can_replace_user_decision": "boolean:false",
                "arbitrary_code_loading_allowed": "boolean:false",
            },
            "additional_properties": False,
        },
        "read_surfaces": ["artifact.version.exact", "artifact.evidence_relations.exact"],
        "local_write_surfaces": [],
        "provider_call_budget": 0,
        "market_read_budget": 0,
        "business_write_budget": 0,
        "external_write_allowed": False,
        "failure_policy": "fail_closed",
        **FIXED_PLUGIN_SAFETY,
    },
    "core.round.context/v1": {
        "contract_version": DOMAIN_ADAPTER_PORT_CONTRACT_VERSION,
        "port_id": "core.round.context/v1",
        "port_version": "1.0.0",
        "handler_method": "project_round_context",
        "cardinality": "multiple",
        "input_schema": {
            "version": "project_round_context_input_v1",
            "type": "object",
            "required": ["artifact_binding", "readiness_projection", "room_context"],
            "fields": {
                "artifact_binding": "project_round_focus_artifact_binding_v1",
                "readiness_projection": "project_readiness_projection_v1|none",
                "room_context": "project_round_focus_room_context_v1",
            },
            "additional_properties": False,
        },
        "output_schema": {
            "version": "project_round_context_output_v1",
            "type": "object",
            "required": [
                "version",
                "state",
                "counts",
                "focus_items",
                "suggested_objective",
                "provider_calls_performed",
                "market_reads_performed",
                "adapter_business_writes_performed",
                "host_lineage_write_required",
                "ranking_produced",
                "winner_claim",
                "approval_produced",
                "member_assignment_produced",
                "workflow_mutation_performed",
                "user_final_decision_required",
                "can_replace_user_decision",
                "arbitrary_code_loading_allowed",
            ],
            "fields": {
                "version": "project_round_focus_projection_v1",
                "state": "bootstrap|ready|gaps_present|blocked",
                "counts": "project_round_focus_counts_v1",
                "focus_items": "array<project_round_focus_item_v1>",
                "suggested_objective": "string",
                "provider_calls_performed": "integer:0",
                "market_reads_performed": "integer:0",
                "adapter_business_writes_performed": "integer:0",
                "host_lineage_write_required": "boolean:true",
                "ranking_produced": "boolean:false",
                "winner_claim": "boolean:false",
                "approval_produced": "boolean:false",
                "member_assignment_produced": "boolean:false",
                "workflow_mutation_performed": "boolean:false",
                "user_final_decision_required": "boolean:true",
                "can_replace_user_decision": "boolean:false",
                "arbitrary_code_loading_allowed": "boolean:false",
            },
            "additional_properties": False,
        },
        "read_surfaces": [
            "artifact.projection.sealed",
            "room.round_focus.safe_context",
        ],
        "local_write_surfaces": [],
        "provider_call_budget": 0,
        "market_read_budget": 0,
        "business_write_budget": 0,
        "external_write_allowed": False,
        "failure_policy": "fail_closed",
        **FIXED_PLUGIN_SAFETY,
    },
}


class PluginRegistryError(ValueError):
    """A versioned built-in plugin contract is invalid or incompatible."""


DOMAIN_ADAPTER_CONTRACTS: dict[str, dict[str, Any]] = {
    "football_research": {
        "contract_version": DOMAIN_ADAPTER_CONTRACT_VERSION_V2,
        "adapter_id": "football_research",
        "adapter_version": "1.0.0",
        "pack_ids": ["football_research_readonly"],
        "activation_capabilities": [
            "research.football.match_context.readonly",
            "research.football.evidence_classification",
        ],
        "ports": [{
            "port_id": "core.football.match_context/v1",
            "port_version": "1.0.0",
        }],
        "external_write_allowed": False,
        "provider_call_budget": 0,
        "market_read_budget": 0,
        "business_write_budget": 0,
        "failure_policy": "fail_closed",
        **FIXED_PLUGIN_SAFETY,
    },
    "stock_research": {
        "contract_version": DOMAIN_ADAPTER_CONTRACT_VERSION_V2,
        "adapter_id": "stock_research",
        "adapter_version": "1.0.0",
        "pack_ids": ["stock_research_readonly"],
        "activation_capabilities": [
            "research.stock.readonly_context",
            "research.stock.evidence_classification",
        ],
        "ports": [{
            "port_id": "core.market.readonly_context/v1",
            "port_version": "1.0.0",
        }],
        "external_write_allowed": False,
        "provider_call_budget": 0,
        "market_read_budget": 0,
        "business_write_budget": 0,
        "failure_policy": "fail_closed",
        **FIXED_PLUGIN_SAFETY,
    },
    "storage_research": {
        "contract_version": DOMAIN_ADAPTER_CONTRACT_VERSION,
        "adapter_id": "storage_research",
        "adapter_version": "1.0.0",
        "pack_ids": ["storage_research_readonly"],
        "activation_capabilities": [
            "market.storage.readonly",
            "analytics.storage",
            "simulation.observations",
            "simulation.paper_portfolio",
            "decision.observation_proposals",
        ],
        "read_surfaces": [
            "room.snapshot",
            "round.evidence_manifest",
            "market.storage.readonly",
        ],
        "local_write_surfaces": ["observations.append_proposal"],
        "external_write_allowed": False,
        "provider_call_budget": 0,
        "market_data_policy": {
            "mode": "readonly_on_demand",
            "maximum_snapshot_reads_per_round": 1,
        },
        "failure_policy": "fail_closed",
        **FIXED_PLUGIN_SAFETY,
    },
    "project_readiness": {
        "contract_version": DOMAIN_ADAPTER_CONTRACT_VERSION_V2,
        "adapter_id": "project_readiness",
        "adapter_version": "1.0.0",
        "pack_ids": ["project_readiness_review"],
        "activation_capabilities": ["research.project.readiness_review"],
        "ports": [{
            "port_id": "core.artifact.projection/v1",
            "port_version": "1.0.0",
        }],
        "external_write_allowed": False,
        "provider_call_budget": 0,
        "market_read_budget": 0,
        "business_write_budget": 0,
        "failure_policy": "fail_closed",
        **FIXED_PLUGIN_SAFETY,
    },
    "project_round_focus": {
        "contract_version": DOMAIN_ADAPTER_CONTRACT_VERSION_V2,
        "adapter_id": "project_round_focus",
        "adapter_version": "1.0.0",
        "pack_ids": ["project_round_focus"],
        "activation_capabilities": ["research.project.round_focus"],
        "ports": [{
            "port_id": "core.round.context/v1",
            "port_version": "1.0.0",
        }],
        "external_write_allowed": False,
        "provider_call_budget": 0,
        "market_read_budget": 0,
        "business_write_budget": 0,
        "failure_policy": "fail_closed",
        **FIXED_PLUGIN_SAFETY,
    },
}


UI_CONTRIBUTION_CONTRACTS: dict[str, dict[str, Any]] = {
    "football_research.room_inspector/v1": {
        "contract_version": UI_CONTRIBUTION_CONTRACT_VERSION_V2,
        "contribution_id": "football_research.room_inspector/v1",
        "contribution_version": "1.0.0",
        "pack_id": "football_research_readonly",
        "slot_id": "core.room.inspector/v1",
        "component_key": "football_research_inspector",
        "label": "足球赛事只读证据封印",
        "mode": "host_owned_component",
        "cardinality": "multiple",
        "order": 270,
        "visibility_capabilities": [
            "research.football.match_context.readonly",
        ],
        "allowed_actions": ["football_research.inspect"],
        "source_port": {
            "owner_pack_id": "football_research_readonly",
            "port_id": "core.football.match_context/v1",
            "requirement": "required",
            "cardinality": "one",
        },
        "view_model": {
            "schema_version": "football_research_view_model_v1",
            "schema_sha256": canonical_sha256(
                HOST_UI_VIEW_MODEL_SCHEMAS["football_research_view_model_v1"]
            ),
        },
        **FIXED_PLUGIN_SAFETY,
    },
    "stock_research.room_inspector/v1": {
        "contract_version": UI_CONTRIBUTION_CONTRACT_VERSION_V2,
        "contribution_id": "stock_research.room_inspector/v1",
        "contribution_version": "1.0.0",
        "pack_id": "stock_research_readonly",
        "slot_id": "core.room.inspector/v1",
        "component_key": "stock_research_inspector",
        "label": "通用股票只读证据封印",
        "mode": "host_owned_component",
        "cardinality": "multiple",
        "order": 280,
        "visibility_capabilities": ["research.stock.readonly_context"],
        "allowed_actions": ["stock_research.inspect"],
        "source_port": {
            "owner_pack_id": "stock_research_readonly",
            "port_id": "core.market.readonly_context/v1",
            "requirement": "required",
            "cardinality": "one",
        },
        "view_model": {
            "schema_version": "stock_research_view_model_v1",
            "schema_sha256": canonical_sha256(
                HOST_UI_VIEW_MODEL_SCHEMAS["stock_research_view_model_v1"]
            ),
        },
        **FIXED_PLUGIN_SAFETY,
    },
    "core.capability_pack_settings/v1": {
        "contract_version": UI_CONTRIBUTION_CONTRACT_VERSION,
        "contribution_id": "core.capability_pack_settings/v1",
        "contribution_version": "1.0.0",
        "pack_id": "structured_turn_contract_v1",
        "slot_id": "core.room.settings/v1",
        "component_key": "room_capability_pack_settings",
        "label": "房间能力包设置",
        "mode": "host_owned_component",
        "cardinality": "singleton",
        "order": 100,
        "visibility_capabilities": [],
        "allowed_actions": ["room.settings.update_capability_pack_ids"],
        **FIXED_PLUGIN_SAFETY,
    },
    "project_research.artifact_workspace/v1": {
        "contract_version": UI_CONTRIBUTION_CONTRACT_VERSION,
        "contribution_id": "project_research.artifact_workspace/v1",
        "contribution_version": "1.0.0",
        "pack_id": "structured_project_research",
        "slot_id": "core.artifact.workspace/v1",
        "component_key": "project_research_workspace",
        "label": "项目证据与方案工作区",
        "mode": "host_owned_component",
        "cardinality": "multiple",
        "order": 200,
        "visibility_capabilities": ["research.project.evidence_map"],
        "allowed_actions": ["artifact.project_research.edit"],
        **FIXED_PLUGIN_SAFETY,
    },
    "storage_research.room_inspector/v1": {
        "contract_version": UI_CONTRIBUTION_CONTRACT_VERSION,
        "contribution_id": "storage_research.room_inspector/v1",
        "contribution_version": "1.1.0",
        "pack_id": "storage_research_readonly",
        "slot_id": "core.room.inspector/v1",
        "component_key": "storage_research_inspector",
        "label": "存储产业只读研究状态",
        "mode": "host_owned_component",
        "cardinality": "multiple",
        "order": 200,
        "visibility_capabilities": ["market.storage.readonly"],
        "allowed_actions": [
            "storage.sample_acceptance.review",
            "decision_lineage.manage",
            "paper_portfolio.manage",
            "paper_portfolio.run_walk_forward",
            "candidate_comparison.preview",
            "observation.manage",
            "market.storage.refresh_readonly",
            "market.storage.freeze_official_evidence",
            "material.official_supplement.create",
        ],
        **FIXED_PLUGIN_SAFETY,
    },
    "storage_research.artifact_workspace/v1": {
        "contract_version": UI_CONTRIBUTION_CONTRACT_VERSION,
        "contribution_id": "storage_research.artifact_workspace/v1",
        "contribution_version": "1.0.0",
        "pack_id": "storage_research_readonly",
        "slot_id": "core.artifact.workspace/v1",
        "component_key": "storage_research_artifact_workspace",
        "label": "存储研究与历史模拟工作区",
        "mode": "host_owned_component",
        "cardinality": "multiple",
        "order": 300,
        "visibility_capabilities": ["simulation.paper_portfolio"],
        "allowed_actions": [
            "artifact.storage_research.edit",
            "candidate_experiment.run_historical",
        ],
        **FIXED_PLUGIN_SAFETY,
    },
    "project_readiness.artifact_workspace/v1": {
        "contract_version": UI_CONTRIBUTION_CONTRACT_VERSION_V2,
        "contribution_id": "project_readiness.artifact_workspace/v1",
        "contribution_version": "1.0.0",
        "pack_id": "project_readiness_review",
        "slot_id": "core.artifact.workspace/v1",
        "component_key": "project_readiness_review",
        "label": "项目就绪度只读复核",
        "mode": "host_owned_component",
        "cardinality": "multiple",
        "order": 250,
        "visibility_capabilities": ["research.project.readiness_review"],
        "allowed_actions": ["project_readiness.inspect"],
        "source_port": {
            "owner_pack_id": "project_readiness_review",
            "port_id": "core.artifact.projection/v1",
            "requirement": "required",
            "cardinality": "one",
        },
        "view_model": {
            "schema_version": "project_readiness_view_model_v1",
            "schema_sha256": canonical_sha256(
                HOST_UI_VIEW_MODEL_SCHEMAS["project_readiness_view_model_v1"]
            ),
        },
        **FIXED_PLUGIN_SAFETY,
    },
    "project_round_focus.room_inspector/v1": {
        "contract_version": UI_CONTRIBUTION_CONTRACT_VERSION_V2,
        "contribution_id": "project_round_focus.room_inspector/v1",
        "contribution_version": "1.0.0",
        "pack_id": "project_round_focus",
        "slot_id": "core.room.inspector/v1",
        "component_key": "project_round_focus",
        "label": "Project next-round focus",
        "mode": "host_owned_component",
        "cardinality": "multiple",
        "order": 260,
        "visibility_capabilities": ["research.project.round_focus"],
        "allowed_actions": ["project_round_focus.inspect"],
        "source_port": {
            "owner_pack_id": "project_round_focus",
            "port_id": "core.round.context/v1",
            "requirement": "required",
            "cardinality": "one",
        },
        "view_model": {
            "schema_version": "project_round_focus_view_model_v1",
            "schema_sha256": canonical_sha256(
                HOST_UI_VIEW_MODEL_SCHEMAS["project_round_focus_view_model_v1"]
            ),
        },
        **FIXED_PLUGIN_SAFETY,
    },
}


def _sealed(value: dict[str, Any], field: str) -> dict[str, Any]:
    payload = deepcopy(value)
    payload[field] = canonical_sha256(payload)
    return payload


def _require_exact_keys(
    value: Any,
    *,
    required: set[str],
    optional: set[str] | None = None,
    identity: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PluginRegistryError(f"{identity} must be an object.")
    keys = set(value)
    allowed = required | set(optional or set())
    if not required.issubset(keys) or not keys.issubset(allowed):
        raise PluginRegistryError(f"{identity} has an invalid closed shape.")
    return value


def _require_nonempty_string(value: Any, *, field: str, maximum: int = 500) -> str:
    if not isinstance(value, str):
        raise PluginRegistryError(f"{field} must be a string.")
    clean = value.strip()
    if not clean or len(clean) > maximum:
        raise PluginRegistryError(f"{field} is invalid.")
    return clean


_PACK_MANIFEST_COMMON_KEYS = {
    "id",
    "manifest_version",
    "pack_version",
    "core_protocol_range",
    "dependencies",
    "domain_adapter_ids",
    "ui_contribution_ids",
    "name",
    "category",
    "description",
    "mode_label",
    "capabilities",
    "discussion_protocol",
    *FIXED_PLUGIN_SAFETY.keys(),
}
_DOMAIN_ADAPTER_V1_KEYS = {
    "contract_version",
    "adapter_id",
    "adapter_version",
    "pack_ids",
    "activation_capabilities",
    "read_surfaces",
    "local_write_surfaces",
    "external_write_allowed",
    "provider_call_budget",
    "market_data_policy",
    "failure_policy",
    *FIXED_PLUGIN_SAFETY.keys(),
}
_DOMAIN_ADAPTER_V2_KEYS = {
    "contract_version",
    "adapter_id",
    "adapter_version",
    "pack_ids",
    "activation_capabilities",
    "ports",
    "external_write_allowed",
    "provider_call_budget",
    "market_read_budget",
    "business_write_budget",
    "failure_policy",
    *FIXED_PLUGIN_SAFETY.keys(),
}
_DOMAIN_ADAPTER_PORT_KEYS = {
    "contract_version",
    "port_id",
    "port_version",
    "handler_method",
    "cardinality",
    "input_schema",
    "output_schema",
    "read_surfaces",
    "local_write_surfaces",
    "provider_call_budget",
    "market_read_budget",
    "business_write_budget",
    "external_write_allowed",
    "failure_policy",
    *FIXED_PLUGIN_SAFETY.keys(),
}
_UI_CONTRIBUTION_COMMON_KEYS = {
    "contract_version",
    "contribution_id",
    "contribution_version",
    "pack_id",
    "slot_id",
    "component_key",
    "label",
    "mode",
    "cardinality",
    "order",
    "visibility_capabilities",
    "allowed_actions",
    *FIXED_PLUGIN_SAFETY.keys(),
}
_SNAPSHOT_PACK_V1_KEYS = {
    "id",
    "name",
    "pack_version",
    "manifest_sha256",
    "system_managed",
    "domain_adapter_ids",
    "ui_contribution_ids",
}
_SNAPSHOT_ADAPTER_V1_KEYS = {
    "adapter_id",
    "adapter_version",
    "contract_sha256",
    "status",
}
_SNAPSHOT_UI_V1_KEYS = {
    "contribution_id",
    "contribution_version",
    "contract_sha256",
    "slot_id",
    "component_key",
    "label",
    "order",
    "status",
}
_SNAPSHOT_RESOLVED_PORT_KEYS = {
    "adapter_id",
    "adapter_version",
    "adapter_contract_sha256",
    "port_id",
    "port_version",
    "port_contract_sha256",
    "handler_method",
    "input_schema_version",
    "input_schema_sha256",
    "output_schema_version",
    "output_schema_sha256",
    "provider_call_budget",
    "market_read_budget",
    "business_write_budget",
    "failure_policy",
}
_SNAPSHOT_ADAPTER_PORT_KEYS = {
    "port_id",
    "port_version",
    "contract_sha256",
    "handler_method",
    "cardinality",
    "input_schema_version",
    "input_schema_sha256",
    "output_schema_version",
    "output_schema_sha256",
    "read_surfaces",
    "local_write_surfaces",
    "provider_call_budget",
    "market_read_budget",
    "business_write_budget",
    "external_write_allowed",
    "failure_policy",
    *FIXED_PLUGIN_SAFETY.keys(),
}


def _semver(value: Any, *, field: str) -> str:
    clean = str(value or "").strip()
    if not SEMVER_PATTERN.fullmatch(clean):
        raise PluginRegistryError(f"{field} 必须是完整语义版本。")
    return clean


def _semver_tuple(value: Any, *, field: str) -> tuple[int, int, int]:
    clean = _semver(value, field=field)
    return tuple(int(part) for part in clean.split("."))  # type: ignore[return-value]


def _core_range_supports_current(value: Any, *, field: str) -> str:
    clean = str(value or "").strip()
    if not clean or "*" in clean:
        raise PluginRegistryError(f"{field} 必须声明有限内核兼容范围。")
    current = _semver_tuple(ROOM_KERNEL_VERSION, field="room_kernel_version")
    comparators = [item for item in clean.split() if item]
    if not comparators:
        raise PluginRegistryError(f"{field} 不能为空。")
    for comparator in comparators:
        match = re.fullmatch(r"(>=|<=|>|<|==|=)([0-9]+\.[0-9]+\.[0-9]+)", comparator)
        if not match:
            raise PluginRegistryError(f"{field} 仅支持显式语义版本比较。")
        operator, raw_version = match.groups()
        target = _semver_tuple(raw_version, field=field)
        compatible = {
            ">=": current >= target,
            "<=": current <= target,
            ">": current > target,
            "<": current < target,
            "==": current == target,
            "=": current == target,
        }[operator]
        if not compatible:
            raise PluginRegistryError(
                f"{field} 与当前房间内核 {ROOM_KERNEL_VERSION} 不兼容。"
            )
    return clean


def _version_range_supports(
    version: Any,
    value: Any,
    *,
    field: str,
) -> str:
    clean, parsed = _bounded_version_range(value, field=field)
    current = _semver_tuple(version, field=f"{field}.resolved_version")
    for operator, raw_version in parsed:
        target = _semver_tuple(raw_version, field=field)
        compatible = {
            ">=": current >= target,
            "<=": current <= target,
            ">": current > target,
            "<": current < target,
            "==": current == target,
            "=": current == target,
        }[operator]
        if not compatible:
            raise PluginRegistryError(
                f"{field} 不接受已解析版本 {'.'.join(str(item) for item in current)}。"
            )
    return clean


def _bounded_version_range(
    value: Any,
    *,
    field: str,
) -> tuple[str, list[tuple[str, str]]]:
    clean = str(value or "").strip()
    if not clean or "*" in clean:
        raise PluginRegistryError(f"{field} 必须声明有限版本范围。")
    comparators = [item for item in clean.split() if item]
    parsed: list[tuple[str, str]] = []
    has_lower_bound = False
    has_upper_bound = False
    has_exact_bound = False
    for comparator in comparators:
        match = re.fullmatch(
            r"(>=|<=|>|<|==|=)([0-9]+\.[0-9]+\.[0-9]+)",
            comparator,
        )
        if not match:
            raise PluginRegistryError(f"{field} 仅支持显式语义版本比较。")
        operator, raw_version = match.groups()
        has_lower_bound = has_lower_bound or operator in {">", ">="}
        has_upper_bound = has_upper_bound or operator in {"<", "<="}
        has_exact_bound = has_exact_bound or operator in {"=", "=="}
        parsed.append((operator, raw_version))
    if not has_exact_bound and not (has_lower_bound and has_upper_bound):
        raise PluginRegistryError(f"{field} 必须同时声明上下界或精确版本。")
    return clean, parsed


def _clean_frozen_pack_ids(value: Any) -> list[str]:
    """Validate sealed historical identities without consulting today's catalog."""

    if not isinstance(value, list) or len(value) > 12:
        raise PluginRegistryError("冻结能力包选择必须是最多 12 项的字符串数组。")
    clean = [str(item or "").strip().lower() for item in value]
    if (
        any(not FROZEN_PACK_ID_PATTERN.fullmatch(item) for item in clean)
        or len(clean) != len(set(clean))
    ):
        raise PluginRegistryError("冻结能力包选择包含无效或重复身份。")
    return clean


def _string_list(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list):
        raise PluginRegistryError(f"{field} 必须是字符串数组。")
    clean = [str(item or "").strip() for item in value]
    if any(not item for item in clean) or len(clean) != len(set(clean)):
        raise PluginRegistryError(f"{field} 包含空值或重复项。")
    return clean


def _validate_safety(value: dict[str, Any], *, identity: str) -> None:
    for field, expected in FIXED_PLUGIN_SAFETY.items():
        actual = value.get(field)
        if (
            (type(expected) is bool and actual is not expected)
            or (type(expected) is not bool and (type(actual) is not type(expected) or actual != expected))
        ):
            raise PluginRegistryError(f"插件合同 {identity} 违反安全字段 {field}。")


def _validate_port_schema(value: Any, *, identity: str) -> dict[str, Any]:
    schema = _require_exact_keys(
        value,
        required={"version", "type", "required", "fields", "additional_properties"},
        identity=identity,
    )
    _require_nonempty_string(
        schema.get("version"),
        field=f"{identity}.version",
        maximum=120,
    )
    if schema.get("type") != "object" or schema.get("additional_properties") is not False:
        raise PluginRegistryError(f"{identity} must be a closed object schema.")
    required = _string_list(schema.get("required"), field=f"{identity}.required")
    fields = schema.get("fields")
    if (
        not isinstance(fields, dict)
        or set(fields) != set(required)
        or len(fields) > 100
        or any(
            not isinstance(field_name, str)
            or not field_name
            or len(field_name) > 100
            or not isinstance(field_type, str)
            or not field_type
            or len(field_type) > 160
            for field_name, field_type in fields.items()
        )
    ):
        raise PluginRegistryError(f"{identity}.fields is invalid.")
    return schema


def _domain_adapter_port_catalog() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for port_id, raw in DOMAIN_ADAPTER_PORT_CONTRACTS.items():
        contract = deepcopy(raw)
        _require_exact_keys(
            contract,
            required=_DOMAIN_ADAPTER_PORT_KEYS,
            identity=f"domain adapter port {port_id}",
        )
        if port_id not in HOST_DOMAIN_ADAPTER_PORT_IDS:
            raise PluginRegistryError(f"领域适配器端口 {port_id} 不属于宿主白名单。")
        if contract.get("contract_version") != DOMAIN_ADAPTER_PORT_CONTRACT_VERSION:
            raise PluginRegistryError(f"领域适配器端口 {port_id} 合同版本不受支持。")
        if str(contract.get("port_id") or "") != port_id:
            raise PluginRegistryError(f"领域适配器端口 {port_id} 身份不一致。")
        _semver(contract.get("port_version"), field=f"{port_id}.port_version")
        handler_method = str(contract.get("handler_method") or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", handler_method):
            raise PluginRegistryError(f"领域适配器端口 {port_id} handler 无效。")
        if HOST_DOMAIN_ADAPTER_PORT_HANDLERS.get(port_id) != handler_method:
            raise PluginRegistryError(f"领域适配器端口 {port_id} handler 不受宿主支持。")
        if contract.get("cardinality") not in {"one", "multiple"}:
            raise PluginRegistryError(f"领域适配器端口 {port_id} cardinality 无效。")
        for schema_field in ("input_schema", "output_schema"):
            schema = _validate_port_schema(
                contract.get(schema_field),
                identity=f"{port_id}.{schema_field}",
            )
            contract[f"{schema_field}_sha256"] = canonical_sha256(schema)
        _string_list(contract.get("read_surfaces"), field=f"{port_id}.read_surfaces")
        _string_list(
            contract.get("local_write_surfaces"),
            field=f"{port_id}.local_write_surfaces",
        )
        for budget_field in (
            "provider_call_budget",
            "market_read_budget",
            "business_write_budget",
        ):
            budget = contract.get(budget_field)
            if not isinstance(budget, int) or isinstance(budget, bool) or budget < 0:
                raise PluginRegistryError(
                    f"领域适配器端口 {port_id} {budget_field} 无效。"
                )
        if contract.get("external_write_allowed") is not False:
            raise PluginRegistryError(f"领域适配器端口 {port_id} 禁止外部写入。")
        if contract.get("failure_policy") != "fail_closed":
            raise PluginRegistryError(f"领域适配器端口 {port_id} 必须失败关闭。")
        _validate_safety(contract, identity=port_id)
        rows.append(_sealed(contract, "contract_sha256"))
    return rows


def _clean_port_requirements(value: Any, *, field: str) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PluginRegistryError(f"{field} 必须是对象数组。")
    clean: list[dict[str, str]] = []
    identities: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, dict) or set(raw) != {
            "port_id",
            "requirement",
            "cardinality",
            "version_range",
        }:
            raise PluginRegistryError(f"{field}[{index}] 结构无效。")
        port_id = str(raw.get("port_id") or "").strip()
        requirement = str(raw.get("requirement") or "").strip()
        cardinality = str(raw.get("cardinality") or "").strip()
        version_range = str(raw.get("version_range") or "").strip()
        if port_id not in HOST_DOMAIN_ADAPTER_PORT_IDS or port_id in identities:
            raise PluginRegistryError(f"{field}[{index}].port_id 无效或重复。")
        if requirement not in {"required", "optional"}:
            raise PluginRegistryError(f"{field}[{index}].requirement 无效。")
        if cardinality not in {"one", "multiple"}:
            raise PluginRegistryError(f"{field}[{index}].cardinality 无效。")
        port_contract = DOMAIN_ADAPTER_PORT_CONTRACTS.get(port_id)
        if port_contract is None:
            raise PluginRegistryError(f"{field}[{index}] 引用了未知端口。")
        _version_range_supports(
            port_contract.get("port_version"),
            version_range,
            field=f"{field}[{index}].version_range",
        )
        identities.add(port_id)
        clean.append({
            "port_id": port_id,
            "requirement": requirement,
            "cardinality": cardinality,
            "version_range": version_range,
        })
    return clean


def _clean_frozen_port_requirements(
    value: Any,
    *,
    field: str,
) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise PluginRegistryError(f"{field} must be an array.")
    clean: list[dict[str, str]] = []
    identities: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, dict) or set(raw) != {
            "port_id",
            "requirement",
            "cardinality",
            "version_range",
        }:
            raise PluginRegistryError(f"{field}[{index}] has an invalid shape.")
        port_id = str(raw.get("port_id") or "").strip()
        requirement = str(raw.get("requirement") or "").strip()
        cardinality = str(raw.get("cardinality") or "").strip()
        version_range, _ = _bounded_version_range(
            raw.get("version_range"),
            field=f"{field}[{index}].version_range",
        )
        if (
            port_id not in HOST_DOMAIN_ADAPTER_PORT_IDS
            or port_id in identities
            or requirement not in {"required", "optional"}
            or cardinality not in {"one", "multiple"}
        ):
            raise PluginRegistryError(f"{field}[{index}] is invalid.")
        identities.add(port_id)
        clean.append({
            "port_id": port_id,
            "requirement": requirement,
            "cardinality": cardinality,
            "version_range": version_range,
        })
    return clean


def _pack_manifest_map() -> dict[str, dict[str, Any]]:
    manifests: dict[str, dict[str, Any]] = {}
    for raw in capability_pack_catalog():
        manifest = deepcopy(raw)
        pack_id = str(manifest.get("id") or "").strip()
        if not pack_id or pack_id in manifests:
            raise PluginRegistryError("能力包 manifest ID 无效或重复。")
        manifest_version = str(manifest.get("manifest_version") or "")
        if manifest_version not in {
            CAPABILITY_PACK_MANIFEST_VERSION_V1,
            CAPABILITY_PACK_MANIFEST_VERSION_V2,
        }:
            raise PluginRegistryError(f"能力包 {pack_id} manifest 版本不受支持。")
        if manifest_version == CAPABILITY_PACK_MANIFEST_VERSION_V1:
            _require_exact_keys(
                manifest,
                required=_PACK_MANIFEST_COMMON_KEYS | {"manifest_sha256"},
                optional={"system_managed", "scope"},
                identity=f"capability pack {pack_id}",
            )
        else:
            _require_exact_keys(
                manifest,
                required=(
                    _PACK_MANIFEST_COMMON_KEYS
                    | {"domain_adapter_port_requirements", "manifest_sha256"}
                ),
                identity=f"capability pack {pack_id}",
            )
        _semver(manifest.get("pack_version"), field=f"{pack_id}.pack_version")
        _core_range_supports_current(
            manifest.get("core_protocol_range"),
            field=f"{pack_id}.core_protocol_range",
        )
        dependencies = _string_list(
            manifest.get("dependencies"), field=f"{pack_id}.dependencies"
        )
        adapter_ids = _string_list(
            manifest.get("domain_adapter_ids"),
            field=f"{pack_id}.domain_adapter_ids",
        )
        port_requirements = _clean_port_requirements(
            manifest.get("domain_adapter_port_requirements"),
            field=f"{pack_id}.domain_adapter_port_requirements",
        )
        if manifest_version == CAPABILITY_PACK_MANIFEST_VERSION_V1 and (
            "domain_adapter_port_requirements" in manifest
        ):
            raise PluginRegistryError(f"旧能力包 {pack_id} 不得携带端口要求。")
        if manifest_version == CAPABILITY_PACK_MANIFEST_VERSION_V2 and (
            "domain_adapter_port_requirements" not in manifest
            or not port_requirements
        ):
            raise PluginRegistryError(f"v2 能力包 {pack_id} 必须声明端口要求。")
        contribution_ids = _string_list(
            manifest.get("ui_contribution_ids"),
            field=f"{pack_id}.ui_contribution_ids",
        )
        _string_list(manifest.get("capabilities"), field=f"{pack_id}.capabilities")
        for text_field in ("name", "category", "description", "mode_label"):
            _require_nonempty_string(
                manifest.get(text_field),
                field=f"{pack_id}.{text_field}",
                maximum=2000,
            )
        discussion = _require_exact_keys(
            manifest.get("discussion_protocol"),
            required={"title", "rules", "director_focus"},
            identity=f"{pack_id}.discussion_protocol",
        )
        _require_nonempty_string(
            discussion.get("title"),
            field=f"{pack_id}.discussion_protocol.title",
            maximum=500,
        )
        _string_list(
            discussion.get("rules"),
            field=f"{pack_id}.discussion_protocol.rules",
        )
        _require_nonempty_string(
            discussion.get("director_focus"),
            field=f"{pack_id}.discussion_protocol.director_focus",
            maximum=2000,
        )
        if "system_managed" in manifest and type(manifest.get("system_managed")) is not bool:
            raise PluginRegistryError(f"{pack_id}.system_managed must be a boolean.")
        if "scope" in manifest:
            _require_nonempty_string(manifest.get("scope"), field=f"{pack_id}.scope")
        if any(adapter_id not in DOMAIN_ADAPTER_CONTRACTS for adapter_id in adapter_ids):
            raise PluginRegistryError(f"能力包 {pack_id} 引用了未知领域适配器。")
        if any(
            contribution_id not in UI_CONTRIBUTION_CONTRACTS
            for contribution_id in contribution_ids
        ):
            raise PluginRegistryError(f"能力包 {pack_id} 引用了未知 UI contribution。")
        if any(dependency not in {item["id"] for item in capability_pack_catalog()} for dependency in dependencies):
            raise PluginRegistryError(f"能力包 {pack_id} 引用了未知依赖。")
        if any(
            pack_id not in DOMAIN_ADAPTER_CONTRACTS[adapter_id].get("pack_ids", [])
            for adapter_id in adapter_ids
        ):
            raise PluginRegistryError(f"能力包 {pack_id} 与领域适配器反向绑定不一致。")
        declared_port_ids = [
            str(port.get("port_id") or "")
            for adapter_id in adapter_ids
            for port in DOMAIN_ADAPTER_CONTRACTS[adapter_id].get("ports") or []
            if isinstance(port, dict)
        ]
        for requirement in port_requirements:
            match_count = sum(
                port_id == requirement["port_id"] for port_id in declared_port_ids
            )
            if requirement["requirement"] == "required" and match_count == 0:
                raise PluginRegistryError(
                    f"能力包 {pack_id} 的必需端口 {requirement['port_id']} 未解析。"
                )
            if requirement["cardinality"] == "one" and match_count > 1:
                raise PluginRegistryError(
                    f"能力包 {pack_id} 的端口 {requirement['port_id']} 基数冲突。"
                )
        if any(
            str(UI_CONTRIBUTION_CONTRACTS[contribution_id].get("pack_id") or "")
            != pack_id
            for contribution_id in contribution_ids
        ):
            raise PluginRegistryError(f"能力包 {pack_id} 与 UI contribution 反向绑定不一致。")
        _validate_safety(manifest, identity=pack_id)
        stored_sha256 = str(manifest.pop("manifest_sha256", "") or "").strip().lower()
        calculated_sha256 = canonical_sha256(manifest)
        if stored_sha256 != calculated_sha256:
            raise PluginRegistryError(f"能力包 {pack_id} manifest 哈希无效。")
        manifest["manifest_sha256"] = calculated_sha256
        manifests[pack_id] = manifest
    return manifests


def _domain_adapter_catalog() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    port_catalog = {
        (str(item["port_id"]), str(item["port_version"])): item
        for item in _domain_adapter_port_catalog()
    }
    for adapter_id, raw in DOMAIN_ADAPTER_CONTRACTS.items():
        contract = deepcopy(raw)
        contract_version = str(contract.get("contract_version") or "")
        if contract_version not in {
            DOMAIN_ADAPTER_CONTRACT_VERSION_V1,
            DOMAIN_ADAPTER_CONTRACT_VERSION_V2,
        }:
            raise PluginRegistryError(f"领域适配器 {adapter_id} 合同版本不受支持。")
        _require_exact_keys(
            contract,
            required=(
                _DOMAIN_ADAPTER_V2_KEYS
                if contract_version == DOMAIN_ADAPTER_CONTRACT_VERSION_V2
                else _DOMAIN_ADAPTER_V1_KEYS
            ),
            identity=f"domain adapter {adapter_id}",
        )
        if str(contract.get("adapter_id") or "") != adapter_id:
            raise PluginRegistryError(f"领域适配器 {adapter_id} 身份不一致。")
        _semver(contract.get("adapter_version"), field=f"{adapter_id}.adapter_version")
        _string_list(contract.get("activation_capabilities"), field=f"{adapter_id}.activation_capabilities")
        _string_list(contract.get("pack_ids"), field=f"{adapter_id}.pack_ids")
        if contract_version == DOMAIN_ADAPTER_CONTRACT_VERSION_V2:
            raw_ports = contract.get("ports")
            if not isinstance(raw_ports, list) or not raw_ports:
                raise PluginRegistryError(f"领域适配器 {adapter_id} 必须声明至少一个端口。")
            resolved_ports: list[dict[str, str]] = []
            seen_ports: set[str] = set()
            for index, raw_port in enumerate(raw_ports):
                if not isinstance(raw_port, dict) or set(raw_port) != {
                    "port_id",
                    "port_version",
                }:
                    raise PluginRegistryError(
                        f"领域适配器 {adapter_id}.ports[{index}] 结构无效。"
                    )
                port_id = str(raw_port.get("port_id") or "").strip()
                port_version = _semver(
                    raw_port.get("port_version"),
                    field=f"{adapter_id}.ports[{index}].port_version",
                )
                if port_id not in HOST_DOMAIN_ADAPTER_PORT_IDS or port_id in seen_ports:
                    raise PluginRegistryError(
                        f"领域适配器 {adapter_id}.ports[{index}] 身份无效或重复。"
                    )
                port_contract = port_catalog.get((port_id, port_version))
                if port_contract is None:
                    raise PluginRegistryError(
                        f"领域适配器 {adapter_id}.ports[{index}] 精确合同不存在。"
                    )
                resolved_ports.append({
                    "port_id": port_id,
                    "port_version": port_version,
                    "contract_sha256": str(port_contract["contract_sha256"]),
                    "handler_method": str(port_contract["handler_method"]),
                    "cardinality": str(port_contract["cardinality"]),
                    "input_schema_version": str(
                        (port_contract.get("input_schema") or {}).get("version") or ""
                    ),
                    "input_schema_sha256": str(
                        port_contract.get("input_schema_sha256") or ""
                    ),
                    "output_schema_version": str(
                        (port_contract.get("output_schema") or {}).get("version") or ""
                    ),
                    "output_schema_sha256": str(
                        port_contract.get("output_schema_sha256") or ""
                    ),
                    "read_surfaces": list(port_contract.get("read_surfaces") or []),
                    "local_write_surfaces": list(
                        port_contract.get("local_write_surfaces") or []
                    ),
                    "provider_call_budget": int(
                        port_contract.get("provider_call_budget") or 0
                    ),
                    "market_read_budget": int(
                        port_contract.get("market_read_budget") or 0
                    ),
                    "business_write_budget": int(
                        port_contract.get("business_write_budget") or 0
                    ),
                    "external_write_allowed": False,
                    "failure_policy": "fail_closed",
                    **deepcopy(FIXED_PLUGIN_SAFETY),
                })
                seen_ports.add(port_id)
            contract["ports"] = resolved_ports
            for budget_field in (
                "provider_call_budget",
                "market_read_budget",
                "business_write_budget",
            ):
                if type(contract.get(budget_field)) is not int or contract.get(budget_field) != 0:
                    raise PluginRegistryError(
                        f"领域适配器 {adapter_id} {budget_field} 必须为 0。"
                    )
        elif contract.get("ports") is not None:
            raise PluginRegistryError(f"旧领域适配器 {adapter_id} 不得伪造端口声明。")
        if contract_version == DOMAIN_ADAPTER_CONTRACT_VERSION_V1:
            _string_list(contract.get("read_surfaces"), field=f"{adapter_id}.read_surfaces")
            _string_list(
                contract.get("local_write_surfaces"),
                field=f"{adapter_id}.local_write_surfaces",
            )
            market_policy = _require_exact_keys(
                contract.get("market_data_policy"),
                required={"mode", "maximum_snapshot_reads_per_round"},
                identity=f"{adapter_id}.market_data_policy",
            )
            if market_policy.get("mode") != "readonly_on_demand" or type(
                market_policy.get("maximum_snapshot_reads_per_round")
            ) is not int or int(market_policy["maximum_snapshot_reads_per_round"]) < 0:
                raise PluginRegistryError(f"{adapter_id}.market_data_policy is invalid.")
        if contract.get("external_write_allowed") is not False:
            raise PluginRegistryError(f"领域适配器 {adapter_id} 禁止外部写入。")
        if type(contract.get("provider_call_budget")) is not int or contract.get("provider_call_budget") != 0:
            raise PluginRegistryError(f"领域适配器 {adapter_id} Provider 预算必须为 0。")
        _validate_safety(contract, identity=adapter_id)
        rows.append(_sealed(contract, "contract_sha256"))
    return rows


def _ui_view_model_schema_catalog() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for schema_version, raw in HOST_UI_VIEW_MODEL_SCHEMAS.items():
        schema = deepcopy(raw)
        _require_exact_keys(
            schema,
            required={
                "schema_version",
                "component_key",
                "type",
                "required",
                "fields",
                "additional_properties",
            },
            identity=f"UI view model schema {schema_version}",
        )
        if schema.get("schema_version") != schema_version:
            raise PluginRegistryError(f"UI view model schema {schema_version} identity drifted.")
        _require_nonempty_string(
            schema.get("component_key"),
            field=f"{schema_version}.component_key",
            maximum=120,
        )
        if schema.get("type") != "object" or schema.get("additional_properties") is not False:
            raise PluginRegistryError(f"UI view model schema {schema_version} is not closed.")
        required = _string_list(
            schema.get("required"),
            field=f"{schema_version}.required",
        )
        fields = schema.get("fields")
        if (
            not isinstance(fields, dict)
            or set(fields) != set(required)
            or any(
                not isinstance(field_type, str) or not field_type
                for field_type in fields.values()
            )
        ):
            raise PluginRegistryError(f"UI view model schema {schema_version} fields are invalid.")
        rows.append(_sealed(schema, "schema_sha256"))
    return rows


def _ui_contribution_catalog() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_slot_singletons: set[str] = set()
    view_models = {
        str(row["schema_version"]): row
        for row in _ui_view_model_schema_catalog()
    }
    for contribution_id, raw in UI_CONTRIBUTION_CONTRACTS.items():
        contract = deepcopy(raw)
        contract_version = str(contract.get("contract_version") or "")
        if contract_version not in {
            UI_CONTRIBUTION_CONTRACT_VERSION_V1,
            UI_CONTRIBUTION_CONTRACT_VERSION_V2,
        }:
            raise PluginRegistryError(f"UI contribution {contribution_id} 合同版本不受支持。")
        _require_exact_keys(
            contract,
            required=(
                _UI_CONTRIBUTION_COMMON_KEYS
                | ({"source_port", "view_model"} if contract_version == UI_CONTRIBUTION_CONTRACT_VERSION_V2 else set())
            ),
            identity=f"UI contribution {contribution_id}",
        )
        if str(contract.get("contribution_id") or "") != contribution_id:
            raise PluginRegistryError(f"UI contribution {contribution_id} 身份不一致。")
        _semver(
            contract.get("contribution_version"),
            field=f"{contribution_id}.contribution_version",
        )
        slot_id = str(contract.get("slot_id") or "").strip()
        if not slot_id.startswith("core.") or not slot_id.endswith("/v1"):
            raise PluginRegistryError(f"UI contribution {contribution_id} slot 无效。")
        if contract.get("mode") != "host_owned_component":
            raise PluginRegistryError(f"UI contribution {contribution_id} 不能加载任意组件。")
        if contract.get("cardinality") == "singleton":
            if slot_id in seen_slot_singletons:
                raise PluginRegistryError(f"UI singleton slot 冲突：{slot_id}。")
            seen_slot_singletons.add(slot_id)
        _string_list(
            contract.get("visibility_capabilities"),
            field=f"{contribution_id}.visibility_capabilities",
        )
        _string_list(
            contract.get("allowed_actions"),
            field=f"{contribution_id}.allowed_actions",
        )
        if type(contract.get("order")) is not int:
            raise PluginRegistryError(f"UI contribution {contribution_id} order is invalid.")
        if contract.get("cardinality") not in {"singleton", "multiple"}:
            raise PluginRegistryError(f"UI contribution {contribution_id} cardinality is invalid.")
        if contract_version == UI_CONTRIBUTION_CONTRACT_VERSION_V2:
            source_port = _require_exact_keys(
                contract.get("source_port"),
                required={"owner_pack_id", "port_id", "requirement", "cardinality"},
                identity=f"{contribution_id}.source_port",
            )
            if (
                source_port.get("owner_pack_id") != contract.get("pack_id")
                or source_port.get("port_id") not in HOST_DOMAIN_ADAPTER_PORT_IDS
                or source_port.get("requirement") != "required"
                or source_port.get("cardinality") != "one"
            ):
                raise PluginRegistryError(f"UI contribution {contribution_id} source port is invalid.")
            view_model = _require_exact_keys(
                contract.get("view_model"),
                required={"schema_version", "schema_sha256"},
                identity=f"{contribution_id}.view_model",
            )
            trusted_schema = view_models.get(str(view_model.get("schema_version") or ""))
            if (
                trusted_schema is None
                or str(view_model.get("schema_sha256") or "")
                != str(trusted_schema.get("schema_sha256") or "")
                or str(trusted_schema.get("component_key") or "")
                != str(contract.get("component_key") or "")
            ):
                raise PluginRegistryError(f"UI contribution {contribution_id} view model is invalid.")
        elif "source_port" in contract or "view_model" in contract:
            raise PluginRegistryError(f"Legacy UI contribution {contribution_id} cannot declare ports.")
        _validate_safety(contract, identity=contribution_id)
        rows.append(_sealed(contract, "contract_sha256"))
    return rows


def plugin_registry_catalog() -> dict[str, Any]:
    payload = {
        "version": PLUGIN_REGISTRY_CATALOG_VERSION,
        "room_kernel_version": ROOM_KERNEL_VERSION,
        "capability_packs": list(_pack_manifest_map().values()),
        "domain_adapters": _domain_adapter_catalog(),
        "domain_adapter_ports": _domain_adapter_port_catalog(),
        "ui_contributions": _ui_contribution_catalog(),
        "ui_view_model_schemas": _ui_view_model_schema_catalog(),
        "safety": deepcopy(FIXED_PLUGIN_SAFETY),
    }
    payload["catalog_sha256"] = canonical_sha256(payload)
    return payload


_V3_CONTRACT_KIND_FIELDS: dict[str, tuple[str, str, str, bool]] = {
    "capability_pack": ("id", "pack_version", "manifest_sha256", True),
    "domain_adapter": (
        "adapter_id",
        "adapter_version",
        "contract_sha256",
        True,
    ),
    "domain_adapter_port": (
        "port_id",
        "port_version",
        "contract_sha256",
        True,
    ),
    "ui_contribution": (
        "contribution_id",
        "contribution_version",
        "contract_sha256",
        True,
    ),
    # A view-model schema's component key is its stable host renderer identity;
    # schema_version is the exact immutable contract version.
    "ui_view_model_schema": (
        "component_key",
        "schema_version",
        "schema_sha256",
        False,
    ),
}


def _v3_current_contracts() -> dict[str, list[dict[str, Any]]]:
    current = plugin_registry_catalog()
    return {
        "capability_pack": deepcopy(current["capability_packs"]),
        "domain_adapter": deepcopy(current["domain_adapters"]),
        "domain_adapter_port": deepcopy(current["domain_adapter_ports"]),
        "ui_contribution": deepcopy(current["ui_contributions"]),
        "ui_view_model_schema": deepcopy(current["ui_view_model_schemas"]),
    }


def _v3_exact_native_text(value: Any, *, field: str, maximum: int = 240) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > maximum:
        raise PluginRegistryError(f"{field} must be exact non-empty text.")
    return value


def _v3_validate_contract_shape(kind: str, contract: dict[str, Any]) -> None:
    if kind == "capability_pack":
        manifest_version = str(contract.get("manifest_version") or "")
        required = _PACK_MANIFEST_COMMON_KEYS | {"manifest_sha256"}
        if manifest_version == CAPABILITY_PACK_MANIFEST_VERSION_V2:
            required |= {"domain_adapter_port_requirements"}
        elif manifest_version != CAPABILITY_PACK_MANIFEST_VERSION_V1:
            raise PluginRegistryError("v3 capability-pack manifest version is unsupported.")
        _require_exact_keys(
            contract,
            required=required,
            optional=(
                {"system_managed", "scope"}
                if manifest_version == CAPABILITY_PACK_MANIFEST_VERSION_V1
                else None
            ),
            identity="v3 capability-pack contract",
        )
        _validate_safety(contract, identity=str(contract.get("id") or ""))
        return
    if kind == "domain_adapter":
        contract_version = str(contract.get("contract_version") or "")
        if contract_version == DOMAIN_ADAPTER_CONTRACT_VERSION_V1:
            required = _DOMAIN_ADAPTER_V1_KEYS | {"contract_sha256"}
        elif contract_version == DOMAIN_ADAPTER_CONTRACT_VERSION_V2:
            required = _DOMAIN_ADAPTER_V2_KEYS | {"contract_sha256"}
        else:
            raise PluginRegistryError("v3 domain-adapter contract version is unsupported.")
        _require_exact_keys(
            contract,
            required=required,
            identity="v3 domain-adapter contract",
        )
        _validate_safety(contract, identity=str(contract.get("adapter_id") or ""))
        return
    if kind == "domain_adapter_port":
        if contract.get("contract_version") != DOMAIN_ADAPTER_PORT_CONTRACT_VERSION:
            raise PluginRegistryError("v3 domain-adapter port version is unsupported.")
        _require_exact_keys(
            contract,
            required=(
                _DOMAIN_ADAPTER_PORT_KEYS
                | {
                    "input_schema_sha256",
                    "output_schema_sha256",
                    "contract_sha256",
                }
            ),
            identity="v3 domain-adapter port contract",
        )
        _validate_safety(contract, identity=str(contract.get("port_id") or ""))
        return
    if kind == "ui_contribution":
        contract_version = str(contract.get("contract_version") or "")
        if contract_version == UI_CONTRIBUTION_CONTRACT_VERSION_V1:
            required = _UI_CONTRIBUTION_COMMON_KEYS | {"contract_sha256"}
        elif contract_version == UI_CONTRIBUTION_CONTRACT_VERSION_V2:
            required = (
                _UI_CONTRIBUTION_COMMON_KEYS
                | {"source_port", "view_model", "contract_sha256"}
            )
        else:
            raise PluginRegistryError("v3 UI-contribution contract version is unsupported.")
        _require_exact_keys(
            contract,
            required=required,
            identity="v3 UI-contribution contract",
        )
        _validate_safety(
            contract,
            identity=str(contract.get("contribution_id") or ""),
        )
        return
    if kind == "ui_view_model_schema":
        _require_exact_keys(
            contract,
            required={
                "schema_version",
                "component_key",
                "type",
                "required",
                "fields",
                "additional_properties",
                "schema_sha256",
            },
            identity="v3 UI view-model schema",
        )
        if contract.get("type") != "object" or contract.get("additional_properties") is not False:
            raise PluginRegistryError("v3 UI view-model schema must remain closed.")
        return
    raise PluginRegistryError(f"Unknown plugin registry contract kind: {kind}.")


def _v3_contract_identity(
    kind: str,
    value: Any,
) -> tuple[str, str, str, dict[str, Any]]:
    if kind not in _V3_CONTRACT_KIND_FIELDS:
        raise PluginRegistryError(f"Unknown plugin registry contract kind: {kind}.")
    if type(value) is not dict:
        raise PluginRegistryError(f"v3 {kind} contract must be an object.")
    contract = deepcopy(value)
    stable_field, version_field, hash_field, is_semver = _V3_CONTRACT_KIND_FIELDS[kind]
    stable_id = _v3_exact_native_text(
        contract.get(stable_field),
        field=f"{kind}.{stable_field}",
    )
    exact_version = _v3_exact_native_text(
        contract.get(version_field),
        field=f"{kind}.{version_field}",
    )
    if is_semver and _semver(
        exact_version,
        field=f"{kind}.{stable_id}.{version_field}",
    ) != exact_version:
        raise PluginRegistryError(f"{kind}.{stable_id} version is not canonical.")
    contract_sha256 = _v3_exact_native_text(
        contract.get(hash_field),
        field=f"{kind}.{stable_id}.{hash_field}",
        maximum=64,
    )
    if not SHA256_PATTERN.fullmatch(contract_sha256):
        raise PluginRegistryError(f"{kind}.{stable_id} contract hash is invalid.")
    _v3_validate_contract_shape(kind, contract)
    unsigned = deepcopy(contract)
    unsigned.pop(hash_field, None)
    if canonical_sha256(unsigned) != contract_sha256:
        raise PluginRegistryError(
            f"{kind} ({stable_id}, {exact_version}) contract hash is invalid."
        )
    return stable_id, exact_version, contract_sha256, contract


def _v3_contract_entry(
    *,
    kind: str,
    stable_id: str,
    exact_version: str,
    contract_sha256: str,
    contract: dict[str, Any],
    previous_entry_sha256: str,
) -> dict[str, Any]:
    entry = {
        "exact_version": exact_version,
        "contract_sha256": contract_sha256,
        "previous_entry_sha256": previous_entry_sha256,
        "contract": deepcopy(contract),
    }
    entry["entry_sha256"] = canonical_sha256({
        "kind": kind,
        "stable_id": stable_id,
        **entry,
    })
    return entry


def _build_plugin_registry_catalog_v3(
    historical_contracts: Any,
) -> dict[str, Any]:
    if type(historical_contracts) is not dict or set(historical_contracts) != set(
        PLUGIN_REGISTRY_CONTRACT_KINDS
    ):
        raise PluginRegistryError("v3 historical contract ledger has an invalid closed shape.")
    current_by_kind = _v3_current_contracts()
    histories_payload: dict[str, list[dict[str, Any]]] = {}
    latest_payload: dict[str, list[dict[str, Any]]] = {}
    for kind in PLUGIN_REGISTRY_CONTRACT_KINDS:
        raw_history = historical_contracts.get(kind)
        if type(raw_history) is not list:
            raise PluginRegistryError(f"v3 {kind} history must be an append-only list.")
        grouped: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
        seen_identity_hashes: dict[tuple[str, str], str] = {}
        for raw_contract in [*deepcopy(raw_history), *current_by_kind[kind]]:
            stable_id, exact_version, contract_sha256, contract = _v3_contract_identity(
                kind,
                raw_contract,
            )
            identity = (stable_id, exact_version)
            previous_hash = seen_identity_hashes.get(identity)
            if previous_hash is not None:
                if previous_hash != contract_sha256:
                    raise PluginRegistryError(
                        f"v3 {kind} identity {identity} has conflicting hashes."
                    )
                raise PluginRegistryError(
                    f"v3 {kind} identity {identity} is duplicated."
                )
            seen_identity_hashes[identity] = contract_sha256
            grouped.setdefault(stable_id, []).append(
                (exact_version, contract_sha256, contract)
            )

        kind_histories: list[dict[str, Any]] = []
        kind_latest: list[dict[str, Any]] = []
        _stable_field, _version_field, _hash_field, is_semver = (
            _V3_CONTRACT_KIND_FIELDS[kind]
        )
        for stable_id in sorted(grouped):
            versions: list[dict[str, Any]] = []
            previous_entry_sha256 = ""
            previous_semver: tuple[int, int, int] | None = None
            for exact_version, contract_sha256, contract in grouped[stable_id]:
                if is_semver:
                    current_semver = _semver_tuple(
                        exact_version,
                        field=f"v3.{kind}.{stable_id}.exact_version",
                    )
                    if previous_semver is not None and current_semver <= previous_semver:
                        raise PluginRegistryError(
                            f"v3 {kind} history for {stable_id} is not append-only."
                        )
                    previous_semver = current_semver
                entry = _v3_contract_entry(
                    kind=kind,
                    stable_id=stable_id,
                    exact_version=exact_version,
                    contract_sha256=contract_sha256,
                    contract=contract,
                    previous_entry_sha256=previous_entry_sha256,
                )
                versions.append(entry)
                previous_entry_sha256 = str(entry["entry_sha256"])
            history = {
                "stable_id": stable_id,
                "versions": versions,
                "history_head_sha256": previous_entry_sha256,
            }
            kind_histories.append(history)
            latest = versions[-1]
            kind_latest.append({
                "stable_id": stable_id,
                "exact_version": latest["exact_version"],
                "contract_sha256": latest["contract_sha256"],
                "history_head_sha256": latest["entry_sha256"],
            })
        histories_payload[kind] = kind_histories
        latest_payload[kind] = kind_latest

    payload = {
        "version": PLUGIN_REGISTRY_CATALOG_VERSION_V3,
        "room_kernel_version": ROOM_KERNEL_VERSION,
        "histories": histories_payload,
        "latest_aliases": latest_payload,
        "safety": deepcopy(FIXED_PLUGIN_SAFETY),
    }
    payload["catalog_sha256"] = canonical_sha256(payload)
    return validate_plugin_registry_catalog_v3(payload)


def plugin_registry_catalog_v3() -> dict[str, Any]:
    """Return an exact-version, append-only view without changing v2 callers."""

    return _build_plugin_registry_catalog_v3(
        deepcopy(_PLUGIN_REGISTRY_HISTORICAL_CONTRACTS)
    )


def validate_plugin_registry_catalog_v3(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        raise PluginRegistryError("Plugin registry catalog v3 must be an exact object.")
    catalog = _require_exact_keys(
        value,
        required={
            "version",
            "room_kernel_version",
            "histories",
            "latest_aliases",
            "safety",
            "catalog_sha256",
        },
        identity="plugin registry catalog v3",
    )
    if catalog.get("version") != PLUGIN_REGISTRY_CATALOG_VERSION_V3:
        raise PluginRegistryError("Plugin registry catalog v3 version is unsupported.")
    if catalog.get("room_kernel_version") != ROOM_KERNEL_VERSION:
        raise PluginRegistryError("Plugin registry catalog v3 kernel version drifted.")
    histories = _require_exact_keys(
        catalog.get("histories"),
        required=set(PLUGIN_REGISTRY_CONTRACT_KINDS),
        identity="plugin registry catalog v3 histories",
    )
    latest_aliases = _require_exact_keys(
        catalog.get("latest_aliases"),
        required=set(PLUGIN_REGISTRY_CONTRACT_KINDS),
        identity="plugin registry catalog v3 latest aliases",
    )
    if catalog.get("safety") != FIXED_PLUGIN_SAFETY:
        raise PluginRegistryError("Plugin registry catalog v3 safety drifted.")

    for kind in PLUGIN_REGISTRY_CONTRACT_KINDS:
        kind_histories = histories.get(kind)
        kind_aliases = latest_aliases.get(kind)
        if type(kind_histories) is not list or type(kind_aliases) is not list:
            raise PluginRegistryError(f"Plugin registry v3 {kind} indexes are invalid.")
        seen_stable_ids: set[str] = set()
        expected_aliases: list[dict[str, Any]] = []
        previous_stable_id = ""
        for history in kind_histories:
            history = _require_exact_keys(
                history,
                required={"stable_id", "versions", "history_head_sha256"},
                identity=f"plugin registry v3 {kind} history",
            )
            stable_id = _v3_exact_native_text(
                history.get("stable_id"),
                field=f"plugin registry v3 {kind} stable_id",
            )
            if stable_id in seen_stable_ids or (
                previous_stable_id and stable_id <= previous_stable_id
            ):
                raise PluginRegistryError(f"Plugin registry v3 {kind} histories are ambiguous.")
            seen_stable_ids.add(stable_id)
            previous_stable_id = stable_id
            versions = history.get("versions")
            if type(versions) is not list or not versions:
                raise PluginRegistryError(f"Plugin registry v3 {kind} history is empty.")
            seen_versions: dict[str, str] = {}
            previous_entry_sha256 = ""
            previous_semver: tuple[int, int, int] | None = None
            is_semver = _V3_CONTRACT_KIND_FIELDS[kind][3]
            for raw_entry in versions:
                entry = _require_exact_keys(
                    raw_entry,
                    required={
                        "exact_version",
                        "contract_sha256",
                        "previous_entry_sha256",
                        "contract",
                        "entry_sha256",
                    },
                    identity=f"plugin registry v3 {kind} version entry",
                )
                exact_version = _v3_exact_native_text(
                    entry.get("exact_version"),
                    field=f"plugin registry v3 {kind} exact_version",
                )
                contract_sha256 = _v3_exact_native_text(
                    entry.get("contract_sha256"),
                    field=f"plugin registry v3 {kind} contract_sha256",
                    maximum=64,
                )
                if not SHA256_PATTERN.fullmatch(contract_sha256):
                    raise PluginRegistryError(
                        f"Plugin registry v3 {kind} contract hash is invalid."
                    )
                prior_identity_hash = seen_versions.get(exact_version)
                if prior_identity_hash is not None:
                    if prior_identity_hash != contract_sha256:
                        raise PluginRegistryError(
                            f"Plugin registry v3 {kind} exact identity has conflicting hashes."
                        )
                    raise PluginRegistryError(
                        f"Plugin registry v3 {kind} exact identity is duplicated."
                    )
                seen_versions[exact_version] = contract_sha256
                if entry.get("previous_entry_sha256") != previous_entry_sha256:
                    raise PluginRegistryError(
                        f"Plugin registry v3 {kind} append-only chain is invalid."
                    )
                resolved_id, resolved_version, resolved_hash, contract = (
                    _v3_contract_identity(kind, entry.get("contract"))
                )
                if (
                    resolved_id != stable_id
                    or resolved_version != exact_version
                    or resolved_hash != contract_sha256
                ):
                    raise PluginRegistryError(
                        f"Plugin registry v3 {kind} exact contract binding is invalid."
                    )
                if is_semver:
                    current_semver = _semver_tuple(
                        exact_version,
                        field=f"plugin registry v3 {kind} exact_version",
                    )
                    if previous_semver is not None and current_semver <= previous_semver:
                        raise PluginRegistryError(
                            f"Plugin registry v3 {kind} history is not append-only."
                        )
                    previous_semver = current_semver
                expected_entry = _v3_contract_entry(
                    kind=kind,
                    stable_id=stable_id,
                    exact_version=exact_version,
                    contract_sha256=contract_sha256,
                    contract=contract,
                    previous_entry_sha256=previous_entry_sha256,
                )
                if entry != expected_entry:
                    raise PluginRegistryError(
                        f"Plugin registry v3 {kind} entry seal is invalid."
                    )
                previous_entry_sha256 = str(entry["entry_sha256"])
            if history.get("history_head_sha256") != previous_entry_sha256:
                raise PluginRegistryError(
                    f"Plugin registry v3 {kind} history head is invalid."
                )
            latest = versions[-1]
            expected_aliases.append({
                "stable_id": stable_id,
                "exact_version": latest["exact_version"],
                "contract_sha256": latest["contract_sha256"],
                "history_head_sha256": latest["entry_sha256"],
            })
        if kind_aliases != expected_aliases:
            raise PluginRegistryError(
                f"Plugin registry v3 {kind} latest aliases are invalid."
            )

    supplied_sha256 = _v3_exact_native_text(
        catalog.get("catalog_sha256"),
        field="plugin registry catalog v3 catalog_sha256",
        maximum=64,
    )
    if not SHA256_PATTERN.fullmatch(supplied_sha256):
        raise PluginRegistryError("Plugin registry catalog v3 hash is invalid.")
    unsigned = deepcopy(catalog)
    unsigned.pop("catalog_sha256", None)
    if canonical_sha256(unsigned) != supplied_sha256:
        raise PluginRegistryError("Plugin registry catalog v3 seal is invalid.")
    return deepcopy(catalog)


def resolve_plugin_registry_contract_exact(
    kind: Any,
    stable_id: Any,
    exact_version: Any,
    *,
    catalog: Any | None = None,
) -> dict[str, Any]:
    clean_kind = _v3_exact_native_text(kind, field="contract kind", maximum=80)
    if clean_kind not in PLUGIN_REGISTRY_CONTRACT_KINDS:
        raise PluginRegistryError(f"Unknown plugin registry contract kind: {clean_kind}.")
    clean_stable_id = _v3_exact_native_text(stable_id, field="stable_id")
    clean_exact_version = _v3_exact_native_text(exact_version, field="exact_version")
    trusted = (
        plugin_registry_catalog_v3()
        if catalog is None
        else validate_plugin_registry_catalog_v3(catalog)
    )
    for history in trusted["histories"][clean_kind]:
        if history["stable_id"] != clean_stable_id:
            continue
        for entry in history["versions"]:
            if entry["exact_version"] == clean_exact_version:
                return {
                    "kind": clean_kind,
                    "stable_id": clean_stable_id,
                    **deepcopy(entry),
                }
        raise PluginRegistryError(
            f"Unknown exact plugin registry version: "
            f"({clean_kind}, {clean_stable_id}, {clean_exact_version})."
        )
    raise PluginRegistryError(
        f"Unknown plugin registry stable identity: ({clean_kind}, {clean_stable_id})."
    )


def resolve_plugin_registry_contract_latest(
    kind: Any,
    stable_id: Any,
    *,
    catalog: Any | None = None,
) -> dict[str, Any]:
    clean_kind = _v3_exact_native_text(kind, field="contract kind", maximum=80)
    if clean_kind not in PLUGIN_REGISTRY_CONTRACT_KINDS:
        raise PluginRegistryError(f"Unknown plugin registry contract kind: {clean_kind}.")
    clean_stable_id = _v3_exact_native_text(stable_id, field="stable_id")
    trusted = (
        plugin_registry_catalog_v3()
        if catalog is None
        else validate_plugin_registry_catalog_v3(catalog)
    )
    alias = next(
        (
            item
            for item in trusted["latest_aliases"][clean_kind]
            if item["stable_id"] == clean_stable_id
        ),
        None,
    )
    if alias is None:
        raise PluginRegistryError(
            f"Unknown plugin registry stable identity: ({clean_kind}, {clean_stable_id})."
        )
    resolved = resolve_plugin_registry_contract_exact(
        clean_kind,
        clean_stable_id,
        alias["exact_version"],
        catalog=trusted,
    )
    if (
        resolved["contract_sha256"] != alias["contract_sha256"]
        or resolved["entry_sha256"] != alias["history_head_sha256"]
    ):
        raise PluginRegistryError("Plugin registry latest alias binding is invalid.")
    return resolved


def _resolve_pack_ids(
    selected_pack_ids: Iterable[str],
    manifests: dict[str, dict[str, Any]],
) -> list[str]:
    active: list[str] = []
    visiting: set[str] = set()

    def add(pack_id: str) -> None:
        if pack_id in active:
            return
        if pack_id in visiting:
            raise PluginRegistryError(f"能力包依赖形成循环：{pack_id}。")
        manifest = manifests.get(pack_id)
        if manifest is None:
            raise PluginRegistryError(f"未知能力包 manifest：{pack_id}。")
        visiting.add(pack_id)
        for dependency in manifest.get("dependencies") or []:
            add(str(dependency))
        visiting.remove(pack_id)
        active.append(pack_id)

    for pack_id, manifest in manifests.items():
        if manifest.get("system_managed") is True:
            add(pack_id)
    for pack_id in selected_pack_ids:
        add(pack_id)
    return active


def build_room_plugin_registry_snapshot(
    capability_pack_ids: Iterable[str] | None,
) -> dict[str, Any]:
    selected = clean_capability_pack_ids(list(capability_pack_ids or []))
    catalog = plugin_registry_catalog()
    manifests = {
        str(item["id"]): item
        for item in catalog["capability_packs"]
    }
    active_pack_ids = _resolve_pack_ids(selected, manifests)
    adapter_catalog = {
        str(item["adapter_id"]): item
        for item in catalog["domain_adapters"]
    }
    port_catalog = {
        (str(item["port_id"]), str(item["port_version"])): item
        for item in catalog.get("domain_adapter_ports") or []
    }
    contribution_catalog = {
        str(item["contribution_id"]): item
        for item in catalog["ui_contributions"]
    }
    adapter_ids: list[str] = []
    contribution_ids: list[str] = []
    for pack_id in active_pack_ids:
        manifest = manifests[pack_id]
        for adapter_id in manifest.get("domain_adapter_ids") or []:
            if adapter_id not in adapter_ids:
                adapter_ids.append(adapter_id)
        for contribution_id in manifest.get("ui_contribution_ids") or []:
            if contribution_id not in contribution_ids:
                contribution_ids.append(contribution_id)

    uses_ports = any(
        manifests[pack_id].get("domain_adapter_port_requirements")
        for pack_id in active_pack_ids
    ) or any(
        adapter_catalog[adapter_id].get("contract_version")
        == DOMAIN_ADAPTER_CONTRACT_VERSION_V2
        for adapter_id in adapter_ids
    )

    capability_packs = [
        {
            "id": pack_id,
            "name": str(manifests[pack_id].get("name") or pack_id),
            "pack_version": manifests[pack_id]["pack_version"],
            "manifest_sha256": manifests[pack_id]["manifest_sha256"],
            "system_managed": manifests[pack_id].get("system_managed") is True,
            "domain_adapter_ids": list(manifests[pack_id].get("domain_adapter_ids") or []),
            "ui_contribution_ids": list(manifests[pack_id].get("ui_contribution_ids") or []),
        }
        for pack_id in active_pack_ids
    ]
    domain_adapters = [
        {
            "adapter_id": adapter_id,
            "adapter_version": adapter_catalog[adapter_id]["adapter_version"],
            "contract_sha256": adapter_catalog[adapter_id]["contract_sha256"],
            "status": "ready",
        }
        for adapter_id in adapter_ids
    ]
    ui_contributions = [
        {
            "contribution_id": contribution_id,
            "contribution_version": contribution_catalog[contribution_id]["contribution_version"],
            "contract_sha256": contribution_catalog[contribution_id]["contract_sha256"],
            "slot_id": contribution_catalog[contribution_id]["slot_id"],
            "component_key": contribution_catalog[contribution_id]["component_key"],
            "label": contribution_catalog[contribution_id]["label"],
            "order": int(contribution_catalog[contribution_id]["order"]),
            "status": "ready",
            **({
                "contract_version": UI_CONTRIBUTION_CONTRACT_VERSION_V2,
                "source_port": deepcopy(
                    contribution_catalog[contribution_id]["source_port"]
                ),
                "view_model": deepcopy(
                    contribution_catalog[contribution_id]["view_model"]
                ),
            } if contribution_catalog[contribution_id].get("contract_version")
                 == UI_CONTRIBUTION_CONTRACT_VERSION_V2 else {}),
        }
        for contribution_id in contribution_ids
    ]
    if uses_ports:
        capability_packs = [
            {
                **row,
                "domain_adapter_port_requirements": deepcopy(
                    manifests[str(row["id"])].get(
                        "domain_adapter_port_requirements"
                    ) or []
                ),
            }
            for row in capability_packs
        ]
        domain_adapters = [
            {
                **row,
                "contract_version": str(
                    adapter_catalog[str(row["adapter_id"])].get(
                        "contract_version"
                    ) or ""
                ),
                "ports": deepcopy(
                    adapter_catalog[str(row["adapter_id"])].get("ports") or []
                ),
            }
            for row in domain_adapters
        ]

    port_resolutions: list[dict[str, Any]] = []
    if uses_ports:
        adapters_by_id = {
            str(row["adapter_id"]): row for row in domain_adapters
        }
        for pack_id in active_pack_ids:
            manifest = manifests[pack_id]
            requirements = _clean_port_requirements(
                manifest.get("domain_adapter_port_requirements"),
                field=f"{pack_id}.domain_adapter_port_requirements",
            )
            for requirement in requirements:
                resolved: list[dict[str, Any]] = []
                for adapter_id in manifest.get("domain_adapter_ids") or []:
                    adapter = adapters_by_id.get(str(adapter_id)) or {}
                    for port_ref in adapter.get("ports") or []:
                        if not isinstance(port_ref, dict) or str(
                            port_ref.get("port_id") or ""
                        ) != requirement["port_id"]:
                            continue
                        _version_range_supports(
                            port_ref.get("port_version"),
                            requirement["version_range"],
                            field=(
                                f"{pack_id}.{requirement['port_id']}.version_range"
                            ),
                        )
                        port = port_catalog.get((
                            str(port_ref.get("port_id") or ""),
                            str(port_ref.get("port_version") or ""),
                        ))
                        if port is None or str(port.get("contract_sha256") or "") != str(
                            port_ref.get("contract_sha256") or ""
                        ):
                            raise PluginRegistryError(
                                f"能力包 {pack_id} 的端口解析哈希无效。"
                            )
                        resolved.append({
                            "adapter_id": str(adapter.get("adapter_id") or ""),
                            "adapter_version": str(adapter.get("adapter_version") or ""),
                            "adapter_contract_sha256": str(
                                adapter.get("contract_sha256") or ""
                            ),
                            "port_id": str(port["port_id"]),
                            "port_version": str(port["port_version"]),
                            "port_contract_sha256": str(port["contract_sha256"]),
                            "handler_method": str(port["handler_method"]),
                            "input_schema_version": str(
                                (port.get("input_schema") or {}).get("version") or ""
                            ),
                            "input_schema_sha256": str(
                                port.get("input_schema_sha256") or ""
                            ),
                            "output_schema_version": str(
                                (port.get("output_schema") or {}).get("version") or ""
                            ),
                            "output_schema_sha256": str(
                                port.get("output_schema_sha256") or ""
                            ),
                            "provider_call_budget": int(
                                port.get("provider_call_budget") or 0
                            ),
                            "market_read_budget": int(
                                port.get("market_read_budget") or 0
                            ),
                            "business_write_budget": int(
                                port.get("business_write_budget") or 0
                            ),
                            "failure_policy": str(port.get("failure_policy") or ""),
                        })
                if requirement["requirement"] == "required" and not resolved:
                    raise PluginRegistryError(
                        f"能力包 {pack_id} 的必需端口 {requirement['port_id']} 未解析。"
                    )
                if requirement["cardinality"] == "one" and len(resolved) > 1:
                    raise PluginRegistryError(
                        f"能力包 {pack_id} 的端口 {requirement['port_id']} 基数冲突。"
                    )
                port_resolutions.append({
                    "owner_pack_id": pack_id,
                    **requirement,
                    "resolved": resolved,
                })

        for contribution in ui_contributions:
            if contribution.get("contract_version") != UI_CONTRIBUTION_CONTRACT_VERSION_V2:
                continue
            source_port = contribution.get("source_port") or {}
            matching = [
                row
                for row in port_resolutions
                if row.get("owner_pack_id") == source_port.get("owner_pack_id")
                and row.get("port_id") == source_port.get("port_id")
                and row.get("requirement") == source_port.get("requirement")
                and row.get("cardinality") == source_port.get("cardinality")
            ]
            if len(matching) != 1 or len(matching[0].get("resolved") or []) != 1:
                raise PluginRegistryError(
                    f"UI contribution {contribution.get('contribution_id')} source port is not uniquely resolved."
                )
            resolved_port = matching[0]["resolved"][0]
            contribution["source_port_resolution"] = {
                "owner_pack_id": str(matching[0]["owner_pack_id"]),
                "port_id": str(resolved_port["port_id"]),
                "port_version": str(resolved_port["port_version"]),
                "port_contract_sha256": str(
                    resolved_port["port_contract_sha256"]
                ),
                "output_schema_version": str(
                    resolved_port["output_schema_version"]
                ),
                "output_schema_sha256": str(
                    resolved_port["output_schema_sha256"]
                ),
            }

    resolution_version = (
        "plugin_registry_resolution_v2"
        if uses_ports
        else "plugin_registry_resolution_v1"
    )
    resolved_catalog = {
        "version": resolution_version,
        "room_kernel_version": ROOM_KERNEL_VERSION,
        "selected_capability_pack_ids": selected,
        "capability_packs": capability_packs,
        "domain_adapters": domain_adapters,
        "ui_contributions": ui_contributions,
        **({"port_resolutions": port_resolutions} if uses_ports else {}),
        "safety": deepcopy(FIXED_PLUGIN_SAFETY),
    }
    snapshot = {
        "version": (
            PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2
            if uses_ports
            else PLUGIN_REGISTRY_SNAPSHOT_VERSION_V1
        ),
        "room_kernel_version": ROOM_KERNEL_VERSION,
        # This seal intentionally excludes unrelated catalog entries. Adding a
        # different pack must not invalidate an already-frozen dependency set.
        "resolved_catalog_sha256": canonical_sha256(resolved_catalog),
        "selected_capability_pack_ids": selected,
        "capability_packs": capability_packs,
        "domain_adapters": domain_adapters,
        "ui_contributions": ui_contributions,
        **({"port_resolutions": port_resolutions} if uses_ports else {}),
        "resolution": {
            "dependency_policy": "exact_builtin_only",
            "unknown_version_policy": "fail_closed",
            "duplicate_id_policy": "reject",
            "history_policy": "append_only_keep_frozen_projection",
            "dynamic_code_loading": False,
            **({
                "port_resolution_policy": "manifest_declared_exact_only",
                "undeclared_port_policy": "reject",
                "required_port_policy": "fail_closed",
            } if uses_ports else {}),
        },
        "safety": deepcopy(FIXED_PLUGIN_SAFETY),
    }
    snapshot["registry_snapshot_sha256"] = canonical_sha256(snapshot)
    return snapshot


def validate_room_plugin_registry_snapshot(
    value: Any,
    capability_pack_ids: Iterable[str] | None,
    *,
    require_current: bool = True,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PluginRegistryError("房间插件 registry snapshot 必须是对象。")
    snapshot = deepcopy(value)
    snapshot_version = str(snapshot.get("version") or "")
    if snapshot_version not in {
        PLUGIN_REGISTRY_SNAPSHOT_VERSION_V1,
        PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2,
    }:
        raise PluginRegistryError("房间插件 registry snapshot 版本不受支持。")
    _require_exact_keys(
        snapshot,
        required={
            "version",
            "room_kernel_version",
            "resolved_catalog_sha256",
            "selected_capability_pack_ids",
            "capability_packs",
            "domain_adapters",
            "ui_contributions",
            "resolution",
            "safety",
            "registry_snapshot_sha256",
            *({"port_resolutions"} if snapshot_version == PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2 else set()),
        },
        identity=f"{snapshot_version} snapshot",
    )
    stored_sha256 = str(snapshot.pop("registry_snapshot_sha256", "") or "").lower()
    if not SHA256_PATTERN.fullmatch(stored_sha256):
        raise PluginRegistryError("房间插件 registry snapshot 缺少有效哈希。")
    if canonical_sha256(snapshot) != stored_sha256:
        raise PluginRegistryError("房间插件 registry snapshot 完整性校验失败。")
    snapshot["registry_snapshot_sha256"] = stored_sha256
    safety = _require_exact_keys(
        snapshot.get("safety"),
        required=set(FIXED_PLUGIN_SAFETY),
        identity="room_registry_snapshot.safety",
    )
    _validate_safety(safety, identity="room_registry_snapshot")
    resolution = _require_exact_keys(
        snapshot.get("resolution"),
        required={
            "dependency_policy",
            "unknown_version_policy",
            "duplicate_id_policy",
            "history_policy",
            "dynamic_code_loading",
            *({
                "port_resolution_policy",
                "undeclared_port_policy",
                "required_port_policy",
            } if snapshot_version == PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2 else set()),
        },
        identity="room_registry_snapshot.resolution",
    )
    expected_resolution = {
        "dependency_policy": "exact_builtin_only",
        "unknown_version_policy": "fail_closed",
        "duplicate_id_policy": "reject",
        "history_policy": "append_only_keep_frozen_projection",
        "dynamic_code_loading": False,
        **({
            "port_resolution_policy": "manifest_declared_exact_only",
            "undeclared_port_policy": "reject",
            "required_port_policy": "fail_closed",
        } if snapshot_version == PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2 else {}),
    }
    if resolution != expected_resolution:
        raise PluginRegistryError("room registry resolution policy is invalid.")
    expected_selected = (
        clean_capability_pack_ids(list(capability_pack_ids or []))
        if require_current
        else _clean_frozen_pack_ids(list(capability_pack_ids or []))
    )
    if snapshot.get("selected_capability_pack_ids") != expected_selected:
        raise PluginRegistryError("房间插件 registry snapshot 与能力包选择不一致。")
    for field, identity_field in (
        ("capability_packs", "id"),
        ("domain_adapters", "adapter_id"),
        ("ui_contributions", "contribution_id"),
    ):
        rows = snapshot.get(field)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise PluginRegistryError(f"房间插件 registry snapshot 的 {field} 无效。")
        identities = [str(row.get(identity_field) or "") for row in rows]
        if any(not identity for identity in identities) or len(identities) != len(set(identities)):
            raise PluginRegistryError(f"房间插件 registry snapshot 的 {field} 身份无效。")
    for pack in snapshot.get("capability_packs") or []:
        _require_exact_keys(
            pack,
            required=(
                _SNAPSHOT_PACK_V1_KEYS
                | ({"domain_adapter_port_requirements"} if snapshot_version == PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2 else set())
            ),
            identity=f"snapshot capability pack {pack.get('id')}",
        )
        _semver(pack.get("pack_version"), field="snapshot.pack_version")
        if not SHA256_PATTERN.fullmatch(str(pack.get("manifest_sha256") or "")):
            raise PluginRegistryError("snapshot capability pack hash is invalid.")
        if type(pack.get("system_managed")) is not bool:
            raise PluginRegistryError("snapshot capability pack system flag is invalid.")
        _string_list(pack.get("domain_adapter_ids"), field="snapshot.domain_adapter_ids")
        _string_list(pack.get("ui_contribution_ids"), field="snapshot.ui_contribution_ids")
        if snapshot_version == PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2:
            _clean_frozen_port_requirements(
                pack.get("domain_adapter_port_requirements"),
                field=f"snapshot.{pack.get('id')}.domain_adapter_port_requirements",
            )
    for adapter in snapshot.get("domain_adapters") or []:
        contract_version = str(
            adapter.get("contract_version")
            or DOMAIN_ADAPTER_CONTRACT_VERSION_V1
        )
        _require_exact_keys(
            adapter,
            required=(
                _SNAPSHOT_ADAPTER_V1_KEYS
                | ({"contract_version", "ports"} if snapshot_version == PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2 else set())
            ),
            identity=f"snapshot domain adapter {adapter.get('adapter_id')}",
        )
        if contract_version not in {
            DOMAIN_ADAPTER_CONTRACT_VERSION_V1,
            DOMAIN_ADAPTER_CONTRACT_VERSION_V2,
        } or adapter.get("status") != "ready":
            raise PluginRegistryError("snapshot domain adapter state is invalid.")
    for contribution in snapshot.get("ui_contributions") or []:
        contribution_version = str(
            contribution.get("contract_version")
            or UI_CONTRIBUTION_CONTRACT_VERSION_V1
        )
        is_v2 = contribution_version == UI_CONTRIBUTION_CONTRACT_VERSION_V2
        _require_exact_keys(
            contribution,
            required=(
                _SNAPSHOT_UI_V1_KEYS
                | ({
                    "contract_version",
                    "source_port",
                    "view_model",
                    "source_port_resolution",
                } if is_v2 else set())
            ),
            identity=f"snapshot UI contribution {contribution.get('contribution_id')}",
        )
        if contribution_version not in {
            UI_CONTRIBUTION_CONTRACT_VERSION_V1,
            UI_CONTRIBUTION_CONTRACT_VERSION_V2,
        } or contribution.get("status") != "ready":
            raise PluginRegistryError("snapshot UI contribution state is invalid.")
    if snapshot_version == PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2:
        trusted_catalog = plugin_registry_catalog() if require_current else {}
        trusted_adapters = {
            (str(row.get("adapter_id") or ""), str(row.get("adapter_version") or "")): row
            for row in trusted_catalog.get("domain_adapters") or []
            if isinstance(row, dict)
        }
        trusted_ports = {
            (str(row.get("port_id") or ""), str(row.get("port_version") or "")): row
            for row in trusted_catalog.get("domain_adapter_ports") or []
            if isinstance(row, dict)
        }
        adapter_port_refs: dict[
            tuple[str, str, str, str], dict[str, Any]
        ] = {}
        for adapter in snapshot.get("domain_adapters") or []:
            adapter_id = str(adapter.get("adapter_id") or "")
            adapter_version = str(adapter.get("adapter_version") or "")
            adapter_sha256 = str(adapter.get("contract_sha256") or "").lower()
            if (
                not adapter_id
                or not SEMVER_PATTERN.fullmatch(adapter_version)
                or not SHA256_PATTERN.fullmatch(adapter_sha256)
            ):
                raise PluginRegistryError("v2 registry adapter 精确身份无效。")
            adapter_contract_version = str(
                adapter.get("contract_version")
                or DOMAIN_ADAPTER_CONTRACT_VERSION_V1
            )
            if adapter_contract_version == DOMAIN_ADAPTER_CONTRACT_VERSION_V1:
                if adapter.get("ports"):
                    raise PluginRegistryError("v2 registry 的旧 adapter 不得携带端口。")
                continue
            if adapter_contract_version != DOMAIN_ADAPTER_CONTRACT_VERSION_V2:
                raise PluginRegistryError("v2 registry adapter 合同版本无效。")
            trusted_adapter = trusted_adapters.get((adapter_id, adapter_version))
            if require_current and (
                trusted_adapter is None
                or trusted_adapter.get("contract_version")
                != DOMAIN_ADAPTER_CONTRACT_VERSION_V2
                or str(trusted_adapter.get("contract_sha256") or "")
                != adapter_sha256
            ):
                raise PluginRegistryError("v2 registry adapter 精确合同不受信任。")
            ports = adapter.get("ports")
            if not isinstance(ports, list) or not ports:
                raise PluginRegistryError("v2 registry adapter 缺少精确端口。")
            seen_port_ids: set[str] = set()
            for port in ports:
                _require_exact_keys(
                    port,
                    required=_SNAPSHOT_ADAPTER_PORT_KEYS,
                    identity=f"snapshot adapter port {adapter_id}",
                )
                port_id = str(port.get("port_id") or "")
                port_version = str(port.get("port_version") or "")
                port_sha256 = str(port.get("contract_sha256") or "").lower()
                handler_method = str(port.get("handler_method") or "")
                if (
                    port_id not in HOST_DOMAIN_ADAPTER_PORT_IDS
                    or port_id in seen_port_ids
                    or not SEMVER_PATTERN.fullmatch(port_version)
                    or not SHA256_PATTERN.fullmatch(port_sha256)
                    or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", handler_method)
                ):
                    raise PluginRegistryError("v2 registry adapter 精确端口身份无效。")
                trusted_port = trusted_ports.get((port_id, port_version))
                trusted_adapter_port = next((
                    row
                    for row in (trusted_adapter or {}).get("ports") or []
                    if isinstance(row, dict)
                    and str(row.get("port_id") or "") == port_id
                    and str(row.get("port_version") or "") == port_version
                ), None)
                if require_current and (
                    trusted_port is None
                    or trusted_adapter_port is None
                    or trusted_adapter_port != port
                    or str(trusted_port.get("contract_sha256") or "") != port_sha256
                    or str(trusted_port.get("handler_method") or "") != handler_method
                    or HOST_DOMAIN_ADAPTER_PORT_HANDLERS.get(port_id) != handler_method
                ):
                    raise PluginRegistryError("v2 registry adapter 端口合同不受信任。")
                for hash_field in (
                    "input_schema_sha256",
                    "output_schema_sha256",
                ):
                    if not SHA256_PATTERN.fullmatch(str(port.get(hash_field) or "")):
                        raise PluginRegistryError("v2 registry adapter schema hash is invalid.")
                for version_field in (
                    "input_schema_version",
                    "output_schema_version",
                ):
                    _require_nonempty_string(
                        port.get(version_field),
                        field=f"{adapter_id}.{port_id}.{version_field}",
                        maximum=120,
                    )
                _string_list(
                    port.get("read_surfaces"),
                    field=f"{adapter_id}.{port_id}.read_surfaces",
                )
                _string_list(
                    port.get("local_write_surfaces"),
                    field=f"{adapter_id}.{port_id}.local_write_surfaces",
                )
                if (
                    port.get("cardinality") not in {"one", "multiple"}
                    or port.get("external_write_allowed") is not False
                    or port.get("failure_policy") != "fail_closed"
                    or any(
                        type(port.get(field)) is not int or port.get(field) != 0
                        for field in (
                            "provider_call_budget",
                            "market_read_budget",
                            "business_write_budget",
                        )
                    )
                ):
                    raise PluginRegistryError("v2 registry adapter port policy is invalid.")
                _validate_safety(port, identity=f"{adapter_id}.{port_id}")
                seen_port_ids.add(port_id)
                adapter_port_refs[(
                    adapter_id,
                    adapter_version,
                    port_id,
                    port_version,
                )] = {
                    "adapter_contract_sha256": adapter_sha256,
                    "port_contract_sha256": port_sha256,
                    "handler_method": handler_method,
                    "input_schema_version": str(port["input_schema_version"]),
                    "input_schema_sha256": str(port["input_schema_sha256"]),
                    "output_schema_version": str(port["output_schema_version"]),
                    "output_schema_sha256": str(port["output_schema_sha256"]),
                    "provider_call_budget": 0,
                    "market_read_budget": 0,
                    "business_write_budget": 0,
                    "failure_policy": "fail_closed",
                }
        expected_requirements: dict[tuple[str, str], dict[str, str]] = {}
        owner_adapter_ids: dict[str, set[str]] = {}
        for pack in snapshot.get("capability_packs") or []:
            pack_id = str(pack.get("id") or "")
            owner_adapter_ids[pack_id] = {
                str(item or "") for item in pack.get("domain_adapter_ids") or []
            }
            requirements = _clean_frozen_port_requirements(
                pack.get("domain_adapter_port_requirements"),
                field=f"snapshot.{pack_id}.domain_adapter_port_requirements",
            )
            for requirement in requirements:
                expected_requirements[(pack_id, requirement["port_id"])] = requirement
        port_resolutions = snapshot.get("port_resolutions")
        if not isinstance(port_resolutions, list) or not port_resolutions:
            raise PluginRegistryError("v2 registry 缺少端口解析。")
        seen_requirement_keys: set[tuple[str, str]] = set()
        for resolution in port_resolutions:
            _require_exact_keys(
                resolution,
                required={
                    "owner_pack_id",
                    "port_id",
                    "requirement",
                    "cardinality",
                    "version_range",
                    "resolved",
                },
                identity="snapshot port resolution",
            )
            owner_pack_id = str(resolution.get("owner_pack_id") or "")
            port_id = str(resolution.get("port_id") or "")
            requirement_key = (owner_pack_id, port_id)
            expected_requirement = expected_requirements.get(requirement_key)
            if (
                not owner_pack_id
                or port_id not in HOST_DOMAIN_ADAPTER_PORT_IDS
                or requirement_key in seen_requirement_keys
                or resolution.get("requirement") not in {"required", "optional"}
                or resolution.get("cardinality") not in {"one", "multiple"}
                or expected_requirement is None
                or any(
                    resolution.get(field) != expected_requirement.get(field)
                    for field in (
                        "port_id",
                        "requirement",
                        "cardinality",
                        "version_range",
                    )
                )
            ):
                raise PluginRegistryError("v2 registry 端口要求无效。")
            seen_requirement_keys.add(requirement_key)
            resolved = resolution.get("resolved")
            if not isinstance(resolved, list):
                raise PluginRegistryError("v2 registry 端口解析结果无效。")
            if resolution.get("requirement") == "required" and not resolved:
                raise PluginRegistryError("v2 registry 必需端口没有解析结果。")
            if resolution.get("cardinality") == "one" and len(resolved) > 1:
                raise PluginRegistryError("v2 registry 端口解析基数冲突。")
            for item in resolved:
                _require_exact_keys(
                    item,
                    required=_SNAPSHOT_RESOLVED_PORT_KEYS,
                    identity="snapshot resolved port binding",
                )
                item_key = (
                    str(item.get("adapter_id") or ""),
                    str(item.get("adapter_version") or ""),
                    str(item.get("port_id") or ""),
                    str(item.get("port_version") or ""),
                ) if isinstance(item, dict) else ("", "", "", "")
                adapter_port_ref = adapter_port_refs.get(item_key)
                if (
                    not isinstance(item, dict)
                    or adapter_port_ref is None
                    or item_key[0] not in owner_adapter_ids.get(owner_pack_id, set())
                ):
                    raise PluginRegistryError("v2 registry 端口解析引用无效。")
                for hash_field in (
                    "adapter_contract_sha256",
                    "port_contract_sha256",
                    "input_schema_sha256",
                    "output_schema_sha256",
                ):
                    if not SHA256_PATTERN.fullmatch(
                        str(item.get(hash_field) or "").lower()
                    ):
                        raise PluginRegistryError(
                            f"v2 registry 端口解析 {hash_field} 无效。"
                        )
                expected_exact = {
                    field: adapter_port_ref[field]
                    for field in (
                        "adapter_contract_sha256",
                        "port_contract_sha256",
                        "handler_method",
                        "input_schema_version",
                        "input_schema_sha256",
                        "output_schema_version",
                        "output_schema_sha256",
                        "provider_call_budget",
                        "market_read_budget",
                        "business_write_budget",
                        "failure_policy",
                    )
                }
                if any(item.get(field) != expected for field, expected in expected_exact.items()):
                    raise PluginRegistryError("v2 registry 端口解析合同发生漂移。")
                _version_range_supports(
                    item_key[3],
                    resolution.get("version_range"),
                    field=f"snapshot.{owner_pack_id}.{port_id}.version_range",
                )
                for budget_field in (
                    "provider_call_budget",
                    "market_read_budget",
                    "business_write_budget",
                ):
                    if item.get(budget_field) != 0:
                        raise PluginRegistryError(
                            f"v2 registry 端口解析 {budget_field} 必须为 0。"
                        )
                if item.get("failure_policy") != "fail_closed":
                    raise PluginRegistryError("v2 registry 端口必须失败关闭。")
        if seen_requirement_keys != set(expected_requirements):
            raise PluginRegistryError("v2 registry 端口要求与解析集合不一致。")
        pack_ui_ids = {
            str(pack.get("id") or ""): set(pack.get("ui_contribution_ids") or [])
            for pack in snapshot.get("capability_packs") or []
        }
        for contribution in snapshot.get("ui_contributions") or []:
            if contribution.get("contract_version") != UI_CONTRIBUTION_CONTRACT_VERSION_V2:
                continue
            source_port = _require_exact_keys(
                contribution.get("source_port"),
                required={"owner_pack_id", "port_id", "requirement", "cardinality"},
                identity="snapshot UI source port",
            )
            view_model = _require_exact_keys(
                contribution.get("view_model"),
                required={"schema_version", "schema_sha256"},
                identity="snapshot UI view model",
            )
            source_resolution = _require_exact_keys(
                contribution.get("source_port_resolution"),
                required={
                    "owner_pack_id",
                    "port_id",
                    "port_version",
                    "port_contract_sha256",
                    "output_schema_version",
                    "output_schema_sha256",
                },
                identity="snapshot UI source port resolution",
            )
            owner_pack_id = str(source_port.get("owner_pack_id") or "")
            if (
                source_port.get("requirement") != "required"
                or source_port.get("cardinality") != "one"
                or contribution.get("contribution_id")
                not in pack_ui_ids.get(owner_pack_id, set())
                or not SHA256_PATTERN.fullmatch(
                    str(view_model.get("schema_sha256") or "")
                )
                or not str(view_model.get("schema_version") or "")
            ):
                raise PluginRegistryError("v2 registry UI contribution binding is invalid.")
            matching = [
                row
                for row in port_resolutions
                if row.get("owner_pack_id") == owner_pack_id
                and row.get("port_id") == source_port.get("port_id")
                and row.get("requirement") == "required"
                and row.get("cardinality") == "one"
            ]
            if len(matching) != 1 or len(matching[0].get("resolved") or []) != 1:
                raise PluginRegistryError("v2 registry UI source port is not unique.")
            resolved_port = matching[0]["resolved"][0]
            expected_source_resolution = {
                "owner_pack_id": owner_pack_id,
                "port_id": resolved_port["port_id"],
                "port_version": resolved_port["port_version"],
                "port_contract_sha256": resolved_port["port_contract_sha256"],
                "output_schema_version": resolved_port["output_schema_version"],
                "output_schema_sha256": resolved_port["output_schema_sha256"],
            }
            if source_resolution != expected_source_resolution:
                raise PluginRegistryError("v2 registry UI source port seal drifted.")
    elif "port_resolutions" in snapshot or any(
        isinstance(adapter, dict) and adapter.get("ports")
        for adapter in snapshot.get("domain_adapters") or []
    ):
        raise PluginRegistryError("v1 registry 不得携带未封印端口解析。")
    resolved_catalog = {
        "version": (
            "plugin_registry_resolution_v2"
            if snapshot_version == PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2
            else "plugin_registry_resolution_v1"
        ),
        "room_kernel_version": snapshot.get("room_kernel_version"),
        "selected_capability_pack_ids": snapshot.get(
            "selected_capability_pack_ids"
        ),
        "capability_packs": snapshot.get("capability_packs"),
        "domain_adapters": snapshot.get("domain_adapters"),
        "ui_contributions": snapshot.get("ui_contributions"),
        **({
            "port_resolutions": snapshot.get("port_resolutions"),
        } if snapshot_version == PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2 else {}),
        "safety": snapshot.get("safety"),
    }
    if str(snapshot.get("resolved_catalog_sha256") or "").lower() != canonical_sha256(
        resolved_catalog
    ):
        raise PluginRegistryError("房间插件 registry 的依赖闭包哈希无效。")
    if require_current:
        current = build_room_plugin_registry_snapshot(expected_selected)
        if current != snapshot:
            raise PluginRegistryError("房间插件 registry snapshot 与当前受信任 registry 不兼容。")
    return snapshot


__all__ = [
    "DOMAIN_ADAPTER_CONTRACT_VERSION",
    "DOMAIN_ADAPTER_CONTRACT_VERSION_V1",
    "DOMAIN_ADAPTER_CONTRACT_VERSION_V2",
    "DOMAIN_ADAPTER_PORT_CONTRACT_VERSION",
    "FIXED_PLUGIN_SAFETY",
    "HOST_DOMAIN_ADAPTER_PORT_IDS",
    "HOST_UI_VIEW_MODEL_SCHEMAS",
    "PLUGIN_REGISTRY_CATALOG_VERSION",
    "PLUGIN_REGISTRY_CATALOG_VERSION_V1",
    "PLUGIN_REGISTRY_CATALOG_VERSION_V2",
    "PLUGIN_REGISTRY_CATALOG_VERSION_V3",
    "PLUGIN_REGISTRY_CONTRACT_KINDS",
    "PLUGIN_REGISTRY_SNAPSHOT_VERSION",
    "PLUGIN_REGISTRY_SNAPSHOT_VERSION_V1",
    "PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2",
    "PluginRegistryError",
    "UI_CONTRIBUTION_CONTRACT_VERSION",
    "UI_CONTRIBUTION_CONTRACT_VERSION_V1",
    "UI_CONTRIBUTION_CONTRACT_VERSION_V2",
    "build_room_plugin_registry_snapshot",
    "plugin_registry_catalog",
    "plugin_registry_catalog_v3",
    "resolve_plugin_registry_contract_exact",
    "resolve_plugin_registry_contract_latest",
    "validate_plugin_registry_catalog_v3",
    "validate_room_plugin_registry_snapshot",
]
