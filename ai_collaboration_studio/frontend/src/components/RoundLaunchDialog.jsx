import {
  AlertTriangle,
  CheckCircle2,
  LoaderCircle,
  ShieldCheck,
  X,
} from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import {
  buildRoundLaunchAuthorizationPayload,
  isValidClientRoundRequestId,
  normalizeRoundLaunchPlan,
  PROVIDER_CALL_LIMIT_MAX,
  PROVIDER_CALL_LIMIT_MIN,
  roundLaunchAuthorizationState,
} from "../roundLaunchPlan";
import { useModalFocus } from "../useModalFocus";

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

const styles = Object.freeze({
  dialog: {
    width: "min(760px, 100%)",
    maxHeight: "calc(var(--visual-viewport-height, 100dvh) - 24px)",
    overflowX: "hidden",
  },
  body: {
    display: "grid",
    gap: 14,
    padding: "14px 16px 18px",
  },
  objective: {
    margin: 0,
    color: "#344054",
    fontSize: 13,
    lineHeight: 1.65,
    whiteSpace: "pre-wrap",
    overflowWrap: "anywhere",
  },
  grid: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fit, minmax(min(220px, 100%), 1fr))",
    gap: 10,
  },
  card: {
    minWidth: 0,
    padding: 12,
    border: "1px solid #d9e0e8",
    borderRadius: 6,
    background: "#f8fafc",
  },
  sectionTitle: {
    display: "block",
    marginBottom: 7,
    color: "#202939",
    fontSize: 12,
  },
  muted: {
    margin: 0,
    color: "#667085",
    fontSize: 11,
    lineHeight: 1.55,
    overflowWrap: "anywhere",
  },
  mutedBlock: {
    display: "block",
    margin: 0,
    color: "#667085",
    fontSize: 11,
    lineHeight: 1.55,
    overflowWrap: "anywhere",
  },
  count: {
    display: "block",
    marginBottom: 3,
    color: "#1459b8",
    fontSize: 24,
    lineHeight: 1.1,
  },
  subBudget: {
    display: "grid",
    gap: 5,
    marginTop: 10,
    padding: "8px 9px",
    border: "1px solid #c9d9eb",
    borderRadius: 5,
    background: "#f2f7fd",
  },
  subBudgetHead: {
    display: "flex",
    flexWrap: "wrap",
    alignItems: "center",
    justifyContent: "space-between",
    gap: "5px 9px",
  },
  subBudgetCode: {
    color: "#315f91",
    fontSize: 10,
    fontWeight: 700,
  },
  subBudgetValue: {
    color: "#244d78",
    fontSize: 11,
  },
  list: {
    display: "grid",
    gap: 7,
    margin: 0,
    padding: 0,
    listStyle: "none",
  },
  listItem: {
    display: "flex",
    minWidth: 0,
    flexWrap: "wrap",
    alignItems: "center",
    justifyContent: "space-between",
    gap: "5px 10px",
    padding: "8px 9px",
    border: "1px solid #e2e7ed",
    borderRadius: 5,
    background: "#fff",
    fontSize: 11,
  },
  routeText: {
    minWidth: 0,
    flex: "1 1 180px",
    overflowWrap: "anywhere",
  },
  status: {
    flex: "0 0 auto",
    color: "#365b89",
    fontStyle: "normal",
    fontWeight: 700,
  },
  statusReady: {
    flex: "0 0 auto",
    color: "#24684d",
    fontStyle: "normal",
    fontWeight: 700,
  },
  statusUnavailable: {
    flex: "0 0 auto",
    color: "#8b2f2f",
    fontStyle: "normal",
    fontWeight: 700,
  },
  warning: {
    display: "flex",
    alignItems: "flex-start",
    gap: 8,
    padding: 10,
    border: "1px solid #e6c978",
    borderRadius: 5,
    color: "#704e00",
    background: "#fff9e8",
    fontSize: 12,
    lineHeight: 1.55,
  },
  error: {
    display: "flex",
    alignItems: "flex-start",
    gap: 8,
    padding: 10,
    border: "1px solid #e0aaaa",
    borderRadius: 5,
    color: "#8b2f2f",
    background: "#fff5f5",
    fontSize: 12,
    lineHeight: 1.55,
  },
  safety: {
    display: "flex",
    alignItems: "flex-start",
    gap: 10,
    padding: 12,
    border: "1px solid #b9d9cb",
    borderRadius: 6,
    color: "#245844",
    background: "#f2fbf7",
  },
  focus: {
    display: "grid",
    gap: 6,
    padding: 12,
    border: "1px solid #b9cae7",
    borderRadius: 6,
    color: "#244d78",
    background: "#f4f8fd",
  },
  footer: {
    flexWrap: "wrap",
  },
});

function providerLabel(provider) {
  return PROVIDER_LABELS[provider] || provider || "未知 Provider";
}

function blockerLabel(blocker) {
  const base = BLOCKER_LABELS[blocker.code] || `未识别的阻断项：${blocker.code}`;
  const route = [blocker.provider ? providerLabel(blocker.provider) : "", blocker.model]
    .filter(Boolean)
    .join(" / ");
  return route ? `${base}（${route}）` : base;
}

function initialCallLimit(plan, requestedLimit) {
  if (
    Number.isInteger(requestedLimit)
    && requestedLimit >= PROVIDER_CALL_LIMIT_MIN
    && requestedLimit <= PROVIDER_CALL_LIMIT_MAX
  ) return requestedLimit;
  const recommended = plan.calls.recommended_provider_calls;
  return recommended >= PROVIDER_CALL_LIMIT_MIN && recommended <= PROVIDER_CALL_LIMIT_MAX
    ? recommended
    : PROVIDER_CALL_LIMIT_MIN;
}

export function RoundLaunchDialog({
  open,
  plan,
  clientRoundRequestId,
  initialMaxProviderCalls,
  loading = false,
  busy = false,
  error = "",
  restoreFocusRef,
  onClose,
  onConfirm,
  onRetry,
}) {
  const titleId = useId();
  const descriptionId = useId();
  const dialogRef = useRef(null);
  const closeButtonRef = useRef(null);
  const normalizedPlan = useMemo(() => normalizeRoundLaunchPlan(plan), [plan]);
  const [callLimit, setCallLimit] = useState("");
  const [localError, setLocalError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const effectiveBusy = busy || submitting;
  const numericCallLimit = callLimit === "" ? Number.NaN : Number(callLimit);
  const authorization = useMemo(
    () => roundLaunchAuthorizationState(normalizedPlan, numericCallLimit),
    [normalizedPlan, numericCallLimit],
  );
  const requestIdReady = isValidClientRoundRequestId(clientRoundRequestId);
  const roundContextEntries = normalizedPlan.round_context_authorizations?.contexts || [];
  const projectContextEntry = roundContextEntries.find((entry) => (
    entry.owner_pack_id === "project_round_focus"
    && entry.port_id === "core.round.context/v1"
  ));
  const footballContextEntry = roundContextEntries.find((entry) => (
    entry.owner_pack_id === "football_research_readonly"
    && entry.port_id === "core.football.match_context/v1"
  ));
  const stockContextEntry = roundContextEntries.find((entry) => (
    entry.owner_pack_id === "stock_research_readonly"
    && entry.port_id === "core.market.readonly_context/v1"
  ));
  const focusAuthorization = normalizedPlan.project_round_focus_authorization
    || projectContextEntry?.request
    || null;
  const focusArtifactBinding = focusAuthorization?.artifact_binding || null;
  const footballContextRequest = footballContextEntry?.request || null;
  const footballAuthorization = footballContextRequest?.authorization || null;
  const footballContract = footballContextRequest?.payload || null;
  const footballIdentity = footballContract?.match_identity || null;
  const stockContextRequest = stockContextEntry?.request || null;
  const stockAuthorization = stockContextRequest?.authorization || null;
  const stockContract = stockContextRequest?.payload || null;
  const stockScopeSymbols = stockContract?.stock_room_scope?.symbols || [];
  const canSubmit = authorization.canConfirm
    && requestIdReady
    && typeof onConfirm === "function"
    && Boolean(plan)
    && !loading
    && !error
    && !effectiveBusy;
  const routeByKey = useMemo(
    () => new Map(normalizedPlan.preflight_routes.map((route) => [
      `${route.provider}\u0000${route.model}`,
      route,
    ])),
    [normalizedPlan.preflight_routes],
  );

  useEffect(() => {
    if (!open) return;
    setCallLimit(String(initialCallLimit(normalizedPlan, initialMaxProviderCalls)));
    setLocalError("");
    setSubmitting(false);
  }, [initialMaxProviderCalls, normalizedPlan.plan_hash, open]);

  useModalFocus({
    open,
    containerRef: dialogRef,
    initialFocusRef: closeButtonRef,
    restoreFallbackRef: restoreFocusRef,
    onClose: effectiveBusy ? null : onClose,
  });

  useEffect(() => {
    if (open && effectiveBusy) dialogRef.current?.focus({ preventScroll: true });
  }, [effectiveBusy, open]);

  if (!open) return null;

  const handleSubmit = async (event) => {
    event.preventDefault();
    setLocalError("");
    try {
      const payload = buildRoundLaunchAuthorizationPayload(normalizedPlan, {
        clientRoundRequestId,
        maxProviderCalls: numericCallLimit,
      });
      if (typeof onConfirm !== "function") throw new Error("确认处理器尚未准备好。");
      setSubmitting(true);
      await onConfirm(payload);
    } catch (submitError) {
      setLocalError(submitError?.message || "启动确认失败。");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="dialog-backdrop round-launch-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !effectiveBusy) onClose?.();
      }}
    >
      <form
        ref={dialogRef}
        className="dialog round-launch-dialog"
        style={styles.dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        aria-busy={effectiveBusy || loading}
        tabIndex={-1}
        onSubmit={handleSubmit}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <span>
            <ShieldCheck size={18} aria-hidden="true" />
            <strong id={titleId}>启动前确认</strong>
          </span>
          <button
            ref={closeButtonRef}
            type="button"
            className="icon-button"
            aria-label="关闭启动前确认"
            onClick={onClose}
            disabled={effectiveBusy}
          >
            <X size={18} />
          </button>
        </header>

        <div className="round-launch-body" style={styles.body}>
          {loading ? (
            <div className="round-launch-loading" role="status">
              <LoaderCircle className="spin" size={20} aria-hidden="true" />
              <span>
                <strong>正在读取冻结计划</strong>
                <small id={descriptionId}>这里只读取房间与本机 Provider 状态，不会发起 Provider 调用。</small>
              </span>
            </div>
          ) : !plan ? (
            <div style={styles.error} role="alert">
              <AlertTriangle size={17} aria-hidden="true" />
              <span id={descriptionId}>{error || "启动确认单不可用，请重新读取。"}</span>
            </div>
          ) : (
            <>
          <section aria-label="本轮目标">
            <strong style={styles.sectionTitle}>本轮目标</strong>
            <p id={descriptionId} style={styles.objective}>
              {normalizedPlan.objective || "目标不可用"}
            </p>
          </section>

          {focusAuthorization ? (
            <section style={styles.focus} aria-label="冻结的下一轮项目焦点">
              <strong style={styles.sectionTitle}>冻结的项目焦点上下文</strong>
              <p style={styles.muted}>
                {focusArtifactBinding?.status === "exact"
                  ? `确认产物 ${focusArtifactBinding.artifact_id} · v${focusArtifactBinding.artifact_version}`
                  : "暂无确认产物 · bootstrap 上下文"}
              </p>
              <p style={styles.muted} title={focusAuthorization.preview_sha256}>
                预览封印：{focusAuthorization.preview_sha256.slice(0, 10)}…{focusAuthorization.preview_sha256.slice(-8)}
              </p>
              <p style={styles.muted}>
                确认启动时服务端会再次核验精确来源；若产物或预览变化，本轮将在任何 Provider 或市场调用前阻断。
              </p>
              <p style={styles.muted}>
                这份授权只用于预填可编辑目标；不会自动开始、点名成员或替代你的最终决定。
              </p>
            </section>
          ) : null}

          {footballAuthorization && footballContract ? (
            <section style={styles.focus} aria-label="冻结的足球只读上下文">
              <strong style={styles.sectionTitle}>冻结的足球只读上下文</strong>
              <p style={styles.muted}>
                联赛：{footballIdentity?.competition?.value || "不可用"}
                {footballIdentity?.competition_id?.value
                  ? ` · ${footballIdentity.competition_id.value}`
                  : ""}
              </p>
              <p style={styles.muted}>
                比赛：{footballAuthorization.match_id || "不可用"}
                {footballIdentity?.season?.value ? ` · ${footballIdentity.season.value}` : ""}
              </p>
              <p style={styles.muted}>
                开球 UTC：{footballIdentity?.kickoff_utc?.value || "不可用"}
                {footballIdentity?.venue?.value ? ` · ${footballIdentity.venue.value}` : ""}
              </p>
              <p style={styles.muted}>
                数据截止：{footballAuthorization.data_cutoff_utc || "不可用"}
              </p>
              <p style={styles.muted} title={footballAuthorization.contract_sha256}>
                合同封印：{footballAuthorization.contract_sha256?.slice(0, 10)}…
                {footballAuthorization.contract_sha256?.slice(-8)}
              </p>
              <p style={styles.muted}>
                服务端会在任何 Provider 调用前，用同一 SQLite 事务重新核验材料版本、内容哈希、
                registry 与生命周期；任一漂移都会阻断本轮。
              </p>
              <p style={styles.muted}>
                不生成未来胜率，不显示概率校准指标；赔率仅作代理，不连接钱包、不执行投注或自动下注。
              </p>
            </section>
          ) : null}

          {stockAuthorization && stockContract ? (
            <section style={styles.focus} aria-label="冻结的股票只读上下文">
              <strong style={styles.sectionTitle}>冻结的股票只读上下文</strong>
              <p style={styles.muted}>
                显式股票池：{stockScopeSymbols.length
                  ? stockScopeSymbols.join("、")
                  : "不可用"}
              </p>
              <p style={styles.muted}>
                数据截止：{stockAuthorization.data_cutoff_utc || "不可用"}
              </p>
              <p style={styles.muted} title={stockAuthorization.contract_sha256}>
                合同封印：{stockAuthorization.contract_sha256?.slice(0, 10)}…
                {stockAuthorization.contract_sha256?.slice(-8)}
              </p>
              <p style={styles.muted} title={stockAuthorization.stock_room_scope_sha256}>
                股票池封印：{stockAuthorization.stock_room_scope_sha256?.slice(0, 10)}…
                {stockAuthorization.stock_room_scope_sha256?.slice(-8)}
              </p>
              <p style={styles.muted}>
                服务端会在任何 Provider 或市场读取前重新核验股票池、材料版本、合同与 registry；
                任一漂移都会阻断本轮。
              </p>
              <p style={styles.muted}>
                只读研究，不发现或扩写股票池，不连接钱包、不下单、不自动交易，也不替代你的最终决定。
              </p>
            </section>
          ) : null}

          <div style={styles.grid}>
            <section style={styles.card} aria-label="建议授权 Provider 调用次数">
              <strong style={styles.count}>{normalizedPlan.calls.recommended_provider_calls}</strong>
              <span style={styles.sectionTitle}>建议授权额度</span>
              <p style={styles.muted}>
                核心成功路径 {normalizedPlan.calls.core_success_path_calls} 次 ·
                歧义主持预留 {normalizedPlan.calls.recommended_director_calls} 次 ·
                可选纪要 {normalizedPlan.calls.optional_artifact_calls} 次。
              </p>
              <p style={styles.muted}>这是授权建议，不是预计用量或完成保证。</p>
              <div
                style={styles.subBudget}
                aria-label={`round_director 独立主持子预算 ${normalizedPlan.calls.recommended_director_calls} 次`}
              >
                <span style={styles.subBudgetHead}>
                  <code style={styles.subBudgetCode}>round_director · 独立主持子预算</code>
                  <strong style={styles.subBudgetValue}>
                    {normalizedPlan.calls.recommended_director_calls} 次硬子预算
                  </strong>
                </span>
                <small style={styles.mutedBlock}>
                  确认后随冻结计划写入；它包含在全局硬上限内，只会进一步收紧主持调用，不会扩大授权。
                </small>
              </div>
            </section>
            <section style={styles.card} aria-label="设置 Provider 调用次数绝对上限">
              <label htmlFor={`${titleId}-call-limit`}>
                Provider 调用绝对硬上限
                <input
                  id={`${titleId}-call-limit`}
                  type="number"
                  min={PROVIDER_CALL_LIMIT_MIN}
                  max={PROVIDER_CALL_LIMIT_MAX}
                  step="1"
                  inputMode="numeric"
                  value={callLimit}
                  onChange={(event) => setCallLimit(event.target.value)}
                  disabled={effectiveBusy}
                  aria-invalid={!authorization.validLimit}
                />
              </label>
              <p style={styles.muted}>覆盖本轮全部调用类型，包括运行中用户插话；允许 {PROVIDER_CALL_LIMIT_MIN}–{PROVIDER_CALL_LIMIT_MAX}。主持子预算不会叠加在此上限之外。</p>
            </section>
          </div>

          {authorization.warning ? (
            <div style={styles.warning} role="alert">
              <AlertTriangle size={17} aria-hidden="true" />
              <span>{authorization.warning.message}不能保证完整走完既定流程。</span>
            </div>
          ) : null}

          <div style={styles.grid}>
            <section style={styles.card} aria-label="主持人路由">
              <strong style={styles.sectionTitle}>主持人</strong>
              <p style={styles.objective}>{normalizedPlan.moderator.name || "不可用"}</p>
              <p style={styles.muted}>{normalizedPlan.moderator.identity || "未说明身份"}</p>
              <p style={styles.muted}>
                {providerLabel(normalizedPlan.moderator.provider)} / {normalizedPlan.moderator.model || "默认模型"}
              </p>
            </section>
            <section style={styles.card} aria-label="计划校验信息">
              <strong style={styles.sectionTitle}>冻结计划</strong>
              <p style={styles.muted}>模式：{normalizedPlan.room.discussion_mode === "dynamic" ? "动态主持" : "顺序讨论"}</p>
              <p style={styles.muted}>成员：{normalizedPlan.members.length} 位</p>
              <p style={styles.muted}>
                主持结构上界 {normalizedPlan.calls.maximum_director_calls} 次 ·
                正式路径保守上界 {normalizedPlan.calls.formal_path_conservative_upper_bound} 次
              </p>
              <p style={styles.muted}>上界不是预测，且不包含运行中用户插话。</p>
              <p style={styles.muted}>标识：{normalizedPlan.plan_hash || "不可用"}</p>
            </section>
          </div>

          <section aria-label="各 Provider 建议授权分摊">
            <strong style={styles.sectionTitle}>
              各 Provider 建议授权分摊 · 合计 {normalizedPlan.calls.projected_provider_calls_total} 次
            </strong>
            <ul style={styles.list}>
              {normalizedPlan.provider_call_projection.map((provider) => (
                <li key={provider.provider} style={styles.listItem}>
                  <span style={styles.routeText}>
                    <strong>{providerLabel(provider.provider)}</strong>
                    <small style={styles.mutedBlock}>
                      预检 {provider.projected_preflight_calls} · 成员 {provider.minimum_speaker_calls}
                      {" · "}主持预留 {provider.recommended_director_calls} · 纪要 {provider.optional_artifact_calls}
                      {provider.contingency_calls ? ` · 预留 ${provider.contingency_calls}` : ""}
                      {provider.skipped ? " · 已跳过" : provider.policy_disabled ? " · 本机策略停用" : ""}
                    </small>
                  </span>
                  <em style={styles.status}>{provider.projected_provider_calls} 次</em>
                </li>
              ))}
            </ul>
          </section>

          <details open>
            <summary><strong style={styles.sectionTitle}>成员路由（{normalizedPlan.members.length}）</strong></summary>
            <ul style={styles.list}>
              {normalizedPlan.members.map((member) => {
                const route = routeByKey.get(`${member.provider}\u0000${member.model}`);
                return (
                  <li key={member.id} style={styles.listItem}>
                    <span style={styles.routeText}>
                      <strong>{member.name}</strong>
                      <small style={styles.mutedBlock}>
                        {member.identity || member.stage} · {providerLabel(member.provider)} / {member.model || "默认模型"}
                      </small>
                    </span>
                    <em style={route?.callable ? styles.statusReady : styles.statusUnavailable}>
                      {route?.callable ? "可调用" : "不可调用"}
                    </em>
                  </li>
                );
              })}
            </ul>
          </details>

          <section style={styles.safety} aria-label="Futu 与执行边界">
            {normalizedPlan.safety.verified
              ? <CheckCircle2 size={19} aria-hidden="true" />
              : <AlertTriangle size={19} aria-hidden="true" />}
            <span>
              <strong style={styles.sectionTitle}>Futu 与执行边界</strong>
              <p style={styles.muted}>Futu 行情仅作为只读研究资料；本次确认不授予下单能力。</p>
              <p style={styles.muted}>
                执行能力：{normalizedPlan.safety.execution_capability === "none" ? "无" : "边界未验证"}；
                实盘交易：{normalizedPlan.safety.live_trading_allowed === false ? "禁止" : "边界未验证"}。
              </p>
            </span>
          </section>

          {normalizedPlan.blockers.length ? (
            <div style={styles.error} role="alert">
              <AlertTriangle size={17} aria-hidden="true" />
              <span>
                <strong>当前不能确认</strong>
                <ul>
                  {normalizedPlan.blockers.map((blocker, index) => (
                    <li key={`${blocker.code}-${blocker.provider}-${blocker.model}-${index}`}>
                      {blockerLabel(blocker)}
                    </li>
                  ))}
                </ul>
              </span>
            </div>
          ) : null}

          {!authorization.validLimit ? <div style={styles.error} role="alert">{authorization.error}</div> : null}
          {!requestIdReady ? <div style={styles.error} role="alert">启动请求标识尚未准备好。</div> : null}
          {error || localError ? <div style={styles.error} role="alert">{localError || error}</div> : null}
            </>
          )}
        </div>

        <footer style={styles.footer}>
          <button type="button" className="secondary" onClick={onClose} disabled={effectiveBusy}>取消</button>
          {!loading && !plan ? (
            <button type="button" className="primary" onClick={onRetry} disabled={effectiveBusy || typeof onRetry !== "function"}>
              重新读取
            </button>
          ) : (
            <button type="submit" className="primary" disabled={!canSubmit}>
              {effectiveBusy ? <LoaderCircle className="spin" size={15} aria-hidden="true" /> : <ShieldCheck size={15} aria-hidden="true" />}
              {effectiveBusy ? "确认中…" : loading ? "读取中…" : "确认并启动"}
            </button>
          )}
        </footer>
      </form>
    </div>
  );
}
