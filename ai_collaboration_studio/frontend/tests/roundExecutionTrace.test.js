import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  candidateProjectionViewModel,
  mergeRoundExecutionTracePages,
  normalizeRoundExecutionTrace,
  roundExecutionDirectorBudget,
  roundExecutionEventMeta,
  roundExecutionStatusMeta,
  roundExecutionTraceAnchorState,
  roundExecutionTraceSummaryText,
} from "../src/roundExecutionTrace.js";
import {
  roundTraceDialogProjection,
  roundTraceDisplayText,
  roundTraceErrorMessage,
  roundTraceEventWindow,
} from "../src/roundExecutionTraceDialogUi.js";

const TRACE_HASH = "a".repeat(64);

function traceFixture(overrides = {}) {
  return {
    version: "round_execution_trace_v1",
    trace_hash: TRACE_HASH,
    room_id: "room_1",
    round_id: "round_1",
    round: { status: "COMPLETED", objective: "比较存储股候选" },
    history: { mode: "persisted", coverage: "full", limitations: [] },
    integrity: {
      status: "verified",
      ok: true,
      issues: [],
      round_ledger_verified: true,
      provider_ledger_verified: null,
      trace_snapshot_sha256: "b".repeat(64),
      snapshot_hash_persisted: false,
    },
    summary: {
      event_count: 3,
      anomaly_count: 1,
      provider_calls: { reserved: 2, completed: 2, max: 28, status: "within_limit" },
      director_attempt_count: 1,
      director_decision_count: 1,
      formal_turn_count: 1,
      candidate_update_count: 0,
      risk_review_count: 0,
      risk_count: 0,
      artifact_count: 0,
      user_decision_count: 0,
    },
    events: [
      {
        ordinal: 2,
        event_id: "event_2",
        type: "future_event_type",
        occurred_at: 2,
        finished_at: 3,
        source: { table: "round_events", id: "source_2", sequence_no: 2 },
        actor: { kind: "service", id: "service_1", name: "调度器" },
        status: "future_status",
        refs: { round_id: "round_1" },
        payload: { summary: "服务端新增事件仍可安全展示", nested: { allowed: true } },
        integrity: { status: "verified", issues: [] },
      },
      {
        ordinal: 1,
        event_id: "event_1",
        type: "round_started",
        occurred_at: 1,
        finished_at: 1,
        source: { table: "rounds", id: "round_1", sequence_no: 1 },
        actor: { kind: "user", id: "user", name: "用户" },
        status: "completed",
        refs: {},
        payload: { objective: "比较存储股候选" },
        integrity: { status: "verified", issues: [] },
      },
      {
        ordinal: 2,
        event_id: "event_2",
        type: "future_event_type",
        occurred_at: 2,
        source: {},
        actor: {},
        status: "future_status",
        refs: {},
        payload: {},
        integrity: {},
      },
    ],
    candidate_projection: null,
    page: { limit: 200, cursor: 0, next_cursor: 2, has_more: true, total: 3 },
    sorting: { primary: "ordinal" },
    safety: {
      read_only: true,
      provider_calls_performed: 0,
      execution_capability: "none",
      live_trading_allowed: false,
      can_autonomously_decide: false,
    },
    ...overrides,
  };
}

function candidateProjectionFixture(overrides = {}) {
  return {
    version: "turn_contract_v1",
    qualified_message_count: 5,
    source_message_ids: ["message_a", "message_b", "message_risk", "message_decision"],
    provisional: false,
    authoritative: true,
    projection_sha256: "d".repeat(64),
    candidate_lineage: {
      version: "candidate_lineage_v1",
      applicable: true,
      ready: true,
      status: "ready",
      decision_message_id: "message_decision",
      referenced_candidate_ids: ["candidate_a", "candidate_b"],
      candidates: [
        {
          id: "candidate_a",
          revision: 2,
          origin_message_id: "message_a",
          latest_message_id: "message_a2",
        },
        {
          id: "candidate_b",
          revision: 1,
          origin_message_id: "message_b",
          latest_message_id: "message_b",
        },
      ],
      issues: [],
    },
    candidate_risk_reviews: {
      version: "candidate_risk_review_v1",
      applicable: true,
      ready: true,
      status: "ready",
      target_candidate_count: 2,
      reviewed_candidate_count: 2,
      current_review_count: 2,
      stale_review_count: 1,
      action_counts: { support: 1, challenge: 1, reject: 1 },
      review_actions_are_dispositions_only: true,
      execution_capability: "none",
      live_trading_allowed: false,
      can_autonomously_decide: false,
      reviews: [
        {
          candidate_id: "candidate_a",
          candidate_revision: 2,
          current_candidate_revision: 2,
          candidate_snapshot: {
            title: "方案 A",
            thesis: "以证据 A 为主线。",
            invalidation: "证据 A 被推翻。",
          },
          action: "support",
          status: "current",
          reviewer_name: "风险官",
          review_message_id: "message_risk_a",
        },
        {
          candidate_id: "candidate_a",
          candidate_revision: 1,
          current_candidate_revision: 2,
          candidate_snapshot: { title: "方案 A 旧版" },
          action: "challenge",
          status: "stale",
          reviewer_name: "风险官",
          review_message_id: "message_risk_a_old",
        },
        {
          candidate_id: "candidate_b",
          candidate_revision: 1,
          current_candidate_revision: 1,
          candidate_snapshot: { title: "方案 B" },
          action: "reject",
          status: "current",
          reviewer_name: "风险官",
          review_message_id: "message_risk_b",
        },
      ],
      issues: [],
    },
    decision: {
      status: "candidate",
      preferred_option_id: "candidate_b",
      rationale: "方案 B 在共同维度上更可逆。",
      evidence: [{ type: "message", id: "message_risk_b" }],
      options: [
        {
          id: "candidate_a",
          title: "方案 A",
          description: "以证据 A 为主线。",
          risks: ["证据 A 被推翻。"],
          evidence: [{ type: "message", id: "message_a" }],
          lineage: {
            revision: 2,
            origin_message_id: "message_a",
            latest_message_id: "message_a2",
          },
        },
        {
          id: "candidate_b",
          title: "方案 B",
          description: "以可逆路径 B 为主线。",
          risks: ["成本超过阈值。"],
          evidence: [{ type: "message", id: "message_b" }],
          lineage: {
            revision: 1,
            origin_message_id: "message_b",
            latest_message_id: "message_b",
          },
        },
      ],
    },
    execution_capability: "none",
    live_trading_allowed: false,
    can_autonomously_decide: false,
    ...overrides,
  };
}

test("execution trace normalization validates safety, orders events, and deduplicates ids", () => {
  const trace = normalizeRoundExecutionTrace(traceFixture());

  assert.equal(trace.valid, true);
  assert.equal(trace.events.length, 2);
  assert.deepEqual(trace.events.map((event) => event.event_id), ["event_1", "event_2"]);
  assert.equal(trace.safety.provider_calls_performed, 0);
  assert.equal(trace.safety.execution_capability, "none");
  assert.equal(trace.integrity.round_ledger_verified, true);
  assert.equal(trace.integrity.provider_ledger_verified, null);
  assert.equal(trace.integrity.trace_anchor_verified, null);
  assert.equal(trace.integrity.trace_anchor_sequence, 0);
  assert.equal(trace.page.has_more, true);
  assert.equal(trace.page.cursor, "0");
  assert.equal(trace.page.next_cursor, "2");
});

test("derives the frozen round_director hard sub-budget from persisted provider-run events", () => {
  const trace = normalizeRoundExecutionTrace(traceFixture({
    events: [{
      ordinal: 1,
      event_id: "provider_run_1",
      type: "provider_run_created",
      occurred_at: 1,
      source: { table: "provider_execution_runs", id: "run_1", sequence_no: 1 },
      actor: { kind: "service", id: "service_1", name: "调度器" },
      status: "completed",
      refs: {},
      payload: {
        kind_call_budgets: {
          round_director: { limit: 6, reserved: 2, remaining: 4 },
        },
      },
      integrity: { status: "verified", issues: [] },
    }],
  }));

  assert.deepEqual(roundExecutionDirectorBudget(trace), {
    kind: "round_director",
    recorded: true,
    valid: true,
    limit: 6,
    reserved: 2,
    remaining: 4,
    event_id: "provider_run_1",
  });

  const malformed = normalizeRoundExecutionTrace(traceFixture({
    events: [{
      ...trace.events[0],
      event_id: "provider_run_bad",
      payload: {
        kind_call_budgets: {
          round_director: { limit: 6, reserved: 2, remaining: 5 },
        },
      },
    }],
  }));
  assert.equal(roundExecutionDirectorBudget(malformed).recorded, true);
  assert.equal(roundExecutionDirectorBudget(malformed).valid, false);
});

test("maps trace anchors to persisted, changed, and pending states without inventing history", () => {
  const pending = normalizeRoundExecutionTrace(traceFixture());
  assert.deepEqual(
    { state: roundExecutionTraceAnchorState(pending).state, sequence: roundExecutionTraceAnchorState(pending).sequence },
    { state: "pending", sequence: 0 },
  );

  const changed = normalizeRoundExecutionTrace(traceFixture({
    integrity: {
      ...traceFixture().integrity,
      status: "partial",
      trace_anchor_verified: true,
      trace_anchor_sequence: 2,
      trace_anchor_sha256: "c".repeat(64),
      trace_anchor_version: "round_execution_trace_v1",
      snapshot_hash_persisted: false,
    },
  }));
  assert.deepEqual(
    { state: roundExecutionTraceAnchorState(changed).state, sequence: roundExecutionTraceAnchorState(changed).sequence },
    { state: "changed", sequence: 2 },
  );

  const persisted = normalizeRoundExecutionTrace(traceFixture({
    integrity: {
      ...changed.integrity,
      status: "verified",
      snapshot_hash_persisted: true,
    },
  }));
  const persistedState = roundExecutionTraceAnchorState(persisted);
  assert.equal(persistedState.state, "persisted");
  assert.equal(persistedState.sequence, 2);
  assert.equal(persistedState.anchor_sha256, "c".repeat(64));

  const contradictory = normalizeRoundExecutionTrace(traceFixture({
    integrity: {
      ...traceFixture().integrity,
      trace_anchor_verified: true,
      trace_anchor_sequence: 2,
      trace_anchor_sha256: "not-a-sha256",
      snapshot_hash_persisted: true,
    },
  }));
  assert.equal(roundExecutionTraceAnchorState(contradictory).state, "pending");
});

test("unknown event and status values remain explicit without pretending to understand them", () => {
  assert.deepEqual(roundExecutionEventMeta("future_event_type"), {
    label: "其他记录",
    group: "other",
  });
  assert.deepEqual(roundExecutionStatusMeta("future_status"), {
    label: "future status",
    tone: "muted",
  });
});

test("every current backend event type has an intentional display group", () => {
  const expected = {
    round_started: "round",
    round_terminal: "round",
    provider_run_created: "provider",
    provider_call_started: "provider",
    provider_call_finished: "provider",
    director_attempt_started: "director",
    director_attempt_finished: "director",
    director_decision_recorded: "director",
    round_turn_reserved: "turn",
    round_turn_terminal: "turn",
    message_persisted: "turn",
    candidate_update_submitted: "candidate",
    candidate_risk_review_projected: "review",
    candidate_decision_projected: "candidate",
    risk_registered: "review",
    artifact_created: "artifact",
    artifact_confirmed: "artifact",
    user_decision_recorded: "user",
    decision_lineage_event: "candidate",
  };

  for (const [type, group] of Object.entries(expected)) {
    assert.equal(roundExecutionEventMeta(type).group, group, type);
    assert.notEqual(roundExecutionEventMeta(type).label, "其他记录", type);
  }
});

test("candidate projection view model preserves deep candidate versions, current reviews, and the conditional preferred option", () => {
  const trace = normalizeRoundExecutionTrace(traceFixture({
    candidate_projection: candidateProjectionFixture(),
  }));
  const view = candidateProjectionViewModel(trace.candidate_projection);

  assert.equal(trace.candidate_projection.decision.options[0].title, "方案 A");
  assert.equal(view.available, true);
  assert.equal(view.authoritative, true);
  assert.equal(view.statusLabel, "已封印投影");
  assert.equal(view.candidateCount, 2);
  assert.equal(view.totalRevisionCount, 3);
  assert.equal(view.decision.ready, true);
  assert.equal(view.decision.preferredOptionId, "candidate_b");
  assert.equal(view.decision.preferredTitle, "方案 B");
  assert.equal(view.decision.rationale, "方案 B 在共同维度上更可逆。");
  assert.equal(view.decision.evidenceCount, 1);

  const candidateA = view.candidates.find((candidate) => candidate.id === "candidate_a");
  assert.equal(candidateA.revision, 2);
  assert.equal(candidateA.currentReviewCount, 1);
  assert.equal(candidateA.staleReviewCount, 1);
  assert.deepEqual(candidateA.actionCounts, { support: 1, challenge: 0, reject: 0 });
  assert.equal(candidateA.invalidation, "证据 A 被推翻。");
  assert.equal(candidateA.evidenceCount, 1);

  const candidateB = view.candidates.find((candidate) => candidate.id === "candidate_b");
  assert.equal(candidateB.preferred, true);
  assert.deepEqual(candidateB.actionCounts, { support: 0, challenge: 0, reject: 1 });
  assert.deepEqual(view.riskReview.actionCounts, { support: 1, challenge: 1, reject: 1 });
  assert.equal(view.riskReview.staleReviewCount, 1);
});

test("candidate projection view model distinguishes no projection, provisional work, and unsafe boundaries", () => {
  const missing = candidateProjectionViewModel(null);
  assert.equal(missing.available, false);
  assert.equal(missing.status, "empty");
  assert.equal(missing.candidates.length, 0);

  const provisional = candidateProjectionViewModel(candidateProjectionFixture({
    authoritative: false,
    provisional: true,
    decision: {
      status: "undecided",
      preferred_option_id: "",
      rationale: "",
      evidence: [],
      options: [],
    },
    candidate_lineage: {
      version: "candidate_lineage_v1",
      applicable: true,
      ready: false,
      status: "decision_missing",
      candidates: [],
      issues: [],
    },
    candidate_risk_reviews: {
      version: "candidate_risk_review_v1",
      applicable: true,
      ready: false,
      status: "decision_missing",
      target_candidate_count: 0,
      reviewed_candidate_count: 0,
      current_review_count: 0,
      stale_review_count: 0,
      action_counts: { support: 0, challenge: 0, reject: 0 },
      review_actions_are_dispositions_only: true,
      execution_capability: "none",
      live_trading_allowed: false,
      can_autonomously_decide: false,
      reviews: [],
      issues: [],
    },
  }));
  assert.equal(provisional.available, true);
  assert.equal(provisional.statusLabel, "进行中投影");
  assert.equal(provisional.candidateCount, 0);
  assert.equal(provisional.decision.ready, false);

  const unsafe = candidateProjectionViewModel(candidateProjectionFixture({
    execution_capability: "trade",
  }));
  assert.equal(unsafe.safetyVerified, false);
  assert.equal(unsafe.tone, "blocked");
  assert.match(unsafe.issues[0].message, /只读、无执行和用户最终决定边界/);
});

test("unsupported versions and unsafe execution capabilities fail closed", () => {
  const unsafe = normalizeRoundExecutionTrace(traceFixture({
    version: "round_execution_trace_v2",
    safety: {
      read_only: false,
      provider_calls_performed: 1,
      execution_capability: "trade",
      live_trading_allowed: true,
      can_autonomously_decide: true,
    },
  }));

  assert.equal(unsafe.valid, false);
  assert.match(unsafe.errors.join(" "), /版本无法验证/);
  assert.match(unsafe.errors.join(" "), /只读安全边界无法验证/);

  const falseMasqueradingAsZero = normalizeRoundExecutionTrace(traceFixture({
    safety: {
      ...traceFixture().safety,
      provider_calls_performed: false,
    },
  }));
  assert.equal(falseMasqueradingAsZero.valid, false);

  const extendedHash = normalizeRoundExecutionTrace(traceFixture({
    trace_hash: `${TRACE_HASH}0`,
  }));
  assert.equal(extendedHash.valid, false);
});

test("pagination merge preserves order, deduplicates overlap, and requires one frozen trace", () => {
  const first = normalizeRoundExecutionTrace(traceFixture());
  const second = traceFixture({
    events: [
      first.events[1],
      {
        ordinal: 3,
        event_id: "event_3",
        type: "round_completed",
        occurred_at: 4,
        finished_at: 4,
        source: { table: "rounds", id: "round_1", sequence_no: 3 },
        actor: { kind: "service", id: "service_1", name: "调度器" },
        status: "completed",
        refs: {},
        payload: {},
        integrity: { status: "verified", issues: [] },
      },
    ],
    page: { limit: 200, cursor: 2, next_cursor: null, has_more: false, total: 3 },
  });
  const merged = mergeRoundExecutionTracePages(first, second);

  assert.deepEqual(merged.events.map((event) => event.event_id), ["event_1", "event_2", "event_3"]);
  assert.equal(merged.page.has_more, false);
  assert.throws(
    () => mergeRoundExecutionTracePages(first, { ...second, trace_hash: "c".repeat(64) }),
    /不属于同一冻结快照/,
  );
});

test("compact summary reports persisted counts without triggering work", () => {
  assert.equal(
    roundExecutionTraceSummaryText(traceFixture()),
    "3 步 · 2/28 次调用 · 1 个异常",
  );
});

test("trace UI renders the director sub-budget, candidate projection, and anchor snapshot state from pure helpers", () => {
  const dialogSource = readFileSync(
    new URL("../src/components/RoundExecutionTraceDialog.jsx", import.meta.url),
    "utf8",
  );
  const summarySource = readFileSync(
    new URL("../src/components/RoundExecutionTraceSummary.jsx", import.meta.url),
    "utf8",
  );

  assert.match(dialogSource, /roundExecutionDirectorBudget/);
  assert.match(dialogSource, /candidateProjectionViewModel/);
  assert.match(dialogSource, /候选形成只读投影/);
  assert.match(dialogSource, /当前条件化首选/);
  assert.match(dialogSource, /roundExecutionTraceAnchorState/);
  assert.match(dialogSource, /锚点序列/);
  assert.match(dialogSource, /当前快照/);
  assert.match(summarySource, /roundExecutionTraceAnchorState/);
});

test("trace dialog projection normalizes display input and bounds the mounted event window", () => {
  const projected = roundTraceDialogProjection(traceFixture());
  assert.equal(projected.ready, true);
  const malformed = roundTraceDialogProjection({ valid: true, events: "not-an-array" });
  assert.equal(malformed.ready, false);
  assert.equal(roundTraceDisplayText({ unsafe: true }, "fallback"), "fallback");
  assert.equal(roundTraceErrorMessage({ message: { unsafe: true } }, "safe error"), "safe error");
  const windowed = roundTraceEventWindow(
    Array.from({ length: 230 }, (_, index) => ({ event_id: `event_${index}` })),
    100,
  );
  assert.equal(windowed.visibleCount, 100);
  assert.equal(windowed.hiddenCount, 130);
  assert.equal(windowed.nextCount, 200);
});

test("trace dialog shares modal focus and owns safe-area, reduced-motion, and local reveal contracts", () => {
  const dialogSource = readFileSync(
    new URL("../src/components/RoundExecutionTraceDialog.jsx", import.meta.url),
    "utf8",
  );
  const dialogStyles = readFileSync(
    new URL("../src/styles/round-execution-trace.css", import.meta.url),
    "utf8",
  );
  assert.match(dialogSource, /import \{ useModalFocus \} from "\.\.\/useModalFocus"/);
  assert.match(dialogSource, /useModalFocus\(\{[\s\S]*containerRef: dialogRef,[\s\S]*initialFocusRef: closeButtonRef/);
  assert.doesNotMatch(dialogSource, /querySelectorAll\(/);
  assert.match(dialogSource, /useMemo\(\(\) => roundTraceDialogProjection\(trace\), \[trace\]\)/);
  assert.match(dialogSource, /useEffect\(\(\) => \{\s*setVisibleEventCount\(ROUND_TRACE_INITIAL_EVENT_WINDOW\);\s*\}, \[open, trace\?\.trace_hash\]\)/);
  assert.match(dialogSource, /data-rendered-event-count=\{eventWindow\.visibleCount\}/);
  assert.match(dialogSource, /显示更多已载入步骤/);
  assert.match(dialogStyles, /\.round-trace-window-status\s*\{/);
  assert.match(dialogStyles, /env\(safe-area-inset-top\)/);
  assert.match(dialogStyles, /prefers-reduced-motion: reduce/);
});
