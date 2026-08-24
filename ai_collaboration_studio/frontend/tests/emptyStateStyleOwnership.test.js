import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const hostStyles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
const timelineStyles = readFileSync(
  new URL("../src/styles/chat-timeline.css", import.meta.url),
  "utf8",
);
const timelineComponent = readFileSync(
  new URL("../src/components/ChatTimeline.jsx", import.meta.url),
  "utf8",
);

test("conversation empty-state styles have one component-owned source", () => {
  assert.doesNotMatch(hostStyles, /conversation-empty-/);

  const selectors = timelineStyles.match(/[^{}]*conversation-empty-[^{}]*\{/g) || [];
  assert.ok(selectors.length >= 12);
  for (const selector of selectors) {
    assert.match(selector, /\.chat-timeline-workspace/);
  }
});

test("removed empty hints have neither markup nor style ownership", () => {
  assert.doesNotMatch(timelineComponent, /conversation-empty-hints/);
  assert.doesNotMatch(timelineStyles, /conversation-empty-hints/);
});
