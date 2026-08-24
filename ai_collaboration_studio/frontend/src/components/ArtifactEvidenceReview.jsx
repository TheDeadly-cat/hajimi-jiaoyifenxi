import { AlertTriangle, ArrowRight, CheckCircle2, ShieldQuestion } from "lucide-react";
import { memo, useEffect, useId, useMemo, useRef, useState } from "react";
import { preferredScrollBehavior } from "../motionPreferences";
import { auditDefaults, evidenceRoleLabels, verificationStatusLabels } from "../artifactEvidence";
import {
  evidencePreviewAllowsVerification,
  evidenceSourceDetailKey,
  evidenceSourceDetailRequired as needsSourceDetail,
  summarizeEvidenceRelations,
} from "../artifactEvidenceSources";
import {
  ARTIFACT_EVIDENCE_REVIEW_PAGE_SIZE,
  artifactEvidenceReviewSourceState,
} from "../artifactEvidenceReviewUi";
import { EvidenceSourcePreview } from "./EvidenceSourcePreview";
import { EvidenceStatusStrip } from "./EvidenceStatusStrip";
import "../styles/artifact-evidence-review-refinement.css";

const roleOptions = Object.entries(evidenceRoleLabels);
const statusOptions = Object.entries(verificationStatusLabels);
const completePreviewStatuses = new Set(["source_checked", "corroborated"]);
const reviewedStatuses = new Set(["source_checked", "corroborated", "disputed"]);
const EMPTY_RECORD = Object.freeze({});

function recordMap(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : EMPTY_RECORD;
}

function nonnegativeInteger(value) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const numeric = Number(value);
  return Number.isInteger(numeric) && numeric >= 0 ? numeric : null;
}

function countLabel(value) {
  const numeric = nonnegativeInteger(value);
  return numeric === null ? "未记录" : String(numeric);
}

function sourceVersion(value) {
  return nonnegativeInteger(value);
}

function versionLabel(value) {
  return value === null ? "版本未记录" : `v${value}`;
}

export const ArtifactEvidenceReview = memo(function ArtifactEvidenceReview({
  candidates,
  targets,
  activeTarget,
  onTargetChange,
  selectedEvidence,
  reviewByKey,
  onToggle,
  onReviewChange,
  onVersionDecision,
  reviewSummary,
  onNextUnreviewed,
  sourceDetailByKey,
  onLoadSourceDetail,
  focusedEvidenceKey,
}) {
  const titleId = useId();
  const optionRefs = useRef({});
  const [candidateLimit, setCandidateLimit] = useState(ARTIFACT_EVIDENCE_REVIEW_PAGE_SIZE);
  const reviewSourceState = useMemo(
    () => artifactEvidenceReviewSourceState({ candidates, targets, selectedEvidence }),
    [candidates, selectedEvidence, targets],
  );
  const { candidateEntries, candidateRows, selectedKeys, targetRows } = reviewSourceState;
  const reviews = recordMap(reviewByKey);
  const sourceDetails = recordMap(sourceDetailByKey);
  const summary = recordMap(reviewSummary);
  const selectedEvidenceSet = useMemo(() => new Set(selectedKeys), [selectedKeys]);
  const visibleCandidateEntries = useMemo(
    () => candidateEntries.filter((entry, index) => (
      index < candidateLimit || selectedEvidenceSet.has(entry.key)
    )),
    [candidateEntries, candidateLimit, selectedEvidenceSet],
  );
  const hiddenCandidateCount = candidateEntries.length - visibleCandidateEntries.length;
  const { activePendingCount, counterCount } = useMemo(() => {
    let pending = 0;
    let counter = 0;
    for (const key of selectedKeys) {
      const audit = auditDefaults(reviews[key] || EMPTY_RECORD);
      if (!reviewedStatuses.has(audit.verification_status)) pending += 1;
      if (audit.evidence_role === "counter") counter += 1;
    }
    return { activePendingCount: pending, counterCount: counter };
  }, [reviews, selectedKeys]);
  const totalRelations = nonnegativeInteger(summary.relationCount);
  const reviewedRelations = nonnegativeInteger(summary.reviewedCount);
  const unreviewedRelations = nonnegativeInteger(summary.unreviewedCount);
  const relationCountsKnown = totalRelations !== null
    && reviewedRelations !== null
    && unreviewedRelations !== null;
  const confirmationIssueCount = Array.isArray(summary.issues) ? summary.issues.length : 0;
  const sourceUsage = recordMap(summary.sourceUsage);
  const canChangeTarget = typeof onTargetChange === "function";
  const canToggle = typeof onToggle === "function";
  const canReview = typeof onReviewChange === "function";
  const canDecideVersion = typeof onVersionDecision === "function";
  const canAdvance = typeof onNextUnreviewed === "function";
  const evidenceStatusSummary = useMemo(
    () => summarizeEvidenceRelations(
      candidateRows,
      selectedKeys,
      reviews,
      sourceDetails,
    ),
    [candidateRows, reviews, selectedKeys, sourceDetails],
  );
  useEffect(() => {
    setCandidateLimit(ARTIFACT_EVIDENCE_REVIEW_PAGE_SIZE);
  }, [activeTarget, candidates]);
  useEffect(() => {
    const focusKey = typeof focusedEvidenceKey === "string" ? focusedEvidenceKey : "";
    const target = optionRefs.current[focusKey];
    if (!target) return;
    target.scrollIntoView?.({
      behavior: preferredScrollBehavior(),
      block: "center",
    });
    target.focus?.({ preventScroll: true });
  }, [activeTarget, focusedEvidenceKey]);

  return (
    <section className="artifact-evidence-review evidence-review-workbench" aria-labelledby={titleId}>
      <div className="artifact-evidence-heading">
        <strong id={titleId}>绑定与核验证据</strong>
        <span className={activePendingCount ? "pending" : "reviewed"}>
          {activePendingCount ? <ShieldQuestion size={12} aria-hidden="true" /> : <CheckCircle2 size={12} aria-hidden="true" />}
          {activePendingCount ? `当前条目 ${activePendingCount} 条待完成标注` : selectedKeys.length ? "当前条目已标注" : "尚未选择"}
        </span>
      </div>
      <p>所选来源只绑定到当前条目。“已核对原文”只表示你检查了来源与引用关系，不代表事实已经交叉证实。</p>
      {reviewSourceState.issues.length ? (
        <p className="artifact-evidence-review-source-warning" role="alert">{reviewSourceState.issues?.[0]}</p>
      ) : null}
      <div className="artifact-evidence-review-ledger" role="list" aria-label="当前条目证据状态">
        <span role="listitem"><small>已绑定</small><strong>{selectedKeys.length}</strong></span>
        <span role="listitem"><small>待核验</small><strong>{activePendingCount}</strong></span>
        <span role="listitem"><small>反证</small><strong>{counterCount}</strong></span>
        <span role="listitem"><small>授权结论</small><strong>不产生</strong></span>
      </div>
      <div className="artifact-evidence-progress" data-count-state={relationCountsKnown ? "known" : "unknown"}>
        <span role="status" aria-live="polite" aria-atomic="true">
          <strong>{countLabel(reviewedRelations)} / {countLabel(totalRelations)}</strong>
          条关系已核验 · {countLabel(summary.uniqueSourceCount)} 个去重来源记录
        </span>
        <EvidenceStatusStrip counts={evidenceStatusSummary} />
        <button type="button" className="secondary compact" disabled={!relationCountsKnown || !unreviewedRelations || !canAdvance} onClick={canAdvance ? onNextUnreviewed : undefined}>
          {!relationCountsKnown
            ? <><AlertTriangle size={12} aria-hidden="true" />核验计数未记录</>
            : unreviewedRelations
            ? <><ArrowRight size={12} aria-hidden="true" />下一条未核验</>
            : confirmationIssueCount
              ? <><AlertTriangle size={12} aria-hidden="true" />仍有 {confirmationIssueCount} 项确认问题</>
              : <><CheckCircle2 size={12} aria-hidden="true" />关系标注已完成</>}
        </button>
      </div>
      <label className="artifact-evidence-target">当前标注条目
        <select
          value={typeof activeTarget === "string" ? activeTarget : ""}
          onChange={(event) => canChangeTarget && onTargetChange(event.target.value)}
          disabled={!targetRows.length || !canChangeTarget}
        >
          <option value="" disabled>选择需要核验的条目</option>
          {targetRows.map((target) => <option value={target.key} key={target.key}>{target.label}</option>)}
        </select>
      </label>
      {counterCount ? <p className="artifact-counter-note"><AlertTriangle size={11} aria-hidden="true" />已保留 {counterCount} 条反证；确认前必须填写它为何构成反证。</p> : null}
      {candidateEntries.length ? (
        <>
        <p className="artifact-evidence-review-list-status" role="status" aria-live="polite">
          展示 {visibleCandidateEntries.length} / {candidateEntries.length} 个来源；所有已绑定来源始终可见。
        </p>
        <div className="artifact-evidence-options" role="list" aria-label="可绑定证据来源">
          {visibleCandidateEntries.map(({ item, key }) => {
            const selected = selectedEvidenceSet.has(key);
            const rawAudit = recordMap(reviews[key]);
            const normalizedAudit = auditDefaults(rawAudit);
            const citedVersion = sourceVersion(rawAudit.version) ?? sourceVersion(item.version);
            const latestVersion = sourceVersion(rawAudit.latest_version)
              ?? sourceVersion(item.latestVersion)
              ?? sourceVersion(item.version)
              ?? citedVersion;
            const audit = {
              ...normalizedAudit,
              ...(citedVersion !== null ? { version: citedVersion } : {}),
              ...(latestVersion !== null ? { latest_version: latestVersion } : {}),
            };
            const itemStatus = typeof item.status === "string" ? item.status.trim().toLowerCase() : "";
            const versionChanged = item.type === "material" && (
              (citedVersion !== null && latestVersion !== null && citedVersion < latestVersion)
              || ["superseded", "inactive", "unavailable"].includes(audit.version_status || item.versionStatus)
            );
            const sourceUnavailable = audit.version_status === "unavailable"
              || ["error", "missing", "unavailable", "unresolved"].includes(itemStatus);
            const canMigrate = item.type === "material"
              && audit.source_active !== false
              && item.sourceActive !== false
              && !sourceUnavailable
              && latestVersion !== null
              && latestVersion > 0;
            const sourceDetailKey = evidenceSourceDetailKey(item, audit);
            const sourceDetail = sourceDetails[sourceDetailKey];
            const sourceDetailRequired = needsSourceDetail(item, audit);
            const exactPreviewReady = evidencePreviewAllowsVerification(item, audit, sourceDetail);
            const sourceUsageCount = nonnegativeInteger(sourceUsage[key]);
            return (
              <div
                className={`artifact-evidence-option${selected ? " selected" : ""}${focusedEvidenceKey === key ? " focused" : ""}${item.unresolved === true ? " unresolved" : ""}`}
                key={key}
                ref={(node) => {
                  if (node) optionRefs.current[key] = node;
                  else delete optionRefs.current[key];
                }}
                role="listitem"
                tabIndex={-1}
                data-verification={audit.verification_status}
                data-evidence-role={audit.evidence_role}
                data-source-gap={!exactPreviewReady || item.unresolved === true ? "true" : "false"}
              >
                <label className="artifact-evidence-source">
                  <input
                    type="checkbox"
                    checked={selected}
                    disabled={!canToggle || (!selected && (item.selectable === false || reviewSourceState.blockNewBindings))}
                    title={!canToggle
                      ? "绑定处理器不可用"
                      : item.selectable === false || reviewSourceState.blockNewBindings
                        ? "该来源存在精确读取缺口，不能新增绑定"
                        : ""}
                    onChange={(event) => canToggle && onToggle(key, event.target.checked)}
                  />
                  <span className="artifact-evidence-source-title">{item.label}</span>
                </label>
                {selected || item.unresolved
                  ? <EvidenceStatusStrip source={item} audit={audit} sourceDetail={sourceDetail} compact />
                  : null}
                {selected ? (
                  <>
                    {versionChanged ? (
                      <div className="artifact-version-review">
                        <AlertTriangle size={12} aria-hidden="true" />
                        <span>
                          {sourceUnavailable
                            ? `当前引用${versionLabel(citedVersion)}已不可读取；请取消此引用或先恢复来源。`
                            : `当前引用${versionLabel(citedVersion)}，资料最新版为${versionLabel(latestVersion)}。${audit.version_decision === "keep_snapshot" ? " 已选择保留历史快照，请在核验说明中记录原因。" : " 必须选择处理方式。"}`}
                        </span>
                        {!sourceUnavailable && citedVersion !== null ? (
                          <button type="button" disabled={!canDecideVersion} className={audit.version_decision === "keep_snapshot" ? "active" : ""} onClick={() => canDecideVersion && onVersionDecision(key, "keep_snapshot", latestVersion)}>保留 v{citedVersion}</button>
                        ) : null}
                        {canMigrate ? <button type="button" disabled={!canDecideVersion} onClick={() => canDecideVersion && onVersionDecision(key, "migrate_current", latestVersion)}>迁移到 v{latestVersion}</button> : null}
                      </div>
                    ) : null}
                    <div className="artifact-evidence-audit">
                    <label>用途
                      <select disabled={!canReview} value={audit.evidence_role} onChange={(event) => canReview && onReviewChange(key, "evidence_role", event.target.value)}>
                        {roleOptions.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
                      </select>
                    </label>
                    <label>核验状态
                      <select
                        disabled={!canReview}
                        value={audit.verification_status}
                        title={!exactPreviewReady ? "请先读取被引用的精确来源；当前摘要或最新版不能替代" : ""}
                        onChange={(event) => canReview && onReviewChange(key, "verification_status", event.target.value)}
                      >
                        {statusOptions.map(([value, label]) => (
                          <option
                            value={value}
                            key={value}
                            disabled={!exactPreviewReady && completePreviewStatuses.has(value)}
                          >
                            {label}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="artifact-review-note">核验说明
                      <input
                        disabled={!canReview}
                        value={audit.review_note}
                        autoComplete="off"
                        onChange={(event) => canReview && onReviewChange(key, "review_note", event.target.value)}
                        placeholder={audit.evidence_role === "counter" || audit.verification_status === "disputed" ? "必填：说明冲突、限制或反证关系" : "可选：记录核对位置或交叉来源"}
                      />
                    </label>
                    </div>
                    <EvidenceSourcePreview
                      item={item}
                      citedVersion={citedVersion ?? 0}
                      sourceDetail={sourceDetail}
                      sourceDetailRequired={sourceDetailRequired}
                      onLoadSourceDetail={onLoadSourceDetail}
                      selected
                    />
                    {sourceUsageCount !== null && sourceUsageCount > 1
                      ? <small className="artifact-source-usage">该来源在本产物中被引用 {sourceUsageCount} 次；每条引用关系仍需分别核验。</small>
                      : null}
                  </>
                ) : item.previewComplete === true ? (
                  <EvidenceSourcePreview
                    item={item}
                    citedVersion={citedVersion ?? 0}
                    sourceDetail={sourceDetail}
                    sourceDetailRequired={false}
                    onLoadSourceDetail={onLoadSourceDetail}
                  />
                ) : item.unresolved && item.sourceMeta ? (
                  <small className="artifact-evidence-gap-note">{item.sourceMeta}</small>
                ) : null}
              </div>
            );
          })}
        </div>
        {hiddenCandidateCount ? (
          <button
            type="button"
            className="secondary artifact-evidence-review-more"
            onClick={() => setCandidateLimit((current) => current + ARTIFACT_EVIDENCE_REVIEW_PAGE_SIZE)}
          >
            再显示 {Math.min(ARTIFACT_EVIDENCE_REVIEW_PAGE_SIZE, hiddenCandidateCount)} 个来源
          </button>
        ) : null}
        </>
      ) : reviewSourceState.blockNewBindings
        ? <p className="artifact-evidence-review-source-warning" role="alert">来源集合超过安全显示上限，当前不能新增绑定。</p>
        : <p>当前房间没有可绑定的消息或资料，产物可以保存草稿，但不能确认。</p>}
    </section>
  );
});
