import {
  normalizeWorkflowPolicy,
  stageLabel,
} from "./workflowPolicy.js";

function integerInRange(value, minimum, maximum) {
  const number = Number(value);
  return Number.isInteger(number) && number >= minimum && number <= maximum;
}

function selectorIntegrity(value, label, issues) {
  if (!Array.isArray(value)) {
    issues.push(`${label}不是数组。`);
    return;
  }
  const seen = new Set();
  for (const item of value) {
    if (typeof item !== "string" || !item.trim()) {
      issues.push(`${label}包含无效标识。`);
      continue;
    }
    const id = item.trim().toLowerCase();
    if (seen.has(id)) issues.push(`${label}包含重复项：${id}。`);
    seen.add(id);
  }
}

export function workflowPolicySourceState(policy) {
  const draft = normalizeWorkflowPolicy(policy);
  if (policy == null) {
    return { draft, integrityOk: true, issues: [], sourceKind: "implicit-default" };
  }
  if (typeof policy !== "object" || Array.isArray(policy)) {
    return {
      draft,
      integrityOk: false,
      issues: ["流程策略来源不是对象，已按安全默认值显示。"],
      sourceKind: "repaired",
    };
  }

  const issues = [];
  if (!Array.isArray(policy.stage_order)) {
    issues.push("阶段顺序不是数组。 ");
  } else {
    const seenStages = new Set();
    for (const stage of policy.stage_order) {
      if (typeof stage !== "string" || !stage.trim()) {
        issues.push("阶段顺序包含无效标识。 ");
        continue;
      }
      const id = stage.trim();
      if (seenStages.has(id)) issues.push(`阶段顺序包含重复项：${id}。`);
      if (id === "follow_up") issues.push("follow_up 只能由调度器追加，不能进入标准阶段顺序。 ");
      seenStages.add(id);
    }
  }

  if (!policy.minimum_stage_coverage || typeof policy.minimum_stage_coverage !== "object" || Array.isArray(policy.minimum_stage_coverage)) {
    issues.push("阶段最低覆盖不是对象。 ");
  } else {
    for (const stage of draft.stage_order) {
      if (policy.minimum_stage_coverage[stage] != null
        && !integerInRange(policy.minimum_stage_coverage[stage], 1, 50)) {
        issues.push(`${stageLabel(stage)}的来源最低覆盖无效。`);
      }
    }
  }

  if (!Array.isArray(policy.required_coverage)) {
    issues.push("专业覆盖要求不是数组。 ");
  } else {
    const seenRequirements = new Set();
    for (const [index, requirement] of policy.required_coverage.entries()) {
      if (!requirement || typeof requirement !== "object" || Array.isArray(requirement)) {
        issues.push(`第 ${index + 1} 项专业覆盖要求不是对象。`);
        continue;
      }
      const id = typeof requirement.id === "string" ? requirement.id.trim().toLowerCase() : "";
      if (!id) issues.push(`第 ${index + 1} 项专业覆盖要求缺少标识。`);
      if (id && seenRequirements.has(id)) issues.push(`专业覆盖要求包含重复标识：${id}。`);
      if (id) seenRequirements.add(id);
      if (typeof requirement.label !== "string" || !requirement.label.trim()) {
        issues.push(`第 ${index + 1} 项专业覆盖要求缺少名称。`);
      }
      if (!integerInRange(requirement.minimum, 1, 50)) {
        issues.push(`第 ${index + 1} 项专业覆盖要求的最低人数无效。`);
      }
      if (!requirement.any_of || typeof requirement.any_of !== "object" || Array.isArray(requirement.any_of)) {
        issues.push(`第 ${index + 1} 项专业覆盖要求缺少 any_of 对象。`);
      } else {
        selectorIntegrity(requirement.any_of.stances, `第 ${index + 1} 项立场选择`, issues);
        selectorIntegrity(requirement.any_of.capabilities, `第 ${index + 1} 项能力选择`, issues);
      }
    }
  }

  const numericRules = [
    ["最低总覆盖", policy.minimum_successful_members, 1, 100],
    ["每人发言上限", policy.max_turns_per_member, 1, 5],
    ["追加追问额度", policy.follow_up_budget, 0, 50],
  ];
  for (const [label, value, minimum, maximum] of numericRules) {
    if (value != null && !integerInRange(value, minimum, maximum)) issues.push(`${label}的来源值无效。`);
  }
  if (policy.user_confirmation_required != null && policy.user_confirmation_required !== true) {
    issues.push("来源策略试图关闭用户最终确认。 ");
  }
  if (policy.execution_capability != null && policy.execution_capability !== "none") {
    issues.push("来源策略包含非 none 的执行能力。 ");
  }
  if (policy.live_trading_allowed != null && policy.live_trading_allowed !== false) {
    issues.push("来源策略试图允许真实交易。 ");
  }

  return {
    draft,
    integrityOk: issues.length === 0,
    issues: issues.map((issue) => issue.trim()),
    sourceKind: issues.length ? "repaired" : "explicit",
  };
}

export function workflowPolicyValidation(draft) {
  if (!draft || typeof draft !== "object" || Array.isArray(draft)) {
    return { ok: false, message: "讨论流程草稿不是对象。" };
  }
  if (!Array.isArray(draft.stage_order) || !draft.stage_order.length) {
    return { ok: false, message: "至少保留一个讨论阶段。" };
  }
  const stages = new Set();
  for (const stage of draft.stage_order) {
    if (typeof stage !== "string" || !stage.trim()) {
      return { ok: false, message: "讨论阶段包含无效标识。" };
    }
    if (stages.has(stage)) return { ok: false, message: `讨论阶段重复：${stageLabel(stage)}。` };
    stages.add(stage);
    const value = Number(draft.minimum_stage_coverage?.[stage]);
    if (!Number.isInteger(value) || value < 1 || value > 50) {
      return { ok: false, message: `${stageLabel(stage)}的最低人数必须在 1 到 50 之间。` };
    }
  }

  if (!Array.isArray(draft.required_coverage)) {
    return { ok: false, message: "专业覆盖要求不是数组。" };
  }
  if (draft.required_coverage.length > 24) {
    return { ok: false, message: "专业覆盖要求最多保留 24 项。" };
  }
  const requirementIds = new Set();
  for (const requirement of draft.required_coverage) {
    const id = typeof requirement?.id === "string" ? requirement.id.trim() : "";
    if (!id) return { ok: false, message: "每一项覆盖要求都需要稳定标识。" };
    if (requirementIds.has(id)) return { ok: false, message: `覆盖要求标识重复：${id}。` };
    requirementIds.add(id);
    if (typeof requirement.label !== "string" || !requirement.label.trim()) {
      return { ok: false, message: "每一项覆盖要求都需要一个名称。" };
    }
    const minimum = Number(requirement.minimum);
    if (!Number.isInteger(minimum) || minimum < 1 || minimum > 50) {
      return { ok: false, message: `“${requirement.label}”的最低人数必须在 1 到 50 之间。` };
    }
    const stances = requirement?.any_of?.stances;
    const capabilities = requirement?.any_of?.capabilities;
    if (!Array.isArray(stances) || !Array.isArray(capabilities)) {
      return { ok: false, message: `“${requirement.label}”的成员类型选择无效。` };
    }
    if (!(stances.length || capabilities.length)) {
      return { ok: false, message: `“${requirement.label}”还没有选择可承担这项工作的成员类型。` };
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
      return { ok: false, message: `${label}必须在 ${minimum} 到 ${maximum} 之间。` };
    }
  }
  return { ok: true, message: "流程草稿结构有效。" };
}

export function workflowPolicyErrorMessage(error, fallback = "讨论流程保存失败。") {
  if (error instanceof Error && typeof error.message === "string" && error.message.trim()) {
    return error.message.trim();
  }
  if (typeof error === "string" && error.trim()) return error.trim();
  return fallback;
}

export function workflowPolicySaveControl({
  draft,
  roomId,
  changed,
  busy,
  submitHandlerAvailable,
  closeHandlerAvailable,
}) {
  const validation = workflowPolicyValidation(draft);
  const checks = [
    { id: "draft", ok: validation.ok, label: validation.message },
    { id: "room", ok: typeof roomId === "string" && Boolean(roomId.trim()), label: "房间标识不可用。" },
    { id: "change", ok: changed === true, label: "没有待保存的流程变化。" },
    { id: "handlers", ok: submitHandlerAvailable === true && closeHandlerAvailable === true, label: "保存或关闭处理器不可用。" },
    { id: "idle", ok: busy !== true, label: "正在保存流程，请等待当前请求完成。" },
  ];
  const failed = checks.find((check) => !check.ok);
  return {
    checks,
    canSubmit: !failed,
    phase: busy ? "saving" : failed ? "blocked" : "ready",
    instruction: busy ? "正在保存流程，请等待当前请求完成。" : failed?.label || "流程已通过本地保存前检查。",
  };
}
