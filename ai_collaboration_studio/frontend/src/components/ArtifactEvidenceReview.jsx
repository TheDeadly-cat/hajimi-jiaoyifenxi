import { AlertTriangle, ArrowRight, CheckCircle2, ShieldQuestion } from "lucide-react";
import { useEffect, useMemo, useRef } from "react";
import { preferredScrollBehavior } from "../motionPreferences";
import { evidenceRoleLabels, verificationStatusLabels } from "../artifactEvidence";
import {
  evidencePreviewAllowsVerification,
  evidenceSourceDetailKey,
  evidenceSourceDetailRequired as needsSourceDetail,
  summarizeEvidenceRelations,
} from "../artifactEvidenceSources";
import { EvidenceSourcePreview } from "./EvidenceSourcePreview";
import { EvidenceStatusStrip } from "./EvidenceStatusStrip";

const roleOptions = Object.entries(evidenceRoleLabels);
const statusOptions = Object.entries(verificationStatusLabels);
const completePreviewStatuses = new Set(["source_checked", "corroborated"]);

export function ArtifactEvidenceReview({
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
  const optionRefs = useRef({});
  const selectedEvidenceSet = useMemo(() => new Set(selectedEvidence), [selectedEvidence]);
  const selectedAudits = selectedEvidence.map((key) => reviewByKey[key]).filter(Boolean);
  const activeUnreviewedCount = selectedAudits.filter((audit) => audit.verification_status === "unreviewed").length;
  const counterCount = selectedAudits.filter((audit) => audit.evidence_role === "counter").length;
  const totalRelations = Number(reviewSummary?.relationCount || 0);
  const reviewedRelations = Number(reviewSummary?.reviewedCount || 0);
  const unreviewedRelations = Number(reviewSummary?.unreviewedCount || 0);
  const confirmationIssueCount = Number(reviewSummary?.issues?.length || 0);
  const evidenceStatusSummary = useMemo(
    () => summarizeEvidenceRelations(
      candidates,
      selectedEvidence,
      reviewByKey,
      sourceDetailByKey,
    ),
    [candidates, reviewByKey, selectedEvidence, sourceDetailByKey],
  );
  useEffect(() => {
    const target = optionRefs.current[focusedEvidenceKey];
    if (!target) return;
    target.scrollIntoView({
      behavior: preferredScrollBehavior(),
      block: "center",
    });
    target.focus({ preventScroll: true });
  }, [activeTarget, focusedEvidenceKey]);

  return (
    <section className="artifact-evidence-review">
      <div className="artifact-evidence-heading">
        <strong>绑定与核验证据</strong>
        <span className={activeUnreviewedCount ? "pending" : "ready"}>
          {activeUnreviewedCount ? <ShieldQuestion size={12} /> : <CheckCircle2 size={12} />}
          {activeUnreviewedCount ? `当前条目 ${activeUnreviewedCount} 条未核验` : selectedEvidence.length ? "当前条目已标注" : "尚未选择"}
        </span>
      </div>
      <p>所选来源只绑定到当前条目。“已核对原文”只表示你检查了来源与引用关系，不代表事实已经交叉证实。</p>
      <div className="artifact-evidence-progress" role="status">
        <span>
          <strong>{reviewedRelations} / {totalRelations}</strong>
          条关系已核验 · {Number(reviewSummary?.uniqueSourceCount || 0)} 个去重来源记录
        </span>
        <EvidenceStatusStrip counts={evidenceStatusSummary} />
        <button type="button" className="secondary compact" disabled={!unreviewedRelations} onClick={onNextUnreviewed}>
          {unreviewedRelations
            ? <><ArrowRight size={12} />下一条未核验</>
            : confirmationIssueCount
              ? <><AlertTriangle size={12} />仍有 {confirmationIssueCount} 项确认问题</>
              : <><CheckCircle2 size={12} />确认门已就绪</>}
        </button>
      </div>
      <label className="artifact-evidence-target">当前标注条目
        <select value={activeTarget} onChange={(event) => onTargetChange(event.target.value)}>
          {targets.map((target) => <option value={target.key} key={target.key}>{target.label}</option>)}
        </select>
      </label>
      {counterCount ? <p className="artifact-counter-note"><AlertTriangle size={11} />已保留 {counterCount} 条反证；确认前必须填写它为何构成反证。</p> : null}
      {candidates.length ? (
        <div className="artifact-evidence-options">
          {candidates.map((item) => {
            const key = `${item.type}:${item.id}`;
            const selected = selectedEvidenceSet.has(key);
            const audit = reviewByKey[key] || {
              evidence_role: "context",
              verification_status: "unreviewed",
              review_note: "",
            };
            const citedVersion = Number.isInteger(Number(audit.version)) ? Number(audit.version) : Number(item.version || 0);
            const latestVersion = Number(audit.latest_version || item.latestVersion || item.version || citedVersion);
            const versionChanged = item.type === "material" && (
              citedVersion < latestVersion
              || ["superseded", "inactive", "unavailable"].includes(audit.version_status || item.versionStatus)
            );
            const sourceUnavailable = audit.version_status === "unavailable"
              || ["error", "missing", "unavailable", "unresolved"].includes(item.status);
            const canMigrate = item.type === "material"
              && audit.source_active !== false
              && item.sourceActive !== false
              && !sourceUnavailable
              && latestVersion > 0;
            const sourceDetailKey = evidenceSourceDetailKey(item, audit);
            const sourceDetail = sourceDetailByKey?.[sourceDetailKey];
            const sourceDetailRequired = needsSourceDetail(item, audit);
            const exactPreviewReady = evidencePreviewAllowsVerification(item, audit, sourceDetail);
            return (
              <div
                className={`artifact-evidence-option ${selected ? "selected" : ""} ${focusedEvidenceKey === key ? "focused" : ""}`}
                key={key}
                ref={(node) => { optionRefs.current[key] = node; }}
                tabIndex={-1}
              >
                <label className="artifact-evidence-source">
                  <input
                    type="checkbox"
                    checked={selected}
                    disabled={item.selectable === false && !selected}
                    title={item.selectable === false ? "该来源存在精确读取缺口，不能新增绑定" : ""}
                    onChange={(event) => onToggle(key, event.target.checked)}
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
                        <AlertTriangle size={12} />
                        <span>
                          {sourceUnavailable
                            ? `当前引用 v${citedVersion} 已不可读取；请取消此引用或先恢复来源。`
                            : `当前引用 v${citedVersion}，资料最新版为 v${latestVersion}。${audit.version_decision === "keep_snapshot" ? " 已选择保留历史快照，请在核验说明中记录原因。" : " 必须选择处理方式。"}`}
                        </span>
                        {!sourceUnavailable ? (
                          <button type="button" className={audit.version_decision === "keep_snapshot" ? "active" : ""} onClick={() => onVersionDecision(key, "keep_snapshot", latestVersion)}>保留 v{citedVersion}</button>
                        ) : null}
                        {canMigrate ? <button type="button" onClick={() => onVersionDecision(key, "migrate_current", latestVersion)}>迁移到 v{latestVersion}</button> : null}
                      </div>
                    ) : null}
                    <div className="artifact-evidence-audit">
                    <label>用途
                      <select value={audit.evidence_role} onChange={(event) => onReviewChange(key, "evidence_role", event.target.value)}>
                        {roleOptions.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
                      </select>
                    </label>
                    <label>核验状态
                      <select
                        value={audit.verification_status}
                        title={!exactPreviewReady ? "请先读取被引用的精确来源；当前摘要或最新版不能替代" : ""}
                        onChange={(event) => onReviewChange(key, "verification_status", event.target.value)}
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
                        value={audit.review_note}
                        onChange={(event) => onReviewChange(key, "review_note", event.target.value)}
                        placeholder={audit.evidence_role === "counter" || audit.verification_status === "disputed" ? "必填：说明冲突、限制或反证关系" : "可选：记录核对位置或交叉来源"}
                      />
                    </label>
                    </div>
                    <EvidenceSourcePreview
                      item={item}
                      citedVersion={citedVersion}
                      sourceDetail={sourceDetail}
                      sourceDetailRequired={sourceDetailRequired}
                      onLoadSourceDetail={onLoadSourceDetail}
                      selected
                    />
                    {Number(reviewSummary?.sourceUsage?.[key] || 0) > 1
                      ? <small className="artifact-source-usage">该来源在本产物中被引用 {reviewSummary.sourceUsage[key]} 次；每条引用关系仍需分别核验。</small>
                      : null}
                  </>
                ) : item.previewComplete === true ? (
                  <EvidenceSourcePreview
                    item={item}
                    citedVersion={citedVersion}
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
      ) : <p>当前房间没有可绑定的消息或资料，产物可以保存草稿，但不能确认。</p>}
    </section>
  );
}
