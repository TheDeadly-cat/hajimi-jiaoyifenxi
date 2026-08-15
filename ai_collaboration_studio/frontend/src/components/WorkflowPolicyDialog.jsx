import {
  ArrowDown,
  ArrowUp,
  Check,
  Plus,
  RotateCcw,
  ShieldCheck,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import "../styles/workflow-policy.css";
import { useModalFocus } from "../useModalFocus";
import {
  collectCapabilityOptions,
  collectStanceOptions,
  normalizeWorkflowPolicy,
  policiesEqual,
  stageLabel,
  WORKFLOW_STAGE_LABELS,
} from "../workflowPolicy";

const STANDARD_WORKFLOW_STAGES = Object.keys(WORKFLOW_STAGE_LABELS)
  .filter((stage) => stage !== "follow_up");

function nextRequirementId(requirements) {
  const existing = new Set(requirements.map((item) => item.id));
  let index = 1;
  while (existing.has(`custom_${index}`)) index += 1;
  return `custom_${index}`;
}

function validateDraft(draft) {
  if (!draft.stage_order.length) return "至少保留一个讨论阶段。";
  for (const stage of draft.stage_order) {
    const value = Number(draft.minimum_stage_coverage[stage]);
    if (!Number.isInteger(value) || value < 1 || value > 50) {
      return `${stageLabel(stage)}的最低人数必须在 1 到 50 之间。`;
    }
  }
  for (const requirement of draft.required_coverage) {
    if (!requirement.label.trim()) return "每一项覆盖要求都需要一个名称。";
    const minimum = Number(requirement.minimum);
    if (!Number.isInteger(minimum) || minimum < 1 || minimum > 50) {
      return `“${requirement.label}”的最低人数必须在 1 到 50 之间。`;
    }
    if (!(requirement.any_of.stances.length || requirement.any_of.capabilities.length)) {
      return `“${requirement.label}”还没有选择可承担这项工作的成员类型。`;
    }
  }
  const numericRules = [
    ["最低总覆盖", draft.minimum_successful_members, 1, 100],
    ["每人发言上限", draft.max_turns_per_member, 1, 5],
    ["追加追问额度", draft.follow_up_budget, 0, 50],
  ];
  for (const [label, rawValue, minimum, maximum] of numericRules) {
    const value = Number(rawValue);
    if (!Number.isInteger(value) || value < minimum || value > maximum) {
      return `${label}必须在 ${minimum} 到 ${maximum} 之间。`;
    }
  }
  return "";
}

function memberMatchesRequirement(member, requirement) {
  if (!member?.enabled) return false;
  const stances = requirement?.any_of?.stances || [];
  const capabilities = requirement?.any_of?.capabilities || [];
  return stances.includes(member.stance)
    || (member.capabilities || []).some((capability) => capabilities.includes(capability));
}

export function WorkflowPolicyDialog({
  roomId,
  roomTitle,
  open,
  policy,
  templatePolicy,
  members = [],
  roundRunning = false,
  onClose,
  onSubmit,
}) {
  const dialogRef = useRef(null);
  const closeButtonRef = useRef(null);
  const [draft, setDraft] = useState(() => normalizeWorkflowPolicy(policy));
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState("");
  const [stageToAdd, setStageToAdd] = useState("");

  useEffect(() => {
    if (!open) {
      setBusy(false);
      return;
    }
    setDraft(normalizeWorkflowPolicy(policy));
    setBusy(false);
    setLocalError("");
    setStageToAdd("");
  }, [open, roomId]);

  useModalFocus({
    open,
    containerRef: dialogRef,
    initialFocusRef: closeButtonRef,
    onClose: busy ? null : onClose,
  });

  useEffect(() => {
    if (open && busy) dialogRef.current?.focus({ preventScroll: true });
  }, [busy, open]);

  const capabilityOptions = useMemo(
    () => collectCapabilityOptions(members, draft),
    [draft, members],
  );
  const stanceOptions = useMemo(
    () => collectStanceOptions(members, draft),
    [draft, members],
  );
  const availableStages = STANDARD_WORKFLOW_STAGES
    .filter((stage) => !draft.stage_order.includes(stage));
  const selectedStageToAdd = availableStages.includes(stageToAdd)
    ? stageToAdd
    : availableStages[0] || "";

  if (!open) return null;

  const moveStage = (index, direction) => {
    const target = index + direction;
    if (target < 0 || target >= draft.stage_order.length) return;
    const stageOrder = [...draft.stage_order];
    [stageOrder[index], stageOrder[target]] = [stageOrder[target], stageOrder[index]];
    setDraft({ ...draft, stage_order: stageOrder });
  };

  const updateStageCoverage = (stage, value) => {
    setDraft({
      ...draft,
      minimum_stage_coverage: {
        ...draft.minimum_stage_coverage,
        [stage]: value === "" ? "" : Number(value),
      },
    });
  };

  const addStage = () => {
    if (!selectedStageToAdd) return;
    setDraft({
      ...draft,
      stage_order: [...draft.stage_order, selectedStageToAdd],
      minimum_stage_coverage: {
        ...draft.minimum_stage_coverage,
        [selectedStageToAdd]: 1,
      },
    });
    setStageToAdd("");
  };

  const removeStage = (stage) => {
    if (draft.stage_order.length <= 1) return;
    const nextCoverage = { ...draft.minimum_stage_coverage };
    delete nextCoverage[stage];
    setDraft({
      ...draft,
      stage_order: draft.stage_order.filter((item) => item !== stage),
      minimum_stage_coverage: nextCoverage,
    });
  };

  const updateRequirement = (index, patch) => {
    setDraft({
      ...draft,
      required_coverage: draft.required_coverage.map((item, itemIndex) => (
        itemIndex === index ? { ...item, ...patch } : item
      )),
    });
  };

  const toggleRequirementSelector = (index, selector, value) => {
    const requirement = draft.required_coverage[index];
    const selected = requirement.any_of[selector] || [];
    const next = selected.includes(value)
      ? selected.filter((item) => item !== value)
      : [...selected, value];
    updateRequirement(index, {
      any_of: { ...requirement.any_of, [selector]: next },
    });
  };

  const addRequirement = () => {
    const id = nextRequirementId(draft.required_coverage);
    setDraft({
      ...draft,
      required_coverage: [
        ...draft.required_coverage,
        {
          id,
          label: "新的覆盖要求",
          minimum: 1,
          any_of: { stances: [], capabilities: [] },
          is_counterargument: false,
        },
      ],
    });
  };

  const removeRequirement = (index) => {
    setDraft({
      ...draft,
      required_coverage: draft.required_coverage.filter((_, itemIndex) => itemIndex !== index),
    });
  };

  const restoreTemplate = () => {
    if (!templatePolicy) return;
    setDraft(normalizeWorkflowPolicy(templatePolicy));
    setLocalError("");
  };

  const submit = async (event) => {
    event.preventDefault();
    const validationError = validateDraft(draft);
    if (validationError) {
      setLocalError(validationError);
      return;
    }
    setBusy(true);
    setLocalError("");
    try {
      await onSubmit(normalizeWorkflowPolicy(draft));
      onClose();
    } catch (requestError) {
      setLocalError(requestError.message || "讨论流程保存失败。");
      setBusy(false);
    }
  };

  const templateDefault = templatePolicy && policiesEqual(draft, templatePolicy);

  return (
    <div
      className="dialog-backdrop workflow-dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target !== event.currentTarget) return;
        event.preventDefault();
        if (!busy) onClose();
      }}
    >
      <form
        ref={dialogRef}
        className="dialog workflow-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="workflow-dialog-title"
        aria-busy={busy}
        tabIndex={-1}
        onSubmit={submit}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <span>
            <strong id="workflow-dialog-title">讨论流程设置</strong>
            <small className={templateDefault ? "policy-source-tag template" : "policy-source-tag custom"}>
              {templateDefault ? "模板默认" : "已自定义"}
            </small>
          </span>
          <button ref={closeButtonRef} type="button" className="icon-button" aria-label="关闭讨论流程设置" onClick={onClose} disabled={busy}>
            <X size={18} />
          </button>
        </header>

        <div className="workflow-dialog-body">
          <div className="workflow-intro">
            <strong>{roomTitle || "当前房间"}</strong>
            <p>主持人会在这些边界内动态点名，补齐必要意见后再决定是否继续追问；这不是固定轮询。</p>
            {roundRunning ? <small>当前轮次继续使用启动时的流程快照，本次修改从下一轮开始生效。</small> : null}
          </div>

          <fieldset className="workflow-fieldset">
            <legend>阶段顺序与最低覆盖</legend>
            <p className="workflow-field-help">上下调整阶段；同一成员重复发言不会重复增加阶段覆盖人数。</p>
            <div className="workflow-stage-list">
              {draft.stage_order.map((stage, index) => {
                const assignedMembers = members.filter(
                  (member) => member.enabled && member.workflow_stage === stage,
                ).length;
                const removalBlocked = assignedMembers > 0 || draft.stage_order.length <= 1;
                return (
                  <div className="workflow-stage-row" key={stage}>
                      <span className="workflow-stage-position">{index + 1}</span>
                      <span className="workflow-stage-movers">
                        <button type="button" aria-label={`上移${stageLabel(stage)}`} onClick={() => moveStage(index, -1)} disabled={index === 0 || busy}><ArrowUp size={14} /></button>
                        <button type="button" aria-label={`下移${stageLabel(stage)}`} onClick={() => moveStage(index, 1)} disabled={index === draft.stage_order.length - 1 || busy}><ArrowDown size={14} /></button>
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
                        <Trash2 size={14} />
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
                  <Plus size={14} />加入流程
                </button>
              </div>
            ) : null}
            <p className="workflow-field-help workflow-stage-note">阶段有成员时不能直接移除；先在成员身份中调整其流程阶段，避免留下无归属成员。</p>
          </fieldset>

          <fieldset className="workflow-fieldset coverage-fieldset">
            <legend>必须覆盖的专业意见</legend>
            <p className="workflow-field-help">符合任一所选立场或专业能力的成员，都可以承担对应要求。</p>
            <div className="coverage-rule-list">
              {draft.required_coverage.map((requirement, index) => {
                const matchingMembers = members.filter((member) => memberMatchesRequirement(member, requirement)).length;
                return (
                  <article className="coverage-rule" key={requirement.id}>
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
                      <Trash2 size={15} />
                    </button>
                  </div>
                  <div className={matchingMembers < Number(requirement.minimum) ? "coverage-readiness shortfall" : "coverage-readiness"}>
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
                            <label className={checked ? "choice-chip selected" : "choice-chip"} key={option.id}>
                              <input
                                type="checkbox"
                                checked={checked}
                                onChange={() => toggleRequirementSelector(index, "stances", option.id)}
                                disabled={busy}
                              />
                              {checked ? <Check size={12} /> : null}{option.label}
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
                            <label className={checked ? "choice-chip selected" : "choice-chip"} key={option.id}>
                              <input
                                type="checkbox"
                                checked={checked}
                                onChange={() => toggleRequirementSelector(index, "capabilities", option.id)}
                                disabled={busy}
                              />
                              {checked ? <Check size={12} /> : null}{option.label}
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
              <Plus size={14} />新增覆盖要求
            </button>
          </fieldset>

          <fieldset className="workflow-fieldset">
            <legend>发言与追问边界</legend>
            <div className="workflow-limit-grid">
              <label>最低总覆盖
                <span><input
                  type="number"
                  min="1"
                  max="100"
                  value={draft.minimum_successful_members}
                  onChange={(event) => setDraft({
                    ...draft,
                    minimum_successful_members: event.target.value === "" ? "" : Number(event.target.value),
                  })}
                  disabled={busy}
                /> 位不同成员</span>
              </label>
              <label>每人发言上限
                <span><input
                  type="number"
                  min="1"
                  max="5"
                  value={draft.max_turns_per_member}
                  onChange={(event) => setDraft({
                    ...draft,
                    max_turns_per_member: event.target.value === "" ? "" : Number(event.target.value),
                  })}
                  disabled={busy}
                /> 次</span>
              </label>
              <label>追加追问额度
                <span><input
                  type="number"
                  min="0"
                  max="50"
                  value={draft.follow_up_budget}
                  onChange={(event) => setDraft({
                    ...draft,
                    follow_up_budget: event.target.value === "" ? "" : Number(event.target.value),
                  })}
                  disabled={busy}
                /> 次点名</span>
              </label>
            </div>
          </fieldset>

          <div className="workflow-safety-boundary">
            <ShieldCheck size={19} />
            <span>
              <strong>不可修改的安全边界</strong>
              <small>最终结论必须由你确认；系统只有研究、回测和模拟能力，不连接或执行真实交易。</small>
            </span>
          </div>

          {localError ? <div className="workflow-local-error" role="alert">{localError}</div> : null}
        </div>

        <footer className="workflow-dialog-footer">
          <button type="button" className="secondary restore-policy" onClick={restoreTemplate} disabled={!templatePolicy || templateDefault || busy}>
            <RotateCcw size={14} />恢复模板默认
          </button>
          <span>
            <button type="button" className="secondary" onClick={onClose} disabled={busy}>取消</button>
            <button type="submit" className="primary" disabled={busy}>{busy ? "保存中…" : "保存流程"}</button>
          </span>
        </footer>
      </form>
    </div>
  );
}
