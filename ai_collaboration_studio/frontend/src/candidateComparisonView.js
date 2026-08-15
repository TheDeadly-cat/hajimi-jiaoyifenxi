export const CANDIDATE_COMPARISON_REQUEST_VERSION = "candidate_comparison_request_v1";
export const CANDIDATE_COMPARISON_PREVIEW_VERSION = "candidate_comparison_preview_v1";
export const CANDIDATE_COMPARISON_BASIS_VERSION = "candidate_comparison_basis_v1";

const SCENARIO_IDS = Object.freeze(["baseline", "stressed", "severe"]);
const STORAGE_SYMBOLS = new Set(["US.MU", "US.SNDK", "US.WDC", "US.STX"]);
const SUPPORTED_HORIZONS = new Set([1, 5, 20]);
const REQUIRED_RUN_INTEGRITY = Object.freeze([
  "fully_verified",
  "candidate_simulation_binding_verified",
  "candidate_simulation_lineage_verified",
  "candidate_simulation_marker_binding_verified",
  "integrity_profile_verified",
  "walk_forward_v3_lineage_verified",
]);


function nonEmptyString(value) {
  return typeof value === "string" && value.trim() ? value.trim() : "";
}


function sha256(value) {
  const text = nonEmptyString(value).toLowerCase();
  return /^[0-9a-f]{64}$/.test(text) ? text : "";
}


function finiteNumber(value) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") {
    return null;
  }
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}


function jsonNumber(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}


export function buildCandidateComparisonRequest(runIds) {
  const selected = Array.isArray(runIds)
    ? runIds.map(nonEmptyString).filter(Boolean)
    : [];
  if (selected.length < 2 || selected.length > 6 || new Set(selected).size !== selected.length) {
    throw new TypeError("候选比较必须选择 2–6 条不同的回放记录。");
  }
  return {
    version: CANDIDATE_COMPARISON_REQUEST_VERSION,
    run_ids: selected,
    user_confirmed_historical_only: true,
  };
}


export function candidateComparisonEligibleRuns(portfolios, runsByPortfolio) {
  const portfolioMap = new Map(
    (Array.isArray(portfolios) ? portfolios : [])
      .filter((portfolio) => nonEmptyString(portfolio?.id))
      .map((portfolio) => [String(portfolio.id), portfolio]),
  );
  const eligible = [];
  const seenRunIds = new Set();
  for (const [portfolioId, runs] of Object.entries(runsByPortfolio || {})) {
    const portfolio = portfolioMap.get(String(portfolioId));
    const contract = portfolio?.candidate_simulation_contract;
    if (
      !portfolio
      || contract?.version !== "candidate_simulation_contract_v1"
      || contract?.user_confirmed !== true
      || !sha256(contract?.contract_sha256)
    ) continue;
    for (const run of Array.isArray(runs) ? runs : []) {
      const runId = nonEmptyString(run?.id);
      const exactPortfolioVersion = Number(run?.portfolio_version) === Number(portfolio.version);
      const exactContract = sha256(run?.candidate_simulation_contract_sha256)
        === sha256(contract.contract_sha256);
      const integrityReady = REQUIRED_RUN_INTEGRITY.every((field) => run?.[field] === true);
      if (
        !runId
        || seenRunIds.has(runId)
        || !exactPortfolioVersion
        || !exactContract
        || !integrityReady
        || run?.record_version !== 2
        || run?.engine_version !== "walk_forward_engine_v3"
        || run?.result_version !== "walk_forward_result_v3"
      ) continue;
      seenRunIds.add(runId);
      eligible.push({
        runId,
        portfolioId: String(portfolio.id),
        portfolioVersion: Number(portfolio.version),
        portfolioName: nonEmptyString(portfolio.name) || "未命名纸面组合",
        candidateId: nonEmptyString(contract?.source?.candidate_id),
        candidateTitle: nonEmptyString(contract?.source?.candidate_snapshot?.title) || "未命名候选",
        symbol: nonEmptyString(contract?.implementation?.target_symbol),
        direction: nonEmptyString(contract?.source?.candidate_snapshot?.direction),
        side: nonEmptyString(contract?.implementation?.target_side),
        weightPct: finiteNumber(contract?.implementation?.target_weight_pct),
        horizonDays: finiteNumber(contract?.evaluation?.horizon_days),
        createdAt: finiteNumber(run?.created_at) ?? 0,
        actionableNow: run?.actionable_now === true,
        sourceDecisionCurrent: run?.source_decision_current === true,
      });
    }
  }
  return eligible
    .toSorted((left, right) => right.createdAt - left.createdAt || left.runId.localeCompare(right.runId))
    .slice(0, 30);
}


function scenarioView(value) {
  const scenario = value && typeof value === "object" ? value : {};
  const metrics = scenario.metrics && typeof scenario.metrics === "object" ? scenario.metrics : {};
  const blocked = scenario.blocked === true;
  const metricsVisible = scenario.metrics_visible === true && !blocked;
  const rawMetricValues = [
    jsonNumber(metrics.portfolio_cumulative_return_pct),
    jsonNumber(metrics.historical_positive_window_ratio),
    jsonNumber(metrics.max_drawdown_pct),
    jsonNumber(metrics.mean_window_return_pct),
    jsonNumber(metrics.worst_window_return_pct),
  ];
  const [
    rawCumulativeReturnPct,
    rawHistoricalPositiveWindowRatio,
    rawMaxDrawdownPct,
    rawMeanWindowReturnPct,
    rawWorstWindowReturnPct,
  ] = rawMetricValues;
  const cumulativeReturnPct = metricsVisible ? rawCumulativeReturnPct : null;
  const historicalPositiveWindowRatio = metricsVisible ? rawHistoricalPositiveWindowRatio : null;
  const maxDrawdownPct = metricsVisible ? rawMaxDrawdownPct : null;
  const meanWindowReturnPct = metricsVisible ? rawMeanWindowReturnPct : null;
  const worstWindowReturnPct = metricsVisible ? rawWorstWindowReturnPct : null;
  return {
    id: nonEmptyString(scenario.scenario_id),
    state: nonEmptyString(scenario.state),
    blocked,
    metricsVisible,
    integrityReady: Boolean(nonEmptyString(scenario.state))
      && (blocked
        ? scenario.metrics_visible === false && rawMetricValues.every((metric) => metric === null)
        : metricsVisible
          && rawMetricValues.every((metric) => metric !== null)
          && historicalPositiveWindowRatio >= 0
          && historicalPositiveWindowRatio <= 1),
    cumulativeReturnPct,
    historicalPositiveWindowRatio,
    maxDrawdownPct,
    meanWindowReturnPct,
    worstWindowReturnPct,
    capacityGapUsd: blocked ? finiteNumber(scenario.capacity_gap_usd) : null,
    firstBlocker: blocked && scenario.first_blocker && typeof scenario.first_blocker === "object"
      ? scenario.first_blocker
      : null,
  };
}


function candidateView(value) {
  const candidate = value && typeof value === "object" ? value : {};
  const scenarios = Array.isArray(candidate.scenarios)
    ? candidate.scenarios.map(scenarioView)
    : [];
  const scenarioIds = scenarios.map((scenario) => scenario.id);
  const scenarioShapeReady = scenarioIds.length === SCENARIO_IDS.length
    && SCENARIO_IDS.every((id, index) => scenarioIds[index] === id);
  const runId = nonEmptyString(candidate.run_id);
  const portfolioId = nonEmptyString(candidate.portfolio_id);
  const portfolioVersion = jsonNumber(candidate.portfolio_version);
  const candidateId = nonEmptyString(candidate.candidate_id);
  const candidateRevision = jsonNumber(candidate.candidate_revision);
  const candidateSnapshotSha256 = sha256(candidate.candidate_snapshot_sha256);
  const symbol = nonEmptyString(candidate.symbol);
  const direction = nonEmptyString(candidate.direction);
  const side = nonEmptyString(candidate.side);
  const weightPct = jsonNumber(candidate.target_weight_pct);
  const horizonDays = jsonNumber(candidate.horizon_days);
  const contractSha256 = sha256(candidate.contract_sha256);
  const candidateIntegrityReady = runId
    && portfolioId
    && Number.isInteger(portfolioVersion)
    && portfolioVersion > 0
    && candidateId
    && Number.isInteger(candidateRevision)
    && candidateRevision > 0
    && candidateSnapshotSha256
    && STORAGE_SYMBOLS.has(symbol)
    && ["UP", "DOWN"].includes(direction)
    && side === (direction === "UP" ? "LONG" : "SHORT")
    && weightPct !== null
    && weightPct > 0
    && SUPPORTED_HORIZONS.has(horizonDays)
    && contractSha256;
  return {
    runId,
    portfolioId,
    portfolioVersion,
    candidateId,
    candidateRevision,
    candidateSnapshotSha256,
    title: nonEmptyString(candidate.title) || "未命名候选",
    symbol,
    direction,
    side,
    weightPct,
    horizonDays,
    thesis: nonEmptyString(candidate.thesis),
    invalidation: nonEmptyString(candidate.invalidation),
    contractSha256,
    sourceDecisionCurrent: candidate.source_decision_current === true,
    actionableNow: candidate.actionable_now === true,
    metricsVisible: Boolean(candidateIntegrityReady)
      && candidate.metrics_visible === true
      && scenarioShapeReady
      && scenarios.every((scenario) => scenario.integrityReady),
    scenarios: scenarioShapeReady ? scenarios : [],
  };
}


export function candidateComparisonView(value) {
  const comparison = value && typeof value === "object" ? value : {};
  const candidates = Array.isArray(comparison.candidates)
    ? comparison.candidates.map(candidateView)
    : [];
  const issues = Array.isArray(comparison.issues)
    ? comparison.issues.map((issue) => ({
        code: nonEmptyString(issue?.code),
        message: nonEmptyString(issue?.message),
        runId: nonEmptyString(issue?.run_id),
      })).filter((issue) => issue.code)
    : [];
  const basis = comparison.comparison_basis && typeof comparison.comparison_basis === "object"
    ? comparison.comparison_basis
    : {};
  const candidateIds = candidates.map((candidate) => candidate.runId).filter(Boolean);
  const selectedRunIds = Array.isArray(comparison.selected_run_ids)
    ? comparison.selected_run_ids.map(nonEmptyString).filter(Boolean)
    : [];
  const candidatesReady = candidates.length >= 2
    && candidates.length <= 6
    && candidateIds.length === candidates.length
    && new Set(candidateIds).size === candidates.length
    && candidates.every((candidate) => (
      candidate.candidateId
      && candidate.contractSha256
      && candidate.metricsVisible
    ));
  const exactCandidateKeys = candidates.map((candidate) => (
    `${candidate.candidateId}:${candidate.candidateRevision}:${candidate.candidateSnapshotSha256}`
  ));
  const candidateVersionsUnique = new Set(exactCandidateKeys).size === exactCandidateKeys.length;
  const selectionReady = selectedRunIds.length === candidateIds.length
    && selectedRunIds.every((runId, index) => runId === candidateIds[index]);
  const metricSemantics = comparison.metric_semantics
    && typeof comparison.metric_semantics === "object"
    ? comparison.metric_semantics
    : {};
  const semanticsReady = nonEmptyString(metricSemantics.historical_positive_window_ratio)
    && metricSemantics.ranking_produced === false
    && metricSemantics.winner_claim === false
    && metricSemantics.user_final_decision_required === true;
  const safetyReady = comparison.execution_capability === "none"
    && comparison.live_trading_allowed === false
    && comparison.can_autonomously_decide === false
    && comparison.provider_calls_total === 0
    && comparison.openai_calls === 0
    && comparison.market_data_reads === 0
    && comparison.historical_only === true
    && comparison.out_of_sample_claim === false
    && comparison.future_performance_claim === false
    && comparison.user_confirmed_historical_only === true;
  const basisWeightPct = jsonNumber(basis.target_weight_pct);
  const basisTrainDays = jsonNumber(basis.train_days);
  const basisTestDays = jsonNumber(basis.test_days);
  const basisStepDays = jsonNumber(basis.step_days);
  const commonTradingDays = jsonNumber(basis.common_trading_days);
  const actualStart = nonEmptyString(basis.actual_start);
  const actualEnd = nonEmptyString(basis.actual_end);
  const basisReady = basis.version === CANDIDATE_COMPARISON_BASIS_VERSION
    && sha256(comparison.comparison_basis_sha256)
    && sha256(basis.dataset_content_sha256)
    && sha256(basis.walk_forward_config_sha256)
    && sha256(basis.friction_scenario_set_sha256)
    && sha256(basis.candidate_evaluation_basis_sha256)
    && basis.record_version === 2
    && basis.engine_version === "walk_forward_engine_v3"
    && basis.result_version === "walk_forward_result_v3"
    && basis.input_snapshot_version === "walk_forward_input_snapshot_v2"
    && basis.price_adjustment === "QFQ"
    && basis.friction_scenario_set === "storage_friction_scenarios_v1"
    && basis.unfillable_policy === "block_scenario_no_partial_fill"
    && basisWeightPct !== null
    && basisWeightPct > 0
    && Number.isInteger(basisTrainDays)
    && basisTrainDays > 0
    && SUPPORTED_HORIZONS.has(basisTestDays)
    && basisStepDays === basisTestDays
    && Number.isInteger(commonTradingDays)
    && commonTradingDays > 0
    && /^\d{4}-\d{2}-\d{2}$/.test(actualStart)
    && /^\d{4}-\d{2}-\d{2}$/.test(actualEnd)
    && actualStart <= actualEnd;
  const candidatesMatchBasis = candidates.every((candidate) => (
    candidate.weightPct === basisWeightPct
    && candidate.horizonDays === basisTestDays
  ));
  const ready = Boolean(comparison.version === CANDIDATE_COMPARISON_PREVIEW_VERSION
    && comparison.ready === true
    && comparison.status === "ready"
    && comparison.metrics_visible === true
    && issues.length === 0
    && safetyReady
    && semanticsReady
    && Boolean(basisReady)
    && candidatesReady
    && candidatesMatchBasis
    && candidateVersionsUnique
    && selectionReady
    && sha256(comparison.preview_sha256));
  return {
    ready,
    metricsVisible: ready,
    status: ready ? "ready" : "blocked",
    issues: ready ? [] : (issues.length ? issues : [{
      code: "CANDIDATE_COMPARISON_RESPONSE_INVALID",
      message: "候选比较响应未通过客户端完整性与安全校验。",
      runId: "",
    }]),
    basis: ready ? basis : {},
    basisSha256: ready ? sha256(comparison.comparison_basis_sha256) : "",
    previewSha256: ready ? sha256(comparison.preview_sha256) : "",
    selectedRunIds,
    candidates: ready
      ? candidates
      : candidates.map((candidate) => ({
          ...candidate,
          metricsVisible: false,
          scenarios: [],
        })),
    historicalOnly: true,
    rankingProduced: false,
    winnerClaim: false,
    userFinalDecisionRequired: true,
  };
}


export const CANDIDATE_COMPARISON_SCENARIO_IDS = SCENARIO_IDS;
