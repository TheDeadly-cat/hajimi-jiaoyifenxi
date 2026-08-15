import assert from "node:assert/strict";
import test from "node:test";

import {
  normalizeProjectReadinessResponse,
  PROJECT_READINESS_INSPECT_ACTION,
  PROJECT_READINESS_PROJECTION_VERSION,
  PROJECT_READINESS_RESOLUTION_VERSION,
  projectReadinessLoadPlan,
} from "../src/projectReadiness.js";
import { PROJECT_READINESS_VIEW_MODEL_SCHEMA_VERSION } from "../src/capabilityContributions.js";

const declaredPortId = "core.artifact.projection/v1";
const pluginSafety = {
  execution_capability: "none",
  live_trading_allowed: false,
  can_autonomously_decide: false,
  can_replace_user_decision: false,
  arbitrary_code_loading_allowed: false,
  user_final_decision_required: true,
};

const hashes = {
  plugin: "a".repeat(64),
  contribution: "b".repeat(64),
  adapter: "c".repeat(64),
  port: "d".repeat(64),
  input: "e".repeat(64),
  output: "f".repeat(64),
  viewModel: "3".repeat(64),
  artifact: "1".repeat(64),
  evidence: "2".repeat(64),
};

function fixture() {
  const portBinding = {
    adapter_id: "project_readiness",
    adapter_version: "1.0.0",
    adapter_contract_sha256: hashes.adapter,
    port_id: declaredPortId,
    port_version: "1.0.0",
    port_contract_sha256: hashes.port,
    handler_method: "project_artifact",
    input_schema_version: "artifact_projection_input_v1",
    input_schema_sha256: hashes.input,
    output_schema_version: "artifact_projection_output_v1",
    output_schema_sha256: hashes.output,
    provider_call_budget: 0,
    market_read_budget: 0,
    business_write_budget: 0,
    failure_policy: "fail_closed",
  };
  const artifact = {
    id: "artifact/一",
    version: 3,
    status: "CONFIRMED",
    plugin_registry_context: {
      version: "artifact_plugin_registry_context_v1",
      status: "ready",
      integrity_ok: true,
      runtime_available: true,
      exact_binding: true,
      snapshot_sha256: hashes.plugin,
      snapshot: {
        version: "plugin_registry_snapshot_v2",
        registry_snapshot_sha256: hashes.plugin,
        capability_packs: [{
          id: "project_readiness_review",
          domain_adapter_ids: ["project_readiness"],
          domain_adapter_port_requirements: [{
            port_id: declaredPortId,
            requirement: "required",
            cardinality: "one",
            version_range: ">=1.0.0 <2.0.0",
          }],
          ui_contribution_ids: ["project_readiness.artifact_workspace/v1"],
        }],
        domain_adapters: [{
          contract_version: "domain_adapter_contract_v2",
          adapter_id: "project_readiness",
          adapter_version: "1.0.0",
          contract_sha256: hashes.adapter,
          status: "ready",
          ports: [{
            port_id: declaredPortId,
            port_version: "1.0.0",
            contract_sha256: hashes.port,
            handler_method: "project_artifact",
            cardinality: "multiple",
            input_schema_version: "artifact_projection_input_v1",
            input_schema_sha256: hashes.input,
            output_schema_version: "artifact_projection_output_v1",
            output_schema_sha256: hashes.output,
            read_surfaces: ["artifact.version.exact", "artifact.evidence_relations.exact"],
            local_write_surfaces: [],
            provider_call_budget: 0,
            market_read_budget: 0,
            business_write_budget: 0,
            external_write_allowed: false,
            failure_policy: "fail_closed",
            ...pluginSafety,
          }],
        }],
        ui_contributions: [{
          contribution_id: "project_readiness.artifact_workspace/v1",
          contribution_version: "1.0.0",
          contract_sha256: hashes.contribution,
          slot_id: "core.artifact.workspace/v1",
          component_key: "project_readiness_review",
          status: "ready",
          contract_version: "ui_contribution_contract_v2",
          source_port: {
            owner_pack_id: "project_readiness_review",
            port_id: declaredPortId,
            requirement: "required",
            cardinality: "one",
          },
          view_model: {
            schema_version: PROJECT_READINESS_VIEW_MODEL_SCHEMA_VERSION,
            schema_sha256: hashes.viewModel,
          },
          source_port_resolution: {
            owner_pack_id: "project_readiness_review",
            port_id: declaredPortId,
            port_version: "1.0.0",
            port_contract_sha256: hashes.port,
            output_schema_version: "artifact_projection_output_v1",
            output_schema_sha256: hashes.output,
          },
        }],
        port_resolutions: [{
          owner_pack_id: "project_readiness_review",
          port_id: declaredPortId,
          requirement: "required",
          cardinality: "one",
          version_range: ">=1.0.0 <2.0.0",
          resolved: [portBinding],
        }],
        resolution: {
          dynamic_code_loading: false,
          port_resolution_policy: "manifest_declared_exact_only",
          undeclared_port_policy: "reject",
          required_port_policy: "fail_closed",
        },
      },
    },
  };
  const contribution = {
    id: "project_readiness.artifact_workspace/v1",
    version: "1.0.0",
    contractHash: hashes.contribution,
    slotId: "core.artifact.workspace/v1",
    componentKey: "project_readiness_review",
    packId: "project_readiness_review",
    contractVersion: "ui_contribution_contract_v2",
    sourcePort: {
      ownerPackId: "project_readiness_review",
      portId: declaredPortId,
      requirement: "required",
      cardinality: "one",
    },
    viewModel: {
      schemaVersion: PROJECT_READINESS_VIEW_MODEL_SCHEMA_VERSION,
      schemaHash: hashes.viewModel,
    },
    sourcePortResolution: {
      ownerPackId: "project_readiness_review",
      portId: declaredPortId,
      portVersion: "1.0.0",
      portContractHash: hashes.port,
      outputSchemaVersion: "artifact_projection_output_v1",
      outputSchemaHash: hashes.output,
    },
    present: true,
    active: true,
    allowedActions: [PROJECT_READINESS_INSPECT_ACTION],
    reason: "",
  };
  const slot = { integrityOk: true, status: "ready", reason: "" };
  const room = { id: "room/一" };
  return { artifact, contribution, portBinding, room, slot };
}

function responseFixture(plan) {
  const expectedPort = plan.expected.resolution.port;
  return {
    ok: true,
    projection: {
      version: PROJECT_READINESS_PROJECTION_VERSION,
      integrity_ok: true,
      metrics_visible: true,
      room_id: plan.expected.roomId,
      artifact_id: plan.expected.artifactId,
      artifact_version: plan.expected.artifactVersion,
      artifact_snapshot_sha256: hashes.artifact,
      evidence_graph_sha256: hashes.evidence,
      plugin_registry_snapshot_sha256: hashes.plugin,
      resolution: {
        version: PROJECT_READINESS_RESOLUTION_VERSION,
        contribution: structuredClone(plan.expected.resolution.contribution),
        adapter: structuredClone(plan.expected.resolution.adapter),
        port: {
          port_id: expectedPort.port_id,
          port_version: expectedPort.port_version,
          contract_sha256: expectedPort.contract_sha256,
          input_schema_version: expectedPort.input_schema_version,
          input_schema_sha256: expectedPort.input_schema_sha256,
          output_schema_version: expectedPort.output_schema_version,
          output_schema_sha256: expectedPort.output_schema_sha256,
          provider_call_budget: expectedPort.provider_call_budget,
          market_read_budget: expectedPort.market_read_budget,
          business_write_budget: expectedPort.business_write_budget,
          failure_policy: expectedPort.failure_policy,
        },
      },
      state: "blocked",
      structural_gaps: [{ code: "REQUIREMENT_ACCEPTANCE_MISSING", item_key: "requirements:r1", message: "需求缺少验收条件" }],
      blockers: [{ code: "BLOCKING_RISK_OPEN", item_key: "risks:k1", message: "阻断风险仍未处置" }],
      evidence_gaps: [{ code: "EVIDENCE_UNREVIEWED", item_key: "requirements:r1", message: "证据关系尚未核验" }],
      execution_capability: "none",
      live_trading_allowed: false,
      can_autonomously_decide: false,
      can_replace_user_decision: false,
      arbitrary_code_loading_allowed: false,
      provider_calls_performed: 0,
      market_reads_performed: 0,
      business_writes_performed: 0,
      ranking_produced: false,
      winner_claim: false,
      approval_produced: false,
      user_final_decision_required: true,
    },
  };
}

test("snapshot v2 exact contribution and port resolution authorizes one read-only GET", () => {
  const input = fixture();
  const plan = projectReadinessLoadPlan(input);

  assert.equal(plan.visible, true);
  assert.equal(plan.shouldLoad, true);
  assert.equal(plan.expected.resolution.adapter.adapter_id, "project_readiness");
  assert.equal(plan.expected.resolution.port.port_id, declaredPortId);
  assert.equal(plan.expected.resolution.port.provider_call_budget, 0);
});

test("legacy, v1, unavailable, and drifted port states remain visible with zero requests", () => {
  const cases = [
    (input) => { input.artifact.plugin_registry_context.version = ""; },
    (input) => {
      input.artifact.plugin_registry_context.snapshot.version = "plugin_registry_snapshot_v1";
      delete input.artifact.plugin_registry_context.snapshot.port_resolutions;
      delete input.artifact.plugin_registry_context.snapshot.domain_adapters[0].ports;
    },
    (input) => { input.contribution.active = false; input.contribution.reason = "adapter 当前不可用"; },
    (input) => { input.artifact.plugin_registry_context.snapshot.port_resolutions[0].resolved[0].port_contract_sha256 = "9".repeat(64); },
    (input) => { input.artifact.plugin_registry_context.snapshot.port_resolutions[0].resolved[0].handler_method = "evil"; },
    (input) => { input.artifact.plugin_registry_context.snapshot.port_resolutions[0].version_range = ">=0.0.0"; },
    (input) => {
      input.artifact.plugin_registry_context.snapshot.capability_packs[0]
        .domain_adapter_port_requirements[0].version_range = ">=0.0.0";
    },
    (input) => { input.artifact.plugin_registry_context.snapshot.domain_adapters[0].ports[0].extra = true; },
  ];
  for (const mutate of cases) {
    const input = fixture();
    mutate(input);
    const plan = projectReadinessLoadPlan({ ...input, showLegacyFallback: true });
    assert.equal(plan.visible, true);
    assert.equal(plan.shouldLoad, false);
    assert.equal(plan.mode, "read_only");
    assert.ok(plan.reason);
  }
});

test("strict readiness response exposes metrics only for exact bindings and zero budgets", () => {
  const plan = projectReadinessLoadPlan(fixture());
  const view = normalizeProjectReadinessResponse(responseFixture(plan), plan.expected);

  assert.equal(view.valid, true);
  assert.equal(view.metricsVisible, true);
  assert.equal(view.structuralGaps.length, 1);
  assert.equal(view.blockers.length, 1);
  assert.equal(view.evidenceGaps.length, 1);
});

test("identity, hash, resolution, safety, budget, and metric drift hides the entire projection", () => {
  const plan = projectReadinessLoadPlan(fixture());
  const mutations = [
    (value) => { value.projection.room_id = "other"; },
    (value) => { value.projection.artifact_version += 1; },
    (value) => { value.projection.artifact_snapshot_sha256 = "bad"; },
    (value) => { value.projection.evidence_graph_sha256 = "bad"; },
    (value) => { value.projection.plugin_registry_snapshot_sha256 = "3".repeat(64); },
    (value) => { value.projection.resolution.contribution.contract_sha256 = "3".repeat(64); },
    (value) => { value.projection.resolution.adapter.adapter_id = "other"; },
    (value) => { value.projection.resolution.port.port_id = "core.user_decision/v1"; },
    (value) => { value.projection.resolution.port.input_schema_sha256 = "3".repeat(64); },
    (value) => { value.projection.resolution.port.provider_call_budget = 1; },
    (value) => { value.projection.provider_calls_performed = 1; },
    (value) => { value.projection.market_reads_performed = 1; },
    (value) => { value.projection.business_writes_performed = 1; },
    (value) => { value.projection.ranking_produced = true; },
    (value) => { value.projection.winner_claim = true; },
    (value) => { value.projection.approval_produced = true; },
    (value) => { value.projection.can_autonomously_decide = true; },
    (value) => { value.projection.can_replace_user_decision = true; },
    (value) => { value.projection.arbitrary_code_loading_allowed = true; },
    (value) => { delete value.projection.arbitrary_code_loading_allowed; },
    (value) => { value.projection.state = "ready"; },
    (value) => { value.projection.metrics_visible = false; },
    (value) => { value.projection.unexpected = false; },
    (value) => { value.projection.structural_gaps.push(structuredClone(value.projection.structural_gaps[0])); },
  ];

  for (const mutate of mutations) {
    const response = responseFixture(plan);
    mutate(response);
    const view = normalizeProjectReadinessResponse(response, plan.expected);
    assert.equal(view.valid, false);
    assert.equal(view.metricsVisible, false);
    assert.deepEqual(view.structuralGaps, []);
    assert.deepEqual(view.blockers, []);
    assert.deepEqual(view.evidenceGaps, []);
  }
});
