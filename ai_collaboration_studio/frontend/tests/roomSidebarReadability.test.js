import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("../src/components/RoomSidebar.jsx", import.meta.url),
  "utf8",
);
const styles = readFileSync(
  new URL("../src/styles/room-sidebar-polish.css", import.meta.url),
  "utf8",
);

test("room rows preserve exact keys while parsing one semantic timestamp", () => {
  assert.match(source, /key=\{room\.id\}/);
  assert.match(source, /<strong title=\{room\.title\}>\{room\.title\}<\/strong>/);
  assert.match(
    source,
    /const timestamp = roomTimeDetails\(room\.last_message_at \|\| room\.updated_at, renderedAt\);/,
  );
  assert.match(
    source,
    /function roomTimeDetails\(timestamp, today\)[\s\S]*dateTime: date\.toISOString\(\)[\s\S]*fullLabel: date\.toLocaleString/,
  );
  assert.match(source, /dateTime=\{timestamp\.dateTime\}[\s\S]*aria-label=\{`最后更新：\$\{timestamp\.fullLabel\}`\}/);
  assert.doesNotMatch(source, /function roomDateTime|function roomTime\(/);
});

test("room rows preserve long titles while anchoring current state and sticky groups", () => {
  assert.match(
    styles,
    /\.room-sidebar \.room-row\s*\{[\s\S]*grid-template-columns:\s*12px minmax\(0, 1fr\);/,
  );
  assert.match(
    styles,
    /\.room-sidebar \.room-copy\s*\{[\s\S]*position:\s*relative;[\s\S]*min-width:\s*0;/,
  );
  assert.match(
    styles,
    /\.room-sidebar \.room-title-line\s*\{[\s\S]*display:\s*flex;[\s\S]*padding-right:\s*34px;/,
  );
  assert.match(styles, /\.room-sidebar \.room-copy strong\s*\{[\s\S]*flex:\s*1 1 auto;[\s\S]*-webkit-line-clamp:\s*2;[\s\S]*white-space:\s*normal;/);
  assert.match(
    styles,
    /\.room-sidebar \.room-row time\s*\{[\s\S]*position:\s*absolute;[\s\S]*right:\s*0;/,
  );
  assert.match(source, /active && <span className="room-current-label" aria-hidden="true">当前<\/span>/);
  assert.match(styles, /\.room-sidebar \.room-current-label\s*\{[\s\S]*border-radius:\s*999px;/);
  assert.match(styles, /\.room-sidebar \.room-category-heading\s*\{[\s\S]*position:\s*sticky;[\s\S]*backdrop-filter:\s*blur\(8px\);/);
});
