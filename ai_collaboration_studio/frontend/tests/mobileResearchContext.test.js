import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

test("mobile conversation header keeps the room state visible in a two-line research context", () => {
  assert.match(styles, /@media \(max-width:\s*760px\)[\s\S]*?\.conversation-panel\s*\{\s*grid-template-rows:\s*clamp\(56px,\s*calc\(var\(--visual-viewport-height, 100dvh\) - 394px\),\s*70px\)/);
  assert.match(styles, /\.conversation-header > div:first-child\s*\{[\s\S]*?grid-template-columns:\s*var\(--mobile-touch-target\) minmax\(0, 1fr\);[\s\S]*?grid-template-rows:\s*26px 24px;/);
  assert.match(styles, /\.conversation-header \.mobile-room-toggle\s*\{\s*grid-row:\s*1 \/ span 2;/);
  assert.match(styles, /\.conversation-header \.status\s*\{[\s\S]*?display:\s*inline-flex;[\s\S]*?font-size:\s*9px;/);
  assert.doesNotMatch(styles, /@media \(max-width:\s*760px\)[\s\S]*?\.conversation-header \.status\s*\{\s*display:\s*none;/);
});

test("mobile empty conversation uses a compact evidence brief instead of a landing-page card", () => {
});
