import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  blockerLabel,
  roundLaunchErrorMessage,
  roundLaunchNumericCallLimit,
  roundLaunchRecovery,
  roundLaunchSubmitControl,
} from "../src/roundLaunchUi.js";

test("round launch call limit parser rejects coercive numeric forms", () => {
  assert.equal(Number.isNaN(roundLaunchNumericCallLimit("")), true);
  assert.equal(Number.isNaN(roundLaunchNumericCallLimit("1e2")), true);
  assert.equal(Number.isNaN(roundLaunchNumericCallLimit("2.5")), true);
  assert.equal(Number.isNaN(roundLaunchNumericCallLimit(true)), true);
  assert.equal(roundLaunchNumericCallLimit("28"), 28);
  assert.equal(roundLaunchErrorMessage({ message: "untrusted" }, "未知错误"), "未知错误");
});

test("round launch submit control requires every explicit permit", () => {
  const blocked = roundLaunchSubmitControl({
    authorization: { canConfirm: true },
    requestIdReady: true,
    planPresent: true,
    loading: false,
    externalError: "",
    busy: false,
    confirmHandlerAvailable: false,
  });
  assert.equal(blocked.canSubmit, false);
  assert.match(blocked.instruction, /处理入口/);

  const ready = roundLaunchSubmitControl({
    authorization: { canConfirm: true },
    requestIdReady: true,
    planPresent: true,
    loading: false,
    externalError: "",
    busy: false,
    confirmHandlerAvailable: true,
  });
  assert.equal(ready.canSubmit, true);
  assert.equal(ready.phase, "ready");
  assert.equal(ready.checks.every((check) => check.passed), true);
  assert.match(blockerLabel({ code: "UNKNOWN_CODE" }), /未识别的阻断项/);
});

test("round launch owns styles and guards stale submission completion", () => {
  const componentSource = readFileSync(new URL("../src/components/RoundLaunchDialog.jsx", import.meta.url), "utf8");
  const ownedCss = readFileSync(new URL("../src/styles/round-launch.css", import.meta.url), "utf8");
  const globalCss = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
  assert.match(componentSource, /styles\/round-launch\.css/);
  assert.doesNotMatch(componentSource, /const styles = Object\.freeze/);
  assert.match(componentSource, /operationRef\.current !== sequence/);
  assert.match(componentSource, /data-launch-state/);
  assert.match(componentSource, /LAUNCH PERMIT/);
  assert.doesNotMatch(globalCss, /\.round-launch-backdrop\s*\{/);
  assert.match(ownedCss, /max-height:\s*calc\(var\(--visual-viewport-height, 100dvh\) - 24px\)/);
  assert.match(ownedCss, /env\(safe-area-inset-bottom\)/);
  assert.match(ownedCss, /max\(12px, env\(safe-area-inset-top\)\)/);
});

test("round launch keeps the human decision summary visible without weakening permits", () => {
  const componentSource = readFileSync(new URL("../src/components/RoundLaunchDialog.jsx", import.meta.url), "utf8");
  const ownedCss = readFileSync(new URL("../src/styles/round-launch.css", import.meta.url), "utf8");

  assert.match(componentSource, /<h2 id=\{titleId\}>开始一轮<\/h2>/);
  assert.match(componentSource, /className="round-launch-snapshot"/);
  assert.match(componentSource, /查看计划与校验详情/);
  assert.match(componentSource, /本轮调用总上限/);
  assert.match(componentSource, /无下单、实盘或钱包权限/);
  assert.match(componentSource, /role="status" aria-live="polite"/);
  assert.match(componentSource, /const \[memberRoutesOpen, setMemberRoutesOpen\] = useState\(false\)/);
  assert.match(componentSource, /blockerEntries\.map\(\(\{ blocker, key \}\)/);
  assert.doesNotMatch(componentSource, /blocker, index/);
  assert.match(componentSource, /document\.addEventListener\("focusin", containDialogFocus, true\)/);
  assert.match(componentSource, /dialog\.contains\(event\.target\)/);
  assert.match(ownedCss, /\.round-launch-snapshot-grid\s*\{/);
  assert.match(ownedCss, /grid-template-columns:\s*repeat\(2, minmax\(0, 1fr\)\)/);
  assert.match(ownedCss, /\.round-launch-footer-state\s*\{/);
  assert.match(ownedCss, /@media \(forced-colors: active\)/);
});


test("recovery prioritizes an actionable configuration cause without changing authorization", () => {
  const plan = { blockers: [
    { code: "WORKFLOW_PROVIDER_COVERAGE_INSUFFICIENT" },
    { code: "MODERATOR_PROVIDER_ROUTE_UNAVAILABLE" },
    { code: "PROVIDER_NOT_CONFIGURED", provider: "deepseek" },
  ] };
  const original = structuredClone(plan);
  const recovery = roundLaunchRecovery({ plan, authorization: { validLimit: true }, submitControl: { canSubmit: false } });
  assert.match(recovery.title, /模型尚未配置/);
  assert.match(recovery.detail, /房间信息 → 模型路由与本机配置/);
  assert.match(recovery.detail, /本机设置/);
  assert.equal(recovery.providerSettings, true);
  assert.deepEqual(plan, original);
  const disabled = roundLaunchRecovery({ plan: { blockers: [{ code: "PROVIDER_POLICY_DISABLED" }] } });
  assert.match(disabled.detail, /不能在这里重新启用/);
  const unknown = roundLaunchRecovery({ plan: { blockers: [{ code: "NEW_UNKNOWN_CODE" }] } });
  assert.match(unknown.detail, /NEW_UNKNOWN_CODE/);
  assert.equal(unknown.providerSettings, undefined);
});

test("recovery distinguishes invalid limits, deployment ceiling and a failed local read", () => {
  assert.match(roundLaunchRecovery({ plan: {}, authorization: { validLimit: false, error: "请输入整数" } }).detail, /请输入整数/);
  assert.match(roundLaunchRecovery({ plan: { blockers: [{ code: "RECOMMENDATION_EXCEEDS_DEPLOYMENT_HARD_LIMIT" }] } }).detail, /不能.*绕过/);
  const failed = roundLaunchRecovery({ plan: null, error: "合成读取失败" });
  assert.equal(failed.retry, true);
  assert.match(failed.detail, /不会调用模型/);
  assert.equal(roundLaunchRecovery({ plan: { blockers: [] }, submitControl: { canSubmit: true } }), null);
});
