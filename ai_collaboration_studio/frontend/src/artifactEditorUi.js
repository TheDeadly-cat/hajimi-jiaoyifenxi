export function artifactEditorRecord(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

export function artifactEditorRows(value) {
  return Array.isArray(value) ? value : [];
}

export function artifactEditorFieldText(value, fallback = "") {
  if (typeof value === "string") return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return fallback;
}

export function artifactEditorDisplayText(value, fallback = "") {
  const text = artifactEditorFieldText(value).trim();
  return text || fallback;
}

function positiveVersion(value) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number > 0 ? number : 0;
}

export function artifactEditorSourceState(value) {
  const source = artifactEditorRecord(value);
  const content = artifactEditorRecord(source.content);
  const issues = [];
  if (value !== source || !Object.keys(source).length) issues.push("产物来源不是有效对象。");
  if (!artifactEditorDisplayText(source.id)) issues.push("产物标识缺失。");
  if (!positiveVersion(source.version)) issues.push("产物版本无效。");
  if (source.content != null && source.content !== content) issues.push("产物内容不是对象。");
  if (source.title != null && typeof source.title !== "string") issues.push("产物标题不是文本。");
  if (content.summary != null && typeof content.summary !== "string") issues.push("会议摘要不是文本。");
  for (const section of ["requirements", "risks", "conclusions", "disagreements", "unknowns", "actions"]) {
    if (content[section] != null && !Array.isArray(content[section])) {
      issues.push(`${section} 不是数组，已按空列表显示。`);
    }
  }
  if (content.decision != null && content.decision !== artifactEditorRecord(content.decision)) {
    issues.push("决策板不是对象，已按未决定显示。");
  }
  const normalizedContent = {
    ...content,
    summary: artifactEditorFieldText(content.summary),
    requirements: artifactEditorRows(content.requirements),
    risks: artifactEditorRows(content.risks),
    conclusions: artifactEditorRows(content.conclusions),
    disagreements: artifactEditorRows(content.disagreements),
    unknowns: artifactEditorRows(content.unknowns),
    actions: artifactEditorRows(content.actions),
    decision: artifactEditorRecord(content.decision),
  };
  return {
    artifact: {
      ...source,
      id: artifactEditorDisplayText(source.id),
      version: positiveVersion(source.version),
      title: artifactEditorFieldText(source.title, "会议纪要"),
      status: artifactEditorDisplayText(source.status, "DRAFT").toUpperCase(),
      round_id: artifactEditorDisplayText(source.round_id),
      content: normalizedContent,
    },
    integrityOk: issues.length === 0,
    issues,
  };
}

export function artifactEditorIdentity(artifact, room) {
  const artifactId = artifactEditorDisplayText(artifact?.id);
  const artifactVersion = positiveVersion(artifact?.version);
  const roomId = artifactEditorDisplayText(room?.id);
  const artifactOk = Boolean(artifactId && artifactVersion);
  const roomOk = Boolean(roomId);
  return {
    artifactId,
    artifactVersion,
    roomId,
    artifactOk,
    roomOk,
    integrityOk: artifactOk && roomOk,
    issue: !artifactId
      ? "产物标识缺失。"
      : !artifactVersion
        ? "产物版本无效。"
        : !roomId
          ? "房间标识缺失。"
          : "",
  };
}

export function artifactEditorErrorMessage(error, fallback) {
  if (error instanceof Error && typeof error.message === "string" && error.message.trim()) {
    return error.message.trim();
  }
  if (typeof error === "string" && error.trim()) return error.trim();
  return fallback;
}

export function artifactEditorSavedState(value, expectedArtifact) {
  const source = artifactEditorSourceState(value);
  const expectedId = artifactEditorDisplayText(expectedArtifact?.id);
  if (!source.artifact.id || source.artifact.id !== expectedId) {
    return { ok: false, error: "保存响应的产物标识与当前编辑对象不一致。", artifact: null };
  }
  if (!source.artifact.version || source.artifact.version < positiveVersion(expectedArtifact?.version)) {
    return { ok: false, error: "保存响应的产物版本无效或发生倒退。", artifact: null };
  }
  return { ok: true, error: "", artifact: source.artifact, sourceIntegrityOk: source.integrityOk };
}

export function artifactEditorMutationControl({
  action,
  identity,
  title,
  summary,
  busy,
  inFlight,
  evidenceBlocked,
  confirmDisabledReason,
  saveHandlerAvailable,
  confirmHandlerAvailable,
  exportHandlerAvailable,
}) {
  const mutating = ["progress", "draft", "confirm"].includes(action);
  const confirming = action === "confirm";
  const exporting = action === "export";
  const handlerAvailable = exporting
    ? exportHandlerAvailable === true
    : confirming
      ? confirmHandlerAvailable === true
      : saveHandlerAvailable === true;
  const checks = [
    { id: "identity", ok: exporting ? identity?.artifactOk === true : identity?.integrityOk === true, label: identity?.issue || "产物身份不可用。" },
    { id: "content", ok: !mutating || Boolean(artifactEditorDisplayText(title) && artifactEditorDisplayText(summary)), label: "填写标题和会议摘要后才能保存。" },
    { id: "evidence", ok: !mutating || evidenceBlocked !== true, label: "权威证据来源尚未允许变更。" },
    { id: "confirmation", ok: !confirming || !artifactEditorDisplayText(confirmDisabledReason), label: artifactEditorDisplayText(confirmDisabledReason, "当前不能确认产物。") },
    { id: "handler", ok: handlerAvailable, label: `${exporting ? "导出" : confirming ? "确认" : "保存"}处理器不可用。` },
    { id: "idle", ok: busy !== true && inFlight !== true, label: "已有产物操作正在进行。" },
  ];
  const failed = checks.find((check) => !check.ok);
  return {
    checks,
    canRun: !failed,
    phase: busy || inFlight ? "running" : failed ? "blocked" : "ready",
    instruction: failed?.label || `${exporting ? "导出" : confirming ? "确认" : "保存"}操作已通过本地前置检查。`,
  };
}
