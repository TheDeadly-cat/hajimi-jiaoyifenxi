export const UNASSIGNED_PROVIDER_ID = "unassigned";
export const BULK_ROUTE_PROVIDER_IDS = Object.freeze(["deepseek", "doubao"]);

function record(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function array(value) {
  return Array.isArray(value) ? value : [];
}

function text(value) {
  return typeof value === "string" ? value.trim() : "";
}

function nonNegativeInteger(value) {
  if (
    typeof value !== "number"
    && !(typeof value === "string" && /^(?:0|[1-9]\d*)$/.test(value.trim()))
  ) return null;
  const numeric = typeof value === "number" ? value : Number(value.trim());
  return Number.isSafeInteger(numeric) && numeric >= 0 ? numeric : null;
}

function providerMap(providers) {
  const byId = new Map();
  for (const rawProvider of array(providers)) {
    const provider = record(rawProvider);
    const id = normalizedProviderId(provider.id);
    if (id === UNASSIGNED_PROVIDER_ID || byId.has(id)) continue;
    byId.set(id, provider);
  }
  return byId;
}

export function normalizedProviderId(value) {
  return text(value).toLowerCase() || UNASSIGNED_PROVIDER_ID;
}

export function providerIsAvailable(provider) {
  const value = record(provider);
  return value.configured === true && value.policy_disabled !== true;
}

export function buildProviderRouteSummary(members = [], providers = []) {
  const providerById = providerMap(providers);
  const routes = new Map();
  for (const rawMember of array(members)) {
    const member = record(rawMember);
    if (member.enabled !== true) continue;
    const providerId = normalizedProviderId(member.provider);
    const provider = providerById.get(providerId);
    const model = text(member.model) || text(provider?.model);
    const policyDisabled = provider?.policy_disabled === true;
    const key = JSON.stringify([providerId, model]);
    const route = routes.get(key) || {
      key,
      id: providerId,
      count: 0,
      name: text(provider?.name)
        || (providerId === UNASSIGNED_PROVIDER_ID ? "未分配执行器" : providerId),
      configured: provider?.configured === true,
      policyDisabled,
      available: providerIsAvailable(provider),
      model,
      memberIds: [],
    };
    route.count += 1;
    const memberId = text(member.id);
    if (memberId) route.memberIds.push(memberId);
    routes.set(key, route);
  }
  const entries = [...routes.values()];
  return {
    entries,
    total: entries.reduce((sum, entry) => sum + entry.count, 0),
    hasOpenAI: entries.some((entry) => entry.id === "openai"),
    hasPolicyDisabled: entries.some((entry) => entry.policyDisabled),
    hasUnassigned: entries.some((entry) => entry.id === UNASSIGNED_PROVIDER_ID),
    hasUnavailable: entries.some((entry) => !entry.available),
    label: entries.length
      ? entries.map((entry) => (
          entry.name
          + (entry.model ? " / " + entry.model : "")
          + (entry.policyDisabled ? "（策略禁用）" : "")
          + " ×" + entry.count
        )).join(" · ")
      : "未启用成员",
  };
}

export function normalizeProviderPreflight(data) {
  const envelope = record(data);
  const payload = Object.keys(record(envelope.preflight)).length
    ? record(envelope.preflight)
    : envelope;
  const rawChecks = Array.isArray(payload.results)
    ? payload.results
    : Array.isArray(payload.checks)
      ? payload.checks
      : Array.isArray(payload.provider_checks)
        ? payload.provider_checks
        : Array.isArray(payload.providers)
          ? payload.providers
          : [];
  const issues = [];
  const seenKeys = new Set();
  const checks = rawChecks.map((rawCheck, index) => {
    const check = record(rawCheck);
    if (check !== rawCheck) {
      issues.push({
        code: "PREFLIGHT_CHECK_INVALID",
        message: "第 " + (index + 1) + " 条模型路由检查不是对象。",
      });
    }
    const id = normalizedProviderId(check.provider || check.provider_id || check.id);
    const model = text(check.model);
    const errorCode = text(check.error_code || check.code);
    const policyDisabled = check.policy_disabled === true
      || errorCode.toUpperCase() === "PROVIDER_POLICY_DISABLED";
    const reportedReady = typeof check.ready === "boolean"
      ? check.ready
      : typeof check.available === "boolean"
        ? check.available
        : typeof check.ok === "boolean"
          ? check.ok
          : ["ready", "ok", "available"].includes(text(check.status).toLowerCase())
            ? true
            : ["failed", "unavailable", "error", "blocked"].includes(text(check.status).toLowerCase())
              ? false
              : null;
    const semanticKey = JSON.stringify([id, model]);
    if (seenKeys.has(semanticKey)) {
      issues.push({
        code: "PREFLIGHT_ROUTE_DUPLICATE",
        message: "模型路由检查重复：" + id + " / " + (model || "默认模型"),
      });
    }
    seenKeys.add(semanticKey);
    if (id === UNASSIGNED_PROVIDER_ID) {
      issues.push({
        code: "PREFLIGHT_PROVIDER_MISSING",
        message: "模型路由检查缺少 Provider 标识。",
      });
    }
    if (reportedReady === null) {
      issues.push({
        code: "PREFLIGHT_STATUS_UNKNOWN",
        message: id + " / " + (model || "默认模型") + " 未返回明确检查状态。",
      });
    }
    const memberCount = nonNegativeInteger(check.member_count);
    const latencyMs = nonNegativeInteger(check.latency_ms);
    if (check.member_count !== undefined && memberCount === null) {
      issues.push({
        code: "PREFLIGHT_MEMBER_COUNT_INVALID",
        message: id + " 的成员计数无效。",
      });
    }
    if (check.latency_ms !== undefined && latencyMs === null) {
      issues.push({
        code: "PREFLIGHT_LATENCY_INVALID",
        message: id + " 的检查耗时无效。",
      });
    }
    return {
      key: JSON.stringify([id, model, index]),
      semanticKey,
      id,
      model,
      ready: policyDisabled ? false : reportedReady,
      policyDisabled,
      errorCode,
      error: text(check.error || check.message),
      memberCount: memberCount ?? 0,
      memberNames: array(check.member_names).map(text).filter(Boolean),
      latencyMs: latencyMs ?? 0,
      cached: check.cached === true,
    };
  });

  const explicitReady = payload.ready
    ?? payload.all_ready
    ?? (Object.keys(record(envelope.preflight)).length && typeof payload.ok === "boolean"
      ? payload.ok
      : undefined);
  const reportedReady = typeof explicitReady === "boolean"
    ? explicitReady && checks.every((check) => check.ready !== false)
    : checks.length
      ? checks.every((check) => check.ready === true)
      : false;
  const verificationScope = text(payload.verification_scope);
  const scopeConfirmed = verificationScope === "local_configuration_only";
  const externalCallCount = nonNegativeInteger(payload.external_call_count);
  const noExternalCalls = externalCallCount === 0;
  if (!scopeConfirmed) {
    issues.push({
      code: "PREFLIGHT_SCOPE_UNCONFIRMED",
      message: "预检未明确绑定 local_configuration_only 作用域。",
    });
  }
  if (externalCallCount === null) {
    issues.push({
      code: "PREFLIGHT_EXTERNAL_CALL_COUNT_INVALID",
      message: "预检未返回有效的外部调用计数。",
    });
  } else if (!noExternalCalls) {
    issues.push({
      code: "PREFLIGHT_EXTERNAL_CALLS_DETECTED",
      message: "本次预检报告了 " + externalCallCount + " 次外部调用，不能标记为本机配置检查通过。",
    });
  }
  const confirmed = typeof explicitReady === "boolean"
    || (checks.length > 0 && checks.every((check) => typeof check.ready === "boolean"));
  const ready = Boolean(
    reportedReady
    && checks.length > 0
    && checks.every((check) => check.ready === true)
    && scopeConfirmed
    && noExternalCalls
    && issues.length === 0,
  );
  return {
    checks,
    ready,
    confirmed,
    verificationScope,
    scopeConfirmed,
    externalCallCount,
    noExternalCalls,
    issues,
  };
}

export function buildProviderRoutingPresentation({
  members = [],
  providers = [],
  preflightChecks = [],
} = {}) {
  const routeSummary = buildProviderRouteSummary(members, providers);
  const providerById = providerMap(providers);
  const groupedChecks = new Map();
  for (const rawCheck of array(preflightChecks)) {
    const check = record(rawCheck);
    const id = normalizedProviderId(check.id);
    const current = groupedChecks.get(id) || [];
    current.push(check);
    groupedChecks.set(id, current);
  }
  const catalog = [...providerById.entries()].map(([id, provider]) => {
    const checks = groupedChecks.get(id) || [];
    const policyDisabled = provider.policy_disabled === true;
    const routeCount = routeSummary.entries
      .filter((entry) => entry.id === id)
      .reduce((sum, entry) => sum + entry.count, 0);
    const checkedReady = checks.length && checks.every((check) => check.ready === true);
    const checkedFailed = checks.some((check) => check.ready === false);
    const checkedUnknown = checks.some((check) => ![true, false].includes(check.ready));
    const status = policyDisabled
      ? "failed"
      : checkedFailed
        ? "failed"
        : checkedUnknown
          ? "unknown"
          : checkedReady
            ? "ready"
            : provider.configured === true
              ? "configured"
              : "missing";
    const statusLabel = policyDisabled
      ? "策略禁用"
      : checkedFailed
        ? "检查失败"
        : checkedUnknown
          ? "状态未知"
          : checkedReady
            ? "检查通过"
            : provider.configured === true
              ? "待检查"
              : "未配置";
    return {
      id,
      name: text(provider.name) || id,
      provider,
      routeCount,
      checks,
      available: providerIsAvailable(provider),
      policyDisabled,
      status,
      statusLabel,
      modelLabel: checks.length > 1
        ? checks.length + " 条模型路由"
        : text(checks[0]?.model) || text(provider.model) || "默认模型",
      title: checks.length
        ? checks.map((check) => (
            (text(check.model) || "默认模型")
            + "："
            + (text(check.error) || (check.ready === true ? "检查通过" : "状态未知"))
          )).join("；")
        : text(provider.model) || "默认模型",
    };
  });
  const policyDisabledMembers = routeSummary.entries
    .filter((entry) => entry.policyDisabled)
    .reduce((sum, entry) => sum + entry.count, 0);
  const unassignedMembers = routeSummary.entries
    .filter((entry) => entry.id === UNASSIGNED_PROVIDER_ID)
    .reduce((sum, entry) => sum + entry.count, 0);
  const availableMembers = routeSummary.entries
    .filter((entry) => entry.available)
    .reduce((sum, entry) => sum + entry.count, 0);
  const warnings = [
    policyDisabledMembers ? {
      key: "policy",
      title: policyDisabledMembers + " 位成员使用策略禁用的执行器",
      detail: "服务端不会执行这些路由；请先迁移到可用执行器。",
    } : null,
    unassignedMembers ? {
      key: "unassigned",
      title: unassignedMembers + " 位成员尚未分配执行器",
      detail: "缺失路由不会被当作 OpenAI；请逐成员重新选择。",
    } : null,
  ].filter(Boolean);
  const preflightRows = array(preflightChecks).map((rawCheck) => {
    const check = record(rawCheck);
    const provider = providerById.get(normalizedProviderId(check.id));
    const detail = [
      check.memberCount ? check.memberCount + " 位成员" : "未分配成员",
      check.latencyMs ? check.latencyMs + " ms" : "",
      check.policyDisabled ? "策略禁用，未调用" : "只读本机配置，未调用模型",
    ].filter(Boolean).join(" · ");
    return {
      ...check,
      providerName: text(provider?.name) || normalizedProviderId(check.id),
      detail,
      memberTitle: array(check.memberNames).map(text).filter(Boolean).join("、"),
    };
  });
  const routeFingerprint = array(members)
    .filter((member) => record(member).enabled === true)
    .map((member) => {
      const value = record(member);
      return JSON.stringify([
        text(value.id),
        normalizedProviderId(value.provider),
        text(value.model),
      ]);
    })
    .sort()
    .join("|");
  return {
    routeSummary,
    routeFingerprint,
    catalog,
    warnings,
    preflightRows,
    bulkTargets: BULK_ROUTE_PROVIDER_IDS.map((id) => {
      const provider = providerById.get(id);
      return {
        id,
        provider,
        name: text(provider?.name) || id,
        available: providerIsAvailable(provider),
        policyDisabled: provider?.policy_disabled === true,
      };
    }),
    stats: [
      { key: "enabled", label: "启用成员", value: routeSummary.total },
      { key: "available", label: "可用路由成员", value: availableMembers },
      { key: "routes", label: "模型路由", value: routeSummary.entries.length },
      { key: "blocked", label: "阻断 / 未分配", value: policyDisabledMembers + unassignedMembers },
    ],
  };
}

export function providerRoutingResultFeedback(result, providerName) {
  const value = record(result);
  const name = text(providerName) || "目标 Provider";
  const total = nonNegativeInteger(value.total) ?? 0;
  const succeeded = nonNegativeInteger(value.succeeded) ?? 0;
  const updated = nonNegativeInteger(value.updated) ?? succeeded;
  const unchanged = nonNegativeInteger(value.unchanged) ?? 0;
  const failed = array(value.failed).map(record);
  const refreshError = text(value.refreshError);
  if (failed.length || refreshError) {
    const failedNames = failed.slice(0, 3).map((item) => text(item.name)).filter(Boolean).join("、");
    const failureSuffix = failedNames
      ? "；失败成员：" + failedNames + (failed.length > 3 ? " 等" : "")
      : "";
    const refreshSuffix = refreshError ? "；房间刷新未完成，请手动重开房间确认" : "";
    return {
      tone: "error",
      text: "已将 " + succeeded + "/" + total + " 位启用成员切换到 " + name + failureSuffix + refreshSuffix,
    };
  }
  if (!total) return { tone: "warning", text: "当前房间没有启用成员，无需切换。" };
  return {
    tone: "success",
    text: unchanged
      ? name + " 路由已就绪：更新 " + updated + " 位，保留 " + unchanged + " 位原配置。"
      : "已将 " + updated + " 位启用成员切换到 " + name + "。",
  };
}
