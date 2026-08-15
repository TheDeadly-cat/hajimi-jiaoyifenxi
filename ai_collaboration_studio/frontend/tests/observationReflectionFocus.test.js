import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const observationSource = readFileSync(
  new URL("../src/components/ObservationPanel.jsx", import.meta.url),
  "utf8",
);
const reflectionSource = readFileSync(
  new URL("../src/components/ReflectionDialog.jsx", import.meta.url),
  "utf8",
);
const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
const lineageSource = readFileSync(
  new URL("../src/components/DecisionLineagePanel.jsx", import.meta.url),
  "utf8",
);

test("observation dialog owns focus only after its initialized surface mounts", () => {
  assert.match(observationSource, /import \{ useModalFocus \} from "\.\.\/useModalFocus"/);
  assert.match(observationSource, /useLayoutEffect\(\(\) => \{[\s\S]*if \(!open\)[\s\S]*setForm\(null\)[\s\S]*setInitializedOpen\(true\)/);
  assert.match(observationSource, /const surfaceOpen = Boolean\(open && form && initializedOpen\)/);
  assert.match(observationSource, /useModalFocus\(\{[\s\S]*open: surfaceOpen,[\s\S]*initialFocusRef: closeButtonRef,[\s\S]*restoreFallbackRef: restoreFocusRef,[\s\S]*onClose: busy \? null : onClose/);
  assert.match(observationSource, /role="dialog"[\s\S]*aria-modal="true"[\s\S]*aria-busy=\{busy\}[\s\S]*tabIndex=\{-1\}/);
  assert.match(observationSource, /aria-label="关闭新建模拟观察"[\s\S]*disabled=\{busy\}/);
  assert.match(observationSource, /event\.target === event\.currentTarget && !busy/);
  assert.match(observationSource, /surfaceOpen && busy[\s\S]*dialogRef\.current\?\.focus/);
});

test("reflection dialog keeps every dismissal path fail closed while saving", () => {
  assert.match(reflectionSource, /import \{ useModalFocus \} from "\.\.\/useModalFocus"/);
  assert.match(reflectionSource, /useLayoutEffect\(\(\) => \{[\s\S]*setInitializedReflection\(null\)[\s\S]*setForm\(null\)[\s\S]*setInitializedReflection\(reflection\)/);
  assert.match(reflectionSource, /const surfaceOpen = Boolean\(open && reflection && form && initializedReflection === reflection\)/);
  assert.match(reflectionSource, /useModalFocus\(\{[\s\S]*open: surfaceOpen,[\s\S]*initialFocusRef: closeButtonRef,[\s\S]*restoreFallbackRef: restoreFocusRef,[\s\S]*onClose: busy \? null : onClose/);
  assert.match(reflectionSource, /role="dialog"[\s\S]*aria-modal="true"[\s\S]*aria-busy=\{busy\}[\s\S]*tabIndex=\{-1\}/);
  assert.match(reflectionSource, /aria-label="关闭观察结果复盘"[\s\S]*disabled=\{busy\}/);
  assert.match(reflectionSource, /event\.target === event\.currentTarget && !busy/);
  assert.match(reflectionSource, /surfaceOpen && busy[\s\S]*dialogRef\.current\?\.focus/);
  assert.doesNotMatch(reflectionSource, /window\.addEventListener\("keydown"/);
});

test("focus hardening preserves the single observation and reflection submit chains", () => {
  assert.equal((observationSource.match(/await onSubmit\(form\)/g) || []).length, 1);
  assert.equal((reflectionSource.match(/await onSave\(reflection, form\)/g) || []).length, 1);
  assert.equal((reflectionSource.match(/await onConfirm\(saved\)/g) || []).length, 1);
  assert.match(reflectionSource, /if \(shouldConfirm\) await onConfirm\(saved\);[\s\S]*onClose\(\)/);
});

test("observation and reflection restore the exact nested inspector trigger", () => {
  assert.match(observationSource, /onAdd\(event\.currentTarget\)/);
  assert.match(observationSource, /onEditReflection\(reflection, event\.currentTarget\)/);
  assert.match(lineageSource, /onCreateObservation\(decisionPackage, observationSourceBranch, event\.currentTarget\)/);
  assert.match(appSource, /const observationRestoreFocusRef = useRef\(null\)/);
  assert.match(appSource, /const reflectionRestoreFocusRef = useRef\(null\)/);
  assert.match(appSource, /openObservationFromDecision = \(decisionPackage, branch, launchTrigger = null\)/);
  assert.match(appSource, /openReflection = \(reflection, launchTrigger = null\)/);
  assert.match(appSource, /restoreFocusRef=\{observationRestoreFocusRef\}/);
  assert.match(appSource, /restoreFocusRef=\{reflectionRestoreFocusRef\}/);
});
