import { evidenceRoleLabels, verificationStatusLabels } from "./artifactEvidence.js";
import {
  ARTIFACT_GOVERNANCE_BOUNDARY,
  artifactCandidateGovernance,
} from "./candidateGovernance.js";
import {
  artifactDecisionState,
  artifactUserDecisionPresentation,
  formatUserDecisionTime,
  userDecisionLabel,
} from "./artifactUserDecision.js";

const sectionTitles = {
  requirements: "需求证据地图",
  risks: "项目风险登记",
  conclusions: "结论",
  disagreements: "分歧",
  unknowns: "待验证",
  actions: "待办",
};

const disagreementStatusLabels = {
  open: "待解决",
  resolved: "已解决",
  accepted_risk: "接受风险",
};

const actionStateLabels = {
  open: "待处理",
  in_progress: "进行中",
  blocked: "受阻",
  done: "已完成",
  cancelled: "已取消",
};

const requirementStatusLabels = {
  confirmed: "已确认需求",
  assumption: "工作假设",
  pending: "待补证据",
  rejected: "已排除",
};

const riskStatusLabels = {
  open: "待处理",
  monitoring: "监控中",
  mitigated: "已缓解",
  accepted: "接受风险",
};

const riskLevelLabels = { unknown: "未知", low: "低", medium: "中", high: "高" };

function timestampText(value) {
  const timestamp = Number(value);
  if (!Number.isFinite(timestamp) || timestamp <= 0) return "";
  return new Date(timestamp).toISOString();
}

function evidenceText(evidence = []) {
  if (!evidence.length) return "";
  return `\n  - 证据：${evidence.map((item) => {
    const role = evidenceRoleLabels[item.evidence_role] || "背景";
    const verification = verificationStatusLabels[item.verification_status] || "未核验";
    const note = item.review_note ? `，说明=${item.review_note}` : "";
    const version = item.version_status && item.version_status !== "current"
      ? `，版本状态=${item.version_status === "superseded" ? "存在新版" : item.version_status === "inactive" ? "来源停用" : "来源不可用"}，版本处理=${item.version_decision === "keep_snapshot" ? "保留历史快照" : "待处理"}`
      : "";
    const sourceLabel = item.type === "material"
      ? "资料"
      : item.type === "round_market_snapshot"
        ? "冻结市场快照"
        : "讨论消息";
    const sourceIdentity = item.type === "round_market_snapshot"
      ? `${item.snapshot_id || item.id}（轮次=${item.round_id || "未记录"}，证据版本=${item.source_revision || "未记录"}，SHA=${item.source_snapshot_sha256 || "未记录"}）`
      : `${item.id} v${item.version ?? 0}`;
    return `${sourceLabel} ${sourceIdentity}（用途=${role}，核验=${verification}${version}${note}）`;
  }).join("；")}`;
}

function appendCandidateLineageMarkdown(lines, governance) {
  lines.push("", "## 候选形成谱系", "", "- 层级：第一层 · 服务端只读候选来源");
  if (!governance.available) {
    lines.push("- 状态：此历史产物没有治理快照，不能推断候选形成谱系。");
    return;
  }
  if (!governance.applicable) {
    lines.push(`- 状态：${governance.issues[0] || "该产物未启用候选形成谱系治理；不会补写或推断来源。"}`);
    return;
  }
  const { lineage } = governance;
  if (!lineage.available) {
    lines.push("- 状态：治理快照未包含候选形成谱系，不能推断候选来源。");
    return;
  }
  lines.push(`- 状态：${lineage.ready ? "谱系完整" : "谱系不完整"}`);
  if (!governance.ready) lines.push("- 治理总状态：未通过完整性、版本绑定或无执行能力边界校验");
  if (governance.snapshotSha256) lines.push(`- 治理快照 SHA-256：${governance.snapshotSha256}`);
  if (governance.attestationSha256) lines.push(`- 治理封印 SHA-256：${governance.attestationSha256}`);
  if (lineage.version) lines.push(`- 协议：${lineage.version}`);
  lines.push(`- 决策消息：${lineage.decisionMessageId || "尚未绑定"}`);
  if (!lineage.candidates.length) {
    lines.push("- 暂无可展示候选");
  } else {
    for (const candidate of lineage.candidates) {
      lines.push(`- ${candidate.preferred ? "[决策板首选] " : ""}${candidate.title || candidate.id || "未命名候选"}`);
      lines.push(`  - 候选 ID：${candidate.id || "未记录"}`);
      lines.push(`  - 精确版本：${candidate.revision ? `r${candidate.revision}` : "未记录"}`);
      lines.push(`  - 形成消息：${candidate.originMessageId || "未记录"}`);
      lines.push(`  - 当前版本消息：${candidate.latestMessageId || "未记录"}`);
    }
  }
  for (const issue of lineage.issues) lines.push(`- 谱系缺口：${issue}`);
}

function appendRiskReviewMarkdown(lines, governance) {
  lines.push(
    "",
    "## 精确版本风控意见",
    "",
    "- 层级：第二层 · 服务端只读风控复核",
    `- 固定边界：${ARTIFACT_GOVERNANCE_BOUNDARY}`,
  );
  if (!governance.available) {
    lines.push("- 状态：此历史产物没有治理快照，不能补写或推断风控意见。");
    return;
  }
  if (!governance.applicable) {
    lines.push(`- 状态：${governance.issues[0] || "该产物未启用精确版本风控治理；不会补写或推断意见。"}`);
    return;
  }
  const { riskReview } = governance;
  if (!riskReview.available) {
    lines.push("- 状态：治理快照未包含精确版本风控记录，不能推断风控态度。");
    return;
  }
  if (!riskReview.applicable) {
    lines.push("- 状态：该轮次未启用精确版本风控复核协议；没有补写或猜测意见。");
    return;
  }
  lines.push(`- 状态：${riskReview.ready ? "版本复核完整" : "版本复核不完整"}`);
  if (riskReview.version) lines.push(`- 协议：${riskReview.version}`);
  lines.push(`- 当前版本覆盖：${riskReview.reviewedCandidateCount} / ${riskReview.targetCandidateCount} 个候选`);
  lines.push(`- 意见统计：当前 ${riskReview.currentReviewCount} / 过期 ${riskReview.staleReviewCount} / 处置总计（含过期）支持 ${riskReview.actionCounts.support} / 质疑 ${riskReview.actionCounts.challenge} / 拒绝 ${riskReview.actionCounts.reject}`);
  if (!riskReview.reviews.length) {
    lines.push("- 暂无可展示的精确版本风控意见");
  } else {
    for (const review of riskReview.reviews) {
      const candidateTitle = String(review.candidateSnapshot?.title || "").trim();
      lines.push(`- ${review.dispositionLabel} · ${candidateTitle || review.candidateId || "未命名候选"}`);
      lines.push(`  - 候选 ID：${review.candidateId || "未记录"}`);
      lines.push(`  - 绑定版本：${review.candidateRevision ? `r${review.candidateRevision}` : "未记录"}`);
      lines.push(`  - 版本状态：${review.status === "current" ? "当前精确版本" : review.status === "stale" ? `已过期，当前为 r${review.currentCandidateRevision || "?"}` : "未记录"}`);
      lines.push(`  - 复核消息：${review.reviewMessageId || "未记录"}`);
      lines.push(`  - 复核成员：${review.reviewerName || review.reviewerMemberId || "未记录"}${review.reviewerMemberVersion ? ` v${review.reviewerMemberVersion}` : ""}`);
      if (review.candidateSnapshotSha256) lines.push(`  - 候选快照 SHA-256：${review.candidateSnapshotSha256}`);
      if (review.riskIds.length) lines.push(`  - 关联风险：${review.riskIds.join("、")}`);
    }
  }
  for (const issue of riskReview.issues) lines.push(`- 风控复核缺口：${issue}`);
}

function appendUserDecisionMarkdown(lines, artifact) {
  const { current, latest } = artifactDecisionState(artifact);
  lines.push(
    "",
    "## 用户最终决定",
    "",
    "- 层级：第三层 · 唯一的用户决定记录",
    `- 边界说明：${ARTIFACT_GOVERNANCE_BOUNDARY}`,
    "- 说明：上方风控意见不会自动生成本节记录，也不授予任何执行能力。",
  );
  if (!current) {
    lines.push("- 当前决定：尚未记录当前版本的用户决定");
    if (latest) {
      const presentation = artifactUserDecisionPresentation(artifact, latest);
      lines.push(`- 此前决定（已过期）：${userDecisionLabel(latest)}`);
      lines.push(`  - 原绑定版本：v${latest.artifact_version || "未记录"}`);
      lines.push(`  - AI 首选：${presentation.aiPreferredLabel}`);
      if (presentation.action !== "support") {
        lines.push(`  - 我的选择：无（${userDecisionLabel(latest)}不表示支持候选）`);
      } else if (presentation.hasExplicitSelection) {
        lines.push(`  - 我的选择：${presentation.selectedOptionLabel}`);
        lines.push(`  - 与 AI 首选：${presentation.selectedIsAiPreferred ? "一致" : "不同"}`);
      } else {
        lines.push("  - 我的选择：旧版未单独记录；当时系统等同 AI 首选，不能证明人类做过单独候选选择");
      }
      lines.push(`  - 过期原因：${latest.stale_reason || "产物版本已经变化"}`);
    }
    return;
  }
  const presentation = artifactUserDecisionPresentation(artifact, current);
  lines.push(`- 当前决定：${userDecisionLabel(current)}`);
  lines.push(`- 绑定产物版本：v${current.artifact_version || artifact.version || 1}`);
  lines.push(`- AI 首选：${presentation.aiPreferredLabel}`);
  if (presentation.action !== "support") {
    lines.push(`- 我的选择：无（${userDecisionLabel(current)}不表示支持候选）`);
  } else if (presentation.hasExplicitSelection) {
    lines.push(`- 我的选择：${presentation.selectedOptionLabel}`);
    lines.push(`- 与 AI 首选：${presentation.selectedIsAiPreferred ? "一致" : "不同"}`);
  } else {
    lines.push("- 我的选择：旧版未单独记录；当时系统等同 AI 首选，不能证明人类做过单独候选选择");
  }
  lines.push(`- 决定理由：${current.rationale || "未填写"}`);
  lines.push(`- 记录时间：${formatUserDecisionTime(current.created_at)}`);
}

export function artifactToMarkdown(artifact, roomTitle = "AI 共创室") {
  const content = artifact.content || {};
  const lines = [
    `# ${artifact.title || "会议纪要"}`,
    "",
    `- 房间：${roomTitle}`,
    `- 状态：${artifact.status === "CONFIRMED" ? "用户已确认" : "草稿，尚未确认"}`,
    `- 版本：v${artifact.version || 1}`,
    `- 生成方式：${artifact.generation_source || "未知"}`,
    `- 产物 ID：${artifact.id || "未记录"}`,
    `- 轮次 ID：${artifact.round_id || "未绑定轮次"}`,
    `- 产物类型：${artifact.kind || "meeting_minutes"}`,
    `- 创建者：${artifact.created_by || "未知"}`,
    `- 确认者：${artifact.confirmed_by || "尚未确认"}`,
    ...(timestampText(artifact.created_at) ? [`- 创建时间：${timestampText(artifact.created_at)}`] : []),
    ...(timestampText(artifact.updated_at) ? [`- 更新时间：${timestampText(artifact.updated_at)}`] : []),
    ...(timestampText(artifact.confirmed_at) ? [`- 确认时间：${timestampText(artifact.confirmed_at)}`] : []),
    "",
    "## 摘要",
    "",
    content.summary || "尚未填写。",
  ];
  if (content.summary_evidence?.length) lines.push(evidenceText(content.summary_evidence).trimStart());
  const governance = artifactCandidateGovernance(artifact);
  appendCandidateLineageMarkdown(lines, governance);
  appendRiskReviewMarkdown(lines, governance);
  const decision = content.decision || {};
  const decisionStatusLabels = {
    candidate: "已形成候选首选",
    undecided: "尚未选择",
    deferred: "暂缓决策",
  };
  lines.push("", "## 多方案决策板", "");
  lines.push(`- 决策状态：${decisionStatusLabels[decision.status] || "尚未选择"}`);
  const preferred = (decision.options || []).find((option) => option.id === decision.preferred_option_id);
  lines.push(`- 首选候选：${preferred?.title || "未选择"}`);
  lines.push(`- 选择或暂缓理由：${decision.rationale || "尚未填写"}`);
  const decisionEvidence = evidenceText(decision.evidence).trim();
  if (decisionEvidence) lines.push(`  ${decisionEvidence}`);
  if (!(decision.options || []).length) {
    lines.push("- 暂无可比较方案");
  } else {
    for (const option of decision.options) {
      lines.push(`- ${option.id === decision.preferred_option_id ? "[首选候选] " : ""}${option.title}`);
      lines.push(`  - 方案 ID：${option.id || "未记录"}`);
      lines.push(`  - 内容：${option.description || "尚未填写"}`);
      for (const benefit of option.benefits || []) lines.push(`  - 收益：${benefit}`);
      for (const risk of option.risks || []) lines.push(`  - 风险：${risk}`);
      if (option.value) lines.push(`  - 预期价值：${option.value}`);
      if (option.cost) lines.push(`  - 资源 / 成本：${option.cost}`);
      if (option.timeline) lines.push(`  - 周期：${option.timeline}`);
      for (const dependency of option.dependencies || []) lines.push(`  - 依赖：${dependency}`);
      lines.push(`  - 可逆性：${riskLevelLabels[option.reversibility] || "未知"}`);
      const optionEvidence = evidenceText(option.evidence).trim();
      if (optionEvidence) lines.push(`  ${optionEvidence}`);
    }
  }
  appendUserDecisionMarkdown(lines, artifact);
  for (const section of Object.keys(sectionTitles)) {
    lines.push("", `## ${sectionTitles[section]}`, "");
    const items = content[section] || [];
    if (!items.length) {
      lines.push("- 暂无");
      continue;
    }
    for (const item of items) {
      lines.push(`- ${item.text}`);
      lines.push(`  - 条目 ID：${item.id || "未记录"}`);
      if (section === "disagreements" && item.positions?.length) {
        lines.push(...item.positions.map((position) => `  - 立场：${position}`));
      }
      if (section === "disagreements") {
        lines.push(
          `  - 处理状态：${disagreementStatusLabels[item.status] || item.status || "待解决"}`,
          `  - 是否阻塞候选方案：${typeof item.blocking === "boolean" ? item.blocking ? "是" : "否" : "未标注"}`,
          `  - 负责人：${item.owner || "待分配"}`,
          `  - 处理结论：${item.resolution || "尚未填写"}`,
        );
      }
      if (section === "actions") {
        lines.push(
          `  - 负责人：${item.owner || "待分配"}`,
          `  - 期限 / 里程碑：${item.due || "未设置"}`,
          `  - 执行状态：${actionStateLabels[item.state] || item.state || "待处理"}`,
        );
      }
      if (section === "requirements") {
        lines.push(
          `  - 证据状态：${requirementStatusLabels[item.status] || item.status || "待补证据"}`,
          `  - 确认负责人：${item.owner || "待确认"}`,
          `  - 验收条件：${item.acceptance_criteria || "尚未填写"}`,
        );
      }
      if (section === "risks") {
        lines.push(
          `  - 概率：${riskLevelLabels[item.probability] || "未知"}`,
          `  - 影响：${riskLevelLabels[item.impact] || "未知"}`,
          `  - 处理状态：${riskStatusLabels[item.status] || item.status || "待处理"}`,
          `  - 是否阻塞候选方案：${item.blocking === false ? "否" : "是"}`,
          `  - 触发信号：${item.trigger || "尚未填写"}`,
          `  - 缓解 / 接受说明：${item.mitigation || "尚未填写"}`,
          `  - 负责人：${item.owner || "待分配"}`,
        );
      }
      const evidence = evidenceText(item.evidence).trim();
      if (evidence) lines.push(`  ${evidence}`);
    }
  }
  if (artifact.status !== "CONFIRMED" && content.generation_notes) {
    lines.push("", "## 生成说明", "", content.generation_notes);
  }
  lines.push("", "---", "此文档来自 AI 共创室。用户确认表示确认会议记录及证据标注，不代表所有来源事实都已被独立证实。", "");
  return lines.join("\n");
}

export function downloadArtifactMarkdown(artifact, roomTitle) {
  const markdown = artifactToMarkdown(artifact, roomTitle);
  const blob = new Blob([markdown], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `${(artifact.title || "会议纪要").replace(/[\\/:*?"<>|]/g, "-")}.md`;
  anchor.click();
  URL.revokeObjectURL(url);
}
