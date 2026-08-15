import { AlertTriangle, CheckCircle2, GitCompareArrows, History, LoaderCircle } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { buildArtifactVersionDiff, formatArtifactVersionTime } from "../artifactVersionDiff";

function VersionCard({ record, label }) {
  const review = record?.frozen_evidence_review || {};
  return (
    <article className="artifact-version-card">
      <span><strong>{label} · v{record?.version || "?"}</strong><em className={record?.integrity_ok ? "ok" : "warning"}>{record?.integrity_ok ? <CheckCircle2 size={12} /> : <AlertTriangle size={12} />}{record?.integrity_ok ? "快照完整" : "快照异常"}</em></span>
      <small>{record?.status || "DRAFT"} · {formatArtifactVersionTime(record?.changed_at)}</small>
      <small>证据关系 {review.relation_count || 0} · 已核验 {review.reviewed_relation_count || 0} · 用户决定 {record?.user_decisions?.length || record?.decision_count || 0}</small>
    </article>
  );
}

function ChangeValues({ before, after }) {
  return <span className="artifact-version-change-values"><del>{before || "（空）"}</del><ins>{after || "（空）"}</ins></span>;
}

function DiffView({ left, right }) {
  const diff = useMemo(() => buildArtifactVersionDiff(left, right), [left, right]);
  const changedSections = diff.sections.filter((section) => (
    section.added.length || section.removed.length || section.changed.length || section.reordered
  ));
  return (
    <div className="artifact-version-diff" aria-label={`产物 v${left.version} 与 v${right.version} 对比`}>
      <div className="artifact-version-cards"><VersionCard record={left} label="基准" /><VersionCard record={right} label="对比" /></div>
      {!diff.changed ? <p className="artifact-version-empty">两个版本的结构化内容完全一致。</p> : null}
      {diff.scalarChanges.length ? <section><strong>主要字段变化</strong>{diff.scalarChanges.map((change) => <article key={change.key}><span>{change.label}</span><ChangeValues before={change.before} after={change.after} /></article>)}</section> : null}
      {changedSections.length ? <section><strong>结构化条目变化</strong>{changedSections.map((section) => <article key={section.key} className="artifact-section-diff"><span><b>{section.label}</b><small>新增 {section.added.length} · 删除 {section.removed.length} · 修改 {section.changed.length}{section.reordered ? " · 顺序变化" : ""}</small></span>{section.added.map((item) => <em className="added" key={`add:${item.id}`}>+ {item.label}</em>)}{section.removed.map((item) => <em className="removed" key={`remove:${item.id}`}>− {item.label}</em>)}{section.changed.map((item) => <details key={`change:${item.id}`}><summary>修改 · {item.label}</summary>{item.fieldChanges.map((field) => <div key={field.field}><code>{field.field}</code><ChangeValues before={field.before} after={field.after} /></div>)}{item.evidenceChanged ? <small>该条目的证据关系发生变化。</small> : null}</details>)}</article>)}</section> : null}
      {(diff.evidence.added.length || diff.evidence.removed.length || diff.evidence.changed.length) ? <section><strong>证据关系变化</strong><p>新增 {diff.evidence.added.length} · 删除 {diff.evidence.removed.length} · 审核或版本字段变化 {diff.evidence.changed.length}</p></section> : null}
      <details className="artifact-version-snapshot-preview"><summary>查看对比版本摘要</summary><p>{right.snapshot?.content?.summary || "该版本没有摘要。"}</p></details>
    </div>
  );
}

export function ArtifactVersionHistory({ roomId, artifact }) {
  const [expanded, setExpanded] = useState(false);
  const [versions, setVersions] = useState([]);
  const [baseVersion, setBaseVersion] = useState(0);
  const [targetVersion, setTargetVersion] = useState(0);
  const [pair, setPair] = useState(null);
  const [status, setStatus] = useState("idle");
  const [error, setError] = useState("");
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    setVersions([]);
    setBaseVersion(0);
    setTargetVersion(0);
    setPair(null);
    setStatus("idle");
    setError("");
  }, [artifact.id, artifact.version]);

  useEffect(() => {
    if (!expanded) return undefined;
    let cancelled = false;
    setStatus("loading");
    setError("");
    api.artifactVersions(roomId, artifact.id)
      .then((data) => {
        if (cancelled) return;
        const rows = Array.isArray(data.versions) ? data.versions : [];
        setVersions(rows);
        const target = rows.find((row) => row.version === artifact.version)?.version || rows[0]?.version || 0;
        const base = rows.find((row) => row.version < target)?.version || target;
        setTargetVersion(target);
        setBaseVersion(base);
        setStatus("ready");
      })
      .catch((requestError) => {
        if (cancelled) return;
        setError(requestError.message);
        setStatus("error");
      });
    return () => { cancelled = true; };
  }, [artifact.id, artifact.version, expanded, reloadToken, roomId]);

  useEffect(() => {
    if (!expanded || !baseVersion || !targetVersion) return undefined;
    let cancelled = false;
    setPair(null);
    setError("");
    setStatus("loading-pair");
    const baseRequest = api.artifactVersion(roomId, artifact.id, baseVersion);
    const targetRequest = baseVersion === targetVersion
      ? baseRequest
      : api.artifactVersion(roomId, artifact.id, targetVersion);
    Promise.all([baseRequest, targetRequest])
      .then(([baseData, targetData]) => {
        if (cancelled) return;
        setPair({ left: baseData.artifact_version, right: targetData.artifact_version });
        setStatus("ready");
      })
      .catch((requestError) => {
        if (cancelled) return;
        setError(requestError.message);
        setStatus("error");
      });
    return () => { cancelled = true; };
  }, [artifact.id, baseVersion, expanded, reloadToken, roomId, targetVersion]);

  return (
    <details className="artifact-version-history" onToggle={(event) => setExpanded(event.currentTarget.open)}>
      <summary><span><History size={15} />版本历史与对比</span><small>当前 v{artifact.version}</small></summary>
      <div className="artifact-version-history-body">
        {status === "loading" ? <p><LoaderCircle className="spin" size={14} />正在读取冻结历史…</p> : null}
        {error ? <p className="artifact-version-error" role="alert"><AlertTriangle size={14} /><span>{error}</span><button type="button" className="secondary" onClick={() => setReloadToken((value) => value + 1)}>重试</button></p> : null}
        {versions.length ? <div className="artifact-version-selectors">
          <label>基准版本<select value={baseVersion || ""} onChange={(event) => setBaseVersion(Number(event.target.value))}>{versions.map((row) => <option key={`base:${row.version}`} value={row.version}>v{row.version} · {row.status}</option>)}</select></label>
          <GitCompareArrows size={17} />
          <label>对比版本<select value={targetVersion || ""} onChange={(event) => setTargetVersion(Number(event.target.value))}>{versions.map((row) => <option key={`target:${row.version}`} value={row.version}>v{row.version} · {row.status}</option>)}</select></label>
        </div> : null}
        {versions.length === 1 ? <p className="artifact-version-empty">当前只有一个版本；再次保存或确认后即可进行跨版本比较。</p> : null}
        {status === "loading-pair" ? <p><LoaderCircle className="spin" size={14} />正在对齐两个精确版本…</p> : null}
        {pair ? <DiffView left={pair.left} right={pair.right} /> : null}
      </div>
    </details>
  );
}
