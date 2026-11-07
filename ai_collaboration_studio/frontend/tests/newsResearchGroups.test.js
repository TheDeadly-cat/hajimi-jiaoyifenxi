import assert from "node:assert/strict";
import test from "node:test";
import { buildNewsResearchDraft, newsResearchSubmission } from "../src/newsResearch.js";

const room = { id: "room-1", title: "官方材料研究" };
const sourceHash = "a".repeat(64);
const packageHash = "b".repeat(64);
const url = "https://investors.micron.com/release";
const rawOnly = "UNSELECTED_TABLE_VALUE_9999";

function fixture() {
  const common = { document_id: "article:fixture", url };
  const rows = [
    { ...common, id: "header", source_segment_id: "header", kind: "table_row", table_id: "table-1", row_index: 1,
      text: "季度 GAAP | 美元（百万）", cells: [{ text: "季度 GAAP", tag: "th", colspan: 2, rowspan: 1 }, { text: "美元（百万）", tag: "th", colspan: 1, rowspan: 1 }] },
    { ...common, id: "value", source_segment_id: "value", kind: "table_row", table_id: "table-1", row_index: 2,
      text: "GAAP 净利润 | 123", cells: [{ text: "GAAP 净利润", tag: "td", colspan: 2, rowspan: 1 }, { text: "123", tag: "td", colspan: 1, rowspan: 1 }] },
    { ...common, id: "note", source_segment_id: "note", kind: "paragraph", text: "单位与期间仅适用于本季度，未纳入未核对项目。" },
  ];
  for (const row of rows) Object.assign(row, { start: 0, end: Array.from(row.text).length, truncated: false });
  const group = { ...common, id: "article:fixture:gaap_net_income", kind: "table_context", profile_id: "fixture-v1", scope: "quarterly_actual",
    required_segment_ids: rows.map(row => row.id), targets: ["value"], dependencies: ["header", "note"] };
  const pack = { version_id: "version-2", content_hash: sourceHash, package_hash: packageHash,
    groups: [group], selection: rows.map(({ text, ...row }) => ({ ...row, start: 0, end: Array.from(text).length, truncated: false })),
    omitted_groups: [{ id: "article:fixture:unverified-remainder", kind: "table_context", reason: "dependencies_unverified", segment_count: 20 }],
    omitted_group_count: 1, omitted_group_reasons: { dependencies_unverified: 1 }, omitted_table_groups: 1,
    omitted_segments: 20, selection_rule: "verified_dependencies_or_whole_document_v2", truncated: true };
  const preview = { schema: "news_evidence_preview_v1", status: "ready", version_id: "version-2", content_hash: sourceHash,
    preview_only: true, model_called: false, segments: rows,
    material: { source_text: { title: "官方合成公告", excerpt: "", body: rows.map(row => row.text).join("\n") }, evidence_package: pack } };
  const evidence = { content_status: "full_text", content_hash: sourceHash, body_text: rawOnly,
    segments: [{ id: "raw-unselected", text: rawOnly }], attachments: [{ url: "https://investors.micron.com/attachment.pdf", status: "partial_text", read: true }], extractor_version: "fixture-parser" };
  return { id: "news-1", title: "官方合成公告", source_name: "MU 官方公告", source_id: "ir_mu", url, published_at: "2026-10-29T00:00:00Z",
    discovered_at: "2026-10-29T00:01:00Z", current_version_id: "version-2", excerpt: rawOnly, summary: rawOnly,
    symbols: ["US.MU"], evidence, evidence_preview: preview,
    event: { event_id: "event-1", sources: [{ id: "source-1" }], independent_evidence_count: 1,
      current_version: { id: "version-2", evidence, version_seen_at: "2026-10-29T00:02:00Z" } } };
}

test("research draft imports the complete selected groups with dependencies and cell geometry only", () => {
  const item = fixture();
  const before = structuredClone(item);
  const draft = buildNewsResearchDraft(item, room, "handoff-1");
  assert.doesNotMatch(draft.content, new RegExp(rawOnly));
  assert.match(draft.content, /GAAP 净利润/);
  assert.match(draft.content, /美元（百万）/);
  assert.match(draft.content, /未纳入未核对项目/);
  assert.match(draft.content, /"dependencies":\["header","note"\]/);
  assert.match(draft.content, /"source_segment_id":"value"/);
  assert.match(draft.content, /"row_index":2/);
  assert.match(draft.content, /"colspan":2,"rowspan":1/);
  assert.match(draft.content, /dependencies_unverified/);
  assert.match(draft.content, /已取得部分文本，内容不完整/);
  assert.equal(draft._news_research.evidence_package_hash, packageHash);
  assert.equal(draft._news_research.source_content_hash, sourceHash);
  assert.equal(draft._news_research.version_id, "version-2");
  assert.deepEqual(item, before);
});

test("missing, mismatched or incomplete previews fail without importing raw body, RSS or summary", () => {
  const mutations = [
    item => { delete item.evidence_preview; },
    item => { item.evidence_preview.version_id = "old-version"; },
    item => { item.evidence_preview.content_hash = "c".repeat(64); },
    item => { item.evidence_preview.model_called = true; },
    item => { item.evidence_preview.segments.pop(); },
    item => { item.evidence_preview.material.evidence_package.groups[0].dependencies.push("absent-note"); },
    item => { delete item.evidence_preview.material.evidence_package.package_hash; },
  ];
  for (const mutate of mutations) {
    const item = fixture(); mutate(item);
    assert.throws(() => buildNewsResearchDraft(item, room, "blocked"), /未创建研究草稿/);
  }
});

test("a valid empty selection records omission reasons without fallback source facts", () => {
  const item = fixture();
  item.evidence_preview.status = "no_selected_evidence";
  item.evidence_preview.segments = [];
  item.evidence_preview.material.source_text.body = "";
  item.evidence_preview.material.evidence_package.groups = [];
  item.evidence_preview.material.evidence_package.selection = [];
  const draft = buildNewsResearchDraft(item, room, "empty-selection");
  assert.match(draft.content, /没有完整证据组可以导入/);
  assert.match(draft.content, /表头、期间、单位或脚注/);
  assert.doesNotMatch(draft.content, new RegExp(rawOnly));
  assert.doesNotMatch(draft.content, /本机整理（规则生成/);
});

test("the fifty-thousand-character limit stops the whole draft instead of cutting a dependency group", () => {
  const item = fixture();
  const long = "完整依赖原文".repeat(11000);
  const segment = { id: "long", source_segment_id: "long", document_id: "article:fixture", url, kind: "paragraph", text: long, start: 0, end: Array.from(long).length, truncated: false };
  item.evidence_preview.segments = [segment];
  item.evidence_preview.material.source_text.body = long;
  item.evidence_preview.material.evidence_package.groups = [{ id: "article:fixture:long", document_id: "article:fixture", url, kind: "paragraph", required_segment_ids: ["long"] }];
  const { text: selectedText, ...locator } = segment;
  item.evidence_preview.material.evidence_package.selection = [locator];
  assert.throws(() => buildNewsResearchDraft(item, room, "too-large"), /超过资料长度上限.*未创建研究草稿.*不能截断证据组/);
});

test("submission remains bound to version and derived package even in the same room", () => {
  const draft = buildNewsResearchDraft(fixture(), room, "handoff-bound");
  const context = { ...draft._news_research };
  const submitted = newsResearchSubmission(draft, context, room.id);
  assert.equal(submitted.roomId, room.id);
  assert.equal(submitted.payload._news_research, undefined);
  for (const field of ["version_id", "evidence_package_hash", "source_content_hash"]) {
    assert.throws(() => newsResearchSubmission({ ...draft, _news_research: { ...draft._news_research, [field]: "changed" } }, context, room.id), /交接已取消/);
  }
});

test("legacy RSS without body remains readable and draft generation performs no network calls", () => {
  const item = { id: "old-rss", title: "旧摘录", source_name: "官方订阅", url, excerpt: "原 RSS 摘录。", summary: "本机规则整理。" };
  const previousFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = () => { calls++; throw new Error("Network forbidden"); };
  try {
    const draft = buildNewsResearchDraft(item, room, "legacy");
    assert.match(draft.content, /原 RSS 摘录/);
    assert.match(draft.content, /本机规则整理/);
    assert.equal(draft._news_research.evidence_package_hash, undefined);
    assert.equal(calls, 0);
  } finally { globalThis.fetch = previousFetch; }
});

test("directory labels are not promoted to original titles and do not change the selected evidence package", () => {
  const item = fixture();
  const original = buildNewsResearchDraft(item, room, "title-provenance");
  const directory = buildNewsResearchDraft({ ...item, discovery_evidence: { title_basis: "directory_label" } }, room, "title-provenance");
  assert.match(directory.content, /^目录条目名称（非原文标题）：官方合成公告$/m);
  assert.doesNotMatch(directory.content, /^原标题：/m);
  assert.equal(directory.content.replace("目录条目名称（非原文标题）：", "原标题："), original.content);
  assert.deepEqual(directory._news_research, original._news_research);
  assert.equal(directory._news_research.evidence_package_hash, item.evidence_preview.material.evidence_package.package_hash);
  assert.match(original.content, /^原标题：官方合成公告$/m);
  const legacy = buildNewsResearchDraft({ id: "rss-title", title: "旧订阅标题", source_name: "官方订阅", url, excerpt: "原 RSS 摘录。" }, room, "legacy-title");
  assert.match(legacy.content, /^原标题：旧订阅标题$/m);
  assert.doesNotMatch(legacy.content, /目录条目名称（非原文标题）/);
});

test("known directory attachment references remain explicitly unread when the article has no attachment list", () => {
  const item = fixture();
  item.evidence.attachments = [];
  item.discovery_evidence = { title_basis: "directory_label", attachment_references: [
    {category:"Earnings presentation",url:"https://unregistered.example/slides.pdf",read:false},
    {category:"Prepared remarks",url:"https://unregistered.example/remarks.pdf",read:false},
  ] };
  const draft = buildNewsResearchDraft(item,room,"directory-attachments");
  assert.match(draft.content,/目录另列的附件链接（发现时未读/);
  assert.match(draft.content,/https:\/\/unregistered.example\/slides.pdf/);
  assert.match(draft.content,/https:\/\/unregistered.example\/remarks.pdf/);
  assert.match(draft.content,/仅保存链接，未将附件内容加入草稿/);
  assert.doesNotMatch(draft.content,/未发现或未取得附件清单/);
  assert.equal(draft._news_research.evidence_package_hash,packageHash);
});
