export const STORAGE_SAMPLE_ACCEPTANCE_VERSION = "storage_sample_acceptance_v3";

const LEGACY_STORAGE_SAMPLE_ACCEPTANCE_VERSIONS = new Set([
  "storage_sample_acceptance_v1",
  "storage_sample_acceptance_v2",
]);

export const STORAGE_SAMPLE_STAGE_ORDER = [
  {
    id: "market_snapshot",
    label: "Futu 行情快照",
    aliases: ["market_snapshot_gate", "market_snapshot", "futu_market_snapshot"],
    currentFields: ["current", "ready_symbol_count", "received", "symbol_count"],
    requiredFields: ["required", "required_symbol_count", "requested"],
    defaultCurrent: 0,
    defaultRequired: 4,
  },
  {
    id: "research_evidence",
    label: "官方研究证据",
    aliases: ["research_evidence_gate", "research_evidence", "official_research_evidence"],
    currentFields: ["current", "ready"],
    requiredFields: ["required"],
    defaultCurrent: 0,
    defaultRequired: 1,
  },
  {
    id: "discussion",
    label: "12角色讨论",
    aliases: ["discussion", "roles", "turns", "twelve_role_discussion"],
    currentFields: ["current", "qualified_member_count", "successful_member_count", "completed"],
    requiredFields: ["required", "required_success_count", "minimum_successful_members"],
    defaultCurrent: 0,
    defaultRequired: 12,
  },
  {
    id: "artifact",
    label: "唯一纪要",
    aliases: ["artifact", "minutes", "meeting_minutes", "unique_artifact"],
    currentFields: ["current", "artifact_count", "minutes_count", "qualified_artifact_count"],
    requiredFields: ["required", "required_artifact_count"],
    defaultCurrent: 0,
    defaultRequired: 1,
  },
  {
    id: "evidence",
    label: "证据复核",
    aliases: ["evidence", "evidence_review", "review"],
    currentFields: ["current", "reviewed_count", "verified_count"],
    requiredFields: ["required", "evidence_count", "total_count"],
  },
  {
    id: "user_decision",
    label: "用户决定",
    aliases: ["user_decision", "decision", "final_decision"],
    currentFields: ["current", "decision_count"],
    requiredFields: ["required", "required_decision_count"],
    defaultCurrent: 0,
    defaultRequired: 1,
  },
  {
    id: "paper_portfolio",
    label: "纸面组合",
    aliases: ["paper_portfolio", "portfolio", "simulation_portfolio"],
    currentFields: ["current", "confirmed_portfolio_count", "qualified_portfolio_count"],
    requiredFields: ["required", "required_portfolio_count"],
    defaultCurrent: 0,
    defaultRequired: 1,
  },
  {
    id: "simulation",
    label: "模拟观察",
    aliases: ["simulation", "simulation_observations", "observations", "statistics"],
    currentFields: ["current", "sample_count", "resolved_sample_count"],
    requiredFields: ["required", "minimum_samples"],
    defaultCurrent: 0,
    defaultRequired: 20,
  },
];

const LEGACY_MARKET_DATA_DEFINITION = {
  aliases: ["market_data", "market", "data", "futu"],
};

const ACCEPTANCE_STATE_LABELS = {
  no_round: "等待新轮次",
  legacy: "旧版记录",
  blocked: "验收受阻",
  review_required: "等待用户复核",
  deferred: "用户暂缓",
  returned: "用户退回",
  accepted: "当前样板已验收",
};

const STAGE_STATE_LABELS = {
  passed: "已通过",
  blocked: "受阻",
  review: "待复核",
  deferred: "已暂缓",
  returned: "已退回",
  pending: "未完成",
  legacy: "不计入",
};

const PASSED_STATES = new Set(["accepted", "complete", "completed", "passed", "ready", "resolved"]);
const BLOCKED_STATES = new Set(["blocked", "error", "failed", "invalid", "rejected"]);
const REVIEW_STATES = new Set(["review", "review_required", "unreviewed", "awaiting_review", "awaiting_user"]);
const DEFERRED_STATES = new Set(["deferred", "hold", "held"]);
const RETURNED_STATES = new Set(["returned", "return"]);

function asObject(value) {
  if (value && typeof value === "object" && !Array.isArray(value)) return value;
  if (typeof value === "boolean") return { ready: value };
  return {};
}

function cleanString(value) {
  return typeof value === "string" ? value.trim() : "";
}

function nonNegativeNumber(...values) {
  for (const value of values) {
    if (value === null || value === undefined || value === "") continue;
    const number = Number(value);
    if (Number.isFinite(number) && number >= 0) return number;
  }
  return null;
}

function firstNumber(source, fields, fallback = null) {
  return nonNegativeNumber(...fields.map((field) => source?.[field]), fallback);
}

function sourceFromStages(stages, definition) {
  if (Array.isArray(stages)) {
    const match = stages.find((stage) => definition.aliases.includes(
      cleanString(stage?.id || stage?.key || stage?.name).toLowerCase(),
    ));
    return asObject(match);
  }
  if (stages && typeof stages === "object") {
    for (const alias of definition.aliases) {
      if (Object.prototype.hasOwnProperty.call(stages, alias)) return asObject(stages[alias]);
    }
  }
  return {};
}

function flatStageSource(raw, definition) {
  for (const alias of definition.aliases) {
    if (Object.prototype.hasOwnProperty.call(raw, alias)) return asObject(raw[alias]);
  }
  return {};
}

function normalizedStageState(source, globalState) {
  if (globalState === "legacy") return "legacy";
  const state = cleanString(source.state || source.status).toLowerCase();
  if (source.ready === true || PASSED_STATES.has(state)) return "passed";
  if (BLOCKED_STATES.has(state)) return "blocked";
  if (REVIEW_STATES.has(state)) return "review";
  if (DEFERRED_STATES.has(state)) return "deferred";
  if (RETURNED_STATES.has(state)) return "returned";
  return "pending";
}

function stageDetail(definition, source, current, required, state) {
  const explicit = cleanString(source.detail || source.message);
  if (explicit) return explicit;
  if (state === "legacy") return "该阶段来自旧版轮次，仅保留审计记录。";
  if (definition.id === "market_snapshot") return `${current ?? 0} / ${required ?? 4} 只目标股票具有合格的同轮 Futu 只读快照。`;
  if (definition.id === "research_evidence") {
    return state === "passed"
      ? "当前轮次所需的官方研究证据已通过独立门禁。"
      : "当前轮次所需的官方研究证据尚未通过。";
  }
  if (definition.id === "discussion") return `${current ?? 0} / ${required ?? 12} 位独立职责完成合格发言。`;
  if (definition.id === "artifact") return current >= required && required > 0 ? "已形成绑定唯一轮次的纪要。" : "尚无绑定当前合格轮次的唯一纪要。";
  if (definition.id === "evidence") return required === null ? "纪要证据尚待逐条复核。" : `${current ?? 0} / ${required} 条证据关系已复核。`;
  if (definition.id === "user_decision") return current >= required && required > 0 ? "用户决定已绑定精确纪要版本。" : "等待用户对精确纪要版本作出决定。";
  if (definition.id === "paper_portfolio") {
    if (state === "deferred") return "用户已暂缓研究方案，不创建或确认纸面组合。";
    if (state === "returned") return "用户已退回研究方案，不创建或确认纸面组合。";
    return current >= required && required > 0
      ? "已确认精确、风险合格且无执行能力的纸面组合。"
      : "尚未确认与当前支持决定精确绑定的纸面组合。";
  }
  return `${current ?? 0} / ${required ?? 20} 个独立到期样本进入统计验证。`;
}

function normalizedIssue(value, index, prefix) {
  if (typeof value === "string") {
    return { code: `${prefix}_${index + 1}`, title: value.trim(), detail: "" };
  }
  const issue = asObject(value);
  return {
    code: cleanString(issue.code) || `${prefix}_${index + 1}`,
    title: cleanString(issue.title || issue.label || issue.message) || "未说明的验收问题",
    detail: cleanString(issue.detail || issue.reason),
  };
}

function normalizedActions(value) {
  if (!Array.isArray(value)) return [];
  return value.map((item) => (
    typeof item === "string"
      ? item.trim()
      : cleanString(item?.text || item?.title || item?.detail || item?.action)
  )).filter(Boolean);
}

function legacyRoundIds(raw, globalState) {
  const candidates = [
    ...(Array.isArray(raw.legacy_rounds) ? raw.legacy_rounds : []),
    ...(Array.isArray(raw.history) ? raw.history : []),
  ];
  const ids = candidates.filter((item) => {
    if (typeof item === "string") return true;
    const state = cleanString(item?.state || item?.status).toLowerCase();
    return item?.legacy === true || item?.is_legacy === true || state === "legacy";
  }).map((item) => (
    typeof item === "string" ? item : cleanString(item?.round_id || item?.id)
  )).filter(Boolean);
  if (globalState === "legacy" && cleanString(raw.latest_round_id)) ids.unshift(cleanString(raw.latest_round_id));
  return [...new Set(ids)];
}

function acceptancePayload(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value.storage_sample_acceptance_v3
    || value.storage_sample_acceptance_v2
    || value.storage_sample_acceptance_v1
    || value.storage_sample_acceptance
    || value.acceptance
    || value;
}

export function parseStorageSampleAcceptance(value) {
  const raw = acceptancePayload(value);
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return {
      version: STORAGE_SAMPLE_ACCEPTANCE_VERSION,
      applicable: false,
      state: "no_round",
      stateLabel: ACCEPTANCE_STATE_LABELS.no_round,
      latestRoundId: "",
      meetingReviewed: false,
      researchSampleReady: false,
      userDecisionAction: "",
      decisionStateNotice: "",
      statisticalValidationReady: false,
      stages: [],
      blockers: [],
      nextActions: [],
      statistics: { sampleCount: 0, minimumSamples: 20, qualified: false },
      statisticsLabel: "统计胜率：样本 0 / 20，样本不足",
      safetyReady: false,
      executionCapability: "",
      liveTradingAllowed: null,
      legacyNotice: "",
      legacyRoundIds: [],
    };
  }

  const declaredVersion = cleanString(raw.version);
  const version = declaredVersion || STORAGE_SAMPLE_ACCEPTANCE_VERSION;
  const latestRoundId = cleanString(raw.latest_round_id || raw.round_id);
  const declaredState = cleanString(raw.state || raw.status).toLowerCase();
  const legacySchema = LEGACY_STORAGE_SAMPLE_ACCEPTANCE_VERSIONS.has(version);
  const explicitlyLegacy = raw.legacy === true
    || raw.is_legacy === true
    || declaredState === "legacy"
    || (legacySchema && Boolean(latestRoundId));
  const missingSchemaVersion = !declaredVersion
    && Boolean(latestRoundId || (declaredState && declaredState !== "no_round"));
  const schemaMismatch = missingSchemaVersion || (
    Boolean(declaredVersion)
    && version !== STORAGE_SAMPLE_ACCEPTANCE_VERSION
    && !legacySchema
  );
  const executionCapability = cleanString(raw.execution_capability).toLowerCase();
  const liveTradingAllowed = typeof raw.live_trading_allowed === "boolean" ? raw.live_trading_allowed : null;
  const safetyReady = executionCapability === "none" && liveTradingAllowed === false;

  let state = ACCEPTANCE_STATE_LABELS[declaredState] ? declaredState : "no_round";
  if (explicitlyLegacy) state = "legacy";

  const blockers = (Array.isArray(raw.blockers) ? raw.blockers : [])
    .map((item, index) => normalizedIssue(item, index, "ACCEPTANCE_BLOCKER"));
  if (schemaMismatch && state !== "legacy") {
    blockers.unshift({
      code: "STORAGE_ACCEPTANCE_SCHEMA_UNSUPPORTED",
      title: "验收对象版本不受支持",
      detail: missingSchemaVersion
        ? `该记录没有声明验收版本；当前只接受 ${STORAGE_SAMPLE_ACCEPTANCE_VERSION}。`
        : `当前只接受 ${STORAGE_SAMPLE_ACCEPTANCE_VERSION}。`,
    });
    state = "blocked";
  }
  if (legacySchema && latestRoundId) {
    blockers.unshift({
      code: "STORAGE_ACCEPTANCE_LEGACY_SUPERSEDED",
      title: "旧版验收记录不再计入",
      detail: `${version} 未证明 artifact_user_decision_v2 的显式用户选择；请按 ${STORAGE_SAMPLE_ACCEPTANCE_VERSION} 重新验收。`,
    });
  }
  if (state === "accepted" && !safetyReady) {
    blockers.unshift({
      code: "STORAGE_ACCEPTANCE_SAFETY_BOUNDARY_INVALID",
      title: "只读安全边界未通过",
      detail: "验收对象必须明确 execution_capability=none 且 live_trading_allowed=false。",
    });
    state = "blocked";
  } else if (blockers.length && state === "no_round" && latestRoundId) {
    state = "blocked";
  }

  const statisticsSource = asObject(raw.statistics);
  const legacyMarketSource = {
    ...flatStageSource(raw, LEGACY_MARKET_DATA_DEFINITION),
    ...sourceFromStages(raw.stages, LEGACY_MARKET_DATA_DEFINITION),
  };
  const hasLegacyMarketSource = Object.keys(legacyMarketSource).length > 0;
  const legacyMarketState = cleanString(
    legacyMarketSource.state || legacyMarketSource.status,
  ).toLowerCase();
  const legacyMarketReady = legacyMarketSource.ready === true
    || PASSED_STATES.has(legacyMarketState);
  const stageSources = new Map();
  for (const definition of STORAGE_SAMPLE_STAGE_ORDER) {
    const explicitSource = {
      ...flatStageSource(raw, definition),
      ...sourceFromStages(raw.stages, definition),
    };
    let source = explicitSource;
    if (Object.keys(explicitSource).length === 0 && hasLegacyMarketSource) {
      if (definition.id === "market_snapshot") {
        source = {
          ...legacyMarketSource,
          label: "",
          state: legacyMarketReady ? "ready" : "blocked",
          ready: legacyMarketReady,
          detail: legacyMarketReady
            ? "旧版 v2 仅保存行情与官方研究证据的合并通过状态；按兼容规则显示 Futu 行情快照已通过。"
            : "旧版 v2 合并状态未通过，无法单独确认 Futu 行情快照。",
        };
      } else if (definition.id === "research_evidence") {
        source = {
          label: "",
          state: legacyMarketReady ? "ready" : "blocked",
          ready: legacyMarketReady,
          current: legacyMarketReady ? 1 : 0,
          required: 1,
          detail: legacyMarketReady
            ? "旧版 v2 仅保存行情与官方研究证据的合并通过状态；按兼容规则显示官方证据已通过。"
            : "旧版 v2 合并状态未通过，无法单独确认官方研究证据。",
        };
      }
    }
    if (definition.id === "simulation") {
      source = { ...statisticsSource, ...source };
    }
    stageSources.set(definition.id, source);
  }

  const stages = STORAGE_SAMPLE_STAGE_ORDER.map((definition) => {
    const source = stageSources.get(definition.id) || {};
    const current = firstNumber(source, definition.currentFields, definition.defaultCurrent ?? null);
    const required = firstNumber(source, definition.requiredFields, definition.defaultRequired ?? null);
    const stageState = normalizedStageState(source, state);
    return {
      id: definition.id,
      label: cleanString(source.label) || definition.label,
      state: stageState,
      stateLabel: STAGE_STATE_LABELS[stageState],
      ready: stageState === "passed",
      detail: stageDetail(definition, source, current, required, stageState),
      current,
      required,
      metric: current !== null && required !== null ? `${current} / ${required}` : "",
    };
  });

  const simulation = stages.find((stage) => stage.id === "simulation");
  const paperPortfolio = stages.find((stage) => stage.id === "paper_portfolio");
  const sampleCount = nonNegativeNumber(statisticsSource.sample_count, simulation?.current, 0) ?? 0;
  const minimumSamples = Math.max(1, nonNegativeNumber(statisticsSource.minimum_samples, simulation?.required, 20) ?? 20);
  const qualified = state !== "legacy"
    && safetyReady
    && statisticsSource.qualified === true
    && sampleCount >= minimumSamples;
  const statisticalValidationReady = raw.statistical_validation_ready === true && qualified;

  const historyIds = legacyRoundIds(raw, state);
  const legacyNotice = state === "legacy"
    ? legacySchema
      ? `${version} 旧版验收，不计入当前 v3`
      : "旧版记录，不计入当前验收"
    : historyIds.length ? "历史轮为旧版记录，不计入当前验收" : "";

  const userDecisionAction = cleanString(
    raw.user_decision_action
    || asObject(stageSources.get("user_decision")).action,
  ).toLowerCase();
  const meetingStageIds = [
    "market_snapshot",
    "research_evidence",
    "discussion",
    "artifact",
    "evidence",
    "user_decision",
  ];
  const meetingStagesReady = meetingStageIds.every(
    (stageId) => stages.find((stage) => stage.id === stageId)?.ready === true,
  );
  const meetingReviewed = raw.meeting_reviewed === true
    && meetingStagesReady
    && state !== "legacy"
    && safetyReady;
  const portfolioReady = paperPortfolio?.ready === true;
  const researchSampleReady = raw.research_sample_ready === true
    && state === "accepted"
    && userDecisionAction === "support"
    && meetingReviewed
    && portfolioReady
    && safetyReady;

  const acceptedContractInvalid = state === "accepted" && !researchSampleReady;
  const deferredContractInvalid = state === "deferred" && userDecisionAction !== "hold";
  const returnedContractInvalid = state === "returned" && userDecisionAction !== "return";
  if (acceptedContractInvalid || deferredContractInvalid || returnedContractInvalid) {
    blockers.unshift({
      code: "STORAGE_ACCEPTANCE_V2_STATE_INVALID",
      title: "v2 验收状态自相矛盾",
      detail: acceptedContractInvalid
        ? "accepted 必须同时具备已复核会议、support 决定和精确已确认纸面组合。"
        : "暂缓或退回状态必须与对应的用户决定动作一致。",
    });
    state = "blocked";
  }

  let decisionStateNotice = "";
  if (state === "deferred" || userDecisionAction === "hold") {
    decisionStateNotice = "用户已选择暂缓：会议记录有效，但不会创建研究样板或进入模拟。";
  } else if (state === "returned" || userDecisionAction === "return") {
    decisionStateNotice = "用户已退回候选方案：会议记录有效，需修订后重新确认。";
  } else if (userDecisionAction === "support" && !portfolioReady && state !== "legacy") {
    decisionStateNotice = "用户已支持候选方案；仍需确认精确、风险合格的纸面组合。";
  }

  return {
    version,
    applicable: raw.applicable !== false,
    state,
    stateLabel: ACCEPTANCE_STATE_LABELS[state] || ACCEPTANCE_STATE_LABELS.no_round,
    latestRoundId,
    meetingReviewed,
    researchSampleReady,
    userDecisionAction,
    decisionStateNotice,
    statisticalValidationReady,
    stages,
    blockers,
    nextActions: normalizedActions(raw.next_actions),
    statistics: { sampleCount, minimumSamples, qualified },
    statisticsLabel: qualified
      ? `统计验证：样本 ${sampleCount} / ${minimumSamples}，已达到最低门槛`
      : `统计胜率：样本 ${sampleCount} / ${minimumSamples}，样本不足`,
    safetyReady,
    executionCapability,
    liveTradingAllowed,
    legacyNotice,
    legacyRoundIds: historyIds,
  };
}
