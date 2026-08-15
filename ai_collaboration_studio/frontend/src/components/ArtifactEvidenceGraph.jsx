import { AlertTriangle, ArrowRight, GitBranch, History, ShieldCheck } from "lucide-react";
import { useMemo, useState } from "react";
import { filterEvidencePaths, summarizeEvidenceGraph } from "../artifactEvidenceGraph";

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

export function ArtifactEvidenceGraph({ state, graph, activeTarget, onSelectRelation }) {
  const [filter, setFilter] = useState("all");
  const [pathLimit, setPathLimit] = useState(100);
  const [historyLimit, setHistoryLimit] = useState(50);
  const fullSummary = useMemo(() => summarizeEvidenceGraph(graph), [graph]);
  const targetSummary = useMemo(
    () => summarizeEvidenceGraph(graph, activeTarget),
    [activeTarget, graph],
  );
  const paths = useMemo(
    () => filterEvidencePaths(graph, filter, activeTarget),
    [activeTarget, filter, graph],
  );
  const visiblePaths = paths.slice(0, pathLimit);
  const visibleHistory = [...(graph?.reviewEvents || [])].reverse().slice(0, historyLimit);
  const integrityStatus = graph?.integrity?.status || "";
  const legacyGapCount = Number(graph?.reviewChain?.legacy_untracked_version_count || 0);

  return (
    <section className="artifact-evidence-graph" aria-label="已保存版本的证据路径账本">
      <header className="artifact-evidence-graph-heading">
        <span>
          <GitBranch size={16} />
          <strong>证据路径账本</strong>
          <small>只展示服务端保存的显式关系，不从文字猜测支持、冲突或因果。</small>
        </span>
        {graph?.valid ? (
          <em className={integrityStatus === "verified" ? "verified" : "legacy"}>
            {integrityStatus === "verified" ? <ShieldCheck size={13} /> : <History size={13} />}
            {integrityStatus === "verified"
              ? "来源与哈希链已核验"
              : integrityStatus === "partial"
                ? "部分可验证"
                : "旧版历史未追踪"}
          </em>
        ) : null}
      </header>

      {state === "loading" ? <p className="artifact-evidence-graph-state">正在读取已保存版本的证据关系……</p> : null}
      {state === "error" ? (
        <p className="artifact-evidence-graph-state error" role="alert">
          <AlertTriangle size={14} />证据图暂时无法读取；当前编辑器仍可查看来源，但不能据此宣称审核链完整。
        </p>
      ) : null}
      {state === "ready" && graph && !graph.valid ? (
        <p className="artifact-evidence-graph-state error" role="alert">
          <AlertTriangle size={14} />
          {graph.stale ? "证据图对应的是其他产物版本，请保存或刷新后重试。" : "证据图完整性校验失败，已停止展示关系。"}
        </p>
      ) : null}

      {state === "ready" && graph?.valid ? <>
        {graph.integrity.status === "legacy_untracked" ? (
          <p className="artifact-evidence-graph-state warning">
            这个产物创建于审核事件链启用之前。当前关系可核对，但旧版本审核过程不会被伪造回填；下一次保存后才开始追加记录。
          </p>
        ) : null}
        {graph.integrity.status === "partial" ? (
          <p className="artifact-evidence-graph-state warning">
            关系映射仍可查看，但来源或历史只完成了部分核验；不能把本图当作完整审核证明。
            {legacyGapCount ? ` 前 ${legacyGapCount} 个产物版本没有事件记录。` : ""}
          </p>
        ) : null}

        <div className="artifact-evidence-graph-scopes" aria-label="证据关系统计范围">
          <span><small>全图</small><strong>{metricText(fullSummary)}</strong></span>
          <span><small>当前条目</small><strong>{metricText(targetSummary)}</strong></span>
        </div>

        <div className="artifact-evidence-graph-filters" aria-label="筛选证据路径">
          {[
            ["all", "全部"],
            ["attention", "需关注"],
            ["reviewed", "已核验"],
            ["active", "当前条目"],
          ].map(([value, label]) => (
            <button
              type="button"
              className={filter === value ? "active" : ""}
              key={`${value}:${label}`}
              onClick={() => {
                setFilter(value);
                setPathLimit(100);
              }}
            >
              {label}
            </button>
          ))}
        </div>

        <div className="artifact-evidence-paths">
          {visiblePaths.map((path) => (
            <button
              type="button"
              className={`artifact-evidence-path ${path.edge.item_key === activeTarget ? "current" : ""}`}
              key={path.relationId}
              onClick={() => onSelectRelation?.(path.edge.item_key, path.edge.source_ref)}
              title="跳到这条关系的核验项"
            >
              <span className="source">
                <small>{sourceTypeLabels[path.source.source_type] || "证据来源"}</small>
                <strong>{path.source.label || path.source.source_id}</strong>
                <em>{[
                  path.source.source_version ? `v${path.source.source_version}` : "",
                  sourceStatusLabels[path.source.status] || path.source.status,
                ].filter(Boolean).join(" · ")}</em>
              </span>
              <ArrowRight className="path-arrow" size={15} aria-hidden="true" />
              <span className={`relation ${path.edge.evidence_role} ${path.edge.verification_status}`}>
                <small>引用关系</small>
                <strong>{roleLabels[path.edge.evidence_role]} · {statusLabels[path.edge.verification_status]}</strong>
                {path.edge.review_note ? <em>{path.edge.review_note}</em> : null}
              </span>
              <ArrowRight className="path-arrow" size={15} aria-hidden="true" />
              <span className="target">
                <small>{path.target.node_kind}</small>
                <strong>{path.target.label || path.target.item_key}</strong>
                <em>{path.target.item_key}</em>
              </span>
              <ArrowRight className="path-arrow" size={15} aria-hidden="true" />
              <span className="downstream">
                <small>明确下游</small>
                <strong>{path.downstream.length
                  ? path.downstream.map((node) => node.label || node.action || node.node_kind).join(" · ")
                  : "—"}</strong>
              </span>
            </button>
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
          <summary><History size={14} />审核快照历史（{graph.reviewEvents.length}）</summary>
          {graph.reviewEvents.length ? (
            <ol>
              {visibleHistory.map((event) => (
                <li key={event.event_sha256}>
                  <span><strong>v{event.artifact_version} · {eventLabels[event.event_type] || event.event_type}</strong><small>序号 {event.sequence_no} · {event.created_by}</small></span>
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

        <p className="artifact-evidence-graph-boundary">
          证据映射只说明“哪条保存来源被如何引用”，不证明内容必然真实，也不授权交易、投注、支付或任何资金动作。
        </p>
      </> : null}
    </section>
  );
}
