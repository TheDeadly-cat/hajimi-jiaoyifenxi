import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import test from "node:test";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { createServer } from "vite";

const h = React.createElement;
const dom = new JSDOM("<!doctype html><html><body><div id=host></div></body></html>", {
  pretendToBeVisual: true,
  url: "http://composer.test/",
});
Object.defineProperties(globalThis, {
  window: { configurable: true, value: dom.window },
  document: { configurable: true, value: dom.window.document },
  navigator: { configurable: true, value: dom.window.navigator },
  HTMLElement: { configurable: true, value: dom.window.HTMLElement },
});
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const frontendRoot = fileURLToPath(new URL("../", import.meta.url));
const vite = await createServer({
  appType: "custom",
  logLevel: "silent",
  root: frontendRoot,
  server: { hmr: false, middlewareMode: true },
});
const { createRoot } = await import("react-dom/client");
const { Composer } = await vite.ssrLoadModule("/src/components/Composer.jsx");
const root = createRoot(document.getElementById("host"));
let instance = 0;

async function render({ value = "请检查这条来源", disabled = false } = {}) {
  const sent = [];
  await act(async () => {
    root.render(h(Composer, {
      key: ++instance,
      value,
      disabled,
      members: [],
      onChange() {},
      onSend() { sent.push(value); },
    }));
  });
  return { sent, textarea: document.querySelector("textarea") };
}

async function dispatch(target, event) {
  await act(async () => { target.dispatchEvent(event); });
  return event;
}

function keydown(options = {}) {
  return new dom.window.KeyboardEvent("keydown", {
    key: "Enter",
    keyCode: 13,
    bubbles: true,
    cancelable: true,
    ...options,
  });
}

function composition(type) {
  return new dom.window.CompositionEvent(type, { bubbles: true, data: "来源" });
}

test.after(async () => {
  await act(async () => root.unmount());
  await vite.close();
  dom.window.close();
});

test("Chinese IME confirmation does not send or cancel candidate selection", async () => {
  const { textarea, sent } = await render();
  await dispatch(textarea, composition("compositionstart"));
  const confirm = await dispatch(textarea, keydown({ isComposing: false }));
  assert.deepEqual(sent, []);
  assert.equal(confirm.defaultPrevented, false);

  await dispatch(textarea, composition("compositionend"));
  const send = await dispatch(textarea, keydown());
  assert.deepEqual(sent, ["请检查这条来源"]);
  assert.equal(send.defaultPrevented, true);
});

test("native composition flags protect Enter even without a compositionstart callback", async () => {
  const { textarea, sent } = await render();
  const composing = await dispatch(textarea, keydown({ isComposing: true }));
  assert.deepEqual(sent, []);
  assert.equal(composing.defaultPrevented, false);
  await dispatch(textarea, keydown());
  assert.equal(sent.length, 1);
});

test("legacy IME key code 229 cannot send after compositionend", async () => {
  const { textarea, sent } = await render();
  await dispatch(textarea, composition("compositionstart"));
  await dispatch(textarea, composition("compositionend"));
  const confirm = await dispatch(textarea, keydown({ keyCode: 229, isComposing: false }));
  assert.deepEqual(sent, []);
  assert.equal(confirm.defaultPrevented, false);
  await dispatch(textarea, keydown());
  assert.equal(sent.length, 1);
});

test("ordinary Enter sends once while Shift+Enter keeps its newline behavior", async () => {
  const { textarea, sent } = await render();
  const newline = await dispatch(textarea, keydown({ shiftKey: true }));
  assert.deepEqual(sent, []);
  assert.equal(newline.defaultPrevented, false);
  const send = await dispatch(textarea, keydown());
  assert.equal(sent.length, 1);
  assert.equal(send.defaultPrevented, true);

  for (const props of [{ value: "   " }, { disabled: true }]) {
    const guarded = await render(props);
    await dispatch(guarded.textarea, keydown());
    assert.deepEqual(guarded.sent, []);
  }
});

test("leaving the input clears a cancelled composition without blocking the next message", async () => {
  const { textarea, sent } = await render();
  await dispatch(textarea, composition("compositionstart"));
  await dispatch(textarea, new dom.window.FocusEvent("focusout", { bubbles: true }));
  const send = await dispatch(textarea, keydown());
  assert.equal(sent.length, 1);
  assert.equal(send.defaultPrevented, true);
});
