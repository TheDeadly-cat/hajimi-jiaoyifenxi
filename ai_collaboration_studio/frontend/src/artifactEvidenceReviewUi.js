import { ARTIFACT_EVIDENCE_SOURCE_LIMIT } from "./artifactEvidenceSources.js";

export const ARTIFACT_EVIDENCE_TARGET_LIMIT = 500;
export const ARTIFACT_EVIDENCE_REVIEW_PAGE_SIZE = 80;

const SOURCE_TYPES = new Set(["material", "message", "round_market_snapshot"]);

function record(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function text(value, maxLength = 1000) {
  if (typeof value !== "string" && typeof value !== "number") return "";
  if (typeof value === "number" && !Number.isFinite(value)) return "";
  return String(value).trim().slice(0, maxLength);
}

function integer(value, fallback = 0) {
  if (typeof value !== "string" && typeof value !== "number") return fallback;
  if (typeof value === "string" && !value.trim()) return fallback;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : fallback;
}

function locator(value) {
  const raw = record(value);
  return {
    material_id: text(raw.material_id, 240),
    material_version: integer(raw.material_version),
    message_id: text(raw.message_id, 240),
    snapshot_id: text(raw.snapshot_id, 240),
  };
}

function candidateLabel(type, id, unresolved) {
  if (unresolved) return `已绑定来源不可读取 · ${type || "未知类型"}:${id || "未知标识"}`;
  if (type === "material") return `资料 · ${id}`;
  if (type === "message") return `讨论记录 · ${id}`;
  if (type === "round_market_snapshot") return `本轮冻结市场快照 · ${id}`;
  return "来源记录无效";
}

function projectCandidate(value, index) {
  const raw = record(value);
  const type = text(raw.type, 80).toLowerCase();
  const id = text(raw.id, 240);
  const identityOk = SOURCE_TYPES.has(type) && Boolean(id);
  const unresolved = raw.unresolved === true || !identityOk;
  const key = identityOk ? `${type}:${id}` : `unresolved:${index}`;
  const version = integer(raw.version);
  const latestVersion = integer(raw.latestVersion ?? raw.latest_version, version);
  const exact = raw.exact === true && raw.sourceIdentityExact === true;
  return {
    key,
    item: {
      ...raw,
      type: identityOk ? type : "",
      id,
      version,
      latestVersion,
      memberVersion: integer(raw.memberVersion ?? raw.member_version, version),
      label: text(raw.label, 400) || candidateLabel(type, id, unresolved),
      preview: text(raw.preview, 300 * 1024),
      sourceMeta: text(raw.sourceMeta ?? raw.source_meta, 1000),
      sourceUrl: text(raw.sourceUrl ?? raw.source_url, 2048),
      status: text(raw.status, 80).toLowerCase() || (unresolved ? "unresolved" : "missing"),
      versionStatus: text(raw.versionStatus ?? raw.version_status, 40).toLowerCase(),
      sourceActive: typeof raw.sourceActive === "boolean"
        ? raw.sourceActive
        : typeof raw.source_active === "boolean"
          ? raw.source_active
          : false,
      sourceIdentityExact: raw.sourceIdentityExact === true,
      exact,
      previewComplete: raw.previewComplete === true,
      previewTruncated: raw.previewTruncated === true,
      previewRedacted: raw.previewRedacted === true,
      previewBudgetExhausted: raw.previewBudgetExhausted === true,
      unresolved,
      selectable: identityOk && exact && raw.selectable !== false && !unresolved,
      locator: locator(raw.locator),
      source_snapshot_sha256: text(raw.source_snapshot_sha256, 64).toLowerCase(),
      source_revision: text(raw.source_revision, 160),
      captured_at: text(raw.captured_at, 120),
      sender_name: text(raw.sender_name, 120),
      identity: text(raw.identity, 240),
      statementScope: text(raw.statementScope ?? raw.statement_scope, 80),
      kind: text(raw.kind, 80),
      executionCapability: text(raw.executionCapability ?? raw.execution_capability, 40),
      liveTradingAllowed: raw.liveTradingAllowed === true || raw.live_trading_allowed === true,
    },
  };
}

function selectedSource(value) {
  const key = text(value, 400);
  if (!key) return null;
  const separator = key.indexOf(":");
  const type = separator > 0 ? text(key.slice(0, separator), 80).toLowerCase() : "";
  const id = separator > 0 ? text(key.slice(separator + 1), 240) : key;
  return {
    key,
    item: {
      type: SOURCE_TYPES.has(type) ? type : "",
      id,
      version: 0,
      latestVersion: 0,
      memberVersion: 0,
      label: candidateLabel(type, id, true),
      preview: "",
      sourceMeta: "保存的引用仍存在，但当前来源集合没有返回对应记录。可以解除绑定，不能据此完成核验。",
      sourceUrl: "",
      status: "missing",
      versionStatus: "unavailable",
      sourceActive: false,
      sourceIdentityExact: false,
      exact: false,
      previewComplete: false,
      previewTruncated: false,
      previewRedacted: false,
      previewBudgetExhausted: false,
      unresolved: true,
      selectable: false,
      locator: {},
    },
    synthetic: true,
  };
}

export function artifactEvidenceReviewSourceState({
  candidates = [],
  targets = [],
  selectedEvidence = [],
} = {}) {
  const issues = [];
  const rawCandidates = Array.isArray(candidates) ? candidates : [];
  const rawTargets = Array.isArray(targets) ? targets : [];
  const rawSelected = Array.isArray(selectedEvidence) ? selectedEvidence : [];
  const candidateLimitExceeded = rawCandidates.length > ARTIFACT_EVIDENCE_SOURCE_LIMIT;
  const selectionLimitExceeded = rawSelected.length > ARTIFACT_EVIDENCE_SOURCE_LIMIT;
  const targetLimitExceeded = rawTargets.length > ARTIFACT_EVIDENCE_TARGET_LIMIT;
  if (candidateLimitExceeded) issues.push(`来源集合超过 ${ARTIFACT_EVIDENCE_SOURCE_LIMIT} 条安全上限，已停止新增绑定；已绑定项仍可解除。`);
  if (selectionLimitExceeded) issues.push(`已绑定来源超过 ${ARTIFACT_EVIDENCE_SOURCE_LIMIT} 条安全上限，当前投影不完整。`);
  if (targetLimitExceeded) issues.push(`审核条目超过 ${ARTIFACT_EVIDENCE_TARGET_LIMIT} 条安全上限，已停止切换条目。`);

  const selectedKeys = [];
  const selectedSeen = new Set();
  rawSelected.slice(0, ARTIFACT_EVIDENCE_SOURCE_LIMIT).forEach((value) => {
    const key = text(value, 400);
    if (!key || selectedSeen.has(key)) return;
    selectedSeen.add(key);
    selectedKeys.push(key);
  });

  const candidateEntries = [];
  const candidateSeen = new Set();
  if (!candidateLimitExceeded) {
    rawCandidates.forEach((value, index) => {
      const entry = projectCandidate(value, index);
      if (candidateSeen.has(entry.key)) {
        issues.push(`来源键重复：${entry.key}。仅展示第一条记录。`);
        return;
      }
      candidateSeen.add(entry.key);
      candidateEntries.push(entry);
    });
  }
  selectedKeys.forEach((key) => {
    if (candidateSeen.has(key)) return;
    const entry = selectedSource(key);
    if (!entry) return;
    candidateSeen.add(key);
    candidateEntries.push(entry);
  });

  const targetRows = [];
  const targetSeen = new Set();
  if (!targetLimitExceeded) {
    rawTargets.forEach((value) => {
      const raw = record(value);
      const key = text(raw.key, 240);
      if (!key || targetSeen.has(key)) {
        if (key) issues.push(`审核条目标识重复：${key}。仅展示第一条记录。`);
        return;
      }
      targetSeen.add(key);
      targetRows.push({ key, label: text(raw.label, 400) || `未命名条目 · ${key}` });
    });
  }

  return {
    integrityOk: !candidateLimitExceeded && !selectionLimitExceeded && !targetLimitExceeded,
    blockNewBindings: candidateLimitExceeded || selectionLimitExceeded,
    candidateEntries,
    candidateRows: candidateEntries.map((entry) => entry.item),
    targetRows,
    selectedKeys,
    issues: [...new Set(issues)],
  };
}
