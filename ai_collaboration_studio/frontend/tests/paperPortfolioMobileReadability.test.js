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

test("portfolio ledger summary and empty state compact without losing their boundaries", () => {
  assert.match(source, /\.paper-portfolio-summary dl\s*\{[\s\S]*grid-template-columns:\s*repeat\(4, minmax\(0, 1fr\)\)/);
  assert.match(source, /\.paper-portfolio-empty\s*\{[\s\S]*grid-template-columns:\s*auto minmax\(0, 1fr\)/);
  const compact = source.slice(source.indexOf("@container paper-portfolio (max-width: 520px)"));
  assert.match(compact, /\.paper-portfolio-summary dl\s*\{\s*grid-template-columns:\s*repeat\(2, minmax\(0, 1fr\)\)/);
  assert.match(compact, /\.paper-portfolio-empty ul\s*\{\s*grid-column:\s*1 \/ -1/);
  assert.match(compact, /\.paper-portfolio-more\s*\{\s*justify-self:\s*stretch/);
  assert.match(source, /@media \(forced-colors: active\)[\s\S]*\.paper-portfolio-summary,[\s\S]*\.paper-portfolio-empty/);
  assert.equal((source.match(/\{/g) || []).length, (source.match(/\}/g) || []).length);
});
