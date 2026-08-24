import { artifactCandidateGovernance } from "./candidateGovernance.js";


export const CANDIDATE_EXPERIMENT_REQUEST_VERSION = "candidate_experiment_request_v1";
export const CANDIDATE_EXPERIMENT_AUTHORIZATION_VERSION = "candidate_experiment_authorization_v1";
export const CANDIDATE_EXPERIMENT_COHORT_VERSION = "candidate_experiment_cohort_v1";
export const CANDIDATE_EXPERIMENT_ARM_LIMIT = 6;
export const CANDIDATE_EXPERIMENT_EVIDENCE_LIMIT = 100;
export const CANDIDATE_EXPERIMENT_ISSUE_LIMIT = 200;
export const CANDIDATE_EXPERIMENT_RECORD_FIELD_LIMIT = 100;

const SCENARIO_IDS = Object.freeze(["baseline", "stressed", "severe"]);
const STORAGE_SYMBOLS = new Set(["US.MU", "US.SNDK", "US.WDC", "US.STX"]);
const FORBIDDEN_AUTHORIZATION_KEYS = Object.freeze([
  "action",
  "support",
  "user_decision_id",
  "artifact_user_decision_id",
  "candidate_simulation_contract",
  "candidate_simulation_contract_sha256",
  "run_id",
  "run_ids",
]);


function isRecord(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}


function text(value, maxLength = 1000) {
  return typeof value === "string" && value.trim()
    ? value.trim().slice(0, maxLength)
    : "";
}


function sha256(value) {
  const normalized = text(value).toLowerCase();
  return /^[0-9a-f]{64}$/.test(normalized) ? normalized : "";
}


function positiveInteger(value) {
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}


function strictNumber(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}


function fixedSafetyReady(value) {
  return value?.execution_capability === "none"
    && value?.live_trading_allowed === false
    && value?.can_autonomously_decide === false
    && value?.ranking_produced === false
    && value?.winner_claim === false
    && value?.user_final_decision_required === true;
}


function appendIssue(issues, code, message) {
  if (issues.some((issue) => issue.code === code)) return;
  issues.push({ code, message });
}


function artifactBinding(artifact) {
  const snapshot = isRecord(artifact?.governance_snapshot)
    ? artifact.governance_snapshot
    : {};
  return isRecord(snapshot.artifact) ? snapshot.artifact : {};
}


function matchingCurrentReviews(governance, candidate) {
  return governance.riskReview.reviews.filter((review) => (
    review.candidateId === candidate.id
    && review.status === "current"
    && review.candidateRevision === candidate.revision
    && review.candidateLatestMessageId === candidate.latestMessageId
    && sha256(review.candidateSnapshotSha256)
    && isRecord(review.candidateSnapshot)
  ));
}


/**
 * Resolve exact candidates that may be included in a historical cohort.
 * This authorization is intentionally independent from artifact_user_decision_v2.
 */
export function candidateExperimentAuthorizationGate(artifact) {
  const issues = [];
  const governance = artifactCandidateGovernance(artifact);
  const artifactId = text(artifact?.id);
  const artifactVersion = positiveInteger(artifact?.version);
  const binding = artifactBinding(artifact);
  const boundArtifactId = text(binding.artifact_id);
  const boundArtifactVersion = positiveInteger(binding.artifact_version);
  const attestationSha256 = sha256(governance.attestationSha256);

  if (text(artifact?.status).toUpperCase() !== "CONFIRMED") {
    appendIssue(issues, "ARTIFACT_NOT_CONFIRMED", "只有已确认产物才能授权决定前历史联合实验。");
  }
  if (artifact?.evidence_review?.confirmation_ready === false) {
    appendIssue(issues, "ARTIFACT_CONFIRMATION_INVALID", "当前确认版本未通过现行证据门。");
  }
  if (!artifactId || !artifactVersion) {
    appendIssue(issues, "ARTIFACT_IDENTITY_INVALID", "产物 ID 或版本无效。");
  }
  if (!governance.available || !governance.applicable || !governance.ready) {
    appendIssue(issues, "GOVERNANCE_NOT_READY", "候选治理证明未就绪或不适用于本产物。");
  }
  if (!governance.integrityOk || !governance.safetyOk) {
    appendIssue(issues, "GOVERNANCE_INTEGRITY_INVALID", "候选治理证明完整性或无执行能力边界未通过。");
  }
  if (!attestationSha256) {
    appendIssue(issues, "GOVERNANCE_ATTESTATION_INVALID", "治理证明 SHA-256 缺失或格式无效。");
  }
  if (
    !artifactId
    || !artifactVersion
    || boundArtifactId !== artifactId
    || boundArtifactVersion !== artifactVersion
  ) {
    appendIssue(issues, "ARTIFACT_VERSION_DRIFT", "治理证明绑定的产物精确版本与当前产物不一致。");
  }
  if (!governance.riskReview.applicable || !governance.riskReview.ready) {
    appendIssue(issues, "RISK_REVIEW_NOT_READY", "联合实验要求每个候选都有当前精确版本风控复核。");
  }

  const seenIds = new Set();
  const candidates = [];
  for (const candidate of governance.lineage.candidates) {
    if (!candidate.id || seenIds.has(candidate.id)) {
      appendIssue(
        issues,
        candidate.id ? `CANDIDATE_DUPLICATE:${candidate.id}` : "CANDIDATE_ID_MISSING",
        candidate.id ? `治理快照重复候选 ${candidate.id}。` : "治理快照包含无 ID 候选。",
      );
      continue;
    }
    seenIds.add(candidate.id);
    if (
      !positiveInteger(candidate.revision)
      || !text(candidate.originMessageId)
      || !text(candidate.latestMessageId)
    ) {
      appendIssue(
        issues,
        `CANDIDATE_LINEAGE_INVALID:${candidate.id}`,
        `候选 ${candidate.id} 缺少精确 revision 或来源消息绑定。`,
      );
      continue;
    }
    const reviews = matchingCurrentReviews(governance, candidate);
    const snapshotHashes = new Set(
      reviews.map((review) => sha256(review.candidateSnapshotSha256)).filter(Boolean),
    );
    if (!reviews.length || snapshotHashes.size !== 1) {
      appendIssue(
        issues,
        `CANDIDATE_SNAPSHOT_INVALID:${candidate.id}`,
        `候选 ${candidate.id} 缺少唯一的当前精确快照封印。`,
      );
      continue;
    }
    const review = reviews[0];
    const snapshot = review.candidateSnapshot;
    const symbol = text(snapshot.symbol, 40).toUpperCase();
    const direction = text(snapshot.direction, 20).toUpperCase();
    const horizonDays = positiveInteger(snapshot.horizon_days);
    if (
      !text(snapshot.title, 400)
      || !STORAGE_SYMBOLS.has(symbol)
      || !["UP", "DOWN"].includes(direction)
      || !horizonDays
      || !text(snapshot.thesis, 4000)
      || !text(snapshot.invalidation, 2000)
    ) {
      appendIssue(
        issues,
        `CANDIDATE_SNAPSHOT_FIELDS_INVALID:${candidate.id}`,
        `候选 ${candidate.id} 的冻结快照缺少历史实验所需字段。`,
      );
      continue;
    }
    candidates.push({
      id: candidate.id,
      title: text(snapshot.title, 400) || candidate.title || candidate.id,
      revision: candidate.revision,
      originMessageId: candidate.originMessageId,
      latestMessageId: candidate.latestMessageId,
      snapshotSha256: [...snapshotHashes][0],
      symbol,
      direction,
      horizonDays,
      thesis: text(snapshot.thesis, 4000),
      invalidation: text(snapshot.invalidation, 2000),
    });
  }
  if (candidates.length < 2) {
    appendIssue(issues, "CANDIDATE_COUNT_INSUFFICIENT", "至少需要两个精确治理候选才能授权联合实验。");
  }

  return {
    ready: issues.length === 0,
    reason: issues[0]?.message || "",
    issues,
    artifactId,
    artifactVersion,
    attestationSha256,
    candidates,
    executionCapability: "none",
    userFinalDecisionRequired: true,
  };
}


function validClientRequestId(value) {
  const normalized = text(value);
  return /^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/.test(normalized) ? normalized : "";
}


export function candidateExperimentErrorMessage(error, fallback = "联合实验失败，未展示任何指标。") {
  return text(error?.message, 1000) || text(fallback, 1000) || "联合实验失败，未展示任何指标。";
}


export function createCandidateExperimentClientRequestId() {
  if (typeof globalThis.crypto?.randomUUID !== "function") {
    throw new Error("当前环境不能生成安全的实验请求编号。");
  }
  return `candidate_experiment_${globalThis.crypto.randomUUID()}`;
}


export function candidateExperimentRequestIdentity(
  current,
  fingerprint,
  createId = createCandidateExperimentClientRequestId,
) {
  const semanticFingerprint = text(fingerprint);
  if (!semanticFingerprint) return { fingerprint: "", clientRequestId: "" };
  const currentFingerprint = text(current?.fingerprint);
  const currentRequestId = validClientRequestId(current?.clientRequestId);
  if (currentFingerprint === semanticFingerprint && currentRequestId) {
    return { fingerprint: semanticFingerprint, clientRequestId: currentRequestId };
  }
  const nextRequestId = validClientRequestId(createId());
  if (!nextRequestId) throw new Error("生成的实验 client request id 格式无效。");
  return { fingerprint: semanticFingerprint, clientRequestId: nextRequestId };
}


function exactSelection(candidate) {
  return {
    candidate_id: candidate.id,
    expected_candidate_revision: candidate.revision,
    expected_candidate_origin_message_id: candidate.originMessageId,
    expected_candidate_latest_message_id: candidate.latestMessageId,
    expected_candidate_snapshot_sha256: candidate.snapshotSha256,
  };
}


export function buildCandidateExperimentRequest(
  artifact,
  { candidateIds, clientRequestId } = {},
) {
  const gate = candidateExperimentAuthorizationGate(artifact);
  if (!gate.ready) throw new Error(gate.reason || "当前产物不能授权历史联合实验。");
  const selectedIds = Array.isArray(candidateIds)
    ? candidateIds.map((candidateId) => text(candidateId, 240)).filter(Boolean)
    : [];
  if (
    selectedIds.length < 2
    || selectedIds.length > 6
    || new Set(selectedIds).size !== selectedIds.length
  ) {
    throw new TypeError("联合实验必须选择 2–6 个不同的当前治理候选。");
  }
  const byId = new Map(gate.candidates.map((candidate) => [candidate.id, candidate]));
  const selected = selectedIds.map((candidateId) => byId.get(candidateId));
  if (selected.some((candidate) => !candidate)) {
    throw new Error("所选候选不属于当前已确认产物的精确治理集合。");
  }
  const requestId = validClientRequestId(clientRequestId);
  if (!requestId) throw new TypeError("实验 client request id 格式无效。");
  return {
    version: CANDIDATE_EXPERIMENT_REQUEST_VERSION,
    client_request_id: requestId,
    artifact_id: gate.artifactId,
    expected_artifact_version: gate.artifactVersion,
    expected_governance_attestation_sha256: gate.attestationSha256,
    candidate_selections: selected.map(exactSelection),
    user_authorized_historical_comparison: true,
  };
}


export function candidateExperimentSelectionFingerprint(artifact, candidateIds) {
  const gate = candidateExperimentAuthorizationGate(artifact);
  if (!gate.ready) return "";
  const selectedIds = Array.isArray(candidateIds)
    ? candidateIds.map((candidateId) => text(candidateId, 240)).filter(Boolean)
    : [];
  if (
    selectedIds.length < 2
    || selectedIds.length > 6
    || new Set(selectedIds).size !== selectedIds.length
  ) return "";
  const byId = new Map(gate.candidates.map((candidate) => [candidate.id, candidate]));
  const selected = selectedIds.map((candidateId) => byId.get(candidateId));
  if (selected.some((candidate) => !candidate)) return "";
  return JSON.stringify({
    artifact_id: gate.artifactId,
    artifact_version: gate.artifactVersion,
    attestation_sha256: gate.attestationSha256,
    candidates: selected.map((candidate) => [
      candidate.id,
      candidate.revision,
      candidate.originMessageId,
      candidate.latestMessageId,
      candidate.snapshotSha256,
    ]),
  });
}


/**
 * Project display-only control state from exact candidate semantics.
 * This helper never grants authorization or creates a request.
 */
export function candidateExperimentControlState({
  gateReady = false,
  readOnly = false,
  loading = false,
  acknowledged = false,
  selectedCandidateIds = [],
  selectionFingerprint = "",
  resultReady = false,
} = {}) {
  const selectedIds = Array.isArray(selectedCandidateIds)
    ? selectedCandidateIds.map((candidateId) => text(candidateId, 240)).filter(Boolean)
    : [];
  const selectedCount = selectedIds.length;
  const uniqueSelection = new Set(selectedIds).size === selectedCount;
  const selectionReady = selectedCount >= 2
    && selectedCount <= 6
    && uniqueSelection
    && Boolean(text(selectionFingerprint));
  const controlsAvailable = gateReady === true && readOnly !== true;
  const canAcknowledge = controlsAvailable && loading !== true && selectionReady;
  const canRun = canAcknowledge && acknowledged === true;

  let phase = "select";
  let instruction = "还需选择 " + Math.max(0, 2 - selectedCount) + " 个候选，最多可选 6 个。";
  let actionLabel = "运行所选 " + selectedCount + " 个候选";
  if (readOnly) {
    phase = "read_only";
    instruction = "当前冻结合同仅允许查看，不能创建新的历史实验。";
    actionLabel = "只读，不能运行实验";
  } else if (gateReady !== true) {
    phase = "blocked";
    instruction = "候选治理或精确版本绑定未就绪，实验控制已关闭。";
    actionLabel = "治理授权不可用";
  } else if (loading) {
    phase = "running";
    instruction = "正在以冻结数据和共同规格复算；当前选择已锁定。";
    actionLabel = "正在原子复算并重读完整性…";
  } else if (selectedCount > 6 || !uniqueSelection || (selectedCount >= 2 && !selectionReady)) {
    phase = "select";
    instruction = "当前选择无法形成 2–6 个唯一候选的精确语义封印。";
    actionLabel = "当前选择无法精确绑定";
  } else if (!selectionReady) {
    phase = "select";
  } else if (acknowledged !== true) {
    phase = "authorize";
    instruction = "选择已精确绑定；确认一次性历史比较授权后才能运行。";
    actionLabel = "确认只读授权后运行";
  } else if (resultReady) {
    phase = "review";
    instruction = "完整性已通过；请复核共同规格、三档摩擦与反证，再由用户独立决定。";
    actionLabel = "按同一请求编号重新核验 " + selectedCount + " 个候选";
  } else {
    phase = "ready";
    instruction = "一次性历史比较授权已确认；可以运行受约束实验。";
  }

  const workflowAvailable = gateReady === true && readOnly !== true;
  const authorizationComplete = selectionReady && acknowledged === true;
  const stepDefinitions = [
    { key: "select", order: "01", label: "选择候选" },
    { key: "authorize", order: "02", label: "确认授权" },
    { key: "run", order: "03", label: "原子复算" },
    { key: "review", order: "04", label: "独立复核" },
  ];
  const steps = stepDefinitions.map((step) => {
    let status = "pending";
    if (!workflowAvailable) status = "locked";
    else if (step.key === "select") status = selectionReady ? "complete" : "active";
    else if (step.key === "authorize") {
      status = authorizationComplete ? "complete" : selectionReady ? "active" : "pending";
    } else if (step.key === "run") {
      status = resultReady ? "complete" : loading || canRun ? "active" : "pending";
    } else if (step.key === "review") {
      status = resultReady ? "active" : "pending";
    }
    return { ...step, status };
  });

  return {
    phase,
    selectedCount,
    selectionReady,
    canAcknowledge,
    canRun,
    instruction,
    actionLabel,
    steps,
  };
}


function evidenceItem(value) {
  if (typeof value === "string") {
    const label = text(value);
    return label ? { id: "", label, detail: "" } : null;
  }
  if (!isRecord(value)) return null;
  const id = text(value.id || value.evidence_id || value.source_id);
  const label = text(
    value.label
    || value.title
    || value.summary
    || value.text
    || value.name
    || id,
  );
  if (!label) return null;
  return {
    id,
    label,
    detail: text(value.detail || value.review_note || value.status || value.source_type),
  };
}


function evidenceItems(value) {
  const rows = Array.isArray(value) ? value : [];
  if (rows.length > CANDIDATE_EXPERIMENT_EVIDENCE_LIMIT) {
    return { items: [], integrityReady: false };
  }
  return {
    items: rows.map(evidenceItem).filter(Boolean),
    integrityReady: true,
  };
}


function blockerView(value) {
  const blocker = isRecord(value) ? value : {};
  return {
    message: text(blocker.message, 1000),
    reason: text(blocker.reason, 1000),
    detail: text(blocker.detail, 1000),
    code: text(blocker.code, 120),
    symbol: text(blocker.symbol, 40),
    date: text(blocker.date, 40),
  };
}


function scenarioView(value) {
  const scenario = isRecord(value) ? value : {};
  const metrics = isRecord(scenario.metrics) ? scenario.metrics : {};
  const blocked = scenario.blocked === true;
  const rawMetrics = {
    cumulativeReturnPct: strictNumber(metrics.portfolio_cumulative_return_pct),
    historicalPositiveWindowRatio: strictNumber(metrics.historical_positive_window_ratio),
    maxDrawdownPct: strictNumber(metrics.max_drawdown_pct),
    meanWindowReturnPct: strictNumber(metrics.mean_window_return_pct),
    worstWindowReturnPct: strictNumber(metrics.worst_window_return_pct),
  };
  const rawValues = Object.values(rawMetrics);
  const metricsVisible = !blocked && scenario.metrics_visible === true;
  const integrityReady = Boolean(text(scenario.scenario_id) && text(scenario.state))
    && (blocked
      ? scenario.metrics_visible === false && rawValues.every((metric) => metric === null)
      : metricsVisible
        && rawValues.every((metric) => metric !== null)
        && rawMetrics.historicalPositiveWindowRatio >= 0
        && rawMetrics.historicalPositiveWindowRatio <= 1);
  return {
    id: text(scenario.scenario_id),
    state: text(scenario.state),
    blocked,
    metricsVisible,
    integrityReady,
    cumulativeReturnPct: metricsVisible ? rawMetrics.cumulativeReturnPct : null,
    historicalPositiveWindowRatio: metricsVisible
      ? rawMetrics.historicalPositiveWindowRatio
      : null,
    maxDrawdownPct: metricsVisible ? rawMetrics.maxDrawdownPct : null,
    meanWindowReturnPct: metricsVisible ? rawMetrics.meanWindowReturnPct : null,
    worstWindowReturnPct: metricsVisible ? rawMetrics.worstWindowReturnPct : null,
    capacityGapUsd: blocked ? strictNumber(scenario.capacity_gap_usd) : null,
    firstBlocker: blocked && isRecord(scenario.first_blocker)
      ? blockerView(scenario.first_blocker)
      : null,
  };
}


function sharedHash(arm, primary, alternative) {
  return sha256(arm?.[primary] || arm?.[alternative]);
}


function armView(value, specSha256, datasetSealSha256) {
  const arm = isRecord(value) ? value : {};
  const rawScenarios = Array.isArray(arm.scenarios) ? arm.scenarios : [];
  const scenarios = rawScenarios.length === SCENARIO_IDS.length
    ? rawScenarios.map(scenarioView)
    : [];
  const scenarioShapeReady = scenarios.length === SCENARIO_IDS.length
    && scenarios.every((scenario, index) => (
      scenario.id === SCENARIO_IDS[index] && scenario.integrityReady
    ));
  const allScenariosBlocked = scenarioShapeReady && scenarios.every((scenario) => scenario.blocked);
  const armMetricsFieldReady = arm.metrics_visible === true
    || (arm.metrics_visible === false && allScenariosBlocked);
  const candidateId = text(arm.candidate_id);
  const candidateRevision = positiveInteger(arm.candidate_revision);
  const candidateSnapshotSha256 = sha256(arm.candidate_snapshot_sha256);
  const symbol = text(arm.symbol).toUpperCase();
  const direction = text(arm.direction).toUpperCase();
  const side = text(arm.side).toUpperCase();
  const armSpecSha256 = sharedHash(arm, "shared_spec_sha256", "spec_sha256");
  const armDatasetSealSha256 = sharedHash(
    arm,
    "shared_dataset_seal_sha256",
    "dataset_seal_sha256",
  );
  const evidence = evidenceItems(arm.evidence);
  const counterevidence = evidenceItems(arm.counterevidence);
  const integrityReady = positiveInteger(arm.sequence_no)
    && candidateId
    && candidateRevision
    && candidateSnapshotSha256
    && text(arm.title)
    && STORAGE_SYMBOLS.has(symbol)
    && ["UP", "DOWN"].includes(direction)
    && side === (direction === "UP" ? "LONG" : "SHORT")
    && text(arm.thesis)
    && text(arm.invalidation)
    && sha256(arm.candidate_binding_sha256)
    && armSpecSha256 === specSha256
    && armDatasetSealSha256 === datasetSealSha256
    && arm.integrity_ok === true
    && fixedSafetyReady(arm)
    && evidence.integrityReady
    && counterevidence.integrityReady
    && armMetricsFieldReady
    && scenarioShapeReady;
  return {
    sequenceNo: positiveInteger(arm.sequence_no),
    candidateId,
    candidateRevision,
    candidateSnapshotSha256,
    title: text(arm.title) || candidateId || "未命名候选",
    symbol,
    direction,
    side,
    thesis: text(arm.thesis),
    invalidation: text(arm.invalidation),
    evidence: evidence.items,
    counterevidence: counterevidence.items,
    candidateBindingSha256: sha256(arm.candidate_binding_sha256),
    specSha256: armSpecSha256,
    datasetSealSha256: armDatasetSealSha256,
    integrityReady: Boolean(integrityReady),
    metricsVisible: Boolean(integrityReady) && arm.metrics_visible === true,
    scenarios: scenarioShapeReady ? scenarios : [],
  };
}


function authorizationSelection(value) {
  const selection = isRecord(value) ? value : {};
  return {
    candidateId: text(selection.candidate_id),
    candidateRevision: positiveInteger(
      selection.expected_candidate_revision ?? selection.candidate_revision,
    ),
    originMessageId: text(
      selection.expected_candidate_origin_message_id || selection.candidate_origin_message_id,
    ),
    latestMessageId: text(
      selection.expected_candidate_latest_message_id || selection.candidate_latest_message_id,
    ),
    snapshotSha256: sha256(
      selection.expected_candidate_snapshot_sha256 || selection.candidate_snapshot_sha256,
    ),
  };
}


function normalizeExpectedSelection(value) {
  const selection = isRecord(value) ? value : {};
  return {
    candidateId: text(selection.candidate_id),
    candidateRevision: positiveInteger(selection.expected_candidate_revision),
    originMessageId: text(selection.expected_candidate_origin_message_id),
    latestMessageId: text(selection.expected_candidate_latest_message_id),
    snapshotSha256: sha256(selection.expected_candidate_snapshot_sha256),
  };
}


function sameSelection(left, right) {
  return left.candidateId === right.candidateId
    && left.candidateRevision === right.candidateRevision
    && left.originMessageId === right.originMessageId
    && left.latestMessageId === right.latestMessageId
    && left.snapshotSha256 === right.snapshotSha256;
}


function responseIssues(experiment) {
  const issues = Array.isArray(experiment?.issues) ? experiment.issues : [];
  const integrityIssues = Array.isArray(experiment?.integrity_issues)
    ? experiment.integrity_issues
    : [];
  if (issues.length + integrityIssues.length > CANDIDATE_EXPERIMENT_ISSUE_LIMIT) {
    return [{
      code: "CANDIDATE_EXPERIMENT_ISSUE_LIMIT_EXCEEDED",
      message: `实验问题记录超过 ${CANDIDATE_EXPERIMENT_ISSUE_LIMIT} 条安全上限。`,
    }];
  }
  const projected = [...issues, ...integrityIssues].flatMap((issue) => {
    if (typeof issue === "string" && text(issue)) {
      return [{ code: "CANDIDATE_EXPERIMENT_BLOCKED", message: text(issue, 1000) }];
    }
    if (!isRecord(issue)) return [];
    const code = text(issue.code, 120) || "CANDIDATE_EXPERIMENT_BLOCKED";
    const message = text(issue.message, 1000) || "联合实验完整性未通过。";
    return [{ code, message }];
  });
  const seen = new Set();
  return projected.filter((issue) => {
    const key = `${issue.code}:${issue.message}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}


/** Normalize a server-read cohort. Any integrity drift hides every arm metric. */
export function candidateExperimentView(value, expected = {}) {
  const experiment = isRecord(value) ? value : {};
  const issues = responseIssues(experiment);
  const commonSpec = isRecord(experiment.common_spec) ? experiment.common_spec : {};
  const datasetSeal = isRecord(experiment.dataset_seal) ? experiment.dataset_seal : {};
  const specSha256 = sha256(experiment.spec_sha256);
  const datasetSealSha256 = sha256(experiment.dataset_seal_sha256);
  const inputSealSha256 = sha256(experiment.input_seal_sha256);
  const aggregateSha256 = sha256(experiment.aggregate_sha256);
  const requestSemanticsSha256 = sha256(experiment.request_semantics_sha256);
  const authorization = isRecord(experiment.authorization) ? experiment.authorization : {};
  const rawAuthorizationSelections = Array.isArray(authorization.candidate_selections)
    ? authorization.candidate_selections
    : [];
  const authorizationSelectionShapeReady = rawAuthorizationSelections.length >= 2
    && rawAuthorizationSelections.length <= CANDIDATE_EXPERIMENT_ARM_LIMIT;
  const authorizationSelections = authorizationSelectionShapeReady
    ? rawAuthorizationSelections.map(authorizationSelection)
    : [];
  const rawArms = Array.isArray(experiment.arms) ? experiment.arms : [];
  const armCollectionShapeReady = rawArms.length >= 2
    && rawArms.length <= CANDIDATE_EXPERIMENT_ARM_LIMIT;
  const arms = armCollectionShapeReady
    ? rawArms.map((arm) => armView(arm, specSha256, datasetSealSha256))
    : [];
  const rawExpectedSelections = Array.isArray(expected.candidateSelections)
    ? expected.candidateSelections
    : [];
  const expectedSelectionShapeReady = rawExpectedSelections.length <= CANDIDATE_EXPERIMENT_ARM_LIMIT;
  const expectedSelections = expectedSelectionShapeReady
    ? rawExpectedSelections.map(normalizeExpectedSelection)
    : [];
  const roomId = text(experiment.room_id);
  const artifactId = text(experiment.artifact_id);
  const artifactVersion = positiveInteger(experiment.artifact_version);
  const clientRequestId = validClientRequestId(experiment.client_request_id);
  const attestationSha256 = sha256(
    authorization.expected_governance_attestation_sha256
    || authorization.governance_attestation_sha256,
  );
  const authorizationArtifactVersion = positiveInteger(
    authorization.expected_artifact_version ?? authorization.artifact_version,
  );
  const authorizationReady = authorization.version === CANDIDATE_EXPERIMENT_AUTHORIZATION_VERSION
    && authorization.user_authorized_historical_comparison === true
    && text(authorization.artifact_id) === artifactId
    && authorizationArtifactVersion === artifactVersion
    && validClientRequestId(authorization.client_request_id) === clientRequestId
    && attestationSha256
    && authorization.does_not_imply_artifact_support === true
    && authorization.does_not_create_artifact_user_decision === true
    && !FORBIDDEN_AUTHORIZATION_KEYS.some((key) => Object.hasOwn(authorization, key))
    && authorizationSelections.length >= 2
    && authorizationSelections.length <= CANDIDATE_EXPERIMENT_ARM_LIMIT
    && authorizationSelectionShapeReady
    && authorizationSelections.every((selection) => (
      selection.candidateId
      && selection.candidateRevision
      && selection.originMessageId
      && selection.latestMessageId
      && selection.snapshotSha256
    ));
  const armOrderReady = arms.length === authorizationSelections.length
    && arms.length >= 2
    && arms.length <= CANDIDATE_EXPERIMENT_ARM_LIMIT
    && armCollectionShapeReady
    && new Set(arms.map((arm) => arm.candidateId)).size === arms.length
    && arms.every((arm, index) => (
      arm.sequenceNo === index + 1
      && arm.candidateId === authorizationSelections[index]?.candidateId
      && arm.candidateRevision === authorizationSelections[index]?.candidateRevision
      && arm.candidateSnapshotSha256 === authorizationSelections[index]?.snapshotSha256
      && arm.integrityReady
    ));
  const expectedContextReady = expectedSelectionShapeReady
    && (!expected.roomId || text(expected.roomId) === roomId)
    && (!expected.artifactId || text(expected.artifactId) === artifactId)
    && (!expected.artifactVersion || positiveInteger(expected.artifactVersion) === artifactVersion)
    && (!expected.clientRequestId
      || validClientRequestId(expected.clientRequestId) === clientRequestId)
    && (!expected.attestationSha256
      || sha256(expected.attestationSha256) === attestationSha256)
    && (!expectedSelections.length || (
      expectedSelections.length === authorizationSelections.length
      && expectedSelections.every((selection, index) => (
        sameSelection(selection, authorizationSelections[index])
      ))
    ));
  const safetyReady = fixedSafetyReady(experiment)
    && experiment.market_data_reads === 1
    && experiment.provider_calls_total === 0
    && experiment.openai_calls === 0
    && experiment.historical_only === true
    && experiment.out_of_sample_claim === false
    && experiment.future_performance_claim === false;
  const commonSpecFieldCount = Object.keys(commonSpec).length;
  const datasetSealFieldCount = Object.keys(datasetSeal).length;
  const envelopeReady = experiment.version === CANDIDATE_EXPERIMENT_COHORT_VERSION
    && ["ready", "completed"].includes(text(experiment.status).toLowerCase())
    && experiment.integrity_ok === true
    && experiment.metrics_visible === true
    && text(experiment.id)
    && roomId
    && artifactId
    && artifactVersion
    && clientRequestId
    && commonSpecFieldCount > 0
    && commonSpecFieldCount <= CANDIDATE_EXPERIMENT_RECORD_FIELD_LIMIT
    && datasetSealFieldCount > 0
    && datasetSealFieldCount <= CANDIDATE_EXPERIMENT_RECORD_FIELD_LIMIT
    && specSha256
    && datasetSealSha256
    && inputSealSha256
    && aggregateSha256
    && requestSemanticsSha256;
  const ready = Boolean(
    envelopeReady
    && authorizationReady
    && armOrderReady
    && expectedContextReady
    && safetyReady
    && issues.length === 0,
  );
  const blockedIssues = ready ? [] : (issues.length ? issues : [{
    code: "CANDIDATE_EXPERIMENT_RESPONSE_INVALID",
    message: "联合实验响应未通过完整性、共同指纹或无执行能力校验。",
  }]);
  return {
    ready,
    status: ready ? "ready" : "blocked",
    metricsVisible: ready,
    issues: blockedIssues,
    cohortId: text(experiment.id),
    roomId,
    artifactId,
    artifactVersion,
    clientRequestId,
    authorization: ready ? authorization : {},
    commonSpec: ready ? commonSpec : {},
    datasetSeal: ready ? datasetSeal : {},
    specSha256: ready ? specSha256 : "",
    datasetSealSha256: ready ? datasetSealSha256 : "",
    inputSealSha256: ready ? inputSealSha256 : "",
    aggregateSha256: ready ? aggregateSha256 : "",
    requestSemanticsSha256: ready ? requestSemanticsSha256 : "",
    arms: ready
      ? arms
      : arms.map((arm) => ({
          ...arm,
          evidence: [],
          counterevidence: [],
          thesis: "",
          invalidation: "",
          metricsVisible: false,
          scenarios: [],
        })),
    historicalOnly: true,
    executionCapability: "none",
    liveTradingAllowed: false,
    canAutonomouslyDecide: false,
    rankingProduced: false,
    winnerClaim: false,
    userFinalDecisionRequired: true,
  };
}


export const CANDIDATE_EXPERIMENT_SCENARIO_IDS = SCENARIO_IDS;
