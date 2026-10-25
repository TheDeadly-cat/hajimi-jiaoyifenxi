import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { createServer } from "vite";

const vite = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)), appType: "custom", logLevel: "silent", server: { hmr: false, middlewareMode: true } });
const dom = new JSDOM("<!doctype html><body></body>", { pretendToBeVisual: true, url: "http://news-workspace.test/" });
for (const key of ["window", "document", "navigator", "HTMLElement", "Node", "Event", "KeyboardEvent"]) Object.defineProperty(globalThis, key, { configurable: true, value: key === "window" ? dom.window : dom.window[key] });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const { createRoot } = await import("react-dom/client");
const { NewsWorkspace } = await vite.ssrLoadModule("/src/components/NewsWorkspace.jsx");
const originalFetch = globalThis.fetch;
const source = { id: "fed", name: "合成官方来源", enabled: true, status: "ok", last_success_at: "2026-10-15T02:00:00Z" };
const availableModels = [
  { provider: "doubao", model: "doubao-seed-2-1-pro-260915", label: "豆包" },
  { provider: "deepseek", model: "deepseek-flash", label: "DeepSeek Flash" },
  { provider: "deepseek", model: "deepseek-v4-pro", label: "DeepSeek Pro" },
];
const fixture = (id = "a", extra = {}) => ({ id, title: `合成消息 ${id}`, source_id: "fed", source_name: "合成官方来源", url: "https://www.federalreserve.gov/newsevents/pressreleases/example.htm", published_at: "2026-10-15T01:00:00Z", discovered_at: "2026-10-15T02:00:00Z", read: false, category: "macro", category_label: "宏观", importance: "attention", summary: "合成原文中的本机摘要。", excerpt: "合成正文摘录。", keywords: ["利率"], limitations: ["只覆盖公开摘要"], analysis_mode: "local_rules", ...extra });
let host, root, client, calls, currentOverview, records;
const buttons = () => [...host.querySelectorAll("button")];
const button = (label) => buttons().find((entry) => entry.textContent.trim() === label);
async function click(target) { assert.ok(target); await act(async () => target.click()); }
async function render() { await act(async () => root.render(React.createElement(NewsWorkspace, { apiClient: client, onOpenRooms: () => { calls.rooms++; } }))); }
async function input(target, value) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, "value").set.call(target, value);
    target.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
async function chooseModel(value) {
  await act(async () => {
    const select = host.querySelector('select[aria-label="新闻分析模型"]');
    select.value = value; select.dispatchEvent(new Event("change", { bubbles: true }));
  });
}
const aiCheckbox = (text) => [...host.querySelectorAll(".news-ai-settings label")].find((entry) => entry.textContent.includes(text)).querySelector('input[type="checkbox"]');
function deferred() { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; }

test.beforeEach(() => {
  records = [];
  currentOverview = { enabled: false, running: true, refresh_pending: false, last_success_at: null, next_poll_at: null, counts: { total: 0, last_24h: 0, unread: 0 }, sources: [source], categories: [{ id: "macro", label: "宏观" }], ai: { configured: false, automatic: false, max_calls: 36, budget_cny: 3, calls_reserved: 0, reserved_cny: 0 } };
  currentOverview.ai = { ...currentOverview.ai, provider: "doubao", model: availableModels[0].model, available_models: availableModels, busy: false };
  calls = { lists: [], refresh: 0, control: [], read: [], ai: [], rooms: 0, fetch: 0 };
  client = {
    newsOverview: async () => ({ ok: true, overview: currentOverview }),
    newsItems: async (params) => { calls.lists.push(params); return { ok: true, items: [...records], total: records.length }; },
    newsItem: async (id) => ({ ok: true, item: records.find((entry) => entry.id === id) }),
    newsControl: async (enabled) => { calls.control.push(enabled); currentOverview = { ...currentOverview, enabled }; return { ok: true, overview: currentOverview }; },
    newsRefresh: async () => { calls.refresh++; return { ok: true, overview: { ...currentOverview, refresh_pending: true } }; },
    newsMarkRead: async (id) => { calls.read.push(id); return { ok: true, item: { ...records.find((entry) => entry.id === id), read: true } }; },
    newsAIConfigure: async (payload) => { calls.ai.push(payload); currentOverview.ai = { ...currentOverview.ai, configured: true, provider: payload.provider, model: payload.model, automatic: payload.automatic }; return { ok: true, ai: currentOverview.ai }; },
    newsAIStatus: async () => ({ ok: true, ai: currentOverview.ai }),
    newsAIClear: async () => ({ ok: true, ai: { ...currentOverview.ai, configured: false } }),
    newsAIAnalyze: async (id) => { calls.ai.push(id); return { ok: true, ai_analysis: { status: "queued" } }; },
  };
  globalThis.fetch = async () => { calls.fetch++; throw new Error("No network permitted in this synthetic DOM test"); };
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
test.afterEach(async () => { await act(async () => root.unmount()); assert.equal(calls.fetch, 0); document.body.replaceChildren(); globalThis.fetch = originalFetch; });
test.after(async () => { dom.window.close(); await vite.close(); });

test("empty entry offers real collection controls and never fabricates a news item or AI result", async () => {
  await render();
  assert.match(host.textContent, /还没有抓取到消息/);
  assert.equal(host.querySelectorAll(".news-item").length, 0);
  assert.equal(button("立即检查").disabled, false, "a live worker is not an in-flight fetch");
  assert.match(host.textContent, /AI 未启用/);
  assert.equal(calls.refresh, 0); assert.equal(calls.ai.length, 0);
  await click(button("讨论房间")); assert.equal(calls.rooms, 1);
  await click(button("立即检查")); assert.equal(calls.refresh, 1);
  assert.match(host.textContent, /已安排立即检查/);
});

test("control stays pending until acknowledgement and a failed request does not pretend collection started", async () => {
  const pending = deferred(); client.newsControl = () => pending.promise;
  await render(); await click(button("开始持续抓取"));
  assert.equal(button("开始持续抓取").disabled, true);
  assert.match(host.querySelector(".news-runtime-primary").textContent, /已暂停/);
  await act(async () => pending.resolve({ ok: true, overview: { ...currentOverview, enabled: true } }));
  assert.ok(button("暂停抓取"));
  client.newsControl = async () => { throw new Error("合成暂停失败"); };
  await click(button("暂停抓取"));
  assert.match(host.querySelector("[role=alert]").textContent, /合成暂停失败/);
  assert.ok(button("暂停抓取"), "failed pause preserves authoritative enabled state");
});

test("important and unread filters are sent to the server rather than filtering only the first page", async () => {
  await render(); await click(button("值得关注"));
  assert.equal(calls.lists.at(-1).importance, "attention");
  await click(button("未读")); assert.equal(calls.lists.at(-1).unread, true);
  const field = host.querySelector('.news-filters select');
  await act(async () => { field.value = "fed"; field.dispatchEvent(new Event("change", { bubbles: true })); });
  assert.equal(calls.lists.at(-1).source, "fed");
});

test("late detail responses cannot overwrite the selected item and closing restores focus", async () => {
  records = [fixture("a"), fixture("b")]; const first = deferred();
  client.newsItem = async (id) => id === "a" ? first.promise : { item: fixture("b") };
  await render(); const [a, b] = host.querySelectorAll(".news-item");
  await click(a); await click(b);
  await act(async () => first.resolve({ item: fixture("a") }));
  assert.equal(host.querySelector(".news-detail-body h2").textContent, "合成消息 b");
  assert.equal(document.activeElement, host.querySelector(".news-detail-body h2"));
  await click(button("返回列表")); assert.equal(document.activeElement, b);
  assert.equal(host.querySelector(".news-reader").classList.contains("news-detail-open"), false);
});

test("source text stays text, unsafe original links are absent, and reading is saved only on explicit action", async () => {
  records = [fixture("a", { title: "<img src=x onerror=alert(1)>", url: "javascript:alert(1)" })];
  await render(); await click(host.querySelector(".news-item"));
  assert.equal(host.querySelector(".news-detail-body img"), null);
  assert.equal(host.querySelector(".news-original"), null);
  assert.equal(calls.read.length, 0);
  assert.match(host.textContent, /未使用 AI/);
  await click(button("标为已读")); assert.deepEqual(calls.read, ["a"]); assert.equal(button("已读").disabled, true);
});

test("AI setup requires explicit budget acknowledgement and clears its password field immediately", async () => {
  await render(); const key = host.querySelector('input[type="password"]');
  assert.equal(key.autocomplete, "off");
  assert.equal(host.querySelectorAll('input[type="checkbox"]')[0].checked, false);
  await input(key, "synthetic-test-value-not-a-key");
  assert.equal(button("确认额度并启用").disabled, true);
  await click(host.querySelectorAll('input[type="checkbox"]')[1]);
  const pending = deferred(); client.newsAIConfigure = (payload) => { calls.ai.push(payload); return pending.promise; };
  await act(async () => host.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  assert.equal(key.value, "");
  assert.deepEqual(calls.ai, [{ api_key: "synthetic-test-value-not-a-key", provider: "doubao", model: "doubao-seed-2-1-pro-260915", automatic: false, acknowledge_budget: true }]);
  assert.ok(!host.textContent.includes("synthetic-test-value-not-a-key"));
  await act(async () => pending.resolve({ ai: { configured: true, automatic: false } }));
});

test("model selection defaults to Doubao and clears the key, acknowledgement and automatic option before any request", async () => {
  await render();
  const select = host.querySelector('select[aria-label="新闻分析模型"]');
  assert.equal(select.value, "doubao:doubao-seed-2-1-pro-260915");
  assert.equal(select.options.length, 3);
  assert.equal(select.closest("label").querySelector("span").textContent, "新闻分析模型");
  await input(host.querySelector('input[type="password"]'), "synthetic-doubao-value");
  await click(aiCheckbox("我确认")); await click(aiCheckbox("自动分析"));
  await chooseModel("deepseek:deepseek-flash");
  assert.equal(host.querySelector('input[type="password"]').value, "");
  assert.equal(host.querySelector('input[type="password"]').type, "password");
  assert.match(host.querySelector(".news-ai-key").textContent, /DeepSeek API key/);
  assert.equal(aiCheckbox("我确认").checked, false);
  assert.equal(aiCheckbox("自动分析").checked, false);
  assert.equal(button("确认额度并启用").disabled, true);
  await act(async () => host.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  assert.deepEqual(calls.ai, []);
  assert.match(host.querySelector(".news-ai-settings").textContent, /共享近 24 小时最多 36 次、3 元/);
  assert.match(host.querySelector(".news-ai-budget-help").textContent, /10 元／美元.*不是实时汇率.*不是供应商账单的硬上限/);
});

test("DeepSeek configuration sends the explicitly selected model once and locks selection while the reply is pending", async () => {
  await render(); await chooseModel("deepseek:deepseek-v4-pro");
  await input(host.querySelector('input[type="password"]'), "synthetic-deepseek-value");
  await click(aiCheckbox("我确认"));
  const request = deferred(); client.newsAIConfigure = (payload) => { calls.ai.push(payload); return request.promise; };
  await act(async () => host.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  assert.deepEqual(calls.ai, [{ api_key: "synthetic-deepseek-value", provider: "deepseek", model: "deepseek-v4-pro", automatic: false, acknowledge_budget: true }]);
  assert.equal(host.querySelector('select[aria-label="新闻分析模型"]').disabled, true);
  assert.equal(host.querySelector('input[type="password"]').value, "");
  assert.equal(aiCheckbox("我确认").checked, false);
  currentOverview.ai = { ...currentOverview.ai, configured: true, provider: "deepseek", model: "deepseek-v4-pro" };
  await act(async () => request.resolve({ ai: currentOverview.ai }));
  assert.match(host.querySelector(".news-ai-configured").textContent, /当前启用：DeepSeek Pro/);
  assert.equal(host.querySelector('select[aria-label="新闻分析模型"]').disabled, false);
});

test("a draft model change leaves the active analysis route and shared usage unchanged until confirmation", async () => {
  records = [fixture()];
  currentOverview.ai = { ...currentOverview.ai, configured: true, calls_reserved: 12, reserved_cny: 0.8 };
  await render(); await click(host.querySelector(".news-item"));
  await chooseModel("deepseek:deepseek-flash");
  assert.match(host.querySelector("#news-ai-model-help").textContent, /当前仍启用豆包/);
  assert.ok(button("用豆包分析这条"));
  assert.equal(button("确认额度并切换").disabled, true);
  assert.match(host.querySelector(".news-ai-configured").textContent, /已预约 12 \/ 36 次.*0.80 \/ 3.00 元/);
  assert.deepEqual(calls.ai, []);
});

test("saved analysis identity belongs to its original model and selecting another model does not replay it", async () => {
  records = [fixture("a", { ai_analysis: { status: "completed", provider: "doubao", model: "doubao-seed-2-1-pro-260915", summary: "旧模型保存的摘要" } })];
  currentOverview.ai = { ...currentOverview.ai, configured: true, provider: "deepseek", model: "deepseek-flash" };
  await render(); await click(host.querySelector(".news-item"));
  assert.match(host.querySelector(".news-ai-reading h3").textContent, /豆包 · 分析记录/);
  assert.match(host.querySelector(".news-ai-provenance").textContent, /doubao-seed-2-1-pro-260915.*不会自动重新分析或扣费/);
  assert.doesNotMatch(host.querySelector(".news-ai-reading h3").textContent, /DeepSeek/);
  await chooseModel("deepseek:deepseek-v4-pro");
  assert.equal(host.querySelector(".news-ai-reading button"), null);
  assert.match(host.querySelector(".news-ai-reading").textContent, /旧模型保存的摘要/);
  assert.deepEqual(calls.ai, []);
});

test("legacy saved model identifies its provider while missing saved identity stays unknown", async () => {
  records = [fixture("a", { ai_analysis: { status: "completed", model: "deepseek-v4-pro", summary: "已存Pro摘要" } }), fixture("b", { ai_analysis: { status: "completed", summary: "未记录模型的摘要" } })];
  currentOverview.ai.configured = true;
  await render(); await click(host.querySelectorAll(".news-item")[0]);
  assert.match(host.querySelector(".news-ai-reading h3").textContent, /DeepSeek Pro/);
  await click(host.querySelectorAll(".news-item")[1]);
  assert.match(host.querySelector(".news-ai-reading h3").textContent, /来源未记录/);
  assert.doesNotMatch(host.querySelector(".news-ai-reading h3").textContent, /豆包/);
  assert.deepEqual(calls.ai, []);
});

test("busy analysis prevents model changes without clearing or replacing the confirmed route", async () => {
  currentOverview.ai = { ...currentOverview.ai, configured: true, provider: "deepseek", model: "deepseek-flash", busy: true };
  await render();
  const select = host.querySelector('select[aria-label="新闻分析模型"]');
  assert.equal(select.disabled, true);
  await chooseModel("doubao:doubao-seed-2-1-pro-260915");
  assert.match(host.querySelector(".news-ai-configured").textContent, /当前启用：DeepSeek Flash/);
  assert.equal(host.querySelector(".news-ai-settings form"), null);
  assert.deepEqual(calls.ai, []);
});

test("a mismatched configuration reply does not claim the selected DeepSeek route is active", async () => {
  await render(); await chooseModel("deepseek:deepseek-flash");
  await input(host.querySelector('input[type="password"]'), "synthetic-deepseek-value");
  await click(aiCheckbox("我确认"));
  client.newsAIConfigure = async (payload) => { calls.ai.push(payload); return { ai: { configured: true, provider: "doubao", model: "doubao-seed-2-1-pro-260915" } }; };
  await act(async () => host.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  assert.match(host.textContent, /所选分析路线尚未确认/);
  assert.doesNotMatch(host.querySelector(".news-notice").textContent, /已启用/);
  assert.equal(calls.ai.length, 1);
  assert.equal(host.querySelector('input[type="password"]').value, "");
  assert.equal(aiCheckbox("我确认").checked, false);
});

test("manual AI dispatch is explicit and its queued state is not shown as a completed analysis", async () => {
  records = [fixture()]; currentOverview.ai.configured = true;
  await render(); await click(host.querySelector(".news-item"));
  assert.equal(calls.ai.length, 0);
  await click(button("用豆包分析这条"));
  assert.deepEqual(calls.ai, ["a"]);
  assert.match(host.querySelector(".news-ai-reading").textContent, /已加入分析队列/);
  assert.doesNotMatch(host.querySelector(".news-ai-reading").textContent, /分析已完成/);
});

test("clearing a queued request unlocks model setup only after polling confirms no result, no key and no active analysis", async () => {
  const originalInterval = window.setInterval;
  let poll;
  window.setInterval = (callback, milliseconds, ...args) => { if (milliseconds === 2500) { poll = callback; return 999999; } return originalInterval(callback, milliseconds, ...args); };
  try {
    records = [fixture("a", { ai_analysis: null })]; currentOverview.ai.configured = true;
    client.newsAIClear = async () => { currentOverview.ai = { ...currentOverview.ai, configured: false, busy: false, error: "" }; return { ai: currentOverview.ai }; };
    await render(); await click(host.querySelector(".news-item")); await click(button("用豆包分析这条"));
    assert.match(host.querySelector(".news-ai-reading").textContent, /已加入分析队列/);
    await click(button("清除密钥并停止 AI"));
    assert.equal(host.querySelector('select[aria-label="新闻分析模型"]').disabled, true, "clear acknowledgement alone must not discard an unverified request");
    await act(async () => poll());
    assert.match(host.querySelector(".news-notice").textContent, /排队分析已取消，尚无保存结果/);
    assert.doesNotMatch(host.querySelector(".news-ai-reading").textContent, /已加入分析队列/);
    assert.equal(host.querySelector('select[aria-label="新闻分析模型"]').disabled, false);
    await chooseModel("deepseek:deepseek-flash");
    await input(host.querySelector('input[type="password"]'), "synthetic-new-session-value");
    await click(aiCheckbox("我确认")); await click(button("确认额度并启用"));
    assert.match(host.querySelector(".news-ai-configured").textContent, /当前启用：DeepSeek Flash/);
    assert.deepEqual(calls.ai.map((entry) => typeof entry === "string" ? entry : entry.model), ["a", "deepseek-flash"], "reconfiguration must not automatically resend the cancelled analysis");
    assert.ok(button("用DeepSeek Flash分析这条"));
  } finally { window.setInterval = originalInterval; }
});

test("unconfirmed busy or missing result fields cannot cancel a queued request", async () => {
  const originalInterval = window.setInterval;
  let poll;
  window.setInterval = (callback, milliseconds, ...args) => { if (milliseconds === 2500) { poll = callback; return 999999; } return originalInterval(callback, milliseconds, ...args); };
  try {
    records = [fixture("a", { ai_analysis: null })]; currentOverview.ai.configured = true;
    await render(); await click(host.querySelector(".news-item")); await click(button("用豆包分析这条"));
    currentOverview.ai = { ...currentOverview.ai, configured: false };
    delete currentOverview.ai.busy;
    await act(async () => poll());
    assert.match(host.querySelector(".news-ai-reading").textContent, /已加入分析队列/);
    currentOverview.ai.busy = false;
    delete records[0].ai_analysis;
    await act(async () => poll());
    assert.match(host.querySelector(".news-ai-reading").textContent, /已加入分析队列/);
    assert.equal(host.querySelector('select[aria-label="新闻分析模型"]').disabled, true);
    assert.deepEqual(calls.ai, ["a"]);
  } finally { window.setInterval = originalInterval; }
});

test("a real pending analysis is preserved when clearing returns an error and later unknown results never become a retry", async () => {
  const originalInterval = window.setInterval;
  let poll;
  window.setInterval = (callback, milliseconds, ...args) => { if (milliseconds === 2500) { poll = callback; return 999999; } return originalInterval(callback, milliseconds, ...args); };
  try {
    records = [fixture("a", { ai_analysis: { status: "pending", provider: "doubao", model: "doubao-seed-2-1-pro-260915" } })];
    currentOverview.ai = { ...currentOverview.ai, configured: false, busy: false, error: "合成结果尚未确认" };
    await render(); await click(host.querySelector(".news-item"));
    client.newsItem = async () => ({ item: { ...records[0], ai_analysis: null } });
    await act(async () => poll());
    assert.match(host.querySelector(".news-ai-reading").textContent, /正在分析/);
    assert.equal(host.querySelector(".news-ai-reading button"), null);
    assert.equal(host.querySelector('select[aria-label="新闻分析模型"]').disabled, true);
    client.newsItem = async () => ({ item: { ...records[0], ai_analysis: { status: "unknown", provider: "doubao", model: "doubao-seed-2-1-pro-260915" } } });
    await act(async () => poll());
    assert.match(host.querySelector(".news-ai-reading").textContent, /结果未知，不会自动重试/);
    assert.equal(host.querySelector(".news-ai-reading button"), null);
    assert.deepEqual(calls.ai, []);
  } finally { window.setInterval = originalInterval; }
});

test("unmount cancels list and detail reads without creating extra actions", async () => {
  records = [fixture()]; let listSignal; let detailSignal;
  client.newsItems = async (params) => { listSignal = params.signal; return { items: records, total: 1 }; };
  client.newsItem = (_id, signal) => { detailSignal = signal; return new Promise(() => {}); };
  await render(); await click(host.querySelector(".news-item"));
  await act(async () => root.unmount());
  assert.equal(listSignal.aborted, true); assert.equal(detailSignal.aborted, true);
  assert.equal(calls.refresh, 0); assert.equal(calls.ai.length, 0);
  root = createRoot(host);
});


test("material checks block title-only dispatch even when AI was configured", async () => {
  records = [fixture("a", { excerpt: "", material_quality: { status: "title_only", can_analyze: false, label: "仅有标题，缺少业务摘录", warnings: ["全文尚未读取。"] } })];
  currentOverview.ai.configured = true;
  await render(); await click(host.querySelector(".news-item"));
  const quality = host.querySelector('[aria-label="阅读前核对"]');
  assert.match(quality.textContent, /仅有标题/);
  assert.match(quality.textContent, /不消耗模型额度/);
  assert.equal(button("用豆包分析这条").disabled, true);
  await click(button("用豆包分析这条"));
  assert.equal(calls.ai.length, 0); assert.equal(calls.refresh, 0);
});

test("clock anomalies are visible and evidence stays inert exact source text", async () => {
  const quote = '<img src=x onerror=alert(1)> Future plan remains conditional.';
  records = [fixture("a", { material_quality: { status: "clock_anomaly", can_analyze: true, label: "发布时间待确认", warnings: ["发布时间晚于检查时间，不能视为已发布的新消息。"] }, ai_analysis: { status: "completed", summary: "这是合成摘要。", key_points: ["计划尚未完成"], uncertainties: ["全文未读"], evidence: [{ claim: "计划尚未完成", evidence_field: "excerpt", quote }] } })];
  await render(); await click(host.querySelector(".news-item"));
  assert.match(host.querySelector('[aria-label="阅读前核对"]').textContent, /发布时间待确认/);
  const evidence = host.querySelector(".news-analysis-evidence");
  assert.equal(evidence.open, false);
  await click(evidence.querySelector("summary"));
  assert.equal(evidence.open, true);
  assert.equal(evidence.querySelector("blockquote").textContent, quote);
  assert.equal(evidence.querySelector("img"), null);
  assert.match(evidence.textContent, /仅说明出处/);
  assert.equal(calls.ai.length, 0);
});

test("legacy completed and failed records stay readable without an implicit retry control", async () => {
  records = [fixture("a", { ai_analysis: { status: "completed", legacy: true, summary: "保留的旧版摘要", key_points: ["保留要点"] } }), fixture("b", { ai_analysis: { status: "failed", legacy: true, error: "原失败记录" } })];
  currentOverview.ai.configured = true;
  await render(); await click(host.querySelectorAll(".news-item")[0]);
  assert.match(host.querySelector(".news-ai-reading").textContent, /保留的旧版摘要/);
  assert.match(host.querySelector(".news-analysis-legacy").textContent, /不会自动重新分析或扣费/);
  assert.equal(button("用豆包分析这条"), undefined);
  await click(host.querySelectorAll(".news-item")[1]);
  assert.match(host.querySelector(".news-ai-reading").textContent, /原失败记录/);
  assert.equal(button("用豆包分析这条"), undefined);
  assert.equal(calls.ai.length, 0);
});


test("free insufficient result is not attributed to AI and anomalous time is explicitly unconfirmed", async () => {
  records = [fixture("a", { material_quality: { status: "clock_anomaly", can_analyze: false, label: "发布时间异常，需核对", warnings: [], freshness: { clock_anomaly: true } }, ai_analysis: { status: "material_insufficient", analysis_performed: false, summary: "缺少摘录", key_points: [], uncertainties: [] } })];
  await render(); await click(host.querySelector(".news-item"));
  assert.match(host.querySelector(".news-timestamps").textContent, /来源标注时间（未确认）/);
  assert.match(host.querySelector(".news-ai-reading").textContent, /本机材料检查，未调用 AI/);
  assert.doesNotMatch(host.querySelector(".news-ai-reading").textContent, /AI 根据本条来源内容生成/);
  assert.equal(calls.ai.length, 0);
});


test("a later saved RSS revision distinguishes current version time from first discovery", async () => {
  records = [fixture("a", { material_quality: { status: "sufficient_excerpt", can_analyze: true, label: "订阅条目有更新，全文未核对", warnings: ["首次发现时间不代表本次更新发现时间。"], freshness: { clock_anomaly: false, revision_after_discovery: true } } })];
  await render(); await click(host.querySelector(".news-item"));
  assert.match(host.querySelector(".news-timestamps").textContent, /当前版本标注时间/);
  assert.doesNotMatch(host.querySelector(".news-timestamps").textContent, /未确认/);
  assert.match(host.querySelector('[aria-label="阅读前核对"]').textContent, /订阅条目有更新/);
  assert.equal(calls.ai.length, 0);
});

test("semantic review preserves raw text without accepting it or offering a paid retry", async () => {
  const rawOutput = '<img src=x onerror=alert(1)> {"summary":"unaccepted synthetic text"}';
  records = [fixture("review", { ai_analysis: { status: "needs_review", provider: "deepseek", model: "deepseek-flash", summary: "MUST_NOT_APPEAR_AS_ACCEPTED", raw_model_output: rawOutput, semantic_review: { issues: [{ code: "statistical_comparison_basis", status: "unknown" }] } } })];
  await render(); await click(host.querySelector(".news-item"));
  const section = host.querySelector(".news-ai-reading");
  assert.match(section.textContent, /待复核，不自动重试/);
  assert.match(section.querySelector('[role="status"]').textContent, /依据不足：来源没有充分支持这项同比或环比口径/);
  assert.ok(!section.textContent.includes("MUST_NOT_APPEAR_AS_ACCEPTED"));
  assert.equal(section.querySelector("img"), null);
  assert.equal(section.querySelector("button"), null);
  const details = section.querySelector("details.news-analysis-raw");
  await click(details.querySelector("summary"));
  assert.equal(details.querySelector("pre").textContent, rawOutput);
  assert.equal(calls.ai.length, 0);
});

test("historic completed results remain readable and do not imply verified facts", async () => {
  records = [fixture("historic", { ai_analysis: { status: "completed", summary: "保留原v3审核内容", legacy: true, legacy_notice: "旧记录未经过v4规则重新检查。", quality_version: "rss_excerpt_reading_v3", freshness: { published_at: "2026-10-01T00:00:00Z", discovered_at: "2026-10-02T00:00:00Z", publication_age: "older_than_24h" }, quality_checked_at: "2026-10-20T00:00:00Z" } })];
  await render(); await click(host.querySelector(".news-item"));
  const section = host.querySelector(".news-ai-reading");
  assert.match(section.textContent, /保留原v3审核内容/);
  assert.match(section.textContent, /模型输出已保存，事实未确认/);
  assert.match(section.textContent, /首次发现/);
  assert.match(section.textContent, /本次审核检查/);
  assert.match(section.textContent, /已超过24小时/);
  assert.match(section.textContent, /旧版分析.*尚未经过新规则/);
  assert.equal(section.querySelector("button"), null);
  assert.equal(calls.ai.length, 0);
});

test("future timestamps remain unconfirmed in deterministic analysis time display", async () => {
  records = [fixture("future", { ai_analysis: { status: "needs_review", raw_model_output: "合成未确认输出", freshness: { published_at: "2026-11-01T00:00:00Z", discovered_at: "2026-10-02T00:00:00Z", clock_anomaly: true }, quality_checked_at: "2026-10-20T00:00:00Z" } })];
  await render(); await click(host.querySelector(".news-item"));
  assert.match(host.querySelector(".news-analysis-clock").textContent, /存在异常.*尚未确认/);
  assert.ok(!host.querySelector(".news-analysis-clock").textContent.includes("不超过24小时"));
  assert.equal(calls.ai.length, 0);
});
