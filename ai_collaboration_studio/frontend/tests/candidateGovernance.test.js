import test from "node:test";
import assert from "node:assert/strict";
import {
  ARTIFACT_GOVERNANCE_BOUNDARY,
  artifactCandidateGovernance,
  artifactGovernanceBadge,
  candidateGovernanceRows,
  riskDispositionMeta,
} from "../src/candidateGovernance.js";

test("keeps historical convergence payloads free of new candidate gates", () => {
  assert.deepEqual(candidateGovernanceRows({}), []);
  assert.deepEqual(candidateGovernanceRows(null), []);
});

test("renders a ready frozen candidate lineage", () => {
  assert.deepEqual(candidateGovernanceRows({
    candidate_lineage_gate: {
      applicable: true,
      ready: true,
      candidate_count: 2,
      blockers: [],
    },
  }), [{
    id: "candidate-lineage",
    ready: true,
    label: "候选版本谱系",
    detail: "2 个候选 · 决策仅引用冻结版本",
  }]);
});

test("fails closed and exposes missing or stale exact-version reviews", () => {
  const rows = candidateGovernanceRows({
    candidate_risk_review_gate: {
      applicable: true,
      ready: false,
      candidate_count: 2,
      reviewed_candidate_count: 1,
      support_count: 0,
      challenge_count: 1,
      reject_count: 0,
      stale_review_count: 1,
      blockers: [{ code: "REVIEW_MISSING" }],
    },
  });

  assert.equal(rows[0].ready, false);
  assert.equal(
    rows[0].detail,
    "1 / 2 个精确版本 · 支持 0 / 质疑 1 / 拒绝 0 · 过期 1",
  );
});

test("shows review dispositions without turning them into a user decision", () => {
  const [row] = candidateGovernanceRows({
    candidate_risk_review_gate: {
      applicable: true,
      ready: true,
      candidate_count: 3,
      reviewed_candidate_count: 3,
      support_count: 1,
      challenge_count: 1,
      reject_count: 1,
    },
  });

  assert.equal(row.ready, true);
  assert.match(row.detail, /支持 1 \/ 质疑 1 \/ 拒绝 1/);
  assert.doesNotMatch(row.detail, /用户|决定|执行/);
});

test("keeps legacy artifacts explicitly outside durable candidate governance", () => {
  const governance = artifactCandidateGovernance({ content: {} });

  assert.equal(governance.available, false);
  assert.equal(governance.status, "legacy");
  assert.equal(governance.boundary, ARTIFACT_GOVERNANCE_BOUNDARY);
  assert.equal(artifactGovernanceBadge({ content: {} }), null);
});

test("normalizes server-bound lineage and exact-version risk reviews", () => {
  const artifact = {
    version: 3,
    content: {
      decision: {
        preferred_option_id: "candidate_a",
        options: [
          { id: "candidate_a", title: "可编辑标题不应覆盖冻结标题" },
          { id: "candidate_b", title: "候选 B" },
        ],
      },
    },
    governance_snapshot: {
      version: "turn_contract_v1",
      candidate_lineage: {
        version: "candidate_lineage_v1",
        applicable: true,
        ready: true,
        status: "ready",
        decision_message_id: "message_decision",
        candidates: [
          {
            id: "candidate_a",
            origin_message_id: "message_origin",
            latest_message_id: "message_revision",
            revision: 2,
          },
          {
            id: "candidate_b",
            origin_message_id: "message_b",
            latest_message_id: "message_b",
            revision: 1,
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
        stale_review_count: 0,
        action_counts: { support: 1, challenge: 1, reject: 0 },
        reviews: [
          {
            candidate_id: "candidate_a",
            candidate_revision: 2,
            current_candidate_revision: 2,
            candidate_latest_message_id: "message_revision",
            candidate_snapshot: { title: "冻结候选 A" },
            candidate_snapshot_sha256: "a".repeat(64),
            action: "support",
            status: "current",
            review_message_id: "message_review_a",
            reviewer_member_id: "member_risk",
            reviewer_member_version: 4,
            reviewer_name: "风险负责人",
            risk_ids: ["risk_1"],
          },
          {
            candidate_id: "candidate_b",
            candidate_revision: 1,
            current_candidate_revision: 1,
            action: "challenge",
            status: "current",
            review_message_id: "message_review_b",
          },
        ],
        issues: [],
      },
    },
  };

  const governance = artifactCandidateGovernance(artifact);
  assert.equal(governance.available, true);
  assert.equal(governance.ready, true);
  assert.equal(governance.lineage.candidates[0].title, "冻结候选 A");
  assert.equal(governance.lineage.candidates[0].preferred, true);
  assert.equal(governance.lineage.candidates[0].revision, 2);
  assert.equal(governance.riskReview.reviews[0].candidateSnapshotSha256, "a".repeat(64));
  assert.equal(governance.riskReview.reviews[0].dispositionLabel, "风控意见：支持");
  assert.deepEqual(governance.riskReview.actionCounts, {
    support: 1,
    challenge: 1,
    reject: 0,
  });
  assert.deepEqual(artifactGovernanceBadge(artifact), {
    label: "谱系与风控已绑定",
    tone: "ready",
    title: "2 个候选，2 条当前版本风控意见",
  });
});

test("fails closed for stale or missing durable governance records", () => {
  const artifact = {
    content: { decision: { options: [] } },
    governance_snapshot: {
      candidate_lineage: {
        applicable: true,
        ready: true,
        candidates: [],
      },
      candidate_risk_reviews: {
        applicable: true,
        ready: false,
        target_candidate_count: 1,
        reviewed_candidate_count: 0,
        current_review_count: 0,
        stale_review_count: 1,
        reviews: [{
          candidate_id: "candidate_a",
          candidate_revision: 1,
          current_candidate_revision: 2,
          action: "reject",
          status: "stale",
          review_message_id: "message_old_review",
        }],
        issues: [{ message: "候选 candidate_a 已修订，旧意见失效。" }],
      },
    },
  };

  const governance = artifactCandidateGovernance(artifact);
  assert.equal(governance.ready, false);
  assert.equal(governance.riskReview.reviews[0].status, "stale");
  assert.equal(governance.riskReview.reviews[0].dispositionLabel, "风控意见：拒绝");
  assert.deepEqual(governance.riskReview.issues, ["候选 candidate_a 已修订，旧意见失效。"]);
  assert.equal(artifactGovernanceBadge(artifact).label, "治理记录待补齐");
});

test("risk dispositions remain labels rather than user authorization", () => {
  assert.deepEqual(riskDispositionMeta("support"), {
    label: "风控意见：支持",
    tone: "support",
  });
  assert.equal(
    ARTIFACT_GOVERNANCE_BOUNDARY,
    "风控意见不是用户决定、批准、否决或执行授权。",
  );
  assert.doesNotMatch(riskDispositionMeta("reject").label, /用户|批准|授权/);
});

test("accepts an attestation envelope and fails closed on explicit integrity failure", () => {
  const artifact = {
    content: { decision: { options: [] } },
    governance_snapshot: {
      version: "artifact_governance_v1",
      applicable: true,
      status: "ready",
      attestation_version: "artifact_governance_attestation_v1",
      attestation_sha256: "c".repeat(64),
      integrity_ok: false,
      projection: {
        version: "turn_contract_v1",
        candidate_lineage: {
          applicable: true,
          ready: true,
          candidates: [],
        },
        candidate_risk_reviews: {
          applicable: false,
          ready: true,
          status: "not_required",
          reviews: [],
        },
      },
    },
  };

  const governance = artifactCandidateGovernance(artifact);
  assert.equal(governance.version, "artifact_governance_v1");
  assert.equal(governance.attestationVersion, "artifact_governance_attestation_v1");
  assert.equal(governance.attestationSha256, "c".repeat(64));
  assert.equal(governance.lineage.ready, true);
  assert.equal(governance.riskReview.applicable, false);
  assert.equal(governance.integrityOk, false);
  assert.equal(governance.ready, false);
  assert.equal(artifactGovernanceBadge(artifact).tone, "blocked");
});

test("distinguishes an explicit non-applicable snapshot from a failed snapshot", () => {
  const artifact = {
    governance_snapshot: {
      version: "artifact_governance_v1",
      applicable: false,
      status: "not_applicable",
      integrity_ok: true,
      issues: [{ message: "该产物未绑定正式轮次。" }],
      execution_capability: "none",
      live_trading_allowed: false,
      can_autonomously_decide: false,
    },
  };

  const governance = artifactCandidateGovernance(artifact);
  assert.equal(governance.applicable, false);
  assert.equal(governance.status, "not_applicable");
  assert.equal(governance.ready, false);
  assert.deepEqual(artifactGovernanceBadge(artifact), {
    label: "候选治理不适用",
    tone: "neutral",
    title: "该产物未绑定正式轮次。",
  });
});

test("fails closed when a governance snapshot contradicts the no-execution boundary", () => {
  const artifact = {
    governance_snapshot: {
      version: "artifact_governance_v1",
      applicable: true,
      status: "ready",
      integrity_ok: true,
      attestation_integrity_ok: true,
      candidate_lineage: { applicable: true, ready: true, candidates: [] },
      candidate_risk_reviews: { applicable: false, ready: true, reviews: [] },
      execution_capability: "orders",
      live_trading_allowed: false,
      can_autonomously_decide: false,
    },
  };

  const governance = artifactCandidateGovernance(artifact);
  assert.equal(governance.integrityOk, true);
  assert.equal(governance.safetyOk, false);
  assert.equal(governance.ready, false);
  assert.equal(artifactGovernanceBadge(artifact).tone, "blocked");
});
