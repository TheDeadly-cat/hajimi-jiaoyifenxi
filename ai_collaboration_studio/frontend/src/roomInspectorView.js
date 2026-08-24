export const ROOM_INSPECTOR_MEMBER_LIMIT = 500;
export const ROOM_INSPECTOR_PROVIDER_LIMIT = 500;
export const ROOM_INSPECTOR_ARCHIVED_MEMBER_LIMIT = 500;
export const ROOM_INSPECTOR_MATERIAL_LIMIT = 1000;
export const ROOM_INSPECTOR_DIRECTOR_DECISION_LIMIT = 1000;
export const ROOM_INSPECTOR_ARTIFACT_LIMIT = 2000;
export const ROOM_INSPECTOR_WALK_FORWARD_BUCKET_LIMIT = 2000;
export const ROOM_INSPECTOR_WORKFLOW_STAGE_LIMIT = 20;
export const ROOM_INSPECTOR_COVERAGE_LIMIT = 200;
export const ROOM_INSPECTOR_INITIAL_MEMBER_LIMIT = 12;
export const ROOM_INSPECTOR_INITIAL_ARCHIVED_MEMBER_LIMIT = 12;
export const ROOM_INSPECTOR_INITIAL_MATERIAL_LIMIT = 20;
export const ROOM_INSPECTOR_MEMBER_STEP = 12;
export const ROOM_INSPECTOR_ARCHIVED_MEMBER_STEP = 12;
export const ROOM_INSPECTOR_MATERIAL_STEP = 20;

function record(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function array(value) {
  return Array.isArray(value) ? value : [];
}

export function safeRoomInspectorText(value, maxLength = 1000) {
  if (typeof value !== "string") return "";
  const safeLimit = Number.isSafeInteger(maxLength) && maxLength > 0
    ? Math.min(maxLength, 100000)
    : 1000;
  return value.slice(0, safeLimit).trim();
}

export function safeRoomInspectorColor(value, fallback = "#4f6b8a") {
  const color = safeRoomInspectorText(value, 32);
  return /^#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(color)
    ? color
    : fallback;
}

function projectRecords(value, visibleLimit, hardLimit) {
  const source = array(value);
  const boundedRows = source
    .slice(0, hardLimit)
    .filter((item) => record(item) === item);
  const safeVisibleLimit = Number.isSafeInteger(visibleLimit) && visibleLimit > 0
    ? Math.min(visibleLimit, hardLimit)
    : Math.min(hardLimit, 1);
  const visibleRows = boundedRows.slice(0, safeVisibleLimit);
  return {
    boundedRows,
    visibleRows,
    sourceCount: source.length,
    boundedCount: boundedRows.length,
    visibleCount: visibleRows.length,
    moreAvailable: visibleRows.length < boundedRows.length,
    hardLimited: source.length > hardLimit,
    hardOmittedCount: Math.max(0, source.length - hardLimit),
  };
}

export function buildRoomInspectorListProjection({
  members,
  archivedMembers,
  materials,
  memberLimit = ROOM_INSPECTOR_INITIAL_MEMBER_LIMIT,
  archivedMemberLimit = ROOM_INSPECTOR_INITIAL_ARCHIVED_MEMBER_LIMIT,
  materialLimit = ROOM_INSPECTOR_INITIAL_MATERIAL_LIMIT,
} = {}) {
  return {
    members: projectRecords(members, memberLimit, ROOM_INSPECTOR_MEMBER_LIMIT),
    archivedMembers: projectRecords(
      archivedMembers,
      archivedMemberLimit,
      ROOM_INSPECTOR_ARCHIVED_MEMBER_LIMIT,
    ),
    materials: projectRecords(materials, materialLimit, ROOM_INSPECTOR_MATERIAL_LIMIT),
  };
}

export function buildRoomInspectorProviderIndex(value) {
  const source = array(value);
  const rows = source
    .slice(0, ROOM_INSPECTOR_PROVIDER_LIMIT)
    .filter((item) => record(item) === item);
  const providerMap = new Map();
  for (const provider of rows) {
    const id = safeRoomInspectorText(provider.id, 160).toLowerCase();
    if (id && !providerMap.has(id)) providerMap.set(id, provider);
  }
  return {
    rows,
    providerMap,
    sourceCount: source.length,
    indexedCount: providerMap.size,
    projectionLimited: source.length > ROOM_INSPECTOR_PROVIDER_LIMIT,
    omittedCount: Math.max(0, source.length - ROOM_INSPECTOR_PROVIDER_LIMIT),
  };
}

function decisionTimestamp(value) {
  const candidate = safeRoomInspectorText(value, 80);
  if (!candidate) return 0;
  const timestamp = new Date(candidate).getTime();
  return Number.isFinite(timestamp) ? timestamp : 0;
}

export function buildCurrentRoundDirectorDecisions(value, currentRoundId = "") {
  const source = array(value);
  const boundedRows = source.length > ROOM_INSPECTOR_DIRECTOR_DECISION_LIMIT
    ? source.slice(-ROOM_INSPECTOR_DIRECTOR_DECISION_LIMIT)
    : source;
  const safeRoundId = safeRoomInspectorText(currentRoundId, 240);
  const rows = boundedRows
    .filter((decision) => {
      if (record(decision) !== decision) return false;
      return !safeRoundId || safeRoomInspectorText(decision.round_id, 240) === safeRoundId;
    })
    .slice()
    .sort((left, right) => (
      decisionTimestamp(left.created_at) - decisionTimestamp(right.created_at)
      || (Number(left.sequence_no) || 0) - (Number(right.sequence_no) || 0)
      || safeRoomInspectorText(left.id, 240).localeCompare(
        safeRoomInspectorText(right.id, 240),
      )
    ));
  return {
    rows,
    sourceCount: source.length,
    inspectedCount: boundedRows.length,
    projectionLimited: source.length > ROOM_INSPECTOR_DIRECTOR_DECISION_LIMIT,
    omittedCount: Math.max(0, source.length - ROOM_INSPECTOR_DIRECTOR_DECISION_LIMIT),
  };
}

export function buildRoomInspectorArtifactFingerprint(value) {
  const source = array(value);
  const boundedRows = source.length > ROOM_INSPECTOR_ARTIFACT_LIMIT
    ? source.slice(-ROOM_INSPECTOR_ARTIFACT_LIMIT)
    : source;
  const entries = boundedRows.map((rawArtifact) => {
    const artifact = record(rawArtifact);
    const tuple = [
      safeRoomInspectorText(artifact.id, 240),
      Number.isSafeInteger(Number(artifact.version)) ? Number(artifact.version) : 0,
      safeRoomInspectorText(artifact.status, 80),
      array(record(artifact.content).actions).length,
    ];
    return { key: JSON.stringify(tuple), tuple };
  });
  entries.sort((left, right) => left.key.localeCompare(right.key));
  return {
    fingerprint: JSON.stringify(entries.map((entry) => entry.tuple)),
    sourceCount: source.length,
    projectedCount: boundedRows.length,
    projectionLimited: source.length > ROOM_INSPECTOR_ARTIFACT_LIMIT,
    omittedCount: Math.max(0, source.length - ROOM_INSPECTOR_ARTIFACT_LIMIT),
  };
}

export function inspectWalkForwardFootprint(value) {
  const source = record(value);
  let inspectedCount = 0;
  for (const key in source) {
    if (!Object.prototype.hasOwnProperty.call(source, key)) continue;
    if (inspectedCount >= ROOM_INSPECTOR_WALK_FORWARD_BUCKET_LIMIT) {
      return {
        hasHistory: true,
        confirmedHistory: false,
        inspectedCount,
        projectionLimited: true,
      };
    }
    inspectedCount += 1;
    if (array(source[key]).length > 0) {
      return {
        hasHistory: true,
        confirmedHistory: true,
        inspectedCount,
        projectionLimited: false,
      };
    }
  }
  return {
    hasHistory: false,
    confirmedHistory: false,
    inspectedCount,
    projectionLimited: false,
  };
}

export function buildRoomInspectorWorkflowProjection(value) {
  const workflowPolicy = record(value);
  const rawStages = array(workflowPolicy.stage_order);
  const rawCoverage = array(workflowPolicy.required_coverage);
  const stageOrder = rawStages
    .slice(0, ROOM_INSPECTOR_WORKFLOW_STAGE_LIMIT)
    .map((stage) => safeRoomInspectorText(stage, 80))
    .filter(Boolean);
  const requiredCoverage = rawCoverage
    .slice(0, ROOM_INSPECTOR_COVERAGE_LIMIT)
    .flatMap((rawItem) => {
      const item = record(rawItem);
      if (item !== rawItem) return [];
      const label = safeRoomInspectorText(item.label, 240);
      return label ? [{ ...item, label }] : [];
    });
  return {
    workflowPolicy: workflowPolicy === value ? workflowPolicy : null,
    stageOrder,
    requiredCoverage,
    stageProjectionLimited: rawStages.length > ROOM_INSPECTOR_WORKFLOW_STAGE_LIMIT,
    coverageProjectionLimited: rawCoverage.length > ROOM_INSPECTOR_COVERAGE_LIMIT,
  };
}

export function roomInspectorErrorMessage(error, fallback = "检查面板操作失败。") {
  return safeRoomInspectorText(error?.message, 1000)
    || safeRoomInspectorText(error, 1000)
    || fallback;
}
