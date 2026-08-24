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
const navigationSource = readFileSync(
  new URL("../src/inspectorTargetNavigation.js", import.meta.url),
  "utf8",
);
const refinementStyles = readFileSync(
  new URL("../src/styles/room-inspector-refinement.css", import.meta.url),
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
  assert.match(
    inspectorSource,
    /const DecisionLineagePanel = lazy\(\(\) => import\("\.\/DecisionLineagePanel\.jsx"\)/,
  );
  assert.match(
    inspectorSource,
    /const MarketSnapshotCard = lazy\(\(\) => import\("\.\/MarketSnapshotCard\.jsx"\)/,
  );
  assert.match(
    inspectorSource,
    /const StorageSampleAcceptanceCard = lazy\(\(\) => import\("\.\/StorageSampleAcceptanceCard\.jsx"\)/,
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
  assert.match(inspectorSource, /bindInspectorTargetNavigation\(inspectorRef\.current, scrollTargetId\)/);
  assert.match(navigationSource, /if \(!resolvedTarget\) return/);
  assert.match(navigationSource, /new ResizeObserverClass\(align\)/);
  assert.match(navigationSource, /new MutationObserverClass\(\(\) => \{/);
  assert.match(
    navigationSource,
    /mutationObserver\.observe\(inspector, \{ childList: true, subtree: true \}\)/,
  );
  assert.match(navigationSource, /observeChildren\(\);\s*align\(\)/);
  assert.match(navigationSource, /inspector\.scrollTop \+= offset/);
  assert.match(navigationSource, /resolvedTarget\.focus\(\{ preventScroll: true \}\)/);
  assert.match(navigationSource, /setTimer\(stop, INSPECTOR_TARGET_NAVIGATION_LIFETIME_MS\)/);
  assert.match(navigationSource, /cancelFrame\(animationFrame\)/);
  assert.match(navigationSource, /clearTimer\(lifetimeTimer\)/);
  assert.match(navigationSource, /resizeObserver\?\.disconnect\(\)/);
  assert.match(navigationSource, /mutationObserver\?\.disconnect\(\)/);
  assert.match(navigationSource, /return stop/);
});

test("the loading fallback stays explicit and accessible without mounting a DOM harness", () => {
  assert.match(
    inspectorSource,
    /function InspectorPanelFallback\(\{ label \}\) \{[\s\S]*aria-busy="true" aria-label=\{label\}/,
  );
  assert.match(inspectorSource, /className="inspector-section inspector-panel-loading"/);
  assert.match(inspectorSource, /正在载入只读研究记录/);
});

test("the inspector source keeps bounded projections, action permits, and responsive integrity UI", () => {
  assert.match(inspectorSource, /buildRoomInspectorListProjection/);
  assert.match(inspectorSource, /buildRoomInspectorProviderIndex/);
  assert.match(inspectorSource, /buildRoomInspectorArtifactFingerprint/);
  assert.match(inspectorSource, /roundActionRef/);
  assert.match(inspectorSource, /runRoomAction/);
  assert.match(inspectorSource, /room-inspector-integrity-ledger/);
  assert.match(inspectorSource, /typeof onStartRound !== "function"/);
  assert.match(refinementStyles, /env\(safe-area-inset-bottom\)/);
  assert.match(refinementStyles, /@media \(max-width: 430px\)/);
  assert.match(refinementStyles, /@media \(forced-colors: active\)/);
});
