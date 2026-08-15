import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
const actionOverviewStyles = readFileSync(new URL("../src/styles/action-overview.css", import.meta.url), "utf8");
const footballStyles = readFileSync(new URL("../src/styles/football-research.css", import.meta.url), "utf8");
const stockStyles = readFileSync(new URL("../src/styles/stock-research.css", import.meta.url), "utf8");
const marker = "@media (prefers-reduced-motion: reduce)";
const start = styles.indexOf(marker);
const end = styles.indexOf("/* --------------------------------------------------------------------------", start);
const reducedMotion = styles.slice(start, end);

test("reduced-motion contract targets owned moving surfaces without a blanket reset", () => {
  assert.notEqual(start, -1);
  assert.ok(end > start);
  assert.doesNotMatch(reducedMotion, /(^|,)\s*\*(?:::before|::after)?\s*(?:,|\{)/m);
  assert.match(
    reducedMotion,
    /\.drawer-scrim,\s*\.inspector-wrap,\s*\.room-sidebar\s*\{\s*transition: none !important;\s*transition-delay: 0s !important;\s*\}/,
  );
  assert.match(actionOverviewStyles, /@media \(prefers-reduced-motion: reduce\)[\s\S]*\.action-overview-scrim,[\s\S]*\.action-overview-drawer[\s\S]*transition: none !important;/);
  assert.match(reducedMotion, /\.dialog-backdrop,[\s\S]*\.dialog,[\s\S]*\.deferred-surface-fallback[\s\S]*animation: none !important;[\s\S]*transition: none !important/);
  assert.match(reducedMotion, /\.chat-timeline\s*\{\s*scroll-behavior: auto;/);
});

test("loading and composing states stay visible while their repeated motion stops", () => {
  assert.match(reducedMotion, /\.spin\s*\{\s*animation: none !important;/);
  assert.match(reducedMotion, /\.typing-indicator i\s*\{[\s\S]*animation: none;[\s\S]*opacity: 1;[\s\S]*transform: none;/);
  assert.doesNotMatch(reducedMotion, /\.spin\s*\{[^}]*display:\s*none/);
  assert.doesNotMatch(reducedMotion, /\.spin\s*\{[^}]*opacity:\s*0(?:\D|$)/);
});

test("disclosure direction remains semantic and hover controls no longer shift position", () => {
  assert.match(stockStyles, /@media \(prefers-reduced-motion: reduce\)[\s\S]*\.stock-research-toggle > svg:last-child\s*\{\s*transition: none !important;/);
  assert.match(footballStyles, /@media \(prefers-reduced-motion: reduce\)[\s\S]*\.football-research-toggle > svg:last-child\s*\{\s*transition: none !important;/);
  assert.doesNotMatch(`${stockStyles}\n${footballStyles}`, /(?:stock|football)-research-toggle[^{}]*\{[^}]*transform:\s*none/);
  assert.match(reducedMotion, /\.round-trace-summary:hover,\s*\.rail-button:hover,\s*\.primary:hover\s*\{\s*transform: none !important;/);
});
