import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { createServer } from "vite";

const vite = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)), appType: "custom", logLevel: "silent", server: { hmr: false, middlewareMode: true } });
const dom = new JSDOM("<!doctype html><body></body>", { pretendToBeVisual: true, url: "http://direct-news.test/" });
for (const key of ["window", "document", "navigator", "HTMLElement", "Node", "Event"]) Object.defineProperty(globalThis, key, { configurable: true, value: key === "window" ? dom.window : dom.window[key] });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const { createRoot } = await import("react-dom/client");
const { NewsMarketContext } = await vite.ssrLoadModule("/src/components/NewsMarketContext.jsx");
const { MarketImpact } = await vite.ssrLoadModule("/src/components/NewsEventCard.jsx");
const fetchBefore = globalThis.fetch;
const intervalBefore = window.setInterval;
const clearBefore = window.clearInterval;
let host, root, item, client, calls, timers, configured;
const button = label => [...host.querySelectorAll("button")].find(node => node.textContent.trim() === label);
const click = async target => { assert.ok(target); await act(async () => target.click()); };
const context = { context_id: "only-news", mode: "current_news_only", room_id: null, round_id: null, missing: ["尚不能判断是否超预期。", "尚无可用的对应时点行情，市场反应未确认。"], consensus: [], market: { captured_at: null } };
const render = async (rooms = []) => act(async () => root.render(React.createElement(NewsMarketContext, { item, rooms, apiClient: client, configured, routeLabel: "当前主模型", busy: false })));
test.beforeEach(() => {
  item = { id: "saved-news", current_version_id: "v1", title: "合成季度消息" };
  calls = { prepared: [], sent: [], options: [], read: [], network: 0 };
  configured = true; timers = new Map(); let sequence = 0;
  window.setInterval = callback => { const id = ++sequence; timers.set(id, callback); return id; };
  window.clearInterval = id => timers.delete(id);
  globalThis.fetch = async () => { calls.network++; throw new Error("offline only"); };
  client = {
    newsMarketContext: async (id, payload) => { calls.prepared.push({ id, payload }); return { market_context: context, market_impact: null }; },
    newsMarketAnalyze: async (id, contextId) => { calls.sent.push({ id, contextId }); return { market_impact: { status: "queued", context_id: contextId } }; },
    newsMarketOptions: async roomId => { calls.options.push(roomId); return { rounds: [] }; },
    newsMarketSavedContext: async (id, contextId) => { calls.read.push({ id, contextId }); return { market_context: context, market_impact: { status: "completed", context_id: contextId, facts: [] } }; },
  };
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
test.afterEach(async () => {
  await act(async () => root.unmount()); assert.equal(calls.network, 0);
  document.body.replaceChildren(); globalThis.fetch = fetchBefore; window.setInterval = intervalBefore; window.clearInterval = clearBefore;
});
test.after(async () => { dom.window.close(); await vite.close(); });

test("no room is needed and preparing the exact selected news never sends a model request", async () => {
  await render();
  assert.deepEqual(calls.prepared, []); assert.deepEqual(calls.sent, []); assert.deepEqual(calls.options, []);
  assert.equal(button("解释这条消息").disabled, false);
  await click(button("解释这条消息"));
  assert.deepEqual(calls.prepared, [{ id: "saved-news", payload: { room_id: null, round_id: null, material_refs: [] } }]);
  assert.deepEqual(calls.sent, []);
  assert.match(host.querySelector(".news-market-bound").textContent, /仅这条消息的已有证据.*尚不能判断是否超预期.*市场反应未确认/s);
  assert.ok(button("确认分析市场影响"));
  await click(button("暂不发送市场分析")); assert.deepEqual(calls.sent, []);
  await click(button("核对市场分析发送范围")); await click(button("确认分析市场影响"));
  assert.deepEqual(calls.sent, [{ id: "saved-news", contextId: "only-news" }]);
});

test("configuration is optional for preview, and unavailable room options do not hide direct explanation", async () => {
  configured = false; delete client.newsMarketOptions;
  await render(); await click(button("解释这条消息"));
  assert.match(host.textContent, /先在上方保存 AI 设置/);
  assert.equal(button("确认分析市场影响"), undefined); assert.deepEqual(calls.sent, []);
});

test("a room with no research rounds does not trap the direct current-news path", async () => {
  await render([{ id: "empty-room", title: "空研究室" }]);
  await act(async () => { const select = host.querySelector("select"); select.value = "empty-room"; select.dispatchEvent(new Event("change", { bubbles: true })); });
  assert.match(host.textContent, /尚无已保存轮次.*仍可使用上方/s);
  assert.equal(button("绑定所选资料（不调用 AI）").disabled, true);
  await click(button("解释这条消息"));
  assert.equal(calls.prepared[0].payload.room_id, null); assert.deepEqual(calls.sent, []);
});

test("polling reads the bound context rather than a later different prepared context", async () => {
  await render(); await click(button("解释这条消息")); await click(button("确认分析市场影响"));
  client.newsItem = async () => { throw new Error("Must read exact saved context"); };
  await act(async () => { for (const callback of [...timers.values()]) await callback(); });
  assert.deepEqual(calls.read, [{ id: "saved-news", contextId: "only-news" }]);
  assert.equal(timers.size, 0); assert.equal(calls.sent.length, 1);
});

test("a late prepared response for a previous news version cannot become the new version scope", async () => {
  let resolve; let signal;
  client.newsMarketContext = (id, payload, passedSignal) => { signal = passedSignal; return new Promise(done => { resolve = done; }); };
  await render(); await click(button("解释这条消息"));
  item = { ...item, current_version_id: "v2" }; await render();
  assert.equal(signal.aborted, true);
  await act(async () => resolve({ market_context: context }));
  assert.equal(host.querySelector(".news-market-bound"), null); assert.deepEqual(calls.sent, []);
});

test("unknown and insufficient saved states remain visible without a resend confirmation", async () => {
  client.newsMarketContext = async () => ({ market_context: context, market_impact: { status: "unknown", context_id: context.context_id } });
  await render(); await click(button("解释这条消息"));
  assert.match(host.textContent, /结果未确认.*不自动重发/s); assert.equal(button("确认分析市场影响"), undefined);
  client.newsMarketContext = async () => ({ market_context: context, market_impact: { status: "needs_review", material_insufficient: true, analysis_performed: false } });
  await click(button("解释这条消息"));
  assert.match(host.textContent, /材料不足，本次未发送模型请求/); assert.deepEqual(calls.sent, []);
});

test("six answers separate evidence, conditional inference, research actions and changed-judgment evidence", async () => {
  const impact = { status: "completed", facts: [{ claim: "合成已知事实", quote: "source quote", evidence_id: "article:p1" }], new_information: [], direct_symbols: ["US.MU"],
    inferred_impacts: [{ claim: "合成可能影响", channel: "供给变化", condition: "仅在审批通过后", horizon: "后续季度", quote: "source quote", evidence_id: "article:p1" }], unknowns: ["尚缺市场预期"], counter_evidence: [],
    research_actions: [{ kind: "verify", action: "核对正式审批文件" }, { kind: "observe", action: "观察后续产量披露" }], judgment_change_conditions: ["若审批撤回，应改变执行判断"] };
  await act(async () => root.render(React.createElement(MarketImpact, { impact })));
  for (const label of ["1. 发生了什么", "2. 与之前相比", "3. 可能影响谁", "4. 哪些资料仍缺失", "5. 现在应核实什么", "6. 什么新证据"]) assert.ok(host.textContent.includes(label));
  assert.match(host.textContent, /这是有条件的推断/); assert.match(host.textContent, /影响途径：供给变化/);
  assert.match(host.textContent, /核实：核对正式审批文件/); assert.match(host.textContent, /若审批撤回，应改变执行判断/);
  assert.equal(host.textContent.split("核对正式审批文件").length - 1, 1);
  assert.equal(host.querySelector("blockquote").textContent, "source quote");
});

test("legacy saved answers are not backfilled with invented six-question fields", async () => {
  const impact = { status: "completed", facts: [], new_information: [], inferred_impacts: [], unknowns: [], follow_up_conditions: ["原保存后续条件"] };
  const before = JSON.stringify(impact);
  await act(async () => root.render(React.createElement(MarketImpact, { impact })));
  assert.match(host.textContent, /旧版保存结果.*尚未分别记录研究动作/s);
  assert.equal(host.textContent.split("原保存后续条件").length - 1, 1);
  assert.doesNotMatch(host.textContent, /5\. 现在应核实/); assert.equal(JSON.stringify(impact), before);
});
