import { AlertTriangle, ArrowRight, GitBranch, History, ShieldCheck } from "lucide-react";
import { memo, useEffect, useId, useMemo, useState } from "react";
import {
  filterEvidencePaths,
  summarizeActiveEvidenceGraph,
  summarizeEvidenceGraph,
} from "../artifactEvidenceGraph";
import "../styles/artifact-evidence-graph-refinement.css";

const roleLabels = {
  support: "支持",
  counter: "反证",
  context: "背景",
};

const statusLabels = {
  unreviewed: "未核验",
  source_checked: "已核对原文",
  corroborated: "已交叉印证",
  disputed: "存在争议",
};

const eventLabels = {
  created: "创建",
  revised: "修订",
  confirmed: "用户确认",
};

const sourceTypeLabels = {
  material: "资料版本",
  message: "成员发言",
  round_market_snapshot: "冻结市场快照",
};

const sourceStatusLabels = {
  available: "可读取",
  visible: "可读取",
  missing: "来源缺失",
  unsealed: "旧版未封存",
};

const targetTypeLabels = {
  summary: "摘要",
  requirement: "需求",
  risk: "风险",
  conclusion: "结论",
  disagreement: "分歧",
  unknown: "未知项",
  action: "行动",
  decision: "决策",
  candidate: "候选方案",
};

const filterOptions = Object.freeze([
  ["all", "全部"],
  ["attention", "需关注"],
  ["reviewed", "已核验"],
  ["active", "当前条目"],
]);

function metricText(summary) {
  return `${summary.relationCount} 条关系 · ${summary.sourceIds.size} 个精确来源 · ${summary.unreviewed} 条未核验`;
}

function eventDelta(event) {
  const parts = [];
  if (event.added_relation_count) parts.push(`+${event.added_relation_count}`);
  if (event.removed_relation_count) parts.push(`−${event.removed_relation_count}`);
  if (event.changed_relation_count) parts.push(`变更 ${event.changed_relation_count}`);
  return parts.length ? parts.join(" · ") : "关系未变化";
}


const EvidencePathCard = memo(function EvidencePathCard({
  path,
  activeTarget,
  canSelectRelation,
  onSelectRelation,
}) {
  const roleLabel = roleLabels[path.edge.evidence_role]
    || path.edge.evidence_role
    || "用途未记录";
  const verificationLabel = statusLabels[path.edge.verification_status]
    || path.edge.verification_status
    || "状态未记录";
  const sourceLabel = path.source.label || path.source.source_id || "未命名来源";
  const targetLabel = path.target.label || path.target.item_key || "未命名条目";
  const downstreamText = path.downstream.length
    ? path.downstream
      .map((node) => node.label || node.action || node.node_kind)
      .filter(Boolean)
      .join(" · ") || "下游标签未记录"
    : "—";
  return (
    <button
      type="button"
      className={`artifact-evidence-path ${path.edge.item_key === activeTarget ? "current" : ""}`}
      disabled={!canSelectRelation}
      onClick={() => canSelectRelation && onSelectRelation(path.edge.item_key, path.edge.source_ref)}
      title={canSelectRelation ? "跳到这条关系的核验项" : "当前无法打开关系核验项"}
      aria-label={`${sourceLabel}到${targetLabel}：${roleLabel}，${verificationLabel}`}
    >
      <span className="source">
        <small>{sourceTypeLabels[path.source.source_type] || "证据来源"}</small>
        <strong>{sourceLabel}</strong>
        <em>{[
          path.source.source_version ? `v${path.source.source_version}` : "",
          sourceStatusLabels[path.source.status] || path.source.status || "状态未记录",
        ].filter(Boolean).join(" · ")}</em>
      </span>
      <ArrowRight className="path-arrow" size={15} aria-hidden="true" />
      <span className={`relation ${path.edge.evidence_role} ${path.edge.verification_status}`}>
        <small>引用关系</small>
        <strong>{roleLabel} · {verificationLabel}</strong>
        {path.edge.review_note ? <em>{path.edge.review_note}</em> : null}
      </span>
      <ArrowRight className="path-arrow" size={15} aria-hidden="true" />
      <span className="target">
        <small>{targetTypeLabels[path.target.node_kind] || path.target.node_kind || "条目类型未记录"}</small>
        <strong>{targetLabel}</strong>
        <em>{path.target.item_key || "条目标识缺失"}</em>
      </span>
      <ArrowRight className="path-arrow" size={15} aria-hidden="true" />
      <span className="downstream">
        <small>明确下游</small>
        <strong>{downstreamText}</strong>
      </span>
    </button>
  );
});


export const ArtifactEvidenceGraph = memo(function ArtifactEvidenceGraph({ state, graph, activeTarget, onSelectRelation }) {
  const titleId = useId();
  const [filter, setFilter] = useState("all");
  const [pathLimit, setPathLimit] = useState(100);
  const [historyLimit, setHistoryLimit] = useState(50);
  const fullSummary = useMemo(() => summarizeEvidenceGraph(graph), [graph]);
  const targetSummary = useMemo(
    () => summarizeActiveEvidenceGraph(graph, activeTarget),
    [activeTarget, graph],
  );
  const paths = useMemo(
    () => filterEvidencePaths(graph, filter, activeTarget),
    [activeTarget, filter, graph],
  );
  const visiblePaths = useMemo(() => paths.slice(0, pathLimit), [pathLimit, paths]);
  const visibleHistory = useMemo(
    () => [...(graph?.reviewEvents || [])].reverse().slice(0, historyLimit),
    [graph?.reviewEvents, historyLimit],
  );
  const integrityStatus = graph?.integrity?.status || "";
  const legacyGapCount = Number(graph?.reviewChain?.legacy_untracked_version_count || 0);
  const graphIdentity = graph?.valid
    ? JSON.stringify([graph.roomId, graph.artifact?.id, graph.artifact?.version])
    : "";
  const canSelectRelation = typeof onSelectRelation === "function";
  const selectedFilterLabel = filterOptions.find(([value]) => value === filter)?.[1] || "全部";

  useEffect(() => {
    setFilter("all");
    setPathLimit(100);
    setHistoryLimit(50);
  }, [graphIdentity]);

  useEffect(() => {
    if (!activeTarget) {
      setFilter((current) => current === "active" ? "all" : current);
    }
  }, [activeTarget]);

  return (
    <section className="artifact-evidence-graph evidence-ledger" aria-labelledby={titleId} aria-busy={state === "loading"}>
      <header className="artifact-evidence-graph-heading">
        <span>
          <GitBranch size={16} aria-hidden="true" />
          <strong id={titleId}>证据路径账本</strong>
          <small>只展示服务端保存的显式关系，不从文字猜测支持、冲突或因果。</small>
        </span>
        {graph?.valid ? (
          <em className={integrityStatus === "verified" ? "verified" : "legacy"}>
            {integrityStatus === "verified" ? <ShieldCheck size={13} aria-hidden="true" /> : <History size={13} aria-hidden="true" />}
            {integrityStatus === "verified"
              ? "来源与哈希链已核验"
              : integrityStatus === "partial"
                ? "部分可验证"
                : "旧版历史未追踪"}
          </em>
        ) : null}
      </header>

      {state === "loading" ? <p className="artifact-evidence-graph-state" role="status">正在读取已保存版本的证据关系……</p> : null}
      {state === "error" ? (
        <p className="artifact-evidence-graph-state error" role="alert">
          <AlertTriangle size={14} aria-hidden="true" />证据图暂时无法读取；当前编辑器仍可查看来源，但不能据此宣称审核链完整。
        </p>
      ) : null}
      {state === "ready" && graph && !graph.valid ? (
        <p className="artifact-evidence-graph-state error" role="alert">
          <AlertTriangle size={14} aria-hidden="true" />
          {graph.stale ? "证据图对应的是其他产物版本，请保存或刷新后重试。" : "证据图完整性校验失败，已停止展示关系。"}
        </p>
      ) : null}

      {state === "ready" && graph?.valid ? <>
        {graph.integrity.status === "legacy_untracked" ? (
          <p className="artifact-evidence-graph-state warning" role="note">
            这个产物创建于审核事件链启用之前。当前关系可核对，但旧版本审核过程不会被伪造回填；下一次保存后才开始追加记录。
          </p>
        ) : null}
        {graph.integrity.status === "partial" ? (
          <p className="artifact-evidence-graph-state warning" role="note">
            关系映射仍可查看，但来源或历史只完成了部分核验；不能把本图当作完整审核证明。
            {legacyGapCount ? ` 前 ${legacyGapCount} 个产物版本没有事件记录。` : ""}
          </p>
        ) : null}

        <div className="artifact-evidence-graph-scopes" role="list" aria-label="证据关系统计范围">
          <span role="listitem"><small>全图</small><strong>{metricText(fullSummary)}</strong></span>
          <span role="listitem"><small>当前条目</small><strong>{activeTarget ? metricText(targetSummary) : "尚未选择核验条目"}</strong></span>
          <span role="listitem"><small>授权输出</small><strong>不产生</strong></span>
        </div>

        <div className="artifact-evidence-graph-filters" role="group" aria-label="筛选证据路径">
          {filterOptions.map(([value, label]) => (
            <button
              type="button"
              className={filter === value ? "active" : ""}
              key={`${value}:${label}`}
              aria-pressed={filter === value}
              disabled={value === "active" && !activeTarget}
              onClick={() => {
                setFilter(value);
                setPathLimit(100);
              }}
            >
              {label}
            </button>
          ))}
        </div>
        <p className="artifact-evidence-filter-status" role="status" aria-live="polite" aria-atomic="true">
          <span>{selectedFilterLabel}</span>
          <strong>显示 {visiblePaths.length} / {paths.length} 条路径</strong>
          <small>{activeTarget ? `当前核验条目：${activeTarget}` : "选择编辑条目后可聚焦其证据路径"}</small>
        </p>

        <div className="artifact-evidence-paths">
          {visiblePaths.map((path) => (
            <EvidencePathCard
              activeTarget={activeTarget}
              canSelectRelation={canSelectRelation}
              key={path.relationId}
              onSelectRelation={onSelectRelation}
              path={path}
            />
          ))}
          {!paths.length ? <p className="artifact-evidence-graph-empty">这个筛选范围内还没有已保存的显式证据关系。</p> : null}
          {visiblePaths.length < paths.length ? (
            <button
              type="button"
              className="artifact-evidence-graph-more"
              onClick={() => setPathLimit((current) => current + 100)}
            >
              再显示 {Math.min(100, paths.length - visiblePaths.length)} 条
            </button>
          ) : null}
        </div>

        <details className="artifact-evidence-review-history">
          <summary><History size={14} aria-hidden="true" />审核快照历史（{graph.reviewEvents.length}）</summary>
          {graph.reviewEvents.length ? (
            <ol>
              {visibleHistory.map((event) => (
                <li key={event.event_sha256}>
                  <span><strong>v{event.artifact_version} · {eventLabels[event.event_type] || event.event_type || "其他事件"}</strong><small>序号 {event.sequence_no} · {event.created_by || "未记录操作者"}</small></span>
                  <span><strong>{event.relation_count} 条关系 · {event.unreviewed_count} 条未核验</strong><small>{eventDelta(event)}</small></span>
                  <code title={event.event_sha256}>{event.event_sha256.slice(0, 12)}…</code>
                </li>
              ))}
            </ol>
          ) : (
            <p>暂无可验证的历史事件；旧快照不会被冒充为用户审核。</p>
          )}
          {visibleHistory.length < graph.reviewEvents.length ? (
            <button
              type="button"
              className="artifact-evidence-graph-more"
              onClick={() => setHistoryLimit((current) => current + 50)}
            >
              加载更早的审核快照
            </button>
          ) : null}
        </details>

        <p className="artifact-evidence-graph-boundary" role="note">
          证据映射只说明“哪条保存来源被如何引用”，不证明内容必然真实，也不授权交易、投注、支付或任何资金动作。
        </p>
      </> : null}
    </section>
  );
});
