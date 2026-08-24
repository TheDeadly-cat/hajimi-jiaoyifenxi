function record(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function text(value) {
  return typeof value === "string" ? value.trim() : "";
}

function timestampMilliseconds(value) {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  const clean = text(value);
  if (!clean) return null;
  if (/^(0|[1-9][0-9]*)$/.test(clean)) {
    const numeric = Number(clean);
    return Number.isSafeInteger(numeric) ? numeric : null;
  }
  const parsed = Date.parse(clean);
  return Number.isFinite(parsed) ? parsed : null;
}

function isPlainRecord(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function canonicalInteger(value, { positive = false } = {}) {
  let number = null;
  if (Number.isSafeInteger(value)) number = value;
  else if (typeof value === "string" && /^(0|[1-9][0-9]*)$/.test(value.trim())) {
    const parsed = Number(value.trim());
    if (Number.isSafeInteger(parsed)) number = parsed;
  }
  if (number === null || number < 0 || (positive && number === 0)) return null;
  return number;
}

function normalizedRows(value, label, issues) {
  if (value === null || value === undefined) return [];
  if (!Array.isArray(value)) {
    issues?.push(`${label}必须是数组`);
    return [];
  }
  return value.filter((item) => {
    const valid = item && typeof item === "object" && !Array.isArray(item);
    if (!valid) issues?.push(`${label}包含无效条目`);
    return valid;
  });
}

function validateUniqueIdentities(values, label, identity, issues) {
  const seen = new Set();
  for (const value of values) {
    const id = identity(value);
    if (!id) {
      issues.push(`${label}身份缺失`);
      continue;
    }
    if (seen.has(id)) issues.push(`${label}身份重复`);
    seen.add(id);
  }
}

export function decisionLineageRows(value) {
  return Array.isArray(value)
    ? value.filter((item) => item && typeof item === "object" && !Array.isArray(item))
    : [];
}

export function isPortfolioEvent(event) {
  return text(event?.resource_type).toLowerCase() === "simulation.paper_portfolio";
}

export function isObservationEvent(event) {
  return text(event?.resource_type).toLowerCase().includes("observation");
}

export function isWalkForwardEvent(event) {
  return text(event?.resource_type).toLowerCase().includes("walk_forward");
}

export function eventRevision(event) {
  const direct = canonicalInteger(event?.resource_revision, { positive: true });
  if (direct !== null) return String(direct);
  const nested = canonicalInteger(event?.resource_snapshot?.version, { positive: true });
  return nested === null ? "" : String(nested);
}

export function runPortfolioVersion(run) {
  const direct = canonicalInteger(run?.portfolio_version, { positive: true });
  if (direct !== null) return direct;
  return canonicalInteger(run?.result?.portfolio_version, { positive: true }) || 0;
}

export function shortId(value) {
  const clean = text(value);
  return clean.length > 12 ? `…${clean.slice(-8)}` : clean || "未记录";
}

export function lineageDisplayText(value, fallback = "未记录") {
  return text(value) || fallback;
}

export function createdTime(value) {
  const milliseconds = timestampMilliseconds(value);
  if (milliseconds === null) return "时间未知";
  const date = new Date(milliseconds);
  if (Number.isNaN(date.getTime())) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function packageEvents(decisionPackage) {
  return decisionLineageRows(decisionPackage?.lineage)
    .slice()
    .sort((left, right) => (
      (canonicalInteger(left.sequence_no) || 0) - (canonicalInteger(right.sequence_no) || 0)
      || text(left.id).localeCompare(text(right.id))
    ));
}

export function decisionPackageSource(decisionPackage, event = null) {
  const anchor = record(decisionPackage?.anchor);
  const selectedOption = record(anchor.selected_option);
  return {
    package_id: text(decisionPackage?.package_id) || text(anchor.user_decision_id),
    package_state: text(decisionPackage?.state) || "stale",
    package_integrity_ok: decisionPackage?.integrity_ok === true && anchor.integrity_ok === true,
    user_decision_id: text(anchor.user_decision_id),
    artifact_id: text(anchor.artifact_id),
    artifact_version: canonicalInteger(anchor.artifact_version, { positive: true }) || 0,
    action: text(anchor.action),
    decision_version: text(anchor.decision_version),
    ai_preferred_option_id: text(anchor.ai_preferred_option_id),
    selected_option_id: text(anchor.selected_option_id),
    selected_is_ai_preferred: anchor.selected_is_ai_preferred === true,
    preferred_option_id: text(anchor.selected_option_id) || text(anchor.preferred_option_id),
    selected_option_title: text(selectedOption.title) || text(selectedOption.name) || "已选候选方案",
    candidate_simulation_seed: anchor.candidate_simulation_seed || null,
    relation_type: text(event?.relation_type),
    resource_revision: eventRevision(event),
    derivation_note: text(event?.relation_note),
  };
}

export function buildPortfolioLineageIndex(decisionPackages) {
  const index = new Map();
  for (const decisionPackage of decisionLineageRows(decisionPackages)) {
    for (const event of packageEvents(decisionPackage)) {
      if (!isPortfolioEvent(event) || !text(event.resource_id)) continue;
      const resourceId = text(event.resource_id);
      const entries = index.get(resourceId) || [];
      entries.push({ decisionPackage, event });
      index.set(resourceId, entries);
    }
  }
  for (const entries of index.values()) {
    entries.sort((left, right) => (
      (canonicalInteger(right.event.sequence_no) || 0)
      - (canonicalInteger(left.event.sequence_no) || 0)
    ));
  }
  return index;
}

export function latestResourceEvents(events, predicate) {
  const byResource = new Map();
  for (const event of decisionLineageRows(events)) {
    const resourceId = text(event.resource_id);
    if (!predicate(event) || !resourceId) continue;
    byResource.set(resourceId, event);
  }
  return [...byResource.values()];
}

export function walkForwardState(run) {
  const result = record(run?.result);
  const summary = record(result.summary);
  const status = text(summary.adequacy_status) || text(summary.status) || text(result.state) || "insufficient";
  const folds = canonicalInteger(summary.non_overlapping_test_fold_count)
    ?? canonicalInteger(summary.independent_fold_count)
    ?? 0;
  return {
    label: status === "sufficient" ? "样本达到最低门槛" : "历史样本仍不足",
    tone: status === "sufficient" ? "ready" : "pending",
    detail: `${folds} 个非重叠窗口 · 固定纸面方案回放`,
  };
}

export function decisionPackageKey(decisionPackage, index = 0) {
  const anchor = record(decisionPackage?.anchor);
  return JSON.stringify([
    text(decisionPackage?.package_id),
    text(anchor.user_decision_id),
    text(anchor.decision_version),
    canonicalInteger(anchor.artifact_version, { positive: true }) || 0,
    index,
  ]);
}

export function buildDecisionLineagePanelModel({
  decisionPackages,
  members,
  paperPortfolios,
  observations,
  walkForwardRunsByPortfolio,
} = {}) {
  const issues = [];
  const packages = normalizedRows(decisionPackages, "决策包", issues).slice();
  const memberRows = normalizedRows(members, "成员", issues);
  const portfolioRows = normalizedRows(paperPortfolios, "模拟组合", issues);
  const observationRows = normalizedRows(observations, "观察与提案", issues);
  validateUniqueIdentities(
    packages,
    "决策包",
    (item) => text(item.package_id) || text(item.anchor?.user_decision_id),
    issues,
  );
  validateUniqueIdentities(observationRows, "观察与提案", (item) => text(item.id), issues);
  packages.sort((left, right) => {
    const priority = { chain_broken: 0, active: 1, non_actionable: 2, stale: 3 };
    const leftTime = timestampMilliseconds(left?.anchor?.created_at) || 0;
    const rightTime = timestampMilliseconds(right?.anchor?.created_at) || 0;
    return (priority[left.state] ?? 9) - (priority[right.state] ?? 9) || rightTime - leftTime;
  });
  for (const item of packages) {
    if (item.lineage !== null && item.lineage !== undefined && !Array.isArray(item.lineage)) {
      issues.push(`决策包 ${text(item.package_id) || "unknown"} 的谱系必须是数组`);
      continue;
    }
    const lineage = normalizedRows(
      item.lineage,
      `决策包 ${text(item.package_id) || "unknown"} 的谱系`,
      issues,
    );
    validateUniqueIdentities(
      lineage,
      `决策包 ${text(item.package_id) || "unknown"} 的谱系事件`,
      (event) => text(event.id),
      issues,
    );
  }
  const membersById = new Map();
  for (const member of memberRows) {
    const id = text(member.id);
    if (!id || membersById.has(id)) {
      issues.push("成员身份缺失或重复");
      continue;
    }
    membersById.set(id, member);
  }
  const portfolioById = new Map();
  for (const portfolio of portfolioRows) {
    const id = text(portfolio.id);
    if (!id || portfolioById.has(id)) {
      issues.push("模拟组合身份缺失或重复");
      continue;
    }
    portfolioById.set(id, portfolio);
  }
  const linkedPortfolioIds = new Set();
  const linkedObservationIds = new Set();
  const linkedPortfolioRevisions = new Map();
  for (const decisionPackage of packages) {
    for (const event of packageEvents(decisionPackage)) {
      if (isPortfolioEvent(event)) {
        const resourceId = text(event.resource_id);
        if (!resourceId) continue;
        linkedPortfolioIds.add(resourceId);
        const revisions = linkedPortfolioRevisions.get(resourceId) || new Set();
        revisions.add(canonicalInteger(eventRevision(event), { positive: true }) || 0);
        linkedPortfolioRevisions.set(resourceId, revisions);
      }
      if (isObservationEvent(event) && text(event.resource_id)) {
        linkedObservationIds.add(text(event.resource_id));
      }
    }
  }
  const unlinkedPortfolios = portfolioRows.filter((item) => !linkedPortfolioIds.has(text(item.id)));
  const unlinkedObservations = observationRows.filter((item) => !linkedObservationIds.has(text(item.id)));
  const normalizedRuns = {};
  let historicalWalkForwardCount = 0;
  let unlinkedWalkForwardCount = 0;
  const runMapMissing = walkForwardRunsByPortfolio === null || walkForwardRunsByPortfolio === undefined;
  const runMapValid = runMapMissing || isPlainRecord(walkForwardRunsByPortfolio);
  const runMap = runMapValid && !runMapMissing ? walkForwardRunsByPortfolio : {};
  if (!runMapValid) {
    issues.push("历史回放集合必须是对象映射");
  }
  for (const [portfolioId, rawRuns] of Object.entries(runMap)) {
    const runs = normalizedRows(rawRuns, `组合 ${portfolioId} 的历史回放`, issues);
    normalizedRuns[portfolioId] = runs;
    const revisions = linkedPortfolioRevisions.get(portfolioId);
    for (const run of runs) {
      if (!revisions) unlinkedWalkForwardCount += 1;
      else if (!revisions.has(runPortfolioVersion(run))) historicalWalkForwardCount += 1;
    }
  }
  const current = packages.filter((item) => item.state !== "stale");
  const historical = packages.filter((item) => item.state === "stale");
  const hasUnlinked = Boolean(
    unlinkedPortfolios.length
    || unlinkedObservations.length
    || historicalWalkForwardCount
    || unlinkedWalkForwardCount,
  );
  return {
    current,
    hasUnlinked,
    historical,
    historicalWalkForwardCount,
    integrityOk: issues.length === 0,
    issues: [...new Set(issues)],
    membersById,
    portfolioById,
    stats: {
      current: current.length,
      historical: historical.length,
      broken: packages.filter((item) => item.state === "chain_broken" || item.integrity_ok !== true).length,
      unlinked: unlinkedPortfolios.length + unlinkedObservations.length + historicalWalkForwardCount + unlinkedWalkForwardCount,
    },
    unlinkedObservations,
    unlinkedPortfolios,
    unlinkedWalkForwardCount,
    walkForwardRunsByPortfolio: normalizedRuns,
  };
}
