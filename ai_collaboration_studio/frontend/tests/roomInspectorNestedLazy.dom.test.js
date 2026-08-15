import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// This regression used to mount the complete RoomInspector through a Vite SSR
// server, JSDOM, React Suspense and deliberately unresolved lazy imports. On
// Windows/Node 24 that harness could grow one test worker beyond 70 GiB. The
// product contracts involved are source-level boundaries already exercised by
// focused component tests, so keep this file deterministic and side-effect free.
const inspectorSource = readFileSync(
  new URL("../src/components/RoomInspector.jsx", import.meta.url),
  "utf8",
);

test("storage-only panels remain lazy, host-owned, and read-only at the Suspense boundary", () => {
  assert.match(
    inspectorSource,
    /const paperPortfolioVisible = Boolean\(\s*storageContribution\?\.present\s*\|\| \(paperPortfolios \|\| \[\]\)\.length\s*\|\| walkForwardHistoryExists\s*\|\| candidateComparison,\s*\)/s,
  );
  assert.match(
    inspectorSource,
    /const observationsVisible = Boolean\(\s*storageContribution\?\.present\s*\|\| \(observations \|\| \[\]\)\.length\s*\|\| \(reflections \|\| \[\]\)\.length,\s*\)/s,
  );
  assert.match(
    inspectorSource,
    /const PaperPortfolioPanel = lazy\(\(\) => import\("\.\/PaperPortfolioPanel\.jsx"\)/,
  );
  assert.match(
    inspectorSource,
    /const ObservationPanel = lazy\(\(\) => import\("\.\/ObservationPanel\.jsx"\)/,
  );
  assert.doesNotMatch(
    inspectorSource,
    /import \{\s*PaperPortfolioPanel\s*\} from "\.\/PaperPortfolioPanel(?:\.jsx)?"/,
  );
  assert.doesNotMatch(
    inspectorSource,
    /import \{\s*ObservationPanel\s*\} from "\.\/ObservationPanel(?:\.jsx)?"/,
  );
  assert.match(
    inspectorSource,
    /\{paperPortfolioVisible \? \(\s*<PluginActionBoundary disabled=\{storageReadOnly\} label="模拟组合与风险预算">\s*<Suspense fallback=\{<InspectorPanelFallback label="模拟组合与风险预算" \/>\}>/s,
  );
  assert.match(
    inspectorSource,
    /\{observationsVisible \? \(\s*<PluginActionBoundary disabled=\{storageReadOnly\} label="模拟观察与验证">\s*<Suspense fallback=\{<InspectorPanelFallback label="模拟观察与验证" \/>\}>/s,
  );
  assert.match(inspectorSource, /id="inspector-paper-portfolio" tabIndex=\{-1\}/);
  assert.match(inspectorSource, /id="inspector-observations" tabIndex=\{-1\}/);
});

test("late lazy targets keep bounded observer-based navigation and complete cleanup", () => {
  assert.match(inspectorSource, /querySelector\(`#\$\{scrollTargetId\}`\)/);
  assert.match(inspectorSource, /if \(!resolvedTarget\) return/);
  assert.match(inspectorSource, /new globalThis\.ResizeObserver\(align\)/);
  assert.match(inspectorSource, /new globalThis\.MutationObserver\(align\)/);
  assert.match(
    inspectorSource,
    /mutationObserver\.observe\(inspector, \{ childList: true, subtree: true \}\)/,
  );
  assert.match(inspectorSource, /inspector\.scrollTop \+= targetTop - inspectorTop/);
  assert.match(inspectorSource, /resolvedTarget\.focus\(\{ preventScroll: true \}\)/);
  assert.match(inspectorSource, /lifetimeTimer = globalThis\.setTimeout\(stop, 4000\)/);
  assert.match(inspectorSource, /globalThis\.cancelAnimationFrame\?\.\(animationFrame\)/);
  assert.match(inspectorSource, /globalThis\.clearTimeout\(lifetimeTimer\)/);
  assert.match(inspectorSource, /observer\?\.disconnect\(\)/);
  assert.match(inspectorSource, /mutationObserver\?\.disconnect\(\)/);
  assert.match(inspectorSource, /return stop/);
});

test("the loading fallback stays explicit and accessible without mounting a DOM harness", () => {
  assert.match(
    inspectorSource,
    /function InspectorPanelFallback\(\{ label \}\) \{[\s\S]*aria-busy="true" aria-label=\{label\}/,
  );
  assert.match(inspectorSource, /className="inspector-section inspector-panel-loading"/);
  assert.match(inspectorSource, /正在载入只读研究记录/);
});
