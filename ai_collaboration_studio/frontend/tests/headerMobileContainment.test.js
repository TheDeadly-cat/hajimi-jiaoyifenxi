import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const conversationShellStyles = readFileSync(
  new URL("../src/styles/conversation-shell-polish.css", import.meta.url),
  "utf8",
);

test("narrow conversation identity gives title and status their own content-sized rows", () => {
  assert.match(
    conversationShellStyles,
    /@media\s*\(max-width:\s*760px\)\s*\{[\s\S]*?\.conversation-header > div:first-child\s*\{[^}]*grid-template-rows:\s*auto auto;[^}]*row-gap:\s*4px;/,
  );
  assert.match(conversationShellStyles, /\.conversation-header \.status\s*\{[^}]*white-space:\s*normal;[^}]*overflow-wrap:\s*anywhere;/);
});
