import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const styles = readFileSync(
  new URL("../src/styles/composer-polish.css", import.meta.url),
  "utf8",
);

function rule(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = styles.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`));
  assert.ok(match, `${selector} should exist`);
  return match[1];
}

test("Composer reflows toolbar controls when an inspector narrows a desktop conversation", () => {
  const toolbar = rule(".composer .composer-toolbar");
  assert.match(toolbar, /display:\s*flex;/);
  assert.match(toolbar, /flex-wrap:\s*wrap;/);
  assert.doesNotMatch(styles, /grid-template-areas:|@media \(min-width:\s*1181px\)/);
});

test("optional keyboard hints follow the Composer width rather than the whole viewport", () => {
  assert.match(rule(".composer"), /container-name:\s*message-composer;/);
  assert.match(rule(".composer"), /container-type:\s*inline-size;/);
  assert.match(rule(".composer-keyboard-hint"), /display:\s*none;/);
  assert.match(styles, /@container message-composer \(min-width:\s*880px\)\s*\{\s*\.composer-keyboard-hint\s*\{\s*display:\s*inline-flex;/);
});

test("launch prerequisites remain readable without ellipsis or absolute positioning", () => {
  const status = rule(".composer .composer-provider-summary");
  assert.match(status, /max-width:\s*100%;/);
  assert.match(status, /white-space:\s*normal;/);
  assert.match(status, /overflow-wrap:\s*anywhere;/);
  assert.match(status, /overflow:\s*visible;/);
  assert.doesNotMatch(status, /position:\s*absolute|text-overflow:\s*ellipsis/);
});

test("action buttons can wrap within the available Composer width", () => {
  const actions = rule(".composer .composer-actions");
  assert.match(actions, /flex-wrap:\s*wrap;/);
  assert.match(actions, /min-width:\s*0;/);
  assert.match(actions, /max-width:\s*100%;/);
  assert.doesNotMatch(actions, /overflow:\s*hidden|position:\s*absolute/);
});
