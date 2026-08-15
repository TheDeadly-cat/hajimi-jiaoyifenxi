import test from "node:test";
import assert from "node:assert/strict";
import { artifactToMarkdown } from "../src/artifacts.js";

function governedArtifact() {
  return {
    id: "artifact_1",
    title: "候选治理纪要",
    kind: "meeting_minutes",
    status: "CONFIRMED",
    version: 2,
    round_id: "round_1",
    content: {
      summary: "形成两个候选并完成风险复核。",
      decision: {
        status: "candidate",
        preferred_option_id: "candidate_a",
        rationale: "保留反证后进入用户判断。",
        evidence: [],
        options: [
          {
            id: "candidate_a",
            title: "候选 A",
            description: "仅作为候选，不自动执行。",
            benefits: [],
            risks: [],
            dependencies: [],
            reversibility: "high",
            evidence: [],
          },
          {
            id: "candidate_b",
            title: "候选 B",
            description: "等待更多证据。",
            benefits: [],
            risks: [],
            dependencies: [],
            reversibility: "high",
            evidence: [],
          },
        ],
      },
    },
    governance_snapshot: {
      version: "turn_contract_v1",
      candidate_lineage: {
        version: "candidate_lineage_v1",
        applicable: true,
        ready: true,
        decision_message_id: "message_decision",
        candidates: [
          { id: "candidate_a", revision: 2, origin_message_id: "message_a1", latest_message_id: "message_a2" },
          { id: "candidate_b", revision: 1, origin_message_id: "message_b1", latest_message_id: "message_b1" },
        ],
        issues: [],
      },
      candidate_risk_reviews: {
        version: "candidate_risk_review_v1",
        applicable: true,
        ready: true,
        target_candidate_count: 2,
        reviewed_candidate_count: 2,
        current_review_count: 2,
        stale_review_count: 0,
        action_counts: { support: 1, challenge: 1, reject: 0 },
        reviews: [
          {
            candidate_id: "candidate_a",
            candidate_revision: 2,
            current_candidate_revision: 2,
            action: "support",
            status: "current",
            review_message_id: "message_review_a",
            reviewer_name: "风险负责人",
            reviewer_member_version: 3,
            candidate_snapshot: { title: "候选 A" },
            candidate_snapshot_sha256: "a".repeat(64),
          },
          {
            candidate_id: "candidate_b",
            candidate_revision: 1,
            current_candidate_revision: 1,
            action: "challenge",
            status: "current",
            review_message_id: "message_review_b",
            reviewer_name: "风险负责人",
            reviewer_member_version: 3,
            candidate_snapshot: { title: "候选 B" },
            candidate_snapshot_sha256: "b".repeat(64),
          },
        ],
        issues: [],
      },
    },
  };
}

test("exports candidate lineage, risk dispositions, and user decision as three distinct layers", () => {
  const markdown = artifactToMarkdown(governedArtifact(), "测试房间");
  const lineageIndex = markdown.indexOf("## 候选形成谱系");
  const riskIndex = markdown.indexOf("## 精确版本风控意见");
  const userIndex = markdown.indexOf("## 用户最终决定");

  assert.ok(lineageIndex >= 0);
  assert.ok(riskIndex > lineageIndex);
  assert.ok(userIndex > riskIndex);
  assert.match(markdown, /风控意见：支持 · 候选 A/);
  assert.match(markdown, /风控意见不是用户决定、批准、否决或执行授权。/);
  assert.match(markdown, /当前决定：尚未记录当前版本的用户决定/);
  assert.doesNotMatch(markdown, /当前决定：支持候选/);
});

test("exports AI preference and the user's different explicit choice separately", () => {
  const artifact = governedArtifact();
  artifact.user_decision = {
    id: "decision_1",
    decision_version: "artifact_user_decision_v2",
    action: "support",
    rationale: "我已阅读证据与反证，选择候选 B 继续观察。",
    artifact_version: 2,
    ai_preferred_option_id: "candidate_a",
    selected_option_id: "candidate_b",
    preferred_option_id: "candidate_b",
    selected_is_ai_preferred: false,
    created_at: "2026-08-03T08:00:00Z",
    is_current: true,
  };

  const markdown = artifactToMarkdown(artifact);
  assert.match(markdown, /## 用户最终决定/);
  assert.match(markdown, /- 当前决定：支持候选/);
  assert.match(markdown, /- AI 首选：候选 A/);
  assert.match(markdown, /- 我的选择：候选 B/);
  assert.match(markdown, /- 与 AI 首选：不同/);
  assert.match(markdown, /- 决定理由：我已阅读证据与反证，选择候选 B 继续观察。/);
});

test("exports hold without implying that any candidate was selected", () => {
  const artifact = governedArtifact();
  artifact.user_decision = {
    id: "decision_hold",
    decision_version: "artifact_user_decision_v2",
    action: "hold",
    rationale: "等待新的反证。",
    artifact_version: 2,
    ai_preferred_option_id: "candidate_a",
    selected_option_id: "",
    created_at: "2026-08-03T08:00:00Z",
    is_current: true,
  };

  const markdown = artifactToMarkdown(artifact);
  assert.match(markdown, /- AI 首选：候选 A/);
  assert.match(markdown, /- 我的选择：无（暂时保留不表示支持候选）/);
  assert.doesNotMatch(markdown, /绑定候选/);
});

test("legacy support records do not backfill an invented user selection", () => {
  const artifact = governedArtifact();
  artifact.user_decision = {
    id: "decision_legacy",
    action: "support",
    rationale: "旧版支持记录。",
    artifact_version: 2,
    preferred_option_id: "candidate_a",
    created_at: "2026-08-03T08:00:00Z",
    is_current: true,
  };

  const markdown = artifactToMarkdown(artifact);
  assert.match(markdown, /- AI 首选：候选 A/);
  assert.match(markdown, /- 我的选择：旧版未单独记录；当时系统等同 AI 首选，不能证明人类做过单独候选选择/);
});

test("legacy exports state that governance cannot be inferred", () => {
  const artifact = governedArtifact();
  delete artifact.governance_snapshot;

  const markdown = artifactToMarkdown(artifact);
  assert.match(markdown, /此历史产物没有治理快照，不能推断候选形成谱系/);
  assert.match(markdown, /此历史产物没有治理快照，不能补写或推断风控意见/);
  assert.match(markdown, /当前决定：尚未记录当前版本的用户决定/);
});
