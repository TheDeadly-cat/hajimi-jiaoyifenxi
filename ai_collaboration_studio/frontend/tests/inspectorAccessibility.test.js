import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");

test("compact room inspector exposes one labelled modal drawer", () => {
  assert.match(appSource, /const COMPACT_INSPECTOR_QUERY = "\(max-width: 1180px\)"/);
  assert.match(appSource, /aria-controls="room-inspector-drawer"/);
  assert.match(appSource, /id="room-inspector-drawer"/);
  assert.match(appSource, /role=\{compactInspector && inspectorOpen \? "dialog" : undefined\}/);
  assert.match(appSource, /aria-modal=\{compactInspector && inspectorOpen \? "true" : undefined\}/);
  assert.match(appSource, /aria-hidden=\{compactInspector && !inspectorOpen \? "true" : undefined\}/);
  assert.match(appSource, /inert=\{compactInspector && !inspectorOpen \? "" : undefined\}/);
  assert.match(appSource, /aria-label=\{compactInspector && inspectorOpen \? "房间信息" : undefined\}/);
});

test("compact room inspector owns initial focus and traps both tab directions", () => {
  assert.match(appSource, /inspectorCloseRef\.current\?\.focus\(\{ preventScroll: true \}\)/);
  assert.match(appSource, /if \(event\.key !== "Tab"\) return/);
  assert.match(appSource, /inspector\.querySelectorAll\(INSPECTOR_FOCUSABLE_SELECTOR\)/);
  assert.match(appSource, /event\.shiftKey \? last : first/);
  assert.match(appSource, /event\.shiftKey && activeElement === first/);
  assert.match(appSource, /!event\.shiftKey && activeElement === last/);
  assert.match(appSource, /window\.removeEventListener\("keydown", trapFocus\)/);
});

test("closing the room inspector restores its trigger or an explicit post-close target", () => {
  assert.match(appSource, /ref=\{inspectorToggleRef\}/);
  assert.match(appSource, /ref=\{inspectorCloseRef\}/);
  assert.match(appSource, /inspectorWasOpenRef\.current = false/);
  assert.match(appSource, /const inspectorPostCloseFocusRef = useRef\(null\)/);
  assert.match(appSource, /const postCloseFocusTarget = inspectorPostCloseFocusRef\.current/);
  assert.match(appSource, /inspectorPostCloseFocusRef\.current = null/);
  assert.match(appSource, /inspectorRestoreFocusRef\.current/);
  assert.match(appSource, /target\.getClientRects\(\)\.length > 0/);
  assert.match(appSource, /restoreTarget\?\.focus\(\{ preventScroll: restoreTarget !== postCloseFocusTarget \}\)/);
});
