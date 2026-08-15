import { artifactCandidateGovernance } from "./candidateGovernance.js";

export const ARTIFACT_USER_DECISION_VERSION = "artifact_user_decision_v2";

export const USER_DECISION_ACTIONS = {
  support: {
    label: "支持候选",
    shortLabel: "已支持",
    description: "支持你明确选择的当前候选；可以不同于 AI 首选。",
  },
  hold: {
    label: "暂时保留",
    shortLabel: "已保留",
    description: "保留判断，等待更多证据或条件变化。",
  },
  return: {
    label: "退回修订",
    shortLabel: "已退回",
    description: "要求补充或修改后，再重新提交确认。",
  },
};

function isRecord(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function cleanText(value) {
  return String(value ?? "").trim();
}

function positiveInteger(value) {
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number < 1) return null;
  return number;
}

function isSha256(value) {
  return /^[0-9a-f]{64}$/i.test(cleanText(value));
}

function parsedRecord(value) {
  if (isRecord(value)) return value;
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    const parsed = JSON.parse(value);
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function governanceProjection(artifact) {
  const snapshot = isRecord(artifact?.governance_snapshot)
    ? artifact.governance_snapshot
    : null;
  if (!snapshot) return null;
  return [
    snapshot.projection,
    snapshot.governance_projection,
    snapshot.attestation?.projection,
    parsedRecord(snapshot.projection_json),
  ].find(isRecord) || null;
}

function candidateTitleMap(artifact) {
  const titles = new Map();
  const projectionOptions = governanceProjection(artifact)?.decision?.options;
  for (const option of Array.isArray(projectionOptions) ? projectionOptions : []) {
    const id = cleanText(option?.id);
    const title = cleanText(option?.title);
    if (id && title) titles.set(id, title);
  }
  const artifactOptions = artifact?.content?.decision?.options;
  for (const option of Array.isArray(artifactOptions) ? artifactOptions : []) {
    const id = cleanText(option?.id);
    const title = cleanText(option?.title);
    if (id && title && !titles.has(id)) titles.set(id, title);
  }
  return titles;
}

function aiPreferredOptionId(artifact, decision = null) {
  const explicit = cleanText(decision?.ai_preferred_option_id);
  if (explicit) return explicit;
  const legacy = cleanText(decision?.decision_version) !== ARTIFACT_USER_DECISION_VERSION
    ? cleanText(decision?.preferred_option_id)
    : "";
  if (legacy) return legacy;
  const projected = cleanText(governanceProjection(artifact)?.decision?.preferred_option_id);
  if (projected) return projected;
  const artifactPreferred = cleanText(artifact?.content?.decision?.preferred_option_id);
  if (artifactPreferred) return artifactPreferred;
  return "";
}

function appendIssue(issues, code, message) {
  if (issues.some((issue) => issue.code === code)) return;
  issues.push({ code, message });
}

function appendGovernedProjectionIntegrityIssues(issues, artifact, governance, projection) {
  const decision = isRecord(projection?.decision) ? projection.decision : null;
  const options = Array.isArray(decision?.options) ? decision.options.filter(isRecord) : [];
  const lineageById = new Map(
    governance.lineage.candidates
      .filter((candidate) => candidate.id)
      .map((candidate) => [candidate.id, candidate]),
  );
  const seen = new Set();
  if (!options.length) {
    appendIssue(issues, "CANDIDATES_MISSING", "权威治理投影中没有可选择的候选。");
  }
  for (const option of options) {
    const id = cleanText(option.id);
    if (!id || seen.has(id)) {
      appendIssue(
        issues,
        id ? `CANDIDATE_DUPLICATE:${id}` : "CANDIDATE_ID_MISSING",
        id ? `权威治理投影重复候选 ${id}。` : "权威治理投影包含无 ID 候选。",
      );
      continue;
    }
    seen.add(id);
    const optionLineage = isRecord(option.lineage) ? option.lineage : {};
    const lineage = lineageById.get(id);
    const revision = positiveInteger(optionLineage.revision);
    const originMessageId = cleanText(optionLineage.origin_message_id);
    const latestMessageId = cleanText(optionLineage.latest_message_id);
    if (!(
      lineage
      && revision
      && originMessageId
      && latestMessageId
      && lineage.revision === revision
      && lineage.originMessageId === originMessageId
      && lineage.latestMessageId === latestMessageId
    )) {
      appendIssue(
        issues,
        `CANDIDATE_LINEAGE_DRIFT:${id}`,
        `候选 ${id} 的权威投影与冻结谱系不一致。`,
      );
    }
    if (governance.riskReview.applicable) {
      const exactReview = governance.riskReview.reviews.some((review) => (
        review.candidateId === id
        && review.status === "current"
        && review.candidateRevision === revision
        && review.candidateLatestMessageId === latestMessageId
        && isSha256(review.candidateSnapshotSha256)
      ));
      if (!governance.riskReview.ready || !exactReview) {
        appendIssue(
          issues,
          `CANDIDATE_RISK_REVIEW_NOT_CURRENT:${id}`,
          `候选 ${id} 缺少当前精确版本的合格风控复核。`,
        );
      }
    } else if (!governance.riskReview.ready) {
      appendIssue(issues, "RISK_REVIEW_UNKNOWN", "精确版本风控复核状态未知。");
    }
  }
  if (lineageById.size !== seen.size || [...lineageById.keys()].some((id) => !seen.has(id))) {
    appendIssue(issues, "CANDIDATE_SET_DRIFT", "权威候选集合与冻结谱系候选集合不一致。");
  }
  const preferredOptionId = cleanText(decision?.preferred_option_id);
  if (!preferredOptionId || !seen.has(preferredOptionId)) {
    appendIssue(issues, "AI_PREFERRED_UNKNOWN", "AI 首选未绑定到当前治理候选集合。");
  }
  const artifactPreferred = cleanText(artifact?.content?.decision?.preferred_option_id);
  if (artifactPreferred !== preferredOptionId) {
    appendIssue(issues, "AI_PREFERRED_DRIFT", "产物中的 AI 首选与权威治理投影不一致。");
  }
}

/**
 * Validate whether any final user disposition may be recorded. An explicit,
 * integrity-valid non-applicable snapshot is different from unknown or drift.
 */
export function artifactUserDecisionGate(artifact) {
  const governance = artifactCandidateGovernance(artifact);
  const snapshot = isRecord(artifact?.governance_snapshot)
    ? artifact.governance_snapshot
    : null;
  const projection = governanceProjection(artifact);
  const issues = [];

  if (cleanText(artifact?.status).toUpperCase() !== "CONFIRMED") {
    appendIssue(issues, "ARTIFACT_NOT_CONFIRMED", "只有已确认产物才能记录用户最终决定。");
  }
  if (artifact?.evidence_review?.confirmation_ready === false) {
    appendIssue(issues, "CONFIRMATION_INVALID", "当前确认版本未通过现行证据门。");
  }
  if (!governance.available || !snapshot) {
    appendIssue(issues, "GOVERNANCE_UNKNOWN", "缺少服务端治理快照，不能安全记录最终决定。");
  } else if (!governance.integrityOk || !governance.safetyOk) {
    appendIssue(issues, "GOVERNANCE_NOT_READY", "候选治理证明未就绪或完整性校验未通过。");
  } else if (governance.applicable) {
    if (!projection || !governance.ready) {
      appendIssue(issues, "GOVERNANCE_NOT_READY", "候选治理证明未就绪或完整性校验未通过。");
    } else {
      appendGovernedProjectionIntegrityIssues(issues, artifact, governance, projection);
    }
  } else if (snapshot.ready !== true || snapshot.applicable !== false) {
    appendIssue(issues, "GOVERNANCE_UNKNOWN", "治理适用状态未知，不能安全记录最终决定。");
  }

  const attestationSha256 = cleanText(governance.attestationSha256);
  if (governance.applicable && !isSha256(attestationSha256)) {
    appendIssue(issues, "ATTESTATION_INVALID", "治理证明哈希缺失或格式无效。");
  }

  const boundArtifact = isRecord(snapshot?.artifact) ? snapshot.artifact : {};
  const artifactVersion = positiveInteger(artifact?.version);
  const boundVersion = positiveInteger(boundArtifact.artifact_version);
  if (!artifactVersion || !boundVersion || artifactVersion !== boundVersion) {
    appendIssue(issues, "ARTIFACT_VERSION_DRIFT", "治理证明绑定的产物版本与当前版本不一致。");
  }
  const artifactId = cleanText(artifact?.id);
  const boundArtifactId = cleanText(boundArtifact.artifact_id);
  if (!artifactId || !boundArtifactId || artifactId !== boundArtifactId) {
    appendIssue(issues, "ARTIFACT_ID_DRIFT", "治理证明绑定的产物与当前产物不一致。");
  }

  return {
    ready: issues.length === 0,
    reason: issues[0]?.message || "",
    issues,
    applicable: governance.applicable,
    governed: governance.applicable,
    explicitNonApplicable: Boolean(
      snapshot
      && snapshot.applicable === false
      && snapshot.ready === true
      && governance.integrityOk
      && governance.safetyOk
    ),
    attestationSha256,
    executionCapability: "none",
  };
}

/**
 * Return candidates that may be explicitly selected by a support decision.
 * Governed candidates must carry an exact lineage and current risk review;
 * explicit non-applicable artifacts use their confirmed decision options and
 * intentionally carry no invented governance tokens.
 */
export function artifactUserDecisionSelection(artifact) {
  const decisionGate = artifactUserDecisionGate(artifact);
  const governance = artifactCandidateGovernance(artifact);
  const projection = governanceProjection(artifact);
  const projectedDecision = isRecord(projection?.decision) ? projection.decision : null;
  const artifactDecision = isRecord(artifact?.content?.decision)
    ? artifact.content.decision
    : null;
  const authoritativeDecision = decisionGate.governed
    ? projectedDecision
    : decisionGate.explicitNonApplicable
      ? artifactDecision
      : null;
  const projectedOptions = Array.isArray(authoritativeDecision?.options)
    ? authoritativeDecision.options.filter(isRecord)
    : [];
  const issues = [...decisionGate.issues];

  if (!projectedOptions.length) {
    appendIssue(
      issues,
      "CANDIDATES_MISSING",
      decisionGate.governed
        ? "权威治理投影中没有可选择的候选。"
        : "当前确认产物中没有可选择的候选。",
    );
  }
  const lineageById = new Map(
    governance.lineage.candidates
      .filter((candidate) => candidate.id)
      .map((candidate) => [candidate.id, candidate]),
  );
  const reviews = governance.riskReview.reviews;
  const seen = new Set();
  const candidates = projectedOptions.flatMap((option) => {
    const id = cleanText(option.id);
    if (!id || seen.has(id)) {
      appendIssue(
        issues,
        id ? `CANDIDATE_DUPLICATE:${id}` : "CANDIDATE_ID_MISSING",
        id ? `权威治理投影重复候选 ${id}。` : "权威治理投影包含无 ID 候选。",
      );
      return [];
    }
    seen.add(id);
    const optionLineage = isRecord(option.lineage) ? option.lineage : {};
    const lineage = lineageById.get(id);
    const revision = decisionGate.governed ? positiveInteger(optionLineage.revision) : null;
    const originMessageId = decisionGate.governed
      ? cleanText(optionLineage.origin_message_id)
      : "";
    const latestMessageId = decisionGate.governed
      ? cleanText(optionLineage.latest_message_id)
      : "";
    const exactLineage = Boolean(
      !decisionGate.governed
      || (
        lineage
        && revision
        && originMessageId
        && latestMessageId
        && lineage.revision === revision
        && lineage.originMessageId === originMessageId
        && lineage.latestMessageId === latestMessageId
      )
    );
    if (!exactLineage) {
      appendIssue(
        issues,
        `CANDIDATE_LINEAGE_DRIFT:${id}`,
        `候选 ${id} 的权威投影与冻结谱系不一致。`,
      );
    }

    let riskReview = null;
    if (decisionGate.governed) {
      riskReview = reviews.find((review) => (
        review.candidateId === id
        && review.status === "current"
        && review.candidateRevision === revision
        && review.candidateLatestMessageId === latestMessageId
        && isSha256(review.candidateSnapshotSha256)
      )) || null;
      if (!governance.riskReview.ready || !riskReview) {
        appendIssue(
          issues,
          `CANDIDATE_RISK_REVIEW_NOT_CURRENT:${id}`,
          `候选 ${id} 缺少当前精确版本的合格风控复核。`,
        );
      }
    }

    return [{
      id,
      title: cleanText(option.title) || id,
      description: cleanText(option.description || option.text),
      revision,
      originMessageId,
      latestMessageId,
      aiPreferred: id === aiPreferredOptionId(artifact),
      riskReview,
    }];
  });
  if (decisionGate.governed && lineageById.size !== candidates.length) {
    appendIssue(issues, "CANDIDATE_SET_DRIFT", "权威候选集合与冻结谱系候选集合不一致。");
  }
  const preferredOptionId = aiPreferredOptionId(artifact);
  if (
    decisionGate.governed
    && (!preferredOptionId || !candidates.some((candidate) => candidate.id === preferredOptionId))
  ) {
    appendIssue(issues, "AI_PREFERRED_UNKNOWN", "AI 首选未绑定到当前治理候选集合。");
  }

  return {
    ready: issues.length === 0,
    reason: issues[0]?.message || "",
    issues,
    decisionReady: decisionGate.ready,
    decisionReason: decisionGate.reason,
    governed: decisionGate.governed,
    explicitNonApplicable: decisionGate.explicitNonApplicable,
    candidates,
    aiPreferredOptionId: preferredOptionId,
    attestationSha256: decisionGate.attestationSha256,
    executionCapability: "none",
  };
}

/** Build the exact POST body; hold/return intentionally omit all candidate tokens. */
export function buildArtifactUserDecisionRequest(
  artifact,
  { action, rationale, selectedOptionId = "" } = {},
) {
  const cleanAction = cleanText(action).toLowerCase();
  if (!Object.hasOwn(USER_DECISION_ACTIONS, cleanAction)) {
    throw new Error("最终决定只能是支持候选、暂时保留或退回修订。");
  }
  const cleanRationale = cleanText(rationale);
  if (cleanRationale.length < 3) throw new Error("请填写最终决定理由。");
  if (cleanRationale.length > 4000) throw new Error("最终决定理由不能超过 4000 字。");
  const expectedVersion = positiveInteger(artifact?.version);
  if (!expectedVersion) throw new Error("最终决定必须绑定有效产物版本。");

  const payload = {
    expected_version: expectedVersion,
    action: cleanAction,
    rationale: cleanRationale,
  };
  if (cleanAction !== "support") {
    const decisionGate = artifactUserDecisionGate(artifact);
    if (!decisionGate.ready) {
      throw new Error(decisionGate.reason || "当前候选治理状态不可用于记录最终决定。");
    }
    return payload;
  }

  const selection = artifactUserDecisionSelection(artifact);
  if (!selection.decisionReady) {
    throw new Error(selection.decisionReason || "当前候选治理状态不可用于记录最终决定。");
  }
  if (!selection.ready) {
    throw new Error(selection.reason || "当前候选状态不可用于支持决定。");
  }
  const selectedId = cleanText(selectedOptionId);
  const candidate = selection.candidates.find((item) => item.id === selectedId);
  if (!candidate) throw new Error("支持候选前，请明确选择一个当前治理候选。");
  const supportPayload = {
    ...payload,
    selected_option_id: candidate.id,
  };
  if (!selection.governed) return supportPayload;
  return {
    ...supportPayload,
    expected_candidate_revision: candidate.revision,
    expected_candidate_origin_message_id: candidate.originMessageId,
    expected_candidate_latest_message_id: candidate.latestMessageId,
    expected_governance_attestation_sha256: selection.attestationSha256,
  };
}

/** Keep AI preference and the human's explicit selection separate in every view/export. */
export function artifactUserDecisionPresentation(artifact, decision) {
  const action = cleanText(decision?.action).toLowerCase();
  const titles = candidateTitleMap(artifact);
  const aiPreferredId = aiPreferredOptionId(artifact, decision);
  // The backend may expose a compatibility selected_option_id on v1 rows.
  // Only the v2 contract proves that the human made an explicit candidate choice.
  const hasExplicitSelection = action === "support"
    && cleanText(decision?.decision_version) === ARTIFACT_USER_DECISION_VERSION;
  const selectedOptionId = hasExplicitSelection
    ? cleanText(decision?.selected_option_id)
    : "";
  const selectedIsAiPreferred = Boolean(
    selectedOptionId
    && (
      typeof decision?.selected_is_ai_preferred === "boolean"
        ? decision.selected_is_ai_preferred
        : selectedOptionId === aiPreferredId
    )
  );
  return {
    action,
    aiPreferredOptionId: aiPreferredId,
    aiPreferredLabel: titles.get(aiPreferredId) || aiPreferredId || "未记录",
    selectedOptionId,
    selectedOptionLabel: titles.get(selectedOptionId) || selectedOptionId || "",
    hasExplicitSelection: Boolean(selectedOptionId),
    legacySelectionUnavailable: action === "support" && !selectedOptionId,
    selectedIsAiPreferred,
  };
}

function timestampValue(decision) {
  const raw = decision?.created_at;
  if (typeof raw === "number" && Number.isFinite(raw)) return raw;
  const parsed = Date.parse(String(raw || ""));
  return Number.isFinite(parsed) ? parsed : 0;
}

export function artifactDecisionHistory(artifact) {
  const decisions = [];
  const seen = new Set();
  const append = (decision) => {
    if (!decision || typeof decision !== "object") return;
    const id = String(decision.id || "");
    const fallbackKey = `${decision.artifact_version || 0}:${decision.action || ""}:${decision.created_at || ""}`;
    const key = id || fallbackKey;
    if (seen.has(key)) return;
    seen.add(key);
    decisions.push(decision);
  };
  append(artifact?.user_decision);
  (Array.isArray(artifact?.user_decision_history) ? artifact.user_decision_history : []).forEach(append);
  return decisions.toSorted((left, right) => timestampValue(right) - timestampValue(left));
}

export function artifactDecisionState(artifact) {
  const history = artifactDecisionHistory(artifact);
  const current = history.find((decision) => decision.is_current === true) || null;
  const latest = current || history[0] || null;
  const stale = history.filter((decision) => decision.is_current !== true);
  return { current, latest, stale, history };
}

export function userDecisionLabel(decision, short = false) {
  const meta = USER_DECISION_ACTIONS[String(decision?.action || "")];
  if (!meta) return "未知决定";
  return short ? meta.shortLabel : meta.label;
}

export function formatUserDecisionTime(value) {
  const numeric = typeof value === "number" && Number.isFinite(value) ? value : Number.NaN;
  const date = new Date(Number.isFinite(numeric) ? numeric : String(value || ""));
  if (Number.isNaN(date.getTime())) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}
