import {
  pluginLifecycleCatalogView,
  pluginLifecycleRuntimeReason,
  pluginLifecycleTarget,
} from "./pluginLifecycle.js";

const SHA256_PATTERN = /^[0-9a-f]{64}$/;

export const PLUGIN_REGISTRY_SNAPSHOT_VERSION_V1 = "plugin_registry_snapshot_v1";
export const PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2 = "plugin_registry_snapshot_v2";
export const PLUGIN_REGISTRY_SNAPSHOT_VERSION = PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2;
export const PLUGIN_REGISTRY_CATALOG_VERSION_V1 = "plugin_registry_catalog_v1";
export const PLUGIN_REGISTRY_CATALOG_VERSION_V2 = "plugin_registry_catalog_v2";
export const PLUGIN_REGISTRY_CATALOG_VERSION = PLUGIN_REGISTRY_CATALOG_VERSION_V2;
export const ARTIFACT_PLUGIN_REGISTRY_CONTEXT_VERSION = "artifact_plugin_registry_context_v1";

const UI_CONTRIBUTION_CONTRACT_VERSION_V1 = "ui_contribution_contract_v1";
const UI_CONTRIBUTION_CONTRACT_VERSION_V2 = "ui_contribution_contract_v2";
const DOMAIN_ADAPTER_PORT_CONTRACT_VERSION = "domain_adapter_port_contract_v1";
export const PROJECT_READINESS_VIEW_MODEL_SCHEMA_VERSION = "project_readiness_view_model_v1";
export const PROJECT_ROUND_FOCUS_VIEW_MODEL_SCHEMA_VERSION = "project_round_focus_view_model_v1";
export const FOOTBALL_RESEARCH_VIEW_MODEL_SCHEMA_VERSION = "football_research_view_model_v1";
export const STOCK_RESEARCH_VIEW_MODEL_SCHEMA_VERSION = "stock_research_view_model_v1";

const PROJECT_READINESS_PORT_ID = "core.artifact.projection/v1";
const PROJECT_READINESS_PORT_VERSION = "1.0.0";
const PROJECT_READINESS_PORT_HANDLER = "project_artifact";
const PROJECT_READINESS_PORT_VERSION_RANGE = ">=1.0.0 <2.0.0";
const PROJECT_ROUND_FOCUS_PORT_ID = "core.round.context/v1";
const PROJECT_ROUND_FOCUS_PORT_VERSION = "1.0.0";
const PROJECT_ROUND_FOCUS_PORT_HANDLER = "project_round_context";
const PROJECT_ROUND_FOCUS_PORT_VERSION_RANGE = ">=1.0.0 <2.0.0";
const FOOTBALL_RESEARCH_PORT_ID = "core.football.match_context/v1";
const FOOTBALL_RESEARCH_PORT_VERSION = "1.0.0";
const FOOTBALL_RESEARCH_PORT_HANDLER = "project_football_match_context";
const FOOTBALL_RESEARCH_PORT_VERSION_RANGE = ">=1.0.0 <2.0.0";
const STOCK_RESEARCH_PORT_ID = "core.market.readonly_context/v1";
const STOCK_RESEARCH_PORT_VERSION = "1.0.0";
const STOCK_RESEARCH_PORT_HANDLER = "project_market_readonly_context";
const STOCK_RESEARCH_PORT_VERSION_RANGE = ">=1.0.0 <2.0.0";

const HOST_PORT_SPECS = Object.freeze({
  [PROJECT_READINESS_PORT_ID]: Object.freeze({
    portVersion: PROJECT_READINESS_PORT_VERSION,
    handlerMethod: PROJECT_READINESS_PORT_HANDLER,
    versionRange: PROJECT_READINESS_PORT_VERSION_RANGE,
    cardinality: "multiple",
    inputSchemaVersion: "artifact_projection_input_v1",
    outputSchemaVersion: "artifact_projection_output_v1",
    readSurfaces: Object.freeze([
      "artifact.version.exact",
      "artifact.evidence_relations.exact",
    ]),
  }),
  [PROJECT_ROUND_FOCUS_PORT_ID]: Object.freeze({
    portVersion: PROJECT_ROUND_FOCUS_PORT_VERSION,
    handlerMethod: PROJECT_ROUND_FOCUS_PORT_HANDLER,
    versionRange: PROJECT_ROUND_FOCUS_PORT_VERSION_RANGE,
    cardinality: "multiple",
    inputSchemaVersion: "project_round_context_input_v1",
    outputSchemaVersion: "project_round_context_output_v1",
    readSurfaces: Object.freeze([
      "artifact.projection.sealed",
      "room.round_focus.safe_context",
    ]),
  }),
  [FOOTBALL_RESEARCH_PORT_ID]: Object.freeze({
    portVersion: FOOTBALL_RESEARCH_PORT_VERSION,
    handlerMethod: FOOTBALL_RESEARCH_PORT_HANDLER,
    versionRange: FOOTBALL_RESEARCH_PORT_VERSION_RANGE,
    cardinality: "multiple",
    inputSchemaVersion: "football_match_context_input_v1",
    outputSchemaVersion: "football_match_context_output_v1",
    readSurfaces: Object.freeze([
      "room.material.version.exact",
      "room.material.content_sha256.exact",
      "room.material.snapshot_sha256.exact",
    ]),
  }),
  [STOCK_RESEARCH_PORT_ID]: Object.freeze({
    portVersion: STOCK_RESEARCH_PORT_VERSION,
    handlerMethod: STOCK_RESEARCH_PORT_HANDLER,
    versionRange: STOCK_RESEARCH_PORT_VERSION_RANGE,
    cardinality: "multiple",
    inputSchemaVersion: "stock_market_readonly_context_input_v1",
    outputSchemaVersion: "stock_market_readonly_context_output_v1",
    readSurfaces: Object.freeze([
      "room.stock_room_scope.exact",
      "room.material.version.exact",
      "room.material.content_sha256.exact",
      "room.material.snapshot_sha256.exact",
    ]),
  }),
});

const SUPPORTED_PLUGIN_REGISTRY_SNAPSHOT_VERSIONS = new Set([
  PLUGIN_REGISTRY_SNAPSHOT_VERSION_V1,
  PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2,
]);

export const HOST_SLOT_IDS = Object.freeze({
  roomSettings: "core.room.settings/v1",
  roomInspector: "core.room.inspector/v1",
  artifactWorkspace: "core.artifact.workspace/v1",
});

export const HOST_CONTRIBUTION_IDS = Object.freeze({
  roomCapabilityPackSettings: "core.capability_pack_settings/v1",
  projectArtifactWorkspace: "project_research.artifact_workspace/v1",
  projectReadinessArtifactWorkspace: "project_readiness.artifact_workspace/v1",
  projectRoundFocusRoomInspector: "project_round_focus.room_inspector/v1",
  footballResearchRoomInspector: "football_research.room_inspector/v1",
  stockResearchRoomInspector: "stock_research.room_inspector/v1",
  storageRoomInspector: "storage_research.room_inspector/v1",
  storageArtifactWorkspace: "storage_research.artifact_workspace/v1",
});

const FIXED_SAFETY = Object.freeze({
  execution_capability: "none",
  live_trading_allowed: false,
  can_autonomously_decide: false,
  can_replace_user_decision: false,
  arbitrary_code_loading_allowed: false,
  user_final_decision_required: true,
});

const HOST_UI_CONTRIBUTIONS = Object.freeze({
  [HOST_CONTRIBUTION_IDS.roomCapabilityPackSettings]: Object.freeze({
    slotId: HOST_SLOT_IDS.roomSettings,
    componentKey: "room_capability_pack_settings",
  }),
  [HOST_CONTRIBUTION_IDS.projectArtifactWorkspace]: Object.freeze({
    slotId: HOST_SLOT_IDS.artifactWorkspace,
    componentKey: "project_research_workspace",
  }),
  [HOST_CONTRIBUTION_IDS.projectReadinessArtifactWorkspace]: Object.freeze({
    slotId: HOST_SLOT_IDS.artifactWorkspace,
    componentKey: "project_readiness_review",
    requiredActions: Object.freeze(["project_readiness.inspect"]),
    requiredSnapshotVersion: PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2,
    requiredCatalogVersion: PLUGIN_REGISTRY_CATALOG_VERSION_V2,
    requiredContractVersion: UI_CONTRIBUTION_CONTRACT_VERSION_V2,
    requiredViewModelSchemaVersion: PROJECT_READINESS_VIEW_MODEL_SCHEMA_VERSION,
  }),
  [HOST_CONTRIBUTION_IDS.projectRoundFocusRoomInspector]: Object.freeze({
    slotId: HOST_SLOT_IDS.roomInspector,
    componentKey: "project_round_focus",
    requiredActions: Object.freeze(["project_round_focus.inspect"]),
    requiredSnapshotVersion: PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2,
    requiredCatalogVersion: PLUGIN_REGISTRY_CATALOG_VERSION_V2,
    requiredContractVersion: UI_CONTRIBUTION_CONTRACT_VERSION_V2,
    requiredViewModelSchemaVersion: PROJECT_ROUND_FOCUS_VIEW_MODEL_SCHEMA_VERSION,
  }),
  [HOST_CONTRIBUTION_IDS.footballResearchRoomInspector]: Object.freeze({
    slotId: HOST_SLOT_IDS.roomInspector,
    componentKey: "football_research_inspector",
    requiredActions: Object.freeze(["football_research.inspect"]),
    requiredSnapshotVersion: PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2,
    requiredCatalogVersion: PLUGIN_REGISTRY_CATALOG_VERSION_V2,
    requiredContractVersion: UI_CONTRIBUTION_CONTRACT_VERSION_V2,
    requiredViewModelSchemaVersion: FOOTBALL_RESEARCH_VIEW_MODEL_SCHEMA_VERSION,
  }),
  [HOST_CONTRIBUTION_IDS.stockResearchRoomInspector]: Object.freeze({
    slotId: HOST_SLOT_IDS.roomInspector,
    componentKey: "stock_research_inspector",
    requiredActions: Object.freeze(["stock_research.inspect"]),
    requiredSnapshotVersion: PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2,
    requiredCatalogVersion: PLUGIN_REGISTRY_CATALOG_VERSION_V2,
    requiredContractVersion: UI_CONTRIBUTION_CONTRACT_VERSION_V2,
    requiredViewModelSchemaVersion: STOCK_RESEARCH_VIEW_MODEL_SCHEMA_VERSION,
  }),
  [HOST_CONTRIBUTION_IDS.storageRoomInspector]: Object.freeze({
    slotId: HOST_SLOT_IDS.roomInspector,
    componentKey: "storage_research_inspector",
  }),
  [HOST_CONTRIBUTION_IDS.storageArtifactWorkspace]: Object.freeze({
    slotId: HOST_SLOT_IDS.artifactWorkspace,
    componentKey: "storage_research_artifact_workspace",
  }),
});

const FORBIDDEN_HOST_SLOTS = new Set([
  "core.user_decision/v1",
  "core.user-decision/v1",
]);

function record(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function string(value) {
  return typeof value === "string" ? value.trim() : "";
}

function safeArray(value) {
  return Array.isArray(value) ? value : [];
}

function uniqueRows(rows, identity) {
  const ids = rows.map(identity);
  return ids.every(Boolean) && new Set(ids).size === ids.length;
}

function fixedSafetyMatches(value) {
  const safety = record(value);
  return Object.entries(FIXED_SAFETY).every(([key, expected]) => safety[key] === expected);
}

function exactStringSet(value, expected = []) {
  const actual = safeArray(value).map(string).filter(Boolean);
  return actual.length === expected.length
    && new Set(actual).size === actual.length
    && expected.every((item) => actual.includes(item));
}

function exactKeys(value, expected) {
  const keys = Object.keys(record(value)).sort();
  const wanted = [...expected].sort();
  return keys.length === wanted.length && keys.every((key, index) => key === wanted[index]);
}

function sourcePortReference(value) {
  const source = record(value);
  return {
    ownerPackId: string(source.owner_pack_id || source.ownerPackId),
    portId: string(source.port_id || source.portId),
    requirement: string(source.requirement),
    cardinality: string(source.cardinality),
  };
}

function viewModelReference(value) {
  const viewModel = record(value);
  return {
    schemaVersion: string(viewModel.schema_version || viewModel.schemaVersion),
    schemaHash: string(viewModel.schema_sha256 || viewModel.schemaHash).toLowerCase(),
  };
}

function sourcePortResolutionReference(value) {
  const resolution = record(value);
  return {
    ownerPackId: string(resolution.owner_pack_id || resolution.ownerPackId),
    portId: string(resolution.port_id || resolution.portId),
    portVersion: string(resolution.port_version || resolution.portVersion),
    portContractHash: string(
      resolution.port_contract_sha256 || resolution.portContractHash,
    ).toLowerCase(),
    outputSchemaVersion: string(
      resolution.output_schema_version || resolution.outputSchemaVersion,
    ),
    outputSchemaHash: string(
      resolution.output_schema_sha256 || resolution.outputSchemaHash,
    ).toLowerCase(),
  };
}

const PROJECT_READINESS_VIEW_MODEL_FIELDS = Object.freeze([
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

const PROJECT_ROUND_FOCUS_VIEW_MODEL_FIELDS = Object.freeze([
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

const FOOTBALL_RESEARCH_VIEW_MODEL_FIELDS = Object.freeze([
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
]);

const STOCK_RESEARCH_VIEW_MODEL_FIELDS = Object.freeze([
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
]);

const HOST_VIEW_MODEL_SPECS = Object.freeze({
  [PROJECT_READINESS_VIEW_MODEL_SCHEMA_VERSION]: Object.freeze({
    componentKey: "project_readiness_review",
    fields: PROJECT_READINESS_VIEW_MODEL_FIELDS,
  }),
  [PROJECT_ROUND_FOCUS_VIEW_MODEL_SCHEMA_VERSION]: Object.freeze({
    componentKey: "project_round_focus",
    fields: PROJECT_ROUND_FOCUS_VIEW_MODEL_FIELDS,
  }),
  [FOOTBALL_RESEARCH_VIEW_MODEL_SCHEMA_VERSION]: Object.freeze({
    componentKey: "football_research_inspector",
    fields: FOOTBALL_RESEARCH_VIEW_MODEL_FIELDS,
  }),
  [STOCK_RESEARCH_VIEW_MODEL_SCHEMA_VERSION]: Object.freeze({
    componentKey: "stock_research_inspector",
    fields: STOCK_RESEARCH_VIEW_MODEL_FIELDS,
  }),
});

const FROZEN_PORT_REQUIREMENT_KEYS = Object.freeze([
  "port_id",
  "requirement",
  "cardinality",
  "version_range",
]);

const FROZEN_PORT_RESOLUTION_KEYS = Object.freeze([
  "owner_pack_id",
  ...FROZEN_PORT_REQUIREMENT_KEYS,
  "resolved",
]);

const FROZEN_RESOLVED_PORT_KEYS = Object.freeze([
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
]);

const FROZEN_ADAPTER_PORT_KEYS = Object.freeze([
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
  ...Object.keys(FIXED_SAFETY),
]);

function validateV2AdapterPortReferences(snapshot, errors) {
  if (snapshot.version === PLUGIN_REGISTRY_SNAPSHOT_VERSION_V1) {
    if (
      Object.hasOwn(snapshot, "port_resolutions")
      || safeArray(snapshot.domain_adapters).some((adapter) => Object.hasOwn(record(adapter), "ports"))
    ) {
      errors.push("v1 冻结插件合同不得携带端口解析");
    }
    return;
  }
  if (snapshot.version !== PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2) return;
  const adapters = safeArray(snapshot.domain_adapters);
  const resolutions = safeArray(snapshot.port_resolutions);
  const portBoundAdapters = new Set(resolutions.flatMap((resolution) => (
    safeArray(resolution?.resolved).map((binding) => [
      string(binding?.adapter_id),
      string(binding?.adapter_version),
    ].join("|"))
  )));
  if (!uniqueRows(adapters, (adapter) => string(adapter?.adapter_id))) {
    errors.push("v2 冻结领域适配器身份重复或缺失");
  }
  for (const rawAdapter of adapters) {
    const adapter = record(rawAdapter);
    const adapterId = string(adapter.adapter_id) || "unknown";
    const ports = safeArray(adapter.ports);
    const adapterBoundToPort = portBoundAdapters.has([
      string(adapter.adapter_id),
      string(adapter.adapter_version),
    ].join("|"));
    if (adapterBoundToPort && adapter.contract_version !== "domain_adapter_contract_v2") {
      errors.push(`v2 冻结领域适配器 ${adapterId} 合同版本无效`);
    }
    if (adapterBoundToPort && (!Array.isArray(adapter.ports) || !ports.length)) {
      errors.push(`v2 冻结领域适配器 ${adapterId} 缺少端口引用`);
      continue;
    }
    if (!adapterBoundToPort && adapter.contract_version !== "domain_adapter_contract_v2" && ports.length) {
      errors.push(`v2 冻结领域适配器 ${adapterId} 的旧合同不得声明端口`);
    }
    if (!uniqueRows(ports, (port) => string(port?.port_id))) {
      errors.push(`v2 冻结领域适配器 ${adapterId} 的端口身份重复或缺失`);
    }
    for (const rawPort of ports) {
      const port = record(rawPort);
      if (
        !string(port.port_id)
        || !string(port.port_version)
        || !SHA256_PATTERN.test(string(port.contract_sha256).toLowerCase())
      ) {
        errors.push(`v2 冻结领域适配器 ${adapterId} 的端口引用无效`);
      }
    }
  }
  if (!Array.isArray(snapshot.port_resolutions) || !resolutions.length) {
    errors.push("v2 冻结插件合同缺少端口解析");
    return;
  }
  if (!uniqueRows(resolutions, (resolution) => [
    string(resolution?.owner_pack_id),
    string(resolution?.port_id),
  ].join("|"))) {
    errors.push("v2 冻结端口解析身份重复或缺失");
  }
  for (const rawResolution of resolutions) {
    const resolution = record(rawResolution);
    const resolved = safeArray(resolution.resolved);
    if (
      !string(resolution.owner_pack_id)
      || !string(resolution.port_id)
      || !["required", "optional"].includes(resolution.requirement)
      || !["one", "multiple"].includes(resolution.cardinality)
      || !Array.isArray(resolution.resolved)
      || (resolution.requirement === "required" && !resolved.length)
      || (resolution.cardinality === "one" && resolved.length > 1)
    ) {
      errors.push("v2 冻结端口解析要求无效");
      continue;
    }
    for (const rawBinding of resolved) {
      const binding = record(rawBinding);
      const adapter = adapters.find((item) => (
        string(item?.adapter_id) === string(binding.adapter_id)
        && string(item?.adapter_version) === string(binding.adapter_version)
        && string(item?.contract_sha256).toLowerCase()
          === string(binding.adapter_contract_sha256).toLowerCase()
      ));
      const port = safeArray(adapter?.ports).find((item) => (
        string(item?.port_id) === string(binding.port_id)
        && string(item?.port_version) === string(binding.port_version)
        && string(item?.contract_sha256).toLowerCase()
          === string(binding.port_contract_sha256).toLowerCase()
      ));
      if (
        !adapter
        || !port
        || string(binding.port_id) !== string(resolution.port_id)
        || !string(binding.input_schema_version)
        || !SHA256_PATTERN.test(string(binding.input_schema_sha256).toLowerCase())
        || !string(binding.output_schema_version)
        || !SHA256_PATTERN.test(string(binding.output_schema_sha256).toLowerCase())
        || binding.provider_call_budget !== 0
        || binding.market_read_budget !== 0
        || binding.business_write_budget !== 0
        || binding.failure_policy !== "fail_closed"
      ) {
        errors.push("v2 冻结端口解析结果无效");
      }
    }
    if (
      HOST_PORT_SPECS[string(resolution.port_id)]
      && !frozenPortResolution(snapshot, {
        packId: string(resolution.owner_pack_id),
        portId: string(resolution.port_id),
      })
    ) {
      errors.push("v2 冻结宿主端口解析合同发生漂移");
    }
  }
  if (
    snapshot.resolution?.port_resolution_policy !== "manifest_declared_exact_only"
    || snapshot.resolution?.undeclared_port_policy !== "reject"
    || snapshot.resolution?.required_port_policy !== "fail_closed"
  ) {
    errors.push("v2 冻结端口解析策略漂移");
  }
}

const FROZEN_UI_CONTRIBUTION_V2_KEYS = new Set([
  "contribution_id",
  "contribution_version",
  "contract_sha256",
  "slot_id",
  "component_key",
  "label",
  "order",
  "status",
  "contract_version",
  "source_port",
  "view_model",
  "source_port_resolution",
]);

function validateFrozenUiContributions(snapshot, errors) {
  const contributions = safeArray(snapshot.ui_contributions);
  if (snapshot.version === PLUGIN_REGISTRY_SNAPSHOT_VERSION_V1) {
    if (contributions.some((item) => (
      Object.hasOwn(record(item), "contract_version")
      || Object.hasOwn(record(item), "source_port")
      || Object.hasOwn(record(item), "view_model")
      || Object.hasOwn(record(item), "source_port_resolution")
      || HOST_UI_CONTRIBUTIONS[string(item?.contribution_id)]?.requiredSnapshotVersion
    ))) {
      errors.push("v1 冻结插件合同不得携带 v2 UI contribution 声明");
    }
    return;
  }
  if (snapshot.version !== PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2) return;
  for (const raw of contributions) {
    const contribution = record(raw);
    const id = string(contribution.contribution_id);
    const contractVersion = string(contribution.contract_version);
    const hasV2Declaration = Object.hasOwn(contribution, "source_port")
      || Object.hasOwn(contribution, "view_model")
      || Object.hasOwn(contribution, "source_port_resolution")
      || Boolean(contractVersion);
    if (!hasV2Declaration) {
      if (HOST_UI_CONTRIBUTIONS[id]?.requiredSnapshotVersion) {
        errors.push("版本化冻结贡献缺少 v2 来源端口声明");
      }
      continue;
    }
    const sourcePort = sourcePortReference(contribution.source_port);
    const viewModel = viewModelReference(contribution.view_model);
    const sourceResolution = sourcePortResolutionReference(
      contribution.source_port_resolution,
    );
    const host = HOST_UI_CONTRIBUTIONS[id];
    if (
      contractVersion !== UI_CONTRIBUTION_CONTRACT_VERSION_V2
      || !exactKeys(contribution, FROZEN_UI_CONTRIBUTION_V2_KEYS)
      || !exactKeys(contribution.source_port, ["owner_pack_id", "port_id", "requirement", "cardinality"])
      || !exactKeys(contribution.view_model, ["schema_version", "schema_sha256"])
      || !exactKeys(contribution.source_port_resolution, [
        "owner_pack_id",
        "port_id",
        "port_version",
        "port_contract_sha256",
        "output_schema_version",
        "output_schema_sha256",
      ])
      || !sourcePort.ownerPackId
      || !sourcePort.portId
      || sourcePort.requirement !== "required"
      || sourcePort.cardinality !== "one"
      || viewModel.schemaVersion !== host?.requiredViewModelSchemaVersion
      || !SHA256_PATTERN.test(viewModel.schemaHash)
      || sourceResolution.ownerPackId !== sourcePort.ownerPackId
      || sourceResolution.portId !== sourcePort.portId
      || !sourceResolution.portVersion
      || !SHA256_PATTERN.test(sourceResolution.portContractHash)
      || !sourceResolution.outputSchemaVersion
      || !SHA256_PATTERN.test(sourceResolution.outputSchemaHash)
    ) {
      errors.push(`冻结 UI contribution ${id || "unknown"} 的 v2 声明无效`);
    }
  }
}

export function frozenPortResolution(snapshotValue, { packId, portId }) {
  const snapshot = record(snapshotValue);
  if (snapshot.version !== PLUGIN_REGISTRY_SNAPSHOT_VERSION_V2) return null;
  const portSpec = HOST_PORT_SPECS[string(portId)];
  if (!portSpec) return null;
  const resolutions = safeArray(snapshot.port_resolutions).filter((item) => (
    string(item?.owner_pack_id) === string(packId)
    && string(item?.port_id) === string(portId)
  ));
  const resolution = record(resolutions[0]);
  if (
    resolutions.length !== 1
    || !exactKeys(resolution, FROZEN_PORT_RESOLUTION_KEYS)
    || resolution.requirement !== "required"
    || resolution.cardinality !== "one"
    || resolution.version_range !== portSpec.versionRange
    || safeArray(resolution.resolved).length !== 1
  ) return null;
  const binding = record(resolution.resolved[0]);
  if (!exactKeys(binding, FROZEN_RESOLVED_PORT_KEYS)) return null;
  const pack = safeArray(snapshot.capability_packs).find(
    (item) => string(item?.id) === string(packId),
  );
  const requirements = safeArray(pack?.domain_adapter_port_requirements).filter((item) => (
    string(item?.port_id) === string(portId)
  ));
  const requirement = record(requirements[0]);
  if (
    requirements.length !== 1
    || !exactKeys(requirement, FROZEN_PORT_REQUIREMENT_KEYS)
    || requirement.port_id !== resolution.port_id
    || requirement.requirement !== resolution.requirement
    || requirement.cardinality !== resolution.cardinality
    || requirement.version_range !== resolution.version_range
    || !safeArray(pack?.domain_adapter_ids).map(string).includes(string(binding.adapter_id))
  ) {
    return null;
  }
  const adapter = safeArray(snapshot.domain_adapters).find((item) => (
    string(item?.adapter_id) === string(binding.adapter_id)
    && string(item?.adapter_version) === string(binding.adapter_version)
    && string(item?.contract_sha256).toLowerCase()
      === string(binding.adapter_contract_sha256).toLowerCase()
  ));
  const port = safeArray(adapter?.ports).find((item) => (
    string(item?.port_id) === string(portId)
    && string(item?.port_id) === string(binding.port_id)
    && string(item?.port_version) === string(binding.port_version)
    && string(item?.contract_sha256).toLowerCase()
      === string(binding.port_contract_sha256).toLowerCase()
  ));
  if (
    !adapter
    || !port
    || adapter.contract_version !== "domain_adapter_contract_v2"
    || adapter.status !== "ready"
    || !exactKeys(port, FROZEN_ADAPTER_PORT_KEYS)
    || string(port.port_version) !== portSpec.portVersion
    || port.handler_method !== portSpec.handlerMethod
    || port.cardinality !== portSpec.cardinality
    || port.input_schema_version !== portSpec.inputSchemaVersion
    || port.output_schema_version !== portSpec.outputSchemaVersion
    || !exactStringSet(port.read_surfaces, portSpec.readSurfaces)
    || !exactStringSet(port.local_write_surfaces, [])
    || port.provider_call_budget !== 0
    || port.market_read_budget !== 0
    || port.business_write_budget !== 0
    || port.external_write_allowed !== false
    || port.failure_policy !== "fail_closed"
    || !fixedSafetyMatches(port)
    || !SHA256_PATTERN.test(string(binding.adapter_contract_sha256).toLowerCase())
    || !SHA256_PATTERN.test(string(binding.port_contract_sha256).toLowerCase())
    || !string(binding.input_schema_version)
    || !SHA256_PATTERN.test(string(binding.input_schema_sha256).toLowerCase())
    || !string(binding.output_schema_version)
    || !SHA256_PATTERN.test(string(binding.output_schema_sha256).toLowerCase())
    || binding.provider_call_budget !== 0
    || binding.market_read_budget !== 0
    || binding.business_write_budget !== 0
    || binding.failure_policy !== "fail_closed"
    || binding.handler_method !== portSpec.handlerMethod
    || binding.input_schema_version !== string(port.input_schema_version)
    || string(binding.input_schema_sha256).toLowerCase()
      !== string(port.input_schema_sha256).toLowerCase()
    || binding.output_schema_version !== string(port.output_schema_version)
    || string(binding.output_schema_sha256).toLowerCase()
      !== string(port.output_schema_sha256).toLowerCase()
    || binding.provider_call_budget !== port.provider_call_budget
    || binding.market_read_budget !== port.market_read_budget
    || binding.business_write_budget !== port.business_write_budget
    || binding.failure_policy !== port.failure_policy
  ) return null;
  return {
    adapter: {
      adapter_id: string(binding.adapter_id),
      adapter_version: string(binding.adapter_version),
      contract_version: string(adapter.contract_version),
      contract_sha256: string(binding.adapter_contract_sha256).toLowerCase(),
    },
    port: {
      port_id: string(binding.port_id),
      port_version: string(binding.port_version),
      contract_sha256: string(binding.port_contract_sha256).toLowerCase(),
      input_schema_version: string(binding.input_schema_version),
      input_schema_sha256: string(binding.input_schema_sha256).toLowerCase(),
      output_schema_version: string(binding.output_schema_version),
      output_schema_sha256: string(binding.output_schema_sha256).toLowerCase(),
      handler_method: string(binding.handler_method),
      provider_call_budget: binding.provider_call_budget,
      market_read_budget: binding.market_read_budget,
      business_write_budget: binding.business_write_budget,
      failure_policy: string(binding.failure_policy),
    },
  };
}

function hostSnapshotContractAvailability(context, catalogContribution, host) {
  if (!host?.requiredSnapshotVersion) return { ready: true, reason: "" };
  if (context.snapshot?.version !== host.requiredSnapshotVersion) {
    return {
      ready: false,
      reason: "该冻结贡献来自旧版插件快照，未绑定宿主要求的版本化端口。",
    };
  }
  const sourcePort = sourcePortReference(catalogContribution?.sourcePort);
  const viewModel = viewModelReference(catalogContribution?.viewModel);
  const frozenContribution = safeArray(context.snapshot.ui_contributions).find(
    (item) => string(item?.contribution_id) === catalogContribution?.id,
  );
  const sourceResolution = sourcePortResolutionReference(
    frozenContribution?.source_port_resolution,
  );
  if (
    catalogContribution?.contractVersion !== host.requiredContractVersion
    || sourcePort.ownerPackId !== catalogContribution?.packId
    || !sourcePort.portId
    || sourcePort.requirement !== "required"
    || sourcePort.cardinality !== "one"
    || viewModel.schemaVersion !== host.requiredViewModelSchemaVersion
    || !SHA256_PATTERN.test(viewModel.schemaHash)
    || sourceResolution.ownerPackId !== sourcePort.ownerPackId
    || sourceResolution.portId !== sourcePort.portId
    || !sourceResolution.portVersion
    || !SHA256_PATTERN.test(sourceResolution.portContractHash)
    || !sourceResolution.outputSchemaVersion
    || !SHA256_PATTERN.test(sourceResolution.outputSchemaHash)
  ) {
    return {
      ready: false,
      reason: "冻结贡献缺少宿主支持的精确来源端口或视图模型声明。",
    };
  }
  const pack = safeArray(context.snapshot.capability_packs).find(
    (item) => string(item?.id) === sourcePort.ownerPackId,
  );
  if (!pack) {
    return {
      ready: false,
      reason: "冻结贡献缺少所属能力包。",
    };
  }
  const resolution = frozenPortResolution(context.snapshot, {
    packId: sourcePort.ownerPackId,
    portId: sourcePort.portId,
  });
  const portContract = record(catalogContribution?.portContract);
  if (
    !resolution
    || resolution.adapter.contract_version !== "domain_adapter_contract_v2"
    || resolution.port.port_id !== string(portContract.port_id)
    || resolution.port.port_version !== string(portContract.port_version)
    || resolution.port.contract_sha256 !== string(portContract.contract_sha256).toLowerCase()
    || resolution.port.input_schema_version !== string(portContract.input_schema_version)
    || resolution.port.input_schema_sha256 !== string(portContract.input_schema_sha256).toLowerCase()
    || resolution.port.output_schema_version !== string(portContract.output_schema_version)
    || resolution.port.output_schema_sha256 !== string(portContract.output_schema_sha256).toLowerCase()
    || sourceResolution.portVersion !== resolution.port.port_version
    || sourceResolution.portContractHash !== resolution.port.contract_sha256
    || sourceResolution.outputSchemaVersion !== resolution.port.output_schema_version
    || sourceResolution.outputSchemaHash !== resolution.port.output_schema_sha256
    || resolution.port.provider_call_budget !== 0
    || resolution.port.market_read_budget !== 0
    || resolution.port.business_write_budget !== 0
    || resolution.port.failure_policy !== "fail_closed"
  ) {
    return {
      ready: false,
      reason: "冻结贡献缺少与目录一致的唯一只读来源端口解析。",
    };
  }
  return { ready: true, reason: "" };
}

function isForbiddenHostSlot(value) {
  const slotId = string(value);
  return FORBIDDEN_HOST_SLOTS.has(slotId)
    || slotId.includes("user_decision")
    || slotId.includes("user-decision");
}

function catalogContributionIdentity(value) {
  const contribution = record(value);
  const sourcePort = sourcePortReference(contribution.source_port || contribution.sourcePort);
  const viewModel = viewModelReference(contribution.view_model || contribution.viewModel);
  return [
    string(contribution.contribution_id || contribution.id),
    string(contribution.contribution_version || contribution.version),
    string(contribution.contract_sha256 || contribution.contractHash).toLowerCase(),
    string(contribution.slot_id || contribution.slotId),
    string(contribution.component_key || contribution.componentKey),
    string(contribution.contract_version || contribution.contractVersion)
      || UI_CONTRIBUTION_CONTRACT_VERSION_V1,
    sourcePort.ownerPackId,
    sourcePort.portId,
    sourcePort.requirement,
    sourcePort.cardinality,
    viewModel.schemaVersion,
    viewModel.schemaHash,
  ].join("|");
}

function exactContributionIdentity(value) {
  const contribution = record(value);
  const sourceResolution = sourcePortResolutionReference(
    contribution.source_port_resolution || contribution.sourcePortResolution,
  );
  return [
    catalogContributionIdentity(contribution),
    sourceResolution.ownerPackId,
    sourceResolution.portId,
    sourceResolution.portVersion,
    sourceResolution.portContractHash,
    sourceResolution.outputSchemaVersion,
    sourceResolution.outputSchemaHash,
  ].join("|");
}

const DOMAIN_ADAPTER_PORT_CATALOG_KEYS = new Set([
  "contract_version",
  "port_id",
  "port_version",
  "handler_method",
  "cardinality",
  "input_schema",
  "input_schema_sha256",
  "output_schema",
  "output_schema_sha256",
  "read_surfaces",
  "local_write_surfaces",
  "provider_call_budget",
  "market_read_budget",
  "business_write_budget",
  "external_write_allowed",
  "failure_policy",
  ...Object.keys(FIXED_SAFETY),
  "contract_sha256",
]);

const UI_VIEW_MODEL_SCHEMA_KEYS = new Set([
  "schema_version",
  "component_key",
  "type",
  "required",
  "fields",
  "additional_properties",
  "schema_sha256",
]);

const UI_CONTRIBUTION_V2_KEYS = new Set([
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
  "source_port",
  "view_model",
  ...Object.keys(FIXED_SAFETY),
  "contract_sha256",
]);

function closedSchemaReady(value) {
  const schema = record(value);
  const required = safeArray(schema.required).map(string).filter(Boolean);
  const fields = record(schema.fields);
  return exactKeys(schema, ["version", "type", "required", "fields", "additional_properties"])
    && Boolean(string(schema.version))
    && schema.type === "object"
    && schema.additional_properties === false
    && required.length > 0
    && new Set(required).size === required.length
    && exactStringSet(Object.keys(fields), required)
    && Object.values(fields).every((fieldType) => Boolean(string(fieldType)));
}

function catalogPortView(catalog, errors) {
  if (!Array.isArray(catalog.domain_adapter_ports) || !catalog.domain_adapter_ports.length) {
    errors.push("bootstrap v2 registry 缺少领域适配器端口闭集");
    return new Map();
  }
  if (!uniqueRows(catalog.domain_adapter_ports, (item) => string(item?.port_id))) {
    errors.push("bootstrap 领域适配器端口身份重复或缺失");
  }
  const byId = new Map();
  for (const raw of safeArray(catalog.domain_adapter_ports)) {
    const port = record(raw);
    const portId = string(port.port_id);
    const portSpec = HOST_PORT_SPECS[portId];
    const inputHash = string(port.input_schema_sha256).toLowerCase();
    const outputHash = string(port.output_schema_sha256).toLowerCase();
    const contractHash = string(port.contract_sha256).toLowerCase();
    const ready = exactKeys(port, DOMAIN_ADAPTER_PORT_CATALOG_KEYS)
      && port.contract_version === DOMAIN_ADAPTER_PORT_CONTRACT_VERSION
      && Boolean(portSpec)
      && string(port.port_version) === portSpec?.portVersion
      && port.handler_method === portSpec?.handlerMethod
      && port.cardinality === portSpec?.cardinality
      && closedSchemaReady(port.input_schema)
      && closedSchemaReady(port.output_schema)
      && string(port.input_schema?.version) === portSpec?.inputSchemaVersion
      && string(port.output_schema?.version) === portSpec?.outputSchemaVersion
      && exactStringSet(port.read_surfaces, portSpec?.readSurfaces || [])
      && SHA256_PATTERN.test(inputHash)
      && SHA256_PATTERN.test(outputHash)
      && SHA256_PATTERN.test(contractHash)
      && port.provider_call_budget === 0
      && port.market_read_budget === 0
      && port.business_write_budget === 0
      && port.external_write_allowed === false
      && port.failure_policy === "fail_closed"
      && fixedSafetyMatches(port);
    if (!ready) errors.push(`bootstrap 领域适配器端口 ${portId || "unknown"} 合同无效`);
    byId.set(portId, {
      port_id: portId,
      port_version: string(port.port_version),
      contract_sha256: contractHash,
      input_schema_version: string(port.input_schema?.version),
      input_schema_sha256: inputHash,
      output_schema_version: string(port.output_schema?.version),
      output_schema_sha256: outputHash,
      provider_call_budget: port.provider_call_budget,
      market_read_budget: port.market_read_budget,
      business_write_budget: port.business_write_budget,
      failure_policy: string(port.failure_policy),
      ready,
    });
  }
  return byId;
}

function catalogViewModelView(catalog, errors) {
  if (!Array.isArray(catalog.ui_view_model_schemas) || !catalog.ui_view_model_schemas.length) {
    errors.push("bootstrap v2 registry 缺少 UI 视图模型闭集");
    return new Map();
  }
  if (!uniqueRows(catalog.ui_view_model_schemas, (item) => string(item?.schema_version))) {
    errors.push("bootstrap UI 视图模型身份重复或缺失");
  }
  const byVersion = new Map();
  for (const raw of safeArray(catalog.ui_view_model_schemas)) {
    const schema = record(raw);
    const version = string(schema.schema_version);
    const schemaSpec = HOST_VIEW_MODEL_SPECS[version];
    const hash = string(schema.schema_sha256).toLowerCase();
    const ready = exactKeys(schema, UI_VIEW_MODEL_SCHEMA_KEYS)
      && Boolean(schemaSpec)
      && schema.component_key === schemaSpec?.componentKey
      && schema.type === "object"
      && schema.additional_properties === false
      && exactStringSet(schema.required, schemaSpec?.fields || [])
      && exactStringSet(Object.keys(record(schema.fields)), schemaSpec?.fields || [])
      && Object.values(record(schema.fields)).every((fieldType) => Boolean(string(fieldType)))
      && SHA256_PATTERN.test(hash);
    if (!ready) errors.push(`bootstrap UI 视图模型 ${version || "unknown"} 合同无效`);
    byVersion.set(version, {
      schemaVersion: version,
      schemaHash: hash,
      componentKey: string(schema.component_key),
      ready,
    });
  }
  return byVersion;
}

function pluginCatalogView(pluginRegistry) {
  const catalog = record(pluginRegistry);
  const errors = [];
  const catalogVersion = string(catalog.version);
  if (![PLUGIN_REGISTRY_CATALOG_VERSION_V1, PLUGIN_REGISTRY_CATALOG_VERSION_V2].includes(catalogVersion)) {
    errors.push("bootstrap plugin registry 版本不受支持");
  }
  if (!SHA256_PATTERN.test(string(catalog.catalog_sha256).toLowerCase())) {
    errors.push("bootstrap plugin registry 哈希无效");
  }
  if (!fixedSafetyMatches(catalog.safety)) {
    errors.push("bootstrap plugin registry 安全字段漂移");
  }
  let portsById = new Map();
  let viewModelsByVersion = new Map();
  if (catalogVersion === PLUGIN_REGISTRY_CATALOG_VERSION_V1) {
    if (Object.hasOwn(catalog, "domain_adapter_ports") || Object.hasOwn(catalog, "ui_view_model_schemas")) {
      errors.push("bootstrap v1 registry 不得携带端口或 UI 视图模型闭集");
    }
  } else if (catalogVersion === PLUGIN_REGISTRY_CATALOG_VERSION_V2) {
    portsById = catalogPortView(catalog, errors);
    viewModelsByVersion = catalogViewModelView(catalog, errors);
  }
  const rows = safeArray(catalog.ui_contributions);
  if (!uniqueRows(rows, (item) => string(item?.contribution_id))) {
    errors.push("bootstrap UI contribution 身份重复或缺失");
  }
  const byId = new Map(rows.map((raw) => {
    const contribution = record(raw);
    const id = string(contribution.contribution_id);
    const host = HOST_UI_CONTRIBUTIONS[id];
    const slotId = string(contribution.slot_id);
    const componentKey = string(contribution.component_key);
    const contractHash = string(contribution.contract_sha256).toLowerCase();
    const allowedActions = safeArray(contribution.allowed_actions).map(string).filter(Boolean);
    const contractVersion = string(contribution.contract_version);
    const sourcePort = sourcePortReference(contribution.source_port);
    const viewModel = viewModelReference(contribution.view_model);
    const viewModelCatalog = viewModelsByVersion.get(viewModel.schemaVersion);
    const portContract = portsById.get(sourcePort.portId);
    const legacyContractReady = contractVersion === UI_CONTRIBUTION_CONTRACT_VERSION_V1
      && !Object.hasOwn(contribution, "source_port")
      && !Object.hasOwn(contribution, "view_model");
    const v2ContractReady = catalogVersion === PLUGIN_REGISTRY_CATALOG_VERSION_V2
      && contractVersion === UI_CONTRIBUTION_CONTRACT_VERSION_V2
      && exactKeys(contribution, UI_CONTRIBUTION_V2_KEYS)
      && exactKeys(contribution.source_port, ["owner_pack_id", "port_id", "requirement", "cardinality"])
      && exactKeys(contribution.view_model, ["schema_version", "schema_sha256"])
      && sourcePort.ownerPackId === string(contribution.pack_id)
      && sourcePort.requirement === "required"
      && sourcePort.cardinality === "one"
      && portContract?.ready === true
      && viewModelCatalog?.ready === true
      && viewModel.schemaHash === viewModelCatalog.schemaHash
      && componentKey === viewModelCatalog.componentKey;
    if (catalogVersion === PLUGIN_REGISTRY_CATALOG_VERSION_V1 && (!legacyContractReady
      || host?.requiredCatalogVersion)) {
      errors.push(`bootstrap v1 UI contribution ${id || "unknown"} 版本无效`);
    }
    if (catalogVersion === PLUGIN_REGISTRY_CATALOG_VERSION_V2
      && !legacyContractReady && !v2ContractReady) {
      errors.push(`bootstrap v2 UI contribution ${id || "unknown"} 合同无效`);
    }
    const hostAllowed = Boolean(
      host
      && !isForbiddenHostSlot(slotId)
      && slotId === host.slotId
      && componentKey === host.componentKey
      && contractVersion === (host.requiredContractVersion || UI_CONTRIBUTION_CONTRACT_VERSION_V1)
      && (!host.requiredCatalogVersion || catalogVersion === host.requiredCatalogVersion)
      && (legacyContractReady || v2ContractReady)
      && contribution.mode === "host_owned_component"
      && string(contribution.contribution_version)
      && SHA256_PATTERN.test(contractHash)
      && (!host.requiredActions || exactStringSet(allowedActions, host.requiredActions))
      && fixedSafetyMatches(contribution),
    );
    return [id, {
      id,
      version: string(contribution.contribution_version),
      contractHash,
      slotId,
      componentKey,
      packId: string(contribution.pack_id),
      order: Number.isInteger(contribution.order) ? contribution.order : 0,
      allowedActions,
      contractVersion,
      sourcePort,
      viewModel,
      portContract,
      hostAllowed,
    }];
  }));
  return {
    integrityOk: errors.length === 0,
    errors,
    version: catalogVersion,
    portsById,
    viewModelsByVersion,
    byId,
  };
}

function contributionLifecycleAvailability(context, lifecycleView, contribution, catalogContribution) {
  if (!lifecycleView.integrityOk) {
    return {
      runtimeAvailable: false,
      reason: lifecycleView.errors[0] || "生命周期状态无法验证。",
    };
  }
  const target = pluginLifecycleTarget(lifecycleView, {
    kind: "ui_contribution",
    id: contribution.contribution_id,
    version: contribution.contribution_version,
    sha256: contribution.contract_sha256,
  });
  const pack = safeArray(context.snapshot.capability_packs).find(
    (item) => string(item?.id) === catalogContribution?.packId,
  );
  const packTarget = pack ? pluginLifecycleTarget(lifecycleView, {
    kind: "capability_pack",
    id: pack.id,
    version: pack.pack_version,
    sha256: pack.manifest_sha256,
  }) : null;
  const adapterTargets = safeArray(pack?.domain_adapter_ids).map((adapterId) => {
    const adapter = safeArray(context.snapshot.domain_adapters).find(
      (item) => string(item?.adapter_id) === string(adapterId),
    );
    return adapter ? pluginLifecycleTarget(lifecycleView, {
      kind: "domain_adapter",
      id: adapter.adapter_id,
      version: adapter.adapter_version,
      sha256: adapter.contract_sha256,
    }) : null;
  });
  const requiredTargets = [packTarget, target, ...adapterTargets];
  const unavailable = requiredTargets.find((item) => !item?.runtimeAvailable);
  return {
    runtimeAvailable: requiredTargets.length >= 2 && !unavailable,
    reason: unavailable
      ? pluginLifecycleRuntimeReason(unavailable)
      : requiredTargets.some((item) => !item)
        ? "冻结贡献缺少精确生命周期目标。"
        : "",
  };
}

export function frozenPluginRegistryContext(value, { sourceType = "" } = {}) {
  const raw = record(value);
  const artifactContext = raw.version === ARTIFACT_PLUGIN_REGISTRY_CONTEXT_VERSION;
  const snapshot = record(artifactContext ? raw.snapshot : raw.plugin_registry_snapshot);
  const status = string(
    artifactContext
      ? raw.status
      : raw.plugin_registry_status || (raw.plugin_registry_integrity_ok === true ? "ready" : "legacy_unversioned"),
  ) || "legacy_unversioned";
  const snapshotSha256 = string(
    artifactContext
      ? raw.snapshot_sha256
      : raw.plugin_registry_snapshot_sha256 || snapshot.registry_snapshot_sha256,
  ).toLowerCase();
  const contextSourceType = string(artifactContext ? raw.source_type : sourceType) || "unknown";
  const legacy = status === "legacy_unversioned"
    || (!Object.keys(snapshot).length && !snapshotSha256 && status !== "integrity_failed");
  if (legacy) {
    return {
      status: "legacy_unversioned",
      integrityOk: false,
      runtimeAvailable: false,
      exactBinding: false,
      snapshotSha256,
      snapshot: {},
      sourceType: contextSourceType,
      errors: ["历史记录没有版本化插件合同"],
    };
  }

  const errors = [];
  const declaredIntegrity = artifactContext
    ? raw.integrity_ok === true
    : raw.plugin_registry_integrity_ok === true;
  const exactBinding = artifactContext ? raw.exact_binding === true : declaredIntegrity;
  if (status === "integrity_failed" || !declaredIntegrity) {
    errors.push("服务端未确认冻结插件合同完整性");
  }
  if (!exactBinding) errors.push("冻结插件合同没有精确绑定来源");
  if (!SUPPORTED_PLUGIN_REGISTRY_SNAPSHOT_VERSIONS.has(snapshot.version)) {
    errors.push("冻结插件合同版本不受支持");
  }
  if (
    !SHA256_PATTERN.test(snapshotSha256)
    || snapshotSha256 !== string(snapshot.registry_snapshot_sha256).toLowerCase()
  ) {
    errors.push("冻结插件合同哈希不一致");
  }
  if (!fixedSafetyMatches(snapshot.safety)) errors.push("冻结插件合同安全字段漂移");
  if (artifactContext && !fixedSafetyMatches(raw)) errors.push("产物插件上下文安全字段漂移");
  if (snapshot.resolution?.dynamic_code_loading !== false) {
    errors.push("冻结插件合同禁止动态代码加载");
  }
  const rawContributions = safeArray(snapshot.ui_contributions);
  if (!uniqueRows(rawContributions, (item) => string(item?.contribution_id))) {
    errors.push("冻结 UI contribution 身份重复或缺失");
  }
  if (rawContributions.some((item) => isForbiddenHostSlot(item?.slot_id))) {
    errors.push("用户最终决定区不接受插件贡献");
  }
  validateFrozenUiContributions(snapshot, errors);
  validateV2AdapterPortReferences(snapshot, errors);
  const integrityOk = errors.length === 0;
  return {
    status: integrityOk ? status : "integrity_failed",
    integrityOk,
    runtimeAvailable: integrityOk && (
      artifactContext ? raw.runtime_available === true : status === "ready"
    ),
    exactBinding: integrityOk && exactBinding,
    snapshotSha256: integrityOk ? snapshotSha256 : "",
    snapshot: integrityOk ? snapshot : {},
    snapshotVersion: integrityOk ? string(snapshot.version) : "",
    sourceType: contextSourceType,
    errors: [...new Set(errors)],
  };
}

function resolveFrozenSlot(context, pluginRegistry, lifecycleView, slotId) {
  const catalog = pluginCatalogView(pluginRegistry);
  if (isForbiddenHostSlot(slotId)) {
    return {
      status: "integrity_failed",
      integrityOk: false,
      runtimeAvailable: false,
      enabled: false,
      contributions: [],
      errors: ["用户最终决定区不接受插件贡献"],
    };
  }
  if (context.status === "legacy_unversioned") {
    return {
      status: "legacy_unversioned",
      integrityOk: false,
      runtimeAvailable: false,
      enabled: false,
      contributions: [],
      errors: context.errors,
    };
  }
  if (!context.integrityOk) {
    return {
      status: "integrity_failed",
      integrityOk: false,
      runtimeAvailable: false,
      enabled: false,
      contributions: [],
      errors: context.errors,
    };
  }

  const contributions = safeArray(context.snapshot.ui_contributions)
    .filter((item) => string(item?.slot_id) === slotId)
    .map((raw) => {
      const contribution = record(raw);
      const id = string(contribution.contribution_id);
      const host = HOST_UI_CONTRIBUTIONS[id];
      const hostAllowed = Boolean(
        host
        && host.slotId === slotId
        && string(contribution.component_key) === host.componentKey
        && !isForbiddenHostSlot(slotId),
      );
       const catalogContribution = catalog.byId.get(id);
       const catalogExact = Boolean(
        catalog.integrityOk
        && hostAllowed
        && catalogContribution?.hostAllowed
         && catalogContributionIdentity(contribution) === catalogContributionIdentity(catalogContribution),
        );
      const snapshotContract = hostSnapshotContractAvailability(
        context,
        catalogContribution,
        host,
      );
      const lifecycle = contributionLifecycleAvailability(
        context,
        lifecycleView,
        contribution,
        catalogContribution,
      );
      const runtimeAvailable = Boolean(
        context.runtimeAvailable
        && catalogExact
        && snapshotContract.ready
        && lifecycle.runtimeAvailable
        && contribution.status === "ready",
      );
      return {
        id,
        version: string(contribution.contribution_version),
        contractHash: string(contribution.contract_sha256).toLowerCase(),
        slotId,
        componentKey: string(contribution.component_key),
        packId: catalogExact ? catalogContribution.packId : "",
        label: string(contribution.label) || id,
        order: Number.isInteger(contribution.order) ? contribution.order : 0,
        present: hostAllowed,
        catalogExact,
        contractVersion: catalogExact ? catalogContribution.contractVersion : "",
        sourcePort: catalogExact ? catalogContribution.sourcePort : null,
        sourcePortResolution: catalogExact
          ? sourcePortResolutionReference(contribution.source_port_resolution)
          : null,
        viewModel: catalogExact ? catalogContribution.viewModel : null,
        runtimeAvailable,
        lifecycleRuntimeAvailable: lifecycle.runtimeAvailable,
        lifecycleReason: lifecycle.reason,
        snapshotContractReady: snapshotContract.ready,
        snapshotContractReason: snapshotContract.reason,
        declaredActions: catalogExact ? catalogContribution.allowedActions : [],
      };
    })
    .sort((left, right) => left.order - right.order || left.id.localeCompare(right.id));
  const enabled = contributions.some((item) => item.present);
  const runtimeAvailable = contributions.some((item) => item.runtimeAvailable);
  const implementationUnavailable = contributions.some((item) => item.present && !item.catalogExact)
    || (!catalog.integrityOk && enabled)
    || (context.status === "implementation_unavailable" && enabled);
  return {
    status: runtimeAvailable
      ? "ready"
      : implementationUnavailable
        ? "implementation_unavailable"
        : enabled
          ? "read_only"
          : "not_enabled",
    integrityOk: true,
    runtimeAvailable,
    enabled,
    contributions,
    errors: [...new Set([...context.errors, ...catalog.errors])],
  };
}

export function resolveHostOwnedSlot({
  slotId,
  frozenContext,
  pluginRegistry,
  pluginLifecycle,
  runtimeContext = null,
}) {
  const cleanSlotId = string(slotId);
  const context = frozenPluginRegistryContext(frozenContext);
  const lifecycleView = pluginLifecycleCatalogView(pluginLifecycle);
  const frozen = resolveFrozenSlot(context, pluginRegistry, lifecycleView, cleanSlotId);
  const runtime = runtimeContext
    ? resolveFrozenSlot(
      frozenPluginRegistryContext(runtimeContext),
      pluginRegistry,
      lifecycleView,
      cleanSlotId,
    )
    : null;
  const runtimeByIdentity = new Map(
    safeArray(runtime?.contributions).map((item) => [exactContributionIdentity(item), item]),
  );
  const contributions = frozen.contributions.map((item) => {
    const runtimeMatch = runtime
      ? runtimeByIdentity.get(exactContributionIdentity(item))
      : item;
    const active = Boolean(
      item.runtimeAvailable
      && runtimeMatch?.runtimeAvailable,
    );
    return {
      ...item,
      active,
      readOnly: item.present && !active,
      allowedActions: active ? item.declaredActions : [],
      reason: active
        ? ""
        : item.lifecycleReason
          ? item.lifecycleReason
          : item.snapshotContractReason
            ? item.snapshotContractReason
        : !item.catalogExact
          ? "冻结贡献与当前宿主实现的版本或哈希不一致"
          : runtime && !runtimeMatch?.runtimeAvailable
            ? "当前房间未启用同一精确贡献"
            : "冻结贡献当前仅可只读查看",
    };
  });
  const enabled = contributions.some((item) => item.present);
  const active = contributions.some((item) => item.active);
  const status = frozen.status === "ready" && enabled && !active
    ? "read_only"
    : frozen.status;
  const contributionReason = contributions.find((item) => item.present && !item.active)?.reason || "";
  const reason = status === "legacy_unversioned"
    ? "该历史记录没有版本化插件合同，只能按已保存内容只读展示。"
    : status === "integrity_failed"
      ? "冻结插件合同完整性校验失败，所有插件动作均已关闭。"
      : status === "implementation_unavailable"
        ? "冻结合同仍被保留，但当前宿主没有精确兼容的实现。"
          : status === "read_only"
          ? contributionReason || "冻结贡献可识别，但当前房间未启用同一精确实现，只能查看。"
          : status === "not_enabled"
            ? "该冻结上下文未启用此宿主贡献。"
            : "";
  return {
    slotId: cleanSlotId,
    status,
    integrityOk: frozen.integrityOk,
    runtimeAvailable: active,
    enabled,
    readOnly: enabled && !active,
    snapshotSha256: context.snapshotSha256,
    snapshotVersion: context.snapshotVersion,
    sourceType: context.sourceType,
    reason,
    errors: frozen.errors,
    contributions,
  };
}

export function resolvedHostContribution(slot, contributionId) {
  return safeArray(slot?.contributions).find((item) => item.id === contributionId) || null;
}

export function hasProjectWorkspaceFootprint(content) {
  const value = record(content);
  if (safeArray(value.requirements).length || safeArray(value.risks).length) return true;
  const decision = record(value.decision);
  return safeArray(decision.options).some((raw) => {
    const option = record(raw);
    return Boolean(
      string(option.value)
      || string(option.cost)
      || string(option.timeline)
      || safeArray(option.dependencies).some((item) => string(item))
      || (string(option.reversibility) && string(option.reversibility) !== "unknown"),
    );
  });
}

export function shortPluginHash(value) {
  const hash = string(value).toLowerCase();
  return SHA256_PATTERN.test(hash) ? `${hash.slice(0, 8)}…${hash.slice(-6)}` : "未封印";
}

export function capabilityRegistryView(room, capabilityPacks = []) {
  const snapshot = record(room?.plugin_registry_snapshot);
  const errors = [];
  if (room?.plugin_registry_integrity_ok !== true) errors.push("服务端未确认 registry 完整性");
  if (!SUPPORTED_PLUGIN_REGISTRY_SNAPSHOT_VERSIONS.has(snapshot.version)) {
    errors.push("registry 版本不受支持");
  }
  if (!SHA256_PATTERN.test(string(snapshot.registry_snapshot_sha256).toLowerCase())) {
    errors.push("registry 哈希无效");
  }
  if (!fixedSafetyMatches(snapshot.safety)) errors.push("registry 安全字段漂移");
  if (snapshot.resolution?.dynamic_code_loading !== false) {
    errors.push("registry 禁止动态代码加载");
  }
  validateFrozenUiContributions(snapshot, errors);
  validateV2AdapterPortReferences(snapshot, errors);

  const catalogById = new Map(
    safeArray(capabilityPacks)
      .filter((pack) => pack && typeof pack === "object")
      .map((pack) => [string(pack.id), pack]),
  );
  const rawPacks = safeArray(snapshot.capability_packs);
  if (!uniqueRows(rawPacks, (pack) => string(pack?.id))) errors.push("能力包身份重复或缺失");
  const packs = rawPacks.map((raw) => {
    const pack = record(raw);
    const id = string(pack.id);
    const catalog = record(catalogById.get(id));
    const manifestHash = string(pack.manifest_sha256).toLowerCase();
    const ready = Boolean(
      catalog.id
      && string(catalog.pack_version) === string(pack.pack_version)
      && string(catalog.manifest_sha256).toLowerCase() === manifestHash
      && SHA256_PATTERN.test(manifestHash),
    );
    if (!ready) errors.push(`能力包 ${id || "unknown"} 与受信任目录不一致`);
    return {
      id,
      name: string(pack.name) || string(catalog.name) || id,
      version: string(pack.pack_version),
      manifestHash,
      systemManaged: pack.system_managed === true,
      adapterIds: safeArray(pack.domain_adapter_ids).map(string).filter(Boolean),
      contributionIds: safeArray(pack.ui_contribution_ids).map(string).filter(Boolean),
      ready,
    };
  });

  const rawAdapters = safeArray(snapshot.domain_adapters);
  if (!uniqueRows(rawAdapters, (adapter) => string(adapter?.adapter_id))) {
    errors.push("领域适配器身份重复或缺失");
  }
  const adapters = rawAdapters.map((raw) => {
    const adapter = record(raw);
    const contractHash = string(adapter.contract_sha256).toLowerCase();
    const ready = adapter.status === "ready" && SHA256_PATTERN.test(contractHash);
    if (!ready) errors.push(`领域适配器 ${string(adapter.adapter_id) || "unknown"} 不可用`);
    return {
      id: string(adapter.adapter_id),
      version: string(adapter.adapter_version),
      contractHash,
      ports: safeArray(adapter.ports).map((port) => ({
        id: string(port?.port_id),
        version: string(port?.port_version),
        contractHash: string(port?.contract_sha256).toLowerCase(),
      })),
      ready,
    };
  });

  const rawContributions = safeArray(snapshot.ui_contributions);
  if (!uniqueRows(rawContributions, (item) => string(item?.contribution_id))) {
    errors.push("UI contribution 身份重复或缺失");
  }
  const contributions = rawContributions.map((raw) => {
    const contribution = record(raw);
    const id = string(contribution.contribution_id);
    const host = HOST_UI_CONTRIBUTIONS[id];
    const contractHash = string(contribution.contract_sha256).toLowerCase();
    const ready = Boolean(
      host
      && !isForbiddenHostSlot(contribution.slot_id)
      && contribution.status === "ready"
      && string(contribution.slot_id) === host.slotId
      && string(contribution.component_key) === host.componentKey
      && SHA256_PATTERN.test(contractHash),
    );
    if (!ready) errors.push(`UI contribution ${id || "unknown"} 未被宿主允许`);
    return {
      id,
      version: string(contribution.contribution_version),
      label: string(contribution.label) || id,
      slotId: string(contribution.slot_id),
      contractHash,
      order: Number.isInteger(contribution.order) ? contribution.order : 0,
      ready,
    };
  }).sort((left, right) => left.order - right.order || left.id.localeCompare(right.id));

  return {
    status: errors.length ? "integrity_failed" : "ready",
    integrityOk: errors.length === 0,
    errors: [...new Set(errors)],
    hash: string(snapshot.registry_snapshot_sha256).toLowerCase(),
    version: string(snapshot.version),
    packs,
    adapters,
    contributions,
    dynamicCodeLoading: snapshot.resolution?.dynamic_code_loading === true,
    safety: FIXED_SAFETY,
  };
}

export function capabilityPackContractMeta(pack) {
  const value = record(pack);
  return {
    version: string(value.pack_version),
    hash: string(value.manifest_sha256).toLowerCase(),
    adapterCount: safeArray(value.domain_adapter_ids).length,
    contributionCount: safeArray(value.ui_contribution_ids).length,
  };
}
