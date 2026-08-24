import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  ARTIFACT_PANEL_MEMBER_LIMIT,
  ARTIFACT_PANEL_SECTION_LIMIT,
  artifactPanelControls,
  artifactPanelErrorMessage,
  artifactPanelRows,
} from "../src/artifactPanelView.js";

test("artifact panel controls keep only unique enabled provider-bound members", () => {
  const view = artifactPanelControls({
    members: [
      { id: "synth_1", name: "整合者", enabled: true, provider: "local", model: "m1" },
      { id: "synth_1", name: "重复", enabled: true, provider: "local" },
      { id: "disabled", enabled: false, provider: "local" },
      { id: "missing_provider", enabled: true, provider: "" },
    ],
    selectedSynthesizerId: "synth_1",
  });

  assert.deepEqual(view.synthesizers.map((member) => member.id), ["synth_1"]);
  assert.equal(view.activeSynthesizerId, "synth_1");
  assert.equal(view.generateDisabled, false);
  assert.equal(view.state, "ready");
});

test("artifact rows count only arrays, validate preferred options, and expose the five-row boundary", () => {
  const artifacts = Array.from({ length: 6 }, (_, index) => ({
    id: "artifact_" + index,
    title: index ? "产物 " + index : "  ",
    status: index === 0 ? "confirmed" : "DRAFT",
    version: index + 1,
    content: index === 0 ? {
      requirements: [{ id: "r1" }],
      risks: "not-an-array",
      actions: [{ id: "a1" }],
      decision: {
        preferred_option_id: "candidate_b",
        options: [{ id: "candidate_a" }, { id: "candidate_b" }],
      },
    } : {},
  }));
  const ledger = artifactPanelRows(artifacts, {
    summarizeEvidence: () => ({
      total: 3,
      unreviewed: 1,
      counter: -2,
      conflict: 1,
      gap: 0,
    }),
  });

  assert.equal(ledger.totalCount, 6);
  assert.equal(ledger.visibleCount, 5);
  assert.equal(ledger.hiddenCount, 1);
  assert.equal(ledger.visibleRows[0].title, "未命名会议产物");
  assert.equal(ledger.visibleRows[0].status, "confirmed");
  assert.equal(ledger.visibleRows[0].itemCount, 2);
  assert.equal(ledger.visibleRows[0].projectCount, 1);
  assert.equal(ledger.visibleRows[0].optionCount, 2);
  assert.equal(ledger.visibleRows[0].preferredRecorded, true);
  assert.equal(ledger.visibleRows[0].audit.counter, 0);
});

test("artifact rows project only the visible window and skip oversized evidence traversal", () => {
  let summarizeCalls = 0;
  const artifacts = Array.from({ length: 1000 }, (_, index) => ({
    id: `artifact_${index}`,
    title: `Artifact ${index}`,
    content: index === 0
      ? { requirements: Array.from({ length: ARTIFACT_PANEL_SECTION_LIMIT + 1 }, () => ({})) }
      : {},
  }));
  const ledger = artifactPanelRows(artifacts, {
    limit: 5,
    summarizeEvidence: () => {
      summarizeCalls += 1;
      return { total: 1 };
    },
  });

  assert.equal(ledger.totalCount, 1000);
  assert.equal(ledger.visibleRows.length, 5);
  assert.equal(summarizeCalls, 4);
  assert.equal(ledger.visibleRows[0].projectionLimited, true);
  assert.equal(ledger.visibleRows[0].metrics.at(-1).value, "—");
});

test("member overflow and missing handlers close generation controls", () => {
  const overflow = artifactPanelControls({
    members: Array.from({ length: ARTIFACT_PANEL_MEMBER_LIMIT + 1 }, () => ({})),
  });
  assert.equal(overflow.generateDisabled, true);
  assert.equal(overflow.memberIntegrityOk, false);
  assert.match(overflow.issue, /安全上限/);

  const missingHandler = artifactPanelControls({ generationHandlerAvailable: false });
  assert.equal(missingHandler.generateDisabled, true);
  assert.match(missingHandler.issue, /处理器不可用/);
  assert.equal(artifactPanelErrorMessage({ message: { unsafe: true } }, "fallback"), "fallback");
});

test("artifact workbench source keeps handler permits, progressive rows, and responsive contracts", () => {
  const component = readFileSync(
    new URL("../src/components/ArtifactPanel.jsx", import.meta.url),
    "utf8",
  );
  const styles = readFileSync(
    new URL("../src/styles/artifact-panel-refinement.css", import.meta.url),
    "utf8",
  );
  assert.match(component, /generationRef\.current/);
  assert.match(component, /typeof onGenerate === "function"/);
  assert.match(component, /typeof onEdit === "function"/);
  assert.match(component, /current \+ 5/);
  assert.match(styles, /artifact-panel-control-ledger/);
  assert.match(styles, /@media \(max-width: 620px\)/);
  assert.match(styles, /forced-colors/);
});
