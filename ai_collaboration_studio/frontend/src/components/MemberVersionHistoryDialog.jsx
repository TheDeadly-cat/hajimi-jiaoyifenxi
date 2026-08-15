import { AlertTriangle, CheckCircle2, GitCompareArrows, History, LoaderCircle, ShieldCheck, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { buildMemberVersionDiff, formatMemberVersionTime, memberVersionSnapshot } from "../memberVersionDiff";
import "../styles/member-version-history.css";
import { useModalFocus } from "../useModalFocus";

function versionNumber(record) {
  return Number(record?.version || record?.snapshot?.version || 0);
}

function lifecycleLabel(record) {
  const snapshot = memberVersionSnapshot(record);
  const archivedAt = Number(snapshot.archived_at || record?.archived_at || 0);
  return snapshot.archived === true || record?.archived === true || archivedAt > 0 ? "已归档" : "活动";
}

function participationLabel(record) {
  const snapshot = memberVersionSnapshot(record);
  const enabled = Object.hasOwn(snapshot, "enabled") ? snapshot.enabled : record?.enabled;
  return enabled ? "参与讨论" : "暂停参与";
}

function VersionCard({ record, label, currentVersion }) {
  const snapshot = memberVersionSnapshot(record);
  const version = versionNumber(record);
  const isCurrent = version === currentVersion;
  return (
    <article className="member-version-card">
      <span>
        <strong>{label} · v{version || "?"}</strong>
        <em className={record?.integrity_ok === false ? "warning" : "ok"}>
          {record?.integrity_ok === false ? <AlertTriangle size={12} /> : <CheckCircle2 size={12} />}
          {record?.integrity_ok === false ? "快照异常" : "冻结快照"}
        </em>
      </span>
      <small>{lifecycleLabel(record)} · {participationLabel(record)}{isCurrent ? " · 当前版本" : ""}</small>
      <small>{snapshot.provider || "未指定服务商"} · {snapshot.model || "默认模型"}</small>
      <small>{formatMemberVersionTime(record?.changed_at || snapshot.updated_at)}</small>
    </article>
  );
}

function ChangeValues({ before, after }) {
  return (
    <span className="member-version-change-values">
      <del>{before || "（空）"}</del>
      <ins>{after || "（空）"}</ins>
    </span>
  );
}

function DiffView({ left, right, currentVersion }) {
  const diff = useMemo(() => buildMemberVersionDiff(left, right), [left, right]);
  const leftVersion = versionNumber(left);
  const rightVersion = versionNumber(right);
  return (
    <div className="member-version-diff" aria-label={`成员身份 v${leftVersion} 与 v${rightVersion} 对比`}>
      <div className="member-version-cards">
        <VersionCard record={left} label="基准" currentVersion={currentVersion} />
        <VersionCard record={right} label="对比" currentVersion={currentVersion} />
      </div>
      {!diff.changed ? <p className="member-version-empty">两个版本的身份、模型配置、参与状态和能力完全一致。</p> : null}
      {diff.fieldChanges.length ? (
        <section className="member-version-field-diff">
          <strong>身份字段变化</strong>
          {diff.fieldChanges.map((change) => (
            <article key={change.key}>
              <span>{change.label}</span>
              <ChangeValues before={change.before} after={change.after} />
            </article>
          ))}
        </section>
      ) : null}
      {diff.capabilities.changed ? (
        <section className="member-capability-diff">
          <strong>能力边界变化</strong>
          <div>
            {diff.capabilities.added.map((capability) => <em className="added" key={`added:${capability}`}>+ {capability}</em>)}
            {diff.capabilities.removed.map((capability) => <em className="removed" key={`removed:${capability}`}>− {capability}</em>)}
          </div>
        </section>
      ) : null}
    </div>
  );
}

export function MemberVersionHistoryDialog({ roomId, member, open, onClose }) {
  const memberId = member?.id || "";
  const memberVersion = Number(member?.version || 0);
  const dialogRef = useRef(null);
  const closeButtonRef = useRef(null);
  const [memberMeta, setMemberMeta] = useState(null);
  const [versions, setVersions] = useState([]);
  const [baseVersion, setBaseVersion] = useState(0);
  const [targetVersion, setTargetVersion] = useState(0);
  const [pair, setPair] = useState(null);
  const [listStatus, setListStatus] = useState("idle");
  const [pairStatus, setPairStatus] = useState("idle");
  const [error, setError] = useState("");
  const [reloadToken, setReloadToken] = useState(0);

  useModalFocus({
    open: open && Boolean(member),
    containerRef: dialogRef,
    initialFocusRef: closeButtonRef,
    onClose,
  });

  useEffect(() => {
    if (!open || !roomId || !memberId) return undefined;
    let cancelled = false;
    setMemberMeta(null);
    setVersions([]);
    setBaseVersion(0);
    setTargetVersion(0);
    setPair(null);
    setListStatus("loading");
    setPairStatus("idle");
    setError("");
    api.memberVersions(roomId, memberId)
      .then((data) => {
        if (cancelled) return;
        const rows = Array.isArray(data.versions) ? data.versions : [];
        const selectableRows = rows.filter((row) => row?.integrity_ok !== false && Number(row?.version) > 0);
        const currentVersion = Number(data.member?.current_version || memberVersion || 0);
        const target = selectableRows.find((row) => Number(row.version) === currentVersion)
          || selectableRows[0];
        const targetValue = Number(target?.version || 0);
        const base = selectableRows.find((row) => Number(row.version) < targetValue)
          || selectableRows.find((row) => Number(row.version) !== targetValue)
          || target;
        setMemberMeta(data.member || null);
        setVersions(rows);
        setTargetVersion(targetValue);
        setBaseVersion(Number(base?.version || 0));
        setListStatus("ready");
      })
      .catch((requestError) => {
        if (cancelled) return;
        setError(requestError.message || "成员身份版本历史读取失败");
        setListStatus("error");
      });
    return () => { cancelled = true; };
  }, [memberId, memberVersion, open, reloadToken, roomId]);

  useEffect(() => {
    if (!open || !roomId || !memberId || !baseVersion || !targetVersion) return undefined;
    let cancelled = false;
    setPair(null);
    setPairStatus("loading");
    setError("");
    const baseRequest = api.memberVersion(roomId, memberId, baseVersion);
    const targetRequest = baseVersion === targetVersion
      ? baseRequest
      : api.memberVersion(roomId, memberId, targetVersion);
    Promise.all([baseRequest, targetRequest])
      .then(([baseData, targetData]) => {
        if (cancelled) return;
        if (!baseData?.member_version || !targetData?.member_version) {
          throw new Error("成员身份版本快照响应不完整");
        }
        setPair({ left: baseData.member_version, right: targetData.member_version });
        setPairStatus("ready");
      })
      .catch((requestError) => {
        if (cancelled) return;
        setError(requestError.message || "成员身份版本对比读取失败");
        setPairStatus("error");
      });
    return () => { cancelled = true; };
  }, [baseVersion, memberId, open, roomId, targetVersion]);

  if (!open || !member) return null;

  const currentVersion = Number(memberMeta?.current_version || memberVersion || 0);
  const selectableVersions = versions.filter((row) => row?.integrity_ok !== false && Number(row?.version) > 0);
  const archived = memberMeta?.archived ?? member.archived ?? Number(member.archived_at || 0) > 0;
  return (
    <div
      className="dialog-backdrop member-history-dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        ref={dialogRef}
        className="dialog member-history-dialog"
        role="dialog"
        aria-modal="true"
        aria-label={`${memberMeta?.name || member.name || "成员"}身份版本历史`}
        tabIndex={-1}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <span><History size={18} /><span><strong>{memberMeta?.name || member.name || "成员"} · 身份版本历史</strong><small>{archived ? "已归档成员" : "活动成员"} · 当前 v{currentVersion || "?"}</small></span></span>
          <button ref={closeButtonRef} type="button" className="icon-button" aria-label="关闭成员身份版本历史" onClick={onClose}><X size={18} /></button>
        </header>
        <div className="member-history-body">
          {listStatus === "loading" ? <p className="member-history-loading"><LoaderCircle className="spin" size={15} />正在读取冻结身份快照…</p> : null}
          {error ? <p className="member-history-error" role="alert"><AlertTriangle size={15} /><span>{error}</span><button type="button" className="secondary" onClick={() => setReloadToken((value) => value + 1)}>重试</button></p> : null}
          {listStatus === "ready" && !versions.length ? <p className="member-version-empty">没有可用的身份版本记录。</p> : null}
          {versions.length ? (
            <div className="member-history-layout">
              <aside className="member-history-ledger" aria-label="成员身份版本记录">
                <span><strong>审计记录</strong><small>{versions.length} 个冻结快照</small></span>
                <ol>
                  {versions.map((row) => {
                    const rowVersion = Number(row.version || 0);
                    const integrityOk = row.integrity_ok !== false;
                    return (
                      <li className={!integrityOk ? "corrupt" : rowVersion === currentVersion ? "current" : ""} key={rowVersion}>
                        <span><strong>v{rowVersion}</strong><small>{formatMemberVersionTime(row.changed_at)}</small></span>
                        <em>{!integrityOk ? <AlertTriangle size={12} /> : rowVersion === currentVersion ? <CheckCircle2 size={12} /> : null}{!integrityOk ? "异常" : rowVersion === currentVersion ? "当前" : lifecycleLabel(row)}</em>
                      </li>
                    );
                  })}
                </ol>
              </aside>
              <section className="member-history-comparison">
                {selectableVersions.length ? (
                  <div className="member-version-selectors">
                    <label>基准版本<select value={baseVersion || ""} onChange={(event) => setBaseVersion(Number(event.target.value))}>{selectableVersions.map((row) => <option key={`base:${row.version}`} value={row.version}>v{row.version} · {lifecycleLabel(row)}</option>)}</select></label>
                    <GitCompareArrows size={18} />
                    <label>对比版本<select value={targetVersion || ""} onChange={(event) => setTargetVersion(Number(event.target.value))}>{selectableVersions.map((row) => <option key={`target:${row.version}`} value={row.version}>v{row.version} · {lifecycleLabel(row)}</option>)}</select></label>
                  </div>
                ) : <p className="member-version-empty">版本记录存在完整性异常，不能加载精确快照进行比较。</p>}
                {selectableVersions.length === 1 ? <p className="member-version-empty">当前只有一个完整版本；再次保存身份或变更生命周期后即可跨版本比较。</p> : null}
                {pairStatus === "loading" ? <p className="member-history-loading"><LoaderCircle className="spin" size={15} />正在并行读取两个精确版本…</p> : null}
                {pair ? <DiffView left={pair.left} right={pair.right} currentVersion={currentVersion} /> : null}
              </section>
            </div>
          ) : null}
        </div>
        <footer className="member-history-footer"><small><ShieldCheck size={13} />只读审计记录；查看与对比不会改变当前身份。</small><button type="button" className="secondary" onClick={onClose}>关闭</button></footer>
      </section>
    </div>
  );
}
