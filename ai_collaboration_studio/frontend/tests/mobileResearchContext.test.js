import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

test("mobile conversation header keeps the room state visible in a two-line research context", () => {
  assert.match(styles, /@media \(max-width:\s*760px\)[\s\S]*?\.conversation-panel\s*\{\s*grid-template-rows:\s*70px/);
  assert.match(styles, /\.conversation-header > div:first-child\s*\{[\s\S]*?grid-template-columns:\s*36px minmax\(0, 1fr\);[\s\S]*?grid-template-rows:\s*26px 24px;/);
  assert.match(styles, /\.conversation-header \.mobile-room-toggle\s*\{\s*grid-row:\s*1 \/ span 2;/);
  assert.match(styles, /\.conversation-header \.status\s*\{[\s\S]*?display:\s*inline-flex;[\s\S]*?font-size:\s*9px;/);
  assert.doesNotMatch(styles, /@media \(max-width:\s*760px\)[\s\S]*?\.conversation-header \.status\s*\{\s*display:\s*none;/);
});

test("mobile empty conversation uses a compact evidence brief instead of a landing-page card", () => {
  assert.match(styles, /@media \(max-width:\s*760px\)[\s\S]*?\.conversation-empty-state\s*\{[\s\S]*?grid-template-columns:\s*38px minmax\(0, 1fr\);[\s\S]*?margin:\s*18px 0 12px;[\s\S]*?padding:\s*16px 16px 16px 18px;[\s\S]*?border-radius:\s*14px;[\s\S]*?box-shadow:\s*none;/);
  assert.match(styles, /\.conversation-empty-state::before\s*\{[\s\S]*?width:\s*3px;[\s\S]*?background:\s*#6575e6;/);
  assert.match(styles, /\.conversation-empty-copy\s*\{\s*grid-column:\s*1 \/ -1;\s*margin-top:\s*12px;/);
  assert.match(styles, /\.conversation-empty-hints\s*\{\s*grid-column:\s*1 \/ -1;\s*gap:\s*6px;\s*margin-top:\s*12px;/);
});
