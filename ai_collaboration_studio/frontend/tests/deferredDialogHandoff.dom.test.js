import assert from "node:assert/strict";
import test from "node:test";
import React, { Suspense, act, lazy, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { DeferredSurfaceFallback } from "../src/DeferredSurfaceFallback.js";
import { useModalFocus } from "../src/useModalFocus.js";

const h = React.createElement;
const mountedRoots = new Set();
let frameSequence = 0;
let pendingFrames = new Map();

function installDom() {
  const dom = new JSDOM("<!doctype html><html><body></body></html>", {
    pretendToBeVisual: true,
    url: "http://deferred-dialog-handoff.test/",
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

async function flushFrames() {
  while (pendingFrames.size) {
    await act(async () => {
      const frames = [...pendingFrames.entries()];
      pendingFrames = new Map();
      for (const [frameId, callback] of frames) callback(frameId);
    });
  }
}

async function mount(element) {
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  mountedRoots.add(root);
  await act(async () => root.render(element));
  return {
    host,
    async unmount() {
      if (!mountedRoots.has(root)) return;
      await act(async () => root.unmount());
      mountedRoots.delete(root);
      host.remove();
    },
  };
}

async function click(target) {
  await act(async () => {
    target.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
  });
}

async function keydown(key) {
  let event;
  await act(async () => {
    event = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, key });
    document.activeElement.dispatchEvent(event);
  });
  return event;
}

function LoadedDialog({ onClose, onSuccess, open, restoreFocusRef }) {
  const dialogRef = useRef(null);
  const closeButtonRef = useRef(null);
  useModalFocus({
    open,
    containerRef: dialogRef,
    initialFocusRef: closeButtonRef,
    restoreFallbackRef: restoreFocusRef,
    onClose,
  });
  if (!open) return null;
  return h(
    "section",
    {
      "aria-label": "启动确认",
      "aria-modal": "true",
      "data-loaded-dialog": "true",
      ref: dialogRef,
      role: "dialog",
      tabIndex: -1,
    },
    h("button", { "data-loaded-close": "true", onClick: onClose, ref: closeButtonRef, type: "button" }, "关闭"),
    h("button", { "data-loaded-confirm": "true", onClick: onSuccess, type: "button" }, "确认启动"),
  );
}

function controlledLazyDialog() {
  let resolveModule;
  const modulePromise = new Promise((resolve) => {
    resolveModule = resolve;
  });
  return {
    Component: lazy(() => modulePromise),
    async resolve() {
      await act(async () => {
        resolveModule({ default: LoadedDialog });
        await modulePromise;
      });
    },
  };
}

function FocusShell({ children, onClose }) {
  const shellRef = useRef(null);
  useModalFocus({
    open: true,
    containerRef: shellRef,
    initialFocusRef: null,
    restoreFallbackRef: null,
    onClose,
  });
  return h("section", { "data-outer-dialog": "true", ref: shellRef, role: "dialog", tabIndex: -1 }, children);
}

function RoundLaunchHandoff({ LazyDialog, onOuterClose }) {
  const [open, setOpen] = useState(false);
  const [started, setStarted] = useState(false);
  const restoreFocusRef = useRef(null);
  const successFocusRef = useRef(null);
  const openDialog = (event) => {
    restoreFocusRef.current = event.currentTarget;
    setOpen(true);
  };
  const closeDialog = () => setOpen(false);
  const startSuccessfully = () => {
    restoreFocusRef.current = successFocusRef.current;
    setStarted(true);
    setOpen(false);
  };
  return h(
    FocusShell,
    { onClose: onOuterClose },
    h("button", {
      "data-launch-trigger": "true",
      disabled: open || started,
      onClick: openDialog,
      type: "button",
    }, "开始一轮"),
    h("div", { "data-success-focus": "true", ref: successFocusRef, tabIndex: -1 }, "讨论已启动"),
    open ? h(
      Suspense,
      {
        fallback: h(DeferredSurfaceFallback, {
          dialog: true,
          label: "启动确认",
          onClose: closeDialog,
          open,
          restoreFocusRef,
        }),
      },
      h(LazyDialog, {
        onClose: closeDialog,
        onSuccess: startSuccessfully,
        open,
        restoreFocusRef,
      }),
    ) : null,
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

test("first lazy fallback disables its trigger, owns modal focus, and Escape closes only the top surface", async () => {
  const controlled = controlledLazyDialog();
  let outerCloseCount = 0;
  const view = await mount(h(RoundLaunchHandoff, {
    LazyDialog: controlled.Component,
    onOuterClose() { outerCloseCount += 1; },
  }));
  await flushFrames();

  const trigger = view.host.querySelector('[data-launch-trigger="true"]');
  trigger.focus();
  await click(trigger);
  assert.equal(trigger.disabled, true);

  const fallback = view.host.querySelector('[role="dialog"][aria-label="启动确认加载中"]');
  assert.ok(fallback);
  assert.equal(fallback.getAttribute("aria-modal"), "true");
  await flushFrames();
  assert.equal(document.activeElement.textContent, "取消加载");

  const escape = await keydown("Escape");
  assert.equal(escape.defaultPrevented, true);
  assert.equal(outerCloseCount, 0);
  assert.equal(view.host.querySelector('[aria-label="启动确认加载中"]'), null);
  assert.equal(trigger.disabled, false);
  await flushFrames();
  assert.equal(document.activeElement, trigger);

  await view.unmount();
});

test("lazy dialog handoff keeps focus stable and ordinary close restores the exact trigger", async () => {
  const controlled = controlledLazyDialog();
  const view = await mount(h(RoundLaunchHandoff, {
    LazyDialog: controlled.Component,
    onOuterClose() {},
  }));
  await flushFrames();
  const trigger = view.host.querySelector('[data-launch-trigger="true"]');
  trigger.focus();
  await click(trigger);
  await flushFrames();
  assert.equal(document.activeElement.textContent, "取消加载");

  await controlled.resolve();
  await flushFrames();
  const loadedClose = view.host.querySelector('[data-loaded-close="true"]');
  assert.ok(loadedClose);
  assert.equal(document.activeElement, loadedClose);
  assert.equal(trigger.disabled, true);

  await click(loadedClose);
  assert.equal(trigger.disabled, false);
  await flushFrames();
  assert.equal(document.activeElement, trigger);

  await view.unmount();
});

test("successful lazy launch restores to status and never focuses the still-disabled trigger", async () => {
  const controlled = controlledLazyDialog();
  const view = await mount(h(RoundLaunchHandoff, {
    LazyDialog: controlled.Component,
    onOuterClose() {},
  }));
  await flushFrames();
  const trigger = view.host.querySelector('[data-launch-trigger="true"]');
  trigger.focus();
  await click(trigger);
  await flushFrames();
  await controlled.resolve();
  await flushFrames();

  const confirm = view.host.querySelector('[data-loaded-confirm="true"]');
  await click(confirm);
  assert.equal(trigger.disabled, true);
  await flushFrames();
  const successTarget = view.host.querySelector('[data-success-focus="true"]');
  assert.equal(document.activeElement, successTarget);
  assert.notEqual(document.activeElement, trigger);

  await view.unmount();
});
