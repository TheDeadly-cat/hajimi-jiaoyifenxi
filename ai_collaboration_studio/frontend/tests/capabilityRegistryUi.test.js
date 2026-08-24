import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  capabilityRegistryPackPresentation,
  capabilityRegistrySelectionState,
  capabilityRegistrySnapshotPresentation,
} from "../src/capabilityRegistryUi.js";

test("registry selection comparison rejects malformed and duplicate values", () => {
  assert.equal(capabilityRegistrySelectionState(["core", "research"], ["research", "core"]).changed, false);
  assert.equal(capabilityRegistrySelectionState(["core"], ["core", "research"]).changed, true);

  const malformed = capabilityRegistrySelectionState(["core"], "core");
  assert.equal(malformed.integrityOk, false);
  assert.equal(malformed.changed, true);
  assert.match(malformed.issue, /数组/);

  const duplicate = capabilityRegistrySelectionState(["core"], ["core", "core"]);
  assert.equal(duplicate.integrityOk, false);
  assert.match(duplicate.issue, /重复/);
});

test("registry presentation keeps frozen trust separate from current lifecycle availability", () => {
  const pack = { id: "research", name: "研究", version: "1.0.0", systemManaged: false };
  const unverified = capabilityRegistryPackPresentation(pack, null, { lifecycleIntegrityOk: false });
  const frozenOnly = capabilityRegistrySnapshotPresentation({
    view: { integrityOk: true, adapters: [], contributions: [] },
    lifecycleIntegrityOk: false,
    packRows: [unverified],
  });
  assert.equal(unverified.runtimeAvailable, false);
  assert.equal(frozenOnly.trustState, "sealed-frozen-only");
  assert.equal(frozenOnly.stats.currentReady, null);

  const ready = capabilityRegistryPackPresentation(pack, {
    runtimeAvailable: true,
    runtimeState: "ready",
  }, { lifecycleIntegrityOk: true });
  const current = capabilityRegistrySnapshotPresentation({
    view: { integrityOk: true, adapters: [{}], contributions: [{}, {}] },
    lifecycleIntegrityOk: true,
    packRows: [ready],
  });
  assert.equal(current.trustState, "sealed-current");
  assert.deepEqual(Object.values(current.stats), [1, 1, 2, 1]);
});

test("registry snapshot owns its styles and accessible source contracts", () => {
  const componentSource = readFileSync(new URL("../src/components/CapabilityRegistrySnapshot.jsx", import.meta.url), "utf8");
  const ownedCss = readFileSync(new URL("../src/styles/capability-registry.css", import.meta.url), "utf8");
  const refinementCss = readFileSync(new URL("../src/styles/capability-registry-refinement.css", import.meta.url), "utf8");
  const globalCss = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
  assert.match(componentSource, /styles\/capability-registry\.css/);
  assert.match(componentSource, /styles\/capability-registry-refinement\.css/);
  assert.match(componentSource, /const titleId = useId\(\)/);
  assert.match(componentSource, /aria-labelledby=\{titleId\}/);
  assert.match(componentSource, /<h4 id=\{titleId\}>当前房间插件合同<\/h4>/);
  assert.match(componentSource, /data-trust-state/);
  assert.doesNotMatch(globalCss, /\.plugin-registry-snapshot/);
  assert.match(ownedCss, /container-name: capability-registry/);
  assert.match(ownedCss, /container-type: inline-size/);
  assert.match(ownedCss, /@container capability-registry \(max-width: 420px\)/);
  assert.match(ownedCss, /prefers-reduced-motion/);
  assert.match(refinementCss, /\.plugin-registry-audit-control\s*\{/);
  assert.match(refinementCss, /@container capability-registry \(max-width: 420px\)/);
});

test("registry exact bindings use progressive disclosure and full identity keys", () => {
  const componentSource = readFileSync(new URL("../src/components/CapabilityRegistrySnapshot.jsx", import.meta.url), "utf8");
  const refinementCss = readFileSync(new URL("../src/styles/capability-registry-refinement.css", import.meta.url), "utf8");

  assert.match(componentSource, /const detailRegionId = useId\(\)/);
  assert.match(componentSource, /setDetailsExpanded\(false\)/);
  assert.match(componentSource, /\}, \[view\.hash\]\)/);
  assert.match(componentSource, /aria-controls=\{detailRegionId\}/);
  assert.match(componentSource, /aria-expanded=\{detailsExpanded\}/);
  assert.match(componentSource, /hidden=\{!detailsExpanded\}/);
  assert.match(componentSource, /exactRegistryKey\("capability_pack", pack\.id, pack\.version, pack\.manifestHash\)/);
  assert.match(componentSource, /exactRegistryKey\("domain_adapter", adapter\.id, adapter\.version, adapter\.contractHash\)/);
  assert.match(componentSource, /exactRegistryKey\("domain_port", adapter\.id, adapter\.version, port\.id, port\.version, port\.contractHash\)/);
  assert.match(componentSource, /exactRegistryKey\("ui_contribution", contribution\.id, contribution\.version, contribution\.contractHash, contribution\.slotId\)/);
  assert.match(refinementCss, /\.plugin-registry-exact-bindings\[hidden\]\s*\{\s*display: none/);
});
