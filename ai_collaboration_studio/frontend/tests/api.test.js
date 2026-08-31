import assert from "node:assert/strict";
import test from "node:test";

import { api } from "../src/api.js";

function jsonResponse(payload = { ok: true }) {
  return {
    ok: true,
    status: 200,
    json: async () => payload,
  };
}

test("manual ChatGPT collaboration keeps reads and mutations on encoded room/session routes", async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  const createPayload = { objective: "比较候选方案", mode: "standard" };
  const reviewPayload = {
    provider: "openai",
    model: "gpt-test",
    client_request_id: "review-one",
    expected_result_sha256: "a".repeat(64),
  };
  const freezePayload = {
    expected_result_sha256: "a".repeat(64),
    decision_card_sha256: "b".repeat(64),
    selected_option_id: "option_1",
    acknowledgement: "RESEARCH_ONLY_USER_DECISION",
  };
  globalThis.fetch = async (path, options = {}) => {
    requests.push({ path, options });
    return jsonResponse({ ok: true, manual_chatgpt: {} });
  };
  try {
    await api.latestManualChatGPT("room/一");
    await api.createManualChatGPT("room/一", createPayload);
    await api.dispatchManualChatGPT("room/一", "session/二");
    await api.importManualChatGPT("room/一", "session/二", "{\"version\":\"manual_chatgpt_result_v1\"}");
    await api.runManualChatGPTReview("room/一", "session/二", reviewPayload);
    await api.freezeManualChatGPT("room/一", "session/二", freezePayload);
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(requests[0].path, "/api/rooms/room%2F%E4%B8%80/chatgpt-collaborations/latest");
  assert.equal(requests[0].options.method, undefined);
  assert.equal(requests[1].path, "/api/rooms/room%2F%E4%B8%80/chatgpt-collaborations");
  assert.equal(requests[1].options.method, "POST");
  assert.deepEqual(JSON.parse(requests[1].options.body), createPayload);
  assert.equal(
    requests[2].path,
    "/api/rooms/room%2F%E4%B8%80/chatgpt-collaborations/session%2F%E4%BA%8C/dispatch",
  );
  assert.equal(requests[2].options.method, "POST");
  assert.deepEqual(JSON.parse(requests[2].options.body), {});
  assert.equal(
    requests[3].path,
    "/api/rooms/room%2F%E4%B8%80/chatgpt-collaborations/session%2F%E4%BA%8C/imports",
  );
  assert.equal(requests[3].options.method, "POST");
  assert.deepEqual(JSON.parse(requests[3].options.body), {
    content: "{\"version\":\"manual_chatgpt_result_v1\"}",
  });
  assert.equal(
    requests[4].path,
    "/api/rooms/room%2F%E4%B8%80/chatgpt-collaborations/session%2F%E4%BA%8C/api-reviews",
  );
  assert.deepEqual(JSON.parse(requests[4].options.body), reviewPayload);
  assert.equal(
    requests[5].path,
    "/api/rooms/room%2F%E4%B8%80/chatgpt-collaborations/session%2F%E4%BA%8C/freeze",
  );
  assert.deepEqual(JSON.parse(requests[5].options.body), freezePayload);
});

test("manual ChatGPT history and zero-call recovery use bounded encoded routes", async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  const recoveryPayload = {
    expected_result_sha256: "c".repeat(64),
    acknowledgement: "REAUTHORIZE_ZERO_CALL_ORPHANED_REVIEW",
  };
  globalThis.fetch = async (path, options = {}) => {
    requests.push({ path, options });
    return jsonResponse({ ok: true, manual_chatgpt_sessions: [] });
  };
  try {
    await api.listManualChatGPT("room/一", 12);
    await api.recoverManualChatGPTReview("room/一", "session/二", recoveryPayload);
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(
    requests[0].path,
    "/api/rooms/room%2F%E4%B8%80/chatgpt-collaborations?limit=12",
  );
  assert.equal(requests[0].options.method, undefined);
  assert.equal(
    requests[1].path,
    "/api/rooms/room%2F%E4%B8%80/chatgpt-collaborations/session%2F%E4%BA%8C/api-reviews/recover",
  );
  assert.equal(requests[1].options.method, "POST");
  assert.deepEqual(JSON.parse(requests[1].options.body), recoveryPayload);
});

test("source inbox API keeps reads and explicit CAS mutations on isolated monitoring routes", async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  const controller = new AbortController();
  const content = "```json\n{\"version\":\"source_import_packet_v1\"}\n```";
  globalThis.fetch = async (path, options = {}) => {
    requests.push({ path, options });
    return jsonResponse({ ok: true, source_inbox: { items: [], counts: {} } });
  };
  try {
    await api.listSourceInbox({
      state: "AWAITING_USER",
      query: "CI 失败",
      source: "official_source_monitor:sec_filings",
      unread: true,
      limit: 12,
      signal: controller.signal,
    });
    await api.sourceMonitoringHealth(controller.signal);
    await api.sourceInboxNotifications({ limit: 25, signal: controller.signal });
    await api.sourceInboxNotifications({
      after: "opaque/cursor+一",
      limit: 25,
      signal: controller.signal,
    });
    await api.sourceInboxItem("event/一", controller.signal);
    await api.sourceMonitoringPromptTemplate(controller.signal);
    await api.previewSourceInboxImport(content, controller.signal);
    await api.importSourceInbox(content, controller.signal);
    await api.acknowledgeSourceInboxItem("event/一", 3, controller.signal);
    await api.attachSourceInboxItem("event/一", "room/二", 4, controller.signal);
    await api.createSourceInboxRoundDraft(
      "event/一",
      "room/二",
      5,
      "仅形成待审阅草稿",
      controller.signal,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(
    requests[0].path,
    "/api/monitoring/inbox?limit=12&state=AWAITING_USER&q=CI+%E5%A4%B1%E8%B4%A5&source=official_source_monitor%3Asec_filings&unread=true",
  );
  assert.equal(requests[0].options.signal, controller.signal);
  assert.equal(requests[1].path, "/api/monitoring/health");
  assert.equal(requests[2].path, "/api/monitoring/notifications?limit=25");
  assert.equal(
    requests[3].path,
    "/api/monitoring/notifications?limit=25&after=opaque%2Fcursor%2B%E4%B8%80",
  );
  assert.equal(requests[4].path, "/api/monitoring/events/event%2F%E4%B8%80");
  assert.equal(requests[4].options.method, undefined);
  assert.equal(requests[5].path, "/api/monitoring/imports/chatgpt/prompt-template");
  assert.equal(requests[5].options.method, undefined);
  assert.equal(requests[6].path, "/api/monitoring/imports/chatgpt/preview");
  assert.deepEqual(JSON.parse(requests[6].options.body), { content });
  assert.equal(requests[7].path, "/api/monitoring/imports/chatgpt");
  assert.deepEqual(JSON.parse(requests[7].options.body), { content });
  assert.equal(requests[8].path, "/api/monitoring/events/event%2F%E4%B8%80/acknowledge");
  assert.deepEqual(JSON.parse(requests[8].options.body), {
    expected_state_version: 3,
    acknowledgement: true,
  });
  assert.equal(requests[9].path, "/api/monitoring/events/event%2F%E4%B8%80/attach");
  assert.deepEqual(JSON.parse(requests[9].options.body), {
    room_id: "room/二",
    expected_state_version: 4,
  });
  assert.equal(requests[10].path, "/api/monitoring/events/event%2F%E4%B8%80/round-draft");
  assert.deepEqual(JSON.parse(requests[10].options.body), {
    room_id: "room/二",
    expected_state_version: 5,
    objective: "仅形成待审阅草稿",
  });
  assert.equal(requests.filter((request) => request.options.method === "POST").length, 5);
  assert.equal(requests.every((request) => request.options.signal === controller.signal), true);
});

test("source import errors retain bounded field issues for repair UI", async () => {
  const originalFetch = globalThis.fetch;
  const issues = [{ path: "$.items[0]", code: "SOURCE_IMPORT_FIELD_INVALID" }];
  globalThis.fetch = async () => jsonResponse({
    ok: false,
    error: "来源包字段无效。",
    code: "SOURCE_IMPORT_FIELD_INVALID",
    issues,
  });
  try {
    await assert.rejects(
      () => api.importSourceInbox("{}"),
      (error) => {
        assert.equal(error.code, "SOURCE_IMPORT_FIELD_INVALID");
        assert.deepEqual(error.issues, issues);
        assert.deepEqual(error.details, issues);
        return true;
      },
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("room plugin registry uses the encoded read-only route", async () => {
  const originalFetch = globalThis.fetch;
  let request;
  globalThis.fetch = async (path, options) => {
    request = { path, options };
    return jsonResponse({ ok: true, plugin_registry: {} });
  };
  try {
    await api.roomPluginRegistry("room/一");
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(request.path, "/api/rooms/room%2F%E4%B8%80/plugin-registry");
  assert.equal(request.options.method, undefined);
});

test("project readiness uses the encoded exact-version GET and forwards cancellation", async () => {
  const originalFetch = globalThis.fetch;
  let request;
  const controller = new AbortController();
  globalThis.fetch = async (path, options) => {
    request = { path, options };
    return jsonResponse({ ok: true, projection: {} });
  };
  try {
    await api.projectReadiness("room/一", "artifact/二", 7, controller.signal);
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(
    request.path,
    "/api/rooms/room%2F%E4%B8%80/artifacts/artifact%2F%E4%BA%8C/versions/7/project-readiness",
  );
  assert.equal(request.options.method, undefined);
  assert.equal(request.options.body, undefined);
  assert.equal(request.options.signal, controller.signal);
});

test("project round focus preview and frozen record use encoded read-only routes", async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  const controller = new AbortController();
  globalThis.fetch = async (path, options) => {
    requests.push({ path, options });
    return jsonResponse({ ok: true, project_round_focus: {} });
  };
  try {
    await api.projectRoundFocus("room/一", controller.signal);
    await api.projectRoundFocusRecord("room/一", "round/二", controller.signal);
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(requests[0].path, "/api/rooms/room%2F%E4%B8%80/project-round-focus");
  assert.equal(requests[1].path, "/api/rooms/room%2F%E4%B8%80/rounds/round%2F%E4%BA%8C/project-round-focus");
  for (const request of requests) {
    assert.equal(request.options.method, undefined);
    assert.equal(request.options.body, undefined);
    assert.equal(request.options.signal, controller.signal);
  }
});

test("action desk GET and transition POST use encoded room routes and forward cancellation", async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  const controller = new AbortController();
  const payload = {
    version: "artifact_action_transition_v1",
    client_request_id: "artifact_action_transition_test0001",
    artifact_id: "artifact/二",
    artifact_version: 3,
    action_id: "action/一",
    expected_action_snapshot_sha256: "a".repeat(64),
    expected_revision: 1,
    transition: "update",
    patch: { owner: "张三", due: "周五", state: "open", note: "" },
    user_confirmed: true,
  };
  globalThis.fetch = async (path, options) => {
    requests.push({ path, options });
    return jsonResponse({ ok: true, action_desk: {} });
  };
  try {
    await api.actionDesk("room/一", controller.signal);
    await api.transitionActionDesk("room/一", payload, controller.signal);
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(requests[0].path, "/api/rooms/room%2F%E4%B8%80/action-desk");
  assert.equal(requests[0].options.method, undefined);
  assert.equal(requests[0].options.signal, controller.signal);
  assert.equal(requests[1].path, "/api/rooms/room%2F%E4%B8%80/action-desk/transitions");
  assert.equal(requests[1].options.method, "POST");
  assert.equal(requests[1].options.signal, controller.signal);
  assert.deepEqual(JSON.parse(requests[1].options.body), payload);
});

test("cross-room action overview is a query-free read-only GET with cancellation", async () => {
  const originalFetch = globalThis.fetch;
  let request;
  const controller = new AbortController();
  globalThis.fetch = async (path, options) => {
    request = { path, options };
    return jsonResponse({ ok: true, action_desk_overview: {} });
  };
  try {
    await api.actionDeskOverview(controller.signal);
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(request.path, "/api/action-desk/overview");
  assert.equal(request.options.method, undefined);
  assert.equal(request.options.body, undefined);
  assert.equal(request.options.signal, controller.signal);
});

test("action continuation uses an encoded read route and explicit POST only", async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  const controller = new AbortController();
  const payload = {
    version: "artifact_action_continuation_v1",
    client_request_id: "artifact_action_continuation_test0001",
    source_artifact_id: "artifact/old",
    source_artifact_version: 2,
    source_action_id: "action/old",
    source_action_snapshot_sha256: "a".repeat(64),
    source_expected_revision: 1,
    target_artifact_id: "artifact/old",
    target_artifact_version: 4,
    target_action_id: "action/new",
    target_action_snapshot_sha256: "b".repeat(64),
    reason: "same follow-up",
    user_confirmed: true,
  };
  globalThis.fetch = async (path, options = {}) => {
    requests.push({ path, options });
    return jsonResponse({ ok: true, continuations: {} });
  };
  try {
    await api.actionDeskContinuations("room/一", controller.signal);
    await api.continueActionDesk("room/一", payload, controller.signal);
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(requests[0].path, "/api/rooms/room%2F%E4%B8%80/action-desk/continuations");
  assert.equal(requests[0].options.method, undefined);
  assert.equal(requests[0].options.signal, controller.signal);
  assert.equal(requests[1].options.method, "POST");
  assert.equal(requests[1].options.signal, controller.signal);
  assert.deepEqual(JSON.parse(requests[1].options.body), payload);
});

test("plugin lifecycle catalog uses the dedicated read-only route", async () => {
  const originalFetch = globalThis.fetch;
  let request;
  globalThis.fetch = async (path, options) => {
    request = { path, options };
    return jsonResponse({ ok: true, plugin_lifecycle: {} });
  };
  try {
    await api.pluginLifecycle();
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(request.path, "/api/plugin-registry/lifecycle");
  assert.equal(request.options.method, undefined);
});

test("plugin lifecycle preview and transition keep their exact global routes", async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  const controller = new AbortController();
  globalThis.fetch = async (path, options) => {
    requests.push({ path, options });
    return jsonResponse(path.endsWith("/preview")
      ? { ok: true, preview: {} }
      : { ok: true, transition: {} });
  };
  const previewPayload = { version: "plugin_lifecycle_preview_request_v1" };
  const transitionPayload = { version: "plugin_lifecycle_transition_request_v1" };
  try {
    await api.previewPluginLifecycle(previewPayload, controller.signal);
    await api.transitionPluginLifecycle(transitionPayload, controller.signal);
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(requests[0].path, "/api/plugin-registry/lifecycle-events/preview");
  assert.equal(requests[0].options.method, "POST");
  assert.equal(requests[0].options.signal, controller.signal);
  assert.deepEqual(JSON.parse(requests[0].options.body), previewPayload);
  assert.equal(requests[1].path, "/api/plugin-registry/lifecycle-events");
  assert.equal(requests[1].options.method, "POST");
  assert.equal(requests[1].options.signal, controller.signal);
  assert.deepEqual(JSON.parse(requests[1].options.body), transitionPayload);
});

test("storage readiness includes the active room without changing the global default", async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (path, options) => {
    requests.push({ path, options });
    return jsonResponse({ ok: true, readiness: {} });
  };
  try {
    await api.storageReadiness(true, undefined, "room storage/一");
    await api.storageReadiness(false);
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(requests[0].path, "/api/market/storage/readiness?force=1&room=room+storage%2F%E4%B8%80");
  assert.equal(requests[1].path, "/api/market/storage/readiness");
});

test("official attestation confirmation posts only to the material-scoped route", async () => {
  const originalFetch = globalThis.fetch;
  let request;
  globalThis.fetch = async (path, options) => {
    request = { path, options };
    return jsonResponse({ ok: true, material: {} });
  };
  const payload = {
    attestation_id: "attestation_123",
    expected_version: 2,
    source_sha256: "a".repeat(64),
    content_sha256: "b".repeat(64),
    material_snapshot_sha256: "c".repeat(64),
    user_confirmed: true,
  };
  try {
    await api.confirmOfficialAttestation("room/一", "material/二", payload);
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(request.path, "/api/rooms/room%2F%E4%B8%80/materials/material%2F%E4%BA%8C/official-attestation/confirm");
  assert.equal(request.options.method, "POST");
  assert.deepEqual(JSON.parse(request.options.body), payload);
});

test("round execution trace uses the encoded read-only route and forwards cancellation", async () => {
  const originalFetch = globalThis.fetch;
  let request;
  const controller = new AbortController();
  globalThis.fetch = async (path, options) => {
    request = { path, options };
    return jsonResponse({ ok: true, trace: {} });
  };
  try {
    await api.roundExecutionTrace("room/一", "round/二", {
      limit: 200,
      cursor: "opaque cursor",
      signal: controller.signal,
    });
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(
    request.path,
    "/api/rooms/room%2F%E4%B8%80/rounds/round%2F%E4%BA%8C/audit-trace?limit=200&cursor=opaque+cursor",
  );
  assert.equal(request.options.signal, controller.signal);
  assert.equal(request.options.method, undefined);
});

test("discussion audit uses the encoded read-only route and forwards independent cancellation", async () => {
  const originalFetch = globalThis.fetch;
  let request;
  const controller = new AbortController();
  globalThis.fetch = async (path, options) => {
    request = { path, options };
    return jsonResponse({ ok: true, discussion_audit: {} });
  };
  try {
    await api.discussionAudit("room/one", "round/two", controller.signal);
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(
    request.path,
    "/api/rooms/room%2Fone/rounds/round%2Ftwo/discussion-audit",
  );
  assert.equal(request.options.signal, controller.signal);
  assert.equal(request.options.method, undefined);
});

test("artifact evidence graph uses the encoded read-only route and forwards cancellation", async () => {
  const originalFetch = globalThis.fetch;
  let request;
  const controller = new AbortController();
  globalThis.fetch = async (path, options) => {
    request = { path, options };
    return jsonResponse({ ok: true, nodes: [], edges: [] });
  };
  try {
    await api.artifactEvidenceGraph("room/one", "artifact/two", controller.signal);
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(
    request.path,
    "/api/rooms/room%2Fone/artifacts/artifact%2Ftwo/evidence-graph",
  );
  assert.equal(request.options.signal, controller.signal);
  assert.equal(request.options.method, undefined);
});

test("candidate comparison preview posts to the encoded room route and forwards cancellation", async () => {
  const originalFetch = globalThis.fetch;
  let request;
  const controller = new AbortController();
  const payload = {
    version: "candidate_comparison_request_v1",
    run_ids: ["run/一", "run/二"],
    user_confirmed_historical_only: true,
  };
  globalThis.fetch = async (path, options) => {
    request = { path, options };
    return jsonResponse({ ok: true, comparison: {} });
  };
  try {
    await api.previewCandidateComparison("room/一", payload, controller.signal);
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(request.path, "/api/rooms/room%2F%E4%B8%80/candidate-comparisons/preview");
  assert.equal(request.options.method, "POST");
  assert.equal(request.options.signal, controller.signal);
  assert.deepEqual(JSON.parse(request.options.body), payload);
});


test("candidate experiment creation posts the exact payload and forwards cancellation", async () => {
  const originalFetch = globalThis.fetch;
  let request;
  const controller = new AbortController();
  const payload = {
    version: "candidate_experiment_request_v1",
    client_request_id: "candidate_experiment_request_12345678",
    artifact_id: "artifact/二",
    expected_artifact_version: 4,
    expected_governance_attestation_sha256: "a".repeat(64),
    candidate_selections: [
      {
        candidate_id: "candidate/一",
        expected_candidate_revision: 2,
        expected_candidate_origin_message_id: "message_origin",
        expected_candidate_latest_message_id: "message_latest",
        expected_candidate_snapshot_sha256: "b".repeat(64),
      },
      {
        candidate_id: "candidate/二",
        expected_candidate_revision: 3,
        expected_candidate_origin_message_id: "message_origin_2",
        expected_candidate_latest_message_id: "message_latest_2",
        expected_candidate_snapshot_sha256: "c".repeat(64),
      },
    ],
    user_authorized_historical_comparison: true,
  };
  globalThis.fetch = async (path, options) => {
    request = { path, options };
    return jsonResponse({ ok: true, experiment: { id: "cohort/三" } });
  };
  try {
    await api.createCandidateExperiment("room/一", payload, controller.signal);
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(request.path, "/api/rooms/room%2F%E4%B8%80/candidate-experiments");
  assert.equal(request.options.method, "POST");
  assert.equal(request.options.signal, controller.signal);
  assert.deepEqual(JSON.parse(request.options.body), payload);
});


test("candidate experiment reread uses the encoded cohort route and forwards cancellation", async () => {
  const originalFetch = globalThis.fetch;
  let request;
  const controller = new AbortController();
  globalThis.fetch = async (path, options) => {
    request = { path, options };
    return jsonResponse({ ok: true, experiment: {} });
  };
  try {
    await api.candidateExperiment("room/一", "cohort/二", controller.signal);
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(
    request.path,
    "/api/rooms/room%2F%E4%B8%80/candidate-experiments/cohort%2F%E4%BA%8C",
  );
  assert.equal(request.options.method, undefined);
  assert.equal(request.options.signal, controller.signal);
});
