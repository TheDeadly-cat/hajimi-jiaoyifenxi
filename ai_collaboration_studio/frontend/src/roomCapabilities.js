export const ROOM_CAPABILITIES = Object.freeze({
  auditableResponseGraph: "discussion.turn_contract_v1",
  storageMarket: "market.storage.readonly",
  storageAnalytics: "analytics.storage",
  observations: "simulation.observations",
  paperPortfolio: "simulation.paper_portfolio",
  observationProposals: "decision.observation_proposals",
  projectEvidenceMap: "research.project.evidence_map",
  projectOptionMatrix: "research.project.option_matrix",
  projectRiskRegister: "research.project.risk_register",
  projectRecommendation: "decision.project_recommendation",
});

const CORE_CAPABILITIES = new Set([
  "collaboration.chat",
  "materials.shared",
  "artifacts.meeting",
  ROOM_CAPABILITIES.auditableResponseGraph,
]);

export const CORE_DISCUSSION_PROTOCOL_PACK_ID = "structured_turn_contract_v1";

export function isCoreDiscussionProtocolPack(pack) {
  return Boolean(
    pack
    && (
      pack.system_managed === true
      || pack.scope === "formal_round_core"
      || pack.id === CORE_DISCUSSION_PROTOCOL_PACK_ID
    )
  );
}

export function splitCapabilityPacks(capabilityPacks) {
  const coreProtocols = [];
  const optionalDomainPacks = [];
  for (const pack of Array.isArray(capabilityPacks) ? capabilityPacks : []) {
    (isCoreDiscussionProtocolPack(pack) ? coreProtocols : optionalDomainPacks).push(pack);
  }
  return { coreProtocols, optionalDomainPacks };
}

const CAPABILITY_LABELS = Object.freeze({
  [ROOM_CAPABILITIES.storageMarket]: "Futu 只读行情",
  [ROOM_CAPABILITIES.storageAnalytics]: "存储产业分析",
  [ROOM_CAPABILITIES.observations]: "模拟观察验证",
  [ROOM_CAPABILITIES.paperPortfolio]: "模拟组合风控",
  [ROOM_CAPABILITIES.observationProposals]: "结构化观察提案",
  [ROOM_CAPABILITIES.projectEvidenceMap]: "需求证据地图",
  [ROOM_CAPABILITIES.projectOptionMatrix]: "候选方案矩阵",
  [ROOM_CAPABILITIES.projectRiskRegister]: "项目风险登记",
  [ROOM_CAPABILITIES.projectRecommendation]: "条件化项目建议",
});

export function hasRoomCapability(room, capability) {
  return Array.isArray(room?.capabilities) && room.capabilities.includes(capability);
}

export function capabilitiesForPackSelection(packIds, capabilityPacks) {
  const selected = new Set(packIds || []);
  return [...new Set((capabilityPacks || []).flatMap((pack) => (
    selected.has(pack.id) ? (pack.capabilities || []) : []
  )))];
}

export function roomDomainCapabilityLabels(room) {
  const labels = (room?.capabilities || [])
    .filter((capability) => !CORE_CAPABILITIES.has(capability))
    .map((capability) => CAPABILITY_LABELS[capability] || capability);
  return labels.length ? labels : ["通用协作"];
}
