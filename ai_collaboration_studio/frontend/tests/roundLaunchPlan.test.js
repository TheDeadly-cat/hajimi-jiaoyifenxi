import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  buildRoundLaunchAuthorizationPayload,
  createRoundLaunchRequestContext,
  normalizeRoundLaunchPlan,
  roundLaunchAuthorizationState,
  roundLaunchPlanContextState,
} from "../src/roundLaunchPlan.js";
import {
  buildRoundContextAuthorizationSet,
  roundContextAuthorizationEntry,
} from "../src/roundContexts.js";

function readyPlan(overrides = {}) {
  return {
    version: "round_launch_plan_v3",
    plan_hash: "a".repeat(64),
    objective: "比较 MU、SNDK、WDC 与 STX 的存储周期证据",
    room: {
      id: "room_storage",
      settings_version: 4,
      discussion_mode: "dynamic",
      domain: "us_equities_research",
      template_id: "us_storage_committee",
      capability_pack_ids: ["us_equities_research"],
      protocols: {
        turn_contract_version: "turn_contract_v1",
        turn_contract_required: true,
        turn_envelope_version: "turn_envelope_v1",
        turn_envelope_schema_sha256: "b".repeat(64),
        provider_output_capabilities_version: "provider_output_capabilities_v1",
        candidate_risk_review_version: "candidate_risk_review_v1",
        candidate_risk_review_required: true,
      },
    },
    members: [
      {
        id: "member_moderator",
        version: 2,
        name: "研究主持人",
        identity: "控制讨论顺序与证据门槛",
        stage: "evidence_intake",
        provider: "deepseek",
        model: "deepseek-v4-pro",
      },
      {
        id: "member_risk",
        version: 1,
        name: "风险负责人",
        identity: "提出反证与停止条件",
        stage: "risk_review",
        provider: "doubao",
        model: "doubao-seed-test",
      },
    ],
    moderator: {
      id: "member_moderator",
      version: 2,
      name: "研究主持人",
      identity: "控制讨论顺序与证据门槛",
      stage: "evidence_intake",
      provider: "deepseek",
      model: "deepseek-v4-pro",
      selection_source: "configured",
    },
    skip_provider_ids: ["openai"],
    preflight_routes: [
      {
        provider: "deepseek",
        model: "deepseek-v4-pro",
        member_ids: ["member_moderator"],
        known: true,
        configured: true,
        policy_disabled: false,
        skipped: false,
        callable: true,
        projected_preflight_calls: 1,
        output_capabilities_version: "provider_output_capabilities_v1",
        provider_output_modes: ["json_object", "prompt_json"],
        turn_output_mode: "json_object",
        output_capabilities_declared: true,
      },
      {
        provider: "doubao",
        model: "doubao-seed-test",
        member_ids: ["member_risk"],
        known: true,
        configured: true,
        policy_disabled: false,
        skipped: false,
        callable: true,
        projected_preflight_calls: 1,
        output_capabilities_version: "provider_output_capabilities_v1",
        provider_output_modes: ["json_object", "prompt_json"],
        turn_output_mode: "json_object",
        output_capabilities_declared: true,
      },
    ],
    provider_call_projection: [
      {
        provider: "deepseek",
        known: true,
        configured: true,
        policy_disabled: false,
        skipped: false,
        projected_preflight_calls: 1,
        minimum_speaker_calls: 9,
        minimum_director_calls: 0,
        recommended_director_calls: 6,
        optional_artifact_calls: 0,
        contingency_calls: 0,
        projected_provider_calls: 16,
      },
      {
        provider: "doubao",
        known: true,
        configured: true,
        policy_disabled: false,
        skipped: false,
        projected_preflight_calls: 1,
        minimum_speaker_calls: 3,
        minimum_director_calls: 0,
        recommended_director_calls: 0,
        optional_artifact_calls: 1,
        contingency_calls: 0,
        projected_provider_calls: 5,
      },
      {
        provider: "openai",
        known: true,
        configured: true,
        policy_disabled: true,
        skipped: true,
        projected_preflight_calls: 0,
        minimum_speaker_calls: 0,
        minimum_director_calls: 0,
        recommended_director_calls: 0,
        optional_artifact_calls: 0,
        contingency_calls: 0,
        projected_provider_calls: 0,
      },
    ],
    calls: {
      version: "provider_call_budget_profile_v1",
      unit: "provider_call_count",
      is_cost_estimate: false,
      is_usage_forecast: false,
      completion_assumes_valid_responses: true,
      unique_preflight_route_count: 2,
      projected_preflight_calls: 2,
      workflow_minimum_speaker_calls: 12,
      minimum_speaker_calls: 12,
      minimum_director_calls: 0,
      recommended_director_calls: 6,
      optional_artifact_calls: 1,
      contingency_calls: 0,
      recommended_provider_calls: 21,
      recommended_authorization_calls: 21,
      projected_provider_calls_total: 21,
      maximum_speaker_calls: 18,
      maximum_director_calls: 17,
      initial_dispatch_provider_calls: 0,
      runtime_rules_first_can_reduce_calls: true,
      core_success_path_calls: 14,
      formal_path_call_ceiling_with_allowance: 27,
      formal_path_conservative_upper_bound: 38,
      unprojected_call_kinds: ["round_interjection"],
      absolute_ceiling_source: "user_authorized_max_provider_calls",
      discussion_call_range: { minimum: 12, maximum: 35 },
      total_call_range: { minimum: 21, maximum: 38 },
    },
    safety: {
      budget_unit: "provider_call_count",
      is_cost_estimate: false,
      execution_capability: "none",
      live_trading_allowed: false,
      user_confirmation_required: true,
    },
    ready_for_authorization: true,
    blockers: [],
    ...overrides,
  };
}

function focusAuthorization(overrides = {}) {
  return {
    version: "project_round_focus_authorization_v1",
    artifact_binding: {
      status: "exact",
      artifact_id: "artifact_project",
      artifact_version: 3,
    },
    preview_sha256: "c".repeat(64),
    user_confirmed: true,
    ...overrides,
  };
}

function focusReadyPlan(overrides = {}) {
  const plan = readyPlan();
  plan.version = "round_launch_plan_v4";
  plan.room.capability_pack_ids.push("project_round_focus");
  plan.room.plugin_registry_snapshot_sha256 = "d".repeat(64);
  plan.project_round_focus_authorization = focusAuthorization();
  return Object.assign(plan, overrides);
}

function footballContextRequest(overrides = {}) {
  const contractSha256 = overrides.contractSha256 || "e".repeat(64);
  const matchId = overrides.matchId || "match-001";
  const dataCutoffUtc = overrides.dataCutoffUtc || "2026-08-12T10:00:00Z";
  return {
    version: "football_round_context_request_v1",
    payload: {
      version: "football_research_contract_v1",
      capability_pack_id: "football_research_readonly",
      match_identity: {
        competition: { value: "Fixture League" },
        competition_id: { value: "fixture.league" },
        season: { value: "2026-27" },
        match_id: { value: matchId },
        kickoff_utc: { value: "2026-08-13T19:00:00Z" },
        venue: { value: "Fixture Stadium" },
      },
      data_cutoff_utc: dataCutoffUtc,
      contract_sha256: contractSha256,
    },
    authorization: {
      version: "football_round_context_authorization_v1",
      owner_pack_id: "football_research_readonly",
      port_id: "core.football.match_context/v1",
      contract_sha256: contractSha256,
      data_cutoff_utc: dataCutoffUtc,
      match_id: matchId,
      user_confirmed: true,
    },
  };
}

function stockContextRequest(overrides = {}) {
  const contractSha256 = overrides.contractSha256 || "7".repeat(64);
  const scopeSha256 = overrides.scopeSha256 || "8".repeat(64);
  const dataCutoffUtc = overrides.dataCutoffUtc || "2026-08-12T10:00:00Z";
  return {
    version: "stock_round_context_request_v1",
    payload: {
      version: "stock_research_contract_v1",
      capability_pack_id: "stock_research_readonly",
      stock_room_scope: {
        version: "stock_room_scope_v1",
        symbols: ["US:AAPL", "US:MSFT"],
      },
      data_cutoff_utc: dataCutoffUtc,
      symbols: [],
      research_ready: false,
      execution_capability: "none",
      live_trading_allowed: false,
      order_placement_allowed: false,
      wallet_connection_allowed: false,
      automatic_trading_allowed: false,
      can_autonomously_decide: false,
      can_replace_user_decision: false,
      user_final_decision_required: true,
      contract_sha256: contractSha256,
    },
    authorization: {
      version: "stock_round_context_authorization_v1",
      owner_pack_id: "stock_research_readonly",
      port_id: "core.market.readonly_context/v1",
      contract_sha256: contractSha256,
      stock_room_scope_sha256: scopeSha256,
      data_cutoff_utc: dataCutoffUtc,
      user_confirmed: true,
    },
  };
}

function v5ContextSet({
  project = false,
  football = false,
  stock = false,
  footballOverrides = {},
  stockOverrides = {},
} = {}) {
  return buildRoundContextAuthorizationSet([
    ...(project ? [roundContextAuthorizationEntry(
      "project_round_focus",
      "core.round.context/v1",
      focusAuthorization(),
    )] : []),
    ...(football ? [roundContextAuthorizationEntry(
      "football_research_readonly",
      "core.football.match_context/v1",
      footballContextRequest(footballOverrides),
    )] : []),
    ...(stock ? [roundContextAuthorizationEntry(
      "stock_research_readonly",
      "core.market.readonly_context/v1",
      stockContextRequest(stockOverrides),
    )] : []),
  ]);
}

function v5ReadyPlan({ project = false, football = false, stock = false, overrides = {} } = {}) {
  const plan = readyPlan();
  plan.version = "round_launch_plan_v5";
  if (project) plan.room.capability_pack_ids.push("project_round_focus");
  if (football) plan.room.capability_pack_ids.push("football_research_readonly");
  if (stock) plan.room.capability_pack_ids.push("stock_research_readonly");
  plan.room.plugin_registry_snapshot_sha256 = "d".repeat(64);
  plan.round_context_authorizations = v5ContextSet({ project, football, stock });
  return Object.assign(plan, overrides);
}

test("normalizes the authorization allowance including DeepSeek 16, Doubao 5 and OpenAI 0", () => {
  const source = readyPlan();
  source.members[0].api_key = "must-not-survive";
  source.preflight_routes[0].upstream_response = "must-not-survive";
  const normalized = normalizeRoundLaunchPlan(source);

  assert.equal(normalized.valid, true);
  assert.equal(normalized.ready_for_authorization, true);
  assert.deepEqual(
    normalized.provider_call_projection.map((item) => [
      item.provider,
      item.projected_provider_calls,
    ]),
    [["deepseek", 16], ["doubao", 5], ["openai", 0]],
  );
  assert.equal(normalized.calls.projected_provider_calls_total, 21);
  assert.equal(normalized.calls.core_success_path_calls, 14);
  assert.equal(normalized.calls.recommended_director_calls, 6);
  assert.equal(normalized.calls.formal_path_conservative_upper_bound, 38);
  assert.doesNotMatch(JSON.stringify(normalized), /must-not-survive|api_key|upstream_response/);

  source.members[0].name = "mutated";
  source.skip_provider_ids.push("glm");
  assert.equal(normalized.members[0].name, "研究主持人");
  assert.deepEqual(normalized.skip_provider_ids, ["openai"]);
});

test("builds the exact confirmation payload from the frozen plan", () => {
  const payload = buildRoundLaunchAuthorizationPayload(readyPlan(), {
    clientRoundRequestId: "round-request-123",
    maxProviderCalls: 28,
  });

  assert.deepEqual(payload, {
    client_round_request_id: "round-request-123",
    plan_hash: "a".repeat(64),
    max_provider_calls: 28,
    objective: "比较 MU、SNDK、WDC 与 STX 的存储周期证据",
    skip_providers: ["openai"],
  });
});

test("v4 freezes one exact focus authorization into the plan and stream payload", () => {
  const plan = focusReadyPlan();
  const normalized = normalizeRoundLaunchPlan(plan);
  const payload = buildRoundLaunchAuthorizationPayload(plan, {
    clientRoundRequestId: "round-request-focus",
    maxProviderCalls: 28,
  });

  assert.equal(normalized.valid, true);
  assert.equal(normalized.version, "round_launch_plan_v4");
  assert.deepEqual(normalized.project_round_focus_authorization, focusAuthorization());
  assert.deepEqual(payload.project_round_focus_authorization, focusAuthorization());
  assert.deepEqual(Object.keys(payload).sort(), [
    "client_round_request_id",
    "max_provider_calls",
    "objective",
    "plan_hash",
    "project_round_focus_authorization",
    "skip_providers",
  ]);
});

test("v5 freezes project context only through the generic authorization set", () => {
  const plan = v5ReadyPlan({ project: true });
  const normalized = normalizeRoundLaunchPlan(plan);
  const payload = buildRoundLaunchAuthorizationPayload(plan, {
    clientRoundRequestId: "round-request-project-v5",
    maxProviderCalls: 28,
  });

  assert.equal(normalized.valid, true);
  assert.equal(normalized.version, "round_launch_plan_v5");
  assert.equal(normalized.project_round_focus_authorization, null);
  assert.deepEqual(normalized.round_context_authorizations, v5ContextSet({ project: true }));
  assert.deepEqual(payload.round_context_authorizations, v5ContextSet({ project: true }));
  assert.equal(Object.hasOwn(payload, "project_round_focus_authorization"), false);
});

test("v5 freezes football and project providers in canonical order without candidate controls", () => {
  const plan = v5ReadyPlan({ project: true, football: true });
  const normalized = normalizeRoundLaunchPlan(plan);
  const payload = buildRoundLaunchAuthorizationPayload(plan, {
    clientRoundRequestId: "round-request-football-project-v5",
    maxProviderCalls: 28,
  });
  const dialogSource = readFileSync(
    new URL("../src/components/RoundLaunchDialog.jsx", import.meta.url),
    "utf8",
  );

  assert.equal(normalized.valid, true);
  assert.deepEqual(
    normalized.round_context_authorizations.contexts.map((entry) => [
      entry.owner_pack_id,
      entry.port_id,
    ]),
    [
      ["football_research_readonly", "core.football.match_context/v1"],
      ["project_round_focus", "core.round.context/v1"],
    ],
  );
  assert.deepEqual(payload.round_context_authorizations, plan.round_context_authorizations);
  assert.equal(Object.hasOwn(payload, "project_round_focus_authorization"), false);
  assert.match(dialogSource, /冻结的足球只读上下文/);
  assert.match(dialogSource, /不生成未来胜率/);
  assert.match(dialogSource, /不连接钱包、不执行投注或自动下注/);
  assert.doesNotMatch(dialogSource, /CandidateExperiment|createCandidateExperiment|候选实验/);
});

test("v5 freezes stock through the generic context with the exact seven-field authorization", () => {
  const plan = v5ReadyPlan({ project: true, football: true, stock: true });
  const normalized = normalizeRoundLaunchPlan(plan);
  const payload = buildRoundLaunchAuthorizationPayload(plan, {
    clientRoundRequestId: "round-request-stock-v5",
    maxProviderCalls: 28,
  });
  const dialogSource = readFileSync(
    new URL("../src/components/RoundLaunchDialog.jsx", import.meta.url),
    "utf8",
  );
  const stockEntry = normalized.round_context_authorizations.contexts.find((entry) => (
    entry.owner_pack_id === "stock_research_readonly"
  ));

  assert.equal(normalized.valid, true);
  assert.deepEqual(
    normalized.round_context_authorizations.contexts.map((entry) => entry.owner_pack_id),
    ["football_research_readonly", "project_round_focus", "stock_research_readonly"],
  );
  assert.deepEqual(Object.keys(stockEntry.request.authorization).sort(), [
    "contract_sha256",
    "data_cutoff_utc",
    "owner_pack_id",
    "port_id",
    "stock_room_scope_sha256",
    "user_confirmed",
    "version",
  ]);
  assert.deepEqual(payload.round_context_authorizations, plan.round_context_authorizations);
  assert.match(dialogSource, /冻结的股票只读上下文/);
  assert.match(dialogSource, /显式股票池/);
  assert.match(dialogSource, /股票池封印/);
  assert.match(dialogSource, /不下单、不自动交易/);
  assert.doesNotMatch(dialogSource, /CandidateExperiment|createCandidateExperiment|候选实验/);
});

test("stock scope hash drift rotates launch identity and cannot satisfy a stock-selected plan", () => {
  let generated = 0;
  const createRequestId = () => `round-stock-request-${++generated}`;
  const plan = v5ReadyPlan({ stock: true });
  const original = v5ContextSet({ stock: true });
  const drifted = v5ContextSet({ stock: true, stockOverrides: { scopeSha256: "9".repeat(64) } });
  const context = {
    roomId: "room_storage",
    roomSettingsVersion: 4,
    objective: plan.objective,
    roundContextAuthorizations: original,
  };
  const first = createRoundLaunchRequestContext(context, { createRequestId });
  const second = createRoundLaunchRequestContext({
    ...context,
    roundContextAuthorizations: drifted,
  }, { previous: first, createRequestId });

  assert.equal(roundLaunchPlanContextState(plan, {
    ...context,
    skipProviders: ["openai"],
  }).matches, true);
  assert.equal(roundLaunchPlanContextState(plan, {
    ...context,
    skipProviders: ["openai"],
    roundContextAuthorizations: drifted,
  }).matches, false);
  assert.notEqual(second.clientRoundRequestId, first.clientRoundRequestId);

  const missing = v5ReadyPlan({ stock: true });
  missing.round_context_authorizations = buildRoundContextAuthorizationSet([]);
  assert.equal(normalizeRoundLaunchPlan(missing).valid, false);
});

test("v5 context authorization fails closed across missing, extra, wrong-port and legacy ambiguity", () => {
  const missing = v5ReadyPlan({ football: true });
  missing.round_context_authorizations = buildRoundContextAuthorizationSet([]);
  assert.equal(normalizeRoundLaunchPlan(missing).valid, false);

  const wrongPort = v5ReadyPlan({ football: true });
  wrongPort.round_context_authorizations = buildRoundContextAuthorizationSet([
    roundContextAuthorizationEntry(
      "football_research_readonly",
      "core.football.wrong_context/v1",
      footballContextRequest(),
    ),
  ]);
  assert.equal(normalizeRoundLaunchPlan(wrongPort).valid, false);

  const extra = v5ReadyPlan({ football: true });
  extra.round_context_authorizations = buildRoundContextAuthorizationSet([
    ...extra.round_context_authorizations.contexts,
    roundContextAuthorizationEntry(
      "project_round_focus",
      "core.round.context/v1",
      focusAuthorization(),
    ),
  ]);
  assert.equal(normalizeRoundLaunchPlan(extra).valid, false);

  const ambiguousPlan = v5ReadyPlan({ football: true });
  ambiguousPlan.project_round_focus_authorization = focusAuthorization();
  assert.equal(normalizeRoundLaunchPlan(ambiguousPlan).valid, false);
  assert.throws(() => createRoundLaunchRequestContext({
    roomId: "room_storage",
    roomSettingsVersion: 4,
    objective: ambiguousPlan.objective,
    projectRoundFocusAuthorization: focusAuthorization(),
    roundContextAuthorizations: ambiguousPlan.round_context_authorizations,
  }, { createRequestId: () => "round-request-ambiguous" }), /cannot be combined/);
});

test("football context hash drift invalidates the frozen plan and rotates request identity", () => {
  let generated = 0;
  const createRequestId = () => `round-football-request-${++generated}`;
  const plan = v5ReadyPlan({ football: true });
  const originalSet = v5ContextSet({ football: true });
  const driftedSet = v5ContextSet({
    football: true,
    footballOverrides: { contractSha256: "f".repeat(64) },
  });
  const context = {
    roomId: "room_storage",
    roomSettingsVersion: 4,
    objective: plan.objective,
    roundContextAuthorizations: originalSet,
  };
  const first = createRoundLaunchRequestContext(context, { createRequestId });
  const retry = createRoundLaunchRequestContext(context, { previous: first, createRequestId });
  const drifted = createRoundLaunchRequestContext({
    ...context,
    roundContextAuthorizations: driftedSet,
  }, { previous: retry, createRequestId });

  assert.equal(roundLaunchPlanContextState(plan, {
    ...context,
    skipProviders: ["openai"],
  }).matches, true);
  const driftState = roundLaunchPlanContextState(plan, {
    ...context,
    skipProviders: ["openai"],
    roundContextAuthorizations: driftedSet,
  });
  assert.equal(driftState.matches, false);
  assert.match(driftState.error, /sources changed/);
  assert.equal(retry.clientRoundRequestId, first.clientRoundRequestId);
  assert.notEqual(drifted.clientRoundRequestId, first.clientRoundRequestId);
});

test("v3 and v4 fail closed across missing, extra, drifted, or unsealed focus authorization", () => {
  const missing = focusReadyPlan();
  delete missing.project_round_focus_authorization;
  assert.equal(normalizeRoundLaunchPlan(missing).valid, false);

  const unsealed = focusReadyPlan();
  delete unsealed.room.plugin_registry_snapshot_sha256;
  assert.equal(normalizeRoundLaunchPlan(unsealed).valid, false);

  const extraOnV3 = readyPlan({ project_round_focus_authorization: focusAuthorization() });
  assert.equal(normalizeRoundLaunchPlan(extraOnV3).valid, false);

  const changedPreview = focusReadyPlan();
  changedPreview.project_round_focus_authorization.preview_sha256 = "e".repeat(64);
  const state = roundLaunchPlanContextState(changedPreview, {
    roomId: "room_storage",
    roomSettingsVersion: 4,
    objective: changedPreview.objective,
    skipProviders: ["openai"],
    projectRoundFocusAuthorization: focusAuthorization(),
  });
  assert.equal(state.matches, false);
  assert.match(state.error, /焦点来源或预览封印已变化/);
});

test("focus request idempotency binds authorization semantics while objective remains independently editable", () => {
  let generated = 0;
  const createRequestId = () => `round-focus-request-${++generated}`;
  const base = {
    roomId: "room_storage",
    roomSettingsVersion: 4,
    objective: "先补齐证据缺口",
    projectRoundFocusAuthorization: focusAuthorization(),
  };
  const first = createRoundLaunchRequestContext(base, { createRequestId });
  const retry = createRoundLaunchRequestContext(base, { previous: first, createRequestId });
  const editedObjective = createRoundLaunchRequestContext({
    ...base,
    objective: "用户编辑后的证据目标",
  }, { previous: retry, createRequestId });
  const changedPreview = createRoundLaunchRequestContext({
    ...base,
    projectRoundFocusAuthorization: focusAuthorization({ preview_sha256: "e".repeat(64) }),
  }, { previous: retry, createRequestId });

  assert.equal(retry.clientRoundRequestId, first.clientRoundRequestId);
  assert.equal(editedObjective.projectRoundFocusAuthorization.preview_sha256, "c".repeat(64));
  assert.notEqual(editedObjective.clientRoundRequestId, first.clientRoundRequestId);
  assert.notEqual(changedPreview.clientRoundRequestId, first.clientRoundRequestId);
});

test("allows an integer below the recommendation but returns an explicit warning", () => {
  const state = roundLaunchAuthorizationState(readyPlan(), 20);

  assert.equal(state.validLimit, true);
  assert.equal(state.canConfirm, true);
  assert.equal(state.belowRecommended, true);
  assert.equal(state.warning.code, "BELOW_RECOMMENDED_PROVIDER_CALLS");
  assert.match(state.warning.message, /低于推荐的 21 次/);
  assert.equal(buildRoundLaunchAuthorizationPayload(readyPlan(), {
    clientRoundRequestId: "round-request-low",
    maxProviderCalls: 20,
  }).max_provider_calls, 20);
});

test("rejects every non-integer or out-of-range Provider call limit", () => {
  for (const value of [0, 29, 101, 1.5, "28", Number.NaN, true]) {
    const state = roundLaunchAuthorizationState(readyPlan(), value);
    assert.equal(state.validLimit, false, String(value));
    assert.equal(state.canConfirm, false, String(value));
    assert.throws(() => buildRoundLaunchAuthorizationPayload(readyPlan(), {
      clientRoundRequestId: "round-request-invalid",
      maxProviderCalls: value,
    }), /1 到 28/);
  }
});

test("fails closed for backend blockers and an unverifiable execution boundary", () => {
  const blocked = readyPlan({
    ready_for_authorization: false,
    blockers: [{
      code: "PROVIDER_NOT_CONFIGURED",
      provider: "deepseek",
      model: "deepseek-v4-pro",
    }],
  });
  const blockedState = roundLaunchAuthorizationState(blocked, 28);
  assert.equal(blockedState.canConfirm, false);
  assert.equal(blockedState.plan.blockers[0].code, "PROVIDER_NOT_CONFIGURED");
  assert.throws(() => buildRoundLaunchAuthorizationPayload(blocked, {
    clientRoundRequestId: "round-request-blocked",
    maxProviderCalls: 28,
  }), /阻断项/);

  const unsafe = readyPlan({
    safety: {
      budget_unit: "provider_call_count",
      is_cost_estimate: false,
      execution_capability: "orders",
      live_trading_allowed: true,
      user_confirmation_required: true,
    },
  });
  const unsafeState = roundLaunchAuthorizationState(unsafe, 28);
  assert.equal(unsafeState.plan.valid, false);
  assert.equal(unsafeState.canConfirm, false);
  assert.ok(unsafeState.plan.blockers.some((item) => item.code === "CLIENT_PLAN_INVALID"));
});

test("fails closed for an unsupported plan version or malformed skip list", () => {
  const unsupported = roundLaunchAuthorizationState(readyPlan({
    version: "round_launch_plan_v0",
  }), 28);
  assert.equal(unsupported.plan.valid, false);
  assert.equal(unsupported.canConfirm, false);

  const malformedSkip = roundLaunchAuthorizationState(readyPlan({
    skip_provider_ids: ["openai", "not a provider"],
  }), 28);
  assert.equal(malformedSkip.plan.valid, false);
  assert.equal(malformedSkip.canConfirm, false);
  assert.ok(malformedSkip.plan.blockers.some((item) => item.code === "CLIENT_PLAN_INVALID"));
});

test("rejects a missing caller request id without mutating the ready plan", () => {
  const source = readyPlan();
  assert.throws(() => buildRoundLaunchAuthorizationPayload(source, {
    clientRoundRequestId: "",
    maxProviderCalls: 28,
  }), /client_round_request_id/);
  assert.equal(source.ready_for_authorization, true);
  assert.deepEqual(source.blockers, []);
});

test("reuses one client request id for the same plan retry and rotates it after drift", () => {
  let generated = 0;
  const createRequestId = () => `round-request-${++generated}`;
  const input = {
    roomId: "room_storage",
    roomSettingsVersion: 4,
    objective: "比较 MU、SNDK、WDC 与 STX 的存储周期证据",
  };
  const first = createRoundLaunchRequestContext(input, { createRequestId });
  const retry = createRoundLaunchRequestContext(input, {
    previous: first,
    createRequestId,
  });
  const changed = createRoundLaunchRequestContext(
    { ...input, roomSettingsVersion: 5 },
    { previous: retry, createRequestId },
  );

  assert.equal(first.clientRoundRequestId, "round-request-1");
  assert.equal(retry.clientRoundRequestId, first.clientRoundRequestId);
  assert.equal(changed.clientRoundRequestId, "round-request-2");
  assert.equal(generated, 2);
});

test("detects room, settings, objective and skip-policy drift before confirmation", () => {
  const context = {
    roomId: "room_storage",
    roomSettingsVersion: 4,
    objective: "比较 MU、SNDK、WDC 与 STX 的存储周期证据",
    skipProviders: ["openai"],
  };
  assert.equal(roundLaunchPlanContextState(readyPlan(), context).matches, true);

  const cases = [
    [{ ...context, roomId: "room_other" }, /房间已切换/],
    [{ ...context, roomSettingsVersion: 5 }, /配置版本已变化/],
    [{ ...context, objective: "另一个目标" }, /目标已变化/],
    [{ ...context, skipProviders: [] }, /跳过策略已变化/],
  ];
  for (const [changedContext, expectedError] of cases) {
    const state = roundLaunchPlanContextState(readyPlan(), changedContext);
    assert.equal(state.matches, false);
    assert.match(state.error, expectedError);
  }
});

test("fails closed when a Provider total does not equal its backend breakdown", () => {
  const plan = readyPlan();
  plan.provider_call_projection[0].projected_provider_calls = 15;
  plan.calls.projected_provider_calls_total = 20;
  const normalized = normalizeRoundLaunchPlan(plan);

  assert.equal(normalized.valid, false);
  assert.equal(normalized.ready_for_authorization, false);
  assert.ok(normalized.normalization_errors.some((item) => item.includes("明细与合计")));
});

test("the formal start path reads a plan before streaming and never calls manual preflight", () => {
  const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
  const planningSource = appSource.slice(
    appSource.indexOf("const requestRoundLaunchPlan"),
    appSource.indexOf("const retryRoundLaunchPlan"),
  );
  const confirmationSource = appSource.slice(
    appSource.indexOf("const confirmRoundLaunch"),
    appSource.indexOf("const resumePausedRound"),
  );

  assert.match(planningSource, /api\.roundLaunchPlan\(/);
  assert.doesNotMatch(planningSource, /preflightProviders|confirmRoundProviders|setComposer\(""\)/);
  assert.match(confirmationSource, /setComposer\(""\)/);
  assert.match(confirmationSource, /streamRound\(targetRoomId, payload/);
});

test("resume delegates preflight to the server under the existing ledger", () => {
  const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
  const apiSource = readFileSync(new URL("../src/api.js", import.meta.url), "utf8");
  const resumeSource = appSource.slice(
    appSource.indexOf("const resumePausedRound"),
    appSource.indexOf("const refreshMarket"),
  );
  const resumeApiSource = apiSource.slice(apiSource.indexOf("export function resumeRound"));

  assert.match(resumeSource, /streamResumeRound\(/);
  assert.doesNotMatch(resumeSource, /preflightProviders|confirmRoundProviders|roundProviderReady/);
  const resumeCallPattern = /\r?\n    \{},\r?\n    onEvent/;
  assert.match(resumeApiSource, resumeCallPattern);
  assert.match(resumeApiSource.replace(/\r?\n/g, "\r\n"), resumeCallPattern);
  assert.doesNotMatch(resumeApiSource, /skip_providers/);
});

test("the stream API forwards the frozen confirmation payload unchanged", () => {
  const apiSource = readFileSync(new URL("../src/api.js", import.meta.url), "utf8");
  const streamSource = apiSource.slice(
    apiSource.indexOf("export function streamRound"),
    apiSource.indexOf("export function resumeRound"),
  );

  assert.match(apiSource, /\/round-launch-plan`/);
  assert.match(streamSource, /authorizationPayload/);
  assert.doesNotMatch(streamSource, /\{ objective, skip_providers/);
});

test("the dialog renders the backend full Provider total instead of preflight-only totals", () => {
  const dialogSource = readFileSync(
    new URL("../src/components/RoundLaunchDialog.jsx", import.meta.url),
    "utf8",
  );

  assert.match(dialogSource, /projected_provider_calls_total/);
  assert.match(dialogSource, /provider\.projected_provider_calls/);
  assert.match(dialogSource, /provider\.minimum_speaker_calls/);
  assert.match(dialogSource, /round_director/);
  assert.match(dialogSource, /独立主持子预算/);
  assert.match(dialogSource, /包含在全局硬上限内/);
});
