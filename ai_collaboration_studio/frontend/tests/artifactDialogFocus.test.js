import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("../src/components/ArtifactDialog.jsx", import.meta.url),
  "utf8",
);

function editorSource() {
  const start = source.indexOf("function ArtifactEditor");
  const end = source.indexOf("\nconst MemoArtifactEditor", start);
  assert.ok(start >= 0 && end > start, "ArtifactEditor source should be discoverable");
  return source.slice(start, end);
}

function dialogSource() {
  const start = source.indexOf("export const ArtifactDialog");
  assert.ok(start >= 0, "ArtifactDialog source should be discoverable");
  return source.slice(start);
}

test("artifact editor owns a complete shared modal focus contract", () => {
  const editor = editorSource();

  assert.match(source, /import \{ useModalFocus \} from "\.\.\/useModalFocus"/);
  assert.match(editor, /const dialogRef = useRef\(null\)/);
  assert.match(editor, /const closeButtonRef = useRef\(null\)/);
  assert.match(editor, /useModalFocus\(\{[\s\S]*open,[\s\S]*containerRef: dialogRef,[\s\S]*initialFocusRef: closeButtonRef,[\s\S]*restoreFallbackRef: restoreFocusRef/);
  assert.match(editor, /role="dialog"[\s\S]*aria-modal="true"[\s\S]*aria-labelledby=\{dialogTitleId\}[\s\S]*aria-describedby=\{dialogDescriptionId\}[\s\S]*aria-busy=\{busy\}[\s\S]*tabIndex=\{-1\}/);
  assert.match(editor, /<strong id=\{dialogTitleId\}>会议产物工作区<\/strong>/);
  assert.match(editor, /<small id=\{dialogDescriptionId\}/);
  assert.match(editor, /ref=\{closeButtonRef\}[\s\S]*aria-label="关闭会议产物工作区"/);
  assert.doesNotMatch(editor, /\bautoFocus\b/);
});

test("every artifact mutation makes all dismissal routes fail closed", () => {
  const editor = editorSource();

  assert.match(editor, /const busy = Boolean\(mutationAction\)/);
  assert.match(editor, /const canClose = typeof onClose === "function"/);
  assert.match(editor, /const requestClose = \(\) => \{[\s\S]*if \(busy\) return;[\s\S]*if \(!canClose\)[\s\S]*onClose\(\)/);
  assert.match(editor, /onClose: busy \|\| !canClose \? null : requestClose/);
  assert.match(editor, /open && busy[\s\S]*dialogRef\.current\?\.focus\(\{ preventScroll: true \}\)/);
  assert.match(editor, /event\.target === event\.currentTarget && !busy/);
  assert.match(editor, /onClick=\{requestClose\} disabled=\{busy \|\| !canClose\}/);
  assert.match(editor, /event\.preventDefault\(\);\s*if \(!busy\) saveAndClose\(\)/);
  assert.match(editor, /setMutationAction\("progress"\)/);
  assert.match(editor, /setMutationAction\("draft"\)/);
  assert.match(editor, /setMutationAction\("confirm"\)/);
});

test("artifact close keeps one hidden lifecycle render for exact trigger restoration", () => {
  const dialog = dialogSource();

  assert.match(dialog, /restoreFocusRef/);
  assert.match(dialog, /const \[retainedArtifact, setRetainedArtifact\] = useState\(null\)/);
  assert.match(dialog, /const surfaceOpen = Boolean\(open && artifact\)/);
  assert.match(dialog, /const renderedArtifact = artifact \|\| retainedArtifact/);
  assert.match(dialog, /open=\{surfaceOpen\}/);
  assert.match(dialog, /restoreFocusRef=\{restoreFocusRef \|\| capturedRestoreFocusRef\}/);
  assert.match(dialog, /surfaceOpen && !wasOpenRef\.current[\s\S]*if \(!restoreFocusRef\)[\s\S]*document\.activeElement/);
  assert.match(dialog, /requestAnimationFrame\(\(\) => \{[\s\S]*requestAnimationFrame\(\(\) => setRetainedArtifact\(null\)\)/);
});

test("every reopened artifact starts a fresh editor session before paint", () => {
  const dialog = dialogSource();

  assert.match(dialog, /const \[editorSession, setEditorSession\] = useState\(0\)/);
  assert.match(dialog, /if \(surfaceOpen && !wasOpenRef\.current\) \{[\s\S]*setEditorSession\(\(current\) => current \+ 1\)/);
  assert.match(dialog, /key=\{JSON\.stringify\(\[renderedArtifact\.id, renderedArtifact\.version, editorSession\]\)\}/);
});

test("focus hardening preserves the unique save confirm decision and export chains", () => {
  const editor = editorSource();

  assert.equal((editor.match(/await saveHandler\(currentArtifact, \{ keepOpen: true \}\)/g) || []).length, 1);
  assert.equal((editor.match(/await saveHandler\(currentArtifact\)/g) || []).length, 1);
  assert.equal((editor.match(/await confirmHandler\(currentArtifact\)/g) || []).length, 1);
  assert.equal((editor.match(/onSubmit=\{onUserDecision\}/g) || []).length, 1);
  assert.equal((editor.match(/await exportHandler\(currentArtifact\)/g) || []).length, 1);
});
