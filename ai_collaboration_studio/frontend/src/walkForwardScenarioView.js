import { walkForwardIntegrityState } from "./walkForwardIntegrity.js";


export const WALK_FORWARD_CONFIG_VERSION = "walk_forward_config_v3";
export const CANDIDATE_WALK_FORWARD_CONFIG_VERSION = "walk_forward_config_v2";
export const WALK_FORWARD_SCENARIO_SET_VERSION = "storage_friction_scenarios_v1";
export const WALK_FORWARD_UNFILLABLE_POLICY = "block_scenario_no_partial_fill";
export const WALK_FORWARD_STRATEGY_RULE_ID = "cross_sectional_total_return_rank_v1";
export const WALK_FORWARD_STRATEGY_CONTRACT_VERSION = "strategy_rule_contract_v1";
export const CANDIDATE_SIMULATION_CONTRACT_VERSION = "candidate_simulation_contract_v1";
export const CANDIDATE_SIMULATION_RULE_ID = "fixed_candidate_direction_replay_v1";

const WALK_FORWARD_V4_CONTRACT = Object.freeze({
  recordVersion: 3,
  engineVersion: "walk_forward_engine_v4",
  resultVersion: "walk_forward_result_v4",
  inputSnapshotVersion: "walk_forward_input_snapshot_v3",
  evaluationMode: "fold_train_only_next_session_test_replay",
  strategyProvenance: "server_whitelisted_fold_trained_rule",
});

export const WALK_FORWARD_LOCKED_SCENARIOS = Object.freeze([
  Object.freeze({
    id: "baseline",
    label: "基准摩擦",
    assumptions: Object.freeze({
      paper_reference_notional_usd: 1_000_000,
      commission_bps_per_side: 10,
      entry_slippage_bps: 5,
      exit_slippage_bps: 5,
      short_borrow_fee_bps_annual: 300,
      max_daily_turnover_participation_pct: 2,
    }),
  }),
  Object.freeze({
    id: "stressed",
    label: "压力摩擦",
    assumptions: Object.freeze({
      paper_reference_notional_usd: 5_000_000,
      commission_bps_per_side: 15,
      entry_slippage_bps: 25,
      exit_slippage_bps: 25,
      short_borrow_fee_bps_annual: 1_500,
      max_daily_turnover_participation_pct: 1,
    }),
  }),
  Object.freeze({
    id: "severe",
    label: "极端摩擦",
    assumptions: Object.freeze({
      paper_reference_notional_usd: 10_000_000,
      commission_bps_per_side: 25,
      entry_slippage_bps: 75,
      exit_slippage_bps: 75,
      short_borrow_fee_bps_annual: 3_000,
      max_daily_turnover_participation_pct: 0.25,
    }),
  }),
]);

const SCENARIOS = WALK_FORWARD_LOCKED_SCENARIOS;

const ASSUMPTION_FIELDS = Object.freeze([
  "paper_reference_notional_usd",
  "commission_bps_per_side",
  "entry_slippage_bps",
  "exit_slippage_bps",
  "short_borrow_fee_bps_annual",
  "max_daily_turnover_participation_pct",
]);

const V2_LEGACY_FRICTION_WARNING = Object.freeze({
  code: "LEGACY_FRICTION_MODEL_V2",
  message: "该 v2 记录使用旧版单一交易成本模型，未包含 v3 的三档滑点、借券费与容量约束；保留历史结果，不将其改写为 v3。",
});


function finiteNumber(value) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") {
    return null;
  }
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}


function strictPositiveInteger(value, field) {
  const number = finiteNumber(value);
  if (!Number.isInteger(number) || number < 1) {
    throw new TypeError(`${field} 必须是正整数`);
  }
  return number;
}


export function buildWalkForwardRequestPayload(portfolioVersion, config, candidateContract = null) {
  const candidateEvaluation = candidateContract?.version === CANDIDATE_SIMULATION_CONTRACT_VERSION
    && candidateContract?.user_confirmed === true
    ? candidateContract.evaluation
    : null;
  if (candidateEvaluation) {
    if (candidateEvaluation.rule_id !== CANDIDATE_SIMULATION_RULE_ID) {
      throw new TypeError("候选模拟合同使用了不受支持的历史回放规则");
    }
    const horizon = strictPositiveInteger(candidateEvaluation.horizon_days, "candidate horizon_days");
    const requestedTestDays = strictPositiveInteger(config?.test_days, "test_days");
    const requestedStepDays = strictPositiveInteger(config?.step_days, "step_days");
    if (requestedTestDays !== horizon || requestedStepDays !== horizon) {
      throw new TypeError("测试窗口与步进必须等于候选合同期限");
    }
    return {
      expected_portfolio_version: strictPositiveInteger(
        portfolioVersion,
        "expected_portfolio_version",
      ),
      version: CANDIDATE_WALK_FORWARD_CONFIG_VERSION,
      train_days: strictPositiveInteger(config?.train_days, "train_days"),
      test_days: horizon,
      step_days: horizon,
      price_adjustment: "QFQ",
      friction_scenario_set: WALK_FORWARD_SCENARIO_SET_VERSION,
      unfillable_policy: WALK_FORWARD_UNFILLABLE_POLICY,
    };
  }
  return {
    expected_portfolio_version: strictPositiveInteger(
      portfolioVersion,
      "expected_portfolio_version",
    ),
    version: WALK_FORWARD_CONFIG_VERSION,
    train_days: strictPositiveInteger(config?.train_days, "train_days"),
    test_days: strictPositiveInteger(config?.test_days, "test_days"),
    step_days: strictPositiveInteger(config?.step_days, "step_days"),
    price_adjustment: "QFQ",
    strategy_rule_id: WALK_FORWARD_STRATEGY_RULE_ID,
    friction_scenario_set: WALK_FORWARD_SCENARIO_SET_VERSION,
    unfillable_policy: WALK_FORWARD_UNFILLABLE_POLICY,
  };
}


function cloneJsonValue(value) {
  if (Array.isArray(value)) return value.map(cloneJsonValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, cloneJsonValue(item)]),
    );
  }
  return value;
}


function firstDefined(...values) {
  return values.find((value) => value !== null && value !== undefined) ?? null;
}


function nonEmptyString(value) {
  return typeof value === "string" && value.trim() ? value.trim() : "";
}


function isSha256(value) {
  return /^[0-9a-f]{64}$/i.test(nonEmptyString(value));
}


function v4IntegrityReady(run) {
  return [
    "strategy_contract_hash_verified",
    "decision_anchor_hash_verified",
    "decision_binding_verified",
    "lineage_binding_verified",
    "result_recomputed_verified",
  ].every((field) => run?.[field] === true);
}


function v4VersionBindingReady(run, result) {
  const runConfig = run?.config && typeof run.config === "object" ? run.config : {};
  const resultConfig = result?.config && typeof result.config === "object" ? result.config : {};
  return Number(run?.record_version) === WALK_FORWARD_V4_CONTRACT.recordVersion
    && run?.engine_version === WALK_FORWARD_V4_CONTRACT.engineVersion
    && run?.result_version === WALK_FORWARD_V4_CONTRACT.resultVersion
    && result?.engine_version === WALK_FORWARD_V4_CONTRACT.engineVersion
    && result?.version === WALK_FORWARD_V4_CONTRACT.resultVersion
    && result?.input_snapshot_version === WALK_FORWARD_V4_CONTRACT.inputSnapshotVersion
    && runConfig.version === WALK_FORWARD_CONFIG_VERSION
    && resultConfig.version === WALK_FORWARD_CONFIG_VERSION;
}


function v4SemanticsReady(result) {
  return result?.evaluation_mode === WALK_FORWARD_V4_CONTRACT.evaluationMode
    && result?.strategy_provenance === WALK_FORWARD_V4_CONTRACT.strategyProvenance
    && result?.out_of_sample_claim === false
    && result?.future_performance_claim === false
    && result?.retrospective_dataset === true;
}


function strategyContractView(run, result) {
  const contract = result?.strategy_rule_contract && typeof result.strategy_rule_contract === "object"
    ? result.strategy_rule_contract
    : {};
  const resultHash = nonEmptyString(result?.strategy_contract_sha256);
  const storedHash = nonEmptyString(run?.strategy_contract_sha256);
  const hashReady = isSha256(resultHash)
    && (!storedHash || (isSha256(storedHash) && storedHash === resultHash));
  const ready = contract.version === WALK_FORWARD_STRATEGY_CONTRACT_VERSION
    && contract.rule_id === WALK_FORWARD_STRATEGY_RULE_ID
    && nonEmptyString(contract.fit_scope)
    && contract.test_data_excluded_from_fit === true
    && hashReady;
  return {
    ready,
    version: nonEmptyString(contract.version),
    ruleId: nonEmptyString(contract.rule_id),
    signal: nonEmptyString(contract.signal),
    fitScope: nonEmptyString(contract.fit_scope),
    ranking: nonEmptyString(contract.ranking),
    longCount: finiteNumber(contract.long_count),
    shortCount: finiteNumber(contract.short_count),
    longBudgetPct: finiteNumber(contract.long_budget_pct),
    shortBudgetPct: finiteNumber(contract.short_budget_pct),
    weighting: nonEmptyString(contract.weighting),
    rebalance: nonEmptyString(contract.rebalance),
    testDataExcludedFromFit: contract.test_data_excluded_from_fit === true,
    universe: Array.isArray(contract.universe)
      ? contract.universe.map(nonEmptyString).filter(Boolean)
      : [],
    sha256: hashReady ? resultHash : "",
  };
}


function evaluationView(result, isV4) {
  if (isV4) {
    const ready = v4SemanticsReady(result);
    return {
      kind: ready ? "prospective" : "invalid",
      ready,
      label: ready ? "逐折训练 → 历史测试" : "评估语义无效",
      detail: ready
        ? "每折规则仅使用该折训练窗信息生成，再进入随后历史测试窗；这是历史 walk-forward 证据，不是未来实盘胜率或成交承诺。"
        : "v4 必须是训练窗专属规则、下一交易日历史测试回放，并明确不声称样本外或未来表现。",
      positiveRateLabel: "历史测试正收益窗口比例（非未来胜率）",
    };
  }
  const ready = result?.evaluation_mode === "retroactive_fixed_plan_replay"
    && result?.strategy_provenance === "current_plan_retroactive"
    && result?.out_of_sample_claim === false;
  return {
    kind: ready ? "retroactive" : "invalid",
    ready,
    label: ready ? "当前方案追溯回放" : "评估语义无效",
    detail: ready
      ? "同一已确认组合被应用到历史窗口；当前方案可能参考过全段历史，因此不是策略样本外验证。"
      : "旧版结果必须明确标记为 current_plan_retroactive 且 out_of_sample_claim=false。",
    positiveRateLabel: "历史测试正收益窗口比例（非未来胜率）",
  };
}


function emptyAssumptions() {
  return Object.fromEntries(ASSUMPTION_FIELDS.map((field) => [field, null]));
}


function scenarioAssumptions(value) {
  const assumptions = value && typeof value === "object" ? value : {};
  return Object.fromEntries(
    ASSUMPTION_FIELDS.map((field) => [field, finiteNumber(assumptions[field])]),
  );
}


function firstBlocker(scenario) {
  if (scenario?.first_blocker !== null && scenario?.first_blocker !== undefined) {
    return cloneJsonValue(scenario.first_blocker);
  }
  if (!Array.isArray(scenario?.folds)) return null;
  for (const fold of scenario.folds) {
    if (fold?.first_blocker !== null && fold?.first_blocker !== undefined) {
      return cloneJsonValue(fold.first_blocker);
    }
    if (
      fold?.fillability?.first_blocker !== null
      && fold?.fillability?.first_blocker !== undefined
    ) {
      return cloneJsonValue(fold.fillability.first_blocker);
    }
    if (!Array.isArray(fold?.fillability_blockers)) continue;
    const blocker = fold.fillability_blockers.find(
      (item) => item !== null && item !== undefined,
    );
    if (blocker !== undefined) return cloneJsonValue(blocker);
  }
  return null;
}


function capacityGap(scenario, blocker) {
  const summary = scenario?.summary && typeof scenario.summary === "object"
    ? scenario.summary
    : {};
  const blockerObject = blocker && typeof blocker === "object" ? blocker : {};
  return cloneJsonValue(firstDefined(
    scenario?.capacity_gap,
    scenario?.capacity_gap_usd,
    summary.capacity_gap,
    summary.capacity_gap_usd,
    blockerObject.capacity_gap,
    blockerObject.capacity_gap_usd,
    blockerObject.capacity_shortfall_usd,
  ));
}


function hiddenRows(state) {
  return SCENARIOS.map((scenario) => ({
    id: scenario.id,
    label: scenario.label,
    state,
    available: false,
    blocked: false,
    metricsVisible: false,
    assumptions: emptyAssumptions(),
    portfolioCumulativeReturnPct: null,
    historicalPositiveFoldRatio: null,
    maxDrawdownPct: null,
    capacityGap: null,
    firstBlocker: null,
  }));
}


function scenarioRow(definition, scenario) {
  if (!scenario) {
    return {
      ...hiddenRows("missing").find((row) => row.id === definition.id),
      label: definition.label,
    };
  }

  const nestedScenario = scenario.scenario && typeof scenario.scenario === "object"
    ? scenario.scenario
    : {};
  const state = String(scenario.state || scenario.status || "unknown").trim().toLowerCase() || "unknown";
  const formalUnfillableFoldCount = finiteNumber(scenario.formal_unfillable_fold_count) ?? 0;
  const blocked = scenario.blocked === true
    || state === "blocked"
    || formalUnfillableFoldCount > 0;
  const summary = scenario.summary && typeof scenario.summary === "object"
    ? scenario.summary
    : {};
  const blocker = blocked ? firstBlocker(scenario) : null;

  return {
    id: definition.id,
    label: String(scenario.label || nestedScenario.label || definition.label),
    state,
    available: true,
    blocked,
    metricsVisible: !blocked,
    assumptions: scenarioAssumptions(scenario.assumptions || nestedScenario),
    portfolioCumulativeReturnPct: blocked
      ? null
      : finiteNumber(summary.portfolio_cumulative_return_pct),
    historicalPositiveFoldRatio: blocked
      ? null
      : finiteNumber(summary.historical_positive_fold_ratio),
    maxDrawdownPct: blocked ? null : finiteNumber(summary.max_drawdown_pct),
    capacityGap: blocked ? capacityGap(scenario, blocker) : null,
    firstBlocker: blocker,
  };
}


function scenarioFoldState(scenario, foldId) {
  if (!scenario || !foldId) {
    return { state: "missing", label: "未返回", blocker: null };
  }
  const scenarioState = String(scenario.state || scenario.status || "").trim().toLowerCase();
  const scenarioBlocked = scenario.blocked === true
    || scenarioState === "blocked"
    || (finiteNumber(scenario.formal_unfillable_fold_count) ?? 0) > 0;
  const fold = Array.isArray(scenario.folds)
    ? scenario.folds.find((item) => String(item?.fold_id || "") === foldId)
    : null;
  if (scenarioBlocked) {
    return {
      state: "blocked",
      label: "整档阻断 · 收益隐藏",
      blocker: firstBlocker(scenario),
    };
  }
  if (!fold) return { state: "missing", label: "未返回", blocker: null };
  if (fold.blocked === true || String(fold.status || "").toLowerCase() === "blocked") {
    return {
      state: "blocked",
      label: "该折阻断 · 收益隐藏",
      blocker: firstBlocker({ folds: [fold] }),
    };
  }
  return { state: "ready", label: "可评估", blocker: null };
}


function strategyDecisionView(fold) {
  const decision = fold?.strategy_decision && typeof fold.strategy_decision === "object"
    ? fold.strategy_decision
    : {};
  const selectedParameters = firstDefined(
    decision.selected_parameters,
    fold?.selected_parameters,
  );
  const trainSelectionEvidence = firstDefined(
    decision.train_selection_evidence,
    fold?.train_selection_evidence,
    decision.scores_pct && decision.ranking
      ? { scores_pct: decision.scores_pct, ranking: decision.ranking }
      : null,
  );
  const generatedPositions = firstDefined(
    decision.generated_positions,
    fold?.generated_positions,
    decision.selected_positions,
  );
  return {
    selectedParameters: cloneJsonValue(
      selectedParameters && typeof selectedParameters === "object" ? selectedParameters : {},
    ),
    trainSelectionEvidence: cloneJsonValue(
      trainSelectionEvidence && typeof trainSelectionEvidence === "object"
        ? trainSelectionEvidence
        : {},
    ),
    generatedPositions: Array.isArray(generatedPositions)
      ? cloneJsonValue(generatedPositions)
      : generatedPositions && typeof generatedPositions === "object"
        ? Object.entries(generatedPositions).map(([symbol, value]) => ({
            symbol,
            ...(value && typeof value === "object" ? cloneJsonValue(value) : { weight_pct: value }),
          }))
        : [],
    decisionInputHash: nonEmptyString(
      decision.decision_input_hash
      || decision.fit_input_hash
      || decision.fit_hash
      || fold?.decision_input_hash,
    ),
  };
}


function v4FoldRows(result, scenariosById) {
  const baseline = scenariosById.get("baseline");
  const sourceFolds = Array.isArray(baseline?.folds)
    ? baseline.folds
    : Array.isArray(result?.folds)
      ? result.folds
      : [];
  return sourceFolds
    .filter((fold) => fold?.non_overlapping_test_window === true)
    .map((fold) => {
      const foldId = nonEmptyString(fold.fold_id);
      return {
        id: foldId,
        index: finiteNumber(fold.fold_index),
        trainStart: nonEmptyString(fold.train_start),
        trainEnd: nonEmptyString(fold.train_end),
        decisionCutoff: nonEmptyString(fold.decision_cutoff),
        scheduledEntryDate: nonEmptyString(
          fold.scheduled_entry_date || fold.execution_start,
        ),
        testStart: nonEmptyString(fold.test_start),
        testEnd: nonEmptyString(fold.test_end),
        strategyDecision: strategyDecisionView(fold),
        scenarios: SCENARIOS.map((definition) => ({
          id: definition.id,
          name: definition.label,
          ...scenarioFoldState(scenariosById.get(definition.id), foldId),
        })),
      };
    });
}


function gateViews(result, rows, metricsVisible) {
  const summary = result?.summary && typeof result.summary === "object" ? result.summary : {};
  const foldCount = finiteNumber(summary.non_overlapping_test_fold_count)
    ?? finiteNumber(summary.independent_fold_count)
    ?? 0;
  const configuredMinimum = finiteNumber(summary.minimum_non_overlapping_test_folds);
  const minimumFoldCount = Number.isInteger(configuredMinimum) && configuredMinimum > 0
    ? configuredMinimum
    : 20;
  const dataReady = metricsVisible && foldCount >= minimumFoldCount;
  const availableRows = rows.filter((row) => row.available);
  const evaluableRows = availableRows.filter((row) => !row.blocked);
  const capacityReady = metricsVisible
    && availableRows.length === SCENARIOS.length
    && evaluableRows.length === SCENARIOS.length;
  return {
    dataGate: {
      ready: dataReady,
      actual: foldCount,
      required: minimumFoldCount,
      label: dataReady ? "窗口数达最低门槛" : "窗口数未达最低门槛",
      detail: `${foldCount}/${minimumFoldCount} 个非重叠历史测试窗口`,
    },
    capacityGate: {
      ready: capacityReady,
      evaluable: evaluableRows.length,
      required: SCENARIOS.length,
      label: capacityReady ? "三档均可评估" : "部分情景容量阻断",
      detail: `${evaluableRows.length}/${SCENARIOS.length} 档可评估`,
    },
  };
}


/**
 * Build the read-only rendering contract for walk-forward friction scenarios.
 *
 * Integrity visibility is delegated to walkForwardIntegrityState. This helper
 * adds version-aware fail-closed behavior: v1/unknown versions expose no audit
 * metrics, verified v2 results retain their old top-level metrics with a
 * warning but gain no fabricated scenario data, verified v3 records populate
 * the legacy friction view, and v4 additionally requires the exact strategy,
 * decision-anchor, lineage, version, and deterministic-recompute bindings.
 */
export function walkForwardScenarioView(run) {
  const baseIntegrity = walkForwardIntegrityState(run);
  const result = run?.result && typeof run.result === "object" ? run.result : {};
  const resultVersion = String(result.version || run?.result_version || "").trim();
  const isV4 = resultVersion === WALK_FORWARD_V4_CONTRACT.resultVersion;
  const isV3 = resultVersion === "walk_forward_result_v3";
  const isV2 = resultVersion === "walk_forward_result_v2";
  const evaluation = evaluationView(result, isV4);
  const strategyContract = strategyContractView(run, result);
  const v4AuditReady = isV4
    && baseIntegrity.metricsVisible
    && v4IntegrityReady(run)
    && v4VersionBindingReady(run, result)
    && v4SemanticsReady(result)
    && strategyContract.ready;
  const integrity = isV4 && baseIntegrity.metricsVisible && !v4AuditReady
    ? {
        ...baseIntegrity,
        metricsVisible: false,
        label: "v4 审计绑定未通过",
        detail: "策略合同、决定锚点、谱系绑定、版本矩阵或确定性重算未全部通过，规则、逐折结果和收益指标已隐藏。",
      }
    : baseIntegrity;
  const auditMetricsVisible = integrity.metricsVisible
    && (isV2 || isV3 || v4AuditReady);
  const actionableValue = firstDefined(run?.actionable_now, result?.actionable_now);
  const actionableNow = typeof actionableValue === "boolean" ? actionableValue : null;

  if (!auditMetricsVisible) {
    const rows = hiddenRows("hidden");
    return {
      integrity,
      resultVersion,
      metricsVisible: false,
      scenarioMetricsVisible: false,
      ruleContractVisible: false,
      legacyFrictionWarning: null,
      evaluation,
      strategyContract: null,
      foldRows: [],
      actionableNow,
      ...gateViews(result, rows, false),
      rows,
    };
  }

  if (isV2) {
    const rows = hiddenRows("legacy_unavailable");
    return {
      integrity,
      resultVersion,
      metricsVisible: true,
      scenarioMetricsVisible: false,
      ruleContractVisible: false,
      legacyFrictionWarning: { ...V2_LEGACY_FRICTION_WARNING },
      evaluation,
      strategyContract: null,
      foldRows: [],
      actionableNow,
      ...gateViews(result, rows, true),
      rows,
    };
  }

  const sourceRows = Array.isArray(result.scenario_results)
    ? result.scenario_results
    : [];
  const byId = new Map();
  for (const scenario of sourceRows) {
    const id = String(
      scenario?.id || scenario?.scenario_id || scenario?.scenario?.scenario_id || "",
    ).trim().toLowerCase();
    if (SCENARIOS.some((definition) => definition.id === id) && !byId.has(id)) {
      byId.set(id, scenario);
    }
  }

  const rows = SCENARIOS.map((definition) => scenarioRow(definition, byId.get(definition.id)));
  return {
    integrity,
    resultVersion,
    metricsVisible: true,
    scenarioMetricsVisible: true,
    ruleContractVisible: isV4,
    legacyFrictionWarning: null,
    evaluation,
    strategyContract: isV4 ? strategyContract : null,
    foldRows: isV4 ? v4FoldRows(result, byId) : [],
    actionableNow,
    ...gateViews(result, rows, true),
    rows,
  };
}


export const WALK_FORWARD_SCENARIO_IDS = Object.freeze(
  SCENARIOS.map((scenario) => scenario.id),
);
