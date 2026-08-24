import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  ARTIFACT_USER_DECISION_VERSION,
  artifactUserDecisionGate,
  artifactUserDecisionPresentation,
  artifactUserDecisionSelection,
  buildArtifactUserDecisionRequest,
} from "../src/artifactUserDecision.js";

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function governedArtifact() {
  const candidates = [
    {
      id: "candidate_a",
      title: "候选 A",
      description: "AI 首选的可逆研究方案。",
      lineage: {
        version: "candidate_lineage_v1",
        revision: 2,
        origin_message_id: "message_a_origin",
        latest_message_id: "message_a_latest",
      },
    },
    {
      id: "candidate_b",
      title: "候选 B",
      description: "用户可以明确选择的替代研究方案。",
      lineage: {
        version: "candidate_lineage_v1",
        revision: 3,
        origin_message_id: "message_b_origin",
        latest_message_id: "message_b_latest",
      },
    },
  ];
  const lineageCandidates = candidates.map((candidate) => ({
    id: candidate.id,
    revision: candidate.lineage.revision,
    origin_message_id: candidate.lineage.origin_message_id,
    latest_message_id: candidate.lineage.latest_message_id,
  }));
  const reviews = candidates.map((candidate, index) => ({
    candidate_id: candidate.id,
    candidate_revision: candidate.lineage.revision,
    current_candidate_revision: candidate.lineage.revision,
    candidate_latest_message_id: candidate.lineage.latest_message_id,
    candidate_snapshot: { title: candidate.title },
    candidate_snapshot_sha256: String(index + 1).repeat(64),
    action: index === 0 ? "support" : "challenge",
    status: "current",
    review_message_id: `review_${candidate.id}`,
    reviewer_member_id: "risk_member",
    reviewer_member_version: 2,
  }));
  const projection = {
    version: "turn_contract_v1",
    decision: {
      status: "candidate",
      preferred_option_id: "candidate_a",
      options: candidates,
    },
    candidate_lineage: {
      version: "candidate_lineage_v1",
      applicable: true,
      ready: true,
      status: "ready",
      candidates: lineageCandidates,
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
      reviews,
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
        options: candidates,
      },
    },
    governance_snapshot: {
      version: "artifact_governance_v1",
      applicable: true,
      ready: true,
      status: "ready",
      integrity_ok: true,
      attestation_integrity_ok: true,
      attestation_sha256: "a".repeat(64),
      artifact: { artifact_id: "artifact_1", artifact_version: 4 },
      artifact_alignment: { ready: true, integrity_ok: true },
      projection,
      execution_capability: "none",
      live_trading_allowed: false,
      can_autonomously_decide: false,
    },
  };
}

function explicitNonGovernedArtifact() {
  const artifact = governedArtifact();
  artifact.governance_snapshot = {
    version: "artifact_governance_v1",
    applicable: false,
    ready: true,
    status: "not_applicable",
    integrity_ok: true,
    attestation_integrity_ok: true,
    artifact: { artifact_id: "artifact_1", artifact_version: 4 },
    artifact_alignment: { applicable: false, ready: true },
    projection: {},
    candidate_lineage: {
      applicable: false,
      ready: true,
      candidates: [],
      issues: [],
    },
    candidate_risk_reviews: {
      applicable: false,
      ready: true,
      reviews: [],
      issues: [],
    },
    execution_capability: "none",
    live_trading_allowed: false,
    can_autonomously_decide: false,
  };
  return artifact;
}

test("exposes only exact governed candidates and keeps AI preference advisory", () => {
  const selection = artifactUserDecisionSelection(governedArtifact());

  assert.equal(selection.ready, true);
  assert.equal(selection.aiPreferredOptionId, "candidate_a");
  assert.deepEqual(selection.candidates.map((candidate) => ({
    id: candidate.id,
    revision: candidate.revision,
    origin: candidate.originMessageId,
    latest: candidate.latestMessageId,
    aiPreferred: candidate.aiPreferred,
  })), [
    {
      id: "candidate_a",
      revision: 2,
      origin: "message_a_origin",
      latest: "message_a_latest",
      aiPreferred: true,
    },
    {
      id: "candidate_b",
      revision: 3,
      origin: "message_b_origin",
      latest: "message_b_latest",
      aiPreferred: false,
    },
  ]);
});

test("support binds the user's candidate and every frozen governance token", () => {
  const payload = buildArtifactUserDecisionRequest(governedArtifact(), {
    action: "support",
    rationale: "我选择候选 B，并保留 AI 首选作为对照。",
    selectedOptionId: "candidate_b",
  });

  assert.deepEqual(payload, {
    expected_version: 4,
    action: "support",
    rationale: "我选择候选 B，并保留 AI 首选作为对照。",
    selected_option_id: "candidate_b",
    expected_candidate_revision: 3,
    expected_candidate_origin_message_id: "message_b_origin",
    expected_candidate_latest_message_id: "message_b_latest",
    expected_governance_attestation_sha256: "a".repeat(64),
  });
});

test("hold and return strictly omit every candidate-selection field", () => {
  const artifact = governedArtifact();
  for (const action of ["hold", "return"]) {
    const payload = buildArtifactUserDecisionRequest(artifact, {
      action,
      rationale: "当前先不支持任何候选。",
      selectedOptionId: "candidate_b",
    });
    assert.deepEqual(Object.keys(payload).sort(), ["action", "expected_version", "rationale"]);
    assert.equal(Object.hasOwn(payload, "selected_option_id"), false);
    assert.equal(Object.hasOwn(payload, "expected_candidate_revision"), false);
    assert.equal(Object.hasOwn(payload, "expected_candidate_origin_message_id"), false);
    assert.equal(Object.hasOwn(payload, "expected_candidate_latest_message_id"), false);
    assert.equal(Object.hasOwn(payload, "expected_governance_attestation_sha256"), false);
  }
});

test("explicit non-applicable governance allows dispositions without inventing tokens", () => {
  const artifact = explicitNonGovernedArtifact();
  assert.equal(artifactUserDecisionGate(artifact).ready, true);
  const selection = artifactUserDecisionSelection(artifact);
  assert.equal(selection.ready, true);
  assert.equal(selection.governed, false);
  assert.deepEqual(selection.candidates.map((candidate) => candidate.id), [
    "candidate_a",
    "candidate_b",
  ]);

  assert.deepEqual(buildArtifactUserDecisionRequest(artifact, {
    action: "support",
    rationale: "明确支持候选 B，但不伪造治理证明。",
    selectedOptionId: "candidate_b",
  }), {
    expected_version: 4,
    action: "support",
    rationale: "明确支持候选 B，但不伪造治理证明。",
    selected_option_id: "candidate_b",
  });
  assert.deepEqual(buildArtifactUserDecisionRequest(artifact, {
    action: "hold",
    rationale: "暂时保留判断。",
    selectedOptionId: "candidate_b",
  }), {
    expected_version: 4,
    action: "hold",
    rationale: "暂时保留判断。",
  });
});

test("a governed artifact without exact risk reviews may be held but not supported", () => {
  const artifact = governedArtifact();
  artifact.governance_snapshot.projection.candidate_risk_reviews = {
    version: "candidate_risk_review_v1",
    applicable: false,
    ready: true,
    status: "not_required",
    reviews: [],
    issues: [],
  };

  const selection = artifactUserDecisionSelection(artifact);
  assert.equal(selection.decisionReady, true);
  assert.equal(selection.ready, false);
  assert.match(selection.reason, /风控复核/);
  assert.deepEqual(buildArtifactUserDecisionRequest(artifact, {
    action: "hold",
    rationale: "没有精确复核时先保留。",
  }), {
    expected_version: 4,
    action: "hold",
    rationale: "没有精确复核时先保留。",
  });
  assert.throws(
    () => buildArtifactUserDecisionRequest(artifact, {
      action: "support",
      rationale: "不得绕过精确复核。",
      selectedOptionId: "candidate_a",
    }),
    /风控复核/,
  );
});

test("fails closed on artifact drift, unknown governance, and stale exact reviews", () => {
  const versionDrift = governedArtifact();
  versionDrift.governance_snapshot.artifact.artifact_version = 3;
  assert.equal(artifactUserDecisionSelection(versionDrift).ready, false);
  assert.match(artifactUserDecisionSelection(versionDrift).reason, /版本/);

  const unknown = governedArtifact();
  delete unknown.governance_snapshot;
  assert.equal(artifactUserDecisionSelection(unknown).ready, false);
  assert.throws(
    () => buildArtifactUserDecisionRequest(unknown, {
      action: "hold",
      rationale: "治理未知时不提交。",
    }),
    /治理快照/,
  );

  const staleRisk = governedArtifact();
  staleRisk.governance_snapshot.projection.candidate_risk_reviews.reviews[1].status = "stale";
  assert.equal(artifactUserDecisionSelection(staleRisk).ready, false);
  assert.equal(artifactUserDecisionGate(staleRisk).ready, false);
  assert.throws(
    () => buildArtifactUserDecisionRequest(staleRisk, {
      action: "hold",
      rationale: "风险版本漂移时不提交。",
    }),
    /风控复核/,
  );
  assert.match(
    artifactUserDecisionSelection(staleRisk).issues.map((issue) => issue.code).join(" "),
    /CANDIDATE_RISK_REVIEW_NOT_CURRENT:candidate_b/,
  );
});

test("fractional artifact and candidate revisions fail closed instead of being rounded", () => {
  const fractionalCandidate = governedArtifact();
  fractionalCandidate.governance_snapshot.projection.decision.options[1].lineage.revision = 3.5;
  fractionalCandidate.governance_snapshot.projection.candidate_lineage.candidates[1].revision = 3.5;
  fractionalCandidate.governance_snapshot.projection.candidate_risk_reviews.reviews[1].candidate_revision = 3.5;
  fractionalCandidate.governance_snapshot.projection.candidate_risk_reviews.reviews[1].current_candidate_revision = 3.5;
  assert.equal(artifactUserDecisionSelection(fractionalCandidate).ready, false);
  assert.throws(
    () => buildArtifactUserDecisionRequest(fractionalCandidate, {
      action: "support",
      rationale: "候选版本必须是严格整数。",
      selectedOptionId: "candidate_b",
    }),
    /候选|版本|治理|风险/,
  );

  const fractionalArtifact = governedArtifact();
  fractionalArtifact.version = 4.5;
  fractionalArtifact.governance_snapshot.artifact.artifact_version = 4.5;
  assert.equal(artifactUserDecisionGate(fractionalArtifact).ready, false);
  assert.throws(
    () => buildArtifactUserDecisionRequest(fractionalArtifact, {
      action: "hold",
      rationale: "产物版本必须是严格整数。",
    }),
    /版本/,
  );
});

test("presentation never conflates AI preference with the user's explicit choice", () => {
  const artifact = governedArtifact();
  const v2 = artifactUserDecisionPresentation(artifact, {
    decision_version: ARTIFACT_USER_DECISION_VERSION,
    action: "support",
    ai_preferred_option_id: "candidate_a",
    selected_option_id: "candidate_b",
    preferred_option_id: "candidate_b",
    selected_is_ai_preferred: false,
  });
  assert.equal(v2.aiPreferredLabel, "候选 A");
  assert.equal(v2.selectedOptionLabel, "候选 B");
  assert.equal(v2.selectedIsAiPreferred, false);
  assert.equal(v2.legacySelectionUnavailable, false);

  const legacy = artifactUserDecisionPresentation(artifact, {
    decision_version: "artifact_user_decision_v1",
    action: "support",
    preferred_option_id: "candidate_a",
    selected_option_id: "candidate_a",
  });
  assert.equal(legacy.aiPreferredLabel, "候选 A");
  assert.equal(legacy.hasExplicitSelection, false);
  assert.equal(legacy.legacySelectionUnavailable, true);

  const hold = artifactUserDecisionPresentation(artifact, {
    decision_version: ARTIFACT_USER_DECISION_VERSION,
    action: "hold",
    ai_preferred_option_id: "candidate_a",
    selected_option_id: "candidate_b",
  });
  assert.equal(hold.selectedOptionId, "");
  assert.equal(hold.hasExplicitSelection, false);
});

test("dialog uses an explicit radio choice and App submits through the contract builder", () => {
  const dialogSource = readFileSync(
    new URL("../src/components/ArtifactDialog.jsx", import.meta.url),
    "utf8",
  );
  const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");

  assert.match(dialogSource, /type="radio"/);
  assert.match(dialogSource, /AI 首选/);
  assert.match(dialogSource, /我的选择/);
  assert.ok(dialogSource.includes(
    "key={JSON.stringify([renderedArtifact.id, renderedArtifact.version, editorSession])}",
  ));
  assert.match(appSource, /buildArtifactUserDecisionRequest\(artifact/);
});
