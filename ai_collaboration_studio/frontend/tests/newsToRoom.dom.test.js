import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { createServer } from "vite";
import { buildNewsResearchDraft, newsResearchSubmission, newsResearchStillCurrent } from "../src/newsResearch.js";

const vite = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)), appType: "custom", logLevel: "silent", server: { hmr: false, middlewareMode: true } });
const dom = new JSDOM("<!doctype html><body></body>", { pretendToBeVisual: true, url: "http://news-to-room.test/" });
for (const key of ["window", "document", "navigator", "HTMLElement", "Node", "Event", "KeyboardEvent", "MouseEvent"]) Object.defineProperty(globalThis, key, { configurable: true, value: key === "window" ? dom.window : dom.window[key] });
globalThis.getComputedStyle = dom.window.getComputedStyle.bind(dom.window);
globalThis.requestAnimationFrame = dom.window.requestAnimationFrame.bind(dom.window);
globalThis.cancelAnimationFrame = dom.window.cancelAnimationFrame.bind(dom.window);
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
dom.window.HTMLElement.prototype.getClientRects = function () { return this.isConnected && !this.hidden ? [{ width: 1, height: 1, top: 0, left: 0, right: 1, bottom: 1 }] : []; };
dom.window.HTMLElement.prototype.scrollIntoView = function () {};
const { createRoot } = await import("react-dom/client");
const { NewsWorkspace } = await vite.ssrLoadModule("/src/components/NewsWorkspace.jsx");
const { MaterialDialog } = await vite.ssrLoadModule("/src/components/dialogs.jsx");
const originalFetch = globalThis.fetch;
const rooms = [{ id: "room-a", title: "宏观研究" }, { id: "room-b", title: "公司公告研究" }];
const item = { id: "news-synthetic-1", title: "合成公告标题", source_name: "合成官方订阅", source_id: "synthetic", url: "https://example.test/news/1", published_at: null, discovered_at: "2026-10-19T01:23:00Z", excerpt: "合成来源摘录，不是完整公告。", summary: "合成本机整理要点。", limitations: ["仅订阅摘录，尚未核验全文。"], read: false };
const overview = { enabled: false, running: true, sources: [], categories: [], counts: {}, ai: { configured: false } };
let root, host, unexpectedFetch, writes, reads, prepared, closed;
const deferred = () => { let resolve, reject; const promise = new Promise((done, fail) => { resolve = done; reject = fail; }); return { promise, resolve, reject }; };
const button = (label) => [...host.querySelectorAll("button")].find((entry) => entry.textContent.trim() === label);
async function click(target) { assert.ok(target); await act(async () => target.click()); }
async function choose(target, value) { await act(async () => { target.value = value; target.dispatchEvent(new Event("change", { bubbles: true })); }); }
async function render(element) { await act(async () => root.render(element)); }
async function settle(predicate) {
  for (let i = 0; i < 160 && !predicate(); i++) await act(async () => new Promise((resolve) => setTimeout(resolve, 10)));
  assert.ok(predicate(), "expected UI did not appear before the bounded wait ended");
}
const client = { newsOverview: async () => ({ overview }), newsItems: async () => ({ items: [item], total: 1 }), newsItem: async () => ({ item }) };
async function openPicker(onPrepareResearch = async (...args) => { prepared.push(args); }) {
  await render(React.createElement(NewsWorkspace, { apiClient: client, rooms, onOpenRooms: () => {}, onPrepareResearch }));
  await click(host.querySelector(".news-item"));
  await click(button("带到讨论房间"));
}

test.beforeEach(() => {
  unexpectedFetch = []; writes = []; reads = []; prepared = []; closed = 0;
  globalThis.fetch = async (url) => { unexpectedFetch.push(url); throw new Error("No network allowed in this synthetic test"); };
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
test.afterEach(async () => { await act(async () => root.unmount()); document.body.replaceChildren(); assert.deepEqual(unexpectedFetch, []); globalThis.fetch = originalFetch; });
test.after(async () => { dom.window.close(); await vite.close(); });

test("draft preserves source, full title, separate timestamps and unknown publication without implying verification", () => {
  const draft = buildNewsResearchDraft(item, rooms[1], "handoff-1");
  assert.equal(draft.metadata.published_at, "");
  assert.equal(draft.metadata.source_type, "internal_note");
  assert.equal(draft.source_url, item.url);
  assert.match(draft.content, /原文发布时间：未知/);
  assert.match(draft.content, /本机发现时间：2026-10-19T01:23:00Z/);
  assert.match(draft.content, /https:\/\/example.test\/news\/1/);
  assert.match(draft.content, /本机消息编号：news-synthetic-1/);
  assert.match(draft.content, /本机整理（规则生成，未核验）/);
  assert.match(draft.content, /不是全文或事实核验/);
  assert.equal(draft._news_research.room_id, "room-b");
});

test("long titles are visibly shortened in the form but retained in full in the content; oversize content is rejected", () => {
  const title = "完整原标题".repeat(30);
  const draft = buildNewsResearchDraft({ ...item, title }, rooms[0], "handoff-1");
  assert.equal(draft.title.length, 120);
  assert.equal(draft._news_research.title_shortened, true);
  assert.ok(draft.content.includes(title));
  assert.throws(() => buildNewsResearchDraft({ ...item, excerpt: "x".repeat(50000) }, rooms[0], "handoff-2"), /超过资料长度上限/);
});

test("invalid or timezone-free publication times and credential-bearing links are never promoted into source data", () => {
  for (const published_at of ["2026-02-30T12:00:00Z", "2026-10-19T12:00:00", "yesterday", null]) {
    const draft = buildNewsResearchDraft({ ...item, published_at, url: "https://user:secret@example.test/news" }, rooms[0], "handoff");
    assert.equal(draft.metadata.published_at, "");
    assert.match(draft.content, /原文发布时间：未知/);
    assert.ok(!draft.content.includes("secret"));
  }
  const draft = buildNewsResearchDraft({ ...item, published_at: "2026-10-18T09:30:00-04:00" }, rooms[0], "handoff");
  assert.equal(draft.metadata.published_at, "2026-10-18T09:30:00-04:00");
});

test("source links exceeding the stored 2000-character limit fail before creating a truncated draft", () => {
  const longUrl = `https://example.test/news?q=${"a".repeat(2000)}`;
  assert.throws(() => buildNewsResearchDraft({ ...item, url: longUrl }, rooms[0], "long-link"), /超过 2000 字符.*未创建草稿/);
  const draft = buildNewsResearchDraft(item, rooms[0], "normal-link");
  const badContext = { ...draft._news_research, source_url: longUrl };
  assert.throws(() => newsResearchSubmission(draft, badContext, "room-a"), /尚未发送保存请求/);
});

test("submission binds the chosen room, strips frontend control metadata and rejects a changed or cancelled context", () => {
  const draft = buildNewsResearchDraft(item, rooms[1], "handoff-1");
  const context = draft._news_research;
  const submission = newsResearchSubmission(draft, context, "room-b");
  assert.equal(submission.roomId, "room-b");
  assert.equal(submission.payload._news_research, undefined);
  assert.equal(submission.payload.metadata.source_type, "internal_note");
  assert.equal(submission.payload.source_url, item.url);
  assert.throws(() => newsResearchSubmission(draft, context, "room-a"), /目标房间已改变/);
  assert.throws(() => newsResearchSubmission(draft, null, "room-b"), /交接已取消/);
  assert.throws(() => newsResearchSubmission({ ...draft, _news_research: { ...context, handoff_id: "new" } }, context, "room-b"), /交接已取消/);
  assert.throws(() => newsResearchSubmission({ ...draft, content: "x".repeat(50001) }, context, "room-b"), /尚未发送保存请求/);
  assert.equal(newsResearchStillCurrent(context, context, "room-b"), true);
  assert.equal(newsResearchStillCurrent(context, { ...context }, "room-b"), false, "late replies cannot close a later draft even in the same room");
});

test("news picker requires an explicit room and cancel does not send a handoff or write", async () => {
  await openPicker();
  const select = host.querySelector(".news-research-picker select");
  assert.equal(select.value, ""); assert.ok(document.activeElement === select, "room selection receives focus");
  assert.equal(button("打开资料草稿").disabled, true);
  await choose(select, "room-b"); assert.equal(button("打开资料草稿").disabled, false);
  await click(button("取消交接"));
  assert.deepEqual(prepared, []); assert.deepEqual(writes, []);
  assert.ok(document.activeElement === button("带到讨论房间"), "cancel returns keyboard focus to the handoff button");
});

test("pending room preparation is cancellable and its failed late reply cannot reopen the selection", async () => {
  const request = deferred(); let signal;
  await openPicker((news, roomId, options) => { prepared.push([news.id, roomId]); signal = options.signal; return request.promise; });
  await choose(host.querySelector(".news-research-picker select"), "room-b");
  await click(button("打开资料草稿")); assert.equal(button("正在打开房间…").disabled, true);
  await click(button("取消交接")); assert.equal(signal.aborted, true);
  await act(async () => request.resolve());
  assert.equal(host.querySelector(".news-research-picker"), null);
  assert.deepEqual(prepared, [[item.id, "room-b"]]);
});

test("existing material dialog previews the chosen room, disables refetch modes and cancels with no save", async () => {
  const draft = buildNewsResearchDraft(item, rooms[1], "handoff-1");
  await render(React.createElement(MaterialDialog, { material: draft, room: rooms[1], open: true, onClose: () => { closed++; }, onSubmit: async (payload) => writes.push(payload) }));
  assert.match(host.textContent, /保存到：公司公告研究/);
  assert.ok(button("添加资料"));
  assert.equal(button("抓取网页"), undefined);
  assert.equal(button("上传文件"), undefined);
  assert.match(host.querySelector("textarea.large").value, /原文发布时间：未知/);
  assert.equal([...host.querySelectorAll("select")].find((entry) => entry.value === "internal_note").disabled, true);
  await click(button("取消")); assert.equal(closed, 1); assert.deepEqual(writes, []);
});

test("dialog saves only on the original explicit submit and blocks a target room change", async () => {
  const draft = buildNewsResearchDraft(item, rooms[1], "handoff-1");
  const props = { material: draft, room: rooms[1], open: true, onClose: () => {}, onSubmit: async (payload) => writes.push(payload) };
  await render(React.createElement(MaterialDialog, props)); assert.deepEqual(writes, []);
  await click(button("添加资料")); assert.equal(writes.length, 1); assert.equal(writes[0]._news_research.room_id, "room-b");
  await render(React.createElement(MaterialDialog, { ...props, room: rooms[0] }));
  assert.equal(button("添加资料").disabled, true);
  await act(async () => host.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  assert.equal(writes.length, 1); assert.match(host.textContent, /目标房间已改变/);
});

test("a different message handoff in the same room initializes a fresh visible draft instead of reusing prior edits", async () => {
  const props = { room: rooms[1], open: true, onClose: () => {}, onSubmit: async () => {} };
  await render(React.createElement(MaterialDialog, { ...props, material: buildNewsResearchDraft(item, rooms[1], "one") }));
  await render(React.createElement(MaterialDialog, { ...props, material: buildNewsResearchDraft({ ...item, id: "two", title: "第二条消息", excerpt: "第二条原文" }, rooms[1], "two") }));
  assert.equal(host.querySelector('input[maxlength="120"]').value, "第二条消息");
  assert.match(host.querySelector("textarea.large").value, /第二条原文/);
  assert.doesNotMatch(host.querySelector("textarea.large").value, /合成来源摘录/);
});

function roomData(roomId, professional = false) {
  return { room: { ...rooms.find((entry) => entry.id === roomId), context: "", capability_pack_ids: [], capabilities: professional && roomId === "room-b" ? ["market.storage.readonly", "analytics.storage", "simulation.observations", "simulation.paper_portfolio"] : [] }, members: [], messages: [], materials: [], artifacts: [], providers: [] };
}
function installAppApi({ roomRequest, saveRequest, snapshotRequest, professional = false, initialRoom = "room-a", allowMarket = false } = {}) {
  globalThis.fetch = async (url, options = {}) => {
    const path = String(url).split("?")[0]; const method = options.method || "GET";
    const respond = (data, ok = true) => ({ ok, status: ok ? 200 : 500, json: async () => data });
    if (method === "POST") {
      writes.push({ path, body: JSON.parse(options.body) });
      if (path !== "/api/rooms/room-b/materials") { unexpectedFetch.push(`${method} ${path}`); throw new Error("unexpected write"); }
      return saveRequest ? saveRequest(writes.at(-1)) : respond({ ok: true, material: { ...writes.at(-1).body, id: "material-1", room_id: "room-b", version: 1 } });
    }
    reads.push(path);
    if (path === "/api/bootstrap") return respond({ ok: true, rooms, active: roomData(initialRoom, professional), providers: [], templates: [], member_templates: [] });
    if (path === "/api/news/overview") return respond({ overview });
    if (path === "/api/news/items") return respond({ items: [item], total: 1 });
    if (path === `/api/news/items/${item.id}`) return respond({ item });
    if (path === "/api/rooms/room-b") return roomRequest ? roomRequest() : respond(roomData("room-b", professional));
    if (path === "/api/rooms/room-a") return respond(roomData("room-a"));
    if (path.endsWith("/convergence")) return respond({ convergence: {} });
    if (path.endsWith("/plugin-registry")) return respond({ registry: null });
    if (path === "/api/rooms/room-b/action-desk" || path === "/api/rooms/room-b/action-desk/continuations") return respond({ ok: false, error: "合成夹具未配置行动台，只读错误不影响资料交接。" }, false);
    if (allowMarket && path === "/api/market/storage/snapshot") return snapshotRequest ? snapshotRequest(String(url), options) : respond({ snapshot: { state: "offline", quotes: [], source_errors: ["synthetic-offline"] } });
    if (allowMarket && path === "/api/market/futu/status") return respond({ status: { state: "offline", connected: false } });
    unexpectedFetch.push(`${method} ${path}`); throw new Error("unexpected read");
  };
}
async function openAppHandoff() {
  const { default: App } = await vite.ssrLoadModule("/src/App.jsx");
  await render(React.createElement(App));
  await settle(() => host.querySelector(".news-item"));
  await click(host.querySelector(".news-item"));
  await click(button("带到讨论房间"));
  await choose(host.querySelector(".news-research-picker select"), "room-b");
  await click(button("打开资料草稿"));
}

test("real App wiring opens the selected room and writes only the original confirmed material POST", async () => {
  installAppApi(); await openAppHandoff();
  await settle(() => button("添加资料") && host.querySelector("#inspector-materials"));
  assert.match(host.querySelector('[role="dialog"]').textContent, /保存到：公司公告研究/);
  assert.deepEqual(writes, []);
  await click(button("添加资料"));
  assert.equal(writes.length, 1); assert.equal(writes[0].path, "/api/rooms/room-b/materials");
  assert.equal(writes[0].body._news_research, undefined);
  assert.equal(writes[0].body.metadata.published_at, "");
  assert.equal(writes[0].body.metadata.source_type, "internal_note");
  assert.equal(writes[0].body.source_url, item.url);
  assert.equal(writes[0].body.kind, "note");
  assert.match(writes[0].body.content, /本机发现时间：2026-10-19T01:23:00Z/);
  assert.equal(host.querySelector(".material-dialog"), null);
});

test("real App cancellation of the material dialog performs zero write requests", async () => {
  installAppApi(); await openAppHandoff();
  await settle(() => button("添加资料") && host.querySelector("#inspector-materials"));
  await click(button("取消"));
  assert.equal(host.querySelector(".material-dialog"), null);
  assert.deepEqual(writes, []);
});

test("real App ignores cancelled room reads and never opens or saves a late handoff", async () => {
  const request = deferred(); installAppApi({ roomRequest: () => request.promise });
  await openAppHandoff();
  assert.ok(button("正在打开房间…"));
  await click(button("取消交接"));
  await act(async () => request.resolve({ ok: true, status: 200, json: async () => roomData("room-b") }));
  assert.ok(host.querySelector(".news-workspace"));
  assert.equal(host.querySelector(".material-dialog"), null);
  assert.deepEqual(writes, []);
});

test("ambiguous App save failure advises checking the target room and never retries on its own", async () => {
  installAppApi({ saveRequest: async () => { throw new Error("合成保存响应中断"); } });
  await openAppHandoff(); await settle(() => button("添加资料") && host.querySelector("#inspector-materials"));
  await click(button("添加资料"));
  assert.equal(writes.length, 1);
  assert.match(host.querySelector(".material-operation-status").textContent, /先核对“公司公告研究”的共享资料.*避免重复/);
  await act(async () => new Promise((resolve) => setTimeout(resolve, 30)));
  assert.equal(writes.length, 1);
});

test("a late App save response remains bound to its original room and cannot close a newly opened draft", async () => {
  const saved = deferred(); installAppApi({ saveRequest: () => saved.promise });
  await openAppHandoff(); await settle(() => button("添加资料") && host.querySelector("#inspector-materials"));
  await click(button("添加资料")); assert.equal(writes.length, 1);
  await click(host.querySelector('.rail-button[data-section="news"]'));
  await settle(() => host.querySelector(".news-item"));
  await click(host.querySelector(".news-item")); await click(button("带到讨论房间"));
  await choose(host.querySelector(".news-research-picker select"), "room-b"); await click(button("打开资料草稿"));
  await settle(() => button("添加资料"));
  await act(async () => saved.resolve({ ok: true, status: 200, json: async () => ({ ok: true, material: { ...writes[0].body, id: "material-late", room_id: "room-b", version: 1 } }) }));
  assert.ok(host.querySelector(".material-dialog"), "the late response must not close the new draft");
  assert.equal(button("添加资料").disabled, false);
  assert.equal(writes.length, 1);
  assert.equal(writes[0].path, "/api/rooms/room-b/materials");
});

test("opening and saving a news draft in a professional market room never auto-reads market or external source APIs", async () => {
  installAppApi({ professional: true }); await openAppHandoff();
  await settle(() => button("添加资料") && host.querySelector("#inspector-materials"));
  assert.match(host.querySelector(".material-dialog").textContent, /不会自动刷新房间的行情或其他来源/);
  assert.deepEqual(reads.filter((path) => path.startsWith("/api/market/")), []);
  await click(button("添加资料"));
  assert.equal(writes.length, 1);
  assert.deepEqual(reads.filter((path) => path.startsWith("/api/market/")), []);
});

test("cancelling a draft in a professional market room leaves market data unrequested and creates no material", async () => {
  installAppApi({ professional: true }); await openAppHandoff();
  await settle(() => button("添加资料") && host.querySelector("#inspector-materials"));
  await click(button("取消"));
  assert.deepEqual(writes, []);
  assert.deepEqual(reads.filter((path) => path.startsWith("/api/market/")), []);
});

test("professional bootstrap stays read-only on the news homepage and ordinary entry to research preserves existing market reads", async () => {
  installAppApi({ professional: true, initialRoom: "room-b", allowMarket: true });
  const { default: App } = await vite.ssrLoadModule("/src/App.jsx");
  await render(React.createElement(App)); await settle(() => host.querySelector(".news-item"));
  assert.deepEqual(reads.filter((path) => path.startsWith("/api/market/")), []);
  await click(host.querySelector('.rail-button[data-section="rooms"]'));
  assert.ok(reads.includes("/api/market/storage/snapshot"));
  assert.ok(reads.includes("/api/market/futu/status"));
  assert.deepEqual(writes, []);
});

test("manual market refresh settles after ordinary navigation within the same professional room", async () => {
  const request = deferred(); let manualRequests = 0;
  const response = () => ({ ok: true, status: 200, json: async () => ({ snapshot: { state: "offline", quotes: [], source_errors: ["synthetic-offline"] } }) });
  installAppApi({ professional: true, initialRoom: "room-b", allowMarket: true, snapshotRequest: (url) => {
    if (url.includes("force=1")) { manualRequests++; return request.promise; }
    return response();
  } });
  const { default: App } = await vite.ssrLoadModule("/src/App.jsx");
  await render(React.createElement(App)); await settle(() => host.querySelector(".news-item"));
  await click(host.querySelector('.rail-button[data-section="materials"]'));
  await settle(() => host.querySelector(".market-refresh"));
  await click(host.querySelector(".market-refresh"));
  assert.equal(manualRequests, 1); assert.equal(host.querySelector(".market-refresh").disabled, true);
  await click(host.querySelector('.rail-button[data-section="members"]'));
  await click(host.querySelector('.rail-button[data-section="rooms"]'));
  await act(async () => request.resolve(response()));
  await click(host.querySelector('.rail-button[data-section="materials"]'));
  await settle(() => host.querySelector(".market-refresh"));
  assert.equal(host.querySelector(".market-refresh").disabled, false, "completed current request releases its spinner despite same-room navigation");
  await click(host.querySelector(".market-refresh"));
  assert.equal(manualRequests, 2, "refresh is usable again after the first request settled");
});

test("a cancelled handoff's later room-read failure does not leak into the next ordinary room page", async () => {
  const request = deferred(); installAppApi({ roomRequest: () => request.promise });
  await openAppHandoff(); await click(button("取消交接"));
  await act(async () => request.reject(new Error("合成已取消交接的旧错误")));
  await click(host.querySelector('.rail-button[data-section="rooms"]'));
  assert.doesNotMatch(host.textContent, /合成已取消交接的旧错误/);
  assert.deepEqual(writes, []);
});

test("an invalidated older market response cannot repopulate the cleared view while a newer request is pending", async () => {
  const oldManual = deferred(); const newAutomatic = deferred(); let automaticRequests = 0; let oldSignal;
  const response = () => ({ ok: true, status: 200, json: async () => ({ snapshot: { state: "offline", quotes: [], source_errors: ["synthetic-offline"] } }) });
  installAppApi({ professional: true, initialRoom: "room-b", allowMarket: true, snapshotRequest: (url, options) => {
    if (url.includes("force=1")) { oldSignal = options.signal; return oldManual.promise; }
    automaticRequests++;
    return automaticRequests === 1 ? response() : newAutomatic.promise;
  } });
  const { default: App } = await vite.ssrLoadModule("/src/App.jsx");
  await render(React.createElement(App)); await settle(() => host.querySelector(".news-item"));
  await click(host.querySelector('.rail-button[data-section="materials"]')); await settle(() => host.querySelector(".market-refresh"));
  await click(host.querySelector(".market-refresh"));
  await click(host.querySelector('.rail-button[data-section="news"]'));
  assert.equal(oldSignal.aborted, true);
  await click(host.querySelector('.rail-button[data-section="materials"]'));
  await settle(() => host.querySelector("#inspector-materials"));
  assert.equal(automaticRequests, 2);
  assert.equal(host.querySelector(".market-refresh"), null, "no old snapshot is shown while the newly selected context is unconfirmed");
  await act(async () => oldManual.resolve(response()));
  assert.equal(host.querySelector(".market-refresh"), null, "old response must not repopulate the cleared market panel");
  await act(async () => newAutomatic.resolve(response()));
  await settle(() => host.querySelector(".market-refresh"));
  assert.equal(host.querySelector(".market-refresh").disabled, false);
});


test("research handoff preserves quality warnings and does not promote anomalous time to publication metadata", () => {
  const material_quality = { freshness: { clock_anomaly: true }, warnings: ["发布时间异常，发布未确认。", "全文尚未读取。"] };
  const draft = buildNewsResearchDraft({ ...item, published_at: "2100-01-01T00:00:00Z", material_quality }, rooms[0], "clock-handoff");
  assert.equal(draft.metadata.published_at, "");
  assert.match(draft.content, /来源标注时间（未确认）：2100-01-01T00:00:00Z/);
  assert.match(draft.content, /发布时间异常，发布未确认/);
  assert.match(draft.content, /全文尚未读取/);
  assert.doesNotMatch(draft.content, /原文发布时间：2100/);
});


test("revised RSS handoff retains current version time and first discovery as distinct fields", () => {
  const updated = { ...item, published_at: "2026-10-19T14:17:10Z", discovered_at: "2026-10-15T16:21:32Z", material_quality: { freshness: { clock_anomaly: false, revision_after_discovery: true }, warnings: ["首次发现时间不代表本次更新发现时间。"] } };
  const draft = buildNewsResearchDraft(updated, rooms[0], "revised-handoff");
  assert.equal(draft.metadata.published_at, updated.published_at);
  assert.match(draft.content, /当前版本标注发布时间：2026-10-19T14:17:10Z/);
  assert.match(draft.content, /本机发现时间：2026-10-15T16:21:32Z/);
  assert.match(draft.content, /首次发现时间不代表本次更新发现时间/);
});
