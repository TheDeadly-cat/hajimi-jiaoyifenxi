import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  Database,
  FileCheck2,
  History,
  Layers3,
  ListChecks,
  Search,
  ShieldCheck,
  UserCheck,
  Users,
} from "lucide-react";
import { useId, useMemo } from "react";
import { parseStorageSampleAcceptance } from "../storageSampleAcceptance";
import { storageAcceptanceAnnouncementText } from "../liveRegionAnnouncements";
import "../styles/storage-sample-acceptance-card.css";

const BLOCKER_LIMIT = 8;
const ACTION_LIMIT = 6;
const TEXT_LIMIT = 1000;

const STAGE_ICONS = {
  market_data: Database,
  market_snapshot: Database,
  research_evidence: ShieldCheck,
  discussion: Users,
  artifact: FileCheck2,
  evidence: Search,
  user_decision: UserCheck,
  paper_portfolio: Layers3,
  simulation: Activity,
};

function arrayValue(value) {
  return Array.isArray(value) ? value : [];
}

function objectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function boundedText(value, fallback = "", limit = TEXT_LIMIT) {
  const text = String(value ?? "").trim();
  const normalized = text || fallback;
  return normalized.length <= limit ? normalized : normalized.slice(0, limit) + "...";
}

function safeToken(value, fallback = "unknown") {
  const token = String(value || "")
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48);
  return token || fallback;
}

function stageView(stage, index) {
  const source = objectValue(stage);
  const id = boundedText(source.id, "stage-" + (index + 1), 100);
  return {
    id,
    key: JSON.stringify(["storage-acceptance-stage", id, index]),
    label: boundedText(source.label, "未命名阶段", 160),
    state: safeToken(source.state, "unknown"),
    stateLabel: boundedText(source.stateLabel, "状态未记录", 100),
    detail: boundedText(source.detail, "阶段说明未记录。"),
    metric: boundedText(source.metric, "", 160),
  };
}

function blockerView(blocker, index) {
  const source = objectValue(blocker);
  return {
    key: JSON.stringify(["storage-acceptance-blocker", source.code, source.title, index]),
    title: boundedText(source.title, "阻断项 " + (index + 1), 200),
    detail: boundedText(source.detail),
  };
}

function acceptanceView(model) {
  const stages = arrayValue(model.stages).map(stageView);
  const blockers = arrayValue(model.blockers).map(blockerView);
  const nextActions = arrayValue(model.nextActions)
    .map((action) => boundedText(action))
    .filter(Boolean);
  const statistics = objectValue(model.statistics);

  return {
    actions: nextActions.slice(0, ACTION_LIMIT),
    actionsRemaining: Math.max(0, nextActions.length - ACTION_LIMIT),
    blockerCount: blockers.length,
    blockers: blockers.slice(0, BLOCKER_LIMIT),
    blockersRemaining: Math.max(0, blockers.length - BLOCKER_LIMIT),
    decisionStateNotice: boundedText(model.decisionStateNotice),
    legacyNotice: boundedText(model.legacyNotice),
    legacyRoundCount: arrayValue(model.legacyRoundIds).length,
    safetyReady: model.safetyReady === true,
    stages,
    state: safeToken(model.state, "unknown"),
    stateLabel: boundedText(model.stateLabel, "验收状态未记录", 120),
    statisticsLabel: boundedText(model.statisticsLabel, "统计样本状态未记录", 240),
    statisticsQualified: statistics.qualified === true,
  };
}

function StageMarker({ stage }) {
  const Icon = STAGE_ICONS[stage.id] || CircleDashed;
  return <span className="storage-acceptance-stage-marker"><Icon size={14} aria-hidden="true" /></span>;
}

export function StorageSampleAcceptanceCard({ acceptance }) {
  const model = useMemo(() => parseStorageSampleAcceptance(acceptance), [acceptance]);
  const view = useMemo(() => acceptanceView(model), [model]);
  const headingId = useId();

  if (!model.applicable) return null;

  const announcement = boundedText(
    storageAcceptanceAnnouncementText(model),
    "存储样板验收状态已更新。",
    1200,
  );

  return (
    <section
      className={"inspector-section storage-sample-acceptance-card storage-sample-acceptance-v2 state-" + view.state}
      data-acceptance-state={view.state}
      aria-labelledby={headingId}
    >
      <div className="screen-reader-announcer" role="status" aria-live="polite" aria-atomic="true">
        {announcement}
      </div>

      <header className="storage-acceptance-heading">
        <span>
          <small>MU · SNDK · WDC · STX</small>
          <strong id={headingId}><ShieldCheck size={16} aria-hidden="true" />样板验收链</strong>
        </span>
        <em>{view.stateLabel}</em>
      </header>

      <div className="storage-acceptance-overview" aria-label="验收覆盖概览">
        <span><small>验收阶段</small><strong>{view.stages.length}</strong></span>
        <span><small>阻断项</small><strong>{view.blockerCount}</strong></span>
        <span><small>后续动作</small><strong>{view.actions.length + view.actionsRemaining}</strong></span>
      </div>

      {view.legacyNotice ? (
        <div className="storage-acceptance-legacy" role="note">
          <History size={14} aria-hidden="true" />
          <span>
            <strong>{view.legacyNotice}</strong>
            <small>{view.legacyRoundCount
              ? "保留 " + view.legacyRoundCount + " 轮历史审计，不参与当前 v3 判定。"
              : "历史内容仍可查看，但必须从新轮重新验收。"}</small>
          </span>
        </div>
      ) : null}

      {view.decisionStateNotice ? (
        <div className={"storage-acceptance-decision-state " + view.state} role="note">
          <UserCheck size={14} aria-hidden="true" />
          <span>{view.decisionStateNotice}</span>
        </div>
      ) : null}

      <ol className="storage-acceptance-rail" aria-label="样板验收阶段">
        {view.stages.map((stage, index) => (
          <li className={"storage-acceptance-stage " + stage.state} data-stage-state={stage.state} key={stage.key}>
            <StageMarker stage={stage} />
            <div>
              <span className="storage-acceptance-stage-title">
                <b>{String(index + 1).padStart(2, "0")}</b>
                <strong>{stage.label}</strong>
                <em>{stage.stateLabel}</em>
              </span>
              <small>{stage.detail}</small>
            </div>
            {stage.metric ? <code aria-label={stage.label + "进度"}>{stage.metric}</code> : null}
          </li>
        ))}
      </ol>

      <div className={"storage-statistics-gate " + (view.statisticsQualified ? "qualified" : "insufficient")}>
        {view.statisticsQualified
          ? <CheckCircle2 size={14} aria-hidden="true" />
          : <CircleDashed size={14} aria-hidden="true" />}
        <span>
          <strong>{view.statisticsLabel}</strong>
          <small>{view.statisticsQualified
            ? "这里只表示统计样本门达到最低要求，仍需检查方法、区间与适用边界。"
            : "未达到门槛前不展示统计胜率；模型主观判断不计作样本。"}</small>
        </span>
      </div>

      {view.blockers.length ? (
        <section className="storage-acceptance-blockers" aria-label="当前阻断项">
          <header><AlertTriangle size={14} aria-hidden="true" /><span><strong>当前阻断项</strong><small>{view.blockerCount} 项</small></span></header>
          <ul>
            {view.blockers.map((blocker) => (
              <li key={blocker.key}><strong>{blocker.title}</strong>{blocker.detail ? <small>{blocker.detail}</small> : null}</li>
            ))}
          </ul>
          {view.blockersRemaining ? <p>另有 {view.blockersRemaining} 项未在紧凑视图展开。</p> : null}
        </section>
      ) : null}

      {view.actions.length ? (
        <section className="storage-acceptance-actions" aria-label="后续动作">
          <header><ListChecks size={14} aria-hidden="true" /><span><strong>后续动作</strong><small>{view.actions.length + view.actionsRemaining} 项</small></span></header>
          <ol>
            {view.actions.map((action, index) => (
              <li key={JSON.stringify(["storage-acceptance-action", action, index])}><span>{String(index + 1).padStart(2, "0")}</span><p>{action}</p></li>
            ))}
          </ol>
          {view.actionsRemaining ? <p>另有 {view.actionsRemaining} 项未在紧凑视图展开。</p> : null}
        </section>
      ) : null}

      <footer className={view.safetyReady ? "safe" : "unsafe"}>
        <ShieldCheck size={13} aria-hidden="true" />
        <span>{view.safetyReady
          ? "仅限只读研究、回测与模拟；样本验收不代表项目就绪，用户保留最终决定，无实盘执行。"
          : "安全边界字段未通过，当前结果不能进入验收。"}</span>
      </footer>
    </section>
  );
}
