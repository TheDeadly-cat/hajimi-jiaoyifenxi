export const EXTERNAL_UNVERIFIED = "external_unverified";

export const SOURCE_INBOX_TIER_META = Object.freeze({
  official_source: Object.freeze({
    id: "official_source",
    code: "L1",
    label: "官方发布通道",
  }),
  readonly_market: Object.freeze({
    id: "readonly_market",
    code: "L2",
    label: "本地只读市场信号",
  }),
  external_manual: Object.freeze({
    id: "external_manual",
    code: "EXT",
    label: "外部人工导入",
  }),
});

export const SOURCE_MONITORING_HEALTH_LABELS = Object.freeze({
  disabled: "已停用",
  idle: "等待首次检查",
  running: "本次检查进行中",
  healthy: "最近检查成功",
  degraded: "最近检查有异常",
  backing_off: "退避等待中",
  failed: "检查失败",
});

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
const KNOWN_SOURCE_TIERS = new Set(Object.keys(SOURCE_INBOX_TIER_META));
const KNOWN_HEALTH_STATES = new Set(Object.keys(SOURCE_MONITORING_HEALTH_LABELS));
const SOURCE_MONITORING_RUNTIME_LABELS = Object.freeze({
  disabled: "已停用",
  stopped: "已停止",
  starting: "启动中",
  running: "运行中",
  degraded: "运行中（有异常）",
  stalled: "心跳停滞",
  failed: "运行失败",
  stopping: "停止中",
});
const KNOWN_RUNTIME_STATUSES = new Set(Object.keys(SOURCE_MONITORING_RUNTIME_LABELS));
const KNOWN_HEALTH_CONFIG_STATES = new Set([
  "absent",
  "unregistered",
  "current",
  "migration_required",
]);
const SHA256_RE = /^[0-9a-f]{64}$/;
const SOURCE_INBOX_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/;
const SOURCE_INBOX_CURSOR_RE = /^[A-Za-z0-9_-]{1,512}$/;
const SOURCE_MONITORING_RUNTIME_ID_RE = /^source_monitor_runtime_[0-9a-f]{32}$/;
const SOURCE_MONITORING_RUNTIME_ERROR_RE = /^[A-Z][A-Z0-9_]{0,99}$/;
const SOURCE_MONITORING_ADAPTER_KEY_RE = /^[a-z][a-z0-9_]{0,63}$/;
const SOURCE_MONITORING_FROM_TIME_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/;
const SOURCE_MONITORING_SETTINGS_FIELDS = Object.freeze([
  "enabled",
  "auto_start",
  "official_only",
  "allow_readonly_market",
  "trading_impact_rules_enabled",
  "dry_run",
  "max_items_per_run",
  "initial_mode",
  "catch_up_max_items",
  "initial_preview_sha256",
  "from_time",
  "continuous_event_cutoff",
]);
const SOURCE_MONITORING_RUNTIME_FIELDS = Object.freeze([
  "version",
  "status",
  "runtime_id",
  "started_at",
  "heartbeat_at",
  "last_loop_at",
  "active_adapter",
  "next_due_at",
  "thread_alive",
  "last_fatal_error_code",
  "heartbeat_age_ms",
  "stall_after_ms",
  "liveness_verified",
  "enabled",
  "auto_start",
  "dry_run",
  "execution_capability",
  "live_trading_allowed",
]);

const IMPACT_ZERO_FIELDS = Object.freeze([
  "model_calls_performed",
  "provider_calls_performed",
  "network_requests_performed",
  "market_calls_performed",
  "database_writes_performed",
]);

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

function hasExactFields(value, fields) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const keys = Object.keys(value);
  return keys.length === fields.length && fields.every((field) => Object.hasOwn(value, field));
}

function validCanonicalFromTime(value) {
  if (typeof value !== "string" || !SOURCE_MONITORING_FROM_TIME_RE.test(value)) return false;
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp) || timestamp < 0) return false;
  const iso = new Date(timestamp).toISOString();
  const canonical = iso.endsWith(".000Z") ? iso.replace(".000Z", "Z") : iso;
  return canonical === value;
}

function boundedCount(value, fallback = 0) {
  return typeof value === "number"
    && Number.isSafeInteger(value)
    && value >= 0
    ? value
    : fallback;
}

function expectedSourceTier(sourceChannel) {
  if (sourceChannel === "official_source_monitor") return "official_source";
  if (sourceChannel === "futu_anomaly_monitor") return "readonly_market";
  return "external_manual";
}

export function sourceInboxTierMeta(sourceChannel, suppliedTier = "") {
  const expected = expectedSourceTier(String(sourceChannel || ""));
  const tier = typeof suppliedTier === "string" ? suppliedTier : "";
  const valid = KNOWN_SOURCE_TIERS.has(tier) && tier === expected;
  return {
    ...(valid ? SOURCE_INBOX_TIER_META[tier] : SOURCE_INBOX_TIER_META.external_manual),
    valid,
  };
}

function normalizeImpactProjection(recordValue, parent) {
  const record = objectValue(recordValue);
  const projection = objectValue(record.projection);
  const sourceBinding = objectValue(projection.source_binding);
  const itemBinding = objectValue(projection.source_item_binding);
  const interpretation = objectValue(projection.interpretation_boundary);
  const accounting = objectValue(projection.accounting);
  const safety = objectValue(record.safety);
  const hypothesisField = projection.hypotheses;
  const rawHypotheses = Array.isArray(hypothesisField)
    ? arrayOfObjects(hypothesisField)
    : [];
  const matchedRuleField = projection.matched_rule_ids;
  const matchedRuleIds = Array.isArray(matchedRuleField)
    ? matchedRuleField.filter((ruleId) => (
      typeof ruleId === "string" && SOURCE_INBOX_ID_RE.test(ruleId)
    ))
    : [];
  const issues = [];
  if (record.version !== "source_inbox_trading_impact_projection_record_v1") {
    issues.push("impact_record_version_invalid");
  }
  if (projection.version !== "trading_impact_projection_v1") {
    issues.push("impact_projection_version_invalid");
  }
  if (
    !Array.isArray(hypothesisField)
    || rawHypotheses.length !== hypothesisField.length
  ) {
    issues.push("impact_hypotheses_invalid");
  }
  if (
    !Array.isArray(matchedRuleField)
    || matchedRuleIds.length !== matchedRuleField.length
    || new Set(matchedRuleIds).size !== matchedRuleIds.length
  ) {
    issues.push("impact_matched_rules_invalid");
  }
  if (
    !SHA256_RE.test(String(record.projection_sha256 || ""))
    || String(record.projection_sha256 || "") !== String(projection.projection_sha256 || "")
  ) {
    issues.push("impact_projection_hash_invalid");
  }
  if (
    String(record.source_item_sha256 || "") !== parent.itemSha256
    || String(record.server_fingerprint || "") !== parent.serverFingerprint
    || String(itemBinding.item_sha256 || "") !== parent.itemSha256
    || String(itemBinding.server_fingerprint || "") !== parent.serverFingerprint
  ) {
    issues.push("impact_parent_binding_invalid");
  }
  if (String(sourceBinding.source_channel || "") !== parent.sourceChannel) {
    issues.push("impact_source_binding_invalid");
  }
  if (projection.verification_state !== EXTERNAL_UNVERIFIED) {
    issues.push("impact_verification_boundary_invalid");
  }
  if (
    interpretation.directional_forecast !== false
    || interpretation.causal_attribution !== "none"
    || interpretation.profitability_claim !== false
    || interpretation.execution_authority !== "none"
    || interpretation.user_review_required !== true
  ) {
    issues.push("impact_interpretation_boundary_invalid");
  }
  if (
    IMPACT_ZERO_FIELDS.some((field) => accounting[field] !== 0)
    || IMPACT_ZERO_FIELDS.some((field) => safety[field] !== 0)
    || safety.formal_rounds_created !== 0
    || safety.live_trading_allowed !== false
    || safety.execution_capability !== "none"
  ) {
    issues.push("impact_zero_capability_invalid");
  }
  const evaluation = String(projection.evaluation || "");
  if (!new Set(["matched", "no_match"]).has(evaluation)) {
    issues.push("impact_evaluation_invalid");
  }
  if (evaluation === "no_match" && rawHypotheses.length) {
    issues.push("impact_no_match_hypotheses_invalid");
  }
  if (evaluation === "matched" && rawHypotheses.length === 0) {
    issues.push("impact_matched_hypotheses_invalid");
  }
  if (
    (evaluation === "no_match" && matchedRuleIds.length !== 0)
    || (evaluation === "matched" && matchedRuleIds.length !== 1)
  ) {
    issues.push("impact_evaluation_rules_invalid");
  }
  if (
    String(record.status || "") !== (evaluation === "matched" ? "MATCHED" : "NO_MATCH")
    || record.hypothesis_count !== rawHypotheses.length
  ) {
    issues.push("impact_record_accounting_invalid");
  }

  const hypotheses = rawHypotheses.map((hypothesis, index) => {
    const impact = objectValue(hypothesis.impact_hypothesis);
    const area = objectValue(hypothesis.affected_area_binding);
    const time = objectValue(hypothesis.time_dimension);
    const confidence = objectValue(hypothesis.confidence_basis);
    const counterevidence = objectValue(hypothesis.counterevidence);
    const securityIds = arrayOfStrings(area.security_ids);
    if (
      hypothesis.version !== "trading_impact_hypothesis_v1"
      || !SOURCE_INBOX_ID_RE.test(String(hypothesis.rule_id || ""))
      || !new Set(["sector", "security"]).has(String(area.kind || ""))
      || !String(area.id || "")
      || !securityIds.length
      || impact.confidence !== 0.5
      || confidence.outcome_probability !== false
      || counterevidence.status !== "unknown"
    ) {
      issues.push(`impact_hypothesis_invalid_${index}`);
    }
    return {
      id: String(hypothesis.hypothesis_sha256 || `${record.id || "impact"}-${index}`),
      ruleId: String(hypothesis.rule_id || ""),
      statement: String(impact.statement || ""),
      affectedArea: String(impact.affected_area || ""),
      timeHorizon: String(impact.time_horizon || time.horizon_id || ""),
      confidence: boundedNumber(impact.confidence),
      areaKind: String(area.kind || ""),
      areaId: String(area.id || ""),
      securityIds,
      counterevidenceStatus: String(counterevidence.status || ""),
    };
  });
  const hypothesisRuleIds = new Set(hypotheses.map((hypothesis) => hypothesis.ruleId));
  if (
    evaluation === "matched"
    && (
      hypothesisRuleIds.size !== matchedRuleIds.length
      || matchedRuleIds.some((ruleId) => !hypothesisRuleIds.has(ruleId))
    )
  ) {
    issues.push("impact_hypothesis_rule_binding_invalid");
  }
  return {
    valid: issues.length === 0,
    issues,
    id: String(record.id || ""),
    adapterId: String(sourceBinding.adapter_id || ""),
    evaluation,
    matchedRuleIds,
    rulesetVersion: String(projection.ruleset_version || ""),
    hypotheses,
    sectorImpacts: hypotheses.filter((hypothesis) => hypothesis.areaKind === "sector"),
    securityImpacts: hypotheses.filter((hypothesis) => hypothesis.areaKind === "security"),
  };
}

export function normalizeSourceInboxItem(value) {
  const record = objectValue(value);
  const item = objectValue(record.item);
  const rawSafety = objectValue(record.safety);
  const rawAttachments = arrayOfObjects(record.attachments);
  const rawDrafts = arrayOfObjects(record.round_drafts);
  const impactProjectionField = record.impact_rule_projections;
  const rawImpactProjections = Array.isArray(impactProjectionField)
    ? arrayOfObjects(impactProjectionField)
    : [];
  const issues = [];
  if (record.version !== "source_inbox_item_record_v1") issues.push("record_version_invalid");
  if (record.item !== item || !Object.keys(item).length) issues.push("item_record_invalid");
  if (item.version !== "project_source_item_v1") issues.push("item_version_invalid");
  if (!SOURCE_INBOX_ID_RE.test(String(record.id || ""))) issues.push("item_id_invalid");
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
  if (
    !Array.isArray(impactProjectionField)
    || rawImpactProjections.length !== impactProjectionField.length
  ) {
    issues.push("impact_projections_invalid");
  }
  if (record.safety !== rawSafety || !Object.keys(rawSafety).length) issues.push("safety_record_invalid");
  for (const field of ["facts", "sources", "impact_hypotheses", "unknowns"]) {
    if (!Array.isArray(item[field])) issues.push(`${field}_invalid`);
  }
  if (record.external_claims_verification !== EXTERNAL_UNVERIFIED) {
    issues.push("external_verification_marker_invalid");
  }
  const tier = sourceInboxTierMeta(record.source_channel, record.source_tier);
  if (!tier.valid) issues.push("source_tier_invalid");
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
  const parentBinding = {
    itemSha256: String(record.item_sha256 || ""),
    serverFingerprint: String(record.server_fingerprint || ""),
    sourceChannel: String(record.source_channel || ""),
  };
  const impactRuleProjections = rawImpactProjections.map((projection) => (
    normalizeImpactProjection(projection, parentBinding)
  ));
  if (impactRuleProjections.some((projection) => !projection.valid)) {
    issues.push("impact_projection_invalid");
  }
  return {
    valid: issues.length === 0,
    issues,
    id: String(record.id || ""),
    version: String(record.version || ""),
    sourceChannel: String(record.source_channel || ""),
    sourceKey: String(record.source_key || ""),
    sourceTier: tier.id,
    sourceTierCode: tier.code,
    sourceTierLabel: tier.label,
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
    impactRuleProjections,
    impactEvaluationState: (
      !Array.isArray(impactProjectionField)
      || rawImpactProjections.length !== impactProjectionField.length
      || impactRuleProjections.some((projection) => !projection.valid)
    )
      ? "invalid"
      : impactRuleProjections.length
        ? (impactRuleProjections.some((projection) => projection.evaluation === "matched")
        ? "matched"
        : "no_match")
        : "not_evaluated",
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
  const root = objectValue(payload);
  const rawInbox = root.source_inbox;
  const inbox = objectValue(rawInbox);
  const rawCounts = objectValue(inbox.counts);
  const rawItems = arrayOfObjects(inbox.items);
  const rawFacets = arrayOfObjects(inbox.source_facets);
  const items = rawItems.map(normalizeSourceInboxItem);
  const issues = [];
  if (rawInbox !== inbox || inbox.version !== "source_inbox_list_v1") {
    issues.push("inbox_version_invalid");
  }
  if (
    !Array.isArray(inbox.items)
    || rawItems.length !== inbox.items.length
    || !Array.isArray(inbox.source_facets)
    || rawFacets.length !== inbox.source_facets.length
    || !Number.isSafeInteger(inbox.total_count)
    || inbox.total_count < 0
    || !Number.isSafeInteger(inbox.unread_count)
    || inbox.unread_count < 0
    || !Number.isSafeInteger(inbox.matched_count)
    || inbox.matched_count < 0
    || !Number.isSafeInteger(inbox.limit)
    || inbox.limit < 1
    || inbox.limit > 200
    || typeof inbox.query !== "string"
    || typeof inbox.state !== "string"
    || typeof inbox.source !== "string"
    || typeof inbox.unread !== "string"
    || !new Set(["", "true", "false"]).has(inbox.unread)
    || rawItems.length > inbox.limit
    || rawItems.length > inbox.matched_count
    || inbox.matched_count > inbox.total_count
    || inbox.unread_count > inbox.total_count
  ) {
    issues.push("inbox_structure_invalid");
  }
  if (
    inbox.counts !== rawCounts
    || Object.entries(rawCounts).some(([state, count]) => (
      !KNOWN_STATES.has(state) || !Number.isSafeInteger(count) || count < 0
    ))
    || Object.values(rawCounts).reduce((total, count) => total + boundedCount(count), 0)
      !== inbox.total_count
  ) {
    issues.push("inbox_counts_invalid");
  }
  const counts = Object.fromEntries(Object.entries(rawCounts).map(([state, count]) => [
    String(state),
    boundedCount(count),
  ]));
  const sourceFacets = rawFacets.map((facet) => {
    const tier = sourceInboxTierMeta(facet.source_channel, facet.source_tier);
    const sourceChannel = String(facet.source_channel || "");
    const sourceKey = String(facet.source_key || "");
    const sourceId = String(facet.source || "");
    const valid = (
      tier.valid
      && SOURCE_INBOX_ID_RE.test(sourceChannel)
      && SOURCE_INBOX_ID_RE.test(sourceKey)
      && sourceId === `${sourceChannel}:${sourceKey}`
      && Number.isSafeInteger(facet.count)
      && facet.count >= 0
      && Number.isSafeInteger(facet.unread_count)
      && facet.unread_count >= 0
      && facet.unread_count <= facet.count
    );
    if (!valid) issues.push("inbox_source_facet_invalid");
    return {
      id: sourceId,
      sourceChannel,
      sourceKey,
      sourceTier: tier.id,
      sourceTierCode: tier.code,
      sourceTierLabel: tier.label,
      count: boundedCount(facet.count),
      unreadCount: boundedCount(facet.unread_count),
      valid,
    };
  });
  if (
    sourceFacets.reduce((total, facet) => total + facet.count, 0) !== inbox.total_count
    || sourceFacets.reduce((total, facet) => total + facet.unreadCount, 0) !== inbox.unread_count
  ) {
    issues.push("inbox_source_accounting_invalid");
  }
  return {
    valid: issues.length === 0,
    issues,
    items,
    counts,
    totalCount: Object.hasOwn(inbox, "total_count")
      ? boundedCount(inbox.total_count)
      : items.length,
    unreadCount: Object.hasOwn(inbox, "unread_count")
      ? boundedCount(inbox.unread_count)
      : items.filter((item) => !item.acknowledged).length,
    matchedCount: Object.hasOwn(inbox, "matched_count")
      ? boundedCount(inbox.matched_count)
      : items.length,
    sourceFacets,
    query: String(inbox.query || ""),
    state: String(inbox.state || ""),
    source: String(inbox.source || ""),
    unread: inbox.unread === "true",
  };
}

export function normalizeSourceMonitoringHealth(payload) {
  const view = objectValue(objectValue(payload).source_monitoring_health);
  const settings = objectValue(view.settings);
  const runtime = objectValue(view.runtime);
  const safety = objectValue(view.safety);
  const rawAdapters = arrayOfObjects(view.adapters);
  const counts = objectValue(view.counts);
  const issues = [];
  if (view.version !== "source_monitoring_health_service_v3") issues.push("health_view_version_invalid");
  if (view.health_projection_version !== "source_monitoring_health_v1") {
    issues.push("health_version_invalid");
  }
  if (!KNOWN_HEALTH_STATES.has(String(view.state || ""))) issues.push("health_state_invalid");
  if (
    !Number.isSafeInteger(view.captured_at_ms)
    || view.captured_at_ms < 0
    || !Number.isSafeInteger(view.adapter_count)
    || view.adapter_count < 0
    || !Array.isArray(view.adapters)
    || rawAdapters.length !== view.adapters.length
    || view.adapter_count !== rawAdapters.length
    || typeof view.persistence_available !== "boolean"
  ) {
    issues.push("health_structure_invalid");
  }
  const countKeys = Object.keys(SOURCE_MONITORING_HEALTH_LABELS);
  if (
    countKeys.some((key) => !Number.isSafeInteger(counts[key]) || counts[key] < 0)
    || Object.keys(counts).some((key) => !KNOWN_HEALTH_STATES.has(key))
    || countKeys.reduce((total, key) => total + boundedCount(counts[key]), 0) !== rawAdapters.length
  ) {
    issues.push("health_counts_invalid");
  }
  if (
    !hasExactFields(settings, SOURCE_MONITORING_SETTINGS_FIELDS)
    || !["seed_only", "catch_up", "from_time"].includes(settings.initial_mode)
    || typeof settings.enabled !== "boolean"
    || typeof settings.auto_start !== "boolean"
    || typeof settings.official_only !== "boolean"
    || typeof settings.allow_readonly_market !== "boolean"
    || typeof settings.trading_impact_rules_enabled !== "boolean"
    || typeof settings.dry_run !== "boolean"
    || !Number.isSafeInteger(settings.max_items_per_run)
    || settings.max_items_per_run < 1
    || settings.max_items_per_run > 50
    || !Number.isSafeInteger(settings.catch_up_max_items)
    || settings.catch_up_max_items < 0
    || settings.catch_up_max_items > 50
    || typeof settings.initial_preview_sha256 !== "string"
    || (settings.initial_preview_sha256 !== "" && !SHA256_RE.test(settings.initial_preview_sha256))
    || typeof settings.from_time !== "string"
    || typeof settings.continuous_event_cutoff !== "string"
    || (settings.continuous_event_cutoff !== ""
      && !validCanonicalFromTime(settings.continuous_event_cutoff))
    || (!settings.official_only && !settings.allow_readonly_market)
    || (settings.auto_start && !settings.enabled)
    || (settings.initial_mode === "seed_only" && (
      settings.catch_up_max_items !== 0
      || settings.initial_preview_sha256 !== ""
      || settings.from_time !== ""
    ))
    || (settings.initial_mode === "catch_up" && (
      settings.catch_up_max_items < 1
      || settings.catch_up_max_items > settings.max_items_per_run
      || settings.from_time !== ""
    ))
    || (settings.initial_mode === "from_time" && (
      settings.catch_up_max_items !== 0
      || settings.initial_preview_sha256 !== ""
      || !validCanonicalFromTime(settings.from_time)
    ))
  ) {
    issues.push("health_settings_invalid");
  }
  const runtimeIntegerFields = [
    "started_at",
    "heartbeat_at",
    "last_loop_at",
    "next_due_at",
    "heartbeat_age_ms",
    "stall_after_ms",
  ];
  const runtimeNeedsId = ["starting", "running", "degraded", "stalled", "stopping"]
    .includes(runtime.status);
  const expectedRuntimeLiveness = (
    runtime.thread_alive === true
    && ["running", "degraded"].includes(runtime.status)
    && Number.isSafeInteger(runtime.heartbeat_age_ms)
    && Number.isSafeInteger(runtime.stall_after_ms)
    && runtime.heartbeat_age_ms <= runtime.stall_after_ms
  );
  if (
    !hasExactFields(runtime, SOURCE_MONITORING_RUNTIME_FIELDS)
    || runtime.version !== "source_monitoring_runtime_health_v1"
    || !KNOWN_RUNTIME_STATUSES.has(String(runtime.status || ""))
    || typeof runtime.runtime_id !== "string"
    || (runtime.runtime_id !== "" && !SOURCE_MONITORING_RUNTIME_ID_RE.test(runtime.runtime_id))
    || (runtimeNeedsId && runtime.runtime_id === "")
    || typeof runtime.active_adapter !== "string"
    || (runtime.active_adapter !== "" && !SOURCE_MONITORING_ADAPTER_KEY_RE.test(runtime.active_adapter))
    || typeof runtime.last_fatal_error_code !== "string"
    || (runtime.last_fatal_error_code !== "" && !SOURCE_MONITORING_RUNTIME_ERROR_RE.test(runtime.last_fatal_error_code))
    || runtimeIntegerFields.some((field) => !Number.isSafeInteger(runtime[field]) || runtime[field] < 0)
    || runtime.stall_after_ms < 1
    || typeof runtime.thread_alive !== "boolean"
    || typeof runtime.liveness_verified !== "boolean"
    || typeof runtime.enabled !== "boolean"
    || typeof runtime.auto_start !== "boolean"
    || typeof runtime.dry_run !== "boolean"
    || runtime.enabled !== settings.enabled
    || runtime.auto_start !== settings.auto_start
    || runtime.dry_run !== settings.dry_run
    || runtime.execution_capability !== "none"
    || runtime.live_trading_allowed !== false
    || runtime.liveness_verified !== expectedRuntimeLiveness
    || (["running", "degraded"].includes(runtime.status) && (
      !runtime.thread_alive
      || runtime.heartbeat_age_ms > runtime.stall_after_ms
    ))
    || (runtime.status === "stalled" && (
      !runtime.thread_alive
      || runtime.liveness_verified
      || runtime.heartbeat_age_ms <= runtime.stall_after_ms
    ))
    || (["disabled", "stopped"].includes(runtime.status) && runtime.thread_alive)
    || (["starting", "stopping"].includes(runtime.status) && !runtime.thread_alive)
    || (runtime.status === "failed" && runtime.last_fatal_error_code === "")
    || (runtime.status !== "failed" && runtime.last_fatal_error_code !== "")
    || (runtime.active_adapter !== "" && !["running", "degraded", "stalled"].includes(runtime.status))
    || (runtime.status === "disabled" && runtime.runtime_id !== "")
    || typeof view.runtime_liveness_verified !== "boolean"
    || view.runtime_liveness_verified !== runtime.liveness_verified
  ) {
    issues.push("health_runtime_invalid");
  }
  if (
    safety.execution_capability !== "none"
    || safety.live_trading_allowed !== false
    || safety.database_writes_performed !== 0
    || safety.provider_calls_performed !== 0
    || safety.network_requests_performed !== 0
    || safety.market_calls_performed !== 0
    || safety.formal_rounds_created !== 0
  ) {
    issues.push("health_execution_boundary_invalid");
  }
  const adapters = rawAdapters.map((adapter, index) => {
    const metadata = objectValue(adapter.metadata);
    const latestRun = adapter.latest_run === null ? null : objectValue(adapter.latest_run);
    const adapterIssues = [];
    if (adapter.version !== "source_adapter_health_v1") adapterIssues.push("version");
    if (!KNOWN_HEALTH_STATES.has(String(adapter.state || ""))) adapterIssues.push("state");
    if (adapter.execution_capability !== "none" || adapter.live_trading_allowed !== false) {
      adapterIssues.push("execution");
    }
    if (typeof adapter.runtime_liveness_verified !== "boolean") adapterIssues.push("liveness");
    if (
      !SOURCE_INBOX_ID_RE.test(String(adapter.adapter_key || ""))
      || typeof adapter.enabled !== "boolean"
      || typeof adapter.running !== "boolean"
      || typeof adapter.catalog_registered !== "boolean"
      || typeof adapter.persisted_state !== "boolean"
      || typeof adapter.persisted_enabled !== "boolean"
      || !KNOWN_HEALTH_CONFIG_STATES.has(String(adapter.config_status || ""))
      || !["last_checked_at_ms", "last_success_at_ms", "last_event_at_ms", "next_due_at_ms", "consecutive_failures", "discovery_delay_ms"]
        .every((field) => Number.isSafeInteger(adapter[field]) && adapter[field] >= 0)
      || (adapter.persisted_enabled === true && adapter.persisted_state !== true)
      || (adapter.enabled === true && (
        adapter.catalog_registered !== true
        || adapter.persisted_enabled !== true
        || adapter.config_status !== "current"
      ))
    ) {
      adapterIssues.push("structure");
    }
    if (
      (adapter.catalog_registered === true && (
        !Object.keys(metadata).length
        || !Number.isSafeInteger(metadata.poll_interval_ms)
        || metadata.poll_interval_ms < 60_000
        || metadata.poll_interval_ms > 604_800_000
        || metadata.execution_capability !== "none"
        || metadata.live_trading_allowed !== false
      ))
      || (adapter.catalog_registered === false && adapter.metadata !== null)
      || (adapter.latest_run !== null && (
        !Object.keys(latestRun).length
        || latestRun.version !== "source_adapter_run_v1"
        || !["RUNNING", "SUCCEEDED", "DEGRADED", "DRY_RUN", "FAILED", "ABANDONED"].includes(latestRun.status)
        || typeof latestRun.dry_run !== "boolean"
        || !["observed_count", "accepted_count", "duplicate_count", "rejected_count", "completed_at_ms"]
          .every((field) => Number.isSafeInteger(latestRun[field]) && latestRun[field] >= 0)
      ))
    ) {
      adapterIssues.push("evidence");
    }
    if (adapterIssues.length) issues.push(`health_adapter_invalid_${index}`);
    return {
      valid: adapterIssues.length === 0,
      adapterKey: String(adapter.adapter_key || ""),
      sourceClass: String(metadata.source_class || ""),
      sourceChannel: String(metadata.source_channel || ""),
      officialSource: metadata.official_source === true,
      state: String(adapter.state || "failed"),
      enabled: adapter.enabled === true,
      running: adapter.running === true,
      runtimeLivenessVerified: adapter.runtime_liveness_verified === true,
      persistedStatePresent: adapter.persisted_state === true,
      persistedEnabled: adapter.persisted_enabled === true,
      configStatus: String(adapter.config_status || ""),
      configVersion: String(metadata.config_version || ""),
      pollIntervalMs: boundedNumber(metadata.poll_interval_ms),
      latestRunStatus: String(objectValue(adapter.latest_run).status || ""),
      latestRun: latestRun === null ? null : {
        status: String(latestRun.status || ""),
        dryRun: latestRun.dry_run === true,
        observedCount: boundedCount(latestRun.observed_count),
        acceptedCount: boundedCount(latestRun.accepted_count),
        duplicateCount: boundedCount(latestRun.duplicate_count),
        rejectedCount: boundedCount(latestRun.rejected_count),
        completedAt: boundedNumber(latestRun.completed_at_ms),
      },
      lastCheckedAt: boundedNumber(adapter.last_checked_at_ms),
      lastSuccessAt: boundedNumber(adapter.last_success_at_ms),
      lastEventAt: boundedNumber(adapter.last_event_at_ms),
      nextDueAt: boundedNumber(adapter.next_due_at_ms),
      consecutiveFailures: boundedCount(adapter.consecutive_failures),
      lastErrorCode: String(adapter.last_error_code || ""),
    };
  });
  if (new Set(adapters.map((adapter) => adapter.adapterKey)).size !== adapters.length) {
    issues.push("health_adapter_identity_invalid");
  }
  const adapterByKey = new Map(adapters.map((adapter) => [adapter.adapterKey, adapter]));
  if (
    runtime.active_adapter !== ""
    && (!adapterByKey.has(runtime.active_adapter) || !adapterByKey.get(runtime.active_adapter).enabled)
  ) {
    issues.push("health_runtime_active_adapter_invalid");
  }
  rawAdapters.forEach((adapter, index) => {
    const expectedLiveness = view.runtime_liveness_verified === true && adapter.enabled === true;
    const expectedRunning = expectedLiveness && runtime.active_adapter === adapter.adapter_key;
    if (
      adapter.runtime_liveness_verified !== expectedLiveness
      || adapter.running !== expectedRunning
    ) {
      issues.push(`health_adapter_runtime_invalid_${index}`);
    }
  });
  for (const state of Object.keys(SOURCE_MONITORING_HEALTH_LABELS)) {
    if (counts[state] !== adapters.filter((adapter) => adapter.state === state).length) {
      issues.push("health_state_accounting_invalid");
      break;
    }
  }
  const projectedStates = new Set(adapters.map((adapter) => adapter.state));
  const expectedOverallState = !adapters.length
    ? "idle"
    : projectedStates.size === 1 && projectedStates.has("disabled")
      ? "disabled"
      : projectedStates.has("failed")
        ? "failed"
        : projectedStates.has("degraded")
          ? "degraded"
          : projectedStates.has("backing_off")
            ? "backing_off"
            : projectedStates.has("running")
              ? "running"
              : projectedStates.has("healthy")
                ? "healthy"
                : "idle";
  if (view.state !== expectedOverallState) issues.push("health_overall_state_invalid");
  if (settings.enabled !== true && adapters.some((adapter) => adapter.enabled)) {
    issues.push("health_default_off_boundary_invalid");
  }
  return {
    valid: issues.length === 0,
    issues,
    capturedAt: boundedNumber(view.captured_at_ms),
    state: String(view.state || "failed"),
    stateLabel: SOURCE_MONITORING_HEALTH_LABELS[view.state] || "健康记录异常",
    globalEnabled: settings.enabled === true,
    autoStart: settings.auto_start === true,
    dryRun: settings.dry_run === true,
    officialOnly: settings.official_only === true,
    allowReadonlyMarket: settings.allow_readonly_market === true,
    initialMode: String(settings.initial_mode || ""),
    catchUpMaxItems: boundedCount(settings.catch_up_max_items),
    initialPreviewSha256: String(settings.initial_preview_sha256 || ""),
    fromTime: String(settings.from_time || ""),
    continuousEventCutoff: String(settings.continuous_event_cutoff || ""),
    runtime: {
      version: String(runtime.version || ""),
      status: String(runtime.status || "failed"),
      statusLabel: SOURCE_MONITORING_RUNTIME_LABELS[runtime.status] || "Runtime 状态异常",
      runtimeId: String(runtime.runtime_id || ""),
      startedAt: boundedNumber(runtime.started_at),
      heartbeatAt: boundedNumber(runtime.heartbeat_at),
      lastLoopAt: boundedNumber(runtime.last_loop_at),
      activeAdapter: String(runtime.active_adapter || ""),
      nextDueAt: boundedNumber(runtime.next_due_at),
      threadAlive: runtime.thread_alive === true,
      lastFatalErrorCode: String(runtime.last_fatal_error_code || ""),
      heartbeatAgeMs: boundedNumber(runtime.heartbeat_age_ms),
      stallAfterMs: boundedNumber(runtime.stall_after_ms),
      livenessVerified: runtime.liveness_verified === true,
      enabled: runtime.enabled === true,
      autoStart: runtime.auto_start === true,
      dryRun: runtime.dry_run === true,
      executionCapability: String(runtime.execution_capability || ""),
      liveTradingAllowed: runtime.live_trading_allowed === true,
    },
    adapters,
    persistenceAvailable: view.persistence_available === true,
    executionCapability: String(safety.execution_capability || ""),
    liveTradingAllowed: safety.live_trading_allowed === true,
  };
}

export function sourceMonitoringCheckLabel(adapter) {
  const run = adapter.latestRun;
  if (!run) return "尚无本轮结果记录";
  if (run.status === "RUNNING") return "本次检查进行中";
  if (["FAILED", "DEGRADED", "ABANDONED"].includes(run.status) || run.rejectedCount > 0) {
    return "最近检查失败或有拒绝项，请查看错误码";
  }
  if (run.status === "DRY_RUN" || run.dryRun) {
    return `试运行检查完成，观察到 ${run.observedCount} 条；未导入收件箱`;
  }
  if (run.status !== "SUCCEEDED" || run.completedAt <= 0) return "最近检查结果待核实";
  if (run.observedCount === 0 && run.acceptedCount === 0 && run.duplicateCount === 0) {
    return "检查成功、无新增";
  }
  return `检查成功：观察 ${run.observedCount} 条，新增导入 ${run.acceptedCount} 条，重复 ${run.duplicateCount} 条`;
}

export function sourceMonitoringNextStep(adapter) {
  if (adapter.adapterKey === "company_ir") {
    if (adapter.lastErrorCode === "COMPANY_IR_BASELINE_UPGRADE_REQUIRED") {
      return "旧公司 IR checkpoint 无法证明完整首次基线；先停用来源并明确升级方案，保留原 checkpoint、收件箱、房间及材料，不自动重置或迁移。";
    }
    if (adapter.lastErrorCode === "COMPANY_IR_CHECKPOINT_CAPACITY_EXCEEDED") {
      return "公司 IR 当前元数据与已保留记录超过已见标识容量上限；停用来源并评估容量与升级方案，保留全部已见标识和收件箱，不自动淘汰、重置或迁移。";
    }
    if (adapter.lastErrorCode === "COMPANY_IR_BASELINE_SCOPE_INCOMPLETE") {
      return "公司 IR 首次基线未覆盖当前已配置来源的完整元数据范围（RSS 或 Q4 JSON 及其绑定的时间元数据）；核查缺失响应、解析错误及被过滤记录，修复后重试检查。该范围不代表全部历史公告；当前基线不视为完成，保留原 checkpoint 和收件箱，不自动重置或迁移。";
    }
  }
  if (adapter.adapterKey === "sec_filings" && (
    adapter.configStatus === "migration_required"
    || /SEC_.*(?:BASELINE|CHECKPOINT|UPGRADE)/.test(adapter.lastErrorCode)
  )) {
    return "旧 SEC 状态需先核对完整基线并明确升级方案；保留原 checkpoint 和收件箱，不自动重置或迁移。";
  }
  if (adapter.configStatus === "migration_required" || adapter.configStatus === "unregistered") {
    return "核对当前来源配置与保存的版本，在接入设置中重读；不要清空旧状态。";
  }
  if (adapter.lastErrorCode) {
    return "按错误码核对来源响应、网络与配置，观察下次退避检查；未成功前不会作为正常监控。";
  }
  return "";
}

export function sourceMonitoringOperationState(health, control = null) {
  if (!health?.valid) return { state: "unknown", label: "监控状态待核实", detail: "重新读取有效健康记录。" };
  if (!health.globalEnabled) return {
    state: "disabled", label: "监控未启用", detail: "全局开关关闭，不会自动检查来源。",
  };
  const enabled = health.adapters.filter((adapter) => adapter.enabled);
  const hasFailure = health.adapters.some((adapter) => (adapter.enabled || adapter.persistedEnabled) && (
    adapter.consecutiveFailures > 0 || adapter.lastErrorCode
    || ["migration_required", "unregistered"].includes(adapter.configStatus)
    || ["failed", "degraded", "backing_off"].includes(adapter.state)
    || ["FAILED", "DEGRADED", "ABANDONED"].includes(adapter.latestRun?.status)
    || adapter.latestRun?.rejectedCount > 0
  ));
  if (["failed", "stalled", "degraded"].includes(health.runtime.status) || hasFailure) return {
    state: "degraded", label: `监控降级 · ${health.runtime.statusLabel}`,
    detail: "应用页面可用不代表监控正常；核对下方错误码、来源配置和下次检查。",
  };
  if (!enabled.length) return {
    state: "disabled", label: "监控未启用 · 等待首次检查", detail: "尚无生效来源；请查看 Adapter 接入设置。",
  };
  if (!health.runtime.livenessVerified) return {
    state: "stopped", label: "监控未运行", detail: "当前后台未通过存活检查；核对自动启动配置和 Runtime 状态。",
  };
  if (health.dryRun) return {
    state: "dry_run", label: "监控试运行（dry-run）", detail: "检查只生成预览；不写收件箱，也不完成首次基线。",
  };
  const controlsMatch = control?.valid === true
    && control.settings.globalEnabled === health.globalEnabled
    && control.settings.autoStart === health.autoStart
    && control.settings.dryRun === health.dryRun;
  const baselineComplete = controlsMatch && enabled.every((adapter) => {
    const sourceControl = control.adapters.find((entry) => entry.adapterKey === adapter.adapterKey);
    return sourceControl?.configVersion === adapter.configVersion
      && sourceControl.effectiveEnabled === true
      && sourceControl.initializationStatus === "complete"
      && sourceControl.initializationCompletedAt <= health.capturedAt;
  });
  if (!baselineComplete) return {
    state: "baseline_pending",
    label: controlsMatch ? "基线未完成或待核实" : "基线状态未读取",
    detail: "在接入设置中读取初始化收据；已有心跳或历史 checkpoint 不能证明完整首次基线。",
  };
  const recentSuccess = enabled.every((adapter) => (
    adapter.lastSuccessAt > 0
    && adapter.lastSuccessAt <= health.capturedAt
    && health.capturedAt - adapter.lastSuccessAt <= adapter.pollIntervalMs + health.runtime.stallAfterMs
  ));
  return recentSuccess
    ? { state: "operational", label: "监控正常", detail: "生效来源均已完成基线，并在轮询周期及运行容许时间内检查成功。" }
    : { state: "degraded", label: "监控降级 · 缺少近期成功检查", detail: "后台有心跳，但来源缺少近期成功检查；核对最近成功时间与轮询周期。" };
}

export function normalizeSourceInboxNotificationFeed(
  payload,
  { requestedCursor = null } = {},
) {
  const feed = objectValue(objectValue(payload).source_notifications);
  const safety = objectValue(feed.safety);
  const rawNotifications = arrayOfObjects(feed.notifications);
  const issues = [];
  if (feed.version !== "source_inbox_notification_feed_v1") {
    issues.push("notification_feed_version_invalid");
  }
  if (typeof feed.baseline !== "boolean") issues.push("notification_baseline_invalid");
  if (
    !Array.isArray(feed.notifications)
    || rawNotifications.length !== feed.notifications.length
    || typeof feed.has_more !== "boolean"
    || !Number.isSafeInteger(feed.limit)
    || feed.limit < 1
    || feed.limit > 100
    || rawNotifications.length > feed.limit
  ) {
    issues.push("notification_structure_invalid");
  }
  if (
    requestedCursor !== null
    && (
      typeof requestedCursor !== "string"
      || (requestedCursor !== "" && !SOURCE_INBOX_CURSOR_RE.test(requestedCursor))
      || (requestedCursor === "" && feed.baseline !== true)
      || (requestedCursor !== "" && feed.baseline !== false)
    )
  ) {
    issues.push("notification_request_binding_invalid");
  }
  if (
    typeof feed.cursor !== "string"
    || !SOURCE_INBOX_CURSOR_RE.test(feed.cursor)
    || typeof feed.head_cursor !== "string"
    || !SOURCE_INBOX_CURSOR_RE.test(feed.head_cursor)
  ) {
    issues.push("notification_cursor_invalid");
  }
  if (boundedCount(feed.unread_count, -1) < 0) issues.push("notification_unread_count_invalid");
  if (
    safety.external_claims_verification !== EXTERNAL_UNVERIFIED
    || safety.execution_capability !== "none"
    || safety.live_trading_allowed !== false
    || safety.provider_calls_performed !== 0
    || safety.market_calls_performed !== 0
    || safety.formal_rounds_created !== 0
  ) {
    issues.push("notification_safety_invalid");
  }
  const notifications = rawNotifications.map((notification, index) => {
    const eventSafety = objectValue(notification.safety);
    const tier = sourceInboxTierMeta(notification.source_channel, notification.source_tier);
    const valid = (
      notification.version === "source_inbox_notification_v1"
      && SOURCE_INBOX_ID_RE.test(String(notification.id || ""))
      && Number.isSafeInteger(notification.created_at)
      && notification.created_at >= 0
      && typeof notification.source_channel === "string"
      && notification.source_channel.length > 0
      && typeof notification.source_key === "string"
      && notification.source_key.length > 0
      && typeof notification.item_type === "string"
      && notification.item_type.length > 0
      && typeof notification.severity === "string"
      && notification.severity.length > 0
      && typeof notification.occurred_at === "string"
      && typeof notification.headline === "string"
      && notification.headline.length > 0
      && notification.acknowledged === false
      && notification.external_claims_verification === EXTERNAL_UNVERIFIED
      && eventSafety.fact_confirmation === false
      && eventSafety.approval === false
      && eventSafety.execution_authorization === false
      && tier.valid
    );
    if (!valid) issues.push(`notification_item_invalid_${index}`);
    return {
      valid,
      eventId: String(notification.id || ""),
      createdAt: boundedNumber(notification.created_at),
      sourceChannel: String(notification.source_channel || ""),
      sourceKey: String(notification.source_key || ""),
      sourceTier: tier.id,
    };
  });
  if (new Set(notifications.map((notification) => notification.eventId)).size !== notifications.length) {
    issues.push("notification_identity_invalid");
  }
  if (boundedCount(feed.unread_count, -1) < rawNotifications.length) {
    issues.push("notification_accounting_invalid");
  }
  if (
    (feed.baseline === true && (
      rawNotifications.length !== 0
      || feed.has_more !== false
      || feed.cursor !== feed.head_cursor
    ))
    || (feed.baseline === false && feed.has_more === false && feed.cursor !== feed.head_cursor)
    || (feed.has_more === true && rawNotifications.length === 0)
    || (feed.has_more === true && feed.cursor === feed.head_cursor)
    || (
      requestedCursor !== null
      && requestedCursor !== ""
      && rawNotifications.length > 0
      && feed.cursor === requestedCursor
    )
  ) {
    issues.push("notification_cursor_semantics_invalid");
  }
  return {
    valid: issues.length === 0,
    issues,
    baseline: feed.baseline === true,
    notifications,
    cursor: String(feed.cursor || ""),
    headCursor: String(feed.head_cursor || ""),
    unreadCount: boundedCount(feed.unread_count),
    hasMore: feed.has_more === true,
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
