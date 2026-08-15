import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { preferredScrollBehavior } from "../src/motionPreferences.js";

const timelineSource = readFileSync(
  new URL("../src/components/ChatTimeline.jsx", import.meta.url),
  "utf8",
);
const evidenceSource = readFileSync(
  new URL("../src/components/ArtifactEvidenceReview.jsx", import.meta.url),
  "utf8",
);

test("programmatic navigation follows the user's reduced-motion preference", () => {
  const originalMatchMedia = globalThis.matchMedia;
  try {
    globalThis.matchMedia = () => ({ matches: true });
    assert.equal(preferredScrollBehavior(), "auto");
    globalThis.matchMedia = () => ({ matches: false });
    assert.equal(preferredScrollBehavior(), "smooth");
    delete globalThis.matchMedia;
    assert.equal(preferredScrollBehavior(), "smooth");
  } finally {
    if (originalMatchMedia === undefined) delete globalThis.matchMedia;
    else globalThis.matchMedia = originalMatchMedia;
  }
});

test("chat and artifact focus navigation use the shared preference helper", () => {
  assert.match(timelineSource, /behavior:\s*preferredScrollBehavior\(\)/);
  assert.match(evidenceSource, /behavior:\s*preferredScrollBehavior\(\)/);
  assert.doesNotMatch(timelineSource, /behavior:\s*"smooth"/);
  assert.doesNotMatch(evidenceSource, /behavior:\s*"smooth"/);
});
