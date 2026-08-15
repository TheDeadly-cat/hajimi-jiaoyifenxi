import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const readStyles = (name) => readFileSync(new URL(`../src/styles/${name}.css`, import.meta.url), "utf8");

const workflowStyles = readStyles("workflow-policy");
const paperStyles = readStyles("paper-portfolio");
const traceStyles = readStyles("round-execution-trace");
const historyStyles = readStyles("member-version-history");
const overviewStyles = readStyles("action-overview");

test("mobile lazy dialog footers reserve the device bottom safe area", () => {
  assert.match(workflowStyles, /\.workflow-dialog > footer\s*\{[^}]*padding-bottom:\s*max\(14px, env\(safe-area-inset-bottom\)\)/s);
  assert.match(paperStyles, /\.paper-portfolio-dialog > footer\s*\{[^}]*padding-bottom:\s*max\(14px, env\(safe-area-inset-bottom\)\)/s);
  assert.match(traceStyles, /\.dialog\.round-trace-dialog > footer\s*\{\s*padding-bottom:\s*max\(11px, env\(safe-area-inset-bottom\)\)/s);
  assert.match(historyStyles, /\.dialog footer\.member-history-footer\s*\{\s*padding-bottom:\s*max\(14px, env\(safe-area-inset-bottom\)\)/s);
  assert.match(overviewStyles, /\.action-overview-boundary\s*\{[^}]*padding-bottom:\s*max\(11px, env\(safe-area-inset-bottom\)\)/s);
});

test("mobile lazy surfaces keep primary actions and disclosure summaries touch sized", () => {
  assert.match(workflowStyles, /\.coverage-selector summary,[\s\S]*\.workflow-dialog-footer button\s*\{\s*min-height:\s*44px/);
  assert.match(paperStyles, /\.paper-portfolio-dialog > footer button,[\s\S]*\.walk-forward-fold-audit > summary\s*\{\s*min-height:\s*44px/);
  assert.match(traceStyles, /\.discussion-audit-details > summary,[\s\S]*\.round-trace-event details > summary\s*\{[^}]*min-height:\s*44px/s);
  assert.match(traceStyles, /\.round-trace-more,[\s\S]*\.round-trace-dialog > footer button\s*\{\s*min-height:\s*44px/);
  assert.match(historyStyles, /\.member-history-footer button,[\s\S]*\.member-history-error button\s*\{\s*min-height:\s*44px/);
  assert.match(overviewStyles, /\.action-overview-filters select,[\s\S]*\.action-overview-item > footer > button\s*\{\s*min-height:\s*44px/);
});
