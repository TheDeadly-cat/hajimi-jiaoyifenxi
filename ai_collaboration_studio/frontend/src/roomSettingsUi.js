import { stockRoomScopeInputValue } from "./stockResearch.js";

const DISCUSSION_MODES = new Set(["dynamic", "sequential"]);
const IDLE_RESPONSE_MODES = new Set(["stored_only", "mention_only", "moderator_auto"]);

function cleanText(value, fallback = "") {
  return typeof value === "string" ? value : fallback;
}

function positiveVersion(value) {
  return Number.isSafeInteger(value) && value > 0;
}

export function roomSettingsRows(value) {
  return Array.isArray(value) ? value : [];
}

export function roomSettingsErrorMessage(error, fallback) {
  if (error instanceof Error && error.message.trim()) return error.message.trim();
  if (typeof error === "string" && error.trim()) return error.trim();
  return fallback;
}

export function roomSettingsPackSelection(value) {
  if (value == null) {
    return { ids: [], integrityOk: true, issue: "" };
  }
  if (!Array.isArray(value)) {
    return {
      ids: [],
      integrityOk: false,
      issue: "能力包选择不是数组，已按空选择显示。",
    };
  }

  const ids = [];
  const seen = new Set();
  const issues = [];
  for (const item of value) {
    if (typeof item !== "string" || !item.trim()) {
      issues.push("能力包选择包含无效标识。");
      continue;
    }
    const id = item.trim();
    if (seen.has(id)) {
      issues.push(`能力包选择包含重复项：${id}`);
      continue;
    }
    seen.add(id);
    ids.push(id);
  }
  return {
    ids,
    integrityOk: issues.length === 0,
    issue: issues[0] || "",
  };
}

export function roomSettingsInitialState(room) {
  const source = room && typeof room === "object" ? room : {};
  const selection = roomSettingsPackSelection(source.capability_pack_ids);
  const issues = [];

  if (typeof source.id !== "string" || !source.id.trim()) issues.push("房间标识缺失。");
  if (!positiveVersion(source.settings_version)) issues.push("设置版本无效。");
  if (!selection.integrityOk) issues.push(selection.issue);

  let stockRoomScopeInput = "";
  try {
    stockRoomScopeInput = stockRoomScopeInputValue(source.stock_room_scope);
  } catch {
    issues.push("股票研究范围无法解析。");
  }

  return {
    form: {
      title: cleanText(source.title),
      objective: cleanText(source.objective),
      category: cleanText(source.category, "通用共创") || "通用共创",
      discussion_mode: cleanText(source.discussion_mode, "dynamic") || "dynamic",
      moderator_member_id: cleanText(source.moderator_member_id),
      idle_response_mode: cleanText(source.idle_response_mode, "mention_only") || "mention_only",
      capability_pack_ids: selection.ids,
      stock_room_scope_input: stockRoomScopeInput,
    },
    integrityOk: issues.length === 0,
    issues,
  };
}

export function sameRoomPackSelection(left, right) {
  const a = roomSettingsPackSelection(left);
  const b = roomSettingsPackSelection(right);
  if (!a.integrityOk || !b.integrityOk || a.ids.length !== b.ids.length) return false;
  const expected = [...a.ids].sort();
  const actual = [...b.ids].sort();
  return expected.every((id, index) => id === actual[index]);
}

export function roomSettingsVersionHistory(result) {
  const source = result && typeof result === "object" ? result.versions : null;
  if (!Array.isArray(source)) {
    return {
      rows: [],
      integrityOk: false,
      issue: "版本历史响应缺少 versions 数组。",
    };
  }

  const rows = [];
  const seen = new Set();
  let issue = "";
  for (const row of source) {
    const version = row && typeof row === "object" ? row.version : null;
    if (!positiveVersion(version)) {
      issue ||= "版本历史包含无效版本号。";
      continue;
    }
    if (seen.has(version)) {
      issue ||= `版本历史包含重复版本：v${version}。`;
      continue;
    }
    seen.add(version);
    rows.push(row);
  }
  return { rows, integrityOk: !issue, issue };
}

export function roomSettingsSaveControl({
  sourceIntegrityOk,
  form,
  room,
  busy,
  packSelectionBlocked,
  stockScopeBlocked,
  moderatorSelectionMissing,
  submitHandlerAvailable,
  closeHandlerAvailable,
}) {
  const title = cleanText(form?.title).trim();
  const objective = cleanText(form?.objective).trim();
  const category = cleanText(form?.category).trim();
  const packs = roomSettingsPackSelection(form?.capability_pack_ids);
  const checks = [
    { id: "source", label: "房间来源完整", ok: sourceIntegrityOk === true },
    { id: "identity", label: "房间与版本有效", ok: typeof room?.id === "string" && Boolean(room.id.trim()) && positiveVersion(room?.settings_version) },
    { id: "title", label: "标题为 1-80 个字符", ok: title.length >= 1 && title.length <= 80 },
    { id: "objective", label: "目标已填写", ok: objective.length > 0 },
    { id: "category", label: "分类已填写", ok: category.length > 0 },
    { id: "discussion", label: "讨论模式有效", ok: DISCUSSION_MODES.has(form?.discussion_mode) },
    { id: "idle", label: "空闲响应模式有效", ok: IDLE_RESPONSE_MODES.has(form?.idle_response_mode) },
    { id: "packs", label: "能力包选择完整", ok: packs.integrityOk },
    { id: "moderator", label: "主持人仍在成员列表", ok: moderatorSelectionMissing !== true },
    { id: "lifecycle", label: "能力包生命周期允许变更", ok: packSelectionBlocked !== true },
    { id: "stock", label: "股票研究范围有效", ok: stockScopeBlocked !== true },
    { id: "handlers", label: "保存与关闭处理器可用", ok: submitHandlerAvailable === true && closeHandlerAvailable === true },
    { id: "idle-state", label: "当前没有保存任务", ok: busy !== true },
  ];
  const failed = checks.find((check) => !check.ok);
  return {
    checks,
    canSubmit: !failed,
    phase: busy ? "saving" : failed ? "blocked" : "ready",
    instruction: busy ? "正在保存设置，请等待当前请求完成。" : failed ? failed.label : "设置已通过本地保存前检查。",
  };
}
