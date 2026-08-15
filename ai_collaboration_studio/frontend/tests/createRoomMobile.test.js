import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const dialogsSource = readFileSync(new URL("../src/components/Dialogs.jsx", import.meta.url), "utf8");
const stylesSource = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

test("mobile create room uses a dedicated full-height shell with persistent controls", () => {
  assert.match(dialogsSource, /create-room-dialog-backdrop/);
  assert.match(dialogsSource, /className="dialog create-room-dialog"/);
  assert.match(dialogsSource, /className="create-room-footer"/);
  assert.match(stylesSource, /@media \(max-width: 760px\)[\s\S]*\.create-room-dialog-backdrop \{ padding: 0; \}/);
  assert.match(stylesSource, /\.dialog\.create-room-dialog \{[\s\S]*height: var\(--visual-viewport-height, 100dvh\);[\s\S]*scroll-padding-block:/);
  assert.match(stylesSource, /\.create-room-dialog > header \{[\s\S]*position: sticky;[\s\S]*top: 0;/);
  assert.match(stylesSource, /footer\.create-room-footer \{[\s\S]*position: sticky;[\s\S]*bottom: 0;/);
  assert.match(stylesSource, /padding-bottom: max\(14px, env\(safe-area-inset-bottom\)\)/);
  assert.match(stylesSource, /\.create-room-actions button \{ min-height: 44px;/);
});

test("create room keeps every fail-closed input and exposes a neutral live summary", () => {
  assert.match(dialogsSource, /const creationBlocked = lifecycleCreationBlocked \|\| stockScopeBlocked/);
  assert.match(dialogsSource, /onSubmit\(stockRoomFormSubmission\(form\)\)/);
  assert.equal((dialogsSource.match(/onSubmit\(stockRoomFormSubmission\(form\)\)/g) || []).length, 1);
  assert.match(dialogsSource, /disabled=\{creationBlocked\}/);
  assert.match(dialogsSource, /创建被阻断：能力包生命周期状态无法安全确认。/);
  assert.match(dialogsSource, /创建被阻断：请修正显式股票池；不会自动补全或扩展标的。/);
  assert.match(dialogsSource, /全部能力仅用于只读研究，不构成执行授权。/);
  assert.match(dialogsSource, /id="create-room-submit-status"[\s\S]*role="status"[\s\S]*aria-live="polite"[\s\S]*aria-atomic="true"/);
  assert.match(dialogsSource, /aria-invalid=\{!stockScopeState\.valid\}/);
  assert.match(dialogsSource, /create-stock-room-scope-help create-stock-room-scope-error/);
  assert.match(dialogsSource, /只绑定你明确输入的标的，不自动发现或扩展股票/);
});
