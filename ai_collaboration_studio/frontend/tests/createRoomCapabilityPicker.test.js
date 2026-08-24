import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const testDirectory = path.dirname(fileURLToPath(import.meta.url));
const dialogsSource = readFileSync(
  path.resolve(testDirectory, "../src/components/Dialogs.jsx"),
  "utf8",
);
const pickerStyles = readFileSync(
  path.resolve(testDirectory, "../src/styles/create-room-capability-picker.css"),
  "utf8",
);

test("create room keeps selected optional packs mounted behind an explicit catalog disclosure", () => {
  assert.match(dialogsSource, /const \[capabilityPickerExpanded, setCapabilityPickerExpanded\] = useState\(false\)/);
  assert.match(dialogsSource, /const optionalPackPreviewLimit = 2/);
  assert.match(dialogsSource, /\.filter\(\(pack\) => form\.capability_pack_ids\.includes\(pack\.id\)\)/);
  assert.match(dialogsSource, /optionalPackPreviewLimit - selectedOptionalPackIds\.size/);
  assert.match(dialogsSource, /\.\.\.selectedOptionalPackIds/);
  assert.match(dialogsSource, /const visibleOptionalDomainPacks = capabilityPickerExpanded/);
  assert.match(dialogsSource, /visibleOptionalDomainPacks\.map\(\(pack\) =>/);
  assert.match(dialogsSource, /aria-controls=\{capabilityPackListId\}/);
  assert.match(dialogsSource, /aria-expanded=\{capabilityPickerExpanded\}/);
  assert.match(dialogsSource, /另有 \{hiddenOptionalPackCount\} 项未挂载/);
  assert.match(dialogsSource, /setCapabilityPickerExpanded\(false\)/);
  assert.match(pickerStyles, /grid-template-columns: minmax\(0, 1fr\) auto/);
  assert.match(pickerStyles, /@container \(max-width: 560px\)/);
  assert.match(pickerStyles, /min-height: 44px/);
});
