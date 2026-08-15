import assert from "node:assert/strict";
import test from "node:test";

import {
  filterEvidencePaths,
  normalizeArtifactEvidenceGraph,
  summarizeEvidenceGraph,
} from "../src/artifactEvidenceGraph.js";

function fixture() {
  const eventHash = "a".repeat(64);
  return {
    version: "artifact_evidence_graph_v2",
    room_id: "room_one",
    round_id: "round_one",
    artifact: { id: "artifact_one", version: 3, title: "Saved artifact", status: "DRAFT" },
    authoritative: true,
    nodes: [
      { node_id: "source_v1", node_kind: "evidence_source", source_type: "material", source_id: "material_one", source_version: 1, label: "Source v1" },
      { node_id: "source_v2", node_kind: "evidence_source", source_type: "material", source_id: "material_one", source_version: 2, label: "Source v2" },
      { node_id: "target_summary", node_kind: "summary", item_key: "summary", label: "Summary" },
      { node_id: "target_candidate", node_kind: "candidate", item_key: "decision_options:option_a", label: "Option A" },
      { node_id: "artifact_v3", node_kind: "artifact_version", artifact_version: 3, label: "Saved artifact" },
      { node_id: "decision_one", node_kind: "user_decision", action: "support", label: "support" },
    ],
    edges: [
      {
        edge_id: "edge_support",
        relation_id: "relation_support",
        edge_type: "supports",
        from_node_id: "source_v1",
        to_node_id: "target_summary",
        item_key: "summary",
        evidence_role: "support",
        verification_status: "source_checked",
        source_ref: { type: "material", id: "material_one", version: 1 },
      },
      {
        edge_id: "edge_counter",
        relation_id: "relation_counter",
        edge_type: "counters",
        from_node_id: "source_v2",
        to_node_id: "target_candidate",
        item_key: "decision_options:option_a",
        evidence_role: "counter",
        verification_status: "disputed",
        review_note: "conflicting frozen version",
        source_ref: { type: "material", id: "material_one", version: 2 },
      },
      { edge_id: "edge_part", edge_type: "part_of", from_node_id: "target_summary", to_node_id: "artifact_v3" },
      { edge_id: "edge_candidate_part", edge_type: "part_of", from_node_id: "target_candidate", to_node_id: "artifact_v3" },
      { edge_id: "edge_selects", edge_type: "selects", from_node_id: "decision_one", to_node_id: "target_candidate" },
    ],
    review_chain: {
      version: "artifact_evidence_review_chain_v1",
      verified: true,
      event_count: 1,
      head_sequence: 1,
      head_sha256: eventHash,
      legacy_untracked_version_count: 0,
      events: [{
        sequence_no: 1,
        artifact_version: 3,
        event_type: "revised",
        event_sha256: eventHash,
        created_at: 1,
      }],
    },
    integrity: {
      status: "verified",
      issues: [],
      current_projection_matches: true,
      review_chain_verified: true,
      graph_sha256: "b".repeat(64),
    },
    execution_capability: "none",
    live_trading_allowed: false,
    can_autonomously_decide: false,
  };
}

const expected = {
  roomId: "room_one",
  artifactId: "artifact_one",
  artifactVersion: 3,
  roundId: "round_one",
};

test("normalizes explicit paths without merging different frozen source versions", () => {
  const graph = normalizeArtifactEvidenceGraph(fixture(), expected);

  assert.equal(graph.valid, true);
  assert.equal(graph.relationPaths.length, 2);
  assert.equal(new Set(graph.relationPaths.map((path) => path.source.node_id)).size, 2);
  const candidatePath = graph.relationPaths.find((path) => path.target.node_kind === "candidate");
  assert.deepEqual(candidatePath.downstream.map((node) => node.node_id), ["artifact_v3", "decision_one"]);
  assert.equal(candidatePath.edge.evidence_role, "counter");
});

test("separates whole-graph and current-target statistics", () => {
  const graph = normalizeArtifactEvidenceGraph(fixture(), expected);
  const whole = summarizeEvidenceGraph(graph);
  const current = summarizeEvidenceGraph(graph, "summary");

  assert.equal(whole.relationCount, 2);
  assert.equal(whole.sourceIds.size, 2);
  assert.equal(current.relationCount, 1);
  assert.equal(current.support, 1);
  assert.equal(filterEvidencePaths(graph, "attention").length, 1);
  assert.equal(filterEvidencePaths(graph, "reviewed").length, 2);
  assert.equal(filterEvidencePaths(graph, "active", "summary").length, 1);
  assert.equal(filterEvidencePaths(graph, "active", "decision_options:option_a").length, 1);
});

test("keeps partial source or legacy integrity visible without calling it verified", () => {
  const payload = fixture();
  payload.integrity.status = "partial";
  payload.integrity.issues = ["MATERIAL_SOURCE_MISSING"];
  payload.integrity.source_integrity = {
    status: "partial",
    issues: ["MATERIAL_SOURCE_MISSING"],
    missing_source_count: 1,
  };
  payload.nodes[0].status = "missing";
  const graph = normalizeArtifactEvidenceGraph(payload, expected);
  assert.equal(graph.valid, true);
  assert.equal(graph.integrity.status, "partial");

  const misclassified = fixture();
  misclassified.review_chain.legacy_untracked_version_count = 2;
  const invalid = normalizeArtifactEvidenceGraph(misclassified, expected);
  assert.equal(invalid.valid, false);
  assert.ok(invalid.issues.includes("LEGACY_GAP_MISCLASSIFIED"));
});

test("fails closed on a stale artifact identity", () => {
  const graph = normalizeArtifactEvidenceGraph(fixture(), { ...expected, artifactVersion: 4 });

  assert.equal(graph.valid, false);
  assert.equal(graph.stale, true);
  assert.ok(graph.issues.includes("EXPECTED_ARTIFACT_VERSION_MISMATCH"));
});

test("fails closed on dangling or invented semantic edges", () => {
  const dangling = fixture();
  dangling.edges[0] = { ...dangling.edges[0], to_node_id: "missing_target" };
  const danglingGraph = normalizeArtifactEvidenceGraph(dangling, expected);
  assert.equal(danglingGraph.valid, false);
  assert.ok(danglingGraph.issues.includes("EDGE_ENDPOINT_INVALID"));

  const invented = fixture();
  invented.edges[0] = { ...invented.edges[0], edge_type: "implies_profit" };
  const inventedGraph = normalizeArtifactEvidenceGraph(invented, expected);
  assert.equal(inventedGraph.valid, false);
  assert.ok(inventedGraph.issues.includes("EDGE_TYPE_INVALID"));

  const wrongSemantics = fixture();
  wrongSemantics.edges[0] = {
    ...wrongSemantics.edges[0],
    from_node_id: "decision_one",
  };
  const semanticGraph = normalizeArtifactEvidenceGraph(wrongSemantics, expected);
  assert.equal(semanticGraph.valid, false);
  assert.ok(semanticGraph.issues.includes("EDGE_SEMANTICS_INVALID"));
});

test("fails closed on review-chain gaps", () => {
  const payload = fixture();
  payload.review_chain.events[0].sequence_no = 2;
  payload.review_chain.head_sequence = 2;
  const graph = normalizeArtifactEvidenceGraph(payload, expected);

  assert.equal(graph.valid, false);
  assert.ok(graph.issues.includes("REVIEW_EVENT_SEQUENCE_INVALID"));
});
