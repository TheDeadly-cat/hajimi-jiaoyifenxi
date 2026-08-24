import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  reflectionDialogSourceState,
  reflectionFormChanged,
  reflectionSaveControl,
} from "../src/reflectionDialogUi.js";

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
const reflectionStyles = readFileSync(
  new URL("../src/styles/reflection-dialog.css", import.meta.url),
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
  assert.equal((reflectionSource.match(/await saveHandler\(reflection, reflectionFormSubmission\(form\)\)/g) || []).length, 1);
  assert.equal((reflectionSource.match(/await confirmHandler\(saved\)/g) || []).length, 1);
  assert.match(reflectionSource, /草稿已保存，但确认入记忆失败/);
  assert.match(reflectionSource, /复盘已确认[\s\S]*窗口关闭失败/);
  assert.match(reflectionSource, /submissionInFlightRef/);
});

test("reflection confirmation requires an auditable source while draft changes remain separable", () => {
  const record = {
    id: "reflection-a",
    observation_id: "observation-a",
    version: 2,
    lesson: "保留教训",
    caveat: "单样本不可外推",
    next_test: "增加样本",
    source_snapshot: { symbol: "US.AAPL", return_pct: 1.2 },
    source_snapshot_hash: "a".repeat(64),
  };
  const sourceState = reflectionDialogSourceState(record);
  assert.equal(sourceState.confirmable, true);
  assert.equal(reflectionFormChanged(sourceState.form, sourceState.form), false);
  const base = {
    form: sourceState.form,
    sourceState,
    changed: false,
    busy: false,
    saveHandlerAvailable: true,
    confirmHandlerAvailable: true,
    closeHandlerAvailable: true,
  };
  assert.equal(reflectionSaveControl({ ...base, mode: "draft" }).canSubmit, false);
  assert.equal(reflectionSaveControl({ ...base, mode: "confirm" }).canSubmit, true);
  const broken = reflectionDialogSourceState({ ...record, source_snapshot_hash: "bad" });
  assert.equal(reflectionSaveControl({ ...base, mode: "confirm", sourceState: broken }).canSubmit, false);
  const identityBroken = reflectionDialogSourceState({ ...record, id: "" });
  assert.equal(reflectionSaveControl({ ...base, mode: "draft", sourceState: identityBroken, changed: true }).canSubmit, false);
  assert.match(reflectionSource, /sourceState\.identityOk \? "可继续保存草稿，但不能确认入记忆。" : "记录身份或版本无效，草稿与确认操作均不可用。"/);
});

test("reflection-owned CSS covers permit ledger, safe areas, narrow layout, and reduced motion", () => {
  assert.match(reflectionStyles, /\.reflection-permit-ledger\s*\{/);
  assert.match(reflectionStyles, /env\(safe-area-inset-top\)/);
  assert.match(reflectionStyles, /@container reflection-dialog \(max-width: 440px\)/);
  assert.match(reflectionStyles, /prefers-reduced-motion: reduce/);
  assert.match(reflectionSource, /data-confirm-state=\{confirmControl\.phase\}/);
  assert.match(reflectionSource, /disabled=\{!confirmControl\.canSubmit\}/);
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
