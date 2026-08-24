import {
  AlertTriangle,
  CheckCircle2,
  LoaderCircle,
  Network,
  RefreshCw,
  Route,
  ShieldCheck,
} from "lucide-react";
import { memo, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  buildProviderRoutingPresentation,
  normalizeProviderPreflight,
  providerIsAvailable,
  providerRoutingResultFeedback,
} from "../providerRouting";
import "../styles/provider-routing.css";

const EMPTY_LIST = Object.freeze([]);

export const ProviderRoutingPanel = memo(function ProviderRoutingPanel({
  room,
  members = EMPTY_LIST,
  providers = EMPTY_LIST,
  roundRunning = false,
  routingBusy = false,
  onRouteMembers,
  onRunPreflight,
}) {
  const titleId = useId();
  const roomId = String(room?.id || "");
  const [action, setAction] = useState("");
  const [feedback, setFeedback] = useState(null);
  const [preflight, setPreflight] = useState(null);
  const operationEpochRef = useRef(0);
  const roomRef = useRef(roomId);
  roomRef.current = roomId;
  const view = useMemo(
    () => buildProviderRoutingPresentation({
      members,
      providers,
      preflightChecks: preflight?.checks || [],
    }),
    [members, preflight?.checks, providers],
  );
  const enabledMembers = view.routeSummary.total;
  const busy = routingBusy || Boolean(action);

  useEffect(() => {
    operationEpochRef.current += 1;
    setAction("");
    setFeedback(null);
    setPreflight(null);
    return () => {
      operationEpochRef.current += 1;
    };
  }, [roomId]);

  useEffect(() => {
    setPreflight(null);
  }, [view.routeFingerprint]);

  const routeAll = async (target) => {
    if (
      !target
      || !providerIsAvailable(target.provider)
      || busy
      || roundRunning
      || typeof onRouteMembers !== "function"
    ) return;
    const operationEpoch = operationEpochRef.current + 1;
    operationEpochRef.current = operationEpoch;
    const operationRoomId = roomId;
    setAction("route:" + target.id);
    setFeedback(null);
    try {
      const result = await onRouteMembers(target.id);
      if (
        operationEpochRef.current !== operationEpoch
        || roomRef.current !== operationRoomId
      ) return;
      setFeedback(providerRoutingResultFeedback(result, target.name));
    } catch (error) {
      if (
        operationEpochRef.current !== operationEpoch
        || roomRef.current !== operationRoomId
      ) return;
      setFeedback({
        tone: "error",
        text: "批量切换未完成：" + (error?.message || "未知错误"),
      });
    } finally {
      if (
        operationEpochRef.current === operationEpoch
        && roomRef.current === operationRoomId
      ) setAction("");
    }
  };

  const runPreflight = async () => {
    if (
      busy
      || roundRunning
      || !roomId
      || typeof onRunPreflight !== "function"
    ) return;
    const operationEpoch = operationEpochRef.current + 1;
    operationEpochRef.current = operationEpoch;
    const operationRoomId = roomId;
    setAction("preflight");
    setFeedback(null);
    try {
      const data = await onRunPreflight();
      if (
        operationEpochRef.current !== operationEpoch
        || roomRef.current !== operationRoomId
      ) return;
      const result = normalizeProviderPreflight(data);
      setPreflight(result);
      const failed = result.checks.filter((check) => check.ready === false);
      setFeedback(result.ready
        ? {
            tone: "success",
            text: "本机配置检查通过："
              + result.checks.length
              + " 条模型路由已配置，作用域已封闭且外部调用为 0。正式连通性只会在确认新轮后检查。",
          }
        : {
            tone: "error",
            text: result.issues[0]?.message
              || (failed.length
                ? "本机配置检查未通过："
                  + failed.map((check) => check.error || check.id).slice(0, 3).join("；")
                : "本机配置检查未通过，请检查成员模型路由。"),
          });
    } catch (error) {
      if (
        operationEpochRef.current !== operationEpoch
        || roomRef.current !== operationRoomId
      ) return;
      const message = error?.message || "未知错误";
      const endpointMissing = /接口不存在|404|not found/i.test(message);
      setFeedback({
        tone: "warning",
        text: endpointMissing
          ? "后端本机配置检查尚未启用；当前未发起模型请求。"
          : "本机配置检查未完成：" + message,
      });
    } finally {
      if (
        operationEpochRef.current === operationEpoch
        && roomRef.current === operationRoomId
      ) setAction("");
    }
  };

  return (
    <section
      className="inspector-section provider-routing-section provider-routing-console"
      id="inspector-providers"
      data-routing-busy={busy ? "true" : "false"}
      aria-labelledby={titleId}
      aria-busy={busy}
    >
      <header className="provider-routing-heading">
        <div>
          <small>ROUTE CONTROL / LOCAL PREFLIGHT</small>
          <strong id={titleId}><Network size={16} aria-hidden="true" />模型路由与本机配置</strong>
          <p>区分已分配、已配置与已检查；本面板不会把本机配置状态表述为真实连通性。</p>
        </div>
        <em><span>ENABLED MEMBERS</span>{enabledMembers}</em>
      </header>

      <div className="provider-routing-stats" role="list" aria-label="模型路由摘要指标">
        {view.stats.map((stat) => (
          <span className={stat.key} key={stat.key} role="listitem">
            <small>{stat.label}</small><strong>{stat.value}</strong>
          </span>
        ))}
      </div>

      <div className="provider-route-summary" role="list" aria-label={"本轮预计模型路由：" + view.routeSummary.label}>
        {view.routeSummary.entries.length ? view.routeSummary.entries.map((entry) => (
          <span
            className={"provider-count-chip " + (!entry.available ? "unavailable" : "available")}
            data-provider-id={entry.id}
            key={entry.key}
            role="listitem"
            title={(entry.model || "默认模型") + " · " + (
              entry.policyDisabled
                ? "服务端策略禁用"
                : entry.id === "unassigned"
                  ? "尚未分配执行器"
                  : entry.configured
                    ? "本机已配置；确认新轮后检查连通性"
                    : "未配置"
            )}
          >
            {entry.available
              ? <Route size={12} aria-hidden="true" />
              : <AlertTriangle size={12} aria-hidden="true" />}
            <span>
              {entry.name}
              <small>{entry.model || "默认模型"}{entry.policyDisabled ? " · 策略禁用" : ""}</small>
            </span>
            <b>{entry.count}</b>
          </span>
        )) : <span className="provider-empty-route" role="listitem">当前没有启用成员</span>}
      </div>

      {view.warnings.map((warning) => (
        <div className="openai-route-warning" key={warning.key}>
          <AlertTriangle size={14} aria-hidden="true" />
          <span><strong>{warning.title}</strong><small>{warning.detail}</small></span>
        </div>
      ))}

      <div className="provider-catalog" role="list" aria-label="Provider 本机配置目录">
        {view.catalog.map((entry) => (
          <div
            className={"provider-catalog-row " + entry.status}
            data-provider-id={entry.id}
            key={entry.id}
            role="listitem"
          >
            <span className={"provider-status-dot " + entry.status} aria-hidden="true" />
            <span className="provider-catalog-copy">
              <strong>{entry.name}</strong>
              <small title={entry.title}>{entry.modelLabel}</small>
            </span>
            <span className={"provider-config-state " + entry.status}>{entry.statusLabel}</span>
            <b aria-label={entry.routeCount + " 位成员"}>{entry.routeCount}</b>
          </div>
        ))}
      </div>

      {preflight ? (
        <div
          className={"provider-preflight-envelope " + (preflight.ready ? "ready" : "failed")}
          role="status"
          aria-label={preflight.ready ? "本机预检合同通过" : "本机预检合同未通过"}
        >
          <header>
            <span>
              {preflight.ready
                ? <CheckCircle2 size={14} aria-hidden="true" />
                : <AlertTriangle size={14} aria-hidden="true" />}
              <strong>{preflight.ready ? "本机预检合同通过" : "本机预检合同未通过"}</strong>
            </span>
            <em>{preflight.checks.length} 条路由</em>
          </header>
          <div className="provider-preflight-contract">
            <span data-contract-state={preflight.scopeConfirmed ? "ready" : "failed"}>
              <small>作用域</small><strong>{preflight.verificationScope || "未确认"}</strong>
            </span>
            <span data-contract-state={preflight.noExternalCalls ? "ready" : "failed"}>
              <small>外部调用</small><strong>{preflight.externalCallCount ?? "无效"}</strong>
            </span>
          </div>
          {view.preflightRows.length ? (
            <div className="provider-preflight-routes" role="list" aria-label="模型路由检查明细">
              {view.preflightRows.map((check) => (
                <div
                  className={"provider-preflight-route " + (check.ready ? "ready" : "failed")}
                  key={check.key}
                  role="listitem"
                >
                  {check.ready
                    ? <CheckCircle2 size={13} aria-hidden="true" />
                    : <AlertTriangle size={13} aria-hidden="true" />}
                  <span>
                    <strong>{check.providerName} · {check.model || "默认模型"}</strong>
                    <small title={check.memberTitle}>{check.detail}</small>
                  </span>
                  <em>{check.ready ? "通过" : "未通过"}</em>
                </div>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="provider-routing-actions">
        {view.bulkTargets.map((target) => {
          const loading = action === "route:" + target.id;
          return (
            <button
              className="secondary compact"
              type="button"
              key={target.id}
              disabled={
                busy
                || roundRunning
                || !enabledMembers
                || !target.available
                || typeof onRouteMembers !== "function"
              }
              aria-busy={loading}
              onClick={() => routeAll(target)}
              title={target.policyDisabled
                ? target.name + " 已被服务端策略禁用"
                : target.provider?.configured === false
                  ? target.name + " 尚未配置"
                  : "将全部启用成员切换到 " + target.name}
            >
              {loading
                ? <LoaderCircle className="spin" size={14} aria-hidden="true" />
                : <Route size={14} aria-hidden="true" />}
              全部切到 {target.name}
            </button>
          );
        })}
        <button
          className="secondary compact provider-preflight-button"
          type="button"
          disabled={
            busy
            || roundRunning
            || !enabledMembers
            || typeof onRunPreflight !== "function"
          }
          aria-busy={action === "preflight"}
          onClick={runPreflight}
        >
          {action === "preflight"
            ? <LoaderCircle className="spin" size={14} aria-hidden="true" />
            : <RefreshCw size={14} aria-hidden="true" />}
          本机配置检查
        </button>
      </div>

      {feedback ? (
        <div className={"provider-routing-feedback " + feedback.tone} role={feedback.tone === "error" ? "alert" : "status"} aria-live="polite">
          {feedback.tone === "success"
            ? <CheckCircle2 size={14} aria-hidden="true" />
            : <AlertTriangle size={14} aria-hidden="true" />}
          <span>{feedback.text}</span>
        </div>
      ) : null}
      <p className="provider-routing-note">
        <ShieldCheck size={14} aria-hidden="true" />
        <span>
          <strong>检查边界</strong>
          此处只允许本机配置预检，作用域必须为 local_configuration_only 且外部调用数必须为 0。
          真实连通性检查只会在你确认正式新轮后计入调用上限；当轮路由一经封印，中途编辑仅从下一轮生效。
        </span>
      </p>
    </section>
  );
});
