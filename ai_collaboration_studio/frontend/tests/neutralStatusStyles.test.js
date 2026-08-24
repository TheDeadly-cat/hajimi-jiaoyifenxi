import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
const capabilityStyles = readFileSync(
  new URL("../src/styles/capability-registry.css", import.meta.url),
  "utf8",
);

function rule(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = styles.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`));
  assert.ok(match, `missing CSS rule: ${selector}`);
  return match[1];
}

test("ordinary waiting and fail-closed states use neutral or attention tokens", () => {
  assert.match(rule(".convergence-badge"), /--state-neutral-/);
  assert.match(rule(".meeting-readiness.pending"), /--state-active-/);
  assert.match(rule(".meeting-readiness.blocked"), /--state-attention-/);
  assert.match(rule(".provider-warning"), /--state-attention-fg/);
  assert.match(rule(".round-provider-preview.warning"), /--state-attention-/);
  assert.match(rule(".composer-provider-summary.warning"), /--state-attention-/);
  assert.match(rule(".workflow-configuration-state.blocked"), /--state-attention-/);
  assert.match(rule(".provider-count-chip.openai"), /--state-attention-/);
  assert.match(rule(".openai-route-warning"), /--state-attention-/);
  assert.match(rule(".member-row.uses-openai"), /--state-attention-/);
  assert.match(rule(".member-row .member-provider-line.warning"), /--state-attention-/);
  assert.match(rule(".market-state.offline"), /--state-attention-/);
  assert.match(rule(".market-gate-reason"), /--state-attention-/);
  assert.match(rule(".industry-proxy-summary summary small.offline"), /--state-attention-/);
});

test("verified and user-recorded states do not imply execution authorization", () => {
  assert.match(rule(".convergence-badge.ready"), /--state-verified-/);
  assert.match(rule(".convergence-gate.user-decision-gate.support"), /--state-active-/);
  assert.doesNotMatch(rule(".convergence-gate.user-decision-gate.support"), /--green|--state-verified-/);
});

test("workflow states no longer reference undefined legacy variables and integrity red remains", () => {
  for (const name of ["--success", "--danger", "--line", "--panel"]) {
    assert.doesNotMatch(styles, new RegExp(`var\\(${name.replace("--", "--")}\\)`));
  }
  assert.match(capabilityStyles, /\.plugin-registry-snapshot\.integrity-failed\s*\{[\s\S]*?(?:#963e47|#fff7f7|#fff8f8)/);
  assert.match(styles, /\.action-desk-row\.integrity-failed\s*\{/);
  assert.match(rule(".meeting-readiness.critical"), /--state-critical-/);
  assert.match(rule(".market-gate-reason.critical"), /--state-critical-/);
});
