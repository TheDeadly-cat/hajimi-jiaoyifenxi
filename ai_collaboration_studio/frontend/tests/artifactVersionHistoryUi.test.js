import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const historySource = readFileSync(
  new URL("../src/components/ArtifactVersionHistory.jsx", import.meta.url),
  "utf8",
);
const refinementStyles = readFileSync(
  new URL("../src/styles/artifact-version-history-refinement.css", import.meta.url),
  "utf8",
);

test("artifact version history uses exact diff identities and bounded section rows", () => {
  assert.match(historySource, /const SECTION_DIFF_PREVIEW_LIMIT = 4/);
  assert.match(historySource, /function artifactVersionListKey\(\.\.\.parts\)/);
  assert.match(historySource, /key=\{artifactVersionListKey\("scalar-change", change\.key\)\}/);
  assert.match(historySource, /key=\{artifactVersionListKey\("section-change", section\.key, leftVersion, rightVersion\)\}/);
  assert.match(historySource, /row\.item\.id,/);
  assert.match(historySource, /artifactVersionListKey\("field-change", section\.key, row\.item\.id, field\.field\)/);
  assert.doesNotMatch(historySource, /sectionIndex|itemIndex|fieldIndex/);
  assert.match(historySource, /rows\.slice\(0, SECTION_DIFF_PREVIEW_LIMIT\)/);
  assert.match(historySource, /aria-controls=\{rowsId\}/);
  assert.match(historySource, /aria-expanded=\{expanded\}/);
  assert.match(historySource, /再显示 \$\{hiddenRowCount\} 条变化/);
  assert.match(refinementStyles, /\.artifact-section-diff-rows\s*\{[^}]*display: grid/s);
  assert.match(refinementStyles, /\.artifact-section-diff-control\s*\{[^}]*min-height: 44px/s);
  assert.match(refinementStyles, /@container \(max-width: 560px\)/);
});
