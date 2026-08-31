import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import test from "node:test";
import React, { act } from "react";
import { JSDOM } from "jsdom";
import { createServer } from "vite";

const h = React.createElement;
const dom = new JSDOM("<!doctype html><html><body><div id=host></div></body></html>", {
  pretendToBeVisual: true,
  url: "http://icon-rail.test/",
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
const { IconRail } = await vite.ssrLoadModule("/src/components/IconRail.jsx");
const root = createRoot(document.getElementById("host"));

async function render(unreadCount) {
  await act(async () => {
    root.render(h(IconRail, {
      activeSection: "rooms",
      onNavigate() {},
      onPreloadInspector() {},
      onPreloadSourceInbox() {},
      sourceInboxUnreadCount: unreadCount,
    }));
  });
  return document.querySelector('[data-section="source-inbox"]');
}

test.after(async () => {
  await act(async () => root.unmount());
  await vite.close();
  dom.window.close();
});

test("source inbox badge is clamped, accessible, and outside the tab order", async () => {
  let button = await render(7);
  let badge = button.querySelector(".source-inbox-unread-badge");
  assert.equal(button.getAttribute("aria-label"), "来源收件箱，7 条未读");
  assert.equal(badge.textContent, "7");
  assert.equal(badge.getAttribute("aria-hidden"), "true");
  assert.equal(badge.hasAttribute("aria-live"), false);
  assert.equal(button.tabIndex, -1, "the badge must not change the existing roving focus owner");

  button = await render(10_000);
  badge = button.querySelector(".source-inbox-unread-badge");
  assert.equal(button.getAttribute("aria-label"), "来源收件箱，99 条以上未读");
  assert.equal(badge.textContent, "99+");

  button = await render(-1);
  assert.equal(button.getAttribute("aria-label"), "来源收件箱");
  assert.equal(button.querySelector(".source-inbox-unread-badge"), null);
});
