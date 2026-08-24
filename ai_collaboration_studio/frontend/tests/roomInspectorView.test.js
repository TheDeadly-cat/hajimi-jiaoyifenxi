import assert from "node:assert/strict";
import test from "node:test";

import {
  ROOM_INSPECTOR_ARTIFACT_LIMIT,
  ROOM_INSPECTOR_DIRECTOR_DECISION_LIMIT,
  ROOM_INSPECTOR_MEMBER_LIMIT,
  ROOM_INSPECTOR_PROVIDER_LIMIT,
  ROOM_INSPECTOR_WALK_FORWARD_BUCKET_LIMIT,
  buildCurrentRoundDirectorDecisions,
  buildRoomInspectorArtifactFingerprint,
  buildRoomInspectorListProjection,
  buildRoomInspectorProviderIndex,
  buildRoomInspectorWorkflowProjection,
  inspectWalkForwardFootprint,
  safeRoomInspectorColor,
} from "../src/roomInspectorView.js";

test("room inspector projections cap collections before local rendering and indexing", () => {
  const members = Array.from(
    { length: ROOM_INSPECTOR_MEMBER_LIMIT + 2 },
    (_, index) => ({ id: "member-" + index }),
  );
  const providers = Array.from(
    { length: ROOM_INSPECTOR_PROVIDER_LIMIT + 1 },
    (_, index) => ({ id: "provider-" + index }),
  );
  const projection = buildRoomInspectorListProjection({ members, memberLimit: 12 });
  const providerIndex = buildRoomInspectorProviderIndex(providers);

  assert.equal(projection.members.visibleCount, 12);
  assert.equal(projection.members.boundedCount, ROOM_INSPECTOR_MEMBER_LIMIT);
  assert.equal(projection.members.hardOmittedCount, 2);
  assert.equal(providerIndex.indexedCount, ROOM_INSPECTOR_PROVIDER_LIMIT);
  assert.equal(providerIndex.projectionLimited, true);
});

test("director decisions use the bounded tail before filtering and deterministic sorting", () => {
  const decisions = Array.from(
    { length: ROOM_INSPECTOR_DIRECTOR_DECISION_LIMIT + 2 },
    (_, index) => ({
      id: "decision-" + index,
      round_id: index === 0 ? "target" : "other",
      created_at: "2026-08-21T10:00:00+08:00",
      sequence_no: index,
    }),
  );
  decisions.at(-1).round_id = "target";
  const projection = buildCurrentRoundDirectorDecisions(decisions, "target");

  assert.equal(projection.projectionLimited, true);
  assert.equal(projection.omittedCount, 2);
  assert.equal(projection.rows.length, 1);
  assert.equal(projection.rows[0].id, decisions.at(-1).id);
});

test("artifact fingerprints are bounded JSON tuples without delimiter ambiguity", () => {
  const first = buildRoomInspectorArtifactFingerprint([
    { id: "a:b", version: 1, status: "ready", content: { actions: [] } },
  ]);
  const second = buildRoomInspectorArtifactFingerprint([
    { id: "a", version: 1, status: "b:ready", content: { actions: [] } },
  ]);
  const capped = buildRoomInspectorArtifactFingerprint(
    Array.from({ length: ROOM_INSPECTOR_ARTIFACT_LIMIT + 1 }, (_, index) => ({ id: index })),
  );

  assert.notEqual(first.fingerprint, second.fingerprint);
  assert.equal(capped.projectedCount, ROOM_INSPECTOR_ARTIFACT_LIMIT);
  assert.equal(capped.projectionLimited, true);
});

test("walk-forward footprint stops at its cap and conservatively preserves visibility", () => {
  const buckets = Object.fromEntries(
    Array.from(
      { length: ROOM_INSPECTOR_WALK_FORWARD_BUCKET_LIMIT + 1 },
      (_, index) => ["portfolio-" + index, []],
    ),
  );
  const footprint = inspectWalkForwardFootprint(buckets);

  assert.equal(footprint.inspectedCount, ROOM_INSPECTOR_WALK_FORWARD_BUCKET_LIMIT);
  assert.equal(footprint.projectionLimited, true);
  assert.equal(footprint.hasHistory, true);
  assert.equal(footprint.confirmedHistory, false);
});

test("workflow and color projections reject unsafe display values", () => {
  const projection = buildRoomInspectorWorkflowProjection({
    stage_order: ["opening", {}, "closing"],
    required_coverage: [{ label: "风险" }, null],
  });
  assert.deepEqual(projection.stageOrder, ["opening", "closing"]);
  assert.deepEqual(projection.requiredCoverage.map((item) => item.label), ["风险"]);
  assert.equal(safeRoomInspectorColor("#1a2b3c"), "#1a2b3c");
  assert.equal(safeRoomInspectorColor("url(https://tracker.invalid)"), "#4f6b8a");
});
