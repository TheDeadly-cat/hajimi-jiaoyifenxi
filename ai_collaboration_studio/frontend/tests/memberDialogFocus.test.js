import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const dialogsSource = readFileSync(new URL("../src/components/Dialogs.jsx", import.meta.url), "utf8");

function memberDialogSource() {
  const start = dialogsSource.indexOf("export function MemberDialog");
  const end = dialogsSource.indexOf("\nfunction materialMode", start);
  assert.ok(start >= 0 && end > start, "MemberDialog source should be discoverable");
  return dialogsSource.slice(start, end);
}

test("member dialog activates focus ownership only after its initialized surface mounts", () => {
  const source = memberDialogSource();

  assert.match(source, /const memberDialogSurfaceOpen = Boolean\([\s\S]*open && form && initializedFormKey === memberInitializationKey/);
  assert.match(source, /useModalFocus\(\{[\s\S]*open: memberDialogSurfaceOpen,[\s\S]*containerRef: dialogRef,[\s\S]*initialFocusRef: closeButtonRef/);
  assert.match(source, /if \(!memberDialogSurfaceOpen\) return null/);
  assert.match(source, /ref=\{dialogRef\}[\s\S]*role="dialog"[\s\S]*aria-modal="true"[\s\S]*aria-label=\{isNew \? "添加 AI 成员" : "编辑 AI 身份"\}/);
  assert.match(source, /ref=\{closeButtonRef\}[\s\S]*aria-label="关闭成员设置"/);
  assert.doesNotMatch(source, /\bautoFocus\b/);
});

test("member save keeps one payload path and makes every dismiss route fail closed", () => {
  const source = memberDialogSource();

  assert.match(source, /const submitMember = async \(event\) => \{[\s\S]*if \(saving\) return;[\s\S]*await onSubmit\(form\)/);
  assert.equal((source.match(/onSubmit\(form\)/g) || []).length, 1);
  assert.match(source, /onClose: saving \? null : onClose/);
  assert.match(source, /memberDialogSurfaceOpen && saving[\s\S]*dialogRef\.current\?\.focus/);
  assert.match(source, /const closeMemberDialog = \(\) => \{[\s\S]*if \(!saving\) onClose\(\)/);
  assert.match(source, /event\.target === event\.currentTarget/);
  assert.match(source, /disabled=\{saving\}[\s\S]*onClick=\{closeMemberDialog\}/);
  assert.match(source, /disabled=\{archiveDisabled \|\| saving\}/);
  assert.match(source, /disabled=\{saving \|\| !providerAssigned\}/);
  assert.match(source, /aria-busy=\{saving\}[\s\S]*tabIndex=\{-1\}/);
});
