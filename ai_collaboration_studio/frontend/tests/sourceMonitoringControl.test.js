import assert from "node:assert/strict";
import test from "node:test";

import {
  normalizeSourceMonitoringEnablementResult,
  normalizeSourceMonitoringOperatorControl,
  normalizeSourceMonitoringOperatorPreview,
  sourceMonitoringConfirmationText,
} from "../src/sourceMonitoringControl.js";

function safety({ preview = false, writeFlags = false, writes = 0 } = {}) {
  return {
    database_writes_performed: writeFlags ? Boolean(writes) : writes,
    checkpoint_writes_performed: writeFlags ? false : 0,
    source_inbox_writes_performed: writeFlags ? false : 0,
    provider_calls_performed: 0,
    model_calls_performed: 0,
    formal_rounds_created: 0,
    market_calls_performed: 0,
    network_requests_performed: preview ? null : 0,
    execution_capability: "none",
    live_trading_allowed: false,
    ...(preview ? { network_requests_accounting: "not_instrumented" } : {}),
  };
}

function adapter(overrides = {}) {
  return {
    version: "source_monitoring_adapter_control_v1",
    adapter_key: "sec_filings",
    config_version: "sec_filings_config_v2",
    state_version: 0,
    persisted_state: false,
    persisted_enabled: false,
    effective_enabled: false,
    active_run: false,
    source_class: "official_source",
    source_channel: "official_source_monitor",
    official_source: true,
    initialization_status: "required",
    initialization_mode: "seed_only",
    initialization_preview_sha256: "",
    initialization_completed_at_ms: 0,
    pending_authorization: false,
    can_preview: true,
    can_enable: false,
    can_disable: false,
    blocked_reason_codes: [],
    ...overrides,
  };
}

function control(overrides = {}) {
  return {
    source_monitoring_operator_control: {
      version: "source_monitoring_operator_control_v2",
      captured_at_ms: 1_777_777_777_000,
      settings: {
        global_enabled: true,
        auto_start: false,
        dry_run: true,
        initial_mode: "seed_only",
        catch_up_max_items: 0,
        from_time: "",
        continuous_event_cutoff: "",
      },
      adapters: [adapter()],
      safety: safety(),
      ...overrides,
    },
  };
}

function trialControl() {
  return control({
    version: "source_monitoring_operator_control_v3",
    profile: {
      version: "source_monitoring_profile_v1",
      profile_id: "sec_micron_trial_v1",
      label: "SEC + Micron 官方来源试用",
      initial_mode: "seed_only",
      sources: [
        { adapter_key: "sec_filings", symbols: ["US.NVDA"], forms: ["8-K"],
          format: "sec_submissions_recent", history_limit: 1000,
          per_symbol_limit: 3, poll_interval_ms: 300_000 },
        { adapter_key: "company_ir", symbols: ["US.MU"], forms: [],
          format: "micron_q4_public_json_v1", history_limit: 30,
          per_symbol_limit: 8, poll_interval_ms: 300_000 },
      ],
      model_calls_allowed: false,
      execution_capability: "none",
      live_trading_allowed: false,
      scope_sha256: "e6f07d2c97d71c1280ab653352fc019cd99845be5bd06468ae6ace65899454e3",
    },
    adapters: [adapter(), adapter({ adapter_key: "company_ir", config_version: "company_ir_config_v2" })],
  });
}

function preview(overrides = {}) {
  return {
    source_monitoring_operator_preview: {
      version: "source_monitoring_operator_preview_v1",
      adapter_key: "sec_filings",
      config_version: "sec_filings_config_v2",
      state_version: 0,
      mode: "seed_only",
      initial_required: true,
      initialization_blocked: false,
      catch_up_max_items: 0,
      from_time: "",
      candidate_count: 3,
      selected_count: 0,
      skipped_count: 3,
      adapter_duplicate_count: 0,
      source_error_count: 0,
      rejected_count: 0,
      earliest_occurred_at: "2026-09-01T00:00:00Z",
      latest_occurred_at: "2026-09-02T00:00:00Z",
      preview_sha256: "a".repeat(64),
      starting_checkpoint_sha256: "b".repeat(64),
      next_checkpoint_sha256: "c".repeat(64),
      captured_at_ms: 1_777_777_777_000,
      safety: safety({ preview: true }),
      ...overrides,
    },
  };
}

function staticSeedPreview(overrides = {}) {
  const payload = preview({
    version: "source_monitoring_operator_static_seed_preview_v2",
    preview_kind: "static_seed_policy",
    candidate_evidence: "deferred_to_first_runtime_poll",
    adapter_key: "futu_anomaly_signals",
    config_version: "futu_anomaly_config_v2_0123456789abcdef",
    candidate_count: 0,
    selected_count: 0,
    skipped_count: 0,
    earliest_occurred_at: "",
    latest_occurred_at: "",
    source_policy_sha256: "d".repeat(64),
    symbol_allowlist: ["US.MU", "US.SNDK", "US.WDC", "US.STX"],
    next_checkpoint_sha256: "b".repeat(64),
    ...overrides,
  });
  payload.source_monitoring_operator_preview.safety.network_requests_performed = 0;
  payload.source_monitoring_operator_preview.safety.network_requests_accounting = "exact";
  return payload;
}

test("operator control accepts exact neutral state and preserves legacy unknown mode", () => {
  const normalized = normalizeSourceMonitoringOperatorControl(control());
  assert.equal(normalized.valid, true, normalized.issues.join(","));
  assert.equal(normalized.settings.autoStart, false);
  assert.equal(normalized.adapters[0].initializationStatus, "required");
  assert.equal(normalized.profile, null);

  const legacyPayload = control({
    adapters: [adapter({
      state_version: 4,
      persisted_state: true,
      initialization_status: "legacy",
      initialization_mode: "",
      can_preview: false,
      can_enable: true,
    })],
  });
  assert.equal(normalizeSourceMonitoringOperatorControl(legacyPayload).valid, true);
});

test("trial control exposes only the exact backend profile and defensive scope copies", () => {
  const payload = trialControl();
  const normalized = normalizeSourceMonitoringOperatorControl(payload);
  assert.equal(normalized.valid, true, normalized.issues.join(","));
  assert.equal(normalized.profile.label, "SEC + Micron 官方来源试用");
  assert.equal(normalized.profile.initialMode, "seed_only");
  assert.equal(normalized.profile.modelCallsAllowed, false);
  assert.deepEqual(normalized.profile.sources.map((source) => source.symbols), [["US.NVDA"], ["US.MU"]]);
  assert.deepEqual(normalized.profile.sources.map((source) => source.pollIntervalMs), [300_000, 300_000]);
  normalized.profile.sources[0].symbols.push("US.MU");
  assert.deepEqual(payload.source_monitoring_operator_control.profile.sources[0].symbols, ["US.NVDA"]);
});

test("trial control rejects scope expansion, authority drift, missing fields and extra adapters", () => {
  const mutations = [
    ["unknown control field", (view) => { view.extra = true; }],
    ["missing profile", (view) => { delete view.profile; }],
    ["unknown profile field", (view) => { view.profile.url = "https://example.invalid"; }],
    ["unknown source field", (view) => { view.profile.sources[0].extra = true; }],
    ["profile id", (view) => { view.profile.profile_id = "other_trial"; }],
    ["scope hash", (view) => { view.profile.scope_sha256 = "a".repeat(64); }],
    ["expanded SEC symbols", (view) => { view.profile.sources[0].symbols.push("US.MU"); }],
    ["expanded IR symbols", (view) => { view.profile.sources[1].symbols.push("US.WDC"); }],
    ["expanded forms", (view) => { view.profile.sources[0].forms.push("10-K"); }],
    ["IR forms", (view) => { view.profile.sources[1].forms.push("8-K"); }],
    ["IR format", (view) => { view.profile.sources[1].format = "rss"; }],
    ["history limit", (view) => { view.profile.sources[1].history_limit = 31; }],
    ["per-symbol limit", (view) => { view.profile.sources[0].per_symbol_limit = 4; }],
    ["poll cadence", (view) => { view.profile.sources[0].poll_interval_ms = 60_000; }],
    ["model authority", (view) => { view.profile.model_calls_allowed = true; }],
    ["execution authority", (view) => { view.profile.execution_capability = "orders"; }],
    ["trading authority", (view) => { view.profile.live_trading_allowed = true; }],
    ["profile initial mode", (view) => { view.profile.initial_mode = "catch_up"; }],
    ["settings initial mode", (view) => { view.settings.initial_mode = "catch_up"; view.settings.catch_up_max_items = 1; }],
    ["continuous cutoff", (view) => { view.settings.continuous_event_cutoff = "2026-09-01T00:00:00Z"; }],
    ["adapter initial mode", (view) => { view.adapters[0].initialization_mode = "catch_up"; }],
    ["extra adapter", (view) => { view.adapters.push(adapter({ adapter_key: "federal_reserve" })); }],
    ["missing adapter", (view) => { view.adapters.pop(); }],
    ["replacement adapter", (view) => { view.adapters[1].adapter_key = "futu_anomaly_signals"; }],
    ["duplicate adapter", (view) => { view.adapters[1] = { ...view.adapters[0] }; }],
    ["market source", (view) => { view.adapters[1].source_class = "readonly_market"; view.adapters[1].official_source = false; }],
    ["legacy v2 with profile", (view) => { view.version = "source_monitoring_operator_control_v2"; }],
  ];
  for (const [name, mutate] of mutations) {
    const payload = trialControl();
    mutate(payload.source_monitoring_operator_control);
    assert.equal(normalizeSourceMonitoringOperatorControl(payload).valid, false, name);
  }
});

test("control fails closed on extra fields, invented legacy mode, or execution capability", () => {
  const oldVersion = control();
  oldVersion.source_monitoring_operator_control.version =
    "source_monitoring_operator_control_v1";
  assert.equal(normalizeSourceMonitoringOperatorControl(oldVersion).valid, false);

  const extra = control();
  extra.source_monitoring_operator_control.pid = 42;
  assert.equal(normalizeSourceMonitoringOperatorControl(extra).valid, false);

  const inventedLegacy = control({
    adapters: [adapter({
      initialization_status: "legacy",
      initialization_mode: "seed_only",
    })],
  });
  assert.equal(normalizeSourceMonitoringOperatorControl(inventedLegacy).valid, false);

  const unsafe = control();
  unsafe.source_monitoring_operator_control.safety.execution_capability = "orders";
  assert.equal(normalizeSourceMonitoringOperatorControl(unsafe).valid, false);
});

test("initial preview seals mode-specific counts and never treats errors as confirmable", () => {
  const normalized = normalizeSourceMonitoringOperatorPreview(preview());
  assert.equal(normalized.valid, true, normalized.issues.join(","));
  assert.match(sourceMonitoringConfirmationText(normalized), /本次预览当前有 3 条，来源变化可能改变数量/);

  const catchUp = normalizeSourceMonitoringOperatorPreview(preview({
    mode: "catch_up",
    catch_up_max_items: 2,
    candidate_count: 3,
    selected_count: 2,
    skipped_count: 1,
  }));
  assert.equal(catchUp.valid, true);
  assert.match(sourceMonitoringConfirmationText(catchUp), /最多补录 2 条 external_unverified/);

  const blocked = preview({
    initialization_blocked: true,
    source_error_count: 1,
  });
  const blockedView = normalizeSourceMonitoringOperatorPreview(blocked);
  assert.equal(blockedView.valid, true);
  assert.equal(blockedView.initializationBlocked, true);

  const forged = preview({ initialization_blocked: false, source_error_count: 1 });
  assert.equal(normalizeSourceMonitoringOperatorPreview(forged).valid, false);
});

test("static market seed preview is explicit deferred evidence with zero live calls", () => {
  const normalized = normalizeSourceMonitoringOperatorPreview(staticSeedPreview());
  assert.equal(normalized.valid, true, normalized.issues.join(","));
  assert.equal(normalized.previewKind, "static_seed_policy");
  assert.equal(normalized.candidateEvidence, "deferred_to_first_runtime_poll");
  assert.deepEqual(normalized.symbolAllowlist, ["US.MU", "US.SNDK", "US.WDC", "US.STX"]);
  assert.match(sourceMonitoringConfirmationText(normalized), /当前未读取行情/);

  const forged = staticSeedPreview({ candidate_count: 1 });
  assert.equal(normalizeSourceMonitoringOperatorPreview(forged).valid, false);

  const missingSymbols = staticSeedPreview();
  delete missingSymbols.source_monitoring_operator_preview.symbol_allowlist;
  const missingSymbolsView = normalizeSourceMonitoringOperatorPreview(missingSymbols);
  assert.equal(missingSymbolsView.valid, false);
  assert.deepEqual(missingSymbolsView.symbolAllowlist, []);

  const nonArraySymbols = staticSeedPreview({ symbol_allowlist: "US.MU" });
  const nonArraySymbolsView = normalizeSourceMonitoringOperatorPreview(nonArraySymbols);
  assert.equal(nonArraySymbolsView.valid, false);
  assert.deepEqual(nonArraySymbolsView.symbolAllowlist, []);
});

test("enablement result is exact, bounded, and cannot grant execution", () => {
  const payload = {
    source_monitoring_enablement_result: {
      version: "source_monitoring_enablement_result_v1",
      adapter_key: "sec_filings",
      config_version: "sec_filings_config_v2",
      state_version: 1,
      persisted_enabled: true,
      initialization_authorized: true,
      preview_sha256: "a".repeat(64),
      safety: safety({ preview: true, writeFlags: true, writes: 1 }),
    },
  };
  const normalized = normalizeSourceMonitoringEnablementResult(payload);
  assert.equal(normalized.valid, true, normalized.issues.join(","));

  payload.source_monitoring_enablement_result.safety.live_trading_allowed = true;
  assert.equal(normalizeSourceMonitoringEnablementResult(payload).valid, false);
});

test("operator safety profiles reject forged write and read evidence", () => {
  const forgedEnablement = {
    source_monitoring_enablement_result: {
      version: "source_monitoring_enablement_result_v1",
      adapter_key: "sec_filings",
      config_version: "sec_filings_config_v2",
      state_version: 2,
      persisted_enabled: false,
      initialization_authorized: false,
      preview_sha256: "",
      safety: safety({ preview: true, writeFlags: true, writes: 1 }),
    },
  };
  forgedEnablement.source_monitoring_enablement_result.safety.checkpoint_writes_performed = true;
  assert.equal(normalizeSourceMonitoringEnablementResult(forgedEnablement).valid, false);
  forgedEnablement.source_monitoring_enablement_result.safety.checkpoint_writes_performed = false;
  forgedEnablement.source_monitoring_enablement_result.safety.database_writes_performed = false;
  assert.equal(normalizeSourceMonitoringEnablementResult(forgedEnablement).valid, false);
  forgedEnablement.source_monitoring_enablement_result.safety.database_writes_performed = true;
  forgedEnablement.source_monitoring_enablement_result.safety.market_calls_performed = 1;
  assert.equal(normalizeSourceMonitoringEnablementResult(forgedEnablement).valid, false);
  forgedEnablement.source_monitoring_enablement_result.safety.market_calls_performed = 0;
  forgedEnablement.source_monitoring_enablement_result.state_version = 0;
  assert.equal(normalizeSourceMonitoringEnablementResult(forgedEnablement).valid, false);

  const forgedControl = control();
  forgedControl.source_monitoring_operator_control.safety.network_requests_performed = 1;
  assert.equal(normalizeSourceMonitoringOperatorControl(forgedControl).valid, false);
});
