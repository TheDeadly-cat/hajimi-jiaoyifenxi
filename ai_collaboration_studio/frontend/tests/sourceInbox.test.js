import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  EXTERNAL_UNVERIFIED,
  normalizeSourceInboxItem,
  normalizeSourceInboxResponse,
  replaceSourceInboxItem,
  sourceInboxItemPermissions,
} from "../src/sourceInbox.js";

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
      items: [first],
      counts: { AWAITING_USER: 1 },
      query: "CI",
      state: "AWAITING_USER",
    },
  });
  assert.equal(response.items.length, 1);
  assert.equal(response.items[0].valid, true);
  assert.deepEqual(response.counts, { AWAITING_USER: 1 });

  const next = normalizeSourceInboxItem(sourceRecord({
    state_version: 2,
    acknowledged: true,
  }));
  const replaced = replaceSourceInboxItem(response.items, next);
  assert.equal(replaced.length, 1);
  assert.equal(replaced[0].stateVersion, 2);
  assert.equal(replaced[0].acknowledged, true);
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
    "sourceInboxItem",
  ]);
  assert.doesNotMatch(panelSource, /streamRound|streamMessage|preflightProviders|storageSnapshot/);
  assert.match(panelSource, /已阅，不代表事实确认/);
  assert.match(panelSource, /草稿不启动 Provider，不创建正式 round，不读取市场/);
  assert.match(panelSource, /external_unverified/);
  assert.match(panelStyles, /@media \(max-width: 760px\)[\s\S]*\.source-inbox-panel\s*\{\s*width:\s*var\(--visual-viewport-width, 100vw\)/);
  assert.match(panelStyles, /@media \(max-width: 620px\)[\s\S]*\.source-inbox-workspace\s*\{\s*display:\s*block;/);
  assert.match(panelStyles, /@media \(forced-colors: active\)/);
  assert.match(panelStyles, /@media \(prefers-reduced-motion: reduce\)/);
});
