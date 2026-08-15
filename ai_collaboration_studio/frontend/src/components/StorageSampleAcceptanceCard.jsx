import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  Database,
  FileCheck2,
  History,
  Layers3,
  Search,
  ShieldCheck,
  UserCheck,
  Users,
} from "lucide-react";
import { parseStorageSampleAcceptance } from "../storageSampleAcceptance";
import { storageAcceptanceAnnouncementText } from "../liveRegionAnnouncements";

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

function StageMarker({ stage }) {
  const Icon = STAGE_ICONS[stage.id] || CircleDashed;
  return <span className="storage-acceptance-stage-marker"><Icon size={14} aria-hidden="true" /></span>;
}

export function StorageSampleAcceptanceCard({ acceptance }) {
  const model = parseStorageSampleAcceptance(acceptance);
  if (!model.applicable) return null;

  const hasLegacyHistory = Boolean(model.legacyNotice);
  const firstBlocker = model.blockers[0];
  const nextAction = model.nextActions[0];

  return (
    <section
      className={`inspector-section storage-sample-acceptance-card state-${model.state}`}
      aria-label="美国存储产业样板验收"
    >
      <div className="screen-reader-announcer" role="status" aria-live="polite" aria-atomic="true">
        {storageAcceptanceAnnouncementText(model)}
      </div>
      <header className="storage-acceptance-heading">
        <span>
          <small>MU · SNDK · WDC · STX</small>
          <strong><ShieldCheck size={15} aria-hidden="true" />样板验收链</strong>
        </span>
        <em>{model.stateLabel}</em>
      </header>

      {hasLegacyHistory ? (
        <div className="storage-acceptance-legacy" role="note">
          <History size={14} aria-hidden="true" />
          <span>
            <strong>{model.legacyNotice}</strong>
            <small>{model.legacyRoundIds.length
              ? `保留 ${model.legacyRoundIds.length} 轮历史审计，不参与当前 v3 判定。`
              : "历史内容仍可查看，但必须从新轮重新验收。"}</small>
          </span>
        </div>
      ) : null}

      {model.decisionStateNotice ? (
        <div className={`storage-acceptance-decision-state ${model.state}`} role="note">
          <UserCheck size={14} aria-hidden="true" />
          <span>{model.decisionStateNotice}</span>
        </div>
      ) : null}

      <ol className="storage-acceptance-rail">
        {model.stages.map((stage, index) => (
          <li className={`storage-acceptance-stage ${stage.state}`} key={stage.id}>
            <StageMarker stage={stage} />
            <div>
              <span className="storage-acceptance-stage-title">
                <b>{String(index + 1).padStart(2, "0")}</b>
                <strong>{stage.label}</strong>
                <em>{stage.stateLabel}</em>
              </span>
              <small>{stage.detail}</small>
            </div>
            {stage.metric ? <code aria-label={`${stage.label}进度`}>{stage.metric}</code> : null}
          </li>
        ))}
      </ol>

      <div className={`storage-statistics-gate ${model.statistics.qualified ? "qualified" : "insufficient"}`}>
        {model.statistics.qualified
          ? <CheckCircle2 size={14} aria-hidden="true" />
          : <CircleDashed size={14} aria-hidden="true" />}
        <span>
          <strong>{model.statisticsLabel}</strong>
          <small>{model.statistics.qualified
            ? "这里只表示统计样本门已达到最低要求，仍需查看方法与区间。"
            : "未达到门槛前不展示统计胜率；模型主观判断不计作样本。"}</small>
        </span>
      </div>

      {firstBlocker ? (
        <div className="storage-acceptance-blocker">
          <AlertTriangle size={13} aria-hidden="true" />
          <span><strong>{firstBlocker.title}</strong>{firstBlocker.detail ? <small>{firstBlocker.detail}</small> : null}</span>
        </div>
      ) : null}

      {nextAction ? <p className="storage-acceptance-next"><span>下一步</span>{nextAction}</p> : null}

      <footer className={model.safetyReady ? "safe" : "unsafe"}>
        <ShieldCheck size={13} aria-hidden="true" />
        <span>{model.safetyReady
          ? "只读研究、回测与模拟；用户保留最终决定，无实盘执行。"
          : "安全边界字段未通过，当前结果不能进入验收。"}</span>
      </footer>
    </section>
  );
}
