import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  ACTION_DESK_CANDIDATE_VERSION,
  ACTION_DESK_ITEM_VERSION,
  ACTION_DESK_TRANSITION_VERSION,
  ACTION_DESK_VERSION,
  actionDeskComposerText,
  buildActionDeskTransitionRequest,
  normalizeActionDeskResponse,
} from "../src/actionDesk.js";

const hashes = {
  actionOne: "a".repeat(64),
  actionTwo: "b".repeat(64),
  eventOne: "c".repeat(64),
  eventTwo: "d".repeat(64),
};

function candidate(overrides = {}) {
  return {
    version: ACTION_DESK_CANDIDATE_VERSION,
    artifact_id: "artifact_exact",
    artifact_version: 4,
    artifact_title: "确认项目纪要",
    action_id: "action_candidate",
    action_snapshot_sha256: hashes.actionOne,
    text: "补齐第一阶段验收标准",
    owner: "",
    due: "",
    state: "open",
    evidence_count: 2,
    source_status: "confirmed_exact",
    ...overrides,
  };
}

function item(overrides = {}) {
  return {
    ...candidate({
      action_id: "action_adopted",
      action_snapshot_sha256: hashes.actionTwo,
      text: "复核供应商容量约束",
      owner: "项目负责人",
      due: "本周五",
      state: "blocked",
      evidence_count: 1,
    }),
    version: ACTION_DESK_ITEM_VERSION,
    revision: 2,
    note: "等待供应商补件",
    latest_event_id: "action_event_2",
    latest_event_sha256: hashes.eventTwo,
    adopted_at: 1786320000000,
    updated_at: 1786323600000,
    source_current: true,
    current_artifact_version: 4,
    integrity_ok: true,
    ...overrides,
  };
}

function responseFixture() {
  return {
    ok: true,
    action_desk: {
      version: ACTION_DESK_VERSION,
      room_id: "room_action",
      integrity_ok: true,
      candidates: [candidate()],
      items: [item()],
      counts: {
        candidate_count: 1,
        item_count: 1,
        open_count: 0,
        in_progress_count: 0,
        blocked_count: 1,
        done_count: 0,
        cancelled_count: 0,
      },
      issues: [],
      execution_capability: "none",
      external_write: false,
      can_autonomously_decide: false,
      can_replace_user_decision: false,
      user_final_decision_required: true,
    },
  };
}

test("strict action desk view exposes exact candidates, adopted items, and trusted counts", () => {
  const view = normalizeActionDeskResponse(responseFixture(), "room_action");

  assert.equal(view.valid, true);
  assert.equal(view.integrityOk, true);
  assert.equal(view.countsVisible, true);
  assert.equal(view.candidates[0].artifactVersion, 4);
  assert.equal(view.candidates[0].sourceStatus, "confirmed_exact");
  assert.equal(view.items[0].revision, 2);
  assert.equal(view.items[0].sourceCurrent, true);
  assert.equal(view.counts.blockedCount, 1);
});

test("top-level binding, integrity, and fixed safety drift fail the whole card closed", () => {
  const mutations = [
    (value) => { value.action_desk.room_id = "other_room"; },
    (value) => { value.action_desk.integrity_ok = false; },
    (value) => { value.action_desk.execution_capability = "write"; },
    (value) => { value.action_desk.external_write = true; },
    (value) => { value.action_desk.can_autonomously_decide = true; },
    (value) => { value.action_desk.can_replace_user_decision = true; },
    (value) => { value.action_desk.user_final_decision_required = false; },
    (value) => { value.action_desk.unexpected = true; },
  ];

  for (const mutate of mutations) {
    const response = responseFixture();
    mutate(response);
    const view = normalizeActionDeskResponse(response, "room_action");
    assert.equal(view.valid, false);
    assert.equal(view.metricsVisible, false);
    assert.deepEqual(view.candidates, []);
    assert.deepEqual(view.items, []);
    assert.equal(view.counts, null);
  }
});

test("integrity failure accepts only exact structured issue rows while hiding all business data", () => {
  const response = responseFixture();
  response.action_desk.integrity_ok = false;
  response.action_desk.issues = [{
    code: "ACTION_DESK_ITEM_INTEGRITY_FAILED",
    item_key: "artifact:artifact_exact:version:4:action:action_adopted",
    message: "The adopted item failed integrity verification.",
  }];
  response.action_desk.items[0] = item({
    artifact_title: "",
    action_id: "",
    action_snapshot_sha256: "",
    text: "",
    owner: "",
    due: "",
    state: "",
    evidence_count: 0,
    source_status: "integrity_failed",
    revision: 0,
    note: "",
    latest_event_id: "",
    latest_event_sha256: "",
    adopted_at: 0,
    updated_at: 0,
    source_current: false,
    current_artifact_version: 0,
    integrity_ok: false,
  });
  const view = normalizeActionDeskResponse(response, "room_action");

  assert.equal(view.valid, false);
  assert.equal(view.metricsVisible, false);
  assert.equal(view.issueDetails[0].code, "ACTION_DESK_ITEM_INTEGRITY_FAILED");
  assert.deepEqual(view.items, []);
});

test("damaged item integrity or source state redacts that row and all aggregate metrics", () => {
  const cases = [
    (value) => { value.action_desk.items[0].integrity_ok = false; },
    (value) => { value.action_desk.items[0].state = "winner"; },
    (value) => { value.action_desk.items[0].source_status = "latest"; },
    (value) => { value.action_desk.items[0].updated_at = Number.MAX_SAFE_INTEGER; },
    (value) => {
      value.action_desk.items[0].source_current = false;
      value.action_desk.items[0].current_artifact_version = 3;
    },
  ];

  for (const mutate of cases) {
    const response = responseFixture();
    mutate(response);
    const view = normalizeActionDeskResponse(response, "room_action");
    assert.equal(view.valid, true);
    assert.equal(view.items[0].valid, false);
    assert.equal(view.items[0].metricsVisible, false);
    assert.equal(view.items[0].text, "");
    assert.equal(view.items[0].owner, "");
    assert.equal(view.countsVisible, false);
    assert.equal(view.counts, null);
  }
});

test("historical exact items remain explicit and never masquerade as the current artifact", () => {
  const response = responseFixture();
  response.action_desk.items[0].source_current = false;
  response.action_desk.items[0].current_artifact_version = 6;
  const view = normalizeActionDeskResponse(response, "room_action");

  assert.equal(view.items[0].valid, true);
  assert.equal(view.items[0].artifactVersion, 4);
  assert.equal(view.items[0].currentArtifactVersion, 6);
  assert.equal(view.items[0].sourceCurrent, false);
});

test("a same-number source remains readable when the current artifact is no longer confirmed", () => {
  const response = responseFixture();
  response.action_desk.items[0].source_current = false;
  response.action_desk.items[0].current_artifact_version = 4;
  const view = normalizeActionDeskResponse(response, "room_action");

  assert.equal(view.items[0].valid, true);
  assert.equal(view.items[0].artifactVersion, 4);
  assert.equal(view.items[0].currentArtifactVersion, 4);
  assert.equal(view.items[0].sourceCurrent, false);
});

test("adopt and update requests use a closed patch and exact optimistic revision", () => {
  const view = normalizeActionDeskResponse(responseFixture(), "room_action");
  const patch = { owner: "张三", due: "8 月 20 日", state: "in_progress", note: "已开始" };
  const adopt = buildActionDeskTransitionRequest({
    source: view.candidates[0],
    transition: "adopt",
    clientRequestId: "artifact_action_transition_adopt0001",
    patch,
  });
  const update = buildActionDeskTransitionRequest({
    source: view.items[0],
    transition: "update",
    clientRequestId: "artifact_action_transition_update0001",
    patch,
  });

  assert.equal(adopt.version, ACTION_DESK_TRANSITION_VERSION);
  assert.equal(adopt.expected_revision, 0);
  assert.equal(update.expected_revision, 2);
  assert.equal(update.expected_action_snapshot_sha256, hashes.actionTwo);
  assert.deepEqual(Object.keys(update.patch).sort(), ["due", "note", "owner", "state"]);
  assert.equal(update.user_confirmed, true);
  assert.throws(() => buildActionDeskTransitionRequest({
    source: view.items[0],
    transition: "update",
    clientRequestId: "artifact_action_transition_badpatch",
    patch: { ...patch, support: true },
  }));
});

test("discussion prefill names the exact artifact version and source action only", () => {
  const view = normalizeActionDeskResponse(responseFixture(), "room_action");
  const text = actionDeskComposerText(view.items[0]);

  assert.match(text, /确认项目纪要 · v4 · action_adopted/);
  assert.doesNotMatch(text, /自动开始|最终决定|支持候选/);
});

test("panel integration cancels local reads, rereads after POST, and keys reads by artifact fingerprint", () => {
  const panelSource = readFileSync(
    new URL("../src/components/ActionDeskPanel.jsx", import.meta.url),
    "utf8",
  );
  const inspectorSource = readFileSync(
    new URL("../src/components/RoomInspector.jsx", import.meta.url),
    "utf8",
  );
  const inspectorViewSource = readFileSync(
    new URL("../src/roomInspectorView.js", import.meta.url),
    "utf8",
  );
  const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");

  assert.match(panelSource, /new AbortController\(\)/);
  assert.match(panelSource, /controller\?\.abort\(\)/);
  assert.match(panelSource, /await api\.transitionActionDesk/);
  assert.match(
    panelSource,
    /const \[rereadReady\] = await Promise\.all\(\[loadDesk\(\), loadContinuations\(\)\]\)/,
  );
  assert.match(panelSource, /\[normalizedArtifactFingerprint, normalizedRoomId\]/);
  assert.match(panelSource, /setMutation\(EMPTY_MUTATION_STATE\);\s*void loadDesk\(\)/);
  assert.match(inspectorSource, /actionDeskArtifactFingerprint/);
  assert.match(inspectorSource, /buildRoomInspectorArtifactFingerprint/);
  assert.match(inspectorViewSource, /ROOM_INSPECTOR_ARTIFACT_LIMIT/);
  assert.match(inspectorViewSource, /array\(record\(artifact\.content\)\.actions\)\.length/);
  assert.match(inspectorViewSource, /fingerprint: JSON\.stringify/);
  assert.match(inspectorSource, /<ActionDeskPanel/);
  assert.match(appSource, /const fillActionDeskComposer/);
  const callback = appSource.slice(
    appSource.indexOf("const fillActionDeskComposer"),
    appSource.indexOf("const fillRoundFocusObjective"),
  );
  assert.doesNotMatch(callback, /startRound|onUserDecision|createArtifactUserDecision/);
  assert.match(callback, /setInspectorOpen\(false\)/);
});
