import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
const artifactPanelSource = readFileSync(
  new URL("../src/components/ArtifactPanel.jsx", import.meta.url),
  "utf8",
);
const decisionLineageSource = readFileSync(
  new URL("../src/components/DecisionLineagePanel.jsx", import.meta.url),
  "utf8",
);

test("artifact and paper portfolio dialogs restore their exact launch controls", () => {
  assert.match(appSource, /artifactRestoreFocusRef = useRef\(null\)/);
  assert.match(appSource, /paperPortfolioRestoreFocusRef = useRef\(null\)/);
  assert.match(
    appSource,
    /const openArtifact = \(artifact, launchTrigger = null\) => \{[\s\S]*artifactRestoreFocusRef\.current = launchTrigger \|\| document\.activeElement;[\s\S]*setEditingArtifact\(artifact\)/,
  );
  assert.match(
    appSource,
    /const openPaperPortfolio = \(portfolio = \{\}, launchTrigger = null\) => \{[\s\S]*paperPortfolioRestoreFocusRef\.current = launchTrigger \|\| document\.activeElement;[\s\S]*setEditingPaperPortfolio\(portfolio\)/,
  );
  assert.match(appSource, /<ArtifactDialog[\s\S]*restoreFocusRef=\{artifactRestoreFocusRef\}/);
  assert.match(appSource, /<PaperPortfolioDialog[\s\S]*restoreFocusRef=\{paperPortfolioRestoreFocusRef\}/);
});

test("all artifact and paper portfolio entry points forward the real trigger", () => {
  assert.match(
    artifactPanelSource,
    /onGenerate\(activeSynthesizerId, event\.currentTarget\)/,
  );
  assert.match(
    artifactPanelSource,
    /onEdit\(artifact, event\.currentTarget\)/,
  );
  assert.match(
    decisionLineageSource,
    /onCreatePortfolio\(decisionPackage, event\.currentTarget\)/,
  );
  assert.match(appSource, /onEditArtifact=\{openArtifact\}/);
  assert.match(appSource, /onAddPaperPortfolio=\{openPaperPortfolio\}/);
  assert.match(appSource, /onEditPaperPortfolio=\{openPaperPortfolio\}/);
});

