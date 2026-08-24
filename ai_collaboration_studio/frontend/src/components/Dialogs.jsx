import { AlertTriangle, Archive, ChevronDown, ExternalLink, FileText, GitCompareArrows, Globe2, LoaderCircle, NotebookPen, RefreshCcw, ShieldCheck, Upload, X } from "lucide-react";
import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { safeExternalUrl } from "../artifactEvidenceSources";
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
import "../styles/create-room-dialog-refinement.css";
import "../styles/create-room-review-summary.css";
import "../styles/create-room-capability-picker.css";
import "../styles/member-dialog-refinement.css";
import "../styles/material-dialog-refinement.css";

function createRoomListKey(...parts) {
  return JSON.stringify(parts.map((part) => String(part ?? "")));
}

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
  const [submitError, setSubmitError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [templateReviewExpanded, setTemplateReviewExpanded] = useState(false);
  const [capabilityPickerExpanded, setCapabilityPickerExpanded] = useState(false);
  const initializedForOpen = useRef(false);
  const submitRequestRef = useRef(0);
  const dialogRef = useRef(null);
  const titleInputRef = useRef(null);
  const dialogTitleId = useId();
  const templateReviewTitleId = useId();
  const templateReviewDescriptionId = useId();
  const templateReviewRegionId = useId();
  const creationReviewTitleId = useId();
  const capabilityPackListId = useId();
  const submitStatusId = useId();
  const stockScopeHelpId = useId();
  const stockScopeErrorId = useId();
  const requestClose = () => {
    if (!submitting) onClose?.();
  };
  useModalFocus({
    open: open && initializedForOpen.current,
    containerRef: dialogRef,
    initialFocusRef: titleInputRef,
    restoreFallbackRef: restoreFocusRef,
    onClose: submitting ? null : requestClose,
  });
  useEffect(() => () => {
    submitRequestRef.current += 1;
  }, []);
  useLayoutEffect(() => {
    if (!open) {
      submitRequestRef.current += 1;
      initializedForOpen.current = false;
      setSubmitting(false);
      setSubmitError("");
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
    setSubmitting(false);
    setSubmitError("");
  }, [defaultCreationPackSignature, defaultPackSelection.allowed, defaultTemplateCategory, defaultTemplateId, open]);
  useEffect(() => {
    if (open && initializedForOpen.current && submitting) {
      dialogRef.current?.focus({ preventScroll: true });
    }
  }, [open, submitting]);
  useEffect(() => {
    if (open) {
      setTemplateReviewExpanded(false);
      setCapabilityPickerExpanded(false);
    }
  }, [form.template_id, open]);
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
  const submitUnavailable = typeof onSubmit !== "function";
  const submitBlocked = creationBlocked || submitUnavailable || submitting;
  const creationStatus = submitError
    ? submitError
    : submitting
      ? "正在创建房间并等待服务端确认，请勿重复提交。"
      : submitUnavailable
        ? "创建入口不可用：当前视图未提供提交处理器。"
        : lifecycleCreationBlocked
    ? "创建被阻断：能力包生命周期状态无法安全确认。"
    : stockScopeBlocked
      ? "创建被阻断：请修正显式股票池；不会自动补全或扩展标的。"
      : "提交时仍会校验必填字段；全部能力仅用于只读研究，不构成执行授权。";
  const selectedRoster = selectedTemplate ? templateRosterPreview(selectedTemplate) : null;
  const requiredFieldsCount = [form.title, form.objective, form.category]
    .filter((value) => typeof value === "string" && value.trim()).length;
  const selectedOptionalPackCount = optionalDomainPacks
    .filter((pack) => form.capability_pack_ids.includes(pack.id)).length;
  const optionalPackPreviewLimit = 2;
  const selectedOptionalPackIds = new Set(
    optionalDomainPacks
      .filter((pack) => form.capability_pack_ids.includes(pack.id))
      .map((pack) => pack.id),
  );
  const unselectedPreviewSlots = Math.max(
    0,
    optionalPackPreviewLimit - selectedOptionalPackIds.size,
  );
  const unselectedPreviewIds = optionalDomainPacks
    .filter((pack) => !selectedOptionalPackIds.has(pack.id))
    .slice(0, unselectedPreviewSlots)
    .map((pack) => pack.id);
  const previewOptionalPackIds = new Set([
    ...selectedOptionalPackIds,
    ...unselectedPreviewIds,
  ]);
  const visibleOptionalDomainPacks = capabilityPickerExpanded
    ? optionalDomainPacks
    : optionalDomainPacks.filter((pack) => previewOptionalPackIds.has(pack.id));
  const hiddenOptionalPackCount = optionalDomainPacks.length - visibleOptionalDomainPacks.length;
  const workflowStageCount = selectedWorkflowPolicy?.stage_order?.length || 0;
  const creationReviewTone = submitError || creationBlocked || submitUnavailable
    ? "blocked"
    : requiredFieldsCount === 3
      ? "ready"
      : "draft";
  const creationReviewLabel = submitError
    ? "创建请求需要处理"
    : creationReviewTone === "blocked"
      ? "创建配置仍被阻断"
    : creationReviewTone === "ready"
      ? "创建配置已可提交"
      : "创建配置仍需补齐";
  const submitCreate = async (event) => {
    event.preventDefault();
    if (submitBlocked || typeof onSubmit !== "function") return;
    const requestId = submitRequestRef.current + 1;
    submitRequestRef.current = requestId;
    setSubmitting(true);
    setSubmitError("");
    try {
      const payload = stockRoomFormSubmission(form);
      await onSubmit(payload);
      if (submitRequestRef.current === requestId) setSubmitting(false);
    } catch (requestError) {
      if (submitRequestRef.current === requestId) {
        const message = typeof requestError?.message === "string" ? requestError.message.trim() : "";
        setSubmitError((message || "创建房间失败，请检查输入后重试。").slice(0, 1000));
        setSubmitting(false);
      }
    }
  };
  if (!open || !initializedForOpen.current) return null;
  return (
    <div className="dialog-backdrop create-room-dialog-backdrop create-room-dialog-backdrop-v2" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget) requestClose();
    }}>
      <form ref={dialogRef} className="dialog create-room-dialog create-room-dialog-v2" role="dialog" aria-modal="true" aria-labelledby={dialogTitleId} aria-describedby={submitStatusId} aria-busy={submitting} tabIndex={-1} onSubmit={submitCreate} onMouseDown={(event) => event.stopPropagation()}>
        <header className="create-room-heading"><span><small>NEW ROOM / REVIEW FIRST</small><h2 id={dialogTitleId}>新建群聊房间</h2><p>先定义目标与实施结构；创建房间不会替代后续正式轮启动确认。</p></span><button type="button" className="icon-button" aria-label="关闭新建房间" onClick={requestClose} disabled={submitting}><X size={18} aria-hidden="true" /></button></header>
        <label>房间名称<input ref={titleInputRef} required value={form.title} onChange={(event) => setForm((current) => ({ ...current, title: event.target.value }))} placeholder="例如：AI 芯片产业研究室" /></label>
        <label>长期目标<textarea required value={form.objective} onChange={(event) => setForm((current) => ({ ...current, objective: event.target.value }))} placeholder="这个小群聊要持续解决什么问题？" /></label>
        <label>房间模板<select value={form.template_id} onChange={(event) => selectTemplate(event.target.value)}>
          {templates.map((template) => <option key={createRoomListKey("room-template", template.id)} value={template.id}>{template.category} · {template.name}</option>)}
        </select></label>
        <label>归属分类<input required value={form.category} onChange={(event) => setForm((current) => ({ ...current, category: event.target.value }))} placeholder="例如：交易研究 / 美股" /></label>
        <p className="field-help compact-help">使用“/”创建子类。左侧会把每个小群聊归入对应的大类。</p>
        {selectedTemplate ? <section className="create-room-template-review" aria-labelledby={templateReviewTitleId}>
          <div className="create-room-template-review-control">
            <span>
              <small>IMPLEMENTATION PREVIEW / TEMPLATE</small>
              <h3 id={templateReviewTitleId}>{selectedTemplate.name}</h3>
              <p id={templateReviewDescriptionId}>{selectedTemplate.description} 创建后仍可增删成员、修改身份和调整讨论流程。</p>
            </span>
            <button
              aria-controls={templateReviewRegionId}
              aria-describedby={templateReviewDescriptionId}
              aria-expanded={templateReviewExpanded}
              onClick={() => setTemplateReviewExpanded((current) => !current)}
              type="button"
            >
              <span>{templateReviewExpanded ? "收起实施结构" : "查看实施结构"}</span>
              <ChevronDown aria-hidden="true" size={15} />
            </button>
          </div>
          <dl className="create-room-template-review-stats" aria-label="模板实施摘要">
            <div><dt>模板成员</dt><dd>{selectedRoster?.available ? `${selectedRoster.count} 位` : "创建后确认"}</dd></div>
            <div><dt>流程阶段</dt><dd>{workflowStageCount ? `${workflowStageCount} 段` : "未声明"}</dd></div>
            <div><dt>当前领域包</dt><dd>{selectedOptionalPackCount} 项</dd></div>
          </dl>
          <div
            aria-labelledby={templateReviewTitleId}
            className="create-room-template-review-detail"
            hidden={!templateReviewExpanded}
            id={templateReviewRegionId}
            role="region"
          >
            <TemplateRosterPreview template={selectedTemplate} />
            <div className="template-capability-preview" aria-label="模板能力">
              <strong>领域能力</strong>
              <span>{selectedCapabilityLabels.map((label) => <em key={createRoomListKey("capability-label", label)}>{label}</em>)}</span>
            </div>
            {selectedWorkflowPolicy ? (
              <div className="template-workflow-preview">
                <strong>默认讨论流程</strong>
                <span>{selectedWorkflowPolicy.stage_order.map(stageLabel).join(" → ")}</span>
                <small>至少 {selectedWorkflowPolicy.minimum_successful_members} 位不同成员 · 每人最多 {selectedWorkflowPolicy.max_turns_per_member} 次 · 追加追问 {selectedWorkflowPolicy.follow_up_budget} 次</small>
              </div>
            ) : null}
          </div>
        </section> : null}
        {coreProtocols.length ? <fieldset className="capability-pack-picker core-protocol-picker">
          <legend>群聊内核协议（始终启用）</legend>
          <p>所有新正式轮都会冻结并核验 AI 相互回应链；旧轮与暂停恢复严格沿用原协议。</p>
          {coreProtocols.map((pack) => (
            <div className="capability-pack-card selected core-protocol" key={createRoomListKey("core-protocol", pack.id, pack.pack_version, pack.manifest_sha256)}>
              <ShieldCheck size={14} aria-hidden="true" />
              <span><strong>{pack.name}</strong><small>{pack.description}</small>{pack.discussion_protocol?.title ? <small>协议：{pack.discussion_protocol.title}</small> : null}</span>
              <em>{packSelectionAvailability(lifecycleView, pack.id).lifecycle?.newBindingsAllowed === true ? "始终启用" : "状态不可用"}</em>
            </div>
          ))}
        </fieldset> : null}
        {optionalDomainPacks.length ? <fieldset
          className="capability-pack-picker create-room-domain-pack-picker"
          id={capabilityPackListId}
        >
          <legend>领域能力包（可选）</legend>
          <p>能力包只增加资料与模拟研究工具；真实下单能力始终关闭。</p>
          <div className="create-room-pack-catalog-summary">
            <span>
              <small>OPTIONAL RESEARCH PACKS</small>
              <strong>{selectedOptionalPackCount} 已选 / {optionalDomainPacks.length} 目录项</strong>
              <p>默认展示已选项并补足至 2 项；展开目录不会改变选择。</p>
            </span>
            {hiddenOptionalPackCount || capabilityPickerExpanded ? <button
              aria-controls={capabilityPackListId}
              aria-expanded={capabilityPickerExpanded}
              onClick={() => setCapabilityPickerExpanded((current) => !current)}
              type="button"
            >
              <span>{capabilityPickerExpanded ? "收起未选能力包" : `查看全部 ${optionalDomainPacks.length} 项`}</span>
              <ChevronDown aria-hidden="true" size={16} />
            </button> : null}
          </div>
          {hiddenOptionalPackCount ? <p className="create-room-pack-catalog-omission" role="status">
            另有 {hiddenOptionalPackCount} 项未挂载；展开目录后可逐项审阅，不会自动选择。
          </p> : null}
          {visibleOptionalDomainPacks.map((pack) => {
            const selected = form.capability_pack_ids.includes(pack.id);
            const availability = packSelectionAvailability(lifecycleView, pack.id, { selected });
            const lifecycle = availability.lifecycle;
            return <label className={["capability-pack-card", selected ? "selected" : "", lifecycle?.runtimeState || "lifecycle-unverified"].filter(Boolean).join(" ")} key={createRoomListKey("optional-domain-pack", pack.id, pack.pack_version, pack.manifest_sha256)} title={!availability.canToggle ? availability.reason : ""}>
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
              ? stockScopeHelpId
              : stockScopeHelpId + " " + stockScopeErrorId}
            value={form.stock_room_scope_input}
            onChange={(event) => setForm((current) => ({ ...current, stock_room_scope_input: event.target.value }))}
            placeholder={"US:AAPL\nUS:MSFT"}
          />
          <small id={stockScopeHelpId}>保存时规范化并排序为 stock_room_scope_v1；只绑定你明确输入的标的，不自动发现或扩展股票。</small>
          {!stockScopeState.valid ? <em id={stockScopeErrorId} className="stock-room-scope-error" role="alert">{stockScopeState.error}</em> : null}
        </label> : null}
        {selectedTemplatePackSelection.blocked.length ? <p className="field-help plugin-lifecycle-selection-block" role="alert">模板中的以下能力包当前不可建立新绑定，已停止自动选择：{selectedTemplatePackSelection.blocked.join("、")}。</p> : null}
        {lifecycleCreationBlocked ? <p className="field-help plugin-lifecycle-selection-block" role="alert">能力包生命周期状态无法安全确认，暂不能创建新房间。现有历史记录不受影响。</p> : null}
        <section className={`create-room-final-review ${creationReviewTone}`} aria-labelledby={creationReviewTitleId} role="note">
          <div className="create-room-final-review-heading">
            {creationReviewTone === "blocked"
              ? <AlertTriangle aria-hidden="true" size={17} />
              : creationReviewTone === "ready"
                ? <ShieldCheck aria-hidden="true" size={17} />
                : <NotebookPen aria-hidden="true" size={17} />}
            <span><small>CREATE PERMIT / LOCAL DRAFT</small><h3 id={creationReviewTitleId}>{creationReviewLabel}</h3></span>
            <data value={requiredFieldsCount}>{requiredFieldsCount} / 3 必填</data>
          </div>
          <dl aria-label="创建前复核摘要">
            <div><dt>房间模板</dt><dd>{selectedTemplate?.name || "未选择"}</dd></div>
            <div><dt>模板成员</dt><dd>{selectedRoster?.available ? `${selectedRoster.count} 位` : "创建后确认"}</dd></div>
            <div><dt>可选领域包</dt><dd>{selectedOptionalPackCount} 项</dd></div>
          </dl>
          <p>创建房间配置与启动正式讨论轮是两个独立确认步骤；这里不会把模板选择解释为执行授权。</p>
        </section>
        <footer className="create-room-footer">
          <p id={submitStatusId} className={submitBlocked || submitError ? "create-room-submit-status blocked" : "create-room-submit-status"} role={submitError ? "alert" : "status"} aria-live="polite" aria-atomic="true">{creationStatus}</p>
          <span className="create-room-actions"><button type="button" className="secondary" onClick={requestClose} disabled={submitting}>取消</button><button className="primary" type="submit" disabled={submitBlocked}>{submitting ? <LoaderCircle className="spin" size={14} aria-hidden="true" /> : null}{submitting ? "创建中…" : "创建房间"}</button></span>
        </footer>
      </form>
    </div>
  );
}

function memberArrayValue(value) {
  return Array.isArray(value) ? value : [];
}

function memberBoundedText(value, fallback = "", limit = 1000) {
  const text = String(value ?? "").trim();
  const normalized = text || fallback;
  return normalized.length <= limit ? normalized : normalized.slice(0, limit) + "...";
}

function memberProviderOptions(providers, selectedProviderId) {
  const options = [];
  const seen = new Set();
  for (const provider of memberArrayValue(providers)) {
    if (!provider || typeof provider !== "object") continue;
    const id = normalizedProviderId(provider.id);
    if (seen.has(id)) continue;
    seen.add(id);
    options.push({ ...provider, id });
  }
  if (!seen.has(selectedProviderId)) {
    options.unshift({
      id: selectedProviderId,
      name: selectedProviderId === UNASSIGNED_PROVIDER_ID
        ? "未分配执行器"
        : "未知执行器：" + selectedProviderId,
      configured: false,
      policy_disabled: selectedProviderId === UNASSIGNED_PROVIDER_ID,
    });
  }
  return options;
}

function memberProviderStatus(provider, assigned) {
  if (!assigned) return { tone: "blocked", label: "执行器未分配", detail: "必须显式选择模型执行器后才能保存成员。" };
  if (!provider) return { tone: "warning", label: "执行器状态未知", detail: "当前执行器不在服务端目录中，保存时仍会由服务端重新校验。" };
  if (provider.policy_disabled === true) return { tone: "blocked", label: "执行器已被策略禁用", detail: "该执行器不能用于新的身份配置。" };
  if (providerIsAvailable(provider)) return { tone: "available", label: "执行器当前可用", detail: "实际路由仍会在每轮开始时冻结并再次校验。" };
  if (provider.configured === false) return { tone: "warning", label: "执行器尚未配置", detail: "可以保留现有选择，但普通发言前仍需完成配置。" };
  return { tone: "warning", label: "执行器状态待核验", detail: "保存身份不代表执行器已经获得运行授权。" };
}

export function MemberDialog({ member, room, open, onClose, onSubmit, onDelete, archiveDisabled = false, providers = [], memberTemplates = [] }) {
  const providerCatalog = memberArrayValue(providers);
  const templateCatalog = memberArrayValue(memberTemplates);
  const [form, setForm] = useState(null);
  const [selectedTemplateId, setSelectedTemplateId] = useState("");
  const [initializedFormKey, setInitializedFormKey] = useState("");
  const [saving, setSaving] = useState(false);
  const [actionKind, setActionKind] = useState("");
  const [localError, setLocalError] = useState("");
  const initializedMemberKey = useRef("");
  const actionRequestRef = useRef(0);
  const dialogRef = useRef(null);
  const closeButtonRef = useRef(null);
  const nameInputRef = useRef(null);
  const dialogTitleId = useId();
  const memberStatusId = useId();
  const memberErrorId = useId();
  const memberWorkflowId = useId();
  const memberInitializationKey = JSON.stringify([
    "member-dialog",
    room?.id || "",
    member?.id || "new",
    member?.version || 0,
  ]);

  useEffect(() => () => {
    actionRequestRef.current += 1;
  }, []);

  useLayoutEffect(() => {
    if (!open || !member) {
      actionRequestRef.current += 1;
      initializedMemberKey.current = "";
      setInitializedFormKey("");
      setSaving(false);
      setActionKind("");
      setLocalError("");
      return;
    }
    if (initializedMemberKey.current === memberInitializationKey) return;
    actionRequestRef.current += 1;
    initializedMemberKey.current = memberInitializationKey;
    const preferredProviderIds = ["deepseek", "doubao", "glm"];
    const preferredProvider = preferredProviderIds
      .map((providerId) => providerCatalog.find(
        (provider) => normalizedProviderId(provider.id) === providerId
          && providerIsAvailable(provider),
      ))
      .find(Boolean)
      || providerCatalog.find(providerIsAvailable)
      || preferredProviderIds
        .map((providerId) => providerCatalog.find(
          (provider) => normalizedProviderId(provider.id) === providerId
            && provider.policy_disabled !== true,
        ))
        .find(Boolean)
      || providerCatalog.find((provider) => provider.policy_disabled !== true);
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
    setActionKind("");
    setLocalError("");
    setInitializedFormKey(memberInitializationKey);
  }, [member, memberInitializationKey, open, providerCatalog]);

  const memberDialogSurfaceOpen = Boolean(
    open && form && initializedFormKey === memberInitializationKey,
  );
  const closeMemberDialog = () => {
    if (!saving) onClose?.();
  };

  useModalFocus({
    open: memberDialogSurfaceOpen,
    containerRef: dialogRef,
    initialFocusRef: member?.id ? closeButtonRef : nameInputRef,
    onClose: saving ? null : closeMemberDialog,
  });

  useEffect(() => {
    if (memberDialogSurfaceOpen && saving) dialogRef.current?.focus({ preventScroll: true });
  }, [memberDialogSurfaceOpen, saving]);

  useEffect(() => {
    if (!memberDialogSurfaceOpen) return undefined;
    const containDialogFocus = (event) => {
      const dialog = dialogRef.current;
      if (!dialog || dialog.contains(event.target)) return;
      const focusTarget = saving ? dialog : member?.id ? closeButtonRef.current : nameInputRef.current;
      focusTarget?.focus({ preventScroll: true });
    };
    document.addEventListener("focusin", containDialogFocus, true);
    return () => document.removeEventListener("focusin", containDialogFocus, true);
  }, [member?.id, memberDialogSurfaceOpen, saving]);

  if (!memberDialogSurfaceOpen) return null;

  const isNew = !form.id;
  const selectedProviderId = normalizedProviderId(form.provider);
  const providerOptions = memberProviderOptions(providerCatalog, selectedProviderId);
  const selectedProvider = providerOptions.find(
    (provider) => normalizedProviderId(provider.id) === selectedProviderId,
  );
  const providerAssigned = selectedProviderId !== UNASSIGNED_PROVIDER_ID;
  const providerStatus = memberProviderStatus(selectedProvider, providerAssigned);
  const workflowStages = [...new Set([
    ...memberArrayValue(room?.workflow_policy?.stage_order),
    "flexible",
    form.workflow_stage || "flexible",
  ])].filter((stage) => stage && stage !== "follow_up");
  const capabilityOptions = collectCapabilityOptions(
    [{ capabilities: form.capabilities || [] }],
    room?.workflow_policy,
  );
  const memberTemplateGroups = groupMemberTemplates(templateCatalog);
  const isExplicitModerator = !isNew
    && Boolean(room?.moderator_member_id)
    && room.moderator_member_id === member?.id;
  const submitUnavailable = typeof onSubmit !== "function";
  const saveBlocked = saving || !providerAssigned || submitUnavailable;
  const selectedCapabilityCount = memberArrayValue(form.capabilities).length;
  const memberSaveTone = saving
    ? "saving"
    : localError || saveBlocked ? "blocked" : "review";
  const memberSaveLabel = saving
    ? actionKind === "archive" ? "正在归档成员" : "正在保存身份版本"
    : localError
      ? "成员操作需要处理"
      : !providerAssigned
        ? "必须先分配模型执行器"
        : submitUnavailable
          ? "当前视图未提供保存入口"
          : "身份草稿可由你保存";
  const memberSaveDetail = localError || (
    saving
      ? "请等待当前请求完成，期间不能关闭或重复提交。"
      : !providerAssigned || selectedProvider?.policy_disabled === true
        ? providerStatus.detail
        : submitUnavailable
          ? "必须由宿主提供显式保存处理器后才能提交身份草稿。"
          : "保存会创建新的身份版本；实际路由与流程门仍会在下一轮开始时重新核验。"
  );

  const selectMemberTemplate = (templateId) => {
    setSelectedTemplateId(templateId);
    const template = templateCatalog.find((item) => item.id === templateId);
    if (template) setForm((current) => applyMemberTemplate(current, template));
  };

  const toggleCapability = (capability) => {
    setForm((current) => {
      const selected = memberArrayValue(current.capabilities);
      return {
        ...current,
        capabilities: selected.includes(capability)
          ? selected.filter((item) => item !== capability)
          : [...selected, capability],
      };
    });
  };

  const runMemberAction = async (kind, action) => {
    if (saving || typeof action !== "function") return;
    const requestId = actionRequestRef.current + 1;
    actionRequestRef.current = requestId;
    setSaving(true);
    setActionKind(kind);
    setLocalError("");
    try {
      await action();
    } catch (requestError) {
      if (actionRequestRef.current === requestId) {
        const message = typeof requestError?.message === "string" ? requestError.message : "";
        setLocalError(memberBoundedText(message, kind === "archive" ? "归档成员失败，请重试。" : "保存成员失败，请检查输入后重试。"));
      }
    } finally {
      if (actionRequestRef.current === requestId) {
        setSaving(false);
        setActionKind("");
      }
    }
  };

  const archiveMember = () => {
    if (isNew || typeof onDelete !== "function" || saving) return;
    const memberName = memberBoundedText(form.name, "未命名成员", 120);
    if (window.confirm("确定归档「" + memberName + "」吗？历史发言和身份版本会保留，之后可以恢复。")) {
      runMemberAction("archive", () => onDelete(form));
    }
  };

  const submitMember = async (event) => {
    event.preventDefault();
    if (saveBlocked) return;
    await runMemberAction("save", () => onSubmit(form));
  };

  return (
    <div className="dialog-backdrop member-dialog-backdrop-v2" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) closeMemberDialog(); }}>
      <form
        ref={dialogRef}
        className="dialog member-dialog member-dialog-v2"
        role="dialog"
        aria-modal="true"
        aria-labelledby={dialogTitleId}
        aria-describedby={localError ? memberStatusId + " " + memberErrorId : memberStatusId}
        aria-busy={saving}
        tabIndex={-1}
        onSubmit={submitMember}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <div className="member-dialog-heading">
            <div className="member-dialog-heading-copy">
              <small>MEMBER IDENTITY / NEXT ROUND</small>
              <h2 id={dialogTitleId}>{isNew ? "添加 AI 成员" : "编辑 AI 身份"}</h2>
            </div>
            {!isNew && <small className="version-tag">身份版本 v{form.version || 1}</small>}
          </div>
          <button ref={closeButtonRef} type="button" className="icon-button" aria-label="关闭成员设置" disabled={saving} onClick={closeMemberDialog}><X size={18} aria-hidden="true" /></button>
        </header>

        <section className="member-dialog-ledger" aria-label="下一轮成员贡献摘要" role="list">
          <span role="listitem"><small>流程阶段</small><strong>{stageLabel(form.workflow_stage || "flexible")}</strong></span>
          <span role="listitem"><small>研究立场</small><strong>{form.stance || "未指定"}</strong></span>
          <span role="listitem"><small>能力标签</small><strong><data value={selectedCapabilityCount}>{selectedCapabilityCount}</data> 项</strong></span>
          <span role="listitem"><small>后续参与</small><strong>{form.enabled ? "参与" : "暂停"}</strong></span>
        </section>

        <fieldset className="member-dialog-fields" disabled={saving}>
          <legend>成员身份字段</legend>
          <label className="member-template-picker">身份模板（可选）
            <select value={selectedTemplateId} onChange={(event) => selectMemberTemplate(event.target.value)}>
              <option value="">不套用模板，继续当前内容</option>
              {memberTemplateGroups.map((group, groupIndex) => (
                <optgroup key={JSON.stringify(["member-template-group", group.label, groupIndex])} label={group.label}>
                  {group.items.map((template, templateIndex) => (
                    <option key={JSON.stringify(["member-template", template.id, templateIndex])} value={template.id}>{template.name} · {template.identity}</option>
                  ))}
                </optgroup>
              ))}
            </select>
            <small>模板只填入身份、职责、边界、阶段与能力标签；不会改变当前 Provider、模型、启用状态或历史版本。</small>
          </label>

          <div className="form-grid">
            <label>显示名<input ref={nameInputRef} required value={form.name} onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))} placeholder="例如：行业分析师" /></label>
            <label>身份定位<input required value={form.identity} onChange={(event) => setForm((current) => ({ ...current, identity: event.target.value }))} placeholder="负责什么专业判断" /></label>
          </div>

          <section className="member-workflow-config" aria-labelledby={memberWorkflowId}>
            <header>
              <span>
                <small>NEXT ROUND CONTRIBUTION</small>
                <h3 id={memberWorkflowId}>下一轮流程贡献</h3>
              </span>
              <p>阶段、立场与能力用于补齐讨论流程的成员门；保存身份不等于下一轮已获启动授权。</p>
            </header>
            <div className="form-grid">
              <label>研究立场<input value={form.stance} onChange={(event) => setForm((current) => ({ ...current, stance: event.target.value }))} placeholder="例如 bull、bear、risk" /></label>
              <label>流程阶段<select value={form.workflow_stage || "flexible"} onChange={(event) => setForm((current) => ({ ...current, workflow_stage: event.target.value }))}>
                {workflowStages.map((stage, index) => <option value={stage} key={JSON.stringify(["workflow-stage", stage, index])}>{stageLabel(stage)}</option>)}
              </select></label>
            </div>

            <fieldset className="member-capability-fieldset">
              <legend>专业能力标签</legend>
              <p>主持人会用这些标签判断谁最适合补齐证据、反方意见、方案或风控要求，可多选。</p>
              <div className="member-capability-list">
                {capabilityOptions.map((option, index) => {
                  const selected = (form.capabilities || []).includes(option.id);
                  return (
                    <label className={selected ? "member-capability-chip selected" : "member-capability-chip"} key={JSON.stringify(["member-capability", option.id, index])}>
                      <input type="checkbox" checked={selected} onChange={() => toggleCapability(option.id)} />
                      {option.label}
                    </label>
                  );
                })}
              </div>
            </fieldset>
          </section>

          <label>核心职责<textarea required value={form.responsibilities} onChange={(event) => setForm((current) => ({ ...current, responsibilities: event.target.value }))} placeholder="这个 AI 必须完成哪些工作？" /></label>
          <label>行为边界<textarea required value={form.boundaries} onChange={(event) => setForm((current) => ({ ...current, boundaries: event.target.value }))} placeholder="哪些事情不能做，哪些结论必须保留给用户？" /></label>
          <label>补充发言规则<textarea className="large" value={form.instructions} onChange={(event) => setForm((current) => ({ ...current, instructions: event.target.value }))} placeholder="语气、证据格式、必须回应的问题等" /></label>

          <div className="form-grid">
            <label>模型执行器<select required value={selectedProviderId} onChange={(event) => setForm((current) => ({ ...current, provider: event.target.value, model: "" }))}>
              {providerOptions.map((provider, index) => (
                <option
                  disabled={provider.policy_disabled === true}
                  key={JSON.stringify(["member-provider", normalizedProviderId(provider.id), index])}
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
            <label>模型<input value={form.model || ""} onChange={(event) => setForm((current) => ({ ...current, model: event.target.value }))} placeholder={"留空使用默认：" + (selectedProvider?.model || "由执行器决定")} /></label>
          </div>

          <p id={memberStatusId} className={"member-provider-status " + providerStatus.tone}><strong>{providerStatus.label}</strong><span>{providerStatus.detail}</span></p>

          <label className="checkbox-line" title={isExplicitModerator ? "请先在房间设置中改派主持人，再暂停这名成员。" : ""}>
            <input
              type="checkbox"
              checked={Boolean(form.enabled)}
              disabled={isExplicitModerator && Boolean(form.enabled)}
              onChange={(event) => setForm((current) => ({ ...current, enabled: event.target.checked }))}
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
        </fieldset>

        {localError ? <p id={memberErrorId} className="member-dialog-error" role="alert"><AlertTriangle size={14} aria-hidden="true" /><span>{localError}</span></p> : null}

        <footer className="member-dialog-footer">
          {!isNew ? <button type="button" className="danger-text" disabled={archiveDisabled || saving || typeof onDelete !== "function"} title={archiveDisabled ? "当前轮次运行或暂停中，结束后才能归档成员" : typeof onDelete !== "function" ? "当前视图未提供归档处理器" : "保留全部历史并移出活动成员列表"} onClick={archiveMember}><Archive size={15} aria-hidden="true" />{saving && actionKind === "archive" ? "归档中…" : "归档成员"}</button> : <span />}
          <div className={`member-save-summary ${memberSaveTone}`} role="status" aria-live="polite">
            {saving
              ? <LoaderCircle className="spin" size={17} aria-hidden="true" />
              : memberSaveTone === "review"
                ? <ShieldCheck size={17} aria-hidden="true" />
                : <AlertTriangle size={17} aria-hidden="true" />}
            <span><strong>{memberSaveLabel}</strong><small>{memberSaveDetail}</small></span>
          </div>
          <span className="member-dialog-actions"><button type="button" className="secondary" disabled={saving} onClick={closeMemberDialog}>取消</button><button className="primary" type="submit" disabled={saveBlocked}>{saving && actionKind === "save" ? <LoaderCircle className="spin" size={14} aria-hidden="true" /> : null}{saving && actionKind === "save" ? "保存中…" : isNew ? "添加成员" : "保存身份"}</button></span>
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

function materialArrayValue(value) {
  return Array.isArray(value) ? value : [];
}

function materialObjectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function materialBoundedText(value, fallback = "", limit = 1000) {
  const text = String(value ?? "").trim();
  const normalized = text || fallback;
  return normalized.length <= limit ? normalized : normalized.slice(0, limit) + "...";
}

function materialDialogIdentity(material, room) {
  const supplement = materialObjectValue(material?.official_supplement_v1);
  return JSON.stringify([
    "material-dialog",
    room?.id || "",
    material?.id || "new",
    material?.version || 0,
    supplement.symbol,
    supplement.fiscal_period,
    supplement.material_kind,
  ]);
}

function materialVersionSelection(value) {
  const candidates = materialArrayValue(value).filter((row) => (
    row && typeof row === "object"
    && Number.isInteger(Number(row.version))
    && Number(row.version) > 0
  ));
  const counts = candidates.reduce((result, row) => {
    const version = Number(row.version);
    result.set(version, (result.get(version) || 0) + 1);
    return result;
  }, new Map());
  const rows = candidates
    .filter((row) => counts.get(Number(row.version)) === 1)
    .sort((left, right) => Number(right.version) - Number(left.version));
  return {
    rows,
    omittedCount: materialArrayValue(value).length - rows.length,
  };
}

function materialErrorMessage(error, fallback) {
  const message = typeof error?.message === "string" ? error.message : "";
  return materialBoundedText(message, fallback);
}

function materialContentCharacters(value) {
  const number = Number(value);
  return Number.isInteger(number) && number >= 0 ? number.toLocaleString("zh-CN") : "未记录";
}

export function MaterialDialog({ material, room, open, onClose, onSubmit, onFetchUrl, onImportFile, onConfirmOfficialAttestation, versions = [], versionsLoading = false }) {
  const materialKey = materialDialogIdentity(material, room);
  const [form, setForm] = useState(null);
  const [initializedMaterialKey, setInitializedMaterialKey] = useState("");
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
  const initializedMaterialRef = useRef("");
  const versionPairRequest = useRef(0);
  const materialActionRequest = useRef(0);
  const materialActionInFlight = useRef(false);
  const editorDialogRef = useRef(null);
  const editorCloseButtonRef = useRef(null);
  const attestationDialogRef = useRef(null);
  const attestationCloseButtonRef = useRef(null);
  const dialogTitleId = useId();
  const operationStatusId = useId();
  const versionModel = useMemo(() => materialVersionSelection(versions), [versions]);
  const selectableVersions = versionModel.rows;
  const versionSignature = JSON.stringify(selectableVersions.map((row) => Number(row.version)));

  useEffect(() => {
    materialActionRequest.current += 1;
    materialActionInFlight.current = false;
  }, [materialKey, open]);

  useLayoutEffect(() => {
    if (!open || !material) {
      initializedMaterialRef.current = "";
      setInitializedMaterialKey("");
      setBusy(false);
      setLocalError("");
      return;
    }
    if (initializedMaterialRef.current === materialKey) return;
    initializedMaterialRef.current = materialKey;
    const sourceMaterial = materialObjectValue(material);
    const sourceMetadata = materialObjectValue(sourceMaterial.metadata);
    setForm({
      title: "",
      kind: "note",
      source_url: "",
      content: "",
      ...sourceMaterial,
      metadata: {
        source_type: sourceMetadata.source_type || "other",
        event_type: sourceMetadata.event_type || "other",
        publisher: sourceMetadata.publisher || "",
        published_at: sourceMetadata.published_at || "",
        ...sourceMetadata,
        symbols: [...materialArrayValue(sourceMetadata.symbols)],
      },
    });
    setMode(materialMode(sourceMaterial));
    setFile(null);
    setLocalError("");
    setBusy(false);
    setOfficialAttestation(sourceMaterial.official_attestation || sourceMaterial._official_attestation || null);
    setInitializedMaterialKey(materialKey);
  }, [material, materialKey, open]);

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

    Promise.resolve()
      .then(async () => {
        const baseRequest = api.materialVersion(room.id, material.id, baseVersion);
        const targetRequest = baseVersion === targetVersion
          ? baseRequest
          : api.materialVersion(room.id, material.id, targetVersion);
        const [baseData, targetData] = await Promise.all([baseRequest, targetRequest]);
        const left = baseData?.material;
        const right = targetData?.material;
        if (!left || !right) throw new Error("资料版本响应不完整。");
        if (Number(left.version) !== baseVersion || Number(right.version) !== targetVersion) {
          throw new Error("资料版本响应与所选版本不一致。");
        }
        return { left, right };
      })
      .then((pair) => {
        if (versionPairRequest.current !== requestId) return;
        setVersionPair(pair);
        setVersionPairStatus("ready");
      })
      .catch((requestError) => {
        if (versionPairRequest.current !== requestId) return;
        setVersionPairError(materialErrorMessage(requestError, "资料版本对比读取失败。"));
        setVersionPairStatus("error");
      });

    return () => {
      if (versionPairRequest.current === requestId) versionPairRequest.current += 1;
    };
  }, [baseVersion, material?.id, open, room?.id, targetVersion, versionPairReload]);

  const materialSurfaceOpen = Boolean(open && form && initializedMaterialKey === materialKey);
  const attestationSurfaceOpen = materialSurfaceOpen && Boolean(officialAttestation);
  const activeDialogRef = attestationSurfaceOpen ? attestationDialogRef : editorDialogRef;
  const activeCloseButtonRef = attestationSurfaceOpen ? attestationCloseButtonRef : editorCloseButtonRef;
  const finishClose = () => {
    materialActionRequest.current += 1;
    materialActionInFlight.current = false;
    onClose?.();
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
  const metadata = materialObjectValue(form.metadata);
  const metadataSymbols = materialArrayValue(metadata.symbols);
  const officialSupplementObject = materialObjectValue(form.official_supplement_v1);
  const officialSupplement = Object.keys(officialSupplementObject).length ? officialSupplementObject : null;
  const officialSupplementUrl = safeExternalUrl(officialSupplement?.official_url);
  const promptQuarantine = materialPromptQuarantine({ metadata });

  const updateMetadata = (patch) => setForm((current) => ({
    ...current,
    metadata: {
      ...materialObjectValue(current.metadata),
      ...patch,
    },
  }));

  const toggleSymbol = (symbol) => updateMetadata({
    symbols: metadataSymbols.includes(symbol)
      ? metadataSymbols.filter((item) => item !== symbol)
      : [...metadataSymbols, symbol],
  });

  const selectMode = (nextMode) => {
    if (busy) return;
    setMode(nextMode);
    setLocalError("");
    if (nextMode !== "file") setFile(null);
  };

  const selectFile = (event) => {
    const nextFile = event.currentTarget.files?.[0] || null;
    setLocalError("");
    if (nextFile && nextFile.size > MAX_MATERIAL_FILE_BYTES) {
      setFile(null);
      event.currentTarget.value = "";
      setLocalError("文件超过 " + (MAX_MATERIAL_FILE_BYTES / 1_000_000) + " MB 上限，未进入解析队列。");
      return;
    }
    if (nextFile && nextFile.size <= 0) {
      setFile(null);
      event.currentTarget.value = "";
      setLocalError("空文件不能进入资料解析。");
      return;
    }
    setFile(nextFile);
  };

  const run = async (action, onSuccess, fallbackError = "资料操作失败，请重试。") => {
    if (busy || materialActionInFlight.current) return null;
    if (typeof action !== "function") {
      setLocalError("当前视图未提供对应资料处理器。");
      return null;
    }
    const requestId = materialActionRequest.current + 1;
    materialActionRequest.current = requestId;
    materialActionInFlight.current = true;
    setBusy(true);
    setLocalError("");
    try {
      const result = await action();
      if (materialActionRequest.current !== requestId) return result;
      if (onSuccess) await onSuccess(result);
      return result;
    } catch (requestError) {
      if (materialActionRequest.current === requestId) {
        setLocalError(materialErrorMessage(requestError, fallbackError));
      }
      return null;
    } finally {
      if (materialActionRequest.current === requestId) {
        materialActionInFlight.current = false;
        setBusy(false);
      }
    }
  };

  const primaryHandlerAvailable = mode === "url"
    ? typeof onFetchUrl === "function"
    : mode === "file"
      ? typeof onImportFile === "function"
      : typeof onSubmit === "function";

  const submitPrimary = () => {
    if (mode === "url") return run(
      typeof onFetchUrl === "function" ? () => onFetchUrl(form) : null,
      undefined,
      "网页抓取失败，请检查公开 URL 后重试。",
    );
    if (mode === "file") return run(
      typeof onImportFile === "function" ? () => onImportFile(form, file) : null,
      officialSupplement ? (result) => {
        const attestation = result?.official_attestation || result?.material?._official_attestation;
        if (!result?.material?.id || !attestation) {
          throw new Error("服务端未返回官方补证暂存预览；资料仍保持未确认。");
        }
        setForm({ ...result.material, official_supplement_v1: officialSupplement });
        setOfficialAttestation(attestation);
        setFile(null);
      } : undefined,
      "文件解析或导入失败，请检查文件后重试。",
    );
    return run(
      typeof onSubmit === "function" ? () => onSubmit({ ...form, kind: "note", source_url: "" }) : null,
      undefined,
      "资料保存失败，请检查内容后重试。",
    );
  };

  const saveManualRevision = () => run(
    typeof onSubmit === "function" ? () => onSubmit(form) : null,
    undefined,
    "手工修订保存失败，请重试。",
  );

  const confirmOfficialAttestation = (confirmation) => {
    if (!confirmation || !form.id || typeof onConfirmOfficialAttestation !== "function") {
      setLocalError("官方补证确认信息不完整。");
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
      "官方补证确认失败，请重新核对三项哈希。",
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

  const primaryDisabled = busy
    || !primaryHandlerAvailable
    || (mode === "file" && !file)
    || (officialSupplement && officialSupplement.user_confirmed !== true);
  const operationStatus = localError
    ? localError
    : busy
      ? "资料操作正在等待服务端确认，请勿重复提交或关闭对话框。"
      : !primaryHandlerAvailable
        ? "当前来源模式缺少处理器，不能提交。"
        : versionModel.omittedCount
          ? versionModel.omittedCount + " 条版本记录因版本号无效或重复而未进入精确对比。"
          : "资料变更会生成新版本；历史引用不会被静默改写。";

  return (
    <div className="dialog-backdrop material-dialog-backdrop-v2" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy) requestClose();
    }}>
      <form
        ref={editorDialogRef}
        className="dialog material-dialog material-dialog-v2"
        role="dialog"
        aria-modal="true"
        aria-labelledby={dialogTitleId}
        aria-describedby={operationStatusId}
        aria-busy={busy}
        tabIndex={-1}
        onSubmit={(event) => {
          event.preventDefault();
          if (!primaryDisabled) submitPrimary();
        }}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <span><strong id={dialogTitleId}>{officialSupplement ? "上传官方文件补证" : isNew ? "添加共享资料" : "编辑共享资料"}</strong>{!isNew ? <small className="version-tag">资料版本 v{form.version || 1}</small> : null}</span>
          <button ref={editorCloseButtonRef} type="button" className="icon-button" aria-label="关闭资料编辑" onClick={requestClose} disabled={busy}><X size={18} aria-hidden="true" /></button>
        </header>

        <fieldset className="material-dialog-fields" disabled={busy}>
          <legend>共享资料字段</legend>

          {isNew && !officialSupplement ? <div className="material-mode-tabs" role="group" aria-label="资料来源方式">
            <button type="button" aria-pressed={mode === "note"} className={mode === "note" ? "active" : ""} onClick={() => selectMode("note")}><NotebookPen size={15} aria-hidden="true" />手工笔记</button>
            <button type="button" aria-pressed={mode === "url"} className={mode === "url" ? "active" : ""} onClick={() => selectMode("url")}><Globe2 size={15} aria-hidden="true" />抓取网页</button>
            <button type="button" aria-pressed={mode === "file"} className={mode === "file" ? "active" : ""} onClick={() => selectMode("file")}><Upload size={15} aria-hidden="true" />上传文件</button>
          </div> : null}

          {officialSupplement ? <section className="official-supplement-intro" aria-label="官方人工补证范围">
            <span><ShieldCheck size={16} aria-hidden="true" /><strong>{materialBoundedText(officialSupplement.symbol, "标的未知", 40).replace(/^US\./, "")} · {materialBoundedText(officialSupplement.fiscal_period, "期间未知", 80)}</strong><small>{materialBoundedText(officialSupplement.material_kind, "材料类型未知", 100)}</small></span>
            {officialSupplementUrl ? <a href={officialSupplementUrl} target="_blank" rel="noopener noreferrer"><ExternalLink size={11} aria-hidden="true" />{officialSupplementUrl}</a> : <em>官方 URL 缺失或协议不受支持</em>}
            <p>请只上传从上述固定官方入口下载的原文件。首次提交只会暂存并生成核验预览，不会立即解除 readiness 阻断。</p>
          </section> : null}

          <label>资料标题<input required={mode === "note"} value={form.title} onChange={(event) => setForm((current) => ({ ...current, title: event.target.value }))} placeholder={mode === "note" ? "例如：2026 Q2 财报要点" : "可选；留空时从来源自动识别"} /></label>

          {mode === "url" ? <>
            <label>公开网页链接<input required type="url" value={form.source_url || ""} onChange={(event) => setForm((current) => ({ ...current, source_url: event.target.value }))} placeholder="https://example.com/report" /></label>
            <div className="material-safety-note"><Globe2 size={16} aria-hidden="true" /><span>只抓取公开的 HTTP/HTTPS 页面；本机、私网、带账号密码或非标准端口会被拒绝。网页中的指令只视为不可信文本证据。</span></div>
          </> : null}

          {mode === "file" ? <>
            <label className="material-file-picker">选择文件
              <input
                type="file"
                required={isNew}
                accept=".txt,.md,.markdown,.csv,.tsv,.json,.html,.htm,.xml,.docx,.pdf"
                onChange={selectFile}
              />
              <span><Upload size={17} aria-hidden="true" />{file ? materialBoundedText(file.name, "未命名文件", 180) + " · " + (file.size / 1024).toFixed(1) + " KB" : "TXT、MD、CSV、JSON、HTML、XML、DOCX、PDF；最大 " + (MAX_MATERIAL_FILE_BYTES / 1_000_000) + " MB"}</span>
            </label>
            <div className="material-safety-note"><FileText size={16} aria-hidden="true" /><span>只保存提取后的文本、哈希和来源元数据，不保存原文件；PDF 需要安装 requirements.txt，扫描型 PDF 暂不做 OCR。</span></div>
            {officialSupplement ? <label className="official-supplement-confirmation">
              <input
                type="checkbox"
                required
                checked={officialSupplement.user_confirmed === true}
                onChange={(event) => setForm((current) => ({
                  ...current,
                  official_supplement_v1: {
                    ...materialObjectValue(current.official_supplement_v1),
                    user_confirmed: event.target.checked,
                  },
                }))}
              />
              <span><strong>我确认所选文件由我从上方完整官方 URL 下载</strong><small>提交后还需核对服务端返回的文件信息和三项哈希，再进行第二次确认。</small></span>
            </label> : null}
          </> : null}

          <fieldset className="material-evidence-metadata">
            <legend>证据时间与对象</legend>
            <div>
              <label>来源类型<select disabled={Boolean(officialSupplement)} value={metadata.source_type || "other"} onChange={(event) => updateMetadata({ source_type: event.target.value })}>
                {evidenceSourceTypes.map(([value, label], index) => <option key={JSON.stringify(["source-type", value, index])} value={value}>{label}</option>)}
              </select></label>
              <label>事件类型<select disabled={Boolean(officialSupplement)} value={metadata.event_type || "other"} onChange={(event) => updateMetadata({ event_type: event.target.value })}>
                {evidenceEventTypes.map(([value, label], index) => <option key={JSON.stringify(["event-type", value, index])} value={value}>{label}</option>)}
              </select></label>
              <label>发布者<input value={metadata.publisher || ""} onChange={(event) => updateMetadata({ publisher: event.target.value })} placeholder="例如 Micron Investor Relations" /></label>
              <label>发布时间（含时区）<input value={metadata.published_at || ""} onChange={(event) => updateMetadata({ published_at: event.target.value })} placeholder="2026-07-19T08:30:00-04:00" /></label>
            </div>
            {hasRoomCapability(room, ROOM_CAPABILITIES.storageMarket) ? <div className="material-symbol-map">
              <small>关联标的（可多选）</small>
              <span>{storageSymbols.map((symbol, index) => <label key={JSON.stringify(["storage-symbol", symbol, index])}>
                <input type="checkbox" disabled={Boolean(officialSupplement)} checked={metadataSymbols.includes(symbol)} onChange={() => toggleSymbol(symbol)} />
                {symbol.replace(/^US\./, "")}
              </label>)}</span>
            </div> : null}
            <p>公司公告和监管披露按一级来源处理；媒体与分析师研究为二级；社交媒体、内部笔记和未分类来源保持“未核验”。发布时间缺失时，AI 必须明确写成未知。</p>
          </fieldset>

          {(mode === "note" || !isNew) ? <label>可供 AI 使用的内容<textarea className="large" required value={form.content} onChange={(event) => setForm((current) => ({ ...current, content: event.target.value }))} placeholder="粘贴事实、摘要或需要共同核验的原文片段。" /></label> : null}

          {!isNew && Object.keys(metadata).length ? <div className="material-provenance">
            <strong>来源记录</strong>
            <span>{materialBoundedText(metadata.original_name || metadata.final_url || form.source_url, "手工资料", 500)}</span>
            <small>{materialBoundedText(metadata.extraction_method, "manual", 80)} · {materialContentCharacters(metadata.source_bytes)} bytes{metadata.truncated ? " · 内容已截断" : ""}</small>
            <small>{materialBoundedText(metadata.publisher, "发布者未知", 180)} · {materialBoundedText(metadata.published_at, "发布时间未知", 80)} · {metadataSymbols.join("、") || "未映射标的"}</small>
            {metadata.source_sha256 ? <code title={materialBoundedText(metadata.source_sha256, "", 128)}>SHA256 {materialBoundedText(metadata.source_sha256, "", 128).slice(0, 16)}…</code> : null}
          </div> : null}

          {!isNew && promptQuarantine.quarantined ? <div className="material-safety-note quarantined" role="status">
            <ShieldCheck size={16} aria-hidden="true" /><span>检测到可能要求覆盖指令、泄露秘密、调用工具或执行资金动作的文本。原文仍保存在本机供你查看，但该版本不会发送给 AI、进入轮次证据或自动纪要。若需使用其中事实，请新建一份去除指令性内容的清洁摘要。标记：{materialArrayValue(promptQuarantine.labels).join("、") || "未分类风险"}</span>
          </div> : null}

          {!isNew ? <section className="material-version-history material-version-comparison">
            <span className="material-version-heading"><strong>版本历史与只读对比</strong><small>当前 v{form.version || 1}</small></span>
            {versionsLoading ? <span><LoaderCircle className="spin" size={13} aria-hidden="true" />正在读取版本…</span> : null}
            {!versionsLoading && !selectableVersions.length ? <span>暂无唯一且有效的版本记录</span> : null}
            {versionModel.omittedCount ? <p className="material-version-omitted">{versionModel.omittedCount} 条版本记录因版本号无效或重复而排除。</p> : null}
            {selectableVersions.length ? <>
              <div className="material-version-selectors">
                <label>基准版本<select value={baseVersion || ""} onChange={(event) => setBaseVersion(Number(event.target.value))}>
                  {selectableVersions.map((item) => <option value={item.version} key={JSON.stringify(["base-material-version", Number(item.version)])}>v{item.version} · {formatMaterialVersionTime(item.changed_at)}</option>)}
                </select></label>
                <GitCompareArrows size={17} aria-hidden="true" />
                <label>对比版本<select value={targetVersion || ""} onChange={(event) => setTargetVersion(Number(event.target.value))}>
                  {selectableVersions.map((item) => <option value={item.version} key={JSON.stringify(["target-material-version", Number(item.version)])}>v{item.version} · {formatMaterialVersionTime(item.changed_at)}</option>)}
                </select></label>
              </div>
              <details className="material-version-index"><summary>查看 {selectableVersions.length} 条版本索引</summary>
                {selectableVersions.map((item) => <div key={JSON.stringify(["material-version-index", Number(item.version)])}>
                  <b>v{item.version}{Number(item.version) === Number(form.version) ? " · 当前" : ""}</b>
                  <span>{formatMaterialVersionTime(item.changed_at)}</span>
                  <small>{materialBoundedText(item.extraction_method, "方式未记录", 80)} · {materialContentCharacters(item.content_chars)} 字符{item.source_sha256 ? " · " + materialBoundedText(item.source_sha256, "", 128).slice(0, 10) + "…" : ""}</small>
                </div>)}
              </details>
            </> : null}
            {versionPairStatus === "loading" ? <span><LoaderCircle className="spin" size={13} aria-hidden="true" />正在读取两个精确版本…</span> : null}
            {versionPairError ? <span className="material-version-error" role="alert"><AlertTriangle size={13} aria-hidden="true" />{versionPairError}<button type="button" className="secondary" onClick={() => setVersionPairReload((value) => value + 1)}>重试</button></span> : null}
            {versionPair ? <MaterialVersionDiffView left={versionPair.left} right={versionPair.right} /> : null}
          </section> : null}

          <p className="field-help">抓取、替换或手工编辑都会生成新版本。历史消息仍保留引用时的资料版本，不会被新内容静默改写。</p>
        </fieldset>

        <p id={operationStatusId} className={localError ? "material-operation-status error" : busy ? "material-operation-status busy" : "material-operation-status"} role={localError ? "alert" : "status"} aria-live="polite" aria-atomic="true">{operationStatus}</p>

        <footer className="material-dialog-footer">
          <button type="button" className="secondary" onClick={requestClose} disabled={busy}>取消</button>
          {!isNew && mode !== "note" ? <button type="button" className="secondary" onClick={saveManualRevision} disabled={busy || typeof onSubmit !== "function"}><NotebookPen size={14} aria-hidden="true" />保存手工修订</button> : null}
          <button className={officialSupplement ? "primary official-supplement-stage" : "primary"} type="submit" disabled={primaryDisabled}>
            {busy ? <LoaderCircle className="spin" size={15} aria-hidden="true" /> : mode === "url" && !isNew ? <RefreshCcw size={14} aria-hidden="true" /> : mode === "file" ? <Upload size={14} aria-hidden="true" /> : null}
            {busy ? "处理中…" : officialSupplement ? "上传并生成核验预览" : mode === "url" ? (isNew ? "抓取并添加" : "重新抓取为新版本") : mode === "file" ? (isNew ? "解析并添加" : "替换文件为新版本") : (isNew ? "添加资料" : "保存新版本")}
          </button>
        </footer>
      </form>
    </div>
  );
}
