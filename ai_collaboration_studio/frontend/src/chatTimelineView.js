const TIME_FORMATTER = new Intl.DateTimeFormat("zh-CN", {
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

const DAY_FORMATTER = new Intl.DateTimeFormat("zh-CN", {
  month: "long",
  day: "numeric",
  weekday: "short",
});

export const CHAT_TIMELINE_MEMBER_LIMIT = 500;
export const CHAT_TIMELINE_MESSAGE_LIMIT = 2000;
export const CHAT_TIMELINE_DECISION_LIMIT = 500;
export const CHAT_TIMELINE_CITATION_LIMIT = 60;
export const CHAT_TIMELINE_TRANSIENT_ERROR_LIMIT = 100;
export const CHAT_TIMELINE_CONTENT_LIMIT = 40000;
const CHAT_TIMELINE_TEXT_LIMIT = 100000;

function record(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function array(value) {
  return Array.isArray(value) ? value : [];
}

export function safeTimelineText(value, maxLength = CHAT_TIMELINE_TEXT_LIMIT) {
  if (typeof value !== "string") return "";
  const safeLimit = Number.isSafeInteger(maxLength) && maxLength > 0
    ? Math.min(maxLength, CHAT_TIMELINE_TEXT_LIMIT)
    : CHAT_TIMELINE_TEXT_LIMIT;
  return value.slice(0, safeLimit).trim();
}

export function timelineTimestamp(value) {
  const candidate = typeof value === "number"
    ? value
    : safeTimelineText(value, 80);
  if (candidate === "") return 0;
  const timestamp = new Date(candidate).getTime();
  return Number.isFinite(timestamp) ? timestamp : 0;
}

export function formatTimelineTime(value) {
  const timestamp = timelineTimestamp(value);
  return timestamp ? TIME_FORMATTER.format(new Date(timestamp)) : "时间未知";
}

export function chatInitials(value = "AI") {
  const name = safeTimelineText(value) || "AI";
  return name === "我" ? "我" : Array.from(name).slice(0, 2).join("");
}

export function safeAvatarColor(value, fallback = "#4f6b8a") {
  const color = safeTimelineText(value);
  return /^#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(color)
    ? color
    : fallback;
}

function dayIdentity(timestamp) {
  if (!timestamp) return { key: "unknown", label: "时间未知" };
  const date = new Date(timestamp);
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return {
    key: year + "-" + month + "-" + day,
    label: DAY_FORMATTER.format(date),
  };
}

export function normalizeTimelineCitations(value) {
  return buildTimelineCitationPresentation(value).citations;
}

export function buildTimelineCitationPresentation(value) {
  const source = array(value);
  const citations = source.slice(0, CHAT_TIMELINE_CITATION_LIMIT).flatMap((citation, index) => {
    const normalized = record(citation);
    return normalized === citation ? [{ ...normalized, sourceIndex: index }] : [];
  });
  return {
    citations,
    totalCount: source.length,
    hiddenCount: Math.max(0, source.length - CHAT_TIMELINE_CITATION_LIMIT),
    projectionLimited: source.length > CHAT_TIMELINE_CITATION_LIMIT,
  };
}

export function normalizeTransientErrors(value) {
  const keyCounts = new Map();
  return array(value).slice(0, CHAT_TIMELINE_TRANSIENT_ERROR_LIMIT).flatMap((error, index) => {
    const normalized = record(error);
    if (normalized !== error) return [];
    const baseId = safeTimelineText(normalized.id, 240) || "transient-error-" + index;
    const duplicateIndex = keyCounts.get(baseId) || 0;
    keyCounts.set(baseId, duplicateIndex + 1);
    return [{
      id: duplicateIndex ? baseId + ":duplicate-" + duplicateIndex : baseId,
      name: safeTimelineText(normalized.name, 160) || "成员",
      message: safeTimelineText(normalized.message, 1000) || "未知错误",
    }];
  });
}

export function buildTimelineMemberIndex(value) {
  const source = array(value);
  const memberMap = new Map();
  for (const rawMember of source.slice(0, CHAT_TIMELINE_MEMBER_LIMIT)) {
    const member = record(rawMember);
    if (member !== rawMember) continue;
    const id = safeTimelineText(member.id, 240);
    if (id && !memberMap.has(id)) memberMap.set(id, member);
  }
  return {
    memberMap,
    totalCount: source.length,
    indexedCount: memberMap.size,
    projectionLimited: source.length > CHAT_TIMELINE_MEMBER_LIMIT,
  };
}

export function buildChatTimelinePresentation({
  messages = [],
  directorDecisions = [],
  searchState = {},
} = {}) {
  const search = record(searchState);
  const query = safeTimelineText(search.query, 200);
  const searchActive = Boolean(query);
  const sourceMessages = searchActive ? array(search.messages) : array(messages);
  const sourceDirectorDecisions = searchActive
    ? array(search.directorDecisions)
    : array(directorDecisions);
  const visibleMessages = sourceMessages.length > CHAT_TIMELINE_MESSAGE_LIMIT
    ? sourceMessages.slice(-CHAT_TIMELINE_MESSAGE_LIMIT)
    : sourceMessages;
  const visibleDirectorDecisions = sourceDirectorDecisions.length > CHAT_TIMELINE_DECISION_LIMIT
    ? sourceDirectorDecisions.slice(-CHAT_TIMELINE_DECISION_LIMIT)
    : sourceDirectorDecisions;
  const sourceRecordCount = sourceMessages.length + sourceDirectorDecisions.length;
  const omittedRecordCount = sourceRecordCount
    - visibleMessages.length
    - visibleDirectorDecisions.length;
  const loadedMessageIds = new Set(
    visibleMessages
      .map((message) => safeTimelineText(record(message).id, 240))
      .filter(Boolean),
  );
  const records = [
    ...visibleMessages.map((rawMessage, index) => {
      const message = record(rawMessage);
      return {
        baseId: "message:" + (safeTimelineText(message.id, 240) || "missing-" + index),
        kind: "message",
        timestamp: timelineTimestamp(message.created_at),
        sequence: 0,
        value: message,
      };
    }),
    ...visibleDirectorDecisions.map((rawDecision, index) => {
      const decision = record(rawDecision);
      const rawSequence = Number(decision.sequence_no);
      const sequence = Number.isSafeInteger(rawSequence) && rawSequence >= 0 ? rawSequence : 0;
      return {
        baseId: "director:" + (safeTimelineText(decision.id, 240) || "missing-" + index),
        kind: "director",
        timestamp: timelineTimestamp(decision.created_at),
        sequence,
        value: decision,
      };
    }),
  ].sort((left, right) => (
    left.timestamp - right.timestamp
    || (left.kind === right.kind
      ? left.sequence - right.sequence
      : left.kind === "director" ? -1 : 1)
    || left.baseId.localeCompare(right.baseId)
  ));

  const timelineItems = [];
  const keyCounts = new Map();
  let previousDayKey = "";
  for (const item of records) {
    const day = dayIdentity(item.timestamp);
    if (day.key !== previousDayKey) {
      timelineItems.push({
        id: "day:" + day.key,
        kind: "day",
        timestamp: item.timestamp,
        label: day.label,
      });
      previousDayKey = day.key;
    }
    const duplicateIndex = keyCounts.get(item.baseId) || 0;
    keyCounts.set(item.baseId, duplicateIndex + 1);
    timelineItems.push({
      ...item,
      id: duplicateIndex ? item.baseId + ":duplicate-" + duplicateIndex : item.baseId,
      duplicateIndex,
    });
  }
  return {
    searchActive,
    query,
    searchMessageCount: sourceMessages.length,
    searchLoading: search.loading === true,
    searchHasMore: search.hasMore === true,
    visibleMessages,
    visibleDirectorDecisions,
    loadedMessageIds,
    timelineItems,
    recordCount: records.length,
    sourceRecordCount,
    omittedRecordCount,
    projectionLimited: omittedRecordCount > 0,
  };
}
