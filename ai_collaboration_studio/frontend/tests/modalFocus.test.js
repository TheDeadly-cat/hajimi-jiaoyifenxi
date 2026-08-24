import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { modalFocusIndex } from "../src/useModalFocus.js";

const hookSource = readFileSync(new URL("../src/useModalFocus.js", import.meta.url), "utf8");
const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
const dialogsSource = readFileSync(new URL("../src/components/Dialogs.jsx", import.meta.url), "utf8");
const settingsSource = readFileSync(new URL("../src/components/RoomSettingsDialog.jsx", import.meta.url), "utf8");
const overviewSource = readFileSync(new URL("../src/components/ActionOverviewDrawer.jsx", import.meta.url), "utf8");
const roundLaunchSource = readFileSync(new URL("../src/components/RoundLaunchDialog.jsx", import.meta.url), "utf8");
const composerSource = readFileSync(new URL("../src/components/Composer.jsx", import.meta.url), "utf8");
const inspectorSource = readFileSync(new URL("../src/components/RoomInspector.jsx", import.meta.url), "utf8");
const iconRailSource = readFileSync(new URL("../src/components/IconRail.jsx", import.meta.url), "utf8");

test("modal focus index cycles at both ends and enters from outside", () => {
  assert.equal(modalFocusIndex(4, -1, false), 0);
  assert.equal(modalFocusIndex(4, -1, true), 3);
  assert.equal(modalFocusIndex(4, 0, true), 3);
  assert.equal(modalFocusIndex(4, 3, false), 0);
  assert.equal(modalFocusIndex(4, 1, false), -1);
  assert.equal(modalFocusIndex(0, -1, false), -1);
});

test("modal hook owns initial focus, escape, trapping, cleanup and restoration", () => {
  assert.match(hookSource, /restoreTargetRef/);
  assert.match(hookSource, /visibleFocusTarget/);
  assert.match(hookSource, /restoreFallbackRef\?\.current/);
  assert.match(hookSource, /visibleFocusTarget\(capturedTarget\)[\s\S]*\? capturedTarget[\s\S]*: fallbackTarget/);
  assert.match(hookSource, /getComputedStyle\?\.\(target\)/);
  assert.match(hookSource, /style\.visibility !== "hidden"/);
  assert.match(hookSource, /target\.disabled === true/);
  assert.match(hookSource, /activeElement !== document\.body/);
  assert.match(hookSource, /restoreTarget\.focus\(\{ preventScroll: true \}\)/);
  assert.match(hookSource, /visibleFocusTarget\(preferredTarget\) \? preferredTarget : controls\[0\]/);
  assert.match(hookSource, /event\.key === "Escape"/);
  assert.match(hookSource, /if \(typeof closeRef\.current === "function"\) closeRef\.current\(\)/);
  assert.match(hookSource, /event\.key !== "Tab"/);
  assert.match(hookSource, /event\.stopPropagation\(\)/);
  assert.match(hookSource, /activeIndex \+ \(event\.shiftKey \? -1 : 1\)/);
  assert.match(hookSource, /document\.addEventListener\("keydown", handleKeyDown, true\)/);
  assert.match(hookSource, /document\.removeEventListener\("keydown", handleKeyDown, true\)/);
});

test("four primary modal surfaces use the shared focus contract", () => {
  for (const source of [dialogsSource, settingsSource, overviewSource, roundLaunchSource]) {
    assert.match(source, /import \{ useModalFocus \} from "\.\.\/useModalFocus"/);
    assert.match(source, /useModalFocus\(\{/);
    assert.match(source, /ref=\{[a-zA-Z]+Ref\}/);
  }
  assert.match(dialogsSource, /aria-label="关闭新建房间"/);
  assert.match(dialogsSource, /open: open && initializedForOpen\.current/);
  assert.doesNotMatch(dialogsSource, /ref=\{titleInputRef\} autoFocus/);
  assert.match(settingsSource, /const canClose = typeof onClose === "function"/);
  assert.match(settingsSource, /const requestClose = \(\) => \{\s*if \(busy \|\| !canClose\) return/);
  assert.match(settingsSource, /onClose: busy \|\| !canClose \? null : requestClose/);
  assert.match(settingsSource, /disabled=\{busy \|\| !canClose\}/);
  assert.doesNotMatch(settingsSource, /ref=\{titleInputRef\} autoFocus/);
  assert.match(overviewSource, /tabIndex=\{-1\}/);
  assert.match(roundLaunchSource, /initialFocusRef: closeButtonRef/);
  assert.match(roundLaunchSource, /restoreFallbackRef: restoreFocusRef/);
  assert.match(roundLaunchSource, /onClose: effectiveBusy \? null : onClose/);
  assert.match(roundLaunchSource, /ref=\{closeButtonRef\}/);
  assert.match(roundLaunchSource, /tabIndex=\{-1\}/);
  assert.match(roundLaunchSource, /open && effectiveBusy/);
  assert.doesNotMatch(roundLaunchSource, /\bautoFocus\b/);
  assert.doesNotMatch(roundLaunchSource, /window\.addEventListener\("keydown"/);
  assert.match(roundLaunchSource, /event\.target === event\.currentTarget && !effectiveBusy/);
  assert.match(roundLaunchSource, /disabled=\{effectiveBusy\}/);
  assert.match(appSource, /ref=\{mobileRoomToggleRef\}/);
  assert.match(appSource, /const roundLaunchRestoreFocusRef = useRef\(null\)/);
  assert.match(appSource, /const startRound = async \(launchTrigger\)/);
  assert.match(appSource, /launchTrigger \|\| document\.activeElement/);
  assert.match(appSource, /activeLaunchTrigger !== document\.body/);
  assert.match(appSource, /restoreFocusRef=\{roundLaunchRestoreFocusRef\}/);
  assert.match(composerSource, /onStartRound\?\.\(event\.currentTarget\)/);
  assert.match(
    inspectorSource,
    /runRoomAction\([\s\S]*"start",[\s\S]*onStartRound,[\s\S]*event\.currentTarget/,
  );
  assert.match(inspectorSource, /handler\(\.\.\.args\)/);
  assert.match(appSource, /roundLaunchOpenRef\.current = roundLaunchOpen/);
  assert.ok((appSource.match(/if \(roundLaunchOpenRef\.current\) return/g) || []).length >= 2);
  assert.equal((appSource.match(/restoreFocusRef=\{mobileRoomToggleRef\}/g) || []).length, 2);
});

test("mobile inspector restores focus to the actual rail trigger", () => {
  assert.match(iconRailSource, /onNavigate\(section, event\.currentTarget\)/);
  assert.match(appSource, /const inspectorRestoreFocusRef = useRef\(null\)/);
  assert.match(appSource, /navigateRail = \(section, navigationTrigger = null\)/);
  assert.match(appSource, /inspectorRestoreFocusRef\.current = navigationTrigger \|\| document\.activeElement/);
  assert.match(appSource, /inspectorRestoreFocusRef\.current,[\s\S]*inspectorToggleRef\.current,[\s\S]*mobileRoomToggleRef\.current/);
  assert.match(appSource, /target\.getClientRects\(\)\.length > 0/);
  assert.match(appSource, /toggleInspector = \(event\)/);
});
