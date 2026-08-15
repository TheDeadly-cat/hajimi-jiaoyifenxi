import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  discussionAuditViewModel,
  emptyDiscussionAuditState,
  normalizeDiscussionAudit,
} from "../src/discussionAudit.js";

const TRACE_HASH = "a".repeat(64);

function auditFixture(overrides = {}) {
  return {
    version: "discussion_audit_v1",
    audit_hash: "b".repeat(64),
    room_id: "room_demo",
    round_id: "round_demo",
    source: {
      trace_version: "round_execution_trace_v1",
      trace_hash: TRACE_HASH,
      trace_integrity_status: "verified",
      trace_integrity_issue_codes: [],
      turn_contract_applicable: true,
      turn_contract_valid: true,
      turn_contract_version: "round_turn_contract_v1",
    },
    coverage: {
      history_mode: "current_envelope",
      status: "complete",
      limitation_codes: [],
    },
    structural: {
      dynamic_status: "verified",
      selection_count: 2,
      dynamic_selection_count: 2,
      fallback_count: 1,
      selections: [
        {
          sequence_no: 1,
          event_id: "event_one",
          director_decision_id: "decision_one",
          action: "speak",
          selected_member_id: "fundamental_analyst",
          source: "moderator_model",
          decision_authority: "moderator_model",
          discussion_mode: "dynamic",
          moderator_model_call_recorded: true,
          fallback: false,
          structural_status: "verified",
          scheduling_snapshot: {
            recorded: true,
            eligible_member_count: 4,
            gap_count: 2,
            selected_member_eligible: true,
            selected_gap_codes: ["fundamentals"],
          },
        },
        {
          sequence_no: 2,
          event_id: "event_two",
          director_decision_id: "decision_two",
          action: "speak",
          selected_member_id: "risk_reviewer",
          source: "safe_fallback",
          decision_authority: "safety_fallback",
          discussion_mode: "dynamic",
          moderator_model_call_recorded: false,
          fallback: true,
          structural_status: "verified",
          scheduling_snapshot: {
            recorded: true,
            eligible_member_count: 3,
            gap_count: 1,
            selected_member_eligible: true,
            selected_gap_codes: ["risk"],
          },
        },
      ],
      response_edge_count: 1,
      response_edges: [{
        from_message_id: "message_two",
        to_message_id: "message_one",
        target_within_formal_bundle: true,
        persisted_reply_target: true,
        structurally_verified: true,
        semantic_causality_status: "unknown",
      }],
    },
    candidate_checkpoint: {
      applicable: true,
      status: "ready",
      ready: true,
      candidate_count: 2,
      minimum_comparison_count: 2,
      comparison_count_satisfied: true,
      candidates: [
        {
          id: "candidate_long",
          revision: 2,
          origin_message_id: "message_one",
          latest_message_id: "message_two",
        },
        {
          id: "candidate_short",
          revision: 1,
          origin_message_id: "message_three",
          latest_message_id: "message_three",
        },
      ],
      lineage: {
        status: "ready",
        ready: true,
        blocker_codes: [],
        referenced_candidate_ids: ["candidate_long", "candidate_short"],
      },
      risk_review: {
        required: true,
        status: "ready",
        ready: true,
        target_candidate_count: 2,
        reviewed_candidate_count: 2,
        review_count: 3,
        blocker_codes: [],
      },
      decision: {
        status: "candidate",
        preferred_option_id: "candidate_long",
      },
    },
    semantic_causality: {
      status: "unknown",
      proven: false,
      reason_code: "EFFECTIVE_MODEL_INPUT_ATTESTATION_UNAVAILABLE",
    },
    findings: [
      { code: "SEMANTIC_CAUSALITY_UNKNOWN", severity: "info", scope: "round" },
      { code: "FALLBACK_USED", severity: "warning", scope: "director", count: 1 },
    ],
    safety: {
      read_only: true,
      database_writes_performed: 0,
      provider_calls_performed: 0,
      market_data_calls_performed: 0,
      execution_capability: "none",
      live_trading_allowed: false,
      can_autonomously_decide: false,
      raw_content_included: false,
    },
    ...overrides,
  };
}

test("discussion audit keeps dynamic structure, fallback, semantic unknown, edges and checkpoint distinct", () => {
  const audit = normalizeDiscussionAudit(auditFixture(), { expectedTraceHash: TRACE_HASH });
  assert.equal(audit.valid, true);
  assert.equal(audit.structural.dynamic_status, "verified");
  assert.equal(audit.structural.selections[0].fallback, false);
  assert.equal(audit.structural.selections[1].fallback, true);
  assert.equal(audit.semantic_causality.status, "unknown");
  assert.equal(audit.semantic_causality.proven, false);

  const view = discussionAuditViewModel(audit, { expectedTraceHash: TRACE_HASH });
  assert.equal(view.dynamic.label, "动态结构已核验");
  assert.equal(view.dynamic.fallbackCount, 1);
  assert.equal(view.hasFallback, true);
  assert.equal(view.selections[0].sourceLabel, "主持模型");
  assert.equal(view.selections[1].sourceLabel, "安全回退");
  assert.equal(view.semantic.status, "unknown");
  assert.equal(view.responseEdges[0].relationLabel, "持久化回复目标");
  assert.equal(view.responseEdges[0].scopeLabel, "本轮正式消息");
  assert.equal(view.checkpoint.label, "检查点已满足");
  assert.equal(view.checkpoint.countLabel, "2 / 2");
  assert.equal(view.checkpoint.decisionLabel, "条件化首选 candidate_long");
});

test("discussion audit fails closed when its source trace hash drifts from the displayed trace", () => {
  const audit = normalizeDiscussionAudit(auditFixture(), { expectedTraceHash: "c".repeat(64) });
  assert.equal(audit.valid, false);
  assert.ok(audit.errors.includes("讨论审计与当前执行轨迹的冻结快照不一致。"));

  const view = discussionAuditViewModel(auditFixture(), { expectedTraceHash: "d".repeat(64) });
  assert.equal(view.valid, false);
  assert.ok(view.errors.some((message) => message.includes("冻结快照不一致")));
});

test("unknown remains an epistemic boundary and is never treated as fallback", () => {
  const fixture = auditFixture({
    structural: {
      dynamic_status: "legacy_unknown",
      selection_count: 0,
      dynamic_selection_count: 0,
      fallback_count: 0,
      selections: [],
      response_edge_count: 0,
      response_edges: [],
    },
    candidate_checkpoint: {
      applicable: false,
      status: "not_applicable",
      ready: false,
      candidate_count: 0,
      minimum_comparison_count: 2,
      comparison_count_satisfied: false,
      candidates: [],
      lineage: { status: "not_recorded", ready: false, blocker_codes: [], referenced_candidate_ids: [] },
      risk_review: {
        required: false,
        status: "not_recorded",
        ready: false,
        target_candidate_count: 0,
        reviewed_candidate_count: 0,
        review_count: 0,
        blocker_codes: [],
      },
      decision: { status: "undecided", preferred_option_id: "" },
    },
  });
  const view = discussionAuditViewModel(fixture, { expectedTraceHash: TRACE_HASH });
  assert.equal(view.valid, true);
  assert.equal(view.dynamic.status, "legacy_unknown");
  assert.equal(view.dynamic.tone, "unknown");
  assert.equal(view.hasFallback, false);
  assert.equal(view.semantic.status, "unknown");
  assert.equal(view.checkpoint.label, "本轮不适用");
});

test("discussion audit rejects semantic overclaiming and unsafe read responses", () => {
  const semanticOverclaim = auditFixture({
    semantic_causality: {
      status: "verified",
      proven: true,
      reason_code: "MODEL_READ_MESSAGE",
    },
  });
  const unsafe = auditFixture({
    safety: {
      ...auditFixture().safety,
      provider_calls_performed: 1,
    },
  });
  assert.equal(normalizeDiscussionAudit(semanticOverclaim).valid, false);
  assert.ok(normalizeDiscussionAudit(semanticOverclaim).errors.includes("语义因果关系边界无法验证。"));
  assert.equal(normalizeDiscussionAudit(unsafe).valid, false);
  assert.ok(normalizeDiscussionAudit(unsafe).errors.includes("讨论审计的只读安全边界无法验证。"));
});

test("discussion audit state and request path stay independent from execution trace state", () => {
  assert.deepEqual(emptyDiscussionAuditState(), {
    roomId: "",
    roundId: "",
    audit: null,
    loading: false,
    error: "",
    stale: false,
  });

  const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
  const dialogSource = readFileSync(
    new URL("../src/components/RoundExecutionTraceDialog.jsx", import.meta.url),
    "utf8",
  );
  const auditSectionSource = readFileSync(
    new URL("../src/components/DiscussionAuditSection.jsx", import.meta.url),
    "utf8",
  );
  const styles = readFileSync(
    new URL("../src/styles/round-execution-trace.css", import.meta.url),
    "utf8",
  );
  const openHandler = appSource.slice(
    appSource.indexOf("const openRoundExecutionTrace"),
    appSource.indexOf("const retryRoundExecutionTrace"),
  );

  assert.match(appSource, /discussionAuditRequestRef = useRef/);
  assert.match(openHandler, /void loadRoundExecutionTrace/);
  assert.match(openHandler, /void loadDiscussionAudit/);
  assert.doesNotMatch(openHandler, /Promise\.all/);
  assert.match(dialogSource, /expectedTraceHash=\{trace\.trace_hash\}/);
  assert.match(
    dialogSource,
    /event\.key === "Escape"[\s\S]*event\.preventDefault\(\);[\s\S]*event\.stopPropagation\(\);[\s\S]*onClose\?\.\(\)/,
  );
  assert.match(auditSectionSource, /执行轨迹仍可独立查看/);
  assert.match(styles, /@media \(max-width: 760px\)[\s\S]*\.discussion-audit-core-grid \{ grid-template-columns: 1fr; \}/);
  assert.match(styles, /@media \(max-width: 620px\)[\s\S]*\.discussion-audit-candidates \{ grid-template-columns: 1fr; \}/);
});
