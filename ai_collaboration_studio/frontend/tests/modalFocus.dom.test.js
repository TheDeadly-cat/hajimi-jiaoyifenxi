import assert from "node:assert/strict";
import test from "node:test";
import React, { act, useRef } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { useModalFocus } from "../src/useModalFocus.js";

const h = React.createElement;
const mountedRoots = new Set();
let frameSequence = 0;
let pendingFrames = new Map();

function installDom() {
  const dom = new JSDOM("<!doctype html><html><body></body></html>", {
    pretendToBeVisual: true,
    url: "http://modal-focus.test/",
  });
  const { window } = dom;
  Object.defineProperties(globalThis, {
    window: { configurable: true, value: window },
    document: { configurable: true, value: window.document },
    navigator: { configurable: true, value: window.navigator },
    HTMLElement: { configurable: true, value: window.HTMLElement },
    Node: { configurable: true, value: window.Node },
    Event: { configurable: true, value: window.Event },
    KeyboardEvent: { configurable: true, value: window.KeyboardEvent },
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

async function flushFrames() {
  await act(async () => {
    const frames = [...pendingFrames.entries()];
    pendingFrames = new Map();
    for (const [frameId, callback] of frames) callback(frameId);
  });
}

async function mount(element) {
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  mountedRoots.add(root);
  await act(async () => root.render(element));
  return {
    host,
    async render(nextElement) {
      await act(async () => root.render(nextElement));
    },
    async unmount() {
      if (!mountedRoots.has(root)) return;
      await act(async () => root.unmount());
      mountedRoots.delete(root);
      host.remove();
    },
  };
}

function keydown(key, { shiftKey = false } = {}) {
  const event = new KeyboardEvent("keydown", {
    bubbles: true,
    cancelable: true,
    key,
    shiftKey,
  });
  document.activeElement.dispatchEvent(event);
  return event;
}

function FocusModal({
  busy = false,
  children = null,
  label,
  onClose,
  open = true,
  restoreFocusRef,
}) {
  const containerRef = useRef(null);
  const closeButtonRef = useRef(null);
  useModalFocus({
    open,
    containerRef,
    initialFocusRef: closeButtonRef,
    restoreFallbackRef: restoreFocusRef,
    onClose: busy ? null : onClose,
  });
  if (!open) return null;
  return h(
    "section",
    { "aria-label": label, ref: containerRef, role: "dialog", tabIndex: -1 },
    h("button", { "data-focus": `${label}-first`, ref: closeButtonRef, type: "button" }, "close"),
    children,
    h("button", { "data-focus": `${label}-last`, type: "button" }, "last"),
  );
}

test.beforeEach(() => {
  document.body.replaceChildren();
  pendingFrames.clear();
});

test.afterEach(async () => {
  for (const root of [...mountedRoots]) {
    await act(async () => root.unmount());
    mountedRoots.delete(root);
  }
  pendingFrames.clear();
});

test.after(() => dom.window.close());

test("mounted modal takes initial focus and cycles Tab in both directions", async () => {
  const trigger = document.createElement("button");
  trigger.textContent = "open";
  document.body.append(trigger);
  trigger.focus();
  const view = await mount(h(FocusModal, { label: "single", onClose() {} }));

  await flushFrames();
  const first = view.host.querySelector('[data-focus="single-first"]');
  const last = view.host.querySelector('[data-focus="single-last"]');
  assert.equal(document.activeElement, first);

  keydown("Tab", { shiftKey: true });
  assert.equal(document.activeElement, last);
  keydown("Tab");
  assert.equal(document.activeElement, first);
  keydown("Tab");
  assert.equal(document.activeElement, last);

  await view.unmount();
});

test("Escape closes once while busy mode consumes Escape without closing", async () => {
  let closeCount = 0;
  const renderModal = (busy) => h(FocusModal, {
    busy,
    label: "busy",
    onClose() { closeCount += 1; },
  });
  const view = await mount(renderModal(true));
  await flushFrames();

  const blockedEscape = keydown("Escape");
  assert.equal(blockedEscape.defaultPrevented, true);
  assert.equal(closeCount, 0);

  await view.render(renderModal(false));
  const acceptedEscape = keydown("Escape");
  assert.equal(acceptedEscape.defaultPrevented, true);
  assert.equal(closeCount, 1);

  await view.unmount();
});

test("closing restores the exact captured trigger before the fallback", async () => {
  const exactTrigger = document.createElement("button");
  exactTrigger.textContent = "exact";
  const fallbackTrigger = document.createElement("button");
  fallbackTrigger.textContent = "fallback";
  document.body.append(exactTrigger, fallbackTrigger);
  exactTrigger.focus();
  const restoreFocusRef = { current: fallbackTrigger };
  const props = { label: "restore", onClose() {}, restoreFocusRef };
  const view = await mount(h(FocusModal, props));
  await flushFrames();
  assert.notEqual(document.activeElement, exactTrigger);

  await view.render(h(FocusModal, { ...props, open: false }));
  await flushFrames();
  assert.equal(document.activeElement, exactTrigger);

  await view.unmount();
});

test("only the top nested modal handles Tab", async () => {
  let outerCloseCount = 0;
  let innerCloseCount = 0;
  const view = await mount(h(
    FocusModal,
    { label: "outer", onClose() { outerCloseCount += 1; } },
    h(FocusModal, { label: "inner", onClose() { innerCloseCount += 1; } }),
  ));
  await flushFrames();

  const innerFirst = view.host.querySelector('[data-focus="inner-first"]');
  const innerLast = view.host.querySelector('[data-focus="inner-last"]');
  const focusTrail = [];
  view.host.addEventListener("focusin", (event) => {
    focusTrail.push(event.target.getAttribute("data-focus"));
  });
  innerLast.focus();
  focusTrail.length = 0;
  keydown("Tab");
  assert.deepEqual(focusTrail, ["inner-first"]);
  assert.equal(document.activeElement, innerFirst);

  await view.unmount();
});

test("only the top nested modal handles Escape", async () => {
  let outerCloseCount = 0;
  let innerCloseCount = 0;
  const view = await mount(h(
    FocusModal,
    { label: "outer", onClose() { outerCloseCount += 1; } },
    h(FocusModal, { label: "inner", onClose() { innerCloseCount += 1; } }),
  ));
  await flushFrames();

  keydown("Escape");
  assert.equal(innerCloseCount, 1);
  assert.equal(outerCloseCount, 0);

  await view.unmount();
});

test("a busy top modal consumes Escape without closing the modal below it", async () => {
  let outerCloseCount = 0;
  let innerCloseCount = 0;
  const view = await mount(h(
    FocusModal,
    { label: "outer", onClose() { outerCloseCount += 1; } },
    h(FocusModal, {
      busy: true,
      label: "inner",
      onClose() { innerCloseCount += 1; },
    }),
  ));
  await flushFrames();

  const escape = keydown("Escape");
  assert.equal(escape.defaultPrevented, true);
  assert.equal(innerCloseCount, 0);
  assert.equal(outerCloseCount, 0);

  await view.unmount();
});
