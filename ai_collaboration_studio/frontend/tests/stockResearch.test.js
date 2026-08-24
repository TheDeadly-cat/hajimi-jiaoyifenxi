import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { api } from "../src/api.js";
import {
  STOCK_EVIDENCE_CLASSES,
  STOCK_PREFLIGHT_SOURCE_TYPES,
  STOCK_RESEARCH_PACK_ID,
  STOCK_ROOM_SCOPE_MAX_SYMBOLS,
  buildStockRoundContextAuthorization,
  normalizeStockResearchResponse,
  parseStockResearchJson,
  stockResearchInspectorActivation,
  stockRoomFormSubmission,
  stockRoomScopeInputState,
  stockRoundContextAuthorizationState,
} from "../src/stockResearch.js";

const HASH = "a".repeat(64);
const SNAPSHOT_HASH = "b".repeat(64);
const SCOPE_HASH = "c".repeat(64);
const REGISTRY_HASH = "d".repeat(64);
const CUTOFF = "2026-08-12T10:00:00Z";

function source(id) {
  return {
    source_id: `source-${id}`,
    publisher: `Publisher ${id}`,
    source_uri: `urn:ai-studio:material:material-${id}:v1`,
    source_sha256: HASH,
    material_binding: {
      material_id: `material-${id}`,
      material_version: 1,
      content_sha256: HASH,
      snapshot_sha256: SNAPSHOT_HASH,
    },
    published_at_utc: "2026-08-12T08:00:00Z",
    retrieved_at_utc: "2026-08-12T09:00:00Z",
  };
}

function preflight(sourceType) {
  return {
    version: "stock_source_preflight_v1",
    source_type: sourceType,
    status: "ready",
    as_of_utc: "2026-08-12T09:00:00Z",
    reason: "",
    source: source(`aapl-${sourceType}`),
  };
}

function claim(evidenceClass, index) {
  return {
    claim_id: `claim-${evidenceClass}`,
    symbol: "US:AAPL",
    claim: `${evidenceClass} evidence for the sealed stock pool`,
    evidence_class: evidenceClass,
    as_of_utc: "2026-08-12T09:00:00Z",
    source: source(`claim-${index}`),
    inference: evidenceClass === "model_inference" ? {
      method_id: "fixture-method",
      method_version: "1.0.0",
      generated_at_utc: "2026-08-12T09:15:00Z",
      upstream_claim_ids: [
        "claim-official_fact",
        "claim-media_report",
        "claim-market_proxy",
      ],
    } : null,
  };
}

function contractFixture() {
  return {
    version: "stock_research_contract_v1",
    capability_pack_id: STOCK_RESEARCH_PACK_ID,
    stock_room_scope: {
      version: "stock_room_scope_v1",
      symbols: ["US:AAPL"],
    },
    data_cutoff_utc: CUTOFF,
    symbols: [{
      symbol: "US:AAPL",
      issuer_name: "Apple Inc.",
      exchange: "US",
      currency: "USD",
      preflight: Object.fromEntries(
        STOCK_PREFLIGHT_SOURCE_TYPES.map((sourceType) => [sourceType, preflight(sourceType)]),
      ),
      evidence: STOCK_EVIDENCE_CLASSES.map(claim),
    }],
    research_ready: true,
    execution_capability: "none",
    live_trading_allowed: false,
    order_placement_allowed: false,
    wallet_connection_allowed: false,
    automatic_trading_allowed: false,
    can_autonomously_decide: false,
    can_replace_user_decision: false,
    user_final_decision_required: true,
    contract_sha256: HASH,
  };
}

function responseFixture() {
  const contract = contractFixture();
  return {
    stock_research: {
      version: "stock_research_view_model_v1",
      integrity_ok: true,
      metrics_visible: true,
      room_id: "room/股票",
      stock_room_scope: structuredClone(contract.stock_room_scope),
      contract,
      contract_sha256: HASH,
      data_cutoff_utc: CUTOFF,
      research_ready: true,
      symbol_preflights: [{
        version: "stock_symbol_preflight_view_v1",
        symbol: "US:AAPL",
        research_ready: true,
        ...Object.fromEntries(STOCK_PREFLIGHT_SOURCE_TYPES.map((sourceType) => [sourceType, {
          status: "ready",
          as_of_utc: "2026-08-12T09:00:00Z",
          reason: "",
        }])),
      }],
      provider_calls_performed: 0,
      market_reads_performed: 0,
      business_writes_performed: 0,
      execution_capability: "none",
      live_trading_allowed: false,
      order_placement_allowed: false,
      wallet_connection_allowed: false,
      automatic_trading_allowed: false,
      can_autonomously_decide: false,
      can_replace_user_decision: false,
      user_final_decision_required: true,
    },
  };
}

const ROOM = {
  id: "room/股票",
  stock_room_scope: { version: "stock_room_scope_v1", symbols: ["US:AAPL"] },
  stock_room_scope_sha256: SCOPE_HASH,
  stock_room_scope_integrity_ok: true,
};

test("explicit stock-pool input canonicalizes, deduplicates and enforces the 64-symbol ceiling", () => {
  const normalized = stockRoomScopeInputState("us:msft， US:AAPL");
  assert.equal(normalized.valid, true);
  assert.deepEqual(normalized.scope, {
    version: "stock_room_scope_v1",
    symbols: ["US:AAPL", "US:MSFT"],
  });
  assert.equal(normalized.canonicalInput, "US:AAPL\nUS:MSFT");
  assert.equal(stockRoomScopeInputState("US:AAPL us:aapl").valid, false);
  assert.equal(stockRoomScopeInputState("AAPL").valid, false);
  assert.equal(stockRoomScopeInputState("").valid, false);

  const tooMany = Array.from(
    { length: STOCK_ROOM_SCOPE_MAX_SYMBOLS + 1 },
    (_, index) => `US:S${index}`,
  ).join("\n");
  assert.match(stockRoomScopeInputState(tooMany).error, /最多包含 64/);
});

test("room form submissions send stock_room_scope_v1 only while the stock pack is selected", () => {
  const stock = stockRoomFormSubmission({
    title: "Stock room",
    capability_pack_ids: [STOCK_RESEARCH_PACK_ID],
    stock_room_scope_input: "us:msft\nUS:AAPL",
    stock_room_scope: { version: "stale", symbols: ["US:OLD"] },
  });
  assert.deepEqual(stock.stock_room_scope, {
    version: "stock_room_scope_v1",
    symbols: ["US:AAPL", "US:MSFT"],
  });
  assert.equal(Object.hasOwn(stock, "stock_room_scope_input"), false);

  const nonStock = stockRoomFormSubmission({
    capability_pack_ids: ["structured_project_research"],
    stock_room_scope_input: "US:AAPL",
    stock_room_scope: { version: "stock_room_scope_v1", symbols: ["US:AAPL"] },
  });
  assert.equal(Object.hasOwn(nonStock, "stock_room_scope"), false);
  assert.equal(Object.hasOwn(nonStock, "stock_room_scope_input"), false);
  assert.throws(() => stockRoomFormSubmission({
    capability_pack_ids: [STOCK_RESEARCH_PACK_ID],
    stock_room_scope_input: "",
  }), /至少一个/);
});

test("stock response exposes five preflights and four evidence classes only after closed safety validation", () => {
  const view = normalizeStockResearchResponse(responseFixture(), {
    roomId: ROOM.id,
    stockRoomScope: ROOM.stock_room_scope,
  });
  assert.equal(view.valid, true);
  assert.deepEqual(
    [...new Set(view.claims.map((item) => item.evidenceClass))].sort(),
    [...STOCK_EVIDENCE_CLASSES].sort(),
  );
  assert.deepEqual(
    Object.keys(view.contract.symbols[0].preflight).sort(),
    [...STOCK_PREFLIGHT_SOURCE_TYPES].sort(),
  );

  const extra = structuredClone(responseFixture());
  extra.stock_research.contract.symbols[0].preflight.sec.source.wallet = true;
  assert.equal(normalizeStockResearchResponse(extra, { roomId: ROOM.id }).valid, false);

  const futureEvidence = structuredClone(responseFixture());
  futureEvidence.stock_research.contract.symbols[0].evidence[0].as_of_utc = "2026-08-12T11:00:00Z";
  assert.equal(normalizeStockResearchResponse(futureEvidence, { roomId: ROOM.id }).valid, false);

  const unsafe = structuredClone(responseFixture());
  unsafe.stock_research.order_placement_allowed = true;
  assert.equal(normalizeStockResearchResponse(unsafe, { roomId: ROOM.id }).valid, false);

  const brokenInference = structuredClone(responseFixture());
  brokenInference.stock_research.contract.symbols[0].evidence
    .find((item) => item.evidence_class === "model_inference")
    .inference.upstream_claim_ids = ["claim-not-present"];
  assert.equal(normalizeStockResearchResponse(brokenInference, { roomId: ROOM.id }).valid, false);
});

test("local stock JSON parsing is object-only and performs no discovery", () => {
  assert.deepEqual(parseStockResearchJson('{"symbols":[]}'), { symbols: [] });
  assert.throws(() => parseStockResearchJson(""), /粘贴或导入/);
  assert.throws(() => parseStockResearchJson("[]"), /顶层必须是对象/);
  assert.throws(() => parseStockResearchJson("{"), /JSON 格式无效/);
});

test("explicit next-round authorization binds room, registry, stock scope and contract drift", () => {
  const view = normalizeStockResearchResponse(responseFixture(), {
    roomId: ROOM.id,
    stockRoomScope: ROOM.stock_room_scope,
  });
  const activation = { active: true, slot: { snapshotSha256: REGISTRY_HASH } };
  const authorization = buildStockRoundContextAuthorization(view, activation, ROOM);

  assert.equal(authorization.user_confirmed, true);
  assert.equal(authorization.room_id, ROOM.id);
  assert.equal(authorization.owner_pack_id, STOCK_RESEARCH_PACK_ID);
  assert.equal(authorization.port_id, "core.market.readonly_context/v1");
  assert.equal(authorization.contract_sha256, HASH);
  assert.equal(authorization.stock_room_scope_sha256, SCOPE_HASH);
  assert.equal(authorization.plugin_registry_snapshot_sha256, REGISTRY_HASH);
  assert.notEqual(authorization.contract, view.contract);
  assert.equal(stockRoundContextAuthorizationState(authorization, {
    roomId: ROOM.id,
    contractSha256: HASH,
    stockRoomScopeSha256: SCOPE_HASH,
    pluginRegistrySnapshotSha256: REGISTRY_HASH,
  }).valid, true);
  assert.equal(stockRoundContextAuthorizationState(authorization, {
    roomId: ROOM.id,
    stockRoomScopeSha256: "e".repeat(64),
    pluginRegistrySnapshotSha256: REGISTRY_HASH,
  }).valid, false);
  assert.throws(
    () => buildStockRoundContextAuthorization(view, { active: false, slot: {} }, ROOM),
    /精确匹配/,
  );
});

test("stock inspector API encodes the room and posts only the caller payload", async () => {
  const originalFetch = globalThis.fetch;
  let request;
  const payload = { stock_room_scope: ROOM.stock_room_scope };
  const controller = new AbortController();
  globalThis.fetch = async (path, options) => {
    request = { path, options };
    return { ok: true, status: 200, json: async () => responseFixture() };
  };
  try {
    await api.inspectStockResearch(ROOM.id, payload, controller.signal);
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(request.path, "/api/rooms/room%2F%E8%82%A1%E7%A5%A8/stock-research/inspect");
  assert.equal(request.options.method, "POST");
  assert.equal(request.options.signal, controller.signal);
  assert.deepEqual(JSON.parse(request.options.body), { payload });
});

test("host UI keeps stock research static, explicit and free of candidate controls", () => {
  const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
  const panelSource = readFileSync(
    new URL("../src/components/StockResearchPanel.jsx", import.meta.url),
    "utf8",
  );
  const inspectorSource = readFileSync(
    new URL("../src/components/RoomInspector.jsx", import.meta.url),
    "utf8",
  );
  const createSource = readFileSync(
    new URL("../src/components/Dialogs.jsx", import.meta.url),
    "utf8",
  );
  const settingsSource = readFileSync(
    new URL("../src/components/RoomSettingsDialog.jsx", import.meta.url),
    "utf8",
  );

  assert.match(appSource, /stockResearchInspectorActivation\(/);
  assert.match(appSource, /<StockResearchPanel/);
  assert.match(appSource, /stock_round_context_request_v1/);
  assert.match(panelSource, /显式用于下一轮/);
  assert.match(panelSource, /逐标的五项预检/);
  assert.match(panelSource, /四类证据严格分层/);
  assert.match(panelSource, /type="file" accept="\.json,application\/json"/);
  assert.doesNotMatch(panelSource, /CandidateExperiment|createCandidateExperiment|候选实验/);
  assert.match(inspectorSource, /HOST_CONTRIBUTION_IDS\.stockResearchRoomInspector/);
  assert.match(createSource, /stockRoomFormSubmission/);
  assert.match(settingsSource, /stockRoomFormSubmission/);
  assert.match(createSource, /只绑定你明确输入的标的，不自动发现或扩展股票/);
});

test("stock authorization is cleared on edits, drift, room change and successful launch", () => {
  const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
  const panelSource = readFileSync(
    new URL("../src/components/StockResearchPanel.jsx", import.meta.url),
    "utf8",
  );
  const launchGateSource = appSource.slice(
    appSource.indexOf("const canAttemptNewRound"),
    appSource.indexOf("const roundBusy"),
  );
  const confirmationSource = appSource.slice(
    appSource.indexOf("const confirmRoundLaunch"),
    appSource.indexOf("const resumePausedRound"),
  );

  assert.match(
    launchGateSource,
    /!stockRoundContextRequired \|\| activeStockRoundContextAuthorization\.valid/,
  );
  assert.match(appSource, /sameRoom && sameRegistry && sameScope && stockResearchActivation\.active/);
  assert.match(confirmationSource, /setStockRoundContextAuthorization\(null\)/);
  assert.match(panelSource, /const canUpdateRoundAuthorization = typeof onRoundContextAuthorizationChange === "function"/);
  assert.match(panelSource, /const updateSource[\s\S]*if \(roundContextAuthorization && canUpdateRoundAuthorization\) onRoundContextAuthorizationChange\(null\)/);
  assert.match(panelSource, /const inspect[\s\S]*if \(roundContextAuthorization && canUpdateRoundAuthorization\) onRoundContextAuthorizationChange\(null\)/);
});

test("missing registry data never activates the stock contribution", () => {
  const activation = stockResearchInspectorActivation({
    frozenContext: null,
    runtimeContext: null,
    pluginRegistry: null,
    pluginLifecycle: null,
  });
  assert.equal(activation.visible, false);
  assert.equal(activation.active, false);
});
