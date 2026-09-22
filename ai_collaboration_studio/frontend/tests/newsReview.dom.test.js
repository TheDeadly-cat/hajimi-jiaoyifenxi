import assert from "node:assert/strict";
import test from "node:test";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { createServer } from "vite";
import { fileURLToPath } from "node:url";

const dom = new JSDOM("<!doctype html><html><body></body></html>", { url: "http://news-review.test/" });
for (const key of ["window", "document", "navigator", "HTMLElement", "Event", "MouseEvent", "Node"]) {
  Object.defineProperty(globalThis, key, { configurable: true, value: key === "window" ? dom.window : dom.window[key] });
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const vite = await createServer({ configLoader: "runner", root: fileURLToPath(new URL("../", import.meta.url)), appType: "custom", logLevel: "silent", server: { middlewareMode: true, hmr: false } });
const { createRoot } = await import("react-dom/client");
const { NewsReview, NewsReviewControl } = await vite.ssrLoadModule("/src/components/NewsReview.jsx");
let root;
const originalFetch = globalThis.fetch;
const fixture = (state = "REVIEWED") => ({ version: "news_event_review_v1", item_id: "source_event_one", state,
  observation_until: 0, importance: { level: "high", reasons: ["经营结果"], targets: ["US.NVDA"] },
  freshness: { published_at: "2026-09-20T12:00:00Z", discovered_at: 1789905900000 },
  reviews: state === "UNREVIEWED" ? [] : [{ id: "news_review_fixture", state, coverage: {
    scope: "主 HTML", paragraph_count: 3, warnings: ["关键事实可能在未读取附件中"], attachment_reading: "not_read" },
    receipt: state === "REVIEWED" ? { result: { summary: "已审核正文陈述", importance: { level: "high", reason: "涉及业绩" },
      facts: [{ claim: "收入增加", quote: "Synthetic revenue increased.", paragraph_id: "document_fixture:p0001" }],
      inferences: [], counterevidence: [], open_questions: ["附件提供了什么信息？"], limitations: ["未独立核验数字"] } } : null,
  }],
});
async function mount(Component, props = {}) {
  const host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  await act(async () => root.render(React.createElement(Component, props)));
  return host;
}
test.afterEach(async () => { if (root) await act(async () => root.unmount()); root = null; document.body.replaceChildren(); globalThis.fetch = originalFetch; });
test.after(async () => { await vite.close(); dom.window.close(); });

test("restored A selects its historical review while B stays in history", async () => {
  const data = fixture();
  const a = { ...data.reviews[0], id: "review_A", document_version_id: "document_A",
    receipt: { result: { ...data.reviews[0].receipt.result, summary: "Restored A assessment" } } };
  const b = { ...a, id: "review_B", document_version_id: "document_B",
    receipt: { result: { ...a.receipt.result, summary: "Historical B assessment" } } };
  data.reviews = [a, b];
  data.current_document_version_id = "document_A";
  data.current_review_id = "review_A";
  data.coverage = { ...a.coverage, scope: "Current A scope" };
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push(options);
    return { ok: true, json: async () => ({ ok: true, news_review: data }) };
  };
  const host = await mount(NewsReview, { itemId: "source_event_one" });
  assert.match(host.textContent, /Current A scope/);
  assert.doesNotMatch(host.textContent, /以下意见使用此前保存的正文版本/);
  const history = [...host.querySelectorAll("details")].find((node) => node.querySelector("summary")?.textContent.includes("其他审核版本"));
  assert.match(history.textContent, /Historical B assessment/);
  assert.doesNotMatch(history.textContent, /Restored A assessment/);
  assert.match(host.textContent, /Restored A assessment/);
  assert.equal(calls.filter((options) => options.method === "POST").length, 0);
});

test("successful review keeps importance, missing attachments, quotes and factual limits separate", async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => { calls.push({ url, options }); return { ok: true, json: async () => ({ ok: true, news_review: fixture() }) }; };
  const host = await mount(NewsReview, { itemId: "source_event_one" });
  for (const text of ["优先关注", "US.NVDA", "发布时间", "发现时间", "附件：未读取", "Synthetic revenue increased.", "事实尚未独立核验", "不表示不存在反证"]) assert.ok(host.textContent.includes(text), text);
  assert.equal(host.querySelector("blockquote").textContent, "Synthetic revenue increased.");
  assert.equal(calls.length, 1);
  assert.notEqual(calls[0].options.method, "POST");
});

test("unknown response remains unknown and offers no retry or approval button", async () => {
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ ok: true, news_review: fixture("UNKNOWN") }) });
  const host = await mount(NewsReview, { itemId: "source_event_one" });
  assert.match(host.textContent, /结果未知 · 不会自动重发/);
  assert.deepEqual([...host.querySelectorAll("button")].map((b) => b.textContent), ["刷新审核状态"]);
  assert.equal(host.querySelector("blockquote"), null);
});

for (const [state, text] of [["DOCUMENT_CANCELLED", "正文读取已取消"], ["WAITING_AUTHORIZATION", "等待新的授权"]]) {
  test(`authorization interruption is visible: ${state}`, async () => {
    globalThis.fetch = async () => ({ ok: true, json: async () => ({ ok: true, news_review: fixture(state) }) });
    const host = await mount(NewsReview, { itemId: "source_event_one" });
    assert.ok(host.querySelector('[role="status"]').textContent.includes(text));
    assert.equal(host.querySelector('[role="alert"]'), null);
    assert.equal(host.querySelector('blockquote'), null);
    assert.deepEqual([...host.querySelectorAll('button')].map((b) => b.textContent), ['刷新审核状态']);
  });
}

test("missing evidence and request failure cannot render as no important messages", async () => {
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ ok: true, news_review: fixture("UNREVIEWED") }) });
  const host = await mount(NewsReview, { itemId: "source_event_one" });
  assert.match(host.textContent, /未读取成功不代表没有重要消息/);
  globalThis.fetch = async () => { throw new Error("synthetic disconnect"); };
  await act(async () => host.querySelector("button").click());
  assert.match(host.querySelector('[role="alert"]').textContent, /当前状态尚未确认/);
});

test("mismatched event identity is never shown", async () => {
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ ok: true, news_review: fixture() }) });
  const host = await mount(NewsReview, { itemId: "source_event_other" });
  assert.match(host.textContent, /记录不匹配/);
  assert.doesNotMatch(host.textContent, /Synthetic revenue increased/);
});

test("pause sends only the pause action and keeps no-new-event limitation visible", async () => {
  const calls = [];
  let paused = false;
  const policy = { expires_at_ms: Date.now() + 60_000, max_document_requests: 6, max_model_calls: 3, spend_limit_cny: "1" };
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    if (options.method === "POST") paused = true;
    return { ok: true, json: async () => ({ ok: true, news_review: { state: paused ? "PAUSED" : "ACTIVE", policy,
      expired: false, documents_reserved: 0, calls_reserved: 0, cost_reserved_cny: "0", events_observed: 0,
      job_counts: {}, observation: "no_new_event_observed", paid_stop_reason: paused ? "operator_pause" : "" } }) };
  };
  const host = await mount(NewsReviewControl);
  assert.match(host.textContent, /尚未观察到新增事件/);
  await act(async () => [...host.querySelectorAll("button")].find((b) => b.textContent.includes("暂停本次")).click());
  const posts = calls.filter((call) => call.options.method === "POST");
  assert.equal(posts.length, 1);
  assert.deepEqual(JSON.parse(posts[0].options.body), { action: "pause" });
  assert.match(host.textContent, /已暂停新采集、正文读取和审核/);
});
