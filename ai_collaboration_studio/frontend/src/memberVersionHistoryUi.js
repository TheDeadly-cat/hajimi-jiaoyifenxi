function objectRow(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : null;
}

export function memberHistoryText(value, fallback = "") {
  if (typeof value === "string" && value.trim()) return value.trim();
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return fallback;
}

export function memberHistoryVersionNumber(record) {
  const row = objectRow(record);
  const snapshot = objectRow(row?.snapshot);
  const value = Number(row?.version ?? snapshot?.version ?? 0);
  return Number.isSafeInteger(value) && value > 0 ? value : 0;
}

export function memberHistoryIntegrityState(record) {
  if (record?.integrity_ok === true) return { label: "冻结快照", tone: "ok" };
  if (record?.integrity_ok === false) return { label: "快照异常", tone: "warning" };
  return { label: "完整性未记录", tone: "unknown" };
}

export function memberHistorySelectableRows(rows) {
  const source = Array.isArray(rows) ? rows : [];
  const counts = new Map();
  for (const row of source) {
    const version = memberHistoryVersionNumber(row);
    if (version) counts.set(version, (counts.get(version) || 0) + 1);
  }
  return source.filter((row) => {
    const version = memberHistoryVersionNumber(row);
    return row?.integrity_ok === true && version > 0 && counts.get(version) === 1;
  });
}

export function memberHistoryIdentity(roomId, member) {
  const room = memberHistoryText(roomId);
  const memberId = memberHistoryText(member?.id);
  const currentVersion = memberHistoryVersionNumber({ version: member?.version });
  const issue = !room
    ? "房间标识缺失，不能读取成员版本历史。"
    : !memberId
      ? "成员标识缺失，不能读取版本历史。"
      : !currentVersion
        ? "成员当前版本无效，不能绑定历史请求。"
        : "";
  return {
    roomId: room,
    memberId,
    currentVersion,
    name: memberHistoryText(member?.name, "成员"),
    integrityOk: !issue,
    issue,
  };
}

export function memberVersionListProjection(data, { fallbackMember = null } = {}) {
  const payload = objectRow(data);
  if (!payload) {
    return { ok: false, error: "成员版本历史响应不是对象。", rows: [], selectableRows: [] };
  }
  if (!Array.isArray(payload.versions)) {
    return { ok: false, error: "成员版本历史响应缺少 versions 数组。", rows: [], selectableRows: [] };
  }

  const issues = [];
  const rows = [];
  const versionCounts = new Map();
  for (const [index, candidate] of payload.versions.entries()) {
    const row = objectRow(candidate);
    if (!row) {
      issues.push(`第 ${index + 1} 条版本记录不是对象。`);
      continue;
    }
    const version = memberHistoryVersionNumber(row);
    if (!version) issues.push(`第 ${index + 1} 条版本记录缺少有效版本号。`);
    if (version) versionCounts.set(version, (versionCounts.get(version) || 0) + 1);
    rows.push(row);
  }
  for (const [version, count] of versionCounts.entries()) {
    if (count > 1) issues.push(`版本历史包含 ${count} 条 v${version}，已禁止精确比较。`);
  }

  const selectableRows = memberHistorySelectableRows(rows);
  const memberMeta = objectRow(payload.member) || objectRow(fallbackMember) || {};
  const fallbackVersion = memberHistoryVersionNumber({ version: fallbackMember?.version });
  const currentVersion = memberHistoryVersionNumber({ version: memberMeta.current_version }) || fallbackVersion;
  const target = selectableRows.find((row) => memberHistoryVersionNumber(row) === currentVersion)
    || selectableRows[0];
  const targetVersion = memberHistoryVersionNumber(target);
  const base = selectableRows.find((row) => memberHistoryVersionNumber(row) < targetVersion)
    || selectableRows.find((row) => memberHistoryVersionNumber(row) !== targetVersion)
    || target;

  return {
    ok: true,
    rows,
    selectableRows,
    memberMeta,
    currentVersion,
    targetVersion,
    baseVersion: memberHistoryVersionNumber(base),
    integrityOk: issues.length === 0,
    warning: issues[0] || "",
  };
}

export function memberVersionPairProjection(baseData, targetData, { baseVersion, targetVersion }) {
  const basePayload = objectRow(baseData);
  const targetPayload = objectRow(targetData);
  const left = objectRow(basePayload?.member_version);
  const right = objectRow(targetPayload?.member_version);
  if (!left || !right) {
    return { ok: false, error: "成员身份版本快照响应不完整。" };
  }
  const leftVersion = memberHistoryVersionNumber(left);
  const rightVersion = memberHistoryVersionNumber(right);
  if (leftVersion !== Number(baseVersion) || rightVersion !== Number(targetVersion)) {
    return { ok: false, error: "成员身份版本快照与请求版本不一致。" };
  }
  if (left.integrity_ok !== true || right.integrity_ok !== true) {
    return { ok: false, error: "成员身份版本快照未通过完整性验证。" };
  }
  return { ok: true, pair: { left, right } };
}

export function memberHistoryErrorMessage(error, fallback) {
  if (error instanceof Error && typeof error.message === "string" && error.message.trim()) {
    return error.message.trim();
  }
  if (typeof error === "string" && error.trim()) return error.trim();
  return fallback;
}
