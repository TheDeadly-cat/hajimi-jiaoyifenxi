import { AlertTriangle, ArrowRight, LoaderCircle, ShieldCheck, X } from "lucide-react";
import { memo, useEffect, useId, useMemo, useRef, useState } from "react";
import { api } from "../api";
import {
  roomSettingsErrorMessage,
  roomSettingsInitialState,
  roomSettingsRows,
  roomSettingsSaveControl,
  roomSettingsVersionHistory,
  sameRoomPackSelection,
} from "../roomSettingsUi";
import "../styles/room-settings.css";
import { capabilityPackContractMeta, shortPluginHash } from "../capabilityContributions";
import {
  packSelectionAvailability,
  pluginLifecycleCatalogView,
  pluginLifecycleRuntimeReason,
  pluginLifecycleStateLabel,
} from "../pluginLifecycle";
import { isModeratorSelectionMissing } from "../moderatorSelection";
import {
  capabilitiesForPackSelection,
  roomDomainCapabilityLabels,
  splitCapabilityPacks,
} from "../roomCapabilities";
import { buildRoomVersionDiff, formatRoomVersionTime } from "../roomVersionDiff";
import {
  STOCK_RESEARCH_PACK_ID,
  stockRoomFormSubmission,
  stockRoomScopeInputState,
} from "../stockResearch";
import { useModalFocus } from "../useModalFocus";
import { CapabilityRegistrySnapshot } from "./CapabilityRegistrySnapshot";
import { CapabilityPackLifecyclePanel } from "./CapabilityPackLifecyclePanel";

const EMPTY_ROWS = Object.freeze([]);

function RoomVersionSetDiff({ label, change }) {
  if (!change?.changed) return null;
  return <section className="material-version-set-diff"><strong>{label}</strong><div>
    {change.removed.map((item, index) => <em className="removed" key={JSON.stringify(["removed", item, index])}>− {item}</em>)}
    {change.added.map((item, index) => <em className="added" key={JSON.stringify(["added", item, index])}>+ {item}</em>)}
  </div></section>;
}

function PackContractMeta({ pack }) {
  const meta = capabilityPackContractMeta(pack);
  if (!meta.version && !meta.hash) return null;
  return <small className="capability-pack-contract-meta">
    v{meta.version || "?"} · {shortPluginHash(meta.hash)} · {meta.adapterCount} adapter · {meta.contributionCount} UI
  </small>;
}

export const RoomSettingsDialog = memo(function RoomSettingsDialog({
  room,
  open,
  capabilityPacks = EMPTY_ROWS,
  pluginLifecycle = null,
  members = EMPTY_ROWS,
  roundRunning = false,
  latestRound,
  onClose,
  onSubmit,
  onPreviewPluginLifecycle,
  onTransitionPluginLifecycle,
}) {
  const memberRows = useMemo(() => roomSettingsRows(members), [members]);
  const capabilityPackRows = useMemo(() => roomSettingsRows(capabilityPacks), [capabilityPacks]);
  const sourceState = useMemo(() => roomSettingsInitialState(room), [room]);
  const [form, setForm] = useState(() => sourceState.form);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [versionHistory, setVersionHistory] = useState([]);
  const [baseVersion, setBaseVersion] = useState("");
  const [targetVersion, setTargetVersion] = useState("");
  const [versionPair, setVersionPair] = useState(null);
  const [historyError, setHistoryError] = useState("");
  const [versionPairError, setVersionPairError] = useState("");
  const [versionBusy, setVersionBusy] = useState(false);
  const versionError = historyError || versionPairError;
  const historyRequestRef = useRef(0);
  const versionPairRequestRef = useRef(0);
  const saveRequestRef = useRef(0);
  const saveInFlightRef = useRef(false);
  const dialogRef = useRef(null);
  const titleInputRef = useRef(null);
  const dialogTitleId = useId();
  const dialogDescriptionId = useId();
  const impactTitleId = useId();
  const canClose = typeof onClose === "function";
  const requestClose = () => {
    if (busy || !canClose) return;
    try {
      onClose();
    } catch (closeError) {
      setError(roomSettingsErrorMessage(closeError, "房间设置窗口关闭失败"));
    }
  };
  useModalFocus({
    open,
    containerRef: dialogRef,
    initialFocusRef: titleInputRef,
    onClose: busy || !canClose ? null : requestClose,
  });
  useEffect(() => () => {
    saveRequestRef.current += 1;
    saveInFlightRef.current = false;
  }, []);
  useEffect(() => {
    saveRequestRef.current += 1;
    saveInFlightRef.current = false;
    if (!open) return;
    setForm(roomSettingsInitialState(room).form);
    setBusy(false);
    setError("");
  }, [open, room?.id, room?.settings_version]);
  useEffect(() => {
    if (open && busy) dialogRef.current?.focus({ preventScroll: true });
  }, [busy, open]);
  useEffect(() => {
    if (!open || !room) return undefined;
    const containDialogFocus = (event) => {
      const dialog = dialogRef.current;
      if (!dialog || dialog.contains(event.target)) return;
      const focusTarget = busy ? dialog : titleInputRef.current;
      focusTarget?.focus({ preventScroll: true });
    };
    document.addEventListener("focusin", containDialogFocus, true);
    return () => document.removeEventListener("focusin", containDialogFocus, true);
  }, [busy, open, room?.id]);
  useEffect(() => {
    if (!open || !room?.id) return undefined;
    const requestId = ++historyRequestRef.current;
    setVersionHistory([]);
    setBaseVersion("");
    setTargetVersion("");
    setVersionPair(null);
    setHistoryError("");
    setVersionPairError("");
    setVersionBusy(true);
    api.roomVersions(room.id).then((result) => {
      if (requestId !== historyRequestRef.current) return;
      const history = roomSettingsVersionHistory(result);
      const versions = history.rows;
      setVersionHistory(versions);
      if (!history.integrityOk) setHistoryError(history.issue);
      setTargetVersion(String(versions[0]?.version || room.settings_version || ""));
      setBaseVersion(String(versions[1]?.version || versions[0]?.version || room.settings_version || ""));
    }).catch((requestError) => {
      if (requestId === historyRequestRef.current) {
        setHistoryError(roomSettingsErrorMessage(requestError, "房间设置历史读取失败"));
      }
    }).finally(() => {
      if (requestId === historyRequestRef.current) setVersionBusy(false);
    });
    return () => {
      if (requestId === historyRequestRef.current) historyRequestRef.current += 1;
    };
  }, [open, room?.id, room?.settings_version]);
  useEffect(() => {
    if (!open || !room?.id || !baseVersion || !targetVersion) return undefined;
    const requestId = ++versionPairRequestRef.current;
    setVersionPair(null);
    setVersionPairError("");
    setVersionBusy(true);
    Promise.all([
      api.roomVersion(room.id, baseVersion),
      api.roomVersion(room.id, targetVersion),
    ]).then(([base, target]) => {
      if (requestId === versionPairRequestRef.current) setVersionPair({ base, target });
    }).catch((requestError) => {
      if (requestId === versionPairRequestRef.current) {
        setVersionPairError(roomSettingsErrorMessage(requestError, "房间设置版本对比失败"));
      }
    }).finally(() => {
      if (requestId === versionPairRequestRef.current) setVersionBusy(false);
    });
    return () => {
      if (requestId === versionPairRequestRef.current) versionPairRequestRef.current += 1;
    };
  }, [open, room?.id, baseVersion, targetVersion]);
  const lifecycleView = useMemo(
    () => pluginLifecycleCatalogView(pluginLifecycle),
    [pluginLifecycle],
  );
  const activeSelectedPackIds = useMemo(
    () => form.capability_pack_ids.filter((packId) => (
      packSelectionAvailability(lifecycleView, packId, { selected: true }).lifecycle?.runtimeAvailable === true
    )),
    [form.capability_pack_ids, lifecycleView],
  );
  const selectedCapabilities = useMemo(
    () => capabilitiesForPackSelection(activeSelectedPackIds, capabilityPackRows),
    [activeSelectedPackIds, capabilityPackRows],
  );
  const capabilityLabels = useMemo(
    () => roomDomainCapabilityLabels({ capabilities: selectedCapabilities }),
    [selectedCapabilities],
  );
  const { coreProtocols, optionalDomainPacks } = useMemo(
    () => splitCapabilityPacks(capabilityPackRows),
    [capabilityPackRows],
  );
  const versionDiff = useMemo(
    () => versionPair ? buildRoomVersionDiff(versionPair.base, versionPair.target) : null,
    [versionPair],
  );
  const moderatorSelectionMissing = isModeratorSelectionMissing(
    form.moderator_member_id,
    memberRows,
  );
  const frozenRound = roundRunning || ["RUNNING", "PAUSED"].includes(String(latestRound?.status || "").toUpperCase());
  const packSelectionChanged = !sameRoomPackSelection(room?.capability_pack_ids, form.capability_pack_ids);
  const stockPackSelected = form.capability_pack_ids.includes(STOCK_RESEARCH_PACK_ID);
  const stockScopeState = stockRoomScopeInputState(form.stock_room_scope_input, {
    requireNonempty: stockPackSelected,
  });
  const stockScopeBlocked = stockPackSelected && !stockScopeState.valid;
  const unavailableSelectedPackIds = form.capability_pack_ids.filter((packId) => (
    packSelectionAvailability(lifecycleView, packId, { selected: true }).lifecycle?.newBindingsAllowed !== true
  ));
  const packSelectionBlocked = packSelectionChanged && unavailableSelectedPackIds.length > 0;
  const saveControl = roomSettingsSaveControl({
    sourceIntegrityOk: sourceState.integrityOk,
    form,
    room,
    busy,
    packSelectionBlocked,
    stockScopeBlocked,
    moderatorSelectionMissing,
    submitHandlerAvailable: typeof onSubmit === "function",
    closeHandlerAvailable: typeof onClose === "function",
  });
  const settingsImpactBlocked = stockScopeBlocked
    || packSelectionBlocked
    || moderatorSelectionMissing;
  const settingsSaveTone = busy
    ? "saving"
    : error || !saveControl.canSubmit ? "blocked" : "review";
  const settingsSaveLabel = busy
    ? "正在保存房间设置"
    : error
      ? "房间设置保存需要处理"
      : saveControl.canSubmit
        ? "设置草稿可由你保存"
        : "设置草稿尚不能保存";
  const settingsSaveDetail = error || saveControl.instruction;
  if (!open || !room) return null;

  const toggleCapabilityPack = (packId) => {
    const selected = form.capability_pack_ids.includes(packId);
    const availability = packSelectionAvailability(lifecycleView, packId, { selected });
    if (!availability.canToggle) return;
    setForm((current) => ({
      ...current,
      capability_pack_ids: current.capability_pack_ids.includes(packId)
        ? current.capability_pack_ids.filter((item) => item !== packId)
        : [...current.capability_pack_ids, packId],
      ...(packId === STOCK_RESEARCH_PACK_ID ? { stock_room_scope_input: "" } : {}),
    }));
  };
  const submit = async (event) => {
    event.preventDefault();
    if (saveInFlightRef.current) return;
    if (!saveControl.canSubmit) {
      if (!busy) setError(saveControl.instruction);
      return;
    }
    const requestId = ++saveRequestRef.current;
    saveInFlightRef.current = true;
    const submitHandler = onSubmit;
    const closeHandler = onClose;
    setBusy(true);
    setError("");
    try {
      await submitHandler(stockRoomFormSubmission({
        ...form,
        expected_settings_version: room.settings_version,
      }));
    } catch (requestError) {
      if (requestId !== saveRequestRef.current) return;
      setError(roomSettingsErrorMessage(requestError, "房间设置保存失败"));
      return;
    } finally {
      if (requestId === saveRequestRef.current) {
        saveInFlightRef.current = false;
        setBusy(false);
      }
    }
    if (requestId === saveRequestRef.current) {
      try {
        closeHandler();
      } catch (closeError) {
        setError(`房间设置已保存，但窗口关闭失败：${roomSettingsErrorMessage(closeError, "未知关闭错误")}`);
      }
    }
  };

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget) requestClose();
    }}>
      <form ref={dialogRef} className="dialog room-settings-dialog" role="dialog" aria-modal="true" aria-labelledby={dialogTitleId} aria-describedby={dialogDescriptionId} aria-busy={busy} data-save-state={saveControl.phase} tabIndex={-1} onSubmit={submit} onMouseDown={(event) => event.stopPropagation()}>
        <header>
          <div className="room-settings-heading">
            <div className="room-settings-heading-copy">
              <small>ROOM SETTINGS / VERSIONED</small>
              <h2 id={dialogTitleId}>房间设置</h2>
            </div>
            <small id={dialogDescriptionId}>模板来源：{room.template_id} · 修改从下一轮开始生效</small>
          </div>
          <button type="button" className="icon-button" aria-label="关闭房间设置" onClick={requestClose} disabled={busy || !canClose}><X aria-hidden="true" size={18} /></button>
        </header>
        <section className="room-settings-status" aria-label="保存前状态" role="list">
          <span role="listitem"><small>设置版本</small><strong>v<data value={room.settings_version}>{room.settings_version}</data></strong></span>
          <span role="listitem"><small>已选能力包</small><strong><data value={form.capability_pack_ids.length}>{form.capability_pack_ids.length}</data> 项</strong></span>
          <span role="listitem"><small>生效边界</small><strong>{frozenRound ? "冻结轮次不变" : "下一轮"}</strong></span>
        </section>
        {!sourceState.integrityOk ? <p className="room-settings-source-warning" role="alert">来源设置已按安全结构打开：{sourceState.issues[0]} 保存前请复核全部字段。</p> : null}
        <section
          className={`room-settings-impact ${settingsImpactBlocked ? "blocked" : "review"}`}
          aria-labelledby={impactTitleId}
          role="note"
        >
          <header>
            {settingsImpactBlocked
              ? <AlertTriangle size={20} aria-hidden="true" />
              : <ShieldCheck size={20} aria-hidden="true" />}
            <span>
              <small>NEXT ROUND BOUNDARY</small>
              <h3 id={impactTitleId}>{frozenRound ? "当前冻结轮次保持不变" : "设置只从下一轮开始生效"}</h3>
            </span>
            <data value={settingsImpactBlocked ? 1 : 0}>
              {settingsImpactBlocked ? "需修复" : "边界已说明"}
            </data>
          </header>
          <p>保存成功后服务端会生成新的设置版本；不会回写历史消息、已冻结流程、证据或执行路由。</p>
          <div className="room-settings-impact-grid" role="list">
            <span role="listitem">
              <small>能力包选择</small>
              <strong>{packSelectionChanged ? "有待保存变化" : "保持当前集合"}</strong>
            </span>
            <span role="listitem">
              <small>股票池合同</small>
              <strong>{stockPackSelected ? stockScopeState.valid ? "显式范围有效" : "需要修复" : "未启用"}</strong>
            </span>
            <span role="listitem">
              <small>主持选择</small>
              <strong>{moderatorSelectionMissing ? "需要重新选择" : "当前可核验"}</strong>
            </span>
          </div>
        </section>
        <fieldset className="room-settings-editable" disabled={busy}>
          <legend className="room-settings-sr-only">可编辑房间设置</legend>
        <label>房间名称<input ref={titleInputRef} required maxLength={80} value={form.title} onChange={(event) => setForm((current) => ({ ...current, title: event.target.value }))} /></label>
        <label>长期目标<textarea required maxLength={20000} value={form.objective} onChange={(event) => setForm((current) => ({ ...current, objective: event.target.value }))} /></label>
        <div className="form-grid">
          <label>归属分类<input required maxLength={120} value={form.category} onChange={(event) => setForm((current) => ({ ...current, category: event.target.value }))} placeholder="例如：交易研究 / 美股" /></label>
          <label>讨论调度<select value={form.discussion_mode} onChange={(event) => setForm((current) => ({ ...current, discussion_mode: event.target.value }))}>
            <option value="dynamic">主持人动态调度</option>
            <option value="sequential">按成员顺序发言</option>
          </select></label>
          <label>动态主持 AI<select value={form.moderator_member_id} onChange={(event) => setForm((current) => ({ ...current, moderator_member_id: event.target.value }))}>
            <option value="">自动：流程首阶段成员</option>
            {moderatorSelectionMissing ? <option value={form.moderator_member_id} disabled>
              当前主持已失效，请重新选择
            </option> : null}
            {memberRows.map((member, index) => <option key={JSON.stringify([member.id, index])} value={member.id} disabled={!member.enabled}>
              {member.name || "未命名成员"}{member.enabled ? "" : "（已暂停）"}
            </option>)}
          </select></label>
          <label>普通消息响应<select value={form.idle_response_mode} onChange={(event) => setForm((current) => ({ ...current, idle_response_mode: event.target.value }))}>
            <option value="stored_only">仅保存，不自动回复</option>
            <option value="mention_only">只响应明确点名</option>
            <option value="moderator_auto">主持人自动选择一人回复</option>
          </select></label>
        </div>
        <p className="field-help compact-help">使用“/”创建子类；分类只负责组织房间，不改变成员身份和研究能力。</p>
        <p className="field-help compact-help">指定的主持 AI 只负责隐藏动态点名；它的身份、职责与边界会作为调度偏好，不能覆盖证据、安全和用户最终决策规则。新设置从下一轮开始生效。</p>
        <p className="field-help compact-help">“主持人自动选择”会为每条未点名的普通消息触发一次成员模型调用；点名仍优先按你指定的身份版本回复。默认只响应点名。</p>
        {coreProtocols.length ? <fieldset className="capability-pack-picker room-settings-pack-picker core-protocol-picker">
          <legend>群聊内核协议（始终启用）</legend>
          <p>新正式轮固定使用可审计 AI 回应链；能力包设置不能关闭，旧轮与暂停恢复不会被回填。</p>
          {coreProtocols.map((pack) => (
            <div className="capability-pack-card selected core-protocol" key={JSON.stringify(["core", pack.id])}>
              <ShieldCheck size={14} aria-hidden="true" />
              <span><strong>{pack.name}</strong><small>{pack.description}</small><PackContractMeta pack={pack} />{pack.discussion_protocol?.title ? <small>协议：{pack.discussion_protocol.title}</small> : null}</span>
              <em>始终启用</em>
            </div>
          ))}
        </fieldset> : null}
        <fieldset className="capability-pack-picker room-settings-pack-picker">
          <legend>领域能力包</legend>
          <p>能力包可随时调整，只影响下一轮新讨论；历史消息、资料、观察和模拟组合不会被删除。</p>
          {optionalDomainPacks.map((pack) => {
            const selected = form.capability_pack_ids.includes(pack.id);
            const availability = packSelectionAvailability(lifecycleView, pack.id, { selected });
            const lifecycle = availability.lifecycle;
            const classes = ["capability-pack-card", selected ? "selected" : "", lifecycle?.runtimeState || "lifecycle-unverified"].filter(Boolean).join(" ");
            return <label className={classes} key={JSON.stringify(["domain", pack.id])} title={!availability.canToggle ? availability.reason : ""}>
              <input type="checkbox" checked={selected} disabled={!availability.canToggle} onChange={() => toggleCapabilityPack(pack.id)} />
              <span><strong>{pack.name}</strong><small>{pack.description}</small><PackContractMeta pack={pack} />{pack.discussion_protocol?.title ? <small>协议：{pack.discussion_protocol.title}</small> : null}{lifecycle && !lifecycle.runtimeAvailable ? <small className="capability-pack-lifecycle-reason">{pluginLifecycleRuntimeReason(lifecycle)}</small> : null}</span>
              <em>{lifecycle ? pluginLifecycleStateLabel(lifecycle) : "状态未验证"}</em>
            </label>;
          })}
          {!optionalDomainPacks.length ? <p>当前没有可选领域能力包。</p> : null}
          <div className="template-capability-preview room-settings-capability-preview" aria-label="保存后的房间能力">
            <strong>保存后启用</strong><span>{capabilityLabels.map((label, index) => <em key={JSON.stringify([label, index])}>{label}</em>)}</span>
          </div>
        </fieldset>
        {stockPackSelected ? <label className="stock-room-scope-field">
          显式股票池（每行一个 MARKET:TICKER）
          <textarea
            required
            aria-invalid={!stockScopeState.valid}
            value={form.stock_room_scope_input}
            maxLength={10000}
            onChange={(event) => setForm((current) => ({ ...current, stock_room_scope_input: event.target.value }))}
            placeholder={"US:AAPL\nUS:MSFT"}
          />
          <small>修改股票池会生成新的房间设置版本，并使旧的下一轮股票授权立即失效。</small>
          {!stockScopeState.valid ? <em className="stock-room-scope-error" role="alert">{stockScopeState.error}</em> : null}
        </label> : null}
        {packSelectionBlocked ? <p className="field-help plugin-lifecycle-selection-block" role="alert">当前选择仍包含不可用于新绑定的能力包：{unavailableSelectedPackIds.join("、")}。请先移除这些能力包，或保持原选择不变。</p> : null}
        <CapabilityRegistrySnapshot
          room={room}
          capabilityPacks={capabilityPackRows}
          pluginLifecycle={pluginLifecycle}
          pendingPackIds={form.capability_pack_ids}
        />
        <CapabilityPackLifecyclePanel
          pluginLifecycle={pluginLifecycle}
          onPreview={onPreviewPluginLifecycle}
          onTransition={onTransitionPluginLifecycle}
        />
        <section className="material-version-history room-version-history">
          <span className="material-version-heading"><strong>房间设置版本（只读）</strong><small>当前 v{room.settings_version || 1}</small></span>
          {versionHistory.length ? <>
            <div className="material-version-selectors">
              <label>基准版本<select value={baseVersion} disabled={busy || versionBusy} onChange={(event) => setBaseVersion(event.target.value)}>{versionHistory.map((item, index) => <option key={JSON.stringify(["base", item.version, index])} value={item.version}>v{item.version} · {formatRoomVersionTime(item.changed_at)}</option>)}</select></label>
              <ArrowRight size={15} aria-hidden="true" />
              <label>对比版本<select value={targetVersion} disabled={busy || versionBusy} onChange={(event) => setTargetVersion(event.target.value)}>{versionHistory.map((item, index) => <option key={JSON.stringify(["target", item.version, index])} value={item.version}>v{item.version} · {formatRoomVersionTime(item.changed_at)}</option>)}</select></label>
            </div>
            {versionBusy ? <p className="material-version-empty">正在读取冻结设置快照…</p> : null}
            {versionDiff ? <div className="material-version-diff" aria-label={`房间设置 v${baseVersion} 与 v${targetVersion} 对比`}>
              {!versionDiff.changed ? <p className="material-version-empty">两个版本的房间设置完全一致。</p> : null}
              {versionDiff.fieldChanges.length ? <section className="material-version-field-diff"><strong>设置字段变化</strong>{versionDiff.fieldChanges.map((change) => <article className={change.key === "objective" || change.key === "workflow_policy" ? "content-change" : ""} key={change.key}><b>{change.label}</b><span className={change.key === "objective" || change.key === "workflow_policy" ? "material-version-change-values content" : "material-version-change-values"}><del>{change.before || "（空）"}</del><ins>{change.after || "（空）"}</ins></span></article>)}</section> : null}
              <RoomVersionSetDiff label="能力包变化" change={versionDiff.capabilityPacks} />
              <RoomVersionSetDiff label="派生能力变化" change={versionDiff.capabilities} />
              <p className="material-version-readonly-note"><ShieldCheck aria-hidden="true" size={13} />冻结快照仅供审计，不会恢复或覆盖当前房间设置。</p>
            </div> : null}
          </> : versionBusy ? <p className="material-version-empty">正在读取版本索引…</p> : <p className="material-version-empty">暂无可对比的设置版本。</p>}
          {versionError ? <span className="material-version-error" role="alert"><AlertTriangle aria-hidden="true" size={13} />{versionError}</span> : null}
        </section>
        {frozenRound ? <div className="room-settings-freeze-note"><ShieldCheck aria-hidden="true" size={15} /><span><strong>当前轮次不会漂移</strong><small>进行中或已暂停轮次继续使用检查点冻结的流程、能力包和证据；本次修改从下一轮开始生效。</small></span></div> : null}
        {error ? <p className="dialog-error" role="alert">{error}</p> : null}
        </fieldset>
        <footer>
          <div className={`room-settings-save-summary ${settingsSaveTone}`} role="status" aria-live="polite">
            {busy
              ? <LoaderCircle className="spin" size={17} aria-hidden="true" />
              : settingsSaveTone === "review"
                ? <ShieldCheck size={17} aria-hidden="true" />
                : <AlertTriangle size={17} aria-hidden="true" />}
            <span><strong>{settingsSaveLabel}</strong><small>{settingsSaveDetail}</small></span>
          </div>
          <span className="room-settings-actions"><button type="button" className="secondary" onClick={requestClose} disabled={busy || !canClose}>取消</button><button className="primary" type="submit" disabled={!saveControl.canSubmit}>{busy ? <LoaderCircle className="spin" size={14} aria-hidden="true" /> : null}{busy ? "正在保存…" : "保存房间设置"}</button></span>
        </footer>
      </form>
    </div>
  );
});
