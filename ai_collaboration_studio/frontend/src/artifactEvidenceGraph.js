export const ARTIFACT_EVIDENCE_GRAPH_VERSION = "artifact_evidence_graph_v2";
export const ARTIFACT_EVIDENCE_REVIEW_CHAIN_VERSION = "artifact_evidence_review_chain_v1";

const NODE_KINDS = new Set([
  "artifact_version",
  "evidence_source",
  "summary",
  "requirement",
  "risk",
  "conclusion",
  "disagreement",
  "unknown",
  "action",
  "decision",
  "candidate",
  "user_decision",
]);

const EDGE_TYPES = new Set([
  "supports",
  "counters",
  "context_for",
  "part_of",
  "decides_on",
  "selects",
]);

const RELATION_EDGE_TYPES = new Set(["supports", "counters", "context_for"]);
const TARGET_NODE_KINDS = new Set([
  "summary", "requirement", "risk", "conclusion", "disagreement", "unknown",
  "action", "decision", "candidate",
]);
const EVIDENCE_ROLES = new Set(["support", "counter", "context"]);
const VERIFICATION_STATUSES = new Set([
  "unreviewed",
  "source_checked",
  "corroborated",
  "disputed",
]);

function cleanText(value, maxLength = 1000) {
  return String(value ?? "").trim().slice(0, maxLength);
}

function cleanInteger(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : fallback;
}

function invalidGraph(issues, raw = {}) {
  return {
    valid: false,
    stale: issues.some((issue) => issue.startsWith("EXPECTED_")),
    issues: [...new Set(issues)],
    artifact: raw?.artifact && typeof raw.artifact === "object" ? raw.artifact : {},
    nodes: [],
    edges: [],
    relationPaths: [],
    reviewEvents: [],
    summary: {},
    integrity: {},
  };
}

export function normalizeArtifactEvidenceGraph(rawPayload, expected = {}) {
  const raw = rawPayload && typeof rawPayload === "object" ? rawPayload : {};
  const issues = [];
  if (raw.version !== ARTIFACT_EVIDENCE_GRAPH_VERSION) issues.push("GRAPH_VERSION_INVALID");
  if (raw.authoritative !== true) issues.push("GRAPH_NOT_AUTHORITATIVE");
  if (raw.execution_capability !== "none") issues.push("EXECUTION_BOUNDARY_INVALID");
  if (raw.live_trading_allowed !== false) issues.push("LIVE_TRADING_BOUNDARY_INVALID");
  if (raw.can_autonomously_decide !== false) issues.push("DECISION_BOUNDARY_INVALID");
  if (expected.roomId && cleanText(raw.room_id, 240) !== cleanText(expected.roomId, 240)) {
    issues.push("EXPECTED_ROOM_MISMATCH");
  }
  const artifact = raw.artifact && typeof raw.artifact === "object" ? raw.artifact : {};
  if (expected.artifactId && cleanText(artifact.id, 240) !== cleanText(expected.artifactId, 240)) {
    issues.push("EXPECTED_ARTIFACT_MISMATCH");
  }
  if (
    Number.isInteger(Number(expected.artifactVersion))
    && Number(expected.artifactVersion) > 0
    && cleanInteger(artifact.version) !== Number(expected.artifactVersion)
  ) {
    issues.push("EXPECTED_ARTIFACT_VERSION_MISMATCH");
  }
  if (
    Object.hasOwn(expected, "roundId")
    && cleanText(raw.round_id, 240) !== cleanText(expected.roundId, 240)
  ) {
    issues.push("EXPECTED_ROUND_MISMATCH");
  }

  const integrity = raw.integrity && typeof raw.integrity === "object" ? raw.integrity : {};
  if (!["verified", "partial", "legacy_untracked"].includes(integrity.status)) {
    issues.push("GRAPH_INTEGRITY_INVALID");
  }
  if (integrity.current_projection_matches !== true) issues.push("CURRENT_PROJECTION_INVALID");
  if (integrity.review_chain_verified !== true) issues.push("REVIEW_CHAIN_INVALID");
  if (!/^[0-9a-f]{64}$/i.test(cleanText(integrity.graph_sha256, 64))) {
    issues.push("GRAPH_HASH_INVALID");
  }

  const rawNodes = Array.isArray(raw.nodes) ? raw.nodes : [];
  const rawEdges = Array.isArray(raw.edges) ? raw.edges : [];
  if (rawNodes.length > 7500) issues.push("NODE_LIMIT_EXCEEDED");
  if (rawEdges.length > 13000) issues.push("EDGE_LIMIT_EXCEEDED");
  const nodes = [];
  const nodeById = new Map();
  rawNodes.forEach((rawNode) => {
    const node = rawNode && typeof rawNode === "object" ? rawNode : {};
    const nodeId = cleanText(node.node_id, 120);
    const nodeKind = cleanText(node.node_kind, 60);
    if (!nodeId || nodeById.has(nodeId)) {
      issues.push("NODE_ID_INVALID");
      return;
    }
    if (!NODE_KINDS.has(nodeKind)) {
      issues.push("NODE_KIND_INVALID");
      return;
    }
    const normalized = {
      ...node,
      node_id: nodeId,
      node_kind: nodeKind,
      label: cleanText(node.label, 240),
      text: cleanText(node.text, 1000),
    };
    nodes.push(normalized);
    nodeById.set(nodeId, normalized);
  });

  const edges = [];
  const edgeById = new Map();
  const relationIds = new Set();
  rawEdges.forEach((rawEdge) => {
    const edge = rawEdge && typeof rawEdge === "object" ? rawEdge : {};
    const edgeId = cleanText(edge.edge_id, 120);
    const edgeType = cleanText(edge.edge_type, 60);
    const fromNodeId = cleanText(edge.from_node_id, 120);
    const toNodeId = cleanText(edge.to_node_id, 120);
    if (!edgeId || edgeById.has(edgeId)) {
      issues.push("EDGE_ID_INVALID");
      return;
    }
    if (!EDGE_TYPES.has(edgeType)) {
      issues.push("EDGE_TYPE_INVALID");
      return;
    }
    if (!nodeById.has(fromNodeId) || !nodeById.has(toNodeId)) {
      issues.push("EDGE_ENDPOINT_INVALID");
      return;
    }
    const fromNode = nodeById.get(fromNodeId);
    const toNode = nodeById.get(toNodeId);
    const semanticEndpointsValid = RELATION_EDGE_TYPES.has(edgeType)
      ? fromNode.node_kind === "evidence_source" && TARGET_NODE_KINDS.has(toNode.node_kind)
      : edgeType === "part_of"
        ? TARGET_NODE_KINDS.has(fromNode.node_kind) && toNode.node_kind === "artifact_version"
        : edgeType === "decides_on"
          ? fromNode.node_kind === "user_decision" && toNode.node_kind === "artifact_version"
          : edgeType === "selects"
            ? fromNode.node_kind === "user_decision" && toNode.node_kind === "candidate"
            : false;
    if (!semanticEndpointsValid) {
      issues.push("EDGE_SEMANTICS_INVALID");
      return;
    }
    if (RELATION_EDGE_TYPES.has(edgeType)) {
      const relationId = cleanText(edge.relation_id, 120);
      if (!relationId || relationIds.has(relationId) || !cleanText(edge.item_key, 180)) {
        issues.push("RELATION_ID_INVALID");
        return;
      }
      relationIds.add(relationId);
      if (!EVIDENCE_ROLES.has(cleanText(edge.evidence_role, 40))) {
        issues.push("RELATION_ROLE_INVALID");
        return;
      }
      if (!VERIFICATION_STATUSES.has(cleanText(edge.verification_status, 40))) {
        issues.push("RELATION_STATUS_INVALID");
        return;
      }
    }
    const normalized = {
      ...edge,
      edge_id: edgeId,
      edge_type: edgeType,
      from_node_id: fromNodeId,
      to_node_id: toNodeId,
    };
    edges.push(normalized);
    edgeById.set(edgeId, normalized);
  });

  const chain = raw.review_chain && typeof raw.review_chain === "object" ? raw.review_chain : {};
  if (chain.version !== ARTIFACT_EVIDENCE_REVIEW_CHAIN_VERSION) {
    issues.push("REVIEW_CHAIN_VERSION_INVALID");
  }
  const rawEvents = Array.isArray(chain.events) ? chain.events : [];
  if (rawEvents.length > 500) issues.push("REVIEW_EVENT_LIMIT_EXCEEDED");
  const reviewEvents = [];
  let previousArtifactVersion = 0;
  rawEvents.forEach((rawEvent, index) => {
    const event = rawEvent && typeof rawEvent === "object" ? rawEvent : {};
    const sequence = cleanInteger(event.sequence_no);
    if (sequence !== index + 1) issues.push("REVIEW_EVENT_SEQUENCE_INVALID");
    if (!/^[0-9a-f]{64}$/i.test(cleanText(event.event_sha256, 64))) {
      issues.push("REVIEW_EVENT_HASH_INVALID");
    }
    const eventArtifactVersion = cleanInteger(event.artifact_version);
    if (eventArtifactVersion <= previousArtifactVersion) {
      issues.push("REVIEW_EVENT_VERSION_INVALID");
    }
    previousArtifactVersion = eventArtifactVersion;
    reviewEvents.push({
      ...event,
      sequence_no: sequence,
      artifact_version: eventArtifactVersion,
      created_at: cleanInteger(event.created_at),
    });
  });
  if (chain.verified !== true) issues.push("REVIEW_CHAIN_UNVERIFIED");
  if (cleanInteger(chain.event_count) !== reviewEvents.length) {
    issues.push("REVIEW_EVENT_COUNT_INVALID");
  }
  if (reviewEvents.length) {
    const latest = reviewEvents.at(-1);
    if (cleanInteger(chain.head_sequence) !== latest.sequence_no) {
      issues.push("REVIEW_HEAD_SEQUENCE_INVALID");
    }
    if (cleanText(chain.head_sha256, 64) !== cleanText(latest.event_sha256, 64)) {
      issues.push("REVIEW_HEAD_HASH_INVALID");
    }
  } else if (cleanInteger(chain.head_sequence) !== 0 || cleanText(chain.head_sha256, 64)) {
    issues.push("REVIEW_EMPTY_HEAD_INVALID");
  }
  const legacyUntrackedVersionCount = cleanInteger(chain.legacy_untracked_version_count);
  if (integrity.status === "verified" && legacyUntrackedVersionCount > 0) {
    issues.push("LEGACY_GAP_MISCLASSIFIED");
  }

  if (issues.length) return invalidGraph(issues, raw);

  const outgoingByNode = new Map();
  const incomingByNode = new Map();
  edges.forEach((edge) => {
    const outgoing = outgoingByNode.get(edge.from_node_id) || [];
    outgoing.push(edge);
    outgoingByNode.set(edge.from_node_id, outgoing);
    const incoming = incomingByNode.get(edge.to_node_id) || [];
    incoming.push(edge);
    incomingByNode.set(edge.to_node_id, incoming);
  });
  const relationPaths = edges.filter((edge) => RELATION_EDGE_TYPES.has(edge.edge_type)).map((edge) => {
    const source = nodeById.get(edge.from_node_id);
    const target = nodeById.get(edge.to_node_id);
    const downstreamEdges = [
      ...(outgoingByNode.get(target.node_id) || []).filter((item) => item.edge_type === "part_of"),
      ...(incomingByNode.get(target.node_id) || []).filter((item) => item.edge_type === "selects"),
    ];
    const downstream = downstreamEdges.flatMap((item) => {
      const otherId = item.from_node_id === target.node_id ? item.to_node_id : item.from_node_id;
      const node = nodeById.get(otherId);
      return node ? [node] : [];
    });
    return {
      relationId: edge.relation_id,
      edge,
      source,
      target,
      downstream,
    };
  }).sort((left, right) => left.relationId.localeCompare(right.relationId));

  return {
    valid: true,
    stale: false,
    issues: [],
    roomId: cleanText(raw.room_id, 240),
    roundId: cleanText(raw.round_id, 240),
    artifact: {
      ...artifact,
      id: cleanText(artifact.id, 240),
      version: cleanInteger(artifact.version),
      title: cleanText(artifact.title, 240),
    },
    nodes,
    edges,
    nodeById,
    edgeById,
    relationPaths,
    reviewEvents,
    reviewChain: chain,
    summary: raw.summary && typeof raw.summary === "object" ? raw.summary : {},
    integrity,
  };
}

export function summarizeEvidenceGraph(graph, itemKey = "") {
  const paths = graph?.valid ? graph.relationPaths : [];
  const selected = itemKey ? paths.filter((path) => path.edge.item_key === itemKey) : paths;
  return selected.reduce((summary, path) => {
    summary.relationCount += 1;
    summary[path.edge.evidence_role] += 1;
    summary[path.edge.verification_status] += 1;
    summary.sourceIds.add(path.source.node_id);
    return summary;
  }, {
    relationCount: 0,
    support: 0,
    counter: 0,
    context: 0,
    unreviewed: 0,
    source_checked: 0,
    corroborated: 0,
    disputed: 0,
    sourceIds: new Set(),
  });
}

export function filterEvidencePaths(graph, filter = "all", activeTarget = "") {
  const paths = graph?.valid ? graph.relationPaths : [];
  if (filter === "all") return paths;
  if (filter === "reviewed") {
    return paths.filter((path) => path.edge.verification_status !== "unreviewed");
  }
  if (filter === "attention") {
    return paths.filter((path) => (
      path.edge.verification_status === "unreviewed"
      || path.edge.verification_status === "disputed"
      || path.edge.evidence_role === "counter"
    ));
  }
  if (filter === "active") {
    return paths.filter((path) => path.edge.item_key === activeTarget);
  }
  return paths.filter((path) => path.edge.item_key === filter);
}
