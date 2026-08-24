import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  roomSettingsInitialState,
  roomSettingsPackSelection,
  roomSettingsSaveControl,
  roomSettingsVersionHistory,
  sameRoomPackSelection,
} from "../src/roomSettingsUi.js";

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("room settings rejects string and duplicate pack collections", () => {
  assert.equal(roomSettingsPackSelection("pack-a").integrityOk, false);
  assert.deepEqual(roomSettingsPackSelection("pack-a").ids, []);
  const duplicate = roomSettingsPackSelection(["pack-a", "pack-a"]);
  assert.equal(duplicate.integrityOk, false);
  assert.deepEqual(duplicate.ids, ["pack-a"]);
  assert.equal(sameRoomPackSelection(["pack-a"], ["pack-a", "pack-a"]), false);
});

test("room settings source projection contains malformed room data", () => {
  const state = roomSettingsInitialState({
    id: "room-a",
    settings_version: 4,
    title: "研究室",
    objective: "整理证据",
    capability_pack_ids: "pack-a",
  });
  assert.equal(state.integrityOk, false);
  assert.deepEqual(state.form.capability_pack_ids, []);
  assert.equal(state.form.discussion_mode, "dynamic");
});

test("room settings save permit fails closed on source and handler gaps", () => {
  const base = {
    sourceIntegrityOk: true,
    form: {
      title: "研究室",
      objective: "整理证据",
      category: "通用共创",
      discussion_mode: "dynamic",
      idle_response_mode: "mention_only",
      capability_pack_ids: [],
    },
    room: { id: "room-a", settings_version: 4 },
    busy: false,
    packSelectionBlocked: false,
    stockScopeBlocked: false,
    moderatorSelectionMissing: false,
    submitHandlerAvailable: true,
    closeHandlerAvailable: true,
  };
  assert.equal(roomSettingsSaveControl(base).canSubmit, true);
  assert.equal(roomSettingsSaveControl({ ...base, sourceIntegrityOk: false }).canSubmit, false);
  assert.equal(roomSettingsSaveControl({ ...base, submitHandlerAvailable: false }).canSubmit, false);
  assert.equal(roomSettingsSaveControl({ ...base, moderatorSelectionMissing: true }).canSubmit, false);
});

test("room settings history drops invalid and duplicate versions", () => {
  const history = roomSettingsVersionHistory({
    versions: [{ version: 3 }, { version: 3 }, { version: 0 }, { version: 2 }],
  });
  assert.equal(history.integrityOk, false);
  assert.deepEqual(history.rows.map((row) => row.version), [3, 2]);
  assert.equal(roomSettingsVersionHistory({}).integrityOk, false);
});

test("room settings dialog owns its responsive and save-epoch contracts", () => {
  const component = fs.readFileSync(path.join(frontendRoot, "src/components/RoomSettingsDialog.jsx"), "utf8");
  const ownedCss = fs.readFileSync(path.join(frontendRoot, "src/styles/room-settings.css"), "utf8");
  const globalCss = fs.readFileSync(path.join(frontendRoot, "src/styles.css"), "utf8");

  assert.match(component, /styles\/room-settings\.css/);
  assert.match(component, /saveRequestRef/);
  assert.match(component, /aria-busy=\{busy\}/);
  assert.match(component, /data-save-state=\{saveControl\.phase\}/);
  assert.match(component, /<h2 id=\{dialogTitleId\}>房间设置<\/h2>/);
  assert.match(component, /document\.addEventListener\("focusin", containDialogFocus, true\)/);
  assert.match(component, /dialog\.contains\(event\.target\)/);
  assert.match(component, /<fieldset className="room-settings-editable" disabled=\{busy\}>/);
  assert.match(component, /pendingPackIds=\{form\.capability_pack_ids\}/);
  assert.match(component, /disabled=\{!availability\.canToggle\}/);
  assert.doesNotMatch(globalCss, /(?:^|\n)\.room-settings-dialog\s*\{/);
  assert.doesNotMatch(globalCss, /(?:^|\n)\.room-settings-freeze-note\s*\{/);
  assert.match(ownedCss, /env\(safe-area-inset-top\)/);
  assert.match(ownedCss, /@container \(max-width: 460px\)/);
  assert.match(ownedCss, /prefers-reduced-motion: reduce/);
});

test("room settings separates versioned next-round impact from save permission", () => {
  const component = fs.readFileSync(path.join(frontendRoot, "src/components/RoomSettingsDialog.jsx"), "utf8");
  const ownedCss = fs.readFileSync(path.join(frontendRoot, "src/styles/room-settings.css"), "utf8");

  assert.match(component, /NEXT ROUND BOUNDARY/);
  assert.match(component, /当前冻结轮次保持不变/);
  assert.match(component, /不会回写历史消息、已冻结流程、证据或执行路由/);
  assert.match(component, /能力包选择/);
  assert.match(component, /股票池合同/);
  assert.match(component, /主持选择/);
  assert.match(component, /className=\{`room-settings-save-summary \$\{settingsSaveTone\}`\}/);
  assert.match(component, /role="status" aria-live="polite"/);
  assert.match(ownedCss, /\.room-settings-impact\s*\{/);
  assert.match(ownedCss, /\.room-settings-impact-grid\s*\{/);
  assert.match(ownedCss, /\.room-settings-save-summary\s*\{/);
  assert.match(ownedCss, /grid-template-columns:\s*minmax\(0, 1fr\) auto/);
});
