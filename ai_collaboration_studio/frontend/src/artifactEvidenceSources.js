const EVIDENCE_SOURCE_TYPES = new Set([
  "material",
  "message",
  "round_market_snapshot",
]);

const GAP_STATUSES = new Set([
  "error",
  "missing",
  "unavailable",
  "unresolved",
]);

const SENSITIVE_QUERY_KEY_TOKENS = new Set([
  "token", "auth", "authorization", "key", "secret", "password",
  "credential", "signature", "sig", "session", "jwt", "code",
]);

const SENSITIVE_QUERY_KEY_COMPACT = new Set([
  "apikey", "accesstoken", "refreshtoken", "authtoken",
  "xamzcredential", "xamzsignature", "xamzsecuritytoken",
  "xamzalgorithm", "xamzdate",
]);

const CAMEL_CASE_BOUNDARY = /([a-z0-9])([A-Z])/g;
const NON_ALPHANUMERIC = /[^a-z0-9]+/g;
const EVIDENCE_SOURCE_DETAIL_MAX_BYTES = 300 * 1024;

function cleanText(value, maxLength = 4000) {
  return String(value ?? "").trim().slice(0, maxLength);
}

function cleanInteger(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : fallback;
}

function jsonPreview(value) {
  if (value == null) return "";
  if (typeof value === "string") return value.trim();
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return "";
  }
}

function utf8ByteLength(value) {
  return new TextEncoder().encode(String(value ?? "")).length;
}

function cleanMetadata(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return Object.fromEntries(Object.entries(value).slice(0, 40).flatMap(([rawKey, rawValue]) => {
    const key = cleanText(rawKey, 80);
    if (!key) return [];
    if (typeof rawValue === "boolean" || typeof rawValue === "number") return [[key, rawValue]];
    if (typeof rawValue === "string") return [[key, cleanText(rawValue, 400)]];
    if (Array.isArray(rawValue)) {
      return [[key, rawValue.slice(0, 20).map((item) => cleanText(item, 120)).filter(Boolean)]];
    }
    return [];
  }));
}

function sourceType(raw = {}) {
  const type = cleanText(raw.type || raw.source_type, 80).toLowerCase();
  if (EVIDENCE_SOURCE_TYPES.has(type)) return type;
  if (raw.snapshot_id || raw.locator?.snapshot_id) return "round_market_snapshot";
  if (raw.material_id || raw.locator?.material_id) return "material";
  if (raw.message_id || raw.locator?.message_id) return "message";
  return "";
}

function sourceId(raw, type, options = {}) {
  const locator = raw?.locator && typeof raw.locator === "object" ? raw.locator : {};
  const typeId = type === "round_market_snapshot"
    ? raw.snapshot_id || locator.snapshot_id
    : type === "material"
      ? raw.material_id || locator.material_id
      : raw.message_id || locator.message_id;
  const id = cleanText(raw.id || raw.source_id || typeId, 240);
  if (id || !options.unresolved) return id;
  const code = cleanText(raw.code, 80).toLowerCase().replaceAll(/[^a-z0-9_-]/g, "_") || type;
  return `gap_${code}_${cleanInteger(options.unresolvedIndex, 0)}`;
}

function sourceVersion(raw, type) {
  if (type === "material") {
    return cleanInteger(raw.version ?? raw.material_version ?? raw.locator?.material_version, 0);
  }
  if (type === "message") {
    return cleanInteger(raw.version ?? raw.member_version, 0);
  }
  return cleanInteger(raw.version, 0);
}

function sourceLocator(raw, type, id, version) {
  const rawLocator = raw?.locator && typeof raw.locator === "object" ? raw.locator : {};
  if (type === "material") {
    return {
      material_id: cleanText(rawLocator.material_id || raw.material_id || id, 240),
      material_version: cleanInteger(
        rawLocator.material_version ?? raw.material_version ?? version,
        version,
      ),
    };
  }
  if (type === "message") {
    return { message_id: cleanText(rawLocator.message_id || raw.message_id || id, 240) };
  }
  return { snapshot_id: cleanText(rawLocator.snapshot_id || raw.snapshot_id || id, 240) };
}

function sourcePreview(raw, type) {
  if (Object.hasOwn(raw, "preview")) return jsonPreview(raw.preview);
  if (Object.hasOwn(raw, "content")) return jsonPreview(raw.content);
  if (type === "round_market_snapshot" && Object.hasOwn(raw, "payload")) {
    return jsonPreview(raw.payload);
  }
  return "";
}

function sourceIdentityIsExact(raw, unresolved) {
  if (unresolved) return false;
  if (typeof raw.source_identity_exact === "boolean") return raw.source_identity_exact;
  if (typeof raw.sourceIdentityExact === "boolean") return raw.sourceIdentityExact;
  if (typeof raw.exact === "boolean") return raw.exact;
  return false;
}

function sourcePreviewIsComplete(raw, preview) {
  const declaredComplete = typeof raw.preview_complete === "boolean"
    ? raw.preview_complete
    : typeof raw.previewComplete === "boolean"
      ? raw.previewComplete
      : typeof raw.preview_exact === "boolean"
        ? raw.preview_exact
        : typeof raw.previewExact === "boolean"
          ? raw.previewExact
          : false;
  const truncated = raw.preview_truncated === true || raw.previewTruncated === true;
  const redacted = raw.preview_redacted === true || raw.previewRedacted === true;
  const budgetExhausted = raw.preview_budget_exhausted === true
    || raw.previewBudgetExhausted === true;
  return declaredComplete === true
    && Boolean(preview)
    && !truncated
    && !redacted
    && !budgetExhausted;
}

function sourceStatus(raw, { unresolved, exact, type }) {
  const explicit = cleanText(raw.status || raw.source_status, 80).toLowerCase();
  if (explicit) return explicit;
  if (unresolved) return "unresolved";
  if (type === "material" && (raw.source_active === false || raw.active === false)) return "inactive";
  return exact ? "available" : "missing";
}

function sourceLabel(raw, type, id, version) {
  if (cleanText(raw.label, 400)) return cleanText(raw.label, 400);
  if (type === "material") {
    return `资料 · ${cleanText(raw.title, 240) || id} · v${version || 1}`;
  }
  if (type === "message") {
    const sender = cleanText(raw.sender_name, 120) || "未知成员";
    const excerpt = cleanText(raw.preview ?? raw.content, 72);
    return `讨论记录 · ${sender}${excerpt ? ` · ${excerpt}` : ` · ${id}`}`;
  }
  return `本轮冻结市场快照 · ${cleanText(raw.snapshot_id, 240) || id}`;
}

function sourceMeta(raw, type, version) {
  const rawMeta = raw.source_meta ?? raw.sourceMeta;
  const explicit = typeof rawMeta === "string" ? cleanText(rawMeta, 1000) : "";
  if (explicit) return explicit;
  const meta = rawMeta && typeof rawMeta === "object" ? rawMeta : {};
  const gapMessage = cleanText(raw.message, 240);
  if (gapMessage) return gapMessage;
  if (type === "material") {
    const currentVersion = cleanInteger(raw.latest_version ?? meta.current_version, version);
    const active = raw.source_active === false || raw.active === false || meta.current_active === false
      ? "当前已停用"
      : currentVersion > version
        ? `最新版 v${currentVersion}`
        : "版本快照可读取";
    const contentSha = cleanText(meta.content_sha256, 64);
    return `${cleanText(raw.kind || meta.kind, 80) || "note"} · 精确版本 v${version || 1} · ${active}${contentSha ? ` · 内容 SHA ${contentSha.slice(0, 12)}…` : ""}`;
  }
  if (type === "message") {
    const sender = cleanText(raw.sender_name, 120) || "未知成员";
    const identity = cleanText(raw.identity || raw.sender_type || meta.identity || meta.sender_type, 120) || "讨论记录";
    return `${sender} · ${identity} · 成员版本 v${version} · 仅证明该成员曾作此陈述`;
  }
  const revision = cleanText(raw.source_revision || meta.source_revision, 160) || "证据版本未知";
  const sha = cleanText(raw.source_snapshot_sha256 || meta.source_snapshot_sha256, 64);
  return `${cleanText(raw.source || meta.source, 120) || "只读市场数据"} · ${cleanText(raw.state || meta.state, 80) || "状态未知"} · ${cleanText(raw.captured_at || meta.captured_at, 120) || "截面时间未知"} · ${revision}${sha ? ` · SHA ${sha.slice(0, 12)}…` : ""} · 无执行能力`;
}

function normalizedVersionStatus(raw, status) {
  const explicit = cleanText(raw.version_status, 40).toLowerCase();
  if (["current", "superseded", "inactive", "unavailable"].includes(explicit)) return explicit;
  if (["current", "superseded", "inactive", "unavailable"].includes(status)) return status;
  if (["missing", "unavailable", "unresolved", "error"].includes(status)) return "unavailable";
  if (status === "inactive") return "inactive";
  return "current";
}

export function safeExternalUrl(value) {
  const candidate = cleanText(value, 2048);
  if (!candidate) return "";
  try {
    const url = new URL(candidate);
    if (url.protocol !== "http:" && url.protocol !== "https:") return "";
    url.username = "";
    url.password = "";
    url.hash = "";
    for (const key of [...url.searchParams.keys()]) {
      const tokenized = key.replaceAll(CAMEL_CASE_BOUNDARY, "$1_$2").toLowerCase();
      const tokens = tokenized.split(NON_ALPHANUMERIC).filter(Boolean);
      const compact = key.toLowerCase().replaceAll(NON_ALPHANUMERIC, "");
      if (
        tokens.some((token) => SENSITIVE_QUERY_KEY_TOKENS.has(token))
        || SENSITIVE_QUERY_KEY_COMPACT.has(compact)
      ) {
        url.searchParams.set(key, "[REDACTED]");
      }
    }
    return url.href;
  } catch {
    return "";
  }
}

export function messageDomId(messageId) {
  return `message-${encodeURIComponent(cleanText(messageId, 240))}`;
}

export function evidenceMessageId(value = {}) {
  const type = cleanText(value.type || value.source_type, 80).toLowerCase();
  const explicit = value.message_id || value.locator?.message_id;
  if (explicit) return cleanText(explicit, 240);
  return type === "message" ? cleanText(value.id || value.source_id, 240) : "";
}

export function replyTargetMessageId(value = {}) {
  return cleanText(value.reply_to_message_id, 240);
}

export function evidenceSourceIdentityMatches(source = {}, audit = {}) {
  if (source.sourceIdentityExact !== true && source.exact !== true) return false;
  const type = cleanText(source.type, 80).toLowerCase();
  const sourceVersionValue = type === "message"
    ? source.memberVersion ?? source.version
    : source.version;
  const sourceVersion = cleanInteger(sourceVersionValue, 0);
  const citedVersion = cleanInteger(audit.version, -1);
  const sourceSnapshotSha256 = cleanText(source.source_snapshot_sha256, 64).toLowerCase();
  const citedSnapshotSha256 = cleanText(audit.source_snapshot_sha256, 64).toLowerCase();
  if (
    citedSnapshotSha256
    && (!sourceSnapshotSha256 || citedSnapshotSha256 !== sourceSnapshotSha256)
  ) return false;
  if (type === "material") {
    return sourceVersion > 0 && citedVersion > 0 && sourceVersion === citedVersion;
  }
  if (type === "message") {
    return citedVersion === sourceVersion;
  }
  return type === "round_market_snapshot";
}

export function exactMaterialPreviewRequired(source = {}, audit = {}) {
  if (cleanText(source.type, 80).toLowerCase() !== "material") return false;
  const citedVersion = cleanInteger(audit.version, -1);
  return source.previewComplete !== true
    || citedVersion !== cleanInteger(source.version, 0);
}

export function evidenceSourceDetailRequired(source = {}, audit = {}) {
  return source.previewComplete !== true
    || exactMaterialPreviewRequired(source, audit);
}

export function evidenceSourceDetailKey(source = {}, audit = {}) {
  return `${cleanText(source.type, 80)}:${cleanText(source.id, 240)}:v${cleanInteger(audit.version, 0)}`;
}

export function evidencePreviewAllowsVerification(source = {}, audit = {}, exactPreview = null) {
  if (!evidenceSourceIdentityMatches(source, audit)) return false;
  if (!evidenceSourceDetailRequired(source, audit)) return true;
  return exactPreview?.status === "ready"
    && evidenceSourceIdentityMatches(exactPreview, audit)
    && exactPreview.previewComplete === true
    && Boolean(cleanText(exactPreview.preview, 1))
    && exactPreview.previewTruncated !== true
    && exactPreview.previewRedacted !== true
    && exactPreview.previewBudgetExhausted !== true;
}

export function evidenceLocatorLabel(locator = {}) {
  if (locator.material_id) return `资料 ${locator.material_id} · v${cleanInteger(locator.material_version, 0)}`;
  if (locator.message_id) return `消息 ${locator.message_id}`;
  if (locator.snapshot_id) return `快照 ${locator.snapshot_id}`;
  return "";
}

export function normalizeArtifactEvidenceSource(rawSource, options = {}) {
  const raw = rawSource && typeof rawSource === "object" ? rawSource : {};
  const unresolved = options.unresolved === true || raw.unresolved === true;
  const type = sourceType(raw);
  if (!type) return null;
  const id = sourceId(raw, type, options);
  if (!id) return null;
  const version = sourceVersion(raw, type);
  const preview = sourcePreview(raw, type);
  const exact = sourceIdentityIsExact(raw, unresolved);
  const previewComplete = sourcePreviewIsComplete(raw, preview);
  const status = sourceStatus(raw, { unresolved, exact, type });
  const rawMeta = raw.source_meta && typeof raw.source_meta === "object" ? raw.source_meta : {};
  const sourceActive = typeof raw.source_active === "boolean"
    ? raw.source_active
    : typeof raw.active === "boolean"
      ? raw.active
      : typeof rawMeta.current_active === "boolean"
        ? rawMeta.current_active
      : !["inactive", "unavailable", "missing", "unresolved"].includes(status);
  const versionStatus = normalizedVersionStatus(raw, status);
  const latestVersion = cleanInteger(raw.latest_version ?? rawMeta.current_version, version);
  const normalized = {
    type,
    id,
    version,
    round_id: cleanText(raw.round_id || options.roundId, 240),
    label: sourceLabel(raw, type, id, version),
    preview,
    previewExact: previewComplete,
    previewComplete,
    previewTruncated: raw.preview_truncated === true || raw.previewTruncated === true,
    previewRedacted: raw.preview_redacted === true || raw.previewRedacted === true,
    previewBudgetExhausted: raw.preview_budget_exhausted === true
      || raw.previewBudgetExhausted === true,
    sourceIdentityExact: exact,
    exact,
    status,
    locator: sourceLocator(raw, type, id, version),
    sourceMeta: sourceMeta(raw, type, version),
    sourceUrl: safeExternalUrl(raw.source_url || raw.sourceUrl),
    sourceActive,
    latestVersion,
    versionStatus,
    unresolved,
    selectable: !unresolved && exact && !GAP_STATUSES.has(status),
    evidenceRole: cleanText(raw.evidence_role, 40),
    verificationStatus: cleanText(raw.verification_status, 40),
    detailBytes: cleanInteger(raw.detail_bytes, 0),
    detailMaxBytes: cleanInteger(raw.detail_max_bytes, 0),
  };
  if (type === "round_market_snapshot") {
    normalized.snapshot_id = cleanText(raw.snapshot_id || id, 240);
    normalized.source_revision = cleanText(raw.source_revision, 160);
    normalized.source_snapshot_sha256 = cleanText(raw.source_snapshot_sha256, 64).toLowerCase();
    normalized.captured_at = cleanText(raw.captured_at, 120);
    normalized.executionCapability = cleanText(
      raw.execution_capability || rawMeta.execution_capability,
      40,
    );
    normalized.liveTradingAllowed = raw.live_trading_allowed === true
      || rawMeta.live_trading_allowed === true;
  }
  if (type === "message") {
    normalized.sender_name = cleanText(raw.sender_name, 120);
    normalized.created_at = cleanText(raw.created_at, 120);
    normalized.senderType = cleanText(raw.sender_type || rawMeta.sender_type, 40);
    normalized.identity = cleanText(raw.identity || rawMeta.identity, 240);
    normalized.memberVersion = cleanInteger(raw.member_version ?? rawMeta.member_version, version);
    normalized.statementScope = cleanText(raw.statement_scope || rawMeta.statement_scope, 80);
    normalized.source_snapshot_sha256 = cleanText(
      raw.source_snapshot_sha256 || rawMeta.source_snapshot_sha256,
      64,
    ).toLowerCase();
  }
  if (type === "material") {
    normalized.kind = cleanText(raw.kind || rawMeta.kind, 80) || "note";
    normalized.changedAt = cleanInteger(raw.changed_at ?? rawMeta.changed_at, 0);
    normalized.metadata = cleanMetadata(raw.metadata || rawMeta.metadata);
    normalized.source_snapshot_sha256 = cleanText(
      raw.source_snapshot_sha256 || rawMeta.snapshot_sha256,
      64,
    ).toLowerCase();
  }
  return normalized;
}

function uniqueSources(rawSources, options) {
  const seen = new Set();
  return (Array.isArray(rawSources) ? rawSources : []).flatMap((raw, index) => {
    const source = normalizeArtifactEvidenceSource(raw, { ...options, unresolvedIndex: index });
    if (!source) return [];
    const key = `${source.type}:${source.id}`;
    if (seen.has(key)) return [];
    seen.add(key);
    return [source];
  });
}

export function normalizeArtifactEvidenceResponse(payload, { roundId = "" } = {}) {
  const legacyArray = Array.isArray(payload);
  const envelope = legacyArray
    ? { sources: payload }
    : payload && typeof payload === "object"
      ? payload
      : {};
  const normalizedRoundId = cleanText(envelope.round_id || roundId, 240);
  const sources = uniqueSources(envelope.sources, { roundId: normalizedRoundId });
  const sourceKeys = new Set(sources.map((source) => `${source.type}:${source.id}`));
  const unresolved = uniqueSources(envelope.unresolved, {
    roundId: normalizedRoundId,
    unresolved: true,
  }).filter((source) => !sourceKeys.has(`${source.type}:${source.id}`));
  return {
    version: cleanText(envelope.version, 120) || (legacyArray ? "artifact_evidence_sources_v1" : ""),
    artifactId: cleanText(envelope.artifact_id, 240),
    roundId: normalizedRoundId,
    authoritative: typeof envelope.authoritative === "boolean"
      ? envelope.authoritative
      : Boolean(normalizedRoundId),
    sources,
    unresolved,
  };
}

export function normalizeArtifactEvidenceDetailResponse(payload, expected = {}) {
  const envelope = payload && typeof payload === "object" ? payload : {};
  if (cleanText(envelope.version, 120) !== "artifact_evidence_source_detail_v1") return null;
  if (envelope.authoritative !== true) return null;
  const artifactId = cleanText(envelope.artifact_id, 240);
  const roundId = cleanText(envelope.round_id, 240);
  if (cleanText(expected.artifactId, 240) && artifactId !== cleanText(expected.artifactId, 240)) {
    return null;
  }
  if (cleanText(expected.roundId, 240) && roundId !== cleanText(expected.roundId, 240)) {
    return null;
  }
  const source = normalizeArtifactEvidenceSource(envelope.source, { roundId });
  if (!source || source.sourceIdentityExact !== true || source.round_id !== roundId) return null;
  if (
    source.detailMaxBytes !== EVIDENCE_SOURCE_DETAIL_MAX_BYTES
    || source.detailBytes < 1
    || source.detailBytes > source.detailMaxBytes
    || source.detailBytes !== utf8ByteLength(source.preview)
  ) return null;
  if (cleanText(expected.type, 80) && source.type !== cleanText(expected.type, 80).toLowerCase()) {
    return null;
  }
  if (cleanText(expected.id, 240) && source.id !== cleanText(expected.id, 240)) return null;
  if (!["round_market_snapshot", "message"].includes(source.type)) return null;
  return {
    version: "artifact_evidence_source_detail_v1",
    artifactId,
    roundId,
    authoritative: true,
    source,
  };
}

export function buildArtifactEvidenceCandidates({
  roundId = "",
  apiEvidence,
  materials = [],
  messages = [],
  referencedEvidence = [],
}) {
  const cleanRoundId = cleanText(roundId, 240);
  if (cleanRoundId) {
    if (!apiEvidence?.authoritative) return [];
    return [...(apiEvidence.sources || []), ...(apiEvidence.unresolved || [])];
  }

  const referencedKeys = new Set((referencedEvidence || []).map((ref) => (
    `${cleanText(ref?.type, 80)}:${cleanText(ref?.id, 240)}`
  )));
  const recentStart = Math.max(0, messages.length - 40);
  const materialSources = uniqueSources(materials.map((material) => ({
    ...material,
    type: "material",
    exact: true,
    source_identity_exact: true,
    preview_complete: Boolean(cleanText(material.content, 1)),
    status: material.active === false ? "inactive" : "available",
  })), {});
  const messageSources = uniqueSources(messages
    .filter((message, index) => index >= recentStart || referencedKeys.has(`message:${message.id}`))
    .toReversed()
    .map((message) => ({
      ...message,
      type: "message",
      exact: true,
      source_identity_exact: true,
      preview_complete: Boolean(cleanText(message.content, 1)),
      status: "available",
    })), {});
  const candidates = [...materialSources, ...messageSources];
  const knownKeys = new Set(candidates.map((source) => `${source.type}:${source.id}`));
  const historical = (referencedEvidence || []).flatMap((ref) => {
    const key = `${cleanText(ref?.type, 80)}:${cleanText(ref?.id, 240)}`;
    if (knownKeys.has(key)) return [];
    const source = normalizeArtifactEvidenceSource({
      ...ref,
      label: `历史${ref?.type === "material" ? "资料" : "讨论记录"} · ${ref?.id || "未知"} · v${cleanInteger(ref?.version, 0)}`,
      preview: "该历史来源当前不在草稿的可用来源列表中。请读取精确版本后再完成核验。",
      preview_exact: false,
      status: "missing",
    });
    if (!source) return [];
    knownKeys.add(key);
    return [source];
  });
  return [...candidates, ...historical];
}

export function evidenceRelationFlags(source = {}, audit = {}, sourceDetail = null) {
  const evidenceRole = cleanText(audit.evidence_role || source.evidenceRole, 40);
  const verificationStatus = cleanText(
    audit.verification_status || source.verificationStatus,
    40,
  );
  const status = cleanText(source.status, 40).toLowerCase();
  return {
    support: evidenceRole === "support",
    counter: evidenceRole === "counter",
    conflict: verificationStatus === "disputed" || status === "disputed" || status === "conflict",
    gap: source.exact !== true
      || !evidencePreviewAllowsVerification(source, audit, sourceDetail)
      || source.unresolved === true
      || GAP_STATUSES.has(status),
  };
}

export function summarizeEvidenceRelations(
  candidates = [],
  selectedKeys = [],
  reviewByKey = {},
  sourceDetailByKey = {},
) {
  const byKey = new Map(candidates.map((source) => [`${source.type}:${source.id}`, source]));
  return selectedKeys.reduce((summary, key) => {
    const source = byKey.get(key) || { exact: false, status: "missing", unresolved: true };
    const audit = reviewByKey[key] || {};
    const detail = sourceDetailByKey[evidenceSourceDetailKey(source, audit)];
    const flags = evidenceRelationFlags(source, audit, detail);
    summary.selected += 1;
    for (const name of ["support", "counter", "conflict", "gap"]) {
      if (flags[name]) summary[name] += 1;
    }
    return summary;
  }, { selected: 0, support: 0, counter: 0, conflict: 0, gap: 0 });
}
