import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  workflowPolicySaveControl,
  workflowPolicySourceState,
  workflowPolicyValidation,
} from "../src/workflowPolicyUi.js";

const dialogSource = readFileSync(
  new URL("../src/components/WorkflowPolicyDialog.jsx", import.meta.url),
  "utf8",
);
const hostStyles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
const workflowStyles = readFileSync(
  new URL("../src/styles/workflow-policy.css", import.meta.url),
  "utf8",
);

test("workflow policy dialog uses the shared modal focus and busy-close contract", () => {
  assert.match(dialogSource, /import \{ useModalFocus \} from "\.\.\/useModalFocus"/);
  assert.match(dialogSource, /const dialogRef = useRef\(null\)/);
  assert.match(dialogSource, /const closeButtonRef = useRef\(null\)/);
  assert.match(dialogSource, /if \(!open\) \{\s*setBusy\(false\);\s*return;\s*\}/);
  assert.match(dialogSource, /const canClose = typeof onClose === "function"/);
  assert.match(dialogSource, /useModalFocus\(\{[\s\S]*open,[\s\S]*containerRef: dialogRef,[\s\S]*initialFocusRef: closeButtonRef,[\s\S]*onClose: busy \|\| !canClose \? null : requestClose,[\s\S]*\}\)/);
  assert.match(dialogSource, /open && busy[\s\S]*dialogRef\.current\?\.focus\(\{ preventScroll: true \}\)/);
  assert.match(dialogSource, /ref=\{dialogRef\}/);
  assert.match(dialogSource, /aria-busy=\{busy\}/);
  assert.match(dialogSource, /tabIndex=\{-1\}/);
  assert.match(dialogSource, /ref=\{closeButtonRef\}/);
  assert.doesNotMatch(dialogSource, /window\.addEventListener\("keydown"/);
  assert.match(dialogSource, /document\.addEventListener\("focusin", containDialogFocus, true\)/);
  assert.match(dialogSource, /dialog\.contains\(event\.target\)/);

  assert.match(dialogSource, /event\.target !== event\.currentTarget/);
  assert.match(dialogSource, /event\.preventDefault\(\)/);
  assert.match(dialogSource, /const requestClose = \(\) => \{\s*if \(busy \|\| !canClose\) return/);
  assert.match(dialogSource, /aria-label="关闭讨论流程设置"[\s\S]*disabled=\{busy \|\| !canClose\}/);
  assert.match(dialogSource, />取消<\/button>/);
  assert.match(dialogSource, /type="submit" className="primary" disabled=\{!saveControl\.canSubmit\}/);
});

test("workflow policy separates draft saving from the next-round member gate", () => {
  assert.match(dialogSource, /<h2 id=\{dialogTitleId\}>讨论流程设置<\/h2>/);
  assert.match(dialogSource, /NEXT ROUND MEMBER GATE/);
  assert.match(dialogSource, /configurationGate\.blockers\.slice\(0, 3\)/);
  assert.match(dialogSource, /blocker\.title/);
  assert.match(dialogSource, /blocker\.detail/);
  assert.match(dialogSource, /可保存不等于可启动/);
  assert.match(dialogSource, /门禁不会因保存而自动放宽/);
  assert.match(dialogSource, /className=\{`workflow-save-summary \$\{saveStateTone\}`\}/);
  assert.match(dialogSource, /role="status" aria-live="polite"/);
  assert.match(workflowStyles, /\.workflow-readiness-panel\s*\{/);
  assert.match(workflowStyles, /\.workflow-readiness-list\s*\{/);
  assert.match(workflowStyles, /\.workflow-save-summary\s*\{/);
  assert.match(workflowStyles, /grid-template-columns:\s*auto minmax\(0, 1fr\) auto/);
});

test("workflow submit semantics stay normalized and user-confirmed", () => {
  assert.match(dialogSource, /const saveControl = workflowPolicySaveControl\(/);
  assert.match(dialogSource, /!policiesEqual\(draft, policySourceState\.draft\)/);
  assert.match(dialogSource, /if \(submissionInFlightRef\.current\) return/);
  assert.match(dialogSource, /if \(!saveControl\.canSubmit\) \{[\s\S]*setLocalError\(saveControl\.instruction\)/);
  assert.match(dialogSource, /setBusy\(true\)/);
  assert.match(dialogSource, /await submitHandler\(normalizeWorkflowPolicy\(draft\)\)/);
  assert.match(dialogSource, /finally \{[\s\S]*setBusy\(false\)[\s\S]*\}\s*if \(requestSessionRef\.current === submissionSession\) \{[\s\S]*closeHandler\(\)[\s\S]*流程已保存，但窗口关闭失败/);
  assert.match(dialogSource, /最终结论必须由你确认/);
  assert.match(dialogSource, /不连接或执行真实交易/);
});

test("workflow source and save projections fail closed without hiding repairs", () => {
  const malformed = workflowPolicySourceState({
    stage_order: "analysis",
    minimum_stage_coverage: [],
    required_coverage: [{ id: "risk", label: "", minimum: 0, any_of: {} }],
    user_confirmation_required: false,
    execution_capability: "live",
    live_trading_allowed: true,
  });
  assert.equal(malformed.integrityOk, false);
  assert.equal(malformed.draft.user_confirmation_required, true);
  assert.equal(malformed.draft.execution_capability, "none");
  assert.equal(malformed.draft.live_trading_allowed, false);

  const validation = workflowPolicyValidation(malformed.draft);
  assert.equal(validation.ok, false);
  const control = workflowPolicySaveControl({
    draft: malformed.draft,
    roomId: "room-a",
    changed: true,
    busy: false,
    submitHandlerAvailable: true,
    closeHandlerAvailable: true,
  });
  assert.equal(control.canSubmit, false);
  assert.equal(workflowPolicySaveControl({
    ...control,
    draft: workflowPolicySourceState(null).draft,
    roomId: "room-a",
    changed: false,
    busy: false,
    submitHandlerAvailable: true,
    closeHandlerAvailable: true,
  }).canSubmit, false);
});

test("workflow-only CSS follows the lazy dialog while shared inspector and member styles stay eager", () => {
  assert.match(dialogSource, /import "\.\.\/styles\/workflow-policy\.css";/);
  assert.match(workflowStyles, /\.workflow-dialog\s*\{/);
  assert.match(workflowStyles, /\.workflow-dialog-backdrop\s*\{/);
  assert.match(workflowStyles, /\.choice-chip-list\s*\{/);
  assert.match(workflowStyles, /@media \(max-width: 760px\)/);
  assert.match(workflowStyles, /@media \(max-width: 620px\)/);
  assert.match(workflowStyles, /env\(safe-area-inset-top\)/);
  assert.match(workflowStyles, /prefers-reduced-motion: reduce/);
  assert.match(workflowStyles, /\.workflow-policy-ledger\s*\{/);

  assert.doesNotMatch(hostStyles, /\.workflow-dialog\s*\{/);
  assert.doesNotMatch(hostStyles, /\.workflow-dialog-backdrop\s*\{/);
  assert.doesNotMatch(hostStyles, /\.choice-chip(?:-list)?\s*\{/);
  assert.match(hostStyles, /\.member-capability-list\s*\{/);
  assert.match(hostStyles, /\.dialog \.member-capability-chip\s*\{/);
  assert.match(hostStyles, /\.policy-source-tag\s*\{/);
  assert.match(hostStyles, /\.workflow-summary-meta\s*\{/);
});
