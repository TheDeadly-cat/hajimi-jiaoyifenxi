import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  STORAGE_SAMPLE_ACCEPTANCE_VERSION,
  parseStorageSampleAcceptance,
} from "../src/storageSampleAcceptance.js";

const safeBoundary = {
  execution_capability: "none",
  live_trading_allowed: false,
};

test("legacy acceptance copy names the current v3 decision", () => {
  const componentSource = readFileSync(
    new URL("../src/components/StorageSampleAcceptanceCard.jsx", import.meta.url),
    "utf8",
  );

  assert.match(componentSource, /不参与当前 v3 判定/);
  assert.doesNotMatch(componentSource, /不参与当前 v2 判定/);
});

test("normalizes eight display stages while accepting the legacy v2 market_data aggregate", () => {
  const parsed = parseStorageSampleAcceptance({
    version: STORAGE_SAMPLE_ACCEPTANCE_VERSION,
    applicable: true,
    state: "accepted",
    latest_round_id: "round_v1",
    meeting_reviewed: true,
    research_sample_ready: true,
    user_decision_action: "support",
    statistical_validation_ready: false,
    stages: [
      { id: "discussion", ready: true, current: 12, required: 12, detail: "12 份合同合格。" },
      { id: "market_data", ready: true, current: 4, required: 4 },
      { id: "artifact", ready: true, current: 1, required: 1 },
      { id: "evidence", ready: true, current: 18, required: 18 },
      { id: "user_decision", ready: true, current: 1, required: 1, action: "support" },
      { id: "paper_portfolio", ready: true, current: 1, required: 1 },
      { id: "simulation_observations", state: "pending", current: 0, required: 20 },
    ],
    statistics: { sample_count: 0, minimum_samples: 20, qualified: false },
    ...safeBoundary,
  });

  assert.deepEqual(
    parsed.stages.map((stage) => stage.label),
    ["Futu 行情快照", "官方研究证据", "12角色讨论", "唯一纪要", "证据复核", "用户决定", "纸面组合", "模拟观察"],
  );
  assert.deepEqual(parsed.stages.map((stage) => stage.metric), ["4 / 4", "1 / 1", "12 / 12", "1 / 1", "18 / 18", "1 / 1", "1 / 1", "0 / 20"]);
  assert.equal(parsed.meetingReviewed, true);
  assert.equal(parsed.researchSampleReady, true);
});

test("shows a ready Futu snapshot separately from blocked official research evidence", () => {
  const parsed = parseStorageSampleAcceptance({
    version: STORAGE_SAMPLE_ACCEPTANCE_VERSION,
    applicable: true,
    state: "blocked",
    latest_round_id: "round_split_gate",
    market_snapshot_gate: {
      ready: true,
      current: 4,
      required: 4,
      detail: "四股 Futu 快照已就绪。",
    },
    research_evidence_gate: {
      ready: false,
      state: "blocked",
      current: 0,
      required: 1,
      detail: "SEC 官方材料尚未通过。",
    },
    stages: [
      { id: "market_data", ready: false, current: 4, required: 4 },
    ],
    ...safeBoundary,
  });

  const marketSnapshot = parsed.stages.find((stage) => stage.id === "market_snapshot");
  const researchEvidence = parsed.stages.find((stage) => stage.id === "research_evidence");
  assert.equal(marketSnapshot.ready, true);
  assert.equal(marketSnapshot.metric, "4 / 4");
  assert.equal(marketSnapshot.detail, "四股 Futu 快照已就绪。");
  assert.equal(researchEvidence.ready, false);
  assert.equal(researchEvidence.state, "blocked");
  assert.equal(researchEvidence.metric, "0 / 1");
  assert.equal(researchEvidence.detail, "SEC 官方材料尚未通过。");
});

test("falls back to flat gate fields and keeps statistics at sample zero of twenty", () => {
  const parsed = parseStorageSampleAcceptance({
    version: STORAGE_SAMPLE_ACCEPTANCE_VERSION,
    applicable: true,
    state: "no_round",
    data: { ready: false },
    discussion: { ready: false },
    artifact: { ready: false },
    evidence: { state: "pending" },
    user_decision: { state: "pending" },
    statistics: { sample_count: 0, minimum_samples: 20, qualified: false },
    ...safeBoundary,
  });

  assert.equal(parsed.statistics.sampleCount, 0);
  assert.equal(parsed.statistics.minimumSamples, 20);
  assert.equal(parsed.statistics.qualified, false);
  assert.equal(parsed.statisticsLabel, "统计胜率：样本 0 / 20，样本不足");
  assert.doesNotMatch(parsed.statisticsLabel, /模型置信度/);
  assert.equal(parsed.stages.at(-1).metric, "0 / 20");
});

test("marks a historical round as legacy and excludes every old stage from current acceptance", () => {
  const parsed = parseStorageSampleAcceptance({
    version: STORAGE_SAMPLE_ACCEPTANCE_VERSION,
    applicable: true,
    state: "legacy",
    latest_round_id: "round_legacy",
    stages: [
      { id: "market_data", ready: true, current: 4, required: 4 },
      { id: "discussion", ready: true, current: 12, required: 12 },
    ],
    statistics: { sample_count: 20, minimum_samples: 20, qualified: true },
    ...safeBoundary,
  });

  assert.equal(parsed.state, "legacy");
  assert.equal(parsed.legacyNotice, "旧版记录，不计入当前验收");
  assert.deepEqual(parsed.legacyRoundIds, ["round_legacy"]);
  assert.ok(parsed.stages.every((stage) => stage.state === "legacy"));
  assert.equal(parsed.researchSampleReady, false);
  assert.equal(parsed.statistics.qualified, false);
  assert.match(parsed.statisticsLabel, /样本不足/);
});

test("downgrades a v1 accepted payload because it did not prove the paper portfolio gate", () => {
  const parsed = parseStorageSampleAcceptance({
    version: "storage_sample_acceptance_v1",
    applicable: true,
    state: "accepted",
    latest_round_id: "round_v1_old",
    research_sample_ready: true,
    stages: [
      { id: "market_data", ready: true, current: 4, required: 4 },
      { id: "discussion", ready: true, current: 12, required: 12 },
    ],
    ...safeBoundary,
  });

  assert.equal(parsed.state, "legacy");
  assert.equal(parsed.researchSampleReady, false);
  assert.match(parsed.legacyNotice, /v1/);
  assert.equal(parsed.blockers[0].code, "STORAGE_ACCEPTANCE_LEGACY_SUPERSEDED");
});

test("downgrades a v2 accepted payload because it did not prove an explicit v2 user selection", () => {
  const parsed = parseStorageSampleAcceptance({
    version: "storage_sample_acceptance_v2",
    applicable: true,
    state: "accepted",
    latest_round_id: "round_v2_old",
    research_sample_ready: true,
    stages: [
      { id: "market_data", ready: true, current: 4, required: 4 },
      { id: "discussion", ready: true, current: 12, required: 12 },
    ],
    ...safeBoundary,
  });

  assert.equal(parsed.state, "legacy");
  assert.equal(parsed.researchSampleReady, false);
  assert.match(parsed.legacyNotice, /v2/);
  assert.equal(parsed.blockers[0].code, "STORAGE_ACCEPTANCE_LEGACY_SUPERSEDED");
});

test("keeps an exact hold decision as a reviewed meeting but not an accepted research sample", () => {
  const parsed = parseStorageSampleAcceptance({
    version: STORAGE_SAMPLE_ACCEPTANCE_VERSION,
    applicable: true,
    state: "deferred",
    latest_round_id: "round_hold",
    meeting_reviewed: true,
    research_sample_ready: false,
    user_decision_action: "hold",
    stages: [
      { id: "market_data", ready: true, current: 4, required: 4 },
      { id: "discussion", ready: true, current: 12, required: 12 },
      { id: "artifact", ready: true, current: 1, required: 1 },
      { id: "evidence", ready: true, current: 18, required: 18 },
      { id: "user_decision", ready: true, current: 1, required: 1, action: "hold" },
      { id: "paper_portfolio", state: "deferred", current: 0, required: 1 },
    ],
    ...safeBoundary,
  });

  assert.equal(parsed.state, "deferred");
  assert.equal(parsed.meetingReviewed, true);
  assert.equal(parsed.researchSampleReady, false);
  assert.equal(parsed.stages.find((stage) => stage.id === "paper_portfolio").state, "deferred");
  assert.match(parsed.decisionStateNotice, /暂缓/);
});

test("fails closed when accepted lacks a support decision or a confirmed paper portfolio", () => {
  const parsed = parseStorageSampleAcceptance({
    version: STORAGE_SAMPLE_ACCEPTANCE_VERSION,
    applicable: true,
    state: "accepted",
    latest_round_id: "round_false_accept",
    meeting_reviewed: true,
    research_sample_ready: true,
    user_decision_action: "hold",
    stages: [
      { id: "market_data", ready: true, current: 4, required: 4 },
      { id: "discussion", ready: true, current: 12, required: 12 },
      { id: "artifact", ready: true, current: 1, required: 1 },
      { id: "evidence", ready: true, current: 1, required: 1 },
      { id: "user_decision", ready: true, current: 1, required: 1, action: "hold" },
      { id: "paper_portfolio", state: "pending", current: 0, required: 1 },
    ],
    ...safeBoundary,
  });

  assert.equal(parsed.state, "blocked");
  assert.equal(parsed.researchSampleReady, false);
  assert.equal(parsed.blockers[0].code, "STORAGE_ACCEPTANCE_V2_STATE_INVALID");
});

test("fails closed when a historical acceptance payload omits its schema version", () => {
  const parsed = parseStorageSampleAcceptance({
    applicable: true,
    state: "accepted",
    latest_round_id: "round_unversioned",
    meeting_reviewed: true,
    research_sample_ready: true,
    user_decision_action: "support",
    stages: [
      { id: "market_data", ready: true, current: 4, required: 4 },
      { id: "discussion", ready: true, current: 12, required: 12 },
      { id: "artifact", ready: true, current: 1, required: 1 },
      { id: "evidence", ready: true, current: 1, required: 1 },
      { id: "user_decision", ready: true, current: 1, required: 1, action: "support" },
      { id: "paper_portfolio", ready: true, current: 1, required: 1 },
    ],
    ...safeBoundary,
  });

  assert.equal(parsed.state, "blocked");
  assert.equal(parsed.researchSampleReady, false);
  assert.equal(parsed.blockers[0].code, "STORAGE_ACCEPTANCE_SCHEMA_UNSUPPORTED");
  assert.match(parsed.blockers[0].detail, /没有声明验收版本/);
});

test("fails closed when an accepted payload does not preserve the no-execution boundary", () => {
  const parsed = parseStorageSampleAcceptance({
    version: STORAGE_SAMPLE_ACCEPTANCE_VERSION,
    applicable: true,
    state: "accepted",
    research_sample_ready: true,
    statistical_validation_ready: true,
    statistics: { sample_count: 20, minimum_samples: 20, qualified: true },
    execution_capability: "orders",
    live_trading_allowed: true,
  });

  assert.equal(parsed.state, "blocked");
  assert.equal(parsed.safetyReady, false);
  assert.equal(parsed.researchSampleReady, false);
  assert.equal(parsed.blockers[0].code, "STORAGE_ACCEPTANCE_SAFETY_BOUNDARY_INVALID");
});

test("does not unlock statistical validation from a contradictory qualified flag", () => {
  const parsed = parseStorageSampleAcceptance({
    version: STORAGE_SAMPLE_ACCEPTANCE_VERSION,
    applicable: true,
    state: "review_required",
    statistical_validation_ready: true,
    statistics: { sample_count: 2, minimum_samples: 20, qualified: true },
    ...safeBoundary,
  });

  assert.equal(parsed.statistics.qualified, false);
  assert.equal(parsed.statisticalValidationReady, false);
  assert.match(parsed.statisticsLabel, /样本 2 \/ 20/);
});

test("hides the storage-only card when the backend marks acceptance not applicable", () => {
  const parsed = parseStorageSampleAcceptance({
    version: STORAGE_SAMPLE_ACCEPTANCE_VERSION,
    applicable: false,
    state: "no_round",
    ...safeBoundary,
  });

  assert.equal(parsed.applicable, false);
});

test("atomically applies user-decision convergence and acceptance from the same response", () => {
  const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
  const helperSource = appSource.slice(
    appSource.indexOf("function applyArtifactUserDecisionResponse"),
    appSource.indexOf("export default function App"),
  );

  assert.match(helperSource, /Object\.hasOwn\(data, "storage_sample_acceptance"\)/);
  assert.match(helperSource, /convergence: data\.convergence \|\| current\.convergence/);
  assert.match(
    helperSource,
    /storage_sample_acceptance: data\.storage_sample_acceptance \?\? null/,
  );
});

test("explicitly refreshes convergence and acceptance when an old server omits either field", () => {
  const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
  const handlerSource = appSource.slice(
    appSource.indexOf("const createArtifactUserDecision"),
    appSource.indexOf("const pauseRound"),
  );

  assert.match(handlerSource, /if \(!data\.convergence \|\| !acceptanceIncluded\)/);
  assert.match(handlerSource, /refreshTasks\.push\([\s\S]*refreshConvergence\(roomId\)/);
  assert.match(handlerSource, /await Promise\.all\(refreshTasks\)/);
  assert.doesNotMatch(handlerSource, /if \(!data\.convergence\) await syncConvergence/);
});
