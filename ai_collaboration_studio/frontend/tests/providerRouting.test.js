import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  buildProviderRoutingPresentation,
  buildProviderRouteSummary,
  normalizeProviderPreflight,
  normalizedProviderId,
  providerIsAvailable,
  providerRoutingResultFeedback,
  UNASSIGNED_PROVIDER_ID,
} from "../src/providerRouting.js";


const providers = [
  { id: "deepseek", name: "DeepSeek", configured: true, model: "deepseek-default" },
  { id: "doubao", name: "豆包", configured: true, model: "doubao-default" },
];


test("route summary keeps different models on the same provider distinct", () => {
  const summary = buildProviderRouteSummary([
    { id: "m1", enabled: true, provider: "deepseek", model: "model-a" },
    { id: "m2", enabled: true, provider: "deepseek", model: "model-b" },
    { id: "m3", enabled: true, provider: "deepseek", model: "model-a" },
    { id: "m4", enabled: false, provider: "doubao", model: "ignored" },
  ], providers);

  assert.equal(summary.total, 3);
  assert.equal(summary.entries.length, 2);
  assert.deepEqual(
    summary.entries.map((entry) => [entry.id, entry.model, entry.count]),
    [["deepseek", "model-a", 2], ["deepseek", "model-b", 1]],
  );
  assert.match(summary.label, /DeepSeek \/ model-a ×2/);
  assert.match(summary.label, /DeepSeek \/ model-b ×1/);
});


test("blank member model resolves to the provider default route", () => {
  const summary = buildProviderRouteSummary([
    { id: "m1", enabled: true, provider: "deepseek", model: "" },
    { id: "m2", enabled: true, provider: "deepseek" },
  ], providers);

  assert.equal(summary.entries.length, 1);
  assert.equal(summary.entries[0].model, "deepseek-default");
  assert.equal(summary.entries[0].count, 2);
});


test("missing provider remains explicitly unassigned instead of falling back to OpenAI", () => {
  const summary = buildProviderRouteSummary([
    { id: "m1", enabled: true },
    { id: "m2", enabled: true, provider: "   " },
  ], [
    ...providers,
    { id: "openai", name: "OpenAI", configured: true, model: "gpt-default" },
  ]);

  assert.equal(normalizedProviderId(undefined), UNASSIGNED_PROVIDER_ID);
  assert.equal(normalizedProviderId("   "), UNASSIGNED_PROVIDER_ID);
  assert.equal(summary.entries.length, 1);
  assert.equal(summary.entries[0].id, UNASSIGNED_PROVIDER_ID);
  assert.equal(summary.entries[0].name, "未分配执行器");
  assert.equal(summary.entries[0].available, false);
  assert.equal(summary.hasUnassigned, true);
  assert.equal(summary.hasUnavailable, true);
  assert.equal(summary.hasOpenAI, false);
  assert.match(summary.label, /未分配执行器/);
});


test("policy-disabled providers are shown honestly and never count as available", () => {
  const disabledProvider = {
    id: "openai",
    name: "OpenAI",
    configured: true,
    policy_disabled: true,
    model: "gpt-default",
  };
  const summary = buildProviderRouteSummary([
    { id: "m1", enabled: true, provider: "openai" },
  ], [...providers, disabledProvider]);

  assert.equal(providerIsAvailable(providers[0]), true);
  assert.equal(providerIsAvailable(disabledProvider), false);
  assert.equal(providerIsAvailable({ configured: false, policy_disabled: false }), false);
  assert.equal(providerIsAvailable(undefined), false);
  assert.equal(summary.entries[0].configured, true);
  assert.equal(summary.entries[0].policyDisabled, true);
  assert.equal(summary.entries[0].available, false);
  assert.equal(summary.hasPolicyDisabled, true);
  assert.equal(summary.hasUnavailable, true);
  assert.match(summary.label, /OpenAI \/ gpt-default（策略禁用） ×1/);
});


test("preflight normalization preserves every provider-model result", () => {
  const result = normalizeProviderPreflight({
    preflight: {
      ready: false,
      verification_scope: "local_configuration_only",
      external_call_count: 0,
      provider_checks: [
        {
          provider: "deepseek",
          model: "model-a",
          ready: true,
          member_count: 2,
          member_names: ["研究员", "主持人"],
          latency_ms: 120,
          cached: false,
        },
        {
          provider: "deepseek",
          model: "model-b",
          ready: false,
          member_count: 1,
          member_names: ["反方"],
          latency_ms: 240,
          message: "无权访问该模型",
          cached: true,
        },
      ],
    },
  });

  assert.equal(result.confirmed, true);
  assert.equal(result.ready, false);
  assert.equal(result.verificationScope, "local_configuration_only");
  assert.equal(result.externalCallCount, 0);
  assert.equal(result.checks.length, 2);
  assert.notEqual(result.checks[0].key, result.checks[1].key);
  assert.deepEqual(
    result.checks.map((check) => [check.id, check.model, check.ready, check.memberCount]),
    [["deepseek", "model-a", true, 2], ["deepseek", "model-b", false, 1]],
  );
  assert.equal(result.checks[1].error, "无权访问该模型");
  assert.equal(result.checks[1].cached, true);
});


test("preflight cannot report a policy-disabled provider as ready", () => {
  const result = normalizeProviderPreflight({
    preflight: {
      ready: true,
      provider_checks: [
        {
          provider: "openai",
          model: "gpt-default",
          ready: true,
          policy_disabled: true,
        },
        {
          provider: "legacy-provider",
          model: "legacy-model",
          ready: true,
          error_code: "PROVIDER_POLICY_DISABLED",
        },
      ],
    },
  });

  assert.equal(result.confirmed, true);
  assert.equal(result.ready, false);
  assert.deepEqual(
    result.checks.map((check) => [check.id, check.ready, check.policyDisabled]),
    [["openai", false, true], ["legacy-provider", false, true]],
  );
});


test("preflight ready requires local-only scope, zero external calls, and valid counts", () => {
  const remote = normalizeProviderPreflight({
    preflight: {
      ready: true,
      verification_scope: "provider_connectivity",
      external_call_count: 1,
      checks: [{
        provider: "deepseek",
        model: "model-a",
        ready: true,
        member_count: -1,
        latency_ms: Number.POSITIVE_INFINITY,
      }],
    },
  });

  assert.equal(remote.ready, false);
  assert.equal(remote.scopeConfirmed, false);
  assert.equal(remote.noExternalCalls, false);
  assert.ok(remote.issues.some((issue) => issue.code === "PREFLIGHT_EXTERNAL_CALLS_DETECTED"));
  assert.ok(remote.issues.some((issue) => issue.code === "PREFLIGHT_MEMBER_COUNT_INVALID"));
  assert.ok(remote.issues.some((issue) => issue.code === "PREFLIGHT_LATENCY_INVALID"));

  for (const invalidCount of [null, "", false, true]) {
    const invalid = normalizeProviderPreflight({
      preflight: {
        ready: true,
        verification_scope: "local_configuration_only",
        external_call_count: invalidCount,
        checks: [{
          provider: "deepseek",
          model: "model-a",
          ready: true,
          member_count: 1,
          latency_ms: 0,
        }],
      },
    });
    assert.equal(invalid.ready, false);
    assert.equal(invalid.noExternalCalls, false);
    assert.ok(
      invalid.issues.some((issue) => issue.code === "PREFLIGHT_EXTERNAL_CALL_COUNT_INVALID"),
    );
  }
});


test("duplicate preflight routes fail integrity but retain unique rendering keys", () => {
  const result = normalizeProviderPreflight({
    preflight: {
      ready: true,
      verification_scope: "local_configuration_only",
      external_call_count: 0,
      checks: [
        { provider: "deepseek", model: "model-a", ready: true },
        { provider: "deepseek", model: "model-a", ready: true },
      ],
    },
  });
  assert.equal(result.ready, false);
  assert.notEqual(result.checks[0].key, result.checks[1].key);
  assert.equal(result.checks[0].semanticKey, result.checks[1].semanticKey);
  assert.ok(result.issues.some((issue) => issue.code === "PREFLIGHT_ROUTE_DUPLICATE"));
});


test("routing presentation preserves the two-target allowlist and separates configured from checked", () => {
  const view = buildProviderRoutingPresentation({
    members: [
      { id: "m1", enabled: true, provider: "deepseek", model: "model-a" },
      { id: "m2", enabled: true },
    ],
    providers: [
      ...providers,
      { id: "third", name: "第三方", configured: true, model: "third-model" },
    ],
    preflightChecks: [{
      key: "[deepseek,model-a]",
      id: "deepseek",
      model: "model-a",
      ready: true,
      memberCount: 1,
      memberNames: ["研究员"],
      latencyMs: 0,
      policyDisabled: false,
    }],
  });

  assert.deepEqual(view.bulkTargets.map((target) => target.id), ["deepseek", "doubao"]);
  assert.equal(view.catalog.find((entry) => entry.id === "deepseek").status, "ready");
  assert.equal(view.catalog.find((entry) => entry.id === "third").status, "configured");
  assert.equal(view.warnings[0].key, "unassigned");
  assert.equal(view.stats.find((stat) => stat.key === "blocked").value, 1);
});


test("routing feedback and source contracts remain calibrated", () => {
  const feedback = providerRoutingResultFeedback({
    total: 4,
    succeeded: 3,
    failed: [{ name: "反方" }],
  }, "DeepSeek");
  assert.equal(feedback.tone, "error");
  assert.match(feedback.text, /3\/4/);
  assert.match(feedback.text, /反方/);

  const panelSource = readFileSync(
    new URL("../src/components/ProviderRoutingPanel.jsx", import.meta.url),
    "utf8",
  );
  const styles = readFileSync(new URL("../src/styles/provider-routing.css", import.meta.url), "utf8");
  assert.match(panelSource, /local_configuration_only/);
  assert.match(panelSource, /operationEpochRef/);
  assert.match(panelSource, /aria-busy/);
  assert.match(styles, /@media \(max-width: 460px\)/);
  assert.match(styles, /\.provider-preflight-contract/);
});
