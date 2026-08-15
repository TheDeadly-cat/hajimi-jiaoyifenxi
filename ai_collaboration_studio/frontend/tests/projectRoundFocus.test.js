import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  buildProjectRoundFocusAuthorization,
  normalizeProjectRoundFocusAuthorization,
  normalizeProjectRoundFocusResponse,
  projectRoundFocusArtifactFingerprint,
  projectRoundFocusAuthorizationState,
  projectRoundFocusCardSource,
  projectRoundFocusLoadPlan,
  projectRoundFocusRoomContextFingerprint,
} from "../src/projectRoundFocus.js";

const safety = Object.freeze({
  execution_capability: "none",
  live_trading_allowed: false,
  can_autonomously_decide: false,
  can_replace_user_decision: false,
  arbitrary_code_loading_allowed: false,
  user_final_decision_required: true,
});

function focusFixture({ active = true, pending = false } = {}) {
  const registryHash = "1".repeat(64);
  const adapterHash = "2".repeat(64);
  const portHash = "3".repeat(64);
  const contributionHash = "4".repeat(64);
  const inputHash = "5".repeat(64);
  const outputHash = "6".repeat(64);
  const viewModelHash = "7".repeat(64);
  const port = {
    port_id: "core.round.context/v1",
    port_version: "1.0.0",
    contract_sha256: portHash,
    handler_method: "project_round_context",
    cardinality: "multiple",
    input_schema_version: "project_round_context_input_v1",
    input_schema_sha256: inputHash,
    output_schema_version: "project_round_context_output_v1",
    output_schema_sha256: outputHash,
    read_surfaces: ["artifact.projection.sealed", "room.round_focus.safe_context"],
    local_write_surfaces: [],
    provider_call_budget: 0,
    market_read_budget: 0,
    business_write_budget: 0,
    external_write_allowed: false,
    failure_policy: "fail_closed",
    ...safety,
  };
  const binding = {
    adapter_id: "project_round_focus",
    adapter_version: "1.0.0",
    adapter_contract_sha256: adapterHash,
    port_id: port.port_id,
    port_version: port.port_version,
    port_contract_sha256: portHash,
    handler_method: port.handler_method,
    input_schema_version: port.input_schema_version,
    input_schema_sha256: inputHash,
    output_schema_version: port.output_schema_version,
    output_schema_sha256: outputHash,
    provider_call_budget: 0,
    market_read_budget: 0,
    business_write_budget: 0,
    failure_policy: "fail_closed",
  };
  const sourcePort = {
    owner_pack_id: "project_round_focus",
    port_id: port.port_id,
    requirement: "required",
    cardinality: "one",
  };
  const sourcePortResolution = {
    owner_pack_id: "project_round_focus",
    port_id: port.port_id,
    port_version: port.port_version,
    port_contract_sha256: portHash,
    output_schema_version: port.output_schema_version,
    output_schema_sha256: outputHash,
  };
  const frozenContribution = {
    contribution_id: "project_round_focus.room_inspector/v1",
    contribution_version: "1.0.0",
    contract_sha256: contributionHash,
    slot_id: "core.room.inspector/v1",
    component_key: "project_round_focus",
    label: "Project next-round focus",
    order: 260,
    status: "ready",
    contract_version: "ui_contribution_contract_v2",
    source_port: sourcePort,
    view_model: {
      schema_version: "project_round_focus_view_model_v1",
      schema_sha256: viewModelHash,
    },
    source_port_resolution: sourcePortResolution,
  };
  const snapshot = {
    version: "plugin_registry_snapshot_v2",
    registry_snapshot_sha256: registryHash,
    selected_capability_pack_ids: ["project_round_focus"],
    capability_packs: [{
      id: "project_round_focus",
      domain_adapter_ids: ["project_round_focus"],
      domain_adapter_port_requirements: [{
        port_id: port.port_id,
        requirement: "required",
        cardinality: "one",
        version_range: ">=1.0.0 <2.0.0",
      }],
    }],
    domain_adapters: [{
      contract_version: "domain_adapter_contract_v2",
      adapter_id: "project_round_focus",
      adapter_version: "1.0.0",
      contract_sha256: adapterHash,
      status: "ready",
      ports: [port],
    }],
    ui_contributions: [frozenContribution],
    port_resolutions: [{
      owner_pack_id: "project_round_focus",
      port_id: port.port_id,
      requirement: "required",
      cardinality: "one",
      version_range: ">=1.0.0 <2.0.0",
      resolved: [binding],
    }],
  };
  const room = {
    id: "room_project",
    objective: "Repair the current project gaps",
    workflow_policy: { version: 1, stage_order: ["facilitate", "decision"] },
    plugin_lifecycle_current: { current_head_set_sha256: "d".repeat(64) },
    capability_pack_ids: ["project_round_focus"],
    plugin_registry_integrity_ok: true,
    plugin_registry_snapshot_sha256: registryHash,
    plugin_registry_snapshot: snapshot,
  };
  const pendingRound = pending ? {
    id: "round_frozen",
    capability_pack_ids: ["project_round_focus"],
    plugin_registry_integrity_ok: true,
    plugin_registry_snapshot_sha256: registryHash,
    plugin_registry_snapshot: structuredClone(snapshot),
  } : null;
  const contribution = {
    id: frozenContribution.contribution_id,
    version: frozenContribution.contribution_version,
    contractHash: contributionHash,
    slotId: frozenContribution.slot_id,
    componentKey: frozenContribution.component_key,
    packId: "project_round_focus",
    present: true,
    active,
    readOnly: !active,
    contractVersion: "ui_contribution_contract_v2",
    sourcePort: {
      ownerPackId: sourcePort.owner_pack_id,
      portId: sourcePort.port_id,
      requirement: sourcePort.requirement,
      cardinality: sourcePort.cardinality,
    },
    sourcePortResolution: {
      ownerPackId: sourcePortResolution.owner_pack_id,
      portId: sourcePortResolution.port_id,
      portVersion: sourcePortResolution.port_version,
      portContractHash: sourcePortResolution.port_contract_sha256,
      outputSchemaVersion: sourcePortResolution.output_schema_version,
      outputSchemaHash: sourcePortResolution.output_schema_sha256,
    },
    viewModel: {
      schemaVersion: "project_round_focus_view_model_v1",
      schemaHash: viewModelHash,
    },
    declaredActions: ["project_round_focus.inspect"],
    allowedActions: active ? ["project_round_focus.inspect"] : [],
  };
  const slot = { integrityOk: true, reason: "" };
  return { room, pendingRound, contribution, slot, snapshot };
}

function responseFor(expected, { record = false, bootstrap = false } = {}) {
  const focusItems = bootstrap ? [] : [
    {
      sequence_no: 1,
      category: "blocker",
      code: "BLOCKER_ONE",
      item_key: "risk.one",
      message: "先解除阻断条件",
      target_capabilities: ["critical_review"],
    },
    {
      sequence_no: 2,
      category: "evidence",
      code: "EVIDENCE_ONE",
      item_key: "evidence.one",
      message: "补齐证据关系",
      target_capabilities: ["evidence_review"],
    },
    {
      sequence_no: 3,
      category: "structural",
      code: "STRUCTURE_ONE",
      item_key: "structure.one",
      message: "补齐方案结构",
      target_capabilities: ["evidence_review", "decision_synthesis"],
    },
  ];
  const raw = {
    version: record ? "project_round_focus_record_v1" : "project_round_focus_preview_v1",
    integrity_ok: true,
    metrics_visible: true,
    room_id: expected.roomId,
    artifact_binding: bootstrap ? {
      status: "none",
      artifact_id: "",
      artifact_title: "",
      artifact_version: 0,
      artifact_snapshot_sha256: "",
      evidence_review_event_sha256: "",
      evidence_graph_sha256: "",
    } : {
      status: "exact",
      artifact_id: "artifact_project",
      artifact_title: "项目结论",
      artifact_version: 3,
      artifact_snapshot_sha256: "8".repeat(64),
      evidence_review_event_sha256: "9".repeat(64),
      evidence_graph_sha256: "a".repeat(64),
    },
    plugin_registry_snapshot_sha256: expected.pluginRegistrySnapshotSha256,
    input_seal_sha256: "b".repeat(64),
    resolution: {
      version: "project_round_focus_resolution_v1",
      contribution: { ...expected.resolution.contribution },
      adapter: { ...expected.resolution.adapter },
      port: {
        port_id: expected.resolution.port.port_id,
        port_version: expected.resolution.port.port_version,
        contract_sha256: expected.resolution.port.contract_sha256,
        input_schema_version: expected.resolution.port.input_schema_version,
        input_schema_sha256: expected.resolution.port.input_schema_sha256,
        output_schema_version: expected.resolution.port.output_schema_version,
        output_schema_sha256: expected.resolution.port.output_schema_sha256,
        provider_call_budget: 0,
        market_read_budget: 0,
        business_write_budget: 0,
        failure_policy: "fail_closed",
      },
    },
    state: bootstrap ? "bootstrap" : "blocked",
    counts: {
      structural_gap_count: bootstrap ? 0 : 1,
      blocker_count: bootstrap ? 0 : 1,
      evidence_gap_count: bootstrap ? 0 : 1,
      focus_item_count: focusItems.length,
    },
    focus_items: focusItems,
    suggested_objective: bootstrap ? "继续项目研究" : "补齐已冻结的三个项目缺口",
    preview_sha256: "c".repeat(64),
    provider_calls_performed: 0,
    market_reads_performed: 0,
    adapter_business_writes_performed: 0,
    host_lineage_write_required: true,
    execution_capability: "none",
    live_trading_allowed: false,
    can_autonomously_decide: false,
    can_replace_user_decision: false,
    arbitrary_code_loading_allowed: false,
    ranking_produced: false,
    winner_claim: false,
    approval_produced: false,
    member_assignment_produced: false,
    workflow_mutation_performed: false,
    user_final_decision_required: true,
    ...(record ? {
      round_id: expected.roundId,
      frozen_at: "2026-08-10T08:00:00.000Z",
      runtime_available: false,
    } : {}),
  };
  return { ok: true, project_round_focus: raw };
}

test("loads only the exact active v2 preview and fails closed for v1 or port drift", () => {
  const value = focusFixture();
  const plan = projectRoundFocusLoadPlan(value);
  assert.equal(plan.shouldLoad, true);
  assert.equal(plan.requestKind, "preview");
  assert.equal(plan.expected.resolution.adapter.adapter_id, "project_round_focus");
  assert.equal(plan.expected.resolution.port.handler_method, "project_round_context");

  const legacy = focusFixture();
  legacy.room.plugin_registry_snapshot.version = "plugin_registry_snapshot_v1";
  assert.equal(projectRoundFocusLoadPlan(legacy).shouldLoad, false);

  const drifted = focusFixture();
  drifted.snapshot.port_resolutions[0].resolved[0].handler_method = "evil";
  assert.equal(projectRoundFocusLoadPlan(drifted).shouldLoad, false);
});

test("preview request identity follows confirmed-artifact drift while frozen record identity does not", () => {
  const preview = focusFixture();
  const firstPreview = projectRoundFocusLoadPlan({
    ...preview,
    artifactFingerprint: "artifact_project:1:confirmed",
  });
  const nextPreview = projectRoundFocusLoadPlan({
    ...preview,
    artifactFingerprint: "artifact_project:2:confirmed",
  });
  assert.notEqual(firstPreview.requestKey, nextPreview.requestKey);

  const history = focusFixture({ active: false, pending: true });
  const firstRecord = projectRoundFocusLoadPlan({
    ...history,
    artifactFingerprint: "artifact_project:1:confirmed",
  });
  const nextRecord = projectRoundFocusLoadPlan({
    ...history,
    artifactFingerprint: "artifact_project:2:confirmed",
  });
  assert.equal(firstRecord.requestKey, nextRecord.requestKey);
});

test("preview authorization follows every sealed room-context input while a frozen record does not", () => {
  const value = focusFixture();
  const members = [{
    id: "member_one",
    version: 1,
    position: 1,
    enabled: true,
    capabilities: ["evidence_review"],
  }];
  const fingerprint = projectRoundFocusRoomContextFingerprint({ room: value.room, members });
  assert.equal(fingerprint, projectRoundFocusRoomContextFingerprint({
    room: {
      ...value.room,
      workflow_policy: { stage_order: ["facilitate", "decision"], version: 1 },
    },
    members: [{
      id: "member_two",
      version: 1,
      position: 2,
      enabled: false,
      capabilities: ["critical_review"],
    }, ...members],
  }));
  const first = projectRoundFocusLoadPlan({ ...value, roomContextFingerprint: fingerprint });

  const objectiveFingerprint = projectRoundFocusRoomContextFingerprint({
    room: { ...value.room, objective: "A changed room objective" },
    members,
  });
  const workflowFingerprint = projectRoundFocusRoomContextFingerprint({
    room: { ...value.room, workflow_policy: { version: 1, stage_order: ["decision"] } },
    members,
  });
  const memberFingerprint = projectRoundFocusRoomContextFingerprint({
    room: value.room,
    members: [{ ...members[0], version: 2, capabilities: ["critical_review"] }],
  });
  const lifecycleFingerprint = projectRoundFocusRoomContextFingerprint({
    room: {
      ...value.room,
      plugin_lifecycle_current: { current_head_set_sha256: "e".repeat(64) },
    },
    members,
  });
  for (const drifted of [
    objectiveFingerprint,
    workflowFingerprint,
    memberFingerprint,
    lifecycleFingerprint,
  ]) {
    assert.notEqual(fingerprint, drifted);
    assert.notEqual(
      first.requestKey,
      projectRoundFocusLoadPlan({ ...value, roomContextFingerprint: drifted }).requestKey,
    );
  }

  const request = buildProjectRoundFocusAuthorization(
    normalizeProjectRoundFocusResponse(responseFor(first.expected), first.expected),
  );
  const authorization = {
    roomId: value.room.id,
    artifactFingerprint: "artifact_project:3:confirmed",
    roomContextFingerprint: fingerprint,
    pluginRegistrySnapshotSha256: value.room.plugin_registry_snapshot_sha256,
    request,
  };
  assert.equal(projectRoundFocusAuthorizationState(authorization, {
    roomId: value.room.id,
    artifactFingerprint: authorization.artifactFingerprint,
    roomContextFingerprint: fingerprint,
    pluginRegistrySnapshotSha256: value.room.plugin_registry_snapshot_sha256,
  }).valid, true);
  assert.equal(projectRoundFocusAuthorizationState(authorization, {
    roomId: value.room.id,
    artifactFingerprint: authorization.artifactFingerprint,
    roomContextFingerprint: lifecycleFingerprint,
    pluginRegistrySnapshotSha256: value.room.plugin_registry_snapshot_sha256,
  }).valid, false);

  const history = focusFixture({ active: false, pending: true });
  const frozenFirst = projectRoundFocusLoadPlan({
    ...history,
    roomContextFingerprint: fingerprint,
  });
  const frozenAfterCurrentDrift = projectRoundFocusLoadPlan({
    ...history,
    roomContextFingerprint: lifecycleFingerprint,
  });
  assert.equal(frozenFirst.requestKey, frozenAfterCurrentDrift.requestKey);
});

test("loads a frozen historical record even when the current contribution is inactive", () => {
  const value = focusFixture({ active: false, pending: true });
  const plan = projectRoundFocusLoadPlan(value);

  assert.equal(plan.shouldLoad, true);
  assert.equal(plan.requestKind, "record");
  assert.equal(plan.expected.roundId, "round_frozen");
  const view = normalizeProjectRoundFocusResponse(
    responseFor(plan.expected, { record: true }),
    plan.expected,
  );
  assert.equal(view.valid, true);
  assert.equal(view.kind, "record");
  assert.equal(view.runtimeAvailable, false);
});

test("accepts the exact closed preview and hides every metric after any safety or item drift", () => {
  const plan = projectRoundFocusLoadPlan(focusFixture());
  const payload = responseFor(plan.expected);
  const view = normalizeProjectRoundFocusResponse(payload, plan.expected);
  assert.equal(view.valid, true);
  assert.equal(view.metricsVisible, true);
  assert.deepEqual(view.counts, {
    structuralGapCount: 1,
    blockerCount: 1,
    evidenceGapCount: 1,
    focusItemCount: 3,
  });

  const reversedCapabilities = structuredClone(payload);
  reversedCapabilities.project_round_focus.focus_items[2].target_capabilities.reverse();
  const reversed = normalizeProjectRoundFocusResponse(reversedCapabilities, plan.expected);
  assert.equal(reversed.valid, false);
  assert.equal(reversed.metricsVisible, false);
  assert.deepEqual(reversed.focusItems, []);

  const unsafe = structuredClone(payload);
  unsafe.project_round_focus.member_assignment_produced = true;
  const unsafeView = normalizeProjectRoundFocusResponse(unsafe, plan.expected);
  assert.equal(unsafeView.valid, false);
  assert.equal(unsafeView.metricsVisible, false);
});

test("bootstrap never fabricates gaps and authorization is a closed source seal independent of objective edits", () => {
  const value = focusFixture();
  const plan = projectRoundFocusLoadPlan(value);
  const view = normalizeProjectRoundFocusResponse(
    responseFor(plan.expected, { bootstrap: true }),
    plan.expected,
  );
  assert.equal(view.valid, true);
  assert.equal(view.state, "bootstrap");
  assert.deepEqual(view.focusItems, []);

  const request = buildProjectRoundFocusAuthorization(view);
  assert.deepEqual(request.artifact_binding, { status: "none" });
  assert.equal(normalizeProjectRoundFocusAuthorization({ ...request, objective: "not allowed" }).valid, false);
  const authorization = {
    roomId: value.room.id,
    artifactFingerprint: "artifact_project:3:confirmed",
    roomContextFingerprint: projectRoundFocusRoomContextFingerprint({ room: value.room, members: [] }),
    pluginRegistrySnapshotSha256: value.room.plugin_registry_snapshot_sha256,
    request,
  };
  assert.equal(projectRoundFocusAuthorizationState(authorization, {
    roomId: value.room.id,
    artifactFingerprint: authorization.artifactFingerprint,
    roomContextFingerprint: authorization.roomContextFingerprint,
    pluginRegistrySnapshotSha256: value.room.plugin_registry_snapshot_sha256,
    objective: "用户自由改写后的目标",
  }).valid, true);
});

test("source selection prefers a frozen record and artifact drift changes the client authorization fingerprint", () => {
  const value = focusFixture({ pending: true });
  const previewPlan = projectRoundFocusLoadPlan({ ...value, pendingRound: null });
  const recordPlan = projectRoundFocusLoadPlan(value);
  const preview = normalizeProjectRoundFocusResponse(responseFor(previewPlan.expected), previewPlan.expected);
  const record = normalizeProjectRoundFocusResponse(
    responseFor(recordPlan.expected, { record: true }),
    recordPlan.expected,
  );

  assert.equal(projectRoundFocusCardSource({ preview, record }).kind, "record");
  assert.notEqual(
    projectRoundFocusArtifactFingerprint([{ id: "a", version: 1, status: "confirmed" }]),
    projectRoundFocusArtifactFingerprint([{ id: "a", version: 2, status: "confirmed" }]),
  );
});

test("the host card caps visible focus rows and fills only an editable, explicitly sealed objective", () => {
  const cardSource = readFileSync(
    new URL("../src/components/ProjectRoundFocusCard.jsx", import.meta.url),
    "utf8",
  );
  const dialogSource = readFileSync(
    new URL("../src/components/RoundLaunchDialog.jsx", import.meta.url),
    "utf8",
  );
  const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
  const changeComposerSource = appSource.slice(
    appSource.indexOf("const changeComposer"),
    appSource.indexOf("const fillRoundFocusObjective"),
  );
  const providerPreflightSource = appSource.slice(
    appSource.indexOf("const runProviderPreflight"),
    appSource.indexOf("const routeEnabledMembers"),
  );

  assert.match(cardSource, /focusItems\.slice\(0, 3\)/);
  assert.doesNotMatch(cardSource, /<details/);
  assert.match(cardSource, /pluginRegistrySnapshotSha256: view\.pluginRegistrySnapshotSha256/);
  assert.match(cardSource, /roomContextFingerprint,/);
  assert.match(cardSource, /controller\.abort\(\)/);
  assert.match(cardSource, /\[plan\.requestKey\]/);
  assert.match(cardSource, /不自动开始、不点名成员、不改变流程或用户最终决定/);
  assert.match(dialogSource, /冻结的项目焦点上下文/);
  assert.match(dialogSource, /只用于预填可编辑目标/);
  assert.match(dialogSource, /不会自动开始、点名成员或替代你的最终决定/);
  assert.match(appSource, /round_context_authorizations: context\.roundContextAuthorizations/);
  assert.doesNotMatch(appSource, /project_round_focus_authorization: context\.projectRoundFocusAuthorization/);
  assert.match(appSource, /roomContextMatches/);
  assert.doesNotMatch(changeComposerSource, /setRoundFocusAuthorization/);
  assert.doesNotMatch(providerPreflightSource, /activeRoundFocusAuthorization/);
  assert.doesNotMatch(providerPreflightSource, /project_round_focus_authorization/);
  assert.doesNotMatch(providerPreflightSource, /焦点授权.*throw|throw.*焦点授权/);
});
