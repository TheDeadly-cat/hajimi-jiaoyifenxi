import { AlertTriangle, Archive, ExternalLink, FileText, GitCompareArrows, Globe2, LoaderCircle, NotebookPen, RefreshCcw, ShieldCheck, Upload, X } from "lucide-react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import {
  buildOfficialAttestationConfirmation,
  confirmedOfficialAttestationDialogState,
  MAX_MATERIAL_FILE_BYTES,
  materialPromptQuarantine,
  officialAttestationPreviewView,
} from "../materials";
import {
  buildMaterialVersionDiff,
  formatMaterialVersionTime,
  materialVersionSnapshot,
} from "../materialVersionDiff";
import { applyMemberTemplate, groupMemberTemplates } from "../memberTemplates";
import {
  normalizedProviderId,
  providerIsAvailable,
  UNASSIGNED_PROVIDER_ID,
} from "../providerRouting";
import { roomCreationCapabilityPackIds } from "../roomCreationDefaults";
import {
  filterNewPackBindings,
  packSelectionAvailability,
  pluginLifecycleCatalogView,
  pluginLifecycleRuntimeReason,
  pluginLifecycleStateLabel,
} from "../pluginLifecycle";
import { templateRosterPreview } from "../templateRosterPreview";
import {
  STOCK_RESEARCH_PACK_ID,
  stockRoomFormSubmission,
  stockRoomScopeInputState,
} from "../stockResearch";
import { useModalFocus } from "../useModalFocus";
import {
  capabilitiesForPackSelection,
  hasRoomCapability,
  ROOM_CAPABILITIES,
  roomDomainCapabilityLabels,
  splitCapabilityPacks,
} from "../roomCapabilities";
import { collectCapabilityOptions, normalizeWorkflowPolicy, stageLabel } from "../workflowPolicy";

function TemplateRosterPreview({ template }) {
  const roster = templateRosterPreview(template);
  if (!roster.available) {
    return (
      <section className="template-roster-preview unavailable" aria-label="模板阵容预览不可用">
        <div className="template-roster-heading">
          <strong>模板阵容</strong>
          <span>阵容预览不可用</span>
        </div>
        <p>当前服务未提供成员数量与阵容明细；创建房间不会因此报错。</p>
      </section>
    );
  }

  return (
    <section className="template-roster-preview" aria-label={`模板阵容，共 ${roster.count} 位成员`}>
      <div className="template-roster-heading">
        <strong>模板阵容</strong>
        <span>{roster.count} 位成员</span>
        {roster.members.length ? <small>点击成员查看职责与边界</small> : null}
      </div>
      {roster.previewAvailable && roster.members.length ? (
        <div className="template-roster-list">
          {roster.members.map((member) => (
            <details className="template-roster-member" key={member.key}>
              <summary>
                <span className="template-roster-avatar" style={{ background: member.avatarColor }}>{member.name.slice(0, 1)}</span>
                <span className="template-roster-identity">
                  <strong>{member.name}</strong>
                  <small>{member.identity}</small>
                </span>
                {member.workflowStage ? <em>{stageLabel(member.workflowStage)}</em> : null}
              </summary>
              <div className="template-roster-member-detail">
                <dl>
                  <div><dt>核心职责</dt><dd>{member.responsibilities || "未说明"}</dd></div>
                  <div><dt>行为边界</dt><dd>{member.boundaries || "未说明"}</dd></div>
                </dl>
                {(member.stance || member.capabilities.length) ? (
                  <div className="template-roster-tags">
                    {member.stance ? <span>立场 · {member.stance}</span> : null}
                    {member.capabilities.map((capability) => <span key={capability}>{capability}</span>)}
                  </div>
                ) : null}
              </div>
            </details>
          ))}
          {roster.partial ? <p className="template-roster-partial">当前预览 {roster.members.length} 位，共 {roster.count} 位；其余成员将在创建后显示。</p> : null}
        </div>
      ) : roster.previewAvailable ? (
        <p className="template-roster-empty">该模板尚无预设成员。</p>
      ) : (
        <p className="template-roster-empty">已记录成员数量，但阵容明细不可用。</p>
      )}
    </section>
  );
}

export function CreateRoomDialog({ open, onClose, onSubmit, restoreFocusRef, templates = [], capabilityPacks = [], pluginLifecycle = null }) {
  const defaultTemplateId = templates[0]?.id || "open_collaboration";
  const defaultTemplateCategory = templates[0]?.category || "通用共创";
  const defaultCreationPackIds = roomCreationCapabilityPackIds(templates[0]);
  const lifecycleView = useMemo(
    () => pluginLifecycleCatalogView(pluginLifecycle),
    [pluginLifecycle],
  );
  const defaultPackSelection = useMemo(
    () => filterNewPackBindings(defaultCreationPackIds, lifecycleView),
    [defaultCreationPackIds.join("|"), lifecycleView],
  );
  const defaultCreationPackSignature = `${defaultCreationPackIds.join("|")}:${lifecycleView.viewSha256}`;
  const [form, setForm] = useState({ title: "", objective: "", category: "通用共创", template_id: defaultTemplateId, capability_pack_ids: [], stock_room_scope_input: "" });
  const initializedForOpen = useRef(false);
  const dialogRef = useRef(null);
  const titleInputRef = useRef(null);
  useModalFocus({
    open: open && initializedForOpen.current,
    containerRef: dialogRef,
    initialFocusRef: titleInputRef,
    restoreFallbackRef: restoreFocusRef,
    onClose,
  });
  useLayoutEffect(() => {
    if (!open) {
      initializedForOpen.current = false;
      return;
    }
    if (initializedForOpen.current) return;
    initializedForOpen.current = true;
    setForm({
      title: "",
      objective: "",
      category: defaultTemplateCategory,
      template_id: defaultTemplateId,
      capability_pack_ids: [...defaultPackSelection.allowed],
      stock_room_scope_input: "",
    });
  }, [defaultCreationPackSignature, defaultPackSelection.allowed, defaultTemplateCategory, defaultTemplateId, open]);
  const selectedTemplate = useMemo(
    () => templates.find((template) => template.id === form.template_id),
    [form.template_id, templates],
  );
  const selectedWorkflowPolicy = selectedTemplate?.workflow_policy
    ? normalizeWorkflowPolicy(selectedTemplate.workflow_policy)
    : null;
  const selectedCapabilities = capabilitiesForPackSelection(form.capability_pack_ids, capabilityPacks);
  const selectedCapabilityLabels = roomDomainCapabilityLabels({ capabilities: selectedCapabilities });
  const { coreProtocols, optionalDomainPacks } = useMemo(
    () => splitCapabilityPacks(capabilityPacks),
    [capabilityPacks],
  );
  const selectTemplate = (templateId) => {
    const template = templates.find((item) => item.id === templateId);
    const selection = filterNewPackBindings(roomCreationCapabilityPackIds(template), lifecycleView);
    setForm((current) => ({
      ...current,
      template_id: templateId,
      category: template?.category || "通用共创",
      capability_pack_ids: selection.allowed,
      stock_room_scope_input: "",
    }));
  };
  const toggleCapabilityPack = (packId) => {
    const selected = form.capability_pack_ids.includes(packId);
    if (!packSelectionAvailability(lifecycleView, packId, { selected }).canToggle) return;
    setForm((current) => ({
      ...current,
      capability_pack_ids: current.capability_pack_ids.includes(packId)
        ? current.capability_pack_ids.filter((item) => item !== packId)
        : [...current.capability_pack_ids, packId],
      ...(packId === STOCK_RESEARCH_PACK_ID ? { stock_room_scope_input: "" } : {}),
    }));
  };
  const selectedTemplatePackSelection = filterNewPackBindings(
    roomCreationCapabilityPackIds(selectedTemplate),
    lifecycleView,
  );
  const requiredCoreUnavailable = coreProtocols.some((pack) => (
    packSelectionAvailability(lifecycleView, pack.id).lifecycle?.newBindingsAllowed !== true
  ));
  const stockPackSelected = form.capability_pack_ids.includes(STOCK_RESEARCH_PACK_ID);
  const stockScopeState = stockRoomScopeInputState(form.stock_room_scope_input, {
    requireNonempty: stockPackSelected,
  });
  const stockScopeBlocked = stockPackSelected && !stockScopeState.valid;
  const lifecycleCreationBlocked = !lifecycleView.integrityOk || requiredCoreUnavailable;
  const creationBlocked = lifecycleCreationBlocked || stockScopeBlocked;
  const creationStatus = lifecycleCreationBlocked
    ? "创建被阻断：能力包生命周期状态无法安全确认。"
    : stockScopeBlocked
      ? "创建被阻断：请修正显式股票池；不会自动补全或扩展标的。"
      : "提交时仍会校验必填字段；全部能力仅用于只读研究，不构成执行授权。";
  const submitCreate = (event) => {
    event.preventDefault();
    if (creationBlocked) return;
    onSubmit(stockRoomFormSubmission(form));
  };
  if (!open || !initializedForOpen.current) return null;
  return (
    <div className="dialog-backdrop create-room-dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <form ref={dialogRef} className="dialog create-room-dialog" role="dialog" aria-modal="true" aria-label="新建群聊房间" aria-describedby="create-room-submit-status" onSubmit={submitCreate} onMouseDown={(event) => event.stopPropagation()}>
        <header><strong>新建群聊房间</strong><button type="button" className="icon-button" aria-label="关闭新建房间" onClick={onClose}><X size={18} /></button></header>
        <label>房间名称<input ref={titleInputRef} required value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} placeholder="例如：AI 芯片产业研究室" /></label>
        <label>长期目标<textarea required value={form.objective} onChange={(event) => setForm({ ...form, objective: event.target.value })} placeholder="这个小群聊要持续解决什么问题？" /></label>
        <label>房间模板<select value={form.template_id} onChange={(event) => selectTemplate(event.target.value)}>
          {templates.map((template) => <option key={template.id} value={template.id}>{template.category} · {template.name}</option>)}
        </select></label>
        <label>归属分类<input required value={form.category} onChange={(event) => setForm({ ...form, category: event.target.value })} placeholder="例如：交易研究 / 美股" /></label>
        <p className="field-help compact-help">使用“/”创建子类。左侧会把每个小群聊归入对应的大类。</p>
        {selectedTemplate ? <p className="field-help">{selectedTemplate.description} 创建后仍可增删成员、修改全部身份和调整讨论流程。</p> : null}
        {selectedTemplate ? <TemplateRosterPreview template={selectedTemplate} /> : null}
        {coreProtocols.length ? <fieldset className="capability-pack-picker core-protocol-picker">
          <legend>群聊内核协议（始终启用）</legend>
          <p>所有新正式轮都会冻结并核验 AI 相互回应链；旧轮与暂停恢复严格沿用原协议。</p>
          {coreProtocols.map((pack) => (
            <div className="capability-pack-card selected core-protocol" key={pack.id}>
              <ShieldCheck size={14} aria-hidden="true" />
              <span><strong>{pack.name}</strong><small>{pack.description}</small>{pack.discussion_protocol?.title ? <small>协议：{pack.discussion_protocol.title}</small> : null}</span>
              <em>{packSelectionAvailability(lifecycleView, pack.id).lifecycle?.newBindingsAllowed === true ? "始终启用" : "状态不可用"}</em>
            </div>
          ))}
        </fieldset> : null}
        {optionalDomainPacks.length ? <fieldset className="capability-pack-picker">
          <legend>领域能力包（可选）</legend>
          <p>能力包只增加资料与模拟研究工具；真实下单能力始终关闭。</p>
          {optionalDomainPacks.map((pack) => {
            const selected = form.capability_pack_ids.includes(pack.id);
            const availability = packSelectionAvailability(lifecycleView, pack.id, { selected });
            const lifecycle = availability.lifecycle;
            return <label className={["capability-pack-card", selected ? "selected" : "", lifecycle?.runtimeState || "lifecycle-unverified"].filter(Boolean).join(" ")} key={pack.id} title={!availability.canToggle ? availability.reason : ""}>
              <input type="checkbox" checked={selected} disabled={!availability.canToggle} onChange={() => toggleCapabilityPack(pack.id)} />
              <span><strong>{pack.name}</strong><small>{pack.description}</small>{pack.discussion_protocol?.title ? <small>协议：{pack.discussion_protocol.title}</small> : null}{lifecycle && !lifecycle.runtimeAvailable ? <small className="capability-pack-lifecycle-reason">{pluginLifecycleRuntimeReason(lifecycle)}</small> : null}</span>
              <em>{lifecycle ? pluginLifecycleStateLabel(lifecycle) : "状态未验证"}</em>
            </label>;
          })}
        </fieldset> : null}
        {stockPackSelected ? <label className="stock-room-scope-field">
          显式股票池（每行一个 MARKET:TICKER）
          <textarea
            required
            aria-invalid={!stockScopeState.valid}
            aria-describedby={stockScopeState.valid
              ? "create-stock-room-scope-help"
              : "create-stock-room-scope-help create-stock-room-scope-error"}
            value={form.stock_room_scope_input}
            onChange={(event) => setForm({ ...form, stock_room_scope_input: event.target.value })}
            placeholder={"US:AAPL\nUS:MSFT"}
          />
          <small id="create-stock-room-scope-help">保存时规范化并排序为 stock_room_scope_v1；只绑定你明确输入的标的，不自动发现或扩展股票。</small>
          {!stockScopeState.valid ? <em id="create-stock-room-scope-error" className="stock-room-scope-error" role="alert">{stockScopeState.error}</em> : null}
        </label> : null}
        {selectedTemplatePackSelection.blocked.length ? <p className="field-help plugin-lifecycle-selection-block" role="alert">模板中的以下能力包当前不可建立新绑定，已停止自动选择：{selectedTemplatePackSelection.blocked.join("、")}。</p> : null}
        {lifecycleCreationBlocked ? <p className="field-help plugin-lifecycle-selection-block" role="alert">能力包生命周期状态无法安全确认，暂不能创建新房间。现有历史记录不受影响。</p> : null}
        {selectedTemplate ? <div className="template-capability-preview" aria-label="模板能力">
          <strong>领域能力</strong>
          <span>{selectedCapabilityLabels.map((label) => <em key={label}>{label}</em>)}</span>
        </div> : null}
        {selectedWorkflowPolicy ? (
          <div className="template-workflow-preview">
            <strong>默认讨论流程</strong>
            <span>{selectedWorkflowPolicy.stage_order.map(stageLabel).join(" → ")}</span>
            <small>至少 {selectedWorkflowPolicy.minimum_successful_members} 位不同成员 · 每人最多 {selectedWorkflowPolicy.max_turns_per_member} 次 · 追加追问 {selectedWorkflowPolicy.follow_up_budget} 次</small>
          </div>
        ) : null}
        <footer className="create-room-footer">
          <p id="create-room-submit-status" className={creationBlocked ? "create-room-submit-status blocked" : "create-room-submit-status"} role="status" aria-live="polite" aria-atomic="true">{creationStatus}</p>
          <span className="create-room-actions"><button type="button" className="secondary" onClick={onClose}>取消</button><button className="primary" type="submit" disabled={creationBlocked}>创建房间</button></span>
        </footer>
      </form>
    </div>
  );
}

export function MemberDialog({ member, room, open, onClose, onSubmit, onDelete, archiveDisabled = false, providers = [], memberTemplates = [] }) {
  const [form, setForm] = useState(null);
  const [selectedTemplateId, setSelectedTemplateId] = useState("");
  const [initializedFormKey, setInitializedFormKey] = useState("");
  const [saving, setSaving] = useState(false);
  const initializedMemberKey = useRef("");
  const dialogRef = useRef(null);
  const closeButtonRef = useRef(null);
  const memberInitializationKey = `${room?.id || ""}:${member?.id || "new"}:${member?.version || 0}`;
  useLayoutEffect(() => {
    if (!open || !member) {
      initializedMemberKey.current = "";
      setInitializedFormKey("");
      return;
    }
    if (initializedMemberKey.current === memberInitializationKey) return;
    initializedMemberKey.current = memberInitializationKey;
    const preferredProviderIds = ["deepseek", "doubao", "glm"];
    const preferredProvider = preferredProviderIds
      .map((providerId) => providers.find(
        (provider) => normalizedProviderId(provider.id) === providerId
          && providerIsAvailable(provider),
      ))
      .find(Boolean)
      || providers.find(providerIsAvailable)
      || preferredProviderIds
        .map((providerId) => providers.find(
          (provider) => normalizedProviderId(provider.id) === providerId
            && provider.policy_disabled !== true,
        ))
        .find(Boolean)
      || providers.find((provider) => provider.policy_disabled !== true);
    const nextForm = {
      enabled: true,
      name: "",
      identity: "",
      responsibilities: "",
      boundaries: "",
      instructions: "",
      stance: "neutral",
      workflow_stage: "flexible",
      provider: preferredProvider?.id || UNASSIGNED_PROVIDER_ID,
      model: "",
      ...member,
    };
    nextForm.provider = normalizedProviderId(nextForm.provider);
    nextForm.capabilities = Array.isArray(member.capabilities) ? [...member.capabilities] : [];
    setForm(nextForm);
    setSelectedTemplateId("");
    setSaving(false);
    setInitializedFormKey(memberInitializationKey);
  }, [member, memberInitializationKey, open, providers]);
  const memberDialogSurfaceOpen = Boolean(
    open && form && initializedFormKey === memberInitializationKey,
  );
  useModalFocus({
    open: memberDialogSurfaceOpen,
    containerRef: dialogRef,
    initialFocusRef: closeButtonRef,
    onClose: saving ? null : onClose,
  });
  useEffect(() => {
    if (memberDialogSurfaceOpen && saving) dialogRef.current?.focus({ preventScroll: true });
  }, [memberDialogSurfaceOpen, saving]);
  if (!memberDialogSurfaceOpen) return null;
  const isNew = !form.id;
  const selectedProviderId = normalizedProviderId(form.provider);
  const knownProviderIds = new Set(
    providers.map((provider) => normalizedProviderId(provider.id)),
  );
  const providerOptions = knownProviderIds.has(selectedProviderId)
    ? providers
    : [
        {
          id: selectedProviderId,
          name: selectedProviderId === UNASSIGNED_PROVIDER_ID
            ? "未分配执行器"
            : `未知执行器：${selectedProviderId}`,
          configured: false,
          policy_disabled: selectedProviderId === UNASSIGNED_PROVIDER_ID,
        },
        ...providers,
      ];
  const providerAssigned = selectedProviderId !== UNASSIGNED_PROVIDER_ID;
  const workflowStages = [...new Set([
    ...(room?.workflow_policy?.stage_order || []),
    "flexible",
    form.workflow_stage || "flexible",
  ])].filter((stage) => stage && stage !== "follow_up");
  const capabilityOptions = collectCapabilityOptions(
    [{ capabilities: form.capabilities || [] }],
    room?.workflow_policy,
  );
  const memberTemplateGroups = groupMemberTemplates(memberTemplates);
  const isExplicitModerator = !isNew
    && Boolean(room?.moderator_member_id)
    && room.moderator_member_id === member?.id;
  const selectMemberTemplate = (templateId) => {
    setSelectedTemplateId(templateId);
    const template = memberTemplates.find((item) => item.id === templateId);
    if (template) setForm((current) => applyMemberTemplate(current, template));
  };
  const toggleCapability = (capability) => {
    const selected = form.capabilities || [];
    setForm({
      ...form,
      capabilities: selected.includes(capability)
        ? selected.filter((item) => item !== capability)
        : [...selected, capability],
    });
  };
  const archiveMember = () => {
    if (!isNew && window.confirm(`确定归档「${form.name}」吗？历史发言和身份版本会保留，之后可以恢复。`)) onDelete(form);
  };
  const closeMemberDialog = () => {
    if (!saving) onClose();
  };
  const submitMember = async (event) => {
    event.preventDefault();
    if (saving) return;
    setSaving(true);
    try {
      await onSubmit(form);
    } finally {
      setSaving(false);
    }
  };
  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) closeMemberDialog(); }}>
      <form ref={dialogRef} className="dialog member-dialog" role="dialog" aria-modal="true" aria-label={isNew ? "添加 AI 成员" : "编辑 AI 身份"} aria-busy={saving} tabIndex={-1} onSubmit={submitMember} onMouseDown={(event) => event.stopPropagation()}>
        <header>
          <span><strong>{isNew ? "添加 AI 成员" : "编辑 AI 身份"}</strong>{!isNew && <small className="version-tag">身份版本 v{form.version || 1}</small>}</span>
          <button ref={closeButtonRef} type="button" className="icon-button" aria-label="关闭成员设置" disabled={saving} onClick={closeMemberDialog}><X size={18} /></button>
        </header>
        <label className="member-template-picker">身份模板（可选）
          <select value={selectedTemplateId} onChange={(event) => selectMemberTemplate(event.target.value)}>
            <option value="">不套用模板，继续当前内容</option>
            {memberTemplateGroups.map((group) => (
              <optgroup key={group.label} label={group.label}>
                {group.items.map((template) => (
                  <option key={template.id} value={template.id}>{template.name} · {template.identity}</option>
                ))}
              </optgroup>
            ))}
          </select>
          <small>模板只填入身份、职责、边界、阶段与能力标签；不会改变当前 Provider、模型、启用状态或历史版本。</small>
        </label>
        <div className="form-grid">
          <label>显示名<input required value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="例如：行业分析师" /></label>
          <label>身份定位<input required value={form.identity} onChange={(event) => setForm({ ...form, identity: event.target.value })} placeholder="负责什么专业判断" /></label>
        </div>
        <label>核心职责<textarea required value={form.responsibilities} onChange={(event) => setForm({ ...form, responsibilities: event.target.value })} placeholder="这个 AI 必须完成哪些工作？" /></label>
        <label>行为边界<textarea required value={form.boundaries} onChange={(event) => setForm({ ...form, boundaries: event.target.value })} placeholder="哪些事情不能做，哪些结论必须保留给用户？" /></label>
        <label>补充发言规则<textarea className="large" value={form.instructions} onChange={(event) => setForm({ ...form, instructions: event.target.value })} placeholder="语气、证据格式、必须回应的问题等" /></label>
        <div className="form-grid">
          <label>研究立场<input value={form.stance} onChange={(event) => setForm({ ...form, stance: event.target.value })} placeholder="例如 bull、bear、risk" /></label>
          <label>流程阶段<select value={form.workflow_stage || "flexible"} onChange={(event) => setForm({ ...form, workflow_stage: event.target.value })}>
            {workflowStages.map((stage) => <option value={stage} key={stage}>{stageLabel(stage)}</option>)}
          </select></label>
        </div>
        <fieldset className="member-capability-fieldset">
          <legend>专业能力标签</legend>
          <p>主持人会用这些标签判断谁最适合补齐证据、反方意见、方案或风控要求，可多选。</p>
          <div className="member-capability-list">
            {capabilityOptions.map((option) => {
              const selected = (form.capabilities || []).includes(option.id);
              return (
                <label className={selected ? "member-capability-chip selected" : "member-capability-chip"} key={option.id}>
                  <input type="checkbox" checked={selected} onChange={() => toggleCapability(option.id)} />
                  {option.label}
                </label>
              );
            })}
          </div>
        </fieldset>
        <div className="form-grid">
          <label>模型执行器<select required value={selectedProviderId} onChange={(event) => setForm({ ...form, provider: event.target.value, model: "" })}>
            {providerOptions.map((provider) => (
              <option
                disabled={provider.policy_disabled === true}
                key={provider.id}
                value={normalizedProviderId(provider.id)}
              >
                {provider.name || provider.id}
                {provider.policy_disabled === true
                  ? "（策略禁用）"
                  : provider.configured === false
                    ? "（未配置）"
                    : ""}
              </option>
            ))}
          </select></label>
          <label>模型<input value={form.model || ""} onChange={(event) => setForm({ ...form, model: event.target.value })} placeholder={`留空使用默认：${providers.find((provider) => normalizedProviderId(provider.id) === selectedProviderId)?.model || "由执行器决定"}`} /></label>
        </div>
        <label className="checkbox-line" title={isExplicitModerator ? "请先在房间设置中改派主持人，再暂停这名成员。" : ""}>
          <input
            type="checkbox"
            checked={Boolean(form.enabled)}
            disabled={isExplicitModerator && Boolean(form.enabled)}
            onChange={(event) => setForm({ ...form, enabled: event.target.checked })}
          />
          参与后续讨论
        </label>
        <p className="field-help">
          身份、职责、边界、立场、阶段和能力标签从该成员下一次普通发言开始生效；
          本轮一经确认，Provider 与模型继续使用本轮批准路由，新路由从下一轮生效。历史消息保留发言当时的身份、版本与执行路由。
          {isExplicitModerator
            ? " 这名成员是房间显式主持人：隐藏调度在每轮开始时冻结其身份版本和模型；如需暂停，请先在房间设置中改派主持人。"
            : " 房间讨论流程和隐藏主持路由按轮次冻结。"}
        </p>
        <footer className="member-dialog-footer">
          {!isNew ? <button type="button" className="danger-text" disabled={archiveDisabled || saving} title={archiveDisabled ? "当前轮次运行或暂停中，结束后才能归档成员" : "保留全部历史并移出活动成员列表"} onClick={archiveMember}><Archive size={15} />归档成员</button> : <span />}
          <span><button type="button" className="secondary" disabled={saving} onClick={closeMemberDialog}>取消</button><button className="primary" type="submit" disabled={saving || !providerAssigned}>{saving ? "保存中…" : isNew ? "添加成员" : "保存身份"}</button></span>
        </footer>
      </form>
    </div>
  );
}

function materialMode(material) {
  if (material?.kind === "url") return "url";
  if (material?.kind === "file_excerpt") return "file";
  return "note";
}

const evidenceSourceTypes = [
  ["company_ir", "公司公告 / IR"],
  ["regulatory_filing", "监管披露"],
  ["reputable_media", "主流媒体"],
  ["analyst_research", "分析师研究"],
  ["social_media", "社交媒体"],
  ["internal_note", "内部研究笔记"],
  ["other", "其他 / 未核验"],
];

const evidenceEventTypes = [
  ["earnings", "财报"], ["guidance", "指引"], ["product", "产品 / 技术"],
  ["supply_demand", "供需 / 价格"], ["capital_allocation", "资本配置"],
  ["legal_regulatory", "法律 / 监管"], ["management", "管理层"],
  ["macro", "宏观"], ["market_sentiment", "市场情绪"], ["other", "其他"],
];

const storageSymbols = ["US.MU", "US.SNDK", "US.WDC", "US.STX"];

const materialRiskLabels = {
  instruction_override: "覆盖系统或成员指令",
  secret_exfiltration: "索取或泄露秘密",
  tool_execution: "要求调用工具",
  financial_execution: "要求执行资金动作",
};

function MaterialChangeValues({ before, after, content = false }) {
  return <span className={content ? "material-version-change-values content" : "material-version-change-values"}>
    <del>{before || "（空）"}</del>
    <ins>{after || "（空）"}</ins>
  </span>;
}

function MaterialSetDiff({ title, diff, label = (value) => value }) {
  if (!diff.changed) return null;
  return <section className="material-version-set-diff">
    <strong>{title}</strong>
    <div>
      {diff.added.map((value) => <em className="added" key={`added:${value}`}>+ {label(value)}</em>)}
      {diff.removed.map((value) => <em className="removed" key={`removed:${value}`}>− {label(value)}</em>)}
    </div>
  </section>;
}

function MaterialVersionCard({ record, label }) {
  const snapshot = materialVersionSnapshot(record);
  return <article className="material-version-card">
    <span><strong>{label} · v{snapshot.version || "?"}</strong><small>{formatMaterialVersionTime(snapshot.changed_at)}</small></span>
    <b>{snapshot.title || "未命名资料"}</b>
    <small>{snapshot.kind || "note"} · {String(snapshot.content || "").length.toLocaleString()} 字符</small>
  </article>;
}

function MaterialVersionDiffView({ left, right }) {
  const diff = useMemo(() => buildMaterialVersionDiff(left, right), [left, right]);
  const fieldChanges = diff.fieldChanges;
  return <div className="material-version-diff" aria-label={`资料 v${left.version} 与 v${right.version} 对比`}>
    <div className="material-version-cards">
      <MaterialVersionCard record={left} label="基准" />
      <MaterialVersionCard record={right} label="对比" />
    </div>
    {!diff.changed ? <p className="material-version-empty">两个冻结版本的资料内容与来源元数据完全一致。</p> : null}
    {fieldChanges.length ? <section className="material-version-field-diff">
      <strong>字段与来源元数据变化</strong>
      {fieldChanges.map((change) => <article className={change.key === "content" ? "content-change" : ""} key={change.key}>
        <span>{change.label}</span>
        <MaterialChangeValues before={change.before} after={change.after} content={change.key === "content"} />
      </article>)}
    </section> : null}
    <MaterialSetDiff title="关联标的变化" diff={diff.symbols} />
    <MaterialSetDiff title="风险标签变化" diff={diff.riskFlags} label={(value) => materialRiskLabels[value] || value} />
    <p className="material-version-readonly-note"><ShieldCheck size={13} />这是冻结快照的只读比较；不会恢复、覆盖或修改当前资料。</p>
  </div>;
}

function OfficialAttestationPreview({ attestation, material, supplement, onClose, onConfirm, busy, error, dialogRef, closeButtonRef }) {
  const view = officialAttestationPreviewView(attestation, material, supplement);
  const confirmPayload = attestation?.confirm_payload || {};
  const confirmation = attestation?.integrity_ready === true && attestation?.state === "staged"
    ? buildOfficialAttestationConfirmation(attestation)
    : null;
  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy) onClose();
    }}>
      <form
        ref={dialogRef}
        className="dialog material-dialog official-attestation-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="确认官方文件补证"
        aria-busy={busy}
        tabIndex={-1}
        onSubmit={(event) => {
          event.preventDefault();
          if (!busy) onConfirm(confirmation);
        }}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <span><strong>确认官方文件补证</strong><small className="version-tag">服务端暂存 · 尚未纳入就绪</small></span>
          <button ref={closeButtonRef} type="button" className="icon-button" aria-label="关闭官方补证预览" onClick={onClose} disabled={busy}><X size={18} /></button>
        </header>
        <section className="official-attestation-preview" aria-label="官方补证服务端核验预览">
          <div className="official-attestation-state"><ShieldCheck size={17} /><span><strong>等待你的第二次确认</strong><small>以下内容来自服务端暂存记录；前端不会自行推导或替换三项哈希。</small></span></div>
          <dl>
            <div><dt>公司 / 标的</dt><dd>{view.symbol}</dd></div>
            <div><dt>财政期间</dt><dd>{view.fiscalPeriod}</dd></div>
            <div><dt>材料类型</dt><dd>{view.materialKind}</dd></div>
            <div><dt>文件</dt><dd>{view.fileName}{Number.isFinite(view.sourceBytes) ? ` · ${(view.sourceBytes / 1024).toFixed(1)} KB` : ""}{view.contentType ? ` · ${view.contentType}` : ""}</dd></div>
            <div><dt>PDF 页数</dt><dd>{view.pageCount ? `${view.pageCount} 页` : "未提供 / 非 PDF"}</dd></div>
            <div><dt>内容截断</dt><dd>{view.truncated === true ? "是（不可确认）" : view.truncated === false ? "否（完整提取）" : "未提供"}</dd></div>
          </dl>
          <section className="official-attestation-url">
            <strong>完整官方来源 URL</strong>
            {view.officialUrl ? <a href={view.officialUrl} target="_blank" rel="noreferrer"><ExternalLink size={11} />{view.officialUrl}</a> : <span>服务端未返回官方 URL</span>}
          </section>
          <section className="official-attestation-hashes">
            <strong>服务端确认载荷中的三项哈希</strong>
            <label>原文件 SHA256<code>{String(confirmPayload.source_sha256 || "缺失")}</code></label>
            <label>提取内容 SHA256<code>{String(confirmPayload.content_sha256 || "缺失")}</code></label>
            <label>资料快照 SHA256<code>{String(confirmPayload.material_snapshot_sha256 || "缺失")}</code></label>
          </section>
          <section className="official-attestation-blockers">
            <strong>{view.accessCodeLabel}</strong>
            {view.accessCodes.length ? <ul>{view.accessCodes.map((code) => <li key={code}>{code}</li>)}</ul> : <span>未提供待匹配访问阻断码。</span>}
            <span>{view.accessCodeNote}</span>
          </section>
          <p>确认仅表示这份本机文件与上述服务端暂存信息一致；不代表系统已重新从远端下载文件，也不会开放账户、委托或下单能力。</p>
        </section>
        {error ? <div className="material-local-error">{error}</div> : null}
        {!confirmation ? <div className="material-local-error">
          {attestation?.integrity_ready !== true
            ? `服务端完整性检查未通过${Array.isArray(attestation?.integrity_issues) && attestation.integrity_issues.length ? `：${attestation.integrity_issues.join("、")}` : ""}；当前不能确认。`
            : "确认载荷缺少有效见证 ID、版本或三项 SHA256；为防止误认，当前不能确认。"}
        </div> : null}
        <footer className="material-dialog-footer">
          <button type="button" className="secondary" onClick={onClose} disabled={busy}>暂不确认</button>
          <button className="primary official-attestation-confirm" type="submit" disabled={busy || !confirmation}>
            {busy ? <LoaderCircle className="spin" size={15} /> : <ShieldCheck size={14} />}
            {busy ? "确认中…" : "确认三项哈希并重新核验"}
          </button>
        </footer>
      </form>
    </div>
  );
}

export function MaterialDialog({ material, room, open, onClose, onSubmit, onFetchUrl, onImportFile, onConfirmOfficialAttestation, versions = [], versionsLoading = false }) {
  const [form, setForm] = useState(null);
  const [initializedMaterial, setInitializedMaterial] = useState(null);
  const [mode, setMode] = useState("note");
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState("");
  const [baseVersion, setBaseVersion] = useState(0);
  const [targetVersion, setTargetVersion] = useState(0);
  const [versionPair, setVersionPair] = useState(null);
  const [versionPairStatus, setVersionPairStatus] = useState("idle");
  const [versionPairError, setVersionPairError] = useState("");
  const [versionPairReload, setVersionPairReload] = useState(0);
  const [officialAttestation, setOfficialAttestation] = useState(null);
  const initializedMaterialRef = useRef(null);
  const versionPairRequest = useRef(0);
  const materialActionRequest = useRef(0);
  const editorDialogRef = useRef(null);
  const editorCloseButtonRef = useRef(null);
  const attestationDialogRef = useRef(null);
  const attestationCloseButtonRef = useRef(null);
  const selectableVersions = useMemo(
    () => versions.filter((row) => Number(row?.version) > 0),
    [versions],
  );
  const versionSignature = selectableVersions.map((row) => Number(row.version)).join("|");
  useEffect(() => {
    materialActionRequest.current += 1;
  }, [material, open, room?.id]);
  useLayoutEffect(() => {
    if (!open || !material) {
      initializedMaterialRef.current = null;
      setInitializedMaterial(null);
      return;
    }
    if (initializedMaterialRef.current === material) return;
    initializedMaterialRef.current = material;
    setForm({
      title: "",
      kind: "note",
      source_url: "",
      content: "",
      ...material,
      metadata: {
        source_type: material.metadata?.source_type || "other",
        event_type: material.metadata?.event_type || "other",
        publisher: material.metadata?.publisher || "",
        published_at: material.metadata?.published_at || "",
        symbols: material.metadata?.symbols || [],
        ...(material.metadata || {}),
      },
    });
    setMode(materialMode(material));
    setFile(null);
    setLocalError("");
    setBusy(false);
    setOfficialAttestation(material.official_attestation || material._official_attestation || null);
    setInitializedMaterial(material);
  }, [material, open]);
  useEffect(() => {
    versionPairRequest.current += 1;
    setVersionPair(null);
    setVersionPairError("");
    setVersionPairStatus("idle");
    if (!open || !material?.id || !selectableVersions.length) {
      setBaseVersion(0);
      setTargetVersion(0);
      return;
    }
    const currentVersion = Number(material.version) || Number(selectableVersions[0]?.version) || 0;
    const target = selectableVersions.find((row) => Number(row.version) === currentVersion)?.version
      || selectableVersions[0]?.version
      || 0;
    const base = selectableVersions.find((row) => Number(row.version) < Number(target))?.version || target;
    setBaseVersion(Number(base));
    setTargetVersion(Number(target));
  }, [material?.id, material?.version, open, versionSignature]);
  useEffect(() => {
    if (!open || !room?.id || !material?.id || !baseVersion || !targetVersion) return undefined;
    const requestId = versionPairRequest.current + 1;
    versionPairRequest.current = requestId;
    setVersionPair(null);
    setVersionPairError("");
    setVersionPairStatus("loading");
    const baseRequest = api.materialVersion(room.id, material.id, baseVersion);
    const targetRequest = baseVersion === targetVersion
      ? baseRequest
      : api.materialVersion(room.id, material.id, targetVersion);
    Promise.all([baseRequest, targetRequest])
      .then(([baseData, targetData]) => {
        if (versionPairRequest.current !== requestId) return;
        setVersionPair({ left: baseData.material, right: targetData.material });
        setVersionPairStatus("ready");
      })
      .catch((requestError) => {
        if (versionPairRequest.current !== requestId) return;
        setVersionPairError(requestError.message);
        setVersionPairStatus("error");
      });
    return () => {
      if (versionPairRequest.current === requestId) versionPairRequest.current += 1;
    };
  }, [baseVersion, material?.id, open, room?.id, targetVersion, versionPairReload]);
  const materialSurfaceOpen = Boolean(open && form && initializedMaterial === material);
  const attestationSurfaceOpen = materialSurfaceOpen && Boolean(officialAttestation);
  const activeDialogRef = attestationSurfaceOpen ? attestationDialogRef : editorDialogRef;
  const activeCloseButtonRef = attestationSurfaceOpen ? attestationCloseButtonRef : editorCloseButtonRef;
  const finishClose = () => {
    materialActionRequest.current += 1;
    onClose();
  };
  const requestClose = () => {
    if (!busy) finishClose();
  };
  useModalFocus({
    open: materialSurfaceOpen,
    containerRef: activeDialogRef,
    initialFocusRef: activeCloseButtonRef,
    onClose: busy ? null : requestClose,
  });
  useEffect(() => {
    if (materialSurfaceOpen && busy) activeDialogRef.current?.focus({ preventScroll: true });
  }, [activeDialogRef, busy, materialSurfaceOpen]);
  if (!materialSurfaceOpen) return null;
  const isNew = !form.id;
  const metadata = form.metadata || {};
  const officialSupplement = form.official_supplement_v1 || null;
  const promptQuarantine = materialPromptQuarantine({ metadata });
  const updateMetadata = (patch) => setForm({ ...form, metadata: { ...metadata, ...patch } });
  const toggleSymbol = (symbol) => updateMetadata({
    symbols: (metadata.symbols || []).includes(symbol)
      ? (metadata.symbols || []).filter((item) => item !== symbol)
      : [...(metadata.symbols || []), symbol],
  });
  const run = async (action, onSuccess) => {
    if (busy) return null;
    const requestId = materialActionRequest.current + 1;
    materialActionRequest.current = requestId;
    setBusy(true);
    setLocalError("");
    try {
      const result = await action();
      if (materialActionRequest.current !== requestId) return result;
      if (onSuccess) await onSuccess(result);
      if (materialActionRequest.current === requestId) setBusy(false);
      return result;
    } catch (requestError) {
      if (materialActionRequest.current === requestId) {
        setLocalError(requestError.message);
        setBusy(false);
      }
      return null;
    }
  };
  const submitPrimary = () => {
    if (mode === "url") return run(() => onFetchUrl(form));
    if (mode === "file") return run(
      () => onImportFile(form, file),
      officialSupplement ? (result) => {
        const attestation = result?.official_attestation || result?.material?._official_attestation;
        if (!result?.material?.id || !attestation) {
          throw new Error("服务端未返回官方补证暂存预览；资料仍保持未确认");
        }
        setForm({ ...result.material, official_supplement_v1: officialSupplement });
        setOfficialAttestation(attestation);
        setFile(null);
      } : undefined,
    );
    return run(() => onSubmit({ ...form, kind: "note", source_url: "" }));
  };
  const saveManualRevision = () => run(() => onSubmit(form));
  const confirmOfficialAttestation = (confirmation) => {
    if (!confirmation || !form.id || !onConfirmOfficialAttestation) {
      setLocalError("官方补证确认信息不完整");
      return;
    }
    run(
      () => onConfirmOfficialAttestation(form.id, confirmation),
      (result) => {
        const nextState = confirmedOfficialAttestationDialogState(result);
        setForm(nextState.form);
        setOfficialAttestation(nextState.officialAttestation);
        if (nextState.shouldClose) finishClose();
      },
    );
  };
  if (officialAttestation) {
    return <OfficialAttestationPreview
      attestation={officialAttestation}
      material={form}
      supplement={officialSupplement}
      onClose={requestClose}
      onConfirm={confirmOfficialAttestation}
      busy={busy}
      error={localError}
      dialogRef={attestationDialogRef}
      closeButtonRef={attestationCloseButtonRef}
    />;
  }
  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy) requestClose();
    }}>
      <form
        ref={editorDialogRef}
        className="dialog material-dialog"
        role="dialog"
        aria-modal="true"
        aria-label={officialSupplement ? "上传官方文件补证" : isNew ? "添加共享资料" : "编辑共享资料"}
        aria-busy={busy}
        tabIndex={-1}
        onSubmit={(event) => {
          event.preventDefault();
          if (!busy) submitPrimary();
        }}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <span><strong>{officialSupplement ? "上传官方文件补证" : isNew ? "添加共享资料" : "编辑共享资料"}</strong>{!isNew ? <small className="version-tag">资料版本 v{form.version || 1}</small> : null}</span>
          <button ref={editorCloseButtonRef} type="button" className="icon-button" aria-label="关闭资料编辑" onClick={requestClose} disabled={busy}><X size={18} /></button>
        </header>
        {isNew && !officialSupplement ? <div className="material-mode-tabs" role="tablist" aria-label="资料来源方式">
          <button type="button" role="tab" aria-selected={mode === "note"} className={mode === "note" ? "active" : ""} onClick={() => setMode("note")}><NotebookPen size={15} />手工笔记</button>
          <button type="button" role="tab" aria-selected={mode === "url"} className={mode === "url" ? "active" : ""} onClick={() => setMode("url")}><Globe2 size={15} />抓取网页</button>
          <button type="button" role="tab" aria-selected={mode === "file"} className={mode === "file" ? "active" : ""} onClick={() => setMode("file")}><Upload size={15} />上传文件</button>
        </div> : null}
        {officialSupplement ? <section className="official-supplement-intro" aria-label="官方人工补证范围">
          <span><ShieldCheck size={16} /><strong>{officialSupplement.symbol.replace("US.", "")} · {officialSupplement.fiscal_period}</strong><small>{officialSupplement.material_kind}</small></span>
          <a href={officialSupplement.official_url} target="_blank" rel="noreferrer"><ExternalLink size={11} />{officialSupplement.official_url}</a>
          <p>请只上传从上述固定官方入口下载的原文件。首次提交只会暂存并生成核验预览，不会立即解除 readiness 阻断。</p>
        </section> : null}
        <label>资料标题<input required={mode === "note"} value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} placeholder={mode === "note" ? "例如：2026 Q2 财报要点" : "可选；留空时从来源自动识别"} /></label>
        {mode === "url" ? <>
          <label>公开网页链接<input required type="url" value={form.source_url || ""} onChange={(event) => setForm({ ...form, source_url: event.target.value })} placeholder="https://example.com/report" /></label>
          <div className="material-safety-note"><Globe2 size={16} /><span>只抓取公开的 HTTP/HTTPS 页面；本机、私网、带账号密码或非标准端口会被拒绝。网页中的指令只视为不可信文本证据。</span></div>
        </> : null}
        {mode === "file" ? <>
          <label className="material-file-picker">选择文件
            <input
              type="file"
              required={isNew}
              accept=".txt,.md,.markdown,.csv,.tsv,.json,.html,.htm,.xml,.docx,.pdf"
              onChange={(event) => { setFile(event.target.files?.[0] || null); setLocalError(""); }}
            />
            <span><Upload size={17} />{file ? `${file.name} · ${(file.size / 1024).toFixed(1)} KB` : `TXT、MD、CSV、JSON、HTML、XML、DOCX、PDF；最大 ${MAX_MATERIAL_FILE_BYTES / 1_000_000} MB`}</span>
          </label>
          <div className="material-safety-note"><FileText size={16} /><span>只保存提取后的文本、哈希和来源元数据，不保存原文件；PDF 需要安装 requirements.txt，扫描型 PDF 暂不做 OCR。</span></div>
          {officialSupplement ? <label className="official-supplement-confirmation">
            <input
              type="checkbox"
              required
              checked={officialSupplement.user_confirmed === true}
              onChange={(event) => setForm({
                ...form,
                official_supplement_v1: {
                  ...officialSupplement,
                  user_confirmed: event.target.checked,
                },
              })}
            />
            <span><strong>我确认所选文件由我从上方完整官方 URL 下载</strong><small>提交后还需核对服务端返回的文件信息和三项哈希，再进行第二次确认。</small></span>
          </label> : null}
        </> : null}
        <fieldset className="material-evidence-metadata">
          <legend>证据时间与对象</legend>
          <div>
            <label>来源类型<select disabled={Boolean(officialSupplement)} value={metadata.source_type || "other"} onChange={(event) => updateMetadata({ source_type: event.target.value })}>
              {evidenceSourceTypes.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select></label>
            <label>事件类型<select disabled={Boolean(officialSupplement)} value={metadata.event_type || "other"} onChange={(event) => updateMetadata({ event_type: event.target.value })}>
              {evidenceEventTypes.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select></label>
            <label>发布者<input value={metadata.publisher || ""} onChange={(event) => updateMetadata({ publisher: event.target.value })} placeholder="例如 Micron Investor Relations" /></label>
            <label>发布时间（含时区）<input value={metadata.published_at || ""} onChange={(event) => updateMetadata({ published_at: event.target.value })} placeholder="2026-07-19T08:30:00-04:00" /></label>
          </div>
          {hasRoomCapability(room, ROOM_CAPABILITIES.storageMarket) ? <div className="material-symbol-map">
            <small>关联标的（可多选）</small>
            <span>{storageSymbols.map((symbol) => <label key={symbol}>
              <input type="checkbox" disabled={Boolean(officialSupplement)} checked={(metadata.symbols || []).includes(symbol)} onChange={() => toggleSymbol(symbol)} />
              {symbol.replace("US.", "")}
            </label>)}</span>
          </div> : null}
          <p>公司公告和监管披露按一级来源处理；媒体与分析师研究为二级；社交媒体、内部笔记和未分类来源保持“未核验”。发布时间缺失时，AI 必须明确写成未知。</p>
        </fieldset>
        {(mode === "note" || !isNew) ? <label>可供 AI 使用的内容<textarea className="large" required value={form.content} onChange={(event) => setForm({ ...form, content: event.target.value })} placeholder="粘贴事实、摘要或需要共同核验的原文片段。" /></label> : null}
        {!isNew && Object.keys(metadata).length ? <div className="material-provenance">
          <strong>来源记录</strong>
          <span>{metadata.original_name || metadata.final_url || form.source_url || "手工资料"}</span>
          <small>{metadata.extraction_method || "manual"}{metadata.source_bytes ? ` · ${metadata.source_bytes.toLocaleString()} bytes` : ""}{metadata.truncated ? " · 内容已截断" : ""}</small>
          <small>{metadata.publisher || "发布者未知"} · {metadata.published_at || "发布时间未知"} · {(metadata.symbols || []).join("、") || "未映射标的"}</small>
          {metadata.source_sha256 ? <code title={metadata.source_sha256}>SHA256 {metadata.source_sha256.slice(0, 16)}…</code> : null}
        </div> : null}
        {!isNew && promptQuarantine.quarantined ? <div className="material-safety-note quarantined" role="status">
          <ShieldCheck size={16} /><span>检测到可能要求覆盖指令、泄露秘密、调用工具或执行资金动作的文本。原文仍保存在本机供你查看，但该版本不会发送给 AI、进入轮次证据或自动纪要。若需使用其中事实，请新建一份去除指令性内容的清洁摘要。标记：{promptQuarantine.labels.join("、")}</span>
        </div> : null}
        {!isNew ? <section className="material-version-history material-version-comparison">
          <span className="material-version-heading"><strong>版本历史与只读对比</strong><small>当前 v{form.version || 1}</small></span>
          {versionsLoading ? <span><LoaderCircle className="spin" size={13} />正在读取版本…</span> : null}
          {!versionsLoading && !selectableVersions.length ? <span>暂无版本记录</span> : null}
          {selectableVersions.length ? <>
            <div className="material-version-selectors">
              <label>基准版本<select value={baseVersion || ""} onChange={(event) => setBaseVersion(Number(event.target.value))}>
                {selectableVersions.map((item) => <option value={item.version} key={`base:${item.version}`}>v{item.version} · {formatMaterialVersionTime(item.changed_at)}</option>)}
              </select></label>
              <GitCompareArrows size={17} />
              <label>对比版本<select value={targetVersion || ""} onChange={(event) => setTargetVersion(Number(event.target.value))}>
                {selectableVersions.map((item) => <option value={item.version} key={`target:${item.version}`}>v{item.version} · {formatMaterialVersionTime(item.changed_at)}</option>)}
              </select></label>
            </div>
            <details className="material-version-index"><summary>查看 {selectableVersions.length} 条版本索引</summary>
              {selectableVersions.map((item) => <div key={item.version}>
                <b>v{item.version}{item.version === form.version ? " · 当前" : ""}</b>
                <span>{formatMaterialVersionTime(item.changed_at)}</span>
                <small>{item.extraction_method} · {Number(item.content_chars || 0).toLocaleString()} 字符{item.source_sha256 ? ` · ${item.source_sha256.slice(0, 10)}…` : ""}</small>
              </div>)}
            </details>
          </> : null}
          {versionPairStatus === "loading" ? <span><LoaderCircle className="spin" size={13} />正在读取两个精确版本…</span> : null}
          {versionPairError ? <span className="material-version-error" role="alert"><AlertTriangle size={13} />{versionPairError}<button type="button" className="secondary" onClick={() => setVersionPairReload((value) => value + 1)}>重试</button></span> : null}
          {versionPair ? <MaterialVersionDiffView left={versionPair.left} right={versionPair.right} /> : null}
        </section> : null}
        {localError ? <div className="material-local-error">{localError}</div> : null}
        <p className="field-help">抓取、替换或手工编辑都会生成新版本。历史消息仍保留引用时的资料版本，不会被新内容静默改写。</p>
        <footer className="material-dialog-footer">
          <button type="button" className="secondary" onClick={requestClose} disabled={busy}>取消</button>
          {!isNew && mode !== "note" ? <button type="button" className="secondary" onClick={saveManualRevision} disabled={busy}><NotebookPen size={14} />保存手工修订</button> : null}
          <button className={officialSupplement ? "primary official-supplement-stage" : "primary"} type="submit" disabled={busy || (mode === "file" && !file) || (officialSupplement && officialSupplement.user_confirmed !== true)}>
            {busy ? <LoaderCircle className="spin" size={15} /> : mode === "url" && !isNew ? <RefreshCcw size={14} /> : mode === "file" ? <Upload size={14} /> : null}
            {busy ? "处理中…" : officialSupplement ? "上传并生成核验预览" : mode === "url" ? (isNew ? "抓取并添加" : "重新抓取为新版本") : mode === "file" ? (isNew ? "解析并添加" : "替换文件为新版本") : (isNew ? "添加资料" : "保存新版本")}
          </button>
        </footer>
      </form>
    </div>
  );
}
