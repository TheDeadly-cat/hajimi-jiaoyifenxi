const ARTIFACT_SECTIONS = Object.freeze([
  "requirements",
  "risks",
  "conclusions",
  "disagreements",
  "unknowns",
  "actions",
]);
export const ARTIFACT_PANEL_MEMBER_LIMIT = 500;
export const ARTIFACT_PANEL_SECTION_LIMIT = 5000;
export const ARTIFACT_PANEL_VISIBLE_LIMIT = 20;

function record(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function array(value) {
  return Array.isArray(value) ? value : [];
}

function text(value, maxLength = 240) {
  if (typeof value !== "string") return "";
  const normalized = value.trim();
  return normalized && normalized.length <= maxLength ? normalized : "";
}

function displayText(value, maxLength = 400) {
  return typeof value === "string" ? value.trim().slice(0, maxLength) : "";
}

function count(value) {
  return Number.isSafeInteger(value) && value >= 0 ? value : 0;
}

export function artifactPanelControls({
  members = [],
  selectedSynthesizerId = "",
  loading = false,
  generationDisabled = false,
  generationHandlerAvailable = true,
} = {}) {
  const memberRows = array(members);
  const memberIntegrityOk = memberRows.length <= ARTIFACT_PANEL_MEMBER_LIMIT;
  const seen = new Set();
  const synthesizers = [];
  for (const rawMember of memberIntegrityOk ? memberRows : []) {
    const member = record(rawMember);
    const id = text(member.id);
    const provider = text(member.provider);
    if (member.enabled !== true || !id || !provider || seen.has(id)) continue;
    seen.add(id);
    synthesizers.push({
      ...member,
      id,
      name: displayText(member.name, 240) || id,
      provider,
      model: text(member.model, 240),
    });
  }
  const requestedId = text(selectedSynthesizerId);
  const activeSynthesizerId = synthesizers.some((member) => member.id === requestedId)
    ? requestedId
    : "";
  const generateDisabled = loading === true
    || generationDisabled === true
    || generationHandlerAvailable !== true
    || !memberIntegrityOk;
  const state = loading
    ? "loading"
    : generationDisabled || generationHandlerAvailable !== true || !memberIntegrityOk
      ? "blocked"
      : "ready";
  return {
    synthesizers,
    activeSynthesizerId,
    generateDisabled,
    state,
    memberIntegrityOk,
    issue: !memberIntegrityOk
      ? `成员列表超过 ${ARTIFACT_PANEL_MEMBER_LIMIT} 条安全上限，草稿生成已关闭。`
      : generationHandlerAvailable !== true
        ? "草稿生成处理器不可用。"
        : "",
    actionLabel: loading
      ? "正在整理"
      : !memberIntegrityOk
        ? "成员列表超限"
        : generationHandlerAvailable !== true
          ? "生成处理器不可用"
          : generationDisabled
            ? "讨论进行中"
            : "整理会议草稿",
  };
}

export function artifactPanelErrorMessage(error, fallback = "会议产物操作失败。") {
  const message = typeof error?.message === "string" ? error.message.trim().slice(0, 1000) : "";
  const safeFallback = typeof fallback === "string" ? fallback.trim().slice(0, 1000) : "";
  return message || safeFallback || "会议产物操作失败。";
}

export function artifactPanelRows(
  artifacts,
  { limit = 5, summarizeEvidence = () => ({}) } = {},
) {
  const safeLimit = Number.isSafeInteger(limit) && limit > 0
    ? Math.min(limit, ARTIFACT_PANEL_VISIBLE_LIMIT)
    : 5;
  const artifactRows = array(artifacts);
  const rows = artifactRows.slice(0, safeLimit).map((rawArtifact, index) => {
    const artifact = record(rawArtifact);
    const content = record(artifact.content);
    const decision = record(content.decision);
    const options = array(decision.options);
    const projectionLimited = ARTIFACT_SECTIONS.some(
      (section) => array(content[section]).length > ARTIFACT_PANEL_SECTION_LIMIT,
    ) || options.length > ARTIFACT_PANEL_SECTION_LIMIT;
    const itemCount = ARTIFACT_SECTIONS.reduce(
      (total, section) => total + array(content[section]).length,
      0,
    );
    const projectCount = array(content.requirements).length + array(content.risks).length;
    const preferredOptionId = text(decision.preferred_option_id, 240);
    const preferredRecorded = Boolean(
      !projectionLimited
      && preferredOptionId
      && options.some((option) => text(record(option).id, 240) === preferredOptionId),
    );
    const rawAudit = projectionLimited ? {} : record(summarizeEvidence(content));
    const audit = {
      total: count(rawAudit.total),
      unreviewed: count(rawAudit.unreviewed),
      counter: count(rawAudit.counter),
      conflict: count(rawAudit.conflict),
      gap: count(rawAudit.gap),
    };
    const version = Number.isSafeInteger(artifact.version) && artifact.version > 0
      ? artifact.version
      : null;
    const id = text(artifact.id, 240);
    const status = text(artifact.status, 40).toUpperCase() === "CONFIRMED"
      ? "confirmed"
      : "draft";
    return {
      key: [id || "artifact", version || "unknown", index].join(":"),
      artifact,
      id,
      title: displayText(artifact.title, 400) || "未命名会议产物",
      version,
      versionLabel: version ? "v" + version : "版本未知",
      status,
      statusLabel: status === "confirmed" ? "已确认" : "待确认",
      itemCount,
      projectCount,
      optionCount: options.length,
      preferredRecorded,
      projectionLimited,
      audit,
      metrics: [
        { key: "minutes", label: "纪要", value: itemCount },
        { key: "project", label: "项目条目", value: projectCount },
        { key: "candidates", label: "候选", value: options.length },
        { key: "evidence", label: "证据关系", value: projectionLimited ? "—" : audit.total },
        { key: "unreviewed", label: "未核验", value: projectionLimited ? "—" : audit.unreviewed },
      ],
    };
  });
  return {
    totalCount: artifactRows.length,
    visibleRows: rows,
    visibleCount: Math.min(artifactRows.length, safeLimit),
    hiddenCount: Math.max(0, artifactRows.length - safeLimit),
  };
}
