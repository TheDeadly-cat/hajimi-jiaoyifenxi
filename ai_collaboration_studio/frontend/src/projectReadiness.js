import {
  ARTIFACT_PLUGIN_REGISTRY_CONTEXT_VERSION,
  frozenPortResolution,
  HOST_CONTRIBUTION_IDS,
  HOST_SLOT_IDS,
  PLUGIN_REGISTRY_SNAPSHOT_VERSION_V1,
  PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2,
  PROJECT_READINESS_VIEW_MODEL_SCHEMA_VERSION,
} from "./capabilityContributions.js";

export const PROJECT_READINESS_PROJECTION_VERSION = "project_readiness_projection_v1";
export const PROJECT_READINESS_RESOLUTION_VERSION = "project_readiness_resolution_v1";
export const PROJECT_READINESS_INSPECT_ACTION = "project_readiness.inspect";

const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const ITEM_LIMIT = 500;
const PROJECT_READINESS_VIEW_MODEL_FIELDS = new Set([
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
]);

function record(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function text(value) {
  return typeof value === "string" ? value.trim() : "";
}

function sha256(value) {
  const normalized = text(value).toLowerCase();
  return SHA256_PATTERN.test(normalized) ? normalized : "";
}

function positiveInteger(value) {
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

function array(value) {
  return Array.isArray(value) ? value : [];
}

function sameReference(actual, expected, fields) {
  return fields.every((field) => text(actual[field]).toLowerCase() === text(expected[field]).toLowerCase());
}

function exactKeys(value, expected) {
  const keys = Object.keys(record(value)).sort();
  const wanted = [...expected].sort();
  return keys.length === wanted.length && keys.every((key, index) => key === wanted[index]);
}

function fixedSafetyReady(value) {
  const safety = record(value);
  return safety.execution_capability === "none"
    && safety.live_trading_allowed === false
    && safety.can_autonomously_decide === false
    && safety.can_replace_user_decision === false
    && safety.arbitrary_code_loading_allowed === false
    && safety.provider_calls_performed === 0
    && safety.market_reads_performed === 0
    && safety.business_writes_performed === 0
    && safety.ranking_produced === false
    && safety.winner_claim === false
    && safety.approval_produced === false
    && safety.user_final_decision_required === true;
}

function invalidProjection(issues) {
  return {
    valid: false,
    integrityOk: false,
    metricsVisible: false,
    issues: [...new Set(issues)],
    structuralGaps: [],
    blockers: [],
    evidenceGaps: [],
    hashes: {},
    resolution: {},
  };
}

function normalizeGapRows(value, group, issues) {
  if (!Array.isArray(value)) {
    issues.push(`${group.toUpperCase()}_NOT_ARRAY`);
    return [];
  }
  if (value.length > ITEM_LIMIT) issues.push(`${group.toUpperCase()}_LIMIT_EXCEEDED`);
  const seen = new Set();
  return value.slice(0, ITEM_LIMIT).map((raw, index) => {
    const row = record(raw);
    const code = text(row.code);
    const itemKey = text(row.item_key);
    const message = text(row.message);
    const identity = `${code}|${itemKey}`;
    if (!exactKeys(row, ["code", "item_key", "message"]) || !code || !itemKey || !message || seen.has(identity)) {
      issues.push(`${group.toUpperCase()}_ROW_INVALID:${index}`);
    }
    seen.add(identity);
    return {
      code,
      itemKey,
      message,
    };
  });
}

function contributionReference(value) {
  const raw = record(value);
  return {
    contribution_id: text(raw.contribution_id),
    contribution_version: text(raw.contribution_version),
    contract_sha256: sha256(raw.contract_sha256),
    slot_id: text(raw.slot_id),
    component_key: text(raw.component_key),
  };
}

function adapterReference(value) {
  const raw = record(value);
  return {
    adapter_id: text(raw.adapter_id),
    adapter_version: text(raw.adapter_version),
    contract_version: text(raw.contract_version),
    contract_sha256: sha256(raw.contract_sha256),
  };
}

function portReference(value) {
  const raw = record(value);
  return {
    port_id: text(raw.port_id),
    port_version: text(raw.port_version),
    contract_sha256: sha256(raw.contract_sha256),
    input_schema_version: text(raw.input_schema_version),
    input_schema_sha256: sha256(raw.input_schema_sha256),
    output_schema_version: text(raw.output_schema_version),
    output_schema_sha256: sha256(raw.output_schema_sha256),
    provider_call_budget: raw.provider_call_budget,
    market_read_budget: raw.market_read_budget,
    business_write_budget: raw.business_write_budget,
    failure_policy: text(raw.failure_policy),
  };
}

function exactFrozenResolution(artifact, contribution) {
  const context = record(artifact?.plugin_registry_context);
  const snapshot = record(context.snapshot);
  const rawContributions = array(snapshot.ui_contributions);
  const frozenContributions = rawContributions.filter(
    (item) => text(item?.contribution_id) === HOST_CONTRIBUTION_IDS.projectReadinessArtifactWorkspace,
  );
  if (frozenContributions.length !== 1) return null;
  const frozenRaw = record(frozenContributions[0]);
  const frozenContribution = contributionReference(frozenRaw);
  const expectedContribution = {
    contribution_id: contribution?.id,
    contribution_version: contribution?.version,
    contract_sha256: contribution?.contractHash,
    slot_id: HOST_SLOT_IDS.artifactWorkspace,
    component_key: "project_readiness_review",
  };
  if (!sameReference(frozenContribution, expectedContribution, [
    "contribution_id",
    "contribution_version",
    "contract_sha256",
    "slot_id",
    "component_key",
  ])) return null;

  const sourcePort = record(contribution?.sourcePort);
  const sourcePortResolution = record(contribution?.sourcePortResolution);
  const viewModel = record(contribution?.viewModel);
  if (
    contribution?.contractVersion !== "ui_contribution_contract_v2"
    || frozenRaw.contract_version !== contribution.contractVersion
    || text(frozenRaw.source_port?.owner_pack_id) !== text(sourcePort.ownerPackId)
    || text(frozenRaw.source_port?.port_id) !== text(sourcePort.portId)
    || text(frozenRaw.source_port?.requirement) !== text(sourcePort.requirement)
    || text(frozenRaw.source_port?.cardinality) !== text(sourcePort.cardinality)
    || text(frozenRaw.view_model?.schema_version) !== text(viewModel.schemaVersion)
    || sha256(frozenRaw.view_model?.schema_sha256) !== sha256(viewModel.schemaHash)
    || text(frozenRaw.source_port_resolution?.owner_pack_id)
      !== text(sourcePortResolution.ownerPackId)
    || text(frozenRaw.source_port_resolution?.port_id) !== text(sourcePortResolution.portId)
    || text(frozenRaw.source_port_resolution?.port_version)
      !== text(sourcePortResolution.portVersion)
    || sha256(frozenRaw.source_port_resolution?.port_contract_sha256)
      !== sha256(sourcePortResolution.portContractHash)
    || text(frozenRaw.source_port_resolution?.output_schema_version)
      !== text(sourcePortResolution.outputSchemaVersion)
    || sha256(frozenRaw.source_port_resolution?.output_schema_sha256)
      !== sha256(sourcePortResolution.outputSchemaHash)
    || text(viewModel.schemaVersion) !== PROJECT_READINESS_VIEW_MODEL_SCHEMA_VERSION
    || !sha256(viewModel.schemaHash)
  ) return null;

  const packId = text(sourcePort.ownerPackId);
  if (packId !== text(contribution?.packId)) return null;
  const packs = array(snapshot.capability_packs).filter((item) => text(item?.id) === packId);
  if (packs.length !== 1) return null;
  const portResolution = frozenPortResolution(snapshot, {
    packId,
    portId: text(sourcePort.portId),
  });
  if (
    !portResolution
    || portResolution.adapter.contract_version !== "domain_adapter_contract_v2"
    || portResolution.port.port_version !== text(sourcePortResolution.portVersion)
    || portResolution.port.contract_sha256 !== sha256(sourcePortResolution.portContractHash)
    || portResolution.port.output_schema_version !== text(sourcePortResolution.outputSchemaVersion)
    || portResolution.port.output_schema_sha256 !== sha256(sourcePortResolution.outputSchemaHash)
    || portResolution.port.provider_call_budget !== 0
    || portResolution.port.market_read_budget !== 0
    || portResolution.port.business_write_budget !== 0
    || portResolution.port.failure_policy !== "fail_closed"
  ) return null;
  return {
    contribution: frozenContribution,
    adapter: portResolution.adapter,
    port: portResolution.port,
    viewModel: {
      schema_version: text(viewModel.schemaVersion),
      schema_sha256: sha256(viewModel.schemaHash),
    },
  };
}

/**
 * Resolve whether the host may perform the one read-only readiness GET.
 * Historical v1/unversioned snapshots and unavailable exact bindings remain
 * visible as explanatory placeholders but never produce a request.
 */
export function projectReadinessLoadPlan({
  room,
  artifact,
  slot,
  contribution,
  showLegacyFallback = false,
}) {
  const context = record(artifact?.plugin_registry_context);
  const snapshot = record(context.snapshot);
  const base = {
    visible: Boolean(contribution?.present || showLegacyFallback),
    shouldLoad: false,
    mode: "read_only",
    reason: "",
    requestKey: "",
    expected: null,
  };
  if (!base.visible) return { ...base, mode: "hidden" };
  if (context.version !== ARTIFACT_PLUGIN_REGISTRY_CONTEXT_VERSION) {
    return { ...base, reason: "该历史产物没有版本化插件上下文；不会推断项目就绪结论。" };
  }
  if (snapshot.version === PLUGIN_REGISTRY_SNAPSHOT_VERSION_V1) {
    return { ...base, reason: "该产物使用旧版插件快照，尚未冻结项目就绪端口；不会用当前实现替算。" };
  }
  if (snapshot.version !== PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2) {
    return { ...base, reason: "冻结插件快照版本无法识别；项目就绪投影已关闭。" };
  }
  if (slot?.integrityOk !== true || context.integrity_ok !== true || context.exact_binding !== true) {
    return { ...base, reason: slot?.reason || "冻结插件合同完整性无法验证；项目就绪投影已关闭。" };
  }
  if (contribution?.present !== true || contribution?.active !== true) {
    return { ...base, reason: contribution?.reason || slot?.reason || "项目就绪贡献当前不可用，只保留只读说明。" };
  }
  if (
    contribution.id !== HOST_CONTRIBUTION_IDS.projectReadinessArtifactWorkspace
    || contribution.slotId !== HOST_SLOT_IDS.artifactWorkspace
    || contribution.componentKey !== "project_readiness_review"
    || contribution.contractVersion !== "ui_contribution_contract_v2"
    || text(contribution.viewModel?.schemaVersion) !== PROJECT_READINESS_VIEW_MODEL_SCHEMA_VERSION
    || !sha256(contribution.viewModel?.schemaHash)
    || !sha256(contribution.contractHash)
    || array(contribution.allowedActions).length !== 1
    || contribution.allowedActions[0] !== PROJECT_READINESS_INSPECT_ACTION
  ) {
    return { ...base, reason: "项目就绪贡献与宿主允许的精确合同不一致。" };
  }
  const resolution = exactFrozenResolution(artifact, contribution);
  if (!resolution) {
    return { ...base, reason: "项目就绪贡献缺少声明的精确来源端口、adapter 或视图模型绑定。" };
  }
  const roomId = text(room?.id);
  const artifactId = text(artifact?.id);
  const artifactVersion = positiveInteger(artifact?.version);
  const pluginRegistrySnapshotSha256 = sha256(context.snapshot_sha256);
  if (!roomId || !artifactId || !artifactVersion || !pluginRegistrySnapshotSha256) {
    return { ...base, reason: "项目就绪请求缺少精确房间、产物版本或插件封印。" };
  }
  const expected = {
    roomId,
    artifactId,
    artifactVersion,
    pluginRegistrySnapshotSha256,
    resolution,
  };
  return {
    ...base,
    visible: true,
    shouldLoad: true,
    mode: "load",
    reason: "",
    requestKey: [
      roomId,
      artifactId,
      artifactVersion,
      pluginRegistrySnapshotSha256,
      resolution.contribution.contract_sha256,
      resolution.adapter.contract_sha256,
      resolution.port.contract_sha256,
      resolution.port.output_schema_sha256,
      resolution.viewModel.schema_sha256,
    ].join("|"),
    expected,
  };
}

export function normalizeProjectReadinessResponse(payload, expected) {
  const envelope = record(payload);
  const raw = record(envelope.projection);
  const expectedValue = record(expected);
  const issues = [];
  if (!exactKeys(envelope, ["ok", "projection"]) || envelope.ok !== true) {
    issues.push("RESPONSE_NOT_OK");
  }
  if (!exactKeys(raw, PROJECT_READINESS_VIEW_MODEL_FIELDS)) {
    issues.push("VIEW_MODEL_SHAPE_INVALID");
  }
  const expectedViewModel = record(expectedValue.resolution?.viewModel);
  if (
    expectedViewModel.schema_version !== PROJECT_READINESS_VIEW_MODEL_SCHEMA_VERSION
    || !sha256(expectedViewModel.schema_sha256)
  ) {
    issues.push("VIEW_MODEL_BINDING_INVALID");
  }
  if (raw.version !== PROJECT_READINESS_PROJECTION_VERSION) issues.push("VERSION_INVALID");
  if (raw.integrity_ok !== true) issues.push("INTEGRITY_NOT_CONFIRMED");
  if (raw.metrics_visible !== true) issues.push("METRICS_NOT_VISIBLE");
  if (text(raw.room_id) !== text(expectedValue.roomId)) issues.push("ROOM_BINDING_MISMATCH");
  if (text(raw.artifact_id) !== text(expectedValue.artifactId)) issues.push("ARTIFACT_BINDING_MISMATCH");
  if (positiveInteger(raw.artifact_version) !== positiveInteger(expectedValue.artifactVersion)) {
    issues.push("ARTIFACT_VERSION_MISMATCH");
  }

  const hashes = {
    artifactSnapshotSha256: sha256(raw.artifact_snapshot_sha256),
    evidenceGraphSha256: sha256(raw.evidence_graph_sha256),
    pluginRegistrySnapshotSha256: sha256(raw.plugin_registry_snapshot_sha256),
  };
  if (!hashes.artifactSnapshotSha256) issues.push("ARTIFACT_SNAPSHOT_HASH_INVALID");
  if (!hashes.evidenceGraphSha256) issues.push("EVIDENCE_GRAPH_HASH_INVALID");
  if (
    !hashes.pluginRegistrySnapshotSha256
    || hashes.pluginRegistrySnapshotSha256 !== sha256(expectedValue.pluginRegistrySnapshotSha256)
  ) {
    issues.push("PLUGIN_REGISTRY_HASH_MISMATCH");
  }

  const resolutionRaw = record(raw.resolution);
  if (
    !exactKeys(resolutionRaw, ["version", "contribution", "adapter", "port"])
    || resolutionRaw.version !== PROJECT_READINESS_RESOLUTION_VERSION
  ) {
    issues.push("RESOLUTION_VERSION_INVALID");
  }
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
  if (!sameReference(resolution.contribution, record(expectedResolution.contribution), [
    "contribution_id",
    "contribution_version",
    "contract_sha256",
    "slot_id",
    "component_key",
  ])) issues.push("CONTRIBUTION_RESOLUTION_MISMATCH");
  if (!sameReference(resolution.adapter, record(expectedResolution.adapter), [
    "adapter_id",
    "adapter_version",
    "contract_version",
    "contract_sha256",
  ])) issues.push("ADAPTER_RESOLUTION_MISMATCH");
  if (!sameReference(resolution.port, record(expectedResolution.port), [
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
  if (!fixedSafetyReady(raw)) issues.push("SAFETY_OR_BUDGET_DRIFT");

  const structuralGaps = normalizeGapRows(raw.structural_gaps, "structural_gaps", issues);
  const blockers = normalizeGapRows(raw.blockers, "blockers", issues);
  const evidenceGaps = normalizeGapRows(raw.evidence_gaps, "evidence_gaps", issues);
  const expectedState = blockers.length
    ? "blocked"
    : structuralGaps.length || evidenceGaps.length
      ? "gaps_present"
      : "ready";
  if (raw.state !== expectedState) issues.push("STATE_INCONSISTENT");
  if (issues.length) return invalidProjection(issues);
  return {
    valid: true,
    integrityOk: true,
    metricsVisible: true,
    issues: [],
    roomId: text(raw.room_id),
    artifactId: text(raw.artifact_id),
    artifactVersion: positiveInteger(raw.artifact_version),
    state: raw.state,
    structuralGaps,
    blockers,
    evidenceGaps,
    hashes,
    resolution,
  };
}
