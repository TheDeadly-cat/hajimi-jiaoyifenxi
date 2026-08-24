import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  artifactEditorIdentity,
  artifactEditorMutationControl,
  artifactEditorSavedState,
  artifactEditorSourceState,
} from "../src/artifactEditorUi.js";

test("artifact editor source projection contains malformed collections and unsafe text", () => {
  const source = artifactEditorSourceState({
    id: "artifact-a",
    version: 3,
    title: { unsafe: true },
    content: {
      summary: { unsafe: true },
      conclusions: "not-an-array",
      decision: [],
    },
  });
  assert.equal(source.integrityOk, false);
  assert.equal(source.artifact.title, "会议纪要");
  assert.equal(source.artifact.content.summary, "");
  assert.deepEqual(source.artifact.content.conclusions, []);
  assert.deepEqual(source.artifact.content.decision, {});
});

test("artifact editor mutation permits separate save, confirm, and export handlers", () => {
  const identity = artifactEditorIdentity(
    { id: "artifact-a", version: 3 },
    { id: "room-a" },
  );
  const base = {
    identity,
    title: "会议纪要",
    summary: "证据摘要",
    busy: false,
    inFlight: false,
    evidenceBlocked: false,
    confirmDisabledReason: "",
    saveHandlerAvailable: true,
    confirmHandlerAvailable: true,
    exportHandlerAvailable: true,
  };
  assert.equal(artifactEditorMutationControl({ ...base, action: "draft" }).canRun, true);
  assert.equal(artifactEditorMutationControl({ ...base, action: "confirm", confirmHandlerAvailable: false }).canRun, false);
  assert.equal(artifactEditorMutationControl({ ...base, action: "export", exportHandlerAvailable: false }).canRun, false);
  assert.equal(artifactEditorMutationControl({ ...base, action: "draft", inFlight: true }).canRun, false);
});

test("artifact editor rejects mismatched and regressed save responses", () => {
  const current = { id: "artifact-a", version: 3 };
  assert.equal(artifactEditorSavedState({ id: "artifact-b", version: 4 }, current).ok, false);
  assert.equal(artifactEditorSavedState({ id: "artifact-a", version: 2 }, current).ok, false);
  assert.equal(artifactEditorSavedState({ id: "artifact-a", version: 4, content: {} }, current).ok, true);
});

test("artifact dialog owns request epochs, busy fieldset, operation ledger, and source repair notice", () => {
  const source = readFileSync(new URL("../src/components/ArtifactDialog.jsx", import.meta.url), "utf8");
  const styles = readFileSync(new URL("../src/styles/artifact-dialog.css", import.meta.url), "utf8");
  assert.match(source, /mutationRequestRef/);
  assert.match(source, /mutationInFlightRef/);
  assert.match(source, /data-mutation-state=\{mutationAction \|\| "idle"\}/);
  assert.match(source, /<fieldset className="artifact-editor-fields" disabled=\{busy\}>/);
  assert.match(source, /artifact-mutation-ledger/);
  assert.match(source, /artifactEditorSavedState/);
  assert.match(styles, /\.artifact-mutation-ledger\s*\{/);
  assert.match(styles, /env\(safe-area-inset-top\)/);
  assert.match(styles, /prefers-reduced-motion: reduce/);
});
