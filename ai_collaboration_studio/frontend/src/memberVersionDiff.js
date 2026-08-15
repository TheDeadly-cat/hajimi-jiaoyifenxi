const FIELD_READERS = [
  ["name", "成员名称", (snapshot) => textValue(snapshot.name)],
  ["identity", "身份定位", (snapshot) => textValue(snapshot.identity)],
  ["responsibilities", "职责", (snapshot) => textValue(snapshot.responsibilities)],
  ["boundaries", "边界", (snapshot) => textValue(snapshot.boundaries)],
  ["instructions", "系统指令", (snapshot) => textValue(snapshot.instructions)],
  ["stance", "讨论立场", (snapshot) => textValue(snapshot.stance)],
  ["workflow_stage", "工作流阶段", (snapshot) => textValue(snapshot.workflow_stage)],
  ["provider", "模型服务商", (snapshot) => textValue(snapshot.provider)],
  ["model", "模型", (snapshot) => textValue(snapshot.model)],
  ["enabled", "参与状态", (snapshot) => (snapshot.enabled ? "参与讨论" : "暂停参与")],
  ["archived", "生命周期", (snapshot) => (isArchived(snapshot) ? "已归档" : "活动")],
  ["avatar_color", "头像颜色", (snapshot) => textValue(snapshot.avatar_color)],
];

function textValue(value) {
  if (value === null || value === undefined) return "";
  return typeof value === "string" ? value : String(value);
}

function isArchived(snapshot) {
  const archivedAt = Number(snapshot?.archived_at || 0);
  return snapshot?.archived === true || (Number.isFinite(archivedAt) && archivedAt > 0);
}

function parseCapabilities(value) {
  if (Array.isArray(value)) return value;
  if (typeof value !== "string" || !value.trim()) return [];
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function normalizedCapabilities(snapshot) {
  return [...new Set(
    parseCapabilities(snapshot?.capabilities ?? snapshot?.capabilities_json)
      .map((item) => String(item || "").trim())
      .filter(Boolean),
  )].sort((left, right) => left.localeCompare(right));
}

export function memberVersionSnapshot(record) {
  const candidate = record?.member_version || record || {};
  return candidate.snapshot && typeof candidate.snapshot === "object"
    ? candidate.snapshot
    : candidate;
}

export function buildMemberVersionDiff(leftRecord, rightRecord) {
  const leftSnapshot = memberVersionSnapshot(leftRecord);
  const rightSnapshot = memberVersionSnapshot(rightRecord);
  const fieldChanges = FIELD_READERS.flatMap(([key, label, read]) => {
    const before = read(leftSnapshot);
    const after = read(rightSnapshot);
    return before === after ? [] : [{ key, label, before, after }];
  });
  const beforeCapabilities = normalizedCapabilities(leftSnapshot);
  const afterCapabilities = normalizedCapabilities(rightSnapshot);
  const beforeSet = new Set(beforeCapabilities);
  const afterSet = new Set(afterCapabilities);
  const added = afterCapabilities.filter((capability) => !beforeSet.has(capability));
  const removed = beforeCapabilities.filter((capability) => !afterSet.has(capability));
  const capabilities = {
    before: beforeCapabilities,
    after: afterCapabilities,
    added,
    removed,
    changed: Boolean(added.length || removed.length),
  };

  return {
    fieldChanges,
    capabilities,
    changed: Boolean(fieldChanges.length || capabilities.changed),
  };
}

export function formatMemberVersionTime(value) {
  if (!value) return "时间未记录";
  const numericValue = Number(value);
  const date = Number.isFinite(numericValue) ? new Date(numericValue) : new Date(value);
  if (Number.isNaN(date.getTime())) return "时间未记录";
  return date.toLocaleString("zh-CN", { hour12: false });
}
