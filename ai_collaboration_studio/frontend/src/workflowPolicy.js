export const WORKFLOW_STAGE_LABELS = {
  facilitate: "主持开场",
  analysis: "分析取证",
  debate: "观点辩论",
  plan: "方案设计",
  risk: "风险复核",
  decision: "最终整合",
  flexible: "自由协作",
  follow_up: "追问修订",
};

export const STANCE_LABELS = {
  facilitator: "主持与目标守门",
  evidence: "事实与证据",
  challenger: "反方审查",
  builder: "方案设计",
  sector: "产业研究",
  fundamental: "基本面",
  technical: "技术与资金",
  sentiment: "新闻与情绪",
  bull: "多头论证",
  bear: "空头反证",
  paper_trader: "模拟方案",
  risk: "风险审查",
  portfolio_manager: "决策整合",
  neutral: "中立协作",
};

export const CAPABILITY_LABELS = {
  facilitation: "主持与目标守门",
  evidence_research: "事实与证据核验",
  critical_review: "反证审查",
  solution_design: "方案设计",
  storage_sector_analysis: "存储产业分析",
  fundamental_analysis: "基本面分析",
  technical_analysis: "技术与资金分析",
  sentiment_analysis: "新闻与情绪分析",
  bull_case: "多头论证",
  bear_case: "空头反证",
  simulation_planning: "模拟方案设计",
  risk_review: "风险复核",
  decision_synthesis: "决策整合",
};

const CAPABILITY_ORDER = Object.keys(CAPABILITY_LABELS);
const STANCE_ORDER = Object.keys(STANCE_LABELS);

function uniqueStrings(values) {
  return [...new Set((values || []).map((value) => String(value || "").trim()).filter(Boolean))];
}

function positiveInteger(value, fallback, minimum = 1, maximum = 100) {
  const number = Number(value);
  if (!Number.isInteger(number)) return fallback;
  return Math.min(maximum, Math.max(minimum, number));
}

export function stageLabel(stage) {
  return WORKFLOW_STAGE_LABELS[stage] || String(stage || "未命名阶段").replaceAll("_", " ");
}

export function stanceLabel(stance) {
  return STANCE_LABELS[stance] || String(stance || "未分类").replaceAll("_", " ");
}

export function capabilityLabel(capability) {
  return CAPABILITY_LABELS[capability] || String(capability || "未分类").replaceAll("_", " ");
}

export function normalizeWorkflowPolicy(policy) {
  const source = policy && typeof policy === "object" ? policy : {};
  const stageOrder = uniqueStrings(source.stage_order).filter((stage) => stage !== "follow_up");
  const cleanStageOrder = stageOrder.length ? stageOrder : ["facilitate", "flexible"];
  const sourceCoverage = source.minimum_stage_coverage && typeof source.minimum_stage_coverage === "object"
    ? source.minimum_stage_coverage
    : {};
  const minimumStageCoverage = Object.fromEntries(
    cleanStageOrder.map((stage) => [
      stage,
      positiveInteger(sourceCoverage[stage], 1, 1, 50),
    ]),
  );
  const requiredCoverage = Array.isArray(source.required_coverage)
    ? source.required_coverage.map((requirement, index) => ({
      id: String(requirement?.id || `coverage_${index + 1}`).trim().toLowerCase(),
      label: String(requirement?.label || `覆盖要求 ${index + 1}`).trim(),
      minimum: positiveInteger(requirement?.minimum, 1, 1, 50),
      any_of: {
        stances: uniqueStrings(requirement?.any_of?.stances).map((value) => value.toLowerCase()),
        capabilities: uniqueStrings(requirement?.any_of?.capabilities).map((value) => value.toLowerCase()),
      },
      is_counterargument: Boolean(requirement?.is_counterargument),
    }))
    : [];
  return {
    version: 1,
    stage_order: cleanStageOrder,
    minimum_stage_coverage: minimumStageCoverage,
    required_coverage: requiredCoverage,
    minimum_successful_members: positiveInteger(source.minimum_successful_members, 2, 1, 100),
    max_turns_per_member: positiveInteger(source.max_turns_per_member, 2, 1, 5),
    follow_up_budget: positiveInteger(source.follow_up_budget, 2, 0, 50),
    user_confirmation_required: true,
    execution_capability: "none",
    live_trading_allowed: false,
  };
}

export function policiesEqual(left, right) {
  if (!left || !right) return false;
  return JSON.stringify(normalizeWorkflowPolicy(left)) === JSON.stringify(normalizeWorkflowPolicy(right));
}

export function memberMatchesWorkflowRequirement(member, requirement) {
  const stances = requirement?.any_of?.stances || [];
  const capabilities = requirement?.any_of?.capabilities || [];
  return stances.includes(String(member?.stance || "").toLowerCase())
    || (member?.capabilities || []).some(
      (capability) => capabilities.includes(String(capability || "").toLowerCase()),
    );
}

export function workflowConfigurationGate(policy, members = []) {
  const normalized = normalizeWorkflowPolicy(policy);
  const enabled = (members || []).filter((member) => member?.enabled);
  const blockers = [];
  if (enabled.length < normalized.minimum_successful_members) {
    blockers.push({
      code: "WORKFLOW_MEMBER_CAPACITY_MISSING",
      title: "启用成员不足",
      detail: `当前只有 ${enabled.length} 位启用成员，流程要求至少 ${normalized.minimum_successful_members} 位不同成员成功发言。`,
    });
  }
  for (const stage of normalized.stage_order) {
    const configured = enabled.filter(
      (member) => String(member.workflow_stage || "flexible") === stage,
    ).length;
    const required = normalized.minimum_stage_coverage[stage];
    if (configured < required) {
      blockers.push({
        code: `WORKFLOW_STAGE_${stage.toUpperCase()}_MISSING`,
        title: `缺少“${stageLabel(stage)}”阶段成员`,
        detail: `已配置 ${configured} 位，流程要求 ${required} 位。`,
      });
    }
  }
  for (const requirement of normalized.required_coverage) {
    const configured = enabled.filter(
      (member) => memberMatchesWorkflowRequirement(member, requirement),
    ).length;
    if (configured < requirement.minimum) {
      blockers.push({
        code: `WORKFLOW_ROLE_${requirement.id.toUpperCase()}_MISSING`,
        title: `缺少${requirement.label}角色`,
        detail: `已配置 ${configured} 位，流程要求 ${requirement.minimum} 位。`,
      });
    }
  }
  return {
    ready: blockers.length === 0,
    configured_member_count: enabled.length,
    required_success_count: normalized.minimum_successful_members,
    blockers,
  };
}

export function collectCapabilityOptions(members = [], policy = null) {
  const discovered = [];
  for (const member of members || []) discovered.push(...(member.capabilities || []));
  for (const requirement of policy?.required_coverage || []) {
    discovered.push(...(requirement?.any_of?.capabilities || []));
  }
  const all = uniqueStrings([...CAPABILITY_ORDER, ...discovered]);
  return all.map((id) => ({ id, label: capabilityLabel(id) }));
}

export function collectStanceOptions(members = [], policy = null) {
  const discovered = (members || []).map((member) => member.stance);
  for (const requirement of policy?.required_coverage || []) {
    discovered.push(...(requirement?.any_of?.stances || []));
  }
  const all = uniqueStrings([...STANCE_ORDER, ...discovered]);
  return all.map((id) => ({ id, label: stanceLabel(id) }));
}
