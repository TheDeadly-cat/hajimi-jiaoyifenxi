import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const indexSource = readFileSync(new URL("../index.html", import.meta.url), "utf8");
const hostStyles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
const composerSource = readFileSync(new URL("../src/components/Composer.jsx", import.meta.url), "utf8");
const launchSource = readFileSync(new URL("../src/components/RoundLaunchDialog.jsx", import.meta.url), "utf8");
const launchStyles = readFileSync(new URL("../src/styles/round-launch.css", import.meta.url), "utf8");
const historyStyles = readFileSync(new URL("../src/styles/member-version-history.css", import.meta.url), "utf8");
const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");

test("the visual viewport resizes around the software keyboard", () => {
  assert.match(indexSource, /interactive-widget=resizes-content, viewport-fit=cover/);
  assert.match(appSource, /useEffect\(\(\) => bindVisualViewportCssVars\(\), \[\]\)/);
  assert.match(hostStyles, /\.dialog\s*\{[\s\S]*max-height:\s*calc\(var\(--visual-viewport-height, 100dvh\) - 40px\)/);
  assert.match(launchStyles, /max-height:\s*calc\(var\(--visual-viewport-height, 100dvh\) - 24px\)/);
  assert.match(
    historyStyles,
    /max-height:\s*min\(920px, calc\(var\(--visual-viewport-height, 100dvh\) - max\(24px, env\(safe-area-inset-top\)\) - max\(24px, env\(safe-area-inset-bottom\)\)\)\)/,
  );
  assert.match(hostStyles, /\.drawer-scrim\s*\{[\s\S]*height:\s*var\(--visual-viewport-height, 100dvh\)/);
  assert.match(hostStyles, /\.drawer-scrim\s*\{[\s\S]*left:\s*var\(--visual-viewport-offset-left, 0px\);[\s\S]*width:\s*var\(--visual-viewport-width, 100vw\)/);
  assert.match(hostStyles, /\.dialog-backdrop\s*\{[\s\S]*left:\s*var\(--visual-viewport-offset-left, 0px\);[\s\S]*width:\s*var\(--visual-viewport-width, 100vw\)/);
  assert.match(hostStyles, /\.inspector-wrap\s*\{[\s\S]*right:\s*calc\(100vw - var\(--visual-viewport-offset-left, 0px\) - var\(--visual-viewport-width, 100vw\)\)/);
  assert.match(hostStyles, /\.room-sidebar\s*\{[\s\S]*left:\s*var\(--visual-viewport-offset-left, 0px\)/);
  assert.doesNotMatch(`${hostStyles}\n${launchSource}\n${launchStyles}\n${historyStyles}`, /100vh/);
});

test("the mention picker stays reachable in a short keyboard viewport", () => {
  const mentionRule = hostStyles.match(/\.mention-menu\s*\{([^}]*)\}/)?.[1] || "";
  assert.match(mentionRule, /width:\s*min\(248px, calc\(var\(--visual-viewport-width, 100vw\) - 32px\)\)/);
  assert.match(mentionRule, /max-height:\s*clamp\(96px, calc\(var\(--visual-viewport-height, 100dvh\) - 190px\), 384px\)/);
  assert.match(mentionRule, /overflow-y:\s*auto/);
  assert.match(mentionRule, /overscroll-behavior:\s*contain/);
});

test("compact shell and composer controls share one 44px touch target", () => {
  assert.match(hostStyles, /--mobile-touch-target:\s*44px/);
  assert.match(hostStyles, /@media \(max-width: 760px\)[\s\S]*\.icon-button\s*\{[\s\S]*width:\s*var\(--mobile-touch-target\);[\s\S]*height:\s*var\(--mobile-touch-target\)/);
  assert.match(hostStyles, /@media \(max-width: 760px\)[\s\S]*\.conversation-header \.inspector-toggle,[\s\S]*\.history-load-button,[\s\S]*\.history-search-button[\s\S]*min-height:\s*var\(--mobile-touch-target\)/);
  assert.match(hostStyles, /@media \(max-width: 760px\)[\s\S]*\.composer-actions \.secondary,[\s\S]*\.composer-actions \.primary[\s\S]*min-height:\s*var\(--mobile-touch-target\)/);
});

test("the mention menu has bounded keyboard ownership and returns to typing", () => {
  assert.match(composerSource, /aria-haspopup="menu"/);
  assert.match(composerSource, /aria-controls=\{mentionOpen \? mentionMenuId : undefined\}/);
  assert.match(composerSource, /aria-label="选择提及成员"/);
  assert.match(composerSource, /ArrowDown:[\s\S]*ArrowUp:[\s\S]*Home:[\s\S]*End:/);
  assert.match(composerSource, /event\.key === "Escape"[\s\S]*mentionButtonRef\.current\?\.focus\(\)/);
  assert.match(composerSource, /\["Enter", " "\]\.includes\(event\.key\)[\s\S]*event\.target\.click\(\)/);
  assert.match(composerSource, /event\.currentTarget\.contains\(event\.relatedTarget\)/);
  assert.match(composerSource, /textareaRef\.current\?\.focus\(\)/);
  assert.match(composerSource, /tabIndex=\{index === 0 \? 0 : -1\}/);
});
