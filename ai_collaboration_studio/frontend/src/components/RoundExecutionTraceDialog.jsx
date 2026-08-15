import {
  Activity,
  AlertTriangle,
  Bot,
  CheckCircle2,
  CircleDot,
  Cpu,
  FilePenLine,
  GitBranch,
  Link2,
  LoaderCircle,
  MessageSquare,
  RefreshCw,
  ShieldCheck,
  UserCheck,
  X,
} from "lucide-react";
import { useEffect, useId, useRef } from "react";
import { DiscussionAuditSection } from "./DiscussionAuditSection";
import "../styles/round-execution-trace.css";
import {
  candidateProjectionViewModel,
  roundExecutionDirectorBudget,
  roundExecutionEventMeta,
  roundExecutionStatusMeta,
  roundExecutionTraceAnchorState,
} from "../roundExecutionTrace";

const PAYLOAD_LABELS = Object.freeze({
  action: "动作",
  title: "标题",
  summary: "摘要",
  detail: "说明",
  description: "说明",
  reason: "原因",
  error_code: "错误码",
  candidate_id: "候选标识",
  candidate_revision: "候选版本",
  current_candidate_revision: "当前候选版本",
  disposition: "复核处置",
  message_id: "消息标识",
  artifact_id: "产物标识",
  objective: "轮次目标",
  scope: "调用范围",
  kind: "记录类型",
  max_calls: "调用上限",
  reserved_calls: "已预留调用",
  completed_calls: "已完成调用",
  elapsed_ms: "耗时（毫秒）",
  member_id: "成员标识",
  member_name: "成员",
  source: "调度来源",
  stage: "讨论阶段",
  checkpoint_integrity_ok: "检查点完整",
  turn_contract_version: "发言契约版本",
  turn_contract_qualified: "发言契约合格",
  preferred_option_id: "AI / 讨论首选方案",
  ai_preferred_option_id: "AI 首选方案",
  selected_option_id: "用户选择方案",
  selected_is_ai_preferred: "用户选择与 AI 首选一致",
  decision_version: "用户决定版本",
  referenced_candidate_ids: "引用候选",
  risk_ids: "风险标识",
  risk_id: "风险标识",
  impact: "风险影响",
  blocking: "是否阻塞",
  version: "版本",
  generation_source: "生成来源",
  artifact_version: "产物版本",
  is_current: "是否当前版本",
  relation_type: "谱系关系",
  resource_type: "资源类型",
  resource_id: "资源标识",
  resource_revision: "资源版本",
});

const REF_LABELS = Object.freeze({
  director_attempt_id: "主持尝试",
  director_decision_id: "主持决定",
  round_turn_id: "正式发言",
  message_id: "消息",
  candidate_id: "候选",
  candidate_revision: "候选版本",
  artifact_id: "产物",
  user_decision_id: "用户决定",
  provider_attempt_id: "Provider 调用",
  provider_operation_id: "Provider 操作",
  operation_target_id: "操作目标",
});

const GROUP_ICONS = Object.freeze({
  round: Activity,
  provider: Cpu,
  director: GitBranch,
  turn: MessageSquare,
  candidate: FilePenLine,
  review: ShieldCheck,
  artifact: CheckCircle2,
  user: UserCheck,
  other: CircleDot,
});

function timestampLabel(value) {
  const timestamp = Number(value);
  if (!Number.isFinite(timestamp) || timestamp <= 0) return "时间未记录";
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return "时间未记录";
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function compactValue(value) {
  if (Array.isArray(value)) return value.map((item) => String(item)).join("、");
  if (typeof value === "boolean") return value ? "是" : "否";
  if (["string", "number"].includes(typeof value)) return String(value);
  return "";
}

function eventDescription(event) {
  const payload = event.payload || {};
  for (const key of [
    "summary",
    "reason",
    "title",
    "detail",
    "description",
    "member_name",
    "error_code",
    "action",
    "kind",
    "relation_type",
  ]) {
    const value = compactValue(payload[key]).trim();
    if (value) return value.slice(0, 360);
  }
  if (event.actor?.name) return `${event.actor.name} · ${roundExecutionStatusMeta(event.status).label}`;
  return "该步骤已写入只读执行轨迹。";
}

function eventDetailRows(event) {
  const rows = [];
  const actor = event.actor || {};
  const source = event.source || {};
  if (actor.name) rows.push(["参与者", actor.name]);
  if (actor.version) rows.push(["身份版本", `v${actor.version}`]);
  if (actor.provider || actor.model) {
    rows.push(["模型路由", [actor.provider, actor.model].filter(Boolean).join(" / ")]);
  }
  if (source.table || source.id) {
    rows.push(["来源记录", [source.table, source.id].filter(Boolean).join(" · ")]);
  }
  if (source.sequence_no) rows.push(["来源序号", String(source.sequence_no)]);
  for (const [key, value] of Object.entries(event.refs || {})) {
    const display = compactValue(value).trim();
    if (display) rows.push([REF_LABELS[key] || key.replaceAll("_", " "), display]);
  }
  for (const [key, label] of Object.entries(PAYLOAD_LABELS)) {
    const display = compactValue(event.payload?.[key]).trim();
    if (display) rows.push([label, display]);
  }
  if (event.integrity?.status && event.integrity.status !== "unknown") {
    rows.push(["步骤完整性", event.integrity.status]);
  }
  if (event.integrity?.issues?.length) {
    rows.push(["完整性问题", event.integrity.issues.join("；")]);
  }
  return rows;
}

function TraceEvent({ event }) {
  const typeMeta = roundExecutionEventMeta(event.type);
  const statusMeta = roundExecutionStatusMeta(event.status);
  const Icon = GROUP_ICONS[typeMeta.group] || GROUP_ICONS.other;
  const details = eventDetailRows(event);
  return (
    <li className={`round-trace-event ${typeMeta.group}`}>
      <span className="round-trace-event-marker"><Icon size={14} aria-hidden="true" /></span>
      <article>
        <header>
          <span>
            <strong>{typeMeta.label}</strong>
            {typeMeta.group === "other" ? <code>{event.type}</code> : null}
          </span>
          <em className={`round-trace-status ${statusMeta.tone}`}>{statusMeta.label}</em>
        </header>
        <p>{eventDescription(event)}</p>
        <div className="round-trace-event-meta">
          <span>步骤 {event.ordinal}</span>
          <time>{timestampLabel(event.occurred_at)}</time>
          {event.actor?.name ? <span>{event.actor.name}</span> : null}
        </div>
        {details.length ? (
          <details>
            <summary>查看记录详情</summary>
            <dl>
              {details.map(([label, value], index) => (
                <div key={`${label}:${index}`}><dt>{label}</dt><dd>{value}</dd></div>
              ))}
            </dl>
          </details>
        ) : null}
      </article>
    </li>
  );
}

function TraceSummary({ trace }) {
  const summary = trace.summary;
  const calls = summary.provider_calls;
  const callValue = calls.max > 0 ? `${calls.completed} / ${calls.max}` : "0";
  const directorBudget = roundExecutionDirectorBudget(trace);
  const directorValue = directorBudget.valid
    ? `${directorBudget.reserved} / ${directorBudget.limit}`
    : directorBudget.recorded ? "异常" : "未载入";
  return (
    <div className="round-trace-metrics" aria-label="轨迹汇总">
      <span><small>轨迹步骤</small><strong>{summary.event_count}</strong></span>
      <span><small>Provider 调用</small><strong>{callValue}</strong></span>
      <span className={directorBudget.recorded && !directorBudget.valid ? "warning" : ""}>
        <small title="round_director">主持子预算（已用 / 上限）</small><strong>{directorValue}</strong>
      </span>
      <span className={summary.anomaly_count ? "warning" : ""}><small>异常</small><strong>{summary.anomaly_count}</strong></span>
      <span><small>正式发言</small><strong>{summary.formal_turn_count}</strong></span>
      <span><small>候选修订</small><strong>{summary.candidate_update_count}</strong></span>
      <span><small>复核意见</small><strong>{summary.risk_review_count}</strong></span>
    </div>
  );
}

function shortHash(value) {
  return value ? `${value.slice(0, 8)}…${value.slice(-8)}` : "未记录";
}

function CandidateProjectionSection({ projection }) {
  const view = candidateProjectionViewModel(projection);
  const decisionHeading = view.decision.ready
    ? `当前条件化首选：${view.decision.preferredTitle}`
    : view.decision.status === "deferred"
      ? "决策角色暂缓形成首选"
      : "尚未形成可比较首选";

  return (
    <section
      className={`round-trace-candidate-projection ${view.tone}`}
      aria-label="候选形成只读投影"
    >
      <div className="round-trace-candidate-heading">
        <span>
          <span className="round-trace-candidate-icon"><FilePenLine size={15} aria-hidden="true" /></span>
          <span>
            <strong>候选形成</strong>
            <small>只读取合格发言契约，不从聊天文本猜测方案</small>
          </span>
        </span>
        <em>{view.statusLabel}</em>
      </div>

      {!view.available ? (
        <div className="round-trace-candidate-empty">
          尚无合格结构化候选；成员提出或修订候选后，这里才会显示版本、风控与选择依据。
        </div>
      ) : (
        <>
          <div className="round-trace-candidate-facts" aria-label="候选投影汇总">
            <span><small>合格消息</small><strong>{view.qualifiedMessageCount}</strong></span>
            <span><small>当前候选</small><strong>{view.candidateCount}</strong></span>
            <span><small>累计版本</small><strong>{view.totalRevisionCount}</strong></span>
            <span>
              <small>{view.riskReview.applicable ? "精确版本复核" : "风控协议"}</small>
              <strong>{view.riskReview.applicable
                ? `${view.riskReview.reviewedCandidateCount} / ${view.riskReview.targetCandidateCount}`
                : "本轮未要求"}</strong>
            </span>
          </div>

          {view.candidates.length ? (
            <div className="round-trace-candidate-list">
              {view.candidates.map((candidate) => {
                const meta = [
                  candidate.symbol,
                  candidate.direction && candidate.direction !== "UNSPECIFIED" ? candidate.direction : "",
                  candidate.timeline,
                  candidate.evidenceCount ? `证据 ${candidate.evidenceCount}` : "",
                ].filter(Boolean);
                return (
                  <article className={candidate.preferred ? "preferred" : ""} key={candidate.id}>
                    <div className="round-trace-candidate-title">
                      <span>
                        <strong>{candidate.title}</strong>
                        <small>{candidate.id}</small>
                      </span>
                      <span className="round-trace-candidate-badges">
                        <em>r{candidate.revision}</em>
                        {candidate.preferred ? <em className="preferred">当前首选</em> : null}
                      </span>
                    </div>
                    {meta.length ? <p className="round-trace-candidate-meta">{meta.join(" · ")}</p> : null}
                    {candidate.description ? <p className="round-trace-candidate-thesis" title={candidate.description}>{candidate.description}</p> : null}
                    {candidate.invalidation ? (
                      <p className="round-trace-candidate-invalidation" title={candidate.invalidation}>
                        <strong>失效条件</strong>{candidate.invalidation}
                      </p>
                    ) : null}
                    {view.riskReview.applicable ? (
                      <div className="round-trace-candidate-reviews">
                        <span>当前复核 {candidate.currentReviewCount}</span>
                        <span className="support">支持 {candidate.actionCounts.support}</span>
                        <span className="challenge">质疑 {candidate.actionCounts.challenge}</span>
                        <span className="reject">拒绝 {candidate.actionCounts.reject}</span>
                        {candidate.staleReviewCount ? <span className="stale">过期 {candidate.staleReviewCount}</span> : null}
                      </div>
                    ) : null}
                  </article>
                );
              })}
            </div>
          ) : (
            <div className="round-trace-candidate-empty">投影已建立，但当前没有可展示的合格候选。</div>
          )}

          <div className={`round-trace-candidate-decision ${view.decision.ready ? "ready" : "pending"}`}>
            <span>
              {view.decision.ready ? <CheckCircle2 size={15} aria-hidden="true" /> : <CircleDot size={15} aria-hidden="true" />}
              <strong>{decisionHeading}</strong>
            </span>
            <p>{view.decision.rationale || (view.candidateCount < 2
              ? "至少需要两个真实讨论过的候选，才能记录条件化首选与理由。"
              : "仍需补齐谱系、风险复核或选择理由。")}</p>
          </div>

          {view.issues.length ? (
            <div className="round-trace-candidate-issues" role={view.safetyVerified ? "status" : "alert"}>
              <strong>当前阻断</strong>
              <ul>{view.issues.slice(0, 3).map((issue, index) => (
                <li key={`${issue.code}:${issue.candidate_id}:${index}`}>{issue.message}</li>
              ))}</ul>
            </div>
          ) : null}

          {view.projectionSha256 ? (
            <p className="round-trace-candidate-hash">投影指纹 <code title={view.projectionSha256}>{shortHash(view.projectionSha256)}</code></p>
          ) : null}
        </>
      )}

      <p className="round-trace-candidate-boundary">{view.boundary} 风控意见也不等于批准、否决或执行授权。</p>
    </section>
  );
}

function ledgerStatusLabel(value) {
  if (value === true) return { label: "已核验", tone: "verified" };
  if (value === false) return { label: "未通过", tone: "invalid" };
  return { label: "未记录", tone: "partial" };
}

export function RoundExecutionTraceDialog({
  open,
  trace,
  loading = false,
  loadingMore = false,
  error = "",
  stale = false,
  discussionAuditState = null,
  onClose,
  onRetry,
  onRetryDiscussionAudit,
  onLoadMore,
}) {
  const titleId = useId();
  const descriptionId = useId();
  const dialogRef = useRef(null);
  const closeButtonRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const previousFocus = document.activeElement;
    const frame = requestAnimationFrame(() => closeButtonRef.current?.focus());
    return () => {
      cancelAnimationFrame(frame);
      if (previousFocus instanceof HTMLElement) previousFocus.focus();
    };
  }, [open]);

  if (!open) return null;
  const traceReady = Boolean(trace?.valid);
  const integrityTone = trace?.integrity?.status === "verified" && trace?.integrity?.ok
    ? "verified"
    : trace?.integrity?.status === "invalid" || trace?.valid === false
      ? "invalid"
      : "partial";
  const directorBudget = traceReady ? roundExecutionDirectorBudget(trace) : null;
  const anchorState = traceReady ? roundExecutionTraceAnchorState(trace) : null;
  const handleKeyDown = (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      onClose?.();
      return;
    }
    if (event.key !== "Tab" || !dialogRef.current) return;
    const focusable = [...dialogRef.current.querySelectorAll(
      "button:not([disabled]), summary, [href], input:not([disabled]), [tabindex]:not([tabindex='-1'])",
    )].filter((element) => element instanceof HTMLElement && element.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div
      className="dialog-backdrop round-trace-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose?.();
      }}
    >
      <section
        ref={dialogRef}
        className="dialog round-trace-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        aria-busy={loading || loadingMore || discussionAuditState?.loading === true}
        onKeyDown={handleKeyDown}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <span>
            <Activity size={18} aria-hidden="true" />
            <span><strong id={titleId}>本轮执行轨迹</strong><small id={descriptionId}>调用、调度、发言与决策链的只读记录</small></span>
          </span>
          <button ref={closeButtonRef} type="button" className="icon-button" aria-label="关闭执行轨迹" onClick={onClose}><X size={18} /></button>
        </header>

        <div className="round-trace-body">
          {loading && !trace ? (
            <div className="round-trace-loading" role="status">
              <LoaderCircle className="spin" size={21} aria-hidden="true" />
              <strong>正在读取已持久化轨迹</strong>
              <small>这是本地只读查询，不会调用任何模型。</small>
            </div>
          ) : error && !trace ? (
            <div className="round-trace-error" role="alert">
              <AlertTriangle size={20} aria-hidden="true" />
              <strong>执行轨迹读取失败</strong>
              <p>{error}</p>
              <button type="button" className="secondary" onClick={onRetry}><RefreshCw size={14} />重试</button>
            </div>
          ) : trace && !trace.valid ? (
            <div className="round-trace-error" role="alert">
              <AlertTriangle size={20} aria-hidden="true" />
              <strong>执行轨迹校验未通过</strong>
              <p>{trace.errors.join("；")}</p>
              <button type="button" className="secondary" onClick={onRetry}><RefreshCw size={14} />重新读取</button>
            </div>
          ) : traceReady ? (
            <>
              <section className="round-trace-overview">
                <div className="round-trace-round-copy">
                  <span><Bot size={14} aria-hidden="true" />轮次 {trace.round_id.slice(-10)}</span>
                  <strong>{trace.round?.objective || "本轮目标未记录"}</strong>
                  <small>状态 {trace.round?.status || "未记录"} · 当前已载入 {trace.events.length} / {trace.page.total} 步</small>
                </div>
                <span className={`round-trace-integrity ${integrityTone}`}>
                  <ShieldCheck size={13} aria-hidden="true" />
                  {integrityTone === "verified" ? "完整性已核验" : integrityTone === "invalid" ? "完整性异常" : "部分历史记录"}
                </span>
              </section>
              <TraceSummary trace={trace} />
              <DiscussionAuditSection
                state={discussionAuditState}
                expectedTraceHash={trace.trace_hash}
                onRetry={onRetryDiscussionAudit}
              />
              <CandidateProjectionSection projection={trace.candidate_projection} />
              <div className="round-trace-ledgers" aria-label="账本完整性">
                {[
                  ["轮次账本", trace.integrity.round_ledger_verified],
                  ["Provider 账本", trace.integrity.provider_ledger_verified],
                ].map(([label, value]) => {
                  const state = ledgerStatusLabel(value);
                  return (
                    <span key={label}>
                      <small>{label}</small>
                      <strong className={state.tone}>{state.label}</strong>
                    </span>
                  );
                })}
                <span>
                  <small><code>round_director</code> 子预算</small>
                  <strong className={directorBudget?.valid ? "verified" : directorBudget?.recorded ? "invalid" : "partial"}>
                    {directorBudget?.valid
                      ? `已用 ${directorBudget.reserved} · 剩余 ${directorBudget.remaining} · 上限 ${directorBudget.limit}`
                      : directorBudget?.recorded ? "记录异常" : "当前页未载入"}
                  </strong>
                </span>
              </div>
              <section className={`round-trace-anchor ${anchorState.state}`} aria-label="轨迹快照锚点">
                <span className="round-trace-anchor-icon"><Link2 size={15} aria-hidden="true" /></span>
                <div className="round-trace-anchor-copy">
                  <span className="round-trace-anchor-head">
                    <small>轨迹快照锚点</small>
                    <strong>{anchorState.label}</strong>
                  </span>
                  <p>{anchorState.detail}</p>
                  <dl>
                    <div><dt>锚点序列</dt><dd>{anchorState.sequence ? `#${anchorState.sequence}` : "未建立"}</dd></div>
                    <div>
                      <dt>当前快照</dt>
                      <dd><code title={anchorState.snapshot_sha256}>{shortHash(anchorState.snapshot_sha256)}</code></dd>
                    </div>
                    <div>
                      <dt>锚点链头</dt>
                      <dd><code title={anchorState.anchor_sha256}>{shortHash(anchorState.anchor_sha256)}</code></dd>
                    </div>
                  </dl>
                </div>
              </section>

              {stale ? (
                <div className="round-trace-stale" role="status">
                  <span>本轮产生了新记录，当前列表可能不是最新状态。</span>
                  <button type="button" onClick={onRetry} disabled={loading}><RefreshCw size={13} />刷新</button>
                </div>
              ) : null}
              {error ? <div className="round-trace-inline-error" role="alert">刷新失败：{error}</div> : null}
              {trace.history?.limitations?.length ? (
                <details className="round-trace-limitations">
                  <summary>历史覆盖说明</summary>
                  <ul>{trace.history.limitations.map((item, index) => <li key={`${item}:${index}`}>{item}</li>)}</ul>
                </details>
              ) : null}
              {trace.integrity?.issues?.length ? (
                <div className="round-trace-integrity-issues">
                  <strong>完整性提示</strong>
                  <ul>{trace.integrity.issues.map((item, index) => <li key={`${item}:${index}`}>{item}</li>)}</ul>
                </div>
              ) : null}

              {trace.events.length ? (
                <ol className="round-trace-events">
                  {trace.events.map((event) => <TraceEvent event={event} key={event.event_id} />)}
                </ol>
              ) : <div className="round-trace-empty">本轮尚无可展示的执行步骤。</div>}

              {trace.page.has_more ? (
                <button type="button" className="secondary round-trace-more" onClick={onLoadMore} disabled={loadingMore}>
                  {loadingMore ? <LoaderCircle className="spin" size={14} /> : null}
                  {loadingMore ? "正在加载…" : "加载更多记录"}
                </button>
              ) : null}
            </>
          ) : null}
        </div>

        <footer>
          <span><ShieldCheck size={13} aria-hidden="true" />只读审计 · 0 次 Provider 调用 · 无执行能力</span>
          <button type="button" className="secondary" onClick={onClose}>关闭</button>
        </footer>
      </section>
    </div>
  );
}
