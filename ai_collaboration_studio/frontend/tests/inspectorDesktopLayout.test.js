import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

test("desktop conversation only reserves the evidence dossier column while inspector is open", () => {
  assert.match(
    appSource,
    /className=\{inspectorOpen \? "app-shell inspector-open" : "app-shell"\}/,
  );
  assert.match(
    styles,
    /\.app-shell\s*\{[\s\S]*?grid-template-columns:\s*64px 280px minmax\(520px, 1fr\);/,
  );
  assert.match(
    styles,
    /\.app-shell\.inspector-open\s*\{\s*grid-template-columns:\s*64px 280px minmax\(0, 1fr\) 340px;/,
  );
  assert.match(
    styles,
    /@media \(min-width:\s*1181px\)[\s\S]*?\.inspector-wrap:not\(\.open\)\s*\{\s*display:\s*none;/,
  );
  assert.match(styles, /\.inspector-toggle\s*\{\s*display:\s*inline-flex;/);
  assert.doesNotMatch(styles, /\.inspector-toggle\s*\{\s*display:\s*none;/);
});

test("compact and mobile inspector remains an overlay instead of adding a grid column", () => {
  assert.match(
    styles,
    /@media \(max-width:\s*1180px\)[\s\S]*?\.app-shell,\s*\.app-shell\.inspector-open\s*\{\s*grid-template-columns:\s*52px 220px minmax\(0, 1fr\);/,
  );
  assert.match(
    styles,
    /@media \(max-width:\s*760px\)[\s\S]*?\.app-shell,\s*\.app-shell\.inspector-open\s*\{\s*grid-template-columns:\s*1fr;/,
  );
});
