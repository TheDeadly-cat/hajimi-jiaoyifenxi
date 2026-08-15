import {
  AlertTriangle,
  CheckCircle2,
  GitBranch,
  ShieldAlert,
} from "lucide-react";
import { artifactCandidateGovernance } from "../candidateGovernance";

function GovernanceStatus({ ready, readyLabel, blockedLabel, neutral = false }) {
  const Icon = neutral ? GitBranch : ready ? CheckCircle2 : AlertTriangle;
  const tone = neutral ? "neutral" : ready ? "ready" : "blocked";
  return (
    <span className={`artifact-governance-status ${tone}`}>
      <Icon size={12} />
      {neutral ? readyLabel : ready ? readyLabel : blockedLabel}
    </span>
  );
}

function GovernanceIssues({ issues }) {
  if (!issues.length) return null;
  return (
    <ul className="artifact-governance-issues">
      {issues.map((issue, index) => <li key={`${issue}:${index}`}>{issue}</li>)}
    </ul>
  );
}

function CandidateReference({ candidate }) {
  return (
    <article className="artifact-lineage-item">
      <span>
        <strong>{candidate.title || candidate.id || "未命名候选"}</strong>
        {candidate.preferred ? <em>决策板首选</em> : null}
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
}

function reviewVersionText(review) {
  if (!review.candidateRevision) return "候选修订号未记录";
  if (review.status === "current") return `绑定当前精确版本 r${review.candidateRevision}`;
  if (review.status === "stale") {
    return `已过期：绑定 r${review.candidateRevision}，当前 r${review.currentCandidateRevision || "?"}`;
  }
  return `绑定 r${review.candidateRevision}，版本状态未记录`;
}

function RiskReviewReference({ review }) {
  const candidateTitle = String(review.candidateSnapshot?.title || "").trim();
  return (
    <article className={`artifact-risk-review-item ${review.status}`}>
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
          候选快照 SHA-256 <code>{review.candidateSnapshotSha256.slice(0, 16)}…</code>
        </small>
      ) : null}
      {review.riskIds.length ? <small>关联风险：{review.riskIds.join("、")}</small> : null}
    </article>
  );
}

export function ArtifactCandidateGovernance({ artifact }) {
  const governance = artifactCandidateGovernance(artifact);
  if (!governance.available) return null;
  const { lineage, riskReview } = governance;

  return (
    <section className="artifact-candidate-governance" aria-labelledby="artifact-candidate-governance-title">
      <div className="artifact-candidate-governance-heading">
        <span>
          <strong id="artifact-candidate-governance-title">候选治理记录</strong>
          <small>
            服务端只读快照；前两层保留候选来源和风控复核，不写入可编辑纪要正文。
            {governance.version ? ` · ${governance.version}` : ""}
            {governance.attestationVersion ? ` · ${governance.attestationVersion}` : ""}
            {governance.attestationSha256 ? ` · 封印 ${governance.attestationSha256.slice(0, 12)}…` : ""}
            {governance.snapshotSha256 ? ` · 快照 ${governance.snapshotSha256.slice(0, 12)}…` : ""}
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
          {governance.issues[0] || "该产物未启用候选谱系与精确版本风控治理；不会补写或推断相关记录。"}
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

      {governance.applicable ? <div className="artifact-governance-grid">
        <section className="artifact-governance-layer lineage" aria-labelledby="artifact-lineage-title">
          <div className="artifact-governance-layer-heading">
            <GitBranch size={16} />
            <span>
              <strong id="artifact-lineage-title">第一层 · 候选形成谱系</strong>
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
              <div className="artifact-lineage-list">
                {lineage.candidates.map((candidate, index) => (
                  <CandidateReference
                    candidate={candidate}
                    key={candidate.id || `${candidate.originMessageId}:${index}`}
                  />
                ))}
                {!lineage.candidates.length
                  ? <p className="artifact-governance-empty">快照中没有可展示的候选谱系。</p>
                  : null}
              </div>
              <GovernanceIssues issues={lineage.issues} />
            </>
          ) : (
            <p className="artifact-governance-empty warning">治理快照未包含候选形成谱系，不能推断候选来源。</p>
          )}
        </section>

        <section className="artifact-governance-layer risk" aria-labelledby="artifact-risk-review-title">
          <div className="artifact-governance-layer-heading">
            <ShieldAlert size={16} />
            <span>
              <strong id="artifact-risk-review-title">第二层 · 精确版本风控意见</strong>
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
              <div className="artifact-risk-review-list">
                {riskReview.reviews.map((review, index) => (
                  <RiskReviewReference
                    review={review}
                    key={`${review.reviewMessageId}:${review.candidateId}:${index}`}
                  />
                ))}
                {!riskReview.reviews.length
                  ? <p className="artifact-governance-empty">尚无可展示的精确版本风控意见。</p>
                  : null}
              </div>
              <GovernanceIssues issues={riskReview.issues} />
            </>
          )}
        </section>
      </div> : null}

      <p className="artifact-governance-boundary" role="note">
        <ShieldAlert size={15} />
        <span>
          <strong>{governance.boundary}</strong>
          <small>所有记录均无交易、投注、支付或其他资金执行能力；第三层用户最终决定在下方独立记录。</small>
        </span>
      </p>
    </section>
  );
}
