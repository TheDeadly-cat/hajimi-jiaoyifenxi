export const UNASSIGNED_PROVIDER_ID = "unassigned";


export function normalizedProviderId(value) {
  return String(value ?? "").trim().toLowerCase() || UNASSIGNED_PROVIDER_ID;
}


export function providerIsAvailable(provider) {
  return provider?.configured === true && provider?.policy_disabled !== true;
}


export function buildProviderRouteSummary(members = [], providers = []) {
  const providerById = new Map(
    providers.map((provider) => [normalizedProviderId(provider.id), provider]),
  );
  const routes = new Map();
  for (const member of members) {
    if (!member.enabled) continue;
    const providerId = normalizedProviderId(member.provider);
    const provider = providerById.get(providerId);
    const model = String(member.model || provider?.model || "").trim();
    const policyDisabled = provider?.policy_disabled === true;
    const key = JSON.stringify([providerId, model]);
    const route = routes.get(key) || {
      key,
      id: providerId,
      count: 0,
      name: provider?.name || (providerId === UNASSIGNED_PROVIDER_ID ? "未分配执行器" : providerId),
      configured: provider?.configured === true,
      policyDisabled,
      available: providerIsAvailable(provider),
      model,
      memberIds: [],
    };
    route.count += 1;
    route.memberIds.push(member.id);
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
          `${entry.name}${entry.model ? ` / ${entry.model}` : ""}`
          + `${entry.policyDisabled ? "（策略禁用）" : ""} ×${entry.count}`
        )).join(" · ")
      : "未启用成员",
  };
}


export function normalizeProviderPreflight(data) {
  const payload = data?.preflight && typeof data.preflight === "object"
    ? data.preflight
    : data || {};
  const rawChecks = Array.isArray(payload.results)
    ? payload.results
    : Array.isArray(payload.checks)
      ? payload.checks
      : Array.isArray(payload.provider_checks)
        ? payload.provider_checks
        : Array.isArray(payload.providers)
          ? payload.providers
          : [];
  const checks = rawChecks.map((check) => {
    const id = normalizedProviderId(check.provider || check.provider_id || check.id);
    const model = String(check.model || "").trim();
    const errorCode = String(check.error_code || check.code || "").trim();
    const policyDisabled = check.policy_disabled === true
      || errorCode.toUpperCase() === "PROVIDER_POLICY_DISABLED";
    const reportedReady = typeof check.ready === "boolean"
      ? check.ready
      : typeof check.available === "boolean"
        ? check.available
        : typeof check.ok === "boolean"
          ? check.ok
          : ["ready", "ok", "available"].includes(String(check.status || "").toLowerCase())
            ? true
            : ["failed", "unavailable", "error", "blocked"].includes(String(check.status || "").toLowerCase())
              ? false
              : null;
    return {
      key: JSON.stringify([id, model]),
      id,
      model,
      ready: policyDisabled ? false : reportedReady,
      policyDisabled,
      errorCode,
      error: check.error || check.message || "",
      memberCount: Number(check.member_count || 0),
      memberNames: Array.isArray(check.member_names) ? check.member_names : [],
      latencyMs: Number(check.latency_ms || 0),
      cached: check.cached === true,
    };
  });
  const explicitReady = payload.ready
    ?? payload.all_ready
    ?? (data?.preflight && typeof payload.ok === "boolean" ? payload.ok : undefined);
  const ready = typeof explicitReady === "boolean"
    ? explicitReady && checks.every((check) => check.ready !== false)
    : checks.length
      ? checks.every((check) => check.ready === true)
      : false;
  const confirmed = typeof explicitReady === "boolean"
    || (checks.length > 0 && checks.every((check) => typeof check.ready === "boolean"));
  return {
    checks,
    ready,
    confirmed,
    verificationScope: String(payload.verification_scope || "").trim(),
    externalCallCount: Number(payload.external_call_count || 0),
  };
}
