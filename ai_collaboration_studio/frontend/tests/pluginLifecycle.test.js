import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  buildPluginLifecyclePreviewRequest,
  buildPluginLifecycleTransitionRequest,
  filterNewPackBindings,
  packSelectionAvailability,
  pluginLifecycleCatalogView,
  pluginLifecycleImpactPreviewView,
  pluginLifecycleTransitionResultView,
} from "../src/pluginLifecycle.js";

const safety = {
  execution_capability: "none",
  live_trading_allowed: false,
  can_autonomously_decide: false,
  can_replace_user_decision: false,
  arbitrary_code_loading_allowed: false,
  user_final_decision_required: true,
};

function target({ kind, id, version, targetSha256, label, ownerPackIds, systemManaged = false }) {
  return {
    kind,
    id,
    version,
    target_sha256: targetSha256,
    label,
    system_managed: systemManaged,
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
    head_sha256: "d".repeat(64),
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
    available_actions: systemManaged ? [] : ["disable", "quarantine", "deprecate", "tombstone"],
    integrity_issues: [],
    history: [],
  };
}

function fixture() {
  const pack = {
    id: "structured_project_research",
    manifest_version: "capability_pack_manifest_v1",
    pack_version: "1.0.0",
    manifest_sha256: "a".repeat(64),
    name: "结构化项目研究",
    description: "项目研究",
    system_managed: false,
    dependencies: [],
    domain_adapter_ids: [],
    ui_contribution_ids: ["project_research.artifact_workspace/v1"],
    capabilities: ["research.project.evidence_map"],
    ...safety,
  };
  const contribution = {
    contract_version: "ui_contribution_contract_v1",
    contribution_id: "project_research.artifact_workspace/v1",
    contribution_version: "1.0.0",
    contract_sha256: "b".repeat(64),
    pack_id: pack.id,
    slot_id: "core.artifact.workspace/v1",
    component_key: "project_research_workspace",
    label: "项目研究工作区",
    mode: "host_owned_component",
    order: 200,
    allowed_actions: ["artifact.project_research.edit"],
    ...safety,
  };
  const pluginRegistry = {
    version: "plugin_registry_catalog_v1",
    catalog_sha256: "c".repeat(64),
    capability_packs: [pack],
    domain_adapters: [],
    ui_contributions: [contribution],
    safety,
  };
  const lifecycle = {
    version: "plugin_lifecycle_catalog_v1",
    integrity_ok: true,
    targets: [
      target({
        kind: "capability_pack",
        id: pack.id,
        version: pack.pack_version,
        targetSha256: pack.manifest_sha256,
        label: pack.name,
        ownerPackIds: [pack.id],
      }),
      target({
        kind: "ui_contribution",
        id: contribution.contribution_id,
        version: contribution.contribution_version,
        targetSha256: contribution.contract_sha256,
        label: contribution.label,
        ownerPackIds: [pack.id],
      }),
    ],
    plugin_registry: pluginRegistry,
    capability_packs: [pack],
    safety,
    view_sha256: "e".repeat(64),
  };
  return { lifecycle, pack, contribution };
}

function replacementFixture({ missingReplacementTarget = false } = {}) {
  const { lifecycle, contribution } = fixture();
  const source = lifecycle.targets.find((item) => item.kind === "ui_contribution");
  const upgraded = {
    ...contribution,
    contribution_version: "1.1.0",
    contract_sha256: "9".repeat(64),
    label: "项目研究工作区 1.1",
  };
  lifecycle.plugin_registry.ui_contributions = [upgraded];
  source.catalog_state = "deprecated";
  source.runtime_state = "deprecated";
  source.implementation_available = false;
  source.runtime_available = false;
  source.new_bindings_allowed = false;
  source.available_actions = ["disable", "quarantine", "tombstone"];
  source.replacement = {
    kind: "ui_contribution",
    id: upgraded.contribution_id,
    version: upgraded.contribution_version,
    sha256: upgraded.contract_sha256,
  };
  source.replacement_status = {
    declared: true,
    current_runtime_state: missingReplacementTarget ? "replacement_unavailable" : "disabled",
    current_runtime_available: false,
    integrity_ok: !missingReplacementTarget,
    automatic_migration_performed: false,
  };
  if (!missingReplacementTarget) {
    const replacementTarget = target({
      kind: "ui_contribution",
      id: upgraded.contribution_id,
      version: upgraded.contribution_version,
      targetSha256: upgraded.contract_sha256,
      label: upgraded.label,
      ownerPackIds: [upgraded.pack_id],
    });
    replacementTarget.activation_state = "disabled";
    replacementTarget.runtime_state = "disabled";
    replacementTarget.runtime_available = false;
    replacementTarget.new_bindings_allowed = false;
    replacementTarget.available_actions = ["enable", "quarantine", "deprecate", "tombstone"];
    lifecycle.targets.push(replacementTarget);
  }
  return { lifecycle, source, upgraded };
}

function replacementGraphFixture() {
  const { lifecycle, contribution } = fixture();
  const source = lifecycle.targets.find((item) => item.kind === "ui_contribution");
  const versions = [
    {
      ...contribution,
      contribution_version: "1.1.0",
      contract_sha256: "4".repeat(64),
      label: "项目研究工作区 1.1",
    },
    {
      ...contribution,
      contribution_version: "1.2.0",
      contract_sha256: "5".repeat(64),
      label: "项目研究工作区 1.2",
    },
  ];
  lifecycle.plugin_registry.ui_contributions = [contribution, ...versions];
  const nodes = [
    source,
    ...versions.map((item) => target({
      kind: "ui_contribution",
      id: item.contribution_id,
      version: item.contribution_version,
      targetSha256: item.contract_sha256,
      label: item.label,
      ownerPackIds: [item.pack_id],
    })),
  ];
  lifecycle.targets.push(...nodes.slice(1));
  const declare = (from, to) => {
    from.replacement = {
      kind: to.kind,
      id: to.id,
      version: to.version,
      sha256: to.target_sha256,
    };
    from.replacement_status = {
      declared: true,
      current_runtime_state: to.runtime_state,
      current_runtime_available: to.runtime_available,
      integrity_ok: to.integrity_ok,
      automatic_migration_performed: false,
    };
  };
  return { lifecycle, nodes, declare };
}

test("strict lifecycle catalog binds the exact pack version and hash", () => {
  const { lifecycle, pack } = fixture();
  const view = pluginLifecycleCatalogView(lifecycle);

  assert.equal(view.integrityOk, true);
  assert.equal(view.capabilityPacks[0].lifecycle.runtimeState, "ready");
  assert.equal(packSelectionAvailability(view, pack.id).canAdd, true);
  assert.deepEqual(filterNewPackBindings([pack.id], view), { allowed: [pack.id], blocked: [] });
});

test("disabled pack cannot be newly bound but an existing room can remove it", () => {
  const { lifecycle, pack } = fixture();
  const packTarget = lifecycle.targets[0];
  packTarget.activation_state = "disabled";
  packTarget.runtime_state = "disabled";
  packTarget.runtime_available = false;
  packTarget.new_bindings_allowed = false;
  packTarget.available_actions = ["enable", "quarantine", "deprecate", "tombstone"];
  const view = pluginLifecycleCatalogView(lifecycle);

  assert.equal(view.integrityOk, true);
  assert.equal(packSelectionAvailability(view, pack.id).canToggle, false);
  assert.equal(packSelectionAvailability(view, pack.id, { selected: true }).canToggle, true);
  assert.deepEqual(filterNewPackBindings([pack.id], view), { allowed: [], blocked: [pack.id] });
});

test("replacement status is cross-checked against the exact same-id version", () => {
  const { lifecycle, source, upgraded } = replacementFixture();
  const view = pluginLifecycleCatalogView(lifecycle);

  assert.equal(view.integrityOk, true);
  assert.equal(view.replacementDeclarations.length, 1);
  assert.equal(view.replacementDeclarations[0].id, source.id);
  assert.equal(view.replacementDeclarations[0].replacement.version, upgraded.contribution_version);
  assert.equal(view.replacementDeclarations[0].replacementStatus.targetLabel, upgraded.label);
  assert.equal(view.replacementDeclarations[0].replacementStatus.currentRuntimeState, "disabled");
  assert.equal(view.replacementDeclarations[0].replacementStatus.usable, false);
});

test("a declared replacement missing from catalog targets fails closed", () => {
  const { lifecycle } = replacementFixture({ missingReplacementTarget: true });
  const view = pluginLifecycleCatalogView(lifecycle);

  assert.equal(view.integrityOk, false);
  assert.equal(view.replacementDeclarations[0].replacementStatus.exactTargetFound, false);
  assert.equal(view.replacementDeclarations[0].replacementStatus.integrityOk, false);
  assert.equal(view.replacementDeclarations[0].replacementStatus.usable, false);
  assert.match(view.errors.join("\n"), /替代目标未保留在生命周期目录/);
});

test("a persisted replacement with no current implementation stays valid and unavailable", () => {
  const { lifecycle, source } = replacementFixture();
  lifecycle.plugin_registry.ui_contributions = [];
  const replacementTarget = lifecycle.targets.find(
    (item) => item.kind === "ui_contribution" && item.version === "1.1.0",
  );
  replacementTarget.implementation_available = false;
  replacementTarget.activation_state = "enabled";
  replacementTarget.runtime_state = "implementation_unavailable";
  replacementTarget.runtime_available = false;
  replacementTarget.new_bindings_allowed = false;
  replacementTarget.available_actions = ["disable", "quarantine", "deprecate", "tombstone"];
  source.replacement_status.current_runtime_state = "implementation_unavailable";
  source.replacement_status.current_runtime_available = false;
  source.replacement_status.integrity_ok = true;

  const view = pluginLifecycleCatalogView(lifecycle);
  assert.equal(view.integrityOk, true, view.errors.join("\n"));
  assert.equal(view.replacementDeclarations[0].replacementStatus.exactTargetFound, true);
  assert.equal(view.replacementDeclarations[0].replacementStatus.exactlyVerified, true);
  assert.equal(view.replacementDeclarations[0].replacementStatus.usable, false);
});

test("an exact replacement chain remains valid", () => {
  const { lifecycle, nodes, declare } = replacementGraphFixture();
  declare(nodes[0], nodes[1]);
  declare(nodes[1], nodes[2]);
  const view = pluginLifecycleCatalogView(lifecycle);

  assert.equal(view.integrityOk, true);
  assert.equal(view.replacementDeclarations.length, 2);
  assert.equal(view.replacementDeclarations.every((item) => item.replacementStatus.exactlyVerified), true);
});

test("two-node and self replacement cycles fail the whole catalog closed", () => {
  const twoNode = replacementGraphFixture();
  twoNode.declare(twoNode.nodes[0], twoNode.nodes[1]);
  twoNode.declare(twoNode.nodes[1], twoNode.nodes[0]);
  const twoNodeView = pluginLifecycleCatalogView(twoNode.lifecycle);
  assert.equal(twoNodeView.integrityOk, false);
  assert.match(twoNodeView.errors.join("\n"), /替代声明图存在环/);

  const self = replacementGraphFixture();
  self.declare(self.nodes[0], self.nodes[0]);
  const selfView = pluginLifecycleCatalogView(self.lifecycle);
  assert.equal(selfView.integrityOk, false);
  assert.match(selfView.errors.join("\n"), /替代声明图存在环/);
});

test("replacement identity, status drift, and automatic migration fail closed", () => {
  const mutations = [
    (source) => { source.replacement.id = "different.stable.id"; },
    (source) => { source.replacement.version = source.version; },
    (source) => { source.replacement_status.declared = false; },
    (source) => { source.replacement_status.current_runtime_available = true; },
    (source) => { source.replacement_status.automatic_migration_performed = true; },
  ];
  for (const mutate of mutations) {
    const { lifecycle, source } = replacementFixture();
    mutate(source);
    assert.equal(pluginLifecycleCatalogView(lifecycle).integrityOk, false);
  }
});

test("implementation-missing targets expose only safe retirement actions and versioned history", () => {
  const { lifecycle, pack } = fixture();
  lifecycle.capability_packs = [];
  const packTarget = lifecycle.targets.find((item) => item.kind === "capability_pack");
  packTarget.implementation_available = false;
  packTarget.runtime_state = "implementation_unavailable";
  packTarget.runtime_available = false;
  packTarget.new_bindings_allowed = false;
  packTarget.available_actions = ["disable", "quarantine", "deprecate", "tombstone"];
  packTarget.history = [
    {
      version: "plugin_lifecycle_event_v1",
      id: "legacy_event",
      sequence_no: 1,
      action: "disable",
      replacement: null,
      effective_replacement: null,
      implementation_available_at_event: false,
      event_sha256: "7".repeat(64),
    },
    {
      version: "plugin_lifecycle_event_v2",
      id: "current_event",
      sequence_no: 2,
      action: "deprecate",
      replacement: null,
      effective_replacement: null,
      implementation_available_at_event: false,
      event_sha256: "8".repeat(64),
    },
  ];
  const view = pluginLifecycleCatalogView(lifecycle);

  assert.equal(view.integrityOk, true);
  assert.equal(view.targets.find((item) => item.id === pack.id).history[0].implementationAvailableAtEvent, true);
  assert.equal(view.targets.find((item) => item.id === pack.id).history[0].implementationAvailabilityAttested, false);
  assert.equal(view.targets.find((item) => item.id === pack.id).history[1].implementationAvailableAtEvent, false);
  assert.equal(view.targets.find((item) => item.id === pack.id).history[1].implementationAvailabilityAttested, true);

  const recoveryDrift = structuredClone(lifecycle);
  recoveryDrift.targets.find((item) => item.id === pack.id).available_actions = ["enable"];
  assert.equal(pluginLifecycleCatalogView(recoveryDrift).integrityOk, false);

  const missingV2Field = structuredClone(lifecycle);
  delete missingV2Field.targets.find((item) => item.id === pack.id).history[1].implementation_available_at_event;
  assert.equal(pluginLifecycleCatalogView(missingV2Field).integrityOk, false);

  for (const recoveryAction of ["enable", "clear_quarantine", "reinstate"]) {
    const recoveryEventDrift = structuredClone(lifecycle);
    recoveryEventDrift.targets.find((item) => item.id === pack.id).history[1].action = recoveryAction;
    assert.equal(pluginLifecycleCatalogView(recoveryEventDrift).integrityOk, false);
  }

  const zeroSequenceDrift = structuredClone(lifecycle);
  zeroSequenceDrift.targets.find((item) => item.id === pack.id).history[0].sequence_no = 0;
  assert.equal(pluginLifecycleCatalogView(zeroSequenceDrift).integrityOk, false);
});

test("lifecycle catalog fails closed on a user-decision contribution", () => {
  const { lifecycle } = fixture();
  lifecycle.plugin_registry.ui_contributions[0].slot_id = "core.user_decision/v1";
  const view = pluginLifecycleCatalogView(lifecycle);

  assert.equal(view.integrityOk, false);
  assert.match(view.errors.join("\n"), /最终决定区禁止插件贡献/);
});

test("preview and transition builders preserve the exact server contract", () => {
  const { lifecycle } = fixture();
  const view = pluginLifecycleCatalogView(lifecycle);
  const lifecycleTarget = view.capabilityPacks[0].lifecycle;
  const previewRequest = buildPluginLifecyclePreviewRequest(lifecycleTarget, "disable");
  const previewPayload = {
    version: "plugin_lifecycle_impact_preview_v1",
    target: previewRequest.target,
    action: "disable",
    expected_head_sequence: lifecycleTarget.headSequence,
    expected_head_sha256: lifecycleTarget.headSha256,
    replacement: null,
    current: { catalog_state: "active", activation_state: "enabled", runtime_state: "ready" },
    result: {
      catalog_state: "active",
      activation_state: "disabled",
      resume_activation_state: "disabled",
      runtime_state: "disabled",
      new_bindings_allowed: false,
      runtime_available: false,
    },
    impact: {
      affected_room_count: 1,
      affected_rooms: [{ id: "room_one", title: "项目室" }],
      running_round_count: 0,
      paused_round_count: 1,
      historical_round_count: 2,
      historical_artifact_count: 3,
      workspace_labels: ["项目研究工作区"],
      historical_records_preserved: true,
      automatic_replacement_performed: false,
      data_deletion_performed: false,
      user_final_decision_unaffected: true,
      effective_boundary: "new_binding_and_plugin_action_stop",
    },
    safety,
    preview_sha256: "f".repeat(64),
  };
  const preview = pluginLifecycleImpactPreviewView(previewPayload, {
    target: lifecycleTarget,
    action: "disable",
  });
  const transition = buildPluginLifecycleTransitionRequest({
    target: lifecycleTarget,
    action: "disable",
    preview,
    clientRequestId: "plugin-lifecycle:test-request-0001",
    reason: "暂时停用以完成复核",
  });

  assert.equal(preview.integrityOk, true);
  assert.equal(preview.version, "plugin_lifecycle_impact_preview_v1");
  assert.equal(preview.implementationAvailabilityAttested, false);
  assert.equal(preview.currentImplementationAvailable, null);
  assert.equal(Object.hasOwn(preview.current, "implementation_available"), false);
  assert.deepEqual(Object.keys(previewRequest).sort(), [
    "action", "expected_head_sequence", "expected_head_sha256", "replacement", "target", "version",
  ]);
  assert.equal(transition.version, "plugin_lifecycle_transition_request_v1");
  assert.equal(transition.impact_preview_sha256, preview.previewSha256);
  assert.equal(transition.user_confirmed_history_preserved, true);
  assert.equal(transition.user_confirmed_no_automatic_migration, true);
  assert.equal(Object.hasOwn(transition, "decision"), false);

  const v2Payload = structuredClone(previewPayload);
  v2Payload.version = "plugin_lifecycle_impact_preview_v2";
  v2Payload.current.implementation_available = true;
  const v2 = pluginLifecycleImpactPreviewView(v2Payload, {
    target: lifecycleTarget,
    action: "disable",
  });
  assert.equal(v2.integrityOk, true);
  assert.equal(v2.implementationAvailabilityAttested, true);
  assert.equal(v2.currentImplementationAvailable, true);
  assert.equal(v2.current.implementation_available, true);

  const missingV2Field = structuredClone(v2Payload);
  delete missingV2Field.current.implementation_available;
  assert.equal(pluginLifecycleImpactPreviewView(missingV2Field, {
    target: lifecycleTarget,
    action: "disable",
  }).integrityOk, false);

  const driftedV2Field = structuredClone(v2Payload);
  driftedV2Field.current.implementation_available = false;
  assert.equal(pluginLifecycleImpactPreviewView(driftedV2Field, {
    target: lifecycleTarget,
    action: "disable",
  }).integrityOk, false);
});

test("transition result parser keeps lifecycle state separate from user decisions", () => {
  const { lifecycle } = fixture();
  const view = pluginLifecycleCatalogView(lifecycle);
  const original = view.capabilityPacks[0].lifecycle;
  const changed = structuredClone(lifecycle.targets[0]);
  changed.activation_state = "disabled";
  changed.runtime_state = "disabled";
  changed.runtime_available = false;
  changed.new_bindings_allowed = false;
  changed.head_sequence = 1;
  changed.head_sha256 = "1".repeat(64);
  changed.current_event_id = "event_one";
  changed.current_event_sha256 = "2".repeat(64);
  changed.available_actions = ["enable", "quarantine", "deprecate", "tombstone"];
  delete changed.replacement_status;
  const payload = {
    version: "plugin_lifecycle_transition_result_v1",
    event: {
      version: "plugin_lifecycle_event_v2",
      id: "event_one",
      sequence_no: 1,
      action: "disable",
      effective_replacement: null,
      implementation_available_at_event: true,
      event_sha256: "2".repeat(64),
    },
    target: changed,
    safety,
  };
  const result = pluginLifecycleTransitionResultView(payload, {
    target: original,
    action: "disable",
  });

  assert.equal(result.integrityOk, true);
  assert.equal(result.target.runtimeAvailable, false);
  assert.equal(result.target.runtimeState, "disabled");
  assert.equal(result.event.implementationAvailabilityAttested, true);

  const implementationMissingPayload = structuredClone(payload);
  implementationMissingPayload.target.implementation_available = false;
  implementationMissingPayload.target.available_actions = ["quarantine", "deprecate", "tombstone"];
  implementationMissingPayload.event.implementation_available_at_event = false;
  assert.equal(pluginLifecycleTransitionResultView(implementationMissingPayload, {
    target: original,
    action: "disable",
  }).integrityOk, true);

  for (const recoveryAction of ["enable", "clear_quarantine", "reinstate"]) {
    const recoveryEventDrift = structuredClone(implementationMissingPayload);
    recoveryEventDrift.event.action = recoveryAction;
    assert.equal(pluginLifecycleTransitionResultView(recoveryEventDrift, {
      target: original,
      action: recoveryAction,
    }).integrityOk, false);
  }

  const zeroSequenceDrift = structuredClone(payload);
  zeroSequenceDrift.event.sequence_no = 0;
  assert.equal(pluginLifecycleTransitionResultView(zeroSequenceDrift, {
    target: original,
    action: "disable",
  }).integrityOk, false);

  const legacyPayload = structuredClone(payload);
  legacyPayload.event.version = "plugin_lifecycle_event_v1";
  legacyPayload.event.implementation_available_at_event = false;
  const legacy = pluginLifecycleTransitionResultView(legacyPayload, {
    target: original,
    action: "disable",
  });
  assert.equal(legacy.integrityOk, true);
  assert.equal(legacy.event.implementationAvailableAtEvent, true);
  assert.equal(legacy.event.implementation_available_at_event, true);
  assert.equal(legacy.event.implementationAvailabilityAttested, false);

  const missingV2Field = structuredClone(payload);
  delete missingV2Field.event.implementation_available_at_event;
  assert.equal(pluginLifecycleTransitionResultView(missingV2Field, {
    target: original,
    action: "disable",
  }).integrityOk, false);

  const replacementDrift = structuredClone(payload);
  replacementDrift.event.effective_replacement = {
    kind: original.kind,
    id: original.id,
    version: "2.0.0",
    sha256: "6".repeat(64),
  };
  assert.equal(pluginLifecycleTransitionResultView(replacementDrift, {
    target: original,
    action: "disable",
  }).integrityOk, false);
});

test("App and host surfaces wire lifecycle without disabling the final decision", () => {
  const app = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
  const settings = readFileSync(new URL("../src/components/RoomSettingsDialog.jsx", import.meta.url), "utf8");
  const create = readFileSync(new URL("../src/components/Dialogs.jsx", import.meta.url), "utf8");
  const artifact = readFileSync(new URL("../src/components/ArtifactDialog.jsx", import.meta.url), "utf8");
  const inspector = readFileSync(new URL("../src/components/RoomInspector.jsx", import.meta.url), "utf8");
  const lifecyclePanel = readFileSync(new URL("../src/components/CapabilityPackLifecyclePanel.jsx", import.meta.url), "utf8");

  assert.match(app, /setPluginLifecycle\(Object\.hasOwn\(data, "plugin_lifecycle"\)/);
  assert.match(app, /onPreviewPluginLifecycle=\{previewPluginLifecycle\}/);
  assert.match(settings, /<CapabilityPackLifecyclePanel/);
  assert.match(settings, /disabled=\{!availability\.canToggle\}/);
  assert.match(create, /disabled=\{creationBlocked\}/);
  assert.match(artifact, /pluginLifecycle,\s*open/);
  assert.ok(artifact.indexOf("<CandidateExperimentPanel") < artifact.indexOf("<UserFinalDecisionSection"));
  assert.doesNotMatch(artifact.slice(artifact.indexOf("<UserFinalDecisionSection")), /readOnly=\{storageWorkspaceReadOnly\}/);
  assert.match(inspector, /pluginLifecycle,\s*members/);
  assert.match(lifecyclePanel, /替代声明/);
  assert.match(lifecyclePanel, /不会自动迁移/);
  assert.match(lifecyclePanel, /view\.replacementDeclarations/);
});
