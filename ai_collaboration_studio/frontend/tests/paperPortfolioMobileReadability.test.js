import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("../src/styles/paper-portfolio.css", import.meta.url),
  "utf8",
);

test("mobile portfolio evidence keeps decision lineage readable instead of truncating it", () => {
  const mobile = source.slice(source.indexOf("@media (max-width: 620px)"));
  assert.match(mobile, /\.paper-lineage-source strong\s*\{[\s\S]*font-size:\s*11px/);
  assert.match(mobile, /\.paper-lineage-source small\s*\{[\s\S]*font-size:\s*10px/);
  assert.match(mobile, /\.paper-lineage-source small\s*\{[\s\S]*overflow-wrap:\s*anywhere/);
  assert.match(mobile, /\.paper-lineage-source small\s*\{[\s\S]*white-space:\s*normal/);
});

test("mobile portfolio decision actions use a wrapped two-column 44px target grid", () => {
  const mobile = source.slice(source.indexOf("@media (max-width: 620px)"));
  assert.match(mobile, /\.paper-portfolio-card > footer\s*\{[\s\S]*grid-template-columns:\s*repeat\(2, minmax\(0, 1fr\)\)/);
  assert.match(mobile, /\.paper-portfolio-card > footer \.text-action\s*\{[\s\S]*min-height:\s*44px/);
  assert.match(mobile, /\.paper-portfolio-card > footer \.text-action\s*\{[\s\S]*font-size:\s*11px/);
});
