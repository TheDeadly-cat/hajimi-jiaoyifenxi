import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { createServer } from "vite";

const vite = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)), appType: "custom", logLevel: "silent", server: { hmr: false, middlewareMode: true } });
const dom = new JSDOM("<!doctype html><body></body>", { pretendToBeVisual: true, url: "http://saved-news.test/" });
for (const key of ["window", "document", "navigator", "HTMLElement", "Node", "Event"]) Object.defineProperty(globalThis, key, { configurable: true, value: key === "window" ? dom.window : dom.window[key] });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const { createRoot } = await import("react-dom/client");
const { NewsSavedAnalyses } = await vite.ssrLoadModule("/src/components/NewsSavedAnalyses.jsx");
const originalFetch = globalThis.fetch;
let host, root, item, client, calls, record, result;
const button = label => [...host.querySelectorAll("button")].find(node => node.textContent.trim() === label);
const savedButton = () => host.querySelector("li button");
const click = async target => { assert.ok(target); await act(async () => target.click()); };
const render = async (versionId = null) => act(async () => root.render(React.createElement(NewsSavedAnalyses, { item, versionId, apiClient: client })));

test.beforeEach(() => {
  item = { id: "news-1", current_version_id: "v2" };
  record = { context_id: "saved-1", article_version_id: "v1", article_version_number: 1, mode: "current_news_only",
    created_at: "2026-11-07T01:00:00Z", as_of: "2026-11-07T00:55:00Z", evidence_matches_current: false, requires_review: true,
    source_url: "https://example.test/original", title: "合成旧版公告" };
  result = { saved_record: record, market_context: { context_id: record.context_id, mode: record.mode },
    market_impact: { context_id: record.context_id, status: "completed", facts: [{ claim: "原保存的收入数字", quote: "Synthetic revenue was 100.", evidence_id: "v1:paragraph-1" }], inferred_impacts: [], unknowns: ["当时未取得对应行情"] } };
  calls = { list: [], read: [], writes: 0, network: 0 };
  const forbidWrite = () => { calls.writes++; throw new Error("history must be read-only"); };
  client = {
    newsMarketHistory: async (id, versionId, signal, options) => { calls.list.push({ id, versionId, ...options }); return { contexts: [record], total: 1, ...options }; },
    newsMarketSavedContext: async (id, contextId, signal, options) => { calls.read.push({ id, contextId, ...options }); return result; },
    newsMarketContext: forbidWrite, newsMarketAnalyze: forbidWrite, newsAIConfigure: forbidWrite,
  };
  globalThis.fetch = async () => { calls.network++; throw new Error("offline fixture only"); };
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
test.afterEach(async () => {
  await act(async () => root.unmount());
  assert.equal(calls.writes, 0); assert.equal(calls.network, 0);
  document.body.replaceChildren(); globalThis.fetch = originalFetch;
});
test.after(async () => { dom.window.close(); await vite.close(); });

test("a completed ordinary analysis remains readable after a news revision without configuration or resending", async () => {
  await render(); assert.deepEqual(calls.list, []);
  await click(button("查看已保存的分析")); await click(savedButton());
  assert.deepEqual(calls.read, [{ id: "news-1", contextId: "saved-1", version_id: "v1" }]);
  const saved = host.querySelector('[aria-label="所选保存分析"]');
  assert.match(saved.textContent, /当时的消息解释/);
  assert.match(saved.textContent, /消息已有不同版本.*不能作为当前结论/s);
  assert.match(saved.textContent, /相关材料已有变化.*原结果和引用保持原样/s);
  assert.match(saved.textContent, /原保存的收入数字/);
  assert.equal(saved.querySelector("blockquote").textContent, "Synthetic revenue was 100.");
  assert.equal(saved.querySelector("a").href, "https://example.test/original");
  assert.equal(button("确认分析市场影响"), undefined);
});

test("version history can list ordinary saved analyses without relabeling them as historical replays", async () => {
  await render("v1"); await click(button("查看已保存的分析")); await click(savedButton());
  assert.deepEqual(calls.list, [{ id: "news-1", versionId: "v1", limit: 50, offset: 0 }]);
  assert.match(host.textContent, /此版本保存的分析/);
  assert.match(host.querySelector('[aria-label="所选保存分析"]').textContent, /当时的消息解释/);
  assert.equal(result.market_context.mode, "current_news_only");
});

test("shared saved evidence keeps its original ownership and reads the exact selected article version", async () => {
  result.saved_record = { ...record, is_shared: true, source_item_id: "original-news", source_article_version_id: "original-v1" };
  result.market_context.article_version_id = "original-v1";
  await render("v1"); await click(button("查看已保存的分析")); await click(savedButton());
  assert.deepEqual(calls.read, [{ id: "news-1", contextId: "saved-1", version_id: "v1" }]);
  assert.match(host.querySelector('[aria-label="所选保存分析"]').textContent, /最初保存于同源消息记录.*原分析归属和内容保持不变/s);
  assert.equal(result.market_context.article_version_id, "original-v1");
});

test("a mismatched saved response never displays another version's conclusion", async () => {
  await render(); await click(button("查看已保存的分析"));
  result = { ...result, saved_record: { ...record, article_version_id: "v2" } };
  await click(savedButton());
  assert.match(host.querySelector('[role="alert"]').textContent, /与所选保存版本不一致/);
  assert.equal(host.querySelector('[aria-label="所选保存分析"]'), null);
  assert.doesNotMatch(host.textContent, /原保存的收入数字/);
});

test("malformed or cross-version list data is an error rather than a false empty history", async () => {
  client.newsMarketHistory = async () => ({ contexts: [{ ...record, article_version_id: "other-version" }], total: 1, limit: 50, offset: 0 });
  await render("v1"); await click(button("查看已保存的分析"));
  assert.match(host.querySelector('[role="alert"]').textContent, /范围尚未确认/);
  assert.doesNotMatch(host.textContent, /这里还没有保存/);
  assert.equal(savedButton(), null);
});

test("empty history and failed reads have distinct states with a working retry", async () => {
  client.newsMarketHistory = async () => { throw new Error("保存记录暂时不可用"); };
  await render(); await click(button("查看已保存的分析"));
  assert.match(host.querySelector('[role="alert"]').textContent, /暂时不可用/);
  assert.doesNotMatch(host.textContent, /这里还没有保存/);
  client.newsMarketHistory = async () => ({ contexts: [], total: 0, limit: 50, offset: 0 });
  await click(button("刷新保存记录"));
  assert.equal(host.querySelector('[role="alert"]'), null);
  assert.match(host.textContent, /这里还没有保存的分析资料/);
  let resolve;
  client.newsMarketHistory = () => new Promise(done => { resolve = done; });
  await click(button("刷新保存记录"));
  assert.match(host.textContent, /正在读取保存记录/);
  assert.doesNotMatch(host.textContent, /这里还没有保存/);
  await act(async () => resolve({ contexts: [], total: 0, limit: 50, offset: 0 }));
  assert.match(host.textContent, /这里还没有保存的分析资料/);
});

test("switching to a different news item cancels a late saved-analysis response", async () => {
  let resolve, signal;
  client.newsMarketSavedContext = (id, contextId, passedSignal) => { signal = passedSignal; return new Promise(done => { resolve = done; }); };
  await render(); await click(button("查看已保存的分析")); await click(savedButton());
  item = { id: "news-2", current_version_id: "v-other" }; await render();
  assert.equal(signal.aborted, true);
  await act(async () => resolve(result));
  assert.equal(host.querySelector('[aria-label="所选保存分析"]'), null);
  assert.doesNotMatch(host.textContent, /原保存的收入数字/);
});

test("unknown saved results are retained without offering automatic resubmission", async () => {
  result.market_impact = { context_id: record.context_id, status: "unknown" };
  await render(); await click(button("查看已保存的分析")); await click(savedButton());
  assert.match(host.textContent, /结果未确认.*查看不会重新发送/s);
  assert.equal(host.querySelector(".news-event-market"), null);
  assert.equal(button("确认分析市场影响"), undefined);
});

test("matching news evidence never presents a saved market snapshot as current market data", async () => {
  result.saved_record = { ...record, evidence_matches_current: true, requires_review: false };
  await render(); await click(button("查看已保存的分析")); await click(savedButton());
  const saved = host.querySelector('[aria-label="所选保存分析"]');
  assert.match(saved.textContent, /资料保存.*分析材料截止/s);
  assert.match(saved.textContent, /不代表当时的行情或预期仍然有效/);
  assert.doesNotMatch(saved.textContent, /消息已有不同版本/);
});

test("pagination can reach older saved analyses without losing the first page", async () => {
  const rows = Array.from({ length: 51 }, (_, i) => ({ ...record, context_id: `saved-${i}` }));
  client.newsMarketHistory = async (id, versionId, signal, options) => {
    calls.list.push({ id, versionId, ...options });
    return { contexts: rows.slice(options.offset, options.offset + options.limit), total: rows.length, ...options };
  };
  await render(); await click(button("查看已保存的分析"));
  assert.equal(host.querySelectorAll("li button").length, 50);
  await click(button("加载更多保存记录"));
  assert.equal(host.querySelectorAll("li button").length, 51);
  assert.equal(calls.list[1].offset, 50);
  assert.equal(button("加载更多保存记录"), undefined);
});
