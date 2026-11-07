import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { createServer } from "vite";

const vite = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)), appType: "custom", logLevel: "silent", server: { hmr: false, middlewareMode: true } });
const dom = new JSDOM("<!doctype html><body></body>", { pretendToBeVisual: true, url: "http://first-use.test/" });
for (const key of ["window", "document", "navigator", "HTMLElement", "Node", "Event", "KeyboardEvent"]) Object.defineProperty(globalThis, key, { configurable: true, value: key === "window" ? dom.window : dom.window[key] });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const { createRoot } = await import("react-dom/client");
const { NewsWorkspace } = await vite.ssrLoadModule("/src/components/NewsWorkspace.jsx");
const originalFetch = globalThis.fetch;
let host, root, client, overview, calls, focus;
const catalog = { companies: [{ id: "micron", label: "美光", symbol: "US.MU", industries: ["storage", "semiconductors"] }, { id: "seagate", label: "希捷", symbol: "US.STX", industries: ["storage"] }], industries: [{ id: "storage", label: "存储" }], macro_topics: [{ id: "monetary", label: "货币政策", source_ids: ["fed"] }] };
const button = label => [...host.querySelectorAll("button")].find(node => node.textContent.trim() === label);
const checkbox = (scope, text) => [...host.querySelectorAll(`${scope} label`)].find(node => node.textContent.includes(text))?.querySelector('input[type="checkbox"]');
async function click(node) { assert.ok(node); await act(async () => node.click()); }
async function choose(label, value) { await act(async () => { const node = host.querySelector(`select[aria-label="${label}"]`); assert.ok(node); node.value = value; node.dispatchEvent(new Event("change", { bubbles: true })); }); }
async function input(node, value) { await act(async () => { Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, "value").set.call(node, value); node.dispatchEvent(new Event("input", { bubbles: true })); }); }
async function render() { await act(async () => root.render(React.createElement(NewsWorkspace, { apiClient: client, onOpenRooms: () => {} }))); }
function deferred() { let resolve; return { promise: new Promise(done => { resolve = done; }), resolve: value => resolve(value) }; }

test.beforeEach(() => {
  focus = { all_registered: true, companies: [], symbols: [], industries: [], macro_topics: [], automatic_scope: "market" };
  overview = { enabled: false, worker_health: { alive: true, status: "paused", heartbeat_fresh: true }, sources: [{ id: "fed", name: "美联储", enabled: true, status: "ok", poll_interval: 900, next_poll_at: "2026-11-07T09:15:00Z" }], counts: { total: 0, unread: 0 }, categories: [], article_reading: { enabled: false, queue: { pending: 2 } }, analysis_queue: { pending: 4, unknown: 1 }, coverage: [{ source_id: "fed", latest_published_at: "2026-11-07T09:00:00Z" }],
    ai: { configured: false, automatic: false, provider: "deepseek", model: "deepseek-flash", available_models: [{ provider: "deepseek", model: "deepseek-flash" }, { provider: "doubao", model: "doubao-seed-2-1-pro-260915" }], calls_reserved: 3, max_calls: 36, reserved_cny: 0.2, budget_cny: 3, remaining_calls: 33, remaining_cny: 2.8, pending: { total: 4, eligible: 1, held: 1, backlog: 2, unknown: 1, failed: 0 }, budget_resets_at: "2026-11-08T07:00:00Z", expires_at: null, busy: false } };
  calls = { order: [], focus: [], modes: [], configs: [], previews: [], confirms: [], external: 0, control: [], article: [], sources: [] };
  client = { newsOverview: async () => ({ overview }), newsItems: async () => ({ items: [], total: 0 }), newsAIStatus: async () => ({ ai: overview.ai }),
    newsFocus: async () => ({ focus, catalog }), newsFocusSave: async value => { calls.order.push("focus"); calls.focus.push(value); focus = { ...value }; return { focus, catalog }; },
    newsControl: async enabled => { calls.order.push("control"); calls.control.push(enabled); overview = { ...overview, enabled, worker_health: { ...overview.worker_health, status: enabled ? "running" : "paused" } }; return { overview }; },
    newsArticleReading: async enabled => { calls.order.push("article"); calls.article.push(enabled); overview = { ...overview, article_reading: { ...overview.article_reading, enabled } }; return { overview }; },
    newsSourceControl: async (id, enabled) => { calls.order.push("source"); calls.sources.push({ id, enabled }); overview = { ...overview, sources: overview.sources.map(row => row.id === id ? { ...row, enabled } : row) }; return { overview }; },
    newsAIMode: async value => { calls.order.push("mode"); calls.modes.push(value); overview.ai = { ...overview.ai, automatic: value.automatic }; return { ai: overview.ai }; },
    newsAIConfigure: async value => { calls.configs.push(value); overview.ai = { ...overview.ai, provider: value.provider, model: value.model, configured: true, automatic: value.automatic, expires_at: "2026-11-08T08:00:00Z" }; return { ai: overview.ai }; },
    newsAIClear: async () => { overview.ai = { ...overview.ai, configured: false, automatic: false }; return { ai: overview.ai }; },
    newsAIBacklogPreview: async value => { calls.previews.push(value); return { token: "synthetic-only", expires_at: "2099-11-07T09:01:00Z", tasks: [{ task_id: "task-a", item_id: "a", title: "旧待办合成消息", version_id: "v1", task_type: value.task_type, estimated_cost_cny: 0.1 }], count: 1, max_cost_cny: value.max_cost_cny, estimated_cost_cny: 0.1, reasons: [], confirmation_required: true }; },
    newsAIBacklogConfirm: async value => { calls.confirms.push(value); return { status: "queued", count: 1, task_ids: ["task-a"] }; },
  };
  globalThis.fetch = async () => { calls.external++; throw new Error("No external calls in synthetic UI verification"); };
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
test.afterEach(async () => { await act(async () => root.unmount()); assert.equal(calls.external, 0); document.body.replaceChildren(); globalThis.fetch = originalFetch; });
test.after(async () => { dom.window.close(); await vite.close(); });

test("opening shows three separate stages and accurately separates list refresh from source checks", async () => {
  await render();
  const stages = host.querySelector('[aria-label="消息处理三阶段"]');
  assert.match(stages.textContent, /消息监控.*已暂停.*重要原文.*未开启自动补充.*AI 分析.*尚未启用/s);
  assert.match(host.querySelector(".news-list-count").textContent, /每 15 秒刷新已保存列表/);
  assert.match(host.querySelector(".news-source-popover").textContent, /来源检查周期：15 分钟/);
  assert.match(host.querySelector(".news-source-popover").textContent, /最新保存原文时间：2026/);
  assert.deepEqual(calls.order, []); assert.deepEqual(calls.configs, []);
  await click(button("设置关注与工作方式"));
  assert.equal(host.querySelector(".news-setup-flow").open, true);
  assert.equal(document.activeElement, host.querySelector('select[aria-label="关注范围"]'));
});

test("pausing collection immediately applies the AI pause state from its mutation reply without waiting for polling", async () => {
  overview = { ...overview, enabled: true, worker_health: { ...overview.worker_health, status: "running" }, ai: { ...overview.ai, configured: true, automatic: true, pause_reason: null } };
  client.newsControl = async enabled => {
    calls.control.push(enabled);
    overview = { ...overview, enabled, worker_health: { ...overview.worker_health, status: enabled ? "running" : "paused" }, ai: { ...overview.ai, pause_reason: enabled ? null : "monitor_paused" } };
    return { overview };
  };
  await render();
  assert.match(host.querySelector(".news-flow-stages article:last-child strong").textContent, /自动解释重要变化/);
  await click(button("暂停抓取"));
  assert.deepEqual(calls.control, [false]);
  assert.match(host.querySelector(".news-flow-stages article:first-child strong").textContent, /已暂停/);
  assert.match(host.querySelector(".news-flow-stages article:last-child strong").textContent, /等待恢复消息监控/);
  await click(button("开始持续抓取"));
  assert.deepEqual(calls.control, [false, true]);
  assert.match(host.querySelector(".news-flow-stages article:last-child strong").textContent, /自动解释重要变化/);
});

test("unified setup immediately applies the final control reply even while its following overview refresh is pending", async () => {
  overview.ai = { ...overview.ai, configured: true, automatic: true, pause_reason: "monitor_paused" };
  const refresh = deferred();
  client.newsControl = async enabled => {
    calls.control.push(enabled);
    overview = { ...overview, enabled, worker_health: { ...overview.worker_health, status: "running" }, ai: { ...overview.ai, pause_reason: null } };
    client.newsOverview = async () => refresh.promise;
    return { overview };
  };
  await render(); await click(checkbox(".news-setup-flow", "我确认")); await click(button("3. 保存并开始监控"));
  assert.deepEqual(calls.control, [true]);
  assert.match(host.querySelector(".news-flow-stages article:last-child strong").textContent, /自动解释重要变化/);
  await act(async () => refresh.resolve({ overview }));
});

test("a ready free-reading setup uses focus, mode and start without requiring AI credentials", async () => {
  await render(); await choose("关注范围", "macro_topics:monetary"); await choose("工作方式", "collect");
  await click(button("3. 保存并开始监控"));
  assert.deepEqual(calls.order, ["focus", "control"]);
  assert.deepEqual(calls.focus[0].macro_topics, ["monetary"]); assert.equal(calls.focus[0].all_registered, false);
  assert.deepEqual(calls.modes, []); assert.deepEqual(calls.configs, []); assert.deepEqual(calls.article, []);
  assert.match(host.querySelector(".news-notice").textContent, /关注范围已保存.*消息监控开关已开启/);
});

test("choosing MU previews and enables its IR source while explicitly retaining the SEC contact gap", async () => {
  overview.sources.push({ id: "ir_mu", name: "美光官方季度目录", enabled: false, can_configure: true, source_kind: "company_ir", symbols: ["US.MU"] }, { id: "sec_mu", name: "MU SEC 申报", enabled: false, can_configure: true, source_kind: "sec", symbols: ["US.MU"] });
  await render(); await choose("关注范围", "companies:micron");
  assert.match(host.querySelector(".news-setup-source-plan").textContent, /会启用：美光官方季度目录.*暂不启用：MU SEC 申报（需先填写 SEC 联系标识）/s);
  assert.deepEqual(calls.sources, []);
  await click(button("3. 保存并开始监控"));
  assert.deepEqual(calls.order, ["focus", "source", "control"]);
  assert.deepEqual(calls.sources, [{ id: "ir_mu", enabled: true }]);
  assert.match(host.querySelector(".news-notice").textContent, /美光官方季度目录已启用.*MU SEC 申报仍未覆盖/);
});

test("mixed MU and AAPL focus names the unsupported symbol before and after starting only the matched source", async () => {
  overview.sources.push({ id: "ir_mu", name: "美光官方季度目录", enabled: false, can_configure: true, source_kind: "company_ir", symbols: ["US.MU"] });
  await render(); await choose("关注范围", "custom"); await input(host.querySelector('input[aria-label="关注股票代码"]'), "MU AAPL");
  assert.match(host.querySelector(".news-setup-source-plan").textContent, /AAPL暂无已登记来源，本次不会监控它/);
  assert.doesNotMatch(host.querySelector(".news-setup-source-plan").textContent, /MU暂无已登记来源/);
  assert.equal(button("3. 保存并开始监控").disabled, false);
  await click(button("3. 保存并开始监控"));
  assert.deepEqual(calls.sources, [{ id: "ir_mu", enabled: true }]);
  assert.match(host.querySelector(".news-flow-overview").textContent, /AAPL暂无已登记来源，本次不会监控它/);
  assert.deepEqual(calls.control, [true]);
});

test("AAPL alone remains visibly unsupported and cannot announce successful monitoring", async () => {
  focus = { ...focus, all_registered: false, symbols: ["US.AAPL"] };
  await render();
  assert.match(host.querySelector(".news-setup-source-plan").textContent, /AAPL暂无已登记来源，本次不会监控它/);
  assert.match(host.querySelector(".news-flow-overview").textContent, /AAPL暂无已登记来源，本次不会监控它/);
  assert.equal(button("3. 保存并开始监控").disabled, true);
  await click(button("3. 保存并开始监控")); assert.deepEqual(calls.sources, []); assert.deepEqual(calls.control, []); assert.deepEqual(calls.focus, []);
});

test("a source enable failure retains the saved focus and never announces the monitoring flow complete", async () => {
  overview.sources.push({ id: "ir_mu", name: "美光季度目录", enabled: false, can_configure: true, source_kind: "company_ir", symbols: ["US.MU"] });
  client.newsSourceControl = async () => { calls.order.push("source"); throw new Error("合成来源需人工确认"); };
  await render(); await choose("关注范围", "companies:micron"); await click(button("3. 保存并开始监控"));
  assert.deepEqual(calls.order, ["focus", "source"]); assert.deepEqual(calls.control, []);
  assert.match(host.querySelector('[role="alert"]').textContent, /关注范围已保存.*后续步骤未全部完成.*合成来源需人工确认/);
  assert.equal(overview.enabled, false);
});

test("switching the unified workflow to free reading stops automatic AI before changing focus", async () => {
  overview.ai = { ...overview.ai, configured: true, automatic: true }; await render(); await choose("工作方式", "collect");
  await click(button("3. 保存并开始监控"));
  assert.deepEqual(calls.order, ["mode", "focus", "control"]);
  assert.deepEqual(calls.modes, [{ automatic: false, acknowledge_budget: true }]); assert.deepEqual(calls.configs, []);
});

test("manual or automatic setup without a model explains the missing step instead of silently starting", async () => {
  await render(); await choose("工作方式", "automatic");
  assert.equal(button("3. 保存并开始监控").disabled, true);
  assert.match(host.querySelector(".news-setup-required").textContent, /需要先保存一个主模型/);
  assert.deepEqual(calls.order, []);
  await click(button("先设置主模型")); assert.equal(host.querySelector(".news-ai-settings").open, true);
});

test("a new UI configuration explicitly selects the backend recommended DeepSeek route", async () => {
  await render(); await input(host.querySelector('input[type="password"]'), "synthetic-not-a-secret");
  await click(checkbox(".news-ai-settings", "我确认")); await click(button("保存AI设置"));
  assert.equal(calls.configs.length, 1); assert.equal(calls.configs[0].provider, "deepseek"); assert.equal(calls.configs[0].model, "deepseek-flash");
  assert.equal(calls.configs[0].automatic, false); assert.ok(button("切换为自动解释重要变化"));
});

test("switching manual to automatic and back preserves the active model without re-entering a key", async () => {
  overview.ai = { ...overview.ai, configured: true, expires_at: "2026-11-08T08:00:00Z", provider: "doubao", model: "doubao-seed-2-1-pro-260915" };
  await render(); await click(button("切换为自动解释重要变化")); assert.deepEqual(calls.modes, []);
  await click(button("确认开启自动解释"));
  assert.deepEqual(calls.modes, [{ automatic: true, acknowledge_budget: true }]); assert.deepEqual(calls.configs, []);
  assert.equal(overview.ai.expires_at, "2026-11-08T08:00:00Z"); assert.equal(overview.ai.calls_reserved, 3); assert.equal(overview.ai.provider, "doubao");
  await click(button("停止自动，保留手动解释"));
  assert.equal(overview.ai.configured, true); assert.equal(overview.ai.automatic, false); assert.deepEqual(calls.configs, []);
});

test("a mode save failure leaves confirmed state and never silently retries", async () => {
  overview.ai.configured = true; client.newsAIMode = async value => { calls.modes.push(value); throw new Error("合成模式保存失败"); };
  await render(); await click(button("切换为自动解释重要变化")); await click(button("确认开启自动解释"));
  assert.equal(calls.modes.length, 1); assert.equal(overview.ai.automatic, false);
  assert.match(host.querySelector("#news-ai-error").textContent, /合成模式保存失败/);
  assert.ok(button("切换为自动解释重要变化"));
});

test("automatic setup confirms one scope and reports earlier successful steps if a later step fails", async () => {
  overview.ai.configured = true;
  client.newsAIMode = async value => { calls.order.push("mode"); calls.modes.push(value); throw new Error("合成授权到期"); };
  await render(); await choose("工作方式", "automatic");
  await click(checkbox(".news-setup-flow", "自动补充重要原文"));
  await click(checkbox(".news-setup-flow", "我确认")); await click(button("3. 保存并开始监控"));
  assert.deepEqual(calls.order, ["focus", "article", "mode"]); assert.deepEqual(calls.control, []);
  assert.equal(calls.focus[0].automatic_scope, "market"); assert.equal(overview.article_reading.enabled, true);
  assert.match(host.querySelector('[role="alert"]').textContent, /关注范围已保存.*重要原文自动补充已开启.*后续步骤未全部完成.*合成授权到期/);
});

test("expired authorization explains recovery and does not claim pending UNKNOWN work will be retried", async () => {
  overview.ai.pause_reason = "authorization_expired"; overview.ai.error = "本次24小时授权已到期，请重新配置。";
  await render();
  assert.match(host.querySelector(".news-flow-overview").textContent, /授权已到期/);
  assert.match(host.querySelector(".news-flow-budget").textContent, /待处理 4 条.*结果未知 1 条.*下一笔额度释放.*结果未知的请求保留，不自动重发/s);
  assert.equal(button("预览可补处理范围（不调用 AI）"), undefined); assert.deepEqual(calls.previews, []);
});

test("an exhausted budget is visible as stopped analysis rather than an available automatic mode", async () => {
  overview.ai = { ...overview.ai, configured: true, automatic: true, pause_reason: "budget_exhausted", remaining_calls: 0, remaining_cny: 0 };
  await render();
  assert.match(host.querySelector(".news-flow-stages article:last-child strong").textContent, /预约额度已用完/);
  assert.match(host.querySelector(".news-flow-next").textContent, /等待额度释放/);
  assert.deepEqual(calls.modes, []);
});

test("old backlog is previewed with bounded cost before explicit confirmation, and edits discard its token", async () => {
  overview.ai.configured = true; await render();
  await click(button("预览可补处理范围（不调用 AI）"));
  assert.deepEqual(calls.previews, [{ limit: 3, max_cost_cny: 0.3, task_type: "market_impact" }]); assert.deepEqual(calls.confirms, []);
  assert.match(host.querySelector(".news-backlog-preview").textContent, /旧待办合成消息.*综合解释/);
  const limitInput = host.querySelector('.news-backlog-options input[type="number"]'); await input(limitInput, "2");
  assert.equal(host.querySelector(".news-backlog-preview"), null);
  await click(button("预览可补处理范围（不调用 AI）")); await click(button("确认仅补处理这 1 条"));
  assert.deepEqual(calls.confirms, [{ token: "synthetic-only" }]); assert.match(host.querySelector(".news-backlog-recovery").textContent, /已明确安排 1 条旧待办/);
});

test("backlog bounds match the ten-item API limit and empty eligibility is explained without internal codes", async () => {
  overview.ai.configured = true;
  client.newsAIBacklogPreview = async value => { calls.previews.push(value); return { token: null, expires_at: "2099-11-07T09:01:00Z", tasks: [], count: 0, max_cost_cny: value.max_cost_cny, estimated_cost_cny: 0, reasons: ["no_eligible_backlog_within_budget"], confirmation_required: true }; };
  await render(); const limitInput = host.querySelector('.news-backlog-options input[type="number"]');
  assert.equal(limitInput.max, "10"); await input(limitInput, "11"); assert.equal(button("预览可补处理范围（不调用 AI）").disabled, true);
  await click(button("预览可补处理范围（不调用 AI）")); assert.deepEqual(calls.previews, []);
  await input(limitInput, "10"); await click(button("预览可补处理范围（不调用 AI）"));
  assert.match(host.querySelector(".news-backlog-preview").textContent, /当前关注、证据条件和本批预算/);
  assert.doesNotMatch(host.querySelector(".news-backlog-preview").textContent, /no_eligible/);
  assert.equal(button("确认仅补处理这 0 条").disabled, true);
  assert.equal(host.querySelector('.news-backlog-recovery [role="alert"]'), null);
  await click(button("确认仅补处理这 0 条")); assert.deepEqual(calls.confirms, []);
});

for (const invalid of [
  { count: 1, tasks: [], token: "not-enough-tasks" },
  { count: 0, tasks: [], token: "empty-must-not-have-token" },
  { count: 1, tasks: [{ task_id: "a" }], token: null },
]) test(`a mismatched backlog count or token is rejected: ${invalid.token || "missing-positive-token"}`, async () => {
  overview.ai.configured = true;
  client.newsAIBacklogPreview = async () => ({ ...invalid, confirmation_required: true, max_cost_cny: 0.3, estimated_cost_cny: 0 });
  await render(); await click(button("预览可补处理范围（不调用 AI）"));
  assert.equal(host.querySelector(".news-backlog-preview"), null);
  assert.match(host.querySelector('.news-backlog-recovery [role="alert"]').textContent, /预览尚未确认/);
  assert.deepEqual(calls.confirms, []);
});

test("clearing authorization does not accept a late backlog preview for the previous key", async () => {
  overview.ai.configured = true; const pending = deferred(); client.newsAIBacklogPreview = async value => { calls.previews.push(value); return pending.promise; };
  await render(); await click(button("预览可补处理范围（不调用 AI）"));
  await click(button("清除密钥并停止 AI"));
  await act(async () => pending.resolve({ token: "obsolete", expires_at: "2099-11-07T09:01:00Z", tasks: [], count: 0, max_cost_cny: 0.3, estimated_cost_cny: 0, reasons: [], confirmation_required: true }));
  assert.equal(host.querySelector(".news-backlog-preview"), null); assert.deepEqual(calls.confirms, []);
});
