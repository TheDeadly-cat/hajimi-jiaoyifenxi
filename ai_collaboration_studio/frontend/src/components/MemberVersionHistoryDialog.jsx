import { AlertTriangle, CheckCircle2, GitCompareArrows, History, LoaderCircle, ShieldCheck, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import {
  buildMemberVersionDiff,
  formatMemberVersionTime,
  memberLifecycleState,
  memberParticipationState,
  memberVersionSnapshot,
} from "../memberVersionDiff";
import {
  memberHistoryErrorMessage,
  memberHistoryIdentity,
  memberHistoryIntegrityState,
  memberHistorySelectableRows,
  memberHistoryText,
  memberHistoryVersionNumber,
  memberVersionListProjection,
  memberVersionPairProjection,
} from "../memberVersionHistoryUi";
import "../styles/member-version-history.css";
import { useModalFocus } from "../useModalFocus";

function integrityState(record) {
  const state = memberHistoryIntegrityState(record);
  return {
    ...state,
    Icon: state.tone === "ok" ? CheckCircle2 : state.tone === "warning" ? AlertTriangle : ShieldCheck,
  };
}

function diffValue(value) {
  if (value === null || value === undefined || value === "") return "（空）";
  if (value === true) return "是";
  if (value === false) return "否";
  return String(value);
}

function requestStateLabel(state, { idle = "未读取", ready = "已读取" } = {}) {
  return {
    idle,
    loading: "读取中",
    ready,
    error: "读取失败",
  }[state] || "状态未知";
}

function VersionCard({ record, label, currentVersion }) {
  const snapshot = memberVersionSnapshot(record);
  const version = memberHistoryVersionNumber(record);
  const isCurrent = version === currentVersion;
  const integrity = integrityState(record);
  const IntegrityIcon = integrity.Icon;
  return (
    <article className={`member-version-card${isCurrent ? " current" : ""}`}>
      <span>
        <strong>{label} · v{version || "?"}</strong>
        <em className={integrity.tone}>
          <IntegrityIcon size={12} aria-hidden="true" />
          {integrity.label}
        </em>
      </span>
      <small>{memberLifecycleState(record)} · {memberParticipationState(record)}{isCurrent ? " · 当前版本" : ""}</small>
      <small>{memberHistoryText(snapshot.provider, "服务商未记录")} · {memberHistoryText(snapshot.model, "模型未记录")}</small>
      <small>{formatMemberVersionTime(record?.changed_at || snapshot.updated_at)}</small>
    </article>
  );
}

function ChangeValues({ before, after }) {
  return (
    <span className="member-version-change-values">
      <del>{diffValue(before)}</del>
      <ins>{diffValue(after)}</ins>
    </span>
  );
}

function VersionOptions({ rows, prefix }) {
  return rows.map((row) => {
    const version = memberHistoryVersionNumber(row);
    return <option key={`${prefix}:${version}`} value={version}>v{version} · {memberLifecycleState(row)}</option>;
  });
}

function DiffView({ left, right, currentVersion }) {
  const diff = useMemo(() => buildMemberVersionDiff(left, right), [left, right]);
  const leftVersion = memberHistoryVersionNumber(left);
  const rightVersion = memberHistoryVersionNumber(right);
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
  const identity = memberHistoryIdentity(roomId, member);
  const memberId = identity.memberId;
  const memberVersion = identity.currentVersion;
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
  const [integrityWarning, setIntegrityWarning] = useState("");
  const [reloadToken, setReloadToken] = useState(0);
  const selectableVersions = useMemo(() => memberHistorySelectableRows(versions), [versions]);

  useModalFocus({
    open: open && Boolean(member),
    containerRef: dialogRef,
    initialFocusRef: closeButtonRef,
    onClose,
  });

  useEffect(() => {
    if (!open) return undefined;
    let cancelled = false;
    setMemberMeta(null);
    setVersions([]);
    setBaseVersion(0);
    setTargetVersion(0);
    setPair(null);
    setListStatus("loading");
    setPairStatus("idle");
    setError("");
    setIntegrityWarning("");
    if (!identity.integrityOk) {
      setError(identity.issue);
      setListStatus("error");
      return undefined;
    }
    api.memberVersions(identity.roomId, memberId)
      .then((data) => {
        if (cancelled) return;
        const projection = memberVersionListProjection(data, { fallbackMember: member });
        if (!projection.ok) throw new Error(projection.error);
        setMemberMeta(projection.memberMeta);
        setVersions(projection.rows);
        setTargetVersion(projection.targetVersion);
        setBaseVersion(projection.baseVersion);
        setIntegrityWarning(projection.warning);
        setListStatus("ready");
      })
      .catch((requestError) => {
        if (cancelled) return;
        setError(memberHistoryErrorMessage(requestError, "成员身份版本历史读取失败"));
        setListStatus("error");
      });
    return () => { cancelled = true; };
  }, [memberId, memberVersion, open, reloadToken, roomId]);

  useEffect(() => {
    if (!open || !identity.integrityOk || !baseVersion || !targetVersion) return undefined;
    let cancelled = false;
    setPair(null);
    setPairStatus("loading");
    setError("");
    const baseRequest = api.memberVersion(identity.roomId, memberId, baseVersion);
    const targetRequest = baseVersion === targetVersion
      ? baseRequest
      : api.memberVersion(identity.roomId, memberId, targetVersion);
    Promise.all([baseRequest, targetRequest])
      .then(([baseData, targetData]) => {
        if (cancelled) return;
        const projection = memberVersionPairProjection(baseData, targetData, { baseVersion, targetVersion });
        if (!projection.ok) throw new Error(projection.error);
        setPair(projection.pair);
        setPairStatus("ready");
      })
      .catch((requestError) => {
        if (cancelled) return;
        setError(memberHistoryErrorMessage(requestError, "成员身份版本对比读取失败"));
        setPairStatus("error");
      });
    return () => { cancelled = true; };
  }, [baseVersion, memberId, open, roomId, targetVersion]);

  if (!open || !member) return null;

  const currentVersion = memberHistoryVersionNumber({ version: memberMeta?.current_version }) || memberVersion;
  const memberLifecycle = memberLifecycleState(memberMeta || member);
  const displayName = memberHistoryText(memberMeta?.name, identity.name);
  const loading = listStatus === "loading" || pairStatus === "loading";
  return (
    <div
      className="dialog-backdrop member-history-dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose?.();
      }}
    >
      <section
        ref={dialogRef}
        className="dialog member-history-dialog"
        data-list-state={listStatus}
        data-pair-state={pairStatus}
        role="dialog"
        aria-modal="true"
        aria-label={`${displayName}身份版本历史`}
        aria-busy={loading}
        tabIndex={-1}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <span><History size={18} aria-hidden="true" /><span><strong>{displayName} · 身份版本历史</strong><small>{memberLifecycle} · 当前 v{currentVersion || "?"}</small></span></span>
          <button ref={closeButtonRef} type="button" className="icon-button" aria-label="关闭成员身份版本历史" onClick={() => onClose?.()}><X size={18} aria-hidden="true" /></button>
        </header>
        <section className="member-history-status" aria-label="版本证据状态">
          <span><small>记录</small><strong>{versions.length}</strong></span>
          <span><small>可比较</small><strong>{selectableVersions.length}</strong></span>
          <span><small>列表完整性</small><strong>{integrityWarning ? "部分异常" : requestStateLabel(listStatus)}</strong></span>
          <span><small>精确对比</small><strong>{requestStateLabel(pairStatus, { idle: "未绑定", ready: "已绑定" })}</strong></span>
        </section>
        <div className="member-history-body">
          {listStatus === "loading" ? <p className="member-history-loading" role="status"><LoaderCircle className="spin" size={15} aria-hidden="true" />正在读取冻结身份快照…</p> : null}
          {error ? <p className="member-history-error" role="alert"><AlertTriangle size={15} aria-hidden="true" /><span>{error}</span><button type="button" className="secondary" onClick={() => setReloadToken((value) => value + 1)}>重试</button></p> : null}
          {integrityWarning ? <p className="member-history-integrity-warning" role="alert"><ShieldCheck size={15} aria-hidden="true" /><span>{integrityWarning} 异常记录保留在审计台账中，但不会进入精确版本选择。</span></p> : null}
          {listStatus === "ready" && !versions.length ? <p className="member-version-empty">没有可用的身份版本记录。</p> : null}
          {versions.length ? (
            <div className="member-history-layout">
              <aside className="member-history-ledger" aria-label="成员身份版本记录">
                <span><strong>审计记录</strong><small>{versions.length} 个冻结快照</small></span>
                <ol>
                  {versions.map((row, index) => {
                    const rowVersion = memberHistoryVersionNumber(row);
                    const integrity = integrityState(row);
                    const IntegrityIcon = integrity.Icon;
                    const rowClass = integrity.tone === "warning"
                      ? "corrupt"
                      : integrity.tone === "unknown" ? "unknown" : rowVersion === currentVersion ? "current" : "";
                    return (
                      <li className={rowClass} key={`${rowVersion || "unknown"}:${index}`}>
                        <span><strong>v{rowVersion}</strong><small>{formatMemberVersionTime(row.changed_at)}</small></span>
                        <em><IntegrityIcon size={12} aria-hidden="true" />{integrity.tone === "ok" && rowVersion === currentVersion ? "当前" : integrity.label}</em>
                      </li>
                    );
                  })}
                </ol>
              </aside>
              <section className="member-history-comparison" aria-busy={pairStatus === "loading"}>
                {selectableVersions.length ? (
                  <div className="member-version-selectors">
                    <label>基准版本<select value={baseVersion || ""} disabled={loading} onChange={(event) => setBaseVersion(Number(event.target.value))}><VersionOptions rows={selectableVersions} prefix="base" /></select></label>
                    <GitCompareArrows size={18} aria-hidden="true" />
                    <label>对比版本<select value={targetVersion || ""} disabled={loading} onChange={(event) => setTargetVersion(Number(event.target.value))}><VersionOptions rows={selectableVersions} prefix="target" /></select></label>
                  </div>
                ) : <p className="member-version-empty">版本记录存在完整性异常，不能加载精确快照进行比较。</p>}
                {selectableVersions.length === 1 ? <p className="member-version-empty">当前只有一个完整版本；再次保存身份或变更生命周期后即可跨版本比较。</p> : null}
                {pairStatus === "loading" ? <p className="member-history-loading" role="status"><LoaderCircle className="spin" size={15} aria-hidden="true" />正在并行读取两个精确版本…</p> : null}
                {pair ? <DiffView left={pair.left} right={pair.right} currentVersion={currentVersion} /> : null}
              </section>
            </div>
          ) : null}
        </div>
        <footer className="member-history-footer"><small><ShieldCheck size={13} aria-hidden="true" />只读审计记录；查看与对比不会改变当前身份。</small><button type="button" className="secondary" onClick={() => onClose?.()}>关闭</button></footer>
      </section>
    </div>
  );
}
