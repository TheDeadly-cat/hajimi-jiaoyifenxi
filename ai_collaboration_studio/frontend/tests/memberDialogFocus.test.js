import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const dialogsSource = readFileSync(new URL("../src/components/Dialogs.jsx", import.meta.url), "utf8");
const memberStyles = readFileSync(new URL("../src/styles/member-dialog-refinement.css", import.meta.url), "utf8");

function memberDialogSource() {
  const start = dialogsSource.indexOf("export function MemberDialog");
  const end = dialogsSource.indexOf("\nfunction materialMode", start);
  assert.ok(start >= 0 && end > start, "MemberDialog source should be discoverable");
  return dialogsSource.slice(start, end);
}

test("member dialog activates focus ownership only after its initialized surface mounts", () => {
  const source = memberDialogSource();

  assert.match(source, /const memberDialogSurfaceOpen = Boolean\([\s\S]*open && form && initializedFormKey === memberInitializationKey/);
  assert.match(source, /useModalFocus\(\{[\s\S]*open: memberDialogSurfaceOpen,[\s\S]*containerRef: dialogRef,[\s\S]*initialFocusRef: member\?\.id \? closeButtonRef : nameInputRef/);
  assert.match(source, /if \(!memberDialogSurfaceOpen\) return null/);
  assert.match(source, /const dialogTitleId = useId\(\)/);
  assert.match(source, /ref=\{dialogRef\}[\s\S]*role="dialog"[\s\S]*aria-modal="true"[\s\S]*aria-labelledby=\{dialogTitleId\}/);
  assert.match(source, /<h2 id=\{dialogTitleId\}>\{isNew \? "添加 AI 成员" : "编辑 AI 身份"\}<\/h2>/);
  assert.match(source, /ref=\{closeButtonRef\}[\s\S]*aria-label="关闭成员设置"/);
  assert.match(source, /document\.addEventListener\("focusin", containDialogFocus, true\)/);
  assert.match(source, /dialog\.contains\(event\.target\)/);
  assert.doesNotMatch(source, /\bautoFocus\b/);
});

test("member dialog surfaces next-round contribution before long-form identity details", () => {
  const source = memberDialogSource();
  const contributionIndex = source.indexOf("member-workflow-config");
  const responsibilitiesIndex = source.indexOf("核心职责");

  assert.match(source, /className="member-dialog-ledger"/);
  assert.match(source, /NEXT ROUND CONTRIBUTION/);
  assert.match(source, /保存身份不等于下一轮已获启动授权/);
  assert.ok(contributionIndex >= 0 && contributionIndex < responsibilitiesIndex);
  assert.match(source, /className=\{`member-save-summary \$\{memberSaveTone\}`\}/);
  assert.match(source, /role="status" aria-live="polite"/);
  assert.match(memberStyles, /\.member-dialog-ledger\s*\{/);
  assert.match(memberStyles, /\.member-workflow-config\s*\{/);
  assert.match(memberStyles, /grid-template-columns:\s*auto minmax\(0, 1fr\) auto/);
});

test("member save keeps one payload path and makes every dismiss route fail closed", () => {
  const source = memberDialogSource();

  assert.match(source, /const submitUnavailable = typeof onSubmit !== "function"/);
  assert.match(source, /const saveBlocked = saving \|\| !providerAssigned \|\| submitUnavailable/);
  assert.match(source, /const runMemberAction = async \(kind, action\) => \{\s*if \(saving \|\| typeof action !== "function"\) return/);
  assert.match(source, /const submitMember = async \(event\) => \{[\s\S]*if \(saveBlocked\) return;[\s\S]*await runMemberAction\("save", \(\) => onSubmit\(form\)\)/);
  assert.equal((source.match(/onSubmit\(form\)/g) || []).length, 1);
  assert.match(source, /onClose: saving \? null : closeMemberDialog/);
  assert.match(source, /memberDialogSurfaceOpen && saving[\s\S]*dialogRef\.current\?\.focus/);
  assert.match(source, /const closeMemberDialog = \(\) => \{[\s\S]*if \(!saving\) onClose\?\.\(\)/);
  assert.match(source, /event\.target === event\.currentTarget/);
  assert.match(source, /disabled=\{saving\}[\s\S]*onClick=\{closeMemberDialog\}/);
  assert.match(source, /disabled=\{archiveDisabled \|\| saving \|\| typeof onDelete !== "function"\}/);
  assert.match(source, /type="submit" disabled=\{saveBlocked\}/);
  assert.match(source, /aria-busy=\{saving\}[\s\S]*tabIndex=\{-1\}/);
});

test("member option lists keep duplicate labels and ids on unambiguous React keys", () => {
  const source = memberDialogSource();

  assert.match(source, /<optgroup key=\{JSON\.stringify\(\["member-template-group", group\.label, groupIndex\]\)\}/);
  assert.match(source, /<option key=\{JSON\.stringify\(\["member-template", template\.id, templateIndex\]\)\}/);
  assert.match(source, /key=\{JSON\.stringify\(\["workflow-stage", stage, index\]\)\}/);
  assert.doesNotMatch(source, /key=\{(?:group\.label|template\.id|stage)\}/);
});
