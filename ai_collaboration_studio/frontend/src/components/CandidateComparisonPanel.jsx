import { AlertTriangle, BarChart3, CheckCircle2, ShieldCheck } from "lucide-react";
import { memo, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  candidateComparisonEligibility,
  candidateComparisonErrorMessage,
  candidateComparisonSelectionFingerprint,
  candidateComparisonView,
} from "../candidateComparisonView";
import "../styles/candidate-comparison-refinement.css";


const SCENARIO_LABELS = Object.freeze({
  baseline: "基准摩擦",
  stressed: "压力摩擦",
  severe: "极端摩擦",
});
const SCENARIO_ENTRIES = Object.freeze(Object.entries(SCENARIO_LABELS));
const SCENARIO_IDS = Object.freeze(SCENARIO_ENTRIES.map(([id]) => id));
const RUN_SELECTOR_PAGE_SIZE = 18;


function percent(value, digits = 1) {
  return typeof value === "number" && Number.isFinite(value)
    ? `${value.toFixed(digits)}%`
    : "—";
}


function ratioPercent(value) {
  return typeof value === "number" && Number.isFinite(value) ? percent(value * 100) : "—";
}


function shortHash(value) {
  const text = String(value || "").trim();
  return /^[0-9a-f]{64}$/i.test(text) ? `${text.slice(0, 8)}…${text.slice(-6)}` : "—";
}


function formatSymbol(value) {
  const normalized = typeof value === "string" ? value.trim().slice(0, 48) : "";
  return normalized ? normalized.replace(/^US\./, "") : "标的缺失";
}


const ScenarioCell = memo(function ScenarioCell({ scenario }) {
  if (!scenario) return <span className="candidate-comparison-missing">未返回</span>;
  if (scenario.blocked) {
    return (
      <span className="candidate-comparison-blocked">
        容量阻断
        {scenario.capacityGapUsd !== null
          ? <small>缺口 ${Math.round(scenario.capacityGapUsd).toLocaleString("en-US")} USD</small>
          : null}
      </span>
    );
  }
  if (!scenario.metricsVisible) {
    return <span className="candidate-comparison-missing">指标已隐藏</span>;
  }
  return (
    <span className="candidate-comparison-metrics">
      <strong>{percent(scenario.cumulativeReturnPct)}</strong>
      <small>历史正收益窗 {ratioPercent(scenario.historicalPositiveWindowRatio)}</small>
      <small>最大回撤 {percent(scenario.maxDrawdownPct)}</small>
    </span>
  );
});


const RunSelectorOption = memo(function RunSelectorOption({ run, selected, disabled, onToggle }) {
  return (
    <label className={selected ? "selected" : ""}>
      <input
        type="checkbox"
        checked={selected}
        disabled={disabled}
        onChange={() => onToggle(run.runId)}
      />
      <span>
        <strong>{run.candidateTitle}</strong>
        <small>
          {formatSymbol(run.symbol)} · {run.direction || "?"}→{run.side || "?"}
          {` · ${percent(run.weightPct, 0)} · ${run.horizonDays || "?"} 日`}
        </small>
        <small>
          {run.portfolioName} · v{run.portfolioVersion}
          {run.actionableNow ? " · 当前版本匹配" : " · 历史审计"}
        </small>
      </span>
    </label>
  );
});


const ComparisonCandidateRow = memo(function ComparisonCandidateRow({ candidate }) {
  const scenariosById = useMemo(
    () => new Map(candidate.scenarios.map((scenario) => [scenario.id, scenario])),
    [candidate.scenarios],
  );
  return (
    <tr>
      <th scope="row">
        <strong>{candidate.title}</strong>
        <small>{formatSymbol(candidate.symbol)} · {candidate.direction}→{candidate.side}</small>
        <small>{percent(candidate.weightPct, 0)} · {candidate.horizonDays} 日</small>
      </th>
      {SCENARIO_IDS.map((scenarioId) => (
        <td key={scenarioId}>
          <ScenarioCell scenario={scenariosById.get(scenarioId)} />
        </td>
      ))}
    </tr>
  );
});


export const CandidateComparisonPanel = memo(function CandidateComparisonPanel({
  portfolios,
  runsByPortfolio,
  comparison,
  loading,
  error,
  onCompare,
}) {
  const titleId = useId();
  const resultTitleId = useId();
  const eligibility = useMemo(
    () => candidateComparisonEligibility(portfolios, runsByPortfolio),
    [portfolios, runsByPortfolio],
  );
  const eligibleRuns = eligibility.runs;
  const eligibleFingerprint = useMemo(
    () => candidateComparisonSelectionFingerprint(eligibleRuns.map((run) => run.runId)),
    [eligibleRuns],
  );
  const [selectedRunIds, setSelectedRunIds] = useState([]);
  const [runLimit, setRunLimit] = useState(RUN_SELECTOR_PAGE_SIZE);
  const [acknowledged, setAcknowledged] = useState(false);
  const [localError, setLocalError] = useState("");
  const submissionRef = useRef(false);
  const view = useMemo(
    () => (comparison ? candidateComparisonView(comparison) : null),
    [comparison],
  );
  const selectedFingerprint = useMemo(
    () => candidateComparisonSelectionFingerprint(selectedRunIds),
    [selectedRunIds],
  );
  const visibleView = useMemo(
    () => candidateComparisonSelectionFingerprint(view?.selectedRunIds) === selectedFingerprint
      ? view
      : null,
    [selectedFingerprint, view],
  );
  const selectedRunSet = useMemo(() => new Set(selectedRunIds), [selectedRunIds]);
  const visibleEligibleRuns = useMemo(
    () => eligibleRuns.filter((run, index) => index < runLimit || selectedRunSet.has(run.runId)),
    [eligibleRuns, runLimit, selectedRunSet],
  );
  const hiddenRunCount = eligibleRuns.length - visibleEligibleRuns.length;
  const selectionReady = selectedRunIds.length >= 2 && selectedRunIds.length <= 6;
  const minimumComparableRuns = 2;
  const missingComparableRuns = Math.max(0, minimumComparableRuns - eligibleRuns.length);
  const readinessTone = !eligibility.integrityOk
    ? "blocked"
    : missingComparableRuns > 0 ? "waiting" : "ready";
  const readinessLabel = readinessTone === "blocked"
    ? "比较输入完整性阻断"
    : readinessTone === "waiting"
      ? `还需 ${missingComparableRuns} 条已验证回放`
      : "数量前提已满足";
  const readinessDetail = readinessTone === "blocked"
    ? "输入集合未通过完整性门，比较入口保持关闭。"
    : readinessTone === "waiting"
      ? "至少需要 2 条当前合同一致、完整性通过的历史回放。"
      : "仍需选择 2–6 条并确认仅用于历史模拟复核；这不是批准。";
  const canCompare = eligibility.integrityOk
    && typeof onCompare === "function"
    && !loading
    && acknowledged
    && selectionReady;

  useEffect(() => {
    const availableRunIds = new Set(eligibleRuns.map((run) => run.runId));
    setSelectedRunIds((current) => current.filter((runId) => availableRunIds.has(runId)));
    setRunLimit(RUN_SELECTOR_PAGE_SIZE);
    setAcknowledged(false);
    setLocalError("");
  }, [eligibleFingerprint, eligibleRuns]);

  const toggleRun = useCallback((runId) => {
    if (loading) return;
    setAcknowledged(false);
    setLocalError("");
    setSelectedRunIds((current) => {
      if (current.includes(runId)) return current.filter((item) => item !== runId);
      if (current.length >= 6) return current;
      return [...current, runId];
    });
  }, [loading]);

  const submit = async () => {
    if (!canCompare || submissionRef.current) return;
    submissionRef.current = true;
    setLocalError("");
    try {
      await onCompare([...selectedRunIds]);
    } catch (requestError) {
      setLocalError(candidateComparisonErrorMessage(requestError));
    } finally {
      submissionRef.current = false;
    }
  };

  return (
    <section className="candidate-comparison-panel candidate-comparison-workbench" aria-labelledby={titleId} aria-busy={loading}>
      <header className="candidate-comparison-heading">
        <span>
          <h4 id={titleId}><BarChart3 size={14} aria-hidden="true" />已验证回放同口径复核</h4>
          <small>从已有固定方向历史回放中复核；不是决定前的一次性 A/B/C 联合回放</small>
        </span>
        <em><data value={eligibleRuns.length}>{eligibleRuns.length}</data> 条可选记录</em>
      </header>

      <div className="candidate-comparison-scope" role="list" aria-label="候选历史比较范围">
        <span role="listitem"><small>输入</small><strong>已验证历史回放</strong></span>
        <span role="listitem"><small>共同口径</small><strong>同数据 · 同窗口 · 同摩擦</strong></span>
        <span role="listitem"><small>不产生</small><strong>排名 · 赢家 · 授权</strong></span>
      </div>

      <div className={`candidate-comparison-readiness ${readinessTone}`} role="note">
        {readinessTone === "ready"
          ? <CheckCircle2 aria-hidden="true" size={15} />
          : <AlertTriangle aria-hidden="true" size={15} />}
        <span><strong>{readinessLabel}</strong><small>{readinessDetail}</small></span>
        <data
          value={Math.min(eligibleRuns.length, minimumComparableRuns)}
          aria-label={readinessTone === "blocked" ? "比较输入完整性阻断" : `${eligibleRuns.length} / ${minimumComparableRuns} 条数量前提`}
        >
          {readinessTone === "blocked" ? "BLOCKED" : `${eligibleRuns.length} / ${minimumComparableRuns}`}
        </data>
      </div>

      {!eligibility.integrityOk ? (
        <p className="candidate-comparison-source-warning" role="alert">{eligibility.issue}</p>
      ) : eligibleRuns.length >= 2 ? (
        <>
          <fieldset className="candidate-comparison-selector" disabled={loading || !eligibility.integrityOk}>
            <legend>选择 2–6 条已验证且同类的历史回放</legend>
            {visibleEligibleRuns.map((run) => {
              const selected = selectedRunSet.has(run.runId);
              return (
                <RunSelectorOption
                  disabled={loading || (!selected && selectedRunIds.length >= 6)}
                  key={run.runId}
                  onToggle={toggleRun}
                  run={run}
                  selected={selected}
                />
              );
            })}
          </fieldset>
          <p className="candidate-comparison-selector-status" role="status" aria-live="polite">
            展示 {visibleEligibleRuns.length} / {eligibleRuns.length} 条可比较回放；已选 {selectedRunIds.length} / 6。
          </p>
          {hiddenRunCount ? (
            <button
              type="button"
              className="secondary compact candidate-comparison-more"
              disabled={loading}
              onClick={() => setRunLimit((current) => current + RUN_SELECTOR_PAGE_SIZE)}
            >
              再显示 {Math.min(RUN_SELECTOR_PAGE_SIZE, hiddenRunCount)} 条回放
            </button>
          ) : null}
          <label className="candidate-comparison-acknowledgement">
            <input
              type="checkbox"
              checked={acknowledged}
              disabled={loading || !selectionReady || typeof onCompare !== "function"}
              onChange={(event) => setAcknowledged(event.target.checked)}
            />
            <span>我确认这只是历史模拟比较；“历史正收益窗口比例”不是未来胜率，也不是交易指令。</span>
          </label>
          <button
            className="secondary compact candidate-comparison-run"
            type="button"
            disabled={!canCompare}
            aria-busy={loading}
            onClick={submit}
          >
            <BarChart3 size={12} aria-hidden="true" />
            {loading ? "正在核验同一冻结基准…" : `比较已选 ${selectedRunIds.length} 个候选`}
          </button>
        </>
      ) : (
        <div className="candidate-comparison-empty" role="note">
          <BarChart3 aria-hidden="true" size={16} />
          <span>
            <strong>尚无足够的同口径历史回放</strong>
            <small>先完成并核验候选纸面组合的版本化历史回放，再返回此处复核。</small>
          </span>
          <ul aria-label="候选比较开放前提">
            <li>确认组合版本</li>
            <li>完整性通过</li>
            <li>同类候选合同</li>
          </ul>
        </div>
      )}

      {error || localError ? <p className="candidate-comparison-error" role="alert"><AlertTriangle size={13} aria-hidden="true" />{candidateComparisonErrorMessage({ message: error || localError })}</p> : null}
      {visibleView && !visibleView.ready ? (
        <div className="candidate-comparison-result blocked" role="alert">
          <AlertTriangle size={15} aria-hidden="true" />
          <span>
            <strong>比较已失败关闭，所有收益指标均隐藏</strong>
            <small>{visibleView.issues?.[0]?.message || "冻结数据、期限、权重或完整性不一致。"}</small>
          </span>
        </div>
      ) : null}
      {visibleView?.ready ? (
        <div className="candidate-comparison-result ready" role="region" aria-labelledby={resultTitleId}>
          <div className="candidate-comparison-proof" role="status" aria-live="polite">
            <ShieldCheck size={15} aria-hidden="true" />
            <span>
              <h5 id={resultTitleId}>同一冻结基准已核验</h5>
              <small>
                {visibleView.basis.actual_start} → {visibleView.basis.actual_end}
                {` · ${visibleView.basis.common_trading_days} 个共同交易日 · ${visibleView.basis.test_days} 日窗口`}
              </small>
              <small>比较基准 {shortHash(visibleView.basisSha256)}</small>
            </span>
          </div>
          <div className="candidate-comparison-neutrality-ledger" role="list" aria-label="历史比较中立性边界">
            <span role="listitem"><small>同基准候选</small><strong>{visibleView.candidates.length}</strong></span>
            <span role="listitem"><small>共同摩擦情景</small><strong>{SCENARIO_ENTRIES.length}</strong></span>
            <span role="listitem"><small>排名 / 赢家</small><strong>均不产生</strong></span>
            <span role="listitem"><small>授权输出</small><strong>不产生</strong></span>
          </div>
          <div className="candidate-comparison-table-wrap" role="region" aria-label="同口径历史回放比较表" tabIndex={0}>
            <table className="candidate-comparison-table">
              <caption>相同冻结数据和摩擦口径下的已有候选历史回放指标</caption>
              <thead>
                <tr>
                  <th scope="col">候选</th>
                  {SCENARIO_ENTRIES.map(([id, label]) => <th scope="col" key={id}>{label}</th>)}
                </tr>
              </thead>
              <tbody>
                {visibleView.candidates.map((candidate) => (
                  <ComparisonCandidateRow candidate={candidate} key={candidate.runId} />
                ))}
              </tbody>
            </table>
          </div>
          <p className="candidate-comparison-safety" role="note">
            <ShieldCheck size={13} aria-hidden="true" />不产生排名、赢家或自动决定；最终方案仍由用户结合证据、反证和风险条件确认。
          </p>
        </div>
      ) : null}
    </section>
  );
});
