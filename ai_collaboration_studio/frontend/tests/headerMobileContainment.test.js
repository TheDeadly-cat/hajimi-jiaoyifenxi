import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const conversationShellStyles = readFileSync(
  new URL("../src/styles/conversation-shell-polish.css", import.meta.url),
  "utf8",
);

test("narrow conversation identity removes the inherited grid row gap", () => {
  assert.match(
    conversationShellStyles,
    /@media\s*\(max-width:\s*760px\)\s*\{[\s\S]*?\.conversation-header > div:first-child\s*\{[^}]*row-gap:\s*0\s*;/,
  );
});
