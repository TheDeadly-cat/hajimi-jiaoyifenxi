import assert from "node:assert/strict";
import test from "node:test";

import {
  ACTION_CONTINUATION_VERSION,
  buildActionContinuationRequest,
  continuationTargetCandidates,
  normalizeActionDeskContinuationsResponse,
} from "../src/actionContinuation.js";
import { ACTION_DESK_CANDIDATE_VERSION, ACTION_DESK_ITEM_VERSION } from "../src/actionDesk.js";

const hashes = { source: "a".repeat(64), target: "b".repeat(64) };

function candidate(overrides = {}) {
  return {
    version: ACTION_DESK_CANDIDATE_VERSION,
    artifact_id: "artifact_lineage",
    artifact_version: 2,
    artifact_title: "旧确认产物",
    action_id: "action_old",
    action_snapshot_sha256: hashes.source,
    text: "旧版行动",
    owner: "旧负责人",
    due: "",
    state: "open",
    evidence_count: 1,
    source_status: "confirmed_exact",
    ...overrides,
  };
}

function target(overrides = {}) {
  return candidate({
    artifact_version: 4,
    artifact_title: "新版确认产物",
    action_id: "action_new",
    action_snapshot_sha256: hashes.target,
    text: "新版行动",
    owner: "新版负责人",
    ...overrides,
  });
}

function responseFixture() {
  return {
    ok: true,
    continuations: {
      version: ACTION_CONTINUATION_VERSION,
      room_id: "room_lineage",
      integrity_ok: true,
      relations: [{
        version: "artifact_action_continuation_item_v1",
        relation_id: "artifact_action_continuation_event_abcdef123456",
        source: candidate(),
        target: target(),
        source_revision: 2,
        created_at: 1786323600000,
        reason: "新版只是同一件后续事项。",
        integrity_ok: true,
      }],
      counts: { relation_count: 1 },
      issues: [],
      execution_capability: "none",
      external_write: false,
      can_autonomously_decide: false,
      can_replace_user_decision: false,
      user_final_decision_required: true,
    },
  };
}

test("strict continuation view binds one source to one newer exact target", () => {
  const view = normalizeActionDeskContinuationsResponse(responseFixture(), "room_lineage");
  assert.equal(view.valid, true);
  assert.equal(view.metricsVisible, true);
  assert.equal(view.relations.length, 1);
  assert.equal(view.relations[0].source.artifactVersion, 2);
  assert.equal(view.relations[0].target.artifactVersion, 4);
  assert.equal(view.relationBySource.get("artifact_lineage:2:action_old").target.actionId, "action_new");
});

test("request contains only exact identity, revision, reason and explicit confirmation", () => {
  const view = normalizeActionDeskContinuationsResponse(responseFixture(), "room_lineage");
  const source = view.relations[0].source;
  const targetRow = view.relations[0].target;
  const request = buildActionContinuationRequest({
    source,
    target: targetRow,
    sourceRevision: 2,
    reason: "用户确认同一后续事项",
    clientRequestId: "artifact_action_continuation_request_1",
  });
  assert.equal(request.version, ACTION_CONTINUATION_VERSION);
  assert.deepEqual(Object.keys(request).sort(), [
    "client_request_id",
    "reason",
    "source_action_id",
    "source_action_snapshot_sha256",
    "source_artifact_id",
    "source_artifact_version",
    "source_expected_revision",
    "target_action_id",
    "target_action_snapshot_sha256",
    "target_artifact_id",
    "target_artifact_version",
    "user_confirmed",
    "version",
  ].sort());
  assert.equal(request.user_confirmed, true);
  assert.equal(Object.hasOwn(request, "state"), false);
});

test("target candidates require a newer version in the same artifact lineage", () => {
  const view = normalizeActionDeskContinuationsResponse(responseFixture(), "room_lineage");
  const item = {
    ...candidate(),
    version: ACTION_DESK_ITEM_VERSION,
    revision: 2,
    note: "",
    latest_event_id: "artifact_action_event_abcdef123456",
    latest_event_sha256: "c".repeat(64),
    adopted_at: 1786320000000,
    updated_at: 1786323600000,
    source_current: false,
    current_artifact_version: 4,
    integrity_ok: true,
  };
  const candidates = [
    target(),
    target({ artifact_version: 5, action_id: "action_other" }),
    target({ artifact_id: "another_artifact", action_id: "foreign" }),
    candidate({ artifact_version: 1, action_id: "older" }),
  ].map((row) => ({
    ...row,
    valid: true,
    sourceKey: `${row.artifact_id}:${row.artifact_version}:${row.action_id}`,
    artifactId: row.artifact_id,
    artifactVersion: row.artifact_version,
    actionId: row.action_id,
    actionSnapshotSha256: row.action_snapshot_sha256,
  }));
  const filtered = continuationTargetCandidates({
    valid: true,
    artifactId: item.artifact_id,
    artifactVersion: item.artifact_version,
  }, candidates);
  assert.deepEqual(filtered.map((row) => row.artifactVersion), [4, 5]);
});

test("safety, room binding, relation count, and any row drift fail closed", () => {
  const mutations = [
    (value) => { value.continuations.room_id = "other"; },
    (value) => { value.continuations.execution_capability = "write"; },
    (value) => { value.continuations.integrity_ok = false; },
    (value) => { value.continuations.counts.relation_count = 2; },
    (value) => { value.continuations.relations[0].target.artifact_version = 1; },
    (value) => { value.continuations.relations[0].unexpected = true; },
  ];
  for (const mutate of mutations) {
    const payload = responseFixture();
    mutate(payload);
    const view = normalizeActionDeskContinuationsResponse(payload, "room_lineage");
    assert.equal(view.valid === false || view.metricsVisible === false, true);
    assert.deepEqual(view.relations, []);
    assert.equal(view.relationCount, null);
  }
});
