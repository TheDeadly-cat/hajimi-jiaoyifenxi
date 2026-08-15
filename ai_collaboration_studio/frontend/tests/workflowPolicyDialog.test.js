import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

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
  assert.match(dialogSource, /useModalFocus\(\{[\s\S]*open,[\s\S]*containerRef: dialogRef,[\s\S]*initialFocusRef: closeButtonRef,[\s\S]*onClose: busy \? null : onClose,[\s\S]*\}\)/);
  assert.match(dialogSource, /open && busy[\s\S]*dialogRef\.current\?\.focus\(\{ preventScroll: true \}\)/);
  assert.match(dialogSource, /ref=\{dialogRef\}/);
  assert.match(dialogSource, /aria-busy=\{busy\}/);
  assert.match(dialogSource, /tabIndex=\{-1\}/);
  assert.match(dialogSource, /ref=\{closeButtonRef\}/);
  assert.doesNotMatch(dialogSource, /window\.addEventListener\("keydown"/);

  assert.match(dialogSource, /event\.target !== event\.currentTarget/);
  assert.match(dialogSource, /event\.preventDefault\(\)/);
  assert.match(dialogSource, /if \(!busy\) onClose\(\)/);
  assert.match(dialogSource, /aria-label="关闭讨论流程设置"[\s\S]*disabled=\{busy\}/);
  assert.match(dialogSource, />取消<\/button>/);
  assert.match(dialogSource, /type="submit" className="primary" disabled=\{busy\}/);
});

test("workflow submit semantics stay normalized and user-confirmed", () => {
  assert.match(dialogSource, /const validationError = validateDraft\(draft\)/);
  assert.match(dialogSource, /setBusy\(true\)/);
  assert.match(dialogSource, /await onSubmit\(normalizeWorkflowPolicy\(draft\)\);\s*onClose\(\);/);
  assert.match(dialogSource, /最终结论必须由你确认/);
  assert.match(dialogSource, /不连接或执行真实交易/);
});

test("workflow-only CSS follows the lazy dialog while shared inspector and member styles stay eager", () => {
  assert.match(dialogSource, /import "\.\.\/styles\/workflow-policy\.css";/);
  assert.match(workflowStyles, /\.workflow-dialog\s*\{/);
  assert.match(workflowStyles, /\.workflow-dialog-backdrop\s*\{/);
  assert.match(workflowStyles, /\.choice-chip-list\s*\{/);
  assert.match(workflowStyles, /@media \(max-width: 760px\)/);
  assert.match(workflowStyles, /@media \(max-width: 620px\)/);

  assert.doesNotMatch(hostStyles, /\.workflow-dialog\s*\{/);
  assert.doesNotMatch(hostStyles, /\.workflow-dialog-backdrop\s*\{/);
  assert.doesNotMatch(hostStyles, /\.choice-chip(?:-list)?\s*\{/);
  assert.match(hostStyles, /\.member-capability-list\s*\{/);
  assert.match(hostStyles, /\.dialog \.member-capability-chip\s*\{/);
  assert.match(hostStyles, /\.policy-source-tag\s*\{/);
  assert.match(hostStyles, /\.workflow-summary-meta\s*\{/);
});
