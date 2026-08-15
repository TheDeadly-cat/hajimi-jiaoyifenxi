import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
const deferredFallbackSource = readFileSync(
  new URL("../src/DeferredSurfaceFallback.js", import.meta.url),
  "utf8",
);
const hostStyles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
const footballPanelSource = readFileSync(
  new URL("../src/components/FootballResearchPanel.jsx", import.meta.url),
  "utf8",
);
const footballStyles = readFileSync(
  new URL("../src/styles/football-research.css", import.meta.url),
  "utf8",
);
const stockPanelSource = readFileSync(
  new URL("../src/components/StockResearchPanel.jsx", import.meta.url),
  "utf8",
);
const stockStyles = readFileSync(
  new URL("../src/styles/stock-research.css", import.meta.url),
  "utf8",
);
const roomInspectorSource = readFileSync(
  new URL("../src/components/RoomInspector.jsx", import.meta.url),
  "utf8",
);
const actionOverviewSource = readFileSync(
  new URL("../src/components/ActionOverviewDrawer.jsx", import.meta.url),
  "utf8",
);
const actionOverviewStyles = readFileSync(
  new URL("../src/styles/action-overview.css", import.meta.url),
  "utf8",
);
const roundExecutionTraceSource = readFileSync(
  new URL("../src/components/RoundExecutionTraceDialog.jsx", import.meta.url),
  "utf8",
);
const roundExecutionTraceStyles = readFileSync(
  new URL("../src/styles/round-execution-trace.css", import.meta.url),
  "utf8",
);
const paperPortfolioSource = readFileSync(
  new URL("../src/components/PaperPortfolioPanel.jsx", import.meta.url),
  "utf8",
);
const paperPortfolioStyles = readFileSync(
  new URL("../src/styles/paper-portfolio.css", import.meta.url),
  "utf8",
);
const artifactDialogSource = readFileSync(
  new URL("../src/components/ArtifactDialog.jsx", import.meta.url),
  "utf8",
);
const artifactDialogStyles = readFileSync(
  new URL("../src/styles/artifact-dialog.css", import.meta.url),
  "utf8",
);
const observationPanelSource = readFileSync(
  new URL("../src/components/ObservationPanel.jsx", import.meta.url),
  "utf8",
);
const observationStyles = readFileSync(
  new URL("../src/styles/observation.css", import.meta.url),
  "utf8",
);
const reflectionDialogSource = readFileSync(
  new URL("../src/components/ReflectionDialog.jsx", import.meta.url),
  "utf8",
);
const reflectionStyles = readFileSync(
  new URL("../src/styles/reflection-dialog.css", import.meta.url),
  "utf8",
);
const iconRailSource = readFileSync(
  new URL("../src/components/IconRail.jsx", import.meta.url),
  "utf8",
);

test("chat, sidebar and composer stay eager while heavy host surfaces use direct lazy imports", () => {
  assert.match(appSource, /import \{ ChatTimeline \} from "\.\/components\/ChatTimeline"/);
  assert.match(appSource, /import \{ Composer \} from "\.\/components\/Composer"/);
  assert.match(appSource, /import \{ RoomSidebar \} from "\.\/components\/RoomSidebar"/);
  assert.match(appSource, /import \{ IconRail \} from "\.\/components\/IconRail"/);

  for (const component of [
    "ActionOverviewDrawer",
    "ArtifactDialog",
    "FootballResearchPanel",
    "PaperPortfolioDialog",
    "RoundExecutionTraceDialog",
    "StockResearchPanel",
  ]) {
    assert.match(appSource, new RegExp(`const ${component} = lazy\\(\\(\\) => import\\(`));
    assert.doesNotMatch(appSource, new RegExp(`import \\{ ${component} \\} from`));
  }
  assert.match(appSource, /function loadRoomInspector\(\)[\s\S]*import\("\.\/components\/RoomInspector\.jsx"\)/);
  assert.match(appSource, /const RoomInspector = lazy\(loadRoomInspector\)/);
  assert.match(appSource, /onFocus=\{preloadRoomInspector\}[\s\S]*onPointerDown=\{preloadRoomInspector\}[\s\S]*onPointerEnter=\{preloadRoomInspector\}/);
  assert.match(appSource, /onPreloadInspector=\{preloadRoomInspector\}/);
  assert.equal((iconRailSource.match(/section === "rooms" \? undefined : onPreloadInspector/g) || []).length, 3);
  assert.doesNotMatch(appSource, /import\("\.\/components(?:\/index)?"\)/);
});

test("deferred surfaces mount on first open and remain mounted to preserve local state", () => {
  assert.match(appSource, /function useDeferredActivation\(active\)/);
  assert.match(appSource, /if \(active\) setActivated\(true\)/);
  assert.match(appSource, /return Boolean\(active\) \|\| activated/);
  assert.match(appSource, /const inspectorActivated = useDeferredActivation\(inspectorOpen\)/);
  assert.match(appSource, /const artifactActivated = useDeferredActivation\(Boolean\(editingArtifact\)\)/);
  assert.match(appSource, /const paperPortfolioActivated = useDeferredActivation\(Boolean\(editingPaperPortfolio\)\)/);
  assert.match(appSource, /\{inspectorActivated \? <Suspense/);
  assert.match(appSource, /\{artifactActivated \? <Suspense/);
  assert.match(appSource, /\{paperPortfolioActivated \? <Suspense/);
});

test("every lazy host surface has an explicit Suspense fallback", () => {
  assert.match(appSource, /import \{ Suspense, lazy,/);
  assert.match(appSource, /import \{ DeferredSurfaceFallback \} from "\.\/DeferredSurfaceFallback\.js"/);
  assert.match(deferredFallbackSource, /export function DeferredSurfaceFallback/);
  const lazyCount = (appSource.match(/= lazy\(/g) || []).length;
  const suspenseCount = (appSource.match(/<Suspense fallback=/g) || []).length;
  assert.equal(lazyCount, 16);
  assert.equal(suspenseCount, 16);
});

test("football and stock panel CSS follows the corresponding lazy module", () => {
  assert.match(footballPanelSource, /import "\.\.\/styles\/football-research\.css";/);
  assert.match(stockPanelSource, /import "\.\.\/styles\/stock-research\.css";/);

  assert.match(footballStyles, /\.football-research-panel\s*\{/);
  assert.match(footballStyles, /\.football-safety-boundary\s*\{/);
  assert.doesNotMatch(hostStyles, /\.football-research-panel\s*\{/);

  assert.match(stockStyles, /\.stock-research-panel\s*\{/);
  assert.match(stockStyles, /\.stock-symbol-card\s*\{/);
  assert.doesNotMatch(hostStyles, /\.stock-research-panel\s*\{/);
  assert.doesNotMatch(hostStyles, /\.stock-symbol-card\s*\{/);

  // Room settings and host contribution status can render before the stock
  // inspector activates, so their shared styles intentionally remain eager.
  assert.match(hostStyles, /\.stock-room-scope-field\s*\{/);
  assert.match(hostStyles, /\.stock-research-contribution-status\s*\{/);
});

test("action overview drawer CSS follows its lazy module while the eager entry stays styled", () => {
  assert.match(actionOverviewSource, /import "\.\.\/styles\/action-overview\.css";/);
  assert.match(actionOverviewStyles, /\.action-overview-drawer\s*\{/);
  assert.match(actionOverviewStyles, /@media \(max-width: 520px\)/);
  assert.doesNotMatch(hostStyles, /\.action-overview-drawer\s*\{/);
  assert.doesNotMatch(hostStyles, /\.action-overview-scrim\s*\{/);
  assert.match(hostStyles, /\.action-overview-entry\s*\{/);
});

test("execution trace and discussion audit CSS follows the lazy dialog while its inspector summary stays styled", () => {
  assert.match(roundExecutionTraceSource, /import "\.\.\/styles\/round-execution-trace\.css";/);
  assert.match(roundExecutionTraceStyles, /\.dialog\.round-trace-dialog\s*\{/);
  assert.match(roundExecutionTraceStyles, /\.discussion-audit\s*\{/);
  assert.match(roundExecutionTraceStyles, /@media \(max-width: 760px\)/);
  assert.match(roundExecutionTraceStyles, /@media \(max-width: 620px\)/);
  assert.doesNotMatch(hostStyles, /\.dialog\.round-trace-dialog\s*\{/);
  assert.doesNotMatch(hostStyles, /\.discussion-audit\s*\{/);
  assert.match(hostStyles, /\.round-trace-summary\s*\{/);
});

test("storage-only inspector panels stay nested lazy while their CSS follows each module", () => {
  assert.match(paperPortfolioSource, /import "\.\.\/styles\/paper-portfolio\.css";/);
  assert.doesNotMatch(roomInspectorSource, /import \{ PaperPortfolioPanel \} from "\.\/PaperPortfolioPanel";/);
  assert.doesNotMatch(roomInspectorSource, /import \{ ObservationPanel \} from "\.\/ObservationPanel";/);
  assert.match(
    roomInspectorSource,
    /const PaperPortfolioPanel = lazy\(\(\) => import\("\.\/PaperPortfolioPanel\.jsx"\)/,
  );
  assert.match(
    roomInspectorSource,
    /const ObservationPanel = lazy\(\(\) => import\("\.\/ObservationPanel\.jsx"\)/,
  );
  assert.match(roomInspectorSource, /<Suspense fallback=\{<InspectorPanelFallback label="模拟组合与风险预算" \/>\}>/);
  assert.match(roomInspectorSource, /<Suspense fallback=\{<InspectorPanelFallback label="模拟观察与验证" \/>\}>/);

  assert.match(paperPortfolioStyles, /\.paper-portfolio-panel\s*\{/);
  assert.match(paperPortfolioStyles, /\.paper-portfolio-dialog\s*\{/);
  assert.match(paperPortfolioStyles, /\.paper-position-editor\s*\{/);
  assert.match(paperPortfolioStyles, /@media \(max-width: 760px\)/);
  assert.match(paperPortfolioStyles, /@media \(max-width: 620px\)/);
  assert.doesNotMatch(hostStyles, /\.paper-portfolio-panel\s*\{/);
  assert.doesNotMatch(hostStyles, /\.paper-portfolio-dialog\s*\{/);
  assert.doesNotMatch(hostStyles, /\.paper-position-editor\s*\{/);

  // ObservationPanel is also a direct lazy entry and reuses these two form
  // primitives, so they cannot safely move into the portfolio-only module.
  assert.match(observationPanelSource, /className="paper-dialog-lineage-source active"/);
  assert.match(observationPanelSource, /className="paper-derivation-note"/);
  assert.match(hostStyles, /\.paper-dialog-lineage-source\s*\{/);
  assert.match(hostStyles, /\.paper-derivation-note\s*\{/);
  assert.doesNotMatch(paperPortfolioStyles, /\.paper-dialog-lineage-source\s*\{/);
  assert.doesNotMatch(paperPortfolioStyles, /\.paper-derivation-note\s*\{/);
});

test("observation and reflection CSS follows their lazy modules while shared dialog primitives stay eager", () => {
  assert.match(observationPanelSource, /import "\.\.\/styles\/observation\.css";/);
  assert.match(reflectionDialogSource, /import "\.\.\/styles\/reflection-dialog\.css";/);

  for (const selector of [
    "observation-score",
    "calibration-details",
    "observation-row",
    "observation-dialog",
    "observation-evidence",
  ]) {
    assert.match(observationStyles, new RegExp(`\\.${selector}(?:\\.|\\s|\\{)`));
    assert.doesNotMatch(hostStyles, new RegExp(`\\.${selector}(?:\\.|\\s|\\{)`));
  }
  assert.match(observationStyles, /@media \(max-width: 760px\)[\s\S]*\.observation-grid-three,[\s\S]*\.observation-evidence/);

  for (const selector of ["reflection-dialog", "reflection-status", "reflection-source", "reflection-dialog-footer"]) {
    assert.match(reflectionStyles, new RegExp(`\\.${selector}(?:\\.|\\s|\\{)`));
    assert.doesNotMatch(hostStyles, new RegExp(`\\.${selector}(?:\\.|\\s|\\{)`));
  }

  for (const selector of ["dialog-backdrop", "material-safety-note", "material-local-error", "form-grid"]) {
    assert.match(hostStyles, new RegExp(`\\.${selector}(?:\\s|\\{)`));
    assert.doesNotMatch(`${observationStyles}\n${reflectionStyles}`, new RegExp(`\\.${selector}(?:\\s|\\{)`));
  }
});

test("artifact workspace CSS follows its lazy dialog while eager host entries and shared evidence stay styled", () => {
  assert.match(artifactDialogSource, /import "\.\.\/styles\/artifact-dialog\.css";/);

  for (const selector of [
    "dialog\\.artifact-dialog",
    "artifact-version-history",
    "artifact-item-title",
    "candidate-experiment-panel",
    "project-readiness-panel",
    "artifact-final-decision",
  ]) {
    assert.match(artifactDialogStyles, new RegExp(`\\.${selector}\\s*\\{`));
    assert.doesNotMatch(hostStyles, new RegExp(`\\.${selector}\\s*\\{`));
  }

  // These surfaces can render before the ArtifactDialog chunk activates, or
  // are host-owned reusable evidence primitives, so they intentionally stay eager.
  for (const selector of [
    "artifact-panel",
    "artifact-row",
    "artifact-evidence-graph",
    "project-round-focus-card",
    "plugin-action-boundary",
  ]) {
    assert.match(hostStyles, new RegExp(`\\.${selector}(?:\\s*,|\\s*\\{)`));
    assert.doesNotMatch(artifactDialogStyles, new RegExp(`\\.${selector}(?:\\s*,|\\s*\\{)`));
  }

  assert.match(artifactDialogStyles, /@media \(max-width: 620px\)[\s\S]*\.artifact-item-title\s*\{[\s\S]*min-height:\s*44px;[\s\S]*font-size:\s*12px;[\s\S]*line-height:\s*1\.4;[\s\S]*white-space:\s*normal;/);
  assert.match(artifactDialogStyles, /@media \(max-width: 620px\)[\s\S]*\.artifact-item-remove\s*\{\s*width:\s*44px;\s*height:\s*44px;\s*\}/);
});

test("mobile inspector heading precedes every lazy domain panel", () => {
  const wrapIndex = appSource.indexOf('id="room-inspector-drawer"');
  const headingIndex = appSource.indexOf('<div className="inspector-mobile-head">', wrapIndex);
  const footballIndex = appSource.indexOf("<FootballResearchPanel", wrapIndex);
  const stockIndex = appSource.indexOf("<StockResearchPanel", wrapIndex);

  assert.ok(wrapIndex >= 0);
  assert.ok(headingIndex > wrapIndex);
  assert.ok(footballIndex > headingIndex);
  assert.ok(stockIndex > headingIndex);
  assert.doesNotMatch(roomInspectorSource, /inspector-mobile-head/);
});
