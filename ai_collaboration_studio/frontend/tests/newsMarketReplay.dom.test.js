import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { createServer } from "vite";
import { api } from "../src/api.js";

const vite = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)), appType: "custom", logLevel: "silent", server: { hmr: false, middlewareMode: true } });
const dom = new JSDOM("<!doctype html><body></body>", { pretendToBeVisual: true, url: "http://market-replay.test/" });
for (const key of ["window", "document", "navigator", "HTMLElement", "Node", "Event"]) Object.defineProperty(globalThis, key, { configurable: true, value: key === "window" ? dom.window : dom.window[key] });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const { createRoot } = await import("react-dom/client");
const { NewsMarketReplay } = await vite.ssrLoadModule("/src/components/NewsMarketReplay.jsx");
const { NewsMarketContext } = await vite.ssrLoadModule("/src/components/NewsMarketContext.jsx");
const { NewsWorkspace } = await vite.ssrLoadModule("/src/components/NewsWorkspace.jsx");
const originalFetch = globalThis.fetch, originalInterval = window.setInterval, originalClear = window.clearInterval;
let host, root, version, item, client, calls, stored, poll;
const room = { id: "room-a", title: "存储研究室" };
const cutoff = "2020-06-20T04:00:00.000Z";
const button = label => [...host.querySelectorAll("button")].find(value => value.textContent.trim() === label);
const field = label => host.querySelector(`[aria-label="${label}"]`);
async function click(value) { assert.ok(value); await act(async () => value.click()); }
async function fill(value, text) { await act(async () => { Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, "value").set.call(value, text); value.dispatchEvent(new Event("input", { bubbles: true })); }); }
async function choose(value, text) { await act(async () => { value.value = text; value.dispatchEvent(new Event("change", { bubbles: true })); }); }
async function render(extra = {}) { await act(async () => root.render(React.createElement(NewsMarketReplay, { key: version.id, item, version, rooms: [room], apiClient: client, configured: false, initiallyOpen: true, ...extra }))); }
function context(payload = {}) { return { context_id: "historical-context", mode: "historical_replay", article_version_id: version.id, as_of: payload.as_of || cutoff, room_id: payload.room_id || null, round_id: payload.round_id || null, material_refs: payload.material_refs || [], analysis_allowed: true, news: { version_number: version.version_number, version_seen_at: version.version_seen_at, content_status: version.content_status }, evidence: [], consensus: [], market: { status: "missing", rows: [], captured_at: null }, missing: ["尚不能判断是否超预期。", "尚无可用的对应时点行情。"] }; }
const round = (id, day, materialVersion) => ({ id, created_at: Date.parse(`2020-06-${day}T00:00:00Z`), known_at: `2020-06-${day}T01:00:00Z`, materials: [{ id: "material-a", version: materialVersion, title: "已冻结共识原文" }] });
const optionsPage = (rounds, rest = {}) => ({ rounds, scope: "saved_rounds_as_of", as_of: cutoff, total: rounds.length, next_offset: null, has_more: false, skipped_unverified: 0, ...rest });
test.beforeEach(() => {
  version = { id: "v1", item_id: "news-a", version_number: 1, title: "历史公告", version_seen_at: "2020-06-01T00:00:00.123456Z", content_status: "excerpt_only", excerpt: "旧版原文", evidence: {} };
  item = { id: "news-a", title: "当前公告", current_version_id: "v2", market_context: { context_id: "current-never-import" }, market_impact: { status: "completed", facts: ["当前结果不可回填"] } };
  calls = { options: [], prepare: [], list: [], open: [], analyze: [], network: 0, currentReads: 0 }; stored = null; poll = null;
  client = {
    newsMarketOptions: async (roomId, signal, params) => { calls.options.push({ roomId, signal, params }); return optionsPage([round("recent", "19", 3), round("older", "10", 2)]); },
    newsMarketContext: async (id, payload, signal) => { calls.prepare.push({ id, payload, signal }); stored = context(payload); return { market_context: stored, market_impact: null }; },
    newsMarketReplays: async (id, versionId, signal, params) => { calls.list.push({ id, versionId, signal, params }); return { contexts: [{ context_id: "historical-context", article_version_id: version.id, as_of: cutoff, created_at: "2020-06-20T04:01:00Z", room_id: null, round_id: null }], total: 1 }; },
    newsMarketSavedContext: async (id, contextId, signal) => { calls.open.push({ id, contextId, signal }); return { market_context: stored || context(), market_impact: null }; },
    newsMarketAnalyze: async (id, contextId, signal, options) => { calls.analyze.push({ id, contextId, signal, options }); return { market_impact: { status: "queued", context_id: contextId } }; },
    newsItem: async () => { calls.currentReads++; throw new Error("Current item must not be used for a historical result"); },
  };
  globalThis.fetch = async () => { calls.network++; throw new Error("No network in replay fixtures"); };
  window.setInterval = (callback, delay, ...args) => delay === 2500 ? (poll = callback, 98765) : originalInterval.call(window, callback, delay, ...args);
  window.clearInterval = id => { if (id !== 98765) originalClear.call(window, id); };
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
test.afterEach(async () => { await act(async () => root.unmount()); assert.equal(calls.network, 0); assert.equal(calls.currentReads, 0); document.body.replaceChildren(); globalThis.fetch = originalFetch; window.setInterval = originalInterval; window.clearInterval = originalClear; });
test.after(async () => { dom.window.close(); await vite.close(); });

test("no cutoff, latest round, current context or model request is selected by default", async () => {
  await render({ configured: true });
  assert.equal(field("回放截止日期与时间").value, "");
  assert.equal(button("准备历史材料（不调用 AI）").disabled, true);
  assert.doesNotMatch(host.textContent, /current-never-import|当前结果不可回填/);
  assert.deepEqual(calls.options, []); assert.deepEqual(calls.prepare, []); assert.deepEqual(calls.analyze, []);
});

test("explicit exact discovery time prepares a news-only immutable binding without losing sub-millisecond precision", async () => {
  await render(); await click(button("采用此版本首次发现时刻")); await click(button("准备历史材料（不调用 AI）"));
  assert.deepEqual(calls.prepare[0].payload, { room_id: null, round_id: null, material_refs: [], version_id: "v1", as_of: "2020-06-01T00:00:00.123456Z" });
  assert.match(host.querySelector(".news-replay-bound").textContent, /新闻版本 1.*尚不能判断是否超预期/s);
  assert.match(host.textContent, /材料已保存，尚未调用模型/); assert.deepEqual(calls.analyze, []);
});

test("preparing after an empty saved list removes the stale empty claim without an automatic reload", async () => {
  client.newsMarketReplays = async () => { calls.list.push("read"); return { contexts: [], total: 0 }; };
  await render(); await click(button("查看此版本已保存的回放")); assert.match(host.textContent, /此版本尚无已保存的回放材料/);
  await click(button("采用此版本首次发现时刻")); await click(button("准备历史材料（不调用 AI）"));
  assert.ok(host.querySelector(".news-replay-bound")); assert.doesNotMatch(host.textContent, /此版本尚无已保存的回放材料/); assert.equal(calls.list.length, 1);
});

test("response cutoff binding accepts the same precise instant in an explicit UTC offset", async () => {
  client.newsMarketContext = async (id, payload) => ({ market_context: { ...context(payload), as_of: "2020-06-01T08:00:00.123456+08:00" } });
  await render(); await click(button("采用此版本首次发现时刻")); await click(button("准备历史材料（不调用 AI）"));
  assert.ok(host.querySelector(".news-replay-bound")); assert.equal(host.querySelector('[role="alert"]'), null);
});

test("known checkpoint pagination follows next_offset and explicit older-round selection freezes its material version", async () => {
  client.newsMarketOptions = async (roomId, signal, params) => { calls.options.push({ roomId, params }); return params.offset === 0 ? optionsPage([], { total: 52, next_offset: 50, has_more: true, skipped_unverified: 50 }) : optionsPage([round("recent", "19", 3), round("older", "10", 2)], { total: 52 }); };
  await render(); await fill(field("回放截止日期与时间"), "2020-06-20T12:00"); await choose(field("回放资料房间"), "room-a");
  assert.deepEqual(calls.options, []);
  await click(button("读取截止时刻前的已存轮次"));
  assert.match(host.textContent, /本页没有合格轮次，可继续加载/); await click(button("加载更多当时已存轮次"));
  assert.equal(calls.options[1].params.offset, 50); assert.equal(field("回放研究轮次").value, "");
  await choose(field("回放研究轮次"), "older"); await click(host.querySelector('.news-market-replay fieldset input'));
  await click(button("准备历史材料（不调用 AI）"));
  assert.deepEqual(calls.prepare[0].payload, { room_id: "room-a", round_id: "older", material_refs: [{ material_id: "material-a", version: 2 }], version_id: "v1", as_of: cutoff });
  assert.deepEqual(calls.analyze, []);
});

test("unknown, too-early and ambiguous local times cannot prepare a replay", async () => {
  await render(); await fill(field("回放截止日期与时间"), "2020-05-31T12:00");
  assert.equal(button("准备历史材料（不调用 AI）").disabled, true); assert.match(host.textContent, /早于所选版本/);
  await choose(field("回放时区"), "America/New_York"); await fill(field("回放截止日期与时间"), "2020-11-01T01:30");
  assert.match(host.textContent, /夏令时回拨/); assert.equal(button("准备历史材料（不调用 AI）").disabled, true); assert.deepEqual(calls.prepare, []);
});

test("cutoff and room changes abort a pending preparation and a late response cannot restore it", async () => {
  let resolve; client.newsMarketContext = async (id, payload, signal) => { calls.prepare.push({ id, payload, signal }); return new Promise(done => { resolve = done; }); };
  await render(); await click(button("采用此版本首次发现时刻")); await click(button("准备历史材料（不调用 AI）"));
  await choose(field("回放资料房间"), "room-a"); assert.equal(calls.prepare[0].signal.aborted, true);
  await act(async () => resolve({ market_context: context(calls.prepare[0].payload) }));
  assert.equal(host.querySelector(".news-replay-bound"), null);
  assert.equal(field("回放资料房间").value, "room-a");
});

test("changing the selected version cancels its unfinished preparation and ignores the late response", async () => {
  let resolve, oldContext;
  client.newsMarketContext = async (id, payload, signal) => { calls.prepare.push({ id, payload, signal }); oldContext = context(payload); return new Promise(done => { resolve = done; }); };
  await render(); await click(button("采用此版本首次发现时刻")); await click(button("准备历史材料（不调用 AI）"));
  version = { ...version, id: "v2", version_number: 2, version_seen_at: "2020-07-01T00:00:00Z" }; await render();
  assert.equal(calls.prepare[0].signal.aborted, true); await act(async () => resolve({ market_context: oldContext }));
  assert.equal(host.querySelector(".news-replay-bound"), null); assert.match(host.textContent, /当前选择版本 2/); assert.match(host.textContent, /请明确选择回放截止时间/);
});

test("different versions, cutoffs and sub-millisecond substitutions in prepared responses fail closed", async () => {
  for (const patch of [{ article_version_id: "v2" }, { as_of: "2020-06-01T00:00:00.123457Z" }, { mode: "current" }, { room_id: "other-room" }, { material_refs: [{ material_id: "unselected", version: 3 }] }]) {
    client.newsMarketContext = async (id, payload) => ({ market_context: { ...context(payload), ...patch } });
    await render(); await click(button("采用此版本首次发现时刻")); await click(button("准备历史材料（不调用 AI）"));
    assert.equal(host.querySelector(".news-replay-bound"), null); assert.match(host.textContent, /不一致/);
  }
});

test("saved replay list and opening use explicit IDs, never the current latest context", async () => {
  await render(); await click(button("查看此版本已保存的回放"));
  assert.deepEqual(calls.list[0].params, { limit: 50, offset: 0 }); assert.equal(calls.list[0].versionId, "v1");
  await click([...host.querySelectorAll(".news-replay-saved button")].find(value => value.textContent.startsWith("打开截止于")));
  assert.equal(calls.open[0].contextId, "historical-context"); assert.equal(field("回放时区").value, "UTC");
  assert.match(host.querySelector(".news-replay-bound").textContent, /新闻版本 1/); assert.doesNotMatch(host.textContent, /当前结果不可回填/); assert.deepEqual(calls.analyze, []);
});

test("saved quote time preserves its original string and only converts a timezone-qualified timestamp", async () => {
  client.newsMarketContext = async (id, payload) => ({ market_context: { ...context(payload), market: { status: "available", captured_at: cutoff, rows: [{ symbol: "US.MU", last: 100, volume: 1000, updated_at: "2020-06-20T03:59:00Z", market_time: "2020-06-19 23:59:00" }] } } });
  await render(); await fill(field("回放截止日期与时间"), "2020-06-20T12:00"); await click(button("准备历史材料（不调用 AI）"));
  const text = host.querySelector(".news-replay-quotes").textContent;
  assert.match(text, /报价时点（北京时间）.*11:59:00.*2020-06-19 23:59:00（时区未注明）/s); assert.match(text, /100（币种未记录）/);
  const record = host.querySelector(".news-replay-quote-record");
  assert.equal(record.querySelector("h5").textContent, "US.MU");
  assert.equal(record.querySelector("dd.news-replay-number").textContent, "1000");
  assert.equal(record.querySelector("table"), null, "narrow reading uses labeled fields rather than squeezed columns");
});

test("historical analysis requires a separate two-step acknowledgment and polls only the explicit context", async () => {
  await render({ configured: true, routeLabel: "合成测试模型" }); await click(button("采用此版本首次发现时刻")); await click(button("准备历史材料（不调用 AI）"));
  assert.deepEqual(calls.analyze, []); await click(button("核对历史市场分析发送范围")); assert.deepEqual(calls.analyze, []);
  assert.match(host.querySelector(".news-replay-confirm").textContent, /新闻版本 1.*截止.*共享预约预算/s);
  await click(button("暂不发送历史分析")); assert.deepEqual(calls.analyze, []);
  await click(button("核对历史市场分析发送范围")); await click(button("确认分析这个历史时点"));
  assert.equal(calls.analyze.length, 1); assert.deepEqual(calls.analyze[0].options, { acknowledge_historical: true });
  client.newsMarketSavedContext = async (id, contextId) => { calls.open.push({ id, contextId }); return { market_context: stored, market_impact: { context_id: contextId, status: "completed", facts: ["仅截止时点的合成解释"] } }; };
  assert.equal(typeof poll, "function"); await act(async () => poll());
  assert.equal(calls.open[0].contextId, "historical-context"); assert.match(host.textContent, /仅截止时点的合成解释/); assert.equal(calls.analyze.length, 1);
});

test("refreshing saved-round choices clears the old prepared context and its pending analysis confirmation", async () => {
  await render({ configured: true }); await fill(field("回放截止日期与时间"), "2020-06-20T12:00"); await choose(field("回放资料房间"), "room-a");
  await click(button("读取截止时刻前的已存轮次")); await choose(field("回放研究轮次"), "older"); await click(host.querySelector('.news-market-replay fieldset input'));
  await click(button("准备历史材料（不调用 AI）")); await click(button("核对历史市场分析发送范围")); assert.ok(host.querySelector(".news-replay-confirm"));
  const preserved = JSON.stringify(stored);
  await click(button("读取截止时刻前的已存轮次"));
  assert.equal(field("回放研究轮次").value, ""); assert.equal(host.querySelector(".news-replay-confirm"), null); assert.equal(host.querySelector(".news-replay-bound"), null);
  assert.equal(button("确认分析这个历史时点"), undefined); assert.deepEqual(calls.analyze, []); assert.equal(JSON.stringify(stored), preserved, "the saved immutable context is retained");
});

test("unconfirmed historical analysis is retained without a retry or another implicit request", async () => {
  client.newsMarketAnalyze = async () => { calls.analyze.push("attempted"); throw new Error("合成通信未确认"); };
  await render({ configured: true }); await click(button("采用此版本首次发现时刻")); await click(button("准备历史材料（不调用 AI）")); await click(button("核对历史市场分析发送范围")); await click(button("确认分析这个历史时点"));
  assert.match(host.textContent, /历史分析结果未确认.*不会自动重发/s); assert.equal(button("核对历史市场分析发送范围"), undefined); assert.equal(calls.analyze.length, 1);
  await click(button("准备历史材料（不调用 AI）"));
  assert.match(host.textContent, /历史分析结果未确认/); assert.equal(button("核对历史市场分析发送范围"), undefined); assert.equal(calls.analyze.length, 1);
});

test("preparing an already analyzed context reads its saved terminal result without exposing a second request", async () => {
  client.newsMarketContext = async (id, payload) => { const saved = context(payload); return { market_context: saved, market_impact: { context_id: saved.context_id, status: "unknown" } }; };
  await render({ configured: true }); await click(button("采用此版本首次发现时刻")); await click(button("准备历史材料（不调用 AI）"));
  assert.match(host.textContent, /历史分析结果未确认/); assert.equal(button("核对历史市场分析发送范围"), undefined); assert.deepEqual(calls.analyze, []);
});

test("historical no-send material insufficiency is distinguished from review after actual analysis", async () => {
  const refusal = { status: "needs_review", material_insufficient: true, analysis_performed: false, error: "合成原文依据不足；未发送模型请求。" };
  let result = refusal;
  client.newsMarketContext = async (id, payload) => { const saved = context(payload); return { market_context: saved, market_impact: { ...result, context_id: saved.context_id } }; };
  await render({ configured: true }); await click(button("采用此版本首次发现时刻")); await click(button("准备历史材料（不调用 AI）"));
  assert.match(host.querySelector(".news-market-material-insufficient").textContent, /材料不足，本次未发送模型请求.*合成原文依据不足/s); assert.doesNotMatch(host.querySelector(".news-replay-bound").textContent, /历史分析需要复核/);
  result = { status: "needs_review", analysis_performed: true };
  await click(button("准备历史材料（不调用 AI）")); assert.equal(host.querySelector(".news-market-material-insufficient"), null); assert.match(host.textContent, /历史分析需要复核/); assert.deepEqual(calls.analyze, []);
});

test("current market no-send message requires both backend flags and preserves ordinary needs-review wording", async () => {
  const saved = context();
  const show = async impact => act(async () => root.render(React.createElement(NewsMarketContext, { item: { ...item, market_context: saved, market_impact: impact }, rooms: [], apiClient: client, configured: true })));
  await show({ status: "needs_review", material_insufficient: true, analysis_performed: false, error: "没有可引用的原文证据，只有标题或行情不足以分析新闻影响；未发送模型请求。" });
  assert.match(host.querySelector(".news-market-material-insufficient").textContent, /材料不足，本次未发送模型请求.*只有标题或行情/s);
  await show({ status: "needs_review", material_insufficient: true, analysis_performed: true });
  assert.equal(host.querySelector(".news-market-material-insufficient"), null); assert.match(host.textContent, /市场分析需要复核/);
  await show({ status: "needs_review", analysis_performed: false });
  assert.equal(host.querySelector(".news-market-material-insufficient"), null); assert.deepEqual(calls.analyze, []);
});

test("workspace offers replay for its current version and switches to an old snapshot without retaining current bindings", async () => {
  const old = version, latest = { ...version, id: "v2", version_number: 2, version_seen_at: "2020-07-01T00:00:00Z" };
  item = { ...item, read: true, event: { current_version: latest, version_count: 2, sources: [] } };
  const workspaceClient = { ...client, newsOverview: async () => ({ overview: { enabled: false, sources: [], categories: [], counts: {}, ai: { configured: false } } }), newsItems: async () => ({ items: [item], total: 1 }), newsItem: async () => ({ item }), newsVersions: async () => ({ versions: [latest, old] }) };
  await act(async () => root.render(React.createElement(NewsWorkspace, { apiClient: workspaceClient, rooms: [room] }))); await click(host.querySelector(".news-item"));
  assert.ok(host.querySelector(".news-market-replay")); assert.match(host.querySelector(".news-market-replay").textContent, /当前选择版本 2/);
  await click(button("采用此版本首次发现时刻")); await click(button("查看版本历史（2）")); await click([...host.querySelectorAll(".news-event-history button")].find(value => value.textContent.startsWith("版本 1")));
  assert.match(host.querySelector(".news-market-replay").textContent, /当前选择版本 1/); assert.equal(field("回放截止日期与时间").value, ""); assert.match(host.querySelector(".news-market-replay").textContent, /请明确选择回放截止时间/);
  await click(button("返回当前版本")); assert.match(host.querySelector(".news-market-replay").textContent, /当前选择版本 2/); assert.deepEqual(calls.prepare, []);
});

test("a news item with only one saved version still has a usable replay preparation entry", async () => {
  item = { ...item, read: true, current_version_id: "v1", event: { current_version: version, version_count: 1, sources: [] } };
  const workspaceClient = { ...client, newsOverview: async () => ({ overview: { enabled: false, sources: [], categories: [], counts: {}, ai: { configured: false } } }), newsItems: async () => ({ items: [item], total: 1 }), newsItem: async () => ({ item }) };
  await act(async () => root.render(React.createElement(NewsWorkspace, { apiClient: workspaceClient, rooms: [] }))); await click(host.querySelector(".news-item"));
  assert.ok(host.querySelector(".news-market-replay")); await click(button("采用此版本首次发现时刻")); await click(button("准备历史材料（不调用 AI）"));
  assert.equal(calls.prepare[0].payload.version_id, "v1"); assert.match(host.querySelector(".news-replay-bound").textContent, /新闻版本 1/); assert.deepEqual(calls.analyze, []);
});

test("API replay methods preserve encoded time, explicit identities, pagination and historical acknowledgment", async () => {
  const requests = []; globalThis.fetch = async (url, options) => { requests.push({ url, options }); return { ok: true, json: async () => ({ ok: true }) }; };
  const controller = new AbortController();
  await api.newsMarketOptions("room-a", controller.signal, { as_of: "2020-06-20T12:00:00+08:00", limit: 50, offset: 50 });
  await api.newsMarketReplays("news-a", "v1", controller.signal, { limit: 50, offset: 0 });
  await api.newsMarketSavedContext("news-a", "saved-context", controller.signal);
  await api.newsMarketAnalyze("news-a", "saved-context", controller.signal, { acknowledge_historical: true });
  await api.newsMarketAnalyze("news-a", "current-context", controller.signal);
  const optionsQuery = new URL(requests[0].url, "http://local.test").searchParams;
  assert.equal(optionsQuery.get("as_of"), "2020-06-20T12:00:00+08:00"); assert.equal(optionsQuery.get("offset"), "50");
  assert.match(requests[1].url, /replays\?version_id=v1/); assert.match(requests[2].url, /context\?context_id=saved-context/);
  assert.deepEqual(JSON.parse(requests[3].options.body), { context_id: "saved-context", acknowledge_historical: true }); assert.deepEqual(JSON.parse(requests[4].options.body), { context_id: "current-context" });
  assert.ok(requests.every(request => request.options.signal === controller.signal));
});
