import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { createServer } from "vite";
import { f03Plans } from "./helpers/f03RoundLaunchFixtures.js";

const vite = await createServer({
  root: fileURLToPath(new URL("../", import.meta.url)),
  appType: "custom", logLevel: "silent",
  server: { hmr: false, middlewareMode: true },
});
const dom = new JSDOM("<!doctype html><body></body>", {
  pretendToBeVisual: true, url: "http://f03-layout.test/",
});
for (const key of ["window", "document", "navigator", "HTMLElement", "Node", "Event", "KeyboardEvent"]) {
  Object.defineProperty(globalThis, key, { configurable: true, value: key === "window" ? dom.window : dom.window[key] });
}
globalThis.getComputedStyle = dom.window.getComputedStyle.bind(dom.window);
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
let sequence = 0;
const frames = new Map();
globalThis.requestAnimationFrame = (fn) => { frames.set(++sequence, fn); return sequence; };
globalThis.cancelAnimationFrame = (id) => frames.delete(id);
dom.window.HTMLElement.prototype.getClientRects = function () {
  if (!this.isConnected || this.hidden || this.closest("details:not([open])") && this.tagName !== "SUMMARY") return [];
  return [{ width: 1, height: 1, top: 0, left: 0, right: 1, bottom: 1 }];
};
const { createRoot } = await import("react-dom/client");
const { RoundLaunchDialog } = await vite.ssrLoadModule("/src/components/RoundLaunchDialog.jsx");
let host;
let root;
let props;
let counts;
let restore;
const originalFetch = globalThis.fetch;

async function flushFrames() {
  await act(async () => {
    const pending = [...frames.values()]; frames.clear();
    pending.forEach((fn) => fn());
  });
}
async function render(extra = {}) {
  props = { ...props, ...extra };
  await act(async () => root.render(React.createElement(RoundLaunchDialog, props)));
  await flushFrames();
}
function submit() { return host.querySelector('button[type="submit"]'); }

test.beforeEach(async () => {
  counts = { confirm: 0, retry: 0, configure: 0, close: 0, fetch: 0, payload: null };
  globalThis.fetch = async () => { counts.fetch++; throw new Error("F03 test forbids network"); };
  restore = document.createElement("button"); restore.textContent = "开始一轮";
  document.body.append(restore); restore.focus();
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  props = {
    open: true, plan: f03Plans().ready, clientRoundRequestId: "f03-synthetic-round-request-001",
    restoreFocusRef: { current: restore },
    onConfirm: async (payload) => { counts.confirm++; counts.payload = payload; },
    onRetry: () => { counts.retry++; },
    onOpenProviderSettings: () => { counts.configure++; },
    onClose: () => { counts.close++; },
  };
});
test.afterEach(async () => {
  assert.equal(counts.fetch, 0, "rendering and disclosure must not contact a provider");
  await act(async () => root.unmount());
  await flushFrames(); frames.clear(); document.body.replaceChildren();
  globalThis.fetch = originalFetch;
});
test.after(async () => { dom.window.close(); await vite.close(); });

test("missing configuration comes first, preserves disabled submit and exposes explicit navigation only", async () => {
  await render({ plan: f03Plans().missing });
  const body = host.querySelector(".round-launch-body");
  assert.equal(body.firstElementChild.className, "round-launch-recovery");
  assert.match(body.firstElementChild.textContent, /模型尚未配置/);
  assert.match(body.firstElementChild.textContent, /后续 API 复核仍需可用配置和单独确认/);
  assert.equal(submit().disabled, true);
  assert.equal(host.querySelector(".round-launch-technical-details").open, false);
  assert.ok(!host.querySelector(".round-launch-snapshot").closest("details"));
  assert.match(host.querySelector(".round-launch-snapshot").textContent, /本轮调用总上限.*20 次.*无下单、实盘或钱包权限/s);
  assert.equal(counts.configure, 0);
  await act(async () => body.querySelector("button").click());
  assert.equal(counts.configure, 1);
  await act(async () => host.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  assert.equal(counts.confirm, 0, "synthetic submit cannot bypass blocked authorization");
});

test("disabled provider and deployment limit keep their distinct safe recovery paths", async () => {
  await render({ plan: f03Plans().disabled });
  assert.match(host.querySelector(".round-launch-recovery").textContent, /不能在这里重新启用/);
  assert.equal(submit().disabled, true);
  await render({ plan: f03Plans().deploymentLimit });
  assert.match(host.querySelector(".round-launch-recovery").textContent, /超过系统上限/);
  assert.equal(host.querySelector(".round-launch-recovery button"), null);
  assert.equal(submit().disabled, true);
});

test("low but valid budget retains warning and the existing exact authorized limit", async () => {
  await render({ initialMaxProviderCalls: 1 });
  assert.match(host.querySelector(".round-launch-alert.warning").textContent, /低于推荐.*可能提前停止/);
  assert.equal(host.querySelector(".round-launch-alert.warning").closest("details"), null);
  assert.equal(submit().disabled, false);
  await act(async () => host.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  assert.equal(counts.confirm, 1);
  assert.equal(counts.payload.max_provider_calls, 1);
  assert.deepEqual(counts.payload.skip_providers, ["openai"]);
  assert.equal(counts.payload.plan_hash, "a".repeat(64));
});

test("invalid budget cannot submit and gives the editable total as the recovery path", async () => {
  await render();
  const input = host.querySelector('input[type="number"]');
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set.call(input, "0");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  assert.equal(submit().disabled, true);
  assert.match(host.querySelector(".round-launch-recovery").textContent, /修正本轮调用总上限/);
});

test("loading failure retries only on a click and Escape still returns to the launch trigger", async () => {
  await render({ plan: null, loading: true });
  assert.equal(submit().disabled, true);
  await render({ loading: false, error: "合成读取失败" });
  assert.match(host.textContent, /合成读取失败/);
  const retry = [...host.querySelectorAll("button")].find((node) => node.textContent === "重新读取");
  assert.ok(retry); assert.equal(counts.retry, 0);
  await act(async () => retry.click()); assert.equal(counts.retry, 1);
  await act(async () => document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })));
  assert.equal(counts.close, 1);
  await render({ open: false });
  assert.equal(document.activeElement, restore);
  assert.equal(counts.confirm, 0);
});

test("unknown blockers stay visible and never become a configuration bypass", async () => {
  await render({ plan: f03Plans().unknown });
  assert.match(host.querySelector(".round-launch-recovery").textContent, /F03_UNRECOGNIZED_BLOCKER/);
  assert.equal(submit().disabled, true);
  assert.equal(host.querySelector(".round-launch-recovery button"), null);
});
