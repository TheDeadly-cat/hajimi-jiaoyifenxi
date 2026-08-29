import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const hostStyles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
const workflowStyles = readFileSync(new URL("../src/styles/workflow-policy.css", import.meta.url), "utf8");
const reflectionStyles = readFileSync(new URL("../src/styles/reflection-dialog.css", import.meta.url), "utf8");

function normalizeNewlines(value) {
  return value.replace(/\r\n?/g, "\n");
}

function rule(selector, source = hostStyles) {
  const normalizedSelector = normalizeNewlines(selector);
  const normalizedSource = normalizeNewlines(source);
  const escaped = normalizedSelector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return normalizedSource.match(new RegExp(`(?:^|})\\s*${escaped}\\s*\\{([^}]*)\\}`))?.[1] || "";
}

test("CSS rule lookup is stable across Windows checkout line endings", () => {
  const windowsStyles = ".first,\r\n.second {\r\n  font-size: 10px;\r\n}\r\n";

  assert.match(rule(".first,\n.second", windowsStyles), /font-size:\s*10px/);
});

test("provider identity and preflight evidence remain readable without ellipsis-only disclosure", () => {
  assert.match(rule(".provider-count-chip > span > small"), /font-size:\s*10px/);
  assert.match(rule(".provider-count-chip > span > small"), /overflow-wrap:\s*anywhere/);
  assert.doesNotMatch(rule(".provider-count-chip > span > small"), /text-overflow|white-space:\s*nowrap/);
  assert.match(rule(".provider-catalog-copy small"), /font-size:\s*10px/);
  assert.match(rule(".provider-config-state"), /font-size:\s*10px/);
  assert.match(rule(".provider-preflight-route small"), /font-size:\s*10px/);
  assert.match(rule(".provider-preflight-route small"), /white-space:\s*normal/);
});

test("launch readiness and governance boundary text use an evidence-readable scale", () => {
  assert.match(rule(".meeting-readiness-row"), /font-size:\s*11px/);
  assert.match(rule(".meeting-readiness-row strong"), /font-size:\s*11px/);
  assert.match(rule(".meeting-readiness-reason"), /font-size:\s*10px/);
  assert.match(rule(".convergence-gate small"), /font-size:\s*10px/);
  assert.match(rule(".convergence-next,\n.convergence-boundary"), /font-size:\s*10px/);
  assert.match(rule(".workflow-summary-facts dd"), /overflow-wrap:\s*anywhere/);
  assert.doesNotMatch(rule(".workflow-summary-facts dd"), /text-overflow|white-space:\s*nowrap/);
});

test("mobile disclosure and workflow controls keep 44px targets", () => {
  assert.match(hostStyles, /@media \(max-width: 760px\)[\s\S]*\.convergence-details > summary,[\s\S]*\.market-refresh,[\s\S]*\.market-evidence summary,[\s\S]*\.workflow-summary-section \.text-action[\s\S]*min-height:\s*44px/);
  assert.match(workflowStyles, /@media \(max-width: 760px\)[\s\S]*\.workflow-stage-movers button[\s\S]*width:\s*44px;[\s\S]*height:\s*44px/);
  assert.match(workflowStyles, /@media \(max-width: 760px\)[\s\S]*\.dialog \.choice-chip[\s\S]*min-height:\s*44px/);
  assert.match(reflectionStyles, /@media \(max-width: 620px\)[\s\S]*\.reflection-dialog-footer > span[\s\S]*flex-direction:\s*column/);
  assert.match(reflectionStyles, /\.reflection-dialog-footer button[\s\S]*min-height:\s*44px/);
});
