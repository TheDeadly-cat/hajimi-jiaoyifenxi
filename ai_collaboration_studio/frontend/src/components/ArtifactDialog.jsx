import {
  AlertTriangle,
  CheckCircle2,
  CirclePause,
  Download,
  History,
  Plus,
  RotateCcw,
  Save,
  ThumbsUp,
  Trash2,
  X,
} from "lucide-react";
import { Suspense, lazy, memo, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import {
  artifactEditorDisplayText,
  artifactEditorErrorMessage,
  artifactEditorFieldText,
  artifactEditorIdentity,
  artifactEditorMutationControl,
  artifactEditorRecord,
  artifactEditorRows,
  artifactEditorSavedState,
  artifactEditorSourceState,
} from "../artifactEditorUi";
import "../styles/artifact-dialog.css";
import { useModalFocus } from "../useModalFocus";
import { ArtifactCandidateGovernance } from "./ArtifactCandidateGovernance";
import { CandidateExperimentPanel } from "./CandidateExperimentPanel";
import { ArtifactEvidenceGraph } from "./ArtifactEvidenceGraph";
import { ArtifactEvidenceReview } from "./ArtifactEvidenceReview";
import { ArtifactVersionHistory } from "./ArtifactVersionHistory";
import {
  artifactEvidenceReviewSummary,
  auditDefaults,
  cautiousAudit,
  collectEvidence,
  evidenceKey,
} from "../artifactEvidence";
import {
  buildArtifactEvidenceCandidates,
  evidenceRelationFlags,
  evidenceSourceDetailKey,
  evidenceSourceIdentityMatches,
  normalizeArtifactEvidenceDetailResponse,
  normalizeArtifactEvidenceResponse,
} from "../artifactEvidenceSources";
import { normalizeArtifactEvidenceGraph } from "../artifactEvidenceGraph";
import {
  artifactDecisionState,
  artifactUserDecisionPresentation,
  artifactUserDecisionSelection,
  formatUserDecisionTime,
  USER_DECISION_ACTIONS,
  userDecisionLabel,
} from "../artifactUserDecision";
import {
  hasProjectWorkspaceFootprint,
  HOST_CONTRIBUTION_IDS,
  HOST_SLOT_IDS,
  resolveHostOwnedSlot,
  resolvedHostContribution,
} from "../capabilityContributions";

const ProjectReadinessPanel = lazy(() => import("./ProjectReadinessPanel.jsx").then((module) => ({
  default: module.ProjectReadinessPanel,
})));

function ProjectReadinessFallback({ artifactVersion }) {
  return (
    <section
      className="project-readiness-panel"
      aria-label="项目实施前结构复核（只读）"
      aria-busy="true"
    >
      <p className="project-readiness-state" role="status" aria-live="polite">
        正在加载绑定 v{artifactVersion || "?"} 的只读结构复核模块……
      </p>
      <p className="project-readiness-boundary">
        仅加载确定性展示模块；不产生 Provider 调用、市场读取、业务写入或授权结论。
      </p>
    </section>
  );
}

const disagreementStatuses = [
  ["open", "待解决"],
  ["resolved", "已解决"],
  ["accepted_risk", "接受风险"],
];

const actionStates = [
  ["open", "待处理"],
  ["in_progress", "进行中"],
  ["blocked", "受阻"],
  ["done", "已完成"],
];

const decisionStatuses = [
  ["undecided", "尚未选择"],
  ["candidate", "已形成候选首选"],
  ["deferred", "暂缓决策"],
];

const requirementStatuses = [
  ["confirmed", "已确认需求"],
  ["assumption", "工作假设"],
  ["pending", "待补证据"],
  ["rejected", "已排除"],
];

const riskStatuses = [
  ["open", "待处理"],
  ["monitoring", "监控中"],
  ["mitigated", "已缓解"],
  ["accepted", "接受风险"],
];

const riskLevels = [
  ["unknown", "未知"],
  ["low", "低"],
  ["medium", "中"],
  ["high", "高"],
];

const reversibilityLevels = [
  ["unknown", "未知"],
  ["low", "低"],
  ["medium", "中"],
  ["high", "高"],
];

const simpleSections = [
  ["conclusions", "结论", "记录已得到讨论支持的结论"],
  ["unknowns", "待验证", "记录证据缺口或仍需核验的事项"],
];

const validDisagreementStatuses = new Set(disagreementStatuses.map(([value]) => value));
const validActionStates = new Set(actionStates.map(([value]) => value));
const validDecisionStatuses = new Set(decisionStatuses.map(([value]) => value));
const validRequirementStatuses = new Set(requirementStatuses.map(([value]) => value));
const validRiskStatuses = new Set(riskStatuses.map(([value]) => value));
const validRiskLevels = new Set(riskLevels.map(([value]) => value));
const validReversibilityLevels = new Set(reversibilityLevels.map(([value]) => value));

const userDecisionIcons = {
  support: ThumbsUp,
  hold: CirclePause,
  return: RotateCcw,
};

let clientItemSequence = 0;

function newItemId(section) {
  clientItemSequence += 1;
  const randomPart = globalThis.crypto?.randomUUID?.().replaceAll("-", "").slice(0, 12)
    || `${Date.now().toString(36)}${clientItemSequence.toString(36)}`;
  return `item_${section}_${randomPart}`;
}

function normalizedItemId(section, item, seenIds) {
  const candidate = String(item?.id || "");
  if (/^[a-zA-Z0-9_-]{1,80}$/.test(candidate) && !seenIds.has(candidate)) {
    seenIds.add(candidate);
    return candidate;
  }
  const generated = newItemId(section);
  seenIds.add(generated);
  return generated;
}

function normalizeSectionItems(section, rawItems = []) {
  const seenIds = new Set();
  return artifactEditorRows(rawItems).map((rawItem) => {
    const item = typeof rawItem === "string" ? { text: rawItem } : rawItem || {};
    const id = normalizedItemId(section, item, seenIds);
    const base = {
      id,
      text: artifactEditorFieldText(item.text),
      initialEvidence: Array.isArray(item.evidence) ? item.evidence : [],
    };
    if (section === "disagreements") {
      return {
        ...base,
        positions: artifactEditorRows(item.positions).map((position) => artifactEditorFieldText(position)).filter(Boolean),
        status: validDisagreementStatuses.has(item.status) ? item.status : "open",
        blocking: typeof item.blocking === "boolean" ? item.blocking : true,
        owner: artifactEditorFieldText(item.owner, "待分配") || "待分配",
        resolution: artifactEditorFieldText(item.resolution),
      };
    }
    if (section === "actions") {
      return {
        ...base,
        owner: artifactEditorFieldText(item.owner, "待分配") || "待分配",
        due: artifactEditorFieldText(item.due),
        state: validActionStates.has(item.state) ? item.state : "open",
      };
    }
    if (section === "requirements") {
      return {
        ...base,
        status: validRequirementStatuses.has(item.status) ? item.status : "pending",
        owner: artifactEditorFieldText(item.owner, "待确认") || "待确认",
        acceptance_criteria: artifactEditorFieldText(item.acceptance_criteria),
      };
    }
    if (section === "risks") {
      return {
        ...base,
        status: validRiskStatuses.has(item.status) ? item.status : "open",
        probability: validRiskLevels.has(item.probability) ? item.probability : "unknown",
        impact: validRiskLevels.has(item.impact) ? item.impact : "unknown",
        blocking: typeof item.blocking === "boolean" ? item.blocking : true,
        trigger: artifactEditorFieldText(item.trigger),
        mitigation: artifactEditorFieldText(item.mitigation),
        owner: artifactEditorFieldText(item.owner, "待分配") || "待分配",
      };
    }
    return base;
  });
}

function normalizeDecision(rawDecision = {}) {
  rawDecision = artifactEditorRecord(rawDecision);
  const seenIds = new Set();
  const preferredRaw = String(rawDecision.preferred_option_id || "");
  let preferredOptionId = "";
  const options = (Array.isArray(rawDecision.options) ? rawDecision.options : []).map((rawOption) => {
    const option = rawOption && typeof rawOption === "object" ? rawOption : {};
    const rawId = String(option.id || "");
    const id = normalizedItemId("decision_options", option, seenIds);
    if (rawId === preferredRaw && !preferredOptionId) preferredOptionId = id;
    return {
      id,
      title: artifactEditorFieldText(option.title),
      description: artifactEditorFieldText(option.description || option.text),
      benefits: artifactEditorRows(option.benefits).map((item) => artifactEditorFieldText(item)).filter(Boolean),
      risks: artifactEditorRows(option.risks).map((item) => artifactEditorFieldText(item)).filter(Boolean),
      value: artifactEditorFieldText(option.value),
      cost: artifactEditorFieldText(option.cost),
      timeline: artifactEditorFieldText(option.timeline),
      dependencies: artifactEditorRows(option.dependencies).map((item) => artifactEditorFieldText(item)).filter(Boolean),
      reversibility: validReversibilityLevels.has(option.reversibility) ? option.reversibility : "unknown",
      initialEvidence: Array.isArray(option.evidence) ? option.evidence : [],
    };
  });
  return {
    status: validDecisionStatuses.has(rawDecision.status) ? rawDecision.status : "undecided",
    options,
    preferred_option_id: preferredOptionId,
    rationale: artifactEditorFieldText(rawDecision.rationale),
    initialEvidence: Array.isArray(rawDecision.evidence) ? rawDecision.evidence : [],
  };
}

function targetKey(section, itemId) {
  return `${section}:${itemId}`;
}

function createEditorState(artifact) {
  artifact = artifactEditorSourceState(artifact).artifact;
  const content = artifactEditorRecord(artifact.content);
  const normalizedDecision = normalizeDecision(content.decision);
  const normalizedSections = {
    requirements: normalizeSectionItems("requirements", content.requirements),
    risks: normalizeSectionItems("risks", content.risks),
    conclusions: normalizeSectionItems("conclusions", content.conclusions),
    disagreements: normalizeSectionItems("disagreements", content.disagreements),
    unknowns: normalizeSectionItems("unknowns", content.unknowns),
    actions: normalizeSectionItems("actions", content.actions),
  };
  const evidenceByTarget = { summary: {} };
  const addRefs = (key, refs) => {
    const target = evidenceByTarget[key] || {};
    for (const ref of refs || []) {
      const keyForRef = evidenceKey(ref);
      target[keyForRef] = cautiousAudit(target[keyForRef], ref);
    }
    evidenceByTarget[key] = target;
  };
  addRefs("summary", content.summary_evidence);
  addRefs("decision", normalizedDecision.initialEvidence);
  normalizedDecision.options.forEach((option) => {
    addRefs(targetKey("decision_options", option.id), option.initialEvidence);
  });
  for (const [section, items] of Object.entries(normalizedSections)) {
    items.forEach((item) => {
      addRefs(targetKey(section, item.id), item.initialEvidence);
    });
  }
  const sections = Object.fromEntries(
    Object.entries(normalizedSections).map(([section, items]) => [
      section,
      items.map(({ initialEvidence: _initialEvidence, ...item }) => item),
    ]),
  );
  return {
    title: artifactEditorFieldText(artifact.title, "会议纪要") || "会议纪要",
    summary: artifactEditorFieldText(content.summary),
    ...sections,
    decision: {
      ...normalizedDecision,
      options: normalizedDecision.options.map(({ initialEvidence: _initialEvidence, ...option }) => option),
      initialEvidence: undefined,
    },
    evidenceByTarget,
    reviewTarget: "summary",
  };
}

function buildArtifact(artifact, editor, evidenceCandidates) {
  const candidateByKey = new Map(evidenceCandidates.map((item) => [evidenceKey(item), item]));
  const evidenceFor = (key) => Object.entries(editor.evidenceByTarget[key] || {}).flatMap(([sourceKey, audit]) => {
    const candidate = candidateByKey.get(sourceKey);
    if (!candidate) return [];
    const source = {
      type: candidate.type,
      id: candidate.id,
      version: candidate.version || 0,
    };
    if (candidate.source_snapshot_sha256) {
      source.source_snapshot_sha256 = candidate.source_snapshot_sha256;
    }
    if (candidate.type === "round_market_snapshot") {
      Object.assign(source, {
        round_id: candidate.round_id || artifact.round_id || "",
        snapshot_id: candidate.snapshot_id || candidate.id,
        source_revision: candidate.source_revision || "",
        source_snapshot_sha256: candidate.source_snapshot_sha256 || "",
        captured_at: candidate.captured_at || "",
      });
    }
    return [{ ...source, ...auditDefaults(audit) }];
  });
  const content = {
    ...(artifact.content || {}),
    summary: editor.summary.trim(),
    summary_evidence: evidenceFor("summary"),
    requirements: editor.requirements.map((item) => ({
      id: item.id,
      text: item.text.trim(),
      status: validRequirementStatuses.has(item.status) ? item.status : "pending",
      owner: item.owner.trim() || "待确认",
      acceptance_criteria: item.acceptance_criteria.trim(),
      evidence: evidenceFor(targetKey("requirements", item.id)),
    })).filter((item) => item.text),
    risks: editor.risks.map((item) => ({
      id: item.id,
      text: item.text.trim(),
      status: validRiskStatuses.has(item.status) ? item.status : "open",
      probability: validRiskLevels.has(item.probability) ? item.probability : "unknown",
      impact: validRiskLevels.has(item.impact) ? item.impact : "unknown",
      blocking: Boolean(item.blocking),
      trigger: item.trigger.trim(),
      mitigation: item.mitigation.trim(),
      owner: item.owner.trim() || "待分配",
      evidence: evidenceFor(targetKey("risks", item.id)),
    })).filter((item) => item.text),
    conclusions: editor.conclusions.map((item) => ({
      id: item.id,
      text: item.text.trim(),
      evidence: evidenceFor(targetKey("conclusions", item.id)),
    })).filter((item) => item.text),
    disagreements: editor.disagreements.map((item) => ({
      id: item.id,
      text: item.text.trim(),
      positions: item.positions.map((position) => position.trim()).filter(Boolean),
      status: validDisagreementStatuses.has(item.status) ? item.status : "open",
      blocking: Boolean(item.blocking),
      owner: item.owner.trim() || "待分配",
      resolution: item.resolution.trim(),
      evidence: evidenceFor(targetKey("disagreements", item.id)),
    })).filter((item) => item.text),
    unknowns: editor.unknowns.map((item) => ({
      id: item.id,
      text: item.text.trim(),
      evidence: evidenceFor(targetKey("unknowns", item.id)),
    })).filter((item) => item.text),
    actions: editor.actions.map((item) => ({
      id: item.id,
      text: item.text.trim(),
      owner: item.owner.trim() || "待分配",
      due: item.due.trim(),
      state: validActionStates.has(item.state) ? item.state : "open",
      evidence: evidenceFor(targetKey("actions", item.id)),
    })).filter((item) => item.text),
    decision: {
      status: validDecisionStatuses.has(editor.decision.status) ? editor.decision.status : "undecided",
      options: editor.decision.options.map((option) => ({
        id: option.id,
        title: option.title.trim(),
        description: option.description.trim(),
        benefits: option.benefits.map((item) => item.trim()).filter(Boolean),
        risks: option.risks.map((item) => item.trim()).filter(Boolean),
        value: option.value.trim(),
        cost: option.cost.trim(),
        timeline: option.timeline.trim(),
        dependencies: option.dependencies.map((item) => item.trim()).filter(Boolean),
        reversibility: validReversibilityLevels.has(option.reversibility) ? option.reversibility : "unknown",
        evidence: evidenceFor(targetKey("decision_options", option.id)),
      })).filter((option) => option.title && option.description),
      preferred_option_id: editor.decision.preferred_option_id,
      rationale: editor.decision.rationale.trim(),
      evidence: evidenceFor("decision"),
    },
  };
  return { ...artifact, title: editor.title.trim(), content };
}

function SectionHeader({ title, help, onAdd }) {
  return (
    <div className="artifact-structured-heading">
      <span><strong>{title}</strong><small>{help}</small></span>
      <button type="button" className="secondary compact" onClick={onAdd}><Plus aria-hidden="true" size={13} />添加</button>
    </div>
  );
}

function RemoveItemButton({ label, onRemove }) {
  return (
    <button type="button" className="artifact-item-remove" aria-label={`删除${label}`} title={`删除${label}`} onClick={onRemove}>
      <Trash2 aria-hidden="true" size={13} />
    </button>
  );
}

function SimpleItemEditor({ section, label, item, onChange, onRemove, onReview }) {
  return (
    <article className="artifact-item-card">
      <div className="artifact-item-header">
        <button type="button" className="artifact-item-title" onClick={onReview}>为此条目核验证据</button>
        <RemoveItemButton label={label} onRemove={onRemove} />
      </div>
      <label>内容
        <textarea value={item.text} onChange={(event) => onChange(section, item.id, { text: event.target.value })} />
      </label>
    </article>
  );
}

function DisagreementEditor({ item, onChange, onRemove, onReview }) {
  return (
    <article className="artifact-item-card">
      <div className="artifact-item-header">
        <button type="button" className="artifact-item-title" onClick={onReview}>为此分歧核验证据</button>
        <RemoveItemButton label="分歧" onRemove={onRemove} />
      </div>
      <label>分歧主题
        <textarea value={item.text} onChange={(event) => onChange("disagreements", item.id, { text: event.target.value })} />
      </label>
      <label>各方立场（每行一条）
        <textarea
          value={item.positions.join("\n")}
          onChange={(event) => onChange("disagreements", item.id, {
            positions: event.target.value.split("\n"),
          })}
        />
      </label>
      <div className="artifact-item-grid">
        <label>处理状态
          <select value={item.status} onChange={(event) => onChange("disagreements", item.id, { status: event.target.value })}>
            {disagreementStatuses.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
          </select>
        </label>
        <label>负责人
          <input value={item.owner} onChange={(event) => onChange("disagreements", item.id, { owner: event.target.value })} />
        </label>
        <label className="artifact-blocking-toggle">
          <input
            type="checkbox"
            checked={item.blocking}
            onChange={(event) => onChange("disagreements", item.id, { blocking: event.target.checked })}
          />
          阻塞候选方案
        </label>
      </div>
      <label>处理结论 / 接受风险说明
        <textarea
          value={item.resolution}
          onChange={(event) => onChange("disagreements", item.id, { resolution: event.target.value })}
          placeholder={item.status === "open" ? "待解决时可记录下一步验证方式" : "说明如何解决，或为何接受该风险"}
        />
      </label>
    </article>
  );
}

function ActionEditor({ item, onChange, onRemove, onReview }) {
  return (
    <article className="artifact-item-card">
      <div className="artifact-item-header">
        <button type="button" className="artifact-item-title" onClick={onReview}>为此待办核验证据</button>
        <RemoveItemButton label="待办" onRemove={onRemove} />
      </div>
      <label>待办内容
        <textarea value={item.text} onChange={(event) => onChange("actions", item.id, { text: event.target.value })} />
      </label>
      <div className="artifact-item-grid">
        <label>负责人
          <input value={item.owner} onChange={(event) => onChange("actions", item.id, { owner: event.target.value })} />
        </label>
        <label>期限 / 里程碑
          <input value={item.due} onChange={(event) => onChange("actions", item.id, { due: event.target.value })} placeholder="YYYY-MM-DD 或里程碑" />
        </label>
        <label>状态
          <select value={item.state} onChange={(event) => onChange("actions", item.id, { state: event.target.value })}>
            {actionStates.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
          </select>
        </label>
      </div>
    </article>
  );
}

function RequirementEditor({ item, onChange, onRemove, onReview }) {
  return (
    <article className="artifact-item-card project-requirement-card">
      <div className="artifact-item-header">
        <button type="button" className="artifact-item-title" onClick={onReview}>核验此需求的来源</button>
        <RemoveItemButton label="需求证据" onRemove={onRemove} />
      </div>
      <label>需求、事实或假设
        <textarea value={item.text} onChange={(event) => onChange("requirements", item.id, { text: event.target.value })} />
      </label>
      <div className="artifact-item-grid">
        <label>证据状态
          <select value={item.status} onChange={(event) => onChange("requirements", item.id, { status: event.target.value })}>
            {requirementStatuses.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
          </select>
        </label>
        <label>确认负责人
          <input value={item.owner} onChange={(event) => onChange("requirements", item.id, { owner: event.target.value })} />
        </label>
      </div>
      <label>可测试验收条件
        <textarea value={item.acceptance_criteria} onChange={(event) => onChange("requirements", item.id, { acceptance_criteria: event.target.value })} placeholder="什么可观察结果能证明此项已经满足？" />
      </label>
    </article>
  );
}

function RiskEditor({ item, onChange, onRemove, onReview }) {
  return (
    <article className={`artifact-item-card project-risk-card ${item.blocking ? "blocking" : ""}`}>
      <div className="artifact-item-header">
        <button type="button" className="artifact-item-title" onClick={onReview}>核验此风险的依据</button>
        <RemoveItemButton label="项目风险" onRemove={onRemove} />
      </div>
      <label>风险描述
        <textarea value={item.text} onChange={(event) => onChange("risks", item.id, { text: event.target.value })} />
      </label>
      <div className="artifact-item-grid project-risk-grid">
        <label>发生概率
          <select value={item.probability} onChange={(event) => onChange("risks", item.id, { probability: event.target.value })}>
            {riskLevels.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
          </select>
        </label>
        <label>影响
          <select value={item.impact} onChange={(event) => onChange("risks", item.id, { impact: event.target.value })}>
            {riskLevels.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
          </select>
        </label>
        <label>处理状态
          <select value={item.status} onChange={(event) => onChange("risks", item.id, { status: event.target.value })}>
            {riskStatuses.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
          </select>
        </label>
        <label>负责人
          <input value={item.owner} onChange={(event) => onChange("risks", item.id, { owner: event.target.value })} />
        </label>
        <label className="artifact-blocking-toggle">
          <input type="checkbox" checked={item.blocking} onChange={(event) => onChange("risks", item.id, { blocking: event.target.checked })} />
          阻塞候选方案
        </label>
      </div>
      <div className="artifact-item-grid">
        <label>触发信号
          <textarea value={item.trigger} onChange={(event) => onChange("risks", item.id, { trigger: event.target.value })} placeholder="出现什么信号表示风险正在发生？" />
        </label>
        <label>缓解动作 / 接受说明
          <textarea value={item.mitigation} onChange={(event) => onChange("risks", item.id, { mitigation: event.target.value })} />
        </label>
      </div>
    </article>
  );
}

function DecisionMatrixPreview({ options, preferredOptionId }) {
  if (!options.length) return null;
  const reversibilityLabel = Object.fromEntries(reversibilityLevels);
  return (
    <div className="artifact-decision-matrix" role="region" aria-label="候选方案共同维度矩阵">
      <table>
        <thead><tr><th>方案</th><th>价值</th><th>成本</th><th>周期</th><th>关键依赖</th><th>可逆性</th></tr></thead>
        <tbody>{options.map((option) => (
          <tr className={option.id === preferredOptionId ? "preferred" : ""} key={option.id}>
            <th>{option.title || "未命名方案"}{option.id === preferredOptionId ? " · 首选" : ""}</th>
            <td>{option.value || "待评估"}</td>
            <td>{option.cost || "待评估"}</td>
            <td>{option.timeline || "待评估"}</td>
            <td>{option.dependencies.filter(Boolean).join("、") || "待识别"}</td>
            <td>{reversibilityLabel[option.reversibility] || "未知"}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function DecisionOptionEditor({ item, selected, structured, structuredReadOnly, onSelect, onChange, onRemove, onReview }) {
  return (
    <article className={`artifact-item-card decision-option-card ${selected ? "selected" : ""}`}>
      <div className="artifact-item-header">
        <label className="artifact-blocking-toggle">
          <input type="radio" name="preferred-decision-option" checked={selected} onChange={onSelect} />
          设为首选候选
        </label>
        <span>
          <button type="button" className="artifact-item-title" onClick={onReview}>核验此方案证据</button>
          <RemoveItemButton label="候选方案" onRemove={onRemove} />
        </span>
      </div>
      <label>方案名称
        <input value={item.title} onChange={(event) => onChange({ title: event.target.value })} />
      </label>
      <label>方案内容
        <textarea value={item.description} onChange={(event) => onChange({ description: event.target.value })} />
      </label>
      <div className="artifact-item-grid">
        <label>主要收益（每行一条）
          <textarea
            value={item.benefits.join("\n")}
            onChange={(event) => onChange({ benefits: event.target.value.split("\n") })}
          />
        </label>
        <label>主要风险（每行一条）
          <textarea
            value={item.risks.join("\n")}
            onChange={(event) => onChange({ risks: event.target.value.split("\n") })}
          />
        </label>
      </div>
      {structured ? <fieldset className="artifact-option-structured-fields" disabled={structuredReadOnly}>
        <div className="artifact-item-grid">
          <label>预期价值
            <textarea value={item.value} onChange={(event) => onChange({ value: event.target.value })} />
          </label>
          <label>资源 / 成本
            <textarea value={item.cost} onChange={(event) => onChange({ cost: event.target.value })} />
          </label>
          <label>周期
            <textarea value={item.timeline} onChange={(event) => onChange({ timeline: event.target.value })} />
          </label>
        </div>
        <div className="artifact-item-grid">
          <label>关键依赖（每行一条）
            <textarea value={item.dependencies.join("\n")} onChange={(event) => onChange({ dependencies: event.target.value.split("\n") })} />
          </label>
          <label>可逆性
            <select value={item.reversibility} onChange={(event) => onChange({ reversibility: event.target.value })}>
              {reversibilityLevels.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
            </select>
          </label>
        </div>
      </fieldset> : null}
    </article>
  );
}

function userDecisionBindingText(artifact, decision) {
  const presentation = artifactUserDecisionPresentation(artifact, decision);
  const aiPreferred = `AI 首选：${presentation.aiPreferredLabel}`;
  if (presentation.action !== "support") {
    return `${aiPreferred} · 我的选择：无（${userDecisionLabel(decision)}不表示支持候选）`;
  }
  if (!presentation.hasExplicitSelection) {
    return `${aiPreferred} · 我的选择：旧版未单独记录（当时系统等同 AI 首选）`;
  }
  const comparison = presentation.selectedIsAiPreferred
    ? "与 AI 首选一致"
    : "不同于 AI 首选";
  return `${aiPreferred} · 我的选择：${presentation.selectedOptionLabel}（${comparison}）`;
}

function UserFinalDecisionSection({ artifact, onSubmit }) {
  const [rationale, setRationale] = useState("");
  const [selectedOptionId, setSelectedOptionId] = useState("");
  const [busyAction, setBusyAction] = useState("");
  const [decisionError, setDecisionError] = useState("");
  const decisionTitleId = useId();
  const decisionInFlightRef = useRef(false);
  const { current, latest, stale, history } = artifactDecisionState(artifact);
  const displayedDecision = current || latest;
  const selection = useMemo(() => artifactUserDecisionSelection(artifact), [artifact]);
  const selectedCandidate = selection.candidates.find(
    (candidate) => candidate.id === selectedOptionId,
  ) || null;
  const cleanRationale = rationale.trim();
  const currentConfirmationValid = artifact.evidence_review?.confirmation_ready !== false;
  const canSubmit = typeof onSubmit === "function";

  const submitDecision = async (action) => {
    if (!cleanRationale || busyAction || decisionInFlightRef.current) return;
    if (!canSubmit) {
      setDecisionError(() => "当前未提供最终决定提交处理器，不能提交。");
      return;
    }
    if (!selection.decisionReady) {
      setDecisionError(() => selection.decisionReason || "当前治理记录未就绪，不能记录最终决定。");
      return;
    }
    if (action === "support" && !selection.ready) {
      setDecisionError(() => selection.reason || "当前候选状态不可用于支持决定。");
      return;
    }
    if (action === "support" && !selectedCandidate) {
      setDecisionError(() => "支持候选前，请明确选择一个当前治理候选。");
      return;
    }
    decisionInFlightRef.current = true;
    setBusyAction(() => action);
    setDecisionError(() => "");
    try {
      await onSubmit(
        artifact,
        action,
        cleanRationale,
        action === "support" ? selectedCandidate.id : "",
      );
      setRationale(() => "");
      setSelectedOptionId(() => "");
    } catch (requestError) {
      setDecisionError(() => artifactEditorErrorMessage(requestError, "最终决定提交失败，请稍后重试。"));
    } finally {
      decisionInFlightRef.current = false;
      setBusyAction(() => "");
    }
  };

  return (
    <section className="artifact-final-decision" aria-labelledby={decisionTitleId}>
      <div className="artifact-final-decision-heading">
        <span>
          <strong id={decisionTitleId}>第三层 · 用户最终决定</strong>
          <small>这是唯一的用户决定记录；上方风控“支持 / 质疑 / 拒绝”不会生成用户决定，也不代表批准、否决或执行授权。</small>
        </span>
        <em>绑定已确认版本 v{artifact.version}</em>
      </div>

      {current ? (
        <div className={`artifact-user-decision-current ${current.action || "unknown"}`}>
          <CheckCircle2 aria-hidden="true" size={16} />
          <span>
            <strong>当前决定：{userDecisionLabel(current)}</strong>
            <small>
              绑定 v{current.artifact_version || artifact.version}
              {` · ${formatUserDecisionTime(current.created_at)}`}
            </small>
            <small>{userDecisionBindingText(artifact, current)}</small>
            <p>{current.rationale}</p>
          </span>
        </div>
      ) : (
        <div className="artifact-user-decision-empty">
          <CirclePause aria-hidden="true" size={16} />
          <span><strong>尚未记录当前版本的最终决定</strong><small>请先说明理由，再选择下面一种态度。</small></span>
        </div>
      )}

      {!current && displayedDecision ? (
        <div className="artifact-user-decision-stale" role="status">
          <AlertTriangle aria-hidden="true" size={15} />
          <span>
            <strong>此前决定已过期：{userDecisionLabel(displayedDecision)}</strong>
            <small>
              原绑定 v{displayedDecision.artifact_version || "?"} · {displayedDecision.stale_reason || "产物版本已经变化，请重新判断。"}
            </small>
            <small>{userDecisionBindingText(artifact, displayedDecision)}</small>
          </span>
        </div>
      ) : null}

      <div className="artifact-user-decision-candidates">
        <div className="artifact-user-decision-candidates-heading">
          <span>
            <strong>支持候选时，明确选择你的方案</strong>
            <small>
              {selection.governed
                ? "AI 首选只是建议。你的选择可以不同，但必须绑定下面经过治理的精确候选版本。"
                : selection.explicitNonApplicable
                  ? "AI 首选只是建议。此产物明确不适用候选治理，选择只绑定已确认产物，不附加谱系或治理证明。"
                  : "治理状态未知或已经漂移，当前候选不可用于记录最终决定。"}
            </small>
          </span>
          <em>
            {selection.candidates.length} 个
            {selection.governed ? "治理" : selection.explicitNonApplicable ? "产物" : "可用"}候选
          </em>
        </div>
        {selection.candidates.length ? (
          <fieldset
            className="artifact-user-decision-candidate-list"
            aria-label="可供用户支持的当前候选"
            disabled={Boolean(busyAction) || !selection.ready}
          >
            {selection.candidates.map((candidate, candidateIndex) => {
              const selected = selectedOptionId === candidate.id;
              return (
                <label
                  className={`artifact-user-decision-candidate${candidate.aiPreferred ? " ai-preferred" : ""}${selected ? " selected" : ""}`}
                  key={JSON.stringify([candidate.id, candidate.revision, candidateIndex])}
                >
                  <input
                    type="radio"
                    name={`artifact-user-selection-${artifact.id}-${artifact.version}`}
                    value={candidate.id}
                    checked={selected}
                    onChange={() => {
                      setSelectedOptionId(() => candidate.id);
                      setDecisionError(() => "");
                    }}
                  />
                  <span>
                    <span className="artifact-user-decision-candidate-title">
                      <strong>{candidate.title}</strong>
                      {candidate.aiPreferred ? <em className="ai-preferred">AI 首选</em> : null}
                      {selected ? <em className="user-selected">我的选择</em> : null}
                    </span>
                    <small>
                      {selection.governed
                        ? `${candidate.id} · 精确版本 r${candidate.revision || "?"}`
                        : `${candidate.id} · 明确不适用治理令牌`}
                    </small>
                    {candidate.description ? <p>{candidate.description}</p> : null}
                    {candidate.riskReview ? (
                      <small className={`artifact-user-decision-risk ${candidate.riskReview.tone}`}>
                        {candidate.riskReview.dispositionLabel} · 当前精确版本已复核
                      </small>
                    ) : (
                      <small className="artifact-user-decision-risk neutral">
                        {selection.governed
                          ? "缺少当前精确版本风控复核"
                          : "此产物未附加候选谱系或风控复核证明"}
                      </small>
                    )}
                  </span>
                </label>
              );
            })}
          </fieldset>
        ) : (
          <p className="artifact-user-decision-candidate-empty">没有可供支持的当前候选。</p>
        )}
        {!selection.decisionReady ? (
          <p className="artifact-user-decision-error" role="alert">
            {selection.decisionReason || "候选治理状态未知，最终决定已失败关闭。"}
          </p>
        ) : !selection.ready ? (
          <p className="artifact-user-decision-error" role="alert">
            {selection.reason || "当前候选状态不可用于支持决定；仍可保留或退回。"}
          </p>
        ) : null}
      </div>

      <label className="artifact-user-decision-rationale">
        最终决定理由
        <textarea
          required
          maxLength={8000}
          value={rationale}
          onChange={(event) => setRationale(() => event.target.value)}
          placeholder="写明你依据了哪些证据、保留了哪些分歧，以及什么条件会改变这个决定。"
        />
      </label>
      <p className="artifact-user-decision-bind-note">本次选择只绑定当前已确认的 v{artifact.version}；上方尚未保存的修改不包含在本次记录中。</p>
      {!currentConfirmationValid ? (
        <p className="artifact-user-decision-error" role="alert">
          这个历史确认版本未通过当前证据门，不能记录新的最终决定；请重新核验并生成符合现行规则的新版本。
        </p>
      ) : null}

      <div className="artifact-user-decision-actions" aria-label="最终决定选项">
        {Object.entries(USER_DECISION_ACTIONS).map(([action, meta]) => {
          const Icon = userDecisionIcons[action];
          const lacksCandidate = action === "support" && !selectedCandidate;
          const supportUnavailable = action === "support" && !selection.ready;
          const disabled = Boolean(
            busyAction
            || !canSubmit
            || !cleanRationale
            || lacksCandidate
            || !currentConfirmationValid
            || !selection.decisionReady
            || supportUnavailable
          );
          const actionTitle = !canSubmit
            ? "当前未提供最终决定提交处理器"
            : !selection.decisionReady
            ? selection.decisionReason || "当前治理记录未就绪"
            : supportUnavailable
              ? selection.reason || "当前候选状态不可用于支持决定"
            : !currentConfirmationValid
              ? "当前历史确认版本未通过现行证据门"
              : lacksCandidate
                ? "支持候选前，请先明确选择你的候选"
                : !cleanRationale
                  ? "请先填写最终决定理由"
                  : meta.description;
          return (
            <button
              className={`artifact-user-decision-action ${action}`}
              type="button"
              key={action}
              disabled={disabled}
              title={actionTitle}
              onClick={() => submitDecision(action)}
            >
              <Icon aria-hidden="true" size={15} />
              <span><strong>{busyAction === action ? "正在提交…" : meta.label}</strong><small>{meta.description}</small></span>
            </button>
          );
        })}
      </div>
      {decisionError ? <p className="artifact-user-decision-error" role="alert">{decisionError}</p> : null}

      {history.length ? (
        <details className="artifact-user-decision-history">
          <summary><History aria-hidden="true" size={14} />决策历史（{history.length}）{stale.length ? ` · ${stale.length} 条已过期` : ""}</summary>
          <div>
            {history.map((decision, decisionIndex) => (
              <article className={decision.is_current ? "current" : "stale"} key={decision.id
                ? JSON.stringify(["decision_id", decision.id])
                : JSON.stringify(["decision_record", decision.artifact_version, decision.created_at, decisionIndex])}>
                <span><strong>{userDecisionLabel(decision)}</strong><small>绑定 v{decision.artifact_version || "?"} · {formatUserDecisionTime(decision.created_at)}</small></span>
                <small className="artifact-user-decision-history-binding">{userDecisionBindingText(artifact, decision)}</small>
                <p>{decision.rationale}</p>
                {!decision.is_current ? <em>{decision.stale_reason || "该记录不再对应当前确认版本。"}</em> : null}
              </article>
            ))}
          </div>
        </details>
      ) : null}
    </section>
  );
}

const MemoSimpleItemEditor = memo(SimpleItemEditor, (previous, next) => (
  previous.item === next.item
  && previous.section === next.section
  && previous.label === next.label
));
const MemoDisagreementEditor = memo(DisagreementEditor, (previous, next) => previous.item === next.item);
const MemoActionEditor = memo(ActionEditor, (previous, next) => previous.item === next.item);
const MemoRequirementEditor = memo(RequirementEditor, (previous, next) => previous.item === next.item);
const MemoRiskEditor = memo(RiskEditor, (previous, next) => previous.item === next.item);
const MemoDecisionMatrixPreview = memo(DecisionMatrixPreview);
const MemoDecisionOptionEditor = memo(DecisionOptionEditor, (previous, next) => (
  previous.item === next.item
  && previous.selected === next.selected
  && previous.structured === next.structured
  && previous.structuredReadOnly === next.structuredReadOnly
));
const MemoUserFinalDecisionSection = memo(UserFinalDecisionSection);

function ArtifactEditor({ artifact, room, pluginRegistry, pluginLifecycle, open, messages, materials, onClose, onSave, onConfirm, onUserDecision, onExport, restoreFocusRef }) {
  const sourceState = artifactEditorSourceState(artifact);
  const dialogRef = useRef(null);
  const closeButtonRef = useRef(null);
  const dialogTitleId = useId();
  const dialogDescriptionId = useId();
  const [editor, setEditor] = useState(() => createEditorState(sourceState.artifact));
  const [workingArtifact, setWorkingArtifact] = useState(sourceState.artifact);
  const [mutationAction, setMutationAction] = useState("");
  const [mutationError, setMutationError] = useState("");
  const [progressNotice, setProgressNotice] = useState("");
  const [sourceDetailByKey, setSourceDetailByKey] = useState({});
  const [roundEvidenceEnvelope, setRoundEvidenceEnvelope] = useState(null);
  const [roundEvidenceSourceState, setRoundEvidenceSourceState] = useState("loading");
  const [evidenceGraph, setEvidenceGraph] = useState(null);
  const [evidenceGraphState, setEvidenceGraphState] = useState("loading");
  const [focusedEvidenceKey, setFocusedEvidenceKey] = useState("");
  const mutationRequestRef = useRef(0);
  const mutationInFlightRef = useRef(false);
  const sourceDetailRequestGenerationRef = useRef(0);
  const busy = Boolean(mutationAction);
  const canClose = typeof onClose === "function";
  const requestClose = () => {
    if (busy) return;
    if (!canClose) {
      setMutationError("关闭处理器不可用。");
      return;
    }
    try {
      onClose();
    } catch (closeError) {
      setMutationError(artifactEditorErrorMessage(closeError, "窗口关闭失败。"));
    }
  };
  useModalFocus({
    open,
    containerRef: dialogRef,
    initialFocusRef: closeButtonRef,
    restoreFallbackRef: restoreFocusRef,
    onClose: busy || !canClose ? null : requestClose,
  });
  useEffect(() => {
    if (open && busy) dialogRef.current?.focus({ preventScroll: true });
  }, [busy, open]);
  useEffect(() => {
    sourceDetailRequestGenerationRef.current += 1;
    mutationRequestRef.current += 1;
    mutationInFlightRef.current = false;
    if (!open) {
      setMutationAction("");
      setMutationError("");
    }
    return () => {
      sourceDetailRequestGenerationRef.current += 1;
      mutationRequestRef.current += 1;
      mutationInFlightRef.current = false;
    };
  }, [open]);
  useEffect(() => {
    if (!open) return undefined;
    sourceDetailRequestGenerationRef.current += 1;
    let cancelled = false;
    const controller = new AbortController();
    const evidenceIdentity = artifactEditorIdentity(workingArtifact, room);
    setRoundEvidenceEnvelope(null);
    setSourceDetailByKey({});
    setEvidenceGraph(null);
    setEvidenceGraphState("loading");
    if (!evidenceIdentity.integrityOk) {
      setRoundEvidenceSourceState("error");
      setEvidenceGraphState("error");
      return () => {
        cancelled = true;
        controller.abort();
      };
    }
    if (!workingArtifact.round_id) {
      setRoundEvidenceSourceState("ready");
    } else {
      setRoundEvidenceSourceState("loading");
      api.artifactEvidenceSources(evidenceIdentity.roomId, evidenceIdentity.artifactId, controller.signal)
        .then((data) => {
          if (cancelled) return;
          const normalized = normalizeArtifactEvidenceResponse(data, {
            roundId: workingArtifact.round_id,
          });
          setRoundEvidenceEnvelope(normalized);
          setRoundEvidenceSourceState(normalized.authoritative ? "ready" : "untrusted");
        })
        .catch((error) => {
          if (cancelled || error?.name === "AbortError") return;
          setRoundEvidenceSourceState("error");
        });
    }
    api.artifactEvidenceGraph(evidenceIdentity.roomId, evidenceIdentity.artifactId, controller.signal)
      .then((data) => {
        if (cancelled) return;
        setEvidenceGraph(normalizeArtifactEvidenceGraph(data, {
          roomId: evidenceIdentity.roomId,
          artifactId: evidenceIdentity.artifactId,
          artifactVersion: workingArtifact.version,
          roundId: workingArtifact.round_id || "",
        }));
        setEvidenceGraphState("ready");
      })
      .catch((error) => {
        if (cancelled || error?.name === "AbortError") return;
        setEvidenceGraphState("error");
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [open, room?.id, workingArtifact.id, workingArtifact.round_id, workingArtifact.version]);
  const artifactWorkspaceSlot = useMemo(
    () => resolveHostOwnedSlot({
      slotId: HOST_SLOT_IDS.artifactWorkspace,
      frozenContext: workingArtifact.plugin_registry_context,
      runtimeContext: room,
      pluginRegistry,
      pluginLifecycle,
    }),
    [pluginLifecycle, pluginRegistry, room, workingArtifact.plugin_registry_context],
  );
  const projectContribution = resolvedHostContribution(
    artifactWorkspaceSlot,
    HOST_CONTRIBUTION_IDS.projectArtifactWorkspace,
  );
  const storageContribution = resolvedHostContribution(
    artifactWorkspaceSlot,
    HOST_CONTRIBUTION_IDS.storageArtifactWorkspace,
  );
  const projectReadinessContribution = resolvedHostContribution(
    artifactWorkspaceSlot,
    HOST_CONTRIBUTION_IDS.projectReadinessArtifactWorkspace,
  );
  const legacyProjectWorkspace = artifactWorkspaceSlot.status === "legacy_unversioned"
    && hasProjectWorkspaceFootprint(workingArtifact.content);
  const projectWorkspace = Boolean(projectContribution?.present || legacyProjectWorkspace);
  const projectWorkspaceReadOnly = projectWorkspace && (
    legacyProjectWorkspace
    || projectContribution?.active !== true
  );
  const projectWorkspaceReason = legacyProjectWorkspace
    ? "该历史产物没有版本化插件合同；仅按已保存的项目字段只读展示，不推断当前能力包。"
    : projectContribution?.reason || artifactWorkspaceSlot.reason;
  const storageWorkspace = Boolean(storageContribution?.present);
  const storageWorkspaceReadOnly = storageWorkspace && storageContribution?.active !== true;
  const storageWorkspaceReason = storageContribution?.reason || artifactWorkspaceSlot.reason;
  const frozenPluginSnapshot = workingArtifact.plugin_registry_context?.snapshot
    || workingArtifact.plugin_registry_context?.plugin_registry_snapshot
    || {};
  const frozenAndCurrentCapabilityPackIds = [
    ...(Array.isArray(frozenPluginSnapshot.capability_packs)
      ? frozenPluginSnapshot.capability_packs.map((pack) => pack?.id)
      : []),
    ...(Array.isArray(room?.active_capability_pack_ids)
      ? room.active_capability_pack_ids
      : artifactEditorRows(room?.capability_pack_ids)),
  ];
  const footballResearchPackPresent = frozenAndCurrentCapabilityPackIds
    .includes("football_research_readonly");
  const stockResearchPackPresent = frozenAndCurrentCapabilityPackIds
    .includes("stock_research_readonly");
  const storageCandidateExperimentAllowed = storageWorkspace
    && !footballResearchPackPresent
    && !stockResearchPackPresent;
  const evidenceCandidates = useMemo(() => {
    const referencedRefs = collectEvidence(workingArtifact.content || {});
    return buildArtifactEvidenceCandidates({
      roundId: workingArtifact.round_id,
      apiEvidence: roundEvidenceEnvelope,
      materials,
      messages,
      referencedEvidence: referencedRefs,
    });
  }, [materials, messages, roundEvidenceEnvelope, workingArtifact.content, workingArtifact.round_id]);

  const evidenceTargets = useMemo(() => [
    { key: "summary", label: "会议摘要" },
    { key: "decision", label: "首选方案选择理由" },
    ...editor.decision.options.map((item) => ({
      key: targetKey("decision_options", item.id),
      label: `候选方案 · ${item.title.slice(0, 34) || "未命名"}`,
    })),
    ...editor.requirements.map((item) => ({
      key: targetKey("requirements", item.id),
      label: `需求证据 · ${item.text.slice(0, 34) || "未命名"}`,
    })),
    ...editor.risks.map((item) => ({
      key: targetKey("risks", item.id),
      label: `项目风险 · ${item.text.slice(0, 34) || "未命名"}`,
    })),
    ...simpleSections.flatMap(([section, label]) => editor[section].map((item) => ({
      key: targetKey(section, item.id),
      label: `${label} · ${item.text.slice(0, 34) || "未命名"}`,
    }))),
    ...editor.disagreements.map((item) => ({
      key: targetKey("disagreements", item.id),
      label: `分歧 · ${item.text.slice(0, 34) || "未命名"}`,
    })),
    ...editor.actions.map((item) => ({
      key: targetKey("actions", item.id),
      label: `待办 · ${item.text.slice(0, 34) || "未命名"}`,
    })),
  ], [
    editor.actions,
    editor.conclusions,
    editor.decision.options,
    editor.disagreements,
    editor.requirements,
    editor.risks,
    editor.unknowns,
  ]);
  const activeTarget = evidenceTargets.some((target) => target.key === editor.reviewTarget)
    ? editor.reviewTarget
    : "summary";
  const activeReview = editor.evidenceByTarget[activeTarget] || {};
  const selectedEvidence = useMemo(() => Object.keys(activeReview), [activeReview]);
  const currentArtifact = useMemo(
    () => buildArtifact(workingArtifact, editor, evidenceCandidates),
    [editor, evidenceCandidates, workingArtifact],
  );
  const currentContent = currentArtifact.content || {};
  const evidenceReview = useMemo(
    () => artifactEvidenceReviewSummary(currentArtifact.content || {}),
    [currentArtifact.content],
  );
  const candidateByKey = useMemo(
    () => new Map(evidenceCandidates.map((item) => [evidenceKey(item), item])),
    [evidenceCandidates],
  );
  const selectedEvidenceRelations = useMemo(
    () => Object.values(editor.evidenceByTarget).flatMap((target) => (
      Object.entries(target || {}).map(([sourceKey, audit]) => ({ sourceKey, audit }))
    )),
    [editor.evidenceByTarget],
  );
  const selectedAuthoritativeSourceMissing = Boolean(workingArtifact.round_id)
    && selectedEvidenceRelations.some(({ sourceKey }) => !candidateByKey.has(sourceKey));
  const selectedAuthoritativeIdentityMismatch = Boolean(workingArtifact.round_id)
    && selectedEvidenceRelations.some(({ sourceKey, audit }) => {
      const candidate = candidateByKey.get(sourceKey);
      return Boolean(candidate) && !evidenceSourceIdentityMatches(candidate, audit);
    });
  const selectedAuthoritativeGap = Boolean(workingArtifact.round_id)
    && selectedEvidenceRelations.some(({ sourceKey, audit }) => {
      const candidate = candidateByKey.get(sourceKey);
      const sourceDetail = candidate
        ? sourceDetailByKey[evidenceSourceDetailKey(candidate, audit)]
        : null;
      return !candidate || evidenceRelationFlags(candidate, audit, sourceDetail).gap;
    });
  const evidenceMutationBlocked = Boolean(workingArtifact.round_id) && (
    roundEvidenceSourceState !== "ready"
    || selectedAuthoritativeSourceMissing
    || selectedAuthoritativeIdentityMismatch
  );
  const authoritativeSourceStateIssue = !artifact.round_id || roundEvidenceSourceState === "ready"
    ? selectedAuthoritativeSourceMissing
      ? "权威来源响应遗漏了产物已绑定的证据；为避免静默删除旧引用，当前禁止保存。"
      : selectedAuthoritativeIdentityMismatch
        ? "已绑定证据版本与本轮权威来源身份不一致；请取消该引用后重新绑定本轮冻结版本，或解除旧引用。当前禁止保存。"
      : ""
    : roundEvidenceSourceState === "loading"
      ? "正在读取本轮权威证据来源，完成前不能保存或确认。"
      : roundEvidenceSourceState === "untrusted"
        ? "证据接口未声明本轮来源具有权威性，当前不能保存或确认。"
        : "本轮权威证据来源暂时无法读取，当前不能保存或确认。";
  const serverConfirmationIssues = Array.isArray(workingArtifact.evidence_review?.confirmation_issues)
    ? workingArtifact.evidence_review.confirmation_issues
    : [];
  const currentHasMarketSnapshot = collectEvidence(currentContent).some(
    (ref) => ref.type === "round_market_snapshot",
  );
  const requiredMarketSnapshotMissing = !currentHasMarketSnapshot
    && serverConfirmationIssues.includes("产物缺少本轮冻结市场快照证据");
  const unresolvedDecision = (currentContent.disagreements || []).find(
    (item) => item.status !== "open" && !item.resolution.trim(),
  );
  const invalidRequirement = projectWorkspace && (currentContent.requirements || []).find(
    (item) => item.status === "confirmed" && !item.acceptance_criteria.trim(),
  );
  const invalidRisk = projectWorkspace && (currentContent.risks || []).find(
    (item) => !item.trigger.trim()
      || (["mitigated", "accepted"].includes(item.status) && !item.mitigation.trim()),
  );
  const submittedDecision = currentContent.decision || {};
  const submittedOptions = Array.isArray(submittedDecision.options) ? submittedDecision.options : [];
  const candidateDecisionIssue = submittedDecision.status === "candidate"
    ? submittedOptions.length < 2
      ? "候选首选至少需要两个填写完整的可比较方案。"
      : !submittedOptions.some((item) => item.id === submittedDecision.preferred_option_id)
        ? "请从候选方案中选择一个首选项。"
        : !String(submittedDecision.rationale || "").trim()
          ? "请填写首选方案的选择理由。"
          : ""
    : submittedDecision.status === "deferred" && !String(submittedDecision.rationale || "").trim()
      ? "暂缓决策必须填写原因。"
      : "";
  const evidenceReviewReason = evidenceReview.unreviewedCount
    ? `仍有 ${evidenceReview.unreviewedCount} 条证据关系未核验；请使用“下一条未核验”逐项处理。`
    : evidenceReview.issues[0] || "";
  const confirmDisabledReason = !currentArtifact.title.trim() || !String(currentContent.summary || "").trim()
    ? "填写标题和会议摘要后才能确认。"
    : authoritativeSourceStateIssue
      ? authoritativeSourceStateIssue
    : requiredMarketSnapshotMissing
      ? "正式轮次产物必须先保存一次，由服务端绑定本轮冻结市场快照并等待用户核验。"
    : unresolvedDecision
      ? "已解决或接受风险的分歧必须填写处理结论。"
      : invalidRequirement
        ? "已确认需求必须填写可测试验收条件。"
        : invalidRisk
          ? "项目风险必须填写触发信号；已处理风险还要填写缓解或接受说明。"
      : candidateDecisionIssue
        ? candidateDecisionIssue
        : selectedAuthoritativeGap
          ? "正式轮次产物仍引用不可精确读取的证据缺口；请移除该引用，或等待权威来源恢复。"
        : evidenceReviewReason;
  const confirmDisabled = Boolean(confirmDisabledReason);
  const artifactIdentity = artifactEditorIdentity(workingArtifact, room);
  const mutationControlInput = {
    identity: artifactIdentity,
    title: editor.title,
    summary: editor.summary,
    busy,
    inFlight: mutationInFlightRef.current,
    evidenceBlocked: evidenceMutationBlocked,
    confirmDisabledReason,
    saveHandlerAvailable: typeof onSave === "function",
    confirmHandlerAvailable: typeof onConfirm === "function",
    exportHandlerAvailable: typeof onExport === "function",
  };
  const progressControl = artifactEditorMutationControl({ ...mutationControlInput, action: "progress" });
  const draftControl = artifactEditorMutationControl({ ...mutationControlInput, action: "draft" });
  const confirmControl = artifactEditorMutationControl({ ...mutationControlInput, action: "confirm" });
  const exportControl = artifactEditorMutationControl({ ...mutationControlInput, action: "export" });

  const updateItem = (section, itemId, patch) => {
    setEditor((current) => ({
      ...current,
      [section]: current[section].map((item) => item.id === itemId ? { ...item, ...patch } : item),
    }));
  };

  const addItem = (section) => {
    const id = newItemId(section);
    const item = section === "disagreements"
      ? { id, text: "", positions: [], status: "open", blocking: true, owner: "待分配", resolution: "" }
      : section === "actions"
        ? { id, text: "", owner: "待分配", due: "", state: "open" }
        : section === "requirements"
          ? { id, text: "", status: "pending", owner: "待确认", acceptance_criteria: "" }
          : section === "risks"
            ? { id, text: "", status: "open", probability: "unknown", impact: "unknown", blocking: true, trigger: "", mitigation: "", owner: "待分配" }
        : { id, text: "" };
    const key = targetKey(section, id);
    setEditor((current) => ({
      ...current,
      [section]: [...current[section], item],
      evidenceByTarget: { ...current.evidenceByTarget, [key]: {} },
      reviewTarget: key,
    }));
  };

  const updateDecision = (patch) => {
    setEditor((current) => ({
      ...current,
      decision: { ...current.decision, ...patch },
    }));
  };

  const updateDecisionOption = (optionId, patch) => {
    setEditor((current) => ({
      ...current,
      decision: {
        ...current.decision,
        options: current.decision.options.map((item) => item.id === optionId ? { ...item, ...patch } : item),
      },
    }));
  };

  const addDecisionOption = () => {
    const id = newItemId("decision_options");
    const key = targetKey("decision_options", id);
    setEditor((current) => ({
      ...current,
      decision: {
        ...current.decision,
        options: [...current.decision.options, {
          id, title: "", description: "", benefits: [], risks: [], value: "", cost: "", timeline: "",
          dependencies: [], reversibility: "unknown",
        }],
      },
      evidenceByTarget: { ...current.evidenceByTarget, [key]: {} },
      reviewTarget: key,
    }));
  };

  const removeDecisionOption = (optionId) => {
    const key = targetKey("decision_options", optionId);
    setEditor((current) => {
      const evidenceByTarget = { ...current.evidenceByTarget };
      delete evidenceByTarget[key];
      return {
        ...current,
        decision: {
          ...current.decision,
          options: current.decision.options.filter((item) => item.id !== optionId),
          preferred_option_id: current.decision.preferred_option_id === optionId
            ? ""
            : current.decision.preferred_option_id,
        },
        evidenceByTarget,
        reviewTarget: current.reviewTarget === key ? "decision" : current.reviewTarget,
      };
    });
  };

  const removeItem = (section, itemId) => {
    const key = targetKey(section, itemId);
    setEditor((current) => {
      const evidenceByTarget = { ...current.evidenceByTarget };
      delete evidenceByTarget[key];
      return {
        ...current,
        [section]: current[section].filter((item) => item.id !== itemId),
        evidenceByTarget,
        reviewTarget: current.reviewTarget === key ? "summary" : current.reviewTarget,
      };
    });
  };

  const selectReviewTarget = (key) => {
    setFocusedEvidenceKey("");
    setEditor((current) => ({ ...current, reviewTarget: key }));
  };

  const selectGraphRelation = (itemKey, sourceRef = {}) => {
    setEditor((current) => ({ ...current, reviewTarget: itemKey || "summary" }));
    setFocusedEvidenceKey(`${sourceRef.type || ""}:${sourceRef.id || ""}`);
  };

  const toggleEvidence = (key, checked) => {
    setEditor((current) => {
      const target = { ...(current.evidenceByTarget[activeTarget] || {}) };
      if (checked) {
        const source = candidateByKey.get(key);
        target[key] = target[key] || auditDefaults({
          version: source?.version,
          latest_version: source?.latestVersion,
          version_status: source?.versionStatus,
          source_active: source?.sourceActive,
          source_snapshot_sha256: source?.source_snapshot_sha256,
          source_revision: source?.source_revision,
        });
      }
      else delete target[key];
      return {
        ...current,
        evidenceByTarget: { ...current.evidenceByTarget, [activeTarget]: target },
      };
    });
  };

  const changeReview = (key, field, value) => {
    setEditor((current) => ({
      ...current,
      evidenceByTarget: {
        ...current.evidenceByTarget,
        [activeTarget]: {
          ...(current.evidenceByTarget[activeTarget] || {}),
          [key]: {
            ...((current.evidenceByTarget[activeTarget] || {})[key] || auditDefaults()),
            [field]: value,
          },
        },
      },
    }));
  };

  const decideVersion = (key, decision, latestVersion) => {
    const source = candidateByKey.get(key);
    setEditor((current) => {
      const previous = ((current.evidenceByTarget[activeTarget] || {})[key] || auditDefaults());
      const next = decision === "migrate_current"
        ? {
            ...previous,
            version: latestVersion,
            latest_version: latestVersion,
            version_status: "current",
            version_decision: "current",
            verification_status: "unreviewed",
            review_note: "",
            source_snapshot_sha256: source?.source_snapshot_sha256 || "",
            source_revision: source?.source_revision || "",
          }
        : {
            ...previous,
            latest_version: latestVersion,
            version_status: previous.version_status === "inactive" ? "inactive" : "superseded",
            version_decision: "keep_snapshot",
          };
      return {
        ...current,
        evidenceByTarget: {
          ...current.evidenceByTarget,
          [activeTarget]: {
            ...(current.evidenceByTarget[activeTarget] || {}),
            [key]: next,
          },
        },
      };
    });
  };

  const goToNextUnreviewed = () => {
    const relations = evidenceTargets.flatMap((target) => (
      Object.entries(editor.evidenceByTarget[target.key] || {}).map(([sourceKey, audit]) => ({
        targetKey: target.key,
        sourceKey,
        audit: auditDefaults(audit),
      }))
    ));
    const unreviewed = relations.filter((relation) => relation.audit.verification_status === "unreviewed");
    if (!unreviewed.length) return;
    const currentIndex = unreviewed.findIndex((relation) => (
      relation.targetKey === activeTarget && relation.sourceKey === focusedEvidenceKey
    ));
    const next = unreviewed[(currentIndex + 1) % unreviewed.length];
    setEditor((current) => ({ ...current, reviewTarget: next.targetKey }));
    setFocusedEvidenceKey(next.sourceKey);
  };

  const loadEvidenceSourceDetail = async (item, citedVersion) => {
    const previewKey = `${item.type}:${item.id}:v${citedVersion}`;
    if (["loading", "ready"].includes(sourceDetailByKey[previewKey]?.status)) return;
    const requestGeneration = sourceDetailRequestGenerationRef.current;
    setSourceDetailByKey((current) => ({
      ...current,
      [previewKey]: { status: "loading" },
    }));
    try {
      let detail;
      if (item.type === "material") {
        const data = await api.materialVersion(room.id, item.id, citedVersion);
        const material = data.material || {};
        if (
          String(material.id || "") !== String(item.id || "")
          || Number(material.version || 0) !== Number(citedVersion)
        ) {
          throw new Error("资料版本身份不匹配");
        }
        const exactContent = String(material.content || "").trim();
        detail = {
          type: "material",
          id: item.id,
          version: Number(citedVersion),
          exact: true,
          sourceIdentityExact: true,
          preview: exactContent,
          previewComplete: Boolean(exactContent),
          previewTruncated: false,
          previewRedacted: false,
          previewBudgetExhausted: false,
          sourceMeta: `${material.kind || "note"} · 精确历史版本 v${material.version || citedVersion} · ${material.active === false ? "捕获时已停用" : "版本快照可读取"}`,
          sourceUrl: String(material.source_url || "").trim(),
        };
      } else {
        const data = await api.artifactEvidenceSourceDetail(
          room.id,
          workingArtifact.id,
          item.type,
          item.id,
        );
        const normalized = normalizeArtifactEvidenceDetailResponse(data, {
          artifactId: workingArtifact.id,
          roundId: workingArtifact.round_id,
          type: item.type,
          id: item.id,
        });
        if (!normalized) throw new Error("完整冻结来源响应未通过身份校验");
        detail = normalized.source;
      }
      if (requestGeneration !== sourceDetailRequestGenerationRef.current) return;
      setSourceDetailByKey((current) => ({
        ...current,
        [previewKey]: {
          ...detail,
          status: "ready",
        },
      }));
    } catch {
      if (requestGeneration !== sourceDetailRequestGenerationRef.current) return;
      setSourceDetailByKey((current) => ({
        ...current,
        [previewKey]: {
          status: "error",
          error: item.type === "material"
            ? `无法读取资料 ${item.id} 的精确 v${citedVersion} 快照。`
            : `无法读取 ${item.id} 的完整冻结来源。`,
        },
      }));
    }
  };

  const saveProgress = async () => {
    if (mutationInFlightRef.current || !progressControl.canRun) {
      setMutationError(progressControl.instruction);
      return;
    }
    const requestId = ++mutationRequestRef.current;
    mutationInFlightRef.current = true;
    const saveHandler = onSave;
    setMutationAction("progress");
    setMutationError("");
    setProgressNotice("");
    try {
      const saved = await saveHandler(currentArtifact, { keepOpen: true });
      if (requestId !== mutationRequestRef.current) return;
      const savedState = artifactEditorSavedState(saved, workingArtifact);
      if (!savedState.ok) throw new Error(savedState.error);
      if (savedState.artifact) {
        setWorkingArtifact(savedState.artifact);
        setEditor((current) => {
          const normalized = createEditorState(savedState.artifact);
          return {
            ...normalized,
            reviewTarget: normalized.evidenceByTarget[current.reviewTarget]
              ? current.reviewTarget
              : "summary",
          };
        });
        setProgressNotice(`审核进度已保存为 v${savedState.artifact.version}，可以继续逐条核验。`);
      }
    } catch (saveError) {
      if (requestId === mutationRequestRef.current) {
        setMutationError(artifactEditorErrorMessage(saveError, "保存审核进度失败。"));
      }
    } finally {
      if (requestId === mutationRequestRef.current) {
        mutationInFlightRef.current = false;
        setMutationAction("");
      }
    }
  };

  const saveAndClose = async () => {
    if (mutationInFlightRef.current || !draftControl.canRun) {
      setMutationError(draftControl.instruction);
      return;
    }
    const requestId = ++mutationRequestRef.current;
    mutationInFlightRef.current = true;
    const saveHandler = onSave;
    setMutationAction("draft");
    setMutationError("");
    try {
      await saveHandler(currentArtifact);
    } catch (saveError) {
      if (requestId === mutationRequestRef.current) {
        setMutationError(artifactEditorErrorMessage(saveError, "保存草稿失败。"));
      }
    } finally {
      if (requestId === mutationRequestRef.current) {
        mutationInFlightRef.current = false;
        setMutationAction("");
      }
    }
  };

  const saveAndConfirm = async () => {
    if (mutationInFlightRef.current || !confirmControl.canRun) {
      setMutationError(confirmControl.instruction);
      return;
    }
    const requestId = ++mutationRequestRef.current;
    mutationInFlightRef.current = true;
    const confirmHandler = onConfirm;
    setMutationAction("confirm");
    setMutationError("");
    try {
      await confirmHandler(currentArtifact);
    } catch (confirmError) {
      if (requestId === mutationRequestRef.current) {
        setMutationError(artifactEditorErrorMessage(confirmError, "保存并确认产物失败。"));
      }
    } finally {
      if (requestId === mutationRequestRef.current) {
        mutationInFlightRef.current = false;
        setMutationAction("");
      }
    }
  };

  const exportCurrentArtifact = async () => {
    if (mutationInFlightRef.current || !exportControl.canRun) {
      setMutationError(exportControl.instruction);
      return;
    }
    const requestId = ++mutationRequestRef.current;
    mutationInFlightRef.current = true;
    const exportHandler = onExport;
    setMutationAction("export");
    setMutationError("");
    try {
      await exportHandler(currentArtifact);
    } catch (exportError) {
      if (requestId === mutationRequestRef.current) {
        setMutationError(artifactEditorErrorMessage(exportError, "导出 Markdown 失败。"));
      }
    } finally {
      if (requestId === mutationRequestRef.current) {
        mutationInFlightRef.current = false;
        setMutationAction("");
      }
    }
  };

  if (!open) return null;
  return (
    <div
      className="dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) requestClose();
      }}
    >
      <form
        ref={dialogRef}
        className="dialog artifact-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={dialogTitleId}
        aria-describedby={dialogDescriptionId}
        aria-busy={busy}
        data-mutation-state={mutationAction || "idle"}
        tabIndex={-1}
        onSubmit={(event) => {
          event.preventDefault();
          if (!busy) saveAndClose();
        }}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <span>
            <strong id={dialogTitleId}>会议产物工作区</strong>
            <small id={dialogDescriptionId} className={`version-tag ${workingArtifact.status === "CONFIRMED" ? "confirmed" : ""}`}>
              v{workingArtifact.version} · {workingArtifact.status === "CONFIRMED" ? "用户已确认" : "草稿待确认"}
            </small>
          </span>
          <button ref={closeButtonRef} type="button" className="icon-button" aria-label="关闭会议产物工作区" onClick={requestClose} disabled={busy || !canClose}><X aria-hidden="true" size={18} /></button>
        </header>

        <fieldset className="artifact-editor-fields" disabled={busy}>
          <legend className="artifact-editor-fields-legend">会议产物编辑内容</legend>

        <ArtifactVersionHistory roomId={artifactIdentity.roomId} artifact={workingArtifact} />
        <section className="artifact-mutation-ledger" aria-label="产物操作许可">
          <span><small>保存草稿</small><strong>{draftControl.instruction}</strong></span>
          <span><small>确认产物</small><strong>{confirmControl.instruction}</strong></span>
          <span><small>导出</small><strong>{exportControl.instruction}</strong></span>
        </section>
        {!sourceState.integrityOk || !artifactIdentity.integrityOk ? (
          <p className="artifact-source-warning" role="alert">
            {!sourceState.integrityOk ? `来源产物已按安全结构打开：${sourceState.issues[0]} ` : null}
            {artifactIdentity.integrityOk
              ? "保存会写入修复后的草稿结构。"
              : "产物或房间身份无效，当前不能读取证据、保存或确认。"}
          </p>
        ) : null}
        {serverConfirmationIssues.length ? (
          <section className={requiredMarketSnapshotMissing ? "artifact-server-gate required" : "artifact-server-gate"}>
            <span>
              <strong>服务端确认门：仍有 {workingArtifact.evidence_review?.confirmation_issue_count || serverConfirmationIssues.length} 项</strong>
              <small>{requiredMarketSnapshotMissing
                ? "当前旧草稿缺少本轮冻结市场快照。保存后服务端会自动加入精确 revision/SHA，且初始保持未核验。"
                : artifactEditorDisplayText(serverConfirmationIssues[0], "服务端未返回可展示的确认门原因。")}</small>
            </span>
            {requiredMarketSnapshotMissing ? (
              <button type="button" className="secondary" disabled={!progressControl.canRun} onClick={saveProgress}>
                <Save aria-hidden="true" size={13} />{mutationAction === "progress" ? "正在绑定…" : "保存并绑定快照"}
              </button>
            ) : null}
          </section>
        ) : null}

        <label>产物标题
          <input
            required
            value={editor.title}
            onChange={(event) => setEditor((current) => ({ ...current, title: event.target.value }))}
          />
        </label>
        <label>会议摘要
          <textarea
            required
            value={editor.summary}
            onChange={(event) => setEditor((current) => ({ ...current, summary: event.target.value }))}
          />
        </label>

        <div className="artifact-structured-sections">
          {projectWorkspace ? (
            <fieldset
              className={projectWorkspaceReadOnly ? "artifact-plugin-workspace read-only" : "artifact-plugin-workspace"}
              disabled={projectWorkspaceReadOnly}
            >
              <legend>项目研究工作区{projectWorkspaceReadOnly ? " · 只读" : ""}</legend>
              {projectWorkspaceReadOnly ? <p className="plugin-workspace-readonly-note" role="note">{projectWorkspaceReason}</p> : null}
              <section className="artifact-structured-section project-workspace-section">
                <SectionHeader title="需求证据地图" help="区分已确认需求、工作假设、待补证据和已排除事项" onAdd={() => addItem("requirements")} />
                <div className="artifact-item-list">
                  {editor.requirements.map((item) => <MemoRequirementEditor
                    key={item.id}
                    item={item}
                    onChange={updateItem}
                    onRemove={() => removeItem("requirements", item.id)}
                    onReview={() => selectReviewTarget(targetKey("requirements", item.id))}
                  />)}
                  {!editor.requirements.length ? <p className="artifact-empty-section">尚未记录需求证据；不要把工作假设直接当成用户需求。</p> : null}
                </div>
              </section>
              <section className="artifact-structured-section project-workspace-section">
                <SectionHeader title="项目风险登记" help="记录概率、影响、阻断性、触发信号、负责人和处置状态" onAdd={() => addItem("risks")} />
                <div className="artifact-item-list">
                  {editor.risks.map((item) => <MemoRiskEditor
                    key={item.id}
                    item={item}
                    onChange={updateItem}
                    onRemove={() => removeItem("risks", item.id)}
                    onReview={() => selectReviewTarget(targetKey("risks", item.id))}
                  />)}
                  {!editor.risks.length ? <p className="artifact-empty-section">尚未登记项目风险；开放且阻断的风险会阻止候选方案进入用户决策。</p> : null}
                </div>
              </section>
            </fieldset>
          ) : null}
          <ArtifactCandidateGovernance artifact={workingArtifact} />
          <section className="artifact-structured-section decision-board-section">
            <SectionHeader
              title="多方案决策板"
              help="至少比较两个方案后，才能形成需用户确认的首选候选"
              onAdd={addDecisionOption}
            />
            <div className="artifact-item-grid">
              <label>决策状态
                <select
                  value={editor.decision.status}
                  onChange={(event) => updateDecision({
                    status: event.target.value,
                    preferred_option_id: event.target.value === "candidate"
                      ? editor.decision.preferred_option_id
                      : "",
                  })}
                >
                  {decisionStatuses.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
                </select>
              </label>
              <label>首选方案
                <input
                  readOnly
                  value={editor.decision.options.find((item) => item.id === editor.decision.preferred_option_id)?.title || "尚未选择"}
                />
              </label>
            </div>
            {projectWorkspace ? <MemoDecisionMatrixPreview options={editor.decision.options} preferredOptionId={editor.decision.preferred_option_id} /> : null}
            <div className="artifact-item-list">
              {editor.decision.options.map((item) => (
                <MemoDecisionOptionEditor
                  key={item.id}
                  item={item}
                  selected={item.id === editor.decision.preferred_option_id}
                  structured={projectWorkspace}
                  structuredReadOnly={projectWorkspaceReadOnly}
                  onSelect={() => updateDecision({ status: "candidate", preferred_option_id: item.id })}
                  onChange={(patch) => updateDecisionOption(item.id, patch)}
                  onRemove={() => removeDecisionOption(item.id)}
                  onReview={() => selectReviewTarget(targetKey("decision_options", item.id))}
                />
              ))}
              {!editor.decision.options.length ? <p className="artifact-empty-section">尚未记录候选方案；不能据此宣称已有最佳方案。</p> : null}
            </div>
            <label>选择或暂缓理由
              <textarea
                value={editor.decision.rationale}
                onChange={(event) => updateDecision({ rationale: event.target.value })}
                placeholder="说明为什么选择该方案、保留了哪些反证，或为什么暂缓。"
              />
            </label>
            <button type="button" className="secondary compact" onClick={() => selectReviewTarget("decision")}>核验选择理由证据</button>
            <p className="field-help">这里形成的是候选首选，不是自动决策或授权；主观判断不等于统计胜率，最终决定仍由用户确认。</p>
          </section>

          <section className="artifact-structured-section">
            <SectionHeader title="结论" help="稳定条目 ID 保证增删后证据不会错位" onAdd={() => addItem("conclusions")} />
            <div className="artifact-item-list">
              {editor.conclusions.map((item) => (
                <MemoSimpleItemEditor
                  key={item.id}
                  section="conclusions"
                  label="结论"
                  item={item}
                  onChange={updateItem}
                  onRemove={() => removeItem("conclusions", item.id)}
                  onReview={() => selectReviewTarget(targetKey("conclusions", item.id))}
                />
              ))}
              {!editor.conclusions.length ? <p className="artifact-empty-section">暂无结论，可明确保留为空。</p> : null}
            </div>
          </section>

          <section className="artifact-structured-section">
            <SectionHeader title="分歧" help="记录立场、处理状态、阻塞性与处置结论" onAdd={() => addItem("disagreements")} />
            <div className="artifact-item-list">
              {editor.disagreements.map((item) => (
                <MemoDisagreementEditor
                  key={item.id}
                  item={item}
                  onChange={updateItem}
                  onRemove={() => removeItem("disagreements", item.id)}
                  onReview={() => selectReviewTarget(targetKey("disagreements", item.id))}
                />
              ))}
              {!editor.disagreements.length ? <p className="artifact-empty-section">暂无已记录分歧。</p> : null}
            </div>
          </section>

          <section className="artifact-structured-section">
            <SectionHeader title="待验证" help="记录仍需补证或复核的事项" onAdd={() => addItem("unknowns")} />
            <div className="artifact-item-list">
              {editor.unknowns.map((item) => (
                <MemoSimpleItemEditor
                  key={item.id}
                  section="unknowns"
                  label="待验证项"
                  item={item}
                  onChange={updateItem}
                  onRemove={() => removeItem("unknowns", item.id)}
                  onReview={() => selectReviewTarget(targetKey("unknowns", item.id))}
                />
              ))}
              {!editor.unknowns.length ? <p className="artifact-empty-section">暂无待验证事项。</p> : null}
            </div>
          </section>

          <section className="artifact-structured-section">
            <SectionHeader title="待办" help="明确负责人、期限和进展状态" onAdd={() => addItem("actions")} />
            <div className="artifact-item-list">
              {editor.actions.map((item) => (
                <MemoActionEditor
                  key={item.id}
                  item={item}
                  onChange={updateItem}
                  onRemove={() => removeItem("actions", item.id)}
                  onReview={() => selectReviewTarget(targetKey("actions", item.id))}
                />
              ))}
              {!editor.actions.length ? <p className="artifact-empty-section">暂无待办事项。</p> : null}
            </div>
          </section>
        </div>

        <ArtifactEvidenceGraph
          state={evidenceGraphState}
          graph={evidenceGraph}
          activeTarget={activeTarget}
          onSelectRelation={selectGraphRelation}
        />
        <ArtifactEvidenceReview
          candidates={evidenceCandidates}
          targets={evidenceTargets}
          activeTarget={activeTarget}
          onTargetChange={selectReviewTarget}
          selectedEvidence={selectedEvidence}
          reviewByKey={activeReview}
          onToggle={toggleEvidence}
          onReviewChange={changeReview}
          onVersionDecision={decideVersion}
          reviewSummary={evidenceReview}
          onNextUnreviewed={goToNextUnreviewed}
          sourceDetailByKey={sourceDetailByKey}
          onLoadSourceDetail={loadEvidenceSourceDetail}
          focusedEvidenceKey={focusedEvidenceKey}
        />
        {roundEvidenceSourceState === "loading" && workingArtifact.round_id
          ? <p className="field-help artifact-note">正在校验并加载本轮冻结市场快照；不会读取当前实时行情。</p>
          : null}
        {roundEvidenceSourceState === "error" && workingArtifact.round_id
          ? <p className="field-help artifact-note">本轮冻结市场快照暂时无法读取；在恢复前不能把当前行情替代为本轮证据。</p>
          : null}
        {roundEvidenceSourceState === "untrusted" && workingArtifact.round_id
          ? <p className="field-help artifact-note">证据接口未声明本轮来源具有权威性；当前不提供任何房间资料或消息作为替代候选。</p>
          : null}
        {selectedAuthoritativeSourceMissing
          ? <p className="field-help artifact-note">权威来源响应遗漏了已绑定证据。为避免保存时静默删除旧引用，当前编辑只读；请恢复来源接口后重试。</p>
          : null}
        {workingArtifact.status !== "CONFIRMED" && workingArtifact.content?.generation_notes
          ? <p className="field-help artifact-note">{artifactEditorDisplayText(workingArtifact.content.generation_notes)}</p>
          : null}
        {confirmDisabledReason ? <p className="field-help artifact-note">{confirmDisabledReason}</p> : null}
        {workingArtifact.status === "CONFIRMED" ? (
          <Suspense fallback={<ProjectReadinessFallback artifactVersion={workingArtifact.version} />}>
            <ProjectReadinessPanel
              key={JSON.stringify([workingArtifact.id, workingArtifact.version, projectReadinessContribution?.contractHash || "legacy"])}
              room={room}
              artifact={workingArtifact}
              slot={artifactWorkspaceSlot}
              contribution={projectReadinessContribution}
              showLegacyFallback={hasProjectWorkspaceFootprint(workingArtifact.content)}
            />
          </Suspense>
        ) : null}
        {workingArtifact.status === "CONFIRMED" && storageCandidateExperimentAllowed ? (
          <CandidateExperimentPanel
            key={JSON.stringify([workingArtifact.id, workingArtifact.version, workingArtifact.governance_snapshot?.attestation_sha256 || ""])}
            room={room}
            artifact={workingArtifact}
            readOnly={storageWorkspaceReadOnly}
            readOnlyReason={storageWorkspaceReason}
          />
        ) : null}
        {workingArtifact.status === "CONFIRMED" ? (
          <MemoUserFinalDecisionSection
            key={JSON.stringify([workingArtifact.id, workingArtifact.version])}
            artifact={workingArtifact}
            onSubmit={onUserDecision}
          />
        ) : null}
        </fieldset>
        {mutationError ? <p className="artifact-mutation-error" role="alert">{mutationError}</p> : null}
        <footer className="artifact-dialog-footer">
          <button type="button" className="secondary" disabled={!exportControl.canRun} onClick={exportCurrentArtifact}><Download size={14} aria-hidden="true" />{mutationAction === "export" ? "正在导出…" : "导出 Markdown"}</button>
          <span>
            {workingArtifact.status !== "CONFIRMED" ? (
              <button
                className="secondary"
                type="button"
                disabled={!progressControl.canRun}
                onClick={saveProgress}
              >
                <Save aria-hidden="true" size={14} />{mutationAction === "progress" ? "正在保存…" : "保存进度并继续"}
              </button>
            ) : null}
            <button className="secondary" type="submit" disabled={!draftControl.canRun}>
              <Save aria-hidden="true" size={14} />{mutationAction === "draft" ? "正在保存…" : "保存草稿"}
            </button>
            {workingArtifact.status !== "CONFIRMED" ? (
              <button
                className="primary"
                type="button"
                disabled={!confirmControl.canRun}
                title={confirmDisabledReason || "保存当前草稿并请求后端完成证据确认门"}
                onClick={saveAndConfirm}
              >
                <CheckCircle2 aria-hidden="true" size={14} />{mutationAction === "confirm" ? "正在确认…" : "保存并确认"}
              </button>
            ) : null}
          </span>
        </footer>
        {progressNotice ? <p className="field-help artifact-save-progress-note" role="status">{progressNotice}</p> : null}
      </form>
    </div>
  );
}

const MemoArtifactEditor = memo(ArtifactEditor);

export const ArtifactDialog = memo(function ArtifactDialog({ artifact, room, pluginRegistry, pluginLifecycle, open, messages, materials, onClose, onSave, onConfirm, onUserDecision, onExport, restoreFocusRef }) {
  const [retainedArtifact, setRetainedArtifact] = useState(null);
  const [editorSession, setEditorSession] = useState(0);
  const capturedRestoreFocusRef = useRef(null);
  const wasOpenRef = useRef(false);
  const surfaceOpen = Boolean(open && artifact);
  const renderedArtifact = artifact || retainedArtifact;
  useLayoutEffect(() => {
    if (artifact) setRetainedArtifact(artifact);
    if (surfaceOpen && !wasOpenRef.current) {
      setEditorSession((current) => current + 1);
      if (!restoreFocusRef) {
        const activeElement = document.activeElement;
        capturedRestoreFocusRef.current = activeElement && activeElement !== document.body
          ? activeElement
          : null;
      }
    }
    wasOpenRef.current = surfaceOpen;
  }, [artifact, restoreFocusRef, surfaceOpen]);
  useEffect(() => {
    if (surfaceOpen || !retainedArtifact) return undefined;
    let releaseFrame = 0;
    const restoreFrame = globalThis.requestAnimationFrame(() => {
      releaseFrame = globalThis.requestAnimationFrame(() => setRetainedArtifact(null));
    });
    return () => {
      globalThis.cancelAnimationFrame(restoreFrame);
      if (releaseFrame) globalThis.cancelAnimationFrame(releaseFrame);
    };
  }, [retainedArtifact, surfaceOpen]);
  if (!renderedArtifact) return null;
  return (
    <MemoArtifactEditor
      key={JSON.stringify([renderedArtifact.id, renderedArtifact.version, editorSession])}
      artifact={renderedArtifact}
      room={room}
      pluginRegistry={pluginRegistry}
      pluginLifecycle={pluginLifecycle}
      open={surfaceOpen}
      messages={messages}
      materials={materials}
      onClose={onClose}
      onSave={onSave}
      onConfirm={onConfirm}
      onUserDecision={onUserDecision}
      onExport={onExport}
      restoreFocusRef={restoreFocusRef || capturedRestoreFocusRef}
    />
  );
});
