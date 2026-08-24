import { useId, useMemo, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  CircleDot,
  FilePenLine,
  GitBranch,
  LoaderCircle,
  MessageSquare,
  RefreshCw,
  ShieldQuestion,
} from "lucide-react";
import { discussionAuditViewModel } from "../discussionAudit";

const AUDIT_ERROR_DETAIL_LIMIT = 1000;
const AUDIT_SELECTION_PAGE_SIZE = 12;
const AUDIT_EDGE_PAGE_SIZE = 16;
const AUDIT_CANDIDATE_PAGE_SIZE = 8;
const AUDIT_FINDING_PAGE_SIZE = 12;

function boundedAuditDetail(value) {
  const detail = String(value || "").trim();
  if (detail.length <= AUDIT_ERROR_DETAIL_LIMIT) return detail;
  return detail.slice(0, AUDIT_ERROR_DETAIL_LIMIT) + "...";
}
function compactId(value) {
  const normalized = String(value || "");
  if (!normalized) return "未记录";
  return normalized.length > 24
    ? `${normalized.slice(0, 11)}…${normalized.slice(-9)}`
    : normalized;
}

function focusAuditProgressAfterRender(event) {
  const progress = event.currentTarget
    .closest(".discussion-audit-window")
    ?.querySelector("progress");
  globalThis.setTimeout(() => {
    progress?.focus({ preventScroll: true });
  }, 0);
}

function AuditStateMessage({ tone, icon: Icon, title, detail, actionLabel, onAction }) {
  return (
    <div className={`discussion-audit-message ${tone}`} role={tone === "danger" ? "alert" : "status"}>
      <Icon className={tone === "loading" ? "spin" : ""} size={18} aria-hidden="true" />
      <span><strong>{title}</strong><small>{detail}</small></span>
      {actionLabel ? (
        <button type="button" className="secondary" disabled={typeof onAction !== "function"} onClick={() => onAction?.()}>
          <RefreshCw size={13} aria-hidden="true" />{actionLabel}
        </button>
      ) : null}
    </div>
  );
}

function CandidateCheckpoint({ checkpoint }) {
  const candidateListId = useId();
  const [candidateLimit, setCandidateLimit] = useState(AUDIT_CANDIDATE_PAGE_SIZE);
  const visibleCandidates = checkpoint.candidates.slice(0, candidateLimit);
  return (
    <section className={`discussion-audit-checkpoint ${checkpoint.tone}`} aria-label="候选比较检查点">
      <div className="discussion-audit-subhead">
        <span><FilePenLine size={15} aria-hidden="true" /><strong>候选比较检查点</strong></span>
        <em>{checkpoint.label}</em>
      </div>
      <div className="discussion-audit-checkpoint-grid">
        <span>
          <small>可比较候选 / 最低要求</small>
          <strong>{checkpoint.applicable ? checkpoint.countLabel : "不适用"}</strong>
        </span>
        <span>
          <small>候选谱系</small>
          <strong>{checkpoint.lineage.ready ? "已就绪" : checkpoint.lineage.status || "未记录"}</strong>
        </span>
        <span>
          <small>风险复核</small>
          <strong>{checkpoint.risk_review.required
            ? `${checkpoint.risk_review.reviewed_candidate_count} / ${checkpoint.risk_review.target_candidate_count}`
            : "本轮未要求"}</strong>
        </span>
        <span>
          <small>条件化决定</small>
          <strong>{checkpoint.decisionLabel}</strong>
        </span>
      </div>
      {checkpoint.candidates.length ? (
        <div className="discussion-audit-window">
          <p className="discussion-audit-window-status" role="status">
            <span>展示 {visibleCandidates.length} / {checkpoint.candidates.length} 个候选</span>
            <progress aria-label="候选检查点挂载进度" max={checkpoint.candidates.length || 1} tabIndex={-1} value={visibleCandidates.length} />
          </p>
          <div className="discussion-audit-candidates" id={candidateListId} role="list" aria-label="候选比较检查点记录">
            {visibleCandidates.map((candidate) => (
              <article key={candidate.projectionKey} role="listitem">
                <span><strong title={candidate.id}>{compactId(candidate.id)}</strong><em>r{candidate.revision}</em></span>
                <small>
                  来源 <code title={candidate.origin_message_id}>{compactId(candidate.origin_message_id)}</code>
                  {candidate.latest_message_id && candidate.latest_message_id !== candidate.origin_message_id
                    ? <> · 最新 <code title={candidate.latest_message_id}>{compactId(candidate.latest_message_id)}</code></>
                    : null}
                </small>
              </article>
            ))}
          </div>
          {visibleCandidates.length < checkpoint.candidates.length ? (
            <button
              aria-controls={candidateListId}
              className="secondary discussion-audit-more"
              type="button"
              onClickCapture={focusAuditProgressAfterRender}
              onClick={() => setCandidateLimit((current) => Math.min(
                current + AUDIT_CANDIDATE_PAGE_SIZE,
                checkpoint.candidates.length,
              ))}
            >
              再显示 {Math.min(AUDIT_CANDIDATE_PAGE_SIZE, checkpoint.candidates.length - visibleCandidates.length)} 个候选
            </button>
          ) : null}
        </div>
      ) : (
        <p className="discussion-audit-empty-copy">
          {checkpoint.applicable ? "尚无可比较的结构化候选。" : "本轮契约未启用候选比较检查点。"}
        </p>
      )}
      {[...checkpoint.lineage.blocker_codes, ...checkpoint.risk_review.blocker_codes].length ? (
        <p className="discussion-audit-blockers">
          阻断码 {[...new Set([
            ...checkpoint.lineage.blocker_codes,
            ...checkpoint.risk_review.blocker_codes,
          ])].join("、")}
        </p>
      ) : null}
    </section>
  );
}

function DiscussionAuditContent({ audit, expectedTraceHash, loading, error, stale, onRetry }) {
  const view = useMemo(() => discussionAuditViewModel(audit, { expectedTraceHash }), [audit, expectedTraceHash]);
  const selectionListId = useId();
  const edgeListId = useId();
  const findingListId = useId();
  const [selectionLimit, setSelectionLimit] = useState(AUDIT_SELECTION_PAGE_SIZE);
  const [edgeLimit, setEdgeLimit] = useState(AUDIT_EDGE_PAGE_SIZE);
  const [findingLimit, setFindingLimit] = useState(AUDIT_FINDING_PAGE_SIZE);
  const [selectionOpen, setSelectionOpen] = useState(view.selections.length <= 4);
  const [edgeOpen, setEdgeOpen] = useState(
    view.responseEdges.length > 0 && view.responseEdges.length <= 4,
  );
  const visibleSelections = view.selections.slice(0, selectionLimit);
  const visibleEdges = view.responseEdges.slice(0, edgeLimit);
  const visibleFindings = view.findings.slice(0, findingLimit);
  if (!view.valid) {
    return (
      <AuditStateMessage
        tone="danger"
        icon={AlertTriangle}
        title="讨论审计校验未通过"
        detail={view.errors.join("；")}
        actionLabel="重新读取"
        onAction={onRetry}
      />
    );
  }

  return (
    <>
      <div className="discussion-audit-metrics" aria-label="讨论审计汇总">
        <span><small>主持选择</small><strong>{view.dynamic.selectionCount}</strong></span>
        <span><small>动态选择</small><strong>{view.dynamic.dynamicSelectionCount}</strong></span>
        <span className={view.hasFallback ? "warning" : ""}><small>安全回退</small><strong>{view.dynamic.fallbackCount}</strong></span>
        <span><small>结构回应边</small><strong>{view.responseEdges.length}</strong></span>
      </div>

      <div className="discussion-audit-core-grid">
        <section className={`discussion-audit-dynamic ${view.dynamic.tone}`} aria-label="动态调度结构">
          <div className="discussion-audit-subhead">
            <span><GitBranch size={15} aria-hidden="true" /><strong>动态调度结构</strong></span>
            <em>{view.dynamic.label}</em>
          </div>
          <p>{view.dynamic.detail}</p>
          {view.hasFallback ? (
            <div className="discussion-audit-fallback" role="status">
              <AlertTriangle size={14} aria-hidden="true" />
              <span><strong>发现安全回退</strong>这不等于模型主持；请结合每次选择的来源判断。</span>
            </div>
          ) : null}
        </section>

        <section className="discussion-audit-semantic unknown" aria-label="语义因果边界">
          <div className="discussion-audit-subhead">
            <span><ShieldQuestion size={15} aria-hidden="true" /><strong>语义因果边界</strong></span>
            <em>{view.semantic.label}</em>
          </div>
          <p>{view.semantic.detail}</p>
          <code>{view.semantic.reasonCode}</code>
        </section>
      </div>

      {view.selections.length ? (
        <details
          className="discussion-audit-details"
          open={selectionOpen}
          onToggle={(event) => setSelectionOpen(event.currentTarget.open)}
        >
          <summary>主持选择记录 <span>{view.selections.length}</span></summary>
          <div className="discussion-audit-window">
            <p className="discussion-audit-window-status" role="status">
              <span>展示 {visibleSelections.length} / {view.selections.length} 条主持选择</span>
              <progress aria-label="主持选择挂载进度" max={view.selections.length || 1} tabIndex={-1} value={visibleSelections.length} />
            </p>
            <ol className="discussion-audit-selections" id={selectionListId}>
              {visibleSelections.map((selection, index) => (
                <li key={selection.projectionKey}>
                  <span className="discussion-audit-selection-sequence">#{selection.sequence_no || index + 1}</span>
                  <div>
                    <span className="discussion-audit-selection-head">
                      <strong title={selection.selected_member_id}>{selection.memberLabel}</strong>
                      <span>
                        {selection.fallback ? <em className="fallback">安全回退</em> : null}
                        <em className={selection.statusMeta.tone}>{selection.statusMeta.label}</em>
                      </span>
                    </span>
                    <p>{selection.sourceLabel} · 动作 {selection.action} · 权限源 {selection.decision_authority}</p>
                    <small>
                      调度快照 {selection.scheduling_snapshot.recorded ? "已记录" : "未记录"}
                      {selection.action !== "finish"
                        ? ` · 入选资格${selection.scheduling_snapshot.selected_member_eligible ? "已确认" : "未确认"}`
                        : ""}
                      {selection.scheduling_snapshot.selected_gap_codes.length
                        ? ` · 覆盖缺口 ${selection.scheduling_snapshot.selected_gap_codes.join("、")}`
                        : ""}
                    </small>
                  </div>
                </li>
              ))}
            </ol>
            {visibleSelections.length < view.selections.length ? (
              <button
                aria-controls={selectionListId}
                className="secondary discussion-audit-more"
                type="button"
                onClickCapture={focusAuditProgressAfterRender}
                onClick={() => setSelectionLimit((current) => Math.min(
                  current + AUDIT_SELECTION_PAGE_SIZE,
                  view.selections.length,
                ))}
              >
                再显示 {Math.min(AUDIT_SELECTION_PAGE_SIZE, view.selections.length - visibleSelections.length)} 条主持选择
              </button>
            ) : null}
          </div>
        </details>
      ) : (
        <p className="discussion-audit-empty-copy">本轮没有可展示的主持选择记录。</p>
      )}

      <details
        className="discussion-audit-details"
        open={edgeOpen}
        onToggle={(event) => setEdgeOpen(event.currentTarget.open)}
      >
        <summary>回应关系边 <span>{view.responseEdges.length}</span></summary>
        {view.responseEdges.length ? (
          <div className="discussion-audit-window">
            <p className="discussion-audit-window-status" role="status">
              <span>展示 {visibleEdges.length} / {view.responseEdges.length} 条回应边</span>
              <progress aria-label="回应关系边挂载进度" max={view.responseEdges.length || 1} tabIndex={-1} value={visibleEdges.length} />
            </p>
            <ul className="discussion-audit-edges" id={edgeListId}>
              {visibleEdges.map((edge) => (
                <li key={edge.projectionKey}>
                  <span className="discussion-audit-edge-route">
                    <code title={edge.from_message_id}>{compactId(edge.from_message_id)}</code>
                    <ArrowRight size={13} aria-hidden="true" />
                    <code title={edge.to_message_id}>{compactId(edge.to_message_id)}</code>
                  </span>
                  <span className="discussion-audit-edge-badges">
                    <em className="verified"><CheckCircle2 size={11} aria-hidden="true" />结构已核验</em>
                    <em>{edge.relationLabel}</em>
                    <em>{edge.scopeLabel}</em>
                    <em className="unknown"><CircleDot size={11} aria-hidden="true" />语义未知</em>
                  </span>
                </li>
              ))}
            </ul>
            {visibleEdges.length < view.responseEdges.length ? (
              <button
                aria-controls={edgeListId}
                className="secondary discussion-audit-more"
                type="button"
                onClickCapture={focusAuditProgressAfterRender}
                onClick={() => setEdgeLimit((current) => Math.min(
                  current + AUDIT_EDGE_PAGE_SIZE,
                  view.responseEdges.length,
                ))}
              >
                再显示 {Math.min(AUDIT_EDGE_PAGE_SIZE, view.responseEdges.length - visibleEdges.length)} 条回应边
              </button>
            ) : null}
          </div>
        ) : <p className="discussion-audit-empty-copy">本轮没有合格发言契约形成的回应边。</p>}
      </details>

      <CandidateCheckpoint key={view.auditHash} checkpoint={view.checkpoint} />

      {view.findings.length ? (
        <div className="discussion-audit-window">
          <p className="discussion-audit-window-status" role="status">
            <span>展示 {visibleFindings.length} / {view.findings.length} 项审计发现</span>
            <progress aria-label="审计发现挂载进度" max={view.findings.length || 1} tabIndex={-1} value={visibleFindings.length} />
          </p>
          <div className="discussion-audit-findings" id={findingListId} role="list" aria-label="审计发现">
            {visibleFindings.map((finding) => (
              <span className={finding.tone} key={finding.projectionKey} role="listitem" title={finding.code}>
                {finding.label}{finding.count ? ` × ${finding.count}` : ""}
              </span>
            ))}
          </div>
          {visibleFindings.length < view.findings.length ? (
            <button
              aria-controls={findingListId}
              className="secondary discussion-audit-more"
              type="button"
              onClickCapture={focusAuditProgressAfterRender}
              onClick={() => setFindingLimit((current) => Math.min(
                current + AUDIT_FINDING_PAGE_SIZE,
                view.findings.length,
              ))}
            >
              再显示 {Math.min(AUDIT_FINDING_PAGE_SIZE, view.findings.length - visibleFindings.length)} 项审计发现
            </button>
          ) : null}
        </div>
      ) : null}

      {stale ? (
        <div className="discussion-audit-refresh-note" role="status">
          <span>本轮已有新记录，讨论审计可能已过期。</span>
          <button type="button" disabled={loading || typeof onRetry !== "function"} onClick={() => onRetry?.()}><RefreshCw size={13} />刷新审计</button>
        </div>
      ) : null}
      {error ? <div className="discussion-audit-inline-error" role="alert">讨论审计刷新失败：{error}</div> : null}
      <p className="discussion-audit-boundary">{view.boundary}</p>
    </>
  );
}

export function DiscussionAuditSection({ state, expectedTraceHash = "", onRetry }) {
  const audit = state?.audit || null;
  const loading = state?.loading === true;
  const error = boundedAuditDetail(state?.error);
  const stale = state?.stale === true;

  return (
    <section className="discussion-audit" aria-label="动态讨论审计" aria-busy={loading}>
      <header className="discussion-audit-heading">
        <span>
          <span className="discussion-audit-heading-icon"><MessageSquare size={15} aria-hidden="true" /></span>
          <span><strong>动态讨论审计</strong><small>结构证明、回应关系与候选比较门槛</small></span>
        </span>
        {loading && audit ? <LoaderCircle className="spin" size={15} aria-label="正在刷新讨论审计" /> : null}
      </header>

      <div className="discussion-audit-body">
        {loading && !audit ? (
          <AuditStateMessage
            tone="loading"
            icon={LoaderCircle}
            title="正在读取讨论审计"
            detail="与执行轨迹并行的本地只读查询；不会调用模型或行情服务。"
          />
        ) : error && !audit ? (
          <AuditStateMessage
            tone="danger"
            icon={AlertTriangle}
            title="讨论审计暂时不可用"
            detail={`${error}；执行轨迹仍可独立查看。`}
            actionLabel="重试审计"
            onAction={onRetry}
          />
        ) : audit ? (
          <DiscussionAuditContent
            key={JSON.stringify(["discussion-audit-content", audit?.audit_hash, audit?.round_id, expectedTraceHash])}
            audit={audit}
            expectedTraceHash={expectedTraceHash}
            loading={loading}
            error={error}
            stale={stale}
            onRetry={onRetry}
          />
        ) : null}
      </div>
    </section>
  );
}
