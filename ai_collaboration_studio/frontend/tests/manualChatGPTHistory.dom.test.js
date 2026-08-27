import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import test from "node:test";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { createServer } from "vite";

import {
  manualChatGPTReviewClientRequestId,
  normalizeManualChatGPT,
} from "../src/manualChatGPT.js";

const h = React.createElement;
const frontendRoot = fileURLToPath(new URL("../", import.meta.url));
const vite = await createServer({
  appType: "custom",
  logLevel: "silent",
  root: frontendRoot,
  server: { hmr: false, middlewareMode: true },
});
const originalFetch = globalThis.fetch;
const mountedRoots = new Set();
let frameSequence = 0;
let pendingFrames = new Map();

function installDom() {
  const dom = new JSDOM("<!doctype html><html><body></body></html>", {
    pretendToBeVisual: true,
    url: "http://manual-chatgpt-history.test/",
  });
  const { window } = dom;
  Object.defineProperties(globalThis, {
    window: { configurable: true, value: window },
    document: { configurable: true, value: window.document },
    navigator: { configurable: true, value: window.navigator },
    HTMLElement: { configurable: true, value: window.HTMLElement },
    Node: { configurable: true, value: window.Node },
    Event: { configurable: true, value: window.Event },
    MouseEvent: { configurable: true, value: window.MouseEvent },
    getComputedStyle: {
      configurable: true,
      value: window.getComputedStyle.bind(window),
    },
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  window.HTMLElement.prototype.getClientRects = function getClientRects() {
    if (!this.isConnected || this.hidden) return [];
    return [{ bottom: 1, height: 1, left: 0, right: 1, top: 0, width: 1, x: 0, y: 0 }];
  };
  globalThis.requestAnimationFrame = (callback) => {
    const frameId = ++frameSequence;
    pendingFrames.set(frameId, callback);
    return frameId;
  };
  globalThis.cancelAnimationFrame = (frameId) => pendingFrames.delete(frameId);
  return dom;
}

const dom = installDom();
const { createRoot } = await import("react-dom/client");
const { ChatGPTCollaborationDialog } = await vite.ssrLoadModule(
  "/src/components/ChatGPTCollaborationDialog.jsx",
);

function response(payload) {
  return {
    ok: true,
    status: 200,
    json: async () => payload,
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function manualSession({
  eligible = false,
  id,
  objective,
  recoveryCount = 0,
  roomId,
}) {
  return {
    id,
    room_id: roomId,
    round_id: `round_${id}`,
    mode: "standard",
    state: "API_REVIEW",
    objective,
    bundle_sha256: "a".repeat(64),
    context_sha256: "b".repeat(64),
    result_sha256: "c".repeat(64),
    task_prompt: "frozen prompt",
    bundle: {
      context: { roles: [], evidence_index: [], candidate_matrix: [] },
      budget: { chatgpt_panel_calls: 2, independent_api_reviews: 3 },
      planning: {},
    },
    integrity: { ok: true },
    validation_issues: [],
    api_review: {
      available: true,
      status: eligible ? "RUNNING" : "NOT_STARTED",
      expected_calls: 3,
      completed_calls: 0,
      provider: "openai",
      requested_model: "gpt-test",
      reviews: [],
    },
    review_recovery: {
      available: true,
      eligible,
      reason_code: eligible ? "ORPHANED_ZERO_CALL_REVIEW" : "NO_RUNNING_REVIEW",
      acknowledgement: "REAUTHORIZE_ZERO_CALL_ORPHANED_REVIEW",
      recovery_count: recoveryCount,
    },
  };
}

async function settle() {
  for (let index = 0; index < 4; index += 1) {
    await act(async () => {
      await Promise.resolve();
    });
  }
  if (pendingFrames.size) {
    await act(async () => {
      const frames = [...pendingFrames.entries()];
      pendingFrames = new Map();
      for (const [frameId, callback] of frames) callback(frameId);
    });
  }
}

async function mountDialog(props) {
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  mountedRoots.add(root);
  const render = async (nextProps) => {
    await act(async () => {
      root.render(h(ChatGPTCollaborationDialog, {
        initialObjective: "",
        onClose() {},
        open: true,
        restoreFocusRef: { current: null },
        ...nextProps,
      }));
    });
  };
  await render(props);
  return { host, render };
}

async function click(target) {
  assert.ok(target, "expected a clickable control");
  await act(async () => target.click());
  await settle();
}

function buttonWithText(host, value) {
  return [...host.querySelectorAll("button")]
    .find((button) => button.textContent.includes(value));
}

test.beforeEach(() => {
  document.body.replaceChildren();
  pendingFrames.clear();
});

test.afterEach(async () => {
  globalThis.fetch = originalFetch;
  for (const root of [...mountedRoots]) {
    await act(async () => root.unmount());
    mountedRoots.delete(root);
  }
  pendingFrames.clear();
});

test.after(async () => {
  await vite.close();
  dom.window.close();
});

test("history list replaces latest-only loading and switches tasks without automatic calls", async () => {
  const requests = [];
  const current = manualSession({
    id: "mcg_current",
    objective: "Current task",
    roomId: "room_one",
  });
  const recoverable = manualSession({
    eligible: true,
    id: "mcg_recoverable",
    objective: "Recoverable task",
    roomId: "room_one",
  });
  globalThis.fetch = async (path, options = {}) => {
    requests.push({ path, options });
    return response({ ok: true, manual_chatgpt_sessions: [current, recoverable] });
  };

  const view = await mountDialog({ roomId: "room_one" });
  await settle();

  assert.deepEqual(requests.map((item) => item.path), [
    "/api/rooms/room_one/chatgpt-collaborations?limit=30",
  ]);
  assert.equal(view.host.querySelectorAll(".manual-chatgpt-history-list button").length, 2);
  assert.equal(view.host.querySelector(".manual-chatgpt-objective p")?.textContent, "Current task");
  assert.equal(view.host.querySelector(".manual-chatgpt-recovery-note"), null);

  await click(buttonWithText(view.host, "Recoverable task"));

  assert.equal(view.host.querySelector(".manual-chatgpt-objective p")?.textContent, "Recoverable task");
  assert.match(view.host.querySelector(".manual-chatgpt-recovery-note")?.textContent || "", /零调用的孤儿审查/);
  assert.match(buttonWithText(view.host, "重新授权")?.textContent || "", /恢复零调用审查/);
  assert.equal(requests.length, 1, "opening and local task switching must not start a provider review");
  assert.equal(requests.some((item) => item.path.endsWith("/latest")), false);
  assert.equal(requests.some((item) => item.options.method === "POST"), false);
});

test("a late list response from the previous room cannot replace the active room", async () => {
  const stale = deferred();
  const roomA = manualSession({ id: "mcg_a", objective: "Stale room A", roomId: "room_a" });
  const roomB = manualSession({ id: "mcg_b", objective: "Active room B", roomId: "room_b" });
  globalThis.fetch = async (path) => {
    if (path.includes("/room_a/")) return stale.promise;
    return response({ ok: true, manual_chatgpt_sessions: [roomB] });
  };

  const view = await mountDialog({ roomId: "room_a" });
  await view.render({ roomId: "room_b" });
  await settle();
  assert.equal(view.host.querySelector(".manual-chatgpt-objective p")?.textContent, "Active room B");

  stale.resolve(response({ ok: true, manual_chatgpt_sessions: [roomA] }));
  await settle();

  assert.equal(view.host.querySelector(".manual-chatgpt-objective p")?.textContent, "Active room B");
  assert.doesNotMatch(view.host.textContent, /Stale room A/);
});

test("recovery is user-triggered and the next review uses a new client request id", async () => {
  const requests = [];
  const orphaned = manualSession({
    eligible: true,
    id: "mcg_orphaned",
    objective: "Recover review",
    recoveryCount: 0,
    roomId: "room_one",
  });
  const recovered = manualSession({
    id: "mcg_orphaned",
    objective: "Recover review",
    recoveryCount: 1,
    roomId: "room_one",
  });
  globalThis.fetch = async (path, options = {}) => {
    requests.push({ path, options });
    if (options.method !== "POST") {
      return response({ ok: true, manual_chatgpt_sessions: [orphaned] });
    }
    return response({ ok: true, manual_chatgpt: recovered });
  };

  const view = await mountDialog({ roomId: "room_one" });
  await settle();
  assert.equal(requests.length, 1, "loading recovery state must remain read-only");

  await click(buttonWithText(view.host, "重新授权并恢复"));
  assert.equal(requests.length, 2);
  assert.match(requests[1].path, /api-reviews\/recover$/);
  assert.equal(view.host.querySelector(".manual-chatgpt-recovery-note"), null);

  await click(buttonWithText(view.host, "运行 3 次独立 API 审查"));
  assert.equal(requests.length, 3);
  assert.match(requests[2].path, /api-reviews$/);
  const payload = JSON.parse(requests[2].options.body);
  const originalRequestId = manualChatGPTReviewClientRequestId(
    normalizeManualChatGPT(orphaned),
  );
  const recoveredRequestId = manualChatGPTReviewClientRequestId(
    normalizeManualChatGPT(recovered),
  );
  assert.equal(payload.client_request_id, recoveredRequestId);
  assert.notEqual(payload.client_request_id, originalRequestId);
  assert.match(payload.client_request_id, /^manual-review-r1-/);
});
