import assert from "node:assert/strict";
import test from "node:test";

import {
  emptyMessageSearchState,
  mergeMessageSearchPage,
  mergeUniqueHistoryRecords,
  messageHistoryStateFromSnapshot,
} from "../src/messageHistory.js";

test("history records are deduplicated and chronologically ordered", () => {
  const records = mergeUniqueHistoryRecords(
    [
      { id: "message-b", created_at: 20, content: "current" },
      { id: "message-c", created_at: 30 },
    ],
    [
      { id: "message-a", created_at: 10 },
      { id: "message-b", created_at: 20, content: "decorated" },
    ],
  );

  assert.deepEqual(records.map((record) => record.id), [
    "message-a",
    "message-b",
    "message-c",
  ]);
  assert.equal(records[1].content, "decorated");
});

test("room snapshot metadata initializes a bounded history cursor", () => {
  assert.deepEqual(messageHistoryStateFromSnapshot({
    room: { id: "room-a" },
    message_history: { has_more: true, next_cursor: "opaque-cursor" },
  }), {
    roomId: "room-a",
    nextCursor: "opaque-cursor",
    hasMore: true,
    loading: false,
    error: "",
  });
  assert.equal(messageHistoryStateFromSnapshot({
    room: { id: "room-b" },
    message_history: { has_more: true, next_cursor: "" },
  }).hasMore, false);
});

test("search pages append without duplicates and preserve room identity", () => {
  const initial = {
    ...emptyMessageSearchState("room-a"),
    query: "risk",
  };
  const first = mergeMessageSearchPage(initial, {
    messages: [{ id: "m2", created_at: 20 }],
    director_decisions: [{ id: "d2", created_at: 21 }],
    next_cursor: "page-2",
    has_more: true,
  });
  const second = mergeMessageSearchPage(first, {
    messages: [
      { id: "m1", created_at: 10 },
      { id: "m2", created_at: 20 },
    ],
    director_decisions: [
      { id: "d1", created_at: 11 },
      { id: "d2", created_at: 21 },
    ],
    next_cursor: "",
    has_more: false,
  }, { append: true });

  assert.equal(second.roomId, "room-a");
  assert.deepEqual(second.messages.map((message) => message.id), ["m1", "m2"]);
  assert.deepEqual(second.directorDecisions.map((decision) => decision.id), ["d1", "d2"]);
  assert.equal(second.hasMore, false);
});
