import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const component = readFileSync(
  new URL("../src/components/ChatTimeline.jsx", import.meta.url),
  "utf8",
);
const styles = readFileSync(
  new URL("../src/styles/chat-timeline.css", import.meta.url),
  "utf8",
);

test("history toolbar publishes whether its mobile history action is terminal", () => {
  assert.match(
    component,
    /data-history-terminal=\{!searchActive && !historyState\?\.hasMore \? "true" : "false"\}/,
  );
});

test("narrow history search owns a full row while actionable history stays available", () => {
  const narrowStart = styles.indexOf("@media (max-width: 430px)");
  const narrowEnd = styles.indexOf(
    "@media (min-width: 431px) and (max-width: 760px)",
    narrowStart,
  );

  assert.notEqual(narrowStart, -1);
  assert.ok(narrowEnd > narrowStart);

  const narrow = styles.slice(narrowStart, narrowEnd);
  assert.match(narrow, /grid-template-areas:[\s\S]*?"search"[\s\S]*?"history";/);
  assert.match(
    narrow,
    /\.chat-timeline-workspace \.message-history-search\s*\{[\s\S]*?width:\s*100%;[\s\S]*?max-width:\s*none;[\s\S]*?grid-area:\s*search;/,
  );
  assert.match(
    narrow,
    /\.chat-timeline-workspace \.history-page-action\s*\{[\s\S]*?grid-area:\s*history;/,
  );
});

test("terminal mobile history removes only its disabled action row", () => {
  assert.match(
    styles,
    /\.message-history-toolbar\[data-history-terminal="true"\]\s*\{\s*grid-template-areas:\s*"search";\s*\}/,
  );
  assert.match(
    styles,
    /\.message-history-toolbar\[data-history-terminal="true"\] \.history-page-action\s*\{\s*display:\s*none;\s*\}/,
  );
});

test("history search keeps one application-owned clear control in Chromium", () => {
  assert.match(component, /ref=\{searchInputRef\}[\s\S]*aria-controls="chat-timeline-log"/);
  assert.match(component, /event\.key === "Escape"[\s\S]*clearSearch\(\)/);
  assert.match(
    component,
    /runTimelineAction\("clear-search"[\s\S]*if \(completed\) searchInputRef\.current\?\.focus\(\)/,
  );
  assert.match(
    styles,
    /\.message-history-search input::\-webkit-search-cancel-button\s*\{[\s\S]*?display:\s*none;[\s\S]*?\-webkit-appearance:\s*none;/,
  );
  assert.match(styles, /\.history-clear-search\s*\{[\s\S]*?width:\s*34px;[\s\S]*?height:\s*34px;/);
  assert.match(
    styles,
    /@media \(max-width: 430px\)[\s\S]*?\.message-history-search input\s*\{\s*height:\s*44px;[\s\S]*?\.history-clear-search\s*\{\s*width:\s*44px;\s*height:\s*44px;/,
  );
});
