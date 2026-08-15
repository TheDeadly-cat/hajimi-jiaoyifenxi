import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  convergenceAnnouncementText,
  meetingReadinessAnnouncementText,
  nextChatAnnouncementState,
  storageAcceptanceAnnouncementText,
} from "../src/liveRegionAnnouncements.js";

const chatSource = readFileSync(
  new URL("../src/components/ChatTimeline.jsx", import.meta.url),
  "utf8",
);
const convergenceSource = readFileSync(
  new URL("../src/components/ConvergenceCard.jsx", import.meta.url),
  "utf8",
);
const roomInspectorSource = readFileSync(
  new URL("../src/components/RoomInspector.jsx", import.meta.url),
  "utf8",
);
const storageAcceptanceSource = readFileSync(
  new URL("../src/components/StorageSampleAcceptanceCard.jsx", import.meta.url),
  "utf8",
);
const hostStyles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

function message(id, content = `消息 ${id}`, senderName = "研究员") {
  return {
    id,
    content,
    sender_name: senderName,
    sender_type: "member",
  };
}

function transition(previous, options = {}) {
  return nextChatAnnouncementState(previous, {
    messages: [],
    roomId: "room-a",
    searchActive: false,
    historyLoading: false,
    ...options,
  });
}

test("chat announcer stays silent for initial data, history cycles, search, and room replacement", () => {
  let result = transition(null, { messages: [message("m2")] });
  assert.equal(result.announcement, null);
  assert.equal(result.clear, true);

  result = transition(result.next, { messages: [message("m2")], historyLoading: true });
  assert.equal(result.announcement, null);
  result = transition(result.next, {
    messages: [message("m1"), message("m2")],
    historyLoading: false,
  });
  assert.equal(result.announcement, null);

  result = transition(result.next, {
    messages: [message("m1"), message("m2"), message("m3")],
    searchActive: true,
  });
  assert.equal(result.announcement, null);
  assert.equal(result.clear, true);
  result = transition(result.next, {
    messages: [message("m1"), message("m2"), message("m3")],
    searchActive: false,
  });
  assert.equal(result.announcement, null);

  result = transition(result.next, {
    messages: [message("other")],
    roomId: "room-b",
  });
  assert.equal(result.announcement, null);
});

test("chat announcer emits one concise update only when the final tail message is appended", () => {
  let result = transition(null, { messages: [message("m1")] });
  result = transition(result.next, {
    messages: [message("m1"), message("m2", "  已完成\n证据核验。  ", "审计员")],
  });
  assert.deepEqual(result.announcement, {
    id: "m2",
    text: "新消息，审计员：已完成 证据核验。",
  });

  result = transition(result.next, {
    messages: [message("m1"), message("m2", "已完成证据核验。", "审计员")],
  });
  assert.equal(result.announcement, null);
});

test("a new tail arriving during older-history loading is announced once", () => {
  let result = transition(null, { messages: [message("m1"), message("m2")] });
  result = transition(result.next, {
    messages: [message("m0"), message("m1"), message("m2")],
    historyLoading: true,
  });
  assert.equal(result.announcement, null);

  result = transition(result.next, {
    messages: [message("m0"), message("m1"), message("m2"), message("m3", "新尾消息")],
    historyLoading: true,
  });
  assert.equal(result.announcement?.id, "m3");
  assert.match(result.announcement?.text || "", /新尾消息/);

  result = transition(result.next, {
    messages: [message("m0"), message("m1"), message("m2"), message("m3", "新尾消息")],
    historyLoading: false,
  });
  assert.equal(result.announcement, null);
});

test("chat announcer handles the first final message after an initialized empty room", () => {
  let result = transition(null, { messages: [] });
  result = transition(result.next, { messages: [message("m1", "第一条完整消息")] });
  assert.equal(result.announcement?.text, "新消息，研究员：第一条完整消息");
});

test("chat announcement is bounded instead of reading an entire long response", () => {
  let result = transition(null, { messages: [message("m1")] });
  result = transition(result.next, {
    messages: [message("m1"), message("m2", "证".repeat(200))],
  });
  assert.match(result.announcement?.text || "", /^新消息，研究员：证{120}…$/);
});

test("convergence announcer reports one semantic status plus the first blocker or next action", () => {
  assert.equal(convergenceAnnouncementText(null), "收敛状态：正在计算。");
  assert.equal(convergenceAnnouncementText({
    can_host_finish: false,
    can_present_candidate_best: false,
    blockers: [{ title: "  证据\n截止时间缺失  " }, { title: "不应播报第二项" }],
    next_actions: ["不应优先播报"],
  }), "收敛状态：尚未达到收敛条件。首要阻塞：证据 截止时间缺失。");
  assert.equal(convergenceAnnouncementText({
    can_host_finish: true,
    can_present_candidate_best: false,
    blockers: [],
    next_actions: ["交由用户复核"],
  }), "收敛状态：讨论可结束，等待用户复核。下一步：交由用户复核。");
});

test("non-semantic convergence counter updates keep the live text stable", () => {
  const base = {
    can_host_finish: false,
    can_present_candidate_best: false,
    blockers: [{ title: "仍缺反证" }],
    next_actions: ["补充反证"],
  };
  assert.equal(
    convergenceAnnouncementText({ ...base, discussion_gate: { successful_member_count: 1 } }),
    convergenceAnnouncementText({ ...base, discussion_gate: { successful_member_count: 3 } }),
  );
  assert.notEqual(
    convergenceAnnouncementText(base),
    convergenceAnnouncementText({ ...base, blockers: [{ title: "仍缺官方来源" }] }),
  );
});

test("convergence status also bounds untrusted blocker copy", () => {
  const text = convergenceAnnouncementText({
    can_host_finish: false,
    can_present_candidate_best: false,
    blockers: [{ title: "阻".repeat(200) }],
  });
  assert.match(text, /^收敛状态：尚未达到收敛条件。首要阻塞：阻{120}…。$/);
});

test("room readiness and sample acceptance announce one bounded semantic summary", () => {
  assert.equal(meetingReadinessAnnouncementText({
    workflowReady: true,
    marketRequired: true,
    marketReady: false,
    providerStatus: "checking",
    reason: "等待只读行情。",
  }), "房间就绪状态：角色已覆盖；Futu 未就绪；模型检查中。 首要阻塞：等待只读行情。");
  assert.equal(storageAcceptanceAnnouncementText({
    stateLabel: "等待新轮次",
    blockers: [{ title: "行情快照未就绪。" }, { title: "不应播报" }],
    nextActions: ["不应优先播报"],
  }), "样板验收状态：等待新轮次。首要阻塞：行情快照未就绪。");
});

test("components expose one atomic polite status each and no live large container", () => {
  assert.equal((chatSource.match(/role="status"/g) || []).length, 1);
  assert.equal((convergenceSource.match(/role="status"/g) || []).length, 1);
  assert.equal((chatSource.match(/aria-live="polite"/g) || []).length, 1);
  assert.equal((convergenceSource.match(/aria-live="polite"/g) || []).length, 1);
  assert.equal((chatSource.match(/aria-atomic="true"/g) || []).length, 1);
  assert.equal((convergenceSource.match(/aria-atomic="true"/g) || []).length, 1);
  assert.doesNotMatch(chatSource, /className="chat-timeline"[^>]*aria-live/);
  assert.doesNotMatch(convergenceSource, /convergence-section"[^>]*aria-live/);
  assert.doesNotMatch(convergenceSource, /project-workspace-focus"[^>]*aria-live/);
  assert.doesNotMatch(roomInspectorSource, /meeting-readiness[^>]*aria-live/);
  assert.doesNotMatch(storageAcceptanceSource, /storage-sample-acceptance-card[^>]*aria-live/);
  assert.match(roomInspectorSource, /meetingReadinessAnnouncementText\(\{[\s\S]*role="status"|role="status"[\s\S]*meetingReadinessAnnouncementText\(\{/);
  assert.match(storageAcceptanceSource, /storageAcceptanceAnnouncementText\(model\)/);
  assert.match(hostStyles, /\.screen-reader-announcer\s*\{[\s\S]*clip-path:\s*inset\(50%\);[\s\S]*white-space:\s*nowrap;/);
  assert.doesNotMatch(`${chatSource}\n${convergenceSource}`, /style=\{SCREEN_READER_ONLY_STYLE\}/);
});

test("chat accessibility change preserves reduced-motion scroll behavior", () => {
  assert.match(chatSource, /import \{ preferredScrollBehavior \} from "\.\.\/motionPreferences"/);
  assert.equal((chatSource.match(/behavior:\s*preferredScrollBehavior\(\)/g) || []).length, 2);
});
