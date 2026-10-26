import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { createServer } from "vite";

const vite = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)), appType: "custom", logLevel: "silent", server: { hmr: false, middlewareMode: true } });
const dom = new JSDOM("<!doctype html><body></body>", { pretendToBeVisual: true, url: "http://news-review.test/" });
for (const key of ["window", "document", "navigator", "HTMLElement", "Node", "Event"]) Object.defineProperty(globalThis, key, { configurable: true, value: key === "window" ? dom.window : dom.window[key] });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const { createRoot } = await import("react-dom/client");
const { AnalysisReview, AnalysisEvidence, analysisStateLabel } = await vite.ssrLoadModule("/src/components/NewsContentQuality.jsx");
const originalFetch = globalThis.fetch;
let host, root, fetchCalls;
const source = { title: "Synthetic CPI report", excerpt: "CPI increased 0.6%." };
const makeAnalysis = (changes = {}) => ({
  status: "needs_review", quality_version: "rss_excerpt_reading_v4", raw_model_output: '{"summary":"合成模型原输出"}',
  candidate_result: { summary: "合成摘要：CPI环比上涨0.6%。另一句没有被单独定位。", key_points: ["CPI环比上涨0.6%。"], uncertainties: ["具体比较基准未知。", "采样细节未知。"], evidence: [{ claim: "CPI环比上涨0.6%。", evidence_field: "excerpt", quote: "CPI increased 0.6%." }] },
  semantic_review: { issues: [{ code: "statistical_comparison_basis", status: "unknown", field: "key_points.0.claim", topic: "cpi:month_over_month", evidence_fields: ["key_points.0.quote"] }] },
  ...changes,
});
const render = async (analysis, extra = {}) => { await act(async () => root.render(React.createElement(AnalysisReview, { analysis, source, originalUrl: "https://www.federalreserve.gov/example", ...extra }))); };
const click = async (element) => { assert.ok(element); await act(async () => element.click()); };
test.beforeEach(() => {
  fetchCalls = 0;
  globalThis.fetch = async () => { fetchCalls++; throw new Error("No network permitted in this offline presentation test"); };
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
test.afterEach(async () => { await act(async () => root.unmount()); assert.equal(fetchCalls, 0); document.body.replaceChildren(); globalThis.fetch = originalFetch; });
test.after(async () => { dom.window.close(); await vite.close(); });

test("a source-linked issue exposes the saved claim and its exact quote with a next step", async () => {
  const analysis = makeAnalysis();
  const before = JSON.stringify(analysis);
  await render(analysis);
  assert.equal(host.querySelector(".news-review-claim").textContent, "CPI环比上涨0.6%。");
  assert.equal(host.querySelector(".news-review-support blockquote").textContent, source.excerpt);
  assert.match(host.textContent, /来源没有充分支持这项同比或环比口径/);
  assert.match(host.querySelector(".news-review-next").textContent, /暂不要依据这里的表述作判断/);
  assert.match(host.textContent, /不代表 AI 对原文的解读正确/);
  assert.equal(host.querySelector("button"), null, "review must not offer a paid retry");
  assert.equal(JSON.stringify(analysis), before, "presentation must not rewrite the saved result");
});

test("summary pointers show the whole saved paragraph without inventing a bad sentence", async () => {
  const analysis = makeAnalysis({ semantic_review: { issues: [{ code: "statistical_comparison_basis", status: "unknown", field: "summary", evidence_fields: ["source_text.title", "source_text.excerpt"] }] } });
  await render(analysis);
  assert.equal(host.querySelector(".news-review-claim").textContent, analysis.candidate_result.summary);
  assert.match(host.textContent, /尚未精确到其中某一句/);
  assert.match(host.textContent, /没有定位具体原句/);
  assert.match(host.textContent, /当前保存的内容/);
  assert.equal(host.querySelector("mark"), null);
});

test("an uncertainty conflict keeps every saved note when the backend gave no precise note pointer", async () => {
  const analysis = makeAnalysis({ semantic_review: { issues: [{ code: "assertion_uncertainty_conflict", status: "contradicted", field: "key_points.0.claim", evidence_fields: ["uncertainties", "key_points.0.quote"] }] } });
  await render(analysis);
  assert.deepEqual([...host.querySelectorAll(".news-review-support li")].map((entry) => entry.textContent), analysis.candidate_result.uncertainties);
  assert.match(host.textContent, /未指出是哪一项/);
  assert.match(host.textContent, /存在冲突/);
});

test("an exact uncertainty pointer identifies its saved note without parsing raw output", async () => {
  const analysis = makeAnalysis({ semantic_review: { issues: [{ code: "uncertainty_denies_supported_basis", status: "contradicted", field: "uncertainties.1", evidence_fields: ["source_text.excerpt"] }] } });
  await render(analysis);
  assert.equal(host.querySelector(".news-review-claim").textContent, "采样细节未知。");
  assert.match(host.textContent, /第 2 条不确定项/);
});

test("missing candidates and unknown pointers stay explicitly unmapped", async () => {
  const analysis = makeAnalysis({ candidate_result: null, raw_model_output: '{"summary":"不得从原JSON猜成可信摘要"}', semantic_review: { issues: [{ code: "__proto__", status: "unknown", field: "__proto__.summary", evidence_fields: ["constructor.prototype"] }] } });
  await render(analysis);
  assert.equal(host.querySelector(".news-review-claim"), null);
  assert.match(host.textContent, /没有足够记录定位具体表述/);
  assert.match(host.textContent, /依据的位置无法确认/);
  assert.equal(host.querySelector("pre").textContent, analysis.raw_model_output);
  assert.equal(analysisStateLabel({ status: "__proto__" }), "分析状态待确认");
});

test("mismatched current source does not get displayed as a matched quote", async () => {
  await render(makeAnalysis(), { source: { title: "Synthetic revised title", excerpt: "A different saved revision." } });
  assert.match(host.textContent, /无法与当前保存的来源准确对应/);
  assert.equal(host.querySelector(".news-review-support blockquote"), null);
  assert.match(host.textContent, /未单独保存审核时的完整来源副本/);
});

test("inconsistent candidate lists or cross-point quote indices cannot invent a pairing", async () => {
  const analysis = makeAnalysis();
  analysis.candidate_result.key_points[0] = "与原证据不同的要点";
  await render(analysis);
  assert.equal(host.querySelector(".news-review-claim"), null);
  assert.equal(host.querySelector(".news-review-support blockquote"), null);
  const second = makeAnalysis();
  second.semantic_review.issues[0].evidence_fields = ["key_points.1.quote", "key_points.999999999999999999999.quote"];
  await render(second);
  assert.equal(host.querySelector(".news-review-support blockquote"), null);
  assert.match(host.textContent, /无法确认|无法与/);
});

test("saved time evidence is retained without using today's time to reinterpret it", async () => {
  const analysis = makeAnalysis({ freshness: { published_at: "2026-10-01T00:00:00Z", discovered_at: "2026-10-02T00:00:00Z", publication_age: "older_than_24h" }, quality_checked_at: "2026-10-20T00:00:00Z", semantic_review: { issues: [{ code: "publication_check_direction", status: "contradicted", field: "summary", evidence_fields: ["metadata.published_at", "checked_at"] }] } });
  await render(analysis);
  assert.match(host.querySelector(".news-review-card").textContent, /保存的来源标注时间/);
  assert.match(host.querySelector(".news-review-card").textContent, /2026\/10\/20/);
  assert.match(host.querySelector(".news-analysis-clock").textContent, /按保存的审核检查时间/);
  assert.match(host.querySelector(".news-analysis-clock").textContent, /已超过24小时/);
});

test("unknown and failed outcomes explain the next step without suggesting a refund or retry", async () => {
  for (const status of ["unknown", "failed"]) {
    await render({ status });
    assert.match(host.textContent, /仍可阅读已保存的消息/);
    assert.match(host.textContent, /不会自动重试/);
    assert.match(host.textContent, /不表示供应商未计费/);
    assert.equal(host.querySelector("button"), null);
    assert.equal(host.querySelector(".news-review-claim"), null);
  }
});

test("invalid or future times remain unknown and no original link can execute script", async () => {
  await render(makeAnalysis({ freshness: { published_at: "invalid", clock_anomaly: true }, semantic_review: { issues: [{ code: "publication_not_confirmed", status: "unknown", field: "summary", evidence_fields: ["freshness.clock_anomaly"] }] } }), { originalUrl: "javascript:alert(1)" });
  assert.equal(host.querySelector("a"), null);
  assert.match(host.querySelector(".news-analysis-clock").textContent, /存在异常/);
  assert.match(host.querySelector(".news-analysis-clock").textContent, /未确认/);
  assert.doesNotMatch(host.querySelector(".news-analysis-clock").textContent, /不超过24小时/);
});

test("technical details preserve escaped raw output and expanding them performs no request", async () => {
  const raw = '<img src=x onerror=alert(1)> {"summary":"未接纳的原文"}';
  await render(makeAnalysis({ raw_model_output: raw }));
  const details = host.querySelector(".news-analysis-raw");
  assert.equal(details.open, false);
  assert.match(details.querySelector("summary").textContent, /技术详情/);
  await click(details.querySelector("summary"));
  assert.equal(details.querySelector("pre").textContent, raw);
  assert.equal(host.querySelector("img"), null);
  assert.equal(fetchCalls, 0);
  const link = host.querySelector(".news-review-original");
  assert.equal(link.href, "https://www.federalreserve.gov/example");
  assert.equal(link.rel, "noopener noreferrer");
});

test("completed and legacy evidence remain readable without being upgraded to verified facts", async () => {
  assert.equal(analysisStateLabel({ status: "completed" }), "AI 解释已生成，供理解参考");
  await act(async () => root.render(React.createElement(AnalysisEvidence, { analysis: { status: "completed", evidence: [{ claim: "合成已保存要点", evidence_field: "excerpt", quote: "Synthetic saved quote." }] } })));
  assert.match(host.textContent, /合成已保存要点/);
  assert.match(host.textContent, /仅说明出处/);
  assert.match(host.textContent, /全文尚未读取/);
  await act(async () => root.render(React.createElement(AnalysisEvidence, { analysis: { status: "completed", legacy: true, quality_version: "rss_excerpt_reading_v3", evidence: [] } })));
  assert.match(host.textContent, /保留的旧版分析/);
  assert.match(host.textContent, /尚未经过新规则检查/);
  assert.match(host.textContent, /不会自动重新分析或扣费/);
});
