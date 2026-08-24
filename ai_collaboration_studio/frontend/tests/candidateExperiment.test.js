import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  buildCandidateExperimentRequest,
  candidateExperimentAuthorizationGate,
  candidateExperimentControlState,
  candidateExperimentErrorMessage,
  candidateExperimentRequestIdentity,
  candidateExperimentSelectionFingerprint,
  candidateExperimentView,
  CANDIDATE_EXPERIMENT_AUTHORIZATION_VERSION,
  CANDIDATE_EXPERIMENT_ARM_LIMIT,
  CANDIDATE_EXPERIMENT_COHORT_VERSION,
  CANDIDATE_EXPERIMENT_EVIDENCE_LIMIT,
  CANDIDATE_EXPERIMENT_ISSUE_LIMIT,
  CANDIDATE_EXPERIMENT_REQUEST_VERSION,
} from "../src/candidateExperiment.js";
import { buildArtifactUserDecisionRequest } from "../src/artifactUserDecision.js";


const ATTESTATION_SHA = "a".repeat(64);
const SPEC_SHA = "b".repeat(64);
const DATASET_SHA = "c".repeat(64);
const INPUT_SHA = "d".repeat(64);
const AGGREGATE_SHA = "e".repeat(64);
const SEMANTICS_SHA = "f".repeat(64);
const REQUEST_ID = "candidate_experiment_request_12345678";
const SAFETY_FIELDS = Object.freeze({
  execution_capability: "none",
  live_trading_allowed: false,
  can_autonomously_decide: false,
  ranking_produced: false,
  winner_claim: false,
  user_final_decision_required: true,
});


function clone(value) {
  return JSON.parse(JSON.stringify(value));
}


function candidate(candidateId, index, symbol, direction) {
  return {
    id: candidateId,
    title: `候选 ${index + 1}`,
    description: `候选 ${index + 1} 的冻结描述`,
    lineage: {
      version: "candidate_lineage_v1",
      revision: index + 1,
      origin_message_id: `message_${candidateId}_origin`,
      latest_message_id: `message_${candidateId}_latest`,
    },
    snapshot: {
      title: `候选 ${index + 1}`,
      symbol,
      direction,
      horizon_days: 20,
      thesis: `候选 ${index + 1} 的历史研究论点`,
      invalidation: `候选 ${index + 1} 的明确失效条件`,
    },
    snapshotSha256: String(index + 1).repeat(64),
  };
}


function governedArtifact() {
  const candidates = [
    candidate("candidate_a", 0, "US.MU", "UP"),
    candidate("candidate_b", 1, "US.WDC", "DOWN"),
    candidate("candidate_c", 2, "US.STX", "UP"),
  ];
  const options = candidates.map(({ snapshot, snapshotSha256, ...item }) => item);
  const projection = {
    version: "turn_contract_v1",
    decision: {
      status: "candidate",
      preferred_option_id: "candidate_a",
      options,
    },
    candidate_lineage: {
      version: "candidate_lineage_v1",
      applicable: true,
      ready: true,
      status: "ready",
      candidates: candidates.map((item) => ({
        id: item.id,
        revision: item.lineage.revision,
        origin_message_id: item.lineage.origin_message_id,
        latest_message_id: item.lineage.latest_message_id,
      })),
      issues: [],
    },
    candidate_risk_reviews: {
      version: "candidate_risk_review_v1",
      applicable: true,
      ready: true,
      status: "ready",
      target_candidate_count: candidates.length,
      reviewed_candidate_count: candidates.length,
      current_review_count: candidates.length,
      stale_review_count: 0,
      reviews: candidates.map((item, index) => ({
        candidate_id: item.id,
        candidate_revision: item.lineage.revision,
        current_candidate_revision: item.lineage.revision,
        candidate_latest_message_id: item.lineage.latest_message_id,
        candidate_snapshot: item.snapshot,
        candidate_snapshot_sha256: item.snapshotSha256,
        action: index === 0 ? "support" : index === 1 ? "challenge" : "reject",
        status: "current",
        review_message_id: `review_${item.id}`,
        reviewer_member_id: "risk_member",
        reviewer_member_version: 2,
      })),
      issues: [],
      review_actions_are_dispositions_only: true,
      execution_capability: "none",
      live_trading_allowed: false,
      can_autonomously_decide: false,
    },
    execution_capability: "none",
    live_trading_allowed: false,
    can_autonomously_decide: false,
  };
  return {
    id: "artifact_1",
    room_id: "room_1",
    status: "CONFIRMED",
    version: 4,
    evidence_review: { confirmation_ready: true },
    content: {
      decision: {
        status: "candidate",
        preferred_option_id: "candidate_a",
        options,
      },
    },
    governance_snapshot: {
      version: "artifact_governance_v1",
      applicable: true,
      ready: true,
      status: "ready",
      integrity_ok: true,
      attestation_integrity_ok: true,
      attestation_sha256: ATTESTATION_SHA,
      artifact: { artifact_id: "artifact_1", artifact_version: 4 },
      artifact_alignment: { ready: true, integrity_ok: true },
      projection,
      execution_capability: "none",
      live_trading_allowed: false,
      can_autonomously_decide: false,
    },
  };
}


function scenario(scenarioId, offset = 0) {
  return {
    scenario_id: scenarioId,
    state: "sufficient",
    blocked: false,
    metrics_visible: true,
    metrics: {
      portfolio_cumulative_return_pct: 9 - offset,
      historical_positive_window_ratio: 0.56,
      max_drawdown_pct: -4 - offset,
      mean_window_return_pct: 0.7,
      worst_window_return_pct: -1.4 - offset,
    },
    capacity_gap_usd: null,
    first_blocker: null,
  };
}


function requestFor(candidateIds = ["candidate_a", "candidate_b"]) {
  return buildCandidateExperimentRequest(governedArtifact(), {
    candidateIds,
    clientRequestId: REQUEST_ID,
  });
}


function readyExperiment(candidateIds = ["candidate_a", "candidate_b"]) {
  const request = requestFor(candidateIds);
  const artifact = governedArtifact();
  const gate = candidateExperimentAuthorizationGate(artifact);
  const candidateById = new Map(gate.candidates.map((item) => [item.id, item]));
  return {
    version: CANDIDATE_EXPERIMENT_COHORT_VERSION,
    id: "cohort_1",
    room_id: "room_1",
    artifact_id: "artifact_1",
    artifact_version: 4,
    client_request_id: REQUEST_ID,
    status: "ready",
    integrity_ok: true,
    metrics_visible: true,
    authorization: {
      version: CANDIDATE_EXPERIMENT_AUTHORIZATION_VERSION,
      client_request_id: REQUEST_ID,
      artifact_id: "artifact_1",
      expected_artifact_version: 4,
      expected_governance_attestation_sha256: ATTESTATION_SHA,
      candidate_selections: request.candidate_selections,
      user_authorized_historical_comparison: true,
      does_not_imply_artifact_support: true,
      does_not_create_artifact_user_decision: true,
    },
    common_spec: {
      cutoff_date: "2025-12-31",
      price_adjustment: "QFQ",
      trading_calendar: "XNYS-common-v1",
      horizon_days: 20,
      target_weight_pct: 25,
      train_days: 99,
      test_days: 20,
      step_days: 20,
      engine_version: "walk_forward_engine_v3",
      evaluation_rule: "fixed_direction_historical_v1",
      friction_scenario_set: "storage_friction_scenarios_v1",
      unfillable_policy: "block_scenario_no_partial_fill",
    },
    dataset_seal: {
      actual_start: "2024-01-02",
      actual_end: "2025-12-31",
      common_trading_days: 500,
    },
    spec_sha256: SPEC_SHA,
    dataset_seal_sha256: DATASET_SHA,
    input_seal_sha256: INPUT_SHA,
    aggregate_sha256: AGGREGATE_SHA,
    request_semantics_sha256: SEMANTICS_SHA,
    arms: candidateIds.map((candidateId, index) => {
      const candidateItem = candidateById.get(candidateId);
      return {
        sequence_no: index + 1,
        candidate_id: candidateId,
        candidate_revision: candidateItem.revision,
        candidate_snapshot_sha256: candidateItem.snapshotSha256,
        title: candidateItem.title,
        symbol: candidateItem.symbol,
        direction: candidateItem.direction,
        side: candidateItem.direction === "UP" ? "LONG" : "SHORT",
        thesis: candidateItem.thesis,
        invalidation: candidateItem.invalidation,
        evidence: [{ id: `evidence_${candidateId}`, title: `支持证据 ${candidateId}` }],
        counterevidence: [{ id: `counter_${candidateId}`, text: `反证 ${candidateId}` }],
        candidate_binding_sha256: String(index + 4).repeat(64),
        shared_spec_sha256: SPEC_SHA,
        shared_dataset_seal_sha256: DATASET_SHA,
        integrity_ok: true,
        metrics_visible: true,
        scenarios: [scenario("baseline"), scenario("stressed", 1), scenario("severe", 2)],
        ...SAFETY_FIELDS,
      };
    }),
    market_data_reads: 1,
    provider_calls_total: 0,
    openai_calls: 0,
    historical_only: true,
    out_of_sample_claim: false,
    future_performance_claim: false,
    execution_capability: "none",
    live_trading_allowed: false,
    can_autonomously_decide: false,
    ranking_produced: false,
    winner_claim: false,
    user_final_decision_required: true,
    issues: [],
  };
}


function expectedFor(request) {
  return {
    roomId: "room_1",
    artifactId: "artifact_1",
    artifactVersion: 4,
    clientRequestId: REQUEST_ID,
    attestationSha256: ATTESTATION_SHA,
    candidateSelections: request.candidate_selections,
  };
}


test("builds an independent 2-6 candidate experiment authorization without decision or run fields", () => {
  const artifact = governedArtifact();
  const gate = candidateExperimentAuthorizationGate(artifact);
  assert.equal(gate.ready, true);
  assert.deepEqual(gate.candidates.map((item) => item.id), [
    "candidate_a",
    "candidate_b",
    "candidate_c",
  ]);

  const payload = buildCandidateExperimentRequest(artifact, {
    candidateIds: ["candidate_b", "candidate_a"],
    clientRequestId: REQUEST_ID,
  });
  assert.equal(payload.version, CANDIDATE_EXPERIMENT_REQUEST_VERSION);
  assert.equal(payload.artifact_id, "artifact_1");
  assert.equal(payload.expected_artifact_version, 4);
  assert.equal(payload.expected_governance_attestation_sha256, ATTESTATION_SHA);
  assert.deepEqual(payload.candidate_selections.map((item) => item.candidate_id), [
    "candidate_b",
    "candidate_a",
  ]);
  assert.deepEqual(Object.keys(payload).sort(), [
    "artifact_id",
    "candidate_selections",
    "client_request_id",
    "expected_artifact_version",
    "expected_governance_attestation_sha256",
    "user_authorized_historical_comparison",
    "version",
  ]);
  const serialized = JSON.stringify(payload);
  for (const forbidden of ["support", "user_decision", "candidate_simulation_contract", "run_id"]) {
    assert.equal(serialized.includes(forbidden), false);
  }
});


test("authorization fails closed on count, duplicates, artifact drift, and snapshot drift", () => {
  const artifact = governedArtifact();
  assert.throws(
    () => buildCandidateExperimentRequest(artifact, {
      candidateIds: ["candidate_a"],
      clientRequestId: REQUEST_ID,
    }),
    /2–6/,
  );
  assert.throws(
    () => buildCandidateExperimentRequest(artifact, {
      candidateIds: ["candidate_a", "candidate_a"],
      clientRequestId: REQUEST_ID,
    }),
    /2–6/,
  );

  const artifactDrift = governedArtifact();
  artifactDrift.governance_snapshot.artifact.artifact_version = 3;
  assert.equal(candidateExperimentAuthorizationGate(artifactDrift).ready, false);

  const snapshotDrift = governedArtifact();
  snapshotDrift.governance_snapshot.projection.candidate_risk_reviews.reviews[0]
    .candidate_snapshot_sha256 = "not-a-hash";
  assert.equal(candidateExperimentAuthorizationGate(snapshotDrift).ready, false);
});


test("same semantic fingerprint retains one request id and changed semantics rotates it", () => {
  let generated = 0;
  const createId = () => `candidate_experiment_generated_${++generated}_12345678`;
  const first = candidateExperimentRequestIdentity({}, "semantic_a", createId);
  const retry = candidateExperimentRequestIdentity(first, "semantic_a", createId);
  const changed = candidateExperimentRequestIdentity(retry, "semantic_b", createId);

  assert.equal(first.clientRequestId, retry.clientRequestId);
  assert.notEqual(changed.clientRequestId, retry.clientRequestId);
  assert.equal(generated, 2);
});


test("selection fingerprint and control workflow fail closed until 2-6 unique candidates are bound", () => {
  const artifact = governedArtifact();
  assert.equal(candidateExperimentSelectionFingerprint(artifact, ["candidate_a"]), "");
  assert.equal(
    candidateExperimentSelectionFingerprint(artifact, ["candidate_a", "candidate_a"]),
    "",
  );
  const fingerprint = candidateExperimentSelectionFingerprint(
    artifact,
    ["candidate_b", "candidate_a"],
  );
  assert.ok(fingerprint);

  const authorization = candidateExperimentControlState({
    gateReady: true,
    selectedCandidateIds: ["candidate_b", "candidate_a"],
    selectionFingerprint: fingerprint,
  });
  assert.equal(authorization.phase, "authorize");
  assert.equal(authorization.canAcknowledge, true);
  assert.equal(authorization.canRun, false);
  assert.deepEqual(
    authorization.steps.map((step) => step.status),
    ["complete", "active", "pending", "pending"],
  );

  const review = candidateExperimentControlState({
    gateReady: true,
    acknowledged: true,
    selectedCandidateIds: ["candidate_b", "candidate_a"],
    selectionFingerprint: fingerprint,
    resultReady: true,
  });
  assert.equal(review.phase, "review");
  assert.equal(review.canRun, true);
  assert.deepEqual(
    review.steps.map((step) => step.status),
    ["complete", "complete", "complete", "active"],
  );
});


test("a reread cohort preserves authorization order and exposes neutral historical evidence", () => {
  const request = requestFor(["candidate_b", "candidate_a"]);
  const view = candidateExperimentView(
    readyExperiment(["candidate_b", "candidate_a"]),
    expectedFor(request),
  );
  assert.equal(view.ready, true);
  assert.equal(view.metricsVisible, true);
  assert.deepEqual(view.arms.map((arm) => arm.candidateId), ["candidate_b", "candidate_a"]);
  assert.equal(view.arms[0].evidence[0].label, "支持证据 candidate_b");
  assert.equal(view.arms[0].counterevidence[0].label, "反证 candidate_b");
  assert.equal(view.arms[0].scenarios[0].cumulativeReturnPct, 9);
  assert.equal(view.rankingProduced, false);
  assert.equal(view.winnerClaim, false);
  assert.equal(view.userFinalDecisionRequired, true);
});


test("any input, arm, result, aggregate, order, or safety drift hides the whole cohort", () => {
  const request = requestFor();
  for (const mutate of [
    (experiment) => { experiment.authorization.candidate_selections[0].expected_candidate_snapshot_sha256 = "9".repeat(64); },
    (experiment) => { experiment.arms[0].shared_dataset_seal_sha256 = "8".repeat(64); },
    (experiment) => { experiment.arms[0].integrity_ok = false; },
    (experiment) => { experiment.arms.reverse(); },
    (experiment) => { experiment.aggregate_sha256 = "invalid"; },
    (experiment) => { experiment.arms[0].scenarios[0].metrics.mean_window_return_pct = "0.7"; },
    (experiment) => { experiment.market_data_reads = 0; },
    (experiment) => { experiment.provider_calls_total = 1; },
    (experiment) => { experiment.winner_claim = true; },
    (experiment) => { experiment.historical_only = false; },
    (experiment) => { experiment.out_of_sample_claim = true; },
    (experiment) => { experiment.future_performance_claim = true; },
    (experiment) => { experiment.authorization.does_not_imply_artifact_support = false; },
    (experiment) => { experiment.authorization.does_not_create_artifact_user_decision = false; },
    (experiment) => { experiment.arms[0].execution_capability = "paper"; },
    (experiment) => { experiment.arms[0].live_trading_allowed = true; },
    (experiment) => { experiment.arms[0].can_autonomously_decide = true; },
    (experiment) => { experiment.arms[0].ranking_produced = true; },
    (experiment) => { experiment.arms[0].winner_claim = true; },
    (experiment) => { experiment.arms[0].user_final_decision_required = false; },
  ]) {
    const experiment = readyExperiment();
    mutate(experiment);
    const view = candidateExperimentView(experiment, expectedFor(request));
    assert.equal(view.ready, false);
    assert.equal(view.metricsVisible, false);
    assert.ok(view.arms.every((arm) => arm.metricsVisible === false));
    assert.ok(view.arms.every((arm) => arm.scenarios.length === 0));
  }
});


test("a capacity-blocked scenario is a valid historical result and never exposes metrics", () => {
  const request = requestFor();
  const experiment = readyExperiment();
  const blocked = experiment.arms[0].scenarios[2];
  blocked.state = "blocked";
  blocked.blocked = true;
  blocked.metrics_visible = false;
  blocked.metrics = {
    portfolio_cumulative_return_pct: null,
    historical_positive_window_ratio: null,
    max_drawdown_pct: null,
    mean_window_return_pct: null,
    worst_window_return_pct: null,
  };
  blocked.capacity_gap_usd = 125000;
  blocked.first_blocker = { code: "CAPACITY_BLOCKED", message: "共同成交量约束不足" };

  const view = candidateExperimentView(experiment, expectedFor(request));
  assert.equal(view.ready, true);
  assert.equal(view.arms[0].scenarios[2].blocked, true);
  assert.equal(view.arms[0].scenarios[2].metricsVisible, false);
  assert.equal(view.arms[0].scenarios[2].cumulativeReturnPct, null);
  assert.equal(view.arms[0].scenarios[2].capacityGapUsd, 125000);
});


test("bounds errors and oversized cohort collections before display projection", () => {
  assert.equal(candidateExperimentErrorMessage({ message: { unsafe: true } }, "fallback"), "fallback");
  assert.equal(candidateExperimentErrorMessage({ message: "x".repeat(1500) }).length, 1000);

  const tooManyArms = readyExperiment();
  tooManyArms.arms = Array.from({ length: CANDIDATE_EXPERIMENT_ARM_LIMIT + 1 }, () => null);
  assert.equal(candidateExperimentView(tooManyArms, expectedFor(requestFor())).ready, false);

  const tooMuchEvidence = readyExperiment();
  tooMuchEvidence.arms[0].evidence = Array.from({ length: CANDIDATE_EXPERIMENT_EVIDENCE_LIMIT + 1 }, () => "evidence");
  assert.equal(candidateExperimentView(tooMuchEvidence, expectedFor(requestFor())).ready, false);

  const tooManyIssues = readyExperiment();
  tooManyIssues.issues = Array.from({ length: CANDIDATE_EXPERIMENT_ISSUE_LIMIT + 1 }, () => "issue");
  const issueView = candidateExperimentView(tooManyIssues, expectedFor(requestFor()));
  assert.equal(issueView.ready, false);
  assert.equal(issueView.issues[0].code, "CANDIDATE_EXPERIMENT_ISSUE_LIMIT_EXCEEDED");
});


test("historical return does not affect the independent artifact user decision", () => {
  const experiment = readyExperiment();
  experiment.arms[1].scenarios[0].metrics.portfolio_cumulative_return_pct = 40;
  assert.equal(candidateExperimentView(experiment, expectedFor(requestFor())).ready, true);

  const decision = buildArtifactUserDecisionRequest(governedArtifact(), {
    action: "support",
    rationale: "即使候选 B 的历史收益更高，我仍基于证据和失效条件选择候选 A。",
    selectedOptionId: "candidate_a",
  });
  assert.equal(decision.selected_option_id, "candidate_a");
  assert.equal(Object.hasOwn(decision, "candidate_experiment_id"), false);
});


test("panel posts then rereads, ArtifactDialog places it before the independent final decision", () => {
  const panelSource = readFileSync(
    new URL("../src/components/CandidateExperimentPanel.jsx", import.meta.url),
    "utf8",
  );
  const dialogSource = readFileSync(
    new URL("../src/components/ArtifactDialog.jsx", import.meta.url),
    "utf8",
  );
  const hostStyles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
  const artifactDialogStyles = readFileSync(
    new URL("../src/styles/artifact-dialog.css", import.meta.url),
    "utf8",
  );
  const experimentStyles = readFileSync(
    new URL("../src/styles/candidate-experiment.css", import.meta.url),
    "utf8",
  );
  const refinementStyles = readFileSync(
    new URL("../src/styles/candidate-experiment-refinement.css", import.meta.url),
    "utf8",
  );

  assert.ok(panelSource.indexOf("api.createCandidateExperiment") < panelSource.indexOf("api.candidateExperiment"));
  assert.match(panelSource, /requestIdentityRef/);
  assert.match(panelSource, /coordinatorRef\.current\.inFlight/);
  assert.match(panelSource, /runtimeGateReady/);
  assert.match(panelSource, /CANDIDATE_SELECTOR_PAGE_SIZE = 24/);
  assert.match(panelSource, /所有已选候选始终可见/);
  assert.match(panelSource, /"trading_calendar_sha256"/);
  assert.match(panelSource, /不产生排名、赢家、未来胜率或自动决定/);
  assert.match(panelSource, /candidate-experiment\.css/);
  assert.match(panelSource, /coordinatorRef\.current\.cancel\(\);[\s\S]*?selectionFingerprint/);
  assert.equal(panelSource.includes(".sort("), false);
  assert.ok(dialogSource.indexOf("<CandidateExperimentPanel") < dialogSource.indexOf("<MemoUserFinalDecisionSection"));
  assert.match(dialogSource, /footballResearchPackPresent/);
  assert.match(dialogSource, /stockResearchPackPresent/);
  assert.match(dialogSource, /frozenAndCurrentCapabilityPackIds/);
  assert.match(dialogSource, /storageCandidateExperimentAllowed\s*\?\s*\(/);
  assert.match(artifactDialogStyles, /\.candidate-experiment-table-wrap\s*\{[\s\S]*?overflow-x:\s*auto;/);
  assert.match(artifactDialogStyles, /@media \(max-width: 620px\)[\s\S]*?\.candidate-experiment-selector \{ grid-template-columns: 1fr;/);
  assert.match(experimentStyles, /\.candidate-experiment-workbench[\s\S]*?overflow-x:\s*auto;/);
  assert.match(experimentStyles, /\.candidate-experiment-table th:first-child\s*\{[\s\S]*?position:\s*sticky;/);
  assert.match(experimentStyles, /@media \(max-width: 420px\)[\s\S]*?\.candidate-experiment-workflow \{ grid-template-columns: 1fr;/);
  assert.match(refinementStyles, /candidate-experiment-neutrality-ledger/);
  assert.match(refinementStyles, /prefers-reduced-motion/);
  assert.match(refinementStyles, /forced-colors/);
  assert.doesNotMatch(hostStyles, /\.candidate-experiment-table-wrap\s*\{/);
});
