const FIELD_READERS = [
  ["title", "资料标题", (snapshot) => textValue(snapshot?.title)],
  ["kind", "资料类型", (snapshot) => textValue(snapshot?.kind || "note")],
  ["source_url", "来源 URL", (snapshot) => textValue(snapshot?.source_url)],
  ["content", "正文", (snapshot) => textValue(snapshot?.content)],
  ["active", "可用状态", (snapshot) => (snapshot?.active === false ? "已停用" : "当前可用")],
  ["source_type", "来源类型", (snapshot) => textValue(snapshot?.metadata?.source_type)],
  ["event_type", "事件类型", (snapshot) => textValue(snapshot?.metadata?.event_type)],
  ["publisher", "发布者", (snapshot) => textValue(snapshot?.metadata?.publisher)],
  ["published_at", "发布时间", (snapshot) => textValue(snapshot?.metadata?.published_at)],
  ["original_name", "原始文件名", (snapshot) => textValue(snapshot?.metadata?.original_name)],
  ["final_url", "抓取后的 URL", (snapshot) => textValue(snapshot?.metadata?.final_url)],
  ["extraction_method", "提取方式", (snapshot) => textValue(snapshot?.metadata?.extraction_method)],
  ["source_sha256", "来源哈希", (snapshot) => textValue(snapshot?.metadata?.source_sha256)],
  ["content_sha256", "正文哈希", (snapshot) => textValue(snapshot?.metadata?.content_sha256)],
  ["source_bytes", "来源字节数", (snapshot) => numericText(snapshot?.metadata?.source_bytes)],
  ["truncated", "正文截断", (snapshot) => (snapshot?.metadata?.truncated ? "是" : "否")],
  ["risk_flagged", "风险检测结果", (snapshot) => (
    snapshot?.metadata?.prompt_injection_risk?.flagged ? "检测到风险标签" : "未检测到风险标签"
  )],
  ["risk_scanner", "风险检测规则", (snapshot) => textValue(snapshot?.metadata?.prompt_injection_risk?.scanner)],
];

function textValue(value) {
  if (value === null || value === undefined) return "";
  return typeof value === "string" ? value : String(value);
}

function numericText(value) {
  if (value === null || value === undefined || value === "") return "";
  const number = Number(value);
  return Number.isFinite(number) ? number.toLocaleString("zh-CN") : textValue(value);
}

function normalizeStringSet(value) {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.map((item) => String(item || "").trim()).filter(Boolean))]
    .sort((left, right) => left.localeCompare(right));
}

function compareStringSet(beforeValue, afterValue) {
  const before = normalizeStringSet(beforeValue);
  const after = normalizeStringSet(afterValue);
  const beforeSet = new Set(before);
  const afterSet = new Set(after);
  const added = after.filter((value) => !beforeSet.has(value));
  const removed = before.filter((value) => !afterSet.has(value));
  return {
    before,
    after,
    added,
    removed,
    changed: Boolean(added.length || removed.length),
  };
}

export function materialVersionSnapshot(record) {
  const candidate = record?.material_version || record?.material || record || {};
  return candidate.snapshot && typeof candidate.snapshot === "object"
    ? candidate.snapshot
    : candidate;
}

export function buildMaterialVersionDiff(leftRecord, rightRecord) {
  const leftSnapshot = materialVersionSnapshot(leftRecord);
  const rightSnapshot = materialVersionSnapshot(rightRecord);
  const fieldChanges = FIELD_READERS.flatMap(([key, label, read]) => {
    const before = read(leftSnapshot);
    const after = read(rightSnapshot);
    return before === after ? [] : [{ key, label, before, after }];
  });
  const symbols = compareStringSet(
    leftSnapshot?.metadata?.symbols,
    rightSnapshot?.metadata?.symbols,
  );
  const riskFlags = compareStringSet(
    leftSnapshot?.metadata?.prompt_injection_risk?.flags,
    rightSnapshot?.metadata?.prompt_injection_risk?.flags,
  );

  return {
    fieldChanges,
    symbols,
    riskFlags,
    changed: Boolean(fieldChanges.length || symbols.changed || riskFlags.changed),
  };
}

export function formatMaterialVersionTime(value) {
  if (!value) return "时间未记录";
  const numericValue = Number(value);
  const date = Number.isFinite(numericValue) ? new Date(numericValue) : new Date(value);
  if (Number.isNaN(date.getTime())) return "时间未记录";
  return date.toLocaleString("zh-CN", { hour12: false });
}

