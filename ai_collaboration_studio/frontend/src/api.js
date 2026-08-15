let sessionToken = "";

async function jsonRequest(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(sessionToken ? { "X-AI-Studio-Token": sessionToken } : {}),
      ...(options.headers || {}),
    },
  });
  const data = await response.json();
  if (typeof data.session_token === "string" && data.session_token) {
    sessionToken = data.session_token;
  }
  if (!response.ok || data.ok === false) {
    const requestError = new Error(data.error || `请求失败：${response.status}`);
    requestError.code = typeof data.code === "string" ? data.code : "";
    requestError.status = response.status;
    requestError.details = data.diagnostic || data.details || null;
    throw requestError;
  }
  return data;
}

export const api = {
  bootstrap: (roomId = "") => jsonRequest(`/api/bootstrap${roomId ? `?room=${encodeURIComponent(roomId)}` : ""}`),
  room: (roomId) => jsonRequest(`/api/rooms/${encodeURIComponent(roomId)}`),
  roomPluginRegistry: (roomId) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/plugin-registry`,
  ),
  pluginLifecycle: () => jsonRequest("/api/plugin-registry/lifecycle"),
  previewPluginLifecycle: (payload, signal) => jsonRequest(
    "/api/plugin-registry/lifecycle-events/preview",
    { method: "POST", body: JSON.stringify(payload), signal },
  ),
  transitionPluginLifecycle: (payload, signal) => jsonRequest(
    "/api/plugin-registry/lifecycle-events",
    { method: "POST", body: JSON.stringify(payload), signal },
  ),
  updateRoom: (roomId, payload) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}`,
    { method: "PATCH", body: JSON.stringify(payload) },
  ),
  roomVersions: (roomId) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/versions`,
  ),
  roomVersion: (roomId, version) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/versions/${encodeURIComponent(version)}`,
  ),
  convergence: (roomId) => jsonRequest(`/api/rooms/${encodeURIComponent(roomId)}/convergence`),
  storageSampleAcceptance: (roomId) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/storage-sample-acceptance`,
  ),
  createRoom: (payload) => jsonRequest("/api/rooms", { method: "POST", body: JSON.stringify(payload) }),
  sendMessage: (roomId, content) => jsonRequest(`/api/rooms/${encodeURIComponent(roomId)}/messages`, {
    method: "POST",
    body: JSON.stringify({ content, skip_providers: ["openai"] }),
  }),
  messageHistory: (roomId, { limit = 30, before = "", query = "" } = {}) => {
    const parameters = new URLSearchParams({ limit: String(limit) });
    if (before) parameters.set("before", before);
    if (query) parameters.set("q", query);
    return jsonRequest(
      `/api/rooms/${encodeURIComponent(roomId)}/messages?${parameters.toString()}`,
    );
  },
  updateMember: (roomId, memberId, payload) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/members/${encodeURIComponent(memberId)}`,
    { method: "PATCH", body: JSON.stringify(payload) },
  ),
  addMember: (roomId, payload) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/members`,
    { method: "POST", body: JSON.stringify(payload) },
  ),
  archiveMember: (roomId, memberId, expectedVersion) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/members/${encodeURIComponent(memberId)}`,
    { method: "DELETE", body: JSON.stringify({ expected_version: expectedVersion }) },
  ),
  restoreMember: (roomId, memberId, expectedVersion) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/members/${encodeURIComponent(memberId)}/restore`,
    { method: "POST", body: JSON.stringify({ expected_version: expectedVersion }) },
  ),
  memberVersions: (roomId, memberId) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/members/${encodeURIComponent(memberId)}/versions`,
  ),
  memberVersion: (roomId, memberId, version) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/members/${encodeURIComponent(memberId)}/versions/${encodeURIComponent(version)}`,
  ),
  reorderMembers: (roomId, memberIds, expectedMemberIds) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/members/reorder`,
    { method: "POST", body: JSON.stringify({ member_ids: memberIds, expected_member_ids: expectedMemberIds }) },
  ),
  preflightProviders: (roomId, payload = { skip_providers: ["openai"] }) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/providers/preflight`,
    { method: "POST", body: JSON.stringify(payload) },
  ),
  roundLaunchPlan: (roomId, payload, signal) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/round-launch-plan`,
    { method: "POST", body: JSON.stringify(payload), signal },
  ),
  storageSnapshot: (force = false, signal) => jsonRequest(
    `/api/market/storage/snapshot${force ? "?force=1" : ""}`,
    { signal },
  ),
  storageStatus: (signal) => jsonRequest("/api/market/futu/status", { signal }),
  storageReadiness: (force = false, signal, roomId = "") => {
    const parameters = new URLSearchParams();
    if (force) parameters.set("force", "1");
    if (roomId) parameters.set("room", roomId);
    const query = parameters.toString();
    return jsonRequest(`/api/market/storage/readiness${query ? `?${query}` : ""}`, { signal });
  },
  addMaterial: (roomId, payload) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/materials`,
    { method: "POST", body: JSON.stringify(payload) },
  ),
  updateMaterial: (roomId, materialId, payload) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/materials/${encodeURIComponent(materialId)}`,
    { method: "PATCH", body: JSON.stringify(payload) },
  ),
  importMaterialFile: (roomId, payload) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/materials/import-file`,
    { method: "POST", body: JSON.stringify(payload) },
  ),
  confirmOfficialAttestation: (roomId, materialId, payload) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/materials/${encodeURIComponent(materialId)}/official-attestation/confirm`,
    { method: "POST", body: JSON.stringify(payload) },
  ),
  fetchMaterialUrl: (roomId, payload) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/materials/fetch-url`,
    { method: "POST", body: JSON.stringify(payload) },
  ),
  freezeOfficialEvidence: (roomId, payload) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/materials/freeze-official-evidence`,
    { method: "POST", body: JSON.stringify(payload) },
  ),
  materialVersions: (roomId, materialId) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/materials/${encodeURIComponent(materialId)}/versions`,
  ),
  materialVersion: (roomId, materialId, version) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/materials/${encodeURIComponent(materialId)}/versions/${encodeURIComponent(version)}`,
  ),
  artifactEvidenceSources: (roomId, artifactId, signal) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/artifacts/${encodeURIComponent(artifactId)}/evidence-sources`,
    { signal },
  ),
  artifactEvidenceGraph: (roomId, artifactId, signal) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/artifacts/${encodeURIComponent(artifactId)}/evidence-graph`,
    { signal },
  ),
  projectReadiness: (roomId, artifactId, artifactVersion, signal) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/artifacts/${encodeURIComponent(artifactId)}/versions/${encodeURIComponent(artifactVersion)}/project-readiness`,
    { signal },
  ),
  projectRoundFocus: (roomId, signal) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/project-round-focus`,
    { signal },
  ),
  projectRoundFocusRecord: (roomId, roundId, signal) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/rounds/${encodeURIComponent(roundId)}/project-round-focus`,
    { signal },
  ),
  inspectFootballResearch: (roomId, payload, signal) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/football-research/inspect`,
    { method: "POST", body: JSON.stringify({ payload }), signal },
  ),
  inspectStockResearch: (roomId, payload, signal) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/stock-research/inspect`,
    { method: "POST", body: JSON.stringify({ payload }), signal },
  ),
  actionDesk: (roomId, signal) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/action-desk`,
    { signal },
  ),
  actionDeskOverview: (signal) => jsonRequest(
    "/api/action-desk/overview",
    { signal },
  ),
  transitionActionDesk: (roomId, payload, signal) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/action-desk/transitions`,
    { method: "POST", body: JSON.stringify(payload), signal },
  ),
  actionDeskContinuations: (roomId, signal) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/action-desk/continuations`,
    { signal },
  ),
  continueActionDesk: (roomId, payload, signal) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/action-desk/continuations`,
    { method: "POST", body: JSON.stringify(payload), signal },
  ),
  artifactEvidenceSourceDetail: (roomId, artifactId, sourceType, sourceId) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/artifacts/${encodeURIComponent(artifactId)}/evidence-sources/${encodeURIComponent(sourceType)}/${encodeURIComponent(sourceId)}`,
  ),
  artifactVersions: (roomId, artifactId) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/artifacts/${encodeURIComponent(artifactId)}/versions`,
  ),
  artifactVersion: (roomId, artifactId, version) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/artifacts/${encodeURIComponent(artifactId)}/versions/${encodeURIComponent(version)}`,
  ),
  createObservation: (roomId, payload) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/observations`,
    { method: "POST", body: JSON.stringify(payload) },
  ),
  confirmObservation: (roomId, observationId) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/observations/${encodeURIComponent(observationId)}/confirm`,
    { method: "POST", body: JSON.stringify({}) },
  ),
  bindObservationDecisionLineage: (roomId, observationId, payload) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/observations/${encodeURIComponent(observationId)}/decision-lineage`,
    { method: "POST", body: JSON.stringify(payload) },
  ),
  reconcileObservations: (roomId) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/observations/reconcile`,
    { method: "POST", body: JSON.stringify({}) },
  ),
  createPaperPortfolio: (roomId, payload) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/paper-portfolios`,
    { method: "POST", body: JSON.stringify(payload) },
  ),
  updatePaperPortfolio: (roomId, portfolioId, payload) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/paper-portfolios/${encodeURIComponent(portfolioId)}`,
    { method: "PATCH", body: JSON.stringify(payload) },
  ),
  evaluatePaperPortfolio: (roomId, portfolioId, expectedVersion) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/paper-portfolios/${encodeURIComponent(portfolioId)}/evaluate`,
    { method: "POST", body: JSON.stringify({ expected_version: expectedVersion }) },
  ),
  confirmPaperPortfolio: (roomId, portfolioId, expectedVersion) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/paper-portfolios/${encodeURIComponent(portfolioId)}/confirm`,
    { method: "POST", body: JSON.stringify({ expected_version: expectedVersion }) },
  ),
  paperPortfolioVersions: (roomId, portfolioId) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/paper-portfolios/${encodeURIComponent(portfolioId)}/versions`,
  ),
  paperPortfolioWalkForwardRuns: (roomId, portfolioId) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/paper-portfolios/${encodeURIComponent(portfolioId)}/walk-forward`,
  ),
  runPaperPortfolioWalkForward: (roomId, portfolioId, payload) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/paper-portfolios/${encodeURIComponent(portfolioId)}/walk-forward`,
    { method: "POST", body: JSON.stringify(payload) },
  ),
  createCandidateExperiment: (roomId, payload, signal) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/candidate-experiments`,
    { method: "POST", body: JSON.stringify(payload), signal },
  ),
  candidateExperiment: (roomId, cohortId, signal) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/candidate-experiments/${encodeURIComponent(cohortId)}`,
    { signal },
  ),
  previewCandidateComparison: (roomId, payload, signal) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/candidate-comparisons/preview`,
    { method: "POST", body: JSON.stringify(payload), signal },
  ),
  updateReflection: (roomId, observationId, payload) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/observations/${encodeURIComponent(observationId)}/reflection`,
    { method: "PATCH", body: JSON.stringify(payload) },
  ),
  confirmReflection: (roomId, observationId, expectedVersion) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/observations/${encodeURIComponent(observationId)}/reflection/confirm`,
    { method: "POST", body: JSON.stringify({ expected_version: expectedVersion }) },
  ),
  generateArtifact: (roomId, roundId = "", synthesizerMemberId = "") => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/artifacts/generate`,
    {
      method: "POST",
      body: JSON.stringify({
        round_id: roundId,
        synthesizer_member_id: synthesizerMemberId,
        skip_providers: ["openai"],
      }),
    },
  ),
  updateArtifact: (roomId, artifactId, payload) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/artifacts/${encodeURIComponent(artifactId)}`,
    { method: "PATCH", body: JSON.stringify(payload) },
  ),
  confirmArtifact: (roomId, artifactId, expectedVersion) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/artifacts/${encodeURIComponent(artifactId)}/confirm`,
    { method: "POST", body: JSON.stringify({ expected_version: expectedVersion }) },
  ),
  createArtifactUserDecision: (roomId, artifactId, payload) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/artifacts/${encodeURIComponent(artifactId)}/user-decision`,
    { method: "POST", body: JSON.stringify(payload) },
  ),
  pauseRound: (roomId, roundId) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/rounds/${encodeURIComponent(roundId)}/pause`,
    { method: "POST", body: JSON.stringify({}) },
  ),
  cancelRound: (roomId, roundId) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/rounds/${encodeURIComponent(roundId)}/cancel`,
    { method: "POST", body: JSON.stringify({}) },
  ),
  roundExecutionTrace: (
    roomId,
    roundId,
    { limit = 200, cursor = "", signal } = {},
  ) => {
    const parameters = new URLSearchParams({ limit: String(limit) });
    if (cursor) parameters.set("cursor", cursor);
    return jsonRequest(
      `/api/rooms/${encodeURIComponent(roomId)}/rounds/${encodeURIComponent(roundId)}/audit-trace?${parameters.toString()}`,
      { signal },
    );
  },
  discussionAudit: (roomId, roundId, signal) => jsonRequest(
    `/api/rooms/${encodeURIComponent(roomId)}/rounds/${encodeURIComponent(roundId)}/discussion-audit`,
    { signal },
  ),
};

async function streamEvents(path, payload, onEvent, signal) {
  const response = await fetch(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(sessionToken ? { "X-AI-Studio-Token": sessionToken } : {}),
    },
    body: JSON.stringify(payload),
    signal,
  });
  if (!response.ok || !response.body) {
    let errorMessage = `请求失败：${response.status}`;
    try {
      const errorPayload = await response.json();
      if (errorPayload?.error) errorMessage = errorPayload.error;
    } catch {
      // Keep the status-based message when the server did not return JSON.
    }
    throw new Error(errorMessage);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      if (!line.trim()) continue;
      onEvent(JSON.parse(line));
    }
  }
  if (buffer.trim()) onEvent(JSON.parse(buffer));
}

export function streamMessage(roomId, payload, onEvent, signal) {
  return streamEvents(
    `/api/rooms/${encodeURIComponent(roomId)}/messages/stream`,
    { ...payload, skip_providers: ["openai"] },
    onEvent,
    signal,
  );
}

export function resumeChatRequest(roomId, requestId, onEvent, signal) {
  return streamEvents(
    `/api/rooms/${encodeURIComponent(roomId)}/chat-requests/${encodeURIComponent(requestId)}/resume/stream`,
    {},
    onEvent,
    signal,
  );
}

export function streamRound(roomId, authorizationPayload, onEvent, signal) {
  return streamEvents(
    `/api/rooms/${encodeURIComponent(roomId)}/rounds/stream`,
    authorizationPayload,
    onEvent,
    signal,
  );
}

export function resumeRound(roomId, roundId, onEvent, signal) {
  return streamEvents(
    `/api/rooms/${encodeURIComponent(roomId)}/rounds/${encodeURIComponent(roundId)}/resume/stream`,
    {},
    onEvent,
    signal,
  );
}
