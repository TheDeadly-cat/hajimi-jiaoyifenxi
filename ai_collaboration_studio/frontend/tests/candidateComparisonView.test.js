import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  buildCandidateComparisonRequest,
  candidateComparisonEligibility,
  candidateComparisonEligibleRuns,
  candidateComparisonErrorMessage,
  candidateComparisonSelectionFingerprint,
  candidateComparisonView,
  CANDIDATE_COMPARISON_BASIS_VERSION,
  CANDIDATE_COMPARISON_PREVIEW_VERSION,
  CANDIDATE_COMPARISON_RUN_LIMIT,
  CANDIDATE_COMPARISON_REQUEST_VERSION,
} from "../src/candidateComparisonView.js";


const HASH = "a".repeat(64);


function scenario(scenarioId, offset = 0) {
  return {
    scenario_id: scenarioId,
    state: "sufficient",
    blocked: false,
    metrics_visible: true,
    metrics: {
      portfolio_cumulative_return_pct: 8 - offset,
      historical_positive_window_ratio: 0.55,
      max_drawdown_pct: -4 - offset,
      mean_window_return_pct: 0.6,
      worst_window_return_pct: -1.5 - offset,
    },
    capacity_gap_usd: null,
    first_blocker: null,
  };
}


function candidate(candidateId, symbol, direction) {
  return {
    run_id: `run_${candidateId}`,
    portfolio_id: `portfolio_${candidateId}`,
    portfolio_version: 2,
    candidate_id: candidateId,
    candidate_revision: 1,
    candidate_snapshot_sha256: candidateId === "candidate_a" ? "b".repeat(64) : "c".repeat(64),
    title: `${candidateId} historical replay`,
    symbol,
    direction,
    side: direction === "UP" ? "LONG" : "SHORT",
    target_weight_pct: 25,
    horizon_days: 20,
    thesis: "bounded thesis",
    invalidation: "explicit invalidation",
    contract_sha256: candidateId === "candidate_a" ? "d".repeat(64) : "e".repeat(64),
    source_decision_current: true,
    actionable_now: true,
    metrics_visible: true,
    scenarios: [scenario("baseline"), scenario("stressed", 1), scenario("severe", 2)],
  };
}


function readyResponse() {
  const candidates = [
    candidate("candidate_a", "US.MU", "UP"),
    candidate("candidate_b", "US.WDC", "DOWN"),
  ];
  return {
    version: CANDIDATE_COMPARISON_PREVIEW_VERSION,
    room_id: "room_storage",
    selected_run_ids: candidates.map((item) => item.run_id),
    status: "ready",
    ready: true,
    metrics_visible: true,
    issues: [],
    comparison_basis: {
      version: CANDIDATE_COMPARISON_BASIS_VERSION,
      dataset_content_sha256: HASH,
      walk_forward_config_sha256: "1".repeat(64),
      friction_scenario_set_sha256: "2".repeat(64),
      candidate_evaluation_basis_sha256: "3".repeat(64),
      record_version: 2,
      engine_version: "walk_forward_engine_v3",
      result_version: "walk_forward_result_v3",
      input_snapshot_version: "walk_forward_input_snapshot_v2",
      train_days: 99,
      test_days: 20,
      step_days: 20,
      price_adjustment: "QFQ",
      friction_scenario_set: "storage_friction_scenarios_v1",
      unfillable_policy: "block_scenario_no_partial_fill",
      target_weight_pct: 25,
      actual_start: "2024-01-02",
      actual_end: "2025-12-31",
      common_trading_days: 500,
    },
    comparison_basis_sha256: "4".repeat(64),
    candidates,
    metric_semantics: {
      historical_positive_window_ratio: "历史正收益窗口比例，不是未来胜率",
      ranking_produced: false,
      winner_claim: false,
      user_final_decision_required: true,
    },
    user_confirmed_historical_only: true,
    historical_only: true,
    out_of_sample_claim: false,
    future_performance_claim: false,
    provider_calls_total: 0,
    openai_calls: 0,
    market_data_reads: 0,
    execution_capability: "none",
    live_trading_allowed: false,
    can_autonomously_decide: false,
    preview_sha256: "5".repeat(64),
  };
}


test("candidate comparison request requires 2-6 unique historical run ids", () => {
  assert.deepEqual(buildCandidateComparisonRequest(["run_a", "run_b"]), {
    version: CANDIDATE_COMPARISON_REQUEST_VERSION,
    run_ids: ["run_a", "run_b"],
    user_confirmed_historical_only: true,
  });
  assert.throws(() => buildCandidateComparisonRequest(["run_a"]), TypeError);
  assert.throws(() => buildCandidateComparisonRequest(["run_a", "run_a"]), TypeError);
});


test("eligible runs require the exact current candidate contract and all integrity gates", () => {
  const contract = {
    version: "candidate_simulation_contract_v1",
    user_confirmed: true,
    contract_sha256: HASH,
    source: {
      candidate_id: "candidate_a",
      candidate_snapshot: { title: "MU 上行情景", direction: "UP" },
    },
    implementation: { target_symbol: "US.MU", target_side: "LONG", target_weight_pct: 25 },
    evaluation: { horizon_days: 20 },
  };
  const verified = {
    id: "run_verified",
    portfolio_version: 2,
    candidate_simulation_contract_sha256: HASH,
    record_version: 2,
    engine_version: "walk_forward_engine_v3",
    result_version: "walk_forward_result_v3",
    fully_verified: true,
    candidate_simulation_binding_verified: true,
    candidate_simulation_lineage_verified: true,
    candidate_simulation_marker_binding_verified: true,
    integrity_profile_verified: true,
    walk_forward_v3_lineage_verified: true,
    created_at: 123,
  };
  const stale = { ...verified, id: "run_stale", portfolio_version: 1 };
  const tampered = { ...verified, id: "run_tampered", fully_verified: false };

  const eligible = candidateComparisonEligibleRuns(
    [{ id: "portfolio_a", version: 2, name: "MU paper", candidate_simulation_contract: contract }],
    { portfolio_a: [stale, tampered, verified] },
  );

  assert.deepEqual(eligible.map((item) => item.runId), ["run_verified"]);
  assert.equal(eligible[0].candidateTitle, "MU 上行情景");
});


test("eligibility fails closed before sorting oversized run collections", () => {
  const state = candidateComparisonEligibility([], {
    portfolio_a: Array.from({ length: CANDIDATE_COMPARISON_RUN_LIMIT + 1 }, () => null),
  });
  assert.equal(state.integrityOk, false);
  assert.deepEqual(state.runs, []);
  assert.match(state.issue, /安全上限/);
});


test("selection fingerprints avoid delimiter collisions and errors are bounded", () => {
  assert.notEqual(
    candidateComparisonSelectionFingerprint(["run_a", "run_b|run_c"]),
    candidateComparisonSelectionFingerprint(["run_a|run_b", "run_c"]),
  );
  assert.equal(candidateComparisonErrorMessage({ message: { unsafe: true } }, "fallback"), "fallback");
  assert.equal(candidateComparisonErrorMessage({ message: "x".repeat(1500) }).length, 1000);
});


test("ready response exposes same-basis metrics without ranking or winner claims", () => {
  const view = candidateComparisonView(readyResponse());

  assert.equal(view.ready, true);
  assert.equal(typeof view.ready, "boolean");
  assert.equal(view.metricsVisible, true);
  assert.deepEqual(view.selectedRunIds, ["run_candidate_a", "run_candidate_b"]);
  assert.equal(view.candidates[0].scenarios[0].cumulativeReturnPct, 8);
  assert.equal(view.rankingProduced, false);
  assert.equal(view.winnerClaim, false);
  assert.equal(view.userFinalDecisionRequired, true);
});


test("ranking claims, run-order drift, or incomplete metrics hide every metric", () => {
  for (const mutate of [
    (response) => { response.metric_semantics.winner_claim = true; },
    (response) => { response.selected_run_ids.reverse(); },
    (response) => { response.candidates[0].scenarios[0].metrics.mean_window_return_pct = null; },
    (response) => { response.candidates[0].scenarios[0].metrics.mean_window_return_pct = "0.6"; },
    (response) => { response.candidates[0].scenarios[0].metrics.historical_positive_window_ratio = 1.2; },
    (response) => { response.candidates[0].target_weight_pct = 30; },
  ]) {
    const response = readyResponse();
    mutate(response);
    const view = candidateComparisonView(response);
    assert.equal(view.ready, false);
    assert.equal(view.metricsVisible, false);
    assert.ok(view.candidates.every((item) => item.scenarios.length === 0));
  }
});


test("duplicate exact candidate versions and unsafe execution fields fail closed", () => {
  const duplicate = readyResponse();
  duplicate.candidates[1].candidate_id = duplicate.candidates[0].candidate_id;
  duplicate.candidates[1].candidate_revision = duplicate.candidates[0].candidate_revision;
  duplicate.candidates[1].candidate_snapshot_sha256 = duplicate.candidates[0].candidate_snapshot_sha256;
  assert.equal(candidateComparisonView(duplicate).ready, false);

  const unsafe = readyResponse();
  unsafe.live_trading_allowed = true;
  const unsafeView = candidateComparisonView(unsafe);
  assert.equal(unsafeView.ready, false);
  assert.ok(unsafeView.candidates.every((item) => item.metricsVisible === false));
});


test("blocked scenarios cannot leak metrics and selection changes hide stale responses", () => {
  const blocked = readyResponse();
  blocked.candidates[0].scenarios[0].blocked = true;
  blocked.candidates[0].scenarios[0].state = "blocked";
  blocked.candidates[0].scenarios[0].metrics_visible = false;
  assert.equal(candidateComparisonView(blocked).ready, false);

  const panelSource = readFileSync(
    new URL("../src/components/CandidateComparisonPanel.jsx", import.meta.url),
    "utf8",
  );
  const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
  const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
  const refinementStyles = readFileSync(
    new URL("../src/styles/candidate-comparison-refinement.css", import.meta.url),
    "utf8",
  );
  assert.match(panelSource, /if \(loading\) return;\s+setAcknowledged\(false\);/);
  assert.match(panelSource, /candidateComparisonSelectionFingerprint\(view\?\.selectedRunIds\)/);
  assert.match(panelSource, /const selectedRunSet = useMemo\(\(\) => new Set\(selectedRunIds\)/);
  assert.match(panelSource, /const selected = selectedRunSet\.has\(run\.runId\)/);
  assert.match(panelSource, /disabled=\{loading \|\| \(!selected && selectedRunIds\.length >= 6\)\}/);
  assert.match(panelSource, /submissionRef\.current/);
  assert.match(panelSource, /RUN_SELECTOR_PAGE_SIZE = 18/);
  assert.match(panelSource, /role="region"/);
  assert.ok(
    panelSource.indexOf("!eligibility.integrityOk")
      < panelSource.indexOf("eligibleRuns.length >= 2"),
  );
  assert.match(appSource, /candidateComparisonRequestRef\.current\.cancel\(\)/);
  assert.match(appSource, /candidateComparisonContextRef\.current !== targetContext/);
  assert.match(appSource, /returnedRunIds\.some\(\(runId, index\) => runId !== payload\.run_ids\[index\]\)/);
  assert.match(styles, /\.candidate-comparison-table-wrap\s*\{[\s\S]*?overflow-x:\s*auto;/);
  assert.match(refinementStyles, /candidate-comparison-neutrality-ledger/);
  assert.match(refinementStyles, /prefers-reduced-motion/);
  assert.match(refinementStyles, /forced-colors/);
});


test("oversized candidates, scenarios, and string issues fail closed before display", () => {
  const tooManyCandidates = readyResponse();
  tooManyCandidates.candidates = Array.from({ length: 7 }, () => null);
  assert.equal(candidateComparisonView(tooManyCandidates).ready, false);

  const tooManyScenarios = readyResponse();
  tooManyScenarios.candidates[0].scenarios = Array.from({ length: 4 }, () => null);
  assert.equal(candidateComparisonView(tooManyScenarios).ready, false);

  const stringIssue = readyResponse();
  stringIssue.issues = ["server integrity warning"];
  const issueView = candidateComparisonView(stringIssue);
  assert.equal(issueView.ready, false);
  assert.equal(issueView.issues[0].code, "CANDIDATE_COMPARISON_BLOCKED");
});


test("comparison workbench exposes a heading, neutral scope, and explicit quantity gate", () => {
  const panelSource = readFileSync(
    new URL("../src/components/CandidateComparisonPanel.jsx", import.meta.url),
    "utf8",
  );
  const refinementStyles = readFileSync(
    new URL("../src/styles/candidate-comparison-refinement.css", import.meta.url),
    "utf8",
  );

  assert.match(panelSource, /<h4 id=\{titleId\}>[\s\S]*已验证回放同口径复核<\/h4>/);
  assert.match(panelSource, /<em><data value=\{eligibleRuns\.length\}>/);
  assert.match(panelSource, /className="candidate-comparison-scope" role="list"/);
  assert.match(panelSource, /同数据 · 同窗口 · 同摩擦/);
  assert.match(panelSource, /排名 · 赢家 · 授权/);
  assert.match(panelSource, /const minimumComparableRuns = 2/);
  assert.match(panelSource, /const missingComparableRuns = Math\.max\(0, minimumComparableRuns - eligibleRuns\.length\)/);
  assert.match(panelSource, /className=\{`candidate-comparison-readiness \$\{readinessTone\}`\}/);
  assert.match(panelSource, /这不是批准/);
  assert.match(panelSource, /className="candidate-comparison-empty" role="note"/);
  assert.match(panelSource, /aria-label="候选比较开放前提"/);
  assert.match(panelSource, /<h5 id=\{resultTitleId\}>同一冻结基准已核验<\/h5>/);
  assert.match(refinementStyles, /\.candidate-comparison-scope \{[\s\S]*grid-template-columns: repeat\(3, minmax\(0, 1fr\)\)/);
  assert.match(refinementStyles, /\.candidate-comparison-readiness \{[\s\S]*grid-template-columns: auto minmax\(0, 1fr\) auto/);
  assert.match(refinementStyles, /@container candidate-comparison \(max-width: 620px\)[\s\S]*\.candidate-comparison-scope \{ grid-template-columns: minmax\(0, 1fr\); \}/);
  assert.match(refinementStyles, /@media \(forced-colors: active\)[\s\S]*\.candidate-comparison-readiness > data/);
  assert.equal(
    (refinementStyles.match(/\{/g) || []).length,
    (refinementStyles.match(/\}/g) || []).length,
  );
});
