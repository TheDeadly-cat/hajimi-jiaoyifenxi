const FIELD_READERS = [
  ["title", "房间名称", (snapshot) => text(snapshot?.title)],
  ["objective", "长期目标", (snapshot) => text(snapshot?.objective)],
  ["domain", "领域标识", (snapshot) => text(snapshot?.domain)],
  ["category", "归属分类", (snapshot) => text(snapshot?.category)],
  ["template_id", "模板来源", (snapshot) => text(snapshot?.template_id)],
  ["discussion_mode", "讨论调度", (snapshot) => text(snapshot?.discussion_mode)],
  ["moderator_member_id", "动态主持 AI", (snapshot) => text(snapshot?.moderator_member_id)],
  ["idle_response_mode", "普通消息响应", (snapshot) => text(snapshot?.idle_response_mode)],
  ["plugin_registry_snapshot_sha256", "插件合同封印", (snapshot) => text(
    snapshot?.plugin_registry_snapshot_sha256
      || snapshot?.plugin_registry_snapshot?.registry_snapshot_sha256,
  )],
  ["workflow_policy", "流程政策", (snapshot) => stableJson(snapshot?.workflow_policy || {})],
];

function text(value) {
  if (value === null || value === undefined) return "";
  return typeof value === "string" ? value : String(value);
}

function sortObject(value) {
  if (Array.isArray(value)) return value.map(sortObject);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.keys(value).sort().map((key) => [key, sortObject(value[key])]),
  );
}

function stableJson(value) {
  return JSON.stringify(sortObject(value));
}

function stringSet(value) {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.map((item) => String(item || "").trim()).filter(Boolean))]
    .sort((left, right) => left.localeCompare(right));
}

function compareSet(beforeValue, afterValue) {
  const before = stringSet(beforeValue);
  const after = stringSet(afterValue);
  const beforeSet = new Set(before);
  const afterSet = new Set(after);
  const added = after.filter((item) => !beforeSet.has(item));
  const removed = before.filter((item) => !afterSet.has(item));
  return { before, after, added, removed, changed: Boolean(added.length || removed.length) };
}

export function roomVersionSnapshot(record) {
  const candidate = record?.room_version || record?.room || record || {};
  return candidate.snapshot && typeof candidate.snapshot === "object"
    ? candidate.snapshot
    : candidate;
}

export function buildRoomVersionDiff(leftRecord, rightRecord) {
  const left = roomVersionSnapshot(leftRecord);
  const right = roomVersionSnapshot(rightRecord);
  const fieldChanges = FIELD_READERS.flatMap(([key, label, read]) => {
    const before = read(left);
    const after = read(right);
    return before === after ? [] : [{ key, label, before, after }];
  });
  const capabilityPacks = compareSet(left?.capability_pack_ids, right?.capability_pack_ids);
  const capabilities = compareSet(left?.capabilities, right?.capabilities);
  return {
    fieldChanges,
    capabilityPacks,
    capabilities,
    changed: Boolean(fieldChanges.length || capabilityPacks.changed || capabilities.changed),
  };
}

export function formatRoomVersionTime(value) {
  const number = Number(value);
  const date = Number.isFinite(number) ? new Date(number) : new Date(value);
  return Number.isNaN(date.getTime())
    ? "时间未记录"
    : date.toLocaleString("zh-CN", { hour12: false });
}
