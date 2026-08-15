import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import test from "node:test";
import React, { act, useRef, useState } from "react";
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

let frameSequence = 0;
let pendingFrames = new Map();

function installDom() {
  const dom = new JSDOM("<!doctype html><html><body></body></html>", {
    pretendToBeVisual: true,
    url: "http://observation-dialog.test/",
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
const { createRoot } = await import("react-dom/client");
const { ObservationDialog } = await vite.ssrLoadModule("/src/components/ObservationPanel.jsx");

async function flushFrames() {
  await act(async () => {
    const frames = [...pendingFrames.entries()];
    pendingFrames = new Map();
    for (const [frameId, callback] of frames) callback(frameId);
  });
}

function setControlValue(control, value) {
  const prototype = control instanceof window.HTMLSelectElement
    ? window.HTMLSelectElement.prototype
    : control instanceof window.HTMLTextAreaElement
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
  const valueSetter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
  valueSetter.call(control, value);
  control.dispatchEvent(new Event(control instanceof window.HTMLSelectElement ? "change" : "input", { bubbles: true }));
  if (!(control instanceof window.HTMLSelectElement)) {
    control.dispatchEvent(new Event("change", { bubbles: true }));
  }
}

function keydown(key) {
  const event = new KeyboardEvent("keydown", {
    bubbles: true,
    cancelable: true,
    key,
  });
  document.activeElement.dispatchEvent(event);
  return event;
}

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function ObservationHarness({ request, submittedPayloads }) {
  const [open, setOpen] = useState(false);
  const fallbackRef = useRef(null);
  return h(
    React.Fragment,
    null,
    h("button", { "data-testid": "exact-trigger", onClick: () => setOpen(true), type: "button" }, "open"),
    h("button", { "data-testid": "fallback-trigger", ref: fallbackRef, type: "button" }, "fallback"),
    h(ObservationDialog, {
      materials: [
        { id: "material-1", title: "Source one", version: 1 },
        { id: "material-2", title: "Source two", version: 3 },
      ],
      onClose: () => setOpen(false),
      onSubmit: async (payload) => {
        submittedPayloads.push(payload);
        await request.promise;
        setOpen(false);
      },
      open,
      restoreFocusRef: fallbackRef,
    }),
  );
}

test.beforeEach(() => {
  document.body.replaceChildren();
  pendingFrames.clear();
});

test.after(async () => {
  dom.window.close();
  await vite.close();
});

test("mounted observation dialog submits one edited payload and stays fail-closed while pending", async () => {
  const request = deferred();
  const submittedPayloads = [];
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);

  try {
    await act(async () => {
      root.render(h(ObservationHarness, { request, submittedPayloads }));
    });
    const exactTrigger = host.querySelector('[data-testid="exact-trigger"]');
    const fallbackTrigger = host.querySelector('[data-testid="fallback-trigger"]');
    exactTrigger.focus();
    await act(async () => exactTrigger.click());
    await flushFrames();

    const dialog = host.querySelector('[role="dialog"]');
    assert.ok(dialog, "the real observation dialog should mount");
    assert.notEqual(document.activeElement, exactTrigger, "opening should move focus into the dialog");

    const selects = dialog.querySelectorAll("select");
    const numberInputs = dialog.querySelectorAll('input[type="number"]');
    const methodologyInput = dialog.querySelector('input:not([type])');
    const textareas = dialog.querySelectorAll("textarea");
    const evidenceCheckboxes = dialog.querySelectorAll('input[type="checkbox"]');
    const edit = async (control, value) => act(async () => setControlValue(control, value));
    await edit(selects[0], "US.STX");
    await edit(selects[1], "DOWN");
    await edit(selects[2], "20");
    await edit(numberInputs[0], "4.5");
    await edit(numberInputs[1], "61");
    await edit(methodologyInput, "bounded_drawdown");
    await edit(numberInputs[2], "3");
    await edit(textareas[0], "Demand evidence must remain observable at the cutoff.");
    await edit(textareas[1], "Invalidate when the reported demand signal reverses.");
    await act(async () => evidenceCheckboxes[1].click());

    const submitButton = dialog.querySelector('button[type="submit"]');
    assert.equal(dialog.checkValidity(), true, JSON.stringify(
      Array.from(dialog.elements)
        .filter((control) => typeof control.checkValidity === "function" && !control.checkValidity())
        .map((control) => ({ value: control.value, validationMessage: control.validationMessage })),
    ));
    await act(async () => submitButton.click());

    assert.equal(submittedPayloads.length, 1, "one user submit must produce one business call");
    assert.deepEqual(submittedPayloads[0], {
      symbol: "US.STX",
      direction: "DOWN",
      horizon_days: 20,
      threshold_pct: "4.5",
      model_confidence: "61",
      methodology_id: "bounded_drawdown",
      methodology_version: 3,
      thesis: "Demand evidence must remain observable at the cutoff.",
      counter_case: "Invalidate when the reported demand signal reverses.",
      evidence: { material_ids: ["material-2"], message_ids: [] },
    });
    assert.equal(dialog.getAttribute("aria-busy"), "true");
    assert.equal(document.activeElement, dialog, "pending work should move focus to the busy surface");
    for (const button of dialog.querySelectorAll("button")) {
      assert.equal(button.disabled, true, "all dismissal and submit buttons should be disabled while pending");
    }

    submitButton.click();
    assert.equal(submittedPayloads.length, 1, "a pending request must not be submitted twice");
    const escape = keydown("Escape");
    assert.equal(escape.defaultPrevented, true);
    assert.ok(host.querySelector('[role="dialog"]'), "Escape must not dismiss pending work");

    await act(async () => {
      request.resolve();
      await request.promise;
      await Promise.resolve();
    });
    assert.equal(host.querySelector('[role="dialog"]'), null, "the successful host callback should close the dialog");
    await flushFrames();
    assert.equal(document.activeElement, exactTrigger, "focus should return to the exact opener before the fallback");
    assert.notEqual(document.activeElement, fallbackTrigger);
  } finally {
    await act(async () => root.unmount());
    host.remove();
  }
});
