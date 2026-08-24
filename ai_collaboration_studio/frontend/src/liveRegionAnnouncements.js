const CHAT_ANNOUNCEMENT_LIMIT = 120;
const CHAT_SENDER_LIMIT = 48;
const MESSAGE_ID_LIMIT = 240;
const GRAPHEME_SEGMENTER = typeof Intl !== "undefined" && typeof Intl.Segmenter === "function"
  ? new Intl.Segmenter("zh", { granularity: "grapheme" })
  : null;

function normalizedAnnouncementText(value) {
  if (typeof value !== "string") return "";
  return value
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function boundedAnnouncementText(value, limit) {
  const normalized = normalizedAnnouncementText(value);
  const segments = GRAPHEME_SEGMENTER
    ? [...GRAPHEME_SEGMENTER.segment(normalized)].map(({ segment }) => segment)
    : Array.from(normalized);
  return segments.length > limit ? `${segments.slice(0, limit).join("")}…` : normalized;
}

function stableMessageId(value) {
  if (typeof value !== "string") return "";
  const normalized = value.trim();
  return normalized && normalized.length <= MESSAGE_ID_LIMIT ? normalized : "";
}

function messageAnnouncementText(message) {
  const senderFallback = message?.sender_type === "system" ? "系统" : "协作成员";
  const sender = boundedAnnouncementText(message?.sender_name, CHAT_SENDER_LIMIT) || senderFallback;
  const content = boundedAnnouncementText(message?.content, CHAT_ANNOUNCEMENT_LIMIT);
  if (!content) return `新消息，${sender}。`;
  return `新消息，${sender}：${content}`;
}

function tailMessage(messages) {
  const list = Array.isArray(messages) ? messages : [];
  return list.length ? list[list.length - 1] : null;
}

export function nextChatAnnouncementState(previous, {
  messages,
  roomId,
  searchActive = false,
  historyLoading = false,
}) {
  const prior = previous && typeof previous === "object" ? previous : {};
  const list = Array.isArray(messages) ? messages : [];
  const tail = tailMessage(list);
  const tailId = stableMessageId(tail?.id);
  const contextId = stableMessageId(roomId);
  const next = {
    initialized: true,
    roomId: contextId,
    tailId,
    historyLoading: Boolean(historyLoading),
  };
  const contextChanged = prior.initialized !== true || stableMessageId(prior.roomId) !== contextId;
  if (contextChanged || searchActive) {
    return { next, announcement: null, clear: true };
  }
  const tailIdCount = tailId
    ? list.reduce((count, message) => count + (stableMessageId(message?.id) === tailId ? 1 : 0), 0)
    : 0;
  if (!tailId || tailIdCount !== 1) return { next, announcement: null, clear: true };
  if (tailId === stableMessageId(prior.tailId)) {
    return { next, announcement: null, clear: false };
  }
  const previousTailId = stableMessageId(prior.tailId);
  const previousTailStillLoaded = !previousTailId
    || list.some((message) => stableMessageId(message?.id) === previousTailId);
  if (!previousTailStillLoaded) return { next, announcement: null, clear: true };
  return {
    next,
    clear: false,
    announcement: {
      id: tailId,
      text: messageAnnouncementText(tail),
    },
  };
}

function compactAnnouncementText(value, fallback) {
  return boundedAnnouncementText(value, CHAT_ANNOUNCEMENT_LIMIT)
    || boundedAnnouncementText(fallback, CHAT_ANNOUNCEMENT_LIMIT);
}

function announcementClause(value, fallback) {
  return compactAnnouncementText(value, fallback).replace(/[。！？.!?]+$/u, "");
}

export function meetingReadinessAnnouncementText({
  workflowReady = false,
  marketRequired = false,
  marketReady = false,
  marketState = "",
  providerStatus = "idle",
  reason = "",
} = {}) {
  const roleText = workflowReady === true ? "角色已覆盖" : "角色有缺口";
  const marketText = marketRequired === true
    ? `；Futu ${marketReady === true ? "已就绪" : marketState === "checking" ? "检查中" : "未就绪"}`
    : "";
  const providerText = providerStatus === "ready"
    ? "模型检查通过"
    : providerStatus === "checking"
      ? "模型检查中"
      : providerStatus === "failed" || providerStatus === "blocked"
        ? "模型未通过"
        : "模型待检查";
  const base = `房间就绪状态：${roleText}${marketText}；${providerText}。`;
  return reason ? `${base} 首要阻塞：${announcementClause(reason, "存在未满足的启动条件")}。` : base;
}

export function storageAcceptanceAnnouncementText(model = {}) {
  const state = compactAnnouncementText(model.stateLabel, "状态未知");
  const firstBlocker = Array.isArray(model.blockers) ? model.blockers[0] : null;
  if (firstBlocker?.title) {
    return `样板验收状态：${state}。首要阻塞：${announcementClause(firstBlocker.title, "存在未解决的验收阻塞")}。`;
  }
  const firstNextAction = Array.isArray(model.nextActions) ? model.nextActions[0] : "";
  return `样板验收状态：${state}。下一步：${announcementClause(firstNextAction, "等待新的验收输入")}。`;
}

export function convergenceAnnouncementText(convergence) {
  if (!convergence) return "收敛状态：正在计算。";
  const status = convergence.can_present_candidate_best === true
    ? "候选方案可供用户复核"
    : convergence.can_host_finish === true
      ? "讨论可结束，等待用户复核"
      : "尚未达到收敛条件";
  const firstBlocker = Array.isArray(convergence.blockers) ? convergence.blockers[0] : null;
  if (firstBlocker) {
    return `收敛状态：${status}。首要阻塞：${compactAnnouncementText(firstBlocker.title, "存在尚未解决的阻塞项")}。`;
  }
  const firstNextAction = Array.isArray(convergence.next_actions) ? convergence.next_actions[0] : "";
  return `收敛状态：${status}。下一步：${compactAnnouncementText(firstNextAction, "等待本轮目标")}。`;
}
