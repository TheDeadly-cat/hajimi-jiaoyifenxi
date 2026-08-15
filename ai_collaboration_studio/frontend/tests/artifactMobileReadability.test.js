import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const artifactStyles = readFileSync(new URL("../src/styles/artifact-dialog.css", import.meta.url), "utf8");
const mobile = artifactStyles.match(/@media \(max-width: 620px\) \{([\s\S]*)\}\s*$/)?.[1] || "";

test("mobile final-decision explanations remain readable without changing desktop density", () => {
  assert.match(mobile, /\.artifact-final-decision-heading small,[\s\S]*\.artifact-user-decision-action small,[\s\S]*font-size:\s*11px;[\s\S]*line-height:\s*1\.5;/);
  assert.match(mobile, /\.artifact-user-decision-current p,[\s\S]*\.artifact-user-decision-history article p[\s\S]*font-size:\s*12px;[\s\S]*line-height:\s*1\.55;/);
  assert.match(mobile, /\.artifact-user-decision-current strong,[\s\S]*\.artifact-user-decision-action strong[\s\S]*font-size:\s*12px;[\s\S]*line-height:\s*1\.4;/);
});
