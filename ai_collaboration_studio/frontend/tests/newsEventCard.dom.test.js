import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { createServer } from "vite";
import { buildNewsResearchDraft } from "../src/newsResearch.js";

const vite = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)), appType: "custom", logLevel: "silent", server: { hmr: false, middlewareMode: true } });
const dom = new JSDOM("<!doctype html><body></body>", { pretendToBeVisual: true, url: "http://event-evidence.test/" });
for (const key of ["window", "document", "navigator", "HTMLElement", "Node", "Event"]) Object.defineProperty(globalThis, key, { configurable: true, value: key === "window" ? dom.window : dom.window[key] });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const { createRoot } = await import("react-dom/client");
const { NewsWorkspace } = await vite.ssrLoadModule("/src/components/NewsWorkspace.jsx");
const { NewsEventCard } = await vite.ssrLoadModule("/src/components/NewsEventCard.jsx");
const { AnalysisReview, AnalysisEvidence } = await vite.ssrLoadModule("/src/components/NewsContentQuality.jsx");
const { NewsMarketMaterial, marketSourceTime } = await vite.ssrLoadModule("/src/components/NewsMarketMaterial.jsx");
let host, root, item, client, calls, overview;
const originalFetch = globalThis.fetch;
const button = label => [...host.querySelectorAll("button")].find(node => node.textContent.trim() === label);
async function click(target) { assert.ok(target); await act(async () => target.click()); }
async function render() { await act(async () => root.render(React.createElement(NewsWorkspace, { apiClient: client, rooms: [{ id: "room-a", title: "研究室" }], onPrepareResearch() {}, onOpenRooms() {} }))); await click(host.querySelector(".news-item")); }
test.beforeEach(() => {
  const prior = { id: "v1", version_number: 1, content_status: "excerpt_only", version_seen_at: "2026-10-28T01:00:00Z", evidence: { status: "excerpt_only", body_text: "旧版合成内容" }, changes: [], ai_analysis: { status: "completed", summary: "旧版保存的合成解释", key_points: [] } };
  const current = { id: "v2", version_number: 2, content_status: "partial_text", version_seen_at: "2026-10-29T01:00:00Z", source_updated_at: null, evidence: { content_status: "partial_text", body_text: "新正文", segments: [{ id: "p1", text: "合成关键限制：仅在审批后生效。", kind: "paragraph" }, { id: "t1", text: "合成财务表：收入 100，币种美元。", kind: "table" }], truncated: true, attachments: [{ url: "https://www.sec.gov/example/attachment.pdf", status: "unsupported", read: false }], symbols: ["MU"] }, changes: [{ field: "excerpt", before: "合成收入 90", after: "合成收入 100" }] };
  item = { id: "synthetic-event", title: "合成官方公告", url: "https://www.sec.gov/example", source_id: "sec-mu", source_name: "MU SEC", excerpt: "RSS 合成摘录", read: true, current_version_id: "v2", evidence: current.evidence, event: { event_id: "event1", current_version: current, versions: [current, prior], version_count: 2, requires_review: true, independent_evidence_count: 1, sources: [{ item_id: "one", source_name: "MU SEC", url: "https://www.sec.gov/example", relation: "original" }, { item_id: "two", source_name: "合成转载", url: "https://example.test/reprint", relation: "reprint", origin_url: "https://www.sec.gov/example" }] }, ai_analysis: { status: "completed", summary: "当前版本合成分析", key_points: [], uncertainties: [] } };
  calls = { read: [], source: [], versions: 0, network: 0 };
  overview = { enabled: false, running: true, categories: [], counts: { total: 1 }, analysis_queue: { pending: 125, unknown: 1 }, ai: { configured: false }, sources: [{ id: "sec-mu", name: "MU SEC", enabled: false, can_configure: true, coverage: "MU 官方申报" }] };
  client = { newsOverview: async () => ({ overview }), newsItems: async () => ({ items: [item], total: 1 }), newsItem: async () => ({ item }), newsVersions: async () => { calls.versions++; return { versions: item.event.versions }; }, newsReadEvidence: async (id, attachments) => { calls.read.push({ id, attachments }); return { item }; }, newsSourceControl: async (id, enabled) => { calls.source.push({ id, enabled }); overview = { ...overview, sources: overview.sources.map(x => ({ ...x, enabled })) }; return { overview }; } };
  globalThis.fetch = async () => { calls.network++; throw new Error("No network in event UI fixtures"); };
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
test.afterEach(async () => { await act(async () => root.unmount()); assert.equal(calls.network, 0); document.body.replaceChildren(); globalThis.fetch = originalFetch; });
test.after(async () => { dom.window.close(); await vite.close(); });

function savedSixQuestionImpact() {
  return { status: "completed", context_id: "saved-market", facts: [{ claim: "已保存的合成事实", quote: "saved source quote", evidence_id: "article:p1" }],
    new_information: [], inferred_impacts: [], unknowns: ["尚缺预期"], counter_evidence: [],
    research_actions: [{ kind: "verify", action: "核对合成审批条件" }], judgment_change_conditions: ["若官方更正，则需重新核对判断"] };
}

test("saved market answers render once through the real parent after cold reopening", async () => {
  item.market_context = { context_id: "saved-market", room_id: null, round_id: null, consensus: [], market: {}, missing: [] };
  item.market_impact = savedSixQuestionImpact();
  const saved = JSON.stringify(item.market_impact);
  client.newsMarketContext = async () => assert.fail("Reading saved answers must not prepare a new context");
  client.newsMarketAnalyze = async () => assert.fail("Reading saved answers must not send a model request");
  const assertSingle = () => {
    assert.equal(host.querySelectorAll(".news-event-market").length, 1);
    assert.equal([...host.querySelectorAll("h5")].filter(node => node.textContent === "1. 发生了什么？").length, 1);
    assert.equal([...host.querySelectorAll("h5")].filter(node => node.textContent === "6. 什么新证据会改变当前判断？").length, 1);
    assert.equal(host.querySelector(".news-event-card .news-event-market"), null);
    assert.ok(host.querySelector(".news-market-context .news-event-market"));
    assert.equal(JSON.stringify(item.market_impact), saved);
  };
  await render(); assertSingle();
  await act(async () => root.unmount()); root = createRoot(host);
  await render(); assertSingle();
  await click(button("查看版本历史（2）"));
  await click([...host.querySelectorAll(".news-event-history button")].find(node => node.textContent.includes("版本 1")));
  assert.equal(host.querySelector(".news-event-market"), null, "current market answers must not appear in historical reading");
  await click(button("返回当前版本")); assertSingle();
});

test("standalone event cards retain their saved market answers by default", async () => {
  item.market_impact = savedSixQuestionImpact();
  await act(async () => root.render(React.createElement(NewsEventCard, { item, apiClient: client, busy: false, onRead() {} })));
  assert.equal(host.querySelectorAll(".news-event-card .news-event-market").length, 1);
  assert.match(host.textContent, /核对合成审批条件/);
});

test("a parent client without the direct analysis capability retains the card fallback", async () => {
  item.market_impact = savedSixQuestionImpact();
  await render();
  assert.equal(host.querySelectorAll(".news-event-market").length, 1);
  assert.ok(host.querySelector(".news-event-card .news-event-market"));
  assert.equal(host.querySelector(".news-market-context"), null);
});

test("renders distinct content, attachment, source count, update and queue facts without automatic fetching", async () => {
  await render();
  assert.match(host.textContent, /正文不完整/);
  assert.match(host.textContent, /0 \/ 1 已取得文本（完整性另列）/);
  assert.match(host.textContent, /2 处来源 · 1 份原始依据/);
  assert.match(host.textContent, /合成收入 90.*合成收入 100/s);
  assert.match(host.textContent, /125 待处理.*1 结果未确认/s);
  assert.match(host.textContent, /尚未取得可核验的发布前市场预期/);
  assert.deepEqual(calls.read, []); assert.deepEqual(calls.source, []); assert.equal(calls.versions, 0);
});

test("body evidence does not display the old RSS-only unread-fulltext disclaimer", async () => {
  item.material_quality = { scope: { kind: "article_evidence", full_text_read: false }, can_analyze: true };
  item.limitations = ["仅整理官方订阅中的标题与摘录，未抓取或验证全文。", "其他来源条件仍需核对。"];
  await render();
  assert.doesNotMatch(host.querySelector(".news-limitations").textContent, /未抓取或验证全文/);
  assert.match(host.querySelector(".news-limitations").textContent, /其他来源条件仍需核对/);
  assert.equal(item.limitations.length, 2, "saved legacy data is not rewritten");
});

test("body and optional attachments require an explicit click and preserve exact selected item", async () => {
  await render();
  const checkbox = host.querySelector(".news-event-read input");
  await click(checkbox); await click(button("读取官方正文"));
  assert.deepEqual(calls.read, [{ id: item.id, attachments: true }]);
  assert.match(host.querySelector(".news-notice").textContent, /读取结果已保存/);
});

test("historical version hides current AI and current research action, then returns to current state", async () => {
  await render(); await click(button("查看版本历史（2）"));
  assert.equal(calls.versions, 1);
  await click([...host.querySelectorAll(".news-event-history button")].find(node => node.textContent.includes("版本 1")));
  assert.match(host.querySelector(".news-event-evidence").textContent, /旧版合成内容/);
  assert.equal(host.querySelector(".news-ai-reading"), null);
  assert.equal(button("带到讨论房间"), undefined);
  assert.equal(button("读取官方正文"), undefined);
  assert.equal(host.querySelector(".news-excerpt"), null);
  assert.match(host.querySelector(".news-event-history-analysis").textContent, /旧版保存的合成解释/);
  await click(button("返回当前版本"));
  assert.match(host.querySelector(".news-ai-reading").textContent, /当前版本合成分析/);
});

test("new source remains disabled until chosen explicitly and reading does not enable AI", async () => {
  await render(); assert.match(host.querySelector(".news-coverage-scope").textContent, /尚未启用来源/);
  await click(button("启用这个来源"));
  assert.deepEqual(calls.source, [{ id: "sec-mu", enabled: true }]);
  assert.match(host.querySelector(".news-coverage-scope").textContent, /MU SEC/);
  assert.match(host.querySelector(".news-notice").textContent, /不启用 AI/);
});

test("research handoff retains version, evidence locators, attachment scope and known symbols", () => {
  item.evidence.content_hash = "a".repeat(64);
  const segments = item.evidence.segments.map(row => ({ ...row, source_segment_id: row.id, document_id: "article:synthetic", url: item.url, start: 0, end: Array.from(row.text).length, truncated: false }));
  const groups = segments.map(row => ({ id: `article:synthetic:${row.id}`, document_id: row.document_id, kind: "paragraph", url: item.url, required_segment_ids: [row.id] }));
  const pack = { version_id: "v2", content_hash: item.evidence.content_hash, package_hash: "b".repeat(64), groups, selection: segments.map(({ text, ...row }) => row), omitted_group_count: 0 };
  item.evidence_preview = { schema: "news_evidence_preview_v1", status: "ready", version_id: "v2", content_hash: item.evidence.content_hash, preview_only: true, model_called: false, material: { source_text: { title: item.title, body: segments.map(row => row.text).join("\n") }, evidence_package: pack }, segments };
  const draft = buildNewsResearchDraft(item, { id: "room-a", title: "研究室" }, "handoff1");
  assert.equal(draft._news_research.version_id, "v2"); assert.equal(draft._news_research.event_id, "event1");
  assert.deepEqual(draft.metadata.symbols, ["MU"]);
  assert.match(draft.content, /"id":"p1".*审批后生效/);
  assert.match(draft.content, /附件 1：.*未读取/);
  assert.match(draft.content, /来源抽取记录包含截断/);
  assert.match(draft.content, /来源修订时间：未知/);
});

test("body research refuses raw segment fallback when its bound preview is absent", () => {
  item.evidence.segments = [{ id: "p1", text: "有界段落" }, { id: "p2", text: "超长".repeat(15000) }];
  assert.throws(() => buildNewsResearchDraft(item, { id: "room-a", title: "研究室" }, "handoff1"), /没有可信的完整证据组预览/);
});

test("body review requires matching version and exact segment bounds; old result cannot point at new evidence", async () => {
  const quote = item.evidence.segments[0].text;
  const analysis = { status: "needs_review", article_version_id: "v2", candidate_result: { key_points: ["合成解释"], evidence: [{ claim: "合成解释", quote, evidence_field: "body", locator: { segment_id: "p1", start: 0, end: quote.length } }] }, semantic_review: { issues: [{ field: "key_points.0.claim", evidence_fields: ["key_points.0.quote"] }] } };
  await act(async () => root.render(React.createElement(AnalysisReview, { analysis, source: item })));
  assert.equal(host.querySelector(".news-review-support blockquote").textContent, quote);
  await act(async () => root.render(React.createElement(AnalysisReview, { analysis: { ...analysis, article_version_id: "v1" }, source: item })));
  assert.equal(host.querySelector(".news-review-support"), null);
  assert.match(host.textContent, /无法与当前保存的来源准确对应/);
  await act(async () => root.render(React.createElement(AnalysisEvidence, { analysis: { scope: { kind: "article_evidence" }, evidence: analysis.candidate_result.evidence } })));
  assert.match(host.textContent, /正文 · p1/); assert.doesNotMatch(host.textContent, /全文尚未读取/);
});

test("market context binds frozen versions and requires a separate explicit model confirmation", async () => {
  const prepared = [], sent = []; overview.ai.configured = true;
  client.newsMarketOptions = async room => ({ rounds: [{ id: "round-a", created_at: 1793150000000, materials: [{ id: "material-a", version: 3, title: "合成发布前共识" }] }] });
  client.newsMarketContext = async (id, payload) => { prepared.push({ id, payload }); return { market_context: { context_id: "context-a", missing: ["尚不能判断是否超预期。"], consensus: [], market: { captured_at: null } }, market_impact: null }; };
  client.newsMarketAnalyze = async (id, context) => { sent.push({ id, context }); return { market_impact: { status: "queued" } }; };
  await render(); await click(host.querySelector(".news-market-context summary"));
  const choose = async (select, value) => act(async () => { select.value = value; select.dispatchEvent(new Event("change", { bubbles: true })); });
  await choose(host.querySelector(".news-market-context select"), "room-a");
  await choose(host.querySelectorAll(".news-market-context select")[1], "round-a");
  await click(host.querySelector(".news-market-context fieldset input"));
  await click(button("绑定所选资料（不调用 AI）"));
  assert.deepEqual(prepared, [{ id: item.id, payload: { room_id: "room-a", round_id: "round-a", material_refs: [{ material_id: "material-a", version: 3 }] } }]);
  assert.deepEqual(sent, []); assert.match(host.querySelector(".news-market-bound").textContent, /尚不能判断是否超预期/);
  await click(button("核对市场分析发送范围")); assert.deepEqual(sent, []);
  await click(button("暂不发送市场分析")); assert.deepEqual(sent, []);
  await click(button("核对市场分析发送范围")); await click(button("确认分析市场影响"));
  assert.deepEqual(sent, [{ id: item.id, context: "context-a" }]);
  assert.match(host.querySelector(".news-market-bound").textContent, /市场分析已排队/);
});

test("automatic body reading is an explicit independent policy, and absent quality samples remain unknown", async () => {
  overview.article_reading = { enabled: false };
  overview.quality = { body_reads: { full_text_rate: null, readable_text_rate: null }, source_checks: [{ source_id: "sec-mu", name: "MU SEC", success_rate: null, not_modified: 0, failed: 0, unknown_or_in_progress: 0 }], latency: {}, coverage: {}, judged_metrics: {} };
  const policyCalls = [];
  client.newsArticleReading = async enabled => { policyCalls.push(enabled); overview = { ...overview, article_reading: { enabled } }; return { overview }; };
  await render();
  assert.deepEqual(policyCalls, []); assert.match(host.textContent, /暂无可计算样本/);
  assert.match(host.textContent, /证据不足，尚未评定/);
  assert.doesNotMatch(host.querySelector(".news-quality-table").textContent, /0\.0%/);
  await click(host.querySelector(".news-article-policy input"));
  assert.deepEqual(policyCalls, [true]); assert.equal(overview.ai.configured, false);
  assert.match(host.querySelector(".news-notice").textContent, /AI 设置不变/);
});

test("SEC activation waits for explicit contact entry and never echoes the saved value", async () => {
  overview.sec_contact_configured = false;
  overview.sources[0].source_kind = "sec";
  const contacts = [];
  client.newsSecContact = async value => { contacts.push(value); overview = { ...overview, sec_contact_configured: true }; return { overview }; };
  await render(); assert.equal(button("启用这个来源").disabled, true);
  const input = host.querySelector(".news-sec-contact input");
  await act(async () => { Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, "value").set.call(input, "Synthetic App contact@example.test"); input.dispatchEvent(new Event("input", { bubbles: true })); });
  await click(button("保存 SEC 联系标识"));
  assert.deepEqual(contacts, ["Synthetic App contact@example.test"]); assert.equal(input.value, "");
  assert.doesNotMatch(host.textContent, /contact@example\.test/); assert.equal(button("启用这个来源").disabled, false);
  assert.deepEqual(calls.source, []); assert.deepEqual(calls.read, []);
});

test("saved source relation and withdrawal each require user confirmation, with exact saved quote", async () => {
  const origin = { id: "origin-1", title: "合成原始公告及撤回通知", source_name: "合成官方来源", excerpt: "The issuer withdraws the earlier synthetic announcement." };
  const relationships = [], withdrawals = [];
  client.newsItems = async options => ({ items: options?.query ? [origin] : [item], total: 1 });
  client.newsItem = async id => ({ item: id === origin.id ? origin : item });
  client.newsEventRelation = async (id, originId) => { relationships.push({ id, originId }); return { item }; };
  client.newsWithdrawal = async (id, evidenceId, quote) => { withdrawals.push({ id, evidenceId, quote }); return { item }; };
  await render(); await click(host.querySelector(".news-event-actions summary"));
  const setQuery = async () => { const input = host.querySelector('.news-event-actions input:not([type="checkbox"])'); await act(async () => { Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, "value").set.call(input, "合成公告"); input.dispatchEvent(new Event("input", { bubbles: true })); }); await act(async () => new Promise(resolve => setTimeout(resolve, 300))); };
  const choose = async (select,value) => act(async () => { select.value = value; select.dispatchEvent(new Event("change", { bubbles: true })); });
  await setQuery(); await choose(host.querySelectorAll(".news-event-actions select")[1], origin.id);
  assert.equal(button("保存用户确认的转载关系").disabled, true);
  await click(host.querySelector(".news-event-ack input")); await click(button("保存用户确认的转载关系"));
  assert.deepEqual(relationships, [{ id: item.id, originId: origin.id }]); assert.deepEqual(withdrawals, []);
  await choose(host.querySelector(".news-event-actions select"), "withdrawal");
  await act(async () => new Promise(resolve => setTimeout(resolve, 300)));
  await choose(host.querySelectorAll(".news-event-actions select")[1], origin.id);
  await choose(host.querySelectorAll(".news-event-actions select")[2], "excerpt");
  assert.equal(host.querySelector(".news-event-actions textarea").value, origin.excerpt);
  assert.equal(button("保存用户确认的来源撤回").disabled, true);
  await click(host.querySelector(".news-event-ack input")); await click(button("保存用户确认的来源撤回"));
  assert.deepEqual(withdrawals, [{ id: item.id, evidenceId: origin.id, quote: origin.excerpt }]);
});

test("source time uses the selected time zone and rejects ambiguous or nonexistent New York time", () => {
  assert.equal(marketSourceTime("2026-07-01T09:00", "America/New_York"), "2026-07-01T13:00:00.000Z");
  assert.equal(marketSourceTime("2026-12-01T09:00", "America/New_York"), "2026-12-01T14:00:00.000Z");
  assert.equal(marketSourceTime("2026-10-29T09:00", "Asia/Shanghai"), "2026-10-29T01:00:00.000Z");
  assert.throws(() => marketSourceTime("2026-03-08T02:30", "America/New_York"), /不存在或/);
  assert.throws(() => marketSourceTime("2026-11-01T01:30", "America/New_York"), /不存在或/);
});

test("structured market material uses the existing room save contract only after acknowledgment", async () => {
  const writes = [];
  const localClient = { addMaterial: async (roomId, payload) => { writes.push({ roomId, payload }); return { material: { id: "saved-material" } }; } };
  await act(async () => root.render(React.createElement(NewsMarketMaterial, { item, roomId: "room-a", apiClient: localClient, busy: false })));
  const field = label => [...host.querySelectorAll("label")].find(node => node.textContent.startsWith(label)).querySelector("input,textarea");
  const fill = async (node, value) => act(async () => { const prototype = node.tagName === "TEXTAREA" ? dom.window.HTMLTextAreaElement.prototype : dom.window.HTMLInputElement.prototype; Object.getOwnPropertyDescriptor(prototype, "value").set.call(node, value); node.dispatchEvent(new Event("input", { bubbles: true })); });
  await fill(field("所属期间"), "2026-Q3"); await fill(field("数值"), "40");
  await fill(field("来源原文链接"), "https://example.test/consensus");
  await fill(field("来源标注日期与时间"), "2026-10-28T09:00");
  await fill(field("包含数值"), "Revenue consensus was 40 million USD for 2026-Q3.");
  assert.equal(button("保存为房间资料（不运行模型）").disabled, true);
  await click(host.querySelector('.news-market-material input[type="checkbox"]'));
  await click(button("保存为房间资料（不运行模型）"));
  assert.equal(writes.length, 1); assert.equal(writes[0].roomId, "room-a");
  const payload = writes[0].payload;
  assert.equal(payload.kind, "note"); assert.equal(payload.metadata.market_context.value, 40);
  assert.equal(payload.metadata.market_context.known_at, "2026-10-28T01:00:00.000Z");
  assert.equal(payload.metadata.market_context.quote, payload.content);
  assert.equal(payload.created_at, undefined); assert.equal(payload.updated_at, undefined);
  assert.match(host.textContent, /本次没有启动模型或研究轮次/);
});
