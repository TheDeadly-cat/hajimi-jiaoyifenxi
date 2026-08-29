export const EXTERNAL_UNVERIFIED = "external_unverified";

export const SOURCE_INBOX_STATE_LABELS = Object.freeze({
  RECEIVED: "已接收",
  VALIDATED: "结构已校验",
  AWAITING_USER: "待人工审阅",
  ATTACHED: "已附加到房间",
  ROUND_DRAFTED: "仅有轮次草稿",
  REJECTED: "已拒绝",
  DUPLICATE: "重复导入",
  EXPIRED: "已过期",
});

export const SOURCE_INBOX_FILTERS = Object.freeze([
  { id: "", label: "全部" },
  { id: "AWAITING_USER", label: "待审阅" },
  { id: "ATTACHED", label: "已附加" },
  { id: "ROUND_DRAFTED", label: "仅有草稿" },
]);

const ACTIONABLE_STATES = new Set([
  "AWAITING_USER",
  "ATTACHED",
  "ROUND_DRAFTED",
]);
const KNOWN_STATES = new Set(Object.keys(SOURCE_INBOX_STATE_LABELS));
const SHA256_RE = /^[0-9a-f]{64}$/;

function objectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function arrayOfObjects(value) {
  return Array.isArray(value) ? value.filter((item) => (
    item && typeof item === "object" && !Array.isArray(item)
  )) : [];
}

function arrayOfStrings(value) {
  return Array.isArray(value)
    ? value.map((item) => String(item || "").trim()).filter(Boolean)
    : [];
}

function boundedNumber(value, fallback = 0) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
}

export function normalizeSourceInboxItem(value) {
  const record = objectValue(value);
  const item = objectValue(record.item);
  const rawSafety = objectValue(record.safety);
  const rawAttachments = arrayOfObjects(record.attachments);
  const rawDrafts = arrayOfObjects(record.round_drafts);
  const issues = [];
  if (record.version !== "source_inbox_item_record_v1") issues.push("record_version_invalid");
  if (record.item !== item || !Object.keys(item).length) issues.push("item_record_invalid");
  if (item.version !== "project_source_item_v1") issues.push("item_version_invalid");
  if (!String(record.id || "")) issues.push("item_id_missing");
  if (typeof record.source_channel !== "string" || !record.source_channel) {
    issues.push("source_channel_invalid");
  }
  if (!KNOWN_STATES.has(String(record.state || ""))) issues.push("state_invalid");
  if (typeof record.state_version !== "number" || !Number.isInteger(record.state_version) || record.state_version < 1) {
    issues.push("state_version_invalid");
  }
  if (typeof record.acknowledged !== "boolean") issues.push("acknowledged_flag_invalid");
  if (typeof record.received_at !== "number" || !Number.isInteger(record.received_at) || record.received_at <= 0) {
    issues.push("received_at_invalid");
  }
  if (!Array.isArray(record.attachments)) issues.push("attachments_invalid");
  if (!Array.isArray(record.round_drafts)) issues.push("round_drafts_invalid");
  if (record.safety !== rawSafety || !Object.keys(rawSafety).length) issues.push("safety_record_invalid");
  for (const field of ["facts", "sources", "impact_hypotheses", "unknowns"]) {
    if (!Array.isArray(item[field])) issues.push(`${field}_invalid`);
  }
  if (record.external_claims_verification !== EXTERNAL_UNVERIFIED) {
    issues.push("external_verification_marker_invalid");
  }
  if (!SHA256_RE.test(String(record.server_fingerprint || ""))) {
    issues.push("server_fingerprint_invalid");
  }
  if (!SHA256_RE.test(String(record.item_sha256 || ""))) issues.push("item_sha256_invalid");
  if (rawSafety.acknowledgement_is_fact_confirmation !== false) {
    issues.push("acknowledgement_boundary_invalid");
  }
  if (rawSafety.formal_round_created !== false) issues.push("formal_round_boundary_invalid");
  if (rawSafety.provider_calls_performed !== 0) issues.push("provider_boundary_invalid");
  if (rawSafety.market_calls_performed !== 0) issues.push("market_boundary_invalid");
  if (rawSafety.execution_capability !== "none") issues.push("execution_boundary_invalid");
  for (const attachment of rawAttachments) {
    if (
      attachment.version !== "source_inbox_attachment_v1"
      || !String(attachment.id || "")
      || !String(attachment.room_id || "")
      || !String(attachment.material_id || "")
      || typeof attachment.material_version !== "number"
      || !Number.isInteger(attachment.material_version)
      || attachment.material_version < 1
      || !SHA256_RE.test(String(attachment.item_sha256 || ""))
      || !SHA256_RE.test(String(attachment.attachment_sha256 || ""))
    ) {
      issues.push("attachment_record_invalid");
      break;
    }
  }
  for (const draft of rawDrafts) {
    if (
      draft.version !== "source_inbox_round_draft_v1"
      || !String(draft.id || "")
      || !String(draft.room_id || "")
      || !SHA256_RE.test(String(draft.draft_sha256 || ""))
      || draft.formal_round_created !== false
      || draft.provider_calls_performed !== 0
      || draft.market_calls_performed !== 0
      || draft.execution_capability !== "none"
      || draft.user_confirmation_required_to_launch !== true
    ) {
      issues.push("round_draft_boundary_invalid");
      break;
    }
  }
  return {
    valid: issues.length === 0,
    issues,
    id: String(record.id || ""),
    version: String(record.version || ""),
    sourceChannel: String(record.source_channel || ""),
    sourceKey: String(record.source_key || ""),
    externalRunId: String(record.external_run_id || ""),
    receivedAt: boundedNumber(record.received_at),
    serverFingerprint: String(record.server_fingerprint || ""),
    itemSha256: String(record.item_sha256 || ""),
    state: String(record.state || ""),
    stateVersion: Math.max(0, Math.trunc(boundedNumber(record.state_version))),
    acknowledged: record.acknowledged === true,
    acknowledgedBy: String(record.acknowledged_by || ""),
    acknowledgedAt: boundedNumber(record.acknowledged_at),
    expiresAt: boundedNumber(record.expires_at),
    updatedAt: boundedNumber(record.updated_at),
    externalClaimsVerification: String(record.external_claims_verification || ""),
    headline: String(item.headline || "未命名来源"),
    summary: String(item.summary || ""),
    itemType: String(item.item_type || "unspecified"),
    severity: String(item.severity || "unknown"),
    occurredAt: String(item.occurred_at || ""),
    publishedAt: String(item.published_at || ""),
    confidence: boundedNumber(item.confidence),
    recommendedRoute: String(item.recommended_route || ""),
    entities: arrayOfObjects(item.entities).map((entity) => ({
      kind: String(entity.kind || ""),
      id: String(entity.id || ""),
      label: String(entity.label || ""),
    })),
    facts: arrayOfObjects(item.facts).map((fact) => ({
      claim: String(fact.claim || ""),
      sourceIndexes: Array.isArray(fact.source_indexes) ? fact.source_indexes : [],
    })),
    sources: arrayOfObjects(item.sources).map((source) => ({
      url: String(source.url || ""),
      publisher: String(source.publisher || ""),
      sourceType: String(source.source_type || ""),
      publishedAt: String(source.published_at || ""),
      contentSha256: String(source.content_sha256 || ""),
    })),
    impactHypotheses: arrayOfObjects(item.impact_hypotheses).map((hypothesis) => ({
      statement: String(hypothesis.statement || ""),
      affectedArea: String(hypothesis.affected_area || ""),
      timeHorizon: String(hypothesis.time_horizon || ""),
      confidence: boundedNumber(hypothesis.confidence),
      sourceIndexes: Array.isArray(hypothesis.source_indexes)
        ? hypothesis.source_indexes
        : [],
    })),
    unknowns: arrayOfStrings(item.unknowns),
    attachments: rawAttachments.map((attachment) => ({
      id: String(attachment.id || ""),
      roomId: String(attachment.room_id || ""),
      materialId: String(attachment.material_id || ""),
      materialVersion: Math.max(0, Math.trunc(boundedNumber(attachment.material_version))),
      attachedAt: boundedNumber(attachment.attached_at),
      attachmentSha256: String(attachment.attachment_sha256 || ""),
    })),
    roundDrafts: rawDrafts.map((draft) => ({
      id: String(draft.id || ""),
      roomId: String(draft.room_id || ""),
      draftSha256: String(draft.draft_sha256 || ""),
      formalRoundCreated: draft.formal_round_created === true,
      providerCallsPerformed: boundedNumber(draft.provider_calls_performed),
      marketCallsPerformed: boundedNumber(draft.market_calls_performed),
    })),
    events: arrayOfObjects(record.events),
    safety: {
      acknowledgementIsFactConfirmation: rawSafety.acknowledgement_is_fact_confirmation,
      formalRoundCreated: rawSafety.formal_round_created,
      providerCallsPerformed: rawSafety.provider_calls_performed,
      marketCallsPerformed: rawSafety.market_calls_performed,
      executionCapability: String(rawSafety.execution_capability || ""),
    },
  };
}

export function normalizeSourceInboxResponse(payload) {
  const inbox = objectValue(objectValue(payload).source_inbox);
  const rawCounts = objectValue(inbox.counts);
  const counts = Object.fromEntries(Object.entries(rawCounts).map(([state, count]) => [
    String(state),
    Math.max(0, Math.trunc(boundedNumber(count))),
  ]));
  return {
    items: arrayOfObjects(inbox.items).map(normalizeSourceInboxItem),
    counts,
    query: String(inbox.query || ""),
    state: String(inbox.state || ""),
  };
}

export function sourceInboxItemPermissions(item, roomId) {
  const normalizedRoomId = String(roomId || "");
  const actionable = Boolean(item?.id) && item.valid === true && ACTIONABLE_STATES.has(item.state);
  const attachment = item?.attachments?.find((entry) => entry.roomId === normalizedRoomId) || null;
  const roundDraft = item?.roundDrafts?.find((entry) => entry.roomId === normalizedRoomId) || null;
  return {
    actionable,
    attachment,
    roundDraft,
    canAcknowledge: actionable && !item.acknowledged,
    canAttach: actionable && item.acknowledged && Boolean(normalizedRoomId) && !attachment,
    canDraft: actionable && item.acknowledged && Boolean(attachment) && !roundDraft,
  };
}

export function replaceSourceInboxItem(items, nextItem) {
  if (!nextItem?.id) return items;
  let found = false;
  const next = (Array.isArray(items) ? items : []).map((item) => {
    if (item.id !== nextItem.id) return item;
    found = true;
    return nextItem;
  });
  return found ? next : [nextItem, ...next];
}
