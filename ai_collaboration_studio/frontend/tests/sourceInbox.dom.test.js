import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import test from "node:test";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { createServer } from "vite";

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
    url: "http://source-inbox.test/",
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
const { SourceInboxPanel } = await vite.ssrLoadModule("/src/components/SourceInboxPanel.jsx");

function response(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  };
}

function sourceRecord({
  acknowledged = false,
  attachments = [],
  drafts = [],
  state = "AWAITING_USER",
  stateVersion = 1,
} = {}) {
  return {
    version: "source_inbox_item_record_v1",
    id: "source_item_one",
    source_channel: "chatgpt_scheduled_task",
    source_key: "github_ci_watch",
    external_run_id: "run_one",
    received_at: 1_777_777_777_000,
    server_fingerprint: "a".repeat(64),
    item_sha256: "b".repeat(64),
    state,
    state_version: stateVersion,
    acknowledged,
    acknowledged_by: acknowledged ? "local_user" : "",
    acknowledged_at: acknowledged ? 1_777_777_777_100 : 0,
    expires_at: 1_888_888_888_000,
    created_at: 1_777_777_777_000,
    updated_at: 1_777_777_777_100,
    external_claims_verification: "external_unverified",
    item: {
      version: "project_source_item_v1",
      item_type: "ci_run_failure",
      severity: "high",
      occurred_at: "2026-08-28T12:55:00Z",
      published_at: "2026-08-28T12:56:00Z",
      headline: "GitHub / CI 运行结果摘要",
      summary: "外部系统声明隔离测试失败。",
      facts: [{ claim: "workflow conclusion is failure", source_indexes: [0] }],
      sources: [{
        url: "https://github.com/acme/project/actions/runs/100",
        publisher: "GitHub",
        source_type: "official_platform",
        published_at: "2026-08-28T12:56:00Z",
        content_sha256: "c".repeat(64),
      }],
      impact_hypotheses: [{
        statement: "发布窗口可能受影响。",
        affected_area: "release readiness",
        time_horizon: "next publication",
        confidence: 0.7,
        source_indexes: [0],
      }],
      unknowns: ["失败断言尚未导入。"],
      confidence: 0.9,
      recommended_route: "open_round_draft",
    },
    attachments,
    round_drafts: drafts,
    events: [],
    safety: {
      acknowledgement_is_fact_confirmation: false,
      formal_round_created: false,
      provider_calls_performed: 0,
      market_calls_performed: 0,
      execution_capability: "none",
    },
  };
}

async function settle() {
  for (let index = 0; index < 6; index += 1) {
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

async function mountPanel(overrides = {}) {
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  mountedRoots.add(root);
  await act(async () => {
    root.render(h(SourceInboxPanel, {
      activeRoomId: "room_current",
      onClose() {},
      onRoomAttached() {},
      open: true,
      restoreFocusRef: { current: null },
      rooms: [{ id: "room_current", title: "方案共创会" }],
      ...overrides,
    }));
  });
  await settle();
  return host;
}

async function click(target) {
  assert.ok(target, "expected a clickable control");
  await act(async () => target.click());
  await settle();
}

async function change(target, value) {
  assert.ok(target, "expected an editable control");
  await act(async () => {
    const valueSetter = Object.getOwnPropertyDescriptor(
      Object.getPrototypeOf(target),
      "value",
    )?.set;
    assert.equal(typeof valueSetter, "function", "expected a native value setter");
    valueSetter.call(target, value);
    target.dispatchEvent(new Event(target.tagName === "SELECT" ? "change" : "input", {
      bubbles: true,
    }));
  });
  await settle();
}

function buttonWithText(host, text) {
  return [...host.querySelectorAll("button")]
    .find((button) => button.textContent.includes(text));
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

test("source actions require explicit read and room choices and stop at a zero-call draft", async () => {
  const requests = [];
  let current = sourceRecord();
  let attachedRefreshes = 0;
  globalThis.fetch = async (path, options = {}) => {
    requests.push({ path, options });
    if (path.startsWith("/api/monitoring/inbox?")) {
      return response({
        ok: true,
        source_inbox: {
          version: "source_inbox_list_v1",
          items: [current],
          counts: { [current.state]: 1 },
          query: "",
          state: "",
        },
      });
    }
    if (path.endsWith("/acknowledge")) {
      current = sourceRecord({ acknowledged: true, stateVersion: 2 });
      return response({ ok: true, source_item: current });
    }
    if (path.endsWith("/attach")) {
      current = sourceRecord({
        acknowledged: true,
        state: "ATTACHED",
        stateVersion: 3,
        attachments: [{
          version: "source_inbox_attachment_v1",
          id: "attachment_one",
          room_id: "room_current",
          material_id: "material_one",
          material_version: 1,
          item_sha256: "b".repeat(64),
          attachment_sha256: "d".repeat(64),
          attached_at: 1_777_777_777_200,
        }],
      });
      return response({ ok: true, item: current, attachment: current.attachments[0] }, 201);
    }
    if (path.endsWith("/round-draft")) {
      current = sourceRecord({
        acknowledged: true,
        state: "ROUND_DRAFTED",
        stateVersion: 4,
        attachments: [{
          version: "source_inbox_attachment_v1",
          id: "attachment_one",
          room_id: "room_current",
          material_id: "material_one",
          material_version: 1,
          item_sha256: "b".repeat(64),
          attachment_sha256: "d".repeat(64),
        }],
        drafts: [{
          version: "source_inbox_round_draft_v1",
          id: "draft_one",
          room_id: "room_current",
          draft_sha256: "e".repeat(64),
          formal_round_created: false,
          provider_calls_performed: 0,
          market_calls_performed: 0,
          execution_capability: "none",
          user_confirmation_required_to_launch: true,
        }],
      });
      return response({ ok: true, item: current, round_draft: current.round_drafts[0] }, 201);
    }
    return response({ ok: true, source_item: current });
  };

  const host = await mountPanel({
    async onRoomAttached() { attachedRefreshes += 1; },
  });

  assert.match(host.textContent, /external_unverified/);
  assert.match(host.textContent, /已阅，不代表事实确认/);
  assert.match(host.textContent, /草稿不启动 Provider/);
  assert.equal(requests.some((request) => request.options.method === "POST"), false);
  const roomSelect = host.querySelector(".source-inbox-room-actions select");
  assert.equal(roomSelect.value, "", "the active room must not be preselected");
  assert.equal(buttonWithText(host, "附加到房间").disabled, true);

  await click(host.querySelector('.source-inbox-acknowledgement input[type="checkbox"]'));
  await click(buttonWithText(host, "记录已阅"));
  assert.deepEqual(JSON.parse(requests.at(-1).options.body), {
    expected_state_version: 1,
    acknowledgement: true,
  });

  await change(roomSelect, "room_current");
  await click(buttonWithText(host, "附加到房间"));
  assert.equal(attachedRefreshes, 1);
  assert.deepEqual(JSON.parse(requests.at(-1).options.body), {
    room_id: "room_current",
    expected_state_version: 2,
  });

  await change(host.querySelector(".source-inbox-draft-actions textarea"), "仅讨论失败断言");
  await click(buttonWithText(host, "仅生成 round draft"));
  assert.deepEqual(JSON.parse(requests.at(-1).options.body), {
    room_id: "room_current",
    expected_state_version: 3,
    objective: "仅讨论失败断言",
  });
  assert.match(host.textContent, /Provider、正式 round 与市场调用均未启动/);

  const postPaths = requests
    .filter((request) => request.options.method === "POST")
    .map((request) => request.path);
  assert.deepEqual(postPaths, [
    "/api/monitoring/events/source_item_one/acknowledge",
    "/api/monitoring/events/source_item_one/attach",
    "/api/monitoring/events/source_item_one/round-draft",
  ]);
  assert.equal(requests.some((request) => /providers|market|rounds\/stream/.test(request.path)), false);
});

test("a CAS conflict rereads the exact item and requires a fresh acknowledgement", async () => {
  let current = sourceRecord();
  let detailReads = 0;
  globalThis.fetch = async (path, options = {}) => {
    if (path.startsWith("/api/monitoring/inbox?")) {
      return response({
        ok: true,
        source_inbox: { items: [current], counts: { AWAITING_USER: 1 } },
      });
    }
    if (path.endsWith("/acknowledge") && options.method === "POST") {
      current = sourceRecord({ stateVersion: 2 });
      return response({
        ok: false,
        error: "来源条目状态已变化，请刷新后重试。",
        code: "SOURCE_INBOX_STATE_CONFLICT",
      }, 409);
    }
    detailReads += 1;
    return response({ ok: true, source_item: current });
  };

  const host = await mountPanel();
  await click(host.querySelector('.source-inbox-acknowledgement input[type="checkbox"]'));
  await click(buttonWithText(host, "记录已阅"));

  assert.equal(detailReads, 2, "the 409 handler must reread the exact item once");
  assert.match(host.textContent, /已重新读取该条目/);
  assert.equal(host.querySelector('.source-inbox-acknowledgement input[type="checkbox"]').checked, false);
  assert.equal(buttonWithText(host, "记录已阅").disabled, true);
});

test("fenced ChatGPT source packets reach only the inbox import endpoint", async () => {
  const requests = [];
  const imported = sourceRecord();
  let importedVisible = false;
  globalThis.fetch = async (path, options = {}) => {
    requests.push({ path, options });
    if (path === "/api/monitoring/imports/chatgpt") {
      importedVisible = true;
      return response({
        ok: true,
        source_import: {
          idempotent_replay: false,
          items: [imported],
        },
      }, 201);
    }
    if (path.startsWith("/api/monitoring/inbox?")) {
      return response({
        ok: true,
        source_inbox: {
          items: importedVisible ? [imported] : [],
          counts: importedVisible ? { AWAITING_USER: 1 } : {},
        },
      });
    }
    return response({ ok: true, source_item: imported });
  };

  const host = await mountPanel();
  await click(buttonWithText(host, "导入 JSON"));
  const fenced = "```json\n{\"version\":\"source_import_packet_v1\"}\n```";
  await change(host.querySelector('.source-inbox-import textarea'), fenced);
  await click(buttonWithText(host, "仅导入到收件箱"));

  const importRequest = requests.find((request) => request.path === "/api/monitoring/imports/chatgpt");
  assert.ok(importRequest);
  assert.deepEqual(JSON.parse(importRequest.options.body), { content: fenced });
  assert.equal(requests.filter((request) => request.options.method === "POST").length, 1);
  assert.equal(requests.some((request) => /providers|market|rounds\/stream/.test(request.path)), false);
  assert.match(host.textContent, /GitHub \/ CI 运行结果摘要/);
});
