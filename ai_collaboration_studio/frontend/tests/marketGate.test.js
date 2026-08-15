import assert from "node:assert/strict";
import test from "node:test";

import { deriveMarketGate, quoteFreshnessLabel, quoteResearchReady } from "../src/marketGate.js";

const SYMBOLS = ["US.MU", "US.SNDK", "US.WDC", "US.STX"];

function readySnapshot(overrides = {}) {
  return {
    ok: true,
    state: "ready",
    source: "futu_opend",
    snapshot_id: "futu-test-ready",
    captured_at: "2026-08-01T12:00:00Z",
    rows: SYMBOLS.map((symbol, index) => ({
      symbol,
      quality: "ready",
      age_seconds: 60,
      quote_is_live: true,
      freshness_basis: "live_20m_window",
      last: 100 + index,
      market_time: "2026-08-01 15:59:00",
    })),
    missing_symbols: [],
    source_errors: [],
    execution_capability: "none",
    live_trading_allowed: false,
    ...overrides,
  };
}

test("four valid Futu rows with explicit read-only safety pass", () => {
  const gate = deriveMarketGate({ required: true, snapshot: readySnapshot(), loading: false });
  assert.equal(gate.ready, true);
  assert.equal(gate.readyCount, 4);
});

test("prepared independent evidence cannot bypass an offline Futu snapshot", () => {
  const gate = deriveMarketGate({
    required: true,
    loading: false,
    snapshot: {
      ...readySnapshot(),
      ok: false,
      state: "offline",
      rows: [],
      missing_symbols: SYMBOLS,
      source_errors: [{
        source: "futu_opend",
        code: "FUTU_OPEND_OFFLINE",
        message: "本机 Futu OpenD 未连接",
      }],
      evidence: {
        company_ir_releases: { state: "ready", rows: SYMBOLS.map((symbol) => ({ symbol })) },
      },
    },
  });

  assert.equal(gate.ready, false);
  assert.equal(gate.shortLabel, "富途离线");
  assert.equal(gate.readyCount, 0);
  assert.equal(gate.severity, "attention");
});

test("missing read-only safety fields fail closed even with four prices", () => {
  const snapshot = readySnapshot();
  delete snapshot.execution_capability;
  delete snapshot.live_trading_allowed;

  const gate = deriveMarketGate({ required: true, snapshot, loading: false });

  assert.equal(gate.ready, false);
  assert.equal(gate.shortLabel, "只读边界异常");
  assert.equal(gate.severity, "critical");
});

test("closed-session research-ready quote is never labeled live", () => {
  assert.equal(quoteFreshnessLabel({
    quality: "ready",
    quote_is_live: false,
    freshness_basis: "closed_session_latest_snapshot",
    market_state: "AFTER_HOURS_END",
    age_seconds: 60_000,
  }), "最近闭市截面");
  assert.equal(quoteFreshnessLabel({
    quality: "ready",
    quote_is_live: true,
    freshness_basis: "live_20m_window",
    age_seconds: 60,
  }), "实时截面");
  assert.equal(quoteFreshnessLabel({
    quality: "ready",
  }), "新鲜度待核验");
});

test("security status and suspension fail closed while legacy rows remain compatible", () => {
  const live = readySnapshot().rows[0];

  assert.equal(quoteResearchReady(live), true);
  assert.equal(quoteResearchReady({ ...live, security_status: "NORMAL" }), true);
  assert.equal(quoteResearchReady({ ...live, security_status: "SecurityStatus.NORMAL" }), true);
  assert.equal(quoteResearchReady({ ...live, security_status: "DELISTED" }), false);
  assert.equal(quoteResearchReady({ ...live, suspended: true }), false);
  assert.equal(quoteFreshnessLabel({ ...live, security_status: "DELISTED" }), "停牌/状态异常");
  assert.equal(quoteFreshnessLabel({ ...live, quality: "stale", suspended: true }), "停牌/状态异常");
});

test("an abnormal security state blocks the four-symbol market gate", () => {
  const snapshot = readySnapshot();
  snapshot.rows[1] = { ...snapshot.rows[1], security_status: "SecurityStatus.SUSPENDED" };

  const gate = deriveMarketGate({ required: true, snapshot, loading: false });

  assert.equal(gate.ready, false);
  assert.equal(gate.readyCount, 3);
  assert.equal(gate.shortLabel, "停牌/状态异常");
  assert.match(gate.reason, /SNDK/);
});

test("legacy quality-ready rows without freshness contract fail closed", () => {
  const snapshot = readySnapshot({
    rows: SYMBOLS.map((symbol, index) => ({
      symbol,
      quality: "ready",
      last: 100 + index,
      market_time: "2026-08-01 15:59:00",
    })),
  });

  const gate = deriveMarketGate({ required: true, snapshot, loading: false });

  assert.equal(gate.ready, false);
  assert.equal(gate.readyCount, 0);
});
