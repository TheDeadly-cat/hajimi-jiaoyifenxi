import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  pluginLifecycleCatalogPresentation,
  pluginLifecycleReviewControl,
  pluginLifecycleTargetKey,
} from "../src/pluginLifecycleUi.js";

const exactTarget = {
  kind: "capability_pack",
  id: "research_pack",
  version: "2.1.0",
  targetSha256: "a".repeat(64),
  label: "研究能力包",
};

test("tombstone permit remains fail-closed until every explicit confirmation is complete", () => {
  const review = { action: "tombstone", busy: false, preview: { integrityOk: true }, target: exactTarget };
  const incomplete = pluginLifecycleReviewControl({ review, reason: "永久停止这个精确版本的新绑定", historyConfirmed: true, migrationConfirmed: true, tombstoneConfirmation: "研究能力" });
  assert.equal(incomplete.canSubmit, false);
  assert.equal(incomplete.tombstoneRequired, true);
  assert.equal(incomplete.permitChecks.at(-1).passed, false);

  const complete = pluginLifecycleReviewControl({ review, reason: "永久停止这个精确版本的新绑定", historyConfirmed: true, migrationConfirmed: true, tombstoneConfirmation: exactTarget.label });
  assert.equal(complete.canSubmit, true);
  assert.equal(complete.phase, "ready");
  assert.equal(complete.permitChecks.every((check) => check.passed), true);
});

test("catalog presentation and target keys remain deterministic", () => {
  const presentation = pluginLifecycleCatalogPresentation({
    capabilityPacks: [
      { lifecycle: { runtimeAvailable: true, systemManaged: false, availableActions: ["disable"] } },
      { lifecycle: { runtimeAvailable: false, systemManaged: false, availableActions: ["enable"] } },
      { lifecycle: { runtimeAvailable: true, systemManaged: true, availableActions: [] } },
    ],
  });
  assert.deepEqual(Object.values(presentation), [3, 2, 1, 2, 1]);
  assert.equal(pluginLifecycleTargetKey(exactTarget), pluginLifecycleTargetKey({ ...exactTarget }));
  assert.notEqual(pluginLifecycleTargetKey(exactTarget), pluginLifecycleTargetKey({ ...exactTarget, version: "2.1.1" }));
});

test("lifecycle source contracts retain secure IDs, request epochs, busy semantics, and responsive controls", () => {
  const domainSource = readFileSync(new URL("../src/pluginLifecycle.js", import.meta.url), "utf8");
  const componentSource = readFileSync(new URL("../src/components/CapabilityPackLifecyclePanel.jsx", import.meta.url), "utf8");
  const cssSource = readFileSync(new URL("../src/styles/plugin-lifecycle.css", import.meta.url), "utf8");
  assert.doesNotMatch(domainSource, /Math\.random/);
  assert.match(domainSource, /crypto\?\.randomUUID/);
  assert.match(domainSource, /影响预览与生命周期精确目标不一致/);
  assert.match(componentSource, /requestRef\.current\.sequence/);
  assert.match(componentSource, /aria-busy=\{review\.busy\}/);
  assert.match(componentSource, /PERMIT CHECK/);
  assert.match(cssSource, /container-name: plugin-lifecycle/);
  assert.match(cssSource, /container-type: inline-size/);
  assert.match(cssSource, /@container plugin-lifecycle \(max-width: 760px\)/);
  assert.match(cssSource, /@container plugin-lifecycle \(max-width: 440px\)/);
  assert.match(cssSource, /prefers-reduced-motion/);
});
