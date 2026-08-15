import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
const sidebarSource = readFileSync(
  new URL("../src/components/RoomSidebar.jsx", import.meta.url),
  "utf8",
);
const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

test("mobile room sidebar is a real focus-owned dialog while desktop stays a sidebar", () => {
  assert.match(sidebarSource, /useModalFocus\(\{[\s\S]*open: mobileModal && mobileOpen/);
  assert.match(sidebarSource, /role=\{mobileModal && mobileOpen \? "dialog" : undefined\}/);
  assert.match(sidebarSource, /aria-modal=\{mobileModal && mobileOpen \? "true" : undefined\}/);
  assert.match(sidebarSource, /inert=\{mobileModal && !mobileOpen \? "" : undefined\}/);
  assert.match(sidebarSource, /initialFocusRef: closeButtonRef/);
  assert.match(sidebarSource, /restoreFallbackRef: restoreFocusRef/);
});

test("App separates ordinary close restoration from modal transitions and room selection", () => {
  assert.match(appSource, /MOBILE_ROOM_DRAWER_QUERY = "\(max-width: 760px\)"/);
  assert.match(appSource, /roomDrawerRestoreFocusRef = useRef\(null\)/);
  assert.match(appSource, /mobileModal=\{mobileRoomDrawer\}/);
  assert.match(appSource, /restoreFocusRef=\{roomDrawerRestoreFocusRef\}/);
  assert.match(appSource, /roomDrawerRestoreFocusRef\.current = null;[\s\S]*setRoomDrawerOpen\(false\);[\s\S]*setActionOverviewOpen\(true\)/);
  assert.match(appSource, /loadRoom\(targetRoomId\)\.then[\s\S]*roundLaunchSuccessFocusRef\.current\?\.focus/);
  assert.match(appSource, /\(mobileRoomDrawer && roomDrawerOpen\)[\s\S]*\|\| \(compactInspector && inspectorOpen\)/);
});

test("closed compact inspector is absent from the modal accessibility tree", () => {
  assert.match(appSource, /role=\{compactInspector && inspectorOpen \? "dialog" : undefined\}/);
  assert.match(appSource, /aria-modal=\{compactInspector && inspectorOpen \? "true" : undefined\}/);
  assert.match(appSource, /aria-hidden=\{compactInspector && !inspectorOpen \? "true" : undefined\}/);
  assert.match(appSource, /inert=\{compactInspector && !inspectorOpen \? "" : undefined\}/);
});

test("sidebar keeps searchable room semantics and a restrained workspace hierarchy", () => {
  assert.match(sidebarSource, /aria-label="搜索房间"/);
  assert.match(sidebarSource, /aria-current=\{room\.id === activeRoomId \? "page" : undefined\}/);
  assert.match(sidebarSource, /协作空间/);
  assert.match(sidebarSource, /\{visibleRoomCount\} 个可见/);
  assert.match(styles, /\.sidebar-brand > span small/);
  assert.match(styles, /\.sidebar-section-label small/);
});
