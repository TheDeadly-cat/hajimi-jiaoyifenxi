import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { createServer } from "vite";

const vite = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)), appType: "custom", logLevel: "silent", server: { hmr: false, middlewareMode: true } });
const dom = new JSDOM("<!doctype html><body></body>", { pretendToBeVisual: true, url: "http://news-reliability.test/" });
for (const key of ["window", "document", "navigator", "HTMLElement", "Node", "Event"]) Object.defineProperty(globalThis, key, { configurable: true, value: key === "window" ? dom.window : dom.window[key] });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const { createRoot } = await import("react-dom/client");
const { NewsWorkspace } = await vite.ssrLoadModule("/src/components/NewsWorkspace.jsx");
const originalFetch = globalThis.fetch;
let host, root, client, overview, calls;
const button = (label) => [...host.querySelectorAll("button")].find((entry) => entry.textContent.trim() === label);
const startup = () => host.querySelector('.news-startup-settings input[type="checkbox"]');
const history = () => host.querySelector(".news-run-history");
const primary = () => host.querySelector(".news-runtime-primary");
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { resolve, promise }; };
async function render() { await act(async () => root.render(React.createElement(NewsWorkspace, { apiClient: client, onOpenRooms: () => {} }))); }
async function click(target) { assert.ok(target); await act(async () => target.click()); }

test.beforeEach(() => {
  overview = {
    enabled: true, running: true, refresh_pending: false,
    counts: { total: 31, last_24h: 0, unread: 31 },
    sources: [{ id: "fed", name: "合成美联储来源", enabled: true, status: "ok", stale: false, overdue: false, last_success_at: "2026-10-19T01:05:00Z" }],
    categories: [], ai: { configured: false },
    worker_health: { status: "running", alive: true, heartbeat_at: "2026-10-19T01:06:00Z", consecutive_failures: 0 },
    continuity: { schema_version: "news_continuity_v1", evidence_started_at: "2026-10-19T01:00:00Z", session_started_at: "2026-10-19T01:00:00Z", last_heartbeat_at: "2026-10-19T01:06:00Z", previous_session_unclean: false, last_gap: null, unverified_gap_count_24h: 0, continuous_24h_proven: false },
    checks_24h: { started: 1, completed: 1, successful: 1, failed: 0, interrupted: 0, not_modified: 0, new_items: 2, updated_items: 0 },
    recent_checks: [], startup: { supported: true, enabled: false, error: "" },
  };
  calls = { control: [], refresh: 0, startup: [], forbiddenFetch: 0 };
  client = {
    newsOverview: async () => ({ ok: true, overview }),
    newsItems: async () => ({ ok: true, items: [], total: 0 }),
    newsControl: async (enabled) => { calls.control.push(enabled); overview = { ...overview, enabled, worker_health: { ...overview.worker_health, alive: true, status: enabled ? "running" : "paused" } }; return { ok: true, overview }; },
    newsRefresh: async () => { calls.refresh++; return { ok: true, overview }; },
    newsStartup: async (payload) => { calls.startup.push(payload); return { ok: true, startup: { supported: true, enabled: payload.enabled, error: "" } }; },
  };
  globalThis.fetch = async () => { calls.forbiddenFetch++; throw new Error("Real fetch is forbidden in the synthetic DOM suite"); };
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
test.afterEach(async () => { await act(async () => root.unmount()); assert.equal(calls.forbiddenFetch, 0); document.body.replaceChildren(); globalThis.fetch = originalFetch; });
test.after(async () => { dom.window.close(); await vite.close(); });

test("a stopped worker and stale successful source never become a live green status from saved news or pending flags", async () => {
  overview.worker_health = { status: "stopped", alive: false };
  overview.refresh_pending = true;
  overview.sources[0] = { ...overview.sources[0], stale: true, overdue: true, overdue_seconds: 7200 };
  overview.continuity.previous_session_unclean = true;
  await render();
  assert.match(primary().textContent, /抓取后台已停止/);
  assert.doesNotMatch(primary().textContent, /持续抓取已开启|正在抓取/);
  assert.equal(button("立即检查").disabled, false);
  assert.ok(button("恢复持续抓取"));
  assert.match(host.querySelector(".news-runtime-warning").textContent, /检查记录已过期/);
  assert.match(host.querySelector(".news-source-popover").textContent, /检查记录已过期.*不能据此确认.*最近成功/);
  assert.doesNotMatch(host.textContent, /连接正常|连续 24 小时稳定运行已通过/);
  assert.match(history().textContent, /上次退出未确认/);
  assert.equal(history().open, false, "run details must not displace the reading area on entry");
  assert.deepEqual(calls.control, []);
});

test("restore keeps the saved enabled state and waits for an acknowledged live worker", async () => {
  overview.worker_health = { status: "stopped", alive: false };
  const request = deferred();
  client.newsControl = (enabled) => { calls.control.push(enabled); return request.promise; };
  await render(); await click(button("恢复持续抓取"));
  assert.deepEqual(calls.control, [true]);
  assert.equal(button("恢复持续抓取").disabled, true);
  assert.match(primary().textContent, /抓取后台已停止/);
  await act(async () => request.resolve({ overview: { ...overview, worker_health: { status: "running", alive: true } } }));
  assert.match(primary().textContent, /持续抓取已开启/);
  assert.ok(button("暂停抓取"));
});

test("recovering state shows recovery evidence, not a fetching spinner that prevents intervention", async () => {
  overview.worker_health = { status: "recovering", alive: true, next_recovery_at: "2026-10-19T01:07:00Z", consecutive_failures: 3 };
  overview.refresh_pending = true;
  await render();
  assert.match(primary().textContent, /后台异常，正在恢复/);
  assert.doesNotMatch(primary().textContent, /持续抓取已开启/);
  assert.match(history().textContent, /连续异常 3 次/);
  assert.equal(button("立即检查").disabled, false);
  await click(button("暂停抓取")); assert.deepEqual(calls.control, [false]);
  assert.match(primary().textContent, /已暂停/);
});

test("history distinguishes a normal restart from unknown exit and reports source checks separately from message totals", async () => {
  overview.continuity.last_gap = { kind: "clean_restart", started_at: "2026-10-19T00:00:00Z", ended_at: "2026-10-19T01:00:00Z", duration_seconds: 3600 };
  overview.checks_24h = { successful: 12, failed: 2, interrupted: 1, new_items: 4 };
  overview.recent_checks = [
    { source_id: "fed", source_name: "合成美联储来源", started_at: "2026-10-19T01:04:00Z", finished_at: "2026-10-19T01:05:00Z", outcome: "not_modified", new_items: 0, updated_items: 0 },
    { source_id: "bls", source_name: "合成劳工统计局", started_at: "2026-10-19T01:03:00Z", outcome: "interrupted_unknown", new_items: 0, updated_items: 0 },
  ];
  await render();
  assert.match(history().textContent, /正常停机后恢复/);
  assert.doesNotMatch(history().textContent, /上次退出未确认/);
  assert.match(history().textContent, /这段记录不能证明抓取一直在运行/);
  assert.deepEqual([...host.querySelectorAll(".news-run-counts dd")].map((entry) => entry.textContent), ["12", "2", "1", "4"]);
  assert.match(host.querySelector(".news-check-records").textContent, /来源未更新.*新增 0 · 更新 0.*中断，结果未确认/);
  assert.equal(host.querySelectorAll(".news-check-records time").length, 2);
  assert.match(history().textContent, /当前记录不能确认连续 24 小时稳定运行/);
});

test("heartbeat gaps are described as unverified intervals rather than precise downtime", async () => {
  overview.continuity.last_gap = { kind: "heartbeat_gap", started_at: "2026-10-19T01:00:00Z", ended_at: "2026-10-19T01:05:00Z", duration_seconds: 300 };
  overview.continuity.unverified_gap_count_24h = 1;
  await render();
  assert.match(history().querySelector("summary").textContent, /有未确认的运行时段/);
  assert.match(history().textContent, /心跳记录存在缺口.*约 5 分钟/);
  assert.doesNotMatch(history().textContent, /停机 5 分钟|已稳定运行 24/);
});

test("an alive worker with stale heartbeat is unresponsive rather than healthy or dead and keeps intervention available", async () => {
  overview.worker_health = { status: "unresponsive", alive: true, heartbeat_fresh: false };
  overview.refresh_pending = true;
  overview.sources[0].status = "fetching";
  overview.recent_checks = [{ source_id: "fed", source_name: "合成美联储来源", started_at: "2026-10-19T01:00:00Z", outcome: "started", new_items: 0, updated_items: 0 }];
  await render();
  assert.match(primary().textContent, /后台检查暂未响应/);
  assert.doesNotMatch(primary().textContent, /抓取后台正在运行|持续抓取已开启|后台已停止|正在抓取/);
  assert.match(history().textContent, /后台线程仍存在，但近期心跳未确认/);
  assert.match(host.querySelector(".news-check-records").textContent, /结果待确认/);
  assert.match(host.querySelector(".news-source-popover").textContent, /本次检查结果未确认.*不能确认这个来源正在更新/);
  assert.equal(button("立即检查").disabled, false);
  assert.equal(button("暂停抓取").disabled, false);
  assert.equal(button("恢复持续抓取"), undefined, "an existing unresponsive worker must not be mistaken for a stopped worker");
  await click(button("立即检查")); assert.equal(calls.refresh, 1);
  assert.deepEqual(calls.control, []);
  overview.worker_health.status = "running";
  await click(host.querySelector('button[aria-label="重新读取消息列表"]'));
  assert.match(primary().textContent, /后台检查暂未响应/, "heartbeat_fresh=false also overrides an inconsistent running status");
});

test("clock changes are displayed as a continuity uncertainty rather than normal elapsed uptime", async () => {
  overview.continuity.last_gap = { kind: "clock_changed", started_at: "2026-10-19T01:05:00Z", ended_at: "2026-10-19T01:00:00Z", duration_seconds: null };
  overview.continuity.unverified_gap_count_24h = 1;
  await render();
  assert.match(history().textContent, /系统时间发生变化/);
  assert.match(history().textContent, /时长未知/);
  assert.match(history().textContent, /这段记录不能证明抓取一直在运行/);
});

test("missing reliability fields and old running flags stay unknown, with no invented zero counts or startup default", async () => {
  delete overview.worker_health; delete overview.continuity; delete overview.checks_24h; delete overview.startup;
  await render();
  assert.match(primary().textContent, /后台状态待确认/);
  assert.doesNotMatch(primary().textContent, /持续抓取已开启/);
  assert.deepEqual([...host.querySelectorAll(".news-run-counts dd")].map((entry) => entry.textContent), ["—", "—", "—", "—"]);
  assert.equal(startup().disabled, true);
  assert.match(history().textContent, /本机设置待确认，不会自动更改/);
  assert.deepEqual(calls.startup, []);
});

test("Windows startup checkbox uses the confirmed setting and does not optimistically change while saving", async () => {
  overview.startup.enabled = true;
  const request = deferred();
  client.newsStartup = (payload) => { calls.startup.push(payload); return request.promise; };
  await render();
  assert.equal(startup().checked, true);
  await click(startup());
  assert.deepEqual(calls.startup, [{ enabled: false }]);
  assert.equal(startup().checked, true);
  assert.equal(startup().disabled, true);
  assert.match(history().textContent, /正在保存/);
  await act(async () => request.resolve({ startup: { supported: true, enabled: false, error: "" } }));
  assert.equal(startup().checked, false);
  assert.equal(startup().disabled, false);
  assert.match(host.querySelector(".news-notice").textContent, /已关闭登录 Windows 后恢复抓取/);
});

test("startup save failure preserves the confirmed state and exposes an accessible error", async () => {
  client.newsStartup = async () => { throw new Error("合成写入失败，请重试"); };
  await render(); await click(startup());
  assert.equal(startup().checked, false);
  assert.equal(startup().disabled, false);
  assert.match(host.querySelector("#news-startup-error[role=alert]").textContent, /合成写入失败/);
  assert.doesNotMatch(host.querySelector(".news-notice").textContent, /已开启/);
});

test("startup response must confirm the requested boolean before the UI accepts it", async () => {
  client.newsStartup = async () => ({ startup: { supported: true, enabled: false } });
  await render(); await click(startup());
  assert.equal(startup().checked, false);
  assert.match(host.querySelector("#news-startup-error").textContent, /设置尚未确认/);
  client.newsStartup = async () => ({ overview: { ...overview, startup: { supported: true, enabled: true, error: "" } } });
  await click(startup()); assert.equal(startup().checked, true);
});

test("an enabled old-version startup entry can be updated directly without toggling the user's saved preference", async () => {
  overview.startup = { supported: true, enabled: true, version_current: false, error: "" };
  const request = deferred();
  client.newsStartup = (payload) => { calls.startup.push(payload); return request.promise; };
  await render();
  assert.equal(startup().checked, true);
  assert.match(history().textContent, /登录恢复入口仍指向旧版本/);
  await click(button("更新登录恢复入口"));
  assert.deepEqual(calls.startup, [{ enabled: true }]);
  assert.equal(startup().checked, true);
  assert.equal(button("正在更新…").disabled, true);
  await act(async () => request.resolve({ startup: { supported: true, enabled: true, version_current: true, error: "" } }));
  assert.equal(startup().checked, true);
  assert.equal(button("更新登录恢复入口"), undefined);
  assert.match(host.querySelector(".news-notice").textContent, /登录恢复入口已更新/);
});

test("an update reply still pointing to an old version cannot be accepted as a successful startup upgrade", async () => {
  overview.startup = { supported: true, enabled: true, version_current: false, error: "" };
  client.newsStartup = async (payload) => { calls.startup.push(payload); return { startup: { ...overview.startup } }; };
  await render(); await click(button("更新登录恢复入口"));
  assert.equal(startup().checked, true);
  assert.equal(button("更新登录恢复入口").disabled, false);
  assert.match(host.querySelector("#news-startup-error").textContent, /设置尚未确认/);
  assert.doesNotMatch(host.querySelector(".news-notice").textContent, /已更新/);
  assert.deepEqual(calls.startup, [{ enabled: true }]);
});

test("overview errors remove live-status claims while preserving readable historical content", async () => {
  await render();
  client.newsOverview = async () => { throw new Error("合成连接已断开"); };
  await click(host.querySelector('button[aria-label="重新读取消息列表"]'));
  assert.match(primary().textContent, /状态待确认/);
  assert.doesNotMatch(primary().textContent, /后台正在运行|持续抓取已开启/);
  assert.equal(startup().disabled, true);
  assert.match(history().querySelector("h2").textContent, /后台状态待确认/);
  assert.match(host.textContent, /已保留上次读取的内容/);
});

test("unmount aborts a pending startup change without sending it twice", async () => {
  let signal;
  client.newsStartup = (payload, passedSignal) => { calls.startup.push(payload); signal = passedSignal; return new Promise(() => {}); };
  await render(); await click(startup());
  await act(async () => root.unmount());
  assert.equal(signal.aborted, true);
  assert.deepEqual(calls.startup, [{ enabled: true }]);
  root = createRoot(host);
});

test("startup API uses same-origin JSON POST and forwards the cancellation signal", async () => {
  const { api } = await import("../src/api.js");
  const controller = new AbortController();
  let observed;
  globalThis.fetch = async (url, options) => { observed = { url, options }; return { ok: true, json: async () => ({ ok: true, startup: { supported: true, enabled: true } }) }; };
  const result = await api.newsStartup({ enabled: true }, controller.signal);
  assert.equal(observed.url, "/api/news/startup");
  assert.equal(observed.options.method, "POST");
  assert.equal(observed.options.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(observed.options.body), { enabled: true });
  assert.equal(observed.options.signal, controller.signal);
  assert.equal(result.startup.enabled, true);
});


test("source failure shows HTTP evidence and the next scheduled check without automatic refresh", async () => {
  overview.sources[0] = { ...overview.sources[0], status: "error", next_poll_at: "2026-10-19T01:20:00Z", error: "本次请求返回 HTTP 404。" };
  overview.recent_checks = [{ source_id: "fed", source_name: "合成美联储来源", outcome: "failed", http_status: 404, started_at: "2026-10-19T01:05:00Z", finished_at: "2026-10-19T01:05:01Z", new_items: 0, updated_items: 0 }];
  await render();
  assert.match(history().textContent, /检查失败 · HTTP 404/);
  assert.match(host.querySelector(".news-source-popover").textContent, /下次常规检查/);
  assert.match(host.querySelector(".news-source-popover").textContent, /本次请求返回 HTTP 404/);
  assert.equal(calls.refresh, 0);
});

test("rate-limited sources show the server's allowed time and remain waiting rather than fetching", async () => {
  overview.refresh_pending = true;
  overview.sources[0] = { ...overview.sources[0], status: "error", cooldown_active: true, cooldown_reason: "rate_limited", cooldown_until: "2026-10-19T02:30:00Z", refresh_queued: true };
  await render();
  const sources = host.querySelector(".news-source-popover");
  assert.match(sources.textContent, /来源要求稍后访问/);
  assert.match(sources.textContent, /下次允许检查/);
  assert.doesNotMatch(sources.textContent, /下次常规检查/);
  assert.match(sources.textContent, /已排队，到允许时间再检查/);
  assert.match(primary().textContent, /抓取后台正在运行/);
  assert.doesNotMatch(primary().textContent, /正在抓取/);
  assert.match(host.querySelector(".news-coverage-warning").textContent, /来源覆盖暂不完整/);
  assert.equal(button("已排队，等待允许检查").disabled, true);
  assert.equal(calls.refresh, 0, "mount must not request a source refresh or use the browser clock to bypass cooldown");
});

test("access denied stays unattributed and shows a wait without claiming the origin caused it", async () => {
  overview.sources[0] = { ...overview.sources[0], status: "error", cooldown_active: true, cooldown_reason: "access_denied", cooldown_until: "2026-10-19T03:00:00Z", refresh_queued: false };
  await render();
  assert.match(host.querySelector(".news-source-popover").textContent, /访问被拒绝，原因待确认/);
  assert.match(host.querySelector(".news-source-popover").textContent, /核对接入要求和请求路径/);
  assert.doesNotMatch(host.textContent, /原站拒绝|代理导致|已修复/);
  assert.equal(button("安排允许后检查").disabled, false);
  assert.equal(calls.refresh, 0);
});

test("a successful 304 remains recovery observation while schedule history is retained", async () => {
  overview.sources[0] = { ...overview.sources[0], status: "ok", cooldown_active: true, cooldown_reason: "rate_limited", cooldown_until: "2026-10-19T03:00:00Z", recovery_pending: true, recovery_successes: 1, refresh_queued: false };
  overview.recent_checks = [{ source_id: "fed", source_name: "合成美联储来源", outcome: "not_modified", http_status: 304, started_at: "2026-10-19T01:05:00Z", finished_at: "2026-10-19T01:05:01Z", new_items: 0, updated_items: 0 }];
  await render();
  assert.match(host.querySelector(".news-source-popover").textContent, /恢复观察中 · 连续成功 1 次/);
  assert.match(host.querySelector(".news-source-popover").textContent, /单次成功不代表持续恢复/);
  assert.match(host.querySelector(".news-source-status summary").textContent, /恢复中/);
  assert.match(host.querySelector(".news-coverage-warning").textContent, /来源覆盖暂不完整/);
  assert.match(history().textContent, /来源未更新/);
  assert.doesNotMatch(host.textContent, /已持续恢复|所有来源正常/);
});

test("partial cooldown checks available sources and acknowledges the cooled queue without promising immediate access", async () => {
  overview.sources.push({ id: "seagate", name: "合成公司公告", enabled: true, status: "error", cooldown_active: true, cooldown_reason: "rate_limited", cooldown_until: "2026-10-19T03:00:00Z", refresh_queued: false });
  client.newsRefresh = async () => {
    calls.refresh++;
    overview = { ...overview, refresh_pending: true, sources: overview.sources.map((entry) => ({ ...entry, refresh_queued: true })) };
    return { overview };
  };
  await render();
  const action = button("检查可用来源");
  assert.equal(action.disabled, false);
  assert.equal(action.getAttribute("aria-describedby"), "news-refresh-help");
  await click(action);
  assert.equal(calls.refresh, 1);
  assert.match(host.querySelector(".news-notice").textContent, /已安排可用来源检查.*冷却来源已排队，到允许时间再检查/);
  assert.doesNotMatch(host.querySelector(".news-notice").textContent, /已安排立即检查/);
  assert.equal(button("检查已排队").disabled, true);
  await click(button("检查已排队"));
  assert.equal(calls.refresh, 1);
});

test("all cooling sources can be queued once and subsequent clicks cannot add a request", async () => {
  overview.sources[0] = { ...overview.sources[0], status: "error", cooldown_active: true, cooldown_reason: "rate_limited", cooldown_until: "2026-10-19T03:00:00Z", refresh_queued: false };
  const request = deferred();
  client.newsRefresh = () => { calls.refresh++; return request.promise; };
  await render();
  await click(button("安排允许后检查"));
  assert.equal(button("安排允许后检查").disabled, true);
  await click(button("安排允许后检查"));
  assert.equal(calls.refresh, 1);
  await act(async () => request.resolve({ overview: { ...overview, refresh_pending: true, sources: overview.sources.map((entry) => ({ ...entry, refresh_queued: true })) } }));
  assert.equal(button("已排队，等待允许检查").disabled, true);
  await click(button("已排队，等待允许检查"));
  assert.equal(calls.refresh, 1);
  assert.doesNotMatch(primary().textContent, /正在抓取/);
  assert.match(host.querySelector(".news-notice").textContent, /冷却来源已排队，到允许时间再检查/);
});

test("manual hold offers no invented release time or bypass but saved messages remain readable", async () => {
  overview.sources[0] = { ...overview.sources[0], status: "error", cooldown_active: false, cooldown_until: null, cooldown_reason: "access_denied", manual_hold: true, refresh_queued: true, next_poll_at: "2026-10-19T02:00:00Z" };
  const item = { id: "saved", title: "已保存的合成公告", source_name: "合成来源", excerpt: "保留的来源摘录", read: true };
  client.newsItems = async () => ({ items: [item], total: 1 });
  client.newsItem = async () => ({ item });
  await render();
  assert.equal(button("需要检查来源设置").disabled, true);
  await click(button("需要检查来源设置"));
  assert.equal(calls.refresh, 0);
  assert.match(host.querySelector(".news-source-popover").textContent, /等待人工核对/);
  assert.match(host.querySelector(".news-source-popover").textContent, /允许访问的时间尚未确认/);
  assert.doesNotMatch(host.querySelector(".news-source-popover").textContent, /下次常规检查|下次允许检查：/);
  assert.match(host.querySelector(".news-runtime-times").textContent, /需人工检查/);
  await click(host.querySelector(".news-item"));
  assert.match(host.querySelector(".news-detail-body").textContent, /已保存的合成公告/);
  assert.match(host.querySelector(".news-excerpt").textContent, /保留的来源摘录/);
  assert.equal(calls.refresh, 0);
});

test("one manually held source does not prevent checks for other available sources", async () => {
  overview.sources.push({ id: "bls", name: "合成劳工统计来源", enabled: true, status: "error", cooldown_reason: "access_denied", manual_hold: true, cooldown_until: null, refresh_queued: false });
  await render();
  assert.equal(button("检查可用来源").disabled, false);
  await click(button("检查可用来源"));
  assert.equal(calls.refresh, 1);
  assert.match(host.querySelector(".news-notice").textContent, /已安排可用来源检查.*需人工检查的来源仍保持等待/);
  assert.doesNotMatch(host.querySelector(".news-notice").textContent, /已安排立即检查/);
});

test("legacy source payloads keep the normal explicit refresh path without fabricated cooldown or recovery", async () => {
  await render();
  assert.equal(button("立即检查").disabled, false);
  assert.doesNotMatch(host.querySelector(".news-source-popover").textContent, /冷却|恢复观察|已排队|人工检查/);
  assert.equal(host.querySelector("#news-refresh-help"), null);
  await click(button("立即检查"));
  assert.equal(calls.refresh, 1);
  assert.match(host.querySelector(".news-notice").textContent, /已安排立即检查/);
});

test("an unconfirmed queue response does not invent an accepted queue or a release time", async () => {
  overview.sources[0] = { ...overview.sources[0], status: "error", cooldown_active: true, cooldown_reason: "rate_limited", cooldown_until: "invalid-date", refresh_queued: false };
  await render(); await click(button("安排允许后检查"));
  assert.match(host.querySelector(".news-notice").textContent, /冷却来源仍需等待允许时间/);
  assert.doesNotMatch(host.querySelector(".news-notice").textContent, /已排队|已安排立即检查/);
  assert.match(host.querySelector(".news-source-popover").textContent, /下次允许检查：时间待确认/);
  assert.doesNotMatch(host.textContent, /Invalid Date/);
  assert.equal(button("安排允许后检查").disabled, false);
});

test("a disabled held source does not block available collection and actual fetching still shows progress", async () => {
  overview.sources.push({ id: "disabled", name: "合成停用来源", enabled: false, status: "error", manual_hold: true, refresh_queued: true });
  overview.refresh_pending = true;
  overview.sources[0].cooldown_active = false;
  overview.sources[0].refresh_queued = false;
  await render();
  assert.equal(button("立即检查").disabled, false);
  overview.sources[0].status = "fetching";
  await click(host.querySelector('button[aria-label="重新读取消息列表"]'));
  assert.match(primary().textContent, /正在抓取/);
  assert.equal(button("立即检查").disabled, true);
});

test("a partly completed manual batch remains queued while only its cooled source is still waiting", async () => {
  overview.refresh_pending = true;
  overview.sources[0] = { ...overview.sources[0], cooldown_active: false, refresh_queued: false };
  overview.sources.push({ id: "seagate", name: "合成公司公告", enabled: true, status: "error", cooldown_active: true, cooldown_reason: "rate_limited", cooldown_until: "2026-10-19T03:00:00Z", refresh_queued: true });
  await render();
  const queuedButton = button("已排队，等待允许检查");
  assert.equal(queuedButton.disabled, true);
  assert.equal(button("检查可用来源"), undefined);
  assert.match(host.querySelector("#news-refresh-help").textContent, /其他来源仍按原计划检查/);
  assert.doesNotMatch(primary().textContent, /正在抓取/);
  await click(queuedButton);
  assert.equal(calls.refresh, 0, "a pending batch must not pretend to open a new manual batch for the completed source");
  overview = { ...overview, refresh_pending: false, sources: overview.sources.map((entry) => ({ ...entry, refresh_queued: false })) };
  await click(host.querySelector('button[aria-label="重新读取消息列表"]'));
  assert.equal(button("检查可用来源").disabled, false, "another manual batch becomes available only after the prior queue clears");
});

test("an explicit pending flag in a current payload disables another batch even before per-source queue status is published", async () => {
  overview.refresh_pending = true;
  overview.sources[0] = { ...overview.sources[0], cooldown_active: false, refresh_queued: false };
  await render();
  assert.equal(button("检查已排队").disabled, true);
  assert.match(host.querySelector("#news-refresh-help").textContent, /已有检查批次在排队/);
  assert.doesNotMatch(primary().textContent, /正在抓取/);
  await click(button("检查已排队"));
  assert.equal(calls.refresh, 0);
});

test("legacy pending flags keep their existing fetching behavior without inventing the new queue contract", async () => {
  overview.refresh_pending = true;
  await render();
  assert.equal(button("立即检查").disabled, true);
  assert.match(primary().textContent, /正在抓取/);
  assert.equal(host.querySelector("#news-refresh-help"), null);
  assert.equal(calls.refresh, 0);
});

test("a cooldown deadline crossing into the next year includes its year without changing ordinary timestamp labels", async () => {
  overview.sources[0] = { ...overview.sources[0], status: "error", last_success_at: "2026-12-31T12:00:00Z", cooldown_active: true, cooldown_reason: "rate_limited", cooldown_until: "2027-01-02T12:00:00Z", refresh_queued: false };
  await render();
  const labels = [...host.querySelectorAll(".news-source-popover small")].map((entry) => entry.textContent);
  assert.match(labels.find((entry) => entry.startsWith("下次允许检查：")), /2027/);
  assert.doesNotMatch(labels.find((entry) => entry.startsWith("最近成功：")), /2026/);
  assert.equal(calls.refresh, 0);
});
