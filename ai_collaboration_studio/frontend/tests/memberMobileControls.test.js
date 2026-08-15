import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

test("compact member history and ordering controls use a separate 44px action row", () => {
  const mobile = source.slice(source.indexOf("@media (max-width: 620px)"));
  assert.match(mobile, /\.member-row\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\)/);
  assert.match(mobile, /\.member-order-actions\s*\{[\s\S]*grid-template-columns:\s*repeat\(3, 44px\)/);
  assert.match(mobile, /\.member-order-actions\s*\{[\s\S]*gap:\s*4px/);
  assert.match(mobile, /\.member-order-actions button\s*\{[\s\S]*width:\s*44px;[\s\S]*height:\s*44px/);
});
