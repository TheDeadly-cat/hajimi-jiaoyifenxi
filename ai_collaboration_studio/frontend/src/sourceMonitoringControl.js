const SHA256_RE = /^[0-9a-f]{64}$/;
const ADAPTER_KEY_RE = /^[a-z][a-z0-9_]{0,63}$/;
const TOKEN_RE = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/;
const ERROR_CODE_RE = /^[A-Z][A-Z0-9_]{0,99}$/;
const RFC3339_UTC_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/;

const CONTROL_FIELDS = Object.freeze([
  "version", "captured_at_ms", "settings", "adapters", "safety",
]);
const SETTINGS_FIELDS = Object.freeze([
  "global_enabled", "auto_start", "dry_run", "initial_mode",
  "catch_up_max_items", "from_time", "continuous_event_cutoff",
]);
const ADAPTER_FIELDS = Object.freeze([
  "version", "adapter_key", "config_version", "state_version",
  "persisted_state", "persisted_enabled", "effective_enabled", "active_run",
  "source_class", "source_channel", "official_source",
  "initialization_status", "initialization_mode",
  "initialization_preview_sha256", "initialization_completed_at_ms",
  "pending_authorization", "can_preview", "can_enable", "can_disable",
  "blocked_reason_codes",
]);
const PREVIEW_FIELDS = Object.freeze([
  "version", "adapter_key", "config_version", "state_version", "mode",
  "initial_required", "initialization_blocked", "catch_up_max_items",
  "from_time", "candidate_count", "selected_count", "skipped_count",
  "adapter_duplicate_count", "source_error_count", "rejected_count",
  "earliest_occurred_at", "latest_occurred_at", "preview_sha256",
  "starting_checkpoint_sha256", "next_checkpoint_sha256", "captured_at_ms",
  "safety",
]);
const STATIC_SEED_PREVIEW_FIELDS = Object.freeze([
  ...PREVIEW_FIELDS,
  "preview_kind", "candidate_evidence", "source_policy_sha256", "symbol_allowlist",
]);
const RESULT_FIELDS = Object.freeze([
  "version", "adapter_key", "config_version", "state_version",
  "persisted_enabled", "initialization_authorized", "preview_sha256", "safety",
]);
const SAFETY_FIELDS = Object.freeze([
  "database_writes_performed", "checkpoint_writes_performed",
  "source_inbox_writes_performed", "provider_calls_performed",
  "model_calls_performed", "formal_rounds_created", "market_calls_performed",
  "network_requests_performed", "execution_capability", "live_trading_allowed",
]);
const PREVIEW_SAFETY_FIELDS = Object.freeze([
  ...SAFETY_FIELDS,
  "network_requests_accounting",
]);

export const SOURCE_MONITORING_INITIAL_MODES = Object.freeze([
  "seed_only", "catch_up", "from_time",
]);
export const SOURCE_MONITORING_INITIALIZATION_STATUSES = Object.freeze([
  "required", "authorized", "complete", "legacy",
]);

function objectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function hasExactFields(value, fields) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const keys = Object.keys(value);
  return keys.length === fields.length && fields.every((field) => Object.hasOwn(value, field));
}

function safeInteger(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function canonicalFromTime(value) {
  if (typeof value !== "string" || !RFC3339_UTC_RE.test(value)) return false;
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return false;
  const iso = new Date(timestamp).toISOString();
  return value === (iso.endsWith(".000Z") ? iso.replace(".000Z", "Z") : iso);
}

function validModeFields(mode, catchUpMaxItems, fromTime) {
  if (!SOURCE_MONITORING_INITIAL_MODES.includes(mode)) return false;
  if (!safeInteger(catchUpMaxItems) || catchUpMaxItems > 50) return false;
  if (typeof fromTime !== "string") return false;
  if (mode === "seed_only") return catchUpMaxItems === 0 && fromTime === "";
  if (mode === "catch_up") return catchUpMaxItems >= 1 && fromTime === "";
  return catchUpMaxItems === 0 && canonicalFromTime(fromTime);
}

function normalizeSafety(
  rawSafety,
  { profile = "control" } = {},
) {
  const safety = objectValue(rawSafety);
  const actionProfile = profile === "preview" || profile === "enablement";
  const fields = actionProfile ? PREVIEW_SAFETY_FIELDS : SAFETY_FIELDS;
  const zeroEvidence = safety.provider_calls_performed === 0
    && safety.model_calls_performed === 0
    && safety.formal_rounds_created === 0;
  const actionAccounting = actionProfile
    && safeInteger(safety.market_calls_performed)
    && (safety.network_requests_performed === null
      || safeInteger(safety.network_requests_performed))
    && ["exact", "not_instrumented"].includes(safety.network_requests_accounting)
    && (safety.network_requests_performed === null
      ? safety.network_requests_accounting === "not_instrumented"
      : safety.network_requests_accounting === "exact");
  const profileEvidence = profile === "control"
    ? SAFETY_FIELDS.slice(0, 8).every((field) => safety[field] === 0)
    : profile === "preview"
      ? safety.database_writes_performed === 0
        && safety.checkpoint_writes_performed === 0
        && safety.source_inbox_writes_performed === 0
        && actionAccounting
      : profile === "enablement"
        ? safety.database_writes_performed === true
          && safety.checkpoint_writes_performed === false
          && safety.source_inbox_writes_performed === false
          && actionAccounting
        : false;
  const valid = hasExactFields(safety, fields)
    && safety.execution_capability === "none"
    && safety.live_trading_allowed === false
    && zeroEvidence
    && profileEvidence;
  return {
    valid,
    marketCallsPerformed: safeInteger(safety.market_calls_performed)
      ? safety.market_calls_performed
      : null,
    networkRequestsPerformed: safety.network_requests_performed === null
      || safeInteger(safety.network_requests_performed)
      ? safety.network_requests_performed
      : null,
    networkRequestsAccounting: String(safety.network_requests_accounting || ""),
  };
}

function normalizedSettings(rawSettings) {
  const settings = objectValue(rawSettings);
  const valid = hasExactFields(settings, SETTINGS_FIELDS)
    && ["global_enabled", "auto_start", "dry_run"].every(
      (field) => typeof settings[field] === "boolean",
    )
    && (!settings.auto_start || settings.global_enabled)
    && validModeFields(
      settings.initial_mode,
      settings.catch_up_max_items,
      settings.from_time,
    )
    && typeof settings.continuous_event_cutoff === "string"
    && (settings.continuous_event_cutoff === ""
      || canonicalFromTime(settings.continuous_event_cutoff));
  return {
    valid,
    globalEnabled: settings.global_enabled === true,
    autoStart: settings.auto_start === true,
    dryRun: settings.dry_run === true,
    initialMode: String(settings.initial_mode || ""),
    catchUpMaxItems: safeInteger(settings.catch_up_max_items)
      ? settings.catch_up_max_items
      : 0,
    fromTime: String(settings.from_time || ""),
    continuousEventCutoff: String(settings.continuous_event_cutoff || ""),
  };
}

function normalizedAdapter(rawAdapter) {
  const adapter = objectValue(rawAdapter);
  const blocked = Array.isArray(adapter.blocked_reason_codes)
    ? adapter.blocked_reason_codes
    : [];
  const previewHash = String(adapter.initialization_preview_sha256 || "");
  const status = String(adapter.initialization_status || "");
  const valid = hasExactFields(adapter, ADAPTER_FIELDS)
    && adapter.version === "source_monitoring_adapter_control_v1"
    && ADAPTER_KEY_RE.test(String(adapter.adapter_key || ""))
    && TOKEN_RE.test(String(adapter.config_version || ""))
    && safeInteger(adapter.state_version)
    && [
      "persisted_state", "persisted_enabled", "effective_enabled", "active_run",
      "official_source", "pending_authorization", "can_preview", "can_enable",
      "can_disable",
    ].every((field) => typeof adapter[field] === "boolean")
    && (!adapter.persisted_state
      ? adapter.state_version === 0 && !adapter.persisted_enabled
      : adapter.state_version >= 1)
    && (!adapter.persisted_enabled || adapter.persisted_state)
    && (!adapter.effective_enabled || adapter.persisted_enabled)
    && ["official_source", "readonly_market"].includes(adapter.source_class)
    && TOKEN_RE.test(String(adapter.source_channel || ""))
    && (adapter.official_source === (adapter.source_class === "official_source"))
    && SOURCE_MONITORING_INITIALIZATION_STATUSES.includes(status)
    && (status === "legacy"
      ? adapter.initialization_mode === ""
      : SOURCE_MONITORING_INITIAL_MODES.includes(adapter.initialization_mode))
    && (previewHash === "" || SHA256_RE.test(previewHash))
    && safeInteger(adapter.initialization_completed_at_ms)
    && blocked.length === adapter.blocked_reason_codes?.length
    && blocked.length <= 50
    && blocked.every((code) => typeof code === "string" && ERROR_CODE_RE.test(code))
    && (status === "required"
      ? !adapter.pending_authorization && previewHash === "" && adapter.initialization_completed_at_ms === 0
      : true)
    && (status === "authorized"
      ? adapter.pending_authorization && SHA256_RE.test(previewHash) && adapter.initialization_completed_at_ms === 0
      : true)
    && (status === "complete"
      ? !adapter.pending_authorization && SHA256_RE.test(previewHash) && adapter.initialization_completed_at_ms > 0
      : true)
    && (status === "legacy"
      ? !adapter.pending_authorization && previewHash === "" && adapter.initialization_completed_at_ms === 0
      : true)
    && (!adapter.active_run || adapter.persisted_enabled)
    && (!adapter.can_disable || (adapter.persisted_enabled && !adapter.active_run));
  return {
    valid,
    adapterKey: String(adapter.adapter_key || ""),
    configVersion: String(adapter.config_version || ""),
    stateVersion: safeInteger(adapter.state_version) ? adapter.state_version : 0,
    persistedState: adapter.persisted_state === true,
    persistedEnabled: adapter.persisted_enabled === true,
    effectiveEnabled: adapter.effective_enabled === true,
    activeRun: adapter.active_run === true,
    sourceClass: String(adapter.source_class || ""),
    sourceChannel: String(adapter.source_channel || ""),
    officialSource: adapter.official_source === true,
    initializationStatus: status,
    initializationMode: String(adapter.initialization_mode || ""),
    initializationPreviewSha256: previewHash,
    initializationCompletedAt: safeInteger(adapter.initialization_completed_at_ms)
      ? adapter.initialization_completed_at_ms
      : 0,
    pendingAuthorization: adapter.pending_authorization === true,
    canPreview: adapter.can_preview === true,
    canEnable: adapter.can_enable === true,
    canDisable: adapter.can_disable === true,
    blockedReasonCodes: blocked.map(String),
  };
}

export function normalizeSourceMonitoringOperatorControl(payload) {
  const view = objectValue(objectValue(payload).source_monitoring_operator_control);
  const settings = normalizedSettings(view.settings);
  const rawAdapters = Array.isArray(view.adapters) ? view.adapters : [];
  const adapters = rawAdapters.map(normalizedAdapter);
  const safety = normalizeSafety(view.safety, { profile: "control" });
  const issues = [];
  if (!hasExactFields(view, CONTROL_FIELDS)
    || view.version !== "source_monitoring_operator_control_v2") {
    issues.push("operator_control_contract_invalid");
  }
  if (!safeInteger(view.captured_at_ms) || !settings.valid) {
    issues.push("operator_control_settings_invalid");
  }
  if (!Array.isArray(view.adapters)
    || adapters.some((adapter) => !adapter.valid)
    || new Set(adapters.map((adapter) => adapter.adapterKey)).size !== adapters.length) {
    issues.push("operator_control_adapters_invalid");
  }
  if (!safety.valid) issues.push("operator_control_safety_invalid");
  if (adapters.some((adapter) => adapter.effectiveEnabled && !settings.globalEnabled)) {
    issues.push("operator_control_enablement_invalid");
  }
  return {
    valid: issues.length === 0,
    issues,
    capturedAt: safeInteger(view.captured_at_ms) ? view.captured_at_ms : 0,
    settings,
    adapters,
  };
}

export function normalizeSourceMonitoringOperatorPreview(payload) {
  const view = objectValue(objectValue(payload).source_monitoring_operator_preview);
  const safety = normalizeSafety(view.safety, { profile: "preview" });
  const countFields = [
    "candidate_count", "selected_count", "skipped_count",
    "adapter_duplicate_count", "source_error_count", "rejected_count",
  ];
  const hashes = [
    "preview_sha256", "starting_checkpoint_sha256", "next_checkpoint_sha256",
  ];
  const issues = [];
  const staticSeed = view.version === "source_monitoring_operator_static_seed_preview_v2";
  const expectedFields = staticSeed ? STATIC_SEED_PREVIEW_FIELDS : PREVIEW_FIELDS;
  if (!hasExactFields(view, expectedFields)
    || (!staticSeed && view.version !== "source_monitoring_operator_preview_v1")) {
    issues.push("operator_preview_contract_invalid");
  }
  if (!ADAPTER_KEY_RE.test(String(view.adapter_key || ""))
    || !TOKEN_RE.test(String(view.config_version || ""))
    || !safeInteger(view.state_version)
    || typeof view.initial_required !== "boolean"
    || typeof view.initialization_blocked !== "boolean"
    || !validModeFields(view.mode, view.catch_up_max_items, view.from_time)
    || countFields.some((field) => !safeInteger(view[field]))
    || view.selected_count + view.skipped_count !== view.candidate_count
    || typeof view.earliest_occurred_at !== "string"
    || typeof view.latest_occurred_at !== "string"
    || hashes.some((field) => !SHA256_RE.test(String(view[field] || "")))
    || !safeInteger(view.captured_at_ms)
    || (view.initialization_blocked !== (
      view.source_error_count > 0 || view.rejected_count > 0
    ))) {
    issues.push("operator_preview_evidence_invalid");
  }
  if (!safety.valid) issues.push("operator_preview_safety_invalid");
  if (staticSeed && (
    view.preview_kind !== "static_seed_policy"
    || view.candidate_evidence !== "deferred_to_first_runtime_poll"
    || view.mode !== "seed_only"
    || view.initialization_blocked !== false
    || countFields.some((field) => view[field] !== 0)
    || view.earliest_occurred_at !== ""
    || view.latest_occurred_at !== ""
    || view.next_checkpoint_sha256 !== view.starting_checkpoint_sha256
    || !SHA256_RE.test(String(view.source_policy_sha256 || ""))
    || !Array.isArray(view.symbol_allowlist)
    || view.symbol_allowlist.length < 1
    || view.symbol_allowlist.length > 50
    || view.symbol_allowlist.some((symbol) => typeof symbol !== "string" || !symbol)
    || new Set(view.symbol_allowlist).size !== view.symbol_allowlist.length
    || safety.marketCallsPerformed !== 0
    || safety.networkRequestsPerformed !== 0
    || safety.networkRequestsAccounting !== "exact"
  )) issues.push("operator_static_seed_preview_invalid");
  return {
    valid: issues.length === 0,
    issues,
    adapterKey: String(view.adapter_key || ""),
    configVersion: String(view.config_version || ""),
    stateVersion: safeInteger(view.state_version) ? view.state_version : 0,
    mode: String(view.mode || ""),
    initialRequired: view.initial_required === true,
    initializationBlocked: view.initialization_blocked === true,
    catchUpMaxItems: safeInteger(view.catch_up_max_items) ? view.catch_up_max_items : 0,
    fromTime: String(view.from_time || ""),
    candidateCount: safeInteger(view.candidate_count) ? view.candidate_count : 0,
    selectedCount: safeInteger(view.selected_count) ? view.selected_count : 0,
    skippedCount: safeInteger(view.skipped_count) ? view.skipped_count : 0,
    adapterDuplicateCount: safeInteger(view.adapter_duplicate_count)
      ? view.adapter_duplicate_count
      : 0,
    sourceErrorCount: safeInteger(view.source_error_count) ? view.source_error_count : 0,
    rejectedCount: safeInteger(view.rejected_count) ? view.rejected_count : 0,
    earliestOccurredAt: String(view.earliest_occurred_at || ""),
    latestOccurredAt: String(view.latest_occurred_at || ""),
    previewSha256: String(view.preview_sha256 || ""),
    startingCheckpointSha256: String(view.starting_checkpoint_sha256 || ""),
    nextCheckpointSha256: String(view.next_checkpoint_sha256 || ""),
    capturedAt: safeInteger(view.captured_at_ms) ? view.captured_at_ms : 0,
    marketCallsPerformed: safety.marketCallsPerformed ?? 0,
    previewKind: staticSeed ? "static_seed_policy" : "exact_content",
    candidateEvidence: staticSeed
      ? "deferred_to_first_runtime_poll"
      : "observed",
    sourcePolicySha256: staticSeed ? String(view.source_policy_sha256 || "") : "",
    symbolAllowlist: staticSeed && Array.isArray(view.symbol_allowlist)
      ? view.symbol_allowlist.map(String)
      : [],
  };
}

export function normalizeSourceMonitoringEnablementResult(payload) {
  const view = objectValue(objectValue(payload).source_monitoring_enablement_result);
  const safety = normalizeSafety(view.safety, { profile: "enablement" });
  const previewHash = String(view.preview_sha256 || "");
  const issues = [];
  if (!hasExactFields(view, RESULT_FIELDS)
    || view.version !== "source_monitoring_enablement_result_v1") {
    issues.push("operator_enablement_contract_invalid");
  }
  if (!ADAPTER_KEY_RE.test(String(view.adapter_key || ""))
    || !TOKEN_RE.test(String(view.config_version || ""))
    || !safeInteger(view.state_version)
    || view.state_version < 1
    || typeof view.persisted_enabled !== "boolean"
    || typeof view.initialization_authorized !== "boolean"
    || (previewHash !== "" && !SHA256_RE.test(previewHash))
    || (view.initialization_authorized && (!view.persisted_enabled || !SHA256_RE.test(previewHash)))
    || (!view.initialization_authorized && previewHash !== "")) {
    issues.push("operator_enablement_result_invalid");
  }
  if (!safety.valid) issues.push("operator_enablement_safety_invalid");
  if (
    view.initialization_authorized === false
    && (
      safety.marketCallsPerformed !== 0
      || safety.networkRequestsPerformed !== 0
      || safety.networkRequestsAccounting !== "exact"
    )
  ) {
    issues.push("operator_enablement_side_effect_accounting_invalid");
  }
  return {
    valid: issues.length === 0,
    issues,
    adapterKey: String(view.adapter_key || ""),
    configVersion: String(view.config_version || ""),
    stateVersion: safeInteger(view.state_version) ? view.state_version : 0,
    persistedEnabled: view.persisted_enabled === true,
    initializationAuthorized: view.initialization_authorized === true,
    previewSha256: previewHash,
  };
}

export function sourceMonitoringConfirmationText(preview) {
  if (!preview?.valid) return "";
  if (preview.mode === "seed_only") {
    if (preview.previewKind === "static_seed_policy") {
      return "我确认静态只读行情 Seed 政策；当前未读取行情，首次 Runtime 成功轮询将验证完整快照、零历史导入并原子建立 checkpoint。";
    }
    return `我确认首次成功运行将以当时全部候选建立基线；本次预览当前有 ${preview.skippedCount} 条，来源变化可能改变数量。`;
  }
  if (preview.mode === "catch_up") {
    return `我确认最多补录 ${preview.catchUpMaxItems} 条 external_unverified 来源。`;
  }
  return `我确认只处理 ${preview.fromTime} 起的候选；内容仍未经事实核验。`;
}
