import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { createServer } from "vite";

// Captured from backend.news_analysis_policy.classify with synthetic messages;
// these preserve the real API's four dimensions, flags and separate importance.
const policies = {
  "eligible": {
    "policy_version": "news_focus_policy_v1",
    "relevance": "direct",
    "novelty": "new_event",
    "event_type": "earnings",
    "event_type_basis": "local_text_rules",
    "evidence": {
      "status": "excerpt_only",
      "attachments_unread": false,
      "time_unknown": false,
      "conflict": false,
      "selected_complete_groups": null
    },
    "importance": "important",
    "automatic_eligible": true,
    "automatic_scope": "market",
    "reasons": [],
    "priority": 80,
    "matched": {
      "companies": [
        "micron"
      ],
      "symbols": [
        "US.MU"
      ],
      "industries": [
        "semiconductors",
        "storage"
      ],
      "macro_topics": []
    }
  },
  "unrelated_backfill": {
    "policy_version": "news_focus_policy_v1",
    "relevance": "unrelated",
    "novelty": "historical_backfill",
    "event_type": "earnings",
    "event_type_basis": "local_text_rules",
    "evidence": {
      "status": "excerpt_only",
      "attachments_unread": false,
      "time_unknown": false,
      "conflict": false,
      "selected_complete_groups": null
    },
    "importance": "important",
    "automatic_eligible": false,
    "automatic_scope": "market",
    "reasons": [
      "outside_focus",
      "historical_backfill"
    ],
    "priority": 20,
    "matched": {
      "companies": [
        "seagate"
      ],
      "symbols": [
        "US.STX"
      ],
      "industries": [
        "storage"
      ],
      "macro_topics": []
    }
  },
  "evidence_missing": {
    "policy_version": "news_focus_policy_v1",
    "relevance": "direct",
    "novelty": "new_event",
    "event_type": "earnings",
    "event_type_basis": "local_text_rules",
    "evidence": {
      "status": "headline_only",
      "attachments_unread": true,
      "time_unknown": true,
      "conflict": true,
      "selected_complete_groups": null
    },
    "importance": "important",
    "automatic_eligible": false,
    "automatic_scope": "market",
    "reasons": [
      "evidence_requires_review",
      "evidence_missing"
    ],
    "priority": 80,
    "matched": {
      "companies": [
        "micron"
      ],
      "symbols": [
        "US.MU"
      ],
      "industries": [
        "semiconductors",
        "storage"
      ],
      "macro_topics": []
    }
  },
  "duplicate_ordinary": {
    "policy_version": "news_focus_policy_v1",
    "relevance": "direct",
    "novelty": "duplicate_confirmation",
    "event_type": "industry",
    "event_type_basis": "local_text_rules",
    "evidence": {
      "status": "excerpt_only",
      "attachments_unread": false,
      "time_unknown": false,
      "conflict": false,
      "selected_complete_groups": null
    },
    "importance": "ordinary",
    "automatic_eligible": false,
    "automatic_scope": "market",
    "reasons": [
      "not_important",
      "duplicate_confirmation"
    ],
    "priority": 10,
    "matched": {
      "companies": [
        "micron"
      ],
      "symbols": [
        "US.MU"
      ],
      "industries": [
        "semiconductors",
        "storage"
      ],
      "macro_topics": []
    }
  }
};
const vite = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)), appType: "custom", logLevel: "silent", server: { hmr: false, middlewareMode: true } });
const dom = new JSDOM("<!doctype html><body></body>", { pretendToBeVisual: true, url: "http://news-policy.test/" });
for (const key of ["window", "document", "navigator", "HTMLElement", "Node", "Event"]) Object.defineProperty(globalThis, key, { configurable: true, value: key === "window" ? dom.window : dom.window[key] });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const { createRoot } = await import("react-dom/client");
const { NewsWorkspace } = await vite.ssrLoadModule("/src/components/NewsWorkspace.jsx");
const { NewsPolicySummary } = await vite.ssrLoadModule("/src/components/NewsPolicySummary.jsx");
const originalFetch = globalThis.fetch;
let host, root, item, client, calls;
const button = label => [...host.querySelectorAll("button")].find(node => node.textContent.trim() === label);
async function click(target) { assert.ok(target); await act(async () => target.click()); }
async function renderItem(policy) { item.analysis_policy = policy; await act(async () => root.render(React.createElement(NewsWorkspace, { apiClient: client, onOpenRooms() {} }))); await click(host.querySelector(".news-item")); }
test.beforeEach(() => {
  calls = { external: 0, analyze: 0, versions: 0 };
  item = { id: "policy-message", title: "合成政策分类消息", source_id: "ir_mu", source_name: "合成官方来源", excerpt: "合成保存摘录。", url: "https://example.invalid/retained", read: true, current_version_id: "v2", material_quality: { can_analyze: true, status: "sufficient_excerpt" } };
  const overview = { enabled: false, sources: [], counts: { total: 1 }, categories: [], ai: { configured: false, automatic: false } };
  client = { newsOverview: async () => ({ overview }), newsItems: async () => ({ items: [item], total: 1 }), newsItem: async () => ({ item }), newsAIAnalyze: async () => { calls.analyze++; throw new Error("Reading policy must not invoke analysis"); }, newsVersions: async () => { calls.versions++; return { versions: item.event.versions }; } };
  globalThis.fetch = async () => { calls.external++; throw new Error("Offline policy rendering only"); };
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
test.afterEach(async () => { await act(async () => root.unmount()); assert.equal(calls.external, 0); assert.equal(calls.analyze, 0); document.body.replaceChildren(); globalThis.fetch = originalFetch; });
test.after(async () => { dom.window.close(); await vite.close(); });

test("eligible current news exposes four separate dimensions without claiming a model already analyzed it", async () => {
  await renderItem(policies.eligible);
  const panel = host.querySelector(".news-policy-summary");
  assert.equal(panel.querySelector("summary").textContent, "符合本地筛选条件");
  await click(panel.querySelector("summary")); assert.equal(panel.open, true);
  assert.deepEqual([...panel.querySelectorAll("dt")].map(node => node.textContent), ["相关性", "新旧程度", "事件类型", "证据状态"]);
  assert.deepEqual([...panel.querySelectorAll("dd")].map(node => node.textContent), ["直接相关", "新事件", "财报", "只有摘录"]);
  assert.match(panel.textContent, /有效授权和可用额度.*不代表已经分析/s);
  assert.match(panel.textContent, /重要性（本机规则）：重要.*不代表可信程度或利多、利空/);
});

test("unrelated historical backfill explains why this current message is not automatically analyzed", async () => {
  await renderItem(policies.unrelated_backfill);
  const panel = host.querySelector(".news-policy-summary");
  assert.match(panel.querySelector("summary").textContent, /暂不自动解释：不在当前关注范围内.*历史回填/);
  assert.match(panel.textContent, /与当前关注无关.*历史回填/s);
  assert.doesNotMatch(panel.textContent, /outside_focus|historical_backfill|automatic_eligible/);
});

test("missing evidence retains unread attachments, unknown source time and conflict as separate flags", async () => {
  await renderItem(policies.evidence_missing);
  const panel = host.querySelector(".news-policy-summary");
  assert.match(panel.querySelector("summary").textContent, /证据存在冲突.*缺少可引用的正文或摘录/);
  assert.match(panel.textContent, /只有标题.*附件未读.*发布时间未知.*资料存在冲突/s);
  assert.doesNotMatch(panel.textContent, /evidence_requires_review|evidence_missing|headline_only/);
});

test("ordinary duplicates and unavailable complete evidence groups have readable reasons, including unknown-code fallback", async () => {
  const policy = { ...policies.duplicate_ordinary, reasons: [...policies.duplicate_ordinary.reasons, "article_evidence_unavailable_no_send", "future_internal_reason"], evidence: { ...policies.duplicate_ordinary.evidence, selected_complete_groups: false } };
  await act(async () => root.render(React.createElement(NewsPolicySummary, { policy })));
  const panel = host.querySelector(".news-policy-summary");
  assert.match(panel.querySelector("summary").textContent, /尚未达到.*重要性条件.*重复确认或转载.*没有可完整保留的证据组.*部分筛选条件尚未确认/s);
  assert.match(panel.textContent, /重要性（本机规则）：普通/);
  assert.doesNotMatch(panel.textContent, /not_important|duplicate_confirmation|article_evidence_unavailable_no_send|future_internal_reason/);
});

test("legacy details without policy retain their saved reading and do not invent classifications", async () => {
  item.ai_analysis = { status: "completed", summary: "原有事实阅读记录", model: "deepseek-flash", key_points: [] };
  await renderItem(undefined);
  assert.equal(host.querySelector(".news-policy-summary"), null);
  assert.match(host.querySelector(".news-ai-reading").textContent, /原有事实阅读记录/);
});

test("viewing an old version hides current policy and returning restores it without analysis", async () => {
  const current = { id: "v2", version_number: 2, version_seen_at: "2026-11-07T08:00:00Z", evidence: { status: "excerpt_only" }, changes: [] };
  const prior = { id: "v1", version_number: 1, version_seen_at: "2026-11-06T08:00:00Z", evidence: { status: "excerpt_only" }, changes: [], title: "合成旧版消息", excerpt: "旧版保存摘录" };
  item.event = { current_version: current, versions: [current, prior], version_count: 2, sources: [] };
  await renderItem(policies.eligible); assert.ok(host.querySelector(".news-policy-summary"));
  await click(button("查看版本历史（2）"));
  await click([...host.querySelectorAll("button")].find(node => node.textContent.startsWith("版本 1 ·")));
  assert.equal(host.querySelector(".news-policy-summary"), null);
  await click(button("返回当前版本")); assert.ok(host.querySelector(".news-policy-summary"));
  assert.equal(calls.versions, 1);
});
