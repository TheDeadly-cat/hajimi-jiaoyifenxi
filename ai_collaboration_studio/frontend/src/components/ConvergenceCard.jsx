import { AlertTriangle, CheckCircle2, CircleDashed, Scale, ShieldCheck } from "lucide-react";
import { memo } from "react";
import { candidateGovernanceRows } from "../candidateGovernance";
import { convergenceAnnouncementText } from "../liveRegionAnnouncements";
import "../styles/convergence-polish.css";

const EMPTY_LIST = Object.freeze([]);
const USER_DECISION_STATES = Object.freeze({
  user_supported: { label: "用户已支持", tone: "support" },
  user_held: { label: "用户已保留", tone: "hold" },
  returned_for_revision: { label: "用户已退回", tone: "return" },
  awaiting_user_decision: { label: "等待用户最终决定", tone: "pending" },
  artifact_required: { label: "等待用户最终决定", tone: "pending" },
});

const ConvergenceAnnouncer = memo(function ConvergenceAnnouncer({ convergence }) {
  return (
    <div
      className="screen-reader-announcer"
      role="status"
      aria-live="polite"
      aria-atomic="true"
    >
      {convergenceAnnouncementText(convergence)}
    </div>
  );
});

const GateRow = memo(function GateRow({ ready, label, detail }) {
  const Icon = ready ? CheckCircle2 : CircleDashed;
  return (
    <div className={ready ? "convergence-gate ready" : "convergence-gate"} role="listitem">
      <Icon aria-hidden="true" size={13} />
      <span><strong>{label}</strong><small>{detail}</small></span>
    </div>
  );
});

const UserDecisionGateRow = memo(function UserDecisionGateRow({ gate }) {
  const state = USER_DECISION_STATES[gate.status] || USER_DECISION_STATES.awaiting_user_decision;
  const version = Number(gate.artifact_version || 0);
  const versionDetail = version > 0 ? `绑定产物 v${version}` : "尚无可绑定产物版本";
  const safetyVerified = gate.can_autonomously_decide === false
    && gate.execution_capability === "none"
    && gate.live_trading_allowed === false;
  const Icon = gate.ready ? CheckCircle2 : CircleDashed;

  return (
    <div className={`convergence-gate user-decision-gate ${state.tone}`} role="group" aria-label="用户最终决定门">
      <Icon aria-hidden="true" size={13} />
      <span>
        <strong>{state.label}</strong>
        <small>
          {versionDetail} · {safetyVerified
            ? "最终决定仅由用户记录 · 无执行权限 · 禁止实盘"
            : "仅记录决定，不授权交易或其他资金动作"}
        </small>
      </span>
    </div>
  );
});

export const ConvergenceCard = memo(function ConvergenceCard({ convergence, running }) {
  if (!convergence) {
    return (
      <section id="inspector-convergence" tabIndex={-1} className="inspector-section convergence-section" data-convergence-tone="loading" aria-label="收敛与决策门">
        <ConvergenceAnnouncer convergence={null} />
        <div className="section-heading"><strong><Scale aria-hidden="true" size={15} />收敛与决策门</strong><span>正在计算</span></div>
      </section>
    );
  }

  const discussion = convergence.discussion_gate || {};
  const counter = convergence.counterargument_gate || {};
  const candidateGovernanceProjection = candidateGovernanceRows(convergence);
  const candidateGovernance = Array.isArray(candidateGovernanceProjection)
    ? candidateGovernanceProjection
    : EMPTY_LIST;
  const projectWorkspace = convergence.project_workspace || {};
  const evidence = convergence.evidence_gate || {};
  const decision = convergence.decision_gate || {};
  const userDecision = convergence.user_decision_gate || {};
  const data = convergence.data_gate || {};
  const simulation = convergence.simulation_gate || {};
  const portfolio = convergence.portfolio_gate || {};
  const blockers = Array.isArray(convergence.blockers) ? convergence.blockers : EMPTY_LIST;
  const nextActions = Array.isArray(convergence.next_actions) ? convergence.next_actions : EMPTY_LIST;
  const nextAction = nextActions[0] || "等待本轮目标。";
  const marketSymbols = Array.isArray(data.market_symbols) ? data.market_symbols : EMPTY_LIST;
  const technicalGateStates = [
    Boolean(discussion.ready),
    Boolean(counter.ready),
    ...candidateGovernance.map((gate) => Boolean(gate.ready)),
    ...(projectWorkspace.applicable ? [Boolean(projectWorkspace.ready)] : EMPTY_LIST),
    Boolean(evidence.ready),
    ...(decision.applicable ? [Boolean(decision.ready)] : EMPTY_LIST),
    Boolean(data.ready),
    ...(simulation.applicable ? [Boolean(simulation.statistical_claim_allowed)] : EMPTY_LIST),
    ...(portfolio.applicable ? [Boolean(portfolio.ready)] : EMPTY_LIST),
  ];
  const technicalGateCount = technicalGateStates.length;
  const satisfiedTechnicalGateCount = technicalGateStates.filter(Boolean).length;
  const remainingTechnicalGateCount = technicalGateCount - satisfiedTechnicalGateCount;
  const tone = convergence.can_present_candidate_best
    ? "candidate"
    : convergence.can_host_finish ? "review" : "blocked";
  const badgeLabel = running
    ? "动态检查"
    : tone === "candidate"
      ? "条件候选可展示"
      : tone === "review" ? "等待用户复核" : "门禁未通过";
  const OverviewIcon = convergence.can_host_finish ? ShieldCheck : CircleDashed;

  return (
    <section id="inspector-convergence" tabIndex={-1} className="inspector-section convergence-section" data-convergence-tone={tone} aria-label="收敛与决策门">
      <ConvergenceAnnouncer convergence={convergence} />
      <div className="section-heading">
        <strong><Scale aria-hidden="true" size={15} />收敛与决策门</strong>
        <span className={`convergence-badge ${tone}`}>{badgeLabel}</span>
      </div>

      <div className={`convergence-overview ${tone}`}>
        <OverviewIcon aria-hidden="true" size={17} />
        <div>
          <strong>{convergence.can_host_finish ? "讨论可结束，仍需用户复核" : "主持人不能提前宣布收敛"}</strong>
          <small>{convergence.can_present_candidate_best ? "证据与数据门已通过，可展示条件化候选首选，仍待用户复核。" : "当前只能补齐角色、证据或数据缺口。"}</small>
        </div>
      </div>

      <div className="convergence-progress" aria-label="技术门禁证据进度">
        <div>
          <span>技术门禁证据</span>
          <data value={satisfiedTechnicalGateCount}>
            {satisfiedTechnicalGateCount} / {technicalGateCount}
          </data>
        </div>
        <progress
          max={technicalGateCount}
          value={satisfiedTechnicalGateCount}
          aria-label={`已满足 ${satisfiedTechnicalGateCount} / ${technicalGateCount} 项技术门禁`}
        />
        <small>
          {remainingTechnicalGateCount > 0
            ? `尚有 ${remainingTechnicalGateCount} 项技术门未满足；进度仅表示证据条件。`
            : "技术门均已满足；仍需用户复核，不产生执行授权。"}
        </small>
      </div>

      {blockers[0] ? (
        <div className="convergence-primary-blocker" role="note">
          <AlertTriangle aria-hidden="true" size={14} />
          <span>
            <em>首要阻断</em>
            <strong>{blockers[0].title}</strong>
            <small>{blockers[0].detail}</small>
          </span>
        </div>
      ) : null}

      <div className="convergence-next">
        <span>下一步</span>
        <strong>{nextAction}</strong>
      </div>

      <UserDecisionGateRow gate={userDecision} />

      {projectWorkspace.applicable && projectWorkspace.focus ? (
        <div className="project-workspace-focus">
          <span>{projectWorkspace.frozen ? "本轮项目焦点" : "下一轮项目焦点"}</span>
          <strong>{projectWorkspace.focus.title}</strong>
          <small>{projectWorkspace.focus.detail}</small>
        </div>
      ) : null}

      <details
        className="convergence-details"
        key={`${convergence.room_id || "room"}:${convergence.round_id || "round"}`}
      >
        <summary>
          <span>门禁与其他阻断</span>
          <small>{technicalGateCount} 项门禁{blockers.length > 1 ? ` · ${blockers.length - 1} 项其他阻断` : ""}</small>
        </summary>
        <div className="convergence-details-body">
          {blockers.length > 1 ? (
            <div className="convergence-blockers">
              {blockers.slice(1).map((blocker, blockerIndex) => (
                <div key={`${blocker.code || "blocker"}:${blockerIndex}`}><AlertTriangle aria-hidden="true" size={12} /><span><strong>{blocker.title}</strong><small>{blocker.detail}</small></span></div>
              ))}
            </div>
          ) : null}
          <div className="convergence-gates" role="list" aria-label="收敛技术门禁">
            <GateRow
              ready={Boolean(discussion.ready)}
              label="讨论覆盖"
              detail={`${discussion.successful_member_count || 0} / ${discussion.required_success_count || 0} 位不同角色成功发言`}
            />
            <GateRow
              ready={Boolean(counter.ready)}
              label="反证与风控"
              detail={`${counter.successful_count || 0} / ${counter.configured_count || 0} 位反方角色已回应`}
            />
            {candidateGovernance.map((gate) => (
              <GateRow
                key={gate.id}
                ready={gate.ready}
                label={gate.label}
                detail={gate.detail}
              />
            ))}
            {projectWorkspace.applicable ? (
              <GateRow
                ready={Boolean(projectWorkspace.ready)}
                label="项目研究工作区"
                detail={
                  `${projectWorkspace.requirement_count || 0} 项需求 · `
                  + `${projectWorkspace.risk_count || 0} 项风险 · `
                  + `${projectWorkspace.option_count || 0} 个方案 · `
                  + (projectWorkspace.frozen ? "本轮已冻结" : "当前版本")
                }
              />
            ) : null}
            <GateRow
              ready={Boolean(evidence.ready)}
              label="证据复核"
              detail={evidence.artifact_id ? `${evidence.evidence_count || 0} 条关系 · ${evidence.label}` : "本轮尚未生成可审计产物"}
            />
            {decision.applicable ? (
              <GateRow
                ready={Boolean(decision.ready)}
                label="多方案决策板"
                detail={decision.ready
                  ? `${decision.option_count || 0} 个方案 · 已记录首选及理由`
                  : `${decision.option_count || 0} / 2 个可比较方案 · 尚不能宣称候选最优`}
              />
            ) : null}
            <GateRow
              ready={Boolean(data.ready)}
              label="统一数据截面"
              detail={data.market_snapshot_required ? `${marketSymbols.length} / 4 个目标标的` : `${data.active_material_count || 0} 份共享资料`}
            />
            {simulation.applicable ? (
              <GateRow
                ready={Boolean(simulation.statistical_claim_allowed)}
                label="模拟验证"
                detail={`${simulation.sample_count || 0} / ${simulation.minimum_samples || 20} 个用户确认且已到期样本`}
              />
            ) : null}
            {portfolio.applicable ? (
              <GateRow
                ready={Boolean(portfolio.ready)}
                label="组合风险预算"
                detail={portfolio.ready
                  ? `${portfolio.confirmed_count || 0} 个已确认方案通过风险门`
                  : `${portfolio.draft_count || 0} 个草稿仍需复算与用户确认`}
              />
            ) : null}
          </div>
        </div>
      </details>

      <p className="convergence-boundary">{convergence.boundary || "技术门通过不等于用户批准，也不授予任何外部执行权限。"}</p>
    </section>
  );
});
