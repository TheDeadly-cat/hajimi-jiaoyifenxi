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
import { memo, useId, useMemo, useRef, useState } from "react";
import { evidenceAuditSummary } from "../artifactEvidence";
import {
  ARTIFACT_PANEL_VISIBLE_LIMIT,
  artifactPanelControls,
  artifactPanelErrorMessage,
  artifactPanelRows,
} from "../artifactPanelView";
import { artifactDecisionState, userDecisionLabel } from "../artifactUserDecision";
import { artifactGovernanceBadge } from "../candidateGovernance";
import "../styles/artifact-panel.css";
import "../styles/artifact-panel-refinement.css";

const EMPTY_LIST = Object.freeze([]);

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
  if (String(artifact?.status || "").toUpperCase() === "CONFIRMED") {
    return {
      Icon: CirclePause,
      label: "待最终决定",
      tone: "pending",
      title: `当前已确认产物 v${artifact.version} 尚未记录最终决定`,
    };
  }
  return null;
}

export const ArtifactPanel = memo(function ArtifactPanel({
  artifacts = EMPTY_LIST,
  members = EMPTY_LIST,
  loading,
  generationDisabled = false,
  generationDisabledReason = "",
  onGenerate,
  onEdit,
}) {
  const titleId = useId();
  const [selectedSynthesizerId, setSelectedSynthesizerId] = useState("");
  const [artifactLimit, setArtifactLimit] = useState(5);
  const [localError, setLocalError] = useState("");
  const generationRef = useRef(false);
  const generationHandlerAvailable = typeof onGenerate === "function";
  const editHandlerAvailable = typeof onEdit === "function";
  const controls = useMemo(
    () => artifactPanelControls({
      members,
      selectedSynthesizerId,
      loading,
      generationDisabled,
      generationHandlerAvailable,
    }),
    [generationDisabled, generationHandlerAvailable, loading, members, selectedSynthesizerId],
  );
  const ledger = useMemo(
    () => artifactPanelRows(artifacts, {
      limit: artifactLimit,
      summarizeEvidence: evidenceAuditSummary,
    }),
    [artifactLimit, artifacts],
  );
  const safeGenerationReason = artifactPanelErrorMessage(
    { message: generationDisabledReason },
    "草稿生成当前不可用。",
  );
  const canRevealMore = ledger.visibleCount
    < Math.min(ledger.totalCount, ARTIFACT_PANEL_VISIBLE_LIMIT);

  const generateArtifact = async (event) => {
    if (controls.generateDisabled || generationRef.current || !generationHandlerAvailable) return;
    generationRef.current = true;
    const trigger = event.currentTarget;
    setLocalError("");
    try {
      await onGenerate(controls.activeSynthesizerId, trigger);
    } catch (requestError) {
      setLocalError(artifactPanelErrorMessage(requestError, "会议草稿整理失败。"));
    } finally {
      generationRef.current = false;
    }
  };

  const editArtifact = (artifact, trigger) => {
    if (!editHandlerAvailable) return;
    setLocalError("");
    try {
      onEdit(artifact, trigger);
    } catch (editError) {
      setLocalError(artifactPanelErrorMessage(editError, "会议产物无法打开。"));
    }
  };
  return (
    <section
      className="artifact-panel artifact-panel-workbench"
      data-generation-state={controls.state}
      aria-labelledby={titleId}
    >
      <header className="artifact-panel-heading">
        <div>
          <small>ARTIFACT LEDGER</small>
          <strong id={titleId}><GitBranch size={16} aria-hidden="true" />会议产物台账</strong>
          <p>先整理为可核验草稿，再沿版本、证据、治理和用户决定逐项复核。</p>
        </div>
        <em><span>VISIBLE / TOTAL</span>{ledger.visibleCount} / {ledger.totalCount}</em>
      </header>

      <div className="artifact-control-deck">
        {controls.synthesizers.length ? (
          <label className="artifact-synthesizer">
            <span><strong>整理成员</strong><small>只列出当前启用且具备 Provider 绑定的成员</small></span>
            <select
              value={controls.activeSynthesizerId}
              onChange={(event) => setSelectedSynthesizerId(event.target.value)}
              disabled={controls.generateDisabled}
              aria-label="选择会议草稿整理成员"
            >
              <option value="">自动选择方案整合角色</option>
              {controls.synthesizers.map((member) => (
                <option key={member.id} value={member.id}>
                  {member.name} · {member.provider}/{member.model || "默认模型"}
                </option>
              ))}
            </select>
          </label>
        ) : (
          <p className="artifact-auto-route">未指定可选整理成员；生成请求将沿现有安全路由处理。</p>
        )}
        <button
          className="artifact-generate"
          type="button"
          onClick={generateArtifact}
          disabled={controls.generateDisabled}
          aria-busy={loading}
          title={controls.generateDisabled
            ? controls.issue || (generationDisabled ? safeGenerationReason : "草稿生成当前不可用。")
            : "把最近一轮已结束的讨论整理成可核验草稿"}
        >
          {loading
            ? <LoaderCircle className="spin" size={14} aria-hidden="true" />
            : <Sparkles size={14} aria-hidden="true" />}
          {controls.actionLabel}
        </button>
      </div>

      <div className="artifact-panel-control-ledger" role="list" aria-label="产物台账控制状态">
        <span role="listitem"><small>可选整理成员</small><strong>{controls.synthesizers.length}</strong></span>
        <span role="listitem"><small>当前可见产物</small><strong>{ledger.visibleCount}</strong></span>
        <span role="listitem"><small>操作模式</small><strong>{controls.state === "ready" ? "可整理" : controls.state === "loading" ? "处理中" : "受限"}</strong></span>
      </div>

      {controls.issue ? <p className="artifact-generate-reason" role="alert"><AlertTriangle size={13} aria-hidden="true" />{controls.issue}</p> : null}

      {generationDisabled && generationDisabledReason ? (
        <p className="artifact-generate-reason" role="note">
          <AlertTriangle size={13} aria-hidden="true" />{safeGenerationReason}
        </p>
      ) : null}
      {localError ? <p className="artifact-generate-reason" role="alert"><AlertTriangle size={13} aria-hidden="true" />{localError}</p> : null}

      {ledger.visibleRows.length ? (
        <div className="artifact-list" role="list" aria-label="会议产物">
          {ledger.visibleRows.map((row) => {
            const artifact = row.artifact;
            const audit = row.audit;
            const governanceBadge = artifactGovernanceBadge(artifact);
            const GovernanceIcon = governanceBadge?.tone === "ready" ? GitBranch : AlertTriangle;
            const userDecisionBadge = decisionBadge(artifact);
            const UserDecisionIcon = userDecisionBadge?.Icon;
            return (
              <div className="artifact-list-item" role="listitem" key={row.key}>
                <button
                  className="artifact-row"
                  type="button"
                  data-artifact-status={row.status}
                  aria-label={row.title + "，" + row.statusLabel + "，" + row.versionLabel}
                  disabled={!editHandlerAvailable}
                  title={editHandlerAvailable ? "打开精确版本复核" : "产物编辑处理器不可用"}
                  onClick={(event) => editArtifact(artifact, event.currentTarget)}
                >
                  <span className="artifact-row-badges">
                    <span className={"artifact-status " + row.status}>
                      {row.status === "confirmed"
                        ? <CheckCircle2 size={12} aria-hidden="true" />
                        : <FileEdit size={12} aria-hidden="true" />}
                      {row.statusLabel}
                    </span>
                    {governanceBadge ? (
                      <span className={"artifact-governance-badge " + governanceBadge.tone} title={governanceBadge.title}>
                        <GovernanceIcon size={11} aria-hidden="true" />
                        {governanceBadge.label}
                      </span>
                    ) : null}
                    {userDecisionBadge ? (
                      <span className={"artifact-user-decision-badge " + userDecisionBadge.tone} title={userDecisionBadge.title}>
                        <UserDecisionIcon size={11} aria-hidden="true" />
                        {userDecisionBadge.label}
                      </span>
                    ) : null}
                  </span>
                  <span className="artifact-row-title">
                    <strong>{row.title}</strong>
                    <small>{row.versionLabel} · 精确版本复核入口</small>
                  </span>
                  <span className="artifact-row-metrics" aria-label="产物摘要指标">
                    {row.metrics.map((metric) => (
                      <span className={metric.key} key={metric.key}>
                        <small>{metric.label}</small>
                        <strong>{metric.value}</strong>
                      </span>
                    ))}
                  </span>
                  <span className="artifact-row-foot">
                    <span>
                      {row.projectionLimited
                        ? "产物结构超过台账安全摘要上限"
                        : row.optionCount
                        ? row.preferredRecorded ? "条件化首选已记录" : "条件化首选未记录"
                        : "未记录候选集"}
                      {!row.projectionLimited && audit.counter ? " · 反证 " + audit.counter : ""}
                      {!row.projectionLimited && audit.conflict ? " · 冲突 " + audit.conflict : ""}
                      {!row.projectionLimited && audit.gap ? " · 缺口 " + audit.gap : ""}
                    </span>
                    <em>打开复核</em>
                  </span>
                </button>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="empty-resource artifact-empty">
          <FileEdit size={18} aria-hidden="true" />
          <span><strong>尚未生成会议产物</strong><small>结束一轮讨论后，可整理为可核验草稿。</small></span>
        </div>
      )}
      {canRevealMore ? (
        <button
          type="button"
          className="secondary artifact-panel-more"
          onClick={() => setArtifactLimit((current) => Math.min(
            ARTIFACT_PANEL_VISIBLE_LIMIT,
            current + 5,
          ))}
        >
          再显示 {Math.min(5, ledger.hiddenCount)} 项产物
        </button>
      ) : ledger.hiddenCount ? (
        <p className="artifact-list-limit">当前显示前 {ledger.visibleCount} 项，另有 {ledger.hiddenCount} 项可在完整产物视图中复核。</p>
      ) : null}
    </section>
  );
});
