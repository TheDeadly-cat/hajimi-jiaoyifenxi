import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const componentUrl = new URL(
  "../src/components/ArtifactCandidateGovernance.jsx",
  import.meta.url,
);
const stylesheetUrl = new URL(
  "../src/styles/candidate-governance-refinement.css",
  import.meta.url,
);

test("candidate governance pagination preserves modal focus on readable progress", async () => {
  const [componentSource, stylesheetSource] = await Promise.all([
    readFile(componentUrl, "utf8"),
    readFile(stylesheetUrl, "utf8"),
  ]);

  assert.equal(
    componentSource.match(
      /onClickCapture=\{focusGovernanceProgressAfterRender\}/g,
    )?.length,
    2,
  );
  assert.equal(componentSource.match(/tabIndex=\{-1\}/g)?.length, 2);
  assert.match(componentSource, /progress\?\.focus\(\{ preventScroll: true \}\)/);
  assert.match(
    stylesheetSource,
    /\.artifact-governance-list-status progress:focus-visible/,
  );
});
