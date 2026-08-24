import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const styles = readFileSync(
  new URL("../src/styles/composer-polish.css", import.meta.url),
  "utf8",
);

function mediaBlock(startMarker, endMarker) {
  const start = styles.indexOf(startMarker);
  const end = styles.indexOf(endMarker, start + startMarker.length);
  assert.notEqual(start, -1, `${startMarker} should exist`);
  assert.ok(end > start, `${startMarker} should end before ${endMarker}`);
  return styles.slice(start, end);
}

test("desktop Composer reserves one explicit grid area for every toolbar surface", () => {
  const desktop = mediaBlock(
    "@media (min-width: 1181px)",
    "@media (max-width: 760px)",
  );

  assert.match(
    desktop,
    /grid-template-columns:\s*auto minmax\(0, 1fr\) auto auto;/,
  );
  assert.match(
    desktop,
    /grid-template-areas:\s*"mention keyboard status actions";/,
  );
  assert.match(desktop, /\.composer-keyboard-hint\s*\{[\s\S]*?grid-area:\s*keyboard;/);
  assert.match(desktop, /\.composer \.composer-actions\s*\{[\s\S]*?grid-area:\s*actions;/);
});

test("mobile Composer assigns each toolbar surface to an explicit grid area", () => {
  const mobile = mediaBlock(
    "@media (max-width: 760px)",
    "@media (max-width: 430px)",
  );

  assert.match(mobile, /\.composer \.mention-control\s*\{\s*grid-area:\s*mention;\s*\}/);
  assert.match(mobile, /\.composer \.composer-provider-summary\s*\{[\s\S]*?grid-area:\s*status;/);
  assert.match(mobile, /\.composer \.composer-actions\s*\{[\s\S]*?grid-area:\s*actions;/);
});

test("narrow Composer gives provider status its own row without overlap hacks", () => {
  const narrow = mediaBlock(
    "@media (max-width: 430px)",
    "@media (min-width: 431px) and (max-width: 760px)",
  );

  assert.match(narrow, /grid-template-columns:\s*44px minmax\(0, 1fr\);/);
  assert.match(narrow, /grid-template-areas:[\s\S]*?"status status"[\s\S]*?"mention actions";/);
  assert.match(narrow, /\.composer \.composer-provider-summary\s*\{[\s\S]*?max-width:\s*100%;/);
  assert.doesNotMatch(narrow, /position:\s*absolute|margin-(?:left|right):\s*-/);
});

test("mid-width Composer keeps mention, status, and actions in one row", () => {
  const midWidth = mediaBlock(
    "@media (min-width: 431px) and (max-width: 760px)",
    "@media (prefers-reduced-motion: reduce)",
  );

  assert.match(midWidth, /grid-template-columns:\s*44px minmax\(0, 1fr\) auto;/);
  assert.match(midWidth, /grid-template-areas:\s*"mention status actions";/);
  assert.match(midWidth, /max-width:\s*130px;/);
});
