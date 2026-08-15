import { AlertTriangle, BarChart3, CheckCircle2, ShieldCheck } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  candidateComparisonEligibleRuns,
  candidateComparisonView,
} from "../candidateComparisonView";


const SCENARIO_LABELS = Object.freeze({
  baseline: "基准摩擦",
  stressed: "压力摩擦",
  severe: "极端摩擦",
});


function percent(value, digits = 1) {
  return typeof value === "number" && Number.isFinite(value)
    ? `${value.toFixed(digits)}%`
    : "—";
}


function ratioPercent(value) {
  return typeof value === "number" && Number.isFinite(value) ? percent(value * 100) : "—";
}


function shortHash(value) {
  const text = String(value || "");
  return text.length === 64 ? `${text.slice(0, 8)}…${text.slice(-6)}` : "—";
}


function scenarioCell(scenario) {
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
}


export function CandidateComparisonPanel({
  portfolios,
  runsByPortfolio,
  comparison,
  loading,
  error,
  onCompare,
}) {
  const eligibleRuns = useMemo(
    () => candidateComparisonEligibleRuns(portfolios, runsByPortfolio),
    [portfolios, runsByPortfolio],
  );
  const eligibleFingerprint = useMemo(
    () => eligibleRuns.map((run) => run.runId).join("|"),
    [eligibleRuns],
  );
  const [selectedRunIds, setSelectedRunIds] = useState([]);
  const [acknowledged, setAcknowledged] = useState(false);
  const view = useMemo(
    () => (comparison ? candidateComparisonView(comparison) : null),
    [comparison],
  );
  const selectedFingerprint = selectedRunIds.join("|");
  const visibleView = view?.selectedRunIds.join("|") === selectedFingerprint ? view : null;

  useEffect(() => {
    const available = new Set(eligibleFingerprint.split("|").filter(Boolean));
    setSelectedRunIds((current) => current.filter((runId) => available.has(runId)));
    setAcknowledged(false);
  }, [eligibleFingerprint]);

  const toggleRun = (runId) => {
    if (loading) return;
    setAcknowledged(false);
    setSelectedRunIds((current) => {
      if (current.includes(runId)) return current.filter((item) => item !== runId);
      if (current.length >= 6) return current;
      return [...current, runId];
    });
  };

  const submit = () => {
    if (
      loading
      || !acknowledged
      || selectedRunIds.length < 2
      || selectedRunIds.length > 6
    ) return;
    onCompare(selectedRunIds);
  };

  return (
    <section className="candidate-comparison-panel" aria-label="候选方案同口径历史比较">
      <header className="candidate-comparison-heading">
        <span>
          <strong><BarChart3 size={14} />已验证回放同口径复核</strong>
          <small>从已有固定方向历史回放中复核；不是决定前的一次性 A/B/C 联合回放</small>
        </span>
        <em>{eligibleRuns.length} 条可选记录</em>
      </header>

      {eligibleRuns.length >= 2 ? (
        <>
          <div className="candidate-comparison-selector">
            {eligibleRuns.map((run) => (
              <label key={run.runId} className={selectedRunIds.includes(run.runId) ? "selected" : ""}>
                <input
                  type="checkbox"
                  checked={selectedRunIds.includes(run.runId)}
                  disabled={loading || (!selectedRunIds.includes(run.runId) && selectedRunIds.length >= 6)}
                  onChange={() => toggleRun(run.runId)}
                />
                <span>
                  <strong>{run.candidateTitle}</strong>
                  <small>
                    {run.symbol.replace("US.", "") || "标的缺失"} · {run.direction || "?"}→{run.side || "?"}
                    {` · ${percent(run.weightPct, 0)} · ${run.horizonDays || "?"} 日`}
                  </small>
                  <small>
                    {run.portfolioName} · v{run.portfolioVersion}
                    {run.actionableNow ? " · 当前可行动" : " · 历史审计"}
                  </small>
                </span>
              </label>
            ))}
          </div>
          <label className="candidate-comparison-acknowledgement">
            <input
              type="checkbox"
              checked={acknowledged}
              disabled={loading}
              onChange={(event) => setAcknowledged(event.target.checked)}
            />
            <span>我确认这只是历史模拟比较；“历史正收益窗口比例”不是未来胜率，也不是交易指令。</span>
          </label>
          <button
            className="secondary compact candidate-comparison-run"
            type="button"
            disabled={loading || !acknowledged || selectedRunIds.length < 2}
            aria-busy={loading}
            onClick={submit}
          >
            <BarChart3 size={12} />
            {loading ? "正在核验同一冻结基准…" : `比较已选 ${selectedRunIds.length} 个候选`}
          </button>
        </>
      ) : (
        <p className="candidate-comparison-empty">
          至少需要两条已经完成且完整性通过的候选回放，才会开放同口径比较。
        </p>
      )}

      {error ? <p className="candidate-comparison-error" role="alert"><AlertTriangle size={13} />{error}</p> : null}
      {visibleView && !visibleView.ready ? (
        <div className="candidate-comparison-result blocked" role="alert">
          <AlertTriangle size={15} />
          <span>
            <strong>比较已失败关闭，所有收益指标均隐藏</strong>
            <small>{visibleView.issues[0]?.message || "冻结数据、期限、权重或完整性不一致。"}</small>
          </span>
        </div>
      ) : null}
      {visibleView?.ready ? (
        <div className="candidate-comparison-result ready" aria-live="polite">
          <div className="candidate-comparison-proof">
            <CheckCircle2 size={15} />
            <span>
              <strong>同一冻结基准已通过</strong>
              <small>
                {visibleView.basis.actual_start} → {visibleView.basis.actual_end}
                {` · ${visibleView.basis.common_trading_days} 个共同交易日 · ${visibleView.basis.test_days} 日窗口`}
              </small>
              <small>比较基准 {shortHash(visibleView.basisSha256)}</small>
            </span>
          </div>
          <div className="candidate-comparison-table-wrap">
            <table className="candidate-comparison-table">
              <caption>相同冻结数据和摩擦口径下的已有候选历史回放指标</caption>
              <thead>
                <tr>
                  <th scope="col">候选</th>
                  {Object.entries(SCENARIO_LABELS).map(([id, label]) => <th scope="col" key={id}>{label}</th>)}
                </tr>
              </thead>
              <tbody>
                {visibleView.candidates.map((candidate) => (
                  <tr key={candidate.runId}>
                    <th scope="row">
                      <strong>{candidate.title}</strong>
                      <small>{candidate.symbol.replace("US.", "")} · {candidate.direction}→{candidate.side}</small>
                      <small>{percent(candidate.weightPct, 0)} · {candidate.horizonDays} 日</small>
                    </th>
                    {Object.keys(SCENARIO_LABELS).map((scenarioId) => (
                      <td key={scenarioId}>
                        {scenarioCell(candidate.scenarios.find((scenario) => scenario.id === scenarioId))}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="candidate-comparison-safety">
            <ShieldCheck size={13} />不产生排名、赢家或自动决定；最终方案仍由用户结合证据、反证和风险条件确认。
          </p>
        </div>
      ) : null}
    </section>
  );
}
