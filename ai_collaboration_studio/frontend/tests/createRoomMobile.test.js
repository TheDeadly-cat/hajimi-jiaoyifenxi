import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const dialogsSource = readFileSync(new URL("../src/components/Dialogs.jsx", import.meta.url), "utf8");
const stylesSource = [
  readFileSync(new URL("../src/styles.css", import.meta.url), "utf8"),
  readFileSync(new URL("../src/styles/create-room-dialog-refinement.css", import.meta.url), "utf8"),
  readFileSync(new URL("../src/styles/create-room-review-summary.css", import.meta.url), "utf8"),
].join("\n");

test("mobile create room uses a dedicated full-height shell with persistent controls", () => {
  assert.match(dialogsSource, /create-room-dialog-backdrop/);
  assert.match(dialogsSource, /className="dialog create-room-dialog create-room-dialog-v2"/);
  assert.match(dialogsSource, /className="create-room-footer"/);
  assert.match(stylesSource, /@media \(max-width: 760px\)[\s\S]*\.create-room-dialog-backdrop \{ padding: 0; \}/);
  assert.match(stylesSource, /\.dialog\.create-room-dialog \{[\s\S]*height: var\(--visual-viewport-height, 100dvh\);[\s\S]*scroll-padding-block:/);
  assert.match(stylesSource, /\.create-room-dialog > header \{[\s\S]*position: sticky;[\s\S]*top: 0;/);
  assert.match(stylesSource, /footer\.create-room-footer \{[\s\S]*position: sticky;[\s\S]*bottom: 0;/);
  assert.match(stylesSource, /padding-bottom: max\(14px, env\(safe-area-inset-bottom\)\)/);
  assert.match(stylesSource, /\.create-room-actions button \{ min-height: 44px;/);
});

test("create room groups implementation detail behind a persistent preflight summary", () => {
  assert.match(dialogsSource, /className="create-room-heading"/);
  assert.match(dialogsSource, /<h2 id=\{dialogTitleId\}>新建群聊房间<\/h2>/);
  assert.match(stylesSource, /\.create-room-dialog-v2 > \.create-room-heading > span\s*\{[^}]*display: grid[^}]*grid-template-columns: minmax\(0, 1fr\)/s);
  assert.match(dialogsSource, /const templateReviewRegionId = useId\(\)/);
  assert.match(dialogsSource, /aria-controls=\{templateReviewRegionId\}/);
  assert.match(dialogsSource, /aria-expanded=\{templateReviewExpanded\}/);
  assert.match(dialogsSource, /hidden=\{!templateReviewExpanded\}/);
  assert.match(dialogsSource, /CREATE PERMIT \/ LOCAL DRAFT/);
  assert.match(dialogsSource, /const creationReviewTone = submitError \|\| creationBlocked \|\| submitUnavailable/);
  assert.match(dialogsSource, /const creationReviewLabel = submitError\s*\? "创建请求需要处理"/);
  assert.match(dialogsSource, /创建房间配置与启动正式讨论轮是两个独立确认步骤/);
  assert.match(dialogsSource, /createRoomListKey\("core-protocol", pack\.id, pack\.pack_version, pack\.manifest_sha256\)/);
  assert.match(dialogsSource, /createRoomListKey\("optional-domain-pack", pack\.id, pack\.pack_version, pack\.manifest_sha256\)/);
  assert.doesNotMatch(dialogsSource, /\["room-template", template\.id, index\]/);
  assert.match(stylesSource, /\.create-room-template-review-detail\[hidden\]\s*\{\s*display: none/);
  assert.match(stylesSource, /@container create-room-dialog \(max-width: 560px\)/);
});

test("create room keeps every fail-closed input and exposes a neutral live summary", () => {
  assert.match(dialogsSource, /const creationBlocked = lifecycleCreationBlocked \|\| stockScopeBlocked/);
  assert.match(dialogsSource, /const payload = stockRoomFormSubmission\(form\);\s*await onSubmit\(payload\)/);
  assert.equal((dialogsSource.match(/stockRoomFormSubmission\(form\)/g) || []).length, 1);
  assert.match(dialogsSource, /const submitBlocked = creationBlocked \|\| submitUnavailable \|\| submitting/);
  assert.match(dialogsSource, /disabled=\{submitBlocked\}/);
  assert.match(dialogsSource, /创建被阻断：能力包生命周期状态无法安全确认。/);
  assert.match(dialogsSource, /创建被阻断：请修正显式股票池；不会自动补全或扩展标的。/);
  assert.match(dialogsSource, /全部能力仅用于只读研究，不构成执行授权。/);
  assert.match(dialogsSource, /const submitStatusId = useId\(\)/);
  assert.match(dialogsSource, /aria-describedby=\{submitStatusId\}/);
  assert.match(dialogsSource, /id=\{submitStatusId\}[\s\S]*role=\{submitError \? "alert" : "status"\}[\s\S]*aria-live="polite"[\s\S]*aria-atomic="true"/);
  assert.match(dialogsSource, /aria-invalid=\{!stockScopeState\.valid\}/);
  assert.match(dialogsSource, /const stockScopeHelpId = useId\(\)/);
  assert.match(dialogsSource, /const stockScopeErrorId = useId\(\)/);
  assert.match(dialogsSource, /stockScopeHelpId \+ " " \+ stockScopeErrorId/);
  assert.match(dialogsSource, /只绑定你明确输入的标的，不自动发现或扩展股票/);
});
