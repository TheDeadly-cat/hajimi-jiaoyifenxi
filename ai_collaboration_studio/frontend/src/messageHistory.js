function recordTimestamp(record) {
  const timestamp = new Date(record?.created_at).getTime();
  return Number.isFinite(timestamp) ? timestamp : 0;
}

export function mergeUniqueHistoryRecords(current = [], incoming = []) {
  const byId = new Map();
  for (const record of [...current, ...incoming]) {
    if (record?.id) byId.set(record.id, record);
  }
  return [...byId.values()].sort((left, right) => (
    recordTimestamp(left) - recordTimestamp(right)
    || (Number(left.sequence_no) || 0) - (Number(right.sequence_no) || 0)
    || String(left.id).localeCompare(String(right.id))
  ));
}

export function messageHistoryStateFromSnapshot(snapshot) {
  const roomId = String(snapshot?.room?.id || "");
  const metadata = snapshot?.message_history || {};
  return {
    roomId,
    nextCursor: String(metadata.next_cursor || ""),
    hasMore: Boolean(metadata.has_more && metadata.next_cursor),
    loading: false,
    error: "",
  };
}

export function emptyMessageSearchState(roomId = "") {
  return {
    roomId: String(roomId || ""),
    query: "",
    messages: [],
    directorDecisions: [],
    nextCursor: "",
    hasMore: false,
    loading: false,
    error: "",
  };
}

export function mergeMessageSearchPage(current, page, { append = false } = {}) {
  const messages = Array.isArray(page?.messages) ? page.messages : [];
  const directorDecisions = Array.isArray(page?.director_decisions)
    ? page.director_decisions
    : [];
  return {
    ...current,
    messages: append
      ? mergeUniqueHistoryRecords(current?.messages, messages)
      : mergeUniqueHistoryRecords([], messages),
    directorDecisions: append
      ? mergeUniqueHistoryRecords(current?.directorDecisions, directorDecisions)
      : mergeUniqueHistoryRecords([], directorDecisions),
    nextCursor: String(page?.next_cursor || ""),
    hasMore: Boolean(page?.has_more && page?.next_cursor),
    loading: false,
    error: "",
  };
}
