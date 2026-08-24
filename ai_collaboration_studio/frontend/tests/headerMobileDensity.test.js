import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const app = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
const shellStyles = readFileSync(
  new URL("../src/styles/conversation-shell-polish.css", import.meta.url),
  "utf8",
);

test("room inspector toggle keeps its accessible text in the host markup", () => {
  const toggleStart = app.indexOf('className="secondary inspector-toggle"');
  const toggleEnd = app.indexOf("</button>", toggleStart);

  assert.notEqual(toggleStart, -1);
  assert.ok(toggleEnd > toggleStart);
  assert.match(app.slice(toggleStart, toggleEnd), /房间信息/);
  assert.match(
    app,
    /<strong title=\{room\?\.title \|\| "AI 共创室"\}>\{room\?\.title \|\| "AI 共创室"\}<\/strong>/,
  );
});

test("extremely narrow header compresses the inspector to one touch target", () => {
  const marker = "/* Narrow header density: keep the accessible label in markup. */";
  const start = styles.indexOf(marker);

  assert.notEqual(start, -1);
  const block = styles.slice(start);
  assert.match(block, /@media \(max-width:\s*430px\)/);
  assert.match(
    block,
    /\.conversation-header \.inspector-toggle\s*\{[\s\S]*?width:\s*var\(--mobile-touch-target\);[\s\S]*?min-width:\s*var\(--mobile-touch-target\);/,
  );
  assert.match(block, /padding-inline:\s*0;/);
  assert.match(block, /font-size:\s*0;/);
  assert.doesNotMatch(block, /display:\s*none|visibility:\s*hidden/);
});

test("extremely narrow header keeps a bounded two-line room identity", () => {
  const marker = "/* Narrow room identity: preserve two readable lines without growing the shell. */";
  const block = shellStyles.slice(shellStyles.indexOf(marker));

  assert.notEqual(shellStyles.indexOf(marker), -1);
  assert.match(
    block,
    /\.conversation-header > div:first-child\s*\{[\s\S]*grid-template-rows:\s*minmax\(32px, auto\) 20px;/,
  );
  assert.match(
    block,
    /\.conversation-header > div:first-child > strong\s*\{[\s\S]*overflow-wrap:\s*anywhere;[\s\S]*-webkit-line-clamp:\s*2;[\s\S]*white-space:\s*normal;/,
  );
  const globalNarrow = styles.slice(
    styles.indexOf("/* Narrow header density: keep the accessible label in markup. */"),
  );
  assert.doesNotMatch(globalNarrow, /-webkit-line-clamp:\s*2/);
});
