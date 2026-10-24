import test from "node:test";
import assert from "node:assert/strict";
import {
  buildRoundLaunchAuthorizationPayload,
  normalizeRoundLaunchPlan,
  roundLaunchAuthorizationState,
  roundLaunchPlanContextState,
  roundLaunchSkipPolicyState,
} from "../src/roundLaunchPlan.js";
import { readyPlan } from "./helpers/f03RoundLaunchFixtures.js";

const requested = ["openai"];
const disabledIds = ["deepseek", "doubao", "glm", "openai", "qwen"];
function zeroProjection(provider) {
  return {
    provider, known: provider !== "qwen", configured: false, policy_disabled: true, skipped: true,
    projected_preflight_calls: 0, minimum_speaker_calls: 0, minimum_director_calls: 0,
    recommended_director_calls: 0, optional_artifact_calls: 0, contingency_calls: 0,
    projected_provider_calls: 0,
  };
}
function allDisabledPlan() {
  const plan = readyPlan();
  plan.skip_provider_ids = [...disabledIds];
  plan.provider_call_projection = disabledIds.map(zeroProjection);
  plan.preflight_routes = plan.preflight_routes.map((route) => ({
    ...route, configured: false, policy_disabled: true, skipped: true, callable: false,
    projected_preflight_calls: 0,
  }));
  for (const key of Object.keys(plan.calls)) {
    if (typeof plan.calls[key] === "number") plan.calls[key] = 0;
  }
  plan.calls.unique_preflight_route_count = plan.preflight_routes.length;
  plan.calls.discussion_call_range = { minimum: 0, maximum: 0 };
  plan.calls.total_call_range = { minimum: 0, maximum: 0 };
  plan.ready_for_authorization = false;
  plan.blockers = [
    { code: "PROVIDER_SKIPPED", provider: "deepseek" },
    { code: "WORKFLOW_PROVIDER_COVERAGE_INSUFFICIENT" },
    { code: "MODERATOR_PROVIDER_ROUTE_UNAVAILABLE" },
  ];
  return plan;
}
function context(plan, skipProviders) {
  return {
    roomId: plan.room.id, roomSettingsVersion: plan.room.settings_version,
    objective: plan.objective, skipProviders,
  };
}

test("deployment exclusions are bound to the returned plan and never authorize an all-disabled round", () => {
  const plan = allDisabledPlan();
  assert.equal(normalizeRoundLaunchPlan(plan).valid, true);
  assert.match(roundLaunchPlanContextState(plan, context(plan, requested)).error, /跳过策略已变化/);
  const policy = roundLaunchSkipPolicyState(plan, requested);
  assert.equal(policy.matches, true);
  assert.deepEqual(policy.binding.effective_provider_ids, disabledIds);
  assert.equal(roundLaunchPlanContextState(plan, context(plan, policy.binding.effective_provider_ids)).matches, true);
  assert.equal(roundLaunchAuthorizationState(plan, 1).canConfirm, false);
  assert.throws(() => buildRoundLaunchAuthorizationPayload(plan, {
    clientRoundRequestId: "blocked-deployment", maxProviderCalls: 1,
  }));
  assert.ok(Object.isFrozen(policy.binding));
  for (const key of ["requested_provider_ids", "policy_disabled_provider_ids", "effective_provider_ids"]) {
    assert.ok(Object.isFrozen(policy.binding[key]));
  }
});

test("an unused policy-disabled provider preserves a callable plan and its exact confirmation payload", () => {
  const plan = readyPlan();
  plan.skip_provider_ids.push("qwen");
  plan.provider_call_projection.push(zeroProjection("qwen"));
  const policy = roundLaunchSkipPolicyState(plan, requested);
  assert.equal(policy.matches, true);
  assert.equal(roundLaunchAuthorizationState(plan, 21).canConfirm, true);
  const payload = buildRoundLaunchAuthorizationPayload(plan, {
    clientRoundRequestId: "callable-policy-bound", maxProviderCalls: 21,
  });
  assert.deepEqual(payload.skip_providers, ["openai", "qwen"]);
  assert.equal(roundLaunchSkipPolicyState(plan, requested, policy.binding).matches, true);
  assert.equal(roundLaunchPlanContextState(plan, context(plan, requested)).matches, false);
});

test("extra skip without an explicit policy-disabled projection remains rejected", () => {
  for (const kind of ["missing-projection", "not-disabled", "not-skipped"]) {
    const plan = readyPlan();
    plan.skip_provider_ids.push("qwen");
    if (kind !== "missing-projection") {
      plan.provider_call_projection.push({
        ...zeroProjection("qwen"),
        policy_disabled: kind !== "not-disabled",
        skipped: kind !== "not-skipped",
      });
    }
    assert.equal(normalizeRoundLaunchPlan(plan).valid, true);
    assert.equal(roundLaunchSkipPolicyState(plan, requested).matches, false, kind);
  }
});

test("required or declared exclusions cannot disappear from the effective plan", () => {
  for (const removed of ["openai", "qwen"]) {
    const plan = allDisabledPlan();
    plan.skip_provider_ids = plan.skip_provider_ids.filter((id) => id !== removed);
    assert.equal(roundLaunchSkipPolicyState(plan, requested).matches, false, removed);
  }
});

test("policy-disabled projected calls and callable routes fail closed", () => {
  const plannedCalls = readyPlan();
  plannedCalls.skip_provider_ids.push("deepseek");
  const projection = plannedCalls.provider_call_projection.find((item) => item.provider === "deepseek");
  projection.policy_disabled = true;
  projection.skipped = true;
  assert.equal(normalizeRoundLaunchPlan(plannedCalls).valid, true);
  assert.equal(roundLaunchSkipPolicyState(plannedCalls, requested).matches, false);
  const callableRoute = allDisabledPlan();
  callableRoute.preflight_routes[0].callable = true;
  callableRoute.preflight_routes[0].projected_preflight_calls = 1;
  assert.equal(roundLaunchSkipPolicyState(callableRoute, requested).matches, false);
});

test("all-disabled zero-call plans cannot gain authorization by dropping their blockers", () => {
  const plan = allDisabledPlan();
  plan.ready_for_authorization = true;
  plan.blockers = [];
  assert.equal(roundLaunchSkipPolicyState(plan, requested).matches, false);
});

test("the frozen context rejects changed plan hash, policy declarations, or requested exclusions", () => {
  const plan = allDisabledPlan();
  const binding = roundLaunchSkipPolicyState(plan, requested).binding;
  const changedHash = structuredClone(plan);
  changedHash.plan_hash = "d".repeat(64);
  assert.equal(roundLaunchSkipPolicyState(changedHash, requested, binding).matches, false);
  const changedPolicy = structuredClone(plan);
  changedPolicy.skip_provider_ids = changedPolicy.skip_provider_ids.filter((id) => id !== "qwen");
  changedPolicy.provider_call_projection = changedPolicy.provider_call_projection.filter((item) => item.provider !== "qwen");
  assert.equal(roundLaunchSkipPolicyState(changedPolicy, requested).matches, true);
  assert.equal(roundLaunchSkipPolicyState(changedPolicy, requested, binding).matches, false);
  assert.equal(roundLaunchSkipPolicyState(plan, [], binding).matches, false);
});

test("the original openai-only policy remains exactly compatible and malformed requests stay rejected", () => {
  const plan = readyPlan();
  const policy = roundLaunchSkipPolicyState(plan, requested);
  assert.equal(policy.matches, true);
  assert.deepEqual(policy.binding.effective_provider_ids, requested);
  assert.equal(roundLaunchPlanContextState(plan, context(plan, requested)).matches, true);
  assert.equal(roundLaunchSkipPolicyState(plan, "openai").matches, false);
  assert.equal(roundLaunchSkipPolicyState(plan, ["not a provider"]).matches, false);
});

