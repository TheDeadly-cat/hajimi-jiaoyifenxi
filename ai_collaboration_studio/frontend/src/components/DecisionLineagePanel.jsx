import {
  AlertTriangle,
  ArrowDown,
  BarChart3,
  ChevronDown,
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
import { memo, useEffect, useId, useMemo, useRef, useState } from "react";
import { bindableAiProposals } from "../observationLineage";
import {
  buildDecisionLineagePanelModel,
  createdTime,
  decisionLineageRows,
  decisionPackageKey,
  eventRevision,
  isObservationEvent,
  isPortfolioEvent,
  isWalkForwardEvent,
  latestResourceEvents,
  lineageDisplayText,
  packageEvents,
  runPortfolioVersion,
  shortId,
  walkForwardState,
} from "../decisionLineageView";
import "../styles/decision-lineage.css";

export { buildPortfolioLineageIndex, decisionPackageSource } from "../decisionLineageView";


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
const EMPTY_LIST = Object.freeze([]);
const EMPTY_RECORD = Object.freeze({});
const EMPTY_MEMBER_MAP = new Map();


function formatSymbol(value) {
  const normalized = typeof value === "string" ? value.trim().slice(0, 48) : "";
  return normalized ? normalized.replace(/^US\./, "") : "—";
}


const LineageNode = memo(function LineageNode({ icon: Icon, eyebrow, title, detail, tone = "pending" }) {
  return (
    <div className={`decision-lineage-node ${tone}`} data-node-state={tone}>
      <Icon aria-hidden="true" size={14} />
      <span>
        <small>{eyebrow}</small>
        <strong>{title}</strong>
        <em>{detail}</em>
      </span>
    </div>
  );
});


const ProposalBindingControl = memo(function ProposalBindingControl({
  proposals = EMPTY_LIST,
  membersById = EMPTY_MEMBER_MAP,
  anchor,
  sourceBranch,
  onBind,
}) {
  const [proposalId, setProposalId] = useState("");
  const [derivationNote, setDerivationNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const requestRef = useRef(0);
  const noteHelpId = useId();
  const sourceKey = useMemo(() => JSON.stringify([
      anchor?.user_decision_id || "",
      sourceBranch?.event?.resource_id || "",
      sourceBranch?.revision || 0,
      proposals.map((item) => item.id),
    ]), [anchor, proposals, sourceBranch]);
  useEffect(() => {
    requestRef.current += 1;
    setProposalId("");
    setDerivationNote("");
    setBusy(false);
    setError("");
    return () => { requestRef.current += 1; };
  }, [sourceKey]);
  if (!sourceBranch || typeof onBind !== "function" || !proposals.length) return null;
  const effectiveProposalId = proposals.some((item) => item.id === proposalId)
    ? proposalId
    : proposals[0]?.id || "";
  const selected = proposals.find((item) => item.id === effectiveProposalId) || proposals[0];
  const submit = async (event) => {
    event.preventDefault();
    const note = derivationNote.trim();
    if (!selected || note.length < 3 || busy) return;
    const sequence = requestRef.current + 1;
    requestRef.current = sequence;
    setBusy(true);
    setError("");
    try {
      await onBind(selected, {
        user_decision_id: String(anchor.user_decision_id || ""),
        source_portfolio_id: String(sourceBranch.event?.resource_id || ""),
        source_portfolio_version: Number(sourceBranch.revision) || 0,
        derivation_note: note,
      });
      if (requestRef.current !== sequence) return;
      setDerivationNote("");
    } catch (requestError) {
      if (requestRef.current !== sequence) return;
      setError(requestError instanceof Error && requestError.message.trim()
        ? requestError.message.trim().slice(0, 1000)
        : "绑定失败，请检查决定与组合版本。");
    } finally {
      if (requestRef.current === sequence) setBusy(false);
    }
  };
  return (
    <details className="decision-lineage-proposal-bind">
      <summary>
        <Link2 aria-hidden="true" size={13} />
        <span>采纳 AI 原提案（{proposals.length}）</span>
        <ChevronDown className="decision-lineage-disclosure-chevron" aria-hidden="true" size={14} />
      </summary>
      <form aria-busy={busy} onSubmit={submit}>
        <label>待绑定提案
          <select disabled={busy} value={effectiveProposalId} onChange={(event) => setProposalId(event.target.value)}>
            {proposals.map((item, proposalIndex) => (
              <option key={`${item.id}:${proposalIndex}`} value={item.id}>
                {membersById.get(String(item.created_by || ""))?.name || "历史 AI"}
                {` · ${formatSymbol(item.symbol)} · ${item.direction} · ${item.horizon_days}日 · ${item.model_confidence ?? "?"}%`}
              </option>
            ))}
          </select>
        </label>
        <label>采纳说明
          <textarea
            autoComplete="off"
            disabled={busy}
            required
            minLength={3}
            maxLength={1000}
            aria-describedby={noteHelpId}
            value={derivationNote}
            onChange={(event) => setDerivationNote(event.target.value)}
            placeholder="说明该 AI 提案如何验证当前支持方案及已确认组合；原作者、方法、阈值和置信度不会被改写。"
          />
          <span id={noteHelpId} className="decision-lineage-note-meter">
            <span>至少 3 字；原提案字段保持只读。</span>
            <output aria-label={`已输入 ${derivationNote.length} / 1000 字`}>
              {derivationNote.length} / 1000
            </output>
          </span>
        </label>
        {error ? <p className="decision-lineage-proposal-error" role="alert">{error}</p> : null}
        <button type="submit" disabled={busy || derivationNote.trim().length < 3}>
          {busy ? <LoaderCircle aria-hidden="true" className="spin" size={11} /> : <Link2 aria-hidden="true" size={11} />}
          {busy ? "正在绑定…" : "绑定原提案"}
        </button>
      </form>
    </details>
  );
});


const DecisionPackageCard = memo(function DecisionPackageCard({
  decisionPackage,
  membersById,
  unlinkedAiProposals,
  paperPortfolioById,
  walkForwardRunsByPortfolio,
  modelIntegrityOk,
  onCreatePortfolio,
  onCreateObservation,
  onBindObservation,
}) {
  const packageTitleId = useId();
  const anchor = decisionPackage.anchor || {};
  const events = packageEvents(decisionPackage);
  const portfolioEvents = latestResourceEvents(events, isPortfolioEvent);
  const observationEvents = latestResourceEvents(events, isObservationEvent);
  const explicitWalkForwardEvents = latestResourceEvents(events, isWalkForwardEvent);
  const selectedOption = anchor.selected_option || {};
  const selectedOptionTitle = lineageDisplayText(
    selectedOption.title,
    lineageDisplayText(selectedOption.name, "候选方案快照不可用"),
  );
  const state = PACKAGE_STATES[decisionPackage.state] || PACKAGE_STATES.stale;
  const safeBoundary = decisionPackage.execution_capability === "none"
    && decisionPackage.live_trading_allowed === false
    && decisionPackage.can_autonomously_decide === false;
  const integrityReady = modelIntegrityOk
    && decisionPackage.integrity_ok === true
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
  const portfolioCreationAvailable = canCreatePortfolio && typeof onCreatePortfolio === "function";

  const portfolioBranches = portfolioEvents.map((event) => {
    const portfolio = paperPortfolioById.get(String(event.resource_id));
    const revision = Number(eventRevision(event)) || 0;
    const exactLiveRevision = Number(portfolio?.version || 0) === revision;
    const mayShowRevision = decisionPackage.state === "stale" || exactLiveRevision;
    const runs = decisionLineageRows(mayShowRevision ? walkForwardRunsByPortfolio?.[event.resource_id] : [])
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
    <article
      className={`decision-package-card ${state.tone}${packageBroken ? " broken" : ""}`}
      data-package-state={packageBroken ? "broken" : state.tone}
      aria-labelledby={packageTitleId}
    >
      <header>
        <span>
          <GitBranch aria-hidden="true" size={14} />
          <h4 id={packageTitleId}>
            {state.label}
            <span className="decision-lineage-visually-hidden">：{selectedOptionTitle}</span>
          </h4>
        </span>
        <em>{createdTime(anchor.created_at)}</em>
      </header>

      <div className="decision-lineage-primary-flow">
        <LineageNode
          icon={FileCheck2}
          eyebrow="候选产物"
          title={selectedOptionTitle}
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
                  title={lineageDisplayText(
                    portfolio?.name,
                    lineageDisplayText(event.resource_snapshot?.name, shortId(event.resource_id)),
                  )}
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
            title={portfolioCreationAvailable ? "尚未建立关联组合" : canCreatePortfolio ? "组合创建入口不可用" : "当前决定没有可派生组合"}
            detail={canCreatePortfolio ? "从已支持候选推导纸面权重与风险预算" : "不会从保留、退回或旧决定自动派生"}
            tone={packageBroken ? "broken" : "pending"}
          />
        )}
      </div>

      {portfolioCreationAvailable ? (
        <button className="decision-lineage-create" type="button" onClick={(event) => onCreatePortfolio(decisionPackage, event.currentTarget)}>
          <Plus aria-hidden="true" size={13} />建立关联模拟组合
        </button>
      ) : null}
      {canCreatePortfolioBase && candidateSimulationBlocked ? (
        <p className="decision-lineage-warning">
          当前正式候选不能建立严格模拟映射：
          {lineageDisplayText(candidateSimulationSeed?.issues?.[0]?.message, "候选规格不完整。")}
        </p>
      ) : null}

      <div className="decision-lineage-validation" aria-label="并行验证轨">
        <div className="decision-lineage-lane observation">
          <FlaskConical aria-hidden="true" size={14} />
          <span>
            <small>前瞻验证轨</small>
            <strong>{currentObservationEvents.length ? `${currentObservationEvents.length} 条当前版本观察` : "尚无当前版本观察"}</strong>
            <em>
              {observationStateDetail ? `${observationStateDetail} · ` : ""}用户确认后等待到期；不作为回放输入。
              {observationEvents.length > currentObservationEvents.length ? ` 另有 ${observationEvents.length - currentObservationEvents.length} 条历史版本。` : ""}
            </em>
          </span>
          {observationSourceBranch && typeof onCreateObservation === "function" ? (
            <button
              className="decision-lineage-lane-action"
              type="button"
              onClick={(event) => onCreateObservation(decisionPackage, observationSourceBranch, event.currentTarget)}
            >
              <Plus aria-hidden="true" size={11} />建立前向观察
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
          <BarChart3 aria-hidden="true" size={14} />
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
        <p className="decision-lineage-warning"><AlertTriangle aria-hidden="true" size={12} />完整性或安全边界校验失败；该链仅供审计，不能继续派生。</p>
      ) : (
        <p className="decision-lineage-boundary"><ShieldCheck aria-hidden="true" size={12} />仅研究与模拟 · 无自主决策权 · 禁止实盘</p>
      )}
    </article>
  );
});


export const DecisionLineagePanel = memo(function DecisionLineagePanel({
  decisionPackages = EMPTY_LIST,
  members = EMPTY_LIST,
  paperPortfolios = EMPTY_LIST,
  observations = EMPTY_LIST,
  walkForwardRunsByPortfolio = EMPTY_RECORD,
  onCreatePortfolio,
  onCreateObservation,
  onBindObservation,
}) {
  const titleId = useId();
  const model = useMemo(() => buildDecisionLineagePanelModel({
    decisionPackages,
    members,
    observations,
    paperPortfolios,
    walkForwardRunsByPortfolio,
  }), [decisionPackages, members, observations, paperPortfolios, walkForwardRunsByPortfolio]);
  const reviewCount = model.stats.broken + model.stats.unlinked;
  const reviewSignal = !model.integrityOk
    ? {
      tone: "blocked",
      label: "谱系输入已阻断",
      detail: "输入集合未通过完整性门，所有继续派生动作保持关闭。",
    }
    : reviewCount > 0
      ? {
        tone: "attention",
        label: `${reviewCount} 项谱系信号需要复核`,
        detail: `异常链 ${model.stats.broken} · 未关联资源 ${model.stats.unlinked}；计数不用于排序或批准。`,
      }
      : {
        tone: "clear",
        label: "谱系结构暂无待复核项",
        detail: "仅表示当前输入没有异常链或未关联资源，不代表研究结论获批。",
      };

  const renderPackage = (decisionPackage, index) => (
    <DecisionPackageCard
      key={decisionPackageKey(decisionPackage, index)}
      decisionPackage={decisionPackage}
      membersById={model.membersById}
      modelIntegrityOk={model.integrityOk}
      unlinkedAiProposals={model.unlinkedObservations}
      paperPortfolioById={model.portfolioById}
      walkForwardRunsByPortfolio={model.walkForwardRunsByPortfolio}
      onCreatePortfolio={onCreatePortfolio}
      onCreateObservation={onCreateObservation}
      onBindObservation={onBindObservation}
    />
  );

  return (
    <section className="decision-lineage-panel" aria-labelledby={titleId} data-model-state={model.integrityOk ? "verified" : "blocked"}>
      <header className="decision-lineage-intro">
        <div className="decision-lineage-intro-mark"><GitBranch aria-hidden="true" size={18} /><span>PROVENANCE<br />LEDGER</span></div>
        <div><small>DECISION → SIMULATION → VALIDATION</small><h3 id={titleId}>决策研究谱系</h3><p>组合之后分成前瞻观察与历史回放两条验证轨，二者互不冒充。</p></div>
        <dl aria-label="决策谱系摘要">
          <div data-stat="current"><dt>当前</dt><dd><data value={model.stats.current}>{model.stats.current}</data></dd></div>
          <div data-stat="historical"><dt>历史</dt><dd><data value={model.stats.historical}>{model.stats.historical}</data></dd></div>
          <div data-stat="broken"><dt>异常</dt><dd><data value={model.stats.broken}>{model.stats.broken}</data></dd></div>
          <div data-stat="unlinked"><dt>未关联</dt><dd><data value={model.stats.unlinked}>{model.stats.unlinked}</data></dd></div>
        </dl>
      </header>

      <div className={`decision-lineage-review-signal ${reviewSignal.tone}`} role="note">
        {reviewSignal.tone === "clear"
          ? <ShieldCheck aria-hidden="true" size={15} />
          : <AlertTriangle aria-hidden="true" size={15} />}
        <span><strong>{reviewSignal.label}</strong><small>{reviewSignal.detail}</small></span>
        <data
          value={reviewCount}
          aria-label={model.integrityOk ? `${reviewCount} 项待复核信号` : "完整性阻断"}
        >
          {model.integrityOk ? `${reviewCount} 项` : "BLOCKED"}
        </data>
      </div>

      {!model.integrityOk ? <p className="decision-lineage-model-warning" role="alert"><AlertTriangle aria-hidden="true" size={13} />谱系输入无法完整核验，所有继续派生动作已关闭。{model.issues?.[0] || "输入集合无效。"}</p> : null}

      {model.current.length ? model.current.map(renderPackage) : (
        <div className="decision-lineage-empty">
          <CircleDashed aria-hidden="true" size={15} />
          <span><strong>尚无当前决策链</strong><small>确认候选产物并由用户支持、保留或退回后，这里才建立版本锚点。</small></span>
        </div>
      )}

      {model.historical.length ? (
        <details className="decision-lineage-history">
          <summary>
            <History aria-hidden="true" size={14} />
            <span>历史决定链（{model.historical.length}）</span>
            <ChevronDown className="decision-lineage-disclosure-chevron" aria-hidden="true" size={15} />
          </summary>
          <div>{model.historical.map(renderPackage)}</div>
        </details>
      ) : null}

      {model.hasUnlinked ? (
        <div className="decision-lineage-unlinked">
          <Unlink aria-hidden="true" size={14} />
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
    </section>
  );
});
