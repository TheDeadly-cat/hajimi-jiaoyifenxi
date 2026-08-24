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

test("room search exposes one named controlled search, live results, and clear paths", () => {
  assert.match(source, /className="search-box" role="search" aria-label="房间搜索"/);
  assert.match(source, /ref=\{searchInputRef\}[\s\S]*type="search"[\s\S]*value=\{search\}/);
  assert.match(source, /aria-controls="room-sidebar-list"/);
  assert.match(source, /aria-describedby="room-search-status"/);
  assert.match(source, /id="room-search-status" role="status" aria-live="polite" aria-atomic="true"/);
  assert.match(source, /normalizedSearch[\s\S]*`\$\{visibleRoomCount\}\/\$\{totalRoomCount\} 个匹配`/);
  assert.match(source, /event\.key === "Escape" && search[\s\S]*onSearch\(""\)/);
  assert.match(source, /\{search && \([\s\S]*aria-label="清除房间搜索"/);
  assert.match(source, /onSearch\(""\);[\s\S]*searchInputRef\.current\?\.focus\(\)/);
});

test("room search clear control is component-owned and avoids a duplicate native cancel", () => {
  assert.match(
    styles,
    /\.room-sidebar \.search-box\s*\{[\s\S]*display:\s*grid;[\s\S]*grid-template-columns:\s*auto minmax\(0, 1fr\) auto;/,
  );
  assert.match(styles, /\.room-sidebar \.search-box\s*\{[\s\S]*min-height:\s*44px;/);
  assert.match(styles, /input::\-webkit-search-cancel-button\s*\{\s*display:\s*none;/);
  assert.match(
    styles,
    /\.room-sidebar \.room-search-clear\s*\{[\s\S]*width:\s*40px;[\s\S]*height:\s*40px;/,
  );
  assert.match(styles, /\.room-sidebar \.room-search-clear:focus-visible\s*\{[\s\S]*outline:/);
  assert.match(
    styles,
    /@media \(max-width: 760px\)[\s\S]*\.room-sidebar \.room-search-clear\s*\{[\s\S]*width:\s*40px;[\s\S]*height:\s*40px;/,
  );
  assert.match(
    styles,
    /\.room-sidebar \.sidebar-brand,[\s\S]*\.room-sidebar > \.sidebar-section-label\s*\{\s*flex:\s*0 0 auto;/,
  );
  assert.match(
    styles,
    /\.room-sidebar \.room-list\s*\{[\s\S]*flex:\s*1 1 auto;[\s\S]*min-height:\s*0;[\s\S]*overflow-y:\s*auto;/,
  );
  assert.match(source, /className="secondary room-empty-reset"[\s\S]*onSearch\(""\);[\s\S]*searchInputRef\.current\?\.focus\(\)/);
  assert.match(styles, /\.room-sidebar \.empty-note\s*\{[\s\S]*display:\s*grid;[\s\S]*justify-items:\s*center;/);
});
