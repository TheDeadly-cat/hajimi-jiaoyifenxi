import { EXTERNAL_UNVERIFIED, normalizeSourceInboxItem } from "./sourceInbox.js";

export const SOURCE_IMPORT_MAX_BYTES = 256 * 1024;
export const SOURCE_IMPORT_PREVIEW_VERSION = "source_import_preview_v1";
export const SOURCE_MONITORING_PROMPT_TEMPLATE_VERSION = "source_monitoring_prompt_template_v1";

const SHA256_RE = /^[0-9a-f]{64}$/;
const SOURCE_CHANNEL_RE = /^[a-z][a-z0-9_-]{0,79}$/;
const RESERVED_SOURCE_CHANNELS = new Set([
  "official_source_monitor",
  "futu_anomaly_monitor",
]);
const PREVIEW_ROOT_KEYS = [
  "candidate",
  "external_claims_verification",
  "packet",
  "preview_sha256",
  "received_at_ms",
  "safety",
  "store_disposition",
  "valid",
  "version",
];
const CANDIDATE_KEYS = [
  "import_key_sha256",
  "import_key_version",
  "item_count",
  "item_fingerprints",
  "normalized_packet_sha256",
  "source_count",
  "source_payload_bytes",
  "source_payload_sha256",
];
const PREVIEW_SAFETY_KEYS = [
  "chatgpt_automation_performed",
  "chatgpt_page_controlled",
  "database_reads_performed",
  "database_writes_performed",
  "execution_capability",
  "external_task_created",
  "formal_rounds_created",
  "import_performed",
  "market_calls_performed",
  "network_requests_performed",
  "provider_calls_performed",
  "revalidation_required",
  "user_confirmation_required",
];
const PACKET_KEYS = [
  "checked_at",
  "cutoff_at",
  "external_claims_verification",
  "external_run_id",
  "generation",
  "items",
  "meaningful_change",
  "safety",
  "source_channel",
  "source_key",
  "version",
];
const PACKET_VERIFICATION_KEYS = [
  "checked_at",
  "cost",
  "cutoff_at",
  "item_times",
  "model",
  "recommended_routes",
  "source_times",
];
const PACKET_SAFETY_KEYS = [
  "execution_capability",
  "execution_fields_present",
  "market_calls_performed",
  "network_requests_performed",
  "provider_calls_performed",
  "user_action_required",
];
const ITEM_KEYS = [
  "confidence",
  "entities",
  "extensions",
  "external_claims_verification",
  "external_item_id",
  "facts",
  "headline",
  "impact_hypotheses",
  "item_type",
  "occurred_at",
  "published_at",
  "recommended_route",
  "server_fingerprint",
  "server_fingerprint_version",
  "severity",
  "sources",
  "summary",
  "unknowns",
  "version",
];
const PROMPT_ROOT_KEYS = [
  "constraints",
  "default_source_channel",
  "item_version",
  "packet_version",
  "prompt",
  "result_template",
  "safety",
  "template_id",
  "template_sha256",
  "version",
];
const PROMPT_CONSTRAINT_KEYS = [
  "manual_copy_paste_only",
  "markdown_fence_tolerated",
  "max_items",
  "max_payload_bytes",
  "max_sources_per_item",
  "max_total_sources",
  "one_json_object_only",
  "public_http_sources_only",
  "recommended_routes",
  "reserved_source_channels",
  "severities",
  "unmodified_template_is_importable",
];
const PROMPT_SAFETY_KEYS = [
  "chatgpt_automation_performed",
  "chatgpt_page_controlled",
  "database_reads_performed",
  "database_writes_performed",
  "execution_capability",
  "external_task_created",
  "formal_rounds_created",
  "market_calls_performed",
  "network_requests_performed",
  "provider_calls_performed",
  "user_review_required",
];
const IMPORT_RESULT_KEYS = [
  "created_item_count",
  "duplicate_item_count",
  "idempotent_replay",
  "import_id",
  "items",
  "receipt",
  "status",
  "version",
];
const IMPORT_RECEIPT_KEYS = [
  "external_claims_verification",
  "external_run_id",
  "import_key_sha256",
  "import_key_version",
  "item_count",
  "item_fingerprints",
  "normalized_packet_sha256",
  "receipt_sha256",
  "received_at_ms",
  "safety",
  "source_channel",
  "source_count",
  "source_key",
  "source_payload_bytes",
  "source_payload_sha256",
  "status",
  "version",
];
const IMPORT_RECEIPT_SAFETY_KEYS = [
  "database_writes_performed",
  "execution_capability",
  "market_calls_performed",
  "network_requests_performed",
  "provider_calls_performed",
  "user_action_required",
];

function objectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function hasExactKeys(value, expected) {
  const keys = Object.keys(objectValue(value)).sort();
  const expectedKeys = [...expected].sort();
  return keys.length === expectedKeys.length
    && keys.every((key, index) => key === expectedKeys[index]);
}

function safeCount(value) {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0
    ? value
    : -1;
}

function allZeroSafety(safety, keys) {
  return keys.every((key) => safety[key] === 0);
}

function arrayOfNativeStrings(value) {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

export function sourceImportUtf8Bytes(value) {
  return new TextEncoder().encode(String(value ?? "")).byteLength;
}

function normalizePreviewItem(rawValue, index, issues) {
  const raw = objectValue(rawValue);
  const rawSources = Array.isArray(raw.sources) ? raw.sources : [];
  const rawFacts = Array.isArray(raw.facts) ? raw.facts : [];
  const rawUnknowns = Array.isArray(raw.unknowns) ? raw.unknowns : [];
  const rawHypotheses = Array.isArray(raw.impact_hypotheses) ? raw.impact_hypotheses : [];
  const sources = rawSources.map((sourceValue) => {
    const source = objectValue(sourceValue);
    return {
      publisher: typeof source.publisher === "string" ? source.publisher : "",
      sourceType: typeof source.source_type === "string" ? source.source_type : "",
      url: typeof source.url === "string" ? source.url : "",
    };
  });
  const valid = (
    hasExactKeys(raw, ITEM_KEYS)
    && raw.version === "project_source_item_v1"
    && raw.external_claims_verification === EXTERNAL_UNVERIFIED
    && raw.server_fingerprint_version === "project_source_item_fingerprint_v1"
    && SHA256_RE.test(String(raw.server_fingerprint || ""))
    && typeof raw.headline === "string"
    && raw.headline.length > 0
    && typeof raw.summary === "string"
    && typeof raw.item_type === "string"
    && typeof raw.severity === "string"
    && typeof raw.occurred_at === "string"
    && typeof raw.published_at === "string"
    && typeof raw.recommended_route === "string"
    && Array.isArray(raw.entities)
    && Array.isArray(raw.facts)
    && Array.isArray(raw.sources)
    && Array.isArray(raw.impact_hypotheses)
    && arrayOfNativeStrings(raw.unknowns)
    && sources.length === rawSources.length
    && sources.every((source) => source.publisher && source.sourceType && source.url)
  );
  if (!valid) issues.push(`preview_item_invalid_${index}`);
  return {
    valid,
    index,
    fingerprint: String(raw.server_fingerprint || ""),
    externalItemId: String(raw.external_item_id || ""),
    itemType: String(raw.item_type || ""),
    severity: String(raw.severity || ""),
    occurredAt: String(raw.occurred_at || ""),
    publishedAt: String(raw.published_at || ""),
    headline: String(raw.headline || ""),
    summary: String(raw.summary || ""),
    recommendedRoute: String(raw.recommended_route || ""),
    factCount: rawFacts.length,
    sourceCount: rawSources.length,
    unknownCount: rawUnknowns.length,
    hypothesisCount: rawHypotheses.length,
    sources,
  };
}

export function normalizeSourceImportPreview(payload) {
  const preview = objectValue(payload?.source_import_preview ?? payload);
  const packet = objectValue(preview.packet);
  const candidate = objectValue(preview.candidate);
  const storeDisposition = objectValue(preview.store_disposition);
  const safety = objectValue(preview.safety);
  const generation = objectValue(packet.generation);
  const packetSafety = objectValue(packet.safety);
  const packetVerification = objectValue(packet.external_claims_verification);
  const rawItems = Array.isArray(packet.items) ? packet.items : [];
  const issues = [];
  const items = rawItems.map((item, index) => normalizePreviewItem(item, index, issues));
  const fingerprintList = Array.isArray(candidate.item_fingerprints)
    ? candidate.item_fingerprints
    : [];
  const sourceCount = items.reduce((total, item) => total + item.sourceCount, 0);

  if (!hasExactKeys(preview, PREVIEW_ROOT_KEYS)) issues.push("preview_fields_invalid");
  if (!hasExactKeys(candidate, CANDIDATE_KEYS)) issues.push("preview_candidate_fields_invalid");
  if (!hasExactKeys(safety, PREVIEW_SAFETY_KEYS)) issues.push("preview_safety_fields_invalid");
  if (!hasExactKeys(packet, PACKET_KEYS)) issues.push("preview_packet_fields_invalid");
  if (preview.version !== SOURCE_IMPORT_PREVIEW_VERSION || preview.valid !== true) {
    issues.push("preview_version_invalid");
  }
  if (!Number.isSafeInteger(preview.received_at_ms) || preview.received_at_ms < 0) {
    issues.push("preview_time_invalid");
  }
  if (!SHA256_RE.test(String(preview.preview_sha256 || ""))) {
    issues.push("preview_hash_invalid");
  }
  if (
    packet.version !== "source_import_packet_v1"
    || !SOURCE_CHANNEL_RE.test(String(packet.source_channel || ""))
    || RESERVED_SOURCE_CHANNELS.has(packet.source_channel)
    || typeof packet.source_key !== "string"
    || !packet.source_key
    || typeof packet.external_run_id !== "string"
    || packet.external_run_id.length < 1
    || packet.external_run_id.length > 200
    || typeof packet.checked_at !== "string"
    || typeof packet.cutoff_at !== "string"
    || typeof packet.meaningful_change !== "boolean"
    || !Array.isArray(packet.items)
    || generation.channel !== packet.source_channel
    || packet.external_claims_verification == null
  ) {
    issues.push("preview_packet_identity_invalid");
  }
  if (
    (packet.meaningful_change === true && items.length === 0)
    || (packet.meaningful_change === false && items.length !== 0)
  ) {
    issues.push("preview_meaning_invalid");
  }
  if (
    !hasExactKeys(packetVerification, PACKET_VERIFICATION_KEYS)
    || Object.values(packetVerification).some((value) => value !== EXTERNAL_UNVERIFIED)
    || !hasExactKeys(packetSafety, PACKET_SAFETY_KEYS)
    || packetSafety.execution_fields_present !== false
    || packetSafety.execution_capability !== "none"
    || packetSafety.provider_calls_performed !== 0
    || packetSafety.market_calls_performed !== 0
    || packetSafety.network_requests_performed !== 0
    || packetSafety.user_action_required !== true
  ) {
    issues.push("preview_packet_safety_invalid");
  }
  if (
    safeCount(candidate.source_payload_bytes) < 0
    || candidate.source_payload_bytes > SOURCE_IMPORT_MAX_BYTES
    || !SHA256_RE.test(String(candidate.source_payload_sha256 || ""))
    || !SHA256_RE.test(String(candidate.normalized_packet_sha256 || ""))
    || candidate.import_key_version !== "source_import_key_v1"
    || !SHA256_RE.test(String(candidate.import_key_sha256 || ""))
    || safeCount(candidate.item_count) !== items.length
    || safeCount(candidate.source_count) !== sourceCount
    || !fingerprintList.every((value) => typeof value === "string" && SHA256_RE.test(value))
    || fingerprintList.length !== items.length
    || fingerprintList.some((value, index) => value !== items[index].fingerprint)
    || new Set(fingerprintList).size !== fingerprintList.length
  ) {
    issues.push("preview_candidate_invalid");
  }
  if (
    storeDisposition.evaluated !== false
    || storeDisposition.reason !== "preview_does_not_open_database"
    || preview.external_claims_verification !== EXTERNAL_UNVERIFIED
    || !allZeroSafety(safety, [
      "database_reads_performed",
      "database_writes_performed",
      "provider_calls_performed",
      "market_calls_performed",
      "network_requests_performed",
      "formal_rounds_created",
    ])
    || safety.chatgpt_page_controlled !== false
    || safety.chatgpt_automation_performed !== false
    || safety.external_task_created !== false
    || safety.import_performed !== false
    || safety.execution_capability !== "none"
    || safety.revalidation_required !== true
    || safety.user_confirmation_required !== true
  ) {
    issues.push("preview_safety_invalid");
  }

  return {
    valid: issues.length === 0,
    issues,
    receivedAt: Number(preview.received_at_ms || 0),
    previewSha256: String(preview.preview_sha256 || ""),
    sourcePayloadSha256: String(candidate.source_payload_sha256 || ""),
    normalizedPacketSha256: String(candidate.normalized_packet_sha256 || ""),
    importKeySha256: String(candidate.import_key_sha256 || ""),
    payloadBytes: safeCount(candidate.source_payload_bytes),
    sourceChannel: String(packet.source_channel || ""),
    sourceKey: String(packet.source_key || ""),
    externalRunId: String(packet.external_run_id || ""),
    checkedAt: String(packet.checked_at || ""),
    cutoffAt: String(packet.cutoff_at || ""),
    meaningfulChange: packet.meaningful_change === true,
    itemCount: items.length,
    sourceCount,
    items,
    externalClaimsVerification: String(preview.external_claims_verification || ""),
  };
}

export function normalizeSourceMonitoringPromptTemplate(payload) {
  const template = objectValue(payload?.source_monitoring_prompt_template ?? payload);
  const constraints = objectValue(template.constraints);
  const safety = objectValue(template.safety);
  const issues = [];
  const prompt = typeof template.prompt === "string" ? template.prompt : "";

  if (!hasExactKeys(template, PROMPT_ROOT_KEYS)) issues.push("prompt_fields_invalid");
  if (!hasExactKeys(constraints, PROMPT_CONSTRAINT_KEYS)) issues.push("prompt_constraints_invalid");
  if (!hasExactKeys(safety, PROMPT_SAFETY_KEYS)) issues.push("prompt_safety_fields_invalid");
  if (
    template.version !== SOURCE_MONITORING_PROMPT_TEMPLATE_VERSION
    || template.template_id !== "manual_chatgpt_source_monitoring"
    || template.default_source_channel !== "chatgpt_manual"
    || template.packet_version !== "source_import_packet_v1"
    || template.item_version !== "project_source_item_v1"
    || !prompt
    || prompt.length > 100_000
    || !prompt.includes("source_import_packet_v1")
    || !prompt.includes("external_unverified")
    || !prompt.includes("{{monitoring_scope}}")
    || /(?:window\.open|clipboard\.readText|https?:\/\/chatgpt\.com)/i.test(prompt)
    || !SHA256_RE.test(String(template.template_sha256 || ""))
  ) {
    issues.push("prompt_identity_invalid");
  }
  if (
    constraints.one_json_object_only !== true
    || constraints.markdown_fence_tolerated !== true
    || constraints.manual_copy_paste_only !== true
    || constraints.unmodified_template_is_importable !== false
    || constraints.public_http_sources_only !== true
    || constraints.max_payload_bytes !== SOURCE_IMPORT_MAX_BYTES
    || constraints.max_items !== 50
    || constraints.max_sources_per_item !== 12
    || constraints.max_total_sources !== 200
    || !arrayOfNativeStrings(constraints.reserved_source_channels)
    || constraints.reserved_source_channels.length !== 2
    || !constraints.reserved_source_channels.every((value) => RESERVED_SOURCE_CHANNELS.has(value))
    || !arrayOfNativeStrings(constraints.severities)
    || !arrayOfNativeStrings(constraints.recommended_routes)
  ) {
    issues.push("prompt_constraints_value_invalid");
  }
  if (
    !allZeroSafety(safety, [
      "database_reads_performed",
      "database_writes_performed",
      "provider_calls_performed",
      "market_calls_performed",
      "network_requests_performed",
      "formal_rounds_created",
    ])
    || safety.chatgpt_page_controlled !== false
    || safety.chatgpt_automation_performed !== false
    || safety.external_task_created !== false
    || safety.execution_capability !== "none"
    || safety.user_review_required !== true
  ) {
    issues.push("prompt_safety_invalid");
  }

  return {
    valid: issues.length === 0,
    issues,
    prompt,
    templateSha256: String(template.template_sha256 || ""),
    packetVersion: String(template.packet_version || ""),
    defaultSourceChannel: String(template.default_source_channel || ""),
  };
}

export function normalizeSourceImportResult(payload) {
  const result = objectValue(payload?.source_import ?? payload);
  const receipt = objectValue(result.receipt);
  const receiptSafety = objectValue(receipt.safety);
  const rawItems = Array.isArray(result.items) ? result.items : [];
  const items = rawItems.map((item) => normalizeSourceInboxItem(item));
  const itemIds = items.map((item) => item.id);
  const itemFingerprints = Array.isArray(receipt.item_fingerprints)
    ? receipt.item_fingerprints
    : [];
  const createdCount = safeCount(result.created_item_count);
  const duplicateCount = safeCount(result.duplicate_item_count);
  const issues = [];

  if (!hasExactKeys(result, IMPORT_RESULT_KEYS)) issues.push("import_result_fields_invalid");
  if (!hasExactKeys(receipt, IMPORT_RECEIPT_KEYS)) issues.push("import_receipt_fields_invalid");
  if (!hasExactKeys(receiptSafety, IMPORT_RECEIPT_SAFETY_KEYS)) {
    issues.push("import_receipt_safety_fields_invalid");
  }
  if (
    result.version !== "source_inbox_import_result_v1"
    || typeof result.import_id !== "string"
    || !result.import_id
    || typeof result.status !== "string"
    || typeof result.idempotent_replay !== "boolean"
    || !Array.isArray(result.items)
    || items.length !== rawItems.length
    || items.some((item) => item.valid !== true || !item.id)
    || new Set(itemIds).size !== itemIds.length
    || createdCount < 0
    || duplicateCount < 0
    || createdCount + duplicateCount !== items.length
    || (result.idempotent_replay === true && createdCount !== 0)
  ) {
    issues.push("import_result_invalid");
  }
  if (
    receipt.version !== "source_import_receipt_v1"
    || receipt.status !== result.status
    || receipt.external_claims_verification !== EXTERNAL_UNVERIFIED
    || safeCount(receipt.item_count) !== items.length
    || safeCount(receipt.source_count) < 0
    || safeCount(receipt.source_payload_bytes) < 0
    || receipt.source_payload_bytes > SOURCE_IMPORT_MAX_BYTES
    || !SHA256_RE.test(String(receipt.source_payload_sha256 || ""))
    || !SHA256_RE.test(String(receipt.normalized_packet_sha256 || ""))
    || receipt.import_key_version !== "source_import_key_v1"
    || !SHA256_RE.test(String(receipt.import_key_sha256 || ""))
    || !SHA256_RE.test(String(receipt.receipt_sha256 || ""))
    || itemFingerprints.length !== items.length
    || !itemFingerprints.every((value, index) => (
      typeof value === "string"
      && SHA256_RE.test(value)
      && value === items[index].serverFingerprint
    ))
    || receiptSafety.database_writes_performed !== 0
    || receiptSafety.provider_calls_performed !== 0
    || receiptSafety.market_calls_performed !== 0
    || receiptSafety.network_requests_performed !== 0
    || receiptSafety.execution_capability !== "none"
    || receiptSafety.user_action_required !== true
  ) {
    issues.push("import_receipt_invalid");
  }

  return {
    valid: issues.length === 0,
    issues,
    importId: String(result.import_id || ""),
    status: String(result.status || ""),
    idempotentReplay: result.idempotent_replay === true,
    createdItemCount: createdCount,
    duplicateItemCount: duplicateCount,
    items,
  };
}
