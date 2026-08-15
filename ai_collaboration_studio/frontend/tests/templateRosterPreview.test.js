import assert from "node:assert/strict";
import test from "node:test";

import { templateRosterPreview } from "../src/templateRosterPreview.js";

test("normalizes a template roster without aliasing capability arrays", () => {
  const template = {
    member_count: 2,
    member_preview: [
      {
        name: "基本面分析师",
        identity: "财务与估值",
        responsibilities: "比较盈利质量",
        boundaries: "不使用脱离周期的静态估值",
        stance: "fundamental",
        workflow_stage: "analysis",
        capabilities: ["evidence_review", "evidence_review", "valuation"],
        avatar_color: "#7c5ac7",
      },
      {
        name: "风险经理",
        identity: "风险复核",
        capabilities: [],
        avatar_color: "#b45309",
      },
    ],
  };

  const roster = templateRosterPreview(template);

  assert.equal(roster.available, true);
  assert.equal(roster.previewAvailable, true);
  assert.equal(roster.count, 2);
  assert.equal(roster.partial, false);
  assert.equal(roster.members[0].workflowStage, "analysis");
  assert.deepEqual(roster.members[0].capabilities, ["evidence_review", "valuation"]);
  assert.notEqual(roster.members[0].capabilities, template.member_preview[0].capabilities);
});

test("marks old template catalogs without roster fields as unavailable", () => {
  const expectedUnavailable = {
    available: false,
    previewAvailable: false,
    count: null,
    members: [],
    partial: false,
  };
  assert.deepEqual(templateRosterPreview({ id: "legacy_template", name: "旧模板" }), expectedUnavailable);
  assert.deepEqual(templateRosterPreview({ member_count: null }), expectedUnavailable);
});

test("keeps a declared total while identifying a partial preview", () => {
  const roster = templateRosterPreview({
    member_count: 12,
    member_preview: [{ name: "主持人", identity: "流程守门人" }],
  });

  assert.equal(roster.count, 12);
  assert.equal(roster.members.length, 1);
  assert.equal(roster.partial, true);
});

test("falls back safely for malformed optional member fields", () => {
  const roster = templateRosterPreview({
    member_preview: [null, { capabilities: "not-an-array", avatar_color: "url(bad)" }],
  });

  assert.equal(roster.count, 1);
  assert.equal(roster.members[0].name, "未命名成员");
  assert.equal(roster.members[0].identity, "身份定位未说明");
  assert.deepEqual(roster.members[0].capabilities, []);
  assert.equal(roster.members[0].avatarColor, "#64748b");
});
