const MODERATOR_CONTEXT_VERSION = "director_moderator_context_v1";

const SOURCE_LABELS = {
  ai: "主持模型",
  policy: "流程规则",
  rules_first: "规则优先",
  fallback: "安全回退",
  director_circuit_breaker: "主持熔断回退",
  provider_call_budget_reserve: "调用预算保留",
  user_mention: "用户点名",
  user_interjection: "用户插话",
};

const AUTHORITY_LABELS = {
  moderator_model: "主持模型实际选择",
  user_direction: "用户指向，未调用主持模型",
  service_policy: "流程规则决定，未调用主持模型",
  safety_fallback: "安全回退决定，未调用主持模型",
};

function cleanText(value) {
  return typeof value === "string" ? value.trim() : "";
}

export function normalizeModeratorContext(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  if (!Object.keys(value).length) return null;
  return { ...value };
}

export function normalizeDirectorDecision(event = {}, roomId = "", roundId = "") {
  const persisted = event.decision && typeof event.decision === "object" ? event.decision : {};
  const member = event.member && typeof event.member === "object" ? event.member : null;
  const createdAt = persisted.created_at || Date.now();
  const sequenceNo = Number(persisted.sequence_no) || 0;
  const moderatorContext = normalizeModeratorContext(
    persisted.moderator_context || event.moderator_context,
  );
  const decision = {
    id: String(persisted.id || ""),
    room_id: String(persisted.room_id || roomId || ""),
    round_id: String(persisted.round_id || roundId || ""),
    sequence_no: sequenceNo,
    action: persisted.action || event.action || "speak",
    member_id: persisted.member_id || member?.id || "",
    member_name: persisted.member_name || member?.name || "",
    reason: persisted.reason || event.reason || "主持人未提供补充说明。",
    source: persisted.source || event.source || "policy",
    stage: persisted.stage || event.stage || "flexible",
    workspace_focus: persisted.workspace_focus || event.workspace_focus || null,
    moderator_context: moderatorContext,
    created_at: createdAt,
  };
  if (!decision.id) {
    decision.id = [
      "live-director",
      decision.room_id,
      decision.round_id,
      sequenceNo,
      decision.action,
      decision.member_id,
      createdAt,
    ].join(":");
  }
  return decision;
}

export function directorSourceLabel(source) {
  const normalized = cleanText(source).toLowerCase();
  return SOURCE_LABELS[normalized] || cleanText(source) || "流程规则";
}

export function directorModeratorAttribution(context) {
  if (
    !context
    || typeof context !== "object"
    || Array.isArray(context)
    || !Object.keys(context).length
  ) {
    return {
      available: false,
      legacy: true,
      notice: "旧记录未保存主持身份与模型归因",
    };
  }

  const memberName = cleanText(context.member_name);
  const rawMemberVersion = Number(context.member_version);
  const memberVersion = Number.isInteger(rawMemberVersion) && rawMemberVersion > 0
    ? rawMemberVersion
    : null;
  const provider = cleanText(context.provider);
  const model = cleanText(context.model);
  const authority = cleanText(context.decision_authority).toLowerCase();
  const modelUsageKnown = typeof context.model_used === "boolean";
  const modelUsed = context.model_used === true;
  const authorityKnown = Object.hasOwn(AUTHORITY_LABELS, authority);
  const authorityMatchesUsage = modelUsageKnown
    && modelUsed === (authority === "moderator_model");
  const discussionMode = cleanText(context.discussion_mode).toLowerCase();
  const discussionModeKnown = ["dynamic", "sequential"].includes(discussionMode);
  const complete = context.version === MODERATOR_CONTEXT_VERSION
    && Boolean(cleanText(context.member_id))
    && Boolean(memberName)
    && memberVersion !== null
    && Boolean(provider)
    && authorityKnown
    && authorityMatchesUsage
    && discussionModeKnown;

  return {
    available: true,
    legacy: false,
    complete,
    memberName: memberName || "主持人未记录",
    memberVersion,
    identity: cleanText(context.identity),
    provider: provider || "Provider 未记录",
    model: model || "默认模型（未显式指定）",
    authority,
    authorityLabel: authorityKnown && authorityMatchesUsage
      ? AUTHORITY_LABELS[authority]
      : (modelUsageKnown
        ? modelUsed
          ? "模型调用归因不完整"
          : "未调用模型，但决策权威未核实"
        : "是否调用主持模型未记录"),
    modelUsageKnown,
    modelUsed,
    discussionMode,
    discussionModeLabel: discussionMode === "sequential"
      ? "顺序讨论"
      : discussionMode === "dynamic"
        ? "动态讨论"
        : "讨论模式未记录",
    notice: complete ? "" : "主持归因字段不完整",
  };
}
