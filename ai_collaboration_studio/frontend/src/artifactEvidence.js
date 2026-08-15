export const evidenceRoleLabels = {
  support: "支持",
  counter: "反证",
  context: "背景",
};

export const verificationStatusLabels = {
  unreviewed: "未核验",
  source_checked: "已核对原文",
  corroborated: "已交叉佐证",
  disputed: "存在争议",
};

const statusCaution = {
  corroborated: 0,
  source_checked: 1,
  unreviewed: 2,
  disputed: 3,
};

const roleCaution = {
  support: 0,
  context: 1,
  counter: 2,
};

export function evidenceKey(evidence) {
  return `${evidence.type}:${evidence.id}`;
}

export function auditDefaults(evidence = {}) {
  const evidenceRole = evidenceRoleLabels[evidence.evidence_role] ? evidence.evidence_role : "context";
  const verificationStatus = verificationStatusLabels[evidence.verification_status]
    ? evidence.verification_status
    : "unreviewed";
  const audit = {
    evidence_role: evidenceRole,
    verification_status: verificationStatus,
    review_note: String(evidence.review_note || ""),
  };
  const version = Number(evidence.version);
  const latestVersion = Number(evidence.latest_version);
  if (Number.isInteger(version) && version >= 0) audit.version = version;
  if (Number.isInteger(latestVersion) && latestVersion >= 0) audit.latest_version = latestVersion;
  if (["current", "superseded", "inactive", "unavailable"].includes(evidence.version_status)) {
    audit.version_status = evidence.version_status;
  }
  if (["current", "keep_snapshot", "review_required"].includes(evidence.version_decision)) {
    audit.version_decision = evidence.version_decision;
  }
  if (typeof evidence.source_active === "boolean") audit.source_active = evidence.source_active;
  if (/^[0-9a-f]{64}$/i.test(String(evidence.source_snapshot_sha256 || ""))) {
    audit.source_snapshot_sha256 = String(evidence.source_snapshot_sha256).toLowerCase();
  }
  if (evidence.source_revision) audit.source_revision = String(evidence.source_revision).slice(0, 160);
  return audit;
}

export function cautiousAudit(existing, incoming) {
  if (!existing) return auditDefaults(incoming);
  const next = auditDefaults(incoming);
  return {
    ...existing,
    evidence_role: roleCaution[next.evidence_role] > roleCaution[existing.evidence_role]
      ? next.evidence_role
      : existing.evidence_role,
    verification_status: statusCaution[next.verification_status] > statusCaution[existing.verification_status]
      ? next.verification_status
      : existing.verification_status,
    review_note: existing.review_note || next.review_note,
  };
}

export function collectEvidence(content = {}) {
  const refs = [...(content.summary_evidence || [])];
  for (const section of ["requirements", "risks", "conclusions", "disagreements", "unknowns", "actions"]) {
    for (const item of content[section] || []) refs.push(...(item.evidence || []));
  }
  const decision = content.decision || {};
  refs.push(...(decision.evidence || []));
  for (const option of decision.options || []) refs.push(...(option.evidence || []));
  return refs;
}

export function evidenceAuditSummary(content = {}) {
  const refs = collectEvidence(content);
  return refs.reduce((summary, ref) => {
    const audit = auditDefaults(ref);
    summary.total += 1;
    summary[audit.verification_status] += 1;
    summary[audit.evidence_role] += 1;
    return summary;
  }, {
    total: 0,
    unreviewed: 0,
    source_checked: 0,
    corroborated: 0,
    disputed: 0,
    support: 0,
    counter: 0,
    context: 0,
  });
}

export function artifactConfirmationIssues(content = {}) {
  const issues = [];
  const auditEvidence = (label, rawEvidence, requireSupport = false) => {
    const refs = Array.isArray(rawEvidence) ? rawEvidence : [];
    if (!refs.length) {
      issues.push(`${label}缺少证据`);
      return;
    }
    refs.forEach((ref, index) => {
      const audit = auditDefaults(ref);
      const suffix = `证据${index + 1}`;
      if (audit.verification_status === "unreviewed") issues.push(`${label}${suffix}尚未核验`);
      if (
        (audit.verification_status === "disputed" || audit.evidence_role === "counter")
        && !audit.review_note.trim()
      ) {
        issues.push(`${label}${suffix}缺少争议/反证说明`);
      }
      if (audit.version_status && audit.version_status !== "current") {
        if (audit.version_decision !== "keep_snapshot") issues.push(`${label}${suffix}需要处理来源版本变化`);
        else if (!audit.review_note.trim()) issues.push(`${label}${suffix}保留历史快照时必须说明原因`);
      }
    });
    if (requireSupport && !refs.some((ref) => {
      const audit = auditDefaults(ref);
      return audit.evidence_role === "support"
        && ["source_checked", "corroborated"].includes(audit.verification_status);
    })) {
      issues.push(`${label}缺少已核对的支持证据`);
    }
  };

  if (String(content.summary || "").trim()) auditEvidence("会议摘要", content.summary_evidence, true);
  const sectionLabels = {
    requirements: "需求证据",
    risks: "项目风险",
    conclusions: "结论",
    disagreements: "分歧",
    unknowns: "待验证",
    actions: "待办",
  };
  Object.entries(sectionLabels).forEach(([section, label]) => {
    (Array.isArray(content[section]) ? content[section] : []).forEach((item, index) => {
      const requireSupport = section === "conclusions"
        || section === "actions"
        || (section === "requirements" && item?.status === "confirmed");
      auditEvidence(`${label}${index + 1}`, item?.evidence, requireSupport);
    });
  });
  const decision = content.decision && typeof content.decision === "object" ? content.decision : {};
  (Array.isArray(decision.options) ? decision.options : []).forEach((option, index) => {
    auditEvidence(`候选方案${index + 1}`, option?.evidence, true);
  });
  if (decision.status === "candidate") auditEvidence("首选方案选择", decision.evidence, true);
  else if (decision.status === "deferred") auditEvidence("暂缓决策", decision.evidence, true);
  return issues;
}

export function artifactEvidenceReviewSummary(content = {}) {
  const refs = collectEvidence(content);
  const sourceUsage = {};
  let unreviewed = 0;
  refs.forEach((ref) => {
    const key = evidenceKey(ref);
    sourceUsage[key] = (sourceUsage[key] || 0) + 1;
    if (auditDefaults(ref).verification_status === "unreviewed") unreviewed += 1;
  });
  return {
    relationCount: refs.length,
    uniqueSourceCount: Object.keys(sourceUsage).length,
    reviewedCount: refs.length - unreviewed,
    unreviewedCount: unreviewed,
    sourceUsage,
    issues: artifactConfirmationIssues(content),
  };
}
