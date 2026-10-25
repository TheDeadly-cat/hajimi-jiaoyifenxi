import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { createServer } from "vite";
import { readyPlan } from "./helpers/f03RoundLaunchFixtures.js";
import { normalizeRoundLaunchPlan } from "../src/roundLaunchPlan.js";

const providerIds = ["deepseek", "doubao", "glm", "openai", "qwen"];
const roomId = "room_policy_plain";
const objective = "合成普通房间：只读取启动单，不调用模型。";
const vite = await createServer({
  root: fileURLToPath(new URL("../", import.meta.url)),
  appType: "custom", logLevel: "silent",
  server: { hmr: false, middlewareMode: true },
});
const dom = new JSDOM("<!doctype html><body></body>", {
  pretendToBeVisual: true, url: `http://round-launch-policy.test/?room=${roomId}`,
});
for (const key of ["window", "document", "navigator", "HTMLElement", "Node", "Event", "KeyboardEvent", "MouseEvent"]) {
  Object.defineProperty(globalThis, key, {
    configurable: true, value: key === "window" ? dom.window : dom.window[key],
  });
}
globalThis.getComputedStyle = dom.window.getComputedStyle.bind(dom.window);
globalThis.requestAnimationFrame = dom.window.requestAnimationFrame.bind(dom.window);
globalThis.cancelAnimationFrame = dom.window.cancelAnimationFrame.bind(dom.window);
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
dom.window.HTMLElement.prototype.getClientRects = function () {
  return this.isConnected && !this.hidden ? [{ width: 1, height: 1, top: 0, left: 0, right: 1, bottom: 1 }] : [];
};
dom.window.HTMLElement.prototype.scrollIntoView = function () {};
const { createRoot } = await import("react-dom/client");
const { default: App } = await vite.ssrLoadModule("/src/App.jsx");
const originalFetch = globalThis.fetch;
let root, host, requests, unexpectedRequests;

function blockedPlan(allDisabled) {
  const plan = readyPlan();
  plan.objective = objective;
  plan.room = {
    ...plan.room, id: roomId, domain: "general", template_id: "plain",
    capability_pack_ids: [],
  };
  plan.members = plan.members.map((member, index) => ({
    ...member, enabled: true, stage: index === 0 ? "facilitate" : "flexible",
    workflow_stage: index === 0 ? "facilitate" : "flexible",
  }));
  plan.moderator = { ...plan.members[0], selection_source: "configured" };
  plan.skip_provider_ids = allDisabled ? [...providerIds] : ["openai"];
  plan.preflight_routes = plan.preflight_routes.map((route) => ({
    ...route, configured: false, policy_disabled: allDisabled,
    skipped: allDisabled, callable: false, projected_preflight_calls: 0,
  }));
  plan.provider_call_projection = providerIds.map((provider) => ({
    provider, known: true, configured: false,
    policy_disabled: allDisabled || provider === "openai",
    skipped: allDisabled || provider === "openai",
    projected_preflight_calls: 0, minimum_speaker_calls: 0,
    minimum_director_calls: 0, recommended_director_calls: 0,
    optional_artifact_calls: 0, contingency_calls: 0,
    projected_provider_calls: 0,
  }));
  for (const key of Object.keys(plan.calls)) {
    if (typeof plan.calls[key] === "number") plan.calls[key] = 0;
  }
  plan.calls.unique_preflight_route_count = plan.preflight_routes.length;
  plan.calls.discussion_call_range = { minimum: 0, maximum: 0 };
  plan.calls.total_call_range = { minimum: 0, maximum: 0 };
  plan.ready_for_authorization = false;
  plan.blockers = plan.members.map((member) => ({
    code: allDisabled ? "PROVIDER_SKIPPED" : "PROVIDER_NOT_CONFIGURED",
    provider: member.provider, member_id: member.id, model: member.model,
  }));
  const normalized = normalizeRoundLaunchPlan(plan);
  assert.equal(normalized.valid, true, JSON.stringify(normalized.normalization_errors));
  assert.equal(normalized.calls.projected_provider_calls_total, 0);
  assert.deepEqual(normalized.skip_provider_ids, plan.skip_provider_ids);
  return plan;
}

function installSyntheticApi(plan, allDisabled) {
  const providers = providerIds.map((id) => ({
    id, name: id, configured: false, enabled: false,
    policy_disabled: allDisabled || id === "openai", model: `${id}-synthetic`,
  }));
  const room = {
    ...plan.room, title: "合成普通研究房间", objective, context: "",
    capabilities: [], workflow_policy: {
      stage_order: ["facilitate", "flexible"],
      minimum_stage_coverage: { facilitate: 1, flexible: 1 }, required_coverage: [],
      minimum_successful_members: 2,
    },
  };
  const active = {
    room, members: plan.members, messages: [], materials: [], artifacts: [],
    rounds: [], providers,
  };
  const respond = (data) => ({ ok: true, status: 200, json: async () => data });
  globalThis.fetch = async (url, options = {}) => {
    const method = options.method || "GET";
    const path = String(url).split("?")[0];
    const request = { method, path, body: options.body ? JSON.parse(options.body) : null };
    requests.push(request);
    if (method === "GET" && path === "/api/bootstrap") {
      return respond({ ok: true, rooms: [room], active, providers, templates: [], member_templates: [] });
    }
    if (method === "GET" && path === `/api/rooms/${roomId}/plugin-registry`) {
      return respond({ ok: true, registry: null });
    }
    if (method === "POST" && path === `/api/rooms/${roomId}/round-launch-plan`) {
      assert.equal(request.body.objective, objective);
      return respond({ ok: true, plan });
    }
    unexpectedRequests.push(`${method} ${path}`);
    throw new Error("Synthetic plan-read test forbids every other request, including model, market, source and round streams");
  };
}

const button = (label) => [...host.querySelectorAll("button")].find((entry) => entry.textContent.trim() === label);
async function settle(predicate) {
  for (let index = 0; index < 200 && !predicate(); index++) {
    await act(async () => new Promise((resolve) => setTimeout(resolve, 10)));
  }
  assert.ok(predicate(), "expected App state did not appear before the bounded wait ended");
}
async function readLaunchPlan(plan, allDisabled) {
  installSyntheticApi(plan, allDisabled);
  await act(async () => root.render(React.createElement(App)));
  await settle(() => button("开始一轮"));
  const trigger = button("开始一轮");
  assert.equal(trigger.disabled, false, "missing keys or policy status must still allow reading the plan");
  await act(async () => trigger.click());
  await settle(() => host.querySelector("form.round-launch-dialog")
    && !host.querySelector(".round-launch-loading")
    && (host.querySelector(".round-launch-recovery") || host.querySelector(".round-launch-alert.error")));
  assert.equal(requests.filter((request) => request.path.endsWith("/round-launch-plan")).length, 1);
  assert.deepEqual(unexpectedRequests, [], "reading a plan cannot launch a round or contact any external source");
  assert.equal(requests.filter((request) => request.method === "POST").length, 1);
}

test.beforeEach(() => {
  requests = []; unexpectedRequests = [];
  globalThis.fetch = async (url) => { unexpectedRequests.push(String(url)); throw new Error("No real network allowed"); };
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
test.afterEach(async () => {
  await act(async () => root.unmount());
  document.body.replaceChildren(); globalThis.fetch = originalFetch;
  assert.deepEqual(unexpectedRequests, []);
});
test.after(async () => { dom.window.close(); await vite.close(); });

test("real App shows deployment-disabled recovery for a valid all-skipped zero-call plan without claiming policy drift", async (context) => {
  const plan = blockedPlan(true);
  await readLaunchPlan(plan, true);
  const recovery = host.querySelector(".round-launch-recovery")?.textContent || "";
  context.diagnostic(JSON.stringify({ dialog: host.querySelector(".round-launch-dialog").textContent, recovery, requests, confirmDisabled: button("确认开始一轮")?.disabled ?? null, unexpectedRequests }));
  assert.doesNotMatch(host.querySelector(".round-launch-dialog").textContent, /策略已变化/);
  assert.match(recovery, /当前模型被停用，暂不能开始一轮/);
  assert.match(recovery, /不能在这里重新启用/);
  assert.ok(button("查看模型配置"));
  assert.equal(button("确认开始一轮").disabled, true);
  await act(async () => button("确认开始一轮").click());
  assert.equal(requests.filter((request) => request.method === "POST").length, 1);
  assert.ok(plan.provider_call_projection.every((entry) => entry.policy_disabled && entry.skipped && entry.projected_provider_calls === 0));
});

test("real App retains missing-key recovery when only OpenAI is skipped and never confirms the blocked plan", async (context) => {
  await readLaunchPlan(blockedPlan(false), false);
  const recovery = host.querySelector(".round-launch-recovery")?.textContent || "";
  context.diagnostic(JSON.stringify({ recovery, requests, confirmDisabled: button("确认开始一轮")?.disabled ?? null, unexpectedRequests }));
  assert.doesNotMatch(host.querySelector(".round-launch-dialog").textContent, /策略已变化/);
  assert.match(recovery, /模型尚未配置，暂不能开始一轮/);
  assert.ok(button("查看模型配置"));
  assert.equal(button("确认开始一轮").disabled, true);
  await act(async () => button("确认开始一轮").click());
  assert.equal(requests.filter((request) => request.method === "POST").length, 1);
});
