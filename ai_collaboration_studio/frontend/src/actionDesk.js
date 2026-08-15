export const ACTION_DESK_VERSION = "artifact_action_desk_v1";
export const ACTION_DESK_CANDIDATE_VERSION = "artifact_action_candidate_v1";
export const ACTION_DESK_ITEM_VERSION = "artifact_action_item_v1";
export const ACTION_DESK_TRANSITION_VERSION = "artifact_action_transition_v1";

export const ACTION_DESK_STATES = Object.freeze([
  "open",
  "in_progress",
  "blocked",
  "done",
  "cancelled",
]);

export const ACTION_DESK_STATE_LABELS = Object.freeze({
  open: "待处理",
  in_progress: "进行中",
  blocked: "受阻",
  done: "已完成",
  cancelled: "已取消",
});

const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const ID_PATTERN = /^[A-Za-z0-9_-]{1,120}$/;
const ROW_LIMIT = 500;
const TEXT_LIMIT = 3000;
const SHORT_TEXT_LIMIT = 500;

const ACTION_DESK_KEYS = [
  "version",
  "room_id",
  "integrity_ok",
  "candidates",
  "items",
  "counts",
  "issues",
  "execution_capability",
  "external_write",
  "can_autonomously_decide",
  "can_replace_user_decision",
  "user_final_decision_required",
];

const CANDIDATE_KEYS = [
  "version",
  "artifact_id",
  "artifact_version",
  "artifact_title",
  "action_id",
  "action_snapshot_sha256",
  "text",
  "owner",
  "due",
  "state",
  "evidence_count",
  "source_status",
];

const ITEM_KEYS = [
  ...CANDIDATE_KEYS,
  "revision",
  "note",
  "latest_event_id",
  "latest_event_sha256",
  "adopted_at",
  "updated_at",
  "source_current",
  "current_artifact_version",
  "integrity_ok",
];

const COUNT_KEYS = [
  "candidate_count",
  "item_count",
  "open_count",
  "in_progress_count",
  "blocked_count",
  "done_count",
  "cancelled_count",
];

const PATCH_KEYS = ["owner", "due", "state", "note"];
const STATE_SET = new Set(ACTION_DESK_STATES);
const SOURCE_STATE_SET = new Set(["open", "in_progress", "blocked", "done"]);

function record(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function exactKeys(value, expected) {
  const actual = Object.keys(record(value)).sort();
  const wanted = [...expected].sort();
  return actual.length === wanted.length
    && actual.every((key, index) => key === wanted[index]);
}

function cleanText(value, limit = TEXT_LIMIT) {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  return normalized.length <= limit ? normalized : null;
}

function requiredText(value, limit = TEXT_LIMIT) {
  const normalized = cleanText(value, limit);
  return normalized ? normalized : null;
}

function sha256(value) {
  const normalized = cleanText(value, 64)?.toLowerCase() || "";
  return SHA256_PATTERN.test(normalized) ? normalized : null;
}

function positiveInteger(value) {
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

function nonnegativeInteger(value) {
  return Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function epochTimestamp(value) {
  const normalized = positiveInteger(value);
  return normalized !== null && Number.isFinite(new Date(normalized).getTime()) ? normalized : null;
}

function normalizedIssueList(value, issues) {
  if (!Array.isArray(value) || value.length > ROW_LIMIT) {
    issues.push("ISSUES_INVALID");
    return [];
  }
  const seen = new Set();
  const normalized = [];
  value.forEach((entry, index) => {
    const raw = record(entry);
    const code = requiredText(raw.code, 120);
    const itemKey = requiredText(raw.item_key, SHORT_TEXT_LIMIT);
    const message = requiredText(raw.message, TEXT_LIMIT);
    const identity = `${code}|${itemKey}`;
    if (
      !exactKeys(raw, ["code", "item_key", "message"])
      || !code
      || !itemKey
      || !message
      || seen.has(identity)
    ) {
      issues.push(`ISSUE_INVALID:${index}`);
      return;
    }
    seen.add(identity);
    normalized.push({ code, itemKey, message });
  });
  return normalized;
}

function sourceIdentity(raw) {
  const artifactId = requiredText(raw.artifact_id, SHORT_TEXT_LIMIT);
  const artifactVersion = positiveInteger(raw.artifact_version);
  const actionId = requiredText(raw.action_id, SHORT_TEXT_LIMIT);
  return {
    artifactId: artifactId && ID_PATTERN.test(artifactId) ? artifactId : null,
    artifactVersion,
    actionId: actionId && ID_PATTERN.test(actionId) ? actionId : null,
    key: artifactId && ID_PATTERN.test(artifactId) && artifactVersion && actionId && ID_PATTERN.test(actionId)
      ? `${artifactId}:${artifactVersion}:${actionId}`
      : "",
  };
}

function normalizeCandidate(rawValue, index, allowedStates = SOURCE_STATE_SET) {
  const raw = record(rawValue);
  const issues = [];
  if (!exactKeys(raw, CANDIDATE_KEYS)) issues.push("SHAPE_INVALID");
  if (raw.version !== ACTION_DESK_CANDIDATE_VERSION) issues.push("VERSION_INVALID");
  const identity = sourceIdentity(raw);
  const artifactTitle = requiredText(raw.artifact_title, SHORT_TEXT_LIMIT);
  const actionSnapshotSha256 = sha256(raw.action_snapshot_sha256);
  const itemText = requiredText(raw.text, 3000);
  const owner = cleanText(raw.owner, 120);
  const due = cleanText(raw.due, 80);
  const state = allowedStates.has(raw.state) ? raw.state : null;
  const evidenceCount = nonnegativeInteger(raw.evidence_count);
  if (!identity.key) issues.push("SOURCE_IDENTITY_INVALID");
  if (!artifactTitle) issues.push("ARTIFACT_TITLE_INVALID");
  if (!actionSnapshotSha256) issues.push("ACTION_SNAPSHOT_INVALID");
  if (!itemText) issues.push("TEXT_INVALID");
  if (owner === null) issues.push("OWNER_INVALID");
  if (due === null) issues.push("DUE_INVALID");
  if (!state) issues.push("STATE_INVALID");
  if (evidenceCount === null) issues.push("EVIDENCE_COUNT_INVALID");
  if (evidenceCount !== null && evidenceCount > 20) issues.push("EVIDENCE_COUNT_LIMIT_EXCEEDED");
  if (raw.source_status !== "confirmed_exact") issues.push("SOURCE_STATUS_INVALID");
  const valid = issues.length === 0;
  return {
    kind: "candidate",
    index,
    valid,
    metricsVisible: valid,
    issues,
    sourceKey: identity.key,
    artifactId: valid ? identity.artifactId : "",
    artifactVersion: valid ? identity.artifactVersion : null,
    artifactTitle: valid ? artifactTitle : "",
    actionId: valid ? identity.actionId : "",
    actionSnapshotSha256: valid ? actionSnapshotSha256 : "",
    text: valid ? itemText : "",
    owner: valid ? owner : "",
    due: valid ? due : "",
    state: valid ? state : "",
    evidenceCount: valid ? evidenceCount : null,
    sourceStatus: valid ? "confirmed_exact" : "",
  };
}

export function normalizeActionDeskCandidate(rawValue, index = 0) {
  return normalizeCandidate(rawValue, index, SOURCE_STATE_SET);
}

export function normalizeActionDeskItem(rawValue, index) {
  const raw = record(rawValue);
  const candidateShape = {
    ...raw,
    version: ACTION_DESK_CANDIDATE_VERSION,
  };
  for (const key of ITEM_KEYS) {
    if (!CANDIDATE_KEYS.includes(key)) delete candidateShape[key];
  }
  const candidate = normalizeCandidate(candidateShape, index, STATE_SET);
  const issues = [...candidate.issues];
  if (!exactKeys(raw, ITEM_KEYS)) issues.push("SHAPE_INVALID");
  if (raw.version !== ACTION_DESK_ITEM_VERSION) issues.push("VERSION_INVALID");
  const revision = positiveInteger(raw.revision);
  const note = cleanText(raw.note, 4000);
  const latestEventId = requiredText(raw.latest_event_id, 120);
  const latestEventSha256 = sha256(raw.latest_event_sha256);
  const adoptedAt = epochTimestamp(raw.adopted_at);
  const updatedAt = epochTimestamp(raw.updated_at);
  const currentArtifactVersion = positiveInteger(raw.current_artifact_version);
  if (revision === null) issues.push("REVISION_INVALID");
  if (note === null) issues.push("NOTE_INVALID");
  if (!latestEventId || !ID_PATTERN.test(latestEventId)) issues.push("LATEST_EVENT_ID_INVALID");
  if (!latestEventSha256) issues.push("LATEST_EVENT_HASH_INVALID");
  if (!adoptedAt) issues.push("ADOPTED_AT_INVALID");
  if (!updatedAt) issues.push("UPDATED_AT_INVALID");
  if (adoptedAt && updatedAt && updatedAt < adoptedAt) issues.push("UPDATED_AT_BEFORE_ADOPTION");
  if (raw.source_current !== true && raw.source_current !== false) issues.push("SOURCE_CURRENT_INVALID");
  if (currentArtifactVersion === null) issues.push("CURRENT_ARTIFACT_VERSION_INVALID");
  if (raw.integrity_ok !== true) issues.push("ITEM_INTEGRITY_NOT_CONFIRMED");
  if (
    candidate.artifactVersion
    && currentArtifactVersion
    && ((raw.source_current === true && currentArtifactVersion !== candidate.artifactVersion)
      || (raw.source_current === false && currentArtifactVersion < candidate.artifactVersion))
  ) {
    issues.push("SOURCE_CURRENT_INCONSISTENT");
  }
  const valid = issues.length === 0;
  return {
    ...candidate,
    kind: "item",
    valid,
    metricsVisible: valid,
    issues: [...new Set(issues)],
    artifactId: valid ? candidate.artifactId : "",
    artifactVersion: valid ? candidate.artifactVersion : null,
    artifactTitle: valid ? candidate.artifactTitle : "",
    actionId: valid ? candidate.actionId : "",
    actionSnapshotSha256: valid ? candidate.actionSnapshotSha256 : "",
    text: valid ? candidate.text : "",
    owner: valid ? candidate.owner : "",
    due: valid ? candidate.due : "",
    state: valid ? candidate.state : "",
    evidenceCount: valid ? candidate.evidenceCount : null,
    sourceStatus: valid ? candidate.sourceStatus : "",
    revision: valid ? revision : null,
    note: valid ? note : "",
    latestEventId: valid ? latestEventId : "",
    latestEventSha256: valid ? latestEventSha256 : "",
    adoptedAt: valid ? adoptedAt : "",
    updatedAt: valid ? updatedAt : "",
    sourceCurrent: valid ? raw.source_current : null,
    currentArtifactVersion: valid ? currentArtifactVersion : null,
  };
}

function normalizeCounts(value, issues) {
  const raw = record(value);
  if (!exactKeys(raw, COUNT_KEYS)) issues.push("COUNTS_SHAPE_INVALID");
  const counts = {};
  for (const key of COUNT_KEYS) {
    const normalized = nonnegativeInteger(raw[key]);
    if (normalized === null) issues.push(`COUNT_INVALID:${key}`);
    counts[key] = normalized;
  }
  return {
    candidateCount: counts.candidate_count,
    itemCount: counts.item_count,
    openCount: counts.open_count,
    inProgressCount: counts.in_progress_count,
    blockedCount: counts.blocked_count,
    doneCount: counts.done_count,
    cancelledCount: counts.cancelled_count,
  };
}

function countsMatchRows(counts, candidates, items) {
  if (Object.values(counts).some((value) => value === null)) return false;
  const stateCounts = Object.fromEntries(ACTION_DESK_STATES.map((state) => [state, 0]));
  items.forEach((item) => {
    if (item.valid) stateCounts[item.state] += 1;
  });
  return counts.candidateCount === candidates.length
    && counts.itemCount === items.length
    && counts.openCount === stateCounts.open
    && counts.inProgressCount === stateCounts.in_progress
    && counts.blockedCount === stateCounts.blocked
    && counts.doneCount === stateCounts.done
    && counts.cancelledCount === stateCounts.cancelled
    && counts.itemCount === (
      counts.openCount
      + counts.inProgressCount
      + counts.blockedCount
      + counts.doneCount
      + counts.cancelledCount
    );
}

function invalidActionDesk(issues, issueDetails = []) {
  return {
    valid: false,
    integrityOk: false,
    metricsVisible: false,
    countsVisible: false,
    roomId: "",
    candidates: [],
    items: [],
    counts: null,
    issues: [...new Set(issues)],
    issueDetails,
  };
}

export function normalizeActionDeskResponse(payload, expectedRoomId) {
  const envelope = record(payload);
  const raw = record(envelope.action_desk);
  const fatalIssues = [];
  if (!exactKeys(envelope, ["ok", "action_desk"]) || envelope.ok !== true) {
    fatalIssues.push("RESPONSE_NOT_OK");
  }
  if (!exactKeys(raw, ACTION_DESK_KEYS)) fatalIssues.push("ACTION_DESK_SHAPE_INVALID");
  if (raw.version !== ACTION_DESK_VERSION) fatalIssues.push("ACTION_DESK_VERSION_INVALID");
  const roomId = requiredText(raw.room_id, SHORT_TEXT_LIMIT);
  if (!roomId || !ID_PATTERN.test(roomId) || roomId !== requiredText(expectedRoomId, SHORT_TEXT_LIMIT)) {
    fatalIssues.push("ROOM_BINDING_MISMATCH");
  }
  if (raw.integrity_ok !== true) fatalIssues.push("INTEGRITY_NOT_CONFIRMED");
  if (
    raw.execution_capability !== "none"
    || raw.external_write !== false
    || raw.can_autonomously_decide !== false
    || raw.can_replace_user_decision !== false
    || raw.user_final_decision_required !== true
  ) {
    fatalIssues.push("SAFETY_BOUNDARY_DRIFT");
  }
  const declaredIssues = normalizedIssueList(raw.issues, fatalIssues);
  if (!Array.isArray(raw.candidates) || raw.candidates.length > ROW_LIMIT) {
    fatalIssues.push("CANDIDATES_INVALID");
  }
  if (!Array.isArray(raw.items) || raw.items.length > ROW_LIMIT) {
    fatalIssues.push("ITEMS_INVALID");
  }
  if (fatalIssues.length) return invalidActionDesk(fatalIssues, declaredIssues);

  const candidates = raw.candidates.map((value, index) => normalizeCandidate(value, index));
  const items = raw.items.map(normalizeActionDeskItem);
  const rowIssues = [];
  const seenCandidateSources = new Set();
  const seenItemSources = new Set();
  candidates.forEach((candidate) => {
    if (!candidate.valid) rowIssues.push(`CANDIDATE_INVALID:${candidate.index}`);
    if (candidate.sourceKey && seenCandidateSources.has(candidate.sourceKey)) {
      candidate.valid = false;
      candidate.metricsVisible = false;
      candidate.issues = [...candidate.issues, "SOURCE_DUPLICATED"];
      rowIssues.push(`CANDIDATE_DUPLICATED:${candidate.index}`);
    }
    if (candidate.sourceKey) seenCandidateSources.add(candidate.sourceKey);
  });
  items.forEach((item) => {
    if (!item.valid) rowIssues.push(`ITEM_INVALID:${item.index}`);
    if (item.sourceKey && seenItemSources.has(item.sourceKey)) {
      item.valid = false;
      item.metricsVisible = false;
      item.issues = [...item.issues, "SOURCE_DUPLICATED"];
      rowIssues.push(`ITEM_DUPLICATED:${item.index}`);
    }
    if (item.sourceKey) seenItemSources.add(item.sourceKey);
  });

  const countIssues = [];
  const counts = normalizeCounts(raw.counts, countIssues);
  const allRowsValid = candidates.every((candidate) => candidate.valid)
    && items.every((item) => item.valid);
  const countsVisible = countIssues.length === 0
    && allRowsValid
    && countsMatchRows(counts, candidates, items);
  if (!countsVisible) countIssues.push("COUNTS_OR_ROWS_UNTRUSTED");

  return {
    valid: true,
    integrityOk: true,
    metricsVisible: countsVisible,
    countsVisible,
    roomId,
    candidates,
    items,
    counts: countsVisible ? counts : null,
    issues: [...new Set([...declaredIssues.map((issue) => issue.code), ...rowIssues, ...countIssues])],
    issueDetails: declaredIssues,
  };
}

export function newActionDeskClientRequestId() {
  const suffix = globalThis.crypto?.randomUUID?.().replaceAll("-", "")
    || `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
  return `artifact_action_transition_${suffix}`;
}

export function buildActionDeskTransitionRequest({
  source,
  transition,
  patch,
  clientRequestId,
}) {
  const row = record(source);
  const normalizedPatch = record(patch);
  if (transition !== "adopt" && transition !== "update") {
    throw new TypeError("行动台变更类型无效。");
  }
  if (!exactKeys(normalizedPatch, PATCH_KEYS)) {
    throw new TypeError("行动台变更字段不完整。");
  }
  const requestId = requiredText(clientRequestId, 120);
  const owner = cleanText(normalizedPatch.owner, 120);
  const due = cleanText(normalizedPatch.due, 80);
  const state = STATE_SET.has(normalizedPatch.state) ? normalizedPatch.state : null;
  const note = cleanText(normalizedPatch.note, 4000);
  const artifactId = requiredText(row.artifactId, SHORT_TEXT_LIMIT);
  const artifactVersion = positiveInteger(row.artifactVersion);
  const actionId = requiredText(row.actionId, SHORT_TEXT_LIMIT);
  const actionSnapshotSha256 = sha256(row.actionSnapshotSha256);
  const expectedRevision = transition === "adopt" ? 0 : positiveInteger(row.revision);
  if (
    !requestId
    || !ID_PATTERN.test(requestId)
    || owner === null
    || due === null
    || !state
    || note === null
    || !artifactId
    || !ID_PATTERN.test(artifactId)
    || !artifactVersion
    || !actionId
    || !ID_PATTERN.test(actionId)
    || !actionSnapshotSha256
    || expectedRevision === null
  ) {
    throw new TypeError("行动台变更缺少精确来源、修订号或有效字段。");
  }
  return {
    version: ACTION_DESK_TRANSITION_VERSION,
    client_request_id: requestId,
    artifact_id: artifactId,
    artifact_version: artifactVersion,
    action_id: actionId,
    expected_action_snapshot_sha256: actionSnapshotSha256,
    expected_revision: expectedRevision,
    transition,
    patch: { owner, due, state, note },
    user_confirmed: true,
  };
}

export function actionDeskComposerText(row) {
  if (!row?.valid || !row?.text || !row?.artifactTitle || !row?.artifactVersion || !row?.actionId) {
    return "";
  }
  return [
    `继续讨论待办：${row.text}`,
    `精确来源：${row.artifactTitle} · v${row.artifactVersion} · ${row.actionId}`,
  ].join("\n");
}
