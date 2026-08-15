import {
  AlertTriangle,
  CheckCircle2,
  CirclePause,
  FileEdit,
  GitBranch,
  LoaderCircle,
  RotateCcw,
  Sparkles,
  ThumbsUp,
} from "lucide-react";
import { useMemo, useState } from "react";
import { evidenceAuditSummary } from "../artifactEvidence";
import { artifactDecisionState, userDecisionLabel } from "../artifactUserDecision";
import { artifactGovernanceBadge } from "../candidateGovernance";

const statusLabel = (artifact) => artifact.status === "CONFIRMED" ? "已确认" : "待确认";

function decisionBadge(artifact) {
  const { current, latest } = artifactDecisionState(artifact);
  if (current) {
    const icons = { support: ThumbsUp, hold: CirclePause, return: RotateCcw };
    return {
      Icon: icons[current.action] || CheckCircle2,
      label: userDecisionLabel(current, true),
      tone: current.action || "current",
      title: `当前决定绑定产物 v${current.artifact_version || artifact.version}`,
    };
  }
  if (latest) {
    return {
      Icon: AlertTriangle,
      label: "决定已过期",
      tone: "stale",
      title: latest.stale_reason || `此前决定绑定产物 v${latest.artifact_version || "?"}`,
    };
  }
  if (artifact.status === "CONFIRMED") {
    return {
      Icon: CirclePause,
      label: "待最终决定",
      tone: "pending",
      title: `当前已确认产物 v${artifact.version} 尚未记录最终决定`,
    };
  }
  return null;
}

export function ArtifactPanel({
  artifacts,
  members = [],
  loading,
  generationDisabled = false,
  generationDisabledReason = "",
  onGenerate,
  onEdit,
}) {
  const [selectedSynthesizerId, setSelectedSynthesizerId] = useState("");
  const synthesizerMembers = useMemo(
    () => members.filter((member) => member.enabled && member.provider),
    [members],
  );
  const activeSynthesizerId = synthesizerMembers.some(
    (member) => member.id === selectedSynthesizerId,
  ) ? selectedSynthesizerId : "";
  const generateDisabled = loading || generationDisabled;
  return (
    <div className="artifact-panel">
      {synthesizerMembers.length ? (
        <label className="artifact-synthesizer">
          <span>整理成员</span>
          <select
            value={activeSynthesizerId}
            onChange={(event) => setSelectedSynthesizerId(event.target.value)}
            disabled={generateDisabled}
            aria-label="选择会议草稿整理成员"
          >
            <option value="">自动选择方案整合角色</option>
            {synthesizerMembers.map((member) => (
              <option key={member.id} value={member.id}>
                {member.name} · {member.provider}/{member.model || "默认模型"}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      <button
        className="artifact-generate"
        type="button"
        onClick={(event) => onGenerate(activeSynthesizerId, event.currentTarget)}
        disabled={generateDisabled}
        title={generationDisabled ? generationDisabledReason : "把最近一轮已结束的讨论整理成可核验草稿"}
      >
        {loading ? <LoaderCircle className="spin" size={14} /> : <Sparkles size={14} />}
        {loading ? "正在整理" : generationDisabled ? "讨论进行中" : "整理会议草稿"}
      </button>
      {generationDisabled && generationDisabledReason
        ? <p className="artifact-generate-reason">{generationDisabledReason}</p>
        : null}
      {artifacts.length ? (
        <div className="artifact-list">
          {artifacts.slice(0, 5).map((artifact) => {
            const content = artifact.content || {};
            const itemCount = ["requirements", "risks", "conclusions", "disagreements", "unknowns", "actions"]
              .reduce((total, section) => total + (content[section]?.length || 0), 0);
            const projectCount = (content.requirements?.length || 0) + (content.risks?.length || 0);
            const decision = content.decision || {};
            const optionCount = decision.options?.length || 0;
            const preferredRecorded = Boolean(
              decision.preferred_option_id
              && decision.options?.some((option) => option.id === decision.preferred_option_id),
            );
            const audit = evidenceAuditSummary(content);
            const governanceBadge = artifactGovernanceBadge(artifact);
            const GovernanceIcon = governanceBadge?.tone === "ready" ? GitBranch : AlertTriangle;
            const userDecisionBadge = decisionBadge(artifact);
            const UserDecisionIcon = userDecisionBadge?.Icon;
            return (
              <button className="artifact-row" type="button" key={artifact.id} onClick={(event) => onEdit(artifact, event.currentTarget)}>
                <span className="artifact-row-badges">
                  <span className={`artifact-status ${artifact.status === "CONFIRMED" ? "confirmed" : "draft"}`}>
                    {artifact.status === "CONFIRMED" ? <CheckCircle2 size={12} /> : <FileEdit size={12} />}
                    {statusLabel(artifact)}
                  </span>
                  {governanceBadge ? (
                    <span className={`artifact-governance-badge ${governanceBadge.tone}`} title={governanceBadge.title}>
                      <GovernanceIcon size={11} />
                      {governanceBadge.label}
                    </span>
                  ) : null}
                  {userDecisionBadge ? (
                    <span className={`artifact-user-decision-badge ${userDecisionBadge.tone}`} title={userDecisionBadge.title}>
                      <UserDecisionIcon size={11} />
                      {userDecisionBadge.label}
                    </span>
                  ) : null}
                </span>
                <strong>{artifact.title}</strong>
                <small>
                  v{artifact.version} · 纪要 {itemCount} 项
                  {projectCount ? ` · 项目条目 ${projectCount}` : ""}
                  {optionCount ? ` · 候选 ${optionCount} · ${preferredRecorded ? "已选首选" : "未选首选"}` : ""}
                  {` · 证据关系 ${audit.total} · 未核验 ${audit.unreviewed}`}
                  {audit.counter ? ` · 反证 ${audit.counter}` : ""}
                </small>
              </button>
            );
          })}
        </div>
      ) : <div className="empty-resource">尚未生成会议产物</div>}
    </div>
  );
}
