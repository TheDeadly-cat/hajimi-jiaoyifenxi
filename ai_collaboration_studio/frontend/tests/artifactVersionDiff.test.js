import test from "node:test";
import assert from "node:assert/strict";
import { buildArtifactVersionDiff } from "../src/artifactVersionDiff.js";

function record(version, content) {
  return {
    version,
    snapshot: {
      id: "artifact_test",
      room_id: "room_test",
      version,
      title: "测试产物",
      status: "DRAFT",
      content,
    },
  };
}

test("aligns structured items by stable id and reports pure reordering", () => {
  const left = record(1, {
    summary: "旧摘要",
    requirements: [
      { id: "requirement_a", title: "需求 A", status: "proposed", evidence: [] },
      { id: "requirement_b", title: "需求 B", status: "proposed", evidence: [] },
    ],
  });
  const right = record(2, {
    summary: "新摘要",
    requirements: [
      { id: "requirement_b", title: "需求 B", status: "proposed", evidence: [] },
      { id: "requirement_a", title: "需求 A 已确认", status: "confirmed", evidence: [] },
    ],
  });

  const diff = buildArtifactVersionDiff(left, right);
  const requirements = diff.sections.find((section) => section.key === "requirements");

  assert.equal(diff.changed, true);
  assert.equal(diff.scalarChanges.find((change) => change.key === "summary").after, "新摘要");
  assert.equal(requirements.reordered, true);
  assert.deepEqual(requirements.changed.map((item) => item.id), ["requirement_a"]);
  assert.equal(requirements.added.length, 0);
  assert.equal(requirements.removed.length, 0);
});

test("keeps identical sources separate when they support different targets", () => {
  const source = { type: "material", id: "material_1", version: 1, verification_status: "unreviewed" };
  const left = record(1, {
    summary: "摘要",
    summary_evidence: [source],
    requirements: [{ id: "requirement_a", title: "需求 A", evidence: [source] }],
    decision: { status: "undecided", evidence: [] },
  });
  const right = record(2, {
    summary: "摘要",
    summary_evidence: [{ ...source, verification_status: "source_checked" }],
    requirements: [{ id: "requirement_a", title: "需求 A", evidence: [source] }],
    decision: { status: "deferred", evidence: [source] },
  });

  const evidence = buildArtifactVersionDiff(left, right).evidence;

  assert.deepEqual(evidence.changed.map((item) => item.target), ["summary"]);
  assert.deepEqual(evidence.added.map((item) => item.target), ["decision"]);
  assert.equal(evidence.removed.length, 0);
});
