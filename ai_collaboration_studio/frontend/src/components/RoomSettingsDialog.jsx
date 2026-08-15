import { AlertTriangle, ArrowRight, ShieldCheck, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
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
  stockRoomScopeInputValue,
} from "../stockResearch";
import { useModalFocus } from "../useModalFocus";
import { CapabilityRegistrySnapshot } from "./CapabilityRegistrySnapshot";
import { CapabilityPackLifecyclePanel } from "./CapabilityPackLifecyclePanel";

function settingsForm(room) {
  return {
    title: room?.title || "",
    objective: room?.objective || "",
    category: room?.category || "通用共创",
    discussion_mode: room?.discussion_mode || "dynamic",
    moderator_member_id: room?.moderator_member_id || "",
    idle_response_mode: room?.idle_response_mode || "mention_only",
    capability_pack_ids: [...(room?.capability_pack_ids || [])],
    stock_room_scope_input: stockRoomScopeInputValue(room?.stock_room_scope),
  };
}

function RoomVersionSetDiff({ label, change }) {
  if (!change?.changed) return null;
  return <section className="material-version-set-diff"><strong>{label}</strong><div>
    {change.removed.map((item) => <em className="removed" key={`removed-${item}`}>− {item}</em>)}
    {change.added.map((item) => <em className="added" key={`added-${item}`}>+ {item}</em>)}
  </div></section>;
}

function PackContractMeta({ pack }) {
  const meta = capabilityPackContractMeta(pack);
  if (!meta.version && !meta.hash) return null;
  return <small className="capability-pack-contract-meta">
    v{meta.version || "?"} · {shortPluginHash(meta.hash)} · {meta.adapterCount} adapter · {meta.contributionCount} UI
  </small>;
}

function samePackSelection(left, right) {
  const a = [...new Set(left || [])].sort();
  const b = [...new Set(right || [])].sort();
  return a.length === b.length && a.every((item, index) => item === b[index]);
}

export function RoomSettingsDialog({
  room,
  open,
  capabilityPacks = [],
  pluginLifecycle = null,
  members = [],
  roundRunning = false,
  latestRound,
  onClose,
  onSubmit,
  onPreviewPluginLifecycle,
  onTransitionPluginLifecycle,
}) {
  const [form, setForm] = useState(() => settingsForm(room));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [versionHistory, setVersionHistory] = useState([]);
  const [baseVersion, setBaseVersion] = useState("");
  const [targetVersion, setTargetVersion] = useState("");
  const [versionPair, setVersionPair] = useState(null);
  const [versionError, setVersionError] = useState("");
  const [versionBusy, setVersionBusy] = useState(false);
  const historyRequestRef = useRef(0);
  const versionPairRequestRef = useRef(0);
  const dialogRef = useRef(null);
  const titleInputRef = useRef(null);
  useModalFocus({
    open,
    containerRef: dialogRef,
    initialFocusRef: titleInputRef,
    onClose: busy ? null : onClose,
  });
  useEffect(() => {
    if (!open) return;
    setForm(settingsForm(room));
    setBusy(false);
    setError("");
  }, [open, room?.id, room?.settings_version]);
  useEffect(() => {
    if (!open || !room?.id) return undefined;
    const requestId = ++historyRequestRef.current;
    setVersionHistory([]);
    setBaseVersion("");
    setTargetVersion("");
    setVersionPair(null);
    setVersionError("");
    setVersionBusy(true);
    api.roomVersions(room.id).then((result) => {
      if (requestId !== historyRequestRef.current) return;
      const versions = Array.isArray(result.versions) ? result.versions : [];
      setVersionHistory(versions);
      setTargetVersion(String(versions[0]?.version || room.settings_version || ""));
      setBaseVersion(String(versions[1]?.version || versions[0]?.version || room.settings_version || ""));
    }).catch((requestError) => {
      if (requestId === historyRequestRef.current) {
        setVersionError(requestError.message || "房间设置历史读取失败");
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
    setVersionError("");
    setVersionBusy(true);
    Promise.all([
      api.roomVersion(room.id, baseVersion),
      api.roomVersion(room.id, targetVersion),
    ]).then(([base, target]) => {
      if (requestId === versionPairRequestRef.current) setVersionPair({ base, target });
    }).catch((requestError) => {
      if (requestId === versionPairRequestRef.current) {
        setVersionError(requestError.message || "房间设置版本对比失败");
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
    () => capabilitiesForPackSelection(activeSelectedPackIds, capabilityPacks),
    [activeSelectedPackIds, capabilityPacks],
  );
  const capabilityLabels = roomDomainCapabilityLabels({ capabilities: selectedCapabilities });
  const { coreProtocols, optionalDomainPacks } = useMemo(
    () => splitCapabilityPacks(capabilityPacks),
    [capabilityPacks],
  );
  const versionDiff = useMemo(
    () => versionPair ? buildRoomVersionDiff(versionPair.base, versionPair.target) : null,
    [versionPair],
  );
  const moderatorSelectionMissing = isModeratorSelectionMissing(
    form.moderator_member_id,
    members,
  );
  const frozenRound = roundRunning || ["RUNNING", "PAUSED"].includes(String(latestRound?.status || "").toUpperCase());
  const packSelectionChanged = !samePackSelection(room?.capability_pack_ids, form.capability_pack_ids);
  const stockPackSelected = form.capability_pack_ids.includes(STOCK_RESEARCH_PACK_ID);
  const stockScopeState = stockRoomScopeInputState(form.stock_room_scope_input, {
    requireNonempty: stockPackSelected,
  });
  const stockScopeBlocked = stockPackSelected && !stockScopeState.valid;
  const unavailableSelectedPackIds = form.capability_pack_ids.filter((packId) => (
    packSelectionAvailability(lifecycleView, packId, { selected: true }).lifecycle?.newBindingsAllowed !== true
  ));
  const packSelectionBlocked = packSelectionChanged && unavailableSelectedPackIds.length > 0;
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
    if (busy || packSelectionBlocked || stockScopeBlocked) return;
    setBusy(true);
    setError("");
    try {
      await onSubmit(stockRoomFormSubmission({
        ...form,
        expected_settings_version: room.settings_version,
      }));
      onClose();
    } catch (requestError) {
      setError(requestError.message || "房间设置保存失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={busy ? undefined : onClose}>
      <form ref={dialogRef} className="dialog room-settings-dialog" role="dialog" aria-modal="true" aria-label="房间设置" onSubmit={submit} onMouseDown={(event) => event.stopPropagation()}>
        <header><span><strong>房间设置</strong><small>模板来源：{room.template_id}</small></span><button type="button" className="icon-button" aria-label="关闭房间设置" onClick={onClose} disabled={busy}><X size={18} /></button></header>
        <label>房间名称<input ref={titleInputRef} required maxLength={80} value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} /></label>
        <label>长期目标<textarea required value={form.objective} onChange={(event) => setForm({ ...form, objective: event.target.value })} /></label>
        <div className="form-grid">
          <label>归属分类<input required value={form.category} onChange={(event) => setForm({ ...form, category: event.target.value })} placeholder="例如：交易研究 / 美股" /></label>
          <label>讨论调度<select value={form.discussion_mode} onChange={(event) => setForm({ ...form, discussion_mode: event.target.value })}>
            <option value="dynamic">主持人动态调度</option>
            <option value="sequential">按成员顺序发言</option>
          </select></label>
          <label>动态主持 AI<select value={form.moderator_member_id} onChange={(event) => setForm({ ...form, moderator_member_id: event.target.value })}>
            <option value="">自动：流程首阶段成员</option>
            {moderatorSelectionMissing ? <option value={form.moderator_member_id} disabled>
              当前主持已失效，请重新选择
            </option> : null}
            {members.map((member) => <option key={member.id} value={member.id} disabled={!member.enabled}>
              {member.name || "未命名成员"}{member.enabled ? "" : "（已暂停）"}
            </option>)}
          </select></label>
          <label>普通消息响应<select value={form.idle_response_mode} onChange={(event) => setForm({ ...form, idle_response_mode: event.target.value })}>
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
            <div className="capability-pack-card selected core-protocol" key={pack.id}>
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
            return <label className={classes} key={pack.id} title={!availability.canToggle ? availability.reason : ""}>
              <input type="checkbox" checked={selected} disabled={!availability.canToggle} onChange={() => toggleCapabilityPack(pack.id)} />
              <span><strong>{pack.name}</strong><small>{pack.description}</small><PackContractMeta pack={pack} />{pack.discussion_protocol?.title ? <small>协议：{pack.discussion_protocol.title}</small> : null}{lifecycle && !lifecycle.runtimeAvailable ? <small className="capability-pack-lifecycle-reason">{pluginLifecycleRuntimeReason(lifecycle)}</small> : null}</span>
              <em>{lifecycle ? pluginLifecycleStateLabel(lifecycle) : "状态未验证"}</em>
            </label>;
          })}
          {!optionalDomainPacks.length ? <p>当前没有可选领域能力包。</p> : null}
          <div className="template-capability-preview room-settings-capability-preview" aria-label="保存后的房间能力">
            <strong>保存后启用</strong><span>{capabilityLabels.map((label) => <em key={label}>{label}</em>)}</span>
          </div>
        </fieldset>
        {stockPackSelected ? <label className="stock-room-scope-field">
          显式股票池（每行一个 MARKET:TICKER）
          <textarea
            required
            aria-invalid={!stockScopeState.valid}
            value={form.stock_room_scope_input}
            onChange={(event) => setForm({ ...form, stock_room_scope_input: event.target.value })}
            placeholder={"US:AAPL\nUS:MSFT"}
          />
          <small>修改股票池会生成新的房间设置版本，并使旧的下一轮股票授权立即失效。</small>
          {!stockScopeState.valid ? <em className="stock-room-scope-error" role="alert">{stockScopeState.error}</em> : null}
        </label> : null}
        {packSelectionBlocked ? <p className="field-help plugin-lifecycle-selection-block" role="alert">当前选择仍包含不可用于新绑定的能力包：{unavailableSelectedPackIds.join("、")}。请先移除这些能力包，或保持原选择不变。</p> : null}
        <CapabilityRegistrySnapshot
          room={room}
          capabilityPacks={capabilityPacks}
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
              <label>基准版本<select value={baseVersion} onChange={(event) => setBaseVersion(event.target.value)}>{versionHistory.map((item) => <option key={`base-${item.version}`} value={item.version}>v{item.version} · {formatRoomVersionTime(item.changed_at)}</option>)}</select></label>
              <ArrowRight size={15} />
              <label>对比版本<select value={targetVersion} onChange={(event) => setTargetVersion(event.target.value)}>{versionHistory.map((item) => <option key={`target-${item.version}`} value={item.version}>v{item.version} · {formatRoomVersionTime(item.changed_at)}</option>)}</select></label>
            </div>
            {versionBusy ? <p className="material-version-empty">正在读取冻结设置快照…</p> : null}
            {versionDiff ? <div className="material-version-diff" aria-label={`房间设置 v${baseVersion} 与 v${targetVersion} 对比`}>
              {!versionDiff.changed ? <p className="material-version-empty">两个版本的房间设置完全一致。</p> : null}
              {versionDiff.fieldChanges.length ? <section className="material-version-field-diff"><strong>设置字段变化</strong>{versionDiff.fieldChanges.map((change) => <article className={change.key === "objective" || change.key === "workflow_policy" ? "content-change" : ""} key={change.key}><b>{change.label}</b><span className={change.key === "objective" || change.key === "workflow_policy" ? "material-version-change-values content" : "material-version-change-values"}><del>{change.before || "（空）"}</del><ins>{change.after || "（空）"}</ins></span></article>)}</section> : null}
              <RoomVersionSetDiff label="能力包变化" change={versionDiff.capabilityPacks} />
              <RoomVersionSetDiff label="派生能力变化" change={versionDiff.capabilities} />
              <p className="material-version-readonly-note"><ShieldCheck size={13} />冻结快照仅供审计，不会恢复或覆盖当前房间设置。</p>
            </div> : null}
          </> : versionBusy ? <p className="material-version-empty">正在读取版本索引…</p> : <p className="material-version-empty">暂无可对比的设置版本。</p>}
          {versionError ? <span className="material-version-error" role="alert"><AlertTriangle size={13} />{versionError}</span> : null}
        </section>
        {frozenRound ? <div className="room-settings-freeze-note"><ShieldCheck size={15} /><span><strong>当前轮次不会漂移</strong><small>进行中或已暂停轮次继续使用检查点冻结的流程、能力包和证据；本次修改从下一轮开始生效。</small></span></div> : null}
        {error ? <p className="dialog-error" role="alert">{error}</p> : null}
        <footer><button type="button" className="secondary" onClick={onClose} disabled={busy}>取消</button><button className="primary" type="submit" disabled={busy || packSelectionBlocked || stockScopeBlocked}>{busy ? "正在保存…" : "保存房间设置"}</button></footer>
      </form>
    </div>
  );
}
