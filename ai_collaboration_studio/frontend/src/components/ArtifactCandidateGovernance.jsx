import {
  AlertTriangle,
  CheckCircle2,
  GitBranch,
  ShieldAlert,
} from "lucide-react";
import { memo, useEffect, useId, useMemo, useState } from "react";
import { artifactCandidateGovernance } from "../candidateGovernance";
import "../styles/candidate-governance-refinement.css";

const CANDIDATE_PAGE_SIZE = 40;
const REVIEW_PAGE_SIZE = 60;

function governanceListKey(...parts) {
  return JSON.stringify(parts.map((part) => String(part ?? "")));
}

function shortHash(value, visibleLength = 12) {
  const normalized = typeof value === "string" ? value.trim() : "";
  if (!normalized) return "";
  if (normalized.length <= visibleLength) return normalized;
  return `${normalized.slice(0, visibleLength)}…`;
}

const GovernanceStatus = memo(function GovernanceStatus({ ready, readyLabel, blockedLabel, neutral = false }) {
  const Icon = neutral ? GitBranch : ready ? CheckCircle2 : AlertTriangle;
  const tone = neutral ? "neutral" : ready ? "ready" : "blocked";
  return (
    <span className={`artifact-governance-status ${tone}`}>
      <Icon size={12} aria-hidden="true" />
      {neutral ? readyLabel : ready ? readyLabel : blockedLabel}
    </span>
  );
});

const GovernanceIssues = memo(function GovernanceIssues({ issues, label = "治理问题" }) {
  if (!issues.length) return null;
  const visibleIssues = issues.slice(0, 40);
  if (issues.length > 4) {
    return (
      <details className="artifact-governance-issue-dossier">
        <summary>{label}（{issues.length}）</summary>
        <ul className="artifact-governance-issues">
          {visibleIssues.map((issue) => <li key={governanceListKey("governance-issue", issue)}>{issue}</li>)}
        </ul>
        {visibleIssues.length < issues.length ? <small>其余 {issues.length - visibleIssues.length} 项未在界面展开。</small> : null}
      </details>
    );
  }
  return (
    <ul className="artifact-governance-issues">
      {issues.map((issue) => <li key={governanceListKey("governance-issue", issue)}>{issue}</li>)}
    </ul>
  );
});

const CandidateReference = memo(function CandidateReference({ candidate }) {
  return (
    <article className="artifact-lineage-item" role="listitem">
      <span>
        <strong>{candidate.title || candidate.id || "未命名候选"}</strong>
        {candidate.preferred ? <em>条件化首选</em> : null}
      </span>
      <small>
        候选 <code>{candidate.id || "未记录"}</code>
        {candidate.revision ? ` · 精确版本 r${candidate.revision}` : " · 修订号未记录"}
      </small>
      <small>
        形成消息 <code>{candidate.originMessageId || "未记录"}</code>
        {` · 当前版本消息 `}<code>{candidate.latestMessageId || "未记录"}</code>
      </small>
    </article>
  );
});

function reviewVersionText(review) {
  if (!review.candidateRevision) return "候选修订号未记录";
  if (review.status === "current") return `绑定当前精确版本 r${review.candidateRevision}`;
  if (review.status === "stale") {
    return `已过期：绑定 r${review.candidateRevision}，当前 r${review.currentCandidateRevision || "?"}`;
  }
  return `绑定 r${review.candidateRevision}，版本状态未记录`;
}

const RiskReviewReference = memo(function RiskReviewReference({ review }) {
  const candidateTitle = String(review.candidateSnapshot?.title || "").trim();
  return (
    <article className={`artifact-risk-review-item ${review.status}`} role="listitem">
      <div>
        <span className={`artifact-risk-disposition ${review.tone}`}>{review.dispositionLabel}</span>
        <em>{reviewVersionText(review)}</em>
      </div>
      <strong>{candidateTitle || review.candidateId || "未命名候选"}</strong>
      <small>
        候选 <code>{review.candidateId || "未记录"}</code>
        {` · 复核消息 `}<code>{review.reviewMessageId || "未记录"}</code>
      </small>
      <small>
        复核成员 {review.reviewerName || review.reviewerMemberId || "未记录"}
        {review.reviewerMemberVersion ? ` v${review.reviewerMemberVersion}` : ""}
        {review.candidateLatestMessageId
          ? <> · 对应候选消息 <code>{review.candidateLatestMessageId}</code></>
          : null}
      </small>
      {review.candidateSnapshotSha256 ? (
        <small title={review.candidateSnapshotSha256}>
          候选快照 SHA-256 <code>{shortHash(review.candidateSnapshotSha256, 16)}</code>
        </small>
      ) : null}
      {review.riskIds.length ? <small>关联风险：{review.riskIds.join("、")}</small> : null}
    </article>
  );
});

function focusGovernanceProgressAfterRender(event) {
  const progress = event.currentTarget
    .closest(".artifact-governance-layer")
    ?.querySelector("progress");

  globalThis.setTimeout(() => {
    progress?.focus({ preventScroll: true });
  }, 0);
}

export const ArtifactCandidateGovernance = memo(function ArtifactCandidateGovernance({ artifact }) {
  const titleId = useId();
  const boundaryId = useId();
  const lineageTitleId = useId();
  const riskReviewTitleId = useId();
  const candidateListId = useId();
  const riskReviewListId = useId();
  const governance = useMemo(() => artifactCandidateGovernance(artifact), [artifact]);
  const [candidateLimit, setCandidateLimit] = useState(CANDIDATE_PAGE_SIZE);
  const [reviewLimit, setReviewLimit] = useState(REVIEW_PAGE_SIZE);
  const governanceIdentity = useMemo(() => JSON.stringify([
      governance.version,
      governance.attestationSha256,
      governance.snapshotSha256,
      governance.lineage.candidates.length,
      governance.riskReview.reviews.length,
    ]), [governance]);
  useEffect(() => {
    setCandidateLimit(CANDIDATE_PAGE_SIZE);
    setReviewLimit(REVIEW_PAGE_SIZE);
  }, [governanceIdentity]);
  if (!governance.available) return null;
  const { lineage, riskReview } = governance;
  const visibleCandidates = lineage.candidates.slice(0, candidateLimit);
  const visibleReviews = riskReview.reviews.slice(0, reviewLimit);

  return (
    <section
      className="artifact-candidate-governance governance-ledger"
      aria-labelledby={titleId}
      aria-describedby={boundaryId}
      data-governance-state={governance.ready ? "ready" : governance.applicable ? "blocked" : "neutral"}
    >
      <div className="artifact-candidate-governance-heading">
        <span>
          <strong id={titleId}>候选治理记录</strong>
          <small>
            服务端只读快照；前两层保留候选来源和风控复核，不写入可编辑纪要正文。
            {governance.version ? ` · ${governance.version}` : ""}
            {governance.attestationVersion ? ` · ${governance.attestationVersion}` : ""}
            {governance.attestationSha256 ? ` · 封印 ${shortHash(governance.attestationSha256)}` : ""}
            {governance.snapshotSha256 ? ` · 快照 ${shortHash(governance.snapshotSha256)}` : ""}
          </small>
        </span>
        <GovernanceStatus
          ready={governance.ready}
          neutral={!governance.applicable}
          readyLabel={governance.applicable ? "记录完整" : "本产物不适用"}
          blockedLabel="记录待补齐"
        />
      </div>
      {!governance.applicable ? (
        <p className="artifact-governance-empty">
          {governance.issues?.[0] || "该产物未启用候选谱系与精确版本风控治理；不会补写或推断相关记录。"}
        </p>
      ) : null}
      {governance.applicable && !governance.integrityOk ? (
        <p className="artifact-governance-empty warning" role="alert">
          治理快照完整性校验未通过；以下记录仅用于诊断，不能作为已绑定治理记录。
        </p>
      ) : null}
      {governance.applicable && !governance.safetyOk ? (
        <p className="artifact-governance-empty warning" role="alert">
          治理快照的无执行能力边界出现矛盾；前端已按失败关闭，不显示为治理完成。
        </p>
      ) : null}
      {governance.applicable ? <GovernanceIssues issues={governance.issues} /> : null}

      {governance.applicable ? (
        <div className="artifact-governance-ledger" role="list" aria-label="候选治理快照摘要">
          <span role="listitem"><small>冻结候选</small><strong>{lineage.candidates.length}</strong></span>
          <span role="listitem"><small>当前版本覆盖</small><strong>{riskReview.applicable ? `${riskReview.reviewedCandidateCount}/${riskReview.targetCandidateCount}` : "不要求"}</strong></span>
          <span role="listitem"><small>过期意见</small><strong>{riskReview.staleReviewCount}</strong></span>
          <span role="listitem"><small>授权输出</small><strong>不产生</strong></span>
        </div>
      ) : null}

      {governance.applicable ? <div className="artifact-governance-grid">
        <section className="artifact-governance-layer lineage" aria-labelledby={lineageTitleId} data-layer-state={lineage.ready ? "ready" : "blocked"}>
          <div className="artifact-governance-layer-heading">
            <GitBranch size={16} aria-hidden="true" />
            <span>
              <strong id={lineageTitleId}>第一层 · 候选形成谱系</strong>
              <small>候选只能引用形成时的来源消息和精确修订号。</small>
            </span>
            <GovernanceStatus
              ready={lineage.ready}
              readyLabel="谱系完整"
              blockedLabel="谱系不完整"
            />
          </div>
          {lineage.available ? (
            <>
              <p className="artifact-governance-summary">
                {lineage.candidates.length} 个候选
                {lineage.version ? ` · ${lineage.version}` : ""}
                {lineage.decisionMessageId
                  ? <> · 决策消息 <code>{lineage.decisionMessageId}</code></>
                  : " · 尚未绑定决策消息"}
              </p>
              <p className="artifact-governance-list-status" role="status">
                <span>展示 {visibleCandidates.length} / {lineage.candidates.length} 个冻结候选</span>
                <progress aria-label="冻结候选挂载进度" max={lineage.candidates.length || 1} tabIndex={-1} value={visibleCandidates.length} />
              </p>
              <div className="artifact-lineage-list" id={candidateListId} role="list" aria-label="冻结候选谱系">
                {visibleCandidates.map((candidate) => (
                  <CandidateReference
                    candidate={candidate}
                    key={candidate.projectionKey}
                  />
                ))}
                {!lineage.candidates.length
                  ? <p className="artifact-governance-empty">快照中没有可展示的候选谱系。</p>
                  : null}
              </div>
              {visibleCandidates.length < lineage.candidates.length ? (
                <button aria-controls={candidateListId} type="button" className="secondary artifact-governance-more" onClickCapture={focusGovernanceProgressAfterRender} onClick={() => setCandidateLimit((current) => Math.min(current + CANDIDATE_PAGE_SIZE, lineage.candidates.length))}>
                  再显示 {Math.min(CANDIDATE_PAGE_SIZE, lineage.candidates.length - visibleCandidates.length)} 个候选
                </button>
              ) : null}
              <GovernanceIssues issues={lineage.issues} label="谱系问题" />
            </>
          ) : (
            <p className="artifact-governance-empty warning">治理快照未包含候选形成谱系，不能推断候选来源。</p>
          )}
        </section>

        <section className="artifact-governance-layer risk" aria-labelledby={riskReviewTitleId} data-layer-state={riskReview.ready ? "ready" : riskReview.applicable ? "blocked" : "neutral"}>
          <div className="artifact-governance-layer-heading">
            <ShieldAlert size={16} aria-hidden="true" />
            <span>
              <strong id={riskReviewTitleId}>第二层 · 精确版本风控意见</strong>
              <small>每条意见绑定候选修订、复核消息和成员版本。</small>
            </span>
            <GovernanceStatus
              ready={riskReview.ready}
              readyLabel={riskReview.applicable ? "版本复核完整" : "本轮未要求"}
              blockedLabel="版本复核不完整"
            />
          </div>
          {!riskReview.available ? (
            <p className="artifact-governance-empty warning">治理快照未包含精确版本风控记录，不能推断风控态度。</p>
          ) : !riskReview.applicable ? (
            <p className="artifact-governance-empty">该轮次未启用精确版本风控复核协议；不会补写或猜测风控意见。</p>
          ) : (
            <>
              <p className="artifact-governance-summary">
                当前版本覆盖 {riskReview.reviewedCandidateCount} / {riskReview.targetCandidateCount} 个候选
                {` · 当前意见 ${riskReview.currentReviewCount} · 过期 ${riskReview.staleReviewCount}`}
                {` · 处置总计（含过期）：支持 ${riskReview.actionCounts.support} / 质疑 ${riskReview.actionCounts.challenge} / 拒绝 ${riskReview.actionCounts.reject}`}
              </p>
              <p className="artifact-governance-list-status" role="status">
                <span>展示 {visibleReviews.length} / {riskReview.reviews.length} 条精确版本意见</span>
                <progress aria-label="精确版本意见挂载进度" max={riskReview.reviews.length || 1} tabIndex={-1} value={visibleReviews.length} />
              </p>
              <div className="artifact-risk-review-list" id={riskReviewListId} role="list" aria-label="精确版本风控意见">
                {visibleReviews.map((review) => (
                  <RiskReviewReference
                    review={review}
                    key={review.projectionKey}
                  />
                ))}
                {!riskReview.reviews.length
                  ? <p className="artifact-governance-empty">尚无可展示的精确版本风控意见。</p>
                  : null}
              </div>
              {visibleReviews.length < riskReview.reviews.length ? (
                <button aria-controls={riskReviewListId} type="button" className="secondary artifact-governance-more" onClickCapture={focusGovernanceProgressAfterRender} onClick={() => setReviewLimit((current) => Math.min(current + REVIEW_PAGE_SIZE, riskReview.reviews.length))}>
                  再显示 {Math.min(REVIEW_PAGE_SIZE, riskReview.reviews.length - visibleReviews.length)} 条意见
                </button>
              ) : null}
              <GovernanceIssues issues={riskReview.issues} label="风控复核问题" />
            </>
          )}
        </section>
      </div> : null}

      <p className="artifact-governance-boundary" id={boundaryId} role="note">
        <ShieldAlert size={15} aria-hidden="true" />
        <span>
          <strong>{governance.boundary || "候选治理记录不产生执行或最终决定权限。"}</strong>
          <small>所有记录均无交易、投注、支付或其他资金执行能力；第三层用户最终决定在下方独立记录。</small>
        </span>
      </p>
    </section>
  );
});
