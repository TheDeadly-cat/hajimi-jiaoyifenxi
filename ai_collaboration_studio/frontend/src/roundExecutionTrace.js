export const ROUND_EXECUTION_TRACE_VERSION = "round_execution_trace_v1";

const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const MAX_PAGE_LIMIT = 500;
const MAX_TRACE_EVENTS = 20_000;
const ROUND_DIRECTOR_KIND = "round_director";

const EVENT_META = Object.freeze({
  round_started: { label: "轮次开始", group: "round" },
  round_resumed: { label: "轮次继续", group: "round" },
  round_paused: { label: "轮次暂停", group: "round" },
  round_completed: { label: "轮次完成", group: "round" },
  round_terminal: { label: "轮次终态", group: "round" },
  provider_call_attempt: { label: "Provider 调用", group: "provider" },
  provider_attempt: { label: "Provider 调用", group: "provider" },
  provider_run_created: { label: "Provider 预算建立", group: "provider" },
  provider_call_started: { label: "Provider 调用开始", group: "provider" },
  provider_call_finished: { label: "Provider 调用结束", group: "provider" },
  director_attempt: { label: "主持调度尝试", group: "director" },
  director_attempt_started: { label: "主持调度开始", group: "director" },
  director_attempt_finished: { label: "主持调度结束", group: "director" },
  director_decision: { label: "主持选择", group: "director" },
  director_decision_recorded: { label: "主持选择", group: "director" },
  formal_turn: { label: "成员发言", group: "turn" },
  round_turn: { label: "成员发言", group: "turn" },
  round_turn_reserved: { label: "正式发言预留", group: "turn" },
  round_turn_terminal: { label: "正式发言结束", group: "turn" },
  message: { label: "成员发言", group: "turn" },
  message_persisted: { label: "消息已持久化", group: "turn" },
  candidate_update: { label: "候选修订", group: "candidate" },
  candidate_update_submitted: { label: "候选修订", group: "candidate" },
  candidate_decision_projected: { label: "候选决策投影", group: "candidate" },
  decision_lineage_event: { label: "决策谱系", group: "candidate" },
  risk_review: { label: "复核意见", group: "review" },
  candidate_risk_review: { label: "复核意见", group: "review" },
  candidate_risk_review_projected: { label: "候选风险复核", group: "review" },
  risk: { label: "风险记录", group: "review" },
  risk_registered: { label: "风险已登记", group: "review" },
  artifact: { label: "共创产物", group: "artifact" },
  artifact_created: { label: "共创产物创建", group: "artifact" },
  artifact_confirmed: { label: "共创产物确认", group: "artifact" },
  user_decision: { label: "用户决定", group: "user" },
  user_decision_recorded: { label: "用户决定", group: "user" },
});

const STATUS_META = Object.freeze({
  started: { label: "进行中", tone: "active" },
  running: { label: "进行中", tone: "active" },
  open: { label: "待处理", tone: "active" },
  pending: { label: "待处理", tone: "active" },
  responded: { label: "已响应", tone: "success" },
  completed: { label: "已完成", tone: "success" },
  confirmed: { label: "已确认", tone: "success" },
  verified: { label: "已核验", tone: "success" },
  supported: { label: "已支持", tone: "success" },
  failed: { label: "失败", tone: "danger" },
  invalid: { label: "未通过", tone: "danger" },
  rejected: { label: "已拒绝", tone: "danger" },
  cancelled: { label: "已取消", tone: "muted" },
  abandoned: { label: "已放弃", tone: "muted" },
  paused: { label: "已暂停", tone: "warning" },
  partial: { label: "部分记录", tone: "warning" },
  exhausted: { label: "预算已用尽", tone: "warning" },
  speak: { label: "继续发言", tone: "active" },
  finish: { label: "建议结束", tone: "success" },
  support: { label: "支持", tone: "success" },
  oppose: { label: "反对", tone: "danger" },
  hold: { label: "暂不决定", tone: "warning" },
  ai: { label: "AI 消息", tone: "active" },
  user: { label: "用户消息", tone: "success" },
  system: { label: "系统消息", tone: "muted" },
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

function boolean(value, fallback = false) {
  return typeof value === "boolean" ? value : fallback;
}

function optionalBoolean(value) {
  return typeof value === "boolean" ? value : null;
}

function optionalInteger(value, minimum = 0, maximum = Number.MAX_SAFE_INTEGER) {
  const numericInput = typeof value === "number"
    || (typeof value === "string" && /^-?\d+$/.test(value.trim()));
  if (!numericInput) return null;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < minimum || parsed > maximum) return null;
  return parsed;
}

function sha256(value) {
  const normalized = text(value, 64).toLowerCase();
  return SHA256_PATTERN.test(normalized) ? normalized : "";
}

function cursorText(value) {
  if (typeof value === "string") return text(value, 1_000);
  if (Number.isInteger(value) && value >= 0) return String(value);
  return "";
}

function boundedValue(value, depth = 0) {
  if (depth > 3) return null;
  if (typeof value === "string") return value.slice(0, 4_000);
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "boolean" || value === null) return value;
  if (Array.isArray(value)) {
    return value.slice(0, 50).map((item) => boundedValue(item, depth + 1));
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value)
        .slice(0, 60)
        .map(([key, item]) => [text(key, 120), boundedValue(item, depth + 1)])
        .filter(([key]) => Boolean(key)),
    );
  }
  return null;
}

function stringList(value, limit = 50) {
  if (!Array.isArray(value)) return [];
  return value
    .slice(0, limit)
    .map((item) => {
      if (typeof item === "string") return text(item, 1_000);
      const source = record(item);
      const code = text(source.code, 160);
      const detail = text(source.detail || source.message, 800);
      return [code, detail].filter(Boolean).join("：");
    })
    .filter(Boolean);
}

function projectionIssues(value, limit = 12) {
  return (Array.isArray(value) ? value : [])
    .slice(0, limit)
    .map((item) => {
      if (typeof item === "string") {
        const message = text(item, 800);
        return message ? { code: "", message, candidate_id: "" } : null;
      }
      const source = record(item);
      const code = text(source.code, 160);
      const message = text(source.message || source.detail, 800);
      const candidateId = text(source.candidate_id, 160);
      if (!code && !message) return null;
      return { code, message: message || code, candidate_id: candidateId };
    })
    .filter(Boolean);
}

function normalizeCandidateOption(value) {
  const source = record(value);
  const lineage = record(source.lineage);
  const risks = stringList(source.risks, 12);
  return {
    id: text(source.id, 160),
    title: text(source.title, 240),
    description: text(source.description || source.thesis, 3_000),
    invalidation: text(source.invalidation, 800) || risks[0] || "",
    symbol: text(source.symbol, 40).toUpperCase(),
    direction: text(source.direction, 40).toUpperCase(),
    horizon_days: optionalInteger(source.horizon_days, 1, 100_000),
    timeline: text(source.timeline, 240),
    evidence_count: integer(
      source.evidence_count,
      Array.isArray(source.evidence) ? Math.min(50, source.evidence.length) : 0,
      0,
      50,
    ),
    risks,
    lineage: {
      revision: optionalInteger(lineage.revision, 1, 1_000_000),
      origin_message_id: text(lineage.origin_message_id, 160),
      latest_message_id: text(lineage.latest_message_id, 160),
    },
  };
}

function normalizeCandidateReview(value) {
  const source = record(value);
  const snapshot = record(source.candidate_snapshot);
  return {
    candidate_id: text(source.candidate_id, 160),
    candidate_revision: optionalInteger(source.candidate_revision, 1, 1_000_000),
    current_candidate_revision: optionalInteger(source.current_candidate_revision, 1, 1_000_000),
    action: text(source.action, 40).toLowerCase(),
    status: text(source.status, 40).toLowerCase(),
    reviewer_name: text(source.reviewer_name, 240),
    review_message_id: text(source.review_message_id, 160),
    risk_ids: stringList(source.risk_ids, 24),
    candidate_snapshot: {
      title: text(snapshot.title, 240),
      thesis: text(snapshot.thesis, 3_000),
      invalidation: text(snapshot.invalidation, 800),
      symbol: text(snapshot.symbol, 40).toUpperCase(),
      direction: text(snapshot.direction, 40).toUpperCase(),
      horizon_days: optionalInteger(snapshot.horizon_days, 1, 100_000),
    },
  };
}

function normalizeCandidateProjection(value) {
  if (value == null) return null;
  const source = record(value);
  const lineage = record(source.candidate_lineage);
  const riskReview = record(source.candidate_risk_reviews);
  const decision = record(source.decision);
  const actionCounts = record(riskReview.action_counts);
  return {
    version: text(source.version, 80),
    qualified_message_count: integer(source.qualified_message_count, 0, 0),
    source_message_ids: stringList(source.source_message_ids, 100),
    provisional: source.provisional === true,
    authoritative: source.authoritative === true,
    projection_sha256: sha256(source.projection_sha256),
    candidate_lineage: {
      version: text(lineage.version, 80),
      applicable: lineage.applicable === true,
      ready: lineage.ready === true,
      status: text(lineage.status, 80).toLowerCase(),
      decision_message_id: text(lineage.decision_message_id, 160),
      candidates: (Array.isArray(lineage.candidates) ? lineage.candidates : [])
        .slice(0, 8)
        .map((item) => {
          const candidate = record(item);
          return {
            id: text(candidate.id, 160),
            revision: optionalInteger(candidate.revision, 1, 1_000_000),
            origin_message_id: text(candidate.origin_message_id, 160),
            latest_message_id: text(candidate.latest_message_id, 160),
          };
        })
        .filter((candidate) => Boolean(candidate.id)),
      issues: projectionIssues(lineage.issues),
    },
    candidate_risk_reviews: {
      version: text(riskReview.version, 80),
      applicable: riskReview.applicable === true,
      ready: riskReview.ready === true,
      status: text(riskReview.status, 80).toLowerCase(),
      target_candidate_count: integer(riskReview.target_candidate_count, 0, 0),
      reviewed_candidate_count: integer(riskReview.reviewed_candidate_count, 0, 0),
      current_review_count: integer(riskReview.current_review_count, 0, 0),
      stale_review_count: integer(riskReview.stale_review_count, 0, 0),
      action_counts: {
        support: integer(actionCounts.support, 0, 0),
        challenge: integer(actionCounts.challenge, 0, 0),
        reject: integer(actionCounts.reject, 0, 0),
      },
      reviews: (Array.isArray(riskReview.reviews) ? riskReview.reviews : [])
        .slice(0, 50)
        .map(normalizeCandidateReview)
        .filter((review) => Boolean(review.candidate_id)),
      issues: projectionIssues(riskReview.issues),
      dispositions_only: riskReview.review_actions_are_dispositions_only !== false
        && riskReview.dispositions_only !== false,
      execution_capability: text(riskReview.execution_capability, 40).toLowerCase(),
      live_trading_allowed: riskReview.live_trading_allowed,
      can_autonomously_decide: riskReview.can_autonomously_decide,
    },
    decision: {
      status: text(decision.status, 80).toLowerCase() || "undecided",
      options: (Array.isArray(decision.options) ? decision.options : [])
        .slice(0, 8)
        .map(normalizeCandidateOption)
        .filter((candidate) => Boolean(candidate.id)),
      preferred_option_id: text(decision.preferred_option_id, 160),
      rationale: text(decision.rationale, 3_000),
      evidence_count: integer(
        decision.evidence_count,
        Array.isArray(decision.evidence) ? Math.min(50, decision.evidence.length) : 0,
        0,
        50,
      ),
    },
    issues: projectionIssues(source.issues),
    execution_capability: text(source.execution_capability, 40).toLowerCase(),
    live_trading_allowed: source.live_trading_allowed,
    can_autonomously_decide: source.can_autonomously_decide,
  };
}

function reviewActionCounts(reviews) {
  const counts = { support: 0, challenge: 0, reject: 0 };
  for (const review of reviews) {
    if (Object.prototype.hasOwnProperty.call(counts, review.action)) counts[review.action] += 1;
  }
  return counts;
}

export function candidateProjectionViewModel(value) {
  const projection = normalizeCandidateProjection(value);
  const empty = {
    available: false,
    status: "empty",
    statusLabel: "尚无候选投影",
    tone: "neutral",
    authoritative: false,
    provisional: false,
    qualifiedMessageCount: 0,
    projectionSha256: "",
    candidates: [],
    candidateCount: 0,
    totalRevisionCount: 0,
    lineage: { ready: false, status: "missing", issues: [] },
    riskReview: {
      applicable: false,
      ready: false,
      targetCandidateCount: 0,
      reviewedCandidateCount: 0,
      currentReviewCount: 0,
      staleReviewCount: 0,
      actionCounts: { support: 0, challenge: 0, reject: 0 },
      issues: [],
    },
    decision: {
      status: "undecided",
      preferredOptionId: "",
      preferredTitle: "",
      rationale: "",
      evidenceCount: 0,
      ready: false,
    },
    issues: [],
    safetyVerified: true,
    boundary: "只展示服务端结构化候选；不会从自然语言猜测方案，也不会替用户作最终决定。",
  };
  if (!projection) return empty;

  const lineageById = new Map(
    projection.candidate_lineage.candidates.map((candidate) => [candidate.id, candidate]),
  );
  const reviews = projection.candidate_risk_reviews.reviews;
  const reviewSnapshotById = new Map();
  for (const review of reviews) {
    if (!reviewSnapshotById.has(review.candidate_id)) {
      reviewSnapshotById.set(review.candidate_id, review.candidate_snapshot);
    }
  }
  const optionById = new Map(projection.decision.options.map((candidate) => [candidate.id, candidate]));
  const candidateIds = [...new Set([
    ...projection.decision.options.map((candidate) => candidate.id),
    ...projection.candidate_lineage.candidates.map((candidate) => candidate.id),
    ...reviews.map((review) => review.candidate_id),
  ].filter(Boolean))].slice(0, 8);
  const preferredOptionId = projection.decision.preferred_option_id;
  const candidates = candidateIds.map((id) => {
    const option = optionById.get(id) || { id };
    const lineage = lineageById.get(id) || option.lineage || {};
    const snapshot = reviewSnapshotById.get(id) || {};
    const candidateReviews = reviews.filter((review) => review.candidate_id === id);
    const currentReviews = candidateReviews.filter((review) => review.status === "current");
    const staleReviews = candidateReviews.filter((review) => review.status === "stale");
    const actionCounts = reviewActionCounts(currentReviews);
    const horizonDays = option.horizon_days || snapshot.horizon_days || null;
    const timeline = option.timeline || (horizonDays ? `观察期限：${horizonDays} 天` : "");
    const revision = lineage.revision || option.lineage?.revision || 1;
    return {
      id,
      title: option.title || snapshot.title || id,
      description: option.description || snapshot.thesis || "",
      invalidation: option.invalidation || snapshot.invalidation || "",
      symbol: option.symbol || snapshot.symbol || "",
      direction: option.direction || snapshot.direction || "",
      horizonDays,
      timeline,
      revision,
      originMessageId: lineage.origin_message_id || option.lineage?.origin_message_id || "",
      latestMessageId: lineage.latest_message_id || option.lineage?.latest_message_id || "",
      evidenceCount: option.evidence_count || 0,
      preferred: Boolean(id && id === preferredOptionId),
      currentReviewCount: currentReviews.length,
      staleReviewCount: staleReviews.length,
      actionCounts,
    };
  });
  const risk = projection.candidate_risk_reviews;
  const lineageIssues = projection.candidate_lineage.issues;
  const riskIssues = risk.issues;
  const safetyVerified = projection.execution_capability === "none"
    && projection.live_trading_allowed === false
    && projection.can_autonomously_decide === false
    && (!risk.applicable || (
      risk.execution_capability === "none"
      && risk.live_trading_allowed === false
      && risk.can_autonomously_decide === false
      && risk.dispositions_only
    ));
  const issues = [...projection.issues, ...lineageIssues, ...riskIssues];
  if (!safetyVerified) {
    issues.unshift({
      code: "CANDIDATE_PROJECTION_SAFETY_UNVERIFIED",
      message: "候选投影的只读、无执行和用户最终决定边界未完整核验。",
      candidate_id: "",
    });
  }
  const riskReady = risk.applicable ? risk.ready : true;
  const preferredCandidate = candidates.find((candidate) => candidate.preferred);
  const decisionReady = projection.decision.status === "candidate"
    && Boolean(preferredCandidate)
    && candidates.length >= 2
    && projection.candidate_lineage.ready
    && riskReady
    && safetyVerified;
  const tone = !safetyVerified || issues.length ? "blocked"
    : projection.authoritative ? "authoritative"
      : projection.provisional ? "provisional" : "neutral";
  const statusLabel = !safetyVerified ? "边界异常"
    : projection.authoritative ? "已封印投影"
      : projection.provisional ? "进行中投影" : "只读投影";

  return {
    ...empty,
    available: true,
    status: projection.authoritative ? "authoritative" : projection.provisional ? "provisional" : "readonly",
    statusLabel,
    tone,
    authoritative: projection.authoritative,
    provisional: projection.provisional,
    qualifiedMessageCount: projection.qualified_message_count,
    projectionSha256: projection.projection_sha256,
    candidates,
    candidateCount: candidates.length,
    totalRevisionCount: candidates.reduce((total, candidate) => total + candidate.revision, 0),
    lineage: {
      ready: projection.candidate_lineage.ready,
      status: projection.candidate_lineage.status || "unknown",
      issues: lineageIssues,
    },
    riskReview: {
      applicable: risk.applicable,
      ready: riskReady,
      targetCandidateCount: risk.target_candidate_count,
      reviewedCandidateCount: risk.reviewed_candidate_count,
      currentReviewCount: risk.current_review_count,
      staleReviewCount: risk.stale_review_count,
      actionCounts: risk.action_counts,
      issues: riskIssues,
    },
    decision: {
      status: projection.decision.status,
      preferredOptionId,
      preferredTitle: preferredCandidate?.title || "",
      rationale: projection.decision.rationale,
      evidenceCount: projection.decision.evidence_count,
      ready: decisionReady,
    },
    issues,
    safetyVerified,
  };
}

function normalizeActor(value) {
  const source = record(value);
  return {
    kind: text(source.kind, 40).toLowerCase(),
    id: text(source.id, 160),
    name: text(source.name, 240),
    version: integer(source.version, 0, 0),
    provider: text(source.provider, 80).toLowerCase(),
    model: text(source.model, 240),
  };
}

function normalizeSource(value) {
  const source = record(value);
  return {
    table: text(source.table, 120),
    id: text(source.id, 180),
    sequence_no: integer(source.sequence_no, 0, 0),
  };
}

function normalizeRefs(value) {
  const source = record(value);
  return Object.fromEntries(
    Object.entries(source)
      .slice(0, 60)
      .map(([key, item]) => {
        const cleanKey = text(key, 120);
        if (Array.isArray(item)) {
          return [cleanKey, item.slice(0, 50).map((entry) => text(String(entry), 240)).filter(Boolean)];
        }
        if (["string", "number", "boolean"].includes(typeof item)) {
          return [cleanKey, typeof item === "string" ? text(item, 500) : item];
        }
        return [cleanKey, null];
      })
      .filter(([key, item]) => Boolean(key) && item !== null),
  );
}

function normalizeEvent(value, index) {
  const source = record(value);
  const integrity = record(source.integrity);
  const type = text(source.type, 120).toLowerCase() || "unknown";
  const eventId = text(source.event_id, 240)
    || `${type}:${integer(source.ordinal, index + 1, 0)}:${text(record(source.source).id, 160)}`;
  return {
    ordinal: integer(source.ordinal, index + 1, 0),
    event_id: eventId,
    type,
    occurred_at: integer(source.occurred_at, 0, 0),
    finished_at: integer(source.finished_at, 0, 0),
    source: normalizeSource(source.source),
    actor: normalizeActor(source.actor),
    status: text(source.status, 80).toLowerCase() || "unknown",
    refs: normalizeRefs(source.refs),
    payload: boundedValue(record(source.payload)) || {},
    integrity: {
      status: text(integrity.status, 40).toLowerCase() || "unknown",
      issues: stringList(integrity.issues),
    },
  };
}

function normalizeEvents(value) {
  const seen = new Set();
  return (Array.isArray(value) ? value : [])
    .slice(0, MAX_TRACE_EVENTS)
    .map(normalizeEvent)
    .filter((event) => {
      if (seen.has(event.event_id)) return false;
      seen.add(event.event_id);
      return true;
    })
    .sort((left, right) => (
      left.ordinal - right.ordinal
      || left.occurred_at - right.occurred_at
      || left.event_id.localeCompare(right.event_id)
    ));
}

function normalizeSummary(value) {
  const source = record(value);
  const providerCalls = record(source.provider_calls);
  return {
    event_count: integer(source.event_count, 0, 0),
    anomaly_count: integer(source.anomaly_count, 0, 0),
    provider_calls: {
      reserved: integer(providerCalls.reserved, 0, 0),
      completed: integer(providerCalls.completed, 0, 0),
      max: integer(providerCalls.max, 0, 0),
      status: text(providerCalls.status, 40).toLowerCase(),
    },
    director_attempt_count: integer(source.director_attempt_count, 0, 0),
    director_decision_count: integer(source.director_decision_count, 0, 0),
    formal_turn_count: integer(source.formal_turn_count, 0, 0),
    candidate_update_count: integer(source.candidate_update_count, 0, 0),
    risk_review_count: integer(source.risk_review_count, 0, 0),
    risk_count: integer(source.risk_count, 0, 0),
    artifact_count: integer(source.artifact_count, 0, 0),
    user_decision_count: integer(source.user_decision_count, 0, 0),
  };
}

export function normalizeRoundExecutionTrace(value) {
  const source = record(value);
  const errors = [];
  const version = text(source.version, 80);
  const traceHash = text(source.trace_hash, 128).toLowerCase();
  const roomId = text(source.room_id, 160);
  const roundId = text(source.round_id, 160);
  const integrity = record(source.integrity);
  const history = record(source.history);
  const page = record(source.page);
  const safety = record(source.safety);

  if (version !== ROUND_EXECUTION_TRACE_VERSION) errors.push("执行轨迹版本无法验证。");
  if (!SHA256_PATTERN.test(traceHash)) errors.push("执行轨迹标识无法验证。");
  if (!roomId || !roundId) errors.push("执行轨迹缺少房间或轮次标识。");

  const safeBoundary = safety.read_only === true
    && integer(safety.provider_calls_performed, -1, 0) === 0
    && text(safety.execution_capability, 40).toLowerCase() === "none"
    && safety.live_trading_allowed === false
    && safety.can_autonomously_decide === false;
  if (!safeBoundary) errors.push("执行轨迹的只读安全边界无法验证。");

  const events = normalizeEvents(source.events);
  const summary = normalizeSummary(source.summary);
  const pageLimit = integer(page.limit, events.length || 1, 1, MAX_PAGE_LIMIT);
  const normalized = {
    version,
    trace_hash: traceHash,
    room_id: roomId,
    round_id: roundId,
    round: boundedValue(record(source.round)) || {},
    history: {
      mode: text(history.mode, 80),
      coverage: text(history.coverage, 240),
      limitations: stringList(history.limitations),
    },
    integrity: {
      status: text(integrity.status, 40).toLowerCase() || "invalid",
      ok: boolean(integrity.ok),
      issues: stringList(integrity.issues),
      round_ledger_verified: optionalBoolean(integrity.round_ledger_verified),
      provider_ledger_verified: optionalBoolean(integrity.provider_ledger_verified),
      trace_snapshot_sha256: text(integrity.trace_snapshot_sha256, 64).toLowerCase(),
      snapshot_hash_persisted: boolean(integrity.snapshot_hash_persisted),
      trace_anchor_verified: optionalBoolean(integrity.trace_anchor_verified),
      trace_anchor_sequence: integer(integrity.trace_anchor_sequence, 0, 0),
      trace_anchor_sha256: text(integrity.trace_anchor_sha256, 64).toLowerCase(),
      trace_anchor_version: text(integrity.trace_anchor_version, 80),
    },
    summary,
    events,
    candidate_projection: normalizeCandidateProjection(source.candidate_projection),
    page: {
      limit: pageLimit,
      cursor: cursorText(page.cursor),
      next_cursor: cursorText(page.next_cursor),
      has_more: page.has_more === true && Boolean(cursorText(page.next_cursor)),
      total: integer(page.total, summary.event_count || events.length, 0),
    },
    sorting: boundedValue(record(source.sorting)) || {},
    safety: {
      read_only: safety.read_only === true,
      provider_calls_performed: integer(safety.provider_calls_performed, -1, 0),
      execution_capability: text(safety.execution_capability, 40).toLowerCase(),
      live_trading_allowed: safety.live_trading_allowed === true,
      can_autonomously_decide: safety.can_autonomously_decide === true,
    },
    valid: errors.length === 0,
    errors,
  };

  if (summary.event_count < events.length) {
    normalized.errors.push("执行轨迹汇总数量小于当前返回事件数。");
    normalized.valid = false;
  }
  return normalized;
}

export function mergeRoundExecutionTracePages(currentValue, nextValue) {
  const current = normalizeRoundExecutionTrace(currentValue);
  const next = normalizeRoundExecutionTrace(nextValue);
  if (!current.valid || !next.valid) throw new TypeError("执行轨迹分页无法验证。");
  if (
    current.room_id !== next.room_id
    || current.round_id !== next.round_id
    || current.trace_hash !== next.trace_hash
  ) {
    throw new TypeError("执行轨迹分页不属于同一冻结快照。");
  }
  const byId = new Map();
  for (const event of [...current.events, ...next.events]) byId.set(event.event_id, event);
  return {
    ...next,
    events: [...byId.values()].sort((left, right) => (
      left.ordinal - right.ordinal
      || left.occurred_at - right.occurred_at
      || left.event_id.localeCompare(right.event_id)
    )),
  };
}

export function roundExecutionEventMeta(type) {
  const normalized = text(type, 120).toLowerCase();
  return EVENT_META[normalized] || { label: "其他记录", group: "other" };
}

export function roundExecutionStatusMeta(status) {
  const normalized = text(status, 80).toLowerCase();
  return STATUS_META[normalized] || {
    label: normalized ? normalized.replaceAll("_", " ") : "状态未记录",
    tone: "muted",
  };
}

export function roundExecutionDirectorBudget(trace) {
  const globalLimit = optionalInteger(trace?.summary?.provider_calls?.max);
  let result = {
    kind: ROUND_DIRECTOR_KIND,
    recorded: false,
    valid: false,
    limit: null,
    reserved: null,
    remaining: null,
    event_id: "",
  };
  for (const event of Array.isArray(trace?.events) ? trace.events : []) {
    if (text(event?.type, 120).toLowerCase() !== "provider_run_created") continue;
    const kindBudgets = record(record(event?.payload).kind_call_budgets);
    if (!Object.prototype.hasOwnProperty.call(kindBudgets, ROUND_DIRECTOR_KIND)) continue;
    const budget = record(kindBudgets[ROUND_DIRECTOR_KIND]);
    const limit = optionalInteger(budget.limit);
    const reserved = optionalInteger(budget.reserved);
    const remaining = optionalInteger(budget.remaining);
    const valid = limit !== null
      && reserved !== null
      && remaining !== null
      && reserved <= limit
      && remaining === limit - reserved
      && (globalLimit === null || limit <= globalLimit);
    result = {
      kind: ROUND_DIRECTOR_KIND,
      recorded: true,
      valid,
      limit: valid ? limit : null,
      reserved: valid ? reserved : null,
      remaining: valid ? remaining : null,
      event_id: text(event?.event_id, 240),
    };
  }
  return result;
}

export function roundExecutionTraceAnchorState(trace) {
  const integrity = record(trace?.integrity);
  const history = record(trace?.history);
  const limitations = Array.isArray(history.limitations) ? history.limitations : [];
  const verified = integrity.trace_anchor_verified === true;
  const persisted = integrity.snapshot_hash_persisted === true;
  const sequence = optionalInteger(integrity.trace_anchor_sequence) || 0;
  const snapshotSha256 = sha256(integrity.trace_snapshot_sha256);
  const anchorSha256 = sha256(integrity.trace_anchor_sha256);
  const version = text(integrity.trace_anchor_version, 80);
  const anchorProofReady = verified
    && sequence > 0
    && Boolean(snapshotSha256)
    && Boolean(anchorSha256);

  if (anchorProofReady && persisted) {
    return {
      state: "persisted",
      label: "已持久化",
      detail: `当前轨迹快照与已核验的锚点 #${sequence} 一致。`,
      sequence,
      snapshot_sha256: snapshotSha256,
      anchor_sha256: anchorSha256,
      version,
    };
  }
  if (anchorProofReady) {
    return {
      state: "changed",
      label: "已变化",
      detail: `锚点 #${sequence} 已核验，但当前轨迹在上次持久化后出现了变化。`,
      sequence,
      snapshot_sha256: snapshotSha256,
      anchor_sha256: anchorSha256,
      version,
    };
  }
  const unavailable = limitations.some((item) => String(item).includes("TRACE_ANCHOR_UNAVAILABLE"));
  return {
    state: "pending",
    label: "待持久化",
    detail: unavailable
      ? "该历史轮没有持久化锚点，不能把当前快照描述为已封印。"
      : "尚未取得已核验的持久化锚点，当前快照不能描述为已封印。",
    sequence,
    snapshot_sha256: snapshotSha256,
    anchor_sha256: anchorSha256,
    version,
  };
}

export function roundExecutionTraceSummaryText(trace) {
  const normalized = normalizeRoundExecutionTrace(trace);
  if (!normalized.valid) return "轨迹校验未通过";
  const calls = normalized.summary.provider_calls;
  const callLabel = calls.max > 0 ? `${calls.completed}/${calls.max} 次调用` : "无 Provider 调用";
  return `${normalized.summary.event_count} 步 · ${callLabel} · ${normalized.summary.anomaly_count} 个异常`;
}
