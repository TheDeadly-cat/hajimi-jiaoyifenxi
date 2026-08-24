import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const dialogsSource = readFileSync(new URL("../src/components/Dialogs.jsx", import.meta.url), "utf8");

function officialPreviewSource() {
  const start = dialogsSource.indexOf("function OfficialAttestationPreview");
  const end = dialogsSource.indexOf("\nexport function MaterialDialog", start);
  assert.ok(start >= 0 && end > start, "OfficialAttestationPreview source should be discoverable");
  return dialogsSource.slice(start, end);
}

function materialDialogSource() {
  const start = dialogsSource.indexOf("export function MaterialDialog");
  assert.ok(start >= 0, "MaterialDialog source should be discoverable");
  return dialogsSource.slice(start);
}

test("material focus ownership follows the real editor or attestation surface without closing the modal lifecycle", () => {
  const source = materialDialogSource();
  const preview = officialPreviewSource();

  assert.match(source, /useLayoutEffect\(\(\) => \{[\s\S]*initializedMaterialRef\.current === materialKey[\s\S]*setInitializedMaterialKey\(materialKey\)/);
  assert.match(source, /const materialSurfaceOpen = Boolean\(open && form && initializedMaterialKey === materialKey\)/);
  assert.match(source, /const attestationSurfaceOpen = materialSurfaceOpen && Boolean\(officialAttestation\)/);
  assert.match(source, /const activeDialogRef = attestationSurfaceOpen \? attestationDialogRef : editorDialogRef/);
  assert.match(source, /const activeCloseButtonRef = attestationSurfaceOpen \? attestationCloseButtonRef : editorCloseButtonRef/);
  assert.match(source, /useModalFocus\(\{[\s\S]*open: materialSurfaceOpen,[\s\S]*containerRef: activeDialogRef,[\s\S]*initialFocusRef: activeCloseButtonRef/);
  assert.match(source, /dialogRef=\{attestationDialogRef\}[\s\S]*closeButtonRef=\{attestationCloseButtonRef\}/);
  assert.match(source, /ref=\{editorDialogRef\}[\s\S]*role="dialog"[\s\S]*aria-modal="true"[\s\S]*tabIndex=\{-1\}/);
  assert.match(preview, /ref=\{dialogRef\}[\s\S]*role="dialog"[\s\S]*aria-modal="true"[\s\S]*aria-label="确认官方文件补证"[\s\S]*tabIndex=\{-1\}/);
  assert.match(source, /ref=\{editorCloseButtonRef\}[\s\S]*aria-label="关闭资料编辑"/);
  assert.match(preview, /ref=\{closeButtonRef\}[\s\S]*aria-label="关闭官方补证预览"/);
  assert.doesNotMatch(source, /\bautoFocus\b/);
});

test("material and attestation busy states fail closed for every dismiss and submit route", () => {
  const source = materialDialogSource();
  const preview = officialPreviewSource();

  assert.match(source, /const requestClose = \(\) => \{[\s\S]*if \(!busy\) finishClose\(\)/);
  assert.match(source, /onClose: busy \? null : requestClose/);
  assert.match(source, /materialSurfaceOpen && busy[\s\S]*activeDialogRef\.current\?\.focus\(\{ preventScroll: true \}\)/);
  assert.match(source, /const run = async \(action, onSuccess, fallbackError = "资料操作失败，请重试。"\) => \{\s*if \(busy \|\| materialActionInFlight\.current\) return null/);
  assert.match(source, /if \(typeof action !== "function"\) \{[\s\S]*当前视图未提供对应资料处理器/);
  assert.match(source, /event\.target === event\.currentTarget && !busy/);
  assert.match(preview, /event\.target === event\.currentTarget && !busy/);
  assert.match(source, /aria-busy=\{busy\}/);
  assert.match(preview, /aria-busy=\{busy\}/);
  assert.match(source, /event\.preventDefault\(\);\s*if \(!primaryDisabled\) submitPrimary\(\)/);
  assert.match(preview, /event\.preventDefault\(\);\s*if \(!busy\) onConfirm\(confirmation\)/);
  assert.match(source, /aria-label="关闭资料编辑"[\s\S]*disabled=\{busy\}/);
  assert.match(preview, /aria-label="关闭官方补证预览"[\s\S]*disabled=\{busy\}/);
  assert.match(source, /onClick=\{requestClose\} disabled=\{busy\}>取消/);
  assert.match(preview, /onClick=\{onClose\} disabled=\{busy\}>暂不确认/);
  assert.match(source, /type="submit" disabled=\{primaryDisabled\}/);
  assert.match(preview, /type="submit" disabled=\{busy \|\| !confirmation\}/);
});

test("focus hardening preserves material and server-hash confirmation payload paths", () => {
  const source = materialDialogSource();
  const preview = officialPreviewSource();

  assert.match(source, /const primaryHandlerAvailable = mode === "url"[\s\S]*typeof onFetchUrl === "function"[\s\S]*typeof onImportFile === "function"[\s\S]*typeof onSubmit === "function"/);
  assert.match(source, /if \(mode === "url"\) return run\(\s*typeof onFetchUrl === "function" \? \(\) => onFetchUrl\(form\) : null/);
  assert.match(source, /if \(mode === "file"\) return run\(\s*typeof onImportFile === "function" \? \(\) => onImportFile\(form, file\) : null/);
  assert.match(source, /typeof onSubmit === "function" \? \(\) => onSubmit\(\{ \.\.\.form, kind: "note", source_url: "" \}\) : null/);
  assert.match(source, /const saveManualRevision = \(\) => run\(\s*typeof onSubmit === "function" \? \(\) => onSubmit\(form\) : null/);
  assert.match(source, /onConfirmOfficialAttestation\(form\.id, confirmation\)/);
  assert.match(preview, /buildOfficialAttestationConfirmation\(attestation\)/);
  assert.match(preview, /onConfirm\(confirmation\)/);
});
