import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const composerStyles = readFileSync(
  new URL("../src/styles/composer-polish.css", import.meta.url),
  "utf8",
);

test("an expanded mention menu raises the composer stacking context", () => {
  assert.match(
    composerStyles,
    /\.composer:has\(\.mention-menu\) \{[\s\S]*z-index: 4;/,
  );
});
