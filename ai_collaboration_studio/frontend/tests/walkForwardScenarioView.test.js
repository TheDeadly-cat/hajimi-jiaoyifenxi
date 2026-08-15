import assert from "node:assert/strict";
import test from "node:test";

import {
  buildWalkForwardRequestPayload,
  CANDIDATE_SIMULATION_CONTRACT_VERSION,
  CANDIDATE_SIMULATION_RULE_ID,
  CANDIDATE_WALK_FORWARD_CONFIG_VERSION,
  WALK_FORWARD_CONFIG_VERSION,
  WALK_FORWARD_LOCKED_SCENARIOS,
  WALK_FORWARD_SCENARIO_IDS,
  WALK_FORWARD_SCENARIO_SET_VERSION,
  WALK_FORWARD_STRATEGY_CONTRACT_VERSION,
  WALK_FORWARD_STRATEGY_RULE_ID,
  WALK_FORWARD_UNFILLABLE_POLICY,
  walkForwardScenarioView,
} from "../src/walkForwardScenarioView.js";


function verifiedRun(version, result = {}) {
  return {
    integrity_ok: true,
    fully_verified: true,
    integrity_status: "verified",
    integrity_issues: [],
    result_version: version,
    result: { version, ...result },
  };
}


function completeAssumptions(seed = 0) {
  return {
    paper_reference_notional_usd: 1_000_000 + seed,
    commission_bps_per_side: 10 + seed,
    entry_slippage_bps: 5 + seed,
    exit_slippage_bps: 5 + seed,
    short_borrow_fee_bps_annual: 300 + seed,
    max_daily_turnover_participation_pct: 2 + seed,
  };
}


function verifiedV4Run() {
  const strategyHash = "a".repeat(64);
  const strategyDecision = {
    version: "fold_strategy_decision_v1",
    scores_pct: { "US.MU": 12, "US.SNDK": 5, "US.WDC": 2, "US.STX": -3 },
    ranking: ["US.MU", "US.SNDK", "US.WDC", "US.STX"],
    selected_positions: [
      { symbol: "US.MU", side: "LONG", weight_pct: 50 },
      { symbol: "US.STX", side: "SHORT", weight_pct: 50 },
    ],
    fit_input_hash: "b".repeat(64),
  };
  const formalFold = {
    fold_index: 1,
    fold_id: "fold_001",
    train_start: "2024-01-02",
    train_end: "2024-05-24",
    decision_cutoff: "2024-05-24",
    scheduled_entry_date: "2024-05-28",
    test_start: "2024-05-28",
    test_end: "2024-06-25",
    non_overlapping_test_window: true,
    blocked: false,
    status: "ready",
    strategy_decision: strategyDecision,
  };
  const overlappingFold = {
    ...formalFold,
    fold_index: 2,
    fold_id: "fold_002",
    non_overlapping_test_window: false,
  };
  const scenarios = ["baseline", "stressed", "severe"].map((scenarioId, index) => ({
    scenario_id: scenarioId,
    label: `${scenarioId} scenario`,
    state: scenarioId === "stressed" ? "blocked" : "sufficient",
    blocked: scenarioId === "stressed",
    formal_unfillable_fold_count: scenarioId === "stressed" ? 1 : 0,
    first_blocker: scenarioId === "stressed"
      ? { reason_code: "UNFILLABLE", symbol: "US.MU", phase: "entry", capacity_gap_usd: 25_000 }
      : null,
    assumptions: completeAssumptions(index),
    summary: {
      portfolio_cumulative_return_pct: scenarioId === "stressed" ? 999 : 3 - index,
      historical_positive_fold_ratio: scenarioId === "stressed" ? 1 : 0.55,
      max_drawdown_pct: scenarioId === "stressed" ? 0 : -5 - index,
    },
    folds: [
      {
        ...formalFold,
        blocked: scenarioId === "stressed",
        status: scenarioId === "stressed" ? "blocked" : "ready",
      },
      overlappingFold,
    ],
  }));
  return {
    integrity_ok: true,
    fully_verified: true,
    integrity_status: "verified",
    integrity_issues: [],
    record_version: 3,
    engine_version: "walk_forward_engine_v4",
    result_version: "walk_forward_result_v4",
    strategy_contract_sha256: strategyHash,
    strategy_contract_hash_verified: true,
    decision_anchor_hash_verified: true,
    decision_binding_verified: true,
    lineage_binding_verified: true,
    result_recomputed_verified: true,
    actionable_now: false,
    config: { version: WALK_FORWARD_CONFIG_VERSION, train_days: 99, test_days: 20, step_days: 20 },
    result: {
      version: "walk_forward_result_v4",
      engine_version: "walk_forward_engine_v4",
      input_snapshot_version: "walk_forward_input_snapshot_v3",
      config: { version: WALK_FORWARD_CONFIG_VERSION, train_days: 99, test_days: 20, step_days: 20 },
      evaluation_mode: "fold_train_only_next_session_test_replay",
      strategy_provenance: "server_whitelisted_fold_trained_rule",
      out_of_sample_claim: false,
      future_performance_claim: false,
      retrospective_dataset: true,
      strategy_contract_sha256: strategyHash,
      strategy_rule_contract: {
        version: WALK_FORWARD_STRATEGY_CONTRACT_VERSION,
        rule_id: WALK_FORWARD_STRATEGY_RULE_ID,
        signal: "total_return",
        fit_scope: "rolling_train_window_only",
        ranking: "descending_cross_sectional",
        long_count: 1,
        short_count: 1,
        long_budget_pct: 50,
        short_budget_pct: 50,
        weighting: "equal_weight_per_side",
        rebalance: "once_per_fold",
        test_data_excluded_from_fit: true,
        universe: ["US.MU", "US.SNDK", "US.WDC", "US.STX"],
      },
      summary: {
        non_overlapping_test_fold_count: 20,
        minimum_non_overlapping_test_folds: 20,
      },
      scenario_results: scenarios,
      folds: [formalFold, overlappingFold],
    },
  };
}


test("returns v3 scenarios in the fixed baseline, stressed, severe order", () => {
  const run = verifiedRun("walk_forward_result_v3", {
    scenario_results: [
      {
        scenario_id: "severe",
        label: "极端场景",
        state: "insufficient",
        assumptions: completeAssumptions(2),
        unfillable_fold_count: 0,
        summary: {
          portfolio_cumulative_return_pct: "-12.5",
          historical_positive_fold_ratio: 0.25,
          max_drawdown_pct: -18.75,
        },
        folds: [],
      },
      {
        scenario_id: "baseline",
        label: "基准场景",
        state: "sufficient",
        assumptions: completeAssumptions(),
        unfillable_fold_count: 0,
        summary: {
          portfolio_cumulative_return_pct: 8.5,
          historical_positive_fold_ratio: "0.6",
          max_drawdown_pct: -7.25,
        },
        folds: [],
      },
      { scenario_id: "unknown", state: "sufficient", summary: {} },
      {
        scenario_id: "stressed",
        label: "压力场景",
        state: "sufficient",
        assumptions: completeAssumptions(1),
        unfillable_fold_count: 0,
        summary: {
          portfolio_cumulative_return_pct: 1.5,
          historical_positive_fold_ratio: 0.45,
          max_drawdown_pct: -10,
        },
        folds: [],
      },
    ],
  });

  const view = walkForwardScenarioView(run);

  assert.deepEqual(view.rows.map((row) => row.id), WALK_FORWARD_SCENARIO_IDS);
  assert.equal(view.metricsVisible, true);
  assert.equal(view.scenarioMetricsVisible, true);
  assert.equal(view.legacyFrictionWarning, null);
  assert.deepEqual(
    view.rows.map((row) => row.portfolioCumulativeReturnPct),
    [8.5, 1.5, -12.5],
  );
  assert.equal(view.rows[0].historicalPositiveFoldRatio, 0.6);
  assert.deepEqual(view.rows[1].assumptions, completeAssumptions(1));
});


test("candidate contract locks the fixed replay rule and exact horizon", () => {
  const candidateContract = {
    version: CANDIDATE_SIMULATION_CONTRACT_VERSION,
    user_confirmed: true,
    evaluation: {
      rule_id: CANDIDATE_SIMULATION_RULE_ID,
      horizon_days: 5,
    },
  };
  const payload = buildWalkForwardRequestPayload(9, {
    train_days: 99,
    test_days: 5,
    step_days: 5,
  }, candidateContract);

  assert.deepEqual(payload, {
    expected_portfolio_version: 9,
    version: CANDIDATE_WALK_FORWARD_CONFIG_VERSION,
    train_days: 99,
    test_days: 5,
    step_days: 5,
    price_adjustment: "QFQ",
    friction_scenario_set: WALK_FORWARD_SCENARIO_SET_VERSION,
    unfillable_policy: WALK_FORWARD_UNFILLABLE_POLICY,
  });
  assert.equal("strategy_rule_id" in payload, false);
  assert.throws(
    () => buildWalkForwardRequestPayload(9, {
      train_days: 99,
      test_days: 20,
      step_days: 20,
    }, candidateContract),
    /必须等于候选合同期限/,
  );
});


test("blocked scenario exposes assumptions and blocker context but hides all audit metrics", () => {
  const firstBlocker = {
    reason_code: "UNFILLABLE",
    symbol: "US.MU",
    required_notional_usd: 1_000_000,
    available_capacity_usd: 250_000,
    capacity_gap_usd: 750_000,
  };
  const run = verifiedRun("walk_forward_result_v3", {
    scenario_results: [{
      id: "baseline",
      label: "基准场景",
      state: "blocked",
      assumptions: completeAssumptions(),
      unfillable_fold_count: 1,
      summary: {
        portfolio_cumulative_return_pct: 99,
        historical_positive_fold_ratio: 1,
        max_drawdown_pct: 0,
      },
      first_blocker: firstBlocker,
      folds: [],
    }],
  });

  const row = walkForwardScenarioView(run).rows[0];

  assert.equal(row.blocked, true);
  assert.equal(row.metricsVisible, false);
  assert.equal(row.portfolioCumulativeReturnPct, null);
  assert.equal(row.historicalPositiveFoldRatio, null);
  assert.equal(row.maxDrawdownPct, null);
  assert.equal(row.capacityGap, 750_000);
  assert.deepEqual(row.firstBlocker, firstBlocker);
  assert.deepEqual(row.assumptions, completeAssumptions());
});


test("formal unfillable folds block a scenario and provide the first nested fold blocker", () => {
  const foldBlocker = {
    reason_code: "LIQUIDITY_PROXY_MISSING",
    capacity_shortfall_usd: 125_000,
  };
  const run = verifiedRun("walk_forward_result_v3", {
    scenario_results: [{
      id: "stressed",
      state: "sufficient",
      assumptions: completeAssumptions(1),
      unfillable_fold_count: 2,
      formal_unfillable_fold_count: 2,
      summary: {
        portfolio_cumulative_return_pct: 4,
        historical_positive_fold_ratio: 0.7,
        max_drawdown_pct: -3,
      },
      folds: [
        { fillability: { first_blocker: null } },
        { fillability: { first_blocker: foldBlocker } },
      ],
    }],
  });

  const row = walkForwardScenarioView(run).rows[1];

  assert.equal(row.blocked, true);
  assert.equal(row.portfolioCumulativeReturnPct, null);
  assert.equal(row.historicalPositiveFoldRatio, null);
  assert.equal(row.maxDrawdownPct, null);
  assert.equal(row.capacityGap, 125_000);
  assert.deepEqual(row.firstBlocker, foldBlocker);
});


test("overlapping-only unfillable folds do not hide scenario metrics", () => {
  const run = verifiedRun("walk_forward_result_v3", {
    scenario_results: [{
      scenario_id: "baseline",
      state: "sufficient",
      blocked: false,
      assumptions: completeAssumptions(),
      unfillable_fold_count: 3,
      formal_unfillable_fold_count: 0,
      summary: {
        portfolio_cumulative_return_pct: 2.5,
        historical_positive_fold_ratio: 0.55,
        max_drawdown_pct: -6,
      },
      folds: [{ blocked: true, first_blocker: { capacity_gap_usd: 250_000 } }],
    }],
  });

  const row = walkForwardScenarioView(run).rows[0];

  assert.equal(row.blocked, false);
  assert.equal(row.metricsVisible, true);
  assert.equal(row.portfolioCumulativeReturnPct, 2.5);
  assert.equal(row.capacityGap, null);
  assert.equal(row.firstBlocker, null);
});


test("missing v3 scenarios remain fixed unavailable rows without invented blockers", () => {
  const view = walkForwardScenarioView(verifiedRun("walk_forward_result_v3", {
    scenario_results: [],
  }));

  assert.deepEqual(view.rows.map((row) => row.id), ["baseline", "stressed", "severe"]);
  for (const row of view.rows) {
    assert.equal(row.state, "missing");
    assert.equal(row.available, false);
    assert.equal(row.metricsVisible, false);
    assert.equal(row.capacityGap, null);
    assert.equal(row.firstBlocker, null);
  }
});


test("verified v2 keeps top-level visibility but warns and never fabricates v3 rows", () => {
  const view = walkForwardScenarioView(verifiedRun("walk_forward_result_v2", {
    summary: { portfolio_cumulative_return_pct: 12.5 },
  }));

  assert.equal(view.metricsVisible, true);
  assert.equal(view.scenarioMetricsVisible, false);
  assert.equal(view.legacyFrictionWarning.code, "LEGACY_FRICTION_MODEL_V2");
  assert.match(view.legacyFrictionWarning.message, /旧版单一交易成本模型/);
  for (const row of view.rows) {
    assert.equal(row.state, "legacy_unavailable");
    assert.equal(row.portfolioCumulativeReturnPct, null);
    assert.equal(row.historicalPositiveFoldRatio, null);
    assert.equal(row.maxDrawdownPct, null);
  }
});


test("integrity failure hides the entire scenario audit model", () => {
  const run = {
    ...verifiedRun("walk_forward_result_v3", {
      scenario_results: [{
        id: "baseline",
        state: "blocked",
        assumptions: completeAssumptions(),
        first_blocker: { capacity_gap_usd: 999 },
        summary: { portfolio_cumulative_return_pct: 999 },
      }],
    }),
    integrity_ok: false,
    fully_verified: false,
    integrity_status: "failed",
    integrity_issues: ["WALK_FORWARD_RESULT_HASH_MISMATCH"],
  };

  const view = walkForwardScenarioView(run);

  assert.equal(view.integrity.label, "完整性校验失败");
  assert.equal(view.metricsVisible, false);
  assert.equal(view.scenarioMetricsVisible, false);
  for (const row of view.rows) {
    assert.equal(row.state, "hidden");
    assert.equal(row.portfolioCumulativeReturnPct, null);
    assert.equal(row.capacityGap, null);
    assert.equal(row.firstBlocker, null);
    assert.deepEqual(row.assumptions, {
      paper_reference_notional_usd: null,
      commission_bps_per_side: null,
      entry_slippage_bps: null,
      exit_slippage_bps: null,
      short_borrow_fee_bps_annual: null,
      max_daily_turnover_participation_pct: null,
    });
  }
});


test("v1 and unknown versions fail closed even if integrity flags claim verified", () => {
  for (const version of ["walk_forward_result_v1", "walk_forward_result_future"]) {
    const view = walkForwardScenarioView(verifiedRun(version, {
      scenario_results: [{
        id: "baseline",
        state: "sufficient",
        summary: { portfolio_cumulative_return_pct: 10 },
      }],
    }));

    assert.equal(view.metricsVisible, false);
    assert.equal(view.scenarioMetricsVisible, false);
    assert.equal(view.rows[0].portfolioCumulativeReturnPct, null);
  }
});


test("verified v4 exposes the flat strategy contract, separate gates, and formal fold audit", () => {
  const view = walkForwardScenarioView(verifiedV4Run());

  assert.equal(view.metricsVisible, true);
  assert.equal(view.ruleContractVisible, true);
  assert.equal(view.evaluation.kind, "prospective");
  assert.equal(view.evaluation.positiveRateLabel, "历史测试正收益窗口比例（非未来胜率）");
  assert.match(view.evaluation.detail, /不是未来实盘胜率/);
  assert.equal(view.strategyContract.version, WALK_FORWARD_STRATEGY_CONTRACT_VERSION);
  assert.equal(view.strategyContract.ruleId, WALK_FORWARD_STRATEGY_RULE_ID);
  assert.equal(view.strategyContract.signal, "total_return");
  assert.equal(view.strategyContract.fitScope, "rolling_train_window_only");
  assert.equal(view.strategyContract.testDataExcludedFromFit, true);
  assert.equal(view.strategyContract.longCount, 1);
  assert.equal(view.strategyContract.shortBudgetPct, 50);
  assert.deepEqual(view.strategyContract.universe, ["US.MU", "US.SNDK", "US.WDC", "US.STX"]);
  assert.equal(view.dataGate.ready, true);
  assert.equal(view.dataGate.label, "窗口数达最低门槛");
  assert.equal(view.capacityGate.ready, false);
  assert.equal(view.capacityGate.detail, "2/3 档可评估");
  assert.equal(view.actionableNow, false);
  assert.equal(view.foldRows.length, 1);
  assert.equal(view.foldRows[0].id, "fold_001");
  assert.deepEqual(view.foldRows[0].strategyDecision.selectedParameters, {});
  assert.deepEqual(
    view.foldRows[0].strategyDecision.trainSelectionEvidence.ranking,
    ["US.MU", "US.SNDK", "US.WDC", "US.STX"],
  );
  assert.equal(view.foldRows[0].strategyDecision.generatedPositions[0].symbol, "US.MU");
  assert.deepEqual(
    view.foldRows[0].scenarios.map(({ id, state }) => [id, state]),
    [["baseline", "ready"], ["stressed", "blocked"], ["severe", "ready"]],
  );
  assert.match(view.foldRows[0].scenarios[1].label, /收益隐藏/);
  assert.equal(view.rows[1].portfolioCumulativeReturnPct, null);
  assert.equal(view.rows[1].historicalPositiveFoldRatio, null);
  assert.equal(view.rows[1].maxDrawdownPct, null);
});


test("v4 fails closed when any new binding flag or train-only contract invariant fails", () => {
  for (const field of [
    "strategy_contract_hash_verified",
    "decision_anchor_hash_verified",
    "decision_binding_verified",
    "lineage_binding_verified",
    "result_recomputed_verified",
  ]) {
    const run = verifiedV4Run();
    run[field] = false;
    const view = walkForwardScenarioView(run);
    assert.equal(view.metricsVisible, false, field);
    assert.equal(view.ruleContractVisible, false, field);
    assert.equal(view.strategyContract, null, field);
    assert.deepEqual(view.foldRows, [], field);
    assert.equal(view.integrity.label, "v4 审计绑定未通过", field);
  }

  const leakedTestData = verifiedV4Run();
  leakedTestData.result.strategy_rule_contract.test_data_excluded_from_fit = false;
  const leakedView = walkForwardScenarioView(leakedTestData);
  assert.equal(leakedView.metricsVisible, false);
  assert.equal(leakedView.strategyContract, null);
  assert.deepEqual(leakedView.foldRows, []);
});


test("v4 exact version and semantics matrix cannot be weakened by verified flags", () => {
  const mutations = [
    (run) => { run.record_version = 2; },
    (run) => { run.engine_version = "walk_forward_engine_v3"; },
    (run) => { run.result.input_snapshot_version = "walk_forward_input_snapshot_v2"; },
    (run) => { run.result.config.version = "walk_forward_config_v2"; },
    (run) => { run.result.evaluation_mode = "retroactive_fixed_plan_replay"; },
    (run) => { run.result.strategy_provenance = "current_plan_retroactive"; },
    (run) => { run.result.future_performance_claim = true; },
    (run) => { run.result.retrospective_dataset = false; },
    (run) => { run.result.strategy_contract_sha256 = "not-a-hash"; },
  ];
  for (const mutate of mutations) {
    const run = verifiedV4Run();
    mutate(run);
    const view = walkForwardScenarioView(run);
    assert.equal(view.metricsVisible, false);
    assert.equal(view.ruleContractVisible, false);
    assert.deepEqual(view.foldRows, []);
  }
});


test("assumptions are fixed-shape finite numbers and view data does not alias blockers", () => {
  const blocker = { capacity_gap: { amount_usd: 400 }, nested: { symbol: "US.WDC" } };
  const run = verifiedRun("walk_forward_result_v3", {
    scenario_results: [{
      id: "severe",
      state: "blocked",
      assumptions: {
        paper_reference_notional_usd: "10000000",
        commission_bps_per_side: false,
        entry_slippage_bps: Infinity,
      },
      first_blocker: blocker,
      summary: {},
      folds: [],
    }],
  });

  const row = walkForwardScenarioView(run).rows[2];
  assert.equal(row.assumptions.paper_reference_notional_usd, 10_000_000);
  assert.equal(row.assumptions.commission_bps_per_side, null);
  assert.equal(row.assumptions.entry_slippage_bps, null);
  assert.equal(row.assumptions.exit_slippage_bps, null);
  assert.deepEqual(row.capacityGap, { amount_usd: 400 });

  row.firstBlocker.nested.symbol = "MUTATED";
  assert.equal(blocker.nested.symbol, "US.WDC");
});


test("builds the exact v3 request contract without client-controlled friction or rule bodies", () => {
  const payload = buildWalkForwardRequestPayload(7, {
    train_days: "99",
    test_days: 20,
    step_days: "20",
    transaction_cost_bps: 999,
    friction_scenario_set: "caller_override",
  });

  assert.deepEqual(payload, {
    expected_portfolio_version: 7,
    version: WALK_FORWARD_CONFIG_VERSION,
    train_days: 99,
    test_days: 20,
    step_days: 20,
    price_adjustment: "QFQ",
    strategy_rule_id: WALK_FORWARD_STRATEGY_RULE_ID,
    friction_scenario_set: WALK_FORWARD_SCENARIO_SET_VERSION,
    unfillable_policy: WALK_FORWARD_UNFILLABLE_POLICY,
  });
  assert.equal("transaction_cost_bps" in payload, false);
  assert.equal("strategy_rule_contract" in payload, false);
  assert.throws(
    () => buildWalkForwardRequestPayload(7, { train_days: 99.5, test_days: 20, step_days: 20 }),
    /train_days 必须是正整数/,
  );
});


test("publishes an immutable copy of the three server-owned display assumptions", () => {
  assert.deepEqual(
    WALK_FORWARD_LOCKED_SCENARIOS.map(({ id, assumptions }) => ({ id, ...assumptions })),
    [
      {
        id: "baseline",
        paper_reference_notional_usd: 1_000_000,
        commission_bps_per_side: 10,
        entry_slippage_bps: 5,
        exit_slippage_bps: 5,
        short_borrow_fee_bps_annual: 300,
        max_daily_turnover_participation_pct: 2,
      },
      {
        id: "stressed",
        paper_reference_notional_usd: 5_000_000,
        commission_bps_per_side: 15,
        entry_slippage_bps: 25,
        exit_slippage_bps: 25,
        short_borrow_fee_bps_annual: 1_500,
        max_daily_turnover_participation_pct: 1,
      },
      {
        id: "severe",
        paper_reference_notional_usd: 10_000_000,
        commission_bps_per_side: 25,
        entry_slippage_bps: 75,
        exit_slippage_bps: 75,
        short_borrow_fee_bps_annual: 3_000,
        max_daily_turnover_participation_pct: 0.25,
      },
    ],
  );
  assert.equal(Object.isFrozen(WALK_FORWARD_LOCKED_SCENARIOS), true);
  assert.equal(Object.isFrozen(WALK_FORWARD_LOCKED_SCENARIOS[0].assumptions), true);
});
