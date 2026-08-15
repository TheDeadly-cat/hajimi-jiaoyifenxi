export const DISCUSSION_AUDIT_VERSION = "discussion_audit_v1";

const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const DYNAMIC_STATUSES = new Set([
  "verified",
  "partial",
  "not_dynamic",
  "not_recorded",
  "legacy_unknown",
]);
const SELECTION_STRUCTURAL_STATUSES = new Set([
  "verified",
  "partial",
  "not_dynamic",
  "unknown",
]);

const DYNAMIC_STATUS_META = Object.freeze({
  verified: {
    label: "动态结构已核验",
    tone: "verified",
    detail: "主持人的动态选择、候选范围与调度快照均有结构化记录。",
  },
  partial: {
    label: "动态结构部分核验",
    tone: "warning",
    detail: "已记录动态选择，但部分调度上下文或资格校验不完整。",
  },
  not_dynamic: {
    label: "本轮不是动态调度",
    tone: "neutral",
    detail: "执行记录显示本轮采用顺序发言，不能视为主持人动态选人。",
  },
  not_recorded: {
    label: "动态结构未记录",
    tone: "warning",
    detail: "当前记录不足以确认本轮使用了哪种发言调度结构。",
  },
  legacy_unknown: {
    label: "历史轮次结构未知",
    tone: "unknown",
    detail: "该轮次来自旧版历史记录，无法按当前契约追溯动态调度。",
  },
});

const FINDING_LABELS = Object.freeze({
  SEMANTIC_CAUSALITY_UNKNOWN: "模型实际输入未留存证明，语义因果关系未知",
  LEGACY_HISTORY_PARTIAL: "旧版历史记录仅能做部分结构审计",
  STRUCTURAL_DYNAMIC_NOT_RECORDED: "未记录可核验的动态调度结构",
  STRUCTURAL_DYNAMIC_PARTIAL: "动态调度结构仅部分核验",
  FALLBACK_USED: "主持调度使用过安全回退",
  CANDIDATE_GENERATION_INSUFFICIENT: "可比较候选数量不足",
  CANDIDATE_CHECKPOINT_BLOCKED: "候选检查点尚未满足",
});

function record(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function text(value, maxLength = 4_000) {
  return typeof value === "string" ? value.trim().slice(0, maxLength) : "";
}

function integer(value, fallback = 0, minimum = 0, maximum = Number.MAX_SAFE_INTEGER) {
  const numericInput = typeof value === "number"
    || (typeof value === "string" && /^-?\d+$/.test(value.trim()));
  if (!numericInput) return fallback;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < minimum || parsed > maximum) return fallback;
  return parsed;
}

function stringList(value, maximum = 100) {
  if (!Array.isArray(value)) return [];
  return value.slice(0, maximum).map((item) => text(item, 240)).filter(Boolean);
}

function normalizeFinding(value) {
  const source = record(value);
  return {
    code: text(source.code, 80).toUpperCase(),
    severity: text(source.severity, 40).toLowerCase() || "info",
    scope: text(source.scope, 80).toLowerCase() || "round",
    count: integer(source.count, 0, 0),
    candidate_count: integer(source.candidate_count, 0, 0),
    minimum_required: integer(source.minimum_required, 0, 0),
  };
}

function normalizeSelection(value) {
  const source = record(value);
  const scheduling = record(source.scheduling_snapshot);
  return {
    sequence_no: integer(source.sequence_no, 0, 0),
    event_id: text(source.event_id, 160),
    director_decision_id: text(source.director_decision_id, 160),
    action: text(source.action, 80).toLowerCase() || "unrecorded",
    selected_member_id: text(source.selected_member_id, 160),
    source: text(source.source, 80).toLowerCase() || "unrecognized",
    decision_authority: text(source.decision_authority, 80).toLowerCase() || "unrecorded",
    discussion_mode: text(source.discussion_mode, 80).toLowerCase() || "unrecorded",
    moderator_model_call_recorded: source.moderator_model_call_recorded === true,
    fallback: source.fallback === true,
    structural_status: text(source.structural_status, 40).toLowerCase() || "unknown",
    scheduling_snapshot: {
      recorded: scheduling.recorded === true,
      eligible_member_count: integer(scheduling.eligible_member_count, 0, 0),
      gap_count: integer(scheduling.gap_count, 0, 0),
      selected_member_eligible: scheduling.selected_member_eligible === true,
      selected_gap_codes: stringList(scheduling.selected_gap_codes, 50),
    },
  };
}

function normalizeResponseEdge(value) {
  const source = record(value);
  return {
    from_message_id: text(source.from_message_id, 160),
    to_message_id: text(source.to_message_id, 160),
    target_within_formal_bundle: source.target_within_formal_bundle === true,
    persisted_reply_target: source.persisted_reply_target === true,
    structurally_verified: source.structurally_verified === true,
    semantic_causality_status: text(source.semantic_causality_status, 40).toLowerCase()
      || "unknown",
  };
}

function normalizeCandidateCheckpoint(value) {
  const source = record(value);
  const lineage = record(source.lineage);
  const riskReview = record(source.risk_review);
  const decision = record(source.decision);
  return {
    applicable: source.applicable === true,
    status: text(source.status, 40).toLowerCase() || "blocked",
    ready: source.ready === true,
    candidate_count: integer(source.candidate_count, 0, 0),
    minimum_comparison_count: integer(source.minimum_comparison_count, 2, 1),
    comparison_count_satisfied: source.comparison_count_satisfied === true,
    candidates: (Array.isArray(source.candidates) ? source.candidates : [])
      .slice(0, 100)
      .map((item) => {
        const candidate = record(item);
        return {
          id: text(candidate.id, 160),
          revision: integer(candidate.revision, 1, 1),
          origin_message_id: text(candidate.origin_message_id, 160),
          latest_message_id: text(candidate.latest_message_id, 160),
        };
      })
      .filter((candidate) => Boolean(candidate.id)),
    lineage: {
      status: text(lineage.status, 80).toLowerCase() || "not_recorded",
      ready: lineage.ready === true,
      blocker_codes: stringList(lineage.blocker_codes, 100),
      referenced_candidate_ids: stringList(lineage.referenced_candidate_ids, 100),
    },
    risk_review: {
      required: riskReview.required === true,
      status: text(riskReview.status, 80).toLowerCase() || "not_recorded",
      ready: riskReview.ready === true,
      target_candidate_count: integer(riskReview.target_candidate_count, 0, 0),
      reviewed_candidate_count: integer(riskReview.reviewed_candidate_count, 0, 0),
      review_count: integer(riskReview.review_count, 0, 0),
      blocker_codes: stringList(riskReview.blocker_codes, 100),
    },
    decision: {
      status: text(decision.status, 80).toLowerCase() || "undecided",
      preferred_option_id: text(decision.preferred_option_id, 160),
    },
  };
}

export function emptyDiscussionAuditState(overrides = {}) {
  return {
    roomId: "",
    roundId: "",
    audit: null,
    loading: false,
    error: "",
    stale: false,
    ...overrides,
  };
}

export function normalizeDiscussionAudit(value, { expectedTraceHash = "" } = {}) {
  const source = record(value);
  const errors = [];
  const version = text(source.version, 80);
  const auditHash = text(source.audit_hash, 64).toLowerCase();
  const roomId = text(source.room_id, 160);
  const roundId = text(source.round_id, 160);
  const sourceMeta = record(source.source);
  const coverage = record(source.coverage);
  const structural = record(source.structural);
  const semantic = record(source.semantic_causality);
  const safety = record(source.safety);
  const sourceTraceHash = text(sourceMeta.trace_hash, 64).toLowerCase();
  const selections = (Array.isArray(structural.selections) ? structural.selections : [])
    .slice(0, 1_000)
    .map(normalizeSelection);
  const responseEdges = (Array.isArray(structural.response_edges) ? structural.response_edges : [])
    .slice(0, 2_000)
    .map(normalizeResponseEdge);
  const checkpoint = normalizeCandidateCheckpoint(source.candidate_checkpoint);

  if (version !== DISCUSSION_AUDIT_VERSION) errors.push("讨论审计版本无法验证。");
  if (!SHA256_PATTERN.test(auditHash)) errors.push("讨论审计标识无法验证。");
  if (!roomId || !roundId) errors.push("讨论审计缺少房间或轮次标识。");
  if (!SHA256_PATTERN.test(sourceTraceHash)) errors.push("讨论审计来源轨迹标识无法验证。");
  const normalizedExpectedTraceHash = text(expectedTraceHash, 64).toLowerCase();
  if (expectedTraceHash && (
    !SHA256_PATTERN.test(normalizedExpectedTraceHash)
    || sourceTraceHash !== normalizedExpectedTraceHash
  )) {
    errors.push("讨论审计与当前执行轨迹的冻结快照不一致。");
  }

  const dynamicStatus = text(structural.dynamic_status, 40).toLowerCase();
  if (!DYNAMIC_STATUSES.has(dynamicStatus)) errors.push("动态讨论结构状态无法验证。");
  if (selections.some((item) => !SELECTION_STRUCTURAL_STATUSES.has(item.structural_status))) {
    errors.push("主持选择的结构状态无法验证。");
  }
  if (integer(structural.selection_count, -1, 0) !== selections.length) {
    errors.push("主持选择数量与明细不一致。");
  }
  if (
    integer(structural.dynamic_selection_count, -1, 0)
    !== selections.filter((item) => item.discussion_mode === "dynamic").length
  ) {
    errors.push("动态主持选择数量与明细不一致。");
  }
  if (integer(structural.fallback_count, -1, 0) !== selections.filter((item) => item.fallback).length) {
    errors.push("主持回退数量与明细不一致。");
  }
  if (integer(structural.response_edge_count, -1, 0) !== responseEdges.length) {
    errors.push("回应边数量与明细不一致。");
  }
  if (responseEdges.some((edge) => (
    !edge.from_message_id
    || !edge.to_message_id
    || !edge.structurally_verified
    || edge.semantic_causality_status !== "unknown"
  ))) {
    errors.push("回应边的结构证明或语义边界无法验证。");
  }

  if (
    text(semantic.status, 40).toLowerCase() !== "unknown"
    || semantic.proven !== false
    || text(semantic.reason_code, 120).toUpperCase()
      !== "EFFECTIVE_MODEL_INPUT_ATTESTATION_UNAVAILABLE"
  ) {
    errors.push("语义因果关系边界无法验证。");
  }

  if (checkpoint.candidate_count !== checkpoint.candidates.length) {
    errors.push("候选检查点数量与明细不一致。");
  }
  if (
    !new Set(["ready", "blocked", "not_applicable"]).has(checkpoint.status)
    || checkpoint.ready !== (checkpoint.status === "ready")
    || checkpoint.applicable === (checkpoint.status === "not_applicable")
  ) {
    errors.push("候选检查点状态无法验证。");
  }
  if (
    checkpoint.comparison_count_satisfied
    !== (checkpoint.candidate_count >= checkpoint.minimum_comparison_count)
  ) {
    errors.push("候选比较门槛状态不一致。");
  }

  const safeBoundary = safety.read_only === true
    && integer(safety.database_writes_performed, -1, 0) === 0
    && integer(safety.provider_calls_performed, -1, 0) === 0
    && integer(safety.market_data_calls_performed, -1, 0) === 0
    && text(safety.execution_capability, 40).toLowerCase() === "none"
    && safety.live_trading_allowed === false
    && safety.can_autonomously_decide === false
    && safety.raw_content_included === false;
  if (!safeBoundary) errors.push("讨论审计的只读安全边界无法验证。");

  return {
    version,
    audit_hash: auditHash,
    room_id: roomId,
    round_id: roundId,
    source: {
      trace_version: text(sourceMeta.trace_version, 80),
      trace_hash: sourceTraceHash,
      trace_integrity_status: text(sourceMeta.trace_integrity_status, 40).toLowerCase()
        || "unknown",
      trace_integrity_issue_codes: stringList(sourceMeta.trace_integrity_issue_codes, 100),
      turn_contract_applicable: sourceMeta.turn_contract_applicable === true,
      turn_contract_valid: sourceMeta.turn_contract_valid === true,
      turn_contract_version: text(sourceMeta.turn_contract_version, 80),
    },
    coverage: {
      history_mode: text(coverage.history_mode, 80).toLowerCase() || "unknown",
      status: text(coverage.status, 80).toLowerCase() || "unknown",
      limitation_codes: stringList(coverage.limitation_codes, 100),
    },
    structural: {
      dynamic_status: dynamicStatus || "not_recorded",
      selection_count: integer(structural.selection_count, 0, 0),
      dynamic_selection_count: integer(structural.dynamic_selection_count, 0, 0),
      fallback_count: integer(structural.fallback_count, 0, 0),
      selections,
      response_edge_count: integer(structural.response_edge_count, 0, 0),
      response_edges: responseEdges,
    },
    candidate_checkpoint: checkpoint,
    semantic_causality: {
      status: text(semantic.status, 40).toLowerCase() || "unknown",
      proven: semantic.proven === true,
      reason_code: text(semantic.reason_code, 120).toUpperCase(),
    },
    findings: (Array.isArray(source.findings) ? source.findings : [])
      .slice(0, 200)
      .map(normalizeFinding)
      .filter((finding) => Boolean(finding.code)),
    safety: {
      read_only: safety.read_only === true,
      database_writes_performed: integer(safety.database_writes_performed, -1, 0),
      provider_calls_performed: integer(safety.provider_calls_performed, -1, 0),
      market_data_calls_performed: integer(safety.market_data_calls_performed, -1, 0),
      execution_capability: text(safety.execution_capability, 40).toLowerCase(),
      live_trading_allowed: safety.live_trading_allowed === true,
      can_autonomously_decide: safety.can_autonomously_decide === true,
      raw_content_included: safety.raw_content_included === true,
    },
    valid: errors.length === 0,
    errors,
  };
}

function selectionStatusMeta(value) {
  if (value === "verified") return { label: "结构已核验", tone: "verified" };
  if (value === "partial") return { label: "部分核验", tone: "warning" };
  if (value === "not_dynamic") return { label: "顺序调度", tone: "neutral" };
  return { label: "结构未知", tone: "unknown" };
}

function checkpointStatusMeta(checkpoint) {
  if (!checkpoint.applicable || checkpoint.status === "not_applicable") {
    return { label: "本轮不适用", tone: "neutral" };
  }
  if (checkpoint.ready && checkpoint.status === "ready") {
    return { label: "检查点已满足", tone: "verified" };
  }
  return { label: "检查点受阻", tone: "warning" };
}

export function discussionAuditViewModel(value, options = {}) {
  const audit = normalizeDiscussionAudit(value, options);
  const dynamic = DYNAMIC_STATUS_META[audit.structural.dynamic_status]
    || DYNAMIC_STATUS_META.not_recorded;
  const checkpointMeta = checkpointStatusMeta(audit.candidate_checkpoint);
  const findings = audit.findings.map((finding) => ({
    ...finding,
    label: FINDING_LABELS[finding.code] || finding.code,
    tone: finding.severity === "warning" || finding.severity === "error"
      ? "warning"
      : "neutral",
  }));

  return {
    valid: audit.valid,
    errors: audit.errors,
    auditHash: audit.audit_hash,
    coverage: audit.coverage,
    dynamic: {
      status: audit.structural.dynamic_status,
      ...dynamic,
      selectionCount: audit.structural.selection_count,
      dynamicSelectionCount: audit.structural.dynamic_selection_count,
      fallbackCount: audit.structural.fallback_count,
    },
    selections: audit.structural.selections.map((selection) => ({
      ...selection,
      statusMeta: selectionStatusMeta(selection.structural_status),
      memberLabel: selection.action === "finish"
        ? "主持人建议结束"
        : selection.selected_member_id || "成员未记录",
      sourceLabel: selection.fallback
        ? "安全回退"
        : selection.decision_authority === "moderator_model"
          && selection.moderator_model_call_recorded
          ? "主持模型"
          : "规则或未记录",
    })),
    semantic: {
      status: "unknown",
      label: "语义因果关系未知",
      reasonCode: audit.semantic_causality.reason_code,
      detail: "只能确认回应目标与结构化记录相连，无法证明模型实际读取、理解或因该内容作答。",
    },
    responseEdges: audit.structural.response_edges.map((edge) => ({
      ...edge,
      relationLabel: edge.persisted_reply_target ? "持久化回复目标" : "契约回应目标",
      scopeLabel: edge.target_within_formal_bundle ? "本轮正式消息" : "本轮外目标",
    })),
    checkpoint: {
      ...audit.candidate_checkpoint,
      ...checkpointMeta,
      countLabel: `${audit.candidate_checkpoint.candidate_count} / ${audit.candidate_checkpoint.minimum_comparison_count}`,
      decisionLabel: audit.candidate_checkpoint.decision.preferred_option_id
        ? `条件化首选 ${audit.candidate_checkpoint.decision.preferred_option_id}`
        : audit.candidate_checkpoint.decision.status === "deferred"
          ? "决策已暂缓"
          : "尚无条件化首选",
    },
    findings,
    hasFallback: audit.structural.fallback_count > 0,
    boundary: "此处只核验持久化结构，不证明语义因果，也不授予交易、执行或最终决策权限。",
  };
}
