import { AlertTriangle, CheckCircle2, LoaderCircle, Network, RefreshCw, Route } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  buildProviderRouteSummary,
  normalizeProviderPreflight,
  normalizedProviderId,
  providerIsAvailable,
  UNASSIGNED_PROVIDER_ID,
} from "../providerRouting";

function routingResultText(result, providerName) {
  const total = Number(result?.total || 0);
  const succeeded = Number(result?.succeeded || 0);
  const updated = Number(result?.updated ?? succeeded);
  const unchanged = Number(result?.unchanged || 0);
  const failed = Array.isArray(result?.failed) ? result.failed : [];
  const refreshError = String(result?.refreshError || "");
  if (failed.length || refreshError) {
    const failedNames = failed.slice(0, 3).map((item) => item.name).filter(Boolean).join("、");
    const failureSuffix = failedNames ? `；失败成员：${failedNames}${failed.length > 3 ? " 等" : ""}` : "";
    const refreshSuffix = refreshError ? "；房间刷新未完成，请手动重开房间确认" : "";
    return {
      tone: "error",
      text: `已将 ${succeeded}/${total} 位启用成员切换到 ${providerName}${failureSuffix}${refreshSuffix}`,
    };
  }
  if (!total) return { tone: "warning", text: "当前房间没有启用成员，无需切换。" };
  return {
    tone: "success",
    text: unchanged
      ? `${providerName} 路由已就绪：更新 ${updated} 位，保留 ${unchanged} 位原配置。`
      : `已将 ${updated} 位启用成员切换到 ${providerName}。`,
  };
}

export function ProviderRoutingPanel({
  room,
  members = [],
  providers = [],
  roundRunning = false,
  routingBusy = false,
  onRouteMembers,
  onRunPreflight,
}) {
  const [action, setAction] = useState("");
  const [feedback, setFeedback] = useState(null);
  const [preflightChecks, setPreflightChecks] = useState([]);
  const routeSummary = useMemo(
    () => buildProviderRouteSummary(members, providers),
    [members, providers],
  );
  const routeFingerprint = useMemo(
    () => members
      .filter((member) => member.enabled)
      .map((member) => [
        member.id,
        normalizedProviderId(member.provider),
        String(member.model || ""),
      ].join(":"))
      .sort()
      .join("|"),
    [members],
  );
  const providerById = useMemo(
    () => new Map(providers.map((provider) => [normalizedProviderId(provider.id), provider])),
    [providers],
  );
  const preflightByProvider = useMemo(() => {
    const grouped = new Map();
    for (const check of preflightChecks) {
      const current = grouped.get(check.id) || [];
      current.push(check);
      grouped.set(check.id, current);
    }
    return grouped;
  }, [preflightChecks]);
  const enabledMembers = routeSummary.total;
  const policyDisabledMembers = routeSummary.entries
    .filter((entry) => entry.policyDisabled)
    .reduce((sum, entry) => sum + entry.count, 0);
  const unassignedMembers = routeSummary.entries
    .filter((entry) => entry.id === UNASSIGNED_PROVIDER_ID)
    .reduce((sum, entry) => sum + entry.count, 0);
  const busy = routingBusy || Boolean(action);

  useEffect(() => {
    setAction("");
    setFeedback(null);
    setPreflightChecks([]);
  }, [room?.id]);

  useEffect(() => {
    setPreflightChecks([]);
  }, [routeFingerprint]);

  const routeAll = async (providerId) => {
    const provider = providerById.get(providerId);
    if (!providerIsAvailable(provider) || busy || roundRunning) return;
    setAction(`route:${providerId}`);
    setFeedback(null);
    try {
      const result = await onRouteMembers(providerId);
      setFeedback(routingResultText(result, provider.name || providerId));
    } catch (error) {
      setFeedback({ tone: "error", text: `批量切换未完成：${error.message}` });
    } finally {
      setAction("");
    }
  };

  const runPreflight = async () => {
    if (busy || roundRunning || !room?.id) return;
    setAction("preflight");
    setFeedback(null);
    try {
      const data = await onRunPreflight();
      const result = normalizeProviderPreflight(data);
      setPreflightChecks(result.checks);
      const failed = result.checks.filter((check) => check.ready === false);
      setFeedback(result.ready
        ? {
            tone: "success",
            text: `本机配置检查通过${result.checks.length ? `：${result.checks.length} 条模型路由已配置` : ""}。正式连通性只会在确认新轮后检查。`,
          }
        : {
            tone: "error",
            text: failed.length
              ? `本机配置检查未通过：${failed.map((check) => check.error || check.id).slice(0, 3).join("；")}`
              : "本机配置检查未通过，请检查成员模型路由。",
          });
    } catch (error) {
      const endpointMissing = /接口不存在|404|not found/i.test(error.message);
      setFeedback({
        tone: "warning",
        text: endpointMissing
          ? "后端本机配置检查尚未启用；当前未发起模型请求。"
          : `本机配置检查未完成：${error.message}`,
      });
    } finally {
      setAction("");
    }
  };

  return (
    <section className="inspector-section provider-routing-section" id="inspector-providers">
      <div className="section-heading">
        <strong><Network size={15} />模型路由与本机配置</strong>
        <span>{enabledMembers} 位启用成员</span>
      </div>

      <div className="provider-route-summary" aria-label={`本轮预计模型路由：${routeSummary.label}`}>
        {routeSummary.entries.length ? routeSummary.entries.map((entry) => (
          <span
            className={entry.id === "openai" || entry.policyDisabled || entry.id === UNASSIGNED_PROVIDER_ID
              ? "provider-count-chip openai"
              : "provider-count-chip"}
            key={entry.key}
            title={`${entry.model || "默认模型"} · ${entry.policyDisabled
              ? "服务端策略禁用"
              : entry.id === UNASSIGNED_PROVIDER_ID
                ? "尚未分配执行器"
                : entry.configured
                  ? "本机已配置；确认新轮后检查连通性"
                  : "未配置"}`}
          >
            {entry.available ? <Route size={12} /> : <AlertTriangle size={12} />}
            <span>{entry.name}<small>{entry.model || "默认模型"}{entry.policyDisabled ? " · 策略禁用" : ""}</small></span><b>{entry.count}</b>
          </span>
        )) : <span className="provider-empty-route">当前没有启用成员</span>}
      </div>

      {policyDisabledMembers ? (
        <div className="openai-route-warning">
          <AlertTriangle size={14} />
          <span><strong>{policyDisabledMembers} 位成员使用策略禁用的执行器</strong><small>服务端不会执行这些路由；请先迁移到可用执行器。</small></span>
        </div>
      ) : null}

      {unassignedMembers ? (
        <div className="openai-route-warning">
          <AlertTriangle size={14} />
          <span><strong>{unassignedMembers} 位成员尚未分配执行器</strong><small>缺失路由不会再被当作 OpenAI；请逐成员重新选择。</small></span>
        </div>
      ) : null}

      <div className="provider-catalog">
        {providers.map((provider) => {
          const providerId = normalizedProviderId(provider.id);
          const policyDisabled = provider.policy_disabled === true;
          const routeEntries = routeSummary.entries.filter((entry) => entry.id === providerId);
          const routeCount = routeEntries.reduce((sum, entry) => sum + entry.count, 0);
          const providerChecks = preflightByProvider.get(providerId) || [];
          const checkedReady = policyDisabled
            ? false
            : providerChecks.length
              ? providerChecks.every((check) => check.ready === true)
              : null;
          const checkedFailed = policyDisabled
            || providerChecks.some((check) => check.ready === false);
          const checkedModelLabel = providerChecks.length > 1
            ? `${providerChecks.length} 条模型路由`
            : providerChecks[0]?.model || provider.model || "默认模型";
          const checkedTitle = providerChecks.length
            ? providerChecks.map((check) => `${check.model || "默认模型"}：${check.error || (check.ready ? "检查通过" : "状态未知")}`).join("；")
            : provider.model || "默认模型";
          const statusClass = policyDisabled
            ? "failed"
            : checkedReady === false
              ? "failed"
              : checkedReady === true
                ? "ready"
                : provider.configured
                  ? "configured"
                  : "missing";
          const statusLabel = policyDisabled
            ? "策略禁用"
            : checkedFailed
              ? "检查失败"
              : checkedReady === true
                ? "检查通过"
                : provider.configured
                  ? "待检查"
                  : "未配置";
          return (
            <div className={providerId === "openai" ? "provider-catalog-row openai" : "provider-catalog-row"} key={providerId}>
              <span className={`provider-status-dot ${statusClass}`} />
              <span className="provider-catalog-copy">
                <strong>{provider.name || providerId}</strong>
                <small title={checkedTitle}>
                  {checkedModelLabel}
                </small>
              </span>
              <span className={`provider-config-state ${statusClass}`}>{statusLabel}</span>
              <b>{routeCount}</b>
            </div>
          );
        })}
      </div>

      {preflightChecks.length ? (
        <div className="provider-preflight-routes" aria-label="模型路由检查明细">
          {preflightChecks.map((check) => {
            const provider = providerById.get(check.id);
            const detail = [
              check.memberCount ? `${check.memberCount} 位成员` : "未分配成员",
              check.latencyMs ? `${check.latencyMs} ms` : "",
              check.policyDisabled ? "策略禁用，未调用" : "只读本机配置，未调用模型",
            ].filter(Boolean).join(" · ");
            return (
              <div className={check.ready ? "provider-preflight-route ready" : "provider-preflight-route failed"} key={check.key}>
                {check.ready ? <CheckCircle2 size={13} /> : <AlertTriangle size={13} />}
                <span>
                  <strong>{provider?.name || check.id} · {check.model || "默认模型"}</strong>
                  <small title={check.memberNames.join("、")}>{detail}</small>
                </span>
                <em>{check.ready ? "通过" : "未通过"}</em>
              </div>
            );
          })}
        </div>
      ) : null}

      <div className="provider-routing-actions">
        {["deepseek", "doubao"].map((providerId) => {
          const provider = providerById.get(providerId);
          const loading = action === `route:${providerId}`;
          const policyDisabled = provider?.policy_disabled === true;
          return (
            <button
              className="secondary compact"
              type="button"
              key={providerId}
              disabled={busy || roundRunning || !enabledMembers || !providerIsAvailable(provider)}
              onClick={() => routeAll(providerId)}
              title={policyDisabled
                ? `${provider?.name || providerId} 已被服务端策略禁用`
                : provider?.configured === false
                  ? `${provider.name || providerId} 尚未配置`
                  : `将全部启用成员切换到 ${provider?.name || providerId}`}
            >
              {loading ? <LoaderCircle className="spin" size={14} /> : <Route size={14} />}
              全部切到 {provider?.name || providerId}
            </button>
          );
        })}
        <button
          className="secondary compact provider-preflight-button"
          type="button"
          disabled={busy || roundRunning || !enabledMembers}
          onClick={runPreflight}
        >
          {action === "preflight" ? <LoaderCircle className="spin" size={14} /> : <RefreshCw size={14} />}
          本机配置检查
        </button>
      </div>

      {feedback ? (
        <div className={`provider-routing-feedback ${feedback.tone}`} role="status" aria-live="polite">
          {feedback.tone === "success" ? <CheckCircle2 size={14} /> : <AlertTriangle size={14} />}
          <span>{feedback.text}</span>
        </div>
      ) : null}
      <p className="provider-routing-note">
        此处只检查本机配置，不调用模型；真实连通性检查须在你确认正式新轮后才会计入调用上限。
        正式轮次确认后，本轮成员与主持人的 Provider/模型路由会被封印；中途编辑的新路由仅从下一轮生效。
      </p>
    </section>
  );
}
