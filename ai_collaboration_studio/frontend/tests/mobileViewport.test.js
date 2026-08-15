import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const indexSource = readFileSync(new URL("../index.html", import.meta.url), "utf8");
const hostStyles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
const launchSource = readFileSync(new URL("../src/components/RoundLaunchDialog.jsx", import.meta.url), "utf8");
const historyStyles = readFileSync(new URL("../src/styles/member-version-history.css", import.meta.url), "utf8");
const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");

test("the visual viewport resizes around the software keyboard", () => {
  assert.match(indexSource, /interactive-widget=resizes-content, viewport-fit=cover/);
  assert.match(appSource, /useEffect\(\(\) => bindVisualViewportCssVars\(\), \[\]\)/);
  assert.match(hostStyles, /\.dialog\s*\{[\s\S]*max-height:\s*calc\(var\(--visual-viewport-height, 100dvh\) - 40px\)/);
  assert.match(launchSource, /maxHeight:\s*"calc\(var\(--visual-viewport-height, 100dvh\) - 24px\)"/);
  assert.match(historyStyles, /max-height:\s*calc\(var\(--visual-viewport-height, 100dvh\) - 40px\)/);
  assert.match(hostStyles, /\.drawer-scrim\s*\{[\s\S]*height:\s*var\(--visual-viewport-height, 100dvh\)/);
  assert.match(hostStyles, /\.drawer-scrim\s*\{[\s\S]*left:\s*var\(--visual-viewport-offset-left, 0px\);[\s\S]*width:\s*var\(--visual-viewport-width, 100vw\)/);
  assert.match(hostStyles, /\.dialog-backdrop\s*\{[\s\S]*left:\s*var\(--visual-viewport-offset-left, 0px\);[\s\S]*width:\s*var\(--visual-viewport-width, 100vw\)/);
  assert.match(hostStyles, /\.inspector-wrap\s*\{[\s\S]*right:\s*calc\(100vw - var\(--visual-viewport-offset-left, 0px\) - var\(--visual-viewport-width, 100vw\)\)/);
  assert.match(hostStyles, /\.room-sidebar\s*\{[\s\S]*left:\s*var\(--visual-viewport-offset-left, 0px\)/);
  assert.doesNotMatch(`${hostStyles}\n${launchSource}\n${historyStyles}`, /100vh/);
});

test("the mention picker stays reachable in a short keyboard viewport", () => {
  const mentionRule = hostStyles.match(/\.mention-menu\s*\{([^}]*)\}/)?.[1] || "";
  assert.match(mentionRule, /width:\s*min\(248px, calc\(var\(--visual-viewport-width, 100vw\) - 32px\)\)/);
  assert.match(mentionRule, /max-height:\s*clamp\(96px, calc\(var\(--visual-viewport-height, 100dvh\) - 190px\), 384px\)/);
  assert.match(mentionRule, /overflow-y:\s*auto/);
  assert.match(mentionRule, /overscroll-behavior:\s*contain/);
});
