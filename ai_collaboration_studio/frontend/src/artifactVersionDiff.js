const SECTION_LABELS = {
  requirements: "需求",
  risks: "风险",
  conclusions: "结论",
  disagreements: "分歧",
  unknowns: "待验证",
  actions: "待办",
  decision_options: "候选方案",
};

const SCALAR_PATHS = [
  ["title", "产物标题", (snapshot) => snapshot?.title || ""],
  ["status", "产物状态", (snapshot) => snapshot?.status || "DRAFT"],
  ["generation_source", "生成来源", (snapshot) => snapshot?.generation_source || ""],
  ["summary", "会议摘要", (snapshot) => snapshot?.content?.summary || ""],
  ["generation_notes", "生成说明", (snapshot) => snapshot?.content?.generation_notes || ""],
  ["decision_status", "决策状态", (snapshot) => snapshot?.content?.decision?.status || "undecided"],
  ["preferred_option_id", "首选方案", (snapshot) => snapshot?.content?.decision?.preferred_option_id || ""],
  ["decision_rationale", "选择理由", (snapshot) => snapshot?.content?.decision?.rationale || ""],
];

function stableValue(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

function sectionItems(snapshot, section) {
  if (section === "decision_options") {
    return Array.isArray(snapshot?.content?.decision?.options)
      ? snapshot.content.decision.options
      : [];
  }
  return Array.isArray(snapshot?.content?.[section]) ? snapshot.content[section] : [];
}

function itemLabel(item, fallback) {
  return String(item?.title || item?.text || item?.name || fallback || "未命名条目");
}

function itemFields(item) {
  return Object.fromEntries(
    Object.entries(item || {})
      .filter(([key]) => !["id", "evidence"].includes(key))
      .map(([key, value]) => [key, stableValue(value)]),
  );
}

function compareSection(leftSnapshot, rightSnapshot, section) {
  const left = sectionItems(leftSnapshot, section);
  const right = sectionItems(rightSnapshot, section);
  const leftById = new Map(left.map((item, index) => [String(item?.id || `legacy_${index}`), item]));
  const rightById = new Map(right.map((item, index) => [String(item?.id || `legacy_${index}`), item]));
  const added = [];
  const removed = [];
  const changed = [];
  const unchanged = [];

  for (const [id, item] of rightById) {
    if (!leftById.has(id)) {
      added.push({ id, label: itemLabel(item, id) });
      continue;
    }
    const before = leftById.get(id);
    const beforeFields = itemFields(before);
    const afterFields = itemFields(item);
    const fieldNames = [...new Set([...Object.keys(beforeFields), ...Object.keys(afterFields)])];
    const fieldChanges = fieldNames
      .filter((field) => beforeFields[field] !== afterFields[field])
      .map((field) => ({ field, before: beforeFields[field] || "", after: afterFields[field] || "" }));
    const evidenceChanged = stableValue(before?.evidence || []) !== stableValue(item?.evidence || []);
    if (fieldChanges.length || evidenceChanged) {
      changed.push({
        id,
        label: itemLabel(item, itemLabel(before, id)),
        fieldChanges,
        evidenceChanged,
      });
    } else {
      unchanged.push({ id, label: itemLabel(item, id) });
    }
  }
  for (const [id, item] of leftById) {
    if (!rightById.has(id)) removed.push({ id, label: itemLabel(item, id) });
  }

  const commonLeft = left.map((item, index) => String(item?.id || `legacy_${index}`)).filter((id) => rightById.has(id));
  const commonRight = right.map((item, index) => String(item?.id || `legacy_${index}`)).filter((id) => leftById.has(id));
  return {
    key: section,
    label: SECTION_LABELS[section],
    added,
    removed,
    changed,
    unchanged,
    reordered: commonLeft.join("|") !== commonRight.join("|"),
  };
}

function evidenceTargets(snapshot) {
  const content = snapshot?.content || {};
  const targets = [["summary", content.summary_evidence || []]];
  for (const section of ["requirements", "risks", "conclusions", "disagreements", "unknowns", "actions"]) {
    (content[section] || []).forEach((item, index) => {
      targets.push([`${section}:${item?.id || `legacy_${index}`}`, item?.evidence || []]);
    });
  }
  const decision = content.decision || {};
  targets.push(["decision", decision.evidence || []]);
  (decision.options || []).forEach((item, index) => {
    targets.push([`decision_options:${item?.id || `legacy_${index}`}`, item?.evidence || []]);
  });
  const byIdentity = new Map();
  targets.forEach(([target, refs]) => {
    (Array.isArray(refs) ? refs : []).forEach((ref) => {
      const identity = `${target}|${ref?.type || ""}|${ref?.id || ""}`;
      byIdentity.set(identity, { target, identity, ref: ref || {} });
    });
  });
  return byIdentity;
}

function compareEvidence(leftSnapshot, rightSnapshot) {
  const left = evidenceTargets(leftSnapshot);
  const right = evidenceTargets(rightSnapshot);
  const added = [];
  const removed = [];
  const changed = [];
  for (const [identity, item] of right) {
    if (!left.has(identity)) {
      added.push(item);
      continue;
    }
    const before = left.get(identity);
    if (stableValue(before.ref) !== stableValue(item.ref)) {
      changed.push({ identity, target: item.target, before: before.ref, after: item.ref });
    }
  }
  for (const [identity, item] of left) {
    if (!right.has(identity)) removed.push(item);
  }
  return { added, removed, changed, unchanged: left.size - removed.length - changed.length };
}

export function buildArtifactVersionDiff(leftRecord, rightRecord) {
  const leftSnapshot = leftRecord?.snapshot || {};
  const rightSnapshot = rightRecord?.snapshot || {};
  const scalarChanges = SCALAR_PATHS.flatMap(([key, label, read]) => {
    const before = stableValue(read(leftSnapshot));
    const after = stableValue(read(rightSnapshot));
    return before === after ? [] : [{ key, label, before, after }];
  });
  const sections = Object.keys(SECTION_LABELS).map((section) => (
    compareSection(leftSnapshot, rightSnapshot, section)
  ));
  const evidence = compareEvidence(leftSnapshot, rightSnapshot);
  return {
    scalarChanges,
    sections,
    evidence,
    changed: Boolean(
      scalarChanges.length
      || sections.some((section) => section.added.length || section.removed.length || section.changed.length || section.reordered)
      || evidence.added.length
      || evidence.removed.length
      || evidence.changed.length
    ),
  };
}

export function formatArtifactVersionTime(value) {
  if (!value) return "时间未记录";
  return new Date(Number(value)).toLocaleString("zh-CN", { hour12: false });
}

