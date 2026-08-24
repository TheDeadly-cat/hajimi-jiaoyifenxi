import {
  PROVIDER_CALL_LIMIT_MAX,
  PROVIDER_CALL_LIMIT_MIN,
} from "./roundLaunchPlan.js";

const PROVIDER_LABELS = Object.freeze({
  openai: "OpenAI",
  deepseek: "DeepSeek",
  doubao: "豆包",
  glm: "GLM",
});

const BLOCKER_LABELS = Object.freeze({
  PROVIDER_STATUS_UNAVAILABLE: "无法读取 Provider 本地状态",
  PROVIDER_SKIPPED: "该 Provider 路由已跳过",
  PROVIDER_POLICY_DISABLED: "该 Provider 被本机策略停用",
  PROVIDER_UNKNOWN: "该 Provider 未注册",
  PROVIDER_NOT_CONFIGURED: "该 Provider 尚未完成本机配置",
  WORKFLOW_PROVIDER_COVERAGE_INSUFFICIENT: "可调用成员不足以覆盖既定讨论流程",
  MODERATOR_PROVIDER_ROUTE_UNAVAILABLE: "主持人的 Provider 路由不可用",
  RECOMMENDATION_EXCEEDS_DEPLOYMENT_HARD_LIMIT: "推荐 Provider 调用次数超过系统硬上限",
  PLAN_NOT_READY: "该计划尚未达到确认条件",
  CLIENT_PLAN_INVALID: "确认单结构不完整或安全边界无法验证",
  ROUND_FOCUS_AUTHORIZATION_REQUIRED: "需要先显式确认下一轮项目焦点",
  ROUND_FOCUS_AUTHORIZATION_INVALID: "下一轮项目焦点授权已失效",
  ROUND_LAUNCH_PLAN_DRIFT: "确认产物或焦点封印已变化",
});

function text(value) {
  return typeof value === "string" ? value.trim() : "";
}

export function providerLabel(provider) {
  const clean = text(provider);
  return PROVIDER_LABELS[clean] || clean || "未知 Provider";
}

export function blockerLabel(blocker = {}) {
  const code = text(blocker.code);
  const base = BLOCKER_LABELS[code] || `未识别的阻断项：${code || "UNKNOWN"}`;
  const route = [blocker.provider ? providerLabel(blocker.provider) : "", text(blocker.model)]
    .filter(Boolean)
    .join(" / ");
  return route ? `${base}（${route}）` : base;
}

export function roundLaunchNumericCallLimit(value) {
  const clean = text(value);
  if (!/^(0|[1-9][0-9]*)$/.test(clean)) return Number.NaN;
  const parsed = Number(clean);
  return Number.isSafeInteger(parsed) ? parsed : Number.NaN;
}

export function initialCallLimit(plan, requestedLimit) {
  if (Number.isSafeInteger(requestedLimit)
    && requestedLimit >= PROVIDER_CALL_LIMIT_MIN
    && requestedLimit <= PROVIDER_CALL_LIMIT_MAX) return requestedLimit;
  const recommended = plan?.calls?.recommended_provider_calls;
  return Number.isSafeInteger(recommended)
    && recommended >= PROVIDER_CALL_LIMIT_MIN
    && recommended <= PROVIDER_CALL_LIMIT_MAX
    ? recommended
    : PROVIDER_CALL_LIMIT_MIN;
}

export function roundLaunchErrorMessage(error, fallback = "启动确认失败。") {
  if (error instanceof Error && text(error.message)) return text(error.message);
  if (typeof error === "string" && text(error)) return text(error);
  return fallback;
}

export function roundLaunchShortHash(value) {
  const clean = text(value);
  return clean.length >= 18 ? `${clean.slice(0, 10)}…${clean.slice(-8)}` : "封印不可用";
}

export function roundLaunchSubmitControl({
  authorization,
  requestIdReady,
  planPresent,
  loading,
  externalError,
  busy,
  confirmHandlerAvailable,
} = {}) {
  const checks = [
    { id: "plan", label: "冻结计划存在", passed: planPresent === true },
    { id: "authorization", label: "计划与调用额度通过授权校验", passed: authorization?.canConfirm === true },
    { id: "request", label: "客户端启动请求标识有效", passed: requestIdReady === true },
    { id: "handler", label: "确认处理入口可用", passed: confirmHandlerAvailable === true },
    { id: "idle", label: "当前没有读取、错误或提交任务", passed: !loading && !externalError && !busy },
  ];
  const canSubmit = checks.every((check) => check.passed);
  const phase = busy ? "submitting" : loading ? "loading" : canSubmit ? "ready" : "blocked";
  const firstMissing = checks.find((check) => !check.passed);
  return {
    canSubmit,
    checks,
    phase,
    instruction: phase === "submitting"
      ? "正在提交与当前计划封印绑定的启动授权。"
      : phase === "loading"
        ? "正在读取冻结计划，不会发起 Provider 调用。"
        : phase === "ready"
          ? "全部启动许可已满足，可以显式确认。"
          : firstMissing?.label || "启动许可未完成。",
  };
}
