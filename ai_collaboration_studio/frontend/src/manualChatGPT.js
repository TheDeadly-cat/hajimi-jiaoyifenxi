export const MANUAL_CHATGPT_MODES = Object.freeze([
  Object.freeze({ id: "quick", label: "快速", panels: 1, reviews: 2 }),
  Object.freeze({ id: "standard", label: "标准", panels: 2, reviews: 3 }),
  Object.freeze({ id: "deep", label: "深度", panels: 3, reviews: 4 }),
]);

export const CHATGPT_CONTINUATION_URL = "https://chatgpt.com/";

const KNOWN_STATES = new Set([
  "DRAFT",
  "BUNDLE_READY",
  "WAITING_FOR_CHATGPT",
  "RESULT_IMPORTED",
  "VALIDATING",
  "API_REVIEW",
  "READY_FOR_DECISION",
  "FROZEN",
  "CONTEXT_STALE",
  "IMPORT_REJECTED",
  "BUDGET_BLOCKED",
  "NEEDS_USER_ACTION",
]);

function text(value, limit = 4000) {
  return typeof value === "string" ? value.trim().slice(0, limit) : "";
}

function object(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function list(value) {
  return Array.isArray(value) ? value : [];
}

function nonnegativeInteger(value) {
  return Number.isInteger(value) && value >= 0 ? value : 0;
}

function decimalText(value) {
  return typeof value === "string" && /^\d+(?:\.\d+)?$/.test(value) ? value : "";
}

function stringList(value, limit = 1000, maximumItems = 100) {
  return list(value)
    .slice(0, maximumItems)
    .map((item) => text(item, limit))
    .filter(Boolean);
}

function normalizeImportedResult(value) {
  const source = object(value);
  const panels = list(source.panels).slice(0, 8).flatMap((rawPanel) => {
    const panel = object(rawPanel);
    const panelId = text(panel.panel_id, 80);
    if (!panelId) return [];
    const roleViews = list(panel.role_views).slice(0, 64).flatMap((rawRole) => {
      const role = object(rawRole);
      const roleId = text(role.role_id, 80);
      if (!roleId) return [];
      return [{
        role_id: roleId,
        assessment: text(role.assessment, 3000),
        evidence_refs: stringList(role.evidence_refs, 80, 100),
        uncertainty: text(role.uncertainty, 1500),
      }];
    });
    return [{
      panel_id: panelId,
      panel_kind: text(panel.panel_kind, 80),
      call_index: nonnegativeInteger(panel.call_index),
      declared_independence: text(panel.declared_independence, 80),
      summary: text(panel.summary, 6000),
      conclusion: text(panel.conclusion, 6000),
      disagreements: stringList(panel.disagreements, 2000, 30),
      risks: stringList(panel.risks, 2000, 30),
      evidence_refs: stringList(panel.evidence_refs, 80, 100),
      role_views: roleViews,
    }];
  });
  const finalSynthesis = object(source.final_synthesis);
  return {
    version: text(source.version, 80),
    panels,
    final_synthesis: {
      summary: text(finalSynthesis.summary, 10_000),
      recommended_option_id: text(finalSynthesis.recommended_option_id, 80),
      open_questions: stringList(finalSynthesis.open_questions, 1000, 30),
      evidence_refs: stringList(finalSynthesis.evidence_refs, 80, 100),
    },
  };
}

export function normalizeManualChatGPT(value) {
  const source = object(value);
  const bundle = object(source.bundle);
  const context = object(bundle.context);
  const budget = object(bundle.budget);
  const planning = object(bundle.planning);
  const contextSize = object(planning.context_size);
  const workload = object(planning.workload);
  const cost = object(planning.estimated_api_cost);
  const integrity = object(source.integrity);
  const apiReview = object(source.api_review);
  const reviewRecovery = object(source.review_recovery);
  const decisionCard = object(source.decision_card);
  const confirmation = object(source.confirmation);
  const state = KNOWN_STATES.has(source.state) ? source.state : "IMPORT_REJECTED";
  const integrityOk = integrity.ok === true;
  const reviewRecoveryReasonCode = text(reviewRecovery.reason_code, 80);
  const reviewRecoveryAcknowledgement = text(reviewRecovery.acknowledgement, 80);
  const reviewRecoveryEligible = Boolean(
    integrityOk
    && state === "API_REVIEW"
    && reviewRecovery.available === true
    && reviewRecovery.eligible === true
    && reviewRecoveryReasonCode === "ORPHANED_ZERO_CALL_REVIEW"
    && reviewRecoveryAcknowledgement,
  );
  return {
    id: text(source.id, 80),
    roomId: text(source.room_id, 80),
    roundId: text(source.round_id, 80),
    mode: MANUAL_CHATGPT_MODES.some((mode) => mode.id === source.mode)
      ? source.mode
      : "standard",
    state,
    objective: text(source.objective),
    bundleSha256: text(source.bundle_sha256, 64),
    contextSha256: text(source.context_sha256, 64),
    taskPrompt: text(source.task_prompt, 500_000),
    resultSha256: text(source.result_sha256, 64),
    declaredModel: text(source.declared_model, 160),
    declaredModelTrusted: false,
    roleCount: list(context.roles).length,
    evidenceCount: list(context.evidence_index).length,
    candidateGroupCount: list(context.candidate_matrix).length,
    chatGPTPanels: Number.isInteger(budget.chatgpt_panel_calls)
      ? budget.chatgpt_panel_calls
      : 0,
    apiReviews: Number.isInteger(budget.independent_api_reviews)
      ? budget.independent_api_reviews
      : 0,
    planningVersion: text(planning.version, 80),
    contextCharacters: nonnegativeInteger(contextSize.characters),
    contextUtf8Bytes: nonnegativeInteger(contextSize.utf8_bytes),
    estimatedContextTokens: nonnegativeInteger(contextSize.estimated_tokens),
    tokenEstimationMethod: text(contextSize.token_estimation_method, 80),
    estimatedApiReviewInputTokens: nonnegativeInteger(
      workload.estimated_api_review_input_tokens,
    ),
    apiReviewOutputTokenBudget: nonnegativeInteger(
      workload.api_review_output_token_budget,
    ),
    costStatus: ["estimated", "unavailable", "invalid_configuration"].includes(cost.status)
      ? cost.status
      : "unavailable",
    estimatedApiCostUsd: decimalText(cost.amount_usd),
    costReasonCode: text(cost.reason_code, 80),
    costRateCardLabel: text(cost.rate_card_label, 120),
    manualChatGPTCostIncluded: false,
    issues: list(source.validation_issues).map((issue) => ({
      path: text(object(issue).path, 240) || "$",
      code: text(object(issue).code, 80) || "INVALID",
      message: text(object(issue).message, 1000) || "导入不符合契约。",
    })),
    repairPrompt: text(source.repair_prompt, 500_000),
    integrityOk,
    result: normalizeImportedResult(source.result),
    apiReviewAvailable: apiReview.available === true,
    apiReviewMigrationRequired: apiReview.migration_required === true,
    apiReviewStatus: text(apiReview.status, 40) || "NOT_STARTED",
    apiReviewProvider: text(apiReview.provider, 80),
    apiReviewModel: text(apiReview.requested_model, 160),
    apiReviewExpectedCalls: nonnegativeInteger(apiReview.expected_calls),
    apiReviewCompletedCalls: nonnegativeInteger(apiReview.completed_calls),
    apiReviewDistinctCalls: apiReview.all_calls_are_distinct === true,
    apiReviewRecords: list(apiReview.reviews).map((review) => {
      const item = object(review);
      return {
        reviewIndex: nonnegativeInteger(item.review_index),
        reviewKind: text(item.review_kind, 80),
        verdict: text(item.verdict, 20),
        summary: text(item.summary, 4000),
        provider: text(item.provider, 80),
        requestedModel: text(item.requested_model, 160),
        responseModel: text(item.response_model, 160),
        independenceClassification: text(item.independence_classification, 80),
        findings: list(item.findings).map((finding) => ({
          severity: text(object(finding).severity, 20),
          claim: text(object(finding).claim, 1000),
          rationale: text(object(finding).rationale, 2000),
        })),
      };
    }),
    reviewRecoveryAvailable: reviewRecovery.available === true,
    reviewRecoveryEligible,
    reviewRecoveryReasonCode,
    reviewRecoveryAcknowledgement,
    reviewRecoveryAgeMs: nonnegativeInteger(reviewRecovery.age_ms),
    reviewRecoveryCount: nonnegativeInteger(reviewRecovery.recovery_count),
    decisionCardSha256: text(source.decision_card_sha256, 64),
    decisionCard: {
      ready: decisionCard.ready_for_user_decision === true,
      summary: text(decisionCard.summary, 8000),
      importedRecommendedOptionId: text(
        decisionCard.imported_recommended_option_id,
        120,
      ),
      options: list(decisionCard.decision_options).map((option) => ({
        optionId: text(object(option).option_id, 120),
        title: text(object(option).title, 1000),
        rationale: text(object(option).rationale, 4000),
        risks: list(object(option).risks).map((risk) => text(risk, 1000)).filter(Boolean),
      })).filter((option) => option.optionId && option.title),
      blockingFindings: list(decisionCard.blocking_findings).map((finding) => ({
        reviewKind: text(object(finding).review_kind, 80),
        claim: text(object(finding).claim, 1000),
        rationale: text(object(finding).rationale, 2000),
      })),
      nonblockingFindings: list(decisionCard.nonblocking_findings).map((finding) => ({
        reviewKind: text(object(finding).review_kind, 80),
        severity: text(object(finding).severity, 20),
        claim: text(object(finding).claim, 1000),
      })),
      openQuestions: list(decisionCard.open_questions)
        .map((question) => text(question, 1000))
        .filter(Boolean),
    },
    confirmedOptionId: text(confirmation.selected_option_id, 120),
    confirmationSha256: text(confirmation.confirmation_sha256, 64),
  };
}

export function manualChatGPTReviewClientRequestId(value) {
  const source = object(value);
  const sessionId = text(source.id, 80);
  const resultSha256 = text(source.resultSha256, 64);
  const recoveryCount = nonnegativeInteger(source.reviewRecoveryCount);
  if (!sessionId || !/^[0-9a-f]{64}$/.test(resultSha256)) return "";
  // Keep the recovery generation before any length truncation. A recovered
  // authorization must never reuse the idempotency key of the orphaned run.
  return `manual-review-r${recoveryCount}-${sessionId}-${resultSha256}`.slice(0, 160);
}

export function manualChatGPTStateView(value) {
  const session = normalizeManualChatGPT(value);
  const states = {
    DRAFT: ["正在准备", "任务包尚未冻结", "working"],
    BUNDLE_READY: ["任务包已冻结", "下一步：复制并打开 ChatGPT", "ready"],
    WAITING_FOR_CHATGPT: [
      "等待 ChatGPT",
      `在同一会话按提示完成 ${session.chatGPTPanels || 1} 次回复，最后复制唯一 JSON；若新页未出现，请手动打开 ChatGPT`,
      "waiting",
    ],
    RESULT_IMPORTED: ["结果已导入", "正在进入确定性校验", "working"],
    VALIDATING: ["正在校验", "核对 Schema、角色、引用与上下文哈希", "working"],
    API_REVIEW: ["ChatGPT 结果已通过", "下一步：按冻结预算运行独立 API 审查", "review"],
    READY_FOR_DECISION: ["可供决定", "独立审查齐备后由你确认冻结", "ready"],
    FROZEN: ["已冻结", "决定卡已由用户确认", "frozen"],
    CONTEXT_STALE: ["上下文已变化", "请生成新的冻结任务包", "blocked"],
    IMPORT_REJECTED: ["导入被拒绝", "按字段路径修复后重新导入", "blocked"],
    BUDGET_BLOCKED: ["预算已阻断", "调整预算后再继续", "blocked"],
    NEEDS_USER_ACTION: ["需要你的处理", "完成当前人工步骤后再继续", "waiting"],
  };
  const [label, detail, tone] = states[session.state] || states.IMPORT_REJECTED;
  return { ...session, label, detail, tone };
}

export function manualChatGPTPrimaryAction(view, options = {}) {
  const source = object(view);
  const state = text(source.state, 40);
  const hasImportText = options.hasImportText === true;
  const hasReviewRoute = options.hasReviewRoute === true;
  const hasDecision = options.hasDecision === true;
  const freezeAcknowledged = options.freezeAcknowledged === true;
  const reset = (label = "创建新任务") => ({
    id: "reset_for_new_bundle",
    label,
    enabled: true,
  });

  if (source.integrityOk !== true) return reset();
  if (state === "BUNDLE_READY") {
    return { id: "copy_and_open_chatgpt", label: "复制任务包并打开 ChatGPT", enabled: true };
  }
  if (state === "WAITING_FOR_CHATGPT") {
    return { id: "import_clipboard", label: "从剪贴板导入", enabled: true };
  }
  if (state === "IMPORT_REJECTED") {
    if (hasImportText) {
      return { id: "reimport_fixed_json", label: "重新校验修复结果", enabled: true };
    }
    if (text(source.repairPrompt, 500_000)) {
      return { id: "copy_repair_prompt", label: "复制修复提示", enabled: true };
    }
    return reset();
  }
  if (state === "CONTEXT_STALE") return reset("生成新任务包");
  if (state === "API_REVIEW") {
    if (source.reviewRecoveryEligible === true) {
      return {
        id: "recover_api_review",
        label: "重新授权并恢复零调用审查",
        enabled: true,
      };
    }
    return {
      id: "run_api_review",
      label: `运行 ${nonnegativeInteger(source.apiReviews)} 次独立 API 审查`,
      enabled: hasReviewRoute && source.apiReviewAvailable === true,
    };
  }
  if (state === "READY_FOR_DECISION") {
    return {
      id: "confirm_freeze",
      label: "确认并冻结决定",
      enabled: hasDecision && freezeAcknowledged,
    };
  }
  if (["BUDGET_BLOCKED", "NEEDS_USER_ACTION", "FROZEN", "DRAFT"].includes(state)) {
    return reset(state === "FROZEN" ? "开始新任务" : "创建新任务");
  }
  if (["RESULT_IMPORTED", "VALIDATING"].includes(state)) {
    return { id: "pending", label: "正在进行确定性校验", enabled: false };
  }
  return reset();
}

export function shortManualChatGPTHash(value) {
  const clean = text(value, 64);
  return clean.length === 64 ? `${clean.slice(0, 8)}…${clean.slice(-6)}` : "不可用";
}

export function formatManualChatGPTContextSize(value) {
  const bytes = nonnegativeInteger(value?.contextUtf8Bytes);
  const tokens = nonnegativeInteger(value?.estimatedContextTokens);
  if (!bytes || !tokens) return "不可用";
  const size = bytes < 1024
    ? `${bytes} B`
    : `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  return `${size} · 约 ${tokens.toLocaleString("zh-CN")} Token`;
}

export function formatManualChatGPTCostEstimate(value) {
  if (value?.costStatus !== "estimated" || !value?.estimatedApiCostUsd) {
    return "待配置";
  }
  const amount = Number(value.estimatedApiCostUsd);
  if (!Number.isFinite(amount) || amount < 0) return "待配置";
  return `约 US$${amount.toFixed(amount < 0.01 ? 4 : 2)}`;
}

export function manualChatGPTIndependenceLabel(value) {
  return {
    same_answer_multi_role_views: "同一回答中的多角色视角",
    same_model_independent_call: "同模型独立调用",
    different_provider_independent_opinion: "不同 Provider 独立意见",
  }[value] || "独立性声明不可识别";
}
