function nonNegativeInteger(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) return 0;
  return Math.floor(number);
}

function blockerCount(gate) {
  return Array.isArray(gate?.blockers) ? gate.blockers.length : 0;
}

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

function firstRecord(...values) {
  return values.find(isRecord) || null;
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

function issueMessages(source) {
  return (Array.isArray(source?.issues) ? source.issues : []).flatMap((issue) => {
    if (typeof issue === "string" && issue.trim()) return [issue.trim()];
    const message = cleanText(issue?.message);
    return message ? [message] : [];
  });
}

function hasOwn(source, key) {
  return isRecord(source) && Object.prototype.hasOwnProperty.call(source, key);
}

function safetyFieldsOk(source) {
  if (!isRecord(source)) return true;
  const executionCapability = cleanText(source.execution_capability).toLowerCase();
  if (executionCapability && executionCapability !== "none") return false;
  if (hasOwn(source, "live_trading_allowed") && source.live_trading_allowed !== false) return false;
  if (hasOwn(source, "can_autonomously_decide") && source.can_autonomously_decide !== false) return false;
  return true;
}

function alignmentOk(source) {
  if (!isRecord(source)) return true;
  return source.ready !== false
    && source.integrity_ok !== false
    && source.aligned !== false
    && source.matches !== false;
}

function actionCount(source, action, reviews) {
  const fromActionCounts = source?.action_counts?.[action];
  if (Number.isFinite(Number(fromActionCounts))) return nonNegativeInteger(fromActionCounts);
  const legacyField = source?.[`${action}_count`];
  if (Number.isFinite(Number(legacyField))) return nonNegativeInteger(legacyField);
  return reviews.filter((review) => review.action === action).length;
}

export const ARTIFACT_GOVERNANCE_BOUNDARY = "风控意见不是用户决定、批准、否决或执行授权。";

const dispositionMeta = {
  support: { label: "风控意见：支持", tone: "support" },
  challenge: { label: "风控意见：质疑", tone: "challenge" },
  reject: { label: "风控意见：拒绝", tone: "reject" },
};

export function riskDispositionMeta(action) {
  return dispositionMeta[cleanText(action).toLowerCase()]
    || { label: "风控意见：未分类", tone: "unknown" };
}

function normalizedRiskReviews(source) {
  return (Array.isArray(source?.reviews) ? source.reviews : [])
    .filter(isRecord)
    .map((review) => {
      const action = cleanText(review.action).toLowerCase();
      const meta = riskDispositionMeta(action);
      const status = cleanText(review.status).toLowerCase();
      return {
        candidateId: cleanText(review.candidate_id),
        candidateRevision: positiveInteger(review.candidate_revision),
        currentCandidateRevision: positiveInteger(review.current_candidate_revision),
        candidateLatestMessageId: cleanText(review.candidate_latest_message_id),
        candidateSnapshot: isRecord(review.candidate_snapshot) ? review.candidate_snapshot : {},
        candidateSnapshotSha256: cleanText(review.candidate_snapshot_sha256),
        action,
        dispositionLabel: meta.label,
        tone: meta.tone,
        status: status === "current" ? "current" : status === "stale" ? "stale" : "unknown",
        reviewMessageId: cleanText(review.review_message_id),
        reviewerMemberId: cleanText(review.reviewer_member_id),
        reviewerMemberVersion: positiveInteger(review.reviewer_member_version),
        reviewerName: cleanText(review.reviewer_name),
        reviewerStage: cleanText(review.reviewer_stage),
        riskIds: (Array.isArray(review.risk_ids) ? review.risk_ids : [])
          .map(cleanText)
          .filter(Boolean),
      };
    });
}

function candidateTitleSources(artifact, reviews) {
  const titles = new Map();
  for (const review of reviews) {
    const title = cleanText(review.candidateSnapshot?.title);
    if (review.candidateId && title && !titles.has(review.candidateId)) {
      titles.set(review.candidateId, title);
    }
  }
  const options = artifact?.content?.decision?.options;
  for (const option of Array.isArray(options) ? options : []) {
    const id = cleanText(option?.id);
    const title = cleanText(option?.title);
    if (id && title && !titles.has(id)) titles.set(id, title);
  }
  return titles;
}

export function artifactCandidateGovernance(artifact) {
  const snapshot = isRecord(artifact?.governance_snapshot)
    ? artifact.governance_snapshot
    : null;
  if (!snapshot) {
    return {
      available: false,
      ready: false,
      status: "legacy",
      integrityOk: false,
      applicable: false,
      safetyOk: true,
      version: "",
      attestationVersion: "",
      attestationSha256: "",
      snapshotSha256: "",
      issues: [],
      lineage: {
        available: false,
        applicable: false,
        ready: false,
        status: "legacy",
        version: "",
        decisionMessageId: "",
        candidates: [],
        issues: [],
      },
      riskReview: {
        available: false,
        applicable: false,
        ready: false,
        status: "legacy",
        version: "",
        decisionMessageId: "",
        targetCandidateCount: 0,
        reviewedCandidateCount: 0,
        currentReviewCount: 0,
        staleReviewCount: 0,
        actionCounts: { support: 0, challenge: 0, reject: 0 },
        reviews: [],
        issues: [],
      },
      boundary: ARTIFACT_GOVERNANCE_BOUNDARY,
    };
  }

  const projection = firstRecord(
    snapshot.projection,
    snapshot.governance_projection,
    snapshot.attestation?.projection,
    parsedRecord(snapshot.projection_json),
    snapshot,
  );
  const lineageSource = firstRecord(
    projection?.candidate_lineage,
    projection?.candidate_lineage_gate,
    snapshot.candidate_lineage,
    snapshot.candidate_lineage_gate,
  );
  const riskSource = firstRecord(
    projection?.candidate_risk_reviews,
    projection?.candidate_risk_review,
    projection?.candidate_risk_review_gate,
    snapshot.candidate_risk_reviews,
    snapshot.candidate_risk_review,
    snapshot.candidate_risk_review_gate,
  );
  const reviews = normalizedRiskReviews(riskSource);
  const titles = candidateTitleSources(artifact, reviews);
  const preferredOptionId = cleanText(artifact?.content?.decision?.preferred_option_id);
  const candidates = (Array.isArray(lineageSource?.candidates) ? lineageSource.candidates : [])
    .filter(isRecord)
    .map((candidate) => {
      const id = cleanText(candidate.id);
      return {
        id,
        title: titles.get(id) || "",
        revision: positiveInteger(candidate.revision),
        originMessageId: cleanText(candidate.origin_message_id),
        latestMessageId: cleanText(candidate.latest_message_id),
        preferred: Boolean(id && id === preferredOptionId),
      };
    });
  const lineageAvailable = Boolean(lineageSource);
  const riskAvailable = Boolean(riskSource);
  const riskApplicable = riskSource?.applicable === true;
  const lineageReady = lineageAvailable && lineageSource.ready === true;
  const dispositionsOnly = riskSource?.review_actions_are_dispositions_only !== false;
  const riskReady = riskAvailable && (
    riskApplicable
      ? riskSource.ready === true && dispositionsOnly && safetyFieldsOk(riskSource)
      : riskSource?.applicable === false
  );
  const attestation = firstRecord(snapshot.attestation);
  const integrityOk = snapshot.integrity_ok !== false
    && snapshot.attestation_integrity_ok !== false
    && snapshot.hash_valid !== false
    && attestation?.integrity_ok !== false
    && attestation?.hash_valid !== false;
  const applicable = snapshot.applicable !== false;
  const snapshotStatus = cleanText(snapshot.status).toLowerCase();
  const topLevelStatusReady = !snapshotStatus || snapshotStatus === "ready";
  const safetyOk = safetyFieldsOk(snapshot) && safetyFieldsOk(projection);
  const artifactAlignmentOk = alignmentOk(snapshot.artifact_alignment);
  const ready = applicable
    && lineageReady
    && riskReady
    && integrityOk
    && safetyOk
    && artifactAlignmentOk
    && snapshot.ready !== false
    && topLevelStatusReady;
  const currentReviewCount = Number.isFinite(Number(riskSource?.current_review_count))
    ? nonNegativeInteger(riskSource.current_review_count)
    : reviews.filter((review) => review.status === "current").length;
  const staleReviewCount = Number.isFinite(Number(riskSource?.stale_review_count))
    ? nonNegativeInteger(riskSource.stale_review_count)
    : reviews.filter((review) => review.status === "stale").length;
  const reviewedCandidateCount = Number.isFinite(Number(riskSource?.reviewed_candidate_count))
    ? nonNegativeInteger(riskSource.reviewed_candidate_count)
    : new Set(
      reviews
        .filter((review) => review.status === "current")
        .map((review) => review.candidateId)
        .filter(Boolean),
    ).size;
  const targetCandidateCount = Number.isFinite(Number(
    riskSource?.target_candidate_count ?? riskSource?.candidate_count,
  ))
    ? nonNegativeInteger(riskSource?.target_candidate_count ?? riskSource?.candidate_count)
    : Array.isArray(riskSource?.target_candidate_ids)
      ? riskSource.target_candidate_ids.length
      : candidates.length;

  return {
    available: true,
    ready,
    applicable,
    status: snapshotStatus || (ready ? "ready" : applicable ? "blocked" : "not_applicable"),
    integrityOk,
    safetyOk,
    version: cleanText(
      snapshot.version
      || snapshot.protocol_version
      || projection?.version,
    ),
    attestationVersion: cleanText(
      snapshot.attestation_version
      || attestation?.version
      || attestation?.attestation_version,
    ),
    attestationSha256: cleanText(
      snapshot.attestation_sha256
      || snapshot.governance_attestation_sha256
      || attestation?.sha256
      || attestation?.attestation_sha256,
    ),
    snapshotSha256: cleanText(snapshot.snapshot_sha256),
    issues: issueMessages(snapshot),
    lineage: {
      available: lineageAvailable,
      applicable: lineageAvailable,
      ready: lineageReady,
      status: cleanText(lineageSource?.status) || (lineageReady ? "ready" : "blocked"),
      version: cleanText(lineageSource?.version),
      decisionMessageId: cleanText(lineageSource?.decision_message_id),
      candidates,
      issues: issueMessages(lineageSource),
    },
    riskReview: {
      available: riskAvailable,
      applicable: riskApplicable,
      ready: riskReady,
      status: cleanText(riskSource?.status) || (riskReady ? "ready" : "blocked"),
      version: cleanText(riskSource?.version),
      decisionMessageId: cleanText(riskSource?.decision_message_id),
      targetCandidateCount,
      reviewedCandidateCount,
      currentReviewCount,
      staleReviewCount,
      actionCounts: {
        support: actionCount(riskSource, "support", reviews),
        challenge: actionCount(riskSource, "challenge", reviews),
        reject: actionCount(riskSource, "reject", reviews),
      },
      reviews,
      issues: issueMessages(riskSource),
      dispositionsOnly,
    },
    boundary: ARTIFACT_GOVERNANCE_BOUNDARY,
  };
}

export function artifactGovernanceBadge(artifact) {
  const governance = artifactCandidateGovernance(artifact);
  if (!governance.available) return null;
  if (!governance.applicable) {
    return {
      label: "候选治理不适用",
      tone: "neutral",
      title: governance.issues[0] || "该产物未启用候选谱系与精确版本风控治理",
    };
  }
  if (!governance.ready) {
    return {
      label: "治理记录待补齐",
      tone: "blocked",
      title: "候选谱系或精确版本风控意见尚未完整绑定",
    };
  }
  if (!governance.riskReview.applicable) {
    return {
      label: "候选谱系已绑定",
      tone: "ready",
      title: `${governance.lineage.candidates.length} 个候选已绑定只读谱系；本轮未要求精确版本风控复核`,
    };
  }
  return {
    label: "谱系与风控已绑定",
    tone: "ready",
    title: `${governance.lineage.candidates.length} 个候选，${governance.riskReview.currentReviewCount} 条当前版本风控意见`,
  };
}

export function candidateGovernanceRows(convergence) {
  const source = convergence && typeof convergence === "object" ? convergence : {};
  const lineage = source.candidate_lineage_gate;
  const riskReview = source.candidate_risk_review_gate;
  const rows = [];

  if (lineage?.applicable === true) {
    const candidateCount = nonNegativeInteger(lineage.candidate_count);
    const ready = lineage.ready === true;
    rows.push({
      id: "candidate-lineage",
      ready,
      label: "候选版本谱系",
      detail: ready
        ? `${candidateCount} 个候选 · 决策仅引用冻结版本`
        : `${candidateCount} 个候选 · ${blockerCount(lineage)} 项谱系缺口`,
    });
  }

  if (riskReview?.applicable === true) {
    const candidateCount = nonNegativeInteger(riskReview.candidate_count);
    const reviewedCount = nonNegativeInteger(riskReview.reviewed_candidate_count);
    const supportCount = nonNegativeInteger(riskReview.support_count);
    const challengeCount = nonNegativeInteger(riskReview.challenge_count);
    const rejectCount = nonNegativeInteger(riskReview.reject_count);
    const staleCount = nonNegativeInteger(riskReview.stale_review_count);
    const ready = riskReview.ready === true;
    const staleDetail = staleCount ? ` · 过期 ${staleCount}` : "";
    rows.push({
      id: "candidate-risk-review",
      ready,
      label: "候选风险复核",
      detail: (
        `${reviewedCount} / ${candidateCount} 个精确版本 · `
        + `支持 ${supportCount} / 质疑 ${challengeCount} / 拒绝 ${rejectCount}`
        + staleDetail
      ),
    });
  }

  return rows;
}
