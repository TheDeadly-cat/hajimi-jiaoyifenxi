import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { createServer } from "vite";
import { validatedEvidencePreview, resolveSavedEvidenceLocator } from "../src/newsEvidence.js";

// Vite is a local JSX transformer in middleware mode; no listening server.
const vite = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)), appType: "custom", logLevel: "silent", server: { hmr: false, middlewareMode: true } });
const dom = new JSDOM("<!doctype html><body></body>", { pretendToBeVisual: true, url: "http://evidence-preview.test/" });
for (const key of ["window", "document", "navigator", "HTMLElement", "Node", "Event"]) Object.defineProperty(globalThis, key, { configurable: true, value: key === "window" ? dom.window : dom.window[key] });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const { createRoot } = await import("react-dom/client");
const { NewsEventCard } = await vite.ssrLoadModule("/src/components/NewsEventCard.jsx");
const { AnalysisEvidence, AnalysisReview } = await vite.ssrLoadModule("/src/components/NewsContentQuality.jsx");
const originalFetch = globalThis.fetch;
let host, root, item, calls;
const url = "https://example.test/saved-announcement";
const contentHash = "a".repeat(64);
const cell = text => ({ text, colspan: 1, rowspan: 1, tag: "td" });
function fixture(versionId = "v2", amount = "13,800") {
  const documentId = "article:saved";
  const segments = [
    { id: "p1", kind: "paragraph", text: "Dollars in millions, except per share amounts." },
    { id: "table1-r1", kind: "table_row", table_id: "table1", row_index: 1, cells: [cell("Three months ended"), cell("May 28, 2026")], text: "Three months ended | May 28, 2026" },
    { id: "table1-r2", kind: "table_row", table_id: "table1", row_index: 2, cells: [cell("Net income"), cell(amount)], text: `Net income | ${amount}` },
  ].map(row => ({ ...row, source_segment_id: row.id, document_id: documentId, url, start: 0, end: Array.from(row.text).length, truncated: false }));
  const group = { id: `${documentId}:gaap_net_income`, document_id: documentId, kind: "table_context", url, profile_id: "synthetic-profile", scope: "quarterly_actual", targets: ["table1-r2"], dependencies: ["p1", "table1-r1"], required_segment_ids: segments.map(row => row.id) };
  const pack = { version_id: versionId, content_hash: contentHash, package_hash: "b".repeat(64), groups: [group], selection: segments.map(({ text, ...row }) => structuredClone(row)), verified_profiles: [{ id: "synthetic-profile", currency_symbol: "$", currency_iso: null }], omitted_group_count: 2, omitted_group_reasons: { dependencies_unverified: 1, budget: 1 }, excerpt_omitted_for_table_context: true, truncated: true };
  const evidence = { url, content_status: "full_text", content_hash: contentHash, segments, body_text: segments.map(row => row.text).join("\n"), attachments: [] };
  const preview = { schema: "news_evidence_preview_v1", status: "ready", version_id: versionId, content_hash: contentHash, preview_only: true, model_called: false, material: { source_text: { title: "合成已保存公告", excerpt: "", body: evidence.body_text }, evidence_package: pack }, segments };
  const version = { id: versionId, version_number: versionId === "v2" ? 2 : 1, title: "合成已保存公告", url, content_status: "full_text", evidence, evidence_preview: preview };
  return { id: "saved-item", title: version.title, url, current_version_id: versionId, evidence, evidence_preview: preview, event: { current_version: version, version_count: 2, sources: [] } };
}
async function renderCard(value = item, apiClient = {}) { await act(async () => root.render(React.createElement(NewsEventCard, { item: value, apiClient }))); }
async function clickText(value) { const button = [...host.querySelectorAll("button")].find(button => button.textContent.includes(value)); assert.ok(button); await act(async () => button.click()); }
test.beforeEach(() => {
  item = fixture(); calls = 0;
  globalThis.fetch = async () => { calls++; throw new Error("Network forbidden in saved preview tests"); };
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
test.afterEach(async () => { await act(async () => root.unmount()); assert.equal(calls, 0); document.body.replaceChildren(); globalThis.fetch = originalFetch; });
test.after(async () => { dom.window.close(); await vite.close(); });

test("preview exposes target, complete period/unit dependencies, omission reasons and unknown currency without implying analysis", async () => {
  await renderCard();
  const preview = host.querySelector(".news-evidence-preview");
  assert.match(preview.textContent, /可用依据预览（未发送）/);
  assert.match(preview.textContent, /GAAP 净利润.*季度已报告数值/s);
  assert.match(preview.textContent, /核对目标.*Net income \| 13,800/s);
  assert.match(preview.textContent, /期间、单位与脚注依据.*Dollars in millions.*May 28, 2026/s);
  assert.match(preview.textContent, /明确币种：未确认/);
  assert.match(preview.textContent, /依赖关系尚未核对.*完整组超出本次材料长度限制/s);
  assert.equal(host.querySelector(".news-analysis-used-scope"), null);
});

test("preview refuses wrong version or hash and leaves saved source readable", async () => {
  for (const patch of [{ version_id: "v1" }, { content_hash: "wrong" }, { model_called: true }]) {
    const bad = fixture(); Object.assign(bad.evidence_preview, patch);
    await renderCard(bad);
    assert.equal(host.querySelector(".news-evidence-preview .news-evidence-group"), null);
    assert.match(host.querySelector(".news-event-evidence").textContent, /Net income \| 13,800/);
  }
});

test("shared preview validator rejects incomplete groups, missing structure, mismatched rows, and body substitutions", () => {
  assert.equal(validatedEvidencePreview(item.evidence_preview, { versionId: "v2", contentHash }).valid, true);
  const mutations = [
    p => p.segments.pop(),
    p => p.material.evidence_package.groups[0].required_segment_ids.push("missing"),
    p => p.material.evidence_package.groups[0].dependencies.push("missing"),
    p => p.material.evidence_package.selection.reverse(),
    p => p.segments[1].cells[0].rowspan = 0,
    p => p.segments[1].cells[0] = ["Three months ended", 1, 1, "td"],
    p => p.material.source_text.body += "extra",
    p => p.segments[0].url = "https://elsewhere.test/",
  ];
  for (const mutate of mutations) { const p = structuredClone(item.evidence_preview); mutate(p); assert.equal(validatedEvidencePreview(p, { versionId: "v2", contentHash }).valid, false); }
  const unavailable = { ...item.evidence_preview, status: "unavailable", content_hash: null, material: null, segments: [], reason: "content_hash_missing" };
  assert.equal(validatedEvidencePreview(unavailable, { versionId: "v2", contentHash: null }).reason, "content_hash_missing");
});

test("no selected evidence remains an explicit empty preview with omission reasons", async () => {
  const p = item.evidence_preview; p.status = "no_selected_evidence"; p.segments = []; p.material.source_text.body = "";
  p.material.evidence_package.groups = []; p.material.evidence_package.selection = [];
  await renderCard();
  assert.match(host.querySelector(".news-evidence-preview").textContent, /没有可完整选入的依据组/);
  assert.match(host.querySelector(".news-evidence-preview").textContent, /已选 0 组.*省略 2 组/s);
});

test("selection and decoded preview must have identical full-segment locations and table geometry", async () => {
  const mutations = [
    p => p.material.evidence_package.selection[1].document_id = "article:other",
    p => p.material.evidence_package.selection[1].source_segment_id = "other-row",
    p => p.material.evidence_package.selection[1].url = "https://example.test/other",
    p => p.material.evidence_package.selection[1].kind = "paragraph",
    p => p.material.evidence_package.selection[1].table_id = "table99",
    p => p.material.evidence_package.selection[1].row_index = 9,
    p => p.material.evidence_package.selection[1].group_id = "other-group",
    p => p.material.evidence_package.selection[1].start = 1,
    p => p.material.evidence_package.selection[1].end--,
    p => p.material.evidence_package.selection[1].truncated = true,
    p => p.segments[1].start = 1,
    p => delete p.segments[1].end,
    p => p.material.evidence_package.selection[1].cells[0].colspan = 2,
    p => p.material.evidence_package.selection[1].cells[0].rowspan = 2,
    p => p.material.evidence_package.selection[1].cells[0].tag = "th",
    p => p.material.evidence_package.selection[1].cells[0].text = "Different period",
    p => { p.segments[1].cells[0].text = "Different period"; p.segments[1].text = "Different period | May 28, 2026"; p.material.source_text.body = p.segments.map(row => row.text).join("\n"); },
  ];
  for (const mutate of mutations) {
    const p = structuredClone(item.evidence_preview); mutate(p);
    const result = validatedEvidencePreview(p, { versionId: "v2", contentHash });
    assert.equal(result.valid, false); assert.deepEqual(result.segments, []); assert.equal(result.package, null);
  }
  item.evidence_preview.material.evidence_package.selection[1].cells[0].colspan = 2;
  await renderCard();
  assert.equal(host.querySelector(".news-evidence-preview .news-evidence-group"), null);
});

test("real compact cell projection decodes exactly and preserves Unicode code point lengths", () => {
  const p = item.evidence_preview;
  p.segments[0].text = `🧾${p.segments[0].text}`;
  p.segments[0].end = Array.from(p.segments[0].text).length;
  p.material.evidence_package.selection[0].end = p.segments[0].end;
  p.material.source_text.body = p.segments.map(row => row.text).join("\n");
  for (const selected of p.material.evidence_package.selection) {
    delete selected.url;
    if (selected.cells) { selected.cells = selected.cells.map(cell => [cell.text, cell.colspan, cell.rowspan, cell.tag]); selected.cell_encoding = "text_colspan_rowspan_tag_v1"; }
  }
  const before = JSON.stringify(p);
  assert.equal(validatedEvidencePreview(p, { versionId: "v2", contentHash }).valid, true);
  assert.equal(JSON.stringify(p), before, "checking must not rewrite saved projections");
  p.material.evidence_package.selection[0].end = p.segments[0].text.length;
  assert.equal(validatedEvidencePreview(p, { versionId: "v2", contentHash }).valid, false);
});

test("profile groups cannot omit a declared dependency or duplicate it as a target", () => {
  for (const mutate of [
    group => group.dependencies.pop(),
    group => delete group.dependencies,
    group => group.targets.pop(),
    group => group.targets.push(group.dependencies[0]),
    group => group.required_segment_ids.pop(),
  ]) {
    const p = structuredClone(item.evidence_preview); mutate(p.material.evidence_package.groups[0]);
    assert.equal(validatedEvidencePreview(p, { versionId: "v2", contentHash }).valid, false);
  }
});

test("history uses its own bound preview and never fills a missing old preview from current", async () => {
  const prior = fixture("v1", "2,000").event.current_version;
  await renderCard(item, { newsVersions: async () => ({ versions: [item.event.current_version, prior] }) });
  await clickText("查看版本历史"); await clickText("版本 1");
  assert.match(host.querySelector(".news-evidence-preview").textContent, /Net income \| 2,000/);
  assert.doesNotMatch(host.querySelector(".news-evidence-preview").textContent, /13,800/);
  await act(async () => root.unmount()); root = createRoot(host);
  delete prior.evidence_preview;
  await renderCard(item, { newsVersions: async () => ({ versions: [item.event.current_version, prior] }) });
  await clickText("查看版本历史"); await clickText("版本 1");
  assert.match(host.querySelector(".news-evidence-preview").textContent, /此版本尚无可用的依据预览/);
  assert.equal(host.querySelector(".news-evidence-preview .news-evidence-group"), null);
});

test("saved analysis scope is labeled separately and does not invent preview source paragraphs", async () => {
  await act(async () => root.render(React.createElement(AnalysisEvidence, { analysis: { status: "completed", evidence_package: item.evidence_preview.material.evidence_package, evidence: [] } })));
  assert.match(host.textContent, /已使用范围（保存的分析记录）/);
  assert.match(host.textContent, /此记录保留了定位标识，未附原段文本/);
  assert.doesNotMatch(host.textContent, /可用依据预览|Net income \| 13,800/);
});

test("older saved packages leave missing group counts unknown and RSS-only reading has no preview error", async () => {
  await act(async () => root.render(React.createElement(AnalysisEvidence, { analysis: { status: "completed", evidence_package: { selection: [{ id: "p1" }] }, evidence: [] } })));
  assert.match(host.textContent, /此记录未保存完整组清单.*省略数量未记录/s);
  assert.doesNotMatch(host.textContent, /已选 0 组/);
  item.evidence = { status: "excerpt_only" }; item.event.current_version.evidence = item.evidence;
  delete item.evidence_preview; delete item.event.current_version.evidence_preview;
  await renderCard();
  assert.equal(host.querySelector(".news-evidence-preview"), null);
});

test("partial PDF, failed and disallowed attachments stay distinct and saved partial text can be read", async () => {
  item.evidence.attachments = [
    { url: "https://example.test/partial.pdf", label: "合成财务附件", status: "partial_text", read: true, evidence: { reading_scope: "pdf_text_only_no_ocr_no_layout_validation", segments: [{ id: "page1", text: "已取得的部分附件原文" }] } },
    { url: "https://example.test/failed.pdf", status: "fetch_failed", read: false, reason: "attachment_read_failed" },
    { url: "https://elsewhere.test/blocked.pdf", status: "access_restricted", read: false, reason: "attachment_host_not_allowed" },
    { url: "https://example.test/unread.pdf", status: "not_read", read: false },
  ];
  await renderCard();
  const text = host.querySelector(".news-event-attachments").textContent;
  assert.match(text, /1 \/ 4 已取得文本（完整性另列）/);
  assert.match(text, /合成财务附件.*已取得部分文本，内容不完整/s);
  assert.match(text, /未做图片文字识别或版式核验/);
  assert.match(text, /附件读取失败.*附件地址不在允许来源内.*尚未读取/s);
  assert.match(text, /已取得的部分附件原文/);
  assert.doesNotMatch(text, /attachment_read_failed|attachment_host_not_allowed/);
});

function attachmentReview() {
  const quote = "批准后生效。", sourceText = `🧾${quote}`;
  item.evidence.attachments = [{ url: "https://example.test/part.pdf", status: "partial_text", read: true, evidence: { segments: [{ id: "p1", text: sourceText }] } }];
  const locator = { segment_id: "attachment-1:p1", source_segment_id: "p1", document_id: "attachment-1:saved", url: item.evidence.attachments[0].url, start: 1, end: Array.from(sourceText).length };
  const analysis = { status: "needs_review", article_version_id: "v2", candidate_result: { key_points: ["合成解释"], evidence: [{ claim: "合成解释", quote, evidence_field: "body", locator }] }, semantic_review: { issues: [{ field: "key_points.0.claim", evidence_fields: ["key_points.0.quote"] }] } };
  return { quote, locator, analysis };
}

test("saved attachment quotes resolve namespaced or original source IDs with Unicode code point bounds", async () => {
  const { quote, locator, analysis } = attachmentReview();
  await act(async () => root.render(React.createElement(AnalysisReview, { analysis, source: item })));
  assert.equal(host.querySelector(".news-review-support blockquote").textContent, quote);
  assert.match(host.querySelector(".news-review-support h5").textContent, /已保存附件 1 · p1/);
  assert.equal(host.querySelector(".news-review-support a").href, locator.url);
  assert.equal(resolveSavedEvidenceLocator(item, analysis, { ...locator, segment_id: "p1" }, quote).matched, true);
  assert.equal(resolveSavedEvidenceLocator(item, analysis, { ...locator, segment_id: undefined }, quote).matched, true);
});

test("changed version, wrong source URL, unread attachment and UTF-16 offsets cannot match an old quote", async () => {
  const { quote, locator, analysis } = attachmentReview();
  assert.equal(resolveSavedEvidenceLocator(item, { ...analysis, article_version_id: "v1" }, locator, quote).matched, false);
  assert.equal(resolveSavedEvidenceLocator(item, analysis, { ...locator, url: item.url }, quote).matched, false);
  assert.equal(resolveSavedEvidenceLocator(item, analysis, { ...locator, source_segment_id: "p2" }, quote).matched, false);
  assert.equal(resolveSavedEvidenceLocator(item, analysis, { ...locator, start: 2 }, quote).matched, false);
  item.evidence.attachments[0].read = false;
  await act(async () => root.render(React.createElement(AnalysisReview, { analysis, source: item })));
  assert.equal(host.querySelector(".news-review-support"), null);
  assert.match(host.textContent, /无法与当前保存的来源准确对应/);
});

test("revision evidence labels the old version separately and records both complete saved scopes", async () => {
  const comparison = { status: "available", documents: [
    { role: "before", version_number: 1, scope: "saved_body_only_attachments_not_compared", url },
    { role: "after", version_number: 2, scope: "saved_body_only_attachments_not_compared", url },
  ] };
  const analysis = { revision_comparison: comparison, evidence: [{ claim: "旧版原句", quote: "Old reported amount", evidence_field: "revision", locator: { revision_role: "before", article_version_id: "v1", source_segment_id: "table-row-3", url } }] };
  await act(async () => root.render(React.createElement(AnalysisEvidence, { analysis })));
  assert.match(host.textContent, /旧版本 1.*所选新版本 2/s);
  assert.match(host.textContent, /旧版数值只作为旧版依据，不代表当前结果/);
  assert.match(host.querySelector(".news-analysis-evidence small").textContent, /旧版本原文.*table-row-3/);
  assert.equal(host.querySelector("blockquote").textContent, "Old reported amount");
});

test("omitted or malformed comparison cannot be presented as a verified change", async () => {
  for (const comparison of [{ status: "unavailable", reason: "comparison_budget" }, { status: "available", documents: [] }]) {
    await act(async () => root.render(React.createElement(AnalysisEvidence, { analysis: { revision_comparison: comparison } })));
    assert.match(host.textContent, /新增或更正尚无法核验/);
    assert.doesNotMatch(host.textContent, /已同时纳入/);
  }
});
