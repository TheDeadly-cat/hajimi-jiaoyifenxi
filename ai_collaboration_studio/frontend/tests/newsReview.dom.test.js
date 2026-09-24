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
    return { ok: true, json: async () => ({ ok: true, news_review: { version: "news_event_review_v1", policy_sha256: "a".repeat(64), state: paused ? "PAUSED" : "ACTIVE", policy: { ...policy, policy_id: "trial" },
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

const controlFixture = (until) => ({ version: "news_event_review_v1", state: "ACTIVE", expired: false,
  policy_sha256: "a".repeat(64), policy: { policy_id: "trial", expires_at_ms: until, max_document_requests: 24, max_model_calls: 3, spend_limit_cny: "1.50" },
  documents_reserved: 0, calls_reserved: 0, cost_reserved_cny: "0", events_observed: 0, job_counts: {}, observation: "no_new_event_observed" });
const ok = (data) => ({ ok: true, json: async () => ({ ok: true, news_review: data }) });
const advance = async (t, ms) => act(async () => { t.mock.timers.tick(ms); });
const epoch = Date.parse("2026-09-24T16:00:00Z");

for (const [label, Component, props, makeData] of [
  ["detail", NewsReview, { itemId: "source_event_one" }, (until) => ({ ...fixture(), observation_until: until })],
  ["control", NewsReviewControl, {}, controlFixture],
]) {
  test(`${label}: transient GET failure recovers, preserves last success and never posts`, async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: epoch });
    const calls = [];
    globalThis.fetch = async (url, options) => {
      calls.push({ url, options });
      if (calls.length === 2) throw new TypeError("temporary disconnect");
      return ok(makeData(epoch + 120_000));
    };
    const host = await mount(Component, props);
    const originalTime = host.querySelector("time").dateTime;
    await advance(t, 10_000);
    assert.match(host.textContent, /退避重试/);
    assert.equal(host.querySelector("time").dateTime, originalTime);
    await advance(t, 1_999); assert.equal(calls.length, 2);
    await advance(t, 1); assert.equal(calls.length, 3);
    assert.equal(host.querySelector('[role="alert"]'), null);
    assert.notEqual(host.querySelector("time").dateTime, originalTime);
    await advance(t, 10_000); assert.equal(calls.length, 4);
    assert.ok(calls.every(({ options }) => !options.method || options.method === "GET"));
  });
  test(`${label}: first-read outages have five bounded retries and manual GET recovery`, async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: epoch });
    const calls = [];
    let recovered = false;
    globalThis.fetch = async (_url, options) => {
      calls.push(options);
      if (!recovered) return { ok: false, status: 503, json: async () => ({ error: "temporarily unavailable" }) };
      return ok(makeData(epoch + 500_000));
    };
    const host = await mount(Component, props);
    for (const delay of [2_000, 4_000, 8_000, 16_000, 30_000]) await advance(t, delay);
    assert.equal(calls.length, 6);
    assert.match(host.textContent, /重试已达上限/);
    await advance(t, 120_000); assert.equal(calls.length, 6);
    recovered = true;
    await act(async () => [...host.querySelectorAll("button")].find((b) => b.textContent.startsWith("刷新")).click());
    assert.equal(calls.length, 7);
    assert.equal(host.querySelector('[role="alert"]'), null);
    assert.ok(calls.every((options) => !options.method || options.method === "GET"));
  });
  test(`${label}: expiry stops retries and aborts a pending GET without applying late results`, async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: epoch });
    let calls = 0, pending, pendingSignal;
    globalThis.fetch = async (_url, options) => {
      calls++;
      if (calls === 1) return ok(makeData(epoch + 15_000));
      pendingSignal = options.signal;
      return new Promise((resolve) => { pending = resolve; });
    };
    const host = await mount(Component, props);
    await advance(t, 10_000);
    await advance(t, 5_000);
    assert.equal(pendingSignal.aborted, true);
    assert.match(host.textContent, /观察窗口已到期/);
    await act(async () => pending(ok(makeData(epoch + 600_000))));
    await advance(t, 120_000);
    assert.equal(calls, 2);
    assert.equal(host.querySelector("time").dateTime, new Date(epoch).toISOString());
  });
  test(`${label}: protocol errors are terminal and unmount cancels retry timers`, async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: epoch });
    let calls = 0;
    globalThis.fetch = async () => { calls++; return ok({ ...makeData(epoch + 120_000), version: "invalid" }); };
    const host = await mount(Component, props);
    assert.match(host.textContent, /已停止自动刷新/);
    await advance(t, 120_000); assert.equal(calls, 1);
    let signal;
    globalThis.fetch = async (_url, options) => { calls++; signal = options.signal; throw new TypeError("offline"); };
    await act(async () => [...host.querySelectorAll("button")].find((b) => b.textContent.startsWith("刷新")).click());
    assert.equal(calls, 2);
    await act(async () => root.unmount()); root = null;
    assert.equal(signal.aborted, true);
    await advance(t, 120_000); assert.equal(calls, 2);
  });
  test(`${label}: abort and integrity errors do not retry`, async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: epoch });
    let calls = 0;
    globalThis.fetch = async () => { calls++; throw new DOMException("cancelled", "AbortError"); };
    const host = await mount(Component, props);
    await advance(t, 120_000); assert.equal(calls, 1);
    globalThis.fetch = async () => { calls++; return { ok: false, status: 500, json: async () => ({ error: "identity failed", code: "policy_identity_mismatch" }) }; };
    await act(async () => [...host.querySelectorAll("button")].find((b) => b.textContent.startsWith("刷新")).click());
    await advance(t, 120_000); assert.equal(calls, 2);
    assert.match(host.textContent, /identity failed/);
  });
  test(`${label}: manual read failure cannot renew an expired retry window`, async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: epoch });
    let calls = 0;
    globalThis.fetch = async () => { calls++; return ok(makeData(epoch + 5_000)); };
    const host = await mount(Component, props);
    await advance(t, 5_000);
    globalThis.fetch = async () => { calls++; throw new TypeError("offline"); };
    await act(async () => [...host.querySelectorAll("button")].find((b) => b.textContent.startsWith("刷新")).click());
    await advance(t, 120_000);
    assert.equal(calls, 2);
    assert.match(host.textContent, /观察窗口已到期/);
  });
}

test("control: a different policy cannot replace the confirmed polling identity", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: epoch });
  let calls = 0;
  globalThis.fetch = async () => ok({ ...controlFixture(epoch + 120_000), policy_sha256: (++calls === 1 ? "a" : "b").repeat(64) });
  const host = await mount(NewsReviewControl);
  await advance(t, 10_000);
  assert.match(host.textContent, /授权身份不匹配/);
  await advance(t, 120_000); assert.equal(calls, 2);
});

test("detail: changing the selected item aborts an old retry and hides its last success", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: epoch });
  const signals = [], calls = [];
  globalThis.fetch = async (url, options) => { calls.push(url); signals.push(options.signal); throw new TypeError("offline"); };
  const host = await mount(NewsReview, { itemId: "source_event_one" });
  await act(async () => root.render(React.createElement(NewsReview, { itemId: "source_event_two" })));
  assert.equal(signals[0].aborted, true);
  await advance(t, 2_000);
  assert.equal(calls.filter((url) => url.includes("source_event_one")).length, 1);
  assert.equal(calls.filter((url) => url.includes("source_event_two")).length, 2);
  assert.equal(host.querySelector("time"), null);
});
