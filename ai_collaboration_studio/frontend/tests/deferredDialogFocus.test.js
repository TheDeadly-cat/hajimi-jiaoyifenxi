import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
const deferredFallbackSource = readFileSync(
  new URL("../src/DeferredSurfaceFallback.js", import.meta.url),
  "utf8",
);

test("deferred dialog fallback is a real top-level modal while its chunk loads", () => {
  const source = deferredFallbackSource;

  assert.match(appSource, /import \{ DeferredSurfaceFallback \} from "\.\/DeferredSurfaceFallback\.js"/);
  assert.match(source, /import \{ useModalFocus \} from "\.\/useModalFocus\.js"/);
  assert.match(source, /const ownsModalFocus = dialog && typeof onClose === "function"/);
  assert.match(source, /useModalFocus\(\{[\s\S]*open: ownsModalFocus && open/);
  assert.match(source, /containerRef: fallbackRef/);
  assert.match(source, /initialFocusRef: onClose \? closeButtonRef : fallbackRef/);
  assert.match(source, /restoreFallbackRef: restoreFocusRef/);
  assert.match(source, /onClose: dialog \? onClose : null/);
  assert.match(source, /role: "dialog"[\s\S]*"aria-modal": "true"[\s\S]*"aria-busy": "true"[\s\S]*tabIndex: -1/);
  assert.match(source, /ref: closeButtonRef[\s\S]*"取消加载"/);
  assert.match(source, /if \(ownsModalFocus && !open\) return null/);
  assert.match(source, /if \(!ownsModalFocus\)[\s\S]*deferred-dialog-backdrop/);
});

test("formal round lazy fallback can abort read-only planning and restore its exact trigger", () => {
  assert.match(appSource, /label="启动确认"[\s\S]*dialog[\s\S]*open=\{roundLaunchOpen\}[\s\S]*onClose=\{discardRoundLaunch\}[\s\S]*restoreFocusRef=\{roundLaunchRestoreFocusRef\}/);
  assert.match(appSource, /launchControl\.controller\?\.abort\(\)/);
  assert.match(appSource, /if \(streamControl\?\.transition === "plan"\) streamControl\.transition = ""/);
  assert.doesNotMatch(deferredFallbackSource, /onConfirm|streamRound|Provider/);
});

test("a successful formal start restores focus to the visible conversation status instead of a disabled trigger", () => {
  assert.match(appSource, /const roundLaunchSuccessFocusRef = useRef\(null\)/);
  assert.match(appSource, /activeRoomIdRef\.current === targetRoomId[\s\S]*roundLaunchRestoreFocusRef\.current = roundLaunchSuccessFocusRef\.current[\s\S]*setRoundLaunch\(emptyRoundLaunchState\(\)\)/);
  assert.match(appSource, /ref=\{roundLaunchSuccessFocusRef\}[\s\S]*className="conversation-header"[\s\S]*aria-label=\{`\$\{room\?\.title \|\| "AI 共创室"\}讨论状态：\$\{roundStatusLabel\}`\}[\s\S]*tabIndex=\{-1\}/);
});
