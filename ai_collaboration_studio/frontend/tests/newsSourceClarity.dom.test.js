import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { createServer } from "vite";

const vite = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)), appType: "custom", logLevel: "silent", server: { hmr: false, middlewareMode: true } });
const dom = new JSDOM("<!doctype html><body></body>", { pretendToBeVisual: true, url: "http://source-clarity.test/" });
for (const key of ["window", "document", "navigator", "HTMLElement", "Node", "Event"]) Object.defineProperty(globalThis, key, { configurable: true, value: key === "window" ? dom.window : dom.window[key] });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const { createRoot } = await import("react-dom/client");
const { NewsWorkspace } = await vite.ssrLoadModule("/src/components/NewsWorkspace.jsx");
const originalFetch = globalThis.fetch;
let host, root, client, overview, items, calls;
const check = (outcome, parsed_items, new_items = 0, updated_items = 0) => ({ outcome, parsed_items, new_items, updated_items, http_status: outcome === "failed" ? 404 : 200, finished_at: "2026-10-28T01:00:00Z" });
const sourceCard = id => host.querySelector(`.news-source-entry[data-source-id="${id}"]`);
const button = text => [...host.querySelectorAll("button")].find(button => button.textContent.trim() === text);
async function click(element) { assert.ok(element); await act(async () => element.click()); }
async function render() { await act(async () => root.render(React.createElement(NewsWorkspace, { apiClient: client }))); }
test.beforeEach(() => {
  calls = { configure: [], network: 0 };
  const directory = { id: "ir_mu", name: "美光季度业绩目录", source_kind: "company_ir", discovery_method: "quarterly_directory", enabled: false, can_configure: true, status: "error", url: "https://investors.micron.com/quarterly-results", coverage: "MU 官方季度业绩和历史资料", time_semantics: "目录未提供发布时间；资料期间与本机发现时间分别保存", history_capability: "quarterly_directory_snapshot", access_policy: "继承等待期限，不自动读取链接正文", cooldown_active: true, cooldown_until: "2026-10-30T02:00:00Z", cooldown_reason: "access_denied", endpoint_change: { previous_url: "https://investors.micron.com/rss/news-releases.xml?items=30", current_url: "https://investors.micron.com/quarterly-results", changed_at: "2026-10-29T01:00:00Z", explicit_reenable_required: true }, last_check: check("failed", 0) };
  overview = { enabled: false, sources: [directory], categories: [], counts: { total: 0 }, ai: { configured: false }, recent_checks: [], checks_24h: {} };
  items = [];
  client = { newsOverview: async () => ({ overview }), newsItems: async () => ({ items, total: items.length }), newsItem: async id => ({ item: items.find(item => item.id === id) }), newsSourceControl: async (id, enabled) => { calls.configure.push({ id, enabled }); overview = { ...overview, sources: overview.sources.map(source => source.id === id ? { ...source, enabled, endpoint_change: { ...source.endpoint_change, explicit_reenable_required: false } } : source) }; return { overview }; } };
  globalThis.fetch = async () => { calls.network++; throw new Error("External requests are forbidden in source clarity fixtures"); };
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
test.afterEach(async () => { await act(async () => root.unmount()); assert.equal(calls.network, 0); document.body.replaceChildren(); globalThis.fetch = originalFetch; });
test.after(async () => { dom.window.close(); await vite.close(); });

test("directory identity and endpoint receipt preserve old RSS failure and inherited wait", async () => {
  overview.recent_checks = [{ source_id: "ir_mu", source_name: "美光季度业绩目录", ...overview.sources[0].last_check }];
  const before = JSON.stringify(overview);
  await render();
  const card = sourceCard("ir_mu");
  assert.match(card.querySelector(".news-source-capabilities").textContent, /发现入口：季度业绩目录.*历史范围：当前目录列出的季度历史资料.*目录未提供发布时间/s);
  assert.match(card.querySelector(".news-source-endpoint-change").textContent, /2026.*更换入口不代表旧 RSS 已恢复.*需你明确重新启用/s);
  assert.match(card.querySelector(".news-source-last-check").textContent, /更换入口前的检查记录.*检查失败 · HTTP 404/s);
  assert.match(host.querySelector(".news-check-records").textContent, /美光季度业绩目录 · 更换入口前的记录.*检查失败 · HTTP 404/s);
  assert.match(card.textContent, /下次允许检查：2026/);
  assert.ok(button("重新启用目录来源"));
  assert.deepEqual(calls.configure, []); assert.equal(JSON.stringify(overview), before);
  assert.equal(card.querySelectorAll('.news-source-endpoint-change a').length, 2);
});

test("reenabling uses the existing source ID only after the explicit action and does not erase old failure", async () => {
  await render(); await click(button("重新启用目录来源"));
  assert.deepEqual(calls.configure, [{ id: "ir_mu", enabled: true }]);
  assert.ok(button("暂停这个来源"));
  assert.match(sourceCard("ir_mu").querySelector(".news-source-last-check").textContent, /更换入口前的检查记录.*检查失败/s);
  assert.equal(overview.sources[0].cooldown_active, true);
  assert.equal(overview.ai.configured, false);
});

test("zero parsed, old rows without changes and failures have different visible results", async () => {
  overview.sources = [
    { id: "zero", name: "空目录", enabled: true, status: "ok", last_check: check("success", 0) },
    { id: "old", name: "旧条目目录", enabled: true, status: "ok", last_check: check("success", 12) },
    { id: "failed", name: "失败入口", enabled: true, status: "error", last_check: check("failed", 0) },
  ];
  overview.recent_checks = overview.sources.map(source => ({ source_id: source.id, source_name: source.name, ...source.last_check }));
  await render();
  assert.match(sourceCard("zero").textContent, /检查成功，本次读取 0 条/);
  assert.match(sourceCard("old").textContent, /检查成功，读到 12 条，均无新增或更新/);
  assert.match(sourceCard("failed").textContent, /检查失败 · HTTP 404/);
  assert.doesNotMatch(sourceCard("failed").textContent, /检查成功|本次读取 0 条/);
  assert.match(host.querySelector(".news-check-records").textContent, /新入库条目 0/);
  assert.doesNotMatch(host.querySelector(".news-run-counts").textContent, /新增消息/);
});

test("historical directory items keep unknown publication and separate the fiscal period from discovery time", async () => {
  items = [{ id: "historical", title: "合成季度资料", source_name: "美光季度业绩目录", source_id: "ir_mu", url: "https://investors.micron.com/synthetic", read: true, published_at: null, discovered_at: "2026-10-29T01:00:00Z", discovery_kind: "historical_backfill", reference_period: "2018 财年 Q3", published_time_source: "unknown_directory_listing", automatic_reading_eligible: false }];
  await render();
  const row = host.querySelector(".news-item");
  assert.equal(row.querySelector(".news-item-history").textContent, "历史资料");
  assert.equal(row.querySelector(".news-item-period").textContent, "2018 财年 Q3");
  assert.equal(row.querySelector("time").textContent, "未注明发布时间");
  assert.equal(row.querySelector("time").hasAttribute("datetime"), false);
  await click(row);
  const provenance = host.querySelector(".news-item-provenance");
  assert.match(provenance.textContent, /新入库不代表公告刚刚发布.*2018 财年 Q3.*所属期间不代替发布时间.*不纳入自动正文读取/s);
  assert.match(host.querySelector(".news-timestamps").textContent, /未注明发布时间.*2026/s);
  assert.deepEqual(calls.configure, []);
});

test("publication dates retain their year and legacy payloads do not invent discovery semantics", async () => {
  overview.sources = [{ id: "fed", name: "Federal Reserve", enabled: true, status: "ok" }];
  items = [{ id: "legacy", title: "旧版保存消息", source_name: "Federal Reserve", url: "https://example.test/legacy", read: true, published_at: "2019-10-29T01:00:00Z", discovered_at: "2019-10-29T02:00:00Z" }];
  await render();
  const row = host.querySelector(".news-item");
  assert.match(row.querySelector("time").textContent, /2019/);
  assert.equal(row.querySelector(".news-item-history"), null);
  await click(row);
  assert.equal(host.querySelector(".news-item-provenance"), null);
  assert.match(host.querySelector(".news-timestamps").textContent, /2019/);
  assert.equal(sourceCard("fed").querySelector(".news-source-last-check"), null);
  assert.equal(sourceCard("fed").querySelector(".news-source-endpoint-change"), null);
});

test("RSS coverage stays distinct and legacy success counts remain unknown rather than empty", async () => {
  overview.sources.push({ id: "rss", name: "官方 RSS", enabled: true, status: "ok", source_kind: "rss", discovery_method: "rss", url: "https://example.test/feed", history_capability: "feed_window_only", time_semantics: "RSS 声明的发布时间", last_check: check("success", null) });
  await render();
  assert.match(sourceCard("rss").querySelector(".news-source-capabilities").textContent, /RSS 订阅.*当前 RSS 订阅窗口/s);
  assert.match(sourceCard("rss").querySelector(".news-source-last-check").textContent, /读取条数未记录/);
  assert.doesNotMatch(sourceCard("rss").querySelector(".news-source-last-check").textContent, /读取 0 条/);
  assert.match(sourceCard("ir_mu").querySelector(".news-source-capabilities").textContent, /季度业绩目录/);
});

test("directory attachments preserve discovery-time unread status independently of saved article body", async () => {
  const references = ["Earnings presentation", "Prepared remarks", "Form 10-Q / 10K"].map((category, index) => ({ category, reference_period: "FY2026-Q3", url: `https://unregistered.example.test/attachment-${index}.pdf`, read: false, status: "not_read", registered_host_allowed: false }));
  items = [{ id: "q3", title: "合成 Q3 正文已保存", source_name: "美光季度业绩目录", read: true, evidence: { content_status: "full_text", body_text: "已经保存的正文", attachments: [] }, discovery_evidence: { attachment_references: references } }];
  const before = JSON.stringify(items);
  await render(); await click(host.querySelector(".news-item"));
  const details = host.querySelector(".news-directory-attachments");
  assert.match(details.querySelector("summary").textContent, /目录另列的附件（发现时未读） · 3 份/);
  assert.equal(details.querySelectorAll("li").length, 3);
  for (const row of details.querySelectorAll("li")) { assert.match(row.textContent, /FY2026-Q3.*目录发现时未读.*不在自动来源范围/s); assert.equal(row.querySelector("a").protocol, "https:"); }
  assert.match(details.textContent, /正文取得不表示这些附件已读/);
  assert.equal(JSON.stringify(items), before); assert.deepEqual(calls.configure, []);
});
