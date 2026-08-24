import assert from "node:assert/strict";
import test from "node:test";
import React, { Suspense, act, lazy, useEffect, useRef } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { bindInspectorTargetNavigation } from "../src/inspectorTargetNavigation.js";

const h = React.createElement;
const mountedRoots = new Set();
let frameSequence = 0;
let timerSequence = 0;
let pendingFrames = new Map();
let pendingTimers = new Map();

class ControlledResizeObserver {
  static instances = [];

  constructor(callback) {
    this.callback = callback;
    this.disconnected = false;
    this.observed = new Set();
    ControlledResizeObserver.instances.push(this);
  }

  observe(target) {
    this.observed.add(target);
  }

  disconnect() {
    this.disconnected = true;
    this.observed.clear();
  }

  emit() {
    if (!this.disconnected) this.callback([], this);
  }
}

class ControlledMutationObserver {
  static instances = [];

  constructor(callback) {
    this.callback = callback;
    this.disconnected = false;
    this.target = null;
    this.options = null;
    ControlledMutationObserver.instances.push(this);
  }

  observe(target, options) {
    this.target = target;
    this.options = options;
  }

  disconnect() {
    this.disconnected = true;
    this.target = null;
  }

  emit() {
    if (!this.disconnected) this.callback([], this);
  }
}

function installDom() {
  const dom = new JSDOM("<!doctype html><html><body></body></html>", {
    pretendToBeVisual: true,
    url: "http://inspector-target-navigation.test/",
  });
  const { window } = dom;
  Object.defineProperties(globalThis, {
    window: { configurable: true, value: window },
    document: { configurable: true, value: window.document },
    navigator: { configurable: true, value: window.navigator },
    HTMLElement: { configurable: true, value: window.HTMLElement },
    Node: { configurable: true, value: window.Node },
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  return dom;
}

const dom = installDom();
const runtime = {
  ResizeObserver: ControlledResizeObserver,
  MutationObserver: ControlledMutationObserver,
  requestAnimationFrame(callback) {
    const frameId = ++frameSequence;
    pendingFrames.set(frameId, callback);
    return frameId;
  },
  cancelAnimationFrame(frameId) {
    pendingFrames.delete(frameId);
  },
  setTimeout(callback) {
    const timerId = ++timerSequence;
    pendingTimers.set(timerId, callback);
    return timerId;
  },
  clearTimeout(timerId) {
    pendingTimers.delete(timerId);
  },
};

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

function LoadedActionDesk({ inspectorRef, layout }) {
  return h("section", {
    id: "inspector-action-desk",
    ref(target) {
      if (!target) return;
      target.getBoundingClientRect = () => ({
        top: 20 + layout.targetOffset - inspectorRef.current.scrollTop,
      });
    },
    tabIndex: -1,
  }, "Action Desk");
}

function controlledLazyActionDesk() {
  let resolveModule;
  const modulePromise = new Promise((resolve) => {
    resolveModule = resolve;
  });
  return {
    Component: lazy(() => modulePromise),
    async resolve() {
      await act(async () => {
        resolveModule({ default: LoadedActionDesk });
        await modulePromise;
      });
    },
  };
}

function NavigationHarness({ LazyActionDesk, layout }) {
  const inspectorRef = useRef(null);
  useEffect(() => bindInspectorTargetNavigation(
    inspectorRef.current,
    "inspector-action-desk",
    runtime,
  ), []);
  return h(
    "aside",
    {
      "data-inspector": "true",
      ref(node) {
        inspectorRef.current = node;
        if (node) node.getBoundingClientRect = () => ({ top: 20 });
      },
    },
    h("section", { "data-preceding-lazy-panel": "true" }, "Preceding lazy panel"),
    h(
      Suspense,
      { fallback: h("section", { "data-action-desk-fallback": "true" }, "Loading Action Desk") },
      h(LazyActionDesk, { inspectorRef, layout }),
    ),
  );
}

test.beforeEach(() => {
  document.body.replaceChildren();
  pendingFrames.clear();
  pendingTimers.clear();
  ControlledResizeObserver.instances = [];
  ControlledMutationObserver.instances = [];
});

test.afterEach(async () => {
  for (const root of [...mountedRoots]) {
    await act(async () => root.unmount());
    mountedRoots.delete(root);
  }
  pendingFrames.clear();
  pendingTimers.clear();
});

test.after(() => dom.window.close());

test("delayed Action Desk mount receives focus and remains aligned after a preceding lazy layout shift", async () => {
  const controlled = controlledLazyActionDesk();
  const layout = { targetOffset: 620 };
  const view = await mount(h(NavigationHarness, {
    LazyActionDesk: controlled.Component,
    layout,
  }));
  const inspector = view.host.querySelector('[data-inspector="true"]');
  const resizeObserver = ControlledResizeObserver.instances[0];
  const mutationObserver = ControlledMutationObserver.instances[0];

  assert.ok(view.host.querySelector('[data-action-desk-fallback="true"]'));
  assert.equal(view.host.querySelector("#inspector-action-desk"), null);
  assert.equal(inspector.scrollTop, 0);
  assert.equal(pendingTimers.size, 1);
  assert.deepEqual(mutationObserver.options, { childList: true, subtree: true });

  await controlled.resolve();
  const target = view.host.querySelector("#inspector-action-desk");
  assert.ok(target);
  mutationObserver.emit();
  await flushFrames();

  assert.equal(inspector.scrollTop, 620);
  assert.equal(document.activeElement, target);
  assert.ok(resizeObserver.observed.has(target));

  layout.targetOffset = 780;
  resizeObserver.emit();
  await flushFrames();
  assert.equal(inspector.scrollTop, 780);
  assert.equal(document.activeElement, target);

  await view.unmount();
  assert.equal(resizeObserver.disconnected, true);
  assert.equal(mutationObserver.disconnected, true);
  assert.equal(pendingFrames.size, 0);
  assert.equal(pendingTimers.size, 0);
});
