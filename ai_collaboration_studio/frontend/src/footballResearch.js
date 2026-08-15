import {
  FOOTBALL_RESEARCH_VIEW_MODEL_SCHEMA_VERSION,
  HOST_CONTRIBUTION_IDS,
  HOST_SLOT_IDS,
  resolveHostOwnedSlot,
  resolvedHostContribution,
} from "./capabilityContributions.js";

export const FOOTBALL_RESEARCH_PACK_ID = "football_research_readonly";
export const FOOTBALL_RESEARCH_PORT_ID = "core.football.match_context/v1";
export const FOOTBALL_RESEARCH_ACTION_ID = "football_research.inspect";
export const FOOTBALL_RESEARCH_CONTRACT_VERSION = "football_research_contract_v1";
export const FOOTBALL_PROBABILITY_STATE = "withheld_no_calibration";
export const FOOTBALL_ROUND_CONTEXT_AUTHORIZATION_VERSION = "football_round_context_authorization_v1";

export const FOOTBALL_EVIDENCE_CLASSES = Object.freeze([
  "official_fact",
  "media_report",
  "model_inference",
  "odds_proxy",
]);

const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const VIEW_MODEL_KEYS = Object.freeze([
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

function record(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function string(value) {
  return typeof value === "string" ? value.trim() : "";
}

function exactKeys(value, expected) {
  const actual = Object.keys(record(value)).sort();
  const wanted = [...expected].sort();
  return actual.length === wanted.length
    && actual.every((key, index) => key === wanted[index]);
}

function exactStringSet(value, expected) {
  const actual = Array.isArray(value) ? value.map(string).filter(Boolean) : [];
  return actual.length === expected.length
    && new Set(actual).size === actual.length
    && expected.every((item) => actual.includes(item));
}

export function footballResearchInspectorActivation({
  frozenContext,
  runtimeContext = null,
  pluginRegistry,
  pluginLifecycle,
}) {
  const slot = resolveHostOwnedSlot({
    slotId: HOST_SLOT_IDS.roomInspector,
    frozenContext,
    pluginRegistry,
    pluginLifecycle,
    runtimeContext: runtimeContext || frozenContext,
  });
  const contribution = resolvedHostContribution(
    slot,
    HOST_CONTRIBUTION_IDS.footballResearchRoomInspector,
  );
  const sourcePort = record(contribution?.sourcePort);
  const sourceResolution = record(contribution?.sourcePortResolution);
  const viewModel = record(contribution?.viewModel);
  const exactV2 = Boolean(
    slot.integrityOk === true
    && slot.status === "ready"
    && slot.runtimeAvailable === true
    && slot.snapshotVersion === "plugin_registry_snapshot_v2"
    && contribution?.present === true
    && contribution?.active === true
    && contribution?.contractVersion === "ui_contribution_contract_v2"
    && contribution?.componentKey === "football_research_inspector"
    && contribution?.packId === FOOTBALL_RESEARCH_PACK_ID
    && sourcePort.ownerPackId === FOOTBALL_RESEARCH_PACK_ID
    && sourcePort.portId === FOOTBALL_RESEARCH_PORT_ID
    && sourcePort.requirement === "required"
    && sourcePort.cardinality === "one"
    && sourceResolution.ownerPackId === FOOTBALL_RESEARCH_PACK_ID
    && sourceResolution.portId === FOOTBALL_RESEARCH_PORT_ID
    && sourceResolution.portVersion === "1.0.0"
    && SHA256_PATTERN.test(string(sourceResolution.portContractHash).toLowerCase())
    && sourceResolution.outputSchemaVersion === "football_match_context_output_v1"
    && SHA256_PATTERN.test(string(sourceResolution.outputSchemaHash).toLowerCase())
    && viewModel.schemaVersion === FOOTBALL_RESEARCH_VIEW_MODEL_SCHEMA_VERSION
    && SHA256_PATTERN.test(string(viewModel.schemaHash).toLowerCase())
    && exactStringSet(contribution?.allowedActions, [FOOTBALL_RESEARCH_ACTION_ID])
  );
  return {
    visible: contribution?.present === true,
    active: exactV2,
    reason: exactV2
      ? ""
      : contribution?.reason || slot.reason || "足球只读贡献缺少精确 v2 端口绑定。",
    slot,
    contribution,
  };
}

function fixedViewModelBoundaries(value) {
  return value.integrity_ok === true
    && value.metrics_visible === false
    && value.probability_state === FOOTBALL_PROBABILITY_STATE
    && value.future_probability_available === false
    && value.probability_metrics_visible === false
    && value.odds_are_proxy_only === true
    && value.provider_calls_performed === 0
    && value.market_reads_performed === 0
    && value.business_writes_performed === 0
    && value.execution_capability === "none"
    && value.live_trading_allowed === false
    && value.betting_allowed === false
    && value.automatic_betting_allowed === false
    && value.wallet_connection_allowed === false
    && value.order_placement_allowed === false
    && value.can_autonomously_decide === false
    && value.can_replace_user_decision === false
    && value.user_final_decision_required === true;
}

function fixedContractBoundaries(contract) {
  return contract.version === FOOTBALL_RESEARCH_CONTRACT_VERSION
    && contract.capability_pack_id === FOOTBALL_RESEARCH_PACK_ID
    && contract.probability_state === FOOTBALL_PROBABILITY_STATE
    && contract.future_probability_available === false
    && contract.probability_metrics_visible === false
    && contract.odds_are_proxy_only === true
    && contract.execution_capability === "none"
    && contract.betting_allowed === false
    && contract.live_betting_allowed === false
    && contract.automatic_betting_allowed === false
    && contract.wallet_connection_allowed === false
    && contract.order_placement_allowed === false
    && contract.can_autonomously_decide === false
    && contract.can_replace_user_decision === false
    && contract.user_final_decision_required === true;
}

export function footballEvidenceClaims(contractValue) {
  const claims = [];
  const seen = new Set();
  const visit = (value, path) => {
    if (Array.isArray(value)) {
      value.forEach((item, index) => visit(item, `${path}[${index}]`));
      return;
    }
    if (!value || typeof value !== "object") return;
    if (
      typeof value.claim_id === "string"
      && FOOTBALL_EVIDENCE_CLASSES.includes(value.evidence_class)
      && value.source
      && typeof value.source === "object"
    ) {
      const identity = `${value.claim_id}|${path}`;
      if (!seen.has(identity)) {
        seen.add(identity);
        claims.push({
          path,
          claimId: value.claim_id,
          evidenceClass: value.evidence_class,
          asOfUtc: string(value.as_of_utc),
          publisher: string(value.source.publisher),
          sourceUri: string(value.source.source_uri),
          publicationState: string(value.source.publication?.state),
          publishedAtUtc: string(value.source.publication?.published_at_utc),
          observedAtUtc: string(value.source.publication?.observed_at_utc),
          materialId: string(value.source.material_binding?.material_id),
          materialVersion: Number(value.source.material_binding?.material_version) || 0,
        });
      }
    }
    Object.entries(value).forEach(([key, child]) => visit(child, path ? `${path}.${key}` : key));
  };
  visit(record(contractValue), "");
  return claims;
}

export function normalizeFootballResearchResponse(payload, expectedRoomId) {
  const raw = record(record(payload).football_research || payload);
  const contract = record(raw.contract);
  const claims = footballEvidenceClaims(contract);
  const invalidEvidenceClass = (() => {
    let invalid = false;
    const visit = (value) => {
      if (invalid || !value || typeof value !== "object") return;
      if (Array.isArray(value)) {
        value.forEach(visit);
        return;
      }
      if (Object.hasOwn(value, "evidence_class")
        && !FOOTBALL_EVIDENCE_CLASSES.includes(value.evidence_class)) invalid = true;
      Object.values(value).forEach(visit);
    };
    visit(contract);
    return invalid;
  })();
  const roomId = string(raw.room_id);
  const contractHash = string(raw.contract_sha256).toLowerCase();
  const contractStoredHash = string(contract.contract_sha256).toLowerCase();
  const valid = Boolean(exactKeys(raw, VIEW_MODEL_KEYS)
    && raw.version === FOOTBALL_RESEARCH_VIEW_MODEL_SCHEMA_VERSION
    && (!expectedRoomId || roomId === string(expectedRoomId))
    && fixedViewModelBoundaries(raw)
    && fixedContractBoundaries(contract)
    && SHA256_PATTERN.test(contractHash)
    && contractStoredHash === contractHash
    && string(raw.data_cutoff_utc) === string(contract.data_cutoff_utc)
    && record(contract.match_identity).match_id
    && record(contract.teams).home
    && record(contract.teams).away
    && !invalidEvidenceClass);
  if (!valid) {
    return {
      valid: false,
      reason: "足球研究返回未通过闭合视图、精确房间或固定安全边界校验。",
      raw: null,
      contract: null,
      claims: [],
    };
  }
  return {
    valid: true,
    reason: "",
    raw,
    contract,
    claims,
  };
}

export function parseFootballResearchJson(source) {
  const text = string(source);
  if (!text) throw new Error("请粘贴或导入足球研究 JSON。");
  let payload;
  try {
    payload = JSON.parse(text);
  } catch {
    throw new Error("JSON 格式无效，请检查逗号、引号与括号。");
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("足球研究 JSON 顶层必须是对象。");
  }
  return payload;
}

export function footballEvidenceValue(field, fallback = null) {
  return field && typeof field === "object" && Object.hasOwn(field, "value")
    ? field.value
    : fallback;
}

function cloneJson(value) {
  return typeof globalThis.structuredClone === "function"
    ? globalThis.structuredClone(value)
    : JSON.parse(JSON.stringify(value));
}

export function buildFootballRoundContextAuthorization(view, activation) {
  if (!view?.valid || activation?.active !== true) {
    throw new Error("只有通过精确 v2 贡献检查的足球合同才能显式用于下一轮。");
  }
  const contract = record(view.contract);
  const matchId = string(footballEvidenceValue(contract.match_identity?.match_id));
  const dataCutoffUtc = string(contract.data_cutoff_utc);
  const contractSha256 = string(contract.contract_sha256).toLowerCase();
  const registrySha256 = string(activation.slot?.snapshotSha256).toLowerCase();
  if (
    !matchId
    || !dataCutoffUtc
    || !SHA256_PATTERN.test(contractSha256)
    || !SHA256_PATTERN.test(registrySha256)
  ) throw new Error("足球合同缺少比赛、截止时间或精确封印哈希。");
  return {
    version: FOOTBALL_ROUND_CONTEXT_AUTHORIZATION_VERSION,
    room_id: string(view.raw?.room_id),
    capability_pack_id: FOOTBALL_RESEARCH_PACK_ID,
    match_id: matchId,
    data_cutoff_utc: dataCutoffUtc,
    contract: cloneJson(contract),
    contract_sha256: contractSha256,
    plugin_registry_snapshot_sha256: registrySha256,
    user_authorized: true,
  };
}

export function footballRoundContextAuthorizationState(value, expected = {}) {
  const authorization = record(value);
  const contract = record(authorization.contract);
  const valid = authorization.version === FOOTBALL_ROUND_CONTEXT_AUTHORIZATION_VERSION
    && authorization.capability_pack_id === FOOTBALL_RESEARCH_PACK_ID
    && authorization.user_authorized === true
    && string(authorization.room_id) === string(expected.roomId)
    && string(authorization.match_id)
      === string(footballEvidenceValue(contract.match_identity?.match_id))
    && string(authorization.data_cutoff_utc) === string(contract.data_cutoff_utc)
    && string(authorization.contract_sha256).toLowerCase()
      === string(contract.contract_sha256).toLowerCase()
    && SHA256_PATTERN.test(string(authorization.contract_sha256).toLowerCase())
    && SHA256_PATTERN.test(string(authorization.plugin_registry_snapshot_sha256).toLowerCase())
    && (!expected.contractSha256
      || string(authorization.contract_sha256).toLowerCase()
        === string(expected.contractSha256).toLowerCase())
    && (!expected.pluginRegistrySnapshotSha256
      || string(authorization.plugin_registry_snapshot_sha256).toLowerCase()
        === string(expected.pluginRegistrySnapshotSha256).toLowerCase());
  return { valid: Boolean(valid), authorization: valid ? authorization : null };
}
