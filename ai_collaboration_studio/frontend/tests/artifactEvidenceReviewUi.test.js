import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  ARTIFACT_EVIDENCE_REVIEW_PAGE_SIZE,
  artifactEvidenceReviewSourceState,
} from "../src/artifactEvidenceReviewUi.js";
import {
  ARTIFACT_EVIDENCE_SOURCE_LIMIT,
  normalizeArtifactEvidenceResponse,
} from "../src/artifactEvidenceSources.js";

test("projects safe review rows and restores missing selected sources", () => {
  const state = artifactEvidenceReviewSourceState({
    candidates: [{
      type: "message",
      id: "message_one",
      label: { unsafe: true },
      exact: true,
      sourceIdentityExact: true,
      selectable: true,
    }, {
      type: "message",
      id: "message_one",
      label: "duplicate",
    }],
    targets: [{ key: "summary", label: { unsafe: true } }],
    selectedEvidence: ["message:message_one", "material:missing", "material:missing"],
  });

  assert.deepEqual(state.selectedKeys, ["message:message_one", "material:missing"]);
  assert.equal(state.candidateEntries.length, 2);
  assert.equal(state.candidateEntries[0].item.label, "讨论记录 · message_one");
  assert.equal(state.candidateEntries[1].key, "material:missing");
  assert.equal(state.candidateEntries[1].synthetic, true);
  assert.equal(state.candidateEntries[1].item.selectable, false);
  assert.equal(state.targetRows[0].label, "未命名条目 · summary");
  assert.ok(state.issues.some((issue) => issue.includes("来源键重复")));
});

test("fails closed before normalizing oversized authoritative source envelopes", () => {
  const payload = {
    round_id: "round_one",
    authoritative: true,
    sources: Array.from({ length: ARTIFACT_EVIDENCE_SOURCE_LIMIT + 1 }, () => null),
  };
  const normalized = normalizeArtifactEvidenceResponse(payload);

  assert.equal(normalized.authoritative, false);
  assert.deepEqual(normalized.sources, []);
  assert.deepEqual(normalized.issues, ["SOURCE_LIMIT_EXCEEDED"]);

  const state = artifactEvidenceReviewSourceState({
    candidates: payload.sources,
    selectedEvidence: ["message:still_bound"],
  });
  assert.equal(state.blockNewBindings, true);
  assert.equal(state.candidateEntries.length, 1);
  assert.equal(state.candidateEntries[0].key, "message:still_bound");
});

test("review workbench source keeps handler permits, pagination, and responsive contracts", () => {
  const component = readFileSync(
    new URL("../src/components/ArtifactEvidenceReview.jsx", import.meta.url),
    "utf8",
  );
  const styles = readFileSync(
    new URL("../src/styles/artifact-evidence-review-refinement.css", import.meta.url),
    "utf8",
  );

  assert.equal(ARTIFACT_EVIDENCE_REVIEW_PAGE_SIZE, 80);
  assert.match(component, /reviewSourceState\.blockNewBindings/);
  assert.match(component, /typeof onToggle === "function"/);
  assert.match(component, /visibleCandidateEntries/);
  assert.match(component, /所有已绑定来源始终可见/);
  assert.match(styles, /container-name: artifact-evidence-review evidence-review/);
  assert.match(styles, /container-type: inline-size/);
  assert.match(styles, /@container evidence-review \(max-width: 560px\)/);
  assert.match(styles, /prefers-reduced-motion/);
  assert.match(styles, /forced-colors/);
});
