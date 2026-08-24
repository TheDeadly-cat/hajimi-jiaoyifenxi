import {
  normalizeActionDeskCandidate,
} from "./actionDesk.js";

export const ACTION_CONTINUATION_VERSION = "artifact_action_continuation_v1";
export const ACTION_CONTINUATION_ITEM_VERSION = "artifact_action_continuation_item_v1";
export const ACTION_CONTINUATION_RESULT_VERSION = "artifact_action_continuation_result_v1";

const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const ID_PATTERN = /^[A-Za-z0-9_-]{1,120}$/;
const TEXT_LIMIT = 4000;

const CONTINUATIONS_KEYS = [
  "version",
  "room_id",
  "integrity_ok",
  "relations",
  "counts",
  "issues",
  "execution_capability",
  "external_write",
  "can_autonomously_decide",
  "can_replace_user_decision",
  "user_final_decision_required",
];
const RELATION_KEYS = [
  "version",
  "relation_id",
  "source",
  "target",
  "source_revision",
  "created_at",
  "reason",
  "integrity_ok",
];

function record(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function exactKeys(value, expected) {
  const actual = Object.keys(record(value)).sort();
  const wanted = [...expected].sort();
  return actual.length === wanted.length && actual.every((key, index) => key === wanted[index]);
}

function cleanText(value, limit = TEXT_LIMIT) {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  return normalized.length <= limit && ![...normalized].some((character) => character.charCodeAt(0) < 32)
    ? normalized
    : null;
}

function requiredText(value, limit = 500) {
  const normalized = cleanText(value, limit);
  return normalized ? normalized : null;
}

function positiveInteger(value) {
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

function sha256(value) {
  const normalized = cleanText(value, 64)?.toLowerCase() || "";
  return SHA256_PATTERN.test(normalized) ? normalized : null;
}

function actionCandidateIdentity(value) {
  const candidate = record(value);
  const artifactId = requiredText(candidate.artifactId, 120);
  const artifactVersion = positiveInteger(candidate.artifactVersion);
  const actionId = requiredText(candidate.actionId, 120);
  const actionSnapshotSha256 = sha256(candidate.actionSnapshotSha256);
  if (
    candidate.valid !== true
    || !artifactId
    || !ID_PATTERN.test(artifactId)
    || artifactVersion === null
    || !actionId
    || !ID_PATTERN.test(actionId)
    || !actionSnapshotSha256
  ) return null;
  return {
    artifactId,
    artifactVersion,
    actionId,
    actionSnapshotSha256,
    sourceKey: `${artifactId}:${artifactVersion}:${actionId}`,
  };
}

function actionLineageIdentity(value) {
  const item = record(value);
  const artifactId = requiredText(item.artifactId, 120);
  const artifactVersion = positiveInteger(item.artifactVersion);
  return item.valid === true && artifactId && ID_PATTERN.test(artifactId) && artifactVersion !== null
    ? { artifactId, artifactVersion }
    : null;
}

function epochTimestamp(value) {
  const normalized = positiveInteger(value);
  return normalized !== null && Number.isFinite(new Date(normalized).getTime()) ? normalized : null;
}

function normalizeIssues(value, issues) {
  if (!Array.isArray(value) || value.length > 100) {
    issues.push("ISSUES_INVALID");
    return [];
  }
  const seen = new Set();
  return value.flatMap((entry, index) => {
    const raw = record(entry);
    const code = requiredText(raw.code, 120);
    const itemKey = requiredText(raw.item_key, 500);
    const message = requiredText(raw.message, 4000);
    const identity = `${code}|${itemKey}`;
    if (!exactKeys(raw, ["code", "item_key", "message"]) || !code || !itemKey || !message || seen.has(identity)) {
      issues.push(`ISSUE_INVALID:${index}`);
      return [];
    }
    seen.add(identity);
    return [{ code, itemKey, message }];
  });
}

function invalidView(issues, issueDetails = []) {
  return {
    valid: false,
    integrityOk: false,
    metricsVisible: false,
    roomId: "",
    relations: [],
    relationBySource: new Map(),
    relationCount: null,
    issues: [...new Set(issues)],
    issueDetails,
  };
}

function normalizeRelation(rawValue, index) {
  const raw = record(rawValue);
  const issues = [];
  if (!exactKeys(raw, RELATION_KEYS)) issues.push("SHAPE_INVALID");
  if (raw.version !== ACTION_CONTINUATION_ITEM_VERSION) issues.push("VERSION_INVALID");
  const relationId = requiredText(raw.relation_id, 120);
  const source = normalizeActionDeskCandidate(raw.source, index);
  const target = normalizeActionDeskCandidate(raw.target, index);
  const sourceRevision = positiveInteger(raw.source_revision);
  const createdAt = epochTimestamp(raw.created_at);
  const reason = cleanText(raw.reason, TEXT_LIMIT);
  if (!relationId || !ID_PATTERN.test(relationId)) issues.push("RELATION_ID_INVALID");
  if (!source.valid) issues.push("SOURCE_INVALID");
  if (!target.valid) issues.push("TARGET_INVALID");
  if (source.valid && target.valid && (
    source.artifactId !== target.artifactId || target.artifactVersion <= source.artifactVersion
  )) issues.push("VERSION_LINEAGE_INVALID");
  if (sourceRevision === null) issues.push("SOURCE_REVISION_INVALID");
  if (createdAt === null) issues.push("CREATED_AT_INVALID");
  if (reason === null) issues.push("REASON_INVALID");
  if (raw.integrity_ok !== true) issues.push("RELATION_INTEGRITY_NOT_CONFIRMED");
  const valid = issues.length === 0;
  const sourceKey = source.sourceKey;
  return {
    index,
    valid,
    metricsVisible: valid,
    issues,
    relationId: valid ? relationId : "",
    source: valid ? source : null,
    target: valid ? target : null,
    sourceRevision: valid ? sourceRevision : null,
    createdAt: valid ? createdAt : null,
    reason: valid ? reason : "",
    sourceKey: valid ? sourceKey : "",
  };
}

export function normalizeActionDeskContinuationsResponse(payload, expectedRoomId) {
  const envelope = record(payload);
  const raw = record(envelope.continuations);
  const fatalIssues = [];
  if (!exactKeys(envelope, ["ok", "continuations"]) || envelope.ok !== true) fatalIssues.push("RESPONSE_NOT_OK");
  if (!exactKeys(raw, CONTINUATIONS_KEYS)) fatalIssues.push("CONTINUATIONS_SHAPE_INVALID");
  if (raw.version !== ACTION_CONTINUATION_VERSION) fatalIssues.push("VERSION_INVALID");
  const roomId = requiredText(raw.room_id, 120);
  if (!roomId || !ID_PATTERN.test(roomId) || roomId !== requiredText(expectedRoomId, 120)) fatalIssues.push("ROOM_BINDING_MISMATCH");
  if (raw.execution_capability !== "none" || raw.external_write !== false || raw.can_autonomously_decide !== false || raw.can_replace_user_decision !== false || raw.user_final_decision_required !== true) fatalIssues.push("SAFETY_BOUNDARY_DRIFT");
  const issueDetails = normalizeIssues(raw.issues, fatalIssues);
  if (!Array.isArray(raw.relations) || raw.relations.length > 500) fatalIssues.push("RELATIONS_INVALID");
  const rawCounts = record(raw.counts);
  if (!exactKeys(rawCounts, ["relation_count"]) || !Number.isSafeInteger(rawCounts.relation_count) || rawCounts.relation_count < 0) fatalIssues.push("COUNTS_INVALID");
  if (raw.integrity_ok !== true) fatalIssues.push("INTEGRITY_NOT_CONFIRMED");
  if (fatalIssues.length) return invalidView(fatalIssues, issueDetails);

  const relations = raw.relations.map((value, index) => normalizeRelation(value, index));
  const rowIssues = [];
  const seenRelationIds = new Set();
  const seenSources = new Set();
  const seenTargets = new Set();
  relations.forEach((relation) => {
    if (!relation.valid) rowIssues.push(`RELATION_INVALID:${relation.index}`);
    if (relation.relationId && seenRelationIds.has(relation.relationId)) {
      relation.valid = false;
      relation.metricsVisible = false;
      relation.issues.push("RELATION_ID_DUPLICATED");
      rowIssues.push(`RELATION_ID_DUPLICATED:${relation.index}`);
    }
    if (relation.relationId) seenRelationIds.add(relation.relationId);
    if (relation.sourceKey && seenSources.has(relation.sourceKey)) {
      relation.valid = false;
      relation.metricsVisible = false;
      relation.issues.push("SOURCE_DUPLICATED");
      rowIssues.push(`SOURCE_DUPLICATED:${relation.index}`);
    }
    if (relation.sourceKey) seenSources.add(relation.sourceKey);
    const targetKey = relation.target?.sourceKey || "";
    if (targetKey && seenTargets.has(targetKey)) {
      relation.valid = false;
      relation.metricsVisible = false;
      relation.issues.push("TARGET_DUPLICATED");
      rowIssues.push(`TARGET_DUPLICATED:${relation.index}`);
    }
    if (targetKey) seenTargets.add(targetKey);
  });
  const countVisible = rowIssues.length === 0 && relations.every((relation) => relation.valid)
    && rawCounts.relation_count === relations.length;
  if (!countVisible) rowIssues.push("COUNTS_OR_ROWS_UNTRUSTED");
  const relationBySource = new Map();
  if (countVisible) relations.forEach((relation) => relationBySource.set(relation.sourceKey, relation));
  return {
    valid: true,
    integrityOk: true,
    metricsVisible: countVisible,
    roomId,
    relations: countVisible ? relations : [],
    relationBySource,
    relationCount: countVisible ? rawCounts.relation_count : null,
    issues: [...new Set([...issueDetails.map((issue) => issue.code), ...rowIssues])],
    issueDetails,
  };
}

export function newActionContinuationClientRequestId() {
  const suffix = globalThis.crypto?.randomUUID?.().replaceAll("-", "")
    || `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
  return `artifact_action_continuation_${suffix}`;
}

export function buildActionContinuationRequest({ source, target, sourceRevision, reason = "", clientRequestId }) {
  const sourceIdentity = actionCandidateIdentity(source);
  const targetIdentity = actionCandidateIdentity(target);
  if (
    !sourceIdentity
    || !targetIdentity
    || sourceIdentity.artifactId !== targetIdentity.artifactId
    || targetIdentity.artifactVersion <= sourceIdentity.artifactVersion
  ) {
    throw new TypeError("行动延续必须绑定同一产物谱系中的更新确认版本。 ");
  }
  const requestId = requiredText(clientRequestId, 120);
  const cleanReason = cleanText(reason, TEXT_LIMIT);
  if (!requestId || !ID_PATTERN.test(requestId) || positiveInteger(sourceRevision) === null || cleanReason === null) {
    throw new TypeError("行动延续缺少精确来源、修订号或有效说明。");
  }
  return {
    version: ACTION_CONTINUATION_VERSION,
    client_request_id: requestId,
    source_artifact_id: sourceIdentity.artifactId,
    source_artifact_version: sourceIdentity.artifactVersion,
    source_action_id: sourceIdentity.actionId,
    source_action_snapshot_sha256: sourceIdentity.actionSnapshotSha256,
    source_expected_revision: sourceRevision,
    target_artifact_id: targetIdentity.artifactId,
    target_artifact_version: targetIdentity.artifactVersion,
    target_action_id: targetIdentity.actionId,
    target_action_snapshot_sha256: targetIdentity.actionSnapshotSha256,
    reason: cleanReason,
    user_confirmed: true,
  };
}

export function continuationTargetCandidates(item, candidates) {
  const sourceLineage = actionLineageIdentity(item);
  if (!sourceLineage || !Array.isArray(candidates)) return [];
  const seen = new Set();
  return candidates.flatMap((candidate) => {
    const identity = actionCandidateIdentity(candidate);
    if (
      !identity
      || identity.artifactId !== sourceLineage.artifactId
      || identity.artifactVersion <= sourceLineage.artifactVersion
      || seen.has(identity.sourceKey)
    ) return [];
    seen.add(identity.sourceKey);
    return [{
      ...candidate,
      ...identity,
    }];
  }).sort((left, right) => (
    left.artifactVersion - right.artifactVersion
    || (left.sourceKey < right.sourceKey ? -1 : left.sourceKey > right.sourceKey ? 1 : 0)
  ));
}
