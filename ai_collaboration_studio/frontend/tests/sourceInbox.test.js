import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  EXTERNAL_UNVERIFIED,
  normalizeSourceInboxItem,
  normalizeSourceInboxNotificationFeed,
  normalizeSourceInboxResponse,
  normalizeSourceMonitoringHealth,
  replaceSourceInboxItem,
  sourceInboxItemPermissions,
} from "../src/sourceInbox.js";
import {
  normalizeSourceImportPreview,
  normalizeSourceImportResult,
  normalizeSourceMonitoringPromptTemplate,
  SOURCE_IMPORT_MAX_BYTES,
  sourceImportUtf8Bytes,
} from "../src/sourceInboxImport.js";

const panelSource = readFileSync(
  new URL("../src/components/SourceInboxPanel.jsx", import.meta.url),
  "utf8",
);
const panelStyles = readFileSync(
  new URL("../src/styles/source-inbox.css", import.meta.url),
  "utf8",
);

function sourceRecord(overrides = {}) {
  return {
    version: "source_inbox_item_record_v1",
    id: "source_item_one",
    source_channel: "chatgpt_scheduled_task",
    source_key: "github_ci_watch",
    source_tier: "external_manual",
    external_run_id: "run_one",
    received_at: 1_777_777_777_000,
    server_fingerprint: "a".repeat(64),
    item_sha256: "b".repeat(64),
    state: "AWAITING_USER",
    state_version: 1,
    acknowledged: false,
    acknowledged_by: "",
    acknowledged_at: 0,
    expires_at: 1_888_888_888_000,
    updated_at: 1_777_777_777_000,
    external_claims_verification: EXTERNAL_UNVERIFIED,
    item: {
      version: "project_source_item_v1",
      item_type: "ci_run_failure",
      severity: "high",
      occurred_at: "2026-08-28T12:55:00Z",
      published_at: "2026-08-28T12:56:00Z",
      headline: "CI 运行结果摘要",
      summary: "外部系统声明测试失败。",
      facts: [{ claim: "workflow conclusion is failure", source_indexes: [0] }],
      sources: [{
        url: "https://github.com/acme/project/actions/runs/100",
        publisher: "GitHub",
        source_type: "official_platform",
        published_at: "2026-08-28T12:56:00Z",
        content_sha256: "c".repeat(64),
      }],
      impact_hypotheses: [{
        statement: "发布窗口可能受影响。",
        affected_area: "release readiness",
        time_horizon: "next publication",
        confidence: 0.7,
        source_indexes: [0],
      }],
      unknowns: ["失败断言尚未导入。"],
      confidence: 0.9,
      recommended_route: "open_round_draft",
    },
    attachments: [],
    round_drafts: [],
    impact_rule_projections: [],
    events: [],
    safety: {
      acknowledgement_is_fact_confirmation: false,
      formal_round_created: false,
      provider_calls_performed: 0,
      market_calls_performed: 0,
      execution_capability: "none",
    },
    ...overrides,
  };
}

function sourceImportPreview(overrides = {}) {
  const item = {
    version: "project_source_item_v1",
    external_item_id: "github-run-100",
    item_type: "ci_run_failure",
    severity: "high",
    occurred_at: "2026-08-28T12:55:00Z",
    published_at: "2026-08-28T12:56:00Z",
    entities: [{ kind: "repository", id: "acme/project", label: "project" }],
    headline: "CI 运行结果摘要",
    summary: "外部系统声明测试失败。",
    facts: [{ claim: "workflow conclusion is failure", source_indexes: [0] }],
    sources: [{
      url: "https://github.com/acme/project/actions/runs/100",
      publisher: "GitHub",
      source_type: "official_platform",
      published_at: "2026-08-28T12:56:00Z",
      content_sha256: "c".repeat(64),
    }],
    impact_hypotheses: [{
      statement: "发布窗口可能受影响。",
      affected_area: "release readiness",
      time_horizon: "next publication",
      source_indexes: [0],
      confidence: 0.7,
    }],
    unknowns: ["失败断言尚未导入。"],
    confidence: 0.9,
    recommended_route: "open_round_draft",
    extensions: {},
    external_claims_verification: EXTERNAL_UNVERIFIED,
    server_fingerprint_version: "project_source_item_fingerprint_v1",
    server_fingerprint: "a".repeat(64),
  };
  return {
    ok: true,
    source_import_preview: {
      version: "source_import_preview_v1",
      valid: true,
      received_at_ms: 1_777_777_777_000,
      packet: {
        version: "source_import_packet_v1",
        source_channel: "chatgpt_manual",
        source_key: "github_ci_watch",
        external_run_id: "run one with spaces",
        checked_at: "2026-08-28T13:03:00Z",
        cutoff_at: "2026-08-28T13:00:00Z",
        meaningful_change: true,
        items: [item],
        generation: {
          channel: "chatgpt_manual",
          model: "",
          cost: { status: "unavailable", amount: null, currency: "", usage_source: "subscription_unavailable" },
          correlated_output: true,
        },
        external_claims_verification: {
          checked_at: EXTERNAL_UNVERIFIED,
          cutoff_at: EXTERNAL_UNVERIFIED,
          item_times: EXTERNAL_UNVERIFIED,
          source_times: EXTERNAL_UNVERIFIED,
          model: EXTERNAL_UNVERIFIED,
          cost: EXTERNAL_UNVERIFIED,
          recommended_routes: EXTERNAL_UNVERIFIED,
        },
        safety: {
          execution_fields_present: false,
          execution_capability: "none",
          provider_calls_performed: 0,
          market_calls_performed: 0,
          network_requests_performed: 0,
          user_action_required: true,
        },
      },
      candidate: {
        source_payload_bytes: 1024,
        source_payload_sha256: "b".repeat(64),
        normalized_packet_sha256: "c".repeat(64),
        import_key_version: "source_import_key_v1",
        import_key_sha256: "d".repeat(64),
        item_count: 1,
        source_count: 1,
        item_fingerprints: ["a".repeat(64)],
      },
      store_disposition: { evaluated: false, reason: "preview_does_not_open_database" },
      external_claims_verification: EXTERNAL_UNVERIFIED,
      safety: {
        database_reads_performed: 0,
        database_writes_performed: 0,
        provider_calls_performed: 0,
        market_calls_performed: 0,
        network_requests_performed: 0,
        formal_rounds_created: 0,
        chatgpt_page_controlled: false,
        chatgpt_automation_performed: false,
        external_task_created: false,
        import_performed: false,
        execution_capability: "none",
        revalidation_required: true,
        user_confirmation_required: true,
      },
      preview_sha256: "e".repeat(64),
      ...overrides,
    },
  };
}

function promptTemplateResponse(overrides = {}) {
  return {
    source_monitoring_prompt_template: {
      version: "source_monitoring_prompt_template_v1",
      template_id: "manual_chatgpt_source_monitoring",
      default_source_channel: "chatgpt_manual",
      packet_version: "source_import_packet_v1",
      item_version: "project_source_item_v1",
      prompt: "只读监控 {{monitoring_scope}}\nsource_import_packet_v1\nexternal_unverified",
      result_template: {},
      constraints: {
        one_json_object_only: true,
        markdown_fence_tolerated: true,
        manual_copy_paste_only: true,
        unmodified_template_is_importable: false,
        public_http_sources_only: true,
        reserved_source_channels: ["futu_anomaly_monitor", "official_source_monitor"],
        severities: ["critical", "high", "info", "low", "medium"],
        recommended_routes: ["attach_to_room", "notify_only", "open_round_draft"],
        max_payload_bytes: SOURCE_IMPORT_MAX_BYTES,
        max_items: 50,
        max_sources_per_item: 12,
        max_total_sources: 200,
      },
      safety: {
        database_reads_performed: 0,
        database_writes_performed: 0,
        provider_calls_performed: 0,
        market_calls_performed: 0,
        network_requests_performed: 0,
        formal_rounds_created: 0,
        chatgpt_page_controlled: false,
        chatgpt_automation_performed: false,
        external_task_created: false,
        execution_capability: "none",
        user_review_required: true,
      },
      template_sha256: "f".repeat(64),
      ...overrides,
    },
  };
}

function sourceImportResult(items = [sourceRecord()], overrides = {}) {
  return {
    source_import: {
      version: "source_inbox_import_result_v1",
      import_id: "source_import_one",
      status: "AWAITING_USER",
      receipt: {
        version: "source_import_receipt_v1",
        status: "AWAITING_USER",
        received_at_ms: 1_777_777_777_000,
        source_payload_bytes: 1024,
        source_payload_sha256: "1".repeat(64),
        normalized_packet_sha256: "2".repeat(64),
        import_key_version: "source_import_key_v1",
        import_key_sha256: "3".repeat(64),
        source_channel: "chatgpt_manual",
        source_key: "github_ci_watch",
        external_run_id: "run_one",
        item_count: items.length,
        source_count: items.length,
        item_fingerprints: items.map((item) => item.server_fingerprint),
        external_claims_verification: EXTERNAL_UNVERIFIED,
        safety: {
          database_writes_performed: 0,
          provider_calls_performed: 0,
          market_calls_performed: 0,
          network_requests_performed: 0,
          execution_capability: "none",
          user_action_required: true,
        },
        receipt_sha256: "4".repeat(64),
      },
      items,
      idempotent_replay: false,
      created_item_count: items.length,
      duplicate_item_count: 0,
      ...overrides,
    },
  };
}

function impactProjection(overrides = {}) {
  const hypothesis = {
    version: "trading_impact_hypothesis_v1",
    hypothesis_sha256: "d".repeat(64),
    rule_id: "macro_release_review_v1",
    impact_hypothesis: {
      statement: "A fixed macro rule maps this event to DRAM research review.",
      affected_area: "sector:dram",
      time_horizon: "next_release_window",
      source_indexes: [0],
      confidence: 0.5,
    },
    affected_area_binding: {
      kind: "sector",
      id: "dram",
      security_ids: ["US.MU"],
    },
    time_dimension: { horizon_id: "next_release_window" },
    confidence_basis: { outcome_probability: false },
    counterevidence: { status: "unknown" },
  };
  return {
    version: "source_inbox_trading_impact_projection_record_v1",
    id: "impact_one",
    source_item_sha256: "b".repeat(64),
    server_fingerprint: "a".repeat(64),
    projection_sha256: "e".repeat(64),
    status: "MATCHED",
    hypothesis_count: 1,
    projection: {
      version: "trading_impact_projection_v1",
      ruleset_version: "trading_impact_rules_v1",
      source_binding: {
        adapter_id: "federal_reserve",
        source_channel: "official_source_monitor",
      },
      source_item_binding: {
        item_sha256: "b".repeat(64),
        server_fingerprint: "a".repeat(64),
      },
      evaluation: "matched",
      matched_rule_ids: ["macro_release_review_v1"],
      hypotheses: [hypothesis],
      verification_state: EXTERNAL_UNVERIFIED,
      interpretation_boundary: {
        directional_forecast: false,
        causal_attribution: "none",
        profitability_claim: false,
        execution_authority: "none",
        user_review_required: true,
      },
      accounting: {
        model_calls_performed: 0,
        provider_calls_performed: 0,
        network_requests_performed: 0,
        market_calls_performed: 0,
        database_writes_performed: 0,
      },
      projection_sha256: "e".repeat(64),
    },
    safety: {
      model_calls_performed: 0,
      provider_calls_performed: 0,
      network_requests_performed: 0,
      market_calls_performed: 0,
      database_writes_performed: 0,
      formal_rounds_created: 0,
      live_trading_allowed: false,
      execution_capability: "none",
    },
    ...overrides,
  };
}

test("source inbox normalization preserves the external-unverified boundary", () => {
  const item = normalizeSourceInboxItem(sourceRecord());

  assert.equal(item.valid, true);
  assert.equal(item.externalClaimsVerification, EXTERNAL_UNVERIFIED);
  assert.equal(item.headline, "CI 运行结果摘要");
  assert.deepEqual(item.facts.map((fact) => fact.claim), ["workflow conclusion is failure"]);
  assert.equal(item.sources[0].publisher, "GitHub");
  assert.equal(item.safety.formalRoundCreated, false);
  assert.equal(item.safety.providerCallsPerformed, 0);
  assert.equal(item.safety.marketCallsPerformed, 0);
  assert.equal(item.sourceTier, "external_manual");
  assert.equal(item.sourceTierLabel, "外部人工导入");
});

test("sealed channels determine provenance tier without changing verification", () => {
  const official = normalizeSourceInboxItem(sourceRecord({
    source_channel: "official_source_monitor",
    source_key: "federal_reserve",
    source_tier: "official_source",
    impact_rule_projections: [impactProjection()],
  }));
  assert.equal(official.valid, true);
  assert.equal(official.sourceTier, "official_source");
  assert.equal(official.externalClaimsVerification, EXTERNAL_UNVERIFIED);
  assert.equal(official.impactEvaluationState, "matched");
  assert.deepEqual(official.impactRuleProjections[0].sectorImpacts.map((item) => item.areaId), ["dram"]);

  const forgedTier = normalizeSourceInboxItem(sourceRecord({ source_tier: "official_source" }));
  assert.equal(forgedTier.valid, false);
  assert.equal(forgedTier.sourceTierCode, "EXT");
  assert.equal(forgedTier.sourceTierLabel, "外部人工导入");
  assert.ok(forgedTier.issues.includes("source_tier_invalid"));

  const missingOfficialTierRecord = sourceRecord({
    source_channel: "official_source_monitor",
    source_key: "federal_reserve",
  });
  delete missingOfficialTierRecord.source_tier;
  const missingOfficialTier = normalizeSourceInboxItem(missingOfficialTierRecord);
  assert.equal(missingOfficialTier.valid, false);
  assert.equal(missingOfficialTier.sourceTier, "external_manual");
  assert.ok(missingOfficialTier.issues.includes("source_tier_invalid"));

  const directional = impactProjection();
  directional.projection.interpretation_boundary.directional_forecast = true;
  const unsafe = normalizeSourceInboxItem(sourceRecord({
    source_channel: "official_source_monitor",
    source_key: "federal_reserve",
    source_tier: "official_source",
    impact_rule_projections: [directional],
  }));
  assert.equal(unsafe.valid, false);
  assert.ok(unsafe.issues.includes("impact_projection_invalid"));
  assert.equal(unsafe.impactRuleProjections[0].valid, false);
  assert.equal(unsafe.impactEvaluationState, "invalid");
  assert.equal(sourceInboxItemPermissions(unsafe, "room_one").actionable, false);

  for (const impact_rule_projections of ["not-an-array", [null, 42]]) {
    const malformedSidecar = normalizeSourceInboxItem(sourceRecord({ impact_rule_projections }));
    assert.equal(malformedSidecar.valid, false);
    assert.ok(malformedSidecar.issues.includes("impact_projections_invalid"));
    assert.equal(malformedSidecar.impactEvaluationState, "invalid");
    assert.equal(sourceInboxItemPermissions(malformedSidecar, "room_one").actionable, false);
  }

  const missingHypotheses = impactProjection();
  delete missingHypotheses.projection.hypotheses;
  const missingNestedArray = normalizeSourceInboxItem(sourceRecord({
    source_channel: "official_source_monitor",
    source_key: "federal_reserve",
    source_tier: "official_source",
    impact_rule_projections: [missingHypotheses],
  }));
  assert.equal(missingNestedArray.valid, false);
  assert.ok(missingNestedArray.impactRuleProjections[0].issues.includes("impact_hypotheses_invalid"));

  const emptyMatch = impactProjection();
  emptyMatch.projection.hypotheses = [];
  emptyMatch.hypothesis_count = 0;
  const emptyMatchedProjection = normalizeSourceInboxItem(sourceRecord({
    source_channel: "official_source_monitor",
    source_key: "federal_reserve",
    source_tier: "official_source",
    impact_rule_projections: [emptyMatch],
  }));
  assert.equal(emptyMatchedProjection.valid, false);
  assert.ok(emptyMatchedProjection.impactRuleProjections[0].issues.includes("impact_matched_hypotheses_invalid"));

  const unboundRule = impactProjection();
  delete unboundRule.projection.matched_rule_ids;
  const unboundProjection = normalizeSourceInboxItem(sourceRecord({
    source_channel: "official_source_monitor",
    source_key: "federal_reserve",
    source_tier: "official_source",
    impact_rule_projections: [unboundRule],
  }));
  assert.equal(unboundProjection.valid, false);
  assert.ok(unboundProjection.impactRuleProjections[0].issues.includes("impact_matched_rules_invalid"));

  const mismatchedRule = impactProjection();
  mismatchedRule.projection.matched_rule_ids = ["different_rule_v1"];
  const mismatchedProjection = normalizeSourceInboxItem(sourceRecord({
    source_channel: "official_source_monitor",
    source_key: "federal_reserve",
    source_tier: "official_source",
    impact_rule_projections: [mismatchedRule],
  }));
  assert.equal(mismatchedProjection.valid, false);
  assert.ok(mismatchedProjection.impactRuleProjections[0].issues.includes("impact_hypothesis_rule_binding_invalid"));
});

test("malformed safety or trust projections fail closed and cannot mutate", () => {
  const item = normalizeSourceInboxItem(sourceRecord({
    external_claims_verification: "verified",
    safety: {
      acknowledgement_is_fact_confirmation: true,
      formal_round_created: true,
      provider_calls_performed: 1,
      market_calls_performed: 1,
      execution_capability: "granted",
    },
  }));
  const permissions = sourceInboxItemPermissions(item, "room_one");

  assert.equal(item.valid, false);
  assert.ok(item.issues.includes("external_verification_marker_invalid"));
  assert.ok(item.issues.includes("provider_boundary_invalid"));
  assert.deepEqual(permissions, {
    actionable: false,
    attachment: null,
    roundDraft: null,
    canAcknowledge: false,
    canAttach: false,
    canDraft: false,
  });

  const coercedVersion = normalizeSourceInboxItem(sourceRecord({ state_version: "1" }));
  assert.equal(coercedVersion.valid, false);
  assert.ok(coercedVersion.issues.includes("state_version_invalid"));
  assert.equal(sourceInboxItemPermissions(coercedVersion, "room_one").canAcknowledge, false);

  const malformedId = normalizeSourceInboxItem(sourceRecord({ id: " source_item_one" }));
  assert.equal(malformedId.valid, false);
  assert.ok(malformedId.issues.includes("item_id_invalid"));
  assert.equal(sourceInboxItemPermissions(malformedId, "room_one").actionable, false);
});

test("acknowledge, attach and round-draft permissions remain explicit and ordered", () => {
  const awaiting = normalizeSourceInboxItem(sourceRecord());
  assert.equal(sourceInboxItemPermissions(awaiting, "").canAcknowledge, true);
  assert.equal(sourceInboxItemPermissions(awaiting, "room_one").canAttach, false);

  const acknowledged = normalizeSourceInboxItem(sourceRecord({ acknowledged: true }));
  assert.equal(sourceInboxItemPermissions(acknowledged, "").canAttach, false);
  assert.equal(sourceInboxItemPermissions(acknowledged, "room_one").canAttach, true);
  assert.equal(sourceInboxItemPermissions(acknowledged, "room_one").canDraft, false);

  const attached = normalizeSourceInboxItem(sourceRecord({
    state: "ATTACHED",
    state_version: 3,
    acknowledged: true,
    attachments: [{
      version: "source_inbox_attachment_v1",
      id: "attachment_one",
      room_id: "room_one",
      material_id: "material_one",
      material_version: 1,
      item_sha256: "b".repeat(64),
      attachment_sha256: "d".repeat(64),
      attached_at: 1_777_777_778_000,
    }],
  }));
  assert.equal(sourceInboxItemPermissions(attached, "room_one").canAttach, false);
  assert.equal(sourceInboxItemPermissions(attached, "room_one").canDraft, true);
  assert.equal(sourceInboxItemPermissions(attached, "room_two").canDraft, false);

  const drafted = normalizeSourceInboxItem(sourceRecord({
    state: "ROUND_DRAFTED",
    state_version: 4,
    acknowledged: true,
    attachments: [{
      version: "source_inbox_attachment_v1",
      id: "attachment_one",
      room_id: "room_one",
      material_id: "material_one",
      material_version: 1,
      item_sha256: "b".repeat(64),
      attachment_sha256: "d".repeat(64),
    }],
    round_drafts: [{
      version: "source_inbox_round_draft_v1",
      id: "draft_one",
      room_id: "room_one",
      draft_sha256: "e".repeat(64),
      formal_round_created: false,
      provider_calls_performed: 0,
      market_calls_performed: 0,
      execution_capability: "none",
      user_confirmation_required_to_launch: true,
    }],
  }));
  assert.equal(sourceInboxItemPermissions(drafted, "room_one").canDraft, false);
});

test("list normalization and replacement keep bounded current records", () => {
  const first = sourceRecord();
  const response = normalizeSourceInboxResponse({
    source_inbox: {
      version: "source_inbox_list_v1",
      items: [first],
      counts: { AWAITING_USER: 4 },
      total_count: 4,
      unread_count: 3,
      matched_count: 1,
      source_facets: [{
        source: "chatgpt_scheduled_task:github_ci_watch",
        source_channel: "chatgpt_scheduled_task",
        source_key: "github_ci_watch",
        source_tier: "external_manual",
        count: 4,
        unread_count: 3,
      }],
      query: "CI",
      state: "AWAITING_USER",
      source: "",
      unread: "",
      limit: 100,
    },
  });
  assert.equal(response.valid, true);
  assert.equal(response.items.length, 1);
  assert.equal(response.items[0].valid, true);
  assert.deepEqual(response.counts, { AWAITING_USER: 4 });
  assert.equal(response.totalCount, 4);
  assert.equal(response.unreadCount, 3);
  assert.equal(response.matchedCount, 1);
  assert.equal(response.sourceFacets[0].valid, true);

  const incomplete = normalizeSourceInboxResponse({
    source_inbox: {
      version: "source_inbox_list_v1",
      items: null,
      counts: {},
      total_count: 0,
      unread_count: 0,
      matched_count: 0,
      source_facets: null,
      query: "",
      state: "",
      source: "",
      unread: "",
      limit: 100,
    },
  });
  assert.equal(incomplete.valid, false);
  assert.ok(incomplete.issues.includes("inbox_structure_invalid"));

  const next = normalizeSourceInboxItem(sourceRecord({
    state_version: 2,
    acknowledged: true,
  }));
  const replaced = replaceSourceInboxItem(response.items, next);
  assert.equal(replaced.length, 1);
  assert.equal(replaced[0].stateVersion, 2);
  assert.equal(replaced[0].acknowledged, true);
});

test("monitoring health remains textual, default-off, and nonexecuting", () => {
  const healthPayload = {
    source_monitoring_health: {
      version: "source_monitoring_health_service_v2",
      health_projection_version: "source_monitoring_health_v1",
      runtime_liveness_verified: false,
      settings: {
        enabled: false,
        auto_start: false,
        official_only: true,
        allow_readonly_market: false,
        trading_impact_rules_enabled: false,
        dry_run: true,
        max_items_per_run: 50,
        initial_mode: "seed_only",
        catch_up_max_items: 0,
        initial_preview_sha256: "",
        from_time: "",
      },
      runtime: {
        version: "source_monitoring_runtime_health_v1",
        status: "disabled",
        runtime_id: "",
        started_at: 0,
        heartbeat_at: 0,
        last_loop_at: 0,
        active_adapter: "",
        next_due_at: 0,
        thread_alive: false,
        last_fatal_error_code: "",
        heartbeat_age_ms: 0,
        stall_after_ms: 120000,
        liveness_verified: false,
        enabled: false,
        auto_start: false,
        dry_run: true,
        execution_capability: "none",
        live_trading_allowed: false,
      },
      captured_at_ms: 1_777_777_777_000,
      state: "disabled",
      adapter_count: 1,
      counts: { disabled: 1, idle: 0, running: 0, healthy: 0, degraded: 0, backing_off: 0, failed: 0 },
      adapters: [{
        version: "source_adapter_health_v1",
        adapter_key: "sec_filings",
        catalog_registered: true,
        persisted_state: false,
        persisted_enabled: false,
        config_status: "absent",
        persisted_config_version: "",
        runtime_liveness_verified: false,
        metadata: {
          contract_version: "source_adapter_contract_v1",
          config_version: "sec_filings_config_v1",
          poll_interval_ms: 900000,
          max_candidates_per_poll: 50,
          source_class: "official_source",
          source_channel: "official_source_monitor",
          official_source: true,
          max_market_calls_per_poll: 0,
          execution_capability: "none",
          live_trading_allowed: false,
        },
        latest_run: null,
        state: "disabled",
        enabled: false,
        running: false,
        last_checked_at_ms: 0,
        last_success_at_ms: 0,
        last_event_at_ms: 0,
        next_due_at_ms: 0,
        consecutive_failures: 0,
        discovery_delay_ms: 0,
        last_error_code: "",
        execution_capability: "none",
        live_trading_allowed: false,
      }],
      persistence_available: true,
      safety: {
        database_writes_performed: 0,
        provider_calls_performed: 0,
        network_requests_performed: 0,
        market_calls_performed: 0,
        formal_rounds_created: 0,
        execution_capability: "none",
        live_trading_allowed: false,
      },
    },
  };
  const normalized = normalizeSourceMonitoringHealth(healthPayload);
  assert.equal(normalized.valid, true);
  assert.equal(normalized.stateLabel, "已停用");
  assert.equal(normalized.globalEnabled, false);
  assert.equal(normalized.adapters[0].persistedStatePresent, false);
  assert.equal(normalized.runtime.status, "disabled");
  assert.equal(normalized.runtime.statusLabel, "已停用");
  assert.equal(normalized.initialMode, "seed_only");

  const legacyServicePayload = structuredClone(healthPayload);
  legacyServicePayload.source_monitoring_health.version = "source_monitoring_health_service_v1";
  const legacyService = normalizeSourceMonitoringHealth(legacyServicePayload);
  assert.equal(legacyService.valid, false);
  assert.ok(legacyService.issues.includes("health_view_version_invalid"));

  const catchUpPayload = structuredClone(healthPayload);
  Object.assign(catchUpPayload.source_monitoring_health.settings, {
    initial_mode: "catch_up",
    catch_up_max_items: 10,
    initial_preview_sha256: "2".repeat(64),
  });
  assert.equal(normalizeSourceMonitoringHealth(catchUpPayload).valid, true);

  const fromTimePayload = structuredClone(healthPayload);
  Object.assign(fromTimePayload.source_monitoring_health.settings, {
    initial_mode: "from_time",
    from_time: "2026-08-28T12:55:00.123Z",
  });
  assert.equal(normalizeSourceMonitoringHealth(fromTimePayload).valid, true);

  const noncanonicalFromTimePayload = structuredClone(fromTimePayload);
  noncanonicalFromTimePayload.source_monitoring_health.settings.from_time = "2026-08-28T12:55:00.000Z";
  const noncanonicalFromTime = normalizeSourceMonitoringHealth(noncanonicalFromTimePayload);
  assert.equal(noncanonicalFromTime.valid, false);
  assert.ok(noncanonicalFromTime.issues.includes("health_settings_invalid"));

  const conflictingInitialModePayload = structuredClone(catchUpPayload);
  conflictingInitialModePayload.source_monitoring_health.settings.from_time = "2026-08-28T12:55:00Z";
  const conflictingInitialMode = normalizeSourceMonitoringHealth(conflictingInitialModePayload);
  assert.equal(conflictingInitialMode.valid, false);
  assert.ok(conflictingInitialMode.issues.includes("health_settings_invalid"));

  const extraRuntimeFieldPayload = structuredClone(healthPayload);
  extraRuntimeFieldPayload.source_monitoring_health.runtime.pid = 42;
  const extraRuntimeField = normalizeSourceMonitoringHealth(extraRuntimeFieldPayload);
  assert.equal(extraRuntimeField.valid, false);
  assert.ok(extraRuntimeField.issues.includes("health_runtime_invalid"));

  const unsafe = normalizeSourceMonitoringHealth({
    source_monitoring_health: {
      version: "source_monitoring_health_service_v2",
      health_projection_version: "source_monitoring_health_v1",
      runtime_liveness_verified: true,
      settings: {},
      state: "healthy",
      adapters: [],
      safety: { execution_capability: "orders", live_trading_allowed: true },
    },
  });
  assert.equal(unsafe.valid, false);

  const incomplete = normalizeSourceMonitoringHealth({
    source_monitoring_health: {
      version: "source_monitoring_health_service_v2",
      health_projection_version: "source_monitoring_health_v1",
      runtime_liveness_verified: false,
      state: "healthy",
      safety: {
        database_writes_performed: 0,
        provider_calls_performed: 0,
        network_requests_performed: 0,
        market_calls_performed: 0,
        formal_rounds_created: 0,
        execution_capability: "none",
        live_trading_allowed: false,
      },
    },
  });
  assert.equal(incomplete.valid, false);
  assert.ok(incomplete.issues.includes("health_structure_invalid"));
  assert.ok(incomplete.issues.includes("health_settings_invalid"));

  const forgedAggregatePayload = structuredClone(healthPayload);
  forgedAggregatePayload.source_monitoring_health.state = "healthy";
  forgedAggregatePayload.source_monitoring_health.counts = {
    disabled: 0,
    idle: 0,
    running: 0,
    healthy: 1,
    degraded: 0,
    backing_off: 0,
    failed: 0,
  };
  const forgedAggregate = normalizeSourceMonitoringHealth(forgedAggregatePayload);
  assert.equal(forgedAggregate.valid, false);
  assert.ok(forgedAggregate.issues.includes("health_state_accounting_invalid"));
  assert.ok(forgedAggregate.issues.includes("health_overall_state_invalid"));

  const runningPayload = structuredClone(healthPayload);
  Object.assign(runningPayload.source_monitoring_health.settings, {
    enabled: true,
    auto_start: true,
  });
  Object.assign(runningPayload.source_monitoring_health.runtime, {
    status: "running",
    runtime_id: `source_monitor_runtime_${"1".repeat(32)}`,
    started_at: 1_777_777_776_000,
    heartbeat_at: 1_777_777_776_900,
    last_loop_at: 1_777_777_776_900,
    active_adapter: "sec_filings",
    next_due_at: 1_777_777_780_000,
    thread_alive: true,
    heartbeat_age_ms: 100,
    liveness_verified: true,
    enabled: true,
    auto_start: true,
  });
  runningPayload.source_monitoring_health.runtime_liveness_verified = true;
  Object.assign(runningPayload.source_monitoring_health.adapters[0], {
    persisted_state: true,
    persisted_enabled: true,
    config_status: "current",
    persisted_config_version: "sec_filings_config_v1",
    state: "running",
    enabled: true,
    running: true,
    runtime_liveness_verified: true,
  });
  runningPayload.source_monitoring_health.state = "running";
  runningPayload.source_monitoring_health.counts = {
    disabled: 0,
    idle: 0,
    running: 1,
    healthy: 0,
    degraded: 0,
    backing_off: 0,
    failed: 0,
  };
  const running = normalizeSourceMonitoringHealth(runningPayload);
  assert.equal(running.valid, true);
  assert.equal(running.runtime.livenessVerified, true);
  assert.equal(running.adapters[0].running, true);

  const forgedLivenessPayload = structuredClone(runningPayload);
  forgedLivenessPayload.source_monitoring_health.runtime.thread_alive = false;
  const forgedLiveness = normalizeSourceMonitoringHealth(forgedLivenessPayload);
  assert.equal(forgedLiveness.valid, false);
  assert.ok(forgedLiveness.issues.includes("health_runtime_invalid"));

  const forgedAdapterPayload = structuredClone(runningPayload);
  forgedAdapterPayload.source_monitoring_health.runtime.active_adapter = "";
  const forgedAdapter = normalizeSourceMonitoringHealth(forgedAdapterPayload);
  assert.equal(forgedAdapter.valid, false);
  assert.ok(forgedAdapter.issues.includes("health_adapter_runtime_invalid_0"));

  const stalledPayload = structuredClone(runningPayload);
  Object.assign(stalledPayload.source_monitoring_health.runtime, {
    status: "stalled",
    heartbeat_age_ms: 120001,
    liveness_verified: false,
  });
  stalledPayload.source_monitoring_health.runtime_liveness_verified = false;
  Object.assign(stalledPayload.source_monitoring_health.adapters[0], {
    state: "healthy",
    running: false,
    runtime_liveness_verified: false,
  });
  stalledPayload.source_monitoring_health.state = "healthy";
  stalledPayload.source_monitoring_health.counts = {
    disabled: 0,
    idle: 0,
    running: 0,
    healthy: 1,
    degraded: 0,
    backing_off: 0,
    failed: 0,
  };
  const stalled = normalizeSourceMonitoringHealth(stalledPayload);
  assert.equal(stalled.valid, true);
  assert.equal(stalled.runtime.status, "stalled");
  assert.equal(stalled.runtime.livenessVerified, false);

  const failedPayload = structuredClone(stalledPayload);
  Object.assign(failedPayload.source_monitoring_health.runtime, {
    status: "failed",
    active_adapter: "",
    thread_alive: false,
    heartbeat_age_ms: 100,
    last_fatal_error_code: "SOURCE_MONITORING_RUNTIME_FATAL",
  });
  const failed = normalizeSourceMonitoringHealth(failedPayload);
  assert.equal(failed.valid, true);
  assert.equal(failed.runtime.status, "failed");
  assert.equal(failed.runtime.statusLabel, "运行失败");
});

test("notification feed accepts only cursor-bound unacknowledged zero-authority events", () => {
  const feed = normalizeSourceInboxNotificationFeed({
    source_notifications: {
      version: "source_inbox_notification_feed_v1",
      baseline: false,
      notifications: [{
        version: "source_inbox_notification_v1",
        id: "source_item_one",
        created_at: 1_777_777_777_000,
        source_channel: "official_source_monitor",
        source_key: "sec_filings",
        source_tier: "official_source",
        item_type: "official_filing",
        severity: "high",
        occurred_at: "2026-08-28T12:55:00Z",
        headline: "External filing event",
        acknowledged: false,
        external_claims_verification: EXTERNAL_UNVERIFIED,
        safety: {
          fact_confirmation: false,
          approval: false,
          execution_authorization: false,
        },
      }],
      cursor: "opaque-head",
      head_cursor: "opaque-head",
      unread_count: 1,
      has_more: false,
      limit: 50,
      safety: {
        external_claims_verification: EXTERNAL_UNVERIFIED,
        execution_capability: "none",
        live_trading_allowed: false,
        provider_calls_performed: 0,
        market_calls_performed: 0,
        formal_rounds_created: 0,
      },
    },
  }, { requestedCursor: "opaque-prior" });
  assert.equal(feed.valid, true);
  assert.equal(feed.notifications[0].eventId, "source_item_one");

  const tampered = structuredClone({
    source_notifications: {
      version: "source_inbox_notification_feed_v1",
      baseline: false,
      notifications: [],
      cursor: "opaque-next",
      head_cursor: "opaque-head",
      unread_count: 0,
      has_more: false,
      limit: 50,
      safety: {
        external_claims_verification: EXTERNAL_UNVERIFIED,
        execution_capability: "trade",
        live_trading_allowed: true,
        provider_calls_performed: 1,
        market_calls_performed: 1,
        formal_rounds_created: 1,
      },
    },
  });
  assert.equal(normalizeSourceInboxNotificationFeed(tampered).valid, false);

  const incomplete = structuredClone(tampered);
  incomplete.source_notifications.safety = {
    external_claims_verification: EXTERNAL_UNVERIFIED,
    execution_capability: "none",
    live_trading_allowed: false,
    provider_calls_performed: 0,
    market_calls_performed: 0,
    formal_rounds_created: 0,
  };
  incomplete.source_notifications.cursor = "opaque-head";
  delete incomplete.source_notifications.notifications;
  delete incomplete.source_notifications.has_more;
  const incompleteFeed = normalizeSourceInboxNotificationFeed(incomplete);
  assert.equal(incompleteFeed.valid, false);
  assert.ok(incompleteFeed.issues.includes("notification_structure_invalid"));

  const undercounted = normalizeSourceInboxNotificationFeed({
    source_notifications: {
      version: "source_inbox_notification_feed_v1",
      baseline: false,
      notifications: [{
        version: "source_inbox_notification_v1",
        id: "source_item_one",
        created_at: 1_777_777_777_000,
        source_channel: "official_source_monitor",
        source_key: "sec_filings",
        source_tier: "official_source",
        item_type: "official_filing",
        severity: "high",
        occurred_at: "2026-08-28T12:55:00Z",
        headline: "External filing event",
        acknowledged: false,
        external_claims_verification: EXTERNAL_UNVERIFIED,
        safety: { fact_confirmation: false, approval: false, execution_authorization: false },
      }],
      cursor: "opaque-prior",
      head_cursor: "opaque-prior",
      unread_count: 0,
      has_more: false,
      limit: 50,
      safety: {
        external_claims_verification: EXTERNAL_UNVERIFIED,
        execution_capability: "none",
        live_trading_allowed: false,
        provider_calls_performed: 0,
        market_calls_performed: 0,
        formal_rounds_created: 0,
      },
    },
  }, { requestedCursor: "opaque-prior" });
  assert.equal(undercounted.valid, false);
  assert.ok(undercounted.issues.includes("notification_accounting_invalid"));
  assert.ok(undercounted.issues.includes("notification_cursor_semantics_invalid"));
});

test("manual import preview is strict, no-store, and preserves every item summary", () => {
  const preview = normalizeSourceImportPreview(sourceImportPreview());
  assert.equal(preview.valid, true, preview.issues.join(","));
  assert.equal(preview.sourceChannel, "chatgpt_manual");
  assert.equal(preview.externalRunId, "run one with spaces");
  assert.equal(preview.itemCount, 1);
  assert.equal(preview.sourceCount, 1);
  assert.equal(preview.items[0].headline, "CI 运行结果摘要");
  assert.equal(preview.items[0].sources[0].url, "https://github.com/acme/project/actions/runs/100");

  const storeClaim = structuredClone(sourceImportPreview());
  storeClaim.source_import_preview.store_disposition.evaluated = true;
  assert.equal(normalizeSourceImportPreview(storeClaim).valid, false);

  const reserved = structuredClone(sourceImportPreview());
  reserved.source_import_preview.packet.source_channel = "official_source_monitor";
  reserved.source_import_preview.packet.generation.channel = "official_source_monitor";
  assert.equal(normalizeSourceImportPreview(reserved).valid, false);

  const extra = structuredClone(sourceImportPreview());
  extra.source_import_preview.unexpected = true;
  assert.equal(normalizeSourceImportPreview(extra).valid, false);
});

test("manual import bytes and prompt template fail closed without browser automation", () => {
  assert.equal(sourceImportUtf8Bytes("猫"), 3);
  assert.ok(sourceImportUtf8Bytes("猫".repeat(87_382)) > SOURCE_IMPORT_MAX_BYTES);

  const template = normalizeSourceMonitoringPromptTemplate(promptTemplateResponse());
  assert.equal(template.valid, true);
  assert.match(template.prompt, /source_import_packet_v1/);

  const automated = promptTemplateResponse({
    prompt: "{{monitoring_scope}} source_import_packet_v1 external_unverified window.open('https://chatgpt.com')",
  });
  assert.equal(normalizeSourceMonitoringPromptTemplate(automated).valid, false);
});

test("manual import result validates every item, identity, receipt, and accounting", () => {
  const first = sourceRecord();
  const second = sourceRecord({
    id: "source_item_two",
    server_fingerprint: "5".repeat(64),
    item_sha256: "6".repeat(64),
  });
  const valid = normalizeSourceImportResult(sourceImportResult([first, second]));
  assert.equal(valid.valid, true);
  assert.deepEqual(valid.items.map((item) => item.id), ["source_item_one", "source_item_two"]);

  const invalidSecond = structuredClone(sourceImportResult([first, second]));
  invalidSecond.source_import.items[1].safety.execution_capability = "trade";
  assert.equal(normalizeSourceImportResult(invalidSecond).valid, false);

  const duplicateIdentity = structuredClone(sourceImportResult([first, second]));
  duplicateIdentity.source_import.items[1].id = "source_item_one";
  assert.equal(normalizeSourceImportResult(duplicateIdentity).valid, false);
});

test("panel source and responsive styles preserve the zero-execution boundary", () => {
  const calledApiMethods = [...panelSource.matchAll(/api\.([A-Za-z0-9_]+)/g)]
    .map((match) => match[1])
    .sort();
  assert.deepEqual(calledApiMethods, [
    "acknowledgeSourceInboxItem",
    "attachSourceInboxItem",
    "createSourceInboxRoundDraft",
    "importSourceInbox",
    "listSourceInbox",
    "previewSourceInboxImport",
    "previewSourceMonitoringAdapterInitialization",
    "setSourceMonitoringAdapterEnablement",
    "sourceInboxItem",
    "sourceMonitoringHealth",
    "sourceMonitoringOperatorControl",
    "sourceMonitoringPromptTemplate",
  ]);
  assert.doesNotMatch(panelSource, /streamRound|streamMessage|preflightProviders|storageSnapshot/);
  assert.match(panelSource, /已阅，不代表事实确认/);
  assert.match(panelSource, /草稿不启动 Provider，不创建正式 round，不读取市场/);
  assert.match(panelSource, /external_unverified/);
  assert.match(panelSource, /不是方向预测、因果结论、盈利声明或执行授权/);
  assert.match(panelSource, /只在你明确启用后申请权限/);
  assert.match(panelSource, /内容已更改，请重新预览/);
  assert.match(panelSource, /确认仅导入收件箱/);
  assert.match(panelSource, /本页不打开、登录或控制 ChatGPT/);
  assert.match(panelSource, /保存 Adapter 启用状态不证明 Runtime 在线/);
  assert.match(panelSource, /不会删除 checkpoint、初始化收据或 Source Inbox 记录/);
  assert.match(panelSource, /ENABLE_SOURCE_MONITORING_ADAPTER/);
  assert.match(panelSource, /DISABLE_SOURCE_MONITORING_ADAPTER/);
  assert.doesNotMatch(panelSource, /window\.open|clipboard\.readText|chatgpt\.com/);
  const selectionBlock = panelSource.slice(
    panelSource.indexOf("setSelectedItemId((current) =>"),
    panelSource.indexOf("const loadHealth"),
  );
  assert.ok(selectionBlock.indexOf("if (requestedItemId)") < selectionBlock.indexOf("if (current &&"));
  assert.match(panelSource, /if \(item\.id !== itemId\)/);
  assert.match(panelSource, /adoptItem\(payload\.source_item \|\| payload\.item, targetItemId\)/);
  assert.match(panelSource, /if \(!item\.valid \|\| !item\.id/);
  assert.match(panelSource, /if \(open && requestedItemId\) setSelectedItemId\(requestedItemId\)/);
  assert.match(panelSource, /invalidProjectionCount > 0\s*\? \[\]/);
  assert.match(panelSource, /\{health\?\.valid \? \(/);
  assert.match(panelStyles, /@media \(max-width: 760px\)[\s\S]*\.source-inbox-panel\s*\{\s*width:\s*var\(--visual-viewport-width, 100vw\)/);
  assert.match(panelStyles, /@media \(max-width: 620px\)[\s\S]*\.source-inbox-workspace\s*\{\s*display:\s*block;/);
  assert.match(panelStyles, /@media \(max-width: 620px\)[\s\S]*\.source-inbox-import > footer button[\s\S]*min-height:\s*44px/);
  assert.match(panelStyles, /@media \(forced-colors: active\)/);
  assert.match(panelStyles, /@media \(prefers-reduced-motion: reduce\)/);
});
