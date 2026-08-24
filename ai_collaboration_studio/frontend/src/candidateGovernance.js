export const CANDIDATE_GOVERNANCE_CANDIDATE_LIMIT = 1000;
export const CANDIDATE_GOVERNANCE_REVIEW_LIMIT = 2000;
export const CANDIDATE_GOVERNANCE_RISK_ID_LIMIT = 100;
export const CANDIDATE_GOVERNANCE_ISSUE_LIMIT = 200;

const SHA256_PATTERN = /^[0-9a-f]{64}$/i;
const PROJECTION_JSON_LIMIT = 1024 * 1024;

function parsedNonNegativeInteger(value) {
  if (
    value === null
    || value === undefined
    || typeof value === "boolean"
    || (typeof value !== "string" && typeof value !== "number")
    || (typeof value === "string" && !value.trim())
  ) return null;
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number < 0) return null;
  return number;
}

function nonNegativeInteger(value) {
  return parsedNonNegativeInteger(value) ?? 0;
}

function blockerCount(gate) {
  return Array.isArray(gate?.blockers) ? gate.blockers.length : 0;
}

function isRecord(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function cleanText(value, maxLength = 1000) {
  if (typeof value !== "string" && typeof value !== "number") return "";
  if (typeof value === "number" && !Number.isFinite(value)) return "";
  return String(value).trim().slice(0, maxLength);
}

function positiveInteger(value) {
  if (typeof value !== "string" && typeof value !== "number") return null;
  if (typeof value === "string" && !value.trim()) return null;
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
  if (value.length > PROJECTION_JSON_LIMIT) return null;
  try {
    const parsed = JSON.parse(value);
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function issueMessages(source) {
  const rawIssues = Array.isArray(source?.issues) ? source.issues : [];
  const messages = rawIssues.slice(0, CANDIDATE_GOVERNANCE_ISSUE_LIMIT).flatMap((issue) => {
    if (typeof issue === "string" && issue.trim()) return [cleanText(issue, 1000)];
    const message = cleanText(issue?.message, 1000);
    return message ? [message] : [];
  });
  if (rawIssues.length > CANDIDATE_GOVERNANCE_ISSUE_LIMIT) {
    messages.push(`问题记录超过 ${CANDIDATE_GOVERNANCE_ISSUE_LIMIT} 条安全上限，仅保留前序诊断。`);
  }
  return [...new Set(messages)];
}

function withProjectionKeys(records, prefix, identityParts) {
  const occurrences = new Map();
  return records.map((record) => {
    const parts = [prefix, ...identityParts(record)];
    const identity = JSON.stringify(parts);
    const occurrence = occurrences.get(identity) || 0;
    occurrences.set(identity, occurrence + 1);
    return {
      ...record,
      projectionKey: JSON.stringify([...parts, occurrence]),
    };
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

function declaredActionCount(source, action) {
  if (isRecord(source?.action_counts) && hasOwn(source.action_counts, action)) {
    return { declared: true, value: parsedNonNegativeInteger(source.action_counts[action]) };
  }
  const legacyKey = `${action}_count`;
  if (hasOwn(source, legacyKey)) {
    return { declared: true, value: parsedNonNegativeInteger(source[legacyKey]) };
  }
  return { declared: false, value: null };
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
  const rawReviews = Array.isArray(source?.reviews) ? source.reviews : [];
  let projectionOk = rawReviews.length <= CANDIDATE_GOVERNANCE_REVIEW_LIMIT;
  if (!projectionOk) return { reviews: [], projectionOk: false };
  const reviews = rawReviews.flatMap((review) => {
      if (!isRecord(review)) {
        projectionOk = false;
        return [];
      }
      const action = cleanText(review.action).toLowerCase();
      const meta = riskDispositionMeta(action);
      const status = cleanText(review.status).toLowerCase();
      const rawRiskIds = Array.isArray(review.risk_ids) ? review.risk_ids : [];
      if (rawRiskIds.length > CANDIDATE_GOVERNANCE_RISK_ID_LIMIT) projectionOk = false;
      const snapshotHash = cleanText(review.candidate_snapshot_sha256, 64).toLowerCase();
      return [{
        candidateId: cleanText(review.candidate_id, 240),
        candidateRevision: positiveInteger(review.candidate_revision),
        currentCandidateRevision: positiveInteger(review.current_candidate_revision),
        candidateLatestMessageId: cleanText(review.candidate_latest_message_id, 240),
        candidateSnapshot: {
          title: cleanText(review.candidate_snapshot?.title, 400),
          symbol: cleanText(review.candidate_snapshot?.symbol, 40).toUpperCase(),
          direction: cleanText(review.candidate_snapshot?.direction, 20).toUpperCase(),
          horizon_days: positiveInteger(review.candidate_snapshot?.horizon_days),
          thesis: cleanText(review.candidate_snapshot?.thesis, 4000),
          invalidation: cleanText(review.candidate_snapshot?.invalidation, 2000),
        },
        candidateSnapshotSha256: snapshotHash,
        candidateSnapshotHashValid: !snapshotHash || SHA256_PATTERN.test(snapshotHash),
        action,
        dispositionLabel: meta.label,
        tone: meta.tone,
        status: status === "current" ? "current" : status === "stale" ? "stale" : "unknown",
        reviewMessageId: cleanText(review.review_message_id, 240),
        reviewerMemberId: cleanText(review.reviewer_member_id, 240),
        reviewerMemberVersion: positiveInteger(review.reviewer_member_version),
        reviewerName: cleanText(review.reviewer_name, 240),
        reviewerStage: cleanText(review.reviewer_stage, 120),
        riskIds: [...new Set(rawRiskIds
          .slice(0, CANDIDATE_GOVERNANCE_RISK_ID_LIMIT)
          .map((riskId) => cleanText(riskId, 240))
          .filter(Boolean))],
      }];
    });
  return { reviews, projectionOk };
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
  for (const option of (Array.isArray(options) ? options : []).slice(0, CANDIDATE_GOVERNANCE_CANDIDATE_LIMIT)) {
    const id = cleanText(option?.id, 240);
    const title = cleanText(option?.title, 400);
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
  const reviewProjection = normalizedRiskReviews(riskSource);
  const reviews = withProjectionKeys(
    reviewProjection.reviews,
    "risk-review",
    (review) => [
      review.reviewMessageId,
      review.candidateId,
      review.candidateRevision,
      review.currentCandidateRevision,
      review.status,
      review.action,
      review.reviewerMemberId,
      review.reviewerMemberVersion,
      review.candidateSnapshotSha256,
    ],
  );
  const titles = candidateTitleSources(artifact, reviews);
  const preferredOptionId = cleanText(artifact?.content?.decision?.preferred_option_id, 240);
  const rawCandidates = Array.isArray(lineageSource?.candidates) ? lineageSource.candidates : [];
  let lineageProjectionOk = rawCandidates.length <= CANDIDATE_GOVERNANCE_CANDIDATE_LIMIT;
  const candidateRecords = lineageProjectionOk ? rawCandidates.flatMap((candidate) => {
      if (!isRecord(candidate)) {
        lineageProjectionOk = false;
        return [];
      }
      const id = cleanText(candidate.id, 240);
      return [{
        id,
        title: titles.get(id) || "",
        revision: positiveInteger(candidate.revision),
        originMessageId: cleanText(candidate.origin_message_id, 240),
        latestMessageId: cleanText(candidate.latest_message_id, 240),
        preferred: Boolean(id && id === preferredOptionId),
      }];
    }) : [];
  const candidates = withProjectionKeys(
    candidateRecords,
    "lineage-candidate",
    (candidate) => [
      candidate.id,
      candidate.revision,
      candidate.originMessageId,
      candidate.latestMessageId,
    ],
  );
  const lineageAvailable = Boolean(lineageSource);
  const riskAvailable = Boolean(riskSource);
  const riskApplicable = riskSource?.applicable === true;
  const candidateIds = candidates.map((candidate) => candidate.id).filter(Boolean);
  const candidateById = new Map(candidates.map((candidate) => [candidate.id, candidate]));
  const recordedLineageCandidateCount = parsedNonNegativeInteger(lineageSource?.candidate_count);
  const lineageCountConsistent = recordedLineageCandidateCount === null
    || recordedLineageCandidateCount === candidates.length;
  const lineageRecordsValid = candidates.every((candidate) => (
    Boolean(candidate.id)
    && candidate.revision !== null
    && Boolean(candidate.originMessageId)
    && Boolean(candidate.latestMessageId)
  ))
    && new Set(candidateIds).size === candidates.length
    && lineageProjectionOk
    && lineageCountConsistent;
  const lineageIssues = issueMessages(lineageSource);
  if (!lineageProjectionOk) {
    lineageIssues.push(`候选谱系超过 ${CANDIDATE_GOVERNANCE_CANDIDATE_LIMIT} 条安全上限或包含无效记录，已停止投影。`);
  }
  if (!lineageCountConsistent) {
    lineageIssues.push("候选谱系声明数量与实际唯一候选记录不一致，已在本地降级。");
  }
  if (lineageAvailable && lineageSource.ready === true && !lineageRecordsValid) {
    lineageIssues.push("候选谱系缺少唯一候选 ID、精确修订号或来源消息绑定，已在本地降级。 ".trim());
  }
  const lineageReady = lineageAvailable && lineageSource.ready === true && lineageRecordsValid;
  const currentReviews = reviews.filter((review) => review.status === "current");
  const staleReviews = reviews.filter((review) => review.status === "stale");
  const currentCandidateIds = currentReviews.map((review) => review.candidateId).filter(Boolean);
  const reviewMessageIds = reviews.map((review) => review.reviewMessageId).filter(Boolean);
  const riskRecordsValid = !riskApplicable || (reviewProjection.projectionOk && reviews.every((review) => (
    Boolean(review.candidateId)
    && candidateById.has(review.candidateId)
    && review.candidateRevision !== null
    && review.currentCandidateRevision !== null
    && Boolean(review.reviewMessageId)
    && review.candidateSnapshotHashValid
    && ["current", "stale"].includes(review.status)
    && ["support", "challenge", "reject"].includes(review.action)
    && review.currentCandidateRevision === candidateById.get(review.candidateId)?.revision
    && (!review.candidateLatestMessageId
      || review.candidateLatestMessageId === candidateById.get(review.candidateId)?.latestMessageId)
    && (review.status !== "current" || review.candidateRevision === review.currentCandidateRevision)
    && (review.status !== "stale" || review.candidateRevision !== review.currentCandidateRevision)
  ))
    && new Set(currentCandidateIds).size === currentCandidateIds.length
    && new Set(reviewMessageIds).size === reviews.length);

  const rawTargetCandidateIds = Array.isArray(riskSource?.target_candidate_ids)
    ? riskSource.target_candidate_ids
    : [];
  let targetIdsProjectionOk = rawTargetCandidateIds.length <= CANDIDATE_GOVERNANCE_CANDIDATE_LIMIT;
  const targetCandidateIds = [];
  const targetCandidateSeen = new Set();
  rawTargetCandidateIds.slice(0, CANDIDATE_GOVERNANCE_CANDIDATE_LIMIT).forEach((value) => {
    const id = cleanText(value, 240);
    if (!id || !candidateById.has(id) || targetCandidateSeen.has(id)) {
      targetIdsProjectionOk = false;
      return;
    }
    targetCandidateSeen.add(id);
    targetCandidateIds.push(id);
  });
  const effectiveTargetCandidateIds = rawTargetCandidateIds.length
    ? targetCandidateIds
    : candidateIds;
  const effectiveTargetSet = new Set(effectiveTargetCandidateIds);
  const currentReviewedCandidateIds = new Set(
    currentCandidateIds.filter((candidateId) => effectiveTargetSet.has(candidateId)),
  );
  const currentReviewCount = currentReviews.length;
  const staleReviewCount = staleReviews.length;
  const reviewedCandidateCount = currentReviewedCandidateIds.size;
  const targetCandidateCount = effectiveTargetCandidateIds.length;
  const actionCounts = {
    support: reviews.filter((review) => review.action === "support").length,
    challenge: reviews.filter((review) => review.action === "challenge").length,
    reject: reviews.filter((review) => review.action === "reject").length,
  };
  const recordedCurrentReviewCount = parsedNonNegativeInteger(riskSource?.current_review_count);
  const recordedStaleReviewCount = parsedNonNegativeInteger(riskSource?.stale_review_count);
  const recordedReviewedCandidateCount = parsedNonNegativeInteger(riskSource?.reviewed_candidate_count);
  const recordedTargetCandidateCount = parsedNonNegativeInteger(
    riskSource?.target_candidate_count ?? riskSource?.candidate_count,
  );
  const currentCountDeclared = hasOwn(riskSource, "current_review_count");
  const staleCountDeclared = hasOwn(riskSource, "stale_review_count");
  const reviewedCountDeclared = hasOwn(riskSource, "reviewed_candidate_count");
  const targetCountDeclared = hasOwn(riskSource, "target_candidate_count")
    || hasOwn(riskSource, "candidate_count");
  const declaredActions = {
    support: declaredActionCount(riskSource, "support"),
    challenge: declaredActionCount(riskSource, "challenge"),
    reject: declaredActionCount(riskSource, "reject"),
  };
  const riskCountsConsistent = (!currentCountDeclared || recordedCurrentReviewCount === currentReviewCount)
    && (!staleCountDeclared || recordedStaleReviewCount === staleReviewCount)
    && (!reviewedCountDeclared || recordedReviewedCandidateCount === reviewedCandidateCount)
    && (!targetCountDeclared || recordedTargetCandidateCount === targetCandidateCount)
    && Object.entries(declaredActions).every(([action, declared]) => (
      !declared.declared || declared.value === actionCounts[action]
    ));
  const coverageComplete = reviewedCandidateCount === targetCandidateCount;
  const riskIssues = issueMessages(riskSource);
  if (!reviewProjection.projectionOk) {
    riskIssues.push(`精确版本风控记录超过 ${CANDIDATE_GOVERNANCE_REVIEW_LIMIT} 条安全上限、风险 ID 超限或包含无效记录，已停止完成判定。`);
  }
  if (!targetIdsProjectionOk) {
    riskIssues.push("风控目标候选包含重复、缺失或谱系外标识，已在本地降级。");
  }
  if (!riskCountsConsistent) {
    riskIssues.push("风控声明计数与实际精确版本复核记录不一致，展示值已按记录重算。");
  }
  if (riskApplicable && !coverageComplete) {
    riskIssues.push("当前精确版本风控意见尚未覆盖全部目标候选。");
  }
  if (riskAvailable && riskSource.ready === true && !riskRecordsValid) {
    riskIssues.push("精确版本风控记录缺少候选版本、状态、处置或复核消息绑定，已在本地降级。 ".trim());
  }
  const dispositionsOnly = riskSource?.review_actions_are_dispositions_only !== false;
  const riskReady = riskAvailable && (
    riskApplicable
      ? riskSource.ready === true
        && riskRecordsValid
        && targetIdsProjectionOk
        && riskCountsConsistent
        && coverageComplete
        && dispositionsOnly
        && safetyFieldsOk(riskSource)
      : riskSource?.applicable === false
  );
  const attestation = firstRecord(snapshot.attestation);
  const attestationVersion = cleanText(
    snapshot.attestation_version
    || attestation?.version
    || attestation?.attestation_version,
    120,
  );
  const attestationSha256 = cleanText(
    snapshot.attestation_sha256
    || snapshot.governance_attestation_sha256
    || attestation?.sha256
    || attestation?.attestation_sha256,
    64,
  ).toLowerCase();
  const snapshotSha256 = cleanText(snapshot.snapshot_sha256, 64).toLowerCase();
  const attestationHashDeclared = hasOwn(snapshot, "attestation_sha256")
    || hasOwn(snapshot, "governance_attestation_sha256")
    || hasOwn(attestation, "sha256")
    || hasOwn(attestation, "attestation_sha256");
  const attestationVersionDeclared = hasOwn(snapshot, "attestation_version")
    || hasOwn(attestation, "version")
    || hasOwn(attestation, "attestation_version");
  const attestationShapeOk = (!attestationHashDeclared || SHA256_PATTERN.test(attestationSha256))
    && (!attestationVersionDeclared || Boolean(attestationVersion));
  const snapshotHashShapeOk = !snapshotSha256 || SHA256_PATTERN.test(snapshotSha256);
  const integrityOk = snapshot.integrity_ok !== false
    && snapshot.attestation_integrity_ok !== false
    && snapshot.hash_valid !== false
    && attestation?.integrity_ok !== false
    && attestation?.hash_valid !== false
    && attestationShapeOk
    && snapshotHashShapeOk;
  const applicable = snapshot.applicable !== false;
  const snapshotStatus = cleanText(snapshot.status).toLowerCase();
  const topLevelStatusReady = !snapshotStatus || snapshotStatus === "ready";
  const safetyOk = safetyFieldsOk(snapshot)
    && safetyFieldsOk(projection)
    && safetyFieldsOk(lineageSource)
    && safetyFieldsOk(riskSource);
  const artifactAlignmentOk = alignmentOk(snapshot.artifact_alignment);
  const ready = applicable
    && lineageReady
    && riskReady
    && integrityOk
    && safetyOk
    && artifactAlignmentOk
    && snapshot.ready !== false
    && topLevelStatusReady;
  const snapshotIssues = issueMessages(snapshot);
  if (!attestationShapeOk) snapshotIssues.push("治理 attestation 版本或 SHA-256 形状无效，已在本地降级。");
  if (!snapshotHashShapeOk) snapshotIssues.push("治理快照 SHA-256 形状无效，已在本地降级。");

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
    attestationVersion,
    attestationSha256,
    snapshotSha256,
    issues: [...new Set(snapshotIssues)],
    lineage: {
      available: lineageAvailable,
      applicable: lineageAvailable,
      ready: lineageReady,
      status: cleanText(lineageSource?.status) || (lineageReady ? "ready" : "blocked"),
      version: cleanText(lineageSource?.version),
      decisionMessageId: cleanText(lineageSource?.decision_message_id),
      candidates,
      issues: lineageIssues,
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
      actionCounts,
      reviews,
      issues: riskIssues,
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
