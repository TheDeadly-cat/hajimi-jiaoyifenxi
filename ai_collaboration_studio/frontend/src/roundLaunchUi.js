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

// Presentation only: every authorization decision remains in the existing gate.
export function roundLaunchRecovery({ plan, authorization, submitControl, error = "" } = {}) {
  if (error) return {
    title: "开始一轮前需要重新检查",
    detail: `${roundLaunchErrorMessage(error)} 请重新读取确认单；这一步不会调用模型。`,
    retry: true,
  };
  if (!plan) return {
    title: "暂时无法读取本轮计划",
    detail: "请重新读取确认单；这一步不会调用模型。仍失败时可取消返回，保留当前输入。",
    retry: true,
  };
  const blockers = Array.isArray(plan.blockers) ? plan.blockers : [];
  const codes = new Set(blockers.map((blocker) => blocker.code));
  if (codes.has("CLIENT_PLAN_INVALID")) return {
    title: "本轮计划校验未通过",
    detail: "计划或执行边界无法验证。请重新读取确认单；不能跳过校验开始讨论。",
    retry: true,
  };
  if (authorization?.validLimit === false) return {
    title: "请修正本轮调用总上限",
    detail: authorization.error || "请输入允许范围内的整数。",
  };
  if (codes.has("PROVIDER_POLICY_DISABLED") || codes.has("PROVIDER_SKIPPED")) return {
    title: "当前模型被停用，暂不能开始一轮",
    detail: "在“房间信息 → 模型路由与本机配置”中查看可用模型，并显式调整成员路由。被策略停用的模型不能在这里重新启用。",
    providerSettings: true,
  };
  if (codes.has("PROVIDER_NOT_CONFIGURED")) return {
    title: "模型尚未配置，暂不能开始一轮",
    detail: "先到“房间信息 → 模型路由与本机配置”查看缺失项。密钥需按项目 README 的模型配置说明在本机设置；此处不能填写密钥。",
    providerSettings: true,
  };
  if (codes.has("PROVIDER_STATUS_UNAVAILABLE")) return {
    title: "暂时无法读取模型配置状态",
    detail: "请重新读取确认单。仍失败时到“房间信息 → 模型路由与本机配置”查看状态；不会自动检查连通性或切换模型。",
    providerSettings: true,
    retry: true,
  };
  if (["PROVIDER_UNKNOWN", "MODERATOR_PROVIDER_ROUTE_UNAVAILABLE", "WORKFLOW_PROVIDER_COVERAGE_INSUFFICIENT"].some((code) => codes.has(code))) return {
    title: "主持人或讨论成员的模型不可用",
    detail: "请在“房间信息 → 模型路由与本机配置”核对成员路由；到“房间设置”核对主持人和讨论流程，再重新开始一轮。",
    providerSettings: true,
  };
  if (codes.has("RECOMMENDATION_EXCEEDS_DEPLOYMENT_HARD_LIMIT")) return {
    title: "本轮所需调用次数超过系统上限",
    detail: "请取消返回，在房间设置中缩小讨论流程后重新开始一轮。不能通过提高输入值绕过系统上限。",
  };
  if (blockers.length) return {
    title: "本轮仍有条件未满足",
    detail: `${blockerLabel(blockers[0])}。请核对下方详细原因；当前不能开始一轮。`,
  };
  if (!submitControl?.canSubmit) return {
    title: "本轮确认尚未准备好",
    detail: submitControl?.instruction || "请取消后重新开始一轮；不能跳过确认。",
  };
  return null;
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
