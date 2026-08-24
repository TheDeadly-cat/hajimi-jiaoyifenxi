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
import {
  blockerLabel,
  initialCallLimit,
  providerLabel,
  roundLaunchErrorMessage,
  roundLaunchNumericCallLimit,
  roundLaunchShortHash,
  roundLaunchSubmitControl,
} from "../roundLaunchUi";
import "../styles/round-launch.css";

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
  const operationRef = useRef(0);
  const normalizedPlan = useMemo(() => normalizeRoundLaunchPlan(plan), [plan]);
  const [callLimit, setCallLimit] = useState("");
  const [memberRoutesOpen, setMemberRoutesOpen] = useState(false);
  const [localError, setLocalError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const effectiveBusy = busy || submitting;
  const numericCallLimit = roundLaunchNumericCallLimit(callLimit);
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
  const externalError = error
    ? roundLaunchErrorMessage(error, "启动确认出现无法识别的错误。")
    : "";
  const submitControl = useMemo(() => roundLaunchSubmitControl({
    authorization,
    requestIdReady,
    planPresent: Boolean(plan),
    loading,
    externalError,
    busy: effectiveBusy,
    confirmHandlerAvailable: typeof onConfirm === "function",
  }), [authorization, effectiveBusy, externalError, loading, onConfirm, plan, requestIdReady]);
  const canSubmit = submitControl.canSubmit;
  const passedPermitCount = submitControl.checks.filter((check) => check.passed).length;
  const permitCount = submitControl.checks.length;
  const blockerEntries = useMemo(() => {
    const occurrences = new Map();
    return normalizedPlan.blockers.map((blocker) => {
      const identity = [
        blocker.code || "UNKNOWN_BLOCKER",
        blocker.provider || "local",
        blocker.model || "default",
        blocker.member_id || blocker.member || "room",
        blocker.reason || blocker.message || blockerLabel(blocker),
      ].join("\u0000");
      const occurrence = occurrences.get(identity) || 0;
      occurrences.set(identity, occurrence + 1);
      return { blocker, key: `${identity}\u0000${occurrence}` };
    });
  }, [normalizedPlan.blockers]);
  const launchSummaryTone = loading || effectiveBusy
    ? "loading"
    : canSubmit ? "review" : "blocked";
  const launchSummaryLabel = effectiveBusy
    ? "正在提交用户确认"
    : loading
      ? "正在读取冻结计划"
      : !plan
        ? "启动确认单不可用"
        : canSubmit
          ? "许可条件已齐备，等待你的最终确认"
          : "尚不能提交启动确认";
  const launchSummaryDetail = localError || externalError || (
    effectiveBusy
      ? "服务端正在重新核验冻结来源，请勿重复提交。"
      : loading
        ? "读取过程不会发起 Provider 调用。"
        : !plan
          ? "重新读取后仍需完整通过全部许可条件。"
          : canSubmit
            ? `冻结计划 ${roundLaunchShortHash(normalizedPlan.plan_hash)} · 本轮最多 ${numericCallLimit} 次 Provider 调用；不包含交易权限。`
            : submitControl.instruction
  );
  const routeByKey = useMemo(
    () => new Map(normalizedPlan.preflight_routes.map((route) => [
      `${route.provider}\u0000${route.model}`,
      route,
    ])),
    [normalizedPlan.preflight_routes],
  );

  useEffect(() => {
    operationRef.current += 1;
    setLocalError("");
    setSubmitting(false);
    if (!open) return;
    setCallLimit(String(initialCallLimit(normalizedPlan, initialMaxProviderCalls)));
    setMemberRoutesOpen(false);
  }, [clientRoundRequestId, initialMaxProviderCalls, normalizedPlan.plan_hash, open]);

  useEffect(() => () => { operationRef.current += 1; }, []);

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

  useEffect(() => {
    if (!open) return undefined;
    const containDialogFocus = (event) => {
      const dialog = dialogRef.current;
      if (!dialog || dialog.contains(event.target)) return;
      const focusTarget = effectiveBusy ? dialog : closeButtonRef.current;
      focusTarget?.focus({ preventScroll: true });
    };
    document.addEventListener("focusin", containDialogFocus, true);
    return () => document.removeEventListener("focusin", containDialogFocus, true);
  }, [effectiveBusy, open]);

  if (!open) return null;

  const handleSubmit = async (event) => {
    event.preventDefault();
    setLocalError("");
    if (!submitControl.canSubmit) {
      setLocalError(`启动许可未完成：${submitControl.instruction}`);
      return;
    }
    const sequence = operationRef.current + 1;
    operationRef.current = sequence;
    try {
      const payload = buildRoundLaunchAuthorizationPayload(normalizedPlan, {
        clientRoundRequestId,
        maxProviderCalls: numericCallLimit,
      });
      if (typeof onConfirm !== "function") throw new Error("确认处理器尚未准备好。");
      setSubmitting(true);
      await onConfirm(payload);
      if (operationRef.current !== sequence) return;
    } catch (submitError) {
      if (operationRef.current !== sequence) return;
      setLocalError(roundLaunchErrorMessage(submitError));
    } finally {
      if (operationRef.current === sequence) setSubmitting(false);
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
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        aria-busy={effectiveBusy || loading}
        data-launch-state={submitControl.phase}
        tabIndex={-1}
        onSubmit={handleSubmit}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <div className="round-launch-title">
            <ShieldCheck size={18} aria-hidden="true" />
            <div><small>CONTROLLED ROUND AUTHORIZATION</small><h2 id={titleId}>启动前确认</h2></div>
          </div>
          <button
            ref={closeButtonRef}
            type="button"
            className="icon-button"
            aria-label="关闭启动前确认"
            onClick={onClose}
            disabled={effectiveBusy}
          >
            <X aria-hidden="true" size={18} />
          </button>
        </header>

        <div className="round-launch-body">
          {loading ? (
            <div className="round-launch-loading" role="status">
              <LoaderCircle className="spin" size={20} aria-hidden="true" />
              <span>
                <strong>正在读取冻结计划</strong>
                <small id={descriptionId}>这里只读取房间与本机 Provider 状态，不会发起 Provider 调用。</small>
              </span>
            </div>
          ) : !plan ? (
            <div className="round-launch-alert error" role="alert">
              <AlertTriangle size={17} aria-hidden="true" />
              <span id={descriptionId}>{externalError || "启动确认单不可用，请重新读取。"}</span>
            </div>
          ) : (
            <>
          <section aria-label="本轮目标">
            <h3 className="round-launch-section-title">本轮目标</h3>
            <p id={descriptionId} className="round-launch-objective">
              {normalizedPlan.objective || "目标不可用"}
            </p>
          </section>

          <section className="round-launch-snapshot" aria-label="启动授权摘要">
            <div className="round-launch-snapshot-grid" role="list">
              <span className={`round-launch-snapshot-item ${launchSummaryTone}`} role="listitem">
                <small>TECHNICAL PERMIT</small>
                <strong>
                  <data value={passedPermitCount}>{passedPermitCount}</data>
                  <span aria-hidden="true"> / </span>
                  <data value={permitCount}>{permitCount}</data>
                </strong>
                <em>{canSubmit ? "技术条件已通过，仍需你的确认" : "仍有启动许可未完成"}</em>
              </span>
              <span className="round-launch-snapshot-item" role="listitem">
                <small>PROVIDER CEILING</small>
                <strong>
                  {authorization.validLimit
                    ? <><data value={numericCallLimit}>{numericCallLimit}</data> 次</>
                    : "待修正"}
                </strong>
                <em>全局绝对硬上限，不是预计用量</em>
              </span>
              <span className="round-launch-snapshot-item boundary" role="listitem">
                <small>EXECUTION BOUNDARY</small>
                <strong>只读研究</strong>
                <em>无下单、实盘或钱包权限</em>
              </span>
            </div>
            <p className="round-launch-boundary-note">
              <ShieldCheck size={16} aria-hidden="true" />
              本确认只授权这一轮冻结研究计划；服务端仍会在任何 Provider 或市场读取前重新核验来源。
            </p>
          </section>

          <section className="round-launch-permit" aria-label="启动许可清单">
            <header>
              <span>LAUNCH PERMIT</span>
              <strong><data value={passedPermitCount}>{passedPermitCount}</data>/{permitCount}</strong>
            </header>
            <ul>{submitControl.checks.map((check) => <li className={check.passed ? "passed" : "pending"} key={check.id}><span aria-hidden="true">{check.passed ? "✓" : "·"}</span>{check.label}</li>)}</ul>
            <p>{submitControl.instruction}</p>
          </section>

          {focusAuthorization ? (
            <section className="round-launch-context-card" aria-label="冻结的下一轮项目焦点">
              <h3 className="round-launch-section-title">冻结的项目焦点上下文</h3>
              <p className="round-launch-muted">
                {focusArtifactBinding?.status === "exact"
                  ? `确认产物 ${focusArtifactBinding.artifact_id} · v${focusArtifactBinding.artifact_version}`
                  : "暂无确认产物 · bootstrap 上下文"}
              </p>
              <p className="round-launch-muted" title={focusAuthorization.preview_sha256}>
                预览封印：{roundLaunchShortHash(focusAuthorization.preview_sha256)}
              </p>
              <p className="round-launch-muted">
                确认启动时服务端会再次核验精确来源；若产物或预览变化，本轮将在任何 Provider 或市场调用前阻断。
              </p>
              <p className="round-launch-muted">
                这份授权只用于预填可编辑目标；不会自动开始、点名成员或替代你的最终决定。
              </p>
            </section>
          ) : null}

          {footballAuthorization && footballContract ? (
            <section className="round-launch-context-card" aria-label="冻结的足球只读上下文">
              <h3 className="round-launch-section-title">冻结的足球只读上下文</h3>
              <p className="round-launch-muted">
                联赛：{footballIdentity?.competition?.value || "不可用"}
                {footballIdentity?.competition_id?.value
                  ? ` · ${footballIdentity.competition_id.value}`
                  : ""}
              </p>
              <p className="round-launch-muted">
                比赛：{footballAuthorization.match_id || "不可用"}
                {footballIdentity?.season?.value ? ` · ${footballIdentity.season.value}` : ""}
              </p>
              <p className="round-launch-muted">
                开球 UTC：{footballIdentity?.kickoff_utc?.value || "不可用"}
                {footballIdentity?.venue?.value ? ` · ${footballIdentity.venue.value}` : ""}
              </p>
              <p className="round-launch-muted">
                数据截止：{footballAuthorization.data_cutoff_utc || "不可用"}
              </p>
              <p className="round-launch-muted" title={footballAuthorization.contract_sha256}>
                合同封印：{roundLaunchShortHash(footballAuthorization.contract_sha256)}
              </p>
              <p className="round-launch-muted">
                服务端会在任何 Provider 调用前，用同一 SQLite 事务重新核验材料版本、内容哈希、
                registry 与生命周期；任一漂移都会阻断本轮。
              </p>
              <p className="round-launch-muted">
                不生成未来胜率，不显示概率校准指标；赔率仅作代理，不连接钱包、不执行投注或自动下注。
              </p>
            </section>
          ) : null}

          {stockAuthorization && stockContract ? (
            <section className="round-launch-context-card" aria-label="冻结的股票只读上下文">
              <h3 className="round-launch-section-title">冻结的股票只读上下文</h3>
              <p className="round-launch-muted">
                显式股票池：{stockScopeSymbols.length
                  ? stockScopeSymbols.join("、")
                  : "不可用"}
              </p>
              <p className="round-launch-muted">
                数据截止：{stockAuthorization.data_cutoff_utc || "不可用"}
              </p>
              <p className="round-launch-muted" title={stockAuthorization.contract_sha256}>
                合同封印：{roundLaunchShortHash(stockAuthorization.contract_sha256)}
              </p>
              <p className="round-launch-muted" title={stockAuthorization.stock_room_scope_sha256}>
                股票池封印：{roundLaunchShortHash(stockAuthorization.stock_room_scope_sha256)}
              </p>
              <p className="round-launch-muted">
                服务端会在任何 Provider 或市场读取前重新核验股票池、材料版本、合同与 registry；
                任一漂移都会阻断本轮。
              </p>
              <p className="round-launch-muted">
                只读研究，不发现或扩写股票池，不连接钱包、不下单、不自动交易，也不替代你的最终决定。
              </p>
            </section>
          ) : null}

          <div className="round-launch-grid">
            <section className="round-launch-card" aria-label="建议授权 Provider 调用次数">
              <strong className="round-launch-count">{normalizedPlan.calls.recommended_provider_calls}</strong>
              <span className="round-launch-section-title">建议授权额度</span>
              <p className="round-launch-muted">
                核心成功路径 {normalizedPlan.calls.core_success_path_calls} 次 ·
                歧义主持预留 {normalizedPlan.calls.recommended_director_calls} 次 ·
                可选纪要 {normalizedPlan.calls.optional_artifact_calls} 次。
              </p>
              <p className="round-launch-muted">这是授权建议，不是预计用量或完成保证。</p>
              <div
                className="round-launch-sub-budget"
                aria-label={`round_director 独立主持子预算 ${normalizedPlan.calls.recommended_director_calls} 次`}
              >
                <span className="round-launch-sub-budget-head">
                  <code className="round-launch-sub-budget-code">round_director · 独立主持子预算</code>
                  <strong className="round-launch-sub-budget-value">
                    {normalizedPlan.calls.recommended_director_calls} 次硬子预算
                  </strong>
                </span>
                <small className="round-launch-muted-block">
                  确认后随冻结计划写入；它包含在全局硬上限内，只会进一步收紧主持调用，不会扩大授权。
                </small>
              </div>
            </section>
            <section className="round-launch-card" aria-label="设置 Provider 调用次数绝对上限">
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
              <p className="round-launch-muted">覆盖本轮全部调用类型，包括运行中用户插话；允许 {PROVIDER_CALL_LIMIT_MIN}–{PROVIDER_CALL_LIMIT_MAX}。主持子预算不会叠加在此上限之外。</p>
            </section>
          </div>

          {authorization.warning ? (
            <div className="round-launch-alert warning" role="alert">
              <AlertTriangle size={17} aria-hidden="true" />
              <span>{authorization.warning.message}不能保证完整走完既定流程。</span>
            </div>
          ) : null}

          <div className="round-launch-grid">
            <section className="round-launch-card" aria-label="主持人路由">
              <strong className="round-launch-section-title">主持人</strong>
              <p className="round-launch-objective">{normalizedPlan.moderator.name || "不可用"}</p>
              <p className="round-launch-muted">{normalizedPlan.moderator.identity || "未说明身份"}</p>
              <p className="round-launch-muted">
                {providerLabel(normalizedPlan.moderator.provider)} / {normalizedPlan.moderator.model || "默认模型"}
              </p>
            </section>
            <section className="round-launch-card" aria-label="计划校验信息">
              <strong className="round-launch-section-title">冻结计划</strong>
              <p className="round-launch-muted">模式：{normalizedPlan.room.discussion_mode === "dynamic" ? "动态主持" : "顺序讨论"}</p>
              <p className="round-launch-muted">成员：{normalizedPlan.members.length} 位</p>
              <p className="round-launch-muted">
                主持结构上界 {normalizedPlan.calls.maximum_director_calls} 次 ·
                正式路径保守上界 {normalizedPlan.calls.formal_path_conservative_upper_bound} 次
              </p>
              <p className="round-launch-muted">上界不是预测，且不包含运行中用户插话。</p>
              <p className="round-launch-muted">标识：{normalizedPlan.plan_hash || "不可用"}</p>
            </section>
          </div>

          <section aria-label="各 Provider 建议授权分摊">
            <h3 className="round-launch-section-title">
              各 Provider 建议授权分摊 · 合计 {normalizedPlan.calls.projected_provider_calls_total} 次
            </h3>
            <ul className="round-launch-list">
              {normalizedPlan.provider_call_projection.map((provider) => (
                <li key={provider.provider} className="round-launch-list-item">
                  <span className="round-launch-route-text">
                    <strong>{providerLabel(provider.provider)}</strong>
                    <small className="round-launch-muted-block">
                      预检 {provider.projected_preflight_calls} · 成员 {provider.minimum_speaker_calls}
                      {" · "}主持预留 {provider.recommended_director_calls} · 纪要 {provider.optional_artifact_calls}
                      {provider.contingency_calls ? ` · 预留 ${provider.contingency_calls}` : ""}
                      {provider.skipped ? " · 已跳过" : provider.policy_disabled ? " · 本机策略停用" : ""}
                    </small>
                  </span>
                  <em className="round-launch-status">{provider.projected_provider_calls} 次</em>
                </li>
              ))}
            </ul>
          </section>

          <details
            open={memberRoutesOpen}
            onToggle={(event) => setMemberRoutesOpen(event.currentTarget.open)}
          >
            <summary>
              <span className="round-launch-summary-copy">
                <strong className="round-launch-section-title">成员路由（{normalizedPlan.members.length}）</strong>
                <small>默认收起；需要逐人核对 Provider 与模型时再展开。</small>
              </span>
            </summary>
            <ul className="round-launch-list">
              {normalizedPlan.members.map((member) => {
                const route = routeByKey.get(`${member.provider}\u0000${member.model}`);
                return (
                  <li key={member.id} className="round-launch-list-item">
                    <span className="round-launch-route-text">
                      <strong>{member.name}</strong>
                      <small className="round-launch-muted-block">
                        {member.identity || member.stage} · {providerLabel(member.provider)} / {member.model || "默认模型"}
                      </small>
                    </span>
                    <em className={`round-launch-status ${route?.callable ? "ready" : "unavailable"}`}>
                      {route?.callable ? "可调用" : "不可调用"}
                    </em>
                  </li>
                );
              })}
            </ul>
          </details>

          <section className="round-launch-safety" aria-label="Futu 与执行边界">
            {normalizedPlan.safety.verified
              ? <CheckCircle2 size={19} aria-hidden="true" />
              : <AlertTriangle size={19} aria-hidden="true" />}
            <span>
              <h3 className="round-launch-section-title">Futu 与执行边界</h3>
              <p className="round-launch-muted">Futu 行情仅作为只读研究资料；本次确认不授予下单能力。</p>
              <p className="round-launch-muted">
                执行能力：{normalizedPlan.safety.execution_capability === "none" ? "无" : "边界未验证"}；
                实盘交易：{normalizedPlan.safety.live_trading_allowed === false ? "禁止" : "边界未验证"}。
              </p>
            </span>
          </section>

          {normalizedPlan.blockers.length ? (
            <div className="round-launch-alert error" role="alert">
              <AlertTriangle size={17} aria-hidden="true" />
              <span>
                <strong>当前不能确认</strong>
                <ul>
                  {blockerEntries.map(({ blocker, key }) => (
                    <li key={key}>
                      {blockerLabel(blocker)}
                    </li>
                  ))}
                </ul>
              </span>
            </div>
          ) : null}

          {!authorization.validLimit ? <div className="round-launch-alert error" role="alert">{authorization.error}</div> : null}
          {!requestIdReady ? <div className="round-launch-alert error" role="alert">启动请求标识尚未准备好。</div> : null}
          {externalError || localError ? <div className="round-launch-alert error" role="alert">{localError || externalError}</div> : null}
            </>
          )}
        </div>

        <footer>
          <div className={`round-launch-footer-state ${launchSummaryTone}`} role="status" aria-live="polite">
            {launchSummaryTone === "loading"
              ? <LoaderCircle className="spin" size={18} aria-hidden="true" />
              : launchSummaryTone === "review"
                ? <CheckCircle2 size={18} aria-hidden="true" />
                : <AlertTriangle size={18} aria-hidden="true" />}
            <span>
              <strong>{launchSummaryLabel}</strong>
              <small>{launchSummaryDetail}</small>
            </span>
          </div>
          <div className="round-launch-footer-actions">
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
          </div>
        </footer>
      </form>
    </div>
  );
}
