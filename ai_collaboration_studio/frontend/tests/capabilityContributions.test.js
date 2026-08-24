import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  ARTIFACT_PLUGIN_REGISTRY_CONTEXT_VERSION,
  capabilityPackContractMeta,
  capabilityRegistryView,
  frozenPortResolution,
  hasProjectWorkspaceFootprint,
  HOST_CONTRIBUTION_IDS,
  HOST_SLOT_IDS,
  resolveHostOwnedSlot,
  resolvedHostContribution,
  shortPluginHash,
  PLUGIN_REGISTRY_CATALOG_VERSION_V2,
  PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2,
  PROJECT_READINESS_VIEW_MODEL_SCHEMA_VERSION,
  STOCK_RESEARCH_VIEW_MODEL_SCHEMA_VERSION,
} from "../src/capabilityContributions.js";
import { stockResearchInspectorActivation } from "../src/stockResearch.js";

const safety = {
  execution_capability: "none",
  live_trading_allowed: false,
  can_autonomously_decide: false,
  can_replace_user_decision: false,
  arbitrary_code_loading_allowed: false,
  user_final_decision_required: true,
};

const readinessViewModelRequired = [
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
];

const stockViewModelRequired = [
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
];

function fixture() {
  const packHash = "b".repeat(64);
  const room = {
    capability_pack_ids: ["structured_project_research"],
    plugin_registry_integrity_ok: true,
    plugin_registry_snapshot: {
      version: "plugin_registry_snapshot_v1",
      registry_snapshot_sha256: "a".repeat(64),
      safety,
      capability_packs: [{
        id: "structured_project_research",
        name: "结构化项目研究",
        pack_version: "1.0.0",
        manifest_sha256: packHash,
        system_managed: false,
        domain_adapter_ids: [],
        ui_contribution_ids: ["project_research.artifact_workspace/v1"],
      }],
      domain_adapters: [],
      ui_contributions: [{
        contribution_id: "project_research.artifact_workspace/v1",
        contribution_version: "1.0.0",
        contract_sha256: "c".repeat(64),
        slot_id: "core.artifact.workspace/v1",
        component_key: "project_research_workspace",
        label: "项目证据与方案工作区",
        order: 200,
        status: "ready",
      }],
      resolution: { dynamic_code_loading: false },
    },
  };
  const packs = [{
    id: "structured_project_research",
    name: "结构化项目研究",
    pack_version: "1.0.0",
    manifest_sha256: packHash,
    domain_adapter_ids: [],
    ui_contribution_ids: ["project_research.artifact_workspace/v1"],
  }];
  const contribution = room.plugin_registry_snapshot.ui_contributions[0];
  const pluginRegistry = {
    version: "plugin_registry_catalog_v1",
    catalog_sha256: "e".repeat(64),
    safety,
    ui_contributions: [{
      contract_version: "ui_contribution_contract_v1",
      contribution_id: contribution.contribution_id,
      contribution_version: contribution.contribution_version,
      contract_sha256: contribution.contract_sha256,
      slot_id: contribution.slot_id,
      component_key: contribution.component_key,
      label: contribution.label,
      mode: "host_owned_component",
      order: contribution.order,
      allowed_actions: ["artifact.project_research.edit"],
      ...safety,
    }],
  };
  const lifecycleTarget = (kind, id, version, targetSha256, label, ownerPackIds) => ({
    kind,
    id,
    version,
    target_sha256: targetSha256,
    label,
    system_managed: false,
    owner_pack_ids: ownerPackIds,
    dependencies: [],
    catalog_state: "active",
    activation_state: "enabled",
    runtime_state: "ready",
    integrity_ok: true,
    implementation_available: true,
    runtime_available: true,
    new_bindings_allowed: true,
    head_sequence: 0,
    head_sha256: "1".repeat(64),
    current_event_id: "",
    current_event_sha256: "",
    reason: "",
    replacement: null,
    replacement_status: {
      declared: false,
      current_runtime_state: "",
      current_runtime_available: false,
      integrity_ok: true,
      automatic_migration_performed: false,
    },
    available_actions: ["disable", "quarantine", "deprecate", "tombstone"],
    integrity_issues: [],
    history: [],
  });
  const pluginLifecycle = {
    version: "plugin_lifecycle_catalog_v1",
    integrity_ok: true,
    targets: [
      lifecycleTarget(
        "capability_pack",
        packs[0].id,
        packs[0].pack_version,
        packs[0].manifest_sha256,
        packs[0].name,
        [packs[0].id],
      ),
      lifecycleTarget(
        "ui_contribution",
        contribution.contribution_id,
        contribution.contribution_version,
        contribution.contract_sha256,
        contribution.label,
        [packs[0].id],
      ),
    ],
    plugin_registry: pluginRegistry,
    capability_packs: packs,
    safety,
    view_sha256: "2".repeat(64),
  };
  const artifactContext = {
    version: ARTIFACT_PLUGIN_REGISTRY_CONTEXT_VERSION,
    status: "ready",
    integrity_ok: true,
    runtime_available: true,
    exact_binding: true,
    snapshot_sha256: room.plugin_registry_snapshot.registry_snapshot_sha256,
    snapshot: structuredClone(room.plugin_registry_snapshot),
    source_type: "round",
    ...safety,
  };
  return { room, packs, pluginRegistry, pluginLifecycle, artifactContext };
}

function readinessV2Fixture() {
  const base = fixture();
  const readinessPackHash = "3".repeat(64);
  const adapterHash = "4".repeat(64);
  const portHash = "5".repeat(64);
  const contributionHash = "6".repeat(64);
  const inputHash = "7".repeat(64);
  const outputHash = "8".repeat(64);
  const viewModelHash = "a".repeat(64);
  const readinessPack = {
    id: "project_readiness_review",
    name: "项目就绪度只读复核",
    pack_version: "1.0.0",
    manifest_sha256: readinessPackHash,
    system_managed: false,
    domain_adapter_ids: ["project_readiness"],
    domain_adapter_port_requirements: [{
      port_id: "core.artifact.projection/v1",
      requirement: "required",
      cardinality: "one",
      version_range: ">=1.0.0 <2.0.0",
    }],
    ui_contribution_ids: ["project_readiness.artifact_workspace/v1"],
  };
  const storagePack = {
    id: "storage_research_readonly",
    name: "存储研究",
    pack_version: "1.1.0",
    manifest_sha256: "9".repeat(64),
    system_managed: false,
    domain_adapter_ids: ["storage_research"],
    domain_adapter_port_requirements: [],
    ui_contribution_ids: [],
  };
  const projectAdapterPort = {
    port_id: "core.artifact.projection/v1",
    port_version: "1.0.0",
    contract_sha256: portHash,
    handler_method: "project_artifact",
    cardinality: "multiple",
    input_schema_version: "artifact_projection_input_v1",
    input_schema_sha256: inputHash,
    output_schema_version: "artifact_projection_output_v1",
    output_schema_sha256: outputHash,
    read_surfaces: ["artifact.version.exact", "artifact.evidence_relations.exact"],
    local_write_surfaces: [],
    provider_call_budget: 0,
    market_read_budget: 0,
    business_write_budget: 0,
    external_write_allowed: false,
    failure_policy: "fail_closed",
    ...safety,
  };
  const projectAdapter = {
    contract_version: "domain_adapter_contract_v2",
    adapter_id: "project_readiness",
    adapter_version: "1.0.0",
    contract_sha256: adapterHash,
    status: "ready",
    ports: [projectAdapterPort],
  };
  const legacyStorageAdapter = {
    contract_version: "domain_adapter_contract_v1",
    adapter_id: "storage_research",
    adapter_version: "1.0.0",
    contract_sha256: "0".repeat(64),
    status: "ready",
    ports: [],
  };
  const frozenContribution = {
    contribution_id: "project_readiness.artifact_workspace/v1",
    contribution_version: "1.0.0",
    contract_sha256: contributionHash,
    slot_id: "core.artifact.workspace/v1",
    component_key: "project_readiness_review",
    label: "项目就绪度只读复核",
    order: 250,
    status: "ready",
    contract_version: "ui_contribution_contract_v2",
    source_port: {
      owner_pack_id: readinessPack.id,
      port_id: "core.artifact.projection/v1",
      requirement: "required",
      cardinality: "one",
    },
    view_model: {
      schema_version: PROJECT_READINESS_VIEW_MODEL_SCHEMA_VERSION,
      schema_sha256: viewModelHash,
    },
    source_port_resolution: {
      owner_pack_id: readinessPack.id,
      port_id: "core.artifact.projection/v1",
      port_version: "1.0.0",
      port_contract_sha256: portHash,
      output_schema_version: "artifact_projection_output_v1",
      output_schema_sha256: outputHash,
    },
  };
  const portBinding = {
    adapter_id: "project_readiness",
    adapter_version: "1.0.0",
    adapter_contract_sha256: adapterHash,
    port_id: "core.artifact.projection/v1",
    port_version: "1.0.0",
    port_contract_sha256: portHash,
    handler_method: "project_artifact",
    input_schema_version: "artifact_projection_input_v1",
    input_schema_sha256: inputHash,
    output_schema_version: "artifact_projection_output_v1",
    output_schema_sha256: outputHash,
    provider_call_budget: 0,
    market_read_budget: 0,
    business_write_budget: 0,
    failure_policy: "fail_closed",
  };
  const snapshotPatch = {
    version: PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2,
    capability_packs: [
      ...base.room.plugin_registry_snapshot.capability_packs,
      readinessPack,
      storagePack,
    ],
    domain_adapters: [projectAdapter, legacyStorageAdapter],
    ui_contributions: [
      ...base.room.plugin_registry_snapshot.ui_contributions,
      frozenContribution,
    ],
    port_resolutions: [{
      owner_pack_id: readinessPack.id,
      port_id: "core.artifact.projection/v1",
      requirement: "required",
      cardinality: "one",
      version_range: ">=1.0.0 <2.0.0",
      resolved: [portBinding],
    }],
    resolution: {
      ...base.room.plugin_registry_snapshot.resolution,
      port_resolution_policy: "manifest_declared_exact_only",
      undeclared_port_policy: "reject",
      required_port_policy: "fail_closed",
    },
  };
  Object.assign(base.room.plugin_registry_snapshot, structuredClone(snapshotPatch));
  Object.assign(base.artifactContext.snapshot, structuredClone(snapshotPatch));
  base.room.capability_pack_ids.push(readinessPack.id, storagePack.id);
  base.packs.push(readinessPack, storagePack);

  const catalogContribution = {
    contract_version: "ui_contribution_contract_v2",
    contribution_id: frozenContribution.contribution_id,
    contribution_version: frozenContribution.contribution_version,
    contract_sha256: frozenContribution.contract_sha256,
    pack_id: readinessPack.id,
    slot_id: frozenContribution.slot_id,
    component_key: frozenContribution.component_key,
    label: frozenContribution.label,
    mode: "host_owned_component",
    cardinality: "multiple",
    order: frozenContribution.order,
    visibility_capabilities: ["research.project.readiness_review"],
    allowed_actions: ["project_readiness.inspect"],
    source_port: structuredClone(frozenContribution.source_port),
    view_model: structuredClone(frozenContribution.view_model),
    ...safety,
  };
  base.pluginRegistry.version = PLUGIN_REGISTRY_CATALOG_VERSION_V2;
  base.pluginRegistry.domain_adapter_ports = [{
    contract_version: "domain_adapter_port_contract_v1",
    port_id: "core.artifact.projection/v1",
    port_version: "1.0.0",
    handler_method: "project_artifact",
    cardinality: "multiple",
    input_schema: {
      version: "artifact_projection_input_v1",
      type: "object",
      required: ["artifact", "evidence_relations"],
      fields: { artifact: "object", evidence_relations: "array<object>" },
      additional_properties: false,
    },
    input_schema_sha256: inputHash,
    output_schema: {
      version: "artifact_projection_output_v1",
      type: "object",
      required: ["version"],
      fields: { version: "project_readiness_projection_v1" },
      additional_properties: false,
    },
    output_schema_sha256: outputHash,
    read_surfaces: ["artifact.version.exact", "artifact.evidence_relations.exact"],
    local_write_surfaces: [],
    provider_call_budget: 0,
    market_read_budget: 0,
    business_write_budget: 0,
    external_write_allowed: false,
    failure_policy: "fail_closed",
    ...safety,
    contract_sha256: portHash,
  }];
  base.pluginRegistry.ui_view_model_schemas = [{
    schema_version: PROJECT_READINESS_VIEW_MODEL_SCHEMA_VERSION,
    component_key: "project_readiness_review",
    type: "object",
    required: readinessViewModelRequired,
    fields: Object.fromEntries(readinessViewModelRequired.map((field) => [field, "declared"])),
    additional_properties: false,
    schema_sha256: viewModelHash,
  }];
  base.pluginRegistry.ui_contributions.push(catalogContribution);
  base.pluginRegistry.domain_adapters = [
    { ...projectAdapter, ...safety },
    { ...legacyStorageAdapter, ...safety },
  ];
  base.pluginLifecycle.capability_packs.push(readinessPack, storagePack);
  const sourceTarget = base.pluginLifecycle.targets[0];
  const target = (kind, id, version, targetHash, ownerPackIds) => ({
    ...structuredClone(sourceTarget),
    kind,
    id,
    version,
    target_sha256: targetHash,
    owner_pack_ids: ownerPackIds,
  });
  base.pluginLifecycle.targets.push(
    target("capability_pack", readinessPack.id, readinessPack.pack_version, readinessPackHash, [readinessPack.id]),
    target("capability_pack", storagePack.id, storagePack.pack_version, storagePack.manifest_sha256, [storagePack.id]),
    target("domain_adapter", projectAdapter.adapter_id, projectAdapter.adapter_version, adapterHash, [readinessPack.id]),
    target("domain_adapter", legacyStorageAdapter.adapter_id, legacyStorageAdapter.adapter_version, legacyStorageAdapter.contract_sha256, [storagePack.id]),
    target("ui_contribution", frozenContribution.contribution_id, frozenContribution.contribution_version, contributionHash, [readinessPack.id]),
  );
  return { ...base, portBinding };
}

function stockV2Fixture() {
  const base = readinessV2Fixture();
  const packHash = "c".repeat(64);
  const adapterHash = "d".repeat(64);
  const portHash = "e".repeat(64);
  const contributionHash = "f".repeat(64);
  const inputHash = "1".repeat(64);
  const outputHash = "2".repeat(64);
  const viewModelHash = "3".repeat(64);
  const pack = {
    id: "stock_research_readonly",
    name: "通用股票只读研究",
    pack_version: "1.0.0",
    manifest_sha256: packHash,
    system_managed: false,
    domain_adapter_ids: ["stock_research"],
    domain_adapter_port_requirements: [{
      port_id: "core.market.readonly_context/v1",
      requirement: "required",
      cardinality: "one",
      version_range: ">=1.0.0 <2.0.0",
    }],
    ui_contribution_ids: ["stock_research.room_inspector/v1"],
  };
  const adapterPort = {
    port_id: "core.market.readonly_context/v1",
    port_version: "1.0.0",
    contract_sha256: portHash,
    handler_method: "project_market_readonly_context",
    cardinality: "multiple",
    input_schema_version: "stock_market_readonly_context_input_v1",
    input_schema_sha256: inputHash,
    output_schema_version: "stock_market_readonly_context_output_v1",
    output_schema_sha256: outputHash,
    read_surfaces: [
      "room.stock_room_scope.exact",
      "room.material.version.exact",
      "room.material.content_sha256.exact",
      "room.material.snapshot_sha256.exact",
    ],
    local_write_surfaces: [],
    provider_call_budget: 0,
    market_read_budget: 0,
    business_write_budget: 0,
    external_write_allowed: false,
    failure_policy: "fail_closed",
    ...safety,
  };
  const adapter = {
    contract_version: "domain_adapter_contract_v2",
    adapter_id: "stock_research",
    adapter_version: "1.0.0",
    contract_sha256: adapterHash,
    status: "ready",
    ports: [adapterPort],
  };
  const frozenContribution = {
    contribution_id: "stock_research.room_inspector/v1",
    contribution_version: "1.0.0",
    contract_sha256: contributionHash,
    slot_id: HOST_SLOT_IDS.roomInspector,
    component_key: "stock_research_inspector",
    label: "通用股票只读检查",
    order: 240,
    status: "ready",
    contract_version: "ui_contribution_contract_v2",
    source_port: {
      owner_pack_id: pack.id,
      port_id: "core.market.readonly_context/v1",
      requirement: "required",
      cardinality: "one",
    },
    view_model: {
      schema_version: STOCK_RESEARCH_VIEW_MODEL_SCHEMA_VERSION,
      schema_sha256: viewModelHash,
    },
    source_port_resolution: {
      owner_pack_id: pack.id,
      port_id: "core.market.readonly_context/v1",
      port_version: "1.0.0",
      port_contract_sha256: portHash,
      output_schema_version: "stock_market_readonly_context_output_v1",
      output_schema_sha256: outputHash,
    },
  };
  const portBinding = {
    adapter_id: adapter.adapter_id,
    adapter_version: adapter.adapter_version,
    adapter_contract_sha256: adapterHash,
    port_id: adapterPort.port_id,
    port_version: adapterPort.port_version,
    port_contract_sha256: portHash,
    handler_method: adapterPort.handler_method,
    input_schema_version: adapterPort.input_schema_version,
    input_schema_sha256: inputHash,
    output_schema_version: adapterPort.output_schema_version,
    output_schema_sha256: outputHash,
    provider_call_budget: 0,
    market_read_budget: 0,
    business_write_budget: 0,
    failure_policy: "fail_closed",
  };

  base.room.capability_pack_ids.push(pack.id);
  base.room.plugin_registry_snapshot.capability_packs.push(structuredClone(pack));
  base.room.plugin_registry_snapshot.domain_adapters.push(structuredClone(adapter));
  base.room.plugin_registry_snapshot.ui_contributions.push(structuredClone(frozenContribution));
  base.room.plugin_registry_snapshot.port_resolutions.push({
    owner_pack_id: pack.id,
    port_id: adapterPort.port_id,
    requirement: "required",
    cardinality: "one",
    version_range: ">=1.0.0 <2.0.0",
    resolved: [structuredClone(portBinding)],
  });

  base.pluginRegistry.domain_adapter_ports.push({
    contract_version: "domain_adapter_port_contract_v1",
    port_id: adapterPort.port_id,
    port_version: adapterPort.port_version,
    handler_method: adapterPort.handler_method,
    cardinality: "multiple",
    input_schema: {
      version: adapterPort.input_schema_version,
      type: "object",
      required: ["contract"],
      fields: { contract: "stock_research_contract_v1" },
      additional_properties: false,
    },
    input_schema_sha256: inputHash,
    output_schema: {
      version: adapterPort.output_schema_version,
      type: "object",
      required: ["version"],
      fields: { version: "stock_research_contract_v1" },
      additional_properties: false,
    },
    output_schema_sha256: outputHash,
    read_surfaces: [...adapterPort.read_surfaces],
    local_write_surfaces: [],
    provider_call_budget: 0,
    market_read_budget: 0,
    business_write_budget: 0,
    external_write_allowed: false,
    failure_policy: "fail_closed",
    ...safety,
    contract_sha256: portHash,
  });
  base.pluginRegistry.ui_view_model_schemas.push({
    schema_version: STOCK_RESEARCH_VIEW_MODEL_SCHEMA_VERSION,
    component_key: "stock_research_inspector",
    type: "object",
    required: stockViewModelRequired,
    fields: Object.fromEntries(stockViewModelRequired.map((field) => [field, "declared"])),
    additional_properties: false,
    schema_sha256: viewModelHash,
  });
  base.pluginRegistry.ui_contributions.push({
    contract_version: "ui_contribution_contract_v2",
    contribution_id: frozenContribution.contribution_id,
    contribution_version: frozenContribution.contribution_version,
    contract_sha256: contributionHash,
    pack_id: pack.id,
    slot_id: frozenContribution.slot_id,
    component_key: frozenContribution.component_key,
    label: frozenContribution.label,
    mode: "host_owned_component",
    cardinality: "multiple",
    order: frozenContribution.order,
    visibility_capabilities: ["research.stock.readonly"],
    allowed_actions: ["stock_research.inspect"],
    source_port: structuredClone(frozenContribution.source_port),
    view_model: structuredClone(frozenContribution.view_model),
    ...safety,
  });
  base.pluginRegistry.domain_adapters.push({ ...structuredClone(adapter), ...safety });
  base.packs.push(pack);
  base.pluginLifecycle.capability_packs.push(pack);
  const sourceTarget = base.pluginLifecycle.targets[0];
  const target = (kind, id, version, hash, ownerPackIds) => ({
    ...structuredClone(sourceTarget),
    kind,
    id,
    version,
    target_sha256: hash,
    owner_pack_ids: ownerPackIds,
  });
  base.pluginLifecycle.targets.push(
    target("capability_pack", pack.id, pack.pack_version, packHash, [pack.id]),
    target("domain_adapter", adapter.adapter_id, adapter.adapter_version, adapterHash, [pack.id]),
    target(
      "ui_contribution",
      frozenContribution.contribution_id,
      frozenContribution.contribution_version,
      contributionHash,
      [pack.id],
    ),
  );
  return base;
}

test("stock inspector activates only through its exact v2 read-only market port", () => {
  const { room, pluginRegistry, pluginLifecycle } = stockV2Fixture();
  const activation = stockResearchInspectorActivation({
    frozenContext: room,
    runtimeContext: room,
    pluginRegistry,
    pluginLifecycle,
  });

  assert.equal(activation.visible, true);
  assert.equal(activation.active, true);
  assert.equal(activation.contribution.id, HOST_CONTRIBUTION_IDS.stockResearchRoomInspector);
  assert.deepEqual(activation.contribution.allowedActions, ["stock_research.inspect"]);
  assert.equal(activation.contribution.sourcePort.portId, "core.market.readonly_context/v1");

  const drifted = structuredClone(room);
  drifted.plugin_registry_snapshot.domain_adapters
    .find((item) => item.adapter_id === "stock_research")
    .ports[0].handler_method = "project_market_write_context";
  assert.equal(stockResearchInspectorActivation({
    frozenContext: drifted,
    runtimeContext: drifted,
    pluginRegistry,
    pluginLifecycle,
  }).active, false);
});

test("versioned room registry resolves only host-owned contributions", () => {
  const { room, packs } = fixture();
  const view = capabilityRegistryView(room, packs);

  assert.equal(view.status, "ready");
  assert.equal(view.packs[0].version, "1.0.0");
  assert.equal(view.contributions[0].slotId, "core.artifact.workspace/v1");
  assert.equal(view.dynamicCodeLoading, false);
  assert.equal(view.safety.can_replace_user_decision, false);
});

test("same pack id with a changed manifest hash fails closed", () => {
  const { room, packs } = fixture();
  packs[0].manifest_sha256 = "d".repeat(64);
  const view = capabilityRegistryView(room, packs);

  assert.equal(view.status, "integrity_failed");
  assert.match(view.errors.join("\n"), /受信任目录不一致/);
});

test("unknown slot or renderer is rejected instead of dynamically loaded", () => {
  const { room, packs } = fixture();
  room.plugin_registry_snapshot.ui_contributions[0].slot_id = "core.user_decision/v1";
  room.plugin_registry_snapshot.ui_contributions[0].component_key = "remote_bundle";
  const view = capabilityRegistryView(room, packs);

  assert.equal(view.integrityOk, false);
  assert.match(view.errors.join("\n"), /未被宿主允许/);
});

test("contract metadata and settings UI expose exact versions and frozen registry", () => {
  const { packs } = fixture();
  const meta = capabilityPackContractMeta(packs[0]);
  const source = readFileSync(
    new URL("../src/components/RoomSettingsDialog.jsx", import.meta.url),
    "utf8",
  );

  assert.equal(meta.version, "1.0.0");
  assert.equal(meta.contributionCount, 1);
  assert.equal(shortPluginHash("a".repeat(64)), "aaaaaaaa…aaaaaa");
  assert.match(source, /<CapabilityRegistrySnapshot/);
  assert.match(source, /pendingPackIds=\{form\.capability_pack_ids\}/);
});

test("host resolver requires an exact bootstrap catalog contribution and current room binding", () => {
  const { room, pluginRegistry, pluginLifecycle, artifactContext } = fixture();
  const slot = resolveHostOwnedSlot({
    slotId: HOST_SLOT_IDS.artifactWorkspace,
    frozenContext: artifactContext,
    runtimeContext: room,
    pluginRegistry,
    pluginLifecycle,
  });
  const project = resolvedHostContribution(slot, HOST_CONTRIBUTION_IDS.projectArtifactWorkspace);

  assert.equal(slot.status, "ready");
  assert.equal(slot.integrityOk, true);
  assert.equal(project.active, true);
  assert.deepEqual(project.allowedActions, ["artifact.project_research.edit"]);
});

test("host resolver follows the catalog exact version instead of hard-coding 1.0.0", () => {
  const { room, pluginRegistry, pluginLifecycle, artifactContext } = fixture();
  for (const contribution of [
    room.plugin_registry_snapshot.ui_contributions[0],
    artifactContext.snapshot.ui_contributions[0],
    pluginRegistry.ui_contributions[0],
  ]) {
    contribution.contribution_version = "1.1.0";
    contribution.contract_sha256 = "f".repeat(64);
  }
  const lifecycleContribution = pluginLifecycle.targets.find((item) => item.kind === "ui_contribution");
  lifecycleContribution.version = "1.1.0";
  lifecycleContribution.target_sha256 = "f".repeat(64);
  const slot = resolveHostOwnedSlot({
    slotId: HOST_SLOT_IDS.artifactWorkspace,
    frozenContext: artifactContext,
    runtimeContext: room,
    pluginRegistry,
    pluginLifecycle,
  });

  assert.equal(slot.status, "ready");
  assert.equal(slot.contributions[0].version, "1.1.0");
  assert.equal(slot.contributions[0].active, true);
});

test("readiness activates only from an exact v2 port resolution while a mixed v1 adapter remains valid", () => {
  const { room, pluginRegistry, pluginLifecycle, artifactContext } = readinessV2Fixture();
  const slot = resolveHostOwnedSlot({
    slotId: HOST_SLOT_IDS.artifactWorkspace,
    frozenContext: artifactContext,
    runtimeContext: room,
    pluginRegistry,
    pluginLifecycle,
  });
  const readiness = resolvedHostContribution(
    slot,
    HOST_CONTRIBUTION_IDS.projectReadinessArtifactWorkspace,
  );
  const resolution = frozenPortResolution(artifactContext.snapshot, {
    packId: "project_readiness_review",
    portId: "core.artifact.projection/v1",
  });

  assert.equal(slot.integrityOk, true);
  assert.equal(slot.snapshotVersion, PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2);
  assert.equal(readiness.active, true, JSON.stringify({ readiness, errors: slot.errors }));
  assert.deepEqual(readiness.allowedActions, ["project_readiness.inspect"]);
  assert.equal(resolution.adapter.adapter_id, "project_readiness");
  assert.equal(resolution.port.provider_call_budget, 0);
});

test("v1 snapshots and drifted top-level port resolutions never activate readiness", () => {
  const v1 = readinessV2Fixture();
  for (const context of [v1.room.plugin_registry_snapshot, v1.artifactContext.snapshot]) {
    context.version = "plugin_registry_snapshot_v1";
    delete context.port_resolutions;
    for (const adapter of context.domain_adapters) delete adapter.ports;
  }
  const v1Slot = resolveHostOwnedSlot({
    slotId: HOST_SLOT_IDS.artifactWorkspace,
    frozenContext: v1.artifactContext,
    runtimeContext: v1.room,
    pluginRegistry: v1.pluginRegistry,
    pluginLifecycle: v1.pluginLifecycle,
  });
  const v1Readiness = resolvedHostContribution(
    v1Slot,
    HOST_CONTRIBUTION_IDS.projectReadinessArtifactWorkspace,
  );
  assert.equal(v1Slot.integrityOk, false);
  assert.equal(v1Readiness, null);

  const drifted = readinessV2Fixture();
  drifted.artifactContext.snapshot.port_resolutions[0].resolved[0].port_contract_sha256 = "a".repeat(64);
  const driftedSlot = resolveHostOwnedSlot({
    slotId: HOST_SLOT_IDS.artifactWorkspace,
    frozenContext: drifted.artifactContext,
    runtimeContext: drifted.room,
    pluginRegistry: drifted.pluginRegistry,
    pluginLifecycle: drifted.pluginLifecycle,
  });
  assert.equal(driftedSlot.integrityOk, false);
  assert.equal(driftedSlot.runtimeAvailable, false);
});

test("catalog v1 cannot activate the v2 readiness contribution", () => {
  const value = readinessV2Fixture();
  value.pluginRegistry.version = "plugin_registry_catalog_v1";
  const slot = resolveHostOwnedSlot({
    slotId: HOST_SLOT_IDS.artifactWorkspace,
    frozenContext: value.artifactContext,
    runtimeContext: value.room,
    pluginRegistry: value.pluginRegistry,
    pluginLifecycle: value.pluginLifecycle,
  });
  const readiness = resolvedHostContribution(
    slot,
    HOST_CONTRIBUTION_IDS.projectReadinessArtifactWorkspace,
  );

  assert.equal(slot.runtimeAvailable, false);
  assert.equal(readiness?.active, false);
  assert.deepEqual(readiness?.allowedActions, []);
});

test("catalog and frozen v2 readiness declaration drift fails closed", () => {
  const cases = [
    ["port catalog missing", (value) => { delete value.pluginRegistry.domain_adapter_ports; }],
    ["view-model fields missing", (value) => { delete value.pluginRegistry.ui_view_model_schemas[0].fields; }],
    ["catalog source port drift", (value) => { value.pluginRegistry.ui_contributions.at(-1).source_port.port_id = "core.user_decision/v1"; }],
    ["catalog view-model hash drift", (value) => { value.pluginRegistry.ui_contributions.at(-1).view_model.schema_sha256 = "f".repeat(64); }],
    ["frozen source resolution drift", (value) => {
      value.artifactContext.snapshot.ui_contributions.at(-1)
        .source_port_resolution.output_schema_sha256 = "f".repeat(64);
    }],
  ];

  for (const [label, mutate] of cases) {
    const value = readinessV2Fixture();
    mutate(value);
    const slot = resolveHostOwnedSlot({
      slotId: HOST_SLOT_IDS.artifactWorkspace,
      frozenContext: value.artifactContext,
      runtimeContext: value.room,
      pluginRegistry: value.pluginRegistry,
      pluginLifecycle: value.pluginLifecycle,
    });
    const readiness = resolvedHostContribution(
      slot,
      HOST_CONTRIBUTION_IDS.projectReadinessArtifactWorkspace,
    );
    assert.notEqual(readiness?.active, true, label);
    assert.deepEqual(readiness?.allowedActions || [], [], label);
  }
});

test("frozen readiness port requires exact handler, range, requirement, and closed rows", () => {
  const cases = [
    ["binding handler", (snapshot) => { snapshot.port_resolutions[0].resolved[0].handler_method = "evil"; }],
    ["resolution range", (snapshot) => { snapshot.port_resolutions[0].version_range = ">=0.0.0"; }],
    ["pack requirement", (snapshot) => {
      snapshot.capability_packs.find((pack) => pack.id === "project_readiness_review")
        .domain_adapter_port_requirements[0].version_range = ">=0.0.0";
    }],
    ["binding extra field", (snapshot) => { snapshot.port_resolutions[0].resolved[0].extra = true; }],
    ["adapter port handler", (snapshot) => {
      snapshot.domain_adapters.find((adapter) => adapter.adapter_id === "project_readiness")
        .ports[0].handler_method = "evil";
    }],
    ["adapter port extra field", (snapshot) => {
      snapshot.domain_adapters.find((adapter) => adapter.adapter_id === "project_readiness")
        .ports[0].extra = true;
    }],
  ];

  for (const [label, mutate] of cases) {
    const { artifactContext } = readinessV2Fixture();
    mutate(artifactContext.snapshot);
    assert.equal(frozenPortResolution(artifactContext.snapshot, {
      packId: "project_readiness_review",
      portId: "core.artifact.projection/v1",
    }), null, label);
  }
});

test("integrity remains valid while an unavailable historical implementation is read only", () => {
  const { room, pluginRegistry, pluginLifecycle, artifactContext } = fixture();
  artifactContext.status = "implementation_unavailable";
  artifactContext.runtime_available = false;
  artifactContext.snapshot.ui_contributions[0].contribution_version = "0.9.0";
  const slot = resolveHostOwnedSlot({
    slotId: HOST_SLOT_IDS.artifactWorkspace,
    frozenContext: artifactContext,
    runtimeContext: room,
    pluginRegistry,
    pluginLifecycle,
  });
  const project = resolvedHostContribution(slot, HOST_CONTRIBUTION_IDS.projectArtifactWorkspace);

  assert.equal(slot.integrityOk, true);
  assert.equal(slot.status, "implementation_unavailable");
  assert.equal(project.present, true);
  assert.equal(project.active, false);
  assert.deepEqual(project.allowedActions, []);
});

test("removing the current room contribution preserves the frozen renderer but denies actions", () => {
  const { room, pluginRegistry, pluginLifecycle, artifactContext } = fixture();
  room.plugin_registry_snapshot.ui_contributions = [];
  const slot = resolveHostOwnedSlot({
    slotId: HOST_SLOT_IDS.artifactWorkspace,
    frozenContext: artifactContext,
    runtimeContext: room,
    pluginRegistry,
    pluginLifecycle,
  });
  const project = resolvedHostContribution(slot, HOST_CONTRIBUTION_IDS.projectArtifactWorkspace);

  assert.equal(slot.status, "read_only");
  assert.equal(project.present, true);
  assert.equal(project.readOnly, true);
  assert.deepEqual(project.allowedActions, []);
});

test("dynamic loading and user-decision slots fail closed", () => {
  const { pluginRegistry, pluginLifecycle, artifactContext } = fixture();
  artifactContext.snapshot.resolution.dynamic_code_loading = true;
  const dynamic = resolveHostOwnedSlot({
    slotId: HOST_SLOT_IDS.artifactWorkspace,
    frozenContext: artifactContext,
    pluginRegistry,
    pluginLifecycle,
  });
  const forbidden = resolveHostOwnedSlot({
    slotId: "core.user_decision/v1",
    frozenContext: artifactContext,
    pluginRegistry,
    pluginLifecycle,
  });

  assert.equal(dynamic.status, "integrity_failed");
  assert.equal(dynamic.runtimeAvailable, false);
  assert.match(dynamic.reason, /完整性/);
  assert.equal(forbidden.status, "integrity_failed");
  assert.match(forbidden.reason, /完整性|最终决定/);
});

test("legacy fallback recognizes only persisted project-specific content", () => {
  assert.equal(hasProjectWorkspaceFootprint({ requirements: [{ id: "r1" }] }), true);
  assert.equal(hasProjectWorkspaceFootprint({ risks: [{ id: "risk1" }] }), true);
  assert.equal(hasProjectWorkspaceFootprint({
    decision: { options: [{ value: "降低等待时间", dependencies: [] }] },
  }), true);
  assert.equal(hasProjectWorkspaceFootprint({
    decision: { options: [{ title: "普通候选", reversibility: "unknown" }] },
  }), false);
});

test("App and host components wire catalog contexts without current-room history gates", () => {
  const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
  const artifactSource = readFileSync(
    new URL("../src/components/ArtifactDialog.jsx", import.meta.url),
    "utf8",
  );
  const inspectorSource = readFileSync(
    new URL("../src/components/RoomInspector.jsx", import.meta.url),
    "utf8",
  );
  const experimentSource = readFileSync(
    new URL("../src/components/CandidateExperimentPanel.jsx", import.meta.url),
    "utf8",
  );
  const readinessSource = readFileSync(
    new URL("../src/components/ProjectReadinessPanel.jsx", import.meta.url),
    "utf8",
  );

  assert.match(appSource, /setPluginRegistry\(data\.plugin_registry/);
  assert.match(appSource, /pluginRegistry=\{pluginRegistry\}/);
  assert.doesNotMatch(artifactSource, /hasRoomCapability/);
  assert.match(artifactSource, /workingArtifact\.plugin_registry_context/);
  assert.match(artifactSource, /disabled=\{projectWorkspaceReadOnly\}/);
  assert.doesNotMatch(artifactSource, /import \{ ProjectReadinessPanel \} from "\.\/ProjectReadinessPanel"/);
  assert.match(
    artifactSource,
    /const ProjectReadinessPanel = lazy\(\(\) => import\("\.\/ProjectReadinessPanel\.jsx"\)/,
  );
  assert.ok(artifactSource.indexOf("<ProjectReadinessPanel") < artifactSource.indexOf("<MemoUserFinalDecisionSection"));
  assert.ok(artifactSource.indexOf("<CandidateExperimentPanel") < artifactSource.indexOf("<MemoUserFinalDecisionSection"));
  assert.doesNotMatch(inspectorSource, /hasRoomCapability/);
  assert.match(inspectorSource, /roomInspectorRegistrySource = pendingRound \|\| room/);
  assert.match(inspectorSource, /<PluginActionBoundary disabled=\{storageReadOnly\}/);
  assert.match(experimentSource, /\|\| readOnly\s*\|\| !runtimeGateReady/);
  assert.match(readinessSource, /api\.projectReadiness\(/);
  assert.doesNotMatch(readinessSource, /import\s*\(/);
});
