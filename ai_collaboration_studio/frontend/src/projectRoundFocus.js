import {
  frozenPortResolution,
  HOST_CONTRIBUTION_IDS,
  HOST_SLOT_IDS,
  PLUGIN_REGISTRY_SNAPSHOT_VERSION_V1,
  PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2,
  PROJECT_ROUND_FOCUS_VIEW_MODEL_SCHEMA_VERSION,
} from "./capabilityContributions.js";

export const PROJECT_ROUND_FOCUS_PREVIEW_VERSION = "project_round_focus_preview_v1";
export const PROJECT_ROUND_FOCUS_RECORD_VERSION = "project_round_focus_record_v1";
export const PROJECT_ROUND_FOCUS_RESOLUTION_VERSION = "project_round_focus_resolution_v1";
export const PROJECT_ROUND_FOCUS_AUTHORIZATION_VERSION = "project_round_focus_authorization_v1";
export const PROJECT_ROUND_FOCUS_INSPECT_ACTION = "project_round_focus.inspect";

const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;
const MAX_FOCUS_ITEMS = 16;
const FOCUS_CATEGORIES = new Set(["structural", "evidence", "blocker"]);
const TARGET_CAPABILITIES_BY_CATEGORY = Object.freeze({
  blocker: Object.freeze(["critical_review"]),
  evidence: Object.freeze(["evidence_review"]),
  structural: Object.freeze(["evidence_review", "decision_synthesis"]),
});
const PREVIEW_FIELDS = Object.freeze([
  "version",
  "integrity_ok",
  "metrics_visible",
  "room_id",
  "artifact_binding",
  "plugin_registry_snapshot_sha256",
  "resolution",
  "state",
  "counts",
  "focus_items",
  "suggested_objective",
  "preview_sha256",
  "input_seal_sha256",
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
]);
const RECORD_FIELDS = Object.freeze([
  ...PREVIEW_FIELDS,
  "round_id",
  "frozen_at",
  "runtime_available",
]);

function record(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function array(value) {
  return Array.isArray(value) ? value : [];
}

function text(value, maximum = 4000) {
  return typeof value === "string" ? value.trim().slice(0, maximum) : "";
}

function sha256(value) {
  const normalized = text(value, 64).toLowerCase();
  return SHA256_PATTERN.test(normalized) ? normalized : "";
}

function fingerprint(value) {
  return typeof value === "string" ? value : "";
}

function nonNegativeInteger(value, maximum = 10000) {
  return Number.isSafeInteger(value) && value >= 0 && value <= maximum ? value : null;
}

function positiveInteger(value, maximum = Number.MAX_SAFE_INTEGER) {
  return Number.isSafeInteger(value) && value > 0 && value <= maximum ? value : null;
}

function exactKeys(value, expected) {
  const keys = Object.keys(record(value)).sort();
  const wanted = [...expected].sort();
  return keys.length === wanted.length && keys.every((key, index) => key === wanted[index]);
}

function exactStringSet(value, expected) {
  const actual = array(value).map((item) => text(item, 160)).filter(Boolean);
  return actual.length === expected.length
    && new Set(actual).size === actual.length
    && expected.every((item) => actual.includes(item));
}

function exactStringSequence(value, expected) {
  const actual = array(value).map((item) => text(item, 160)).filter(Boolean);
  return actual.length === expected.length
    && actual.every((item, index) => item === expected[index]);
}

function sameReference(actual, expected, fields) {
  return fields.every((field) => text(actual?.[field], 500).toLowerCase()
    === text(expected?.[field], 500).toLowerCase());
}

function contributionReference(value) {
  const raw = record(value);
  return {
    contribution_id: text(raw.contribution_id, 160),
    contribution_version: text(raw.contribution_version, 80),
    contract_sha256: sha256(raw.contract_sha256),
    slot_id: text(raw.slot_id, 160),
    component_key: text(raw.component_key, 160),
  };
}

function adapterReference(value) {
  const raw = record(value);
  return {
    adapter_id: text(raw.adapter_id, 160),
    adapter_version: text(raw.adapter_version, 80),
    contract_version: text(raw.contract_version, 80),
    contract_sha256: sha256(raw.contract_sha256),
  };
}

function portReference(value) {
  const raw = record(value);
  return {
    port_id: text(raw.port_id, 160),
    port_version: text(raw.port_version, 80),
    contract_sha256: sha256(raw.contract_sha256),
    input_schema_version: text(raw.input_schema_version, 160),
    input_schema_sha256: sha256(raw.input_schema_sha256),
    output_schema_version: text(raw.output_schema_version, 160),
    output_schema_sha256: sha256(raw.output_schema_sha256),
    provider_call_budget: raw.provider_call_budget,
    market_read_budget: raw.market_read_budget,
    business_write_budget: raw.business_write_budget,
    failure_policy: text(raw.failure_policy, 80),
  };
}

function artifactBinding(value, issues = null) {
  const raw = record(value);
  const status = text(raw.status, 20);
  const expectedKeys = [
    "status",
    "artifact_id",
    "artifact_title",
    "artifact_version",
    "artifact_snapshot_sha256",
    "evidence_review_event_sha256",
    "evidence_graph_sha256",
  ];
  const binding = {
    status,
    artifactId: text(raw.artifact_id, 160),
    artifactTitle: text(raw.artifact_title, 500),
    artifactVersion: nonNegativeInteger(raw.artifact_version, Number.MAX_SAFE_INTEGER),
    artifactSnapshotSha256: sha256(raw.artifact_snapshot_sha256),
    evidenceReviewEventSha256: sha256(raw.evidence_review_event_sha256),
    evidenceGraphSha256: sha256(raw.evidence_graph_sha256),
  };
  const noneReady = status === "none"
    && binding.artifactId === ""
    && binding.artifactTitle === ""
    && binding.artifactVersion === 0
    && text(raw.artifact_snapshot_sha256, 64) === ""
    && text(raw.evidence_review_event_sha256, 64) === ""
    && text(raw.evidence_graph_sha256, 64) === "";
  const exactReady = status === "exact"
    && ID_PATTERN.test(binding.artifactId)
    && positiveInteger(binding.artifactVersion)
    && Boolean(binding.artifactSnapshotSha256)
    && Boolean(binding.evidenceReviewEventSha256)
    && Boolean(binding.evidenceGraphSha256);
  if (!exactKeys(raw, expectedKeys) || (!noneReady && !exactReady)) {
    issues?.push("ARTIFACT_BINDING_INVALID");
  }
  return binding;
}

function exactFrozenResolution(snapshot, contribution) {
  const frozenRows = array(snapshot.ui_contributions).filter(
    (item) => text(item?.contribution_id, 160)
      === HOST_CONTRIBUTION_IDS.projectRoundFocusRoomInspector,
  );
  if (frozenRows.length !== 1) return null;
  const frozen = record(frozenRows[0]);
  const frozenContribution = contributionReference(frozen);
  const expectedContribution = {
    contribution_id: contribution?.id,
    contribution_version: contribution?.version,
    contract_sha256: contribution?.contractHash,
    slot_id: HOST_SLOT_IDS.roomInspector,
    component_key: "project_round_focus",
  };
  if (!sameReference(frozenContribution, expectedContribution, [
    "contribution_id",
    "contribution_version",
    "contract_sha256",
    "slot_id",
    "component_key",
  ])) return null;
  const sourcePort = record(contribution?.sourcePort);
  const sourceResolution = record(contribution?.sourcePortResolution);
  const viewModel = record(contribution?.viewModel);
  if (
    contribution?.contractVersion !== "ui_contribution_contract_v2"
    || frozen.contract_version !== contribution.contractVersion
    || text(frozen.source_port?.owner_pack_id, 160) !== text(sourcePort.ownerPackId, 160)
    || text(frozen.source_port?.port_id, 160) !== text(sourcePort.portId, 160)
    || frozen.source_port?.requirement !== sourcePort.requirement
    || frozen.source_port?.cardinality !== sourcePort.cardinality
    || text(frozen.view_model?.schema_version, 160) !== text(viewModel.schemaVersion, 160)
    || sha256(frozen.view_model?.schema_sha256) !== sha256(viewModel.schemaHash)
    || text(frozen.source_port_resolution?.owner_pack_id, 160)
      !== text(sourceResolution.ownerPackId, 160)
    || text(frozen.source_port_resolution?.port_id, 160)
      !== text(sourceResolution.portId, 160)
    || text(frozen.source_port_resolution?.port_version, 80)
      !== text(sourceResolution.portVersion, 80)
    || sha256(frozen.source_port_resolution?.port_contract_sha256)
      !== sha256(sourceResolution.portContractHash)
    || text(frozen.source_port_resolution?.output_schema_version, 160)
      !== text(sourceResolution.outputSchemaVersion, 160)
    || sha256(frozen.source_port_resolution?.output_schema_sha256)
      !== sha256(sourceResolution.outputSchemaHash)
    || viewModel.schemaVersion !== PROJECT_ROUND_FOCUS_VIEW_MODEL_SCHEMA_VERSION
    || !sha256(viewModel.schemaHash)
  ) return null;
  const packId = text(sourcePort.ownerPackId, 160);
  const packs = array(snapshot.capability_packs).filter((item) => text(item?.id, 160) === packId);
  if (packs.length !== 1 || packId !== text(contribution?.packId, 160)) return null;
  const resolution = frozenPortResolution(snapshot, {
    packId,
    portId: text(sourcePort.portId, 160),
  });
  if (
    !resolution
    || resolution.adapter.adapter_id !== "project_round_focus"
    || resolution.adapter.adapter_version !== "1.0.0"
    || resolution.port.port_id !== "core.round.context/v1"
    || resolution.port.port_version !== "1.0.0"
    || resolution.port.input_schema_version !== "project_round_context_input_v1"
    || resolution.port.output_schema_version !== "project_round_context_output_v1"
    || resolution.port.provider_call_budget !== 0
    || resolution.port.market_read_budget !== 0
    || resolution.port.business_write_budget !== 0
    || resolution.port.failure_policy !== "fail_closed"
  ) return null;
  return {
    contribution: frozenContribution,
    adapter: resolution.adapter,
    port: resolution.port,
    viewModel: {
      schema_version: viewModel.schemaVersion,
      schema_sha256: sha256(viewModel.schemaHash),
    },
  };
}

export function projectRoundFocusArtifactFingerprint(artifacts) {
  return array(artifacts)
    .map((artifact) => [
      text(artifact?.id, 160),
      nonNegativeInteger(artifact?.version, Number.MAX_SAFE_INTEGER) ?? -1,
      text(artifact?.status, 40),
    ].join(":"))
    .filter((identity) => !identity.startsWith(":"))
    .sort()
    .join("|");
}

function stableJsonValue(value) {
  if (Array.isArray(value)) return value.map((item) => stableJsonValue(item));
  if (value && typeof value === "object") {
    return Object.keys(value)
      .sort()
      .reduce((result, key) => {
        result[key] = stableJsonValue(value[key]);
        return result;
      }, {});
  }
  if (["string", "number", "boolean"].includes(typeof value) || value === null) {
    return value;
  }
  return null;
}

export function projectRoundFocusRoomContextFingerprint({ room, members } = {}) {
  const roomValue = record(room);
  const enabledMembers = array(members)
    .filter((member) => member?.enabled === true)
    .slice()
    .sort((left, right) => (
      (nonNegativeInteger(left?.position, Number.MAX_SAFE_INTEGER) ?? Number.MAX_SAFE_INTEGER)
        - (nonNegativeInteger(right?.position, Number.MAX_SAFE_INTEGER) ?? Number.MAX_SAFE_INTEGER)
      || text(left?.id, 160).localeCompare(text(right?.id, 160))
    ))
    .map((member) => ({
      id: text(member?.id, 160),
      version: positiveInteger(member?.version, Number.MAX_SAFE_INTEGER) ?? 0,
      capabilities: array(member?.capabilities)
        .map((capability) => text(capability, 160).toLowerCase())
        .filter(Boolean),
    }));
  return JSON.stringify(stableJsonValue({
    objective: text(roomValue.objective, 4000),
    workflow_policy: record(roomValue.workflow_policy),
    enabled_members: enabledMembers,
    plugin_lifecycle_head_set_sha256: sha256(
      roomValue.plugin_lifecycle_current?.current_head_set_sha256,
    ),
  }));
}

export function projectRoundFocusLoadPlan({
  room,
  pendingRound = null,
  slot,
  contribution,
  showLegacyFallback = false,
  artifactFingerprint = "",
  roomContextFingerprint = "",
}) {
  const source = record(pendingRound || room);
  const snapshot = record(source.plugin_registry_snapshot);
  const packSelected = array(source.capability_pack_ids).includes("project_round_focus")
    || array(snapshot.selected_capability_pack_ids).includes("project_round_focus")
    || array(snapshot.capability_packs).some((pack) => pack?.id === "project_round_focus");
  const base = {
    visible: Boolean(contribution?.present || packSelected || showLegacyFallback),
    shouldLoad: false,
    mode: "read_only",
    reason: "",
    requestKind: pendingRound?.id ? "record" : "preview",
    requestKey: "",
    expected: null,
  };
  if (!base.visible) return { ...base, mode: "hidden" };
  if (snapshot.version === PLUGIN_REGISTRY_SNAPSHOT_VERSION_V1) {
    return { ...base, reason: "该冻结上下文使用旧版插件快照，未绑定下一轮焦点端口。" };
  }
  if (snapshot.version !== PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2) {
    return { ...base, reason: "冻结插件快照版本无法识别；不会用当前实现替算。" };
  }
  if (slot?.integrityOk !== true || source.plugin_registry_integrity_ok !== true) {
    return { ...base, reason: slot?.reason || "冻结插件合同完整性无法验证；焦点读取已关闭。" };
  }
  if (
    contribution?.present !== true
    || (base.requestKind === "preview" && contribution?.active !== true)
  ) {
    return { ...base, reason: contribution?.reason || slot?.reason || "下一轮焦点贡献当前不可用，只保留只读说明。" };
  }
  const declaredActions = base.requestKind === "record"
    ? contribution.declaredActions
    : contribution.allowedActions;
  if (
    contribution.id !== HOST_CONTRIBUTION_IDS.projectRoundFocusRoomInspector
    || contribution.slotId !== HOST_SLOT_IDS.roomInspector
    || contribution.componentKey !== "project_round_focus"
    || contribution.contractVersion !== "ui_contribution_contract_v2"
    || contribution.viewModel?.schemaVersion !== PROJECT_ROUND_FOCUS_VIEW_MODEL_SCHEMA_VERSION
    || !sha256(contribution.viewModel?.schemaHash)
    || !sha256(contribution.contractHash)
    || !exactStringSet(declaredActions, [PROJECT_ROUND_FOCUS_INSPECT_ACTION])
  ) {
    return { ...base, reason: "下一轮焦点贡献与宿主允许的精确合同不一致。" };
  }
  const resolution = exactFrozenResolution(snapshot, contribution);
  if (!resolution) {
    return { ...base, reason: "下一轮焦点缺少精确来源端口、adapter 或视图模型绑定。" };
  }
  const roomId = text(room?.id, 160);
  const roundId = pendingRound?.id ? text(pendingRound.id, 160) : "";
  const pluginRegistrySnapshotSha256 = sha256(
    source.plugin_registry_snapshot_sha256 || snapshot.registry_snapshot_sha256,
  );
  if (!roomId || (pendingRound && !roundId) || !pluginRegistrySnapshotSha256) {
    return { ...base, reason: "焦点请求缺少精确房间、轮次或插件封印。" };
  }
  const expected = {
    roomId,
    roundId,
    requestKind: base.requestKind,
    pluginRegistrySnapshotSha256,
    resolution,
  };
  return {
    ...base,
    shouldLoad: true,
    mode: "load",
    reason: "",
    requestKey: [
      base.requestKind,
      roomId,
      roundId,
      pluginRegistrySnapshotSha256,
      resolution.contribution.contract_sha256,
      resolution.adapter.contract_sha256,
      resolution.port.contract_sha256,
      resolution.port.output_schema_sha256,
      resolution.viewModel.schema_sha256,
      ...(base.requestKind === "preview" ? [
        fingerprint(artifactFingerprint),
        fingerprint(roomContextFingerprint),
      ] : []),
    ].join("|"),
    expected,
  };
}

function invalidView(issues) {
  return {
    valid: false,
    integrityOk: false,
    metricsVisible: false,
    issues: [...new Set(issues)],
    kind: "invalid",
    counts: {
      structuralGapCount: 0,
      blockerCount: 0,
      evidenceGapCount: 0,
      focusItemCount: 0,
    },
    focusItems: [],
    suggestedObjective: "",
    artifactBinding: null,
    resolution: {},
  };
}

function normalizeFocusItems(value, issues) {
  if (!Array.isArray(value) || value.length > MAX_FOCUS_ITEMS) {
    issues.push("FOCUS_ITEMS_INVALID");
    return [];
  }
  const identities = new Set();
  return value.map((raw, index) => {
    const item = record(raw);
    const sequenceNo = positiveInteger(item.sequence_no, MAX_FOCUS_ITEMS);
    const category = text(item.category, 40);
    const code = text(item.code, 120);
    const itemKey = text(item.item_key, 240);
    const message = text(item.message, 1000);
    const targetCapabilities = array(item.target_capabilities)
      .map((capability) => text(capability, 160))
      .filter(Boolean);
    const identity = `${sequenceNo}|${category}|${code}|${itemKey}`;
    if (
      !exactKeys(item, [
        "sequence_no",
        "category",
        "code",
        "item_key",
        "message",
        "target_capabilities",
      ])
      || sequenceNo !== index + 1
      || !FOCUS_CATEGORIES.has(category)
      || !code
      || !itemKey
      || !message
      || targetCapabilities.length !== array(item.target_capabilities).length
      || !exactStringSequence(targetCapabilities, TARGET_CAPABILITIES_BY_CATEGORY[category] || [])
      || identities.has(identity)
    ) issues.push(`FOCUS_ITEM_INVALID:${index}`);
    identities.add(identity);
    return { sequenceNo, category, code, itemKey, message, targetCapabilities };
  });
}

function normalizeCounts(value, issues) {
  const raw = record(value);
  const counts = {
    structuralGapCount: nonNegativeInteger(raw.structural_gap_count, MAX_FOCUS_ITEMS),
    blockerCount: nonNegativeInteger(raw.blocker_count, MAX_FOCUS_ITEMS),
    evidenceGapCount: nonNegativeInteger(raw.evidence_gap_count, MAX_FOCUS_ITEMS),
    focusItemCount: nonNegativeInteger(raw.focus_item_count, MAX_FOCUS_ITEMS),
  };
  if (
    !exactKeys(raw, [
      "structural_gap_count",
      "blocker_count",
      "evidence_gap_count",
      "focus_item_count",
    ])
    || Object.values(counts).some((count) => count == null)
  ) issues.push("COUNTS_INVALID");
  return counts;
}

function fixedBoundaryReady(raw) {
  return raw.provider_calls_performed === 0
    && raw.market_reads_performed === 0
    && raw.adapter_business_writes_performed === 0
    && raw.host_lineage_write_required === true
    && raw.execution_capability === "none"
    && raw.live_trading_allowed === false
    && raw.can_autonomously_decide === false
    && raw.can_replace_user_decision === false
    && raw.arbitrary_code_loading_allowed === false
    && raw.ranking_produced === false
    && raw.winner_claim === false
    && raw.approval_produced === false
    && raw.member_assignment_produced === false
    && raw.workflow_mutation_performed === false
    && raw.user_final_decision_required === true;
}

export function normalizeProjectRoundFocusResponse(payload, expected) {
  const envelope = record(payload);
  const raw = record(envelope.project_round_focus);
  const expectedValue = record(expected);
  const issues = [];
  const recordMode = expectedValue.requestKind === "record";
  const expectedVersion = recordMode
    ? PROJECT_ROUND_FOCUS_RECORD_VERSION
    : PROJECT_ROUND_FOCUS_PREVIEW_VERSION;
  if (!exactKeys(envelope, ["ok", "project_round_focus"]) || envelope.ok !== true) {
    issues.push("RESPONSE_NOT_OK");
  }
  if (!exactKeys(raw, recordMode ? RECORD_FIELDS : PREVIEW_FIELDS)) {
    issues.push("VIEW_MODEL_SHAPE_INVALID");
  }
  if (raw.version !== expectedVersion) issues.push("VERSION_INVALID");
  if (raw.integrity_ok !== true) issues.push("INTEGRITY_NOT_CONFIRMED");
  if (raw.metrics_visible !== true) issues.push("METRICS_NOT_VISIBLE");
  if (text(raw.room_id, 160) !== text(expectedValue.roomId, 160)) {
    issues.push("ROOM_BINDING_MISMATCH");
  }
  if (recordMode) {
    if (text(raw.round_id, 160) !== text(expectedValue.roundId, 160)) {
      issues.push("ROUND_BINDING_MISMATCH");
    }
    if (!text(raw.frozen_at, 80) || typeof raw.runtime_available !== "boolean") {
      issues.push("FROZEN_RECORD_METADATA_INVALID");
    }
  }
  const pluginHash = sha256(raw.plugin_registry_snapshot_sha256);
  if (!pluginHash || pluginHash !== sha256(expectedValue.pluginRegistrySnapshotSha256)) {
    issues.push("PLUGIN_REGISTRY_HASH_MISMATCH");
  }
  const binding = artifactBinding(raw.artifact_binding, issues);
  const resolutionRaw = record(raw.resolution);
  if (
    !exactKeys(resolutionRaw, ["version", "contribution", "adapter", "port"])
    || resolutionRaw.version !== PROJECT_ROUND_FOCUS_RESOLUTION_VERSION
  ) issues.push("RESOLUTION_VERSION_INVALID");
  if (!exactKeys(resolutionRaw.contribution, [
    "contribution_id",
    "contribution_version",
    "contract_sha256",
    "slot_id",
    "component_key",
  ])) issues.push("CONTRIBUTION_RESOLUTION_SHAPE_INVALID");
  if (!exactKeys(resolutionRaw.adapter, [
    "adapter_id",
    "adapter_version",
    "contract_version",
    "contract_sha256",
  ])) issues.push("ADAPTER_RESOLUTION_SHAPE_INVALID");
  if (!exactKeys(resolutionRaw.port, [
    "port_id",
    "port_version",
    "contract_sha256",
    "input_schema_version",
    "input_schema_sha256",
    "output_schema_version",
    "output_schema_sha256",
    "provider_call_budget",
    "market_read_budget",
    "business_write_budget",
    "failure_policy",
  ])) issues.push("PORT_RESOLUTION_SHAPE_INVALID");
  const resolution = {
    contribution: contributionReference(resolutionRaw.contribution),
    adapter: adapterReference(resolutionRaw.adapter),
    port: portReference(resolutionRaw.port),
  };
  const expectedResolution = record(expectedValue.resolution);
  if (!sameReference(resolution.contribution, expectedResolution.contribution, [
    "contribution_id",
    "contribution_version",
    "contract_sha256",
    "slot_id",
    "component_key",
  ])) issues.push("CONTRIBUTION_RESOLUTION_MISMATCH");
  if (!sameReference(resolution.adapter, expectedResolution.adapter, [
    "adapter_id",
    "adapter_version",
    "contract_version",
    "contract_sha256",
  ])) issues.push("ADAPTER_RESOLUTION_MISMATCH");
  if (!sameReference(resolution.port, expectedResolution.port, [
    "port_id",
    "port_version",
    "contract_sha256",
    "input_schema_version",
    "input_schema_sha256",
    "output_schema_version",
    "output_schema_sha256",
  ])) issues.push("PORT_RESOLUTION_MISMATCH");
  if (
    resolution.port.provider_call_budget !== 0
    || resolution.port.market_read_budget !== 0
    || resolution.port.business_write_budget !== 0
    || resolution.port.failure_policy !== "fail_closed"
    || resolution.port.provider_call_budget !== expectedResolution.port?.provider_call_budget
    || resolution.port.market_read_budget !== expectedResolution.port?.market_read_budget
    || resolution.port.business_write_budget !== expectedResolution.port?.business_write_budget
    || resolution.port.failure_policy !== expectedResolution.port?.failure_policy
  ) issues.push("PORT_BUDGET_OR_POLICY_MISMATCH");
  const counts = normalizeCounts(raw.counts, issues);
  const focusItems = normalizeFocusItems(raw.focus_items, issues);
  const suggestedObjective = text(raw.suggested_objective, 4000);
  const previewSha256 = sha256(raw.preview_sha256);
  const inputSealSha256 = sha256(raw.input_seal_sha256);
  if (!suggestedObjective) issues.push("SUGGESTED_OBJECTIVE_INVALID");
  if (!previewSha256) issues.push("PREVIEW_HASH_INVALID");
  if (!inputSealSha256) issues.push("INPUT_SEAL_HASH_INVALID");
  if (counts.focusItemCount !== focusItems.length) issues.push("FOCUS_COUNT_MISMATCH");
  const categories = {
    structural: focusItems.filter((item) => item.category === "structural").length,
    evidence: focusItems.filter((item) => item.category === "evidence").length,
    blocker: focusItems.filter((item) => item.category === "blocker").length,
  };
  if (
    counts.structuralGapCount !== categories.structural
    || counts.evidenceGapCount !== categories.evidence
    || counts.blockerCount !== categories.blocker
  ) issues.push("CATEGORY_COUNT_MISMATCH");
  const expectedState = binding.status === "none"
    ? "bootstrap"
    : counts.blockerCount
      ? "blocked"
      : counts.structuralGapCount || counts.evidenceGapCount
        ? "gaps_present"
        : "ready";
  if (raw.state !== expectedState) issues.push("STATE_INCONSISTENT");
  if (
    binding.status === "none"
    && (focusItems.length || Object.values(counts).some((count) => count !== 0))
  ) issues.push("BOOTSTRAP_GAPS_FABRICATED");
  if (!fixedBoundaryReady(raw)) issues.push("SAFETY_OR_BUDGET_DRIFT");
  if (issues.length) return invalidView(issues);
  return {
    valid: true,
    integrityOk: true,
    metricsVisible: true,
    issues: [],
    kind: recordMode ? "record" : "preview",
    roomId: text(raw.room_id, 160),
    roundId: recordMode ? text(raw.round_id, 160) : "",
    frozenAt: recordMode ? text(raw.frozen_at, 80) : "",
    runtimeAvailable: recordMode ? raw.runtime_available : true,
    state: raw.state,
    artifactBinding: binding,
    pluginRegistrySnapshotSha256: pluginHash,
    counts,
    focusItems,
    suggestedObjective,
    previewSha256,
    inputSealSha256,
    resolution,
  };
}

export function projectRoundFocusCardSource({ record: frozenRecord, preview } = {}) {
  if (frozenRecord?.valid && frozenRecord.kind === "record") return frozenRecord;
  if (preview?.valid && preview.kind === "preview") return preview;
  return null;
}

export function normalizeProjectRoundFocusAuthorization(value) {
  const raw = record(value);
  const rawBinding = record(raw.artifact_binding);
  const status = text(rawBinding.status, 20);
  const bindingKeys = status === "none"
    ? ["status"]
    : ["status", "artifact_id", "artifact_version"];
  const artifactId = text(rawBinding.artifact_id, 160);
  const artifactVersion = status === "exact" ? positiveInteger(rawBinding.artifact_version) : 0;
  const valid = exactKeys(raw, ["version", "artifact_binding", "preview_sha256", "user_confirmed"])
    && raw.version === PROJECT_ROUND_FOCUS_AUTHORIZATION_VERSION
    && raw.user_confirmed === true
    && Boolean(sha256(raw.preview_sha256))
    && exactKeys(rawBinding, bindingKeys)
    && (
      (status === "none")
      || (status === "exact" && ID_PATTERN.test(artifactId) && Boolean(artifactVersion))
    );
  return {
    valid,
    version: text(raw.version, 80),
    previewSha256: sha256(raw.preview_sha256),
    userConfirmed: raw.user_confirmed === true,
    artifactBinding: { status, artifactId, artifactVersion },
  };
}

export function projectRoundFocusAuthorizationPayload(value) {
  const normalized = normalizeProjectRoundFocusAuthorization(value);
  if (!normalized.valid) return null;
  return {
    version: PROJECT_ROUND_FOCUS_AUTHORIZATION_VERSION,
    artifact_binding: normalized.artifactBinding.status === "none"
      ? { status: "none" }
      : {
        status: "exact",
        artifact_id: normalized.artifactBinding.artifactId,
        artifact_version: normalized.artifactBinding.artifactVersion,
      },
    preview_sha256: normalized.previewSha256,
    user_confirmed: true,
  };
}

export function buildProjectRoundFocusAuthorization(view) {
  if (!view?.valid || view.kind !== "preview" || !view.previewSha256) {
    throw new TypeError("下一轮焦点预览尚未通过完整性校验。");
  }
  const binding = view.artifactBinding;
  const artifactBindingValue = binding?.status === "none"
    ? { status: "none" }
    : binding?.status === "exact" && binding.artifactId && positiveInteger(binding.artifactVersion)
      ? {
        status: "exact",
        artifact_id: binding.artifactId,
        artifact_version: binding.artifactVersion,
      }
      : null;
  if (!artifactBindingValue) throw new TypeError("下一轮焦点缺少精确产物绑定。");
  return {
    version: PROJECT_ROUND_FOCUS_AUTHORIZATION_VERSION,
    artifact_binding: artifactBindingValue,
    preview_sha256: view.previewSha256,
    user_confirmed: true,
  };
}

export function projectRoundFocusAuthorizationState(
  authorization,
  {
    roomId,
    artifactFingerprint = "",
    roomContextFingerprint = "",
    pluginRegistrySnapshotSha256 = "",
  } = {},
) {
  const source = record(authorization);
  const request = normalizeProjectRoundFocusAuthorization(source.request);
  const matches = request.valid
    && text(source.roomId, 160) === text(roomId, 160)
    && fingerprint(source.artifactFingerprint) === fingerprint(artifactFingerprint)
    && fingerprint(source.roomContextFingerprint) === fingerprint(roomContextFingerprint)
    && sha256(source.pluginRegistrySnapshotSha256) === sha256(pluginRegistrySnapshotSha256);
  return { valid: matches, request: matches ? source.request : null };
}
