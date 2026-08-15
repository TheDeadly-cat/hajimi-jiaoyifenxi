import {
  AlertTriangle,
  ArrowDown,
  BarChart3,
  CircleDashed,
  FileCheck2,
  FlaskConical,
  GitBranch,
  History,
  Link2,
  LoaderCircle,
  Plus,
  ShieldCheck,
  Unlink,
  UserCheck,
  WalletCards,
} from "lucide-react";
import { useMemo, useState } from "react";
import { bindableAiProposals } from "../observationLineage";


const PACKAGE_STATES = Object.freeze({
  active: { label: "当前支持链", tone: "active" },
  non_actionable: { label: "当前决定不派生", tone: "paused" },
  stale: { label: "历史决定链", tone: "stale" },
  chain_broken: { label: "谱系校验失败", tone: "broken" },
});

const ACTION_LABELS = Object.freeze({
  support: "支持候选",
  hold: "暂时保留",
  return: "退回修订",
});


function isPortfolioEvent(event) {
  return String(event?.resource_type || "").toLowerCase() === "simulation.paper_portfolio";
}


function isObservationEvent(event) {
  return String(event?.resource_type || "").toLowerCase().includes("observation");
}


function isWalkForwardEvent(event) {
  return String(event?.resource_type || "").toLowerCase().includes("walk_forward");
}


function eventRevision(event) {
  return String(event?.resource_revision || event?.resource_snapshot?.version || "");
}


function runPortfolioVersion(run) {
  const direct = Number(run?.portfolio_version);
  if (Number.isInteger(direct) && direct > 0) return direct;
  const nested = Number(run?.result?.portfolio_version);
  return Number.isInteger(nested) && nested > 0 ? nested : 0;
}


function shortId(value) {
  const text = String(value || "");
  return text.length > 12 ? `…${text.slice(-8)}` : text || "未记录";
}


function createdTime(value) {
  const date = new Date(Number(value) || String(value || ""));
  if (Number.isNaN(date.getTime())) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}


function packageEvents(decisionPackage) {
  return (Array.isArray(decisionPackage?.lineage) ? decisionPackage.lineage : [])
    .slice()
    .sort((left, right) => (
      (Number(left.sequence_no) || 0) - (Number(right.sequence_no) || 0)
      || String(left.id || "").localeCompare(String(right.id || ""))
    ));
}


export function decisionPackageSource(decisionPackage, event = null) {
  const anchor = decisionPackage?.anchor || {};
  const selectedOption = anchor.selected_option || {};
  return {
    package_id: String(decisionPackage?.package_id || anchor.user_decision_id || ""),
    package_state: String(decisionPackage?.state || "stale"),
    package_integrity_ok: decisionPackage?.integrity_ok === true && anchor.integrity_ok === true,
    user_decision_id: String(anchor.user_decision_id || ""),
    artifact_id: String(anchor.artifact_id || ""),
    artifact_version: Number(anchor.artifact_version) || 0,
    action: String(anchor.action || ""),
    decision_version: String(anchor.decision_version || ""),
    ai_preferred_option_id: String(anchor.ai_preferred_option_id || ""),
    selected_option_id: String(anchor.selected_option_id || ""),
    selected_is_ai_preferred: anchor.selected_is_ai_preferred === true,
    preferred_option_id: String(anchor.selected_option_id || anchor.preferred_option_id || ""),
    selected_option_title: String(selectedOption.title || selectedOption.name || "已选候选方案"),
    candidate_simulation_seed: anchor.candidate_simulation_seed || null,
    relation_type: String(event?.relation_type || ""),
    resource_revision: eventRevision(event),
    derivation_note: String(event?.relation_note || ""),
  };
}


export function buildPortfolioLineageIndex(decisionPackages) {
  const index = new Map();
  for (const decisionPackage of decisionPackages || []) {
    for (const event of packageEvents(decisionPackage)) {
      if (!isPortfolioEvent(event) || !event.resource_id) continue;
      const resourceId = String(event.resource_id);
      const entries = index.get(resourceId) || [];
      entries.push({ decisionPackage, event });
      index.set(resourceId, entries);
    }
  }
  for (const entries of index.values()) {
    entries.sort((left, right) => (
      (Number(right.event.sequence_no) || 0) - (Number(left.event.sequence_no) || 0)
    ));
  }
  return index;
}


function latestResourceEvents(events, predicate) {
  const byResource = new Map();
  for (const event of events) {
    if (!predicate(event) || !event.resource_id) continue;
    byResource.set(String(event.resource_id), event);
  }
  return [...byResource.values()];
}


function walkForwardState(run) {
  const result = run?.result || {};
  const summary = result.summary || {};
  const status = String(summary.adequacy_status || summary.status || result.state || "insufficient");
  const folds = Number(summary.non_overlapping_test_fold_count ?? summary.independent_fold_count ?? 0) || 0;
  return {
    label: status === "sufficient" ? "样本达到最低门槛" : "历史样本仍不足",
    tone: status === "sufficient" ? "ready" : "pending",
    detail: `${folds} 个非重叠窗口 · 固定纸面方案回放`,
  };
}


function LineageNode({ icon: Icon, eyebrow, title, detail, tone = "pending" }) {
  return (
    <div className={`decision-lineage-node ${tone}`}>
      <Icon size={14} />
      <span>
        <small>{eyebrow}</small>
        <strong>{title}</strong>
        <em>{detail}</em>
      </span>
    </div>
  );
}


function ProposalBindingControl({ proposals, membersById, anchor, sourceBranch, onBind }) {
  const [proposalId, setProposalId] = useState("");
  const [derivationNote, setDerivationNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  if (!sourceBranch || !onBind || !proposals.length) return null;
  const effectiveProposalId = proposals.some((item) => item.id === proposalId)
    ? proposalId
    : proposals[0]?.id || "";
  const selected = proposals.find((item) => item.id === effectiveProposalId) || proposals[0];
  const submit = async (event) => {
    event.preventDefault();
    const note = derivationNote.trim();
    if (!selected || note.length < 3 || busy) return;
    setBusy(true);
    setError("");
    try {
      await onBind(selected, {
        user_decision_id: String(anchor.user_decision_id || ""),
        source_portfolio_id: String(sourceBranch.event?.resource_id || ""),
        source_portfolio_version: Number(sourceBranch.revision) || 0,
        derivation_note: note,
      });
      setDerivationNote("");
    } catch (requestError) {
      setError(requestError.message || "绑定失败，请检查决定与组合版本。");
    } finally {
      setBusy(false);
    }
  };
  return (
    <details className="decision-lineage-proposal-bind">
      <summary><Link2 size={11} />采纳 AI 原提案（{proposals.length}）</summary>
      <form onSubmit={submit}>
        <label>待绑定提案
          <select value={effectiveProposalId} onChange={(event) => setProposalId(event.target.value)}>
            {proposals.map((item) => (
              <option key={item.id} value={item.id}>
                {membersById.get(String(item.created_by || ""))?.name || "历史 AI"}
                {` · ${String(item.symbol || "").replace("US.", "")} · ${item.direction} · ${item.horizon_days}日 · ${item.model_confidence ?? "?"}%`}
              </option>
            ))}
          </select>
        </label>
        <label>采纳说明
          <textarea
            required
            minLength={3}
            maxLength={1000}
            value={derivationNote}
            onChange={(event) => setDerivationNote(event.target.value)}
            placeholder="说明该 AI 提案如何验证当前支持方案及已确认组合；原作者、方法、阈值和置信度不会被改写。"
          />
        </label>
        {error ? <p className="decision-lineage-proposal-error">{error}</p> : null}
        <button type="submit" disabled={busy || derivationNote.trim().length < 3}>
          {busy ? <LoaderCircle className="spin" size={11} /> : <Link2 size={11} />}
          绑定原提案
        </button>
      </form>
    </details>
  );
}


function DecisionPackageCard({
  decisionPackage,
  membersById,
  unlinkedAiProposals,
  paperPortfolioById,
  walkForwardRunsByPortfolio,
  onCreatePortfolio,
  onCreateObservation,
  onBindObservation,
}) {
  const anchor = decisionPackage.anchor || {};
  const events = packageEvents(decisionPackage);
  const portfolioEvents = latestResourceEvents(events, isPortfolioEvent);
  const observationEvents = latestResourceEvents(events, isObservationEvent);
  const explicitWalkForwardEvents = latestResourceEvents(events, isWalkForwardEvent);
  const selectedOption = anchor.selected_option || {};
  const state = PACKAGE_STATES[decisionPackage.state] || PACKAGE_STATES.stale;
  const safeBoundary = decisionPackage.execution_capability === "none"
    && decisionPackage.live_trading_allowed === false
    && decisionPackage.can_autonomously_decide === false;
  const integrityReady = decisionPackage.integrity_ok === true
    && anchor.integrity_ok === true
    && safeBoundary;
  const hasImplementation = events.some((event) => event.relation_type === "implements");
  const candidateSimulationSeed = anchor.candidate_simulation_seed || null;
  const candidateSimulationBlocked = candidateSimulationSeed?.applicable === true
    && candidateSimulationSeed?.ready !== true;
  const canCreatePortfolioBase = decisionPackage.state === "active"
    && anchor.current === true
    && anchor.action === "support"
    && integrityReady
    && !hasImplementation;
  const canCreatePortfolio = canCreatePortfolioBase && !candidateSimulationBlocked;

  const portfolioBranches = portfolioEvents.map((event) => {
    const portfolio = paperPortfolioById.get(String(event.resource_id));
    const revision = Number(eventRevision(event)) || 0;
    const exactLiveRevision = Number(portfolio?.version || 0) === revision;
    const mayShowRevision = decisionPackage.state === "stale" || exactLiveRevision;
    const runs = (mayShowRevision ? walkForwardRunsByPortfolio?.[event.resource_id] || [] : [])
      .filter((run) => runPortfolioVersion(run) === revision)
      .toSorted((left, right) => (Number(right.created_at) || 0) - (Number(left.created_at) || 0));
    return { event, portfolio, revision, exactLiveRevision, latestRun: runs[0] || null };
  });
  const portfolioRevisionById = new Map(
    portfolioBranches
      .filter((branch) => decisionPackage.state !== "active" || branch.exactLiveRevision)
      .map((branch) => [String(branch.event.resource_id), branch.revision]),
  );
  const currentObservationEvents = observationEvents.filter((event) => {
    const snapshot = event.resource_snapshot || {};
    const sourceId = String(snapshot.source_portfolio_id || "");
    return sourceId
      && portfolioRevisionById.get(sourceId) === Number(snapshot.source_portfolio_version || 0);
  });
  const currentWalkForwardEvents = explicitWalkForwardEvents.filter((event) => {
    const snapshot = event.resource_snapshot || {};
    const sourceId = String(snapshot.portfolio_id || "");
    return sourceId
      && portfolioRevisionById.get(sourceId) === Number(snapshot.portfolio_version || 0);
  });
  const observationStates = new Map();
  for (const event of currentObservationEvents) {
    const stateKey = String(event.resource_state || "UNKNOWN").toUpperCase();
    observationStates.set(stateKey, (observationStates.get(stateKey) || 0) + 1);
  }
  const observationStateDetail = [...observationStates.entries()]
    .map(([stateKey, count]) => `${stateKey} ${count}`)
    .join(" · ");
  const latestWalkForwardEvent = currentWalkForwardEvents.toSorted(
    (left, right) => (Number(right.sequence_no) || 0) - (Number(left.sequence_no) || 0),
  )[0] || null;
  const currentWalkForwardRuns = portfolioBranches.map((branch) => branch.latestRun).filter(Boolean);
  const latestRun = currentWalkForwardRuns.toSorted(
    (left, right) => (Number(right.created_at) || 0) - (Number(left.created_at) || 0),
  )[0] || null;
  const walkForwardSummary = latestRun ? walkForwardState(latestRun) : null;
  const packageBroken = decisionPackage.state === "chain_broken" || !integrityReady;
  const observationSourceBranch = portfolioBranches.find(({ event, portfolio, exactLiveRevision }) => (
    decisionPackage.state === "active"
    && anchor.current === true
    && anchor.action === "support"
    && !packageBroken
    && exactLiveRevision
    && event.integrity_ok === true
    && event.relation_type === "confirms"
    && String(event.resource_state || "").toUpperCase() === "CONFIRMED"
    && String(portfolio?.status || "").toUpperCase() === "CONFIRMED"
  ));
  const candidateTone = packageBroken ? "broken" : anchor.integrity_ok ? "ready" : "broken";
  const compatibleAiProposals = bindableAiProposals(
    unlinkedAiProposals,
    anchor.artifact_round_id,
  );
  const decisionTone = packageBroken
    ? "broken"
    : anchor.current && anchor.action === "support"
      ? "ready"
      : anchor.current ? "paused" : "stale";
  const selectionSummary = anchor.action === "support"
    ? `AI 首选 ${anchor.ai_preferred_option_id || "未记录"} · 用户选择 ${anchor.selected_option_id || "未记录"}${anchor.selected_is_ai_preferred === true ? "（一致）" : "（可不同）"}`
    : `AI 首选 ${anchor.ai_preferred_option_id || "未记录"} · 用户选择 无`;

  return (
    <article className={`decision-package-card ${state.tone}${packageBroken ? " broken" : ""}`}>
      <header>
        <span>
          <GitBranch size={14} />
          <strong>{state.label}</strong>
        </span>
        <em>{createdTime(anchor.created_at)}</em>
      </header>

      <div className="decision-lineage-primary-flow">
        <LineageNode
          icon={FileCheck2}
          eyebrow="候选产物"
          title={selectedOption.title || selectedOption.name || "候选方案快照不可用"}
          detail={`产物 ${shortId(anchor.artifact_id)} · v${anchor.artifact_version || "?"}`}
          tone={candidateTone}
        />
        <ArrowDown className="decision-lineage-arrow" size={13} aria-hidden="true" />
        <LineageNode
          icon={UserCheck}
          eyebrow="用户最终决定"
          title={ACTION_LABELS[anchor.action] || "决定状态未知"}
          detail={`${selectionSummary} · ${anchor.rationale || `决定 ${shortId(anchor.user_decision_id)}`}`}
          tone={decisionTone}
        />
        <ArrowDown className="decision-lineage-arrow" size={13} aria-hidden="true" />
        {portfolioBranches.length ? (
          <div className="decision-lineage-portfolio-stack">
            {portfolioBranches.map(({ event, portfolio, revision, exactLiveRevision }) => {
              const eventReady = event.integrity_ok === true && exactLiveRevision && !packageBroken;
              return (
                <LineageNode
                  key={`${event.resource_id}:${event.resource_revision}`}
                  icon={WalletCards}
                  eyebrow={event.relation_type === "implements" ? "关联模拟组合" : "组合版本事件"}
                  title={portfolio?.name || event.resource_snapshot?.name || shortId(event.resource_id)}
                  detail={`事件 v${revision || "?"} · ${event.resource_state || "状态未知"}${exactLiveRevision ? "" : " · 当前版本不同"}`}
                  tone={eventReady ? "ready" : packageBroken ? "broken" : "stale"}
                />
              );
            })}
          </div>
        ) : (
          <LineageNode
            icon={WalletCards}
            eyebrow="模拟组合"
            title={canCreatePortfolio ? "尚未建立关联组合" : "当前决定没有可派生组合"}
            detail={canCreatePortfolio ? "从已支持候选推导纸面权重与风险预算" : "不会从保留、退回或旧决定自动派生"}
            tone={packageBroken ? "broken" : "pending"}
          />
        )}
      </div>

      {canCreatePortfolio ? (
        <button className="decision-lineage-create" type="button" onClick={(event) => onCreatePortfolio(decisionPackage, event.currentTarget)}>
          <Plus size={13} />建立关联模拟组合
        </button>
      ) : null}
      {canCreatePortfolioBase && candidateSimulationBlocked ? (
        <p className="decision-lineage-warning">
          当前正式候选不能建立严格模拟映射：
          {candidateSimulationSeed?.issues?.[0]?.message || "候选规格不完整。"}
        </p>
      ) : null}

      <div className="decision-lineage-validation" aria-label="并行验证轨">
        <div className="decision-lineage-lane observation">
          <FlaskConical size={14} />
          <span>
            <small>前瞻验证轨</small>
            <strong>{currentObservationEvents.length ? `${currentObservationEvents.length} 条当前版本观察` : "尚无当前版本观察"}</strong>
            <em>
              {observationStateDetail ? `${observationStateDetail} · ` : ""}用户确认后等待到期；不作为回放输入。
              {observationEvents.length > currentObservationEvents.length ? ` 另有 ${observationEvents.length - currentObservationEvents.length} 条历史版本。` : ""}
            </em>
          </span>
          {observationSourceBranch && onCreateObservation ? (
            <button
              className="decision-lineage-lane-action"
              type="button"
              onClick={(event) => onCreateObservation(decisionPackage, observationSourceBranch, event.currentTarget)}
            >
              <Plus size={11} />建立前向观察
            </button>
          ) : null}
          <ProposalBindingControl
            proposals={compatibleAiProposals}
            membersById={membersById}
            anchor={anchor}
            sourceBranch={observationSourceBranch}
            onBind={onBindObservation}
          />
        </div>
        <div className={`decision-lineage-lane walk-forward ${walkForwardSummary?.tone || "pending"}`}>
          <BarChart3 size={14} />
          <span>
            <small>历史验证轨</small>
            <strong>{walkForwardSummary?.label || (currentWalkForwardEvents.length ? `${currentWalkForwardEvents.length} 条当前版本回放` : "尚无当前版本回放")}</strong>
            <em>
              {walkForwardSummary?.detail || (latestWalkForwardEvent
                ? `组合 v${latestWalkForwardEvent.resource_snapshot?.portfolio_version || "?"} · ${latestWalkForwardEvent.resource_state || "COMPLETED"}`
                : "仅接受与组合事件版本完全一致的结果。")}
              {explicitWalkForwardEvents.length > currentWalkForwardEvents.length ? ` 另有 ${explicitWalkForwardEvents.length - currentWalkForwardEvents.length} 条历史版本。` : ""}
            </em>
          </span>
        </div>
      </div>

      {packageBroken ? (
        <p className="decision-lineage-warning"><AlertTriangle size={12} />完整性或安全边界校验失败；该链仅供审计，不能继续派生。</p>
      ) : (
        <p className="decision-lineage-boundary"><ShieldCheck size={12} />仅研究与模拟 · 无自主决策权 · 禁止实盘</p>
      )}
    </article>
  );
}


export function DecisionLineagePanel({
  decisionPackages = [],
  members = [],
  paperPortfolios = [],
  observations = [],
  walkForwardRunsByPortfolio = {},
  onCreatePortfolio,
  onCreateObservation,
  onBindObservation,
}) {
  const membersById = useMemo(
    () => new Map((members || []).map((item) => [String(item.id), item])),
    [members],
  );
  const model = useMemo(() => {
    const packages = (decisionPackages || []).toSorted((left, right) => {
      const priority = { chain_broken: 0, active: 1, non_actionable: 2, stale: 3 };
      return (priority[left.state] ?? 9) - (priority[right.state] ?? 9)
        || (Number(right.anchor?.created_at) || 0) - (Number(left.anchor?.created_at) || 0);
    });
    const current = packages.filter((item) => item.state !== "stale");
    const historical = packages.filter((item) => item.state === "stale");
    const portfolioById = new Map((paperPortfolios || []).map((item) => [String(item.id), item]));
    const linkedPortfolioIds = new Set();
    const linkedObservationIds = new Set();
    const linkedPortfolioRevisions = new Map();
    for (const decisionPackage of packages) {
      for (const event of packageEvents(decisionPackage)) {
        if (isPortfolioEvent(event)) {
          const resourceId = String(event.resource_id || "");
          if (!resourceId) continue;
          linkedPortfolioIds.add(resourceId);
          const revisions = linkedPortfolioRevisions.get(resourceId) || new Set();
          revisions.add(Number(eventRevision(event)) || 0);
          linkedPortfolioRevisions.set(resourceId, revisions);
        }
        if (isObservationEvent(event) && event.resource_id) linkedObservationIds.add(String(event.resource_id));
      }
    }
    const unlinkedPortfolios = (paperPortfolios || []).filter((item) => !linkedPortfolioIds.has(String(item.id)));
    const unlinkedObservations = (observations || []).filter((item) => !linkedObservationIds.has(String(item.id)));
    let historicalWalkForwardCount = 0;
    let unlinkedWalkForwardCount = 0;
    for (const [portfolioId, runs] of Object.entries(walkForwardRunsByPortfolio || {})) {
      const revisions = linkedPortfolioRevisions.get(String(portfolioId));
      for (const run of runs || []) {
        if (!revisions) unlinkedWalkForwardCount += 1;
        else if (!revisions.has(runPortfolioVersion(run))) historicalWalkForwardCount += 1;
      }
    }
    return {
      current,
      historical,
      portfolioById,
      unlinkedPortfolios,
      unlinkedObservations,
      historicalWalkForwardCount,
      unlinkedWalkForwardCount,
    };
  }, [decisionPackages, observations, paperPortfolios, walkForwardRunsByPortfolio]);

  const renderPackage = (decisionPackage) => (
    <DecisionPackageCard
      key={decisionPackage.package_id}
      decisionPackage={decisionPackage}
      membersById={membersById}
      unlinkedAiProposals={model.unlinkedObservations}
      paperPortfolioById={model.portfolioById}
      walkForwardRunsByPortfolio={walkForwardRunsByPortfolio}
      onCreatePortfolio={onCreatePortfolio}
      onCreateObservation={onCreateObservation}
      onBindObservation={onBindObservation}
    />
  );

  const hasUnlinked = model.unlinkedPortfolios.length
    || model.unlinkedObservations.length
    || model.historicalWalkForwardCount
    || model.unlinkedWalkForwardCount;

  return (
    <div className="decision-lineage-panel">
      <div className="decision-lineage-intro">
        <GitBranch size={15} />
        <p><strong>决策研究谱系</strong><span>组合之后分成前瞻观察与历史回放两条验证轨，二者互不冒充。</span></p>
      </div>

      {model.current.length ? model.current.map(renderPackage) : (
        <div className="decision-lineage-empty">
          <CircleDashed size={15} />
          <span><strong>尚无当前决策链</strong><small>确认候选产物并由用户支持、保留或退回后，这里才建立版本锚点。</small></span>
        </div>
      )}

      {model.historical.length ? (
        <details className="decision-lineage-history">
          <summary><History size={13} />历史决定链（{model.historical.length}）</summary>
          <div>{model.historical.map(renderPackage)}</div>
        </details>
      ) : null}

      {hasUnlinked ? (
        <div className="decision-lineage-unlinked">
          <Unlink size={14} />
          <span>
            <strong>未关联与历史资源</strong>
            <small>
              未关联组合 {model.unlinkedPortfolios.length} · 未关联观察 {model.unlinkedObservations.length}
              {model.historicalWalkForwardCount ? ` · 旧版本回放 ${model.historicalWalkForwardCount}` : ""}
              {model.unlinkedWalkForwardCount ? ` · 未关联回放 ${model.unlinkedWalkForwardCount}` : ""}
            </small>
          </span>
        </div>
      ) : null}
    </div>
  );
}
