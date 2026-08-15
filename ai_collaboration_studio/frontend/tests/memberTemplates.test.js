import test from "node:test";
import assert from "node:assert/strict";
import { applyMemberTemplate, groupMemberTemplates } from "../src/memberTemplates.js";

test("applies identity fields without changing model routing or lifecycle state", () => {
  const current = {
    id: "member_1",
    version: 7,
    provider: "deepseek",
    model: "deepseek-chat",
    enabled: false,
    identity: "旧身份",
    capabilities: ["old"],
  };
  const template = {
    name: "风险经理",
    identity: "风险预算与否决权",
    responsibilities: "复核风险。",
    boundaries: "不得下单。",
    instructions: "给出条件结论。",
    stance: "risk",
    workflow_stage: "risk",
    avatar_color: "#123456",
    capabilities: ["risk_review"],
    provider: "glm",
    model: "ignored-model",
    enabled: true,
  };

  const applied = applyMemberTemplate(current, template);

  assert.equal(applied.name, "风险经理");
  assert.equal(applied.identity, "风险预算与否决权");
  assert.deepEqual(applied.capabilities, ["risk_review"]);
  assert.equal(applied.id, "member_1");
  assert.equal(applied.version, 7);
  assert.equal(applied.provider, "deepseek");
  assert.equal(applied.model, "deepseek-chat");
  assert.equal(applied.enabled, false);
  assert.notEqual(applied.capabilities, template.capabilities);
});

test("groups templates by their server-provided category", () => {
  const grouped = groupMemberTemplates([
    { id: "a", source_category: "通用共创" },
    { id: "b", source_category: "交易研究 / 美股" },
    { id: "c", source_category: "通用共创" },
  ]);

  assert.deepEqual(
    grouped.map((group) => group.label),
    ["通用共创", "交易研究 / 美股"],
  );
  assert.deepEqual(grouped[0].items.map((item) => item.id), ["a", "c"]);
});
