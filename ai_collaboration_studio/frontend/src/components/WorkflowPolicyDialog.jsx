import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Check,
  CheckCircle2,
  LoaderCircle,
  Plus,
  RotateCcw,
  ShieldCheck,
  Trash2,
  X,
} from "lucide-react";
import { memo, useEffect, useId, useMemo, useRef, useState } from "react";
import "../styles/workflow-policy.css";
import { useModalFocus } from "../useModalFocus";
import {
  collectCapabilityOptions,
  collectStanceOptions,
  memberMatchesWorkflowRequirement,
  normalizeWorkflowPolicy,
  policiesEqual,
  stageLabel,
  workflowConfigurationGate,
  WORKFLOW_STAGE_LABELS,
} from "../workflowPolicy";
import {
  workflowPolicyErrorMessage,
  workflowPolicySaveControl,
  workflowPolicySourceState,
} from "../workflowPolicyUi";

const STANDARD_WORKFLOW_STAGES = Object.keys(WORKFLOW_STAGE_LABELS)
  .filter((stage) => stage !== "follow_up");
const EMPTY_MEMBERS = Object.freeze([]);

function nextRequirementId(requirements) {
  const existing = new Set(requirements.map((item) => item.id));
  let index = 1;
  while (existing.has(`custom_${index}`)) index += 1;
  return `custom_${index}`;
}

function memberMatchesRequirement(member, requirement) {
  return member?.enabled === true && memberMatchesWorkflowRequirement(member, requirement);
}

export const WorkflowPolicyDialog = memo(function WorkflowPolicyDialog({
  roomId,
  roomTitle,
  open,
  policy,
  templatePolicy,
  members = EMPTY_MEMBERS,
  roundRunning = false,
  onClose,
  onSubmit,
}) {
  const dialogRef = useRef(null);
  const closeButtonRef = useRef(null);
  const dialogTitleId = useId();
  const dialogDescriptionId = useId();
  const dialogBoundaryId = useId();
  const dialogGateId = useId();
  const requestSessionRef = useRef(0);
  const submissionInFlightRef = useRef(false);
  const policySourceState = useMemo(() => workflowPolicySourceState(policy), [policy]);
  const policyFingerprint = useMemo(
    () => JSON.stringify(policySourceState.draft),
    [policySourceState.draft],
  );
  const normalizedTemplatePolicy = useMemo(
    () => (templatePolicy ? normalizeWorkflowPolicy(templatePolicy) : null),
    [templatePolicy],
  );
  const [draft, setDraft] = useState(() => policySourceState.draft);
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState("");
  const [stageToAdd, setStageToAdd] = useState("");
  const memberRows = useMemo(
    () => (Array.isArray(members) ? members : EMPTY_MEMBERS),
    [members],
  );
  const canClose = typeof onClose === "function";

  useEffect(() => {
    requestSessionRef.current += 1;
    submissionInFlightRef.current = false;
    return () => {
      requestSessionRef.current += 1;
    };
  }, [open, policyFingerprint, roomId]);

  useEffect(() => {
    if (!open) {
      setBusy(false);
      return;
    }
    setDraft(workflowPolicySourceState(policy).draft);
    setBusy(false);
    setLocalError("");
    setStageToAdd("");
  }, [open, policyFingerprint, roomId]);

  const requestClose = () => {
    if (busy || !canClose) return;
    try {
      onClose();
    } catch (closeError) {
      setLocalError(workflowPolicyErrorMessage(closeError, "讨论流程窗口关闭失败。"));
    }
  };

  useModalFocus({
    open,
    containerRef: dialogRef,
    initialFocusRef: closeButtonRef,
    onClose: busy || !canClose ? null : requestClose,
  });

  useEffect(() => {
    if (open && busy) dialogRef.current?.focus({ preventScroll: true });
  }, [busy, open]);

  useEffect(() => {
    if (!open) return undefined;
    const containDialogFocus = (event) => {
      const dialog = dialogRef.current;
      if (!dialog || dialog.contains(event.target)) return;
      const focusTarget = busy ? dialog : closeButtonRef.current;
      focusTarget?.focus({ preventScroll: true });
    };
    document.addEventListener("focusin", containDialogFocus, true);
    return () => document.removeEventListener("focusin", containDialogFocus, true);
  }, [busy, open]);

  const capabilityOptions = useMemo(
    () => collectCapabilityOptions(memberRows, draft),
    [draft, memberRows],
  );
  const stanceOptions = useMemo(
    () => collectStanceOptions(memberRows, draft),
    [draft, memberRows],
  );
  const configurationGate = useMemo(
    () => workflowConfigurationGate(draft, memberRows),
    [draft, memberRows],
  );
  const availableStages = useMemo(
    () => STANDARD_WORKFLOW_STAGES.filter((stage) => !draft.stage_order.includes(stage)),
    [draft.stage_order],
  );
  const selectedStageToAdd = availableStages.includes(stageToAdd)
    ? stageToAdd
    : availableStages[0] || "";
  const policyChanged = !policySourceState.integrityOk
    || !policiesEqual(draft, policySourceState.draft);
  const saveControl = workflowPolicySaveControl({
    draft,
    roomId,
    changed: policyChanged,
    busy,
    submitHandlerAvailable: typeof onSubmit === "function",
    closeHandlerAvailable: typeof onClose === "function",
  });
  const visibleConfigurationBlockers = configurationGate.blockers.slice(0, 3);
  const remainingConfigurationBlockers = configurationGate.blockers.slice(3);
  const saveStateTone = busy
    ? "saving"
    : localError
      ? "blocked"
      : saveControl.canSubmit
        ? "review"
        : policyChanged ? "blocked" : "clean";
  const saveStateLabel = busy
    ? "正在保存讨论流程"
    : localError
      ? "保存状态需要处理"
      : saveControl.canSubmit
        ? "草稿已通过本地检查，等待你的保存"
        : policyChanged
          ? "草稿尚不能保存"
          : "当前没有待保存变化";
  const saveStateDetail = localError || saveControl.instruction;

  if (!open) return null;

  const moveStage = (index, direction) => {
    setDraft((current) => {
      const target = index + direction;
      if (target < 0 || target >= current.stage_order.length) return current;
      const stageOrder = [...current.stage_order];
      [stageOrder[index], stageOrder[target]] = [stageOrder[target], stageOrder[index]];
      return { ...current, stage_order: stageOrder };
    });
  };

  const updateStageCoverage = (stage, value) => {
    setDraft((current) => ({
      ...current,
      minimum_stage_coverage: {
        ...current.minimum_stage_coverage,
        [stage]: value === "" ? "" : Number(value),
      },
    }));
  };

  const addStage = () => {
    if (!selectedStageToAdd) return;
    setDraft((current) => {
      if (current.stage_order.includes(selectedStageToAdd)) return current;
      return {
        ...current,
        stage_order: [...current.stage_order, selectedStageToAdd],
        minimum_stage_coverage: {
          ...current.minimum_stage_coverage,
          [selectedStageToAdd]: 1,
        },
      };
    });
    setStageToAdd("");
  };

  const removeStage = (stage) => {
    if (memberRows.some((member) => member?.enabled === true && member.workflow_stage === stage)) return;
    setDraft((current) => {
      if (current.stage_order.length <= 1) return current;
      const nextCoverage = { ...current.minimum_stage_coverage };
      delete nextCoverage[stage];
      return {
        ...current,
        stage_order: current.stage_order.filter((item) => item !== stage),
        minimum_stage_coverage: nextCoverage,
      };
    });
  };

  const updateRequirement = (index, patch) => {
    setDraft((current) => ({
      ...current,
      required_coverage: current.required_coverage.map((item, itemIndex) => (
        itemIndex === index ? { ...item, ...patch } : item
      )),
    }));
  };

  const toggleRequirementSelector = (index, selector, value) => {
    setDraft((current) => {
      const requirement = current.required_coverage[index];
      if (!requirement) return current;
      const selected = requirement.any_of[selector] || [];
      const next = selected.includes(value)
        ? selected.filter((item) => item !== value)
        : [...selected, value];
      return {
        ...current,
        required_coverage: current.required_coverage.map((item, itemIndex) => (
          itemIndex === index
            ? { ...item, any_of: { ...item.any_of, [selector]: next } }
            : item
        )),
      };
    });
  };

  const addRequirement = () => {
    setDraft((current) => {
      const id = nextRequirementId(current.required_coverage);
      return {
        ...current,
        required_coverage: [
          ...current.required_coverage,
          {
            id,
            label: "新的覆盖要求",
            minimum: 1,
            any_of: { stances: [], capabilities: [] },
            is_counterargument: false,
          },
        ],
      };
    });
  };

  const removeRequirement = (index) => {
    setDraft((current) => ({
      ...current,
      required_coverage: current.required_coverage.filter((_, itemIndex) => itemIndex !== index),
    }));
  };

  const restoreTemplate = () => {
    if (!normalizedTemplatePolicy) return;
    setDraft(normalizedTemplatePolicy);
    setLocalError("");
  };

  const submit = async (event) => {
    event.preventDefault();
    if (submissionInFlightRef.current) return;
    if (!saveControl.canSubmit) {
      setLocalError(saveControl.instruction);
      return;
    }
    const submissionSession = requestSessionRef.current + 1;
    requestSessionRef.current = submissionSession;
    submissionInFlightRef.current = true;
    const submitHandler = onSubmit;
    const closeHandler = onClose;
    setBusy(true);
    setLocalError("");
    try {
      await submitHandler(normalizeWorkflowPolicy(draft));
    } catch (requestError) {
      if (requestSessionRef.current !== submissionSession) return;
      setLocalError(workflowPolicyErrorMessage(requestError));
      return;
    } finally {
      if (requestSessionRef.current === submissionSession) {
        submissionInFlightRef.current = false;
        setBusy(false);
      }
    }
    if (requestSessionRef.current === submissionSession) {
      try {
        closeHandler();
      } catch (closeError) {
        const detail = workflowPolicyErrorMessage(closeError, "");
        setLocalError(detail
          ? `流程已保存，但窗口关闭失败：${detail}`
          : "流程已保存，但窗口关闭失败。");
      }
    }
  };

  const templateDefault = normalizedTemplatePolicy && policiesEqual(draft, normalizedTemplatePolicy);

  return (
    <div
      className="dialog-backdrop workflow-dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target !== event.currentTarget) return;
        event.preventDefault();
        requestClose();
      }}
    >
      <form
        ref={dialogRef}
        className="dialog workflow-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={dialogTitleId}
        aria-describedby={`${dialogDescriptionId} ${dialogBoundaryId}`}
        aria-busy={busy}
        data-save-state={saveControl.phase}
        tabIndex={-1}
        onSubmit={submit}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <div className="workflow-dialog-title">
            <div className="workflow-dialog-heading-copy">
              <small>WORKFLOW POLICY / NEXT ROUND</small>
              <h2 id={dialogTitleId}>讨论流程设置</h2>
            </div>
            <small className={templateDefault ? "policy-source-tag template" : "policy-source-tag custom"}>
              {templateDefault ? "模板默认" : "已自定义"}
            </small>
          </div>
          <button ref={closeButtonRef} type="button" className="icon-button" aria-label="关闭讨论流程设置" onClick={requestClose} disabled={busy || !canClose}>
            <X size={18} aria-hidden="true" />
          </button>
        </header>

        <div className="workflow-dialog-body">
          <div className="workflow-intro">
            <h3>{roomTitle || "当前房间"}</h3>
            <p id={dialogDescriptionId}>主持人会在这些边界内动态点名，补齐必要意见后再决定是否继续追问；这不是固定轮询。</p>
            {roundRunning ? <small>当前轮次继续使用启动时的流程快照，本次修改从下一轮开始生效。</small> : null}
            {!policySourceState.integrityOk ? <small className="workflow-source-warning" role="alert">来源策略已按安全默认值归一化；保存会写入修复后的结构。{policySourceState.issues[0]}</small> : null}
          </div>

          <section className="workflow-policy-ledger" aria-label="流程配置状态" role="list">
            <span role="listitem"><small>阶段</small><strong><data value={draft.stage_order.length}>{draft.stage_order.length}</data> 个</strong></span>
            <span role="listitem"><small>专业覆盖</small><strong><data value={draft.required_coverage.length}>{draft.required_coverage.length}</data> 项</strong></span>
            <span role="listitem"><small>成员门禁</small><strong>{configurationGate.ready ? "当前满足" : `${configurationGate.blockers.length} 项缺口`}</strong></span>
            <span role="listitem"><small>草稿变化</small><strong>{policyChanged ? "待保存" : "无变化"}</strong></span>
          </section>

          <section
            className={`workflow-readiness-panel ${configurationGate.ready ? "clear" : "blocked"}`}
            aria-labelledby={dialogGateId}
            role="note"
          >
            <header>
              {configurationGate.ready
                ? <CheckCircle2 size={20} aria-hidden="true" />
                : <AlertTriangle size={20} aria-hidden="true" />}
              <span>
                <small>NEXT ROUND MEMBER GATE</small>
                <h3 id={dialogGateId}>
                  {configurationGate.ready
                    ? "成员配置满足当前流程"
                    : `下一轮仍有 ${configurationGate.blockers.length} 项成员配置缺口`}
                </h3>
              </span>
              <data
                value={configurationGate.blockers.length}
                aria-label={`成员配置缺口 ${configurationGate.blockers.length} 项`}
              >
                {configurationGate.blockers.length} 项
              </data>
            </header>
            <p className="workflow-readiness-scope">
              这里只核验启用成员数量、阶段、立场与能力；可保存不等于可启动，Provider、数据和用户确认仍会独立核验。
            </p>
            {configurationGate.ready ? (
              <p className="workflow-readiness-clear">
                当前有 {configurationGate.configured_member_count} 位启用成员，最低成功覆盖为 {configurationGate.required_success_count} 位。
              </p>
            ) : (
              <>
                <ul className="workflow-readiness-list">
                  {visibleConfigurationBlockers.map((blocker) => (
                    <li key={blocker.code}>
                      <strong>{blocker.title}</strong>
                      <small>{blocker.detail}</small>
                    </li>
                  ))}
                </ul>
                {remainingConfigurationBlockers.length ? (
                  <details className="workflow-readiness-more">
                    <summary>查看其余 {remainingConfigurationBlockers.length} 项缺口</summary>
                    <ul className="workflow-readiness-list">
                      {remainingConfigurationBlockers.map((blocker) => (
                        <li key={blocker.code}>
                          <strong>{blocker.title}</strong>
                          <small>{blocker.detail}</small>
                        </li>
                      ))}
                    </ul>
                  </details>
                ) : null}
                <p className="workflow-readiness-guidance">
                  可回到成员身份调整流程阶段、研究立场或专业能力，也可以在这里降低相应覆盖要求；门禁不会因保存而自动放宽。
                </p>
              </>
            )}
          </section>

          <fieldset className="workflow-fieldset" disabled={busy}>
            <legend>阶段顺序与最低覆盖</legend>
            <p className="workflow-field-help">上下调整阶段；同一成员重复发言不会重复增加阶段覆盖人数。</p>
            <div className="workflow-stage-list" role="list">
              {draft.stage_order.map((stage, index) => {
                const assignedMembers = memberRows.filter(
                  (member) => member?.enabled === true && member.workflow_stage === stage,
                ).length;
                const removalBlocked = assignedMembers > 0 || draft.stage_order.length <= 1;
                return (
                  <div className="workflow-stage-row" key={JSON.stringify(["stage", stage])} role="listitem">
                      <span className="workflow-stage-position">{index + 1}</span>
                      <span className="workflow-stage-movers">
                        <button type="button" aria-label={`上移${stageLabel(stage)}`} onClick={() => moveStage(index, -1)} disabled={index === 0 || busy}><ArrowUp size={14} aria-hidden="true" /></button>
                        <button type="button" aria-label={`下移${stageLabel(stage)}`} onClick={() => moveStage(index, 1)} disabled={index === draft.stage_order.length - 1 || busy}><ArrowDown size={14} aria-hidden="true" /></button>
                      </span>
                      <span className={assignedMembers < Number(draft.minimum_stage_coverage[stage]) ? "workflow-stage-name shortfall" : "workflow-stage-name"}>
                        <strong>{stageLabel(stage)}</strong>
                        <small>已分配 {assignedMembers} 位成员</small>
                      </span>
                      <label>
                        至少
                        <input
                          type="number"
                          min="1"
                          max="50"
                          value={draft.minimum_stage_coverage[stage] ?? 1}
                          onChange={(event) => updateStageCoverage(stage, event.target.value)}
                          disabled={busy}
                        />
                        位
                      </label>
                      <button
                        type="button"
                        className="danger-icon workflow-stage-remove"
                        aria-label={`移除${stageLabel(stage)}`}
                        title={assignedMembers > 0 ? "请先把该阶段成员调整到其他阶段" : draft.stage_order.length <= 1 ? "至少保留一个阶段" : "移除阶段"}
                        onClick={() => removeStage(stage)}
                        disabled={busy || removalBlocked}
                      >
                        <Trash2 size={14} aria-hidden="true" />
                      </button>
                  </div>
                );
              })}
            </div>
            {availableStages.length ? (
              <div className="workflow-stage-add">
                <label>
                  增加阶段
                  <select value={selectedStageToAdd} onChange={(event) => setStageToAdd(event.target.value)} disabled={busy}>
                    {availableStages.map((stage) => <option value={stage} key={stage}>{stageLabel(stage)}</option>)}
                  </select>
                </label>
                <button className="secondary" type="button" onClick={addStage} disabled={busy || !selectedStageToAdd}>
                  <Plus size={14} aria-hidden="true" />加入流程
                </button>
              </div>
            ) : null}
            <p className="workflow-field-help workflow-stage-note">阶段有成员时不能直接移除；先在成员身份中调整其流程阶段，避免留下无归属成员。</p>
          </fieldset>

          <fieldset className="workflow-fieldset coverage-fieldset" disabled={busy}>
            <legend>必须覆盖的专业意见</legend>
            <p className="workflow-field-help">符合任一所选立场或专业能力的成员，都可以承担对应要求。</p>
            <div className="coverage-rule-list" role="list">
              {draft.required_coverage.map((requirement, index) => {
                const matchingMembers = memberRows.filter((member) => memberMatchesRequirement(member, requirement)).length;
                return (
                  <article className="coverage-rule" key={JSON.stringify(["requirement", requirement.id])} role="listitem">
                  <div className="coverage-rule-head">
                    <label>
                      要求名称
                      <input
                        value={requirement.label}
                        maxLength={80}
                        onChange={(event) => updateRequirement(index, { label: event.target.value })}
                        disabled={busy}
                      />
                    </label>
                    <label>
                      至少
                      <span><input
                        type="number"
                        min="1"
                        max="50"
                        value={requirement.minimum}
                        onChange={(event) => updateRequirement(index, {
                          minimum: event.target.value === "" ? "" : Number(event.target.value),
                        })}
                        disabled={busy}
                      /> 位</span>
                    </label>
                    <button type="button" className="danger-icon" aria-label={`删除${requirement.label}`} onClick={() => removeRequirement(index)} disabled={busy}>
                      <Trash2 size={15} aria-hidden="true" />
                    </button>
                  </div>
                  <div className={matchingMembers < Number(requirement.minimum) ? "coverage-readiness shortfall" : "coverage-readiness"} aria-live="polite">
                    当前有 {matchingMembers} 位启用成员可承担
                    {matchingMembers < Number(requirement.minimum) ? `，还差 ${Math.max(0, Number(requirement.minimum) - matchingMembers)} 位` : ""}
                  </div>

                  <details className="coverage-selector">
                    <summary>
                      选择可承担成员
                      <span>{requirement.any_of.stances.length + requirement.any_of.capabilities.length} 个标签</span>
                    </summary>
                    <div className="coverage-selector-group">
                      <strong>研究立场</strong>
                      <div className="choice-chip-list">
                        {stanceOptions.map((option) => {
                          const checked = requirement.any_of.stances.includes(option.id);
                          return (
                            <label className={checked ? "choice-chip selected" : "choice-chip"} key={JSON.stringify(["stance", option.id])}>
                              <input
                                type="checkbox"
                                checked={checked}
                                onChange={() => toggleRequirementSelector(index, "stances", option.id)}
                                disabled={busy}
                              />
                              {checked ? <Check size={12} aria-hidden="true" /> : null}{option.label}
                            </label>
                          );
                        })}
                      </div>
                    </div>
                    <div className="coverage-selector-group">
                      <strong>专业能力</strong>
                      <div className="choice-chip-list">
                        {capabilityOptions.map((option) => {
                          const checked = requirement.any_of.capabilities.includes(option.id);
                          return (
                            <label className={checked ? "choice-chip selected" : "choice-chip"} key={JSON.stringify(["capability", option.id])}>
                              <input
                                type="checkbox"
                                checked={checked}
                                onChange={() => toggleRequirementSelector(index, "capabilities", option.id)}
                                disabled={busy}
                              />
                              {checked ? <Check size={12} aria-hidden="true" /> : null}{option.label}
                            </label>
                          );
                        })}
                      </div>
                    </div>
                  </details>

                  <label className="counterargument-toggle">
                    <input
                      type="checkbox"
                      checked={requirement.is_counterargument}
                      onChange={(event) => updateRequirement(index, { is_counterargument: event.target.checked })}
                      disabled={busy}
                    />
                    这是必须听到的反方或风险意见
                  </label>
                  </article>
                );
              })}
            </div>
            <button className="secondary add-coverage-rule" type="button" onClick={addRequirement} disabled={busy || draft.required_coverage.length >= 24}>
              <Plus size={14} aria-hidden="true" />新增覆盖要求
            </button>
          </fieldset>

          <fieldset className="workflow-fieldset" disabled={busy}>
            <legend>发言与追问边界</legend>
            <div className="workflow-limit-grid">
              <label>最低总覆盖
                <span><input
                  type="number"
                  min="1"
                  max="100"
                  value={draft.minimum_successful_members}
                  onChange={(event) => setDraft((current) => ({
                    ...current,
                    minimum_successful_members: event.target.value === "" ? "" : Number(event.target.value),
                  }))}
                  disabled={busy}
                /> 位不同成员</span>
              </label>
              <label>每人发言上限
                <span><input
                  type="number"
                  min="1"
                  max="5"
                  value={draft.max_turns_per_member}
                  onChange={(event) => setDraft((current) => ({
                    ...current,
                    max_turns_per_member: event.target.value === "" ? "" : Number(event.target.value),
                  }))}
                  disabled={busy}
                /> 次</span>
              </label>
              <label>追加追问额度
                <span><input
                  type="number"
                  min="0"
                  max="50"
                  value={draft.follow_up_budget}
                  onChange={(event) => setDraft((current) => ({
                    ...current,
                    follow_up_budget: event.target.value === "" ? "" : Number(event.target.value),
                  }))}
                  disabled={busy}
                /> 次点名</span>
              </label>
            </div>
          </fieldset>

          <div className="workflow-safety-boundary">
            <ShieldCheck size={19} aria-hidden="true" />
            <span>
              <strong>不可修改的安全边界</strong>
              <small id={dialogBoundaryId}>最终结论必须由你确认；系统只有研究、回测和模拟能力，不连接或执行真实交易。</small>
            </span>
          </div>

          {localError ? <div className="workflow-local-error" role="alert">{localError}</div> : null}
        </div>

        <footer className="workflow-dialog-footer">
          <button type="button" className="secondary restore-policy" onClick={restoreTemplate} disabled={!normalizedTemplatePolicy || templateDefault || busy}>
            <RotateCcw size={14} aria-hidden="true" />恢复模板默认
          </button>
          <div className={`workflow-save-summary ${saveStateTone}`} role="status" aria-live="polite">
            {busy
              ? <LoaderCircle className="spin" size={17} aria-hidden="true" />
              : saveControl.canSubmit
                ? <CheckCircle2 size={17} aria-hidden="true" />
                : <AlertTriangle size={17} aria-hidden="true" />}
            <span>
              <strong>{saveStateLabel}</strong>
              <small>{saveStateDetail}</small>
            </span>
          </div>
          <span className="workflow-footer-actions">
            <button type="button" className="secondary" onClick={requestClose} disabled={busy || !canClose}>取消</button>
            <button type="submit" className="primary" disabled={!saveControl.canSubmit}>{busy ? "保存中…" : "保存流程"}</button>
          </span>
        </footer>
      </form>
    </div>
  );
});
