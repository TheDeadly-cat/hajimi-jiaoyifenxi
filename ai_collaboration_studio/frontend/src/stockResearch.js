import {
  HOST_CONTRIBUTION_IDS,
  HOST_SLOT_IDS,
  resolveHostOwnedSlot,
  resolvedHostContribution,
  STOCK_RESEARCH_VIEW_MODEL_SCHEMA_VERSION,
} from "./capabilityContributions.js";

export const STOCK_RESEARCH_PACK_ID = "stock_research_readonly";
export const STOCK_RESEARCH_PORT_ID = "core.market.readonly_context/v1";
export const STOCK_RESEARCH_ACTION_ID = "stock_research.inspect";
export const STOCK_RESEARCH_CONTRACT_VERSION = "stock_research_contract_v1";
export const STOCK_ROOM_SCOPE_VERSION = "stock_room_scope_v1";
export const STOCK_ROUND_CONTEXT_AUTHORIZATION_VERSION = "stock_round_context_authorization_v1";
export const STOCK_ROUND_CONTEXT_REQUEST_VERSION = "stock_round_context_request_v1";
export const STOCK_ROOM_SCOPE_MAX_SYMBOLS = 64;

export const STOCK_PREFLIGHT_SOURCE_TYPES = Object.freeze([
  "futu",
  "sec",
  "investor_relations",
  "price_adjustment",
  "corporate_actions",
]);
export const STOCK_EVIDENCE_CLASSES = Object.freeze([
  "official_fact",
  "media_report",
  "model_inference",
  "market_proxy",
]);

const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const STOCK_SYMBOL_PATTERN = /^[A-Z][A-Z0-9]{1,7}:[A-Z0-9][A-Z0-9.-]{0,31}$/;
const IDENTIFIER_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const MATERIAL_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const UTC_PATTERN = /^(?:19|20|21)[0-9]{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z$/;
const VIEW_MODEL_KEYS = Object.freeze([
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
const CONTRACT_KEYS = Object.freeze([
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
]);
const SYMBOL_KEYS = Object.freeze([
  "symbol",
  "issuer_name",
  "exchange",
  "currency",
  "preflight",
  "evidence",
]);
const PREFLIGHT_ENTRY_KEYS = Object.freeze([
  "version",
  "source_type",
  "status",
  "as_of_utc",
  "reason",
  "source",
]);
const PREFLIGHT_VIEW_KEYS = Object.freeze([
  "version",
  "symbol",
  "research_ready",
  ...STOCK_PREFLIGHT_SOURCE_TYPES,
]);
const PREFLIGHT_SUMMARY_KEYS = Object.freeze(["status", "as_of_utc", "reason"]);
const EVIDENCE_KEYS = Object.freeze([
  "claim_id",
  "symbol",
  "claim",
  "evidence_class",
  "as_of_utc",
  "source",
  "inference",
]);
const SOURCE_KEYS = Object.freeze([
  "source_id",
  "publisher",
  "source_uri",
  "source_sha256",
  "material_binding",
  "published_at_utc",
  "retrieved_at_utc",
]);
const MATERIAL_BINDING_KEYS = Object.freeze([
  "material_id",
  "material_version",
  "content_sha256",
  "snapshot_sha256",
]);
const INFERENCE_KEYS = Object.freeze([
  "method_id",
  "method_version",
  "generated_at_utc",
  "upstream_claim_ids",
]);
const AUTHORIZATION_STATE_KEYS = Object.freeze([
  "version",
  "owner_pack_id",
  "port_id",
  "room_id",
  "capability_pack_id",
  "contract",
  "contract_sha256",
  "stock_room_scope_sha256",
  "data_cutoff_utc",
  "plugin_registry_snapshot_sha256",
  "user_confirmed",
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

function cloneJson(value) {
  return typeof globalThis.structuredClone === "function"
    ? globalThis.structuredClone(value)
    : JSON.parse(JSON.stringify(value));
}

function asciiSort(values) {
  return [...values].sort((left, right) => (left < right ? -1 : left > right ? 1 : 0));
}

export function stockRoomScopeInputState(value, { requireNonempty = true } = {}) {
  const tokens = typeof value === "string"
    ? value.split(/[\s,，;；]+/u).map((item) => item.trim()).filter(Boolean)
    : [];
  const symbols = tokens.map((item) => item.toUpperCase());
  let error = "";
  if (requireNonempty && symbols.length === 0) {
    error = "股票能力包需要至少一个 MARKET:TICKER 标的。";
  } else if (symbols.length > STOCK_ROOM_SCOPE_MAX_SYMBOLS) {
    error = `股票池最多包含 ${STOCK_ROOM_SCOPE_MAX_SYMBOLS} 个标的。`;
  } else if (symbols.some((item) => !STOCK_SYMBOL_PATTERN.test(item))) {
    error = "股票池只能使用 MARKET:TICKER，例如 US:AAPL。";
  } else if (new Set(symbols).size !== symbols.length) {
    error = "股票池不能包含重复标的。";
  }
  const canonicalSymbols = error ? [] : asciiSort(symbols);
  return {
    valid: !error,
    error,
    scope: {
      version: STOCK_ROOM_SCOPE_VERSION,
      symbols: canonicalSymbols,
    },
    canonicalInput: canonicalSymbols.join("\n"),
  };
}

export function stockRoomScopeInputValue(value) {
  const scope = record(value);
  return Array.isArray(scope.symbols)
    ? scope.symbols.filter((item) => typeof item === "string").join("\n")
    : "";
}

export function stockRoomFormSubmission(value) {
  const form = record(value);
  const { stock_room_scope_input: input = "", stock_room_scope: _ignored, ...payload } = form;
  const packIds = Array.isArray(payload.capability_pack_ids) ? payload.capability_pack_ids : [];
  if (!packIds.includes(STOCK_RESEARCH_PACK_ID)) return payload;
  const state = stockRoomScopeInputState(input);
  if (!state.valid) throw new TypeError(state.error);
  return { ...payload, stock_room_scope: state.scope };
}

export function stockResearchInspectorActivation({
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
    HOST_CONTRIBUTION_IDS.stockResearchRoomInspector,
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
    && contribution?.componentKey === "stock_research_inspector"
    && contribution?.packId === STOCK_RESEARCH_PACK_ID
    && sourcePort.ownerPackId === STOCK_RESEARCH_PACK_ID
    && sourcePort.portId === STOCK_RESEARCH_PORT_ID
    && sourcePort.requirement === "required"
    && sourcePort.cardinality === "one"
    && sourceResolution.ownerPackId === STOCK_RESEARCH_PACK_ID
    && sourceResolution.portId === STOCK_RESEARCH_PORT_ID
    && sourceResolution.portVersion === "1.0.0"
    && SHA256_PATTERN.test(string(sourceResolution.portContractHash).toLowerCase())
    && sourceResolution.outputSchemaVersion === "stock_market_readonly_context_output_v1"
    && SHA256_PATTERN.test(string(sourceResolution.outputSchemaHash).toLowerCase())
    && viewModel.schemaVersion === STOCK_RESEARCH_VIEW_MODEL_SCHEMA_VERSION
    && SHA256_PATTERN.test(string(viewModel.schemaHash).toLowerCase())
    && exactStringSet(contribution?.allowedActions, [STOCK_RESEARCH_ACTION_ID])
  );
  return {
    visible: contribution?.present === true,
    active: exactV2,
    reason: exactV2
      ? ""
      : contribution?.reason || slot.reason || "股票只读贡献缺少精确 v2 端口绑定。",
    slot,
    contribution,
  };
}

function canonicalScope(value, { allowEmpty = false } = {}) {
  const scope = record(value);
  const symbols = Array.isArray(scope.symbols) ? scope.symbols : [];
  const valid = exactKeys(scope, ["version", "symbols"])
    && scope.version === STOCK_ROOM_SCOPE_VERSION
    && (allowEmpty || symbols.length > 0)
    && symbols.length <= STOCK_ROOM_SCOPE_MAX_SYMBOLS
    && symbols.every((item) => typeof item === "string" && STOCK_SYMBOL_PATTERN.test(item))
    && new Set(symbols).size === symbols.length
    && JSON.stringify(symbols) === JSON.stringify(asciiSort(symbols));
  return valid ? { version: STOCK_ROOM_SCOPE_VERSION, symbols: [...symbols] } : null;
}

function fixedBoundaries(value) {
  return value.execution_capability === "none"
    && value.live_trading_allowed === false
    && value.order_placement_allowed === false
    && value.wallet_connection_allowed === false
    && value.automatic_trading_allowed === false
    && value.can_autonomously_decide === false
    && value.can_replace_user_decision === false
    && value.user_final_decision_required === true;
}

function canonicalUtc(value) {
  if (typeof value !== "string" || !UTC_PATTERN.test(value)) return null;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp)
    && new Date(timestamp).toISOString().replace(".000Z", "Z") === value
    ? timestamp
    : null;
}

function utcAtOrBefore(value, cutoff) {
  const timestamp = canonicalUtc(value);
  const cutoffTimestamp = canonicalUtc(cutoff);
  return timestamp !== null && cutoffTimestamp !== null && timestamp <= cutoffTimestamp;
}

function canonicalText(value, maximum) {
  return typeof value === "string"
    && value.length > 0
    && value.length <= maximum
    && value === value.trim();
}

function validSource(value, cutoff) {
  const source = record(value);
  const material = record(source.material_binding);
  if (
    !exactKeys(source, SOURCE_KEYS)
    || typeof source.source_id !== "string"
    || !IDENTIFIER_PATTERN.test(source.source_id)
    || !canonicalText(source.publisher, 180)
    || !canonicalText(source.source_uri, 2048)
    || !SHA256_PATTERN.test(source.source_sha256)
    || !exactKeys(material, MATERIAL_BINDING_KEYS)
    || typeof material.material_id !== "string"
    || !MATERIAL_ID_PATTERN.test(material.material_id)
    || !Number.isInteger(material.material_version)
    || material.material_version < 1
    || material.material_version > 2_147_483_647
    || !SHA256_PATTERN.test(material.content_sha256)
    || !SHA256_PATTERN.test(material.snapshot_sha256)
    || source.source_sha256 !== material.content_sha256
    || (source.published_at_utc !== null && !utcAtOrBefore(source.published_at_utc, cutoff))
    || !utcAtOrBefore(source.retrieved_at_utc, cutoff)
  ) return false;
  let url;
  try {
    url = new URL(source.source_uri);
  } catch {
    return false;
  }
  if (url.protocol === "https:") return Boolean(url.host);
  if (url.protocol !== "urn:") return false;
  return source.source_uri
    === `urn:ai-studio:material:${material.material_id}:v${material.material_version}`;
}

function validPreflightEntry(value, sourceType, cutoff) {
  const entry = record(value);
  return exactKeys(entry, PREFLIGHT_ENTRY_KEYS)
    && entry.version === "stock_source_preflight_v1"
    && entry.source_type === sourceType
    && ["ready", "unavailable"].includes(entry.status)
    && utcAtOrBefore(entry.as_of_utc, cutoff)
    && typeof entry.reason === "string"
    && entry.reason.length <= 500
    && (entry.status === "ready"
      ? !string(entry.reason) && validSource(entry.source, cutoff)
      : Boolean(string(entry.reason)) && (entry.source === null || validSource(entry.source, cutoff)));
}

function validPreflightView(value, stock) {
  const view = record(value);
  if (
    !exactKeys(view, PREFLIGHT_VIEW_KEYS)
    || view.version !== "stock_symbol_preflight_view_v1"
    || view.symbol !== stock.symbol
    || typeof view.research_ready !== "boolean"
  ) return false;
  const allReady = STOCK_PREFLIGHT_SOURCE_TYPES.every((sourceType) => {
    const summary = record(view[sourceType]);
    const entry = record(stock.preflight)[sourceType];
    return exactKeys(summary, PREFLIGHT_SUMMARY_KEYS)
      && summary.status === entry.status
      && summary.as_of_utc === entry.as_of_utc
      && summary.reason === entry.reason;
  });
  return allReady && view.research_ready
    === STOCK_PREFLIGHT_SOURCE_TYPES.every((sourceType) => view[sourceType].status === "ready");
}

function validEvidence(value, symbol, cutoff) {
  const claim = record(value);
  const inference = record(claim.inference);
  const upstream = Array.isArray(inference.upstream_claim_ids)
    ? inference.upstream_claim_ids
    : [];
  const inferenceValid = claim.evidence_class === "model_inference"
    ? exactKeys(inference, INFERENCE_KEYS)
      && typeof inference.method_id === "string"
      && IDENTIFIER_PATTERN.test(inference.method_id)
      && canonicalText(inference.method_version, 80)
      && utcAtOrBefore(inference.generated_at_utc, cutoff)
      && upstream.length > 0
      && upstream.every((item) => typeof item === "string" && IDENTIFIER_PATTERN.test(item))
      && new Set(upstream).size === upstream.length
    : claim.inference === null;
  return exactKeys(claim, EVIDENCE_KEYS)
    && typeof claim.claim_id === "string"
    && IDENTIFIER_PATTERN.test(claim.claim_id)
    && claim.symbol === symbol
    && canonicalText(claim.claim, 4000)
    && STOCK_EVIDENCE_CLASSES.includes(claim.evidence_class)
    && utcAtOrBefore(claim.as_of_utc, cutoff)
    && validSource(claim.source, cutoff)
    && inferenceValid;
}

function validInferenceGraph(claims) {
  const byId = new Map(claims.map((claim) => [claim.claim_id, claim]));
  if (byId.size !== claims.length) return false;
  const visiting = new Set();
  const visited = new Set();
  const visit = (claimId) => {
    if (visiting.has(claimId)) return false;
    if (visited.has(claimId)) return true;
    const claim = byId.get(claimId);
    if (!claim) return false;
    visiting.add(claimId);
    const upstream = claim.inference?.upstream_claim_ids || [];
    if (upstream.includes(claimId) || upstream.some((item) => !visit(item))) return false;
    visiting.delete(claimId);
    visited.add(claimId);
    return true;
  };
  return [...byId.keys()].every(visit);
}

export function stockEvidenceClaims(contractValue) {
  const contract = record(contractValue);
  return (Array.isArray(contract.symbols) ? contract.symbols : []).flatMap((stock) => (
    Array.isArray(stock?.evidence) ? stock.evidence.map((claim) => ({
      symbol: string(stock.symbol),
      claimId: string(claim?.claim_id),
      claim: string(claim?.claim),
      evidenceClass: string(claim?.evidence_class),
      asOfUtc: string(claim?.as_of_utc),
      publisher: string(claim?.source?.publisher),
      sourceUri: string(claim?.source?.source_uri),
      materialId: string(claim?.source?.material_binding?.material_id),
      materialVersion: Number(claim?.source?.material_binding?.material_version) || 0,
    })) : []
  ));
}

export function normalizeStockResearchResponse(payload, expected = {}) {
  const raw = record(record(payload).stock_research || payload);
  const contract = record(raw.contract);
  const scope = canonicalScope(raw.stock_room_scope);
  const contractScope = canonicalScope(contract.stock_room_scope);
  const stocks = Array.isArray(contract.symbols) ? contract.symbols : [];
  const cutoff = string(contract.data_cutoff_utc);
  const preflightViews = Array.isArray(raw.symbol_preflights) ? raw.symbol_preflights : [];
  const stockSymbols = stocks.map((item) => string(item?.symbol));
  const rowsValid = stocks.length > 0 && stocks.every((stock) => (
    exactKeys(stock, SYMBOL_KEYS)
    && typeof stock.symbol === "string"
    && STOCK_SYMBOL_PATTERN.test(stock.symbol)
    && stock.exchange === stock.symbol.split(":", 1)[0]
    && canonicalText(stock.issuer_name, 180)
    && canonicalText(stock.exchange, 16)
    && canonicalText(stock.currency, 8)
    && /^[A-Z]{3,8}$/.test(stock.currency)
    && exactKeys(stock.preflight, STOCK_PREFLIGHT_SOURCE_TYPES)
    && STOCK_PREFLIGHT_SOURCE_TYPES.every((sourceType) => (
      validPreflightEntry(stock.preflight[sourceType], sourceType, cutoff)
    ))
    && Array.isArray(stock.evidence)
    && stock.evidence.every((claim) => validEvidence(claim, stock.symbol, cutoff))
  ));
  const claims = stocks.flatMap((stock) => stock.evidence || []);
  const allReady = rowsValid && stocks.every((stock) => (
    STOCK_PREFLIGHT_SOURCE_TYPES.every((sourceType) => stock.preflight[sourceType].status === "ready")
  ));
  const expectedRoomId = string(expected.roomId);
  const expectedScope = expected.stockRoomScope ? canonicalScope(expected.stockRoomScope) : null;
  const rawContractHash = typeof raw.contract_sha256 === "string"
    ? raw.contract_sha256
    : "";
  const contractHash = rawContractHash.toLowerCase();
  const valid = Boolean(
    exactKeys(raw, VIEW_MODEL_KEYS)
    && exactKeys(contract, CONTRACT_KEYS)
    && raw.version === STOCK_RESEARCH_VIEW_MODEL_SCHEMA_VERSION
    && raw.integrity_ok === true
    && raw.metrics_visible === true
    && (!expectedRoomId || string(raw.room_id) === expectedRoomId)
    && fixedBoundaries(raw)
    && contract.version === STOCK_RESEARCH_CONTRACT_VERSION
    && contract.capability_pack_id === STOCK_RESEARCH_PACK_ID
    && fixedBoundaries(contract)
    && scope
    && contractScope
    && JSON.stringify(scope) === JSON.stringify(contractScope)
    && (!expectedScope || JSON.stringify(scope) === JSON.stringify(expectedScope))
    && rowsValid
    && validInferenceGraph(claims)
    && JSON.stringify(stockSymbols) === JSON.stringify(scope.symbols)
    && JSON.stringify(stockSymbols) === JSON.stringify(asciiSort(stockSymbols))
    && preflightViews.length === stocks.length
    && preflightViews.every((view, index) => validPreflightView(view, stocks[index]))
    && raw.research_ready === allReady
    && contract.research_ready === allReady
    && canonicalUtc(raw.data_cutoff_utc) !== null
    && string(raw.data_cutoff_utc) === cutoff
    && SHA256_PATTERN.test(contractHash)
    && rawContractHash === contractHash
    && contract.contract_sha256 === contractHash
    && raw.provider_calls_performed === 0
    && raw.market_reads_performed === 0
    && raw.business_writes_performed === 0
  );
  return valid ? {
    valid: true,
    reason: "",
    raw,
    contract,
    stockRoomScope: scope,
    claims: stockEvidenceClaims(contract),
  } : {
    valid: false,
    reason: "股票研究返回未通过房间股票池、五项预检、证据分类或固定安全边界校验。",
    raw: null,
    contract: null,
    stockRoomScope: null,
    claims: [],
  };
}

export function parseStockResearchJson(source) {
  const text = string(source);
  if (!text) throw new Error("请粘贴或导入股票研究 JSON。");
  let payload;
  try {
    payload = JSON.parse(text);
  } catch {
    throw new Error("JSON 格式无效，请检查逗号、引号与括号。");
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("股票研究 JSON 顶层必须是对象。");
  }
  return payload;
}

export function buildStockRoundContextAuthorization(view, activation, room) {
  const roomScope = canonicalScope(room?.stock_room_scope);
  const contract = record(view?.contract);
  const contractHash = string(contract.contract_sha256).toLowerCase();
  const scopeHash = string(room?.stock_room_scope_sha256).toLowerCase();
  const registryHash = string(activation?.slot?.snapshotSha256).toLowerCase();
  if (
    !view?.valid
    || activation?.active !== true
    || !roomScope
    || room?.stock_room_scope_integrity_ok !== true
    || !SHA256_PATTERN.test(contractHash)
    || !SHA256_PATTERN.test(scopeHash)
    || !SHA256_PATTERN.test(registryHash)
    || JSON.stringify(roomScope) !== JSON.stringify(view.stockRoomScope)
  ) throw new Error("只有精确匹配房间股票池与 v2 端口的已核验合同才能显式用于下一轮。");
  return {
    version: STOCK_ROUND_CONTEXT_AUTHORIZATION_VERSION,
    owner_pack_id: STOCK_RESEARCH_PACK_ID,
    port_id: STOCK_RESEARCH_PORT_ID,
    room_id: string(view.raw?.room_id),
    capability_pack_id: STOCK_RESEARCH_PACK_ID,
    contract: cloneJson(contract),
    contract_sha256: contractHash,
    stock_room_scope_sha256: scopeHash,
    data_cutoff_utc: string(contract.data_cutoff_utc),
    plugin_registry_snapshot_sha256: registryHash,
    user_confirmed: true,
  };
}

export function stockRoundContextAuthorizationState(value, expected = {}) {
  const authorization = record(value);
  const contract = record(authorization.contract);
  const scope = canonicalScope(contract.stock_room_scope);
  const valid = exactKeys(authorization, AUTHORIZATION_STATE_KEYS)
    && authorization.version === STOCK_ROUND_CONTEXT_AUTHORIZATION_VERSION
    && authorization.owner_pack_id === STOCK_RESEARCH_PACK_ID
    && authorization.port_id === STOCK_RESEARCH_PORT_ID
    && authorization.capability_pack_id === STOCK_RESEARCH_PACK_ID
    && authorization.user_confirmed === true
    && fixedBoundaries(contract)
    && Boolean(scope)
    && string(authorization.room_id) === string(expected.roomId)
    && SHA256_PATTERN.test(string(authorization.contract_sha256).toLowerCase())
    && string(authorization.contract_sha256).toLowerCase()
      === string(contract.contract_sha256).toLowerCase()
    && SHA256_PATTERN.test(string(authorization.stock_room_scope_sha256).toLowerCase())
    && SHA256_PATTERN.test(string(authorization.plugin_registry_snapshot_sha256).toLowerCase())
    && string(authorization.data_cutoff_utc) === string(contract.data_cutoff_utc)
    && (!expected.contractSha256
      || string(authorization.contract_sha256).toLowerCase()
        === string(expected.contractSha256).toLowerCase())
    && (!expected.stockRoomScopeSha256
      || string(authorization.stock_room_scope_sha256).toLowerCase()
        === string(expected.stockRoomScopeSha256).toLowerCase())
    && (!expected.pluginRegistrySnapshotSha256
      || string(authorization.plugin_registry_snapshot_sha256).toLowerCase()
        === string(expected.pluginRegistrySnapshotSha256).toLowerCase());
  return { valid: Boolean(valid), authorization: valid ? authorization : null };
}
