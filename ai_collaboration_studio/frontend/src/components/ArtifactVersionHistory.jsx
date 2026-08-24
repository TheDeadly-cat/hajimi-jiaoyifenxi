import { AlertTriangle, CheckCircle2, GitCompareArrows, History, LoaderCircle, ShieldCheck } from "lucide-react";
import { useEffect, useId, useMemo, useState } from "react";
import { api } from "../api";
import { buildArtifactVersionDiff, formatArtifactVersionTime } from "../artifactVersionDiff";
import "../styles/artifact-version-history-refinement.css";

const ERROR_MESSAGE_LIMIT = 1000;
const DIFF_VALUE_LIMIT = 2000;
const SNAPSHOT_SUMMARY_LIMIT = 4000;
const SECTION_DIFF_PREVIEW_LIMIT = 4;

function artifactVersionListKey(...parts) {
  return JSON.stringify(parts.map((part) => String(part ?? "")));
}

function versionNumber(record) {
  const value = Number(record?.version || record?.snapshot?.version || 0);
  return Number.isInteger(value) && value > 0 ? value : 0;
}

function integrityState(record) {
  if (record?.integrity_ok === true) return { label: "快照完整", tone: "ok", Icon: CheckCircle2 };
  if (record?.integrity_ok === false) return { label: "快照异常", tone: "warning", Icon: AlertTriangle };
  return { label: "完整性未记录", tone: "unknown", Icon: ShieldCheck };
}

function nonnegativeIntegerLabel(value) {
  const numeric = Number(value);
  return Number.isInteger(numeric) && numeric >= 0 ? String(numeric) : "未记录";
}

function decisionCountLabel(record) {
  if (Array.isArray(record?.user_decisions)) return String(record.user_decisions.length);
  return nonnegativeIntegerLabel(record?.decision_count);
}

function selectableVersionRows(rows) {
  const eligibleRows = (Array.isArray(rows) ? rows : []).filter(
    (row) => row?.integrity_ok === true && versionNumber(row) > 0,
  );
  const versionCounts = eligibleRows.reduce((counts, row) => {
    const version = versionNumber(row);
    counts.set(version, (counts.get(version) || 0) + 1);
    return counts;
  }, new Map());
  return eligibleRows
    .filter((row) => versionCounts.get(versionNumber(row)) === 1)
    .sort((left, right) => versionNumber(right) - versionNumber(left));
}

function boundedText(value, limit) {
  const text = String(value ?? "");
  return text.length <= limit ? text : `${text.slice(0, limit)}...`;
}

function requestErrorMessage(error, fallback) {
  const message = typeof error?.message === "string" ? error.message.trim() : "";
  return boundedText(message || fallback, ERROR_MESSAGE_LIMIT);
}

function displayDiffValue(value) {
  if (value === null || value === undefined || value === "") return "（空）";
  if (value === true) return "是";
  if (value === false) return "否";
  return boundedText(value, DIFF_VALUE_LIMIT);
}

function VersionCard({ record, label }) {
  const review = record?.frozen_evidence_review && typeof record.frozen_evidence_review === "object"
    ? record.frozen_evidence_review
    : {};
  const integrity = integrityState(record);
  const IntegrityIcon = integrity.Icon;
  const statusLabel = typeof record?.status === "string" && record.status.trim()
    ? record.status.trim()
    : "状态未记录";
  return (
    <article className="artifact-version-card">
      <span><strong>{label} · v{versionNumber(record) || "?"}</strong><em className={integrity.tone}><IntegrityIcon size={12} aria-hidden="true" />{integrity.label}</em></span>
      <small>{statusLabel} · {formatArtifactVersionTime(record?.changed_at)}</small>
      <small>证据关系 {nonnegativeIntegerLabel(review.relation_count)} · 已核验 {nonnegativeIntegerLabel(review.reviewed_relation_count)} · 用户决定 {decisionCountLabel(record)}</small>
    </article>
  );
}

function ChangeValues({ before, after }) {
  return (
    <span className="artifact-version-change-values">
      <span className="artifact-version-change-value before"><small>变更前</small><del>{displayDiffValue(before)}</del></span>
      <span className="artifact-version-change-value after"><small>变更后</small><ins>{displayDiffValue(after)}</ins></span>
    </span>
  );
}

function DiffSection({ leftVersion, rightVersion, section }) {
  const [expanded, setExpanded] = useState(false);
  const rowsId = useId();
  const rows = [
    ...section.added.map((item) => ({ kind: "added", item })),
    ...section.removed.map((item) => ({ kind: "removed", item })),
    ...section.changed.map((item) => ({ kind: "changed", item })),
  ];
  const visibleRows = expanded ? rows : rows.slice(0, SECTION_DIFF_PREVIEW_LIMIT);
  const hiddenRowCount = rows.length - visibleRows.length;
  return (
    <article className="artifact-section-diff">
      <span className="artifact-section-diff-heading">
        <b>{section.label}</b>
        <small>新增 {section.added.length} · 删除 {section.removed.length} · 修改 {section.changed.length}{section.reordered ? " · 顺序变化" : ""}</small>
      </span>
      <div className="artifact-section-diff-rows" id={rowsId} role="list">
        {visibleRows.map((row) => {
          const rowKey = artifactVersionListKey(
            `${row.kind}-item`,
            section.key,
            row.item.id,
            leftVersion,
            rightVersion,
          );
          if (row.kind === "added") {
            return <em className="added" key={rowKey} role="listitem">+ {row.item.label}</em>;
          }
          if (row.kind === "removed") {
            return <em className="removed" key={rowKey} role="listitem">− {row.item.label}</em>;
          }
          return (
            <details key={rowKey} role="listitem">
              <summary>修改 · {row.item.label}</summary>
              {row.item.fieldChanges.map((field) => (
                <div key={artifactVersionListKey("field-change", section.key, row.item.id, field.field)}>
                  <code>{field.field}</code>
                  <ChangeValues before={field.before} after={field.after} />
                </div>
              ))}
              {row.item.evidenceChanged ? <small>该条目的证据关系发生变化。</small> : null}
            </details>
          );
        })}
      </div>
      {rows.length > SECTION_DIFF_PREVIEW_LIMIT ? (
        <button
          aria-controls={rowsId}
          aria-expanded={expanded}
          className="artifact-section-diff-control secondary"
          onClick={() => setExpanded((current) => !current)}
          type="button"
        >
          {expanded ? `收起至前 ${SECTION_DIFF_PREVIEW_LIMIT} 条` : `再显示 ${hiddenRowCount} 条变化`}
        </button>
      ) : null}
    </article>
  );
}

function DiffView({ left, right }) {
  const diff = useMemo(() => buildArtifactVersionDiff(left, right), [left, right]);
  const leftVersion = versionNumber(left);
  const rightVersion = versionNumber(right);
  const changedSections = diff.sections.filter((section) => (
    section.added.length || section.removed.length || section.changed.length || section.reordered
  ));
  const structuralChangeCount = changedSections.reduce((total, section) => (
    total + section.added.length + section.removed.length + section.changed.length + (section.reordered ? 1 : 0)
  ), 0);
  const evidenceChangeCount = diff.evidence.added.length + diff.evidence.removed.length + diff.evidence.changed.length;
  const rawSnapshotSummary = typeof right.snapshot?.content?.summary === "string"
    ? right.snapshot.content.summary.trim()
    : "";
  const snapshotSummary = rawSnapshotSummary
    ? boundedText(rawSnapshotSummary, SNAPSHOT_SUMMARY_LIMIT)
    : "该版本没有摘要。";
  return (
    <div className="artifact-version-diff" aria-label={`产物 v${leftVersion || "?"} 与 v${rightVersion || "?"} 对比`}>
      <div className="artifact-version-cards"><VersionCard record={left} label="基准" /><VersionCard record={right} label="对比" /></div>
      <div className={`artifact-version-diff-summary ${diff.changed ? "changed" : "unchanged"}`} role="status">
        <GitCompareArrows size={17} aria-hidden="true" />
        <span><strong>{diff.changed ? "检测到版本结构变化" : "两个版本结构一致"}</strong><small>字段 {diff.scalarChanges.length} · 条目 {structuralChangeCount} · 证据关系 {evidenceChangeCount}</small></span>
      </div>
      {!diff.changed ? <p className="artifact-version-empty">结构化字段、条目与证据关系均未检测到变化。</p> : null}
      {diff.scalarChanges.length ? <section aria-label="主要字段变化"><strong>主要字段变化</strong>{diff.scalarChanges.map((change) => <article key={artifactVersionListKey("scalar-change", change.key)}><span>{change.label}</span><ChangeValues before={change.before} after={change.after} /></article>)}</section> : null}
      {changedSections.length ? <section aria-label="结构化条目变化"><strong>结构化条目变化</strong>{changedSections.map((section) => (
        <DiffSection
          key={artifactVersionListKey("section-change", section.key, leftVersion, rightVersion)}
          leftVersion={leftVersion}
          rightVersion={rightVersion}
          section={section}
        />
      ))}</section> : null}
      {evidenceChangeCount ? <section aria-label="证据关系变化"><strong>证据关系变化</strong><p>新增 {diff.evidence.added.length} · 删除 {diff.evidence.removed.length} · 审核或版本字段变化 {diff.evidence.changed.length}</p></section> : null}
      <details className="artifact-version-snapshot-preview"><summary>查看对比版本摘要</summary><p>{snapshotSummary}</p></details>
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
  const [listReloadToken, setListReloadToken] = useState(0);
  const [pairReloadToken, setPairReloadToken] = useState(0);

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
    setVersions([]);
    setBaseVersion(0);
    setTargetVersion(0);
    setPair(null);
    setStatus("loading-list");
    setError("");
    api.artifactVersions(roomId, artifact.id)
      .then((data) => {
        if (cancelled) return;
        const rows = Array.isArray(data?.versions) ? data.versions : [];
        const selectableRows = selectableVersionRows(rows);
        setVersions(rows);
        const artifactVersion = versionNumber(artifact);
        const target = versionNumber(
          selectableRows.find((row) => versionNumber(row) === artifactVersion) || selectableRows[0],
        );
        const base = versionNumber(
          selectableRows.find((row) => versionNumber(row) < target)
          || selectableRows.find((row) => versionNumber(row) !== target)
          || selectableRows.find((row) => versionNumber(row) === target),
        );
        setTargetVersion(target);
        setBaseVersion(base);
        setStatus("ready");
      })
      .catch((requestError) => {
        if (cancelled) return;
        setError(requestErrorMessage(requestError, "产物版本历史读取失败。"));
        setStatus("error-list");
      });
    return () => { cancelled = true; };
  }, [artifact.id, artifact.version, expanded, listReloadToken, roomId]);

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
        const left = baseData?.artifact_version;
        const right = targetData?.artifact_version;
        if (!left || !right) throw new Error("产物版本快照响应不完整。");
        if (left.integrity_ok !== true || right.integrity_ok !== true) {
          throw new Error("产物版本快照完整性未通过，已停止比较。");
        }
        if (versionNumber(left) !== baseVersion || versionNumber(right) !== targetVersion) {
          throw new Error("产物版本快照与所选版本不一致。");
        }
        setPair({ left, right });
        setStatus("ready");
      })
      .catch((requestError) => {
        if (cancelled) return;
        setError(requestErrorMessage(requestError, "产物版本对比读取失败。"));
        setStatus("error-pair");
      });
    return () => { cancelled = true; };
  }, [artifact.id, baseVersion, expanded, pairReloadToken, roomId, targetVersion]);

  const selectableVersions = useMemo(() => selectableVersionRows(versions), [versions]);
  const omittedVersionCount = versions.length - selectableVersions.length;
  const retry = () => {
    if (status === "error-pair") {
      setPairReloadToken((value) => value + 1);
      return;
    }
    setListReloadToken((value) => value + 1);
  };

  return (
    <details className="artifact-version-history" aria-busy={status === "loading-list" || status === "loading-pair"} onToggle={(event) => setExpanded(event.currentTarget.open)}>
      <summary><span><History size={15} aria-hidden="true" />版本历史与对比</span><small>当前 v{versionNumber(artifact) || "?"}</small></summary>
      <div className="artifact-version-history-body">
        {status === "loading-list" ? <p role="status"><LoaderCircle className="spin" size={14} aria-hidden="true" />正在读取冻结历史…</p> : null}
        {error ? <p className="artifact-version-error" role="alert"><AlertTriangle size={14} aria-hidden="true" /><span>{error}</span><button type="button" className="secondary" onClick={retry}>重试</button></p> : null}
        {status === "ready" && !versions.length ? <p className="artifact-version-empty">没有可用的产物版本记录。</p> : null}
        {omittedVersionCount > 0 ? <p className="artifact-version-omitted"><ShieldCheck size={14} aria-hidden="true" />{omittedVersionCount} 个版本的完整性、版本号或唯一性不满足要求，已从精确比较中排除。</p> : null}
        {selectableVersions.length ? <div className="artifact-version-selectors">
          <label>基准版本<select value={baseVersion || ""} onChange={(event) => setBaseVersion(Number(event.target.value))}>{selectableVersions.map((row) => <option key={JSON.stringify(["base-version", versionNumber(row)])} value={versionNumber(row)}>v{versionNumber(row)} · {typeof row.status === "string" && row.status.trim() ? row.status : "状态未记录"}</option>)}</select></label>
          <GitCompareArrows size={17} aria-hidden="true" />
          <label>对比版本<select value={targetVersion || ""} onChange={(event) => setTargetVersion(Number(event.target.value))}>{selectableVersions.map((row) => <option key={JSON.stringify(["target-version", versionNumber(row)])} value={versionNumber(row)}>v{versionNumber(row)} · {typeof row.status === "string" && row.status.trim() ? row.status : "状态未记录"}</option>)}</select></label>
        </div> : null}
        {versions.length > 0 && !selectableVersions.length ? <p className="artifact-version-empty">版本记录存在完整性或版本号异常，不能加载精确快照进行比较。</p> : null}
        {selectableVersions.length === 1 ? <p className="artifact-version-empty">当前只有一个完整版本；再次保存或确认后即可进行跨版本比较。</p> : null}
        {status === "loading-pair" ? <p role="status"><LoaderCircle className="spin" size={14} aria-hidden="true" />正在对齐两个精确版本…</p> : null}
        {pair ? <DiffView left={pair.left} right={pair.right} /> : null}
      </div>
    </details>
  );
}
