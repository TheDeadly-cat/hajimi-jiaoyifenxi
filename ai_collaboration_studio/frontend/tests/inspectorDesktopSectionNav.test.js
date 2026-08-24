import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const styleSource = readFileSync(
  new URL("../src/styles/room-inspector-refinement.css", import.meta.url),
  "utf8",
);

test("desktop inspector keeps a sticky six-section index", () => {
  const desktopBlock = styleSource.split("/* Desktop inspector section index */")[1] ?? "";

  assert.match(desktopBlock, /@media \(min-width: 701px\)/);
  assert.match(
    desktopBlock,
    /#root:has\(\.app-shell\.inspector-open\) \{[\s\S]*overflow: clip;/,
  );
  assert.match(
    desktopBlock,
    /\.room-inspector-section-nav \{[\s\S]*position: sticky;[\s\S]*top: 0;[\s\S]*grid-template-columns: repeat\(6, minmax\(44px, 1fr\)\);/,
  );
  assert.match(
    desktopBlock,
    /\.room-inspector-section-nav a \{[\s\S]*min-width: 44px;[\s\S]*min-height: 44px;/,
  );
  assert.equal(desktopBlock.match(/:has\(#inspector-[a-z-]+:target\)/g)?.length, 6);
  assert.match(desktopBlock, /scroll-padding-top: 68px;/);
  assert.doesNotMatch(desktopBlock, /scroll-margin-top:/);
});
