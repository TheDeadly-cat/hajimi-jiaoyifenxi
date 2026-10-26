import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { createServer } from "vite";

const vite = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)), appType: "custom", logLevel: "silent", server: { hmr: false, middlewareMode: true } });
const dom = new JSDOM("<!doctype html><body></body>", { pretendToBeVisual: true, url: "http://news-beginner.test/" });
for (const key of ["window", "document", "navigator", "HTMLElement", "Node", "Event", "KeyboardEvent"]) Object.defineProperty(globalThis, key, { configurable: true, value: key === "window" ? dom.window : dom.window[key] });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const { createRoot } = await import("react-dom/client");
const { NewsWorkspace } = await vite.ssrLoadModule("/src/components/NewsWorkspace.jsx");
const originalFetch = globalThis.fetch;
const models = [{ provider: "doubao", model: "doubao-seed-2-1-pro-260915", label: "豆包" }, { provider: "deepseek", model: "deepseek-flash", label: "DeepSeek Flash" }];
const item = (id, extra = {}) => ({ id, title: `保留消息${id}`, source_id: "fed", source_name: "美联储", excerpt: "已保存的合成公开摘录。", url: "https://www.federalreserve.gov/example", read: true, material_quality: { can_analyze: true, status: "sufficient_excerpt" }, ...extra });
let host, root, client, overview, items, calls;
const button = label => [...host.querySelectorAll("button")].find(x => x.textContent.trim() === label);
const checkBox = text => [...host.querySelectorAll(".news-ai-settings label")].find(x => x.textContent.includes(text)).querySelector('input[type="checkbox"]');
async function click(target) { assert.ok(target); await act(async () => target.click()); }
async function input(value) { await act(async () => { const field = host.querySelector('input[type="password"]'); Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, "value").set.call(field, value); field.dispatchEvent(new Event("input", { bubbles: true })); }); }
async function render() { await act(async () => root.render(React.createElement(NewsWorkspace, { apiClient: client, onOpenRooms: () => {} }))); }
function deferred() { let resolve; return { promise: new Promise(done => { resolve = done; }), resolve: value => resolve(value) }; }

test.beforeEach(() => {
  items = [item("a"), item("b")]; calls = { configure: [], analyze: [], refresh: 0, control: 0, external: 0 };
  overview = { enabled: true, running: true, refresh_pending: false, worker_health: { alive: true, status: "running", heartbeat_fresh: true }, counts: { total: 2, unread: 0 }, categories: [],
    sources: [{ id: "fed", name: "美联储", enabled: true, status: "ok" }, { id: "bls", name: "BLS", enabled: true, status: "error", cooldown_reason: "access_denied", cooldown_active: true, cooldown_until: "2026-10-30T01:00:00Z", error: "HTTP 403 synthetic" }, { id: "seagate", name: "希捷", enabled: true, status: "error", cooldown_reason: "rate_limited", cooldown_active: true, cooldown_until: "2026-10-30T02:00:00Z" }],
    startup: { supported: false, enabled: false, mode: "portable" },
    ai: { configured: false, automatic: false, provider: models[0].provider, model: models[0].model, available_models: models, max_calls: 36, budget_cny: 3, calls_reserved: 0, reserved_cny: 0, busy: false } };
  client = { newsOverview: async () => ({ overview }), newsItems: async () => ({ items, total: items.length }), newsItem: async id => ({ item: items.find(x => x.id === id) }), newsAIStatus: async () => ({ ai: overview.ai }),
    newsAIConfigure: async payload => { calls.configure.push(payload); overview.ai = { ...overview.ai, configured: true, provider: payload.provider, model: payload.model, automatic: payload.automatic }; return { ai: overview.ai }; },
    newsAIAnalyze: async id => { calls.analyze.push(id); return { ai_analysis: { status: "queued" } }; },
    newsRefresh: async () => { calls.refresh++; return { overview }; }, newsControl: async () => { calls.control++; return { overview }; } };
  globalThis.fetch = async () => { calls.external++; throw new Error("Synthetic test forbids network"); };
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
test.afterEach(async () => { await act(async () => root.unmount()); assert.equal(calls.external, 0); document.body.replaceChildren(); globalThis.fetch = originalFetch; });
test.after(async () => { dom.window.close(); await vite.close(); });

test("free reading can skip a partially filled setup without configuring AI or touching collection", async () => {
  await render(); const settings = host.querySelector(".news-ai-settings");
  await click(settings.querySelector("summary")); await input("synthetic-not-a-secret"); await click(checkBox("我确认"));
  assert.equal(host.querySelector(".news-ai-advanced").open, false);
  await click(button("先免费阅读"));
  assert.equal(settings.open, false); assert.equal(host.querySelector('input[type="password"]').value, "");
  assert.equal(checkBox("我确认").checked, false); assert.equal(checkBox("自动分析").checked, false);
  assert.equal(document.activeElement, host.querySelector('[aria-label="消息阅读区"]'));
  await click(host.querySelector(".news-item"));
  assert.match(host.querySelector(".news-excerpt").textContent, /已保存的合成公开摘录/);
  assert.ok(host.querySelector("a.news-original"));
  assert.deepEqual(calls.configure, []); assert.deepEqual(calls.analyze, []); assert.equal(calls.control, 0); assert.equal(calls.refresh, 0);
});

test("saving configuration does not analyze, and a single item needs an explicit send confirmation", async () => {
  await render(); await input("synthetic-not-a-secret"); await click(checkBox("我确认")); await click(button("保存AI设置"));
  assert.equal(calls.configure.length, 1); assert.equal(calls.configure[0].automatic, false);
  assert.equal(calls.configure[0].model, "doubao-seed-2-1-pro-260915"); assert.deepEqual(calls.analyze, []);
  assert.match(host.querySelector(".news-notice").textContent, /配置已保存.*没有验证/);
  await click(host.querySelector(".news-item")); await click(button("用豆包分析这条"));
  assert.deepEqual(calls.analyze, []); assert.match(host.querySelector(".news-ai-confirm").textContent, /保留消息a/);
  assert.equal(document.activeElement, host.querySelector(".news-ai-confirm h4"), "keyboard focus enters the explicit send confirmation");
  await click(button("暂不发送")); assert.deepEqual(calls.analyze, []);
  await click(button("用豆包分析这条"));
  const request = deferred(); client.newsAIAnalyze = id => { calls.analyze.push(id); return request.promise; };
  const confirm = button("确认发送这一条"); await act(async () => { confirm.click(); confirm.click(); });
  assert.deepEqual(calls.analyze, ["a"]);
  await act(async () => request.resolve({ ai_analysis: { status: "queued" } }));
  assert.equal(button("确认发送这一条"), undefined); assert.match(host.querySelector(".news-ai-reading").textContent, /已加入分析队列/);
});

test("changing the selected item discards the previous manual-send confirmation", async () => {
  overview.ai.configured = true;
  await render(); const rows = host.querySelectorAll(".news-item");
  await click(rows[0]); await click(button("用豆包分析这条")); assert.ok(button("确认发送这一条"));
  await click(rows[1]); assert.equal(button("确认发送这一条"), undefined);
  await click(button("用豆包分析这条")); assert.match(host.querySelector(".news-ai-confirm-title").textContent, /保留消息b/);
  await click(button("确认发送这一条")); assert.deepEqual(calls.analyze, ["b"]);
});

test("source restrictions explain affected coverage while healthy sources and saved reading remain available", async () => {
  await render();
  assert.match(host.querySelector(".news-coverage-scope").textContent, /美联储.*美国劳工统计局.*希捷公司公告.*不是全市场/);
  assert.match(host.querySelector(".news-coverage-warning").textContent, /其他未受限来源仍按各自计划/);
  const sources = host.querySelector(".news-source-popover");
  assert.match(sources.textContent, /来源要求稍后访问/); assert.match(sources.textContent, /新消息可能缺失/);
  assert.equal(sources.querySelector(".news-source-diagnostics").open, false);
  assert.match(sources.querySelector(".news-source-diagnostics").textContent, /HTTP 403/);
  assert.equal(calls.refresh, 0); assert.equal(calls.analyze.length, 0);
  overview.worker_health = { alive: false, status: "stopped" };
  await click(host.querySelector('.news-list-reload'));
  assert.doesNotMatch(host.querySelector(".news-coverage-warning").textContent, /其他未受限来源仍按各自计划/);
  await click(host.querySelector(".news-item")); assert.ok(host.querySelector("a.news-original"));
});

test("runtime help is mode-specific, expired AI stays optional, and an unknown result is not accepted or resent", async () => {
  overview.ai.error = "本次24小时授权已到期，请重新配置。";
  items[0] = item("a", { ai_analysis: { status: "unknown", summary: "MUST_NOT_BE_ACCEPTED", raw_model_output: "保留的合成未知结果" } });
  await render(); assert.match(host.querySelector(".news-use-help").textContent, /独立运行版.*不设置Windows登录后恢复/s);
  assert.match(host.querySelector(".news-ai-unavailable").textContent, /免费收集与阅读仍可使用.*授权已到期/);
  await click(host.querySelector(".news-item"));
  assert.doesNotMatch(host.querySelector(".news-ai-reading").textContent, /MUST_NOT_BE_ACCEPTED/);
  assert.match(host.querySelector(".news-ai-reading").textContent, /本次结果未确认/);
  assert.equal(host.querySelector(".news-ai-reading button"), null);
  delete overview.startup.mode; await click(host.querySelector('.news-list-reload'));
  assert.match(host.querySelector(".news-use-help").textContent, /运行方式尚未确认/);
  assert.doesNotMatch(host.querySelector(".news-use-help h2").textContent, /独立运行版/);
  overview.startup = { mode: "installed", supported: true, enabled: false }; await click(host.querySelector('.news-list-reload'));
  assert.match(host.querySelector(".news-use-help").textContent, /本机安装版.*运行记录.*不会自动配置AI/s);
  assert.equal(calls.configure.length, 0); assert.equal(calls.analyze.length, 0); assert.equal(calls.control, 0);
});
