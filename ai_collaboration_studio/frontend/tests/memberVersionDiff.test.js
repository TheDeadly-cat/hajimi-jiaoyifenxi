import test from "node:test";
import assert from "node:assert/strict";
import { buildMemberVersionDiff, memberVersionSnapshot } from "../src/memberVersionDiff.js";

function record(version, snapshot) {
  return { version, changed_at: 1_700_000_000_000 + version, snapshot };
}

test("compares frozen identity snapshots including lifecycle and capability sets", () => {
  const left = record(1, {
    name: "研究员",
    identity: "基本面分析师",
    responsibilities: "核验财报",
    boundaries: "不做价格预测",
    instructions: "优先使用一手证据",
    stance: "evidence",
    workflow_stage: "analysis",
    provider: "deepseek",
    model: "deepseek-chat",
    enabled: true,
    archived_at: 0,
    capabilities: ["critical_review", "evidence_review"],
    avatar_color: "#2563eb",
  });
  const right = record(2, {
    ...left.snapshot,
    identity: "反方基本面分析师",
    responsibilities: "核验财报并提出反证",
    boundaries: "不输出交易指令",
    provider: "glm",
    model: "glm-4.5",
    enabled: false,
    archived: true,
    archived_at: 1_700_000_000_500,
    capabilities: ["evidence_review", "risk_review"],
  });

  const diff = buildMemberVersionDiff(left, right);

  assert.equal(diff.changed, true);
  assert.deepEqual(
    diff.fieldChanges.map((change) => change.key),
    ["identity", "responsibilities", "boundaries", "provider", "model", "enabled", "archived"],
  );
  assert.deepEqual(diff.capabilities.added, ["risk_review"]);
  assert.deepEqual(diff.capabilities.removed, ["critical_review"]);
});

test("ignores capability ordering and record metadata when snapshots are semantically equal", () => {
  const snapshot = {
    name: "风控",
    identity: "风险负责人",
    enabled: true,
    archived: false,
    capabilities: ["risk_review", "evidence_review"],
  };
  const left = record(3, snapshot);
  const right = record(9, {
    ...snapshot,
    capabilities: ["evidence_review", "risk_review", "risk_review"],
  });

  const diff = buildMemberVersionDiff(left, right);

  assert.equal(diff.changed, false);
  assert.deepEqual(diff.fieldChanges, []);
  assert.equal(diff.capabilities.changed, false);
});

test("accepts API envelopes and direct summary-shaped records", () => {
  const envelope = {
    member_version: record(4, {
      identity: "技术分析师",
      capabilities_json: '["technical_analysis"]',
      enabled: true,
    }),
  };
  const direct = {
    identity: "技术分析师与情绪观察员",
    capabilities: ["technical_analysis", "sentiment_analysis"],
    enabled: true,
  };

  assert.equal(memberVersionSnapshot(envelope).identity, "技术分析师");
  const diff = buildMemberVersionDiff(envelope, direct);
  assert.deepEqual(diff.fieldChanges.map((change) => change.key), ["identity"]);
  assert.deepEqual(diff.capabilities.added, ["sentiment_analysis"]);
});
