import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  CHAT_TIMELINE_CITATION_LIMIT,
  CHAT_TIMELINE_DECISION_LIMIT,
  CHAT_TIMELINE_MEMBER_LIMIT,
  CHAT_TIMELINE_MESSAGE_LIMIT,
  CHAT_TIMELINE_TRANSIENT_ERROR_LIMIT,
  buildTimelineCitationPresentation,
  buildTimelineMemberIndex,
  buildChatTimelinePresentation,
  chatInitials,
  formatTimelineTime,
  normalizeTimelineCitations,
  normalizeTransientErrors,
  safeAvatarColor,
} from "../src/chatTimelineView.js";

test("timeline presentation safely orders messages, decisions, day dividers, and duplicate ids", () => {
  const view = buildChatTimelinePresentation({
    messages: [
      { id: "same", created_at: "2026-08-21T10:00:00+08:00", content: "一" },
      { id: "same", created_at: "2026-08-21T10:00:00+08:00", content: "二" },
      null,
    ],
    directorDecisions: [{
      id: "d1",
      sequence_no: 1,
      created_at: "2026-08-21T10:00:00+08:00",
    }],
  });

  assert.equal(view.recordCount, 4);
  assert.deepEqual(
    view.timelineItems.map((item) => item.kind),
    ["day", "message", "day", "director", "message", "message"],
  );
  assert.notEqual(view.timelineItems[4].id, view.timelineItems[5].id);
  assert.equal(new Set(view.timelineItems.map((item) => item.id)).size, view.timelineItems.length);
});

test("blank search stays inactive and malformed optional collections remain empty", () => {
  const view = buildChatTimelinePresentation({
    messages: [{ id: "m1", created_at: "invalid" }],
    directorDecisions: "not-an-array",
    searchState: { query: "   ", messages: null },
  });
  assert.equal(view.searchActive, false);
  assert.equal(view.recordCount, 1);
  assert.equal(formatTimelineTime("invalid"), "时间未知");
  assert.deepEqual(normalizeTimelineCitations({ length: 1 }), []);
  assert.deepEqual(normalizeTransientErrors([null, "bad"]), []);
});

test("initials and avatar colors reject unsafe display inputs", () => {
  assert.equal(chatInitials("研究员"), "研究");
  assert.equal(chatInitials("😀研究"), "😀研");
  assert.equal(chatInitials(42), "AI");
  assert.equal(safeAvatarColor("#1a2b3c"), "#1a2b3c");
  assert.equal(safeAvatarColor("url(javascript:bad)"), "#4f6b8a");
});

test("timeline projection bounds collections before sorting and reports omitted records", () => {
  const messages = Array.from(
    { length: CHAT_TIMELINE_MESSAGE_LIMIT + 2 },
    (_, index) => ({ id: "m-" + index, created_at: "2026-08-21T10:00:00+08:00" }),
  );
  const directorDecisions = Array.from(
    { length: CHAT_TIMELINE_DECISION_LIMIT + 1 },
    (_, index) => ({ id: "d-" + index, created_at: "2026-08-21T10:00:00+08:00" }),
  );
  const view = buildChatTimelinePresentation({ messages, directorDecisions });

  assert.equal(view.recordCount, CHAT_TIMELINE_MESSAGE_LIMIT + CHAT_TIMELINE_DECISION_LIMIT);
  assert.equal(view.sourceRecordCount, messages.length + directorDecisions.length);
  assert.equal(view.omittedRecordCount, 3);
  assert.equal(view.projectionLimited, true);
  assert.equal(view.visibleMessages[0].id, "m-2");
});

test("citation, member, and transient error projections expose bounded deterministic views", () => {
  const citationView = buildTimelineCitationPresentation(
    Array.from({ length: CHAT_TIMELINE_CITATION_LIMIT + 3 }, (_, index) => ({ id: "c-" + index })),
  );
  const memberIndex = buildTimelineMemberIndex(
    Array.from({ length: CHAT_TIMELINE_MEMBER_LIMIT + 1 }, (_, index) => ({ id: "member-" + index })),
  );
  const errors = normalizeTransientErrors(
    Array.from(
      { length: CHAT_TIMELINE_TRANSIENT_ERROR_LIMIT + 2 },
      () => ({ id: "same", message: "失败" }),
    ),
  );

  assert.equal(citationView.citations.length, CHAT_TIMELINE_CITATION_LIMIT);
  assert.equal(citationView.hiddenCount, 3);
  assert.equal(memberIndex.indexedCount, CHAT_TIMELINE_MEMBER_LIMIT);
  assert.equal(memberIndex.projectionLimited, true);
  assert.equal(errors.length, CHAT_TIMELINE_TRANSIENT_ERROR_LIMIT);
  assert.equal(new Set(errors.map((error) => error.id)).size, errors.length);
});

test("timeline source keeps anchoring, handler permits, bounded rendering, and narrow layout contracts", () => {
  const source = readFileSync(
    new URL("../src/components/ChatTimeline.jsx", import.meta.url),
    "utf8",
  );
  const styles = readFileSync(new URL("../src/styles/chat-timeline.css", import.meta.url), "utf8");

  assert.match(source, /useLayoutEffect/);
  assert.match(source, /historyAnchorRef/);
  assert.match(source, /scrollHeight - anchor\.scrollHeight/);
  assert.match(source, /safeExternalUrl/);
  assert.match(source, /tabIndex=\{0\}/);
  assert.match(source, /actionRequestRef/);
  assert.match(source, /runTimelineAction/);
  assert.match(source, /item\.duplicateIndex === 0/);
  assert.match(source, /timeline-integrity-ledger/);
  assert.match(source, /citationView\.hiddenCount/);
  assert.match(source, /const \[isAwayFromBottom, setIsAwayFromBottom\] = useState\(false\)/);
  assert.match(source, /pinnedToBottomRef\.current !== pinnedToBottom[\s\S]*setIsAwayFromBottom\(!pinnedToBottom\)/);
  assert.match(source, /!searchActive && isAwayFromBottom/);
  assert.match(source, /data-new-messages=\{hasNewBelow \? "true" : "false"\}/);
  assert.match(source, /hasNewBelow \? "有新消息" : "回到最新"/);
  assert.match(source, /element\.scrollTop = Math\.max\(0, element\.scrollHeight - element\.clientHeight\)/);
  assert.match(source, /element\.focus\(\{ preventScroll: true \}\)/);
  assert.match(source, /element\.style\.scrollBehavior = "auto"/);
  assert.match(source, /globalThis\.requestAnimationFrame\(restoreScrollBehavior\)/);
  assert.match(styles, /overflow-x:\s*hidden/);
  assert.match(styles, /env\(safe-area-inset-bottom\)/);
  assert.match(styles, /\.new-message-button\s*\{[\s\S]*min-height:\s*44px;/);
  assert.match(styles, /\.new-message-button\.return-only\s*\{[\s\S]*background:/);
  assert.match(styles, /\.new-message-button\.has-new\s*\{[\s\S]*box-shadow:/);
  assert.match(styles, /@media \(forced-colors: active\)/);
  assert.match(styles, /@media \(max-width: 430px\)/);
});
